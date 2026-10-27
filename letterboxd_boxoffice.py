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
import datetime
import math
import unicodedata

CACHE_DIR = os.path.join(os.path.expanduser("~"), ".letterboxd_cache")
CACHE = os.path.join(CACHE_DIR, "boxoffice.json")
HISTORY = os.path.join(CACHE_DIR, "boxoffice_history.json")    # Friday of the weekend -> chart
WEEKS = os.path.join(CACHE_DIR, "madison_weeks.json")          # every newsletter week's listings
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


_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def friday(label, today=None):
    """The weekend's Friday as an ISO date: from "October 23-25, 2026", or,
    with no label, the last Friday before today (charts come out Sunday/Monday)."""
    m = re.match(r"([A-Za-z]{3})[a-z]*\.? (\d{1,2}).*?(\d{4})", label or "")
    if m and m.group(1).lower() in _MONTHS:
        return datetime.date(int(m.group(3)), _MONTHS.index(m.group(1).lower()) + 1, int(m.group(2))).isoformat()
    d = today or datetime.date.today()
    return (d - datetime.timedelta(days=(d.weekday() - 4) % 7 or 7)).isoformat()


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, d):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, path)


def split(text):
    """Several charts pasted together, one per weekend heading ("October 23-25, 2026")."""
    starts = [m.start() for m in re.finditer(
        r"(?m)^\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2}\s?[-–]", text)]
    if len(starts) < 2:
        return [text]
    return [text[a:b] for a, b in zip([0] + starts[1:], starts[1:] + [len(text)])]


def save(text):
    """Use the newest chart now, and keep them all so the guesses can be checked later.
    Returns (rows in the newest chart, weekends saved)."""
    charts = [(friday(weekend(t)), weekend(t), parse(t)) for t in split(text)] if text.strip() else []
    charts = [c for c in charts if c[2]]
    if charts:
        hist = _load(HISTORY, {})
        for fri, label, rows in charts:
            hist[fri] = {"weekend": label, "at": time.time(), "rows": rows}
        _save(HISTORY, hist)
    newest = max(charts) if charts else ("", "", [])
    if not text.strip() or charts:
        _save(CACHE, {"at": time.time(), "weekend": newest[1], "rows": newest[2]})
    return newest[2], [c[0] for c in charts]


def load(week=None):
    """The pasted chart, if it's for the weekend inside `week` (start, end): that
    weekend, charted on Monday, is what the newsletter at the end of the week
    decides on. Without a week, any chart pasted in the last few days."""
    try:
        with open(CACHE) as f:
            d = json.load(f)
    except Exception:
        return None
    if not d.get("rows"):
        return None
    fri = friday(d.get("weekend", ""), datetime.date.fromtimestamp(d.get("at", 0)))
    d["friday"] = fri
    if week:
        return d if week[0].isoformat() <= fri <= week[1].isoformat() else None
    return d if time.time() - d.get("at", 0) <= KEEP_DAYS * 86400 else None


# ------------------------------------------------- learning from past weeks
#
# A chart for the weekend of Friday F belongs to the newsletter week holding F
# (weeks run Thursday to Wednesday). Each regular run that week that's on the
# chart is one example; whether it's in the following week's listings is the
# answer. With enough examples, a small logistic model fitted to them replaces
# the fixed rules, but only if it does better on weeks it wasn't fitted to.

MIN_EXAMPLES = 30


def examples():
    """[(chart row, held over?, Friday)] for every week with both a chart and the next week's listings."""
    hist, weeks = _load(HISTORY, {}), _load(WEEKS, {})
    out = []
    for fri, chart in sorted(hist.items()):
        this = next((w for s, w in weeks.items() if s <= fri <= w["end"]), None)
        if not this:
            continue
        nxt = weeks.get((datetime.date.fromisoformat(this["end"]) + datetime.timedelta(days=1)).isoformat())
        if not nxt:
            continue
        later = {norm(f["title"]) for f in nxt["films"].values()} | set(nxt["films"])
        for slug, f in this["films"].items():
            if not f.get("regular"):
                continue
            r = match(chart["rows"], f["title"])
            if r and not r.get("reissue"):
                out.append((r, slug in later or norm(f["title"]) in later, fri))
    return out


def _features(r):
    th, thc = r.get("theaters") or 0, r.get("thChange") or 0
    before = th - thc
    return [math.log10((r.get("gross") or 0) + 1),
            min(r.get("weeks") or 3, 10),
            thc / before if before > 0 else 0.0,
            math.log10((r.get("avg") or 0) + 1)]


def _fit(X, y, l2=1.0):
    """Logistic regression by Newton's method; X is already standardised, with a bias column."""
    k = len(X[0])
    w = [0.0] * k
    for _ in range(25):
        p = [1 / (1 + math.exp(-max(-30, min(30, sum(a * b for a, b in zip(w, x)))))) for x in X]
        g = [sum((p[i] - y[i]) * X[i][j] for i in range(len(X))) + (l2 * w[j] if j else 0) for j in range(k)]
        H = [[sum(p[i] * (1 - p[i]) * X[i][a] * X[i][b] for i in range(len(X))) + (l2 if a == b and a else 0)
              for b in range(k)] for a in range(k)]
        H[0][0] += 1e-6
        step = _solve(H, g)
        w = [a - b for a, b in zip(w, step)]
        if max(abs(v) for v in step) < 1e-6:
            break
    return w


def _solve(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[piv] = M[piv], M[c]
        if abs(M[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                M[r] = [a - f * b for a, b in zip(M[r], M[c])]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]


def _model(exs):
    raw = [_features(r) for r, _, _ in exs]
    mean = [sum(c) / len(c) for c in zip(*raw)]
    sd = [max(1e-6, (sum((v - m) ** 2 for v in c) / len(c)) ** 0.5) for c, m in zip(zip(*raw), mean)]
    X = [[1.0] + [(v - m) / s for v, m, s in zip(x, mean, sd)] for x in raw]
    return {"w": _fit(X, [1.0 if h else 0.0 for _, h, _ in exs]), "mean": mean, "sd": sd}


def _prob(model, r):
    x = [1.0] + [(v - m) / s for v, m, s in zip(_features(r), model["mean"], model["sd"])]
    return 1 / (1 + math.exp(-max(-30, min(30, sum(a * b for a, b in zip(model["w"], x))))))


def _label(p):
    return "likely" if p >= 0.7 else "unlikely" if p <= 0.3 else ""


_learned = {}


def learn():
    """What past weeks say: how the rules have done, hold-over rates, and a fitted model if it earns its place."""
    key = tuple(os.path.getmtime(p) if os.path.exists(p) else 0 for p in (HISTORY, WEEKS))
    if _learned.get("key") == key:
        return _learned["value"]
    exs = examples()
    rules_right = sum(1 for r, held, _ in exs if guess(r)[0] == ("likely" if held else "unlikely"))
    rules_sure = sum(1 for r, _, _ in exs if guess(r)[0])

    def rate(pick):
        sub = [h for r, h, _ in exs if pick(r)]
        return [len(sub), sum(sub)]
    trends = {
        "weeks": [["weeks 1\u20132", rate(lambda r: (r.get("weeks") or 0) <= 2)],
                  ["weeks 3\u20134", rate(lambda r: 3 <= (r.get("weeks") or 0) <= 4)],
                  ["week 5+", rate(lambda r: (r.get("weeks") or 0) >= 5)]],
        "avg": [["under $1,000 a theater", rate(lambda r: (r.get("avg") or 0) < 1000)],
                ["$1,000\u20133,000", rate(lambda r: 1000 <= (r.get("avg") or 0) < 3000)],
                ["$3,000+", rate(lambda r: (r.get("avg") or 0) >= 3000)]],
    }
    value = {"examples": len(exs), "weekends": len({f for _, _, f in exs}), "held": sum(h for _, h, _ in exs),
             "rulesRight": rules_right, "rulesSure": rules_sure, "trends": trends, "model": None}
    held = value["held"]
    if len(exs) >= MIN_EXAMPLES and min(held, len(exs) - held) >= 5:
        # Test on whole weekends left out, so one big week can't flatter it.
        right = sure = 0
        for fri in {f for _, _, f in exs}:
            m = _model([e for e in exs if e[2] != fri])
            for r, h, f in exs:
                if f == fri:
                    lab = _label(_prob(m, r))
                    sure += bool(lab)
                    right += lab == ("likely" if h else "unlikely")
        value["modelRight"], value["modelSure"] = right, sure
        if right > rules_right:
            value["model"] = _model(exs)
    _learned.update(key=key, value=value)
    return value


def hint(r):
    """guess(), from the fitted model once there is one."""
    m = learn().get("model")
    if m and not r.get("reissue"):
        p = _prob(m, r)
        return _label(p), f"{round(p * 100)}% chance by the pattern in past weeks"
    return guess(r)
