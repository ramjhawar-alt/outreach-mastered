# Outreach Mastered

**CLI pipeline for web scraping, contact extraction, LLM-assisted enrichment, Google Sheets workflows, and template-based email outreach** — built to run locally with free-tier APIs where possible.

---

## Why this project (portfolio)

This repository demonstrates **end-to-end automation** across problems recruiters care about:

| Area | What the code shows |
|------|---------------------|
| **Browser automation** | Playwright-driven scraping with multiple extraction strategies (tables, link directories, single pages, infinite scroll). |
| **Data integration** | Google Sheets as a live CRM: append exports, in-place column updates, validation, and status columns. |
| **External APIs** | Service account auth (Google), REST clients (Groq, OpenRouter, You.com-style search, optional Apollo). |
| **Email systems** | MIME construction (HTML + plain-text fallback, PDF attachments), SMTP send, optional IMAP (drafts + reply sync). |
| **Reliability & safety** | Daily send caps, random inter-send delays, duplicate-address failsafes, file-based send state, optional send locking. |

It is a **small but real systems project**: configuration, error paths, and CLI design matter as much as the happy path.

---

## Tech stack

| Layer | Technologies |
|--------|----------------|
| Runtime | Python 3 |
| Browser | Playwright (Chromium) |
| Parsing | BeautifulSoup |
| Sheets | `gspread`, Google service account (Sheets + Drive scopes) |
| HTTP | `httpx` |
| LLM | Groq SDK; OpenRouter via HTTP (enrichment fallback) |
| Config | `python-dotenv` |
| Email | `smtplib`, `imaplib`, `email` (MIME) |

---

## Features

1. **Scrape** — Auto, table, links, or single-page modes; optional deep crawl to company sites for emails; scroll for lazy-loaded lists.  
2. **Enrich** — Web search + LLM to resolve company sites and short “What they do” copy; optional in-place sheet updates without re-scraping.  
3. **Export** — Append structured rows to Google Sheets with headers and email-status tracking.  
4. **Outreach** — Curly-brace placeholders in templates (`{contact_name}`, `{organization}`, `{email}`, `{what_they_do}`); plain text or HTML; multiple PDFs for event-style templates.  
5. **Operations** — Daily caps (`EMAIL_DAILY_LIMIT`), random delays, Gmail-centric reply sync back to the sheet, dry-run and draft modes.

---

## Quick start

```bash
git clone https://github.com/ramjhawar-alt/outreach-mastered.git
cd outreach-mastered
pip install -r requirements.txt
python3 -m playwright install chromium
cp .env.example .env
# Edit .env — never commit real keys (see .gitignore)
```

### Google Cloud & Sheets

1. Enable **Google Sheets API** and **Google Drive API**.  
2. Create a **service account**, download the JSON key (e.g. `credentials.json`).  
3. Create a spreadsheet in your own Google account and **share it with the service account email as Editor**.  
4. Use the spreadsheet ID from the URL: `https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit`.

Service accounts **cannot** own new Drive files in the same way a user account can; creating the sheet yourself and sharing it is the supported path.

### Environment variables (overview)

| Variable | Role |
|----------|------|
| `GOOGLE_CREDENTIALS_PATH` | Path to service account JSON |
| `GMAIL_FROM`, `GMAIL_APP_PASSWORD` | SMTP (and IMAP where used) for sending / drafts / sync |
| `YDC_API_KEY` | Web search for enrichment (provider-specific) |
| `GROQ_API_KEY` / `OPENROUTER_API_KEY` | LLM phrase generation and fallbacks |
| `APOLLO_API_KEY` | Optional people search (used only from explicit Python calls — not invoked by default CLI scrape/send) |
| `OUTREACH_SHEET_ID`, `OUTREACH_WORKSHEET` | Defaults when omitting `--from-sheet` / `--worksheet` |
| `OUTREACH_RESUME_PDF` | Default PDF for generic templates |
| `DEMO_DAY_ATTACHMENT_PDFS` | Comma-separated PDFs when using `templates/demo_day.html` |

See **`.env.example`** for a full template.

---

## Usage (examples)

### Scrape and export

```bash
python3 main.py -e -s YOUR_SHEET_ID "https://example.com/directory"
python3 main.py -m table -e -s YOUR_SHEET_ID "https://example.com/table-page"
python3 main.py -m links --scroll "https://example.com/infinite-list"
```

### Enrich an existing sheet (in place)

```bash
python3 main.py --from-sheet YOUR_SHEET_ID --worksheet YourTab \
  --enrich-sheet-in-place --enrich-org --enrich-sheet-what-only --enrich-only-empty
```

### Email outreach

Template first line: `Subject: …` — then body with placeholders. Preview:

```bash
python3 main.py --from-sheet YOUR_SHEET_ID --worksheet YourTab \
  --email --template templates/outreach_example.txt --dry-run
```

Daily-capped sends (random delay between messages; updates sheet + local state):

```bash
python3 -u main.py --email --email-daily --template templates/outreach_example.txt --yes \
  --from-sheet YOUR_SHEET_ID --worksheet YourTab
```

Sync Gmail → sheet status (`email sent` / `replied`):

```bash
python3 -u main.py --sync-email-replies --from-sheet YOUR_SHEET_ID --worksheet YourTab
```

### Optional: Apollo (Python API only)

```python
from src.apollo import find_founder_email

name, email, title = find_founder_email("ExampleCo", domain="example.com")
```

Apollo is **not** called automatically from `main.py` scrape or send paths; use it deliberately when you need founder lookup.

---

## Sheet schema

Flexible header matching (order-independent). Typical columns:

| Contact | Organization | Email | What they do | Source URL | Extracted At | Email status |
|---------|--------------|-------|----------------|------------|--------------|----------------|

Status values include `email not sent`, `email sent`, and `replied` (see `src/sheets.py`).

---

## CLI flags (cheat sheet)

| Flag | Purpose |
|------|---------|
| `-m auto\|table\|links\|single` | Extraction mode |
| `-e` / `--export` | Export to Google Sheets |
| `-s SHEET_ID` | Append to existing sheet |
| `--from-sheet`, `--worksheet` | Read contacts from a tab |
| `--email`, `--template` | Send from template |
| `--email-daily` | Daily cap, delays, state file, per-row updates |
| `--dry-run`, `--save-draft`, `-y` | Preview, drafts, skip confirmation |
| `--limit N` | Cap number of sends (testing) |
| `--sync-email-replies` | Gmail → sheet status |
| `--enrich-org`, `--enrich-sheet-in-place`, … | Org / phrase enrichment |

Run `python3 main.py -h` for the full list.

---

## Security & repo hygiene

- **Never commit** `.env`, `credentials.json`, or `outreach_send_state.json` (see `.gitignore`).  
- Use **app passwords** and least-privilege service accounts.  
- Respect provider **rate limits** and recipient consent; this tooling is for legitimate outreach you own.

---

## Project layout (high level)

```
main.py              # CLI entrypoint
src/
  browser.py         # Playwright page loads
  extractor.py       # HTML / text extraction
  sheets.py          # gspread read/write, migrations, status column
  emailer.py         # Templates, MIME, SMTP, IMAP drafts
  config.py          # Environment-driven settings
  outreach_state.py  # Daily quota + per-row send metadata
  reply_sync.py      # Gmail → sheet reconciliation
templates/           # Example .txt / .html bodies
attachments/         # PDFs (gitignored); see attachments/README.md
```

---

## Links

- **Repository:** [github.com/ramjhawar-alt/outreach-mastered](https://github.com/ramjhawar-alt/outreach-mastered)

If you are a **recruiter or reviewer**: the sections above are meant to map directly to **shipping skills** (integration, CLI UX, email protocols, and cautious automation). Clone, read `main.py` and `src/emailer.py`, and trace one command end-to-end for the fastest code tour.
