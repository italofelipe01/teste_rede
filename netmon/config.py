"""Configuração com precedência: padrões < `config.json` < argumentos de linha de comando.

`config.json` é opcional e criado/atualizado automaticamente quando as configurações são
alteradas pelo painel. Caminhos relativos do arquivo são resolvidos a partir da pasta do
projeto (funciona mesmo quando o programa é iniciado de outra pasta ou por duplo clique).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from .probes import validate_target

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
DEFAULT_CSV_PATH = PROJECT_ROOT / "data" / "monitoramento_rota.csv"


@dataclass(frozen=True)
class Settings:
    target: str = "8.8.8.8"
    interval_sec: float = 1.0
    timeout_ms: int = 1000
    max_hops: int = 30
    outage_min_cycles: int = 3
    route_refresh_min: float = 10.0
    stats_window: int = 120
    history_points: int = 3600
    csv_path: Path = DEFAULT_CSV_PATH
    keep_sessions: int = 0
    host: str = "127.0.0.1"
    port: int = 8000
    open_browser: bool = True
    allow_remote_control: bool = False

    @property
    def archive_dir(self) -> Path:
        return self.csv_path.parent / "sessoes"

    def public_dict(self) -> dict:
        data = asdict(self)
        data["csv_path"] = str(self.csv_path)
        data["data_dir"] = str(self.csv_path.parent)
        return data


EDITABLE_FIELDS = ("target", "interval_sec", "timeout_ms", "max_hops", "outage_min_cycles", "route_refresh_min")
"""Campos que o painel pode alterar em tempo de execução (persistidos em `config.json`)."""

LIMITS: dict[str, tuple[float, float]] = {
    "interval_sec": (0.2, 3600.0),
    "timeout_ms": (100, 10000),
    "max_hops": (1, 64),
    "outage_min_cycles": (1, 120),
    "route_refresh_min": (0, 1440),
    "stats_window": (10, 100000),
    "history_points": (60, 200000),
    "keep_sessions": (0, 100000),
    "port": (0, 65535),
}

LABELS = {
    "target": "Destino",
    "interval_sec": "Intervalo entre ciclos (s)",
    "timeout_ms": "Timeout do ping (ms)",
    "max_hops": "Máximo de saltos",
    "outage_min_cycles": "Ciclos sem resposta para confirmar queda",
    "route_refresh_min": "Redescoberta da rota (min, 0 = desligado)",
    "stats_window": "Janela de métricas recentes (ciclos)",
    "history_points": "Pontos no histórico em memória",
    "keep_sessions": "Sessões arquivadas mantidas (0 = todas)",
    "port": "Porta do portal",
}

_FIELD_TYPES = {f.name: f.type for f in fields(Settings)}


def _to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "sim", "yes", "on"}:
        return True
    if text in {"0", "false", "nao", "não", "no", "off", ""}:
        return False
    raise ValueError(f"valor lógico inválido: {value!r}")


def coerce(name: str, value, base_dir: Path = PROJECT_ROOT):
    """Converte e valida um campo. Levanta `ValueError` com mensagem em português."""
    label = LABELS.get(name, name)
    if name not in _FIELD_TYPES:
        raise ValueError(f"Configuração desconhecida: {name}")
    kind = _FIELD_TYPES[name]
    try:
        if name == "target":
            return validate_target(str(value))
        if name == "csv_path":
            path = Path(os.path.expanduser(str(value)))
            return path if path.is_absolute() else (base_dir / path).resolve()
        if name == "host":
            host = str(value).strip()
            if not host or any(ch.isspace() for ch in host):
                raise ValueError("host inválido")
            return host
        if kind in ("bool", bool):
            return _to_bool(value)
        if isinstance(value, bool):
            raise ValueError("número esperado")
        number = float(value) if kind in ("float", float) else int(float(value))
        if kind in ("int", int) and float(value) != number:
            raise ValueError("número inteiro esperado")
    except (TypeError, ValueError) as exc:
        if name == "target":
            raise
        raise ValueError(f"{label}: valor inválido {value!r} ({exc}).") from None
    low, high = LIMITS.get(name, (float("-inf"), float("inf")))
    if not low <= number <= high:
        raise ValueError(f"{label}: use um valor entre {low:g} e {high:g}.")
    return number


def apply_changes(settings: Settings, changes: dict, allowed: tuple[str, ...] = EDITABLE_FIELDS) -> Settings:
    """Aplica alterações validadas a partir do painel/API."""
    updates = {}
    for name, value in (changes or {}).items():
        if name not in allowed:
            raise ValueError(f"A configuração '{name}' não pode ser alterada pelo painel.")
        updates[name] = coerce(name, value)
    return replace(settings, **updates)


def read_config_file(path: Path) -> tuple[dict, list[str]]:
    """Lê `config.json` sem nunca impedir a inicialização (erros viram avisos)."""
    if not path.exists():
        return {}, []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    except (OSError, ValueError) as exc:
        return {}, [f"Ignorando {path.name}: arquivo inválido ({exc})."]
    if not isinstance(data, dict):
        return {}, [f"Ignorando {path.name}: o conteúdo deve ser um objeto JSON."]
    return data, []


def load_settings(config_path: Path | None = None, overrides: dict | None = None) -> tuple[Settings, list[str]]:
    """Monta as configurações finais. Valores inválidos do arquivo são ignorados com aviso."""
    path = config_path or DEFAULT_CONFIG_PATH
    data, warnings = read_config_file(path)
    base_dir = path.resolve().parent
    values: dict = {}
    for name, value in data.items():
        if name.startswith("_"):
            continue
        try:
            values[name] = coerce(name, value, base_dir=base_dir)
        except ValueError as exc:
            warnings.append(f"{path.name}: {exc} Usando o padrão.")
    for name, value in (overrides or {}).items():
        if value is None:
            continue
        values[name] = coerce(name, value, base_dir=Path.cwd())
    return Settings(**values), warnings


def save_user_config(path: Path, settings: Settings, keys: tuple[str, ...] = EDITABLE_FIELDS) -> None:
    """Persiste os campos editáveis em `config.json`, preservando as demais chaves do arquivo."""
    data, _ = read_config_file(path)
    for key in keys:
        data[key] = getattr(settings, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)
