# Newspapers.com Clippings Scraper

Downloads all your saved clippings from newspapers.com into a local PostgreSQL
database, including images, OCR text, and metadata (newspaper name, date, author, page).

## Prerequisites

- Python 3.11+
- PostgreSQL running locally (or a connection string to a remote instance)
- A newspapers.com account with saved clippings

---

## Setup

### 1. Install dependencies

```bash
git clone https://github.com/wydna-group/newspapers.git
cd newspapers
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure environment

```bash
copy .env.example .env
```

Edit `.env` and set your PostgreSQL connection string:

```
DATABASE_URL=postgresql://username:password@localhost:5432/newspapers
```

### 3. Export your newspapers.com cookies

1. Open Chrome or Firefox and log in to [newspapers.com](https://www.newspapers.com)
2. Install the **"Get cookies.txt LOCALLY"** extension:
   - Chrome: search the Chrome Web Store
   - Firefox: search Mozilla Add-ons
3. Click the extension icon while on newspapers.com
4. Click **Export** (make sure it says "Current Site" not "All Sites")
5. Save the file as `cookies.txt` in this project folder

> **Note:** Cookies expire. If you get 401 errors mid-run, repeat step 3.

---

## Usage

All commands must be run from the project root with the virtualenv active.

```bash
# Recommended: test with 5 clippings first
python -m scraper download --limit 5

# Phase 1: Discover all clippings (stores metadata in DB, no images yet)
python -m scraper discover

# Phase 2: Download images + OCR for all pending clippings
python -m scraper download

# Run both phases in sequence
python -m scraper run

# Check progress
python -m scraper status

# Retry any that failed
python -m scraper retry-failed
```

### Options

```
--cookies PATH        Path to cookies.txt (default: cookies.txt)
--no-headless         Open a visible browser window during discovery
--images-dir PATH     Where to save images (default: images/)
--limit N             Only process N clippings (download command only)
```

---

## Database Schema

```sql
SELECT id, title, newspaper_name, publication_date, author, page_number,
       image_path, ocr_text, status
FROM clippings
ORDER BY publication_date DESC;
```

Use [pgAdmin](https://www.pgadmin.org/) or the `psql` CLI to query your data.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Cookie file not found` | Export cookies.txt per Step 3 above |
| `DATABASE_URL not set` | Copy `.env.example` to `.env` and fill it in |
| `Session expired (401)` | Re-export cookies.txt |
| Discovery finds 0 clippings | Try `--no-headless` to watch the browser; check Network tab in DevTools for the API URL |
| Images downloading slowly | Normal — there is a 1.5–3.5 s polite delay between requests |

---

## How It Works

**Discovery (Phase 1)**

Playwright launches a headless Chromium browser with your cookies loaded and
navigates to `newspapers.com/clippings/`. The site is a React SPA, so instead
of parsing HTML we intercept XHR/fetch responses. The internal API returns JSON
with full metadata for each clipping. We paginate until all clippings are
collected, then upsert into PostgreSQL.

**Download (Phase 2)**

For each clipping with `status='pending'`, we use `requests` (with your cookies)
to download the image file and attempt to fetch OCR text. A polite random delay
between requests avoids triggering rate limits.

The `status` column acts as a checkpoint — you can stop and resume at any time.
