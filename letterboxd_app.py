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


def all_results():
    """
    Every film of every year in one ranking. Decades cover every film; a year
    refreshed more recently than its decade (this year and last, daily) wins
    for its films. Each row names the list it came from, for "who?".
    """
    idx = get_index()
    merged = {}
    for s in sorted(saved_periods(), key=lambda d: d["mtime"]):
        for r in (idx.get(s["period"]) or {}).get("rows", []):
            merged[r["slug"]] = dict(r, src=s["period"])
    # Years derived from their decade have no per-person file of their own.
    has_audit = {s["period"] for s in saved_periods()
                 if os.path.exists(os.path.join(HOME, f"friends_watched_{s['period']}_per_friend.json"))}
    for r in merged.values():
        if r["src"] not in has_audit:
            dec = str(r.get("year") or "")[:3] + "0s"
            if dec in has_audit:
                r["src"] = dec
    rows = list(merged.values())
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


def madison_due(today=None):
    """
    The guide comes out on Wednesdays. From Wednesday on, check every hour
    until that week's issue is in, then not again until the next Wednesday.
    (A Tuesday post counts too; a late one is simply waited for.)
    """
    today = today or datetime.date.today()
    wednesday = today - datetime.timedelta(days=(today.weekday() - 2) % 7)
    data = load_madison() or {}
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", (data.get("latest") or {}).get("title", ""))
    if not m:
        return True
    issue = datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    return issue < wednesday - datetime.timedelta(days=1)


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
    While the app is open, check the feed every hour whenever madison_due()
    says this week's issue isn't in yet (no "checked recently" rule: one with
    the same interval skips every other check).
    """
    while True:
        time.sleep(every_minutes * 60)
        try:
            if madison_due():
                refresh_madison()
        except Exception:
            pass


def best_and_stats():
    """{slug: (most friends in any list, that list)}, {slug: rating/liked}."""
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
    return best, stats


def madison_listings():
    """What's playing now and next, each film ranked by how many friends watched it."""
    data = load_madison() or {}
    today = datetime.date.today()
    best, stats = best_and_stats()

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
        item = bucket.setdefault(e["slug"], {**e, "venues": [], "links": {}, "next": "",
                                             "allWeek": False, "days": []})
        # A venue line with no date is a regular run ("all week"); dated lines
        # are one-off screenings on those days.
        item["allWeek"] = item["allWeek"] or any(not venue_times(v) for v in live)
        item["days"] = sorted(set(item["days"]) | {d.isoformat() for d in upcoming})
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


# ------------------------------------------------- films on any web page

IMDB_LB = os.path.join(HOME, ".letterboxd_cache", "imdb_to_letterboxd.json")     # tt -> [slug, title, year]


def page_films(url):
    """The films on a web page, ranked by how many people you follow have seen them."""
    import letterboxd_pagefilms as pf
    res = pf.films_on(url)
    films = pf.index()["films"]
    best, stats = best_and_stats()
    try:
        with open(IMDB_LB) as f:
            imdb_lb = json.load(f)
    except Exception:
        imdb_lb = {}
    looked_up, out = 0, []
    for f in res["films"]:
        key = f["key"]
        if key.startswith("imdb:"):
            tt = key[5:]
            if tt not in imdb_lb and looked_up < 15:      # ask Letterboxd which film it is
                imdb_lb[tt] = pf.resolve_imdb(tt)
                looked_up += 1
            hit = imdb_lb.get(tt)
            if not hit:
                out.append({"slug": "", "imdb": tt, "title": f["text"], "year": "", "seats": 0,
                            "text": f["text"], "how": f["how"], "times": f["times"],
                            "rating": None, "ratings": 0, "liked": None})
                continue
            slug, title, year = hit
        else:
            slug = key
            m = re.match(r"(.*?)(?:-(\d{4}))?$", slug)       # not in your lists yet: a title from the slug
            title, year, _ = films.get(slug, (m.group(1).replace("-", " ").title(), m.group(2) or "", 0))
        seats = best.get(slug, (0, ""))
        out.append({"slug": slug, "title": title, "year": year, "seats": seats[0], "seatsIn": seats[1],
                    "text": f["text"] if f["text"] and f["text"] != title else "",
                    "how": f["how"], "times": f["times"], "checked": f.get("checked"),
                    **stats.get(slug, {"rating": None, "ratings": 0, "liked": None})})
    if looked_up:
        with open(IMDB_LB, "w") as fh:
            json.dump(imdb_lb, fh)
    seen = set()
    out = [x for x in out if not (x["slug"] and (x["slug"] in seen or seen.add(x["slug"])))]
    out.sort(key=lambda x: -x["seats"])
    return {"url": res["url"], "title": res["title"], "films": out, "unmatched": res["unmatched"],
            "checkedWith": res.get("checkedWith")}


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

    # Is a tick running right now? A long one (a decade, or one the Mac slept
    # through) can last hours, and "last started 4h ago" isn't "not running".
    running_since = None
    lock_path = os.path.join(HOME, ".letterboxd_tick.lock")
    try:
        holder = open(lock_path).read().strip()
        m = re.match(r"pid (\d+)", holder)
        if m:
            os.kill(int(m.group(1)), 0)                     # raises if it's gone
            ps = subprocess.run(["ps", "-p", m.group(1), "-o", "command="],
                                capture_output=True, text=True).stdout
            if "letterboxd_tick" in ps:
                running_since = os.path.getmtime(lock_path)
    except Exception:
        pass

    return {"lastData": last, "lastLog": finished, "outcome": outcome,
            "tickRunningSince": running_since,
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
            body, ctype = ui_file("index.html")
            return self._send(200, body, ctype)
        if u.path.startswith("/ui/"):
            hit = ui_file(u.path[len("/ui/"):])
            if hit is None:
                return self._send(404, {"error": "not found"})
            return self._send(200, hit[0], hit[1], {"Cache-Control": "no-cache"})
        if u.path == "/api/saved":
            return self._send(200, saved_periods())
        if u.path == "/api/status":
            return self._send(200, job_snapshot())
        if u.path == "/api/results" and period == "all":
            saved = saved_periods()
            return self._send(200, {"period": "all", "rows": all_results(),
                                    "mtime": max((x["mtime"] for x in saved), default=0),
                                    "hasPrev": False})
        if u.path == "/api/results":
            rows = read_results(period)
            if rows is None:
                return self._send(404, {"error": f"no results for {period}"})
            return self._send(200, {"period": period, "rows": rows,
                                    "mtime": os.path.getmtime(csv_path(period)),
                                    "hasPrev": os.path.exists(prev_path(period))})
        if u.path == "/api/page":
            import letterboxd_pagefilms as pf
            try:
                return self._send(200, page_films((q.get("url") or [""])[0].strip()))
            except pf.PageError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                return self._send(502, {"error": f"couldn't read that page: {e}"})
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


# The interface lives in ui/ (index.html, app.css, app.js), next to this file.
UI_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "ui")
UI_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8"}


def ui_file(name):
    """(bytes, content type) for a file in ui/, or None."""
    path = os.path.realpath(os.path.join(UI_DIR, name))
    if not path.startswith(UI_DIR + os.sep) or not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return f.read(), UI_TYPES.get(os.path.splitext(path)[1], "application/octet-stream")


if __name__ == "__main__":
    if not os.path.exists(SCRAPER):
        raise SystemExit(f"Scraper not found at {SCRAPER}")
    maybe_refresh_my_films()          # your own watched list, in the background
    threading.Thread(target=watch_my_films, daemon=True).start()
    maybe_refresh_liked_lists()       # and the lists you've liked
    if madison_due():                 # from Wednesday until the week's guide is in
        threading.Thread(target=refresh_madison, daemon=True).start()
    def warm_page_index():            # so the first pasted link doesn't wait ~15s
        try:
            import letterboxd_pagefilms
            letterboxd_pagefilms.index()
        except Exception:
            pass
    threading.Thread(target=warm_page_index, daemon=True).start()
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
