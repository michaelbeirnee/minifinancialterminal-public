// MFT — frontend controller.
// Friendly design system (IBM Plex, plain-English copy) carrying the full
// feature set: today's brief, tiles, detail + comparison charts, sectors,
// assets, news, the data explorer, insights, the backtest wizard, saved
// actions, history, system — plus ticker autocomplete, calendar date inputs
// and the chart crosshair/measure tool.
const API = "";
let token = localStorage.getItem("mft_token") || null;
let authMode = "login";
let workspaceOwner = "local";
const charts = {};

// ---------- helpers ----------
const $ = (id) => document.getElementById(id);
function setStatus(msg) { const s = $("status"); if (s) s.textContent = msg; }

async function api(path, { method = "GET", body, form } = {}) {
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  let payload;
  if (form) {
    payload = new URLSearchParams(form).toString();
    headers["Content-Type"] = "application/x-www-form-urlencoded";
  } else if (body) {
    payload = JSON.stringify(body);
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(API + path, { method, headers, body: payload });
  if (res.status === 401) { logout(); throw new Error("Session expired"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

// ---------- live quotes ----------
// One Server-Sent-Events connection per provider carries the union of every
// symbol some view is watching; the views themselves only ever register a
// callback. Watchers carry a `scope` (the view they belong to) so switching
// views parks their symbols instead of streaming prices nobody can see. The
// tape is scope "global" and streams everywhere.
const Live = (() => {
  const watchers = new Map();          // id -> {symbols, provider, scope, onTick}
  const conns = new Map();             // provider -> {abort, symbols, state}
  const status = { available: null, providers: {} };
  let nextId = 1, activeView = "market", rebuildTimer = null;
  const DEFAULT = "default";

  function pill(state, provider, detail) {
    const el = $("live-pill");
    if (!el) return;
    el.dataset.state = state;
    const label = provider && provider !== DEFAULT ? provider.toUpperCase() : (status.available?.default_provider || "yahoo").toUpperCase();
    el.textContent = state === "live" ? `● LIVE · ${label}` : state === "connecting" ? "● CONNECTING" : state === "error" ? "● RECONNECTING" : "● IDLE";
    el.title = detail || (state === "live" ? "Streaming prices" : state === "idle" ? "No live view open" : "");
  }

  async function ensureStatus() {
    if (status.available) return status.available;
    try { status.available = await api("/api/stream/status"); } catch { status.available = { default_provider: "yahoo", providers: {} }; }
    return status.available;
  }

  function wanted() {
    const byProv = new Map();
    watchers.forEach((w) => {
      if (w.scope !== "global" && w.scope !== activeView) return;
      const p = w.provider || DEFAULT;
      if (!byProv.has(p)) byProv.set(p, new Set());
      w.symbols.forEach((s) => byProv.get(p).add(s));
    });
    return byProv;
  }

  function dispatch(provider, ticks) {
    watchers.forEach((w) => {
      if ((w.provider || DEFAULT) !== provider) return;
      if (w.scope !== "global" && w.scope !== activeView) return;
      const mine = ticks.filter((t) => w.symbols.has(t.symbol));
      if (mine.length) { try { w.onTick(mine); } catch (e) { console.warn("live watcher", e); } }
    });
  }

  function refreshPill() {
    const live = [...conns.values()].filter((c) => c.state === "live");
    if (live.length) return pill("live", live[0].provider, `${live.map((c) => c.symbols.size).reduce((a, b) => a + b, 0)} symbols streaming`);
    const bad = [...conns.values()].find((c) => c.state === "error");
    if (bad) return pill("error", bad.provider, bad.detail);
    if (conns.size) return pill("connecting");
    pill("idle");
  }

  async function open(provider, symbols) {
    const conn = { provider, symbols, abort: new AbortController(), state: "connecting", detail: "", backoff: 1000 };
    conns.set(provider, conn);
    refreshPill();
    const url = `/api/stream/quotes?symbols=${encodeURIComponent([...symbols].join(","))}` +
      (provider !== DEFAULT ? `&provider=${encodeURIComponent(provider)}` : "");
    for (;;) {
      if (conn.abort.signal.aborted) return;
      try {
        const res = await fetch(API + url, { headers: { Authorization: `Bearer ${token}` }, signal: conn.abort.signal });
        if (res.status === 401) { logout(); return; }
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          conn.state = "error"; conn.detail = body.detail || `HTTP ${res.status}`; refreshPill();
          if (res.status < 500) return;               // a bad request will not fix itself
          throw new Error(conn.detail);
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const frames = buffer.split("\n\n");
          buffer = frames.pop();
          for (const frame of frames) {
            const line = frame.split("\n").find((l) => l.startsWith("data: "));
            if (!line) continue;
            let ev; try { ev = JSON.parse(line.slice(6)); } catch { continue; }
            if (ev.type === "hello") { conn.provider = ev.provider; conn.backoff = 1000; }
            else if (ev.type === "status") {
              conn.state = ev.connected ? "live" : "error"; conn.detail = ev.last_error || ""; refreshPill();
            } else if (ev.type === "ticks") {
              if (conn.state !== "live") { conn.state = "live"; refreshPill(); }
              dispatch(provider, ev.ticks);
            }
          }
        }
      } catch (e) {
        if (conn.abort.signal.aborted) return;
        conn.state = "error"; conn.detail = e.message; refreshPill();
      }
      if (conn.abort.signal.aborted) return;
      await new Promise((r) => setTimeout(r, conn.backoff));
      conn.backoff = Math.min(conn.backoff * 2, 30000);
    }
  }

  function rebuild() {
    rebuildTimer = null;
    const want = wanted();
    conns.forEach((conn, provider) => {
      const w = want.get(provider);
      const same = w && w.size === conn.symbols.size && [...w].every((s) => conn.symbols.has(s));
      if (!same) { conn.abort.abort(); conns.delete(provider); }
    });
    want.forEach((symbols, provider) => {
      if (!conns.has(provider) && symbols.size) open(provider, symbols);
    });
    refreshPill();
  }
  function schedule() { clearTimeout(rebuildTimer); rebuildTimer = setTimeout(rebuild, 250); }

  return {
    /** Stream `symbols`; `onTick(ticks)` gets the newest tick per symbol. Returns a handle with close(). */
    watch(symbols, onTick, { scope = "global", provider = null } = {}) {
      const id = nextId++;
      const set = new Set((Array.isArray(symbols) ? symbols : String(symbols).split(",")).map((s) => String(s).trim().toUpperCase()).filter(Boolean));
      watchers.set(id, { symbols: set, provider, scope, onTick });
      schedule();
      return { id, close() { if (watchers.delete(id)) schedule(); } };
    },
    setView(view) { activeView = view; schedule(); },
    ensureStatus,
    providers() { return status.available?.providers || {}; },
    defaultProvider() { return status.available?.default_provider || "yahoo"; },
    closeAll() { watchers.clear(); schedule(); },
  };
})();

// Flash a cell green or red as its number moves. `prev` is compared to `next`
// so an unchanged print stays quiet.
function livePaint(el, next, prev, text) {
  if (!el) return;
  if (text != null) el.textContent = text;
  if (prev == null || next == null || next === prev) return;
  el.classList.remove("flash-up", "flash-down");
  void el.offsetWidth; // restart the animation
  el.classList.add(next > prev ? "flash-up" : "flash-down");
}
const fmtLive = (x) => (x == null ? "-" : x >= 1000 ? x.toLocaleString(undefined, { maximumFractionDigits: 2 }) : x >= 10 ? x.toFixed(2) : x.toFixed(4).replace(/0+$/, "").replace(/\.$/, ".0"));

const fmtPct = (x, sign) => { const s = (x * 100).toFixed(1) + "%"; return sign && x >= 0 ? "+" + s : s; };
// Portfolio weights need a second decimal — the tail of a basket lives below 0.1%.
const pctWeight = (x, dp = 2) => (x == null ? "-" : (x * 100).toFixed(dp) + "%");
const fmt$ = (x) => "$" + Math.round(x).toLocaleString("en-US");
const cls = (x) => (x >= 0 ? "pos" : "neg");
const NAMES = { AAPL: "Apple", MSFT: "Microsoft", NVDA: "NVIDIA", AMZN: "Amazon", TSLA: "Tesla",
  GOOGL: "Alphabet", META: "Meta", SPY: "S&P 500 ETF" };
const name = (s) => NAMES[s] || s;
function isoAgo(days) { const d = new Date(); d.setDate(d.getDate() - days); return d.toISOString().slice(0, 10); }
const RANGE_DAYS = { "1M": 31, "6M": 183, "1Y": 366, "3Y": 1096 };
const RANGE_NAME = { "1M": "the last month", "6M": "the last 6 months", "1Y": "the last year", "3Y": "the last 3 years" };

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function timeAgo(value) {
  if (!value) return "";
  const d = new Date(value);
  if (isNaN(d)) return "";
  const s = (Date.now() - d.getTime()) / 1000;
  if (s < 90) return "just now";
  if (s < 5400) return Math.round(s / 60) + "m ago";
  if (s < 172800) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}
function simpleTable(rows, maxRows = 25) {
  if (!rows || !rows.length) return `<div class="empty">no rows</div>`;
  const cols = [...new Set(rows.flatMap((r) => Object.keys(r)))];
  const cell = (c, v) => {
    if (v == null) return "-";
    if (c.includes("weight")) {
      const f = parseFloat(v);
      if (!isNaN(f)) return (f * 100).toFixed(2) + "%";
    }
    return escapeHtml(String(v)).slice(0, 60);
  };
  return `<table><tr>${cols.map((c) => `<th>${c.replace(/_/g, " ")}</th>`).join("")}</tr>` +
    rows.slice(0, maxRows).map((r) =>
      `<tr>${cols.map((c) => `<td>${cell(c, r[c])}</td>`).join("")}</tr>`).join("") + "</table>";
}

// ---------- auth ----------
$("tab-login").onclick = () => switchAuth("login");
$("tab-register").onclick = () => switchAuth("register");
function switchAuth(mode) {
  authMode = mode;
  $("tab-login").classList.toggle("active", mode === "login");
  $("tab-register").classList.toggle("active", mode === "register");
  $("au-email").style.display = mode === "register" ? "block" : "none";
  $("au-submit").textContent = mode === "register" ? "Create account" : "Sign in";
}
$("au-submit").onclick = async () => {
  const username = $("au-username").value.trim();
  const password = $("au-password").value;
  const msg = $("au-msg");
  msg.className = "msg"; msg.textContent = "";
  try {
    if (authMode === "register") {
      await api("/api/auth/register", { method: "POST", body: { username, email: $("au-email").value.trim(), password } });
      msg.className = "msg ok"; msg.textContent = "Account created. Signing in…";
    }
    const tok = await api("/api/auth/login", { method: "POST", form: { username, password } });
    token = tok.access_token;
    localStorage.setItem("mft_token", token);
    await enterTerminal();
  } catch (e) { msg.textContent = e.message; }
};
function logout() {
  Live.closeAll();
  token = null;
  registry = null;
  localStorage.removeItem("mft_token");
  $("terminal").style.display = "none";
  $("auth").style.display = "flex";
}
// Revoke the session server-side so the token really stops working.
$("logout").onclick = async () => {
  try { await api("/api/auth/logout", { method: "POST" }); } catch { /* log out anyway */ }
  logout();
};

async function enterTerminal() {
  $("auth").style.display = "none";
  $("terminal").style.display = "flex";
  const me = await api("/api/auth/me");
  workspaceOwner = me.username || "local";
  workspaceItems = null;
  $("who").textContent = `${me.username} · research`;
  Live.ensureStatus();
  loadStockMode();
  await Promise.all([loadStrategies(), loadWatchCards(), loadMarketAll()]);
}

// ---------- nav ----------
document.querySelectorAll(".navbtn").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll(".navbtn").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".view").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $("view-" + b.dataset.view).classList.add("active");
    document.querySelector(".container").classList.toggle("workspace-open", b.dataset.view === "workspace");
    document.querySelector(".container").classList.remove("stock-wide");
    Live.setView(b.dataset.view);
    if (b.dataset.view === "workspace") loadWorkspace();
    if (b.dataset.view === "history") loadHistory();
    if (b.dataset.view === "system") loadSystem();
    if (b.dataset.view === "data") loadRegistry();
    if (b.dataset.view === "saved") loadSavedView();
    if (b.dataset.view === "sectors") loadSectors();
    if (b.dataset.view === "screener") loadScreener();
    if (b.dataset.view === "thesis") loadThesisView();
    if (b.dataset.view === "thesis-sectors") loadSectorThesisView();
    if (b.dataset.view === "flagged") loadFlaggedView();
    if (b.dataset.view === "modeling") loadModelingView();
    if (b.dataset.view === "assets") loadAssets();
    if (b.dataset.view === "news") loadNewsInit();
    if (b.dataset.view === "calendar") loadCalendarInit();
    if (b.dataset.view === "sentiment") loadSentimentInit();
    if (b.dataset.view === "volatility") loadVolatility();
    if (b.dataset.view === "portfolio") loadPortfolioView();
    if (b.dataset.view === "assistant") loadAssistantView();
    if (b.dataset.view === "research") loadResearchWorkbench();
  };
});

// ---------- sidebar: breadcrumbs, the jump filter, a keyboard shortcut ----------
// The sidebar's grouping is the one source of truth: each view's breadcrumb is
// read off the group its nav item sits in, so moving an item moves its crumb.
(function initSidebar() {
  const sidebar = $("sidebar");
  const jump = $("nav-jump");
  if (!sidebar || !jump) return;

  const groups = [...sidebar.querySelectorAll(".nav-group")];
  const crumbFor = new Map();
  groups.forEach((g) => g.querySelectorAll(".navbtn").forEach((b) => crumbFor.set(b.dataset.view, g.dataset.group)));
  document.querySelectorAll(".navbtn[data-view]").forEach((b) => {
    if (!crumbFor.has(b.dataset.view)) crumbFor.set(b.dataset.view, "TERMINAL");
  });
  crumbFor.forEach((label, view) => {
    const section = $("view-" + view);
    const h1 = section && section.querySelector("h1");
    if (!h1) return;
    // The workspace already carries its own eyebrow above the title.
    const prev = h1.previousElementSibling;
    if (prev && prev.classList.contains("workspace-eyebrow")) return;
    const crumb = document.createElement("div");
    crumb.className = "crumb";
    crumb.textContent = label;
    h1.before(crumb);
  });

  // Filter: case-insensitive substring on the label; a group with no match
  // disappears with its items. Enter opens the first match, Escape clears.
  let empty = null;
  function applyFilter() {
    const q = jump.value.trim().toLowerCase();
    let shown = 0;
    groups.forEach((g) => {
      let any = false;
      g.querySelectorAll(".navbtn").forEach((b) => {
        const hit = !q || b.textContent.toLowerCase().includes(q);
        b.classList.toggle("nav-hidden", !hit);
        if (hit) any = true;
      });
      g.classList.toggle("nav-hidden", !any);
      if (any) shown++;
    });
    if (!shown && !empty) {
      empty = document.createElement("div");
      empty.className = "nav-empty";
      empty.textContent = "No view matches.";
      sidebar.insertBefore(empty, sidebar.querySelector(".nav-foot"));
    } else if (shown && empty) { empty.remove(); empty = null; }
  }
  function clearFilter() { jump.value = ""; applyFilter(); }
  jump.addEventListener("input", applyFilter);
  jump.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { clearFilter(); jump.blur(); }
    if (e.key === "Enter") {
      const first = sidebar.querySelector(".nav-group:not(.nav-hidden) .navbtn:not(.nav-hidden)");
      if (first) { first.click(); clearFilter(); jump.blur(); }
    }
  });
  // "/" or Ctrl/Cmd-K focuses the jump box from anywhere that is not a field.
  document.addEventListener("keydown", (e) => {
    const t = e.target;
    const typing = t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
    const cmdK = e.key.toLowerCase() === "k" && (e.metaKey || e.ctrlKey);
    if (cmdK || (e.key === "/" && !typing && !e.metaKey && !e.ctrlKey && !e.altKey)) {
      e.preventDefault();
      jump.focus();
      jump.select();
    }
  });
})();

// ---------- research workbench ----------
// The workbench is a shell, not a one-off report. Its context packet keeps the
// two evidence lanes independent and gives later modules a stable object to
// build on. The human-written bridge is intentionally never inferred here.
let rwLoaded = false;
let rwContext = null;

function loadResearchWorkbench() {
  if (rwLoaded) return;
  rwLoaded = true;
  $("rw-symbol").focus();
}

function rwStateClass(state) {
  return state === "constructive" ? "pos" : state === "challenged" ? "neg" :
    state === "mixed" ? "warn" : "dim";
}

function rwAlignmentClass(key) {
  if (key === "aligned_constructive") return "pos";
  if (key === "aligned_challenged") return "neg";
  if (key === "incomplete") return "dim";
  return "warn";
}

function rwLabel(value) {
  return String(value || "unknown").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function rwMetricValue(key, value) {
  if (value == null || value === "" || !Number.isFinite(Number(value))) return "—";
  const number = Number(value);
  if (/growth|margin|yield|return_on/.test(key)) return fmtPct(number);
  if (/cash_flow|market_cap|enterprise_value/.test(key)) return fmt$(number);
  if (/pe$|peg_ratio|price_to_|ev_to_|current_ratio|quick_ratio|debt_to_equity|beta/.test(key))
    return number.toLocaleString("en-US", { maximumFractionDigits: 2 }) + "×";
  return number.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

function rwReasons(rows) {
  return rows && rows.length
    ? rows.map((r) => `<div class="rw-reason">${escapeHtml(r)}</div>`).join("")
    : `<div class="empty">Not enough observations for a mechanical read.</div>`;
}

function rwRender(packet) {
  rwContext = packet;
  $("rw-empty").style.display = "none";
  $("rw-result").style.display = "";

  const subject = packet.subject || {};
  const settings = packet.settings || {};
  const assessment = packet.assessment || {};
  const top = packet.top_down || {};
  const bottom = packet.bottom_up || {};
  const alignment = assessment.alignment || {};
  const coverage = assessment.coverage || {};
  $("rw-title").textContent = `${subject.symbol || "—"} · ${subject.name || subject.symbol || "Unknown security"}`;
  $("rw-meta").textContent = [subject.sector, subject.industry,
    `${subject.benchmark || settings.benchmark || "SPY"} benchmark`,
    rwLabel(settings.horizon)].filter(Boolean).join(" · ");
  $("rw-coverage").textContent = `${coverage.successful || 0}/${coverage.total || 0} sources · ${
    Math.round((coverage.ratio || 0) * 100)}% coverage`;
  $("rw-warnings").innerHTML = (packet.warnings || []).length
    ? `<div class="warnbox">Partial packet: ${packet.warnings.map(escapeHtml).join(" · ")}</div>` : "";

  const states = [
    ["Top down", top.state, "Market and sector context"],
    ["Bottom up", bottom.state, "Company snapshot"],
  ];
  $("rw-assessment").innerHTML = states.map(([label, state, note]) => `
    <div class="rw-state"><div class="rw-state-k">${label}</div>
      <div class="rw-state-v ${rwStateClass(state)}">${rwLabel(state)}</div><div class="dim">${note}</div></div>`).join("") + `
    <div class="rw-state rw-state-wide"><div class="rw-state-k">Joined read</div>
      <div class="rw-state-v ${rwAlignmentClass(alignment.key)}">${escapeHtml(alignment.label || "Evidence incomplete")}</div>
      <div class="dim">${escapeHtml(alignment.reading || "")}</div></div>`;

  $("rw-top-reasons").innerHTML = rwReasons(top.reasons);
  $("rw-bottom-reasons").innerHTML = rwReasons(bottom.reasons);
  $("rw-regime").innerHTML = (top.signals || []).length
    ? top.signals.map((s) => `<div class="sig-card t-${escapeHtml(s.tone || "")}">
        <div class="sig-label">${escapeHtml(s.label || "Signal")}</div>
        <div class="sig-value">${escapeHtml(s.value == null ? "—" : s.value)}</div>
        <div class="sig-read">${escapeHtml(s.read || s.reading || "")}</div></div>`).join("")
    : `<div class="empty">Market signal detail unavailable.</div>`;

  const horizon = settings.horizon || "three_month";
  const benchmark = subject.benchmark || settings.benchmark || "SPY";
  const sectorRelative = top.sector_relative ?? (top.sector || {})[`relative_${horizon}`];
  const relativeRows = [
    [`${subject.symbol || "Security"} return`, top.subject_return],
    [`vs ${benchmark}`, top.relative_return],
    [`${subject.sector_etf || "Sector"} vs ${benchmark}`, sectorRelative],
  ];
  $("rw-relative").innerHTML = relativeRows.map(([label, value]) => `
    <div class="metric"><div class="k">${escapeHtml(label)}</div>
      <div class="v ${value == null ? "dim" : cls(value)}">${value == null ? "—" : fmtPct(value, true)}</div>
      <div class="note">${escapeHtml(rwLabel(horizon))}</div></div>`).join("");

  const metricLabels = {
    revenue_growth: "Revenue growth", earnings_growth: "Earnings growth",
    operating_margin: "Operating margin", profit_margin: "Profit margin",
    free_cash_flow: "Free cash flow", forward_pe: "Forward P/E",
    price_to_sales: "Price / sales", debt_to_equity: "Debt / equity",
  };
  const metrics = bottom.metrics || {};
  $("rw-metrics").innerHTML = Object.entries(metricLabels).map(([key, label]) => `
    <div class="metric"><div class="k">${label}</div><div class="v">${rwMetricValue(key, metrics[key])}</div></div>`).join("");

  const consensus = bottom.consensus || {};
  const target = consensus.target_mean;
  const current = consensus.current_price;
  const upside = Number.isFinite(target) && Number.isFinite(current) && current !== 0 ? target / current - 1 : null;
  $("rw-consensus").innerHTML = `<div class="rw-consensus">
    <span><small>Street view</small><b>${escapeHtml(rwLabel(consensus.recommendation || "unavailable"))}</b></span>
    <span><small>Analysts</small><b class="mono">${consensus.analyst_count ?? "—"}</b></span>
    <span><small>Mean target</small><b class="mono">${target == null ? "—" : fmt$(target)}</b></span>
    <span><small>Implied change</small><b class="mono ${upside == null ? "dim" : cls(upside)}">${upside == null ? "—" : fmtPct(upside, true)}</b></span>
  </div>`;

  const framework = bottom.framework || {};
  const frameworkGroup = (title, rows) => `<div><div class="subhead">${title}</div><ul>${(rows || [])
    .map((row) => `<li>${escapeHtml(row)}</li>`).join("") || "<li>Deeper work required.</li>"}</ul></div>`;
  $("rw-framework").innerHTML = frameworkGroup("Operating focus", framework.focus) +
    frameworkGroup("Valuation lens", framework.valuation) + frameworkGroup("Common traps", framework.traps);

  const prompts = (packet.exposure_bridge || {}).driver_prompts || [];
  $("rw-driver-prompts").innerHTML = `<span class="dim">Prompts for this sector:</span>` + prompts.map((p) =>
    `<button type="button" class="chip" data-rw-driver="${escapeHtml(p)}">${escapeHtml(p)}</button>`).join("");
  $("rw-driver-prompts").querySelectorAll("[data-rw-driver]").forEach((button) => {
    button.onclick = () => { $("rw-driver").value = button.dataset.rwDriver; $("rw-driver").focus(); };
  });

  const sources = packet.sources || [];
  $("rw-sources").innerHTML = sources.length ? `<table><thead><tr><th>Lane</th><th>Input</th><th>Command</th><th>Parameters</th><th>Provider</th><th>Status</th></tr></thead><tbody>` +
    sources.map((source) => `<tr><td>${escapeHtml(rwLabel(source.lane))}</td>
      <td>${escapeHtml(rwLabel(source.key))}</td><td class="mono dim">${escapeHtml(source.path || "")}</td>
      <td class="mono dim">${escapeHtml(JSON.stringify(source.params || {}))}</td>
      <td class="mono">${escapeHtml(source.provider || "—")}</td>
      <td class="${source.status === "ok" ? "pos" : "neg"}">${escapeHtml(source.status || "unknown")}</td></tr>`).join("") +
    `</tbody></table>` : `<div class="empty">No source manifest returned.</div>`;
}

async function rwRun() {
  const symbol = $("rw-symbol").value.trim().toUpperCase();
  const benchmark = $("rw-benchmark").value.trim().toUpperCase();
  const status = $("rw-status");
  if (!symbol || !benchmark) { status.textContent = "Enter both a security and a benchmark."; return; }
  const button = $("rw-run");
  button.disabled = true;
  status.textContent = "Building both evidence lanes…";
  try {
    const qs = new URLSearchParams({ symbol, benchmark, horizon: $("rw-horizon").value });
    const response = await api(`/api/v1/research/context?${qs}`);
    rwRender(response.results || {});
    status.textContent = "Context packet ready. The exposure bridge remains yours to prove.";
    setStatus(`RESEARCH CONTEXT READY · ${symbol}`);
  } catch (e) {
    status.textContent = e.message;
    setStatus("ERR: " + e.message);
  } finally {
    button.disabled = false;
  }
}

function rwNotes(packet) {
  const bridge = [
    ["Driver", $("rw-driver").value.trim()],
    ["Disclosed exposure", $("rw-exposure").value.trim()],
    ["Financial transmission", $("rw-financial").value.trim()],
    ["Expectations gap", $("rw-expectations").value.trim()],
  ];
  const assessment = packet.assessment || {};
  return [
    `Research Workbench · top down: ${assessment.top_down_state || "unknown"} · bottom up: ${assessment.bottom_up_state || "unknown"} · joined: ${(assessment.alignment || {}).label || "incomplete"}`,
    ...bridge.map(([label, value]) => `${label}: ${value || "Not yet proven"}`),
  ].join("\n");
}

async function rwCreateThesis() {
  const message = $("rw-msg");
  const claim = $("rw-claim").value.trim();
  if (!rwContext) { message.textContent = "Build a context packet first."; return; }
  if (!claim) { message.textContent = "Write a falsifiable claim first."; return; }
  const symbol = rwContext.subject.symbol;
  const days = Math.max(1, Math.min(730, parseInt($("rw-days").value, 10) || 90));
  const reviewBy = new Date();
  reviewBy.setDate(reviewBy.getDate() + days);
  const button = $("rw-create");
  button.disabled = true;
  message.className = "msg";
  message.textContent = "Creating thesis and freezing the packet…";
  try {
    const thesis = await api("/api/theses", { method: "POST", body: {
      title: `${symbol} — ${claim}`.slice(0, 200), claim, symbols: symbol,
      direction: $("rw-direction").value, source: "research_workbench",
      review_by: reviewBy.toISOString(), notes: rwNotes(rwContext),
    }});
    let evidenceFrozen = true;
    try {
      await api(`/api/theses/${thesis.id}/evidence`, { method: "POST", body: {
        command_path: "/research/context",
        parameters: {
          symbol, benchmark: rwContext.settings.benchmark,
          horizon: rwContext.settings.horizon,
        },
        leg: "top_down_bottom_up_context",
        note: "Point-in-time Research Workbench packet.",
      }});
    } catch {
      evidenceFrozen = false;
    }
    message.className = evidenceFrozen ? "msg ok" : "msg warn";
    message.innerHTML = `${evidenceFrozen ? "Tracked thesis created and packet frozen." : "Tracked thesis created; the evidence snapshot could not be refreshed."} ` +
      `<button type="button" class="linkbtn" data-rw-thesis="${thesis.id}">Review thesis →</button>`;
    message.querySelector("[data-rw-thesis]").onclick = () => rwOpenThesis(thesis.id);
    thLoaded = false;
  } catch (e) {
    message.textContent = e.message;
  } finally {
    button.disabled = false;
  }
}

async function rwOpenThesis(id) {
  const nav = document.querySelector('.navbtn[data-view="thesis"]');
  if (nav) nav.click();
  await thLoadTheses();
  await thShowThesis(id);
  $("th-detail").scrollIntoView({ behavior: "smooth" });
}

$("rw-run").onclick = rwRun;
$("rw-create").onclick = rwCreateThesis;
["rw-symbol", "rw-benchmark"].forEach((id) => {
  $(id).onkeydown = (event) => { if (event.key === "Enter") rwRun(); };
  $(id).oninput = () => { $(id).value = $(id).value.toUpperCase(); };
});
document.querySelectorAll("[data-rw-view]").forEach((button) => {
  button.onclick = () => {
    const nav = document.querySelector(`.navbtn[data-view="${button.dataset.rwView}"]`);
    if (nav) nav.click();
  };
});
window.openResearchWorkbench = (symbol) => {
  const nav = document.querySelector('.navbtn[data-view="research"]');
  if (nav) nav.click();
  if (symbol) $("rw-symbol").value = String(symbol).toUpperCase();
  rwRun();
};

// ---------- watchlist: tape + quote cards ----------
// The tape and the Markets card grid are driven by the user's default
// server-side watchlist (the same one the Saved tab manages). Clicking a
// card expands into the full stock page.
const TAPE_SYMS = "AAPL,MSFT,NVDA,AMZN,TSLA,SPY,GOOGL,META"; // seed for brand-new accounts
let marketWatchlist = null;

async function ensureMarketWatchlist(refresh) {
  if (marketWatchlist && !refresh) return marketWatchlist;
  const lists = await api("/api/user/watchlists");
  marketWatchlist = lists.find((w) => w.is_default) || lists[0] || null;
  if (!marketWatchlist) {
    marketWatchlist = await api("/api/user/watchlists", {
      method: "POST",
      body: { name: "My Watchlist", symbols: TAPE_SYMS.split(","), is_default: true },
    });
  }
  return marketWatchlist;
}

let tapeLive = null;
async function loadWatchCards() {
  try {
    const w = await ensureMarketWatchlist(true);
    $("wm-note").textContent = `${w.name} · ${w.items.length} symbols`;
    if (!w.items.length) {
      $("tape").innerHTML = "";
      $("mk-grid").innerHTML = `<div class="empty">Your watchlist is empty — add a ticker above.</div>`;
      $("mk-summary").textContent = "Add some tickers to your watchlist to see them here.";
      return;
    }
    const d = await api(`/api/user/watchlists/${w.id}/quotes`);
    const quotes = d.results.filter((q) => q.last_price != null);

    $("tape").innerHTML = quotes.map((q) => {
      const chg = (q.change_percent ?? 0) * 100;
      const dir = chg >= 0 ? "up" : "down";
      return `<span class="tick" data-sym="${escapeHtml(q.symbol)}"><span class="sym">${q.symbol}</span> <span class="px">${q.last_price.toFixed(2)}</span>
        <span class="chg ${dir}">${chg >= 0 ? "▲" : "▼"}${Math.abs(chg).toFixed(2)}%</span></span>`;
    }).join("");

    const ups = quotes.filter((q) => (q.change_percent ?? 0) >= 0).length;
    $("mk-summary").textContent = `${ups} of ${quotes.length} stocks on your watchlist are up today. ` +
      (ups >= quotes.length * 0.6 ? "A calm, mostly green day." :
       ups >= quotes.length * 0.4 ? "A mixed day across the board." : "A rough day — most names are lower.");

    $("mk-grid").innerHTML = quotes.map((q) => {
      const chg = (q.change_percent ?? 0) * 100;
      const dir = chg >= 0 ? "up" : "down";
      return `<div class="qcard" data-sym="${q.symbol}">
        <button class="q-x" data-rm="${q.symbol}" title="Remove from watchlist">&times;</button>
        <div class="head"><span class="s">${q.symbol}</span>
          <span class="n">${escapeHtml((q.name || name(q.symbol) || "").slice(0, 22))}</span></div>
        <div class="p">${q.last_price.toFixed(2)}</div>
        <div class="c ${dir}">${chg >= 0 ? "▲ +" : "▼ "}${Math.abs(chg).toFixed(2)}% today</div>
      </div>`;
    }).join("");

    document.querySelectorAll("#mk-grid .qcard").forEach((c) => {
      c.onclick = () => openStock(c.dataset.sym);
    });
    // The tape rides along everywhere; the cards only while Markets is open.
    if (tapeLive) tapeLive.close();
    const tapeSpans = {}, cardEls = {}, tapeLast = {};
    quotes.forEach((q) => { tapeLast[q.symbol] = q.last_price; });
    document.querySelectorAll("#tape .tick").forEach((el) => { tapeSpans[el.dataset.sym] = el; });
    document.querySelectorAll("#mk-grid .qcard").forEach((el) => { cardEls[el.dataset.sym] = el; });
    const paintTape = (t) => {
      const el = tapeSpans[t.symbol];
      if (!el || t.price == null) return;
      livePaint(el.querySelector(".px"), t.price, tapeLast[t.symbol], fmtLive(t.price));
      const c = el.querySelector(".chg");
      if (c && t.change_percent != null) {
        const pct = t.change_percent * 100;
        c.className = "chg " + (pct >= 0 ? "up" : "down");
        c.textContent = `${pct >= 0 ? "▲" : "▼"}${Math.abs(pct).toFixed(2)}%`;
      }
    };
    const paintCard = (t) => {
      const el = cardEls[t.symbol];
      if (!el || t.price == null) return;
      livePaint(el.querySelector(".p"), t.price, tapeLast[t.symbol], fmtLive(t.price));
      const c = el.querySelector(".c");
      if (c && t.change_percent != null) {
        const pct = t.change_percent * 100;
        c.className = "c " + (pct >= 0 ? "up" : "down");
        c.textContent = `${pct >= 0 ? "▲ +" : "▼ "}${Math.abs(pct).toFixed(2)}% today`;
      }
    };
    tapeLive = Live.watch(quotes.map((q) => q.symbol), (ticks) => {
      ticks.forEach((t) => { paintTape(t); paintCard(t); tapeLast[t.symbol] = t.price ?? tapeLast[t.symbol]; });
    }, { scope: "global" });
    document.querySelectorAll("#mk-grid .q-x").forEach((x) => {
      x.onclick = async (ev) => {
        ev.stopPropagation(); // don't open the stock we're removing
        try {
          await api(`/api/user/watchlists/${marketWatchlist.id}/items/${x.dataset.rm}`, { method: "DELETE" });
          loadWatchCards();
        } catch (e) { setStatus("ERR: " + e.message); }
      };
    });
  } catch (e) {
    $("mk-grid").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`;
  }
}
$("wm-refresh").onclick = loadWatchCards;
$("wm-add").onkeydown = async (ev) => {
  if (ev.key !== "Enter" || ev.defaultPrevented) return;
  const s = $("wm-add").value.trim().toUpperCase();
  if (!s) return;
  try {
    const w = await ensureMarketWatchlist();
    await api(`/api/user/watchlists/${w.id}/items`, { method: "POST", body: { symbol: s } });
    $("wm-add").value = "";
    loadWatchCards();
  } catch (e) { setStatus("ERR: " + e.message); }
};

// ---------- markets orchestrator ----------
function loadMarketAll() {
  loadBrief();
  loadMarketTiles();
  loadDetail();
  loadCompareChart();
  loadYieldCurve();
  loadSpreads();
  loadFedPolicy();
}

// ---------- today's brief ----------
// One composite endpoint; the judgment (readings, regime call) happens
// server-side so the CLI prints the same brief.
async function loadBrief() {
  try {
    const d = await api("/api/v1/overview/brief");
    const b = d.results;
    $("ov-asof").textContent = "regime: " + b.regime.toLowerCase() +
      " · " + new Date(b.as_of).toLocaleTimeString();

    $("ov-signals").innerHTML = b.signals.map((s) => `
      <div class="sig-card t-${s.tone}">
        <div class="sig-label">${escapeHtml(s.label)}</div>
        <div class="sig-value">${escapeHtml(s.value)}</div>
        <div class="sig-read">${escapeHtml(s.reading || "")}</div>
      </div>`).join("");

    const mover = (r) => `<div class="mini-row"><span>${escapeHtml(r.symbol)}</span>
      <span class="${cls(r.change_percent ?? 0)}">${r.change_percent == null ? "-" : fmtPct(r.change_percent, true)}</span></div>`;
    const m = b.movers || {};
    $("ov-movers").innerHTML =
      (m.gainers || []).map(mover).join("") + (m.losers || []).map(mover).join("") ||
      `<div class="empty">unavailable</div>`;

    $("ov-news").innerHTML = (b.headlines || []).slice(0, 5).map((h) => `
      <a class="mini-link" href="${escapeHtml(String(h.url || "#"))}" target="_blank" rel="noopener"
         title="${escapeHtml(String(h.title || ""))}">
        ${escapeHtml(String(h.title || "").slice(0, 78))}
        <span class="mini-src">${escapeHtml(String(h.source || ""))} · ${timeAgo(h.date)}</span>
      </a>`).join("") || `<div class="empty">unavailable</div>`;

    $("ov-earnings").innerHTML = (b.earnings_today || []).slice(0, 8).map((e) =>
      `<div class="mini-row"><span>${escapeHtml(e.symbol)}</span>
       <span class="mini-src">${escapeHtml(e.name || "")}</span></div>`).join("")
      || `<div class="empty">no reports scheduled</div>`;

    if (d.warnings && d.warnings.length) setStatus("BRIEF LOADED (" + d.warnings.length + " SOURCE(S) SKIPPED)");
  } catch (e) {
    $("ov-signals").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`;
  }
}
$("ov-refresh").onclick = loadBrief;

// ---------- index & asset tiles ----------
const MARKET_TILES = [
  ["^GSPC", "S&P 500"], ["^IXIC", "Nasdaq Comp"], ["^DJI", "Dow Jones"], ["^RUT", "Russell 2000"],
  ["SPY", "SPY ETF"], ["QQQ", "QQQ ETF"], ["IWM", "IWM ETF"], ["TLT", "20Y+ Treasury ETF"],
  ["GLD", "Gold"], ["BTC-USD", "Bitcoin"], ["^VIX", "VIX"], ["DX-Y.NYB", "Dollar Index"],
];
const PALETTE = ["#00c805", "#5ac8fa", "#ffd60a", "#ff5000", "#bf5af2", "#f5f6f7", "#ff8fab", "#64d2ff"];

let mkTilesLive = null;
async function loadMarketTiles() {
  try {
    const syms = MARKET_TILES.map(([s]) => s).join(",");
    const d = await api(`/api/v1/equity/price/quote?symbol=${encodeURIComponent(syms)}`);
    const bySym = {};
    d.results.forEach((r) => { bySym[r.symbol] = r; });
    $("mk-indices").innerHTML = MARKET_TILES.map(([sym, label]) => {
      const q = bySym[sym];
      if (!q || q.last_price == null) return "";
      const chg = q.change_percent;
      const big = q.last_price >= 1000 ? q.last_price.toLocaleString(undefined, { maximumFractionDigits: 0 })
                                       : q.last_price.toFixed(2);
      return `<div class="tile" data-sym="${sym}">
        <div class="t-sym">${sym}</div><div class="t-name">${label}</div>
        <div class="t-px">${big}</div>
        <div class="t-chg ${cls(chg ?? 0)}">${chg == null ? "-" : (chg >= 0 ? "▲ " : "▼ ") + fmtPct(Math.abs(chg))}</div>
      </div>`;
    }).join("") || `<div class="empty">No quotes returned.</div>`;
    $("mk-asof").textContent = "as of " + new Date().toLocaleTimeString();
    document.querySelectorAll("#mk-indices .tile").forEach((t) => {
      t.onclick = () => openStock(t.dataset.sym);
    });
    // From here the tiles move on their own: the board subscribes to the
    // live feed and repaints price and change as prints arrive.
    if (mkTilesLive) mkTilesLive.close();
    const lastPx = Object.fromEntries(MARKET_TILES.map(([sym]) => [sym, bySym[sym]?.last_price]));
    mkTilesLive = Live.watch(MARKET_TILES.map(([sym]) => sym), (ticks) => {
      ticks.forEach((t) => {
        const tile = document.querySelector(`#mk-indices .tile[data-sym="${CSS.escape(t.symbol)}"]`);
        if (!tile || t.price == null) return;
        livePaint(tile.querySelector(".t-px"), t.price, lastPx[t.symbol], fmtLive(t.price));
        lastPx[t.symbol] = t.price;
        const chg = tile.querySelector(".t-chg");
        if (chg && t.change_percent != null) {
          chg.className = `t-chg ${cls(t.change_percent)}`;
          chg.textContent = (t.change_percent >= 0 ? "▲ " : "▼ ") + fmtPct(Math.abs(t.change_percent));
        }
      });
      $("mk-asof").textContent = "live · " + new Date().toLocaleTimeString();
    }, { scope: "market" });
  } catch (e) { $("mk-indices").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`; }
}
$("mk-refresh").onclick = loadMarketTiles;

// ---------- single-symbol detail ----------
let mkRange = "1Y";
document.querySelectorAll("#mk-ranges .chip").forEach((c) => {
  c.onclick = () => {
    document.querySelectorAll("#mk-ranges .chip").forEach((x) => x.classList.remove("active"));
    c.classList.add("active");
    mkRange = c.dataset.range;
    loadDetail();
  };
});
$("mk-load").onclick = loadDetail;
async function loadDetail() {
  setStatus("LOADING PRICES…");
  try {
    const sym = $("mk-symbol").value.trim().toUpperCase();
    const start = isoAgo(RANGE_DAYS[mkRange]);
    const d = await api(`/api/data/history/${encodeURIComponent(sym)}?start=${start}`);
    $("mk-src").textContent = `${d.source} · ${d.rows} bars`;
    document.querySelectorAll(".qcard").forEach((c) => c.classList.toggle("sel", c.dataset.sym === sym));
    const closes = d.bars.map((b) => b.close);
    const last = closes[closes.length - 1], first = closes[0];
    const ch = last / first - 1;
    const hi = Math.max(...closes), lo = Math.min(...closes);
    $("mk-dsym").innerHTML = `${escapeHtml(sym)}<small>${escapeHtml(name(sym))}</small>`;
    $("mk-dprice").textContent = "$" + last.toFixed(2);
    drawLine("mk-chart", d.bars.map((b) => b.date),
      [{ label: sym, data: closes, color: ch >= 0 ? "#00c805" : "#ff5000", fill: true }]);
    $("mk-stats").innerHTML = [
      ["Change", fmtPct(ch, true), cls(ch)],
      ["High", "$" + hi.toFixed(2), ""],
      ["Low", "$" + lo.toFixed(2), ""],
      ["From high", fmtPct(last / hi - 1, true), ""],
    ].map(([k, v, c]) => `<div class="metric"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`).join("");
    $("mk-explain").textContent = `Over ${RANGE_NAME[mkRange]}, ${name(sym)} ${ch >= 0 ? "gained" : "lost"} ${Math.abs(ch * 100).toFixed(1)}%. ` +
      `It traded between $${lo.toFixed(0)} and $${hi.toFixed(0)}, and sits ${Math.abs((last / hi - 1) * 100).toFixed(1)}% below its highest point in this period.`;
    setStatus(`LOADED ${sym} · ${d.source.toUpperCase()}`);
  } catch (e) { setStatus("ERR: " + e.message); }
}

// ---------- custom workspace ----------
// A workspace is intentionally browser-local: it is a personal arrangement
// of existing live views, not a second copy of market or portfolio data. The
// data in every box still comes from the same authenticated API as its full
// terminal page.
const WS_CATALOG = {
  pulse: {
    title: "Market pulse", icon: "●", defaultSize: "standard",
    description: "Regime and the strongest signals from today's brief.",
  },
  quote: {
    title: "Price chart", icon: "↗", defaultSize: "wide", chart: true,
    description: "A six-month chart and live quote for any ticker.",
  },
  watchlist: {
    title: "Watchlist", icon: "☆", defaultSize: "wide",
    description: "Live prices for the symbols in your default watchlist.",
  },
  news: {
    title: "Top news", icon: "N", defaultSize: "standard",
    description: "The latest headlines from across the market wire.",
  },
  markets: {
    title: "Major markets", icon: "M", defaultSize: "wide",
    description: "A compact board of indexes and cross-asset benchmarks.",
  },
  yield: {
    title: "Yield curve", icon: "⌁", defaultSize: "standard", chart: true,
    description: "The current US Treasury curve and key maturities.",
  },
  portfolio: {
    title: "Portfolio", icon: "P", defaultSize: "standard",
    description: "Total value, today's move, cash, and largest holdings.",
  },
  monitor: {
    title: "Quote monitor", icon: "◉", defaultSize: "wide",
    description: "A streaming grid of any tickers: last, change, bid/ask, size, time.",
  },
};
const WS_MONITOR_DEFAULT = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA", "BTC-USD", "EURUSD=X"];
// Live handles per workspace box, closed whenever the box is redrawn or removed.
const wsLive = {};
function wsLiveClose(id) { if (wsLive[id]) { wsLive[id].close(); delete wsLive[id]; } }

let workspaceItems = null;
let wsRenderVersion = 0;
let wsDragged = null;
let wsSavedTimer = null;

function wsDefaultItems() {
  return [
    { id: "pulse-1", type: "pulse", size: "standard" },
    { id: "quote-1", type: "quote", size: "wide", symbol: "AAPL" },
    { id: "watchlist-1", type: "watchlist", size: "wide" },
    { id: "news-1", type: "news", size: "standard" },
    { id: "markets-1", type: "markets", size: "wide" },
    { id: "yield-1", type: "yield", size: "standard" },
  ];
}

function wsStorageKey() {
  return "mft_workspace_v1_" + String(workspaceOwner || "local").replace(/[^a-z0-9_-]/gi, "_");
}

function wsRead() {
  const raw = localStorage.getItem(wsStorageKey());
  if (raw == null) return wsDefaultItems();
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) throw new Error("invalid workspace");
    return parsed.filter((item) => item && WS_CATALOG[item.type]).map((item, i) => {
      const symbol = String(item.symbol || "AAPL").toUpperCase().slice(0, 15);
      return {
        id: /^[a-z0-9-]+$/i.test(String(item.id || "")) ? String(item.id) : `${item.type}-${Date.now()}-${i}`,
        type: item.type,
        size: item.size === "wide" ? "wide" : "standard",
        ...(item.type === "quote" ? { symbol: /^[A-Z0-9^.=-]{1,15}$/.test(symbol) ? symbol : "AAPL" } : {}),
        ...(item.type === "monitor" ? {
          symbols: (Array.isArray(item.symbols) ? item.symbols : WS_MONITOR_DEFAULT)
            .map((x) => String(x).toUpperCase().slice(0, 20)).filter((x) => /^[A-Z0-9^.=-]{1,20}$/.test(x)).slice(0, 40),
          provider: item.provider === "alpaca" ? "alpaca" : null,
        } : {}),
      };
    });
  } catch {
    return wsDefaultItems();
  }
}

function wsSave(flash = true) {
  try { localStorage.setItem(wsStorageKey(), JSON.stringify(workspaceItems)); } catch { /* layout still works for this tab */ }
  if (!flash) return;
  const note = $("ws-saved");
  note.textContent = "Layout saved";
  note.classList.add("flash");
  clearTimeout(wsSavedTimer);
  wsSavedTimer = setTimeout(() => {
    note.textContent = "Saved on this device";
    note.classList.remove("flash");
  }, 1300);
}

function wsPickerHtml() {
  return Object.entries(WS_CATALOG).map(([type, item]) => `
    <button class="workspace-picker-card" data-ws-pick="${type}">
      <span class="workspace-picker-icon">${item.icon}</span>
      <span class="workspace-picker-copy"><b>${escapeHtml(item.title)}</b><span>${escapeHtml(item.description)}</span></span>
      <span class="workspace-picker-plus">＋</span>
    </button>`).join("");
}

function openWsPicker() {
  $("ws-picker").innerHTML = wsPickerHtml();
  $("ws-picker").querySelectorAll("[data-ws-pick]").forEach((button) => {
    button.onclick = () => {
      const type = button.dataset.wsPick;
      const meta = WS_CATALOG[type];
      workspaceItems.push({
        id: `${type}-${Date.now()}-${Math.floor(Math.random() * 1000)}`,
        type, size: meta.defaultSize,
        ...(type === "quote" ? { symbol: "AAPL" } : {}),
        ...(type === "monitor" ? { symbols: [...WS_MONITOR_DEFAULT], provider: null } : {}),
      });
      wsSave();
      $("ws-dialog").close();
      renderWorkspace();
      setStatus(`${meta.title.toUpperCase()} ADDED TO WORKSPACE`);
    };
  });
  $("ws-dialog").showModal();
}

function loadWorkspace() {
  if (workspaceItems === null) workspaceItems = wsRead();
  renderWorkspace();
}

function wsBody(item, version) {
  const body = $(`ws-body-${item.id}`);
  if (!body || body.dataset.render !== String(version)) return null;
  return body;
}

function wsFail(item, version, error) {
  const body = wsBody(item, version);
  if (body) body.innerHTML = `<div class="workspace-error">This view could not load.<br>${escapeHtml(error.message || String(error))}</div>`;
}

function renderWorkspace() {
  const grid = $("ws-grid");
  const version = ++wsRenderVersion;
  Object.keys(charts).filter((id) => id.startsWith("ws-chart-")).forEach((id) => {
    charts[id].destroy(); delete charts[id];
  });
  Object.keys(wsLive).forEach(wsLiveClose);

  if (!workspaceItems.length) {
    grid.innerHTML = `<div class="workspace-empty">
      <div class="workspace-empty-mark">＋</div>
      <h2>Build your workspace</h2>
      <p>Add the live views you use most. Each one appears in its own box and the layout stays saved here.</p>
      <button class="primary" data-ws-add-empty>＋ Add your first view</button>
    </div>`;
    grid.querySelector("[data-ws-add-empty]").onclick = openWsPicker;
    return;
  }

  grid.innerHTML = workspaceItems.map((item) => {
    const meta = WS_CATALOG[item.type];
    const sub = item.type === "quote"
      ? `<button class="ws-symbol-button" data-ws-symbol title="Change ticker">${escapeHtml(item.symbol || "AAPL")} ▾</button>`
      : item.type === "monitor"
      ? `<button class="ws-symbol-button" data-ws-symbols title="Edit tickers">${(item.symbols || []).length} tickers ▾</button>
         <button class="ws-symbol-button ws-provider-button" data-ws-provider title="Price source">${escapeHtml((item.provider || "auto").toUpperCase())}</button>`
      : `<span class="workspace-widget-sub">LIVE VIEW</span>`;
    return `<article class="workspace-widget ${item.size === "wide" ? "ws-wide" : ""}"
        data-wsid="${item.id}" data-render="${version}" draggable="true">
      <header class="workspace-widget-head">
        <span class="workspace-widget-dot"></span>
        <span class="workspace-widget-title">${escapeHtml(meta.title)}</span>
        ${sub}
        <span class="workspace-widget-actions">
          <button class="workspace-icon-btn" data-ws-refresh title="Refresh view" aria-label="Refresh ${escapeHtml(meta.title)}">↻</button>
          <button class="workspace-icon-btn" data-ws-size title="${item.size === "wide" ? "Make compact" : "Make wide"}" aria-label="Resize ${escapeHtml(meta.title)}">↔</button>
          <button class="workspace-icon-btn ws-remove" data-ws-remove title="Remove view" aria-label="Remove ${escapeHtml(meta.title)}">&times;</button>
        </span>
      </header>
      <div id="ws-body-${item.id}" class="workspace-widget-body${meta.chart ? " ws-body-chart" : ""}" data-render="${version}">
        <div class="workspace-loading">Loading ${escapeHtml(meta.title.toLowerCase())}</div>
      </div>
    </article>`;
  }).join("");

  grid.querySelectorAll(".workspace-widget").forEach((card) => {
    const id = card.dataset.wsid;
    const item = workspaceItems.find((entry) => entry.id === id);
    card.querySelector("[data-ws-refresh]").onclick = () => {
      const body = wsBody(item, version);
      if (body) body.innerHTML = `<div class="workspace-loading">Refreshing view</div>`;
      loadWsWidget(item, version);
    };
    card.querySelector("[data-ws-size]").onclick = () => {
      item.size = item.size === "wide" ? "standard" : "wide";
      wsSave(); renderWorkspace();
    };
    card.querySelector("[data-ws-remove]").onclick = () => {
      wsLiveClose(id);
      workspaceItems = workspaceItems.filter((entry) => entry.id !== id);
      wsSave(); renderWorkspace();
      setStatus(`${WS_CATALOG[item.type].title.toUpperCase()} REMOVED FROM WORKSPACE`);
    };
    const symbol = card.querySelector("[data-ws-symbol]");
    if (symbol) symbol.onclick = () => {
      const next = prompt("Ticker symbol", item.symbol || "AAPL");
      if (next == null) return;
      const clean = next.trim().toUpperCase();
      if (!/^[A-Z0-9^.=-]{1,15}$/.test(clean)) { setStatus("ENTER A VALID TICKER"); return; }
      item.symbol = clean; wsSave(); renderWorkspace();
    };
    const symbols = card.querySelector("[data-ws-symbols]");
    if (symbols) symbols.onclick = () => {
      const next = prompt("Tickers, comma-separated (up to 40)", (item.symbols || []).join(", "));
      if (next == null) return;
      const clean = [...new Set(next.split(/[,\s]+/).map((x) => x.trim().toUpperCase()).filter(Boolean))];
      if (!clean.length || clean.some((x) => !/^[A-Z0-9^.=-]{1,20}$/.test(x))) { setStatus("ENTER VALID TICKERS"); return; }
      item.symbols = clean.slice(0, 40); wsSave(); renderWorkspace();
    };
    const providerBtn = card.querySelector("[data-ws-provider]");
    if (providerBtn) providerBtn.onclick = async () => {
      await Live.ensureStatus();
      const alpaca = Live.providers().alpaca;
      if (!alpaca || !alpaca.available) {
        setStatus("ALPACA NOT CONFIGURED — SET MFT_ALPACA_API_KEY/SECRET FOR BID/ASK");
        alert("Auto uses Yahoo's key-free feed (last price only).\n\n" + (alpaca?.note || "Set MFT_ALPACA_API_KEY and MFT_ALPACA_API_SECRET to add licensed bid/ask."));
        return;
      }
      item.provider = item.provider === "alpaca" ? null : "alpaca";
      wsSave(); renderWorkspace();
    };

    card.ondragstart = (event) => {
      wsDragged = id;
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", id);
      requestAnimationFrame(() => card.classList.add("dragging"));
    };
    card.ondragover = (event) => {
      if (!wsDragged || wsDragged === id) return;
      event.preventDefault(); card.classList.add("dragover");
    };
    card.ondragleave = () => card.classList.remove("dragover");
    card.ondrop = (event) => {
      event.preventDefault(); card.classList.remove("dragover");
      const from = workspaceItems.findIndex((entry) => entry.id === wsDragged);
      const originalTarget = workspaceItems.findIndex((entry) => entry.id === id);
      if (from < 0 || originalTarget < 0 || wsDragged === id) return;
      const [moved] = workspaceItems.splice(from, 1);
      let target = workspaceItems.findIndex((entry) => entry.id === id);
      if (from < originalTarget) target += 1;
      workspaceItems.splice(Math.max(0, target), 0, moved);
      wsDragged = null; wsSave(); renderWorkspace();
    };
    card.ondragend = () => {
      wsDragged = null;
      grid.querySelectorAll(".workspace-widget").forEach((entry) => entry.classList.remove("dragging", "dragover"));
    };
  });

  workspaceItems.forEach((item) => loadWsWidget(item, version));
}

async function loadWsWidget(item, version) {
  try {
    if (item.type === "quote") await loadWsQuote(item, version);
    else if (item.type === "watchlist") await loadWsWatchlist(item, version);
    else if (item.type === "news") await loadWsNews(item, version);
    else if (item.type === "markets") await loadWsMarkets(item, version);
    else if (item.type === "yield") await loadWsYield(item, version);
    else if (item.type === "portfolio") await loadWsPortfolio(item, version);
    else if (item.type === "monitor") await loadWsMonitor(item, version);
    else await loadWsPulse(item, version);
  } catch (error) { wsFail(item, version, error); }
}

async function loadWsQuote(item, version) {
  const sym = String(item.symbol || "AAPL").toUpperCase();
  const [quoteData, history] = await Promise.all([
    api(`/api/v1/equity/price/quote?symbol=${encodeURIComponent(sym)}`),
    api(`/api/data/history/${encodeURIComponent(sym)}?start=${isoAgo(183)}`),
  ]);
  const body = wsBody(item, version);
  if (!body) return;
  const q = Array.isArray(quoteData.results) ? quoteData.results[0] : quoteData.results;
  if (!q || !history.bars?.length) throw new Error(`No price data returned for ${sym}`);
  const closes = history.bars.map((bar) => bar.close);
  const first = closes[0], last = closes[closes.length - 1];
  const periodChange = last / first - 1;
  const daily = q.change_percent;
  const high = Math.max(...closes), low = Math.min(...closes);
  const canvasId = `ws-chart-${item.id}`;
  body.innerHTML = `<div class="ws-hero">
      <div><button class="ws-symbol-button ws-hero-symbol" data-open-symbol>${escapeHtml(sym)}</button>
        <div class="ws-hero-name">${escapeHtml(q.name || name(sym))} · 6 months</div></div>
      <div class="ws-hero-price">${q.last_price == null ? "$" + last.toFixed(2) : "$" + q.last_price.toFixed(2)}
        <div class="ws-hero-change ${cls(daily ?? periodChange)}">${daily == null ? "" : `${daily >= 0 ? "▲ +" : "▼ "}${Math.abs(daily * 100).toFixed(2)}% today`}</div>
      </div>
    </div>
    <div class="ws-chart"><canvas id="${canvasId}"></canvas></div>
    <div class="ws-quote-stats">
      <div class="ws-quote-stat"><span>6M return</span><b class="${cls(periodChange)}">${fmtPct(periodChange, true)}</b></div>
      <div class="ws-quote-stat"><span>Period high</span><b>$${high.toFixed(2)}</b></div>
      <div class="ws-quote-stat"><span>Period low</span><b>$${low.toFixed(2)}</b></div>
    </div>`;
  drawLine(canvasId, history.bars.map((bar) => bar.date),
    [{ label: sym, data: closes, color: periodChange >= 0 ? "#00c805" : "#ff5000", fill: true }],
    { fitBox: true });
  body.querySelector("[data-open-symbol]").onclick = () => openStock(sym, "workspace");
  // The hero price follows the live feed; the chart stays the daily history.
  wsLiveClose(item.id);
  let lastPx = q.last_price ?? last;
  const priceEl = body.querySelector(".ws-hero-price");
  const changeEl = body.querySelector(".ws-hero-change");
  wsLive[item.id] = Live.watch([sym], (ticks) => {
    const t = ticks[ticks.length - 1];
    if (!t || t.price == null || !priceEl.isConnected) return;
    // Repaint only the price text node; the change line is its own element.
    livePaint(priceEl, t.price, lastPx);
    priceEl.firstChild.nodeValue = "$" + fmtLive(t.price);
    lastPx = t.price;
    if (t.change_percent != null) {
      changeEl.className = `ws-hero-change ${cls(t.change_percent)}`;
      changeEl.textContent = `${t.change_percent >= 0 ? "▲ +" : "▼ "}${Math.abs(t.change_percent * 100).toFixed(2)}% today`;
    }
  }, { scope: "workspace" });
}

async function loadWsPulse(item, version) {
  const d = await api("/api/v1/overview/brief");
  const body = wsBody(item, version);
  if (!body) return;
  const brief = d.results;
  const signals = (brief.signals || []).slice(0, 6);
  body.innerHTML = `<div class="ws-regime"><i></i><b>${escapeHtml(String(brief.regime || "Market").toUpperCase())}</b>
      <span>· updated ${new Date(brief.as_of || Date.now()).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span></div>` +
    (signals.length ? signals.map((signal) => `<div class="ws-signal-row">
      <span class="ws-signal-label">${escapeHtml(signal.label)}</span>
      <span class="ws-signal-value ${signal.tone === "pos" ? "pos" : signal.tone === "neg" ? "neg" : ""}">${escapeHtml(signal.value)}</span>
    </div>`).join("") : `<div class="empty">No signals returned.</div>`);
}

async function loadWsMarkets(item, version) {
  const selected = MARKET_TILES.slice(0, 6);
  const d = await api(`/api/v1/equity/price/quote?symbol=${encodeURIComponent(selected.map(([sym]) => sym).join(","))}`);
  const body = wsBody(item, version);
  if (!body) return;
  const quotes = Object.fromEntries((d.results || []).map((q) => [q.symbol, q]));
  body.innerHTML = `<div class="ws-market-grid">` + selected.map(([sym, label]) => {
    const q = quotes[sym];
    if (!q || q.last_price == null) return "";
    const price = q.last_price >= 1000 ? q.last_price.toLocaleString(undefined, { maximumFractionDigits: 0 }) : q.last_price.toFixed(2);
    return `<div class="ws-market-tile" data-ws-market="${escapeHtml(sym)}">
      <div class="ws-market-tile-top"><span class="s">${escapeHtml(sym)}</span><span class="c ${cls(q.change_percent ?? 0)}">${q.change_percent == null ? "-" : fmtPct(q.change_percent, true)}</span></div>
      <div class="p">${price}</div><div class="ws-hero-name">${escapeHtml(label)}</div>
    </div>`;
  }).join("") + `</div>`;
  body.querySelectorAll("[data-ws-market]").forEach((tile) => {
    tile.onclick = () => openStock(tile.dataset.wsMarket, "workspace");
  });
  wsLiveClose(item.id);
  const lastPx = Object.fromEntries(selected.map(([sym]) => [sym, quotes[sym]?.last_price]));
  wsLive[item.id] = Live.watch(selected.map(([sym]) => sym), (ticks) => {
    ticks.forEach((t) => {
      const tile = body.querySelector(`[data-ws-market="${CSS.escape(t.symbol)}"]`);
      if (!tile || t.price == null) return;
      livePaint(tile.querySelector(".p"), t.price, lastPx[t.symbol], fmtLive(t.price));
      lastPx[t.symbol] = t.price;
      const c = tile.querySelector(".c");
      if (c && t.change_percent != null) { c.className = `c ${cls(t.change_percent)}`; c.textContent = fmtPct(t.change_percent, true); }
    });
  }, { scope: "workspace" });
}

async function loadWsWatchlist(item, version) {
  const list = await ensureMarketWatchlist();
  const d = await api(`/api/user/watchlists/${list.id}/quotes`);
  const body = wsBody(item, version);
  if (!body) return;
  const rows = (d.results || []).slice(0, 10);
  body.innerHTML = rows.length ? `<table class="ws-table"><tr><th>Symbol</th><th>Name</th><th>Last</th><th>Today</th></tr>` +
    rows.map((q) => `<tr><td><button class="linkbtn" data-ws-watch="${escapeHtml(q.symbol)}">${escapeHtml(q.symbol)}</button></td>
      <td class="ws-name">${escapeHtml(q.name || name(q.symbol))}</td>
      <td class="mono">${q.last_price == null ? "-" : "$" + q.last_price.toFixed(2)}</td>
      <td class="mono ${cls(q.change_percent ?? 0)}">${q.change_percent == null ? "-" : fmtPct(q.change_percent, true)}</td></tr>`).join("") +
    `</table>` : `<div class="workspace-loading">Your watchlist is empty</div>`;
  body.querySelectorAll("[data-ws-watch]").forEach((button) => {
    button.onclick = () => openStock(button.dataset.wsWatch, "workspace");
  });
  wsLiveClose(item.id);
  const lastPx = Object.fromEntries(rows.map((q) => [q.symbol, q.last_price]));
  wsLive[item.id] = Live.watch(rows.map((q) => q.symbol), (ticks) => {
    ticks.forEach((t) => {
      const button = body.querySelector(`[data-ws-watch="${CSS.escape(t.symbol)}"]`);
      const tr = button && button.closest("tr");
      if (!tr || t.price == null) return;
      const cells = tr.querySelectorAll("td.mono");
      livePaint(cells[0], t.price, lastPx[t.symbol], "$" + fmtLive(t.price));
      lastPx[t.symbol] = t.price;
      if (cells[1] && t.change_percent != null) { cells[1].className = `mono ${cls(t.change_percent)}`; cells[1].textContent = fmtPct(t.change_percent, true); }
    });
  }, { scope: "workspace" });
}

// The quote monitor is the one box that is *only* live: it draws its rows
// from the stream's own snapshot and never calls the delayed quote endpoint.
async function loadWsMonitor(item, version) {
  const symbols = (item.symbols || []).length ? item.symbols : WS_MONITOR_DEFAULT;
  const status = await Live.ensureStatus();
  const provider = item.provider === "alpaca" && status.providers?.alpaca?.available ? "alpaca" : null;
  const showBook = provider === "alpaca";
  const body = wsBody(item, version);
  if (!body) return;
  const feedNote = provider ? "Alpaca · licensed trades and bid/ask (IEX feed)"
    : `Yahoo · key-free last price${status.providers?.alpaca?.available ? " · switch the source above for bid/ask" : ""}`;
  body.innerHTML = `<div class="ws-monitor-note"><span class="ws-monitor-dot"></span><span data-monitor-note>${escapeHtml(feedNote)} · waiting for prints…</span></div>
    <div class="ws-monitor-wrap"><table class="ws-table ws-monitor">
      <tr><th>Symbol</th><th class="num">Last</th><th class="num">Chg</th><th class="num">Chg %</th>${showBook ? '<th class="num">Bid</th><th class="num">Ask</th>' : ""}<th class="num">${showBook ? "Size" : "Volume"}</th><th class="num">Time</th></tr>
      ${symbols.map((sym) => `<tr data-monitor-row="${escapeHtml(sym)}">
        <td><button class="linkbtn" data-monitor-open="${escapeHtml(sym)}">${escapeHtml(sym)}</button></td>
        <td class="mono num" data-f="price">-</td><td class="mono num" data-f="change">-</td><td class="mono num" data-f="pct">-</td>
        ${showBook ? '<td class="mono num" data-f="bid">-</td><td class="mono num" data-f="ask">-</td>' : ""}
        <td class="mono num" data-f="size">-</td><td class="mono num dim" data-f="time">-</td>
      </tr>`).join("")}
    </table></div>`;
  body.querySelectorAll("[data-monitor-open]").forEach((b) => { b.onclick = () => openStock(b.dataset.monitorOpen, "workspace"); });
  const lastPx = {};
  let prints = 0;
  const note = body.querySelector("[data-monitor-note]");
  const paint = (t) => {
    const tr = body.querySelector(`[data-monitor-row="${CSS.escape(t.symbol)}"]`);
    if (!tr) return;
    const cell = (f) => tr.querySelector(`[data-f="${f}"]`);
    if (t.price != null) { livePaint(cell("price"), t.price, lastPx[t.symbol], fmtLive(t.price)); lastPx[t.symbol] = t.price; }
    if (t.change != null) { const c = cell("change"); c.textContent = (t.change >= 0 ? "+" : "-") + fmtLive(Math.abs(t.change)); c.className = `mono num ${cls(t.change)}`; }
    if (t.change_percent != null) { const c = cell("pct"); c.textContent = (t.change_percent >= 0 ? "+" : "") + (t.change_percent * 100).toFixed(2) + "%"; c.className = `mono num ${cls(t.change_percent)}`; }
    if (showBook) {
      const bid = cell("bid"), ask = cell("ask");
      if (t.bid != null) bid.textContent = `${fmtLive(t.bid)}${t.bid_size != null ? " ×" + t.bid_size : ""}`;
      if (t.ask != null) ask.textContent = `${fmtLive(t.ask)}${t.ask_size != null ? " ×" + t.ask_size : ""}`;
      if (t.size != null) cell("size").textContent = Number(t.size).toLocaleString();
    } else if (t.volume != null) cell("size").textContent = Number(t.volume).toLocaleString();
    if (t.time) cell("time").textContent = new Date(t.time).toLocaleTimeString([], { hour12: false });
  };
  wsLiveClose(item.id);
  wsLive[item.id] = Live.watch(symbols, (ticks) => {
    if (!body.isConnected) return;
    ticks.forEach(paint);
    prints += ticks.length;
    if (note) note.textContent = `${feedNote} · ${prints.toLocaleString()} prints · ${new Date().toLocaleTimeString([], { hour12: false })}`;
  }, { scope: "workspace", provider });
}

async function loadWsNews(item, version) {
  const d = await api("/api/v1/news/world?limit=8");
  const body = wsBody(item, version);
  if (!body) return;
  const rows = (d.results || []).slice(0, 7);
  body.innerHTML = rows.length ? rows.map((story) => `<a class="ws-news-item" href="${escapeHtml(story.url || "#")}" target="_blank" rel="noopener">
      <span class="ws-news-title">${escapeHtml(story.title || "Untitled story")}</span>
      <span class="ws-news-meta"><span>${escapeHtml(story.source || "Wire")}</span><span>${timeAgo(story.date)}</span></span>
    </a>`).join("") : `<div class="workspace-loading">No recent stories</div>`;
}

async function loadWsYield(item, version) {
  const d = await api("/api/v1/fixedincome/government/yield_curve");
  const body = wsBody(item, version);
  if (!body) return;
  const rows = d.results || [];
  if (!rows.length) throw new Error("No curve data returned");
  const canvasId = `ws-chart-${item.id}`;
  const rate = (maturity) => rows.find((row) => String(row.maturity).toLowerCase() === maturity)?.rate;
  const two = rate("2 yr"), ten = rate("10 yr");
  // The last card is a spread, so it carries a sign and a colour; the rest are levels.
  const cards = [["3M", rate("3 mo")], ["2Y", two], ["10Y", ten], ["30Y", rate("30 yr")],
    ["10Y−2Y", two != null && ten != null ? ten - two : null, true]];
  body.innerHTML = `<div class="ws-hero-name">US Treasury par yields · ${escapeHtml(rows[0].date || "latest")}</div>
    <div class="ws-chart"><canvas id="${canvasId}"></canvas></div>
    <div class="ws-rate-strip">${cards.map(([label, value, spread]) => `<div class="ws-rate"><span>${label}</span><b class="${spread ? cls(value ?? 0) : ""}">${value == null ? "-" : (spread && value >= 0 ? "+" : "") + value.toFixed(2) + "%"}</b></div>`).join("")}</div>`;
  drawLine(canvasId, rows.map((row) => row.maturity),
    [{ label: "Par yield %", data: rows.map((row) => row.rate), color: "#00c805" }],
    { fitBox: true });
}

async function loadWsPortfolio(item, version) {
  const portfolios = await api("/api/portfolios");
  const body = wsBody(item, version);
  if (!body) return;
  if (!portfolios.length) {
    body.innerHTML = `<div class="workspace-empty" style="min-height:100%;border:0;background:transparent">
      <div class="workspace-empty-mark">P</div><h2>No portfolio yet</h2><p>Create one on the Portfolio tab to see it here.</p>
    </div>`;
    return;
  }
  const portfolio = portfolios.find((entry) => entry.is_default) || portfolios[0];
  const d = await api(`/api/portfolios/${portfolio.id}/summary`);
  const freshBody = wsBody(item, version);
  if (!freshBody) return;
  const t = d.totals;
  freshBody.innerHTML = `<div class="ws-portfolio-label">${escapeHtml(portfolio.name)} · total value</div>
    <div class="ws-portfolio-value">${fmt$(t.total_value)}</div>
    <div class="ws-hero-change ${cls(t.day_change || 0)}">${t.day_change == null ? "No daily move yet" : `${t.day_change >= 0 ? "▲ +" : "▼ "}${fmt$(Math.abs(t.day_change))} today`}</div>
    <div class="ws-portfolio-strip">
      <div class="ws-portfolio-cell"><span>Total P&amp;L</span><b class="${cls(t.total_pnl || 0)}">${fmt$(t.total_pnl || 0)}</b></div>
      <div class="ws-portfolio-cell"><span>Cash</span><b>${fmt$(t.cash || 0)}</b></div>
      <div class="ws-portfolio-cell"><span>Holdings</span><b>${d.positions.length}</b></div>
    </div>
    ${(d.positions || []).slice(0, 5).map((position) => `<div class="ws-holding"><b>${escapeHtml(position.symbol)}</b><span>${fmtPct(position.weight)}</span><span class="${cls(position.unrealized_pnl || 0)}">${fmt$(position.market_value)}</span></div>`).join("") || `<div class="empty">No open positions yet.</div>`}`;
}

$("ws-add").onclick = openWsPicker;
$("ws-dialog-close").onclick = () => $("ws-dialog").close();
$("ws-dialog").onclick = (event) => { if (event.target === $("ws-dialog")) $("ws-dialog").close(); };
$("ws-reset").onclick = () => {
  if (!confirm("Reset the workspace to its original set of views?")) return;
  workspaceItems = wsDefaultItems(); wsSave(); renderWorkspace();
  setStatus("WORKSPACE RESET");
};

// ---------- stock page (expand into a symbol) ----------
// Opened from watchlist cards, index tiles or the Saved tab. Each section
// loads and fails independently, so an index or ETF still shows its chart
// and stats even where fundamentals do not exist.
let stSym = null, stRange = "1Y", stFrom = "market";

// Per-stock view state. `mode` is the only piece that outlives the page — it is
// a preference, so it rides on the account rather than on this tab.
let stMode = "simple";
let stAdvRange = "1Y", stChartType = "candles";
const stOverlays = { sma50: true, sma200: true, bollinger: false };
let stQuote = null;

// Simple is the narrow reading view; the other two are diagrams and tables that
// want the full window.
const ST_MODES = ["simple", "advanced", "exposure", "financials", "compare"];
const stWide = () => stMode !== "simple";

function switchToView(id) {
  document.querySelectorAll(".navbtn").forEach((x) => x.classList.remove("active"));
  document.querySelectorAll(".view").forEach((x) => x.classList.remove("active"));
  $(id).classList.add("active");
  const container = document.querySelector(".container");
  container.classList.toggle("workspace-open", id === "view-workspace");
  container.classList.toggle("stock-wide", id === "view-stock" && stWide());
  document.querySelector(".content").scrollTop = 0;
}

// The mode survives navigation and sign-in: localStorage answers instantly on
// the first paint, the account setting is the copy that follows you to another
// browser. A failed write is not worth interrupting anyone over.
const ST_MODE_KEY = "mft_stock_mode";
async function loadStockMode() {
  const local = localStorage.getItem(ST_MODE_KEY);
  if (ST_MODES.includes(local)) stMode = local;
  try {
    const rows = await api("/api/user/settings");
    const saved = (rows || []).find((r) => r.key === ST_MODE_KEY);
    if (saved && ST_MODES.includes(saved.value)) {
      stMode = saved.value;
      localStorage.setItem(ST_MODE_KEY, stMode);
      if ($("view-stock").classList.contains("active")) applyStockMode();
    }
  } catch { /* the local copy is enough */ }
}

function setStockMode(mode) {
  if (mode === stMode) return;
  stMode = mode;
  try { localStorage.setItem(ST_MODE_KEY, mode); } catch { /* private mode */ }
  api("/api/user/settings", { method: "PUT", body: { key: ST_MODE_KEY, value: mode } })
    .catch(() => { /* the local copy still holds */ });
  applyStockMode();
}

function applyStockMode() {
  document.querySelectorAll("#st-mode .chip").forEach((c) =>
    c.classList.toggle("active", c.dataset.mode === stMode));
  $("st-simple").style.display = stMode === "simple" ? "" : "none";
  $("st-advanced").style.display = stMode === "advanced" ? "" : "none";
  $("st-exposure").style.display = stMode === "exposure" ? "" : "none";
  $("st-financials").style.display = stMode === "financials" ? "" : "none";
  $("st-compare").style.display = stMode === "compare" ? "" : "none";
  document.querySelector(".container").classList.toggle("stock-wide", stWide());
  if (stMode === "advanced") openStockAdvanced();
  else if (stMode === "exposure") openStockExposure();
  else if (stMode === "financials") openStockFinancials();
  else if (stMode === "compare") openStockCompare();
  else stResizeCharts();          // the simple chart was laid out while hidden
}
document.querySelectorAll("#st-mode .chip").forEach((c) => {
  c.onclick = () => setStockMode(c.dataset.mode);
});

function openStock(sym, from = "market") {
  stSym = String(sym).trim().toUpperCase();
  stFrom = from;
  stQuote = null;
  switchToView("view-stock");
  $("st-back").textContent = from === "sector" && seRow ? `← Back to ${seRow.group}`
    : from === "portfolio" ? "← Back to Portfolio"
    : from === "workspace" ? "← Back to Workspace"
    : from === "screener" ? "← Back to Screener" : "← Back to Markets";
  $("st-sym").innerHTML = `${escapeHtml(stSym)}<small></small>`;
  $("st-price").textContent = "";
  $("st-chg").textContent = "";
  $("st-meta").textContent = "";
  $("st-stats").innerHTML = "";
  $("st-explain").textContent = "";
  ["st-about", "st-analyst", "st-fin", "st-news", "st-ratings"].forEach((id) => {
    $(id).innerHTML = `<div class="empty">Loading…</div>`;
  });
  resetStockAdvanced();
  resetStockExposure();
  resetStockFinancials();
  resetStockCompare();
  loadStockChart();
  loadStockSummary();
  loadStockQuote();
  loadStockProfile();
  loadStockAnalyst();
  loadStockRatings();
  loadStockFinancials();
  loadStockNews();
  updateWatchButton();
  applyStockMode();
}
$("st-back").onclick = () => {
  if (stFrom === "sector" && seRow) switchToView("view-sector");
  else if (stFrom === "portfolio") document.querySelector('.navbtn[data-view="portfolio"]').click();
  else if (stFrom === "workspace") document.querySelector('.navbtn[data-view="workspace"]').click();
  else if (stFrom === "screener") document.querySelector('.navbtn[data-view="screener"]').click();
  else if (stFrom === "calendar") document.querySelector('.navbtn[data-view="calendar"]').click();
  else if (stFrom === "volatility") document.querySelector('.navbtn[data-view="volatility"]').click();
  else if (stFrom === "flagged") document.querySelector('.navbtn[data-view="flagged"]').click();
  else document.querySelector('.navbtn[data-view="market"]').click();
};
document.querySelectorAll("#st-ranges .chip").forEach((c) => {
  c.onclick = () => {
    document.querySelectorAll("#st-ranges .chip").forEach((x) => x.classList.remove("active"));
    c.classList.add("active");
    stRange = c.dataset.range;
    loadStockChart();
    loadStockSummary();
  };
});

function fmtBig(x) {
  if (x == null || isNaN(x)) return "-";
  const a = Math.abs(x);
  if (a >= 1e12) return (x / 1e12).toFixed(2) + "T";
  if (a >= 1e9) return (x / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return (x / 1e6).toFixed(1) + "M";
  return Math.round(x).toLocaleString();
}
const one = (results) => Array.isArray(results) ? results[0] : results;

async function loadStockChart() {
  try {
    const d = await api(`/api/data/history/${encodeURIComponent(stSym)}?start=${isoAgo(RANGE_DAYS[stRange])}`);
    const closes = d.bars.map((b) => b.close);
    const ch = closes[closes.length - 1] / closes[0] - 1;
    drawLine("st-chart", d.bars.map((b) => b.date),
      [{ label: stSym, data: closes, color: ch >= 0 ? "#00c805" : "#ff5000", fill: true }]);
  } catch (e) { setStatus("ERR: " + e.message); }
}

async function loadStockQuote() {
  try {
    const d = await api(`/api/v1/equity/price/quote?symbol=${encodeURIComponent(stSym)}`);
    const q = one(d.results);
    if (!q) throw new Error("no quote");
    stQuote = q;
    $("st-sym").innerHTML = `${escapeHtml(stSym)}<small>${escapeHtml(q.name || "")}</small>`;
    if (q.last_price != null) $("st-price").textContent = "$" + (q.last_price >= 1000
      ? q.last_price.toLocaleString(undefined, { maximumFractionDigits: 0 }) : q.last_price.toFixed(2));
    // Absolute move first, then the percentage — the header format the rest of
    // the terminal's price rows use.
    if (q.change != null || q.change_percent != null) {
      const abs = q.change, pct = q.change_percent;
      const up = (abs ?? pct ?? 0) >= 0;
      $("st-chg").textContent = [
        abs != null ? (up ? "+" : "−") + Math.abs(abs).toFixed(2) : null,
        pct != null ? (up ? "+" : "−") + Math.abs(pct * 100).toFixed(2) + "%" : null,
      ].filter(Boolean).join("  ");
      $("st-chg").className = "mono " + (up ? "pos" : "neg");
    }
    $("st-meta").textContent = [q.exchange, q.currency].filter(Boolean).join(" · ");
    renderSimpleStats();
    renderStockKeyStats();
  } catch (e) { $("st-stats").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`; }
}

// ---- simple mode: everything below the chart is derived from the window ----
// Nothing here is written by hand: the numbers come from /quantitative and
// /technical over exactly the range the chips select, and the paragraph is
// assembled from those same numbers, so the prose can never contradict them.
let stPerf = null, stMaNow = null;

async function loadStockSummary() {
  stPerf = null; stMaNow = null;
  const start = isoAgo(RANGE_DAYS[stRange]);
  const perf = api(`/api/v1/quantitative/performance?symbol=${encodeURIComponent(stSym)},SPY&start_date=${start}`)
    .then((d) => {
      const rows = d.results || [];
      stPerf = {
        subject: rows.find((r) => r.symbol === stSym) || null,
        bench: rows.find((r) => r.symbol === "SPY") || null,
      };
    }).catch(() => { stPerf = null; });
  const mas = Promise.all([50, 200].map((len) =>
    api(`/api/v1/technical/sma?symbol=${encodeURIComponent(stSym)}&length=${len}&limit=1`)
      .then((d) => one(d.results)?.[`sma_${len}`] ?? null).catch(() => null)))
    .then(([sma50, sma200]) => { stMaNow = { sma50, sma200 }; });
  await Promise.all([perf, mas]);
  renderSimpleStats();
}

function renderSimpleStats() {
  const subject = stPerf && stPerf.subject, bench = stPerf && stPerf.bench;
  const ret = subject ? subject.total_return : null;
  const vol = subject ? subject.annualised_volatility : null;
  const benchRet = bench ? bench.total_return : null;
  const gap = ret != null && benchRet != null ? (ret - benchRet) * 100 : null;
  const cells = [
    [`${stRange} return`, ret == null ? "-" : fmtPct(ret, true), ret == null ? "" : cls(ret)],
    ["Volatility", vol == null ? "-" : (vol * 100).toFixed(1) + "%", ""],
    ["vs S&P 500", gap == null ? "-" : `${gap >= 0 ? "+" : "−"}${Math.abs(gap).toFixed(1)} pts`,
      gap == null ? "" : cls(gap)],
    ["Market cap", stQuote ? fmtBig(stQuote.market_cap) : "-", ""],
  ];
  $("st-stats").innerHTML = cells.map(([k, v, c]) =>
    `<div class="metric"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`).join("");

  const bits = [];
  if (ret != null) {
    bits.push(`${ret >= 0 ? "Up" : "Down"} ${Math.abs(ret * 100).toFixed(1)}% over ${RANGE_NAME[stRange]}`);
    if (gap != null) {
      bits.push(gap >= 0
        ? `ahead of the S&P 500 by about ${Math.abs(gap).toFixed(0)} points`
        : `behind the S&P 500 by about ${Math.abs(gap).toFixed(0)} points`);
    }
  }
  const px = stQuote ? stQuote.last_price : null;
  if (px != null && stMaNow) {
    const { sma50, sma200 } = stMaNow;
    const over50 = sma50 ? px >= sma50 : null, over200 = sma200 ? px >= sma200 : null;
    if (over50 != null && over200 != null) {
      bits.push("trading " + (over50 && over200 ? "above both its 50- and 200-day averages"
        : over200 ? "above its 200-day average but under its 50-day"
        : over50 ? "above its 50-day average but under its 200-day"
        : "below both its 50- and 200-day averages"));
    }
  }
  $("st-explain").textContent = bits.length
    ? bits.join(", ") + ". Switch to the advanced view for indicators, risk statistics, " +
      "the options chain and peer comparison."
    : "";
}

async function loadStockProfile() {
  try {
    const d = await api(`/api/v1/equity/profile?symbol=${encodeURIComponent(stSym)}`);
    const p = one(d.results) || {};
    const facts = [
      p.sector && `<span class="badge">${escapeHtml(p.sector)}</span>`,
      p.industry && `<span class="badge">${escapeHtml(p.industry)}</span>`,
      p.country && `<span class="badge">${escapeHtml(p.country)}</span>`,
    ].filter(Boolean).join(" ");
    const desc = p.description ? escapeHtml(String(p.description).slice(0, 600)) +
      (String(p.description).length > 600 ? "…" : "") : "No description available.";
    const extras = [
      p.employees != null && `${Number(p.employees).toLocaleString()} employees`,
      p.website && `<a href="${escapeHtml(p.website)}" target="_blank" rel="noopener">${escapeHtml(p.website)}</a>`,
    ].filter(Boolean).join(" · ");
    $("st-about").innerHTML = `<div style="margin-bottom:10px">${facts}</div>
      <p class="explain" style="margin-top:0">${desc}</p>
      <div class="dim" style="margin-top:10px">${extras}</div>`;
  } catch (e) { $("st-about").innerHTML = `<div class="empty">No profile available (${escapeHtml(e.message)})</div>`; }
}

async function loadStockAnalyst() {
  try {
    const d = await api(`/api/v1/equity/estimates/consensus?symbol=${encodeURIComponent(stSym)}`);
    const c = one(d.results);
    if (!c || (!c.recommendation && c.target_mean == null)) throw new Error("no coverage");
    const rec = String(c.recommendation || "none").replace(/_/g, " ");
    const recColor = /buy/i.test(rec) ? "var(--green)" : /sell/i.test(rec) ? "var(--red)" : "#ffd60a";
    let target = "";
    if (c.target_mean != null && c.current_price != null && c.current_price > 0) {
      const upside = c.target_mean / c.current_price - 1;
      target = `Average price target <b class="mono">$${c.target_mean.toFixed(2)}</b> — ` +
        `<b class="${cls(upside)}">${fmtPct(upside, true)}</b> vs today. ` +
        `Range $${(c.target_low ?? 0).toFixed(0)}–$${(c.target_high ?? 0).toFixed(0)}.`;
    }
    $("st-analyst").innerHTML = `
      <div style="font-size:18px;font-weight:700;text-transform:uppercase;color:${recColor};margin-bottom:6px">${escapeHtml(rec)}</div>
      <p class="explain" style="margin-top:0">${target}</p>
      <div class="dim">${c.analyst_count != null ? c.analyst_count + " analysts covering" : ""}</div>`;
  } catch (e) { $("st-analyst").innerHTML = `<div class="empty">No analyst coverage for ${escapeHtml(stSym)}.</div>`; }
}

const RATING_ACTIONS = {
  up: ["Upgrade", "pos"], down: ["Downgrade", "neg"], init: ["Initiated", ""],
  main: ["Maintains", "dim"], reit: ["Reiterates", "dim"],
};

async function loadStockRatings() {
  try {
    const d = await api(`/api/v1/equity/estimates/upgrades_downgrades?symbol=${encodeURIComponent(stSym)}&limit=1000`);
    const rows = d.results || [];
    if (!rows.length) throw new Error("no ratings");
    const render = (n) => {
      const body = rows.slice(0, n).map((r) => {
        const [label, klass] = RATING_ACTIONS[r.Action] || [r.Action || "-", ""];
        const grade = r.FromGrade && r.FromGrade !== r.ToGrade
          ? `<span class="dim">${escapeHtml(r.FromGrade)} →</span> ${escapeHtml(r.ToGrade || "-")}`
          : escapeHtml(r.ToGrade || "-");
        const cur = r.currentPriceTarget > 0 ? "$" + r.currentPriceTarget.toFixed(2).replace(/\.00$/, "") : null;
        const prior = r.priorPriceTarget > 0 ? "$" + r.priorPriceTarget.toFixed(2).replace(/\.00$/, "") : null;
        const tgtClass = r.priceTargetAction === "Raises" ? "pos" : r.priceTargetAction === "Lowers" ? "neg" : "";
        const target = cur
          ? (prior && prior !== cur ? `<span class="dim">${prior} →</span> <span class="${tgtClass}">${cur}</span>` : cur)
          : "-";
        return `<tr><td class="dim">${String(r.date || "").slice(0, 10)}</td>
          <td>${escapeHtml(r.Firm || "-")}</td>
          <td class="${klass}">${escapeHtml(label)}</td>
          <td>${grade}</td>
          <td class="mono">${target}</td></tr>`;
      }).join("");
      $("st-ratings").innerHTML =
        `<table><tr><th>Date</th><th>Firm</th><th>Action</th><th>Rating</th><th>Price target</th></tr>${body}</table>` +
        (rows.length > n
          ? `<button class="chip" id="st-ratings-more" style="margin-top:10px">Show all ${rows.length} actions</button>`
          : "");
      const more = $("st-ratings-more");
      if (more) more.onclick = () => render(rows.length);
    };
    render(25);
  } catch (e) { $("st-ratings").innerHTML = `<div class="empty">No public rating actions for ${escapeHtml(stSym)}.</div>`; }
}

async function loadStockFinancials() {
  try {
    const d = await api(`/api/v1/equity/fundamental/income?symbol=${encodeURIComponent(stSym)}&period=annual&limit=4`);
    const rows = (d.results || []).slice().reverse(); // newest first
    if (!rows.length) throw new Error("no filings");
    $("st-fin").innerHTML = `<table><tr><th>Fiscal year</th><th>Revenue</th><th>Net income</th><th>Margin</th></tr>` +
      rows.map((r) => {
        const margin = r.revenue && r.net_income != null ? (r.net_income / r.revenue) : null;
        return `<tr><td>${String(r.period_ending || "").slice(0, 10)}</td>
          <td>${fmtBig(r.revenue)}</td>
          <td class="${cls(r.net_income ?? 0)}">${fmtBig(r.net_income)}</td>
          <td>${margin != null ? (margin * 100).toFixed(1) + "%" : "-"}</td></tr>`;
      }).join("") + "</table>";
  } catch (e) { $("st-fin").innerHTML = `<div class="empty">No filed financials — indexes, ETFs and crypto do not file with the SEC.</div>`; }
}

async function loadStockNews() {
  try {
    const d = await api(`/api/v1/news/company?symbol=${encodeURIComponent(stSym)}&limit=6`);
    const rows = d.results || [];
    if (!rows.length) throw new Error("no stories");
    $("st-news").innerHTML = rows.map((r) => `
      <div class="feed-item">
        <div class="feed-meta">
          ${r.source ? `<span class="src-badge">${escapeHtml(String(r.source))}</span>` : ""}
          <span>${timeAgo(r.date)}</span>
        </div>
        ${r.url
          ? `<a class="feed-title" href="${escapeHtml(String(r.url))}" target="_blank" rel="noopener">${escapeHtml(String(r.title || ""))}</a>`
          : `<span class="feed-title">${escapeHtml(String(r.title || ""))}</span>`}
      </div>`).join("");
  } catch (e) { $("st-news").innerHTML = `<div class="empty">No recent stories found.</div>`; }
}

async function updateWatchButton() {
  const btn = $("st-watch");
  btn.disabled = false;
  try {
    const w = await ensureMarketWatchlist();
    const watching = w.items.some((i) => i.symbol === stSym);
    btn.textContent = watching ? "✓ Watching" : "☆ Watch";
    btn.classList.toggle("chip", false);
    btn.style.color = watching ? "var(--green)" : "";
    btn.style.borderColor = watching ? "var(--green)" : "";
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        if (watching) {
          await api(`/api/user/watchlists/${w.id}/items/${stSym}`, { method: "DELETE" });
        } else {
          await api(`/api/user/watchlists/${w.id}/items`, { method: "POST", body: { symbol: stSym } });
        }
        marketWatchlist = await api(`/api/user/watchlists/${w.id}`);
        loadWatchCards();
        updateWatchButton();
      } catch (e) { setStatus("ERR: " + e.message); btn.disabled = false; }
    };
  } catch { btn.textContent = "☆ Watch"; }
}

// ---------- stock page: advanced mode ----------
// A denser presentation of the same symbol. Every panel below loads and fails
// on its own — a symbol with no listed options still gets its chart, and an
// index with no filings still gets its risk statistics.
//
// The chart is four Chart.js panes stacked on one x-axis. They stay aligned
// because each one reserves exactly the same 52px right-hand gutter for its
// price scale, so their plot areas start and end on the same pixel.
const ST_BARS = { "1M": 22, "6M": 126, "1Y": 252, "3Y": 756 };
const ST_PANES = ["st-adv-price", "st-adv-vol", "st-adv-rsi", "st-adv-macd"];
const ST_UP = "#00c805", ST_DOWN = "#ff5000", ST_FAST = "#ffd60a", ST_SLOW = "#5b9dff";
const ST_MONO = { family: "'IBM Plex Mono', monospace", size: 10 };

let stAdvSym = null;      // the symbol the advanced panels currently describe
let stSeries = [];        // OHLCV merged with the server's indicator series
let stHover = null;
let stExpiries = [], stExpiryIdx = 0, stChainLoaded = false, stBandsLoaded = false;
let stAtmIv = null, stRealisedVol = null;
// Chains are kept per expiry, so widening the ladder or switching to the greek
// columns re-renders what is already here instead of hitting the provider again.
let stChainRows = {}, stChainSpan = 12, stChainView = "quotes", stExpiryShowAll = false;
let stHoverFrame = null;

function stResizeCharts() {
  ["st-chart", "st-cmp-chart", ...ST_PANES].forEach((id) => {
    if (charts[id]) charts[id].resize();
  });
}

function resetStockAdvanced() {
  stAdvSym = null; stSeries = []; stHover = null;
  stExpiries = []; stExpiryIdx = 0; stChainLoaded = false; stBandsLoaded = false;
  stAtmIv = null; stRealisedVol = null;
  stChainRows = {}; stChainSpan = 12; stChainView = "quotes"; stExpiryShowAll = false;
  stCurrentMultiples = {}; stMultipleSeries = null;
  if (charts["st-metric-chart"]) { charts["st-metric-chart"].destroy(); delete charts["st-metric-chart"]; }
  if ($("st-metric-dialog").open) $("st-metric-dialog").close();
  ST_PANES.forEach((id) => { if (charts[id]) { charts[id].destroy(); delete charts[id]; } });
  $("st-keystats").innerHTML = `<div class="empty" style="padding:12px 16px">Loading…</div>`;
  $("st-signals").innerHTML = "";
  $("st-readout").innerHTML = "";
  $("st-dateaxis").innerHTML = "";
  $("st-expiries").innerHTML = "";
  $("st-chain-ctl").innerHTML = "";
  ["st-risk", "st-capm", "st-val", "st-chain", "st-peers", "st-funds", "st-senti"].forEach((id) => {
    $(id).innerHTML = `<div class="empty">Loading…</div>`;
  });
  $("st-advnews").innerHTML = "";
}

function openStockAdvanced() {
  if (stAdvSym === stSym) { stResizeCharts(); return; }
  stAdvSym = stSym;
  renderStockKeyStats();
  loadAdvSeries();
  loadAdvKeyStats();
  loadAdvRisk();
  loadAdvValuation();
  loadAdvChain();
  loadAdvPeers();
  loadAdvFundamentals();
  loadAdvSentiment();
  loadAdvEarnings();
}

// --- series -----------------------------------------------------------------
// One pass over the widest window the ranges can ask for, plus enough warm-up
// that the 200-day average already has a value on the first bar a 3Y view
// shows. Switching ranges after this is a slice, not a fetch.
const ST_SERIES_DAYS = RANGE_DAYS["3Y"] + 320;

const stTechnical = (path, params) =>
  api(`/api/v1/technical/${path}?symbol=${encodeURIComponent(stSym)}` +
      `&start_date=${isoAgo(ST_SERIES_DAYS)}&limit=4000${params || ""}`)
    .then((d) => d.results || []).catch(() => []);

const stByDate = (rows, pick) => {
  const map = new Map();
  (rows || []).forEach((r) => { if (r.date) map.set(String(r.date).slice(0, 10), pick(r)); });
  return map;
};

async function loadAdvSeries() {
  const symbolAtStart = stSym;
  try {
    const [hist, sma50, sma200, rsi, macd] = await Promise.all([
      api(`/api/data/history/${encodeURIComponent(stSym)}?start=${isoAgo(ST_SERIES_DAYS)}`),
      stTechnical("sma", "&length=50"),
      stTechnical("sma", "&length=200"),
      stTechnical("rsi", "&length=14"),
      stTechnical("macd"),
    ]);
    if (symbolAtStart !== stSym) return;
    const m50 = stByDate(sma50, (r) => r.sma_50);
    const m200 = stByDate(sma200, (r) => r.sma_200);
    const mRsi = stByDate(rsi, (r) => r.rsi_14);
    const mMacd = stByDate(macd, (r) => [r.macd, r.macd_signal, r.macd_histogram]);
    stSeries = (hist.bars || []).map((b) => {
      const md = mMacd.get(b.date) || [null, null, null];
      return {
        date: b.date, o: b.open, h: b.high, l: b.low, c: b.close, v: b.volume,
        sma50: m50.get(b.date) ?? null, sma200: m200.get(b.date) ?? null,
        rsi: mRsi.get(b.date) ?? null,
        macd: md[0], macdSignal: md[1], macdHist: md[2],
        bbUpper: null, bbLower: null,
      };
    });
    if (!stSeries.length) throw new Error("no price history");
    if (stOverlays.bollinger) await loadAdvBands();
    renderAdvChart();
    renderStockSignals();
  } catch (e) {
    $("st-readout").innerHTML = `<span class="errbox">${escapeHtml(e.message)}</span>`;
  }
}

// Bollinger is the one overlay nobody sees until they ask for it, so it is the
// one series fetched on demand rather than up front.
async function loadAdvBands() {
  if (stBandsLoaded || !stSeries.length) return;
  const rows = await stTechnical("bbands", "&length=20&std=2");
  const upper = stByDate(rows, (r) => r.bb_upper);
  const lower = stByDate(rows, (r) => r.bb_lower);
  stSeries.forEach((b) => {
    b.bbUpper = upper.get(b.date) ?? null;
    b.bbLower = lower.get(b.date) ?? null;
  });
  stBandsLoaded = true;
}

const stView = () => stSeries.slice(-ST_BARS[stAdvRange]);

// --- chart plugins ----------------------------------------------------------
// Candles, guide lines and the crosshair are painted by hand: Chart.js has no
// OHLC type, and drawing the crosshair ourselves keeps it pinned to the cursor
// without a data update behind it.
const stCandles = {
  id: "stCandles",
  beforeDatasetsDraw(chart) {
    const bars = chart.$bars;
    if (!bars || !bars.length || chart.$ctype !== "candles") return;
    const { ctx, chartArea: area, scales } = chart;
    const step = (area.right - area.left) / bars.length;
    const body = Math.max(step * 0.64, 1);
    ctx.save();
    ctx.beginPath();
    ctx.rect(area.left, area.top, area.right - area.left, area.bottom - area.top);
    ctx.clip();
    ctx.lineWidth = 1;
    bars.forEach((b, i) => {
      const cx = scales.x.getPixelForValue(i);
      const tone = b.c >= b.o ? ST_UP : ST_DOWN;
      ctx.strokeStyle = tone; ctx.fillStyle = tone;
      const wick = Math.round(cx) + 0.5;
      ctx.beginPath();
      ctx.moveTo(wick, scales.y.getPixelForValue(b.h));
      ctx.lineTo(wick, scales.y.getPixelForValue(b.l));
      ctx.stroke();
      const top = scales.y.getPixelForValue(Math.max(b.o, b.c));
      const height = Math.max(Math.abs(scales.y.getPixelForValue(b.o) - scales.y.getPixelForValue(b.c)), 1);
      ctx.fillRect(cx - body / 2, top, body, height);
    });
    ctx.restore();
  },
};

const stGuides = {
  id: "stGuides",
  beforeDatasetsDraw(chart) {
    const guides = chart.$guides;
    if (!guides) return;
    const { ctx, chartArea: area, scales } = chart;
    ctx.save();
    ctx.lineWidth = 1;
    guides.forEach((g) => {
      const y = Math.round(scales.y.getPixelForValue(g.value)) + 0.5;
      ctx.strokeStyle = "#2c3031";
      ctx.setLineDash(g.dash ? [3, 3] : []);
      ctx.beginPath();
      ctx.moveTo(area.left, y); ctx.lineTo(area.right, y); ctx.stroke();
    });
    ctx.restore();
  },
};

const stCrosshair = {
  id: "stCrosshair",
  afterDatasetsDraw(chart) {
    const bars = chart.$bars;
    if (stHover == null || !bars || stHover >= bars.length) return;
    const { ctx, chartArea: area, scales } = chart;
    ctx.save();
    ctx.setLineDash([2, 3]);
    ctx.strokeStyle = "#82868a";
    ctx.lineWidth = 1;
    const x = Math.round(scales.x.getPixelForValue(stHover)) + 0.5;
    ctx.beginPath(); ctx.moveTo(x, area.top); ctx.lineTo(x, area.bottom); ctx.stroke();
    if (chart.$priceCrosshair) {
      const y = Math.round(scales.y.getPixelForValue(bars[stHover].c)) + 0.5;
      ctx.beginPath(); ctx.moveTo(area.left, y); ctx.lineTo(area.right, y); ctx.stroke();
    }
    ctx.restore();
  },
};

// Every pane hands its vertical scale the same width, which is what keeps the
// four x-axes on the same pixels.
const stGutter = (scale) => { scale.width = 52; };

function stPaneScales(yScale) {
  return {
    x: { type: "category", display: false, offset: true, grid: { display: false } },
    y: { position: "right", border: { display: false }, ...yScale, afterFit: stGutter },
  };
}

const ST_PANE_BASE = {
  animation: false, maintainAspectRatio: false, responsive: true,
  layout: { padding: 0 },
  plugins: { legend: { display: false }, tooltip: { enabled: false } },
  interaction: { mode: null },
};

function stMake(id, config, bars) {
  if (charts[id]) charts[id].destroy();
  const chart = new Chart($(id), config);
  chart.$bars = bars;
  charts[id] = chart;
  return chart;
}

function renderAdvChart() {
  const view = stView();
  if (!view.length) return;
  const labels = view.map((b) => b.date);
  const line = stChartType === "line";
  const bands = stOverlays.bollinger;

  // --- price ---
  let lo = Infinity, hi = -Infinity;
  view.forEach((b) => {
    lo = Math.min(lo, b.l); hi = Math.max(hi, b.h);
    if (bands && b.bbUpper != null) { hi = Math.max(hi, b.bbUpper); lo = Math.min(lo, b.bbLower); }
  });
  const pad = (hi - lo) * 0.04 || 1;
  const overlay = (label, key, color, dash) => ({
    label, data: view.map((b) => b[key]), borderColor: color, borderWidth: dash ? 1 : 1.2,
    borderDash: dash ? [3, 3] : [], pointRadius: 0, spanGaps: false, fill: false, tension: 0,
  });
  const priceSets = [{
    label: "Close", data: view.map((b) => b.c), borderColor: ST_UP,
    borderWidth: line ? 1.6 : 0, pointRadius: 0, tension: 0,
    fill: line ? "origin" : false, backgroundColor: "rgba(0,200,5,.08)",
  }];
  if (stOverlays.sma50) priceSets.push(overlay("SMA 50", "sma50", ST_FAST));
  if (stOverlays.sma200) priceSets.push(overlay("SMA 200", "sma200", ST_SLOW));
  if (bands) {
    priceSets.push(overlay("BB upper", "bbUpper", "#4a4e50", true));
    priceSets.push(overlay("BB lower", "bbLower", "#4a4e50", true));
  }
  const price = stMake("st-adv-price", {
    type: "line",
    data: { labels, datasets: priceSets },
    options: {
      ...ST_PANE_BASE,
      scales: stPaneScales({
        min: lo - pad, max: hi + pad,
        grid: { color: "#1a1d1e", drawTicks: false },
        // Five evenly spaced labels rather than Chart.js's round numbers: the
        // gutter reads as a scale of this window, top and bottom included.
        afterBuildTicks: (scale) => {
          const step = (scale.max - scale.min) / 4;
          scale.ticks = [0, 1, 2, 3, 4].map((i) => ({ value: scale.max - i * step }));
        },
        ticks: { color: "#6f7377", font: ST_MONO, padding: 6,
                 callback: (v) => Number(v).toFixed(2) },
      }),
    },
    plugins: [stCandles, stCrosshair],
  }, view);
  price.$ctype = stChartType;
  price.$priceCrosshair = true;

  // --- volume ---
  const peak = Math.max(...view.map((b) => b.v || 0)) || 1;
  stMake("st-adv-vol", {
    type: "bar",
    data: {
      labels,
      datasets: [{
        data: view.map((b) => b.v), borderWidth: 0,
        backgroundColor: view.map((b) => (b.c >= b.o ? "rgba(0,200,5,.45)" : "rgba(255,80,0,.45)")),
        barPercentage: 0.64, categoryPercentage: 1,
      }],
    },
    options: {
      ...ST_PANE_BASE,
      scales: stPaneScales({
        min: 0, max: peak, grid: { display: false },
        afterBuildTicks: (scale) => { scale.ticks = [{ value: peak }]; },
        ticks: { color: "#6f7377", font: ST_MONO, padding: 6, callback: (v) => fmtBig(v) },
      }),
    },
    plugins: [stCrosshair],
  }, view);

  // --- RSI ---
  const rsi = stMake("st-adv-rsi", {
    type: "line",
    data: {
      labels,
      datasets: [{ data: view.map((b) => b.rsi), borderColor: "#c9cccd", borderWidth: 1.3,
                   pointRadius: 0, spanGaps: false, tension: 0, fill: false }],
    },
    options: {
      ...ST_PANE_BASE,
      scales: stPaneScales({
        min: 0, max: 100, grid: { display: false },
        afterBuildTicks: (scale) => { scale.ticks = [{ value: 70 }, { value: 30 }]; },
        ticks: { color: "#6f7377", font: ST_MONO, padding: 6, callback: (v) => String(v) },
      }),
    },
    plugins: [stGuides, stCrosshair],
  }, view);
  rsi.$guides = [{ value: 70, dash: true }, { value: 30, dash: true }];

  // --- MACD ---
  let span = 0;
  view.forEach((b) => {
    [b.macd, b.macdSignal, b.macdHist].forEach((v) => { if (v != null) span = Math.max(span, Math.abs(v)); });
  });
  span = span || 1;
  const macd = stMake("st-adv-macd", {
    type: "bar",
    data: {
      labels,
      datasets: [
        { type: "bar", data: view.map((b) => b.macdHist), borderWidth: 0,
          backgroundColor: view.map((b) => ((b.macdHist ?? 0) >= 0 ? "rgba(0,200,5,.5)" : "rgba(255,80,0,.5)")),
          barPercentage: 0.6, categoryPercentage: 1, order: 3 },
        { type: "line", data: view.map((b) => b.macd), borderColor: ST_SLOW, borderWidth: 1.2,
          pointRadius: 0, spanGaps: false, tension: 0, fill: false, order: 1 },
        { type: "line", data: view.map((b) => b.macdSignal), borderColor: ST_FAST, borderWidth: 1.2,
          pointRadius: 0, spanGaps: false, tension: 0, fill: false, order: 2 },
      ],
    },
    options: {
      ...ST_PANE_BASE,
      scales: stPaneScales({
        min: -span, max: span, grid: { display: false },
        ticks: { display: false },
      }),
    },
    plugins: [stGuides, stCrosshair],
  }, view);
  macd.$guides = [{ value: 0, dash: false }];

  // Chart.js paints once inside its own constructor, before the fields the
  // candle, guide and crosshair plugins read have been attached. One redraw
  // once every pane exists is what puts them on screen.
  ST_PANES.forEach((id) => { if (charts[id]) charts[id].render(); });

  renderAdvDateAxis(view);
  renderAdvReadout();
}

function renderAdvDateAxis(view) {
  const at = (f) => view[Math.min(view.length - 1, Math.round(f * (view.length - 1)))];
  $("st-dateaxis").innerHTML = [0, 0.25, 0.5, 0.75, 1]
    .map((f) => `<span>${escapeHtml(stShortDate(at(f).date))}</span>`).join("");
}

function stShortDate(iso) {
  const d = new Date(iso + "T00:00:00Z");
  if (isNaN(d)) return iso;
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "2-digit", timeZone: "UTC" });
}
function stLongDate(iso) {
  const d = new Date(iso + "T00:00:00Z");
  if (isNaN(d)) return iso;
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
}
const stNum = (v, d = 2) => (v == null || isNaN(v) ? "—" : Number(v).toFixed(d));

// The readout shows the hovered bar, or the last one when the cursor is away.
function renderAdvReadout() {
  const view = stView();
  if (!view.length) return;
  const i = stHover != null && stHover < view.length ? stHover : view.length - 1;
  const bar = view[i], prev = i > 0 ? view[i - 1] : null;
  const chg = prev && prev.c ? (bar.c - prev.c) / prev.c * 100 : null;
  const fields = [
    ["", stLongDate(bar.date), "#f5f6f7"],
    ["O", stNum(bar.o), "#c9cccd"],
    ["H", stNum(bar.h), "#c9cccd"],
    ["L", stNum(bar.l), "#c9cccd"],
    ["C", stNum(bar.c), "#f5f6f7"],
    ["CHG", chg == null ? "—" : (chg >= 0 ? "+" : "−") + Math.abs(chg).toFixed(2) + "%",
      chg == null ? "#c9cccd" : chg >= 0 ? ST_UP : ST_DOWN],
    ["VOL", fmtBig(bar.v), "#c9cccd"],
    ["SMA50", stNum(bar.sma50), ST_FAST],
    ["SMA200", stNum(bar.sma200), ST_SLOW],
  ];
  $("st-readout").innerHTML = fields.map(([k, v, color]) =>
    `<span class="r">${k ? `<span class="k">${k}</span>` : ""}<span style="color:${color}">${escapeHtml(v)}</span></span>`
  ).join("");
  $("st-rsi-hdr").textContent = `RSI 14 · ${stNum(bar.rsi, 1)}`;
  $("st-macd-hdr").textContent = `MACD 12/26/9 · ${stNum(bar.macd)} / ${stNum(bar.macdSignal)}`;
}

// --- chart interaction ------------------------------------------------------
document.querySelectorAll("#st-adv-ranges .chip").forEach((c) => {
  c.onclick = () => {
    document.querySelectorAll("#st-adv-ranges .chip").forEach((x) => x.classList.remove("active"));
    c.classList.add("active");
    stAdvRange = c.dataset.range;
    stHover = null;
    renderAdvChart();
    renderStockSignals();
    loadAdvRisk();          // the risk and CAPM panels describe this window
  };
});
document.querySelectorAll("#st-adv-types .chip").forEach((c) => {
  c.onclick = () => {
    document.querySelectorAll("#st-adv-types .chip").forEach((x) => x.classList.remove("active"));
    c.classList.add("active");
    stChartType = c.dataset.ctype;
    renderAdvChart();
  };
});
document.querySelectorAll("#st-adv-ovs .chip").forEach((c) => {
  c.onclick = async () => {
    const key = c.dataset.ov;
    stOverlays[key] = !stOverlays[key];
    c.classList.toggle("active", stOverlays[key]);
    if (key === "bollinger" && stOverlays.bollinger) await loadAdvBands();
    renderAdvChart();
  };
});

$("st-panes").addEventListener("mousemove", (event) => {
  const chart = charts["st-adv-price"];
  if (!chart || !stSeries.length) return;
  const rect = chart.canvas.getBoundingClientRect();
  const raw = chart.scales.x.getValueForPixel(event.clientX - rect.left);
  const last = stView().length - 1;
  const index = Math.max(0, Math.min(last, Math.round(raw)));
  if (index === stHover) return;
  stHover = index;
  stPaintHover();
});
$("st-panes").addEventListener("mouseleave", () => {
  if (stHover === null) return;
  stHover = null;
  stPaintHover();
});

// The crosshair must track the cursor 1:1, so redraws are coalesced onto the
// frame rather than fired per mousemove.
function stPaintHover() {
  if (stHoverFrame) return;
  stHoverFrame = requestAnimationFrame(() => {
    stHoverFrame = null;
    renderAdvReadout();
    ST_PANES.forEach((id) => { if (charts[id]) charts[id].render(); });
  });
}

// --- a. key-stat strip ------------------------------------------------------
let stShares = null;

function renderStockKeyStats() {
  if (!stQuote) return;
  const q = stQuote, s = stShares;
  const range = (a, b) => (a != null && b != null ? `${stNum(a)}–${stNum(b)}` : "—");
  const shortPct = s && s.shares_short && (s.float_shares || s.shares_outstanding)
    ? s.shares_short / (s.float_shares || s.shares_outstanding) * 100 : null;
  const cells = [
    ["Open", stNum(q.open), false],
    ["Day range", range(q.low, q.high), false],
    ["Prev close", stNum(q.prev_close), false],
    ["Volume", fmtBig(q.volume), false],
    ["Avg vol 3m", fmtBig(q.avg_volume), true],
    ["52w range", range(q.year_low, q.year_high), false],
    ["Mkt cap", fmtBig(q.market_cap), false],
    ["Float", s ? fmtBig(s.float_shares) : "—", true],
    ["Short int.", shortPct == null ? "—" : shortPct.toFixed(2) + "%", true],
  ];
  $("st-keystats").innerHTML = cells.map(([k, v, soft]) =>
    `<div class="kcell"><div class="k">${k}</div><div class="v${soft ? " soft" : ""}">${escapeHtml(v)}</div></div>`
  ).join("");
}

async function loadAdvKeyStats() {
  try {
    const d = await api(`/api/v1/equity/ownership/share_statistics?symbol=${encodeURIComponent(stSym)}`);
    stShares = one(d.results) || null;
  } catch { stShares = null; }
  renderStockKeyStats();
}

// --- b. signal cards --------------------------------------------------------
// Each card states what the number is and what it means. Both halves come from
// the series, so the reading cannot drift from the value above it.
let stEarnings = null;

function stSigCard(label, value, reading, tone) {
  return `<div class="sig-card ${tone}">
    <div class="sig-label">${label}</div>
    <div class="sig-value">${escapeHtml(value)}</div>
    <div class="sig-read">${escapeHtml(reading)}</div></div>`;
}

function renderStockSignals() {
  const view = stView();
  if (!view.length) return;
  const last = view[view.length - 1];
  const cards = [];

  if (last.sma200 != null && last.c != null) {
    const over = (last.c / last.sma200 - 1) * 100;
    cards.push(stSigCard("Trend", over >= 0 ? "ABOVE 200D" : "BELOW 200D",
      `Price ${Math.abs(over).toFixed(1)}% ${over >= 0 ? "over" : "under"} the 200-day average`,
      over >= 0 ? "t-pos" : "t-neg"));
  } else {
    cards.push(stSigCard("Trend", "—", "Not enough history for a 200-day average", ""));
  }

  if (last.rsi != null) {
    cards.push(stSigCard("RSI 14", last.rsi.toFixed(0),
      last.rsi > 70 ? "Overbought territory" : last.rsi < 30 ? "Oversold territory" : "Neutral, no extreme",
      last.rsi > 70 || last.rsi < 30 ? "t-warn" : ""));
  } else {
    cards.push(stSigCard("RSI 14", "—", "No relative-strength reading yet", ""));
  }

  if (last.macd != null && last.macdSignal != null) {
    const bull = last.macd > last.macdSignal;
    cards.push(stSigCard("MACD", bull ? "BULLISH X" : "BEARISH X",
      `Line ${bull ? "over" : "under"} signal`, bull ? "t-pos" : "t-neg"));
  } else {
    cards.push(stSigCard("MACD", "—", "No MACD reading yet", ""));
  }

  // Implied against realised, rather than an IV rank: one expiry of listed
  // quotes cannot say where this year's implied vol sits in its own range, but
  // it can say what the options are charging against what the stock has done.
  if (stAtmIv != null) {
    const iv = stAtmIv * 100;
    const rv = stRealisedVol != null ? stRealisedVol * 100 : null;
    const rich = rv != null ? iv - rv : null;
    cards.push(stSigCard("ATM IV", iv.toFixed(1) + "%",
      rv == null ? "At-the-money implied volatility, nearest expiry"
        : `${Math.abs(rich).toFixed(1)} pts ${rich >= 0 ? "above" : "below"} realised ${rv.toFixed(1)}%`,
      rich == null ? "" : rich >= 0 ? "t-warn" : "t-pos"));
  } else {
    cards.push(stSigCard("ATM IV", "—", "No listed options for this symbol", ""));
  }

  if (stEarnings && stEarnings.date) {
    const days = Math.round((new Date(stEarnings.date) - Date.now()) / 86400000);
    cards.push(stSigCard("Earnings",
      days >= 0 ? `IN ${days}D` : `${Math.abs(days)}D AGO`,
      `${stLongDate(String(stEarnings.date).slice(0, 10))}` +
        (stEarnings.eps_estimate != null ? ` · ${stEarnings.eps_estimate.toFixed(2)} EPS expected` : ""),
      days >= 0 && days <= 21 ? "t-warn" : ""));
  } else {
    cards.push(stSigCard("Earnings", "—", "No scheduled date published", ""));
  }

  $("st-signals").innerHTML = cards.join("");
}

async function loadAdvEarnings() {
  try {
    const d = await api(`/api/v1/equity/fundamental/earnings?symbol=${encodeURIComponent(stSym)}&limit=8`);
    const rows = (d.results || []).filter((r) => r.date);
    const now = Date.now();
    const upcoming = rows.filter((r) => new Date(r.date) >= now)
      .sort((a, b) => new Date(a.date) - new Date(b.date));
    const past = rows.filter((r) => new Date(r.date) < now)
      .sort((a, b) => new Date(b.date) - new Date(a.date));
    stEarnings = upcoming[0] || past[0] || null;
  } catch { stEarnings = null; }
  renderStockSignals();
}

// --- c. risk / CAPM / valuation rail ---------------------------------------
const stKvRows = (rows) => rows.map(([k, v, tone]) =>
  `<div class="r"><span class="k">${k}</span><span class="v ${tone || ""}">${escapeHtml(v)}</span></div>`).join("");

const stPct1 = (x) => (x == null ? "—" : (x >= 0 ? "+" : "−") + Math.abs(x * 100).toFixed(1) + "%");
const stPct2 = (x) => (x == null ? "—" : (x * 100).toFixed(1) + "%");

async function loadAdvRisk() {
  const window = `${stAdvRange} window`;
  $("st-risk-note").textContent = window;
  $("st-capm-note").textContent = "vs SPY · " + window;
  const start = isoAgo(RANGE_DAYS[stAdvRange]);
  const seq = ++loadAdvRisk.seq;

  api(`/api/v1/quantitative/performance?symbol=${encodeURIComponent(stSym)}&start_date=${start}`)
    .then((d) => {
      if (seq !== loadAdvRisk.seq) return;
      const r = one(d.results);
      if (!r) throw new Error("no observations");
      stRealisedVol = r.annualised_volatility ?? null;
      $("st-risk").innerHTML = stKvRows([
        ["CAGR", stPct1(r.cagr), cls(r.cagr ?? 0)],
        ["Total return", stPct1(r.total_return), cls(r.total_return ?? 0)],
        ["Ann. volatility", stPct2(r.annualised_volatility)],
        ["Sharpe", stNum(r.sharpe), r.sharpe == null ? "" : cls(r.sharpe)],
        ["Sortino", stNum(r.sortino), r.sortino == null ? "" : cls(r.sortino)],
        ["Max drawdown", stPct1(r.max_drawdown), "neg"],
        ["VaR 95%", stPct1(r.value_at_risk), "neg"],
        ["CVaR 95%", stPct1(r.conditional_var), "neg"],
        ["Win rate", stPct2(r.win_rate)],
      ]);
      renderStockSignals();
    })
    .catch((e) => {
      if (seq !== loadAdvRisk.seq) return;
      $("st-risk").innerHTML = `<div class="empty">No risk statistics for ${escapeHtml(stSym)} over ${stAdvRange} (${escapeHtml(e.message)}).</div>`;
    });

  api(`/api/v1/quantitative/capm?symbol=${encodeURIComponent(stSym)}&benchmark=SPY&start_date=${start}`)
    .then((d) => {
      if (seq !== loadAdvRisk.seq) return;
      const r = one(d.results);
      if (!r) throw new Error("no overlap");
      $("st-capm").innerHTML = stKvRows([
        ["Beta", stNum(r.beta)],
        ["Alpha (ann.)", stPct1(r.alpha_annualised), cls(r.alpha_annualised ?? 0)],
        ["R²", stNum(r.r_squared)],
        ["Correlation", stNum(r.correlation)],
        ["Tracking error", stPct2(r.tracking_error)],
        ["Observations", String(r.observations ?? "—")],
      ]);
    })
    .catch(() => {
      if (seq !== loadAdvRisk.seq) return;
      $("st-capm").innerHTML = `<div class="empty">Not enough overlap with SPY to estimate a beta.</div>`;
    });
}
loadAdvRisk.seq = 0;

// Every multiple here is a price over a fundamental, and both move. The current
// number alone cannot say whether 34x is this company's normal or its extreme,
// so each row opens its own history.
// `kind` decides both the sentence and which end of the range is the expensive
// one: a high multiple is dear, a high yield is cheap, and earnings are neither.
const ST_MULTIPLES = {
  pe_trailing: { label: "P/E trailing", unit: "x", digits: 1, kind: "multiple" },
  forward_pe: { label: "P/E forward", unit: "x", digits: 1, kind: "multiple", proxy: "pe_trailing" },
  ev_ebitda: { label: "EV/EBITDA", unit: "x", digits: 1, kind: "multiple" },
  ps_trailing: { label: "P/S", unit: "x", digits: 1, kind: "multiple" },
  eps_ttm: { label: "EPS (TTM)", unit: "$", digits: 2, kind: "level" },
  fcf_yield: { label: "FCF yield", unit: "%", digits: 2, kind: "yield" },
  dividend_yield: { label: "Dividend", unit: "%", digits: 2, kind: "yield" },
};
const stMultipleValue = (key, v) => {
  if (v == null || isNaN(v)) return "—";
  const spec = ST_MULTIPLES[key];
  if (spec.unit === "%") return (v * 100).toFixed(spec.digits) + "%";
  if (spec.unit === "$") return "$" + Number(v).toFixed(spec.digits);
  return Number(v).toFixed(spec.digits) + "x";
};

async function loadAdvValuation() {
  try {
    const d = await api(`/api/v1/equity/fundamental/metrics?symbol=${encodeURIComponent(stSym)}`);
    const m = one(d.results);
    if (!m) throw new Error("no metrics");
    stCurrentMultiples = {
      pe_trailing: m.trailing_pe, forward_pe: m.forward_pe, ev_ebitda: m.ev_to_ebitda,
      ps_trailing: m.price_to_sales,
      eps_ttm: m.trailing_pe && stQuote && stQuote.last_price ? stQuote.last_price / m.trailing_pe : null,
      fcf_yield: m.free_cash_flow != null && m.market_cap ? m.free_cash_flow / m.market_cap : null,
      // Yahoo publishes this one already in percent.
      dividend_yield: m.dividend_yield == null ? null : m.dividend_yield / 100,
    };
    $("st-val").innerHTML = Object.keys(ST_MULTIPLES).map((key) => {
      const value = stMultipleValue(key, stCurrentMultiples[key]);
      return `<div class="r clickable" data-metric="${key}" role="button" tabindex="0"
        title="${escapeHtml(ST_MULTIPLES[key].label)} over time">
        <span class="k">${ST_MULTIPLES[key].label}<span class="hist mono">HISTORY ›</span></span>
        <span class="v">${value}</span></div>`;
    }).join("");
    $("st-val").querySelectorAll(".r.clickable").forEach((row) => {
      const open = () => openMultipleHistory(row.dataset.metric);
      row.onclick = open;
      row.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
    });
  } catch {
    $("st-val").innerHTML = `<div class="empty">No valuation multiples — indexes, ETFs and crypto do not carry them.</div>`;
  }
}

// --- valuation history ------------------------------------------------------
let stCurrentMultiples = {};
let stMultipleSeries = null;   // { symbol, rows, extra } for the open symbol
let stMultipleKey = null, stMultipleYears = 5;

const ST_MULTIPLE_DAYS = 366 * 5 + 10;

async function loadMultipleSeries() {
  if (stMultipleSeries && stMultipleSeries.symbol === stSym) return stMultipleSeries;
  const d = await api(`/api/v1/equity/fundamental/multiples_history?symbol=${encodeURIComponent(stSym)}` +
                      `&start_date=${isoAgo(ST_MULTIPLE_DAYS)}`);
  stMultipleSeries = { symbol: stSym, rows: d.results || [], extra: d.extra || {} };
  return stMultipleSeries;
}

async function openMultipleHistory(key) {
  stMultipleKey = key;
  stMultipleYears = 5;
  document.querySelectorAll("#st-metric-ranges .chip").forEach((c) =>
    c.classList.toggle("active", Number(c.dataset.years) === stMultipleYears));
  const spec = ST_MULTIPLES[key];
  $("st-metric-title").textContent = `${spec.label} · ${stSym}`;
  $("st-metric-sub").textContent = "Loading…";
  $("st-metric-stats").innerHTML = "";
  $("st-metric-note").textContent = "";
  if (charts["st-metric-chart"]) { charts["st-metric-chart"].destroy(); delete charts["st-metric-chart"]; }
  $("st-metric-dialog").showModal();
  try {
    await loadMultipleSeries();
    renderMultipleHistory();
  } catch (e) {
    $("st-metric-sub").textContent = "";
    $("st-metric-note").textContent =
      `No history available for ${stSym} — ${e.message}. Indexes, ETFs and crypto do not file the ` +
      `statements these ratios are built from.`;
  }
}

function renderMultipleHistory() {
  const spec = ST_MULTIPLES[stMultipleKey];
  // Forward P/E has no history anywhere in this stack: a forward multiple is a
  // price over an estimate, and nothing archives the estimate. Rather than
  // invent one, plot the trailing series and mark today's forward reading.
  const field = spec.proxy || stMultipleKey;
  const all = stMultipleSeries.rows;
  const cutoff = isoAgo(Math.round(stMultipleYears * 365.25));
  const rows = all.filter((r) => r.date >= cutoff && r[field] != null && isFinite(r[field]));

  // With nothing to plot, the chips and the empty canvas are just furniture
  // around an apology — collapse them and leave the reason.
  const empty = !rows.length;
  $("st-metric-ranges").style.display = empty ? "none" : "";
  document.querySelector(".metric-chartwrap").style.display = empty ? "none" : "";
  if (empty) {
    $("st-metric-sub").textContent = "";
    $("st-metric-stats").innerHTML = "";
    if (charts["st-metric-chart"]) { charts["st-metric-chart"].destroy(); delete charts["st-metric-chart"]; }
    $("st-metric-note").textContent =
      `${spec.label} cannot be built for ${stSym}: the quarterly filings this reads do not tag ` +
      `every figure it needs. The current value in the panel still comes from the live quote.`;
    return;
  }

  const values = rows.map((r) => r[field]);
  const sorted = values.slice().sort((a, b) => a - b);
  const at = (q) => sorted[Math.min(sorted.length - 1, Math.floor(q * (sorted.length - 1)))];
  const median = at(0.5), low = sorted[0], high = sorted[sorted.length - 1];
  const latest = values[values.length - 1];
  const below = sorted.filter((v) => v <= latest).length;
  const percentile = Math.round(below / sorted.length * 100);
  const forward = spec.proxy ? stCurrentMultiples[stMultipleKey] : null;

  $("st-metric-sub").textContent =
    `${rows.length.toLocaleString()} trading days · trailing twelve months · ` +
    `filings held back ${stMultipleSeries.extra.lag_days ?? 45} days until they were public`;

  // A multiple in its top decile is dear; a yield in its top decile is the
  // opposite, so the colour follows the meaning rather than the number.
  const rich = spec.kind === "multiple" ? percentile : 100 - percentile;
  const cells = [
    ["Latest", stMultipleValue(stMultipleKey, latest), ""],
    [`${stMultipleYears}Y median`, stMultipleValue(stMultipleKey, median), ""],
    ["Low", stMultipleValue(stMultipleKey, low), ""],
    ["High", stMultipleValue(stMultipleKey, high), ""],
    ["Percentile", percentile + "%",
      spec.kind === "level" ? "" : rich >= 80 ? "neg" : rich <= 20 ? "pos" : ""],
  ];
  $("st-metric-stats").innerHTML = cells.map(([k, v, c]) =>
    `<div class="metric"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`).join("");

  const scale = spec.unit === "%" ? 100 : 1;
  const datasets = [{
    label: spec.label, data: values.map((v) => v * scale), borderColor: "#00c805",
    borderWidth: 1.6, pointRadius: 0, tension: 0, fill: "origin",
    backgroundColor: "rgba(0,200,5,.08)",
  }, {
    label: `${stMultipleYears}Y median`, data: values.map(() => median * scale), borderColor: "#82868a",
    borderWidth: 1, borderDash: [5, 4], pointRadius: 0, fill: false,
  }];
  if (forward != null && isFinite(forward)) {
    datasets.push({
      label: "Forward, today", data: values.map(() => forward * scale), borderColor: "#5b9dff",
      borderWidth: 1.2, borderDash: [3, 3], pointRadius: 0, fill: false,
    });
  }
  if (charts["st-metric-chart"]) charts["st-metric-chart"].destroy();
  charts["st-metric-chart"] = new Chart($("st-metric-chart"), {
    type: "line",
    data: { labels: rows.map((r) => r.date), datasets },
    options: {
      animation: false, maintainAspectRatio: false, responsive: true,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: true, labels: { color: "#c9cccd", boxWidth: 14, font: { size: 11 } } },
        tooltip: {
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: ${stMultipleValue(stMultipleKey, ctx.parsed.y / scale)}`,
          },
        },
      },
      scales: {
        x: { ticks: { color: "#6f7377", font: ST_MONO, maxTicksLimit: 7 }, grid: { color: "#191b1c" } },
        y: {
          ticks: { color: "#6f7377", font: ST_MONO,
                   callback: (v) => stMultipleValue(stMultipleKey, v / scale) },
          grid: { color: "#191b1c" },
        },
      },
    },
  });

  const shown = stMultipleValue(stMultipleKey, latest);
  const lead = spec.kind === "yield" ? `${stSym} yields ${shown}`
    : spec.kind === "level" ? `${stSym} earned ${shown} a share over the last twelve months`
    : `${stSym} trades at ${shown}`;
  const rank = percentile >= 99 ? "the highest it has been in this window"
    : percentile <= 1 ? "the lowest it has been in this window"
    : spec.kind === "multiple" ? `dearer than on ${percentile}% of the days in this window`
    : `higher than on ${percentile}% of the days in this window`;
  const notes = [
    `${lead}, ${latest < median ? "below" : "above"} its ${stMultipleYears}-year median of ` +
    `${stMultipleValue(stMultipleKey, median)} — ${rank}.`,
  ];
  if (spec.proxy) {
    notes.push(`This is the trailing series. A forward multiple divides by an estimate, and no ` +
      `part of this terminal archives what the estimate used to be, so its history cannot be ` +
      `reconstructed${forward != null ? " — today's forward reading is marked instead" : ""}.`);
  }
  $("st-metric-note").textContent = notes.join(" ");
}

document.querySelectorAll("#st-metric-ranges .chip").forEach((c) => {
  c.onclick = () => {
    document.querySelectorAll("#st-metric-ranges .chip").forEach((x) => x.classList.remove("active"));
    c.classList.add("active");
    stMultipleYears = Number(c.dataset.years);
    if (stMultipleSeries) renderMultipleHistory();
  };
});
$("st-metric-close").onclick = () => $("st-metric-dialog").close();
$("st-metric-dialog").onclick = (e) => { if (e.target === $("st-metric-dialog")) $("st-metric-dialog").close(); };

// --- d. options chain -------------------------------------------------------
// Yahoo publishes quotes and implied volatility but no greeks, so every greek
// here is Black–Scholes on the published IV, at zero rate and zero carry. The
// panel note says so — these are the shape of the risk, not a broker's book.
function stNormCdf(x) {
  const t = 1 / (1 + 0.2316419 * Math.abs(x));
  const d = 0.3989423 * Math.exp(-x * x / 2);
  const p = 1 - d * t * (1.330274 * t ** 4 - 1.821256 * t ** 3 + 1.781478 * t * t - 0.356538 * t + 0.319382);
  return x >= 0 ? p : 1 - p;
}
const stNormPdf = (x) => 0.3989423 * Math.exp(-x * x / 2);

const ST_NO_GREEKS = { delta: null, gamma: null, theta: null, vega: null };

// d1 is the only quantity the greeks need; a null return means the inputs
// cannot support a model value (no IV published, or the expiry is today).
function stGreeks(spot, strike, years, iv, isCall) {
  if (!(spot > 0 && strike > 0 && years > 0 && iv > 0)) return ST_NO_GREEKS;
  const sqrtT = Math.sqrt(years);
  const d1 = (Math.log(spot / strike) + 0.5 * iv * iv * years) / (iv * sqrtT);
  const pdf = stNormPdf(d1);
  return {
    delta: isCall ? stNormCdf(d1) : stNormCdf(d1) - 1,
    gamma: pdf / (spot * iv * sqrtT),
    theta: -spot * pdf * iv / (2 * sqrtT) / 365,  // per share per calendar day
    vega: spot * pdf * sqrtT / 100,               // per share per IV point
  };
}

// Strike windows are ±N around spot, so widening keeps the money centred.
const ST_CHAIN_SPANS = [{ label: "±6", n: 6 }, { label: "±12", n: 12 },
                        { label: "±25", n: 25 }, { label: "All", n: Infinity }];
const ST_CHAIN_VIEWS = [{ id: "quotes", label: "Quotes" }, { id: "greeks", label: "Greeks" }];
const ST_EXPIRY_PAGE = 12;   // expiry chips shown before the "+N more" affordance

async function loadAdvChain() {
  if (stChainLoaded) return;
  stChainLoaded = true;
  try {
    const d = await api(`/api/v1/derivatives/options/expirations?symbol=${encodeURIComponent(stSym)}`);
    stExpiries = d.results || [];
    if (!stExpiries.length) throw new Error("no listed expirations");
    stExpiryIdx = 0;
    renderAdvExpiries();
    renderChainControls();
    loadAdvChainRows();
  } catch (e) {
    $("st-chain").innerHTML = `<div class="empty">No listed options for ${escapeHtml(stSym)}.</div>`;
    renderStockSignals();
  }
}

function renderAdvExpiries() {
  const shown = stExpiryShowAll ? stExpiries : stExpiries.slice(0, ST_EXPIRY_PAGE);
  const hidden = stExpiries.length - shown.length;
  const chips = shown.map((e, i) =>
    `<button class="chip sm${i === stExpiryIdx ? " active" : ""}" data-i="${i}">${escapeHtml(e.expiration)}  ·  ${e.days_to_expiry}d</button>`);
  if (hidden > 0) chips.push(`<button class="chip sm" data-more="1">+${hidden} more</button>`);
  $("st-expiries").innerHTML = chips.join("");
  $("st-expiries").querySelectorAll(".chip").forEach((c) => {
    c.onclick = () => {
      if (c.dataset.more) { stExpiryShowAll = true; renderAdvExpiries(); return; }
      stExpiryIdx = Number(c.dataset.i);
      renderAdvExpiries();
      loadAdvChainRows();
    };
  });
}

function renderChainControls() {
  const chip = (active, data, label) =>
    `<button class="chip sm${active ? " active" : ""}" ${data}>${label}</button>`;
  $("st-chain-ctl").innerHTML =
    `<span class="ctl-lbl">STRIKES</span>` +
    ST_CHAIN_SPANS.map((s) => chip(s.n === stChainSpan, `data-span="${s.n}"`, s.label)).join("") +
    `<span class="ctl-lbl">SHOW</span>` +
    ST_CHAIN_VIEWS.map((v) => chip(v.id === stChainView, `data-view="${v.id}"`, v.label)).join("");
  $("st-chain-ctl").querySelectorAll(".chip").forEach((c) => {
    c.onclick = () => {
      if (c.dataset.span) stChainSpan = Number(c.dataset.span);
      else stChainView = c.dataset.view;
      renderChainControls();
      renderChainTable();
    };
  });
}

async function loadAdvChainRows() {
  const expiry = stExpiries[stExpiryIdx];
  if (!expiry) return;
  if (stChainRows[expiry.expiration]) { renderChainTable(); return; }
  $("st-chain").innerHTML = `<div class="empty">Loading…</div>`;
  const seq = ++loadAdvChainRows.seq;
  try {
    const d = await api(`/api/v1/derivatives/options/chains?symbol=${encodeURIComponent(stSym)}` +
                        `&expiration=${encodeURIComponent(expiry.expiration)}`);
    if (seq !== loadAdvChainRows.seq) return;
    stChainRows[expiry.expiration] = d.results || [];
    renderChainTable();
  } catch (e) {
    if (seq !== loadAdvChainRows.seq) return;
    $("st-chain").innerHTML = `<div class="empty">No chain for that expiry (${escapeHtml(e.message)}).</div>`;
  }
}
loadAdvChainRows.seq = 0;

// Both sides carry the same columns mirrored about the strike, so the eye reads
// outward from the money in either direction.
function renderChainTable() {
  const expiry = stExpiries[stExpiryIdx];
  const rows = expiry && stChainRows[expiry.expiration];
  if (!rows) return;
  const spot = (stQuote && stQuote.last_price) ||
    (stSeries.length ? stSeries[stSeries.length - 1].c : null);
  if (!spot) {
    $("st-chain").innerHTML = `<div class="empty">No spot price for ${escapeHtml(stSym)}.</div>`;
    return;
  }
  const calls = new Map(), puts = new Map();
  rows.forEach((r) => (r.option_type === "put" ? puts : calls).set(r.strike, r));
  const ladder = [...new Set(rows.map((r) => r.strike))].sort((a, b) => a - b);
  const atIdx = ladder.findIndex((k) => k >= spot);
  const centre = atIdx === -1 ? ladder.length : atIdx;
  const strikes = stChainSpan === Infinity ? ladder
    : ladder.slice(Math.max(0, centre - stChainSpan), centre + stChainSpan);
  if (!strikes.length) {
    $("st-chain").innerHTML = `<div class="empty">No strikes for that expiry.</div>`;
    return;
  }
  const years = Math.max(expiry.days_to_expiry, 0.5) / 365;
  const atmStrike = strikes.reduce((best, k) =>
    (Math.abs(k - spot) < Math.abs(best - spot) ? k : best), strikes[0]);

  // Everything mirrors about the strike except the price pair, which always
  // reads bid-then-ask — a bid printed to the right of its own ask is a misread
  // waiting to happen.
  const greeks = stChainView === "greeks";
  const leadHeads = greeks ? ["IV", "Δ", "Γ", "Θ", "ν"] : ["IV", "Δ", "Vol", "OI"];
  const priceHeads = greeks ? ["Mid"] : ["Bid", "Ask"];
  const big = (v) => (v == null || isNaN(v) ? "—" : fmtBig(v));
  const mid = (r) => (r.bid != null && r.ask != null ? (r.bid + r.ask) / 2 : null);

  const side = (r, g, itm, tone, isCall) => {
    const lead = greeks
      ? [stPct2(r.implied_volatility), stNum(g.delta), stNum(g.gamma, 4),
         stNum(g.theta, 3), stNum(g.vega, 3)]
      : [stPct2(r.implied_volatility), stNum(g.delta), big(r.volume), big(r.open_interest)];
    const price = greeks ? [stNum(mid(r))] : [stNum(r.bid), stNum(r.ask)];
    // Every cell on a side is a handle for that side's contract, so clicking
    // anywhere along the row opens the simulator on the option you were reading.
    const sim = ` data-sim="${isCall ? "call" : "put"}"`;
    const suffix = itm ? " itm" : "";
    const leadCells = lead.map((v) => `<td class="soft${suffix}"${sim}>${escapeHtml(v)}</td>`);
    const priceCells = price.map((v) => `<td class="${tone}${suffix}"${sim}>${escapeHtml(v)}</td>`);
    return isCall ? [...leadCells, ...priceCells] : [...priceCells, ...leadCells.reverse()];
  };
  const body = strikes.map((k) => {
    const c = calls.get(k) || {}, p = puts.get(k) || {};
    const cCells = side(c, stGreeks(spot, k, years, c.implied_volatility, true), k < spot, "cbid", true);
    const pCells = side(p, stGreeks(spot, k, years, p.implied_volatility, false), k > spot, "pbid", false);
    return `<tr class="${k === atmStrike ? "atm" : ""}" data-strike="${k}">${cCells.join("")}` +
           `<td class="strike">${escapeHtml(stNum(k))}</td>${pCells.join("")}</tr>`;
  }).join("");
  const th = (h) => `<th>${h}</th>`;
  $("st-chain").innerHTML = `<table><thead><tr>` +
    [...leadHeads, ...priceHeads].map(th).join("") +
    `<th class="strike">Strike</th>` +
    [...priceHeads, ...leadHeads.slice().reverse()].map(th).join("") +
    `</tr></thead><tbody>${body}</tbody></table>`;

  // Open on the money rather than at the top of the ladder.
  const wrap = $("st-chain"), atmRow = wrap.querySelector("tr.atm");
  if (atmRow) {
    wrap.scrollTop += atmRow.getBoundingClientRect().top
      - wrap.getBoundingClientRect().top - wrap.clientHeight / 2;
  }

  // The at-the-money pair is the honest single read of what the options are
  // charging; it drives the ATM IV signal card. The put/call ratios describe
  // the whole expiry, not just the strikes currently in the window.
  const atmIvs = [calls.get(atmStrike), puts.get(atmStrike)]
    .map((r) => r && r.implied_volatility).filter((v) => v > 0);
  stAtmIv = atmIvs.length ? atmIvs.reduce((a, b) => a + b, 0) / atmIvs.length : null;
  const total = (m, f) => [...m.values()].reduce((t, r) => t + (Number(r[f]) || 0), 0);
  const callVol = total(calls, "volume"), putVol = total(puts, "volume");
  const callOi = total(calls, "open_interest"), putOi = total(puts, "open_interest");
  $("st-chain-note").textContent =
    `${strikes.length} of ${ladder.length} strikes · calls left, puts right` +
    (stAtmIv != null ? ` · ATM IV ${(stAtmIv * 100).toFixed(1)}%` : "") +
    (callVol ? ` · P/C vol ${(putVol / callVol).toFixed(2)}` : "") +
    (callOi ? ` · P/C OI ${(putOi / callOi).toFixed(2)}` : "") +
    ` · greeks from Black–Scholes on the published IV`;
  renderStockSignals();
  $("st-chain").querySelectorAll("td[data-sim]").forEach((td) => {
    td.onclick = () => simOpen({
      expiration: expiry.expiration,
      strike: Number(td.parentElement.dataset.strike),
      option_type: td.dataset.sim,
    });
  });
}

// --- d. hedge simulator -----------------------------------------------------
// One dialog, two ways in: the header button lets the engine pick the strikes,
// a chain row pins the contract you clicked. Both run the portfolio hedge
// engine over a one-name book, so "protection", "cost" and the verdict mean
// exactly what they mean on the Portfolio tab. Nothing here is a position —
// the endpoint reads no book and writes no record.
let simContract = null;   // null = let the engine choose the structure
let simSymbol = null;     // the symbol the notional box is currently sized for

const simSpot = () => (stQuote && stQuote.last_price) ||
  (stSeries.length ? stSeries[stSeries.length - 1].c : null);

function simOpen(contract) {
  simContract = contract || null;
  const spot = simSpot();
  $("sim-title").textContent = stSym;
  $("sim-sub").textContent = simContract
    ? `${simContract.expiration} · ${stNum(simContract.strike)} ${simContract.option_type} — ` +
      (simContract.option_type === "put"
        ? "protection you buy"
        : "an overwrite you write against the shares")
    : `Best protection the engine can build from ${stSym}'s own listed options`;
  // One contract's worth is the smallest position an option can hedge, so it
  // is the honest amount to open on rather than a round number that cannot.
  if (simSymbol !== stSym) {
    simSymbol = stSym;
    $("sim-notional").value = spot ? Math.ceil(spot * 100) : 25000;
  }
  simSizing();
  $("sim-verdict").innerHTML = "";
  $("sim-rows").innerHTML = "";
  $("sim-notes").innerHTML = "";
  hgScenario("sim", {});
  $("sim-dialog").showModal();
}

// Dollars floor to shares and shares floor to contracts; showing both while
// the user types stops them discovering the granularity wall after a request.
function simSizing() {
  const spot = simSpot(), notional = Number($("sim-notional").value);
  if (!spot || !(notional > 0)) { $("sim-sizing").textContent = ""; return; }
  const shares = Math.floor(notional / spot + 1e-9);
  const contracts = Math.floor(shares / 100);
  $("sim-sizing").textContent =
    `≈ ${shares.toLocaleString()} shares at ${fmt$(spot)} · covers ${contracts} contract` +
    (contracts === 1 ? "" : "s") +
    (contracts < 1
      ? ` — under one contract, so no option can be sized against it (${fmt$(spot * 100)} buys one)`
      : "");
}

async function simRun() {
  const button = $("sim-run");
  button.disabled = true;
  $("sim-verdict").innerHTML = "";
  $("sim-notes").innerHTML = "";
  $("sim-rows").innerHTML = `<div class="empty">Pricing chains and replaying shocks…</div>`;
  try {
    const body = {
      symbol: stSym,
      notional: Number($("sim-notional").value),
      horizon_days: Number($("sim-horizon").value),
      target_reduction_fraction: Number($("sim-fraction").value),
    };
    if (simContract) body.contract = simContract;
    simRender(await api("/api/hedge/simulate", { method: "POST", body }));
  } catch (e) {
    $("sim-rows").innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
    hgScenario("sim", {});
  } finally {
    button.disabled = false;
  }
}

function simRender(d) {
  const sell = d.verdict.action === "de_risk_by_selling";
  $("sim-verdict").innerHTML =
    `<div class="kv"><div class="r"><span class="k">Tail loss on ${fmt$(d.value)} of ` +
    `${escapeHtml(d.symbol)} (${fmtPct(1 - d.request.var_level)} confidence, ` +
    `${d.request.horizon_days} sessions)</span>` +
    `<span class="v neg">${fmt$(Math.abs(d.target.cvar_unhedged))}</span></div>` +
    `<div class="r"><span class="k">Looking to remove</span>` +
    `<span class="v">${fmt$(d.target.reduction_sought)}</span></div>` +
    `<div class="r"><span class="k">Verdict</span><span class="v ${sell ? "neg" : "pos"}">` +
    (sell ? "Sell, don't hedge"
          : `Hedge — ${escapeHtml(HG_KINDS[d.verdict.best_candidate] || d.verdict.best_candidate)}, ` +
            `${escapeHtml(d.verdict.size)}`) +
    `</span></div></div>` +
    (d.verdict.reason ? `<p class="explain">${escapeHtml(d.verdict.reason)}.</p>` : "");

  $("sim-rows").innerHTML = d.rows.length
    ? `<table><tr><th>structure</th><th>size</th><th>strikes</th><th>reaches goal?</th>` +
      `<th>protection</th><th>cost</th><th>per $1 protection</th>` +
      `<th>if it rallies 10%</th><th>stock exposure left</th></tr>` +
      d.rows.map((r) => {
        const ci = r.protection_bps_ci95, ratio = r.cost_per_unit_protection;
        const legs = (r.legs || []).map((l) =>
          `${l.quantity > 0 ? "+" : ""}${l.quantity} ${stNum(l.strike)}${l.option_type[0]}`).join(" / ");
        return `<tr><td>${escapeHtml(HG_KINDS[r.kind] || r.kind)}</td>` +
          `<td class="mono">${r.quantity} contract${r.quantity === 1 ? "" : "s"}</td>` +
          `<td class="mono dim">${escapeHtml(legs)}</td>` +
          `<td class="${r.meets_target ? "pos" : "neg"}">${r.meets_target ? "yes" : "no"}</td>` +
          `<td class="mono">${r.protection_bps.toFixed(0)} bps ` +
          `<span class="dim">(${ci[0].toFixed(0)}–${ci[1].toFixed(0)})</span></td>` +
          `<td class="mono ${cls(-r.cost_bps)}">${r.cost_bps.toFixed(0)} bps</td>` +
          `<td class="mono">${ratio == null ? "-" : ratio.toFixed(2)}</td>` +
          `<td class="mono ${cls(r.upside_loss["+10%"])}">${fmt$(r.upside_loss["+10%"])}</td>` +
          `<td class="mono">${r.residual_beta_dollars == null ? "-" : fmt$(r.residual_beta_dollars)}</td></tr>`;
      }).join("") + "</table>"
    : `<div class="empty">No hedge could be built from live quotes.</div>`;

  const notes = [];
  if (d.position.note) notes.push(escapeHtml(d.position.note));
  notes.push(`Shocks: ${d.shocks.windows} overlapping ${d.shocks.horizon_days}-session windows ` +
    `(${d.shocks.independent_windows} genuinely independent) of ${escapeHtml(d.shocks.driver)}'s own ` +
    `history, ${d.shocks.period[0]} to ${d.shocks.period[1]}.`);
  (d.excluded || []).forEach((x) =>
    notes.push(`Skipped ${escapeHtml(HG_KINDS[x.kind] || x.kind)}: ${escapeHtml(x.reason)}.`));
  (d.warnings || []).forEach((w) => notes.push(escapeHtml(w)));
  d.rows.filter((r) => r.granularity_warning).forEach((r) =>
    notes.push(escapeHtml(r.granularity_warning)));
  $("sim-notes").innerHTML = notes.map((n) => `<div>· ${n}</div>`).join("");
  hgScenario("sim", d);
}

$("st-hedge").onclick = () => simOpen(null);
$("sim-run").onclick = simRun;
$("sim-notional").oninput = simSizing;
$("sim-close").onclick = () => $("sim-dialog").close();
$("sim-dialog").onclick = (e) => { if (e.target === $("sim-dialog")) $("sim-dialog").close(); };

// --- d. peers ---------------------------------------------------------------
// Who the comparables are is decided by three sources that disagree (industry
// classification, SIC registration, and the filings that name this company as
// competition) — the ranking and the evidence come back with the list. The
// columns are filled from one batched quote call and one batched history call,
// so a table of ten names costs two requests rather than twenty.
async function loadAdvPeers() {
  try {
    const d = await api(`/api/v1/equity/compare/peers?symbol=${encodeURIComponent(stSym)}&limit=10`);
    const rows = d.results || [];
    const peers = rows.map((r) => String(r.symbol).toUpperCase()).filter(Boolean);
    const symbols = [stSym, ...peers.filter((s) => s !== stSym)].slice(0, 11);
    if (symbols.length < 2) throw new Error("no comparables found");
    const subject = (d.extra && d.extra.subject) || {};
    $("st-peers-note").textContent =
      [subject.industry, subject.sic_description].filter(Boolean).join(" · ") || "same industry";
    const list = symbols.join(",");
    const [quotes, perf] = await Promise.all([
      api(`/api/v1/equity/price/quote?symbol=${encodeURIComponent(list)}`)
        .then((r) => r.results || []).catch(() => []),
      api(`/api/v1/quantitative/performance?symbol=${encodeURIComponent(list)}&start_date=${isoAgo(RANGE_DAYS["1Y"])}`)
        .then((r) => r.results || []).catch(() => []),
    ]);
    const byQuote = new Map(quotes.map((q) => [q.symbol, q]));
    const byPerf = new Map(perf.map((p) => [p.symbol, p]));
    const byPeer = new Map(rows.map((r) => [String(r.symbol).toUpperCase(), r]));
    const body = symbols.map((sym) => {
      const q = byQuote.get(sym) || {}, p = byPerf.get(sym) || {}, peer = byPeer.get(sym) || {};
      return `<tr class="${sym === stSym ? "subject" : ""}" data-sym="${escapeHtml(sym)}">
        <td class="sym">${escapeHtml(sym)}</td>
        <td class="num">${fmtBig(q.market_cap ?? peer.market_cap)}</td>
        <td class="num">${stNum(q.pe_ratio, 1)}</td>
        <td class="num ${p.total_return == null ? "" : cls(p.total_return)}">${stPct1(p.total_return)}</td>
        <td class="num">${stPct2(p.annualised_volatility)}</td>
        <td class="num">${stNum(q.beta)}</td>
        <td class="why">${escapeHtml(sym === stSym ? "the company being compared" : (peer.why || ""))}</td></tr>`;
    }).join("");
    $("st-peers").innerHTML = `<table class="clickrows">
      <tr><th>Symbol</th><th class="num">Mkt cap</th><th class="num">P/E</th>
          <th class="num">1Y</th><th class="num">Vol</th><th class="num">Beta</th>
          <th>Why it is here</th></tr>${body}</table>`;
    $("st-peers").querySelectorAll("tr[data-sym]").forEach((tr) => {
      tr.style.cursor = "pointer";
      tr.onclick = () => { if (tr.dataset.sym !== stSym) openStock(tr.dataset.sym, stFrom); };
    });
  } catch (e) {
    $("st-peers").innerHTML = `<div class="empty">No peer group for ${escapeHtml(stSym)}.</div>`;
  }
}

$("st-peers-compare").onclick = () => setStockMode("compare");

// --- e. fundamentals --------------------------------------------------------
async function loadAdvFundamentals() {
  try {
    const [income, cash] = await Promise.all([
      api(`/api/v1/equity/fundamental/income?symbol=${encodeURIComponent(stSym)}&period=annual&limit=5`)
        .then((d) => d.results || []),
      api(`/api/v1/equity/fundamental/cash?symbol=${encodeURIComponent(stSym)}&period=annual&limit=5`)
        .then((d) => d.results || []).catch(() => []),
    ]);
    if (!income.length) throw new Error("no filings");
    const years = income.map((r) => String(r.period_ending || "").slice(0, 10));
    const byYear = new Map(cash.map((r) => [String(r.period_ending || "").slice(0, 10), r]));
    const fy = (iso) => "FY" + iso.slice(2, 4);
    const rows = [
      ["Revenue", income.map((r) => fmtBig(r.revenue)), null],
      ["Gross margin", income.map((r) => stPct2(r.revenue ? r.gross_profit / r.revenue : null)), null],
      ["Operating income", income.map((r) => fmtBig(r.operating_income)), null],
      ["Net income", income.map((r) => fmtBig(r.net_income)), null],
      ["Diluted EPS", income.map((r) => stNum(r.eps_diluted)), null],
      ["Free cash flow", income.map((r) => {
        const c = byYear.get(String(r.period_ending || "").slice(0, 10));
        if (!c || c.operating_cash_flow == null || c.capital_expenditure == null) return "—";
        return fmtBig(c.operating_cash_flow - Math.abs(c.capital_expenditure));
      }), null],
      ["YoY revenue", income.map((r, i) => {
        if (!i || !income[i - 1].revenue) return "—";
        return stPct1(r.revenue / income[i - 1].revenue - 1);
      }), income.map((r, i) => (!i || !income[i - 1].revenue ? "" : cls(r.revenue / income[i - 1].revenue - 1)))],
    ];
    $("st-funds").innerHTML = `<table>
      <tr><th></th>${years.map((y) => `<th class="num">${fy(y)}</th>`).join("")}</tr>
      ${rows.map(([label, cells, tones]) => `<tr><td class="lbl">${label}</td>` +
        cells.map((v, i) => `<td class="num ${tones ? tones[i] : ""}">${escapeHtml(v)}</td>`).join("") +
        `</tr>`).join("")}</table>`;
  } catch {
    $("st-funds").innerHTML = `<div class="empty">No filed financials — indexes, ETFs and crypto do not file with the SEC.</div>`;
  }
}

// --- e. news & sentiment ----------------------------------------------------
async function loadAdvSentiment() {
  try {
    const d = await api(`/api/v1/sentiment/symbol?symbol=${encodeURIComponent(stSym)}&limit=25`);
    const row = one(d.results);
    if (!row) throw new Error("no coverage");
    const [label, color] = SENTI_LBL[row.label] || SENTI_LBL.neutral;
    $("st-senti").innerHTML = `<div class="advsenti">
      <div>
        <div class="senti-word" style="color:${color}">${label}</div>
        <div class="senti-num">${fmtScore(row.score)} · ${row.articles ?? 0} stories</div>
      </div>
      <div class="senti-meterwrap">${sentiMeterHtml(row.score)}</div>
    </div>
    <p class="explain" style="margin:0 0 4px">${escapeHtml(row.reading || "")}</p>`;

    const articles = ((d.extra && d.extra.articles) || {})[stSym] || [];
    $("st-advnews").innerHTML = articles.slice(0, 5).map((a) => `
      <div class="feed-item">
        <div class="feed-meta">
          ${a.source ? `<span class="src-badge">${escapeHtml(String(a.source))}</span>` : ""}
          <span>${timeAgo(a.date)}</span>
          <span class="sc ${a.score > 0 ? "pos" : a.score < 0 ? "neg" : "dim"}">${
            a.score ? fmtScore(a.score) : "0.00"}</span>
        </div>
        ${a.url
          ? `<a class="feed-title" href="${escapeHtml(String(a.url))}" target="_blank" rel="noopener">${escapeHtml(String(a.title || ""))}</a>`
          : `<span class="feed-title">${escapeHtml(String(a.title || ""))}</span>`}
      </div>`).join("") || `<div class="empty">No recent stories found.</div>`;
  } catch {
    $("st-senti").innerHTML = `<div class="empty">No scored coverage for ${escapeHtml(stSym)}.</div>`;
    $("st-advnews").innerHTML = "";
  }
}

// ---------- stock page: exposure mode ----------
// A map of who this company trades with, built from what companies are obliged
// to disclose: any counterparty past a concentration threshold, named, with a
// percentage. The backend does the reading (see backend/providers/supplychain);
// this draws it, and always keeps the sentence a relationship came from within
// one click, because a mined relationship the reader cannot check is a rumour.
let stExpoSym = null, stExpoRows = [];

function resetStockExposure() {
  stExpoSym = null;
  stExpoRows = [];
  $("st-expo-note").textContent = "";
  $("st-expo-lede").textContent = "";
  $("st-expo-legend").innerHTML = "";
  $("st-expo-map").innerHTML = `<div class="empty">Loading…</div>`;
  $("st-expo-table").innerHTML = `<div class="empty">Loading…</div>`;
}

async function openStockExposure() {
  if (stExpoSym === stSym) { drawExposureLinks(); return; }
  stExpoSym = stSym;
  const symbolAtStart = stSym;
  $("st-expo-map").innerHTML =
    `<div class="empty">Reading SEC filings that name ${escapeHtml(stSym)}. ` +
    `The first look at a company takes a few seconds; after that it is cached.</div>`;
  $("st-expo-table").innerHTML = `<div class="empty">Loading…</div>`;
  try {
    const d = await api(`/api/v1/equity/relationships/graph?symbol=${encodeURIComponent(stSym)}`);
    if (symbolAtStart !== stSym) return;   // moved on while the filings were read
    stExpoRows = d.results || [];
    renderExposure(d);
    paintExposureQuotes(symbolAtStart);
  } catch (e) {
    if (symbolAtStart !== stSym) return;
    stExpoSym = null;                       // a retry should be allowed to re-run
    $("st-expo-map").innerHTML =
      `<div class="empty">No disclosed relationships for ${escapeHtml(stSym)} (${escapeHtml(e.message)}).<br>
       Only SEC filers appear here, and only when a filing puts a percentage next to a company's name.</div>`;
    $("st-expo-table").innerHTML = "";
  }
}

const EXPO_SIDE_LABEL = { supplier: "SUPPLIER", customer: "CUSTOMER", peer: "COMPARABLE" };

function renderExposure(payload) {
  const subject = ((payload.extra || {}).subject) || {};
  const rows = stExpoRows;
  const suppliers = rows.filter((r) => r.relationship === "supplier");
  const customers = rows.filter((r) => r.relationship === "customer");
  const peers = rows.filter((r) => r.relationship === "peer");

  $("st-expo-note").textContent = [subject.sector, subject.industry].filter(Boolean).join(" · ");
  $("st-expo-lede").innerHTML =
    `Every company below names ${escapeHtml(stSym)} in an SEC filing, or is named by it, with a ` +
    `percentage attached. <b>Suppliers</b> sell to ${escapeHtml(stSym)}; <b>customers</b> buy from it. ` +
    `Each percentage is a share of the books of whichever company disclosed it — the node says whose.`;

  $("st-expo-map").innerHTML = `
    <svg class="expo-svg" id="st-expo-svg" aria-hidden="true"></svg>
    <div class="expo-grid">
      <div class="expo-col" id="st-expo-sup">
        <div class="expo-side-h">SUPPLIERS — SELL TO ${escapeHtml(stSym)}</div>
        ${suppliers.map(expoNodeHtml).join("") ||
          `<div class="empty">No filer discloses ${escapeHtml(stSym)} as a customer.</div>`}
      </div>
      ${expoHubHtml("st-expo-hub-sup", suppliers.length, "Supplier")}
      <div id="st-expo-subject">${expoSubjectHtml(subject, suppliers, customers)}</div>
      ${expoHubHtml("st-expo-hub-cus", customers.length, "Customer")}
      <div class="expo-col" id="st-expo-cus">
        <div class="expo-side-h r">CUSTOMERS — BUY FROM ${escapeHtml(stSym)}</div>
        ${customers.map(expoNodeHtml).join("") ||
          `<div class="empty">No filer discloses buying from ${escapeHtml(stSym)}.</div>`}
      </div>
    </div>
    ${peers.length ? `<div class="expo-below">
      ${expoHubHtml("st-expo-hub-peer", peers.length, "Comparable")}
      <div class="expo-peerrow" id="st-expo-peers">${peers.map(expoNodeHtml).join("")}</div>
    </div>` : ""}`;

  const sources = (payload.extra || {}).sources || {};
  const leg = (key, label) => {
    const s = sources[key];
    if (!s) return "";
    return `<span><b>${label}:</b> ${s.error ? "nothing found" : s.rows + " found"}</span>`;
  };
  $("st-expo-legend").innerHTML =
    leg("counterparty_filings", "From other companies' annual reports") +
    leg("own_filing", `From ${escapeHtml(stSym)}'s own annual report`) +
    leg("peers", "Industry comparables") +
    `<span>Border colour is today's price move. Click any company to open it.</span>`;

  renderExposureTable(rows);
  requestAnimationFrame(drawExposureLinks);
}

function expoHubHtml(id, count, noun) {
  return `<div class="expo-hub ${count ? "" : "empty-hub"}" id="${id}">
    <span class="hub-label">${count} ${escapeHtml(noun)}${count === 1 ? "" : "s"}</span>
    <span class="hub-dot"></span>
  </div>`;
}

function expoSubjectHtml(subject, suppliers, customers) {
  const row = (k, v) => v ? `<div class="s-row"><span>${k}</span><span>${v}</span></div>` : "";
  // The two averages say how concentrated the disclosed relationships are —
  // the number a supply-chain screen exists to surface.
  const avg = (list) => {
    const vals = list.map((r) => r.exposure_pct).filter((v) => v != null);
    return vals.length ? (vals.reduce((a, b) => a + b, 0) / vals.length).toFixed(1) + "%" : null;
  };
  return `<div class="expo-subject" id="st-expo-center">
    <div class="s-sym">${escapeHtml(stSym)}</div>
    <div class="s-name">${escapeHtml(subject.name || "")}</div>
    ${row("Market cap", subject.market_cap ? fmtBig(subject.market_cap) : null)}
    ${row("Revenue TTM", subject.revenue_ttm ? fmtBig(subject.revenue_ttm) : null)}
    ${row("Gross margin", subject.gross_margin != null ? fmtPct(subject.gross_margin) : null)}
    ${row("Avg supplier exposure", avg(suppliers))}
    ${row("Avg customer exposure", avg(customers))}
  </div>`;
}

function expoNodeHtml(r) {
  const sym = r.symbol ? String(r.symbol).toUpperCase() : "";
  const pct = r.exposure_pct != null
    ? `<b>${r.exposure_pct}%</b> of ${escapeHtml(String(r.pct_of || sym))} ${escapeHtml(r.exposure_basis || "revenue")}`
    : escapeHtml(r.exposure_basis || "same industry");
  return `<button class="expo-node flat" data-expo-sym="${escapeHtml(sym)}"
      title="${escapeHtml(r.quote || r.company || sym)}">
    <span class="n-name">${escapeHtml(r.company || sym)}</span>
    <span class="n-meta"><span class="n-chg" data-expo-chg="${escapeHtml(sym)}"></span>${pct}</span>
  </button>`;
}

// Colour every node by the day's move, in one batched quote call rather than
// one per company.
async function paintExposureQuotes(symbolAtStart) {
  const symbols = [...new Set(stExpoRows.map((r) => r.symbol).filter(Boolean))].slice(0, 40);
  if (!symbols.length) return;
  let quotes = [];
  try {
    const d = await api(`/api/v1/equity/price/quote?symbol=${encodeURIComponent(symbols.join(","))}`);
    quotes = d.results || [];
  } catch { return; }
  if (symbolAtStart !== stSym) return;
  const bySym = new Map(quotes.map((q) => [String(q.symbol).toUpperCase(), q]));
  document.querySelectorAll("#st-expo-map [data-expo-sym]").forEach((node) => {
    const q = bySym.get(node.dataset.expoSym);
    const chg = q && q.change_percent;
    if (chg == null) return;
    node.classList.remove("flat");
    node.classList.add(chg >= 0 ? "up" : "down");
    const slot = node.querySelector("[data-expo-chg]");
    if (slot) slot.textContent = fmtPct(chg, true);
  });
}

// The connectors are measured, never authored: read where the browser actually
// put each node and join it to its hub. Re-run on resize for the same reason.
function drawExposureLinks() {
  const svg = $("st-expo-svg"), map = $("st-expo-map");
  if (!svg || !map) return;
  const box = map.getBoundingClientRect();
  if (box.width < 2) return;
  svg.setAttribute("viewBox", `0 0 ${box.width} ${box.height}`);
  // Where to anchor a connector on an element, in the map's own coordinates.
  const at = (el, edge) => {
    const r = el.getBoundingClientRect();
    const midY = r.top - box.top + r.height / 2;
    const midX = r.left - box.left + r.width / 2;
    if (edge === "left") return [r.left - box.left, midY];
    if (edge === "right") return [r.right - box.left, midY];
    if (edge === "top") return [midX, r.top - box.top];
    if (edge === "bottom") return [midX, r.bottom - box.top];
    return [midX, midY];
  };
  const stacked = getComputedStyle($("st-expo-map").querySelector(".expo-grid"))
    .gridTemplateColumns.split(" ").length < 5;
  if (stacked) { svg.innerHTML = ""; return; }   // narrow layout: no diagram to wire

  const centre = $("st-expo-center");
  const paths = [];
  const hubDot = (id) => $(id) && $(id).querySelector(".hub-dot");
  const fan = (hubId, colId, side) => {
    const hub = hubDot(hubId), col = $(colId);
    if (!hub || !col || !centre) return;
    const [hx, hy] = at(hub);
    col.querySelectorAll("[data-expo-sym]").forEach((node) => {
      const [nx, ny] = at(node, side === "left" ? "right" : "left");
      paths.push(`M ${nx} ${ny} L ${hx} ${hy}`);
    });
    const [cx, cy] = at(centre, side === "left" ? "left" : "right");
    paths.push(`M ${hx} ${hy} L ${cx} ${cy}`);
  };
  fan("st-expo-hub-sup", "st-expo-sup", "left");
  fan("st-expo-hub-cus", "st-expo-cus", "right");

  const peerHub = hubDot("st-expo-hub-peer");
  if (peerHub && centre) {
    const [px, py] = at(peerHub);
    const [cx, cy] = at(centre, "bottom");
    paths.push(`M ${cx} ${cy} L ${px} ${py}`);
    ($("st-expo-peers") || map).querySelectorAll("[data-expo-sym]").forEach((node) => {
      const [nx, ny] = at(node, "top");
      paths.push(`M ${px} ${py} L ${nx} ${ny}`);
    });
  }
  svg.innerHTML = paths
    .map((d) => `<path d="${d}" fill="none" stroke="#2b6f4a" stroke-width="1" opacity=".75"/>`)
    .join("");
}

function renderExposureTable(rows) {
  const disclosed = rows.filter((r) => r.quote);
  if (!disclosed.length) {
    $("st-expo-table").innerHTML =
      `<div class="empty">Nothing on the map came from a filing — only industry comparables.</div>`;
    return;
  }
  const body = disclosed.map((r) => {
    const sym = String(r.symbol || "").toUpperCase();
    const link = r.filing_url
      ? ` <a href="${escapeHtml(r.filing_url)}" target="_blank" rel="noopener">${escapeHtml(r.form || "filing")}
          ${escapeHtml(String(r.filing_date || "").slice(0, 10))} →</a>`
      : "";
    return `<tr data-sym="${escapeHtml(sym)}">
      <td class="sym">${escapeHtml(sym)}</td>
      <td>${escapeHtml(r.company || "")}</td>
      <td class="side ${escapeHtml(r.relationship)}">${EXPO_SIDE_LABEL[r.relationship] || ""}</td>
      <td class="num">${r.exposure_pct != null ? r.exposure_pct + "%" : "-"}</td>
      <td>${escapeHtml(String(r.pct_of || ""))} ${escapeHtml(r.exposure_basis || "")}</td>
      <td class="quote">“${escapeHtml(r.quote)}”${link}</td></tr>`;
  }).join("");
  $("st-expo-table").innerHTML = `<table class="clickrows">
    <tr><th>Symbol</th><th>Company</th><th>Side</th><th class="num">Disclosed</th>
        <th>Share of</th><th>What the filing says</th></tr>${body}</table>`;
}

// One delegated handler for the whole mode: nodes and table rows both open the
// company they name.
$("st-exposure").addEventListener("click", (event) => {
  const node = event.target.closest("[data-expo-sym]");
  const row = event.target.closest("#st-expo-table tr[data-sym]");
  if (event.target.closest("a")) return;         // filing links open in a new tab
  const sym = node ? node.dataset.expoSym : row ? row.dataset.sym : null;
  if (sym && sym !== stSym) openStock(sym, stFrom);
});

window.addEventListener("resize", () => {
  if (stMode === "exposure" && $("view-stock").classList.contains("active")) drawExposureLinks();
});

// ---------- stock page: financials mode ----------
// The three statements as filed, straight off the company's own XBRL (Yahoo
// stands in where a filer reports under IFRS and tags no us-gaap). One fetch
// covers all three statements and every period; the tab, period and view chips
// then re-render what is already here rather than going back for it.
let stFsSym = null, stFsPeriod = "annual", stFsTab = "income", stFsView = "reported";
let stFsRows = [], stFsMeta = null;

function resetStockFinancials() {
  stFsSym = null;
  stFsRows = [];
  stFsMeta = null;
  resetStockSegments();
  stFsTab = "income";
  stFsView = "reported";
  syncFsChips();
  $("st-fs-note").textContent = "";
  $("st-fs-scale").textContent = "";
  $("st-fs-foot").innerHTML = "";
  $("st-fs-table").innerHTML = `<div class="empty">Loading…</div>`;
}

function syncFsChips() {
  const set = (sel, attr, value) => document.querySelectorAll(sel).forEach((c) =>
    c.classList.toggle("active", c.dataset[attr] === value));
  set("#st-fs-tabs .chip", "fs", stFsTab);
  set("#st-fs-periods .chip", "fsperiod", stFsPeriod);
  set("#st-fs-views .chip", "fsview", stFsView);
}

async function openStockFinancials() {
  const key = `${stSym}:${stFsPeriod}`;
  if (stFsSym === key) { renderFinancials(); return; }
  stFsSym = key;
  const symbolAtStart = stSym;
  $("st-fs-table").innerHTML = `<div class="empty">Reading ${escapeHtml(stSym)}'s filings…</div>`;
  try {
    // The year-to-date view is four columns that are not periods, so it has an
    // endpoint of its own; everything downstream of the fetch is shared.
    const d = await api(stFsPeriod === "ytd"
      ? `/api/v1/equity/fundamental/statements_ytd?symbol=${encodeURIComponent(stSym)}`
      : `/api/v1/equity/fundamental/statements?symbol=${encodeURIComponent(stSym)}` +
        `&period=${stFsPeriod}&limit=${stFsPeriod === "annual" ? 8 : 10}`);
    if (symbolAtStart !== stSym) return;
    stFsRows = d.results || [];
    stFsMeta = d.extra || {};
    renderFinancials();
  } catch (e) {
    if (symbolAtStart !== stSym) return;
    stFsSym = null;
    stFsMeta = null;
    $("st-fs-note").textContent = "";
    $("st-fs-scale").textContent = "";
    $("st-fs-foot").innerHTML = "";
    $("st-fs-table").innerHTML =
      `<div class="empty">No filed statements for ${escapeHtml(stSym)}.<br>
       Indexes, ETFs and crypto do not file with the SEC. (${escapeHtml(e.message)})</div>`;
  }
}

const FS_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Columns are labelled by the date the period ends. Quarters deliberately do
// not get a "Q3" label: fiscal quarters only line up with calendar ones for a
// December year-end, and the quarter ending June is Apple's third, not its
// second. The month is unambiguous for every filer; the full date is on hover.
// The year-to-date view's columns are not periods at all, so it names its own —
// except on the balance sheet, where nothing is added up and the column is a
// date the company stood somewhere rather than a stretch of the year.
function fsPeriodLabel(iso) {
  const named = (stFsMeta || {}).period_labels;
  if (named && named[iso]) {
    return stFsTab === "balance" ? named[iso].replace(/^YTD /, "At ") : named[iso];
  }
  const year = iso.slice(2, 4);
  if (stFsPeriod === "annual") return `FY${year}`;
  return `${FS_MONTHS[Number(iso.slice(5, 7)) - 1]} ${year}`;
}

function fsPeriodTitle(iso) {
  const titles = (stFsMeta || {}).period_titles;
  return (titles && titles[iso]) || iso;
}

// One scale for the whole statement, chosen from its largest number, so the
// columns can be read against each other without re-checking a unit per row.
function fsScale(values) {
  const max = Math.max(0, ...values.map((v) => Math.abs(v || 0)));
  if (max >= 1e12) return { div: 1e9, unit: "$ billions" };
  if (max >= 1e9) return { div: 1e6, unit: "$ millions" };
  if (max >= 1e6) return { div: 1e3, unit: "$ thousands" };
  return { div: 1, unit: "$" };
}

const fsNum = (x, digits = 0) =>
  x == null ? "—"
    : (x < 0 ? "(" + Math.abs(x).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits }) + ")"
             : x.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits }));

// One cell, under whichever view is selected. Shared by the statements and the
// segment table, which are the same shape: a labelled row, one value a period.
// ``ctx`` carries what differs — the common-size base, the scale the columns
// were sized to, the period axis, and any explicit pairing for the growth view.
function fsCell(row, period, ctx) {
  const raw = row[period];
  // The estimate is not a filed number and is marked wherever it appears.
  const est = period === "projected_fy"
    ? ` fs-est" title="${escapeHtml(row.projection_basis === "runrate"
        ? "Run rate: the year so far scaled to four quarters, for want of a comparable year"
        : row.projection_basis === "seasonal"
          ? "Estimated from what the rest of the year added in previous years"
          : "Estimated from the lines above")}`
    : "";
  if (raw == null) return `<td class="v none">—</td>`;
  if (stFsView === "common") {
    const base = ctx.baseRow && ctx.baseRow[period];
    if (!base || ["pershare", "shares"].includes(row.weight)) return `<td class="v none">—</td>`;
    return `<td class="v${est}">${(raw / base * 100).toFixed(1)}%</td>`;
  }
  if (stFsView === "growth") {
    // Newest first, so the period being compared against is the next one
    // along — except where the columns are not a timeline and the server
    // says which pairs off against which.
    const against = (ctx.compareTo || {})[period];
    if (ctx.compareTo && !against) return `<td class="v none">—</td>`;
    const prior = row[against || ctx.allPeriods[ctx.allPeriods.indexOf(period) + 1]];
    // Percentage change across a sign flip is not a meaningful number —
    // a loss turning into a profit is not "up 240%".
    if (prior == null || prior === 0 || Math.sign(prior) !== Math.sign(raw)) {
      return `<td class="v none">—</td>`;
    }
    const change = raw / prior - 1;
    return `<td class="v ${cls(change)}${est}">${change >= 0 ? "+" : ""}${(change * 100).toFixed(1)}%</td>`;
  }
  if (row.weight === "pershare") return `<td class="v${est}">${fsNum(raw, 2)}</td>`;
  if (row.weight === "shares") return `<td class="v${est}">${fsNum(raw / 1e6, 0)}M</td>`;
  return `<td class="v ${raw < 0 ? "neg" : ""}${est}">${fsNum(raw / ctx.scale.div)}</td>`;
}

// Rows arrive grouped, each carrying the heading it belongs under.
function fsBody(rows, periods, ctx) {
  let section = null;
  return rows.map((row) => {
    let head = "";
    if (row.section && row.section !== section) {
      section = row.section;
      head = `<tr class="fs-section"><td colspan="${periods.length + 1}">${escapeHtml(section)}</td></tr>`;
    }
    const classes = ["indent-" + row.indent, row.weight ? "w-" + row.weight : "",
                     row.derived ? "derived" : ""].filter(Boolean).join(" ");
    // A statement line is labelled; a segment row is named for the segment.
    return head + `<tr class="${classes}"><td class="lbl">${escapeHtml(row.label || row.segment)}</td>` +
      periods.map((p) => fsCell(row, p, ctx)).join("") + `</tr>`;
  }).join("");
}

function fsTable(rows, periods, ctx) {
  return `<table>
    <thead><tr><th></th>${periods.map((p) =>
      `<th class="v ${p === "projected_fy" ? "fs-est" : ""}" title="${escapeHtml(fsPeriodTitle(p))}"
        >${escapeHtml(fsPeriodLabel(p))}</th>`).join("")}</tr></thead>
    <tbody>${fsBody(rows, periods, ctx)}</tbody></table>`;
}

function renderFinancials() {
  syncFsChips();
  if (stFsTab === "segments") { renderSegments(); return; }
  const meta = stFsMeta || {};
  const allPeriods = meta.periods || [];
  const rows = stFsRows.filter((r) => r.statement === stFsTab);
  // The period axis spans all three statements, so one of them can be missing a
  // year the others have. Drop the columns this statement has nothing in —
  // but keep the full axis for growth, which needs the period before the first
  // one shown to have something to compare against.
  const periods = allPeriods.filter((p) => rows.some((r) => r[p] != null));
  if (!rows.length || !periods.length) {
    $("st-fs-table").innerHTML =
      `<div class="empty">No ${escapeHtml(stFsTab === "cash" ? "cash-flow" : stFsTab)} statement
       filed for ${escapeHtml(stSym)} on this basis.</div>`;
    $("st-fs-scale").textContent = "";
    renderFinancialsFoot();
    return;
  }

  // Per-share and share-count lines are not money and must not be scaled with
  // the rest of the statement.
  const money = rows.filter((r) => !["pershare", "shares"].includes(r.weight));
  const scale = fsScale(money.flatMap((r) => periods.map((p) => r[p])));
  // A common-size income statement is a share of revenue; a common-size
  // balance sheet is a share of total assets, because a balance sheet has no
  // revenue on it.
  const baseRow = stFsRows.find((r) =>
    r.line_item === (stFsTab === "balance" ? "total_assets" : "revenue"));

  $("st-fs-table").innerHTML =
    fsTable(rows, periods, { baseRow, scale, allPeriods, compareTo: meta.compare_to });

  $("st-fs-scale").textContent =
    stFsView === "common" ? (stFsTab === "balance" ? "PERCENT OF TOTAL ASSETS" : "PERCENT OF REVENUE")
      : stFsView === "growth" ? (stFsPeriod === "ytd" ? "CHANGE VS A YEAR AGO" : "CHANGE VS PRIOR PERIOD")
        : scale.unit.toUpperCase();
  renderFinancialsFoot();
}

function renderFinancialsFoot() {
  const meta = stFsMeta || {};
  const served = meta.provider_by_statement || {};
  const source = served[stFsTab];
  const projection = meta.projection || {};
  $("st-fs-note").textContent = stFsPeriod === "annual"
    ? "fiscal years, newest first"
    : stFsPeriod === "quarter" ? "three-month periods, newest first"
      : `${meta.quarters_elapsed || 0} quarter${meta.quarters_elapsed === 1 ? "" : "s"}` +
        ` filed since ${meta.fiscal_year_opened || ""}`;
  const notes = [];
  notes.push(`<span><b>Columns</b> are labelled by the date the period ends — hover for it.</span>`);
  if (stFsPeriod === "ytd") {
    // One statement can lag the other two. Its columns are still compared
    // like for like, but they cover less of the year than the header says.
    const mine = (meta.quarters_by_statement || {})[stFsTab];
    if (mine && mine !== meta.quarters_elapsed) {
      notes.push(`<span><b>This statement is a quarter behind:</b> it is filed only to
        ${escapeHtml((meta.through_by_statement || {})[stFsTab] || "")}, so its columns cover
        ${mine} quarter${mine === 1 ? "" : "s"} — compared against the same ${mine} a year ago,
        not the ${meta.quarters_elapsed} in the heading.</span>`);
    }
    notes.push(stFsTab === "balance"
      // Worth being blunt: a balance sheet is not a running total, and the
      // estimate column is deliberately empty rather than quietly wrong.
      ? `<span><b>The balance sheet is a position, not a total:</b> the year-to-date column is
         where ${escapeHtml(stSym)} stood at the latest quarter end, not a sum of quarters. There
         is no estimate — a year-end balance sheet is a roll-forward, not an extrapolation.</span>`
      : `<span><b>Year to date</b> adds up the quarters filed since the fiscal year opened.</span>`);
    if (stFsTab !== "balance") {
      notes.push(projection.method === "seasonal"
        ? `<span><b>${escapeHtml(meta.period_labels?.projected_fy || "The estimate")}</b> scales the
           year so far by what the rest of the year added over the last
           ${projection.seasonal_years} year${projection.seasonal_years === 1 ? "" : "s"} — the median,
           so one odd year does not set it.</span>`
        : `<span><b>${escapeHtml(meta.period_labels?.projected_fy || "The estimate")}</b> is a plain
           run rate — the year so far scaled to four quarters. No comparable earlier year was filed,
           so any seasonality in the business is not in this number.</span>`);
      if (projection.method === "seasonal" && projection.lines_by_method?.runrate) {
        notes.push(`<span>${projection.lines_by_method.runrate} line${
          projection.lines_by_method.runrate === 1 ? " falls" : "s fall"} back to a run rate for
          want of a comparable year — hover the estimate to see which.</span>`);
      }
      notes.push(`<span>The estimate assumes the year behaves like the ones before it. It is not a
        forecast, and it knows nothing of guidance, orders or anything else since the last filing.</span>`);
    }
  }
  notes.push(source === "sec"
    ? `<span><b>Source:</b> SEC XBRL — the company's own tags, as filed</span>`
    : source === "yahoo"
      ? `<span><b>Source:</b> Yahoo Finance — used where a filer reports no us-gaap XBRL</span>`
      : "");
  notes.push(`<span><b>Italic rows</b> are computed here, not filed.</span>`);
  notes.push(`<span>Cash outflows are shown negative, in (brackets).</span>`);
  if (stFsPeriod === "quarter" && stFsTab === "income") {
    // Worth saying plainly: nobody files fiscal Q4 on its own.
    notes.push(`<span>Fiscal Q4 is the full year less the first three quarters, so
      per-share and share-count rows are blank for it.</span>`);
  }
  const missing = Object.keys(meta.missing || {});
  if (missing.length) {
    notes.push(`<span><b>Not filed:</b> ${escapeHtml(missing.join(", "))}</span>`);
  }
  $("st-fs-foot").innerHTML = notes.filter(Boolean).join("");
}

// ---------- stock page: revenue segments ----------
// A fourth tab on the same table. The three statements come from one call and
// this one from another, so it is fetched when the tab is first opened rather
// than alongside them — a segment table is read out of the filings themselves
// and there is no reason to pay for it unless it is being looked at.
let stSegKey = null, stSegRows = [], stSegMeta = null, stSegError = "";

function resetStockSegments() {
  stSegKey = null;
  stSegRows = [];
  stSegMeta = null;
  stSegError = "";
}

async function openStockSegments() {
  // Year to date is a running total of the quarters filed so far; segments are
  // filed by year or by quarter, and there is no year-to-date version of them.
  if (stFsPeriod === "ytd") { renderSegments(); return; }
  const key = `${stSym}:${stFsPeriod}`;
  if (stSegKey === key) { renderSegments(); return; }
  resetStockSegments();
  stSegKey = key;
  const symbolAtStart = stSym;
  $("st-fs-note").textContent = "";
  $("st-fs-scale").textContent = "";
  $("st-fs-foot").innerHTML = "";
  $("st-fs-table").innerHTML =
    `<div class="empty">Reading ${escapeHtml(stSym)}'s segment tables out of its filings…</div>`;
  try {
    const d = await api(`/api/v1/equity/fundamental/revenue_segments?symbol=${encodeURIComponent(stSym)}` +
                        `&period=${stFsPeriod}&limit=${stFsPeriod === "annual" ? 6 : 8}`);
    if (symbolAtStart !== stSym) return;
    stSegRows = d.results || [];
    stSegMeta = d.extra || {};
    stSegMeta.warnings = d.warnings || [];
  } catch (e) {
    if (symbolAtStart !== stSym) return;
    stSegError = e.message;
  }
  renderSegments();
}

function renderSegments() {
  syncFsChips();
  const meta = stSegMeta || {};
  const allPeriods = meta.periods || [];
  const periods = allPeriods.filter((p) => stSegRows.some((r) => r[p] != null));
  if (stFsPeriod === "ytd" || !periods.length) {
    $("st-fs-table").innerHTML = stFsPeriod === "ytd"
      ? `<div class="empty">Segments are filed by fiscal year or by quarter.<br>
         Pick <b>Annual</b> or <b>Quarterly</b> to see them.</div>`
      : `<div class="empty">${escapeHtml(stSym)} files no revenue breakdown in XBRL.<br>
         A single-segment company has nothing to split, and filings before about 2010
         predate the tagging.${stSegError ? ` (${escapeHtml(stSegError)})` : ""}</div>`;
    $("st-fs-note").textContent = "";
    $("st-fs-scale").textContent = "";
    $("st-fs-foot").innerHTML = "";
    return;
  }

  const scale = fsScale(stSegRows.flatMap((r) => periods.map((p) => r[p])));
  // Common size is a share of consolidated revenue, which is the row the table
  // closes on — so a group that does not add up to it reads as it should.
  const baseRow = stSegRows.find((r) => r.dimension === "total");
  $("st-fs-table").innerHTML = fsTable(stSegRows, periods, { baseRow, scale, allPeriods });

  $("st-fs-scale").textContent =
    stFsView === "common" ? "PERCENT OF REVENUE"
      : stFsView === "growth" ? "CHANGE VS PRIOR PERIOD"
        : scale.unit.toUpperCase();
  $("st-fs-note").textContent = stFsPeriod === "annual"
    ? "fiscal years, newest first" : "three-month periods, newest first";
  renderSegmentsFoot();
}

function renderSegmentsFoot() {
  const meta = stSegMeta || {};
  const notes = [`<span><b>Source:</b> SEC XBRL — read from the filings themselves. The
    company-facts API every other statement here uses drops the segment tagging.</span>`];
  (meta.dimensions || []).forEach((d) => {
    if (d.coverage == null) return;
    notes.push(`<span><b>${escapeHtml(d.section)}:</b> ${d.members} reported,
      ${(d.coverage * 100).toFixed(0)}% of revenue${d.table ? ` · ${escapeHtml(d.table)}` : ""}</span>`);
  });
  (meta.warnings || []).forEach((w) => notes.push(`<span>${escapeHtml(w)}</span>`));
  if ((meta.superseded || []).length) {
    notes.push(`<span><b>Replaced by a finer split:</b>
      ${escapeHtml(meta.superseded.join(", "))}</span>`);
  }
  const filings = (meta.filings || []).filter((f) => f.url && f.segment_facts);
  if (filings.length) {
    notes.push(`<span><b>Filings read:</b> ` + filings.map((f) =>
      `<a href="${escapeHtml(f.url)}" target="_blank" rel="noopener">${escapeHtml(f.form)}
       ${escapeHtml(String(f.filed || "").slice(0, 4))}</a>`).join(", ") + `</span>`);
  }
  $("st-fs-foot").innerHTML = notes.join("");
}

document.querySelectorAll("#st-fs-tabs .chip").forEach((c) => {
  c.onclick = () => {
    stFsTab = c.dataset.fs;
    if (stFsTab === "segments") openStockSegments();
    else renderFinancials();
  };
});
document.querySelectorAll("#st-fs-views .chip").forEach((c) => {
  c.onclick = () => { stFsView = c.dataset.fsview; renderFinancials(); };
});
document.querySelectorAll("#st-fs-periods .chip").forEach((c) => {
  c.onclick = () => {
    if (stFsPeriod === c.dataset.fsperiod) return;
    stFsPeriod = c.dataset.fsperiod;      // a different basis, so a new fetch
    syncFsChips();
    if (stFsTab === "segments") openStockSegments();
    else openStockFinancials();
  };
});

// ---------- stock page: compare mode ----------
// The peer list is a judgement, so it is editable and the edit is remembered
// per symbol. Everything below it — the chart, the metric table, the revenue
// mix — is a function of whichever group is on screen, so changing the group
// reloads the three of them and nothing else.
let stCmpSym = null;            // the symbol the loaded group belongs to
let stCmpGroup = [];            // peers, subject excluded
let stCmpSuggested = [];        // rows from /equity/compare/peers
let stCmpMeta = null;
let stCmpEdited = false;        // has this group been changed from the suggestion?

const CMP_MAX = 7;              // peers, so eight columns with the subject
const cmpKey = (sym) => `mft_peers_${sym}`;

function resetStockCompare() {
  stCmpSym = null;
  stCmpGroup = [];
  stCmpSuggested = [];
  stCmpMeta = null;
  stCmpEdited = false;
  $("st-cmp-group").innerHTML = "";
  $("st-cmp-lede").textContent = "";
  $("st-cmp-note").textContent = "";
  $("st-cmp-suggested").innerHTML = `<div class="empty">Loading…</div>`;
  $("st-cmp-table").innerHTML = `<div class="empty">Loading…</div>`;
  $("st-cmp-foot").innerHTML = "";
  $("st-cmp-mix").innerHTML = `<div class="empty">Loading…</div>`;
}

async function openStockCompare() {
  if (stCmpSym === stSym) { stResizeCharts(); return; }
  stCmpSym = stSym;
  const symbolAtStart = stSym;
  $("st-cmp-suggested").innerHTML =
    `<div class="empty">Reading who ${escapeHtml(stSym)} competes with…</div>`;
  try {
    const d = await api(`/api/v1/equity/compare/peers?symbol=${encodeURIComponent(stSym)}&limit=14`);
    if (symbolAtStart !== stSym) return;
    stCmpSuggested = d.results || [];
    stCmpMeta = d.extra || {};
  } catch (e) {
    if (symbolAtStart !== stSym) return;
    stCmpSuggested = [];
    stCmpMeta = { error: e.message };
  }
  const saved = await loadPeerGroup(stSym);
  if (symbolAtStart !== stSym) return;
  stCmpEdited = Boolean(saved);
  stCmpGroup = saved || stCmpSuggested.slice(0, 4).map((r) => r.symbol);
  renderCompareGroup();
  loadCompareViews();
}

// The saved group lives on the account so it follows you to another browser;
// localStorage answers first so the panel is not empty while that call is out.
async function loadPeerGroup(symbol) {
  const local = localStorage.getItem(cmpKey(symbol));
  let group = null;
  if (local) { try { group = JSON.parse(local); } catch { group = null; } }
  try {
    const rows = await api("/api/user/settings");
    const saved = (rows || []).find((r) => r.key === cmpKey(symbol));
    if (saved && Array.isArray(saved.value)) group = saved.value;
  } catch { /* the local copy is enough */ }
  return Array.isArray(group) && group.length ? group.slice(0, CMP_MAX) : null;
}

async function savePeerGroup() {
  const value = stCmpGroup.slice(0, CMP_MAX);
  localStorage.setItem(cmpKey(stCmpSym), JSON.stringify(value));
  try {
    await api("/api/user/settings", { method: "PUT", body: { key: cmpKey(stCmpSym), value } });
  } catch { /* a failed write is not worth interrupting anyone over */ }
}

async function forgetPeerGroup() {
  localStorage.removeItem(cmpKey(stCmpSym));
  try {
    await api(`/api/user/settings/${encodeURIComponent(cmpKey(stCmpSym))}`, { method: "DELETE" });
  } catch { /* it may never have been saved */ }
}

function renderCompareGroup() {
  const subject = `<span class="cmp-chip subject">${escapeHtml(stSym)}<small>subject</small></span>`;
  const chips = stCmpGroup.map((sym) =>
    `<span class="cmp-chip" data-cmp-sym="${escapeHtml(sym)}">${escapeHtml(sym)}
     <button class="cmp-x" type="button" title="Remove ${escapeHtml(sym)}">×</button></span>`).join("");
  $("st-cmp-group").innerHTML = subject + chips ||
    `<div class="empty">No companies in the group yet — add one below.</div>`;
  $("st-cmp-group").querySelectorAll(".cmp-x").forEach((btn) => {
    btn.onclick = () => removeFromGroup(btn.parentElement.dataset.cmpSym);
  });

  const subjectInfo = (stCmpMeta && stCmpMeta.subject) || {};
  $("st-cmp-note").textContent = [subjectInfo.industry, subjectInfo.sic_description]
    .filter(Boolean).join(" · ");
  $("st-cmp-lede").innerHTML = stCmpSuggested.length
    ? `Three sources, none of them paid: the industry classification, every SEC registrant
       filing under the same SIC code, and the filings that name ${escapeHtml(stSym)} as
       competition. Agreement between them ranks the list, and size settles the rest —
       everyone names the giant in their industry. ${stCmpEdited
        ? "<b>This group has been edited</b> and is remembered for this symbol."
        : "Tick a row to add it to the comparison."}`
    : "";
  renderSuggested();
}

function renderSuggested() {
  if (!stCmpSuggested.length) {
    $("st-cmp-suggested").innerHTML = `<div class="empty">No comparables found for
      ${escapeHtml(stSym)}${stCmpMeta && stCmpMeta.error ? ` (${escapeHtml(stCmpMeta.error)})` : ""}.
      Add tickers by hand to compare anyway.</div>`;
    return;
  }
  const body = stCmpSuggested.map((r) => {
    const inGroup = stCmpGroup.includes(r.symbol);
    const filing = r.filing_url
      ? `<a href="${escapeHtml(r.filing_url)}" target="_blank" rel="noopener"
          title="${escapeHtml(r.form || "")} filed ${escapeHtml(r.filed || "")}">filing</a>`
      : "";
    return `<tr data-sym="${escapeHtml(r.symbol)}" class="${inGroup ? "picked" : ""}">
      <td><input type="checkbox" ${inGroup ? "checked" : ""} aria-label="Compare ${escapeHtml(r.symbol)}"></td>
      <td class="sym">${escapeHtml(r.symbol)}</td>
      <td>${escapeHtml(r.company || "")}</td>
      <td class="num">${fmtBig(r.market_cap)}</td>
      <td class="why">${escapeHtml(r.why || "")} ${filing}</td>
      <td class="num">${stNum(r.score, 2)}</td></tr>`;
  }).join("");
  $("st-cmp-suggested").innerHTML = `<table>
    <tr><th></th><th>Symbol</th><th>Company</th><th class="num">Mkt cap</th>
        <th>Why it is here</th><th class="num">Score</th></tr>${body}</table>`;
  $("st-cmp-suggested").querySelectorAll("tr[data-sym] input").forEach((box) => {
    box.onchange = () => {
      const sym = box.closest("tr").dataset.sym;
      if (box.checked) addToGroup(sym); else removeFromGroup(sym);
    };
  });
}

function addToGroup(symbol) {
  const sym = String(symbol || "").trim().toUpperCase();
  if (!sym || sym === stSym || stCmpGroup.includes(sym)) return;
  if (stCmpGroup.length >= CMP_MAX) {
    $("st-cmp-note").textContent = `Eight columns is the most that stays readable — remove one first.`;
    return;
  }
  stCmpGroup.push(sym);
  stCmpEdited = true;
  savePeerGroup();
  renderCompareGroup();
  loadCompareViews();
}

function removeFromGroup(symbol) {
  stCmpGroup = stCmpGroup.filter((s) => s !== symbol);
  stCmpEdited = true;
  savePeerGroup();
  renderCompareGroup();
  loadCompareViews();
}

// One place that knows the group changed, so the three views below always
// agree about who is in it.
function loadCompareViews() {
  const group = [stSym, ...stCmpGroup];
  if (group.length < 2) {
    const nothing = `<div class="empty">Add a company to compare ${escapeHtml(stSym)} against.</div>`;
    $("st-cmp-table").innerHTML = nothing;
    $("st-cmp-mix").innerHTML = nothing;
    $("st-cmp-foot").innerHTML = "";
    if (charts["st-cmp-chart"]) { charts["st-cmp-chart"].destroy(); delete charts["st-cmp-chart"]; }
    return;
  }
  loadCompareRebased(group);
  loadCompareTable(group);
  loadCompareMix(group);
}

async function loadCompareRebased(group) {
  const symbolAtStart = stSym;
  try {
    const info = await loadRebasedChart("st-cmp-chart", group.map((s) => [s, s]),
                                        isoAgo(RANGE_DAYS["3Y"]));
    if (symbolAtStart !== stSym) return;
    $("st-cmp-chart-note").textContent = `${info.bars} sessions · ${info.series} series · ${info.provider}`;
  } catch (e) {
    if (symbolAtStart !== stSym) return;
    $("st-cmp-chart-note").textContent = e.message;
  }
}

async function loadCompareTable(group) {
  const symbolAtStart = stSym;
  $("st-cmp-table").innerHTML = `<div class="empty">Loading ${group.length} companies…</div>`;
  try {
    const d = await api(`/api/v1/equity/compare/table?symbol=${encodeURIComponent(group.join(","))}`);
    if (symbolAtStart !== stSym) return;
    renderCompareTable(d.results || [], d.extra || {}, d.warnings || []);
  } catch (e) {
    if (symbolAtStart !== stSym) return;
    $("st-cmp-table").innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
    $("st-cmp-foot").innerHTML = "";
  }
}

// Values arrive raw and each row says how it should be read, so one formatter
// serves market caps, multiples, percentages and ratios alike.
function cmpValue(value, shape) {
  if (value == null) return "—";
  if (shape === "money") return fmtBig(value);
  if (shape === "percent") return (value * 100).toFixed(1) + "%";
  if (shape === "multiple") return value.toFixed(1) + "×";
  return value.toFixed(2);
}

function renderCompareTable(rows, meta, warnings) {
  const symbols = meta.symbols || [];
  if (!rows.length || !symbols.length) {
    $("st-cmp-table").innerHTML = `<div class="empty">Nothing to compare.</div>`;
    return;
  }
  const head = `<tr><th></th>${symbols.map((s) =>
    `<th class="v ${s === meta.subject ? "cmp-subject" : ""}"
      title="${escapeHtml((meta.names || {})[s] || s)}">${escapeHtml(s)}</th>`).join("")}
    <th class="v cmp-median">Median</th></tr>`;

  let section = null;
  const body = rows.map((row) => {
    let header = "";
    if (row.section !== section) {
      section = row.section;
      header = `<tr class="fs-section"><td colspan="${symbols.length + 2}">${escapeHtml(section)}</td></tr>`;
    }
    // Returns are read against zero, so they keep the platform's colouring;
    // a multiple is not better for being bigger and stays plain.
    const signed = ["total_return", "cagr", "max_drawdown", "revenue_growth",
                    "earnings_growth"].includes(row.metric);
    const cells = symbols.map((s) => {
      const v = row[s];
      return `<td class="v ${s === meta.subject ? "cmp-subject" : ""} ${
        signed && v != null ? cls(v) : ""}">${escapeHtml(cmpValue(v, row.format))}</td>`;
    }).join("");
    return header + `<tr class="indent-1"><td class="lbl">${escapeHtml(row.label)}</td>${cells}
      <td class="v cmp-median">${escapeHtml(cmpValue(row.median, row.format))}</td></tr>`;
  }).join("");

  $("st-cmp-table").innerHTML = `<table><thead>${head}</thead><tbody>${body}</tbody></table>`;
  const window = meta.window || {};
  const notes = [
    `<span><b>Median</b> is of the peers, ${escapeHtml(meta.subject || "")} excluded.</span>`,
    `<span><b>Valuation and growth</b> are the vendor's trailing-twelve-month snapshot.</span>`,
    `<span><b>Returns, risk and correlation</b> are computed here from
      ${escapeHtml(String(window.observations || 0))} sessions since ${escapeHtml(window.start || "")}.</span>`,
  ];
  warnings.forEach((w) => notes.push(`<span>${escapeHtml(w)}</span>`));
  $("st-cmp-foot").innerHTML = notes.join("");
}

async function loadCompareMix(group) {
  const symbolAtStart = stSym;
  $("st-cmp-mix").innerHTML = `<div class="empty">Reading ${group.length} companies' filings…</div>`;
  try {
    const d = await api(`/api/v1/equity/compare/revenue_mix?symbol=${encodeURIComponent(group.join(","))}`);
    if (symbolAtStart !== stSym) return;
    renderCompareMix(d.results || [], d.extra || {}, group);
  } catch (e) {
    if (symbolAtStart !== stSym) return;
    $("st-cmp-mix").innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
}

// One bar per company, split by segment. Two companies in the same industry
// bucket earning their money in different places is the thing this panel is
// for, so the segment names sit on the bar rather than in a legend.
function renderCompareMix(rows, meta, group) {
  if (!rows.length) {
    $("st-cmp-mix").innerHTML = `<div class="empty">None of these companies files a revenue split.</div>`;
    return;
  }
  const bySymbol = new Map();
  rows.forEach((r) => {
    if (!bySymbol.has(r.symbol)) bySymbol.set(r.symbol, []);
    bySymbol.get(r.symbol).push(r);
  });
  const bars = group.filter((s) => bySymbol.has(s)).map((sym) => {
    const parts = bySymbol.get(sym);
    const segments = parts.map((p, i) => {
      const pct = (p.share || 0) * 100;
      const colour = PALETTE[i % PALETTE.length];
      return `<span class="cmp-seg" style="width:${pct.toFixed(2)}%;background:${colour}"
        title="${escapeHtml(p.segment)} · ${pct.toFixed(1)}% of revenue">
        <em>${escapeHtml(p.segment)}</em></span>`;
    }).join("");
    return `<div class="cmp-mixrow">
      <div class="cmp-mixhead"><b>${escapeHtml(sym)}</b>
        <span>${escapeHtml((parts[0] || {}).section || "")} · ${escapeHtml((parts[0] || {}).period_ending || "")}</span></div>
      <div class="cmp-bar">${segments}</div></div>`;
  }).join("");
  const missing = Object.keys(meta.missing || {});
  $("st-cmp-mix").innerHTML = bars + (missing.length
    ? `<div class="cmp-missing">No filed split for ${escapeHtml(missing.join(", "))}.</div>` : "");
  $("st-cmp-mix-note").textContent =
    `revenue split, newest filed year · ${escapeHtml(String((meta.covered || []).length))} of ${group.length} companies`;
}

$("st-cmp-add-form").onsubmit = (event) => {
  event.preventDefault();
  addToGroup($("st-cmp-add").value);
  $("st-cmp-add").value = "";
};
$("st-cmp-reset").onclick = async () => {
  await forgetPeerGroup();
  stCmpEdited = false;
  stCmpGroup = stCmpSuggested.slice(0, 4).map((r) => r.symbol);
  renderCompareGroup();
  loadCompareViews();
};

// ---------- multi-symbol comparison (rebased to 100) ----------
async function loadRebasedChart(canvasId, symbolMap, start) {
  const symbols = symbolMap.map(([s]) => s);
  const names = Object.fromEntries(symbolMap);
  const d = await api(`/api/v1/equity/price/historical?symbol=${encodeURIComponent(symbols.join(","))}` +
                      `&start_date=${start}`);
  const rows = d.results;
  const multi = rows.some((r) => r.symbol);
  const series = multi ? [...new Set(rows.map((r) => r.symbol))] : [symbols[0]];
  const dates = [...new Set(rows.map((r) => r.date))].sort();
  const closes = {};
  series.forEach((s) => { closes[s] = {}; });
  rows.forEach((r) => { closes[multi ? r.symbol : series[0]][r.date] = r.close; });
  const datasets = series.map((s, i) => {
    let base = null;
    return {
      label: names[s] || s, color: PALETTE[i % PALETTE.length],
      data: dates.map((dt) => {
        const v = closes[s][dt];
        if (v == null) return null;
        if (base === null) base = v;
        return (v / base) * 100;
      }),
    };
  });
  drawLine(canvasId, dates, datasets);
  return { provider: d.provider, bars: dates.length, series: series.length };
}

async function loadCompareChart() {
  setStatus("LOADING COMPARISON…");
  try {
    const pairs = $("cmp-symbols").value.trim().toUpperCase().split(",")
      .map((s) => [s.trim(), s.trim()]).filter(([s]) => s);
    const info = await loadRebasedChart("cmp-chart", pairs, $("cmp-start").value);
    $("cmp-src").textContent = `${info.provider} · ${info.bars} bars · ${info.series} series`;
    setStatus("COMPARISON LOADED");
  } catch (e) { setStatus("ERR: " + e.message); }
}
$("cmp-load").onclick = loadCompareChart;

// ---------- yield curve + spreads ----------
async function loadYieldCurve() {
  try {
    const d = await api("/api/v1/fixedincome/government/yield_curve");
    const rows = d.results;
    drawLine("mk-curve", rows.map((r) => r.maturity),
      [{ label: "par yield %", data: rows.map((r) => r.rate), color: "#00c805" }]);
    $("mk-curve-note").textContent = "as of " + (rows[0]?.date || "");
    const rate = (m) => rows.find((r) => r.maturity.toLowerCase() === m)?.rate;
    const two = rate("2 yr"), ten = rate("10 yr");
    const cards = [
      ["3M", rate("3 mo")], ["2Y", two], ["10Y", ten], ["30Y", rate("30 yr")],
      ["10Y-2Y", two != null && ten != null ? ten - two : null],
    ];
    $("mk-keyrates").innerHTML = cards.map(([k, v]) =>
      `<div class="metric"><div class="k">${k}</div>
       <div class="v ${k === "10Y-2Y" ? cls(v ?? 0) : ""}">${v == null ? "-" : v.toFixed(2) + "%"}</div></div>`
    ).join("");
  } catch (e) { $("mk-keyrates").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`; }
}

async function loadSpreads() {
  try {
    const start = new Date(); start.setFullYear(start.getFullYear() - 2);
    const d = await api(`/api/v1/fixedincome/spreads?start_date=${start.toISOString().slice(0, 10)}`);
    const rows = d.results;
    const latest = (col) => { for (let i = rows.length - 1; i >= 0; i--) if (rows[i][col] != null) return rows[i][col]; return null; };
    const sampled = rows.filter((_, i) => i % 5 === 0 || i === rows.length - 1);
    drawLine("mk-spreads", sampled.map((r) => r.date), [
      { label: "10Y-2Y %", data: sampled.map((r) => r.T10Y2Y), color: "#5ac8fa" },
      { label: "HY OAS %", data: sampled.map((r) => r.BAMLH0A0HYM2), color: "#ff5000" },
    ]);
    const cards = [["10Y-2Y", latest("T10Y2Y")], ["10Y-3M", latest("T10Y3M")],
                   ["HY OAS", latest("BAMLH0A0HYM2")], ["IG OAS", latest("BAMLC0A0CM")],
                   ["BAA-10Y", latest("BAA10Y")]];
    $("mk-credit").innerHTML = cards.map(([k, v]) =>
      `<div class="metric"><div class="k">${k}</div><div class="v">${v == null ? "-" : v.toFixed(2) + "%"}</div></div>`
    ).join("");
  } catch (e) { $("mk-credit").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`; }
}

// ---------- federal reserve policy rate ----------
// The target is a step function, so everything here reads it as decisions —
// the moves, the cycles they group into, and where that leaves policy today —
// with the level series only as the backdrop.
let fedYears = 10;

const fedDay = (iso) => iso
  ? new Date(iso + "T00:00:00").toLocaleDateString("en-US",
      { month: "short", day: "numeric", year: "numeric" })
  : "-";
const fedBps = (bps) => (bps > 0 ? "+" : "") + Math.round(bps) + " bps";
const fedBand = (lo, hi) => lo == null || hi == null ? "-"
  : lo === hi ? lo.toFixed(2) + "%" : `${lo.toFixed(2)}-${hi.toFixed(2)}%`;

function loadFedPolicy() {
  loadFedStance();
  loadFedPath();
  loadFedDecisions();
}

async function loadFedStance() {
  try {
    const s = (await api("/api/v1/economy/fed/stance")).results;
    const held = s.days_since_last_move;
    const cards = [
      ["Target range", fedBand(s.target_lower, s.target_upper), `set ${fedDay(s.last_move_date)}`],
      ["Effective rate", s.effective_rate == null ? "-" : s.effective_rate.toFixed(2) + "%",
        "where fed funds actually trade"],
      ["Last move", fedBps(s.last_move_bps), `${s.last_move} · ${fedDay(s.last_move_date)}`],
      ["On hold", held + (held === 1 ? " day" : " days"), "since that decision"],
      ["This cycle", fedBps(s.cycle_total_bps),
        `${s.cycle_moves} move${s.cycle_moves === 1 ? "" : "s"} from ${s.cycle_from_rate.toFixed(2)}%`],
      ["Real policy rate", s.real_policy_rate == null ? "-" : s.real_policy_rate.toFixed(2) + "%",
        s.core_pce_yoy == null ? "midpoint less core PCE"
          : `midpoint less core PCE ${s.core_pce_yoy.toFixed(1)}%`],
      ["2Y vs target", s.two_year_minus_midpoint == null ? "-"
        : (s.two_year_minus_midpoint >= 0 ? "+" : "") + s.two_year_minus_midpoint.toFixed(2),
        s.two_year_yield == null ? "no 2-year yield" : `2-year at ${s.two_year_yield.toFixed(2)}%`],
      ["Next meeting", s.next_meeting ? fedDay(s.next_meeting) : "-",
        s.next_meeting ? `in ${s.days_to_next_meeting} days${s.next_meeting_projections ? " · dot plot" : ""}`
          : "calendar unavailable"],
    ];
    $("fed-stance").innerHTML = cards.map(([k, v, note]) =>
      `<div class="metric"><div class="k">${escapeHtml(k)}</div><div class="v">${escapeHtml(v)}</div>
       <div class="note">${escapeHtml(note)}</div></div>`).join("");
    $("fed-note").textContent = `as of ${s.as_of} · ${s.cycle}`;
    $("fed-explain").textContent = fedSentence(s);
  } catch (e) {
    $("fed-stance").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`;
  }
}

/** The stance in a sentence, including what the 2-year says about the next move. */
function fedSentence(s) {
  const gap = s.two_year_minus_midpoint;
  const market = gap == null ? ""
    : gap > 0.25 ? " The 2-year Treasury trades above the midpoint, which is the market leaning towards higher rates."
    : gap < -0.25 ? " The 2-year Treasury trades below the midpoint, which is the market pricing cuts."
    : " The 2-year Treasury sits close to the midpoint, so the market expects policy to stay about here.";
  return `The target range is ${fedBand(s.target_lower, s.target_upper)}, unchanged for `
    + `${s.days_since_last_move} days after a ${Math.abs(Math.round(s.last_move_bps))}bp `
    + `${s.last_move} on ${fedDay(s.last_move_date)} — move ${s.cycle_moves} of the ${s.cycle}, `
    + `which has taken the target ${Math.abs(Math.round(s.cycle_total_bps))}bp `
    + `${s.cycle_kind === "tightening" ? "up" : "down"} from ${s.cycle_from_rate.toFixed(2)}%.`
    + market;
}

async function loadFedPath() {
  setStatus("LOADING FED POLICY PATH…");
  try {
    const params = new URLSearchParams();
    if (fedYears) {
      const start = new Date();
      start.setFullYear(start.getFullYear() - fedYears);
      params.set("start_date", start.toISOString().slice(0, 10));
    } else {
      params.set("start_date", "1982-01-01");   // the target series starts here
    }
    // Long windows are drawn from weekly observations: the daily series is
    // 16,000 rows of a step function, and the step survives the thinning.
    if (fedYears === 0 || fedYears > 10) params.set("frequency", "w");
    const rows = (await api(`/api/v1/economy/fed/policy_rate?${params}`)).results;
    const step = Math.max(1, Math.ceil(rows.length / 700));
    const sampled = rows.filter((_, i) => i % step === 0 || i === rows.length - 1);
    drawLine("fed-chart", sampled.map((r) => r.date), [
      { label: "target upper %", data: sampled.map((r) => r.target_upper), color: "#00c805" },
      { label: "effective %", data: sampled.map((r) => r.effective_rate), color: "#5ac8fa" },
    ]);
    setStatus("FED POLICY PATH LOADED");
  } catch (e) { setStatus("ERR: " + e.message); }
}

async function loadFedDecisions() {
  try {
    const moves = (await api("/api/v1/economy/fed/rate_changes?limit=8")).results;
    $("fed-moves").innerHTML = `<table><tr><th>Effective</th><th>Move</th><th>To</th><th>Cycle</th></tr>` +
      moves.map((m) => `<tr><td>${escapeHtml(fedDay(m.date))}</td>
        <td class="mono ${m.change_bps >= 0 ? "pos" : "neg"}">${fedBps(m.change_bps)}</td>
        <td class="mono">${escapeHtml(fedBand(m.target_lower, m.target_upper))}</td>
        <td>${escapeHtml(m.cycle || "-")}</td></tr>`).join("") + `</table>`;
  } catch (e) { $("fed-moves").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`; }

  try {
    const cycles = (await api("/api/v1/economy/fed/cycles")).results.slice(0, 8);
    $("fed-cycles").innerHTML = `<table><tr><th>Cycle</th><th>Moves</th><th>Total</th><th>Path</th>
      <th>Months</th><th>Then held</th></tr>` +
      cycles.map((c) => `<tr><td>${escapeHtml(c.cycle)}</td>
        <td class="mono">${c.moves}</td>
        <td class="mono ${c.total_bps >= 0 ? "pos" : "neg"}">${fedBps(c.total_bps)}</td>
        <td class="mono">${c.from_rate.toFixed(2)} → ${c.to_rate.toFixed(2)}%</td>
        <td class="mono">${c.months.toFixed(0)}</td>
        <td class="mono">${Math.round(c.hold_days / 30.44)} mo${c.status === "current" ? " so far" : ""}</td>
      </tr>`).join("") + `</table>`;
  } catch (e) { $("fed-cycles").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`; }
}

// ---------- fed projections, balance sheet and communications ----------
// Each tab pays for its own data the first time it is opened: the SEP, the
// H.4.1 and the Board's feeds are three more round trips that a reader looking
// at the rate path has not asked for.
const fedLoaded = new Set();

const fedArrow = (change) => change == null || change === 0 ? ""
  : `<span class="${change > 0 ? "pos" : "neg"}">${change > 0 ? "▲" : "▼"}${Math.abs(change).toFixed(2)}</span>`;

async function loadFedProjections() {
  try {
    const [sep, dots] = await Promise.all([
      api("/api/v1/economy/fed/projections"),
      api("/api/v1/economy/fed/dot_plot"),
    ]);
    // The SEP arrives long — one row per variable per horizon — and reads as a
    // grid, so it is pivoted here rather than server-side, where the long shape
    // is what an API caller wants.
    const horizons = [...new Set(sep.results.map((r) => r.horizon))];
    const variables = [...new Set(sep.results.map((r) => r.variable))];
    const cell = (v, h) => sep.results.find((r) => r.variable === v && r.horizon === h);
    $("fed-sep").innerHTML = `<table><tr><th>Projection</th>${horizons.map((h) =>
      `<th>${escapeHtml(h)}</th>`).join("")}</tr>` +
      variables.map((v) => `<tr><td>${escapeHtml(v)}</td>` + horizons.map((h) => {
        const c = cell(v, h);
        return `<td class="mono">${c ? escapeHtml(String(c.median ?? "-")) : "-"}
          ${c ? fedArrow(c.change) : ""}</td>`;
      }).join("") + `</tr>`).join("") + `</table>`;
    $("fed-sep-note").textContent = `Summary of Economic Projections published at the `
      + `${fedDay(sep.extra.meeting)} meeting; the arrow is the revision against the `
      + `${sep.extra.previous_projection || "previous"} projections.`;

    const info = dots.extra.horizons || {};
    const dotHorizons = Object.keys(info);
    const rates = [...new Set(dots.results.map((r) => r.rate))].sort((a, b) => b - a);
    const count = (rate, h) => dots.results.find((r) => r.rate === rate && r.horizon === h)?.participants;
    $("fed-dots").innerHTML = `<table><tr><th>Rate</th>${dotHorizons.map((h) =>
      `<th>${escapeHtml(h)}</th>`).join("")}</tr>` +
      rates.map((rate) => `<tr><td class="mono">${rate.toFixed(3)}%</td>` +
        dotHorizons.map((h) => {
          const n = count(rate, h);
          return `<td class="mono">${n ? "●".repeat(Math.min(n, 10)) + (n > 10 ? ` ${n}` : "") : ""}</td>`;
        }).join("") + `</tr>`).join("") +
      `<tr><td><b>Median</b></td>${dotHorizons.map((h) =>
        `<td class="mono"><b>${info[h].sep_median ?? info[h].median_dot}%</b> ${fedArrow(info[h].change)}</td>`
      ).join("")}</tr></table>`;
  } catch (e) {
    $("fed-sep").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`;
    $("fed-dots").innerHTML = "";
  }
}

async function loadFedSheet() {
  try {
    const start = new Date(); start.setFullYear(start.getFullYear() - 12);
    const d = await api(`/api/v1/economy/fed/balance_sheet?start_date=${start.toISOString().slice(0, 10)}`);
    const x = d.extra;
    const tn = (bn) => bn == null ? "-" : "$" + (bn / 1000).toFixed(2) + "tn";
    const cards = [
      ["Total assets", tn(x.total_assets_bn), `as of ${x.as_of}`],
      ["Treasuries", tn(x.treasuries_bn), "held outright"],
      ["MBS", tn(x.mbs_bn), "agency mortgage-backed"],
      ["Bank reserves", tn(x.reserves_bn), "held at the Fed"],
      ["13-week change", (x.change_13w_bn >= 0 ? "+" : "") + "$" + Math.abs(x.change_13w_bn).toFixed(0) + "bn",
        `${x.monthly_pace_bn >= 0 ? "+" : "−"}$${Math.abs(x.monthly_pace_bn).toFixed(0)}bn a month`],
      ["Off the peak", "$" + Math.abs(x.off_peak_bn / 1000).toFixed(2) + "tn", `peak ${tn(x.peak_assets_bn)}`],
    ];
    $("fed-sheet-metrics").innerHTML = cards.map(([k, v, note]) =>
      `<div class="metric"><div class="k">${escapeHtml(k)}</div><div class="v">${escapeHtml(v)}</div>
       <div class="note">${escapeHtml(note)}</div></div>`).join("");
    $("fed-sheet-note").textContent = `The balance sheet is ${x.regime}. `
      + `Quantitative tightening is a runoff rate rather than an announcement, so the pace `
      + `column is the policy.`;
    const rows = d.results.filter((_, i, all) => i % Math.max(1, Math.ceil(all.length / 500)) === 0);
    drawLine("fed-sheet-chart", rows.map((r) => r.date), [
      { label: "total assets $bn", data: rows.map((r) => r.total_assets), color: "#00c805" },
      { label: "treasuries $bn", data: rows.map((r) => r.treasuries), color: "#5ac8fa" },
      { label: "MBS $bn", data: rows.map((r) => r.mbs), color: "#c084fc" },
    ]);
  } catch (e) { $("fed-sheet-metrics").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`; }

  try {
    const d = await api("/api/v1/economy/fed/liquidity?start_date=2007-01-01");
    const facilities = d.extra.facilities || {};
    $("fed-liquidity").innerHTML = `<table><tr><th>Facility</th><th>Now</th><th>Peak</th>
      <th>Peak date</th><th>Status</th></tr>` +
      Object.entries(facilities).map(([name, f]) => `<tr>
        <td>${escapeHtml(name.replace(/_/g, " "))}</td>
        <td class="mono">${f.latest_bn == null ? "-" : "$" + f.latest_bn.toFixed(1) + "bn"}</td>
        <td class="mono">$${f.peak_bn.toFixed(0)}bn</td>
        <td class="mono">${escapeHtml(f.peak_date)}</td>
        <td class="${f.elevated ? "neg" : ""}">${f.elevated ? "elevated" : "quiet"}</td></tr>`).join("") +
      `</table><div class="hintline" style="margin-top:8px">${escapeHtml(d.extra.reading)}. Emergency
       lending is announced in a press release and measured here — a spike is the response.</div>`;
  } catch (e) { $("fed-liquidity").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`; }
}

async function loadFedSpeak() {
  try {
    const d = await api("/api/v1/economy/fed/statement");
    const s = d.results;
    const chips = (list, cls) => (list || []).map((p) =>
      `<span class="badge ${cls}">${escapeHtml(p.phrase)}</span>`).join(" ");
    const diff = (lines, sign, cls) => (lines || []).length
      ? `<div class="fed-diff">${lines.map((l) =>
          `<div class="${cls}">${sign} ${escapeHtml(l)}</div>`).join("")}</div>`
      : "";
    $("fed-statement").innerHTML = `
      <div class="hintline" style="margin-bottom:10px">
        ${escapeHtml(fedDay(s.meeting))} · vote ${s.votes_for ?? "?"}–${s.votes_against ?? "?"}
        ${s.unanimous ? "(unanimous)" : ""} ·
        <a href="${escapeHtml(s.url)}" target="_blank" rel="noopener">full statement</a></div>
      ${s.dissent ? `<p class="explain" style="margin:0 0 10px">${escapeHtml(s.dissent)}</p>` : ""}
      <div style="margin-bottom:10px">${chips(s.hawkish_phrases, "pos")} ${chips(s.dovish_phrases, "neg")}
        ${chips(s.guidance_phrases, "")}</div>
      ${s.compared_with ? `<div class="hintline">Against ${escapeHtml(fedDay(s.compared_with))}:</div>` : ""}
      ${diff(s.sentences_added, "+", "pos")}
      ${diff(s.sentences_removed, "−", "neg")}
      ${s.unchanged ? `<div class="hintline">The wording did not change.</div>` : ""}`;
  } catch (e) { $("fed-statement").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`; }

  try {
    const d = await api("/api/v1/economy/fed/communications?days=120&limit=14");
    $("fed-speeches").innerHTML = d.results.map((r) => {
      const tags = [r.congressional ? "congressional testimony" : null,
                    r.jackson_hole ? "Jackson Hole" : null,
                    r.off_calendar ? "between meetings" : null,
                    r.kind].filter(Boolean).join(" · ");
      return `<a class="ws-news-item" href="${escapeHtml(r.url || "#")}" target="_blank" rel="noopener">
        <span class="ws-news-title">${escapeHtml(r.speaker ? r.speaker + ": " + r.title : r.title)}</span>
        <span class="ws-news-meta"><span>${escapeHtml(tags)}</span><span>${escapeHtml(r.date)}</span></span>
      </a>`;
    }).join("") || `<div class="empty">No Fed communications in the window.</div>`;
  } catch (e) { $("fed-speeches").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`; }
}

const FED_TAB_LOADERS = { projections: loadFedProjections, sheet: loadFedSheet, speak: loadFedSpeak };

document.querySelectorAll("#fed-tabs .tab").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("#fed-tabs .tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".fedtab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $("fedtab-" + b.dataset.fedtab).classList.add("active");
    const load = FED_TAB_LOADERS[b.dataset.fedtab];
    if (load && !fedLoaded.has(b.dataset.fedtab)) {
      fedLoaded.add(b.dataset.fedtab);
      load();
    }
  };
});

$("fed-refresh").onclick = () => {
  fedLoaded.forEach((tab) => FED_TAB_LOADERS[tab] && FED_TAB_LOADERS[tab]());
  loadFedPolicy();
};
document.querySelectorAll("#fed-ranges .chip").forEach((chip) => {
  chip.onclick = () => {
    document.querySelectorAll("#fed-ranges .chip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    fedYears = Number(chip.dataset.fedrange);
    loadFedPath();
  };
});

// ---------- sectors & assets tabs ----------
const GROUP_WINDOWS = [["one_day", "1D"], ["one_week", "1W"], ["one_month", "1M"],
                       ["three_month", "3M"], ["six_month", "6M"], ["ytd", "YTD"], ["one_year", "1Y"]];

function renderGroupTable(elId, rows, { firstCol = "Group", onRowClick = null } = {}) {
  const maxAbs = Math.max(...rows.map((r) => Math.abs(r.ytd ?? 0)), 0.0001);
  const pct = (v) => v == null ? "<td>-</td>" : `<td class="${cls(v)}">${fmtPct(v)}</td>`;
  $(elId).innerHTML = `<table class="${onRowClick ? "clickrows" : ""}"><tr><th>${firstCol}</th><th></th>` +
    GROUP_WINDOWS.map(([, l]) => `<th>${l}</th>`).join("") + "</tr>" +
    rows.map((r, i) => {
      const w = Math.round(Math.abs(r.ytd ?? 0) / maxAbs * 70);
      return `<tr data-i="${i}"><td>${r.group} <span class="badge">${r.symbol}</span></td>
        <td style="width:80px"><div class="perfbar"><div class="bar ${(r.ytd ?? 0) >= 0 ? "pos" : "neg"}"
            style="width:${w}px"></div></div></td>` +
        GROUP_WINDOWS.map(([k]) => pct(r[k])).join("") + "</tr>";
    }).join("") + "</table>";
  if (onRowClick) {
    $(elId).querySelectorAll("tr[data-i]").forEach((tr) => {
      tr.onclick = () => onRowClick(rows[+tr.dataset.i]);
    });
  }
}

const SECTOR_ETFS = [["XLK", "Technology"], ["XLV", "Health Care"], ["XLF", "Financials"],
  ["XLE", "Energy"], ["XLY", "Cons Discretionary"], ["XLP", "Cons Staples"],
  ["XLI", "Industrials"], ["XLB", "Materials"], ["XLU", "Utilities"],
  ["XLRE", "Real Estate"], ["XLC", "Communication"]];
const SECTOR_KEYS = {
  "Technology": "technology", "Health Care": "healthcare", "Financials": "financial-services",
  "Energy": "energy", "Consumer Discretionary": "consumer-cyclical",
  "Consumer Staples": "consumer-defensive", "Industrials": "industrials",
  "Materials": "basic-materials", "Utilities": "utilities", "Real Estate": "real-estate",
  "Communication Services": "communication-services",
};
const ASSET_ETFS = [["SPY", "US Equities"], ["EFA", "Intl Developed"], ["EEM", "Emerging Mkts"],
  ["AGG", "US Agg Bonds"], ["TLT", "Long Treasuries"], ["HYG", "High Yield"], ["TIP", "TIPS"],
  ["GLD", "Gold"], ["DBC", "Commodities"], ["VNQ", "REITs"], ["UUP", "US Dollar"]];

let sectorsLoaded = false, assetsLoaded = false;

// Kept so the concentration table below can hand a clicked ETF back to
// openSector(), which wants the performance row rather than a bare ticker.
let scRows = [];

async function loadSectors(force) {
  if (sectorsLoaded && !force) return;
  const firstTime = !sectorsLoaded;
  sectorsLoaded = true;
  if (firstTime || force) { loadSectorChart(); loadSectorConcentration(); }
  $("sc-table").innerHTML = `<div class="empty">Loading sector returns…</div>`;
  try {
    const d = await api("/api/v1/equity/compare/groups?group=sector");
    scRows = d.results || [];
    renderGroupTable("sc-table", scRows, { firstCol: "Sector", onRowClick: openSector });
  } catch (e) { $("sc-table").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`; }
}

// One row per sector fund, ranked by how much of it is its ten largest names.
async function loadSectorConcentration() {
  const symbols = SECTOR_ETFS.map(([s]) => s).join(",");
  try {
    const d = await api(`/api/v1/etf/basket/concentration?symbol=${encodeURIComponent(symbols)}`);
    const rows = d.results || [];
    if (!rows.length) throw new Error("no baskets returned");
    const label = Object.fromEntries(SECTOR_ETFS);
    $("sc-conc").innerHTML =
      `<table class="clickrows"><tr><th>Sector</th><th>Holdings</th><th>Largest</th>
        <th>Top 10</th><th></th><th>Half the fund is…</th><th>Really this many names</th></tr>` +
      rows.map((r) => `<tr data-sym="${escapeHtml(String(r.symbol))}">
        <td>${escapeHtml(label[r.symbol] || r.symbol)} <span class="badge">${escapeHtml(String(r.symbol))}</span></td>
        <td class="mono">${r.holdings ?? "-"}</td>
        <td>${escapeHtml(String(r.largest_holding || "-"))}
          <span class="dim mono">${pctWeight(r.largest_weight)}</span></td>
        <td class="mono">${pctWeight(r.top_10_weight)}</td>
        <td style="width:90px"><div class="perfbar"><div class="bar ${(r.top_10_weight ?? 0) > 0.6 ? "neg" : "pos"}"
            style="width:${Math.round((r.top_10_weight ?? 0) * 80)}px"></div></div></td>
        <td class="mono">${r.holdings_to_half ?? "-"} name${r.holdings_to_half === 1 ? "" : "s"}</td>
        <td class="mono">${r.effective_holdings != null ? r.effective_holdings.toFixed(0) : "-"}</td>
      </tr>`).join("") + "</table>" +
      `<p class="hintline">"Really this many names" is 1/HHI — the number of equally weighted
        holdings that would be this concentrated. It is always smaller, often by a lot, than the
        number of companies on the list.</p>`;
    $("sc-conc").querySelectorAll("tr[data-sym]").forEach((tr) => {
      tr.onclick = () => {
        const row = scRows.find((r) => r.symbol === tr.dataset.sym);
        if (row) openSector(row);
      };
    });
  } catch (e) { $("sc-conc").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`; }
}
async function loadSectorChart() {
  setStatus("LOADING SECTOR CHART…");
  try {
    const info = await loadRebasedChart("sc-chart", SECTOR_ETFS, $("sc-start").value);
    setStatus(`SECTOR CHART · ${info.bars} BARS`);
  } catch (e) { setStatus("ERR: " + e.message); }
}
$("sc-refresh").onclick = () => { loadSectors(true); };
$("sc-load").onclick = loadSectorChart;

// ---------- sector page (expand into a sector) ----------
// Opened by clicking a row on the Sectors tab. The performance strip reuses
// the group-table row, so it renders instantly; everything else loads and
// fails independently.
let seRow = null, seRange = "1Y", seFrom = "sectors";

function sectorKey(group) {
  return SECTOR_KEYS[group] || String(group).toLowerCase().replace(/ /g, "-");
}

function openSector(row, from = "sectors") {
  seRow = row;
  seFrom = from;
  switchToView("view-sector");
  $("se-name").innerHTML = `${escapeHtml(row.group)}<small>${escapeHtml(row.symbol)} · SPDR sector ETF</small>`;
  $("se-price").textContent = "";
  $("se-chg").textContent = "";
  $("se-cos-h").innerHTML = `BIGGEST ${escapeHtml(row.group.toUpperCase())} COMPANIES ` +
    `<span class="panel-note">by index weight · click a company to expand</span>`;
  ["se-about", "se-news", "se-cos"].forEach((id) => {
    $(id).innerHTML = `<div class="empty">Loading…</div>`;
  });
  $("se-perf").innerHTML = GROUP_WINDOWS.map(([k, l]) =>
    `<div class="metric"><div class="k">${l}</div>` +
    `<div class="v ${cls(row[k] ?? 0)}">${row[k] == null ? "-" : fmtPct(row[k])}</div></div>`).join("");
  loadSectorPageChart();
  loadSectorQuote();
  loadSectorAbout();
  loadSectorCompanies();
  loadSectorNews();
  resetBasket(row.symbol);
}

$("se-back").onclick = () => document.querySelector(`.navbtn[data-view="${seFrom}"]`).click();
document.querySelectorAll("#se-ranges .chip").forEach((c) => {
  c.onclick = () => {
    document.querySelectorAll("#se-ranges .chip").forEach((x) => x.classList.remove("active"));
    c.classList.add("active");
    seRange = c.dataset.range;
    loadSectorPageChart();
  };
});

async function loadSectorPageChart() {
  try {
    const info = await loadRebasedChart("se-chart",
      [[seRow.symbol, seRow.group], ["SPY", "S&P 500"]], isoAgo(RANGE_DAYS[seRange]));
    setStatus(`${seRow.symbol} VS SPY · ${info.bars} BARS`);
  } catch (e) { setStatus("ERR: " + e.message); }
}

async function loadSectorQuote() {
  try {
    const d = await api(`/api/v1/equity/price/quote?symbol=${encodeURIComponent(seRow.symbol)}`);
    const q = one(d.results);
    if (!q || q.last_price == null) return;
    $("se-price").textContent = "$" + q.last_price.toFixed(2);
    if (q.change_percent != null) {
      $("se-chg").textContent = `${q.change_percent >= 0 ? "▲ +" : "▼ "}` +
        `${Math.abs(q.change_percent * 100).toFixed(2)}% today · via ${seRow.symbol}`;
      $("se-chg").className = "mono " + (q.change_percent >= 0 ? "pos" : "neg");
    }
  } catch { /* price header is optional */ }
}

async function loadSectorAbout() {
  try {
    const d = await api(`/api/v1/equity/compare/sector_overview?sector=${encodeURIComponent(sectorKey(seRow.group))}`);
    const o = d.results || {};
    const stats = [
      ["Share of US market", o.market_weight != null ? (o.market_weight * 100).toFixed(1) + "%" : "-"],
      ["Market cap", fmtBig(o.market_cap)],
      ["Companies", o.companies_count != null ? Number(o.companies_count).toLocaleString() : "-"],
      ["Industries", o.industries_count ?? "-"],
      ["Employees", fmtBig(o.employee_count)],
    ];
    $("se-about").innerHTML =
      `<p class="explain" style="margin-top:0">${escapeHtml(String(o.description || "No description available."))}</p>
       <div class="metrics">` + stats.map(([k, v]) =>
        `<div class="metric"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("") + `</div>`;
  } catch (e) { $("se-about").innerHTML = `<div class="empty">No overview available (${escapeHtml(e.message)})</div>`; }
}

async function loadSectorCompanies() {
  try {
    const d = await api(`/api/v1/equity/compare/sector_companies?sector=${encodeURIComponent(sectorKey(seRow.group))}&limit=25`);
    const rows = d.results || [];
    if (!rows.length) throw new Error("no companies returned");
    const maxW = Math.max(...rows.map((r) => r.market_weight ?? 0), 0.0001);
    $("se-cos").innerHTML = `<table class="clickrows"><tr><th>#</th><th>Company</th><th>Weight</th><th></th><th>Analyst rating</th></tr>` +
      rows.map((r, i) => `<tr data-sym="${escapeHtml(String(r.symbol || ""))}">
        <td class="dim">${i + 1}</td>
        <td>${escapeHtml(String(r.name || r.symbol || ""))} <span class="badge">${escapeHtml(String(r.symbol || ""))}</span></td>
        <td class="mono">${r.market_weight != null ? (r.market_weight * 100).toFixed(1) + "%" : "-"}</td>
        <td style="width:90px"><div class="perfbar"><div class="bar pos" style="width:${Math.round((r.market_weight ?? 0) / maxW * 80)}px"></div></div></td>
        <td>${escapeHtml(String(r.rating || "-"))}</td></tr>`).join("") + "</table>";
    $("se-cos").querySelectorAll("tr[data-sym]").forEach((tr) => {
      tr.onclick = () => tr.dataset.sym && openStock(tr.dataset.sym, "sector");
    });
  } catch (e) { $("se-cos").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`; }
}

async function loadSectorNews() {
  try {
    const d = await api(`/api/v1/news/search?query=${encodeURIComponent(seRow.group + " sector stocks")}&limit=6`);
    const rows = d.results || [];
    if (!rows.length) throw new Error("no stories");
    $("se-news").innerHTML = rows.map((r) => `
      <div class="feed-item">
        <div class="feed-meta">
          ${r.source ? `<span class="src-badge">${escapeHtml(String(r.source))}</span>` : ""}
          <span>${timeAgo(r.date)}</span>
        </div>
        ${r.url
          ? `<a class="feed-title" href="${escapeHtml(String(r.url))}" target="_blank" rel="noopener">${escapeHtml(String(r.title || ""))}</a>`
          : `<span class="feed-title">${escapeHtml(String(r.title || ""))}</span>`}
      </div>`).join("");
  } catch { $("se-news").innerHTML = `<div class="empty">No recent stories found.</div>`; }
}

// ---------- inside the ETF (the sector page's second tab) ----------
// Everything here reads the fund sponsor's own daily basket rather than a
// vendor's top-ten summary, so the panels are heavier than the overview's and
// none of them load until the tab is actually opened.
const SB_WINDOWS = { "1M": 31, "3M": 92, "6M": 183, "1Y": 366 };
let sbSymbol = null, sbRange = "3M", sbLines = [], sbLoaded = false;

function resetBasket(symbol) {
  sbSymbol = symbol;
  sbLines = [];
  sbLoaded = false;
  document.querySelectorAll("#sx-tabs .tab").forEach((x, i) => x.classList.toggle("active", i === 0));
  $("sxtab-overview").classList.add("active");
  $("sxtab-basket").classList.remove("active");
  $("sb-showall").checked = false;
  $("sb-filter").value = "";
  fillOverlapChoices(symbol);
}

document.querySelectorAll("#sx-tabs .tab").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("#sx-tabs .tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".sxtab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $("sxtab-" + b.dataset.sxtab).classList.add("active");
    if (b.dataset.sxtab === "basket") loadBasket();
  };
});

$("sb-refresh").onclick = () => loadBasket(true);
$("sb-filter").oninput = () => renderBasketHoldings();
$("sb-showall").onchange = () => loadBasketHoldings();
$("sb-vs").onchange = () => loadBasketOverlap();
document.querySelectorAll("#sb-ranges .chip").forEach((c) => {
  c.onclick = () => {
    document.querySelectorAll("#sb-ranges .chip").forEach((x) => x.classList.remove("active"));
    c.classList.add("active");
    sbRange = c.dataset.range;
    loadBasketContribution();
  };
});

// A basket can only be compared with another fund whose sponsor publishes one,
// so the list is the SPDR funds this terminal already knows about.
function fillOverlapChoices(symbol) {
  const options = [["SPY", "S&P 500 (SPY)"]].concat(
    SECTOR_ETFS.filter(([s]) => s !== symbol).map(([s, n]) => [s, `${n} (${s})`]));
  $("sb-vs").innerHTML = options.map(([s, n]) =>
    `<option value="${escapeHtml(s)}">${escapeHtml(n)}</option>`).join("");
}

function loadBasket(force) {
  if (!sbSymbol || (sbLoaded && !force)) return;
  sbLoaded = true;
  loadBasketHoldings();
  loadBasketConcentration();
  loadBasketIndustries();
  loadBasketContribution();
  loadBasketOverlap();
}

// ---- the holdings themselves ----
async function loadBasketHoldings() {
  const showAll = $("sb-showall").checked;
  $("sb-holdings").innerHTML = `<div class="empty">Reading ${escapeHtml(sbSymbol)}'s published basket…</div>`;
  try {
    const d = await api(`/api/v1/etf/basket/holdings?symbol=${encodeURIComponent(sbSymbol)}` +
                        `&limit=600&line_type=${showAll ? "all" : "equity"}`);
    sbLines = d.results || [];
    const x = d.extra || {};
    $("sb-asof").textContent = x.as_of ? `as of ${x.as_of} · published by the fund` : "";
    $("sb-hold-note").textContent =
      `${x.holdings ?? sbLines.length} holdings · ${pctWeight(x.equity_weight, 1)} of the fund` +
      (x.cash_weight ? ` · ${pctWeight(x.cash_weight)} cash` : "");
    renderBasketHoldings();
    drawBasketCurve();
  } catch (e) { $("sb-holdings").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`; }
}

// A Lorenz-style curve: cumulative weight against rank, with the equal-weight
// diagonal for reference. The gap between the two lines is the concentration.
function drawBasketCurve() {
  const rows = sbLines.filter((r) => r.line_type === "equity");
  if (!rows.length) return;
  const total = rows.reduce((a, r) => a + (r.weight || 0), 0);
  const labels = [""], actual = [0], even = [0];
  let running = 0;
  rows.forEach((r, i) => {
    running += r.weight || 0;
    labels.push(String(i + 1));
    actual.push(running * 100);
    even.push((i + 1) / rows.length * total * 100);
  });
  drawLine("sb-curve", labels, [
    { label: `${sbSymbol} — heaviest holding first`, data: actual, color: PALETTE[0], fill: true },
    { label: "if every holding were the same size", data: even, color: PALETTE[5], dash: true },
  ]);
}

function renderBasketHoldings() {
  const q = $("sb-filter").value.trim().toLowerCase();
  const rows = q
    ? sbLines.filter((r) => [r.symbol, r.name, r.industry].some((v) => String(v || "").toLowerCase().includes(q)))
    : sbLines;
  if (!rows.length) {
    $("sb-holdings").innerHTML = `<div class="empty">Nothing in the basket matches "${escapeHtml(q)}".</div>`;
    return;
  }
  const maxW = Math.max(...rows.map((r) => r.weight ?? 0), 0.0001);
  $("sb-holdings").innerHTML =
    `<table class="clickrows"><tr><th>#</th><th>Holding</th><th>Industry</th><th>Weight</th><th></th>
      <th>Running total</th><th>Shares held</th></tr>` +
    rows.map((r) => `<tr ${r.line_type === "equity" ? `data-sym="${escapeHtml(String(r.symbol))}"` : ""}>
      <td class="dim">${r.rank}</td>
      <td>${escapeHtml(String(r.name || ""))}
        ${r.symbol ? `<span class="badge">${escapeHtml(String(r.symbol))}</span>` : ""}
        ${r.line_type !== "equity" ? `<span class="badge">${escapeHtml(String(r.line_type))}</span>` : ""}</td>
      <td class="dim">${escapeHtml(String(r.industry || "—"))}</td>
      <td class="mono ${(r.weight ?? 0) < 0 ? "neg" : ""}">${pctWeight(r.weight)}</td>
      <td style="width:90px"><div class="perfbar"><div class="bar pos"
          style="width:${Math.round(Math.abs(r.weight ?? 0) / maxW * 80)}px"></div></div></td>
      <td class="mono dim">${pctWeight(r.cumulative_weight, 1)}</td>
      <td class="mono dim">${fmtBig(r.shares_held)}</td></tr>`).join("") + "</table>";
  $("sb-holdings").querySelectorAll("tr[data-sym]").forEach((tr) => {
    tr.onclick = () => tr.dataset.sym && openStock(tr.dataset.sym, "sector");
  });
}

// ---- how concentrated it is ----
async function loadBasketConcentration() {
  $("sb-stats").innerHTML = `<div class="empty">Reading the published basket…</div>`;
  try {
    const d = await api(`/api/v1/etf/basket/concentration?symbol=${encodeURIComponent(sbSymbol)}`);
    const c = one(d.results);
    if (!c) throw new Error("no basket returned");
    const stats = [
      ["Holdings", c.holdings ?? "-"],
      ["Largest", `${c.largest_holding || "-"} ${pctWeight(c.largest_weight, 1)}`],
      ["Top 5", pctWeight(c.top_5_weight, 1)],
      ["Top 10", pctWeight(c.top_10_weight, 1)],
      ["Half the fund is", `${c.holdings_to_half} name${c.holdings_to_half === 1 ? "" : "s"}`],
      ["Really this many", c.effective_holdings != null ? c.effective_holdings.toFixed(0) : "-"],
      ["Median holding", pctWeight(c.median_weight)],
      ["Cash", pctWeight(c.cash_weight)],
    ];
    $("sb-stats").innerHTML = stats.map(([k, v]) =>
      `<div class="metric"><div class="k">${escapeHtml(k)}</div><div class="v">${escapeHtml(String(v))}</div></div>`).join("");
    $("sb-read").textContent =
      `${c.holdings} companies, but half the money sits in ${c.holdings_to_half} of them and the ` +
      `ten largest are ${pctWeight(c.top_10_weight, 0)} of the fund. Spread evenly, that is the same ` +
      `concentration as ${Math.round(c.effective_holdings)} equal positions — so a move in ` +
      `${c.largest_holding}, at ${pctWeight(c.largest_weight, 1)}, is a move in the sector.`;
  } catch (e) {
    $("sb-stats").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`;
    $("sb-read").textContent = "";
  }
}

// ---- what the sector label actually covers ----
async function loadBasketIndustries() {
  $("sb-inds").innerHTML = `<div class="empty">Loading…</div>`;
  try {
    const d = await api(`/api/v1/etf/basket/industries?symbol=${encodeURIComponent(sbSymbol)}`);
    const rows = (d.results || []).slice(0, 12);
    if (!rows.length) throw new Error("no industry breakdown");
    const maxW = Math.max(...rows.map((r) => r.weight ?? 0), 0.0001);
    $("sb-inds").innerHTML = `<table><tr><th>Industry</th><th>Weight</th><th></th><th>Names</th></tr>` +
      rows.map((r) => `<tr>
        <td title="${escapeHtml(String(r.members || ""))}">${escapeHtml(String(r.industry || "—"))}</td>
        <td class="mono">${pctWeight(r.weight, 1)}</td>
        <td style="width:70px"><div class="perfbar"><div class="bar pos"
            style="width:${Math.round((r.weight ?? 0) / maxW * 60)}px"></div></div></td>
        <td class="dim mono">${r.holdings}</td></tr>`).join("") + "</table>" +
      `<p class="hintline">Hover an industry to see the tickers in it.</p>`;
  } catch (e) { $("sb-inds").innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`; }
}

// ---- which holdings produced the move ----
async function loadBasketContribution() {
  if (!sbSymbol) return;
  ["sb-up", "sb-down"].forEach((id) => {
    $(id).innerHTML = `<div class="empty">Pricing every holding over ${sbRange}…</div>`;
  });
  $("sb-attrib-read").textContent = "";
  try {
    const d = await api(`/api/v1/etf/basket/contribution?symbol=${encodeURIComponent(sbSymbol)}` +
                        `&start_date=${isoAgo(SB_WINDOWS[sbRange])}`);
    const rows = d.results || [];
    const x = d.extra || {};
    const pp = (v) => (v == null ? "-" : (v >= 0 ? "+" : "") + (v * 100).toFixed(1) + "pp");
    $("sb-attrib-stats").innerHTML = [
      ["Fund moved", x.fund_return == null ? "-" : fmtPct(x.fund_return, true), cls(x.fund_return ?? 0)],
      ["Explained by holdings", pp(x.total_contribution), cls(x.total_contribution ?? 0)],
      ["Top 5 names", pp(x.top_5_contribution), cls(x.top_5_contribution ?? 0)],
      ["Rose / fell", `${x.advancers} / ${x.decliners}`, ""],
      ["Unexplained", pp(x.unexplained), "dim"],
    ].map(([k, v, c]) =>
      `<div class="metric"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`).join("");
    $("sb-attrib-read").textContent =
      `Over ${sbRange} the fund returned ${fmtPct(x.fund_return, true)}. Its five largest ` +
      `contributors added ${pp(x.top_5_contribution)} of that between them, with ${x.decliners} of ` +
      `${x.priced_holdings} holdings falling. Weights are the ones each position started the window ` +
      `at, backed out of today's published weight — what the two totals do not agree on is index ` +
      `changes and the quarterly rebalance, which this decomposition cannot see.`;
    renderContribution("sb-up", rows.filter((r) => r.contribution > 0).slice(0, 10));
    renderContribution("sb-down", rows.filter((r) => r.contribution < 0).slice(-10).reverse());
  } catch (e) {
    ["sb-up", "sb-down"].forEach((id) => { $(id).innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`; });
  }
}

function renderContribution(elId, rows) {
  if (!rows.length) { $(elId).innerHTML = `<div class="empty">Nothing here over this window.</div>`; return; }
  const maxAbs = Math.max(...rows.map((r) => Math.abs(r.contribution)), 0.0001);
  $(elId).innerHTML = `<table class="clickrows"><tr><th>Holding</th><th>Weight</th><th>Return</th>
      <th>Added</th><th></th></tr>` +
    rows.map((r) => `<tr data-sym="${escapeHtml(String(r.symbol))}">
      <td>${escapeHtml(String(r.name || r.symbol))} <span class="badge">${escapeHtml(String(r.symbol))}</span></td>
      <td class="mono dim">${pctWeight(r.start_weight, 1)}</td>
      <td class="mono ${cls(r.return)}">${fmtPct(r.return, true)}</td>
      <td class="mono ${cls(r.contribution)}">${(r.contribution * 100).toFixed(2)}pp</td>
      <td style="width:70px"><div class="perfbar"><div class="bar ${cls(r.contribution)}"
          style="width:${Math.round(Math.abs(r.contribution) / maxAbs * 60)}px"></div></div></td>
    </tr>`).join("") + "</table>";
  $(elId).querySelectorAll("tr[data-sym]").forEach((tr) => {
    tr.onclick = () => tr.dataset.sym && openStock(tr.dataset.sym, "sector");
  });
}

// ---- how much of it you already own ----
async function loadBasketOverlap() {
  if (!sbSymbol) return;
  const versus = $("sb-vs").value;
  $("sb-overlap").innerHTML = `<div class="empty">Comparing baskets…</div>`;
  try {
    const d = await api(`/api/v1/etf/basket/overlap?symbol=${encodeURIComponent(sbSymbol)}` +
                        `&versus=${encodeURIComponent(versus)}&limit=15`);
    const rows = d.results || [];
    const x = d.extra || {};
    const mine = x[`share_of_${sbSymbol.toLowerCase()}`];
    const theirs = x[`share_of_${versus.toLowerCase()}`];
    $("sb-overlap-stats").innerHTML = [
      ["Shared holdings", `${x.shared_holdings} of ${x.holdings}`],
      [`Of ${sbSymbol}`, pctWeight(mine, 1)],
      [`Of ${versus}`, pctWeight(theirs, 1)],
      [`Only in ${sbSymbol}`, pctWeight(x[`only_in_${sbSymbol.toLowerCase()}`], 1)],
    ].map(([k, v]) =>
      `<div class="metric"><div class="k">${escapeHtml(k)}</div><div class="v">${v}</div></div>`).join("");
    $("sb-overlap-read").textContent =
      `${pctWeight(mine, 0)} of ${sbSymbol} is weight you already hold through ${versus}, and that ` +
      `same weight is ${pctWeight(theirs, 0)} of ${versus}. Overlap counts the smaller of the two ` +
      `weights for each shared name, so it is the part one fund genuinely duplicates — not just the ` +
      `list of tickers they have in common.`;
    if (!rows.length) { $("sb-overlap").innerHTML = `<div class="empty">These two funds share nothing.</div>`; return; }
    $("sb-overlap").innerHTML = `<table class="clickrows"><tr><th>Holding</th>
        <th>In ${escapeHtml(sbSymbol)}</th><th>In ${escapeHtml(versus)}</th><th>Shared</th></tr>` +
      rows.map((r) => `<tr data-sym="${escapeHtml(String(r.symbol))}">
        <td>${escapeHtml(String(r.name || r.symbol))} <span class="badge">${escapeHtml(String(r.symbol))}</span></td>
        <td class="mono">${pctWeight(r[`${sbSymbol.toLowerCase()}_weight`])}</td>
        <td class="mono">${pctWeight(r[`${versus.toLowerCase()}_weight`])}</td>
        <td class="mono">${pctWeight(r.shared_weight)}</td></tr>`).join("") + "</table>";
    $("sb-overlap").querySelectorAll("tr[data-sym]").forEach((tr) => {
      tr.onclick = () => tr.dataset.sym && openStock(tr.dataset.sym, "sector");
    });
  } catch (e) {
    $("sb-overlap").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`;
    $("sb-overlap-read").textContent = "";
  }
}

async function loadAssets(force) {
  if (assetsLoaded && !force) return;
  const firstTime = !assetsLoaded;
  assetsLoaded = true;
  if (firstTime || force) loadAssetChart();
  for (const [group, el, first] of [["asset_class", "as-table", "Asset class"],
                                    ["style", "as-style", "Style"],
                                    ["country", "as-country", "Country"]]) {
    $(el).innerHTML = `<div class="empty">Loading…</div>`;
    api(`/api/v1/equity/compare/groups?group=${group}`)
      .then((d) => renderGroupTable(el, d.results, { firstCol: first }))
      .catch((e) => { $(el).innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`; });
  }
}
async function loadAssetChart() {
  setStatus("LOADING ASSET-CLASS CHART…");
  try {
    const info = await loadRebasedChart("as-chart", ASSET_ETFS, $("as-start").value);
    setStatus(`ASSET CHART · ${info.bars} BARS`);
  } catch (e) { setStatus("ERR: " + e.message); }
}
$("as-refresh").onclick = () => { loadAssets(true); };
$("as-load").onclick = loadAssetChart;

// ---------- screener tab ----------
// The heavy lifting (universe membership, a year of prices for every member,
// vol/beta/alpha/trend metrics) happens server-side and is cached per universe
// for an hour; every re-run here is just a filter+sort over that table.
const SCR_TF_LABEL = { one_day: "1D", one_week: "1W", one_month: "1M", three_month: "3M",
  six_month: "6M", ytd: "YTD", one_year: "1Y" };
const SCR_FIELDS = ["scr-move", "scr-mcapmin", "scr-mcapmax", "scr-volmin", "scr-volmax",
  "scr-betamin", "scr-betamax", "scr-alphamin", "scr-alphamax", "scr-rsimin", "scr-rsimax"];
// Each preset: clear everything, then set these controls and run.
const SCR_PRESETS = {
  momentum:  { tf: "three_month", dir: "up", "scr-move": "10", ma200: "true", sort: ["three_month", false] },
  defensive: { "scr-volmax": "25", "scr-betamax": "0.7", sort: ["alpha", false] },
  oversold:  { "scr-rsimax": "35", "scr-mcapmin": "25", sort: ["rsi14", true] },
  highbeta:  { tf: "one_week", "scr-betamin": "1.5", sort: ["one_week", false] },
};
let scrDir = "any", scrSort = "market_cap", scrAsc = false, scrLoaded = false, scrBusy = false;
let scrRows = [], scrShownTf = "one_month", scrLastParams = null;

function loadScreener() {
  if (scrLoaded) return;
  scrLoaded = true;
  runScreen();
}

function scrSetDir(dir) {
  scrDir = dir;
  document.querySelectorAll("#scr-dir .chip").forEach((x) =>
    x.classList.toggle("active", x.dataset.dir === dir));
}

function scrClearFilters() {
  SCR_FIELDS.forEach((id) => { $(id).value = ""; });
  $("scr-limit").value = "50";
  $("scr-sector").value = "";
  $("scr-ma50").value = "";
  $("scr-ma200").value = "";
  scrSetDir("any");
  scrSort = "market_cap"; scrAsc = false;
  document.querySelectorAll("#scr-presets .chip").forEach((x) => x.classList.remove("active"));
}

document.querySelectorAll("#scr-dir .chip").forEach((c) => {
  c.onclick = () => { scrSetDir(c.dataset.dir); runScreen(); };
});
document.querySelectorAll("#scr-presets .chip").forEach((c) => {
  c.onclick = () => {
    const p = SCR_PRESETS[c.dataset.preset];
    scrClearFilters();
    c.classList.add("active");
    if (p.tf) $("scr-tf").value = p.tf;
    if (p.dir) scrSetDir(p.dir);
    if (p.ma200) $("scr-ma200").value = p.ma200;
    Object.keys(p).filter((k) => k.startsWith("scr-")).forEach((k) => { $(k).value = p[k]; });
    [scrSort, scrAsc] = p.sort;
    runScreen();
  };
});
$("scr-run").onclick = () => {
  document.querySelectorAll("#scr-presets .chip").forEach((x) => x.classList.remove("active"));
  runScreen();
};
$("scr-index").onchange = () => { $("scr-sector").value = ""; runScreen(); };
$("scr-tf").onchange = () => runScreen();
$("scr-sector").onchange = () => runScreen();
$("scr-ma50").onchange = () => runScreen();
$("scr-ma200").onchange = () => runScreen();
$("scr-reset").onclick = () => { scrClearFilters(); runScreen(); };

function scrNum(id) {
  const v = parseFloat($(id).value);
  return isNaN(v) ? null : v;
}

async function runScreen() {
  if (scrBusy) return;
  scrBusy = true;
  const tf = $("scr-tf").value;
  const universe = $("scr-index").value;
  // Typed values, so a ★-saved screen re-executes with real numbers/booleans.
  const typed = {
    timeframe: tf, direction: scrDir,
    sort: scrSort === "move" ? tf : scrSort, ascending: scrAsc,
    limit: scrNum("scr-limit") ?? 50,
  };
  $("scr-table").innerHTML = `<div class="empty">Screening — a fresh universe can take up to a minute…</div>`;
  $("scr-warn").innerHTML = "";
  setStatus("SCREENING…");
  try {
    if (universe === "watchlist") {
      const w = await ensureMarketWatchlist();
      const syms = (w.items || []).map((i) => i.symbol).filter(Boolean);
      if (!syms.length) throw new Error("Your watchlist is empty — add tickers on the Markets tab first.");
      typed.symbols = syms.join(",");
    } else {
      typed.index = universe;
    }
    if ($("scr-sector").value) typed.sector = $("scr-sector").value;
    if ($("scr-ma50").value) typed.above_ma50 = $("scr-ma50").value === "true";
    if ($("scr-ma200").value) typed.above_ma200 = $("scr-ma200").value === "true";
    for (const [key, id] of [["min_move", "scr-move"], ["mcap_min", "scr-mcapmin"],
      ["mcap_max", "scr-mcapmax"], ["vol_min", "scr-volmin"], ["vol_max", "scr-volmax"],
      ["beta_min", "scr-betamin"], ["beta_max", "scr-betamax"],
      ["alpha_min", "scr-alphamin"], ["alpha_max", "scr-alphamax"],
      ["rsi_min", "scr-rsimin"], ["rsi_max", "scr-rsimax"]]) {
      const v = scrNum(id);
      if (v != null) typed[key] = v;
    }
    const params = new URLSearchParams();
    Object.entries(typed).forEach(([k, v]) => params.set(k, v));
    const d = await api(`/api/v1/screener/run?${params}`);
    const x = d.extra || {};
    const label = universe === "watchlist" ? "My Watchlist" : (x.label || "");
    scrRows = d.results; scrShownTf = tf;
    scrLastParams = typed;
    $("scr-badge").style.display = "";
    $("scr-badge").textContent = `${label} vs ${x.benchmark || ""} · as of ${x.as_of || ""}`;
    $("scr-note").textContent = `beta & alpha vs ${x.benchmark || "the index"}, 1 year of daily returns`;
    $("scr-count").textContent =
      `${x.matched ?? d.results.length} of ${x.universe_size ?? "?"} members match · showing ${d.results.length} · click a header to sort, a row to open`;
    $("scr-csv").style.display = d.results.length ? "" : "none";
    scrFillSectors(x.sectors || []);
    if (d.warnings && d.warnings.length) {
      $("scr-warn").innerHTML = `<div class="warnbox">${escapeHtml(d.warnings.join(" · "))}</div>`;
    }
    renderScreenTable(d.results, tf);
    setStatus(`SCREEN · ${x.matched ?? d.results.length} MATCHES`);
  } catch (e) {
    $("scr-table").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`;
    setStatus("ERR: " + e.message);
  } finally { scrBusy = false; }
}

// Sector names differ per universe (GICS vs Nasdaq taxonomies), so the
// dropdown refills from each response; the current pick survives when the new
// universe still has it.
function scrFillSectors(sectors) {
  const sel = $("scr-sector");
  const current = sel.value;
  sel.innerHTML = `<option value="">All sectors</option>` +
    sectors.map((s) => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join("");
  if (sectors.includes(current)) sel.value = current;
}

function renderScreenTable(rows, tf) {
  if (!rows.length) {
    $("scr-table").innerHTML = `<div class="empty">Nothing matches those filters — try loosening them.</div>`;
    return;
  }
  const pct = (v, sign) => v == null ? "<td>-</td>" : `<td class="${cls(v)}">${fmtPct(v, sign)}</td>`;
  const cols = [
    ["symbol", "Symbol"], ["sector", "Sector"], ["last_price", "Price"],
    ["market_cap", "Mkt cap"], ["move", `${SCR_TF_LABEL[tf]} move`], ["one_day", "1D"],
    ["ytd", "YTD"], ["volatility", "Vol"], ["beta", "Beta"], ["alpha", "Alpha"],
    ["ma200_dist", "vs 200d"], ["high52_dist", "Off high"], ["rsi14", "RSI"],
  ];
  const maxAbs = Math.max(...rows.map((r) => Math.abs(r[tf] ?? 0)), 0.0001);
  const arrow = (key) => {
    const active = scrSort === key || (key === "move" && scrSort === tf);
    return active ? (scrAsc ? " ▲" : " ▼") : "";
  };
  $("scr-table").innerHTML = `<table class="clickrows"><tr><th>#</th>` +
    cols.map(([k, l]) => `<th class="scr-th" data-sort="${k}" style="cursor:pointer">${l}${arrow(k)}</th>`).join("") +
    "</tr>" +
    rows.map((r, i) => {
      const move = r[tf];
      const w = Math.round(Math.abs(move ?? 0) / maxAbs * 60);
      return `<tr data-i="${i}"><td class="dim">${i + 1}</td>
        <td>${escapeHtml(r.symbol)} <span class="badge">${escapeHtml(String(r.name || "").slice(0, 24))}</span></td>
        <td>${escapeHtml(String(r.sector || "-"))}</td>
        <td>${r.last_price != null ? "$" + r.last_price.toFixed(2) : "-"}</td>
        <td>${fmtBig(r.market_cap)}</td>
        <td>${move == null ? "-" : `<span class="${cls(move)}">${fmtPct(move, true)}</span>
          <div class="perfbar"><div class="bar ${move >= 0 ? "pos" : "neg"}" style="width:${w}px"></div></div>`}</td>` +
        pct(r.one_day, true) + pct(r.ytd, true) +
        `<td>${r.volatility != null ? (r.volatility * 100).toFixed(0) + "%" : "-"}</td>
        <td>${r.beta != null ? r.beta.toFixed(2) : "-"}</td>` +
        pct(r.alpha, true) + pct(r.ma200_dist, true) + pct(r.high52_dist, true) +
        `<td>${r.rsi14 != null ? r.rsi14.toFixed(0) : "-"}</td></tr>`;
    }).join("") + "</table>";
  $("scr-table").querySelectorAll("tr[data-i]").forEach((tr) => {
    tr.onclick = () => openStock(rows[+tr.dataset.i].symbol, "screener");
  });
  $("scr-table").querySelectorAll(".scr-th").forEach((th) => {
    th.onclick = () => {
      const key = th.dataset.sort;
      const same = scrSort === key || (key === "move" && scrSort === tf);
      scrAsc = same ? !scrAsc : key === "symbol" || key === "sector";
      scrSort = key;
      runScreen();
    };
  });
}

$("scr-save").onclick = async () => {
  if (!scrLastParams) { setStatus("RUN A SCREEN FIRST"); return; }
  const label = $("scr-index").selectedOptions[0]?.textContent || "screen";
  const nm = prompt("Name this screen:", `Screener: ${label}`);
  if (!nm) return;
  try {
    await api("/api/user/saved", {
      method: "POST",
      body: { name: nm, command_path: "/screener/run", parameters: scrLastParams, is_favorite: true },
    });
    setStatus(`SAVED "${nm}" — find it on the Saved tab`);
  } catch (e) { setStatus("ERR: " + e.message); }
};

$("scr-csv").onclick = () => {
  if (!scrRows.length) return;
  const cols = [...new Set(scrRows.flatMap((r) => Object.keys(r)))];
  const esc = (v) => {
    const s = v === null || v === undefined ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const csv = [cols.join(","), ...scrRows.map((r) => cols.map((c) => esc(r[c])).join(","))].join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = `screener_${$("scr-index").value}_${scrShownTf}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
};

// ---------- news tab ----------
// The catalogue is ~290 feeds in ~20 desks. Desk chips select whole desks;
// once a desk is picked its feeds appear underneath for narrowing further.
// Selection resolves to one `sources` string: feeds if any are picked, else
// desks if any are picked, else nothing (the server's default newswire).
let newsInitDone = false;
let newsCatalogue = [];            // rows of /news/sources: source, category, default
const newsDesks = new Set();       // selected desk names
const newsSel = new Set();         // selected feed names

function newsSourcesParam() {
  const picked = newsSel.size ? [...newsSel] : [...newsDesks];
  return picked.length ? `&sources=${encodeURIComponent(picked.join(","))}` : "";
}

function renderNewsFeedChips() {
  const rows = newsCatalogue.filter((r) => newsDesks.has(r.category));
  // A feed whose desk was just deselected drops out of the selection too.
  const visible = new Set(rows.map((r) => r.source));
  [...newsSel].forEach((s) => { if (!visible.has(s)) newsSel.delete(s); });
  $("nw-sources").innerHTML = rows.length
    ? `<span class="nw-hint">narrow to:</span>` + rows.map((r) =>
      `<button class="chip nw-chip sm${newsSel.has(r.source) ? " sel" : ""}" data-src="${escapeHtml(r.source)}" title="${escapeHtml(r.feed_url || "")}">${escapeHtml(r.source)}</button>`).join("")
    : "";
  $("nw-sources").querySelectorAll(".nw-chip").forEach((el) => {
    el.onclick = () => {
      const s = el.dataset.src;
      newsSel.has(s) ? newsSel.delete(s) : newsSel.add(s);
      el.classList.toggle("sel");
      $("nw-query").value = ""; $("nw-symbol").value = "";
      loadNews();
    };
  });
}

async function loadNewsInit() {
  if (newsInitDone) return;
  newsInitDone = true;
  try {
    const d = await api("/api/v1/news/sources");
    newsCatalogue = d.results;
    const desks = [];
    newsCatalogue.forEach((r) => {
      let desk = desks.find((x) => x.name === r.category);
      if (!desk) { desk = { name: r.category, n: 0, dflt: !!r.default }; desks.push(desk); }
      desk.n += 1;
    });
    $("nw-desks").innerHTML = desks.map((k) =>
      `<button class="chip nw-chip nw-desk${k.dflt ? " dflt" : ""}" data-desk="${escapeHtml(k.name)}"
         title="${k.dflt ? "On the default tape · " : ""}${k.n} feeds">${escapeHtml(k.name.replace(/_/g, " "))}<small>${k.n}</small></button>`).join("");
    $("nw-desks").querySelectorAll(".nw-desk").forEach((el) => {
      el.onclick = () => {
        const k = el.dataset.desk;
        newsDesks.has(k) ? newsDesks.delete(k) : newsDesks.add(k);
        el.classList.toggle("sel");
        renderNewsFeedChips();
        $("nw-query").value = ""; $("nw-symbol").value = "";
        loadNews();
      };
    });
  } catch { /* desk chips are optional */ }
  loadNews();
}

async function loadNews() {
  $("nw-feed").innerHTML = `<div class="empty">Loading feed…</div>`;
  $("nw-warn").innerHTML = "";
  const sym = $("nw-symbol").value.trim().toUpperCase();
  const q = $("nw-query").value.trim();
  const url = sym ? `/api/v1/news/company?symbol=${encodeURIComponent(sym)}&limit=40`
    : q ? `/api/v1/news/search?query=${encodeURIComponent(q)}&limit=40`
    : `/api/v1/news/world?limit=60` + newsSourcesParam();
  try {
    const d = await api(url);
    if (d.warnings && d.warnings.length) {
      $("nw-warn").innerHTML =
        `<div class="warnbox">! ${d.warnings.slice(0, 3).map((w) => escapeHtml(String(w))).join("<br>! ")}</div>`;
    }
    const rows = d.results;
    $("nw-feed").innerHTML = rows.length ? rows.map((r) => `
      <div class="feed-item">
        <div class="feed-meta">
          ${r.source ? `<span class="src-badge">${escapeHtml(String(r.source))}</span>` : ""}
          ${r.category ? `<span class="src-badge muted">${escapeHtml(String(r.category).replace(/_/g, " "))}</span>` : ""}
          ${r.symbol ? `<span class="src-badge">${escapeHtml(String(r.symbol))}</span>` : ""}
          <span>${timeAgo(r.date)}</span>
        </div>
        ${r.url
          ? `<a class="feed-title" href="${escapeHtml(String(r.url))}" target="_blank" rel="noopener">${escapeHtml(String(r.title || ""))}</a>`
          : `<span class="feed-title">${escapeHtml(String(r.title || ""))}</span>`}
        ${r.summary ? `<div class="feed-sum">${escapeHtml(String(r.summary).slice(0, 240))}</div>` : ""}
      </div>`).join("") : `<div class="empty">No stories returned.</div>`;
    setStatus(`${rows.length} STORIES`);
  } catch (e) { $("nw-feed").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`; }
}
$("nw-refresh").onclick = loadNews;
$("nw-go").onclick = loadNews;
$("nw-clear").onclick = () => {
  $("nw-query").value = ""; $("nw-symbol").value = "";
  newsSel.clear(); newsDesks.clear();
  document.querySelectorAll(".nw-chip.sel").forEach((el) => el.classList.remove("sel"));
  renderNewsFeedChips();
  loadNews();
};
$("nw-query").onkeydown = (ev) => { if (ev.key === "Enter") loadNews(); };
$("nw-symbol").onkeydown = (ev) => { if (ev.key === "Enter" && !ev.defaultPrevented) loadNews(); };

// ---------- calendar ----------
// One grid over five feeds. The server normalises them onto a single row shape
// (/calendar/events), so everything here is layout: which types are ticked,
// which month is showing, and month-grid versus agenda.
//
// The user's own notes are the exception — they live behind auth at
// /api/user/calendar, so every load is two requests merged client-side.
const CAL_DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const CAL_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
  "August", "September", "October", "November", "December"];
// Types ticked on a first visit. Macro is off because it has its own tab and
// would otherwise dominate the grid — a fortnight of it is several hundred
// rows. Dividends are off because they are the one type that costs a request
// per day whichever source is picked, and a first paint should be quick;
// ticking them says the wait is wanted.
const CAL_DEFAULT_TYPES = ["earnings", "split", "ipo", "fomc", "custom"];
const CAL_PILL_LIMIT = 3;

let calTypes = [];
let calSel = new Set(CAL_DEFAULT_TYPES);
let calAnchor = null;     // any date inside the displayed month
let calMode = "month";
let calEvents = [];
let calCounts = {};
let calFocusDate = null;  // agenda scoped to one day, set by clicking a cell
let calInitDone = false;
let calReqId = 0;         // guards against a slow response landing after a fast one

const calColor = (type) => `var(--ev-${type}, var(--muted))`;
const calISO = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const calToday = () => calISO(new Date());

function calMonthStart(d) { return new Date(d.getFullYear(), d.getMonth(), 1); }

/** The dates the month grid actually shows: the whole weeks the month spans.
 *  Fetching only the 1st-to-31st would leave the leading and trailing cells
 *  visibly empty rather than merely out of month. */
function calWindow() {
  const first = calMonthStart(calAnchor);
  const start = new Date(first);
  start.setDate(1 - first.getDay());
  const end = new Date(first.getFullYear(), first.getMonth() + 1, 0);
  end.setDate(end.getDate() + (6 - end.getDay()));
  return { start, end };
}

async function loadCalendarInit() {
  if (calInitDone) return;
  calInitDone = true;
  calAnchor = new Date();
  $("cal-n-date").value = calToday();
  try {
    const d = await api("/api/v1/calendar/event_types");
    calTypes = d.results || [];
  } catch {
    // The rail is the filter UI; without it fall back to the default ticks so
    // the grid still loads rather than showing an empty page.
    calTypes = CAL_DEFAULT_TYPES.map((k) => ({ key: k, label: k, group: "Events", available: true }));
  }
  renderCalTypes();
  loadCalendar();
}

function renderCalTypes() {
  const groups = [];
  calTypes.forEach((t) => {
    let g = groups.find((x) => x.name === (t.group || "Events"));
    if (!g) groups.push((g = { name: t.group || "Events", items: [] }));
    g.items.push(t);
  });
  $("cal-types").innerHTML = groups.map((g) => `
    <div class="cal-group">${escapeHtml(g.name)}</div>
    ${g.items.map((t) => {
      const on = calSel.has(t.key);
      const n = calCounts[t.key];
      // An unavailable type is rendered, disabled, with the reason on hover —
      // dropping it would read as "nothing scheduled" rather than "no source".
      return `<label class="cal-type${t.available ? "" : " off"}"${
        t.available ? "" : ` title="${escapeHtml(t.why || "No free source for this event type.")}"`}>
        <input type="checkbox" data-cal-type="${escapeHtml(t.key)}"
          ${on ? "checked" : ""} ${t.available ? "" : "disabled"} />
        <span class="cal-badge" style="background:${t.available ? calColor(t.key) : "var(--line)"}"
          >${escapeHtml(t.badge || "•")}</span>
        <span class="lbl">${escapeHtml(t.label)}</span>
        ${t.available && n ? `<span class="cal-typecount">${n}</span>` : ""}
      </label>`;
    }).join("")}`).join("");

  $("cal-types").querySelectorAll("[data-cal-type]").forEach((el) => {
    el.onchange = () => {
      el.checked ? calSel.add(el.dataset.calType) : calSel.delete(el.dataset.calType);
      loadCalendar();
    };
  });
  const avail = calTypes.filter((t) => t.available).length;
  const off = calTypes.length - avail;
  if (off) {
    $("cal-types").insertAdjacentHTML("beforeend",
      `<div class="hintline" style="margin-top:12px;line-height:1.5">${off} more types a
       terminal usually carries have no free public source — hover one to see why.</div>`);
  }
}

/** Server events for the window, merged with the user's own notes. */
async function loadCalendar() {
  const req = ++calReqId;
  const { start, end } = calWindow();
  const startISO = calISO(start), endISO = calISO(end);
  const monthName = `${CAL_MONTHS[calAnchor.getMonth()]} ${calAnchor.getFullYear()}`;
  $("cal-title").textContent = monthName.toUpperCase();
  $("cal-warn").innerHTML = "";
  $("cal-count").textContent = "";

  const wanted = [...calSel];
  const platform = wanted.filter((t) => t !== "custom");
  const provider = $("cal-provider").value;
  // Dividends only exist as a per-day feed, so they cost a request per weekday
  // whichever source is picked. Say so up front rather than letting a month
  // look frozen.
  const slow = provider === "nasdaq" || platform.some((t) => t.startsWith("dividend_"));
  $("cal-body").innerHTML = slow
    ? `<div class="empty">Loading ${escapeHtml(monthName)} — this source serves one day per
       request, so a fresh month takes up to a minute. It is cached after that.</div>`
    : `<div class="empty">Loading ${escapeHtml(monthName)}…</div>`;

  const symbols = $("cal-symbols").value.trim();
  // The server ignores the size floor once symbols are named — reflect that
  // here so the control does not look like it is still doing something.
  const importance = symbols ? "1" : $("cal-importance").value;
  $("cal-importance").disabled = !!symbols;
  $("cal-importance").title = symbols
    ? "Ignored while symbols are named — those companies show whatever their size."
    : "";
  const qs = new URLSearchParams({
    start_date: startISO, end_date: endISO, types: platform.join(","),
    min_importance: importance, provider, limit: "3000", max_days: "45",
  });
  if (symbols) qs.set("symbols", symbols);

  const jobs = [
    platform.length
      ? api(`/api/v1/calendar/events?${qs}`).catch((e) => ({ error: e.message, results: [] }))
      : Promise.resolve({ results: [], extra: {} }),
    calSel.has("custom")
      ? api(`/api/user/calendar?start_date=${startISO}&end_date=${endISO}`
          + (symbols ? `&symbol=${encodeURIComponent(symbols.split(/[ ,]+/)[0])}` : ""))
          .catch(() => [])
      : Promise.resolve([]),
  ];
  const [feed, notes] = await Promise.all(jobs);
  if (req !== calReqId) return;  // a newer request already owns the view

  // Notes are never filtered by the size floor. It is a market-cap threshold
  // and a note has no market cap — dropping something the user typed onto their
  // own calendar because a company somewhere is too small would be nonsense.
  const noteRows = (Array.isArray(notes) ? notes : []).map((n) => ({
    date: n.event_date, time: n.time, type: "custom", type_label: "Custom / Notes",
    symbol: n.symbol, name: null, title: n.title, detail: n.detail,
    importance: n.importance || 2, source: "you", id: n.id,
  }));

  calEvents = [...(feed.results || []), ...noteRows]
    .sort((a, b) => a.date.localeCompare(b.date) || b.importance - a.importance
      || a.type.localeCompare(b.type) || (a.symbol || a.title).localeCompare(b.symbol || b.title));

  calCounts = {};
  calEvents.forEach((e) => { calCounts[e.type] = (calCounts[e.type] || 0) + 1; });
  renderCalTypes();

  const warns = [...(feed.warnings || [])];
  if (feed.error) warns.unshift(feed.error);
  $("cal-warn").innerHTML = warns.length
    ? `<div class="warnbox">! ${warns.slice(0, 4).map((w) => escapeHtml(String(w))).join("<br>! ")}</div>` : "";
  $("cal-count").textContent = calEvents.length
    ? `${calEvents.length} event${calEvents.length === 1 ? "" : "s"}` : "";

  renderCalendar();
  setStatus(`${calEvents.length} EVENTS`);
}

function renderCalendar() {
  renderCalLegend();
  if (!calSel.size) {
    $("cal-body").innerHTML = `<div class="empty">No event types ticked — choose at least one on the left.</div>`;
    return;
  }
  if (calMode === "month") renderCalMonth(); else renderCalAgenda();
}

function renderCalLegend() {
  const shown = calTypes.filter((t) => t.available && calSel.has(t.key));
  $("cal-legend").innerHTML = shown.length
    ? shown.map((t) => `<span><i style="background:${calColor(t.key)}"></i>${escapeHtml(t.label)}</span>`).join("")
    : "";
}

function calByDate() {
  const map = {};
  calEvents.forEach((e) => { (map[e.date] = map[e.date] || []).push(e); });
  return map;
}

function renderCalMonth() {
  const { start, end } = calWindow();
  const byDate = calByDate();
  const month = calAnchor.getMonth();
  const today = calToday();

  let cells = "";
  for (let d = new Date(start); d <= end; d.setDate(d.getDate() + 1)) {
    const iso = calISO(d);
    const rows = byDate[iso] || [];
    const shown = rows.slice(0, CAL_PILL_LIMIT);
    const rest = rows.length - shown.length;
    cells += `<div class="cal-day${d.getMonth() === month ? "" : " out"}${iso === today ? " today" : ""}">
      <span class="cal-daynum">${d.getDate()}</span>
      ${shown.map((e) => `
        <button class="cal-pill" style="border-left-color:${calColor(e.type)}"
          data-cal-day="${iso}" title="${escapeHtml(calEventTitle(e))}">
          ${e.symbol ? `<span class="p-sym">${escapeHtml(e.symbol)}</span> ` : ""}${escapeHtml(calPillText(e))}
        </button>`).join("")}
      ${rest > 0 ? `<button class="cal-more" data-cal-day="${iso}">+${rest} more</button>` : ""}
    </div>`;
  }

  $("cal-body").innerHTML =
    `<div class="cal-grid">${CAL_DOW.map((d) => `<div class="cal-dow">${d}</div>`).join("")}${cells}</div>`;
  // Any cell click drops into the agenda for that one day — the grid shows
  // three events per cell and a busy earnings day has thirty.
  $("cal-body").querySelectorAll("[data-cal-day]").forEach((el) => {
    el.onclick = () => {
      calFocusDate = el.dataset.calDay;
      calMode = "agenda";
      document.querySelectorAll(".cal-mode").forEach((x) =>
        x.classList.toggle("active", x.dataset.mode === "agenda"));
      renderCalendar();
    };
  });
}

/** Short text for a month cell — the symbol is already rendered beside it. */
function calPillText(e) {
  if (e.type === "earnings") return e.time === "pre-market" ? "earnings (bmo)"
    : e.time === "after-hours" ? "earnings (amc)" : "earnings";
  if (e.type === "dividend_ex") return "ex-div";
  if (e.type === "dividend_pay") return "div paid";
  if (e.type === "split") return "split";
  if (e.type === "ipo") return "IPO";
  if (e.type === "custom") return e.title;
  return e.title;
}

function calEventTitle(e) {
  return [e.title, e.detail].filter(Boolean).join(" — ");
}

function renderCalAgenda() {
  let rows = calEvents;
  if (calFocusDate) rows = rows.filter((e) => e.date === calFocusDate);
  if (!rows.length) {
    $("cal-body").innerHTML = `<div class="empty">Nothing scheduled${
      calFocusDate ? " that day" : " in this window"} for the ticked types.</div>`
      + (calFocusDate ? `<button class="linkbtn" id="cal-unfocus">Show the whole month</button>` : "");
    if ($("cal-unfocus")) $("cal-unfocus").onclick = calClearFocus;
    return;
  }

  const byDate = {};
  rows.forEach((e) => { (byDate[e.date] = byDate[e.date] || []).push(e); });
  const today = calToday();

  const body = Object.keys(byDate).sort().map((iso) => {
    const d = new Date(iso + "T00:00:00");
    return `<div class="cal-agenda-day">
      <div class="cal-agenda-date${iso === today ? " is-today" : ""}">
        ${CAL_DOW[d.getDay()]}<span class="d-num">${d.getDate()}</span>
        ${CAL_MONTHS[d.getMonth()].slice(0, 3)}
      </div>
      <div class="cal-agenda-rows">
        ${byDate[iso].map((e) => `
          <div class="cal-ev imp-${e.importance || 1}">
            <span class="ev-dot" style="background:${calColor(e.type)}"></span>
            ${e.symbol
              ? `<button class="ev-sym linkbtn" style="margin:0" data-cal-sym="${escapeHtml(e.symbol)}"
                   >${escapeHtml(e.symbol)}</button>`
              : `<span class="ev-sym plain">${escapeHtml(e.name || "—")}</span>`}
            <span class="ev-type">${escapeHtml(e.type_label || e.type)}</span>
            <span class="ev-what">${escapeHtml(e.title)}
              ${e.detail ? `<span class="ev-detail"> · ${escapeHtml(e.detail)}</span>` : ""}
              ${e.time ? `<span class="ev-time"> · ${escapeHtml(e.time)}</span>` : ""}
              ${e.type === "custom" ? `<button class="ev-del" data-cal-del="${e.id}"
                 title="Delete this note" aria-label="Delete note">×</button>` : ""}
            </span>
          </div>`).join("")}
      </div>
    </div>`;
  }).join("");

  $("cal-body").innerHTML =
    (calFocusDate ? `<button class="linkbtn" id="cal-unfocus">‹ Whole month</button>` : "")
    + `<div class="cal-agenda">${body}</div>`;

  if ($("cal-unfocus")) $("cal-unfocus").onclick = calClearFocus;
  $("cal-body").querySelectorAll("[data-cal-sym]").forEach((el) => {
    el.onclick = () => openStock(el.dataset.calSym, "calendar");
  });
  $("cal-body").querySelectorAll("[data-cal-del]").forEach((el) => {
    el.onclick = async () => {
      try {
        await api(`/api/user/calendar/${el.dataset.calDel}`, { method: "DELETE" });
        loadCalendar();
      } catch (e) { $("cal-warn").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`; }
    };
  });
}

function calClearFocus() { calFocusDate = null; renderCalendar(); }

function calShift(months) {
  calAnchor = new Date(calAnchor.getFullYear(), calAnchor.getMonth() + months, 1);
  calFocusDate = null;
  loadCalendar();
}

$("cal-prev").onclick = () => calShift(-1);
$("cal-next").onclick = () => calShift(1);
$("cal-today").onclick = () => { calAnchor = new Date(); calFocusDate = null; loadCalendar(); };
$("cal-provider").onchange = loadCalendar;
$("cal-importance").onchange = loadCalendar;
$("cal-symbols").onchange = loadCalendar;
$("cal-symbols").onkeydown = (ev) => { if (ev.key === "Enter") loadCalendar(); };
document.querySelectorAll(".cal-mode").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll(".cal-mode").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    calMode = b.dataset.mode;
    if (calMode === "month") calFocusDate = null;
    renderCalendar();
  };
});
$("cal-types-all").onclick = () => {
  calTypes.filter((t) => t.available).forEach((t) => calSel.add(t.key));
  renderCalTypes(); loadCalendar();
};
$("cal-types-none").onclick = () => {
  calSel.clear(); renderCalTypes(); renderCalendar();
};

// --- the user's own notes ---
async function calAddNote() {
  const date = $("cal-n-date").value;
  const title = $("cal-n-title").value.trim();
  const msg = $("cal-n-msg");
  msg.className = "msg";
  if (!date || !title) { msg.textContent = "A date and a title, please."; return; }
  try {
    await api("/api/user/calendar", {
      method: "POST",
      body: { event_date: date, title, symbol: $("cal-n-sym").value.trim().toUpperCase() || null },
    });
    $("cal-n-title").value = ""; $("cal-n-sym").value = "";
    msg.className = "msg ok"; msg.textContent = "Added.";
    calSel.add("custom");
    renderCalTypes();
    // Jump to the month the note landed in, so it is visible straight away.
    const landed = new Date(date + "T00:00:00");
    if (landed.getMonth() !== calAnchor.getMonth() || landed.getFullYear() !== calAnchor.getFullYear()) {
      calAnchor = landed;
    }
    loadCalendar();
  } catch (e) { msg.className = "msg"; msg.textContent = e.message; }
}
$("cal-n-add").onclick = calAddNote;
$("cal-n-title").onkeydown = (ev) => { if (ev.key === "Enter") calAddNote(); };
$("cal-n-sym").onkeydown = (ev) => { if (ev.key === "Enter") calAddNote(); };

// --- calendar sub-tabs ---
document.querySelectorAll("#cal-tabs .tab").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("#cal-tabs .tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll("#caltab-events, #caltab-economic").forEach((x) =>
      x.classList.remove("active"));
    b.classList.add("active");
    $("caltab-" + b.dataset.caltab).classList.add("active");
    if (b.dataset.caltab === "economic") loadEconomicInit();
  };
});

// ---------- economic calendar ----------
// Split out because macro filters on things company events do not have: the
// releases are global and unranked at source, so region and importance carry
// the whole view.
let econInitDone = false;
let econRegions = new Set(["US"]);
let econAllRegions = [];

function econInit() {
  const start = new Date();
  const end = new Date();
  end.setDate(end.getDate() + 14);
  $("ec-start").value = calISO(start);
  $("ec-end").value = calISO(end);
}

function loadEconomicInit() {
  if (econInitDone) return;
  econInitDone = true;
  econInit();
  loadEconomic();
}

function econShift(days) {
  const start = new Date($("ec-start").value + "T00:00:00");
  const end = new Date($("ec-end").value + "T00:00:00");
  start.setDate(start.getDate() + days);
  end.setDate(end.getDate() + days);
  $("ec-start").value = calISO(start);
  $("ec-end").value = calISO(end);
  loadEconomic();
}

async function loadEconomic() {
  $("ec-body").innerHTML = `<div class="empty">Loading releases…</div>`;
  $("ec-warn").innerHTML = "";
  $("ec-count").textContent = "";
  const qs = new URLSearchParams({
    start_date: $("ec-start").value, end_date: $("ec-end").value,
    min_importance: $("ec-importance").value, limit: "600",
  });
  // Region filtering happens client-side: the request is one call either way,
  // and the chip row has to list every region the window actually contains.
  try {
    const d = await api(`/api/v1/calendar/economic?${qs}`);
    const rows = d.results || [];
    econAllRegions = (d.extra && d.extra.regions) || [...new Set(rows.map((r) => r.name))].sort();
    renderEconRegions();
    if (d.warnings && d.warnings.length) {
      $("ec-warn").innerHTML =
        `<div class="warnbox">! ${d.warnings.map((w) => escapeHtml(String(w))).join("<br>! ")}</div>`;
    }
    renderEconomic(rows);
  } catch (e) {
    $("ec-body").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`;
  }
}

function renderEconRegions() {
  $("ec-regions").innerHTML = econAllRegions.map((r) =>
    `<button class="chip sm ec-region${econRegions.has(r) ? " active" : ""}"
      data-region="${escapeHtml(r)}">${escapeHtml(r)}</button>`).join("")
    + `<button class="chip sm" id="ec-all-regions">All regions</button>`;
  $("ec-regions").querySelectorAll(".ec-region").forEach((el) => {
    el.onclick = () => {
      const r = el.dataset.region;
      econRegions.has(r) ? econRegions.delete(r) : econRegions.add(r);
      el.classList.toggle("active");
      renderEconomic(econLastRows);
    };
  });
  $("ec-all-regions").onclick = () => {
    econRegions.clear();
    renderEconRegions();
    renderEconomic(econLastRows);
  };
}

let econLastRows = [];
function renderEconomic(rows) {
  econLastRows = rows || [];
  const shown = econRegions.size
    ? econLastRows.filter((r) => econRegions.has(r.name))
    : econLastRows;
  $("ec-count").textContent = `${shown.length} release${shown.length === 1 ? "" : "s"}`;
  if (!shown.length) {
    $("ec-body").innerHTML = `<div class="empty">No releases match that region and importance.</div>`;
    return;
  }
  const byDate = {};
  shown.forEach((e) => { (byDate[e.date] = byDate[e.date] || []).push(e); });
  const today = calToday();

  $("ec-body").innerHTML = `<div class="cal-agenda">` + Object.keys(byDate).sort().map((iso) => {
    const d = new Date(iso + "T00:00:00");
    return `<div class="cal-agenda-day">
      <div class="cal-agenda-date${iso === today ? " is-today" : ""}">
        ${CAL_DOW[d.getDay()]}<span class="d-num">${d.getDate()}</span>
        ${CAL_MONTHS[d.getMonth()].slice(0, 3)}
      </div>
      <div class="cal-agenda-rows">
        ${byDate[iso].map((e) => `
          <div class="cal-ev imp-${e.importance || 1}">
            <span class="ev-dot" style="background:${calColor("economic")}"></span>
            <span class="ev-sym plain">${escapeHtml(e.name || "—")}</span>
            <span class="ev-type">${e.importance === 3 ? "Major" : e.importance === 2 ? "Notable" : ""}</span>
            <span class="ev-what">${escapeHtml(e.title.replace(/^[A-Z]{2} · /, ""))}
              ${e.detail ? `<span class="ev-detail"> · ${escapeHtml(e.detail)}</span>` : ""}
            </span>
          </div>`).join("")}
      </div>
    </div>`;
  }).join("") + `</div>`;
}

$("ec-go").onclick = loadEconomic;
$("ec-prev").onclick = () => econShift(-14);
$("ec-next").onclick = () => econShift(14);
$("ec-today").onclick = () => { econInit(); loadEconomic(); };
$("ec-importance").onchange = loadEconomic;

// ---------- sentiment tab ----------
// All scoring happens server-side (/sentiment/*) so the CLI and raw API read
// the same mood. The frontend only renders: an aggregate gauge for the wire,
// per-source bars, per-ticker cards, and the story-by-story scored feed.
//
// The tab is split in two, because the two questions are different: "what is
// the mood of the market" (one number off the whole wire) and "what is the
// mood of the things I own" (per ticker and per sector). Each half owns its
// own scored-headline feed — "se-" for the market wire, "sa-" for assets —
// so opening a sector's stories no longer wipes the market tape below it.
let sentimentInitDone = false;
let sectorLoadStarted = false;

function loadSentimentInit() {
  if (sentimentInitDone) return;
  sentimentInitDone = true;
  loadMarketSentiment();
}

// The sector board is 11 separate Google News queries (~10s cold), so it only
// runs once the user actually opens the half of the tab that shows it.
function loadAssetsSentimentInit() {
  if (sectorLoadStarted) return;
  sectorLoadStarted = true;
  loadSectorSentiment();
  seedSymbolsFromWatchlist();
}

document.querySelectorAll("#se-tabs .tab").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("#se-tabs .tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".setab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $("setab-" + b.dataset.setab).classList.add("active");
    if (b.dataset.setab === "assets") loadAssetsSentimentInit();
  };
});

// "Your stocks" should mean the user's actual watchlist, not a hardcoded four.
// Best-effort: a brand-new account with no list keeps the seed tickers.
async function seedSymbolsFromWatchlist() {
  try {
    const wl = await ensureMarketWatchlist();
    const syms = ((wl && wl.items) || []).map((i) => i.symbol).filter(Boolean);
    if (syms.length) $("se-symbols").value = syms.slice(0, 8).join(",");
  } catch (e) {
    /* watchlist unavailable — leave the seed tickers in place */
  }
}

const SENTI_LBL = {
  bullish: ["BULLISH", "var(--green)"],
  bearish: ["BEARISH", "var(--red)"],
  neutral: ["NEUTRAL", "#ffd60a"],
};
const fmtScore = (s) => (s >= 0 ? "+" : "") + Number(s ?? 0).toFixed(2);

function sentiMeterHtml(score) {
  const pct = Math.max(2, Math.min(98, ((Number(score) || 0) + 1) / 2 * 100));
  return `<div class="senti-meter"><i class="senti-pin" style="left:${pct}%"></i></div>
    <div class="senti-ends"><span>-1 bearish</span><span>neutral</span><span>bullish +1</span></div>`;
}

// `target` picks which half's feed to fill: "se" = the market wire (overall
// market sub-tab), "sa" = tickers and sectors (your stocks sub-tab).
function renderSentiFeed(rows, note, target = "se") {
  $(target + "-feed-note").textContent = note || "";
  $(target + "-feed").innerHTML = (rows || []).length ? rows.map((r) => {
    const noSig = !(r.pos_terms || r.neg_terms);
    const [lbl] = SENTI_LBL[r.label] || SENTI_LBL.neutral;
    const badgeCls = noSig ? "" : r.label === "bullish" ? "pos" : r.label === "bearish" ? "neg" : "mid";
    const why = [r.pos_terms ? "+ " + r.pos_terms : "", r.neg_terms ? "− " + r.neg_terms : ""]
      .filter(Boolean).join("   ");
    return `<div class="feed-item">
      <div class="feed-meta">
        <span class="senti-badge ${badgeCls}" title="${escapeHtml(why || "no scoring terms found")}">
          ${noSig ? "· no signal" : fmtScore(r.score) + " " + lbl}</span>
        ${r.source ? `<span class="src-badge">${escapeHtml(String(r.source))}</span>` : ""}
        ${r.symbol ? `<span class="src-badge">${escapeHtml(String(r.symbol))}</span>` : ""}
        <span>${timeAgo(r.date)}</span>
      </div>
      ${r.url
        ? `<a class="feed-title" href="${escapeHtml(String(r.url))}" target="_blank" rel="noopener">${escapeHtml(String(r.title || ""))}</a>`
        : `<span class="feed-title">${escapeHtml(String(r.title || ""))}</span>`}
      ${why ? `<div class="feed-sum senti-why">${escapeHtml(why)}</div>` : ""}
    </div>`;
  }).join("") : `<div class="empty">No stories returned.</div>`;
}

async function loadMarketSentiment() {
  $("se-gauge").innerHTML = `<div class="empty">Reading the wire…</div>`;
  try {
    const d = await api("/api/v1/sentiment/market?limit=80");
    const x = d.extra || {};
    const agg = x.aggregate || {};
    const [lbl, color] = SENTI_LBL[agg.label] || SENTI_LBL.neutral;
    $("se-mkt-note").textContent =
      `${agg.articles ?? 0} stories · as of ${agg.as_of ? new Date(agg.as_of).toLocaleTimeString() : "now"}`;
    $("se-gauge").innerHTML = `
      <div class="senti-hero">
        <div>
          <div class="senti-word" style="color:${color}">${lbl}</div>
          <div class="senti-num mono">${fmtScore(agg.score)}</div>
        </div>
        <div class="senti-meterwrap">${sentiMeterHtml(agg.score)}</div>
      </div>
      <p class="explain" style="margin-top:12px">${escapeHtml(agg.reading || "")}</p>
      <div class="metrics" style="margin-top:14px">
        ${[["Leaning positive", agg.bullish, "pos"], ["Leaning negative", agg.bearish, "neg"],
           ["Mixed", agg.neutral, ""], ["No signal", agg.no_signal, ""]]
          .map(([k, v, c]) => `<div class="metric"><div class="k">${k}</div><div class="v ${c}">${v ?? 0}</div></div>`).join("")}
      </div>`;

    $("se-sources").innerHTML = (x.by_source || []).map((s) => {
      const w = Math.min(50, Math.abs(s.score) * 50);
      const fill = s.score >= 0
        ? `left:50%;width:${w}%;background:var(--green)`
        : `left:${50 - w}%;width:${w}%;background:var(--red)`;
      return `<div class="sbar" title="${s.scored} of ${s.articles} stories used directional language">
        <span class="l">${escapeHtml(String(s.source))}</span>
        <div class="track"><div class="fill" style="${fill}"></div></div>
        <span class="val ${cls(s.score)}">${fmtScore(s.score)}</span></div>`;
    }).join("") || `<div class="empty">No source used directional language.</div>`;

    const dr = x.drivers || {};
    const item = (a) => `<a class="mini-link" href="${escapeHtml(String(a.url || "#"))}" target="_blank" rel="noopener">
        <b class="mono ${a.score >= 0 ? "pos" : "neg"}">${fmtScore(a.score)}</b>
        ${escapeHtml(String(a.title || "").slice(0, 96))}
        <span class="mini-src">${escapeHtml(String(a.source || ""))} · ${timeAgo(a.date)}</span></a>`;
    $("se-drivers").innerHTML = [...(dr.bullish || []), ...(dr.bearish || [])].map(item).join("")
      || `<div class="empty">No strongly-worded stories right now.</div>`;

    renderSentiFeed(d.results, "the newswire, scored");
    setStatus(`MARKET MOOD: ${lbl} (${fmtScore(agg.score)}) · ${agg.scored ?? 0} SCORED STORIES`);
  } catch (e) {
    $("se-gauge").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`;
    $("se-sources").innerHTML = ""; $("se-drivers").innerHTML = "";
  }
}
$("se-mkt-refresh").onclick = loadMarketSentiment;

// ---- sector mood board ----
// One row per GICS sector, most bullish first. "Stories" drops that sector's
// scored articles into the feed below; "History →" reuses the history panel
// via the sector's SPDR ETF (the backend maps XLE-style tickers to sector news).
let sectorArticles = {};

async function loadSectorSentiment() {
  $("ss-table").innerHTML = `<div class="empty">Reading sector news — 11 queries, ~10s on first load…</div>`;
  try {
    const d = await api("/api/v1/sentiment/sectors?limit=25");
    sectorArticles = (d.extra || {}).articles || {};
    $("ss-table").innerHTML = `<table><tr><th>Sector</th><th style="text-align:center">Mood</th>
      <th>Score</th><th>Lean</th><th></th></tr>` + d.results.map((r) => {
        const w = Math.min(50, Math.abs(r.score) * 50);
        const fill = r.score >= 0
          ? `left:50%;width:${w}%;background:var(--green)`
          : `left:${50 - w}%;width:${w}%;background:var(--red)`;
        return `<tr>
          <td>${escapeHtml(r.sector)} <span class="badge">${escapeHtml(r.etf)}</span></td>
          <td style="min-width:140px"><div class="sbar" style="grid-template-columns:1fr;margin:0" title="${escapeHtml(r.reading || "")}">
            <div class="track"><div class="fill" style="${fill}"></div></div></div></td>
          <td class="${cls(r.score)}">${fmtScore(r.score)}</td>
          <td><span class="pos">${r.bullish}▲</span> <span class="neg">${r.bearish}▼</span> <span class="dim">${r.neutral}·</span></td>
          <td><button class="linkbtn" style="margin:0" data-stories="${r.key}">Stories</button>
              <button class="linkbtn" style="margin:0 0 0 10px" data-hist="${r.etf}">History →</button></td>
        </tr>`;
      }).join("") + "</table>";

    $("ss-table").querySelectorAll("[data-stories]").forEach((el) => {
      el.onclick = () => {
        const key = el.dataset.stories;
        const row = d.results.find((r) => r.key === key);
        renderSentiFeed(sectorArticles[key] || [], `${row ? row.sector : key} sector headlines`, "sa");
        $("sa-feed").scrollIntoView({ behavior: "smooth", block: "nearest" });
      };
    });
    $("ss-table").querySelectorAll("[data-hist]").forEach((el) => {
      el.onclick = () => {
        $("sh-symbol").value = el.dataset.hist;
        $("sh-symbol").scrollIntoView({ behavior: "smooth", block: "center" });
        loadSentimentHistory();
      };
    });
    if (d.warnings && d.warnings.length) setStatus(`SECTOR MOOD LOADED (${d.warnings.length} SECTOR(S) SKIPPED)`);
  } catch (e) { $("ss-table").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`; }
}
$("ss-refresh").onclick = loadSectorSentiment;

async function runSymbolSentiment() {
  const syms = $("se-symbols").value.trim().toUpperCase();
  if (!syms) return;
  setStatus("SCORING HEADLINES…");
  $("se-warn").innerHTML = "";
  $("se-cards").innerHTML = `<div class="empty">Reading recent stories…</div>`;
  try {
    const d = await api(`/api/v1/sentiment/symbol?symbol=${encodeURIComponent(syms)}&limit=25`);
    $("se-src").style.display = "inline-block";
    $("se-src").textContent = `provider: ${d.provider}`;
    if (d.warnings && d.warnings.length) {
      $("se-warn").innerHTML =
        `<div class="warnbox">! ${d.warnings.slice(0, 3).map((w) => escapeHtml(String(w))).join("<br>! ")}</div>`;
    }
    $("se-cards").innerHTML = d.results.map((r) => {
      const [lbl, color] = SENTI_LBL[r.label] || SENTI_LBL.neutral;
      return `<div class="fcard">
        <div class="head"><span class="s">${escapeHtml(String(r.symbol))}</span>
          <span class="n">${r.articles} recent stories</span>
          <span class="edge" style="color:${color}">${lbl} ${fmtScore(r.score)}</span></div>
        ${sentiMeterHtml(r.score)}
        <div class="conf">${escapeHtml(r.reading || "")}</div>
      </div>`;
    }).join("") || `<div class="empty">No results.</div>`;
    const byNewest = Object.values((d.extra || {}).articles || {}).flat()
      .sort((a, b) => new Date(b.date || 0) - new Date(a.date || 0));
    renderSentiFeed(byNewest, `headlines for ${syms}`, "sa");
    setStatus(`SENTIMENT SCORED FOR ${d.results.length} SYMBOL(S)`);
  } catch (e) {
    $("se-cards").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`;
    setStatus("ERR: " + e.message);
  }
}
$("se-run").onclick = runSymbolSentiment;
$("se-symbols").onkeydown = (ev) => { if (ev.key === "Enter" && !ev.defaultPrevented) runSymbolSentiment(); };
$("se-watch").onclick = async () => {
  await seedSymbolsFromWatchlist();
  runSymbolSentiment();
};

// ---- sentiment history & backtest ----
// /sentiment/history rebuilds past mood from the Google News archive in
// weekly windows (monthly on 2Y). The backtest runs the news_sentiment
// strategy through the normal engine, benchmarked against holding the stock.
let shMonths = 12;
document.querySelectorAll("#sh-ranges .chip").forEach((c) => {
  c.onclick = () => {
    document.querySelectorAll("#sh-ranges .chip").forEach((x) => x.classList.remove("active"));
    c.classList.add("active");
    shMonths = +c.dataset.months;
  };
});

const shStartIso = () => isoAgo(Math.round(shMonths * 30.44));

async function loadSentimentHistory() {
  const sym = $("sh-symbol").value.trim().toUpperCase();
  if (!sym) return;
  setStatus("REBUILDING PAST SENTIMENT — FIRST RUN CAN TAKE A MINUTE…");
  $("sh-load").disabled = true;
  $("sh-bench").innerHTML = `<div class="empty">Walking the news archive window by window…</div>`;
  $("sh-signal").textContent = "";
  try {
    const start = shStartIso();
    const [hist, px] = await Promise.all([
      api(`/api/v1/sentiment/history?symbol=${encodeURIComponent(sym)}&start_date=${start}`),
      api(`/api/data/history/${encodeURIComponent(sym)}?start=${start}`).catch(() => null),
    ]);
    const rows = hist.results;
    const x = hist.extra || {};

    // Mood on the left axis, the share price on the right.
    const datasets = [{ label: "news mood", data: rows.map((r) => r.score), color: "#ffd60a", fill: true }];
    if (px && px.bars && px.bars.length) {
      const byDate = {}; px.bars.forEach((b) => { byDate[b.date] = b.close; });
      const pxDates = px.bars.map((b) => b.date);
      const asof = (d) => { // close on or before the window end
        for (let i = pxDates.length - 1; i >= 0; i--) if (pxDates[i] <= d) return byDate[pxDates[i]];
        return null;
      };
      datasets.push({ label: `${sym} price`, data: rows.map((r) => asof(String(r.date))), color: "#5ac8fa", y2: true });
    }
    $("sh-chart").style.display = "block";
    drawLine("sh-chart", rows.map((r) => String(r.date)), datasets);

    const b = x.benchmark;
    $("sh-bench").innerHTML = b ? `
      <div class="metrics" style="margin-top:2px">
        ${[["Latest window", fmtScore(b.latest), b.latest >= 0.1 ? "pos" : b.latest <= -0.1 ? "neg" : ""],
           ["Vs its own past", b.percentile + "th pctile", b.percentile >= 60 ? "pos" : b.percentile <= 40 ? "neg" : ""],
           ["Average", fmtScore(b.mean), ""],
           ["Best / worst", `${fmtScore(b.best.score)} / ${fmtScore(b.worst.score)}`, ""]]
          .map(([k, v, c]) => `<div class="metric"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`).join("")}
      </div>
      <p class="explain" style="margin:12px 0 0">${escapeHtml(b.reading || "")}</p>`
      : `<div class="empty">Not enough scored windows to benchmark yet.</div>`;

    $("sh-signal").textContent = (x.signal && x.signal.reading)
      ? x.signal.reading + (x.signal.correlation != null
          ? ` Mood-to-next-window correlation: ${x.signal.correlation}.` : "")
      : "";
    $("sh-src").style.display = "inline-block";
    $("sh-src").textContent = `${rows.length} windows`;
    if (hist.warnings && hist.warnings.length) setStatus("HISTORY LOADED (" + hist.warnings[0] + ")");
    else setStatus(`SENTIMENT HISTORY · ${sym} · ${rows.length} WINDOWS`);
  } catch (e) {
    $("sh-bench").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`;
    setStatus("ERR: " + e.message);
  }
  $("sh-load").disabled = false;
}
$("sh-load").onclick = loadSentimentHistory;
$("sh-symbol").onkeydown = (ev) => { if (ev.key === "Enter" && !ev.defaultPrevented) loadSentimentHistory(); };

$("sh-backtest").onclick = async () => {
  const sym = $("sh-symbol").value.trim().toUpperCase();
  if (!sym) return;
  const btn = $("sh-backtest");
  btn.disabled = true; btn.textContent = "Running…";
  setStatus("BACKTESTING THE SENTIMENT SIGNAL — FIRST RUN CAN TAKE A MINUTE…");
  try {
    const cap = 100000;
    const d = await api("/api/backtest/run", {
      method: "POST",
      body: {
        strategy: "news_sentiment", engine: "vectorized",
        symbols: [sym], benchmark: sym, // benchmark = holding the same stock
        start: shStartIso(), end: null,
        commission_bps: 1.0, slippage_bps: 2.0, initial_capital: cap,
        params: { threshold: 0.05, smooth: 2 },
      },
    });
    const m = d.metrics;
    const finalVal = d.equity_curve.values[d.equity_curve.values.length - 1];
    $("sh-headline").textContent =
      `Trading ${sym} on its news mood would have turned ${fmt$(cap)} into ${fmt$(finalVal)} — ` +
      `${m.total_return >= 0 ? "a gain of" : "a loss of"} ${Math.abs(m.total_return * 100).toFixed(1)}%.`;
    let compare = "";
    if (d.benchmark) {
      const bTr = d.benchmark.total_return;
      compare = `Just holding ${sym} returned ${fmtPct(bTr, true)} over the same period — ` +
        (m.total_return > bTr ? "the sentiment signal came out ahead."
                              : "holding it the whole time did better.");
    }
    $("sh-compare").textContent = compare;
    const datasets = [{ label: "Sentiment strategy", data: d.equity_curve.values, color: "#00c805" }];
    if (d.benchmark) datasets.push({ label: `hold ${sym}`, data: d.benchmark.values, color: "#6f7377", dash: true });
    drawLine("sh-btchart", d.equity_curve.dates, datasets);
    renderMetrics("sh-btmetrics", m, d);
    $("sh-result").style.display = "block";
    setStatus(`SENTIMENT BACKTEST DONE · #${d.run_id} — saved under Past tests`);
  } catch (e) { setStatus("ERR: " + e.message); }
  btn.disabled = false; btn.textContent = "▶ Backtest this signal";
};

// ---------- data platform explorer ----------
let registry = null;
let currentCmd = null;
let lastRows = [];
let lastProvider = null;

let menuGuides = {};
async function loadRegistry() {
  if (registry) return;
  setStatus("LOADING COMMAND REGISTRY…");
  const d = await api("/api/v1/_registry");
  registry = d.results;
  menuGuides = d.guides || {};
  $("cx-search").placeholder = `Search ${registry.length} commands…`;
  renderCommandList("");
  setStatus(`${registry.length} DATA COMMANDS AVAILABLE`);
}
$("cx-search").oninput = (e) => renderCommandList(e.target.value.trim().toLowerCase());

function renderCommandList(filter) {
  if (!registry) return;
  const hits = registry.filter(
    (c) => !filter || c.path.toLowerCase().includes(filter) ||
           c.description.toLowerCase().includes(filter) ||
           c.providers.some((p) => p.includes(filter))
  );
  const groups = {};
  for (const c of hits) (groups[c.menu || "/"] ||= []).push(c);
  $("cx-list").innerHTML = Object.keys(groups).sort().map((menu) =>
    `<div class="cmdgroup">${menu.replace(/^\//, "").toUpperCase() || "ROOT"}</div>` +
    groups[menu].map((c) =>
      `<button class="cmditem" data-path="${c.path}">${c.name}
        <small>${c.description}</small></button>`).join("")
  ).join("") || `<div class="hint">no matches</div>`;
  document.querySelectorAll(".cmditem").forEach((el) => {
    el.onclick = () => {
      document.querySelectorAll(".cmditem").forEach((x) => x.classList.remove("active"));
      el.classList.add("active");
      selectCommand(el.dataset.path);
    };
  });
}

function selectCommand(path) {
  currentCmd = registry.find((c) => c.path === path);
  $("cx-title").textContent = path;
  $("cx-desc").innerHTML =
    `${currentCmd.description}<br /><code>${currentCmd.methods[0]} ${currentCmd.endpoint}</code>` +
    (currentCmd.providers.length ? ` · providers: ${currentCmd.providers.join(", ")}` : "");

  // Menu guide — what this whole area is for.
  const topMenu = currentCmd.path.split("/")[1];
  const guide = menuGuides[topMenu];
  $("cx-guide").style.display = guide ? "block" : "none";
  if (guide) $("cx-guide").innerHTML =
    `<b>About the ${topMenu} menu:</b> ${escapeHtml(guide)}`;

  // The command's own long-form documentation, when it has one.
  const doc = (currentCmd.doc || "").trim();
  const extraDoc = doc && doc.split("\n")[0] !== currentCmd.description ? doc
    : doc.split("\n").slice(1).join("\n").trim();
  $("cx-doc").style.display = extraDoc ? "block" : "none";
  if (extraDoc) $("cx-doc").innerHTML =
    `<b>Details:</b><br /><span style="white-space:pre-line">${escapeHtml(extraDoc)}</span>`;

  // Runnable example with a one-click fill.
  const ex = currentCmd.example;
  $("cx-example").style.display = ex ? "block" : "none";
  if (ex) {
    $("cx-example").innerHTML = `<b>Example:</b>
      <button class="linkbtn" id="cx-try" style="margin:0 0 0 8px">Use this example ↓</button><br />
      <code>${escapeHtml(ex.url)}</code><br />
      <code>${escapeHtml(ex.python)}</code>`;
  }

  const DATE_PARAMS = new Set(["start_date", "end_date", "date", "compare_date", "as_of",
                               "day", "expiration"]);
  $("cx-params").innerHTML = currentCmd.parameters.map((p) => {
    const value = p.default === null || p.default === undefined ? "" : p.default;
    const isDate = DATE_PARAMS.has(p.name) && /str/i.test(p.type);
    const help = p.description ? escapeHtml(p.description)
      : `${p.type}${p.required ? " · required" : ""}`;
    return `<label>${p.name}${p.required ? " *" : ""}
      <input data-param="${p.name}" ${isDate ? 'type="date"' : ""}
             value="${String(value).replace(/"/g, "&quot;")}"
             placeholder="${p.type}" />
      <small title="${escapeHtml(p.type)}">${help}</small></label>`;
  }).join("") || `<div class="hint">no parameters</div>`;

  if (ex) {
    $("cx-try").onclick = () => {
      for (const [k, v] of Object.entries(ex.params)) {
        const input = document.querySelector(`#cx-params input[data-param="${k}"]`);
        if (input) input.value = v;
      }
      $("cx-run").click();
    };
  }
  document.querySelectorAll("#cx-params input").forEach((el) => {
    const p = el.dataset.param;
    if (["symbol", "symbols", "benchmark"].includes(p)) attachAutocomplete(el, p !== "benchmark");
  });
  $("cx-actions").style.display = "flex";
  $("cx-out").innerHTML = "";
  $("cx-chart").style.display = "none";
  $("cx-meta").style.display = "none";
  $("cx-csv").style.display = "none";
  $("cx-saveres").style.display = "none";
}

function currentParams() {
  const params = {};
  for (const el of document.querySelectorAll("#cx-params input")) {
    const raw = el.value.trim();
    if (!raw) continue;
    const spec = currentCmd.parameters.find((p) => p.name === el.dataset.param);
    if (/List|Dict/.test(spec.type)) {
      try { params[spec.name] = JSON.parse(raw); } catch { continue; }
    } else if (/int|float/.test(spec.type) && !isNaN(Number(raw))) {
      params[spec.name] = Number(raw);
    } else if (/bool/.test(spec.type)) {
      params[spec.name] = raw.toLowerCase() === "true";
    } else {
      params[spec.name] = raw;
    }
  }
  return params;
}

$("cx-run").onclick = async () => {
  if (!currentCmd) return;
  setStatus(`RUNNING ${currentCmd.path}…`);
  $("cx-out").innerHTML = "";
  const query = new URLSearchParams();
  const body = {};
  for (const el of document.querySelectorAll("#cx-params input")) {
    const raw = el.value.trim();
    if (!raw) continue;
    const spec = currentCmd.parameters.find((p) => p.name === el.dataset.param);
    if (/List|Dict/.test(spec.type)) {
      try { body[spec.name] = JSON.parse(raw); }
      catch { $("cx-out").innerHTML = `<div class="errbox">${spec.name} must be valid JSON</div>`; return; }
    } else {
      query.set(spec.name, raw);
    }
  }
  const qs = query.toString();
  const url = currentCmd.endpoint + (qs ? `?${qs}` : "");
  const method = currentCmd.methods[0];
  try {
    const d = await api(url, method === "POST" ? { method, body } : {});
    renderCommandResult(d);
    setStatus(`${currentCmd.path} · ${Array.isArray(d.results) ? d.results.length : 1} ROW(S)`);
  } catch (e) {
    $("cx-out").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`;
    setStatus("ERR: " + e.message);
  }
};

$("cx-save").onclick = async () => {
  if (!currentCmd) return;
  const nm = prompt("Name this saved command:", currentCmd.name);
  if (!nm) return;
  try {
    await api("/api/user/saved", {
      method: "POST",
      body: { name: nm, command_path: currentCmd.path, parameters: currentParams(), is_favorite: true },
    });
    setStatus(`SAVED "${nm}"`);
  } catch (e) { setStatus("ERR: " + e.message); }
};

function renderCommandResult(d) {
  const rows = Array.isArray(d.results) ? d.results : [d.results];
  lastRows = rows;
  $("cx-meta").style.display = "inline-block";
  $("cx-meta").textContent = `provider: ${d.provider || "-"} · ${rows.length} row(s)`;
  $("cx-csv").style.display = rows.length ? "inline-block" : "none";
  $("cx-saveres").style.display = rows.length ? "inline-block" : "none";
  lastProvider = d.provider || null;
  let warn = "";
  if (d.warnings && d.warnings.length)
    warn = `<div class="warnbox">! ${d.warnings.slice(0, 4).map((w) => escapeHtml(String(w))).join("<br>! ")}</div>`;
  if (!rows.length || typeof rows[0] !== "object") {
    $("cx-out").innerHTML = warn + `<pre>${escapeHtml(JSON.stringify(d.results, null, 2).slice(0, 4000))}</pre>`;
    $("cx-chart").style.display = "none";
    return;
  }
  const cols = [...new Set(rows.flatMap((r) => Object.keys(r)))];
  const shown = rows.slice(0, 250);
  const cell = (v) =>
    v === null || v === undefined ? "-"
      : typeof v === "number" ? (Number.isInteger(v) ? v.toLocaleString() : v.toPrecision(6))
      : escapeHtml(String(v).length > 90 ? String(v).slice(0, 90) + "…" : String(v));
  const html = `<table><tr>${cols.map((c) => `<th>${c}</th>`).join("")}</tr>` +
    shown.map((r) => `<tr>${cols.map((c) => `<td>${cell(r[c])}</td>`).join("")}</tr>`).join("") +
    `</table>` + (rows.length > shown.length
      ? `<div class="hint">showing ${shown.length} of ${rows.length} rows — download the CSV for all</div>` : "");
  $("cx-out").innerHTML = warn + html;
  autoChart(rows, cols);
}

function autoChart(rows, cols) {
  const xCol = cols.find((c) => ["date", "period_ending", "record_date", "maturity"].includes(c));
  const numeric = cols.filter(
    (c) => c !== xCol && rows.some((r) => typeof r[c] === "number") &&
           !["year", "cik", "lag", "window"].includes(c)
  ).slice(0, 4);
  if (!xCol || !numeric.length || rows.length < 3) {
    $("cx-chart").style.display = "none";
    return;
  }
  const colours = ["#00c805", "#5ac8fa", "#ffd60a", "#ff5000"];
  $("cx-chart").style.display = "block";
  drawLine("cx-chart", rows.map((r) => r[xCol]),
    numeric.map((c, i) => ({ label: c, data: rows.map((r) => r[c]), color: colours[i % 4] })));
}

// Store the exact rows on the server — a point-in-time snapshot, unlike
// ★ Save which re-runs the command for fresh data.
$("cx-saveres").onclick = async () => {
  if (!lastRows.length || !currentCmd) return;
  const nm = prompt("Name this result set:",
    `${currentCmd.name} ${new Date().toISOString().slice(0, 10)}`);
  if (!nm) return;
  try {
    const saved = await api("/api/user/results", {
      method: "POST",
      body: {
        name: nm,
        command_path: currentCmd.path,
        parameters: currentParams(),
        results: lastRows,
        provider: lastProvider,
      },
    });
    setStatus(`SAVED ${saved.row_count} ROW(S) AS "${nm}"${saved.truncated ? " (TRUNCATED TO 5000)" : ""}`);
  } catch (e) { setStatus("ERR: " + e.message); }
};

$("cx-csv").onclick = () => {
  if (!lastRows.length) return;
  const cols = [...new Set(lastRows.flatMap((r) => Object.keys(r)))];
  const esc = (v) => {
    const s = v === null || v === undefined ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const csv = [cols.join(","), ...lastRows.map((r) => cols.map((c) => esc(r[c])).join(","))].join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = (currentCmd ? currentCmd.path.replace(/\//g, "_").replace(/^_/, "") : "export") + ".csv";
  a.click();
  URL.revokeObjectURL(a.href);
};

// ---------- modeling (DCF) ----------
// A model is one object — `mdAssumptions` — and the whole screen is a function
// of it. Editing any control writes into that object and re-values; nothing on
// screen holds state of its own. The valuation itself is computed server-side
// so the arithmetic has exactly one implementation (backend/valuation/dcf.py).
let mdSymbol = null, mdAssumptions = null, mdEvidence = null, mdValuation = null;
let mdSavedModels = [], mdActiveId = null, mdTimer = null;

const MD_PCT = (x, digits = 1) => x == null ? "—" : (x * 100).toFixed(digits) + "%";
const mdMoney = (x) => x == null ? "—" : (x < 0 ? "-" : "") + "$" + fmtBig(Math.abs(x));

async function loadModelingView() {
  await loadSavedModels();
  if (!mdSymbol) {
    const seededFrom = $("md-symbol").value.trim();
    if (!seededFrom) $("md-symbol").focus();
  }
}

async function loadSavedModels() {
  try {
    mdSavedModels = await api("/api/modeling/models");
  } catch { mdSavedModels = []; }
  renderSavedModels();
}

function renderSavedModels() {
  if (!mdSavedModels.length) {
    $("md-saved").innerHTML = `<div class="empty">Nothing saved yet. Build a model and name it.</div>`;
    return;
  }
  $("md-saved").innerHTML = mdSavedModels.map((m) => {
    const gap = m.value_per_share && m.price_at_save ? m.value_per_share / m.price_at_save - 1 : null;
    return `<div class="md-saved-item ${m.id === mdActiveId ? "active" : ""}" data-model="${m.id}">
      <div class="t"><span class="nm">${escapeHtml(m.name)}</span>
        <span class="sy">${escapeHtml(m.symbol)}</span></div>
      <div class="m">
        <span>$${(m.value_per_share ?? 0).toFixed(2)}</span>
        ${gap == null ? "" : `<span class="${cls(gap)}">${gap >= 0 ? "+" : ""}${(gap * 100).toFixed(0)}%</span>`}
        <span class="del" data-del="${m.id}" title="Delete">✕</span>
      </div></div>`;
  }).join("");
  $("md-saved").querySelectorAll("[data-model]").forEach((el) => {
    el.onclick = (event) => {
      if (event.target.dataset.del) return;
      openSavedModel(Number(el.dataset.model));
    };
  });
  $("md-saved").querySelectorAll("[data-del]").forEach((el) => {
    el.onclick = async (event) => {
      event.stopPropagation();
      const model = mdSavedModels.find((m) => m.id === Number(el.dataset.del));
      if (!confirm(`Delete “${model ? model.name : "this model"}”?`)) return;
      try { await api(`/api/modeling/models/${el.dataset.del}`, { method: "DELETE" }); } catch { /* gone already */ }
      if (mdActiveId === Number(el.dataset.del)) mdActiveId = null;
      loadSavedModels();
    };
  });
}

async function buildModel(symbol) {
  const sym = String(symbol || "").trim().toUpperCase();
  if (!sym) return;
  $("md-build-msg").className = "msg";
  $("md-build-msg").textContent = `Reading ${sym}'s filings…`;
  try {
    const d = await api(`/api/modeling/seed?symbol=${encodeURIComponent(sym)}`);
    mdSymbol = d.symbol;
    mdAssumptions = d.assumptions;
    mdEvidence = d.evidence;
    mdValuation = d.valuation;
    mdActiveId = null;
    $("md-build-msg").textContent = "";
    $("md-name-input").value = `${mdSymbol} base case`;
    showModel();
  } catch (e) {
    $("md-build-msg").textContent = e.message;
  }
}

async function openSavedModel(id) {
  const saved = mdSavedModels.find((m) => m.id === id);
  if (!saved) return;
  $("md-build-msg").textContent = "";
  mdActiveId = id;
  mdSymbol = saved.symbol;
  $("md-name-input").value = saved.name;
  // Re-seed for the evidence panel (the filings may have moved on), but keep
  // the saved assumptions — those are the model.
  try {
    const seeded = await api(`/api/modeling/seed?symbol=${encodeURIComponent(saved.symbol)}`);
    mdEvidence = seeded.evidence;
  } catch { mdEvidence = null; }
  const full = await api(`/api/modeling/models/${id}`);
  mdAssumptions = full.assumptions;
  showModel();
  renderSavedModels();
  revalue();
}

function showModel() {
  $("md-empty").style.display = "none";
  $("md-model").style.display = "";
  renderModel();
}

// Typing in a box should not fire a request per keystroke.
function scheduleRevalue() {
  clearTimeout(mdTimer);
  mdTimer = setTimeout(revalue, 320);
}

async function revalue() {
  if (!mdSymbol || !mdAssumptions) return;
  try {
    mdValuation = await api("/api/modeling/value", {
      method: "POST",
      body: { symbol: mdSymbol, assumptions: mdAssumptions, sensitivity: true },
    });
    $("md-save-msg").className = "msg";
    $("md-save-msg").textContent = "";
    renderAnswer();
  } catch (e) {
    // An impossible corner (r ≤ g, say) is a normal thing to type on the way
    // to a sensible one: say so and leave the last good answer on screen.
    $("md-save-msg").className = "msg";
    $("md-save-msg").textContent = e.message;
  }
}

function renderModel() {
  renderAssumptions();
  renderAnswer();
  renderEvidence();
}

function renderAnswer() {
  const v = mdValuation;
  if (!v) return;
  const ev = mdEvidence || {};
  $("md-sym").textContent = mdSymbol;
  $("md-name").textContent = ev.name || "";
  $("md-source").textContent = ev.periods
    ? `seeded from ${ev.periods.length} filed periods · ${Object.values(ev.provider_by_statement || {})
        .filter(Boolean).join("/") || "sec"}`
    : "";

  $("md-value").textContent = "$" + (v.value_per_share ?? 0).toLocaleString("en-US",
    { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const up = $("md-upside");
  if (v.price) {
    up.className = "md-upside mono " + cls(v.upside);
    up.textContent = `${v.upside >= 0 ? "+" : ""}${(v.upside * 100).toFixed(1)}% vs $${v.price.toFixed(2)}`;
  } else { up.className = "md-upside mono dim"; up.textContent = "no live price"; }

  const row = (k, val, cssClass = "") =>
    `<div class="r ${cssClass}"><span>${k}</span><span>${val}</span></div>`;
  $("md-bridge").innerHTML =
    row("PV of forecast", mdMoney(v.pv_explicit)) +
    row("PV of terminal", `${mdMoney(v.pv_terminal)} <span class="dim">(${MD_PCT(v.terminal_share, 0)})</span>`) +
    row("Enterprise value", mdMoney(v.enterprise_value), "tot") +
    row("Less net debt", mdMoney(-v.net_debt)) +
    row("Equity value", mdMoney(v.equity_value), "tot") +
    row("Discount rate", MD_PCT(v.discount_rate, 2));

  $("md-warnings").innerHTML = (v.warnings || [])
    .map((w) => `<div class="w">${escapeHtml(w)}</div>`).join("");

  renderProjection(v);
  renderSensitivity(v);
  $("md-wacc-note").textContent = mdAssumptions.discount_rate != null
    ? "set directly" : `weighted: ${MD_PCT(v.discount_rate, 2)}`;
  $("md-terminal-note").textContent = MD_PCT(v.terminal_share, 0) + " of enterprise value";
}

function renderProjection(v) {
  const rows = v.projections || [];
  if (!rows.length) { $("md-projection").innerHTML = ""; return; }
  const line = (label, pick, money = true) =>
    `<tr><td class="lbl">${label}</td>` +
    rows.map((p) => `<td class="num">${money ? mdMoney(pick(p)) : pick(p)}</td>`).join("") + `</tr>`;
  $("md-projection").innerHTML = `<table>
    <tr><th></th>${rows.map((p) => `<th class="num">Year ${p.year}</th>`).join("")}</tr>
    ${line("Revenue", (p) => p.revenue)}
    ${line("Operating income", (p) => p.ebit)}
    ${line("NOPAT (after tax)", (p) => p.nopat)}
    ${line("+ Depreciation", (p) => p.depreciation)}
    ${line("− Capital expenditure", (p) => -p.capex)}
    ${line("− Change in working capital", (p) => -p.nwc_change)}
    <tr class="tot"><td class="lbl">Free cash flow</td>${
      rows.map((p) => `<td class="num">${mdMoney(p.free_cash_flow)}</td>`).join("")}</tr>
    ${line("Discount factor", (p) => p.discount_factor.toFixed(3), false)}
    <tr class="tot"><td class="lbl">Present value</td>${
      rows.map((p) => `<td class="num">${mdMoney(p.present_value)}</td>`).join("")}</tr>
  </table>`;
}

function renderSensitivity(v) {
  const s = v.sensitivity;
  if (!s) { $("md-sensitivity").innerHTML = ""; return; }
  const byGrowth = s.terminal_axis === "terminal_growth";
  $("md-sens-note").textContent =
    `value per share across discount rate and ${byGrowth ? "terminal growth" : "exit multiple"}`;
  const head = s.terminal_values.map((t) =>
    `<th class="num axis">${byGrowth ? MD_PCT(t, 2) : t.toFixed(1) + "×"}</th>`).join("");
  const body = s.grid.map((row, i) => {
    const rate = s.discount_rates[i];
    const cells = row.map((cell, j) => {
      if (cell == null) return `<td class="cellv dim">—</td>`;
      // Shade by upside against the live price: this is the question the grid
      // is being asked, not the absolute dollar value.
      const gap = v.price ? cell / v.price - 1 : 0;
      const strength = Math.min(Math.abs(gap), 0.6) / 0.6 * 0.28;
      const tint = v.price ? (gap >= 0 ? `rgba(0,200,5,${strength})` : `rgba(255,80,0,${strength})`) : "";
      const here = Math.abs(rate - v.discount_rate) < 1e-9 &&
        Math.abs(s.terminal_values[j] - (byGrowth ? mdAssumptions.terminal_growth
                                                  : mdAssumptions.exit_multiple)) < 1e-9;
      return `<td class="cellv ${here ? "here" : ""}" style="background:${tint}"
        title="${v.price ? (gap >= 0 ? "+" : "") + (gap * 100).toFixed(0) + "% vs price" : ""}"
        >$${cell.toFixed(2)}</td>`;
    }).join("");
    return `<tr><td class="lbl">${MD_PCT(rate, 2)}</td>${cells}</tr>`;
  }).join("");
  $("md-sensitivity").innerHTML = `<table>
    <tr><th class="axis">WACC \\ ${byGrowth ? "g" : "EV/EBITDA"}</th>${head}</tr>${body}</table>`;
}

// --- the controls ------------------------------------------------------------
function mdSet(key, value) {
  mdAssumptions[key] = value;
  scheduleRevalue();
}

// Percentages are typed as percentages and money is typed in millions — a
// twelve-digit raw figure neither fits the box nor is what anyone means when
// they say "net debt". `scale` records the multiplier so the value written back
// into the model is still in units.
function mdNumberRow(key, label, { pct = false, step = 0.5, hint = "", digits = 2,
                                   scale = 1, unit = "" } = {}) {
  const raw = mdAssumptions[key];
  const shown = pct ? (raw * 100).toFixed(digits) : (raw / scale).toFixed(digits);
  return `<div class="md-row">
    <label>${label}${unit ? ` <span class="unit">${unit}</span>` : ""}
      ${hint ? `<span class="was">${escapeHtml(hint)}</span>` : ""}</label>
    <input type="number" step="${step}" value="${shown}"
      data-md="${key}" data-pct="${pct ? 1 : 0}" data-scale="${scale}" />
  </div>`;
}

function mdPerYearRow(key, label, hint) {
  const years = mdAssumptions.years;
  const values = Array.isArray(mdAssumptions[key])
    ? mdAssumptions[key] : Array(years).fill(mdAssumptions[key]);
  const boxes = Array.from({ length: years }, (_, i) => `<div>
      <label>Y${i + 1}</label>
      <input type="number" step="0.5" value="${((values[i] ?? values[values.length - 1]) * 100).toFixed(1)}"
        data-mdyear="${key}" data-i="${i}" />
    </div>`).join("");
  return `<div class="md-row wide">
    <label>${label} <span class="unit">% per year</span>
      ${hint ? `<span class="was">${escapeHtml(hint)}</span>` : ""}</label>
    <div class="peryear">${boxes}</div>
  </div>`;
}

function renderAssumptions() {
  const h = mdEvidence && mdEvidence.history ? mdEvidence.history : {};
  const avg = (list) => (list && list.length)
    ? `filed: ${list.map((v) => (v * 100).toFixed(1) + "%").join(", ")}` : "";

  $("md-assumptions").innerHTML =
    `<div class="md-row">
      <label>Forecast years</label>
      <select data-md-years>${[3, 5, 7, 10].map((y) =>
        `<option value="${y}" ${y === mdAssumptions.years ? "selected" : ""}>${y}</option>`).join("")}</select>
    </div>` +
    mdPerYearRow("revenue_growth", "Revenue growth",
      h.revenue_cagr != null ? `trailing CAGR ${(h.revenue_cagr * 100).toFixed(1)}%` : "") +
    mdPerYearRow("operating_margin", "Operating margin", avg(h.operating_margin)) +
    mdNumberRow("tax_rate", "Tax rate", { pct: true, hint: avg(h.effective_tax_rate) }) +
    mdNumberRow("depreciation_pct_revenue", "Depreciation, % of revenue",
      { pct: true, hint: avg(h.depreciation_pct_revenue) }) +
    mdNumberRow("capex_pct_revenue", "Capital expenditure, % of revenue",
      { pct: true, hint: avg(h.capex_pct_revenue) }) +
    mdNumberRow("nwc_pct_revenue_change", "Working capital, % of revenue growth",
      { pct: true, hint: "charged on the increase in revenue, not its level" }) +
    mdNumberRow("net_debt", "Net debt", { step: 100, digits: 0, scale: 1e6, unit: "$m",
      hint: `${mdMoney(mdAssumptions.net_debt)} — debt less cash and short-term investments` }) +
    mdNumberRow("shares_diluted", "Diluted shares", { step: 1, digits: 1, scale: 1e6, unit: "m",
      hint: fmtBig(mdAssumptions.shares_diluted) + " shares" }) +
    mdNumberRow("revenue_base", "Base revenue (last filed year)",
      { step: 100, digits: 0, scale: 1e6, unit: "$m", hint: mdMoney(mdAssumptions.revenue_base) });

  const override = mdAssumptions.discount_rate != null;
  $("md-wacc").innerHTML =
    `<div class="md-row">
      <label>Set the rate directly<span class="was">otherwise it is built from the weights below</span></label>
      <input type="checkbox" data-md-override ${override ? "checked" : ""} />
    </div>` +
    (override
      ? mdNumberRow("discount_rate", "Discount rate", { pct: true })
      : mdNumberRow("equity_weight", "Equity weight", { pct: true,
          hint: `debt weight ${MD_PCT(1 - mdAssumptions.equity_weight, 1)}` }) +
        mdNumberRow("cost_of_equity", "Cost of equity", { pct: true,
          hint: mdEvidence ? `CAPM: ${MD_PCT(mdEvidence.risk_free_rate, 2)} + ${
            (mdEvidence.beta ?? 1).toFixed(2)}β × ${MD_PCT(mdEvidence.equity_risk_premium, 1)}` : "" }) +
        mdNumberRow("cost_of_debt", "Cost of debt (pre-tax)", { pct: true,
          hint: "interest expense over total debt" }));

  const perpetuity = mdAssumptions.terminal_method === "perpetuity";
  $("md-terminal").innerHTML =
    `<div class="md-row">
      <label>Method</label>
      <select data-md-terminal>
        <option value="perpetuity" ${perpetuity ? "selected" : ""}>Perpetuity</option>
        <option value="exit_multiple" ${!perpetuity ? "selected" : ""}>Exit multiple</option>
      </select>
    </div>` +
    (perpetuity
      ? mdNumberRow("terminal_growth", "Terminal growth", { pct: true, step: 0.1,
          hint: "must stay below the discount rate" })
      : mdNumberRow("exit_multiple", "Exit EV / EBITDA", { step: 0.5, digits: 1 })) +
    `<div class="md-row">
      <label>Mid-year discounting<span class="was">cash arrives across the year, not on its last day</span></label>
      <input type="checkbox" data-md-midyear ${mdAssumptions.mid_year ? "checked" : ""} />
    </div>`;

  wireAssumptionInputs();
}

function wireAssumptionInputs() {
  document.querySelectorAll("#view-modeling [data-md]").forEach((el) => {
    el.oninput = () => {
      const raw = parseFloat(el.value);
      if (Number.isNaN(raw)) return;
      const scale = Number(el.dataset.scale || 1);
      mdSet(el.dataset.md, el.dataset.pct === "1" ? raw / 100 : raw * scale);
    };
  });
  document.querySelectorAll("#view-modeling [data-mdyear]").forEach((el) => {
    el.oninput = () => {
      const raw = parseFloat(el.value);
      if (Number.isNaN(raw)) return;
      const key = el.dataset.mdyear;
      const years = mdAssumptions.years;
      const current = Array.isArray(mdAssumptions[key])
        ? mdAssumptions[key].slice() : Array(years).fill(mdAssumptions[key]);
      while (current.length < years) current.push(current[current.length - 1]);
      current[Number(el.dataset.i)] = raw / 100;
      mdSet(key, current.slice(0, years));
    };
  });
  const years = document.querySelector("#view-modeling [data-md-years]");
  if (years) years.onchange = () => {
    mdAssumptions.years = Number(years.value);
    renderAssumptions();            // the per-year rows change shape
    scheduleRevalue();
  };
  const override = document.querySelector("#view-modeling [data-md-override]");
  if (override) override.onchange = () => {
    mdAssumptions.discount_rate = override.checked
      ? Number((mdValuation ? mdValuation.discount_rate : 0.09).toFixed(4)) : null;
    renderAssumptions();
    scheduleRevalue();
  };
  const terminal = document.querySelector("#view-modeling [data-md-terminal]");
  if (terminal) terminal.onchange = () => {
    mdAssumptions.terminal_method = terminal.value;
    renderAssumptions();
    scheduleRevalue();
  };
  const midYear = document.querySelector("#view-modeling [data-md-midyear]");
  if (midYear) midYear.onchange = () => mdSet("mid_year", midYear.checked);
}

function renderEvidence() {
  const ev = mdEvidence;
  if (!ev) { $("md-evidence").innerHTML = ""; return; }
  const h = ev.history || {};
  const line = (label, values, format) => {
    const list = values || [];
    if (!list.length) return "";
    return `<tr><td class="lbl">${label}</td>` +
      ev.periods.slice(0, list.length).map((p, i) =>
        `<td class="num">${format(list[i])}</td>`).join("") + `</tr>`;
  };
  const money = (x) => mdMoney(x);
  const pct = (x) => MD_PCT(x, 1);
  $("md-evidence").innerHTML = `<table>
    <tr><th></th>${(ev.periods || []).slice(0, 6).map((p) =>
      `<th class="num">${escapeHtml(String(p).slice(0, 7))}</th>`).join("")}</tr>
    ${line("Revenue", h.revenue, money)}
    ${line("Operating margin", h.operating_margin, pct)}
    ${line("Effective tax rate", h.effective_tax_rate, pct)}
    ${line("Depreciation, % revenue", h.depreciation_pct_revenue, pct)}
    ${line("Capital expenditure, % revenue", h.capex_pct_revenue, pct)}
  </table>
  <div class="fs-foot">${(ev.notes || []).map((n) => `<span>${escapeHtml(n)}</span>`).join("")}</div>`;
}

$("md-build").onclick = () => buildModel($("md-symbol").value);
$("md-symbol").onkeydown = (e) => { if (e.key === "Enter") buildModel($("md-symbol").value); };
$("md-reseed").onclick = () => { if (mdSymbol) buildModel(mdSymbol); };

$("md-save").onclick = async () => {
  if (!mdSymbol || !mdAssumptions) return;
  const name = $("md-name-input").value.trim();
  const msg = $("md-save-msg");
  if (!name) { msg.className = "msg"; msg.textContent = "Give the model a name."; return; }
  try {
    if (mdActiveId) {
      await api(`/api/modeling/models/${mdActiveId}`, {
        method: "PUT", body: { name, assumptions: mdAssumptions },
      });
    } else {
      const created = await api("/api/modeling/models", {
        method: "POST", body: { name, symbol: mdSymbol, assumptions: mdAssumptions },
      });
      mdActiveId = created.id;
    }
    msg.className = "msg ok";
    msg.textContent = "Saved.";
    loadSavedModels();
  } catch (e) { msg.className = "msg"; msg.textContent = e.message; }
};

// ---------- insights (factors) ----------
const FACTOR_LABELS = { MKT: "Follows market", MOM: "Rides trends", LOWVOL: "Stays steady" };
const barPct = { MKT: (v) => Math.min(100, (v / 2) * 100), MOM: (v) => Math.min(100, Math.max(4, ((v + 1) / 2.5) * 100)), LOWVOL: (v) => Math.min(100, Math.max(4, ((v + 1) / 2) * 100)) };
$("fa-run").onclick = async () => {
  setStatus("RUNNING FACTOR REGRESSIONS…");
  try {
    const d = await api("/api/factors/analyze", {
      method: "POST",
      body: { symbols: $("fa-symbols").value.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean), start: $("fa-start").value.trim() },
    });
    $("fa-out").innerHTML = d.symbols.map((sym) => {
      const a = d.assets[sym];
      if (a.error) return `<div class="fcard"><div class="head"><span class="s">${sym}</span></div><p>${escapeHtml(a.error)}</p></div>`;
      const b = a.betas.MKT ?? Object.values(a.betas)[0] ?? 0;
      const marketDesc = b > 1.5 ? "swings much harder than the market" : b > 1.1 ? "moves a bit more than the market" : "moves roughly with the market";
      const alphaDesc = a.alpha_tstat > 2 ? `it has genuinely beaten the market by about ${(a.alpha_annual * 100).toFixed(1)}% a year after accounting for risk`
        : a.alpha_tstat > 0.8 ? "it may have a small edge over the market, but the evidence is thin"
        : a.alpha_annual < 0 ? "it has slightly trailed what its risk level would predict"
        : "its returns are about what its risk level would predict";
      const edgeColor = a.alpha_annual >= 0.02 && a.alpha_tstat > 1 ? "var(--green)" : a.alpha_annual < 0 ? "var(--red)" : "var(--muted)";
      const bars = d.factors.map((f) => {
        const v = a.betas[f];
        const pct = (barPct[f] ? barPct[f](v) : Math.min(100, Math.abs(v) * 60)).toFixed(0);
        return `<div class="fbar"><span class="l">${FACTOR_LABELS[f] || f}</span>
          <div class="track"><div class="fill" style="width:${pct}%"></div></div>
          <span class="val">${v.toFixed(2)}</span></div>`;
      }).join("");
      const conf = a.r_squared > 0.7 ? `High confidence — these factors explain ${(a.r_squared * 100).toFixed(0)}% of its moves.`
        : a.r_squared > 0.55 ? `Moderate confidence — factors explain ${(a.r_squared * 100).toFixed(0)}% of its moves.`
        : `Low confidence — ${sym} often moves for its own reasons (${(a.r_squared * 100).toFixed(0)}% explained).`;
      return `<div class="fcard">
        <div class="head"><span class="s">${sym}</span><span class="n">${name(sym)}</span>
          <span class="edge" style="color:${edgeColor}">${a.alpha_annual >= 0 ? "+" : ""}${(a.alpha_annual * 100).toFixed(1)}%/yr edge</span></div>
        <p>${name(sym)} ${marketDesc} — when the market moves 1%, it tends to move ${b.toFixed(1)}%. And ${alphaDesc}.</p>
        ${bars}<div class="conf">${conf}</div></div>`;
    }).join("");
    setStatus("FACTOR ANALYSIS COMPLETE");
  } catch (e) { setStatus("ERR: " + e.message); }
};

// ---------- backtest wizard ----------
const STRAT_META = {
  buy_and_hold: { name: "Buy & hold", desc: "Buy once and keep it. The simplest baseline — everything else has to beat this.", params: [] },
  sma_crossover: { name: "Trend following", desc: "Buy when the price starts climbing steadily, step out when the trend turns down.",
    params: [["fast", "Fast average (days)", "20", "Short-term trend — reacts quickly"], ["slow", "Slow average (days)", "50", "Long-term trend — smooths noise"]] },
  momentum: { name: "Momentum", desc: "Each month, own whatever has performed best recently and drop the laggards.",
    params: [["lookback", "Look back (days)", "126", "How far back to measure performance"]] },
  mean_reversion: { name: "Dip buying", desc: "Buy when a stock falls unusually far below its normal level, sell once it bounces back.",
    params: [["window", "Window (days)", "20", "Period that defines “normal”"], ["z", "Drop size (z-score)", "2.0", "How unusual a dip must be to buy"]] },
  news_sentiment: { name: "News sentiment", desc: "Own a stock — or a sector ETF like XLE — only in weeks when its news reads positive. First run is slow — it rebuilds months of headlines from the archive.",
    params: [["threshold", "Bullish threshold", "0.05", "Weekly mood needed to stay invested (−1 to +1)"], ["smooth", "Smoothing (weeks)", "2", "Average of the last N weekly scores"]] },
};
let btStrategy = "momentum";
let btPicked = new Set(["AAPL", "MSFT", "NVDA", "SPY"]);
let btPeriod = "3y";
let btEngine = "vectorized";

async function loadStrategies() {
  const d = await api("/api/backtest/strategies");
  $("bt-strategies").innerHTML = d.strategies.map((id) => {
    const m = STRAT_META[id] || { name: id, desc: "", params: [] };
    return `<div class="scard ${id === btStrategy ? "sel" : ""}" data-id="${id}">
      <div class="t">${m.name}</div><div class="d">${m.desc}</div></div>`;
  }).join("");
  document.querySelectorAll(".scard").forEach((c) => {
    c.onclick = () => {
      btStrategy = c.dataset.id;
      document.querySelectorAll(".scard").forEach((x) => x.classList.toggle("sel", x === c));
      renderParams();
    };
  });
  renderSymChips();
  renderParams();
}
function renderSymChips() {
  const all = [...new Set([...TAPE_SYMS.split(","), ...btPicked])];
  $("bt-symbols").innerHTML = all.map((s) =>
    `<button class="chip ${btPicked.has(s) ? "active" : ""}" data-sym="${s}">${s}</button>`).join("");
  document.querySelectorAll("#bt-symbols .chip").forEach((c) => {
    c.onclick = () => {
      const s = c.dataset.sym;
      btPicked.has(s) ? btPicked.delete(s) : btPicked.add(s);
      c.classList.toggle("active", btPicked.has(s));
    };
  });
}
$("bt-addsym").onkeydown = (ev) => {
  if (ev.key !== "Enter" || ev.defaultPrevented) return;
  const s = $("bt-addsym").value.trim().toUpperCase();
  if (!s) return;
  btPicked.add(s);
  $("bt-addsym").value = "";
  renderSymChips();
};
function renderParams() {
  const m = STRAT_META[btStrategy] || { params: [] };
  $("bt-params").innerHTML = m.params.length
    ? m.params.map(([key, label, def, hint]) =>
        `<label>${label}<input class="mono" data-param="${key}" value="${def}" /><span class="hintline">${hint}</span></label>`).join("")
    : `<span>Nothing to tune — this rule has no settings.</span>`;
}
document.querySelectorAll("[data-period]").forEach((c) => {
  c.onclick = () => {
    document.querySelectorAll("[data-period]").forEach((x) => x.classList.remove("active"));
    c.classList.add("active");
    btPeriod = c.dataset.period;
  };
});
document.querySelectorAll("[data-engine]").forEach((c) => {
  c.onclick = () => {
    document.querySelectorAll("[data-engine]").forEach((x) => x.classList.remove("active"));
    c.classList.add("active");
    btEngine = c.dataset.engine;
    $("bt-engine-hint").textContent = btEngine === "event_driven"
      ? "Simulates every single trade — cash, latency, order fills."
      : "Quick math over the whole history at once.";
  };
});
$("bt-adv-toggle").onclick = () => {
  const p = $("bt-advanced");
  const open = p.style.display !== "none";
  p.style.display = open ? "none" : "block";
  $("bt-adv-toggle").textContent = open
    ? "Advanced settings (engine, costs, capital, benchmark, end date) ▸"
    : "Hide advanced settings";
};

$("bt-run").onclick = async () => {
  setStatus("RUNNING BACKTEST…");
  $("bt-run").textContent = "Running…";
  try {
    const params = {};
    // Scoped to the wizard's own inputs — the Data tab reuses data-param.
    document.querySelectorAll("#bt-params [data-param]").forEach((i) => { params[i.dataset.param] = parseFloat(i.value); });
    const extraRaw = $("bt-extra").value.trim();
    if (extraRaw) {
      try { Object.assign(params, JSON.parse(extraRaw)); }
      catch { throw new Error("Extra parameters must be valid JSON"); }
    }
    const years = { "1y": 366, "3y": 1096, "5y": 1827 }[btPeriod];
    const cap = parseFloat($("bt-cap").value) || 100000;
    const d = await api("/api/backtest/run", {
      method: "POST",
      body: {
        strategy: btStrategy,
        engine: btEngine,
        symbols: [...btPicked],
        benchmark: $("bt-bench").value.trim().toUpperCase() || null,
        start: isoAgo(years),
        end: $("bt-end").value.trim() || null,
        commission_bps: parseFloat($("bt-comm").value),
        slippage_bps: parseFloat($("bt-slip").value),
        initial_capital: cap,
        params,
      },
    });
    const m = d.metrics;
    const finalVal = d.equity_curve.values[d.equity_curve.values.length - 1];
    const stName = (STRAT_META[btStrategy] || {}).name || btStrategy;
    const periodLabel = { "1y": "1 year", "3y": "3 years", "5y": "5 years" }[btPeriod];
    $("bt-headline").textContent = `${stName} would have turned ${fmt$(cap)} into ${fmt$(finalVal)} — ` +
      `${m.total_return >= 0 ? "a gain of" : "a loss of"} ${Math.abs(m.total_return * 100).toFixed(1)}% over ${periodLabel}.`;
    let compare = "";
    if (d.benchmark) {
      const bTr = d.benchmark.values[d.benchmark.values.length - 1] / d.benchmark.values[0] - 1;
      compare = `Just holding the market (${d.benchmark.symbol}) would have returned ${fmtPct(bTr, true)} over the same period. ` +
        (m.total_return > bTr ? "Your strategy came out ahead." : "Simply holding the market did better.");
    }
    $("bt-compare").textContent = compare;
    renderMetrics("bt-metrics", m, d);
    const datasets = [{ label: "Your strategy", data: d.equity_curve.values, color: "#00c805" }];
    if (d.benchmark) datasets.push({ label: d.benchmark.symbol, data: d.benchmark.values, color: "#6f7377", dash: true });
    drawLine("bt-chart", d.equity_curve.dates, datasets);
    $("bt-result").style.display = "block";
    setStatus(`DONE · #${d.run_id} · ${d.engine.toUpperCase()} · ${d.num_trades} TRADES`);
  } catch (e) { setStatus("ERR: " + e.message); }
  $("bt-run").textContent = "Run the test";
};

function renderMetrics(elId, m, d) {
  const sharpeNote = m.sharpe >= 1.5 ? "Excellent — well paid for the risk taken."
    : m.sharpe >= 1 ? "Good — solid return for the risk taken."
    : m.sharpe >= 0.5 ? "Okay — some reward for the risk."
    : "Weak — the risk was not well rewarded.";
  const cards = [
    ["Total return", fmtPct(m.total_return, true), cls(m.total_return), "Overall growth of your money."],
    ["Per year", fmtPct(m.cagr, true), cls(m.cagr), "Average yearly growth rate (CAGR)."],
    ["Risk-adjusted", m.sharpe.toFixed(2), m.sharpe >= 1 ? "pos" : m.sharpe < 0.3 ? "neg" : "", sharpeNote],
    ["Worst dip", fmtPct(m.max_drawdown), "neg", `At its lowest you would have been down ${Math.abs(m.max_drawdown * 100).toFixed(0)}% from a peak.`],
    ["Choppiness", fmtPct(m.annual_volatility), "", m.annual_volatility > 0.3 ? "A bumpy ride — expect big daily swings." : "How much the value swings day to day."],
    ["Up days", fmtPct(m.win_rate), "", "Share of days the strategy made money."],
  ];
  if (d && d.total_costs != null) cards.push(["Costs paid", fmt$(d.total_costs), "", "Total commissions and slippage."]);
  $(elId).innerHTML = cards.map(([k, v, c, note]) =>
    `<div class="metric"><div class="k">${k}</div><div class="v ${c}">${v}</div><div class="note">${note}</div></div>`).join("");
}

// ---------- saved (account, commands, watchlists, alerts, activity) ----------
async function loadSavedView() {
  await Promise.all([
    loadAccountStats(), loadSavedCommands(), loadSavedResults(), loadWatchlists(),
    loadAlerts(), loadActivity(),
  ]);
}

// ---------- saved results (data snapshots) ----------
let openResult = null; // the full payload of the result being viewed

async function loadSavedResults() {
  try {
    const rows = await api("/api/user/results");
    if (!rows.length) {
      $("sv-results").innerHTML =
        `<div class="empty">No saved results yet — run a command in the Data tab and press 💾 Save results.</div>`;
      return;
    }
    $("sv-results").innerHTML = `<table><tr><th>Name</th><th>Command</th><th>Parameters</th>
      <th>Rows</th><th>Provider</th><th>Saved</th><th></th></tr>` + rows.map((r) => `<tr>
        <td>${escapeHtml(r.name)}${r.truncated ? ' <span class="wl-badge" title="stored first 5000 rows">truncated</span>' : ""}</td>
        <td>${r.command_path}</td>
        <td>${escapeHtml(Object.entries(r.parameters || {}).map(([k, v]) => `${k}=${v}`).join(", ")) || "-"}</td>
        <td>${r.row_count}</td><td>${r.provider || "-"}</td><td>${fmtTime(r.created_at)}</td>
        <td><button class="linkbtn" data-view="${r.id}">View</button>
            <button class="linkbtn danger" data-delres="${r.id}">Delete</button></td>
      </tr>`).join("") + "</table>";

    $("sv-results").querySelectorAll("[data-view]").forEach((el) => {
      el.onclick = async () => {
        setStatus("LOADING SAVED RESULT…");
        try {
          openResult = await api(`/api/user/results/${el.dataset.view}`);
          $("sv-results-title").textContent =
            `${openResult.name} · ${openResult.command_path} · saved ${fmtTime(openResult.created_at)}`;
          $("sv-results-table").innerHTML = simpleTable(openResult.results, 250) +
            (openResult.results.length > 250
              ? `<div class="hint">showing 250 of ${openResult.results.length} rows — download the CSV for all</div>` : "");
          $("sv-results-view").style.display = "block";
          setStatus(`OPENED "${openResult.name}"`);
        } catch (e) { setStatus("ERR: " + e.message); }
      };
    });
    $("sv-results").querySelectorAll("[data-delres]").forEach((el) => {
      el.onclick = async () => {
        await api(`/api/user/results/${el.dataset.delres}`, { method: "DELETE" });
        $("sv-results-view").style.display = "none";
        loadSavedResults(); loadAccountStats();
      };
    });
  } catch (e) { $("sv-results").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`; }
}
$("sv-results-close").onclick = () => { $("sv-results-view").style.display = "none"; };
$("sv-results-csv").onclick = () => {
  if (!openResult || !openResult.results.length) return;
  const rows = openResult.results;
  const cols = [...new Set(rows.flatMap((r) => Object.keys(r)))];
  const esc = (v) => {
    const s = v === null || v === undefined ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const csv = [cols.join(","), ...rows.map((r) => cols.map((c) => esc(r[c])).join(","))].join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = openResult.name.replace(/[^\w-]+/g, "_") + ".csv";
  a.click();
  URL.revokeObjectURL(a.href);
};

async function loadAccountStats() {
  try {
    const s = await api("/api/user/stats");
    $("sv-stats").innerHTML = [
      ["Saved commands", s.saved_commands], ["Result sets", s.saved_results ?? 0],
      ["Watchlists", s.watchlists], ["Alerts", s.alerts],
      ["Backtests", s.backtests], ["Commands run", s.command_runs],
      ["Failed", s.failed_runs], ["Logins", s.login_count],
    ].map(([k, v]) => `<div class="metric"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
    const me = await api("/api/auth/me");
    $("sv-fullname").value = me.full_name || "";
  } catch (e) { setStatus("ERR: " + e.message); }
}
$("sv-save-profile").onclick = async () => {
  try {
    await api("/api/auth/me", { method: "PATCH", body: { full_name: $("sv-fullname").value.trim() } });
    setStatus("PROFILE UPDATED");
  } catch (e) { setStatus("ERR: " + e.message); }
};
$("sv-sessions-toggle").onclick = async () => {
  const box = $("sv-sessions");
  if (box.style.display === "block") { box.style.display = "none"; return; }
  const rows = await api("/api/auth/sessions");
  box.innerHTML = `<table><tr><th>Signed in</th><th>Last seen</th><th>Client</th>
    <th>Status</th><th></th></tr>` + rows.map((s) => `<tr>
      <td>${fmtTime(s.issued_at)}</td><td>${fmtTime(s.last_seen_at)}</td>
      <td>${escapeHtml((s.user_agent || "-").slice(0, 42))}</td>
      <td class="${s.is_active ? "pos" : "neg"}">${s.is_active ? "active" : "revoked"}</td>
      <td>${s.is_active ? `<button class="linkbtn danger" data-revoke="${s.id}">Revoke</button>` : ""}</td>
    </tr>`).join("") + "</table>";
  box.style.display = "block";
  box.querySelectorAll("[data-revoke]").forEach((el) => {
    el.onclick = async () => {
      await api(`/api/auth/sessions/${el.dataset.revoke}`, { method: "DELETE" });
      $("sv-sessions").style.display = "none";
      $("sv-sessions-toggle").click();
    };
  });
};

async function loadSavedCommands() {
  const rows = await api("/api/user/saved");
  if (!rows.length) { $("sv-saved").innerHTML = `<div class="empty">Nothing saved yet — run something in the Data tab and press ★ Save.</div>`; return; }
  $("sv-saved").innerHTML = `<table><tr><th>Name</th><th>Command</th><th>Parameters</th>
    <th>Runs</th><th>Last run</th><th></th></tr>` + rows.map((s) => `<tr>
      <td>${s.is_favorite ? "★ " : ""}${escapeHtml(s.name)}</td>
      <td>${s.command_path}</td>
      <td>${escapeHtml(Object.entries(s.parameters || {}).map(([k, v]) => `${k}=${v}`).join(", ")) || "-"}</td>
      <td>${s.run_count}</td><td>${fmtTime(s.last_run_at)}</td>
      <td><button class="linkbtn" data-run="${s.id}">Run</button>
          <button class="linkbtn danger" data-del="${s.id}">Delete</button></td>
    </tr>`).join("") + "</table>";
  $("sv-saved").querySelectorAll("[data-run]").forEach((el) => {
    el.onclick = async () => {
      setStatus("RUNNING SAVED COMMAND…");
      try {
        const d = await api(`/api/user/saved/${el.dataset.run}/run`, { method: "POST" });
        const n = Array.isArray(d.results) ? d.results.length : 1;
        setStatus(`${d.saved_command.name}: ${n} ROW(S) VIA ${(d.provider || "-").toUpperCase()}`);
        loadSavedCommands();
      } catch (e) { setStatus("ERR: " + e.message); }
    };
  });
  $("sv-saved").querySelectorAll("[data-del]").forEach((el) => {
    el.onclick = async () => {
      await api(`/api/user/saved/${el.dataset.del}`, { method: "DELETE" });
      loadSavedCommands();
    };
  });
}

async function loadWatchlists() {
  const lists = await api("/api/user/watchlists");
  if (!lists.length) { $("sv-watchlists").innerHTML = `<div class="empty">No watchlists yet.</div>`; return; }
  $("sv-watchlists").innerHTML = lists.map((w) => `
    <div class="wl-card">
      <div class="wl-head">
        <span class="wl-title">${escapeHtml(w.name)}</span>
        ${w.is_default ? `<span class="wl-badge">default</span>` : ""}
        <span class="wl-badge">${w.items.length} symbols</span>
        <span class="spacer"></span>
        <button class="linkbtn" data-quotes="${w.id}">Quotes</button>
        <button class="linkbtn danger" data-delwl="${w.id}">Delete</button>
      </div>
      <div>${w.items.map((i) => `<span class="symchip">${i.symbol}
        <button data-rm="${w.id}|${i.symbol}" title="remove">&times;</button></span>`).join("")
        || `<span class="empty">empty</span>`}</div>
      <div class="row" style="margin:8px 0 0">
        <input data-add="${w.id}" class="mono" placeholder="add symbol" style="max-width:160px;text-transform:uppercase" />
      </div>
      <div id="wl-quotes-${w.id}" class="tablewrap"></div>
    </div>`).join("");

  const wl = $("sv-watchlists");
  wl.querySelectorAll("[data-delwl]").forEach((el) => {
    el.onclick = async () => {
      await api(`/api/user/watchlists/${el.dataset.delwl}`, { method: "DELETE" });
      loadWatchlists(); loadAccountStats();
    };
  });
  wl.querySelectorAll("[data-rm]").forEach((el) => {
    el.onclick = async () => {
      const [id, symbol] = el.dataset.rm.split("|");
      await api(`/api/user/watchlists/${id}/items/${symbol}`, { method: "DELETE" });
      loadWatchlists();
    };
  });
  wl.querySelectorAll("[data-add]").forEach((el) => {
    attachAutocomplete(el, false); // bound first so a dropdown Enter fills, not submits
    el.onkeydown = async (ev) => {
      if (ev.key !== "Enter" || ev.defaultPrevented || !el.value.trim()) return;
      try {
        await api(`/api/user/watchlists/${el.dataset.add}/items`,
          { method: "POST", body: { symbol: el.value.trim() } });
        loadWatchlists();
      } catch (e) { setStatus("ERR: " + e.message); }
    };
  });
  wl.querySelectorAll("[data-quotes]").forEach((el) => {
    el.onclick = async () => {
      const id = el.dataset.quotes;
      setStatus("LOADING QUOTES…");
      try {
        const d = await api(`/api/user/watchlists/${id}/quotes`);
        $(`wl-quotes-${id}`).innerHTML = d.results.length
          ? `<table><tr><th>Symbol</th><th>Name</th><th>Last</th><th>Change</th><th>Note</th></tr>` +
            d.results.map((q) => `<tr><td><button class="linkbtn" data-open="${q.symbol}">${q.symbol}</button></td><td>${escapeHtml((q.name || "-").slice(0, 28))}</td>
              <td>${q.last_price?.toFixed(2) ?? "-"}</td>
              <td class="${cls(q.change_percent ?? 0)}">${q.change_percent != null ? fmtPct(q.change_percent, true) : "-"}</td>
              <td>${escapeHtml(q.note || "")}</td></tr>`).join("") + "</table>"
          : `<div class="empty">Watchlist is empty.</div>`;
        $(`wl-quotes-${id}`).querySelectorAll("[data-open]").forEach((btn) => {
          btn.onclick = () => openStock(btn.dataset.open);
        });
        setStatus("QUOTES UPDATED");
      } catch (e) { setStatus("ERR: " + e.message); }
    };
  });
}
$("wl-create").onclick = async () => {
  const nm = $("wl-name").value.trim();
  if (!nm) { setStatus("WATCHLIST NEEDS A NAME"); return; }
  const symbols = $("wl-symbols").value.split(",").map((s) => s.trim()).filter(Boolean);
  try {
    await api("/api/user/watchlists", { method: "POST", body: { name: nm, symbols } });
    $("wl-name").value = ""; $("wl-symbols").value = "";
    loadWatchlists(); loadAccountStats();
  } catch (e) { setStatus("ERR: " + e.message); }
};

async function loadAlerts(evaluation) {
  const rows = await api("/api/user/alerts");
  const seen = {};
  (evaluation?.results || []).forEach((r) => { seen[r.id] = r; });
  if (!rows.length) { $("sv-alerts").innerHTML = `<div class="empty">No alerts set.</div>`; return; }
  $("sv-alerts").innerHTML = `<table><tr><th>Symbol</th><th>Condition</th><th>Threshold</th>
    <th>Last value</th><th>State</th><th>Triggers</th><th></th></tr>` + rows.map((a) => {
      const live = seen[a.id];
      const state = live ? (live.triggered ? "TRIGGERED" : "ok") : (a.is_active ? "armed" : "paused");
      return `<tr><td>${a.symbol}</td><td>${a.condition.replace(/_/g, " ")}</td>
        <td>${a.threshold}</td><td>${a.last_value?.toFixed(2) ?? "-"}</td>
        <td class="${live?.triggered ? "trig" : "notrig"}">${state}</td>
        <td>${a.trigger_count}</td>
        <td><button class="linkbtn" data-toggle="${a.id}|${a.is_active}">${a.is_active ? "Pause" : "Arm"}</button>
            <button class="linkbtn danger" data-delal="${a.id}">Delete</button></td></tr>`;
    }).join("") + "</table>";
  $("sv-alerts").querySelectorAll("[data-delal]").forEach((el) => {
    el.onclick = async () => {
      await api(`/api/user/alerts/${el.dataset.delal}`, { method: "DELETE" });
      loadAlerts(); loadAccountStats();
    };
  });
  $("sv-alerts").querySelectorAll("[data-toggle]").forEach((el) => {
    el.onclick = async () => {
      const [id, active] = el.dataset.toggle.split("|");
      await api(`/api/user/alerts/${id}`, { method: "PATCH", body: { is_active: active !== "true" } });
      loadAlerts();
    };
  });
}
$("al-create").onclick = async () => {
  const symbol = $("al-symbol").value.trim();
  const threshold = parseFloat($("al-threshold").value);
  if (!symbol || isNaN(threshold)) { setStatus("ALERT NEEDS A SYMBOL AND A NUMERIC THRESHOLD"); return; }
  try {
    await api("/api/user/alerts", {
      method: "POST",
      body: { symbol, condition: $("al-condition").value, threshold },
    });
    $("al-symbol").value = ""; $("al-threshold").value = "";
    loadAlerts(); loadAccountStats();
  } catch (e) { setStatus("ERR: " + e.message); }
};
$("al-evaluate").onclick = async () => {
  setStatus("CHECKING ALERTS…");
  try {
    const d = await api("/api/user/alerts/evaluate", { method: "POST" });
    await loadAlerts(d);
    setStatus(`${d.triggered.length} OF ${d.checked} ALERT(S) TRIGGERED`);
  } catch (e) { setStatus("ERR: " + e.message); }
};

async function loadActivity() {
  const rows = await api("/api/user/history?limit=25");
  if (!rows.length) { $("sv-activity").innerHTML = `<div class="empty">No commands run yet.</div>`; return; }
  $("sv-activity").innerHTML = `<table><tr><th>When</th><th>Command</th><th>Parameters</th>
    <th>Provider</th><th>Rows</th><th>ms</th><th>Status</th></tr>` + rows.map((r) => `<tr>
      <td>${fmtTime(r.created_at)}</td><td>${r.command_path}</td>
      <td>${escapeHtml(Object.entries(r.parameters || {}).map(([k, v]) => `${k}=${v}`).join(", ")) || "-"}</td>
      <td>${r.provider || "-"}</td><td>${r.row_count ?? "-"}</td><td>${r.duration_ms ?? "-"}</td>
      <td class="${r.status === "ok" ? "pos" : "neg"}" title="${escapeHtml((r.error || ""))}">${r.status}</td>
    </tr>`).join("") + "</table>";
}
$("sv-clear-history").onclick = async () => {
  const d = await api("/api/user/history", { method: "DELETE" });
  setStatus(`CLEARED ${d.deleted} HISTORY ROW(S)`);
  loadActivity(); loadAccountStats();
};
function fmtTime(value) {
  if (!value) return "-";
  const d = new Date(value);
  return isNaN(d) ? "-" : d.toLocaleString(undefined,
    { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

// ---------- history ----------
async function loadHistory() {
  try {
    const d = await api("/api/backtest/history");
    if (!d.runs.length) { $("hi-out").innerHTML = `<p class="dim">No saved tests yet — run one from the "Test a strategy" tab.</p>`; return; }
    $("hi-out").innerHTML = d.runs.map((r) => {
      const stName = (STRAT_META[r.strategy] || {}).name || r.strategy;
      const ret = r.total_return ?? 0;
      return `<div class="run">
        <div class="body"><div class="t">${stName} · ${r.symbols}</div>
          <div class="s">${r.start} to ${r.end || "today"}</div></div>
        <div><div class="ret ${ret >= 0 ? "up" : "down"}">${fmtPct(ret, true)}</div>
          <div class="sh">Risk-adjusted ${(r.sharpe ?? 0).toFixed(2)}</div></div>
        <a href="/api/reports/${r.id}" onclick="return openReport(${r.id})">Full report →</a>
      </div>`;
    }).join("");
  } catch (e) { setStatus("ERR: " + e.message); }
}
window.openReport = (id) => {
  fetch(`/api/reports/${id}`, { headers: { Authorization: `Bearer ${token}` } })
    .then((r) => r.text())
    .then((html) => { const w = window.open(); w.document.write(html); w.document.close(); })
    .catch((e) => setStatus("ERR: " + e.message));
  return false;
};

// ---------- system ----------
async function loadSystem() {
  try {
    const [health, cache] = await Promise.all([api("/api/system/health"), api("/api/system/cache")]);
    const row = (k, v, color) => `<div class="r"><span class="k">${k}</span><span class="v" style="${color ? "color:" + color : ""}">${v}</span></div>`;
    $("sy-health").innerHTML = Object.entries(health).map(([k, v]) =>
      row(k.replace(/_/g, " "), String(v).toUpperCase(), String(v).match(/ok|healthy|true|up/i) ? "var(--green)" : "")).join("");
    const hits = cache.hits ?? 0, misses = cache.misses ?? 0;
    $("sy-cache").innerHTML = [
      row("Answered from memory (hits)", hits),
      row("Fetched fresh (misses)", misses),
      row("Hit rate", hits + misses ? ((hits / (hits + misses)) * 100).toFixed(0) + "%" : "—"),
      ...Object.entries(cache).filter(([k]) => !["hits", "misses"].includes(k)).map(([k, v]) => row(k.replace(/_/g, " "), v)),
    ].join("");
    try {
      const db = await api("/api/system/database");
      $("sy-db").innerHTML = row("dialect", db.dialect) +
        db.tables.map((t) => row(t.table, `${t.rows ?? "-"} rows · ${t.columns.length} cols`)).join("");
    } catch { $("sy-db").innerHTML = `<div class="empty">unavailable</div>`; }
    setStatus("SYSTEM STATUS LOADED");
  } catch (e) { setStatus("ERR: " + e.message); }
}
$("sy-clear").onclick = async () => {
  try { await api("/api/system/cache/clear", { method: "POST" }); await loadSystem(); setStatus("CACHE CLEARED"); }
  catch (e) { setStatus("ERR: " + e.message); }
};

// ---------- ticker autocomplete ----------
// One shared dropdown serves every symbol input, including ones created later
// (Data-tab parameter forms, watchlist add boxes). Suggestions come from
// /api/data/suggest — a local directory, fast enough to hit per keystroke.
const acMenu = document.createElement("div");
acMenu.id = "ac-menu";
document.body.appendChild(acMenu);
let acState = { input: null, items: [], sel: -1, multi: false };
let acTimer = null, acSeq = 0;

function acHide() { acMenu.style.display = "none"; acState = { input: null, items: [], sel: -1, multi: false }; }
document.addEventListener("scroll", acHide, true);
window.addEventListener("resize", acHide);

function acToken(el, multi) {
  if (!multi) return el.value.trim();
  const parts = el.value.split(",");
  return parts[parts.length - 1].trim();
}
function acApply(item) {
  const el = acState.input;
  if (!el) return;
  if (acState.multi) {
    const parts = el.value.split(",");
    parts[parts.length - 1] = (parts.length > 1 ? " " : "") + item.symbol;
    el.value = parts.join(",");
  } else {
    el.value = item.symbol;
  }
  acHide();
  el.focus();
}
function acRender() {
  const el = acState.input;
  if (!el || !acState.items.length) { acHide(); return; }
  const r = el.getBoundingClientRect();
  acMenu.style.left = r.left + "px";
  acMenu.style.top = (r.bottom + 3) + "px";
  acMenu.style.minWidth = Math.max(r.width, 260) + "px";
  acMenu.innerHTML = acState.items.map((it, i) =>
    `<div class="ac-item ${i === acState.sel ? "sel" : ""}" data-i="${i}">
      <span class="ac-sym">${it.symbol}</span>
      <span class="ac-name">${escapeHtml(it.name || "")}</span>
      <span class="ac-type">${it.type || ""}</span>
    </div>`).join("");
  acMenu.style.display = "block";
  acMenu.querySelectorAll(".ac-item").forEach((node) => {
    node.onmousedown = (ev) => { ev.preventDefault(); acApply(acState.items[+node.dataset.i]); };
  });
}
async function acQuery(el, multi) {
  const tok = acToken(el, multi);
  if (!tok) { acHide(); return; }
  const seq = ++acSeq;
  try {
    const d = await api(`/api/data/suggest?q=${encodeURIComponent(tok)}&limit=9`);
    if (seq !== acSeq || document.activeElement !== el) return;
    acState = { input: el, items: d.results, sel: -1, multi };
    acRender();
  } catch { acHide(); }
}
function attachAutocomplete(el, multi) {
  if (!el || el.dataset.acBound) return;
  el.dataset.acBound = "1";
  el.setAttribute("autocomplete", "off");
  el.addEventListener("input", () => {
    clearTimeout(acTimer);
    acTimer = setTimeout(() => acQuery(el, multi), 150);
  });
  el.addEventListener("keydown", (ev) => {
    if (acMenu.style.display !== "block" || acState.input !== el) return;
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      acState.sel = Math.min(acState.sel + 1, acState.items.length - 1);
      acRender();
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      acState.sel = Math.max(acState.sel - 1, 0);
      acRender();
    } else if ((ev.key === "Enter" || ev.key === "Tab") && acState.sel >= 0) {
      ev.preventDefault();
      acApply(acState.items[acState.sel]);
    } else if (ev.key === "Enter" || ev.key === "Escape") {
      acHide(); // fall through so Enter can still submit the field
    }
  });
  el.addEventListener("blur", () => setTimeout(acHide, 120));
}

// Static symbol inputs, bound once at boot.
[["mk-symbol", false], ["wm-add", false], ["cmp-symbols", true], ["fa-symbols", true],
 ["bt-bench", false], ["bt-addsym", false], ["wl-symbols", true], ["al-symbol", false],
 ["nw-symbol", false], ["se-symbols", true], ["sh-symbol", false], ["vl-symbol", false]]
  .forEach(([id, multi]) => attachAutocomplete($(id), multi));

// Default the comparison/sector/asset chart windows to year-to-date.
const jan1 = new Date().getFullYear() + "-01-01";
["cmp-start", "sc-start", "as-start"].forEach((id) => { if ($(id)) $(id).value = jan1; });

// ---------- chart: crosshair + measurement plugin ----------
//   hover        -> vertical line, readout shows change from that point to the latest
//   click        -> pins an anchor line; hovering then measures anchor -> hovered point
//   click again  -> clears the anchor
const measurePlugin = {
  id: "mftMeasure",
  afterEvent(chart, args) {
    const st = chart.$measure;
    if (!st) return;
    const e = args.event;
    const hit = () => {
      const els = chart.getElementsAtEventForMode(e.native, "index", { intersect: false }, true);
      return els.length ? els[0].index : null;
    };
    if (e.type === "mousemove") {
      st.hover = hit();
      args.changed = true;
    } else if (e.type === "click") {
      st.anchor = st.anchor == null ? hit() : null;
      args.changed = true;
    } else if (e.type === "mouseout") {
      st.hover = null;
      args.changed = true;
    }
    renderMeasure(chart);
  },
  afterDraw(chart) {
    const st = chart.$measure;
    if (!st) return;
    const { ctx, chartArea: { top, bottom } } = chart;
    const xFor = (i) => chart.scales.x.getPixelForValue(i);
    if (st.anchor != null && st.hover != null && st.hover !== st.anchor) {
      const a = xFor(Math.min(st.anchor, st.hover));
      const b = xFor(Math.max(st.anchor, st.hover));
      ctx.save(); ctx.fillStyle = "rgba(0,200,5,0.06)";
      ctx.fillRect(a, top, b - a, bottom - top); ctx.restore();
    }
    for (const [idx, color, dash] of [[st.anchor, "#00c805", []], [st.hover, "#8c8f90", [4, 3]]]) {
      if (idx == null) continue;
      ctx.save(); ctx.beginPath();
      ctx.strokeStyle = color; ctx.setLineDash(dash); ctx.lineWidth = 1;
      ctx.moveTo(xFor(idx), top); ctx.lineTo(xFor(idx), bottom);
      ctx.stroke(); ctx.restore();
    }
  },
};

function fmtVal(v) {
  if (v == null) return "-";
  const a = Math.abs(v);
  return a >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 })
       : a >= 10 ? v.toFixed(2) : v.toFixed(3);
}
function lastIndex(data) {
  for (let i = data.length - 1; i >= 0; i--) if (data[i] != null) return i;
  return null;
}
function renderMeasure(chart) {
  const st = chart.$measure;
  if (!st || !st.box) return;
  const labels = chart.data.labels || [];
  const hint = st.anchor == null
    ? "hover: change vs latest · click to pin a start point"
    : "pinned — hover to measure from the pin · click to clear";
  const start = st.anchor != null ? st.anchor : st.hover;
  if (start == null) { st.box.innerHTML = `<span>${hint}</span>`; return; }
  const parts = chart.data.datasets.map((ds) => {
    let i1 = start;
    let i2 = (st.anchor != null && st.hover != null) ? st.hover : lastIndex(ds.data);
    if (i2 == null) return "";
    if (i1 > i2) [i1, i2] = [i2, i1];
    const a = ds.data[i1], b = ds.data[i2];
    if (a == null || b == null) return "";
    const diff = b - a;
    const pct = a ? ` (${diff >= 0 ? "+" : ""}${(diff / Math.abs(a) * 100).toFixed(2)}%)` : "";
    return `<span class="m-series"><span class="m-dot" style="background:${ds.borderColor}"></span>
      ${escapeHtml(ds.label)} ${fmtVal(a)} → ${fmtVal(b)}
      <b class="${diff >= 0 ? "pos" : "neg"}">${diff >= 0 ? "+" : ""}${fmtVal(diff)}${pct}</b></span>`;
  }).filter(Boolean);
  const endIdx = (st.anchor != null && st.hover != null) ? Math.max(start, st.hover) : null;
  const startIdx = endIdx == null ? start : Math.min(start, st.hover);
  const header = `<span class="m-hdr">${labels[startIdx]} → ${endIdx == null ? "latest" : labels[endIdx]}</span>`;
  st.box.innerHTML = parts.length ? header + parts.join("") : `<span>${hint}</span>`;
}

// opts.fitBox: size the chart to the box it sits in rather than to the canvas
// aspect ratio. Charts in the markup carry a height="..." attribute and scale
// from it; the workspace builds its canvases from script inside a fixed-height
// box, so without this the chart is drawn narrow and stretched across the box.
function drawLine(canvasId, labels, datasets, opts = {}) {
  if (charts[canvasId]) charts[canvasId].destroy();
  // A dataset with y2:true plots against a second axis on the right — used
  // where two series live on incompatible scales (sentiment vs price).
  const hasY2 = datasets.some((ds) => ds.y2);
  const axisTicks = { color: "#6f7377", font: { family: "'IBM Plex Mono', monospace", size: 10 } };
  // A widget box is short and often narrow: keep labels flat and let Chart.js
  // drop the ones that do not fit instead of turning them on their side.
  const xTicks = opts.fitBox
    ? { ...axisTicks, maxTicksLimit: 5, maxRotation: 0, autoSkipPadding: 10 }
    : { ...axisTicks, maxTicksLimit: 8 };
  const chart = new Chart($(canvasId), {
    type: "line",
    data: {
      labels,
      datasets: datasets.map((ds) => ({
        label: ds.label, data: ds.data, borderColor: ds.color,
        borderWidth: 1.8, pointRadius: 0, spanGaps: true,
        fill: ds.fill || false, backgroundColor: ds.color + "14",
        borderDash: ds.dash ? [5, 4] : [],
        yAxisID: ds.y2 ? "y2" : "y",
      })),
    },
    options: {
      animation: false,
      ...(opts.fitBox ? { maintainAspectRatio: false } : {}),
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: datasets.length > 1, labels: { color: "#f5f6f7", boxWidth: 14 } } },
      scales: {
        x: { ticks: xTicks, grid: { color: "#191b1c" } },
        y: { ticks: axisTicks, grid: { color: "#191b1c" } },
        ...(hasY2 ? { y2: { position: "right", ticks: axisTicks, grid: { drawOnChartArea: false } } } : {}),
      },
    },
    plugins: [measurePlugin],
  });
  charts[canvasId] = chart;
  let box = document.getElementById(canvasId + "-measure");
  if (!box) {
    box = document.createElement("div");
    box.id = canvasId + "-measure";
    box.className = "measure-box";
    $(canvasId).insertAdjacentElement("afterend", box);
  }
  chart.$measure = { hover: null, anchor: null, box };
  renderMeasure(chart);
}

// ---------- volatility ----------
// Two halves. The top is the listed volatility complex — the fear gauges quote
// their own level in vol points, so they need no derivation. The bottom is one
// name at a time, where the honest question is what it has actually delivered
// (realised, out of daily closes) against what its options are charging
// (implied, out of the listed chain). Every panel fails on its own: a name with
// no listed options still shows its realised vol and cones.
const VOL_TILES = [
  ["^VIX", "S&P 500 · 30 day"], ["^VIX9D", "S&P 500 · 9 day"], ["^VIX3M", "S&P 500 · 3 month"],
  ["^VIX6M", "S&P 500 · 6 month"], ["^VXN", "Nasdaq 100"], ["^VXD", "Dow Jones"],
  ["^VVIX", "Volatility of the VIX"], ["^MOVE", "Treasuries"], ["^OVX", "Crude oil"],
  ["^GVZ", "Gold"], ["^EVZ", "Euro / dollar"],
];
// The four S&P tenors Cboe publishes as their own indices — a term structure
// without touching a single option contract.
const VOL_TERM = [["^VIX9D", "9-day"], ["^VIX", "30-day"], ["^VIX3M", "3-month"], ["^VIX6M", "6-month"]];
// Expiries to sample for a name's own term structure, in days out.
const VOL_TENORS = [7, 30, 60, 90, 180, 365];
const VOL_RV_WINDOWS = [21, 63, 252];
let volLoaded = false, volQuotes = {}, volHistDays = 1096, volHistStat = null;
let volSym = null, volRv = null, volAtmIv = null, volSeq = 0;

const volPts = (x) => (x == null || !isFinite(x) ? "—" : (x * 100).toFixed(1) + "%");
// 1st / 2nd / 3rd / 4th — the teens are the exception that catches everyone.
function volOrdinal(x) {
  if (x == null || !isFinite(x)) return "—";
  const n = Math.round(x), tens = n % 100, ones = n % 10;
  const suffix = tens >= 11 && tens <= 13 ? "th" : ones === 1 ? "st" : ones === 2 ? "nd" : ones === 3 ? "rd" : "th";
  return n + suffix;
}

function loadVolatility() {
  if (volLoaded) return;
  volLoaded = true;
  loadVolComplex();
  loadVolHistory();
  loadVolName();
}

async function loadVolComplex() {
  try {
    const syms = VOL_TILES.map(([s]) => s).join(",");
    const d = await api(`/api/v1/equity/price/quote?symbol=${encodeURIComponent(syms)}`);
    volQuotes = {};
    (d.results || []).forEach((r) => { volQuotes[r.symbol] = r; });
    $("vl-tiles").innerHTML = VOL_TILES.map(([sym, label]) => {
      const q = volQuotes[sym];
      if (!q || q.last_price == null) return "";
      const chg = q.change_percent;
      return `<div class="tile" data-sym="${sym}">
        <div class="t-sym">${escapeHtml(sym.replace("^", ""))}</div>
        <div class="t-name">${label}</div>
        <div class="t-px">${q.last_price.toFixed(2)}</div>
        <div class="t-chg ${cls(chg ?? 0)}">${chg == null ? "-" : (chg >= 0 ? "▲ " : "▼ ") + fmtPct(Math.abs(chg))}</div>
      </div>`;
    }).join("") || `<div class="empty">No quotes returned.</div>`;
    $("vl-asof").textContent = "as of " + new Date().toLocaleTimeString();
    document.querySelectorAll("#vl-tiles .tile").forEach((t) => {
      t.onclick = () => openStock(t.dataset.sym, "volatility");
    });
    renderVolTerm();
    renderVolSignals();
  } catch (e) {
    $("vl-tiles").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`;
    $("vl-signals").innerHTML = `<div class="empty">Fear gauges unavailable.</div>`;
  }
}

const volLevel = (sym) => {
  const q = volQuotes[sym];
  return q && q.last_price != null ? q.last_price : null;
};

function renderVolTerm() {
  const points = VOL_TERM.map(([sym, label]) => [label, volLevel(sym)]).filter(([, v]) => v != null);
  if (points.length < 2) {
    $("vl-term-read").textContent = "The Cboe tenor indices did not quote.";
    return;
  }
  drawLine("vl-term", points.map(([l]) => l),
    [{ label: "Implied vol", data: points.map(([, v]) => v), color: "#5ac8fa", fill: true }]);
  const front = volLevel("^VIX"), back = volLevel("^VIX3M");
  if (front == null || back == null) { $("vl-term-read").textContent = ""; return; }
  const ratio = back / front;
  $("vl-term-read").textContent = ratio >= 1.02
    ? `Three-month vol sits ${(ratio).toFixed(2)}× the 30-day. That upward slope — contango — is the ` +
      `resting state: nothing urgent is priced into the next month, and protection gets dearer the ` +
      `further out you buy it.`
    : ratio <= 0.98
      ? `Thirty-day vol is above the three-month, at ${(ratio).toFixed(2)}× — an inverted curve. ` +
        `The market is charging most for the nearest risk, which is how a stressed tape prices.`
      : `The curve is flat (${ratio.toFixed(2)}×): near-dated and three-month vol cost about the same, ` +
        `so no particular horizon is being singled out.`;
}

function renderVolSignals() {
  const vix = volLevel("^VIX"), nine = volLevel("^VIX9D"), three = volLevel("^VIX3M");
  const cards = [];
  if (vix != null) {
    cards.push({
      label: "S&P 500 · 30-day implied", value: vix.toFixed(2),
      tone: vix < 16 ? "pos" : vix > 25 ? "neg" : "warn",
      read: `Options are pricing a move of about ±${(vix / Math.sqrt(12)).toFixed(1)}% over the next month, ` +
        `or ±${(vix / Math.sqrt(252)).toFixed(1)}% on a typical day.`,
    });
  }
  if (vix != null && three != null) {
    const ratio = three / vix;
    cards.push({
      label: "Term structure", value: ratio.toFixed(2) + "×",
      tone: ratio >= 1.02 ? "pos" : ratio <= 0.98 ? "neg" : "warn",
      read: ratio >= 1.02 ? "Three-month over 30-day — calm shape."
        : ratio <= 0.98 ? "Inverted: near-term stress is being priced." : "Flat curve.",
    });
  }
  if (vix != null && nine != null) {
    cards.push({
      label: "Next two weeks", value: nine.toFixed(2),
      tone: nine < vix ? "pos" : "neg",
      read: nine < vix
        ? "The next nine sessions are priced quieter than the month as a whole."
        : "The next nine sessions carry more implied risk than the month — an event sits in that window.",
    });
  }
  if (volHistStat) {
    cards.push({
      label: "Where the VIX sits", value: volOrdinal(volHistStat.percentile),
      tone: volHistStat.percentile < 33 ? "pos" : volHistStat.percentile > 66 ? "neg" : "warn",
      read: `percentile of its own last ${(volHistDays / 365).toFixed(0)} years — it has closed ` +
        `lower than today on ${Math.round(volHistStat.percentile)}% of those sessions.`,
    });
  }
  $("vl-signals").innerHTML = cards.length ? cards.map((c) => `
    <div class="sig-card t-${c.tone}">
      <div class="sig-label">${escapeHtml(c.label)}</div>
      <div class="sig-value">${escapeHtml(c.value)}</div>
      <div class="sig-read">${escapeHtml(c.read)}</div>
    </div>`).join("") : `<div class="empty">No gauges quoted.</div>`;
}

async function loadVolHistory() {
  $("vl-hist-note").textContent = "loading…";
  try {
    const d = await api(`/api/data/history/${encodeURIComponent("^VIX")}?start=${isoAgo(volHistDays)}`);
    const closes = d.bars.map((b) => b.close).filter((c) => c != null);
    if (!closes.length) throw new Error("no VIX history returned");
    const last = closes[closes.length - 1];
    const sorted = [...closes].sort((a, b) => a - b);
    const below = sorted.filter((c) => c < last).length;
    volHistStat = {
      percentile: (below / sorted.length) * 100,
      median: sorted[Math.floor(sorted.length / 2)],
      aboveTwenty: closes.filter((c) => c >= 20).length / closes.length,
      max: sorted[sorted.length - 1],
    };
    drawLine("vl-hist", d.bars.map((b) => b.date),
      [{ label: "VIX", data: d.bars.map((b) => b.close), color: "#ffd60a", fill: true }]);
    $("vl-hist-stats").innerHTML = [
      ["Last", last.toFixed(2), ""],
      ["Percentile", volOrdinal(volHistStat.percentile), ""],
      ["Median", volHistStat.median.toFixed(2), ""],
      ["Sessions over 20", (volHistStat.aboveTwenty * 100).toFixed(0) + "%", ""],
    ].map(([k, v, c]) => `<div class="metric"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`).join("");
    $("vl-hist-note").textContent = `${d.rows} sessions · peak ${volHistStat.max.toFixed(1)}`;
    renderVolSignals();
  } catch (e) {
    $("vl-hist-note").textContent = "";
    $("vl-hist-stats").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`;
  }
}

// Annualised standard deviation of daily log returns, rolled forward. Null
// until the window has filled, so the series starts where the maths does.
function volRealisedSeries(closes, window) {
  const rets = closes.map((c, i) => (i && closes[i - 1] > 0 && c > 0 ? Math.log(c / closes[i - 1]) : null));
  const out = new Array(closes.length).fill(null);
  for (let i = window; i < closes.length; i++) {
    const slice = rets.slice(i - window + 1, i + 1);
    if (slice.some((r) => r == null || !isFinite(r))) continue;
    const mean = slice.reduce((a, b) => a + b, 0) / slice.length;
    const variance = slice.reduce((a, b) => a + (b - mean) ** 2, 0) / (slice.length - 1);
    out[i] = Math.sqrt(variance * 252);
  }
  return out;
}

async function loadVolName() {
  const sym = ($("vl-symbol").value || "").trim().toUpperCase();
  if (!sym) return;
  const seq = ++volSeq;
  volSym = sym;
  volAtmIv = null;
  volRv = null;
  $("vl-warn").innerHTML = "";
  $("vl-name-note").textContent = sym;
  $("vl-rv-read").textContent = `Reading ${sym}'s daily closes…`;
  $("vl-cones").innerHTML = `<div class="empty">Loading…</div>`;
  $("vl-ivterm-read").textContent = "Reading the listed chain…";
  $("vl-skew-read").textContent = "Reading the listed chain…";
  $("vl-skew-note").textContent = "";
  setStatus(`LOADING VOLATILITY · ${sym}`);

  let spot = null;
  try {
    const [history, quote] = await Promise.all([
      api(`/api/data/history/${encodeURIComponent(sym)}?start=${isoAgo(1096)}`),
      api(`/api/v1/equity/price/quote?symbol=${encodeURIComponent(sym)}`).catch(() => null),
    ]);
    if (seq !== volSeq) return;
    const bars = history.bars.filter((b) => b.close > 0);
    if (bars.length < 40) throw new Error(`Not enough price history for ${sym}`);
    spot = (quote && quote.results && quote.results[0] && quote.results[0].last_price)
      || bars[bars.length - 1].close;
    const closes = bars.map((b) => b.close);
    const series = VOL_RV_WINDOWS.map((w) => volRealisedSeries(closes, w));
    volRv = { dates: bars.map((b) => b.date), series };
    const latest = series.map((s) => [...s].reverse().find((v) => v != null) ?? null);
    $("vl-name-stats").innerHTML = [
      ["21-day realised", volPts(latest[0])],
      ["63-day realised", volPts(latest[1])],
      ["1-year realised", volPts(latest[2])],
      ["30-day implied", "—"],
    ].map(([k, v]) => `<div class="metric"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
    $("vl-name-note").textContent = `${sym} · ${history.rows} sessions · spot ${spot.toFixed(2)}`;
    renderVolRvChart();
    setStatus(`VOLATILITY · ${sym} · ${history.source.toUpperCase()}`);
  } catch (e) {
    if (seq !== volSeq) return;
    // Without prices there is no spot, so the option panels have nothing to
    // hang off either — clear them rather than leave them spinning.
    $("vl-warn").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`;
    $("vl-name-stats").innerHTML = "";
    $("vl-rv-read").textContent = "";
    $("vl-cones").innerHTML = `<div class="empty">No prices for ${escapeHtml(sym)}.</div>`;
    $("vl-ivterm-read").textContent = `No prices for ${sym}, so there is nothing to price options against.`;
    $("vl-skew-read").textContent = "";
    ["vl-rv", "vl-ivterm", "vl-skew"].forEach((id) => {
      if (charts[id]) { charts[id].destroy(); delete charts[id]; }
    });
    setStatus("ERR: " + e.message);
    return;
  }
  loadVolCones(sym, seq);
  loadVolOptions(sym, spot, seq);
}

// The realised lanes are drawn as soon as prices land; the implied line is
// grafted on afterwards, because a chain takes several requests to assemble.
function renderVolRvChart() {
  if (!volRv) return;
  const start = Math.max(0, volRv.dates.length - 380);
  const dates = volRv.dates.slice(start);
  const datasets = [
    { label: "21-day realised", data: volRv.series[0].slice(start).map((v) => (v == null ? null : v * 100)), color: "#00c805" },
    { label: "63-day realised", data: volRv.series[1].slice(start).map((v) => (v == null ? null : v * 100)), color: "#5ac8fa" },
  ];
  if (volAtmIv != null) {
    datasets.push({ label: "30-day implied", data: dates.map(() => volAtmIv * 100), color: "#ffd60a", dash: true });
  }
  drawLine("vl-rv", dates, datasets);

  const rv21 = [...volRv.series[0]].reverse().find((v) => v != null);
  const rv63 = [...volRv.series[1]].reverse().find((v) => v != null);
  if (rv21 == null) { $("vl-rv-read").textContent = ""; return; }
  const trend = rv63 == null ? "" : rv21 > rv63
    ? ` It is running hotter than its three-month pace of ${volPts(rv63)}, so the recent tape is the noisy part.`
    : ` It is quieter than its three-month pace of ${volPts(rv63)} — the last month has been the calm stretch.`;
  const premium = volAtmIv == null ? ""
    : ` At-the-money options are charging ${volPts(volAtmIv)}, ` +
      (volAtmIv > rv21
        ? `${((volAtmIv - rv21) * 100).toFixed(1)} points above what the stock has actually delivered — the usual ` +
          `direction, and what option sellers are paid for.`
        : `${((rv21 - volAtmIv) * 100).toFixed(1)} points below what the stock has actually delivered — options look ` +
          `cheap against recent movement.`);
  $("vl-rv-read").textContent =
    `${volSym} has moved at an annualised ${volPts(rv21)} over the last 21 sessions.${trend}${premium}`;
}

// Where today's realised vol sits inside its own distribution, window by
// window. Percentile is interpolated between the five published knots.
function volConePercentile(row) {
  const knots = [[row.min, 0], [row.p25, 25], [row.median, 50], [row.p75, 75], [row.max, 100]];
  const x = row.realised;
  if (x == null || knots.some(([v]) => v == null)) return null;
  if (x <= knots[0][0]) return 0;
  for (let i = 1; i < knots.length; i++) {
    const [lo, pLo] = knots[i - 1], [hi, pHi] = knots[i];
    if (x <= hi) return hi === lo ? pHi : pLo + ((x - lo) / (hi - lo)) * (pHi - pLo);
  }
  return 100;
}

async function loadVolCones(sym, seq) {
  try {
    const d = await api(`/api/v1/technical/cones?symbol=${encodeURIComponent(sym)}`);
    if (seq !== volSeq) return;
    const rows = d.results || [];
    if (!rows.length) throw new Error("no cone data");
    $("vl-cones").innerHTML = `<table><tr><th>Window</th><th>Now</th><th>Percentile</th><th></th>
        <th>Low</th><th>Median</th><th>High</th></tr>` +
      rows.map((r) => {
        const pct = volConePercentile(r);
        const tone = pct == null ? "" : pct > 75 ? "neg" : pct < 25 ? "pos" : "";
        return `<tr>
          <td class="mono">${r.window}d</td>
          <td class="mono ${tone}">${volPts(r.realised)}</td>
          <td class="mono">${volOrdinal(pct)}</td>
          <td style="width:90px"><div class="perfbar"><div class="bar ${tone || "pos"}"
            style="width:${Math.round((pct ?? 0) / 100 * 80)}px"></div></div></td>
          <td class="mono dim">${volPts(r.min)}</td>
          <td class="mono dim">${volPts(r.median)}</td>
          <td class="mono dim">${volPts(r.max)}</td></tr>`;
      }).join("") + "</table>";
  } catch (e) {
    if (seq !== volSeq) return;
    $("vl-cones").innerHTML = `<div class="empty">No cones for ${escapeHtml(sym)} (${escapeHtml(e.message)}).</div>`;
  }
}

// One chain per sampled tenor rather than the whole surface: six requests buy
// a term structure that actually spans a year, where the first six listed
// expiries would all sit inside a month.
async function loadVolOptions(sym, spot, seq) {
  try {
    const d = await api(`/api/v1/derivatives/options/expirations?symbol=${encodeURIComponent(sym)}`);
    if (seq !== volSeq) return;
    const expiries = (d.results || []).filter((e) => e.days_to_expiry >= 0);
    if (!expiries.length) throw new Error("no listed expirations");
    const picks = [];
    VOL_TENORS.forEach((target) => {
      const best = expiries.reduce((a, b) =>
        (Math.abs(b.days_to_expiry - target) < Math.abs(a.days_to_expiry - target) ? b : a));
      if (!picks.some((p) => p.expiration === best.expiration)) picks.push(best);
    });
    const chains = (await Promise.all(picks.map((expiry) =>
      api(`/api/v1/derivatives/options/chains?symbol=${encodeURIComponent(sym)}` +
          `&expiration=${encodeURIComponent(expiry.expiration)}`)
        .then((r) => ({ expiry, rows: r.results || [] }))
        .catch(() => null)))).filter(Boolean);
    if (seq !== volSeq) return;
    if (!chains.length) throw new Error("no chains returned");

    const term = chains
      .map((c) => ({ expiry: c.expiry, iv: volChainAtmIv(c.rows, spot) }))
      .filter((p) => p.iv != null)
      .sort((a, b) => a.expiry.days_to_expiry - b.expiry.days_to_expiry);
    renderVolIvTerm(term);

    // The 30-day chain is both the implied line on the realised chart and the
    // one the skew is read from — near-dated enough to be liquid, long enough
    // that the wings still carry a real quote.
    const nearMonth = chains.reduce((a, b) =>
      (Math.abs(b.expiry.days_to_expiry - 30) < Math.abs(a.expiry.days_to_expiry - 30) ? b : a));
    volAtmIv = volChainAtmIv(nearMonth.rows, spot);
    if (volAtmIv != null) {
      const cell = document.querySelectorAll("#vl-name-stats .metric .v")[3];
      if (cell) cell.textContent = volPts(volAtmIv);
      renderVolRvChart();
    }
    renderVolSkew(nearMonth, spot);
  } catch (e) {
    if (seq !== volSeq) return;
    if (charts["vl-ivterm"]) { charts["vl-ivterm"].destroy(); delete charts["vl-ivterm"]; }
    if (charts["vl-skew"]) { charts["vl-skew"].destroy(); delete charts["vl-skew"]; }
    const note = `No listed options for ${escapeHtml(sym)} (${escapeHtml(e.message)}).`;
    $("vl-ivterm-read").innerHTML = note;
    $("vl-skew-read").innerHTML = note;
  }
}

// The straddle at the nearest strike to spot: the cleanest single read of what
// an expiry is charging, and the same convention the chain tab uses.
function volChainAtmIv(rows, spot) {
  if (!spot) return null;
  const usable = (rows || []).filter((r) => r.implied_volatility > 0 && r.strike > 0);
  if (!usable.length) return null;
  const nearest = usable.reduce((a, b) =>
    (Math.abs(b.strike - spot) < Math.abs(a.strike - spot) ? b : a)).strike;
  const ivs = ["call", "put"]
    .map((side) => usable.find((r) => r.strike === nearest && r.option_type === side))
    .filter(Boolean).map((r) => r.implied_volatility);
  return ivs.length ? ivs.reduce((a, b) => a + b, 0) / ivs.length : null;
}

function renderVolIvTerm(term) {
  if (term.length < 2) {
    if (charts["vl-ivterm"]) { charts["vl-ivterm"].destroy(); delete charts["vl-ivterm"]; }
    $("vl-ivterm-read").textContent = "Too few expiries quoted implied vol to draw a term structure.";
    return;
  }
  drawLine("vl-ivterm", term.map((p) => `${p.expiry.days_to_expiry}d`),
    [{ label: "At-the-money implied", data: term.map((p) => p.iv * 100), color: "#bf5af2", fill: true }]);
  const front = term[0], back = term[term.length - 1];
  const slope = back.iv - front.iv;
  $("vl-ivterm-read").textContent = Math.abs(slope) < 0.01
    ? `Implied vol is flat across the curve at about ${volPts(front.iv)} — no expiry is being singled out.`
    : slope > 0
      ? `The curve rises from ${volPts(front.iv)} at ${front.expiry.days_to_expiry} days to ` +
        `${volPts(back.iv)} at ${back.expiry.days_to_expiry} days. Longer-dated options cost more vol, ` +
        `which is the ordinary shape for a name with nothing imminent priced in.`
      : `The curve is inverted: ${volPts(front.iv)} at ${front.expiry.days_to_expiry} days against ` +
        `${volPts(back.iv)} at ${back.expiry.days_to_expiry} days. Something dated — earnings, a ruling, ` +
        `a deal — is being priced into the front.`;
}

// Out-of-the-money wings only: puts below spot, calls above. That is the curve
// a trader actually pays, and it keeps in-the-money quotes (wide, stale) out.
function volSkewCurve(rows, spot) {
  const points = [];
  (rows || []).forEach((r) => {
    if (!(r.implied_volatility > 0) || !(r.strike > 0)) return;
    const moneyness = r.strike / spot;
    if (moneyness < 0.7 || moneyness > 1.3) return;
    if ((r.option_type === "put" && moneyness <= 1) || (r.option_type === "call" && moneyness > 1)) {
      points.push({ moneyness, iv: r.implied_volatility });
    }
  });
  return points.sort((a, b) => a.moneyness - b.moneyness);
}

function volInterpolate(points, moneyness) {
  if (points.length < 2 || moneyness < points[0].moneyness ||
      moneyness > points[points.length - 1].moneyness) return null;
  for (let i = 1; i < points.length; i++) {
    const lo = points[i - 1], hi = points[i];
    if (moneyness <= hi.moneyness) {
      const span = hi.moneyness - lo.moneyness;
      return span === 0 ? hi.iv : lo.iv + ((moneyness - lo.moneyness) / span) * (hi.iv - lo.iv);
    }
  }
  return null;
}

function renderVolSkew(chain, spot) {
  const points = volSkewCurve(chain.rows, spot);
  $("vl-skew-note").textContent = `${chain.expiry.expiration} · ${chain.expiry.days_to_expiry} days · ` +
    `puts below spot, calls above`;
  if (points.length < 4) {
    if (charts["vl-skew"]) { charts["vl-skew"].destroy(); delete charts["vl-skew"]; }
    $("vl-skew-read").textContent = "That expiry does not quote enough out-of-the-money contracts to draw a skew.";
    return;
  }
  drawLine("vl-skew", points.map((p) => Math.round(p.moneyness * 100) + "%"),
    [{ label: "Implied vol by strike", data: points.map((p) => p.iv * 100), color: "#ff8fab" }]);
  const downside = volInterpolate(points, 0.9), upside = volInterpolate(points, 1.1);
  if (downside == null || upside == null) {
    $("vl-skew-read").textContent =
      `The quoted wings do not reach 10% either side of spot, so there is no comparable skew number — ` +
      `read the curve above instead.`;
    return;
  }
  const spread = (downside - upside) * 100;
  $("vl-skew-read").textContent = spread > 0
    ? `Puts 10% below spot cost ${volPts(downside)}, calls 10% above cost ${volPts(upside)} — ` +
      `${spread.toFixed(1)} vol points more for the downside. That premium is the normal state of an ` +
      `equity chain: protection is bid, and a crash is priced as more violent than a melt-up.`
    : `Calls 10% above spot cost ${volPts(upside)} against ${volPts(downside)} for puts 10% below — ` +
      `${Math.abs(spread).toFixed(1)} points of upside skew. That is unusual for a single name, and ` +
      `normally means the upside is where the event risk sits.`;
}

$("vl-refresh").onclick = () => { loadVolComplex(); loadVolHistory(); };
$("vl-load").onclick = () => loadVolName();
$("vl-symbol").onkeydown = (ev) => { if (ev.key === "Enter" && !ev.defaultPrevented) loadVolName(); };
document.querySelectorAll("#vl-hist-ranges .chip").forEach((c) => {
  c.onclick = () => {
    document.querySelectorAll("#vl-hist-ranges .chip").forEach((x) => x.classList.remove("active"));
    c.classList.add("active");
    volHistDays = Number(c.dataset.days);
    loadVolHistory();
  };
});

// ---------- portfolio ----------
// One portfolio at a time. The summary panel is the only one that must load;
// performance, risk, factors and allocation each need price history and fail
// independently, so a brand-new book still shows its holdings.
let pfPortfolios = [];
let pfId = null;

async function loadPortfolioView() {
  try {
    pfPortfolios = await api("/api/portfolios");
  } catch (e) { setStatus("ERR: " + e.message); return; }
  const select = $("pf-select");
  select.innerHTML = pfPortfolios.map((p) =>
    `<option value="${p.id}">${escapeHtml(p.name)} · ${p.cost_basis_method.toUpperCase()} · vs ${escapeHtml(p.benchmark)}</option>`).join("");
  if (!pfPortfolios.length) {
    $("pf-empty").style.display = "block";
    $("pf-body").style.display = "none";
    return;
  }
  if (!pfPortfolios.some((p) => p.id === pfId)) {
    pfId = (pfPortfolios.find((p) => p.is_default) || pfPortfolios[0]).id;
  }
  select.value = String(pfId);
  $("pf-empty").style.display = "none";
  $("pf-body").style.display = "block";
  await loadPortfolio();
}

$("pf-select").onchange = () => { pfId = Number($("pf-select").value); loadPortfolio(); };
$("pf-refresh").onclick = () => loadPortfolio();

$("pf-create").onclick = async () => {
  const name = $("pf-newname").value.trim();
  if (!name) return;
  const cash = parseFloat($("pf-newcash").value) || 0;
  try {
    const created = await api("/api/portfolios", {
      method: "POST", body: { name, initial_cash: cash, is_default: !pfPortfolios.length },
    });
    $("pf-newname").value = ""; $("pf-newcash").value = "";
    pfId = created.id;
    await loadPortfolioView();
    setStatus(`PORTFOLIO "${name.toUpperCase()}" CREATED`);
  } catch (e) { setStatus("ERR: " + e.message); }
};

$("pf-delete").onclick = async () => {
  if (!pfId) return;
  const current = pfPortfolios.find((p) => p.id === pfId);
  if (!confirm(`Delete "${current ? current.name : "this portfolio"}" and every transaction in it?`)) return;
  try {
    await api(`/api/portfolios/${pfId}`, { method: "DELETE" });
    pfId = null;
    await loadPortfolioView();
  } catch (e) { setStatus("ERR: " + e.message); }
};

// Buys and sells need a symbol and a price; cash movements do not.
$("tx-side").onchange = () => {
  const cashOnly = ["deposit", "withdraw", "fee"].includes($("tx-side").value);
  $("tx-symbol").disabled = cashOnly;
  $("tx-price").disabled = cashOnly || $("tx-side").value === "dividend";
  $("tx-qty").placeholder = cashOnly || $("tx-side").value === "dividend" ? "Amount" : "Quantity";
};

$("tx-add").onclick = async () => {
  const msg = $("tx-msg");
  msg.className = "msg"; msg.textContent = "";
  const side = $("tx-side").value;
  const body = {
    side,
    symbol: $("tx-symbol").value.trim().toUpperCase() || null,
    quantity: parseFloat($("tx-qty").value),
    price: parseFloat($("tx-price").value) || 1,
    fees: parseFloat($("tx-fees").value) || 0,
  };
  if (!body.quantity || body.quantity <= 0) {
    msg.textContent = "Enter a quantity greater than zero."; return;
  }
  if ($("tx-date").value) body.trade_date = $("tx-date").value + "T00:00:00";
  try {
    await api(`/api/portfolios/${pfId}/transactions`, { method: "POST", body });
    ["tx-symbol", "tx-qty", "tx-price", "tx-fees"].forEach((id) => { $(id).value = ""; });
    msg.className = "msg ok"; msg.textContent = "Recorded.";
    await loadPortfolio();
  } catch (e) { msg.textContent = e.message; }
};

async function loadPortfolio() {
  if (!pfId) return;
  setStatus("VALUING PORTFOLIO…");
  ["pf-holdings", "pf-alloc", "pf-riskcontrib", "pf-factors", "pf-blotter"].forEach((id) => {
    $(id).innerHTML = `<div class="empty">Loading…</div>`;
  });
  // Hedging is opt-in per book: it fetches live option chains, so it waits
  // for the button rather than running on every portfolio switch.
  $("hg-verdict").innerHTML = "";
  $("hg-notes").innerHTML = "";
  $("hg-rows").innerHTML = `<div class="empty">Choose a horizon and press Analyse.</div>`;
  hgLast = null;
  hgScenario("hg", {});
  $("hg-narrative").innerHTML = "";
  hgLoadLog();
  hgNarrateStatus();
  await loadPortfolioSummary();
  loadPortfolioBlotter();
  loadPortfolioPerformance();
  loadPortfolioRisk();
  loadPortfolioFactors();
  loadPortfolioAllocation();
}

async function loadPortfolioSummary() {
  try {
    const d = await api(`/api/portfolios/${pfId}/summary`);
    const t = d.totals;
    const cards = [
      ["Total value", fmt$(t.total_value), "", "Holdings plus cash."],
      ["Profit so far", fmt$(t.total_pnl), cls(t.total_pnl),
        t.total_pnl_pct == null ? "Value beyond the money you put in."
          : `${fmtPct(t.total_pnl_pct, true)} on ${fmt$(t.net_deposits)} paid in.`],
      ["Today", fmt$(t.day_change), cls(t.day_change),
        t.day_change_pct == null ? "Change since yesterday's close."
          : `${fmtPct(t.day_change_pct, true)} since yesterday's close.`],
      ["Unrealised", fmt$(t.unrealized_pnl), cls(t.unrealized_pnl), "Paper gains on what you still hold."],
      ["Realised", fmt$(t.realized_pnl), cls(t.realized_pnl), "Locked in by selling."],
      ["Cash", fmt$(t.cash), "", `${d.positions.length} holding${d.positions.length === 1 ? "" : "s"}, ${fmt$(t.dividends)} of dividends received.`],
    ];
    $("pf-totals").innerHTML = cards.map(([k, v, c, note]) =>
      `<div class="metric"><div class="k">${k}</div><div class="v ${c}">${v}</div><div class="note">${note}</div></div>`).join("");

    $("pf-asof").textContent = d.warnings.length ? d.warnings[0] : "live prices";
    if (!d.positions.length) {
      $("pf-holdings").innerHTML = `<div class="empty">No open positions — record a buy above.</div>`;
      return;
    }
    $("pf-holdings").innerHTML =
      `<table><tr><th>symbol</th><th>shares</th><th>avg cost</th><th>price</th><th>value</th>` +
      `<th>gain</th><th>today</th><th>weight</th><th></th></tr>` +
      d.positions.map((r) => `<tr>
        <td><a class="linkbtn" data-pfsym="${escapeHtml(r.symbol)}">${escapeHtml(r.symbol)}</a></td>
        <td>${r.quantity.toLocaleString()}</td>
        <td>${r.avg_cost.toFixed(2)}</td>
        <td>${r.last_price == null ? "-" : r.last_price.toFixed(2)}</td>
        <td>${fmt$(r.market_value)}</td>
        <td class="${cls(r.unrealized_pnl)}">${fmt$(r.unrealized_pnl)}${
          r.unrealized_pnl_pct == null ? "" : ` <small>(${fmtPct(r.unrealized_pnl_pct, true)})</small>`}</td>
        <td class="${cls(r.day_change || 0)}">${r.day_change == null ? "-" : fmt$(r.day_change)}</td>
        <td>${fmtPct(r.weight)}</td>
        <td><button class="ghost" data-pfsell="${escapeHtml(r.symbol)}" data-pfqty="${r.quantity}">Sell all</button></td>
      </tr>`).join("") + "</table>";

    $("pf-holdings").querySelectorAll("[data-pfsym]").forEach((el) => {
      el.onclick = () => openStock(el.dataset.pfsym, "portfolio");
    });
    $("pf-holdings").querySelectorAll("[data-pfsell]").forEach((el) => {
      el.onclick = () => {
        $("tx-side").value = "sell"; $("tx-side").onchange();
        $("tx-symbol").value = el.dataset.pfsell;
        $("tx-qty").value = el.dataset.pfqty;
        $("tx-price").focus();
      };
    });
  } catch (e) {
    $("pf-holdings").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`;
  }
  setStatus("PORTFOLIO UPDATED");
}

async function loadPortfolioPerformance() {
  $("pf-perf").innerHTML = "";
  $("pf-perf-head").textContent = "Loading…";
  $("pf-perf-note").textContent = "";
  try {
    const d = await api(`/api/portfolios/${pfId}/performance`);
    const t = d.totals, m = d.metrics;
    $("pf-perf-head").textContent =
      `${fmt$(t.starting_value)} became ${fmt$(t.ending_value)} — ` +
      `${t.time_weighted_return >= 0 ? "a gain of" : "a loss of"} ${Math.abs(t.time_weighted_return * 100).toFixed(1)}% ` +
      `since ${d.period.start}.`;
    $("pf-perf-note").textContent = d.benchmark
      ? `${d.benchmark.symbol} returned ${fmtPct(d.benchmark.total_return, true)} over the same period — ` +
        (d.benchmark.excess_return >= 0 ? "you came out ahead." : "the market did better.")
      : "";

    const labels = d.series.map((p) => p.date);
    const datasets = [{ label: "Your portfolio", data: d.series.map((p) => p.total_value), color: "#00c805", fill: true }];
    if (d.benchmark && d.benchmark.series.length) {
      // The benchmark comes back on the same dates, as growth of 1 — rebase it
      // to the opening value so both curves are in dollars.
      datasets.push({
        label: d.benchmark.symbol,
        data: d.benchmark.series.map((g) => t.starting_value * g),
        color: "#6f7377", dash: true,
      });
    }
    drawLine("pf-chart", labels, datasets);

    const cards = [
      ["Per year", m.cagr == null ? "-" : fmtPct(m.cagr, true), cls(m.cagr || 0),
        "Time-weighted — deposits do not flatter it."],
      ["What you earned", t.money_weighted_return_annual == null ? "-" : fmtPct(t.money_weighted_return_annual, true),
        cls(t.money_weighted_return_annual || 0), "Yearly return on your actual cash, timing included."],
      ["Risk-adjusted", m.sharpe == null ? "-" : m.sharpe.toFixed(2), m.sharpe >= 1 ? "pos" : "",
        "Sharpe ratio — return per unit of risk."],
      ["Worst dip", m.max_drawdown == null ? "-" : fmtPct(m.max_drawdown), "neg", "Biggest fall from a peak."],
      ["Choppiness", m.annualised_volatility == null ? "-" : fmtPct(m.annualised_volatility), "",
        "How much the value swings, per year."],
      ["Market sensitivity", d.benchmark && d.benchmark.beta != null ? d.benchmark.beta.toFixed(2) : "-", "",
        "Beta — 1.0 moves with the market, 0.5 half as much."],
    ];
    $("pf-perf").innerHTML = cards.map(([k, v, c, note]) =>
      `<div class="metric"><div class="k">${k}</div><div class="v ${c}">${v}</div><div class="note">${note}</div></div>`).join("");
  } catch (e) {
    $("pf-perf-head").textContent = "";
    $("pf-perf").innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
}

async function loadPortfolioRisk() {
  try {
    const d = await api(`/api/portfolios/${pfId}/risk`);
    const v = d.value_at_risk;
    $("pf-risk").innerHTML = [
      [`A bad day (${fmtPct(d.confidence)} confidence)`, `${fmt$(Math.abs(v.historical_amount))} (${fmtPct(Math.abs(v.historical_pct))})`],
      ["A very bad day", v.conditional_amount == null ? "-" : fmt$(Math.abs(v.conditional_amount))],
      ["Yearly choppiness", d.volatility.annualised == null ? "-" : fmtPct(d.volatility.annualised)],
      ["Worst dip so far", d.drawdown.max == null ? "-" : fmtPct(d.drawdown.max)],
      ["Down from the peak now", d.drawdown.current == null ? "-" : fmtPct(d.drawdown.current)],
      ["Genuinely diversified across", `${d.concentration.effective_positions ?? "-"} names`],
    ].map(([k, val]) =>
      `<div class="r"><span class="k">${k}</span><span class="v">${val}</span></div>`).join("");

    $("pf-riskcontrib").innerHTML = d.risk_contribution.length
      ? `<table><tr><th>symbol</th><th>weight</th><th>own swings</th><th>share of risk</th></tr>` +
        d.risk_contribution.map((r) => `<tr><td>${escapeHtml(r.symbol)}</td><td>${fmtPct(r.weight)}</td>` +
          `<td>${fmtPct(r.volatility)}</td><td><b>${fmtPct(r.pct_of_risk)}</b></td></tr>`).join("") + "</table>"
      : `<div class="empty">Need at least two holdings to split the risk up.</div>`;
  } catch (e) {
    $("pf-risk").innerHTML = "";
    $("pf-riskcontrib").innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
}

async function loadPortfolioFactors() {
  try {
    const d = await api(`/api/portfolios/${pfId}/factors`);
    if (d.exposure.error) {
      $("pf-factors").innerHTML = `<div class="empty">Not enough overlapping history yet — ${escapeHtml(d.exposure.error)}.</div>`;
      return;
    }
    const b = d.exposure.betas || {};
    const LABELS = {
      MKT: ["Follows the market", "How much it moves with stocks as a whole."],
      MOM: ["Rides winners", "Positive means it leans towards recent strong performers."],
      LOWVOL: ["Prefers steady names", "Positive means it favours calmer stocks."],
    };
    $("pf-factors").innerHTML =
      `<table><tr><th>driver</th><th>exposure</th><th>reliable?</th><th>what it means</th></tr>` +
      Object.keys(b).map((k) => {
        const t = (d.exposure.tstats || {})[k];
        const sure = Math.abs(t) >= 2 ? "yes" : "not really";
        const [label, note] = LABELS[k] || [k, ""];
        return `<tr><td>${label}</td><td><b>${b[k].toFixed(2)}</b></td><td>${sure}</td><td class="dim">${note}</td></tr>`;
      }).join("") +
      `</table><p class="explain">These three drivers explain ` +
      `${fmtPct(d.exposure.r_squared)} of your portfolio's day-to-day movement. ` +
      `The rest is stock-specific.</p>`;
  } catch (e) {
    $("pf-factors").innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
}

async function loadPortfolioAllocation() {
  try {
    const d = await api(`/api/portfolios/${pfId}/allocation`);
    if (!d.by_sector.length) { $("pf-alloc").innerHTML = `<div class="empty">Nothing held yet.</div>`; return; }
    const bar = (w) => `<div class="wbar"><i style="width:${Math.min(100, Math.abs(w) * 100).toFixed(1)}%"></i></div>`;
    $("pf-alloc").innerHTML =
      `<table><tr><th>sector</th><th>weight</th><th></th><th>holdings</th></tr>` +
      d.by_sector.map((s) => `<tr><td>${escapeHtml(s.sector)}</td><td>${fmtPct(s.weight)}</td>` +
        `<td style="width:40%">${bar(s.weight)}</td><td class="dim">${escapeHtml(s.symbols.join(", "))}</td></tr>`).join("") +
      "</table>" +
      (d.cash_weight == null ? "" : `<p class="explain">Cash is ${fmtPct(d.cash_weight)} of the account.</p>`);
  } catch (e) {
    $("pf-alloc").innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
}

async function loadPortfolioBlotter() {
  try {
    const rows = await api(`/api/portfolios/${pfId}/transactions?limit=200`);
    if (!rows.length) { $("pf-blotter").innerHTML = `<div class="empty">No entries yet.</div>`; return; }
    $("pf-blotter").innerHTML =
      `<table><tr><th>date</th><th>what</th><th>symbol</th><th>quantity</th><th>price</th>` +
      `<th>fees</th><th>cash</th><th></th></tr>` +
      rows.map((r) => `<tr>
        <td>${(r.trade_date || "").slice(0, 10)}</td>
        <td>${escapeHtml(r.side)}</td>
        <td>${escapeHtml(r.symbol || "-")}</td>
        <td>${r.quantity.toLocaleString()}</td>
        <td>${["buy", "sell"].includes(r.side) ? r.price.toFixed(2) : "-"}</td>
        <td>${r.fees ? r.fees.toFixed(2) : "-"}</td>
        <td class="${cls(r.cash_flow)}">${fmt$(r.cash_flow)}</td>
        <td><button class="ghost" data-pftx="${r.id}">Delete</button></td>
      </tr>`).join("") + "</table>";
    $("pf-blotter").querySelectorAll("[data-pftx]").forEach((el) => {
      el.onclick = async () => {
        if (!confirm("Delete this entry? Your positions will be recalculated.")) return;
        await api(`/api/portfolios/${pfId}/transactions/${el.dataset.pftx}`, { method: "DELETE" });
        loadPortfolio();
      };
    });
  } catch (e) {
    $("pf-blotter").innerHTML = `<div class="errbox">${escapeHtml(e.message)}</div>`;
  }
}

// ---------- assistant ----------
// The transcript lives here and is replayed to the server on every turn — the
// Messages API is stateless, so the browser is the only place it exists.
let asHistory = [];
let asStatus = null;
let asAbort = null;

async function loadAssistantView() {
  if (!asStatus) {
    try { asStatus = await api("/api/assistant/status"); }
    catch (e) { asStatus = { enabled: false, reason: e.message }; }
  }
  $("as-on").style.display = asStatus.enabled ? "block" : "none";
  $("as-off").style.display = asStatus.enabled ? "none" : "block";
  if (!asStatus.enabled) $("as-off-why").textContent = asStatus.reason || "Unavailable.";
  else $("as-input").focus();
}

// Just enough Markdown for a chat reply: bold, italics, inline code and
// bullets. Everything is escaped first, so the only tags in the output are the
// ones added here.
function mdLite(src) {
  // Code spans are stashed behind NUL sentinels so their contents escape the
  // bold/italic passes, and so the marker can never collide with real text (a
  // bare digit placeholder would match the "5" in "up 5 percent").
  const code = [];
  let s = escapeHtml(src).replace(/`([^`\n]+)`/g, (_, c) => {
    code.push(c); return `\u0000${code.length - 1}\u0000`;
  });
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
       .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s.,;:!?)]|$)/g, "$1<em>$2</em>");

  const html = s.split(/\n{2,}/).map((block) => {
    const lines = block.split("\n");
    if (lines.every((l) => /^\s*[-*]\s+/.test(l) || !l.trim())) {
      const items = lines.filter((l) => l.trim())
        .map((l) => `<li>${l.replace(/^\s*[-*]\s+/, "")}</li>`).join("");
      return items ? `<ul>${items}</ul>` : "";
    }
    return `<p>${lines.join("<br>")}</p>`;
  }).join("");

  return html.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${code[i]}</code>`);
}

function asAddTurn(role) {
  const el = document.createElement("div");
  el.className = "turn" + (role === "user" ? " me" : "");
  el.innerHTML = `<div class="who">${role === "user" ? "YOU" : "ASSISTANT"}</div>
    <div class="toolrow"></div><div class="body"></div>`;
  $("as-log").appendChild(el);
  el.querySelector(".toolrow").style.display = "none";
  return el;
}
function asScroll() { const l = $("as-log"); l.scrollTop = l.scrollHeight; }

// A chip per command the assistant reaches for, so the reader can see which
// part of the platform an answer actually came from.
function asToolChip(row, event) {
  const label = event.name === "run_command" || event.name === "describe_command"
    ? (event.input && event.input.path) || event.name
    : event.name === "search_commands"
      ? `search "${(event.input && event.input.query) || ""}"`
      : event.name === "get_user_context" ? "your saved data" : event.name;
  const chip = document.createElement("span");
  chip.className = "toolchip running";
  chip.innerHTML = `<i class="dot"></i>${escapeHtml(String(label).slice(0, 52))}`;
  row.style.display = "flex";
  row.appendChild(chip);
  return chip;
}

async function askAssistant(text) {
  text = (text || "").trim();
  if (!text || asAbort) return;

  $("as-input").value = "";
  asHistory.push({ role: "user", content: text });
  asAddTurn("user").querySelector(".body").textContent = text;

  const turn = asAddTurn("assistant");
  const body = turn.querySelector(".body");
  const toolrow = turn.querySelector(".toolrow");
  const pending = [];
  let answer = "";
  const draw = () => { body.innerHTML = mdLite(answer) + `<span class="caret"></span>`; asScroll(); };
  draw();

  asAbort = new AbortController();
  $("as-send").style.display = "none";
  $("as-stop").style.display = "inline-block";
  setStatus("ASSISTANT THINKING…");

  try {
    const res = await fetch(API + "/api/assistant/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ messages: asHistory }),
      signal: asAbort.signal,
    });
    if (res.status === 401) { logout(); return; }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    // SSE frames are separated by a blank line; a frame may straddle chunks.
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop();
      for (const frame of frames) {
        const line = frame.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line.slice(6)); } catch { continue; }

        if (ev.type === "text") { answer += ev.text; draw(); }
        else if (ev.type === "tool") pending.push(asToolChip(toolrow, ev));
        else if (ev.type === "tool_done") {
          const chip = pending.shift();
          if (chip) chip.className = "toolchip" + (ev.ok ? "" : " failed");
        } else if (ev.type === "error") {
          answer += (answer ? "\n\n" : "") + ev.message;
          body.classList.add("errbox");
        }
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") answer += (answer ? "\n\n" : "") + e.message;
  } finally {
    asAbort = null;
    $("as-send").style.display = "inline-block";
    $("as-stop").style.display = "none";
    setStatus("READY");
    body.innerHTML = mdLite(answer || "(no reply)");
    pending.forEach((c) => { c.className = "toolchip"; });
    if (answer) asHistory.push({ role: "assistant", content: answer });
    asScroll();
  }
}

$("as-send").onclick = () => askAssistant($("as-input").value);
$("as-stop").onclick = () => { if (asAbort) asAbort.abort(); };
$("as-clear").onclick = () => { asHistory = []; $("as-log").innerHTML = ""; $("as-input").focus(); };
$("as-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); askAssistant($("as-input").value); }
});
$("as-suggest").querySelectorAll("[data-ask]").forEach((b) => {
  b.onclick = () => askAssistant(b.dataset.ask);
});

// ---------- boot ----------
(async () => {
  if (token) { try { await enterTerminal(); } catch { logout(); } }
  switchAuth("login");
})();

// ---------- thesis ----------
let thLoaded = false;
let thCurrentId = null;

function loadThesisView() {
  if (thLoaded) return;
  thLoaded = true;
  thLoadTheses();
  thLoadSources();
  thTriageStatus();
}

// ---------- idea sources ----------
// The funnel menu comes from the backend, so a source registered server-side
// shows up here without a frontend change. Its params arrive with their own
// kinds and clamps, which is what lets one panel drive every scanner.
let thSources = null;

function thSource() {
  if (!thSources) return null;
  return thSources.sources.find((s) => s.name === $("th-source").value) || null;
}

async function thLoadSources() {
  try {
    thSources = await api("/api/theses/triage/sources?universe=stocks");
  } catch (e) {
    $("th-cand-note").textContent = e.message;
    return;
  }
  $("th-source").innerHTML = thSources.sources.map((s) =>
    `<option value="${escapeHtml(s.name)}"${s.name === thSources.default ? " selected" : ""}>${
      escapeHtml(s.label)}</option>`).join("");
  $("th-source").onchange = thRenderSource;
  thRenderSource();
}

function thRenderSource() {
  const src = thSource();
  if (!src) return;
  $("th-cand-note").textContent = src.scope;
  $("th-params").innerHTML = Object.entries(src.params || {}).map(([name, spec]) => {
    const id = `th-p-${name}`;
    const label = escapeHtml(name.replace(/_/g, " "));
    const title = escapeHtml(spec.help || "");
    if (spec.kind === "bool") {
      return `<label class="panel-note" style="margin-left:0" title="${title}">
        <input type="checkbox" id="${id}"${spec.default ? " checked" : ""} /> ${label}</label>`;
    }
    const bounds = (spec.min == null ? "" : ` min="${spec.min}"`) +
                   (spec.max == null ? "" : ` max="${spec.max}"`);
    return `<label class="panel-note" style="margin-left:0" title="${title}">${label}
      <input type="${spec.kind === "str" ? "text" : "number"}" id="${id}" class="mono"
        style="width:120px;margin-left:6px"
        value="${spec.default == null ? "" : escapeHtml(String(spec.default))}"${bounds} /></label>`;
  }).join("");
  $("th-cand-warn").innerHTML = "";
  $("th-cands").innerHTML = `<div class="empty">Press “Find candidates” to scan.</div>`;
}

function thParams() {
  const src = thSource();
  const qs = new URLSearchParams();
  if (!src) return qs;
  qs.set("source", src.name);
  for (const [name, spec] of Object.entries(src.params || {})) {
    const el = $(`th-p-${name}`);
    if (!el) continue;
    qs.set(name, spec.kind === "bool" ? String(el.checked) : el.value);
  }
  return qs;
}

async function thTriageStatus() {
  const note = $("th-triage-note");
  const btn = $("th-triage-run");
  try {
    const s = await api("/api/theses/triage/status");
    if (s.enabled) {
      btn.disabled = false;
      note.textContent = "model: " + (s.model || "ready");
    } else {
      btn.disabled = true;
      note.textContent = s.reason || "switched off";
    }
  } catch (e) {
    btn.disabled = true;
    note.textContent = e.message;
  }
}

async function thTriageRun() {
  const box = $("th-triage");
  const btn = $("th-triage-run");
  btn.disabled = true;
  box.innerHTML = `<div class="empty">Running the funnel and asking the model… this is one call but it reads every card.</div>`;
  try {
    const d = await api(`/api/theses/triage?${thParams()}`, { method: "POST" });
    thRenderTriage(d);
  } catch (e) {
    box.innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
  btn.disabled = false;
}

function thRenderTriage(d) {
  const box = $("th-triage");
  const cands = d.candidates || [];
  if (!cands.length) {
    box.innerHTML = `<div class="empty">${escapeHtml(d.note || "The model promoted nothing — which is a valid answer.")}</div>`;
    return;
  }
  const srcTag = (leg) => leg.source === "world_knowledge"
    ? `<span class="badge">needs verifying</span>` : `<span class="dim">from the data</span>`;
  // Which funnel these came from is part of reading the verdict: the model was
  // told that source's characteristic failure mode and argued against it.
  const ran = thSources && thSources.sources.find((s) => s.name === d.source);
  box.innerHTML =
    `<div class="explain">${ran ? `<strong>${escapeHtml(ran.label)}</strong> · ` : ""}${
      escapeHtml(d.disclaimer || "")}</div>` +
    cands.map((c, i) => `
    <div class="panel" style="margin-top:10px">
      <div class="panel-h">
        <span class="mono">${escapeHtml(c.symbol)}</span>
        <span class="${c.promote ? "pos" : "dim"}" style="margin-left:10px">${c.promote ? "PROMOTED" : "passed over"}</span>
        <span class="${c.direction === "long" ? "pos" : c.direction === "short" ? "neg" : "dim"}" style="margin-left:10px">${escapeHtml((c.direction || "neutral").toUpperCase())}</span>
        <span class="panel-note">confidence ${escapeHtml(c.confidence || "?")} · scanner-artifact risk ${escapeHtml(c.calendar_artifact_risk || "?")}</span>
        ${c.promote ? `<button class="ghost panel-btn" data-dive="${i}">Deep dive → draft thesis</button>` : ""}
      </div>
      ${c.claim_sketch ? `<p class="explain">${escapeHtml(c.claim_sketch)}</p>` : ""}
      <p class="explain dim">${escapeHtml(c.reason || "")}</p>
      ${(c.legs || []).map((l) => `
        <div class="explain" style="margin:4px 0 0 12px">
          • ${escapeHtml(l.claim)} ${srcTag(l)}
          ${l.rejected ? `<span class="neg"> — rejected: ${escapeHtml(l.rejected)}</span>` : ""}
          ${l.if_absent ? `<div class="dim" style="margin-left:14px">if nothing found: ${escapeHtml(l.if_absent)}</div>` : ""}
          ${(l.verify_with || []).map((v) => `
            <div class="mono dim" style="margin-left:14px">check: ${escapeHtml(v.path)}${v.unknown_command ? ` <span class="neg">(not a real command)</span>` : ""} — ${escapeHtml(v.expect || "")}</div>`).join("")}
        </div>`).join("")}
      <div class="msg" id="th-dive-msg-${i}"></div>
    </div>`).join("");
  box.querySelectorAll("[data-dive]").forEach((btn) => {
    btn.onclick = () => thDeepDive(cands[+btn.dataset.dive], +btn.dataset.dive, btn);
  });
}

async function thDeepDive(candidate, i, btn) {
  const msg = $(`th-dive-msg-${i}`);
  btn.disabled = true;
  msg.textContent = "Verifying each leg against live data… this runs several tool calls and can take a couple of minutes.";
  try {
    const d = await api(`/api/theses/deepdive?create_draft=true`, {
      method: "POST",
      body: {
        symbol: candidate.symbol,
        direction: candidate.direction || "neutral",
        idea_source: candidate.idea_source || (thSource() && thSource().name),
        legs: candidate.legs || [],
      },
    });
    const dossier = d.dossier || {};
    if (d.draft_thesis_id) {
      msg.innerHTML = `<span class="pos">Draft thesis created</span> — ${d.evidence_frozen || 0} evidence snapshot(s) frozen, ` +
        `${d.checks_installed || 0} falsifier(s) installed.` +
        (d.skipped_citations ? ` <span class="dim">Skipped: ${escapeHtml(d.skipped_citations.join(", "))}</span>` : "") +
        // Every proposed falsifier is run before it is kept, so a rejection is
        // a fact about live data and worth reading rather than hiding.
        (d.rejected_checks ? `<div class="dim" style="margin-top:4px">Rejected falsifiers: ` +
          escapeHtml(d.rejected_checks.map((c) => `${c.name} (${c.reason})`).join("; ")) + `</div>` : "");
      await thLoadTheses();
      thShowThesis(d.draft_thesis_id);
      $("th-detail").scrollIntoView({ behavior: "smooth" });
    } else {
      msg.innerHTML = `<span class="dim">The model chose not to proceed:</span> ${escapeHtml(dossier.summary || "no summary")}`;
    }
  } catch (e) {
    msg.textContent = e.message;
  }
  btn.disabled = false;
}

//: Column names whose values are dollars rather than counts.
const TH_MONEY = /(^|_)(value|amount|amount_floor|usd)$/;

function thCell(column, value) {
  if (value == null || value === "") return `<td class="dim"></td>`;
  if (typeof value === "boolean") return `<td>${value ? "yes" : "no"}</td>`;
  if (typeof value === "number") {
    return `<td class="num">${TH_MONEY.test(column) ? fmt$(value) : value.toLocaleString()}</td>`;
  }
  // Families are written machine-side (board_backed_strategic); everything else
  // is a name or a date and must survive intact.
  const text = column === "family" ? String(value).replace(/_/g, " ") : String(value);
  return `<td class="${column === "symbol" ? "mono" : "dim"}">${escapeHtml(text)}</td>`;
}

async function thRunCandidates() {
  const src = thSource();
  if (!src) return;
  const box = $("th-cands");
  box.innerHTML = `<div class="empty">Scanning… a cold run downloads source data and can take a minute.</div>`;
  $("th-cand-warn").innerHTML = "";
  try {
    // The funnel is a registry command, so it takes the source's params but
    // not the source name itself.
    const qs = thParams();
    qs.delete("source");
    const d = await api(`/api/v1${src.command}?${qs}`);
    if (d.warnings && d.warnings.length)
      $("th-cand-warn").innerHTML = `<div class="explain">${escapeHtml(d.warnings[0])}</div>`;
    const rows = d.results || [];
    if (!rows.length) { box.innerHTML = `<div class="empty">Nothing met the gate.</div>`; return; }

    // Columns are whatever this source emitted — the scanner already decided
    // what is worth showing, so a new funnel needs no table of its own.
    const cols = Object.keys(rows[0]).filter((c) => c !== "action");
    box.innerHTML = `<table><thead><tr>` +
      cols.map((c) => `<th${typeof rows[0][c] === "number" ? ` class="num"` : ""}>${
        escapeHtml(c.replace(/_/g, " "))}</th>`).join("") +
      `</tr></thead><tbody>` +
      rows.map((r) => `<tr class="clickrow" data-sym="${escapeHtml(r.symbol || "")}">` +
        cols.map((c) => thCell(c, r[c])).join("") + `</tr>`).join("") +
      `</tbody></table>`;
    box.querySelectorAll("tr.clickrow").forEach((tr) => {
      if (!tr.dataset.sym) return;
      tr.style.cursor = "pointer";
      tr.onclick = () => { $("th-act-symbol").value = tr.dataset.sym; thShowActivity(tr.dataset.sym); };
    });
  } catch (e) {
    box.innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
}

// ---------- sector thesis generation ----------
// This is a separate workspace, but it feeds the same graded thesis spine.
// The source registry owns the split, so adding another sector funnel later
// only requires registering it with universe="sectors" on the backend.
let thsLoaded = false;
let thsSources = null;

async function loadSectorThesisView() {
  if (thsLoaded) return;
  thsLoaded = true;
  await thsLoadSources();
  thsLoadTheses();
  thsTriageStatus();
}

function thsSource() {
  if (!thsSources) return null;
  return thsSources.sources.find((s) => s.name === $("ths-source").value) || null;
}

async function thsLoadSources() {
  try {
    thsSources = await api("/api/theses/triage/sources?universe=sectors");
  } catch (e) {
    $("ths-cand-note").textContent = e.message;
    return;
  }
  $("ths-source").innerHTML = thsSources.sources.map((s) =>
    `<option value="${escapeHtml(s.name)}"${s.name === thsSources.default ? " selected" : ""}>${
      escapeHtml(s.label)}</option>`).join("");
  $("ths-source").onchange = thsRenderSource;
  thsRenderSource();
}

function thsRenderSource() {
  const src = thsSource();
  if (!src) {
    $("ths-cand-note").textContent = "No sector idea sources are registered.";
    return;
  }
  $("ths-cand-note").textContent = src.scope;
  $("ths-params").innerHTML = Object.entries(src.params || {}).map(([name, spec]) => {
    const id = `ths-p-${name}`;
    const label = escapeHtml(name.replace(/_/g, " "));
    const title = escapeHtml(spec.help || "");
    if (spec.kind === "bool") {
      return `<label class="panel-note" style="margin-left:0" title="${title}">
        <input type="checkbox" id="${id}"${spec.default ? " checked" : ""} /> ${label}</label>`;
    }
    const bounds = (spec.min == null ? "" : ` min="${spec.min}"`) +
                   (spec.max == null ? "" : ` max="${spec.max}"`);
    return `<label class="panel-note" style="margin-left:0" title="${title}">${label}
      <input type="${spec.kind === "str" ? "text" : "number"}" id="${id}" class="mono"
        style="width:120px;margin-left:6px"
        value="${spec.default == null ? "" : escapeHtml(String(spec.default))}"${bounds} /></label>`;
  }).join("");
  $("ths-cand-warn").innerHTML = "";
  $("ths-cands").innerHTML = `<div class="empty">Press “Find sector candidates” to scan.</div>`;
}

function thsParams() {
  const src = thsSource();
  const qs = new URLSearchParams();
  if (!src) return qs;
  qs.set("source", src.name);
  for (const [name, spec] of Object.entries(src.params || {})) {
    const el = $(`ths-p-${name}`);
    if (!el) continue;
    qs.set(name, spec.kind === "bool" ? String(el.checked) : el.value);
  }
  return qs;
}

async function thsTriageStatus() {
  const note = $("ths-triage-note");
  const btn = $("ths-triage-run");
  try {
    const s = await api("/api/theses/triage/status");
    btn.disabled = !s.enabled;
    note.textContent = s.enabled ? `model: ${s.model || "ready"}` : (s.reason || "switched off");
  } catch (e) {
    btn.disabled = true;
    note.textContent = e.message;
  }
}

const THS_PERCENT_COLUMNS = new Set([
  "one_month", "three_month", "ytd", "one_year", "relative_one_month",
  "relative_three_month", "relative_ytd", "relative_one_year",
]);

function thsCell(column, value) {
  if (value == null || value === "") return `<td class="dim"></td>`;
  if (THS_PERCENT_COLUMNS.has(column)) {
    return `<td class="num ${cls(value)}">${fmtPct(value)}</td>`;
  }
  return thCell(column, value);
}

async function thsRunCandidates() {
  const src = thsSource();
  if (!src) return;
  const box = $("ths-cands");
  box.innerHTML = `<div class="empty">Comparing the 11 sector ETFs with SPY…</div>`;
  $("ths-cand-warn").innerHTML = "";
  try {
    const qs = thsParams();
    qs.delete("source");
    const d = await api(`/api/v1${src.command}?${qs}`);
    if (d.warnings && d.warnings.length) {
      $("ths-cand-warn").innerHTML = `<div class="explain">${
        d.warnings.map((w) => escapeHtml(String(w))).join("<br>")}</div>`;
    }
    const rows = d.results || [];
    if (!rows.length) {
      box.innerHTML = `<div class="empty">No sector performance was available.</div>`;
      return;
    }
    const wanted = ["sector", "symbol", "family", "one_month", "three_month",
      "relative_three_month", "ytd", "relative_ytd", "one_year", "relative_one_year"];
    const cols = wanted.filter((c) => Object.prototype.hasOwnProperty.call(rows[0], c));
    box.innerHTML = `<table><thead><tr>${cols.map((c) =>
      `<th${THS_PERCENT_COLUMNS.has(c) ? ` class="num"` : ""}>${
        escapeHtml(c.replace(/_/g, " "))}</th>`).join("")}</tr></thead><tbody>` +
      rows.map((r, i) => `<tr class="clickrow" data-row="${i}" style="cursor:pointer">${
        cols.map((c) => thsCell(c, r[c])).join("")}</tr>`).join("") +
      `</tbody></table>`;
    box.querySelectorAll("tr.clickrow").forEach((tr) => {
      tr.onclick = () => openSector({
        group: rows[+tr.dataset.row].sector,
        symbol: rows[+tr.dataset.row].symbol,
        one_month: rows[+tr.dataset.row].one_month,
        three_month: rows[+tr.dataset.row].three_month,
        ytd: rows[+tr.dataset.row].ytd,
        one_year: rows[+tr.dataset.row].one_year,
      }, "thesis-sectors");
    });
  } catch (e) {
    box.innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
}

async function thsTriageRun() {
  const box = $("ths-triage");
  const btn = $("ths-triage-run");
  btn.disabled = true;
  box.innerHTML = `<div class="empty">Testing whether the rotation has a defensible mechanism…</div>`;
  try {
    const d = await api(`/api/theses/triage?${thsParams()}`, { method: "POST" });
    thsRenderTriage(d);
  } catch (e) {
    box.innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
  btn.disabled = false;
}

function thsRenderTriage(d) {
  const box = $("ths-triage");
  const cands = d.candidates || [];
  if (!cands.length) {
    box.innerHTML = `<div class="empty">${escapeHtml(
      d.note || "The model promoted nothing — which is a valid answer.")}</div>`;
    return;
  }
  const ran = thsSources && thsSources.sources.find((s) => s.name === d.source);
  const srcTag = (leg) => leg.source === "world_knowledge"
    ? `<span class="badge">needs verifying</span>` : `<span class="dim">from the data</span>`;
  box.innerHTML =
    `<div class="explain">${ran ? `<strong>${escapeHtml(ran.label)}</strong> · ` : ""}${
      escapeHtml(d.disclaimer || "")}</div>` +
    cands.map((c, i) => `
      <div class="panel" style="margin-top:10px">
        <div class="panel-h">
          <span class="mono">${escapeHtml(c.symbol)}</span>
          <span class="${c.promote ? "pos" : "dim"}" style="margin-left:10px">${
            c.promote ? "PROMOTED" : "passed over"}</span>
          <span class="${c.direction === "long" ? "pos" : c.direction === "short" ? "neg" : "dim"}"
            style="margin-left:10px">${escapeHtml((c.direction || "neutral").toUpperCase())}</span>
          <span class="panel-note">confidence ${escapeHtml(c.confidence || "?")} · proxy-artifact risk ${
            escapeHtml(c.calendar_artifact_risk || "?")}</span>
          ${c.promote ? `<button class="ghost panel-btn" data-ths-dive="${i}">Deep dive → draft thesis</button>` : ""}
        </div>
        ${c.claim_sketch ? `<p class="explain">${escapeHtml(c.claim_sketch)}</p>` : ""}
        <p class="explain dim">${escapeHtml(c.reason || "")}</p>
        ${(c.legs || []).map((leg) => `
          <div class="explain" style="margin:4px 0 0 12px">
            • ${escapeHtml(leg.claim)} ${srcTag(leg)}
            ${leg.rejected ? `<span class="neg"> — rejected: ${escapeHtml(leg.rejected)}</span>` : ""}
            ${leg.if_absent ? `<div class="dim" style="margin-left:14px">if nothing found: ${
              escapeHtml(leg.if_absent)}</div>` : ""}
            ${(leg.verify_with || []).map((v) => `
              <div class="mono dim" style="margin-left:14px">check: ${escapeHtml(v.path)}${
                v.unknown_command ? ` <span class="neg">(not a real command)</span>` : ""} — ${
                escapeHtml(v.expect || "")}</div>`).join("")}
          </div>`).join("")}
        <div class="msg" id="ths-dive-msg-${i}"></div>
      </div>`).join("");
  box.querySelectorAll("[data-ths-dive]").forEach((btn) => {
    btn.onclick = () => thsDeepDive(cands[+btn.dataset.thsDive], +btn.dataset.thsDive, btn);
  });
}

async function thsDeepDive(candidate, i, btn) {
  const msg = $(`ths-dive-msg-${i}`);
  btn.disabled = true;
  msg.textContent = "Trying to refute each leg with live data…";
  try {
    const d = await api("/api/theses/deepdive?create_draft=true", {
      method: "POST",
      body: {
        symbol: candidate.symbol,
        direction: candidate.direction || "neutral",
        idea_source: candidate.idea_source || (thsSource() && thsSource().name),
        legs: candidate.legs || [],
      },
    });
    if (d.draft_thesis_id) {
      msg.innerHTML = `<span class="pos">Draft sector thesis created</span> — ${
        d.evidence_frozen || 0} evidence snapshot(s), ${d.checks_installed || 0} falsifier(s). ` +
        `<button class="linkbtn" data-open-thesis="${d.draft_thesis_id}">Review the draft →</button>`;
      msg.querySelector("[data-open-thesis]").onclick = () => thsOpenThesis(d.draft_thesis_id);
      await thsLoadTheses();
    } else {
      msg.innerHTML = `<span class="dim">The model chose not to proceed:</span> ${
        escapeHtml((d.dossier && d.dossier.summary) || "no summary")}`;
    }
  } catch (e) {
    msg.textContent = e.message;
  }
  btn.disabled = false;
}

async function thsLoadTheses() {
  const box = $("ths-list");
  try {
    const all = await api("/api/theses");
    const sourceNames = new Set((thsSources && thsSources.sources || []).map((s) => s.name));
    const rows = all.filter((t) => sourceNames.has(t.source));
    if (!rows.length) {
      box.innerHTML = `<div class="empty">No sector theses yet — promote a setup above to create a draft.</div>`;
      return;
    }
    box.innerHTML = `<table><thead><tr>
        <th>Title</th><th>ETF</th><th>Direction</th><th>Status</th><th>Review by</th>
      </tr></thead><tbody>` + rows.map((t) => `
      <tr class="clickrow" data-id="${t.id}" style="cursor:pointer">
        <td>${t.reviewed_at ? "" : `<span class="chip warn">draft</span> `}${escapeHtml(t.title)}</td>
        <td class="mono">${escapeHtml(t.symbols || "")}</td>
        <td class="${t.direction === "long" ? "pos" : t.direction === "short" ? "neg" : "dim"}">${
          escapeHtml(t.direction || "neutral")}</td>
        <td class="${TH_STATUS[t.status] || "dim"}">${escapeHtml(t.status)}</td>
        <td class="mono dim">${t.review_by ? escapeHtml(String(t.review_by).slice(0, 10)) : "—"}</td>
      </tr>`).join("") + `</tbody></table>`;
    box.querySelectorAll("tr.clickrow").forEach((tr) => {
      tr.onclick = () => thsOpenThesis(+tr.dataset.id);
    });
  } catch (e) {
    box.innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
}

async function thsOpenThesis(id) {
  document.querySelector('.navbtn[data-view="thesis"]').click();
  await thShowThesis(id);
  $("th-detail").scrollIntoView({ behavior: "smooth" });
}

async function thShowActivity(sym) {
  sym = (sym || $("th-act-symbol").value || "").trim().toUpperCase();
  if (!sym) return;
  const box = $("th-activity");
  box.innerHTML = `<div class="empty">Reading ${escapeHtml(sym)}'s Form 4 filings…</div>`;
  $("th-holders").innerHTML = `<div class="empty">Checking watched funds…</div>`;
  $("th-congress").innerHTML = `<div class="empty">Reading Senate disclosures…</div>`;
  try {
    const d = await api(`/api/v1/thesis/insider_activity?symbol=${encodeURIComponent(sym)}&days=400`);
    const x = d.extra || {};
    const gate = x.meets_calibrated_gate
      ? `<span class="pos">meets the attention gate</span>`
      : `<span class="dim">does not meet the attention gate</span>`;
    $("th-act-note").innerHTML = `${escapeHtml(sym)} · bought ${fmt$(x.buy_value || 0)} · sold ${fmt$(x.sell_value || 0)} · ${gate}`;
    box.innerHTML = `<table><thead><tr>
        <th>Date</th><th>Who</th><th>Role</th><th>Side</th><th class="num">Value</th><th>Plan?</th>
      </tr></thead><tbody>` + (d.results || []).map((r) => `
      <tr>
        <td class="mono">${escapeHtml(r.trade_date)}</td>
        <td>${escapeHtml(r.owner)}</td>
        <td class="dim">${escapeHtml(r.role)}</td>
        <td class="${r.side === "buy" ? "pos" : "neg"}">${r.side}</td>
        <td class="num">${fmt$(r.value)}</td>
        <td class="dim">${r.on_10b5_1_plan === true ? "10b5-1" : r.on_10b5_1_plan === false ? "no" : "?"}</td>
      </tr>`).join("") + `</tbody></table>`;
  } catch (e) {
    box.innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
  try {
    const h = await api(`/api/v1/thesis/notable_holders?symbol=${encodeURIComponent(sym)}`);
    $("th-holders").innerHTML = `<table><thead><tr>
        <th>Fund</th><th>Type</th><th>Holds it?</th><th class="num">Value</th><th>As of</th>
      </tr></thead><tbody>` + (h.results || []).map((r) => `
      <tr>
        <td>${escapeHtml(r.fund)}</td>
        <td class="dim">${escapeHtml(r.kind || "")}</td>
        <td>${r.holds === true ? `<span class="pos">yes</span>` : r.holds === false ? `<span class="dim">no</span>` : `<span class="dim">${escapeHtml(r.note || "n/a")}</span>`}</td>
        <td class="num">${r.value_usd ? fmt$(r.value_usd) : ""}</td>
        <td class="mono dim">${escapeHtml(r.period || "")}</td>
      </tr>`).join("") + `</tbody></table>`;
  } catch (e) {
    $("th-holders").innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
  try {
    const c = await api(`/api/v1/thesis/congress_trades?symbol=${encodeURIComponent(sym)}&days=365`);
    // The bracket is printed whole. Collapsing it to a midpoint would put a
    // number on screen that nobody disclosed and nobody traded.
    const bracket = (r) => r.amount_low == null ? "—"
      : r.amount_high == null ? `over ${fmt$(r.amount_low)}`
      : `${fmt$(r.amount_low)}–${fmt$(r.amount_high)}`;
    $("th-congress").innerHTML = `<table><thead><tr>
        <th>Filed</th><th>Traded</th><th>Member</th><th>Account</th><th>Side</th>
        <th class="num">Disclosed range</th><th></th>
      </tr></thead><tbody>` + (c.results || []).map((r) => `
      <tr>
        <td class="mono">${escapeHtml(String(r.filing_date || "").slice(0, 10))}</td>
        <td class="mono dim">${escapeHtml(String(r.transaction_date || "").slice(0, 10))}</td>
        <td>${escapeHtml(r.member || "")}</td>
        <td class="dim">${escapeHtml(r.owner || "")}</td>
        <td class="${r.side === "buy" ? "pos" : r.side === "sell" ? "neg" : "dim"}">${escapeHtml(r.side || "")}</td>
        <td class="num">${bracket(r)}</td>
        <td>${r.filing_url ? `<a href="${escapeHtml(r.filing_url)}" target="_blank" rel="noopener" class="dim">filing</a>` : ""}</td>
      </tr>`).join("") + `</tbody></table>`;
  } catch (e) {
    // Silence is the norm here: the Senate is 100 of 535 members, so nobody
    // disclosing this name says nothing about whether anybody traded it.
    $("th-congress").innerHTML = `<div class="empty">${escapeHtml(e.message)} — Senate coverage only, so this is not evidence that nobody traded it.</div>`;
  }
}

const TH_STATUS = { open: "dim", supported: "pos", broken: "neg", expired: "dim", closed: "dim" };

async function thLoadTheses() {
  try {
    const rows = await api("/api/theses");
    const box = $("th-list");
    if (!rows.length) { box.innerHTML = `<div class="empty">No theses yet — create one above, straight from a candidate.</div>`; return; }
    box.innerHTML = `<table><thead><tr>
        <th>Title</th><th>Symbols</th><th>Status</th><th>Review by</th><th>Made</th>
      </tr></thead><tbody>` + rows.map((t) => `
      <tr class="clickrow" data-id="${t.id}" style="cursor:pointer">
        <td>${t.reviewed_at ? "" : `<span class="chip warn" title="No human has reviewed this yet">draft</span> `}${escapeHtml(t.title)}</td>
        <td class="mono">${escapeHtml(t.symbols || "")}</td>
        <td class="${TH_STATUS[t.status] || "dim"}">${escapeHtml(t.status)}</td>
        <td class="mono dim">${t.review_by ? escapeHtml(String(t.review_by).slice(0, 10)) : "—"}</td>
        <td class="dim">${timeAgo(t.created_at)}</td>
      </tr>`).join("") + `</tbody></table>`;
    box.querySelectorAll("tr.clickrow").forEach((tr) => { tr.onclick = () => thShowThesis(+tr.dataset.id); });
  } catch (e) {
    $("th-list").innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
}

async function thShowThesis(id) {
  thCurrentId = id;
  try {
    const t = await api(`/api/theses/${id}`);
    $("th-detail").style.display = "";
    $("th-d-title").textContent = t.title.toUpperCase();
    $("th-d-claim").textContent = t.claim;
    $("th-d-meta").innerHTML =
      `status <span class="${TH_STATUS[t.status] || "dim"}">${escapeHtml(t.status)}</span>` +
      ` · direction <span class="${t.direction === "long" ? "pos" : t.direction === "short" ? "neg" : "dim"}">${escapeHtml(t.direction || "neutral")}</span>` +
      (t.review_by ? ` · review by ${escapeHtml(String(t.review_by).slice(0, 10))}` : "") +
      (t.source ? ` · from ${escapeHtml(t.source)}` : "") +
      (t.reviewed_at
        ? ` · <span class="dim">reviewed ${timeAgo(t.reviewed_at)}</span>`
        : ` · <span class="warn">unreviewed — a machine proposed this and nobody has checked it</span>`) +
      (t.outcome_note ? ` · ${escapeHtml(t.outcome_note)}` : "");
    // Grading is unaffected either way: review decides whether the thesis is
    // yours, never whether it counts.
    $("th-d-review").textContent = t.reviewed_at ? "Mark unreviewed" : "Mark reviewed";
    $("th-d-review").dataset.reviewed = t.reviewed_at ? "1" : "";
    $("th-d-checks").innerHTML = (t.checks || []).length
      ? `<table><thead><tr><th>Check</th><th>Watches</th><th>Breaks if</th><th class="num">Last value</th><th>Status</th></tr></thead><tbody>` +
        t.checks.map((c) => `
        <tr>
          <td>${escapeHtml(c.name)}</td>
          <td class="mono dim">${escapeHtml(c.command_path)} → ${escapeHtml(c.field)}</td>
          <td class="mono">${escapeHtml(c.comparator)} ${c.threshold}</td>
          <td class="num">${c.last_value != null ? (+c.last_value).toLocaleString("en-US", { maximumFractionDigits: 2 }) : "—"}</td>
          <td class="${c.status === "broken" ? "neg" : c.status === "holding" ? "pos" : "dim"}">${escapeHtml(c.status)}${c.last_error ? ` <span class="dim" title="${escapeHtml(c.last_error)}">⚠</span>` : ""}</td>
        </tr>`).join("") + `</tbody></table>`
      : `<div class="empty">No checks yet — a thesis without a way to be wrong is just an opinion.</div>`;
    $("th-d-evidence").innerHTML = (t.evidence || []).length
      ? `<table><thead><tr><th>Leg</th><th>Command</th><th class="num">Rows</th><th>Frozen</th></tr></thead><tbody>` +
        t.evidence.map((e) => `
        <tr>
          <td>${escapeHtml(e.leg || "—")}</td>
          <td class="mono dim">${escapeHtml(e.command_path)}</td>
          <td class="num">${e.row_count}</td>
          <td class="dim">${timeAgo(e.as_of)}</td>
        </tr>`).join("") + `</tbody></table>`
      : `<div class="empty">Nothing frozen yet — snapshot the data your claim rests on.</div>`;
  } catch (e) {
    setStatus(e.message);
  }
}

function thBindOnce() {
  $("th-run").onclick = thRunCandidates;
  $("th-triage-run").onclick = thTriageRun;
  $("th-act-run").onclick = () => thShowActivity();
  $("th-act-symbol").onkeydown = (ev) => { if (ev.key === "Enter") thShowActivity(); };
  $("th-refresh").onclick = thLoadTheses;
  $("ths-run").onclick = thsRunCandidates;
  $("ths-triage-run").onclick = thsTriageRun;
  $("ths-refresh").onclick = thsLoadTheses;

  $("th-create").onclick = async () => {
    const title = $("th-new-title").value.trim();
    const claim = $("th-new-claim").value.trim();
    if (!title || !claim) { $("th-msg").textContent = "A thesis needs a title and a falsifiable claim."; return; }
    const days = parseInt($("th-new-days").value, 10);
    const body = {
      title, claim,
      symbols: $("th-new-symbol").value.trim().toUpperCase(),
    };
    if (days > 0) { const d = new Date(); d.setDate(d.getDate() + days); body.review_by = d.toISOString(); }
    try {
      const t = await api("/api/theses", { method: "POST", body });
      $("th-msg").textContent = "";
      ["th-new-title", "th-new-claim", "th-new-symbol", "th-new-days"].forEach((i) => { $(i).value = ""; });
      await thLoadTheses();
      thShowThesis(t.id);
    } catch (e) { $("th-msg").textContent = e.message; }
  };

  $("th-d-evaluate").onclick = async () => {
    if (!thCurrentId) return;
    try { await api(`/api/theses/${thCurrentId}/evaluate`, { method: "POST" }); } catch (e) { setStatus(e.message); }
    thShowThesis(thCurrentId);
    thLoadTheses();
  };

  $("th-d-review").onclick = async () => {
    if (!thCurrentId) return;
    const reviewed = !$("th-d-review").dataset.reviewed;
    try { await api(`/api/theses/${thCurrentId}`, { method: "PATCH", body: { reviewed } }); }
    catch (e) { setStatus(e.message); return; }
    thShowThesis(thCurrentId);
    thLoadTheses();
  };

  $("th-d-delete").onclick = async () => {
    if (!thCurrentId || !confirm("Delete this thesis and its record?")) return;
    try { await api(`/api/theses/${thCurrentId}`, { method: "DELETE" }); } catch (e) { setStatus(e.message); }
    $("th-detail").style.display = "none";
    thCurrentId = null;
    thLoadTheses();
  };

  $("th-c-add").onclick = async () => {
    if (!thCurrentId) return;
    let params = {};
    try { params = $("th-c-params").value.trim() ? JSON.parse($("th-c-params").value) : {}; }
    catch { setStatus("Check parameters must be JSON, e.g. {\"symbol\":\"MGM\"}"); return; }
    const body = {
      name: $("th-c-name").value.trim() || "check",
      command_path: $("th-c-path").value.trim(),
      parameters: params,
      field: $("th-c-field").value.trim(),
      comparator: $("th-c-cmp").value,
      threshold: parseFloat($("th-c-threshold").value),
    };
    // The server runs the check before storing it. A field it cannot read is
    // refused outright; one that is already true is refused until you say you
    // meant it, because such a check breaks the thesis on the next sweep.
    const post = (qs = "") => api(`/api/theses/${thCurrentId}/checks${qs}`, { method: "POST", body });
    try {
      try {
        await post();
      } catch (e) {
        if (!/allow_breached=true/.test(e.message)) throw e;
        if (!confirm(`${e.message.replace(/ — pass allow_breached=true.*/, "")}\n\nStore it anyway? The thesis will read as broken at the next check.`)) return;
        await post("?allow_breached=true");
      }
      ["th-c-name", "th-c-path", "th-c-params", "th-c-field", "th-c-threshold"].forEach((i) => { $(i).value = ""; });
      thShowThesis(thCurrentId);
    } catch (e) { setStatus(e.message); }
  };

  $("th-e-add").onclick = async () => {
    if (!thCurrentId) return;
    let params = {};
    try { params = $("th-e-params").value.trim() ? JSON.parse($("th-e-params").value) : {}; }
    catch { setStatus("Evidence parameters must be JSON."); return; }
    try {
      await api(`/api/theses/${thCurrentId}/evidence`, { method: "POST", body: {
        command_path: $("th-e-path").value.trim(),
        parameters: params,
        leg: $("th-e-leg").value.trim() || null,
      }});
      ["th-e-path", "th-e-params", "th-e-leg"].forEach((i) => { $(i).value = ""; });
      thShowThesis(thCurrentId);
    } catch (e) { setStatus(e.message); }
  };
}
thBindOnce();

// --------------------------------------------------------------------------
// Hedge panel (docs/hedge-construction.md step 5)
// --------------------------------------------------------------------------
//: The analysis whose candidates the Record buttons refer to.
let hgLast = null;

const HG_KINDS = {
  protective_put: "Protective put",
  put_spread: "Put spread",
  collar: "Collar",
  short_etf: "Short index ETF",
  // Named for what it is. An overwrite sells premium; it is not protection,
  // and the simulator says so beside it.
  covered_call: "Covered call (overwrite)",
};

function hgInstruments() {
  const picked = [];
  if ($("hg-i-put").checked) picked.push("protective_put");
  if ($("hg-i-spread").checked) picked.push("put_spread");
  if ($("hg-i-collar").checked) picked.push("collar");
  if ($("hg-i-short").checked) picked.push("short_etf");
  return picked;
}

async function hgRun() {
  if (!pfId) return;
  const button = $("hg-run");
  button.disabled = true;
  $("hg-verdict").innerHTML = "";
  $("hg-notes").innerHTML = "";
  $("hg-rows").innerHTML = `<div class="empty">Pricing chains and replaying shocks…</div>`;
  try {
    const d = await api(`/api/portfolios/${pfId}/hedge/analyze`, {
      method: "POST",
      body: {
        horizon_days: Number($("hg-horizon").value),
        target_reduction_fraction: Number($("hg-fraction").value),
        instruments: hgInstruments(),
      },
    });
    hgRender(d);
    setStatus("HEDGE ANALYSIS COMPLETE");
  } catch (e) {
    $("hg-rows").innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
    hgScenario("hg", {});
    setStatus("ERR: " + e.message);
  } finally {
    button.disabled = false;
  }
}

function hgRender(d) {
  hgLast = d;
  const sell = d.verdict.action === "de_risk_by_selling";
  const target = d.target;
  $("hg-verdict").innerHTML =
    `<div class="kv"><div class="r"><span class="k">Tail loss now (${fmtPct(1 - d.request.var_level)} confidence, ` +
    `${d.request.horizon_days} sessions)</span><span class="v neg">${fmt$(Math.abs(target.cvar_unhedged))}</span></div>` +
    `<div class="r"><span class="k">Looking to remove</span><span class="v">${fmt$(target.reduction_sought)}</span></div>` +
    `<div class="r"><span class="k">Verdict</span><span class="v ${sell ? "neg" : "pos"}">` +
    (sell ? "Sell, don't hedge" : `Hedge — ${escapeHtml(HG_KINDS[d.verdict.best_candidate] || d.verdict.best_candidate)}, ${escapeHtml(d.verdict.size)}`) +
    `</span></div></div>` +
    (d.verdict.reason ? `<p class="explain">${escapeHtml(d.verdict.reason)}.</p>` : "");

  $("hg-rows").innerHTML = d.rows.length
    ? `<table><tr><th>hedge</th><th>size</th><th>reaches goal?</th><th>protection</th>` +
      `<th>cost</th><th>cost per $1 protection</th><th>if it rallies 10%</th><th>residual beta $</th><th></th></tr>` +
      d.rows.map((r, i) => {
        const size = r.quantity != null ? `${r.quantity} contracts` : fmt$(r.notional);
        const ci = r.protection_bps_ci95;
        const ratio = r.cost_per_unit_protection;
        return `<tr><td>${escapeHtml(HG_KINDS[r.kind] || r.kind)} <span class="dim">${escapeHtml(r.underlying)}</span></td>` +
          `<td class="mono">${size}</td>` +
          `<td class="${r.meets_target ? "pos" : "neg"}">${r.meets_target ? "yes" : "no"}</td>` +
          `<td class="mono">${r.protection_bps.toFixed(0)} bps <span class="dim">(${ci[0].toFixed(0)}–${ci[1].toFixed(0)})</span></td>` +
          `<td class="mono ${cls(-r.cost_bps)}">${r.cost_bps.toFixed(0)} bps</td>` +
          `<td class="mono">${ratio == null ? "-" : ratio.toFixed(2)}</td>` +
          `<td class="mono ${cls(r.upside_loss["+10%"])}">${fmt$(r.upside_loss["+10%"])}</td>` +
          `<td class="mono">${r.residual_beta_dollars == null ? "-" : fmt$(r.residual_beta_dollars)}</td>` +
          `<td><button class="ghost panel-btn hg-rec" data-i="${i}">Record</button></td></tr>`;
      }).join("") + "</table>"
    : `<div class="empty">No hedge could be built from live quotes.</div>`;
  $("hg-rows").querySelectorAll("button.hg-rec").forEach((b) => {
    b.onclick = () => hgRecord(Number(b.dataset.i));
  });

  const caveats = [];
  caveats.push(`Shocks: ${d.shocks.windows} overlapping ${d.shocks.horizon_days}-session windows ` +
    `(${d.shocks.independent_windows} genuinely independent) from ${d.shocks.period[0]} to ${d.shocks.period[1]}.`);
  caveats.push(d.shocks.vol_symbol
    ? `Volatility moves with the market via ${escapeHtml(d.shocks.vol_symbol)}.`
    : `No volatility index available — option protection here is understated.`);
  if (d.shocks.fallback_symbols.length) {
    caveats.push(`Estimated from beta (too little history): ${d.shocks.fallback_symbols.map(escapeHtml).join(", ")}.`);
  }
  (d.excluded || []).forEach((x) => {
    caveats.push(`Skipped ${escapeHtml(HG_KINDS[x.kind] || x.kind)}: ${escapeHtml(x.reason)}.`);
  });
  (d.warnings || []).forEach((w) => caveats.push(escapeHtml(w)));
  d.rows.filter((r) => r.granularity_warning).forEach((r) => {
    caveats.push(escapeHtml(r.granularity_warning));
  });
  $("hg-notes").innerHTML = caveats.map((c) => `<div>· ${c}</div>`).join("");
  hgScenario("hg", d);
}

$("hg-run").onclick = hgRun;

// --- The picture the table cannot draw --------------------------------------
// Every candidate is ranked on one number — cost per dollar of tail removed —
// and that number is an average over the worst windows. It cannot say where
// protection starts, where a put spread stops paying, or what the structure
// costs if the move never comes. The curve says all three at a glance.
//
// Display only, and drawn last on purpose: the engine ranked these rows before
// anything reached a canvas, so nothing here can flatter a candidate into a
// position (docs/hedge-construction.md — the grid communicates, never ranks).
const HG_SCEN = { exposure: "#82868a", frozen: "#5ac8fa", paired: "#00c805" };

//: Signed percent with no false precision: -30% stays −30%, -12.5% keeps its half.
function hgPct(x) {
  const v = Math.abs(Math.round(x * 1000) / 10);
  const sign = x > 0 ? "+" : Math.round(x * 1000) < 0 ? "−" : "";
  return `${sign}${Number.isInteger(v) ? v : v.toFixed(1)}%`;
}

//: A loss reads "−$1,200", not "$-1,200"; a rounded-away cent is not a loss.
const hgMoney = (v) => `${v < -0.5 ? "−" : ""}${fmt$(Math.abs(v))}`;

// The dashed vertical marks where the historical tail begins — the only
// probabilistic fact on the chart, and labelled as history rather than forecast.
const hgTailMarker = {
  id: "hgTail",
  afterDatasetsDraw(chart, _args, opts) {
    if (!opts || opts.index == null) return;
    const { ctx, chartArea: area, scales } = chart;
    const x = scales.x.getPixelForValue(opts.index);
    if (!(x >= area.left && x <= area.right)) return;
    ctx.save();
    ctx.strokeStyle = "#ff5000";
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(x, area.top);
    ctx.lineTo(x, area.bottom);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#ff5000";
    ctx.font = "10px 'IBM Plex Mono', monospace";
    const flip = x > area.right - 70;
    ctx.textAlign = flip ? "right" : "left";
    ctx.fillText(opts.label, x + (flip ? -5 : 5), area.top + 10);
    ctx.restore();
  },
};

function hgScenario(prefix, data) {
  const wrap = $(`${prefix}-scenario`);
  const drawable = (data.rows || []).filter((r) => r.scenario && r.scenario.points.length);
  const id = `${prefix}-chart`;
  if (charts[id]) { charts[id].destroy(); delete charts[id]; }
  if (!drawable.length) { wrap.style.display = "none"; return; }

  wrap.style.display = "";
  const picker = $(`${prefix}-scen-picker`);
  picker.innerHTML = drawable.map((row, i) =>
    `<button class="chip sm${i ? "" : " active"}" data-scen="${i}">` +
    `${escapeHtml(HG_KINDS[row.kind] || row.kind)}</button>`).join("");
  picker.querySelectorAll("[data-scen]").forEach((button) => {
    button.onclick = () => {
      picker.querySelectorAll("[data-scen]").forEach((b) => b.classList.remove("active"));
      button.classList.add("active");
      hgDrawScenario(prefix, data, drawable[Number(button.dataset.scen)]);
    };
  });
  hgDrawScenario(prefix, data, drawable[0]);
}

function hgDrawScenario(prefix, data, row) {
  const scen = row.scenario, points = scen.points;
  // The paired-vol line exists only where a vol index was available. Where it
  // is missing the chart must not draw a flat-IV line as if vol had been
  // measured and found still — the caption says so instead.
  const paired = points.every((p) => p.hedged_pnl_iv != null);
  const exposure = scen.exposure === "portfolio" ? "Portfolio" : scen.exposure;
  const axisTicks = { color: "#6f7377", font: { family: "'IBM Plex Mono', monospace", size: 10 } };

  const series = [
    { label: `${exposure}, unhedged`, values: points.map((p) => p.exposure_pnl),
      color: HG_SCEN.exposure, dash: true },
    { label: "Hedged, vol unchanged", values: points.map((p) => p.hedged_pnl),
      color: HG_SCEN.frozen },
  ];
  if (paired) {
    series.push({ label: "Hedged, vol moving with the market",
      values: points.map((p) => p.hedged_pnl_iv), color: HG_SCEN.paired });
  }

  let tailIndex = null;
  if (scen.tail_shock != null) {
    tailIndex = points.reduce((best, p, i) =>
      Math.abs(p.shock - scen.tail_shock) < Math.abs(points[best].shock - scen.tail_shock) ? i : best, 0);
  }

  const id = `${prefix}-chart`;
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart($(id), {
    type: "line",
    data: {
      labels: points.map((p) => hgPct(p.shock)),
      datasets: series.map((s) => ({
        label: s.label, data: s.values, borderColor: s.color, borderWidth: 1.8,
        pointRadius: 0, tension: 0, borderDash: s.dash ? [5, 4] : [], fill: false,
      })),
    },
    options: {
      animation: false, maintainAspectRatio: false, responsive: true,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { labels: { color: "#f5f6f7", boxWidth: 14, font: { size: 11 } } },
        tooltip: {
          callbacks: {
            // Canvas text, not markup — escaping here would print the entity.
            title: (items) => `${scen.underlying} ${items[0].label} at ${scen.horizon_date}`,
            label: (item) => `${item.dataset.label}: ${hgMoney(item.parsed.y)}`,
          },
        },
        hgTail: tailIndex == null ? {} : {
          index: tailIndex,
          label: `${hgPct(scen.tail_shock)} — the historical tail`,
        },
      },
      scales: {
        x: { ticks: { ...axisTicks, maxTicksLimit: 9 }, grid: { color: "#191b1c" } },
        y: {
          ticks: { ...axisTicks, callback: (v) => hgMoney(v) },
          // Break-even is the line the eye should find first.
          grid: { color: (c) => (c.tick.value === 0 ? "#4a4e50" : "#191b1c") },
        },
      },
    },
    plugins: [hgTailMarker],
  });

  hgScenarioTable(prefix, data, scen, paired);
}

// The three or four moves worth reading off the curve, as numbers. A chart
// shows the shape; this says what the shape is worth in dollars.
function hgScenarioTable(prefix, data, scen, paired) {
  const points = scen.points;
  const at = (target) => points.reduce((best, p) =>
    Math.abs(p.shock - target) < Math.abs(best.shock - target) ? p : best, points[0]);
  const hedged = (p) => (paired ? p.hedged_pnl_iv : p.hedged_pnl);
  const level = hgPct(data.request.var_level).replace("+", "");

  const scenarios = [
    ...(scen.tail_shock == null
      ? []
      : [[`Falls to the worst ${level} of windows (${hgPct(scen.tail_shock)})`, scen.tail_shock]]),
    ["A hard fall (−20%)", -0.20],
    ["Nothing happens", 0],
    ["Rallies 10%", 0.10],
  ];

  $(`${prefix}-scen-table`).innerHTML =
    `<table><tr><th>if ${escapeHtml(scen.underlying)}…</th><th>unhedged</th>` +
    `<th>hedged</th><th>the hedge added</th></tr>` +
    scenarios.map(([label, shock]) => {
      const p = at(shock), after = hedged(p), added = after - p.exposure_pnl;
      return `<tr><td>${escapeHtml(label)}</td>` +
        `<td class="mono ${cls(p.exposure_pnl)}">${hgMoney(p.exposure_pnl)}</td>` +
        `<td class="mono ${cls(after)}">${hgMoney(after)}</td>` +
        `<td class="mono ${cls(added)}">${added > 0.5 ? "+" : ""}${hgMoney(added)}</td></tr>`;
    }).join("") + "</table>";

  const notes = [
    `Measured at ${escapeHtml(scen.horizon_date)}, ${data.shocks.horizon_days} sessions out, ` +
    `on ${fmt$(scen.exposure_value)} of exposure.`,
    paired
      ? "Vol is put where this sample's own moves put it, so a fall lifts the puts — the blue line freezes it instead, and is the pessimistic reading."
      : "No vol index was available, so vol is frozen at every point: these lines understate what a long put is worth in a fall.",
  ];
  if (scen.tail_shock != null) {
    notes.push(`The dashed line is history, not a forecast: the worst ${level} of ` +
      `${data.shocks.horizon_days}-session windows between ${data.shocks.period[0]} and ` +
      `${data.shocks.period[1]} started beyond ${hgPct(scen.tail_shock)}.`);
  }
  $(`${prefix}-scen-legend`).innerHTML = notes.map((n) => `<div>· ${n}</div>`).join("");
}

// --- Hedge lifecycle log (step 6): decisions only ---
const HG_NEXT = {
  proposed: ["accepted", "closed"],
  accepted: ["executed", "closed"],
  executed: ["rolled", "closed"],
  rolled: ["rolled", "closed"],
};

async function hgRecord(index) {
  const analysis = hgLast;
  if (!analysis) return;
  const row = analysis.rows[index];
  try {
    await api(`/api/portfolios/${pfId}/hedge/records`, {
      method: "POST",
      body: {
        kind: row.kind,
        underlying: row.underlying,
        state: "proposed",
        quantity: row.quantity ?? null,
        notional: row.notional ?? null,
        legs: row.legs || [],
        quote_snapshot: { as_of: analysis.as_of, benchmark: analysis.benchmark,
                          liquidity: row.liquidity || {} },
        assumptions: analysis.assumptions,
        estimator_version: analysis.estimator_version,
        target_exposure: analysis.target,
        expected_cvar_reduction: analysis.target.reduction_sought,
        expected_cvar_reduction_low: (row.protection_bps_ci95 || [])[0] * analysis.value / 10000,
        expected_cvar_reduction_high: (row.protection_bps_ci95 || [])[1] * analysis.value / 10000,
        cost_bps: row.cost_bps,
        protection_bps: row.protection_bps,
        portfolio_value_at_entry: analysis.value,
        entry_cost: (row.cost_breakdown || {}).entry_cost ?? null,
      },
    });
    setStatus("HEDGE RECORDED AS PROPOSED");
    await hgLoadLog();
  } catch (e) { setStatus("ERR: " + e.message); }
}

async function hgAdvance(id, state) {
  const body = { state };
  if (state === "closed") {
    const exit = prompt("What was the hedge worth when it came off? (blank to skip)");
    if (exit !== null && exit.trim() !== "") body.exit_value = parseFloat(exit);
    const bookPnl = prompt("What did the book itself do over that window? (blank to skip)");
    if (bookPnl !== null && bookPnl.trim() !== "") body.realised_book_pnl = parseFloat(bookPnl);
  }
  try {
    await api(`/api/portfolios/${pfId}/hedge/records/${id}`, { method: "PATCH", body });
    await hgLoadLog();
  } catch (e) { setStatus("ERR: " + e.message); }
}

async function hgDelete(id) {
  try {
    await api(`/api/portfolios/${pfId}/hedge/records/${id}`, { method: "DELETE" });
    await hgLoadLog();
  } catch (e) { setStatus("ERR: " + e.message); }
}

async function hgLoadLog() {
  if (!pfId) return;
  try {
    const [records, card] = await Promise.all([
      api(`/api/portfolios/${pfId}/hedge/records`),
      api(`/api/portfolios/${pfId}/hedge/scorecard`),
    ]);
    $("hg-log").innerHTML = records.length
      ? `<table><tr><th>state</th><th>hedge</th><th>size</th><th>expected cut</th>` +
        `<th>realised</th><th>proposed</th><th></th></tr>` +
        records.map((r) => {
          const size = r.quantity != null ? `${r.quantity} contracts` : fmt$(r.notional || 0);
          const moves = (HG_NEXT[r.state] || [])
            .map((s) => `<button class="ghost panel-btn hg-adv" data-id="${r.id}" data-state="${s}">${s}</button>`)
            .join(" ");
          return `<tr><td><b>${escapeHtml(r.state)}</b></td>` +
            `<td>${escapeHtml(HG_KINDS[r.kind] || r.kind)} <span class="dim">${escapeHtml(r.underlying)}</span></td>` +
            `<td class="mono">${size}</td>` +
            `<td class="mono">${r.expected_cvar_reduction == null ? "-" : fmt$(r.expected_cvar_reduction)}</td>` +
            `<td class="mono ${r.realised_hedge_pnl == null ? "" : cls(r.realised_hedge_pnl)}">` +
            `${r.realised_hedge_pnl == null ? "-" : fmt$(r.realised_hedge_pnl)}</td>` +
            `<td class="dim">${timeAgo(r.proposed_at)}</td>` +
            `<td>${moves} <button class="ghost panel-btn danger hg-del" data-id="${r.id}">del</button></td></tr>`;
        }).join("") + "</table>"
      : `<div class="empty">No hedge decisions recorded for this book.</div>`;
    $("hg-log").querySelectorAll("button.hg-adv").forEach((b) => {
      b.onclick = () => hgAdvance(Number(b.dataset.id), b.dataset.state);
    });
    $("hg-log").querySelectorAll("button.hg-del").forEach((b) => {
      b.onclick = () => hgDelete(Number(b.dataset.id));
    });

    $("hg-scorecard").innerHTML = card.graded
      ? [
          ["Closed hedges on record", card.graded],
          ["What they returned", fmt$(card.realised_hedge_pnl)],
          // A credit structure is paid *to* you, so the label follows the sign.
          [card.premium_paid < 0 ? "Premium received" : "Premium paid",
           fmt$(Math.abs(card.premium_paid))],
          ["Paid off when the book fell", `${card.paid_when_the_book_fell} of ${card.book_down_episodes}`],
        ].map(([k, v]) => `<div class="r"><span class="k">${k}</span><span class="v">${v}</span></div>`).join("") +
        `<p class="explain">${escapeHtml(card.note)}</p>`
      : `<p class="explain">${escapeHtml(card.note || "")}</p>`;
  } catch (e) {
    $("hg-log").innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
}

// --- CBOE reference overlays (step 7): the long record ---
async function hgLoadOverlays() {
  const button = $("hg-overlays");
  button.disabled = true;
  $("hg-overlay-body").innerHTML = `<div class="empty">Loading two decades of index history…</div>`;
  try {
    const d = await api("/api/backtest/overlays?start=2006-01-01");
    const rows = (list, protective) => list.map((r) => {
      const ratio = r.cagr_give_up_per_drawdown_removed;
      return `<tr><td>${escapeHtml(r.name)} <span class="dim">${escapeHtml(r.symbol)}</span></td>` +
        `<td class="dim">${r.period.years}y</td>` +
        `<td class="mono">${fmtPct(r.cagr)}</td>` +
        `<td class="mono ${cls(-r.cagr_give_up)}">${fmtPct(r.cagr_give_up, true)}</td>` +
        `<td class="mono">${fmtPct(r.max_drawdown)}</td>` +
        `<td class="mono">${protective ? fmtPct(r.drawdown_removed, true) : "no floor"}</td>` +
        `<td class="mono">${protective && ratio != null ? ratio.toFixed(2) : "-"}</td>` +
        `<td class="mono">${fmtPct(r.downside_capture)} / ${fmtPct(r.upside_capture)}</td></tr>`;
    }).join("");

    // "Given up", not "vs index": the figure is reference minus strategy, so a
    // positive number is return surrendered. Labelling it "vs index" would read
    // as outperformance and invert the meaning.
    const head = `<tr><th>strategy</th><th>span</th><th>return p.a.</th><th>given up p.a.</th>` +
      `<th>worst fall</th><th>fall avoided</th><th>cost per unit avoided</th>` +
      `<th>down / up capture</th></tr>`;

    let html = "";
    if (!d.protective_available) {
      html += `<div class="empty" style="text-align:left">${escapeHtml(d.notes[0])}</div>`;
    } else {
      html += `<div class="tablewrap"><table>${head}${rows(d.overlays, true)}</table></div>`;
    }
    if (d.comparators.length) {
      html += `<p class="explain"><b>Not hedges.</b> These sell option premium: they earn a
        return and soften falls a little, but they set no floor, so they are shown apart
        and never ranked as protection.</p>` +
        `<div class="tablewrap"><table>${head}${rows(d.comparators, false)}</table></div>`;
    }
    html += `<div class="dim" style="margin-top:8px">` +
      d.notes.slice(d.protective_available ? 0 : 1)
        .map((n) => `<div>· ${escapeHtml(n)}</div>`).join("") +
      (d.skipped.length
        ? `<div>· Unavailable: ${d.skipped.map((s) => escapeHtml(s.symbol + " (" + s.reason + ")")).join(", ")}.</div>`
        : "") +
      `</div>`;
    $("hg-overlay-body").innerHTML = html;
    setStatus("OVERLAY RECORD LOADED");
  } catch (e) {
    $("hg-overlay-body").innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  } finally {
    button.disabled = false;
  }
}

$("hg-overlays").onclick = hgLoadOverlays;

// --- Narrative layer (step 8): explains the verdict, never changes it ---
async function hgNarrateStatus() {
  if (!pfId) return;
  const button = $("hg-narrate");
  const note = $("hg-narrate-note");
  try {
    const state = await api(`/api/portfolios/${pfId}/hedge/narrate/status`);
    button.disabled = !state.enabled;
    note.textContent = state.enabled ? "model: " + (state.model || "ready") : (state.reason || "switched off");
  } catch (e) {
    button.disabled = true;
    note.textContent = e.message;
  }
}

async function hgNarrate() {
  if (!pfId) return;
  const button = $("hg-narrate");
  button.disabled = true;
  $("hg-narrative").innerHTML = `<div class="empty">Re-running the analysis and explaining it…</div>`;
  try {
    const d = await api(`/api/portfolios/${pfId}/hedge/narrate`, {
      method: "POST",
      body: {
        horizon_days: Number($("hg-horizon").value),
        target_reduction_fraction: Number($("hg-fraction").value),
        instruments: hgInstruments(),
      },
    });
    hgRender(d.analysis);
    const n = d.narrative;
    const sell = n.recommended_action === "de_risk_by_selling";
    const limits = (n.limits_of_this_analysis || [])
      .map((l) => `<li>${escapeHtml(l)}</li>`).join("");
    $("hg-narrative").innerHTML =
      `<div class="headline ${sell ? "neg" : "pos"}">${escapeHtml(n.headline)}</div>` +
      `<div class="kv" style="margin-top:8px">` +
      [["What you give up", n.what_you_give_up],
       ["What stays unprotected", n.what_stays_unprotected],
       ["How much to trust this", n.sample_caution],
       ...(n.candidate_kind ? [["Why this one", n.why_this_candidate]] : [])]
        .map(([k, v]) => `<div class="r"><span class="k">${k}</span><span class="v">${escapeHtml(v || "-")}</span></div>`)
        .join("") + `</div>` +
      (limits ? `<p class="explain">What this analysis cannot see:</p><ul class="dim">${limits}</ul>` : "") +
      (n.contradicted_engine || []).map((f) =>
        `<div class="hg-flag">The explanation was corrected against the engine: ${escapeHtml(f)}</div>`).join("");
    setStatus("EXPLANATION READY");
  } catch (e) {
    $("hg-narrative").innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
    setStatus("ERR: " + e.message);
  } finally {
    hgNarrateStatus();
  }
}

$("hg-narrate").onclick = hgNarrate;

// ---------- flagged: change detection ----------
// The backend does every diff; this view is layout. Three tabs share one
// vocabulary: the market screen ranks a whole cross-section on one accrual
// flag, the company scan runs every flag type against one filer's last two
// filings, and the catalogue is the list of flag types with the way each one
// lies — which is what the cards quote back under every row.
let fgLoaded = false;
let fgCatalogue = null;
let fgKinds = null;          // Set of flag slugs ticked for the company scan

function loadFlaggedView() {
  if (fgLoaded) return;
  fgLoaded = true;
  fgLoadCatalogue();
  fgYearOptions();
  $("fg-scan-symbol").focus();
}

document.querySelectorAll("#fg-tabs .tab").forEach((t) => {
  t.onclick = () => {
    document.querySelectorAll("#fg-tabs .tab").forEach((x) => x.classList.toggle("active", x === t));
    document.querySelectorAll(".fgtab").forEach((p) => p.classList.toggle("active", p.id === "fgtab-" + t.dataset.fgtab));
  };
});

function fgYearOptions() {
  // The newest complete calendar year lags the calendar by about a quarter;
  // the backend picks that default, and this only offers the recent past.
  const now = new Date();
  const newest = now.getMonth() >= 3 ? now.getFullYear() - 1 : now.getFullYear() - 2;
  $("fg-mkt-year").innerHTML = [0, 1, 2, 3, 4].map((back) => {
    const y = newest - back;
    return `<option value="${y}"${back === 0 ? " selected" : ""}>${y} vs ${y - 1}</option>`;
  }).join("");
}

async function fgLoadCatalogue() {
  try {
    const d = await api("/api/v1/flagged/catalogue");
    fgCatalogue = d.results;
  } catch (e) {
    $("fg-catalogue").innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
    return;
  }
  fgKinds = new Set(fgCatalogue.map((f) => f.flag));
  fgRenderKindChips();
  fgRenderCatalogue();
}

function fgFlagLabel(slug) {
  const f = (fgCatalogue || []).find((x) => x.flag === slug);
  return f ? f.label : slug.replace(/_/g, " ");
}
function fgArtifact(slug) {
  const f = (fgCatalogue || []).find((x) => x.flag === slug);
  return f ? f.artifact : "";
}

function fgRenderKindChips() {
  const el = $("fg-scan-kinds");
  el.innerHTML = fgCatalogue.map((f) =>
    `<button class="chip sm ${fgKinds.has(f.flag) ? "active" : ""}" data-fg-kind="${f.flag}"
       title="${escapeHtml(f.what)}">${escapeHtml(f.label)}
       <span class="fg-kind">${escapeHtml(f.read_from)}</span></button>`).join("") +
    `<button class="chip sm ghost" id="fg-kinds-all">All</button>` +
    `<button class="chip sm ghost" id="fg-kinds-cheap">Skip the document reads</button>`;
  el.querySelectorAll("[data-fg-kind]").forEach((c) => {
    c.onclick = () => {
      const k = c.dataset.fgKind;
      if (fgKinds.has(k)) fgKinds.delete(k); else fgKinds.add(k);
      c.classList.toggle("active", fgKinds.has(k));
    };
  });
  $("fg-kinds-all").onclick = () => { fgKinds = new Set(fgCatalogue.map((f) => f.flag)); fgRenderKindChips(); };
  $("fg-kinds-cheap").onclick = () => {
    fgKinds = new Set(fgCatalogue.filter((f) => f.read_from !== "document").map((f) => f.flag));
    fgRenderKindChips();
  };
}

function fgRenderCatalogue() {
  // One card per flag type. A six-column table put the whole point — how the
  // flag lies — in a sliver at the right edge; here it gets the full width.
  $("fg-catalogue").innerHTML = fgCatalogue.map((f) => `<div class="fg-card fg-catalogue">
    <div class="fg-card-h">
      <span class="fg-flag">${escapeHtml(f.label)}</span>
      <span class="mono dim">${escapeHtml(f.flag)}</span>
      <span class="fg-kind">${escapeHtml(f.read_from)}</span>
      <span class="fg-date">${f.reading ? "carries a conventional reading" : "direction-neutral by construction"}</span>
    </div>
    <div class="fg-cat-grid">
      <div><div class="subhead">Measures</div><div class="fg-summary">${escapeHtml(f.what)}</div></div>
      <div><div class="subhead">Compares</div><div class="fg-summary">${escapeHtml(f.compares)}</div></div>
      <div><div class="subhead">Conventional reading</div><div class="fg-summary">${f.reading ? escapeHtml(f.reading) : `<span class="dim">None. A count cannot tell a short setup from a capitulation bottom, and the flag does not pretend to.</span>`}</div></div>
    </div>
    <div class="fg-artifact"><b>How it lies:</b> ${escapeHtml(f.artifact)}</div>
  </div>`).join("");
}

// --- market screen ---
const FG_MKT_COLS = {
  receivables: [
    ["dso_change_days", "DSO Δ (days)", (v) => (v > 0 ? "+" : "") + v.toFixed(0)],
    ["prior_dso", "DSO then", (v) => v.toFixed(0)],
    ["dso", "DSO now", (v) => v.toFixed(0)],
    ["receivables_growth", "Receivables", (v) => fmtPct(v, true)],
    ["revenue_growth", "Revenue", (v) => fmtPct(v, true)],
    ["revenue", "Revenue $", (v) => fgMoney(v)],
  ],
  deferred_revenue: [
    ["divergence", "Divergence", (v) => fmtPct(v, true)],
    ["deferred_growth", "Deferred rev.", (v) => fmtPct(v, true)],
    ["revenue_growth", "Revenue", (v) => fmtPct(v, true)],
    ["deferred_revenue", "Deferred $", (v) => fgMoney(v)],
    ["revenue", "Revenue $", (v) => fgMoney(v)],
  ],
  buybacks: [
    ["share_count_change_pct", "Diluted count", (v) => fmtPct(v, true)],
    ["repurchase_payments", "Repurchased $", (v) => fgMoney(v)],
    ["prior_diluted_shares", "Shares then", (v) => fgCount(v)],
    ["diluted_shares", "Shares now", (v) => fgCount(v)],
  ],
};
function fgMoney(v) {
  if (v == null) return "-";
  const a = Math.abs(v);
  if (a >= 1e9) return "$" + (v / 1e9).toFixed(1) + "B";
  if (a >= 1e6) return "$" + (v / 1e6).toFixed(0) + "M";
  return fmt$(v);
}
function fgCount(v) {
  if (v == null) return "-";
  if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(1) + "M";
  return Math.round(v).toLocaleString("en-US");
}

async function fgRunMarket() {
  const screen = $("fg-mkt-screen").value;
  const year = $("fg-mkt-year").value;
  const limit = $("fg-mkt-limit").value;
  $("fg-mkt-note").textContent = "reading the market…";
  $("fg-mkt-warn").innerHTML = "";
  $("fg-mkt-table").innerHTML = `<div class="empty">Four to six requests for the whole market — a few seconds cold.</div>`;
  setStatus("FLAGGED: SCANNING THE MARKET");
  try {
    const d = await api(`/api/v1/flagged/market?screen=${screen}&year=${year}&limit=${limit}`);
    const x = d.extra || {};
    $("fg-mkt-note").textContent =
      `${x.universe ?? "?"} filers compared · ${x.crossed_gates ?? "?"} crossed the gates · ` +
      `${x.returned ?? d.results.length} shown` +
      (x.misaligned_periods_dropped ? ` · ${x.misaligned_periods_dropped} off-calendar filers not compared` : "");
    const artifact = fgArtifact(d.results[0]?.flag || "");
    const cols = FG_MKT_COLS[screen] || [];
    $("fg-mkt-table").innerHTML = `<table>
      <tr><th>#</th><th>Symbol</th><th>Company</th><th>Filed</th><th>Pctl</th>${cols.map((c) => `<th>${c[1]}</th>`).join("")}<th>Filing</th></tr>` +
      d.results.map((r, i) => `<tr>
        <td class="dim">${i + 1}</td>
        <td><a href="#" data-fg-sym="${escapeHtml(r.symbol)}" class="mono">${escapeHtml(r.symbol)}</a></td>
        <td style="text-align:left;white-space:normal;max-width:220px">${escapeHtml(r.issuer || "")}</td>
        <td class="mono dim">${escapeHtml(r.known_on || "")}</td>
        <td class="fg-pctl">${r.market_percentile != null ? r.market_percentile.toFixed(1) : "-"}</td>
        ${cols.map((c) => `<td class="mono">${r[c[0]] == null ? "-" : c[2](r[c[0]])}</td>`).join("")}
        <td>${r.accession_number ? `<a href="https://www.sec.gov/Archives/edgar/data/${parseInt(r.cik, 10)}/${escapeHtml(r.accession_number.replace(/-/g, ""))}/" target="_blank" rel="noopener">${escapeHtml(r.form || "filing")} ↗</a>` : "-"}</td>
      </tr>`).join("") + `</table>` +
      (artifact ? `<div class="fg-artifact"><b>How this flag lies:</b> ${escapeHtml(artifact)}</div>` : "");
    $("fg-mkt-table").querySelectorAll("[data-fg-sym]").forEach((a) => {
      a.onclick = (ev) => { ev.preventDefault(); fgScanSymbol(a.dataset.fgSym); };
    });
    if (d.warnings?.length) $("fg-mkt-warn").innerHTML = `<div class="warnbox">${escapeHtml(d.warnings[0])}</div>`;
    setStatus("FLAGGED: MARKET SCREEN READY");
  } catch (e) {
    $("fg-mkt-note").textContent = "";
    $("fg-mkt-table").innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
    setStatus("ERR: " + e.message);
  }
}

// --- one company ---
function fgScanSymbol(sym) {
  document.querySelector('#fg-tabs .tab[data-fgtab="company"]').click();
  $("fg-scan-symbol").value = sym;
  fgRunScan();
}

function fgFilingLinks(r) {
  const parts = [];
  if (r.form || r.period_end) parts.push(`${r.form || ""}${r.period_end ? " for period ending " + r.period_end : ""}`.trim());
  if (r.filing_url) parts.push(`<a href="${escapeHtml(r.filing_url)}" target="_blank" rel="noopener">filing ↗</a>`);
  if (r.prior_filing_url) parts.push(`<a href="${escapeHtml(r.prior_filing_url)}" target="_blank" rel="noopener">prior filing${r.prior_filing_date ? " (" + r.prior_filing_date + ")" : ""} ↗</a>`);
  if (r.accession_number && !r.filing_url && r.cik) parts.push(`<a href="https://www.sec.gov/Archives/edgar/data/${parseInt(r.cik, 10)}/${escapeHtml(r.accession_number.replace(/-/g, ""))}/" target="_blank" rel="noopener">filing ↗</a>`);
  if (r.market_percentile != null) parts.push(`${r.market_percentile}th percentile of ${r.universe || "?"} filers`);
  if (r.read_via) parts.push(`read via ${escapeHtml(r.read_via)}`);
  if (r.auditor_source) parts.push(`auditor from ${escapeHtml(r.auditor_source)}`);
  return parts.join(" · ");
}

function fgCard(r) {
  let body = "";
  if (r.paragraphs?.length) {
    body += `<div class="fg-paras">` + r.paragraphs.map((p) =>
      `<div class="fg-para">${p.heading ? `<b>${escapeHtml(p.heading)}</b> — ` : ""}${escapeHtml(p.text)}` +
      `${p.best_match != null ? `<span class="fg-match">nearest ${(p.best_match * 100).toFixed(0)}%</span>` : ""}</div>`).join("") +
      (r.count > r.paragraphs.length ? `<div class="fg-para dim">…and ${r.count - r.paragraphs.length} more</div>` : "") +
      `</div>`;
    if (r.rewrite_suspected) body += `<div class="fg-meta warn">Additions and removals balance — likely a rewrite of the section rather than a change of position.</div>`;
  }
  if (r.quote) body += `<div class="fg-paras"><div class="fg-para">“${escapeHtml(r.quote)}”</div></div>`;
  if (r.watched?.length) {
    body += `<div class="fg-paras">` + r.watched.map((w) =>
      `<div class="fg-para"><b>${escapeHtml(w.means || w.concept)}</b> — ${escapeHtml(w.concept)}${w.value != null ? ` = ${escapeHtml(String(w.value))} ${escapeHtml(w.unit || "")}` : ""}</div>`).join("") + `</div>`;
  }
  if (r.top_sellers?.length || r.top_buyers?.length) {
    const movers = (r.direction === "distribution" ? r.top_sellers : r.top_buyers) || [];
    body += `<div class="fg-paras">` + movers.slice(0, 5).map((m) =>
      `<div class="fg-para"><b>${escapeHtml(m.filer)}</b> <span class="mono ${m.change < 0 ? "neg" : "pos"}">${m.change > 0 ? "+" : ""}${fgCount(m.change)}</span>` +
      `${m.held_now ? ` — still holds ${fgCount(m.held_now)}` : " — position closed"}${m.passive ? " <span class=\"fg-kind\">index</span>" : ""}${m.filed ? ` <span class="dim mono">filed ${escapeHtml(m.filed)}</span>` : ""}</div>`).join("") + `</div>`;
  }
  if (r.actions?.length) {
    body += `<div class="fg-paras">` + r.actions.map((a) =>
      `<div class="fg-para"><span class="mono">${escapeHtml(a.date)}</span> ${escapeHtml(a.firm)} <b class="${a.action === "up" ? "pos" : "neg"}">${escapeHtml(a.action)}</b> ${a.from_grade ? escapeHtml(a.from_grade) + " → " : ""}${escapeHtml(a.to_grade)}</div>`).join("") + `</div>`;
  }
  return `<div class="fg-card">
    <div class="fg-card-h">
      <span class="fg-sym" data-fg-open="${escapeHtml(r.symbol)}">${escapeHtml(r.symbol)}</span>
      <span class="dim">${escapeHtml(r.issuer || "")}</span>
      <span class="fg-flag">${escapeHtml(fgFlagLabel(r.flag))}</span>
      <span class="fg-date">first public ${escapeHtml(r.known_on)}</span>
    </div>
    <div class="fg-summary">${escapeHtml(r.summary || "")}</div>
    ${body}
    <div class="fg-meta">${fgFilingLinks(r)}</div>
    <div class="fg-artifact"><b>How this flag lies:</b> ${escapeHtml(fgArtifact(r.flag))}</div>
  </div>`;
}

async function fgRunScan() {
  const symbol = $("fg-scan-symbol").value.trim().toUpperCase();
  if (!symbol) { $("fg-scan-symbol").focus(); return; }
  if (!fgKinds || !fgKinds.size) { $("fg-scan-warn").innerHTML = `<div class="warnbox">Tick at least one flag type.</div>`; return; }
  const kinds = [...fgKinds].join(",");
  const period = $("fg-scan-period").value;
  const heavy = [...fgKinds].some((k) => (fgCatalogue || []).find((f) => f.flag === k)?.read_from === "document");
  $("fg-scan-note").textContent = "scanning…";
  $("fg-scan-warn").innerHTML = "";
  $("fg-scan-out").innerHTML = `<div class="empty">${heavy ? "Reading two annual reports per symbol — a few seconds on a cold cache." : "Reading the tagged facts…"}</div>`;
  setStatus("FLAGGED: SCANNING " + symbol);
  try {
    const d = await api(`/api/v1/flagged/scan?symbol=${encodeURIComponent(symbol)}&kinds=${encodeURIComponent(kinds)}&period=${period}`);
    const x = d.extra || {};
    $("fg-scan-note").textContent = `${x.flags ?? d.results.length} flag(s) across ${(x.symbols || []).length} symbol(s), ${(x.kinds || []).length} type(s) checked`;
    $("fg-scan-out").innerHTML = d.results.map(fgCard).join("") || `<div class="empty">Nothing moved.</div>`;
    $("fg-scan-out").querySelectorAll("[data-fg-open]").forEach((s) => { s.onclick = () => openStock(s.dataset.fgOpen, "flagged"); });
    const skipped = (x.skipped || []);
    if (skipped.length) $("fg-scan-warn").innerHTML = `<div class="warnbox">Could not read: ${escapeHtml(skipped.join(" · "))}</div>`;
    setStatus("FLAGGED: SCAN READY");
  } catch (e) {
    $("fg-scan-note").textContent = "";
    // A quiet filer is a 404 by platform convention; say so in plain words.
    const quiet = /No flags for/.test(e.message);
    $("fg-scan-out").innerHTML = `<div class="empty">${quiet ? "Nothing moved on the ticked dimensions." : escapeHtml(e.message)}</div>`;
    if (quiet) {
      const m = e.message.match(/\(skipped: (.*)\)/);
      if (m) $("fg-scan-warn").innerHTML = `<div class="warnbox">Could not read: ${escapeHtml(m[1])}</div>`;
    }
    setStatus(quiet ? "FLAGGED: NOTHING MOVED" : "ERR: " + e.message);
  }
}

// --- 13F flow against the tape ---
function fgFlowRow(r) {
  const movers = (r.direction === "distribution" ? r.top_sellers : r.top_buyers) || [];
  const labels = [];
  if (r.issuance_suspected) labels.push("issuance?");
  if (r.single_filer_suspect) labels.push("single filer?");
  if (r.denominator_suspect) labels.push("denominators?");
  if (r.foreign_domicile) labels.push(escapeHtml(r.domicile));
  if (r.passive_share >= 0.25) labels.push(`${(r.passive_share * 100).toFixed(0)}% index`);
  return `<tr>
    <td class="dim">${r.screen_rank || "-"}</td>
    <td><a href="#" data-fg-flsym="${escapeHtml(r.symbol)}" class="mono">${escapeHtml(r.symbol)}</a></td>
    <td style="text-align:left;white-space:normal;max-width:200px">${escapeHtml(r.issuer || "")}</td>
    <td class="${r.direction === "distribution" ? "neg" : "pos"}">${r.direction === "distribution" ? "selling" : "buying"}</td>
    <td class="mono">${r.days_of_volume.toFixed(1)}</td>
    <td class="mono">${r.overhang_days != null ? r.overhang_days.toFixed(1) : "-"}</td>
    <td class="mono">${(r.net_change > 0 ? "+" : "") + fgCount(r.net_change)}</td>
    <td class="mono">${fgCount(r.adv_shares)}</td>
    <td class="mono">${fgMoney(r.adv_dollars)}</td>
    <td class="mono">${fgMoney(r.market_cap)}</td>
    <td class="mono">${(r.institutional_pct * 100).toFixed(0)}%</td>
    <td style="text-align:left;white-space:normal;max-width:260px">${movers.slice(0, 2).map((m) =>
      `${escapeHtml(String(m.filer).slice(0, 26))} <span class="mono ${m.change < 0 ? "neg" : "pos"}">${m.change > 0 ? "+" : ""}${fgCount(m.change)}</span>${m.held_now ? ` <span class="dim">holds ${fgCount(m.held_now)}</span>` : ""}`).join("<br>")}</td>
    <td style="text-align:left"><span class="fg-kind">${labels.join("</span> <span class=\"fg-kind\">") || "—"}</span></td>
  </tr>`;
}

async function fgRunFlows(symbol) {
  const qs = symbol
    ? `symbol=${encodeURIComponent(symbol)}`
    : `direction=${$("fg-fl-direction").value}&max_market_cap_bn=${$("fg-fl-cap").value}` +
      `&min_days_of_volume=${$("fg-fl-days").value}&include_suspect=${$("fg-fl-suspect").checked}&limit=100`;
  $("fg-fl-note").textContent = symbol ? `reading ${symbol}…` : "reading two 13F data sets and the tape…";
  $("fg-fl-warn").innerHTML = "";
  $("fg-fl-out").innerHTML = `<div class="empty">First run on a cold cache reads ~200 MB of SEC data sets (about a minute); afterwards a few seconds.</div>`;
  setStatus("FLAGGED: 13F FLOW");
  try {
    const d = await api(`/api/v1/flagged/flows?${qs}`);
    const x = d.extra || {};
    $("fg-fl-note").textContent =
      `${x.period_end || "?"} vs ${x.prior_period_end || "?"} · known ${x.known_on || "?"} · ` +
      (symbol ? `${d.results.length} row(s)` :
        `${x.common_filers ?? "?"} filers in both quarters · ${x.priced ?? "?"} priced · ${x.crossed_gates ?? "?"} crossed · ` +
        `${x.suspect_dropped ?? 0} suspect, ${x.spacs_dropped ?? 0} SPACs, ${x.identity_changes_dropped ?? 0} identity changes set aside`);
    $("fg-fl-out").innerHTML = `<div class="tablewrap"><table>
      <tr><th>#</th><th>Symbol</th><th>Company</th><th>Side</th><th>Days of vol</th><th>Overhang</th><th>Net shares</th><th>ADV</th><th>ADV $</th><th>Mkt cap</th><th>Inst %</th><th>Largest movers</th><th>Labels</th></tr>` +
      d.results.map(fgFlowRow).join("") + `</table></div>` +
      `<div class="fg-artifact"><b>How this flag lies:</b> ${escapeHtml(fgArtifact("institutional_flow"))}</div>`;
    $("fg-fl-out").querySelectorAll("[data-fg-flsym]").forEach((a) => {
      a.onclick = (ev) => { ev.preventDefault(); openStock(a.dataset.fgFlsym, "flagged"); };
    });
    setStatus("FLAGGED: 13F FLOW READY");
  } catch (e) {
    $("fg-fl-note").textContent = "";
    $("fg-fl-out").innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
    setStatus("ERR: " + e.message);
  }
}

// --- shared end-market read-through ---
function fgLineName(key) { return String(key || "").replace(/_/g, " "); }

async function fgRunReadThrough() {
  const hub = $("fg-rt-symbol").value.trim().toUpperCase();
  if (!hub) { $("fg-rt-symbol").focus(); return; }
  const peers = $("fg-rt-peers").value.trim().toUpperCase();
  const qs = `symbol=${encodeURIComponent(hub)}&min_agreeing=${$("fg-rt-agree").value}&min_exposure_pct=${$("fg-rt-exposure").value}` +
    (peers ? `&peers=${encodeURIComponent(peers)}` : "");
  $("fg-rt-note").textContent = `clustering ${hub}…`;
  $("fg-rt-warn").innerHTML = "";
  $("fg-rt-out").innerHTML = `<div class="empty">Reading the peer group, then a filing per member…</div>`;
  setStatus("FLAGGED: READ-THROUGH " + hub);
  let d;
  try {
    d = await api(`/api/v1/flagged/read_through?${qs}`);
  } catch (e) {
    // "No laggard" is a 404 with the cluster summary in the message — still worth showing.
    $("fg-rt-note").textContent = "";
    $("fg-rt-out").innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
    setStatus(/No laggard/.test(e.message) ? "FLAGGED: NO READ-THROUGH" : "ERR: " + e.message);
    return;
  }
  const x = d.extra || {};
  $("fg-rt-note").textContent = `${(x.cluster || []).length} in cluster · ${(x.members_disaggregating || []).length} disaggregate · ${(x.lines || []).length} shared lines · ${d.results.length} read-through(s)`;
  const linesTable = `<div class="subhead" style="margin-top:14px">SHARED LINES</div><div class="tablewrap"><table>
      <tr><th>Line</th><th>Members</th><th>Reported this quarter</th><th>Accelerating</th><th>Decelerating</th><th>Verdict</th><th>Known</th></tr>` +
    (x.lines || []).map((c) => `<tr>
      <td>${escapeHtml(fgLineName(c.line))}</td>
      <td class="mono">${(c.members || []).length}</td>
      <td class="mono">${(c.reported_this_quarter || []).length}</td>
      <td style="text-align:left" class="pos">${(c.accelerating || []).join(", ") || "-"}</td>
      <td style="text-align:left" class="neg">${(c.decelerating || []).join(", ") || "-"}</td>
      <td style="text-align:left" class="${c.verdict === "common inflection" ? "warn" : "dim"}">${escapeHtml(c.verdict || "")}${c.direction ? " (" + c.direction + ")" : ""}</td>
      <td class="mono dim">${escapeHtml(c.known_on || "")}</td>
    </tr>`).join("") + `</table></div>`;
  const cards = d.results.map((r) => `<div class="fg-card">
      <div class="fg-card-h">
        <span class="fg-sym" data-fg-open="${escapeHtml(r.symbol)}">${escapeHtml(r.symbol)}</span>
        <span class="fg-flag">${escapeHtml(fgLineName(r.line))} · ${r.direction === "down" ? "decelerating" : "accelerating"}</span>
        <span class="fg-kind">${escapeHtml(r.own_status || "")}</span>
        <span class="fg-date">pattern complete ${escapeHtml(r.known_on)}</span>
      </div>
      <div class="fg-summary">${escapeHtml(r.summary)}</div>
      <div class="fg-paras">${(r.confirmers || []).map((c) =>
        `<div class="fg-para"><b>${escapeHtml(c.symbol)}</b> ${fgLineName(r.line)} growth ${c.growth != null ? fmtPct(c.growth, true) : "?"} ` +
        `<span class="mono ${c.inflection < 0 ? "neg" : "pos"}">(${c.inflection > 0 ? "+" : ""}${(c.inflection * 100).toFixed(0)}pp)</span> quarter to ${escapeHtml(c.quarter || "?")}, filed ${escapeHtml(c.filed || "?")}` +
        `${c.consensus_drift_90d != null ? ` · consensus ${fmtPct(c.consensus_drift_90d, true)} / 90d` : ""}</div>`).join("")}</div>
      <div class="fg-meta">exposure ${(r.exposure * 100).toFixed(0)}% of revenue on ${escapeHtml(r.line_label || r.line)} (${escapeHtml(r.dimension || "")}) · ` +
      `own FY1 EPS consensus ${fmtPct(r.consensus_drift_90d, true)} over 90d, net revisions ${r.net_revisions_30d ?? "?"} · cluster: ${(r.cluster || []).join(", ")}</div>
      <div class="fg-artifact"><b>How this flag lies:</b> ${escapeHtml(fgArtifact("read_through"))}</div>
    </div>`).join("");
  $("fg-rt-out").innerHTML = cards + linesTable;
  $("fg-rt-out").querySelectorAll("[data-fg-open]").forEach((s) => { s.onclick = () => openStock(s.dataset.fgOpen, "flagged"); });
  setStatus("FLAGGED: READ-THROUGH READY");
}
$("fg-rt-run").onclick = fgRunReadThrough;
$("fg-rt-symbol").onkeydown = (ev) => { if (ev.key === "Enter") fgRunReadThrough(); };

$("fg-fl-run").onclick = () => fgRunFlows(null);
$("fg-fl-one").onclick = () => { const s = $("fg-fl-symbol").value.trim().toUpperCase(); if (s) fgRunFlows(s); };
$("fg-fl-symbol").onkeydown = (ev) => { if (ev.key === "Enter") $("fg-fl-one").click(); };

$("fg-mkt-run").onclick = fgRunMarket;
$("fg-scan-run").onclick = fgRunScan;
$("fg-scan-symbol").onkeydown = (ev) => { if (ev.key === "Enter") fgRunScan(); };
