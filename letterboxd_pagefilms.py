"""
Films on any web page — a cinema's programme, a festival lineup, a "new this
month" article — matched to Letterboxd so the app can rank them by how many of
the people you follow have seen them.

Films are recognised by, in order:
  - links to Letterboxd film pages or to IMDb titles,
  - their English title, or their Greek title (athinorama, filmy.gr, IMDb's
    Greek titles, Letterboxd), as a line of the page,
  - for an English title spelled out in Greek letters ("Κλέι Φέις" →
    Clayface), a sound-alike match against recent films.

When a title fits several films (Οδύσσεια), a year printed next to it wins,
otherwise the most recent film — a programme is nearly always current films.
Pages whose robots.txt asks tools to stay away are not read.
"""

import os
import re
import sys
import time
import html as htmlmod
import difflib
import unicodedata
import urllib.parse
import urllib.robotparser

HOME = os.path.expanduser("~")
sys.path.insert(0, os.path.join(HOME, "Desktop"))

UA = "letterboxd-friends (personal project)"
BLOCK = r"</?(?:p|div|br|li|tr|td|th|h\d|section|article|dd|dt|table|ul|ol|header|footer)\b[^>]*>"
TIME = re.compile(r"\b(?:[01]?\d|2[0-3])[:.]?[0-5]\d\b|\b\d{1,2}(?::\d\d)?\s*[ap]\.?m\b", re.I)
GREEK = re.compile("[Ͱ-Ͽἀ-῿]")


# ------------------------------------------------------------------ reading

class PageError(Exception):
    pass


def allowed(url):
    parts = urllib.parse.urlsplit(url)
    rp = urllib.robotparser.RobotFileParser()
    # Read it ourselves, with our user agent: Python's default one gets refused
    # by some sites, and a refusal would wrongly count as "keep out".
    import requests
    try:
        r = requests.get(f"{parts.scheme}://{parts.netloc}/robots.txt", headers={"User-Agent": UA}, timeout=15)
    except Exception:
        return True                       # no robots.txt to honour
    if r.status_code != 200 or "<html" in r.text[:500].lower():
        return True
    rp.parse(r.text.splitlines())
    return rp.can_fetch(UA, url)


def fetch(url):
    import requests
    if not re.match(r"https?://", url):
        url = "https://" + url
    if not allowed(url):
        raise PageError("that site's robots.txt asks automated tools not to read this page")
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en, el;q=0.8"}, timeout=25)
    except Exception as e:
        raise PageError(f"couldn't load the page ({type(e).__name__})")
    if r.status_code != 200:
        raise PageError(f"the site answered HTTP {r.status_code}")
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return url, r.text


def page_lines(page):
    page = re.sub(r"<(script|style|noscript)\b.*?</\1>", " ", page, flags=re.S | re.I)
    page = re.sub(BLOCK, "\n", page, flags=re.I)
    text = htmlmod.unescape(re.sub(r"<[^>]+>", " ", page))
    return [re.sub(r"\s+", " ", l).strip() for l in text.split("\n") if l.strip()]


# --------------------------------------------------------------- matching

def norm(s):
    s = unicodedata.normalize("NFD", s.lower().replace("&", " and ").replace("ς", "σ"))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(re.findall(r"\w+", s))


_GR = [("ου", "u"), ("ει", "i"), ("οι", "i"), ("υι", "i"), ("αι", "e"), ("αυ", "av"), ("ευ", "ev"),
       ("ντ", "d"), ("μπ", "b"), ("γκ", "g"), ("γγ", "g"), ("τσ", "ts"), ("τζ", "dz")]
_GR1 = dict(zip("αβγδεζηθικλμνξοπρστυφχψω", ["a", "v", "g", "d", "e", "z", "i", "th", "i", "k", "l", "m", "n",
                                            "ks", "o", "p", "r", "s", "t", "i", "f", "h", "ps", "o"]))
_EN = [("ph", "f"), ("th", "t"), ("ck", "k"), ("qu", "kv"), ("q", "k"), ("x", "ks"), ("w", "u"),
       ("ee", "i"), ("ea", "i"), ("oo", "u"), ("ou", "u"), ("ay", "ei"), ("ai", "ei"), ("ey", "ei"),
       ("y", "i"), ("j", "dz")]


def sounds(s, greek):
    """A rough pronunciation key, so 'Κλέι Φέις' and 'Clayface' land close together."""
    if greek:                                    # "Κλέι": é and ι said apart, as in "clay"
        s = re.sub(r"([έάό])ι", lambda m: {"έ": "εj", "ά": "αj", "ό": "οj"}[m.group(1)], s.lower())
    s = norm(s).replace(" ", "")
    if greek:
        for a, b in _GR:
            s = s.replace(a, b)
        s = "".join(_GR1.get(c, c) for c in s)
    else:
        s = s.replace("aw", "o")                # "Paw" as Greek writes it: Πο
        s = re.sub(r"c(?=[eiy])", "s", s)
        s = s.replace("c", "k")
        s = re.sub(r"a([bdfgklmnprstvz])e\b", r"ei\1", s)    # face → feis
        s = re.sub(r"i([bdfgklmnprstvz])e\b", r"ai\1", s)    # time → taim
        for a, b in _EN:
            s = s.replace(a, b)
        s = re.sub(r"e\b|e$", "", s)            # silent final e
        s = s.replace("nt", "d").replace("mp", "b")   # as Greek spells them: ντ, μπ
    s = s.replace("j", "i").replace("z", "s").replace("h", "").replace("v", "b")   # β is b or v
    return re.sub(r"(.)\1+", r"\1", s)


_index = {"at": 0, "data": None}


def index():
    """{normalized title: [slug]} for English and Greek titles, plus lookups. Rebuilt hourly."""
    if _index["data"] and time.time() - _index["at"] < 3600:
        return _index["data"]
    import letterboxd_greek_titles as gt
    films = gt.films()
    by_title, tt2slug = {}, {}
    for slug, (title, year, _) in films.items():
        if norm(title):
            by_title.setdefault(norm(title), []).append(slug)
    for slug, (el, _) in gt.resolve_all().items():
        by_title.setdefault(norm(el), []).append(slug)
    for slug, els in gt.greek_hints().items():
        for el in els:
            by_title.setdefault(norm(el), []).append(slug)
    ids = gt._load(gt.IMDB_IDS, {})
    for slug, tt in ids.items():
        if tt:
            tt2slug[tt] = slug
    for tt, el in gt.imdb_greek().items():
        by_title.setdefault(norm(el), []).append(tt2slug.get(tt, "imdb:" + tt))
    this_year = time.localtime().tm_year
    recent_films = [(slug, t) for slug, (t, y, _) in films.items() if y.isdigit() and int(y) >= this_year - 1]
    recent = [(sounds(t, False), slug) for slug, t in recent_films]
    recent_heads = [(norm(head(t)), sounds(head(t), False), slug) for slug, t in recent_films if norm(head(t))]
    _index.update(at=time.time(), data={"films": films, "by_title": by_title, "tt2slug": tt2slug,
                                        "recent": recent, "recent_heads": recent_heads})
    return _index["data"]


def head(title):
    """The part of a title before '&', ':' or a dash — 'Minions' of 'Minions & Monsters'."""
    return re.split(r"\s*(?:&|:|\s[-–]\s|\band\b|\bκαι\b)\s*", title, maxsplit=1)[0]


def _pick(cands, films, hint_year=None):
    """One film out of several with the same title: a printed year, else the newest."""
    cands = list(dict.fromkeys(cands))
    known = [c for c in cands if c in films]
    if hint_year:
        exact = [c for c in known if films[c][1] == hint_year]
        if exact:
            return exact[0]
    if known:
        return max(known, key=lambda c: (films[c][1] or "0", films[c][2]))
    return cands[0]


LABELS = re.compile(r"^(εισιτηρια|ωρεσ προβολων|προβολεσ|πρεμιερα|προσεχωσ|τρειλερ|trailer|tickets?|showtimes|"
                    r"book now|buy tickets|αγοραστε.*|περισσοτερα|δειτε περισσοτερα|more info|απορρητο|"
                    r"coming soon|now showing|now playing|today|tonight|this week|movies|cinemas|kids.? movies|"
                    r"open.air cinemas|παιζονται τωρα|σημερα|ταινιεσ|κινηματογραφοι|παιδικεσ ταινιεσ)$")


COUNT = re.compile(r"\s*[·|-]?\s*\d+\s+(?:cinemas?|theaters?|theatres?|screens?|venues?|showtimes?|movies?|films?|"
                   r"αίθουσες|αίθουσα|σινεμά|κινηματογράφοι|κινηματογράφους|προβολές|ταινίες)\s*$", re.I)


def is_label(line):
    """Buttons, headings and footers that happen to share a film's title."""
    l = norm(COUNT.sub("", line))
    return bool(LABELS.match(l)) or bool(re.search(r"©|cookie", line, re.I)) or \
        bool(re.search(r"αιθουσα\s*(\d|[α-ω]\b)", l))


def _recent(slug, films, years=2):
    y = films.get(slug, ("", "", 0))[1]
    return y.isdigit() and int(y) >= time.localtime().tm_year - years


def match_line(line, ix, listing=False):
    """
    (slug or 'imdb:tt…', how) for a line that is a film's title, else None.
    `listing` says the line is followed by showtimes, as in a programme: then
    one-word and sound-alike matches are trusted; elsewhere a one-word line
    ("Thursday", "Other") only counts for a recent film.
    """
    if not 2 <= len(line) <= 90 or len(line.split()) > 14 or line.startswith("(") or is_label(line):
        return None
    year = re.search(r"\((\d{4})\)|\b(19\d\d|20\d\d)\b", line)
    hint = (year.group(1) or year.group(2)) if year else None
    line = COUNT.sub("", line).strip()             # "Clayface 52 cinemas" → "Clayface"
    bare = re.sub(r"\(.*?\)|\[.*?\]|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b(19|20)\d\d\b", " ", line).strip(" -–:·|")
    for text in (line, bare):
        key = norm(text)
        hit = ix["by_title"].get(key) if key else None
        if hit:
            slug = _pick(hit, ix["films"], hint)
            if len(key.split()) == 1 and not listing and not (hint or _recent(slug, ix["films"])):
                return None
            if len(key.split()) == 1 and slug.startswith("imdb:"):
                return None
            return slug, "greek title" if GREEK.search(text) else "title"
    # Half-translated titles: "Minions & Τέρατα", "Paw Patrol: Κουτάβια και Δεινόσαυροι",
    # or the same spelled in Greek ("Μίνιον & Τέρατα"). The first part names the
    # film; it's matched against the first part of recent films' titles.
    first = head(bare)
    if first != bare and len(norm(first)) >= 5:
        f = norm(first)
        exact = [sl for h, _, sl in ix["recent_heads"] if h == f]
        if exact and not GREEK.search(first):
            return _pick(exact, ix["films"], hint), "title (first part)"
        if listing and GREEK.search(first):
            key = sounds(first, True)
            best = max(((difflib.SequenceMatcher(None, key, k).ratio(), sl) for _, k, sl in ix["recent_heads"]
                        if abs(len(k) - len(key)) <= 2), default=(0, None))
            if best[0] >= 0.88:
                return best[1], "spelled in Greek"
    if listing and GREEK.search(bare) and len(norm(bare)) >= 4:
        key = sounds(bare, True)
        best = max(((difflib.SequenceMatcher(None, key, k).ratio(), s) for k, s in ix["recent"]
                    if abs(len(k) - len(key)) <= 3), default=(0, None))
        need = 1.0 if len(key) < 5 else 0.88
        if best[0] >= need:
            return best[1], "spelled in Greek"
    return None


def athinorama_cards(page):
    """athinorama.gr's TV and cinema listings: each film's Greek title, original
    title, year, and channel and time (or venue), straight from its card."""
    out, seen = [], set()
    for m in re.finditer(r'<a href="(/(?:tv|cinema)/movie/[^"]+)"[^>]*>([^<]{2,120})</a>', page):
        href, greek = m.group(1), htmlmod.unescape(m.group(2)).strip()
        if href in seen:
            continue
        seen.add(href)
        win = page[m.end():m.end() + 1500]
        nxt = re.search(r'<a href="/(?:tv|cinema)/movie/', win)
        win = win[:nxt.start()] if nxt else win            # stop at the next film's card
        orig = re.search(r'original-title[^>]*>\s*<span>(.*?)</span>', win, re.S)
        when = re.search(r'<span class="channel">(.*?)</span>\s*<span class="time">(.*?)</span>', win, re.S)
        details = re.sub(r"<p\b.*?</p>", " ", win, flags=re.S)          # not the plot ("…του έτους 2035")
        year = re.search(r"\b(19[1-9]\d|20[0-3]\d)\b", re.sub(r"<[^>]+>", " ", details))
        if year and int(year.group(1)) > time.localtime().tm_year + 1:
            year = None
        out.append({"href": href, "greek": greek, "original": htmlmod.unescape(orig.group(1)).strip() if orig else "",
                    "year": year.group(1) if year else None,
                    "when": " ".join(htmlmod.unescape(x).strip() for x in when.groups()) if when else ""})
    return out


def match_card(c, ix):
    """(slug, how) for one athinorama card: titles and year together, else Letterboxd's search."""
    films, by = ix["films"], ix["by_title"]
    greek_hits = set(by.get(norm(c["greek"]), [])) if c["greek"] else set()
    orig_hits = set(by.get(norm(c["original"]), [])) if c["original"] and c["original"] != c["greek"] else set()
    both = greek_hits & orig_hits
    for pool, how in ((both, "both titles"), (orig_hits, "original title"), (greek_hits, "greek title")):
        known = [x for x in pool if x in films]
        if c["year"]:
            known = [x for x in known if films[x][1] and abs(int(films[x][1]) - int(c["year"])) <= 1]
        if known:                               # several: the most watched (TV shows old films too)
            return max(known, key=lambda x: films[x][2]), how
    if not c["year"] and c.get("href"):          # the film's own page has the year
        try:
            _, page = fetch("https://www.athinorama.gr" + c["href"])
            y = re.search(r'class="year">\s*(\d{4})', page)
            c["year"] = y.group(1) if y else None
        except Exception:
            pass
    for q in filter(None, (c["original"], c["greek"])):
        hit = letterboxd_search(q, c["year"], any_year=not c["year"])
        if hit:
            SEARCHED[hit[0]] = hit
            return hit[0], "Letterboxd search"
    return None


def films_on(url, limit=150, cross_check=True):
    """{"url", "title", "films": [...], "unmatched": [...]} for a web page."""
    url, page = fetch(url)
    ix = index()
    found, order = {}, []
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    title = htmlmod.unescape(title.group(1).strip()) if title else url

    if "athinorama.gr" in urllib.parse.urlsplit(url).netloc:
        cards = athinorama_cards(page)
        if cards:
            films, unmatched = [], []
            for c in cards:
                hit = match_card(c, ix)
                if hit and hit[0] not in found:
                    found[hit[0]] = {"key": hit[0], "how": hit[1], "times": [c["when"]] if c["when"] else [],
                                     "text": c["greek"] + (f" ({c['original']})" if c["original"] and c["original"] != c["greek"] else "")}
                    films.append(found[hit[0]])
                elif not hit:
                    unmatched.append(c["greek"])
            return {"url": url, "title": title, "films": films, "links": {}, "unmatched": unmatched,
                    "checkedWith": None}

    def add(key, how, text, times=None):
        if key not in found:
            found[key] = {"key": key, "how": how, "text": text, "times": []}
            order.append(key)
        if times:
            found[key]["times"] = list(dict.fromkeys(found[key]["times"] + times))[:6]

    for slug in re.findall(r"letterboxd\.com/film/([a-z0-9-]+)/", page):
        add(slug, "Letterboxd link", "")
    for tt in re.findall(r"imdb\.com/title/(tt\d+)", page):
        add(ix["tt2slug"].get(tt, "imdb:" + tt), "IMDb link", "")

    lines = page_lines(page)
    unmatched = []
    page_title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    site_words = set(norm(htmlmod.unescape(page_title.group(1))).split()) if page_title else set()
    for i, line in enumerate(lines):
        # notes in brackets ("(Ντοκιμαντέρ)", "(μτγλτ)") can sit between a title and its times
        nxt = [l for l in lines[i + 1:i + 6] if not (l.startswith("(") and l.endswith(")"))]
        after = [l for l in nxt[:3] if TIME.search(l) and "©" not in l and not match_line(l, ix)][:3]
        listing = bool(after) and bool(nxt) and nxt[0] in after
        if any(upcoming(l) for l in [line] + after):
            continue                            # "coming soon": not playing yet, so left out
        hit = match_line(line, ix, listing)
        if hit:
            add(hit[0], hit[1], COUNT.sub("", line).strip(), after)
        elif listing and not TIME.search(line) and not re.search(r"\d{3,4}\b", line) and 3 <= len(line) <= 60 \
                and len(line.split()) <= 7 and not line.endswith(".") and not line.startswith("(") \
                and not re.search(r"www\.|https?:|cinema|\bpage\b", line, re.I) and not is_label(line) \
                and not set(norm(line).split()) <= site_words:
            unmatched.append(line)              # looks like a programme entry we couldn't place
        if len(order) >= limit:
            break

    # Titles that link to the cinema's own page for the film: that page often
    # names the director, whose Letterboxd page settles which film it is.
    site = urllib.parse.urlsplit(url).netloc
    links = {}
    for href, text in re.findall(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', page, re.S | re.I):
        text = re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", text))).strip()
        href = urllib.parse.urljoin(url, href)
        if text and urllib.parse.urlsplit(href).netloc == site and \
                re.search(r"/(movies?|films?|tainies|tainia|ταινίες)/[^/]+", href, re.I):
            links.setdefault(text, href)
    # A title that matched an old film of the same name ("The Suitcase", 2008)
    # is checked against the film's own page: a different year means it's
    # another film, found through its director.
    by_name = {undated(COUNT.sub("", t)): h for t, h in links.items()}
    for key in list(order):
        f = found[key]
        href = by_name.get(f["text"])
        if not href or key not in ix["films"] or _recent(key, ix["films"], 2):
            continue
        _, year = film_page(href)
        if year and abs(int(year) - int(ix["films"][key][1] or 0)) > 1:
            found_it = letterboxd_search(f["text"], year)
            better = found_it[0] if found_it else by_director(href, ix)
            if found_it:
                SEARCHED[better] = found_it
            if better and better not in found:
                found[better] = dict(f, key=better, how=f"newer film ({year})")
                order[order.index(key)] = better
                del found[key]

    # The film page's own address often is the film's name: /movies/clayface.
    for text, href in list(links.items()):
        name = undated(COUNT.sub("", text))
        if name in {undated(found[k]["text"]) for k in order} or upcoming(text) or is_label(name):
            continue
        slug = by_address(href, ix)
        if slug:
            add(slug, "page address", name)
    unmatched = [u for u in unmatched if undated(COUNT.sub("", u)) not in {undated(found[k]["text"]) for k in order}]
    placed = {undated(found[k]["text"]) for k in order}
    tried, done = 0, set()
    for text, href in links.items():
        name = undated(COUNT.sub("", text))
        if name in placed or name in done or tried >= 20 or not 2 <= len(name) <= 80 or upcoming(text) \
                or is_label(name) or set(norm(name).split()) <= site_words or match_line(name, ix, True):
            continue
        tried += 1
        done.add(name)
        _, year = film_page(href)
        found_it = letterboxd_search(name, year)
        hit = found_it[0] if found_it else by_director(href, ix)
        if hit:
            add(hit, "Letterboxd search" if found_it else "director", name)
            placed.add(name)
            if found_it:
                SEARCHED[hit] = found_it
        else:
            unmatched.append(name)
    unmatched = [u for u in dict.fromkeys(undated(u) for u in unmatched) if u not in placed]
    still = []
    for u in unmatched[:25]:
        found_it = letterboxd_search(u)
        if found_it and found_it[0] not in found:
            add(found_it[0], "Letterboxd search", u)
            SEARCHED[found_it[0]] = found_it
        else:
            still.append(u)
    unmatched = still + unmatched[25:]

    if cross_check:
        helper = CROSS_CHECK.get(site.replace("www.", ""))
        if helper:
            unmatched = cross_checked(found, order, unmatched, helper, ix)

    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    return {"url": url, "title": htmlmod.unescape(title.group(1).strip()) if title else url,
            "films": [found[k] for k in order], "links": {undated(t): h for t, h in links.items()},
            "unmatched": list(dict.fromkeys(unmatched))[:30],
            "checkedWith": CROSS_CHECK.get(site.replace("www.", "")) if cross_check else None}


_searches = {}
SEARCHED = {}          # slug -> (slug, title, year) for films found by search, for display


def letterboxd_search(title, year=None, any_year=False):
    """
    A film by title through Letterboxd's own search box: (slug, title, year) for
    a result whose title or original title is this one — from the given year if
    known, else the newest (a cinema shows current films). None if unsure.
    """
    q = re.sub(r"\(.*?\)", " ", undated(title))
    q = re.sub(r"\s+", " ", re.sub(r"[.…,;!?]+", " ", q)).strip()      # "Μια Τρελή... Τρελή..." spoils the search
    if len(q) < 3:
        return None
    key = (norm(q), year, any_year)
    if key in _searches:
        return _searches[key]
    hit = None
    try:
        import letterboxd_net as net
        r = net.session().get("https://letterboxd.com/s/autocompletefilm",
                              params={"q": q, "limit": 10}, timeout=20)
        results = r.json().get("data", []) if r.status_code == 200 else []
        loose = lambda t: re.sub(r"(.)\1+", r"\1", norm(t))         # old spellings: Τρελλή = Τρελή
        same = [d for d in results if loose(q) in (loose(d.get("name") or ""), loose(d.get("originalName") or ""))]
        if year:
            same = [d for d in same if d.get("releaseYear") and abs(int(d["releaseYear"]) - int(year)) <= 1]
        elif any_year:
            same = same[:1]                     # Letterboxd's own best match
        else:
            this_year = time.localtime().tm_year
            same = [d for d in same if d.get("releaseYear") and int(d["releaseYear"]) >= this_year - 1]
        if same:
            d = max(same, key=lambda d: d.get("releaseYear") or 0)
            m = re.search(r"/film/([a-z0-9-]+)/", d.get("url") or "")
            if m:
                hit = (m.group(1), d.get("name"), str(d.get("releaseYear") or ""))
    except Exception:
        hit = None
    _searches[key] = hit
    time.sleep(0.7)
    return hit


def by_address(href, ix):
    """A Letterboxd film named by the last part of a film page's address, if it's a recent film."""
    last = urllib.parse.urlsplit(href).path.rstrip("/").rsplit("/", 1)[-1].lower()
    last = re.sub(r"[^a-z0-9-]", "", re.sub(r"^\d+-", "", last))
    if len(last) < 3:
        return None
    year = time.localtime().tm_year
    for cand in (last, f"{last}-{year}", f"{last}-{year - 1}"):
        if cand in ix["films"] and _recent(cand, ix["films"], 2):
            return cand
    return None


DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


def undated(text):
    return re.sub(r"\s+", " ", DATE.sub(" ", text)).strip()


def upcoming(text):
    """True when the text carries a date that's still ahead: a "coming soon" premiere."""
    import datetime
    for d, m, y in DATE.findall(text):
        try:
            if datetime.date(int(y), int(m), int(d)) > datetime.date.today():
                return True
        except ValueError:
            pass
    return False


def latin_name(greek_name):
    """A person's name as spelled in English, from Wikidata, given its Greek spelling."""
    import requests
    try:
        r = requests.get("https://www.wikidata.org/w/api.php", timeout=20,
                         headers={"User-Agent": "168SeatsPersonalProject/1.0 (personal film list)"},
                         params={"action": "wbsearchentities", "search": greek_name, "language": "el",
                                 "uselang": "en", "type": "item", "limit": 5, "format": "json"})
        for hit in r.json().get("search", []):
            desc = (hit.get("description") or "").lower()
            if any(w in desc for w in ("director", "filmmaker", "screenwriter", "actor", "actress")):
                return hit.get("label")
    except Exception:
        pass
    return None


_pages = {}


def film_page(film_url):
    """(director, year) a cinema's page for one film gives — from its schema.org
    Movie data when it has it, else from a "Director:" line. Cached per page."""
    if film_url in _pages:
        return _pages[film_url]
    out = (None, None)
    try:
        if allowed(film_url):
            _, page = fetch(film_url)
            out = _director_and_year(page)
    except Exception:
        pass
    _pages[film_url] = out
    return out


def _director_and_year(page):
    import json
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(block)
        except Exception:
            continue
        for g in (data.get("@graph") or [data]) if isinstance(data, dict) else data:
            if isinstance(g, dict) and g.get("@type") == "Movie":
                d = g.get("director")
                d = d[0] if isinstance(d, list) and d else d
                name = d.get("name") if isinstance(d, dict) else (d if isinstance(d, str) else None)
                date = str(g.get("datePublished") or g.get("dateCreated") or g.get("copyrightYear") or "")
                if name:
                    return name, (date[:4] if re.match(r"\d{4}", date) else None)
    text = " ".join(page_lines(page))
    m = re.search(r"(?:Σκηνοθεσία|Directed by|Director|Regie|Réalisation)\s*:?\s*([A-Za-zÀ-ÿͰ-Ͽἀ-῿.'’ -]{4,60}?)"
                  r"(?=\s+(?:Ηθοποιοί|Cast|Starring|Πρωταγωνιστούν|Σενάριο|Writer)|,|\s{2}|$)", text)
    return (m.group(1).strip(), None) if m else (None, None)


def by_director(film_url, ix):
    """
    The film a cinema's film page is about, from the director (and year) it
    gives: that director's film from that year on Letterboxd, else their newest.
    A Greek spelling of a foreign name is turned into English via Wikidata.
    """
    name, year = film_page(film_url)
    if not name:
        return None
    if GREEK.search(name):
        name = latin_name(name)            # "Ζαν-Πολ Σαλομέ" → "Jean-Paul Salomé"
        if not name:
            return None
    slug = "-".join(re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", name.lower())
                                              .encode("ascii", "ignore").decode()))
    if not slug:
        return None
    try:
        import letterboxd_net as net
        r = net.session().get(f"https://letterboxd.com/director/{slug}/by/release/", timeout=20)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    films = list(dict.fromkeys(re.findall(r'data-film-slug="([^"]+)"', r.text)
                               or re.findall(r'/film/([a-z0-9-]+)/', r.text)))
    if year:                                    # the director's film from that year
        dated = [f for f in films if ix["films"].get(f, ("", ""))[1] in (year, str(int(year) - 1))
                 or f.endswith("-" + year)]
        if dated:
            return dated[0]
    known = [f for f in films if _recent(f, ix["films"], 1)]
    if known:
        return max(known, key=lambda f: ix["films"][f][1])
    return films[0] if films else None          # newest first: what's playing is the latest film


# ------------------------------------------------------------ cross-checking

# A listing whose titles are checked against a second site for the same town:
# cinema-her.com (every Heraklion cinema, Greek titles only) against the
# Texnopolis chain's own pages (English titles and directors for many films).
CROSS_CHECK = {"cinema-her.com": "https://www.texnopolis.net/movies/"}
STRONG = {"title", "title (first part)", "director", "Letterboxd link", "IMDb link"}
_helpers = {}


def clean_title(text):
    """'Minions & Τέρατα (Μεταγλωττισμένο) 29/10/2026' → 'minions τερατα', for comparing sites."""
    text = re.sub(r"\(.*?\)|\|.*$", " ", undated(text))
    return norm(text)


def helper_map(url, ix):
    """{clean title: {"slug", "how", "href"}} from the second site, kept for six hours."""
    hit = _helpers.get(url)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    res = films_on(url, cross_check=False)
    out = {}
    for f in res["films"]:
        k = clean_title(f["text"])
        if k:
            out[k] = {"slug": f["key"], "how": f["how"], "href": None}
    for text, href in res["links"].items():
        k = clean_title(text)
        if k:
            out.setdefault(k, {"slug": None, "how": None, "href": None})["href"] = href
    _helpers[url] = (time.time(), out)
    return out


def _lookup(helper, text):
    k = clean_title(text)
    if k in helper:
        return helper[k]
    close = difflib.get_close_matches(k, list(helper), n=1, cutoff=0.88)   # Θησαυροί / Θυσαυροί
    if close:
        return helper[close[0]]
    if GREEK.search(text):          # "Δε Ινφλουένσερ Πρότζεκτ" on one site, "The Influencer Project" on the other
        key = sounds(re.sub(r"\(.*?\)", " ", undated(text)), True)
        best = max(((difflib.SequenceMatcher(None, key, sounds(h, False)).ratio(), h) for h in helper
                    if not GREEK.search(h) and len(h) >= 4), default=(0, None))
        if best[0] >= 0.85:
            return helper[best[1]]
    return None


def cross_checked(found, order, unmatched, helper_url, ix):
    """Confirm, correct or fill in the page's matches from the second site."""
    try:
        helper = helper_map(helper_url, ix)
    except Exception:
        return unmatched
    site = urllib.parse.urlsplit(helper_url).netloc.replace("www.", "")
    on_helper = {h["slug"] for h in helper.values() if h["slug"]}
    for i, key in enumerate(list(order)):
        f = found[key]
        h = _lookup(helper, f["text"]) if f["text"] else None
        old_namesake = key in ix["films"] and not _recent(key, ix["films"], 2)
        if not old_namesake and (key in on_helper or (h and h["slug"] == key)):
            f["checked"] = site                        # both sites point to the same film
            continue
        if not h:
            continue
        better = h["slug"] if h["slug"] and h["how"] in STRONG and h["slug"] != key else None
        if not better and h["href"] and old_namesake:
            better = by_director(h["href"], ix)        # an old namesake: ask the film's own page
        if better and better != key and better not in found:
            found[better] = dict(f, key=better, how=f"corrected by {site}", checked=site)
            order[order.index(key)] = better
            del found[key]
    still = []
    for text in unmatched:
        h = _lookup(helper, text)
        slug = h["slug"] if h and h["slug"] else (by_director(h["href"], ix) if h and h["href"] else None)
        if slug and slug not in found:
            found[slug] = {"key": slug, "how": f"found via {site}", "text": text, "times": [], "checked": site}
            order.append(slug)
        elif not slug:
            still.append(text)
    return still


def resolve_imdb(tt):
    """Letterboxd's page for an IMDb title we don't have: (slug, title, year) or None."""
    try:
        import letterboxd_net as net
        r = net.session().get(f"https://letterboxd.com/imdb/{tt}/", timeout=20)
        m = re.search(r"letterboxd\.com/film/([a-z0-9-]+)/", r.url)
        if r.status_code != 200 or not m:
            return None
        t = re.search(r'<meta property="og:title" content="([^"]+)"', r.text)
        title = htmlmod.unescape(t.group(1)) if t else m.group(1)
        y = re.search(r"\((\d{4})\)\s*$", title)
        return m.group(1), re.sub(r"\s*\(\d{4}\)\s*$", "", title), y.group(1) if y else ""
    except Exception:
        return None
