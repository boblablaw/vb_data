/* VB Fantasy — vanilla JS client for the vb_data stats API. No build step, no deps. */
"use strict";

/* ---------- tiny DOM helpers ---------- */
function el(tag, attrs, children) {
  const n = document.createElement(tag);
  if (attrs) {
    for (const k in attrs) {
      const v = attrs[k];
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "html") n.innerHTML = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
      else if (k === "dataset") for (const d in v) n.dataset[d] = v[d];
      else n.setAttribute(k, v);
    }
  }
  if (children != null) {
    (Array.isArray(children) ? children : [children]).forEach((c) => {
      if (c == null || c === false) return;
      n.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
    });
  }
  return n;
}
const $ = (sel, root) => (root || document).querySelector(sel);
const clear = (n) => { while (n.firstChild) n.removeChild(n.firstChild); return n; };

/* ---------- API ---------- */
async function api(path, params) {
  const url = new URL(path, window.location.origin);
  if (params) for (const k in params) {
    const v = params[k];
    if (v != null && v !== "") url.searchParams.set(k, v);
  }
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  if (!res.ok) {
    let detail = res.statusText;
    try { const j = await res.json(); detail = j.detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.json();
}

let toastTimer = null;
function toast(msg, isErr) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, isErr ? 5000 : 2500);
}

/* ---------- formatting ---------- */
const fmt = (v, d) => (v == null ? "—" : Number(v).toFixed(d == null ? 1 : d));
const fmtInt = (v) => (v == null ? "—" : Math.round(v).toLocaleString());
const heightStr = (inches) => (inches == null ? null : `${Math.floor(inches / 12)}'${inches % 12}"`);

/* ---------- app state ---------- */
const DEFAULT_WEIGHTS = {
  kills: 1.0, aces: 1.5, digs: 0.5, assists: 0.25, block_solos: 1.0,
  block_assists: 0.5, errors: -0.5, serr: -0.5, rerr: -0.25, berr: -0.25, bhe: -0.25,
};
const WEIGHT_LABELS = {
  kills: "Kills", aces: "Aces", digs: "Digs", assists: "Assists",
  block_solos: "Block solos", block_assists: "Block assists", errors: "Attack errors",
  serr: "Service errors", rerr: "Recept. errors", berr: "Block errors", bhe: "BHE",
};

// Leaderboard stat catalog (label, api key, decimals). *_per_set are rate boards.
const STATS = [
  { key: "kills", label: "Kills", d: 0 },
  { key: "pts", label: "Points", d: 1 },
  { key: "total_blocks", label: "Blocks", d: 0 },
  { key: "assists", label: "Assists", d: 0 },
  { key: "digs", label: "Digs", d: 0 },
  { key: "retatt", label: "Receptions", d: 0 },
  { key: "aces", label: "Aces", d: 0 },
  { key: "hit_pct", label: "Hit %", d: 3 },
  { key: "kills_per_set", label: "Kills/set", d: 2 },
  { key: "assists_per_set", label: "Assists/set", d: 2 },
  { key: "digs_per_set", label: "Digs/set", d: 2 },
  { key: "aces_per_set", label: "Aces/set", d: 2 },
  { key: "blocks_per_set", label: "Blocks/set", d: 2 },
  { key: "pts_per_set", label: "Points/set", d: 2 },
];
const statMeta = (k) => STATS.find((s) => s.key === k) || { key: k, label: k, d: 1 };

// Rate-stat qualifiers: a leaderboard for a rate needs a minimum-sample floor, or a player
// with one lucky kill (Hit% 1.000) or a couple sets tops the board. Hit% floors on attempts
// (a libero plays plenty of sets but rarely attacks), per-set rates floor on sets. Counting
// stats need no floor — the raw total already self-qualifies. Defaults are scale-aware (the
// season is live and growing) and user-adjustable in the filter box.
const QUALIFIERS = {
  hit_pct:         { by: "attacks", label: "Min attacks", season: 20, week: 8 },
  kills_per_set:   { by: "sets", label: "Min sets", season: 6, week: 2 },
  assists_per_set: { by: "sets", label: "Min sets", season: 6, week: 2 },
  digs_per_set:    { by: "sets", label: "Min sets", season: 6, week: 2 },
  aces_per_set:    { by: "sets", label: "Min sets", season: 6, week: 2 },
  blocks_per_set:  { by: "sets", label: "Min sets", season: 6, week: 2 },
  pts_per_set:     { by: "sets", label: "Min sets", season: 6, week: 2 },
};
const state = {
  tab: "top",
  season: null,
  seasons: [],
  weeks: [],
  conferences: [],
  // Per-tab filter state: each leaderboard-style screen keeps its OWN scope/week/conference/position/
  // stat/qualifier, so changing one screen's filters never leaks into another. Season is global (it
  // lives in the topbar and applies everywhere). Tabs with no filters (compare, player) get no slice.
  filters: {
    top: defaultFilters(),
    fantasy: defaultFilters(),
    teams: defaultFilters(),
    waiver: defaultFilters(),
    team: defaultFilters(),
  },
  minSets: 0,
  weights: loadWeights(),
  compare: loadCompare(),
};

function defaultFilters() {
  return { scope: "season", week: "", conf: "", pos: "", stat: "kills", min: null };
}

// The active tab's filter slice (created on demand for any tab that needs one).
function f() {
  return state.filters[state.tab] || (state.filters[state.tab] = defaultFilters());
}

// Active qualifier for the current Stat-Leaders stat+scope. The slice's `min` overrides the default
// (null = use default); the default recomputes when the stat or scope changes.
function activeQualifier() {
  const cur = f();
  const q = QUALIFIERS[cur.stat];
  if (!q) return null;
  const def = cur.scope === "week" ? q.week : q.season;
  return { by: q.by, label: q.label, def, val: cur.min == null ? def : cur.min };
}

function loadWeights() {
  try {
    const raw = localStorage.getItem("vb-weights");
    if (raw) return Object.assign({}, DEFAULT_WEIGHTS, JSON.parse(raw));
  } catch (e) {}
  return Object.assign({}, DEFAULT_WEIGHTS);
}
function saveWeights() { try { localStorage.setItem("vb-weights", JSON.stringify(state.weights)); } catch (e) {} }
function loadCompare() {
  try { return JSON.parse(localStorage.getItem("vb-compare") || "[]"); } catch (e) { return []; }
}
function saveCompare() { try { localStorage.setItem("vb-compare", JSON.stringify(state.compare)); } catch (e) {} }

/* ---------- URL routing (the URL is the source of truth for "where you were") ----------
   The view lives in the location hash — e.g. `#top?season=2026&scope=week&week=3&stat=kills&
   conf=Southeastern%20Conference&pos=OH`. A refresh re-reads it, so you land on the same tab with
   the same scope/week/filters (and the same open player/team). Navigation between tabs and detail
   pages goes through history.pushState, so the browser Back/Forward buttons and the in-app "← Back"
   links all step through real history. Filter tweaks use replaceState (they update the current
   entry rather than pile up history). pushState/replaceState never fire popstate/hashchange, so
   there's no sync loop; we re-read the URL only on the user's Back/Forward (popstate). */
let historyDepth = 0;  // # of app-pushed entries deep; lets "← Back" fall back to a parent tab

// Serialize the current view to a hash string, including only the params that matter for the tab.
function viewToHash() {
  const s = state;
  const cur = state.filters[s.tab];  // undefined for compare/player (no filters)
  const p = new URLSearchParams();
  if (s.season != null) p.set("season", s.season);
  if (cur && cur.scope === "week") { p.set("scope", "week"); if (cur.week) p.set("week", cur.week); }
  if (s.tab === "top") {
    p.set("stat", cur.stat);
    if (cur.conf) p.set("conf", cur.conf);
    if (cur.pos) p.set("pos", cur.pos);
    if (cur.min != null) p.set("min", cur.min);
  } else if (s.tab === "fantasy") {
    if (cur.conf) p.set("conf", cur.conf);
    if (cur.pos) p.set("pos", cur.pos);
  } else if (s.tab === "teams" || s.tab === "waiver") {
    if (cur.conf) p.set("conf", cur.conf);
  } else if (s.tab === "player") {
    if (s.playerId != null) p.set("pid", s.playerId);
  } else if (s.tab === "team") {
    if (s.teamId != null) p.set("tid", s.teamId);
    if (s.teamName) p.set("tname", s.teamName);
  }
  const q = p.toString();
  return "#" + s.tab + (q ? "?" + q : "");
}

// Parse the current hash into state, validating away anything stale (a season/conference that no
// longer exists, a detail tab with no id). The week is left for refreshWeeks() to validate.
const TABS = ["top", "fantasy", "teams", "waiver", "compare", "player", "team"];
function applyHash() {
  const h = location.hash.replace(/^#/, "");
  const qi = h.indexOf("?");
  const tab = (qi >= 0 ? h.slice(0, qi) : h) || "top";
  const p = new URLSearchParams(qi >= 0 ? h.slice(qi + 1) : "");
  state.tab = TABS.includes(tab) ? tab : "top";
  const cur = state.filters[state.tab];  // undefined for compare/player (no filters)

  const seasonRaw = p.get("season");
  if (seasonRaw != null && state.seasons.some((x) => String(x) === seasonRaw)) {
    state.season = typeof state.seasons[0] === "number" ? Number(seasonRaw) : seasonRaw;
  }
  if (cur) {
    cur.scope = p.get("scope") === "week" ? "week" : "season";
    const wk = p.get("week");
    if (wk != null) cur.week = wk;  // validated against the season's weeks by refreshWeeks()
    const stat = p.get("stat");
    if (stat && STATS.some((x) => x.key === stat)) cur.stat = stat;
    const conf = p.get("conf");
    cur.conf = conf && state.conferences.some((c) => c.name === conf) ? conf : "";
    cur.pos = p.get("pos") || "";
    const min = p.get("min");
    cur.min = min != null && min !== "" ? Number(min) : null;
  }
  const pid = p.get("pid"); if (pid != null) state.playerId = pid;
  const tid = p.get("tid"); if (tid != null) state.teamId = tid;
  const tname = p.get("tname"); if (tname != null) state.teamName = tname;

  if ((state.tab === "player" && state.playerId == null) ||
      (state.tab === "team" && state.teamId == null)) {
    state.tab = "top";
  }
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === state.tab));
}

// Push a new history entry for the current view (used for tab switches and opening a detail page).
function navigate() {
  historyDepth += 1;
  history.pushState({ depth: historyDepth }, "", viewToHash());
}
// Update the current history entry's URL in place (used by renders after a filter change).
function replaceURL() {
  history.replaceState(history.state, "", viewToHash());
}
// In-app "← Back": use real history when we're deeper than the entry point, else the parent tab.
function goBack(fallbackTab) {
  if (historyDepth > 0) history.back();
  else setTab(fallbackTab);
}

// Non-default weight overrides -> w_<stat> query params.
function weightParams() {
  const p = {};
  for (const k in state.weights) {
    if (state.weights[k] !== DEFAULT_WEIGHTS[k]) p["w_" + k] = state.weights[k];
  }
  return p;
}

const scopeParams = () => {
  const cur = f();
  const p = { season: state.season, scope: cur.scope };
  if (cur.scope === "week") p.week = cur.week;
  return p;
};

/* ---------- boot ---------- */
async function boot() {
  wireTopbar();
  wireTabs();
  wireSearch();
  try {
    const [seasons, confs] = await Promise.all([api("/seasons"), api("/conferences")]);
    state.seasons = seasons.length ? seasons : [new Date().getFullYear()];
    state.conferences = confs;
    state.season = state.seasons[0];
  } catch (e) {
    toast("Failed to load metadata: " + e.message, true);
    state.seasons = [new Date().getFullYear()];
    state.season = state.seasons[0];
  }
  applyHash();  // parse the initial URL into state (validated against the loaded metadata)
  populateSeasons();
  await refreshWeeks();  // validates each tab's selected week against the season's weeks
  history.replaceState({ depth: 0 }, "", viewToHash());  // normalize the entry-point URL
  // Back/Forward: re-read the URL and re-render. render() replaceStates the same entry (harmless).
  window.addEventListener("popstate", (e) => {
    historyDepth = e.state && typeof e.state.depth === "number" ? e.state.depth : 0;
    applyHash();
    render();
  });
  render();
}

function populateSeasons() {
  const sel = clear($("#season-select"));
  state.seasons.forEach((s) => sel.appendChild(el("option", { value: s, text: String(s) })));
  sel.value = state.season;
}

async function refreshWeeks() {
  try {
    state.weeks = await api("/weeks", { season: state.season });
  } catch (e) {
    state.weeks = [];
  }
  // Week lives per-tab now; keep every slice's selected week valid for the current season, defaulting
  // to the most recent numbered week (weeks are ascending). The week dropdown itself is built per
  // screen from state.weeks at render time.
  const numbered = state.weeks.filter((w) => w.week_number != null);
  const latest = numbered.length ? numbered[numbered.length - 1].week_number : "";
  for (const k in state.filters) {
    const fl = state.filters[k];
    if (!fl.week || !state.weeks.some((w) => String(w.week_number) === String(fl.week))) {
      fl.week = latest;
    }
  }
}

/* ---------- topbar wiring ---------- */
function wireTopbar() {
  $("#theme-toggle").addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
    const next = cur === "light" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("vb-theme", next); } catch (e) {}
  });
  $("#season-select").addEventListener("change", async (e) => {
    state.season = Number(e.target.value);
    await refreshWeeks();
    render();
  });
}

function wireTabs() {
  $("#tabs").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-tab]");
    if (!btn) return;
    setTab(btn.dataset.tab);
  });
}

function setTab(tab) {
  state.tab = tab;
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  navigate();  // a tab switch is a new history entry
  render();
}
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

/* ---------- search ---------- */
let searchTimer = null;
function wireSearch() {
  const input = $("#search-input");
  const box = $("#search-results");
  input.addEventListener("input", () => {
    clearTimeout(searchTimer);
    const q = input.value.trim();
    if (q.length < 2) { box.hidden = true; return; }
    searchTimer = setTimeout(() => runSearch(q), 200);
  });
  input.addEventListener("focus", () => { if (box.children.length) box.hidden = false; });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".search")) box.hidden = true;
  });
}

async function runSearch(q) {
  const box = $("#search-results");
  try {
    const res = await api("/search", { q, season: state.season });
    clear(box);
    if (!res.players.length && !res.teams.length) {
      box.appendChild(el("div", { class: "empty", text: `No matches for “${q}”` }));
    } else {
      if (res.players.length) {
        box.appendChild(el("div", { class: "group-label", text: "Players" }));
        res.players.forEach((p) => box.appendChild(el("div", {
          class: "item",
          onclick: () => { box.hidden = true; $("#search-input").value = ""; openPlayer(p.id); },
        }, [
          el("span", {}, p.name),
          el("span", { class: "sub" }, [(p.team_short || p.team) || "", p.position ? " · " + p.position : ""].join("")),
        ])));
      }
      if (res.teams.length) {
        box.appendChild(el("div", { class: "group-label", text: "Teams" }));
        res.teams.forEach((t) => box.appendChild(el("div", {
          class: "item",
          onclick: () => { box.hidden = true; $("#search-input").value = ""; openTeam(t.id, t.short_name || t.name); },
        }, [
          el("span", {}, t.short_name || t.name),
          el("span", { class: "sub" }, t.conference || ""),
        ])));
      }
    }
    box.hidden = false;
  } catch (e) {
    toast("Search failed: " + e.message, true);
  }
}

/* ---------- render dispatch ---------- */
function render() {
  const v = clear($("#view"));
  const map = {
    top: renderTop, fantasy: renderFantasy, teams: renderTeams,
    waiver: renderWaiver, compare: renderCompare,
    player: renderPlayer, team: renderTeamDetail,
  };
  (map[state.tab] || renderTop)(v);
}

function spinner(root) { root.appendChild(el("div", { class: "spinner", text: "Loading…" })); }
function emptyState(root, msg) { root.appendChild(el("div", { class: "empty-state", text: msg })); }

/* Editable short name for a conference, sourced from conferences.short_name in the DB. Every
   conference is seeded with one (e.g. "Big Ten", "SEC", "Pac-12"), so this is the single source
   of truth for the short label — the front-end no longer trims names at display time. */
function confAbbr(name) {
  const c = state.conferences.find((x) => x.name === name);
  return (c && c.short_name) || null;
}

/* Short form for dropdowns: the DB short_name; the full name only as a fallback if a conference
   somehow has no short_name yet. */
function confShort(name) {
  return confAbbr(name) || name;
}

/* Group header: the DB short_name, expanded to "Full Name (ABBR)" when the short form is a distinct
   abbreviation (i.e. not contained in the full name, like "SEC" for "Southeastern Conference"); the
   plain short form otherwise (e.g. "Big Ten"). */
function confHeader(name) {
  const s = confAbbr(name);
  if (!s) return name;
  return name.includes(s) ? s : `${name} (${s})`;
}

/* Filters shared by leaderboard-style views. */
function confSelect(value, onchange) {
  const sel = el("select", { onchange: (e) => onchange(e.target.value) });
  sel.appendChild(el("option", { value: "", text: "All conferences" }));
  state.conferences.forEach((c) => sel.appendChild(el("option", { value: c.name, text: confShort(c.name) })));
  sel.value = value || "";
  return sel;
}
function posSelect(value, onchange) {
  const sel = el("select", { onchange: (e) => onchange(e.target.value) });
  [["", "All positions"], ["OH", "OH"], ["MB", "MB"], ["S", "Setter"], ["OPP", "Opposite"],
   ["L", "Libero"], ["DS", "DS"]].forEach(([v, l]) =>
    sel.appendChild(el("option", { value: v, text: l })));
  sel.value = value || "";
  return sel;
}
function field(labelText, control) {
  return el("label", { class: "field" }, [el("span", { text: labelText }), control]);
}
function scopeLabel() {
  const cur = f();
  if (cur.scope === "week") {
    const w = state.weeks.find((x) => String(x.week_number) === String(cur.week));
    return w ? `Week ${w.week_number}` : "Week";
  }
  return `${state.season} season`;
}

/* Scope (Season/Week) toggle plus, when Week is selected, the week dropdown — as filter fields for
   the screens that aggregate by scope (Stat Leaders, Fantasy, team detail). Returns an array of
   fields; changing either re-renders via rerender(). The week dropdown is absent under Season scope. */
function scopeFields(rerender) {
  const cur = f();
  const scopeSel = el("select", { onchange: (e) => { cur.scope = e.target.value; rerender(); } });
  [["season", "Season"], ["week", "Week"]].forEach(([v, l]) =>
    scopeSel.appendChild(el("option", { value: v, text: l })));
  scopeSel.value = cur.scope;
  const fields = [field("Scope", scopeSel)];
  if (cur.scope === "week") {
    const weekSel = el("select", { onchange: (e) => { cur.week = e.target.value; rerender(); } });
    state.weeks.filter((w) => w.week_number != null).forEach((w) =>
      weekSel.appendChild(el("option", {
        value: w.week_number,
        text: `Wk ${w.week_number} (${w.start ? w.start.slice(5) : "?"}–${w.end ? w.end.slice(5) : "?"})`,
      })));
    if (cur.week) weekSel.value = cur.week;
    fields.push(field("Week", weekSel));
  }
  return fields;
}

/* ---------- leaderboard table ---------- */
function leaderTable(rows, statKey) {
  const m = statMeta(statKey);
  const table = el("table");
  table.appendChild(el("thead", {}, el("tr", {}, [
    el("th", { text: "#" }),
    el("th", { class: "l", text: "Player" }),
    el("th", { class: "l", text: "Team" }),
    el("th", { class: "l", text: "Conf" }),
    el("th", { text: "GP" }),
    el("th", { text: "Sets" }),
    el("th", { class: "sorted", text: m.label }),
  ])));
  const tb = el("tbody");
  rows.forEach((r, i) => {
    tb.appendChild(el("tr", {}, [
      el("td", { text: i + 1 }),
      el("td", { class: "l" }, el("a", { class: "link", onclick: () => openPlayer(r.player_id) },
        [r.name, r.position ? el("span", { class: "pos-tag", text: r.position }) : null])),
      el("td", { class: "l" }, r.team_id
        ? el("a", { class: "link", onclick: () => openTeam(r.team_id, r.team_short || r.team) }, (r.team_short || r.team) || "—")
        : ((r.team_short || r.team) || "—")),
      el("td", { class: "l muted", text: r.conference || "—" }),
      el("td", { class: "num", text: fmtInt(r.games) }),
      el("td", { class: "num", text: fmt(r.sets, 0) }),
      el("td", { class: "num", text: fmt(r.value, m.d) }),
    ]));
  });
  table.appendChild(tb);
  return table;
}

/* ---------- Top Players ---------- */
async function renderTop(root) {
  replaceURL();
  const cur = f();
  const statSel = el("select", { onchange: (e) => {
    cur.stat = e.target.value;
    cur.min = null;  // reset to the new stat's default qualifier
    renderTop(clear(root));
  } });
  STATS.forEach((s) => statSel.appendChild(el("option", { value: s.key, text: s.label })));
  statSel.value = cur.stat;

  const qual = activeQualifier();
  const filters = [
    ...scopeFields(() => renderTop(clear(root))),
    field("Stat", statSel),
    field("Conference", confSelect(cur.conf, (v) => { cur.conf = v; renderTop(clear(root)); })),
    field("Position", posSelect(cur.pos, (v) => { cur.pos = v; renderTop(clear(root)); })),
  ];
  if (qual) {
    const minInp = el("input", {
      type: "number", min: 0, step: 1, value: qual.val, style: "width:80px",
      title: `Minimum ${qual.by} to qualify (default ${qual.def})`,
      onchange: (e) => { cur.min = Math.max(0, Number(e.target.value) || 0); renderTop(clear(root)); },
    });
    filters.push(field(qual.label, minInp));
  }

  root.appendChild(el("div", { class: "view-head" }, [
    el("h1", { text: "Stat Leaders" }),
    el("div", { class: "spacer" }),
    el("div", { class: "filters" }, filters),
  ]));

  const card = el("div", { class: "card" }, el("div", { class: "card-title" }, [
    statMeta(cur.stat).label + " leaders",
    el("span", { class: "badge", text: scopeLabel() }),
  ]));
  root.appendChild(card);
  const body = el("div"); card.appendChild(body); spinner(body);

  try {
    const params = Object.assign(scopeParams(), {
      stat: cur.stat, conference: cur.conf, position: cur.pos, limit: 200,
    });
    if (qual) params[qual.by === "attacks" ? "min_attacks" : "min_sets"] = qual.val;
    const rows = await api("/leaderboards", params);
    clear(body);
    if (!rows.length) emptyState(body, "No data for this selection.");
    else body.appendChild(leaderTable(rows, cur.stat));
  } catch (e) {
    clear(body); emptyState(body, "Error: " + e.message);
  }
}

/* ---------- Fantasy ---------- */
function weightsPanel(onApply) {
  const wrap = el("details", { class: "weights-wrap" });
  wrap.appendChild(el("summary", { text: "Fantasy scoring weights" }));
  const panel = el("div", { class: "panel" });
  const grid = el("div", { class: "weights" });
  const inputs = {};
  Object.keys(DEFAULT_WEIGHTS).forEach((k) => {
    const inp = el("input", { type: "number", step: 0.25, value: state.weights[k] });
    inputs[k] = inp;
    grid.appendChild(el("label", {}, [WEIGHT_LABELS[k] || k, inp]));
  });
  panel.appendChild(grid);
  panel.appendChild(el("div", { class: "weights-actions" }, [
    el("button", {
      class: "btn",
      onclick: () => {
        Object.keys(inputs).forEach((k) => {
          const v = parseFloat(inputs[k].value);
          state.weights[k] = isNaN(v) ? DEFAULT_WEIGHTS[k] : Math.max(-10, Math.min(10, v));
        });
        saveWeights();
        onApply();
      },
    }, "Apply"),
    el("button", {
      class: "btn ghost",
      onclick: () => {
        state.weights = Object.assign({}, DEFAULT_WEIGHTS);
        saveWeights();
        onApply();
      },
    }, "Reset to defaults"),
  ]));
  wrap.appendChild(panel);
  return wrap;
}

async function renderFantasy(root) {
  replaceURL();
  const cur = f();
  root.appendChild(el("div", { class: "view-head" }, [
    el("h1", { text: "Fantasy Points" }),
    el("div", { class: "spacer" }),
    el("div", { class: "filters" }, [
      ...scopeFields(() => renderFantasy(clear(root))),
      field("Conference", confSelect(cur.conf, (v) => { cur.conf = v; renderFantasy(clear(root)); })),
      field("Position", posSelect(cur.pos, (v) => { cur.pos = v; renderFantasy(clear(root)); })),
    ]),
  ]));
  root.appendChild(weightsPanel(() => renderFantasy(clear(root))));

  const card = el("div", { class: "card" }, el("div", { class: "card-title" }, [
    "Fantasy leaders", el("span", { class: "badge", text: scopeLabel() }),
  ]));
  root.appendChild(card);
  const body = el("div"); card.appendChild(body); spinner(body);

  try {
    const rows = await api("/leaderboards/fantasy", Object.assign(
      scopeParams(), weightParams(),
      { conference: cur.conf, position: cur.pos, min_sets: state.minSets, limit: 200 }
    ));
    clear(body);
    if (!rows.length) { emptyState(body, "No data for this selection."); return; }
    const table = el("table");
    table.appendChild(el("thead", {}, el("tr", {}, [
      el("th", { text: "#" }), el("th", { class: "l", text: "Player" }),
      el("th", { class: "l", text: "Team" }), el("th", { class: "l", text: "Conf" }),
      el("th", { text: "GP" }), el("th", { text: "Sets" }),
      el("th", { text: "FP" }), el("th", { text: "FP/set" }),
    ])));
    const tb = el("tbody");
    rows.forEach((r, i) => {
      const fpps = r.sets ? r.value / r.sets : null;
      tb.appendChild(el("tr", {}, [
        el("td", { text: i + 1 }),
        el("td", { class: "l" }, el("a", { class: "link", onclick: () => openPlayer(r.player_id) },
          [r.name, r.position ? el("span", { class: "pos-tag", text: r.position }) : null])),
        el("td", { class: "l" }, r.team_id
          ? el("a", { class: "link", onclick: () => openTeam(r.team_id, r.team_short || r.team) }, (r.team_short || r.team) || "—")
          : ((r.team_short || r.team) || "—")),
        el("td", { class: "l muted", text: r.conference || "—" }),
        el("td", { class: "num", text: fmtInt(r.games) }),
        el("td", { class: "num", text: fmt(r.sets, 0) }),
        el("td", { class: "num", text: fmt(r.value, 1) }),
        el("td", { class: "num", text: fmt(fpps, 2) }),
      ]));
    });
    table.appendChild(tb);
    body.appendChild(table);
  } catch (e) {
    clear(body); emptyState(body, "Error: " + e.message);
  }
}

/* ---------- Teams (records / standings, by conference) ---------- */
function rec(w, l) { return `${w || 0}-${l || 0}`; }

function streakText(s) {
  if (!s) return "—";
  return (s > 0 ? "W" : "L") + Math.abs(s);
}

// RPI rank, flagged with the prior season when the record shows more games than played this
// season (the NCAA RPI table still reflects last year until ~late September).
function rpiText(r) {
  if (r.rpi_rank == null) return "—";
  let stale = false;
  const m = r.rpi_record && r.rpi_record.match(/(\d+)\s*-\s*(\d+)/);
  if (m && (+m[1] + +m[2]) > (r.games || 0)) stale = true;
  return "#" + r.rpi_rank + (stale ? ` (${state.season - 1})` : "");
}

async function renderTeams(root) {
  replaceURL();
  const cur = f();
  root.appendChild(el("div", { class: "view-head" }, [
    el("h1", { text: "Teams" }),
    el("div", { class: "spacer" }),
    el("div", { class: "filters" }, [
      field("Conference", confSelect(cur.conf, (v) => { cur.conf = v; renderTeams(clear(root)); })),
    ]),
  ]));
  const holder = el("div"); root.appendChild(holder); spinner(holder);

  try {
    const rows = await api("/team-records", { season: state.season, conference: cur.conf });
    clear(holder);
    if (!rows.length) { emptyState(holder, "No results recorded yet for this selection."); return; }
    // group by conference
    const groups = {};
    rows.forEach((r) => { (groups[r.conference || "Independent"] ||= []).push(r); });
    Object.keys(groups).sort().forEach((conf) => {
      const card = el("div", { class: "card conf-group" });
      card.appendChild(el("div", { class: "card-title" }, [
        confHeader(conf), el("span", { class: "badge", text: `${groups[conf].length} teams` }),
      ]));
      const table = el("table");
      table.appendChild(el("thead", {}, el("tr", {}, [
        el("th", { class: "l", text: "Team" }), el("th", { text: "GP" }),
        el("th", { text: "W" }), el("th", { text: "L" }), el("th", { text: "Set%" }),
        el("th", { text: "Strk" }), el("th", { text: "Conf" }), el("th", { text: "Non-Conf" }),
        el("th", { text: "Opp Rec" }), el("th", { text: "RPI" }), el("th", { text: "Opp RPI" }),
      ])));
      const tb = el("tbody");
      groups[conf]
        .sort((a, b) => (b.wins - a.wins) || (a.losses - b.losses) || ((b.set_pct || 0) - (a.set_pct || 0)))
        .forEach((r) => {
          tb.appendChild(el("tr", {}, [
            el("td", { class: "l" }, el("a", { class: "link", onclick: () => openTeam(r.team_id, r.team_short || r.team) }, r.team_short || r.team)),
            el("td", { class: "num", text: fmtInt(r.games) }),
            el("td", { class: "num", text: fmtInt(r.wins) }),
            el("td", { class: "num", text: fmtInt(r.losses) }),
            el("td", { class: "num", text: r.set_pct == null ? "—" : (r.set_pct * 100).toFixed(1) + "%" }),
            el("td", { class: "num", text: streakText(r.win_streak) }),
            el("td", { class: "num", text: rec(r.conf_wins, r.conf_losses) }),
            el("td", { class: "num", text: rec(r.nonconf_wins, r.nonconf_losses) }),
            el("td", { class: "num", text: rec(r.opp_wins, r.opp_losses) }),
            el("td", { class: "num", text: rpiText(r) }),
            el("td", { class: "num", text: r.opp_rpi == null ? "—" : String(r.opp_rpi) }),
          ]));
        });
      table.appendChild(tb);
      card.appendChild(table);
      root.appendChild(card);
    });
  } catch (e) {
    clear(holder); emptyState(holder, "Error: " + e.message);
  }
}

/* ---------- Leaderboard (top performers by category, season or week) ---------- */
async function renderWaiver(root) {
  replaceURL();
  const cur = f();
  root.appendChild(el("div", { class: "view-head" }, [
    el("h1", { text: "Leaderboard" }),
    el("div", { class: "spacer" }),
    el("div", { class: "filters" }, [
      ...scopeFields(() => renderWaiver(clear(root))),
      field("Conference", confSelect(cur.conf, (v) => { cur.conf = v; renderWaiver(clear(root)); })),
    ]),
  ]));

  if (cur.scope === "week" && !state.weeks.some((w) => w.week_number != null)) {
    emptyState(root, "No weeks available for this season yet."); return;
  }

  const grid = el("div"); root.appendChild(grid);
  const cats = [
    { stat: "kills", label: "Kills" }, { stat: "assists", label: "Assists" },
    { stat: "digs", label: "Digs" }, { stat: "aces", label: "Aces" },
    { stat: "total_blocks", label: "Blocks" }, { stat: "pts", label: "Points" },
  ];
  const badge = scopeLabel();

  // Fantasy card first.
  const fpCard = el("div", { class: "card" }, el("div", { class: "card-title" }, [
    "Fantasy leaders", el("span", { class: "badge", text: badge }),
  ]));
  const fpBody = el("div"); fpCard.appendChild(fpBody); spinner(fpBody); grid.appendChild(fpCard);

  try {
    const rows = await api("/leaderboards/fantasy", Object.assign(
      scopeParams(), { conference: cur.conf, limit: 15 }, weightParams()
    ));
    clear(fpBody);
    fpBody.appendChild(miniLeaderTable(rows, (r) => fmt(r.value, 1)));
  } catch (e) { clear(fpBody); emptyState(fpBody, "Error: " + e.message); }

  for (const c of cats) {
    const card = el("div", { class: "card" }, el("div", { class: "card-title" }, [
      c.label, el("span", { class: "badge", text: badge }),
    ]));
    const body = el("div"); card.appendChild(body); spinner(body); grid.appendChild(card);
    try {
      const rows = await api("/leaderboards", Object.assign(scopeParams(), {
        stat: c.stat, conference: cur.conf, limit: 10,
      }));
      clear(body);
      body.appendChild(miniLeaderTable(rows, (r) => fmtInt(r.value)));
    } catch (e) { clear(body); emptyState(body, "Error: " + e.message); }
  }
}

function miniLeaderTable(rows, valFn) {
  if (!rows.length) return el("div", { class: "empty-state", text: "No data." });
  const table = el("table");
  const tb = el("tbody");
  rows.forEach((r, i) => {
    tb.appendChild(el("tr", {}, [
      el("td", { text: i + 1 }),
      el("td", { class: "l" }, el("a", { class: "link", onclick: () => openPlayer(r.player_id) }, r.name)),
      el("td", { class: "l muted", text: (r.team_short || r.team) || "—" }),
      el("td", { class: "num", text: valFn(r) }),
    ]));
  });
  table.appendChild(tb);
  return table;
}

/* ---------- Compare ---------- */
const COMPARE_MAX = 3;

// A filled slot: the player's name with a remove button.
function comparePlayerCard(c, root) {
  return el("div", { class: "compare-card" }, [
    el("button", {
      class: "compare-remove", title: "Remove",
      onclick: () => {
        state.compare = state.compare.filter((x) => x.id !== c.id);
        saveCompare();
        renderCompare(clear(root));
      },
    }, "×"),
    el("div", { class: "compare-card-name" },
      el("a", { class: "link", onclick: () => openPlayer(c.id) }, c.name)),
    el("div", { class: "muted", text: c.team || "" }),
  ]);
}

// The empty "add player" slot: an inline search that adds the picked player to the comparison.
function addPlayerCard(root) {
  const card = el("div", { class: "compare-card add" });
  card.appendChild(el("div", { class: "compare-card-name muted", text: "＋ Add player" }));
  const input = el("input", { class: "compare-search", type: "text", placeholder: "Search player…" });
  const results = el("div", { class: "compare-results" });
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { clear(results); return; }
    timer = setTimeout(async () => {
      try {
        const res = await api("/search", { q, season: state.season });
        clear(results);
        const players = res.players
          .filter((p) => !state.compare.some((c) => c.id === p.id))
          .slice(0, 8);
        if (!players.length) { results.appendChild(el("div", { class: "muted", text: "No players" })); return; }
        players.forEach((p) => results.appendChild(el("div", {
          class: "compare-result",
          onclick: () => { addToCompare(p.id, p.name, p.team_short || p.team); renderCompare(clear(root)); },
        }, [
          el("span", {}, p.name),
          el("span", { class: "sub", text: [(p.team_short || p.team), p.position].filter(Boolean).join(" · ") }),
        ])));
      } catch (e) {
        clear(results);
        results.appendChild(el("div", { class: "muted", text: "Search failed" }));
      }
    }, 200);
  });
  card.appendChild(input);
  card.appendChild(results);
  return card;
}

async function renderCompare(root) {
  replaceURL();
  root.appendChild(el("div", { class: "view-head" }, [
    el("h1", { text: "Compare Players" }),
    el("div", { class: "spacer" }),
    el("span", { class: "muted", text: `Compare up to ${COMPARE_MAX} players` }),
  ]));

  // Inline player slots: a card per added player + an "add player" search card (until full).
  const slots = el("div", { class: "compare-slots" });
  state.compare.forEach((c) => slots.appendChild(comparePlayerCard(c, root)));
  if (state.compare.length < COMPARE_MAX) slots.appendChild(addPlayerCard(root));
  root.appendChild(slots);

  if (!state.compare.length) return;  // add card is shown above; nothing to tabulate yet

  const card = el("div", { class: "card" });
  card.appendChild(el("div", { class: "card-title", text: `Season ${state.season} — per-set rates` }));
  const body = el("div"); card.appendChild(body); spinner(body); root.appendChild(card);

  try {
    const stats = await Promise.all(state.compare.map((c) =>
      api(`/players/${c.id}/season-stats`, { season: state.season }).catch(() => null)));
    clear(body);
    const rowsDef = [
      ["GP", (s) => fmtInt(s.gp)], ["Sets", (s) => fmt(s.sp, 0)],
      ["Kills", (s) => fmtInt(s.kills)], ["K/set", (s) => fmt(s.kills_per_set, 2)],
      ["Assists", (s) => fmtInt(s.assists)], ["A/set", (s) => fmt(s.assists_per_set, 2)],
      ["Digs", (s) => fmtInt(s.digs)], ["D/set", (s) => fmt(s.digs_per_set, 2)],
      ["Aces", (s) => fmtInt(s.aces)], ["Blocks", (s) => fmt(s.total_blocks, 0)],
      ["Points", (s) => fmt(s.pts, 1)], ["Hit %", (s) => fmt(s.hit_pct, 3)],
    ];
    const table = el("table");
    const head = el("tr", {}, [el("th", { class: "l", text: "Stat" })]);
    state.compare.forEach((c, i) => head.appendChild(el("th", { text: stats[i] ? c.name : c.name + " (n/a)" })));
    table.appendChild(el("thead", {}, head));
    const tb = el("tbody");
    rowsDef.forEach(([label, fn]) => {
      const tr = el("tr", {}, [el("td", { class: "l", text: label })]);
      state.compare.forEach((c, i) => tr.appendChild(el("td", { class: "num", text: stats[i] ? fn(stats[i]) : "—" })));
      tb.appendChild(tr);
    });
    table.appendChild(tb);
    body.appendChild(table);
  } catch (e) { clear(body); emptyState(body, "Error: " + e.message); }
}

function addToCompare(id, name, team) {
  if (state.compare.some((c) => c.id === id)) { toast(`${name} already in compare`); return; }
  if (state.compare.length >= COMPARE_MAX) { toast(`Compare holds up to ${COMPARE_MAX} players`, true); return; }
  state.compare.push({ id, name, team: team || null });
  saveCompare();
  toast(`Added ${name} to compare`);
}

/* ---------- Player detail ---------- */
async function openPlayer(id) {
  state.playerId = id;
  setTab("player");
}

async function renderPlayer(root) {
  replaceURL();
  const id = state.playerId;
  root.appendChild(el("div", { class: "back-link" },
    el("a", { class: "link", onclick: () => goBack("top") }, "← Back")));
  const holder = el("div"); root.appendChild(holder); spinner(holder);

  try {
    const [p, ss, log] = await Promise.all([
      api(`/players/${id}`),
      api(`/players/${id}/season-stats`, { season: state.season }).catch(() => null),
      api(`/players/${id}/game-log`, { season: state.season }).catch(() => []),
    ]);
    clear(holder);

    const meta = [p.position, p.class_year, heightStr(p.height_inches), p.hometown].filter(Boolean).join(" · ");
    holder.appendChild(el("div", { class: "player-head" }, [
      el("h1", { text: p.name }),
      p.team_id ? el("a", { class: "link", onclick: () => openTeam(p.team_id, p.team_short || p.team) }, (p.team_short || p.team) || "") : el("span", { text: (p.team_short || p.team) || "" }),
      el("span", { class: "meta", text: meta }),
      el("div", { class: "spacer", style: "flex:1" }),
      el("button", { class: "btn ghost", onclick: () => addToCompare(p.id, p.name) }, "＋ Compare"),
    ]));

    if (ss) {
      const fp = fantasyOf(ss);
      const boxes = [
        ["Fantasy Pts", fmt(fp, 1), true], ["FP/set", fmt(ss.sp ? fp / ss.sp : null, 2), true],
        ["GP", fmtInt(ss.gp)], ["Sets", fmt(ss.sp, 0)],
        ["Kills", fmtInt(ss.kills)], ["K/set", fmt(ss.kills_per_set, 2)],
        ["Assists", fmtInt(ss.assists)], ["A/set", fmt(ss.assists_per_set, 2)],
        ["Digs", fmtInt(ss.digs)], ["D/set", fmt(ss.digs_per_set, 2)],
        ["Aces", fmtInt(ss.aces)], ["Blocks", fmt(ss.total_blocks, 0)],
        ["Points", fmt(ss.pts, 1)], ["Hit %", fmt(ss.hit_pct, 3)],
      ];
      const grid = el("div", { class: "statline" });
      boxes.forEach(([k, v, isFp]) => grid.appendChild(el("div", { class: "stat-box" }, [
        el("div", { class: "k", text: k }),
        el("div", { class: "v" + (isFp ? " fp" : ""), text: v }),
      ])));
      holder.appendChild(grid);
    } else {
      holder.appendChild(el("div", { class: "muted", style: "margin:16px 0", text: "No derived season stats for this player/season." }));
    }

    // Game log — every stat column, horizontally scrollable (mirrors the team table).
    const card = el("div", { class: "card" });
    card.appendChild(el("div", { class: "card-title" }, [
      "Game log", el("span", { class: "badge", text: "all stats · scroll sideways →" }),
    ]));
    if (!log.length) card.appendChild(el("div", { class: "empty-state", text: "No games recorded." }));
    else card.appendChild(gameLogTable(log, ss));
    holder.appendChild(card);
  } catch (e) {
    clear(holder); emptyState(holder, "Error: " + e.message);
  }
}

// Fantasy points from any row carrying the counting-stat keys (season line or game log),
// using the user's current weights so player detail matches the Fantasy tab site-wide.
function fantasyOf(stats) {
  let fp = 0;
  for (const k in state.weights) fp += state.weights[k] * (stats[k] || 0);
  return fp;
}

// Hit % from a row's raw kills/errors/attacks (game log rows don't carry a hit_pct field).
const hitPct = (r) => (r && r.total_attacks ? (r.kills - (r.errors || 0)) / r.total_attacks : null);

// Full per-game stat columns for the player game log (mirrors the team table's breadth).
// `calc` derives a value not present as a plain column.
const GAMELOG_COLS = [
  { key: "sets", label: "Sets", d: 0 },
  { key: "kills", label: "Kills", int: true }, { key: "errors", label: "Err", int: true },
  { key: "total_attacks", label: "TA", int: true },
  { key: "hit_pct", label: "Hit%", d: 3, calc: hitPct },
  { key: "assists", label: "Ast", int: true }, { key: "aces", label: "Ace", int: true },
  { key: "serr", label: "SE", int: true }, { key: "digs", label: "Dig", int: true },
  { key: "retatt", label: "Rec", int: true }, { key: "rerr", label: "RE", int: true },
  { key: "block_solos", label: "BS", int: true }, { key: "block_assists", label: "BA", int: true },
  { key: "total_blocks", label: "Blk", int: true }, { key: "berr", label: "BE", int: true },
  { key: "bhe", label: "BHE", int: true }, { key: "pts", label: "Pts", d: 1 },
  { key: "fantasy_points", label: "FP", d: 1, calc: fantasyOf },
];
function statCell(col, row) {
  const v = col.calc ? col.calc(row) : row[col.key];
  return el("td", { class: "num", text: col.int ? fmtInt(v) : fmt(v, col.d) });
}

// Wide, horizontally-scrolling game log with every stat column, plus a season-total footer row.
function gameLogTable(log, ss) {
  const table = el("table", { class: "wide-table" });
  const htr = el("tr", {}, [
    el("th", { class: "l sticky-col", text: "Opponent" }),
    el("th", { class: "l", text: "Wk" }), el("th", { class: "l", text: "Date" }),
  ]);
  GAMELOG_COLS.forEach((c) => htr.appendChild(el("th", { text: c.label })));
  table.appendChild(el("thead", {}, htr));

  const tb = el("tbody");
  log.forEach((g) => {
    const tr = el("tr", {}, [
      el("td", { class: "l sticky-col" }, g.opponent_id
        ? el("a", { class: "link", onclick: () => openTeam(g.opponent_id, g.opponent_short || g.opponent) }, (g.opponent_short || g.opponent) || "—")
        : ((g.opponent_short || g.opponent) || "—")),
      el("td", { class: "l muted", text: g.week_number == null ? "—" : g.week_number }),
      el("td", { class: "l muted", text: g.date ? g.date.slice(0, 10) : "—" }),
    ]);
    GAMELOG_COLS.forEach((c) => tr.appendChild(statCell(c, g)));
    tb.appendChild(tr);
  });
  // Season total from the derived line (which names sets `sp`, games `gp`).
  if (ss) {
    const total = Object.assign({}, ss, { sets: ss.sp });
    const tr = el("tr", { class: "total-row" }, [
      el("td", { class: "l sticky-col", text: "Season total" }),
      el("td", { class: "l muted", text: "" }), el("td", { class: "l muted", text: "" }),
    ]);
    GAMELOG_COLS.forEach((c) => tr.appendChild(statCell(c, total)));
    tb.appendChild(tr);
  }
  table.appendChild(tb);
  return el("div", { class: "table-scroll" }, table);
}

/* ---------- Team detail (roster) ---------- */
async function openTeam(id, name) {
  state.teamId = id;
  state.teamName = name;
  setTab("team");
}

// Full stat columns for the team roster table (label, decimals, integer display). FP last.
const TEAM_COLS = [
  { key: "games", label: "GP", int: true }, { key: "sets", label: "Sets", d: 0 },
  { key: "kills", label: "Kills", int: true }, { key: "errors", label: "Err", int: true },
  { key: "total_attacks", label: "TA", int: true }, { key: "hit_pct", label: "Hit%", d: 3 },
  { key: "assists", label: "Ast", int: true }, { key: "aces", label: "Ace", int: true },
  { key: "serr", label: "SE", int: true }, { key: "digs", label: "Dig", int: true },
  { key: "retatt", label: "Rec", int: true }, { key: "rerr", label: "RE", int: true },
  { key: "block_solos", label: "BS", int: true }, { key: "block_assists", label: "BA", int: true },
  { key: "total_blocks", label: "Blk", d: 1 }, { key: "berr", label: "BE", int: true },
  { key: "bhe", label: "BHE", int: true }, { key: "pts", label: "Pts", d: 1 },
  { key: "kills_per_set", label: "K/S", d: 2 }, { key: "assists_per_set", label: "A/S", d: 2 },
  { key: "aces_per_set", label: "SA/S", d: 2 }, { key: "digs_per_set", label: "D/S", d: 2 },
  { key: "blocks_per_set", label: "B/S", d: 2 }, { key: "pts_per_set", label: "P/S", d: 2 },
  { key: "fantasy_points", label: "FP", d: 1 },
];

async function renderTeamDetail(root) {
  replaceURL();
  const id = state.teamId;
  root.appendChild(el("div", { class: "back-link" },
    el("a", { class: "link", onclick: () => goBack("teams") }, "← Back")));

  // Heading shows the NCAA short name, with the full institution name as a subtitle. Fall back to
  // whatever label the caller passed while the fetch is in flight.
  const head = el("div", { class: "view-head" }, [
    el("h1", { text: state.teamName || "Team" }),
    el("div", { class: "spacer" }),
    el("span", { class: "muted", text: "Tap a column to sort · scroll table sideways →" }),
  ]);
  root.appendChild(head);
  api(`/teams/${id}`).then((t) => {
    clear(head);
    head.appendChild(el("div", { class: "team-title" }, [
      el("h1", { text: t.short_name || t.name }),
      t.short_name && t.name !== t.short_name
        ? el("span", { class: "team-fullname muted", text: t.name }) : null,
    ]));
    head.appendChild(el("div", { class: "spacer" }));
    head.appendChild(el("span", { class: "muted", text: "Tap a column to sort · scroll table sideways →" }));
  }).catch(() => {});

  root.appendChild(el("div", { class: "filters" }, scopeFields(() => renderTeamDetail(clear(root)))));

  const card = el("div", { class: "card" }, el("div", { class: "card-title" }, [
    "Player stats", el("span", { class: "badge", text: scopeLabel() }),
  ]));
  const body = el("div"); card.appendChild(body); spinner(body); root.appendChild(card);

  try {
    const rows = await api(`/teams/${id}/player-stats`, Object.assign(scopeParams(), weightParams()));
    clear(body);
    if (!rows.length) { emptyState(body, "No stats for this team in the selected scope."); return; }
    renderTeamTable(body, rows);
  } catch (e) { clear(body); emptyState(body, "Error: " + e.message); }
}

// Team cumulative line: counting stats sum across the roster; hit% is recomputed from the
// summed kills/errors/attempts; GP and sets take the roster max (a player who appears in every
// match reflects the team's games/sets — summing per-player GP/sets would be meaningless).
function teamTotals(rows) {
  const SUM = ["kills", "errors", "total_attacks", "assists", "aces", "serr", "digs", "retatt",
    "rerr", "block_solos", "block_assists", "total_blocks", "berr", "bhe", "pts", "fantasy_points"];
  const t = {};
  SUM.forEach((k) => {
    let any = false, s = 0;
    rows.forEach((r) => { if (r[k] != null) { any = true; s += Number(r[k]); } });
    t[k] = any ? s : null;
  });
  const maxOf = (k) => {
    const vals = rows.map((r) => r[k]).filter((v) => v != null).map(Number);
    return vals.length ? Math.max(...vals) : null;
  };
  t.games = maxOf("games");
  t.sets = maxOf("sets");
  t.hit_pct = t.total_attacks ? (t.kills - (t.errors || 0)) / t.total_attacks : null;
  // Per-set columns can't be summed — recompute from the team totals over team sets.
  const PER_SET = {
    kills_per_set: "kills", assists_per_set: "assists", aces_per_set: "aces",
    digs_per_set: "digs", blocks_per_set: "total_blocks", pts_per_set: "pts",
  };
  Object.entries(PER_SET).forEach(([k, base]) => {
    t[k] = t.sets ? (t[base] || 0) / t.sets : null;
  });
  return t;
}

function renderTeamTable(body, rows) {
  const sort = state.teamSort || { key: "fantasy_points", dir: -1 };
  const sorted = rows.slice().sort((a, b) => {
    // Players who haven't played (no games) always sink to the bottom, whatever the sort column.
    const as = a.games == null, bs = b.games == null;
    if (as !== bs) return as ? 1 : -1;
    const av = a[sort.key], bv = b[sort.key];
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    return sort.dir * (av < bv ? -1 : av > bv ? 1 : 0);
  });
  clear(body);
  const table = el("table", { class: "wide-table" });
  const htr = el("tr", {}, el("th", { class: "l sticky-col", text: "Player" }));
  TEAM_COLS.forEach((c) => htr.appendChild(el("th", {
    class: "sortable" + (sort.key === c.key ? " sorted" : ""),
    text: c.label,
    onclick: () => {
      state.teamSort = { key: c.key, dir: sort.key === c.key ? -sort.dir : -1 };
      renderTeamTable(body, rows);
    },
  })));
  table.appendChild(el("thead", {}, htr));
  const tb = el("tbody");
  sorted.forEach((r) => {
    const tr = el("tr", {}, el("td", { class: "l sticky-col" },
      el("a", { class: "link", onclick: () => openPlayer(r.player_id) },
        [r.name, r.position ? el("span", { class: "pos-tag", text: r.position }) : null])));
    TEAM_COLS.forEach((c) => tr.appendChild(el("td", {
      class: "num", text: c.int ? fmtInt(r[c.key]) : fmt(r[c.key], c.d),
    })));
    tb.appendChild(tr);
  });
  // Team cumulative totals footer.
  const totals = teamTotals(rows);
  const ttr = el("tr", { class: "total-row" },
    el("td", { class: "l sticky-col", text: "Team totals" }));
  TEAM_COLS.forEach((c) => ttr.appendChild(el("td", {
    class: "num", text: c.int ? fmtInt(totals[c.key]) : fmt(totals[c.key], c.d),
  })));
  tb.appendChild(ttr);
  table.appendChild(tb);
  body.appendChild(el("div", { class: "table-scroll" }, table));
}

/* ---------- go ---------- */
boot();
