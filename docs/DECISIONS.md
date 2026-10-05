# Decisions Log

## 2026-05-25
- Established architectural governance docs (`AI_CONTRACT.md`, `ARCHITECTURE.md`, `projectmap.md`, `specs/SPEC_TEMPLATE.md`) to prevent code rot and local hacks.
- Chosen boundary: domain semantics (outage detection, metric aggregation) remain in Python backend; UI only renders and filters.
- Accepted trade-off: keep current file-based persistence (`;` CSV + TXT summary) for BI/tech support interoperability instead of introducing database complexity.
- Adopted explicit unit policy in contracts and UI labels (`ms`, `%`, `s`, `pkt`) to reduce interpretation errors in technical handoff.
- Preserved dual ingestion mode in dashboard (API + file fallback) for operational resilience when API is unavailable.
- Historical latency chart source standardized to persisted log (`monitoramento_rota_latencia_log.csv`) rather than volatile browser memory.

## 2026-10-05
Spec: `specs/cross_platform_realtime_monitoring.md`.

- **Cross-platform probing without admin rights.** Replaced Windows-only `tracert`/`ping -n -w` with an OS-aware command builder (Windows, BSD/macOS, Linux) and language-independent parsing (only IPs, `ttl=` and `ms` values are used), so localized Windows outputs (pt-BR, en-US, others) and decimal RTTs from Linux/macOS are parsed. Trade-off: still depends on the system `ping` binary instead of raw sockets, which keeps the tool privilege-free.
- **Route discovery by parallel TTL-limited pings**, with native `tracert`/`traceroute`/`tracepath` as automatic fallback. Discovery takes about one timeout instead of minutes, which also makes periodic route refresh (default 10 min) cheap. Hops that miss one scan keep their known IP to avoid false "route changed" events.
- **Destination is always monitored and always the last hop.** Previously, when the destination did not answer the traceroute, outage detection silently used the last router. Monitoring now starts with the destination alone and adds hops when discovery succeeds.
- **Outage semantics (behavior change):** an outage is confirmed after `outage_min_cycles` consecutive destination failures (default 3; `1` reproduces v1) and starts at the first failed cycle. Isolated failures count as packet loss only. Rationale: single lost ICMP replies were being reported as outages, which weakens evidence sent to providers.
- **Outage scope:** each outage records the farthest intermediate hop that kept answering (`Last_OK_Hop`, `Last_OK_IP`) and a `Scope` (`local` when no hop answered although some normally do, `external` when the route answers up to a hop, `unknown` otherwise).
- **Contract additions (append-only):** snapshot CSV `StDev_ms;Jitter_ms`; outages CSV `Last_OK_Hop;Last_OK_IP;Scope`; latency log `Last_ms`; summary extra keys; new files `_eventos.csv` and `_relatorio.txt`. `total_downtime_sec` now includes the ongoing outage while the session is running (final value unchanged).
- **Latency chart plots per-cycle RTT (`Last_ms`) with client-side moving average** instead of the cumulative average, which converges and hides spikes. The cumulative `Avrg_ms` remains in the log for BI compatibility.
- **API-first dashboard:** `/api/history` (incremental, in-memory ring buffer) and `/api/stream` (SSE) replace downloading the whole latency log every 2 s, which grew without bound. File fallback is kept as an explicit offline import (CSV/TXT/ZIP).
- **Domain values moved out of the UI:** link status (`up/checking/down`), ongoing outage, availability, diagnosis and color thresholds are computed in `netmon` and delivered by the API.
- **Security hardening:** static files are served from a closed whitelist relative to the script (v1 served the whole current working directory); data from reverse DNS is rendered with `textContent` (v1 used `innerHTML`, an XSS vector through PTR records); control endpoints are loopback-only by default and require JSON + same origin.
- **Resilience:** the engine never stops on network errors (DNS/route/ping retried with backoff), tolerates evidence files locked by other programs (rows are buffered and flushed later), and finalizes evidence on SIGTERM/Ctrl+C. Each new session archives the previous evidence to `data/sessoes/<start>/` instead of overwriting it.
- **Easy setup:** stdlib only, `iniciar.bat`/`iniciar.sh`/`iniciar.command` launchers that check/offer to install prerequisites, `config.json` written by the dashboard, free-port fallback, single-instance detection, `--check` environment report, optional login autostart. Default bind stays `127.0.0.1` (no firewall prompt); LAN access is an explicit `--lan`/`host` choice.
- **No external front-end resources** (Google Fonts removed): the dashboard must render during the very outages it reports.
