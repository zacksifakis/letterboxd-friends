"""
Who counts.

Everyone you follow is still scraped, but anyone listed in
~/.letterboxd_cache/excluded.json is left out when films are counted — on the
lists, the sites, the "who?" panel and the seat total. Because the scrape keeps
everyone's per-person data, excluding someone (or bringing them back) is just a
recount of what's on disk: no Letterboxd requests, a few seconds.

    python3 letterboxd_people.py                      # who's excluded
    python3 letterboxd_people.py exclude a b c        # leave these out, recount
    python3 letterboxd_people.py include a            # count them again, recount
    python3 letterboxd_people.py recount              # recount every list
"""

import os
import re
import csv
import sys
import glob
import json
import datetime

HOME = os.path.expanduser("~")
CACHE = os.path.join(HOME, ".letterboxd_cache")
EXCLUDED = os.path.join(CACHE, "excluded.json")
SNAPS = os.path.join(HOME, ".letterboxd_history", "snapshots")

FIELDS = ["Film Title", "Year", "Slug", "Friend Watch Count", "Avg Rating", "Ratings",
          "Likes", "Liked %", "Letterboxd URL"]


def excluded():
    try:
        with open(EXCLUDED) as f:
            return set(json.load(f).get("users", []))
    except Exception:
        return set()


def _state():
    try:
        with open(EXCLUDED) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(users, history=None, aliases=None):
    os.makedirs(CACHE, exist_ok=True)
    st = _state()
    st.update({"users": sorted(users, key=str.lower),
               "changed": datetime.datetime.now().isoformat(timespec="seconds")})
    if history is not None:
        st["history"] = sorted(history, key=str.lower)   # already taken out of the dated copies
    if aliases is not None:
        st["aliases"] = aliases                          # old username -> new one
    with open(EXCLUDED, "w") as f:
        json.dump(st, f, indent=1)


def following():
    """Everyone in the newest audit file — the people the last scrape covered."""
    audits = sorted(glob.glob(f"{HOME}/friends_watched_*_per_friend.json"), key=os.path.getmtime)
    for path in reversed(audits):
        try:
            with open(path) as f:
                return set(json.load(f))
        except Exception:
            continue
    return set()


def counted():
    """How many seats there are: everyone followed, minus anyone left out."""
    people = following()
    return len(people - excluded()) if people else 0


def _periods():
    out = []
    for p in glob.glob(f"{HOME}/friends_watched_*.csv"):
        m = re.fullmatch(r"friends_watched_([0-9]{4}s?)\.csv", os.path.basename(p))
        if m:
            out.append(m.group(1))
    return sorted(out)


def _audit(period):
    path = os.path.join(HOME, f"friends_watched_{period}_per_friend.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _marks(films):
    """An audit entry, old (list of slugs) or new ({slug: [rating, liked]})."""
    if isinstance(films, dict):
        return {s: (list(m) + [0, 0])[:2] for s, m in films.items()}
    return {s: [0, 0] for s in films}


def carry_renames(prev_audit, now_films, log=print):
    """
    Letterboxd lets people change their username, and an exclusion is by
    username: someone excluded renamed their account and was suddenly counted again.
    If an excluded account has vanished from the people you follow and a new
    one has appeared with (nearly) the same films, it's the same person —
    move the exclusion to the new name. Returns {old: new}.
    """
    skip = excluded()
    gone = [u for u in skip if u in prev_audit and u not in now_films]
    fresh = [u for u in now_films if u not in prev_audit]
    renamed = {}
    for g in gone:
        old = set(_marks(prev_audit[g]))
        if len(old) < 5:
            continue                      # too few films to tell people apart
        for f in fresh:
            new = set(now_films[f])
            if len(new) >= 5 and len(old & new) >= 0.9 * max(len(old), len(new)):
                renamed[g] = f
                break
    if renamed:
        # Keep BOTH names excluded: every list scraped before the rename still
        # has them under the old one until it's re-scraped.
        st = _state()
        users = set(st.get("users", [])) | set(renamed.values())
        history = set(st.get("history", []))
        history |= {renamed[g] for g in renamed if g in history}   # same person, already out
        st_aliases = dict(st.get("aliases", {}))
        st_aliases.update(renamed)
        _save(users, history=history, aliases=st_aliases)
        for g, f in renamed.items():
            log(f"  {g} renamed their account to {f} — still excluded")
    return renamed


def departed():
    """Excluded usernames no longer among the people you follow (known renames aside)."""
    people, old_names = following(), set(_state().get("aliases", {}))
    return sorted(u for u in excluded() if people and u not in people and u not in old_names)


def recount_period(period, out=None):
    """
    Rewrite one list's counts from its per-person data, leaving out the
    excluded. A year without its own per-person file is counted from its
    decade's. The file keeps its date, so a recount never looks like a scrape.
    """
    path = os.path.join(HOME, f"friends_watched_{period}.csv")
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return None
    # A year with its own, current per-person file uses it; one written out
    # of its decade's scrape is counted from the decade's.
    audit = _audit(period)
    own = os.path.join(HOME, f"friends_watched_{period}_per_friend.json")
    if not period.endswith("s") and (audit is None or os.path.getmtime(own) < os.path.getmtime(path) - 120):
        audit = _audit(period[:3] + "0s") or audit
    if audit is None:
        return None

    skip = excluded()
    info = {r["Slug"]: r for r in rows if r.get("Slug")}
    counts, stars, likes = {}, {}, {}
    for user, films in audit.items():
        if user in skip:
            continue
        for slug, (rating, liked) in _marks(films).items():
            if slug not in info:
                continue                      # another year of the decade
            counts[slug] = counts.get(slug, 0) + 1
            if rating:
                stars.setdefault(slug, []).append(rating)
            if liked:
                likes[slug] = likes.get(slug, 0) + 1

    has_ratings = any("Avg Rating" in r for r in rows) and isinstance(next(iter(audit.values()), {}), dict)
    new = []
    for slug, c in counts.items():
        r = info[slug]
        row = {"Film Title": r.get("Film Title", slug), "Year": r.get("Year", ""), "Slug": slug,
               "Friend Watch Count": c, "Letterboxd URL": r.get("Letterboxd URL", "")}
        if has_ratings:
            row.update({"Avg Rating": round(sum(stars[slug]) / len(stars[slug]) / 2, 2) if stars.get(slug) else "",
                        "Ratings": len(stars.get(slug, [])), "Likes": likes.get(slug, 0),
                        "Liked %": round(100 * likes.get(slug, 0) / c) if c else 0})
        new.append(row)
    new.sort(key=lambda r: -r["Friend Watch Count"])
    fields = [f for f in FIELDS if has_ratings or f not in ("Avg Rating", "Ratings", "Likes", "Liked %")]

    stamp = os.stat(path)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(new)
    os.replace(tmp, path)
    os.utime(path, (stamp.st_atime, stamp.st_mtime))
    return len(rows), len(new)


def adjust_history():
    """
    Take the excluded out of the dated copies too, so week/month comparisons
    don't show every film they'd seen as "losing" a seat. Their current lists
    stand in for their past ones — close enough for a seat-level diff.
    """
    now = excluded()
    before = set(_state().get("history", []))
    take, give = now - before, before - now      # newly left out / brought back
    if not take and not give:
        return 0
    marked = {}                                # period -> {slug: seats to change}
    for period in _periods():
        audit = _audit(period) or (None if period.endswith("s") else _audit(period[:3] + "0s"))
        if not audit:
            continue
        m = {}
        for user in (take | give) & set(audit):
            step = -1 if user in take else 1
            for slug in _marks(audit[user]):
                m[slug] = m.get(slug, 0) + step
        marked[period] = m
    done = 0
    copies = glob.glob(os.path.join(SNAPS, "*", "*.csv")) + \
        glob.glob(os.path.join(HOME, ".letterboxd_history", "*.prev.csv"))
    for path in copies:
        period = (os.path.basename(os.path.dirname(path)) if path.startswith(SNAPS)
                  else os.path.basename(path).split(".")[0])
        drop = marked.get(period)
        if not drop:
            continue
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            fields, rows = reader.fieldnames, list(reader)
        changed = False
        for r in rows:
            n = drop.get(r.get("Slug"))
            if n:
                r["Friend Watch Count"] = max(0, int(r["Friend Watch Count"] or 0) + n)
                changed = True
        if not changed:
            continue
        rows = [r for r in rows if int(r["Friend Watch Count"] or 0) > 0]
        stamp = os.stat(path)
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        os.utime(path, (stamp.st_atime, stamp.st_mtime))
        done += 1
    _save(now, history=now)
    return done


def recount_all(log=print):
    changed = 0
    for period in _periods():
        res = recount_period(period)
        if res:
            changed += 1
    copies = adjust_history()
    log(f"recounted {changed} lists without {len(excluded())} excluded "
        f"({counted()} seats); adjusted {copies} dated copies")


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "show"
    if cmd in ("exclude", "include"):
        people = following()
        names = set(args[1:])
        unknown = names - people
        if unknown:
            print("not among the people you follow:", ", ".join(sorted(unknown)))
        users = excluded() | (names & people) if cmd == "exclude" else excluded() - names
        _save(users)
        recount_all()
    elif cmd == "recount":
        recount_all()
    else:
        ex = sorted(excluded(), key=str.lower)
        print(f"following {len(following())}, counting {counted()}")
        print("excluded:", ", ".join(ex) if ex else "nobody")
