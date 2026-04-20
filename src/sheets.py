"""Google Sheets integration for exporting extracted data."""

import re
from datetime import datetime
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

from .config import GOOGLE_CREDENTIALS_PATH
from .extractor import ExtractedData

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Column G dropdown values (exact strings)
EMAIL_STATUS_NOT_SENT = "email not sent"
EMAIL_STATUS_SENT = "email sent"
EMAIL_STATUS_REPLIED = "replied"

HEADERS = [
    "Contact",
    "Organization",
    "Email",
    "What they do",
    "Source URL",
    "Extracted At",
    "Email status",
]


def _pad_row(row: list[str], length: int) -> list[str]:
    r = list(row)
    while len(r) < length:
        r.append("")
    return r[:length]


def _looks_like_http_url(cell: str) -> bool:
    c = (cell or "").strip()
    return c.startswith("http://") or c.startswith("https://")


def _is_five_column_header_row(row: list[str]) -> bool:
    """Old export: Contact, Organization, Email, Source URL, Extracted At (no What they do)."""
    if len(row) < 5:
        return False
    h = [x.strip().lower() for x in _pad_row(row, 5)]
    if "contact" not in h[0] and h[0] not in ("name", "contact name"):
        return False
    if "organization" not in h[1] and "company" not in h[1] and h[1] not in ("org", "company"):
        return False
    if "email" not in h[2]:
        return False
    if "source" not in h[3] and "url" not in h[3]:
        return False
    return "what they" not in " ".join(h)


def _looks_like_legacy_five_column_data_row(row: list[str]) -> bool:
    """Data row: name, org, email@, http URL, date — no What they do column."""
    if len(row) < 4:
        return False
    if "@" not in (row[2] or ""):
        return False
    return _looks_like_http_url(row[3])


def _migrate_legacy_five_column_sheet(worksheet) -> None:
    """
    Rewrite sheets that used 5 columns (no What they do) into the 7-column layout.

    Handles: (1) header + data with 5 cols, (2) data starting row 1 with no header row.
    """
    rows = worksheet.get_all_values()
    if not rows:
        return

    first = _pad_row(rows[0], 6)

    if _is_five_column_header_row(rows[0]):
        migrated: list[list[str]] = [HEADERS]
        for r in rows[1:]:
            p = _pad_row(r, 5)
            migrated.append(
                [p[0], p[1], p[2], "", p[3], p[4], EMAIL_STATUS_NOT_SENT]
            )
        worksheet.update(
            f"A1:G{len(migrated)}",
            migrated,
            value_input_option="USER_ENTERED",
        )
        return

    if _looks_like_legacy_five_column_data_row(first):
        migrated = [HEADERS]
        for r in rows:
            p = _pad_row(r, 5)
            migrated.append(
                [p[0], p[1], p[2], "", p[3], p[4], EMAIL_STATUS_NOT_SENT]
            )
        worksheet.update(
            f"A1:G{len(migrated)}",
            migrated,
            value_input_option="USER_ENTERED",
        )


def _apply_email_status_validation(worksheet) -> None:
    """Dropdown on column G (rows 2–10001): email not sent / email sent / replied."""
    spreadsheet = worksheet.spreadsheet
    sheet_id = worksheet.id
    body = {
        "requests": [
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": 10001,
                        "startColumnIndex": 6,
                        "endColumnIndex": 7,
                    },
                    "rule": {
                        "condition": {
                            "type": "ONE_OF_LIST",
                            "values": [
                                {"userEnteredValue": EMAIL_STATUS_NOT_SENT},
                                {"userEnteredValue": EMAIL_STATUS_SENT},
                                {"userEnteredValue": EMAIL_STATUS_REPLIED},
                            ],
                        },
                        "showCustomUi": True,
                        "strict": True,
                    },
                }
            }
        ]
    }
    try:
        spreadsheet.batch_update(body)
    except Exception as e:
        print(f"  (Could not set Email status dropdown: {e})")


def _ensure_email_status_column(worksheet) -> None:
    """Ensure column G exists with header 'Email status' and default status on blank cells."""
    rows = worksheet.get_all_values()
    max_w = max((len(r) for r in rows), default=0) if rows else 0

    # Expand the worksheet grid so column G exists in the UI (6-col tabs often have col_count=6).
    if worksheet.col_count < 7:
        worksheet.resize(cols=max(7, worksheet.col_count))

    if not rows:
        worksheet.update(
            "A1:G1",
            [HEADERS],
            value_input_option="USER_ENTERED",
        )
        _apply_email_status_validation(worksheet)
        return

    out: list[list[str]] = []
    # If no row ever had a 7th cell, we must write G for header + data even if values look unchanged.
    changed = max_w < 7
    for i, row in enumerate(rows):
        p = _pad_row(row, 7)
        if i == 0:
            if (p[6] or "").strip().lower() != "email status":
                p[6] = "Email status"
                changed = True
            out.append(p[:7])
            continue
        if len(row) < 7 or not (p[6] or "").strip():
            p[6] = EMAIL_STATUS_NOT_SENT
            changed = True
        out.append(p[:7])
    if changed:
        worksheet.update(
            f"A1:G{len(out)}",
            out,
            value_input_option="USER_ENTERED",
        )
    _apply_email_status_validation(worksheet)


def ensure_email_status_column_on_sheet(
    spreadsheet_id: str,
    worksheet_name: str = "Organizations",
) -> str:
    """
    Open the tab, migrate to 7 columns, set 'Email status' header + defaults, apply dropdown.
    Use this if column G is missing in the browser.
    """
    client = _get_client()
    sheet_id = _extract_sheet_id(spreadsheet_id)
    spreadsheet = client.open_by_key(sheet_id)
    worksheet = spreadsheet.worksheet(worksheet_name)
    _prepare_worksheet(worksheet)
    return spreadsheet.url


def _prepare_worksheet(worksheet) -> None:
    """Run migrations so the tab has 7 columns + email status."""
    _migrate_legacy_five_column_sheet(worksheet)
    _ensure_email_status_column(worksheet)


def _sheet_needs_full_headers(first_row: list[str]) -> bool:
    """True if row 1 is clearly a short header row (not data)."""
    if not first_row:
        return True
    if len(first_row) >= len(HEADERS):
        return False
    a = (first_row[0] or "").strip().lower()
    if a in ("contact", "name", "contact name"):
        return True
    return False


def _extract_sheet_id(value: str) -> str:
    """Extract sheet ID from full URL or return as-is if already an ID."""
    value = value.strip()
    if "docs.google.com" in value or "/spreadsheets/d/" in value:
        match = re.search(r"/d/([a-zA-Z0-9_-]+)", value)
        if match:
            return match.group(1)
    return value


def normalize_spreadsheet_id(value: str) -> str:
    """Accept raw sheet ID or full `docs.google.com` URL (e.g. from zsh-quoted paste)."""
    return _extract_sheet_id((value or "").strip())


def _get_client() -> gspread.Client:
    """Create authenticated gspread client."""
    creds_path = Path(GOOGLE_CREDENTIALS_PATH).expanduser().resolve()
    if not creds_path.exists():
        raise FileNotFoundError(
            f"Credentials file not found: {creds_path}\n"
            "Create a service account in Google Cloud Console and download the JSON key."
        )
    creds = Credentials.from_service_account_file(str(creds_path), scopes=SCOPES)
    return gspread.authorize(creds)


def _data_to_rows(data: list[ExtractedData]) -> list[list]:
    """Convert ExtractedData list to rows for the sheet."""
    rows = []
    now = datetime.utcnow().strftime("%Y-%m-%d")
    for item in data:
        contact_name = getattr(item, "contact_name", None) or ""
        wtd = getattr(item, "what_they_do", None) or ""
        status = getattr(item, "email_status", None) or EMAIL_STATUS_NOT_SENT
        if item.emails:
            for email in item.emails:
                rows.append(
                    [
                        contact_name,
                        item.organization,
                        email,
                        wtd,
                        item.source_url,
                        now,
                        status,
                    ]
                )
        else:
            rows.append(
                [
                    contact_name,
                    item.organization,
                    "",
                    wtd,
                    item.source_url,
                    now,
                    status,
                ]
            )
    return rows


def append_to_sheet(
    spreadsheet_id: str | None,
    worksheet_name: str = "Organizations",
    data: list[ExtractedData] | None = None,
) -> str:
    """
    Append extracted data to a Google Sheet.

    Args:
        spreadsheet_id: The ID from the sheet URL, or None to create a new sheet
        worksheet_name: Name of the worksheet/tab
        data: List of ExtractedData to append

    Returns:
        The spreadsheet URL
    """
    if not data:
        raise ValueError("No data to export")

    client = _get_client()

    if spreadsheet_id:
        sheet_id = _extract_sheet_id(spreadsheet_id)
        spreadsheet = client.open_by_key(sheet_id)
    else:
        title = f"Organizations - {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"
        try:
            spreadsheet = client.create(title)
        except Exception as e:
            if "quota" in str(e).lower() or "403" in str(e):
                raise RuntimeError(
                    "Cannot create a new spreadsheet — the service account has no Drive storage. "
                    "Create the sheet manually in Google Sheets, share it with the service account "
                    "email (Editor), and pass -s SHEET_ID."
                ) from e
            raise

    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows=1000, cols=10)

    _prepare_worksheet(worksheet)

    existing = worksheet.get_all_values()
    if not existing:
        worksheet.update("A1:G1", [HEADERS], value_input_option="USER_ENTERED")
    elif _sheet_needs_full_headers(existing[0]):
        worksheet.update("A1:G1", [HEADERS], value_input_option="USER_ENTERED")

    rows = _data_to_rows(data)
    if rows:
        worksheet.append_rows(rows, value_input_option="USER_ENTERED")

    return spreadsheet.url


def _header_column_indices(
    headers_lower: list[str],
) -> tuple[int, int, int, int | None, int | None, int | None]:
    """Return (contact_idx, org_idx, email_idx, what_idx, source_url_idx, email_status_idx)."""
    contact_idx = org_idx = email_idx = what_idx = url_idx = status_idx = None
    for i, h in enumerate(headers_lower):
        if h in ("contact", "name", "contact name"):
            contact_idx = i
        elif h in ("organization", "org", "company"):
            org_idx = i
        elif h in ("email", "e-mail"):
            email_idx = i
        elif h in (
            "what they do",
            "what they do / pitch",
            "pitch",
            "description",
            "tagline",
        ) or "what they" in h:
            what_idx = i
        elif "source" in h and "url" in h:
            url_idx = i
        elif h in ("url", "website", "company url", "source url"):
            url_idx = i
        elif h == "email status" or h == "email_status" or (
            "email" in h and "status" in h
        ):
            status_idx = i

    if contact_idx is None:
        contact_idx = 0
    if org_idx is None:
        org_idx = 1
    if email_idx is None:
        email_idx = 2
    if status_idx is None and len(headers_lower) > 6:
        status_idx = 6
    return contact_idx, org_idx, email_idx, what_idx, url_idx, status_idx


def read_from_sheet_with_row_numbers(
    spreadsheet_id: str,
    worksheet_name: str = "Organizations",
) -> list[tuple[int, ExtractedData, str, str]]:
    """
    Read sheet rows as ExtractedData with 1-based sheet row numbers and original D/E text.

    Returns:
        List of (row_number, item, original_what_they_do, original_source_url)
    """
    client = _get_client()
    sheet_id = _extract_sheet_id(spreadsheet_id)
    spreadsheet = client.open_by_key(sheet_id)
    worksheet = spreadsheet.worksheet(worksheet_name)
    _prepare_worksheet(worksheet)
    rows = worksheet.get_all_values()

    if not rows:
        return []

    headers = [h.strip().lower() for h in rows[0]]
    contact_idx, org_idx, email_idx, what_idx, url_idx, status_idx = (
        _header_column_indices(headers)
    )

    idxs = [contact_idx, org_idx, email_idx]
    if what_idx is not None:
        idxs.append(what_idx)
    if url_idx is not None:
        idxs.append(url_idx)
    if status_idx is not None:
        idxs.append(status_idx)
    max_idx = max(idxs)

    out: list[tuple[int, ExtractedData, str, str]] = []
    for row_num, row in enumerate(rows[1:], start=2):
        row = _pad_row(row, max(max_idx + 1, 7))
        contact = row[contact_idx].strip() if contact_idx < len(row) else ""
        org = row[org_idx].strip() if org_idx < len(row) else ""
        email = row[email_idx].strip() if email_idx < len(row) else ""
        what = (
            row[what_idx].strip()
            if what_idx is not None and what_idx < len(row)
            else ""
        )
        src = (
            row[url_idx].strip()
            if url_idx is not None and url_idx < len(row)
            else ""
        )
        estatus: str | None = None
        if status_idx is not None and status_idx < len(row):
            estatus = row[status_idx].strip() or None
        if not org and not email:
            continue
        item = ExtractedData(
            organization=org or "(unknown)",
            emails=[email] if email else [],
            source_url=src,
            contact_name=contact or None,
            what_they_do=what or None,
            sheet_row=row_num,
            email_status=estatus,
        )
        out.append((row_num, item, what, src))
    return out


def read_from_sheet(
    spreadsheet_id: str,
    worksheet_name: str = "Organizations",
) -> list[ExtractedData]:
    """
    Read contacts from a Google Sheet. Expects columns: Contact, Organization, Email,
    optionally What they do (or similar). Uses first row as headers, maps by header name.

    Args:
        spreadsheet_id: Sheet ID or full URL
        worksheet_name: Worksheet/tab name

    Returns:
        List of ExtractedData
    """
    return [t[1] for t in read_from_sheet_with_row_numbers(spreadsheet_id, worksheet_name)]


def update_enrichment_columns_in_place(
    spreadsheet_id: str,
    worksheet_name: str = "Organizations",
    row_snapshots: list[tuple[int, ExtractedData, str, str]] | None = None,
    *,
    url_only: bool = False,
    what_column_only: bool = False,
) -> str:
    """
    Write column D (What they do) and optionally E (Source URL) for existing rows.

    row_snapshots: (sheet_row_1based, item, original_D, original_E) after enrichment was
    applied to each item in memory. Uses per-row ranges so blank rows between data rows
    do not misalign updates.

    If what_column_only is True, only column D is written (E is left unchanged on the sheet).

    Returns:
        Spreadsheet URL
    """
    if not row_snapshots:
        raise ValueError("No rows to update")

    client = _get_client()
    sheet_id = _extract_sheet_id(spreadsheet_id)
    spreadsheet = client.open_by_key(sheet_id)
    worksheet = spreadsheet.worksheet(worksheet_name)
    _prepare_worksheet(worksheet)

    batch: list[dict] = []
    for r, item, orig_what, orig_src in row_snapshots:
        new_what = item.what_they_do or orig_what
        new_src = item.source_url or orig_src
        if url_only:
            new_what = orig_what
            new_src = item.source_url or orig_src
        batch.append({"range": f"D{r}", "values": [[new_what]]})
        if not what_column_only:
            batch.append({"range": f"E{r}", "values": [[new_src]]})

    chunk = 100
    for i in range(0, len(batch), chunk):
        worksheet.batch_update(
            batch[i : i + chunk],
            raw=False,
            value_input_option="USER_ENTERED",
        )
    return spreadsheet.url


def update_email_status_cells(
    spreadsheet_id: str,
    worksheet_name: str,
    updates: list[tuple[int, str]],
) -> None:
    """Write column G (Email status) for specific 1-based row numbers."""
    if not updates:
        return
    client = _get_client()
    sheet_id = _extract_sheet_id(spreadsheet_id)
    spreadsheet = client.open_by_key(sheet_id)
    worksheet = spreadsheet.worksheet(worksheet_name)
    _prepare_worksheet(worksheet)
    batch = [{"range": f"G{r}", "values": [[v]]} for r, v in updates]
    chunk = 100
    for i in range(0, len(batch), chunk):
        worksheet.batch_update(
            batch[i : i + chunk],
            raw=False,
            value_input_option="USER_ENTERED",
        )


def create_new_sheet(data: list[ExtractedData], worksheet_name: str = "Organizations") -> str:
    """
    Create a new Google Sheet and populate it with extracted data.

    Args:
        data: List of ExtractedData
        worksheet_name: Name of the worksheet

    Returns:
        The spreadsheet URL
    """
    return append_to_sheet(None, worksheet_name, data)
