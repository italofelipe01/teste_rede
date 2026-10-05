"use strict";

/*
 * Painel de Estabilidade de Rede: somente apresentação e interação.
 * Regras de domínio (quedas, diagnóstico, métricas, limites) chegam prontas da API.
 * Todo texto vindo de dados é inserido com textContent (sem innerHTML com dados).
 */

const API = {
  snapshot: "/api/snapshot",
  history: "/api/history",
  stream: "/api/stream",
  health: "/api/health",
  config: "/api/config",
  exportZip: "/api/export",
  sessions: "/api/sessions",
  rediscover: "/api/actions/rediscover",
  reset: "/api/actions/reset",
};
const MAX_HISTORY_POINTS = 200000;
const PALETTE = [
  "#0f766e", "#2563eb", "#d97706", "#7c3aed", "#db2777", "#0891b2",
  "#65a30d", "#dc2626", "#4f46e5", "#ca8a04", "#059669", "#9333ea",
];
const DEFAULT_THRESHOLDS = { loss_warn_pct: 2, loss_bad_pct: 10, jitter_warn_ms: 30 };
const EVENT_LABELS = {
  session_start: ["Sessão iniciada", "ok"],
  session_end: ["Sessão encerrada", ""],
  outage_start: ["Queda detectada", "bad"],
  outage_end: ["Conexão restabelecida", "ok"],
  route_discovered: ["Rota descoberta", ""],
  route_changed: ["Rota alterada", "warn"],
  target_resolved: ["Destino resolvido", ""],
  settings_changed: ["Configuração alterada", "warn"],
};
const PHASE_LABELS = {
  starting: "Iniciando",
  resolving: "Resolvendo destino",
  discovering: "Descobrindo rota",
  monitoring: "Monitorando",
  error: "Com erro",
  stopped: "Parado",
};
const PREFS_KEY = "netmon.dashboard.prefs";

const $ = (id) => document.getElementById(id);

const ui = {
  targetLabel: $("target-label"),
  conn: $("conn-indicator"),
  connText: $("conn-text"),
  banner: $("status-banner"),
  statusTitle: $("status-title"),
  statusDetail: $("status-detail"),
  statusTimer: $("status-timer"),
  alerts: $("alerts"),
  diagnosisCard: $("diagnosis-card"),
  diagnosisText: $("diagnosis-text"),
  latencyCanvas: $("latency-chart"),
  latencyOverlay: $("latency-overlay"),
  tooltip: $("chart-tooltip"),
  latencyMeta: $("latency-meta"),
  latencyRange: $("latency-range"),
  latencyMa: $("latency-ma-window"),
  latencyRaw: $("latency-show-raw"),
  latencyShowMa: $("latency-show-ma"),
  seriesList: $("latency-series-list"),
  statusCanvas: $("link-status-chart"),
  statusMeta: $("status-meta"),
  lossCanvas: $("loss-chart"),
  lossMeta: $("loss-meta"),
  lossBasis: $("loss-basis"),
  hideFullLoss: $("filter-hide-full-loss"),
  onlyLoss: $("filter-only-loss"),
  outagesBody: document.querySelector("#outages-table tbody"),
  outageMeta: $("outage-meta"),
  hopsBody: document.querySelector("#hops-table tbody"),
  snapshotMeta: $("snapshot-meta"),
  events: $("events-list"),
  eventsMeta: $("events-meta"),
  facts: $("facts"),
  access: $("access-box"),
  footer: $("footer-text"),
  settingsDialog: $("settings-dialog"),
  settingsForm: $("settings-form"),
  settingsError: $("settings-error"),
  settingsNote: $("settings-note"),
  presets: $("target-presets"),
  sessionsDialog: $("sessions-dialog"),
  sessionsBody: document.querySelector("#sessions-table tbody"),
  fileInput: $("file-input"),
  toast: $("toast"),
  moreMenu: $("more-menu"),
};

const state = {
  conn: "connecting",
  snapshot: null,
  history: [],
  lastSeq: 0,
  instance: null,
  sessionId: null,
  health: null,
  controlAllowed: false,
  clockOffsetMs: 0,
  lastMessageAt: 0,
  offline: false,
  eventSource: null,
  pollTimer: null,
  hiddenSeries: new Set(),
  colors: new Map(),
  latencyLayout: null,
  renderQueued: false,
};

// ------------------------------------------------------------------ utilidades

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "className") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "style") Object.assign(node.style, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function num(value) {
  if (value === null || value === undefined || value === "" || value === "-") return null;
  const parsed = typeof value === "number" ? value : Number.parseFloat(String(value).replace(",", "."));
  return Number.isFinite(parsed) ? parsed : null;
}

function fmt(value, digits = 2) {
  const n = num(value);
  if (n === null) return "-";
  return n.toLocaleString("pt-BR", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function fmtMs(value, digits = 1) {
  const n = num(value);
  return n === null ? "-" : `${fmt(n, digits)} ms`;
}

function fmtPct(value, digits = 2) {
  const n = num(value);
  return n === null ? "-" : `${fmt(n, digits)}%`;
}

function formatDuration(totalSeconds) {
  const sec = Math.max(0, Math.floor(num(totalSeconds) || 0));
  const days = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const hms = `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return days > 0 ? `${days}d ${hms}` : hms;
}

function parseTs(ts) {
  if (!ts) return null;
  const date = new Date(String(ts).replace(" ", "T"));
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatTs(ts, withDate = false) {
  const date = ts instanceof Date ? ts : parseTs(ts);
  if (!date) return ts ? String(ts) : "-";
  const time = date.toLocaleTimeString("pt-BR", { hour12: false });
  if (!withDate) return time;
  return `${date.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" })} ${time}`;
}

function serverNow() {
  return Date.now() - state.clockOffsetMs;
}

function thresholds() {
  return (state.snapshot && state.snapshot.thresholds) || DEFAULT_THRESHOLDS;
}

function lossClass(loss) {
  const n = num(loss);
  if (n === null) return "";
  const t = thresholds();
  if (n < t.loss_warn_pct) return "good";
  if (n < t.loss_bad_pct) return "mid";
  return "bad";
}

function colorFor(ip) {
  if (!state.colors.has(ip)) state.colors.set(ip, PALETTE[state.colors.size % PALETTE.length]);
  return state.colors.get(ip);
}

function themeColors() {
  const css = getComputedStyle(document.documentElement);
  const get = (name) => css.getPropertyValue(name).trim();
  return {
    ink: get("--ink"),
    muted: get("--muted"),
    grid: get("--grid"),
    ok: get("--ok"),
    warn: get("--warn"),
    bad: get("--bad"),
    badSoft: get("--bad-soft"),
    accent: get("--accent"),
    font: get("--font"),
  };
}

function setupCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(120, Math.round(rect.width));
  const height = Math.max(20, Math.round(rect.height));
  if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}

function niceStep(maxValue, ticks) {
  const raw = maxValue > 0 ? maxValue / ticks : 1;
  const exp = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const factor of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) {
    if (factor * exp >= raw) return factor * exp;
  }
  return 10 * exp;
}

function loadPrefs() {
  try {
    const prefs = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}");
    if (prefs.range) ui.latencyRange.value = prefs.range;
    if (prefs.ma) ui.latencyMa.value = prefs.ma;
    if (typeof prefs.raw === "boolean") ui.latencyRaw.checked = prefs.raw;
    if (typeof prefs.showMa === "boolean") ui.latencyShowMa.checked = prefs.showMa;
    if (prefs.lossBasis) ui.lossBasis.value = prefs.lossBasis;
    if (typeof prefs.hideFull === "boolean") ui.hideFullLoss.checked = prefs.hideFull;
    if (typeof prefs.onlyLoss === "boolean") ui.onlyLoss.checked = prefs.onlyLoss;
    if (Array.isArray(prefs.hidden)) state.hiddenSeries = new Set(prefs.hidden);
  } catch (_) {
    /* preferências são opcionais */
  }
}

function savePrefs() {
  try {
    localStorage.setItem(
      PREFS_KEY,
      JSON.stringify({
        range: ui.latencyRange.value,
        ma: ui.latencyMa.value,
        raw: ui.latencyRaw.checked,
        showMa: ui.latencyShowMa.checked,
        lossBasis: ui.lossBasis.value,
        hideFull: ui.hideFullLoss.checked,
        onlyLoss: ui.onlyLoss.checked,
        hidden: [...state.hiddenSeries],
      }),
    );
  } catch (_) {
    /* armazenamento indisponível */
  }
}

let toastTimer = null;
function toast(message, ms = 4000) {
  ui.toast.textContent = message;
  ui.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    ui.toast.hidden = true;
  }, ms);
}

async function getJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

async function postJson(url, body = {}) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch (_) {
    /* corpo vazio */
  }
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

// ------------------------------------------------------------------ conexão com a API

function setConn(mode) {
  state.conn = mode;
  const labels = {
    connecting: "Conectando...",
    live: "Ao vivo",
    polling: "Atualizando (modo compatível)",
    lost: "Reconectando...",
    stale: "Sem atualizações",
    offline: "Modo offline",
  };
  ui.conn.className = `chip conn conn-${mode === "stale" ? "lost" : mode}`;
  ui.connText.textContent = labels[mode] || mode;
}

function cursor() {
  return state.instance ? `${state.instance}:${state.lastSeq}` : String(state.lastSeq);
}

function applyMessage(msg) {
  if (!msg || state.offline) return;
  const changedOrigin = (msg.instance && msg.instance !== state.instance) || (msg.session_id && msg.session_id !== state.sessionId);
  if (msg.reset || changedOrigin) {
    state.history = [];
    state.lastSeq = 0;
  }
  for (const point of msg.points || []) {
    if (point.seq > state.lastSeq) {
      state.history.push(point);
      state.lastSeq = point.seq;
    }
  }
  if (state.history.length > MAX_HISTORY_POINTS) state.history.splice(0, state.history.length - MAX_HISTORY_POINTS);
  if (typeof msg.latest_seq === "number") state.lastSeq = Math.max(state.lastSeq, msg.latest_seq);
  if (msg.instance) state.instance = msg.instance;
  if (msg.session_id !== undefined) state.sessionId = msg.session_id;
  if (msg.snapshot) {
    state.snapshot = msg.snapshot;
    const serverTime = parseTs(msg.snapshot.server_time);
    if (serverTime) state.clockOffsetMs = Date.now() - serverTime.getTime();
  }
  state.lastMessageAt = Date.now();
  scheduleRender();
}

async function boot() {
  loadPrefs();
  bindUi();
  setInterval(tick, 1000);
  if (location.protocol === "file:") {
    enterOffline("Painel aberto como arquivo local: importe as evidências (.zip, .csv, .txt) de uma sessão.");
    return;
  }
  await connect();
}

async function connect() {
  try {
    state.health = await getJson(API.health);
    state.controlAllowed = Boolean(state.health.control_allowed);
    const [snapshot, history] = await Promise.all([getJson(API.snapshot), getJson(`${API.history}?since=0`)]);
    applyMessage({ ...history, snapshot });
    updateControls();
    startStream();
  } catch (_) {
    setConn("lost");
    renderStatus();
    setTimeout(() => {
      if (!state.offline) connect();
    }, 3000);
  }
}

function startStream() {
  if (!("EventSource" in window)) {
    startPolling();
    return;
  }
  let received = false;
  let failures = 0;
  const source = new EventSource(`${API.stream}?since=${encodeURIComponent(cursor())}`);
  state.eventSource = source;
  source.onopen = () => setConn("live");
  source.onmessage = (event) => {
    received = true;
    failures = 0;
    setConn("live");
    try {
      applyMessage(JSON.parse(event.data));
    } catch (_) {
      /* mensagem inválida: ignora */
    }
  };
  source.onerror = () => {
    failures += 1;
    setConn("lost");
    if (!received && failures >= 3) {
      source.close();
      state.eventSource = null;
      startPolling();
    }
  };
}

function startPolling() {
  clearTimeout(state.pollTimer);
  const poll = async () => {
    try {
      const [snapshot, history] = await Promise.all([
        getJson(API.snapshot),
        getJson(`${API.history}?since=${state.lastSeq}&instance=${encodeURIComponent(state.instance || "")}`),
      ]);
      applyMessage({ ...history, snapshot });
      setConn("polling");
    } catch (_) {
      setConn("lost");
    }
    const interval = num(state.snapshot && state.snapshot.settings && state.snapshot.settings.interval_sec) || 2;
    state.pollTimer = setTimeout(poll, Math.min(5000, Math.max(1000, interval * 1000)));
  };
  poll();
}

function tick() {
  if (state.offline) return;
  const interval = num(state.snapshot && state.snapshot.settings && state.snapshot.settings.interval_sec) || 1;
  const staleMs = Math.max(10000, interval * 3000 + 5000);
  if ((state.conn === "live" || state.conn === "polling") && state.lastMessageAt && Date.now() - state.lastMessageAt > staleMs) {
    setConn("stale");
  }
  renderStatus();
  renderSessionClock();
}

// ------------------------------------------------------------------ renderização

function scheduleRender() {
  if (state.renderQueued) return;
  state.renderQueued = true;
  requestAnimationFrame(() => {
    state.renderQueued = false;
    render();
  });
}

function render() {
  const snap = state.snapshot;
  ui.targetLabel.textContent = snap ? targetText(snap) : "-";
  renderStatus();
  renderAlerts();
  renderKpis();
  renderDiagnosis();
  renderSeriesList();
  renderLatency();
  renderLoss();
  renderOutages();
  renderHops();
  renderEvents();
  renderFacts();
  renderFooter();
}

function targetText(snap) {
  const target = snap.target || {};
  const input = target.input || snap.destino || "-";
  if (target.ip && target.ip !== input) return `${input} (${target.ip})`;
  if (target.name && target.name !== "-") return `${input} (${target.name})`;
  return input;
}

function destinationHop(snap) {
  const hops = (snap && snap.hops) || [];
  return hops.find((hop) => hop.Is_Destination) || hops[hops.length - 1] || null;
}

function renderStatus() {
  const snap = state.snapshot;
  let cls = "st-unknown";
  let title = "Aguardando dados...";
  let detail = "Conectando ao monitor.";
  let timer = "";

  if (state.offline) {
    cls = "st-info";
    title = "Modo offline";
    detail = snap ? `Sessão importada: ${targetText(snap)}.` : "Importe as evidências de uma sessão pelo menu Mais.";
  } else if (state.conn === "lost" || state.conn === "stale") {
    cls = "st-error";
    title = state.conn === "lost" ? "Sem conexão com o monitor" : "Monitor sem atualizações";
    detail = "Os dados exibidos podem estar desatualizados. Verifique se o programa continua aberto; a reconexão é automática.";
  } else if (snap) {
    const status = snap.status || {};
    const dest = destinationHop(snap);
    const phase = PHASE_LABELS[snap.phase] || snap.phase;
    if (status.link === "down") {
      cls = "st-down";
      title = "Queda em andamento";
      const since = formatTs(status.down_since);
      const where = status.breakpoint_hop
        ? ` A rota responde até H${status.breakpoint_hop} (${status.breakpoint_ip}).`
        : status.scope === "local"
          ? " Nenhum salto responde (rede local, roteador ou acesso do provedor)."
          : "";
      detail = `Destino sem resposta desde ${since}.${where}`;
      const start = parseTs(status.down_since);
      timer = start ? formatDuration((serverNow() - start.getTime()) / 1000) : "";
    } else if (status.link === "checking") {
      cls = "st-checking";
      title = "Instável: verificando";
      detail = `Destino sem resposta há ${status.pending_failures} ciclo(s); a queda é confirmada após ${snap.settings.outage_min_cycles}.`;
    } else if (status.link === "up") {
      cls = "st-up";
      title = "Online";
      const last = dest ? dest.Last : null;
      detail = `Destino respondendo${last !== null && last !== undefined ? ` em ${fmtMs(last)}` : ""} · ${phase} · ciclo ${snap.cycle}`;
      const lastUpdate = parseTs(snap.last_update);
      if (lastUpdate) detail += ` · atualizado às ${formatTs(lastUpdate)}`;
    } else if (snap.phase === "error") {
      cls = "st-error";
      title = "Monitor com pendência";
      detail = snap.error || "Erro desconhecido.";
    } else {
      cls = "st-info";
      title = phase || "Iniciando";
      detail = snap.route && snap.route.discovering ? "Descobrindo a rota até o destino..." : "Preparando as primeiras medições...";
    }
  }
  ui.banner.className = `status-banner ${cls}`;
  ui.statusTitle.textContent = title;
  ui.statusDetail.textContent = detail;
  ui.statusTimer.textContent = timer;
}

function renderAlerts() {
  const snap = state.snapshot;
  const items = [];
  if (state.offline) {
    items.push(["info", "Visualizando evidências importadas.", { text: "Voltar ao vivo", action: () => location.reload() }]);
  }
  if (snap && !state.offline) {
    if (snap.error && snap.status && snap.status.link && snap.status.link !== "unknown") items.push(["error", snap.error]);
    for (const warning of snap.warnings || []) items.push(["warn", warning]);
  }
  ui.alerts.replaceChildren(
    ...items.map(([kind, text, button]) =>
      el(
        "div",
        { className: `alert alert-${kind}` },
        text,
        button ? el("button", { className: "link-btn", type: "button", onclick: button.action, text: ` ${button.text}` }) : null,
      ),
    ),
  );
}

function renderKpis() {
  const snap = state.snapshot;
  if (!snap) return;
  const summary = snap.summary || {};
  const status = snap.status || {};
  const dest = destinationHop(snap);
  const t = thresholds();

  const availability = num(summary.availability_pct);
  const availEl = $("kpi-availability");
  availEl.textContent = availability === null ? "-" : `${fmt(availability, availability >= 99.995 ? 2 : 3)}%`;
  availEl.className = availability === null ? "" : availability >= 99.9 ? "good" : availability >= 99 ? "mid" : "bad";
  $("kpi-availability-sub").textContent = "tempo com o destino respondendo";

  renderSessionClock();

  const falls = num(summary.outage_count) || 0;
  const fallsEl = $("kpi-falls");
  fallsEl.textContent = String(falls + (status.link === "down" ? 1 : 0));
  fallsEl.className = falls > 0 || status.link === "down" ? "bad" : "good";
  $("kpi-falls-sub").textContent = status.link === "down" ? "inclui a queda em andamento" : `critério: ${snap.settings ? snap.settings.outage_min_cycles : "-"} ciclo(s) sem resposta`;

  $("kpi-downtime").textContent = formatDuration(summary.total_downtime_sec);
  const lastOutage = (snap.outages || [])[snap.outages ? snap.outages.length - 1 : 0];
  $("kpi-downtime-sub").textContent = lastOutage ? `última queda: ${formatTs(lastOutage.Start, true)}` : "nenhuma queda encerrada";

  const latencyEl = $("kpi-latency");
  latencyEl.textContent = dest ? fmtMs(dest.Recent_Avrg ?? dest.Avrg) : "-";
  const current = dest && num(dest.Last) !== null ? fmtMs(dest.Last) : "sem resposta";
  $("kpi-latency-sub").textContent = dest ? `atual ${current} · pior ${fmtMs(dest.Worst)}` : "-";

  const jitterEl = $("kpi-jitter");
  jitterEl.textContent = dest ? fmtMs(dest.Jitter) : "-";
  jitterEl.className = dest && num(dest.Jitter) !== null ? (num(dest.Jitter) >= t.jitter_warn_ms ? "mid" : "good") : "";

  const lossEl = $("kpi-dest-loss");
  lossEl.textContent = dest ? fmtPct(dest.Loss_Pct) : "-";
  lossEl.className = dest ? lossClass(dest.Loss_Pct) : "";
  $("kpi-dest-loss-sub").textContent = dest ? `recente ${fmtPct(dest.Recent_Loss_Pct)} · ${dest.Sent - dest.Recv} de ${dest.Sent} pkt` : "-";

  const hops = snap.hops || [];
  $("kpi-hops").textContent = String(hops.length);
  const route = snap.route || {};
  let routeText = route.complete ? `rota completa via ${route.source}` : route.discovering ? "descobrindo rota..." : "rota parcial";
  if (state.offline && !snap.route) routeText = "dados importados";
  $("kpi-hops-sub").textContent = routeText;
}

function renderSessionClock() {
  const snap = state.snapshot;
  if (!snap || !snap.summary) return;
  const start = parseTs(snap.summary.monitoring_start);
  let seconds = num(snap.summary.session_sec);
  if (start && !state.offline && snap.phase !== "stopped" && (state.conn === "live" || state.conn === "polling")) {
    seconds = (serverNow() - start.getTime()) / 1000;
  }
  $("kpi-session-time").textContent = formatDuration(seconds);
  $("kpi-session-sub").textContent = start ? `desde ${formatTs(start, true)}` : "-";
}

function renderDiagnosis() {
  const diagnosis = state.snapshot && state.snapshot.diagnosis;
  if (!diagnosis) {
    if (state.offline && state.snapshot) {
      ui.diagnosisCard.className = "card diagnosis dg-info";
      ui.diagnosisText.textContent = "Diagnóstico disponível ao vivo ou ao importar o pacote .zip exportado pelo painel.";
    }
    return;
  }
  ui.diagnosisCard.className = `card diagnosis dg-${diagnosis.level || "info"}`;
  ui.diagnosisText.textContent = diagnosis.message;
}

// ------------------------------------------------------------------ gráfico de latência

function seriesDefs() {
  const hops = (state.snapshot && state.snapshot.hops) || [];
  return hops.map((hop) => ({
    ip: hop.Host_IP,
    label: `H${hop.Hop}${hop.Is_Destination ? " destino" : ""}`,
    short: `H${hop.Hop}`,
    name: hop.Host_Name,
    dest: Boolean(hop.Is_Destination),
    responsive: (num(hop.Recv) || 0) > 0,
    color: colorFor(hop.Host_IP),
  }));
}

function renderSeriesList() {
  const defs = seriesDefs();
  ui.seriesList.replaceChildren(
    ...defs.map((def) =>
      el(
        "button",
        {
          type: "button",
          className: "series-toggle",
          "aria-pressed": state.hiddenSeries.has(def.ip) ? "false" : "true",
          title: `${def.ip}${def.name && def.name !== "-" ? ` (${def.name})` : ""}${def.responsive ? "" : " · sem resposta até agora"}`,
          onclick: () => {
            if (state.hiddenSeries.has(def.ip)) state.hiddenSeries.delete(def.ip);
            else state.hiddenSeries.add(def.ip);
            savePrefs();
            scheduleRender();
          },
        },
        el("span", { className: "sw", style: { background: def.color } }),
        `${def.label} · ${def.ip}`,
      ),
    ),
  );
}

function windowPoints() {
  const points = state.history;
  const range = Number.parseInt(ui.latencyRange.value, 10) || 0;
  if (!points.length || !range) return points;
  const start = points[points.length - 1].t - range * 1000;
  let lo = 0;
  let hi = points.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid].t < start) lo = mid + 1;
    else hi = mid;
  }
  return points.slice(lo);
}

function movingAverage(values, size) {
  if (size <= 1) return values.slice();
  const out = new Array(values.length);
  let sum = 0;
  let count = 0;
  for (let i = 0; i < values.length; i++) {
    const add = values[i];
    if (add !== null) {
      sum += add;
      count += 1;
    }
    if (i >= size) {
      const drop = values[i - size];
      if (drop !== null) {
        sum -= drop;
        count -= 1;
      }
    }
    out[i] = count > 0 ? sum / count : null;
  }
  return out;
}

function bucketize(times, arrays, buckets) {
  if (times.length <= buckets * 1.5) return { times, arrays };
  const t0 = times[0];
  const span = Math.max(1, times[times.length - 1] - t0);
  const outTimes = [];
  const sums = arrays.map(() => []);
  const counts = arrays.map(() => []);
  const tSum = [];
  const tCount = [];
  for (let i = 0; i < times.length; i++) {
    const b = Math.min(buckets - 1, Math.floor(((times[i] - t0) / span) * buckets));
    tSum[b] = (tSum[b] || 0) + times[i];
    tCount[b] = (tCount[b] || 0) + 1;
    arrays.forEach((values, s) => {
      const v = values[i];
      if (v !== null) {
        sums[s][b] = (sums[s][b] || 0) + v;
        counts[s][b] = (counts[s][b] || 0) + 1;
      }
    });
  }
  const outArrays = arrays.map(() => []);
  for (let b = 0; b < buckets; b++) {
    if (!tCount[b]) continue;
    outTimes.push(tSum[b] / tCount[b]);
    arrays.forEach((_, s) => outArrays[s].push(counts[s][b] ? sums[s][b] / counts[s][b] : null));
  }
  return { times: outTimes, arrays: outArrays };
}

function renderLatency() {
  const colors = themeColors();
  const { ctx, width, height } = setupCanvas(ui.latencyCanvas);
  setupCanvas(ui.latencyOverlay);
  const pad = { top: 12, right: 16, bottom: 28, left: 56 };
  const w = width - pad.left - pad.right;
  const h = height - pad.top - pad.bottom;
  const points = windowPoints();
  const defs = seriesDefs().filter((def) => !state.hiddenSeries.has(def.ip));
  const showRaw = ui.latencyRaw.checked;
  const showMa = ui.latencyShowMa.checked;
  const maSize = Number.parseInt(ui.latencyMa.value, 10) || 1;
  ctx.font = `12px ${colors.font}`;
  state.latencyLayout = null;

  if (!points.length || !defs.length || (!showRaw && !showMa)) {
    ctx.fillStyle = colors.muted;
    ctx.fillText(points.length ? "Selecione ao menos um salto e um tipo de linha." : "Sem histórico de latência ainda.", pad.left, pad.top + 24);
    ui.latencyMeta.textContent = "Sem histórico";
    drawStatusStrip([], 0, 1, pad);
    return;
  }

  const times = points.map((p) => p.t);
  const raw = defs.map((def) => points.map((p) => (p.rtt && p.rtt[def.ip] !== undefined ? p.rtt[def.ip] : null)));
  const ma = raw.map((values) => movingAverage(values, maSize));
  const reduced = bucketize(times, raw.concat(ma), Math.max(60, Math.floor(w)));
  const plotTimes = reduced.times;
  const plotRaw = reduced.arrays.slice(0, defs.length);
  const plotMa = reduced.arrays.slice(defs.length);

  const t0 = times[0];
  const t1 = Math.max(times[times.length - 1], t0 + 1000);
  const span = t1 - t0;
  const xOf = (t) => pad.left + ((t - t0) / span) * w;

  let maxY = 0;
  (showRaw ? plotRaw : plotMa).concat(showMa ? plotMa : []).forEach((values) =>
    values.forEach((v) => {
      if (v !== null && v > maxY) maxY = v;
    }),
  );
  const yStep = niceStep(Math.max(maxY * 1.08, 1), 4);
  maxY = yStep * 4;
  const yOf = (v) => pad.top + h - (v / maxY) * h;
  const yDigits = yStep < 1 ? 2 : Number.isInteger(yStep) ? 0 : 1;

  // Faixas de queda (pontos sem resposta do destino).
  ctx.fillStyle = colors.badSoft;
  for (let i = 0; i < points.length; i++) {
    if (points[i].up) continue;
    let j = i;
    while (j + 1 < points.length && !points[j + 1].up) j++;
    const xStart = xOf(points[i].t);
    const xEnd = j + 1 < points.length ? xOf(points[j + 1].t) : xOf(points[j].t) + 2;
    ctx.fillRect(xStart, pad.top, Math.max(2, xEnd - xStart), h);
    i = j;
  }

  // Grade e eixos.
  ctx.strokeStyle = colors.grid;
  ctx.fillStyle = colors.muted;
  ctx.lineWidth = 1;
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i++) {
    const value = (maxY / 4) * i;
    const y = Math.round(yOf(value)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(pad.left + w, y);
    ctx.stroke();
    ctx.fillText(`${fmt(value, yDigits)} ms`, pad.left - 8, y + 4);
  }
  ctx.textAlign = "center";
  const ticks = Math.max(2, Math.min(6, Math.floor(w / 120)));
  for (let i = 0; i <= ticks; i++) {
    const t = t0 + (span * i) / ticks;
    const label = formatTs(new Date(t));
    ctx.fillText(label, Math.min(pad.left + w - 24, Math.max(pad.left + 24, xOf(t))), pad.top + h + 18);
  }

  const drawLine = (values, color, lineWidth, alpha) => {
    ctx.strokeStyle = color;
    ctx.globalAlpha = alpha;
    ctx.lineWidth = lineWidth;
    ctx.lineJoin = "round";
    ctx.beginPath();
    let open = false;
    values.forEach((v, i) => {
      if (v === null) {
        open = false;
        return;
      }
      const x = xOf(plotTimes[i]);
      const y = yOf(v);
      if (!open) {
        ctx.moveTo(x, y);
        open = true;
      } else {
        ctx.lineTo(x, y);
      }
    });
    ctx.stroke();
    ctx.globalAlpha = 1;
  };
  defs.forEach((def, s) => {
    if (showRaw) drawLine(plotRaw[s], def.color, def.dest ? 1.4 : 1, showMa ? 0.3 : 0.9);
    if (showMa) drawLine(plotMa[s], def.color, def.dest ? 2.8 : 1.8, 1);
  });

  state.latencyLayout = { pad, w, h, t0, span, xOf, yOf, points, defs, raw, ma, showRaw, showMa };
  const visible = defs.filter((_, s) => raw[s].some((v) => v !== null)).length;
  ui.latencyMeta.textContent = `${visible} salto(s) com dados · ${points.length} amostra(s) · ${formatTs(new Date(t0))}–${formatTs(new Date(t1))}`;
  drawStatusStrip(points, t0, span, pad);
}

function drawStatusStrip(points, t0, span, pad) {
  const colors = themeColors();
  const { ctx, width, height } = setupCanvas(ui.statusCanvas);
  const w = width - pad.left - pad.right;
  ctx.fillStyle = colors.grid;
  ctx.fillRect(pad.left, 4, w, height - 8);
  if (!points.length) {
    ui.statusMeta.textContent = "Status do destino no tempo: sem dados";
    return;
  }
  const xOf = (t) => pad.left + ((t - t0) / span) * w;
  let downs = 0;
  for (let i = 0; i < points.length; i++) {
    const x = xOf(points[i].t);
    const next = i + 1 < points.length ? xOf(points[i + 1].t) : x + 2;
    ctx.fillStyle = points[i].up ? colors.ok : colors.bad;
    if (!points[i].up) downs += 1;
    ctx.fillRect(x, 4, Math.max(1, next - x + 0.5), height - 8);
  }
  ui.statusMeta.textContent = downs
    ? `Status do destino: ${downs} ciclo(s) sem resposta na janela`
    : "Status do destino: respondeu em todos os ciclos da janela";
}

function onLatencyHover(event) {
  const layout = state.latencyLayout;
  const { ctx } = setupCanvas(ui.latencyOverlay);
  if (!layout) {
    ui.tooltip.hidden = true;
    return;
  }
  const rect = ui.latencyCanvas.getBoundingClientRect();
  const x = event.clientX - rect.left;
  if (x < layout.pad.left || x > layout.pad.left + layout.w) {
    ui.tooltip.hidden = true;
    return;
  }
  const t = layout.t0 + ((x - layout.pad.left) / layout.w) * layout.span;
  const points = layout.points;
  let lo = 0;
  let hi = points.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid].t < t) lo = mid + 1;
    else hi = mid;
  }
  if (lo > 0 && Math.abs(points[lo - 1].t - t) < Math.abs(points[lo].t - t)) lo -= 1;
  const point = points[lo];
  const px = layout.xOf(point.t);
  const colors = themeColors();
  ctx.strokeStyle = colors.muted;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(px, layout.pad.top);
  ctx.lineTo(px, layout.pad.top + layout.h);
  ctx.stroke();
  ctx.setLineDash([]);

  const rows = [];
  layout.defs.forEach((def, s) => {
    const rawValue = layout.raw[s][lo];
    const maValue = layout.ma[s][lo];
    const shown = layout.showMa ? maValue : rawValue;
    if (shown !== null) {
      ctx.fillStyle = def.color;
      ctx.beginPath();
      ctx.arc(px, layout.yOf(shown), 3.5, 0, Math.PI * 2);
      ctx.fill();
    }
    const valueText = rawValue === null ? "sem resposta" : fmtMs(rawValue, 2);
    const maText = layout.showMa && maValue !== null ? ` (média ${fmtMs(maValue, 1)})` : "";
    rows.push(
      el(
        "div",
        { className: "tt-row" },
        el("span", {}, el("i", { className: "sw", style: { background: def.color } }), def.short),
        el("span", { text: valueText + maText }),
      ),
    );
  });
  ui.tooltip.replaceChildren(
    el("div", { className: "tt-time", text: `${formatTs(new Date(point.t), true)} · ${point.up ? "destino respondeu" : "destino sem resposta"}` }),
    ...rows,
  );
  ui.tooltip.hidden = false;
  const tipWidth = ui.tooltip.offsetWidth;
  const left = px + 14 + tipWidth > rect.width ? px - tipWidth - 14 : px + 14;
  ui.tooltip.style.left = `${Math.max(0, left)}px`;
  ui.tooltip.style.top = `${layout.pad.top + 4}px`;
}

function clearLatencyHover() {
  setupCanvas(ui.latencyOverlay);
  ui.tooltip.hidden = true;
}

// ------------------------------------------------------------------ perda por salto

function renderLoss() {
  const colors = themeColors();
  const { ctx, width, height } = setupCanvas(ui.lossCanvas);
  const snap = state.snapshot;
  const basis = ui.lossBasis.value;
  const key = basis === "recent" ? "Recent_Loss_Pct" : "Loss_Pct";
  const rows = ((snap && snap.hops) || []).filter((hop) => {
    const loss = num(hop[key]) ?? num(hop.Loss_Pct) ?? 0;
    if (ui.hideFullLoss.checked && loss >= 100) return false;
    if (ui.onlyLoss.checked && loss <= 0) return false;
    return true;
  });
  ctx.font = `12px ${colors.font}`;
  const pad = { top: 22, right: 10, bottom: 30, left: 44 };
  const w = width - pad.left - pad.right;
  const h = height - pad.top - pad.bottom;
  ui.lossMeta.textContent = basis === "recent" ? "últimos ciclos (janela recente)" : "desde o início da sessão";
  if (!rows.length) {
    ctx.fillStyle = colors.muted;
    ctx.fillText("Sem saltos para exibir com os filtros atuais.", pad.left, pad.top + 20);
    return;
  }
  ctx.strokeStyle = colors.grid;
  ctx.fillStyle = colors.muted;
  ctx.textAlign = "right";
  for (let tick = 0; tick <= 100; tick += 25) {
    const y = Math.round(pad.top + h - (tick / 100) * h) + 0.5;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(pad.left + w, y);
    ctx.stroke();
    ctx.fillText(`${tick}%`, pad.left - 8, y + 4);
  }
  const slot = w / rows.length;
  const barWidth = Math.max(6, Math.min(48, slot * 0.68));
  ctx.textAlign = "center";
  rows.forEach((hop, i) => {
    const loss = Math.min(100, Math.max(0, num(hop[key]) ?? num(hop.Loss_Pct) ?? 0));
    const cx = pad.left + slot * i + slot / 2;
    const barHeight = Math.max(loss > 0 ? 2 : 0, (loss / 100) * h);
    const cls = lossClass(loss);
    const noReply = (num(hop.Recv) || 0) === 0;
    ctx.fillStyle = noReply ? colors.grid : cls === "good" ? colors.ok : cls === "mid" ? colors.warn : colors.bad;
    ctx.fillRect(cx - barWidth / 2, pad.top + h - barHeight, barWidth, barHeight);
    ctx.fillStyle = colors.muted;
    if (slot > 34) ctx.fillText(noReply ? "s/ resp." : `${fmt(loss, loss >= 10 ? 0 : 1)}%`, cx, pad.top + h - barHeight - 6);
    ctx.fillStyle = hop.Is_Destination ? colors.accent : colors.ink;
    ctx.fillText(`H${hop.Hop}`, cx, pad.top + h + 18);
  });
}

// ------------------------------------------------------------------ tabelas e listas

function scopeText(outage) {
  if (outage.Scope === "external" && outage.Last_OK_Hop !== null && outage.Last_OK_Hop !== undefined && outage.Last_OK_Hop !== "-") {
    return `Após H${outage.Last_OK_Hop} (${outage.Last_OK_IP})`;
  }
  if (outage.Scope === "local") return "Nenhum salto respondeu (rede local/roteador/acesso)";
  return "Indeterminado";
}

function renderOutages() {
  const snap = state.snapshot;
  const outages = ((snap && snap.outages) || []).slice().reverse();
  const status = (snap && snap.status) || {};
  const rows = [];
  if (status.link === "down") {
    const start = parseTs(status.down_since);
    rows.push(
      el(
        "tr",
        { className: "ongoing" },
        el("td", { text: "agora" }),
        el("td", { text: formatTs(status.down_since, true) }),
        el("td", { text: "em andamento" }),
        el("td", { text: start ? formatDuration((serverNow() - start.getTime()) / 1000) : "-" }),
        el("td", {
          text: status.breakpoint_hop ? `Após H${status.breakpoint_hop} (${status.breakpoint_ip})` : status.scope === "local" ? "Nenhum salto respondeu" : "-",
        }),
      ),
    );
  }
  for (const outage of outages) {
    rows.push(
      el(
        "tr",
        {},
        el("td", { text: outage.Outage_ID }),
        el("td", { text: formatTs(outage.Start, true) }),
        el("td", { text: formatTs(outage.End, true) }),
        el("td", { text: formatDuration(outage.Duration_Sec), title: `${fmt(outage.Duration_Sec, 1)} s · ${outage.Down_Cycles} ciclo(s)` }),
        el("td", { text: scopeText(outage) }),
      ),
    );
  }
  if (!rows.length) rows.push(el("tr", {}, el("td", { colspan: 5, className: "empty", text: "Nenhuma queda registrada nesta sessão." })));
  ui.outagesBody.replaceChildren(...rows);
  ui.outageMeta.textContent = `${outages.length} encerrada(s)${status.link === "down" ? " + 1 em andamento" : ""}`;
}

function renderHops() {
  const snap = state.snapshot;
  const hops = (snap && snap.hops) || [];
  const cell = (value, extra = {}) => el("td", { className: "num", ...extra, text: value });
  const rows = hops.map((hop) => {
    const noReply = (num(hop.Recv) || 0) === 0 && (num(hop.Sent) || 0) > 0;
    return el(
      "tr",
      {
        className: hop.Is_Destination ? "is-dest" : noReply ? "no-reply" : "",
        title: noReply && !hop.Is_Destination ? "Este roteador não responde a ping (comum e normalmente inofensivo)." : null,
      },
      el("td", {}, `H${hop.Hop}`, hop.Is_Destination ? el("span", { className: "badge", text: "destino" }) : null),
      el("td", { text: hop.Host_IP }),
      el("td", { text: hop.Host_Name || "-" }),
      cell(hop.Sent ?? "-"),
      cell(hop.Recv ?? "-"),
      cell(fmt(hop.Loss_Pct), { className: `num ${lossClass(hop.Loss_Pct)}` }),
      cell(hop.Recent_Loss_Pct === null || hop.Recent_Loss_Pct === undefined ? "-" : fmt(hop.Recent_Loss_Pct), {
        className: `num ${lossClass(hop.Recent_Loss_Pct)}`,
      }),
      cell(fmt(hop.Best)),
      cell(fmt(hop.Worst)),
      cell(fmt(hop.Avrg)),
      cell(fmt(hop.Last)),
      cell(fmt(hop.Jitter)),
    );
  });
  if (!rows.length) rows.push(el("tr", {}, el("td", { colspan: 12, className: "empty", text: "Aguardando a descoberta da rota." })));
  ui.hopsBody.replaceChildren(...rows);
  if (snap) {
    ui.snapshotMeta.textContent = state.offline
      ? "Dados importados"
      : `Ciclo ${snap.cycle || 0} · atualizado às ${formatTs(snap.last_update ? new Date(snap.last_update) : snap.server_time)}`;
  }
}

function renderEvents() {
  const events = ((state.snapshot && state.snapshot.events) || []).slice().reverse();
  ui.events.replaceChildren(
    ...events.map((event) => {
      const [label, tone] = EVENT_LABELS[event.event] || [event.event, ""];
      return el(
        "li",
        { className: tone ? `ev-${tone}` : "" },
        el("time", { text: formatTs(event.ts, true) }),
        el("span", { className: "ev-kind", text: label }),
        el("span", { className: "ev-detail", text: event.detail }),
      );
    }),
  );
  if (!events.length) ui.events.replaceChildren(el("li", { className: "empty", text: "Sem eventos." }));
  ui.eventsMeta.textContent = `${events.length} evento(s)`;
}

function renderFacts() {
  const snap = state.snapshot;
  const health = state.health || {};
  const env = health.environment || {};
  const route = (snap && snap.route) || {};
  const target = (snap && snap.target) || {};
  const facts = [];
  if (snap) {
    facts.push(["Destino", `${target.input || snap.destino || "-"}${target.ip ? ` → ${target.ip}` : ""}${target.name && target.name !== "-" ? ` (${target.name})` : ""}`]);
    const routeParts = [];
    if (route.source && route.source !== "-") routeParts.push(`via ${route.source}`);
    routeParts.push(route.complete ? "completa" : "parcial");
    if (route.discovered_at) routeParts.push(`descoberta às ${formatTs(route.discovered_at)}`);
    routeParts.push(`${route.changes || 0} mudança(s)`);
    facts.push(["Rota", routeParts.join(" · ")]);
    if (route.discovering) facts.push(["Próxima verificação", "em andamento"]);
    else if (num(route.next_refresh_sec) !== null) facts.push(["Próxima verificação", `em ${formatDuration(route.next_refresh_sec)}`]);
    const settings = snap.settings || {};
    facts.push(["Medição", `a cada ${fmt(settings.interval_sec, 1)} s · timeout ${settings.timeout_ms} ms · queda após ${settings.outage_min_cycles} ciclo(s)`]);
    facts.push(["Estado do monitor", PHASE_LABELS[snap.phase] || snap.phase || "-"]);
  }
  if (env.platform) facts.push(["Sistema", `${env.platform} · Python ${env.python} · ping ${env.ping_style}`]);
  if (health.data_dir) facts.push(["Evidências em", health.data_dir]);
  ui.facts.replaceChildren(...facts.flatMap(([key, value]) => [el("dt", { text: key }), el("dd", { text: value })]));

  const urls = health.access_urls || [];
  if (state.offline) {
    ui.access.replaceChildren();
  } else if (urls.length > 1) {
    ui.access.replaceChildren(
      el("span", { text: "Acesso por outros dispositivos da rede: " }),
      ...urls.slice(1).flatMap((url, i) => [i ? ", " : "", el("a", { href: url, text: url })]),
    );
  } else {
    ui.access.replaceChildren(
      el("small", {}, "Para abrir o painel em outros dispositivos da rede, inicie com ", el("code", { text: "--lan" }), " ou defina ", el("code", { text: '"host": "0.0.0.0"' }), " no config.json."),
    );
  }
}

function renderFooter() {
  const health = state.health || {};
  ui.footer.textContent = `Network Stability Monitor${health.version ? ` v${health.version}` : ""} · os dados ficam somente nesta máquina`;
}

// ------------------------------------------------------------------ ações

function updateControls() {
  const live = !state.offline && state.conn !== "offline";
  const allowed = live && state.controlAllowed;
  for (const id of ["btn-rediscover", "btn-reset"]) {
    const button = $(id);
    button.disabled = !allowed;
    button.title = allowed ? "" : "Disponível apenas no computador que executa o monitor.";
  }
  $("btn-export").disabled = !live;
  $("btn-sessions").disabled = !live;
  $("btn-settings").disabled = state.offline;
}

async function runAction(url, success) {
  try {
    await postJson(url);
    toast(success);
  } catch (error) {
    toast(`Não foi possível executar: ${error.message}`, 6000);
  }
}

async function openSettings() {
  ui.settingsError.textContent = "";
  let config;
  try {
    config = await getJson(API.config);
  } catch (error) {
    toast(`Não foi possível carregar as configurações: ${error.message}`);
    return;
  }
  const settings = config.settings || {};
  const allowed = Boolean(config.control_allowed);
  for (const input of ui.settingsForm.querySelectorAll("input[name]")) {
    input.value = settings[input.name] ?? "";
    const limits = (config.limits || {})[input.name];
    if (limits) {
      input.min = limits[0];
      input.max = limits[1];
    }
    input.disabled = !allowed;
  }
  $("settings-save").disabled = !allowed;
  ui.settingsNote.textContent = allowed
    ? `As alterações são aplicadas na hora e salvas em ${config.config_path || "config.json"}.`
    : "Somente leitura: alterações só podem ser feitas no computador que executa o monitor.";
  const presets = [
    ["8.8.8.8", "Google DNS"],
    ["1.1.1.1", "Cloudflare DNS"],
  ];
  const firstHop = ((state.snapshot && state.snapshot.hops) || [])[0];
  if (firstHop && !firstHop.Is_Destination) presets.push([firstHop.Host_IP, "Roteador/gateway (H1)"]);
  ui.presets.replaceChildren(
    ...presets.map(([ip, label]) =>
      el("button", {
        type: "button",
        text: `${label}: ${ip}`,
        disabled: !allowed,
        onclick: () => {
          $("cfg-target").value = ip;
        },
      }),
    ),
  );
  ui.settingsDialog.showModal();
}

async function saveSettings(event) {
  event.preventDefault();
  const body = {};
  for (const input of ui.settingsForm.querySelectorAll("input[name]")) {
    body[input.name] = input.type === "number" ? Number(input.value) : input.value.trim();
  }
  try {
    const result = await postJson(API.config, body);
    ui.settingsDialog.close();
    toast(result.restarted ? `Nova sessão iniciada para ${result.settings.target}.` : "Configurações aplicadas.");
    if (result.restarted) {
      state.history = [];
      state.lastSeq = 0;
    }
  } catch (error) {
    ui.settingsError.textContent = error.message;
  }
}

async function openSessions() {
  let data;
  try {
    data = await getJson(API.sessions);
  } catch (error) {
    toast(`Não foi possível listar as sessões: ${error.message}`);
    return;
  }
  const sessions = data.sessions || [];
  const rows = sessions.map((session) => {
    const summary = session.summary || {};
    const period = summary.monitoring_start ? `${formatTs(summary.monitoring_start, true)} → ${formatTs(summary.monitoring_end, true)}` : "-";
    return el(
      "tr",
      {},
      el("td", { text: session.name }),
      el("td", { text: summary.target || "-" }),
      el("td", { text: period }),
      el("td", { className: "num", text: summary.outage_count ?? "-" }),
      el("td", { className: "num", text: summary.availability_pct ? `${fmt(summary.availability_pct, 3)}%` : "-" }),
      el("td", {}, el("a", { className: "btn", href: `/api/sessions/${encodeURIComponent(session.name)}/export`, text: "Baixar .zip" })),
    );
  });
  if (!rows.length) rows.push(el("tr", {}, el("td", { colspan: 6, className: "empty", text: "Nenhuma sessão arquivada ainda." })));
  ui.sessionsBody.replaceChildren(...rows);
  ui.sessionsDialog.showModal();
}

function downloadExport() {
  const link = el("a", { href: API.exportZip, download: "" });
  document.body.append(link);
  link.click();
  link.remove();
  toast("Gerando pacote de evidências (.zip)...");
}

// ------------------------------------------------------------------ modo offline (importação)

function enterOffline(message) {
  state.offline = true;
  if (state.eventSource) state.eventSource.close();
  clearTimeout(state.pollTimer);
  setConn("offline");
  updateControls();
  if (message) toast(message, 7000);
  scheduleRender();
}

function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  const src = String(text || "").replace(/^﻿/, "");
  for (let i = 0; i < src.length; i++) {
    const ch = src[i];
    if (quoted) {
      if (ch === '"' && src[i + 1] === '"') {
        field += '"';
        i++;
      } else if (ch === '"') {
        quoted = false;
      } else {
        field += ch;
      }
    } else if (ch === '"') {
      quoted = true;
    } else if (ch === ";") {
      row.push(field);
      field = "";
    } else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && src[i + 1] === "\n") i++;
      row.push(field);
      if (row.some((v) => v !== "")) rows.push(row);
      row = [];
      field = "";
    } else {
      field += ch;
    }
  }
  if (field !== "" || row.length) {
    row.push(field);
    if (row.some((v) => v !== "")) rows.push(row);
  }
  if (rows.length < 1) return [];
  const headers = rows[0].map((h) => h.trim());
  return rows.slice(1).map((values) => Object.fromEntries(headers.map((key, idx) => [key, (values[idx] ?? "").trim()])));
}

function parseSummary(text) {
  const out = {};
  for (const line of String(text || "").split(/\r?\n/)) {
    const idx = line.indexOf("=");
    if (idx > 0) out[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
  }
  return out;
}

async function unzip(buffer) {
  const view = new DataView(buffer);
  const bytes = new Uint8Array(buffer);
  let eocd = -1;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 65557); i--) {
    if (view.getUint32(i, true) === 0x06054b50) {
      eocd = i;
      break;
    }
  }
  if (eocd < 0) throw new Error("arquivo ZIP inválido");
  const count = view.getUint16(eocd + 10, true);
  let ptr = view.getUint32(eocd + 16, true);
  const decoder = new TextDecoder();
  const files = {};
  for (let n = 0; n < count; n++) {
    if (view.getUint32(ptr, true) !== 0x02014b50) break;
    const method = view.getUint16(ptr + 10, true);
    const compressedSize = view.getUint32(ptr + 20, true);
    const nameLen = view.getUint16(ptr + 28, true);
    const extraLen = view.getUint16(ptr + 30, true);
    const commentLen = view.getUint16(ptr + 32, true);
    const localOffset = view.getUint32(ptr + 42, true);
    const name = decoder.decode(bytes.subarray(ptr + 46, ptr + 46 + nameLen));
    ptr += 46 + nameLen + extraLen + commentLen;
    const start = localOffset + 30 + view.getUint16(localOffset + 26, true) + view.getUint16(localOffset + 28, true);
    const data = bytes.subarray(start, start + compressedSize);
    let content;
    if (method === 0) {
      content = data;
    } else if (method === 8) {
      if (!("DecompressionStream" in window)) throw new Error("navegador sem suporte a ZIP; extraia e importe os arquivos");
      const stream = new Blob([data]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
      content = new Uint8Array(await new Response(stream).arrayBuffer());
    } else {
      continue;
    }
    files[name.split("/").pop()] = decoder.decode(content);
  }
  return files;
}

function hopFromCsv(row) {
  return {
    Hop: num(row.Hop),
    Host_IP: row.Host_IP,
    Host_Name: row.Host_Name,
    Sent: num(row.Sent_pkt),
    Recv: num(row.Recv_pkt),
    Loss_Pct: num(row.Loss_Pct),
    Best: num(row.Best_ms),
    Worst: num(row.Worst_ms),
    Avrg: num(row.Avrg_ms),
    Last: num(row.Last_ms),
    StDev: num(row.StDev_ms),
    Jitter: num(row.Jitter_ms),
    Recent_Loss_Pct: null,
    Recent_Avrg: null,
    Is_Destination: false,
  };
}

function buildOfflineSnapshot(files) {
  const names = Object.keys(files);
  const find = (re) => names.find((name) => re.test(name));
  const snapshotName = find(/snapshot\.json$/);
  const base = snapshotName ? JSON.parse(files[snapshotName]) : {};
  const mainName = find(/\.csv$/) && names.find((name) => name.endsWith(".csv") && !/_(quedas|latencia_log|eventos)\.csv$/.test(name));
  const outagesName = find(/_quedas\.csv$/);
  const summaryName = find(/_resumo\.txt$/);
  const latencyName = find(/_latencia_log\.csv$/);
  const eventsName = find(/_eventos\.csv$/);

  const snap = { ...base };
  if (!snap.hops && mainName) {
    snap.hops = parseCsv(files[mainName]).map(hopFromCsv);
    // Contrato do CSV: o destino é sempre a última linha.
    if (snap.hops.length) snap.hops[snap.hops.length - 1].Is_Destination = true;
  }
  if (!snap.outages && outagesName) {
    snap.outages = parseCsv(files[outagesName]).map((row) => ({
      ...row,
      Duration_Sec: num(row.Duration_Sec),
      Down_Cycles: num(row.Down_Cycles),
      Last_OK_Hop: num(row.Last_OK_Hop),
    }));
  }
  const summary = summaryName ? parseSummary(files[summaryName]) : {};
  if (!snap.summary) snap.summary = summary;
  if (!snap.events && eventsName) {
    snap.events = parseCsv(files[eventsName]).map((row) => ({ ts: row.Timestamp, event: row.Event, detail: row.Detail }));
  }
  snap.destino = snap.destino || summary.target || "-";
  snap.target = snap.target || { input: summary.target || snap.destino, ip: summary.target_ip || null };
  snap.status = { link: "offline" };
  snap.settings = snap.settings || { outage_min_cycles: summary.outage_min_cycles || "-" };
  snap.phase = "stopped";

  const outageRanges = (snap.outages || [])
    .map((o) => [parseTs(o.Start), parseTs(o.End)])
    .filter(([start, end]) => start && end)
    .map(([start, end]) => [start.getTime(), end.getTime()]);
  const history = [];
  if (latencyName) {
    const byTs = new Map();
    for (const row of parseCsv(files[latencyName])) {
      if (!row.Timestamp) continue;
      if (!byTs.has(row.Timestamp)) {
        const date = parseTs(row.Timestamp);
        if (!date) continue;
        byTs.set(row.Timestamp, { seq: byTs.size + 1, ts: row.Timestamp, t: date.getTime(), up: true, rtt: {} });
      }
      const value = "Last_ms" in row ? num(row.Last_ms) : num(row.Avrg_ms);
      byTs.get(row.Timestamp).rtt[row.Host_IP] = value;
    }
    for (const point of byTs.values()) {
      point.up = !outageRanges.some(([start, end]) => point.t >= start && point.t < end);
      history.push(point);
    }
    history.sort((a, b) => a.t - b.t);
  }
  return { snap, history };
}

async function importFiles(fileList) {
  const files = {};
  try {
    for (const file of fileList) {
      if (file.name.toLowerCase().endsWith(".zip")) Object.assign(files, await unzip(await file.arrayBuffer()));
      else files[file.name] = await file.text();
    }
    const { snap, history } = buildOfflineSnapshot(files);
    if (!snap.hops && !history.length) throw new Error("nenhum arquivo de evidência reconhecido");
    enterOffline();
    state.snapshot = snap;
    state.history = history;
    state.colors = new Map();
    ui.latencyRange.value = "0";
    toast(`Evidências importadas (${Object.keys(files).length} arquivo(s)).`);
    scheduleRender();
  } catch (error) {
    toast(`Falha ao importar: ${error.message}`, 7000);
  }
}

// ------------------------------------------------------------------ eventos de interface

function bindUi() {
  const rerender = () => {
    savePrefs();
    scheduleRender();
  };
  for (const input of [ui.latencyRange, ui.latencyMa, ui.latencyRaw, ui.latencyShowMa, ui.lossBasis, ui.hideFullLoss, ui.onlyLoss]) {
    input.addEventListener("change", rerender);
  }
  $("series-all").addEventListener("click", () => {
    state.hiddenSeries.clear();
    rerender();
  });
  $("series-dest").addEventListener("click", () => {
    state.hiddenSeries = new Set(seriesDefs().filter((def) => !def.dest).map((def) => def.ip));
    rerender();
  });
  ui.latencyCanvas.addEventListener("pointermove", onLatencyHover);
  ui.latencyCanvas.addEventListener("pointerleave", clearLatencyHover);

  $("btn-settings").addEventListener("click", openSettings);
  $("btn-export").addEventListener("click", downloadExport);
  $("btn-rediscover").addEventListener("click", () => {
    ui.moreMenu.open = false;
    runAction(API.rediscover, "Redescoberta da rota solicitada.");
  });
  $("btn-reset").addEventListener("click", () => {
    ui.moreMenu.open = false;
    if (confirm("Arquivar a sessão atual e começar uma nova? As evidências atuais ficam em data/sessoes/.")) {
      runAction(API.reset, "Nova sessão iniciada; a anterior foi arquivada.");
    }
  });
  $("btn-sessions").addEventListener("click", () => {
    ui.moreMenu.open = false;
    openSessions();
  });
  $("btn-import").addEventListener("click", () => {
    ui.moreMenu.open = false;
    ui.fileInput.click();
  });
  ui.fileInput.addEventListener("change", () => {
    const files = Array.from(ui.fileInput.files);
    ui.fileInput.value = "";
    if (files.length) importFiles(files);
  });
  ui.settingsForm.addEventListener("submit", saveSettings);
  $("settings-cancel").addEventListener("click", () => ui.settingsDialog.close());
  $("sessions-close").addEventListener("click", () => ui.sessionsDialog.close());
  document.addEventListener("click", (event) => {
    if (ui.moreMenu.open && !ui.moreMenu.contains(event.target)) ui.moreMenu.open = false;
  });

  if ("ResizeObserver" in window) {
    new ResizeObserver(() => scheduleRender()).observe(ui.latencyCanvas);
  } else {
    window.addEventListener("resize", scheduleRender);
  }
  if (window.matchMedia) {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    if (media.addEventListener) media.addEventListener("change", scheduleRender);
  }
}

boot();
