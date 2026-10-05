"""Motor de monitoramento contínuo, compartilhado pelo portal web e pelo modo console.

Roda em uma thread própria com loop asyncio e nunca encerra sozinho por falhas de rede:
- começa a monitorar o destino imediatamente e adiciona os saltos quando a rota é descoberta;
- tenta de novo (com espera crescente) se o DNS, a rota ou o `ping` falharem;
- redescobre a rota periodicamente e registra mudanças como eventos;
- tolera arquivos de evidência bloqueados (ex.: abertos no Excel) sem perder dados.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import storage
from .config import EDITABLE_FIELDS, LABELS, Settings, apply_changes
from .metrics import (
    JITTER_WARN_MS,
    LOSS_BAD_PCT,
    LOSS_WARN_PCT,
    HopStats,
    OutageTracker,
    availability_pct,
    destination_of,
    diagnose,
    farthest_responding,
    has_reference_hops,
)
from .probes import Prober, ProbeUnavailable, RouteDiscovery, environment_report, resolve_ipv4, reverse_lookup

log = logging.getLogger("netmon")

ROUTE_RETRY_MIN_S = 15.0
ROUTE_RETRY_MAX_S = 300.0
RESOLVE_RETRY_S = 10.0
PROBE_RETRY_S = 15.0
MAX_EVENTS = 300
REPORT_EVERY_S = 60.0

ProberFactory = Callable[[Settings], Prober]


def _round(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def hop_to_dict(item: HopStats) -> dict:
    """Representação de um salto na API (chaves compatíveis com a versão 1 + métricas novas)."""
    return {
        "Hop": item.hop,
        "Host_IP": item.host,
        "Host_Name": item.host_name,
        "Sent": item.sent,
        "Recv": item.recv,
        "Loss_Pct": round(item.loss_pct, 2),
        "Best": _round(item.best),
        "Worst": _round(item.worst),
        "Avrg": _round(item.avg),
        "Last": _round(item.last),
        "StDev": _round(item.stdev),
        "Jitter": _round(item.jitter),
        "Recent_Loss_Pct": round(item.recent_loss_pct, 2),
        "Recent_Avrg": _round(item.recent_avg),
        "Is_Destination": item.is_destination,
    }


class Session:
    """Estado de uma sessão de monitoramento (um destino, um conjunto de evidências)."""

    def __init__(self, settings: Settings, started_at: datetime) -> None:
        self.id = started_at.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        self.target = settings.target
        self.target_ip: str | None = None
        self.stats: list[HopStats] = []
        self.tracker = OutageTracker(min_cycles=settings.outage_min_cycles)
        self.started_at = started_at
        self.finished_at: datetime | None = None
        self.cycle = 0
        self.last_update: datetime | None = None
        self.paths = storage.EvidencePaths.from_csv(settings.csv_path)
        self.latency_log = storage.CsvAppender(self.paths.latency_log, storage.LATENCY_HEADERS)
        self.events_log = storage.CsvAppender(self.paths.events, storage.EVENT_HEADERS)
        self.events: deque = deque(maxlen=MAX_EVENTS)
        self.names: dict[str, str] = {}
        self.outages_written = -1
        self.report_written_at: float | None = None
        # Rota
        self.route_source = "-"
        self.route_reached = False
        self.route_discovered_at: datetime | None = None
        self.route_changes = 0
        self.route_ok_loop_time: float | None = None
        self.next_route_at = 0.0
        self.route_backoff = ROUTE_RETRY_MIN_S
        self.route_running = False


class MonitorEngine:
    """Coordena sessões de monitoramento e expõe estado thread-safe para API e console."""

    def __init__(
        self,
        settings: Settings,
        prober_factory: ProberFactory | None = None,
        on_cycle: Callable[[MonitorEngine], None] | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.settings = settings
        self._prober_factory = prober_factory or (lambda s: Prober(timeout_ms=s.timeout_ms))
        self._on_cycle = on_cycle
        self._clock = clock
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._actions: set[str] = set()
        self._name_tasks: set[asyncio.Future] = set()
        self.instance_id = secrets.token_hex(4)
        self.started_at = clock()
        self.session: Session | None = None
        self.history: deque = deque(maxlen=settings.history_points)
        self.seq = 0
        self.version = 0
        self.phase = "starting"
        self.error: str | None = None
        self.warnings: dict[str, str] = {}
        self.environment = environment_report()

    # ------------------------------------------------------------------ ciclo de vida

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, name="netmon-engine", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Encerra o monitoramento finalizando as evidências da sessão."""
        self._stop.set()
        self._wake_loop()
        with self._cond:
            self._cond.notify_all()
        if self._thread:
            self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception:  # pragma: no cover - proteção final
            log.exception("Motor de monitoramento encerrado por erro inesperado")
        finally:
            with self._cond:
                self.phase = "stopped"
                self._touch()

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        while not self._stop.is_set():
            with self._lock:
                self._actions.difference_update({"restart", "reset"})
            try:
                await self._run_session()
            except Exception as exc:  # pragma: no cover - proteção final
                log.exception("Falha inesperada na sessão de monitoramento")
                self._set_phase("error", f"Falha inesperada no monitor: {exc}")
                await self._sleep(5)
        for task in list(self._name_tasks):
            task.cancel()

    # ------------------------------------------------------------------ comandos (thread-safe)

    def update_settings(self, changes: dict) -> tuple[Settings, bool]:
        """Valida e aplica alterações. Retorna (configurações, se uma nova sessão será iniciada)."""
        with self._lock:
            old = self.settings
            new = apply_changes(old, changes)
            self.settings = new
            restart = new.target != old.target
            actions = {"restart"} if restart else {"live"}
            if not restart and new.max_hops != old.max_hops:
                actions.add("rediscover")
            if new.history_points != old.history_points:
                self.history = deque(self.history, maxlen=new.history_points)
            self._touch()
            session = self.session
        changed = [
            f"{LABELS.get(key, key)}: {getattr(old, key)} > {getattr(new, key)}"
            for key in EDITABLE_FIELDS
            if getattr(old, key) != getattr(new, key)
        ]
        if session is not None and changed:
            self._event(session, self._clock(), "settings_changed", " | ".join(changed))
        self._signal(*actions)
        return new, restart

    def request_rediscover(self) -> None:
        self._signal("rediscover")

    def reset_session(self) -> None:
        """Arquiva a sessão atual e inicia uma nova com as mesmas configurações."""
        self._signal("reset")

    def _signal(self, *actions: str) -> None:
        with self._lock:
            self._actions.update(actions)
        self._wake_loop()

    def _take_actions(self) -> set[str]:
        with self._lock:
            actions = set(self._actions)
            self._actions.clear()
            return actions

    def _wake_loop(self) -> None:
        loop, wake = self._loop, self._wake
        if loop is None or wake is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(wake.set)

    async def _sleep(self, seconds: float) -> None:
        if seconds <= 0 or self._stop.is_set() or self._actions or self._wake is None:
            return
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        finally:
            self._wake.clear()

    # ------------------------------------------------------------------ sessão

    async def _run_session(self) -> None:
        session = self._begin_session()
        try:
            await self._session_loop(session)
        finally:
            self._end_session(session)

    def _begin_session(self) -> Session:
        settings = self.settings
        now = self._clock()
        session = Session(settings, now)
        archived = None
        try:
            archived = storage.archive_previous_session(session.paths, settings.archive_dir)
            storage.prune_archives(settings.archive_dir, settings.keep_sessions)
        except OSError as exc:
            self._warn("archive", f"Não foi possível arquivar a sessão anterior: {exc}")
        try:
            session.latency_log.reset()
            session.events_log.reset()
        except OSError as exc:
            self._warn("storage", f"Não foi possível criar os arquivos de evidência: {exc}")
        with self._cond:
            self.session = session
            self.history.clear()
            self.phase = "resolving"
            self.error = None
            self._touch()
        detail = (
            f"Destino {settings.target} | intervalo {settings.interval_sec:g} s | timeout {settings.timeout_ms} ms | "
            f"queda após {settings.outage_min_cycles} ciclo(s)"
        )
        if archived is not None:
            detail += f" | sessão anterior arquivada em {archived.name}"
        self._event(session, now, "session_start", detail)
        return session

    def _end_session(self, session: Session) -> None:
        now = self._clock()
        with self._cond:
            session.tracker.close_open_outage(now)
            session.finished_at = now
        self._event(session, now, "session_end", f"Sessão encerrada após {session.cycle} ciclo(s)")
        with self._cond:
            self._persist(session, now, final=True)
            self._touch()

    async def _session_loop(self, session: Session) -> None:
        loop = asyncio.get_running_loop()
        prober: Prober | None = None
        route_task: asyncio.Future | None = None
        resolve_at = 0.0
        try:
            while not self._stop.is_set():
                actions = self._take_actions()
                if actions & {"restart", "reset"}:
                    return
                settings = self.settings
                if "rediscover" in actions:
                    session.next_route_at = 0.0
                if "live" in actions:
                    session.tracker.min_cycles = settings.outage_min_cycles
                    if prober is not None:
                        prober.timeout_ms = settings.timeout_ms
                    if session.route_ok_loop_time is not None and session.route_reached:
                        session.next_route_at = self._next_refresh(session.route_ok_loop_time, settings)
                tick = loop.time()
                try:
                    if prober is None:
                        prober = self._prober_factory(settings)
                        await prober.prepare()
                    if session.target_ip is None and tick >= resolve_at:
                        if not await self._resolve(session):
                            resolve_at = tick + RESOLVE_RETRY_S
                    if session.target_ip and route_task is None and tick >= session.next_route_at:
                        session.route_running = True
                        route_task = asyncio.ensure_future(prober.discover_route(session.target_ip, settings.max_hops))
                        if not session.route_reached:
                            self._set_phase("discovering")
                    if route_task is not None and route_task.done():
                        self._apply_discovery(session, route_task)
                        route_task = None
                    if session.stats:
                        await self._cycle(session, prober)
                except ProbeUnavailable as exc:
                    prober = None
                    self._set_phase("error", str(exc))
                    await self._sleep(PROBE_RETRY_S)
                    continue
                except Exception as exc:
                    log.exception("Falha no ciclo de monitoramento")
                    self._set_phase("error", f"Falha no ciclo de monitoramento: {exc}")
                await self._sleep(max(0.05, settings.interval_sec - (loop.time() - tick)))
        finally:
            if route_task is not None and not route_task.done():
                route_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await route_task

    async def _resolve(self, session: Session) -> bool:
        self._set_phase("resolving")
        loop = asyncio.get_running_loop()
        try:
            ip = await loop.run_in_executor(None, resolve_ipv4, session.target)
        except OSError as exc:
            self._set_phase(
                "error",
                f"Não foi possível resolver '{session.target}' ({exc}). Nova tentativa em {RESOLVE_RETRY_S:.0f} s.",
            )
            return False
        now = self._clock()
        with self._cond:
            session.target_ip = ip
            session.stats = [HopStats(hop=1, host=ip, is_destination=True, window=self.settings.stats_window)]
            self.phase = "discovering"
            self.error = None
            self._touch()
        if ip != session.target:
            self._event(session, now, "target_resolved", f"{session.target} resolvido para {ip}")
        self._schedule_names(session, [ip])
        return True

    @staticmethod
    def _next_refresh(base_loop_time: float, settings: Settings) -> float:
        if settings.route_refresh_min <= 0:
            return float("inf")
        return base_loop_time + settings.route_refresh_min * 60.0

    # ------------------------------------------------------------------ rota

    def _apply_discovery(self, session: Session, task: asyncio.Future) -> None:
        loop_time = asyncio.get_running_loop().time()
        session.route_running = False
        result: RouteDiscovery | None = None
        try:
            result = task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.warning("Falha na descoberta de rota: %s", exc)

        if result is None or not result.hops or (session.route_reached and not result.reached):
            session.next_route_at = loop_time + session.route_backoff
            session.route_backoff = min(ROUTE_RETRY_MAX_S, session.route_backoff * 2)
            if not session.route_reached:
                self._warn(
                    "route",
                    f"Rota ainda não descoberta (nova tentativa em {session.next_route_at - loop_time:.0f} s). "
                    "O destino continua sendo monitorado.",
                )
            return

        if result.reached:
            session.route_reached = True
            session.route_ok_loop_time = loop_time
            session.route_backoff = ROUTE_RETRY_MIN_S
            session.next_route_at = self._next_refresh(loop_time, self.settings)
            self._clear_warning("route")
        else:
            session.next_route_at = loop_time + session.route_backoff
            session.route_backoff = min(ROUTE_RETRY_MAX_S, session.route_backoff * 2)
            self._warn(
                "route",
                "O destino não respondeu à descoberta de rota; monitorando os saltos encontrados "
                f"(nova tentativa em {session.next_route_at - loop_time:.0f} s).",
            )

        now = self._clock()
        with self._cond:
            old_ips = [s.host for s in session.stats]
            first = session.route_discovered_at is None
            new_stats = self._merge_route(session, result)
            new_ips = [s.host for s in new_stats]
            session.stats = new_stats
            session.route_source = result.source
            session.route_discovered_at = now
            changed = not first and old_ips != new_ips
            if changed:
                session.route_changes += 1
            self._touch()
        if first:
            route_txt = " > ".join(new_ips)
            self._event(session, now, "route_discovered", f"{len(new_ips)} salto(s) via {result.source}: {route_txt}")
        elif changed:
            self._event(session, now, "route_changed", self._describe_route_change(old_ips, new_ips))
        self._schedule_names(session, [ip for ip in new_ips if ip not in session.names])

    def _merge_route(self, session: Session, result: RouteDiscovery) -> list[HopStats]:
        dest_ip = session.target_ip or ""
        responders = {ttl: ip for ttl, ip in result.hops}
        dest_ttl = next((ttl for ttl, ip in result.hops if ip == dest_ip), None)
        # Saltos que não responderam nesta varredura mantêm o IP conhecido (evita "mudanças" falsas).
        for item in session.stats:
            if item.is_destination or item.hop in responders:
                continue
            if dest_ttl is None or item.hop < dest_ttl:
                responders[item.hop] = item.host
        hops = [(ttl, ip) for ttl, ip in sorted(responders.items()) if ip != dest_ip]
        deduped: list[tuple[int, str]] = []
        seen: set[str] = set()
        for ttl, ip in hops:
            if ip not in seen:
                seen.add(ip)
                deduped.append((ttl, ip))
        if dest_ttl is None:
            dest_ttl = (deduped[-1][0] + 1) if deduped else 1
        deduped = [(ttl, ip) for ttl, ip in deduped if ttl < dest_ttl]
        deduped.append((dest_ttl, dest_ip))

        existing = {item.host: item for item in session.stats}
        merged = []
        for ttl, ip in deduped:
            item = existing.get(ip) or HopStats(
                hop=ttl, host=ip, host_name=session.names.get(ip, "-"), window=self.settings.stats_window
            )
            item.hop = ttl
            item.is_destination = ip == dest_ip
            merged.append(item)
        return merged

    @staticmethod
    def _describe_route_change(old_ips: list[str], new_ips: list[str]) -> str:
        removed = [ip for ip in old_ips if ip not in new_ips]
        added = [ip for ip in new_ips if ip not in old_ips]
        parts = []
        if added:
            parts.append("novos: " + ", ".join(added))
        if removed:
            parts.append("removidos: " + ", ".join(removed))
        if not parts:
            parts.append("ordem dos saltos alterada")
        return f"Rota alterada ({len(old_ips)} > {len(new_ips)} saltos) | " + " | ".join(parts)

    def _schedule_names(self, session: Session, ips: list[str]) -> None:
        for ip in ips:
            task = asyncio.ensure_future(self._resolve_name(session, ip))
            self._name_tasks.add(task)
            task.add_done_callback(self._name_tasks.discard)

    async def _resolve_name(self, session: Session, ip: str) -> None:
        name = await asyncio.get_running_loop().run_in_executor(None, reverse_lookup, ip)
        with self._cond:
            session.names[ip] = name
            for item in session.stats:
                if item.host == ip:
                    item.host_name = name
            self._touch()

    # ------------------------------------------------------------------ ciclo de medição

    async def _cycle(self, session: Session, prober: Prober) -> None:
        stats = list(session.stats)
        results = await asyncio.gather(*(prober.ping(item.host) for item in stats), return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors and len(errors) == len(results) and isinstance(errors[0], OSError):
            raise ProbeUnavailable(f"Falha ao executar o ping: {errors[0]}")
        values: list[float | None] = [
            float(r) if isinstance(r, (int, float)) and not isinstance(r, bool) else None for r in results
        ]
        now = self._clock()
        event = None
        with self._cond:
            for item, value in zip(stats, values):
                if value is None:
                    item.register_timeout()
                else:
                    item.register_success(value)
            dest_ok = values[-1] is not None
            tracker = session.tracker
            event = tracker.update(dest_ok, now, farthest_responding(stats, values), has_reference_hops(stats))
            session.cycle += 1
            session.last_update = now
            self.seq += 1
            self.history.append(
                {
                    "seq": self.seq,
                    "ts": storage.fmt_ts(now),
                    "t": int(now.timestamp() * 1000),
                    "up": dest_ok,
                    "rtt": {item.host: _round(value) for item, value in zip(stats, values)},
                }
            )
            if self.phase != "monitoring" or self.error:
                self.phase = "monitoring"
                self.error = None
            try:
                session.latency_log.append(storage.latency_rows(stats, now))
                self._clear_warning("latency_log", touch=False)
            except OSError as exc:
                self._warn("latency_log", self._locked_file_msg(session.paths.latency_log, exc), touch=False)
            self._persist(session, now)
            self._touch()
        if event == "down":
            since = storage.fmt_ts(tracker.down_start)
            self._event(session, now, "outage_start", f"Destino sem resposta desde {since}")
        elif event == "up":
            record = tracker.records[-1]
            where = storage.describe_scope(record.scope, record.last_ok_hop, record.last_ok_ip)
            self._event(
                session,
                now,
                "outage_end",
                f"Queda #{len(tracker.records)} encerrada: {record.duration_sec:.1f} s ({where})",
            )
        if self._on_cycle is not None:
            try:
                self._on_cycle(self)
            except Exception:  # pragma: no cover - callback de apresentação
                log.exception("Falha no callback de ciclo")

    # ------------------------------------------------------------------ persistência

    @staticmethod
    def _locked_file_msg(path, exc: OSError) -> str:
        return (
            f"Não foi possível gravar {path.name} ({exc.strerror or exc}). Se o arquivo estiver aberto em outro "
            "programa (ex.: Excel), feche-o: os dados ficam guardados e são gravados em seguida."
        )

    def _summary_extra(self, session: Session, now: datetime) -> dict:
        tracker = session.tracker
        session_sec = max(0.0, (now - session.started_at).total_seconds())
        dest = destination_of(session.stats)
        return {
            "target": session.target,
            "target_ip": session.target_ip or "",
            "cycles": session.cycle,
            "session_sec": f"{session_sec:.2f}",
            "availability_pct": f"{availability_pct(session_sec, tracker.total_downtime_sec(now)):.3f}",
            "link_status": "down" if tracker.is_down else "up",
            "down_since": storage.fmt_ts(tracker.down_start) if tracker.is_down else "",
            "destination_loss_pct": f"{dest.loss_pct:.2f}" if dest else "",
            "destination_avg_ms": storage.fmt_num(dest.avg) if dest else "",
            "destination_jitter_ms": storage.fmt_num(dest.jitter) if dest else "",
            "outage_min_cycles": tracker.min_cycles,
        }

    def _persist(self, session: Session, now: datetime, final: bool = False) -> None:
        """Grava snapshot, quedas e resumo (chamar com o lock adquirido)."""
        paths = session.paths
        try:
            if session.stats:
                storage.write_snapshot_csv(paths.snapshot, session.stats)
            if final or session.outages_written != len(session.tracker.records):
                storage.write_outages_csv(paths.outages, session.tracker)
                session.outages_written = len(session.tracker.records)
            extra = self._summary_extra(session, now)
            storage.write_summary(paths.summary, session.tracker, session.started_at, now, extra)
            written = session.report_written_at
            report_due = written is None or time.monotonic() - written >= REPORT_EVERY_S
            if final or report_due:
                # Atualizado periodicamente: o relatório existe mesmo se a janela for fechada abruptamente.
                paths.report.write_text(self._report_text(session, now), encoding="utf-8")
                session.report_written_at = time.monotonic()
            if final:
                session.latency_log.flush()
                session.events_log.flush()
            self._clear_warning("storage", touch=False)
        except OSError as exc:
            self._warn("storage", self._locked_file_msg(paths.snapshot, exc), touch=False)

    def _event(self, session: Session, ts: datetime, kind: str, detail: str) -> None:
        entry = {"ts": storage.fmt_ts(ts), "event": kind, "detail": detail}
        with self._cond:
            session.events.append(entry)
            try:
                session.events_log.append([[entry["ts"], kind, detail]])
            except OSError as exc:
                self._warn("events_log", self._locked_file_msg(session.paths.events, exc), touch=False)
            self._touch()
        log.info("%s | %s", kind, detail)

    # ------------------------------------------------------------------ estado

    def _touch(self) -> None:
        """Marca mudança de estado e acorda quem aguarda (SSE). Chamar com o lock adquirido."""
        self.version += 1
        self._cond.notify_all()

    def _set_phase(self, phase: str, error: str | None = None) -> None:
        with self._cond:
            if self.phase == phase and self.error == error:
                return
            self.phase = phase
            self.error = error
            self._touch()

    def _warn(self, key: str, message: str, touch: bool = True) -> None:
        with self._cond:
            if self.warnings.get(key) != message:
                self.warnings[key] = message
                if touch:
                    self._touch()

    def _clear_warning(self, key: str, touch: bool = True) -> None:
        with self._cond:
            if self.warnings.pop(key, None) is not None and touch:
                self._touch()

    def wait_for_change(self, version: int, timeout: float) -> int:
        """Bloqueia até o estado mudar (ou timeout) e retorna a versão atual."""
        with self._cond:
            self._cond.wait_for(lambda: self.version != version or self._stop.is_set(), timeout=timeout)
            return self.version

    def history_since(self, since: int = 0, instance: str | None = None) -> dict:
        """Pontos do histórico em memória com `seq > since` (incremental para a API)."""
        with self._lock:
            reset = bool(instance) and instance != self.instance_id
            if reset:
                since = 0
            points = []
            for point in reversed(self.history):
                if point["seq"] <= since:
                    break
                points.append(point)
            points.reverse()
            if since and self.history and since < self.history[0]["seq"] - 1:
                reset = True
            return {
                "instance": self.instance_id,
                "session_id": self.session.id if self.session else None,
                "latest_seq": self.seq,
                "reset": reset,
                "points": points,
            }

    def _report_text(self, session: Session, now: datetime) -> str:
        return storage.build_report(
            target=session.target,
            target_ip=session.target_ip,
            started_at=session.started_at,
            now=session.finished_at or now,
            stats=session.stats,
            tracker=session.tracker,
            diagnosis=diagnose(session.stats, session.tracker).to_dict(),
            events=list(session.events),
            environment=self.environment,
            settings=self.settings.public_dict(),
        )

    def snapshot(self) -> dict:
        """Estado completo para a API (chaves da versão 1 preservadas)."""
        with self._lock:
            now = self._clock()
            settings = self.settings
            session = self.session
            base = {
                "app": "netmon",
                "instance": self.instance_id,
                "seq": self.seq,
                "version": self.version,
                "server_time": storage.fmt_ts(now),
                "phase": self.phase,
                "error": self.error,
                "warnings": list(self.warnings.values()),
                "settings": {key: getattr(settings, key) for key in EDITABLE_FIELDS},
                "thresholds": {
                    "loss_warn_pct": LOSS_WARN_PCT,
                    "loss_bad_pct": LOSS_BAD_PCT,
                    "jitter_warn_ms": JITTER_WARN_MS,
                },
            }
            if session is None:
                base.update({"destino": settings.target, "cycle": 0, "last_update": None, "hops": [], "outages": []})
                return base

            tracker = session.tracker
            end = session.finished_at or now
            session_sec = max(0.0, (end - session.started_at).total_seconds())
            downtime = tracker.total_downtime_sec(end)
            dest = destination_of(session.stats)
            breakpoint, scope = tracker.current_breakpoint() if tracker.is_down else (None, None)
            if dest is None or dest.sent == 0:
                link = "unknown"
            elif tracker.is_down:
                link = "down"
            elif tracker.pending_cycles > 0:
                link = "checking"
            else:
                link = "up"
            next_route = None
            if session.target_ip and self._loop is not None and session.next_route_at != float("inf"):
                with contextlib.suppress(RuntimeError):
                    next_route = max(0.0, session.next_route_at - self._loop.time())

            base.update(
                {
                    "session_id": session.id,
                    "destino": session.target,
                    "cycle": session.cycle,
                    "last_update": session.last_update.isoformat() if session.last_update else None,
                    "target": {
                        "input": session.target,
                        "ip": session.target_ip,
                        "name": session.names.get(session.target_ip or "", "-"),
                    },
                    "hops": [hop_to_dict(item) for item in session.stats],
                    "outages": [
                        {
                            "Outage_ID": idx,
                            "Start": storage.fmt_ts(rec.start),
                            "End": storage.fmt_ts(rec.end),
                            "Duration_Sec": round(rec.duration_sec, 2),
                            "Down_Cycles": rec.down_cycles,
                            "Last_OK_Hop": rec.last_ok_hop,
                            "Last_OK_IP": rec.last_ok_ip,
                            "Scope": rec.scope,
                        }
                        for idx, rec in enumerate(tracker.records, start=1)
                    ],
                    "summary": {
                        "monitoring_start": storage.fmt_ts(session.started_at),
                        "monitoring_end": storage.fmt_ts(end),
                        "outage_count": tracker.total_outages,
                        "total_downtime_sec": round(downtime, 2),
                        "session_sec": round(session_sec, 2),
                        "availability_pct": round(availability_pct(session_sec, downtime), 3),
                    },
                    "status": {
                        "link": link,
                        "down_since": storage.fmt_ts(tracker.down_start) if tracker.is_down else None,
                        "current_outage_sec": round(tracker.current_outage_sec(end), 1),
                        "pending_failures": tracker.pending_cycles,
                        "breakpoint_hop": breakpoint[0] if breakpoint else None,
                        "breakpoint_ip": breakpoint[1] if breakpoint else None,
                        "scope": scope,
                    },
                    "route": {
                        "source": session.route_source,
                        "complete": session.route_reached,
                        "discovered_at": storage.fmt_ts(session.route_discovered_at) or None,
                        "changes": session.route_changes,
                        "discovering": session.route_running,
                        "next_refresh_sec": round(next_route, 0) if next_route is not None else None,
                    },
                    "diagnosis": diagnose(session.stats, tracker).to_dict(),
                    "events": list(session.events)[-50:],
                }
            )
            return base

    # ------------------------------------------------------------------ exportação

    def export_zip(self) -> tuple[str, bytes]:
        """ZIP com as evidências da sessão atual + relatório legível + snapshot JSON."""
        with self._lock:
            session = self.session
            if session is None:
                raise RuntimeError("Nenhuma sessão ativa.")
            now = self._clock()
            report = self._report_text(session, now)
            snapshot = self.snapshot()
            with contextlib.suppress(OSError):
                session.latency_log.flush()
                session.events_log.flush()
            files = [p for p in session.paths.all() if p != session.paths.report]
            target = session.target
        safe_target = re.sub(r"[^A-Za-z0-9_.-]", "_", target)
        name = f"evidencias_{safe_target}_{now.strftime('%Y%m%d-%H%M%S')}.zip"
        extra = {
            session.paths.report.name: report,
            "snapshot.json": json.dumps(snapshot, ensure_ascii=False, indent=2),
        }
        return name, storage.build_zip(files, extra)

    def current_files(self) -> dict[str, Path]:
        """Arquivos de evidência da sessão atual, por nome (para `/data/<nome>`)."""
        with self._lock:
            session = self.session
            if session is None:
                return {}
            return {path.name: path for path in session.paths.all()}

    def list_sessions(self) -> list[dict]:
        return storage.list_archives(self.settings.archive_dir)
