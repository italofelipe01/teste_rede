# Architecture

## System Overview
This repository contains a network connectivity monitoring system with:
- A continuous probe engine (traceroute + per-hop ping)
- Persistence/export in CSV/TXT evidence files
- A web portal with real-time dashboard and historical visualizations

## Layers and Boundaries

### 1) UI Layer
Files:
- `dashboard.html`
- `dashboard.css`
- `dashboard.js`

Responsibilities:
- Render tables/charts
- User interactions (filters, manual file load)
- Data presentation formatting

Constraints:
- Must not define outage/business semantics
- Must not hardcode domain exceptions for specific IPs/hops
- Consumes contracts from files/API only

### 2) Application/API Layer
File:
- `portal_rede.py`

Responsibilities:
- Host HTTP server and static assets
- Expose runtime snapshot at `/api/snapshot`
- Coordinate monitor worker lifecycle with portal lifecycle

Constraints:
- No presentation logic
- No duplicated domain computations if already available in domain/services

### 3) Domain + Monitoring Engine
File:
- `monitor_rota.py`

Responsibilities:
- Route discovery (`tracert` parsing)
- Parallel ping execution
- Metric aggregation per hop
- Outage detection state machine
- Writing evidence artifacts (`monitoramento_rota*.csv`, summary)

Constraints:
- Source of truth for metric semantics
- Deterministic calculations with explicit units

### 4) Persistence/Contracts
Outputs:
- `monitoramento_rota.csv` (snapshot by hop)
- `monitoramento_rota_quedas.csv` (outage incidents)
- `monitoramento_rota_resumo.txt` (executive summary)
- `monitoramento_rota_latencia_log.csv` (historical avg latency series)

Rules:
- CSV delimiter: `;`
- Headers are API/data contracts and must be versioned/documented when changed

## Communication Rules
- UI <-reads- API snapshot and evidence files
- API <-uses- domain functions from monitor engine
- Domain <-writes- persistence outputs
- UI must not call shell/network commands directly

## Change Policy
When introducing new behavior:
1. Domain model/rules first
2. Service/API integration second
3. UI rendering last
4. Tests/checks and docs updates mandatory
