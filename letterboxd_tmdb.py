"""
Greek plot summaries from TMDB for the Greek page ("168 Θέσεις").

Needs your own free TMDB API key, saved (by you) in ~/.letterboxd_cache/tmdb_key.
Either the short "API Key" or the long "API Read Access Token" works.

Films covered: every film that gets a poster (the top of each list) plus every
film with Wikidata details. TMDB's own Greek summary comes first, then a short
summary Claude wrote in its own words (~/.letterboxd_cache/plots_el_claude.json),
then TMDB's English summary as is. TMDB's text is never translated: a
translation would still be TMDB's text. The page labels which is which.

    python3 letterboxd_tmdb.py fetch            # fetch whatever is missing
    python3 letterboxd_tmdb.py fetch --max 200  # stop after 200
    python3 letterboxd_tmdb.py status
"""

import os
import re
import sys
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime

import letterboxd_net as net

HOME = os.path.expanduser("~")
CACHE_DIR = os.path.join(HOME, ".letterboxd_cache")
KEY_FILE = os.path.join(CACHE_DIR, "tmdb_key")
CACHE = os.path.join(CACHE_DIR, "tmdb_el.json")
WRITTEN = os.path.join(CACHE_DIR, "plots_el_claude.json")          # {slug: Greek summary written by Claude}
WIKIDATA = os.path.join(CACHE_DIR, "wikidata_el.json")
UA = "168SeatsPersonalProject/1.0 (personal film list; low request volume)"
RECHECK_EMPTY_DAYS = 30        # Greek summaries get added over time
RECHECK_FOUND_DAYS = 180


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save(obj):
    tmp = CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, CACHE)


def key():
    try:
        with open(KEY_FILE) as f:
            k = f.read().strip()
        return k or None
    except OSError:
        return None


def targets():
    """Slugs worth a summary, most useful first."""
    sys.path.insert(0, os.path.join(HOME, "Desktop"))
    import letterboxd_posters as lp
    out, seen = [], set()
    for s in lp.wanted():
        if s not in seen:
            seen.add(s)
            out.append(s)
    for s, d in (_load(WIKIDATA, {}).get("detail") or {}).items():
        if d and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _tmdb_ids_from_wikidata(slugs):
    """{slug: ("movie"|"tv", id)} via Wikidata's Letterboxd ID (P6127) -> TMDB IDs."""
    found = {}
    for i in range(0, len(slugs), 250):
        chunk = slugs[i:i + 250]
        values = " ".join(json.dumps(s) for s in chunk)
        q = f"""SELECT ?lb ?m ?t WHERE {{ VALUES ?lb {{ {values} }} ?item wdt:P6127 ?lb .
                OPTIONAL {{ ?item wdt:P4947 ?m }} OPTIONAL {{ ?item wdt:P4983 ?t }} }}"""
        body = urllib.parse.urlencode({"query": q}).encode()
        for attempt in range(4):
            try:
                req = urllib.request.Request("https://query.wikidata.org/sparql", data=body,
                                             headers={"User-Agent": UA,
                                                      "Accept": "application/sparql-results+json"})
                rows = json.load(urllib.request.urlopen(req, timeout=60))["results"]["bindings"]
                for r in rows:
                    lb = r["lb"]["value"]
                    if "m" in r and r["m"]["value"].isdigit():
                        found.setdefault(lb, ("movie", int(r["m"]["value"])))
                    elif "t" in r and r["t"]["value"].isdigit():
                        found.setdefault(lb, ("tv", int(r["t"]["value"])))
                break
            except Exception:
                time.sleep(10 * (attempt + 1))
        time.sleep(1.5)
    return found


def _tmdb_id_from_letterboxd(session, slug):
    try:
        r = session.get(f"https://letterboxd.com/film/{slug}/", timeout=20)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    m = re.search(r"themoviedb\.org/(movie|tv)/([1-9]\d*)", r.text)
    return (m.group(1), int(m.group(2))) if m else None


def _tmdb_get(kind, tid, k):
    params = {"language": "el-GR", "append_to_response": "translations"}
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if len(k) > 40:
        headers["Authorization"] = f"Bearer {k}"      # read access token
    else:
        params["api_key"] = k                          # v3 API key
    url = f"https://api.themoviedb.org/3/{kind}/{tid}?" + urllib.parse.urlencode(params)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise SystemExit("TMDB refused the key (401). Check ~/.letterboxd_cache/tmdb_key.")
            if e.code == 404:
                return {}
            time.sleep(2 * (attempt + 1))
        except Exception:
            time.sleep(2 * (attempt + 1))
    return None


def _clean(text):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text


def fetch(max_n=None, log=print):
    k = key()
    if not k:
        log("greek summaries: no TMDB key saved yet — skipping")
        return 0
    cache = _load(CACHE, {})
    now = time.time()

    def due(s):
        e = cache.get(s)
        if not e:
            return True
        age = now - e.get("at", 0)
        if "en" not in e and e.get("tmdb"):
            return True                                   # checked before English was kept
        return age > (RECHECK_FOUND_DAYS if e.get("plot") else RECHECK_EMPTY_DAYS) * 86400

    todo = [s for s in targets() if due(s)]
    if max_n:
        todo = todo[:max_n]
    log(f"greek summaries: {len(todo)} to check")
    if not todo:
        return 0

    need_id = [s for s in todo if not (cache.get(s) or {}).get("tmdb")]
    ids = _tmdb_ids_from_wikidata(need_id) if need_id else {}

    session = None
    got = empty = failed = 0
    for n, slug in enumerate(todo, 1):
        e = cache.get(slug) or {}
        ref = tuple(e["tmdb"]) if e.get("tmdb") else ids.get(slug)
        if not ref:
            if session is None:
                session = net.session()
            ref = _tmdb_id_from_letterboxd(session, slug)
            time.sleep(1.0)
        if not ref:
            cache[slug] = {"plot": "", "at": now}           # nothing on TMDB to ask about
            empty += 1
        else:
            data = _tmdb_get(ref[0], ref[1], k)
            if data is None:
                failed += 1
                continue                                      # try again next run
            el = _clean(data.get("overview"))
            en = ""
            for t in ((data.get("translations") or {}).get("translations") or []):
                if t.get("iso_639_1") == "en":
                    en = _clean((t.get("data") or {}).get("overview")) or en
                    if t.get("iso_3166_1") == "US":
                        break
            cache[slug] = {"tmdb": list(ref), "plot": el, "en": en, "at": now}
            got += bool(el)
            empty += not el
            time.sleep(0.25)
        if n % 100 == 0:
            _save(cache)
            log(f"greek summaries: {n}/{len(todo)}  (found {got}, none in Greek {empty}, failed {failed})")
    _save(cache)
    log(f"greek summaries: done — found {got}, none in Greek {empty}, failed {failed}")
    return got


def plots():
    """
    {slug: (text, kind)} for every film with a summary, best first:
      "el"  TMDB's own Greek summary (only with a TMDB key)
      "cl"  a short Greek summary written by Claude in its own words
      "en"  TMDB's English summary, as is
    """
    out = {}
    for s, e in _load(CACHE, {}).items():
        if e.get("en"):
            out[s] = (e["en"], "en")
    out.update({s: (t, "cl") for s, t in _load(WRITTEN, {}).items() if t})
    for s, e in _load(CACHE, {}).items():
        if e.get("plot"):
            out[s] = (e["plot"], "el")
    return out


def status():
    cache = _load(CACHE, {})
    t = targets()
    have = sum(1 for s in t if s in cache)
    p = plots()
    kinds = [p[s][1] for s in t if s in p]
    print(f"TMDB key saved        : {'yes' if key() else 'no'}")
    print(f"films to cover        : {len(t):,}")
    print(f"  checked             : {have:,}")
    print(f"  Greek from TMDB     : {kinds.count('el'):,}")
    print(f"  written by Claude   : {kinds.count('cl'):,}")
    print(f"  English only so far : {kinds.count('en'):,}")
    print(f"  no summary at all   : {have - len(kinds):,}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "fetch":
        fetch(int(sys.argv[sys.argv.index("--max") + 1]) if "--max" in sys.argv else None)
    else:
        status()
