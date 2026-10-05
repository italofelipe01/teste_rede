# Architecture

## System Overview
This repository contains a cross-platform (Windows, Linux, macOS) network connectivity monitoring system with:
- A continuous, self-healing probe engine (TTL-scan route discovery + per-hop parallel ping)
- Persistence/export of evidence files (`;` CSV + TXT summary/report + ZIP export)
- A local web portal with a REST + Server-Sent Events API and a real-time dashboard

Runtime requirements: Python 3.9+ standard library and the operating system `ping`. No third-party packages.

## Layers and Boundaries

### 1) UI Layer
Files:
- `web/dashboard.html`
- `web/dashboard.css`
- `web/dashboard.js`
- `web/favicon.svg`

Responsibilities:
- Render status banner, KPIs, tables and charts
- User interactions (filters, series selection, settings form, actions, offline evidence import)
- Data presentation formatting (pt-BR numbers/dates, colors from API-provided thresholds)

Constraints:
- Must not define outage/business semantics (link status, outage scope, diagnosis and thresholds come from the API)
- Must not hardcode domain exceptions for specific IPs/hops
- Consumes contracts from API (`/api/*`) or imported evidence files only
- Must render data with `textContent`/DOM APIs (never `innerHTML` with data)
- Must not load external resources (the dashboard has to work while the internet is down)

### 2) Application/API Layer
Files:
- `portal_rede.py` (web portal + API)
- `monitor_rota.py` (console mode, same engine)

Responsibilities:
- Host the HTTP server and a closed whitelist of static assets (no directory listing, no CWD dependency)
- Expose the runtime API (see "API contract")
- Coordinate engine lifecycle with process lifecycle (SIGTERM/Ctrl+C finalize evidence)
- Startup ergonomics: config loading, free-port fallback, single-instance detection, browser opening, `--check`, autostart

Constraints:
- No presentation logic
- No domain computations (delegates to `netmon`)
- Control endpoints (POST) only from loopback unless `allow_remote_control`; JSON content type and same-origin required

### 3) Domain + Monitoring Engine (`netmon/` package)
Files:
- `netmon/metrics.py`: `HopStats` (O(1) aggregates, jitter, stdev, recent window), `OutageTracker` (threshold state machine + failure scope), `diagnose()`; pure, no I/O
- `netmon/probes.py`: OS-specific `ping` commands, language-independent parsing, TTL-scan route discovery with native traceroute fallback, target validation/resolution, environment report
- `netmon/engine.py`: `MonitorEngine` (thread + asyncio loop), sessions, retries/backoff, periodic route refresh, history ring buffer, thread-safe snapshot/commands
- `netmon/storage.py`: evidence contracts, atomic/locked-file tolerant writers, session archiving, report and ZIP builders
- `netmon/config.py`: defaults < `config.json` < CLI precedence and validation
- `netmon/autostart.py`: per-OS login autostart entries

Constraints:
- Source of truth for metric semantics
- Deterministic calculations with explicit units
- The destination is always the last monitored hop (`Is_Destination`)

### 4) Persistence/Contracts
Outputs (default `data/`, previous sessions in `data/sessoes/<start>/`):
- `monitoramento_rota.csv`: `Hop;Host_IP;Host_Name;Sent_pkt;Recv_pkt;Loss_Pct;Best_ms;Worst_ms;Avrg_ms;Last_ms;StDev_ms;Jitter_ms`
- `monitoramento_rota_quedas.csv`: `Outage_ID;Start;End;Duration_Sec;Down_Cycles;Last_OK_Hop;Last_OK_IP;Scope` (`Scope` in `local|external|unknown`)
- `monitoramento_rota_resumo.txt`: `key=value` (`monitoring_start`, `monitoring_end`, `outage_count`, `total_downtime_sec`, then `target`, `target_ip`, `cycles`, `session_sec`, `availability_pct`, `link_status`, `down_since`, `destination_*`, `outage_min_cycles`)
- `monitoramento_rota_latencia_log.csv`: `Timestamp;Hop;Host_IP;Host_Name;Avrg_ms;Last_ms`
- `monitoramento_rota_eventos.csv`: `Timestamp;Event;Detail`
- `monitoramento_rota_relatorio.txt`: human-readable report (refreshed every 60 s and at session end)

Rules:
- CSV delimiter: `;`
- Headers are API/data contracts; new columns are appended at the end and documented in `DECISIONS.md`

## API contract
- `GET /api/snapshot`: v1 keys (`destino`, `cycle`, `last_update`, `error`, `hops[]`, `outages[]`, `summary`) plus `status`, `diagnosis`, `route`, `events`, `settings`, `thresholds`, `warnings`, `phase`, `session_id`, `instance`, `seq`
- `GET /api/history?since=<instance>:<seq>`: `{instance, session_id, latest_seq, reset, points[{seq, ts, t, up, rtt{ip: ms|null}}]}`
- `GET /api/stream`: SSE; each message = history delta + `snapshot`; event id `<instance>:<seq>` enables resume via `Last-Event-ID`
- `GET /api/health`, `GET /api/config`, `GET /api/export`, `GET /api/sessions`, `GET /api/sessions/<name>/export`, `GET /data/<file>`
- `POST /api/config`, `POST /api/actions/rediscover`, `POST /api/actions/reset`

## Communication Rules
- UI <-reads- API (SSE first, polling fallback) or imported evidence files (offline mode)
- API <-uses- `MonitorEngine` (thread-safe methods only)
- Engine <-uses- probes (I/O), metrics (rules), storage (persistence)
- UI must not call shell/network commands directly

## Change Policy
When introducing new behavior:
1. Domain model/rules first
2. Service/API integration second
3. UI rendering last
4. Tests/checks and docs updates mandatory
