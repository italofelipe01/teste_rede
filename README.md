# Network Stability Monitor

Network route and connectivity monitoring toolkit with real-time dashboard.

## Features
- Route discovery (`tracert`) and per-hop probing
- Parallel ping loop with rolling metrics
- Outage detection based on destination reachability
- Evidence outputs for support and BI (`;` CSV + summary TXT)
- Web portal with live dashboard and historical latency chart

## Project Structure
- `monitor_rota.py`: domain logic, monitoring loop, file outputs
- `portal_rede.py`: web server and runtime API (`/api/snapshot`)
- `web/dashboard.html|css|js`: presentation, filtering, charts
- `docs/AI_CONTRACT.md`: non-negotiable engineering rules
- `docs/ARCHITECTURE.md`: layer boundaries
- `docs/DECISIONS.md`: design decision log
- `docs/projectmap.md`: project mental model and change map

## Run
```powershell
python portal_rede.py 8.8.8.8 --intervalo 0.5 --timeout-ms 400 --port 8000
```
Open:
- `http://127.0.0.1:8000`

## Generated Runtime Artifacts
- `data/monitoramento_rota.csv`
- `data/monitoramento_rota_quedas.csv`
- `data/monitoramento_rota_resumo.txt`
- `data/monitoramento_rota_latencia_log.csv`

These files are gitignored by default.

## Governance Workflow
Before core feature coding:
- Create/update `specs/<feature_name>.md`
- Align implementation with `AI_CONTRACT.md` and `ARCHITECTURE.md`
- Update `DECISIONS.md` and `projectmap.md` after architectural changes

## License
MIT (`LICENSE`).
