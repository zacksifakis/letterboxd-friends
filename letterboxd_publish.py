"""
Build the shareable "Seats" page from the scraped CSVs.

    python3 letterboxd_publish.py OUTPUT.html               # for claude.ai
    python3 letterboxd_publish.py index.html --standalone   # for any web host

Only aggregate counts go into the page (how many of the people you follow
logged each film) — never which person watched what, and no usernames.
"""

import os
import re
import csv
import sys
import json
import glob
import gzip
import base64
import datetime

HOME = os.path.expanduser("~")


def load(path):
    rows = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            slug = r.get("Slug")
            if not slug:
                continue
            rows[slug] = (r.get("Film Title") or slug,
                          (r.get("Year") or "").strip(),
                          int(r.get("Friend Watch Count") or 0))
    return rows


def house_size():
    """Seats: people followed in the most recent scrape, minus anyone left out."""
    try:
        import letterboxd_people
        n = letterboxd_people.counted()
        if n:
            return n
    except Exception:
        pass
    audits = sorted(glob.glob(f"{HOME}/friends_watched_*_per_friend.json"), key=os.path.getmtime)
    if audits:
        try:
            with open(audits[-1]) as f:
                return len(json.load(f))
        except Exception:
            pass
    return 168


def follow_size():
    try:
        import letterboxd_people
        return len(letterboxd_people.following()) or house_size()
    except Exception:
        return house_size()


PAGE_LIMIT = 16_000_000        # claude.ai caps a page at 16 MiB (16.78 MB); keep a margin


def pack(obj):
    """The page's data as base64 gzip — the page unpacks it in the browser."""
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.b64encode(gzip.compress(raw, 9, mtime=0)).decode("ascii")


def attach_posters(data, template_len):
    """
    Posters to embed: ({film number: base64 WebP}, {period: lowest seat count
    with a poster}). If the page would outgrow claude.ai's limit, years get
    fewer posters (40, 30, 20, 15, 10, 5, then none) — decades and this year keep
    their top 100.
    """
    try:
        import letterboxd_posters as lp
    except Exception:
        return {}, {}
    films = data["films"]
    index = {f[0]: i for i, f in enumerate(films)}
    lists = {p: [(films[i][0], c) for i, c in v["rows"]] for p, v in data["periods"].items()}
    size = template_len + len(pack(data))
    for year_top in (lp.YEAR_TOP, 30, 20, 15, 10, 5, 0):
        posters, floor = lp.embed(lists, index, year_top) if year_top else lp.embed(
            {p: v for p, v in lists.items() if lp.depth(p) == lp.TOP}, index, 0)
        extra = sum(len(b) + 12 for b in posters.values()) + 20 * len(floor)
        if size + extra <= PAGE_LIMIT:
            return posters, floor
    return {}, {}


def poster_line(posters, poster_min):
    kb = sum(len(b) for b in posters.values()) / 1e6
    return f"posters: {len(posters):,} embedded ({kb:.1f} MB) across {len(poster_min)} lists"


PLOTS_EN = os.path.join(HOME, ".letterboxd_cache", "plots_en_claude.json")


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _genre_name(label):
    """'science fiction film' → 'Science fiction' (Wikidata's genre labels)."""
    label = re.sub(r"\s+film$", "", (label or "").strip())
    return label[:1].upper() + label[1:] if label else ""


def film_details(index):
    """
    {film number: [directors, cast, genres, minutes]} and {film number: summary}
    for the English page's details panel: Wikidata first, Letterboxd's film
    pages where it has nothing (usual for new releases), and short summaries
    Claude wrote (plots_en_claude.json).
    """
    wd = _load_json(os.path.join(HOME, ".letterboxd_cache", "wikidata_el.json"), {})
    detail, labels = wd.get("detail", {}), wd.get("labels", {})
    pages = _load_json(os.path.join(HOME, ".letterboxd_cache", "filmpages.json"), {})
    facts = _load_json(os.path.join(HOME, ".letterboxd_cache", "plot_facts.json"), {})
    en = lambda q: re.sub(r"\s*\([^)]*\)\s*$", "", (labels.get(q) or {}).get("en", "")).strip()
    out = {}
    for slug, i in index.items():
        d, pg, fa = detail.get(slug) or {}, pages.get(slug) or {}, facts.get(slug) or {}
        dirs = [n for n in map(en, d.get("dir", [])) if n] or pg.get("dir") or \
            [x for x in fa.get("director", []) if x.strip() and "*/" not in x]
        cast = [n for n in map(en, d.get("cast", [])) if n] or pg.get("cast") or \
            [x for x in fa.get("cast", []) if x.strip() and "*/" not in x]
        genres = pg.get("genres") or [g for g in (_genre_name(en(q)) for q in d.get("genre", [])) if g]
        mins = d.get("min") or pg.get("min") or 0
        if dirs or cast or genres or mins:
            out[i] = [dirs[:3], cast[:6], list(dict.fromkeys(genres))[:3], mins]
    plots = {index[s]: t for s, t in _load_json(PLOTS_EN, {}).items() if s in index and t}
    return out, plots


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: python3 letterboxd_publish.py OUTPUT.html")
    out = sys.argv[1]

    raw = {}
    for path in glob.glob(f"{HOME}/friends_watched_*.csv"):
        m = re.fullmatch(r"friends_watched_([0-9]{4}s?)\.csv", os.path.basename(path))
        if m:
            raw[m.group(1)] = (load(path), os.path.getmtime(path))

    films, index = [], {}
    periods = {}
    for p, (rows, mtime) in raw.items():
        if not rows:
            continue
        stamp = mtime
        # A year written out of a decade scrape carries the decade's date, not
        # the moment it was split off.
        if not p.endswith("s"):
            dec = raw.get(p[:3] + "0s")
            if dec:
                subset = {s: c for s, (_, y, c) in dec[0].items() if y == p}
                mine = {s: c for s, (_, _, c) in rows.items()}
                if subset and subset == mine:
                    stamp = dec[1]

        ordered = sorted(rows.items(), key=lambda kv: -kv[1][2])
        packed = []
        for slug, (title, year, count) in ordered:
            if slug not in index:
                index[slug] = len(films)
                films.append([slug, title, year])
            elif year and not films[index[slug]][2]:
                films[index[slug]][2] = year
            packed.append([index[slug], count])
        periods[p] = {"asOf": datetime.date.fromtimestamp(stamp).isoformat(), "rows": packed}

    house = house_size()
    details, plots = film_details(index)
    base = {"house": house, "films": films, "periods": periods, "details": details, "plots": plots}
    posters, poster_min = attach_posters(base, len(TEMPLATE))
    page = (TEMPLATE.replace("__HOUSE__", str(house)).replace("__FOLLOW__", str(follow_size()))
                    .replace("__DATA__", pack(dict(base, posterMin=poster_min)))
                    .replace("__POSTERS__", json.dumps({"posters": posters}, separators=(",", ":"))))

    if "--standalone" in sys.argv:
        # Hosted anywhere other than claude.ai there's no wrapper document, so
        # supply one. noindex keeps it out of search results: it's meant for
        # people you send the link to, not for strangers to stumble on.
        head, body = page.split('<header class="mast">', 1)
        page = ('<!doctype html>\n<html lang="en">\n<head>\n'
                + head.replace('<meta charset="utf-8">',
                               '<meta charset="utf-8">\n<meta name="robots" content="noindex">')
                + '</head>\n<body>\n<header class="mast">' + body + '\n</body>\n</html>\n')
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)

    n_rows = sum(len(v["rows"]) for v in periods.values())
    print(f"wrote {out}: {len(periods)} lists, {n_rows:,} rows, {len(films):,} films, "
          f"house of {house}, details for {len(details):,}, summaries for {len(plots):,}, "
          f"{os.path.getsize(out) / 1e6:.1f} MB")
    print(poster_line(posters, poster_min))


TEMPLATE = r"""<meta charset="utf-8">
<title>__HOUSE__ Seats</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@700;800&family=IBM+Plex+Mono:wght@400;500&family=Schibsted+Grotesk:wght@400;500;600&display=swap">
<style>
:root{
  color-scheme: light;
  --ground:#eceef1; --surface:#f8f9fb; --ink:#17151d; --muted:#5b5866; --faint:#8b8895;
  --rule:#d5d8df; --accent:#8b1d3b; --accent-soft:rgba(139,29,59,.09);
  --seat:#d2d5dc; --seat-on:#8b1d3b; --track:#dcdfe5; --hit:rgba(139,29,59,.13);
  --display:"Big Shoulders Display","Arial Narrow","Helvetica Neue",Arial,sans-serif;
  --body:"Schibsted Grotesk",-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,"SF Mono",Menlo,Consolas,monospace;
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
body{margin:0;background:var(--ground);color:var(--ink);font:400 15px/1.5 var(--body)}
a{color:inherit;text-decoration-color:var(--rule);text-underline-offset:3px}
a:hover{color:var(--accent);text-decoration-color:currentColor}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.wrap{max-width:1180px;margin:0 auto;padding:0 28px}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
.eyebrow{font:500 11px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}

/* masthead */
.mast .wrap{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;
  gap:10px 28px;padding-block:26px 20px}
.name{margin:0;font:800 46px/.9 var(--display);text-transform:uppercase;letter-spacing:.01em}
.name span{color:var(--accent)}
.tag{margin:0;color:var(--muted);max-width:56ch;text-wrap:pretty}

/* now showing */
.now{background:var(--surface);border-block:1px solid var(--rule)}
.now .wrap{display:grid;grid-template-columns:minmax(0,1fr) minmax(260px,340px);
  gap:24px 56px;align-items:center;padding-block:28px 34px}
.pick{display:flex;flex-wrap:wrap;align-items:center;gap:10px 14px;margin-bottom:26px}
.select{position:relative;display:inline-flex}
.select select{appearance:none;-webkit-appearance:none;background:var(--ground);color:var(--ink);
  border:1px solid var(--rule);border-radius:3px;font:600 17px/1.2 var(--body);
  padding:9px 40px 9px 12px;cursor:pointer;font-variant-numeric:tabular-nums;max-width:100%}
.select.small select{font:500 14px/1.2 var(--body);padding:8px 34px 8px 10px;background:var(--surface)}
.select::after{content:"";position:absolute;right:15px;top:50%;width:7px;height:7px;
  border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);
  transform:translateY(-70%) rotate(45deg);pointer-events:none}
.select.small::after{right:13px;width:6px;height:6px}
optgroup{font-weight:600}
.asof{color:var(--muted);font-size:12.5px}
.feat-rank{font:800 17px/1 var(--display);letter-spacing:.1em;text-transform:uppercase;color:var(--accent)}
.feat-title{margin:10px 0 16px;font:800 clamp(38px,5.6vw,74px)/.92 var(--display);
  text-transform:uppercase;letter-spacing:.005em;text-wrap:balance;overflow-wrap:anywhere}
.feat-title a{text-decoration:none}
.feat-line{margin:0 0 6px;font-size:17px}
.feat-line strong{font:500 17px var(--mono);color:var(--accent)}
.feat-sub{margin:0;color:var(--muted);font-size:14px}

.house{margin:0;display:grid;gap:8px;width:100%}
#seats{display:block;width:100%;height:auto}
.seat{fill:var(--seat)}
.seat.on{fill:var(--seat-on)}
.scr{fill:none;stroke:var(--faint);stroke-width:2;stroke-linecap:round}
.scr-label{fill:var(--faint);font:500 8px var(--mono);letter-spacing:.3em}
.animate .seat{transition:fill .3s ease}
.house figcaption{font:400 12px/1.45 var(--mono);color:var(--muted);text-align:center}
.house figcaption b{color:var(--ink);font-weight:500}

/* board */
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
th{text-align:left;font:500 11px/1 var(--mono);letter-spacing:.12em;text-transform:uppercase;
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
td.film a{text-decoration:none}
td.film a:hover{text-decoration:underline}
td.yr{width:62px;font:400 13px var(--mono);color:var(--muted)}
td.seats{width:58px;font:500 14px var(--mono)}
td.bar{width:132px;vertical-align:middle}
td.bar span{display:block;height:4px;border-radius:2px;
  background:linear-gradient(var(--accent),var(--accent)) 0 0/var(--w) 100% no-repeat,var(--track)}
tr.top td{padding-block:11px}
tr.top td.rank{font:800 30px/1 var(--display);color:var(--ink)}
tr.top td.film{font-size:17px;font-weight:600}
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
.more{display:flex;align-items:center;justify-content:center;gap:14px;padding:22px 0 0}
.btn{background:transparent;color:var(--ink);border:1px solid var(--ink);border-radius:3px;
  font:600 14px/1 var(--body);padding:10px 16px;cursor:pointer}
.btn:hover{background:var(--ink);color:var(--ground)}
.empty{padding:28px 8px;color:var(--muted)}

/* side rail */
.side{position:sticky;top:18px;display:grid;gap:26px}
.side section+section{padding-top:24px;border-top:1px solid var(--rule)}
.side h2{margin:0 0 8px;font:800 23px/1 var(--display);text-transform:uppercase;letter-spacing:.02em}
.side p{margin:0 0 10px;color:var(--muted);font-size:14px;text-wrap:pretty}
.side .field{width:100%}
.results{display:grid;gap:14px;margin-top:14px;max-height:52vh;overflow:auto;padding-right:4px}
.res-t{font-weight:600;line-height:1.3}
.res-t a{text-decoration:none}
.res-t small{font:400 12px var(--mono);color:var(--muted);margin-left:4px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.chip{background:var(--accent-soft);color:var(--ink);border:1px solid transparent;border-radius:3px;
  font:400 12px/1.2 var(--mono);padding:5px 7px;cursor:pointer;font-variant-numeric:tabular-nums}
.chip b{color:var(--accent);font-weight:500}
.chip:hover{border-color:var(--accent)}
.notes{margin:0;padding-left:16px;color:var(--muted);font-size:13.5px;display:grid;gap:6px}

.fm{margin-top:2px;color:var(--muted);font-size:12.5px;line-height:1.45;font-weight:400}
.info{all:unset;cursor:pointer;color:var(--accent);font:500 12px var(--body);margin-left:6px;white-space:nowrap}
.info:hover{text-decoration:underline}
.info:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
tr.drow td{background:var(--surface);padding:12px 8px 16px}
.plot{margin:0 0 12px;max-width:68ch;font-size:14.5px;line-height:1.6;text-wrap:pretty}
.pnote{display:block;margin-top:5px;color:var(--faint);font:400 11.5px/1.4 var(--mono)}
.facts{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:4px 14px;margin:0 0 10px;font-size:14px}
.facts dt{color:var(--muted);font:400 12px/1.9 var(--mono)}
.facts dd{margin:0}
.links a{color:var(--accent);font-size:13.5px}
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
  .name{font-size:38px}
  td.bar,th.bar{display:none}
  td.rank{width:44px;padding-right:10px}
  .count{margin-left:0;width:100%}
  .fw{--pw:28px;--ph:42px}
  tr.top .fw{--pw:34px;--ph:51px}
}
@media (prefers-reduced-motion: reduce){
  .animate .seat{transition:none}
}
</style>

<header class="mast">
  <div class="wrap">
    <h1 class="name"><span>__HOUSE__</span> Seats</h1>
    <p class="tag">Every film __HOUSE__ of the people I follow on Letterboxd have logged, ranked by how many of them watched it — by release year and decade.</p>
  </div>
</header>

<section class="now" aria-labelledby="featTitle">
  <div class="wrap">
    <div>
      <div class="pick">
        <label class="eyebrow" for="pick">Now showing</label>
        <span class="select"><select id="pick"></select></span>
        <span class="asof mono" id="asof"></span>
      </div>
      <div class="feat-rank" id="featRank">No. 1</div>
      <h2 class="feat-title" id="featTitle"></h2>
      <p class="feat-line" id="featLine"></p>
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
        <input class="field" id="filter" type="search" placeholder="Filter this list by title" aria-label="Filter this list by title">
        <label class="label-inline" for="minSeats">Watched by at least</label>
        <span class="select small">
          <select id="minSeats">
            <option value="1">anyone</option>
            <option value="2">2 people</option>
            <option value="5">5 people</option>
            <option value="10">10 people</option>
            <option value="25">25 people</option>
            <option value="30">30 people</option>
            <option value="50">50 people</option>
          </select>
        </span>
        <span class="count" id="count" aria-live="polite"></span>
      </div>
      <div class="tablewrap">
        <table>
          <thead><tr>
            <th class="r"><button data-k="count" aria-label="Sort by rank">No.</button></th>
            <th data-sortable="title"><button data-k="title">Film</button></th>
            <th data-sortable="year"><button data-k="year">Year</button></th>
            <th class="r" data-sortable="count" aria-sort="descending"><button data-k="count">Seats</button></th>
            <th class="bar" aria-hidden="true"></th>
          </tr></thead>
          <tbody id="tb"></tbody>
        </table>
      </div>
      <div class="empty" id="empty" hidden>No films in this list match that.</div>
      <div class="more" id="more" hidden>
        <button class="btn" id="loadMore" type="button">Show more</button>
        <span class="mono asof" id="moreInfo"></span>
      </div>
    </main>

    <aside class="side">
      <section>
        <h2>Find a film</h2>
        <p>See where a film ranks in every list it appears in.</p>
        <input class="field" id="find" type="search" placeholder="e.g. Interstellar" aria-label="Find a film by title">
        <div class="results" id="results"></div>
      </section>
      <section>
        <h2>About the numbers</h2>
        <ul class="notes">
          <li>A film's seats are how many of the people I follow have it marked as watched on Letterboxd. Each person counts once, however often they've seen it.</li>
          <li>Years and decades go by release date.</li>
          <li>There are __HOUSE__ seats: I follow __FOLLOW__ people and count __HOUSE__ of them. Ratings and likes come from the same __HOUSE__. Lists compiled earlier may be off by a seat or two.</li>
          <li>Each list shows the date it was compiled.</li>
        </ul>
      </section>
    </aside>
  </div>
</div>

<footer>
  <div class="wrap">Compiled from public Letterboxd profiles. Not affiliated with or endorsed by Letterboxd. Film links and posters: letterboxd.com. Film details: Wikidata (CC0) and Letterboxd. Short summaries marked as such are written by Claude.</div>
</footer>

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
  const HOUSE = DATA.house, FILMS = DATA.films, PERIODS = DATA.periods;

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
  const fmt = n => n.toLocaleString("en-US");
  const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  const filmUrl = i => `https://letterboxd.com/film/${FILMS[i][0]}/`;
  const niceDate = d => new Date(d + "T12:00:00").toLocaleDateString("en-US", {month:"short", day:"numeric", year:"numeric"});

  const keys = Object.keys(PERIODS);
  const years = keys.filter(k => !k.endsWith("s")).sort((a, b) => b - a);
  const decades = keys.filter(k => k.endsWith("s")).sort((a, b) => parseInt(b) - parseInt(a));

  // Competition ranking: films on the same count share the better rank.
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

  /* ---------- period picker ---------- */
  const opt = p => `<option value="${p}">${p} — ${fmt(PERIODS[p].rows.length)} films</option>`;
  $("pick").innerHTML =
      (decades.length ? `<optgroup label="Decades">${decades.map(opt).join("")}</optgroup>` : "")
    + (years.length ? `<optgroup label="Years">${years.map(opt).join("")}</optgroup>` : "");

  /* ---------- the house ---------- */
  const COLS = 14, ROWS = Math.ceil(HOUSE / COLS), CELL = 20, AISLE = 14, TOP = 30;
  const W = COLS * CELL + AISLE, H = TOP + ROWS * CELL + 2;
  const svg = $("seats");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  let marks = `<path class="scr" d="M ${CELL * 1.2} 14 Q ${W / 2} 4 ${W - CELL * 1.2} 14"/>`
            + `<text class="scr-label" x="${W / 2}" y="26" text-anchor="middle">SCREEN</text>`;
  for (let i = 0; i < HOUSE; i++){
    const row = Math.floor(i / COLS), col = i % COLS;
    const cx = col * CELL + (col >= COLS / 2 ? AISLE : 0) + CELL / 2;
    const cy = TOP + row * CELL + CELL / 2;
    marks += `<circle class="seat" cx="${cx}" cy="${cy}" r="6.6"/>`;
  }
  svg.innerHTML = marks;
  const seatEls = [...svg.querySelectorAll(".seat")];

  function fillSeats(n){
    for (let i = 0; i < seatEls.length; i++){
      const el = seatEls[i], on = i < n;
      if (el.classList.contains("on") !== on){
        el.style.transitionDelay = `${Math.round(i * 2.2)}ms`;
        el.classList.toggle("on", on);
      }
    }
  }

  /* ---------- list ---------- */
  const CHUNK = 400;
  let cur = null, sortKey = "count", sortDir = -1, view = [], rendered = 0;

  /* ---------- details: plot, director, cast, genres, running time ---------- */
  const DETAILS = DATA.details || {}, PLOTS = DATA.plots || {};
  function metaLine(i){
    const d = DETAILS[i], parts = [];
    if (d){
      if (d[2].length) parts.push(d[2].slice(0, 2).join(", "));
      if (d[0].length) parts.push(`dir. ${d[0][0]}`);
      if (d[1].length) parts.push(d[1].slice(0, 3).join(", "));
    }
    if (!PLOTS[i] && !parts.length && !(d && d[3])) return "";
    return `<div class="fm">${esc(parts.join(" · "))}<button class="info" type="button" aria-expanded="false">more</button></div>`;
  }
  function detailsHtml(i){
    const d = DETAILS[i] || [[], [], [], 0], [dir, cast, genres, mins] = d;
    let facts = "";
    if (dir.length) facts += `<dt>Director</dt><dd>${esc(dir.join(", "))}</dd>`;
    if (cast.length) facts += `<dt>Cast</dt><dd>${esc(cast.join(", "))}</dd>`;
    if (genres.length) facts += `<dt>Genre</dt><dd>${esc(genres.join(" · "))}</dd>`;
    if (mins) facts += `<dt>Running time</dt><dd>${mins} min</dd>`;
    return (PLOTS[i] ? `<p class="plot">${esc(PLOTS[i])}<span class="pnote">short summary written by Claude</span></p>` : "")
      + (facts ? `<dl class="facts">${facts}</dl>` : "")
      + `<div class="links"><a href="${filmUrl(i)}" target="_blank" rel="noopener">Letterboxd</a></div>`;
  }

  function rowHtml(x){
    const [, title, year] = FILMS[x.f], p = hasPoster(x);
    return `<tr class="${x.r <= 10 ? "top" : ""}${p ? " p" : ""}" data-f="${x.f}">`
      + `<td class="rank r">${x.r}</td>`
      + `<td class="film">${withPoster(x, filmUrl(x.f),
          `<a href="${filmUrl(x.f)}" target="_blank" rel="noopener">${esc(title)}</a>${metaLine(x.f)}`)}</td>`
      + `<td class="yr">${year || "—"}</td>`
      + `<td class="seats r">${x.c}</td>`
      + `<td class="bar"><span style="--w:${(100 * x.c / HOUSE).toFixed(1)}%"></span></td></tr>`;
  }

  function buildView(){
    const q = $("filter").value.trim().toLowerCase();
    const min = +$("minSeats").value || 1;
    const all = ranked(cur);
    let v = all.filter(x => x.c >= min && (!q || FILMS[x.f][1].toLowerCase().includes(q)));
    if (sortKey === "title"){
      v.sort((a, b) => sortDir * FILMS[a.f][1].localeCompare(FILMS[b.f][1], "en", {sensitivity: "base"}));
    } else if (sortKey === "year"){
      v.sort((a, b) => sortDir * ((+FILMS[a.f][2] || 0) - (+FILMS[b.f][2] || 0)) || a.r - b.r);
    } else if (sortDir === 1){
      v.reverse();
    }
    view = v; rendered = 0;
    $("tb").innerHTML = "";
    $("empty").hidden = v.length > 0;
    $("count").textContent = v.length === all.length
      ? `${fmt(all.length)} films`
      : `${fmt(v.length)} of ${fmt(all.length)} films`;
    // "No." and "Seats" both sort by count; only Seats carries the arrow.
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
    $("loadMore").textContent = `Show ${fmt(Math.min(CHUNK, left))} more`;
    $("moreInfo").textContent = `${fmt(rendered)} of ${fmt(view.length)} shown`;
  }

  function setPeriod(p){
    cur = p;
    setPosters(p);
    const all = ranked(p), P = PERIODS[p];
    $("pick").value = p;
    $("asof").textContent = `compiled ${niceDate(P.asOf)}`;
    const top = all[0], [, title, year] = FILMS[top.f];
    const tied = all.filter(x => x.r === 1).length;
    $("featRank").textContent = tied > 1 ? `No. 1 (tied)` : `No. 1 · ${p}`;
    $("featTitle").innerHTML = `<a href="${filmUrl(top.f)}" target="_blank" rel="noopener">${esc(title)}</a>`;
    $("featLine").innerHTML = `Watched by <strong>${top.c}</strong> of the ${HOUSE} people I follow`
      + (year && p.endsWith("s") ? ` · released ${year}` : "");
    const runner = all.find(x => x.r > 1);
    $("featSub").textContent = tied > 1
      ? `Level with ${tied - 1} other film${tied > 2 ? "s" : ""} at the top.`
      : runner ? `Runner-up: ${FILMS[runner.f][1]}, with ${runner.c}.` : "";
    fillSeats(top.c);
    $("seatCap").innerHTML = `Each seat is one person I follow · <b>${top.c}</b> of ${HOUSE} filled`;
    buildView();
  }

  $("pick").addEventListener("change", e => setPeriod(e.target.value));
  $("filter").addEventListener("input", buildView);
  $("minSeats").addEventListener("change", buildView);
  $("loadMore").addEventListener("click", append);
  $("tb").addEventListener("click", e => {
    const btn = e.target.closest(".info");
    if (!btn) return;
    const tr = btn.closest("tr"), i = +tr.dataset.f, open = btn.getAttribute("aria-expanded") === "true";
    if (open){
      if (tr.nextElementSibling && tr.nextElementSibling.classList.contains("drow")) tr.nextElementSibling.remove();
    } else {
      tr.insertAdjacentHTML("afterend", `<tr class="drow"><td></td><td colspan="4">${detailsHtml(i)}</td></tr>`);
    }
    btn.setAttribute("aria-expanded", String(!open));
    btn.textContent = open ? "more" : "less";
  });
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
    const q = $("find").value.trim().toLowerCase();
    const box = $("results");
    if (q.length < 2){ box.innerHTML = ""; return; }
    const ap = appearances(), hits = [];
    for (let i = 0; i < FILMS.length && hits.length < 500; i++){
      if (FILMS[i][1].toLowerCase().includes(q) && ap.has(i)) hits.push(i);
    }
    if (!hits.length){
      box.innerHTML = `<p>No film with “${esc($("find").value.trim())}” in its title appears in these lists.</p>`;
      return;
    }
    const best = i => Math.max(...ap.get(i).map(a => a.c));
    hits.sort((a, b) => best(b) - best(a));
    box.innerHTML = hits.slice(0, 12).map(i => {
      const [, title, year] = FILMS[i];
      const chips = ap.get(i)
        .sort((a, b) => a.p.length - b.p.length || b.p.localeCompare(a.p))
        .map(a => `<button class="chip" type="button" data-p="${a.p}" data-f="${i}">`
                + `${a.p} <b>No. ${a.r}</b> of ${fmt(a.total)}</button>`).join("");
      return `<div><div class="res-t"><a href="${filmUrl(i)}" target="_blank" rel="noopener">${esc(title)}</a>`
           + `<small>${year || ""}${year ? " · " : ""}${best(i)} seats</small></div>`
           + `<div class="chips">${chips}</div></div>`;
    }).join("");
    box.querySelectorAll(".chip").forEach(c => c.addEventListener("click", () => jump(c.dataset.p, +c.dataset.f)));
  }

  function jump(p, f){
    $("filter").value = ""; $("minSeats").value = "1";
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

  /* ---------- open on the current year ---------- */
  const thisYear = String(new Date().getFullYear());
  setPeriod(PERIODS[thisYear] ? thisYear : (years[0] || decades[0]));
  requestAnimationFrame(() => requestAnimationFrame(() => svg.classList.add("animate")));
})();
</script>
"""

if __name__ == "__main__":
    main()
