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
import re
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "dashboard_template.html"

GENRES: dict[str, list[str]] = {}   # artist -> genres; filled by --genres (see genres.py)
GENRE_WEIGHTS = [0.5, 0.3, 0.2]      # an artist's minutes are split across its top 3 genres

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


REASONS = {
    "trackdone": "Played to the end", "endplay": "Stopped or switched", "fwdbtn": "Skipped (next button)",
    "backbtn": "Went back", "clickrow": "Picked another song", "remote": "Changed from another device",
    "logout": "Logged out", "playbtn": "Pressed play", "appload": "App opened",
    "unexpected-exit": "App closed while playing", "unexpected-exit-while-paused": "App closed while paused",
    "trackerror": "Playback error", "popup": "Popup", "uriopen": "Opened a link",
}


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


def load(src: Path, tz: ZoneInfo) -> tuple[list[dict], list[str]]:
    """Every play in the export (all years), sorted, plus the files read."""
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


def genre_minutes(played: list[dict]) -> tuple[Counter, float]:
    """Minutes per genre, and the % of minutes whose artist has a known genre."""
    out, known, total = Counter(), 0.0, 0.0
    for r in played:
        m = r["ms"] / 60000
        total += m
        gs = GENRES.get(r["artist"]) or []
        if not gs:
            continue
        known += m
        w = GENRE_WEIGHTS[:len(gs[:3])]
        for g, wt in zip(gs, w):
            out[g] += m * wt / sum(w)
    return out, (100 * known / total if total else 0.0)


def _genre_months(by_month: dict, year_g: Counter) -> list[dict]:
    """Top genre each month, plus the 'standout': the genre whose share that
    month most exceeds its share for the whole year (needs 30+ minutes)."""
    ytot = sum(year_g.values()) or 1
    out = []
    for m, rs in sorted(by_month.items()):
        g, _ = genre_minutes(rs)
        if not g:
            continue
        mtot = sum(g.values())
        lift = [(v / mtot - year_g[k] / ytot, k) for k, v in g.items() if v >= 30]
        out.append({"month": MONTHS[m - 1], "genre": g.most_common(1)[0][0],
                    "standout": max(lift)[1] if lift else None})
    return out


def compute(rows: list[dict], year: int, past: list[dict] | None = None, light: bool = False) -> dict:
    """Stats for one year. `past` = plays from earlier years (for discovery,
    loyalty and comeback stats). `light` skips the heavy extras (used for the
    per-year comparison summaries)."""
    past = past or []
    prior_artists = {r["artist"] for r in past
                     if r["kind"] == "track" and r["ms"] >= STREAM_MS}
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

    # Discovery: artists first heard each month. When the export covers earlier
    # years, artists you'd already streamed before this year don't count as new.
    first_seen = {}
    for r in streams:
        if r["artist"] not in prior_artists:
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
    reason_start = Counter(REASONS.get(r["reason_start"], r["reason_start"]) for r in rows if r["reason_start"])
    reason_end = Counter(REASONS.get(r["reason_end"], r["reason_end"]) for r in rows if r["reason_end"])

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
    n_months = rows[-1]["end"].month if rows else 12   # don't chart months that haven't happened

    extras = {} if light else extra_stats(rows, streams, played, past, year, prior_artists,
                                         first_seen, by_artist_min, tkey)
    gmin, gcov = genre_minutes(played)
    genres = None
    if GENRES:
        by_m = defaultdict(list)
        for r in played:
            by_m[r["end"].month].append(r)
        top_artist_in = {}
        for g, _ in gmin.most_common(20):
            c = Counter()
            for r in played:
                if g in (GENRES.get(r["artist"]) or [])[:3]:
                    c[r["artist"]] += r["ms"] / 60000
            top_artist_in[g] = c.most_common(1)[0][0] if c else None
        genres = {
            "top": [{"name": g, "value": round(v), "artist": top_artist_in.get(g)} for g, v in gmin.most_common(15)],
            "coverage": round(gcov, 1),
            "distinct": sum(1 for v in gmin.values() if v >= 30),
            "top3_share": round(100 * sum(v for _, v in gmin.most_common(3)) / (sum(gmin.values()) or 1), 1),
            "by_month": [] if light else _genre_months(by_m, gmin),
            "all": [[g, round(v)] for g, v in gmin.most_common(60)],
        }
    clock = [sum(hourly[5:12]), sum(hourly[12:17]), sum(hourly[17:22]), sum(hourly[22:]) + sum(hourly[:5])]
    return {
        "year": year,
        "extras": extras,
        "genres": genres,
        "clock": [{"label": l, "value": round(100 * v / (total_min or 1), 1)}
                  for l, v in zip(["Morning (5–12)", "Afternoon (12–5)", "Evening (5–10)", "Night (10–5)"], clock)],
        "artist_minutes": [[a, round(m), by_artist_plays[a]] for a, m in by_artist_min.most_common(200)],
        "track_minutes": [[t, round(m), by_track_plays[t]] for t, m in by_track_min.most_common(100)],
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
        "monthly": [{"label": MONTHS[i], "value": round(v)} for i, v in enumerate(monthly[:n_months])],
        "hourly": [{"label": f"{h}", "value": round(v)} for h, v in enumerate(hourly)],
        "weekday": [{"label": DAYS[i], "value": round(v)} for i, v in enumerate(weekday_tot)],
        "heatmap": [[round(v) for v in row] for row in heat],
        "calendar": calendar,
        "new_artists_per_month": [{"label": MONTHS[i], "value": v} for i, v in enumerate(new_per_month[:n_months])],
        "new_artists_total": sum(new_per_month),
        "has_prior_history": bool(prior_artists),
        "top_new_artists": _top(Counter({a: by_artist_min[a] for a in first_seen}), 10),
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
# Objective artist score
# --------------------------------------------------------------------------
SCORE_WEIGHTS = {   # how much each signal counts toward an artist's score
    "minutes": 0.25,     # total time
    "days": 0.20,        # how many different days you played them
    "months": 0.15,      # how many months of the year they showed up
    "songs": 0.15,       # how many of their songs you played (depth)
    "completion": 0.10,  # share of plays you didn't skip
    "years": 0.15,       # how many years of your history they appear in
}
SCORE_LABELS = {"minutes": "Minutes", "days": "Days played", "months": "Months present",
                "songs": "Different songs", "completion": "Didn't skip", "years": "Years in your history"}


def artist_scores(played, streams, past, n=10):
    """Rank artists on several signals at once, so one binge week can't win.
    Each signal is scaled 0-1, then combined with SCORE_WEIGHTS into a
    0-100 score. Only artists you streamed 10+ times are ranked."""
    m = defaultdict(lambda: {"minutes": 0.0, "plays": 0, "streams": 0, "days": set(),
                             "months": set(), "songs": set()})
    for r in played:
        a = m[r["artist"]]
        a["minutes"] += r["ms"] / 60000
        a["plays"] += 1
    for r in streams:
        a = m[r["artist"]]
        a["streams"] += 1
        a["days"].add(r["end"].date())
        a["months"].add(r["end"].month)
        a["songs"].add(r["track"])
    past_years = defaultdict(Counter)
    for r in past:
        if r["kind"] == "track" and r["ms"] >= STREAM_MS:
            past_years[r["artist"]][r["end"].year] += 1
    pool = {k: v for k, v in m.items() if v["streams"] >= 10}
    if not pool:
        return []
    raw = {k: {"minutes": v["minutes"], "days": len(v["days"]), "months": len(v["months"]),
               "songs": len(v["songs"]), "completion": v["streams"] / max(1, v["plays"]),
               "years": 1 + sum(1 for c in past_years[k].values() if c >= 5)}
           for k, v in pool.items()}

    # Normalize each signal to 0-1. Volume signals are measured against your
    # #1 artist on a square-root scale (so big gaps still matter but one
    # binge can't dominate); months, completion and years are already bounded.
    n_months = len({r["end"].month for r in streams}) or 12
    max_years = max(x["years"] for x in raw.values())
    peak = {k: max(x[k] for x in raw.values()) or 1 for k in ("minutes", "days", "songs")}
    pcts = {
        "minutes": {k: math.sqrt(x["minutes"] / peak["minutes"]) for k, x in raw.items()},
        "days": {k: math.sqrt(x["days"] / peak["days"]) for k, x in raw.items()},
        "songs": {k: math.sqrt(x["songs"] / peak["songs"]) for k, x in raw.items()},
        "months": {k: x["months"] / n_months for k, x in raw.items()},
        "completion": {k: x["completion"] for k, x in raw.items()},
        "years": {k: x["years"] / max_years for k, x in raw.items()},
    }
    score = {k: 100 * sum(w * pcts[metric][k] for metric, w in SCORE_WEIGHTS.items()) for k in raw}
    by_min = [a for a, _ in sorted(raw.items(), key=lambda kv: -kv[1]["minutes"])]
    out = []
    for k in sorted(score, key=lambda k: -score[k])[:n]:
        x = raw[k]
        out.append({
            "name": k, "score": round(score[k], 1), "minutes_rank": by_min.index(k) + 1,
            "minutes": round(x["minutes"]), "days": x["days"], "months": x["months"],
            "songs": x["songs"], "completion": round(100 * x["completion"]), "years": x["years"],
            "since": min(past_years[k]) if past_years[k] else None,
            "parts": {metric: round(100 * pcts[metric][k]) for metric in SCORE_WEIGHTS},
        })
    return out


# --------------------------------------------------------------------------
# More unique stats
# --------------------------------------------------------------------------
FEAT_RX = re.compile(r"[(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+([^)\]]+)[)\]]", re.I)


def extra_stats(rows, streams, played, past, year, prior_artists, first_seen, by_artist_min, tkey):
    x = {}
    music_rows = [r for r in rows if r["kind"] == "track"]

    # Objective top artists
    x["true_top"] = artist_scores(played, streams, past)
    x["score_weights"] = {SCORE_LABELS[k]: v for k, v in SCORE_WEIGHTS.items()}

    # Genres you picked up this year: real time now, almost none before
    if GENRES:
        now_g, _ = genre_minutes(played)
        past_played = [r for r in past if r["kind"] == "track" and r["ms"] > 0]
        past_g, _ = genre_minutes(past_played)
        picked = []
        for g, v in now_g.most_common():
            if v >= 45 and past_g.get(g, 0) < 20:
                fans = Counter()
                first = None
                for r in played:
                    if g in (GENRES.get(r["artist"]) or [])[:3]:
                        fans[r["artist"]] += r["ms"] / 60000
                        first = first or r["end"]
                picked.append({"name": g, "value": round(v), "artist": fans.most_common(1)[0][0],
                               "since": first.strftime("%b %-d") if first else None})
        x["new_genres"] = picked[:10]
        # Rediscovered: genre that was big in an earlier year, quiet last year, back now
        by_year = defaultdict(list)
        for r in past_played:
            by_year[r["end"].year].append(r)
        yg = {y: genre_minutes(rs)[0] for y, rs in by_year.items()}
        last = yg.get(year - 1, Counter())
        back = []
        for g, v in now_g.most_common(40):
            peaks = [(yg[y].get(g, 0), y) for y in yg if y < year - 1]
            if peaks and v >= 60:
                pk, py = max(peaks)
                if pk >= 300 and last.get(g, 0) < pk * 0.1:
                    back.append({"name": g, "value": round(v), "peak_year": py, "peak": round(pk)})
        if back:
            x["genre_comeback"] = max(back, key=lambda b: b["value"])
        # Most niche genre: smallest genre with real listening (fewest artists in your library)
        artists_in = Counter()
        for a, gs in GENRES.items():
            for g in gs[:3]:
                artists_in[g] += 1
        niche = [(artists_in[g], -v, g) for g, v in now_g.items() if v >= 60]
        if niche:
            n, v, g = min(niche)
            x["niche_genre"] = {"name": g, "minutes": round(-v), "artists": n}

    # Longest-running favorite: artist in your current top 50 you've known longest
    past_first = {}
    past_year_sets = defaultdict(set)
    for r in past:
        if r["kind"] == "track" and r["ms"] >= STREAM_MS:
            past_first.setdefault(r["artist"], r["end"])
            past_year_sets[r["artist"]].add(r["end"].year)
    top50 = [a for a, _ in by_artist_min.most_common(50)]
    olds = [(past_first[a], a) for a in top50 if a in past_first]
    if olds:
        d, a = min(olds)
        x["oldest_favorite"] = {"artist": a, "since": d.strftime("%b %Y"),
                                "years": year - d.year}
    # Most loyal across years: in the most different years (incl. this one)
    if past_year_sets:
        here = {r["artist"] for r in streams}
        loyal = max(here, key=lambda a: (len(past_year_sets.get(a, ())), by_artist_min[a]), default=None)
        if loyal and past_year_sets.get(loyal):
            yrs = sorted(past_year_sets[loyal] | {year})
            x["most_loyal"] = {"artist": loyal, "years": len(yrs), "first": yrs[0]}

    # Comeback song: big in an earlier year, silent last year, back this year
    past_track_year = defaultdict(Counter)
    for r in past:
        if r["kind"] == "track" and r["ms"] >= STREAM_MS:
            past_track_year[tkey(r)][r["end"].year] += 1
    now_plays = Counter(tkey(r) for r in streams)
    comebacks = []
    for t, n in now_plays.items():
        hist = past_track_year.get(t)
        if not hist or n < 5:
            continue
        peak_year, peak = max(hist.items(), key=lambda kv: kv[1])
        if peak >= 10 and hist.get(year - 1, 0) == 0 and peak_year < year - 1:
            comebacks.append((n, t, peak_year, peak))
    if comebacks:
        n, t, py, pk = max(comebacks)
        x["comeback_song"] = {"track": t, "plays": n, "peak_year": py, "peak_plays": pk}

    # Fastest obsession: new artist with most minutes in their first 30 days
    fast = Counter()
    first_play = {}
    for r in played:
        a = r["artist"]
        if a in prior_artists:
            continue
        first_play.setdefault(a, r["end"])
        if r["end"] - first_play[a] <= timedelta(days=30):
            fast[a] += r["ms"] / 60000
    if fast:
        a, mins = fast.most_common(1)[0]
        x["fastest_obsession"] = {"artist": a, "minutes": round(mins),
                                  "since": first_play[a].strftime("%b %-d")}

    # Seasonal fling: artist (60+ min) whose listening is most packed into one month
    am = defaultdict(Counter)
    for r in played:
        am[r["artist"]][r["end"].month] += r["ms"] / 60000
    flings = [(c.most_common(1)[0][1] / sum(c.values()), a, c.most_common(1)[0][0], sum(c.values()))
              for a, c in am.items() if sum(c.values()) >= 60]
    if flings:
        share, a, mo, tot = max(flings, key=lambda f: (round(f[0], 2), f[3]))
        x["seasonal_fling"] = {"artist": a, "month": MONTHS[mo - 1], "share": round(100 * share),
                               "minutes": round(tot)}

    # Skip patience: how long before you usually skip
    skipped = sorted(r["ms"] for r in played if r["skipped"] or r["reason_end"] == "fwdbtn")
    if skipped:
        x["skip_patience_sec"] = round(skipped[len(skipped) // 2] / 1000)

    # Most impatient day (30+ plays)
    day_p, day_s = Counter(), Counter()
    for r in played:
        d = r["end"].date()
        day_p[d] += 1
        if r["skipped"] or r["ms"] < STREAM_MS:
            day_s[d] += 1
    rates = [(day_s[d] / n, d, n) for d, n in day_p.items() if n >= 30]
    if rates:
        rt, d, n = max(rates)
        x["skippiest_day"] = {"date": d.strftime("%A, %b %-d"), "rate": round(100 * rt), "plays": n}

    # Longest single play
    if music_rows:
        r = max(music_rows, key=lambda r: r["ms"])
        x["longest_play"] = {"track": tkey(r), "minutes": round(r["ms"] / 60000, 1)}

    # Most-featured guest artist (from "feat." in titles)
    feats = Counter()
    for r in streams:
        for m in FEAT_RX.findall(r["track"]):
            for name in re.split(r",|&| and ", m):
                if name.strip():
                    feats[name.strip()] += 1
    if feats:
        a, n = feats.most_common(1)[0]
        x["feature_king"] = {"artist": a, "plays": n}

    # Album purist: share of plays that followed a different song from the same album
    seq = same = 0
    for a, b in zip(played, played[1:]):
        if b["start"] - a["end"] > SESSION_GAP or not b["album"]:
            continue
        seq += 1
        same += (a["album"] == b["album"] and a["track"] != b["track"])
    if seq:
        x["album_flow"] = round(100 * same / seq, 1)

    # Morning starter / night closer: most common first & last song of the day
    firsts, lasts = {}, {}
    for r in streams:
        d = (r["end"] - timedelta(hours=4)).date()   # day "starts" at 4 AM
        firsts.setdefault(d, tkey(r))
        lasts[d] = tkey(r)
    if firsts:
        t, n = Counter(firsts.values()).most_common(1)[0]
        x["day_opener"] = {"track": t, "days": n}
        t, n = Counter(lasts.values()).most_common(1)[0]
        x["day_closer"] = {"track": t, "days": n}

    # Hidden gem: most-played song by an artist you only played one song from
    songs_by_artist = defaultdict(set)
    for r in streams:
        songs_by_artist[r["artist"]].add(tkey(r))
    gems = [(now_plays[next(iter(s))], next(iter(s))) for a, s in songs_by_artist.items() if len(s) == 1]
    if gems:
        n, t = max(gems)
        x["hidden_gem"] = {"track": t, "plays": n}

    # Weekday vs weekend top artist
    wk, we = Counter(), Counter()
    for r in played:
        (we if r["end"].weekday() >= 5 else wk)[r["artist"]] += r["ms"] / 60000
    if wk and we:
        x["weekday_artist"] = wk.most_common(1)[0][0]
        x["weekend_artist"] = we.most_common(1)[0][0]

    # Old vs new: share of minutes on artists you knew before this year
    if prior_artists:
        tot = sum(by_artist_min.values()) or 1
        x["familiar_share"] = round(100 * sum(v for a, v in by_artist_min.items() if a in prior_artists) / tot, 1)

    # Song-hopping: average different songs per session-hour
    days_count = len({r["end"].date() for r in streams}) or 1
    x["songs_per_day"] = round(len(streams) / days_count, 1)

    # Most consistent song: top song by number of different weeks played
    weeks = defaultdict(set)
    for r in streams:
        weeks[tkey(r)].add(r["end"].isocalendar()[1])
    if weeks:
        t, w = max(weeks.items(), key=lambda kv: len(kv[1]))
        x["all_year_song"] = {"track": t, "weeks": len(w)}

    return x


# --------------------------------------------------------------------------
# Year-over-year
# --------------------------------------------------------------------------
def year_summary(st: dict) -> dict:
    t, f = st["totals"], st["fun"]
    return {
        "minutes": t["minutes"], "streams": t["streams"], "plays": t["plays"],
        "unique_tracks": t["unique_tracks"], "unique_artists": t["unique_artists"],
        "active_days": t["active_days"], "avg_per_active_day": t["avg_per_active_day"],
        "new_artists": st["new_artists_total"],
        "top5_share": f["top5_share"], "effective_artists": f["effective_artists"],
        # older exports leave the skip flag empty, which reads as 0% — treat as unknown
        "skip_rate": f["skip_rate"] or None, "shuffle_rate": f["shuffle_rate"],
        "night_share": f["night_share"], "weekend_share": f["weekend_share"],
        "peak_hour": f["peak_hour"], "persona": f["persona"],
        "streak": f["streak"]["days"], "one_song_artists": f["one_song_artists"],
        "artists": st["artist_minutes"], "tracks": st["track_minutes"][:100],
        "genres": st["genres"]["all"] if st.get("genres") else [],
    }


def build_history(all_rows, year, cutoff):
    """Per-year summaries for every year with real listening. 'ytd' cuts each
    year at the same month/day as the target year's last play, for a fair
    comparison with an unfinished year."""
    by_year = defaultdict(list)
    for r in all_rows:
        by_year[r["end"].year].append(r)
    out = {}
    for y in sorted(by_year):
        if y > year:
            continue
        rs = by_year[y]
        if sum(1 for r in rs if r["ms"] >= STREAM_MS) < 200:   # too little data to compare
            continue
        past = [r for r in all_rows if r["end"].year < y]
        full = compute(rs, y, past, light=True)
        ytd_rows = [r for r in rs if (r["end"].month, r["end"].day) <= cutoff]
        ytd = compute(ytd_rows, y, past, light=True) if ytd_rows else None
        out[y] = {"full": year_summary(full), "ytd": year_summary(ytd) if ytd else None}
    return out


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(HERE / "data"), help="folder or .zip of your Spotify export")
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--tz", default=None, help="IANA timezone, e.g. America/New_York (default: this computer's)")
    ap.add_argument("--out", default=str(HERE / "output" / "spotify_2026.html"))
    ap.add_argument("--json", default=None, help="also write raw stats JSON here")
    ap.add_argument("--genres", action="store_true",
                    help="look up artist genres on MusicBrainz (sends artist names only; cached in data/)")
    ap.add_argument("--genre-artists", type=int, default=300,
                    help="how many of your top artists to look up for the main year (default 300)")
    args = ap.parse_args()

    tz = ZoneInfo(args.tz) if args.tz else datetime.now().astimezone().tzinfo
    all_rows, files = load(Path(args.data).expanduser(), tz)
    if not files:
        raise SystemExit(f"No streaming-history JSON found in {args.data}. "
                         "See README.md for how to request your export.")
    rows = [r for r in all_rows if r["end"].year == args.year]
    if not rows:
        raise SystemExit(f"Found {len(files)} file(s) but no plays in {args.year}.")
    past = [r for r in all_rows if r["end"].year < args.year]

    import sys
    sys.path.insert(0, str(HERE))
    import genres as genre_lookup
    cache = HERE / "data" / "genres_cache.json"
    if args.genres:
        mins = defaultdict(lambda: Counter())
        for r in all_rows:
            if r["kind"] == "track" and r["end"].year <= args.year:
                mins[r["end"].year][r["artist"]] += r["ms"]
        want = [a for a, _ in mins[args.year].most_common(args.genre_artists)]
        for y, c in mins.items():
            if y != args.year:
                want += [a for a, _ in c.most_common(100)]
        GENRES.update(genre_lookup.fetch(want, cache))
    elif cache.exists():
        GENRES.update(json.loads(cache.read_text()))   # reuse earlier lookups, no network
    for artist, gs in list(GENRES.items()):           # drop tags that aren't genres
        GENRES[artist] = [g for g in gs if genre_lookup.is_genre(g)]

    stats = compute(rows, args.year, past)
    last = rows[-1]["end"]
    stats["history"] = build_history(all_rows, args.year, (last.month, last.day))
    stats["cutoff"] = last.strftime("%b %-d")
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
