/**
 * The dashboard page: one self-contained document, no build step, no external
 * asset. Everything it renders comes from `/events`; the only thing it reads
 * from the URL is the capability token, which is why it sets no referrer and
 * loads nothing remotely.
 */

export function dashboardPage() {
  return `<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="referrer" content="no-referrer" />
<title>Swarm · Mission Control</title>
<style>
  :root {
    --bg: #06080f;
    --panel: rgba(14, 20, 34, 0.82);
    --panel-line: rgba(90, 120, 190, 0.18);
    --text: #dbe4f5;
    --muted: #7c8aa8;
    --accent: #6f8dff;
    --cyan: #47e0d0;
    --warn: #ffb454;
    --bad: #ff6b7a;
    --good: #48d68a;
    --grid: rgba(90, 120, 190, 0.08);
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; min-height: 100%; }
  body {
    background:
      radial-gradient(1200px 600px at 12% -10%, rgba(60, 90, 180, 0.18), transparent 60%),
      radial-gradient(900px 500px at 100% 0%, rgba(40, 190, 180, 0.10), transparent 55%),
      linear-gradient(var(--grid) 1px, transparent 1px) 0 0 / 100% 44px,
      linear-gradient(90deg, var(--grid) 1px, transparent 1px) 0 0 / 44px 100%,
      var(--bg);
    color: var(--text);
    font: 13px/1.45 ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, Consolas, monospace;
    padding: 18px 18px 28px;
    min-height: 100vh;
  }
  header { display: flex; flex-wrap: wrap; gap: 12px; align-items: baseline; justify-content: space-between; margin-bottom: 14px; }
  h1 { font-size: 15px; letter-spacing: 0.34em; text-transform: uppercase; margin: 0; font-weight: 600; color: #eaf0ff; }
  h1 span { color: var(--cyan); }
  .meta { display: flex; gap: 14px; flex-wrap: wrap; color: var(--muted); }
  .meta b { color: var(--text); font-weight: 500; }
  .pill {
    display: inline-flex; align-items: center; gap: 7px; padding: 4px 10px; border-radius: 999px;
    border: 1px solid var(--panel-line); background: var(--panel); letter-spacing: 0.08em; font-size: 11px;
  }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); }
  .pill[data-conn="live"] .dot { background: var(--good); box-shadow: 0 0 8px var(--good); }
  .pill[data-conn="reconnecting"] .dot { background: var(--warn); }
  .pill[data-conn="offline"] .dot { background: var(--bad); }
  .metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(132px, 1fr)); gap: 10px; margin-bottom: 14px; }
  .card {
    background: var(--panel); border: 1px solid var(--panel-line); border-radius: 10px; padding: 10px 12px;
    backdrop-filter: blur(6px);
  }
  .card .label { color: var(--muted); font-size: 10px; letter-spacing: 0.16em; text-transform: uppercase; }
  .card .value { font-size: 20px; margin-top: 3px; letter-spacing: 0.02em; }
  .card .sub { color: var(--muted); font-size: 11px; margin-top: 2px; min-height: 14px; }
  .layout { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(280px, 1fr); gap: 14px; align-items: start; }
  @media (max-width: 900px) { .layout { grid-template-columns: 1fr; } }
  .stage { position: relative; background: var(--panel); border: 1px solid var(--panel-line); border-radius: 12px; padding: 14px; overflow: hidden; }
  .stage::after {
    content: ""; position: absolute; inset: 0; pointer-events: none;
    background: linear-gradient(180deg, rgba(110, 141, 255, 0.06), transparent 30%);
  }
  svg.links { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
  svg.links line { stroke: rgba(110, 141, 255, 0.22); stroke-width: 1; }
  svg.links line.hot { stroke: var(--cyan); stroke-width: 1.6; stroke-dasharray: 6 5; animation: flow 0.9s linear infinite; }
  @keyframes flow { to { stroke-dashoffset: -22; } }
  .nodes { position: relative; display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
  .node {
    border: 1px solid var(--panel-line); border-radius: 9px; padding: 9px 10px; background: rgba(9, 14, 25, 0.72);
    min-height: 86px; transition: border-color 0.25s ease, box-shadow 0.25s ease;
  }
  .node[data-state="running"] { border-color: rgba(71, 224, 208, 0.55); box-shadow: 0 0 0 1px rgba(71, 224, 208, 0.10), 0 0 22px rgba(71, 224, 208, 0.10); }
  .node[data-state="completed"] { border-color: rgba(72, 214, 138, 0.42); }
  .node[data-state="failed"] { border-color: rgba(255, 107, 122, 0.55); }
  .node[data-state="unknown"] { border-style: dashed; opacity: 0.86; }
  .node .name { text-transform: uppercase; letter-spacing: 0.18em; font-size: 10px; color: var(--muted); display: flex; justify-content: space-between; gap: 6px; }
  .node .state { font-size: 12px; margin-top: 6px; text-transform: lowercase; letter-spacing: 0.04em; }
  .node[data-state="running"] .state { color: var(--cyan); }
  .node[data-state="completed"] .state { color: var(--good); }
  .node[data-state="failed"] .state { color: var(--bad); }
  .node[data-state="unknown"] .state { color: var(--warn); }
  .node .detail { color: var(--muted); font-size: 11px; margin-top: 4px; word-break: break-word; }
  .beacon { width: 8px; height: 8px; border-radius: 50%; background: #2a3550; display: inline-block; }
  .node[data-state="running"] .beacon { background: var(--cyan); box-shadow: 0 0 0 0 rgba(71, 224, 208, 0.55); animation: pulse 1.6s ease-out infinite; }
  .node[data-state="completed"] .beacon { background: var(--good); }
  .node[data-state="failed"] .beacon { background: var(--bad); }
  .node[data-state="unknown"] .beacon { background: var(--warn); }
  @keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(71, 224, 208, 0.45); } 70% { box-shadow: 0 0 0 9px rgba(71, 224, 208, 0); } 100% { box-shadow: 0 0 0 0 rgba(71, 224, 208, 0); } }
  .panel-title { text-transform: uppercase; letter-spacing: 0.2em; font-size: 10px; color: var(--muted); margin: 0 0 8px; }
  .bars { display: grid; gap: 9px; }
  .bar .top { display: flex; justify-content: space-between; font-size: 11px; color: var(--muted); margin-bottom: 4px; }
  .bar .track { height: 5px; border-radius: 99px; background: rgba(90, 120, 190, 0.16); overflow: hidden; }
  .bar .fill { height: 100%; width: 0; background: linear-gradient(90deg, var(--accent), var(--cyan)); transition: width 0.4s ease; }
  .bar.warn .fill { background: linear-gradient(90deg, var(--warn), var(--bad)); }
  .log { margin-top: 12px; max-height: 320px; overflow: auto; display: grid; gap: 4px; }
  .entry { display: grid; grid-template-columns: 58px 1fr; gap: 8px; font-size: 11px; color: var(--muted); padding: 3px 0; border-bottom: 1px dashed rgba(90, 120, 190, 0.10); }
  .entry .t { color: #5d6a86; }
  .entry b { color: var(--text); font-weight: 500; }
  .empty { color: var(--muted); font-size: 11px; }
  footer { margin-top: 12px; color: #55627d; font-size: 10px; letter-spacing: 0.06em; }
  @media (prefers-reduced-motion: reduce) {
    svg.links line.hot { animation: none; stroke-dasharray: none; }
    .node[data-state="running"] .beacon { animation: none; }
    .bar .fill { transition: none; }
  }
</style>
</head>
<body>
<header>
  <h1>Swarm <span>//</span> Mission Control</h1>
  <div class="meta">
    <span class="pill" id="conn" data-conn="offline"><i class="dot"></i><span id="connText">connecting</span></span>
    <span id="runMeta">run <b>none</b></span>
    <span id="updated">updated <b>—</b></span>
  </div>
</header>

<section class="metrics" id="metrics"></section>

<div class="layout">
  <section class="stage">
    <p class="panel-title">Agents</p>
    <svg class="links" id="links"></svg>
    <div class="nodes" id="nodes"></div>
  </section>
  <aside>
    <section class="card">
      <p class="panel-title" style="margin-bottom:10px">Budget</p>
      <div class="bars" id="bars"><span class="empty">Unavailable</span></div>
    </section>
    <section class="card" style="margin-top:12px">
      <p class="panel-title">Events</p>
      <div class="log" id="log"><span class="empty">Waiting for the first observed event…</span></div>
    </section>
    <footer>read-only · 127.0.0.1 · measured values only</footer>
  </aside>
</div>

<script>
(function () {
  "use strict";
  var params = new URLSearchParams(location.search);
  var token = params.get("token") || "";
  var agents = ["manager", "researcher", "builder", "visual", "qc", "improve"];
  var state = null;
  var log = [];
  var source = null;
  var attempts = 0;
  var maxAttempts = 5;
  var timer = null;

  function setConn(kind, text) {
    var pill = document.getElementById("conn");
    pill.setAttribute("data-conn", kind);
    document.getElementById("connText").textContent = text;
  }

  function value(v, format, suffix) {
    if (v === null || v === undefined || (typeof v === "number" && !isFinite(v))) return "Unavailable";
    var shown = format ? format(v) : String(v);
    return suffix ? shown + suffix : shown;
  }

  function num(v) { return typeof v === "number" && isFinite(v); }

  function ms(v) {
    if (!num(v)) return "Unavailable";
    if (v < 1000) return Math.round(v) + "ms";
    var s = v / 1000;
    if (s < 60) return s.toFixed(1) + "s";
    var m = Math.floor(s / 60);
    return m + "m " + Math.round(s - m * 60) + "s";
  }

  function money(v) {
    if (!num(v)) return "Unavailable";
    return "$" + v.toFixed(4);
  }

  function metric(label, text, sub) {
    var card = document.createElement("div");
    card.className = "card";
    var l = document.createElement("div"); l.className = "label"; l.textContent = label;
    var v = document.createElement("div"); v.className = "value"; v.textContent = text;
    var s = document.createElement("div"); s.className = "sub"; if (sub) s.textContent = sub;
    card.appendChild(l); card.appendChild(v); card.appendChild(s);
    return card;
  }

  function renderMetrics() {
    var root = document.getElementById("metrics");
    root.textContent = "";
    var run = state && state.run;
    var counters = (state && state.counters) || {};
    root.appendChild(metric("Phase", state ? state.phase : "unknown", run && run.workflow ? "workflow " + run.workflow : ""));
    root.appendChild(metric("Elapsed", run ? ms(run.elapsedMs) : "Unavailable", run && run.kind ? "kind " + run.kind : ""));
    root.appendChild(metric("Model calls", value(counters.modelCalls), "delegations + judgements"));
    root.appendChild(metric("Tokens", value(counters.tokens, function (v) { return v.toLocaleString(); }), "measured only"));
    root.appendChild(metric("Cost", value(counters.costUSD, money), "reported by runtime"));
    var v = state && state.verification;
    root.appendChild(metric("Verification", v ? (v.passed ? "passed" : "failed") : "Unavailable", v && v.independence ? "independent judge: " + v.independence : ""));
  }

  function nodeEl(id) {
    var el = document.createElement("div");
    el.className = "node";
    el.setAttribute("data-agent", id);
    var name = document.createElement("div"); name.className = "name";
    var label = document.createElement("span"); label.textContent = id;
    var beacon = document.createElement("span"); beacon.className = "beacon";
    name.appendChild(label); name.appendChild(beacon);
    var st = document.createElement("div"); st.className = "state";
    var detail = document.createElement("div"); detail.className = "detail";
    el.appendChild(name); el.appendChild(st); el.appendChild(detail);
    return el;
  }

  function renderNodes() {
    var root = document.getElementById("nodes");
    var byAgent = (state && state.agents) || {};
    for (var i = 0; i < agents.length; i++) {
      var id = agents[i];
      var el = root.querySelector('[data-agent="' + id + '"]') || root.appendChild(nodeEl(id));
      var info = byAgent[id] || { state: "unknown" };
      el.setAttribute("data-state", info.state || "unknown");
      el.querySelector(".state").textContent = info.state || "unknown";
      var detail = el.querySelector(".detail");
      var parts = [];
      if (info.stepId) parts.push("step " + info.stepId);
      if (info.model) parts.push(info.model);
      if (num(info.durationMs)) parts.push(ms(info.durationMs));
      else if (info.state === "running" && num(info.startedAt)) parts.push("since " + new Date(info.startedAt).toLocaleTimeString());
      if (info.state === "idle") parts.push("no observed work");
      detail.textContent = parts.join(" · ");
    }
  }

  function renderBars() {
    var root = document.getElementById("bars");
    root.textContent = "";
    var budget = state && state.budget;
    if (!budget || !budget.limits || !budget.used) {
      var none = document.createElement("span"); none.className = "empty"; none.textContent = "Unavailable";
      root.appendChild(none);
      return;
    }
    var rows = [
      ["tokens", "Tokens"],
      ["costUSD", "Cost"],
      ["ms", "Time"],
      ["steps", "Steps"],
      ["toolCalls", "Tool calls"]
    ];
    for (var i = 0; i < rows.length; i++) {
      var key = rows[i][0];
      var limits = { tokens: "maxTokens", costUSD: "maxCostUSD", ms: "maxMs", steps: "maxSteps", toolCalls: "maxToolCalls" };
      var limit = budget.limits[limits[key]];
      var used = budget.used[key];
      if (!num(limit) || !num(used)) continue;
      var pct = limit > 0 ? Math.min(100, (used / limit) * 100) : 0;
      var bar = document.createElement("div");
      bar.className = "bar" + (pct > 80 ? " warn" : "");
      var top = document.createElement("div"); top.className = "top";
      var left = document.createElement("span"); left.textContent = rows[i][1];
      var right = document.createElement("span");
      var remainingLabel = key === "ms" ? ms(Math.max(0, limit - used)) : (key === "costUSD" ? money(Math.max(0, limit - used)) : Math.max(0, Math.round(limit - used)));
      right.textContent = (key === "costUSD" ? money(used) : Math.round(used)) + " / " + (key === "costUSD" ? money(limit) : Math.round(limit)) + " · left " + remainingLabel;
      top.appendChild(left); top.appendChild(right);
      var track = document.createElement("div"); track.className = "track";
      var fill = document.createElement("div"); fill.className = "fill"; fill.style.width = pct.toFixed(1) + "%";
      track.appendChild(fill);
      bar.appendChild(top); bar.appendChild(track);
      root.appendChild(bar);
    }
    if (!root.children.length) {
      var empty = document.createElement("span"); empty.className = "empty"; empty.textContent = "Unavailable";
      root.appendChild(empty);
    }
  }

  function renderLog() {
    var root = document.getElementById("log");
    root.textContent = "";
    if (!log.length) {
      var empty = document.createElement("span"); empty.className = "empty"; empty.textContent = "No events observed yet.";
      root.appendChild(empty);
      return;
    }
    for (var i = log.length - 1; i >= 0; i--) {
      var e = log[i];
      var row = document.createElement("div"); row.className = "entry";
      var t = document.createElement("span"); t.className = "t";
      t.textContent = new Date(e.at || Date.now()).toLocaleTimeString();
      var body = document.createElement("span");
      var b = document.createElement("b"); b.textContent = e.type;
      body.appendChild(b);
      var extra = describe(e);
      if (extra) body.appendChild(document.createTextNode(" · " + extra));
      row.appendChild(t); row.appendChild(body);
      root.appendChild(row);
    }
  }

  function describe(e) {
    var bits = [];
    if (e.stepId) bits.push(e.stepId);
    if (e.role) bits.push(e.role);
    if (e.status) bits.push(e.status);
    if (e.state) bits.push(e.state);
    if (e.workflow) bits.push(e.workflow);
    if (e.passed !== undefined) bits.push(e.passed ? "passed" : "failed");
    if (e.model) bits.push(e.model);
    if (e.reason) bits.push(String(e.reason).slice(0, 60));
    return bits.join(" ");
  }

  function renderLinks() {
    var svg = document.getElementById("links");
    if (!state) return;
    var nodes = document.getElementById("nodes");
    if (!nodes.querySelector('[data-agent="manager"]')) return;
    var box = nodes.getBoundingClientRect();
    svg.setAttribute("viewBox", "0 0 " + box.width + " " + box.height);
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    var manager = nodes.querySelector('[data-agent="manager"]').getBoundingClientRect();
    var hot = {};
    var handoffs = state.handoffs || [];
    var last = handoffs[handoffs.length - 1];
    if (last) hot[last.from + ">" + last.to] = true;
    for (var i = 0; i < agents.length; i++) {
      var id = agents[i];
      if (id === "manager") continue;
      var el = nodes.querySelector('[data-agent="' + id + '"]');
      if (!el) continue;
      var r = el.getBoundingClientRect();
      var line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("x1", String(manager.left - box.left + manager.width / 2));
      line.setAttribute("y1", String(manager.top - box.top + manager.height / 2));
      line.setAttribute("x2", String(r.left - box.left + r.width / 2));
      line.setAttribute("y2", String(r.top - box.top + r.height / 2));
      var isHot = hot["manager>" + id] || hot[id + ">manager"];
      if (isHot) line.setAttribute("class", "hot");
      svg.appendChild(line);
    }
  }

  function render() {
    if (!state) return;
    document.getElementById("runMeta").innerHTML = "run <b>" + (state.run && state.run.id ? state.run.id.slice(0, 18) : "none") + "</b>";
    document.getElementById("updated").innerHTML = "updated <b>" + new Date(state.updatedAt || Date.now()).toLocaleTimeString() + "</b>";
    renderMetrics();
    renderNodes();
    renderBars();
    renderLog();
    renderLinks();
  }

  function ingest(events) {
    for (var i = 0; i < events.length; i++) {
      log.push(events[i]);
      if (log.length > 60) log.shift();
    }
  }

  function applySnapshot(payload) {
    state = payload;
    ingest((payload && payload.events) || []);
    render();
  }

  function connect() {
    if (timer) { clearTimeout(timer); timer = null; }
    setConn(attempts ? "reconnecting" : "offline", attempts ? "reconnecting (" + attempts + "/" + maxAttempts + ")" : "connecting");
    var url = "/events?token=" + encodeURIComponent(token);
    if (typeof EventSource === "undefined") { setConn("offline", "unsupported browser"); return; }
    source = new EventSource(url);
    source.addEventListener("snapshot", function (event) {
      attempts = 0;
      setConn("live", "live");
      try { applySnapshot(JSON.parse(event.data)); } catch (e) { /* ignore malformed frame */ }
    });
    source.addEventListener("swarm", function (event) {
      try {
        var payload = JSON.parse(event.data);
        if (payload && payload.state) { state = payload.state; }
        if (payload && payload.event) { ingest([payload.event]); }
        render();
      } catch (e) { /* ignore malformed frame */ }
    });
    source.onerror = function () {
      source.close();
      source = null;
      attempts += 1;
      if (attempts > maxAttempts) { setConn("offline", "disconnected"); return; }
      setConn("reconnecting", "reconnecting (" + attempts + "/" + maxAttempts + ")");
      var delay = Math.min(8000, 500 * Math.pow(2, attempts - 1));
      timer = setTimeout(connect, delay);
    };
  }

  window.addEventListener("resize", function () { try { renderLinks(); } catch (e) {} });
  document.addEventListener("visibilitychange", function () {
    // A hidden tab must not hold a stream open indefinitely.
    if (document.hidden && source) { source.close(); source = null; }
    if (!document.hidden && !source && attempts <= maxAttempts) connect();
  });
  connect();
})();
</script>
</body>
</html>`;
}
