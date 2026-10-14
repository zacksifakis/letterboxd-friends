"""
Dated copies of each list, so changes can be measured over any stretch of time,
not just since the last scrape.

Every finished scrape files a copy under
~/.letterboxd_history/snapshots/<period>/<YYYY-MM-DD>.csv (one per day — a
second scrape the same day replaces it). Older copies are thinned to one a
week after a month, and dropped after six months.

    python3 letterboxd_history.py seed     # file what's on disk right now
    python3 letterboxd_history.py list 2026
"""

import os
import csv
import sys
import glob
import shutil
import datetime

HOME = os.path.expanduser("~")
HISTORY = os.path.join(HOME, ".letterboxd_history")
SNAPS = os.path.join(HISTORY, "snapshots")
KEEP_DAILY_DAYS = 30
KEEP_DAYS = 180


def csv_path(period):
    return os.path.join(HOME, f"friends_watched_{period}.csv")


def _dir(period):
    return os.path.join(SNAPS, period)


def save(period, when=None):
    """File the current list under the date it was written. Returns the path."""
    src = csv_path(period)
    if not os.path.exists(src):
        return None
    when = when or datetime.date.fromtimestamp(os.path.getmtime(src))
    os.makedirs(_dir(period), exist_ok=True)
    dst = os.path.join(_dir(period), f"{when.isoformat()}.csv")
    shutil.copy2(src, dst)
    prune(period)
    return dst


def snapshots(period):
    """[(date, path), ...] oldest first."""
    out = []
    for p in glob.glob(os.path.join(_dir(period), "*.csv")):
        try:
            out.append((datetime.date.fromisoformat(os.path.basename(p)[:-4]), p))
        except ValueError:
            pass
    return sorted(out)


def at_or_before(period, when):
    """The newest copy filed on or before `when`, or None."""
    older = [s for s in snapshots(period) if s[0] <= when]
    return older[-1] if older else None


def oldest(period):
    snaps = snapshots(period)
    return snaps[0] if snaps else None


def prune(period):
    """Daily for a month, then weekly, then gone after six months."""
    today = datetime.date.today()
    kept_weeks = set()
    for date, path in reversed(snapshots(period)):        # newest first
        age = (today - date).days
        if age <= KEEP_DAILY_DAYS:
            continue
        if age > KEEP_DAYS:
            os.remove(path)
            continue
        week = date.isocalendar()[:2]
        if week in kept_weeks:
            os.remove(path)
        else:
            kept_weeks.add(week)


def periods():
    import re
    out = []
    for p in glob.glob(f"{HOME}/friends_watched_*.csv"):
        m = re.fullmatch(r"friends_watched_([0-9]{4}s?)\.csv", os.path.basename(p))
        if m:
            out.append(m.group(1))
    return sorted(out)


def seed(log=print):
    """
    File what's already on disk: today's lists, plus the single previous copy
    each period kept before snapshots existed (dated by when it was written),
    so week-over-week comparisons work from the start where possible.
    """
    filed = 0
    for period in periods():
        prev = os.path.join(HISTORY, f"{period}.prev.csv")
        if os.path.exists(prev):
            when = datetime.date.fromtimestamp(os.path.getmtime(prev))
            dst = os.path.join(_dir(period), f"{when.isoformat()}.csv")
            if not os.path.exists(dst):
                os.makedirs(_dir(period), exist_ok=True)
                shutil.copy2(prev, dst)
                filed += 1
        if save(period):
            filed += 1
    log(f"snapshots: filed {filed} copies for {len(periods())} lists")
    return filed


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "seed"
    if cmd == "seed":
        seed()
    elif cmd == "list":
        period = sys.argv[2] if len(sys.argv) > 2 else str(datetime.date.today().year)
        for date, path in snapshots(period):
            rows = sum(1 for _ in open(path)) - 1
            print(f"  {date}  {rows:7,} films  {path}")
    else:
        print(__doc__)
