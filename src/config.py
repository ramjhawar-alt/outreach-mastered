"""Configuration and environment variables."""

import os
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
load_dotenv(_PROJECT_ROOT / ".env.example", override=False)

GOOGLE_CREDENTIALS_PATH = os.getenv(
    "GOOGLE_CREDENTIALS_PATH",
    str(Path.cwd() / "credentials.json"),
)

# Web search for --enrich-org (You.com / YDC)
YDC_API_KEY = (os.getenv("YDC_API_KEY") or "").strip()
YDC_SEARCH_COUNT = int(os.getenv("YDC_SEARCH_COUNT", "10"))

# Groq (free tier) — primary LLM for "What they do" phrase generation
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# Which LLM to try first: "groq" or "openrouter" (the other is fallback)
ENRICH_LLM_PRIMARY = (os.getenv("ENRICH_LLM_PRIMARY") or "groq").strip().lower()

# OpenRouter (free tier fallback)
OPENROUTER_API_KEY = (os.getenv("OPENROUTER_API_KEY") or "").strip()
OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "arcee-ai/trinity-large-preview:free",
)
OPENROUTER_HTTP_REFERER = os.getenv(
    "OPENROUTER_HTTP_REFERER",
    "https://github.com/local/outreach-mastered",
)
OPENROUTER_ENRICH_GAP_SEC = float(os.getenv("OPENROUTER_ENRICH_GAP_SEC", "3"))

PAGE_LOAD_TIMEOUT_MS = 30000
REQUEST_DELAY_SEC = 1

# Gmail SMTP
GMAIL_FROM = (os.getenv("GMAIL_FROM", "") or "").strip()
_raw_gmail_aliases = (os.getenv("GMAIL_FROM_ALIASES") or "").strip()
GMAIL_FROM_ALIASES: list[str] = [
    x.strip().lower() for x in _raw_gmail_aliases.split(",") if x.strip()
]
_raw_app_pw = (os.getenv("GMAIL_APP_PASSWORD", "") or "").strip()
GMAIL_APP_PASSWORD = _raw_app_pw.replace(" ", "")
EMAIL_DELAY_SEC = int(os.getenv("EMAIL_DELAY_SEC", "3"))
EMAIL_DELAY_MIN_SEC = float(os.getenv("EMAIL_DELAY_MIN_SEC", "60"))
EMAIL_DELAY_MAX_SEC = float(os.getenv("EMAIL_DELAY_MAX_SEC", "180"))
EMAIL_DAILY_LIMIT = int(os.getenv("EMAIL_DAILY_LIMIT", "50"))

OUTREACH_SHEET_ID = (os.getenv("OUTREACH_SHEET_ID") or "").strip()

_raw_state = (os.getenv("OUTREACH_STATE_PATH") or "").strip()
if _raw_state:
    _sp = Path(_raw_state).expanduser()
    OUTREACH_STATE_PATH: Path = (
        _sp.resolve() if _sp.is_absolute() else (_PROJECT_ROOT / _sp).resolve()
    )
else:
    OUTREACH_STATE_PATH = (_PROJECT_ROOT / "outreach_send_state.json").resolve()

OUTREACH_SYNC_VERBOSE = os.getenv("OUTREACH_SYNC_VERBOSE", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

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
