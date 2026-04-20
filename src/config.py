"""Configuration and environment variables."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Project root (parent of src/)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Prefer .env; fill missing keys from .env.example (e.g. Gmail copied into example only)
load_dotenv(_PROJECT_ROOT / ".env")
load_dotenv(_PROJECT_ROOT / ".env.example", override=False)

# Google Sheets
GOOGLE_CREDENTIALS_PATH = os.getenv(
    "GOOGLE_CREDENTIALS_PATH",
    str(Path.cwd() / "credentials.json"),
)


# Web search for --enrich-org: You.com (YDC) if YDC_API_KEY or ydc-sk- key in BRAVE_API_KEY;
# else Brave; else Google CSE.
# Set BRAVE_API_KEY in .env. If .env has BRAVE_API_KEY= with nothing after =, it overrides
# any default—leave the line out until you paste a real key.
YDC_API_KEY = (os.getenv("YDC_API_KEY") or "").strip()
BRAVE_API_KEY = (os.getenv("BRAVE_API_KEY") or "").strip()
BRAVE_SEARCH_COUNT = int(os.getenv("BRAVE_SEARCH_COUNT", "10"))
# Brave may 422 if true on plans without extra_snippets; set BRAVE_EXTRA_SNIPPETS=1 to enable
BRAVE_EXTRA_SNIPPETS = os.getenv("BRAVE_EXTRA_SNIPPETS", "").lower() in ("1", "true", "yes")

# Google Custom Search JSON API — fallback when BRAVE_API_KEY is unset
GOOGLE_CSE_API_KEY = os.getenv("GOOGLE_CSE_API_KEY", "")
GOOGLE_CSE_CX = os.getenv("GOOGLE_CSE_CX", "")

# Groq Chat Completions (free tier) — short "approach to …" phrase
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# Phrase step: try this provider first ("groq" or "openrouter"); other is fallback if its key is set
ENRICH_LLM_PRIMARY = (os.getenv("ENRICH_LLM_PRIMARY") or "openrouter").strip().lower()

# OpenRouter — optional primary or fallback per ENRICH_LLM_PRIMARY
OPENROUTER_API_KEY = (os.getenv("OPENROUTER_API_KEY") or "").strip()
OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "arcee-ai/trinity-large-preview:free",
)
OPENROUTER_HTTP_REFERER = os.getenv(
    "OPENROUTER_HTTP_REFERER",
    "https://github.com/local/outreach-mastered",
)
# Extra pause between orgs during --enrich-org (reduces OpenRouter 429s on free models)
OPENROUTER_ENRICH_GAP_SEC = float(os.getenv("OPENROUTER_ENRICH_GAP_SEC", "3"))

# Browser settings
PAGE_LOAD_TIMEOUT_MS = 30000  # 30 seconds
REQUEST_DELAY_SEC = 1  # Delay between requests to be respectful

# Email (Gmail SMTP) — from .env and/or .env.example
GMAIL_FROM = (os.getenv("GMAIL_FROM", "") or "").strip()
# Comma-separated extra From addresses (Gmail "Send mail as") for Sent/reply IMAP matching
_raw_gmail_aliases = (os.getenv("GMAIL_FROM_ALIASES") or "").strip()
GMAIL_FROM_ALIASES: list[str] = [
    x.strip().lower() for x in _raw_gmail_aliases.split(",") if x.strip()
]
_raw_app_pw = (os.getenv("GMAIL_APP_PASSWORD", "") or "").strip()
# Google displays app passwords with spaces; SMTP expects 16 chars without spaces
GMAIL_APP_PASSWORD = _raw_app_pw.replace(" ", "")
EMAIL_DELAY_SEC = int(os.getenv("EMAIL_DELAY_SEC", "3"))
# Randomized delay between outreach emails (used when min/max both set or for --email-daily)
EMAIL_DELAY_MIN_SEC = float(os.getenv("EMAIL_DELAY_MIN_SEC", "60"))
EMAIL_DELAY_MAX_SEC = float(os.getenv("EMAIL_DELAY_MAX_SEC", "180"))
# Max sends per local calendar day (--email-daily); tracked in outreach_send_state.json
EMAIL_DAILY_LIMIT = int(os.getenv("EMAIL_DAILY_LIMIT", "50"))
# Default sheet for --email-daily when --from-sheet omitted
OUTREACH_SHEET_ID = (os.getenv("OUTREACH_SHEET_ID") or "").strip()
# Local JSON: quota + per-row Message-ID / sent_at for reply sync (gitignored by default)
_raw_state = (os.getenv("OUTREACH_STATE_PATH") or "").strip()
if _raw_state:
    _sp = Path(_raw_state).expanduser()
    OUTREACH_STATE_PATH: Path = (
        _sp.resolve() if _sp.is_absolute() else (_PROJECT_ROOT / _sp).resolve()
    )
else:
    OUTREACH_STATE_PATH = (_PROJECT_ROOT / "outreach_send_state.json").resolve()
# Set to 1 to print per-row debug during --sync-email-replies
OUTREACH_SYNC_VERBOSE = os.getenv("OUTREACH_SYNC_VERBOSE", "").strip().lower() in (
    "1",
    "true",
    "yes",
)
# PDF attached to each outreach email (absolute path or path relative to project root)
_raw_resume = (os.getenv("OUTREACH_RESUME_PDF") or "").strip()
# Apollo.io — people search + email enrichment
APOLLO_API_KEY = (os.getenv("APOLLO_API_KEY") or "").strip()
APOLLO_CONTACT_TITLES = [
    t.strip()
    for t in (os.getenv("APOLLO_CONTACT_TITLES") or "Founder,CEO,Co-Founder,CTO").split(",")
    if t.strip()
]

OUTREACH_RESUME_PDF: Path | None
if _raw_resume:
    _rp = Path(_raw_resume).expanduser()
    OUTREACH_RESUME_PDF = (
        _rp.resolve() if _rp.is_absolute() else (_PROJECT_ROOT / _rp).resolve()
    )
else:
    OUTREACH_RESUME_PDF = None
