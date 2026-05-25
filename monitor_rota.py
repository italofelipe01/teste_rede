from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
import socket
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
TRACERT_HOP_RE = re.compile(r"^\s*(\d+)\s+(.*)$")
PING_TIME_RE = re.compile(r"(?:time|tempo)\s*(?:=|<)\s*(\d+)\s*ms", re.IGNORECASE)


@dataclass
class HopStats:
    """Métricas acumuladas de conectividade para um salto da rota."""

    hop: int
    host: str
    host_name: str
    sent: int = 0
    recv: int = 0
    best: float | None = None
    worst: float | None = None
    avg: float | None = None
    last: float | None = None
    _samples: list[float] = field(default_factory=list, repr=False)

    def register_success(self, latency_ms: float) -> None:
        """Registra sucesso de ping e atualiza métricas de latência."""
        self.sent += 1
        self.recv += 1
        self.last = latency_ms
        self._samples.append(latency_ms)

        if self.best is None or latency_ms < self.best:
            self.best = latency_ms
        if self.worst is None or latency_ms > self.worst:
            self.worst = latency_ms

        self.avg = statistics.fmean(self._samples)

    def register_timeout(self) -> None:
        """Registra envio sem resposta (timeout/perda)."""
        self.sent += 1
        self.last = None

    @property
    def loss_pct(self) -> float:
        """Calcula percentual de perda acumulado para o salto."""
        if self.sent == 0:
            return 0.0
        return ((self.sent - self.recv) / self.sent) * 100.0


@dataclass
class OutageRecord:
    """Registro de um período de indisponibilidade detectado."""

    start: datetime
    end: datetime
    duration_sec: float
    down_cycles: int


@dataclass
class OutageTracker:
    """Controla eventos de queda/retorno com base no reachability do destino final."""

    is_down: bool = False
    down_start: datetime | None = None
    down_cycles: int = 0
    total_outages: int = 0
    records: list[OutageRecord] = field(default_factory=list)

    def update(self, destination_ok: bool, event_time: datetime) -> None:
        """Atualiza estado de queda para o ciclo corrente."""
        if destination_ok:
            if self.is_down and self.down_start is not None:
                duration = (event_time - self.down_start).total_seconds()
                self.records.append(
                    OutageRecord(
                        start=self.down_start,
                        end=event_time,
                        duration_sec=duration,
                        down_cycles=self.down_cycles,
                    )
                )
                self.total_outages += 1
                self.is_down = False
                self.down_start = None
                self.down_cycles = 0
            return

        if not self.is_down:
            self.is_down = True
            self.down_start = event_time
            self.down_cycles = 1
        else:
            self.down_cycles += 1

    def close_open_outage(self, event_time: datetime) -> None:
        """Fecha uma queda ainda aberta no encerramento do monitoramento."""
        if not self.is_down or self.down_start is None:
            return

        duration = (event_time - self.down_start).total_seconds()
        self.records.append(
            OutageRecord(
                start=self.down_start,
                end=event_time,
                duration_sec=duration,
                down_cycles=self.down_cycles,
            )
        )
        self.total_outages += 1
        self.is_down = False
        self.down_start = None
        self.down_cycles = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Monitora conectividade por salto da rota até um destino, "
            "com ping paralelo contínuo e exportação CSV ETL-ready."
        )
    )
    parser.add_argument("destino", help="IP ou host de destino (ex.: 8.8.8.8)")
    parser.add_argument(
        "--intervalo",
        type=float,
        default=2.0,
        help="Intervalo em segundos entre ciclos (padrão: 2.0)",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=1000,
        help="Timeout do ping por salto em milissegundos (padrão: 1000)",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("data/monitoramento_rota.csv"),
        help="Arquivo CSV de saída (padrão: data/monitoramento_rota.csv)",
    )
    parser.add_argument(
        "--max-hops",
        type=int,
        default=30,
        help="Quantidade máxima de saltos no tracert (padrão: 30)",
    )
    parser.add_argument(
        "--duracao-seg",
        type=float,
        default=0.0,
        help="Duração total do monitoramento em segundos (0 = infinito, padrão: 0)",
    )
    return parser.parse_args()


async def run_command(*args: str) -> tuple[int, str, str]:
    """Executa comando assíncrono e retorna código, stdout e stderr."""
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await process.communicate()
    return_code = process.returncode if process.returncode is not None else -1
    return return_code, stdout_b.decode(errors="replace"), stderr_b.decode(errors="replace")


def extract_hops_from_tracert(output: str) -> list[tuple[int, str]]:
    """Extrai lista ordenada de (hop, ip) a partir da saída do tracert."""
    hops: list[tuple[int, str]] = []
    seen: set[str] = set()

    for line in output.splitlines():
        match = TRACERT_HOP_RE.match(line)
        if not match:
            continue

        hop = int(match.group(1))
        body = match.group(2)
        ips = IPV4_RE.findall(body)
        if not ips:
            continue

        ip = ips[-1]
        if ip in seen:
            continue

        seen.add(ip)
        hops.append((hop, ip))

    return sorted(hops, key=lambda item: item[0])


async def discover_route(destino: str, max_hops: int) -> list[tuple[int, str]]:
    """Executa tracert para descobrir rota até o destino.

    Usa `-d` para evitar DNS reverso e reduzir tempo de descoberta.
    """
    cmd = ("tracert", "-d", "-h", str(max_hops), destino)
    code, stdout, stderr = await run_command(*cmd)
    if code != 0:
        raise RuntimeError(f"Falha ao executar tracert ({code}): {stderr.strip() or stdout.strip()}")

    hops = extract_hops_from_tracert(stdout)
    if not hops:
        raise RuntimeError(
            "Nenhum salto identificado no tracert. "
            "Verifique conectividade, destino e permissões locais de rede."
        )

    return hops


async def ping_once(host: str, timeout_ms: int) -> float | None:
    """Dispara um único ping ICMP no Windows e retorna latência em ms.

    Retorna `None` para timeout/perda.

    Observação: alguns ambientes corporativos podem bloquear ICMP por política.
    """
    code, stdout, _ = await run_command("ping", "-n", "1", "-w", str(timeout_ms), host)
    if code != 0:
        return None

    for line in stdout.splitlines():
        m = PING_TIME_RE.search(line)
        if not m:
            continue
        value = m.group(1)
        return float(value)

    lower_stdout = stdout.lower()
    if "time<1ms" in lower_stdout or "tempo<1ms" in lower_stdout:
        return 0.5

    return None


def render_console(stats: Iterable[HopStats], destino: str, csv_path: Path, ciclo: int) -> None:
    """Renderiza tabela de status no console."""
    rows = []
    for item in stats:
        rows.append(
            [
                str(item.hop),
                item.host,
                item.host_name,
                str(item.sent),
                str(item.recv),
                f"{item.loss_pct:.2f}",
                fmt_num(item.best),
                fmt_num(item.worst),
                fmt_num(item.avg),
                fmt_num(item.last),
            ]
        )

    headers = [
        "Hop",
        "Host/IP",
        "Host_Name",
        "Sent(pkt)",
        "Recv(pkt)",
        "Loss_Pct(%)",
        "Best(ms)",
        "Worst(ms)",
        "Avrg(ms)",
        "Last(ms)",
    ]
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, col in enumerate(row):
            widths[idx] = max(widths[idx], len(col))

    os.system("cls" if os.name == "nt" else "clear")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"Monitor de rota até {destino} | Ciclo {ciclo} | {now}")
    print(f"CSV: {csv_path.resolve()}")
    print("=" * (sum(widths) + len(widths) * 3 - 1))
    print(" | ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
    print("-" * (sum(widths) + len(widths) * 3 - 1))
    for row in rows:
        print(" | ".join(row[i].ljust(widths[i]) for i in range(len(row))))


def fmt_num(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}"


def write_csv(csv_path: Path, stats: Iterable[HopStats]) -> None:
    """Sobrescreve CSV com snapshot atual para consumo ETL/BI."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = csv_path.with_suffix(csv_path.suffix + ".tmp")

    with temp_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(
            ["Hop", "Host_IP", "Host_Name", "Sent_pkt", "Recv_pkt", "Loss_Pct", "Best_ms", "Worst_ms", "Avrg_ms", "Last_ms"]
        )

        for item in stats:
            writer.writerow(
                [
                    item.hop,
                    item.host,
                    item.host_name,
                    item.sent,
                    item.recv,
                    f"{item.loss_pct:.2f}",
                    fmt_num(item.best),
                    fmt_num(item.worst),
                    fmt_num(item.avg),
                    fmt_num(item.last),
                ]
            )

    temp_path.replace(csv_path)


def write_outages_csv(outages_path: Path, tracker: OutageTracker) -> None:
    """Sobrescreve CSV de incidentes de queda para evidência técnica."""
    outages_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = outages_path.with_suffix(outages_path.suffix + ".tmp")

    with temp_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["Outage_ID", "Start", "End", "Duration_Sec", "Down_Cycles"])
        for idx, rec in enumerate(tracker.records, start=1):
            writer.writerow(
                [
                    idx,
                    rec.start.isoformat(sep=" ", timespec="seconds"),
                    rec.end.isoformat(sep=" ", timespec="seconds"),
                    f"{rec.duration_sec:.2f}",
                    rec.down_cycles,
                ]
            )

    temp_path.replace(outages_path)


def write_summary(summary_path: Path, tracker: OutageTracker, started_at: datetime, finished_at: datetime) -> None:
    """Escreve resumo executivo do monitoramento para envio ao provedor."""
    total_downtime = sum(rec.duration_sec for rec in tracker.records)
    lines = [
        f"monitoring_start={started_at.isoformat(sep=' ', timespec='seconds')}",
        f"monitoring_end={finished_at.isoformat(sep=' ', timespec='seconds')}",
        f"outage_count={tracker.total_outages}",
        f"total_downtime_sec={total_downtime:.2f}",
    ]
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def init_latency_log(log_path: Path) -> None:
    """Inicializa o arquivo de histórico de latência média."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["Timestamp", "Hop", "Host_IP", "Host_Name", "Avrg_ms"])


def append_latency_log(log_path: Path, stats: Iterable[HopStats], timestamp: datetime) -> None:
    """Acrescenta snapshot de Avrg(ms) por hop para série temporal histórica."""
    with log_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        ts = timestamp.isoformat(sep=" ", timespec="seconds")
        for item in stats:
            writer.writerow([ts, item.hop, item.host, item.host_name, fmt_num(item.avg)])


def resolve_hostname(ip: str) -> str:
    """Resolve nome reverso de um IP. Retorna '-' quando indisponível."""
    try:
        return socket.gethostbyaddr(ip)[0]
    except (socket.herror, socket.gaierror, TimeoutError, OSError):
        return "-"


async def monitor(
    destino: str,
    intervalo: float,
    timeout_ms: int,
    csv_path: Path,
    max_hops: int,
    duracao_seg: float,
) -> None:
    """Fluxo principal de descoberta de rota e monitoramento contínuo."""
    hops = await discover_route(destino, max_hops=max_hops)
    stats = [HopStats(hop=hop, host=host, host_name=resolve_hostname(host)) for hop, host in hops]
    tracker = OutageTracker()
    outages_path = csv_path.with_name(f"{csv_path.stem}_quedas.csv")
    summary_path = csv_path.with_name(f"{csv_path.stem}_resumo.txt")
    latency_log_path = csv_path.with_name(f"{csv_path.stem}_latencia_log.csv")
    started_at = datetime.now()
    init_latency_log(latency_log_path)

    ciclo = 0
    start_time = asyncio.get_running_loop().time()
    while True:
        ciclo += 1
        tasks = [ping_once(item.host, timeout_ms=timeout_ms) for item in stats]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for item, result in zip(stats, results):
            if isinstance(result, BaseException):
                item.register_timeout()
                continue
            if result is None:
                item.register_timeout()
            else:
                item.register_success(float(result))

        event_time = datetime.now()
        destination_ok = bool(results) and not isinstance(results[-1], BaseException) and results[-1] is not None
        tracker.update(destination_ok=destination_ok, event_time=event_time)

        write_csv(csv_path, stats)
        write_outages_csv(outages_path, tracker)
        append_latency_log(latency_log_path, stats, timestamp=event_time)
        render_console(stats, destino=destino, csv_path=csv_path, ciclo=ciclo)
        if tracker.is_down and tracker.down_start is not None:
            print(
                f"\nSTATUS LINK: FORA DO AR desde {tracker.down_start.strftime('%Y-%m-%d %H:%M:%S')} "
                f"| quedas_finalizadas={tracker.total_outages}"
            )
        else:
            print(f"\nSTATUS LINK: ONLINE | quedas_finalizadas={tracker.total_outages}")
        print(f"Incidentes CSV: {outages_path.resolve()}")
        print(f"Resumo: {summary_path.resolve()}")
        print(f"Latência Log CSV: {latency_log_path.resolve()}")

        if duracao_seg > 0:
            elapsed = asyncio.get_running_loop().time() - start_time
            if elapsed >= duracao_seg:
                break

        await asyncio.sleep(intervalo)

    finished_at = datetime.now()
    tracker.close_open_outage(event_time=finished_at)
    write_outages_csv(outages_path, tracker)
    write_summary(summary_path, tracker, started_at=started_at, finished_at=finished_at)


def main() -> int:
    args = parse_args()

    if os.name != "nt":
        print("Aviso: este script foi otimizado para Windows (`tracert` e `ping` do Windows).")

    try:
        asyncio.run(
            monitor(
                destino=args.destino,
                intervalo=args.intervalo,
                timeout_ms=args.timeout_ms,
                csv_path=args.csv,
                max_hops=args.max_hops,
                duracao_seg=args.duracao_seg,
            )
        )
    except KeyboardInterrupt:
        print("\nMonitoramento interrompido pelo usuário (Ctrl+C).")
        return 0
    except PermissionError:
        print(
            "Erro de permissão ao executar comandos de rede. "
            "Tente abrir o terminal como Administrador."
        )
        return 2
    except FileNotFoundError as exc:
        print(f"Comando não encontrado no sistema: {exc}")
        return 3
    except Exception as exc:
        print(f"Erro fatal: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
