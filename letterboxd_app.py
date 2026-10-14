"""
Local web UI for the Letterboxd friends-watched scraper.

    python3 letterboxd_app.py

Opens http://localhost:8765. Pick a year or decade, hit Scrape, watch progress
live, then sort/filter the results. Already-scraped periods load instantly.
Re-scraping keeps a snapshot of the previous run so you can see exactly what
changed since last time.

Standard library only — no pip installs. The scraping itself is delegated to
letterboxd_friends_v2.py, so results are identical to running it by hand.
"""

import os
import re
import csv
import json
import time
import shutil
import html
import datetime
import unicodedata
import glob
import urllib.parse
import sys
import threading
import subprocess
import webbrowser
import letterboxd_net as net
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = 8765
HOME = os.path.expanduser("~")
SCRAPER = os.path.join(HOME, "Desktop", "letterboxd_friends_v2.py")
HISTORY = os.path.join(HOME, ".letterboxd_history")

job = {"running": False, "period": None, "lines": [], "started": None,
       "finished": None, "returncode": None}
job_lock = threading.Lock()
server = None


# ------------------------------------------------------------------ data

def csv_path(period):
    return os.path.join(HOME, f"friends_watched_{period}.csv")


def prev_path(period):
    return os.path.join(HISTORY, f"{period}.prev.csv")


def load_csv(path):
    """-> {slug: {title, year, count, url, rating, ratings, liked}}"""
    if not os.path.exists(path):
        return None
    out = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            slug = r.get("Slug", "")
            if not slug:
                continue
            try:
                count = int(r.get("Friend Watch Count") or 0)
            except ValueError:
                count = 0
            def num(key, cast=int, default=None):
                try:
                    return cast(r[key]) if r.get(key) not in (None, "") else default
                except (ValueError, KeyError):
                    return default
            out[slug] = {"title": r.get("Film Title", ""), "year": r.get("Year", "") or "",
                         "count": count, "url": r.get("Letterboxd URL", ""),
                         # only in lists scraped since ratings were added
                         "rating": num("Avg Rating", float), "ratings": num("Ratings", int, 0),
                         "liked": num("Liked %", int)}
    return out


def saved_periods():
    out = []
    for name in os.listdir(HOME):
        m = re.fullmatch(r"friends_watched_([0-9]{4}s?)\.csv", name)
        if not m:
            continue
        p, full = m.group(1), os.path.join(HOME, name)
        out.append({"period": p, "mtime": os.path.getmtime(full),
                    "rows": max(0, sum(1 for _ in open(full)) - 1),
                    "hasPrev": os.path.exists(prev_path(p))})
    out.sort(key=lambda d: d["mtime"], reverse=True)
    return out


def read_results(period):
    data = load_csv(csv_path(period))
    if data is None:
        return None
    rows = [{"slug": s, **v} for s, v in data.items()]
    rows.sort(key=lambda d: -d["count"])
    return rows


_index = {"stamp": None, "data": None}


def build_index():
    """{period: {"rows": [...], "rankOf": {count: rank}}} across every saved CSV."""
    data = {}
    for s in saved_periods():
        rows = read_results(s["period"])
        if not rows:
            continue
        rank_of, rank = {}, 0
        for i, r in enumerate(rows):          # rows are sorted by count desc
            if r["count"] not in rank_of:
                rank_of[r["count"]] = i + 1   # ties share the better rank
            rank = i + 1
        data[s["period"]] = {"rows": rows, "rankOf": rank_of, "total": rank}
    return data


def get_index():
    # File size too: an exclusion recount rewrites lists but keeps their dates.
    excl = os.path.join(HOME, ".letterboxd_cache", "excluded.json")
    stamp = (tuple(sorted((s["period"], s["mtime"], os.path.getsize(csv_path(s["period"])))
                          for s in saved_periods())),
             os.path.getmtime(excl) if os.path.exists(excl) else 0)
    if _index["stamp"] != stamp:
        _index["data"] = build_index()
        _index["stamp"] = stamp
    return _index["data"]


def search_films(q, limit=25):
    """Find films by title and report where they rank in each period they appear in."""
    q = q.strip().lower()
    if len(q) < 2:
        return []
    hits = {}
    for period, blob in get_index().items():
        for r in blob["rows"]:
            if q not in r["title"].lower():
                continue
            e = hits.setdefault(r["slug"], {"slug": r["slug"], "title": r["title"],
                                            "year": r["year"], "url": r["url"],
                                            "best": 0, "in": [], "rating": None,
                                            "ratings": 0, "liked": None, "seats": 0})
            # Ratings from the film's own year list where there is one (years
            # like this one are refreshed daily), otherwise its decade.
            if r.get("rating") is not None or r.get("liked") is not None:
                if e["rating"] is None or not period.endswith("s"):
                    e.update(rating=r.get("rating"), ratings=r.get("ratings", 0),
                             liked=r.get("liked"), seats=r["count"])
            e["in"].append({"period": period, "rank": blob["rankOf"][r["count"]],
                            "count": r["count"], "outOf": blob["total"]})
            e["best"] = max(e["best"], r["count"])
    out = sorted(hits.values(), key=lambda d: -d["best"])[:limit]
    for e in out:
        # exact year first, then decade, then anything else
        e["in"].sort(key=lambda x: (len(x["period"]), x["period"]))
    return out


_person_cache = {}


def _slugify(name):
    """'Timothée Chalamet' -> 'timothee-chalamet' (matches Letterboxd's scheme)."""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", n.lower()).strip("-")


def _clean_name(title):
    """'Stanley Kubrick filmography' -> 'Stanley Kubrick'."""
    t = re.sub(r"\s*\(.*?\)\s*", " ", title)
    t = re.sub(r"^List of\s+", "", t)
    t = re.sub(r"\s+(filmography|on screen and stage|performances|discography).*$", "",
               t, flags=re.I)
    return re.sub(r"'s$", "", t.strip()).strip()


def _name_candidates(query, role):
    """Expand a partial name ('kubrick') into plausible full names."""
    url = ("https://en.wikipedia.org/w/api.php?action=query&list=search&format=json"
           f"&srlimit=8&srsearch={urllib.parse.quote(query + ' film ' + role)}")
    try:
        hits = net.session().get(url, timeout=15).json()
        titles = [h["title"] for h in hits["query"]["search"]]
    except Exception:
        return []
    out, seen = [], set()
    for t in titles:
        c = _clean_name(t)
        if not c or len(c.split()) > 4 or c.lower() in seen:
            continue
        seen.add(c.lower())
        out.append(c)
    q = query.lower()
    out.sort(key=lambda n: (q not in n.lower(),))   # surname match first
    return out


class LookupUnavailable(Exception):
    """Letterboxd didn't give a usable answer — distinct from 'doesn't exist'."""


_lb_session = None
_lb_lock = threading.Lock()


def _letterboxd_get(url, tries=4):
    """
    GET with retries on ONE reused session. Returns (status, html); only 200
    and 404 count as real answers. Reusing a session keeps its cookies, which
    makes repeated requests far more reliable than starting fresh each time.
    """
    global _lb_session
    status = 0
    with _lb_lock:
        for attempt in range(tries):
            if _lb_session is None:
                _lb_session = net.session()
            try:
                r = _lb_session.get(url, timeout=20)
                if r.status_code in (200, 404):
                    return r.status_code, r.text
                status = r.status_code
            except Exception:
                status, _lb_session = 0, None
            time.sleep(1.0 + attempt)
        _lb_session = None          # this one is stuck; start clean next time
    return status, None


def _person_page(role, slug):
    """
    (exists, display_name, films_on_first_page). exists is None when Letterboxd
    didn't answer properly, so callers can tell 'no such person' from 'try again'.
    """
    status, page = _letterboxd_get(f"https://letterboxd.com/{role}/{slug}/")
    if status == 404:
        return False, None, 0
    if status != 200:
        return None, None, 0
    # Raw markup is "&lrm;Films starring Joaquin Phoenix &bull; Letterboxd",
    # so decode the entities before matching.
    t = re.search(r"<title>(.*?)</title>", page, re.S)
    title = html.unescape(t.group(1)) if t else ""
    m = re.search(r"(?:starring|directed by)\s+(.+?)\s*•", title)
    name = m.group(1).strip() if m else None
    return True, name, page.count("data-item-slug=")


def resolve_person(name, role):
    """
    (slug, display_name) for a possibly-partial name, or None if no such person.
    Raises LookupUnavailable rather than claiming 'not found' when Letterboxd
    was unreachable, so a hiccup never masquerades as a missing person.
    """
    inconclusive = False
    direct = _slugify(name)
    if direct:
        exists, proper, _ = _person_page(role, direct)
        if exists:
            return direct, proper or name
        inconclusive |= exists is None
    for cand in _name_candidates(name, role):
        slug = _slugify(cand)
        if not slug or slug == direct:
            continue
        exists, proper, _ = _person_page(role, slug)
        if exists:
            return slug, proper or cand
        inconclusive |= exists is None
    if inconclusive:
        raise LookupUnavailable("Letterboxd didn't respond properly — try again in a moment.")
    return None


def _filmography(role, slug):
    """Films credited to a person on Letterboxd -> [(slug, title, year)]."""
    from bs4 import BeautifulSoup

    films, page = [], 1
    while page <= 15:
        status, page_html = _letterboxd_get(f"https://letterboxd.com/{role}/{slug}/page/{page}/")
        if status != 200:
            # A partial filmography would silently hide films, so fail loudly.
            raise LookupUnavailable(
                f"Letterboxd stopped responding while loading page {page} of the "
                f"filmography — try again in a moment.")
        soup = BeautifulSoup(page_html, "html.parser")
        got = soup.select("div[data-item-slug]")
        if not got:
            break
        for p in got:
            fslug = p.get("data-item-slug")
            if not fslug:
                continue
            name = p.get("data-item-name") or ""
            m = re.search(r"\((\d{4})\)\s*$", name)
            title = re.sub(r"\s*\(\d{4}\)$", "", name).strip() or fslug.replace("-", " ").title()
            films.append((fslug, title, m.group(1) if m else ""))
        nxt = soup.select_one(".paginate-nextprev .next")
        if not (nxt and nxt.name == "a" and nxt.get("href")):
            break
        page += 1
        time.sleep(0.5)
    return list({f[0]: f for f in films}.values())


def person_films(name, role=None):
    """A person's filmography, annotated with where each film ranks locally."""
    roles = [role] if role in ("director", "actor") else ["director", "actor"]

    best = None
    for rl in roles:
        found = resolve_person(name, rl)
        if not found:
            continue
        slug, display = found
        key = (rl, slug)
        films = _person_cache.get(key)
        if films is None:
            films = _filmography(rl, slug)
            if films:                      # never remember an empty result
                _person_cache[key] = films
        if films and (best is None or len(films) > len(best[1])):
            best = (rl, films, slug, display)
    if not best:
        return None

    rl, films, slug, name = best

    # Searching "Scorsese" as an actor finds real credits (Letterboxd counts
    # documentary and interview appearances), so point at the other role too.
    # Letterboxd serves a page for any role even with zero credits, so only
    # offer the other role when it actually lists films.
    other = "director" if rl == "actor" else "actor"
    try:
        exists, _, n_other = _person_page(other, slug)
        also = other if exists and n_other > 0 else None
    except Exception:
        also = None
    idx = get_index()
    out = []
    for fslug, title, year in films:
        appearances, stats = [], {"rating": None, "ratings": 0, "liked": None, "seats": 0}
        for period, blob in idx.items():
            for r in blob["rows"]:
                if r["slug"] == fslug:
                    appearances.append({"period": period, "rank": blob["rankOf"][r["count"]],
                                        "count": r["count"], "outOf": blob["total"]})
                    # year list first (refreshed most often), else the decade
                    if (r.get("rating") is not None or r.get("liked") is not None) and \
                            (stats["rating"] is None or not period.endswith("s")):
                        stats = {"rating": r.get("rating"), "ratings": r.get("ratings", 0),
                                 "liked": r.get("liked"), "seats": r["count"]}
                    break
        appearances.sort(key=lambda x: (len(x["period"]), x["period"]))
        out.append({"slug": fslug, "title": title, "year": year,
                    "url": f"https://letterboxd.com/film/{fslug}/",
                    "in": appearances, **stats,
                    "best": max([a["count"] for a in appearances], default=-1)})
    out.sort(key=lambda d: -d["best"])
    return {"name": name, "role": rl, "slug": slug, "alsoRole": also,
            "personUrl": f"https://letterboxd.com/{rl}/{slug}/",
            "films": out,
            "scraped": sum(1 for f in out if f["in"]), "total": len(out)}


# ------------------------------------------------- what you've seen yourself

MY_FILMS = os.path.join(HOME, ".letterboxd_cache", "my_films.json")
my_films_job = {"running": False, "error": ""}


def scraper_username():
    """Your Letterboxd username, from your local settings file."""
    import letterboxd_settings
    return letterboxd_settings.require_username()


def load_my_films():
    if os.path.exists(MY_FILMS):
        try:
            with open(MY_FILMS) as f:
                return json.load(f)
        except Exception:
            return None
    return None


def scrape_my_films():
    """Every film on your own profile — about ten pages."""
    from bs4 import BeautifulSoup
    user, slugs, page = scraper_username(), [], 1
    while page <= 80:
        status, page_html = _letterboxd_get(f"https://letterboxd.com/{user}/films/page/{page}/")
        if status != 200:
            raise LookupUnavailable(f"Letterboxd stopped responding on page {page} of your films.")
        soup = BeautifulSoup(page_html, "html.parser")
        got = [p.get("data-item-slug") for p in soup.select("div[data-item-slug]")]
        got = [g for g in got if g]
        if not got:
            break
        slugs.extend(got)
        nxt = soup.select_one(".paginate-nextprev .next")
        if not (nxt and nxt.name == "a" and nxt.get("href")):
            break
        page += 1
        time.sleep(0.4)
    return sorted(set(slugs))


def refresh_my_films():
    if my_films_job["running"]:
        return
    my_films_job.update(running=True, error="")
    try:
        slugs = scrape_my_films()
        os.makedirs(os.path.dirname(MY_FILMS), exist_ok=True)
        with open(MY_FILMS, "w") as f:
            json.dump({"asOf": datetime.datetime.now().isoformat(timespec="seconds"),
                       "slugs": slugs}, f)
    except Exception as e:
        my_films_job["error"] = str(e)
    finally:
        my_films_job["running"] = False


def my_films_payload():
    data = load_my_films() or {}
    return {"slugs": data.get("slugs", []), "asOf": data.get("asOf", ""),
            "count": len(data.get("slugs", [])),
            "running": my_films_job["running"], "error": my_films_job["error"]}


def maybe_refresh_my_films(max_age_hours=3):
    """
    Your watched list, refetched when it's more than a few hours old — about
    ten requests. It used to wait three days and only check at launch, so a
    film you'd just logged could stay unticked for most of a week.
    """
    data = load_my_films()
    if data:
        try:
            age = (datetime.datetime.now()
                   - datetime.datetime.fromisoformat(data["asOf"])).total_seconds() / 3600
            if age < max_age_hours:
                return
        except Exception:
            pass
    threading.Thread(target=refresh_my_films, daemon=True).start()


def watch_my_films():
    """Check hourly whether your watched list is due, while the app is open."""
    while True:
        time.sleep(3600)
        try:
            maybe_refresh_my_films()
        except Exception:
            pass


# ------------------------------------------------- lists you liked

LIKED_LISTS = os.path.join(HOME, ".letterboxd_cache", "liked_lists.json")
liked_job = {"running": False, "error": ""}


def load_liked_lists():
    if os.path.exists(LIKED_LISTS):
        try:
            with open(LIKED_LISTS) as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _list_films(path, max_pages=320):
    """
    Every film in one Letterboxd list, in the order the list has them.
    Returns (name, films, truncated) — some lists run to hundreds of pages
    (20,000+ films), and stopping early without saying so would quietly
    report films as missing from a list they're in.
    """
    from bs4 import BeautifulSoup
    films, name, page, truncated = [], "", 1, False
    while True:
        if page > max_pages:
            truncated = True
            break
        status, page_html = _letterboxd_get(f"https://letterboxd.com{path}page/{page}/")
        if status != 200:
            break
        soup = BeautifulSoup(page_html, "html.parser")
        if page == 1:
            meta = soup.find("meta", property="og:title")
            if meta and meta.get("content"):
                name = re.sub(r"\s*[\u2022|]\s*Letterboxd\s*$", "", meta["content"]).strip()
        got = [d.get("data-item-slug") for d in soup.select("div[data-item-slug]")]
        got = [g for g in got if g]
        if not got:
            break
        films.extend(got)
        nxt = soup.select_one(".paginate-nextprev .next")
        if not (nxt and nxt.name == "a" and nxt.get("href")):
            break
        page += 1
        time.sleep(0.35)
    return name, films, truncated


def scrape_liked_lists():
    from bs4 import BeautifulSoup
    user, paths, page = scraper_username(), [], 1
    while page <= 20:
        status, page_html = _letterboxd_get(
            f"https://letterboxd.com/{user}/likes/lists/page/{page}/")
        if status != 200:
            raise LookupUnavailable("Letterboxd stopped responding while reading your liked lists.")
        found = sorted(set(re.findall(r'href="(/[^/"]+/list/[^/"]+/)"', page_html)))
        if not found:
            break
        paths.extend(found)
        soup = BeautifulSoup(page_html, "html.parser")
        nxt = soup.select_one(".paginate-nextprev .next")
        if not (nxt and nxt.name == "a" and nxt.get("href")):
            break
        page += 1
        time.sleep(0.4)

    out = []
    for path in sorted(set(paths)):
        name, films, truncated = _list_films(path)
        if films:
            out.append({"url": "https://letterboxd.com" + path,
                        "owner": path.strip("/").split("/")[0],
                        "name": name or path.strip("/").split("/")[-1].replace("-", " "),
                        "truncated": truncated,
                        "films": films})
    return out


def refresh_liked_lists():
    if liked_job["running"]:
        return
    liked_job.update(running=True, error="")
    try:
        lists = scrape_liked_lists()
        os.makedirs(os.path.dirname(LIKED_LISTS), exist_ok=True)
        with open(LIKED_LISTS, "w") as f:
            json.dump({"asOf": datetime.datetime.now().isoformat(timespec="seconds"),
                       "lists": lists}, f)
    except Exception as e:
        liked_job["error"] = str(e)
    finally:
        liked_job["running"] = False


def maybe_refresh_liked_lists(max_age_days=14):
    data = load_liked_lists()
    if data:
        try:
            if (datetime.datetime.now()
                    - datetime.datetime.fromisoformat(data["asOf"])).days < max_age_days:
                return
        except Exception:
            pass
    threading.Thread(target=refresh_liked_lists, daemon=True).start()


def lists_with(slug):
    """Which of your liked lists contain this film, and where in each."""
    data = load_liked_lists()
    if not data:
        return None
    hits = []
    for entry in data["lists"]:
        if slug in entry["films"]:
            hits.append({"name": entry["name"], "url": entry["url"], "owner": entry["owner"],
                         "position": entry["films"].index(slug) + 1,
                         "size": len(entry["films"]),
                         "truncated": entry.get("truncated", False)})
    hits.sort(key=lambda h: -h["size"])
    return hits


# ------------------------------------------------- playing in Madison

# The Horizon Line's weekly "Screening Report" lists every film showing in
# Madison, each linked to its Letterboxd page — so films match by slug exactly.
# Only facts are taken (film, venues, showtimes, section); nothing of the
# newsletter's writing is stored, and each entry links back to its report.
MADISON = os.path.join(HOME, ".letterboxd_cache", "madison.json")
MADISON_FEED = "https://horizonlinemadison.substack.com/feed"
madison_job = {"running": False, "error": ""}


def load_madison():
    if os.path.exists(MADISON):
        try:
            with open(MADISON) as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _mdy(s):
    m, d, y = (int(x) for x in s.split("/"))
    return datetime.date(y, m, d)


def _parse_report(post_html, post_url, post_title):
    from bs4 import BeautifulSoup, NavigableString, Tag
    soup = BeautifulSoup(post_html, "html.parser")
    entries, week, section = [], None, ""

    for block in soup.find_all(["p", "h2", "h3", "h4"]):
        text = block.get_text(" ", strip=True)
        head = re.match(r"(THIS WEEK|NEXT WEEK)\b[^()]*\(\s*(\d{1,2}/\d{1,2}/\d{4})\s*[-–]\s*"
                        r"(\d{1,2}/\d{1,2}/\d{4})\s*\)", text)
        if head and len(text) < 80:
            week = (head.group(1).title(), _mdy(head.group(2)), _mdy(head.group(3)))
            section = ""
            continue
        if week is None:
            continue
        if not block.find("a") and len(text) < 50:
            if text.isupper():            # a new all-caps heading ends the listings
                week = None
            elif block.find("em"):
                section = text            # "New Releases", "Repertory", ...
            continue

        parts = []

        def flush():
            films = [(re.search(r"letterboxd\.com/film/([^/?#]+)", href).group(1), label)
                     for kind, href, label in parts
                     if kind == "a" and re.search(r"letterboxd\.com/film/([^/?#]+)", href)]
            if films:
                line = re.sub(r"\s+", " ", " ".join(label for _, _, label in parts)).strip()
                title = films[-1][1]
                after = line.split(title, 1)[1] if title in line else ""
                note = ""
                paren = re.match(r"\s*\(([^)]{1,60})\)", after)
                if paren:
                    note, after = paren.group(1), after[paren.end():]
                after = after.strip(" -–—:")
                pieces = [v.strip(" -–—") for v in re.split(r"\s*,\s*", after) if v.strip(" -–—")]
                # "7p, 9:45p 10/13 at Flix" lists two times for one date: a
                # bare time belongs with the piece after it, not on its own.
                venues = []
                for v in pieces:
                    if venues and re.fullmatch(r"\d{1,2}(:\d{2})?\s*[ap]\.?m?\.?", venues[-1], re.I):
                        venues[-1] = f"{venues[-1]}, {v}"
                    else:
                        venues.append(v)
                # The newsletter links most venues to that film's page there
                # (AMC, Flix, Cinematheque…). Match each venue to the link whose
                # text it is: an exact match first, else the link containing it.
                vlinks = [(lbl.strip(), href) for kind, href, lbl in parts
                          if kind == "a" and href.startswith("http") and "letterboxd.com" not in href]
                links = {}
                for v in venues:
                    exact = [h for lbl, h in vlinks if lbl == v]
                    inside = sorted(((len(lbl), h) for lbl, h in vlinks if v in lbl or lbl in v), reverse=True)
                    if exact or inside:
                        links[v] = exact[0] if exact else inside[0][1]
                for slug, label in films:
                    entries.append({"slug": slug, "title": label, "section": section,
                                    "venues": venues, "links": links, "note": note, "week": week[0],
                                    "start": week[1].isoformat(), "end": week[2].isoformat(),
                                    "post": post_title, "url": post_url})
            parts.clear()

        for node in block.descendants:
            if isinstance(node, Tag) and node.name == "br":
                flush()
            elif isinstance(node, Tag) and node.name == "a":
                parts.append(("a", node.get("href", ""), node.get_text(" ", strip=True)))
            elif isinstance(node, NavigableString) and not node.find_parent("a"):
                t = str(node).strip()
                if t:
                    parts.append(("t", "", t))
        flush()
    return entries


def _norm_title(text):
    """Titles compared loosely: case, accents and punctuation don't count."""
    t = unicodedata.normalize("NFKD", text or "").lower()
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _film_identity(slug):
    """
    {"slug", "title", "year"} for a Letterboxd film, or None if Letterboxd
    didn't answer. The slug is where /film/<slug>/ ends up after redirects:
    films get renamed after release, and the newsletter may still link
    "untitled-resident-evil-reboot", which is now "resident-evil-2026".
    """
    global _lb_session
    with _lb_lock:
        for attempt in range(4):
            if _lb_session is None:
                _lb_session = net.session()
            try:
                r = _lb_session.get(f"https://letterboxd.com/film/{slug}/", timeout=20)
                if r.status_code == 200:
                    m = re.search(r"/film/([^/?#]+)/?", r.url)
                    name = re.search(r'<meta property="og:title" content="([^"]*)"', r.text)
                    title = html.unescape(name.group(1)) if name else ""
                    year = re.search(r"\((\d{4})\)\s*$", title)
                    return {"slug": m.group(1) if m else slug,
                            "title": re.sub(r"\s*\(\d{4}\)\s*$", "", title).strip(),
                            "year": year.group(1) if year else ""}
                if r.status_code == 404:
                    return {"slug": slug, "title": "", "year": ""}
            except Exception:
                _lb_session = None
            time.sleep(1.0 + attempt)
    return None


def refresh_madison():
    if madison_job["running"]:
        return
    madison_job.update(running=True, error="")
    try:
        import xml.etree.ElementTree as ET
        r = net.session().get(MADISON_FEED, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"the newsletter feed answered HTTP {r.status_code}")
        root = ET.fromstring(r.content)
        ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
        reports = [it for it in root.findall("./channel/item")
                   if (it.findtext("title") or "").lower().startswith("screening report")][:2]
        entries = []
        for it in reversed(reports):             # newer report wins on overlaps
            entries += _parse_report(it.findtext("content:encoded", namespaces=ns) or "",
                                     it.findtext("link") or "", it.findtext("title") or "")
        latest = reports[0] if reports else None

        # Two things go wrong with the newsletter's film links: a film renamed
        # after release ("untitled-resident-evil-reboot" is now
        # "resident-evil-2026"), and a link copied from the wrong film (one
        # week "Primetime" pointed at Verity's page). Ask Letterboxd once what
        # each unknown link actually is, remember the answer, and when the
        # linked film isn't the one the newsletter names, look the name up in
        # your own lists instead.
        book = dict((load_madison() or {}).get("films", {}))
        known, by_title = {}, {}
        for blob in get_index().values():
            for r in blob["rows"]:
                known.setdefault(r["slug"], r)
                by_title.setdefault(_norm_title(r["title"]), set()).add((r.get("year") or "", r["slug"]))
        for slug in {e["slug"] for e in entries}:
            if slug in known or slug in book:
                continue
            ident = _film_identity(slug)          # a failed lookup is retried next time
            if ident:
                book[slug] = ident
        for e in entries:
            moved = (book.get(e["slug"]) or {}).get("slug")
            if moved:
                e["slug"] = moved

        this_year = datetime.date.today().year
        fixed = []
        for e in entries:
            linked = (known.get(e["slug"]) or book.get(e["slug"]) or {}).get("title")
            if not linked or _norm_title(linked) == _norm_title(e["title"]):
                continue                          # linked film is the one named: nothing to do
            picks = sorted(by_title.get(_norm_title(e["title"]), ()), reverse=True)
            if e["section"] == "New Releases":
                recent = [p for p in picks if p[0] >= str(this_year - 1)]
                picks = recent[:1]                # a new release: the newest film by that name
            elif len(picks) > 1:
                picks = []                        # an old title shared by several films: leave it
            if picks and picks[0][1] != e["slug"]:
                fixed.append({"title": e["title"], "linked": e["slug"], "used": picks[0][1]})
                e["slug"] = picks[0][1]
        madison_job["fixed"] = fixed

        os.makedirs(os.path.dirname(MADISON), exist_ok=True)
        with open(MADISON, "w") as f:
            json.dump({"asOf": datetime.datetime.now().isoformat(timespec="seconds"),
                       "latest": {"title": latest.findtext("title"), "url": latest.findtext("link")}
                                 if latest is not None else None,
                       "films": book, "fixed": fixed, "entries": entries}, f)
    except Exception as e:
        madison_job["error"] = str(e)
    finally:
        madison_job["running"] = False


def maybe_refresh_madison(max_age_hours=12):
    data = load_madison()
    if data:
        try:
            age = datetime.datetime.now() - datetime.datetime.fromisoformat(data["asOf"])
            if age.total_seconds() < max_age_hours * 3600:
                return
        except Exception:
            pass
    threading.Thread(target=refresh_madison, daemon=True).start()


TICK = os.path.join(HOME, "Desktop", "letterboxd_tick.py")


def run_tick():
    """
    Start the hourly housekeeping (letterboxd_tick.py) in the background.
    launchd runs it too; this is the safety net, so overdue data gets refreshed
    whenever the app is in use even if launchd didn't fire. The tick's own
    lock means two can never run at once. Detached, so quitting the app
    doesn't cut a scrape short.
    """
    if not os.path.exists(TICK):
        return
    try:
        log = open(os.path.join(HOME, "letterboxd_refresh.log"), "a")
        subprocess.Popen([sys.executable, "-u", TICK], stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, cwd=HOME, start_new_session=True)
    except Exception:
        pass


def keep_ticking(every_minutes=60):
    while True:
        time.sleep(every_minutes * 60)
        run_tick()


def watch_madison(every_minutes=60):
    """
    Re-read the feed every hour while the app is open. Unconditional on
    purpose: a "skip if checked recently" rule with the same interval skips
    every other check, which is how a new report went unnoticed for hours.
    """
    while True:
        time.sleep(every_minutes * 60)
        try:
            refresh_madison()
        except Exception:
            pass


def madison_listings():
    """What's playing now and next, each film ranked by how many friends watched it."""
    data = load_madison() or {}
    today = datetime.date.today()
    best, stats = {}, {}
    for period, blob in get_index().items():
        for r in blob["rows"]:
            if r["count"] > best.get(r["slug"], (0,))[0]:
                best[r["slug"]] = (r["count"], period)
            # friends' average and liked share: the year list if it has them
            if (r.get("rating") is not None or r.get("liked") is not None) and \
                    (r["slug"] not in stats or not period.endswith("s")):
                stats[r["slug"]] = {"rating": r.get("rating"), "ratings": r.get("ratings", 0),
                                    "liked": r.get("liked")}

    # Two guides overlap: last week's issue previews a week that this week's
    # issue then lists properly. For any week, use only the newest guide that
    # covers it. Then "now" is the week containing today, and "next" is only
    # the week right after it — not every future week lumped together.
    def issue_date(e):
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", e.get("post", ""))
        return datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else datetime.date.min
    entries = data.get("entries", [])
    newest = {}
    for e in entries:
        wk = (e["start"], e["end"])
        newest[wk] = max(newest.get(wk, datetime.date.min), issue_date(e))
    entries = [e for e in entries if issue_date(e) == newest[(e["start"], e["end"])]]
    weeks = sorted({(datetime.date.fromisoformat(e["start"]), datetime.date.fromisoformat(e["end"]))
                    for e in entries})
    now_wk = next((w for w in weeks if w[0] <= today <= w[1]), None)
    next_wk = next((w for w in weeks if w[0] > today), None)

    now, soon = {}, {}
    for e in entries:
        start, end = datetime.date.fromisoformat(e["start"]), datetime.date.fromisoformat(e["end"])
        bucket = now if (start, end) == now_wk else soon if (start, end) == next_wk else None
        if bucket is None:
            continue
        # Each venue is judged on its own. A dated screening ("7p 10/1 at UW
        # Cinematheque") is over once its dates pass; a plain theatre name
        # ("Marcus Point") is an ongoing run. A film drops off "now" only when
        # every venue is a screening that has already happened.
        def venue_times(v):
            """Each screening in a venue line as a datetime: "9:15p 10/9" is
            21:15 on Oct 9; a date with no time counts until the end of the day."""
            out = []
            for hh, mm, ap, mo, dy in re.findall(
                    r"(?:\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m?\.?\s+)?\b(\d{1,2})/(\d{1,2})\b", v, re.I):
                try:
                    d = datetime.date(start.year, int(mo), int(dy))
                    if d < start - datetime.timedelta(days=60):
                        d = d.replace(year=d.year + 1)
                    if hh:
                        h = int(hh) % 12 + (12 if ap.lower() == "p" else 0)
                        t = datetime.time(h, int(mm or 0))
                    else:
                        t = datetime.time(23, 59)
                    out.append(datetime.datetime.combine(d, t))
                except ValueError:
                    pass
            return out
        moment = datetime.datetime.now()
        live, upcoming = [], []
        for v in e["venues"]:
            ts = venue_times(v)
            if bucket is now and ts and not any(t >= moment for t in ts):
                continue                       # that screening has started or finished
            live.append(v)
            upcoming += [t.date() for t in ts if t >= moment]
        if bucket is now and e["venues"] and not live:
            continue
        item = bucket.setdefault(e["slug"], {**e, "venues": [], "links": {}, "next": ""})
        for v in live:
            if v not in item["venues"]:
                item["venues"].append(v)
            if (e.get("links") or {}).get(v):
                item["links"][v] = e["links"][v]
        if upcoming:
            nxt = min(upcoming).isoformat()
            item["next"] = min(item["next"], nxt) if item["next"] else nxt
        seats = best.get(e["slug"])
        item["seats"], item["seatsIn"] = (seats if seats else (0, ""))
        item.update(stats.get(e["slug"], {"rating": None, "ratings": 0, "liked": None}))

    order = lambda d: sorted(d.values(), key=lambda x: (-x["seats"], x["title"].lower()))
    return {"asOf": data.get("asOf", ""), "latest": data.get("latest"),
            "now": order(now), "soon": order(soon),
            "running": madison_job["running"], "error": madison_job["error"]}


# ------------------------------------------------- are the scheduled runs ok?

def health():
    saved = saved_periods()
    last = max((s["mtime"] for s in saved), default=0)

    outcome, finished, tail = "unknown", 0, []
    log_path = os.path.join(HOME, "letterboxd_refresh.log")
    if os.path.exists(log_path):
        with open(log_path, errors="replace") as f:
            lines = [l.rstrip() for l in f.readlines()[-400:]]
        tail = lines[-10:]
        outcome_at = 0.0
        for line in reversed(lines):
            stamp = re.match(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]", line)
            at = (datetime.datetime.strptime(stamp.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                  if stamp else 0.0)
            if "FAILED" in line:
                outcome, outcome_at = "failed", at
                break
            if "INCOMPLETE friends" in line:
                outcome, outcome_at = "incomplete", at
                break
            if "[verified]" in line or "Every friend fetched completely" in line:
                outcome, outcome_at = "clean", at
                break
        # A bad run followed by newer data (e.g. a manual re-scrape) is resolved.
        if outcome in ("failed", "incomplete") and outcome_at and last > outcome_at + 60:
            outcome = "recovered"
        finished = os.path.getmtime(log_path)

    try:
        listed = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    except Exception:
        listed = ""
    last_tick = 0.0
    if os.path.exists(log_path):
        for line in reversed(lines):
            stamp = re.match(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] tick", line)
            if stamp:
                last_tick = datetime.datetime.strptime(stamp.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                break

    now = time.time()
    mtimes = {s_["period"]: s_["mtime"] for s_ in saved}
    year = str(datetime.date.today().year)
    decades = [f"{d}s" for d in range(1920, datetime.date.today().year // 10 * 10 + 1, 10)]
    ages = {d: (now - mtimes[d]) / 86400 if d in mtimes else None for d in decades}
    stale = sorted((d for d, a in ages.items() if a is None or a > 14),
                   key=lambda d: -(ages[d] if ages[d] is not None else 1e9))

    return {"lastData": last, "lastLog": finished, "outcome": outcome,
            "tickLoaded": "local.letterboxd.tick" in listed, "lastTick": last_tick,
            "yearAge": (now - mtimes[year]) / 86400 if year in mtimes else None,
            "staleDecades": stale,
            "oldest": [stale[0], ages[stale[0]]] if stale else None,
            "periods": len(saved), "tail": tail, "check": last_selfcheck()}


def last_selfcheck():
    """The hourly self-check's findings, so the strip can show real problems."""
    try:
        with open(os.path.join(HOME, ".letterboxd_cache", "selfcheck.json")) as f:
            res = json.load(f)
    except Exception:
        return None
    live = []
    for p in res.get("problems", []):
        period = p.get("period")
        if period and os.path.exists(csv_path(period)) and \
                (time.time() - os.path.getmtime(csv_path(period))) / 3600 < 36:
            continue          # refreshed since the check ran — no longer true
        live.append(p)
    return {"at": res.get("at"), "ok": not any(p["level"] == "serious" for p in live),
            "problems": live[:4]}


# ------------------------------------------------- who watched a given film

def watchers(period, slug):
    """Friends who logged this film, from the scrape's own audit file."""
    path = os.path.join(HOME, f"friends_watched_{period}_per_friend.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            per_friend = json.load(f)
    except Exception:
        return None
    try:
        import letterboxd_people
        skip = letterboxd_people.excluded()
    except Exception:
        skip = set()
    out = []
    for user, films in per_friend.items():
        if slug not in films or user in skip:
            continue
        mark = films[slug] if isinstance(films, dict) else None
        rating, liked = ((list(mark) + [0, 0])[:2] if isinstance(mark, (list, tuple)) else (0, 0))
        out.append({"user": user, "stars": (rating / 2) if rating else None, "liked": bool(liked)})
    return sorted(out, key=lambda d: d["user"].lower())


_rising_cache = {}


def rising(days=7, limit=60):
    """
    The biggest gainers across every list, not just the one on screen. A film
    in both a year and its decade counts once, kept at its largest gain.
    """
    key = (days, limit)
    hit = _rising_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]

    best, oldest_used = {}, None
    for s_ in saved_periods():
        period = s_["period"]
        cur = load_csv(csv_path(period))
        base, label = baseline_for(period, f"{days}d")
        prev = load_csv(base) if base else None
        if not cur or not prev:
            continue
        stamp = os.path.getmtime(base)
        oldest_used = stamp if oldest_used is None else min(oldest_used, stamp)
        for slug, c in cur.items():
            p_ = prev.get(slug)
            delta = c["count"] - p_["count"] if p_ else c["count"]
            if delta <= 0:
                continue
            row = {"slug": slug, "title": c["title"], "year": c["year"], "count": c["count"],
                   "delta": delta, "period": period, "isNew": p_ is None,
                   "rating": c.get("rating"), "ratings": c.get("ratings", 0),
                   "liked": c.get("liked"), "seats": c["count"]}
            if slug not in best or delta > best[slug]["delta"]:
                best[slug] = row
    rows = sorted(best.values(), key=lambda r: (-r["delta"], -r["count"]))[:limit]
    out = {"days": days, "since": oldest_used, "rows": rows, "films": len(best)}
    _rising_cache[key] = (time.time(), out)
    return out


def baseline_for(period, since):
    """
    What to compare against: the previous run ("last"), or the newest dated
    copy from at least N days ago ("7d", "30d"). Falls back to the oldest copy
    there is, so a young history still says something useful.
    """
    if since in (None, "", "last"):
        p = prev_path(period)
        return (p, "the previous scrape") if os.path.exists(p) else (None, "")
    try:
        import letterboxd_history
    except Exception:
        return (None, "")
    days = int(str(since).rstrip("d") or 7)
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    hit = letterboxd_history.at_or_before(period, cutoff) or letterboxd_history.oldest(period)
    if not hit:
        return (None, "")
    date, path = hit
    age = (datetime.date.today() - date).days
    label = ("a week ago" if age == 7 else "a month ago" if age == 30
             else f"{age} days ago" if age else "earlier today")
    if age < days - 1:
        label += " — as far back as this list goes"
    return (path, f"{label} ({date.isoformat()})")


def compute_diff(period, since="last"):
    """What changed between an earlier state of this list and the current one."""
    cur = load_csv(csv_path(period))
    base, label = baseline_for(period, since)
    prev = load_csv(base) if base else None
    if cur is None or prev is None:
        return None

    gained, dropped, added = [], [], []
    for slug, c in cur.items():
        p = prev.get(slug)
        # today's friends' average and liked share, for showing beside the change
        extra = {"rating": c.get("rating"), "ratings": c.get("ratings", 0),
                 "liked": c.get("liked"), "seats": c["count"]}
        if p is None:
            added.append({"slug": slug, "title": c["title"], "year": c["year"],
                          "count": c["count"], "delta": c["count"], **extra})
        elif c["count"] != p["count"]:
            entry = {"slug": slug, "title": c["title"], "year": c["year"],
                     "count": c["count"], "delta": c["count"] - p["count"], **extra}
            (gained if entry["delta"] > 0 else dropped).append(entry)

    gained.sort(key=lambda d: -d["delta"])
    added.sort(key=lambda d: -d["count"])
    dropped.sort(key=lambda d: d["delta"])
    return {
        "period": period, "since": since or "last", "baseline": label,
        "prevTime": os.path.getmtime(base),
        "curTime": os.path.getmtime(csv_path(period)),
        "prevFilms": len(prev), "curFilms": len(cur),
        "gained": gained[:40], "added": added[:40], "dropped": dropped[:20],
        "nGained": len(gained), "nAdded": len(added), "nDropped": len(dropped),
    }


# ------------------------------------------------------------------ job

def run_scraper(period):
    # Snapshot the existing results so we can diff against them afterwards.
    if os.path.exists(csv_path(period)):
        os.makedirs(HISTORY, exist_ok=True)
        shutil.copy2(csv_path(period), prev_path(period))

    with job_lock:
        job.update(running=True, period=period, lines=[], started=time.time(),
                   finished=None, returncode=None)
    try:
        proc = subprocess.Popen([sys.executable, "-u", SCRAPER, period],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, cwd=HOME)
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                with job_lock:
                    job["lines"].append(line)
        proc.wait()
        rc = proc.returncode
        if rc == 0:
            try:
                import letterboxd_history
                letterboxd_history.save(period)     # dated copy for week/month diffs
            except Exception:
                pass
    except Exception as e:
        with job_lock:
            job["lines"].append(f"ERROR: {e}")
        rc = -1
    with job_lock:
        job.update(running=False, finished=time.time(), returncode=rc)


def job_snapshot():
    with job_lock:
        lines = list(job["lines"])
        snap = {k: job[k] for k in ("running", "period", "started", "finished", "returncode")}
    snap["lines"] = lines

    quality = None
    for ln in lines:
        s = ln.strip()
        if "Every friend fetched completely" in s:
            quality = {"ok": True, "text": "Every friend fetched completely — counts verified."}
        elif s.startswith("INCOMPLETE friends") and not s.endswith(": 0"):
            quality = {"ok": False, "text": s}
        elif "MISSING" in s and "profile" in s:
            quality = {"ok": False, "text": s}
    snap["quality"] = quality

    snap["progress"] = None
    for ln in reversed(lines):
        m = re.search(r"(\d+)/(\d+) friends", ln)
        if m:
            snap["progress"] = {"done": int(m.group(1)), "total": int(m.group(2))}
            break
    return snap


# ------------------------------------------------------------------ server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        period = (q.get("period") or [""])[0]

        if u.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if u.path == "/api/saved":
            return self._send(200, saved_periods())
        if u.path == "/api/status":
            return self._send(200, job_snapshot())
        if u.path == "/api/results":
            rows = read_results(period)
            if rows is None:
                return self._send(404, {"error": f"no results for {period}"})
            return self._send(200, {"period": period, "rows": rows,
                                    "mtime": os.path.getmtime(csv_path(period)),
                                    "hasPrev": os.path.exists(prev_path(period))})
        if u.path == "/api/search":
            return self._send(200, {"hits": search_films((q.get("q") or [""])[0])})
        if u.path == "/api/person":
            name = (q.get("name") or [""])[0]
            role = (q.get("role") or [""])[0]
            if len(name.strip()) < 2:
                return self._send(400, {"error": "name too short"})
            try:
                res = person_films(name, role)
            except LookupUnavailable as e:
                return self._send(503, {"error": str(e)})
            except Exception as e:
                return self._send(502, {"error": f"lookup failed: {e}"})
            if res is None:
                return self._send(404, {"error": f'No Letterboxd page for "{name}"'})
            return self._send(200, res)
        if u.path == "/api/seen":
            return self._send(200, my_films_payload())
        if u.path == "/api/lists":
            hits = lists_with((q.get("slug") or [""])[0])
            data = load_liked_lists() or {}
            return self._send(200, {"lists": hits, "asOf": data.get("asOf", ""),
                                    "total": len(data.get("lists", [])),
                                    "running": liked_job["running"], "error": liked_job["error"]})
        if u.path == "/api/posters":
            # which films have a poster cached locally, so rows only ask for real ones
            try:
                import letterboxd_posters as lp
                have = [os.path.basename(f)[:-4] for f in glob.glob(os.path.join(lp.CACHE, "*.jpg"))]
            except Exception:
                have = []
            return self._send(200, {"slugs": have})
        if u.path.startswith("/poster/"):
            slug = urllib.parse.unquote(u.path[len("/poster/"):])
            slug = slug[:-4] if slug.endswith(".jpg") else slug
            path = os.path.join(HOME, ".letterboxd_cache", "posters", slug + ".jpg")
            if "/" in slug or ".." in slug or not os.path.exists(path):
                return self._send(404, {"error": "no poster"})
            with open(path, "rb") as f:
                blob = f.read()
            return self._send(200, blob, "image/jpeg", {"Cache-Control": "max-age=86400"})
        if u.path == "/api/rising":
            days = int((q.get("days") or ["7"])[0])
            return self._send(200, rising(days))
        if u.path == "/api/madison":
            return self._send(200, madison_listings())
        if u.path == "/api/health":
            return self._send(200, health())
        if u.path == "/api/watchers":
            who = watchers(period, (q.get("slug") or [""])[0])
            if who is None:
                return self._send(404, {"error": "no audit file for this list yet — "
                                                 "it appears after the next scrape"})
            return self._send(200, {"watchers": who})
        if u.path == "/api/diff":
            d = compute_diff(period, (q.get("since") or ["last"])[0])
            if d is None:
                return self._send(404, {"error": "no previous run to compare against"})
            return self._send(200, d)
        if u.path == "/api/download":
            path = csv_path(period)
            if not os.path.exists(path):
                return self._send(404, {"error": "not found"})
            with open(path, "rb") as f:
                data = f.read()
            return self._send(200, data, "text/csv",
                              {"Content-Disposition":
                               f'attachment; filename="friends_watched_{period}.csv"'})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)

        if u.path == "/api/lists/refresh":
            threading.Thread(target=refresh_liked_lists, daemon=True).start()
            return self._send(200, {"started": True})

        if u.path == "/api/madison/refresh":
            threading.Thread(target=refresh_madison, daemon=True).start()
            return self._send(200, {"started": True})

        if u.path == "/api/seen/refresh":
            threading.Thread(target=refresh_my_films, daemon=True).start()
            return self._send(200, {"started": True})

        if u.path == "/api/quit":
            self._send(200, {"bye": True})
            threading.Thread(target=lambda: (time.sleep(0.3), server.shutdown()),
                             daemon=True).start()
            return

        if u.path != "/api/run":
            return self._send(404, {"error": "not found"})

        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})

        period = str(payload.get("period", "")).strip().lower()
        if not re.fullmatch(r"[0-9]{4}s?", period):
            return self._send(400, {"error": 'Use a year like "2024" or a decade like "1990s".'})
        with job_lock:
            if job["running"]:
                return self._send(409, {"error": f"already running {job['period']}"})
        threading.Thread(target=run_scraper, args=(period,), daemon=True).start()
        return self._send(200, {"started": period})


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Letterboxd — Friends Watched</title>
<style>
  :root{--bg:#14181c;--panel:#1c2228;--panel2:#20262d;--line:#2c343c;
        --text:#e8eef4;--dim:#9ab;--green:#00e054;--orange:#ff8000;--blue:#40bcf4;--red:#ff5c5c}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
  header{padding:20px 28px;border-bottom:1px solid var(--line);
         display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
  h1{margin:0;font-size:19px;letter-spacing:.3px}
  h1 span{color:var(--green)}
  .sub{color:var(--dim);font-size:13px}
  .quit{margin-left:auto;color:var(--dim);font-size:12px;cursor:pointer}
  .quit:hover{color:var(--red)}
  main{padding:22px 28px;max-width:1100px;margin:0 auto}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
        padding:18px;margin-bottom:18px}
  label{display:block;color:var(--dim);font-size:12px;text-transform:uppercase;
        letter-spacing:.7px;margin-bottom:8px}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  input[type=text]{background:var(--panel2);border:1px solid var(--line);color:var(--text);
        padding:10px 12px;border-radius:7px;font-size:15px;width:170px;outline:none}
  input[type=text]:focus{border-color:var(--green)}
  button{background:var(--green);color:#08210f;border:0;padding:10px 18px;border-radius:7px;
         font-weight:700;font-size:14px;cursor:pointer}
  button:disabled{opacity:.45;cursor:not-allowed}
  button.ghost{background:transparent;color:var(--text);border:1px solid var(--line);font-weight:500}
  button.ghost:hover:not(:disabled){border-color:var(--green);color:var(--green)}
  select{background:var(--panel2);border:1px solid var(--line);color:var(--text);
         padding:10px 12px;border-radius:7px;font-size:15px;min-width:300px;outline:none;
         font-variant-numeric:tabular-nums;cursor:pointer}
  select:focus{border-color:var(--green)}
  optgroup{color:var(--dim);font-style:normal}
  .dl a{color:var(--text);text-decoration:none}
  .dl a:hover{color:var(--blue);text-decoration:underline}
  .riselist{display:grid;gap:2px}
  .rise{display:flex;align-items:center;gap:10px;padding:5px 0;border-bottom:1px solid var(--line)}
  .rise:last-child{border-bottom:0}
  .rise img,.rise .noimg{flex:none;width:28px;height:42px;border-radius:2px;background:var(--panel2);object-fit:cover}
  .rise .up{color:var(--green);font-weight:600;font-size:13px;min-width:34px;text-align:right}
  .rise .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .rise .t a{color:var(--text);text-decoration:none}
  .rise .t a:hover{color:var(--blue);text-decoration:underline}
  .rise .where{color:var(--dim);font-size:12px;white-space:nowrap}
  .mark{color:var(--orange);font-size:11.5px;margin-left:3px;white-space:nowrap}
  .mv a{color:var(--dim);text-decoration:underline;text-decoration-color:var(--line);text-underline-offset:2px}
  .mv a:hover{color:var(--blue);text-decoration-color:currentColor}
  .hitstats{margin-left:10px;font-size:12.5px;color:var(--dim);white-space:nowrap}
  .hitstats b{color:var(--text);font-weight:600}
  .hitstats .heart{color:var(--red);margin-left:4px}
  td.rate{width:60px;color:var(--dim);font-size:12.5px;white-space:nowrap;text-align:right}
  td.rate b{color:var(--text);font-weight:600;font-size:13px}
  td.rate .heart{color:var(--red)}
  td.rate .few{opacity:.45;font-weight:400}
  .recbtn{all:unset;cursor:pointer;margin-left:auto;padding:5px 12px;border-radius:6px;font-size:12.5px;
    font-weight:600;color:#14181c;background:var(--green);white-space:nowrap}
  .recbtn:hover{filter:brightness(1.1)}
  .recbtn.on{background:transparent;color:var(--green);box-shadow:inset 0 0 0 1px var(--green)}
  .recbtn:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
  .recnote{margin:4px 0 10px;padding:8px 12px;border-radius:6px;background:var(--panel2);
    color:var(--dim);font-size:13px}
  .recnote b{color:var(--text)}
  .since{display:inline-flex;gap:0;border:1px solid var(--line);border-radius:6px;overflow:hidden}
  .since button{all:unset;cursor:pointer;padding:4px 10px;font-size:11.5px;color:var(--dim);
    letter-spacing:.3px;white-space:nowrap}
  .since button:hover{color:var(--text);background:var(--panel2)}
  .since button.on{background:var(--green);color:#14181c;font-weight:600}
  .since button+button{border-left:1px solid var(--line)}
  option{color:var(--text);background:var(--panel)}
  .chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:12px}
  .chip{background:var(--panel2);border:1px solid var(--line);color:var(--dim);
        padding:5px 11px;border-radius:20px;cursor:pointer;font-size:12.5px;position:relative}
  .chip:hover{border-color:var(--green);color:var(--green)}
  .chip.has{color:var(--text);border-color:#3a4650}
  .chip .age{font-size:10px;opacity:.55;margin-left:5px}
  .chip.stale .age{color:var(--orange);opacity:.9}
  pre{background:#0e1216;border:1px solid var(--line);border-radius:7px;padding:12px;
      max-height:210px;overflow:auto;font-size:12px;color:#b9c6d2;margin:0;white-space:pre-wrap}
  .bar{height:6px;background:var(--panel2);border-radius:3px;overflow:hidden;margin:12px 0 6px}
  .bar>i{display:block;height:100%;background:var(--green);width:0;transition:width .4s}
  .flag{padding:10px 13px;border-radius:7px;font-size:13px;margin-bottom:14px}
  .flag.ok{background:rgba(0,224,84,.10);border:1px solid rgba(0,224,84,.35);color:#7ff0ab}
  .flag.bad{background:rgba(255,128,0,.10);border:1px solid rgba(255,128,0,.4);color:#ffb866}
  table{width:100%;border-collapse:collapse}
  th{text-align:left;color:var(--dim);font-size:11.5px;text-transform:uppercase;
     letter-spacing:.6px;padding:8px 10px;border-bottom:1px solid var(--line);
     cursor:pointer;user-select:none;white-space:nowrap}
  th:hover{color:var(--green)}
  td{padding:9px 10px;border-bottom:1px solid #232a31;vertical-align:middle}
  tr:hover td{background:#1f262d}
  .rank{color:var(--dim);font-variant-numeric:tabular-nums;width:44px}
  .yr{color:var(--dim);font-variant-numeric:tabular-nums;width:56px}
  .cnt{font-variant-numeric:tabular-nums;text-align:right;width:56px;font-weight:700}
  .meter{width:130px}
  .meter>i{display:block;height:8px;background:var(--orange);border-radius:2px}
.health{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;padding:9px 13px;
        border-radius:7px;font-size:13px;margin-bottom:18px;border:1px solid var(--line);
        background:var(--panel);color:var(--dim)}
  .health b{color:var(--text);font-weight:600}
  .health .dot{width:8px;height:8px;border-radius:50%;background:var(--green);flex:none}
  .health.warn{border-color:rgba(255,128,0,.45);background:rgba(255,128,0,.08)}
  .health.warn .dot{background:var(--orange)}
  .health .sep{color:var(--line)}
  .chk{display:inline-flex;align-items:center;gap:7px;color:var(--dim);font-size:13px;cursor:pointer}
  .chk input{accent-color:var(--green);width:15px;height:15px;cursor:pointer}
  tr.seen td.film a{color:var(--dim)}
  td.film{vertical-align:middle}
  td.film > .pos{display:inline-block;vertical-align:middle;margin-right:12px;width:34px;height:51px;
    border-radius:3px;background:var(--panel2);object-fit:contain;object-position:center;
    box-shadow:0 0 0 1px rgba(255,255,255,.07)}
  td.film > .fi{display:inline-block;vertical-align:middle}
  tbody.posters td{padding-block:6px}
  tbody:not(.posters) td.film > .pos{display:none}
  .tick{color:var(--green);font-size:11px;margin-right:6px}
  .who,.lists{all:unset;cursor:pointer;color:var(--blue);font-size:12px;margin-left:8px;white-space:nowrap}
  .who:hover,.lists:hover{text-decoration:underline}
  .who:focus-visible,.lists:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
  .pos{color:var(--dim);font-family:ui-monospace,Menlo,monospace;font-size:11px}
  .madh{margin:0;font-size:13px;letter-spacing:.5px;text-transform:uppercase}
  .madlist{display:grid;gap:2px}
  .mad{display:grid;grid-template-columns:44px minmax(0,1fr);gap:12px;align-items:baseline;
       padding:7px 0;border-bottom:1px solid #232a31}
  .mad:last-child{border-bottom:0}
  .mseats{font-family:ui-monospace,Menlo,monospace;font-weight:700;text-align:right;color:var(--text)}
  .mseats.none{color:var(--dim);font-weight:400}
  .mt a{color:var(--text);text-decoration:none}
  .mt a:hover{color:var(--blue)}
  .mv{color:var(--dim);font-size:12.5px;margin-top:1px}
  .tag{display:inline-block;font-size:10.5px;color:var(--dim);border:1px solid var(--line);
       border-radius:3px;padding:0 5px;margin-left:6px;vertical-align:1px}
  .tag.today{color:var(--green);border-color:rgba(0,224,84,.5)}
  .madb{all:unset;cursor:pointer;font-size:11px;border-radius:3px;padding:1px 6px;margin-left:8px;
        white-space:nowrap;vertical-align:1px}
  .madb.now{color:#08210f;background:var(--green);font-weight:600}
  .madb.soon{color:var(--green);border:1px solid rgba(0,224,84,.5)}
  .madb:focus-visible{outline:2px solid var(--green);outline-offset:2px}
  tr.wrow td{background:#1a2027;font-size:13px;line-height:1.85;color:var(--dim);padding:10px 10px 14px}
  tr.wrow a{color:var(--text);text-decoration:none;border-bottom:1px solid var(--line)}
  tr.wrow a:hover{border-bottom-color:var(--blue);color:var(--blue)}
  .more{display:none;gap:12px;align-items:center;justify-content:center;padding:16px 0 4px}
  .up{color:var(--green);font-weight:700;font-variant-numeric:tabular-nums}
  .down{color:var(--red);font-weight:700;font-variant-numeric:tabular-nums}
  .new{color:var(--blue);font-size:10px;border:1px solid var(--blue);border-radius:3px;
       padding:0 4px;margin-left:6px;vertical-align:middle}
  a{color:var(--blue);text-decoration:none}
  a:hover{text-decoration:underline}
  .muted{color:var(--dim)}
  .stats{display:flex;gap:20px;flex-wrap:wrap;color:var(--dim);font-size:13px;margin-bottom:12px}
  .stats b{color:var(--text);font-variant-numeric:tabular-nums}
  .cols{display:grid;grid-template-columns:1fr 1fr;gap:20px}
  .cols h4{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--dim)}
  .dl{font-size:13px;line-height:1.9;max-height:230px;overflow:auto}
  .modes{display:inline-flex;border:1px solid var(--line);border-radius:7px;overflow:hidden}
  .mode{background:var(--panel2);color:var(--dim);border:0;border-radius:0;padding:9px 14px;
        font-weight:500;font-size:13px}
  .mode+.mode{border-left:1px solid var(--line)}
  .mode.on{background:var(--green);color:#08210f;font-weight:700}
  .hit{padding:8px 0;border-bottom:1px solid #232a31}
  .hit:last-child{border-bottom:0}
  .hit .t{font-size:14px}
  .pos{display:inline-block;background:var(--panel2);border:1px solid var(--line);
       border-radius:5px;padding:1px 8px;margin:2px 6px 2px 0;font-size:12px;
       font-variant-numeric:tabular-nums}
  .pos b{color:var(--green)}
  .pos.none{color:var(--dim);opacity:.7}
  @media(max-width:720px){.cols{grid-template-columns:1fr}}
</style></head><body>
<header>
  <h1>Letterboxd · <span>Friends Watched</span></h1>
  <div class="sub">what the people you follow have actually seen</div>
  <div class="quit" id="quit">quit app ✕</div>
</header>
<main>

  <div class="health" id="health" hidden></div>

  <div class="card" id="riseCard" hidden>
    <div class="row" style="justify-content:space-between;align-items:baseline;margin-bottom:8px">
      <h4 class="madh">Rising across every list</h4>
      <span id="riseSince" class="since">
        <button type="button" data-days="7" class="on">last 7 days</button>
        <button type="button" data-days="30">last 30 days</button>
      </span>
      <span class="muted" id="riseMeta"></span>
    </div>
    <div class="riselist" id="riseList"></div>
    <div class="row" style="margin-top:10px;font-size:13px"><a href="#" id="riseMore"></a></div>
  </div>

  <div class="card" id="madCard" hidden>
    <div class="row" style="justify-content:space-between;align-items:baseline;margin-bottom:8px">
      <h4 class="madh" id="madHead">Playing in Madison</h4>
      <span class="muted" id="madMeta"></span>
    </div>
    <div class="madlist" id="madList"></div>
    <div class="row" style="margin-top:10px;gap:18px;font-size:13px">
      <a href="#" id="madMore"></a>
      <a href="#" id="madToggle"></a>
    </div>
  </div>

  <div class="card">
    <label>Year or decade</label>
    <div class="row">
      <select id="pick"></select>
      <button id="run">Scrape</button>
      <span class="muted" id="hint"></span>
    </div>
    <div id="progwrap" style="display:none">
      <div class="bar"><i id="bar"></i></div>
      <pre id="log"></pre>
    </div>
  </div>

  <div class="card">
    <label>Look up a film or a person</label>
    <div class="row">
      <input type="text" id="q" placeholder="Interstellar / Kubrick…" style="width:250px">
      <span class="modes">
        <button class="mode on" data-m="film">Film</button><button class="mode"
          data-m="director">Director</button><button class="mode" data-m="actor">Actor</button>
      </span>
      <button class="ghost" id="go">Search</button>
      <span class="muted" id="qhint"></span>
    </div>
    <div id="qout" class="dl" style="margin-top:14px;max-height:420px"></div>
  </div>

  <div class="card" id="diffCard" style="display:none">
    <div class="row" style="justify-content:space-between;margin-bottom:12px">
      <h4 style="margin:0;font-size:13px;letter-spacing:.5px">WHAT CHANGED</h4>
      <span id="diffSince" class="since">
        <button type="button" data-since="last" class="on">since last scrape</button>
        <button type="button" data-since="7d">last 7 days</button>
        <button type="button" data-since="30d">last 30 days</button>
      </span>
      <span class="muted" id="diffMeta"></span>
    </div>
    <div class="cols">
      <div><h4 id="gainHead">Gained watches</h4><div class="dl" id="gained"></div></div>
      <div><h4 id="addHead">New films</h4><div class="dl" id="added"></div></div>
    </div>
  </div>

  <div class="card" id="resultsCard" style="display:none">
    <div id="quality"></div>
    <div class="stats">
      <span>period <b id="sPeriod">—</b></span>
      <span>films <b id="sFilms">—</b></span>
      <span>top count <b id="sTop">—</b></span>
      <span>scraped <b id="sAge">—</b></span>
      <span><a id="dl" href="#">download CSV</a></span>
      <span><a id="showDiff" href="#" style="display:none">what changed?</a></span>
    </div>
    <div class="row" style="margin-bottom:10px">
      <input type="text" id="filter" placeholder="filter titles…" style="width:240px">
      <span class="muted">seen by at least</span>
      <select id="minSeen" style="min-width:0;font-size:14px;padding:9px 10px">
        <option value="1">all</option>
        <option value="2">2 friends</option>
        <option value="5">5 friends</option>
        <option value="10">10 friends</option>
        <option value="25">25 friends</option>
        <option value="30">30 friends</option>
        <option value="50">50 friends</option>
      </select>
      <label class="chk"><input type="checkbox" id="hideSeen"> Hide films I've seen</label>
      <label class="chk"><input type="checkbox" id="onlyMadison"> Playing in Madison</label>
      <label class="chk"><input type="checkbox" id="showPosters" checked> Posters</label>
      <button type="button" id="recBtn" class="recbtn">Recommend me something</button>
      <span class="muted" id="shown"></span>
    </div>
    <div class="recnote" id="recNote" hidden></div>
    <table>
      <thead><tr>
        <th style="width:44px">#</th><th data-k="title">Film</th>
        <th data-k="year" style="width:56px">Year</th>
        <th data-k="rating" style="width:62px;text-align:right" title="friends' average; films with fewer than 10 ratings sort last">Rating</th>
        <th data-k="liked" style="width:58px;text-align:right" title="share of watchers who liked it; films with fewer than 10 watchers sort last">Liked</th>
        <th data-k="count" style="width:56px">Seen</th>
        <th style="width:130px"></th>
      </tr></thead>
      <tbody id="tb"></tbody>
    </table>
    <div class="more" id="more">
      <button class="ghost" id="loadMore">Load more</button>
      <span class="muted" id="moreInfo"></span>
    </div>
  </div>

</main>
<script>
let rows=[], sortKey="count", sortDir=-1, maxCount=1, poll=null, curPeriod=null;
const $ = id => document.getElementById(id);

function ago(ts){
  const s = Date.now()/1000 - ts;
  if (s < 3600) return Math.round(s/60)+"m ago";
  if (s < 86400) return Math.round(s/3600)+"h ago";
  const d = Math.round(s/86400);
  return d+(d===1?" day ago":" days ago");
}

let savedInfo = new Map();

async function loadPicker(selected){
  const saved = await (await fetch("/api/saved")).json();
  savedInfo = new Map(saved.map(s => [s.period, s]));
  const nowY = new Date().getFullYear();

  const years = [], decades = [];
  for (let y = nowY; y >= 1920; y--) years.push(String(y));
  for (let d = Math.floor(nowY/10)*10; d >= 1920; d -= 10) decades.push(d + "s");
  // anything scraped outside those ranges (e.g. a stray 2027) still gets listed
  for (const s of saved) {
    const list = s.period.endsWith("s") ? decades : years;
    if (!list.includes(s.period)) list.unshift(s.period);
  }
  years.sort((a,b) => b - a);
  decades.sort((a,b) => parseInt(b) - parseInt(a));

  const option = p => {
    const i = savedInfo.get(p);
    const label = i ? `${p}   ·   ${i.rows.toLocaleString()} film${i.rows === 1 ? "" : "s"}   ·   ${ago(i.mtime)}`
                    : `${p}   ·   not scraped`;
    return `<option value="${p}">${label}</option>`;
  };
  $("pick").innerHTML =
      `<optgroup label="── DECADES ──">${decades.map(option).join("")}</optgroup>`
    + `<optgroup label="── YEARS ──">${years.map(option).join("")}</optgroup>`;

  $("pick").value = selected || String(nowY);
  const nYears = saved.filter(s => !s.period.endsWith("s")).length;
  $("hint").textContent = `${nYears} years and ${saved.length - nYears} decades scraped`;
}

function onPick(){
  const p = $("pick").value;
  if (savedInfo.has(p)) {
    $("run").textContent = "Refresh";
    showResults(p);
  } else {
    $("run").textContent = "Scrape";
    $("resultsCard").style.display = "none";
    $("diffCard").style.display = "none";
  }
}

// Big periods have tens of thousands of films; drawing them all at once makes
// sorting and filtering sluggish, so rows are added in batches as you scroll.
const CHUNK = 500;
let viewRows = [], rendered = 0;

let SEEN = new Set();
let MAD = {now: new Map(), soon: new Map(), data: null, showing: "now", all: false};

const shortDate = iso => new Date(iso + "T12:00:00").toLocaleDateString("en-US", {month: "short", day: "numeric"});
const todayIso = () => new Date().toLocaleDateString("en-CA");

async function loadMadison(){
  try{
    const d = await (await fetch("/api/madison")).json();
    MAD.data = d;
    MAD.now = new Map(d.now.map(x => [x.slug, x]));
    MAD.soon = new Map(d.soon.map(x => [x.slug, x]));
    renderMadison();
    if (d.running) setTimeout(() => loadMadison().then(() => rows.length && render()), 5000);
  }catch(e){}
}

// A venue the newsletter linked opens that film's page at that theater.
function venueLinks(x){
  const links = x.links || {};
  return x.venues.map(v => links[v]
    ? `<a href="${links[v]}" target="_blank" rel="noopener">${v}</a>` : v).join(", ");
}

function renderMadison(){
  const d = MAD.data;
  if (!d || (!d.now.length && !d.soon.length)){ $("madCard").hidden = true; return; }
  const list = MAD.showing === "now" ? d.now : d.soon;
  const range = list.length ? `${shortDate(list[0].start)}\u2013${shortDate(list[0].end)}` : "";
  $("madHead").textContent = MAD.showing === "now" ? "Playing in Madison" : "Coming to Madison next week";
  $("madMeta").innerHTML = `${range} \u00b7 ${list.length} films \u00b7 from `
    + (d.latest ? `<a href="${d.latest.url}" target="_blank" rel="noopener">${d.latest.title}</a>` : "The Horizon Line")
    + `, The Horizon Line`;
  const shown = MAD.all ? list : list.slice(0, 10);
  $("madList").innerHTML = shown.map(x => {
    const seen = SEEN.has(x.slug);
    const today = x.next && x.next === todayIso();
    return `<div class="mad"><span class="mseats ${x.seats ? "" : "none"}" title="${x.seats ? `seen by ${x.seats} people you follow (${x.seatsIn})` : "not in any list you've scraped"}">${x.seats || "\u2014"}</span>`
      + `<div><div class="mt">${seen ? '<span class="tick" title="You have seen this">&#10003;</span>' : ""}`
      + `<a href="https://letterboxd.com/film/${x.slug}/" target="_blank" rel="noopener">${x.title}</a>`
      + hitStats(x)
      + (x.section ? `<span class="tag">${x.section}</span>` : "")
      + (today ? `<span class="tag today">today</span>` : "")
      + (x.note ? `<span class="tag">${x.note}</span>` : "") + `</div>`
      + `<div class="mv">${venueLinks(x) || "venues not listed"}</div></div></div>`;
  }).join("");
  $("madMore").textContent = list.length > 10 ? (MAD.all ? "show fewer" : `show all ${list.length}`) : "";
  const other = MAD.showing === "now" ? d.soon.length : d.now.length;
  $("madToggle").textContent = other ? (MAD.showing === "now" ? `coming next week (${other})` : "back to this week") : "";
  $("madCard").hidden = false;
}

function showMadison(tr){
  const btn = tr.querySelector(".madb"), slug = tr.dataset.slug;
  const x = MAD.now.get(slug) || MAD.soon.get(slug);
  if (!x) return;
  const open = panelBelow(tr, "madison");
  if (open){ open.remove(); return; }
  const when = MAD.now.has(slug) ? `this week (${shortDate(x.start)}\u2013${shortDate(x.end)})`
                                  : `next week (${shortDate(x.start)}\u2013${shortDate(x.end)})`;
  const body = `Playing in Madison ${when}${x.section ? ` \u00b7 ${x.section}` : ""}${x.note ? ` \u00b7 ${x.note}` : ""}: `
    + `<b>${venueLinks(x) || "venues not listed"}</b> \u00b7 `
    + `<a href="${x.url}" target="_blank" rel="noopener">${x.post}</a>`;
  tr.insertAdjacentHTML("afterend", `<tr class="wrow" data-kind="madison"><td></td><td colspan="4">${body}</td></tr>`);
}

let riseDays = 7, riseAll = false;
async function loadRising(days){
  if (days) { riseDays = days; riseAll = false; }
  let d;
  try { d = await (await fetch(`/api/rising?days=${riseDays}`)).json(); } catch (e) { return; }
  const rows = riseAll ? d.rows : d.rows.slice(0, 8);
  document.querySelectorAll("#riseSince button").forEach(b =>
    b.classList.toggle("on", +b.dataset.days === riseDays));
  $("riseMeta").textContent = d.rows.length
    ? `${d.films.toLocaleString()} films gained seats` + (d.since ? ` · since ${ago(d.since)}` : "")
    : "nothing has moved yet";
  $("riseList").innerHTML = rows.map(x => {
    const img = POSTERS.has(x.slug)
      ? `<img src="/poster/${encodeURIComponent(x.slug)}.jpg" alt="" loading="lazy">`
      : '<span class="noimg"></span>';
    return `<div class="rise">${img}<span class="up">+${x.delta}</span>`
      + `<span class="t"><a href="https://letterboxd.com/film/${x.slug}/" target="_blank" rel="noopener">${x.title}</a>`
      + (x.isNew ? '<span class="new">NEW</span>' : "")
      + `<span class="where"> ${x.year || ""}</span>${hitStats(x)}</span>`
      + `<span class="where">${x.count} seats · ${x.period}</span></div>`;
  }).join("");
  $("riseMore").textContent = d.rows.length > 8 ? (riseAll ? "show fewer" : `show all ${d.rows.length}`) : "";
  $("riseCard").hidden = !d.rows.length;
}

let POSTERS = new Set();         // films with a poster cached on this Mac
async function loadPosters(){
  try {
    const d = await (await fetch("/api/posters")).json();
    POSTERS = new Set(d.slugs || []);
  } catch (e) {}
}

let rankOf = new Map();          // count -> rank, so ties share a number

function computeRanks(){
  rankOf = new Map();
  rows.forEach((x, i) => { if (!rankOf.has(x.count)) rankOf.set(x.count, i + 1); });
}

function ratingCell(x){
  // only lists scraped since ratings were added carry these
  if (x.rating == null) return "";
  const few = (x.ratings || 0) < 10;
  return `<b class="${few ? "few" : ""}" title="average of ${x.ratings} friend${x.ratings === 1 ? "" : "s"} who rated it`
    + `${few ? " — fewer than 10, so it sorts below the rest" : ""}">${x.rating.toFixed(1)}\u2605</b>`;
}
function likedCell(x){
  if (x.liked == null) return "";
  const few = (x.count || 0) < 10;
  return `<span class="heart${few ? " few" : ""}" title="${x.liked}% of the ${x.count} who watched it liked it`
    + `${few ? " — fewer than 10, so it sorts below the rest" : ""}">${x.liked}%\u2665</span>`;
}

function rowHtml(x){
  const seen = SEEN.has(x.slug);
  const poster = POSTERS.has(x.slug)
    ? `<img class="pos" src="/poster/${encodeURIComponent(x.slug)}.jpg" alt="" loading="lazy" decoding="async">`
    : '<span class="pos"></span>';
  return `<tr class="${seen ? "seen" : ""}" data-slug="${x.slug}"><td class="rank">${rankOf.get(x.count)}</td>`
    + `<td class="film">${poster}<span class="fi">${seen ? '<span class="tick" title="You have seen this">&#10003;</span>' : ""}`
    + `<a href="${x.url}" target="_blank" rel="noopener">${x.title}</a>`
    + (MAD.now.has(x.slug) ? `<button class="madb now" type="button">in Madison</button>`
       : MAD.soon.has(x.slug) ? `<button class="madb soon" type="button">Madison next week</button>` : "")
    + `<button class="who" type="button" aria-expanded="false">who?</button>`
    + `<button class="lists" type="button" aria-expanded="false">lists</button></span></td>`
    + `<td class="yr">${x.year || ""}</td>`
    + `<td class="rate">${ratingCell(x)}</td><td class="rate">${likedCell(x)}</td>`
    + `<td class="cnt">${x.count}</td>`
    + `<td class="meter"><i style="width:${Math.max(2, 100 * x.count / maxCount)}%"></i></td></tr>`;
}

async function loadSeen(){
  try{
    const d = await (await fetch("/api/seen")).json();
    SEEN = new Set(d.slugs || []);
    const label = $("hideSeen").parentElement;
    label.title = d.count
      ? `${d.count.toLocaleString()} films on your own profile, checked ${ago(Date.parse(d.asOf) / 1000)}`
      : (d.running ? "Fetching your own watched list\u2026" : "Your own watched list hasn't been fetched yet");
    if (d.running) setTimeout(() => loadSeen().then(render), 4000);
  }catch(e){}
}
// The app can stay open for days: pick up newly logged films without a reload.
setInterval(async () => {
  const before = SEEN.size;
  await loadSeen();
  if (SEEN.size !== before && rows.length) render();
}, 20 * 60 * 1000);

function panelBelow(tr, kind){
  for (let el = tr.nextElementSibling; el && el.classList.contains("wrow"); el = el.nextElementSibling)
    if (el.dataset.kind === kind) return el;
  return null;
}

async function showWatchers(tr){
  const btn = tr.querySelector(".who"), slug = tr.dataset.slug;
  if (btn.getAttribute("aria-expanded") === "true"){
    const panel = panelBelow(tr, "who");
    if (panel) panel.remove();
    btn.setAttribute("aria-expanded", "false"); btn.textContent = "who?";
    return;
  }
  btn.setAttribute("aria-expanded", "true"); btn.textContent = "hide";
  const res = await fetch(`/api/watchers?period=${encodeURIComponent(curPeriod)}&slug=${encodeURIComponent(slug)}`);
  const d = await res.json();
  const body = res.ok
    ? (d.watchers.length
        ? `<b>${d.watchers.length}</b> of the people you follow: `
          + d.watchers.map(w => {
              const u = typeof w === "string" ? w : w.user;
              const mark = (typeof w === "object" && (w.stars || w.liked))
                ? `<span class="mark">${w.stars ? w.stars.toFixed(1).replace(".0", "") + "\u2605" : ""}`
                  + `${w.liked ? " \u2665" : ""}</span>` : "";
              return `<a href="https://letterboxd.com/${u}/" target="_blank" rel="noopener">${u}</a>${mark}`;
            }).join(", ")
        : "None of the people you follow logged this one.")
    : d.error;
  tr.insertAdjacentHTML("afterend", `<tr class="wrow" data-kind="who"><td></td><td colspan="4">${body}</td></tr>`);
}

// "Recommend me something": films you haven't seen, rated by at least 10
// friends, best-rated first. Combines with the title filter and with
// "Playing in Madison", which makes it "what should I go see this week".
let recommend = false;
function setRecommend(on){
  recommend = on;
  $("recBtn").classList.toggle("on", on);
  $("recBtn").textContent = on ? "\u2715 Back to the full list" : "Recommend me something";
  if (on) { sortKey = "rating"; sortDir = -1; }
  else { sortKey = "count"; sortDir = -1; }
  render();
}
$("recBtn").onclick = () => setRecommend(!recommend);

function render(){
  const q = $("filter").value.toLowerCase();
  const min = parseInt($("minSeen").value || "1", 10);
  const hide = $("hideSeen").checked || recommend;
  const onlyMad = $("onlyMadison").checked;
  let r = rows.filter(x => x.count >= min && (!hide || !SEEN.has(x.slug))
                        && (!onlyMad || MAD.now.has(x.slug))
                        && (!recommend || (x.rating != null && (x.ratings || 0) >= 10))
                        && (!q || x.title.toLowerCase().includes(q)));
  const note = $("recNote");
  if (recommend) {
    note.innerHTML = r.length
      ? `<b>${r.length.toLocaleString()} film${r.length === 1 ? "" : "s"} you haven't seen</b>, `
        + `rated by at least 10 friends, best first${onlyMad ? " — playing in Madison now" : ""}.`
      : (onlyMad ? "Nothing playing in Madison this week that you haven't seen has 10+ ratings."
                 : "Nothing here you haven't seen has 10+ ratings yet.");
    note.hidden = false;
  } else note.hidden = true;
  // Rating and Liked sort strictly by the number shown. Films with fewer than
  // MIN_BASE ratings (or watchers, for Liked) go below the rest, still in order,
  // so a 5.0 from one person can't take the top spot.
  const MIN_BASE = 10;
  const base = (x, k) => k === "rating" ? (x.ratings || 0) : (x.count || 0);
  r.sort((a,b)=>{ let A=a[sortKey],B=b[sortKey];
    if(sortKey==="rating"||sortKey==="liked"){
      const fa = base(a, sortKey) < MIN_BASE, fb = base(b, sortKey) < MIN_BASE;
      if (A != null && B != null && fa !== fb) return fa ? 1 : -1;   // few-ratings films last
      if(A==null&&B==null) return b.count-a.count;
      if(A==null) return 1;              // unrated films sink, either direction
      if(B==null) return -1;
      return A<B?-sortDir:A>B?sortDir:b.count-a.count;
    }
    if(sortKey!=="count"){A=String(A).toLowerCase();B=String(B).toLowerCase();}
    return A<B?-sortDir:A>B?sortDir:0; });
  $("shown").textContent = `${r.length.toLocaleString()} of ${rows.length.toLocaleString()}`;
  viewRows = r; rendered = 0;
  $("tb").innerHTML = "";
  appendRows();
}

function appendRows(){
  const next = viewRows.slice(rendered, rendered + CHUNK);
  if (next.length) {
    $("tb").insertAdjacentHTML("beforeend", next.map(rowHtml).join(""));
    rendered += next.length;
  }
  const left = viewRows.length - rendered;
  $("more").style.display = left > 0 ? "flex" : "none";
  $("moreInfo").textContent = `showing ${rendered.toLocaleString()} of ${viewRows.length.toLocaleString()}`;
  $("loadMore").textContent = `Load ${Math.min(CHUNK, left).toLocaleString()} more`;
}

window.addEventListener("scroll", () => {
  if (rendered < viewRows.length &&
      window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 1500)
    appendRows();
}, {passive: true});

async function showResults(period){
  const res = await fetch("/api/results?period="+encodeURIComponent(period));
  if(!res.ok){ alert("No saved results for "+period); return; }
  const d = await res.json();
  rows = d.rows; curPeriod = d.period; maxCount = Math.max(1,...rows.map(r=>r.count));
  computeRanks();
  $("resultsCard").style.display="block";
  $("sPeriod").textContent=d.period; $("sFilms").textContent=rows.length.toLocaleString();
  $("sTop").textContent=maxCount; $("sAge").textContent=ago(d.mtime);
  $("dl").href="/api/download?period="+encodeURIComponent(d.period);
  $("showDiff").style.display = d.hasPrev ? "inline" : "none";
  $("diffCard").style.display="none";
  render();
}

let diffSince = "last";
async function showDiff(period, since){
  if (since) diffSince = since;
  const res = await fetch(`/api/diff?period=${encodeURIComponent(period)}&since=${diffSince}`);
  if(!res.ok){ alert("Nothing to compare against yet for that stretch of time."); return; }
  const d = await res.json();
  $("diffCard").style.display="block";
  document.querySelectorAll("#diffSince button").forEach(b =>
    b.classList.toggle("on", b.dataset.since === d.since));
  $("diffMeta").textContent =
    `vs ${d.baseline} · ${ago(d.prevTime)} → ${ago(d.curTime)} · `
    + `${d.prevFilms.toLocaleString()} → ${d.curFilms.toLocaleString()} films`;
  $("gainHead").textContent = `Gained watches (${d.nGained})`;
  $("addHead").textContent = `New films (${d.nAdded})`;
  const filmLink = x =>
    `<a href="https://letterboxd.com/film/${x.slug}/" target="_blank" rel="noopener">${x.title}</a>`;
  $("gained").innerHTML = d.gained.length
    ? d.gained.map(x=>`<div><span class="up">+${x.delta}</span> ${filmLink(x)} <span class="muted">(${x.year||"?"}) → ${x.count}</span>${hitStats(x)}</div>`).join("")
    : '<div class="muted">nothing gained</div>';
  $("added").innerHTML = d.added.length
    ? d.added.map(x=>`<div>${filmLink(x)} <span class="muted">(${x.year||"?"})</span> <span class="up">${x.count}</span><span class="new">NEW</span>${hitStats(x)}</div>`).join("")
    : '<div class="muted">no new films</div>';
  if (d.nDropped) $("added").innerHTML +=
    `<div class="muted" style="margin-top:8px">${d.nDropped} film(s) lost watches</div>`;
}

document.querySelectorAll("th[data-k]").forEach(th => th.onclick = ()=>{
  const k=th.dataset.k;
  if(sortKey===k) sortDir=-sortDir; else {sortKey=k; sortDir = k==="count"?-1:1;}
  render();
});
$("filter").oninput = render;
$("loadMore").onclick = appendRows;
$("tb").addEventListener("click", e => {
  const who = e.target.closest(".who");
  if (who) { showWatchers(who.closest("tr")); return; }
  const lists = e.target.closest(".lists");
  if (lists) { showLists(lists.closest("tr")); return; }
  const mad = e.target.closest(".madb");
  if (mad) showMadison(mad.closest("tr"));
});

async function showLists(tr){
  const btn = tr.querySelector(".lists"), slug = tr.dataset.slug;
  if (btn.getAttribute("aria-expanded") === "true"){
    const panel = panelBelow(tr, "lists");
    if (panel) panel.remove();
    btn.setAttribute("aria-expanded", "false"); btn.textContent = "lists";
    return;
  }
  btn.setAttribute("aria-expanded", "true"); btn.textContent = "hide";
  const d = await (await fetch("/api/lists?slug=" + encodeURIComponent(slug))).json();
  let body;
  if (d.lists === null)
    body = d.running ? "Still reading your liked lists\u2026 try again in a minute."
                     : (d.error || "Your liked lists haven't been read yet.");
  else if (!d.lists.length)
    body = `Not in any of your ${d.total} liked lists.`;
  else
    body = `In <b>${d.lists.length}</b> of your ${d.total} liked lists: `
      + d.lists.map(l => `<a href="${l.url}" target="_blank" rel="noopener">${l.name}</a>`
        + ` <span class="pos">#${l.position.toLocaleString()} of ${l.size.toLocaleString()}</span>`).join(", ");
  tr.insertAdjacentHTML("afterend", `<tr class="wrow" data-kind="lists"><td></td><td colspan="4">${body}</td></tr>`);
}
try { $("showPosters").checked = localStorage.getItem("showPosters") !== "0"; } catch (e) {}
function paintPosters(){
  $("tb").classList.toggle("posters", $("showPosters").checked);
}
$("showPosters").onchange = () => {
  try { localStorage.setItem("showPosters", $("showPosters").checked ? "1" : "0"); } catch (e) {}
  paintPosters();
};
paintPosters();
try { $("hideSeen").checked = localStorage.getItem("hideSeen") === "1"; } catch (e) {}
$("onlyMadison").onchange = render;
$("madMore").onclick = e => { e.preventDefault(); MAD.all = !MAD.all; renderMadison(); };
$("madToggle").onclick = e => {
  e.preventDefault(); MAD.showing = MAD.showing === "now" ? "soon" : "now"; MAD.all = false; renderMadison();
};
$("hideSeen").onchange = () => {
  try { localStorage.setItem("hideSeen", $("hideSeen").checked ? "1" : "0"); } catch (e) {}
  render();
};

async function loadHealth(){
  const h = await (await fetch("/api/health")).json();
  const el = $("health"), now = Date.now() / 1000;
  const sinceTick = h.lastTick ? (now - h.lastTick) / 3600 : Infinity;
  const problems = [];
  if (!h.tickLoaded) problems.push("<b>auto-sync is not installed</b>");
  else if (sinceTick > 3) problems.push(`<b>auto-sync hasn't run for ${h.lastTick ? ago(h.lastTick).replace(" ago", "") : "a while"}</b>`);
  if (h.yearAge === null || h.yearAge > 1.25) problems.push("<b>this year's data is out of date</b>");
  if (h.outcome === "failed") problems.push("<b>last refresh failed</b>");
  if (h.outcome === "incomplete") problems.push("<b>last refresh had missing friends</b>");
  // whatever the hourly self-check found, in its own words
  for (const p of (h.check?.problems || []))
    problems.push(p.level === "serious" ? `<b>${p.what}</b>` : p.what);

  const parts = ['<span class="dot"></span>'];
  parts.push(h.yearAge !== null ? `this year refreshed <b>${ago(now - h.yearAge * 86400)}</b>` : "this year never refreshed");
  parts.push('<span class="sep">|</span>');
  parts.push(h.staleDecades.length
    ? `${h.staleDecades.length} decade${h.staleDecades.length > 1 ? "s" : ""} catching up`
      + (h.oldest ? ` (oldest: ${h.oldest[0]}, ${h.oldest[1] === null ? "never" : Math.round(h.oldest[1]) + " days"})` : "")
    : "all decades current");
  parts.push('<span class="sep">|</span>');
  parts.push(h.lastTick ? `auto-sync last ran ${ago(h.lastTick)}` : "auto-sync hasn't run yet");
  if (problems.length) parts.push('<span class="sep">|</span>', problems.join(" \u00b7 "));
  el.innerHTML = parts.join(" ");
  el.className = problems.length ? "health warn" : "health";
  el.hidden = false;
}
try { const v = localStorage.getItem("minSeen"); if (v) $("minSeen").value = v; } catch (e) {}
$("minSeen").onchange = () => {
  try { localStorage.setItem("minSeen", $("minSeen").value); } catch (e) {}
  render();
};
$("pick").onchange = onPick;
$("showDiff").onclick = e => { e.preventDefault(); showDiff(curPeriod); };
$("riseSince").addEventListener("click", e => {
  const b = e.target.closest("button");
  if (b) loadRising(+b.dataset.days);
});
$("riseMore").onclick = e => { e.preventDefault(); riseAll = !riseAll; loadRising(); };
$("diffSince").addEventListener("click", e => {
  const b = e.target.closest("button");
  if (b) showDiff(curPeriod, b.dataset.since);
});
$("quit").onclick = async ()=>{
  if(!confirm("Stop the app?")) return;
  await fetch("/api/quit",{method:"POST"});
  document.body.innerHTML='<main><div class="card">App stopped. '
    +'Restart with <code>python3 ~/Desktop/letterboxd_app.py</code> '
    +'or the Letterboxd Friends app.</div></main>';
};

$("run").onclick = async ()=>{
  const period=$("pick").value;
  const res=await fetch("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},
                                    body:JSON.stringify({period})});
  if(!res.ok){ alert((await res.json()).error); return; }
  $("progwrap").style.display="block"; $("run").disabled=true;
  $("quality").innerHTML=""; $("diffCard").style.display="none";
  if(poll) clearInterval(poll);
  poll=setInterval(tick,1200); tick();
};

async function tick(){
  const s = await (await fetch("/api/status")).json();
  $("log").textContent = s.lines.slice(-40).join("\n");
  $("log").scrollTop = $("log").scrollHeight;
  if(s.progress) $("bar").style.width = (100*s.progress.done/s.progress.total)+"%";
  if(s.quality) $("quality").innerHTML =
    `<div class="flag ${s.quality.ok?"ok":"bad"}">${s.quality.text}</div>`;
  if(!s.running && s.finished){
    clearInterval(poll); poll=null; $("run").disabled=false; $("bar").style.width="100%";
    if(s.returncode===0){ await loadPicker(s.period); await showResults(s.period);
                          $("run").textContent = "Refresh";
                          await showDiff(s.period).catch(()=>{}); }
  }
}

let mode = "film";
document.querySelectorAll(".mode").forEach(b => b.onclick = ()=>{
  document.querySelectorAll(".mode").forEach(x=>x.classList.remove("on"));
  b.classList.add("on"); mode = b.dataset.m;
});

function posChips(list){
  if(!list.length) return '<span class="pos none">not scraped yet</span>';
  return list.map(a=>`<span class="pos">${a.period} <b>#${a.rank}</b>`
    + `<span class="muted"> of ${a.outOf.toLocaleString()} · ${a.count} seen</span></span>`).join("");
}

async function doSearch(){
  const q = $("q").value.trim();
  if(q.length < 2){ alert("Type at least 2 characters."); return; }
  $("qout").innerHTML = '<span class="muted">searching…</span>';
  $("qhint").textContent = "";

  if(mode === "film"){
    const d = await (await fetch("/api/search?q="+encodeURIComponent(q))).json();
    if(!d.hits.length){
      $("qout").innerHTML = '<span class="muted">No film titles match that in your scraped periods.</span>'
        + '<div style="margin-top:8px">Looking for a person? Search it as '
        + '<a href="#" class="switch" data-m="actor">an actor</a> or '
        + '<a href="#" class="switch" data-m="director">a director</a>.</div>';
      wireSwitches(); return;
    }
    $("qhint").textContent = `${d.hits.length} match${d.hits.length>1?"es":""}`;
    $("qout").innerHTML = d.hits.map(h=>`<div class="hit">
        <div class="t"><a href="${h.url}" target="_blank" rel="noopener">${h.title}</a>
        <span class="muted">${h.year?"("+h.year+")":""}</span>${hitStats(h)}</div>
        <div>${posChips(h.in)}</div></div>`).join("");
    return;
  }

  const res = await fetch(`/api/person?name=${encodeURIComponent(q)}&role=${mode}`);
  if(!res.ok){ $("qout").innerHTML =
      `<span class="muted">${(await res.json()).error}</span>`; return; }
  const d = await res.json();
  $("qhint").textContent = `${d.name} as ${d.role} · ${d.scraped} of ${d.total} films in your data`;
  const also = d.alsoRole
    ? ` · also credited as ${d.alsoRole === "actor" ? "an actor" : "a director"} — `
      + `<a href="#" class="switch" data-m="${d.alsoRole}">show those</a>`
    : "";
  $("qout").innerHTML =
    `<div class="hit"><a href="${d.personUrl}" target="_blank" rel="noopener">${d.personUrl}</a>${also}</div>`
    + d.films.map(f=>`<div class="hit">
        <div class="t"><a href="${f.url}" target="_blank" rel="noopener">${f.title}</a>
        <span class="muted">${f.year?"("+f.year+")":""}</span>${hitStats(f)}</div>
        <div>${posChips(f.in)}</div></div>`).join("");
  wireSwitches();
}

// Friends' average and liked share beside a search result, when the list it
// came from has them.
function hitStats(h){
  if (h.rating == null && h.liked == null) return "";
  return `<span class="hitstats" title="${h.ratings} of the ${h.seats} friends who watched it rated it">`
    + (h.rating != null ? `<b>${h.rating.toFixed(1)}\u2605</b>` : "")
    + (h.liked != null ? ` <span class="heart">${h.liked}%\u2665</span>` : "") + `</span>`;
}

function wireSwitches(){
  document.querySelectorAll("#qout .switch").forEach(a => a.onclick = e => {
    e.preventDefault();
    document.querySelector(`.mode[data-m="${a.dataset.m}"]`).click();
    doSearch();
  });
}

$("go").onclick = doSearch;
$("q").addEventListener("keydown", e=>{ if(e.key==="Enter") doSearch(); });

loadSeen().then(() => loadPosters()).then(() => loadRising()).then(() => loadMadison())
          .then(() => loadPicker().then(onPick));

// Re-read the listings every 10 minutes: picks up a new weekly report, and
// rolls "this week" over at midnight even when nothing new was fetched.
setInterval(async () => {
  const stamp = d => d ? `${d.asOf}|${d.now.length}|${d.soon.length}` : "";
  const before = stamp(MAD.data);
  await loadMadison();
  if (stamp(MAD.data) !== before && rows.length) render();
}, 10 * 60 * 1000);
loadHealth();
(async()=>{ const s=await (await fetch("/api/status")).json();
  if(s.running){ $("progwrap").style.display="block"; $("run").disabled=true;
                 poll=setInterval(tick,1200); tick(); } })();
</script>
</body></html>
"""


if __name__ == "__main__":
    if not os.path.exists(SCRAPER):
        raise SystemExit(f"Scraper not found at {SCRAPER}")
    maybe_refresh_my_films()          # your own watched list, in the background
    threading.Thread(target=watch_my_films, daemon=True).start()
    maybe_refresh_liked_lists()       # and the lists you've liked
    threading.Thread(target=refresh_madison, daemon=True).start()   # always check on launch
    threading.Thread(target=watch_madison, daemon=True).start()
    run_tick()                                                   # catch up on launch
    threading.Thread(target=keep_ticking, daemon=True).start()   # and hourly while open
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Letterboxd app running at {url}   (Ctrl+C, or 'quit app' in the UI)")
    # The .app launcher opens its own chrome-less window, so it passes
    # --no-browser to stop us also opening a browser tab.
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print("\nstopped")
