"""
Refresh every decade of friends-watched data, one after another.

    python3 letterboxd_refresh.py              # all decades that have data
    python3 letterboxd_refresh.py 1990s 2000s  # just these
    python3 letterboxd_refresh.py --stale 14   # only ones older than 14 days
    python3 letterboxd_refresh.py --current-year   # just this calendar year

Run on a schedule by ~/Library/LaunchAgents/local.letterboxd.refresh.plist.

Scraping a decade also covers every year inside it: the CSV carries a Year
column, so the app can serve any single year from its decade without a
separate scrape.
"""

import os
import re
import sys
import csv
import time
import shutil
import subprocess
from datetime import datetime

HOME = os.path.expanduser("~")
SCRAPER = os.path.join(HOME, "Desktop", "letterboxd_friends_v2.py")
HISTORY = os.path.join(HOME, ".letterboxd_history")
LOG = os.path.join(HOME, "letterboxd_refresh.log")

# Decades worth scraping. Anything earlier has almost nothing logged.
DECADES = ["1920s", "1930s", "1940s", "1950s", "1960s", "1970s",
           "1980s", "1990s", "2000s", "2010s", "2020s"]


def _stdout_is_log():
    # Under launchd, stdout already *is* the log file — printing as well would
    # write every line twice.
    try:
        a, b = os.fstat(sys.stdout.fileno()), os.stat(LOG)
        return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
    except Exception:
        return False


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    if not _stdout_is_log():
        print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def csv_path(p):
    return os.path.join(HOME, f"friends_watched_{p}.csv")


def age_days(p):
    path = csv_path(p)
    if not os.path.exists(path):
        return None
    return (time.time() - os.path.getmtime(path)) / 86400


def summarise(period):
    """Top film + row count, for the log."""
    path = csv_path(period)
    if not os.path.exists(path):
        return "no output"
    rows = list(csv.DictReader(open(path)))
    if not rows:
        return "0 films"
    top = max(rows, key=lambda r: int(r.get("Friend Watch Count") or 0))
    return (f"{len(rows):,} films, top: {top.get('Film Title')} "
            f"({top.get('Friend Watch Count')})")


def refresh(period):
    if os.path.exists(csv_path(period)):
        os.makedirs(HISTORY, exist_ok=True)
        shutil.copy2(csv_path(period), os.path.join(HISTORY, f"{period}.prev.csv"))

    log(f"--- {period}: starting")
    before = os.path.getmtime(csv_path(period)) if os.path.exists(csv_path(period)) else 0
    started = time.time()
    proc = subprocess.run([sys.executable, "-u", SCRAPER, period],
                          capture_output=True, text=True, cwd=HOME)
    out = proc.stdout or ""

    # Record every run, so a strange one leaves evidence behind.
    with open(os.path.join(HOME, "letterboxd_runs.log"), "a") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {period} rc={proc.returncode} "
                f"{time.time() - started:.1f}s exe={sys.executable}\n"
                f"  stdout: {out[:300]!r}\n  stderr: {(proc.stderr or '')[:300]!r}\n")

    # A clean exit that didn't rewrite the CSV isn't a refresh, whatever the
    # exit code says — treat it as a failure instead of reporting old data.
    after = os.path.getmtime(csv_path(period)) if os.path.exists(csv_path(period)) else 0
    if proc.returncode == 0 and after <= before:
        dump = os.path.join(HOME, f"letterboxd_failure_{period}_{datetime.now():%Y%m%d_%H%M}.log")
        with open(dump, "w") as f:
            f.write(out + "\n--- stderr ---\n" + (proc.stderr or ""))
        log(f"--- {period}: FAILED — exited after {time.time() - started:.0f}s without "
            f"writing new data; output saved to {dump}")
        return False

    incomplete = [l.strip() for l in out.splitlines()
                  if l.strip().startswith("INCOMPLETE friends") and not l.strip().endswith(": 0")]
    clean = "Every friend fetched completely" in out
    mins = (time.time() - started) / 60

    if proc.returncode != 0:
        # Keep the whole output — the useful part (retries, per-friend progress)
        # is never in the last few lines.
        dump = os.path.join(HOME, f"letterboxd_failure_{period}_{datetime.now():%Y%m%d_%H%M}.log")
        with open(dump, "w") as f:
            f.write(out + "\n--- stderr ---\n" + (proc.stderr or ""))
        summary = [l for l in out.splitlines() if l.strip()][-1:] or ["no output"]
        log(f"--- {period}: FAILED rc={proc.returncode} :: {summary[0][:160]}")
        log(f"--- {period}: full output saved to {dump}")
        return False
    # File a dated copy, so the app can show what changed over a week or a month.
    try:
        import letterboxd_history
        letterboxd_history.save(period)
    except Exception as e:
        log(f"--- {period}: could not file a dated copy ({e})")

    filled = [l.strip() for l in out.splitlines()
              if l.strip().startswith("filled from previous") and not l.strip().endswith(": 0")]
    log(f"--- {period}: done in {mins:.0f}m — {summarise(period)}"
        + ("  [verified]" if clean else "")
        + (f"  [{filled[0].split('->')[0].strip()}]" if filled else "")
        + (f"  [{incomplete[0]}]" if incomplete else ""))
    return True


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    stale = None
    if "--stale" in sys.argv:
        i = sys.argv.index("--stale")
        stale = float(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 14.0

    if "--current-year" in sys.argv:
        # The daily job also fetches Madison listings, so they're current the
        # moment the app opens. Done first, so a failed scrape can't skip it.
        try:
            sys.path.insert(0, os.path.join(HOME, "Desktop"))
            import letterboxd_app
            letterboxd_app.refresh_madison()
            err = letterboxd_app.madison_job["error"]
            data = letterboxd_app.load_madison() or {}
            log("--- madison listings: " + (f"ERROR {err}" if err else
                f"updated from {(data.get('latest') or {}).get('title', '?')}"))
        except Exception as e:
            log(f"--- madison listings: ERROR {e}")
        # Resolved at run time, so the daily job rolls over to 2027 by itself.
        periods = [str(datetime.now().year)]
    else:
        periods = [a for a in args if re.fullmatch(r"[0-9]{4}s?", a)] or DECADES
    if stale is not None:
        keep = []
        for p in periods:
            a = age_days(p)
            if a is None or a >= stale:
                keep.append(p)
            else:
                log(f"--- {p}: skipping, only {a:.1f} days old")
        periods = keep

    if not periods:
        log("nothing to refresh")
        return

    log(f"=== refresh starting: {', '.join(periods)}")
    t0, ok = time.time(), 0
    for p in periods:
        try:
            ok += bool(refresh(p))
        except Exception as e:
            log(f"--- {p}: ERROR {e}")
        time.sleep(20)          # breathe between decades
    log(f"=== refresh finished: {ok}/{len(periods)} ok in {(time.time()-t0)/60:.0f}m")

    # Rebuild and publish the website — fortnightly run only, not the daily
    # current-year one, so the site updates on the two-week rhythm.
    site = os.path.join(HOME, "Desktop", "letterboxd_site.py")
    if ok and os.path.exists(site) and "--current-year" not in sys.argv:
        log("=== updating the website")
        r = subprocess.run([sys.executable, site], capture_output=True, text=True)
        for line in (r.stdout + r.stderr).splitlines():
            if line.strip():
                log("    " + line.rstrip())


if __name__ == "__main__":
    main()
