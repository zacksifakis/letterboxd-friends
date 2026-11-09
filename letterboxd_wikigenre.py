"""
The genre as Wikipedia's first sentence puts it — "is a 2026 American
psychological horror film" → "psychological horror" — for the pages' film
details, with a Greek version for the Greek page.

Each film's English Wikipedia article comes from Wikidata (its enwiki
sitelink); the first sentence from Wikipedia's API, 20 articles a request.
Greek versions of the phrases are kept in genre_phrases_el.json: a phrase is
translated once and reused by every film that has it. Films without an
article, or whose phrase has no Greek version yet, keep Letterboxd's genres.

    python3 letterboxd_wikigenre.py fetch            # new films, and recent ones weekly
    python3 letterboxd_wikigenre.py phrases          # distinct phrases still lacking Greek
"""

import os
import re
import sys
import json
import time
import urllib.parse

HOME = os.path.expanduser("~")
sys.path.insert(0, os.path.join(HOME, "Desktop"))
CACHE_DIR = os.path.join(HOME, ".letterboxd_cache")
CACHE = os.path.join(CACHE_DIR, "wikigenre.json")              # slug -> {"title", "phrase", "at"}
PHRASES_EL = os.path.join(CACHE_DIR, "genre_phrases_el.json")  # phrase -> Greek
API = "https://en.wikipedia.org/w/api.php"
UA = "168SeatsPersonalProject/1.0 (personal film list; low request volume)"
RECHECK_DAYS = 7          # recent films' articles change as they're written up
CHUNK = 200               # films between saves


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


# Nationalities and other words that come before the genre but aren't it.
_SKIP = re.compile(r"^(?:[A-Z][a-zé]+(?:[-–][A-Z][a-zé]+)*|independent|feature(?:-length)?|"
                   r"low-budget|big-budget|short|silent|black-and-white|English-language|"
                   r"[A-Z][a-z]+-language|live-action)$")


# Capitalised words that are part of the genre, not a nationality.
_GENRE_CAPS = {"gothic", "western", "christmas", "halloween", "biblical", "victorian", "lovecraftian",
               "shakespearean", "kafkaesque", "bollywood", "noir", "giallo", "wuxia", "kaiju"}


def genre_phrase(sentence):
    """'X is a 2026 American psychological horror film directed by …' → 'psychological horror'."""
    m = re.search(r"\b(?:is|was) an? (?:(?:\d{4}|upcoming|unreleased|forthcoming)\s+)*(.{2,120}?)\s+"
                  r"(?:film|movie|motion picture)\b", sentence)
    if not m:
        return ""
    words = [w for w in re.split(r"\s+", m.group(1).replace(" and ", " and ")) if w]
    while words and words[0].lower() not in _GENRE_CAPS and (_SKIP.match(words[0]) or re.fullmatch(r"\d{4}", words[0])):
        words.pop(0)
    phrase = " ".join(words).strip(" ,")
    if not phrase or len(phrase) > 60 or re.search(r"\b(directed|written|starring|based)\b", phrase):
        return ""
    return phrase.lower()


def _enwiki_titles(qids):
    """{qid: English Wikipedia title, or "" if it has no article}, only for what Wikidata answered; 50 a request."""
    import letterboxd_wikidata as wd
    out = {}
    for i in range(0, len(qids), 50):
        batch = qids[i:i + 50]
        url = wd.API + "?" + urllib.parse.urlencode({
            "action": "wbgetentities", "ids": "|".join(batch), "props": "sitelinks",
            "sitefilter": "enwiki", "format": "json"})
        data = {}
        for attempt in range(4):                 # a busy Wikidata answers with an error, not data
            data = wd.fetch_json(url)
            if "entities" in data:
                break
            time.sleep(10 * (attempt + 1))
        for q, ent in data.get("entities", {}).items():
            out[q] = ent.get("sitelinks", {}).get("enwiki", {}).get("title") or ""
        time.sleep(0.4)
    return out


def _first_sentences(titles):
    """{title: first sentence of the article} from Wikipedia's API, 20 a request."""
    import letterboxd_wikidata as wd
    out = {}
    for i in range(0, len(titles), 20):
        batch = titles[i:i + 20]
        url = API + "?" + urllib.parse.urlencode({
            "action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1, "exsentences": 3,
            "exlimit": 20, "redirects": 1, "titles": "|".join(batch), "format": "json"})
        data = wd.fetch_json(url)
        q = data.get("query", {})
        back = {r["to"]: r["from"] for r in q.get("redirects", []) + q.get("normalized", [])}
        for page in q.get("pages", {}).values():
            t = page.get("title", "")
            out[back.get(t, t)] = (page.get("extract") or "").strip()
        time.sleep(1.0)
    return out


def _due(s, cache, recent, now):
    return s not in cache or (s in recent and now - cache[s].get("at", 0) > RECHECK_DAYS * 86400)


def _linked(part, q_of, cache, now):
    """Phrases for films Wikidata links to an article. A film whose lookup got no answer stays out of the cache."""
    titles = _enwiki_titles(list({q_of[s] for s in part}))
    wanted = sorted({t for t in titles.values() if t})
    sentences = _first_sentences(wanted)
    found = 0
    for s in part:
        if q_of[s] not in titles:
            continue
        t = titles[q_of[s]]
        phrase = genre_phrase(sentences.get(t, "")) if t else ""
        cache[s] = {"title": t, "phrase": phrase, "at": now}
        found += bool(phrase)
    return found


def _by_name(part, films, cache, now):
    """Films Wikidata doesn't link yet (common for new releases): try the usual article
    names, accepting one only if its first sentence says it's a film from that year."""
    names = lambda t, y: ([f"{t} ({y} film)"] if y else []) + [f"{t} (film)", t]
    text = _first_sentences(sorted({g for s in part for g in names(*films[s][:2])}))
    found = 0
    for s in part:
        t, y, _ = films[s]
        cache[s] = {"title": "", "phrase": "", "at": now}
        for g in names(t, y):
            sent = text.get(g, "")
            if y and re.search(rf"\b(?:is|was) an? {y}\b", sent) and genre_phrase(sent):
                cache[s] = {"title": g, "phrase": genre_phrase(sent), "at": now}
                found += 1
                break
    return found


def fetch(slugs=None, log=log, limit=None):
    """
    Phrases for new films, and for this year's and last year's again weekly, at
    most `limit` films a run. Saved every CHUNK films, so being throttled part
    way loses nothing and the next run carries on from there.
    """
    import letterboxd_filmpages as fp
    import letterboxd_greek_titles as gt
    wdc = _load(os.path.join(CACHE_DIR, "wikidata_el.json"), {})
    cache, now = _load(CACHE, {}), time.time()
    recent, films = gt.recent_slugs(), gt.films()
    slugs = slugs or fp.targets()
    q_of = {s: wdc["films"][s]["q"] for s in slugs if (wdc.get("films", {}).get(s) or {}).get("q")}
    linked = [s for s in slugs if s in q_of and _due(s, cache, recent, now)]
    unlinked = [s for s in slugs if s not in q_of and s in films and _due(s, cache, recent, now)]
    if limit is not None:
        linked = linked[:limit]
        unlinked = unlinked[:max(0, limit - len(linked))]
    if not linked and not unlinked:
        return 0
    log(f"wikipedia genres: {len(linked):,} films to look up, {len(unlinked):,} more by title")
    found = done = 0
    try:
        for job, todo in ((lambda p: _linked(p, q_of, cache, now), linked),
                          (lambda p: _by_name(p, films, cache, now), unlinked)):
            for i in range(0, len(todo), CHUNK):
                found += job(todo[i:i + CHUNK])
                done += len(todo[i:i + CHUNK])
                _save(CACHE, cache)
                log(f"wikipedia genres: {done:,} looked up, {found:,} with a genre phrase")
    except RuntimeError as e:
        log(f"wikipedia genres: stopped early, the next run carries on ({e})")
    return found


def phrases(slugs=None):
    """{slug: (English phrase, Greek or None)}."""
    cache, el = _load(CACHE, {}), _load(PHRASES_EL, {})
    return {s: (d["phrase"], el.get(d["phrase"])) for s, d in cache.items()
            if d.get("phrase") and (slugs is None or s in slugs)}


def missing_greek():
    """Distinct phrases with no Greek version yet, most common first."""
    from collections import Counter
    c = Counter(p for p, g in phrases().values() if not g)
    return c.most_common()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "phrases"
    if cmd == "fetch":
        fetch()
    else:
        m = missing_greek()
        print(f"{len(m)} phrases without Greek")
        for p, n in m[:50]:
            print(f"  {n:4}  {p}")
