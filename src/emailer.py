"""Email outreach: template-based personalized emails via Gmail SMTP."""

import imaplib
import random
import smtplib
import time
from collections.abc import Callable
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.policy import SMTP
from email.utils import make_msgid
from pathlib import Path

from .config import (
    EMAIL_DELAY_MAX_SEC,
    EMAIL_DELAY_MIN_SEC,
    EMAIL_DELAY_SEC,
    GMAIL_APP_PASSWORD,
    GMAIL_FROM,
    OUTREACH_RESUME_PDF,
)
from .extractor import ExtractedData

PLACEHOLDERS = {"contact_name", "organization", "email"}

# Project root = parent of `src/` (so `templates/foo.txt` works no matter the shell cwd)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_template_path(path: str) -> Path:
    """Absolute path to template; relative paths are resolved from project root, not cwd."""
    p = Path(path).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (_PROJECT_ROOT / p).resolve()


def _first_name_for_greeting(full_name: str) -> str:
    """First token only for 'Hi {name},' (e.g. 'Ewerton Rocha' -> 'Ewerton')."""
    s = (full_name or "").strip()
    if not s:
        return ""
    return s.split()[0]


def load_template(path: str) -> tuple[str, str]:
    """
    Load email template from file. First line starting with "Subject:" is the subject.
    Rest is body. Placeholders: {contact_name} (first name only), {organization}, {email}, {what_they_do}

    Returns:
        (subject, body)
    """
    p = _resolve_template_path(path)
    if not p.exists():
        raise FileNotFoundError(f"Template not found: {p}")

    text = p.read_text(encoding="utf-8")
    lines = text.strip().split("\n")
    subject = ""
    body_lines: list[str] = []

    for line in lines:
        if line.strip().lower().startswith("subject:"):
            subject = line.split(":", 1)[1].strip()
        else:
            body_lines.append(line)

    body = "\n".join(body_lines).strip()
    if not subject:
        subject = "Reaching out"
    return subject, body


def personalize(subject: str, body: str, item: ExtractedData) -> tuple[str, str]:
    """Replace placeholders with values from ExtractedData."""
    org = item.organization or ""
    email = item.emails[0] if item.emails else ""
    raw_contact = getattr(item, "contact_name", None) or ""
    contact = _first_name_for_greeting(raw_contact)
    what = getattr(item, "what_they_do", None) or ""
    if not what.strip():
        what = "what you're building"  # Natural fallback for {what_they_do}

    replacements = {
        "{contact_name}": contact,
        "{organization}": org,
        "{email}": email,
        "{what_they_do}": what,
    }
    subj = subject
    b = body
    for k, v in replacements.items():
        subj = subj.replace(k, v)
        b = b.replace(k, v)
    return subj, b


def _build_outreach_mime(
    from_email: str,
    to_email: str,
    subject: str,
    body: str,
    attachment_pdf: Path | None,
    message_id: str | None = None,
) -> MIMEMultipart:
    """Plain-text body plus optional PDF (same structure for SMTP send and IMAP draft)."""
    msg = MIMEMultipart()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    mid = message_id or make_msgid()
    msg["Message-ID"] = mid
    msg.attach(MIMEText(body, "plain", "utf-8"))

    if attachment_pdf is not None and attachment_pdf.is_file():
        with open(attachment_pdf, "rb") as fp:
            part = MIMEBase("application", "pdf")
            part.set_payload(fp.read())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            "attachment",
            filename=attachment_pdf.name,
        )
        msg.attach(part)
    return msg


def send_email(
    to_email: str,
    subject: str,
    body: str,
    from_email: str,
    app_password: str,
    attachment_pdf: Path | None = None,
    message_id: str | None = None,
) -> tuple[bool, str | None]:
    """
    Send a single email via Gmail SMTP. Optional PDF attachment.

    Returns:
        (success, message_id) — message_id is set when the MIME was built (for reply tracking).
    """
    if not to_email or not app_password:
        return False, None

    mid = message_id or make_msgid()
    msg = _build_outreach_mime(
        from_email, to_email, subject, body, attachment_pdf, message_id=mid
    )

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(from_email, app_password)
            server.sendmail(from_email, to_email, msg.as_string())
        return True, mid
    except Exception:
        return False, None


def save_gmail_draft(
    to_email: str,
    subject: str,
    body: str,
    from_email: str,
    app_password: str,
    attachment_pdf: Path | None = None,
) -> bool:
    """
    Append a message to Gmail's Drafts folder over IMAP (same app password as SMTP).
    Open Gmail → Drafts to review; attachment appears there (not in terminal previews).
    """
    if not to_email or not app_password:
        return False

    msg = _build_outreach_mime(from_email, to_email, subject, body, attachment_pdf)
    raw = msg.as_bytes(policy=SMTP)

    draft_mailboxes = ("[Gmail]/Drafts", "Drafts")
    try:
        conn = imaplib.IMAP4_SSL("imap.gmail.com")
        conn.login(from_email, app_password)
        last_err: str | None = None
        for mbox in draft_mailboxes:
            try:
                typ, _ = conn.append(
                    mbox,
                    "",
                    imaplib.Time2Internaldate(time.time()),
                    raw,
                )
                if typ == "OK":
                    conn.logout()
                    return True
                last_err = f"{mbox}: {typ}"
            except imaplib.IMAP4.error as e:
                last_err = f"{mbox}: {e}"
        conn.logout()
        if last_err:
            print(f"  IMAP draft error: {last_err}")
        return False
    except imaplib.IMAP4.error as e:
        print(f"  IMAP login/draft error: {e}")
        return False
    except OSError as e:
        print(f"  IMAP connection error: {e}")
        return False


def send_outreach(
    items: list[ExtractedData],
    template_path: str,
    dry_run: bool = False,
    save_draft: bool = False,
    limit: int | None = None,
    *,
    use_random_delay: bool = False,
    after_each_send: Callable[[ExtractedData, str, str], None] | None = None,
) -> tuple[int, int]:
    """
    Send personalized emails to each contact with an email address.

    Args:
        items: List of ExtractedData (must have emails)
        template_path: Path to template file
        dry_run: If True, only preview, don't send
        save_draft: If True, append each message to Gmail Drafts (IMAP); mutually exclusive with dry_run
        limit: Max emails to send (None = all)
        use_random_delay: If True, sleep a random duration between EMAIL_DELAY_MIN_SEC and
            EMAIL_DELAY_MAX_SEC between sends (else EMAIL_DELAY_SEC).
        after_each_send: If set, called after each successful SMTP send as
            (item, to_email, message_id).

    Returns:
        (sent_count, skipped_count)
    """
    if dry_run and save_draft:
        raise ValueError("dry_run and save_draft cannot both be True")

    resolved_tpl = _resolve_template_path(template_path)
    if dry_run:
        mtime = datetime.fromtimestamp(resolved_tpl.stat().st_mtime)
        print(
            f"Template: {resolved_tpl}\n"
            f"Last saved on disk: {mtime:%Y-%m-%d %H:%M:%S} "
            "(save the file in your editor before running to pick up edits)\n"
        )

    subject_tpl, body_tpl = load_template(template_path)

    # Filter to items with emails
    to_send = [i for i in items if i.emails]
    if limit:
        to_send = to_send[:limit]

    if not to_send:
        return 0, len(items)

    if not dry_run and (not GMAIL_APP_PASSWORD or not GMAIL_FROM):
        raise ValueError(
            "Set GMAIL_APP_PASSWORD and GMAIL_FROM in .env. "
            "Get app password from Google Account > Security > App passwords."
        )

    if save_draft:
        print(
            "Creating Gmail draft(s) via IMAP — open **Drafts** in Gmail to see the full message "
            "and PDF (the terminal only shows plain text).\n"
        )

    if not dry_run and OUTREACH_RESUME_PDF is not None and not OUTREACH_RESUME_PDF.is_file():
        raise FileNotFoundError(
            f"Resume PDF not found (OUTREACH_RESUME_PDF): {OUTREACH_RESUME_PDF}"
        )

    resume_path = (
        OUTREACH_RESUME_PDF
        if OUTREACH_RESUME_PDF is not None and OUTREACH_RESUME_PDF.is_file()
        else None
    )
    if dry_run:
        if resume_path:
            rm = datetime.fromtimestamp(resume_path.stat().st_mtime)
            print(
                f"Resume attachment: {resume_path.name}\n"
                f"  Path: {resume_path}\n"
                f"  Last saved on disk: {rm:%Y-%m-%d %H:%M:%S}\n"
                f"  (The terminal cannot show the PDF; use --save-draft to create a Gmail draft with it.)\n"
            )
        elif OUTREACH_RESUME_PDF is not None:
            print(f"Warning: OUTREACH_RESUME_PDF set but file missing: {OUTREACH_RESUME_PDF}\n")
        else:
            print("Resume attachment: none (set OUTREACH_RESUME_PDF in .env to attach a PDF)\n")

    sent = 0
    skipped = len(items) - len(to_send)

    for i, item in enumerate(to_send):
        email_addr = item.emails[0]
        subj, body = personalize(subject_tpl, body_tpl, item)

        if dry_run:
            print(f"\n--- Email {i + 1} (dry run) ---")
            print(f"To: {email_addr}")
            print(f"Subject: {subj}")
            print(f"Body:\n{body}\n")
            sent += 1
            continue

        if save_draft:
            success = save_gmail_draft(
                email_addr,
                subj,
                body,
                GMAIL_FROM,
                GMAIL_APP_PASSWORD,
                attachment_pdf=resume_path,
            )
            if success:
                sent += 1
                print(f"  Draft saved for {item.organization} → {email_addr}")
            else:
                skipped += 1
                print(f"  Draft failed: {email_addr}")
        else:
            success, msg_id = send_email(
                email_addr,
                subj,
                body,
                GMAIL_FROM,
                GMAIL_APP_PASSWORD,
                attachment_pdf=resume_path,
            )
            if success:
                sent += 1
                print(f"  Sent to {item.organization} ({email_addr})")
                if after_each_send and msg_id:
                    after_each_send(item, email_addr, msg_id)
            else:
                skipped += 1
                print(f"  Failed: {email_addr}")

        if i < len(to_send) - 1:
            if use_random_delay:
                delay = random.uniform(EMAIL_DELAY_MIN_SEC, EMAIL_DELAY_MAX_SEC)
            else:
                delay = float(EMAIL_DELAY_SEC)
            time.sleep(delay)

    return sent, skipped
