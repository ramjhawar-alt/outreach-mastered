# Outreach Mastered

A small **command-line tool** for anyone doing outreach: pull contacts from the web (or a Google Sheet), keep everything in **Sheets** as your working list, and send **personalized emails** from a template—with sensible limits so you do not blast inboxes by accident.

Works well on **free tiers** where possible (Groq, OpenRouter, You.com-style search, etc.).

**Built with:** Python · Playwright · BeautifulSoup · Google Sheets (`gspread`) · Gmail SMTP/IMAP · Groq / OpenRouter (optional Apollo from Python only).

---

## What it does

1. **Scrape** — Tables, link lists, or single pages; optional scroll for long pages and optional “deep” fetch for emails on company sites.  
2. **Enrich** — Web search + LLM to fill in company URLs and a short “What they do” line; can update an existing sheet in place.  
3. **Export to Sheets** — One row per contact, plus an **Email status** column you can filter on.  
4. **Send mail** — Plain text or HTML templates, PDF attachments, daily caps, random gaps between sends, and optional sync so **replies** can be reflected on the sheet.

---

## Setup

```bash
git clone https://github.com/ramjhawar-alt/outreach-mastered.git
cd outreach-mastered
pip install -r requirements.txt
python3 -m playwright install chromium
cp .env.example .env
```

Edit `.env` with your keys (never commit `.env` or `credentials.json`).

| Key | What it’s for |
|-----|----------------|
| `GOOGLE_CREDENTIALS_PATH` | Service account JSON for Sheets |
| `GMAIL_FROM` + `GMAIL_APP_PASSWORD` | Send email (and IMAP for drafts / reply sync) |
| `YDC_API_KEY` | Web search for enrichment |
| `GROQ_API_KEY` / `OPENROUTER_API_KEY` | “What they do” text (with fallback) |
| `APOLLO_API_KEY` | Optional; only if you call Apollo from Python yourself |
| `OUTREACH_SHEET_ID`, `OUTREACH_WORKSHEET` | Defaults so you can skip `--from-sheet` / `--worksheet` when you want |
| `OUTREACH_RESUME_PDF` | PDF attached on generic templates |
| `DEMO_DAY_ATTACHMENT_PDFS` | PDFs for `templates/demo_day.html` |

**Google Sheets:** enable Sheets + Drive APIs, create a service account, download the key, create a spreadsheet in your Google account, and **share that sheet with the service account email (Editor)**. Copy the sheet ID from the URL.

---

## Usage

### Scrape and push to a sheet

```bash
python3 main.py -e -s YOUR_SHEET_ID "https://example.com/directory"
python3 main.py -m table -e -s YOUR_SHEET_ID "https://example.com/table-page"
python3 main.py -m links --scroll "https://example.com/long-list"
```

### Enrich rows you already have

```bash
python3 main.py --from-sheet YOUR_SHEET_ID --worksheet YourTab \
  --enrich-sheet-in-place --enrich-org --enrich-sheet-what-only --enrich-only-empty
```

### Email from a template

First line of the template file: `Subject: …`. Body supports `{contact_name}`, `{organization}`, `{email}`, `{what_they_do}`.

**Preview (nothing sent):**

```bash
python3 main.py --from-sheet YOUR_SHEET_ID --worksheet YourTab \
  --email --template templates/outreach_example.txt --dry-run
```

**Send with a daily cap** (default 50/day, random delay between messages; updates the sheet + local state file):

```bash
python3 -u main.py --email --email-daily --template templates/outreach_example.txt --yes \
  --from-sheet YOUR_SHEET_ID --worksheet YourTab
```

**Sync Gmail → sheet** (mark sent / replied):

```bash
python3 -u main.py --sync-email-replies --from-sheet YOUR_SHEET_ID --worksheet YourTab
```

### Apollo (optional, Python only)

```python
from src.apollo import find_founder_email

name, email, title = find_founder_email("ExampleCo", domain="example.com")
```

The normal CLI **does not** call Apollo or spend credits by itself.

---

## Sheet columns

Headers are matched by name (order does not matter). Typical layout:

| Contact | Organization | Email | What they do | Source URL | Extracted At | Email status |
|---------|--------------|-------|----------------|------------|--------------|----------------|

Statuses include `email not sent`, `email sent`, and `replied` (see `src/sheets.py`).

---

## CLI cheat sheet

| Flag | Meaning |
|------|---------|
| `-m auto\|table\|links\|single` | How to parse the page |
| `-e` / `--export` | Write rows to Google Sheets |
| `-s SHEET_ID` | Append to this spreadsheet |
| `--from-sheet` / `--worksheet` | Read contacts from a tab |
| `--email` + `--template` | Send mail |
| `--email-daily` | Daily limit + delays + state |
| `--dry-run` / `--save-draft` / `-y` | Preview / drafts / no confirm |
| `--limit N` | Send at most N (testing) |
| `--sync-email-replies` | Pull status from Gmail |
| `--enrich-org`, `--enrich-sheet-in-place`, … | Fill in org / pitch text |

`python3 main.py -h` lists everything.

---

## Repo layout

```
main.py
src/browser.py, extractor.py, sheets.py, emailer.py, config.py
src/outreach_state.py, reply_sync.py
templates/          # example bodies
attachments/        # put PDFs here (see attachments/README.md)
```

---

## Notes

- Do not commit secrets; use app passwords and share sheets only with your service account.  
- Respect rate limits and only mail people you have a legitimate reason to contact.

Repository: [github.com/ramjhawar-alt/outreach-mastered](https://github.com/ramjhawar-alt/outreach-mastered)
