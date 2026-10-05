"""Modo console (sem portal web): monitora a rota e grava as evidências em CSV/TXT.

Exemplos:
    python monitor_rota.py 8.8.8.8
    python monitor_rota.py 8.8.8.8 --intervalo 2 --duracao-seg 3600

Para o painel web use `portal_rede.py` (ou os scripts `iniciar`).
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 9):  # noqa: UP036 - mensagem amigável para instalações antigas
    sys.exit("Python 3.9 ou superior é necessário. Baixe em https://www.python.org/downloads/")

import argparse
import asyncio
import os
import time
from pathlib import Path

from netmon.config import DEFAULT_CONFIG_PATH, load_settings
from netmon.engine import MonitorEngine
from netmon.metrics import HopStats, OutageRecord, OutageTracker
from netmon.probes import (
    Prober,
    ProbeUnavailable,
    extract_hops_from_tracert,
    parse_ping_rtt,
    parse_trace_output,
    resolve_hostname,
)
from netmon.storage import (
    append_latency_log,
    fmt_duration,
    fmt_num,
    init_latency_log,
    write_csv,
    write_outages_csv,
    write_summary,
)

# Nomes reexportados para compatibilidade com integrações da versão 1.
__all__ = [
    "HopStats",
    "OutageRecord",
    "OutageTracker",
    "append_latency_log",
    "extract_hops_from_tracert",
    "fmt_num",
    "init_latency_log",
    "parse_ping_rtt",
    "parse_trace_output",
    "resolve_hostname",
    "write_csv",
    "write_outages_csv",
    "write_summary",
]

CLEAR_SCREEN = "\033[H\033[2J"
HEADERS = ["Hop", "Host/IP", "Host_Name", "Sent(pkt)", "Recv(pkt)", "Loss_Pct(%)", "Best(ms)", "Worst(ms)",
           "Avrg(ms)", "Last(ms)", "Jitter(ms)"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Monitora conectividade por salto da rota até um destino, "
            "com ping paralelo contínuo e exportação CSV ETL-ready (Windows, Linux e macOS)."
        )
    )
    parser.add_argument("destino", nargs="?", help="IP ou host de destino (padrão: config.json ou 8.8.8.8)")
    parser.add_argument("--intervalo", type=float, help="Intervalo em segundos entre ciclos (padrão: 1.0)")
    parser.add_argument("--timeout-ms", type=int, help="Timeout do ping por salto em milissegundos (padrão: 1000)")
    parser.add_argument("--csv", type=Path, help="Arquivo CSV de saída (padrão: data/monitoramento_rota.csv)")
    parser.add_argument("--max-hops", type=int, help="Quantidade máxima de saltos da rota (padrão: 30)")
    parser.add_argument("--min-ciclos-queda", type=int, help="Ciclos sem resposta para confirmar queda (padrão: 3)")
    parser.add_argument(
        "--duracao-seg",
        type=float,
        default=0.0,
        help="Duração total do monitoramento em segundos (0 = infinito, padrão: 0)",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Arquivo de configuração JSON")
    return parser.parse_args(argv)


def render_console(engine: MonitorEngine) -> None:
    """Renderiza a tabela de status no console a cada ciclo."""
    snap = engine.snapshot()
    rows = [
        [
            str(hop["Hop"]) + ("*" if hop["Is_Destination"] else ""),
            hop["Host_IP"],
            hop["Host_Name"],
            str(hop["Sent"]),
            str(hop["Recv"]),
            f"{hop['Loss_Pct']:.2f}",
            fmt_num(hop["Best"]),
            fmt_num(hop["Worst"]),
            fmt_num(hop["Avrg"]),
            fmt_num(hop["Last"]),
            fmt_num(hop["Jitter"]),
        ]
        for hop in snap["hops"]
    ]
    widths = [len(h) for h in HEADERS]
    for row in rows:
        for idx, col in enumerate(row):
            widths[idx] = max(widths[idx], len(col))
    total = sum(widths) + len(widths) * 3 - 1
    out = [CLEAR_SCREEN + f"Monitor de rota até {snap['destino']} | Ciclo {snap['cycle']} | {snap['server_time']}"]
    out.append(f"CSV: {engine.settings.csv_path}")
    out.append("=" * total)
    out.append(" | ".join(HEADERS[i].ljust(widths[i]) for i in range(len(HEADERS))))
    out.append("-" * total)
    out += [" | ".join(row[i].ljust(widths[i]) for i in range(len(row))) for row in rows]
    status = snap.get("status", {})
    summary = snap.get("summary", {})
    if status.get("link") == "down":
        out.append(
            f"\nSTATUS LINK: FORA DO AR desde {status.get('down_since')} "
            f"({fmt_duration(status.get('current_outage_sec', 0))}) "
            f"| quedas_finalizadas={summary.get('outage_count', 0)}"
        )
    else:
        out.append(
            f"\nSTATUS LINK: ONLINE | quedas_finalizadas={summary.get('outage_count', 0)} | "
            f"disponibilidade={summary.get('availability_pct', 100):.3f}%"
        )
    diagnosis = snap.get("diagnosis")
    if diagnosis:
        out.append(f"Diagnóstico: {diagnosis['message']}")
    for warning in snap.get("warnings", []):
        out.append(f"Aviso: {warning}")
    print("\n".join(out), flush=True)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")  # console legado do Windows não quebra com acentos
    args = parse_args(argv)
    try:
        settings, warnings = load_settings(
            args.config,
            {
                "target": args.destino,
                "interval_sec": args.intervalo,
                "timeout_ms": args.timeout_ms,
                "csv_path": args.csv,
                "max_hops": args.max_hops,
                "outage_min_cycles": args.min_ciclos_queda,
            },
        )
    except ValueError as exc:
        print(f"Erro: {exc}")
        return 2
    for warning in warnings:
        print(f"Aviso: {warning}")

    try:
        asyncio.run(Prober(settings.timeout_ms).prepare())
    except ProbeUnavailable as exc:
        print(f"Comando não encontrado no sistema: {exc}")
        return 3

    if os.name == "nt":
        os.system("")  # habilita sequências ANSI no console do Windows 10+

    engine = MonitorEngine(settings, on_cycle=render_console)
    engine.start()
    started = time.monotonic()
    try:
        while engine.is_alive():
            time.sleep(0.25)
            if args.duracao_seg > 0 and time.monotonic() - started >= args.duracao_seg:
                break
    except KeyboardInterrupt:
        print("\nMonitoramento interrompido pelo usuário (Ctrl+C).")
    finally:
        engine.stop()

    paths = engine.session.paths if engine.session else None
    if paths is not None:
        print(f"Incidentes CSV: {paths.outages}")
        print(f"Resumo: {paths.summary}")
        print(f"Relatório: {paths.report}")
        print(f"Latência Log CSV: {paths.latency_log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
