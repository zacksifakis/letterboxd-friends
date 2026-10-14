"""
Posters for the website.

claude.ai pages can only show images embedded in the page itself, and a page
is capped at 16 MB, so posters go to the lists' top films:

  - every decade, and the current year:  the first 100 films
  - every other year:                    the first 20

A tie straddling the cut is kept whole (up to 20% extra) or left out whole, so
posters always stop at a change of rank. ~2,300 films in all.

Each poster is fetched once, as a 70x105 thumbnail, into
~/.letterboxd_cache/posters/<slug>.jpg. Later runs only fetch films that are
new to a top list, so after the first pass it's a handful of requests a week.

    python3 letterboxd_posters.py fetch            # fetch whatever is missing
    python3 letterboxd_posters.py fetch --max 500  # stop after 500
    python3 letterboxd_posters.py status           # how many are cached
"""

import os
import io
import re
import sys
import csv
import json
import glob
import time
import base64
from datetime import datetime

import letterboxd_net as net

HOME = os.path.expanduser("~")
CACHE = os.path.join(HOME, ".letterboxd_cache", "posters")
INDEX = os.path.join(HOME, ".letterboxd_cache", "posters.json")
TOP = 100                   # decades and the current year
YEAR_TOP = 20               # every other year
W, H = 70, 105              # stored size; shown at about half, so sharp on Retina
WEBP_QUALITY = 65
RETRY_FAILED_AFTER_DAYS = 3


def _load_index():
    try:
        with open(INDEX) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_index(ix):
    tmp = INDEX + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ix, f)
    os.replace(tmp, INDEX)


def _lists():
    """{period: [(slug, count), ...]} — every saved list, in rank order."""
    out = {}
    for path in glob.glob(f"{HOME}/friends_watched_*.csv"):
        m = re.fullmatch(r"friends_watched_([0-9]{4}s?)\.csv", os.path.basename(path))
        if not m:
            continue
        with open(path, newline="") as f:
            rows = [r for r in csv.DictReader(f) if r.get("Slug")]
        rows.sort(key=lambda r: -int(r.get("Friend Watch Count") or 0))
        out[m.group(1)] = [(r["Slug"], int(r.get("Friend Watch Count") or 0)) for r in rows]
    return out


def top_slugs(ranked, n):
    """
    The first n of a ranked [(slug, count)] list, never splitting a tie: a tie
    straddling the cut comes in whole if it ends within n + 20%, otherwise it
    is left out whole.
    """
    if len(ranked) <= n:
        return [s for s, _ in ranked]
    edge = ranked[n - 1][1]
    end = n
    while end < len(ranked) and ranked[end][1] == edge:
        end += 1
    if end <= n + n // 5:
        return [s for s, _ in ranked[:end]]
    start = n - 1
    while start > 0 and ranked[start - 1][1] == edge:
        start -= 1
    return [s for s, _ in ranked[:start]]


def depth(period, year_top=YEAR_TOP):
    return TOP if period.endswith("s") or period == str(datetime.now().year) else year_top


def wanted():
    """Slugs needing a poster, most useful first: this year, decades, then years newest-first."""
    lists = _lists()
    this_year = str(datetime.now().year)
    order = ([this_year] + sorted((p for p in lists if p.endswith("s")), reverse=True)
             + sorted((p for p in lists if not p.endswith("s") and p != this_year), reverse=True))
    seen, out = set(), []
    for p in order:
        for s in top_slugs(lists.get(p, []), depth(p)):
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out


def have(slug):
    return os.path.exists(os.path.join(CACHE, slug + ".jpg"))


def fetch(max_n=None, delay=0.6, log=print):
    import fcntl
    os.makedirs(CACHE, exist_ok=True)
    lock = open(os.path.join(HOME, ".letterboxd_posters.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)   # one fetcher at a time
    except BlockingIOError:
        log("posters: another fetch is already running — skipping")
        return 0
    ix = _load_index()
    now = time.time()

    todo = []
    for s in wanted():
        if have(s):
            continue
        e = ix.get(s, {})
        if e.get("none"):
            continue                                      # Letterboxd has no poster
        if e.get("failed") and now - e["failed"] < RETRY_FAILED_AFTER_DAYS * 86400:
            continue
        todo.append(s)
    if max_n:
        todo = todo[:max_n]
    log(f"posters: {len(todo)} to fetch")
    if not todo:
        return 0

    session = net.session()
    got = none = failed = 0
    blocked_streak = 0
    for i, slug in enumerate(todo, 1):
        url = None
        for attempt in range(3):
            try:
                r = session.get(f"https://letterboxd.com/film/{slug}/poster/std/{W}/", timeout=20)
            except Exception:
                r = None
            if r is not None and r.status_code == 200:
                m = re.search(r'https://a\.ltrbxd\.com/resized/[^"\'\s]+?-0-%d-0-%d-crop\.jpg[^"\'\s]*' % (W, H), r.text)
                url = m.group(0) if m else ""
                break
            if r is not None and r.status_code == 404:
                url = ""
                break
            # A refusal a fresh session can't fix: ask the network layer
            # for a new setup before the next attempt.
            session = net.rotate(f"HTTP 403 for {slug}", log=log) if attempt else net.session()
            time.sleep(3 * (attempt + 1))

        if url is None:
            failed += 1
            blocked_streak += 1
            ix[slug] = {"failed": now}
            if blocked_streak >= 15:
                log("posters: Letterboxd keeps refusing — stopping for now; the rest will be fetched next time")
                break
        elif url == "":
            none += 1
            blocked_streak = 0
            ix[slug] = {"none": True}                     # no poster on Letterboxd
        else:
            blocked_streak = 0
            try:
                img = session.get(url.replace("&amp;", "&"), timeout=20)
                if img.status_code == 200 and img.content[:2] == b"\xff\xd8":
                    tmp = os.path.join(CACHE, slug + ".jpg.tmp")
                    with open(tmp, "wb") as f:
                        f.write(img.content)
                    os.replace(tmp, os.path.join(CACHE, slug + ".jpg"))
                    ix[slug] = {"ok": True}
                    got += 1
                else:
                    ix[slug] = {"failed": now}
                    failed += 1
            except Exception:
                ix[slug] = {"failed": now}
                failed += 1

        if i % 50 == 0:
            _save_index(ix)
            log(f"posters: {i}/{len(todo)}  (fetched {got}, no poster {none}, failed {failed})")
        time.sleep(delay)

    _save_index(ix)
    log(f"posters: done — fetched {got}, no poster {none}, failed {failed}")
    return got


_encoded = {}


def _webp_b64(slug):
    if slug not in _encoded:
        from PIL import Image
        path = os.path.join(CACHE, slug + ".jpg")
        try:
            im = Image.open(path).convert("RGB")
            if im.size != (W, H):
                im = im.resize((W, H), Image.LANCZOS)
            b = io.BytesIO()
            im.save(b, "WEBP", quality=WEBP_QUALITY, method=4)
            _encoded[slug] = base64.b64encode(b.getvalue()).decode("ascii")
        except Exception:
            try:
                os.remove(path)                           # damaged download: fetch again next time
            except OSError:
                pass
            _encoded[slug] = None
    return _encoded[slug]


def embed(lists, film_index, year_top=YEAR_TOP):
    """
    Posters to embed in a page. `lists` is {period: [(slug, count), ...]} in
    rank order and `film_index` maps slug -> the page's film number. Returns
    ({film number: base64 WebP}, {period: lowest count that gets a poster}).

    A list only gets posters once every film in its top has been tried, so a
    list is never half-filled while a fetch is still catching up.
    """
    ix = _load_index()
    posters, floor = {}, {}
    for period, ranked in lists.items():
        top = top_slugs(ranked, depth(period, year_top))
        if not top or not all(have(s) or s in ix for s in top):
            continue
        for s in top:
            if s in film_index and have(s):
                b = _webp_b64(s)
                if b:
                    posters[film_index[s]] = b
        counts = dict(ranked)
        floor[period] = min(counts[s] for s in top)
    return posters, floor


def status():
    w = wanted()
    ix = _load_index()
    ok = sum(have(s) for s in w)
    none = sum(1 for s in w if not have(s) and ix.get(s, {}).get("none"))
    failed = sum(1 for s in w if not have(s) and ix.get(s, {}).get("failed"))
    print(f"films that get a poster: {len(w):,}")
    print(f"  posters cached : {ok:,}")
    print(f"  no poster      : {none:,}")
    print(f"  failed, retry  : {failed:,}")
    print(f"  not tried yet  : {len(w) - ok - none - failed:,}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "fetch":
        n = int(sys.argv[sys.argv.index("--max") + 1]) if "--max" in sys.argv else None
        d = float(sys.argv[sys.argv.index("--delay") + 1]) if "--delay" in sys.argv else 0.6
        fetch(n, delay=d)
    else:
        status()
