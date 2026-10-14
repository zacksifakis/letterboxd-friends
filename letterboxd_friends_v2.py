"""
Count how many of your Letterboxd friends (people you follow) have watched each
film from a given year or decade.

Usage:
    python3 letterboxd_friends_v2.py 1970s
    python3 letterboxd_friends_v2.py 2025

Unlike v1, this version refuses to silently under-report. Every page that fails
is retried, and anything still broken at the end is listed explicitly so you know
whether the numbers are trustworthy.
"""

import os
import re
import sys
import time
import json
import glob
import random
import shutil
import pandas as pd
import letterboxd_net as net
from bs4 import BeautifulSoup

import letterboxd_settings as _settings
USERNAME = _settings.require_username()
BASE = "https://letterboxd.com"

# Accounts you follow that Letterboxd's own /following/ pages fail to list.
# Its profile counter includes them but the list endpoint omits them, so they
# have to be added by hand or their watches go uncounted.
EXTRA_FRIENDS = _settings.EXTRA_FRIENDS

PERIOD = sys.argv[1] if len(sys.argv) > 1 else "2025"
IS_DECADE = PERIOD.endswith("s")

# Pacing. Slower than v1 on purpose: the failures we were chasing only appear
# once a run has made several hundred requests.
PAGE_PAUSE = (0.6, 1.2)
FRIEND_PAUSE = (0.3, 0.8)
ROTATE_EVERY = 40          # new session after this many requests
MAX_RETRIES = 5

_session = None
_requests_made = 0
stats = {"requests": 0, "retries": 0, "rotations": 0}


def session():
    """One session, rotated periodically to avoid per-session throttling."""
    global _session, _requests_made
    if _session is None or _requests_made >= ROTATE_EVERY:
        _session = net.session()
        _requests_made = 0
        stats["rotations"] += 1
    return _session


def fetch(url):
    """GET a page, retrying hard. Returns (soup, ok). ok=False means give up."""
    global _requests_made, _session
    for attempt in range(MAX_RETRIES):
        try:
            s = session()
            _requests_made += 1
            stats["requests"] += 1
            r = s.get(url, timeout=20)
        except Exception:
            stats["retries"] += 1
            _session = None
            time.sleep(2 + 3 * attempt)
            continue

        if r.status_code == 200:
            return BeautifulSoup(r.text, "html.parser"), True

        # 403/429/5xx: back off and get a fresh session before trying again
        stats["retries"] += 1
        print(f"      [retry {attempt + 1}/{MAX_RETRIES}] HTTP {r.status_code}  {url}")
        _session = None
        # Refusals that a fresh session doesn't fix: ask the network layer
        # for a new setup before the next attempt.
        if r.status_code == 403 and attempt == 1:
            net.rotate(f"HTTP 403 on {url}", log=lambda m: print("      " + m))
            stats["rotations"] += 1
        time.sleep(3 + 4 * attempt)

    return None, False


def has_next(soup):
    nxt = soup.select_one(".paginate-nextprev .next")
    return bool(nxt and nxt.name == "a" and nxt.get("href"))


def profile_following_count(username):
    """The number Letterboxd itself shows, used to verify our friend list."""
    soup, ok = fetch(f"{BASE}/{username}/")
    if not ok:
        return None
    m = re.search(r"([\d,]+)\s+Following", soup.get_text(" ", strip=True))
    return int(m.group(1).replace(",", "")) if m else None


def get_friends(username):
    """People you follow. Uses a.name (exact) rather than a.avatar (includes you)."""
    print("Fetching your friends list...")
    expected = profile_following_count(username)
    friends, page = [], 1

    while True:
        soup, ok = fetch(f"{BASE}/{username}/following/page/{page}/")
        if not ok:
            print(f"  !! could not load following page {page} — friend list is INCOMPLETE")
            break

        names = [a.get("href", "").strip("/") for a in soup.select("a.name[href^='/']")]
        names = [n for n in names if n and "/" not in n and n.lower() != username.lower()]
        if not names:
            break
        friends.extend(names)

        if not has_next(soup):
            break
        page += 1
        time.sleep(random.uniform(*PAGE_PAUSE))

    scraped = list(dict.fromkeys(friends))
    added = [e for e in EXTRA_FRIENDS if e not in scraped]
    friends = scraped + added
    if added:
        print(f"  + added {len(added)} manually ({', '.join(added)}) "
              f"— omitted by Letterboxd's own following pages")

    # The profile stat we can read while logged out does not include the
    # accounts in EXTRA_FRIENDS, so verify the scraped list against it and
    # account for the manual additions separately.
    if expected is None:
        print(f"Using {len(friends)} friends (could not read profile count to verify).")
    elif len(scraped) == expected:
        print(f"Using {len(friends)} friends — the {len(scraped)} public ones match "
              f"your profile's Following count. ✓")
    elif len(scraped) < expected:
        print(f"Scraped only {len(scraped)} of the {expected} your profile lists. "
              f"MISSING {expected - len(scraped)} — counts will be low.")
    else:
        print(f"Scraped {len(scraped)} but your profile lists {expected} — "
              f"unexpected extras, counts may be high.")
    return friends


def get_films(user, period):
    """
    All films this user logged from `period`, with how they rated it and
    whether they liked it — both are on the same pages, so this costs nothing
    extra. Returns ([(slug, title, year, rating, liked)], complete), where
    rating is in half stars (1-10, 0 = not rated) and liked is 0/1.
    complete=False means we know data is missing.
    """
    kind = "decade" if IS_DECADE else "year"
    films, page = [], 1

    while True:
        soup, ok = fetch(f"{BASE}/{user}/films/{kind}/{period}/page/{page}/")
        if not ok:
            return films, False

        posters = soup.select("div[data-item-slug]")
        if not posters:
            # Page 1 legitimately empty = user logged nothing from this period.
            # A later page empty is a broken response: we only got here because
            # the previous page said there was a next one.
            return films, page == 1

        for p in posters:
            slug = p.get("data-item-slug")
            if not slug:
                continue
            img = p.find("img")
            title = (img.get("alt") or "").strip() if img else ""
            if not title:
                title = slug.replace("-", " ").title()
            m = re.search(r"\((\d{4})\)\s*$", p.get("data-item-name") or "")

            # The star rating and the heart live next to the poster, in the
            # <li> around it: "rated-8" is four stars, "rated-1" is half a one.
            item = p.parent
            rating, liked = 0, 0
            if item is not None:
                star = item.select_one("span.rating")
                if star:
                    n = re.search(r"rated-(\d+)", " ".join(star.get("class") or []))
                    rating = int(n.group(1)) if n else 0
                liked = 1 if item.select_one("span.like") else 0
            films.append((slug, title, m.group(1) if m else "", rating, liked))

        if not has_next(soup):
            return films, True
        page += 1
        time.sleep(random.uniform(*PAGE_PAUSE))


def _meta_path(period):
    return os.path.expanduser(f"~/friends_watched_{period}.meta.json")


def _was_clean(period):
    """True if this period's last write came from a run with every friend fetched."""
    try:
        with open(_meta_path(period)) as f:
            m = json.load(f)
        return not (m.get("filled") or m.get("missing"))
    except Exception:
        return False


def write_year_csvs(df, period, meta):
    """
    A decade scrape already knows each film's release year, so every single-year
    list inside it can be written straight out — no extra requests.

    An existing year file is only replaced when this decade's data is newer, so
    a freshly scraped year is never clobbered by an older decade run — and a
    decade run that had to lean on old data never replaces a year whose own
    run was complete. Each year gets the decade run's meta, so the hourly job
    knows how much to trust it.
    """
    decade_clean = not (meta.get("filled") or meta.get("missing"))
    if not period.endswith("s"):
        return []

    decade_prefix = period[:3]
    decade_file = os.path.expanduser(f"~/friends_watched_{period}.csv")
    decade_mtime = os.path.getmtime(decade_file) if os.path.exists(decade_file) else time.time()
    history = os.path.expanduser("~/.letterboxd_history")

    written = []
    for year, sub in df[df["Year"].astype(str) != ""].groupby("Year"):
        year = str(year)
        if not year.startswith(decade_prefix):
            continue                       # stray year, not part of this decade

        target = os.path.expanduser(f"~/friends_watched_{year}.csv")
        if os.path.exists(target):
            if os.path.getmtime(target) > decade_mtime:
                continue                   # that year was scraped more recently
            if not decade_clean and _was_clean(year):
                continue                   # keep the complete year over a patchy decade
            os.makedirs(history, exist_ok=True)
            shutil.copy2(target, os.path.join(history, f"{year}.prev.csv"))

        (sub.sort_values("Friend Watch Count", ascending=False)
            .reset_index(drop=True)
            .to_csv(target, index=False))
        with open(_meta_path(year), "w") as f:
            json.dump(dict(meta, derived_from=period), f, indent=1)
        written.append((year, len(sub)))

    written.sort()
    return written


def acquire_scrape_lock():
    """
    Only one scrape at a time, however it was started (app, daily job, biweekly
    job, terminal). Parallel runs would double the request rate against
    Letterboxd and race to write the same CSVs. A second run waits its turn.
    """
    import fcntl
    lock = open(os.path.expanduser("~/.letterboxd_scrape.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another scrape is already running — waiting for it to finish...", flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
        print("Previous scrape finished, starting now.", flush=True)
    return lock            # keep a reference: the lock lasts as long as the process


def last_known_friends():
    """How many friends the most recent good run used, from the meta files."""
    best = 0
    for path in glob.glob(os.path.expanduser("~/friends_watched_*.meta.json")):
        try:
            with open(path) as f:
                best = max(best, int(json.load(f).get("friends") or 0))
        except Exception:
            pass
    return best


def main():
    _lock = acquire_scrape_lock()
    started = time.time()
    friends = get_friends(USERNAME)
    if not friends:
        print("No friends found. Check the username and profile privacy.")
        sys.exit(3)

    # Letterboxd sometimes answers the following pages with nothing, which once
    # left a run with a single friend — and it happily overwrote a full list
    # with its findings. A list that short is a failure, not a smaller circle.
    known = last_known_friends()
    if known and len(friends) < 0.9 * known:
        print(f"Only {len(friends)} friends came back, but the last run used {known}. "
              f"Letterboxd is not answering properly, so this run is stopping.")
        print(f"The existing {PERIOD} data has been left as it was. Try again later.")
        sys.exit(3)

    friend_films = {}
    incomplete, empty = [], []

    print(f"\nChecking {len(friends)} friends for films from {PERIOD}...")
    for i, friend in enumerate(friends, 1):
        films, complete = get_films(friend, PERIOD)
        friend_films[friend] = films
        if not complete:
            incomplete.append(friend)
            print(f"   !! {friend}: INCOMPLETE ({len(films)} films before failure)")
        elif not films:
            empty.append(friend)
        if i % 10 == 0 or i == len(friends):
            seen = {f[0] for fl in friend_films.values() for f in fl}
            print(f"   {i}/{len(friends)} friends  ({len(seen)} distinct films so far)")
        time.sleep(random.uniform(*FRIEND_PAUSE))

    # Second and third chances. Most failures are temporary — Letterboxd
    # throttling for a minute, or the Mac sleeping mid-run.
    recovered = []
    for round_no in (1, 2):
        if not incomplete:
            break
        wait = 30 * round_no
        print(f"\nRetrying {len(incomplete)} incomplete friend(s) in {wait}s (round {round_no})...")
        time.sleep(wait)
        globals()["_session"] = None
        still = []
        for friend in incomplete:
            films, complete = get_films(friend, PERIOD)
            if complete:
                friend_films[friend] = films
                recovered.append(friend)
                if not films:
                    empty.append(friend)
                print(f"   {friend}: recovered ({len(films)} films)")
            else:
                if len(films) > len(friend_films[friend]):
                    friend_films[friend] = films
                still.append(friend)
            time.sleep(random.uniform(*FRIEND_PAUSE))
        incomplete = still

    # If a big share still failed, Letterboxd was blocking us. Filling that many
    # from old data would just be republishing stale numbers — keep the old file.
    if len(incomplete) > 0.2 * len(friends):
        print(f"\n{len(incomplete)} of {len(friends)} friends still failed after retries "
              f"— Letterboxd was most likely blocking the scrape.")
        print(f"The existing {PERIOD} data has been left as it was. Try again later.")
        sys.exit(2)

    # Anyone still missing keeps their list from the previous run (merged with
    # whatever we did get), so a friend Letterboxd didn't answer for today still
    # counts instead of silently lowering every film they've seen.
    filled, missing = [], []
    if incomplete:
        prev_audit_path = os.path.expanduser(f"~/friends_watched_{PERIOD}_per_friend.json")
        prev_csv_path = os.path.expanduser(f"~/friends_watched_{PERIOD}.csv")
        prev_audit, prev_rows = {}, {}
        if os.path.exists(prev_audit_path):
            with open(prev_audit_path) as f:
                prev_audit = json.load(f)
        if os.path.exists(prev_csv_path):
            import csv as _csv
            with open(prev_csv_path, newline="") as f:
                for r in _csv.DictReader(f):
                    prev_rows[r["Slug"]] = (r.get("Film Title") or r["Slug"], (r.get("Year") or "").strip())
        for friend in incomplete:
            old = prev_audit.get(friend)
            if not old:
                missing.append(friend)
                continue
            have = {f[0] for f in friend_films[friend]}
            # The audit is {slug: [rating, liked]} now, a plain list before.
            old_items = old.items() if isinstance(old, dict) else ((sl, [0, 0]) for sl in old)
            for slug, mark in old_items:
                if slug not in have:
                    title, year = prev_rows.get(slug, (slug.replace("-", " ").title(), ""))
                    rating, liked = (list(mark) + [0, 0])[:2]
                    friend_films[friend].append((slug, title, year, rating, liked))
            filled.append(friend)

    # Someone excluded who renamed their account still counts as excluded.
    try:
        import letterboxd_people
        prev_path = os.path.expanduser(f"~/friends_watched_{PERIOD}_per_friend.json")
        if os.path.exists(prev_path):
            with open(prev_path) as f:
                prev_audit = json.load(f)
            letterboxd_people.carry_renames(
                prev_audit, {u: {x[0] for x in films} for u, films in friend_films.items()})
    except Exception as e:
        print(f"  (could not check for renamed accounts: {e})")

    counts, titles, years, per_friend = {}, {}, {}, {}
    stars, likes = {}, {}          # slug -> [ratings in half stars], slug -> hearts
    # People left out (letterboxd_people.py) are still scraped and kept in the
    # per-person file, so bringing them back is a recount — they just don't count.
    try:
        import letterboxd_people
        skip = letterboxd_people.excluded()
    except Exception:
        skip = set()
    for friend, films in friend_films.items():
        mine = {}
        for slug, title, year, rating, liked in films:
            if slug in mine:
                continue
            mine[slug] = [rating, liked]
            if friend in skip:
                titles.setdefault(slug, title)
                if year:
                    years.setdefault(slug, year)
                continue
            counts[slug] = counts.get(slug, 0) + 1
            titles.setdefault(slug, title)
            if year:
                years.setdefault(slug, year)
            if rating:
                stars.setdefault(slug, []).append(rating)
            if liked:
                likes[slug] = likes.get(slug, 0) + 1
        per_friend[friend] = {s_: mine[s_] for s_ in sorted(mine)}

    if not counts:
        # Every page came back empty or failed — almost always Letterboxd
        # blocking us. Say so plainly and leave the existing CSV untouched
        # rather than dying inside pandas with an unhelpful KeyError.
        print(f"\nNothing collected from any of the {len(friends)} friends "
              f"({len(incomplete)} incomplete, {len(empty)} empty).")
        print(f"{stats['requests']} requests, {stats['retries']} retries. "
              f"Letterboxd was most likely blocking the scrape.")
        print(f"The existing {PERIOD} data has been left as it was. Try again later.")
        sys.exit(2)

    df = pd.DataFrame(
        [
            {
                "Film Title": titles.get(s, s),
                "Year": years.get(s, ""),
                "Slug": s,
                "Friend Watch Count": c,
                # out of 5, from whoever rated it; Letterboxd rates in half stars
                "Avg Rating": round(sum(stars[s]) / len(stars[s]) / 2, 2) if stars.get(s) else "",
                "Ratings": len(stars.get(s, [])),
                "Likes": likes.get(s, 0),
                "Liked %": round(100 * likes.get(s, 0) / c) if c else 0,
                "Letterboxd URL": f"{BASE}/film/{s}/",
            }
            for s, c in counts.items()
        ]
    ).sort_values("Friend Watch Count", ascending=False).reset_index(drop=True)

    out = os.path.expanduser(f"~/friends_watched_{PERIOD}.csv")
    df.to_csv(out, index=False)

    audit = os.path.expanduser(f"~/friends_watched_{PERIOD}_per_friend.json")
    with open(audit, "w") as f:
        json.dump(per_friend, f, indent=1)

    # How complete this run was — the hourly job reads it to redo any period
    # that had to lean on old data.
    meta = {"finished": time.strftime("%Y-%m-%dT%H:%M:%S"), "friends": len(friends),
            "recovered": recovered, "filled": filled, "missing": missing}
    with open(_meta_path(PERIOD), "w") as f:
        json.dump(meta, f, indent=1)

    split = write_year_csvs(df, PERIOD, meta)
    if split:
        print(f"\nDerived {len(split)} year lists from this decade "
              f"(no extra scraping): "
              + ", ".join(f"{y} ({n:,})" for y, n in split))

    print("\n=== TOP 15 MOST WATCHED FILMS BY YOUR FRIENDS ===")
    print(df.head(15).to_string())

    print("\n--- data quality ---")
    print(f"friends checked      : {len(friends)}")
    print(f"friends with 0 films : {len(empty)}")
    print(f"recovered on retry   : {len(recovered)}"
          + (f"  -> {', '.join(recovered)}" if recovered else ""))
    print(f"filled from previous : {len(filled)}"
          + (f"  -> {', '.join(filled)}" if filled else ""))
    print(f"INCOMPLETE friends   : {len(missing)}"
          + (f"  -> {', '.join(missing)}" if missing else ""))
    print(f"http requests        : {stats['requests']}  (retries: {stats['retries']}, "
          f"session rotations: {stats['rotations']})")
    print(f"elapsed              : {time.time() - started:.0f}s")
    if missing:
        print("\nCounts are LOW — re-run to pick up the friends listed above.")
    elif filled:
        print(f"\nAll friends accounted for — {len(filled)} from the previous run because "
              f"Letterboxd didn't answer for them today. The hourly job will redo this soon.")
    else:
        print("\nEvery friend fetched completely. Counts should match Letterboxd. ✓")

    print(f"\nSpreadsheet : {out}")
    print(f"Audit trail : {audit}")


if __name__ == "__main__":
    main()
