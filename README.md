# Puget Sound Salmon Creel Report

Automated analysis of [WDFW Puget Sound creel reports](https://wdfw.wa.gov/fishing/reports/creel/puget).
Pick **any of the 5 Pacific salmon**, **any marine areas**, a **report frequency**, and an
**end date** — it scrapes the WDFW creel HTML table (no public API exists for the marine
areas), aggregates catch and effort per area and per boat launch, and reports:

- **Daily digest** — per-area catch-per-angler (CPUE) with day-over-day / week-over-week
  deltas, anomaly flags, and a **7-day aggregated** (smoothed) read per area and overall.
- **Weekly report** — the **top recommended launches** (ranked by recent CPUE + week-over-week
  "hot bite"), per-area aggregates, and a **line chart** of CPUE per area over time.
  Can be emailed as HTML with the chart embedded (renders in Outlook via a CID attachment).

When multiple species are selected, their catches are combined into one CPUE metric.
Data is raw and revised by WDFW after QA/QC, so each run re-scrapes the trailing
`trailing_days` days and overwrites stored values. All 5 species are stored regardless of
selection, so changing species/areas needs no re-scrape.

## Configure

Everything is driven by `config.json` (or `--config PATH`), and any field can be overridden
on the command line. Priority: built-in defaults → config file → CLI flags.

```json
{
  "areas": ["8-2", "9", "10", "11"],   // marine area codes as shown on WDFW (e.g. 8-2, 9, 10, 11)
  "species": ["coho"],                  // any of: chinook, coho, chum, pink, sockeye
  "trailing_days": 14,                  // days re-scraped each run (captures QA/QC revisions)
  "top_launches": 3,                    // launches listed in the weekly report
  "min_week_anglers": 20,               // sample floor for a launch to be recommended
  "anomaly_mult": 1.5,                  // daily "what changed" flag threshold (x baseline)
  "daily_min_anglers": 15,              // min anglers for a day to anchor the daily digest
  "until": null                         // "YYYY-MM-DD": --email stops on/after this date
}
```

## Setup

```bash
pip install --user matplotlib   # only needed for the weekly chart; the rest is stdlib
```

## Usage

```bash
python3 creel_report.py                          # daily per-area digest -> stdout
python3 creel_report.py --weekly                 # weekly launch report + chart -> stdout
python3 creel_report.py --weekly --email         # build HTML report + email it

# select on the fly (overrides config.json):
python3 creel_report.py --species chinook,coho --areas 9,10 --weekly
python3 creel_report.py --species pink --areas 8-2,8-1,9 --weekly --email --until 2026-10-31
python3 creel_report.py --config myconfig.json --weekly

python3 creel_report.py --no-fetch ...           # use stored data, skip scraping
```

Flags: `--species a,b`, `--areas a,b`, `--until YYYY-MM-DD`, `--config PATH`,
`--weekly` (else daily), `--email`, `--no-fetch`.

## Report frequency

The script produces one report per invocation; **frequency = how often you schedule it**.
Point a scheduler (cron / launchd / OpenClaw cron) at the mode you want:

```cron
30 8 * * *   cd /path/to/repo && python3 creel_report.py            # daily digest
0  9 * * 5   cd /path/to/repo && python3 creel_report.py --weekly --email --until 2026-09-25
```

The `--until` date self-limits emailing so a recurring job stops on schedule.

## Email

`--email` reads SMTP credentials from `~/.openclaw/creel_email.json` (never committed):

```json
{
  "smtp_host": "smtp.gmail.com",
  "smtp_port": 465,
  "smtp_user": "you@gmail.com",
  "smtp_pass": "APP_PASSWORD",
  "from": "you@gmail.com",
  "to": ["you@example.com"],
  "bcc": ["friend1@example.com", "friend2@example.com"]
}
```

For Gmail, use an **App Password** (requires 2-Step Verification).

**Multiple recipients (email list):** `to`, `cc`, and `bcc` each accept a single address,
a comma-separated string, or a list. Put the list on **`bcc`** so recipients don't see each
other's addresses (keep yourself on `to`). One send goes to everyone. Gmail allows up to
~100 recipients per message and ~500 recipients/day on a normal account.

**Self-serve signups (Google Form → Sheet):** add `"subscribers_url"` to the creds file
with a published-as-CSV link to a Google Sheet, and each send pulls the live list and merges
it into `bcc` (de-duped, case-insensitive; fails soft if the URL is unreachable).

1. Create a Google Form with an **Email** field → responses go to a linked Sheet.
2. In the Sheet: **File → Share → Publish to web → CSV**, copy the link (looks like
   `https://docs.google.com/spreadsheets/d/e/…/pub?output=csv`).
3. Put it in the creds file:

   ```json
   "subscribers_url": "https://docs.google.com/spreadsheets/d/e/…/pub?output=csv"
   ```

Every email-looking cell in the CSV is treated as a subscriber, so column order doesn't
matter. Anyone with the form link can add themselves; consider an unsubscribe note in the
footer since a public form has no built-in opt-out.

## Data / output

Written to `memory/creel/` relative to the workspace (gitignored): `daily.json`
(ramp-level history, all species), `cpue_areas.png` (chart), `latest_digest.md`,
`weekly_report.md`.
