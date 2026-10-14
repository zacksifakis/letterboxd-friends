"""
Rank an actor's or director's filmography by how many of your Letterboxd
friends have watched each film.

    python3 letterboxd_person.py "Anne Hathaway"
    python3 letterboxd_person.py "Stanley Kubrick" --director
    python3 letterboxd_person.py "Greta Gerwig" --actor

Letterboxd has no per-member person filter, so the first query touching a given
decade scrapes that decade across all your friends and caches it under
~/.letterboxd_cache/. Every later query hitting a cached decade is instant.
"""

import os
import re
import sys
import json
import time
import random
import pandas as pd
import letterboxd_net as net
from bs4 import BeautifulSoup

import letterboxd_settings as _settings
USERNAME = _settings.require_username()
BASE = "https://letterboxd.com"

# Accounts you follow that Letterboxd's own /following/ pages fail to list.
EXTRA_FRIENDS = _settings.EXTRA_FRIENDS

CACHE = os.path.expanduser("~/.letterboxd_cache")
PAGE_PAUSE = (0.6, 1.2)
ROTATE_EVERY = 40
MAX_RETRIES = 5

_session, _used = None, 0
stats = {"requests": 0, "retries": 0}


def session():
    global _session, _used
    if _session is None or _used >= ROTATE_EVERY:
        _session = net.session()
        _used = 0
    return _session


def fetch(url):
    """GET with retries. Returns (soup, ok)."""
    global _session, _used
    for attempt in range(MAX_RETRIES):
        try:
            s = session()
            _used += 1
            stats["requests"] += 1
            r = s.get(url, timeout=20)
        except Exception:
            stats["retries"] += 1
            _session = None
            time.sleep(2 + 3 * attempt)
            continue
        if r.status_code == 200:
            return BeautifulSoup(r.text, "html.parser"), True
        if r.status_code == 404:
            return None, False
        stats["retries"] += 1
        _session = None
        time.sleep(3 + 4 * attempt)
    return None, False


def has_next(soup):
    n = soup.select_one(".paginate-nextprev .next")
    return bool(n and n.name == "a" and n.get("href"))


def posters(soup):
    """Every film poster on a page -> (slug, title, year)."""
    out = []
    for p in soup.select("div[data-item-slug]"):
        slug = p.get("data-item-slug")
        if not slug:
            continue
        name = p.get("data-item-name") or ""
        m = re.search(r"\((\d{4})\)\s*$", name)
        title = re.sub(r"\s*\(\d{4}\)$", "", name).strip()
        if not title:
            img = p.find("img")
            title = (img.get("alt") or "").strip() if img else slug.replace("-", " ").title()
        out.append((slug, title, m.group(1) if m else ""))
    return out


def slugify(name):
    n = name.lower().strip()
    for a, b in [("é","e"),("è","e"),("ê","e"),("ë","e"),("á","a"),("à","a"),("â","a"),
                 ("ä","a"),("ó","o"),("ò","o"),("ô","o"),("ö","o"),("ú","u"),("ù","u"),
                 ("ü","u"),("í","i"),("ì","i"),("ï","i"),("ñ","n"),("ç","c"),("ø","o"),
                 ("å","a"),("ß","ss"),("æ","ae")]:
        n = n.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", n).strip("-")


# ---------------------------------------------------------------- friends

def get_friends():
    print("Fetching your friends list...")
    friends, page = [], 1
    while True:
        soup, ok = fetch(f"{BASE}/{USERNAME}/following/page/{page}/")
        if not ok:
            print(f"  !! following page {page} failed — friend list INCOMPLETE")
            break
        names = [a.get("href", "").strip("/") for a in soup.select("a.name[href^='/']")]
        names = [n for n in names if n and "/" not in n and n.lower() != USERNAME.lower()]
        if not names:
            break
        friends.extend(names)
        if not has_next(soup):
            break
        page += 1
        time.sleep(random.uniform(*PAGE_PAUSE))

    friends = list(dict.fromkeys(friends))
    friends += [e for e in EXTRA_FRIENDS if e not in friends]
    print(f"  {len(friends)} friends")
    return friends


# ---------------------------------------------------------------- cache

def cache_path(decade):
    return os.path.join(CACHE, f"decade_{decade}.json")


def load_decade(decade, allow_partial=False):
    p = cache_path(decade)
    if os.path.exists(p):
        with open(p) as f:
            data = json.load(f)
        if data.get("partial") and not allow_partial:
            return None          # incomplete build: caller should resume it
        return data
    return None


def build_decade(decade, friends):
    """Scrape one decade across all friends and cache it."""
    os.makedirs(CACHE, exist_ok=True)
    per_friend, meta, incomplete = {}, {}, []

    # Resume an interrupted build rather than starting over.
    prev = load_decade(decade, allow_partial=True)
    if prev and prev.get("partial"):
        per_friend = prev["per_friend"]
        meta = prev["meta"]
        incomplete = prev["incomplete"]
        print(f"\nResuming {decade} build — {len(per_friend)} friends already cached")

    todo = [f for f in friends if f not in per_friend]
    print(f"\nBuilding cache for {decade}: {len(todo)} friends to fetch "
          f"(one-time; later queries reuse it)...")
    started = time.time()

    def save(partial):
        with open(cache_path(decade), "w") as f:
            json.dump({"friends": friends, "per_friend": per_friend, "meta": meta,
                       "incomplete": incomplete, "partial": partial}, f)

    for i, friend in enumerate(todo, 1):
        films, page, complete = [], 1, True
        while True:
            soup, ok = fetch(f"{BASE}/{friend}/films/decade/{decade}/page/{page}/")
            if not ok:
                complete = False
                break
            got = posters(soup)
            if not got:
                complete = page == 1
                break
            films.extend(got)
            if not has_next(soup):
                break
            page += 1
            time.sleep(random.uniform(*PAGE_PAUSE))

        if not complete:
            incomplete.append(friend)
        per_friend[friend] = sorted({f[0] for f in films})
        for slug, title, year in films:
            if slug not in meta:
                meta[slug] = {"title": title, "year": year}

        if i % 20 == 0 or i == len(todo):
            save(partial=True)          # checkpoint
            print(f"   {i}/{len(todo)} friends  ({time.time() - started:.0f}s)")

    save(partial=False)
    data = load_decade(decade)

    if incomplete:
        print(f"  !! {len(incomplete)} friends INCOMPLETE: {', '.join(incomplete)}")
    else:
        print(f"  {decade} cached cleanly ({time.time() - started:.0f}s)")
    return data


# ---------------------------------------------------------------- person

def resolve(name, forced_role=None):
    """Find the person on Letterboxd and return (role, slug, films)."""
    slug = slugify(name)
    roles = [forced_role] if forced_role else ["actor", "director"]
    best = None
    for role in roles:
        soup, ok = fetch(f"{BASE}/{role}/{slug}/")
        if not ok:
            continue
        films, page = posters(soup), 1
        while has_next(soup) and page < 20:
            page += 1
            soup, ok = fetch(f"{BASE}/{role}/{slug}/page/{page}/")
            if not ok:
                break
            films.extend(posters(soup))
            time.sleep(random.uniform(*PAGE_PAUSE))
        films = list({f[0]: f for f in films}.values())
        if films and (best is None or len(films) > len(best[2])):
            best = (role, slug, films)
    return best


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    if not args:
        print('Usage: python3 letterboxd_person.py "Anne Hathaway" [--actor|--director]')
        return
    name = " ".join(args)
    role = "director" if "--director" in flags else "actor" if "--actor" in flags else None

    found = resolve(name, role)
    if not found:
        print(f'No Letterboxd page found for "{name}" (tried /actor/{slugify(name)}/ '
              f'and /director/{slugify(name)}/).')
        print("Check the spelling, or pass --director / --actor explicitly.")
        return

    role, slug, films = found
    print(f'\n{name} -> {BASE}/{role}/{slug}/  ({len(films)} films)')

    decades = sorted({f[2][:3] + "0s" for f in films if f[2]})
    print(f"Decades needed: {', '.join(decades)}")

    friends = None
    caches = {}
    for d in decades:
        c = load_decade(d)
        if c is None:
            if friends is None:
                friends = get_friends()
            c = build_decade(d, friends)
        else:
            print(f"  {d}: using cache ({len(c['per_friend'])} friends)")
        caches[d] = c

    counts = []
    for fslug, title, year in films:
        if not year:
            continue
        c = caches.get(year[:3] + "0s")
        if not c:
            continue
        n = sum(1 for slugs in c["per_friend"].values() if fslug in slugs)
        counts.append({"Film": title, "Year": year, "Slug": fslug,
                       "Friends Watched": n, "URL": f"{BASE}/film/{fslug}/"})

    df = (pd.DataFrame(counts)
          .sort_values(["Friends Watched", "Year"], ascending=[False, False])
          .reset_index(drop=True))

    total = len(set().union(*[set(c["per_friend"].keys()) for c in caches.values()]))
    print(f"\n=== {name.upper()} — ranked by how many of your {total} friends watched ===")
    for i, r in enumerate(df.itertuples(), 1):
        print(f"  #{i:<3} {r.Film[:48]:50s} ({r.Year})  {getattr(r, '_4'):3d} watched")

    out = os.path.expanduser(f"~/person_{slug}.csv")
    df.to_csv(out, index=False)
    print(f"\nSaved: {out}")

    bad = sorted({f for c in caches.values() for f in c["incomplete"]})
    if bad:
        print(f"\n!! {len(bad)} friends had incomplete data: {', '.join(bad)}")
        print("   Counts may be low. Delete the affected ~/.letterboxd_cache/decade_*.json "
              "and re-run to fix.")


if __name__ == "__main__":
    main()
