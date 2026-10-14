# Letterboxd Friends

A personal project: for every year and decade of film, how many of the people I
follow on Letterboxd have seen each movie — plus what they thought of it, what's
rising, and what's playing in my city this week. It runs on my Mac as a small
native app, keeps itself up to date in the background, and publishes a public
summary page (with a Greek edition).

Letterboxd shows "watched by N friends" one film at a time. This answers the
questions you can't ask it directly: *what are the 50 most-watched 1970s films
among my friends? which new releases are they actually loving? what's playing
nearby that I haven't seen but they have?*

## What it does

- **Counts, per year and per decade** — every film the people I follow have
  logged, with how many of them watched it, their average rating (half-star
  precision) and the share who liked it.
- **A native macOS app** (Swift + WebKit around a local Python server): sortable
  lists with posters, film and director/actor search, "who watched it" with each
  person's rating, liked lists containing a film, and a one-click
  **"Recommend me something"** (unseen by me, rated by 10+ friends, best first).
- **What changed** — since the last scrape, over 7 days or 30 days, from dated
  snapshots, plus a **Rising** view across every list at once.
- **Local screenings** — parses a weekly local film newsletter, matches its films
  to Letterboxd (following renames and catching mislinked entries), drops
  screenings once their showtime has passed, and links each venue to that
  film's page at the theater.
- **Two published pages** — English and Greek (Greek titles, cast, genres from
  Wikidata and Letterboxd), with posters for the top of every list, compressed to
  fit a 16 MB page limit (8.7 MB of data → 3.7 MB, decompressed in the browser).
- **Excluding people** — anyone can be left out of every count; it's a recount
  of stored per-person data, not a re-scrape, and it survives username changes.

## How it stays correct

Most of the work went into making the numbers trustworthy without me watching:

- **A run that can't see everyone never overwrites good data.** Failed fetches
  are retried; stragglers fall back to their last known list; if too many fail,
  or the friends list itself comes back short, the run stops and leaves the
  existing data untouched.
- **A self-check every hour** — that requests still get through, runs used the
  whole friends list, nothing is overdue, no list has shrunk, posters are
  readable, local listings are current — with a macOS notification for new
  serious problems.
- **Freshness by age, not by timer.** An hourly check refreshes whatever is due
  (this year and last daily, each decade every 14 days), so sleep, restarts and
  missed runs simply catch up on wake.
- **Renamed accounts are recognised** by matching film lists, so an exclusion
  follows the person, not the username.

## Layout

| File | Role |
|---|---|
| `letterboxd_friends_v2.py` | the scraper: one period → counts, ratings, likes, per-person data |
| `letterboxd_refresh.py`, `letterboxd_tick.py` | refreshing, scheduling, catching up |
| `letterboxd_selfcheck.py` | the hourly health checks |
| `letterboxd_app.py` | local server + the app's interface |
| `mac/main.swift` | the native macOS shell |
| `letterboxd_publish.py`, `letterboxd_publish_el.py` | the English and Greek pages |
| `letterboxd_posters.py`, `letterboxd_filmpages.py`, `letterboxd_wikidata.py` | posters, genres, Greek titles, film details |
| `letterboxd_people.py` | exclusions and recounts |
| `letterboxd_history.py` | dated snapshots for week/month comparisons |
| `letterboxd_net.py` | how requests are made |
| `letterboxd_settings.py` | your username, read from a local file outside the repo |
| `launchd/` | the background job definition |

## Running it

```bash
pip install -r requirements.txt
mkdir -p ~/.letterboxd_cache
cp settings.example.json ~/.letterboxd_cache/settings.json   # then put your username in it
python3 letterboxd_friends_v2.py 2026                        # one year…
python3 letterboxd_friends_v2.py 1990s                       # …or a decade
python3 letterboxd_app.py                                    # the interface, on 127.0.0.1:8765
```

All data — scraped lists, per-person files, caches, settings — lives in your home
folder, never in this repository.

## A note on scraping

This reads public Letterboxd pages. Letterboxd's terms don't allow automated
access, and its servers may refuse it; this version makes plain requests and
reports when they're refused. It's a personal, non-commercial project — keep
request rates low, keep the results to yourself, and don't use it to collect
data about people who haven't agreed to it.
