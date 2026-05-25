# Decisions Log

## 2026-05-25
- Established architectural governance docs (`AI_CONTRACT.md`, `ARCHITECTURE.md`, `projectmap.md`, `specs/README.md`) to prevent code rot and local hacks.
- Chosen boundary: domain semantics (outage detection, metric aggregation) remain in Python backend; UI only renders and filters.
- Accepted trade-off: keep current file-based persistence (`;` CSV + TXT summary) for BI/tech support interoperability instead of introducing database complexity.
- Adopted explicit unit policy in contracts and UI labels (`ms`, `%`, `s`, `pkt`) to reduce interpretation errors in technical handoff.
- Preserved dual ingestion mode in dashboard (API + file fallback) for operational resilience when API is unavailable.
- Historical latency chart source standardized to persisted log (`monitoramento_rota_latencia_log.csv`) rather than volatile browser memory.
