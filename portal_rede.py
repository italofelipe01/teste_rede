from __future__ import annotations

import argparse
import asyncio
import functools
import json
import threading
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from monitor_rota import (
    HopStats,
    OutageTracker,
    discover_route,
    ping_once,
    resolve_hostname,
    append_latency_log,
    write_csv,
    init_latency_log,
    write_outages_csv,
    write_summary,
)


class MonitorState:
    def __init__(self, destino: str, intervalo: float, timeout_ms: int, csv_path: Path, max_hops: int) -> None:
        self.destino = destino
        self.intervalo = intervalo
        self.timeout_ms = timeout_ms
        self.csv_path = csv_path
        self.max_hops = max_hops

        self.outages_path = csv_path.with_name(f"{csv_path.stem}_quedas.csv")
        self.summary_path = csv_path.with_name(f"{csv_path.stem}_resumo.txt")
        self.latency_log_path = csv_path.with_name(f"{csv_path.stem}_latencia_log.csv")
        self.started_at = datetime.now()

        self.stats: list[HopStats] = []
        self.tracker = OutageTracker()
        self.cycle = 0
        self.last_update: datetime | None = None
        self.error: str | None = None

        self._lock = threading.Lock()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total_downtime = sum(r.duration_sec for r in self.tracker.records)
            return {
                "destino": self.destino,
                "cycle": self.cycle,
                "last_update": self.last_update.isoformat() if self.last_update else None,
                "error": self.error,
                "hops": [
                    {
                        "Hop": s.hop,
                        "Host_IP": s.host,
                        "Host_Name": s.host_name,
                        "Sent": s.sent,
                        "Recv": s.recv,
                        "Loss_Pct": round(s.loss_pct, 2),
                        "Best": round(s.best, 2) if s.best is not None else None,
                        "Worst": round(s.worst, 2) if s.worst is not None else None,
                        "Avrg": round(s.avg, 2) if s.avg is not None else None,
                        "Last": round(s.last, 2) if s.last is not None else None,
                    }
                    for s in self.stats
                ],
                "outages": [
                    {
                        "Outage_ID": idx,
                        "Start": rec.start.isoformat(sep=" ", timespec="seconds"),
                        "End": rec.end.isoformat(sep=" ", timespec="seconds"),
                        "Duration_Sec": round(rec.duration_sec, 2),
                        "Down_Cycles": rec.down_cycles,
                    }
                    for idx, rec in enumerate(self.tracker.records, start=1)
                ],
                "summary": {
                    "monitoring_start": self.started_at.isoformat(sep=" ", timespec="seconds"),
                    "monitoring_end": datetime.now().isoformat(sep=" ", timespec="seconds"),
                    "outage_count": self.tracker.total_outages,
                    "total_downtime_sec": round(total_downtime, 2),
                },
            }


async def monitor_loop(state: MonitorState, stop_event: threading.Event) -> None:
    try:
        hops = await discover_route(state.destino, max_hops=state.max_hops)
        stats = [HopStats(hop=hop, host=host, host_name=resolve_hostname(host)) for hop, host in hops]

        with state._lock:
            state.stats = stats
            state.error = None
            init_latency_log(state.latency_log_path)

        while not stop_event.is_set():
            results = await asyncio.gather(
                *[ping_once(item.host, timeout_ms=state.timeout_ms) for item in stats],
                return_exceptions=True,
            )

            now = datetime.now()
            for item, result in zip(stats, results):
                if isinstance(result, BaseException) or result is None:
                    item.register_timeout()
                else:
                    item.register_success(float(result))

            destination_ok = bool(results) and not isinstance(results[-1], BaseException) and results[-1] is not None

            with state._lock:
                state.cycle += 1
                state.last_update = now
                state.tracker.update(destination_ok=destination_ok, event_time=now)
                write_csv(state.csv_path, stats)
                write_outages_csv(state.outages_path, state.tracker)
                write_summary(state.summary_path, state.tracker, state.started_at, now)
                append_latency_log(state.latency_log_path, stats, timestamp=now)

            await asyncio.sleep(state.intervalo)

    except Exception as exc:
        with state._lock:
            state.error = str(exc)


def monitor_thread(state: MonitorState, stop_event: threading.Event) -> None:
    asyncio.run(monitor_loop(state, stop_event))


class Handler(SimpleHTTPRequestHandler):
    state: MonitorState

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/snapshot":
            payload = json.dumps(self.state.snapshot()).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if parsed.path == "/":
            self.path = "/web/dashboard.html"
        return super().do_GET()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Portal web + monitoramento de conectividade em tempo real")
    parser.add_argument("destino", help="IP ou host destino")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--intervalo", type=float, default=2.0)
    parser.add_argument("--timeout-ms", type=int, default=1000)
    parser.add_argument("--csv", type=Path, default=Path("data/monitoramento_rota.csv"))
    parser.add_argument("--max-hops", type=int, default=30)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    stop_event = threading.Event()
    state = MonitorState(
        destino=args.destino,
        intervalo=args.intervalo,
        timeout_ms=args.timeout_ms,
        csv_path=args.csv,
        max_hops=args.max_hops,
    )

    Handler.state = state
    server = ThreadingHTTPServer((args.host, args.port), functools.partial(Handler, directory="."))

    worker = threading.Thread(target=monitor_thread, args=(state, stop_event), daemon=True)
    worker.start()

    print(f"Portal em http://{args.host}:{args.port} (Ctrl+C para encerrar)")
    print(f"Monitorando destino: {args.destino}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
