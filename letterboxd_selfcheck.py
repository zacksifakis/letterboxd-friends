"""
A standing health check: the things that have actually gone wrong here, asked
about once an hour so the next new kind of breakage is caught before it
reaches your data.

Each check answers one question and says plainly what to do about it. The
result is written to ~/.letterboxd_cache/selfcheck.json, shown in the app's
health strip, and — for anything serious that wasn't there last time — sent to
Notification Centre once.

    python3 letterboxd_selfcheck.py           # run the checks, print them
    python3 letterboxd_selfcheck.py --quiet   # only print problems
    python3 letterboxd_selfcheck.py --notify  # also notify on new problems
"""

import os
import re
import csv
import sys
import json
import glob
import time
import subprocess
from datetime import datetime, date, timedelta

HOME = os.path.expanduser("~")
DESKTOP = os.path.join(HOME, "Desktop")
sys.path.insert(0, DESKTOP)
CACHE = os.path.join(HOME, ".letterboxd_cache")
RESULT = os.path.join(CACHE, "selfcheck.json")
NOTIFIED = os.path.join(CACHE, "selfcheck_notified.json")

YEAR_MAX_HOURS = 36            # the hourly job aims for 20
DECADE_MAX_DAYS = 16           # it aims for 14
SHRINK_ALLOWED = 0.95          # a list losing more than 5% of its films is suspect
RENOTIFY_HOURS = 12


def periods():
    out = []
    for p in glob.glob(f"{HOME}/friends_watched_*.csv"):
        m = re.fullmatch(r"friends_watched_([0-9]{4}s?)\.csv", os.path.basename(p))
        if m:
            out.append(m.group(1))
    return sorted(out)


def rows_in(path):
    try:
        with open(path, newline="") as f:
            return sum(1 for _ in csv.reader(f)) - 1
    except Exception:
        return None


def meta(period):
    try:
        with open(os.path.join(HOME, f"friends_watched_{period}.meta.json")) as f:
            return json.load(f)
    except Exception:
        return {}


def age_hours(path):
    return (time.time() - os.path.getmtime(path)) / 3600 if os.path.exists(path) else None


# ---------------------------------------------------------------- the checks

def check_letterboxd_answers(problems, notes):
    """Letterboxd still answers our requests."""
    try:
        import letterboxd_net as net
    except Exception as e:
        problems.append(("serious", "can't load the Letterboxd helper", str(e)))
        return
    try:
        import letterboxd_settings
        r = net.session().get(f"https://letterboxd.com/{letterboxd_settings.require_username()}/", timeout=20)
        if r.status_code == 200:
            notes.append(f"letterboxd answers as '{net.current()}'")
            return
        found = net.probe(f"https://letterboxd.com/{letterboxd_settings.USERNAME}/", log=lambda m: None)
        if found:
            notes.append(f"letterboxd refused '{net.current()}' — switched to '{found}'")
        else:
            problems.append(("serious", "Letterboxd is refusing requests",
                             "scrapes will fail until this clears; try again later"))
    except Exception as e:
        problems.append(("warn", "couldn't reach Letterboxd", str(e)))


def check_friends(problems, notes):
    """Runs are using the whole following list, not a truncated one."""
    best, recent = 0, []
    for p in periods():
        m = meta(p)
        n = m.get("friends") or 0
        best = max(best, n)
        if m.get("finished"):
            recent.append((m["finished"], p, n, len(m.get("missing") or []), len(m.get("filled") or [])))
    if not recent:
        return
    recent.sort()
    when, p, n, missing, filled = recent[-1]
    if best and n < 0.9 * best:
        problems.append(("serious", f"the last run ({p}) used only {n} of {best} friends",
                         "counts from it would be too low"))
    elif missing:
        problems.append(("warn", f"the last run ({p}) missed {missing} friends",
                         "counts may be a seat or two low; the hourly job redoes it"))
    else:
        notes.append(f"last run {p}: {n} friends" + (f", {filled} filled from previous" if filled else ""))


def check_freshness(problems, notes):
    """Nothing has quietly stopped refreshing."""
    year = str(date.today().year)
    last_year = str(date.today().year - 1)
    lh = age_hours(os.path.join(HOME, f"friends_watched_{last_year}.csv"))
    if lh is not None and lh > YEAR_MAX_HOURS:
        problems.append(("warn", f"{last_year} data is {lh / 24:.1f} days old",
                         "last year should refresh daily too — check the log", last_year))
    h = age_hours(os.path.join(HOME, f"friends_watched_{year}.csv"))
    if h is None:
        problems.append(("serious", f"no data for {year}", "run a scrape for this year"))
    elif h > YEAR_MAX_HOURS:
        problems.append(("serious", f"{year} data is {h / 24:.1f} days old",
                         "the hourly job should refresh it daily — check the log", year))
    stale = []
    for p in periods():
        if not p.endswith("s"):
            continue
        d = (age_hours(os.path.join(HOME, f"friends_watched_{p}.csv")) or 0) / 24
        if d > DECADE_MAX_DAYS:
            stale.append(f"{p} ({d:.0f}d)")
    if stale:
        problems.append(("warn", f"{len(stale)} decade(s) overdue", ", ".join(sorted(stale))))
    if not stale and h is not None and h <= YEAR_MAX_HOURS:
        notes.append(f"{year} refreshed {h:.0f}h ago, every decade within {DECADE_MAX_DAYS} days")


def check_no_list_shrank(problems, notes):
    """A list never loses a chunk of its films — that's how the 1950s broke."""
    try:
        import letterboxd_history
    except Exception:
        return
    shrunk = []
    for p in periods():
        now = rows_in(os.path.join(HOME, f"friends_watched_{p}.csv"))
        snaps = [s for s in letterboxd_history.snapshots(p)
                 if s[0] < date.fromtimestamp(os.path.getmtime(os.path.join(HOME, f"friends_watched_{p}.csv")))]
        if not snaps or now is None:
            continue
        before = rows_in(snaps[-1][1])
        if before and now < before * SHRINK_ALLOWED:
            shrunk.append(f"{p}: {before:,} -> {now:,}")
    if shrunk:
        problems.append(("serious", f"{len(shrunk)} list(s) lost films since the copy before",
                         "; ".join(shrunk) + " — restore from ~/.letterboxd_history/snapshots"))
    else:
        notes.append("no list has shrunk")


def check_posters(problems, notes):
    """The poster cache is readable and roughly complete."""
    try:
        import letterboxd_posters as lp
        from PIL import Image
    except Exception:
        return
    want = lp.wanted()
    have = [s for s in want if lp.have(s)]
    broken = []
    for s in have[:8]:
        try:
            Image.open(os.path.join(lp.CACHE, s + ".jpg")).verify()
        except Exception:
            broken.append(s)
    if broken:
        problems.append(("warn", f"{len(broken)} poster file(s) unreadable",
                         ", ".join(broken) + " — delete them and they refetch"))
    elif want and len(have) < 0.9 * len(want):
        problems.append(("warn", f"posters cached for {len(have):,} of {len(want):,} films",
                         "the hourly job fetches up to 300 per run"))
    else:
        notes.append(f"posters: {len(have):,} of {len(want):,}")


def check_madison(problems, notes):
    """Madison listings are current and matched to films."""
    path = os.path.join(CACHE, "madison.json")
    h = age_hours(path)
    if h is None:
        return
    try:
        import letterboxd_app
        due = letterboxd_app.madison_due()
    except Exception:
        due = True
    # Between issues nothing is checked on purpose; only a check that's due
    # and hasn't happened is a problem.
    if due and h > 6:
        problems.append(("warn", f"this week's Madison guide isn't in, and the feed wasn't checked for {h:.0f}h",
                         "the hourly job checks it every hour from Wednesday until the issue arrives"))
        return
    try:
        with open(path) as f:
            d = json.load(f)
        fixed = len(d.get("fixed") or [])
        notes.append(f"madison: {len(d.get('entries') or [])} screenings"
                     + (f", {fixed} link(s) corrected" if fixed else ""))
    except Exception as e:
        problems.append(("warn", "Madison cache unreadable", str(e)))


def check_exclusions(problems, notes):
    """Excluded people are still people you follow — a rename would sneak them back in."""
    try:
        import letterboxd_people as lp
    except Exception:
        return
    gone = lp.departed()
    if gone:
        problems.append(("warn", f"{len(gone)} excluded account(s) no longer in your following list",
                         ", ".join(gone) + " — unfollowed, or renamed (a rename is carried over "
                         "automatically when their films match)"))
    else:
        notes.append(f"exclusions: {len(lp.excluded())} people, all still followed")


CHECKS = [check_letterboxd_answers, check_exclusions, check_friends, check_freshness,
          check_no_list_shrank, check_posters, check_madison]


# ---------------------------------------------------------------- running it

def run():
    problems, notes = [], []
    for check in CHECKS:
        try:
            check(problems, notes)
        except Exception as e:
            problems.append(("warn", f"{check.__name__} could not run", str(e)))
    result = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "ok": not any(p[0] == "serious" for p in problems),
        # freshness findings name their list, so the app can drop ones its live data has outdated
        "problems": [{"level": p[0], "what": p[1], "detail": p[2],
                      **({"period": p[3]} if len(p) > 3 else {})} for p in problems],
        "notes": notes,
    }
    os.makedirs(CACHE, exist_ok=True)
    tmp = RESULT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(result, f, indent=1)
    os.replace(tmp, RESULT)
    return result


def notify(result):
    """Tell Notification Centre about serious problems that are new."""
    serious = [p for p in result["problems"] if p["level"] == "serious"]
    try:
        with open(NOTIFIED) as f:
            seen = json.load(f)
    except Exception:
        seen = {}
    now = time.time()
    fresh = [p for p in serious if now - seen.get(p["what"], 0) > RENOTIFY_HOURS * 3600]
    if fresh:
        body = fresh[0]["what"] + (f" (+{len(fresh) - 1} more)" if len(fresh) > 1 else "")
        subprocess.run(["osascript", "-e",
                        'display notification "%s" with title "Letterboxd Friends" '
                        'subtitle "something needs a look"' % body.replace('"', "'")],
                       capture_output=True)
    for p in serious:
        seen[p["what"]] = now
    for what in list(seen):                       # forget problems that cleared
        if what not in {p["what"] for p in serious}:
            del seen[what]
    with open(NOTIFIED, "w") as f:
        json.dump(seen, f)
    return len(fresh)


if __name__ == "__main__":
    res = run()
    if "--notify" in sys.argv:
        notify(res)
    quiet = "--quiet" in sys.argv
    if not quiet:
        for n in res["notes"]:
            print(f"  ok    {n}")
    for p in res["problems"]:
        print(f"  {p['level']:7} {p['what']}\n          {p['detail']}")
    if not res["problems"] and not quiet:
        print("\nall clear")
    sys.exit(0 if res["ok"] else 1)
