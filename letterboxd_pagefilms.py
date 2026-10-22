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
    rp.set_url(f"{parts.scheme}://{parts.netloc}/robots.txt")
    try:
        rp.read()
    except Exception:
        return True                       # no robots.txt to honour
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
    s = norm(s).replace(" ", "")
    if greek:
        for a, b in _GR:
            s = s.replace(a, b)
        s = "".join(_GR1.get(c, c) for c in s)
    else:
        s = re.sub(r"c(?=[eiy])", "s", s)
        s = s.replace("c", "k")
        s = re.sub(r"a([bdfgklmnprstvz])e\b", r"ei\1", s)    # face → feis
        s = re.sub(r"i([bdfgklmnprstvz])e\b", r"ai\1", s)    # time → taim
        for a, b in _EN:
            s = s.replace(a, b)
        s = re.sub(r"e\b|e$", "", s)            # silent final e
    s = s.replace("z", "s").replace("h", "")
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
    recent = [(sounds(t, False), slug) for slug, (t, y, _) in films.items()
              if y.isdigit() and int(y) >= this_year - 1]
    _index.update(at=time.time(), data={"films": films, "by_title": by_title,
                                        "tt2slug": tt2slug, "recent": recent})
    return _index["data"]


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
    if not 2 <= len(line) <= 90 or len(line.split()) > 14 or line.startswith("("):
        return None
    year = re.search(r"\((\d{4})\)|\b(19\d\d|20\d\d)\b", line)
    hint = (year.group(1) or year.group(2)) if year else None
    bare = re.sub(r"\(.*?\)|\[.*?\]|\b(19|20)\d\d\b", " ", line).strip(" -–:·|")
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
    if listing and GREEK.search(bare) and len(norm(bare)) >= 4:
        key = sounds(bare, True)
        best = max(((difflib.SequenceMatcher(None, key, k).ratio(), s) for k, s in ix["recent"]
                    if abs(len(k) - len(key)) <= 3), default=(0, None))
        need = 1.0 if len(key) < 5 else 0.8
        if best[0] >= need:
            return best[1], "spelled in Greek"
    return None


def films_on(url, limit=150):
    """{"url", "title", "films": [...], "unmatched": [...]} for a web page."""
    url, page = fetch(url)
    ix = index()
    found, order = {}, []

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
    for i, line in enumerate(lines):
        # notes in brackets ("(Ντοκιμαντέρ)", "(μτγλτ)") can sit between a title and its times
        nxt = [l for l in lines[i + 1:i + 6] if not (l.startswith("(") and l.endswith(")"))]
        after = [l for l in nxt[:3] if TIME.search(l) and not match_line(l, ix)][:3]
        listing = bool(after) and bool(nxt) and nxt[0] in after
        hit = match_line(line, ix, listing)
        if hit:
            add(hit[0], hit[1], line, after)
        elif listing and not TIME.search(line) and not re.search(r"\d{3,4}\b", line) and 3 <= len(line) <= 60 \
                and len(line.split()) <= 7 and not line.endswith(".") and not line.startswith("(") \
                and not re.search(r"www\.|https?:|cinema|\bpage\b", line, re.I):
            unmatched.append(line)              # looks like a programme entry we couldn't place
        if len(order) >= limit:
            break

    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    return {"url": url, "title": htmlmod.unescape(title.group(1).strip()) if title else url,
            "films": [found[k] for k in order],
            "unmatched": list(dict.fromkeys(unmatched))[:30]}


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
