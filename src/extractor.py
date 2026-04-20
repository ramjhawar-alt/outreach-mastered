"""Extract organization names and contact emails from webpage content."""

import json
import re
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

# Email regex - standard pattern
EMAIL_PATTERN = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
    re.IGNORECASE,
)

# Domains to exclude (tracking, CDNs, common non-contact)
EXCLUDED_EMAIL_DOMAINS = {
    "wixpress.com",
    "sentry.io",
    "example.com",
    "example.org",
    "domain.com",
    "email.com",
    "test.com",
    "yoursite.com",
    "placeholder.com",
    "sentry.dev",
    "w3.org",
    "schema.org",
    "gravatar.com",
    "github.com",
    "google.com",
    "facebook.com",
    "twitter.com",
    "linkedin.com",
    "youtube.com",
    "cloudflare.com",
    "amazonaws.com",
    "googleapis.com",
    "gstatic.com",
    "doubleclick.net",
    "googletagmanager.com",
    "google-analytics.com",
    "facebook.net",
    "hotjar.com",
    "intercom.io",
    "segment.io",
    "mixpanel.com",
    "amplitude.com",
    "imgix.net",
    "cloudinary.com",
}

# Common patterns for organization names
COPYRIGHT_PATTERN = re.compile(
    r"©\s*(?:\d{4}[-\s]*(?:\d{4})?[,\s]*)?(.+?)(?:\s*[|\.]|$)",
    re.IGNORECASE,
)
ABOUT_PATTERN = re.compile(
    r"(?:about|contact)\s+([A-Z][A-Za-z0-9\s&\.\-]+?)(?:\s*[|\.\-]|$)",
    re.IGNORECASE,
)


@dataclass
class ExtractedData:
    """Result of extraction from a single page or table row."""

    organization: str
    emails: list[str]
    source_url: str
    contact_name: str | None = None  # From table rows
    what_they_do: str | None = None  # Short line for outreach templates (e.g. "building X")
    sheet_row: int | None = None  # 1-based Google Sheet row when loaded from --from-sheet
    email_status: str | None = None  # Column G: email not sent / email sent / replied


def _truncate_for_email(text: str, max_len: int = 180) -> str:
    """Shorten to a single sentence suitable for email copy."""
    text = " ".join(text.split())
    if len(text) <= max_len:
        return text
    cut = text[: max_len - 3]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.strip() + "..."


def extract_what_they_do_from_plain_text(text: str) -> str | None:
    """
    Fallback when the raw HTML is sparse (SPA) but Playwright already collected visible text.
    """
    text = " ".join((text or "").split())
    if len(text) < 45:
        return None
    low = text.lower()
    cut = 0
    for noise in ("skip to main", "skip to content", "menu", "search"):
        idx = low.find(noise)
        if 0 <= idx < 100:
            cut = max(cut, min(idx + 40, len(text) // 4))
    snippet = text[cut : cut + 800]
    m = re.search(r".{35,280}?[.!?](?:\s|$)", snippet)
    if m:
        return _truncate_for_email(m.group(0).strip())
    if len(snippet) > 50:
        return _truncate_for_email(snippet[:300])
    return None


def extract_what_they_do_from_html(html: str) -> str | None:
    """
    Pull a one-line description from page HTML (og:description, meta, first paragraph).
    """
    soup = BeautifulSoup(html, "html.parser")
    for prop in ("og:description",):
        m = soup.find("meta", property=prop)
        if m and m.get("content"):
            t = m["content"].strip()
            if t and len(t) > 15:
                return _truncate_for_email(t)
    for name in ("description", "twitter:description"):
        m = soup.find("meta", attrs={"name": name})
        if m and m.get("content"):
            t = m["content"].strip()
            if t and len(t) > 15 and "y combinator" not in t.lower():
                return _truncate_for_email(t)
    # JSON-LD (common on .gov and CMS sites)
    for script in soup.find_all("script", type=lambda v: v and "ld+json" in v.lower()):
        raw = (script.string or script.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        objs: list = []
        if isinstance(data, list):
            objs = data
        elif isinstance(data, dict):
            if isinstance(data.get("@graph"), list):
                objs = data["@graph"]
            else:
                objs = [data]
        for obj in objs:
            if not isinstance(obj, dict):
                continue
            d = obj.get("description")
            if isinstance(d, str) and len(d.strip()) > 20:
                return _truncate_for_email(d.strip())
    # First substantial paragraph in main/article
    for sel in ("article p", "main p", '[class*="description"]', '[class*="summary"]'):
        p = soup.select_one(sel)
        if p:
            t = p.get_text(strip=True)
            if t and 30 < len(t) < 500:
                return _truncate_for_email(t)
    # Project / detail pages often have a strong h1 (gov sites, program pages)
    h1 = soup.find("h1")
    if h1:
        t = h1.get_text(strip=True)
        if t and 12 < len(t) < 220:
            skip = ("home", "menu", "search", "log in", "sign in")
            if not any(x in t.lower() for x in skip):
                return _truncate_for_email(t)
    return None


def _absolute_href(href: str, page_url: str) -> str | None:
    """Resolve a link to an absolute http(s) URL, or None for mailto/tel/anchors."""
    href = (href or "").strip()
    if not href or href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
        return None
    joined = urljoin(page_url, href)
    if joined.startswith("http"):
        return joined.split("#")[0]
    return None


def _url_key(u: str) -> str:
    return (u or "").strip().rstrip("/").lower()


def _first_row_http_link(tr, page_url: str) -> str | None:
    """
    Best http(s) link in a table row: prefer a URL different from the listing page
    (first link is often 'same page' or generic nav).
    """
    listing = _url_key(page_url)
    cands: list[str] = []
    for a in tr.find_all("a", href=True):
        u = _absolute_href(a["href"], page_url)
        if u and u.startswith("http"):
            cands.append(u)
    if not cands:
        return None
    for u in cands:
        if _url_key(u) != listing:
            return u
    return cands[0]


def _cell_looks_like_url(text: str) -> bool:
    t = text.strip().lower()
    return t.startswith("http://") or t.startswith("https://") or t.startswith("www.")


def _get_domain_from_url(url: str) -> str:
    """Extract domain name from URL for fallback org name."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path
        # Remove www.
        if domain.startswith("www."):
            domain = domain[4:]
        # Take first part (e.g. 'acme' from 'acme.com')
        parts = domain.split(".")
        if len(parts) >= 2:
            return parts[0].replace("-", " ").title()
        return domain.replace("-", " ").title()
    except Exception:
        return "Unknown"


def _extract_emails(html: str, text: str) -> list[str]:
    """Extract and filter contact emails from HTML and text."""
    # Combine sources
    combined = html + " " + text
    found = set(EMAIL_PATTERN.findall(combined))

    result = []
    for email in found:
        email = email.lower().strip()
        domain = email.split("@")[-1].lower()
        if domain in EXCLUDED_EMAIL_DOMAINS:
            continue
        # Skip very long emails (likely encoded/obfuscated)
        if len(email) > 80:
            continue
        # Skip image/data URIs
        if "data:" in email or "image/" in email:
            continue
        result.append(email)

    return list(dict.fromkeys(result))  # Preserve order, remove dupes


def _extract_org_from_meta(soup: BeautifulSoup) -> str | None:
    """Extract org name from meta tags (og:site_name, etc.)."""
    # og:site_name
    og = soup.find("meta", property="og:site_name")
    if og and og.get("content"):
        return og["content"].strip()

    # twitter:site
    tw = soup.find("meta", attrs={"name": "twitter:site"})
    if tw and tw.get("content"):
        return tw["content"].strip()

    # application-name
    app = soup.find("meta", attrs={"name": "application-name"})
    if app and app.get("content"):
        return app["content"].strip()

    return None


def _extract_org_from_title(soup: BeautifulSoup) -> str | None:
    """Extract org name from title tag."""
    title_tag = soup.find("title")
    if not title_tag or not title_tag.string:
        return None
    title = title_tag.string.strip()
    if not title:
        return None
    # Often "Page Name | Company" or "Company - Page"
    for sep in [" | ", " – ", " - ", " — "]:
        if sep in title:
            parts = title.split(sep, 1)
            # Prefer the part that looks like a company (shorter, no "Home" etc.)
            for p in parts:
                p = p.strip()
                if p and len(p) < 80 and p.lower() not in ("home", "welcome"):
                    return p
    return title


def _extract_org_from_content(soup: BeautifulSoup, text: str) -> str | None:
    """Extract org name from page content (copyright, about, etc.)."""
    # Copyright
    for m in COPYRIGHT_PATTERN.finditer(text):
        name = m.group(1).strip()
        if name and len(name) < 100 and name.lower() not in ("all rights reserved", "copyright"):
            return name

    # About / Contact patterns
    for m in ABOUT_PATTERN.finditer(text):
        name = m.group(1).strip()
        if name and len(name) < 80:
            return name

    # First h1
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        t = h1.get_text(strip=True)
        if len(t) < 100:
            return t

    return None


# Header patterns for table column mapping (case-insensitive)
NAME_HEADERS = {"name", "contact", "contact name", "person", "representative"}
ORG_HEADERS = {"organization", "org", "company", "institution", "affiliation"}
EMAIL_HEADERS = {"email", "e-mail", "contact email", "mail"}
DESC_HEADERS = {
    "research",
    "description",
    "about",
    "focus",
    "summary",
    "what we do",
    "product",
    "project",
    "program",
    "topic",
    "title",
    "abstract",
    "overview",
    "teaming",
    "initiative",
    "opportunity",
    "award",
    "area",
    "mission",
}


def _extract_emails_from_table_row(row) -> list[str]:
    """Collect emails from cell text and mailto: links in one table row (<tr>)."""
    found: list[str] = []
    seen: set[str] = set()

    def add_raw(addr: str) -> None:
        a = addr.strip().strip("<>")
        if not EMAIL_PATTERN.match(a):
            return
        low = a.lower()
        if low in seen:
            return
        domain = low.split("@")[-1]
        if domain in EXCLUDED_EMAIL_DOMAINS:
            return
        seen.add(low)
        found.append(low)

    for cell in row.find_all(["td", "th"]):
        text = cell.get_text(separator=" ", strip=True)
        for m in EMAIL_PATTERN.finditer(text):
            add_raw(m.group(0))
        for a in cell.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if not href.lower().startswith("mailto:"):
                continue
            inner = unquote(href[7:].split("?", 1)[0].strip())
            add_raw(inner)

    return found


def _map_table_columns(headers: list[str]) -> tuple[int | None, int | None, int | None, int | None]:
    """Map header indices to name, org, email, description. Returns (name_idx, org_idx, email_idx, desc_idx)."""
    name_idx = org_idx = email_idx = desc_idx = None
    for i, h in enumerate(headers):
        h_lower = h.lower().strip()
        if h_lower in NAME_HEADERS or any(h_lower.startswith(p) for p in ("name", "contact")):
            name_idx = i
        elif h_lower in ORG_HEADERS or any(h_lower.startswith(p) for p in ("org", "company")):
            org_idx = i
        elif h_lower in EMAIL_HEADERS or "email" in h_lower or "mail" in h_lower:
            email_idx = i
        elif h_lower in DESC_HEADERS or any(
            x in h_lower for x in ("research", "description", "about", "project", "program", "summary")
        ):
            desc_idx = i
    return (name_idx, org_idx, email_idx, desc_idx)


def _is_email(text: str) -> bool:
    """Check if text looks like an email."""
    return bool(EMAIL_PATTERN.match(text.strip()))


def extract_table(html: str, url: str) -> list[ExtractedData]:
    """
    Extract organization name and contact emails from HTML tables.

    Supports tables with or without header rows. Uses header mapping when
    available, otherwise falls back to position-based (0=name, 1=org, 2=email).

    Args:
        html: Full HTML of the page
        url: Source URL

    Returns:
        List of ExtractedData, one per table row with org+email
    """
    soup = BeautifulSoup(html, "html.parser")
    results: list[ExtractedData] = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        # Try to detect headers from first row
        first_cells = [c.get_text(strip=True) for c in rows[0].find_all(["td", "th"])]
        name_idx, org_idx, email_idx, _desc_idx = _map_table_columns(first_cells)

        # If first row looks like data (has email), use position fallback
        if email_idx is None or any(_is_email(c) for c in first_cells):
            # No headers found, use common layout: name, org, email
            name_idx, org_idx, email_idx = 0, 1, 2
            data_rows = rows
        else:
            data_rows = rows[1:]

        for row in data_rows:
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 3:
                continue

            # Get values by index
            name = cells[name_idx] if name_idx is not None and name_idx < len(cells) else ""
            org = cells[org_idx] if org_idx is not None and org_idx < len(cells) else ""

            row_emails = _extract_emails_from_table_row(row)
            emails_out: list[str] = []
            if email_idx is not None and email_idx < len(cells):
                raw_e = cells[email_idx].strip()
                if EMAIL_PATTERN.match(raw_e):
                    low = raw_e.lower()
                    if low.split("@")[-1] not in EXCLUDED_EMAIL_DOMAINS:
                        emails_out.append(low)
            for e in row_emails:
                if e not in emails_out:
                    emails_out.append(e)
            if not emails_out:
                for c in cells:
                    cand = c.strip()
                    if EMAIL_PATTERN.match(cand):
                        low = cand.lower()
                        if low.split("@")[-1] not in EXCLUDED_EMAIL_DOMAINS:
                            emails_out.append(low)
                            break

            row_page_url = _first_row_http_link(row, url)
            source = row_page_url or url

            if not org and not emails_out:
                continue

            # Do not copy directory columns (e.g. "current research focus") into what_they_do;
            # use --enrich-org for an LLM summary from the real company site + search.
            results.append(
                ExtractedData(
                    organization=org or "(unknown)",
                    emails=emails_out,
                    source_url=source,
                    contact_name=name or None,
                    what_they_do=None,
                )
            )

    # Deduplicate by (org, emails, contact)
    seen: set[tuple[str, tuple[str, ...], str]] = set()
    unique: list[ExtractedData] = []
    for r in results:
        key = (r.organization, tuple(sorted(r.emails)), (r.contact_name or ""))
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def extract_links(
    html: str,
    url: str,
    link_pattern: str = "/companies/",
    base_url: str | None = None,
) -> list[ExtractedData]:
    """
    Extract org names and URLs from link-based pages (e.g. YC internships, directories).

    Finds links whose href contains link_pattern. Uses link text as org name.
    No emails - use --deep to fetch from company websites.

    Args:
        html: Full HTML
        url: Source page URL
        link_pattern: Substring to match in href (e.g. /companies/)
        base_url: Base for relative URLs (default: parsed from url)

    Returns:
        List of ExtractedData with organization, source_url (full company page URL), empty emails
    """
    soup = BeautifulSoup(html, "html.parser")
    parsed = urlparse(url)
    base = base_url or f"{parsed.scheme}://{parsed.netloc}"

    results: list[ExtractedData] = []
    seen_urls: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if link_pattern not in href or "/jobs" in href:
            continue
        text = a.get_text(strip=True)
        if not text or len(text) > 150:
            continue
        # Build full URL
        if href.startswith("/"):
            full_url = base + href
        elif href.startswith("http"):
            full_url = href
        else:
            continue
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)
        # Clean org name - text after • is often a one-line pitch (YC / Work at a Startup)
        what_td: str | None = None
        if "•" in text:
            parts = text.split("•", 1)
            org = parts[0].strip()
            if len(parts) > 1:
                raw = parts[1].strip()
                # Strip trailing metadata like "(22 days ago)"
                raw = re.sub(r"\s*\([^)]*ago\)\s*$", "", raw, flags=re.I)
                if raw:
                    what_td = _truncate_for_email(raw)
        else:
            org = text
        if len(org) > 100:
            org = org[:100] + "..."
        results.append(
            ExtractedData(
                organization=org,
                emails=[],
                source_url=full_url,
                contact_name=None,
                what_they_do=what_td,
            )
        )
    return results


# Domains to skip when finding company website from YC-style pages
SOCIAL_AND_SKIP_DOMAINS = {
    "ycombinator.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "github.com",
    "crunchbase.com",
    "workatastartup.com",
    "account.ycombinator.com",
    "startupschool.org",
}


def _get_company_website_from_yc_page(html: str) -> str | None:
    """Extract company website URL from YC company page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip().split("?")[0]  # Drop query params
        if not href.startswith("http"):
            continue
        try:
            parsed = urlparse(href)
            domain = (parsed.netloc or "").lower()
            if not domain:
                continue
            if domain.startswith("www."):
                domain = domain[4:]
            if domain in SOCIAL_AND_SKIP_DOMAINS or any(
                domain == d or domain.endswith("." + d) for d in SOCIAL_AND_SKIP_DOMAINS
            ):
                continue
            # Prefer short domains (likely company sites) over long ones
            candidates.append(href)
        except Exception:
            continue
    # Return first that looks like a company site (not utm-heavy, not too long)
    for c in candidates:
        if "utm_" not in c and len(c) < 80:
            return c
    return candidates[0] if candidates else None


def extract(html: str, text: str, url: str) -> ExtractedData:
    """
    Extract organization name and contact emails from page content.

    Args:
        html: Full HTML of the page
        text: Visible text content
        url: Source URL (for fallback org name and result)

    Returns:
        ExtractedData with organization, emails, and source_url
    """
    soup = BeautifulSoup(html, "html.parser")
    emails = _extract_emails(html, text)

    org = (
        _extract_org_from_meta(soup)
        or _extract_org_from_title(soup)
        or _extract_org_from_content(soup, text)
        or _get_domain_from_url(url)
    )

    # Clean org name
    if org:
        org = org.strip()
        if len(org) > 120:
            org = org[:120] + "..."

    wtd = extract_what_they_do_from_html(html)

    return ExtractedData(
        organization=org or _get_domain_from_url(url),
        emails=emails,
        source_url=url,
        what_they_do=wtd,
    )
