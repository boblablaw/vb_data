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
// Secondary position line ("OH") under a box-score player name — kept tiny so more stat
// columns fit on a phone. Height/class are intentionally omitted here.
const playerMeta = (p) => (p.position ? el("div", { class: "player-meta", text: p.position }) : null);

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
// stats need no floor — the raw total already self-qualifies. The default floor is ADAPTIVE:
// it scales with season progress and is fetched from GET /leaderboards/qualifier per stat+scope;
// only the sample column (`by`) and label live here. The user can override it in the filter box.
const QUALIFIERS = {
  hit_pct:         { by: "attacks", label: "Min attacks" },
  kills_per_set:   { by: "sets", label: "Min sets" },
  assists_per_set: { by: "sets", label: "Min sets" },
  digs_per_set:    { by: "sets", label: "Min sets" },
  aces_per_set:    { by: "sets", label: "Min sets" },
  blocks_per_set:  { by: "sets", label: "Min sets" },
  pts_per_set:     { by: "sets", label: "Min sets" },
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
  // Per-game box-score hitting filters, keyed by contest id then side ("away"/"home"), so each
  // team card filters independently and the selection survives Advanced-toggle / modal re-renders.
  gameFilters: {},
  minSets: 0,
  weights: loadWeights(),
  compare: loadCompare(),
  // Auth / personalization. token+user drive the header and gated tabs; favorites is a Set of
  // "player:<id>" / "team:<id>" keys used to light up ★ markers across every screen.
  token: loadToken(),
  user: null,
  favorites: new Set(),
  // Advanced-stats display toggle (per-set rates + play-by-play stats). Anonymous-friendly and
  // independent of the account fantasy pref, so it's persisted in localStorage.
  adv: loadAdv(),
};

function loadToken() { try { return localStorage.getItem("vb-token") || null; } catch (e) { return null; } }
function loadAdv() { try { return localStorage.getItem("vb-adv") === "1"; } catch (e) { return false; } }
function saveToken(t) {
  state.token = t || null;
  try { t ? localStorage.setItem("vb-token", t) : localStorage.removeItem("vb-token"); } catch (e) {}
}
const favKey = (type, id) => `${type}:${id}`;
const isFav = (type, id) => state.favorites.has(favKey(type, id));
const favIdsByType = (type) =>
  new Set((state.favoriteRows || []).filter((r) => r.entity_type === type).map((r) => r.entity_id));
const favConferenceIds = () => favIdsByType("conference");
const favPlayerIds = () => favIdsByType("player");
const confShortById = (id) => {
  const c = (state.conferences || []).find((x) => x.id === id);
  return c ? (c.short_name || c.name) : null;
};
// Distinct badge colors for favorite-conference pills on the scoreboard. Assigned by the
// conference's position among the (sorted) favorite ids, so each conference keeps the same color
// across every game. Wraps if more conferences are favorited than palette entries.
const CONF_BADGE_COLORS = ["#6d4bd8", "#0ea5a3", "#e0632e", "#c2317f", "#3b7dd8", "#b8860b"];
const confBadgeColor = (id) => {
  const ids = [...favConferenceIds()].sort((a, b) => a - b);
  const i = ids.indexOf(id);
  return CONF_BADGE_COLORS[(i < 0 ? 0 : i) % CONF_BADGE_COLORS.length];
};
// Map of team_id -> list of the user's favorite-player names on that team. Drives the "N Players"
// badge on the scoreboard (count) and its tooltip (names) under the "Favorite players" filter.
// Built entirely from state.favoriteRows already in memory — no extra request.
const favPlayerTeamMap = () => {
  const m = new Map();
  (state.favoriteRows || []).forEach((r) => {
    if (r.entity_type === "player" && r.team_id != null) {
      if (!m.has(r.team_id)) m.set(r.team_id, []);
      m.get(r.team_id).push(r.name || "Player");
    }
  });
  return m;
};

/* ---------- fantasy opt-in (per-user; off by default) ----------
   Fantasy is invisible until a signed-in user opts in. The choice lives in User.prefs.fantasy
   (true / false / absent) and round-trips through /auth/me. Absent = never asked (we prompt on
   first sign-in, and treat as off meanwhile); false = declined/off; true = on. Anonymous visitors
   are always off and never prompted — the prompt is a post-sign-in event. */
function fantasyEnabled() {
  return !!(state.user && state.user.prefs && state.user.prefs.fantasy === true);
}
// Landing tab when the URL names no view. Games leads on every season (a historical season shows
// that season's weeks).
function defaultTab() {
  return "games";
}
// Fantasy features render whenever the user opted in — including on historical seasons, since
// fantasy points derive from per-game stats that exist for all seasons.
function fantasyActive() {
  return fantasyEnabled();
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

/* Advanced-stats display toggle — reveals per-set rates + play-by-play stats across the box score
   and team roster tables at once. A plain display preference (localStorage), so it works signed-out. */
function advEnabled() { return state.adv; }
function setAdv(on) {
  state.adv = !!on;
  try { localStorage.setItem("vb-adv", on ? "1" : "0"); } catch (e) {}
  render();
  // The box-score modal renders outside the main view tree, so render() alone won't refresh it —
  // redraw the open modal in place so the toggle takes effect there too.
  const m = document.getElementById("game-modal");
  if (m && !m.hidden && _gameModalRerender) _gameModalRerender();
}
// A pill that lives in a stat table's card title; flipping it re-renders every stat table in view.
function advToggle() {
  const on = advEnabled();
  return el("button", {
    class: "adv-toggle" + (on ? " on" : ""), type: "button",
    title: "Show advanced stats — per-set rates and play-by-play stats (set attempts, assist %, "
      + "serve efficiency, attack phase splits, points played)",
    onclick: () => setAdv(!advEnabled()),
    text: on ? "Advanced ✓" : "Advanced +",
  });
}

function defaultFilters() {
  return { scope: "season", week: "", conf: "", pos: "", cls: "", stat: "kills", min: null };
}

// The active tab's filter slice (created on demand for any tab that needs one).
function f() {
  return state.filters[state.tab] || (state.filters[state.tab] = defaultFilters());
}

// Active qualifier descriptor for the current Stat-Leaders stat ({by, label}), or null for a
// counting stat. The numeric default is adaptive and fetched from the server in renderTop; the
// slice's `min` (null = use the adaptive default) overrides it.
function activeQualifier() {
  const q = QUALIFIERS[f().stat];
  return q ? { by: q.by, label: q.label } : null;
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
  if (state.user && state.user.email_verified) {
    req("PATCH", "/auth/me", { fantasy_weights: state.weights }).catch(() => {});
  } else if (state.user) {
    // Unverified: the account save is gated, so keep weights locally and nudge to verify.
    try { localStorage.setItem("vb-weights", JSON.stringify(state.weights)); } catch (e) {}
    toast("Verify your email to save fantasy weights to your account", true);
  } else {
    try { localStorage.setItem("vb-weights", JSON.stringify(state.weights)); } catch (e) {}
  }
}
function loadCompare() {
  try { return JSON.parse(localStorage.getItem("vb-compare") || "[]"); } catch (e) { return []; }
}
function saveCompare() { try { localStorage.setItem("vb-compare", JSON.stringify(state.compare)); } catch (e) {} }
// The selected season persists across reloads via localStorage (it's no longer carried in the URL).
function saveSeason() { try { localStorage.setItem("vb-season", String(state.season)); } catch (e) {} }

/* ---------- URL routing (the URL is the source of truth for "where you were") ----------
   The view lives in the location hash — e.g. `#top?scope=week&week=3&stat=kills&
   conf=Southeastern%20Conference&pos=OH`. A refresh re-reads it, so you land on the same tab with
   the same scope/week/filters (and the same open player/team). Season is the one exception: it's
   owned by the topbar selector and persisted to localStorage, not the URL (see viewToHash). Navigation between tabs and detail
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
  // Season is deliberately NOT in the hash — the topbar selector (persisted to localStorage) is its
  // single source of truth. Keeping it out of the URL avoids the selector/content desync that used to
  // show up on Back/Forward, and means links open in whatever season the viewer currently has picked.
  if (cur && cur.scope === "week") { p.set("scope", "week"); if (cur.week) p.set("week", cur.week); }
  if (s.tab === "top") {
    p.set("stat", cur.stat);
    if (cur.conf) p.set("conf", cur.conf);
    if (cur.pos) p.set("pos", cur.pos);
    if (cur.cls) p.set("cls", cur.cls);
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
  "player", "team", "game", "verify-email", "signin"];
function applyHash() {
  const h = location.hash.replace(/^#\/?/, "");  // tolerate both "#tab" and "#/tab" (email links)
  const qi = h.indexOf("?");
  const tab = (qi >= 0 ? h.slice(0, qi) : h) || defaultTab();
  const p = new URLSearchParams(qi >= 0 ? h.slice(qi + 1) : "");
  state.tab = TABS.includes(tab) ? tab : defaultTab();
  if (state.tab === "verify-email") state.verifyToken = p.get("token") || null;
  if (state.tab === "signin") state.signinToken = p.get("token") || null;
  const cur = state.filters[state.tab];  // undefined for compare/player (no filters)

  // Season is not read from the hash (see viewToHash): it's owned by the topbar selector + localStorage.
  if (cur) {
    cur.scope = p.get("scope") === "week" ? "week" : "season";
    const wk = p.get("week");
    if (wk != null) cur.week = wk;  // validated against the season's weeks by refreshWeeks()
    const stat = p.get("stat");
    if (stat && STATS.some((x) => x.key === stat)) cur.stat = stat;
    const conf = p.get("conf");
    cur.conf = conf && state.conferences.some((c) => c.name === conf) ? conf : "";
    cur.pos = p.get("pos") || "";
    cur.cls = p.get("cls") || "";
    const min = p.get("min");
    cur.min = min != null && min !== "" ? Number(min) : null;
    const show = p.get("show");  // Games "Show" picker — persist across refresh
    if (show === "favorites") cur.gamesScope = "fav_teams";  // legacy value
    else if (show && ["all", "fav_teams", "fav_confs", "fav_players", "ranked"].includes(show)) {
      cur.gamesScope = show;
    }
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
  // Restore the last-viewed season from localStorage (the selector is its source of truth; it's no
  // longer in the URL). Defaults to the latest season when unset or stale.
  try {
    const saved = localStorage.getItem("vb-season");
    if (saved != null && state.seasons.some((x) => String(x) === saved)) {
      state.season = typeof state.seasons[0] === "number" ? Number(saved) : saved;
    }
  } catch (e) {}
  await refreshAuth();  // resolve the saved token to a user + favorites before first render
  applyHash();  // parse the initial URL into state (validated against the loaded metadata)
  populateSeasons();
  // Season-derived slices for the (possibly deep-linked) selected season.
  await Promise.all([refreshWeeks(), refreshSeasonConferences()]);
  history.replaceState({ depth: 0 }, "", viewToHash());  // normalize the entry-point URL
  // Back/Forward: re-read the URL and re-render. render() replaceStates the same entry (harmless).
  window.addEventListener("popstate", (e) => {
    historyDepth = e.state && typeof e.state.depth === "number" ? e.state.depth : 0;
    applyHash();
    render();
  });
  render();
  // Pull-to-refresh disabled for now — it fought with normal scrolling on iOS. The implementation
  // (initPullToRefresh) is kept below; re-enable by uncommenting once the gesture is reliable.
  // initPullToRefresh();
}

// Pull-to-refresh for the installed iOS PWA. iOS standalone mode disables Safari's native
// swipe-down reload, so we synthesize it: when the page is scrolled to the very top and the user
// drags down, show an indicator; past a threshold, drop the GET cache and re-render the current
// view. Only wired in standalone mode so the browser tab keeps its normal behaviour.
function initPullToRefresh() {
  const standalone = window.matchMedia("(display-mode: standalone)").matches
    || window.navigator.standalone === true;
  if (!standalone) return;

  const THRESHOLD = 90;    // px of damped pull needed to trigger (≈180px of finger travel)
  const MAX_PULL = 120;    // px cap on how far the indicator travels
  const DEADZONE = 14;     // px of raw movement before we decide the gesture's direction
  const ind = el("div", { class: "ptr-indicator", html: '<span class="ptr-spinner"></span>' });
  document.body.appendChild(ind);

  let startY = 0;
  let startX = 0;
  let pulling = false;     // armed at the top, direction not yet decided
  let captured = false;    // confirmed a deliberate downward pull — now own the gesture
  let refreshing = false;

  const setPull = (dist) => {
    ind.style.transform = `translateX(-50%) translateY(${dist}px)`;
    ind.classList.toggle("ready", dist >= THRESHOLD);
  };
  const reset = () => {
    pulling = false; captured = false;
    ind.style.transform = ""; ind.classList.remove("ready");
  };

  document.addEventListener("touchstart", (e) => {
    if (refreshing || e.touches.length !== 1) return;
    // Never arm pull-to-refresh while a modal is open — its body scrolls independently.
    if (document.body.classList.contains("modal-open")) return;
    // Only arm when already at the very top; otherwise this is a normal scroll gesture.
    if (window.scrollY <= 0) {
      startY = e.touches[0].clientY; startX = e.touches[0].clientX;
      pulling = true; captured = false;
    }
  }, { passive: true });

  document.addEventListener("touchmove", (e) => {
    if (!pulling || refreshing) return;
    const dy = e.touches[0].clientY - startY;
    const dx = e.touches[0].clientX - startX;
    if (!captured) {
      // Wait until the finger clearly commits, then only claim the gesture if it's a downward,
      // predominantly-vertical drag. An upward flick (scroll) or a sideways swipe bails for good,
      // so ordinary scrolling is never hijacked.
      if (Math.abs(dy) < DEADZONE && Math.abs(dx) < DEADZONE) return;
      if (dy <= 0 || Math.abs(dx) > Math.abs(dy)) { pulling = false; return; }
      captured = true;
    }
    // Resist past the cap and suppress the rubber-band scroll while pulling.
    e.preventDefault();
    setPull(Math.min(MAX_PULL, dy * 0.5));
  }, { passive: false });

  const endPull = async () => {
    if (!pulling || !captured) { reset(); return; }
    pulling = false; captured = false;
    const ready = ind.classList.contains("ready");
    if (!ready) { reset(); return; }
    refreshing = true;
    ind.classList.add("spinning");
    setPull(THRESHOLD);
    _getCache.clear();
    try {
      await Promise.all([refreshAuth(), refreshWeeks()]);
      await render();
    } catch (e) {
      /* leave the view as-is on failure */
    } finally {
      ind.classList.remove("spinning", "ready");
      ind.style.transform = "";
      refreshing = false;
    }
  };
  document.addEventListener("touchend", endPull, { passive: true });
  document.addEventListener("touchcancel", () => {
    if (pulling && !refreshing) reset();
  }, { passive: true });
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
  // to the CURRENT week — the latest one that has already started (start <= today). The /weeks list
  // now includes upcoming, unplayed weeks (derived from the schedule), so its tail runs to the end of
  // the season; picking the last entry would land the picker in November. For a past season every
  // week has started, so this resolves to the final week — the same as before. The week dropdown
  // itself is built per screen from state.weeks at render time.
  const numbered = state.weeks.filter((w) => w.week_number != null);
  const today = new Date().toISOString().slice(0, 10);
  let latest = numbered.length ? numbered[0].week_number : "";
  for (const w of numbered) { if (w.start && w.start <= today) latest = w.week_number; }
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
    saveSeason();
    await onSeasonChanged();
  });
}

// The single path a deliberate season switch funnels through, so every season-derived slice is
// re-validated together. Detail screens that are season-specific are handled here: a game belongs to
// exactly one season, so switching bounces off it; the player screen re-resolves in renderPlayerBody.
async function onSeasonChanged() {
  updateTabVisibility();   // Fantasy/Games tabs depend on whether this is the current season
  if (state.tab === "game") { state.contestId = null; state.tab = defaultTab(); }
  // A smaller season could strand a paged view past its end — reset per-tab pagination.
  for (const k in state.filters) { if (state.filters[k].fpOffset != null) state.filters[k].fpOffset = 0; }
  // Snap the week back to the new season's natural default rather than carrying the old season's
  // selected week across: clearing it lets refreshWeeks() re-pick the current week (live season) or
  // the final week (a completed season). Without this, e.g. 2026 wk3 → 2025 would stay on wk3.
  for (const k in state.filters) state.filters[k].week = "";
  await Promise.all([refreshWeeks(), refreshSeasonConferences()]);
  // Drop a held conference filter that this season has no teams in (realignment / new-in-season).
  const names = new Set((state.seasonConferences || []).map((c) => c.name));
  if (names.size) {
    for (const k in state.filters) {
      const fl = state.filters[k];
      if (fl.conf && !names.has(fl.conf)) fl.conf = "";
    }
  }
  // Favorites are per-season — swap in the newly-selected season's set.
  state.favPlayerContests = {};
  if (state.user) await loadFavorites();
  render();
}

// The conferences that had ≥1 team in the selected season (realignment-aware) — drives the conf
// dropdown. Kept separate from state.conferences (the full list, used for name/abbr/logo lookups).
async function refreshSeasonConferences() {
  try { state.seasonConferences = await api("/conferences", { season: state.season }); }
  catch (e) { state.seasonConferences = state.conferences; }
}

function wireTabs() {
  $("#tabs").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-tab]");
    if (!btn) return;
    setTab(btn.dataset.tab);
  });
}

function setTab(tab) {
  closeGameModal();  // a nav from inside the box-score modal (team/player link) dismisses it
  state.tab = tab;
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  navigate();  // a tab switch is a new history entry
  const done = render();
  scrollToTop();
  // Detail views (team/player) fill their header + sections asynchronously; re-assert the top once
  // that layout settles so mobile doesn't end up parked mid-page (e.g. on "Schedule & Results").
  Promise.resolve(done).finally(scrollToTop);
}

// Scroll the window to the very top, retrying on the next frame — some mobile browsers ignore an
// immediate scrollTo issued before the freshly-rendered layout has settled.
function scrollToTop() {
  window.scrollTo(0, 0);
  requestAnimationFrame(() => window.scrollTo(0, 0));
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
          onclick: () => { box.hidden = true; $("#search-input").value = ""; openPlayer(p.id, p.ncaa_player_id); },
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
  // Keep the tab bar in sync with the current season/user on EVERY render — boot, back/forward, and
  // season toggle all funnel through here, so this is the one place that can't fall out of step
  // (e.g. a refresh that restores a historical season before the auth pass ran updateTabVisibility).
  updateTabVisibility();
  if (state.tab === "fantasy" && !fantasyActive()) { setTab("top"); return; }
  const map = {
    top: renderTop, fantasy: renderFantasy, teams: renderTeams,
    games: renderGames, waiver: renderWaiver, compare: renderCompare,
    player: renderPlayer, team: renderTeamDetail, game: renderGame,
    favorites: renderFavorites, ask: renderAsk, admin: renderAdmin,
    "verify-email": renderVerifyEmail, signin: renderSignin,
  };
  return (map[state.tab] || renderTop)(v);
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

/* A conference logo <img>, wrapped in a light chip. The marks are dark, full-color brand logos (no
   light/dark variants), so the chip gives them a light backdrop that reads on either theme. Looks
   the conference up in state.conferences by name or id; returns null when it has no sourced logo. */
function confLogoImg(nameOrId, cls) {
  const c = (state.conferences || []).find((x) => x.id === nameOrId || x.name === nameOrId);
  if (!c || !c.logo) return null;
  return el("span", { class: "conf-logo-chip" + (cls ? " " + cls : "") },
    el("img", {
      class: "conf-logo", src: c.logo, alt: "",
      onerror: (e) => { const chip = e.target.closest(".conf-logo-chip"); if (chip) chip.remove(); },
    }));
}

/* Filters shared by leaderboard-style views. */
function confSelect(value, onchange) {
  const sel = el("select", { onchange: (e) => onchange(e.target.value) });
  sel.appendChild(el("option", { value: "", text: "All conferences" }));
  // Season-scoped list (realignment-aware); falls back to the full list before it has loaded.
  (state.seasonConferences || state.conferences).forEach(
    (c) => sel.appendChild(el("option", { value: c.name, text: confShort(c.name) })));
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
function classSelect(value, onchange) {
  const sel = el("select", { onchange: (e) => onchange(e.target.value) });
  [["", "All classes"], ["Fr", "Freshman"], ["So", "Sophomore"], ["Jr", "Junior"],
   ["Sr", "Senior"]].forEach(([v, l]) =>
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
  // Favorites are per-season and can be managed on any season, including historical ones.
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
  const S = { label: "SP", get: (r) => fmt(r.sets, 0) };
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
  // Standard competition ranking ("1224"): players tied on the displayed sorted value share a
  // rank, and the next distinct value skips ahead — e.g. a 3-way tie for 1st reads 1,1,1,4. Ties
  // are keyed on the *displayed* value (what the reader sees), so equal-looking numbers never get
  // different ranks. Only the FIRST row of a tie group prints its number; the tied rows below it
  // show "—" so the eye reads them as "same as above". The rows arrive already sorted from the API.
  const sortedCol = cols.find((col) => col.sorted);
  const dispVal = (r) => (sortedCol ? sortedCol.get(r) : String(r.value));
  const tb = el("tbody");
  let rank = 0;
  let prevVal = null;
  rows.forEach((r, i) => {
    const v = dispVal(r);
    const tie = i !== 0 && v === prevVal;
    if (!tie) { rank = i + 1; prevVal = v; }
    const nameCell = playerNameCell(r, { hidePos: true });
    nameCell.classList.add("c-player");
    tb.appendChild(el("tr", {}, [
      el("td", { class: "c-rank", text: tie ? "—" : rank }),
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
  // fit-scroll: the (≤200-row) board scrolls vertically inside a window-sized box, sticky header.
  mountFrozenTable(container, leaderTable(rows, statKey), "fit-scroll");
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
  const qual = activeQualifier();  // {by, label} for rate stats, else null

  // Rate boards get an adaptive minimum-sample floor that grows with season progress; fetch the
  // current default so the box shows it. A user override (cur.min, incl. 0 = show all) wins.
  let defMin = 0;
  if (qual) {
    try {
      const q = await api("/leaderboards/qualifier", Object.assign(scopeParams(),
        { stat: cur.stat, conference: cur.conf, position: cur.pos }));
      defMin = (q && q.min) || 0;
    } catch (e) { defMin = 0; }
  }
  const minVal = qual ? (cur.min == null ? defMin : cur.min) : 0;

  const statSel = el("select", { onchange: (e) => {
    cur.stat = e.target.value;
    cur.min = null;  // reset to the new stat's adaptive default qualifier
    renderTop(clear(root));
  } });
  STATS.forEach((s) => statSel.appendChild(el("option", { value: s.key, text: s.label })));
  statSel.value = cur.stat;

  const filters = [
    ...scopeFields(() => renderTop(clear(root))),
    field("Stat", statSel),
    field("Conference", confSelect(cur.conf, (v) => { cur.conf = v; renderTop(clear(root)); })),
    field("Position", posSelect(cur.pos, (v) => { cur.pos = v; renderTop(clear(root)); })),
    field("Class", classSelect(cur.cls, (v) => { cur.cls = v; renderTop(clear(root)); })),
  ];
  if (qual) {
    const minInp = el("input", {
      type: "number", min: 0, step: 1, value: minVal, style: "width:80px",
      title: `Min ${qual.by} to qualify — scales with games played (default ${defMin}); type to override`,
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
      stat: cur.stat, conference: cur.conf, position: cur.pos, class_year: cur.cls, limit: 200,
    });
    if (qual) params[qual.by === "attacks" ? "min_attacks" : "min_sets"] = minVal;
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
  // Expanded by default: it only shows once fantasy is enabled, so the weights are the point.
  const wrap = el("details", { class: "weights-wrap", open: true });
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

// The fantasy board is paged (the full ~4.6k-player composite is heavy to load at once). Each page
// is 200 rows that scroll vertically inside a window-sized box; Search is server-side (a `q` param),
// debounced, and resets to page 1. Only the table + pager re-render on search, so the box keeps focus.
const FANTASY_PAGE_SIZE = 200;

async function renderFantasy(root) {
  replaceURL();
  const cur = f();
  if (cur.fpOffset == null) cur.fpOffset = 0;

  // Scope/conference/position changes rebuild the whole view (and reset to page 1).
  const reload = () => { cur.fpOffset = 0; renderFantasy(clear(root)); };
  const search = el("input", {
    type: "search", class: "table-search", placeholder: "Search player or team…",
    value: cur.fpQuery || "",
  });

  const count = el("span", { class: "muted table-hint" });
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

  const card = el("div", { class: "card" }, el("div", { class: "card-title" }, [
    "Fantasy leaders", el("span", { class: "badge", text: scopeLabel() }), count,
  ]));
  root.appendChild(card);
  const body = el("div"); card.appendChild(body);
  const pager = el("div", { class: "pager" }); card.appendChild(pager);

  // Fetch + render one page into `body` (and rebuild the pager) without touching the header, so the
  // search box keeps focus across keystrokes.
  async function loadPage() {
    clear(pager);
    clear(body); spinner(body);
    try {
      const params = Object.assign(scopeParams(), weightParams(), {
        conference: cur.conf, position: cur.pos, min_sets: state.minSets,
        limit: FANTASY_PAGE_SIZE + 1, offset: cur.fpOffset,
      });
      if (cur.fpQuery) params.q = cur.fpQuery;
      const raw = await api("/leaderboards/fantasy", params);
      const hasNext = raw.length > FANTASY_PAGE_SIZE;
      const rows = hasNext ? raw.slice(0, FANTASY_PAGE_SIZE) : raw;
      clear(body);
      if (!rows.length) {
        emptyState(body, cur.fpQuery ? "No players match your search." : "No data for this selection.");
        count.textContent = "";
        return;
      }

      const table = el("table", { class: "leader-table wide-table" });
      table.appendChild(el("thead", {}, el("tr", {}, [
        el("th", { class: "c-rank", text: "#" }),
        el("th", { class: "l c-player", text: "Player" }),
        el("th", { class: "l", text: "Team" }), el("th", { class: "l", text: "Conf" }),
        el("th", { class: "num", text: "GP" }), el("th", { class: "num", text: "Sets" }),
        el("th", { class: "num sorted", text: "FP" }), el("th", { class: "num", text: "FP/set" }),
      ])));
      const tb = el("tbody");
      rows.forEach((r, i) => {
        const fpps = r.sets ? r.value / r.sets : null;
        const nameCell = playerNameCell(r);
        nameCell.classList.add("c-player");
        tb.appendChild(el("tr", {}, [
          el("td", { class: "c-rank", text: cur.fpOffset + i + 1 }),
          nameCell,
          teamLogoCell(r),
          el("td", { class: "l muted", text: r.conference || "—" }),
          el("td", { class: "num", text: fmtInt(r.games) }),
          el("td", { class: "num", text: fmt(r.sets, 0) }),
          el("td", { class: "num sorted", text: fmt(r.value, 1) }),
          el("td", { class: "num", text: fmt(fpps, 2) }),
        ]));
      });
      table.appendChild(tb);
      mountFrozenTable(body, table, "fit-scroll");

      const first = cur.fpOffset + 1, last = cur.fpOffset + rows.length;
      count.textContent = `${first}–${last}`;
      clear(pager);
      pager.appendChild(el("button", {
        class: "btn ghost", disabled: cur.fpOffset === 0,
        onclick: () => { cur.fpOffset = Math.max(0, cur.fpOffset - FANTASY_PAGE_SIZE); loadPage(); },
      }, "‹ Prev"));
      pager.appendChild(el("span", { class: "pager-info", text: `${first}–${last}` }));
      pager.appendChild(el("button", {
        class: "btn ghost", disabled: !hasNext,
        onclick: () => { cur.fpOffset += FANTASY_PAGE_SIZE; loadPage(); },
      }, "Next ›"));
    } catch (e) {
      clear(body); emptyState(body, "Error: " + e.message);
    }
  }

  // Server-side search: debounce keystrokes, reset to page 1, refetch just the table.
  let searchTimer = null;
  search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      const v = search.value.trim();
      if (v === (cur.fpQuery || "")) return;
      cur.fpQuery = v;
      cur.fpOffset = 0;
      loadPage();
    }, 250);
  });

  loadPage();
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
    // Favorited conferences float to the top (then the rest alphabetically). Group keys are
    // conference *names*; favorites store ids, so map through state.conferences.
    const confId = (name) => (state.conferences.find((c) => c.name === name) || {}).id;
    const favConfs = favConferenceIds();
    const isFavConf = (name) => favConfs.has(confId(name));
    Object.keys(groups).sort((a, b) =>
      (isFavConf(b) - isFavConf(a)) || a.localeCompare(b)
    ).forEach((conf) => {
      const cid = confId(conf);
      const card = el("div", { class: "card conf-group" });
      card.appendChild(el("div", { class: "card-title" }, [
        favStar("conference", cid),
        confLogoImg(cid ?? conf, "conf-logo-head"),
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

  // Fantasy card first — only when the user has fantasy enabled (and on the current season).
  if (fantasyActive()) {
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
  // Standard competition ranking with "same as above" ties: rows sharing the displayed value share
  // a rank; only the first row of a tie group prints its number, the rest show "—" (e.g. 2, 2 -> 2, —).
  const tb = el("tbody");
  let rank = 0;
  let prevVal = null;
  rows.forEach((r, i) => {
    const v = valFn(r);
    const tie = i !== 0 && v === prevVal;
    if (!tie) { rank = i + 1; prevVal = v; }
    tb.appendChild(el("tr", {}, [
      el("td", { text: tie ? "—" : rank }),
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
// `r` is the current-season resolution ({ seasonId, team } or null) so the name links to — and the
// sub-label shows — the player as they were that season (handles transfers between seasons).
function comparePlayerCard(c, root, r) {
  const pid = (r && r.seasonId) || c.id;
  const teamLabel = (r && r.team) || c.team || "";
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
    playerHeadshot({ photo_path: (r && r.photo_path) || null, name: c.name }, "compare-card-photo"),
    el("div", { class: "compare-card-name" },
      el("a", { class: "link", onclick: () => openPlayer(pid, c.ncaa_player_id) }, c.name)),
    el("div", { class: "muted compare-card-sub", text: teamLabel }),
    stats,
  ]);
  return { cardEl, stats };
}

// Resolve a stored compare entry into the currently selected season. A player's id is season-specific
// (NCAA reissues both the player id and its ncaa_player_id every year), so a stored id only has stats
// for the season it was added in and no id bridges seasons. Load the stored id to recover durable
// identity (name + hometown + high_school + team), then resolve THAT into the selected season.
// Returns { seasonId, team, photo_path } for the season, or null if the player didn't play it.
async function resolveCompareEntry(c) {
  if (c.id == null) return null;
  let base;
  try { base = await api(`/players/${c.id}`); }
  catch (e) { return null; }  // stored id no longer exists
  if (base.season === state.season) {
    return { seasonId: base.id, team: base.team_short || base.team, photo_path: base.photo_path };
  }
  try {
    const p = await api("/players/resolve", {
      season: state.season, name: base.name,
      hometown: base.hometown, high_school: base.high_school, team_id: base.team_id,
    });
    return { seasonId: p.id, team: p.team_short || p.team, photo_path: p.photo_path };
  } catch (e) { return null; }  // didn't play this season
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
          .filter((p) => !state.compare.some((c) => c.id === p.id || (p.ncaa_player_id && c.ncaa_player_id === p.ncaa_player_id)))
          .slice(0, 8);
        if (!players.length) { results.appendChild(el("div", { class: "muted", text: "No players" })); return; }
        players.forEach((p) => results.appendChild(el("div", {
          class: "compare-result",
          onclick: () => { addToCompare(p.id, p.name, p.team_short || p.team, p.ncaa_player_id); renderCompare(clear(root)); },
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

  // Re-resolve each compared person into the selected season (same player → different id per season)
  // before rendering, so switching seasons shows the right season's line without a remove/re-add.
  const resolved = await Promise.all(state.compare.map(resolveCompareEntry));

  // A card per added player (each with its own stat line) + an "add player" search card until full.
  const grid = el("div", { class: "compare-slots" });
  const entries = state.compare.map((c, i) => {
    const { cardEl, stats } = comparePlayerCard(c, root, resolved[i]);
    grid.appendChild(cardEl); return { c, stats };
  });
  if (state.compare.length < COMPARE_MAX) grid.appendChild(addPlayerCard(root));
  root.appendChild(grid);

  if (!state.compare.length) return;  // add card is shown above; nothing to fill yet

  const stats = await Promise.all(resolved.map((r) =>
    r && r.seasonId
      ? api(`/players/${r.seasonId}/season-stats`, { season: state.season }).catch(() => null)
      : Promise.resolve(null)));
  // Fantasy Points is appended only when fantasy is active (opted in + not a historical season).
  const rows = fantasyActive()
    ? [...COMPARE_ROWS, ["FP", (s) => fmt(fantasyOf(s), 1)]]
    : COMPARE_ROWS;
  entries.forEach(({ stats: sEl }, i) => {
    clear(sEl);
    const ss = stats[i];
    if (!ss) { sEl.appendChild(el("div", { class: "muted", text: `No stats for ${state.season}` })); return; }
    rows.forEach(([label, fn]) => sEl.appendChild(el("div", { class: "compare-stat" }, [
      el("span", { class: "k", text: label }),
      el("span", { class: "v", text: fn(ss) }),
    ])));
  });
}

function addToCompare(id, name, team, ncaaId) {
  // Dedup by the stable cross-season key when we have it, else by the season-specific id.
  const dup = state.compare.some((c) => c.id === id || (ncaaId && c.ncaa_player_id === ncaaId));
  if (dup) { toast(`${name} already in compare`); return; }
  if (state.compare.length >= COMPARE_MAX) { toast(`Compare holds up to ${COMPARE_MAX} players`, true); return; }
  state.compare.push({ id, name, team: team || null, ncaa_player_id: ncaaId || null });
  saveCompare();
  toast(`Added ${name} to compare`);
}

/* ---------- Player detail ---------- */
// `id` is a season-specific player id. It stays valid only for its own season; renderPlayerBody
// re-resolves the same person into the selected season by durable identity after a season switch
// (NCAA reissues every player id per season). The legacy `ncaaId` arg is ignored — kept so existing
// callers don't need touching.
async function openPlayer(id, ncaaId) {  // eslint-disable-line no-unused-vars
  state.playerId = id;
  setTab("player");
}

async function renderPlayer(root) {
  replaceURL();
  root.appendChild(el("div", { class: "back-link" },
    el("a", { class: "link", onclick: () => goBack("top") }, "← Back")));
  const holder = el("div"); root.appendChild(holder);
  await renderPlayerBody(holder, state.playerId);
}

// Player detail content (head + comprehensive game-log stats table), rendered into `holder`. Split out of
// renderPlayer so the box-score modal can drill into a player without leaving the overlay. It does
// NOT touch the URL or add a back-link — the caller owns navigation chrome.
async function renderPlayerBody(holder, id) {
  spinner(holder);
  try {
    // A player's id is season-specific — NCAA reissues both the player id AND its ncaa_player_id
    // every year, so `id` only carries stats for the season it was opened in and no id bridges
    // seasons. Load the opened id to get durable identity (name + hometown + high_school + team),
    // then resolve THAT into the selected season (mirrors resolveCompareEntry) so switching seasons
    // shows the right season's line instead of an empty one. Skip the extra hop when the opened id
    // already belongs to the selected season.
    const base = await api(`/players/${id}`);
    let seasonId = id, playedSeason = true;
    if (base.season !== state.season) {
      try {
        const r = await api("/players/resolve", {
          season: state.season, name: base.name,
          hometown: base.hometown, high_school: base.high_school, team_id: base.team_id,
        });
        seasonId = r.id;
      } catch (e) { playedSeason = false; }  // didn't play the selected season
    }
    if (playedSeason) { state.playerId = seasonId; replaceURL(); }

    const [p, ss, log, tr] = await Promise.all([
      seasonId === id ? Promise.resolve(base) : api(`/players/${seasonId}`),
      playedSeason ? api(`/players/${seasonId}/season-stats`, { season: state.season }).catch(() => null) : null,
      playedSeason ? api(`/players/${seasonId}/game-log`, { season: state.season }).catch(() => []) : [],
      playedSeason ? api(`/players/${seasonId}/transfer`).catch(() => null) : null,
    ]);
    clear(holder);

    const meta = [p.position, p.class_year, heightStr(p.height_inches), p.hometown].filter(Boolean).join(" · ");
    // "Previous school" line for a transfer: the team the same person played for the prior season.
    const prevName = tr && tr.transferred ? (tr.previous_team_short || tr.previous_team) : null;
    const prevLine = prevName ? el("div", { class: "player-head-transfer" }, [
      el("span", { class: "meta", text: "Previous school: " }),
      tr.previous_team_id
        ? el("a", { class: "link", onclick: () => openTeam(tr.previous_team_id, prevName) }, prevName)
        : el("span", { text: prevName }),
    ]) : null;
    holder.appendChild(el("div", { class: "player-head" }, [
      playerHeadshot(p),
      el("div", { class: "player-head-main" }, [
        el("h1", { text: p.name }),
        el("div", { class: "player-head-sub" }, [
          p.team_id ? el("a", { class: "link", onclick: () => openTeam(p.team_id, p.team_short || p.team) }, (p.team_short || p.team) || "") : el("span", { text: (p.team_short || p.team) || "" }),
          el("span", { class: "meta", text: meta }),
        ]),
        ...(prevLine ? [prevLine] : []),
      ]),
      el("div", { class: "spacer", style: "flex:1" }),
      favBtn("player", p.id),
      el("button", { class: "btn ghost", onclick: () => addToCompare(p.id, p.name, p.team_short || p.team, p.ncaa_player_id) }, "＋ Compare"),
    ]));

    // Game log — the single comprehensive stats table. It carries every box-score + advanced
    // column (same machinery as the team roster table, behind the Advanced toggle); its
    // season-total footer row is the player's cumulative line, so there's no separate stat-card
    // grid or cumulative table.
    const card = el("div", { class: "card" });
    card.appendChild(el("div", { class: "card-title" }, ["Game log", advToggle()]));
    if (!playedSeason) card.appendChild(el("div", { class: "empty-state", text: `Did not play in ${state.season}.` }));
    else if (!log.length) card.appendChild(el("div", { class: "empty-state", text: "No games recorded." }));
    else card.appendChild(gameLogTable(log, ss));
    holder.appendChild(card);
  } catch (e) {
    clear(holder); emptyState(holder, "Error: " + e.message);
  }
}

// Player headshot for the detail header: the scraped 2026 photo when present, else an initials
// monogram. A broken/missing photo file also falls back to the monogram (mirrors teamLogoImg).
// `extraClass` adds a size/context modifier (e.g. "sm" on the compare/favorites cards) to both the
// photo and its monogram fallback so they stay the same size after an onerror swap.
function playerHeadshot(p, extraClass) {
  const mono = playerMonogram(p.name, extraClass);
  if (p.photo_path) {
    return el("img", {
      class: "player-photo" + (extraClass ? " " + extraClass : ""),
      src: "/ui/" + p.photo_path,
      alt: p.name || "",
      loading: "lazy",
      onerror: (e) => e.target.replaceWith(mono),
    });
  }
  return mono;
}

// Colored circle with the player's initials — the missing-photo placeholder. Hue is derived from
// the name so a player's monogram color stays consistent across visits.
function playerMonogram(name, extraClass) {
  const parts = (name || "").trim().split(/\s+/).filter(Boolean);
  const initials = ((parts[0] || "")[0] || "") + (parts.length > 1 ? (parts[parts.length - 1][0] || "") : "");
  let h = 0;
  for (let i = 0; i < (name || "").length; i++) h = (h * 31 + name.charCodeAt(i)) % 360;
  return el("div", { class: "player-monogram" + (extraClass ? " " + extraClass : ""), style: `--mono-h:${h}`, "aria-hidden": "true" },
    initials.toUpperCase() || "?");
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

// Columns visible right now, given the toggles + table context:
//  - fp  cols (Fantasy points) show only when fantasy is on
//  - adv cols (per-set rates + play-by-play stats) show only when the Advanced toggle is on
//  - teamOnly cols show only on the season roster table (ctx "team"); the per-game box ("box")
//    additionally shows the bio cols (Pos/Cls/Ht) but not Games-played (which is season-only)
const visibleCols = (cols, ctx) => cols.filter((c) =>
  (!c.fp || fantasyActive())
  && (!c.adv || advEnabled())
  && (!c.teamOnly || ctx === "team" || (c.bio && ctx === "box")));
function statCell(col, row) {
  const v = col.calc ? col.calc(row) : row[col.key];
  // `str` columns (bio: position, class, height) render their value verbatim and centered (to match
  // the box-table's centered numeric columns), no numeric format.
  if (col.str) {
    return el("td", {
      class: "center muted" + (col.grpStart ? " grp-start" : ""),
      text: v == null || v === "" ? "—" : String(v),
    });
  }
  return el("td", {
    class: "num" + (col.grpStart ? " grp-start" : ""),
    text: col.int ? fmtInt(v) : fmt(v, col.d),
  });
}

// The player game log: the single comprehensive stats table. It reuses the team roster table's
// grouped header + column set (statHead/STAT_COLS/statCell) so every box-score and advanced column
// is available behind the Advanced toggle, with leading Opponent/Wk/Date columns and a season-total
// footer row that doubles as the player's cumulative line (no separate stat cards / totals table).
function gameLogTable(log, ss) {
  const head = statHead(null, (c) => el("th", { text: c.label, title: c.title || c.label }));
  const grpTr = head.rows[0];
  grpTr.firstChild.textContent = "Opponent";                 // relabel the sticky leading column
  grpTr.insertBefore(el("th", { class: "l", rowspan: 2, text: "Date" }), grpTr.children[1]);
  grpTr.insertBefore(el("th", { class: "l", rowspan: 2, text: "Wk" }), grpTr.children[1]);
  const cols = head.cols;
  const table = el("table", { class: "wide-table dense-table box-table" });
  table.appendChild(el("thead", {}, head.rows));

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

// The public NCAA game page. NOTE: this takes ncaa.com's OWN game id (g.ncaa_game_id), which is a
// SEPARATE id system from our stats.ncaa.org contest_id — passing contest_id here lands on an
// unrelated game (often another sport). The id is backfilled by `vb map-ncaa-games`.
function ncaaGameUrl(ncaaId) {
  return `https://www.ncaa.com/game/${ncaaId}`;
}

// NCAA publishes and stores all game times in US Eastern wall-clock ("07:30 PM"), verified against
// ncaa.com (e.g. startDate "2026-09-03T15:00:00-04:00" == our stored "03:00 PM"). We display that
// time verbatim for every viewer, only appending the correct Eastern label ("EDT" in season, "EST"
// after the early-November switch) so it reads unambiguously regardless of the viewer's own zone.
// Falls back to the raw string if either part doesn't parse.
function fmtGameTime(dateStr, timeStr) {
  const dm = /^(\d{4})-(\d{2})-(\d{2})/.exec(dateStr || "");
  let hour24, minute;
  const tm = /^(\d{1,2}):(\d{2})\s*([AP]M)$/i.exec((timeStr || "").trim());
  if (tm) {
    hour24 = (Number(tm[1]) % 12) + (/PM/i.test(tm[3]) ? 12 : 0);
    minute = tm[2];
  } else {
    // Played contests carry no separate game_time; their start lives as a 24h suffix on the date
    // ("2026-09-06 20:00", Eastern), so parse that too — otherwise completed cards read "TBD".
    // Treat 00:00 as "time unknown" (the usual sentinel) rather than a real midnight game.
    const dt = /^\d{4}-\d{2}-\d{2}[ T](\d{1,2}):(\d{2})/.exec(dateStr || "");
    if (dt && !(dt[1] === "00" && dt[2] === "00")) { hour24 = Number(dt[1]); minute = dt[2]; }
  }
  // "12:00 AM" is the no-published-tip sentinel (ncaa.com sometimes omits the time), NOT a real
  // midnight game — show TBD rather than a bogus "12:00 AM EDT", matching the 00:00 sentinel above.
  if (hour24 === 0 && minute === "00") return "TBD";
  if (!dm || hour24 == null || minute == null) return timeStr || "TBD";
  const hour12 = hour24 % 12 || 12;
  const ampm = hour24 < 12 ? "AM" : "PM";
  // Ask Intl what Eastern's short zone name is on this date (noon UTC shares the day's DST state),
  // so the label flips EDT→EST automatically at the seasonal boundary.
  const noon = new Date(Date.UTC(Number(dm[1]), Number(dm[2]) - 1, Number(dm[3]), 12, 0));
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", timeZoneName: "short",
  }).formatToParts(noon);
  const zone = (parts.find((x) => x.type === "timeZoneName") || {}).value || "ET";
  return `${hour12}:${minute} ${ampm} ${zone}`;
}

// ── Unified stat-column model ────────────────────────────────────────────────────────────────
// ONE grouped definition drives BOTH the per-game box score and the season roster table, so the two
// always show the same columns in the same order (only the values differ). Columns are organised
// into stat categories (Hitting / Setting / Serving / Passing / Defense / Blocks) rendered as a
// grouped two-row header. Flags: `int` integer display, `d` decimal places, `calc` derives a value
// from the row, `adv` hidden until the Advanced toggle is on, `fp` shown only when fantasy is on,
// `teamOnly` shown only on the season table (never the per-game box). Per-set rates and hit%/blocks
// are `calc`-derived from the base counts + sets so they're identical in both tables and the totals.
const perSet = (key) => (r) => (r.sets ? (Number(r[key]) || 0) / r.sets : null);
const totalBlocksOf = (r) => (Number(r.block_solos) || 0) + (Number(r.block_assists) || 0);
// A block assist is credited to every player on the block, so summing per-player totals for a TEAM
// double-counts assisted blocks. NCAA's official team figure halves block assists. Totals-row
// objects set an explicit (halved) `total_blocks`; individual player rows either carry their own
// whole-number total_blocks from the backend or fall back to solos+assists — both correct per-player.
const teamBlocksOf = (r) => (Number(r.block_solos) || 0) + (Number(r.block_assists) || 0) / 2;
const blocksOf = (r) => (Number.isFinite(r.total_blocks) ? Number(r.total_blocks) : totalBlocksOf(r));
const STAT_GROUPS = [
  // Bio group (its own group so a separator falls between it and GP). `bio` cols also show in the
  // per-game box score (ctx "box"), unlike Games-played which is season-only.
  { label: "", cols: [
    { key: "position", label: "Pos", title: "Position", str: true, teamOnly: true, bio: true },
    { key: "class_year", label: "Cls", title: "Class year", str: true, teamOnly: true, bio: true },
    { key: "height_inches", label: "Ht", title: "Height", str: true, teamOnly: true, bio: true,
      calc: (r) => heightStr(r.height_inches) },
  ] },
  { label: "", cols: [
    { key: "games", label: "GP", title: "Games played", int: true, teamOnly: true },
    { key: "sets", label: "SP", title: "Sets played", d: 0 },
  ] },
  { label: "Hitting", cols: [
    { key: "kills", label: "K", title: "Kills", int: true },
    { key: "errors", label: "E", title: "Attack errors", int: true },
    { key: "total_attacks", label: "TA", title: "Total attacks", int: true },
    { key: "hit_pct", label: "Hit%", title: "Hitting percentage — (kills − errors) ÷ attacks", d: 3, calc: hitPct },
    { key: "kill_pct", label: "Kill%", title: "Kill percentage — percent of attack attempts that end in a kill (kills ÷ attempts)", d: 3, adv: true,
      calc: (r) => { const ta = Number(r.total_attacks) || 0; return ta > 0 ? (Number(r.kills) || 0) / ta : null; } },
    { key: "atk_pct_fbso", label: "FBSO %", title: "First-ball side-out attack efficiency — (kills − errors) ÷ (attacks − errors), off a serve reception (play-by-play)", d: 3, adv: true,
      calc: (r) => { const ta = Number(r.fbso_attacks) || 0, e = Number(r.fbso_errors) || 0, k = Number(r.fbso_kills) || 0; return (ta - e) > 0 ? (k - e) / (ta - e) : null; } },
    { key: "atk_pct_trans", label: "TRANS %", title: "Transition attack efficiency — (kills − errors) ÷ (attacks − errors), all non-first-ball attacks (play-by-play)", d: 3, adv: true,
      calc: (r) => { const ta = Number(r.trans_attacks) || 0, e = Number(r.trans_errors) || 0, k = Number(r.trans_kills) || 0; return (ta - e) > 0 ? (k - e) / (ta - e) : null; } },
    { key: "kills_per_set", label: "K/S", title: "Kills per set", d: 2, adv: true, calc: perSet("kills") },
  ] },
  { label: "Setting", cols: [
    { key: "assists", label: "A", title: "Assists", int: true },
    { key: "set_attempts", label: "ATT", title: "Set attempts — every set touch (play-by-play)", int: true, adv: true },
    { key: "assist_pct", label: "A%", title: "Assist % — assists ÷ set attempts", d: 3, adv: true,
      calc: (r) => (r.set_attempts ? (Number(r.assists) || 0) / r.set_attempts : null) },
    { key: "assists_per_set", label: "A/S", title: "Assists per set", d: 2, adv: true, calc: perSet("assists") },
    { key: "bhe", label: "BHE", title: "Ball-handling errors", int: true },
  ] },
  { label: "Serving", cols: [
    { key: "serve_attempts", label: "ATT", title: "Serve attempts — every serve taken (play-by-play)", int: true, adv: true },
    { key: "aces", label: "SA", title: "Service aces", int: true },
    { key: "serr", label: "SE", title: "Service errors", int: true },
    { key: "ace_pct", label: "Ace%", title: "Ace % — aces per serve attempt (play-by-play)", d: 3, adv: true,
      calc: (r) => (r.serve_attempts ? (Number(r.aces) || 0) / r.serve_attempts : null) },
    { key: "serve_eff", label: "SEff", title: "Serve efficiency — (aces − service errors) / serve attempts", d: 3, adv: true,
      calc: (r) => (r.serve_attempts ? ((Number(r.aces) || 0) - (Number(r.serr) || 0)) / r.serve_attempts : null) },
    { key: "aces_per_set", label: "SA/S", title: "Service aces per set", d: 2, adv: true, calc: perSet("aces") },
  ] },
  { label: "Passing", cols: [
    { key: "retatt", label: "RC", title: "Reception attempts", int: true },
    { key: "rerr", label: "RE", title: "Reception errors", int: true },
    { key: "rec_pct", label: "Rec%", title: "Reception % — (reception attempts − errors) / attempts", d: 3, adv: true,
      calc: (r) => (r.retatt ? ((Number(r.retatt) || 0) - (Number(r.rerr) || 0)) / r.retatt : null) },
  ] },
  { label: "Defense", cols: [
    { key: "digs", label: "D", title: "Digs", int: true },
    { key: "digs_per_set", label: "D/S", title: "Digs per set", d: 2, adv: true, calc: perSet("digs") },
  ] },
  { label: "Blocks", cols: [
    { key: "block_solos", label: "BS", title: "Block solos", int: true },
    { key: "block_assists", label: "BA", title: "Block assists", int: true },
    { key: "total_blocks", label: "TB", title: "Total blocks", int: true, calc: blocksOf },
    { key: "berr", label: "BE", title: "Block errors", int: true },
    { key: "blk_pct", label: "Blk%", title: "Block % — total blocks / (total blocks + block errors)", d: 3, adv: true,
      calc: (r) => { const tb = blocksOf(r), be = Number(r.berr) || 0; return (tb + be) ? tb / (tb + be) : null; } },
    { key: "blocks_per_set", label: "B/S", title: "Blocks per set", d: 2, adv: true,
      calc: (r) => (r.sets ? blocksOf(r) / r.sets : null) },
  ] },
  { label: "Points", cols: [
    { key: "pts", label: "Pts", title: "Points", d: 1 },
    { key: "pts_per_set", label: "P/S", title: "Points per set", d: 2, adv: true, calc: perSet("pts") },
    { key: "points_played", label: "PP", title: "Points played — rallies on court (derived from subs; approximate)", int: true, adv: true },
    { key: "fantasy_points", label: "FP", title: "Fantasy points", d: 1, fp: true, calc: fantasyOf },
  ] },
];
const STAT_COLS = STAT_GROUPS.flatMap((g) => g.cols);
// Additive columns summed for a table's totals row (per-set rates / hit% / FP recompute via calc).
const STAT_SUM_KEYS = [
  "kills", "errors", "total_attacks", "assists", "set_attempts", "serve_attempts",
  "aces", "serr", "digs",
  "retatt", "rerr", "block_solos", "block_assists", "berr", "bhe", "pts",
  // Phase-split attack counts so the ATK% FBSO / ATK% TRANS totals recompute via calc.
  "fbso_kills", "fbso_errors", "fbso_attacks", "trans_kills", "trans_errors", "trans_attacks",
];

// Build a grouped two-row header (category labels over column labels) for a stat table. `ctx` is
// "team" for the season roster (enables teamOnly cols), null for the per-game box score. `colTh`
// builds each column's <th> (box score: plain; team table: sortable). Returns the two header rows
// plus the flat list of visible columns to render body/total cells against.
function statHead(ctx, colTh, opts) {
  const hittingOnly = opts && opts.hittingOnly;
  const hideCols = (opts && opts.hideCols) || null;  // Set of column keys to drop this render
  const groups = STAT_GROUPS
    // Under a hitting filter (setter / first-ball / transition) only the Hitting group is meaningful.
    .filter((g) => !hittingOnly || g.label === "Hitting")
    .map((g) => ({
      label: g.label,
      cols: visibleCols(g.cols, ctx).filter((c) => !hideCols || !hideCols.has(c.key)),
    }))
    .filter((g) => g.cols.length);
  // Tag the first visible column of every group after the first: drives the vertical separator that
  // runs down the header + body so each column's category reads at a glance. Re-tagged every render
  // (the boundary shifts with the Advanced toggle / teamOnly cols) and consumed synchronously below.
  groups.forEach((g, gi) => g.cols.forEach((c, ci) => { c.grpStart = gi > 0 && ci === 0; }));
  const grpTr = el("tr", { class: "grp-row" },
    [el("th", { class: "l sticky-col", rowspan: 2, text: "Player" })]);
  const colTr = el("tr", { class: "col-row" });
  groups.forEach((g, gi) => {
    grpTr.appendChild(el("th", {
      class: "grp" + (g.label ? "" : " grp-empty") + (gi > 0 ? " grp-start" : ""),
      colspan: g.cols.length, text: g.label,
    }));
    g.cols.forEach((c) => {
      const th = colTh(c);
      if (c.grpStart) th.classList.add("grp-start");
      colTr.appendChild(th);
    });
  });
  return { rows: [grpTr, colTr], cols: groups.flatMap((g) => g.cols) };
}

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
  if (cur.gamesScope === "favorites") cur.gamesScope = "fav_teams";  // legacy value
  const scopeSel = el("select", { onchange: (e) => {
    cur.gamesScope = e.target.value; renderGames(clear(root));
  } });
  [["all", "All games"], ["fav_teams", "★ Favorite teams"], ["fav_confs", "★ Favorite conferences"],
   ["fav_players", "★ Favorite players"], ["ranked", "Top 25 matchups"]]
    .forEach(([v, t]) => scopeSel.appendChild(el("option", { value: v, text: t })));
  scopeSel.value = cur.gamesScope;

  root.appendChild(el("div", { class: "filters games-filters" },
    [field("Week", wkSel), field("Show", scopeSel)]));

  const holder = el("div"); root.appendChild(holder); spinner(holder);
  if (!wkSel.value) { clear(holder); emptyState(holder, "No weeks available yet."); return; }
  // Favorite-based scopes need an account: prompt anonymous users to sign in instead of an empty board.
  if (FAV_SCOPES[cur.gamesScope] && !state.user) {
    clear(holder); signInEmptyState(holder, FAV_SCOPES[cur.gamesScope]); return;
  }
  try {
    const all = await apiCached("/games", { season: state.season, week: wkSel.value });
    // The "Favorite players" scope needs the set of contests those players appeared in.
    const favContests = cur.gamesScope === "fav_players" ? await loadFavPlayerContests() : null;
    clear(holder);
    const games = filterScoreboard(all, cur.gamesScope, favContests);
    if (!all.length) { emptyState(holder, "No games for this selection."); return; }
    if (!games.length) { emptyState(holder, GAMES_SCOPE_EMPTY[cur.gamesScope] || "No games for this selection."); return; }
    renderWeekBoard(holder, games, cur.gamesScope, cur);
  } catch (e) { clear(holder); emptyState(holder, "Error: " + e.message); }
}

const GAMES_SCOPE_EMPTY = {
  fav_teams: "No games this week involve your favorite teams.",
  fav_confs: "No games this week involve your favorite conferences.",
  fav_players: "No games this week involve your favorite players.",
  ranked: "No Top-25 matchups this week.",
};

// The noun shown in the "Sign in to favorite …" prompt for each favorites-only scope.
const FAV_SCOPES = { fav_teams: "teams", fav_confs: "conferences", fav_players: "players" };

// Empty state for anonymous users on a favorites scope: a clickable "Sign in" opening the auth modal.
function signInEmptyState(root, noun) {
  root.appendChild(el("div", { class: "empty-state" }, [
    el("a", { class: "link", onclick: (e) => { e.preventDefault(); openAuthModal("login"); } }, "Sign in"),
    el("span", { text: ` to favorite ${noun}` }),
  ]));
}

// Games involving the signed-in user's favorite players this season: the contests they appeared in
// (played games) plus their team ids (to also catch *upcoming* games, which have no contest yet).
// Cached per season in state (invalidated when favorites change — see toggleFavorite).
async function loadFavPlayerContests() {
  state.favPlayerContests = state.favPlayerContests || {};
  const key = state.season;
  if (!state.favPlayerContests[key]) {
    const d = await api("/favorites/contests", { season: state.season })
      .catch(() => ({ contest_ids: [], team_ids: [] }));
    state.favPlayerContests[key] = {
      contests: new Set(d.contest_ids || []),
      teams: new Set(d.team_ids || []),
    };
  }
  return state.favPlayerContests[key];
}

// Client-side scoreboard filter. fav_teams: either side is a favorited team. fav_confs: either
// side belongs to a favorited conference. fav_players: the contest is one a favorite player
// appeared in. ranked: top-25-vs-top-25. "all" (or anything else) is a pass-through.
function filterScoreboard(games, scope, favContests) {
  if (scope === "fav_teams") {
    return games.filter((g) =>
      (g.away_team && isFav("team", g.away_team.id)) ||
      (g.home_team && isFav("team", g.home_team.id)));
  }
  if (scope === "fav_confs") {
    const confs = favConferenceIds();
    return games.filter((g) =>
      (g.away_team && confs.has(g.away_team.conference_id)) ||
      (g.home_team && confs.has(g.home_team.conference_id)));
  }
  if (scope === "fav_players") {
    const fp = favContests || { contests: new Set(), teams: new Set() };
    return games.filter((g) =>
      (g.contest_id && fp.contests.has(g.contest_id)) ||          // played: they appeared
      (g.away_team && fp.teams.has(g.away_team.id)) ||            // or their team is playing
      (g.home_team && fp.teams.has(g.home_team.id)));            // (catches upcoming games)
  }
  if (scope === "ranked") {
    return games.filter((g) =>
      isRankedMatchup(g.away_team && g.away_team.avca_rank, g.home_team && g.home_team.avca_rank));
  }
  return games;
}

// "2026-09-01 18:00" -> "2026-09-01": contests carry a time suffix, so group on the calendar day.
const dayKey = (iso) => (iso ? iso.slice(0, 10) : "TBD");

// Today's calendar day in the viewer's local zone (not UTC), for "is this game in the past".
function localTodayStr() {
  const n = new Date();
  return `${n.getFullYear()}-${String(n.getMonth() + 1).padStart(2, "0")}-${String(n.getDate()).padStart(2, "0")}`;
}

// Parse a clock string to minutes-since-midnight. Handles both 24h ("18:00", from a contest's date
// suffix) and 12h ("6:00 PM", "10:00 AM", from schedule game_time). Returns null when unparseable,
// so callers can sort timeless games last. Lexical sort of the raw strings is wrong — it groups all
// AM and PM times together (both "10:.." land next to each other), so always sort on these minutes.
function clockMinutes(s) {
  const m = /(\d{1,2}):(\d{2})\s*([AP]M)?/i.exec((s || "").trim());
  if (!m) return null;
  let h = Number(m[1]);
  if (m[3]) h = (h % 12) + (/PM/i.test(m[3]) ? 12 : 0);
  const mins = h * 60 + Number(m[2]);
  // 00:00 / "12:00 AM" is the no-published-tip sentinel, not a real midnight game: report it as
  // timeless so it isn't mistaken for a small-hours-ET game and rolled back to the previous day.
  return mins === 0 ? null : mins;
}

// Minutes-since-midnight for a scoreboard game: played games carry a 24h suffix on `date`
// ("2026-09-04 18:00"); upcoming games carry a 12h `game_time`.
function gameMinutes(g) {
  return g.status === "played" ? clockMinutes((g.date || "").slice(10)) : clockMinutes(g.game_time);
}

// Times are stored in US Eastern wall-clock, so a Hawaii/late-Pacific match tips past midnight ET
// (e.g. LMU @ Hawaii at 1 AM EDT). No legit NCAA match starts between midnight and 5 AM ET, so treat
// any such game as belonging to the PREVIOUS day's slate — that's when it was actually played.
const EARLY_AM_CUTOFF = 5 * 60;  // minutes since ET midnight
function isEarlyAmGame(g) {
  const mins = gameMinutes(g);
  return mins != null && mins < EARLY_AM_CUTOFF;
}
// Shift a "YYYY-MM-DD" day by n days (UTC math avoids any local-zone drift).
function addDays(ymd, n) {
  const [y, m, d] = ymd.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d + n));
  return `${dt.getUTCFullYear()}-${String(dt.getUTCMonth() + 1).padStart(2, "0")}-`
    + `${String(dt.getUTCDate()).padStart(2, "0")}`;
}
// The day a game is grouped under on the scoreboard: its calendar day, rolled back one for
// small-hours-ET games so they sit with the previous evening's slate.
function scoreboardDayKey(g) {
  const base = dayKey(g.date);
  return (base !== "TBD" && isEarlyAmGame(g)) ? addDays(base, -1) : base;
}
// Sort key within a day: small-hours-ET games sort AFTER the prior evening's games (they played
// later), so push them past a full day of minutes.
function sortMinutes(g) {
  const m = gameMinutes(g);
  return m == null ? null : (isEarlyAmGame(g) ? m + 1440 : m);
}

// A game is "done" if it has a scraped result, or it's dated before today (played, but our box-score
// scrape hasn't pulled the final from stats.ncaa.org yet — those show as "final, score pending").
function isGameDone(g, today) {
  return g.status === "played" || dayKey(g.date) < today;
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

// Weekday abbreviation ("Mon") + day-of-month ("7") for a YYYY-MM-DD key, parsed from local date
// parts (mirrors fmtDateShort) so no UTC drift can shift the weekday label.
function dayPillParts(ymd) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(ymd || "");
  if (!m) return { wd: "", dom: "" };
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return { wd: d.toLocaleDateString(undefined, { weekday: "short" }), dom: String(d.getDate()) };
}

// One pill in the week day-strip. Empty days (no games) are inert; "all" and any day with games are
// clickable. `day` is "all" or a YYYY-MM-DD; stored on data-day so selectDay can toggle .active.
function dayPill(day, top, dom, count, isToday, active, onSelect) {
  const empty = day !== "all" && count === 0;
  const attrs = {
    class: "day-pill" + (active ? " active" : "") + (isToday ? " today" : "") + (empty ? " empty" : ""),
    "data-day": day,
  };
  if (empty) attrs.disabled = true; else attrs.onclick = () => onSelect(day);
  return el("button", attrs, [
    el("span", { class: "day-pill-top", text: top }),
    el("span", { class: "day-pill-dom", text: dom }),
    el("span", { class: "day-pill-count", text: count ? String(count) : "–" }),
  ]);
}

// The Games tab's week board: a horizontal strip of day pills for the selected week (ESPN/NCAA
// scoreboard pattern) above a body showing the picked day's cards. "All" keeps the stacked
// full-week view. Day switching is pure client-side — the whole week is already fetched.
function renderWeekBoard(root, games, scope, cur) {
  const byDate = {};
  games.forEach((g) => { const k = scoreboardDayKey(g); (byDate[k] = byDate[k] || []).push(g); });
  // The 7 calendar days of the selected week (Mon..Sun), from the week's start date.
  const wk = state.weeks.find((w) => String(w.week_number) === String(cur.week));
  const weekDays = [];
  if (wk && wk.start) {
    const mon = wk.start.slice(0, 10);
    for (let i = 0; i < 7; i++) weekDays.push(addDays(mon, i));
  }
  // Pill days = the 7 week days ∪ any day a game actually landed on (an early-AM rollback can sit
  // just outside Mon..Sun), so no game is ever hidden.
  const days = Array.from(new Set([...weekDays, ...Object.keys(byDate)])).sort();
  const today = localTodayStr();
  const withGames = days.filter((d) => (byDate[d] || []).length);

  // Validate / default the selected day. "all" persists across weeks; a stale specific day (left
  // over from another week, or emptied by a scope change) falls back to today (if in-week with
  // games), else the first day with games, else "all".
  if (cur.gamesDay !== "all" && !withGames.includes(cur.gamesDay)) cur.gamesDay = null;
  if (!cur.gamesDay) cur.gamesDay = withGames.includes(today) ? today : (withGames[0] || "all");

  const strip = el("div", { class: "day-strip" });
  const body = el("div", { class: "week-board-body" });
  const favPlayerByTeam = scope === "fav_players" ? favPlayerTeamMap() : null;

  const drawBody = () => {
    clear(body);
    if (cur.gamesDay === "all") { renderScoreboard(body, games, scope); return; }
    const dayGames = (byDate[cur.gamesDay] || []).slice().sort(dayGameSort(today));
    body.appendChild(el("div", { class: "day-board-head", text: fmtDateShort(cur.gamesDay) || "TBD" }));
    if (!dayGames.length) { emptyState(body, "No games on this day."); return; }
    const grid = el("div", { class: "game-grid" });
    dayGames.forEach((g) => grid.appendChild(scoreCard(g, scope, favPlayerByTeam)));
    body.appendChild(grid);
  };
  const selectDay = (d) => {
    cur.gamesDay = d;
    Array.from(strip.children).forEach((p) => p.classList.toggle("active", p.dataset.day === d));
    drawBody();
  };

  strip.appendChild(dayPill("all", "All", "Week", games.length, false, cur.gamesDay === "all", selectDay));
  days.forEach((d) => {
    const { wd, dom } = dayPillParts(d);
    strip.appendChild(dayPill(d, wd, dom, (byDate[d] || []).length, d === today, cur.gamesDay === d, selectDay));
  });

  root.appendChild(strip);
  root.appendChild(body);
  drawBody();
}

// Within-day ordering: completed games first, then upcoming — each block in chronological order (by
// parsed minutes, so 10 AM precedes 10 PM). Timeless games (null minutes) sink to the bottom.
function dayGameSort(today) {
  return (a, b) => {
    const ad = isGameDone(a, today) ? 0 : 1, bd = isGameDone(b, today) ? 0 : 1;
    if (ad !== bd) return ad - bd;
    const am = sortMinutes(a), bm = sortMinutes(b);
    if (am == null) return bm == null ? 0 : 1;
    if (bm == null) return -1;
    return am - bm;
  };
}

function renderScoreboard(root, games, scope) {
  const byDate = {};
  const favPlayerByTeam = scope === "fav_players" ? favPlayerTeamMap() : null;
  games.forEach((g) => { const k = scoreboardDayKey(g); (byDate[k] = byDate[k] || []).push(g); });
  // "All Games" opens every day expanded (the user picked the full-week view to see it all at once);
  // each day still toggles independently to hide a finished day. Local date (not UTC) for sorting.
  const today = localTodayStr();
  Object.keys(byDate).sort().forEach((d) => {
    byDate[d].sort(dayGameSort(today));
    const list = el("div", { class: "game-grid" });
    byDate[d].forEach((g) => list.appendChild(scoreCard(g, scope, favPlayerByTeam)));
    root.appendChild(el("details", { class: "card day-card", open: true }, [
      el("summary", { class: "card-title day-summary" }, [
        fmtDateShort(d) || "TBD",
        el("span", { class: "badge", text: byDate[d].length + (byDate[d].length === 1 ? " game" : " games") }),
      ]),
      list,
    ]));
  });
}

// Decoration pills shared by the scoreboard row + card: a Top-25 matchup badge always, plus
// per-scope pills — favorite-conference pills under "fav_confs", a "N Players" count under
// "fav_players". Returns an array of nodes (possibly empty).
function gameBadges(g, scope, favPlayerByTeam) {
  const badges = [];
  const ranked = isRankedMatchup(g.away_team && g.away_team.avca_rank, g.home_team && g.home_team.avca_rank);
  if (ranked) badges.push(el("span", { class: "matchup-badge", title: "Top-25 matchup", text: "Top 25" }));
  // Only under the "Favorite conferences" filter: a pill per favorited conference either team is in
  // (deduped, so a same-conference matchup shows one). Never shown under any other filter.
  if (scope === "fav_confs") {
    const favConfs = favConferenceIds();
    const seen = new Set();
    [g.away_team, g.home_team].forEach((t) => {
      const cid = t && t.conference_id;
      if (cid != null && favConfs.has(cid) && !seen.has(cid)) {
        seen.add(cid);
        badges.push(el("span", { class: "conf-badge", title: "Favorite conference",
          style: `background:${confBadgeColor(cid)}`, text: confShortById(cid) || "Conf" }));
      }
    });
  }
  // Under the "Favorite players" filter: how many of the user's favorite players are in this game,
  // with their names in the tooltip (both from favoriteRows already in memory — no extra request).
  if (scope === "fav_players" && favPlayerByTeam) {
    const names = [
      ...(favPlayerByTeam.get(g.away_team && g.away_team.id) || []),
      ...(favPlayerByTeam.get(g.home_team && g.home_team.id) || []),
    ];
    if (names.length) {
      badges.push(el("span", { class: "player-badge", title: names.join(", "),
        text: `${names.length} ${names.length === 1 ? "Player" : "Players"}` }));
    }
  }
  return badges;
}

// Upper-right cluster of network tags on a game card: one pill per broadcaster carrying the game.
// A logo <img> where we ship an SVG (falling back to a text pill if the asset 404s), a text pill
// otherwise. Deduped upstream; capped at 3 here with a "+N" overflow pill listing the rest.
function networkTags(g) {
  const list = (g && g.broadcasts) || [];
  if (!list.length) return null;
  const MAX = 3;
  // TPS event-feed slot ("ESPN+ 45") is only meaningful while the game is upcoming — TPS renumbers
  // slots daily — so show it in the tooltip only for upcoming games, not played ones.
  const upcoming = !g || g.status !== "played";
  const tip = (b) => (upcoming && b.channel_no ? `${b.network} · Ch ${b.channel_no}` : b.network);
  const textPill = (b) => el("span", { class: "net-tag", title: tip(b), text: b.network });
  const nodes = list.slice(0, MAX).map((b) => {
    if (!b.logo_key) return textPill(b);
    return el("img", {
      class: "net-logo", alt: b.network, title: tip(b),
      src: `assets/logos/networks/${b.logo_key}.svg`,
      onerror: (e) => e.target.replaceWith(textPill(b)),  // missing asset -> text fallback
    });
  });
  if (list.length > MAX) {
    nodes.push(el("span", { class: "net-tag net-more",
      title: list.slice(MAX).map((b) => b.network).join(", "), text: `+${list.length - MAX}` }));
  }
  return el("div", { class: "gc-networks" }, nodes);
}

// A scoreboard game as a card (the scoreboard renders a responsive grid of these). Away @ home,
// laid out vertically: a badge slot (always reserved so team rows align across cards even without a
// badge), two stacked team lines each with logo/name/rank/★, that team's per-set scores as aligned
// columns, and a big sets-won number, then a footer with attendance + NCAA link (played) or start
// time (upcoming).
function scoreCard(g, scope, favPlayerByTeam) {
  const played = g.status === "played";
  const bothScores = g.home_sets_won != null && g.away_sets_won != null;
  const homeWon = played && bothScores && g.home_sets_won > g.away_sets_won;
  const awayWon = played && bothScores && g.away_sets_won > g.home_sets_won;
  const pastUnplayed = !played && dayKey(g.date) < localTodayStr();
  const ss = g.set_scores || {};
  // One team's per-set scores as a row of fixed-width cells — both team rows use the same cell
  // width + set count, so the columns line up vertically between away and home.
  const setCells = (arr) => el("span", { class: "gc-sets-line" },
    (arr || []).map((v) => el("span", { class: "gc-set", text: v == null ? "" : String(v) })));
  const teamLine = (t, fallback, won, setsWon, sideScores) => {
    const name = t ? (t.short_name || t.name) : (fallback || "TBD");
    // Suppress the favorite ★/highlight on a team's own page (scope "team") — every card there is the
    // viewed team, so the marker is just noise. It still shows on the Games tab and elsewhere.
    const fav = scope !== "team" && t && isFav("team", t.id);
    const nameEl = t
      ? el("a", { class: "link game-team-name" + (won ? " win" : ""),
          onclick: (e) => { e.stopPropagation(); openTeam(t.id, name); } }, name)
      : el("span", { class: "game-team-name" + (won ? " win" : ""), text: name });
    return el("div", { class: "gc-team" + (fav ? " is-fav" : "") + (won ? " win" : "") }, [
      fav ? favMark() : null,
      teamLogoImg(t, "game-logo"),
      el("span", { class: "gc-team-name" }, [
        nameEl,
        t ? rankChip(t.avca_rank) : null,
        (!t && isNonD1Opp(fallback, false)) ? nonD1Tag() : null,
      ]),
      played ? setCells(sideScores) : null,
      played ? el("span", { class: "gc-sets" + (won ? " win" : ""), text: setsWon == null ? "–" : setsWon }) : null,
    ]);
  };
  const timeText = fmtGameTime(g.date, g.game_time);
  const hasTime = timeText && timeText !== "TBD";
  // The Games tab groups cards under day headers, so the card itself is dateless there. A team's own
  // Schedule (scope "team") has no such grouping, so surface the date on each card.
  const dateText = scope === "team" ? fmtDateShort(g.date) : "";
  const dateEl = () => (dateText ? el("span", { class: "game-date muted", text: dateText }) : null);
  const foot = played
    ? el("div", { class: "gc-foot" }, [
        dateEl(),
        hasTime ? el("span", { class: "game-time muted", text: timeText }) : null,
        g.attendance != null ? el("span", { class: "muted", text: `Attend: ${g.attendance.toLocaleString()}` })
          : (hasTime ? null : el("span", { class: "muted", text: "final" })),
        g.ncaa_game_id ? el("a", { class: "game-ncaa muted ncaa-link", href: ncaaGameUrl(g.ncaa_game_id),
          target: "_blank", rel: "noopener", title: "View on NCAA.com",
          onclick: (e) => e.stopPropagation() }, "NCAA ↗") : null,
      ])
    : pastUnplayed
    ? el("div", { class: "gc-foot" }, [
        dateEl(),
        g.ncaa_game_id
          ? el("a", { class: "game-score pending ncaa-link", href: ncaaGameUrl(g.ncaa_game_id),
              target: "_blank", rel: "noopener", title: "Final on NCAA.com — box score pending",
              onclick: (e) => e.stopPropagation() }, "final ↗")
          : el("span", { class: "game-score pending", text: "final" }),
        el("span", { class: "muted", text: "score pending" }),
      ])
    : el("div", { class: "gc-foot" }, [
        dateEl(),
        g.ncaa_game_id
          ? el("a", { class: "game-time muted ncaa-link", href: ncaaGameUrl(g.ncaa_game_id),
              target: "_blank", rel: "noopener", title: "View on NCAA.com",
              onclick: (e) => e.stopPropagation() }, `${timeText} ↗`)
          : el("span", { class: "game-time muted", text: timeText }),
      ]);
  const badges = gameBadges(g, scope, favPlayerByTeam);
  // On a team's own schedule, lead each result card with a W/L pill (and tint the card edge) so wins
  // and losses read at a glance without parsing the set scores.
  const wl = scope === "team" && g.self_won != null;
  if (wl) {
    badges.unshift(el("span", { class: "wl-badge " + (g.self_won ? "win" : "loss"),
      title: g.self_won ? "Win" : "Loss", text: g.self_won ? "W" : "L" }));
  }
  const resultClass = wl ? (g.self_won ? " result-win" : " result-loss") : "";
  const card = el("div", { class: "game-card" + resultClass + (played && g.contest_id ? " clickable" : "") }, [
    el("div", { class: "game-badges" }, badges),  // always present — reserves top space so rows align
    networkTags(g),                                // absolute upper-right; null when no broadcasts
    teamLine(g.away_team, g.away_name, awayWon, g.away_sets_won, ss.away),
    teamLine(g.home_team, g.home_name, homeWon, g.home_sets_won, ss.home),
    foot,
  ]);
  if (played && g.contest_id) card.addEventListener("click", () => openGame(g.contest_id));
  return card;
}

// Adapt a /teams/{id}/games row (opponent-relative) to the /games ScoreboardGame shape (home/away)
// so a team's schedule can render with the shared `scoreCard`. `selfTeam` is the viewed team's
// TeamOut (id + name + logos + rank). Orientation mirrors the old row renderer: self is the nominal
// home side only when site === "home" (neutral/away -> self is the away side), which keeps the raw
// home/away-keyed `set_scores` aligned with the team lines without re-keying.
function teamGameToScoreboard(g, selfTeam) {
  // A resolved (D1) opponent becomes a TeamRef; a non-D1 / unresolved opponent (no id) is left null
  // so scoreCard renders it as a plain name + non-D1 tag via the away_name/home_name fallback.
  const opp = g.opponent_id
    ? {
        id: g.opponent_id,
        name: g.opponent,
        short_name: g.opponent_short,
        logo_light: g.opponent_logo_light,
        logo_dark: g.opponent_logo_dark,
        avca_rank: g.opponent_avca_rank,
      }
    : null;
  const selfHome = g.site === "home";
  return {
    contest_id: g.contest_id,
    ncaa_game_id: g.ncaa_game_id,
    date: g.date,
    game_time: g.game_time,
    status: g.status,
    home_team: selfHome ? selfTeam : opp,
    away_team: selfHome ? opp : selfTeam,
    home_name: selfHome ? (selfTeam && selfTeam.name) : g.opponent,
    away_name: selfHome ? g.opponent : (selfTeam && selfTeam.name),
    home_sets_won: selfHome ? g.team_sets_won : g.opponent_sets_won,
    away_sets_won: selfHome ? g.opponent_sets_won : g.team_sets_won,
    set_scores: g.set_scores || null,   // already {home, away}-keyed — passes through unchanged
    // Result from the VIEWED team's perspective, for the W/L marker on its own schedule cards.
    self_won: g.status === "played" && g.team_sets_won != null && g.opponent_sets_won != null
      ? g.team_sets_won > g.opponent_sets_won
      : null,
  };
}

// A `.card` whose title row is click-to-toggle (chevron + title + any extra header nodes), collapsed
// by default. Returns { card, body } — append content to `body`. Extra header nodes that are
// themselves interactive (e.g. a seg-toggle) should stopPropagation so their clicks don't also toggle
// the card. State lives in a local closure, so each render defaults collapsed.
function collapsibleCard(title, extra) {
  const chev = el("span", { class: "chev", text: "▸" });
  const head = el("div", { class: "card-title collapse-head" }, [chev, title, ...(extra || [])]);
  const body = el("div", { class: "collapse-body", hidden: true });
  const card = el("div", { class: "card" }, [head, body]);
  let open = false;
  const setOpen = (v) => { open = v; body.hidden = !open; chev.textContent = open ? "▾" : "▸"; };
  head.addEventListener("click", () => setOpen(!open));
  return { card, body };
}

// A team's Schedule & Results as two collapsible sections of game cards (the same `scoreCard` the
// Games tab uses). Results are open by default; Upcoming is collapsed in season scope but expanded
// when a single week is in scope (short list, worth showing). `selfTeam` supplies the viewed team's
// logo/name/rank for the card's home/away lines.
function renderTeamGames(root, games, expandUpcoming, selfTeam) {
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
    // Oldest first so the most recent result sits at the bottom of the list.
    const playedAsc = played.slice().sort((a, b) => {
      const ad = dayKey(a.date), bd = dayKey(b.date);
      return ad < bd ? -1 : ad > bd ? 1 : 0;
    });
    const list = el("div", { class: "game-grid" });
    playedAsc.forEach((g) =>
      list.appendChild(scoreCard(teamGameToScoreboard(g, selfTeam), "team")));
    root.appendChild(section("Results", played.length, true, list));
  }
  if (upcoming.length) {
    // Sort by day then start time. game_time is a 12h "6:00 PM" string, so a lexical sort mixes up
    // AM/PM — gameMinutes()/clockMinutes() parse it to real minutes. Timeless games sink last.
    upcoming.sort((a, b) => {
      const ad = dayKey(a.date), bd = dayKey(b.date);
      if (ad !== bd) return ad < bd ? -1 : 1;
      const am = gameMinutes(a), bm = gameMinutes(b);
      if (am == null) return bm == null ? 0 : 1;
      if (bm == null) return -1;
      return am - bm;
    });
    const list = el("div", { class: "game-grid" });
    upcoming.forEach((g) =>
      list.appendChild(scoreCard(teamGameToScoreboard(g, selfTeam), "team")));
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

// Game detail (#/game?cid=…): shared header + a tabbed body (Overview / Team Stats / Individual
// Stats / Play By Play), mirroring stats.ncaa.org's game view.
async function renderGame(root) {
  replaceURL();
  const cid = state.contestId;
  root.appendChild(el("div", { class: "back-link" },
    el("a", { class: "link", onclick: () => goBack("games") }, "← Back")));
  const holder = el("div"); root.appendChild(holder); spinner(holder);
  try {
    const [c, stats, pbp] = await Promise.all([
      api(`/contests/${cid}`),
      api(`/contests/${cid}/stats`).catch(() => []),
      apiCached(`/contests/${cid}/pbp`).catch(() => null),
    ]);
    // A contest belongs to exactly one season; keep the topbar honest when we land here on a game
    // from another season (deep link / back-forward) by syncing the picker to the game's season.
    if (c.season != null && c.season !== state.season) {
      state.season = c.season;
      saveSeason();
      const sel = $("#season-select"); if (sel) sel.value = String(c.season);
      updateTabVisibility();
      await Promise.all([refreshWeeks(), refreshSeasonConferences()]);
    }
    clear(holder);
    holder.appendChild(gameHeader(c));
    holder.appendChild(gameTabs(c, stats, pbp));
  } catch (e) { clear(holder); emptyState(holder, "Error: " + e.message); }
}

// The tabbed body under the game header, shared by the full page and the modal. `opts.playerClick`
// (modal) drills into a player within the overlay; omitted → openPlayer navigates the full page.
// The active tab lives in state.gameTab so it survives an Advanced-toggle re-render (which re-runs
// renderGame / the modal draw) instead of snapping back to Overview.
function gameTabs(c, stats, pbp, opts) {
  opts = opts || {};
  const awayStats = stats.filter((s) => s.team_id === c.away_team_id);
  const homeStats = stats.filter((s) => s.team_id === c.home_team_id);
  const hasPbp = !!(pbp && pbp.sets && pbp.sets.length);
  const hasLineups = !!(pbp && pbp.lineups && pbp.lineups.some((t) => t.sets && t.sets.length));
  const hasRotations = !!(pbp && pbp.rotations && pbp.rotations.some((t) => t.sets && t.sets.length));
  // [key, full label, short label] — the short label shows on narrow screens so all tabs fit.
  const TABS = [
    ["overview", "Overview", "Overview"],
    ["team", "Team Stats", "Team"],
    ["individual", "Individual Stats", "Individual"],
  ];
  if (hasLineups) TABS.push(["lineups", "Lineups", "Lineups"]);
  if (hasRotations) TABS.push(["rotations", "Rotations", "Rot"]);
  if (hasPbp) TABS.push(["pbp", "Play By Play", "PBP"]);
  if (!TABS.some(([k]) => k === state.gameTab)) state.gameTab = "overview";

  const wrap = el("div", { class: "game-tabs-wrap" });
  const toggle = el("div", { class: "seg-toggle game-tabs" });
  const body = el("div", { class: "game-tab-body" });

  const draw = () => {
    clear(body);
    const t = state.gameTab;
    if (t === "overview") body.appendChild(overviewTab(c, awayStats, homeStats, pbp));
    else if (t === "team") body.appendChild(teamStatsTab(c, awayStats, homeStats));
    else if (t === "lineups") body.appendChild(lineupsTab(pbp, c, opts.playerClick));
    else if (t === "rotations") body.appendChild(rotationsTab(pbp, c));
    else if (t === "pbp") body.appendChild(pbpCard(pbp, c) || emptyCard("No play-by-play for this game."));
    else {
      const cid = c.contest_id;
      const gf = state.gameFilters[cid]
        || (state.gameFilters[cid] = { away: defaultHitting(), home: defaultHitting() });
      body.appendChild(boxScoreCard(c.away_team, awayStats, opts.playerClick,
        { cur: gf.away, contestId: cid }));
      body.appendChild(boxScoreCard(c.home_team, homeStats, opts.playerClick,
        { cur: gf.home, contestId: cid }));
    }
    setGameModalFit(t === "individual");  // widen the modal for the two wide box-score tables
  };
  const setGameTab = (t) => {
    state.gameTab = t;
    Array.from(toggle.children).forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
    draw();
  };
  TABS.forEach(([k, label, short]) => toggle.appendChild(
    el("button", { class: "seg-btn" + (k === state.gameTab ? " active" : ""),
      "data-tab": k, onclick: () => setGameTab(k) }, [
        el("span", { class: "tab-full", text: label }),
        el("span", { class: "tab-short", text: short }),
      ])));
  wrap.appendChild(toggle);
  wrap.appendChild(body);
  draw();
  return wrap;
}

// A plain card holding a single empty-state message (used for empty tab states).
function emptyCard(msg) {
  return el("div", { class: "card" }, el("div", { class: "empty-state", text: msg }));
}

// Team totals for a set of box-score rows: additive sums + official team blocks (half-weight
// assists) + hitting % from the summed kills/errors/attacks. Reuses teamTotals() then fills hit_pct.
function gameTeamTotals(rows) {
  const t = teamTotals(rows);
  t.hit_pct = hitPct(t);
  return t;
}

// A column header for the Overview grids: team logo + name, centered under its column.
function ovTeamCol(t, nm) {
  return el("div", { class: "ov-team" }, [
    teamLogoImg(t, "game-logo"),
    el("span", { class: "ov-team-name", text: nm }),
  ]);
}

// Overview tab: per-team game leaders + a per-set score-progression chart for each set. Pure
// composition of the already-fetched box score + PBP payload (the line score sits in the header
// above the tabs; full team totals live under the Team Stats tab).
function overviewTab(c, awayStats, homeStats, pbp) {
  const wrap = el("div");
  const awayNm = c.away_team ? (c.away_team.short_name || c.away_team.name) : "Away";
  const homeNm = c.home_team ? (c.home_team.short_name || c.home_team.name) : "Home";
  wrap.appendChild(gameLeadersCard(c, awayStats, homeStats, awayNm, homeNm));
  if (pbp && pbp.sets && pbp.sets.length) {
    pbp.sets.forEach((s) => wrap.appendChild(setScoreChartCard(s, c, awayNm, homeNm)));
  }
  return wrap;
}

// Lineups tab: per team, the set-to-set lineup CHANGES first (the reliable signal), then each set's
// starting group + bench subs. Reads the per-set lineups surfaced by /contests/{id}/pbp (no extra
// fetch). "Starters" = who was on court at each set's first serve (reconstructed from the sub log);
// a starter kept even if subbed out and back in (e.g. a setter swap). Libero/defensive-sub slots are
// approximate where the feed omits a substitution.
function lineupsTab(pbp, c, playerClick) {
  const click = playerClick || openPlayer;
  const wrap = el("div");
  const lineups = (pbp && pbp.lineups) || [];
  // Away first, then home — match the rest of the game UI.
  const ordered = lineups.slice().sort((a, b) =>
    (a.side === "away" ? 0 : 1) - (b.side === "away" ? 0 : 1));
  ordered.forEach((t) => {
    const teamRef = t.side === "away" ? c.away_team : c.home_team;
    const nm = t.team || (teamRef ? (teamRef.short_name || teamRef.name)
      : (t.side === "away" ? "Away" : "Home"));
    const card = el("div", { class: "card lineups-card" });
    card.appendChild(el("div", { class: "card-title" }, [
      teamRef ? ovTeamCol(teamRef, nm) : el("span", { text: nm }),
      el("span", { class: "badge", text: "beta" }),
    ]));

    // Change summary — the decision-relevant part, shown up top.
    if (t.starters_changed && t.starter_changes && t.starter_changes.length) {
      const box = el("div", { class: "lineup-changes" });
      t.starter_changes.forEach((ch) => {
        const parts = [el("span", { class: "lineup-change-set", text: `Set ${ch.set_number}` })];
        if (ch.added && ch.added.length)
          parts.push(el("span", { class: "lineup-in", text: `In ${ch.added.join(", ")}` }));
        if (ch.removed && ch.removed.length)
          parts.push(el("span", { class: "lineup-out", text: `Out ${ch.removed.join(", ")}` }));
        box.appendChild(el("div", { class: "lineup-change-row" }, parts));
      });
      card.appendChild(box);
    } else {
      card.appendChild(el("div", { class: "lineup-nochange muted",
        text: "Same starting group every set." }));
    }

    // Per-set starters + subs.
    (t.sets || []).forEach((s) => {
      const setBox = el("div", { class: "lineup-set" });
      setBox.appendChild(el("div", { class: "lineup-set-title", text: `Set ${s.set_number}` }));
      setBox.appendChild(lineupGroup("Starters", s.starters, click));
      if (s.subs && s.subs.length) setBox.appendChild(lineupGroup("Subs", s.subs, click));
      card.appendChild(setBox);
    });

    card.appendChild(el("div", { class: "lineup-note muted",
      text: "Starters = on court at the first serve, from play-by-play; "
        + "libero/defensive-sub slots are approximate." }));
    wrap.appendChild(card);
  });
  if (!ordered.length) wrap.appendChild(emptyCard("No lineup data for this game."));
  return wrap;
}

// One labeled row of player chips (Starters or Subs). Names link to the player page when the id
// resolved; otherwise plain text (defensive, like the rally log).
function lineupGroup(label, players, click) {
  const row = el("div", { class: "lineup-group" });
  row.appendChild(el("span", { class: "lineup-group-label muted", text: label }));
  const list = el("span", { class: "lineup-players" });
  (players || []).forEach((p) => {
    const num = p.number != null ? `#${p.number} ` : "";
    const pos = p.position ? ` (${p.position})` : "";
    const txt = `${num}${p.player || ("#" + (p.player_id != null ? p.player_id : "?"))}${pos}`;
    if (p.player_id != null)
      list.appendChild(el("a", { class: "link lineup-player",
        onclick: () => click(p.player_id), text: txt }));
    else
      list.appendChild(el("span", { class: "lineup-player", text: txt }));
  });
  row.appendChild(list);
  return row;
}

// Rotations tab: per team, a six-row table (R1-R6) of per-rotation point +/-, sideout %, serve/hold %,
// and the attack line. Rotations are reconstructed from the pbp rally log and anchored to the setter
// (R1 = setter in the serving position / zone 1). A Match / Per set toggle switches between match
// totals and a table per set. Reads /contests/{id}/pbp — no extra fetch, positional so subs don't
// perturb the numbering. `state.rotationScope` persists the toggle across re-renders (like gameTab).
function rotationsTab(pbp, c) {
  const wrap = el("div");
  const rotations = (pbp && pbp.rotations) || [];
  if (!rotations.length) { wrap.appendChild(emptyCard("No rotation data for this game.")); return wrap; }
  if (state.rotationScope !== "sets") state.rotationScope = "match";

  // Away first, then home — match the rest of the game UI.
  const ordered = rotations.slice().sort((a, b) =>
    (a.side === "away" ? 0 : 1) - (b.side === "away" ? 0 : 1));

  const toggle = el("div", { class: "seg-toggle rot-scope" });
  const cards = el("div");
  const drawCards = () => {
    clear(cards);
    ordered.forEach((t) => cards.appendChild(rotationTeamCard(t, c)));
  };
  [["match", "Match"], ["sets", "Per set"]].forEach(([k, label]) => toggle.appendChild(
    el("button", { class: "seg-btn" + (k === state.rotationScope ? " active" : ""),
      onclick: (e) => {
        state.rotationScope = k;
        Array.from(toggle.children).forEach((b) => b.classList.remove("active"));
        e.currentTarget.classList.add("active");
        drawCards();
      }, text: label })));
  wrap.appendChild(toggle);
  wrap.appendChild(cards);
  drawCards();
  return wrap;
}

// One team's rotations card: the Match/Per-set choice comes from state.rotationScope. "Match" renders
// the totals table; "Per set" renders one table per set.
function rotationTeamCard(t, c) {
  const teamRef = t.side === "away" ? c.away_team : c.home_team;
  const nm = t.team || (teamRef ? (teamRef.short_name || teamRef.name)
    : (t.side === "away" ? "Away" : "Home"));
  const card = el("div", { class: "card rotations-card" });
  card.appendChild(el("div", { class: "card-title" }, [
    teamRef ? ovTeamCol(teamRef, nm) : el("span", { text: nm }),
    el("span", { class: "badge", text: "beta" }),
  ]));
  if (state.rotationScope === "sets") {
    (t.sets || []).forEach((s) => {
      card.appendChild(el("div", { class: "rot-set-title", text: `Set ${s.set_number}` }));
      card.appendChild(rotationTable(s.rotations));
    });
  } else {
    card.appendChild(rotationTable(t.totals));
  }
  card.appendChild(el("div", { class: "rot-note muted",
    text: "Rotations R1-R6 from play-by-play; R1 = the setter's serving rotation. "
      + "SO% = sideout (won on receive); Hold% = points held on serve." }));
  return card;
}

// A six-row rotation table. Each row: R#, point +/-, sideout %, serve/hold %, and K / E / hit%.
// Percent cells show "—" when that rotation had no rallies of the relevant phase (avoids /0).
function rotationTable(rows) {
  const pct = (num, den) => (den ? (num / den * 100).toFixed(0) + "%" : "—");
  const table = el("table", { class: "wide-table dense-table rot-table" });
  table.appendChild(el("tr", {}, [
    el("th", { class: "l", text: "Rot" }),
    el("th", { class: "num", text: "+/-", title: "Points won − points lost in this rotation" }),
    el("th", { class: "num", text: "SO%", title: "Sideout % — rallies won while receiving serve" }),
    el("th", { class: "num", text: "Hold%", title: "Hold % — rallies won while serving" }),
    el("th", { class: "num", text: "K", title: "Kills" }),
    el("th", { class: "num", text: "E", title: "Attack errors" }),
    el("th", { class: "num", text: "Hit%", title: "Hitting % — (kills − errors) ÷ attacks" }),
  ]));
  (rows || []).forEach((r) => {
    const diff = (r.points_won || 0) - (r.points_lost || 0);
    const diffCls = "num " + (diff > 0 ? "rot-pos" : diff < 0 ? "rot-neg" : "");
    const hit = r.attack_attempts ? (r.kills - (r.attack_errors || 0)) / r.attack_attempts : null;
    table.appendChild(el("tr", {}, [
      el("td", { class: "l rot-label", text: "R" + r.rotation }),
      el("td", { class: diffCls, text: (diff > 0 ? "+" : "") + diff }),
      el("td", { class: "num", text: pct(r.recv_won, r.recv_rallies) }),
      el("td", { class: "num", text: pct(r.serve_won, r.serve_rallies) }),
      el("td", { class: "num", text: r.kills || 0 }),
      el("td", { class: "num", text: r.attack_errors || 0 }),
      el("td", { class: "num", text: fmt(hit, 3) }),
    ]));
  });
  return table;
}

// SVG namespace element helper (el() makes HTML elements; SVG needs createElementNS).
function svgEl(tag, attrs, children) {
  const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  if (attrs) for (const k in attrs) if (attrs[k] != null) n.setAttribute(k, attrs[k]);
  (Array.isArray(children) ? children : [children]).forEach((ch) => {
    if (ch != null) n.appendChild(typeof ch === "string" ? document.createTextNode(ch) : ch);
  });
  return n;
}

// One set's score-progression chart (step lines for each team) + a compact per-set stat summary.
// Reads the /pbp timeline (each point carries the running away_score/home_score) — no extra fetch.
function setScoreChartCard(s, c, awayNm, homeNm) {
  const card = el("div", { class: "card set-chart-card" });
  const meta = [];
  if (s.ties != null) meta.push(`Ties: ${s.ties}`);
  if (s.lead_changes != null) meta.push(`Lead changes: ${s.lead_changes}`);
  card.appendChild(el("div", { class: "card-title" }, [
    el("span", { text: `Set ${s.set_number}` }),
    meta.length ? el("span", { class: "muted set-chart-meta", text: meta.join("  ·  ") }) : null,
  ]));
  card.appendChild(el("div", { class: "set-chart-row" }, [
    setScoreChartSvg(s, awayNm, homeNm),
    setChartSummary(s, c, awayNm, homeNm),
  ]));
  return card;
}

// The step-line SVG. away = accent (blue), home = neutral (dark). Responsive via viewBox.
function setScoreChartSvg(s, awayNm, homeNm) {
  const pts = (s.timeline || []).filter((p) => p.away_score != null && p.home_score != null);
  const away = [0], home = [0];
  pts.forEach((p) => { away.push(p.away_score); home.push(p.home_score); });
  const W = 340, H = 190, padL = 26, padR = 8, padT = 10, padB = 26;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const xMax = Math.max(1, away.length - 1);
  const finalMax = Math.max(25, ...away, ...home);
  const yMax = Math.ceil(finalMax / 5) * 5;  // round up to a 5-line
  const x = (i) => padL + (i / xMax) * plotW;
  const y = (v) => padT + plotH - (v / yMax) * plotH;
  // Proper step path: hold the value, then jump when it changes.
  const stepPath = (arr) => {
    let d = `M ${x(0).toFixed(1)} ${y(arr[0]).toFixed(1)}`;
    for (let i = 1; i < arr.length; i++) {
      d += ` H ${x(i).toFixed(1)} V ${y(arr[i]).toFixed(1)}`;
    }
    return d;
  };
  const grid = [];
  for (let v = 0; v <= yMax; v += 5) {
    grid.push(svgEl("line", { class: "sc-grid", x1: padL, y1: y(v), x2: W - padR, y2: y(v) }));
    grid.push(svgEl("text", { class: "sc-axis", x: padL - 5, y: y(v) + 3, "text-anchor": "end" }, String(v)));
  }
  const svg = svgEl("svg", {
    class: "set-chart-svg", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "xMidYMid meet",
    role: "img", "aria-label": `Set ${s.set_number} score progression`,
  }, [
    ...grid,
    svgEl("path", { class: "sc-line sc-home", d: stepPath(home) }),
    svgEl("path", { class: "sc-line sc-away", d: stepPath(away) }),
  ]);
  const legend = el("div", { class: "sc-legend" }, [
    el("span", { class: "sc-key" }, [el("span", { class: "sc-swatch sc-away" }), el("span", { text: awayNm })]),
    el("span", { class: "sc-key" }, [el("span", { class: "sc-swatch sc-home" }), el("span", { text: homeNm })]),
  ]);
  return el("div", { class: "set-chart-plot" }, [svg, legend]);
}

// Compact per-set stat summary (K / E / B / H% / Pts per team), mirroring the NCAA set box.
function setChartSummary(s, c, awayNm, homeNm) {
  const ROWS = [
    { t: c.away_team, nm: awayNm, agg: s.away },
    { t: c.home_team, nm: homeNm, agg: s.home },
  ];
  const cell = (agg, key) => {
    if (key === "hit_pct") {
      const v = agg.attack_attempts ? (agg.kills - (agg.attack_errors || 0)) / agg.attack_attempts : null;
      return fmt(v, 3);
    }
    return agg[key] ?? 0;
  };
  const head = el("tr", {}, [
    el("th", { class: "l", text: "" }),
    el("th", { class: "num", text: "K", title: "Kills" }),
    el("th", { class: "num", text: "E", title: "Attack errors" }),
    el("th", { class: "num", text: "B", title: "Block points" }),
    el("th", { class: "num", text: "H%", title: "Hitting %" }),
    el("th", { class: "num", text: "Pts", title: "Points" }),
  ]);
  const tb = el("tbody");
  ROWS.forEach((r) => {
    tb.appendChild(el("tr", {}, [
      el("td", { class: "l" }, ovTeamCol(r.t, r.nm)),
      el("td", { class: "num", text: cell(r.agg, "kills") }),
      el("td", { class: "num", text: cell(r.agg, "attack_errors") }),
      el("td", { class: "num", text: cell(r.agg, "blocks") }),
      el("td", { class: "num", text: cell(r.agg, "hit_pct") }),
      el("td", { class: "num sc-pts", text: cell(r.agg, "points") }),
    ]));
  });
  return el("table", { class: "set-chart-summary dense-table" }, [el("thead", {}, head), tb]);
}

// Per-team leaders in the marquee categories (points, kills, assists, digs, blocks). Each cell names
// the top player on that team for the stat, with the value.
function gameLeadersCard(c, awayStats, homeStats, awayNm, homeNm) {
  // Each category names a value accessor (blocks aren't a per-player field — derive via blocksOf).
  const CATS = [
    { label: "Points", val: (r) => Number(r.pts) || 0, int: false, d: 1 },
    { label: "Kills", val: (r) => Number(r.kills) || 0, int: true },
    { label: "Assists", val: (r) => Number(r.assists) || 0, int: true },
    { label: "Digs", val: (r) => Number(r.digs) || 0, int: true },
    { label: "Blocks", val: (r) => blocksOf(r), int: false, d: 1 },
  ];
  const topBy = (rows, val) => rows.reduce((best, r) =>
    val(r) > (best ? val(best) : -1) ? r : best, null);
  const cell = (rows, cat) => {
    const p = topBy(rows, cat.val);
    const n = p ? cat.val(p) : 0;
    const v = cat.int ? fmtInt(n) : fmt(n, cat.d);
    return el("div", { class: "ov-cell lead-cell" }, p && n > 0
      ? [el("span", { class: "lead-name", text: p.player_name || ("#" + p.player_id) }),
         el("span", { class: "lead-val", text: v })]
      : [el("span", { class: "muted", text: "—" })]);
  };
  const card = el("div", { class: "card ov-leaders" });
  card.appendChild(el("div", { class: "card-title" }, [el("span", { text: "Game leaders" })]));
  const grid = el("div", { class: "ov-grid" }, [
    el("div", { class: "ov-cell ov-head", text: "" }),
    el("div", { class: "ov-cell ov-head" }, ovTeamCol(c.away_team, awayNm)),
    el("div", { class: "ov-cell ov-head" }, ovTeamCol(c.home_team, homeNm)),
  ]);
  CATS.forEach((cat) => {
    grid.appendChild(el("div", { class: "ov-cell ov-label", text: cat.label }));
    grid.appendChild(cell(awayStats, cat));
    grid.appendChild(cell(homeStats, cat));
  });
  card.appendChild(grid);
  return card;
}

// Team Stats tab: both teams' key totals side by side — one column per team (logo header), one row
// per stat, using the same centered card layout the Overview tab used to carry.
function teamStatsTab(c, awayStats, homeStats) {
  const awayNm = c.away_team ? (c.away_team.short_name || c.away_team.name) : "Away";
  const homeNm = c.home_team ? (c.home_team.short_name || c.home_team.name) : "Home";
  const at = gameTeamTotals(awayStats), ht = gameTeamTotals(homeStats);
  const KEYS = [
    { label: "Kills", get: (x) => fmtInt(x.kills) },
    { label: "Hit %", get: (x) => fmt(x.hit_pct, 3) },
    { label: "Assists", get: (x) => fmtInt(x.assists) },
    { label: "Aces", get: (x) => fmtInt(x.aces) },
    { label: "Digs", get: (x) => fmtInt(x.digs) },
    { label: "Blocks", get: (x) => fmt(x.total_blocks, 1) },
  ];
  const card = el("div", { class: "card ov-teamstats" });
  card.appendChild(el("div", { class: "card-title" }, [el("span", { text: "Team stats" })]));
  const grid = el("div", { class: "ov-grid" }, [
    el("div", { class: "ov-cell ov-head" }, ""),
    el("div", { class: "ov-cell ov-head" }, ovTeamCol(c.away_team, awayNm)),
    el("div", { class: "ov-cell ov-head" }, ovTeamCol(c.home_team, homeNm)),
  ]);
  KEYS.forEach((k) => {
    grid.appendChild(el("div", { class: "ov-cell ov-label", text: k.label }));
    grid.appendChild(el("div", { class: "ov-cell num", text: k.get(at) }));
    grid.appendChild(el("div", { class: "ov-cell num", text: k.get(ht) }));
  });
  card.appendChild(grid);
  return card;
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
  if (c.location || c.attendance != null) {
    const bits = [];
    if (c.location) bits.push(c.location);
    if (c.attendance != null) bits.push("Attendance: " + c.attendance.toLocaleString());
    card.appendChild(el("div", { class: "gh-venue muted", text: bits.join("  ·  ") }));
  }
  if (c.ncaa_game_id) {
    card.appendChild(el("div", { class: "gh-ncaa" }, el("a", {
      class: "link ncaa-link", href: ncaaGameUrl(c.ncaa_game_id),
      target: "_blank", rel: "noopener",
    }, "View on NCAA.com ↗")));
  }
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

// Renders the full box-score table (every stat group + team totals) for one team into `container`.
function renderBoxBody(container, stats, playerClick) {
  clear(container);
  const rows = stats.slice().sort((a, b) => (b.pts || 0) - (a.pts || 0));
  const head = statHead("box", (c) => el("th", { text: c.label, title: c.title || c.label }));
  const tb = el("tbody");
  rows.forEach((s) => {
    // Jersey number only — position/class/height now render as their own Pos/Cls/Ht columns.
    const gutter = el("div", { class: "box-num" }, [
      s.number != null ? el("span", { class: "jersey", text: s.number }) : null,
    ]);
    const nameCell = el("div", { class: "box-player" }, [
      gutter,
      el("a", { class: "link box-name", onclick: () => playerClick(s.player_id) }, s.player_name || ("#" + s.player_id)),
    ]);
    const tr = el("tr", {}, [
      el("td", { class: "l sticky-col" }, [nameCell]),
    ]);
    head.cols.forEach((c) => tr.appendChild(statCell(c, s)));
    tb.appendChild(tr);
  });
  // Team totals: sum the additive columns; Hit%/Blk/rates/FP recompute from the sums via their calc
  // fns. Sets/games are per-player, so blank them (but keep total.sets for the per-set rate calcs).
  const total = {};
  STAT_SUM_KEYS.forEach((k) => { total[k] = rows.reduce((a, s) => a + (Number(s[k]) || 0), 0); });
  total.sets = rows.reduce((m, s) => Math.max(m, Number(s.sets) || 0), 0);
  total.total_blocks = teamBlocksOf(total);  // official team blocks (half-weight assists); may be .5
  const totalRow = el("tr", { class: "total-row" }, [
    el("td", { class: "l sticky-col", text: "Team" }),
  ]);
  head.cols.forEach((c) =>
    totalRow.appendChild(c.key === "sets" || c.key === "games" || c.str
      ? el("td", { class: "num muted", text: "" })  // sets/games + bio (Pos/Cls/Ht) have no team total
      : c.key === "total_blocks"
        ? el("td", { class: "num", text: fmt(total.total_blocks, 1) })
        : statCell(c, total)));
  tb.appendChild(totalRow);
  const table = el("table", { class: "wide-table dense-table box-table" },
    [el("thead", {}, head.rows), tb]);
  container.appendChild(el("div", { class: "table-scroll" }, table));
}

// One team's box score. `filter` = { cur, contestId } enables the Position / Phase / Setter hitting
// filters (per-game FBSO/transition + per-setter splits, same engine as the team page); omit it for
// a plain box score.
function boxScoreCard(team, stats, onPlayer, filter) {
  const playerClick = onPlayer || openPlayer;  // modal passes a drill-in-overlay handler
  const name = team ? (team.short_name || team.name) : "Team";
  const card = el("div", { class: "card" });
  card.appendChild(el("div", { class: "card-title" }, [
    teamLogoImg(team, "game-logo"),
    team ? el("a", { class: "link", onclick: () => openTeam(team.id, name) }, name) : el("span", { text: name }),
    el("span", { class: "badge", text: "box score" }),
    advToggle(),
  ]));
  if (!stats.length) {
    card.appendChild(el("div", { class: "empty-state", text: "No player stats recorded." }));
    return card;
  }
  const body = el("div");
  if (filter && team) {
    const filterHolder = el("div"); card.appendChild(filterHolder); card.appendChild(body);
    // Normalize to the field names the shared engine / team table expect (they use `name`).
    const rows = stats.map((s) => Object.assign({}, s, { name: s.player_name || ("#" + s.player_id) }));
    makeHittingFilter({
      holder: filterHolder, body, cur: filter.cur, baseRows: rows,
      fetchSplits: (v) => api(`/teams/${team.id}/attack-splits`,
        { season: state.season, contest_id: filter.contestId, setter_player_id: v }),
      renderFull: (rws) => renderBoxBody(body, rws, playerClick),
      renderHitting: (rws, opts) => renderTeamTable(body, rws, Object.assign({ onPlayer: playerClick }, opts)),
    });
  } else {
    card.appendChild(body);
    renderBoxBody(body, stats, playerClick);
  }
  return card;
}

// Per-set touch aggregates shown in the Play-by-play card. Short labels + tooltips echo the
// box score. SA here is *set attempts* (every set touch); ACE is service aces (a separate column).
// Pts is the set score (rallies won) and is highlighted; HIT% = (kills − attack errors) ÷ attacks.
// Play-by-play card: a reconstructed rally log (one line per scored point). The per-set touch
// aggregates now live in the Overview set charts, so this tab is just the rally log. Pure function
// of the /pbp payload (fetched by the caller); returns null when there's no PBP so the caller hides
// the tab cleanly.
function pbpCard(pbp, c) {
  if (!pbp || !pbp.sets || !pbp.sets.length) return null;
  const awayNm = c.away_team ? (c.away_team.short_name || c.away_team.name) : "Away";
  const homeNm = c.home_team ? (c.home_team.short_name || c.home_team.name) : "Home";

  const card = el("div", { class: "card" });
  card.appendChild(el("div", { class: "card-title" }, [
    el("span", { text: "Play-by-play" }),
    el("span", { class: "badge", text: "beta" }),
  ]));
  card.appendChild(pbpRallyLog(pbp, c, awayNm, homeNm));
  return card;
}

// Humanize a terminal_type into a scoring phrase: "kill" → "Kill", "attack_error" → "Attack error".
function terminalPhrase(tt) {
  if (!tt) return "Point";
  if (tt === "kill") return "Kill";
  if (tt === "ace") return "Ace";
  if (tt === "block") return "Block";
  const words = tt.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);  // "attack error" → "Attack error"
}

// Reconstructed rally log: one line per scored point, grouped by set. Reads the extended timeline
// (scorer + assisting setter surfaced by /contests/{id}/pbp). Shows the running score, the scoring
// team, and a sentence like "Kill by A. Smith, assisted by J. Lee". Collapsed under <details> so it
// doesn't dominate the card; open the set you care about.
function pbpRallyLog(pbp, c, awayNm, homeNm) {
  const teamNm = (id) => (id === c.away_team_id ? awayNm : id === c.home_team_id ? homeNm : "");
  const wrap = el("div", { class: "pbp-log" });
  wrap.appendChild(el("div", { class: "pbp-log-title muted", text: "Rally log" }));
  pbp.sets.forEach((s) => {
    const points = (s.timeline || []).filter((p) => p.terminal_type || p.scorer_name);
    if (!points.length) return;
    const list = el("div", { class: "pbp-log-list" });
    points.forEach((p) => {
      const scorer = p.scorer_name || "";
      const phrase = terminalPhrase(p.terminal_type);
      let text = scorer ? `${phrase} by ${scorer}` : phrase;
      if (p.terminal_type === "kill" && p.assist_name) text += `, assisted by ${p.assist_name}`;
      const scoreTxt = (p.away_score != null && p.home_score != null) ? `${p.away_score}–${p.home_score}` : "";
      const scoringSide = p.scoring_team_id === c.away_team_id ? "away"
        : p.scoring_team_id === c.home_team_id ? "home" : "";
      list.appendChild(el("div", { class: "pbp-rally" }, [
        el("span", { class: "pbp-rally-score", text: scoreTxt }),
        el("span", { class: "pbp-rally-team " + scoringSide, text: teamNm(p.scoring_team_id) }),
        el("span", { class: "pbp-rally-text", text }),
      ]));
    });
    wrap.appendChild(el("details", { class: "pbp-log-set", open: true }, [
      el("summary", {}, `Set ${s.set_number}`),
      list,
    ]));
  });
  return wrap;
}

/* ---------- Team detail (roster) ---------- */
async function openTeam(id, name) {
  state.teamId = id;
  state.teamName = name;
  setTab("team");
}

// Open a played game's box score in a large modal overlay (header + both teams' box scores).
// A direct #game?cid=… deep-link still renders the full page via renderGame(); in-app clicks
// use the modal so you don't lose your place in the scoreboard. Clicking a player name drills
// into that player *inside* the overlay (with a "← Box score" back step); a team name closes the
// modal and navigates to the full team page.
let _gameModalBack = null;  // when set, Escape pops one level (player → box score) instead of closing
let _gameModalRerender = null;  // redraws the current modal level in place (used by the Advanced toggle)

function closeGameModal() {
  const m = $("#game-modal");
  if (!m) return;
  m.hidden = true;
  m.onclick = null;
  _gameModalBack = null;
  _gameModalRerender = null;
  clear(m);
  document.body.classList.remove("modal-open");
  document.removeEventListener("keydown", gameModalKey);
}
// Widen the open box-score modal for wide content (the Individual Stats tables). No-op on the full
// page or when the modal is closed, so it's safe to call from the shared gameTabs draw(). Lets the
// overlay use the desktop's room instead of scrolling two wide tables inside the 1100px shell.
function setGameModalFit(wide) {
  const m = $("#game-modal");
  if (!m || m.hidden) return;
  const panel = m.querySelector(".modal");
  if (panel) panel.classList.toggle("modal-fit", !!wide);
}
function gameModalKey(e) {
  if (e.key !== "Escape") return;
  if (_gameModalBack) _gameModalBack();  // step back to the box score first
  else closeGameModal();
}

// A modal head row: either a plain title, or a "← <back label>" link when `onBack` is given.
function modalHead(onBack, backLabel, title) {
  return el("div", { class: "modal-head" }, [
    onBack
      ? el("a", { class: "link modal-back", onclick: onBack }, "← " + backLabel)
      : el("h2", { text: title }),
    el("button", { class: "icon-btn", onclick: closeGameModal, title: "Close" }, "×"),
  ]);
}

async function openGame(cid) {
  state.contestId = cid;
  const m = clear($("#game-modal"));
  m.hidden = false;
  m.onclick = closeGameModal;  // click the backdrop to dismiss
  document.body.classList.add("modal-open");  // lock background scroll (esp. on mobile)
  const panel = el("div", { class: "modal modal-xl" });
  panel.addEventListener("click", (e) => e.stopPropagation());
  m.appendChild(panel);
  document.addEventListener("keydown", gameModalKey);
  showBoxScoreInModal(panel, cid);
}

// Level 1: the tabbed game view. Player clicks drill into showPlayerInModal within the same panel.
async function showBoxScoreInModal(panel, cid) {
  _gameModalBack = null;  // top level — Escape closes
  panel.classList.remove("modal-fit");  // back to the box-score width (player level widens it)
  clear(panel);
  panel.appendChild(modalHead(null, null, "Game"));
  const body = el("div", { class: "modal-xl-body" }); panel.appendChild(body);
  const holder = el("div"); body.appendChild(holder); spinner(holder);
  try {
    const [c, stats, pbp] = await Promise.all([
      api(`/contests/${cid}`),
      api(`/contests/${cid}/stats`).catch(() => []),
      apiCached(`/contests/${cid}/pbp`).catch(() => null),
    ]);
    const drill = (pid) => showPlayerInModal(panel, pid);
    // Redraw from already-fetched data so the Advanced toggle re-renders in place without refetching.
    const draw = () => {
      clear(holder);
      holder.appendChild(gameHeader(c));
      holder.appendChild(gameTabs(c, stats, pbp, { playerClick: drill }));
    };
    _gameModalRerender = draw;
    draw();
  } catch (e) { clear(holder); emptyState(holder, "Error: " + e.message); }
}

// Level 2: a player drilled into from the box score, with a back step to the box score.
async function showPlayerInModal(panel, playerId) {
  const cid = state.contestId;
  _gameModalBack = () => showBoxScoreInModal(panel, cid);  // Escape / back → box score
  panel.classList.add("modal-fit");  // wide game-log table — grow toward the viewport on desktop
  clear(panel);
  panel.appendChild(modalHead(_gameModalBack, "Box score", null));
  const body = el("div", { class: "modal-xl-body" }); panel.appendChild(body);
  const holder = el("div"); body.appendChild(holder);
  _gameModalRerender = () => renderPlayerBody(holder, playerId);  // Advanced toggle redraws in place
  await renderPlayerBody(holder, playerId);
}

// The team roster table shares STAT_GROUPS/STAT_COLS with the box score (see the unified stat model
// above), so the two tables always show the same columns and grouping — only the values differ.

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
  // Pass the season so the header shows the team's conference *for that season* (realignment).
  const teamP = api(`/teams/${id}`, { season: state.season });
  teamP.then((t) => {
    renderTeamInfoCard(info, t);
  }).catch(() => { clear(info); info.remove(); });

  const cur = f();
  root.appendChild(el("div", { class: "filters team-scope" },
    scopeFields(() => renderTeamDetail(clear(root)))));

  // Schedule & Results — scoped to the same Season/Week selector as the player stats below.
  // Collapsed by default; the inner Results/Upcoming sections keep their own expand rules once opened.
  const { card: schedCard, body: schedBody } = collapsibleCard(
    "Schedule & Results", [el("span", { class: "badge", text: scopeLabel() })]);
  spinner(schedBody);
  root.appendChild(schedCard);
  const schedParams = { season: state.season };
  if (cur.scope === "week" && cur.week) schedParams.week = cur.week;
  Promise.all([apiCached(`/teams/${id}/games`, schedParams), teamP.catch(() => null)]).then(([games, t]) => {
    clear(schedBody);
    if (!games.length) {
      emptyState(schedBody, cur.scope === "week" ? "No games this week." : "No games for this season.");
      return;
    }
    renderTeamGames(schedBody, games, cur.scope === "week", t);
  }).catch(() => { clear(schedBody); emptyState(schedBody, "Could not load schedule."); });

  // Quality wins — wins over an opponent ranked (top 25) as of the game date. Toggle AVCA vs RPI.
  // Collapsed by default; the AVCA/RPI toggle stops propagation so it doesn't also toggle the card.
  let qwPoll = "avca";
  const qwToggle = el("div", { class: "seg-toggle" });
  const { card: qwCard, body: qwBody } = collapsibleCard("Quality wins", [
    qwToggle,
    el("span", { class: "muted table-hint", text: "beat a top-25 team (rank as of game day)" }),
  ]);
  const setPoll = (p) => {
    qwPoll = p;
    Array.from(qwToggle.children).forEach((b) => b.classList.toggle("active", b.dataset.poll === p));
    clear(qwBody); spinner(qwBody);
    api(`/teams/${id}/quality-wins`, { season: state.season, poll: qwPoll, threshold: 25 })
      .then((res) => { clear(qwBody); renderQualityWins(qwBody, res); })
      .catch(() => { clear(qwBody); emptyState(qwBody, "Could not load quality wins."); });
  };
  [["avca", "AVCA"], ["rpi", "RPI"]].forEach(([p, label]) =>
    qwToggle.appendChild(el("button", { class: "seg-btn", "data-poll": p,
      onclick: (e) => { e.stopPropagation(); setPoll(p); } }, label)));
  root.appendChild(qwCard);
  setPoll("avca");

  const card = el("div", { class: "card" }, el("div", { class: "card-title" }, [
    "Player stats", el("span", { class: "badge", text: scopeLabel() }),
    advToggle(),
  ]));
  const filterHolder = el("div"); card.appendChild(filterHolder);  // hitting filters (season scope)
  const body = el("div"); card.appendChild(body); spinner(body); root.appendChild(card);

  try {
    const baseRows = await api(`/teams/${id}/player-stats`, Object.assign(scopeParams(), weightParams()));
    clear(body);
    if (!baseRows.length) { emptyState(body, "No stats for this team in the selected scope."); return; }

    // Season and Week both work: Season reads the batch FBSO/transition columns, Week (and the
    // per-game box score) get them from a live play-by-play replay on the server.
    makeHittingFilter({
      holder: filterHolder, body, cur, baseRows,
      fetchSplits: (v) => api(`/teams/${id}/attack-splits`,
        Object.assign({ season: state.season, setter_player_id: v },
          cur.scope === "week" && cur.week ? { week: cur.week } : {})),
      renderFull: (rows) => renderTeamTable(body, rows),
      renderHitting: (rows, opts) => renderTeamTable(body, rows, opts),
    });
  } catch (e) { clear(body); emptyState(body, "Error: " + e.message); }
}

// The default per-view hitting-filter state (Position / Phase / Setter), shared by the team stats
// table and each per-game box-score card.
function defaultHitting() { return { pos: "", phase: "all", setter: "" }; }

// Shared hitting-filter engine. `cur` holds the filter selections; `baseRows` are the unfiltered
// stat rows (must carry name/player_id/position plus the fbso_*/trans_* counts and
// setter_hit_attacks). `fetchSplits(setterId)` returns a promise of the live per-setter attacking
// split; `renderFull(rows)` draws the unfiltered / position-only view; `renderHitting(rows, opts)`
// draws the hitting-only view. Used by both the team page (Season + Week) and the box score.
function makeHittingFilter({ holder, body, cur, baseRows, fetchSplits, renderFull, renderHitting }) {
  if (cur.phase == null) cur.phase = "all";
  let splitRows = null, splitSetter = null;  // cached attack-splits for the picked setter

  // A "hitting filter" (setter and/or first-ball/transition phase) reshapes the table to hitting
  // only; a bare position filter just prunes rows and keeps the full view.
  const hittingActive = () => !!cur.setter || (cur.phase && cur.phase !== "all");

  function currentRows() {
    let src = (cur.setter && splitRows) ? splitRows : baseRows;
    src = src.map((r) => Object.assign({}, r));  // copy so the phase remap doesn't mutate base
    if (cur.phase === "fbso" || cur.phase === "transition") {
      const p = cur.phase === "fbso" ? "fbso_" : "trans_";
      src.forEach((r) => {
        r.kills = r[p + "kills"]; r.errors = r[p + "errors"]; r.total_attacks = r[p + "attacks"];
      });
    }
    // A setter can't set themselves, so never list the selected setter as a hitter.
    if (cur.setter) src = src.filter((r) => String(r.player_id) !== String(cur.setter));
    if (cur.pos) src = src.filter((r) => (r.position || "").toUpperCase() === cur.pos);
    // Only show players with stats under an active hitting filter.
    if (hittingActive()) src = src.filter((r) => (Number(r.total_attacks) || 0) > 0);
    return src;
  }

  function renderNow() {
    const rows = currentRows();
    clear(body);
    if (!rows.length) {
      emptyState(body, cur.setter
        ? "No attacks off that setter with the current filters."
        : "No players match the current filters.");
      return;
    }
    if (hittingActive()) {
      renderHitting(rows, {
        hittingOnly: true,
        hidePhaseCols: cur.phase === "fbso" || cur.phase === "transition",
        hideKS: !!cur.setter,
      });
    } else {
      renderFull(rows);
    }
  }

  async function pickSetter(v) {
    cur.setter = v;
    if (!v) { splitRows = null; splitSetter = null; renderNow(); return; }
    if (splitSetter !== v) {
      splitSetter = v; splitRows = null;
      clear(body); spinner(body);
      try { splitRows = await fetchSplits(v); }
      catch (e) { clear(body); emptyState(body, "Could not load setter splits."); return; }
    }
    renderNow();
  }

  buildTeamFilterBar(holder, baseRows, cur, pickSetter, renderNow);
  if (cur.setter) pickSetter(cur.setter);  // re-entry with a setter: load its splits
  else renderNow();
}

// The hitting-filter bar: Position (client-side), Phase (first-ball SO / transition), and Setter (a
// live per-setter attacking split). Shared by the team stats table (Season + Week) and the per-game
// box score — the FBSO/transition + setter splits all come from play-by-play. The controls live in a
// collapsible section (collapsed by default) with an active-filter count and a Reset button.
function buildTeamFilterBar(holder, baseRows, cur, pickSetter, renderNow) {
  clear(holder);
  const bar = el("div", { class: "filters team-filters" });

  const activeCount = () =>
    (cur.pos ? 1 : 0) + (cur.phase && cur.phase !== "all" ? 1 : 0) + (cur.setter ? 1 : 0);

  const posSel = posSelect(cur.pos, (v) => { cur.pos = v; updateCount(); renderNow(); });
  bar.appendChild(field("Position", posSel));

  const phaseToggle = el("div", { class: "seg-toggle" });
  [["all", "All"],
   ["fbso", "FBSO"],
   ["transition", "Transition"]].forEach(([v, l]) =>
    phaseToggle.appendChild(el("button", {
      class: "seg-btn" + (cur.phase === v ? " active" : ""),
      "data-phase": v,
      title: v === "fbso"
        ? "First-ball side-out — the receiving team's first attack after a serve reception"
        : v === "transition"
          ? "Transition — every attack that isn't a first-ball side-out"
          : "All attacks",
      onclick: () => {
        cur.phase = v;
        Array.from(phaseToggle.children).forEach((b) =>
          b.classList.toggle("active", b.dataset.phase === v));
        updateCount();
        renderNow();
      },
    }, l)));
  bar.appendChild(el("div", { class: "field" }, [el("span", { text: "Phase" }), phaseToggle]));

  // The dropdown lists anyone who could have set: rostered setters (position S) plus anyone who has
  // run sets in the play-by-play (a libero/DS on an out-of-system dig, an OH on an overpass, etc.).
  // Rostered setters are marked with a ★ so they stand out from the incidental setters.
  const setters = baseRows
    .filter((r) => (Number(r.setter_hit_attacks) > 0) || r.position === "S")
    .sort((a, b) => (Number(b.setter_hit_attacks) || 0) - (Number(a.setter_hit_attacks) || 0));
  const setterSel = el("select",
    { title: "★ = rostered setter", onchange: (e) => { pickSetter(e.target.value); updateCount(); } });
  setterSel.appendChild(el("option", { value: "", text: "All players" }));
  setters.forEach((s) => setterSel.appendChild(
    el("option", { value: s.player_id, text: s.position === "S" ? "★ " + s.name : s.name })));
  setterSel.value = cur.setter || "";
  bar.appendChild(field("Setter", setterSel));

  // Reset: clear every selection back to defaults and re-sync the controls in place (so the section
  // stays open). pickSetter("") clears the setter and re-renders.
  const resetBtn = el("button", {
    class: "btn-reset", type: "button",
    onclick: () => {
      cur.pos = "";
      cur.phase = "all";
      posSel.value = "";
      Array.from(phaseToggle.children).forEach((b) =>
        b.classList.toggle("active", b.dataset.phase === "all"));
      setterSel.value = "";
      pickSetter("");   // clears setter state + re-renders the table
      updateCount();
    },
  }, "Reset Filters");
  bar.appendChild(el("div", { class: "field" }, [el("span", { text: " " }), resetBtn]));

  // Collapsible wrapper — collapsed by default (fresh each render, not persisted in state).
  const chev = el("span", { class: "chev", text: "▸" });
  const countBadge = el("span", { class: "filter-count", hidden: true });
  const head = el("button", { class: "filter-collapse-head", type: "button",
    onclick: () => setOpen(!open) }, [chev, el("span", { text: "Filters" }), countBadge]);
  const bodyWrap = el("div", { class: "filter-collapse-body" }, [bar]);
  const wrap = el("div", { class: "filter-collapse" }, [head, bodyWrap]);
  let open = false;
  function setOpen(v) {
    open = v;
    bodyWrap.hidden = !open;
    chev.textContent = open ? "▾" : "▸";
    wrap.classList.toggle("open", open);
  }
  function updateCount() {
    const n = activeCount();
    countBadge.textContent = n ? String(n) : "";
    countBadge.hidden = !n;
    head.classList.toggle("has-active", !!n);
  }
  setOpen(false);
  updateCount();

  holder.appendChild(wrap);
  // Within a covered match every attack is classified fbso XOR transition, so fbso+trans == total
  // attacks exactly. The only way the splits fall short of the box-score totals is if some matches
  // in this scope have no play-by-play at all — detect that and only then warn about the mismatch.
  const totalAtt = baseRows.reduce((s, r) => s + (Number(r.total_attacks) || 0), 0);
  const splitAtt = baseRows.reduce(
    (s, r) => s + (Number(r.fbso_attacks) || 0) + (Number(r.trans_attacks) || 0), 0);
  if (totalAtt - splitAtt > 0.5) {
    holder.appendChild(el("div", { class: "muted filter-note",
      text: "Some matches in this view have no play-by-play, so the first-ball / transition "
          + "splits won't add up to the box-score attack totals." }));
  }
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
  const t = {};
  STAT_SUM_KEYS.forEach((k) => {
    let any = false, s = 0;
    rows.forEach((r) => { if (r[k] != null) { any = true; s += Number(r[k]); } });
    t[k] = any ? s : null;
  });
  const maxOf = (k) => {
    const vals = rows.map((r) => r[k]).filter((v) => v != null).map(Number);
    return vals.length ? Math.max(...vals) : null;
  };
  // GP and sets are the team's own totals, not a sum of per-player lines (each line already reflects
  // the team's games/sets). Hit%, total blocks, per-set rates and FP recompute from these via each
  // column's `calc` when the totals row is rendered with statCell.
  t.games = maxOf("games");
  t.sets = maxOf("sets");
  // Official team blocks: half-weight block assists (summing per-player totals double-counts them).
  t.total_blocks = (t.block_solos != null || t.block_assists != null) ? teamBlocksOf(t) : null;
  return t;
}

function renderTeamTable(body, rows, opts) {
  const hittingOnly = opts && opts.hittingOnly;
  const onPlayer = (opts && opts.onPlayer) || openPlayer;  // box score modal drills in an overlay
  // Columns to drop for the active hitting filter:
  //  - single phase: Hit%/K% already reflect it, so the ATK% FBSO/TRANS comparison cols are
  //    redundant; K/S isn't phase-split so it would show the misleading season rate.
  //  - setter picked: the per-setter split has no per-set data, so K/S is meaningless.
  const hidden = new Set();
  if (opts && opts.hidePhaseCols) ["atk_pct_fbso", "atk_pct_trans", "kills_per_set"].forEach((k) => hidden.add(k));
  if (opts && opts.hideKS) hidden.add("kills_per_set");
  const hideCols = hidden.size ? hidden : null;
  // Default sort follows the leading value column: total attacks under a hitting filter, else FP
  // when fantasy is on / total Points when off.
  const sort = state.teamSort
    || { key: hittingOnly ? "total_attacks" : (fantasyActive() ? "fantasy_points" : "pts"), dir: -1 };
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
  const table = el("table", { class: "wide-table dense-table box-table team-box" });
  // Shared grouped header + column set (same model as the box score), with sortable column headers.
  const head = statHead("team", (c) => el("th", {
    class: "sortable" + (sort.key === c.key ? " sorted" : ""),
    text: c.label,
    title: c.title || c.label,
    onclick: () => {
      state.teamSort = { key: c.key, dir: sort.key === c.key ? -sort.dir : -1 };
      renderTeamTable(body, rows, opts);
    },
  }), { hittingOnly, hideCols });
  table.appendChild(el("thead", {}, head.rows));
  const tb = el("tbody");
  sorted.forEach((r) => {
    // Position has its own Pos column on the full table; under a hitting filter that column is
    // dropped, so fall back to showing it under the jersey number there (avoids double-display).
    const gutter = el("div", { class: "box-num" }, [
      r.number != null ? el("span", { class: "jersey", text: r.number }) : null,
      r.position && hittingOnly ? el("span", { class: "box-pos", text: r.position }) : null,
    ]);
    // Height has its own Ht column on the full table; under a hitting filter that column is dropped,
    // so fall back to showing it under the name there (mirrors the position fallback below).
    const ht = heightStr(r.height_inches);
    const nameStack = el("div", { class: "box-name-stack" }, [
      el("a", { class: "link box-name", onclick: () => onPlayer(r.player_id) }, r.name),
      ht && hittingOnly ? el("span", { class: "ht-tag box-ht", text: ht }) : null,
    ]);
    const tr = el("tr", {}, el("td", { class: "l sticky-col" + (isFav("player", r.player_id) ? " is-fav" : "") }, [
      el("div", { class: "box-player" }, [favStar("player", r.player_id), gutter, nameStack]),
    ]));
    head.cols.forEach((c) => tr.appendChild(statCell(c, r)));
    tb.appendChild(tr);
  });
  // Team cumulative totals footer.
  const totals = teamTotals(rows);
  const ttr = el("tr", { class: "total-row" },
    el("td", { class: "l sticky-col", text: "Team totals" }));
  head.cols.forEach((c) => ttr.appendChild(
    c.key === "total_blocks"
      ? el("td", { class: "num", text: totals.total_blocks != null ? fmt(totals.total_blocks, 1) : "—" })
      : statCell(c, totals)));
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
// Fantasy, Games and Favorites are current-season features — all hidden on a historical season.
function updateTabVisibility() {
  $$("#tabs button[data-auth]").forEach((b) => { b.hidden = !state.user; });
  $$("#tabs button[data-admin]").forEach((b) => { b.hidden = !(state.user && state.user.is_admin); });
  $$("#tabs button[data-ai]").forEach((b) => { b.hidden = !(state.user && state.user.ai_enabled); });
  $$("#tabs button[data-fantasy]").forEach((b) => { b.hidden = !fantasyActive(); });
  $$("#tabs button[data-tab='games']").forEach((b) => { b.hidden = false; });
  $$("#tabs button[data-tab='favorites']").forEach((b) => { b.hidden = !state.user; });
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
      el("button", { class: "menu-item", onclick: () => openSettingsModal() }, "Settings"),
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
    b.appendChild(el("span", { text: "Verify your email to save favorites, use Ask, and save fantasy weights. " }));
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
    const rows = await api("/favorites", { season: state.season });  // favorites are per-season
    state.favoriteRows = rows;
    state.favorites = new Set(rows.map((r) => favKey(r.entity_type, r.entity_id)));
  } catch (e) {
    state.favoriteRows = []; state.favorites = new Set();
  }
}

async function toggleFavorite(type, id) {
  if (!state.user) { openAuthModal("login"); toast("Sign in to save favorites", true); return; }
  if (!state.user.email_verified) { toast("Verify your email to save favorites", true); return; }
  const on = isFav(type, id);
  try {
    if (on) { await req("DELETE", `/favorites/${type}/${id}?season=${state.season}`); state.favorites.delete(favKey(type, id)); }
    else { await req("POST", "/favorites", { entity_type: type, entity_id: id, season: state.season }); state.favorites.add(favKey(type, id)); }
    state.favPlayerContests = {};  // favorite players changed → drop the Games-filter cache
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

// Inline "add a favorite" card for the Favorites tab: a Players/Teams/Conferences segmented toggle
// plus a search box. Players/teams come from /search; conferences filter the client-side list (and
// list all when the box is empty). Clicking a result toggles the favorite, which re-renders the tab.
function addFavoriteCard() {
  const card = el("div", { class: "card fav-add" });
  const toggle = el("div", { class: "seg-toggle" });
  const input = el("input", { class: "compare-search", type: "search", placeholder: "Search players…" });
  const results = el("div", { class: "compare-results" });
  let kind = "player";
  let timer = null;

  const placeholder = { player: "Search players…", team: "Search teams…", conference: "Search conferences…" };

  function renderRows(items) {
    clear(results);
    const fresh = items.filter((it) => !isFav(it.type, it.id));
    if (!fresh.length) {
      results.appendChild(el("div", { class: "muted", text: "No matches" }));
      return;
    }
    fresh.forEach((it) => results.appendChild(el("div", {
      class: "compare-result", onclick: () => toggleFavorite(it.type, it.id),
    }, [
      el("span", {}, it.name),
      el("span", { class: "sub", text: it.sub || "" }),
    ])));
  }

  async function runSearch() {
    const q = input.value.trim();
    if (kind === "conference") {
      const ql = q.toLowerCase();
      const matches = (state.conferences || [])
        .filter((c) => !ql || (`${c.name} ${c.short_name || ""}`).toLowerCase().includes(ql))
        .slice(0, 12)
        .map((c) => ({ type: "conference", id: c.id, name: c.name, sub: c.short_name || "" }));
      renderRows(matches);
      return;
    }
    if (q.length < 2) { clear(results); return; }
    try {
      const res = await api("/search", { q, season: state.season });
      const items = kind === "team"
        ? (res.teams || []).map((t) => ({
            type: "team", id: t.id, name: t.name,
            sub: [t.short_name, t.conference].filter(Boolean).join(" · "),
          }))
        : (res.players || []).map((p) => ({
            type: "player", id: p.id, name: p.name,
            sub: [(p.team_short || p.team), p.position].filter(Boolean).join(" · "),
          }));
      renderRows(items.slice(0, 8));
    } catch {
      clear(results);
      results.appendChild(el("div", { class: "muted", text: "Search failed" }));
    }
  }

  function setKind(k) {
    kind = k;
    Array.from(toggle.children).forEach((b) => b.classList.toggle("active", b.dataset.kind === k));
    input.placeholder = placeholder[k];
    input.value = ""; clear(results);
    if (k === "conference") runSearch();  // list all conferences up front
    input.focus();
  }

  [["player", "Players"], ["team", "Teams"], ["conference", "Conferences"]].forEach(([k, label]) =>
    toggle.appendChild(el("button", { class: "seg-btn", "data-kind": k, onclick: () => setKind(k) }, label)));

  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(runSearch, 200); });
  input.addEventListener("focus", () => { if (kind === "conference" && !input.value) runSearch(); });

  card.appendChild(el("div", { class: "card-title" }, ["Add a favorite", toggle]));
  card.appendChild(input);
  card.appendChild(results);
  Array.from(toggle.children)[0].classList.add("active");
  return card;
}

async function renderFavorites(root) {
  replaceURL();
  root.appendChild(el("div", { class: "view-head" }, [el("h1", { text: "★ Favorites" })]));
  if (!state.user) { emptyState(root, "Sign in to favorite players and teams."); return; }
  root.appendChild(addFavoriteCard());
  const rows = state.favoriteRows || [];
  // Sort each group alphabetically by its displayed name.
  const byName = (key) => (a, b) =>
    (key(a) || "").localeCompare(key(b) || "", undefined, { sensitivity: "base" });
  const players = rows.filter((r) => r.entity_type === "player").sort(byName((r) => r.name));
  const teams = rows.filter((r) => r.entity_type === "team").sort(byName((r) => r.team_short || r.name));
  const confs = rows.filter((r) => r.entity_type === "conference").sort(byName((r) => r.team_short || r.name));
  if (!rows.length) {
    emptyState(root, "No favorites yet. Search above, or tap the ☆ next to any player, team, or conference.");
    return;
  }
  if (confs.length) {
    const card = el("div", { class: "card" });
    card.appendChild(el("div", { class: "card-title" }, ["Conferences", el("span", { class: "badge", text: confs.length })]));
    const grid = el("div", { class: "fav-cards" });
    const entries = confs.map((c) => {
      const { cardEl, stats } = favConfShell(c);
      grid.appendChild(cardEl); return { c, cardEl, stats };
    });
    card.appendChild(grid); root.appendChild(card);
    fillConfCards(entries);  // async, fills each conference card in place
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
    (await apiCached("/team-records", { season: state.season }))
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
        miniBox(`${rec.wins}–${rec.losses}`, "Overall", true),
        miniBox(`${rec.nonconf_wins}–${rec.nonconf_losses}`, "Non-Conference"),
        miniBox(`${rec.conf_wins}–${rec.conf_losses}`, "Conference"),
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
        el("span", { class: "muted", text: [fmtDateShort(next.date), next.game_time ? fmtGameTime(next.date, next.game_time) : ""].filter(Boolean).join(" ") }),
      ]));
    }
    if (!rec && !last && !next) stats.appendChild(el("div", { class: "muted", text: "No games yet this season." }));
  }));
}

function favConfShell(c) {
  const nameRow = el("div", { class: "name-row" }, [
    el("span", { class: "name", text: c.team_short || c.name || "—" }),
  ]);
  const head = el("div", { class: "fav-card-head" }, [
    favStar("conference", c.entity_id),
    confLogoImg(c.entity_id ?? c.name, "conf-logo-head"),
    el("div", { class: "fav-card-title" }, [nameRow, el("div", { class: "muted sub", text: c.name || "" })]),
  ]);
  const stats = el("div", { class: "fav-card-stats" }); spinner(stats);
  const cardEl = el("div", { class: "fav-card fav-conf" }, [head, stats]);
  return { cardEl, stats };
}

// A compact standings list (team + conf W-L) for the top/bottom of a conference card.
function confStandingList(label, teams) {
  const list = el("div", { class: "conf-stand" }, [el("div", { class: "conf-stand-h muted", text: label })]);
  teams.forEach((r) => {
    list.appendChild(el("div", { class: "conf-stand-row" }, [
      teamLogoImg({ logo_light: r.team_logo_light, logo_dark: r.team_logo_dark }, "conf-stand-logo"),
      el("a", { class: "link conf-stand-name", onclick: () => openTeam(r.team_id, r.team_short || r.team) },
        r.team_short || r.team),
      el("span", { class: "conf-stand-rec muted", title: "Overall record", text: `${r.wins}–${r.losses}` }),
    ]));
  });
  return list;
}

async function fillConfCards(entries) {
  await Promise.all(entries.map(async ({ c, stats }) => {
    let d;
    try {
      d = await apiCached(`/conferences/${c.entity_id}/summary`, { season: state.season });
    } catch (e) {
      clear(stats); stats.appendChild(el("div", { class: "muted", text: "Error: " + e.message })); return;
    }
    clear(stats);
    stats.appendChild(el("div", { class: "fav-mini" }, [
      miniBox(`${d.overall_wins}–${d.overall_losses}`, "Overall", true),
      miniBox(`${d.interconf_wins}–${d.interconf_losses}`, "Non-Conf Record"),
      miniBox(d.avg_rpi_rank != null ? String(Math.round(d.avg_rpi_rank)) : "—", "Avg RPI"),
      miniBox(String(d.ranked_count), "Top 25"),
    ]));
    const st = d.standings || [];
    if (st.length) {
      const top = st.slice(0, 4);
      const cols = [confStandingList(`Top ${top.length}`, top)];
      if (st.length > 4) {
        const bottom = st.slice(Math.max(4, st.length - 4));  // last 4, never overlapping the top
        cols.push(confStandingList(`Bottom ${bottom.length}`, bottom));
      }
      stats.appendChild(el("div", { class: "conf-stands" }, cols));
    } else {
      stats.appendChild(el("div", { class: "muted", text: "No games yet this season." }));
    }
  }));
}

function favPlayerShell(p) {
  const nameRow = el("div", { class: "name-row" }, [
    el("a", { class: "link name", onclick: () => openPlayer(p.entity_id) }, p.name || "—"),
    p.position ? el("span", { class: "pos-tag", text: p.position }) : null,
  ]);
  const head = el("div", { class: "fav-card-head" }, [
    favStar("player", p.entity_id),
    playerHeadshot({ photo_path: p.photo_path, name: p.name }, "fav-card-photo"),
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
  if (!state.user.ai_enabled) {
    emptyState(root, "The AI assistant isn't enabled for your account.");
    return;
  }

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
    ["Play-by-play (one match)", [
      "How many lead changes in Nebraska's last match?",
      "Biggest scoring run in Pittsburgh's last game",
      "Who scored the most points in Texas's last match?",
      "Was Wisconsin's last match close or back-and-forth?",
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

  // Signups over time — sourced from our own DB (users.created_at), no external analytics needed.
  root.appendChild(renderSignupsCard());

  // Monitoring: operator's daily health guide + jump links (static; mirrors README §Observability).
  root.appendChild(renderMonitoringCard());

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
      el("th", { text: "AI" }),
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
        el("td", { class: "num" }, adminToggle(u, "ai_enabled", false)),
        el("td", { class: "l muted", text: (u.created_at || "").slice(0, 10) }),
        el("td", { class: "num" }, isSelf ? el("span", { class: "muted", text: "you" })
          : el("button", { class: "btn ghost danger", onclick: () => deleteUser(u) }, "Delete")),
      ]));
    });
    table.appendChild(tb);
    userBody.appendChild(table);
  } catch (e) { clear(userBody); emptyState(userBody, "Error: " + e.message); }
}

// Signups-over-time card. Lazily loads /admin/metrics/signups and draws headline totals + a
// daily-new bar chart. This is our own first-party data (users.created_at); anonymous visitor
// metrics (new vs returning, time on site) come from the privacy-first analytics tag instead.
function renderSignupsCard() {
  const card = el("div", { class: "card" });
  card.appendChild(el("div", { class: "card-title", text: "Signups" }));
  const body = el("div"); card.appendChild(body); spinner(body);
  (async () => {
    try {
      const d = await api("/admin/metrics/signups");
      clear(body);
      if (!d.days || !d.days.length) { emptyState(body, "No signups yet."); return; }
      body.appendChild(signupSummary(d));
      body.appendChild(signupChart(d.days));
    } catch (e) { clear(body); emptyState(body, "Error: " + e.message); }
  })();
  return card;
}

// Headline stat tiles: total accounts + rolling 7/30-day new signups (the series is one entry per
// day, so the last N entries are the last N days).
function signupSummary(d) {
  const sumLast = (n) => d.days.slice(-n).reduce((a, x) => a + x.new, 0);
  const tile = (val, label) => el("div", { class: "stat" }, [
    el("div", { class: "stat-val", text: String(val) }),
    el("div", { class: "stat-label", text: label }),
  ]);
  return el("div", { class: "stat-row" }, [
    tile(d.total, "Total users"),
    tile(sumLast(7), "Last 7 days"),
    tile(sumLast(30), "Last 30 days"),
  ]);
}

// A dependency-free SVG bar chart of new signups per day (hover a bar for the exact date/count).
// Built as markup and injected via html: — the el() helper uses createElement, which can't make
// namespaced SVG nodes.
function signupChart(days) {
  const W = 720, H = 170, padL = 26, padR = 6, padT = 10, padB = 26;
  const iw = W - padL - padR, ih = H - padT - padB;
  const max = Math.max(1, ...days.map((x) => x.new));
  const n = days.length;
  const bw = iw / n;
  const gap = bw > 6 ? 2 : 0.5;
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  let bars = "";
  days.forEach((x, i) => {
    const h = Math.round((x.new / max) * ih);
    const bx = padL + i * bw + gap / 2;
    const by = padT + ih - h;
    bars += `<rect class="sc-bar" x="${bx.toFixed(1)}" y="${by}" width="${Math.max(0.5, bw - gap).toFixed(1)}" height="${h}" rx="1">`
      + `<title>${esc(x.date)}: ${x.new} new (${x.cumulative} total)</title></rect>`;
  });
  const svg = `<svg viewBox="0 0 ${W} ${H}" class="signup-chart" role="img" aria-label="New signups per day">`
    + `<line class="sc-axis" x1="${padL}" y1="${padT + ih}" x2="${W - padR}" y2="${padT + ih}"/>`
    + `<text class="sc-tick" x="${padL - 4}" y="${padT + 8}" text-anchor="end">${max}</text>`
    + bars
    + `<text class="sc-tick" x="${padL}" y="${H - 6}">${esc(days[0].date)}</text>`
    + `<text class="sc-tick" x="${W - padR}" y="${H - 6}" text-anchor="end">${esc(days[n - 1].date)}</text>`
    + `</svg>`;
  return el("div", { class: "signup-chart-wrap", html: svg });
}

// Static monitoring/observability card for the Admin view. Links to Sentry + a short daily
// checklist; the authoritative version lives in the README (§Observability & monitoring).
function renderMonitoringCard() {
  const S = "https://jason-beatty.sentry.io";
  const link = (href, text) => el("a", { class: "btn ghost", href, target: "_blank", rel: "noopener", text });
  const card = el("div", { class: "card" });
  card.appendChild(el("div", { class: "card-title", text: "Monitoring" }));
  const body = el("div", { class: "admin-settings" }); card.appendChild(body);

  body.appendChild(el("div", { class: "muted", style: "margin-bottom:10px",
    text: "Health & observability (Sentry). Full guide: README → Observability & monitoring." }));

  body.appendChild(el("div", { style: "display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px" }, [
    link(S, "Open Sentry"),
    link(S + "/issues/", "Issues"),
    link(S + "/insights/crons/", "Crons"),
    link(S + "/insights/backend/", "Traces"),
    link(S + "/insights/uptime/", "Uptime"),
    link("/health", "/health"),
  ]));

  const daily = el("ol", { style: "margin:0; padding-left:18px; line-height:1.7" }, [
    el("li", { html: "<b>Uptime</b> — is <code>vballr.com/health</code> green? Red = whole site down." }),
    el("li", { html: "<b>Crons</b> — are <code>vb-daily-scrape</code>, <code>vb-hourly-scrape</code>, "
      + "<code>vb-weekly-rosters</code> green? A miss = stats stopped updating even if the site is up." }),
    el("li", { html: "<b>Issues</b> — any new unresolved errors (sort by Last seen)? Each shows the "
      + "release <code>vb-data@&lt;sha&gt;</code> that introduced it. Browser JS errors land here too." }),
  ]);
  body.appendChild(el("div", { class: "muted", style: "margin-bottom:4px", text: "30-second daily check:" }));
  body.appendChild(daily);
  return card;
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

/* ---------- magic-link sign-in route (#/signin?token=…) ---------- */
async function renderSignin(root) {
  root.appendChild(el("div", { class: "view-head" }, [el("h1", { text: "Signing in…" })]));
  const card = el("div", { class: "card" }); const body = el("div"); card.appendChild(body); root.appendChild(card);
  spinner(body);
  const token = state.signinToken;
  if (!token) { clear(body); emptyState(body, "Missing sign-in token."); return; }
  try {
    const auth = await req("POST", "/auth/link/consume", { token });
    state.signinToken = null;
    // Navigate off the one-time link (a refresh must not try to reuse it) before completing login.
    history.replaceState(null, "", "#top");
    state.tab = "top";
    await completeLogin(auth);
  } catch (e) {
    clear(body);
    emptyState(body, e.message || "This sign-in link is invalid or expired.");
    body.appendChild(el("button", { class: "btn", style: "margin-top:12px",
      onclick: () => openAuthModal("magic") }, "Email me a new link"));
  }
}

/* ---------- auth modal (login / register / passkey / magic link) ---------- */
function closeAuthModal() { const m = $("#auth-modal"); m.hidden = true; clear(m); }

function openAuthModal(mode) {
  const m = clear($("#auth-modal"));
  m.hidden = false;
  const panel = el("div", { class: "modal" });
  panel.addEventListener("click", (e) => e.stopPropagation());
  m.onclick = closeAuthModal;
  if (mode === "magic") renderMagicForm(panel);
  else renderAuthForm(panel, mode || "login");
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
        + "customizable scoring-weights editor. You can turn it on or off anytime in Settings." }));
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
    isReg ? null : el("button", { class: "link-btn forgot-link", onclick: () => renderMagicForm(panel) },
      "Forgot password?"),
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

// Forgot-password / passwordless: request a magic sign-in link by email.
function renderMagicForm(panel) {
  clear(panel);
  panel.appendChild(el("div", { class: "modal-head" }, [
    el("h2", { text: "Email me a sign-in link" }),
    el("button", { class: "icon-btn", onclick: closeAuthModal, title: "Close" }, "×"),
  ]));
  const email = el("input", { type: "email", placeholder: "you@example.com", autocomplete: "email" });
  const errBox = el("div", { class: "form-err", hidden: true });

  async function submit() {
    errBox.hidden = true;
    const e = email.value.trim();
    if (!e) { errBox.textContent = "Enter your email."; errBox.hidden = false; return; }
    try {
      await req("POST", "/auth/link/send", { email: e });
      clear(panel);
      panel.appendChild(el("div", { class: "modal-head" }, [
        el("h2", { text: "Check your email" }),
        el("button", { class: "icon-btn", onclick: closeAuthModal, title: "Close" }, "×"),
      ]));
      panel.appendChild(el("p", { class: "muted", style: "margin:0 0 8px",
        text: "If that email has an account, a sign-in link is on its way. It expires in 24 hours "
            + "and can be used once." }));
    } catch (err) { errBox.textContent = err.message; errBox.hidden = false; }
  }
  email.addEventListener("keydown", (ev) => { if (ev.key === "Enter") submit(); });

  panel.appendChild(el("div", { class: "auth-form" }, [
    el("p", { class: "muted", style: "margin:0 0 4px",
      text: "We'll email you a link that signs you in — no password needed. Once in, you can set a "
          + "new password in Account." }),
    field2("Email", email),
    errBox,
    el("button", { class: "btn primary wide", onclick: submit }, "Email me a sign-in link"),
  ]));
  panel.appendChild(el("div", { class: "modal-foot" }, [
    el("button", { class: "link-btn", onclick: () => renderAuthForm(panel, "login") }, "Back to sign in"),
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

// Dedicated (wider) Settings modal. Houses the fantasy opt-in + scoring-weights editor today, with
// room to grow; Account keeps profile/password/passkeys.
function openSettingsModal() {
  const d = $(".user-dropdown"); if (d) d.hidden = true;
  const m = clear($("#auth-modal"));
  m.hidden = false;
  const panel = el("div", { class: "modal modal-lg", id: "settings-modal-open" });
  panel.addEventListener("click", (e) => e.stopPropagation());
  m.onclick = closeAuthModal;
  m.appendChild(panel);
  renderSettings(panel);
}

function renderSettings(panel) {
  clear(panel);
  panel.appendChild(el("div", { class: "modal-head" }, [
    el("h2", { text: "Settings" }),
    el("button", { class: "icon-btn", onclick: closeAuthModal, title: "Close" }, "×"),
  ]));

  // Fantasy features (opt-in): the on/off toggle plus, when on, the scoring-weights editor.
  const fanWrap = el("div", { class: "auth-form" });
  fanWrap.appendChild(el("h3", { text: "Fantasy" }));
  const fanToggle = el("input", { type: "checkbox" });
  fanToggle.checked = fantasyEnabled();
  fanToggle.addEventListener("change", async () => {
    await setFantasy(fanToggle.checked);
    renderSettings(panel);  // reveal/hide the weights editor to match
  });
  fanWrap.appendChild(el("label", { class: "toggle-row" }, [
    fanToggle,
    el("span", { text: "Enable fantasy features (Fantasy tab, FP columns, scoring weights)" }),
  ]));
  if (fantasyEnabled()) fanWrap.appendChild(weightsPanel(() => render()));
  panel.appendChild(fanWrap);
}

async function renderAccount(panel) {
  clear(panel);
  panel.appendChild(el("div", { class: "modal-head" }, [
    el("h2", { text: "Account" }),
    el("button", { class: "icon-btn", onclick: closeAuthModal, title: "Close" }, "×"),
  ]));
  panel.appendChild(el("div", { class: "muted", text: state.user.email }));

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

  // Passkeys. One passkey per account: the "Add" button only shows when none exists; otherwise the
  // user must remove the current one first (the API enforces this too).
  const pkWrap = el("div", { class: "auth-form" });
  pkWrap.appendChild(el("h3", { text: "Passkeys" }));
  const supported = !!(window.SimpleWebAuthn && window.SimpleWebAuthn.browserSupportsWebAuthn
    && window.SimpleWebAuthn.browserSupportsWebAuthn());
  const pkAction = el("div", { class: "pk-action" }); pkWrap.appendChild(pkAction);
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
    clear(pkAction);
    if (!supported) {
      pkAction.appendChild(el("div", { class: "muted", text: "This browser doesn't support passkeys." }));
    } else if (!creds.length) {
      pkAction.appendChild(el("button", { class: "btn ghost", onclick: passkeyRegister }, "🔑 Add a passkey"));
    } else {
      pkAction.appendChild(el("div", { class: "muted", text: "Remove your existing passkey to add a new one." }));
    }
  } catch (e) { clear(pkList); pkList.appendChild(el("div", { class: "muted", text: "Couldn't load passkeys." })); }
}

/* ---------- go ---------- */
boot();
