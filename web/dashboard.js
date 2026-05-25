const FILE_MAIN = "/data/monitoramento_rota.csv";
const FILE_OUTAGES = "/data/monitoramento_rota_quedas.csv";
const FILE_SUMMARY = "/data/monitoramento_rota_resumo.txt";
const FILE_LATENCY_LOG = "/data/monitoramento_rota_latencia_log.csv";
const NAME_MAIN = "monitoramento_rota.csv";
const NAME_OUTAGES = "monitoramento_rota_quedas.csv";
const NAME_SUMMARY = "monitoramento_rota_resumo.txt";
const NAME_LATENCY = "monitoramento_rota_latencia_log.csv";

const hopsTableBody = document.querySelector("#hops-table tbody");
const outagesTableBody = document.querySelector("#outages-table tbody");
const snapshotMeta = document.getElementById("snapshot-meta");
const outageMeta = document.getElementById("outage-meta");

const kpiFalls = document.getElementById("kpi-falls");
const kpiDowntime = document.getElementById("kpi-downtime");
const kpiHops = document.getElementById("kpi-hops");
const kpiDestLoss = document.getElementById("kpi-dest-loss");
const kpiSessionTime = document.getElementById("kpi-session-time");
const kpiDowntimePct = document.getElementById("kpi-downtime-pct");

const refreshBtn = document.getElementById("refresh-btn");
const chart = document.getElementById("loss-chart");
const ctx = chart.getContext("2d");
const latencyChart = document.getElementById("latency-chart");
const latencyCtx = latencyChart.getContext("2d");
const linkStatusChart = document.getElementById("link-status-chart");
const linkStatusCtx = linkStatusChart.getContext("2d");
const hideFullLoss = document.getElementById("filter-hide-full-loss");
const onlyLoss = document.getElementById("filter-only-loss");
const hopFilterList = document.getElementById("hop-filter-list");
const latencyHopFilterList = document.getElementById("latency-hop-filter-list");
const latencyMeta = document.getElementById("latency-meta");
const latencyLegend = document.getElementById("latency-legend");
const latencyMaWindow = document.getElementById("latency-ma-window");
const latencyRange = document.getElementById("latency-range");
const latencyShowRaw = document.getElementById("latency-show-raw");
const latencyShowMa = document.getElementById("latency-show-ma");
const statusMeta = document.getElementById("status-meta");

let latestHopRows = [];
let selectedHops = new Set();
let selectedLatencyHops = new Set();
let latencyHistory = [];
let latestOutageRows = [];
let knownLossHops = new Set();
let knownLatencyHops = new Set();

refreshBtn.addEventListener("click", () => loadFromDisk());
hideFullLoss.addEventListener("change", () => renderLossChartWithFilters());
onlyLoss.addEventListener("change", () => renderLossChartWithFilters());
window.addEventListener("resize", () => renderLatencyChartWithFilters());
latencyMaWindow.addEventListener("change", () => renderLatencyChartWithFilters());
latencyRange.addEventListener("change", () => renderLatencyChartWithFilters());
latencyShowRaw.addEventListener("change", () => renderLatencyChartWithFilters());
latencyShowMa.addEventListener("change", () => renderLatencyChartWithFilters());

loadFromDisk();
setInterval(() => loadFromApi().catch(() => {}), 2000);

async function loadFromDisk() {
  try {
    await loadFromApi();
    return;
  } catch (_) {}

  try {
    const [mainRaw, outagesRaw, summaryRaw] = await Promise.all([
      fetchText(FILE_MAIN),
      fetchText(FILE_OUTAGES),
      fetchText(FILE_SUMMARY),
    ]);
    const latencyRaw = await fetchText(FILE_LATENCY_LOG).catch(() => "");
    hydrateDashboard(mainRaw, outagesRaw, summaryRaw, latencyRaw);
  } catch (error) {
    snapshotMeta.textContent = "Não foi possível carregar dados automaticamente.";
  }
}

async function loadFromApi() {
  const response = await fetch("/api/snapshot", { cache: "no-store" });
  if (!response.ok) {
    throw new Error("API indisponível");
  }

  const payload = await response.json();
  const latencyRaw = await fetchText(FILE_LATENCY_LOG).catch(() => "");
  if (payload.error) {
    snapshotMeta.textContent = `Erro do monitor: ${payload.error}`;
    return;
  }

  const hopRows = payload.hops || [];
  const outageRows = payload.outages || [];
  const summary = payload.summary || {};

  renderHops(hopRows);
  renderOutages(outageRows);
  latestOutageRows = outageRows;
  renderKpis(hopRows, outageRows, summary);
  latestHopRows = hopRows;
  syncHopFilters(hopRows);
  latencyHistory = parseLatencyLog(latencyRaw);
  syncLatencyHopFilters(hopRows);
  renderLossChartWithFilters();
  renderLatencyChartWithFilters();

  snapshotMeta.textContent = `API ao vivo: ciclo ${payload.cycle || 0} | ${new Date().toLocaleString("pt-BR")}`;
  outageMeta.textContent = `${outageRows.length} incidente(s)`;
}

async function fetchText(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Falha ao carregar ${path}`);
  }
  return response.text();
}

function hydrateDashboard(mainRaw, outagesRaw, summaryRaw, latencyRaw = "") {
  const hopRows = parseCsv(mainRaw);
  const outageRows = parseCsv(outagesRaw);
  const summary = parseSummary(summaryRaw);

  renderHops(hopRows);
  renderOutages(outageRows);
  latestOutageRows = outageRows;
  renderKpis(hopRows, outageRows, summary);
  latestHopRows = hopRows;
  syncHopFilters(hopRows);
  latencyHistory = parseLatencyLog(latencyRaw);
  syncLatencyHopFilters(hopRows);
  renderLossChartWithFilters();
  renderLatencyChartWithFilters();

  snapshotMeta.textContent = `Snapshot atualizado em ${new Date().toLocaleString("pt-BR")}`;
  outageMeta.textContent = `${outageRows.length} incidente(s)`;
}

function parseCsv(text) {
  const lines = (text || "").trim().split(/\r?\n/).filter(Boolean);
  if (lines.length <= 1) {
    return [];
  }

  const headers = lines[0].split(";").map((h) => h.trim());
  return lines.slice(1).map((line) => {
    const values = line.split(";").map((v) => v.trim());
    return headers.reduce((acc, key, idx) => {
      acc[key] = values[idx] ?? "";
      return acc;
    }, {});
  });
}

function parseSummary(text) {
  const out = {};
  for (const line of (text || "").split(/\r?\n/)) {
    if (!line.includes("=")) {
      continue;
    }
    const [key, value] = line.split("=");
    out[key.trim()] = (value || "").trim();
  }
  return out;
}

function renderHops(rows) {
  hopsTableBody.innerHTML = "";
  for (const row of rows) {
    const sent = pick(row, ["Sent", "Sent_pkt"]);
    const recv = pick(row, ["Recv", "Recv_pkt"]);
    const best = pick(row, ["Best", "Best_ms"]);
    const worst = pick(row, ["Worst", "Worst_ms"]);
    const avrg = pick(row, ["Avrg", "Avrg_ms"]);
    const last = pick(row, ["Last", "Last_ms"]);
    const tr = document.createElement("tr");
    const loss = Number.parseFloat(row.Loss_Pct || "0");
    tr.innerHTML = `
      <td>${safe(row.Hop)}</td>
      <td>${safe(row.Host_IP)}</td>
      <td>${safe(row.Host_Name)}</td>
      <td>${safe(sent)}</td>
      <td>${safe(recv)}</td>
      <td class="${lossClass(loss)}">${formatLoss(loss)}</td>
      <td>${formatMs(best)}</td>
      <td>${formatMs(worst)}</td>
      <td>${formatMs(avrg)}</td>
      <td>${formatMs(last)}</td>
    `;
    hopsTableBody.appendChild(tr);
  }
}

function renderOutages(rows) {
  outagesTableBody.innerHTML = "";
  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${safe(row.Outage_ID)}</td>
      <td>${safe(row.Start)}</td>
      <td>${safe(row.End)}</td>
      <td>${formatSeconds(row.Duration_Sec)}</td>
      <td>${safe(row.Down_Cycles)}</td>
    `;
    outagesTableBody.appendChild(tr);
  }
}

function renderKpis(hops, outages, summary) {
  const destination = hops[hops.length - 1] || {};
  const destLoss = Number.parseFloat(destination.Loss_Pct || "0");
  const downtimeSec = Number.parseFloat(summary.total_downtime_sec || "0");
  const start = parseTimestamp(summary.monitoring_start);
  const end = parseTimestamp(summary.monitoring_end);
  const sessionSec = start && end ? Math.max(0, (end.getTime() - start.getTime()) / 1000) : 0;
  const downtimePct = sessionSec > 0 ? (downtimeSec / sessionSec) * 100 : 0;

  kpiFalls.textContent = safe(summary.outage_count || String(outages.length));
  kpiDowntime.textContent = formatDuration(downtimeSec);
  kpiHops.textContent = String(hops.length);
  kpiDestLoss.textContent = formatLoss(destLoss);
  kpiDestLoss.className = lossClass(destLoss);
  kpiSessionTime.textContent = formatDuration(sessionSec);
  kpiDowntimePct.textContent = `${downtimePct.toFixed(2)}%`;
}

function renderLossChart(rows) {
  const hops = rows.map((r) => Number.parseInt(r.Hop || "0", 10));
  const losses = rows.map((r) => Number.parseFloat(r.Loss_Pct || "0"));

  ctx.clearRect(0, 0, chart.width, chart.height);

  if (rows.length === 0) {
    ctx.fillStyle = "#4e7a84";
    ctx.font = "14px Montserrat";
    ctx.fillText("Sem dados para plotar", 24, 40);
    return;
  }

  const pad = { top: 22, right: 18, bottom: 42, left: 42 };
  const w = chart.width - pad.left - pad.right;
  const h = chart.height - pad.top - pad.bottom;
  const barWidth = Math.max(12, Math.floor(w / rows.length) - 6);

  ctx.strokeStyle = "rgba(8, 76, 97, 0.18)";
  for (let t = 0; t <= 100; t += 25) {
    const y = pad.top + h - (t / 100) * h;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(pad.left + w, y);
    ctx.stroke();
    ctx.fillStyle = "#4e7a84";
    ctx.font = "11px Montserrat";
    ctx.fillText(`${t}%`, 8, y + 4);
  }

  rows.forEach((_, i) => {
    const x = pad.left + i * (barWidth + 6);
    const loss = Math.min(100, Math.max(0, losses[i]));
    const y = pad.top + h - (loss / 100) * h;
    const bh = (loss / 100) * h;

    ctx.fillStyle = barColor(loss);
    ctx.fillRect(x, y, barWidth, bh);

    ctx.fillStyle = "#083b46";
    ctx.font = "11px Montserrat";
    ctx.fillText(`H${hops[i]}`, x, pad.top + h + 16);
  });
}

function renderLossChartWithFilters() {
  const filtered = latestHopRows.filter((row) => {
    const hop = Number.parseInt(row.Hop || "0", 10);
    const loss = Number.parseFloat(row.Loss_Pct || "0");
    if (!selectedHops.has(hop)) return false;
    if (hideFullLoss.checked && loss >= 100) return false;
    if (onlyLoss.checked && loss <= 0) return false;
    return true;
  });
  renderLossChart(filtered);
}

function parseLatencyLog(text) {
  const rows = parseCsv(text);
  if (!rows.length) return [];
  const byTs = new Map();
  for (const row of rows) {
    const tsRaw = row.Timestamp || "";
    if (!tsRaw) continue;
    if (!byTs.has(tsRaw)) byTs.set(tsRaw, { ts: tsRaw, values: {} });
    const point = byTs.get(tsRaw);
    const hop = Number.parseInt(row.Hop || "0", 10);
    const avrg = Number.parseFloat(String(row.Avrg_ms || ""));
    if (Number.isFinite(hop)) point.values[hop] = Number.isFinite(avrg) ? avrg : null;
  }
  const ordered = Array.from(byTs.values()).sort((a, b) => a.ts.localeCompare(b.ts));
  const maxPoints = 1800;
  return ordered.length > maxPoints ? ordered.slice(ordered.length - maxPoints) : ordered;
}

function syncHopFilters(rows) {
  const hops = rows.map((r) => Number.parseInt(r.Hop || "0", 10)).filter((n) => Number.isFinite(n));
  const valid = new Set(hops);
  if (selectedHops.size === 0 && knownLossHops.size === 0) {
    selectedHops = new Set(valid);
  } else {
    selectedHops = new Set([...selectedHops].filter((hop) => valid.has(hop)));
    for (const hop of valid) {
      if (!knownLossHops.has(hop)) {
        selectedHops.add(hop);
      }
    }
  }
  knownLossHops = valid;

  hopFilterList.innerHTML = "";
  hops.forEach((hop) => {
    const id = `hop-filter-${hop}`;
    const label = document.createElement("label");
    label.innerHTML = `<input type="checkbox" id="${id}" ${selectedHops.has(hop) ? "checked" : ""}/> H${hop}`;
    const input = label.querySelector("input");
    input.addEventListener("change", () => {
      if (input.checked) selectedHops.add(hop);
      else selectedHops.delete(hop);
      renderLossChartWithFilters();
    });
    hopFilterList.appendChild(label);
  });
}

function syncLatencyHopFilters(rows) {
  const hops = rows.map((r) => Number.parseInt(r.Hop || "0", 10)).filter((n) => Number.isFinite(n));
  const valid = new Set(hops);
  if (selectedLatencyHops.size === 0 && knownLatencyHops.size === 0) {
    selectedLatencyHops = new Set(valid);
  } else {
    selectedLatencyHops = new Set([...selectedLatencyHops].filter((hop) => valid.has(hop)));
    for (const hop of valid) {
      if (!knownLatencyHops.has(hop)) {
        selectedLatencyHops.add(hop);
      }
    }
  }
  knownLatencyHops = valid;

  latencyHopFilterList.innerHTML = "";
  hops.forEach((hop) => {
    const id = `latency-hop-filter-${hop}`;
    const label = document.createElement("label");
    label.innerHTML = `<input type="checkbox" id="${id}" ${selectedLatencyHops.has(hop) ? "checked" : ""}/> H${hop}`;
    const input = label.querySelector("input");
    input.addEventListener("change", () => {
      if (input.checked) selectedLatencyHops.add(hop);
      else selectedLatencyHops.delete(hop);
      renderLatencyChartWithFilters();
    });
    latencyHopFilterList.appendChild(label);
  });
}

function renderLatencyChartWithFilters() {
  const rangeVal = Number.parseInt(latencyRange.value || "0", 10);
  const slicedHistory = rangeVal > 0 ? latencyHistory.slice(-rangeVal) : latencyHistory.slice();
  const activeHops = [...selectedLatencyHops].sort((a, b) => a - b);
  const filteredSeries = activeHops.map((hop) => ({
    hop,
    points: slicedHistory.map((entry) => entry.values[hop]),
  }));
  const labels = slicedHistory.map((x) => formatTimeLabel(x.ts));
  const tsRaw = slicedHistory.map((x) => x.ts);
  renderLatencyLegend(activeHops);
  drawLatencyChart(filteredSeries, labels, tsRaw);
  drawLinkStatusChart(tsRaw);
}

function drawLatencyChart(series, labels, tsRaw) {
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(640, Math.floor(latencyChart.clientWidth || 900));
  const height = Math.max(260, Math.floor(latencyChart.clientHeight || 320));
  latencyChart.width = Math.floor(width * dpr);
  latencyChart.height = Math.floor(height * dpr);
  latencyCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  latencyCtx.clearRect(0, 0, width, height);

  const hasData = series.some((s) => s.points.some((p) => Number.isFinite(p)));
  if (!hasData) {
    latencyCtx.fillStyle = "#4e7a84";
    latencyCtx.font = "14px Montserrat";
    latencyCtx.fillText("Sem histórico de latência para os filtros selecionados", 20, 36);
    latencyMeta.textContent = "Sem histórico";
    return;
  }

  let maxY = 0;
  const maWindow = Number.parseInt(latencyMaWindow.value || "5", 10);
  const showRaw = latencyShowRaw.checked;
  const showMa = latencyShowMa.checked;
  const seriesForPlot = series.map((s) => ({
    hop: s.hop,
    raw: s.points,
    ma: movingAverage(s.points, maWindow),
  }));
  seriesForPlot.forEach((s) => {
    const source = showMa ? s.ma : s.raw;
    source.forEach((p) => { if (Number.isFinite(p)) maxY = Math.max(maxY, p); });
  });
  maxY = Math.max(10, Math.ceil(maxY * 1.15));

  const pad = { top: 16, right: 18, bottom: 38, left: 52 };
  const w = width - pad.left - pad.right;
  const h = height - pad.top - pad.bottom;
  const stepX = labels.length > 1 ? w / (labels.length - 1) : w;
  drawOutageBands(latencyCtx, tsRaw, pad, w, h);

  latencyCtx.strokeStyle = "rgba(8, 76, 97, 0.18)";
  latencyCtx.fillStyle = "#4e7a84";
  latencyCtx.font = "11px Montserrat";
  for (let i = 0; i <= 4; i++) {
    const val = (maxY / 4) * i;
    const y = pad.top + h - (val / maxY) * h;
    latencyCtx.beginPath();
    latencyCtx.moveTo(pad.left, y);
    latencyCtx.lineTo(pad.left + w, y);
    latencyCtx.stroke();
    latencyCtx.fillText(`${val.toFixed(0)} ms`, 8, y + 4);
  }

  seriesForPlot.forEach((s) => {
    const color = colorForHop(s.hop);
    if (showRaw) {
      drawSeries(latencyCtx, s.raw, color, 1.1, 0.35, pad, stepX, h, maxY);
    }
    if (showMa) {
      drawSeries(latencyCtx, s.ma, color, 2.2, 1.0, pad, stepX, h, maxY);
    }
  });

  const recentCount = Math.min(labels.length, 2);
  if (recentCount > 0) {
    latencyCtx.fillStyle = "#4e7a84";
    latencyCtx.font = "11px Montserrat";
    latencyCtx.fillText(labels[Math.max(0, labels.length - recentCount)], pad.left, pad.top + h + 18);
    latencyCtx.fillText(labels[labels.length - 1], pad.left + w - 56, pad.top + h + 18);
  }

  const activeCount = series.filter((s) => s.points.some((p) => Number.isFinite(p))).length;
  latencyMeta.textContent = `${activeCount} hop(s) visível(is), ${labels.length} amostra(s), MA=${maWindow}`;
}

function renderLatencyLegend(activeHops) {
  latencyLegend.innerHTML = "";
  if (!activeHops.length) return;
  activeHops.forEach((hop) => {
    const item = document.createElement("span");
    item.className = "legend-item";
    item.innerHTML = `<span class="legend-swatch" style="background:${colorForHop(hop)}"></span>H${hop}`;
    latencyLegend.appendChild(item);
  });
}

function drawSeries(context, points, color, lineWidth, alpha, pad, stepX, h, maxY) {
  context.strokeStyle = color;
  context.globalAlpha = alpha;
  context.lineWidth = lineWidth;
  context.beginPath();
  let started = false;
  points.forEach((p, i) => {
    if (!Number.isFinite(p)) return;
    const x = pad.left + i * stepX;
    const y = pad.top + h - (p / maxY) * h;
    if (!started) {
      context.moveTo(x, y);
      started = true;
    } else {
      context.lineTo(x, y);
    }
  });
  context.stroke();
  context.globalAlpha = 1;
}

function movingAverage(points, windowSize) {
  if (windowSize <= 1) return points.slice();
  const out = [];
  for (let i = 0; i < points.length; i++) {
    let sum = 0;
    let count = 0;
    for (let j = Math.max(0, i - windowSize + 1); j <= i; j++) {
      if (Number.isFinite(points[j])) {
        sum += points[j];
        count += 1;
      }
    }
    out.push(count > 0 ? sum / count : null);
  }
  return out;
}

function drawOutageBands(context, tsRaw, pad, w, h) {
  if (!latestOutageRows.length || tsRaw.length < 2) return;
  const tsDates = tsRaw.map(parseTimestamp).filter(Boolean);
  if (tsDates.length < 2) return;
  const start = tsDates[0].getTime();
  const end = tsDates[tsDates.length - 1].getTime();
  const span = Math.max(1, end - start);

  context.fillStyle = "rgba(204, 51, 79, 0.12)";
  latestOutageRows.forEach((outage) => {
    const os = parseTimestamp(outage.Start);
    const oe = parseTimestamp(outage.End);
    if (!os || !oe) return;
    const x1 = pad.left + ((os.getTime() - start) / span) * w;
    const x2 = pad.left + ((oe.getTime() - start) / span) * w;
    const rx = Math.max(pad.left, Math.min(pad.left + w, x1));
    const rw = Math.max(1, Math.min(pad.left + w, x2) - rx);
    if (rw > 0) context.fillRect(rx, pad.top, rw, h);
  });
}

function drawLinkStatusChart(tsRaw) {
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(640, Math.floor(linkStatusChart.clientWidth || 900));
  const height = Math.max(90, Math.floor(linkStatusChart.clientHeight || 120));
  linkStatusChart.width = Math.floor(width * dpr);
  linkStatusChart.height = Math.floor(height * dpr);
  linkStatusCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  linkStatusCtx.clearRect(0, 0, width, height);

  if (tsRaw.length < 2) {
    statusMeta.textContent = "Status do link no tempo: sem dados";
    return;
  }

  const dates = tsRaw.map(parseTimestamp);
  const validDates = dates.filter(Boolean);
  if (validDates.length < 2) {
    statusMeta.textContent = "Status do link no tempo: sem dados";
    return;
  }

  const pad = { top: 8, right: 14, bottom: 24, left: 52 };
  const w = width - pad.left - pad.right;
  const h = height - pad.top - pad.bottom;
  const start = validDates[0].getTime();
  const end = validDates[validDates.length - 1].getTime();
  const span = Math.max(1, end - start);

  const statusByTs = new Map();
  validDates.forEach((d) => statusByTs.set(d.getTime(), 1));
  latestOutageRows.forEach((o) => {
    const os = parseTimestamp(o.Start);
    const oe = parseTimestamp(o.End);
    if (!os || !oe) return;
    validDates.forEach((d) => {
      if (d >= os && d <= oe) statusByTs.set(d.getTime(), 0);
    });
  });

  linkStatusCtx.strokeStyle = "rgba(8, 76, 97, 0.2)";
  linkStatusCtx.beginPath();
  linkStatusCtx.moveTo(pad.left, pad.top);
  linkStatusCtx.lineTo(pad.left + w, pad.top);
  linkStatusCtx.moveTo(pad.left, pad.top + h);
  linkStatusCtx.lineTo(pad.left + w, pad.top + h);
  linkStatusCtx.stroke();

  linkStatusCtx.fillStyle = "#4e7a84";
  linkStatusCtx.font = "11px Montserrat";
  linkStatusCtx.fillText("UP", 22, pad.top + 4);
  linkStatusCtx.fillText("DOWN", 8, pad.top + h + 4);

  linkStatusCtx.strokeStyle = "#05505e";
  linkStatusCtx.lineWidth = 2;
  linkStatusCtx.beginPath();
  let started = false;
  validDates.forEach((d) => {
    const x = pad.left + ((d.getTime() - start) / span) * w;
    const st = statusByTs.get(d.getTime()) ?? 1;
    const y = st === 1 ? pad.top : pad.top + h;
    if (!started) {
      linkStatusCtx.moveTo(x, y);
      started = true;
    } else {
      linkStatusCtx.lineTo(x, y);
    }
  });
  linkStatusCtx.stroke();

  const downCount = [...statusByTs.values()].filter((x) => x === 0).length;
  statusMeta.textContent = `Status do link no tempo: ${downCount > 0 ? "com quedas no período" : "sem quedas no período"}`;
}

function lossClass(loss) {
  if (loss < 5) return "loss-good";
  if (loss < 30) return "loss-mid";
  return "loss-bad";
}

function barColor(loss) {
  if (loss < 5) return "#1f9d6a";
  if (loss < 30) return "#d68c00";
  return "#cc334f";
}

function formatLoss(loss) {
  if (!Number.isFinite(loss)) return "-";
  return `${loss.toFixed(2)}%`;
}

function formatMs(value) {
  const num = Number.parseFloat(String(value ?? ""));
  if (!Number.isFinite(num)) return "-";
  return num.toFixed(2);
}

function formatSeconds(value) {
  const num = Number.parseFloat(String(value ?? ""));
  if (!Number.isFinite(num)) return "-";
  return num.toFixed(2);
}

function safe(value) {
  const text = String(value ?? "-");
  return text.length ? text : "-";
}

function pick(row, keys) {
  for (const key of keys) {
    if (row[key] !== undefined && row[key] !== null && String(row[key]).length > 0) {
      return row[key];
    }
  }
  return "";
}

function formatTimeLabel(ts) {
  if (!ts) return "";
  const part = String(ts).split(" ");
  return part[1] || part[0] || "";
}

function parseTimestamp(ts) {
  if (!ts) return null;
  const normalized = String(ts).replace(" ", "T");
  const d = new Date(normalized);
  return Number.isNaN(d.getTime()) ? null : d;
}

function formatDuration(totalSeconds) {
  const sec = Math.max(0, Math.floor(totalSeconds || 0));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function colorForHop(hop) {
  const palette = ["#0f766e", "#155e75", "#2563eb", "#f59e0b", "#dc2626", "#7c3aed", "#be123c", "#0ea5e9", "#0891b2", "#059669"];
  const idx = Math.abs(Number(hop || 0)) % palette.length;
  return palette[idx];
}
