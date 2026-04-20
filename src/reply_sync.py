"""Sync Email status from Gmail: Sent Mail (already sent) and Inbox (replies)."""

from __future__ import annotations

import email
import imaplib
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses
from pathlib import Path

from .config import (
    GMAIL_APP_PASSWORD,
    GMAIL_FROM,
    GMAIL_FROM_ALIASES,
    OUTREACH_SYNC_VERBOSE,
)
from .outreach_state import get_row_send_meta, load_state
from .sheets import (
    EMAIL_STATUS_NOT_SENT,
    EMAIL_STATUS_REPLIED,
    EMAIL_STATUS_SENT,
    normalize_spreadsheet_id,
    read_from_sheet_with_row_numbers,
    update_email_status_cells,
)

# Addresses in To/Cc/Bcc (getaddresses misses some encoded / odd formats)
_HEADER_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
    re.IGNORECASE,
)


def _emails_in_text(text: str) -> set[str]:
    return {m.lower() for m in _HEADER_EMAIL_RE.findall(text or "")}


def _our_sender_emails() -> set[str]:
    s = {GMAIL_FROM.lower().strip()} | set(GMAIL_FROM_ALIASES)
    s.discard("")
    return s


def _norm_sheet_text(s: str) -> str:
    """Strip NBSP / odd unicode from Google Sheets cells."""
    t = unicodedata.normalize("NFKC", (s or ""))
    return t.replace("\u00a0", " ").replace("\u200b", "").strip()


def _row_eligible_for_sent_scan(status: str) -> bool:
    """Sheet G still means 'we have not recorded a send for this row'."""
    s = (status or "").strip().lower()
    if not s:
        return True
    if s == EMAIL_STATUS_NOT_SENT:
        return True
    if s in ("not sent", "unsent", "pending", "0", "no"):
        return True
    return False


def _parse_sent_at(sent_at_iso: str) -> datetime:
    try:
        raw = sent_at_iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone()
    except (TypeError, ValueError):
        return datetime.now().astimezone()


def _imap_since_str(sent_at_iso: str) -> str:
    """IMAP SINCE date: 06-Apr-2026"""
    dt = _parse_sent_at(sent_at_iso) if sent_at_iso else datetime.now().astimezone()
    return dt.strftime("%d-%b-%Y")


def _decode_mime_header(value: str) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _message_references_our_id(raw_bytes: bytes, our_message_id: str) -> bool:
    mid = (our_message_id or "").strip()
    if not mid:
        return False
    mid_norm = mid.strip("<>").lower()
    try:
        msg = email.message_from_bytes(raw_bytes)
    except Exception:
        return False
    for key in ("In-Reply-To", "References"):
        v = msg.get(key)
        if not v:
            continue
        text = _decode_mime_header(v).lower()
        compact = text.replace("<", "").replace(">", "").replace(" ", "")
        if mid_norm in compact:
            return True
    return False


def _fetch_rfc822(conn: imaplib.IMAP4_SSL, uid: bytes) -> bytes | None:
    try:
        typ, data = conn.uid("FETCH", uid, "(RFC822)")
    except imaplib.IMAP4.error:
        return None
    if typ != "OK" or not data or not isinstance(data[0], tuple):
        return None
    return data[0][1] if len(data[0]) > 1 else None


def _message_addresses_contain(msg: email.message.Message, want: str) -> bool:
    """True if To/Cc/Bcc includes this address (case-insensitive)."""
    want_l = want.lower().strip()
    if not want_l:
        return False
    for key in ("To", "Cc", "Bcc"):
        raw = msg.get(key)
        if not raw:
            continue
        dec = _decode_mime_header(raw)
        for _name, addr in getaddresses([dec]):
            if addr.lower() == want_l:
                return True
        for em in _emails_in_text(dec):
            if em == want_l:
                return True
    return False


def _message_from_any_account(msg: email.message.Message, accounts: set[str]) -> bool:
    """True if From matches GMAIL_FROM or any GMAIL_FROM_ALIASES (Send mail as)."""
    if not accounts:
        return False
    raw = msg.get("From") or ""
    dec = _decode_mime_header(raw)
    low = dec.lower()
    for a in accounts:
        if a and a in low:
            return True
    for _name, addr in getaddresses([dec]):
        if addr.lower() in accounts:
            return True
    for em in _emails_in_text(dec):
        if em in accounts:
            return True
    return False


def _quoted_segments_from_list_line(raw: bytes) -> list[str]:
    s = raw.decode("utf-8", errors="replace")
    return re.findall(r'"([^"]*)"', s)


def _mailbox_name_from_list_line(raw: bytes) -> str | None:
    quoted = _quoted_segments_from_list_line(raw)
    for cand in reversed(quoted):
        if cand not in ("/", ".", ""):
            return cand
    return None


def _imap_mailbox_arg(name: str) -> str:
    """Quote mailbox names so Gmail accepts names with spaces like Sent Mail."""
    escaped = (name or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _select_sent_mail(conn: imaplib.IMAP4_SSL) -> str:
    """Return mailbox name that worked.

    Gmail marks the real Sent folder with the \\Sent IMAP flag. Do **not** use substring
    ``\"sent\" in name`` — that matches unrelated labels (e.g. *consent*, *present*, *absent*)
    and opens an empty mailbox so every SEARCH returns nothing.
    """
    typ, data = conn.list()
    flagged: list[str] = []
    if typ == "OK" and data:
        for raw in data:
            if not raw:
                continue
            s = raw.decode("utf-8", errors="replace")
            if "\\Noselect" in s:
                continue
            if "\\Sent" not in s:
                continue
            name = _mailbox_name_from_list_line(raw)
            if name and name not in flagged:
                flagged.append(name)

    try_order = list(flagged)
    for extra in (
        "[Gmail]/Sent Mail",
        "[Google Mail]/Sent Mail",
        "[Gmail]/Sent",
        "Sent",
        "INBOX.Sent",
    ):
        if extra not in try_order:
            try_order.append(extra)

    seen: set[str] = set()
    for mbox in try_order:
        if mbox in seen:
            continue
        seen.add(mbox)
        try:
            typ, _ = conn.select(_imap_mailbox_arg(mbox))
            if typ == "OK":
                return mbox
        except imaplib.IMAP4.error:
            continue
    raise RuntimeError(
        "Could not open Sent Mail over IMAP. Enable IMAP in Gmail settings and use an app password."
    )


def _select_all_mail(conn: imaplib.IMAP4_SSL) -> str:
    """Return All Mail mailbox name (\\All flag, then common paths)."""
    typ, data = conn.list()
    flagged: list[str] = []
    if typ == "OK" and data:
        for raw in data:
            if not raw:
                continue
            s = raw.decode("utf-8", errors="replace")
            if "\\Noselect" in s:
                continue
            if "\\All" not in s:
                continue
            name = _mailbox_name_from_list_line(raw)
            if name and name not in flagged:
                flagged.append(name)

    try_order = list(flagged)
    for extra in ("[Gmail]/All Mail", "[Google Mail]/All Mail", "All Mail"):
        if extra not in try_order:
            try_order.append(extra)

    seen: set[str] = set()
    for mbox in try_order:
        if mbox in seen:
            continue
        seen.add(mbox)
        try:
            typ, _ = conn.select(_imap_mailbox_arg(mbox))
            if typ == "OK":
                return mbox
        except imaplib.IMAP4.error:
            continue
    raise RuntimeError("Could not open All Mail over IMAP.")


def _imap_safe_disconnect(conn: imaplib.IMAP4_SSL | None) -> None:
    if conn is None:
        return
    try:
        try:
            conn.logout()
        except Exception:
            conn.shutdown()
    except Exception:
        pass


def _xgmraw_search_uids(conn: imaplib.IMAP4_SSL, queries: list[str]) -> list[bytes]:
    """Gmail UID SEARCH X-GM-RAW for each query until UIDs are returned."""
    for q in queries:
        iq = q.replace("\\", "\\\\").replace('"', '\\"')
        for args in (("X-GM-RAW", q), (f'(X-GM-RAW "{iq}")',)):
            try:
                typ, data = conn.uid("SEARCH", *args)
            except imaplib.IMAP4.error:
                typ, data = "NO", [b""]
            if typ == "OK" and data and data[0]:
                found = data[0].split()
                if found:
                    return found
    return []


def _fetch_header_fields_peek(conn: imaplib.IMAP4_SSL, uid: bytes) -> str | None:
    try:
        typ, data = conn.uid(
            "FETCH",
            uid,
            "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT)])",
        )
    except imaplib.IMAP4.error:
        return None
    if typ != "OK" or not data:
        return None
    for part in data:
        if isinstance(part, tuple) and len(part) >= 2:
            payload = part[1]
            if isinstance(payload, bytes):
                return payload.decode("utf-8", errors="replace")
            return str(payload)
    return None


def probe_sent_mail_for_address(addr: str, *, since_days: int = 730) -> None:
    """
    Minimal IMAP diagnostics: open Sent, print SEARCH ALL count, print X-GM-RAW results
    for one address, optionally FETCH first hit's From/To/Subject. No sheet access.
    """
    if not GMAIL_FROM or not GMAIL_APP_PASSWORD:
        raise ValueError("Set GMAIL_FROM and GMAIL_APP_PASSWORD.")

    addr = _norm_sheet_text(addr)
    if not addr or "@" not in addr:
        raise ValueError("Pass a single recipient email address.")

    conn: imaplib.IMAP4_SSL | None = None
    try:
        conn = imaplib.IMAP4_SSL("imap.gmail.com")
        conn.login(GMAIL_FROM, GMAIL_APP_PASSWORD)
        print(f"Probe: logged in as {GMAIL_FROM!r}, looking for mail to {addr!r}\n")

        sent_mbox: str | None = None
        use_xgmraw = False
        try:
            sent_mbox = _select_sent_mail(conn)
            print(f"Selected Sent mailbox: {sent_mbox!r}")
        except RuntimeError as e:
            print(f"Could not select Sent ({e}); will use All Mail + in:sent.")
            use_xgmraw = True
            try:
                am = _select_all_mail(conn)
                print(f"Selected All Mail: {am!r}")
            except RuntimeError as e2:
                print(f"Could not select All Mail: {e2}")
                return

        if not use_xgmraw and sent_mbox:
            try:
                typ_ct, dat_ct = conn.search(None, "ALL")
                n_all = (
                    len(dat_ct[0].split())
                    if typ_ct == "OK" and dat_ct and dat_ct[0]
                    else 0
                )
                print(f"SEARCH ALL in Sent → ~{n_all} message(s) (sequence numbers).\n")
            except imaplib.IMAP4.error as e:
                print(f"SEARCH ALL in Sent failed: {e}\n")

        def try_search(label: str, queries: list[str]) -> list[bytes]:
            print(f"--- {label} ---")
            uids_out: list[bytes] = []
            for q in queries:
                iq = q.replace("\\", "\\\\").replace('"', '\\"')
                for args in (("X-GM-RAW", q), (f'(X-GM-RAW "{iq}")',)):
                    try:
                        typ, data = conn.uid("SEARCH", *args)
                    except Exception as ex:
                        print(f"  UID SEARCH {args!r} → ERROR {ex!r}")
                        continue
                    uids = data[0].split() if typ == "OK" and data and data[0] else []
                    print(f"  UID SEARCH {args!r} → typ={typ!r} n_uids={len(uids)}")
                    if uids:
                        uids_out = uids
                        break
                if uids_out:
                    break
            return uids_out

        first_uids: list[bytes] = []
        if not use_xgmraw:
            first_uids = try_search(
                "While Sent is selected",
                [f"to:{addr}", f"in:sent to:{addr}", f"newer_than:{since_days}d to:{addr}"],
            )

        if not first_uids:
            print("\n--- Falling back to All Mail + in:sent ---")
            try:
                _select_all_mail(conn)
            except RuntimeError as e:
                print(f"Select All Mail failed: {e}")
                return
            first_uids = try_search(
                "All Mail selected",
                [
                    f"in:sent to:{addr} newer_than:{since_days}d",
                    f"in:sent to:{addr}",
                    f'in:sent to:"{addr}"',
                ],
            )

        if first_uids:
            uid0 = first_uids[0]
            print(f"\nFirst UID: {uid0.decode()!r} (showing headers)")
            hdr = _fetch_header_fields_peek(conn, uid0)
            if hdr:
                print(hdr.strip())
            else:
                print("(Could not FETCH header fields.)")
        else:
            print("\nNo UIDs found for this address with the tried queries.")

    finally:
        _imap_safe_disconnect(conn)


# Reconnect IMAP every N rows to avoid SSL/socket errors on long full-sheet runs.
_SENT_SYNC_CHUNK_SIZE = 35


def sync_sent_status_from_sent_folder(
    spreadsheet_id: str,
    worksheet_name: str,
    *,
    since_days: int = 730,
    only_row: int | None = None,
) -> int:
    """
    For each row still 'email not sent' (or blank), if Sent Mail has a message From you
    To/Cc that address, set status to 'email sent'.

    Does not use outreach_send_state.json (for emails you sent outside this tool).
    """
    if not GMAIL_FROM or not GMAIL_APP_PASSWORD:
        raise ValueError("Set GMAIL_FROM and GMAIL_APP_PASSWORD.")

    spreadsheet_id = normalize_spreadsheet_id(spreadsheet_id)
    snapshots = read_from_sheet_with_row_numbers(spreadsheet_id, worksheet_name)
    since_dt = datetime.now().astimezone() - timedelta(days=since_days)
    since_imap = since_dt.strftime("%d-%b-%Y")

    pending: list[tuple[int, str]] = []
    for row_num, item, _w, _s in snapshots:
        status = _norm_sheet_text(item.email_status or "").lower()
        if status == EMAIL_STATUS_REPLIED or status == EMAIL_STATUS_SENT:
            continue
        if not _row_eligible_for_sent_scan(status):
            continue
        if not item.emails:
            continue
        to_email = _norm_sheet_text(item.emails[0])
        if not to_email or "@" not in to_email:
            continue
        pending.append((row_num, to_email))

    if only_row is not None:
        pending = [(r, e) for r, e in pending if r == only_row]
        if not pending:
            print(
                f"  No eligible row for --only-row {only_row} "
                f"(missing, no email, or status is already sent/replied)."
            )
            return 0

    if not pending:
        print("  No rows in 'email not sent' (or blank) with an email to check in Sent Mail.")
        return 0

    if OUTREACH_SYNC_VERBOSE:
        print(
            f"  [verbose] Checking Sent for {len(pending)} row(s); "
            f"GMAIL_FROM={GMAIL_FROM!r} aliases={GMAIL_FROM_ALIASES!r}"
        )
        for rn, em in pending[:20]:
            print(f"    row {rn} → {em}")
        if len(pending) > 20:
            print(f"    ... and {len(pending) - 20} more")

    senders = _our_sender_emails()
    to_mark: list[tuple[int, str]] = []

    def open_sent_connection() -> tuple[imaplib.IMAP4_SSL, bool, str | None]:
        c = imaplib.IMAP4_SSL("imap.gmail.com")
        c.login(GMAIL_FROM, GMAIL_APP_PASSWORD)
        use_x = False
        sm: str | None = None
        try:
            sm = _select_sent_mail(c)
        except RuntimeError:
            use_x = True
            try:
                _select_all_mail(c)
            except RuntimeError:
                _imap_safe_disconnect(c)
                raise RuntimeError(
                    "Could not open Sent Mail or All Mail over IMAP. "
                    "In Gmail: Settings → See all settings → Forwarding and POP/IMAP → Enable IMAP."
                ) from None
        if OUTREACH_SYNC_VERBOSE and not use_x and sm:
            try:
                typ_ct, dat_ct = c.search(None, "ALL")
                n_all = (
                    len(dat_ct[0].split())
                    if typ_ct == "OK" and dat_ct and dat_ct[0]
                    else 0
                )
                print(
                    f"  [verbose] Opened IMAP mailbox {sm!r} "
                    f"({n_all} message(s) in folder per SEARCH ALL)."
                )
            except imaplib.IMAP4.error as e:
                print(f"  [verbose] Opened {sm!r} but SEARCH ALL failed: {e}")
        return c, use_x, sm

    def process_chunk(conn: imaplib.IMAP4_SSL, use_xgmraw: bool, sent_mbox: str | None, chunk: list[tuple[int, str]]) -> None:
        nonlocal to_mark
        for row_num, to_email in chunk:
            uids: list[bytes] = []
            addr = to_email.strip()
            if use_xgmraw:
                uids = _xgmraw_search_uids(
                    conn,
                    [
                        f"in:sent to:{addr} newer_than:{since_days}d",
                        f"in:sent to:{addr}",
                        f'in:sent to:"{addr}"',
                    ],
                )
            else:
                uids = _xgmraw_search_uids(
                    conn,
                    [
                        f"to:{addr}",
                        f"newer_than:{since_days}d to:{addr}",
                        f'to:"{addr}"',
                        f"from:{GMAIL_FROM} to:{addr}",
                    ],
                )
                if not uids:
                    safe = to_email.replace("\\", "\\\\").replace('"', '\\"')
                    crit = f'(OR TO "{safe}" CC "{safe}") SINCE {since_imap}'
                    try:
                        typ, data = conn.search(None, crit)
                    except imaplib.IMAP4.error:
                        typ, data = "NO", [b""]
                    if typ == "OK" and data and data[0]:
                        uids = data[0].split()
                if not uids and sent_mbox:
                    am_uids: list[bytes] = []
                    try:
                        _select_all_mail(conn)
                        am_uids = _xgmraw_search_uids(
                            conn,
                            [
                                f"in:sent to:{addr} newer_than:{since_days}d",
                                f"in:sent to:{addr}",
                            ],
                        )
                    except imaplib.IMAP4.error:
                        am_uids = []
                    if am_uids:
                        found_am = False
                        for uid in am_uids[:50]:
                            raw = _fetch_rfc822(conn, uid)
                            if not raw:
                                continue
                            try:
                                msg = email.message_from_bytes(raw)
                            except Exception:
                                continue
                            if _message_from_any_account(msg, senders) and _message_addresses_contain(
                                msg, to_email
                            ):
                                found_am = True
                                break
                        try:
                            if sent_mbox:
                                conn.select(_imap_mailbox_arg(sent_mbox))
                        except imaplib.IMAP4.error:
                            pass
                        if found_am:
                            to_mark.append((row_num, EMAIL_STATUS_SENT))
                        elif OUTREACH_SYNC_VERBOSE:
                            print(
                                f"  [verbose] row {row_num} ({to_email}): All Mail in:sent hits but "
                                f"no From/To match (check GMAIL_FROM / sheet address)."
                            )
                        continue
                    try:
                        if sent_mbox:
                            conn.select(_imap_mailbox_arg(sent_mbox))
                    except imaplib.IMAP4.error:
                        pass

            found = False
            trust_sent_mailbox = not use_xgmraw
            for uid in uids[:50]:
                raw = _fetch_rfc822(conn, uid)
                if not raw:
                    continue
                try:
                    msg = email.message_from_bytes(raw)
                except Exception:
                    continue
                if trust_sent_mailbox:
                    if _message_addresses_contain(msg, to_email):
                        found = True
                        break
                elif _message_from_any_account(msg, senders) and _message_addresses_contain(
                    msg, to_email
                ):
                    found = True
                    break

            if found:
                to_mark.append((row_num, EMAIL_STATUS_SENT))
            elif OUTREACH_SYNC_VERBOSE:
                if uids:
                    print(
                        f"  [verbose] row {row_num} ({to_email}): {len(uids)} IMAP hit(s) but "
                        f"no To/Cc match (compare sheet Email cell to Gmail recipient)."
                    )
                else:
                    print(
                        f"  [verbose] row {row_num} ({to_email}): no messages in Sent for search"
                    )

    n = len(pending)
    if n <= _SENT_SYNC_CHUNK_SIZE or only_row is not None:
        conn, use_xgmraw, sent_mbox = open_sent_connection()
        try:
            process_chunk(conn, use_xgmraw, sent_mbox, pending)
        finally:
            _imap_safe_disconnect(conn)
    else:
        for start in range(0, n, _SENT_SYNC_CHUNK_SIZE):
            chunk = pending[start : start + _SENT_SYNC_CHUNK_SIZE]
            if OUTREACH_SYNC_VERBOSE:
                print(
                    f"  [verbose] IMAP chunk rows {start + 1}–{start + len(chunk)} of {n} "
                    f"(fresh connection)"
                )
            conn, use_xgmraw, sent_mbox = open_sent_connection()
            try:
                process_chunk(conn, use_xgmraw, sent_mbox, chunk)
            finally:
                _imap_safe_disconnect(conn)

    if to_mark:
        update_email_status_cells(spreadsheet_id, worksheet_name, to_mark)
        print(f"  Marked {len(to_mark)} row(s) as 'email sent' (matched Sent Mail).")
    else:
        print("  No Sent Mail matches for not-yet-sent rows.")
        if pending and not OUTREACH_SYNC_VERBOSE:
            print(
                "  Tip: set OUTREACH_SYNC_VERBOSE=1 in .env to list rows checked; "
                "if you use Gmail 'Send mail as', add GMAIL_FROM_ALIASES=other@domain.com"
            )

    return len(to_mark)


def sync_replies_from_inbox(
    spreadsheet_id: str,
    worksheet_name: str,
    state_path: Path,
    *,
    only_row: int | None = None,
) -> int:
    """
    For each sheet row with status 'email sent', if Inbox has a likely reply, set 'replied'.

    Uses outreach_send_state.json for Message-ID and sent time when available.
    """
    if not GMAIL_FROM or not GMAIL_APP_PASSWORD:
        raise ValueError("Set GMAIL_FROM and GMAIL_APP_PASSWORD for reply sync.")

    spreadsheet_id = normalize_spreadsheet_id(spreadsheet_id)
    state = load_state(state_path)
    snapshots = read_from_sheet_with_row_numbers(spreadsheet_id, worksheet_name)

    candidates: list[tuple[int, str, str, str]] = []
    for row_num, item, _w, _s in snapshots:
        status = (item.email_status or "").strip().lower()
        if status != EMAIL_STATUS_SENT:
            continue
        meta = get_row_send_meta(state, row_num)
        to_email = ""
        message_id = ""
        sent_at = ""
        if meta:
            to_email = (meta.get("to") or "").strip()
            message_id = (meta.get("message_id") or "").strip()
            sent_at = (meta.get("sent_at") or "").strip()
        if not to_email and item.emails:
            to_email = item.emails[0].strip()
        if not to_email:
            continue
        if not sent_at:
            sent_at = (datetime.now().astimezone() - timedelta(days=730)).isoformat()
        candidates.append((row_num, to_email, message_id, sent_at))

    if only_row is not None:
        candidates = [c for c in candidates if c[0] == only_row]

    if not candidates:
        print("  No rows with status 'email sent' to check for replies.")
        return 0

    print(f"  Checking {len(candidates)} 'email sent' row(s) for replies…")

    _REPLY_CHUNK_SIZE = 35
    to_mark: list[tuple[int, str]] = []

    def _open_reply_connection() -> imaplib.IMAP4_SSL:
        c = imaplib.IMAP4_SSL("imap.gmail.com")
        c.login(GMAIL_FROM, GMAIL_APP_PASSWORD)
        try:
            _select_all_mail(c)
        except RuntimeError:
            c.select("INBOX")
        return c

    def _check_candidate(
        conn: imaplib.IMAP4_SSL,
        row_num: int,
        to_email: str,
        message_id: str,
        sent_at: str,
    ) -> bool:
        addr = to_email.strip().lower()
        uids = _xgmraw_search_uids(
            conn,
            [
                f"from:{addr} to:{GMAIL_FROM} newer_than:30d",
                f"from:{addr} to:{GMAIL_FROM}",
                f"from:{addr} in:inbox",
            ],
        )
        if not uids:
            since = _imap_since_str(sent_at) if sent_at else _imap_since_str(
                (datetime.now().astimezone() - timedelta(days=60)).isoformat()
            )
            safe_addr = to_email.replace("\\", "\\\\").replace('"', '\\"')
            crit = f'(FROM "{safe_addr}" SINCE {since})'
            try:
                typ, data = conn.search(None, crit)
            except imaplib.IMAP4.error:
                typ, data = "NO", [b""]
            if typ == "OK" and data and data[0]:
                uids = data[0].split()

        for uid in uids[:40]:
            raw = _fetch_rfc822(conn, uid)
            if not raw:
                continue
            if message_id and _message_references_our_id(raw, message_id):
                return True
            try:
                msg = email.message_from_bytes(raw)
                from_h = _decode_mime_header(msg.get("From") or "").lower()
                subj = (msg.get("Subject") or "").lower()
                if addr in from_h and (
                    "re:" in subj
                    or not message_id
                    or _message_references_our_id(raw, message_id)
                ):
                    return True
            except Exception:
                continue
        return False

    n = len(candidates)
    for start in range(0, n, _REPLY_CHUNK_SIZE):
        chunk = candidates[start : start + _REPLY_CHUNK_SIZE]
        conn = _open_reply_connection()
        try:
            for row_num, to_email, message_id, sent_at in chunk:
                if _check_candidate(conn, row_num, to_email, message_id, sent_at):
                    to_mark.append((row_num, EMAIL_STATUS_REPLIED))
                    print(f"    reply found: row {row_num} ({to_email})")
        finally:
            _imap_safe_disconnect(conn)

    if to_mark:
        update_email_status_cells(spreadsheet_id, worksheet_name, to_mark)
        print(f"  Marked {len(to_mark)} row(s) as 'replied'.")
    else:
        print("  No new replies detected for 'email sent' rows.")

    return len(to_mark)


def sync_email_status_from_gmail(
    spreadsheet_id: str,
    worksheet_name: str,
    state_path: Path,
    *,
    only_row: int | None = None,
) -> tuple[int, int]:
    """
    1) Sent Mail → mark 'email sent' where you already emailed the row (incl. outside this tool).
    2) Inbox → mark 'replied' for 'email sent' rows with a matching thread.

    Returns (n_sent_updated, n_replied_updated).
    """
    spreadsheet_id = normalize_spreadsheet_id(spreadsheet_id)
    print("  Step 1: Sent Mail (outbound to sheet emails)…")
    n_sent = sync_sent_status_from_sent_folder(
        spreadsheet_id, worksheet_name, only_row=only_row
    )
    print("  Step 2: Inbox (replies from contacts)…")
    n_rep = sync_replies_from_inbox(
        spreadsheet_id,
        worksheet_name,
        state_path,
        only_row=only_row,
    )
    return n_sent, n_rep
