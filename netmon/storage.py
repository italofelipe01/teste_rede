"""Persistência das evidências: CSV (`;`), resumo TXT, logs, relatório e exportação ZIP.

Contratos de dados (cabeçalhos) estão documentados em `docs/ARCHITECTURE.md`.
Colunas novas são sempre acrescentadas ao final para manter compatibilidade com ETL/BI.
"""

from __future__ import annotations

import csv
import io
import os
import re
import shutil
import time
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .metrics import SCOPE_EXTERNAL, SCOPE_LOCAL, HopStats, OutageTracker

SNAPSHOT_HEADERS = [
    "Hop",
    "Host_IP",
    "Host_Name",
    "Sent_pkt",
    "Recv_pkt",
    "Loss_Pct",
    "Best_ms",
    "Worst_ms",
    "Avrg_ms",
    "Last_ms",
    "StDev_ms",
    "Jitter_ms",
]
OUTAGE_HEADERS = [
    "Outage_ID",
    "Start",
    "End",
    "Duration_Sec",
    "Down_Cycles",
    "Last_OK_Hop",
    "Last_OK_IP",
    "Scope",
]
LATENCY_HEADERS = ["Timestamp", "Hop", "Host_IP", "Host_Name", "Avrg_ms", "Last_ms"]
EVENT_HEADERS = ["Timestamp", "Event", "Detail"]

ARCHIVE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def fmt_num(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}"


def fmt_ts(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat(sep=" ", timespec="seconds")


def fmt_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


@dataclass(frozen=True)
class EvidencePaths:
    """Arquivos de evidência de uma sessão, derivados do caminho do CSV principal."""

    snapshot: Path
    outages: Path
    summary: Path
    latency_log: Path
    events: Path
    report: Path

    @classmethod
    def from_csv(cls, csv_path: Path) -> EvidencePaths:
        stem = csv_path.stem
        return cls(
            snapshot=csv_path,
            outages=csv_path.with_name(f"{stem}_quedas.csv"),
            summary=csv_path.with_name(f"{stem}_resumo.txt"),
            latency_log=csv_path.with_name(f"{stem}_latencia_log.csv"),
            events=csv_path.with_name(f"{stem}_eventos.csv"),
            report=csv_path.with_name(f"{stem}_relatorio.txt"),
        )

    def all(self) -> list[Path]:
        return [self.snapshot, self.outages, self.summary, self.latency_log, self.events, self.report]


# --------------------------------------------------------------------------------------
# Escrita atômica e tolerante a arquivos bloqueados (ex.: CSV aberto no Excel)
# --------------------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    for attempt in range(3):
        try:
            os.replace(temp_path, path)
            return
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.05)


def _csv_text(headers: list[str], rows: Iterable[list]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return buffer.getvalue()


class CsvAppender:
    """Acrescenta linhas a um CSV; se o arquivo estiver bloqueado, guarda e tenta no próximo ciclo."""

    def __init__(self, path: Path, headers: list[str], max_pending: int = 200_000) -> None:
        self.path = path
        self.headers = headers
        self.max_pending = max_pending
        self._pending: list[list] = []

    def reset(self) -> None:
        """Recria o arquivo apenas com o cabeçalho."""
        self._pending.clear()
        _atomic_write(self.path, _csv_text(self.headers, []))

    def append(self, rows: Iterable[list]) -> None:
        self._pending.extend(rows)
        if len(self._pending) > self.max_pending:
            del self._pending[: len(self._pending) - self.max_pending]
        self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        needs_header = not self.path.exists() or self.path.stat().st_size == 0
        with self.path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter=";", lineterminator="\n")
            if needs_header:
                writer.writerow(self.headers)
            writer.writerows(self._pending)
        self._pending.clear()


# --------------------------------------------------------------------------------------
# Contratos de saída
# --------------------------------------------------------------------------------------


def snapshot_rows(stats: Iterable[HopStats]) -> list[list]:
    return [
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
            fmt_num(item.stdev),
            fmt_num(item.jitter),
        ]
        for item in stats
    ]


def write_snapshot_csv(csv_path: Path, stats: Iterable[HopStats]) -> None:
    """Sobrescreve o CSV com o snapshot atual por salto (consumo ETL/BI)."""
    _atomic_write(csv_path, _csv_text(SNAPSHOT_HEADERS, snapshot_rows(stats)))


# Compatibilidade com o nome usado na versão 1.
write_csv = write_snapshot_csv


def outage_rows(tracker: OutageTracker) -> list[list]:
    return [
        [
            idx,
            fmt_ts(rec.start),
            fmt_ts(rec.end),
            f"{rec.duration_sec:.2f}",
            rec.down_cycles,
            rec.last_ok_hop if rec.last_ok_hop is not None else "-",
            rec.last_ok_ip or "-",
            rec.scope,
        ]
        for idx, rec in enumerate(tracker.records, start=1)
    ]


def write_outages_csv(outages_path: Path, tracker: OutageTracker) -> None:
    """Sobrescreve o CSV de quedas encerradas (evidência técnica)."""
    _atomic_write(outages_path, _csv_text(OUTAGE_HEADERS, outage_rows(tracker)))


def write_summary(
    summary_path: Path,
    tracker: OutageTracker,
    started_at: datetime,
    finished_at: datetime,
    extra: dict | None = None,
) -> None:
    """Escreve o resumo executivo `chave=valor` (inclui a queda em andamento no tempo fora do ar)."""
    lines = [
        f"monitoring_start={fmt_ts(started_at)}",
        f"monitoring_end={fmt_ts(finished_at)}",
        f"outage_count={tracker.total_outages}",
        f"total_downtime_sec={tracker.total_downtime_sec(finished_at):.2f}",
    ]
    for key, value in (extra or {}).items():
        lines.append(f"{key}={value}")
    _atomic_write(summary_path, "\n".join(lines) + "\n")


def latency_rows(stats: Iterable[HopStats], timestamp: datetime) -> list[list]:
    ts = fmt_ts(timestamp)
    return [[ts, item.hop, item.host, item.host_name, fmt_num(item.avg), fmt_num(item.last)] for item in stats]


def init_latency_log(log_path: Path) -> None:
    """Inicializa o arquivo de histórico de latência."""
    _atomic_write(log_path, _csv_text(LATENCY_HEADERS, []))


def append_latency_log(log_path: Path, stats: Iterable[HopStats], timestamp: datetime) -> None:
    """Acrescenta Avrg_ms/Last_ms por salto para a série temporal histórica."""
    CsvAppender(log_path, LATENCY_HEADERS).append(latency_rows(stats, timestamp))


# --------------------------------------------------------------------------------------
# Sessões arquivadas
# --------------------------------------------------------------------------------------


def _session_stamp(paths: EvidencePaths) -> str:
    try:
        for line in paths.summary.read_text(encoding="utf-8").splitlines():
            if line.startswith("monitoring_start="):
                started = datetime.fromisoformat(line.split("=", 1)[1].strip())
                return started.strftime("%Y%m%d-%H%M%S")
    except (OSError, ValueError):
        pass
    mtimes = [p.stat().st_mtime for p in paths.all() if p.exists()]
    stamp = datetime.fromtimestamp(min(mtimes)) if mtimes else datetime.now()
    return stamp.strftime("%Y%m%d-%H%M%S")


def archive_previous_session(paths: EvidencePaths, archive_root: Path) -> Path | None:
    """Move evidências de uma sessão anterior para `archive_root/<inicio>` (nada é apagado)."""
    for temp in paths.snapshot.parent.glob("*.tmp"):
        try:
            temp.unlink()
        except OSError:
            pass
    existing = [p for p in paths.all() if p.exists()]
    if not existing:
        return None
    base = _session_stamp(paths)
    target = archive_root / base
    suffix = 2
    while target.exists():
        target = archive_root / f"{base}-{suffix}"
        suffix += 1
    target.mkdir(parents=True)
    for path in existing:
        try:
            shutil.move(str(path), str(target / path.name))
        except OSError:
            shutil.copy2(str(path), str(target / path.name))
    return target


def prune_archives(archive_root: Path, keep: int) -> list[str]:
    """Mantém apenas as `keep` sessões arquivadas mais recentes (0 = manter todas)."""
    if keep <= 0 or not archive_root.is_dir():
        return []
    sessions = sorted((p for p in archive_root.iterdir() if p.is_dir()), key=lambda p: p.name)
    removed = []
    for path in sessions[: max(0, len(sessions) - keep)]:
        shutil.rmtree(path, ignore_errors=True)
        removed.append(path.name)
    return removed


def list_archives(archive_root: Path) -> list[dict]:
    """Lista sessões arquivadas (mais recentes primeiro) com resumo quando disponível."""
    if not archive_root.is_dir():
        return []
    result = []
    for path in sorted((p for p in archive_root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
        files = [f for f in path.iterdir() if f.is_file()]
        summary: dict[str, str] = {}
        for file in files:
            if file.name.endswith("_resumo.txt"):
                summary = parse_summary(file.read_text(encoding="utf-8", errors="replace"))
        result.append(
            {
                "name": path.name,
                "files": sorted(f.name for f in files),
                "size_bytes": sum(f.stat().st_size for f in files),
                "summary": summary,
            }
        )
    return result


def resolve_archive(archive_root: Path, name: str) -> Path | None:
    """Caminho seguro de uma sessão arquivada (impede path traversal)."""
    if not ARCHIVE_NAME_RE.match(name or "") or name in {".", ".."}:
        return None
    candidate = (archive_root / name).resolve()
    try:
        candidate.relative_to(archive_root.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_dir() else None


def parse_summary(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def build_zip(files: Iterable[Path], extra: dict[str, str] | None = None) -> bytes:
    """Compacta arquivos de evidência (e conteúdos extras gerados em memória) em um ZIP."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            if path.is_file():
                archive.write(path, arcname=path.name)
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


# --------------------------------------------------------------------------------------
# Relatório legível (para suporte técnico/provedor)
# --------------------------------------------------------------------------------------


def describe_scope(scope: str, last_ok_hop: int | None, last_ok_ip: str | None) -> str:
    if scope == SCOPE_EXTERNAL and last_ok_hop is not None:
        return f"externa: rota responde até H{last_ok_hop} ({last_ok_ip})"
    if scope == SCOPE_LOCAL:
        return "nenhum salto respondeu (rede local, roteador ou acesso do provedor)"
    return "indeterminado"


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()
    out = [line, "-" * len(line)]
    out += ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows]
    return out


def build_report(
    *,
    target: str,
    target_ip: str | None,
    started_at: datetime,
    now: datetime,
    stats: list[HopStats],
    tracker: OutageTracker,
    diagnosis: dict,
    events: list[dict],
    environment: dict,
    settings: dict,
) -> str:
    """Relatório em texto simples com o resumo da sessão, quedas, saltos e eventos."""
    session_sec = max(0.0, (now - started_at).total_seconds())
    downtime = tracker.total_downtime_sec(now)
    availability = 100.0 if session_sec <= 0 else max(0.0, 100.0 * (1 - downtime / session_sec))
    dest = next((s for s in reversed(stats) if s.is_destination), stats[-1] if stats else None)

    lines = [
        "RELATÓRIO DE ESTABILIDADE DE CONEXÃO",
        "=" * 36,
        f"Gerado em:            {fmt_ts(now)}",
        f"Destino monitorado:   {target}" + (f" ({target_ip})" if target_ip and target_ip != target else ""),
        f"Período:              {fmt_ts(started_at)} até {fmt_ts(now)} ({fmt_duration(session_sec)})",
        f"Disponibilidade:      {availability:.3f}%",
        f"Quedas confirmadas:   {len(tracker.records)}" + (" + 1 em andamento" if tracker.is_down else ""),
        f"Tempo fora do ar:     {fmt_duration(downtime)} ({downtime:.1f} s)",
    ]
    if dest is not None:
        lines += [
            f"Perda no destino:     {dest.loss_pct:.2f}% ({dest.sent - dest.recv} de {dest.sent} pacotes)",
            f"Latência no destino:  média {fmt_num(dest.avg)} ms | melhor {fmt_num(dest.best)} ms | "
            f"pior {fmt_num(dest.worst)} ms | jitter {fmt_num(dest.jitter)} ms",
        ]
    lines += [
        f"Critério de queda:    {settings.get('outage_min_cycles', 1)} ciclo(s) consecutivo(s) sem resposta do destino",
        f"Intervalo/timeout:    {settings.get('interval_sec')} s / {settings.get('timeout_ms')} ms",
        f"Sistema:              {environment.get('platform', '-')} | Python {environment.get('python', '-')}",
        "",
        f"Diagnóstico: {diagnosis.get('message', '-')}",
        "",
        "QUEDAS",
        "------",
    ]
    if tracker.records:
        rows = [
            [
                str(idx),
                fmt_ts(rec.start),
                fmt_ts(rec.end),
                fmt_duration(rec.duration_sec),
                describe_scope(rec.scope, rec.last_ok_hop, rec.last_ok_ip),
            ]
            for idx, rec in enumerate(tracker.records, start=1)
        ]
        lines += _table(["#", "Início", "Fim", "Duração", "Onde"], rows)
    else:
        lines.append("Nenhuma queda encerrada nesta sessão.")
    if tracker.is_down:
        lines.append(f"Queda em andamento desde {fmt_ts(tracker.down_start)}.")

    lines += ["", "SALTOS DA ROTA", "--------------"]
    hop_rows = [
        [
            f"H{s.hop}" + (" (destino)" if s.is_destination else ""),
            s.host,
            s.host_name,
            f"{s.loss_pct:.2f}%",
            fmt_num(s.avg),
            fmt_num(s.worst),
            fmt_num(s.jitter),
        ]
        for s in stats
    ]
    lines += _table(["Salto", "IP", "Nome", "Perda", "Média ms", "Pior ms", "Jitter ms"], hop_rows)
    lines.append("Obs.: perda apenas em saltos intermediários costuma ser limitação de ICMP dos roteadores.")

    lines += ["", "EVENTOS", "-------"]
    lines += [f"{ev['ts']}  {ev['event']:<16}  {ev['detail']}" for ev in events] or ["Sem eventos."]
    return "\n".join(lines) + "\n"
