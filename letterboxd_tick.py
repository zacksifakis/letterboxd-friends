"""
Hourly housekeeping. launchd runs this every hour and at every login
(~/Library/LaunchAgents/local.letterboxd.tick.plist).

It decides what to do from how old the data is, not from a timer, so restarts,
sleep and missed runs don't matter — anything overdue is simply done at the
next tick:

  - Madison listings      every tick (one small feed request)
  - this year and last    on the first check of each day after 5 AM, or when
                          their data is more than 20 hours old (last
                          year's films are still being watched a lot, so it
                          can't wait for its decade's fortnightly scrape)
  - decades               any more than 14 days old, stalest first, at most
                          two per tick so the current year never waits long
  - the website           republished after decades are refreshed
  - posters               any film new to a top 100 (at most 300 per tick)
  - genres, Greek titles  from Letterboxd's film pages, same films (at most 200)
  - Greek summaries       from TMDB, for films new to the pages (once a key is saved)
  - a self-check          the standing health checks, which notify on new trouble

    python3 letterboxd_tick.py            # what launchd runs
    python3 letterboxd_tick.py --dry-run  # say what's due, change nothing
"""

import os
import re
import sys
import time
import fcntl
import subprocess
from datetime import datetime

HOME = os.path.expanduser("~")
DESKTOP = os.path.join(HOME, "Desktop")
sys.path.insert(0, DESKTOP)

import letterboxd_refresh as refresh  # noqa: E402  (refresh(), log(), age_days(), DECADES)

CURRENT_YEAR_MAX_HOURS = 20
DECADE_MAX_DAYS = 14
DECADES_PER_TICK = 2
REDO_AFTER_HOURS = 6
STALE_TICK_HOURS = 4          # a decade takes under an hour awake


def year_is_due(period):
    """
    This year and last refresh on the first check of each new day (after 5 AM),
    so mornings start fresh, and also whenever the data is more than 20 hours
    old, so a day the Mac stays asleep still gets one.
    """
    age = refresh.age_days(period)
    if age is None or age * 24 > CURRENT_YEAR_MAX_HOURS:
        return True
    written = datetime.fromtimestamp(os.path.getmtime(refresh.csv_path(period)))
    now = datetime.now()
    return written.date() < now.date() and now.hour >= 5


def leaned_on_old_data(period, redo_after_hours=REDO_AFTER_HOURS):
    """
    True when the last run had to fill some friends from an earlier run (or
    lost some entirely) and has aged enough to be worth redoing.
    """
    import json
    meta = os.path.join(HOME, f"friends_watched_{period}.meta.json")
    age = refresh.age_days(period)
    if not os.path.exists(meta) or age is None:
        return False
    try:
        with open(meta) as f:
            m = json.load(f)
    except Exception:
        return False
    return bool(m.get("filled") or m.get("missing")) and age * 24 > redo_after_hours


def online(host="letterboxd.com", wait_s=0, every_s=15):
    """True once `host` resolves, checking every `every_s` seconds for up to `wait_s`."""
    import socket
    deadline = time.time() + wait_s
    while True:
        try:
            socket.getaddrinfo(host, 443)
            return True
        except OSError:
            if time.time() >= deadline:
                return False
            time.sleep(every_s)


def main():
    dry = "--dry-run" in sys.argv

    with open(os.path.join(HOME, "letterboxd_runs.log"), "a") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] tick start pid={os.getpid()} "
                f"parent={os.getppid()} home={os.environ.get('HOME')} args={sys.argv[1:]}\n")

    # One tick at a time. A decade takes a while; later ticks step aside — and
    # say so, rather than exiting silently.
    lock = open(os.path.join(HOME, ".letterboxd_tick.lock"), "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.seek(0)
        holder = lock.read().strip() or "unknown"
        # A tick that has held the lock for hours is almost always one frozen by
        # sleep mid-scrape. Stop it (and its scrape — data is only written when a
        # scrape finishes, so nothing is lost) and start over, rather than letting
        # it block every hour after it.
        m = re.match(r"pid (\d+) since (\d\d):(\d\d):(\d\d)", holder)
        age_h = None
        if m:
            try:
                started = os.path.getmtime(os.path.join(HOME, ".letterboxd_tick.lock"))
                age_h = (time.time() - started) / 3600
            except OSError:
                pass
        if not dry and m and age_h is not None and age_h > STALE_TICK_HOURS:
            stale = int(m.group(1))
            refresh.log(f"tick: the tick holding the lock ({holder}) has run {age_h:.1f}h — "
                        f"probably frozen by sleep; stopping it and starting fresh")
            subprocess.run(["pkill", "-f", "letterboxd_friends_v2.py"], capture_output=True)
            try:
                os.kill(stale, 15)
            except OSError:
                pass
            time.sleep(3)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                refresh.log("tick skipped: could not take over the stale lock")
                return
        else:
            if not dry:
                refresh.log(f"tick skipped: another tick holds the lock ({holder})")
            else:
                print(f"another tick holds the lock ({holder})")
            return
    lock.seek(0)
    lock.truncate()
    lock.write(f"pid {os.getpid()} since {datetime.now():%H:%M:%S}")
    lock.flush()

    year = str(datetime.now().year)
    hot_years = [year, str(datetime.now().year - 1)]
    years_due = [y for y in hot_years if year_is_due(y) or leaned_on_old_data(y)]
    year_due = bool(years_due)

    stale = []
    for d in refresh.DECADES:
        age = refresh.age_days(d)
        if age is None or age > DECADE_MAX_DAYS or leaned_on_old_data(d):
            stale.append((age if age is not None else 1e9, d))
    stale.sort(reverse=True)
    decades_due = [d for _, d in stale[:DECADES_PER_TICK]]

    plan = (f"tick: madison" + (f", {', '.join(years_due)}" if years_due else "")
            + (f", {', '.join(decades_due)}" if decades_due else "")
            + (f" ({len(stale) - len(decades_due)} more decades queued)"
               if len(stale) > len(decades_due) else ""))
    if dry:
        print(plan)
        return
    refresh.log(plan)

    # Right after waking, the network often isn't back yet: a scrape started
    # then fails at once and shows up as "couldn't reach Letterboxd". Give it a
    # few minutes, and if it's still offline, try again next hour.
    if not online(wait_s=180):
        refresh.log("tick: offline (can't resolve letterboxd.com) — skipping; next hour will catch up")
        return

    # Madison first: fast, and independent of any scrape going wrong.
    try:
        import letterboxd_app
        letterboxd_app.refresh_madison()
        err = letterboxd_app.madison_job["error"]
        if err:
            refresh.log(f"--- madison listings: ERROR {err}")
    except Exception as e:
        refresh.log(f"--- madison listings: ERROR {e}")

    for y in years_due:
        refresh.refresh(y)

    refreshed = 0
    for d in decades_due:
        refreshed += bool(refresh.refresh(d))
        time.sleep(20)

    if refreshed:
        site = os.path.join(DESKTOP, "letterboxd_site.py")
        if os.path.exists(site):
            refresh.log("=== updating the website")
            r = subprocess.run([sys.executable, site], capture_output=True, text=True)
            for line in (r.stdout + r.stderr).splitlines():
                if line.strip():
                    refresh.log("    " + line.rstrip())

    # Posters for films new to a top 100, so the weekly page build has them.
    try:
        import letterboxd_posters
        letterboxd_posters.fetch(max_n=300, log=lambda m: refresh.log("--- " + m)
                                 if not m.endswith(" 0 to fetch") else None)
    except Exception as e:
        refresh.log(f"--- posters: ERROR {e}")

    # Genres and Greek titles for films new to a top list (the Greek site).
    try:
        import letterboxd_filmpages
        letterboxd_filmpages.fetch(max_n=200, log=lambda m: refresh.log("--- " + m)
                                   if not m.endswith(" 0 to fetch") else None)
    except Exception as e:
        refresh.log(f"--- film pages: ERROR {e}")

    # Greek release titles: IMDb's dataset monthly, athinorama and filmy.gr
    # pages for films new to the lists — a few hundred requests at most.
    try:
        import letterboxd_greek_titles as gt
        gt.log = lambda m: refresh.log("--- " + m) if " 0 pages not yet read" not in m else None
        gt.refresh_imdb_titles()
        gt.fetch_imdb_ids(list(gt.films()))
        gt.fetch_athinorama(max_n=150)
        gt.fetch_filmy(max_n=150)
    except Exception as e:
        refresh.log(f"--- Greek titles: ERROR {e}")

    # Greek summaries (TMDB) for films new to the pages — only once a key is saved.
    try:
        import letterboxd_tmdb
        if letterboxd_tmdb.key():
            letterboxd_tmdb.fetch(max_n=300, log=lambda m: refresh.log("--- " + m)
                                  if not m.endswith(" 0 to check") else None)
    except SystemExit as e:
        refresh.log(f"--- greek summaries: {e}")
    except Exception as e:
        refresh.log(f"--- greek summaries: ERROR {e}")

    # If the Mac slept partway through this tick, this year or last may have
    # come due meanwhile. Catch up now, so the self-check below judges fresh
    # data instead of raising a false "out of date" alarm.
    for y in hot_years:
        if y not in years_due and year_is_due(y):
            refresh.log(f"--- {y} came due while this tick was running — refreshing now")
            refresh.refresh(y)

    # Standing health checks. Serious trouble that wasn't there last hour
    # gets a notification, so a broken scrape doesn't sit unnoticed.
    try:
        import letterboxd_selfcheck as selfcheck
        res = selfcheck.run()
        selfcheck.notify(res)
        for p in res["problems"]:
            refresh.log(f"--- selfcheck {p['level']}: {p['what']} — {p['detail']}")
    except Exception as e:
        refresh.log(f"--- selfcheck: ERROR {e}")

    refresh.log("tick done")


if __name__ == "__main__":
    main()
