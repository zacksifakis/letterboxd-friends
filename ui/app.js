let rows=[], sortKey="count", sortDir=-1, maxCount=1, poll=null, curPeriod=null;
const $ = id => document.getElementById(id);

function ago(ts){
  const s = Date.now()/1000 - ts;
  if (s < 3600) return Math.round(s/60)+"m ago";
  if (s < 86400) return Math.round(s/3600)+"h ago";
  const d = Math.round(s/86400);
  return d+(d===1?" day ago":" days ago");
}

let savedInfo = new Map();

async function loadPicker(selected){
  const saved = await (await fetch("/api/saved")).json();
  savedInfo = new Map(saved.map(s => [s.period, s]));
  const nowY = new Date().getFullYear();

  const years = [], decades = [];
  for (let y = nowY; y >= 1920; y--) years.push(String(y));
  for (let d = Math.floor(nowY/10)*10; d >= 1920; d -= 10) decades.push(d + "s");
  // anything scraped outside those ranges (e.g. a stray 2027) still gets listed
  for (const s of saved) {
    const list = s.period.endsWith("s") ? decades : years;
    if (!list.includes(s.period)) list.unshift(s.period);
  }
  years.sort((a,b) => b - a);
  decades.sort((a,b) => parseInt(b) - parseInt(a));

  const option = p => {
    const i = savedInfo.get(p);
    const label = i ? `${p}   ·   ${i.rows.toLocaleString()} film${i.rows === 1 ? "" : "s"}   ·   ${ago(i.mtime)}`
                    : `${p}   ·   not scraped`;
    return `<option value="${p}">${label}</option>`;
  };
  const nFilms = saved.filter(x => x.period.endsWith("s")).reduce((a, x) => a + x.rows, 0);
  $("pick").innerHTML =
      `<option value="all">All years   ·   ${nFilms.toLocaleString()} films</option>`
    + `<optgroup label="── DECADES ──">${decades.map(option).join("")}</optgroup>`
    + `<optgroup label="── YEARS ──">${years.map(option).join("")}</optgroup>`;

  $("pick").value = selected || String(nowY);
  const nYears = saved.filter(s => !s.period.endsWith("s")).length;
  $("hint").textContent = `${nYears} years and ${saved.length - nYears} decades scraped`;
}

function onPick(){
  const p = $("pick").value;
  if (p === "all") {                       // assembled from the saved lists; nothing to scrape
    $("run").style.display = "none";
    showResults(p);
    return;
  }
  $("run").style.display = "";
  if (savedInfo.has(p)) {
    $("run").textContent = "Refresh";
    showResults(p);
  } else {
    $("run").textContent = "Scrape";
    $("resultsCard").style.display = "none";
    $("diffCard").style.display = "none";
  }
}

// Big periods have tens of thousands of films; drawing them all at once makes
// sorting and filtering sluggish, so rows are added in batches as you scroll.
const CHUNK = 500;
let viewRows = [], rendered = 0;

let SEEN = new Set();
let MAD = {now: new Map(), soon: new Map(), data: null, showing: "now", all: false, when: "any"};

const shortDate = iso => new Date(iso + "T12:00:00").toLocaleDateString("en-US", {month: "short", day: "numeric"});
const todayIso = () => new Date().toLocaleDateString("en-CA");

async function loadMadison(){
  try{
    const d = await (await fetch("/api/madison")).json();
    MAD.data = d;
    MAD.now = new Map(d.now.map(x => [x.slug, x]));
    MAD.soon = new Map(d.soon.map(x => [x.slug, x]));
    renderMadison();
    if (d.running) setTimeout(() => loadMadison().then(() => rows.length && render()), 5000);
  }catch(e){}
}

// A venue the newsletter linked opens that film's page at that theater.
function venueLinks(x){
  const links = x.links || {};
  return x.venues.map(v => links[v]
    ? `<a href="${links[v]}" target="_blank" rel="noopener">${v}</a>` : v).join(", ");
}

// A guess from the weekend box office you pasted, with its numbers on hover.
const usd = v => v >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : v >= 1e4 ? `$${Math.round(v / 1e3)}K` : `$${Math.round(v).toLocaleString()}`;
function boxTag(b, x){
  const nums = [`#${b.rank} last weekend`, usd(b.gross) + (b.change != null ? ` (${b.change > 0 ? "+" : ""}${Math.round(b.change)}%)` : "")]
    .concat(b.theaters ? [`${b.theaters.toLocaleString()} theaters` + (b.thChange ? ` (${b.thChange > 0 ? "+" : ""}${b.thChange.toLocaleString()})` : "")] : [])
    .concat(b.avg ? [`${usd(b.avg)} per theater`] : [])
    .concat(b.weeks ? [`week ${b.weeks}`] : []).join(" \u00b7 ");
  const why = b.why ? ` \u2014 ${b.why}` : "";
  const plus = (d, n) => { const t = new Date(d + "T12:00"); t.setDate(t.getDate() + n); return t.toLocaleDateString("en-CA"); };
  const target = x ? ` Will it be in the newsletter out ~${shortDate(x.end)}, for ${shortDate(plus(x.end, 1))}\u2013${shortDate(plus(x.end, 7))}?` : "";
  const title = `${nums}${why}.${target} A guess from last weekend's box office; that newsletter will say for sure.`;
  const label = b.hint === "likely" ? "next week: likely" : b.hint === "unlikely" ? "next week: unlikely" : "next week ?";
  return `<span class="tag ${b.hint}" title="${title.replace(/"/g, "&quot;")}">${label}</span>`;
}

function renderMadison(){
  const d = MAD.data;
  if (!d || (!d.now.length && !d.soon.length)){ $("madCard").hidden = true; return; }
  const list = MAD.showing === "now" ? d.now : d.soon;
  const range = list.length ? `${shortDate(list[0].start)}\u2013${shortDate(list[0].end)}` : "";
  $("madHead").textContent = MAD.showing === "now" ? "Playing in Madison" : "Coming to Madison next week";
  $("madMeta").innerHTML = `${range} \u00b7 ${list.length} films \u00b7 from `
    + (d.latest ? `<a href="${d.latest.url}" target="_blank" rel="noopener">${d.latest.title}</a>` : "The Horizon Line")
    + `, The Horizon Line`;
  // Regular runs vs one-off screenings: a button per day of the week (from
  // today on), each showing that day's screenings plus the all-week runs.
  const t = todayIso();
  const playsOn = (x, d) => (x.days || []).includes(d) || (x.allWeek && x.start <= d && d <= x.end);
  const days = [];
  if (list.length) {
    for (let d = new Date(list[0].start + "T12:00"); ; d.setDate(d.getDate() + 1)) {
      const iso = d.toLocaleDateString("en-CA");
      if (iso > list[0].end) break;
      if (iso >= t) days.push(iso);
    }
  }
  if (MAD.when !== "any" && MAD.when !== "week" && !days.includes(MAD.when)) MAD.when = "any";
  const keep = MAD.when === "any" ? () => true
             : MAD.when === "week" ? x => x.allWeek
             : x => playsOn(x, MAD.when);
  const filtered = list.filter(keep);
  const btn = (when, label, n) =>
    `<button class="mode${MAD.when === when ? " on" : ""}" type="button" data-when="${when}">${label}`
    + (n === undefined ? "" : ` <span class="n">${n}</span>`) + `</button>`;
  $("madWhen").innerHTML = btn("any", "All")
    + days.map(d => btn(d, d === t ? "Today" : shortDay(d), list.filter(x => playsOn(x, d)).length)).join("")
    + (list.some(x => x.allWeek) ? btn("week", "All week", list.filter(x => x.allWeek).length) : "");
  $("madWhenRow").hidden = !list.length;
  const top = topRated(filtered);
  const shown = MAD.all ? filtered : withTop(filtered, 10, top);
  $("madList").innerHTML = shown.map(x => {
    const seen = SEEN.has(x.slug);
    const dayTag = d => `<span class="tag day${d === t ? " is-today" : ""}">${d === t ? "today" : shortDay(d)}</span>`;
    const when = x.allWeek ? `<span class="tag week" title="a regular run, any day this week">all week</span>`
                           : (x.days || []).slice(0, 4).map(dayTag).join("");
    // Does it carry on next week? The newsletter's "next week" section is a
    // preview of what's new, not a full listing (that comes with Wednesday's
    // issue), so only what's certain is marked: listed there, or a one-off
    // screening whose dates are all this week. A regular run not in the
    // preview may well continue: "?".
    let carry = "";
    if (MAD.showing === "now" && d.soon.length) {
      const nx = MAD.soon.get(x.slug);
      if (nx)
        carry = `<span class="tag today" title="also in next week's listings: ${(nx.venues || []).join(", ")}">\u2713 next week too</span>`;
      else if (!x.allWeek && (x.days || []).length)
        carry = `<span class="tag" title="its screenings are all this week">\u2717 not next week</span>`;
      else if (x.box)
        carry = boxTag(x.box, x);
      else
        carry = `<span class="tag" title="not in next week's preview, which only lists what's new; Wednesday's newsletter will say if it continues">next week ?</span>`;
    } else if (MAD.showing === "soon" && !MAD.now.has(x.slug)) {
      carry = `<span class="tag week" title="not playing this week">new</span>`;
    }
    return `<div class="mad${x === top ? " top" : ""}"><span class="mseats ${x.seats ? "" : "none"}" title="${x.seats ? `seen by ${x.seats} people you follow (${x.seatsIn})` : "not in any list you've scraped"}">${x.seats || "\u2014"}</span>`
      + `<div><div class="mt">${seen ? '<span class="tick" title="You have seen this">&#10003;</span>' : ""}`
      + `<a href="https://letterboxd.com/film/${x.slug}/" target="_blank" rel="noopener">${x.title}</a>`
      + hitStats(x) + (x === top ? TOP_TAG : "")
      + (x.section ? `<span class="tag">${x.section}</span>` : "")
      + when + carry
      + (x.note ? `<span class="tag">${x.note}</span>` : "") + `</div>`
      + `<div class="mv">${venueLinks(x) || "venues not listed"}`
      + (x.allWeek ? ` \u00b7 <a href="https://www.google.com/search?q=${encodeURIComponent(x.title + " showtimes Madison WI")}" target="_blank" rel="noopener">showtimes</a>` : "")
      + `</div></div></div>`;
  }).join("");
  if (!filtered.length) $("madList").innerHTML = '<div class="muted">nothing matching that this week</div>';
  $("madMore").textContent = filtered.length > 10 ? (MAD.all ? "show fewer" : `show all ${filtered.length}`) : "";
  const other = MAD.showing === "now" ? d.soon.length : d.now.length;
  $("madToggle").textContent = other ? (MAD.showing === "now" ? `coming next week (${other})` : "back to this week") : "";
  const bx = d.boxoffice, L = d.learned;
  $("madBoxLink").textContent = MAD.showing !== "now" ? ""
    : (bx ? `box office${bx.weekend ? " for " + bx.weekend.replace(/,? \d{4}$/, "") : ""} \u00b7 pasted ${ago(bx.at)}`
          : "paste weekend box office")
      + (L && L.weekends ? ` \u00b7 learned from ${L.weekends} weekend${L.weekends === 1 ? "" : "s"}` : "");
  $("madBoxRecord").innerHTML = boxRecord(L, d.charts || []);
  $("madCard").hidden = false;
}

// How past guesses held up, once a chart's following week has been listed.
function boxRecord(L, charts){
  const saved = charts.length
    ? `Charts kept: ${charts.map(shortDate).join(", ")}.` : "";
  if (!L || !L.examples)
    return `Every chart you paste is kept. Once the following week's newsletter is out, the app checks which films really carried on, `
      + `and the guesses get better as weeks add up. Past weekends count too: the newsletter history goes back to July 16, `
      + `so pasting those weekends' charts (several at once is fine) teaches it straight away. ${saved}`;
  const pct = ([n, k]) => n ? `${Math.round(100 * k / n)}% <span class="n">(${n})</span>` : "\u2014";
  const row = list => list.map(([lab, nk]) => `${lab} ${pct(nk)}`).join(" \u00b7 ");
  let s = `<b>From ${L.weekends} weekend${L.weekends === 1 ? "" : "s"}</b>, ${L.examples} regular runs, ${L.held} carried on. `
    + `The rules got <b>${L.rulesRight} of ${L.rulesSure}</b> sure guesses right.<br>`
    + `Carried on, by week of release: ${row(L.trends.weeks)}<br>`
    + `By take per theater: ${row(L.trends.avg)}<br>`;
  if (L.usingModel)
    s += `Now guessing from a pattern fitted to these weeks: it got ${L.modelRight} of ${L.modelSure} right on weekends it hadn't seen, against ${L.rulesRight} for the rules.`;
  else if (L.modelSure !== undefined)
    s += `A pattern fitted to these weeks didn't beat the rules yet (${L.modelRight} of ${L.modelSure}), so the rules stay.`;
  else
    s += `With ${30 - Math.min(30, L.examples)} more examples it will try fitting its own pattern, and use it only if it beats the rules.`;
  return s + `<br>${saved}`;
}



async function sendBoxOffice(text){
  $("madBoxMsg").textContent = "";
  let res;
  try {
    res = await fetch("/api/boxoffice", {method: "POST", headers: {"Content-Type": "application/json"},
                                         body: JSON.stringify({text})});
  } catch (e) { $("madBoxMsg").textContent = "couldn't reach the app"; return; }
  const d = await res.json();
  if (!res.ok) { $("madBoxMsg").textContent = d.error; return; }
  await loadMadison();
  const hinted = MAD.data.now.filter(x => x.box).length;
  $("madBoxMsg").textContent = !text.trim() ? "cleared"
    : (d.weekends.length > 1 ? `kept ${d.weekends.length} weekends · newest: ` : "")
      + `read ${d.rows} films · ${hinted} playing in Madison this week`;
  if (text.trim()) $("madBoxText").value = "";
}
$("madBoxLink").onclick = e => { e.preventDefault(); $("madBox").hidden = !$("madBox").hidden; if (!$("madBox").hidden) $("madBoxText").focus(); };
$("madBoxGo").onclick = () => sendBoxOffice($("madBoxText").value);
$("madBoxClear").onclick = () => sendBoxOffice("");

function showMadison(tr){
  const btn = tr.querySelector(".madb"), slug = tr.dataset.slug;
  const x = MAD.now.get(slug) || MAD.soon.get(slug);
  if (!x) return;
  const open = panelBelow(tr, "madison");
  if (open){ open.remove(); return; }
  const when = MAD.now.has(slug) ? `this week (${shortDate(x.start)}\u2013${shortDate(x.end)})`
                                  : `next week (${shortDate(x.start)}\u2013${shortDate(x.end)})`;
  const body = `Playing in Madison ${when}${x.section ? ` \u00b7 ${x.section}` : ""}${x.note ? ` \u00b7 ${x.note}` : ""}: `
    + `<b>${venueLinks(x) || "venues not listed"}</b> \u00b7 `
    + `<a href="${x.url}" target="_blank" rel="noopener">${x.post}</a>`;
  tr.insertAdjacentHTML("afterend", `<tr class="wrow" data-kind="madison"><td></td><td colspan="4">${body}</td></tr>`);
}

let riseDays = 7, riseAll = false;
async function loadRising(days){
  if (days) { riseDays = days; riseAll = false; }
  let d;
  try { d = await (await fetch(`/api/rising?days=${riseDays}`)).json(); } catch (e) { return; }
  const top = topRated(d.rows);
  const rows = riseAll ? d.rows : withTop(d.rows, 8, top);
  document.querySelectorAll("#riseSince button").forEach(b =>
    b.classList.toggle("on", +b.dataset.days === riseDays));
  $("riseMeta").textContent = d.rows.length
    ? `${d.films.toLocaleString()} films gained seats` + (d.since ? ` · since ${ago(d.since)}` : "")
    : "nothing has moved yet";
  $("riseList").innerHTML = rows.map(x => {
    const img = POSTERS.has(x.slug)
      ? `<img src="/poster/${encodeURIComponent(x.slug)}.jpg" alt="" loading="lazy">`
      : '<span class="noimg"></span>';
    return `<div class="rise${x === top ? " top" : ""}">${img}<span class="up">+${x.delta}</span>`
      + `<span class="t"><a href="https://letterboxd.com/film/${x.slug}/" target="_blank" rel="noopener">${x.title}</a>`
      + (x.isNew ? '<span class="new">NEW</span>' : "")
      + `<span class="where"> ${x.year || ""}</span>${hitStats(x)}${x === top ? TOP_TAG : ""}</span>`
      + `<span class="where">${x.count} seats · ${x.period}</span></div>`;
  }).join("");
  $("riseMore").textContent = d.rows.length > 8 ? (riseAll ? "show fewer" : `show all ${d.rows.length}`) : "";
  $("riseCard").hidden = !d.rows.length;
}

let POSTERS = new Set();         // films with a poster cached on this Mac
async function loadPosters(){
  try {
    const d = await (await fetch("/api/posters")).json();
    POSTERS = new Set(d.slugs || []);
  } catch (e) {}
}

let rankOf = new Map();          // count -> rank, so ties share a number

function computeRanks(){
  rankOf = new Map();
  rows.forEach((x, i) => { if (!rankOf.has(x.count)) rankOf.set(x.count, i + 1); });
}

function ratingCell(x){
  // only lists scraped since ratings were added carry these
  if (x.rating == null) return "";
  const few = (x.ratings || 0) < 10;
  return `<b class="${few ? "few" : ""}" title="average of ${x.ratings} friend${x.ratings === 1 ? "" : "s"} who rated it`
    + `${few ? " — fewer than 10, so it sorts below the rest" : ""}">${x.rating.toFixed(1)}\u2605</b>`;
}
function likedCell(x){
  if (x.liked == null) return "";
  const few = (x.count || 0) < 10;
  return `<span class="heart${few ? " few" : ""}" title="${x.liked}% of the ${x.count} who watched it liked it`
    + `${few ? " — fewer than 10, so it sorts below the rest" : ""}">${x.liked}%\u2665</span>`;
}

function rowHtml(x){
  const seen = SEEN.has(x.slug);
  const poster = POSTERS.has(x.slug)
    ? `<img class="poster" src="/poster/${encodeURIComponent(x.slug)}.jpg" alt="" loading="lazy" decoding="async">`
    : '<span class="poster"></span>';
  return `<tr class="${seen ? "seen" : ""}" data-slug="${x.slug}"><td class="rank">${rankOf.get(x.count)}</td>`
    + `<td class="film">${poster}<span class="fi">${seen ? '<span class="tick" title="You have seen this">&#10003;</span>' : ""}`
    + `<a href="${x.url}" target="_blank" rel="noopener">${x.title}</a>`
    + (MAD.now.has(x.slug) ? `<button class="madb now" type="button">in Madison</button>`
       : MAD.soon.has(x.slug) ? `<button class="madb soon" type="button">Madison next week</button>` : "")
    + `<button class="who" type="button" aria-expanded="false">who?</button>`
    + `<button class="lists" type="button" aria-expanded="false">lists</button></span></td>`
    + `<td class="yr">${x.year || ""}</td>`
    + `<td class="rate">${ratingCell(x)}</td><td class="rate">${likedCell(x)}</td>`
    + `<td class="cnt">${x.count}</td>`
    + `<td class="meter"><i style="width:${Math.max(2, 100 * x.count / maxCount)}%"></i></td></tr>`;
}

// ?demo shows the app without your own watch history — for screenshots.
const DEMO = new URLSearchParams(location.search).has("demo");

async function loadSeen(){
  if (DEMO) { SEEN = new Set(); return; }
  try{
    const d = await (await fetch("/api/seen")).json();
    SEEN = new Set(d.slugs || []);
    const label = $("hideSeen").parentElement;
    label.title = d.count
      ? `${d.count.toLocaleString()} films on your own profile, checked ${ago(Date.parse(d.asOf) / 1000)}`
      : (d.running ? "Fetching your own watched list\u2026" : "Your own watched list hasn't been fetched yet");
    if (d.running) setTimeout(() => loadSeen().then(render), 4000);
  }catch(e){}
}
// The app can stay open for days: pick up newly logged films without a reload.
setInterval(async () => {
  const before = SEEN.size;
  await loadSeen();
  if (SEEN.size !== before && rows.length) render();
}, 20 * 60 * 1000);

function panelBelow(tr, kind){
  for (let el = tr.nextElementSibling; el && el.classList.contains("wrow"); el = el.nextElementSibling)
    if (el.dataset.kind === kind) return el;
  return null;
}

async function showWatchers(tr){
  const btn = tr.querySelector(".who"), slug = tr.dataset.slug;
  const src = curPeriod === "all" ? (rows.find(r => r.slug === slug) || {}).src : curPeriod;
  if (btn.getAttribute("aria-expanded") === "true"){
    const panel = panelBelow(tr, "who");
    if (panel) panel.remove();
    btn.setAttribute("aria-expanded", "false"); btn.textContent = "who?";
    return;
  }
  btn.setAttribute("aria-expanded", "true"); btn.textContent = "hide";
  const res = await fetch(`/api/watchers?period=${encodeURIComponent(src)}&slug=${encodeURIComponent(slug)}`);
  const d = await res.json();
  const body = res.ok
    ? (d.watchers.length
        ? `<b>${d.watchers.length}</b> of the people you follow: `
          + d.watchers.map(w => {
              const u = typeof w === "string" ? w : w.user;
              const mark = (typeof w === "object" && (w.stars || w.liked))
                ? `<span class="mark">${w.stars ? w.stars.toFixed(1).replace(".0", "") + "\u2605" : ""}`
                  + `${w.liked ? " \u2665" : ""}</span>` : "";
              return `<a href="https://letterboxd.com/${u}/" target="_blank" rel="noopener">${u}</a>${mark}`;
            }).join(", ")
        : "None of the people you follow logged this one.")
    : d.error;
  tr.insertAdjacentHTML("afterend", `<tr class="wrow" data-kind="who"><td></td><td colspan="4">${body}</td></tr>`);
}

// "Recommend me something": films you haven't seen, rated by at least 10
// friends, best-rated first. Combines with the title filter and with
// "Playing in Madison", which makes it "what should I go see this week".
let recommend = false;
function setRecommend(on){
  recommend = on;
  $("recBtn").classList.toggle("on", on);
  $("recBtn").textContent = on ? "\u2715 Back to the full list" : "Recommend me something";
  if (on) { sortKey = "rating"; sortDir = -1; }
  else { sortKey = "count"; sortDir = -1; }
  render();
}
$("recBtn").onclick = () => setRecommend(!recommend);

function render(){
  const q = $("filter").value.toLowerCase();
  const min = parseInt($("minSeen").value || "1", 10);
  const hide = $("hideSeen").checked || recommend;
  const onlyMad = $("onlyMadison").checked;
  let r = rows.filter(x => x.count >= min && (!hide || !SEEN.has(x.slug))
                        && (!onlyMad || MAD.now.has(x.slug))
                        && (!recommend || (x.rating != null && (x.ratings || 0) >= 10))
                        && (!q || x.title.toLowerCase().includes(q)));
  const note = $("recNote");
  if (recommend) {
    note.innerHTML = r.length
      ? `<b>${r.length.toLocaleString()} film${r.length === 1 ? "" : "s"} you haven't seen</b>, `
        + `rated by at least 10 friends, best first${onlyMad ? " — playing in Madison now" : ""}.`
      : (onlyMad ? "Nothing playing in Madison this week that you haven't seen has 10+ ratings."
                 : "Nothing here you haven't seen has 10+ ratings yet.");
    note.hidden = false;
  } else note.hidden = true;
  // Rating and Liked sort strictly by the number shown. Films with fewer than
  // MIN_BASE ratings (or watchers, for Liked) go below the rest, still in order,
  // so a 5.0 from one person can't take the top spot.
  const MIN_BASE = 10;
  const base = (x, k) => k === "rating" ? (x.ratings || 0) : (x.count || 0);
  r.sort((a,b)=>{ let A=a[sortKey],B=b[sortKey];
    if(sortKey==="rating"||sortKey==="liked"){
      const fa = base(a, sortKey) < MIN_BASE, fb = base(b, sortKey) < MIN_BASE;
      if (A != null && B != null && fa !== fb) return fa ? 1 : -1;   // few-ratings films last
      if(A==null&&B==null) return b.count-a.count;
      if(A==null) return 1;              // unrated films sink, either direction
      if(B==null) return -1;
      return A<B?-sortDir:A>B?sortDir:b.count-a.count;
    }
    if(sortKey!=="count"){A=String(A).toLowerCase();B=String(B).toLowerCase();}
    return A<B?-sortDir:A>B?sortDir:0; });
  $("shown").textContent = `${r.length.toLocaleString()} of ${rows.length.toLocaleString()}`;
  viewRows = r; rendered = 0;
  $("tb").innerHTML = "";
  appendRows();
}

function appendRows(){
  const next = viewRows.slice(rendered, rendered + CHUNK);
  if (next.length) {
    $("tb").insertAdjacentHTML("beforeend", next.map(rowHtml).join(""));
    rendered += next.length;
  }
  const left = viewRows.length - rendered;
  $("more").style.display = left > 0 ? "flex" : "none";
  $("moreInfo").textContent = `showing ${rendered.toLocaleString()} of ${viewRows.length.toLocaleString()}`;
  $("loadMore").textContent = `Load ${Math.min(CHUNK, left).toLocaleString()} more`;
}

window.addEventListener("scroll", () => {
  if (rendered < viewRows.length &&
      window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 1500)
    appendRows();
}, {passive: true});

async function showResults(period){
  const res = await fetch("/api/results?period="+encodeURIComponent(period));
  if(!res.ok){ alert("No saved results for "+period); return; }
  const d = await res.json();
  rows = d.rows; curPeriod = d.period; maxCount = Math.max(1,...rows.map(r=>r.count));
  computeRanks();
  $("resultsCard").style.display="block";
  $("sPeriod").textContent = d.period === "all" ? "All years" : d.period; $("sFilms").textContent=rows.length.toLocaleString();
  $("sTop").textContent=maxCount; $("sAge").textContent=ago(d.mtime);
  $("dl").href="/api/download?period="+encodeURIComponent(d.period);
  $("dl").style.display = d.period === "all" ? "none" : "";
  $("showDiff").style.display = d.hasPrev ? "inline" : "none";
  $("diffCard").style.display="none";
  render();
}

let diffSince = "last";
async function showDiff(period, since){
  if (since) diffSince = since;
  const res = await fetch(`/api/diff?period=${encodeURIComponent(period)}&since=${diffSince}`);
  if(!res.ok){ alert("Nothing to compare against yet for that stretch of time."); return; }
  const d = await res.json();
  $("diffCard").style.display="block";
  document.querySelectorAll("#diffSince button").forEach(b =>
    b.classList.toggle("on", b.dataset.since === d.since));
  $("diffMeta").textContent =
    `vs ${d.baseline} · ${ago(d.prevTime)} → ${ago(d.curTime)} · `
    + `${d.prevFilms.toLocaleString()} → ${d.curFilms.toLocaleString()} films`;
  $("gainHead").textContent = `Gained watches (${d.nGained})`;
  $("addHead").textContent = `New films (${d.nAdded})`;
  const filmLink = x =>
    `<a href="https://letterboxd.com/film/${x.slug}/" target="_blank" rel="noopener">${x.title}</a>`;
  $("gained").innerHTML = d.gained.length
    ? d.gained.map(x=>`<div><span class="up">+${x.delta}</span> ${filmLink(x)} <span class="muted">(${x.year||"?"}) → ${x.count}</span>${hitStats(x)}</div>`).join("")
    : '<div class="muted">nothing gained</div>';
  $("added").innerHTML = d.added.length
    ? d.added.map(x=>`<div>${filmLink(x)} <span class="muted">(${x.year||"?"})</span> <span class="up">${x.count}</span><span class="new">NEW</span>${hitStats(x)}</div>`).join("")
    : '<div class="muted">no new films</div>';
  if (d.nDropped) $("added").innerHTML +=
    `<div class="muted" style="margin-top:8px">${d.nDropped} film(s) lost watches</div>`;
}

document.querySelectorAll("th[data-k]").forEach(th => th.onclick = ()=>{
  const k=th.dataset.k;
  if(sortKey===k) sortDir=-sortDir; else {sortKey=k; sortDir = k==="count"?-1:1;}
  render();
});
$("filter").oninput = render;
$("loadMore").onclick = appendRows;
$("tb").addEventListener("click", e => {
  const who = e.target.closest(".who");
  if (who) { showWatchers(who.closest("tr")); return; }
  const lists = e.target.closest(".lists");
  if (lists) { showLists(lists.closest("tr")); return; }
  const mad = e.target.closest(".madb");
  if (mad) showMadison(mad.closest("tr"));
});

async function showLists(tr){
  const btn = tr.querySelector(".lists"), slug = tr.dataset.slug;
  if (btn.getAttribute("aria-expanded") === "true"){
    const panel = panelBelow(tr, "lists");
    if (panel) panel.remove();
    btn.setAttribute("aria-expanded", "false"); btn.textContent = "lists";
    return;
  }
  btn.setAttribute("aria-expanded", "true"); btn.textContent = "hide";
  const d = await (await fetch("/api/lists?slug=" + encodeURIComponent(slug))).json();
  let body;
  if (d.lists === null)
    body = d.running ? "Still reading your liked lists\u2026 try again in a minute."
                     : (d.error || "Your liked lists haven't been read yet.");
  else if (!d.lists.length)
    body = `Not in any of your ${d.total} liked lists.`;
  else
    body = `In <b>${d.lists.length}</b> of your ${d.total} liked lists: `
      + d.lists.map(l => `<a href="${l.url}" target="_blank" rel="noopener">${l.name}</a>`
        + ` <span class="pos">#${l.position.toLocaleString()} of ${l.size.toLocaleString()}</span>`).join(", ");
  tr.insertAdjacentHTML("afterend", `<tr class="wrow" data-kind="lists"><td></td><td colspan="4">${body}</td></tr>`);
}
try { $("showPosters").checked = localStorage.getItem("showPosters") !== "0"; } catch (e) {}
function paintPosters(){
  $("tb").classList.toggle("posters", $("showPosters").checked);
}
$("showPosters").onchange = () => {
  try { localStorage.setItem("showPosters", $("showPosters").checked ? "1" : "0"); } catch (e) {}
  paintPosters();
};
paintPosters();
try { $("hideSeen").checked = localStorage.getItem("hideSeen") === "1"; } catch (e) {}
$("onlyMadison").onchange = render;
const shortDay = iso => new Date(iso + "T12:00").toLocaleDateString("en-US", {weekday: "short", month: "numeric", day: "numeric"});
$("madWhen").addEventListener("click", e => {
  const b = e.target.closest(".mode");
  if (!b) return;
  MAD.when = b.dataset.when; MAD.all = false; renderMadison();
});
$("madMore").onclick = e => { e.preventDefault(); MAD.all = !MAD.all; renderMadison(); };
$("madToggle").onclick = e => {
  e.preventDefault(); MAD.showing = MAD.showing === "now" ? "soon" : "now"; MAD.all = false;
  renderMadison();
};
$("hideSeen").onchange = () => {
  try { localStorage.setItem("hideSeen", $("hideSeen").checked ? "1" : "0"); } catch (e) {}
  render();
};

async function loadHealth(){
  const h = await (await fetch("/api/health")).json();
  const el = $("health"), now = Date.now() / 1000;
  const sinceTick = h.lastTick ? (now - h.lastTick) / 3600 : Infinity;
  const problems = [];
  if (!h.tickLoaded) problems.push("<b>auto-sync is not installed</b>");
  else if (h.tickRunningSince) {
    // a check is running — only worth a word if it's been going far too long
    if ((now - h.tickRunningSince) / 3600 > 6) problems.push("<b>auto-sync has been running for hours</b>");
  }
  else if (sinceTick > 3) problems.push(`<b>auto-sync hasn't run for ${h.lastTick ? ago(h.lastTick).replace(" ago", "") : "a while"}</b>`);
  if (h.yearAge === null || h.yearAge > 1.25) problems.push("<b>this year's data is out of date</b>");
  if (h.outcome === "failed") problems.push("<b>last refresh failed</b>");
  if (h.outcome === "incomplete") problems.push("<b>last refresh had missing friends</b>");
  // whatever the hourly self-check found, in its own words
  for (const p of (h.check?.problems || []))
    problems.push(p.level === "serious" ? `<b>${p.what}</b>` : p.what);

  const parts = ['<span class="dot"></span>'];
  parts.push(h.yearAge !== null ? `this year refreshed <b>${ago(now - h.yearAge * 86400)}</b>` : "this year never refreshed");
  parts.push('<span class="sep">|</span>');
  parts.push(h.staleDecades.length
    ? `${h.staleDecades.length} decade${h.staleDecades.length > 1 ? "s" : ""} catching up`
      + (h.oldest ? ` (oldest: ${h.oldest[0]}, ${h.oldest[1] === null ? "never" : Math.round(h.oldest[1]) + " days"})` : "")
    : "all decades current");
  parts.push('<span class="sep">|</span>');
  parts.push(h.tickRunningSince ? `auto-sync running now (started ${ago(h.tickRunningSince)})`
             : h.lastTick ? `auto-sync last ran ${ago(h.lastTick)}` : "auto-sync hasn't run yet");
  if (problems.length) parts.push('<span class="sep">|</span>', problems.join(" \u00b7 "));
  el.innerHTML = parts.join(" ");
  el.className = problems.length ? "health warn" : "health";
  el.hidden = false;
}
try { const v = localStorage.getItem("minSeen"); if (v) $("minSeen").value = v; } catch (e) {}
$("minSeen").onchange = () => {
  try { localStorage.setItem("minSeen", $("minSeen").value); } catch (e) {}
  render();
};
$("pick").onchange = onPick;
$("showDiff").onclick = e => { e.preventDefault(); showDiff(curPeriod); };
$("riseSince").addEventListener("click", e => {
  const b = e.target.closest("button");
  if (b) loadRising(+b.dataset.days);
});
$("riseMore").onclick = e => { e.preventDefault(); riseAll = !riseAll; loadRising(); };
$("diffSince").addEventListener("click", e => {
  const b = e.target.closest("button");
  if (b) showDiff(curPeriod, b.dataset.since);
});
$("quit").onclick = async ()=>{
  if(!confirm("Stop the app?")) return;
  await fetch("/api/quit",{method:"POST"});
  document.body.innerHTML='<main><div class="card">App stopped. '
    +'Restart with <code>python3 ~/Desktop/letterboxd_app.py</code> '
    +'or the Letterboxd Friends app.</div></main>';
};

$("run").onclick = async ()=>{
  const period=$("pick").value;
  const res=await fetch("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},
                                    body:JSON.stringify({period})});
  if(!res.ok){ alert((await res.json()).error); return; }
  $("progwrap").style.display="block"; $("run").disabled=true;
  $("quality").innerHTML=""; $("diffCard").style.display="none";
  if(poll) clearInterval(poll);
  poll=setInterval(tick,1200); tick();
};

async function tick(){
  const s = await (await fetch("/api/status")).json();
  $("log").textContent = s.lines.slice(-40).join("\n");
  $("log").scrollTop = $("log").scrollHeight;
  if(s.progress) $("bar").style.width = (100*s.progress.done/s.progress.total)+"%";
  if(s.quality) $("quality").innerHTML =
    `<div class="flag ${s.quality.ok?"ok":"bad"}">${s.quality.text}</div>`;
  if(!s.running && s.finished){
    clearInterval(poll); poll=null; $("run").disabled=false; $("bar").style.width="100%";
    if(s.returncode===0){ await loadPicker(s.period); await showResults(s.period);
                          $("run").textContent = "Refresh";
                          await showDiff(s.period).catch(()=>{}); }
  }
}

let mode = "film";
document.querySelectorAll(".mode[data-m]").forEach(b => b.onclick = ()=>{
  document.querySelectorAll(".mode[data-m]").forEach(x=>x.classList.remove("on"));
  b.classList.add("on"); mode = b.dataset.m;
});

function posChips(list){
  if(!list.length) return '<span class="pos none">not scraped yet</span>';
  return list.map(a=>`<span class="pos">${a.period} <b>#${a.rank}</b>`
    + `<span class="muted"> of ${a.outOf.toLocaleString()} · ${a.count} seen</span></span>`).join("");
}

async function doSearch(){
  const q = $("q").value.trim();
  if(q.length < 2){ alert("Type at least 2 characters."); return; }
  $("qout").innerHTML = '<span class="muted">searching…</span>';
  $("qhint").textContent = "";

  if(mode === "film"){
    const d = await (await fetch("/api/search?q="+encodeURIComponent(q))).json();
    if(!d.hits.length){
      $("qout").innerHTML = '<span class="muted">No film titles match that in your scraped periods.</span>'
        + '<div style="margin-top:8px">Looking for a person? Search it as '
        + '<a href="#" class="switch" data-m="actor">an actor</a> or '
        + '<a href="#" class="switch" data-m="director">a director</a>.</div>';
      wireSwitches(); return;
    }
    $("qhint").textContent = `${d.hits.length} match${d.hits.length>1?"es":""}`;
    $("qout").innerHTML = d.hits.map(h=>`<div class="hit">
        <div class="t"><a href="${h.url}" target="_blank" rel="noopener">${h.title}</a>
        <span class="muted">${h.year?"("+h.year+")":""}</span>${hitStats(h)}</div>
        <div>${posChips(h.in)}</div></div>`).join("");
    return;
  }

  const res = await fetch(`/api/person?name=${encodeURIComponent(q)}&role=${mode}`);
  if(!res.ok){ $("qout").innerHTML =
      `<span class="muted">${(await res.json()).error}</span>`; return; }
  const d = await res.json();
  $("qhint").textContent = `${d.name} as ${d.role} · ${d.scraped} of ${d.total} films in your data`;
  const also = d.alsoRole
    ? ` · also credited as ${d.alsoRole === "actor" ? "an actor" : "a director"} — `
      + `<a href="#" class="switch" data-m="${d.alsoRole}">show those</a>`
    : "";
  $("qout").innerHTML =
    `<div class="hit"><a href="${d.personUrl}" target="_blank" rel="noopener">${d.personUrl}</a>${also}</div>`
    + d.films.map(f=>`<div class="hit">
        <div class="t"><a href="${f.url}" target="_blank" rel="noopener">${f.title}</a>
        <span class="muted">${f.year?"("+f.year+")":""}</span>${hitStats(f)}</div>
        <div>${posChips(f.in)}</div></div>`).join("");
  wireSwitches();
}

// Friends' average and liked share beside a search result, when the list it
// came from has them.
// The highest-rated film in a list, by the people you follow: only films with
// 10+ ratings count (fewer says little), ties go to the one more people rated.
function topRated(list){
  let best = null;
  for (const x of list)
    if (x.rating != null && (x.ratings || 0) >= 10
        && (!best || x.rating > best.rating || (x.rating === best.rating && x.ratings > best.ratings)))
      best = x;
  return best;
}
const TOP_TAG = '<span class="tag top" title="the highest rated in this list by the people you follow (10+ ratings)">\u2605 highest rated</span>';

// Show the first n, plus the top-rated film if it would otherwise be cut off.
function withTop(list, n, top){
  const out = list.slice(0, n);
  if (top && !out.includes(top)) out.push(top);
  return out;
}

function hitStats(h){
  if (h.rating == null && h.liked == null) return "";
  // Like the main list: under 10 ratings the average says little, so it's greyed out.
  const few = (h.ratings || 0) < 10;
  return `<span class="hitstats${few ? " few" : ""}" title="${h.ratings} of the ${h.seats} friends who watched it rated it`
    + `${h.liked != null ? `; ${h.liked}% of the ${h.seats} pressed like` : ""}${few ? " — too few ratings to say much" : ""}">`
    + (h.rating != null ? `<b>${h.rating.toFixed(1)}\u2605</b><span class="n">${h.ratings}</span>` : "")
    + (h.liked != null ? ` <span class="heart">${h.liked}%\u2665</span>` : "") + `</span>`;
}

function wireSwitches(){
  document.querySelectorAll("#qout .switch").forEach(a => a.onclick = e => {
    e.preventDefault();
    document.querySelector(`.mode[data-m="${a.dataset.m}"]`).click();
    doSearch();
  });
}

$("go").onclick = doSearch;
$("q").addEventListener("keydown", e=>{ if(e.key==="Enter") doSearch(); });

loadSeen().then(() => loadPosters()).then(() => loadRising()).then(() => loadMadison())
          .then(() => loadPicker().then(onPick));

// Re-read the listings every 10 minutes: picks up a new weekly report, and
// rolls "this week" over at midnight even when nothing new was fetched.
setInterval(async () => {
  const stamp = d => d ? `${d.asOf}|${d.now.length}|${d.soon.length}` : "";
  const before = stamp(MAD.data);
  await loadMadison();
  if (stamp(MAD.data) !== before && rows.length) render();
}, 10 * 60 * 1000);
loadHealth();
(async()=>{ const s=await (await fetch("/api/status")).json();
  if(s.running){ $("progwrap").style.display="block"; $("run").disabled=true;
                 poll=setInterval(tick,1200); tick(); } })();

// ⌘F / Ctrl+F: jump to the list's title filter (the list only draws rows as you
// scroll, so the browser's own find would miss most films); with no list open,
// to film search.
document.addEventListener("keydown", e => {
  if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === "f") {
    const box = $("resultsCard").style.display === "block" ? $("filter") : $("q");
    e.preventDefault();
    box.scrollIntoView({block: "center", behavior: "smooth"});
    box.focus();
    box.select();
  }
});


// Films on any web page (a cinema's programme, a festival lineup), ranked by
// how many people you follow have seen them.
async function findPageFilms(){
  const url = $("pageUrl").value.trim();
  if (!url) return;
  try { localStorage.setItem("pageUrl", url); } catch (e) {}
  $("pageGo").disabled = true;
  $("pageOut").innerHTML = '<div class="muted">reading the page\u2026 (a big programme can take up to a minute)</div>';
  try {
    const res = await fetch("/api/page?url=" + encodeURIComponent(url));
    const d = await res.json();
    if (!res.ok) { $("pageOut").innerHTML = `<div class="muted">${d.error}</div>`; return; }
    const top = topRated(d.films);
    const row = x => {
      const seen = x.slug && SEEN.has(x.slug);
      const name = x.slug
        ? `<a href="https://letterboxd.com/film/${x.slug}/" target="_blank" rel="noopener">${x.title}</a>`
        : `<a href="https://www.imdb.com/title/${x.imdb}/" target="_blank" rel="noopener">${x.title}</a>`;
      return `<div class="mad${x === top ? " top" : ""}"><span class="mseats ${x.seats ? "" : "none"}">${x.seats || "\u2014"}</span>`
        + `<div><div class="mt">${seen ? '<span class="tick" title="You have seen this">&#10003;</span>' : ""}`
        + `${name} <span class="muted">${x.year || ""}</span>${hitStats(x)}${x === top ? TOP_TAG : ""}`
        + (x.text ? `<span class="tag" title="as written on the page">${x.text}</span>` : "")
        + (x.checked ? `<span class="tag today" title="${x.how.startsWith("corrected") || x.how.startsWith("found") ? x.how : "confirmed by " + x.checked}">\u2713 ${x.checked}</span>`
           : x.how === "spelled in Greek" ? `<span class="tag" title="matched by sound — check it">\u2248</span>` : "")
        + `</div>` + (x.times.length ? `<div class="mv">${x.times.join(" \u00b7 ")}</div>` : "")
        + `</div></div>`;
    };
    $("pageOut").innerHTML =
        `<div class="muted" style="margin-bottom:6px">${d.films.length} films on <a href="${d.url}" target="_blank" rel="noopener">${d.title}</a>`
      + (d.checkedWith ? ` \u00b7 titles checked against <a href="${d.checkedWith}" target="_blank" rel="noopener">${new URL(d.checkedWith).hostname.replace("www.", "")}</a>` : "")
      + `</div>`
      + `<div class="madlist">${d.films.map(row).join("") || '<div class="muted">no films recognised on that page</div>'}</div>`
      + (d.unmatched.length ? `<div class="muted" style="margin-top:8px">Couldn't place: ${d.unmatched.join(", ")}</div>` : "");
  } catch (e) {
    $("pageOut").innerHTML = '<div class="muted">something went wrong reading that page</div>';
  } finally {
    $("pageGo").disabled = false;
  }
}
$("pageGo").onclick = findPageFilms;
$("pageUrl").addEventListener("keydown", e => { if (e.key === "Enter") findPageFilms(); });
try { $("pageUrl").value = localStorage.getItem("pageUrl") || ""; } catch (e) {}
