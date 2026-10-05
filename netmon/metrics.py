"""Domínio: métricas por salto, detecção de quedas e diagnóstico. Sem I/O.

Unidades explícitas: latências em `ms`, perdas em `%`, durações em `s`, contagens em `pkt`.
"""

from __future__ import annotations

import math
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

DEFAULT_WINDOW = 120
"""Quantidade de ciclos recentes usada nas métricas "recentes" e no diagnóstico."""

LOSS_WARN_PCT = 2.0
LOSS_BAD_PCT = 10.0
JITTER_WARN_MS = 30.0
INTERMEDIATE_LOSS_INFO_PCT = 20.0
MIN_SAMPLES = 10

SCOPE_LOCAL = "local"
SCOPE_EXTERNAL = "external"
SCOPE_UNKNOWN = "unknown"

HopRef = Optional[tuple[int, str]]


@dataclass
class HopStats:
    """Métricas acumuladas de conectividade para um salto da rota (atualização O(1) por amostra)."""

    hop: int
    host: str
    host_name: str = "-"
    is_destination: bool = False
    window: int = DEFAULT_WINDOW
    sent: int = 0
    recv: int = 0
    best: float | None = None
    worst: float | None = None
    last: float | None = None
    _mean: float = field(default=0.0, repr=False)
    _m2: float = field(default=0.0, repr=False)
    _prev_ok: float | None = field(default=None, repr=False)
    _jitter_sum: float = field(default=0.0, repr=False)
    _jitter_n: int = field(default=0, repr=False)
    _recent: deque = field(default_factory=deque, repr=False)

    def __post_init__(self) -> None:
        self._recent = deque(self._recent, maxlen=max(1, int(self.window)))

    def register_success(self, latency_ms: float) -> None:
        """Registra resposta de ping e atualiza latências (média/desvio por Welford)."""
        self.sent += 1
        self.recv += 1
        self.last = latency_ms

        delta = latency_ms - self._mean
        self._mean += delta / self.recv
        self._m2 += delta * (latency_ms - self._mean)

        if self.best is None or latency_ms < self.best:
            self.best = latency_ms
        if self.worst is None or latency_ms > self.worst:
            self.worst = latency_ms

        if self._prev_ok is not None:
            self._jitter_sum += abs(latency_ms - self._prev_ok)
            self._jitter_n += 1
        self._prev_ok = latency_ms
        self._recent.append(latency_ms)

    def register_timeout(self) -> None:
        """Registra envio sem resposta (timeout/perda)."""
        self.sent += 1
        self.last = None
        self._prev_ok = None
        self._recent.append(None)

    @property
    def avg(self) -> float | None:
        """Latência média acumulada (ms)."""
        return self._mean if self.recv else None

    @property
    def stdev(self) -> float | None:
        """Desvio padrão populacional da latência (ms)."""
        if not self.recv:
            return None
        return math.sqrt(max(0.0, self._m2 / self.recv))

    @property
    def jitter(self) -> float | None:
        """Jitter acumulado: média da variação absoluta entre respostas consecutivas (ms)."""
        return self._jitter_sum / self._jitter_n if self._jitter_n else None

    @property
    def loss_pct(self) -> float:
        """Percentual de perda acumulado para o salto."""
        if self.sent == 0:
            return 0.0
        return ((self.sent - self.recv) / self.sent) * 100.0

    @property
    def recent_loss_pct(self) -> float:
        """Percentual de perda na janela recente."""
        if not self._recent:
            return 0.0
        lost = sum(1 for value in self._recent if value is None)
        return lost / len(self._recent) * 100.0

    @property
    def recent_lost(self) -> int:
        """Pacotes perdidos na janela recente (pkt)."""
        return sum(1 for value in self._recent if value is None)

    @property
    def recent_avg(self) -> float | None:
        """Latência média na janela recente (ms)."""
        values = [value for value in self._recent if value is not None]
        return sum(values) / len(values) if values else None

    @property
    def recent_jitter(self) -> float | None:
        """Jitter na janela recente (ms)."""
        diffs = []
        previous = None
        for value in self._recent:
            if value is not None and previous is not None:
                diffs.append(abs(value - previous))
            previous = value
        return sum(diffs) / len(diffs) if diffs else None


@dataclass
class OutageRecord:
    """Registro de um período de indisponibilidade do destino."""

    start: datetime
    end: datetime
    duration_sec: float
    down_cycles: int
    last_ok_hop: int | None = None
    last_ok_ip: str | None = None
    scope: str = SCOPE_UNKNOWN


@dataclass
class OutageTracker:
    """Máquina de estados de queda/retorno baseada na resposta do destino final.

    Uma queda é confirmada após `min_cycles` ciclos consecutivos sem resposta do destino
    e começa no primeiro ciclo com falha. Falhas isoladas abaixo do limite contam apenas
    como perda de pacotes. O retorno ocorre na primeira resposta do destino.

    Durante a queda é registrado o salto mais distante que continuou respondendo, o que
    permite classificar a falha como `local` (nenhum salto responde) ou `external`.
    """

    min_cycles: int = 1
    is_down: bool = False
    down_start: datetime | None = None
    down_cycles: int = 0
    total_outages: int = 0
    records: list[OutageRecord] = field(default_factory=list)
    pending_cycles: int = 0
    _pending_start: datetime | None = field(default=None, repr=False)
    _breakpoints: Counter = field(default_factory=Counter, repr=False)
    _has_reference: bool = field(default=False, repr=False)

    def update(
        self,
        destination_ok: bool,
        event_time: datetime,
        last_ok: HopRef = None,
        has_reference: bool = False,
    ) -> str | None:
        """Atualiza o estado do ciclo. Retorna `"down"` ao confirmar queda, `"up"` no retorno."""
        if destination_ok:
            event = None
            if self.is_down:
                self._close(event_time)
                event = "up"
            self._clear_pending()
            return event

        if not self.is_down and self.pending_cycles == 0:
            self._pending_start = event_time
            self._breakpoints = Counter()
            self._has_reference = False
        self._breakpoints[last_ok] += 1
        self._has_reference = self._has_reference or has_reference

        if self.is_down:
            self.down_cycles += 1
            return None

        self.pending_cycles += 1
        if self.pending_cycles >= max(1, int(self.min_cycles)):
            self.is_down = True
            self.down_start = self._pending_start
            self.down_cycles = self.pending_cycles
            self.pending_cycles = 0
            self._pending_start = None
            return "down"
        return None

    def current_breakpoint(self) -> tuple[HopRef, str]:
        """Salto mais distante que respondeu durante a queda atual e o escopo da falha."""
        if not self._breakpoints:
            return None, SCOPE_UNKNOWN
        value, _count = self._breakpoints.most_common(1)[0]
        if value is not None:
            return value, SCOPE_EXTERNAL
        return None, SCOPE_LOCAL if self._has_reference else SCOPE_UNKNOWN

    def current_outage_sec(self, now: datetime) -> float:
        if not self.is_down or self.down_start is None:
            return 0.0
        return max(0.0, (now - self.down_start).total_seconds())

    def total_downtime_sec(self, now: datetime) -> float:
        """Tempo fora do ar das quedas encerradas mais a queda em andamento (s)."""
        return sum(rec.duration_sec for rec in self.records) + self.current_outage_sec(now)

    def close_open_outage(self, event_time: datetime) -> None:
        """Fecha uma queda ainda aberta no encerramento do monitoramento."""
        if self.is_down:
            self._close(event_time)
        self._clear_pending()

    def _close(self, end: datetime) -> None:
        start = self.down_start or end
        breakpoint, scope = self.current_breakpoint()
        self.records.append(
            OutageRecord(
                start=start,
                end=end,
                duration_sec=max(0.0, (end - start).total_seconds()),
                down_cycles=self.down_cycles,
                last_ok_hop=breakpoint[0] if breakpoint else None,
                last_ok_ip=breakpoint[1] if breakpoint else None,
                scope=scope,
            )
        )
        self.total_outages += 1
        self.is_down = False
        self.down_start = None
        self.down_cycles = 0
        self._breakpoints = Counter()
        self._has_reference = False

    def _clear_pending(self) -> None:
        self.pending_cycles = 0
        self._pending_start = None
        if not self.is_down:
            self._breakpoints = Counter()
            self._has_reference = False


def availability_pct(session_sec: float, downtime_sec: float) -> float:
    """Disponibilidade do destino na sessão (%)."""
    if session_sec <= 0:
        return 100.0
    return max(0.0, min(100.0, 100.0 * (1.0 - downtime_sec / session_sec)))


def destination_of(stats: list[HopStats]) -> HopStats | None:
    for item in reversed(stats):
        if item.is_destination:
            return item
    return stats[-1] if stats else None


def farthest_responding(stats: list[HopStats], results: list[float | None]) -> HopRef:
    """Salto intermediário mais distante que respondeu neste ciclo."""
    found: HopRef = None
    for item, result in zip(stats, results):
        if not item.is_destination and result is not None:
            found = (item.hop, item.host)
    return found


def has_reference_hops(stats: list[HopStats]) -> bool:
    """Indica se existe ao menos um salto intermediário que já respondeu (base para classificar falhas)."""
    return any(item.recv > 0 for item in stats if not item.is_destination)


def _br(value: float, digits: int = 1) -> str:
    """Número no formato brasileiro para mensagens (vírgula decimal)."""
    return f"{value:.{digits}f}".replace(".", ",")


@dataclass
class Diagnosis:
    """Interpretação das métricas para quem não é especialista em redes."""

    level: str
    code: str
    message: str
    suspect_hop: int | None = None
    suspect_ip: str | None = None

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "suspect_hop": self.suspect_hop,
            "suspect_ip": self.suspect_ip,
        }


def diagnose(stats: list[HopStats], tracker: OutageTracker) -> Diagnosis:
    """Classifica a situação atual (regras no estilo MTR: só conta a perda que chega ao destino)."""
    dest = destination_of(stats)
    if dest is None:
        return Diagnosis("info", "starting", "Aguardando resolução do destino e as primeiras medições.")

    if tracker.is_down:
        since = tracker.down_start.strftime("%H:%M:%S") if tracker.down_start else "-"
        breakpoint, scope = tracker.current_breakpoint()
        if breakpoint is not None:
            return Diagnosis(
                "bad",
                "outage",
                f"Queda em andamento desde {since}: a rota responde até H{breakpoint[0]} ({breakpoint[1]}) "
                "e a falha está depois desse ponto (provedor ou rota externa).",
                suspect_hop=breakpoint[0],
                suspect_ip=breakpoint[1],
            )
        if scope == SCOPE_LOCAL:
            return Diagnosis(
                "bad",
                "outage",
                f"Queda em andamento desde {since}: nenhum salto responde. Verifique Wi-Fi/cabo, "
                "o roteador e o acesso do provedor (modem/ONU).",
            )
        return Diagnosis("bad", "outage", f"Queda em andamento desde {since}: o destino não responde.")

    if dest.sent < MIN_SAMPLES:
        return Diagnosis("info", "collecting", f"Coletando amostras ({dest.sent}/{MIN_SAMPLES})...")

    intermediates = [item for item in stats if not item.is_destination]
    if dest.recv == 0:
        if any(item.recv > 0 for item in intermediates):
            alternative = "1.1.1.1" if dest.host == "8.8.8.8" else "8.8.8.8"
            return Diagnosis(
                "warn",
                "destination_never_responded",
                "O destino não respondeu nenhuma vez, embora saltos da rota respondam: ou ele bloqueia ping (ICMP) "
                f"ou a falha começou antes do monitoramento. Teste outro destino (ex.: {alternative}).",
                suspect_hop=dest.hop,
                suspect_ip=dest.host,
            )
        return Diagnosis("bad", "no_response", "Nenhuma resposta do destino nem dos saltos até agora.")

    dest_loss = dest.recent_loss_pct
    # Uma perda isolada não caracteriza problema: exige ao menos 2 pacotes perdidos na janela.
    if dest_loss >= LOSS_WARN_PCT and dest.recent_lost >= 2:
        level = "bad" if dest_loss >= LOSS_BAD_PCT else "warn"
        responsive = [item for item in intermediates if item.recv > 0]
        threshold = dest_loss * 0.5
        origin_idx = None
        for idx in range(len(responsive)):
            if all(item.recent_loss_pct >= threshold for item in responsive[idx:]):
                origin_idx = idx
                break
        if origin_idx is None:
            return Diagnosis(
                level,
                "loss_at_destination",
                f"Perda de {_br(dest_loss)}% no destino, com saltos intermediários estáveis: "
                "a perda ocorre no trecho final da rota ou no próprio destino.",
                suspect_hop=dest.hop,
                suspect_ip=dest.host,
            )
        origin = responsive[origin_idx]
        if origin.hop <= 1:
            return Diagnosis(
                level,
                "loss_local",
                f"Perda de {_br(dest_loss)}% no destino que já começa no primeiro salto (H{origin.hop} {origin.host}): "
                "indica problema na rede local (Wi-Fi, cabo ou roteador).",
                suspect_hop=origin.hop,
                suspect_ip=origin.host,
            )
        previous = responsive[origin_idx - 1] if origin_idx > 0 else None
        after = f" após H{previous.hop} ({previous.host})" if previous else ""
        return Diagnosis(
            level,
            "loss_external",
            f"Perda de {_br(dest_loss)}% no destino que começa em H{origin.hop} ({origin.host}) e persiste até o fim "
            f"da rota: provável problema no provedor/rota{after}.",
            suspect_hop=origin.hop,
            suspect_ip=origin.host,
        )

    jitter = dest.recent_jitter
    if jitter is not None and jitter >= JITTER_WARN_MS:
        return Diagnosis(
            "warn",
            "high_jitter",
            f"Latência instável no destino: jitter recente de {_br(jitter)} ms "
            "(chamadas de vídeo e jogos podem sofrer).",
            suspect_hop=dest.hop,
            suspect_ip=dest.host,
        )

    lossy = [item for item in intermediates if item.recv > 0 and item.recent_loss_pct >= INTERMEDIATE_LOSS_INFO_PCT]
    avg = dest.recent_avg
    avg_txt = f"{_br(avg)} ms" if avg is not None else "-"
    if lossy:
        names = ", ".join(f"H{item.hop}" for item in lossy[:5])
        return Diagnosis(
            "ok",
            "intermediate_icmp_limit",
            f"Conexão estável (latência média {avg_txt}). Perda aparente apenas em saltos intermediários ({names}): "
            "roteadores limitam respostas a ping e isso não afeta o destino.",
        )

    outages = len(tracker.records)
    history = f" {outages} queda(s) registrada(s) nesta sessão." if outages else ""
    return Diagnosis(
        "ok",
        "stable",
        f"Conexão estável: perda recente de {_br(dest_loss)}% e latência média de {avg_txt} no destino.{history}",
    )
