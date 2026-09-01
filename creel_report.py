#!/usr/bin/env python3
"""
WDFW Puget Sound saltwater creel report — Coho analyzer.

Scope: Marine Areas 8-2, 9, 10, 11 | species Coho (Chinook ignored).
Source: https://wdfw.wa.gov/fishing/reports/creel/puget  (raw daily HTML table, revised after QA/QC)

Deterministic, stdlib only. Each run re-scrapes the trailing DAYS_BACK days and
overwrites stored values so QA/QC revisions are captured.

Modes:
  python3 creel_report.py                 daily per-area digest -> stdout
  python3 creel_report.py --weekly        weekly launch report -> stdout
  python3 creel_report.py --weekly --email  build HTML report + email it (needs creds file)
  python3 creel_report.py --no-fetch ...  use stored data, skip scraping

Email creds file (JSON): ~/.openclaw/creel_email.json
  {"smtp_host":"smtp.gmail.com","smtp_port":465,
   "smtp_user":"you@gmail.com","smtp_pass":"APP_PASSWORD",
   "from":"you@gmail.com","to":"rovertiast@gmail.com"}
"""
import json, re, sys, os, ssl, smtplib, urllib.request, datetime
from email.message import EmailMessage
from html import unescape, escape

HERE = os.path.dirname(os.path.abspath(__file__))
STORE_DIR = os.path.normpath(os.path.join(HERE, "..", "..", "memory", "creel"))
DAILY_JSON = os.path.join(STORE_DIR, "daily.json")
DIGEST_MD = os.path.join(STORE_DIR, "latest_digest.md")
WEEKLY_MD = os.path.join(STORE_DIR, "weekly_report.md")
CREDS_FILE = os.path.expanduser("~/.openclaw/creel_email.json")

PUGET_URL = "https://wdfw.wa.gov/fishing/reports/creel/puget"
TARGET_AREAS = ["8-2", "9", "10", "11"]
AREA_LABEL = {"8-2": "Area 8-2 (Ports Susan/Gardner)", "9": "Area 9 (Admiralty Inlet)",
              "10": "Area 10 (Seattle-Bremerton)", "11": "Area 11 (Tacoma-Vashon)"}
# Okabe-Ito colorblind-safe palette
AREA_COLOR = {"8-2": "#E69F00", "9": "#56B4E9", "10": "#009E73", "11": "#D55E00"}
CHART_PNG = os.path.join(STORE_DIR, "cpue_areas.png")
DAYS_BACK = 14
MAX_PAGES = 12
ANOMALY_MULT = 1.5
MIN_WEEK_ANGLERS = 20      # sample floor for launch recommendations
TOP_N_LAUNCHES = 3
UA = {"User-Agent": "Mozilla/5.0 (creel-report)"}
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


# ---------- fetch / parse ----------
def fetch(url, tries=3):
    last = None
    for _ in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"fetch failed for {url}: {last}")


def clean(s):
    return re.sub(r"\s+", " ", unescape(re.sub("<[^>]+>", "", s))).strip()


def parse_date(caption):
    m = re.search(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})", caption)
    if not m or m.group(1) not in MONTHS:
        return None
    return f"{int(m.group(3)):04d}-{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"


def area_code(area_name):
    m = re.match(r"Area (\d+(?:-\d+)?)", area_name)
    return m.group(1) if m else None


def num(v):
    v = v.replace(",", "").replace(" ", "").strip()
    if v in ("", "-", "N/A"):
        return 0.0
    try:
        return float(v)
    except ValueError:
        return 0.0


def parse_puget(html):
    """Return {date: [ramp_row, ...]} for target areas. ramp_row = ramp/area/anglers/coho/interviews."""
    out = {}
    for tbl in re.findall(r"<table.*?</table>", html, re.S):
        cap = re.search(r"<caption[^>]*>(.*?)</caption>", tbl, re.S)
        if not cap:
            continue
        date = parse_date(clean(cap.group(1)))
        if not date:
            continue
        rows = out.setdefault(date, [])
        for tr in re.findall(r"<tr.*?</tr>", tbl, re.S):
            f = {}
            for fld, val in re.findall(
                    r'<td[^>]*class="[^"]*views-field-([a-z-]+)[^"]*"[^>]*>(.*?)</td>', tr, re.S):
                f[fld] = clean(val)
            area = f.get("catch-area-name")
            code = area_code(area) if area else None
            if code not in TARGET_AREAS:
                continue
            rows.append({
                "ramp": f.get("location-name", "").strip(),
                "area": code,
                "interviews": num(f.get("boats", "0")),
                "anglers": num(f.get("anglers", "0")),
                "coho": num(f.get("coho", "0")),
            })
    return out


def scrape_recent():
    collected = {}
    cutoff = (datetime.date.today() - datetime.timedelta(days=DAYS_BACK)).isoformat()
    for p in range(MAX_PAGES):
        page = parse_puget(fetch(f"{PUGET_URL}?page={p}"))
        if not page:
            break
        new = False
        for date, rows in page.items():
            if date not in collected:
                collected[date] = rows
                new = True
        if min(collected) <= cutoff:
            break
        if not new:
            break
    return {d: r for d, r in collected.items() if d >= cutoff}


# ---------- storage ----------
def load_store():
    if not os.path.exists(DAILY_JSON):
        return {}
    with open(DAILY_JSON) as f:
        raw = json.load(f)
    # migrate: keep only new ramp-list schema (drop old area-dict entries)
    return {d: v for d, v in raw.items() if isinstance(v, list)}


def save_store(store):
    os.makedirs(STORE_DIR, exist_ok=True)
    with open(DAILY_JSON, "w") as f:
        json.dump(store, f, indent=2, sort_keys=True)


# ---------- aggregation helpers ----------
def sampled_dates(store):
    return [d for d in sorted(store)
            if any(r["anglers"] > 0 for r in store[d])]


def area_totals(rows):
    """rows -> {code: {anglers, coho, interviews}} aggregated."""
    out = {}
    for r in rows:
        a = out.setdefault(r["area"], {"anglers": 0.0, "coho": 0.0, "interviews": 0.0})
        a["anglers"] += r["anglers"]
        a["coho"] += r["coho"]
        a["interviews"] += r["interviews"]
    return out


def cpue(coho, anglers):
    return round(coho / anglers, 3) if anglers else 0.0


def window_dates(dates, end, span):
    """sampled dates within [end-span+1, end] inclusive (ISO)."""
    lo = (datetime.date.fromisoformat(end) - datetime.timedelta(days=span - 1)).isoformat()
    return [d for d in dates if lo <= d <= end]


# ---------- daily digest (Coho) ----------
DAILY_MIN_ANGLERS = 15  # a day must have this many anglers across target areas to anchor


def daily_digest(store):
    dates = sampled_dates(store)
    if not dates:
        return "No creel samples in the stored window yet."

    def day_anglers(d):
        return sum(r["anglers"] for r in store[d])

    substantive = [d for d in dates if day_anglers(d) >= DAILY_MIN_ANGLERS]
    latest = substantive[-1] if substantive else dates[-1]
    newest_scraped = sorted(store)[-1]
    prior = [d for d in dates if d < latest]

    def area_metric(date, code):
        t = area_totals(store[date]).get(code)
        return t

    def ref_for(code, target):
        cand = [d for d in prior if area_metric(d, code) and area_metric(d, code)["anglers"] > 0]
        if not cand:
            return None
        t = datetime.date.fromisoformat(target)
        return min(cand, key=lambda d: abs((datetime.date.fromisoformat(d) - t).days))

    wow_target = (datetime.date.fromisoformat(latest) - datetime.timedelta(days=7)).isoformat()
    L = area_totals(store[latest])

    lines = [f"🎣 *Puget Sound Coho Creel — {latest}*",
             "Areas 8-2, 9, 10, 11 · Coho · _raw data, revised after QA/QC_"]
    if newest_scraped != latest:
        lines.append(f"_(Latest sampled day; {newest_scraped} not fully posted yet.)_")
    lines.append("")

    # anomalies lead
    flags = []
    for code in TARGET_AREAS:
        if code not in L:
            continue
        cur = cpue(L[code]["coho"], L[code]["anglers"])
        base = [cpue(area_metric(d, code)["coho"], area_metric(d, code)["anglers"])
                for d in prior if area_metric(d, code) and area_metric(d, code)["anglers"] > 0]
        b = sum(base) / len(base) if base else None
        if b and b > 0 and cur >= b * ANOMALY_MULT and cur >= 0.05:
            flags.append(f"  ⚠️ Area {code}: Coho CPUE {cur:.2f} vs {b:.2f} avg ({cur / b:.1f}×)")
    if flags:
        lines.append("*What changed:*")
        lines += flags + [""]

    lines.append("*By area (latest day):*")
    for code in TARGET_AREAS:
        if code not in L:
            lines.append(f"• *Area {code}* — no samples")
            continue
        cur = cpue(L[code]["coho"], L[code]["anglers"])
        pd = ref_for(code, latest)
        wd = ref_for(code, wow_target)
        pc = cpue(area_metric(pd, code)["coho"], area_metric(pd, code)["anglers"]) if pd else None
        wc = cpue(area_metric(wd, code)["coho"], area_metric(wd, code)["anglers"]) if wd else None
        dod = f"{cur - pc:+.2f}" if pc is not None else "n/a"
        wow = f"{cur - wc:+.2f}" if wc is not None else "n/a"
        lines.append(f"• *Area {code}*: {int(L[code]['anglers'])} anglers, "
                     f"{int(L[code]['coho'])} Coho · CPUE {cur:.2f} (DoD {dod}, WoW {wow})")
    lines.append("")

    # 7-day aggregated coho/angler (smoothed read)
    win = window_dates(dates, latest, 7)
    lines.append(f"*7-day avg (coho/angler, {win[0]}..{win[-1]}):*")
    tot_coho = tot_ang = 0.0
    for code in TARGET_AREAS:
        c = a = 0.0
        for d in win:
            t = area_totals(store[d]).get(code)
            if t:
                c += t["coho"]
                a += t["anglers"]
        tot_coho += c
        tot_ang += a
        if a > 0:
            lines.append(f"• *Area {code}*: {cpue(c, a):.2f}  ({int(c)} coho / {int(a)} anglers)")
        else:
            lines.append(f"• *Area {code}*: no samples")
    lines.append(f"• *All areas*: {cpue(tot_coho, tot_ang):.2f}  "
                 f"({int(tot_coho)} coho / {int(tot_ang)} anglers)")
    lines.append("")
    lines.append(f"_History: {len(dates)} sampled days stored._")
    return "\n".join(lines)


# ---------- weekly launch report (Coho) ----------
def weekly_data(store):
    """Return (launches, area_agg, meta) over recent 7d vs prior 7d."""
    dates = sampled_dates(store)
    if not dates:
        return None
    latest = dates[-1]
    recent = window_dates(dates, latest, 7)
    prior_end = (datetime.date.fromisoformat(latest) - datetime.timedelta(days=7)).isoformat()
    prior = window_dates(dates, prior_end, 7)

    def agg(rows_days, key):
        acc = {}
        for d in rows_days:
            for r in store[d]:
                k = key(r)
                a = acc.setdefault(k, {"anglers": 0.0, "coho": 0.0, "days": set(), "area": r["area"]})
                a["anglers"] += r["anglers"]
                a["coho"] += r["coho"]
                if r["anglers"] > 0:
                    a["days"].add(d)
        return acc

    launch_key = lambda r: (r["ramp"], r["area"])
    rec_l = agg(recent, launch_key)
    pri_l = agg(prior, launch_key)

    launches = []
    for k, a in rec_l.items():
        if a["anglers"] < MIN_WEEK_ANGLERS:
            continue
        rc = cpue(a["coho"], a["anglers"])
        pa = pri_l.get(k)
        pc = cpue(pa["coho"], pa["anglers"]) if pa and pa["anglers"] > 0 else None
        wow = (rc - pc) if pc is not None else None
        score = rc + (max(wow, 0) if wow is not None else 0)
        launches.append({"ramp": k[0], "area": k[1], "cpue": rc, "wow": wow,
                         "coho": int(a["coho"]), "anglers": int(a["anglers"]),
                         "days": len(a["days"]), "score": score})
    launches.sort(key=lambda x: x["score"], reverse=True)

    # per-area aggregates (recent week)
    rec_a = agg(recent, lambda r: r["area"])
    pri_a = agg(prior, lambda r: r["area"])
    area_agg = {}
    for code in TARGET_AREAS:
        a = rec_a.get(code)
        if not a:
            area_agg[code] = None
            continue
        rc = cpue(a["coho"], a["anglers"])
        pa = pri_a.get(code)
        pc = cpue(pa["coho"], pa["anglers"]) if pa and pa["anglers"] > 0 else None
        area_agg[code] = {"anglers": int(a["anglers"]), "coho": int(a["coho"]),
                          "cpue": rc, "wow": (rc - pc) if pc is not None else None}

    # overall 7-day aggregate across all target areas (averaged read)
    rc_all = sum(rec_a[c]["coho"] for c in rec_a)
    ra_all = sum(rec_a[c]["anglers"] for c in rec_a)
    pc_all_c = sum(pri_a[c]["coho"] for c in pri_a)
    pc_all_a = sum(pri_a[c]["anglers"] for c in pri_a)
    overall = {"anglers": int(ra_all), "coho": int(rc_all), "cpue": cpue(rc_all, ra_all),
               "wow": (cpue(rc_all, ra_all) - cpue(pc_all_c, pc_all_a)) if pc_all_a > 0 else None}

    meta = {"latest": latest, "recent": recent, "prior": prior, "overall": overall}
    return launches, area_agg, meta


def weekly_text(store):
    data = weekly_data(store)
    if not data:
        return "No creel samples in the stored window yet."
    launches, area_agg, meta = data
    L = lines = []
    lines.append(f"Puget Sound Coho — Friday Launch Report ({meta['latest']})")
    lines.append(f"Recent week: {meta['recent'][0]}..{meta['recent'][-1]} | "
                 f"Areas 8-2/9/10/11 | Coho (Chinook ignored)")
    lines.append("")
    lines.append(f"TOP {TOP_N_LAUNCHES} LAUNCHES (hot bite + catch/angler):")
    if not launches:
        lines.append(f"  (No launch met the {MIN_WEEK_ANGLERS}-angler/week sample floor.)")
    for i, x in enumerate(launches[:TOP_N_LAUNCHES], 1):
        wow = f"{x['wow']:+.2f} WoW" if x["wow"] is not None else "WoW n/a"
        lines.append(f"  {i}. {x['ramp']} — {AREA_LABEL[x['area']]}")
        dtxt = f"{x['days']} day" + ("s" if x["days"] != 1 else "")
        lines.append(f"       Coho CPUE {x['cpue']:.2f} ({wow}) · "
                     f"{x['coho']} coho / {x['anglers']} anglers over {dtxt}")
    lines.append("")
    lines.append("BY AREA (recent week, aggregated):")
    for code in TARGET_AREAS:
        a = area_agg[code]
        if not a:
            lines.append(f"  {AREA_LABEL[code]}: no samples")
            continue
        wow = f"{a['wow']:+.2f} WoW" if a["wow"] is not None else "WoW n/a"
        lines.append(f"  {AREA_LABEL[code]}: {a['anglers']} anglers, {a['coho']} coho · "
                     f"CPUE {a['cpue']:.2f} ({wow})")
    o = meta["overall"]
    owow = f"{o['wow']:+.2f} WoW" if o["wow"] is not None else "WoW n/a"
    lines.append(f"  ALL AREAS (7-day avg): {o['anglers']} anglers, {o['coho']} coho · "
                 f"CPUE {o['cpue']:.2f} ({owow})")
    return "\n".join(lines)


def make_cpue_chart(store, path=CHART_PNG):
    """Line graph of Coho CPUE per area over the stored sampled dates. Returns path or None."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        import datetime as dt
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"chart skipped (matplotlib unavailable): {e}\n")
        return None

    dates = sampled_dates(store)
    if len(dates) < 2:
        return None
    xs = [dt.date.fromisoformat(d) for d in dates]

    fig, ax = plt.subplots(figsize=(8, 4.2))
    for code in TARGET_AREAS:
        ys = []
        for d in dates:
            t = area_totals(store[d]).get(code)
            ys.append(t["coho"] / t["anglers"] if t and t["anglers"] > 0 else float("nan"))
        ax.plot(xs, ys, marker="o", markersize=4, linewidth=2,
                color=AREA_COLOR[code], label=AREA_LABEL[code])

    ax.set_title("Puget Sound Coho — catch per angler by area")
    ax.set_ylabel("Coho per angler (CPUE)")
    ax.set_ylim(bottom=0)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax.legend(fontsize=8, loc="upper left")
    fig.autofmt_xdate(rotation=45)
    fig.tight_layout()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def weekly_html(store):
    data = weekly_data(store)
    if not data:
        return "<p>No creel samples in the stored window yet.</p>", "Puget Sound Coho — no data"
    launches, area_agg, meta = data
    subject = f"Puget Sound Coho — Friday Launch Report ({meta['latest']})"
    h = [f"<h2>🎣 {escape(subject)}</h2>",
         f"<p style='color:#555'>Recent week {meta['recent'][0]}–{meta['recent'][-1]} · "
         f"Areas 8-2/9/10/11 · Coho (Chinook ignored) · raw data, revised after QA/QC</p>"]
    h.append(f"<h3>Top {TOP_N_LAUNCHES} launches — recommended</h3>")
    if launches[:TOP_N_LAUNCHES]:
        h.append("<ol>")
        for x in launches[:TOP_N_LAUNCHES]:
            wow = f"{x['wow']:+.2f} WoW" if x["wow"] is not None else "WoW n/a"
            dtxt = f"{x['days']} day" + ("s" if x["days"] != 1 else "")
            h.append(f"<li><b>{escape(x['ramp'])}</b> — {escape(AREA_LABEL[x['area']])}<br>"
                     f"<span style='color:#0a6'>Coho CPUE {x['cpue']:.2f}</span> ({wow}) · "
                     f"{x['coho']} coho / {x['anglers']} anglers over {dtxt}</li>")
        h.append("</ol>")
    else:
        h.append(f"<p><i>No launch met the {MIN_WEEK_ANGLERS}-angler/week sample floor.</i></p>")
    h.append("<h3>By area (recent week, aggregated)</h3>")
    h.append("<table cellpadding='6' style='border-collapse:collapse' border='1'>")
    h.append("<tr><th>Area</th><th>Anglers</th><th>Coho</th><th>CPUE</th><th>WoW</th></tr>")
    for code in TARGET_AREAS:
        a = area_agg[code]
        if not a:
            h.append(f"<tr><td>{escape(AREA_LABEL[code])}</td><td colspan='4'>no samples</td></tr>")
            continue
        wow = f"{a['wow']:+.2f}" if a["wow"] is not None else "n/a"
        h.append(f"<tr><td>{escape(AREA_LABEL[code])}</td><td align='right'>{a['anglers']}</td>"
                 f"<td align='right'>{a['coho']}</td><td align='right'>{a['cpue']:.2f}</td>"
                 f"<td align='right'>{wow}</td></tr>")
    o = meta["overall"]
    owow = f"{o['wow']:+.2f}" if o["wow"] is not None else "n/a"
    h.append(f"<tr style='font-weight:bold;background:#f2f2f2'><td>All areas (7-day avg)</td>"
             f"<td align='right'>{o['anglers']}</td><td align='right'>{o['coho']}</td>"
             f"<td align='right'>{o['cpue']:.2f}</td><td align='right'>{owow}</td></tr>")
    h.append("</table>")
    h.append("<h3>Coho catch/angler by area (trend)</h3>")
    h.append("<img src='cid:cpuechart' alt='Coho CPUE by area' "
             "style='max-width:100%;height:auto;border:1px solid #ddd'>")
    h.append("<p style='color:#888;font-size:12px'>Source: WDFW Puget Sound creel reports "
             "(HTML scrape). Auto-generated.</p>")
    return "\n".join(h), subject


# ---------- email ----------
def send_email(html, subject, image_path=None):
    if not os.path.exists(CREDS_FILE):
        raise RuntimeError(f"email creds file missing: {CREDS_FILE}")
    with open(CREDS_FILE) as f:
        c = json.load(f)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = c.get("from", c["smtp_user"])
    msg["To"] = c["to"]
    msg.set_content("HTML email — enable HTML to view the Coho launch report.")
    msg.add_alternative(html, subtype="html")
    if image_path and os.path.exists(image_path):
        with open(image_path, "rb") as f:
            img = f.read()
        html_part = msg.get_payload()[1]  # the text/html alternative
        html_part.add_related(img, maintype="image", subtype="png", cid="<cpuechart>")
    port = int(c.get("smtp_port", 465))
    host = c.get("smtp_host", "smtp.gmail.com")
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(c["smtp_user"], c["smtp_pass"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(c["smtp_user"], c["smtp_pass"])
            s.send_message(msg)
    return c["to"]


# ---------- main ----------
def main():
    args = sys.argv[1:]
    no_fetch = "--no-fetch" in args
    weekly = "--weekly" in args
    do_email = "--email" in args
    until = None
    if "--until" in args:
        i = args.index("--until")
        until = args[i + 1] if i + 1 < len(args) else None

    if until and datetime.date.today().isoformat() > until:
        print(f"Creel weekly window ended (after {until}); nothing sent.")
        return

    os.makedirs(STORE_DIR, exist_ok=True)
    store = load_store()
    if not no_fetch:
        store.update(scrape_recent())
        save_store(store)

    if weekly:
        text = weekly_text(store)
        with open(WEEKLY_MD, "w") as f:
            f.write(text + "\n")
        chart = make_cpue_chart(store)
        if do_email:
            html, subject = weekly_html(store)
            to = send_email(html, subject, image_path=chart)
            print(f"Weekly Coho report emailed to {to}"
                  + (" (with CPUE chart)." if chart else " (chart unavailable)."))
        else:
            print(text)
            if chart:
                print(f"\nChart: {chart}")
    else:
        digest = daily_digest(store)
        with open(DIGEST_MD, "w") as f:
            f.write(digest + "\n")
        print(digest)


if __name__ == "__main__":
    main()
