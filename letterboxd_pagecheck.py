"""
A daily check of the cinema pages you look up in the app ("What's on
elsewhere"), so a page that stops parsing properly is noticed without you
having to spot the missing film yourself.

Every page you open in the app is remembered in page_watch.json. Once a day
each one is read again, and two kinds of trouble are recorded:

  - lines that look like films but couldn't be placed ("Couldn't place: …")
  - films found last time whose title is still on the page but weren't found
    this time (Naza vanishing because of a www. redirect)

A page with trouble is read a second time a minute later, and only what goes
wrong both times counts: Letterboxd and Wikidata sometimes don't answer.
Two sites that list the same cinemas are also compared (cinema-her.com lists
every Heraklion cinema, texnopolis.net the Texnopolis ones): a film one of
them has whose title is written on the other's page, yet wasn't found there,
is a page the app is misreading. Films only at a cinema the other site doesn't
cover (Τάλως), or opening next week, are expected and only noted.
Results go to page_checks.json; the self-check shows them in the health strip.
Lines that are not films (a stage show, a job ad) can be listed under the
page's "ignore" in page_watch.json.

    python3 letterboxd_pagecheck.py          # check now, print the result
"""

import os
import re
import sys
import json
import time

HOME = os.path.expanduser("~")
sys.path.insert(0, os.path.join(HOME, "Desktop"))
CACHE = os.path.join(HOME, ".letterboxd_cache")
WATCH = os.path.join(CACHE, "page_watch.json")      # {url: {"added": ts, "ignore": [...]}}
RESULT = os.path.join(CACHE, "page_checks.json")
EVERY_HOURS = 20
SEED = ["https://cinema-her.com/", "texnopolis.net"]
# (site that lists every cinema, site of one chain, cinemas that only the first covers)
PAIRS = [("https://cinema-her.com/", "texnopolis.net", ["Τάλως"])]


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, d):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def same(url):
    """One key for one page: cinema-her.com, www.cinema-her.com and https://cinema-her.com/ alike."""
    u = re.sub(r"^https?://", "", (url or "").strip().lower())
    return u.removeprefix("www.").rstrip("/")


def watch(url):
    """Remember a page you opened in the app, so the daily check covers it."""
    url = (url or "").strip()
    if not url:
        return
    w = _load(WATCH, {})
    if same(url) not in {same(u) for u in w}:
        w[url] = {"added": time.time(), "ignore": []}
        _save(WATCH, w)


_slugs = {}


def _films(res):
    """{Letterboxd slug: text} — a film known only by its IMDb number gets its slug, so both sites agree."""
    import letterboxd_pagefilms as pf
    out = {}
    for f in res["films"]:
        k = f["key"]
        if k.startswith("imdb:"):
            if k not in _slugs:
                hit = pf.resolve_imdb(k[5:])
                _slugs[k] = hit[0] if hit else k
            k = _slugs[k]
        out[k] = f["text"]
    return out


def _read(url):
    import letterboxd_pagefilms as pf
    res = pf.films_on(url)
    _, page = pf.fetch(url)
    text = pf.norm(" \n ".join(pf.page_lines(page)))
    return res, text


def check_site(url, before, ignore=()):
    import letterboxd_pagefilms as pf
    res, text = _read(url)
    found = _films(res)
    where = dict(zip(found, (f.get("times") or [] for f in res["films"])))
    skip = {pf.norm(i) for i in ignore}
    unplaced = [u for u in res["unmatched"] if pf.norm(u) not in skip]
    # a film the page now announces as coming soon (a premiere banner) isn't lost
    coming = {pf.norm(c) for c in res.get("coming", [])}
    def gone(found, text):
        have = {pf.norm(t) for t in found.values()}     # by title too: a film's ID can change form
        return [t for k, t in (before or {}).items() if k not in found and t and pf.norm(t) not in have
                and pf.norm(t) in text and pf.norm(t) not in coming]
    lost = gone(found, text)
    if unplaced or lost:                          # read again: only lasting trouble counts
        time.sleep(60)
        res2, text2 = _read(url)
        found2 = _films(res2)
        found = {**found, **found2}
        where = {**dict(zip(found2, (f.get("times") or [] for f in res2["films"]))), **where}
        text = text2
        coming |= {pf.norm(c) for c in res2.get("coming", [])}
        unplaced = [u for u in unplaced if u in res2["unmatched"]]
        lost = gone(found, text2)
    return {"films": len(found), "found": found, "where": where, "unplaced": unplaced, "lost": lost,
            "coming": sorted(coming), "_text": text}


def due():
    r = _load(RESULT, {})
    return time.time() - r.get("ts", 0) > EVERY_HOURS * 3600


def run(log=print):
    w, keys = {}, set()
    for url, opts in [(u, {"added": time.time(), "ignore": []}) for u in SEED] + list(_load(WATCH, {}).items()):
        if same(url) not in keys:                 # www. and the bare address are the same page
            keys.add(same(url))
            w[url] = opts
    _save(WATCH, w)
    import letterboxd_pagefilms as pf
    prev = _load(RESULT, {}).get("sites", {})
    sites = {}
    for url, opts in w.items():
        try:
            # films found on earlier runs are remembered, so one bad day can't erase what "lost" compares against
            before = {**prev.get(url, {}).get("known", {}), **prev.get(url, {}).get("found", {})}
            s = check_site(url, before, opts.get("ignore", []))
            s["failed"] = 0
            s["known"] = {k: t for k, t in {**before, **s["found"]}.items()
                          if pf.norm(t) not in set(s["coming"])}
        except Exception as e:
            # small sites are often down for an hour: keep the last good reading, and only
            # count it as trouble when the page can't be read two days running
            last = prev.get(url, {})
            s = {**{k: v for k, v in last.items() if k != "error"},
                 "failed": last.get("failed", 0) + 1, "lastError": f"{type(e).__name__}: {e}"}
            if s["failed"] >= 2:
                s["error"] = s["lastError"]
        sites[url] = s
        bits = [f"{s.get('films', 0)} films"] + ([f"couldn't place {s['unplaced']}"] if s.get("unplaced") else []) \
            + ([f"no longer found {s['lost']}"] if s.get("lost") else []) + ([s["error"]] if s.get("error") else [])
        log(f"page check {url}: " + "; ".join(bits))
    pairs = compare(sites, log)
    for s in sites.values():
        s.pop("_text", None)
    _save(RESULT, {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "ts": time.time(), "sites": sites, "pairs": pairs})
    return sites


def compare(sites, log=print):
    """Films one site of a pair has and the other misreads; expected differences as notes."""
    import letterboxd_pagefilms as pf
    out = []
    for a, b, only_a in PAIRS:
        A, B = sites.get(a, {}), sites.get(b, {})
        if "found" not in A or "found" not in B:
            continue
        missed, notes = [], []
        for here, there, other in ((A, B, b), (B, A, a)):
            for k, t in here["found"].items():
                if k in there["found"]:
                    continue
                times = here.get("where", {}).get(k, [])
                if here is A and times and all(any(v in line for v in only_a) for line in times):
                    notes.append(f"{t}: only at {', '.join(only_a)}")
                elif pf.norm(t) and pf.norm(t) in there.get("_text", ""):
                    missed.append(f"{t}: on {other.split('//')[-1].strip('/')} but not recognised there")
                else:
                    notes.append(f"{t}: not listed on {other.split('//')[-1].strip('/')}")
        out.append({"sites": [a, b], "missed": missed, "notes": notes})
        log(f"page check {a} vs {b}: " + ("; ".join(missed) if missed else "they agree") +
            (f" (expected differences: {'; '.join(notes)})" if notes else ""))
    return out


def problems():
    """[(page, what, detail)] from the last check, for the health strip."""
    out = []
    for url, s in _load(RESULT, {}).get("sites", {}).items():
        if s.get("error"):
            out.append((url, "couldn't be read", s["error"]))
        if s.get("unplaced"):
            out.append((url, f"{len(s['unplaced'])} film(s) couldn't be placed", ", ".join(s["unplaced"])))
        if s.get("lost"):
            out.append((url, f"{len(s['lost'])} film(s) no longer found", ", ".join(s["lost"])))
    for p in _load(RESULT, {}).get("pairs", []):
        if p["missed"]:
            out.append((" vs ".join(p["sites"]), f"{len(p['missed'])} film(s) one site has and the other misreads",
                        "; ".join(p["missed"])))
    return out


if __name__ == "__main__":
    run()
    for p in problems():
        print(" — ".join(p))
