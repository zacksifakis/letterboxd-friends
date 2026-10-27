"""
The weekend box office, pasted into the app, as a hint for whether a film
showing in Madison is held over next week.

The newsletter's "next week" section only previews what's new, so a regular
run that isn't in it is marked "next week ?". Its weekend numbers usually
answer that: a film still near the top of the chart, or in its first two
weeks, stays; one with a small take per theater and several weeks behind it is
usually dropped. These are guesses and the app labels them that way.

Parses a weekend chart copied from a box-office site, either one row per line
(tab- or space-separated) or one cell per line, the way some browsers copy
tables. Nothing is fetched: the chart is whatever you paste.
"""

import os
import re
import json
import time
import difflib
import unicodedata

CACHE = os.path.join(os.path.expanduser("~"), ".letterboxd_cache", "boxoffice.json")
KEEP_DAYS = 6             # a chart is about one weekend; after that the next one matters

_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(m(?:illion)?|k|b(?:illion)?)?\b", re.I)
_PCT = re.compile(r"([+\-−–]?\s?\d[\d,]*(?:\.\d+)?)\s?%")
_INT = re.compile(r"(?<![\w$.,])([+\-−–]?)(\d{1,3}(?:,\d{3})+|\d+)(?![\w%.,]|\.\d)")


def _money(num, unit):
    v = float(num.replace(",", ""))
    unit = (unit or "").lower()[:1]
    return v * {"m": 1e6, "k": 1e3, "b": 1e9}.get(unit, 1)


def norm(title):
    t = unicodedata.normalize("NFKD", re.sub(r"[’'‘]", " ", title)).encode("ascii", "ignore").decode().lower()
    t = t.replace("&", " and ")
    t = re.sub(r"\(\d{4}\)", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    return re.sub(r"^(the|a|an) ", "", t)


def _rows(text):
    """The chart split into rows, each a list of cells."""
    lines = [l.strip() for l in text.replace("\r", "").split("\n")]
    lines = [l for l in lines if l]
    # One row per line: a line that has both a title and a dollar figure.
    whole = [l for l in lines if _MONEY.search(l) and re.search(r"[A-Za-z]{2}", _MONEY.sub("", l))]
    if len(whole) >= max(1, len([l for l in lines if _MONEY.search(l)]) // 2):
        # A row can wrap: Box Office Mojo puts "2026 Re-release" under a
        # title, leaving the figures on a line of their own.
        rows, held = [], []
        for l in lines:
            if not _MONEY.search(l):
                held = held[-3:] + [l]
                continue
            if l not in whole:
                if not held:
                    continue
                l = "\t".join(held + [l])
            held = []
            rows.append(re.split(r"\t+|\s{2,}", l) if "\t" in l or "  " in l else [l])
        return rows
    # One cell per line: a row starts at the bare rank number that comes next (1, 2, 3 …).
    # A weeks-in-release cell can be the same number, so the rank also has to
    # be followed by a title with a dollar figure right after it.
    def starts_row(i):
        j = i + 1
        while j < len(lines) and re.fullmatch(r"\(?\d{1,3}\)?|[-–—]|new", lines[j], re.I):
            j += 1                                   # last week's rank
        return (j < len(lines) and re.search(r"[A-Za-z]{2}", lines[j])
                and any(_MONEY.search(l) for l in lines[j + 1:j + 3]))
    rows, cur, want = [], None, 1
    for i, l in enumerate(lines):
        if l == str(want) and starts_row(i):
            cur = [l]
            rows.append(cur)
            want += 1
        elif cur is not None:
            cur.append(l)
    return rows


def parse(text):
    """[{title, rank, gross, change, theaters, thChange, avg, total, weeks}], in chart order."""
    out = []
    for cells in _rows(text):
        line = "\t".join(cells)
        first = _MONEY.search(line)
        if not first:
            continue
        head, tail = line[:first.start()], line[first.start():]
        monies = [_money(n, u) for n, u in _MONEY.findall(tail)]
        # Title: the head's text cells (rank and last week's rank are numbers).
        texts = [c.strip() for c in re.split(r"\t", head)
                 if re.search(r"[A-Za-z]{2}", c) and c.strip().lower() not in ("new", "n/a")
                 and not re.fullmatch(r"(?:\d{4} )?re-?release", c.strip(), re.I)]
        if not texts:
            continue
        if len(cells) == 1:                          # a space-separated line: drop the ranks in front
            texts[0] = re.sub(r"^(?:(?:\(?\d{1,3}\)?|[-–—]|new)\s+)+", "", texts[0], flags=re.I)
        rank = re.match(r"\s*(\d{1,3})\b", head)
        pct = _PCT.search(tail)
        ints = []                  # counts after the gross: theaters, change, weeks
        for sign, n in _INT.findall(_PCT.sub(" ", _MONEY.sub(" ", tail))):
            ints.append((sign.replace("−", "-").replace("–", "-"), int(n.replace(",", ""))))
        unsigned = [n for s, n in ints if not s]
        signed = [int(s + "1") * n for s, n in ints if s]
        after_total = _MONEY.sub(" ", tail[tail.rfind("$"):]) if len(monies) > 1 else ""
        weeks = [int(n) for n in re.findall(r"(?<![\w,])(\d{1,2})(?![\w,%])", after_total)]
        theaters = next((n for n in unsigned if n >= 10), None)
        out.append({
            "title": texts[0], "rank": int(rank.group(1)) if rank else len(out) + 1,
            "gross": monies[0],
            "change": float(re.sub(r"[\s,]", "", pct.group(1)).replace("−", "-").replace("–", "-")) if pct else None,
            "theaters": theaters,
            "thChange": signed[0] if signed else None,
            "avg": monies[1] if len(monies) >= 3 else (monies[0] / theaters if theaters else None),
            "total": monies[-1] if len(monies) >= 2 else None,
            "weeks": weeks[0] if weeks else None,
            "reissue": bool(re.search(r"re-?release", line, re.I)),
            "loose": len(cells) == 1,                # title may run into the distributor
        })
    return out


def match(rows, title):
    """The chart row for a film title, or None."""
    want = norm(title)
    if not want:
        return None
    for r in rows:
        if norm(r["title"]) == want:
            return r
    for r in rows:                                   # a distributor run on after the title
        rest = norm(r["title"])[len(want) + 1:]
        if (r.get("loose") and len(want) >= 4 and norm(r["title"]).startswith(want + " ")
                and not re.match(r"(\d|[ivx]+\b|part\b|chapter\b)", rest)):   # not a sequel
            return r
    for r in rows:                                   # "Ken Russell's The Devils" is "The Devils"
        t = norm(r["title"])
        if len(t) >= 4 and re.fullmatch(r".+ s (?:(?:the|a|an) )?" + re.escape(t), want):
            return r
    nums = lambda t: re.findall(r"\b(?:\d+|[ivx]+)\b", t)     # a sequel number must agree
    best, score = None, 0.0
    for r in rows:
        if nums(norm(r["title"])) != nums(want):
            continue
        s = difflib.SequenceMatcher(None, norm(r["title"]), want).ratio()
        if s > score:
            best, score = r, s
    return best if score >= 0.9 else None


def guess(r):
    """'likely', 'unlikely' or '' (can't tell), and why."""
    weeks, gross, avg, rank = r.get("weeks"), r.get("gross") or 0, r.get("avg"), r.get("rank") or 99
    th, thc = r.get("theaters"), r.get("thChange")
    if r.get("reissue"):
        return "", "a re-release, often booked for a set number of days"
    if thc is not None and th and thc < 0 and -thc >= 0.4 * (th - thc):
        return "unlikely", "already losing theaters fast"
    if weeks is not None and weeks <= 2:
        return "likely", "only its first or second week"
    if gross >= 2e6 or (rank <= 10 and (r.get("change") is None or r["change"] > -60)):
        return "likely", "still near the top of the chart"
    if avg is not None and avg < 1000 and (weeks is None or weeks >= 3):
        return "unlikely", "a small take per theater"
    if gross < 5e5 and weeks is not None and weeks >= 4:
        return "unlikely", "a small weekend several weeks in"
    return "", ""


def weekend(text):
    """The weekend the chart is for, as the page puts it ("October 23-25, 2026"), or ''."""
    m = re.search(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2}\s?[-–]\s?"
                  r"(?:[A-Z][a-z]+\.? )?\d{1,2},? \d{4}", text)
    return m.group(0) if m else ""


def save(text):
    rows = parse(text) if text.strip() else []
    tmp = CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"at": time.time(), "weekend": weekend(text), "rows": rows}, f)
    os.replace(tmp, CACHE)
    return rows


def load():
    """The pasted chart's rows, if it's recent enough to use."""
    try:
        with open(CACHE) as f:
            d = json.load(f)
    except Exception:
        return None
    if not d.get("rows") or time.time() - d.get("at", 0) > KEEP_DAYS * 86400:
        return None
    return d
