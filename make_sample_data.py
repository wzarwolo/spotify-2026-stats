#!/usr/bin/env python3
"""Generate fake Extended Streaming History so you can preview the dashboard."""
import json, random
from datetime import datetime, timedelta, timezone
from pathlib import Path

random.seed(7)
artists = {f"Artist {c}": [f"Song {c}{i}" for i in range(1, random.randint(2, 14))] for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}
weights = [1 / (i + 1) ** 1.1 for i in range(len(artists))]
plats = ["iOS 18.1 (iPhone15,2)", "OS X 15.0 [arm 2]", "web_player osx", "cast_to_sonos"]
rows, t = [], datetime(2026, 1, 1, 0, 12, tzinfo=timezone.utc)
end = datetime(2026, 11, 24, tzinfo=timezone.utc)
while t < end:
    if random.random() < 0.25:
        t += timedelta(hours=random.randint(3, 30)); continue
    for _ in range(random.randint(3, 25)):
        a = random.choices(list(artists), weights)[0]
        s = random.choice(artists[a])
        skip = random.random() < 0.18
        ms = random.randint(2000, 25000) if skip else random.randint(150000, 260000)
        t += timedelta(milliseconds=ms)
        rows.append({"ts": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "platform": random.choice(plats), "ms_played": ms,
                     "conn_country": random.choice(["US"] * 9 + ["CA"]), "master_metadata_track_name": s,
                     "master_metadata_album_artist_name": a, "master_metadata_album_album_name": f"{a} LP",
                     "spotify_track_uri": f"spotify:track:{abs(hash(s))}", "episode_name": None, "episode_show_name": None,
                     "reason_start": "trackdone", "reason_end": "fwdbtn" if skip else "trackdone",
                     "shuffle": random.random() < 0.4, "skipped": skip, "offline": False, "incognito_mode": False})
    t += timedelta(hours=random.randint(1, 9))
out = Path(__file__).parent / "sample" / "Streaming_History_Audio_2026_sample.json"
out.write_text(json.dumps(rows))
print(f"wrote {len(rows)} fake plays → {out}")
