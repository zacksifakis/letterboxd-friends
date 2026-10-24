"""
Fetch Greek-language film data from Wikidata (public domain, CC0) for the films
in the scraped lists: Greek titles for every film Wikidata knows, plus director,
top-billed cast, genres, running time and Rotten Tomatoes / Greek Wikipedia
links for the most-watched films of each list.

    python3 letterboxd_wikidata.py                 # default: details for top 150 per list
    python3 letterboxd_wikidata.py --detail-top 300

Results are cached in ~/.letterboxd_cache/wikidata_el.json, so re-runs only
fetch films that are new since last time. Nothing about you or your friends is
sent — only public film identifiers.
"""

import os
import re
import csv
import sys
import json
import glob
import time
import urllib.error
import urllib.parse
import urllib.request

HOME = os.path.expanduser("~")
CACHE = os.path.join(HOME, ".letterboxd_cache", "wikidata_el.json")
UA = "168SeatsPersonalProject/1.0 (personal film list; low request volume)"
SPARQL = "https://query.wikidata.org/sparql"
API = "https://www.wikidata.org/w/api.php"


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def fetch_json(url, body=None, tries=6):
    headers = {"User-Agent": UA, "Accept": "application/sparql-results+json, application/json"}
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                wait = int(e.headers.get("Retry-After") or 0) or 10 * (attempt + 1)
                log(f"  HTTP {e.code}, waiting {wait}s")
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            log(f"  {type(e).__name__}: {e} — retrying")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("Wikidata kept failing; run again later to resume from the cache")


def load_cache():
    if os.path.exists(CACHE):
        with open(CACHE) as f:
            return json.load(f)
    return {"films": {}, "detail": {}, "labels": {}}


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, CACHE)


def scraped_lists():
    """{period: [(slug, count), ...]} sorted by count, from the scraper's CSVs."""
    lists = {}
    for path in glob.glob(f"{HOME}/friends_watched_*.csv"):
        m = re.fullmatch(r"friends_watched_([0-9]{4}s?)\.csv", os.path.basename(path))
        if not m:
            continue
        with open(path, newline="") as f:
            rows = [(r["Slug"], int(r.get("Friend Watch Count") or 0)) for r in csv.DictReader(f) if r.get("Slug")]
        lists[m.group(1)] = sorted(rows, key=lambda x: -x[1])
    return lists


# ------------------------------------------------------------ phase 1: titles

def _recent():
    try:
        import letterboxd_greek_titles as gt
        return gt.recent_slugs(), gt.RECENT_RECHECK_DAYS * 86400
    except Exception:
        return set(), 7 * 86400


def fetch_titles(slugs, cache):
    """Letterboxd slug -> Wikidata item + Greek label, for every film. New films
    are often added to Wikidata, or given a Greek label, months after release,
    so this year's and last year's are looked up again weekly."""
    recent, every = _recent()
    checked, now = cache.setdefault("checked", {}), time.time()
    todo = [s for s in slugs if s not in cache["films"]
            or (s in recent and now - checked.get(s, 0) > every)]
    log(f"Greek titles: {len(slugs):,} films, {len(todo):,} to look up (new, or recent and due)")
    size = 250
    for n, i in enumerate(range(0, len(todo), size), 1):
        batch = [s for s in todo[i:i + size] if re.fullmatch(r"[a-z0-9-]+", s)]
        values = " ".join(f'"{s}"' for s in batch)
        query = ('SELECT ?slug ?item ?el WHERE { VALUES ?slug { %s } '
                 '?item wdt:P6127 ?slug . '
                 'OPTIONAL { ?item rdfs:label ?el FILTER(LANG(?el) = "el") } }') % values
        data = fetch_json(SPARQL, urllib.parse.urlencode({"query": query}).encode())
        seen = set()
        for b in data["results"]["bindings"]:
            slug = b["slug"]["value"]
            if slug in seen and not b.get("el"):
                continue                                # one row per film; keep the one with a label
            seen.add(slug)
            cache["films"][slug] = {"q": b["item"]["value"].rsplit("/", 1)[-1],
                                    "el": b.get("el", {}).get("value", "")}
        for s in todo[i:i + size]:
            cache["films"].setdefault(s, None)          # None = not on Wikidata (yet)
            checked[s] = now
        if n % 10 == 0 or i + size >= len(todo):
            save_cache(cache)
            done = min(i + size, len(todo))
            found = sum(1 for s in todo[:done] if cache["films"].get(s))
            log(f"  {done:,}/{len(todo):,} looked up, {found:,} found on Wikidata")
        time.sleep(0.6)


# ------------------------------------------------------------ phase 2: details

def claim_ids(statements, ordered=False):
    out = []
    for idx, st in enumerate(statements):
        if st.get("rank") == "deprecated":
            continue
        snak = st.get("mainsnak", {})
        val = snak.get("datavalue", {}).get("value")
        if not isinstance(val, dict) or "id" not in val:
            continue
        order = 10_000 + idx
        if ordered:
            for q in st.get("qualifiers", {}).get("P1545", []):
                raw = q.get("datavalue", {}).get("value", "")
                if str(raw).isdigit():
                    order = int(raw)
                    break
        out.append((order, val["id"]))
    out.sort()
    seen, ids = set(), []
    for _, qid in out:
        if qid not in seen:
            seen.add(qid)
            ids.append(qid)
    return ids


def minutes(statements):
    for st in statements:
        val = st.get("mainsnak", {}).get("datavalue", {}).get("value")
        if not isinstance(val, dict) or "amount" not in val:
            continue
        amount = float(val["amount"])
        unit = val.get("unit", "").rsplit("/", 1)[-1]
        if unit == "Q7727":
            return round(amount)
        if unit == "Q25235":
            return round(amount * 60)
        if unit == "Q11574":
            return round(amount / 60)
    return None


def fetch_details(slugs, cache):
    """Cast, director, genres, running time — again weekly for recent films,
    whose entries keep filling in after release."""
    recent, every = _recent()
    stamp, now = cache.setdefault("detail_at", {}), time.time()
    want = [s for s in slugs if cache["films"].get(s) and (s not in cache["detail"]
            or (s in recent and now - stamp.get(s, 0) > every))]
    log(f"Details: {len(slugs):,} films selected, {len(want):,} to fetch (new, or recent and due)")
    by_q = {cache["films"][s]["q"]: s for s in want}
    qids = list(by_q)
    for n, i in enumerate(range(0, len(qids), 50), 1):
        batch = qids[i:i + 50]
        url = API + "?" + urllib.parse.urlencode({
            "action": "wbgetentities", "ids": "|".join(batch), "props": "claims|sitelinks",
            "sitefilter": "elwiki", "format": "json", "maxlag": 5})
        data = fetch_json(url)
        for qid, ent in data.get("entities", {}).items():
            slug = by_q.get(qid)
            if not slug or "claims" not in ent:
                continue
            c = ent["claims"]
            rt = ""
            for st in c.get("P1258", []):
                v = st.get("mainsnak", {}).get("datavalue", {}).get("value")
                if isinstance(v, str):
                    rt = v
                    break
            cache["detail"][slug] = {
                "cast": claim_ids(c.get("P161", []), ordered=True)[:6],
                "dir": claim_ids(c.get("P57", []))[:3],
                "genre": claim_ids(c.get("P136", []))[:5],
                "min": minutes(c.get("P2047", [])),
                "rt": rt,
                "elwiki": ent.get("sitelinks", {}).get("elwiki", {}).get("title", ""),
            }
        for q in batch:
            cache["detail"].setdefault(by_q[q], {})
            stamp[by_q[q]] = now
        if n % 10 == 0 or i + 50 >= len(qids):
            save_cache(cache)
            log(f"  {min(i + 50, len(qids)):,}/{len(qids):,} films")
        time.sleep(0.4)


def fetch_labels(cache):
    need = set()
    for d in cache["detail"].values():
        for key in ("cast", "dir", "genre"):
            need.update(d.get(key, []))
    # names still missing a Greek spelling are asked for again monthly
    asked, now = cache.setdefault("labels_at", {}), time.time()
    todo = sorted(q for q in need if q not in cache["labels"]
                  or (not cache["labels"][q].get("el") and now - asked.get(q, 0) > 30 * 86400))
    log(f"Names: {len(need):,} people and genres, {len(todo):,} to fetch")
    for n, i in enumerate(range(0, len(todo), 50), 1):
        batch = todo[i:i + 50]
        url = API + "?" + urllib.parse.urlencode({
            "action": "wbgetentities", "ids": "|".join(batch), "props": "labels",
            "languages": "el|en", "format": "json", "maxlag": 5})
        data = fetch_json(url)
        for qid, ent in data.get("entities", {}).items():
            labels = ent.get("labels", {})
            cache["labels"][qid] = {"el": labels.get("el", {}).get("value", ""),
                                    "en": labels.get("en", {}).get("value", "")}
        for q in batch:
            cache["labels"].setdefault(q, {"el": "", "en": ""})
            asked[q] = now
        if n % 20 == 0 or i + 50 >= len(todo):
            save_cache(cache)
            log(f"  {min(i + 50, len(todo)):,}/{len(todo):,} names")
        time.sleep(0.4)


def main():
    top = 150
    if "--detail-top" in sys.argv:
        top = int(sys.argv[sys.argv.index("--detail-top") + 1])

    lists = scraped_lists()
    all_slugs = sorted({s for rows in lists.values() for s, _ in rows})
    detail = []
    seen = set()
    for rows in lists.values():
        for s, _ in rows[:top]:
            if s not in seen:
                seen.add(s)
                detail.append(s)

    cache = load_cache()
    started = time.time()
    fetch_titles(all_slugs, cache)
    fetch_details(detail, cache)
    fetch_labels(cache)
    save_cache(cache)

    found = sum(1 for s in all_slugs if cache["films"].get(s))
    greek = sum(1 for s in all_slugs if (cache["films"].get(s) or {}).get("el"))
    log(f"Done in {(time.time() - started) / 60:.0f} min — {found:,}/{len(all_slugs):,} films on "
        f"Wikidata, {greek:,} with a Greek title, details for {len(detail):,}.")


if __name__ == "__main__":
    main()
