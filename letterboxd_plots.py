"""
A daily batch of short film summaries, so a film with a poster doesn't stay
without a plot. Claude writes them during the 10 AM run; this script chooses
the films, keeps the original synopses away from the writer, and checks and
saves the results.

Order: recent films missing Greek (both languages), then the Greek backlog,
most watched first, then the English backlog. Only summaries that are missing
are ever written. A film nobody can summarise is set aside, not retried.

    letterboxd_plots.py facts [--max 30]          # fetch fact sheets for new poster films (the hourly tick)
    letterboxd_plots.py next OUT.json [--n 100]   # the day's batch, with synopses, for the note-taker
    letterboxd_plots.py notes BATCH NOTES OUT     # batch + notes -> the writer's copy, no synopses
    letterboxd_plots.py save BATCH RESULT         # check {slug: {"el": ..., "en": ...}} and save
    letterboxd_plots.py status
"""

import os
import re
import sys
import json
import html
import random
from datetime import datetime

HOME = os.path.expanduser("~")
sys.path.insert(0, os.path.join(HOME, "Desktop"))
CACHE = os.path.join(HOME, ".letterboxd_cache")
FACTS = os.path.join(CACHE, "plot_facts.json")
EL = os.path.join(CACHE, "plots_el_claude.json")
EN = os.path.join(CACHE, "plots_en_claude.json")
SKIPPED = os.path.join(CACHE, "plots_skipped.json")     # {slug: [languages]} nobody could summarise
RECENT_YEARS = 2
GREEK = re.compile("[Ͱ-Ͽἀ-῿]")


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, d):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, path)


def pick(wanted, films, has_el, has_en, skipped, n, this_year):
    """[(slug, [languages])], at most n: recent films missing Greek, Greek backlog, then English backlog."""
    count = lambda s: -films.get(s, ("", "", 0))[2]
    year = lambda s: str(films.get(s, ("", "", 0))[1])
    recent = lambda s: year(s) >= str(this_year - RECENT_YEARS)
    open_el = [s for s in wanted if s not in has_el and "el" not in skipped.get(s, [])]
    open_en = [s for s in wanted if s not in has_en and "en" not in skipped.get(s, [])]
    en_too = set(open_en)
    first = sorted((s for s in open_el if recent(s)), key=count)
    second = sorted((s for s in open_el if not recent(s)), key=count)
    third = sorted(open_en, key=lambda s: (not recent(s), count(s)))
    out = [(s, ["el", "en"] if s in en_too else ["el"]) for s in first]
    out += [(s, ["el"]) for s in second]
    chosen = {s for s, _ in out}
    out += [(s, ["en"]) for s in third if s not in chosen]
    return out[:n]


def _state():
    import letterboxd_posters as lp
    import letterboxd_tmdb as tm
    import letterboxd_greek_titles as gt
    return list(lp.wanted()), gt, tm.plots()


def fetch_facts(limit=30, log=print):
    """Fact sheets (director, cast, synopsis) for poster films that have none yet. Returns how many were added."""
    import letterboxd_net as net
    wanted, gt, plots = _state()
    facts = _load(FACTS, {})
    en = _load(EN, {})
    todo = [s for s in wanted if s not in facts and (s not in plots or s not in en)][:limit]
    if not todo:
        return 0
    films, session, added, fails = gt.films(), net.session(), 0, 0
    for slug in todo:
        try:
            r = session.get(f"https://letterboxd.com/film/{slug}/", timeout=20)
            if r.status_code != 200:
                raise RuntimeError(r.status_code)
            p = r.text
            d = re.search(r'<meta name="description" content="([^"]*)"', p)
            dirs = list(dict.fromkeys(re.findall(r'/director/[^/]+/"[^>]*>(?:<span[^>]*>)?([^<]+)<', p)))[:2]
            cast = re.findall(r'/actor/[^/]+/"[^>]*>([^<]+)<', p)[:4]
            t, y, _ = films.get(slug, (slug, "", 0))
            facts[slug] = {"title": t, "year": y, "director": [html.unescape(x) for x in dirs],
                           "cast": [html.unescape(x) for x in cast],
                           "synopsis": html.unescape(d.group(1)) if d else ""}
            added += 1
            fails = 0
        except Exception:
            fails += 1
            if fails >= 3:
                break                             # Letterboxd isn't answering: next hour
    if added:
        _save(FACTS, facts)
    log(f"--- plot facts: {added} added, {len(todo) - added} left for later")
    return added


def next_batch(out, n=100):
    wanted, gt, plots = _state()
    facts, skipped = _load(FACTS, {}), _load(SKIPPED, {})
    ready = [s for s in wanted if s in facts]
    chosen = pick(ready, gt.films(), set(plots), set(k for k, v in _load(EN, {}).items() if v),
                  skipped, n, datetime.now().year)
    greek = gt.resolve_all()
    batch = []
    for slug, langs in chosen:
        f = dict(facts[slug], slug=slug, write=langs)
        for k in ("director", "cast"):
            f[k] = [x.strip() for x in f.get(k, []) if x.strip() and "*/" not in x]
        el = greek.get(slug, ("", ""))[0]
        if el and GREEK.search(el):
            f["greek_title"] = el
        batch.append(f)
    with open(out, "w") as f:
        json.dump(batch, f, ensure_ascii=False, indent=1)
    both = sum(len(x["write"]) == 2 for x in batch)
    print(f"batch: {len(batch)} films ({both} in both languages)")


def writer_copy(batch, notes, out):
    """The writer's copy has the notes in shuffled order and no synopsis, so the original's wording can't carry over."""
    notes = _load(notes, {})
    rows = []
    for x in _load(batch, []):
        y = {k: v for k, v in x.items() if k != "synopsis"}
        items = [i.strip() for i in (notes.get(x["slug"]) or "").split("|") if i.strip()]
        random.Random(x["slug"]).shuffle(items)
        y["notes"] = " | ".join(items)
        rows.append(y)
    with open(out, "w") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print(f"writer copy: {len(rows)} films, {sum(1 for y in rows if not y['notes'])} without notes")


def check(text, lang, synopsis=""):
    """Why this summary shouldn't be saved, or '' if it's fine."""
    letters = [c for c in text if c.isalpha()]
    share = sum(1 for c in letters if GREEK.match(c)) / max(1, len(letters))
    words = len(text.split())
    if lang == "el" and share < 0.6:
        return f"only {share:.0%} Greek letters"
    if lang == "en" and share > 0.1:
        return "not in English"
    if not 15 <= words <= 110:
        return f"{words} words"
    if synopsis and len(synopsis) > 200 and abs(len(text) - len(synopsis)) < 0.03 * len(synopsis):
        return "same length as the synopsis: may be a translation"
    return ""


def merge(batch, result, el, en, skipped):
    """Adds the good summaries to el/en and missing ones to skipped; returns (saved, skipped, [problems])."""
    saved, set_aside, problems = 0, 0, []
    for x in batch:
        slug, got = x["slug"], result.get(x["slug"]) or {}
        for lang in x.get("write", []):
            store = el if lang == "el" else en
            text = re.sub(r"\s+", " ", got.get(lang) or "").strip()
            if store.get(slug):
                continue                          # never replace a summary already there
            if not text:
                skipped.setdefault(slug, [])
                if lang not in skipped[slug]:
                    skipped[slug].append(lang)
                set_aside += 1
                continue
            why = check(text, lang, x.get("synopsis", ""))
            if why:
                problems.append(f"{slug} ({lang}): {why}")
            else:
                store[slug] = text
                saved += 1
    return saved, set_aside, problems


def save(batch, result):
    el, en, skipped = _load(EL, {}), _load(EN, {}), _load(SKIPPED, {})
    saved, set_aside, problems = merge(_load(batch, []), _load(result, {}), el, en, skipped)
    _save(EL, el)
    _save(EN, en)
    _save(SKIPPED, skipped)
    print(f"saved {saved} summaries, set aside {set_aside}")
    for p in problems:
        print("not saved:", p)
    status()


def status():
    wanted, gt, plots = _state()
    en = {k for k, v in _load(EN, {}).items() if v}
    print(f"films with posters: {len(wanted)}; no Greek summary: {sum(s not in plots for s in wanted)}; "
          f"no English summary: {sum(s not in en for s in wanted)}; "
          f"set aside: {len(_load(SKIPPED, {}))}")


def main():
    a = sys.argv[1:]
    num = lambda flag, d: int(a[a.index(flag) + 1]) if flag in a else d
    if a[:1] == ["facts"]:
        fetch_facts(num("--max", 30))
    elif a[:1] == ["next"] and len(a) >= 2:
        next_batch(a[1], num("--n", 100))
    elif a[:1] == ["notes"] and len(a) == 4:
        writer_copy(a[1], a[2], a[3])
    elif a[:1] == ["save"] and len(a) == 3:
        save(a[1], a[2])
    elif a[:1] == ["status"]:
        status()
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
