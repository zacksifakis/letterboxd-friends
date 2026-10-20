"""
Build the Greek edition of the "Seats" page ("168 Θέσεις").

    python3 letterboxd_publish_el.py OUTPUT.html                # for claude.ai
    python3 letterboxd_publish_el.py index.html --standalone    # for any web host

Needs the Wikidata cache from letterboxd_wikidata.py for Greek titles, cast,
directors, genres, running times and links. Films Wikidata has no Greek data
for keep their original title and names — nothing is machine-transliterated.

Only aggregate counts are included — never which person watched what.
"""

import os
import re
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import letterboxd_publish as base  # noqa: E402  (shared CSV loading)

HOME = os.path.expanduser("~")
WIKIDATA = os.path.join(HOME, ".letterboxd_cache", "wikidata_el.json")
GREEK_SCRIPT = re.compile("[\u0370-\u03ff\u1f00-\u1fff]")

# Wikidata genre labels are long ("ταινία επιστημονικής φαντασίας"), so common
# genres get the short names Greek listings use; anything unmapped is left out.
GENRES = {
    "drama film": "Δράμα", "drama": "Δράμα", "comedy film": "Κωμωδία", "comedy-drama": "Κομεντί",
    "horror film": "Τρόμου", "thriller film": "Θρίλερ", "psychological thriller": "Ψυχολογικό θρίλερ",
    "science fiction film": "Επιστημονικής φαντασίας", "fantasy film": "Φαντασίας",
    "action film": "Δράσης", "adventure film": "Περιπέτεια", "crime film": "Αστυνομική",
    "mystery film": "Μυστηρίου", "romance film": "Αισθηματική", "romantic comedy": "Ρομαντική κωμωδία",
    "war film": "Πολεμική", "western film": "Γουέστερν", "musical film": "Μιούζικαλ",
    "animated film": "Κινούμενα σχέδια", "animated feature film": "Κινούμενα σχέδια",
    "documentary film": "Ντοκιμαντέρ", "biographical film": "Βιογραφική", "historical film": "Ιστορική",
    "family film": "Οικογενειακή", "teen film": "Εφηβική", "coming-of-age film": "Ενηλικίωσης",
    "coming-of-age story": "Ενηλικίωσης", "film noir": "Νουάρ", "neo-noir": "Νέο νουάρ",
    "black comedy film": "Μαύρη κωμωδία", "black comedy": "Μαύρη κωμωδία", "satirical film": "Σάτιρα",
    "slasher film": "Σλάσερ", "disaster film": "Καταστροφής", "sports film": "Αθλητική",
    "erotic film": "Ερωτική", "experimental film": "Πειραματική", "LGBT-related film": "LGBTQ+",
    "Christmas film": "Χριστουγεννιάτικη", "superhero film": "Υπερηρώων",
    "martial arts film": "Πολεμικών τεχνών", "spy film": "Κατασκοπική", "heist film": "Ληστείας",
    "body horror": "Σωματικού τρόμου", "zombie film": "Ζόμπι", "monster film": "Τεράτων",
    "psychological horror film": "Ψυχολογικού τρόμου", "supernatural horror film": "Υπερφυσικού τρόμου",
    "road movie": "Road movie", "mockumentary": "Ψευδοντοκιμαντέρ", "short film": "Μικρού μήκους",
    "psychological drama": "Ψυχολογικό δράμα", "science fiction action film": "Δράσης, επιστ. φαντασίας",
    "political thriller": "Πολιτικό θρίλερ", "legal drama": "Δικαστικό δράμα", "epic film": "Έπος",
    "period drama": "Εποχής", "gangster film": "Γκανγκστερική", "buddy film": "Buddy movie",
    "action thriller": "Θρίλερ δράσης", "crime thriller film": "Αστυνομικό θρίλερ",
}


def clean(name):
    """Drop Wikidata disambiguators: 'Αμαρτωλοί (ταινία)' -> 'Αμαρτωλοί'."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", name or "").strip()


def person(labels, qid):
    lab = labels.get(qid) or {}
    return clean(lab.get("el") or lab.get("en") or "")


def build_data():
    wd = {"films": {}, "detail": {}, "labels": {}}
    if os.path.exists(WIKIDATA):
        with open(WIKIDATA) as f:
            wd = json.load(f)
    films_wd, detail_wd, labels = wd["films"], wd["detail"], wd["labels"]
    # Letterboxd's own pages: genres and the official Greek title, for the top
    # films (letterboxd_filmpages.py). Wikidata comes first; these fill gaps.
    try:
        import letterboxd_filmpages as fp
        pages, lb_genre = fp.load(), fp.GENRES_EL
    except Exception:
        pages, lb_genre = {}, {}
    # The Greek release title, checked against athinorama, filmy.gr and IMDb
    # before falling back to Letterboxd and Wikidata (letterboxd_greek_titles.py).
    try:
        import letterboxd_greek_titles as gt
        official = gt.resolve_all()
    except Exception as e:
        print(f"Greek release titles unavailable ({e}); using Letterboxd and Wikidata only")
        official = {}

    import csv, glob, datetime  # noqa: E401
    raw = {}
    for path in glob.glob(f"{HOME}/friends_watched_*.csv"):
        m = re.fullmatch(r"friends_watched_([0-9]{4}s?)\.csv", os.path.basename(path))
        if m:
            raw[m.group(1)] = (base.load(path), os.path.getmtime(path))

    films, index, periods = [], {}, {}
    for p, (rows, mtime) in raw.items():
        if not rows:
            continue
        stamp = mtime
        if not p.endswith("s"):
            dec = raw.get(p[:3] + "0s")
            if dec:
                subset = {s: c for s, (_, y, c) in dec[0].items() if y == p}
                if subset and subset == {s: c for s, (_, _, c) in rows.items()}:
                    stamp = dec[1]
        packed = []
        for slug, (title, year, count) in sorted(rows.items(), key=lambda kv: -kv[1][2]):
            if slug not in index:
                if slug in official:
                    el = official[slug][0]
                    # A release under the original title (Poor Things) means no Greek one.
                    if not GREEK_SCRIPT.search(el):
                        el = ""
                else:
                    el = clean((films_wd.get(slug) or {}).get("el", ""))
                    # Wikidata's Greek label is often just the English title; a
                    # Greek-script title from Letterboxd beats it.
                    if not GREEK_SCRIPT.search(el) and (pages.get(slug) or {}).get("el"):
                        el = clean(pages[slug]["el"][0])
                index[slug] = len(films)
                films.append([slug, title, year, el if el and el != title else ""])
            elif year and not films[index[slug]][2]:
                films[index[slug]][2] = year
            packed.append([index[slug], count])
        periods[p] = {"asOf": datetime.date.fromtimestamp(stamp).isoformat(), "rows": packed}

    details = {}
    for slug, d in detail_wd.items():
        if slug not in index or not d:
            continue
        genres, seen = [], set()
        for g in d.get("genre", []):
            lab = labels.get(g) or {}
            short = GENRES.get(lab.get("en", ""))
            if short and short not in seen:
                seen.add(short)
                genres.append(short)
        entry = [[n for n in (person(labels, q) for q in d.get("dir", [])) if n],
                 [n for n in (person(labels, q) for q in d.get("cast", [])) if n],
                 genres[:3], d.get("min") or 0, d.get("rt") or "", d.get("elwiki") or ""]
        if any(entry):
            details[index[slug]] = entry

    # Genres from Letterboxd where Wikidata had none — including films that
    # Wikidata knows nothing else about.
    for slug, pg in pages.items():
        if slug not in index or not pg.get("genres"):
            continue
        g = [lb_genre.get(x) for x in pg["genres"] if lb_genre.get(x)][:3]
        if not g:
            continue
        i = index[slug]
        if i in details:
            if not details[i][2]:
                details[i][2] = g
        else:
            details[i] = [[], [], g, 0, "", ""]

    stats = {
        "films": len(films),
        "greek_titles": sum(1 for f in films if f[3]),
        "genres": sum(1 for d in details.values() if d[2]),
        "details": len(details),
    }
    return {"house": base.house_size(), "films": films, "periods": periods, "details": details}, stats


def plots_for(films):
    """{film number: [summary, kind]} from the TMDB cache (letterboxd_tmdb.py)."""
    try:
        import letterboxd_tmdb
    except Exception:
        return {}
    kinds = {"el": 0, "en": 2, "cl": 3}
    index = {f[0]: i for i, f in enumerate(films)}
    return {index[s]: [t, kinds[k]] for s, (t, k) in letterboxd_tmdb.plots().items() if s in index}


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: python3 letterboxd_publish_el.py OUTPUT.html [--standalone]")
    out = sys.argv[1]
    data, stats = build_data()
    data["plots"] = plots_for(data["films"])
    stats["plots"] = len(data["plots"])
    posters, data["posterMin"] = base.attach_posters(data, len(TEMPLATE))
    house = str(data["house"])
    kinds_used = {k for _, k in data["plots"].values()}
    note = ((" Περιλήψεις: TMDB. Αυτό το προϊόν χρησιμοποιεί το TMDB API αλλά δεν έχει την έγκριση"
             " ή την πιστοποίηση του TMDB." if kinds_used & {0, 2} else "")
            + (" Οι σύντομες περιλήψεις με σχετική σήμανση είναι γραμμένες από το Claude."
               if 3 in kinds_used else ""))
    page = (TEMPLATE.replace("__HOUSE__", house).replace("__FOLLOW__", str(base.follow_size()))
                    .replace("__TMDB_NOTE__", note)
                    .replace("__DATA__", base.pack(data))
                    .replace("__POSTERS__", json.dumps({"posters": posters}, separators=(",", ":"))))
    data["posters"] = posters

    if "--standalone" in sys.argv:
        head, body = page.split('<div class="page" lang="el">', 1)
        page = ('<!doctype html>\n<html lang="el">\n<head>\n'
                + head.replace('<meta charset="utf-8">',
                               '<meta charset="utf-8">\n<meta name="robots" content="noindex">')
                + '</head>\n<body>\n<div class="page" lang="el">' + body + '\n</body>\n</html>\n')

    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"wrote {out}: {stats['films']:,} films, {stats['greek_titles']:,} Greek titles, "
          f"details for {stats['details']:,}, genres for {stats['genres']:,}, summaries for {stats['plots']:,}, "
          f"{os.path.getsize(out) / 1e6:.1f} MB")
    print(base.poster_line(data["posters"], data["posterMin"]))


TEMPLATE = r"""<meta charset="utf-8">
<title>__HOUSE__ Θέσεις</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Commissioner:wght@400;500;600&family=Fira+Mono:wght@400;500&family=Sofia+Sans+Extra+Condensed:wght@700;800&display=swap">
<style>
:root{
  color-scheme: light;
  --ground:#eceef1; --surface:#f8f9fb; --ink:#17151d; --muted:#5b5866; --faint:#8b8895;
  --rule:#d5d8df; --accent:#8b1d3b; --accent-soft:rgba(139,29,59,.09);
  --seat:#d2d5dc; --seat-on:#8b1d3b; --track:#dcdfe5; --hit:rgba(139,29,59,.13);
  --display:"Sofia Sans Extra Condensed","Arial Narrow","Helvetica Neue",Arial,sans-serif;
  --body:"Commissioner",-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
  --mono:"Fira Mono",ui-monospace,"SF Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    color-scheme: dark;
    --ground:#131016; --surface:#1b1720; --ink:#ede9f0; --muted:#a8a1b1; --faint:#7a7382;
    --rule:#2d2833; --accent:#e45b79; --accent-soft:rgba(228,91,121,.12);
    --seat:#2c2732; --seat-on:#e45b79; --track:#2a2530; --hit:rgba(228,91,121,.16);
  }
}
:root[data-theme="dark"]{
  color-scheme: dark;
  --ground:#131016; --surface:#1b1720; --ink:#ede9f0; --muted:#a8a1b1; --faint:#7a7382;
  --rule:#2d2833; --accent:#e45b79; --accent-soft:rgba(228,91,121,.12);
  --seat:#2c2732; --seat-on:#e45b79; --track:#2a2530; --hit:rgba(228,91,121,.16);
}

*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--ground);color:var(--ink);font:400 15px/1.55 var(--body)}
a{color:inherit;text-decoration-color:var(--rule);text-underline-offset:3px}
a:hover{color:var(--accent);text-decoration-color:currentColor}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.wrap{max-width:1180px;margin:0 auto;padding:0 28px}
.eyebrow{font:500 11px/1 var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}

.mast .wrap{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;
  gap:10px 28px;padding-block:26px 20px}
.name{margin:0;font:800 50px/.88 var(--display);text-transform:uppercase;letter-spacing:.005em}
.name span{color:var(--accent)}
.tag{margin:0;color:var(--muted);max-width:58ch;text-wrap:pretty}

.now{background:var(--surface);border-block:1px solid var(--rule)}
.now .wrap{display:grid;grid-template-columns:minmax(0,1fr) minmax(260px,340px);
  gap:24px 56px;align-items:center;padding-block:28px 34px}
.pick{display:flex;flex-wrap:wrap;align-items:center;gap:10px 14px;margin-bottom:24px}
.select{position:relative;display:inline-flex}
.select select{appearance:none;-webkit-appearance:none;background:var(--ground);color:var(--ink);
  border:1px solid var(--rule);border-radius:3px;font:600 17px/1.2 var(--body);
  padding:9px 40px 9px 12px;cursor:pointer;font-variant-numeric:tabular-nums;max-width:100%}
.select.small select{font:500 14px/1.2 var(--body);padding:8px 34px 8px 10px;background:var(--surface)}
.select::after{content:"";position:absolute;right:15px;top:50%;width:7px;height:7px;
  border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);
  transform:translateY(-70%) rotate(45deg);pointer-events:none}
.select.small::after{right:13px;width:6px;height:6px}
.asof{color:var(--muted);font:400 12.5px var(--mono)}
.feat-rank{font:800 18px/1 var(--display);letter-spacing:.06em;text-transform:uppercase;color:var(--accent)}
.feat-title{margin:8px 0 4px;font:800 clamp(40px,6vw,80px)/.9 var(--display);
  text-transform:uppercase;text-wrap:balance;overflow-wrap:anywhere}
.feat-title a{text-decoration:none}
.feat-orig{margin:0 0 14px;color:var(--muted);font-size:15px}
.feat-line{margin:0 0 10px;font-size:17px}
.feat-line strong{font:500 17px var(--mono);color:var(--accent)}
.facts{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:4px 14px;margin:0 0 12px;font-size:14px}
.facts dt{color:var(--muted);font:400 12px/1.9 var(--mono)}
.facts dd{margin:0}
.links{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:13.5px}
.links a{color:var(--accent);text-decoration:none;border-bottom:1px solid var(--accent-soft)}
.links a:hover{border-bottom-color:var(--accent)}
.feat-sub{margin:10px 0 0;color:var(--muted);font-size:14px}

.house{margin:0;display:grid;gap:8px;width:100%}
#seats{display:block;width:100%;height:auto}
.seat{fill:var(--seat)}
.seat.on{fill:var(--seat-on)}
.scr{fill:none;stroke:var(--faint);stroke-width:2;stroke-linecap:round}
.scr-label{fill:var(--faint);font:500 8px var(--mono);letter-spacing:.3em}
.animate .seat{transition:fill .3s ease}
.house figcaption{font:400 12px/1.45 var(--mono);color:var(--muted);text-align:center}
.house figcaption b{color:var(--ink);font-weight:500}

.board .wrap{display:grid;grid-template-columns:minmax(0,1fr) 292px;gap:32px 52px;
  align-items:start;padding-block:26px 56px}
.list-head{display:flex;flex-wrap:wrap;align-items:center;gap:10px 12px;margin-bottom:8px}
.field{background:var(--surface);color:var(--ink);border:1px solid var(--rule);border-radius:3px;
  font:400 14px/1.2 var(--body);padding:9px 10px;min-width:0}
.field::placeholder{color:var(--faint)}
.list-head .field{flex:1 1 200px;max-width:280px}
.label-inline{color:var(--muted);font-size:13px}
.count{margin-left:auto;font:400 12.5px var(--mono);color:var(--muted)}
.tablewrap{overflow-x:auto}
table{width:100%;border-collapse:collapse}
th{text-align:left;font:500 11px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted);padding:12px 8px;border-bottom:1.5px solid var(--ink);white-space:nowrap}
th button{all:unset;cursor:pointer}
th button:hover{color:var(--accent)}
th button:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
th[aria-sort="descending"] button::after{content:" \2193";color:var(--accent)}
th[aria-sort="ascending"] button::after{content:" \2191";color:var(--accent)}
th.r,td.r{text-align:right}
td{padding:8px;border-bottom:1px solid var(--rule);vertical-align:baseline}
td.rank{width:58px;padding-right:18px;font:400 13px var(--mono);color:var(--muted)}
td.film{font-size:15px}
.ft a{text-decoration:none}
.ft a:hover{text-decoration:underline}
.ft .orig{color:var(--faint);font-size:13px;margin-left:8px}
.fm{margin-top:2px;color:var(--muted);font-size:12.5px;line-height:1.45}
.info{all:unset;cursor:pointer;color:var(--accent);font:500 12px var(--body);margin-left:6px;white-space:nowrap}
.info:hover{text-decoration:underline}
.info:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
td.yr{width:62px;font:400 13px var(--mono);color:var(--muted)}
td.seats{width:58px;font:500 14px var(--mono)}
td.bar{width:120px;vertical-align:middle}
td.bar span{display:block;height:4px;border-radius:2px;
  background:linear-gradient(var(--accent),var(--accent)) 0 0/var(--w) 100% no-repeat,var(--track)}
tr.top td{padding-block:11px}
tr.top td.rank{font:800 32px/1 var(--display);color:var(--ink)}
tr.top .ft{font-size:17px;font-weight:600}
tr.hit td{background:var(--hit)}
/* posters: the first 100 of each list */
.fw{--pw:34px;--ph:51px}
tr.top .fw{--pw:44px;--ph:66px}
.fw.p{display:flex;align-items:center;gap:12px}
.fw.np{padding-left:calc(var(--pw) + 12px)}
.fw .fi{min-width:0}
.pos{flex:none;display:block;width:var(--pw);height:var(--ph);border-radius:2px;overflow:hidden;
  background:var(--track);box-shadow:0 0 0 1px var(--rule)}
.pos img{display:block;width:100%;height:100%;object-fit:cover}
tr.p td{vertical-align:middle}
tr.drow td{background:var(--surface);padding:12px 8px 16px}
tr.drow .facts{margin-bottom:10px}
.plot{margin:0 0 14px;max-width:68ch;font-size:14.5px;line-height:1.6;text-wrap:pretty}
.pnote{display:block;margin-top:5px;color:var(--faint);font:400 11.5px/1.4 var(--mono)}
.more{display:flex;align-items:center;justify-content:center;gap:14px;padding:22px 0 0}
.btn{background:transparent;color:var(--ink);border:1px solid var(--ink);border-radius:3px;
  font:600 14px/1 var(--body);padding:10px 16px;cursor:pointer}
.btn:hover{background:var(--ink);color:var(--ground)}
.empty{padding:28px 8px;color:var(--muted)}

.side{position:sticky;top:18px;display:grid;gap:26px}
.side section+section{padding-top:24px;border-top:1px solid var(--rule)}
.side h2{margin:0 0 8px;font:800 25px/1 var(--display);text-transform:uppercase}
.side p{margin:0 0 10px;color:var(--muted);font-size:14px;text-wrap:pretty}
.side .field{width:100%}
.results{display:grid;gap:14px;margin-top:14px;max-height:52vh;overflow:auto;padding-right:4px}
.res-t{font-weight:600;line-height:1.3}
.res-t a{text-decoration:none}
.res-t small{display:block;font:400 12px var(--mono);color:var(--muted);margin-top:2px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.chip{background:var(--accent-soft);color:var(--ink);border:1px solid transparent;border-radius:3px;
  font:400 12px/1.2 var(--mono);padding:5px 7px;cursor:pointer;font-variant-numeric:tabular-nums}
.chip b{color:var(--accent);font-weight:500}
.chip:hover{border-color:var(--accent)}
.notes{margin:0;padding-left:16px;color:var(--muted);font-size:13.5px;display:grid;gap:6px}

footer{border-top:1px solid var(--rule)}
footer .wrap{padding-block:18px 30px;font:400 12px/1.6 var(--mono);color:var(--faint)}

@media (max-width:920px){
  .now .wrap{grid-template-columns:1fr}
  .house{max-width:340px}
  .board .wrap{grid-template-columns:1fr}
  .side{position:static;order:-1}
  .results{max-height:none}
}
@media (max-width:560px){
  .wrap{padding:0 18px}
  .name{font-size:42px}
  td.bar,th.bar{display:none}
  td.rank{width:44px;padding-right:10px}
  .count{margin-left:0;width:100%}
  .ft .orig{display:block;margin-left:0}
  .fw{--pw:28px;--ph:42px}
  tr.top .fw{--pw:34px;--ph:51px}
}
@media (prefers-reduced-motion: reduce){ .animate .seat{transition:none} }
</style>

<div class="page" lang="el">
<header class="mast">
  <div class="wrap">
    <h1 class="name"><span>__HOUSE__</span> Θέσεις</h1>
    <p class="tag">Όλες οι ταινίες που έχουν δει __HOUSE__ από τους ανθρώπους που ακολουθώ στο Letterboxd, κατά σειρά του πόσοι τις είδαν — ανά χρονιά και δεκαετία κυκλοφορίας.</p>
  </div>
</header>

<section class="now" aria-labelledby="featTitle">
  <div class="wrap">
    <div>
      <div class="pick">
        <label class="eyebrow" for="pick">Προβάλλεται</label>
        <span class="select"><select id="pick"></select></span>
        <span class="asof" id="asof"></span>
      </div>
      <div class="feat-rank" id="featRank"></div>
      <h2 class="feat-title" id="featTitle"></h2>
      <p class="feat-orig" id="featOrig"></p>
      <p class="feat-line" id="featLine"></p>
      <dl class="facts" id="featFacts"></dl>
      <div class="links" id="featLinks"></div>
      <p class="feat-sub" id="featSub"></p>
    </div>
    <figure class="house">
      <svg id="seats" role="img" aria-labelledby="seatCap"></svg>
      <figcaption id="seatCap"></figcaption>
    </figure>
  </div>
</section>

<div class="board">
  <div class="wrap">
    <main>
      <div class="list-head">
        <input class="field" id="filter" type="search" placeholder="Φιλτράρισμα κατά τίτλο" aria-label="Φιλτράρισμα της λίστας κατά τίτλο">
        <label class="label-inline" for="minSeats">Τουλάχιστον</label>
        <span class="select small">
          <select id="minSeats">
            <option value="1">όλες οι ταινίες</option>
            <option value="2">2 θεατές</option>
            <option value="5">5 θεατές</option>
            <option value="10">10 θεατές</option>
            <option value="25">25 θεατές</option>
            <option value="30">30 θεατές</option>
            <option value="50">50 θεατές</option>
          </select>
        </span>
        <label class="label-inline" for="genre">Είδος</label>
        <span class="select small">
          <select id="genre"><option value="">όλα</option></select>
        </span>
        <span class="count" id="count" aria-live="polite"></span>
      </div>
      <div class="tablewrap">
        <table>
          <thead><tr>
            <th class="r"><button data-k="count" aria-label="Ταξινόμηση κατά κατάταξη">Νο</button></th>
            <th data-sortable="title"><button data-k="title">Ταινία</button></th>
            <th data-sortable="year"><button data-k="year">Έτος</button></th>
            <th class="r" data-sortable="count" aria-sort="descending"><button data-k="count">Θέσεις</button></th>
            <th class="bar" aria-hidden="true"></th>
          </tr></thead>
          <tbody id="tb"></tbody>
        </table>
      </div>
      <div class="empty" id="empty" hidden>Καμία ταινία της λίστας δεν ταιριάζει.</div>
      <div class="more" id="more" hidden>
        <button class="btn" id="loadMore" type="button">Περισσότερες</button>
        <span class="asof" id="moreInfo"></span>
      </div>
    </main>

    <aside class="side">
      <section>
        <h2>Βρες μια ταινία</h2>
        <p>Δες σε ποια θέση βρίσκεται σε κάθε λίστα — με ελληνικό ή πρωτότυπο τίτλο.</p>
        <input class="field" id="find" type="search" placeholder="π.χ. Ο Νονός" aria-label="Αναζήτηση ταινίας">
        <div class="results" id="results"></div>
      </section>
      <section>
        <h2>Τι σημαίνουν οι αριθμοί</h2>
        <ul class="notes">
          <li>Οι θέσεις μιας ταινίας είναι πόσοι από όσους ακολουθώ την έχουν σημειώσει ως ιδωμένη στο Letterboxd. Ο καθένας μετράει μία φορά, όσες φορές κι αν την έχει δει.</li>
          <li>Οι χρονιές και οι δεκαετίες αφορούν την κυκλοφορία της ταινίας.</li>
          <li>Οι θέσεις είναι __HOUSE__: ακολουθώ __FOLLOW__ ανθρώπους και μετράω τους __HOUSE__. Παλαιότερες λίστες μπορεί να διαφέρουν κατά μία-δύο θέσεις.</li>
          <li>Σκηνοθέτες, ηθοποιοί και διάρκειες προέρχονται από τη Wikidata· ελληνικοί τίτλοι και είδη από τη Wikidata και, όπου λείπουν, από το Letterboxd. Όπου δεν υπάρχει ελληνική απόδοση, εμφανίζεται το πρωτότυπο.</li>
        </ul>
      </section>
    </aside>
  </div>
</div>

<footer>
  <div class="wrap">Από δημόσια προφίλ του Letterboxd. Δεν συνδέεται με το Letterboxd ούτε έχει την έγκρισή του. Στοιχεία ταινιών: Wikidata (CC0) και Letterboxd. Αφίσες: Letterboxd. Οι σύνδεσμοι οδηγούν στο Letterboxd, στο Rotten Tomatoes και στη Βικιπαίδεια.__TMDB_NOTE__</div>
</footer>
</div>

<script id="data" type="text/plain">__DATA__</script>
<script id="posters" type="application/json">__POSTERS__</script>
<script>
(async function(){
  // The film data travels gzip-compressed (base64) to stay well inside
  // claude.ai's 16 MB page limit; posters sit beside it, already compact.
  async function gunzip(bytes){
    if (typeof DecompressionStream === "function"){
      const s = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
      return await new Response(s).text();
    }
    await new Promise((ok, no) => {
      const sc = document.createElement("script");
      sc.src = "https://cdnjs.cloudflare.com/ajax/libs/pako/2.1.0/pako.min.js";
      sc.onload = ok; sc.onerror = no;
      document.head.appendChild(sc);
    });
    return window.pako.ungzip(bytes, {to: "string"});
  }
  const DATA = await (async () => {
    const raw = document.getElementById("data").textContent.trim();
    let d;
    if (raw.charAt(0) === "{") d = JSON.parse(raw);
    else {
      const bin = atob(raw), bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      d = JSON.parse(await gunzip(bytes));
    }
    const p = document.getElementById("posters");
    if (p) Object.assign(d, JSON.parse(p.textContent));
    return d;
  })();
  const HOUSE = DATA.house, FILMS = DATA.films, PERIODS = DATA.periods, DETAILS = DATA.details || {};
  const PLOTS = DATA.plots || {};   // film -> [summary, 0 TMDB Greek | 2 English | 3 by Claude]

  /* ---------- posters: embedded in the page, for the top of each list ---------- */
  // claude.ai pages can only show images carried inside the page, so each
  // poster is a small base64 WebP. POSTER_MIN holds, per list, the lowest seat
  // count that gets one — posters always stop at a change of rank.
  const POSTERS = DATA.posters || {}, POSTER_MIN = DATA.posterMin || {};
  let pmin = Infinity;
  function setPosters(p){ pmin = p in POSTER_MIN ? POSTER_MIN[p] : Infinity; }
  function hasPoster(x){ return x.c >= pmin && POSTERS[x.f] !== undefined; }
  function withPoster(x, href, inner){
    if (pmin === Infinity) return inner;
    return hasPoster(x)
      ? `<div class="fw p"><a class="pos" href="${href}" target="_blank" rel="noopener" tabindex="-1" aria-hidden="true">`
        + `<img src="data:image/webp;base64,${POSTERS[x.f]}" alt="" width="70" height="105" decoding="async"></a>`
        + `<div class="fi">${inner}</div></div>`
      : `<div class="fw np">${inner}</div>`;
  }
  const $ = id => document.getElementById(id);
  const fmt = n => n.toLocaleString("el-GR");
  const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  const niceDate = d => new Date(d + "T12:00:00").toLocaleDateString("el-GR", {day:"numeric", month:"short", year:"numeric"});
  const fold = s => s.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/ς/g, "σ");

  const lbUrl = i => `https://letterboxd.com/film/${FILMS[i][0]}/`;
  const shown = i => FILMS[i][3] || FILMS[i][1];
  const original = i => FILMS[i][3] ? FILMS[i][1] : "";
  const periodName = p => p.endsWith("s") ? `Δεκαετία ${p.slice(0, 4)}` : p;
  const periodShort = p => p.endsWith("s") ? `Δεκ. ${p.slice(0, 4)}` : p;

  const keys = Object.keys(PERIODS);
  const years = keys.filter(k => !k.endsWith("s")).sort((a, b) => b - a);
  const decades = keys.filter(k => k.endsWith("s")).sort((a, b) => parseInt(b) - parseInt(a));

  const rankCache = {};
  function ranked(p){
    if (rankCache[p]) return rankCache[p];
    const rows = PERIODS[p].rows, out = new Array(rows.length);
    let rank = 0, prev = -1;
    for (let i = 0; i < rows.length; i++){
      const c = rows[i][1];
      if (c !== prev){ rank = i + 1; prev = c; }
      out[i] = {f: rows[i][0], c, r: rank};
    }
    return (rankCache[p] = out);
  }

  /* film facts: [directors, cast, genres, minutes, rtId, elwikiTitle] */
  function factsHtml(i, castCount){
    const d = DETAILS[i];
    if (!d) return "";
    const [dir, cast, genres, mins] = d;
    let h = "";
    if (dir.length) h += `<dt>Σκηνοθεσία</dt><dd>${esc(dir.join(", "))}</dd>`;
    if (cast.length) h += `<dt>Πρωταγωνιστούν</dt><dd>${esc(cast.slice(0, castCount).join(", "))}</dd>`;
    if (genres.length) h += `<dt>Είδος</dt><dd>${esc(genres.join(" · "))}</dd>`;
    if (mins) h += `<dt>Διάρκεια</dt><dd>${mins} λεπτά</dd>`;
    return h;
  }
  function linksHtml(i){
    const d = DETAILS[i] || [];
    let h = `<a href="${lbUrl(i)}" target="_blank" rel="noopener">Letterboxd</a>`;
    if (d[4]) h += `<a href="https://www.rottentomatoes.com/${encodeURI(d[4])}" target="_blank" rel="noopener">Rotten Tomatoes</a>`;
    if (d[5]) h += `<a href="https://el.wikipedia.org/wiki/${encodeURIComponent(d[5].replace(/ /g, "_"))}" target="_blank" rel="noopener">Βικιπαίδεια</a>`;
    return h;
  }
  function plotHtml(i){
    const p = PLOTS[i];
    if (!p) return "";
    const [text, kind] = p;
    const note = kind === 2 ? "η περίληψη υπάρχει μόνο στα αγγλικά"
               : kind === 3 ? "σύντομη περίληψη γραμμένη από το Claude" : "";
    return `<p class="plot"${kind === 2 ? ' lang="en"' : ""}>${esc(text)}`
      + (note ? `<span class="pnote" lang="el">${note}</span>` : "") + `</p>`;
  }
  function metaLine(i){
    const d = DETAILS[i], parts = [];
    if (d){
      if (d[2].length) parts.push(d[2].slice(0, 2).join(", "));
      if (d[0].length) parts.push(`σκην. ${d[0][0]}`);
      if (d[1].length) parts.push(d[1].slice(0, 3).join(", "));
    }
    if (!PLOTS[i] && !(d && (parts.length || d[2].length || d[3]))) return "";
    return `<div class="fm">${esc(parts.join(" · "))}<button class="info" type="button" aria-expanded="false">λεπτομέρειες</button></div>`;
  }

  /* ---------- picker ---------- */
  const opt = p => `<option value="${p}">${periodName(p)} — ${fmt(PERIODS[p].rows.length)} ταινίες</option>`;
  $("pick").innerHTML =
      (decades.length ? `<optgroup label="Δεκαετίες">${decades.map(opt).join("")}</optgroup>` : "")
    + (years.length ? `<optgroup label="Χρονιές">${years.map(opt).join("")}</optgroup>` : "");

  /* ---------- the house ---------- */
  const COLS = 14, ROWS = Math.ceil(HOUSE / COLS), CELL = 20, AISLE = 14, TOP = 30;
  const W = COLS * CELL + AISLE, H = TOP + ROWS * CELL + 2;
  const svg = $("seats");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  let marks = `<path class="scr" d="M ${CELL * 1.2} 14 Q ${W / 2} 4 ${W - CELL * 1.2} 14"/>`
            + `<text class="scr-label" x="${W / 2}" y="26" text-anchor="middle">ΟΘΟΝΗ</text>`;
  for (let i = 0; i < HOUSE; i++){
    const row = Math.floor(i / COLS), col = i % COLS;
    marks += `<circle class="seat" cx="${col * CELL + (col >= COLS / 2 ? AISLE : 0) + CELL / 2}" cy="${TOP + row * CELL + CELL / 2}" r="6.6"/>`;
  }
  svg.innerHTML = marks;
  const seatEls = [...svg.querySelectorAll(".seat")];
  function fillSeats(n){
    seatEls.forEach((el, i) => {
      const on = i < n;
      if (el.classList.contains("on") !== on){
        el.style.transitionDelay = `${Math.round(i * 2.2)}ms`;
        el.classList.toggle("on", on);
      }
    });
  }

  /* ---------- list ---------- */
  const CHUNK = 400;
  let cur = null, sortKey = "count", sortDir = -1, view = [], rendered = 0;

  function rowHtml(x){
    const i = x.f, year = FILMS[i][2], orig = original(i), p = hasPoster(x);
    return `<tr class="${x.r <= 10 ? "top" : ""}${p ? " p" : ""}" data-f="${i}">`
      + `<td class="rank r">${x.r}</td>`
      + `<td class="film">${withPoster(x, lbUrl(i),
          `<div class="ft"><a href="${lbUrl(i)}" target="_blank" rel="noopener">${esc(shown(i))}</a>`
          + (orig ? `<span class="orig">${esc(orig)}</span>` : "") + `</div>${metaLine(i)}`)}</td>`
      + `<td class="yr">${year || "—"}</td>`
      + `<td class="seats r">${x.c}</td>`
      + `<td class="bar"><span style="--w:${(100 * x.c / HOUSE).toFixed(1)}%"></span></td></tr>`;
  }

  let foldCache = null;
  function searchKey(i){
    if (!foldCache) foldCache = new Array(FILMS.length);
    return foldCache[i] || (foldCache[i] = fold(FILMS[i][1] + " " + (FILMS[i][3] || "")));
  }

  function buildView(){
    const q = fold($("filter").value.trim());
    const min = +$("minSeats").value || 1;
    const genre = $("genre").value;
    const all = ranked(cur);
    let v = all.filter(x => x.c >= min && (!q || searchKey(x.f).includes(q))
                            && (!genre || (DETAILS[x.f] && DETAILS[x.f][2].includes(genre))));
    if (sortKey === "title"){
      v.sort((a, b) => sortDir * shown(a.f).localeCompare(shown(b.f), "el", {sensitivity: "base"}));
    } else if (sortKey === "year"){
      v.sort((a, b) => sortDir * ((+FILMS[a.f][2] || 0) - (+FILMS[b.f][2] || 0)) || a.r - b.r);
    } else if (sortDir === 1){
      v.reverse();
    }
    view = v; rendered = 0;
    $("tb").innerHTML = "";
    $("empty").hidden = v.length > 0;
    $("count").textContent = (v.length === all.length
      ? `${fmt(all.length)} ταινίες` : `${fmt(v.length)} από ${fmt(all.length)} ταινίες`)
      + (genre ? " · μόνο όσες έχουν γνωστό είδος" : "");
    document.querySelectorAll("th[data-sortable]").forEach(th => {
      if (th.dataset.sortable === sortKey) th.setAttribute("aria-sort", sortDir === -1 ? "descending" : "ascending");
      else th.removeAttribute("aria-sort");
    });
    append();
  }

  function append(){
    const next = view.slice(rendered, rendered + CHUNK);
    if (next.length){
      $("tb").insertAdjacentHTML("beforeend", next.map(rowHtml).join(""));
      rendered += next.length;
    }
    const left = view.length - rendered;
    $("more").hidden = left <= 0;
    $("loadMore").textContent = `${fmt(Math.min(CHUNK, left))} ακόμη`;
    $("moreInfo").textContent = `εμφανίζονται ${fmt(rendered)} από ${fmt(view.length)}`;
  }

  $("tb").addEventListener("click", e => {
    const btn = e.target.closest(".info");
    if (!btn) return;
    const tr = btn.closest("tr"), i = +tr.dataset.f;
    const open = btn.getAttribute("aria-expanded") === "true";
    if (open){
      if (tr.nextElementSibling && tr.nextElementSibling.classList.contains("drow")) tr.nextElementSibling.remove();
      btn.setAttribute("aria-expanded", "false");
      btn.textContent = "λεπτομέρειες";
    } else {
      tr.insertAdjacentHTML("afterend",
        `<tr class="drow"><td></td><td colspan="4">${plotHtml(i)}<dl class="facts">${factsHtml(i, 6)}</dl>`
        + `<div class="links">${linksHtml(i)}</div></td></tr>`);
      btn.setAttribute("aria-expanded", "true");
      btn.textContent = "λιγότερα";
    }
  });

  // The genres on offer are the ones this list actually has, most common first.
  function fillGenres(p){
    const counts = new Map();
    for (const x of ranked(p)) for (const g of (DETAILS[x.f] || [, , []])[2]) counts.set(g, (counts.get(g) || 0) + 1);
    const keep = $("genre").value;
    $("genre").innerHTML = '<option value="">όλα</option>'
      + [...counts].sort((a, b) => b[1] - a[1])
          .map(([g, n]) => `<option value="${esc(g)}">${esc(g)} (${fmt(n)})</option>`).join("");
    $("genre").value = counts.has(keep) ? keep : "";
  }

  function setPeriod(p){
    cur = p;
    setPosters(p);
    fillGenres(p);
    const all = ranked(p), P = PERIODS[p];
    $("pick").value = p;
    $("asof").textContent = `ενημέρωση ${niceDate(P.asOf)}`;
    const top = all[0], i = top.f;
    const tied = all.filter(x => x.r === 1).length;
    $("featRank").textContent = tied > 1 ? "Νο 1 (ισοβαθμία)" : `Νο 1 · ${periodName(p)}`;
    $("featTitle").innerHTML = `<a href="${lbUrl(i)}" target="_blank" rel="noopener">${esc(shown(i))}</a>`;
    const orig = original(i), year = FILMS[i][2];
    $("featOrig").textContent = [orig, year].filter(Boolean).join(" · ");
    $("featLine").innerHTML = `Την έχουν δει <strong>${top.c}</strong> από τους ${HOUSE} που ακολουθώ`;
    $("featFacts").innerHTML = factsHtml(i, 4);
    $("featLinks").innerHTML = linksHtml(i);
    const runner = all.find(x => x.r > 1);
    $("featSub").textContent = tied > 1
      ? `Ισοβαθμεί με ${tied - 1} ${tied > 2 ? "ακόμη ταινίες" : "ακόμη ταινία"} στην κορυφή.`
      : runner ? `Δεύτερη: ${shown(runner.f)}, με ${runner.c}.` : "";
    fillSeats(top.c);
    $("seatCap").innerHTML = `Κάθε θέση είναι ένας άνθρωπος που ακολουθώ · <b>${top.c}</b> από ${HOUSE} κατειλημμένες`;
    buildView();
  }

  $("pick").addEventListener("change", e => setPeriod(e.target.value));
  $("filter").addEventListener("input", buildView);
  $("minSeats").addEventListener("change", buildView);
  $("genre").addEventListener("change", buildView);
  $("loadMore").addEventListener("click", append);
  window.addEventListener("scroll", () => {
    if (rendered < view.length &&
        window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 1600) append();
  }, {passive: true});
  document.querySelectorAll("th button[data-k]").forEach(b => b.addEventListener("click", () => {
    const k = b.dataset.k;
    if (sortKey === k) sortDir = -sortDir;
    else { sortKey = k; sortDir = k === "title" ? 1 : -1; }
    buildView();
  }));

  /* ---------- find a film ---------- */
  let appear = null;
  function appearances(){
    if (appear) return appear;
    appear = new Map();
    for (const p of keys){
      const total = PERIODS[p].rows.length;
      for (const x of ranked(p)){
        let a = appear.get(x.f);
        if (!a) appear.set(x.f, a = []);
        a.push({p, r: x.r, c: x.c, total});
      }
    }
    return appear;
  }

  function find(){
    const raw = $("find").value.trim(), q = fold(raw), box = $("results");
    if (q.length < 2){ box.innerHTML = ""; return; }
    const ap = appearances(), hits = [];
    for (let i = 0; i < FILMS.length && hits.length < 500; i++){
      if (ap.has(i) && searchKey(i).includes(q)) hits.push(i);
    }
    if (!hits.length){
      box.innerHTML = `<p>Καμία ταινία με «${esc(raw)}» στον τίτλο δεν υπάρχει σε αυτές τις λίστες.</p>`;
      return;
    }
    const best = i => Math.max(...ap.get(i).map(a => a.c));
    hits.sort((a, b) => best(b) - best(a));
    box.innerHTML = hits.slice(0, 12).map(i => {
      const year = FILMS[i][2], orig = original(i);
      const chips = ap.get(i)
        .sort((a, b) => a.p.length - b.p.length || b.p.localeCompare(a.p))
        .map(a => `<button class="chip" type="button" data-p="${a.p}" data-f="${i}">`
                + `${periodShort(a.p)} <b>Νο ${a.r}</b> από ${fmt(a.total)}</button>`).join("");
      return `<div><div class="res-t"><a href="${lbUrl(i)}" target="_blank" rel="noopener">${esc(shown(i))}</a>`
           + `<small>${[orig, year, best(i) + " θέσεις"].filter(Boolean).map(esc).join(" · ")}</small></div>`
           + `<div class="chips">${chips}</div></div>`;
    }).join("");
    box.querySelectorAll(".chip").forEach(c => c.addEventListener("click", () => jump(c.dataset.p, +c.dataset.f)));
  }

  function jump(p, f){
    $("filter").value = ""; $("minSeats").value = "1"; $("genre").value = "";
    sortKey = "count"; sortDir = -1;
    setPeriod(p);
    const idx = view.findIndex(x => x.f === f);
    if (idx < 0) return;
    while (rendered <= idx) append();
    const tr = $("tb").querySelector(`tr[data-f="${f}"]`);
    if (!tr) return;
    const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
    tr.scrollIntoView({block: "center", behavior: reduce ? "auto" : "smooth"});
    tr.classList.add("hit");
    setTimeout(() => tr.classList.remove("hit"), 2600);
  }
  $("find").addEventListener("input", find);

  const thisYear = String(new Date().getFullYear());
  setPeriod(PERIODS[thisYear] ? thisYear : (years[0] || decades[0]));
  requestAnimationFrame(() => requestAnimationFrame(() => svg.classList.add("animate")));
})();
</script>
"""

if __name__ == "__main__":
    main()
