# Project Map

## What This System Does
This project monitors network path stability from local machine to a target host/IP.
It discovers route hops, probes each hop continuously, computes quality metrics, detects outages, and exports evidence for technical support and BI analysis.

## Core Domains

### Route Discovery Domain
- Resolves hop path via `tracert`.
- Extracts ordered hop IPs for monitoring scope.

### Connectivity Metrics Domain
- Per-hop counters: sent/received/loss.
- Per-hop latency aggregates: best/worst/avg/last in milliseconds.
- Maintains consistent numeric formatting and unit semantics.

### Outage Detection Domain
- Uses final-destination reachability to identify outage windows.
- Tracks start/end/duration and number of outage cycles.

### Evidence Persistence Domain
- Snapshot: `monitoramento_rota.csv`
- Outage incidents: `monitoramento_rota_quedas.csv`
- Summary: `monitoramento_rota_resumo.txt`
- Historical avg latency log: `monitoramento_rota_latencia_log.csv`

### Visualization Domain
- Renders tables/KPIs/charts.
- Filters and interaction controls.
- Consumes API and file contracts; does not decide domain correctness.

## High-Level Data Flow
1. `monitor_rota.py` discovers hops and runs parallel ping loop.
2. Domain metrics and outages are updated each cycle.
3. Files are written/updated as evidence outputs.
4. `portal_rede.py` hosts UI and exposes `/api/snapshot`.
5. `dashboard.js` reads snapshot + historical log and renders visual analytics.

## Change Location Guide
- Domain calculations or outage rules: `monitor_rota.py`
- Runtime API contract/server lifecycle: `portal_rede.py`
- Visual behavior, filters, chart rendering: `dashboard.js` + `dashboard.html` + `dashboard.css`
- Governance and architecture updates: `AI_CONTRACT.md`, `ARCHITECTURE.md`, `DECISIONS.md`, `projectmap.md`
- New feature definition before coding: `specs/<feature_name>.md`
