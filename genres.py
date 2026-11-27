"""Artist → genre lookup via MusicBrainz (free, no account needed).

Spotify's data export has no genres, so we look them up by artist name.
Only artist names are sent. Results are cached in data/genres_cache.json so
each artist is fetched once; later runs are instant and work offline.

MusicBrainz asks clients to stay at or under 1 request per second.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://musicbrainz.org/ws/2/artist"
USER_AGENT = "spotify-year-stats/1.1 (personal listening stats script)"
DELAY = 1.1  # seconds between requests

# Tags that aren't genres (MusicBrainz falls back to tags when no curated genre exists)
NOT_GENRES = {
    "seen live", "american", "british", "english", "canadian", "usa", "uk", "us", "female vocalists",
    "male vocalists", "favorites", "favourite", "under 2000 listeners", "spotify", "seen in concert",
    "check out", "awesome", "love", "beautiful", "sexy", "chill", "cool", "best", "good", "all",
    "american rapper", "producer", "singer", "songwriter", "rapper", "dj", "composer", "band",
    "multiple ipi", "death by gun", "death by overdose", "death by suicide", "deceased", "fixme",
}


NOT_GENRE_RX = re.compile(r"fictitious|fictional|character|death by|^\d{4}s?$|^seen |vocalists?$|^under \d")


def is_genre(name: str) -> bool:
    n = name.strip().lower()
    return bool(n) and n not in NOT_GENRES and not NOT_GENRE_RX.search(n)


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (503, 429):          # rate limited: back off
                time.sleep(2 + 3 * attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2 + 3 * attempt)
    return {}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower().replace("&", "and").replace("$", "s"))


def _clean(items: list[dict]) -> list[str]:
    out = []
    for g in sorted(items, key=lambda g: -g.get("count", 0)):
        name = g.get("name", "").strip().lower()
        if is_genre(name) and name not in out:
            out.append(name)
    return out[:5]


def lookup(artist: str) -> list[str]:
    """Best-guess genres for one artist name (most relevant first)."""
    q = urllib.parse.quote(f'artist:"{artist}"')
    data = _get(f"{API}/?query={q}&fmt=json&limit=5")
    time.sleep(DELAY)
    cands = data.get("artists") or []
    want = _norm(artist)
    exact = [a for a in cands if _norm(a.get("name", "")) == want
             or any(_norm(al.get("name", "")) == want for al in a.get("aliases", []) or [])]
    pick = None
    if exact:
        # several artists can share a name: prefer the most-tagged (usually the famous one)
        pick = max(exact, key=lambda a: (sum(t.get("count", 0) for t in a.get("tags", []) or []), a.get("score", 0)))
    elif cands and cands[0].get("score", 0) >= 95:
        pick = cands[0]
    if not pick:
        return []
    detail = _get(f"{API}/{pick['id']}?inc=genres+tags&fmt=json")
    time.sleep(DELAY)
    genres = _clean(detail.get("genres") or [])
    return genres or _clean(detail.get("tags") or pick.get("tags") or [])


def fetch(artists: list[str], cache_path: Path) -> dict[str, list[str]]:
    """Genres for each artist, using and updating the on-disk cache."""
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    todo = [a for a in dict.fromkeys(artists) if a not in cache]
    if todo:
        eta = round(len(todo) * DELAY * 2 / 60)
        print(f"Looking up genres for {len(todo)} artists on MusicBrainz (~{eta} min, cached after this)...",
              file=sys.stderr)
    for i, a in enumerate(todo, 1):
        try:
            cache[a] = lookup(a)
        except Exception as e:  # keep going; a failed artist just has no genre
            print(f"  ! {a}: {e}", file=sys.stderr)
            continue
        if i % 25 == 0 or i == len(todo):
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=0))
            print(f"  {i}/{len(todo)}", file=sys.stderr)
    return cache
