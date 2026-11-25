#!/usr/bin/env python3
"""Spotify listening stats for a single year.

Reads the files from Spotify's "Download your data" export (either the
Extended streaming history or the basic Account data), filters to one year,
computes stats, and writes a self-contained HTML dashboard.

Usage:
    python3 spotify_stats.py                      # reads ./data, year 2026
    python3 spotify_stats.py --data my_spotify_data.zip --year 2026
    python3 spotify_stats.py --tz America/New_York --out report.html

Standard library only. No data leaves your computer.
"""
from __future__ import annotations

import argparse
import json
import math
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "dashboard_template.html"

STREAM_MS = 30_000          # Spotify counts a "stream" at 30 seconds
SESSION_GAP = timedelta(minutes=20)
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def _iter_json_blobs(src: Path):
    """Yield (name, parsed_json) for every streaming-history JSON in src."""
    def wanted(name: str) -> bool:
        n = Path(name).name.lower()
        return n.endswith(".json") and "streaming" in n and "history" in n

    if src.is_file() and src.suffix == ".zip":
        with zipfile.ZipFile(src) as z:
            for name in z.namelist():
                if wanted(name):
                    yield name, json.loads(z.read(name))
        return
    paths = [src] if src.is_file() else sorted(src.rglob("*"))
    for p in paths:
        if p.is_file() and p.suffix == ".zip":
            yield from _iter_json_blobs(p)
        elif p.is_file() and wanted(p.name):
            yield p.name, json.loads(p.read_text(encoding="utf-8"))


def _normalize(row: dict) -> dict | None:
    """Map both export formats onto one schema. Returns None for junk rows."""
    if "ts" in row:  # Extended streaming history
        ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
        track = row.get("master_metadata_track_name")
        episode = row.get("episode_name")
        if not track and not episode:
            return None
        return {
            "end": ts,
            "ms": int(row.get("ms_played") or 0),
            "kind": "track" if track else "episode",
            "track": track or episode,
            "artist": row.get("master_metadata_album_artist_name")
                      or row.get("episode_show_name") or "Unknown",
            "album": row.get("master_metadata_album_album_name"),
            "uri": row.get("spotify_track_uri") or row.get("spotify_episode_uri"),
            "platform": _platform(row.get("platform")),
            "country": row.get("conn_country"),
            "reason_start": row.get("reason_start"),
            "reason_end": row.get("reason_end"),
            "shuffle": row.get("shuffle"),
            "skipped": row.get("skipped"),
            "offline": row.get("offline"),
            "incognito": row.get("incognito_mode"),
        }
    if "endTime" in row:  # Basic account-data export (UTC, minute precision)
        ts = datetime.strptime(row["endTime"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        is_pod = "podcastName" in row or "episodeName" in row
        return {
            "end": ts,
            "ms": int(row.get("msPlayed") or 0),
            "kind": "episode" if is_pod else "track",
            "track": row.get("trackName") or row.get("episodeName") or "Unknown",
            "artist": row.get("artistName") or row.get("podcastName") or "Unknown",
            "album": None, "uri": None, "platform": None, "country": None,
            "reason_start": None, "reason_end": None, "shuffle": None,
            "skipped": None, "offline": None, "incognito": None,
        }
    return None


def _platform(raw: str | None) -> str | None:
    if not raw:
        return None
    r = raw.lower()
    for key, label in [("ios", "iPhone / iPad"), ("android", "Android"),
                       ("osx", "Mac"), ("os x", "Mac"), ("mac", "Mac"),
                       ("windows", "Windows"), ("web", "Web player"),
                       ("cast", "Cast / speaker"), ("sonos", "Cast / speaker"),
                       ("tv", "TV"), ("ps4", "Console"), ("ps5", "Console"),
                       ("xbox", "Console"), ("car", "Car"), ("linux", "Linux")]:
        if key in r:
            return label
    return "Other"


def load(src: Path, year: int, tz: ZoneInfo) -> tuple[list[dict], list[str]]:
    seen, rows, files = set(), [], []
    for name, blob in _iter_json_blobs(src):
        files.append(name)
        for raw in blob if isinstance(blob, list) else []:
            r = _normalize(raw)
            if not r:
                continue
            key = (r["end"], r["track"], r["ms"])
            if key in seen:            # exports sometimes overlap
                continue
            seen.add(key)
            r["end"] = r["end"].astimezone(tz)
            r["start"] = r["end"] - timedelta(milliseconds=r["ms"])
            if r["end"].year == year:
                rows.append(r)
    rows.sort(key=lambda r: r["end"])
    return rows, files


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------
def _top(counter: Counter, n=10, fmt=None):
    return [{"name": k, "value": round(v, 1) if isinstance(v, float) else v,
             **(fmt(k) if fmt else {})} for k, v in counter.most_common(n)]


def _streak(days: list) -> tuple[int, str | None, str | None]:
    best, cur, best_end = 0, 0, None
    prev = None
    for d in days:
        cur = cur + 1 if prev and d - prev == timedelta(days=1) else 1
        if cur > best:
            best, best_end = cur, d
        prev = d
    if not best_end:
        return 0, None, None
    return best, str(best_end - timedelta(days=best - 1)), str(best_end)


def _gini(values) -> float:
    v = sorted(values)
    n = len(v)
    if n == 0 or sum(v) == 0:
        return 0.0
    cum = sum((i + 1) * x for i, x in enumerate(v))
    return (2 * cum) / (n * sum(v)) - (n + 1) / n


def compute(rows: list[dict], year: int) -> dict:
    music = [r for r in rows if r["kind"] == "track"]
    pods = [r for r in rows if r["kind"] == "episode"]
    played = [r for r in music if r["ms"] > 0]
    streams = [r for r in music if r["ms"] >= STREAM_MS]
    total_min = sum(r["ms"] for r in rows) / 60000
    music_min = sum(r["ms"] for r in music) / 60000

    def tkey(r):
        return f'{r["track"]} — {r["artist"]}'

    by_track_min, by_artist_min, by_album_min = Counter(), Counter(), Counter()
    by_track_plays, by_artist_plays = Counter(), Counter()
    for r in played:
        m = r["ms"] / 60000
        by_track_min[tkey(r)] += m
        by_artist_min[r["artist"]] += m
        if r["album"]:
            by_album_min[f'{r["album"]} — {r["artist"]}'] += m
    for r in streams:
        by_track_plays[tkey(r)] += 1
        by_artist_plays[r["artist"]] += 1

    # Time patterns -------------------------------------------------------
    monthly = [0.0] * 12
    hourly = [0.0] * 24
    heat = [[0.0] * 24 for _ in range(7)]
    daily = defaultdict(float)
    for r in rows:
        m = r["ms"] / 60000
        e = r["end"]
        monthly[e.month - 1] += m
        hourly[e.hour] += m
        heat[e.weekday()][e.hour] += m
        daily[e.date()] += m
    weekday_tot = [sum(h) for h in heat]
    active_days = sorted(d for d, v in daily.items() if v > 0)
    streak_len, streak_from, streak_to = _streak(active_days)
    top_day = max(daily.items(), key=lambda kv: kv[1]) if daily else (None, 0)

    # Calendar (every day of the year up to last data point)
    calendar = []
    if rows:
        d = datetime(year, 1, 1).date()
        last = rows[-1]["end"].date()
        while d <= last:
            calendar.append([str(d), round(daily.get(d, 0.0), 1)])
            d += timedelta(days=1)

    # Sessions ------------------------------------------------------------
    sessions, cur = [], []
    for r in rows:
        if cur and r["start"] - cur[-1]["end"] > SESSION_GAP:
            sessions.append(cur)
            cur = []
        cur.append(r)
    if cur:
        sessions.append(cur)
    longest = max(sessions, key=lambda s: s[-1]["end"] - s[0]["start"]) if sessions else []
    longest_info = None
    if longest:
        dur = (longest[-1]["end"] - longest[0]["start"]).total_seconds() / 60
        longest_info = {
            "minutes": round(dur), "tracks": len(longest),
            "start": longest[0]["start"].strftime("%b %-d, %-I:%M %p"),
            "end": longest[-1]["end"].strftime("%b %-d, %-I:%M %p"),
        }

    # Unique / fun stats ----------------------------------------------------
    # On-repeat: most plays of one song in a single day
    day_track = Counter((r["end"].date(), tkey(r)) for r in streams)
    obsession = None
    if day_track:
        (d, t), n = day_track.most_common(1)[0]
        obsession = {"track": t, "plays": n, "date": d.strftime("%b %-d")}

    # Back-to-back repeats: longest run of the same song in a row
    best_run, run, run_track, prev = 0, 0, None, None
    for r in streams:
        k = tkey(r)
        run = run + 1 if k == prev else 1
        if run > best_run:
            best_run, run_track = run, k
        prev = k

    # Comfort song: on the most distinct days
    track_days = defaultdict(set)
    artist_months = defaultdict(set)
    for r in streams:
        track_days[tkey(r)].add(r["end"].date())
        artist_months[r["artist"]].add(r["end"].month)
    comfort = max(track_days.items(), key=lambda kv: len(kv[1])) if track_days else None
    months_present = sorted({r["end"].month for r in streams})
    ride_or_die = [a for a, ms in artist_months.items() if len(ms) == len(months_present)] if months_present else []
    ride_or_die.sort(key=lambda a: -by_artist_min[a])

    # Discovery: artists first heard each month (within this year)
    first_seen = {}
    for r in streams:
        first_seen.setdefault(r["artist"], r["end"].month)
    new_per_month = [0] * 12
    for m in first_seen.values():
        new_per_month[m - 1] += 1

    # Top artist each month
    month_artist = defaultdict(Counter)
    for r in played:
        month_artist[r["end"].month][r["artist"]] += r["ms"] / 60000
    top_by_month = [{"month": MONTHS[m - 1],
                     "artist": c.most_common(1)[0][0],
                     "minutes": round(c.most_common(1)[0][1])}
                    for m, c in sorted(month_artist.items())]

    # Loyalty / diversity
    art_total = sum(by_artist_min.values()) or 1
    top5_share = sum(v for _, v in by_artist_min.most_common(5)) / art_total
    artist_songs = defaultdict(set)
    for r in streams:
        artist_songs[r["artist"]].add(r["track"])
    one_song_artists = sum(1 for songs in artist_songs.values() if len(songs) == 1)
    pod_min = Counter()
    for r in pods:
        pod_min[r["artist"]] += r["ms"] / 60000
    # entropy-based "effective number of artists"
    ent = -sum((v / art_total) * math.log(v / art_total) for v in by_artist_min.values() if v > 0)
    effective_artists = round(math.exp(ent)) if by_artist_min else 0

    # Behavior flags (extended export only)
    def rate(rs, key):
        vals = [r[key] for r in rs if r[key] is not None]
        return round(100 * sum(1 for v in vals if v) / len(vals), 1) if vals else None

    skip_counter = Counter(tkey(r) for r in played if r["skipped"]
                           or r["reason_end"] in ("fwdbtn",))
    quick_skips = Counter(tkey(r) for r in played if r["ms"] < 10_000)
    most_skipped = (skip_counter or quick_skips).most_common(1)
    reason_start = Counter(r["reason_start"] for r in rows if r["reason_start"])
    reason_end = Counter(r["reason_end"] for r in rows if r["reason_end"])

    night = sum(hourly[0:5]) / (total_min or 1)
    morning = sum(hourly[5:12]) / (total_min or 1)
    peak_hour = max(range(24), key=lambda h: hourly[h]) if rows else None
    weekend = (weekday_tot[5] + weekday_tot[6]) / (total_min or 1)

    def persona():
        if night > 0.15:
            return "Night Owl", f"{night:.0%} of your listening happened between midnight and 5 AM."
        if morning > 0.40:
            return "Early Bird", f"{morning:.0%} of your listening happened before noon."
        if top5_share > 0.5:
            return "Superfan", f"Your top 5 artists took {top5_share:.0%} of your music time."
        if effective_artists > 150:
            return "Explorer", f"Your listening was spread across ~{effective_artists} artists' worth of variety."
        return "All-Rounder", "Balanced across the day, the week, and your library."

    p_name, p_why = persona()

    firsts = {
        "first_song": f'{tkey(streams[0])}' if streams else None,
        "first_when": streams[0]["end"].strftime("%b %-d, %-I:%M %p") if streams else None,
        "latest_song": tkey(streams[-1]) if streams else None,
        "latest_when": streams[-1]["end"].strftime("%b %-d, %-I:%M %p") if streams else None,
    }

    # Late night anthem: top track between 0-4
    late = Counter(tkey(r) for r in streams if r["end"].hour < 5)
    late_anthem = late.most_common(1)[0] if late else None

    # Completion rate: share of plays not skipped and >= 30s
    completion = round(100 * len(streams) / len(played), 1) if played else None

    has_extended = any(r["platform"] for r in rows)

    return {
        "year": year,
        "has_extended": has_extended,
        "range": [rows[0]["end"].strftime("%b %-d, %Y"), rows[-1]["end"].strftime("%b %-d, %Y")] if rows else None,
        "totals": {
            "minutes": round(total_min),
            "music_minutes": round(music_min),
            "podcast_minutes": round(sum(r["ms"] for r in pods) / 60000),
            "hours": round(total_min / 60, 1),
            "days": round(total_min / 1440, 1),
            "streams": len(streams),
            "plays": len(played),
            "unique_tracks": len({tkey(r) for r in streams}),
            "unique_artists": len({r["artist"] for r in streams}),
            "unique_albums": len({r["album"] for r in streams if r["album"]}),
            "active_days": len(active_days),
            "avg_per_active_day": round(total_min / len(active_days)) if active_days else 0,
        },
        "top_tracks": _top(by_track_min, 15),
        "top_tracks_plays": _top(by_track_plays, 15),
        "top_artists": _top(by_artist_min, 15),
        "top_artists_plays": _top(by_artist_plays, 15),
        "top_albums": _top(by_album_min, 10),
        "top_podcasts": _top(pod_min, 5),
        "monthly": [{"label": MONTHS[i], "value": round(v)} for i, v in enumerate(monthly)],
        "hourly": [{"label": f"{h}", "value": round(v)} for h, v in enumerate(hourly)],
        "weekday": [{"label": DAYS[i], "value": round(v)} for i, v in enumerate(weekday_tot)],
        "heatmap": [[round(v) for v in row] for row in heat],
        "calendar": calendar,
        "new_artists_per_month": [{"label": MONTHS[i], "value": v} for i, v in enumerate(new_per_month)],
        "top_artist_by_month": top_by_month,
        "platforms": _top(Counter(r["platform"] for r in rows if r["platform"]), 6),
        "countries": _top(Counter(r["country"] for r in rows if r["country"]), 6),
        "fun": {
            "persona": p_name, "persona_why": p_why,
            "peak_hour": peak_hour,
            "busiest_weekday": DAYS[max(range(7), key=lambda i: weekday_tot[i])] if rows else None,
            "weekend_share": round(100 * weekend, 1),
            "night_share": round(100 * night, 1),
            "biggest_day": {"date": top_day[0].strftime("%A, %b %-d") if top_day[0] else None,
                            "minutes": round(top_day[1])},
            "streak": {"days": streak_len, "from": streak_from, "to": streak_to},
            "longest_session": longest_info,
            "sessions": len(sessions),
            "avg_session_min": round(sum((s[-1]["end"] - s[0]["start"]).total_seconds()
                                         for s in sessions) / 60 / len(sessions)) if sessions else 0,
            "obsession": obsession,
            "repeat_run": {"track": run_track, "times": best_run} if best_run > 1 else None,
            "comfort": {"track": comfort[0], "days": len(comfort[1])} if comfort else None,
            "ride_or_die": ride_or_die[:5],
            "top5_share": round(100 * top5_share, 1),
            "effective_artists": effective_artists,
            "artist_gini": round(_gini(list(by_artist_min.values())), 2),
            "one_song_artists": one_song_artists,
            "late_night_anthem": {"track": late_anthem[0], "plays": late_anthem[1]} if late_anthem else None,
            "completion_rate": completion,
            "skip_rate": rate(played, "skipped"),
            "shuffle_rate": rate(rows, "shuffle"),
            "offline_rate": rate(rows, "offline"),
            "incognito_rate": rate(rows, "incognito"),
            "most_skipped": {"track": most_skipped[0][0], "times": most_skipped[0][1]} if most_skipped else None,
            "reason_start": _top(reason_start, 6),
            "reason_end": _top(reason_end, 6),
            **firsts,
        },
    }


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(HERE / "data"), help="folder or .zip of your Spotify export")
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--tz", default=None, help="IANA timezone, e.g. America/New_York (default: this computer's)")
    ap.add_argument("--out", default=str(HERE / "output" / "spotify_2026.html"))
    ap.add_argument("--json", default=None, help="also write raw stats JSON here")
    args = ap.parse_args()

    tz = ZoneInfo(args.tz) if args.tz else datetime.now().astimezone().tzinfo
    rows, files = load(Path(args.data).expanduser(), args.year, tz)
    if not files:
        raise SystemExit(f"No streaming-history JSON found in {args.data}. "
                         "See README.md for how to request your export.")
    if not rows:
        raise SystemExit(f"Found {len(files)} file(s) but no plays in {args.year}.")

    stats = compute(rows, args.year)
    stats["source_files"] = len(files)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__STATS__*/null", json.dumps(stats, ensure_ascii=False).replace("</", "<\\/"))
    out.write_text(html, encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")

    t = stats["totals"]
    print(f"{len(rows):,} plays in {args.year} from {len(files)} file(s)")
    print(f"{t['minutes']:,} minutes · {t['unique_tracks']:,} songs · {t['unique_artists']:,} artists")
    if stats["top_artists"]:
        print(f"Top artist: {stats['top_artists'][0]['name']}")
    print(f"Dashboard → {out}")


if __name__ == "__main__":
    main()
