# Project Map

## What This System Does
This project monitors network path stability from the local machine (Windows, Linux or macOS) to a target host/IP.
It discovers route hops, probes each hop continuously, computes quality metrics, detects and classifies outages,
and exports evidence for technical support and BI analysis. A local web portal shows everything in real time.

## Core Domains

### Route Discovery Domain (`netmon/probes.py`, `netmon/engine.py`)
- Parallel TTL-limited pings (fallback: `tracert`/`traceroute`/`tracepath`).
- Destination always appended as the last hop; periodic refresh records route changes as events.

### Connectivity Metrics Domain (`netmon/metrics.py`)
- Per-hop counters: sent/received/loss (`pkt`, `%`).
- Per-hop latency: best/worst/avg/last/stdev/jitter (`ms`) plus a recent window for current conditions.

### Outage Detection Domain (`netmon/metrics.py`)
- Destination reachability with a consecutive-failure threshold (`outage_min_cycles`).
- Tracks start/end/duration/cycles, ongoing outage and failure scope (last responding hop).

### Diagnosis Domain (`netmon/metrics.py`)
- MTR-style interpretation: only loss that persists to the destination matters; local vs external origin; jitter; destination blocking ICMP.

### Evidence Persistence Domain (`netmon/storage.py`)
- Snapshot, outages, summary, latency log, events, report; session archive in `data/sessoes/`; ZIP export.

### Visualization Domain (`web/`)
- Renders status, KPIs, diagnosis, charts and tables; filters and interaction controls; offline import.
- Consumes API and file contracts; does not decide domain correctness.

## High-Level Data Flow
1. `portal_rede.py` (or `monitor_rota.py`) loads settings (defaults < `config.json` < CLI) and starts `MonitorEngine`.
2. The engine resolves the target, starts pinging it immediately and discovers the route in the background.
3. Each cycle pings all hops in parallel, updates metrics/outage state, appends a history point and writes evidence files.
4. The portal exposes the state through `/api/*`; `/api/stream` pushes each change to the dashboard (SSE).
5. `dashboard.js` merges incremental history and renders; settings/actions go back through `POST /api/*`.
6. On reset/target change/exit the session is finalized (report) and archived at the next start.

## Change Location Guide
- Domain calculations, outage or diagnosis rules: `netmon/metrics.py`
- OS commands, parsing, route discovery: `netmon/probes.py`
- Monitoring loop, retries, sessions, history, snapshot shape: `netmon/engine.py`
- File contracts, archive, report, ZIP: `netmon/storage.py`
- Settings, defaults and validation: `netmon/config.py` (+ `config.example.json`)
- Runtime API contract/server lifecycle: `portal_rede.py`
- Console mode: `monitor_rota.py`
- Visual behavior, filters, chart rendering: `web/dashboard.js` + `web/dashboard.html` + `web/dashboard.css`
- Setup ergonomics: `iniciar.bat`, `iniciar.sh`, `iniciar.command`, `netmon/autostart.py`
- Tests: `tests/` (`python -m unittest discover -s tests -t .`)
- Governance and architecture updates: `AI_CONTRACT.md`, `ARCHITECTURE.md`, `DECISIONS.md`, `projectmap.md`
- New feature definition before coding: `specs/<feature_name>.md`
