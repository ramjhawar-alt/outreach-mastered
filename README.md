# Outreach Mastered

Scrape websites for contacts, enrich with Apollo.io, and send personalized outreach at scale — all from the command line. 100% free-tier compatible.

## What it does

1. **Scrape** any website (structured tables, link directories, or single pages) to extract organizations, contacts, and emails
2. **Enrich** missing data — find founder emails via Apollo.io, generate "What they do" descriptions via free LLMs (Groq / OpenRouter)
3. **Export** everything to Google Sheets with status tracking
4. **Send** personalized emails from templates with daily caps, random delays, and reply detection

## Setup

### 1. Clone and install

```bash
git clone https://github.com/ramjhawar-alt/outreach-mastered.git
cd outreach-mastered
pip install -r requirements.txt
python3 -m playwright install chromium
```

### 2. Configure credentials

```bash
cp .env.example .env
```

Edit `.env` with your keys:

| Key | What it does | Where to get it |
|-----|-------------|----------------|
| `GOOGLE_CREDENTIALS_PATH` | Google Sheets read/write | [Google Cloud Console](https://console.cloud.google.com) — create a service account, download JSON key, save as `credentials.json` |
| `GMAIL_FROM` + `GMAIL_APP_PASSWORD` | Send outreach emails | [Google App Passwords](https://myaccount.google.com/apppasswords) (requires 2FA) |
| `YDC_API_KEY` | Web search for org enrichment | [You.com](https://documentation.you.com/) (free) |
| `GROQ_API_KEY` | LLM for "What they do" phrases | [Groq Console](https://console.groq.com/) (free) |
| `OPENROUTER_API_KEY` | Fallback LLM | [OpenRouter](https://openrouter.ai/keys) (free models available) |
| `APOLLO_API_KEY` | Find founder/CEO emails | [Apollo.io](https://app.apollo.io/#/settings/integrations/api) (free search, 1 credit per email reveal) |

### 3. Set up Google Sheets

1. In Google Cloud Console: enable **Google Sheets API** and **Google Drive API**
2. Create a service account and download the JSON key as `credentials.json`
3. Create a Google Sheet manually and share it with the service account email (as **Editor**)
4. Copy the Sheet ID from the URL: `https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit`

> **Note:** The service account cannot create new spreadsheets. Always create sheets from your personal Google account and share them.

## Usage

### Scrape a website

```bash
# Auto-detect mode (tables → links → single page)
python3 main.py -e -s YOUR_SHEET_ID "https://example.com/directory"

# Table mode (pages with HTML tables of contacts)
python3 main.py -m table -e -s YOUR_SHEET_ID "https://arpa-h.gov/explore-funding/programs/advocate/teaming"

# Link-based directories (e.g. YC companies)
python3 main.py -m links --scroll "https://www.workatastartup.com/companies?jobType=intern"

# Single company page
python3 main.py -m single -e -s YOUR_SHEET_ID "https://acme.com"
```

### Find founder emails with Apollo

```python
from src.apollo import find_founder_email

name, email, title = find_founder_email("Readily", domain="readily.co")
```

Apollo uses two endpoints:
- **People Search** (free) — finds people by company domain + title
- **People Enrichment** (1 credit) — reveals verified work email

### Enrich existing sheet data

```bash
python3 main.py --from-sheet YOUR_SHEET_ID --enrich-sheet-in-place \
  --enrich-org --enrich-sheet-what-only --enrich-only-empty
```

### Send outreach emails

**Create a template** (see `templates/outreach_example.txt`):
```
Subject: Interested in {organization}

Hi {contact_name},

I came across {organization} and found the work towards {what_they_do} really compelling.

Would love to connect and learn more.

Best,
Your Name
```

Placeholders: `{contact_name}`, `{organization}`, `{email}`, `{what_they_do}`

**Preview emails (dry run):**
```bash
python3 main.py --from-sheet YOUR_SHEET_ID \
  --email --template templates/outreach_example.txt --dry-run
```

**Send emails with daily cap (50/day, random 60–180s delays):**
```bash
python3 -u main.py --email --template templates/outreach_example.txt --email-daily --yes
```

**Sync reply status from Gmail:**
```bash
python3 -u main.py --sync-email-replies --from-sheet YOUR_SHEET_ID
```

Updates the sheet: `email not sent` → `email sent` → `replied`.

### Bring your own sheet

The tool reads column headers by name — column order doesn't matter:

| Recognized headers | Maps to |
|---|---|
| `Contact`, `Name`, `Contact Name` | Contact person |
| `Organization`, `Org`, `Company` | Company name |
| `Email`, `E-mail` | Email address |
| `What they do`, `Description`, `Pitch` | Description |
| `URL`, `Website`, `Source URL` | Company URL |
| `Email status` | Send tracking |

## Sheet format

| Contact | Organization | Email | What they do | Source URL | Extracted At | Email status |
|---------|-------------|-------|-------------|-----------|-------------|-------------|
| John Doe | Acme Corp | john@acme.com | Building X for Y | https://acme.com | 2025-03-10 | email not sent |

## CLI reference

| Flag | Description |
|------|------------|
| `-m auto\|table\|links\|single` | Extraction mode (default: auto) |
| `-e` / `--export` | Export results to Google Sheets |
| `-s SHEET_ID` | Append to existing sheet |
| `--from-sheet SHEET_ID` | Read contacts from a sheet instead of scraping |
| `--email` | Send outreach emails (requires `--template`) |
| `--template PATH` | Path to email template file |
| `--email-daily` | Cap sends by daily limit, random delays, auto-sync |
| `--dry-run` | Preview emails without sending |
| `--save-draft` | Save to Gmail Drafts instead of sending |
| `-y` / `--yes` | Skip send confirmation prompt |
| `--limit N` | Max emails to send |
| `--sync-email-replies` | Scan Gmail for sent/replied status |
| `--enrich-org` | Resolve company websites + "What they do" via web search + LLM |
| `--enrich-sheet-in-place` | Update existing sheet columns (no new rows) |
| `--enrich-sheet-what-only` | Only update "What they do" column |
| `--enrich-only-empty` | Only enrich rows with empty descriptions |
| `--scroll` | Scroll page to load infinite-scroll content |
| `--deep` | Follow links to company websites for emails |
| `--ensure-email-status-column` | Add "Email status" column to existing sheet |
