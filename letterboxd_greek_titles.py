"""
Official Greek titles for the Greek site, checked against more than one source.

Letterboxd's alternative titles (from TMDB) and Wikidata's Greek labels are
often a translation rather than the title a film was released under in Greece
— Poor Things came out as "Poor Things", not "Χαμένα Κορμιά". So each film's
Greek title is taken from the most authoritative source that has it:

  1. athinorama.gr  — the Athens film guide's page for the film: the title it
                      played under in Greek cinemas (may be the English title)
  2. filmy.gr       — a Greek film database: its page title is the Greek title
  3. IMDb           — its Greek release title, from IMDb's public datasets
  4. Letterboxd     — alternative titles in Greek script
  5. Wikidata       — the Greek label, when it's in Greek script

Films are linked to IMDb through Wikidata, and to athinorama through its
sitemap (the URL is the transliterated Greek title) with the year confirming.

    python3 letterboxd_greek_titles.py imdb          # refresh IMDb ids + Greek titles
    python3 letterboxd_greek_titles.py athinorama --max 300
    python3 letterboxd_greek_titles.py status
    python3 letterboxd_greek_titles.py show the-substance
"""

import os
import re
import sys
import csv
import glob
import gzip
import json
import time
import unicodedata
import urllib.parse
import urllib.request

HOME = os.path.expanduser("~")
CACHE_DIR = os.path.join(HOME, ".letterboxd_cache")
IMDB_IDS = os.path.join(CACHE_DIR, "imdb_ids.json")          # slug -> tt id ("" = none)
IMDB_GR = os.path.join(CACHE_DIR, "imdb", "akas_gr.tsv")      # IMDb's Greek titles
ATH = os.path.join(CACHE_DIR, "athinorama.json")              # sitemap + matched pages
UA = "168SeatsPersonalProject/1.0 (personal film list; low request volume)"
AKAS_URL = "https://datasets.imdbws.com/title.akas.tsv.gz"
ATH_SITEMAP = "https://www.athinorama.gr/sitemap/movies"
IMDB_MAX_DAYS = 30
SITEMAP_MAX_DAYS = 7

GREEK = re.compile("[Ͱ-Ͽἀ-῿]")


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, d):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, path)


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def films():
    """{slug: (title, year, most friends in any list)} across every saved list."""
    out = {}
    for path in glob.glob(f"{HOME}/friends_watched_*.csv"):
        if not re.fullmatch(r"friends_watched_[0-9]{4}s?\.csv", os.path.basename(path)):
            continue
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                s = r.get("Slug")
                if not s:
                    continue
                c = int(r.get("Friend Watch Count") or 0)
                if c > out.get(s, ("", "", -1))[2]:
                    out[s] = (r.get("Film Title") or s, (r.get("Year") or "").strip(), c)
    return out


# ------------------------------------------------------------------- IMDb

def fetch_imdb_ids(slugs):
    """Letterboxd slug -> IMDb id, via Wikidata (Letterboxd film ID P6127, IMDb ID P345)."""
    import letterboxd_wikidata as wd
    ids = _load(IMDB_IDS, {})
    todo = [s for s in slugs if s not in ids and re.fullmatch(r"[a-z0-9-]+", s)]
    log(f"IMDb ids: {len(todo):,} films not yet looked up")
    size = 250
    for n, i in enumerate(range(0, len(todo), size), 1):
        batch = todo[i:i + size]
        query = ('SELECT ?slug ?imdb WHERE { VALUES ?slug { %s } '
                 '?item wdt:P6127 ?slug ; wdt:P345 ?imdb . }') % " ".join(f'"{s}"' for s in batch)
        data = wd.fetch_json(wd.SPARQL, urllib.parse.urlencode({"query": query}).encode())
        for b in data["results"]["bindings"]:
            if b["imdb"]["value"].startswith("tt"):
                ids[b["slug"]["value"]] = b["imdb"]["value"]
        for s in batch:
            ids.setdefault(s, "")
        if n % 20 == 0 or i + size >= len(todo):
            _save(IMDB_IDS, ids)
            log(f"  {min(i + size, len(todo)):,}/{len(todo):,}")
        time.sleep(0.5)
    _save(IMDB_IDS, ids)
    return ids


def refresh_imdb_titles(force=False):
    """Keep IMDb's Greek titles (one dataset file, filtered to Greece) under a month old."""
    if not force and os.path.exists(IMDB_GR) and time.time() - os.path.getmtime(IMDB_GR) < IMDB_MAX_DAYS * 86400:
        return
    os.makedirs(os.path.dirname(IMDB_GR), exist_ok=True)
    log("IMDb: downloading the alternative-titles dataset (~500 MB, kept: Greece only)")
    req = urllib.request.Request(AKAS_URL, headers={"User-Agent": UA})
    tmp = IMDB_GR + ".tmp"
    with urllib.request.urlopen(req, timeout=600) as r, gzip.GzipFile(fileobj=r) as g, \
            open(tmp, "w", encoding="utf-8") as out:
        for i, raw in enumerate(g):
            line = raw.decode("utf-8")
            if i == 0 or line.split("\t", 4)[3] == "GR":
                out.write(line)
    os.replace(tmp, IMDB_GR)


def imdb_greek():
    """{tt id: Greek release title} — the plain release title, not TV/video/working titles."""
    out = {}
    if not os.path.exists(IMDB_GR):
        return out
    with open(IMDB_GR, encoding="utf-8") as f:
        rows = csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE)
        for r in rows:
            attrs = (r.get("attributes") or "").lower()
            types = (r.get("types") or "").lower()
            if any(w in attrs for w in ("tv", "video", "dvd", "working", "festival", "short")) \
                    or any(w in types for w in ("tv", "video", "working", "festival")):
                continue
            tt, title = r["titleId"], r["title"]
            # Prefer a title in Greek script when IMDb lists more than one.
            if tt not in out or (GREEK.search(title) and not GREEK.search(out[tt])):
                out[tt] = title
    return out


# ------------------------------------------------------------- athinorama

_GL = dict(zip("αβγδεζηικλμνοπρστυφχωςάέήίόύώϊϋΐΰ", "abgdeziiklmnoprstufxosaeiiouoiuiu"))
_GL.update({"θ": "th", "ξ": "ks", "ψ": "ps"})


def url_key(title):
    """A title as athinorama writes it in URLs: Greek transliterated, words joined by _."""
    s = "".join(_GL.get(c, c) for c in title.lower())
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().replace("&", " and ")
    return "_".join(re.findall(r"[a-z0-9]+", s))


def refresh_sitemap(state):
    if state.get("sitemapAt") and time.time() - state["sitemapAt"] < SITEMAP_MAX_DAYS * 86400:
        return
    index = _get(ATH_SITEMAP).decode("utf-8", "replace")
    pages = re.findall(r"https://www\.athinorama\.gr/sitemap/movies/\d+/", index)
    urls = {}
    for p in pages:
        xml = _get(p).decode("utf-8", "replace")
        for key, aid in re.findall(r"/cinema/movie/([^/<\s]*)-(\d+)/", xml):
            urls[aid] = key.strip("_")
        time.sleep(1)
    if urls:
        state["urls"], state["sitemapAt"] = urls, time.time()


def athinorama_candidates(state, all_films, greek_hints):
    """{slug: [athinorama ids]} whose URL matches the film's English or a Greek title."""
    by_key = {}
    for aid, key in state.get("urls", {}).items():
        by_key.setdefault(key, []).append(aid)
    keys = sorted(by_key)
    import bisect
    out = {}
    for slug, (title, year, _) in all_films.items():
        hits = []
        for t in {title, *greek_hints.get(slug, [])}:
            k = url_key(t)
            if len(k) < 3:
                continue
            hits += by_key.get(k, [])
            if len(k) >= 6:                    # "the_substance" -> "the_substance_to_eliksirio_..."
                j = bisect.bisect_left(keys, k + "_")
                while j < len(keys) and keys[j].startswith(k + "_"):
                    hits += by_key[keys[j]]
                    j += 1
        if hits:
            out[slug] = sorted(set(hits))
    return out


def parse_athinorama(page):
    """(title, year, director) from an athinorama film page."""
    m = re.search(r'<script type="application/ld\+json">(\{"@context":"http://schema.org","@type":"Movie".*?)</script>',
                  page, re.S)
    title = year = director = ""
    if m:
        try:
            d = json.loads(m.group(1))
            title = d.get("name", "")
            year = str(d.get("copyrightYear") or "")
            director = (d.get("director") or {}).get("name", "") if isinstance(d.get("director"), dict) else ""
        except Exception:
            pass
    if not year:
        y = re.search(r'<span class="year">(\d{4})</span>', page)
        year = y.group(1) if y else ""
    if not title:
        h = re.search(r"<h1>(.*?)</h1>", page, re.S)
        title = h.group(1).strip() if h else ""
    import html
    return html.unescape(title).strip(), year, director


def fetch_athinorama(max_n=300, delay=1.0):
    """Fetch athinorama pages for candidate films, most-watched first; cached for good."""
    state = _load(ATH, {})
    refresh_sitemap(state)
    pages = state.setdefault("pages", {})          # athinorama id -> [title, year, director]
    all_films = films()
    cand = athinorama_candidates(state, all_films, greek_hints())
    order = sorted(cand, key=lambda s: -all_films[s][2])
    todo = []
    for s in order:
        y = all_films[s][1]
        for aid in cand[s]:
            if aid not in pages and aid not in todo:
                todo.append(aid)
    log(f"athinorama: {len(cand):,} films have candidate pages, {len(todo):,} pages not yet read")
    got = 0
    for i, aid in enumerate(todo[:max_n], 1):
        url = f"https://www.athinorama.gr/cinema/movie/{state['urls'][aid]}-{aid}/"
        try:
            pages[aid] = list(parse_athinorama(_get(url, timeout=30).decode("utf-8", "replace")))
            got += 1
        except Exception as e:
            if "404" in str(e):
                pages[aid] = ["", "", ""]
        if i % 50 == 0:
            _save(ATH, state)
        time.sleep(delay)
    _save(ATH, state)
    log(f"athinorama: read {got} pages")
    return got


def athinorama_matches(state=None, all_films=None):
    """{slug: athinorama title} where a candidate page's year matches the film's."""
    state = state or _load(ATH, {})
    all_films = all_films or films()
    pages = state.get("pages", {})
    out = {}
    for slug, ids in athinorama_candidates(state, all_films, greek_hints()).items():
        year = all_films[slug][1]
        if not year.isdigit():
            continue
        best = None
        for aid in ids:
            t, y, _ = pages.get(aid) or ("", "", "")
            if t and y.isdigit() and abs(int(y) - int(year)) <= 1:
                exact = int(y) == int(year)
                if best is None or (exact and not best[1]):
                    best = (t, exact)
        if best:
            out[slug] = best[0]
    return out


# ----------------------------------------------------------------- filmy.gr

FILMY = os.path.join(CACHE_DIR, "filmy.json")
FILMY_SITEMAP = "https://www.filmy.gr/sitemap_index.xml"


def refresh_filmy_sitemap(state):
    if state.get("sitemapAt") and time.time() - state["sitemapAt"] < SITEMAP_MAX_DAYS * 86400:
        return
    index = _get(FILMY_SITEMAP).decode("utf-8", "replace")
    urls = {}
    for p in re.findall(r"https://www\.filmy\.gr/amy_movie-sitemap\d*\.xml", index):
        xml = _get(p).decode("utf-8", "replace")
        for key in re.findall(r"filmy\.gr/movies-database/([^/<\s]+)/", xml):
            urls[key] = 1
        time.sleep(1)
    if urls:
        state["urls"], state["sitemapAt"] = sorted(urls), time.time()


def filmy_candidates(state, all_films):
    """{slug: [filmy keys]} — filmy's URLs use the original title, much like Letterboxd's slugs."""
    have = set(state.get("urls", []))
    out = {}
    for slug, (title, year, _) in all_films.items():
        keys = {slug, re.sub(r"-\d{4}(-\d+)?$", "", slug), url_key(title).replace("_", "-")}
        if year:
            keys |= {f"{k}-{year}" for k in list(keys)}
        hits = sorted(k for k in keys if k in have)
        if hits:
            out[slug] = hits
    return out


def parse_filmy(page):
    """(Greek title, year) from a filmy.gr film page's <title>: 'Τίτλος (2015) ηθοποιοί, …'."""
    import html
    m = re.search(r"<title>(.*?)</title>", page, re.S)
    t = html.unescape(m.group(1)) if m else ""
    m = re.match(r"\s*(.*?)\s*\((\d{4})\)", t)
    return (m.group(1).strip(), m.group(2)) if m else ("", "")


def fetch_filmy(max_n=300, delay=1.0):
    state = _load(FILMY, {})
    refresh_filmy_sitemap(state)
    pages = state.setdefault("pages", {})
    all_films = films()
    cand = filmy_candidates(state, all_films)
    todo = []
    for s in sorted(cand, key=lambda s: -all_films[s][2]):
        todo += [k for k in cand[s] if k not in pages and k not in todo]
    log(f"filmy.gr: {len(cand):,} films have candidate pages, {len(todo):,} pages not yet read")
    got = 0
    for i, k in enumerate(todo[:max_n], 1):
        try:
            pages[k] = list(parse_filmy(_get(f"https://www.filmy.gr/movies-database/{k}/", timeout=30)
                                        .decode("utf-8", "replace")))
            got += 1
        except Exception as e:
            if "404" in str(e):
                pages[k] = ["", ""]
        if i % 50 == 0:
            _save(FILMY, state)
        time.sleep(delay)
    _save(FILMY, state)
    log(f"filmy.gr: read {got} pages")
    return got


def filmy_matches(all_films=None):
    state = _load(FILMY, {})
    all_films = all_films or films()
    pages = state.get("pages", {})
    out = {}
    for slug, keys in filmy_candidates(state, all_films).items():
        year = all_films[slug][1]
        for k in keys:
            t, y = pages.get(k) or ("", "")
            if t and year.isdigit() and y.isdigit() and abs(int(y) - int(year)) <= 1:
                out[slug] = t
                break
    return out


# ------------------------------------------------------------- the answer

def greek_hints():
    """Greek-script titles Letterboxd and Wikidata suggest, per film."""
    out = {}
    try:
        import letterboxd_filmpages as fp
        for s, d in fp.load().items():
            if d and d.get("el"):
                out.setdefault(s, []).extend(d["el"])
    except Exception:
        pass
    wd = _load(os.path.join(CACHE_DIR, "wikidata_el.json"), {}).get("films", {})
    for s, d in wd.items():
        if d and GREEK.search(d.get("el") or ""):
            out.setdefault(s, []).append(d["el"])
    return out


def resolve_all():
    """{slug: (Greek title, source)} for every film any source covers."""
    all_films = films()
    hints = greek_hints()
    ids = _load(IMDB_IDS, {})
    imdb = imdb_greek()
    ath = athinorama_matches(all_films=all_films)
    filmy = filmy_matches(all_films)
    out = {}
    for slug, (title, _, _) in all_films.items():
        if slug in ath and slug in filmy and GREEK.search(filmy[slug]) and not GREEK.search(ath[slug]) \
                and url_key(filmy[slug]).startswith(url_key(ath[slug]) + "_"):
            # athinorama sometimes drops the Greek subtitle ("Arrival" vs "Arrival: Η Άφιξη")
            out[slug] = (filmy[slug], "filmy")
        elif slug in ath:
            out[slug] = (ath[slug], "athinorama")
        elif slug in filmy:
            out[slug] = (filmy[slug], "filmy")
        elif ids.get(slug) in imdb:
            out[slug] = (imdb[ids[slug]], "imdb")
        elif hints.get(slug):
            out[slug] = (hints[slug][0], "letterboxd/wikidata")
    return out


def status():
    r = resolve_all()
    by = {}
    for slug, (t, src) in r.items():
        by.setdefault(src, 0)
        by[src] += 1
    print(f"films with a Greek-release title: {len(r):,}")
    for k, v in sorted(by.items(), key=lambda kv: -kv[1]):
        print(f"  {k:20} {v:,}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "imdb":
        refresh_imdb_titles(force="--force" in sys.argv)
        fetch_imdb_ids(list(films()))
    elif cmd == "athinorama":
        n = int(sys.argv[sys.argv.index("--max") + 1]) if "--max" in sys.argv else 300
        fetch_athinorama(n)
    elif cmd == "filmy":
        n = int(sys.argv[sys.argv.index("--max") + 1]) if "--max" in sys.argv else 300
        fetch_filmy(n)
    elif cmd == "show":
        r = resolve_all()
        for s in sys.argv[2:]:
            print(s, r.get(s))
    else:
        status()
