"""
triple j roster spin tracker
----------------------------
Pulls play logs from ABC's music play-history feed (the same data behind the
"recently played" lists on the triple j site), matches them against roster.csv,
keeps a running history in data/plays.json, writes docs/data.json for the
dashboard, and optionally emails a weekly summary.

Usage:
  python spins.py                 # fetch the last 2 days, update history + dashboard
  python spins.py --days 90       # backfill 90 days
  python spins.py --email         # also send the weekly email
  python spins.py --dry-run-email # print the email instead of sending it
"""

import argparse
import csv
import json
import os
import re
import smtplib
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).parent
API = "https://music.abcradio.net.au/api/v1/plays/search.json"
HISTORY = ROOT / "data" / "plays.json"
DASH_DATA = ROOT / "docs" / "data.json"
PAGE_SIZE = 100

CONFIG = json.loads((ROOT / "config.json").read_text())
TZ = ZoneInfo(CONFIG.get("timezone", "Australia/Melbourne"))
LABELS = CONFIG.get("station_labels", {})


# ---------------------------------------------------------------- matching

# Bracketed bits that don't change which song it is
NOISE_BRACKETS = re.compile(
    r"[\(\[][^\)\]]*\b(feat|ft|featuring|with|radio|edit|explicit|clean|single|version|mono|stereo)\b[^\)\]]*[\)\]]",
    re.I,
)
FEAT_TAIL = re.compile(r"\s+(feat\.?|ft\.?|featuring)\s+.*$", re.I)


def norm(text: str) -> str:
    """Lowercase, strip accents/punctuation, unify '&'/'and'."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ").replace("+", " and ")
    text = re.sub(r"[’'`]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def norm_title(title: str) -> str:
    title = NOISE_BRACKETS.sub("", title or "")
    title = FEAT_TAIL.sub("", title)
    return norm(title)


def artist_matches(roster_artist: str, play_artists: list[str]) -> bool:
    target = norm(roster_artist)
    if not target:
        return False
    for name in play_artists:
        n = norm(name)
        if n == target:
            return True
        # handles credits logged as one string, e.g. "Lucky & Someone"
        if re.search(rf"(^| ){re.escape(target)}( |$)", n):
            return True
    return False


def load_roster() -> list[dict]:
    rows = []
    with open(ROOT / "roster.csv", newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            artist, track = (r.get("artist") or "").strip(), (r.get("track") or "").strip()
            group = (r.get("group") or "").strip()
            if artist and track:
                rows.append({"artist": artist, "track": track, "group": group, "key": norm_title(track)})
    return rows


# ---------------------------------------------------------------- fetching

def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fetch_window(station: str, start: datetime, end: datetime) -> list[dict]:
    items, offset = [], 0
    while True:
        params = {"station": station, "from": iso(start), "to": iso(end),
                  "limit": PAGE_SIZE, "offset": offset, "page": 0}
        for attempt in range(4):
            try:
                r = requests.get(API, params=params, timeout=30,
                                 headers={"User-Agent": "CM-spin-tracker/1.0"})
                r.raise_for_status()
                data = r.json()
                break
            except (requests.RequestException, ValueError) as e:
                if attempt == 3:
                    raise RuntimeError(f"ABC feed failed for {station} {params['from']}: {e}")
                time.sleep(2 ** attempt * 2)
        batch = data.get("items") or []
        items.extend(batch)
        offset += len(batch)
        total = data.get("total")
        if not batch or (total is not None and offset >= total) or (total is None and len(batch) < PAGE_SIZE):
            return items
        time.sleep(0.25)


WINDOW_HOURS = 3  # ~45 plays per window on triple j, well under one 100-play page


def fetch_plays(station: str, days: int) -> list[dict]:
    """Fetch in short windows so completeness never depends on deep paging."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    out, seen = [], set()
    t = start
    while t < end:
        t2 = min(t + timedelta(hours=WINDOW_HOURS), end)
        for it in fetch_window(station, t, t2):
            k = (it.get("played_time"), (it.get("recording") or {}).get("title"))
            if k not in seen:  # windows can share a boundary play
                seen.add(k)
                out.append(it)
        t = t2
    return out


def find_report(items: list[dict], station: str, text: str, roster: list[dict]) -> None:
    """Print every play whose artist or title contains `text`, matched or not."""
    target = norm(text)
    rows = []
    for it in items:
        rec = it.get("recording") or {}
        artists, title = play_artists(it), rec.get("title") or ""
        if target and target in norm(" ".join(artists) + " " + title) and it.get("played_time"):
            when = datetime.fromisoformat(it["played_time"].replace("Z", "+00:00")).astimezone(TZ)
            counted = bool(match_plays([it], station, roster))
            rows.append((when, f"{when:%a %d %b %Y %H:%M}  {', '.join(artists)} – {title}  "
                               f"[{'COUNTED' if counted else 'NOT on roster / not matched'}]"))
    print(f"\n--- {LABELS.get(station, station)}: {len(rows)} plays containing '{text}' ---")
    for _, line in sorted(rows):
        print("  " + line)


def play_artists(item: dict) -> list[str]:
    rec = item.get("recording") or {}
    names = [a.get("name", "") for a in rec.get("artists") or []]
    if not names:  # sometimes artists only live on the release
        rel = item.get("release") or ((rec.get("releases") or [None])[0]) or {}
        names = [a.get("name", "") for a in rel.get("artists") or []]
    return [n for n in names if n]


def match_plays(items: list[dict], station: str, roster: list[dict]) -> list[dict]:
    by_title: dict[str, list[dict]] = {}
    for r in roster:
        by_title.setdefault(r["key"], []).append(r)
    hits = []
    for it in items:
        rec = it.get("recording") or {}
        title = rec.get("title") or ""
        candidates = by_title.get(norm_title(title))
        if not candidates or not it.get("played_time"):
            continue
        artists = play_artists(it)
        for r in candidates:
            if artist_matches(r["artist"], artists):
                hits.append({
                    "station": it.get("service_id") or station,
                    "played_time": datetime.fromisoformat(
                        it["played_time"].replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(),
                    "artist": r["artist"],
                    "track": r["track"],
                    "logged_as": f"{', '.join(artists)} – {title}",
                })
                break
    return hits


# ---------------------------------------------------------------- storage

def play_id(p: dict) -> str:
    return f"{p['station']}|{p['played_time']}|{norm_title(p['track'])}"


def merge_history(new: list[dict]) -> list[dict]:
    old = json.loads(HISTORY.read_text()) if HISTORY.exists() else []
    seen = {play_id(p): p for p in old}
    added = 0
    for p in new:
        if play_id(p) not in seen:
            seen[play_id(p)] = p
            added += 1
    plays = sorted(seen.values(), key=lambda p: p["played_time"])
    HISTORY.parent.mkdir(exist_ok=True)
    HISTORY.write_text(json.dumps(plays, indent=1, ensure_ascii=False))
    print(f"History: {len(plays)} plays ({added} new)")
    return plays


def write_dashboard_data(plays: list[dict], roster: list[dict], fetched_from: datetime | None) -> None:
    """tracking_since = earliest moment the feed has been scanned, kept across runs,
    so the dashboard knows when a 'previous period' comparison is complete."""
    since = []
    if DASH_DATA.exists():
        try:
            old = json.loads(DASH_DATA.read_text()).get("tracking_since")
            if old:
                since.append(datetime.fromisoformat(old))
        except ValueError:
            pass
    if fetched_from:
        since.append(fetched_from)
    if plays:
        since.append(datetime.fromisoformat(plays[0]["played_time"]))
    DASH_DATA.parent.mkdir(exist_ok=True)
    DASH_DATA.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tracking_since": min(since).isoformat() if since else None,
        "timezone": CONFIG.get("timezone"),
        "stations": CONFIG["stations"],
        "station_labels": LABELS,
        "roster": [{"artist": r["artist"], "track": r["track"], "group": r["group"]} for r in roster],
        "plays": plays,
    }, ensure_ascii=False))


# ---------------------------------------------------------------- email

def weekly_summary(plays: list[dict], roster: list[dict]):
    days = CONFIG.get("report_days", 7)
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    recent = [p for p in plays if datetime.fromisoformat(p["played_time"]) >= start]
    rows = []
    for r in roster:
        mine = [p for p in recent if p["artist"] == r["artist"] and p["track"] == r["track"]]
        per = {s: sum(1 for p in mine if p["station"] == s) for s in CONFIG["stations"]}
        last = max((p["played_time"] for p in plays
                    if p["artist"] == r["artist"] and p["track"] == r["track"]), default=None)
        rows.append({**r, "per": per, "total": len(mine), "last": last})
    rows.sort(key=lambda x: (-x["total"], x["artist"].lower()))
    return start, now, rows


def fmt_local(iso_str: str | None) -> str:
    if not iso_str:
        return "—"
    return datetime.fromisoformat(iso_str).astimezone(TZ).strftime("%a %-d %b, %-I:%M%p").replace("AM", "am").replace("PM", "pm")


def build_email(plays, roster):
    start, end, rows = weekly_summary(plays, roster)
    period = f"{start.astimezone(TZ):%-d %b} – {end.astimezone(TZ):%-d %b %Y}"
    played = [r for r in rows if r["total"]]
    silent = [r for r in rows if not r["total"]]
    stations = CONFIG["stations"]
    dash = os.environ.get("DASHBOARD_URL", "")

    th = "".join(f'<th style="padding:6px 10px;text-align:right">{escape(LABELS.get(s, s))}</th>' for s in stations)
    body_rows = ""
    for r in played:
        cells = "".join(f'<td style="padding:6px 10px;text-align:right">{r["per"][s] or ""}</td>' for s in stations)
        body_rows += (f'<tr style="border-top:1px solid #ddd"><td style="padding:6px 10px">{escape(r["artist"])}</td>'
                      f'<td style="padding:6px 10px">{escape(r["track"])}</td>{cells}'
                      f'<td style="padding:6px 10px;text-align:right"><b>{r["total"]}</b></td>'
                      f'<td style="padding:6px 10px;color:#666">{fmt_local(r["last"])}</td></tr>')
    table = (f'<table style="border-collapse:collapse;font-size:14px"><tr style="color:#555">'
             f'<th style="padding:6px 10px;text-align:left">Artist</th><th style="padding:6px 10px;text-align:left">Track</th>'
             f'{th}<th style="padding:6px 10px;text-align:right">Total</th><th style="padding:6px 10px;text-align:left">Last spin</th></tr>'
             f'{body_rows}</table>') if played else "<p>No roster spins logged this week.</p>"
    silent_html = ("<p style='color:#666;font-size:13px'><b>No spins this week:</b> "
                   + escape("; ".join(f"{r['artist']} – {r['track']}" for r in silent)) + "</p>") if silent else ""
    dash_html = f'<p><a href="{escape(dash)}">Open the dashboard</a> for the full play log.</p>' if dash else ""
    note = ("<p style='color:#888;font-size:12px'>Source: ABC play-history feed. Plays aired off vinyl/CD "
            "(mostly late-night specialist shows) may not be logged.</p>")
    html = (f"<div style='font-family:Arial,sans-serif;color:#222'><h2 style='margin:0 0 4px'>Roster radio spins</h2>"
            f"<p style='margin:0 0 16px;color:#555'>{period} · {sum(r['total'] for r in rows)} spins across "
            f"{len(played)} tracks</p>{table}{silent_html}{dash_html}{note}</div>")

    text_lines = [f"Roster radio spins, {period}", ""]
    for r in played:
        split = ", ".join(f"{LABELS.get(s, s)} {r['per'][s]}" for s in stations if r["per"][s])
        text_lines.append(f"{r['artist']} – {r['track']}: {r['total']} ({split}); last {fmt_local(r['last'])}")
    if not played:
        text_lines.append("No roster spins logged this week.")
    if dash:
        text_lines += ["", f"Dashboard: {dash}"]
    subject = f"Radio spins: {len(played)} roster tracks, {sum(r['total'] for r in rows)} spins ({period})"
    return subject, html, "\n".join(text_lines)


def send_email(subject, html, text):
    to = [a.strip() for a in os.environ.get("EMAIL_TO", "").split(",") if a.strip()]
    if not to:
        sys.exit("EMAIL_TO is not set")
    sender = os.environ.get("EMAIL_FROM") or os.environ["SMTP_USER"]
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, ", ".join(to)
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.sendmail(sender, to, msg.as_string())
    print(f"Email sent to {len(to)} recipient(s)")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=2, help="how many days back to fetch")
    ap.add_argument("--email", action="store_true")
    ap.add_argument("--dry-run-email", action="store_true")
    ap.add_argument("--skip-fetch", action="store_true", help="rebuild outputs from saved history only")
    ap.add_argument("--find", default="", help="list every play whose artist/title contains this text")
    args = ap.parse_args()

    roster = load_roster()
    print(f"Roster: {len(roster)} tracks")
    new = []
    if not args.skip_fetch:
        for st in CONFIG["stations"]:
            items = fetch_plays(st, args.days)
            hits = match_plays(items, st, roster)
            print(f"{LABELS.get(st, st)}: scanned {len(items)} plays over {args.days} days "
                  f"(~{len(items) / max(args.days, 1):.0f}/day), {len(hits)} roster spins")
            if args.find:
                find_report(items, st, args.find, roster)
            new.extend(hits)
            del items
    plays = merge_history(new)
    fetched_from = None if args.skip_fetch else datetime.now(timezone.utc) - timedelta(days=args.days)
    write_dashboard_data(plays, roster, fetched_from)

    if args.email or args.dry_run_email:
        subject, html, text = build_email(plays, roster)
        if args.dry_run_email:
            print(subject, "\n", text)
        else:
            send_email(subject, html, text)


if __name__ == "__main__":
    main()
