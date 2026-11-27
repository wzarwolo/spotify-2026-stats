# Spotify 2026 stats

Turns your Spotify data export into a single-page dashboard of your 2026 listening:
totals, top songs/artists/albums/podcasts, time-of-day and calendar heatmaps, and
a set of unique stats Spotify Wrapped doesn't show you.

Runs locally with Python 3.9+ standard library only. Your data never leaves your machine.

## 1. Get your data from Spotify

Spotify doesn't offer full listening history through its API (it only returns your last 50 plays),
so the export is the only complete source.

1. Go to **spotify.com → Account → Privacy settings** (<https://www.spotify.com/account/privacy/>).
2. Under *Download your data*, tick **Extended streaming history** (best: per-play detail, skips,
   shuffle, devices, countries). *Account data* also works but has fewer fields.
3. Confirm the email. Spotify says it can take up to 30 days; it's often a few days.
4. Download the `my_spotify_data.zip` they email you.

## 2. Build the dashboard

```bash
mkdir -p data && mv ~/Downloads/my_spotify_data.zip data/
python3 spotify_stats.py --tz America/New_York
open output/spotify_2026.html
```

Options: `--year 2025`, `--data path/to/folder_or.zip`, `--out file.html`, `--json stats.json`.

Want to preview first? `python3 make_sample_data.py && python3 spotify_stats.py --data sample`.

## What's in it

**Totals** — minutes/hours/days, streams (30 s+, Spotify's rule), unique songs/artists/albums,
active days, average per listening day, podcast minutes.

**Top lists** — songs, artists, albums, podcasts, by minutes or by play count.

**When** — by month, hour, weekday, a weekday × hour heatmap, and a full-year calendar.

**Your true top 5:** an artist score (0–100) that combines six signals so one binge week can't win:
minutes (25%), days played (20%), months present (15%), different songs (15%), share not skipped (10%),
and years the artist appears in your history (15%). Each signal is scaled against your #1 artist.
Tweak `SCORE_WEIGHTS` in `spotify_stats.py` to change the balance.

**Compare with past years:** pick any earlier year and see changes in minutes, streams, variety,
discovery, loyalty (top-5 share), skip and shuffle rate, and night listening. Toggle between *same dates*
(fair for an unfinished year) and *full years*. Also shows a loyalty check (your old top 25 artists still in
your top 50), biggest climbers, new arrivals, artists who faded out, personality shift, minutes and
discovery per year, and your #1 artist every year. Years with fewer than 200 streams are skipped.

**Unique stats**
- Listening personality (Night Owl / Early Bird / Superfan / Explorer / All-Rounder)
- Biggest day, longest daily streak, longest session (20-min gap = new session)
- On-repeat day (most plays of one song in a single day) and back-to-back record
- Comfort song (played on the most different days), late-night anthem (midnight–5 AM)
- Night-owl score, weekend share, peak hour
- Artist loyalty (top-5 share), variety score (entropy-based effective artist count) and Gini
- One-song wonders, stick-around rate, skip rate, shuffle/offline/private-session rate, most-skipped song
- New artists discovered per month, top artist each month, ride-or-die artists (every month)
- Devices, countries, why tracks ended (extended export only)
- First and most recent song of the year
- Longest-running favorite, most loyal artist across years, comeback song (big years ago, silent last year, back now)
- Fastest obsession (new artist with the most minutes in their first 30 days), seasonal fling, hidden gem
- All-year song (most different weeks), feature king (most-streamed guest from "feat." credits)
- Weekday vs weekend top artist, skip patience (seconds before you usually skip), most impatient day
- Longest single listen, album-listener score, comfort zone (% of time on artists you already knew)
- Songs per day, morning opener and night closer, listening clock (morning/afternoon/evening/night)

## Files

| File | What it does |
|---|---|
| `spotify_stats.py` | Loads the export (folder or zip), filters to the year, computes stats, writes HTML |
| `dashboard_template.html` | The dashboard; stats are injected as JSON. No external scripts or CDNs |
| `make_sample_data.py` | Writes fake data to `sample/` for a demo |

`data/`, `output/` and `sample/` are git-ignored so your listening history isn't pushed.
