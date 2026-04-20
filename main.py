#!/usr/bin/env python3
"""CLI for the browser agent: scrape websites, extract orgs/emails, export to Google Sheets, send outreach emails."""

import argparse
import sys
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlparse

from src.browser import load_page
from src.config import (
    EMAIL_DAILY_LIMIT,
    EMAIL_DELAY_MAX_SEC,
    EMAIL_DELAY_MIN_SEC,
    OUTREACH_SHEET_ID,
    OUTREACH_STATE_PATH,
)
from src.emailer import send_outreach
from src.outreach_state import load_state, record_successful_send, remaining_quota, save_state
from src.outreach_state import acquire_send_lock
from src.reply_sync import probe_sent_mail_for_address, sync_email_status_from_gmail
from src.extractor import (
    ExtractedData,
    _get_company_website_from_yc_page,
    extract,
    extract_links,
    extract_table,
    extract_what_they_do_from_html,
    extract_what_they_do_from_plain_text,
)
from src.org_enrichment import (
    check_enrich_org_config,
    enrich_org_metadata,
    fetch_advocate_teaming_research_by_org,
    _norm_org_key,
)
from src.sheets import (
    EMAIL_STATUS_NOT_SENT,
    EMAIL_STATUS_SENT,
    append_to_sheet,
    ensure_email_status_column_on_sheet,
    normalize_spreadsheet_id,
    read_from_sheet,
    read_from_sheet_with_row_numbers,
    update_email_status_cells,
    update_enrichment_columns_in_place,
)


def _normalize_url(url: str) -> str:
    """Ensure URL has a scheme."""
    url = url.strip()
    parsed = urlparse(url)
    if not parsed.scheme:
        return "https://" + url
    return url


def scrape_url(
    url: str, mode: str = "auto", scroll_to_load: bool = False
) -> ExtractedData | list[ExtractedData] | None:
    """
    Load a URL and extract org/emails. Returns ExtractedData, list of ExtractedData, or None.

    mode: "auto" (try table first), "table", "single", "links" (YC-style link lists)
    """
    url = _normalize_url(url)
    result = load_page(url, scroll_to_load=scroll_to_load)
    if not result:
        return None
    html, text = result

    if mode == "table":
        data = extract_table(html, url)
        return data if data else None
    if mode == "single":
        return extract(html, text, url)
    if mode == "links":
        data = extract_links(html, url)
        return data if data else None

    # auto: try table first, then links, fallback to single
    table_data = extract_table(html, url)
    if len(table_data) >= 2:
        return table_data
    links_data = extract_links(html, url)
    if len(links_data) >= 2:
        return links_data
    return extract(html, text, url)


def enrich_what_they_do_from_pages(
    items: list[ExtractedData],
    listing_url: str | None = None,
    fill_limit: int | None = None,
) -> list[ExtractedData]:
    """
    For rows missing what_they_do, fetch each distinct source_url and pull a line
    from the page (meta / main content). Skips listing_url so we do not refetch
    the directory page once per row.
    """
    import time

    from src.browser import load_page
    from src.config import REQUEST_DELAY_SEC

    def norm(u: str) -> str:
        return _normalize_url(u).strip().rstrip("/")

    listing_key = norm(listing_url) if listing_url else None
    urls: list[str] = []
    seen: set[str] = set()
    for item in items:
        if getattr(item, "what_they_do", None):
            continue
        u = (item.source_url or "").strip()
        if not u.startswith("http"):
            continue
        key = norm(u)
        if listing_key and key == listing_key:
            continue
        if key in seen:
            continue
        seen.add(key)
        urls.append(u)

    if fill_limit is not None:
        urls = urls[: fill_limit]

    # Do not fetch the directory/listing page to fill "What they do" — that copies
    # boilerplate (e.g. program-wide "research focus") onto every row. Leave empty
    # or use --enrich-org for per-company LLM descriptions.

    cache: dict[str, str | None] = {}
    for i, u in enumerate(urls):
        print(f"  What they do {i + 1}/{len(urls)}: {u[:70]}...")
        # networkidle helps JS-heavy pages; plain-text fallback if meta/body HTML is empty
        result = load_page(u, wait_until="networkidle")
        key = norm(u)
        if result:
            html, visible = result
            wtd = extract_what_they_do_from_html(html)
            if not wtd:
                wtd = extract_what_they_do_from_plain_text(visible)
            cache[key] = wtd
        else:
            cache[key] = None
        time.sleep(REQUEST_DELAY_SEC)

    for item in items:
        if getattr(item, "what_they_do", None):
            continue
        u = (item.source_url or "").strip()
        if not u.startswith("http"):
            continue
        key = norm(u)
        if listing_key and key == listing_key:
            continue
        wtd = cache.get(key)
        if wtd:
            item.what_they_do = wtd

    return items


def deep_fetch_emails(
    items: list[ExtractedData], limit: int | None = None
) -> list[ExtractedData]:
    """
    For items with source_url but no emails: visit YC company page, get website,
    visit website, extract emails. Used for YC internships etc.
    """
    import time

    from src.config import REQUEST_DELAY_SEC

    to_fetch = [i for i in items if not i.emails and i.source_url]
    if limit:
        to_fetch = to_fetch[:limit]

    for i, item in enumerate(to_fetch):
        print(f"  Fetching email for {item.organization} ({i + 1}/{len(to_fetch)})...")
        yc_result = load_page(item.source_url)
        if not yc_result:
            continue
        yc_html, _ = yc_result
        if not getattr(item, "what_they_do", None):
            wtd = extract_what_they_do_from_html(yc_html)
            if wtd:
                item.what_they_do = wtd
        company_url = _get_company_website_from_yc_page(yc_html)
        if not company_url:
            continue
        time.sleep(REQUEST_DELAY_SEC)
        site_result = load_page(company_url)
        if not site_result:
            continue
        site_html, site_text = site_result
        extracted = extract(site_html, site_text, company_url)
        if not getattr(item, "what_they_do", None) and extracted.what_they_do:
            item.what_they_do = extracted.what_they_do
        if extracted.emails:
            item.emails = extracted.emails[:1]  # One email per org
            print(f"    -> {extracted.emails[0]}")
        time.sleep(REQUEST_DELAY_SEC)

    return items


def _to_list(data: ExtractedData | list[ExtractedData] | None) -> list[ExtractedData]:
    """Normalize to list of ExtractedData."""
    if data is None:
        return []
    return data if isinstance(data, list) else [data]


def run_interactive(mode: str, scroll_to_load: bool = False) -> None:
    """Interactive mode: prompt for URL, scrape, show results, optionally export."""
    print("Browser Agent - Organization & Email Extractor")
    print("Enter a URL to scrape (or 'quit' to exit)\n")

    all_data: list[ExtractedData] = []

    while True:
        url = input("URL: ").strip()
        if not url or url.lower() in ("quit", "q", "exit"):
            break

        print("Loading page...")
        data = scrape_url(url, mode=mode, scroll_to_load=scroll_to_load)
        items = _to_list(data)
        if not items:
            print("Failed to load the page or no data found. Check the URL and try again.\n")
            continue

        if len(items) == 1:
            d = items[0]
            print(f"\nOrganization: {d.organization}")
            print(f"Emails: {', '.join(d.emails) if d.emails else '(none found)'}")
            print(f"Source: {d.source_url}\n")
        else:
            print(f"\nExtracted {len(items)} rows from table")
            for d in items[:5]:
                email = d.emails[0] if d.emails else "(none)"
                contact = getattr(d, "contact_name", None) or ""
                print(f"  {d.organization} | {contact} | {email}")
            if len(items) > 5:
                print(f"  ... and {len(items) - 5} more\n")

        all_data.extend(items)

        export = input("Export to Google Sheets now? (y/n, or continue scraping): ").strip().lower()
        if export == "y" or export == "yes":
            if mode == "table" and all_data:
                print("\nFilling What they do from detail pages (same as batch -e)...")
                enrich_what_they_do_from_pages(
                    all_data,
                    listing_url=_normalize_url(url),
                    fill_limit=None,
                )
            _do_export(all_data)
            all_data = []
        elif export in ("n", "no"):
            pass

    if all_data:
        export = input("\nExport accumulated results to Google Sheets? (y/n): ").strip().lower()
        if export in ("y", "yes"):
            if mode == "table":
                print("\nFilling What they do from detail pages...")
                enrich_what_they_do_from_pages(all_data, listing_url=None, fill_limit=None)
            _do_export(all_data)


def _do_export(data: list[ExtractedData], spreadsheet_id: str | None = None) -> None:
    """Export data to Google Sheets."""
    try:
        url = append_to_sheet(spreadsheet_id=spreadsheet_id, data=data)
        print(f"Exported to: {url}")
    except FileNotFoundError as e:
        print(f"Error: {e}")
        print("Set GOOGLE_CREDENTIALS_PATH to your service account JSON file.")
        sys.exit(1)
    except Exception as e:
        print(f"Export failed: {e}")
        sys.exit(1)


def run_batch(
    urls: list[str],
    spreadsheet_id: str | None,
    export: bool,
    mode: str,
    scroll_to_load: bool = False,
    fill_desc: bool = False,
    fill_desc_limit: int | None = None,
    enrich_org: bool = False,
    enrich_org_limit: int | None = None,
    enrich_org_url_only: bool = False,
) -> list[ExtractedData]:
    """Batch mode: scrape all URLs, optionally export."""
    data_list: list[ExtractedData] = []

    for url in urls:
        url = _normalize_url(url)
        print(f"Scraping: {url}")
        result = scrape_url(url, mode=mode, scroll_to_load=scroll_to_load)
        items = _to_list(result)
        if items:
            data_list.extend(items)
            if len(items) == 1:
                print(f"  -> {items[0].organization} | {len(items[0].emails)} email(s)")
            else:
                print(f"  -> {len(items)} rows extracted")
        else:
            print("  -> Failed")

    if not data_list:
        print("No data extracted.")
        return []

    print("\n--- Results ---")
    for d in data_list[:20]:
        emails = ", ".join(d.emails) if d.emails else "(none)"
        contact = getattr(d, "contact_name", None) or ""
        print(f"{d.organization} | {contact} | {emails} | {d.source_url}")
    if len(data_list) > 20:
        print(f"... and {len(data_list) - 20} more")

    if export and enrich_org and data_list:
        err = check_enrich_org_config(url_only=enrich_org_url_only)
        if err:
            print(f"\nError: org enrichment requires API keys. {err}")
            print("See .env.example and README (Org enrichment section).")
            sys.exit(1)
        if enrich_org_url_only:
            print("\nResolving company websites (web search only — no “What they do” LLM)...")
        else:
            print("\nEnriching company websites + What they do (web search + LLM)...")
        listing = _normalize_url(urls[0]) if urls else None
        data_list, _ = enrich_org_metadata(
            data_list,
            listing_url=listing,
            limit=enrich_org_limit,
            url_only=enrich_org_url_only,
        )
        if enrich_org_limit is not None:
            print(
                f"\nNote: URL enrichment ran for only {enrich_org_limit} distinct "
                "organization name(s). Omit --enrich-org-limit to resolve every org."
            )
    elif export and fill_desc and data_list:
        print("\nFilling What they do from each row's detail page (one browser load per unique link)...")
        listing = _normalize_url(urls[0]) if urls else None
        enrich_what_they_do_from_pages(
            data_list,
            listing_url=listing,
            fill_limit=fill_desc_limit,
        )

    if export:
        print("\nExporting to Google Sheets...")
        _do_export(data_list, spreadsheet_id)

    return data_list


def _any_distinct_org_missing_wtd(items: list[ExtractedData]) -> bool:
    """True if some organization (first-seen key) has at least one row with empty what_they_do."""
    seen: set[str] = set()
    for it in items:
        k = _norm_org_key(it.organization)
        if k in seen:
            continue
        seen.add(k)
        if not (getattr(it, "what_they_do", None) or "").strip():
            return True
    return False


def run_enrich_sheet_in_place(
    spreadsheet_id: str,
    worksheet_name: str,
    *,
    url_only: bool,
    enrich_limit: int | None,
    what_column_only: bool = False,
    preserve_source_url: bool = False,
    enrich_only_empty_wtd: bool = False,
    sheet_batch_size: int | None = None,
) -> None:
    """
    Read existing sheet rows, run org enrichment in memory, write column D and/or E
    (no new rows, no re-scrape of the directory).
    """
    err = check_enrich_org_config(url_only=url_only)
    if err:
        print(f"Error: org enrichment requires API keys. {err}")
        sys.exit(1)

    # Batched checkpoints: only for "fill all empty What they do" (no row limit).
    use_batches = (
        what_column_only
        and enrich_only_empty_wtd
        and enrich_limit is None
        and not url_only
    )
    if use_batches:
        eff_batch = 20 if sheet_batch_size is None else sheet_batch_size
        if eff_batch <= 0:
            use_batches = False
    else:
        eff_batch = 0

    if use_batches:
        print(f"Reading sheet: {spreadsheet_id} (tab: {worksheet_name})")
        print(
            f"\nBatch mode: updating Google Sheet every {eff_batch} organization(s) "
            "(progress is saved incrementally).\n"
        )
        arpa = fetch_advocate_teaming_research_by_org()
        if arpa:
            print(
                f"  ARPA-H ADVOCATE Teaming: loaded {len(arpa)} org profile(s) once; "
                "reused for all batches.\n"
            )
        else:
            print(
                "  ARPA-H ADVOCATE Teaming: could not load directory; batches use web snippets only.\n"
            )
        batch_n = 0
        last_url = ""
        while True:
            snapshots = read_from_sheet_with_row_numbers(spreadsheet_id, worksheet_name)
            if not snapshots:
                print("No data rows found.")
                sys.exit(1)
            items = [s[1] for s in snapshots]
            if batch_n == 0:
                print(f"Loaded {len(items)} row(s).")
            if not _any_distinct_org_missing_wtd(items):
                if batch_n == 0:
                    print("Nothing to enrich (all distinct orgs already have What they do).")
                else:
                    print(f"\nFinished after {batch_n} batch(es).")
                if last_url:
                    print(f"\nUpdated column D in place: {last_url}")
                return
            batch_n += 1
            print(
                f"\n--- Batch {batch_n} (next up to {eff_batch} orgs with empty What they do) ---"
            )
            items, n_phrases = enrich_org_metadata(
                items,
                listing_url=None,
                limit=eff_batch,
                url_only=False,
                preserve_source_url=True,
                only_empty_wtd=True,
                arpa_profiles_prefetched=arpa,
            )
            if n_phrases == 0:
                print(
                    "\nAborting: no 'What they do' phrases were generated this batch.\n"
                    "Cause: OpenRouter often returns HTTP 429 (rate limit) on free models when "
                    "many orgs run in a row — nothing was saved to the sheet for this batch.\n"
                    "What to do: wait 30–60 minutes and re-run the same command; add GROQ_API_KEY "
                    "in .env as a fallback; use a paid/non-free OpenRouter model; or slow down with "
                    "OPENROUTER_ENRICH_GAP_SEC=8 in .env.\n"
                )
                sys.exit(1)
            last_url = update_enrichment_columns_in_place(
                spreadsheet_id,
                worksheet_name,
                snapshots,
                url_only=False,
                what_column_only=True,
            )
            print(
                f"Checkpoint: batch {batch_n} written to sheet ({n_phrases} new phrase(s) this batch)."
            )
            sys.stdout.flush()

    print(f"Reading sheet: {spreadsheet_id} (tab: {worksheet_name})")
    snapshots = read_from_sheet_with_row_numbers(spreadsheet_id, worksheet_name)
    if not snapshots:
        print("No data rows found.")
        sys.exit(1)
    items = [s[1] for s in snapshots]
    print(f"Loaded {len(items)} row(s).")

    if what_column_only:
        print(
            "\nEnriching What they do in memory (web search snippets + LLM); "
            "using each row's existing Source URL in the prompt; writing column D only…"
        )
    elif url_only:
        print("\nResolving websites in memory (no LLM), then updating columns D and E…")
    else:
        print("\nEnriching in memory (web search + LLM), then updating columns D and E…")

    items, _ = enrich_org_metadata(
        items,
        listing_url=None,
        limit=enrich_limit,
        url_only=url_only,
        preserve_source_url=preserve_source_url,
        only_empty_wtd=enrich_only_empty_wtd,
    )
    if enrich_limit is not None:
        print(
            f"\nNote: Only {enrich_limit} distinct organization name(s) were enriched; "
            "other rows keep their previous Source URL and What they do."
        )

    url = update_enrichment_columns_in_place(
        spreadsheet_id,
        worksheet_name,
        snapshots,
        url_only=url_only,
        what_column_only=what_column_only,
    )
    if what_column_only:
        print(f"\nUpdated column D in place: {url}")
    else:
        print(f"\nUpdated columns D and E in place: {url}")


def _eligible_for_daily_send(item: ExtractedData) -> bool:
    if not item.emails or item.sheet_row is None:
        return False
    s = (item.email_status or "").strip().lower()
    return (not s) or s == EMAIL_STATUS_NOT_SENT


def run_email_outreach(
    data_list: list[ExtractedData],
    template_path: str,
    dry_run: bool,
    limit: int | None,
    save_draft: bool = False,
    skip_send_confirm: bool = False,
    *,
    use_random_delay: bool = False,
    after_each_send: Callable[[ExtractedData, str, str], None] | None = None,
) -> None:
    """Send, preview, or save outreach emails to Gmail Drafts."""
    with_email = [d for d in data_list if d.emails]
    if not with_email:
        print("No contacts with email addresses to send to.")
        return

    if dry_run:
        print(f"\n--- Dry run: previewing {min(len(with_email), limit or len(with_email))} email(s) ---")
        send_outreach(
            data_list,
            template_path,
            dry_run=True,
            limit=limit,
            use_random_delay=use_random_delay,
        )
        return

    if save_draft:
        n = min(len(with_email), limit or len(with_email))
        print(f"\nSaving {n} draft(s) to Gmail (Drafts). Open Gmail → Drafts to see PDF attachments.")
        sent, skipped = send_outreach(
            data_list,
            template_path,
            dry_run=False,
            save_draft=True,
            limit=limit,
            use_random_delay=use_random_delay,
        )
        print(f"\nDone. Drafts saved: {sent}, Skipped: {skipped}")
        return

    n = min(len(with_email), limit or len(with_email))
    if skip_send_confirm:
        print(f"\nSending {n} email(s) (--yes: no confirmation prompt).")
    else:
        confirm = input(f"\nAbout to send {n} email(s). Continue? (y/n): ").strip().lower()
        if confirm not in ("y", "yes"):
            print("Cancelled.")
            return
        print("\nSending emails...")
    sent, skipped = send_outreach(
        data_list,
        template_path,
        dry_run=False,
        limit=limit,
        use_random_delay=use_random_delay,
        after_each_send=after_each_send,
    )
    print(f"\nDone. Sent: {sent}, Skipped: {skipped}")


def _outbound_send_lock_path() -> Path:
    return OUTREACH_STATE_PATH.with_name(f"{OUTREACH_STATE_PATH.name}.send.lock")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract organization names and contact emails from websites, export to Google Sheets."
    )
    parser.add_argument(
        "urls",
        nargs="*",
        help="URLs to scrape (omit for interactive mode)",
    )
    parser.add_argument(
        "-e",
        "--export",
        action="store_true",
        help="Export results to Google Sheets (batch mode)",
    )
    parser.add_argument(
        "-s",
        "--sheet",
        metavar="ID",
        help="Google Sheet ID to append to (from URL). Omit to create new sheet.",
    )
    parser.add_argument(
        "-m",
        "--mode",
        choices=["auto", "table", "single", "links"],
        default="auto",
        help="Extraction mode: auto, table, single, links (YC-style link lists)",
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Fetch emails from company websites (for links mode: visits each company page, then their website)",
    )
    parser.add_argument(
        "--scroll",
        action="store_true",
        help="Scroll page to load more content (for infinite-scroll pages like YC internships)",
    )
    parser.add_argument(
        "--from-sheet",
        metavar="ID",
        help="Read contacts from existing Google Sheet instead of scraping. Use with --email "
        "or --enrich-sheet-in-place. Pass the sheet ID from the URL, or quote full URLs in zsh "
        "(? and & in the URL are globbed otherwise).",
    )
    parser.add_argument(
        "--worksheet",
        default="Organizations",
        help="Worksheet/tab name when using --from-sheet (default: Organizations)",
    )
    parser.add_argument(
        "--email",
        action="store_true",
        help="Send personalized outreach emails (requires --template)",
    )
    parser.add_argument(
        "--template",
        metavar="PATH",
        help="Path to email template (relative paths are from the project folder, not your shell cwd). "
        "Placeholders: {contact_name} (first name), {organization}, {email}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview emails in the terminal only (no PDF shown; not with --save-draft)",
    )
    parser.add_argument(
        "--save-draft",
        action="store_true",
        help="Save each message to Gmail Drafts via IMAP (see body + PDF in Gmail). "
        "Requires IMAP enabled for the account. Same app password as SMTP.",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="With --email (actual send): skip the confirmation prompt and send immediately",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Max number of emails to send (for testing)",
    )
    parser.add_argument(
        "--email-daily",
        action="store_true",
        help="With --from-sheet/--email: cap sends by EMAIL_DAILY_LIMIT per local day, only rows "
        "with empty or 'email not sent' in Email status, random delay between sends; updates "
        "column G and outreach_send_state.json. Set OUTREACH_SHEET_ID to omit --from-sheet.",
    )
    parser.add_argument(
        "--sync-email-replies",
        action="store_true",
        help="Scan Gmail via IMAP: (1) Sent Mail → set 'email sent' if you already emailed that "
        "address; (2) Inbox → set 'replied' for replies. Does not send mail. "
        "Use --from-sheet or OUTREACH_SHEET_ID.",
    )
    parser.add_argument(
        "--sync-email-probe",
        metavar="EMAIL",
        default=None,
        help="Debug only: IMAP login and print Sent / All Mail X-GM-RAW search results for one "
        "recipient address. No Google Sheet. Example: --sync-email-probe friend@company.com",
    )
    parser.add_argument(
        "--only-row",
        type=int,
        default=None,
        metavar="N",
        help="With --sync-email-replies: only run Sent + Inbox sync for this 1-based sheet row.",
    )
    parser.add_argument(
        "--sync-email-replies-before-send",
        action="store_true",
        help="With --email: run Gmail sync (Sent + Inbox) before sending. "
        "This now happens automatically for --email-daily; this flag remains useful for "
        "non-daily sheet-based sends.",
    )
    parser.add_argument(
        "--ensure-email-status-column",
        action="store_true",
        help="Add column G (Email status) with dropdown and default 'email not sent' on existing rows. "
        "Use when the sheet still ends at Extracted At. Requires --from-sheet or OUTREACH_SHEET_ID.",
    )
    parser.add_argument(
        "--no-fill-desc",
        action="store_true",
        help="With table export: skip opening each row's link to fill What they do (faster)",
    )
    parser.add_argument(
        "--fill-desc-limit",
        type=int,
        metavar="N",
        help="Max distinct detail pages to fetch for What they do (default: all rows)",
    )
    parser.add_argument(
        "--enrich-org",
        action="store_true",
        help="Resolve official website (Google CSE) + short What they do phrase (Groq/OpenAI); "
        "disables table fill-desc for this run",
    )
    parser.add_argument(
        "--enrich-org-limit",
        type=int,
        metavar="N",
        help="Max distinct organization names to enrich with --enrich-org (default: all)",
    )
    parser.add_argument(
        "--enrich-org-url-only",
        action="store_true",
        help="With -e/--export: resolve Source URL via web search only (no LLM / What they do).",
    )
    parser.add_argument(
        "--enrich-sheet-in-place",
        action="store_true",
        help="With --from-sheet: update columns D (What they do) and E (Source URL) on existing "
        "rows only — no scrape, no appended rows. Use --enrich-org and/or --enrich-org-url-only.",
    )
    parser.add_argument(
        "--enrich-sheet-what-only",
        action="store_true",
        help="With --enrich-sheet-in-place and --enrich-org: update column D only; keep Source URL "
        "unchanged and pass each row's existing URL into the LLM prompt.",
    )
    parser.add_argument(
        "--enrich-only-empty",
        action="store_true",
        help="With --enrich-sheet-in-place and --enrich-org: only orgs with at least one empty "
        "What they do (sheet order). Pair with --enrich-org-limit N for the next N such orgs.",
    )
    parser.add_argument(
        "--enrich-sheet-batch-size",
        type=int,
        default=None,
        metavar="N",
        help="With --enrich-sheet-what-only, --enrich-only-empty, and no --enrich-org-limit: write "
        "the sheet every N orgs (default 20). Use 0 for one run and a single write at the end.",
    )

    args = parser.parse_args()

    if args.sync_email_probe:
        addr = (args.sync_email_probe or "").strip()
        if not addr:
            print("Error: --sync-email-probe requires an email address, e.g. --sync-email-probe a@b.com")
            sys.exit(1)
        probe_sent_mail_for_address(addr)
        return

    if args.only_row is not None and not args.sync_email_replies:
        print("Error: --only-row requires --sync-email-replies (do not combine with --email).")
        sys.exit(1)
    if args.only_row is not None and args.email:
        print("Error: --only-row is only for standalone --sync-email-replies.")
        sys.exit(1)

    if args.ensure_email_status_column:
        sheet_id = (args.from_sheet or "").strip() or OUTREACH_SHEET_ID
        if not sheet_id:
            print(
                "Error: --ensure-email-status-column requires --from-sheet SHEET_ID or "
                "OUTREACH_SHEET_ID in .env"
            )
            sys.exit(1)
        url = ensure_email_status_column_on_sheet(sheet_id, args.worksheet)
        print(f"Email status column ensured (tab: {args.worksheet}).\n{url}")
        return

    if args.dry_run and args.save_draft:
        print("Error: do not combine --dry-run with --save-draft")
        sys.exit(1)
    if args.save_draft and not args.email:
        print("Error: --save-draft requires --email (and --template)")
        sys.exit(1)
    if args.email_daily and not args.email:
        print("Error: --email-daily requires --email (and --template)")
        sys.exit(1)
    if args.sync_email_replies_before_send and not args.email:
        print("Error: --sync-email-replies-before-send requires --email")
        sys.exit(1)

    if args.sync_email_replies and not args.email:
        sheet_id = (args.from_sheet or "").strip() or OUTREACH_SHEET_ID
        if not sheet_id:
            print(
                "Error: --sync-email-replies requires --from-sheet SHEET_ID or OUTREACH_SHEET_ID in .env"
            )
            sys.exit(1)
        sid = normalize_spreadsheet_id(sheet_id)
        print(f"Syncing email status from Gmail → sheet {sid} (tab: {args.worksheet})…")
        sync_email_status_from_gmail(
            sid, args.worksheet, OUTREACH_STATE_PATH, only_row=args.only_row
        )
        return

    if args.enrich_sheet_in_place:
        if not args.from_sheet:
            print("Error: --enrich-sheet-in-place requires --from-sheet SHEET_ID")
            sys.exit(1)
        if args.urls:
            print("Error: do not pass listing URLs with --enrich-sheet-in-place")
            sys.exit(1)
        if args.export:
            print("Error: --enrich-sheet-in-place does not use -e/--export (nothing is appended)")
            sys.exit(1)
        if not args.enrich_org and not args.enrich_org_url_only:
            print("Error: add --enrich-org (LLM phrase) and/or --enrich-org-url-only (URLs only)")
            sys.exit(1)
        if args.email:
            print("Error: do not combine --enrich-sheet-in-place with --email")
            sys.exit(1)
        if args.enrich_only_empty and not args.enrich_org:
            print("Error: --enrich-only-empty requires --enrich-org (LLM / What they do)")
            sys.exit(1)
        if args.enrich_sheet_what_only:
            if not args.enrich_org:
                print("Error: --enrich-sheet-what-only requires --enrich-org")
                sys.exit(1)
            if args.enrich_org_url_only:
                print("Error: --enrich-sheet-what-only cannot be combined with --enrich-org-url-only")
                sys.exit(1)
            run_enrich_sheet_in_place(
                args.from_sheet,
                args.worksheet,
                url_only=False,
                enrich_limit=args.enrich_org_limit,
                what_column_only=True,
                preserve_source_url=True,
                enrich_only_empty_wtd=args.enrich_only_empty,
                sheet_batch_size=args.enrich_sheet_batch_size,
            )
            return
        url_only = bool(args.enrich_org_url_only and not args.enrich_org)
        run_enrich_sheet_in_place(
            args.from_sheet,
            args.worksheet,
            url_only=url_only,
            enrich_limit=args.enrich_org_limit,
            what_column_only=False,
            preserve_source_url=False,
            enrich_only_empty_wtd=args.enrich_only_empty,
            sheet_batch_size=args.enrich_sheet_batch_size,
        )
        return

    if args.urls and args.enrich_org_url_only and not args.export:
        print("Error: --enrich-org-url-only requires -e/--export")
        sys.exit(1)
    if args.urls and args.enrich_org and not args.export:
        print("Error: --enrich-org only applies when exporting; add -e/--export")
        sys.exit(1)

    # Flow: sheet + --email (read from sheet, send emails); --email-daily uses OUTREACH_SHEET_ID if needed
    if args.email and args.template and (args.from_sheet or args.email_daily):
        sheet_id = (args.from_sheet or "").strip() or OUTREACH_SHEET_ID
        if not sheet_id:
            print(
                "Error: set --from-sheet SHEET_ID or OUTREACH_SHEET_ID in .env for sheet-based email."
            )
            sys.exit(1)
        should_lock_send = args.email and not args.dry_run and not args.save_draft
        lock_ctx = (
            acquire_send_lock(_outbound_send_lock_path())
            if should_lock_send
            else nullcontext()
        )
        try:
            with lock_ctx:
                auto_sync_before_send = bool(args.email_daily or args.sync_email_replies_before_send)
                if auto_sync_before_send:
                    if args.email_daily:
                        print("Syncing Gmail status before daily send…")
                    else:
                        print("Syncing replies from Gmail before send…")
                    sync_email_status_from_gmail(
                        sheet_id, args.worksheet, OUTREACH_STATE_PATH, only_row=None
                    )

                use_random = False
                after_cb: Callable[[ExtractedData, str, str], None] | None = None

                if args.email_daily:
                    use_random = True
                    print(f"Reading contacts from sheet: {sheet_id}")
                    snaps = read_from_sheet_with_row_numbers(sheet_id, args.worksheet)
                    state = load_state(OUTREACH_STATE_PATH)
                    already_sent_rows = {int(k) for k in state.get("by_row", {})}
                    eligible = [
                        t[1] for t in snaps
                        if _eligible_for_daily_send(t[1])
                        and t[1].sheet_row not in already_sent_rows
                    ]
                    rem = remaining_quota(state, EMAIL_DAILY_LIMIT)
                    if args.limit is not None:
                        rem = min(rem, args.limit)
                    n_send = min(rem, len(eligible))
                    if n_send == 0:
                        print(
                            f"No sends queued (eligible with email + not sent: {len(eligible)}, "
                            f"remaining daily quota: {rem})."
                        )
                        return
                    data = eligible[:n_send]
                    print(
                        f"Daily outreach: sending {n_send} email(s); random delay "
                        f"{EMAIL_DELAY_MIN_SEC:.0f}–{EMAIL_DELAY_MAX_SEC:.0f}s between sends."
                    )

                    def _after_send(item: ExtractedData, to_email: str, message_id: str) -> None:
                        st = load_state(OUTREACH_STATE_PATH)
                        if item.sheet_row is None:
                            return
                        record_successful_send(
                            st,
                            sheet_row=item.sheet_row,
                            to_email=to_email,
                            message_id=message_id,
                        )
                        save_state(OUTREACH_STATE_PATH, st)
                        try:
                            update_email_status_cells(
                                sheet_id,
                                args.worksheet,
                                [(item.sheet_row, EMAIL_STATUS_SENT)],
                            )
                        except Exception as e:
                            print(f"  Warning: could not update Email status for row {item.sheet_row}: {e}")

                    after_cb = _after_send
                else:
                    print(f"Reading contacts from sheet: {sheet_id}")
                    data = read_from_sheet(sheet_id, args.worksheet)
                    if not data:
                        print("No contacts found in sheet.")
                        sys.exit(1)
                    print(f"Loaded {len(data)} contacts ({len([d for d in data if d.emails])} with emails)")

                run_email_outreach(
                    data,
                    args.template,
                    args.dry_run,
                    None if args.email_daily else args.limit,
                    save_draft=args.save_draft,
                    skip_send_confirm=args.yes,
                    use_random_delay=use_random,
                    after_each_send=after_cb,
                )
        except RuntimeError as e:
            print(f"Error: {e}")
            sys.exit(1)
        return

    # Flow: scrape + optional deep fetch + optional export + optional email
    if args.urls:
        enrich_org = bool(
            args.export and (args.enrich_org or args.enrich_org_url_only)
        )
        fill_desc = (
            args.mode == "table"
            and args.export
            and not args.no_fill_desc
            and not enrich_org
        )
        data_list = run_batch(
            args.urls,
            args.sheet,
            args.export,
            args.mode,
            scroll_to_load=args.scroll,
            fill_desc=fill_desc,
            fill_desc_limit=args.fill_desc_limit,
            enrich_org=enrich_org,
            enrich_org_limit=args.enrich_org_limit,
            enrich_org_url_only=bool(args.export and args.enrich_org_url_only),
        )
        if data_list and args.deep:
            print("\nDeep fetching emails from company websites...")
            data_list = deep_fetch_emails(data_list, args.limit)
        if data_list and args.email:
            if not args.template:
                print("Error: --email requires --template")
                sys.exit(1)
            should_lock_send = args.email and not args.dry_run and not args.save_draft
            lock_ctx = (
                acquire_send_lock(_outbound_send_lock_path())
                if should_lock_send
                else nullcontext()
            )
            try:
                with lock_ctx:
                    run_email_outreach(
                        data_list,
                        args.template,
                        args.dry_run,
                        args.limit,
                        save_draft=args.save_draft,
                        skip_send_confirm=args.yes,
                    )
            except RuntimeError as e:
                print(f"Error: {e}")
                sys.exit(1)
    else:
        run_interactive(args.mode, scroll_to_load=args.scroll)


if __name__ == "__main__":
    main()
