# AU — Hard Paywall Performance Dashboard

Auto-pulls daily hard-paywall funnel metrics (FI → FC → PV → PC → VIL → BC → BUYS) from Pinot to a Google Sheet, served as a static dashboard on GitHub Pages.

## Architecture

```
Pinot (events + stories tables, auth)
    │
    ▼  daily 02:00 IST via GitHub Actions
Python puller  →  Google Sheet (3 tabs: feed_metrics, paywall_metrics, stories_dim)
    │                 │
    │                 ▼  "Publish to web" → CSV URLs
    ▼
GitHub Pages (docs/index.html) → fetches 3 CSVs → joins + filters client-side
```

## Deploy — one-time setup (~15 min)

### 1. Create a Google Sheet
- New blank Sheet. Name it anything (e.g. "AU Hard Paywall Data").
- Copy its **Sheet ID** from the URL: `.../spreadsheets/d/<SHEET_ID>/edit`
- Share it with the service account email (see step 2) as **Editor**.

### 2. Create a Google Cloud service account
- https://console.cloud.google.com → new project → APIs & Services → enable **Google Sheets API**.
- Credentials → Create credentials → Service account → give it any name → done.
- Open the service account → Keys → Add key → JSON → download.
- Share the Sheet (step 1) with the `client_email` from the downloaded JSON.
- Keep the whole JSON file handy — you'll paste it as one GitHub secret.

### 3. Push this repo to GitHub
```bash
cd au-hard-paywall
git init
git add .
git commit -m "init"
gh repo create au-hard-paywall --private --source=. --push
# or push to an existing repo of your choice
```

### 4. Add GitHub secrets
Repo → Settings → Secrets and variables → Actions → New repository secret. Add **all five**:

| Secret name         | Value                                                               |
|---------------------|---------------------------------------------------------------------|
| `PINOT_HOST`        | `http://46.62.191.123:8099` (broker HTTP endpoint, NOT 9000 UI port) |
| `PINOT_USER`        | Pinot username                                                       |
| `PINOT_PASSWORD`    | Pinot password                                                       |
| `SHEET_ID`          | Sheet ID from step 1                                                 |
| `GOOGLE_CREDS_JSON` | Entire contents of the service account JSON file (paste as-is)       |

### 5. First run — backfill from 1 Nov
Actions tab → `Daily Pinot Pull` → **Run workflow** → fill:
- `date`: `2026-11-01`
- `end_date`: `2026-11-23` (today-1)

Wait ~3-5 min. Check Sheet tabs — should see `feed_metrics`, `paywall_metrics`, `stories_dim` populated.

### 6. Publish the Sheet tabs as CSV (3 URLs)
In the Sheet: **File → Share → Publish to web**.
For each of the 3 tabs:
- "Link" → pick the tab → **Comma-separated values (.csv)** → Publish → copy URL.
- URL format: `https://docs.google.com/spreadsheets/d/e/<pub_id>/pub?gid=<gid>&single=true&output=csv`

Open `docs/index.html` and replace the three `PASTE_…_CSV_URL_HERE` placeholders near the top with your 3 URLs. Commit + push.

### 7. Enable GitHub Pages
Repo → Settings → Pages → Source: **Deploy from a branch** → Branch: `main` / `/docs` → Save.
Your dashboard URL: `https://<your-user>.github.io/au-hard-paywall/`

Done. Daily cron will refresh at 02:00 IST. Dashboard re-fetches live from Sheet on every page load.

## File map

```
.
├── .github/workflows/pull.yml        GitHub Actions schedule + manual run
├── puller/
│   ├── hard_paywall_puller.py        the puller (Pinot → Sheet)
│   └── requirements.txt
├── docs/
│   └── index.html                    the dashboard (GitHub Pages serves this)
└── README.md
```

## Manual backfill or single-date rerun

Actions tab → `Daily Pinot Pull` → Run workflow → fill `date` only (and optional `end_date` for a range). Idempotent — re-running a date **replaces** that date's rows, never duplicates.

## What the dashboard shows

- **KPI strip**: totals across current filter (unique users at each funnel stage, CTRs).
- **Funnel chart**: unique users at each stage.
- **Table**: one row per `(date, hour, position, feed_version_id, story_id)`. Paywall funnel (PV→BUYS) is shown per `(date, hour, story_id)` — same values repeat across positions since paywall events don't carry position.
- **Filters (all multi-select with search)**: date range, hour range, position (5/13/21/27/28), feed version (2691 = 85%, 4810/2694/4809 = ~5% each), story (by title/id/author/URL), author, metric column visibility.
- **Global search**: fuzzy-matches across story_id, title_hn, title_en, author, URL, category.
- **Download CSV**: exports current filtered selection.

## Dashboard safety

- Pinot creds never leave GitHub Actions secrets.
- Google Sheet stays **private** — "Publish to web" generates obscure URLs with no PII (only aggregated counts + public story metadata).
- The static site has **no server, no DB, no API**. All joining + filtering happens in the viewer's browser.

## Notes & gotchas

- Current-day data is **never pulled** — the cron fetches yesterday-IST only, so every day's data is complete.
- `event_hour_ist` is assumed to be `"00"`..`"23"` (STRING). If the actual format differs, first pull will reveal it; adjust `pad2()` in `index.html` and the GROUP BY key in the puller as needed.
- Stories table is not time-versioned — only the latest title is stored. For headline-change CTR analysis, maintain the change-log yourself and overlay on the dashboard output.
- Paywall funnel attribution uses `JSON_EXTRACT_SCALAR(metadata,'$.acqSubSource',...) LIKE '%<story_id>%'`. Story IDs are 24-char ObjectIds so substring collision risk is effectively zero.
- GitHub Actions free tier gives 2,000 min/month — this puller takes ~3 min/day = ~90 min/month. Plenty of room.

## Troubleshooting

- **Puller fails with 401**: Pinot creds wrong, or broker port is different. Try `9000` first in UI to confirm, then find the broker's HTTP port (often `8099`).
- **Puller fails with "No module named ..."**: `requirements.txt` not installed — check the Actions log.
- **Dashboard says "CSV URL not configured"**: step 6 skipped. Replace the 3 placeholders in `docs/index.html`.
- **Dashboard loads but table is empty**: check that the Sheet tabs have data and that "Publish to web" is actually turned on for each tab (not just shared).
- **GitHub Actions IP blocked by Pinot**: Pinot admin may have IP-whitelisted. Either whitelist GitHub Actions ranges (large), or move the puller to a self-hosted runner / small VPS and call it on the same schedule.
