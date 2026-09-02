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
// The single fetch choke point. Attaches the bearer token (when signed in) to every request; on a
// 401 from an authenticated call it drops us back to the logged-out state so a stale token can't
// wedge the UI.
function authHeaders(extra) {
  const h = Object.assign({ Accept: "application/json" }, extra || {});
  if (state.token) h.Authorization = "Bearer " + state.token;
  return h;
}

async function api(path, params) {
  const url = new URL(path, window.location.origin);
  if (params) for (const k in params) {
    const v = params[k];
    if (v != null && v !== "") url.searchParams.set(k, v);
  }
  const res = await fetch(url, { headers: authHeaders() });
  if (!res.ok) {
    if (res.status === 401 && state.token) onAuthExpired();
    let detail = res.statusText;
    try { const j = await res.json(); detail = j.detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.json();
}

// Short-lived in-memory GET cache for slow-changing, user-independent data (the scoreboard and
// team schedules). Keyed by full URL; entries expire after ttlMs so navigating back to a week or
// team is instant without a network round-trip, while a fresh session still sees current data.
const _getCache = new Map();
async function apiCached(path, params, ttlMs = 5 * 60 * 1000) {
  const url = new URL(path, window.location.origin);
  if (params) for (const k in params) {
    const v = params[k];
    if (v != null && v !== "") url.searchParams.set(k, v);
  }
  const key = url.toString();
  const hit = _getCache.get(key);
  if (hit && Date.now() - hit.t < ttlMs) return hit.data;
  const data = await api(path, params);
  _getCache.set(key, { t: Date.now(), data });
  return data;
}

// Write helper for POST/PATCH/DELETE with a JSON body. Returns parsed JSON, or null for 204.
async function req(method, path, body) {
  const opts = { method, headers: authHeaders(body != null ? { "Content-Type": "application/json" } : {}) };
  if (body != null) opts.body = JSON.stringify(body);
  const res = await fetch(new URL(path, window.location.origin), opts);
  if (!res.ok) {
    if (res.status === 401 && state.token) onAuthExpired();
    let detail = res.statusText;
    try { const j = await res.json(); detail = j.detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  try { return await res.json(); } catch (e) { return null; }
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
    games: defaultFilters(),
    waiver: defaultFilters(),
    team: defaultFilters(),
  },
  contestId: null,
  minSets: 0,
  weights: loadWeights(),
  compare: loadCompare(),
  // Auth / personalization. token+user drive the header and gated tabs; favorites is a Set of
  // "player:<id>" / "team:<id>" keys used to light up ★ markers across every screen.
  token: loadToken(),
  user: null,
  favorites: new Set(),
};

function loadToken() { try { return localStorage.getItem("vb-token") || null; } catch (e) { return null; } }
function saveToken(t) {
  state.token = t || null;
  try { t ? localStorage.setItem("vb-token", t) : localStorage.removeItem("vb-token"); } catch (e) {}
}
const favKey = (type, id) => `${type}:${id}`;
const isFav = (type, id) => state.favorites.has(favKey(type, id));

/* ---------- fantasy opt-in (per-user; off by default) ----------
   Fantasy is invisible until a signed-in user opts in. The choice lives in User.prefs.fantasy
   (true / false / absent) and round-trips through /auth/me. Absent = never asked (we prompt on
   first sign-in, and treat as off meanwhile); false = declined/off; true = on. Anonymous visitors
   are always off and never prompted — the prompt is a post-sign-in event. */
function fantasyEnabled() {
  return !!(state.user && state.user.prefs && state.user.prefs.fantasy === true);
}
function fantasyDecided() {
  return !!(state.user && typeof (state.user.prefs || {}).fantasy === "boolean");
}
async function setFantasy(on) {
  if (!state.user) return;
  const prefs = Object.assign({}, state.user.prefs || {}, { fantasy: !!on });
  state.user.prefs = prefs;                       // optimistic; the PATCH persists it server-side
  req("PATCH", "/auth/me", { prefs }).catch(() => {});
  updateTabVisibility();
  if (!on && state.tab === "fantasy") setTab("top");  // don't strand the user on a now-hidden tab
  else render();
}

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
// Weights are per-user when signed in (persisted server-side via PATCH /me) and per-browser when
// anonymous (localStorage). Keep the two stores separate so a logged-in user's tuning never leaks
// into the logged-out experience, and vice-versa.
function saveWeights() {
  if (state.user) {
    req("PATCH", "/auth/me", { fantasy_weights: state.weights }).catch(() => {});
  } else {
    try { localStorage.setItem("vb-weights", JSON.stringify(state.weights)); } catch (e) {}
  }
}
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
  } else if (s.tab === "games") {
    if (cur.week) p.set("week", cur.week);
    if (cur.gamesScope && cur.gamesScope !== "all") p.set("show", cur.gamesScope);
  } else if (s.tab === "player") {
    if (s.playerId != null) p.set("pid", s.playerId);
  } else if (s.tab === "team") {
    if (s.teamId != null) p.set("tid", s.teamId);
    if (s.teamName) p.set("tname", s.teamName);
  } else if (s.tab === "game") {
    if (s.contestId != null) p.set("cid", s.contestId);
  }
  const q = p.toString();
  return "#" + s.tab + (q ? "?" + q : "");
}

// Parse the current hash into state, validating away anything stale (a season/conference that no
// longer exists, a detail tab with no id). The week is left for refreshWeeks() to validate.
const TABS = ["top", "waiver", "teams", "games", "compare", "fantasy", "favorites", "ask", "admin",
  "player", "team", "game", "verify-email"];
function applyHash() {
  const h = location.hash.replace(/^#\/?/, "");  // tolerate both "#tab" and "#/tab" (email links)
  const qi = h.indexOf("?");
  const tab = (qi >= 0 ? h.slice(0, qi) : h) || "top";
  const p = new URLSearchParams(qi >= 0 ? h.slice(qi + 1) : "");
  state.tab = TABS.includes(tab) ? tab : "top";
  if (state.tab === "verify-email") state.verifyToken = p.get("token") || null;
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
    const show = p.get("show");  // Games "Show" picker — persist across refresh
    if (show && ["all", "favorites", "ranked"].includes(show)) cur.gamesScope = show;
  }
  const pid = p.get("pid"); if (pid != null) state.playerId = pid;
  const tid = p.get("tid"); if (tid != null) state.teamId = tid;
  const tname = p.get("tname"); if (tname != null) state.teamName = tname;
  const cid = p.get("cid"); if (cid != null) state.contestId = cid;

  if ((state.tab === "player" && state.playerId == null) ||
      (state.tab === "team" && state.teamId == null) ||
      (state.tab === "game" && state.contestId == null)) {
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
  await refreshAuth();  // resolve the saved token to a user + favorites before first render
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
    swapThemeLogos();  // repoint on-screen logos without a full re-render
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
  v.className = "view";  // reset any per-view modifier (e.g. .view-ask) before dispatch
  if (state.tab === "fantasy" && !fantasyEnabled()) { setTab("top"); return; }
  const map = {
    top: renderTop, fantasy: renderFantasy, teams: renderTeams,
    games: renderGames, waiver: renderWaiver, compare: renderCompare,
    player: renderPlayer, team: renderTeamDetail, game: renderGame,
    favorites: renderFavorites, ask: renderAsk, admin: renderAdmin,
    "verify-email": renderVerifyEmail,
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

/* ---------- favorite star markers ----------
   A small ★ toggle usable in any row that carries a player/team id. Filled + gold when favorited.
   Clicking toggles via the favorites API (or nudges anonymous users to sign in). `id` may be null
   (some rows lack an id) — then no star is shown. */
function favStar(type, id) {
  if (id == null) return null;
  const on = isFav(type, id);
  return el("button", {
    class: "fav-star" + (on ? " on" : ""),
    title: on ? "Remove favorite" : "Add favorite",
    "aria-label": on ? "Remove favorite" : "Add favorite",
    onclick: (e) => { e.stopPropagation(); toggleFavorite(type, id, e.currentTarget); },
  }, on ? "★" : "☆");
}

/* A static (non-interactive) gold ★ marking a favorited team — used where we want to *show* a
   favorite without offering the add/remove toggle (e.g. the Games scoreboard). */
function favMark() {
  return el("span", { class: "fav-mark", title: "In your favorites", "aria-label": "Favorite", text: "★" });
}

/* ---------- AVCA rank chip ----------
   A small "#N" badge for a team's AVCA Coaches Poll rank (top 25 only). `rank` is null for
   unranked teams → nothing shown. Used next to team names across the scoreboard, schedules,
   standings, and box scores. */
function rankChip(rank) {
  if (rank == null) return null;
  return el("span", { class: "rank-chip", title: "AVCA Coaches Poll", text: "#" + rank });
}

/* True when both sides of a game are AVCA top-25 — a marquee "ranked matchup" worth highlighting. */
function isRankedMatchup(rankA, rankB) {
  return rankA != null && rankB != null;
}

/* A player name cell with a leading ★ and the position tag — the shared shape across leaderboards. */
function playerNameCell(r, opts) {
  const showPos = !(opts && opts.hidePos);
  return el("td", { class: "l" + (isFav("player", r.player_id) ? " is-fav" : "") }, [
    favStar("player", r.player_id),
    el("a", { class: "link", onclick: () => openPlayer(r.player_id) },
      [r.name, showPos && r.position ? el("span", { class: "pos-tag", text: r.position }) : null]),
  ]);
}

/* A team name cell (linked) with a leading ★. `short` is the display label. `rank` (optional)
   renders an AVCA rank chip after the name. `logos` (optional {logo_light, logo_dark}) prepends
   the team logo. */
function teamNameCell(id, short, cls, rank, logos) {
  const label = short || "—";
  const inner = id
    ? el("a", { class: "link", onclick: () => openTeam(id, short) }, label)
    : label;
  return el("td", { class: (cls || "l") + " team-cell" + (isFav("team", id) ? " is-fav" : "") },
    [favStar("team", id),
     logos ? teamLogoImg(logos, "leader-logo") : null, inner, rankChip(rank)]);
}

/* A team cell showing the logo + short name, linked (leaderboard identity col). No favorite star:
   Stat Leaders / Fantasy don't offer team-favoriting. */
function teamLogoCell(r) {
  const label = r.team_short || r.team || "—";
  const logo = teamLogoImg(
    { logo_light: r.team_logo_light, logo_dark: r.team_logo_dark }, "leader-logo",
  );
  const inner = r.team_id
    ? el("a", { class: "link", onclick: () => openTeam(r.team_id, label) }, label)
    : label;
  return el("td", { class: "l team-cell" }, [logo, inner]);
}

/* Per-board stat columns, mirroring each NCAA individual stat page's exact column set. Returned
   columns come AFTER the shared identity columns (#, Player, Team, Cl, Ht, Pos). The `sorted`
   column is the ranked metric (`value`); component columns read from `r.components`. */
function statColumns(statKey) {
  const S = { label: "S", get: (r) => fmt(r.sets, 0) };
  const MP = { label: "MP", get: (r) => fmtInt(r.games) };
  const c = (label, key, d = 0) => ({ label, get: (r) => fmt(r.components?.[key], d) });
  const V = (label, d) => ({ label, sorted: true, get: (r) => fmt(r.value, d) });
  switch (statKey) {
    case "kills":        return [MP, S, V("Kills", 0)];
    case "assists":      return [S, V("Assists", 0)];
    case "aces":         return [S, V("Aces", 0)];
    case "digs":         return [S, V("Digs", 0)];
    case "retatt":       return [S, V("Receptions", 0)];
    case "total_blocks": return [S, c("BS", "block_solos"), c("BA", "block_assists"), V("TB", 0)];
    case "pts":          return [c("Kills", "kills"), c("Aces", "aces"),
                                 c("BS", "block_solos"), c("BA", "block_assists"), V("Pts", 1)];
    case "hit_pct":      return [S, c("Kills", "kills"), c("Errors", "errors"),
                                 c("TA", "total_attacks"), V("Pct", 3)];
    case "kills_per_set":   return [S, c("Kills", "kills"), V("Per Set", 2)];
    case "assists_per_set": return [S, c("Assists", "assists"), V("Per Set", 2)];
    case "digs_per_set":    return [S, c("Digs", "digs"), V("Per Set", 2)];
    case "aces_per_set":    return [S, c("Aces", "aces"), V("Per Set", 2)];
    case "blocks_per_set":  return [S, c("BS", "block_solos"), c("BA", "block_assists"),
                                    c("Total", "total_blocks"), V("Per Set", 2)];
    case "pts_per_set":     return [S, c("Kills", "kills"), c("Aces", "aces"),
                                    c("BS", "block_solos"), c("BA", "block_assists"),
                                    V("Per Set", 2)];
    default: { const m = statMeta(statKey); return [S, V(m.label, m.d)]; }
  }
}

/* ---------- leaderboard table (mirrors the NCAA individual stat pages) ---------- */
function leaderTable(rows, statKey) {
  const cols = statColumns(statKey);
  const table = el("table", { class: "leader-table wide-table" });
  table.appendChild(el("thead", {}, el("tr", {}, [
    el("th", { class: "c-rank", text: "Rank" }),
    el("th", { class: "l c-player", text: "Player" }),
    el("th", { class: "l", text: "Team" }),
    el("th", { text: "Cl" }),
    el("th", { text: "Ht" }),
    el("th", { text: "Pos" }),
    ...cols.map((col) => el("th", { class: col.sorted ? "num sorted" : "num", text: col.label })),
  ])));
  const tb = el("tbody");
  rows.forEach((r, i) => {
    const nameCell = playerNameCell(r, { hidePos: true });
    nameCell.classList.add("c-player");
    tb.appendChild(el("tr", {}, [
      el("td", { class: "c-rank", text: i + 1 }),
      nameCell,
      teamLogoCell(r),
      el("td", { class: "num muted", text: r.class_year || "—" }),
      el("td", { class: "num muted", text: heightStr(r.height_inches) || "—" }),
      el("td", { class: "num muted", text: r.position || "—" }),
      ...cols.map((col) => el("td", { class: col.sorted ? "num sorted" : "num", text: col.get(r) })),
    ]));
  });
  table.appendChild(tb);
  return table;
}

/* Wrap a `.leader-table` in a horizontal scroller, freeze Rank+Player, and (on narrow screens
   where the table overflows) scroll to reveal the ranked far-right column by default. The Player
   column's sticky offset must match the Rank column's rendered width, so measure it after layout
   rather than hard-coding. Expects the table to tag its rank/player cells `.c-rank`/`.c-player`. */
function mountFrozenTable(container, table, extraClass) {
  const scroll = el("div", { class: "table-scroll" + (extraClass ? " " + extraClass : "") });
  scroll.appendChild(table);
  container.appendChild(scroll);
  requestAnimationFrame(() => {
    const rankTh = table.querySelector("thead th.c-rank");
    if (rankTh) {
      const left = Math.round(rankTh.getBoundingClientRect().width) + "px";
      table.querySelectorAll(".c-player").forEach((c) => { c.style.left = left; });
    }
    scroll.scrollLeft = scroll.scrollWidth;  // reveal the ranked column; no-op when it fits
  });
}

function mountLeaderTable(container, rows, statKey) {
  mountFrozenTable(container, leaderTable(rows, statKey));
}

/* Freeze the first `frozenCount` columns of a `.wide-table` and let the rest scroll horizontally.
   Unlike mountFrozenTable (which only knows Rank+Player), this measures each frozen column's
   rendered width and assigns cumulative sticky `left` offsets, so it works for an arbitrary run of
   leading columns (e.g. Team/GP/W/L on standings, or Rank/Player on the mini leaders). Widths are
   read from the first row, so it works with or without a <thead>. */
function freezeLeadingCols(table, frozenCount) {
  table.querySelectorAll("tr").forEach((tr) => {
    for (let i = 0; i < frozenCount && i < tr.children.length; i++) {
      tr.children[i].classList.add("frozen-col");
      if (i === frozenCount - 1) tr.children[i].classList.add("frozen-last");
    }
  });
  requestAnimationFrame(() => {
    const firstRow = table.querySelector("tr");
    if (!firstRow) return;
    let left = 0;
    for (let i = 0; i < frozenCount && i < firstRow.children.length; i++) {
      const l = left + "px";
      table.querySelectorAll("tr").forEach((tr) => {
        if (tr.children[i]) tr.children[i].style.left = l;
      });
      left += Math.round(firstRow.children[i].getBoundingClientRect().width);
    }
  });
}

function mountStickyColsTable(container, table, frozenCount) {
  const scroll = el("div", { class: "table-scroll" });
  scroll.appendChild(table);
  container.appendChild(scroll);
  freezeLeadingCols(table, frozenCount);
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
    else mountLeaderTable(body, rows, cur.stat);
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

// The whole fantasy board loads at once (no paging): the table scrolls vertically inside a
// window-sized box, and Search filters the loaded rows in-browser (instant, keeps your place).
const FANTASY_MAX_ROWS = 10000;

async function renderFantasy(root) {
  replaceURL();
  const cur = f();
  // Filter/scope changes refetch; the Search box does NOT (it filters loaded rows client-side).
  const reload = () => renderFantasy(clear(root));
  const search = el("input", {
    type: "search", class: "table-search", placeholder: "Search player, team, or conference…",
    value: cur.fpQuery || "",
  });
  root.appendChild(el("div", { class: "view-head" }, [
    el("h1", { text: "Fantasy Points" }),
    el("div", { class: "spacer" }),
    el("div", { class: "filters" }, [
      ...scopeFields(reload),
      field("Conference", confSelect(cur.conf, (v) => { cur.conf = v; reload(); })),
      field("Position", posSelect(cur.pos, (v) => { cur.pos = v; reload(); })),
      field("Search", search),
    ]),
  ]));

  const count = el("span", { class: "muted table-hint" });
  const card = el("div", { class: "card" }, el("div", { class: "card-title" }, [
    "Fantasy leaders", el("span", { class: "badge", text: scopeLabel() }), count,
  ]));
  root.appendChild(card);
  const body = el("div"); card.appendChild(body); spinner(body);

  try {
    const rows = await api("/leaderboards/fantasy", Object.assign(
      scopeParams(), weightParams(),
      { conference: cur.conf, position: cur.pos, min_sets: state.minSets,
        limit: FANTASY_MAX_ROWS, offset: 0 }
    ));
    clear(body);
    if (!rows.length) { emptyState(body, "No data for this selection."); return; }

    const table = el("table", { class: "leader-table wide-table" });
    table.appendChild(el("thead", {}, el("tr", {}, [
      el("th", { class: "c-rank", text: "#" }),
      el("th", { class: "l c-player", text: "Player" }),
      el("th", { class: "l", text: "Team" }), el("th", { class: "l", text: "Conf" }),
      el("th", { class: "num", text: "GP" }), el("th", { class: "num", text: "Sets" }),
      el("th", { class: "num sorted", text: "FP" }), el("th", { class: "num", text: "FP/set" }),
    ])));
    const tb = el("tbody");
    const trs = [];
    rows.forEach((r, i) => {
      const fpps = r.sets ? r.value / r.sets : null;
      const nameCell = playerNameCell(r);
      nameCell.classList.add("c-player");
      const tr = el("tr", {}, [
        el("td", { class: "c-rank", text: i + 1 }),
        nameCell,
        teamLogoCell(r),
        el("td", { class: "l muted", text: r.conference || "—" }),
        el("td", { class: "num", text: fmtInt(r.games) }),
        el("td", { class: "num", text: fmt(r.sets, 0) }),
        el("td", { class: "num sorted", text: fmt(r.value, 1) }),
        el("td", { class: "num", text: fmt(fpps, 2) }),
      ]);
      // Precompute the haystack once so filtering is a cheap substring test per keystroke.
      tr._hay = `${r.name || ""} ${r.team || ""} ${r.team_short || ""} ${r.conference || ""}`.toLowerCase();
      trs.push(tr);
      tb.appendChild(tr);
    });
    table.appendChild(tb);
    mountFrozenTable(body, table, "fantasy-scroll");

    // Client-side search: hide non-matching rows (rank column keeps each player's true FP rank).
    const applySearch = () => {
      const q = (cur.fpQuery || "").trim().toLowerCase();
      const terms = q ? q.split(/\s+/) : [];
      let shown = 0;
      trs.forEach((tr) => {
        const hit = !terms.length || terms.every((t) => tr._hay.includes(t));
        tr.hidden = !hit;
        if (hit) shown++;
      });
      count.textContent = q ? `${shown} of ${trs.length}` : `${trs.length} players`;
    };
    search.addEventListener("input", () => { cur.fpQuery = search.value; applySearch(); });
    applySearch();
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

// RPI is "stale" when the record shows more games than played this season — the NCAA RPI table
// still reflects last year until ~late September. When stale, the year is shown once in the column
// header (see renderTeams) rather than repeated on every row.
function rpiStale(r) {
  const m = r.rpi_record && r.rpi_record.match(/(\d+)\s*-\s*(\d+)/);
  return !!(m && (+m[1] + +m[2]) > (r.games || 0));
}
function rpiText(r) {
  return r.rpi_rank == null ? "—" : String(r.rpi_rank);
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
      // RPI (and opponents' RPI) come from the same NCAA table, which lags a season until ~late
      // Sept — annotate the year once in the headers instead of on every row.
      const rpiYr = groups[conf].some(rpiStale) ? ` (${state.season - 1})` : "";
      const table = el("table", { class: "wide-table" });
      table.appendChild(el("thead", {}, el("tr", {}, [
        el("th", { class: "l", text: "Team" }), el("th", { text: "GP" }),
        el("th", { text: "W" }), el("th", { text: "L" }), el("th", { text: "Set%" }),
        el("th", { text: "Strk" }), el("th", { text: "Conf" }), el("th", { text: "Non-Conf" }),
        el("th", { text: "Opp Rec" }), el("th", { text: "RPI" + rpiYr }),
        el("th", { text: "Opp RPI" + rpiYr }),
      ])));
      const tb = el("tbody");
      groups[conf]
        .sort((a, b) => (b.wins - a.wins) || (a.losses - b.losses) || ((b.set_pct || 0) - (a.set_pct || 0)))
        .forEach((r) => {
          tb.appendChild(el("tr", {}, [
            teamNameCell(r.team_id, r.team_short || r.team, null, r.avca_rank,
              { logo_light: r.team_logo_light, logo_dark: r.team_logo_dark }),
            el("td", { class: "num", text: fmtInt(r.games) }),
            el("td", { class: "num", text: fmtInt(r.wins) }),
            el("td", { class: "num", text: fmtInt(r.losses) }),
            el("td", { class: "num", text: r.set_pct == null ? "—" : (r.set_pct * 100).toFixed(1) + "%" }),
            el("td", { class: "num", text: streakText(r.win_streak) }),
            el("td", { class: "num", text: rec(r.conf_wins, r.conf_losses) }),
            el("td", { class: "num", text: rec(r.nonconf_wins, r.nonconf_losses) }),
            el("td", { class: "num", text: rec(r.opp_wins, r.opp_losses) }),
            el("td", { class: "num", text: rpiText(r) }),
            el("td", { class: "num", text: r.opp_rpi == null ? "—" : String(Math.round(r.opp_rpi)) }),
          ]));
        });
      table.appendChild(tb);
      mountStickyColsTable(card, table, 4);  // freeze Team, GP, W, L; scroll the rest
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

  const grid = el("div", { class: "leader-grid" }); root.appendChild(grid);
  const cats = [
    { stat: "kills", label: "Kills" }, { stat: "assists", label: "Assists" },
    { stat: "digs", label: "Digs" }, { stat: "aces", label: "Aces" },
    { stat: "total_blocks", label: "Blocks" }, { stat: "pts", label: "Points" },
  ];
  const badge = scopeLabel();

  // Fantasy card first — only when the user has fantasy enabled.
  if (fantasyEnabled()) {
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
  }

  for (const c of cats) {
    const card = el("div", { class: "card" }, el("div", { class: "card-title" }, [
      c.label, el("span", { class: "badge", text: badge }),
    ]));
    const body = el("div"); card.appendChild(body); spinner(body); grid.appendChild(card);
    try {
      const rows = await api("/leaderboards", Object.assign(scopeParams(), {
        stat: c.stat, conference: cur.conf, limit: 15,
      }));
      clear(body);
      body.appendChild(miniLeaderTable(rows, (r) => fmtInt(r.value)));
    } catch (e) { clear(body); emptyState(body, "Error: " + e.message); }
  }
}

function miniLeaderTable(rows, valFn) {
  if (!rows.length) return el("div", { class: "empty-state", text: "No data." });
  // Fixed layout with a shared column scheme so every category's table lines up column-for-column.
  const table = el("table", { class: "mini-leader wide-table" });
  table.appendChild(el("colgroup", {}, [
    el("col", { class: "c-rank" }), el("col", { class: "c-player" }),
    el("col", { class: "c-team" }), el("col", { class: "c-val" }),
  ]));
  const tb = el("tbody");
  rows.forEach((r, i) => {
    tb.appendChild(el("tr", {}, [
      el("td", { text: i + 1 }),
      el("td", { class: "l" + (isFav("player", r.player_id) ? " is-fav" : "") }, [
        favStar("player", r.player_id),
        el("a", { class: "link", onclick: () => openPlayer(r.player_id) }, r.name),
      ]),
      el("td", { class: "l muted team-cell" }, [
        teamLogoImg({ logo_light: r.team_logo_light, logo_dark: r.team_logo_dark }, "leader-logo"),
        (r.team_short || r.team) || "—",
      ]),
      el("td", { class: "num", text: valFn(r) }),
    ]));
  });
  table.appendChild(tb);
  // On narrow screens the four columns overflow the card; freeze Rank+Player and scroll the rest,
  // starting scrolled fully right so the ranked value is visible by default (like Stat Leaders).
  const scroll = el("div", { class: "table-scroll" });
  scroll.appendChild(table);
  freezeLeadingCols(table, 2);
  requestAnimationFrame(() => { scroll.scrollLeft = scroll.scrollWidth; });
  return scroll;
}

/* ---------- Compare ---------- */
const COMPARE_MAX = 10;

// Stat rows shown on each compare card (same set as the old side-by-side table).
const COMPARE_ROWS = [
  ["GP", (s) => fmtInt(s.gp)], ["Sets", (s) => fmt(s.sp, 0)],
  ["Kills", (s) => fmtInt(s.kills)], ["K/set", (s) => fmt(s.kills_per_set, 2)],
  ["Assists", (s) => fmtInt(s.assists)], ["A/set", (s) => fmt(s.assists_per_set, 2)],
  ["Digs", (s) => fmtInt(s.digs)], ["D/set", (s) => fmt(s.digs_per_set, 2)],
  ["Aces", (s) => fmtInt(s.aces)], ["Blocks", (s) => fmt(s.total_blocks, 0)],
  ["Points", (s) => fmt(s.pts, 1)], ["Pts/set", (s) => fmt(s.pts_per_set, 2)],
  ["Hit %", (s) => fmt(s.hit_pct, 3)],
];

// A filled slot: the player's name, a remove button, and their season stat line (filled async).
function comparePlayerCard(c, root) {
  const stats = el("div", { class: "compare-statlist" }); spinner(stats);
  const cardEl = el("div", { class: "compare-card" }, [
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
    el("div", { class: "muted compare-card-sub", text: c.team || "" }),
    stats,
  ]);
  return { cardEl, stats };
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

  // A card per added player (each with its own stat line) + an "add player" search card until full.
  const grid = el("div", { class: "compare-slots" });
  const entries = state.compare.map((c) => {
    const { cardEl, stats } = comparePlayerCard(c, root);
    grid.appendChild(cardEl); return { c, stats };
  });
  if (state.compare.length < COMPARE_MAX) grid.appendChild(addPlayerCard(root));
  root.appendChild(grid);

  if (!state.compare.length) return;  // add card is shown above; nothing to fill yet

  const stats = await Promise.all(state.compare.map((c) =>
    api(`/players/${c.id}/season-stats`, { season: state.season }).catch(() => null)));
  entries.forEach(({ stats: sEl }, i) => {
    clear(sEl);
    const ss = stats[i];
    if (!ss) { sEl.appendChild(el("div", { class: "muted", text: `No stats for ${state.season}` })); return; }
    COMPARE_ROWS.forEach(([label, fn]) => sEl.appendChild(el("div", { class: "compare-stat" }, [
      el("span", { class: "k", text: label }),
      el("span", { class: "v", text: fn(ss) }),
    ])));
  });
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
      favBtn("player", p.id),
      el("button", { class: "btn ghost", onclick: () => addToCompare(p.id, p.name) }, "＋ Compare"),
    ]));

    if (ss) {
      const fp = fantasyOf(ss);
      const boxes = [
        ...(fantasyEnabled()
          ? [["Fantasy Pts", fmt(fp, 1), true], ["FP/set", fmt(ss.sp ? fp / ss.sp : null, 2), true]]
          : []),
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
  { key: "fantasy_points", label: "FP", d: 1, calc: fantasyOf, fp: true },
];
// Columns visible right now — the FP column is dropped entirely when fantasy is off.
const visibleCols = (cols) => cols.filter((c) => !c.fp || fantasyEnabled());
function statCell(col, row) {
  const v = col.calc ? col.calc(row) : row[col.key];
  return el("td", { class: "num", text: col.int ? fmtInt(v) : fmt(v, col.d) });
}

// Wide, horizontally-scrolling game log with every stat column, plus a season-total footer row.
function gameLogTable(log, ss) {
  const cols = visibleCols(GAMELOG_COLS);
  const table = el("table", { class: "wide-table" });
  const htr = el("tr", {}, [
    el("th", { class: "l sticky-col", text: "Opponent" }),
    el("th", { class: "l", text: "Wk" }), el("th", { class: "l", text: "Date" }),
  ]);
  cols.forEach((c) => htr.appendChild(el("th", { text: c.label })));
  table.appendChild(el("thead", {}, htr));

  const tb = el("tbody");
  log.forEach((g) => {
    const tr = el("tr", {}, [
      el("td", { class: "l sticky-col" }, g.opponent_id
        ? el("a", { class: "link", onclick: () => openTeam(g.opponent_id, g.opponent_short || g.opponent) }, (g.opponent_short || g.opponent) || "—")
        : ((g.opponent_short || g.opponent) || "—")),
      el("td", { class: "l muted", text: g.week_number == null ? "—" : g.week_number }),
      el("td", { class: "l" }, g.contest_id
        ? el("a", { class: "link", onclick: () => openGame(g.contest_id) }, g.date ? g.date.slice(0, 10) : "box")
        : el("span", { class: "muted", text: g.date ? g.date.slice(0, 10) : "—" })),
    ]);
    cols.forEach((c) => tr.appendChild(statCell(c, g)));
    tb.appendChild(tr);
  });
  // Season total from the derived line (which names sets `sp`, games `gp`).
  if (ss) {
    const total = Object.assign({}, ss, { sets: ss.sp });
    const tr = el("tr", { class: "total-row" }, [
      el("td", { class: "l sticky-col", text: "Season total" }),
      el("td", { class: "l muted", text: "" }), el("td", { class: "l muted", text: "" }),
    ]);
    cols.forEach((c) => tr.appendChild(statCell(c, total)));
    tb.appendChild(tr);
  }
  table.appendChild(tb);
  return el("div", { class: "table-scroll" }, table);
}

/* ---------- Games: scoreboard, box score, team schedule ---------- */

// "2026-09-03" -> "Wed, Sep 3" (date-only, no timezone shift). Falls back to the raw string.
function fmtDateShort(iso) {
  if (!iso) return "";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!m) return iso;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

// Box-score player columns (mirrors the game log, minus week/fantasy; blocks/hit% are derived).
const BOX_COLS = [
  { key: "sets", label: "Sets", d: 0 },
  { key: "kills", label: "Kills", int: true }, { key: "errors", label: "Err", int: true },
  { key: "total_attacks", label: "TA", int: true },
  { key: "hit_pct", label: "Hit%", d: 3, calc: hitPct },
  { key: "assists", label: "Ast", int: true }, { key: "aces", label: "Ace", int: true },
  { key: "serr", label: "SE", int: true }, { key: "digs", label: "Dig", int: true },
  { key: "retatt", label: "Rec", int: true }, { key: "rerr", label: "RE", int: true },
  { key: "block_solos", label: "BS", int: true }, { key: "block_assists", label: "BA", int: true },
  { key: "total_blocks", label: "Blk", int: true,
    calc: (r) => (r.block_solos || 0) + (r.block_assists || 0) },
  { key: "berr", label: "BE", int: true }, { key: "bhe", label: "BHE", int: true },
  { key: "pts", label: "Pts", d: 1 },
];

// Top-level Games tab: a week/date picker + a grouped scoreboard of played + upcoming games.
async function renderGames(root) {
  replaceURL();
  const cur = state.filters.games || (state.filters.games = defaultFilters());
  const numbered = state.weeks.filter((w) => w.week_number != null);
  if (!cur.week && numbered.length) cur.week = numbered[numbered.length - 1].week_number;

  const wkSel = el("select", { onchange: (e) => {
    cur.week = e.target.value; renderGames(clear(root));
  } });
  numbered.forEach((w) => wkSel.appendChild(el("option", {
    value: w.week_number,
    text: `Wk ${w.week_number} (${w.start ? w.start.slice(5) : "?"}–${w.end ? w.end.slice(5) : "?"})`,
  })));
  if (cur.week) wkSel.value = cur.week;

  if (!cur.gamesScope) cur.gamesScope = "all";
  const scopeSel = el("select", { onchange: (e) => {
    cur.gamesScope = e.target.value; renderGames(clear(root));
  } });
  [["all", "All games"], ["favorites", "★ Favorites"], ["ranked", "Top 25 matchups"]]
    .forEach(([v, t]) => scopeSel.appendChild(el("option", { value: v, text: t })));
  scopeSel.value = cur.gamesScope;

  root.appendChild(el("div", { class: "filters games-filters" },
    [field("Week", wkSel), field("Show", scopeSel)]));

  const holder = el("div"); root.appendChild(holder); spinner(holder);
  if (!wkSel.value) { clear(holder); emptyState(holder, "No weeks available yet."); return; }
  try {
    const all = await apiCached("/games", { season: state.season, week: wkSel.value });
    clear(holder);
    const games = filterScoreboard(all, cur.gamesScope);
    if (!all.length) { emptyState(holder, "No games for this selection."); return; }
    if (!games.length) {
      emptyState(holder, cur.gamesScope === "favorites"
        ? "No games this week involve your favorite teams."
        : "No Top-25 matchups this week.");
      return;
    }
    renderScoreboard(holder, games);
  } catch (e) { clear(holder); emptyState(holder, "Error: " + e.message); }
}

// Client-side scoreboard filter: "favorites" keeps games where either side is a favorited team;
// "ranked" keeps top-25-vs-top-25 matchups; "all" (or anything else) is a pass-through.
function filterScoreboard(games, scope) {
  if (scope === "favorites") {
    return games.filter((g) =>
      (g.away_team && isFav("team", g.away_team.id)) ||
      (g.home_team && isFav("team", g.home_team.id)));
  }
  if (scope === "ranked") {
    return games.filter((g) =>
      isRankedMatchup(g.away_team && g.away_team.avca_rank, g.home_team && g.home_team.avca_rank));
  }
  return games;
}

// "2026-09-01 18:00" -> "2026-09-01": contests carry a time suffix, so group on the calendar day.
const dayKey = (iso) => (iso ? iso.slice(0, 10) : "TBD");

// Per-set line score as "a-b, a-b, …" from a {away:[…], home:[…]} pair (already oriented by caller).
function setLine(awayArr, homeArr) {
  const away = awayArr || [], home = homeArr || [];
  const n = Math.max(away.length, home.length);
  const parts = [];
  for (let i = 0; i < n; i++) {
    if (away[i] == null && home[i] == null) continue;
    parts.push(`${away[i] == null ? "–" : away[i]}-${home[i] == null ? "–" : home[i]}`);
  }
  return parts.length ? parts.join(", ") : null;
}

// Group a scoreboard by date, one collapsible card per day (open by default; each day toggles
// independently so you can hide a finished day and keep others expanded).
// An opponent with a real name but no linked team record is a non-D1 school (D2/D3/NAIA) — we only
// track D1 teams, so these can't link anywhere. "TBA"/"TBD" placeholders are not tagged.
function isNonD1Opp(name, hasId) {
  if (hasId) return false;
  const n = (name || "").trim().toLowerCase();
  return !!n && n !== "tba" && n !== "tbd";
}
function nonD1Tag() {
  return el("span", { class: "nd1-tag", title: "Not an NCAA Division I team", text: "non-D1" });
}

function renderScoreboard(root, games) {
  const byDate = {};
  games.forEach((g) => { const k = dayKey(g.date); (byDate[k] = byDate[k] || []).push(g); });
  Object.keys(byDate).sort().forEach((d) => {
    const list = el("div", { class: "game-list" });
    byDate[d].forEach((g) => list.appendChild(scoreRow(g)));
    root.appendChild(el("details", { class: "card day-card", open: true }, [
      el("summary", { class: "card-title day-summary" }, [
        fmtDateShort(d) || "TBD",
        el("span", { class: "badge", text: byDate[d].length + (byDate[d].length === 1 ? " game" : " games") }),
      ]),
      list,
    ]));
  });
}

// One scoreboard row: away @ home with the final set score (played) or start time (upcoming).
function scoreRow(g) {
  const played = g.status === "played";
  const bothScores = g.home_sets_won != null && g.away_sets_won != null;
  const homeWon = played && bothScores && g.home_sets_won > g.away_sets_won;
  const awayWon = played && bothScores && g.away_sets_won > g.home_sets_won;
  const teamCell = (t, fallback, won) => {
    const name = t ? (t.short_name || t.name) : (fallback || "TBD");
    const fav = t && isFav("team", t.id);
    const label = t
      ? el("a", { class: "link" + (won ? " win" : ""),
          onclick: (e) => { e.stopPropagation(); openTeam(t.id, name); } }, name)
      : el("span", { class: won ? "win" : "", text: name });
    // No add/remove toggle on the scoreboard — just a static ★ so favorites are visible without
    // colliding with the "Top 25" matchup badge.
    return el("div", { class: "game-team" + (fav ? " is-fav" : "") }, [
      fav ? favMark() : null,
      teamLogoImg(t, "game-logo"),
      label,
      t ? rankChip(t.avca_rank) : null,
      (!t && isNonD1Opp(fallback, false)) ? nonD1Tag() : null,
    ]);
  };
  // Scoreboard orientation is away @ home, so per-set scores read away-home too.
  const sets = played && g.set_scores ? setLine(g.set_scores.away, g.set_scores.home) : null;
  const right = played
    ? el("div", { class: "game-result" }, [
        el("div", { class: "game-score", text: bothScores ? `${g.away_sets_won}–${g.home_sets_won}` : "final" }),
        sets ? el("div", { class: "game-sets muted", text: sets }) : null,
      ])
    : el("div", { class: "game-time muted", text: g.game_time || "TBD" });
  const ranked = isRankedMatchup(g.away_team && g.away_team.avca_rank, g.home_team && g.home_team.avca_rank);
  const row = el("div", { class: "game-row" + (played && g.contest_id ? " clickable" : "") }, [
    ranked ? el("span", { class: "matchup-badge", title: "Top-25 matchup", text: "Top 25" }) : null,
    el("div", { class: "game-teams" }, [
      teamCell(g.away_team, g.away_name, awayWon),
      el("span", { class: "at muted", text: "@" }),
      teamCell(g.home_team, g.home_name, homeWon),
    ]),
    right,
  ]);
  if (played && g.contest_id) row.addEventListener("click", () => openGame(g.contest_id));
  return row;
}

// A team's Schedule & Results as two collapsible sections. Results are open by default; Upcoming
// is collapsed in season scope but expanded when a single week is in scope (short list, worth
// showing).
function renderTeamGames(root, games, expandUpcoming) {
  const oppCell = (g) => {
    const prefix = g.site === "away" ? "@ " : g.site === "neutral" ? "vs " : "vs ";
    const name = g.opponent_short || g.opponent || "TBD";
    const link = g.opponent_id
      ? el("a", { class: "link", onclick: (e) => { e.stopPropagation(); openTeam(g.opponent_id, name); } }, name)
      : el("span", { text: name });
    const logo = teamLogoImg(
      { logo_light: g.opponent_logo_light, logo_dark: g.opponent_logo_dark }, "sched-logo");
    return el("span", { class: "sched-opp" + (g.opponent_id && isFav("team", g.opponent_id) ? " is-fav" : "") }, [
      el("span", { class: "muted", text: prefix }),
      logo,
      link,
      rankChip(g.opponent_avca_rank),
      isNonD1Opp(g.opponent, g.opponent_id) ? nonD1Tag() : null,
    ]);
  };
  // A <details> section with a count in the summary; `open` controls default expand state.
  const section = (title, count, open, list) =>
    el("details", { class: "sched-section", open }, [
      el("summary", { class: "sched-subhead" }, [
        title, el("span", { class: "sched-count muted", text: `(${count})` }),
      ]),
      list,
    ]);
  const upcoming = games.filter((g) => g.status === "upcoming");
  const played = games.filter((g) => g.status === "played");

  if (played.length) {
    const list = el("div", { class: "sched-list" });
    played.slice().reverse().forEach((g) => {
      const sc = (g.team_sets_won != null && g.opponent_sets_won != null)
        ? `${g.team_sets_won}–${g.opponent_sets_won}` : "";
      // Contest set_scores are keyed home/away; orient to team–opponent using this game's site.
      const ss = g.set_scores;
      const sets = ss
        ? (g.site === "home" ? setLine(ss.home, ss.away) : setLine(ss.away, ss.home))
        : null;
      const row = el("div", { class: "sched-row" + (g.contest_id ? " clickable" : "") }, [
        el("span", { class: "sched-date muted", text: fmtDateShort(g.date) }),
        oppCell(g),
        sets ? el("span", { class: "sched-sets muted", text: sets }) : null,
        g.result ? el("span", { class: "result " + (g.result === "W" ? "win" : "loss"), text: g.result }) : el("span"),
        el("span", { class: "sched-score", text: sc }),
      ]);
      if (g.contest_id) row.addEventListener("click", () => openGame(g.contest_id));
      list.appendChild(row);
    });
    root.appendChild(section("Results", played.length, true, list));
  }
  if (upcoming.length) {
    const list = el("div", { class: "sched-list" });
    upcoming.forEach((g) => {
      list.appendChild(el("div", { class: "sched-row" }, [
        el("span", { class: "sched-date muted", text: fmtDateShort(g.date) }),
        oppCell(g),
        el("span", { class: "sched-time muted", text: g.game_time || "" }),
      ]));
    });
    root.appendChild(section("Upcoming", upcoming.length, !!expandUpcoming, list));
  }
}

// Quality-wins list for a team: each row = a beaten opponent + the rank it held on game day.
function renderQualityWins(root, res) {
  const wins = (res && res.wins) || [];
  const pollLabel = res && res.poll === "rpi" ? "RPI" : "AVCA";
  if (!wins.length) {
    emptyState(root, `No wins yet over ${pollLabel} top-${(res && res.threshold) || 25} teams. `
      + "Rankings are tracked as of each game date, so wins before tracking began aren't counted.");
    return;
  }
  const list = el("div", { class: "sched-list" });
  wins.forEach((w) => {
    const name = w.opponent_short || w.opponent || "?";
    const chip = el("span", { class: "rank-chip", title: pollLabel + " rank on game day", text: "#" + w.rank_at_time });
    const opp = el("span", { class: "sched-opp" + (w.opponent_id && isFav("team", w.opponent_id) ? " is-fav" : "") }, [
      el("span", { class: "muted", text: "vs " }),
      teamLogoImg({ logo_light: w.opponent_logo_light, logo_dark: w.opponent_logo_dark }, "sched-logo"),
      w.opponent_id
        ? el("a", { class: "link", onclick: (e) => { e.stopPropagation(); openTeam(w.opponent_id, name); } }, name)
        : el("span", { text: name }),
      chip,
    ]);
    const row = el("div", { class: "sched-row" + (w.contest_id ? " clickable" : "") }, [
      el("span", { class: "sched-date muted", text: fmtDateShort(w.date) }),
      opp,
      el("span", { class: "result win", text: "W" }),
      el("span", { class: "sched-score", text: w.score || "" }),
    ]);
    if (w.contest_id) row.addEventListener("click", () => openGame(w.contest_id));
    list.appendChild(row);
  });
  root.appendChild(list);
}

// Box-score detail (#/game?cid=…): header + per-set line score + both teams' player tables.
async function renderGame(root) {
  replaceURL();
  const cid = state.contestId;
  root.appendChild(el("div", { class: "back-link" },
    el("a", { class: "link", onclick: () => goBack("games") }, "← Back")));
  const holder = el("div"); root.appendChild(holder); spinner(holder);
  try {
    const [c, stats] = await Promise.all([
      api(`/contests/${cid}`),
      api(`/contests/${cid}/stats`).catch(() => []),
    ]);
    clear(holder);
    holder.appendChild(gameHeader(c));
    holder.appendChild(boxScoreCard(c.away_team, stats.filter((s) => s.team_id === c.away_team_id)));
    holder.appendChild(boxScoreCard(c.home_team, stats.filter((s) => s.team_id === c.home_team_id)));
  } catch (e) { clear(holder); emptyState(holder, "Error: " + e.message); }
}

function gameHeader(c) {
  const both = c.home_sets_won != null && c.away_sets_won != null;
  const teamBlock = (t, sets, won) => el("div", { class: "gh-team" + (won ? " win" : "") }, [
    teamLogoImg(t, "team-logo-lg"),
    el("div", { class: "gh-name" }, t
      ? [el("a", { class: "link", onclick: () => openTeam(t.id, t.short_name || t.name) }, t.short_name || t.name),
         rankChip(t.avca_rank)]
      : el("span", { text: "TBD" })),
    el("div", { class: "gh-sets", text: sets == null ? "–" : sets }),
  ]);
  const card = el("div", { class: "card game-header" }, [
    el("div", { class: "muted", text: c.date ? fmtDateShort(c.date) : "" }),
    el("div", { class: "gh-grid" }, [
      teamBlock(c.away_team, c.away_sets_won, both && c.away_sets_won > c.home_sets_won),
      el("div", { class: "gh-vs muted", text: "@" }),
      teamBlock(c.home_team, c.home_sets_won, both && c.home_sets_won > c.away_sets_won),
    ]),
  ]);
  const ss = c.set_scores;
  if (ss && (ss.home || ss.away)) card.appendChild(lineScoreTable(c, ss));
  return card;
}

function lineScoreTable(c, ss) {
  const away = ss.away || [], home = ss.home || [];
  const n = Math.max(away.length, home.length);
  if (!n) return el("div");
  const nm = (t, fb) => (t ? (t.short_name || t.name) : fb);
  const htr = el("tr", {}, [el("th", { class: "l", text: "" })]);
  for (let i = 0; i < n; i++) htr.appendChild(el("th", { text: "S" + (i + 1) }));
  const row = (label, arr) => {
    const tr = el("tr", {}, [el("td", { class: "l", text: label })]);
    for (let i = 0; i < n; i++) tr.appendChild(el("td", { class: "num", text: arr[i] == null ? "" : arr[i] }));
    return tr;
  };
  const table = el("table", { class: "line-score" }, [
    el("thead", {}, htr),
    el("tbody", {}, [row(nm(c.away_team, "Away"), away), row(nm(c.home_team, "Home"), home)]),
  ]);
  return el("div", { class: "table-scroll" }, table);
}

function boxScoreCard(team, stats) {
  const name = team ? (team.short_name || team.name) : "Team";
  const card = el("div", { class: "card" });
  card.appendChild(el("div", { class: "card-title" }, [
    teamLogoImg(team, "game-logo"),
    team ? el("a", { class: "link", onclick: () => openTeam(team.id, name) }, name) : el("span", { text: name }),
    el("span", { class: "badge", text: "box score" }),
  ]));
  if (!stats.length) {
    card.appendChild(el("div", { class: "empty-state", text: "No player stats recorded." }));
    return card;
  }
  const rows = stats.slice().sort((a, b) => (b.pts || 0) - (a.pts || 0));
  const htr = el("tr", {}, [el("th", { class: "l sticky-col", text: "Player" })]);
  BOX_COLS.forEach((c) => htr.appendChild(el("th", { text: c.label })));
  const tb = el("tbody");
  rows.forEach((s) => {
    const tr = el("tr", {}, [
      el("td", { class: "l sticky-col" },
        el("a", { class: "link", onclick: () => openPlayer(s.player_id) }, s.player_name || ("#" + s.player_id))),
    ]);
    BOX_COLS.forEach((c) => tr.appendChild(statCell(c, s)));
    tb.appendChild(tr);
  });
  const table = el("table", { class: "wide-table" }, [el("thead", {}, htr), tb]);
  card.appendChild(el("div", { class: "table-scroll" }, table));
  return card;
}

/* ---------- Team detail (roster) ---------- */
async function openTeam(id, name) {
  state.teamId = id;
  state.teamName = name;
  setTab("team");
}

// Open a played game's box score (#/game?cid=…).
function openGame(cid) {
  state.contestId = cid;
  setTab("game");
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
  { key: "fantasy_points", label: "FP", d: 1, fp: true },
];

// Pick the logo variant that reads on the current theme. The fields are named for the BACKGROUND
// they suit: logo_dark = the light-ink logo for a dark background, logo_light = the dark-ink logo
// for a light background. Fall back to whichever exists (a few teams have only one).
function teamLogoUrl(t) {
  const dark = document.documentElement.getAttribute("data-theme") !== "light";
  return (dark ? t.logo_dark : t.logo_light) || t.logo_light || t.logo_dark || null;
}

// A team logo <img> that remembers BOTH variants (as data attrs) so a theme toggle can swap the
// src in place — see swapThemeLogos() — instead of forcing a full re-render that would collapse
// open day cards / schedule sections. Returns null when the team has no logo at all.
function teamLogoImg(t, cls) {
  if (!t) return null;
  const src = teamLogoUrl(t);
  if (!src) return null;
  return el("img", {
    class: cls, src, alt: "",
    dataset: { logo: "1", logoLight: t.logo_light || "", logoDark: t.logo_dark || "" },
    onerror: (e) => e.target.remove(),
  });
}

// Re-point every on-screen team logo at the variant that reads on the just-applied theme.
function swapThemeLogos() {
  const dark = document.documentElement.getAttribute("data-theme") !== "light";
  document.querySelectorAll("img[data-logo]").forEach((img) => {
    const url = (dark ? img.dataset.logoDark : img.dataset.logoLight)
      || img.dataset.logoLight || img.dataset.logoDark;
    if (url) img.src = url;
  });
}

async function renderTeamDetail(root) {
  replaceURL();
  const id = state.teamId;
  root.appendChild(el("div", { class: "back-link" },
    el("a", { class: "link", onclick: () => goBack("teams") }, "← Back")));

  // Single card holds the whole team header: logo + name + favorite, facts, links and coach.
  const info = el("div", { class: "card team-info" }); spinner(info); root.appendChild(info);

  // Fetch once; reused for the header card and to flag top-25 matchups in the schedule below.
  const teamP = api(`/teams/${id}`);
  teamP.then((t) => {
    renderTeamInfoCard(info, t);
  }).catch(() => { clear(info); info.remove(); });

  const cur = f();
  root.appendChild(el("div", { class: "filters team-scope" },
    scopeFields(() => renderTeamDetail(clear(root)))));

  // Schedule & Results — scoped to the same Season/Week selector as the player stats below.
  const schedCard = el("div", { class: "card" });
  schedCard.appendChild(el("div", { class: "card-title" }, [
    "Schedule & Results", el("span", { class: "badge", text: scopeLabel() }),
  ]));
  const schedBody = el("div"); schedCard.appendChild(schedBody); spinner(schedBody);
  root.appendChild(schedCard);
  const schedParams = { season: state.season };
  if (cur.scope === "week" && cur.week) schedParams.week = cur.week;
  Promise.all([apiCached(`/teams/${id}/games`, schedParams), teamP.catch(() => null)]).then(([games, t]) => {
    clear(schedBody);
    if (!games.length) {
      emptyState(schedBody, cur.scope === "week" ? "No games this week." : "No games for this season.");
      return;
    }
    renderTeamGames(schedBody, games, cur.scope === "week");
  }).catch(() => { clear(schedBody); emptyState(schedBody, "Could not load schedule."); });

  // Quality wins — wins over an opponent ranked (top 25) as of the game date. Toggle AVCA vs RPI.
  const qwCard = el("div", { class: "card" });
  let qwPoll = "avca";
  const qwToggle = el("div", { class: "seg-toggle" });
  const qwBody = el("div");
  const setPoll = (p) => {
    qwPoll = p;
    Array.from(qwToggle.children).forEach((b) => b.classList.toggle("active", b.dataset.poll === p));
    clear(qwBody); spinner(qwBody);
    api(`/teams/${id}/quality-wins`, { season: state.season, poll: qwPoll, threshold: 25 })
      .then((res) => { clear(qwBody); renderQualityWins(qwBody, res); })
      .catch(() => { clear(qwBody); emptyState(qwBody, "Could not load quality wins."); });
  };
  [["avca", "AVCA"], ["rpi", "RPI"]].forEach(([p, label]) =>
    qwToggle.appendChild(el("button", { class: "seg-btn", "data-poll": p, onclick: () => setPoll(p) }, label)));
  qwCard.appendChild(el("div", { class: "card-title" }, [
    "Quality wins", qwToggle,
    el("span", { class: "muted table-hint", text: "beat a top-25 team (rank as of game day)" }),
  ]));
  qwCard.appendChild(qwBody); root.appendChild(qwCard);
  setPoll("avca");

  const card = el("div", { class: "card" }, el("div", { class: "card-title" }, [
    "Player stats", el("span", { class: "badge", text: scopeLabel() }),
    el("span", { class: "muted table-hint", text: "Tap a column to sort · scroll table sideways →" }),
  ]));
  const body = el("div"); card.appendChild(body); spinner(body); root.appendChild(card);

  try {
    const rows = await api(`/teams/${id}/player-stats`, Object.assign(scopeParams(), weightParams()));
    clear(body);
    if (!rows.length) { emptyState(body, "No stats for this team in the selected scope."); return; }
    renderTeamTable(body, rows);
  } catch (e) { clear(body); emptyState(body, "Error: " + e.message); }
}

// Team overview: logo, conference/location, season record + RPI, head coach, and site links.
// Record and coach fetches are best-effort — the card renders whatever resolves.
function renderTeamInfoCard(card, t) {
  clear(card);
  const loc = [t.city, t.state].filter(Boolean).join(", ");

  const facts = el("div", { class: "team-facts" });
  const addFact = (label, value) => {
    if (value == null || value === "") return;
    facts.appendChild(el("div", { class: "fact" }, [
      el("span", { class: "fact-label", text: label }),
      el("span", { class: "fact-value", text: value }),
    ]));
  };
  addFact("Conference", t.conference ? confShort(t.conference) : null);
  if (loc) addFact("Location", loc);
  addFact("AVCA", t.avca_rank != null ? "#" + t.avca_rank : null);
  addFact("RPI", t.rpi_rank != null ? "#" + t.rpi_rank : null);

  const links = el("div", { class: "team-links" });
  if (t.website) links.appendChild(el("a", { class: "btn-link", href: t.website,
    target: "_blank", rel: "noopener", text: "Official site ↗" }));
  if (t.stats_url) links.appendChild(el("a", { class: "btn-link", href: t.stats_url,
    target: "_blank", rel: "noopener", text: "Team stats ↗" }));

  const title = el("div", { class: "team-title" }, [
    el("h1", { text: t.short_name || t.name }),
    t.short_name && t.name !== t.short_name
      ? el("span", { class: "team-fullname muted", text: t.name }) : null,
    favBtn("team", t.id),
  ]);

  card.appendChild(el("div", { class: "team-info-grid" }, [
    teamLogoImg(t, "team-logo-lg"),
    el("div", { class: "team-info-main" },
      [title, facts, links.childNodes.length ? links : null]),
  ]));

  // Season record (from linescores) — appended as its own fact row when it resolves.
  api("/team-records", { season: state.season, team_id: t.id }).then((rows) => {
    const r = rows && rows[0];
    if (!r) return;
    addFact("Record", `${r.wins}-${r.losses}`);
    const conf = (r.conf_wins != null && r.conf_losses != null)
      ? `${r.conf_wins}-${r.conf_losses}` : null;
    if (conf) addFact("Conf record", conf);
    if (r.sets_won != null) addFact("Sets", `${r.sets_won}-${r.sets_lost}`);
    if (r.win_streak) addFact("Streak",
      (r.win_streak > 0 ? "W" : "L") + Math.abs(r.win_streak));
  }).catch(() => {});

  // Head coach (lowest sort_order for the season). Best-effort.
  api(`/teams/${t.id}/coaches`, { season: state.season }).then((coaches) => {
    const c = (coaches || [])[0];
    if (!c) return;
    const tenure = c.seasons
      ? c.seasons + (String(c.seasons) === "1" ? " season" : " seasons") : null;
    // The "Head coach" label already names the role, so don't repeat c.title (usually "Head Coach").
    const bits = [c.record ? "Career " + c.record : null, tenure]
      .filter(Boolean).join(" · ");
    card.appendChild(el("div", { class: "team-coach" }, [
      el("span", { class: "fact-label", text: "Head coach" }),
      el("span", { class: "coach-name", text: c.name }),
      bits ? el("span", { class: "muted coach-meta", text: bits }) : null,
    ]));
  }).catch(() => {});
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
  const cols = visibleCols(TEAM_COLS);
  // Default sort follows the leading value column: FP when fantasy is on, total Points when off.
  const sort = state.teamSort || { key: fantasyEnabled() ? "fantasy_points" : "pts", dir: -1 };
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
  cols.forEach((c) => htr.appendChild(el("th", {
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
    const tr = el("tr", {}, el("td", { class: "l sticky-col" + (isFav("player", r.player_id) ? " is-fav" : "") }, [
      favStar("player", r.player_id),
      el("a", { class: "link", onclick: () => openPlayer(r.player_id) },
        [r.name, r.position ? el("span", { class: "pos-tag", text: r.position }) : null]),
    ]));
    cols.forEach((c) => tr.appendChild(el("td", {
      class: "num", text: c.int ? fmtInt(r[c.key]) : fmt(r[c.key], c.d),
    })));
    tb.appendChild(tr);
  });
  // Team cumulative totals footer.
  const totals = teamTotals(rows);
  const ttr = el("tr", { class: "total-row" },
    el("td", { class: "l sticky-col", text: "Team totals" }));
  cols.forEach((c) => ttr.appendChild(el("td", {
    class: "num", text: c.int ? fmtInt(totals[c.key]) : fmt(totals[c.key], c.d),
  })));
  tb.appendChild(ttr);
  table.appendChild(tb);
  body.appendChild(el("div", { class: "table-scroll" }, table));
}

/* ==========================================================================================
   Accounts, favorites, admin & Ask — everything that needs a signed-in user.
   ========================================================================================== */

/* ---------- session lifecycle ---------- */
// Resolve the stored token to a user (and their favorites). Called on boot and after any auth
// change. Safe to call with no token — it just renders the logged-out header.
async function refreshAuth() {
  if (state.token) {
    try {
      state.user = await api("/auth/me");
      // Always source weights from the account — reset to defaults when the user has none saved,
      // so anonymous localStorage weights don't bleed into a signed-in session.
      state.weights = state.user.fantasy_weights && Object.keys(state.user.fantasy_weights).length
        ? Object.assign({}, DEFAULT_WEIGHTS, state.user.fantasy_weights)
        : Object.assign({}, DEFAULT_WEIGHTS);
      await loadFavorites();
    } catch (e) {
      // Token invalid/expired — fall back to anonymous without nagging.
      saveToken(null); state.user = null; state.favorites = new Set();
    }
  } else {
    state.user = null; state.favorites = new Set();
  }
  renderAuthArea();
  updateTabVisibility();
  updateVerifyBanner();
}

// Called by api()/req() on a 401 from an authenticated request: drop the session and re-render.
function onAuthExpired() {
  saveToken(null); state.user = null; state.favorites = new Set();
  renderAuthArea(); updateTabVisibility(); updateVerifyBanner();
  toast("Your session expired — please sign in again.", true);
}

async function completeLogin(auth) {
  saveToken(auth.token);
  state.user = auth.user;
  // Hydrate weights from the account (defaults when none saved), never from anonymous localStorage.
  state.weights = auth.user.fantasy_weights && Object.keys(auth.user.fantasy_weights).length
    ? Object.assign({}, DEFAULT_WEIGHTS, auth.user.fantasy_weights)
    : Object.assign({}, DEFAULT_WEIGHTS);
  await loadFavorites();
  closeAuthModal();
  renderAuthArea(); updateTabVisibility(); updateVerifyBanner();
  toast(`Welcome, ${auth.user.name || auth.user.email}`);
  render();
  // First-ever sign-in: no fantasy decision on file yet -> ask once. Declining stores `false`,
  // so it never asks again (changeable later in Account settings).
  if (!fantasyDecided()) openFantasyPrompt();
}

function logout() {
  saveToken(null); state.user = null; state.favorites = new Set();
  state.weights = loadWeights();  // revert to this browser's anonymous weights
  renderAuthArea(); updateTabVisibility(); updateVerifyBanner();
  if (state.tab === "favorites" || state.tab === "admin") setTab("top");
  else render();
  toast("Signed out");
}

// Show/hide the gated tabs. Favorites needs a user; Admin needs an admin; Fantasy needs opt-in.
function updateTabVisibility() {
  $$("#tabs button[data-auth]").forEach((b) => { b.hidden = !state.user; });
  $$("#tabs button[data-admin]").forEach((b) => { b.hidden = !(state.user && state.user.is_admin); });
  $$("#tabs button[data-fantasy]").forEach((b) => { b.hidden = !fantasyEnabled(); });
}

/* ---------- header auth area ---------- */
function renderAuthArea() {
  const area = clear($("#auth-area"));
  if (!state.user) {
    area.appendChild(el("button", { class: "btn", onclick: () => openAuthModal("login") }, "Sign in"));
    return;
  }
  const label = state.user.name || state.user.email.split("@")[0];
  const menu = el("div", { class: "user-menu" }, [
    el("button", { class: "btn ghost user-btn", onclick: (e) => {
      const m = e.currentTarget.nextSibling; m.hidden = !m.hidden;
    } }, [label, state.user.is_admin ? el("span", { class: "admin-chip", text: "admin" }) : null]),
    el("div", { class: "user-dropdown", hidden: true }, [
      el("button", { class: "menu-item", onclick: () => { setTab("favorites"); } }, "★ Favorites"),
      state.user.is_admin ? el("button", { class: "menu-item", onclick: () => setTab("admin") }, "Admin") : null,
      el("button", { class: "menu-item", onclick: () => openAccountModal() }, "Account & passkeys"),
      el("button", { class: "menu-item", onclick: () => logout() }, "Sign out"),
    ]),
  ]);
  area.appendChild(menu);
}

// Close the user dropdown when clicking elsewhere.
document.addEventListener("click", (e) => {
  if (!e.target.closest(".user-menu")) {
    const d = $(".user-dropdown"); if (d) d.hidden = true;
  }
});

/* ---------- email verification banner ---------- */
function updateVerifyBanner() {
  const b = $("#verify-banner");
  if (!b) return;
  if (state.user && !state.user.email_verified) {
    clear(b);
    b.appendChild(el("span", { text: "Please verify your email to secure your account. " }));
    b.appendChild(el("button", { class: "link-btn", onclick: resendVerification }, "Resend link"));
    b.hidden = false;
  } else {
    b.hidden = true;
  }
}

async function resendVerification() {
  try { await req("POST", "/auth/email/send"); toast("Verification email sent."); }
  catch (e) { toast("Could not send: " + e.message, true); }
}

/* ---------- favorites ---------- */
async function loadFavorites() {
  try {
    const rows = await api("/favorites");
    state.favoriteRows = rows;
    state.favorites = new Set(rows.map((r) => favKey(r.entity_type, r.entity_id)));
  } catch (e) {
    state.favoriteRows = []; state.favorites = new Set();
  }
}

async function toggleFavorite(type, id) {
  if (!state.user) { openAuthModal("login"); toast("Sign in to save favorites", true); return; }
  const on = isFav(type, id);
  try {
    if (on) { await req("DELETE", `/favorites/${type}/${id}`); state.favorites.delete(favKey(type, id)); }
    else { await req("POST", "/favorites", { entity_type: type, entity_id: id }); state.favorites.add(favKey(type, id)); }
    await loadFavorites();  // keep the cached rows (used by the Favorites tab) in sync
    render();               // reflect the new state across the current screen
  } catch (e) {
    toast("Favorite failed: " + e.message, true);
  }
}

// A labeled favorite toggle button for detail-page headers.
function favBtn(type, id) {
  const on = isFav(type, id);
  return el("button", {
    class: "btn ghost fav-btn" + (on ? " on" : ""),
    onclick: () => toggleFavorite(type, id),
  }, on ? "★ Favorited" : "☆ Favorite");
}

/* ---------- Favorites tab ---------- */
// Counting categories a player card can headline. `perKey` is the per-set rate used to rank which
// stats to surface — so a libero (high digs/set) leads with Digs, a hitter with Kills, a setter
// with Assists — no position hardcoding needed. `ab` is the compact game-log abbreviation.
const FAV_PLAYER_CATS = [
  { key: "kills", perKey: "kills_per_set", label: "Kills", ab: "K" },
  { key: "assists", perKey: "assists_per_set", label: "Assists", ab: "A" },
  { key: "digs", perKey: "digs_per_set", label: "Digs", ab: "D" },
  { key: "total_blocks", perKey: "blocks_per_set", label: "Blocks", ab: "B" },
  { key: "aces", perKey: "aces_per_set", label: "Aces", ab: "Ace" },
];

function miniBox(value, label, accent) {
  return el("div", { class: "box" }, [
    el("div", { class: "v" + (accent ? " accent" : ""), text: value }),
    el("div", { class: "k", text: label }),
  ]);
}

async function renderFavorites(root) {
  replaceURL();
  root.appendChild(el("div", { class: "view-head" }, [el("h1", { text: "★ Favorites" })]));
  if (!state.user) { emptyState(root, "Sign in to favorite players and teams."); return; }
  const rows = state.favoriteRows || [];
  const players = rows.filter((r) => r.entity_type === "player");
  const teams = rows.filter((r) => r.entity_type === "team");
  if (!rows.length) {
    emptyState(root, "No favorites yet. Tap the ☆ next to any player or team to add one.");
    return;
  }
  if (teams.length) {
    const card = el("div", { class: "card" });
    card.appendChild(el("div", { class: "card-title" }, ["Teams", el("span", { class: "badge", text: teams.length })]));
    const grid = el("div", { class: "fav-cards" });
    const entries = teams.map((t) => {
      const { cardEl, stats } = favTeamShell(t);
      grid.appendChild(cardEl); return { t, cardEl, stats };
    });
    card.appendChild(grid); root.appendChild(card);
    fillTeamCards(entries);  // async, fills each card's stats area in place
  }
  if (players.length) {
    const card = el("div", { class: "card" });
    card.appendChild(el("div", { class: "card-title" }, ["Players", el("span", { class: "badge", text: players.length })]));
    const grid = el("div", { class: "fav-cards" });
    const entries = players.map((p) => {
      const { cardEl, stats } = favPlayerShell(p);
      grid.appendChild(cardEl); return { p, cardEl, stats };
    });
    card.appendChild(grid); root.appendChild(card);
    fillPlayerCards(entries);
  }
}

function favTeamShell(t) {
  const nameRow = el("div", { class: "name-row" }, [
    el("a", { class: "link name", onclick: () => openTeam(t.entity_id, t.team_short || t.name) }, t.team_short || t.name || "—"),
  ]);
  const head = el("div", { class: "fav-card-head" }, [
    favStar("team", t.entity_id),
    teamLogoImg(t, "fav-card-logo"),
    el("div", { class: "fav-card-title" }, [nameRow, el("div", { class: "muted sub", text: t.conference || "" })]),
  ]);
  const stats = el("div", { class: "fav-card-stats" }); spinner(stats);
  const cardEl = el("div", { class: "fav-card" }, [head, stats]);
  return { cardEl, stats };
}

async function fillTeamCards(entries) {
  const recById = {};
  try {
    (await apiCached("/stats/team-records", { season: state.season }))
      .forEach((r) => { recById[r.team_id] = r; });
  } catch { /* records optional */ }
  const today = new Date().toISOString().slice(0, 10);
  await Promise.all(entries.map(async ({ t, cardEl, stats }) => {
    const rec = recById[t.entity_id];
    const games = await apiCached(`/teams/${t.entity_id}/games`, { season: state.season }).catch(() => []);
    clear(stats);
    if (rec && rec.avca_rank) {
      const chip = rankChip(rec.avca_rank);
      if (chip) cardEl.querySelector(".name-row").append(chip);
    }
    if (rec) {
      const streak = rec.win_streak ? (rec.win_streak > 0 ? "W" : "L") + Math.abs(rec.win_streak) : "—";
      stats.appendChild(el("div", { class: "fav-mini" }, [
        miniBox(`${rec.wins}–${rec.losses}`, "Record", true),
        miniBox(rec.set_pct != null ? fmt(rec.set_pct, 3) : "—", "Set %"),
        miniBox(streak, "Streak"),
      ]));
    }
    const played = games.filter((g) => g.status === "played");
    const last = played.length ? played[played.length - 1] : null;
    if (last) {
      const sc = (last.team_sets_won != null && last.opponent_sets_won != null)
        ? `${last.team_sets_won}–${last.opponent_sets_won}` : "";
      const line = el("div", { class: "fav-last" + (last.contest_id ? " clickable" : "") }, [
        el("span", { class: "muted", text: "Last" }),
        last.result ? el("span", { class: "result " + (last.result === "W" ? "win" : "loss"), text: last.result }) : null,
        el("span", { text: sc }),
        el("span", { class: "muted", text: (last.site === "away" ? "@ " : "vs ") }),
        el("b", { text: last.opponent_short || last.opponent || "—" }),
        el("span", { class: "muted", text: fmtDateShort(last.date) }),
      ]);
      if (last.contest_id) line.addEventListener("click", () => openGame(last.contest_id));
      stats.appendChild(line);
    }
    const next = games.find((g) => g.status === "upcoming" && (g.date || "") >= today)
      || games.find((g) => g.status === "upcoming");
    if (next) {
      stats.appendChild(el("div", { class: "fav-last" }, [
        el("span", { class: "muted", text: "Next" }),
        el("span", { class: "muted", text: (next.site === "away" ? "@ " : "vs ") }),
        el("b", { text: next.opponent_short || next.opponent || "TBD" }),
        el("span", { class: "muted", text: [fmtDateShort(next.date), next.game_time].filter(Boolean).join(" ") }),
      ]));
    }
    if (!rec && !last && !next) stats.appendChild(el("div", { class: "muted", text: "No games yet this season." }));
  }));
}

function favPlayerShell(p) {
  const nameRow = el("div", { class: "name-row" }, [
    el("a", { class: "link name", onclick: () => openPlayer(p.entity_id) }, p.name || "—"),
    p.position ? el("span", { class: "pos-tag", text: p.position }) : null,
  ]);
  const head = el("div", { class: "fav-card-head" }, [
    favStar("player", p.entity_id),
    el("div", { class: "fav-card-title" }, [nameRow, el("div", { class: "muted sub", text: p.team_short || p.team || "" })]),
  ]);
  const stats = el("div", { class: "fav-card-stats" }); spinner(stats);
  const cardEl = el("div", { class: "fav-card" }, [head, stats]);
  return { cardEl, stats };
}

async function fillPlayerCards(entries) {
  await Promise.all(entries.map(async ({ p, stats }) => {
    const ss = await apiCached(`/players/${p.entity_id}/season-stats`, { season: state.season }).catch(() => null);
    const log = await apiCached(`/players/${p.entity_id}/game-log`, { season: state.season }).catch(() => []);
    clear(stats);
    if (!ss) { stats.appendChild(el("div", { class: "muted", text: "No stats yet this season." })); return; }
    // Rank categories by per-set rate; headline the player's best two.
    const top = FAV_PLAYER_CATS
      .map((c) => ({ ...c, per: ss[c.perKey] || 0 }))
      .filter((c) => c.per > 0)
      .sort((a, b) => b.per - a.per)
      .slice(0, 2);
    const mini = el("div", { class: "fav-mini" });
    top.forEach((c, i) => mini.appendChild(miniBox(fmt(c.per, 2), c.label + "/set", i === 0)));
    const ptsPerSet = ss.pts_per_set != null ? ss.pts_per_set : (ss.sp ? (ss.pts || 0) / ss.sp : null);
    mini.appendChild(miniBox(ptsPerSet != null ? fmt(ptsPerSet, 2) : "—", "Pts/set"));
    stats.appendChild(mini);
    // Most recent game with court time, showing the same headline categories' raw counts.
    const lastG = [...log].reverse().find((g) => g.sets);
    if (lastG) {
      const cats = (top.length ? top : FAV_PLAYER_CATS.slice(0, 2))
        .map((c) => `${fmtInt(lastG[c.key])} ${c.ab}`).join(", ");
      const line = el("div", { class: "fav-last" + (lastG.contest_id ? " clickable" : "") }, [
        el("span", { class: "muted", text: "Last" }),
        el("span", { class: "muted", text: "vs" }),
        el("b", { text: lastG.opponent_short || lastG.opponent || "—" }),
        el("span", { class: "muted", text: fmtDateShort(lastG.date) }),
        el("span", { text: "· " + cats }),
      ]);
      if (lastG.contest_id) line.addEventListener("click", () => openGame(lastG.contest_id));
      stats.appendChild(line);
    }
  }));
}

/* ---------- Ask (in-app AI over the stat tools) ---------- */
// The conversation is a single ongoing thread stored server-side (GET/DELETE /ask/history); each
// question replays the stored context, so follow-ups keep continuity across reloads and devices.
async function renderAsk(root) {
  replaceURL();
  root.classList.add("view-ask");  // full-height chat layout (transcript fills, input pinned)
  root.appendChild(el("div", { class: "view-head" }, [
    el("h1", { text: "Ask" }),
    el("div", { class: "spacer" }),
    el("span", { class: "muted", text: "Natural-language questions over the stats" }),
  ]));
  if (!state.user) { emptyState(root, "Sign in to use the AI assistant."); return; }

  let history = [];  // [{role, content, tools?}]
  let busy = false;

  const card = el("div", { class: "card ask-card" });
  const transcript = el("div", { class: "ask-transcript" });
  const input = el("textarea", {
    class: "ask-input", rows: 2,
    placeholder: "e.g. Who are the sophomores with the most kills per set?",
  });
  // Grow the textarea with its content from 2 lines up to 5, then scroll.
  function autoGrow() {
    input.style.height = "auto";
    const cs = getComputedStyle(input);
    const line = parseFloat(cs.lineHeight) || 20;
    const extra = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom)
      + parseFloat(cs.borderTopWidth) + parseFloat(cs.borderBottomWidth);
    const min = line * 2 + extra, max = line * 5 + extra;
    input.style.height = Math.min(Math.max(input.scrollHeight, min), max) + "px";
    input.style.overflowY = input.scrollHeight > max ? "auto" : "hidden";
  }
  input.addEventListener("input", autoGrow);

  // A short primary row of starter questions, plus a "More" popover grouped by theme so the full
  // menu of what the assistant can answer stays discoverable without a giant chip list.
  const ASK_PRIMARY = [
    "Top international players", "MAC kill leaders",
    "First year players with the most assists", "Top passers in the Big Ten",
    "Best teams by set win %",
  ];
  const ASK_MORE = [
    ["Single-game highs", [
      "Most kills in a single match this season",
      "Best single-game dig performances",
      "Who has the most double-doubles?",
      "Any triple-doubles this year?",
    ]],
    ["Rosters & origins", [
      "Which team has the most international players?",
      "Youngest team in the country",
      "Which state sends the most players to the Big Ten?",
      "How many countries are represented in D1?",
    ]],
    ["Size", [
      "Tallest team in the MAC",
      "Tallest players in D1",
      "Which team has the tallest middle blockers?",
    ]],
    ["Teams & standings", [
      "Best hitting team in the Big Ten",
      "Who's ranked #1 in the AVCA poll?",
      "Best quality wins in the Big Ten",
      "What have been the biggest upsets so far?",
      "Which team has the most aces?",
    ]],
    ["Defense", [
      "Best opponent hitting percentage in the MAC",
      "Which teams hold opponents to the lowest hitting %?",
      "Who has beaten the most ranked teams?",
    ]],
  ];
  const askExample = (q) => { input.value = q; autoGrow(); ask(); };

  const examples = el("div", { class: "ask-examples" });
  ASK_PRIMARY.forEach((q) =>
    examples.appendChild(el("button", { class: "chip", onclick: () => askExample(q) }, q)));

  const moreWrap = el("div", { class: "ask-more" });
  const morePanel = el("div", { class: "ask-more-panel" });
  morePanel.hidden = true;
  ASK_MORE.forEach(([label, qs]) => {
    morePanel.appendChild(el("div", { class: "ask-more-group" }, [
      el("div", { class: "ask-more-label", text: label }),
      el("div", { class: "ask-more-chips" }, qs.map((q) =>
        el("button", { class: "chip", onclick: () => { toggleMore(false); askExample(q); } }, q))),
    ]));
  });
  const onDocClick = (e) => { if (!moreWrap.contains(e.target)) toggleMore(false); };
  function toggleMore(force) {
    const show = force === undefined ? morePanel.hidden : force;
    morePanel.hidden = !show;
    moreBtn.textContent = show ? "More ▴" : "More ▾";
    if (show) setTimeout(() => document.addEventListener("click", onDocClick), 0);
    else document.removeEventListener("click", onDocClick);
  }
  const moreBtn = el("button", {
    class: "chip chip-more",
    onclick: (e) => { e.stopPropagation(); toggleMore(); },
  }, "More ▾");
  moreWrap.appendChild(moreBtn);
  moreWrap.appendChild(morePanel);
  examples.appendChild(moreWrap);

  function renderTranscript(thinking) {
    clear(transcript);
    if (!history.length && !thinking) {
      transcript.appendChild(el("div", { class: "muted ask-hint",
        text: "Ask a question to start — follow-ups keep the conversation's context." }));
    }
    history.forEach((m) => {
      const turn = el("div", { class: "ask-turn " + m.role });
      turn.appendChild(el("div", { class: "ask-bubble", text: m.content }));
      if (m.role === "assistant" && m.tools && m.tools.length) {
        turn.appendChild(el("div", { class: "muted ask-tools", text: "Used: " + m.tools.join(", ") }));
      }
      transcript.appendChild(turn);
    });
    if (thinking) {
      transcript.appendChild(el("div", { class: "ask-turn assistant" },
        el("div", { class: "ask-bubble" }, el("span", { class: "spinner", text: "Thinking…" }))));
    }
    transcript.scrollTop = transcript.scrollHeight;
  }

  async function ask() {
    if (busy) return;
    const question = input.value.trim();
    if (!question) return;
    busy = true;
    history.push({ role: "user", content: question });
    input.value = "";
    autoGrow();
    renderTranscript(true);
    try {
      const res = await req("POST", "/ask", { question, season: state.season });
      history.push({ role: "assistant", content: res.answer, tools: res.tools_used || [] });
    } catch (e) {
      history.push({ role: "assistant", content: "Error: " + e.message });
    } finally {
      busy = false;
      renderTranscript(false);
    }
  }

  async function newChat() {
    if (busy) return;
    try { await req("DELETE", "/ask/history"); } catch (e) {}
    history = []; input.value = ""; autoGrow(); renderTranscript(false);
  }

  const askBtn = el("button", { class: "btn", onclick: ask }, "Ask");
  const clearBtn = el("button", { class: "btn ghost", onclick: newChat }, "New chat");
  // Enter sends; Shift+Enter (or Cmd/Ctrl+Enter) inserts a newline.
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.metaKey && !e.ctrlKey) { e.preventDefault(); ask(); }
  });

  card.appendChild(transcript);
  card.appendChild(input);
  card.appendChild(el("div", { class: "ask-actions" }, [askBtn, clearBtn, examples]));
  root.appendChild(card);
  autoGrow(); // size to 2 lines now that the textarea is in the DOM

  // Load the existing thread.
  renderTranscript(false);
  try {
    const rows = await req("GET", "/ask/history");
    if (Array.isArray(rows) && rows.length) {
      history = rows.map((m) => ({ role: m.role, content: m.content, tools: m.tools || [] }));
      renderTranscript(false);
    }
  } catch (e) {}
}

/* ---------- Admin ---------- */
async function renderAdmin(root) {
  replaceURL();
  root.appendChild(el("div", { class: "view-head" }, [el("h1", { text: "Admin" })]));
  if (!(state.user && state.user.is_admin)) { emptyState(root, "Admins only."); return; }

  // Settings: MCP token + global AI key. Values are never returned — only "is set" flags.
  const setCard = el("div", { class: "card" });
  setCard.appendChild(el("div", { class: "card-title", text: "Integrations" }));
  const setBody = el("div", { class: "admin-settings" }); setCard.appendChild(setBody); root.appendChild(setCard);
  spinner(setBody);

  // Users table.
  const userCard = el("div", { class: "card" });
  userCard.appendChild(el("div", { class: "card-title", text: "Users" }));
  const userBody = el("div"); userCard.appendChild(userBody); root.appendChild(userCard);
  spinner(userBody);

  try {
    const s = await api("/admin/settings");
    clear(setBody);
    setBody.appendChild(secretField("MCP access token", "mcp_token", s.has_mcp_token,
      "Bearer token external MCP clients use to reach /mcp.", true));
    setBody.appendChild(secretField("Anthropic API key (AI assistant)", "anthropic_api_key_global", s.has_global_ai_key,
      "Powers the in-app Ask box. Stored server-side, never shown."));
  } catch (e) { clear(setBody); emptyState(setBody, "Error: " + e.message); }

  try {
    const users = await api("/admin/users");
    clear(userBody);
    const table = el("table");
    table.appendChild(el("thead", {}, el("tr", {}, [
      el("th", { class: "l", text: "Email" }), el("th", { class: "l", text: "Name" }),
      el("th", { text: "Admin" }), el("th", { text: "Verified" }),
      el("th", { class: "l", text: "Joined" }), el("th", { text: "" }),
    ])));
    const tb = el("tbody");
    users.forEach((u) => {
      const isSelf = state.user && u.id === state.user.id;
      tb.appendChild(el("tr", {}, [
        el("td", { class: "l", text: u.email }),
        el("td", { class: "l", text: u.name || "—" }),
        el("td", { class: "num" }, adminToggle(u, "is_admin", isSelf)),
        el("td", { class: "num" }, adminToggle(u, "email_verified", false)),
        el("td", { class: "l muted", text: (u.created_at || "").slice(0, 10) }),
        el("td", { class: "num" }, isSelf ? el("span", { class: "muted", text: "you" })
          : el("button", { class: "btn ghost danger", onclick: () => deleteUser(u) }, "Delete")),
      ]));
    });
    table.appendChild(tb);
    userBody.appendChild(table);
  } catch (e) { clear(userBody); emptyState(userBody, "Error: " + e.message); }
}

function adminToggle(u, field, disabled) {
  const cb = el("input", { type: "checkbox" });
  cb.checked = !!u[field];
  cb.disabled = !!disabled;
  cb.addEventListener("change", async () => {
    try { await req("PATCH", `/admin/users/${u.id}`, { [field]: cb.checked }); u[field] = cb.checked; toast("Saved"); }
    catch (e) { cb.checked = !cb.checked; toast("Update failed: " + e.message, true); }
  });
  return cb;
}

async function deleteUser(u) {
  if (!confirm(`Delete ${u.email}? This cannot be undone.`)) return;
  try { await req("DELETE", `/admin/users/${u.id}`); toast("User deleted"); renderAdmin(clear($("#view"))); }
  catch (e) { toast("Delete failed: " + e.message, true); }
}

// A cryptographically-random 64-char hex token (32 bytes), for generating access tokens client-side.
function genToken() {
  const a = new Uint8Array(32);
  (window.crypto || window.msCrypto).getRandomValues(a);
  return Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("");
}

// A masked secret input with save/clear + an "is set" indicator (value never round-trips).
// When `generate` is true, adds a "Generate" button that fills a fresh random token and reveals it
// (so the admin can copy it) — they still click Save to store it.
function secretField(label, key, isSet, hint, generate) {
  const input = el("input", { type: "password", class: "secret-input", placeholder: isSet ? "•••••••• (set)" : "Not set" });
  const status = el("span", { class: "secret-status " + (isSet ? "on" : "off"), text: isSet ? "Set" : "Not set" });
  const save = el("button", { class: "btn", onclick: async () => {
    const v = input.value.trim();
    if (!v) { toast("Enter a value first", true); return; }
    try { await req("PUT", "/admin/settings", { [key]: v }); input.value = ""; input.type = "password"; input.placeholder = "•••••••• (set)"; status.textContent = "Set"; status.className = "secret-status on"; toast("Saved"); }
    catch (e) { toast("Save failed: " + e.message, true); }
  } }, "Save");
  const clr = el("button", { class: "btn ghost", onclick: async () => {
    try { await req("PUT", "/admin/settings", { [key]: "" }); input.value = ""; input.type = "password"; input.placeholder = "Not set"; status.textContent = "Not set"; status.className = "secret-status off"; toast("Cleared"); }
    catch (e) { toast("Clear failed: " + e.message, true); }
  } }, "Clear");
  const controls = [input, save, clr];
  if (generate) {
    controls.push(el("button", { class: "btn ghost", onclick: () => {
      input.value = genToken();
      input.type = "text";  // reveal so it can be copied — it won't be shown again after Save
      toast("Generated — copy it now, then click Save");
    } }, "Generate"));
  }
  controls.push(status);
  return el("div", { class: "secret-row" }, [
    el("div", { class: "secret-label" }, [label, hint ? el("span", { class: "muted secret-hint", text: hint }) : null]),
    el("div", { class: "secret-controls" }, controls),
  ]);
}

/* ---------- verify-email route (#/verify-email?token=…) ---------- */
async function renderVerifyEmail(root) {
  root.appendChild(el("div", { class: "view-head" }, [el("h1", { text: "Email verification" })]));
  const card = el("div", { class: "card" }); const body = el("div"); card.appendChild(body); root.appendChild(card);
  spinner(body);
  const token = state.verifyToken;
  if (!token) { clear(body); emptyState(body, "Missing verification token."); return; }
  try {
    await req("POST", `/auth/email/verify/${encodeURIComponent(token)}`);
    if (state.user) { state.user.email_verified = true; updateVerifyBanner(); }
    clear(body);
    body.appendChild(el("div", { class: "verify-ok", text: "✓ Your email is verified. Thank you!" }));
    body.appendChild(el("button", { class: "btn", onclick: () => setTab("top") }, "Continue"));
  } catch (e) {
    clear(body); emptyState(body, e.message || "This link is invalid or expired.");
  }
}

/* ---------- auth modal (login / register / passkey) ---------- */
function closeAuthModal() { const m = $("#auth-modal"); m.hidden = true; clear(m); }

function openAuthModal(mode) {
  const m = clear($("#auth-modal"));
  m.hidden = false;
  const panel = el("div", { class: "modal" });
  panel.addEventListener("click", (e) => e.stopPropagation());
  m.onclick = closeAuthModal;
  renderAuthForm(panel, mode || "login");
  m.appendChild(panel);
}

// One-time opt-in shown right after a user's first sign-in. Either choice records a decision
// (true/false) via setFantasy, so it never reappears; it's changeable later in Account settings.
function openFantasyPrompt() {
  const m = clear($("#auth-modal"));
  m.hidden = false;
  const panel = el("div", { class: "modal" });
  panel.addEventListener("click", (e) => e.stopPropagation());
  const choose = (on) => { closeAuthModal(); setFantasy(on); };
  m.onclick = () => choose(false);  // dismissing the backdrop counts as "no thanks"
  panel.appendChild(el("div", { class: "modal-head" }, [
    el("h2", { text: "Enable fantasy features?" }),
    el("button", { class: "icon-btn", onclick: () => choose(false), title: "No thanks" }, "×"),
  ]));
  panel.appendChild(el("p", { class: "muted", style: "margin:0 0 16px",
    text: "Fantasy adds a Fantasy Points leaderboard, FP columns on player and team pages, and a "
        + "customizable scoring-weights editor. You can turn it on or off anytime in Account settings." }));
  panel.appendChild(el("div", { class: "modal-actions" }, [
    el("button", { class: "btn primary", onclick: () => choose(true) }, "Enable fantasy"),
    el("button", { class: "btn ghost", onclick: () => choose(false) }, "No thanks"),
  ]));
  m.appendChild(panel);
}

function renderAuthForm(panel, mode) {
  clear(panel);
  const isReg = mode === "register";
  panel.appendChild(el("div", { class: "modal-head" }, [
    el("h2", { text: isReg ? "Create account" : "Sign in" }),
    el("button", { class: "icon-btn", onclick: closeAuthModal, title: "Close" }, "×"),
  ]));

  const email = el("input", { type: "email", placeholder: "you@example.com", autocomplete: "email" });
  const pw = el("input", { type: "password", placeholder: "Password", autocomplete: isReg ? "new-password" : "current-password" });
  const name = el("input", { type: "text", placeholder: "Name (optional)", autocomplete: "name" });
  const errBox = el("div", { class: "form-err", hidden: true });

  function showErr(msg) { errBox.textContent = msg; errBox.hidden = false; }

  async function submit() {
    errBox.hidden = true;
    const e = email.value.trim(), p = pw.value;
    if (!e || !p) { showErr("Email and password are required."); return; }
    if (isReg && p.length < 8) { showErr("Password must be at least 8 characters."); return; }
    try {
      const auth = isReg
        ? await req("POST", "/auth/register", { email: e, password: p, name: name.value.trim() || null })
        : await req("POST", "/auth/login", { email: e, password: p });
      await completeLogin(auth);
    } catch (err) { showErr(err.message); }
  }

  const form = el("div", { class: "auth-form" }, [
    field2("Email", email),
    isReg ? field2("Name", name) : null,
    field2("Password", pw),
    errBox,
    el("button", { class: "btn primary wide", onclick: submit }, isReg ? "Create account" : "Sign in"),
  ]);
  [email, pw, name].forEach((i) => i.addEventListener("keydown", (ev) => { if (ev.key === "Enter") submit(); }));
  panel.appendChild(form);

  // Passkey login (usernameless/discoverable) — only when the browser supports WebAuthn.
  if (window.SimpleWebAuthn && window.SimpleWebAuthn.browserSupportsWebAuthn && window.SimpleWebAuthn.browserSupportsWebAuthn()) {
    panel.appendChild(el("div", { class: "or-sep", text: "or" }));
    panel.appendChild(el("button", { class: "btn ghost wide", onclick: () => passkeyLogin(email.value.trim(), showErr) },
      "🔑 Sign in with a passkey"));
  }

  panel.appendChild(el("div", { class: "modal-foot" }, [
    el("span", { class: "muted", text: isReg ? "Already have an account? " : "New here? " }),
    el("button", { class: "link-btn", onclick: () => renderAuthForm(panel, isReg ? "login" : "register") },
      isReg ? "Sign in" : "Create one"),
  ]));
}

function field2(label, control) {
  return el("label", { class: "form-field" }, [el("span", { text: label }), control]);
}

/* ---------- passkeys (WebAuthn via @simplewebauthn/browser) ---------- */
async function passkeyLogin(email, showErr) {
  const swa = window.SimpleWebAuthn;
  if (!swa) return;
  try {
    const opts = await req("POST", "/auth/passkey/login/start", { email: email || null });
    const assertion = await swa.startAuthentication({ optionsJSON: opts.options });
    const auth = await req("POST", "/auth/passkey/login/finish",
      { request_id: opts.request_id, credential: assertion });
    await completeLogin(auth);
  } catch (e) {
    const msg = "Passkey sign-in failed: " + (e.message || e);
    if (showErr) showErr(msg); else toast(msg, true);
  }
}

async function passkeyRegister() {
  const swa = window.SimpleWebAuthn;
  if (!swa) { toast("Passkeys aren't supported in this browser.", true); return; }
  try {
    const opts = await req("POST", "/auth/passkey/register/start");
    const att = await swa.startRegistration({ optionsJSON: opts.options });
    await req("POST", "/auth/passkey/register/finish", { request_id: opts.request_id, credential: att });
    toast("Passkey added.");
    if ($("#account-modal-open")) openAccountModal();  // refresh the list
  } catch (e) {
    toast("Couldn't add passkey: " + (e.message || e), true);
  }
}

/* ---------- account modal (profile, password, passkeys) ---------- */
function openAccountModal() {
  const d = $(".user-dropdown"); if (d) d.hidden = true;
  const m = clear($("#auth-modal"));
  m.hidden = false;
  const panel = el("div", { class: "modal", id: "account-modal-open" });
  panel.addEventListener("click", (e) => e.stopPropagation());
  m.onclick = closeAuthModal;
  m.appendChild(panel);
  renderAccount(panel);
}

async function renderAccount(panel) {
  clear(panel);
  panel.appendChild(el("div", { class: "modal-head" }, [
    el("h2", { text: "Account" }),
    el("button", { class: "icon-btn", onclick: closeAuthModal, title: "Close" }, "×"),
  ]));
  panel.appendChild(el("div", { class: "muted", text: state.user.email }));

  // Fantasy features (opt-in): the on/off toggle plus, when on, the scoring-weights editor —
  // the weights live here now instead of on the Fantasy tab.
  const fanWrap = el("div", { class: "auth-form" });
  fanWrap.appendChild(el("h3", { text: "Fantasy" }));
  const fanToggle = el("input", { type: "checkbox" });
  fanToggle.checked = fantasyEnabled();
  fanToggle.addEventListener("change", async () => {
    await setFantasy(fanToggle.checked);
    renderAccount(panel);  // reveal/hide the weights editor to match
  });
  fanWrap.appendChild(el("label", { class: "toggle-row" }, [
    fanToggle,
    el("span", { text: "Enable fantasy features (Fantasy tab, FP columns, scoring weights)" }),
  ]));
  if (fantasyEnabled()) fanWrap.appendChild(weightsPanel(() => render()));
  panel.appendChild(fanWrap);

  // Change password.
  const cur = el("input", { type: "password", placeholder: "Current password", autocomplete: "current-password" });
  const nw = el("input", { type: "password", placeholder: "New password (min 8)", autocomplete: "new-password" });
  const pwErr = el("div", { class: "form-err", hidden: true });
  panel.appendChild(el("div", { class: "auth-form" }, [
    el("h3", { text: "Change password" }),
    field2("Current", cur), field2("New", nw), pwErr,
    el("button", { class: "btn", onclick: async () => {
      pwErr.hidden = true;
      if (nw.value.length < 8) { pwErr.textContent = "New password must be at least 8 characters."; pwErr.hidden = false; return; }
      try {
        await req("PATCH", "/auth/me", { current_password: cur.value, new_password: nw.value });
        cur.value = ""; nw.value = ""; toast("Password updated.");
      } catch (e) { pwErr.textContent = e.message; pwErr.hidden = false; }
    } }, "Update password"),
  ]));

  // Passkeys.
  const pkWrap = el("div", { class: "auth-form" });
  pkWrap.appendChild(el("h3", { text: "Passkeys" }));
  if (window.SimpleWebAuthn && window.SimpleWebAuthn.browserSupportsWebAuthn && window.SimpleWebAuthn.browserSupportsWebAuthn()) {
    pkWrap.appendChild(el("button", { class: "btn ghost", onclick: passkeyRegister }, "🔑 Add a passkey"));
  } else {
    pkWrap.appendChild(el("div", { class: "muted", text: "This browser doesn't support passkeys." }));
  }
  const pkList = el("div", { class: "pk-list" }); pkWrap.appendChild(pkList);
  panel.appendChild(pkWrap);
  try {
    const creds = await api("/auth/passkey/credentials");
    clear(pkList);
    if (!creds.length) pkList.appendChild(el("div", { class: "muted", text: "No passkeys yet." }));
    creds.forEach((c) => pkList.appendChild(el("div", { class: "pk-row" }, [
      el("span", { text: c.display_name || "Passkey" }),
      el("span", { class: "muted", text: c.created_at ? c.created_at.slice(0, 10) : "" }),
      el("button", { class: "link-btn danger", onclick: async () => {
        try { await req("DELETE", `/auth/passkey/credentials/${c.id}`); renderAccount(panel); }
        catch (e) { toast("Remove failed: " + e.message, true); }
      } }, "Remove"),
    ])));
  } catch (e) { clear(pkList); pkList.appendChild(el("div", { class: "muted", text: "Couldn't load passkeys." })); }
}

/* ---------- go ---------- */
boot();
