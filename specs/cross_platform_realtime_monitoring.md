# Spec: Cross-platform, self-healing monitoring with real-time API and dashboard

## 1. Problem statement
Version 1 only ran on Windows (`tracert`, `ping -n -w`), counted every lost ping to the destination as an
outage, stopped forever if route discovery failed at startup, did not show an ongoing outage in the
dashboard, served the whole working directory over HTTP and re-downloaded an unbounded latency log every
2 seconds. Setting it up on another machine required manual steps and Windows.

Goal: any machine with Python 3.9+ and the system `ping` (Windows, Linux, macOS) starts monitoring with one
double-click, keeps working through network failures without intervention, exposes everything through a
local API and shows it in a dashboard that works offline.

## 2. Non-goals
- IPv6 targets (rejected with a clear message).
- Raw-socket ICMP (would require administrator/root privileges).
- Multi-target monitoring in one process, user accounts or remote/cloud storage.
- Database persistence (file contracts remain the interoperability layer).

## 3. Behavior rules (domain constraints)
- Ping/route commands are built per OS; parsing uses only IPv4 addresses, `ttl=` and `ms` values.
- Route discovery: parallel TTL-limited pings (2 passes); native traceroute only if no TTL reply arrives.
- The destination is monitored from the first cycle and is always the last hop.
- An outage is confirmed after `outage_min_cycles` consecutive destination failures and starts at the first
  failed cycle; it ends at the first destination reply. Pending failures below the threshold are loss only.
- Outage scope = most frequent farthest responding intermediate hop during the outage; `local` when none
  answered but some hop answered before; `unknown` without reference hops.
- Diagnosis considers only loss that persists to the destination (recent window); intermediate-only loss is informational.
- Route refresh: complete routes are refreshed every `route_refresh_min`; incomplete/failed discoveries retry with backoff (15 s → 300 s); an incomplete result never replaces a complete route.
- Changing the target or requesting a reset starts a new session; the previous evidence is archived, never deleted (unless `keep_sessions` > 0).
- Units: `ms`, `%`, `s`, `pkt` in contracts and UI labels.

## 4. Edge cases
- DNS unavailable at startup (hostname target): retry every 10 s, phase `error` with message.
- Destination never answers ICMP while hops do: diagnosis `destination_never_responded` suggesting another target.
- `ping` missing or not permitted: phase `error` with OS-specific install hint; retried every 15 s.
- Evidence file locked by another program (Excel): rows buffered and flushed later; warning in the API.
- Port in use: next free port; another instance of this app on the port: open it instead of starting another.
- Console-less start (pythonw/autostart): logs go to `data/monitor.log`.
- Server restart while the dashboard is open: SSE resume with a different instance id triggers a history reset.

## 5. Risks
- Unusual `ping` variants (e.g., BusyBox) may not report TTL-exceeded: mitigated by the native traceroute fallback and destination-only monitoring.
- ECMP load balancing can produce legitimate "route changed" events.
- Per-cycle subprocess spawning has CPU cost on very long routes with sub-second intervals (interval floor 0.2 s).

## 6. Test plan
- Parser fixtures for Windows (pt-BR/en-US), Linux, macOS ping outputs and tracert/traceroute/tracepath.
- Unit tests for `HopStats`, `OutageTracker` (threshold, scope, close), `diagnose`, storage writers, archive, report, ZIP, config precedence/validation, autostart entries.
- Engine scenarios with a simulated prober: discovery, outage start/end with scope, route change, live settings, target change archiving, export, finalization.
- HTTP tests: static whitelist and traversal, v1 snapshot keys, history cursor, gzip, SSE first event, ZIP export, control validation (content type, origin, invalid values, loopback rule).
- Real system `ping` against 127.0.0.1 on Windows, Linux and macOS CI runners, plus `portal_rede.py --check`.
- Manual: dashboard in Chromium (desktop, dark, mobile) live and during a simulated outage.

## 7. Acceptance checks
- `iniciar.*` starts the monitor and opens the dashboard on Windows, Linux and macOS without editing files.
- Unplugging the network produces an ongoing outage banner with timer, then a closed outage with scope.
- `python -m unittest discover -s tests -t .`, `ruff check .`, `node --check web/dashboard.js` pass; CI green on the OS matrix.
- v1 snapshot keys and CSV leading columns unchanged.
