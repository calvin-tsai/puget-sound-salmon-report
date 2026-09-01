# Puget Sound Coho Creel Report

Automated daily/weekly analysis of [WDFW Puget Sound creel reports](https://wdfw.wa.gov/fishing/reports/creel/puget)
for **Coho salmon** in Marine Areas **8-2, 9, 10, 11**.

It scrapes the WDFW creel HTML table (no public API exists for the marine areas),
aggregates catch and effort per area and per boat launch, and reports:

- **Daily digest** — per-area Coho catch-per-angler (CPUE) with day-over-day / week-over-week
  deltas, anomaly flags, and a **7-day aggregated** (smoothed) read per area and overall.
- **Weekly report** — the **top 3 recommended launches** (ranked by recent CPUE + week-over-week
  "hot bite"), per-area aggregates, and a **line chart** of Coho CPUE per area over time.
  Can be emailed as HTML with the chart embedded.

The data is raw and revised by WDFW after QA/QC, so each run re-scrapes the trailing 14 days
and overwrites stored values.

## Setup

```bash
pip install --user matplotlib   # only needed for the weekly chart; the rest is stdlib
```

## Usage

```bash
python3 creel_report.py                 # daily per-area digest -> stdout
python3 creel_report.py --weekly        # weekly launch report + chart -> stdout
python3 creel_report.py --weekly --email  # build HTML report + email it
python3 creel_report.py --no-fetch ...  # use stored data, skip scraping
python3 creel_report.py --weekly --email --until 2026-09-25  # stop emailing after a date
```

## Email

`--email` reads SMTP credentials from `~/.openclaw/creel_email.json` (never committed):

```json
{
  "smtp_host": "smtp.gmail.com",
  "smtp_port": 465,
  "smtp_user": "you@gmail.com",
  "smtp_pass": "APP_PASSWORD",
  "from": "you@gmail.com",
  "to": "recipient@example.com"
}
```

For Gmail, use an **App Password** (requires 2-Step Verification). The chart is attached
via `Content-ID` so it renders inline in Outlook and other clients.

## Data / output

Written to `memory/creel/` (relative to the workspace, gitignored):
`daily.json` (ramp-level history), `cpue_areas.png` (chart), `latest_digest.md`, `weekly_report.md`.

## Scheduling

Designed to run under a scheduler (OpenClaw cron / launchd / cron). Typical setup:
- Daily digest ~08:30 local.
- Weekly email Fridays ~09:00 local.
