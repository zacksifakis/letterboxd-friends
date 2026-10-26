"""
Genres and Greek titles from Letterboxd's own film pages, for the Greek site.

Wikidata gives Greek titles for only about 4,000 films and genres mostly for
older ones. Every Letterboxd film page lists the film's genres and its
alternative titles — usually including the official Greek one — so one page
per film fills both gaps. Covered: the films that get posters (the top of every
list). Cached in ~/.letterboxd_cache/filmpages.json; later runs only fetch
films new to a top list.

    python3 letterboxd_filmpages.py fetch            # fetch whatever is missing
    python3 letterboxd_filmpages.py fetch --max 300
    python3 letterboxd_filmpages.py status
"""

import os
import re
import sys
import json
import time
import html as htmlmod

HOME = os.path.expanduser("~")
DESKTOP = os.path.join(HOME, "Desktop")
sys.path.insert(0, DESKTOP)
CACHE = os.path.join(HOME, ".letterboxd_cache", "filmpages.json")
RECHECK_DAYS = 60

# Letterboxd uses TMDB's genre set; these are the Greek names the site shows.
GENRES_EL = {
    "Action": "Δράσης", "Adventure": "Περιπέτεια", "Animation": "Κινούμενα σχέδια",
    "Comedy": "Κωμωδία", "Crime": "Αστυνομική", "Documentary": "Ντοκιμαντέρ",
    "Drama": "Δράμα", "Family": "Οικογενειακή", "Fantasy": "Φαντασίας",
    "History": "Ιστορική", "Horror": "Τρόμου", "Music": "Μουσική",
    "Mystery": "Μυστηρίου", "Romance": "Αισθηματική", "Science Fiction": "Επιστημονικής φαντασίας",
    "Thriller": "Θρίλερ", "TV Movie": "Τηλεταινία", "War": "Πολεμική", "Western": "Γουέστερν",
}

GREEK = re.compile(r"[Ͱ-Ͽἀ-῿]")


def load():
    try:
        with open(CACHE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d):
    tmp = CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, CACHE)


def parse(page):
    """(genres in English, Greek titles) from a Letterboxd film page."""
    genres = []
    m = re.search(r'"genre"\s*:\s*\[([^\]]*)\]', page)
    if m:
        genres = [htmlmod.unescape(g) for g in re.findall(r'"([^"]+)"', m.group(1))]
    greek = []
    alt = re.search(r"Alternative Titles?</span></h3>\s*<div[^>]*>\s*<p>(.*?)</p>", page, re.S)
    if alt:
        for t in htmlmod.unescape(re.sub(r"<[^>]+>", "", alt.group(1))).split(","):
            t = t.strip()
            if t and GREEK.search(t) and t not in greek:
                greek.append(t)
    return genres, greek


def people(page):
    """(directors, top-billed cast) from a Letterboxd film page — Wikidata often
    has neither for new releases."""
    dirs = [htmlmod.unescape(x).strip() for x in re.findall(r'/director/[^/"]+/"[^>]*>(?:<span[^>]*>)?([^<]+)<', page)]
    cast = [htmlmod.unescape(x).strip() for x in re.findall(r'/actor/[^/"]+/"[^>]*>([^<]+)<', page)]
    clean = lambda xs, n: [x for x in dict.fromkeys(xs) if x and "*/" not in x][:n]
    return clean(dirs, 3), clean(cast, 6)


def targets():
    """The films that get posters, then the rest of every list's top 100."""
    import letterboxd_posters as lp
    out = lp.wanted()
    seen = set(out)
    for ranked in lp._lists().values():
        for s in lp.top_slugs(ranked, 100):
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out


def fetch(max_n=None, delay=1.0, log=print):
    import fcntl
    import letterboxd_net as net
    lock = open(os.path.join(HOME, ".letterboxd_filmpages.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("film pages: another fetch is already running — skipping")
        return 0
    cache, now = load(), time.time()
    try:
        import letterboxd_greek_titles as gt
        recent, every = gt.recent_slugs(), gt.RECENT_RECHECK_DAYS
    except Exception:
        recent, every = set(), RECHECK_DAYS
    # this year's and last year's films weekly (Greek titles and genres arrive late), the rest every 60 days
    todo = [s for s in targets()
            if s not in cache or "min" not in cache[s]          # read before cast and running time were kept
            or now - cache[s].get("at", 0) > (every if s in recent else RECHECK_DAYS) * 86400]
    if max_n:
        todo = todo[:max_n]
    log(f"film pages: {len(todo)} to fetch")
    session, got, failed, streak = net.session(), 0, 0, 0
    for i, slug in enumerate(todo, 1):
        page = None
        for attempt in range(3):
            try:
                r = session.get(f"https://letterboxd.com/film/{slug}/", timeout=25)
            except Exception:
                r = None
            if r is not None and r.status_code == 200:
                page = r.text
                break
            if r is not None and r.status_code == 404:
                page = ""
                break
            session = net.rotate(f"HTTP {getattr(r, 'status_code', '?')} on a film page", log=log) \
                if attempt else net.session()
            time.sleep(3 * (attempt + 1))
        if page is None:
            failed += 1
            streak += 1
            if streak >= 15:
                log("film pages: Letterboxd keeps refusing — stopping; the rest next time")
                break
        else:
            streak = 0
            genres, greek = parse(page)
            dirs, cast = people(page)
            mins = re.search(r"(\d{1,3})&nbsp;mins", page)
            cache[slug] = {"genres": genres, "el": greek, "dir": dirs, "cast": cast,
                           "min": int(mins.group(1)) if mins else 0, "at": now}
            got += 1
        if i % 100 == 0:
            _save(cache)
            log(f"film pages: {i}/{len(todo)}  (done {got}, failed {failed})")
        time.sleep(delay)
    _save(cache)
    log(f"film pages: done — {got} fetched, {failed} failed")
    return got


def status():
    cache, t = load(), targets()
    have = [s for s in t if s in cache]
    print(f"films covered: {len(t):,}  fetched: {len(have):,}")
    print(f"  with genres      : {sum(1 for s in have if cache[s]['genres']):,}")
    print(f"  with Greek title : {sum(1 for s in have if cache[s]['el']):,}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "fetch":
        fetch(int(sys.argv[sys.argv.index("--max") + 1]) if "--max" in sys.argv else None)
    else:
        status()
