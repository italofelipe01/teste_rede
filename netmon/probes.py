"""Sondas de rede multiplataforma (Windows, Linux e macOS) baseadas no `ping` do sistema.

Princípios:
- Não exige privilégios de administrador nem bibliotecas externas.
- Parsing independente de idioma: usa apenas IPs, o marcador `ttl=` e valores em `ms`,
  que aparecem iguais no Windows em português, inglês ou outros idiomas.
- A rota é descoberta com pings paralelos de TTL crescente (rápido: ~1 timeout no total);
  `tracert`/`traceroute`/`tracepath` ficam como alternativa automática.
"""

from __future__ import annotations

import asyncio
import ipaddress
import math
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field

IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
TRACE_HOP_RE = re.compile(r"^\s*(\d+)\??:?\s+(.*)$")
RTT_RE = re.compile(r"([=<])\s*(\d+(?:[.,]\d+)?)\s*ms\b", re.IGNORECASE)
HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}\.?$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$"
)

WINDOWS = os.name == "nt"


def detect_ping_style() -> str:
    """Identifica a família de sintaxe do `ping`: `windows`, `bsd` (macOS/FreeBSD) ou `linux`."""
    if WINDOWS:
        return "windows"
    if sys.platform.startswith(("darwin", "freebsd")):
        return "bsd"
    return "linux"


PING_STYLE = detect_ping_style()


def _child_env() -> dict[str, str] | None:
    """Força saída em inglês/locale C nos sistemas POSIX (parsing previsível)."""
    if WINDOWS:
        return None
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return env


_CHILD_ENV = _child_env()


class ProbeUnavailable(RuntimeError):
    """Ferramenta de rede ausente ou inutilizável neste sistema."""


@dataclass
class RouteDiscovery:
    """Resultado de uma descoberta de rota."""

    hops: list[tuple[int, str]] = field(default_factory=list)
    source: str = "-"
    reached: bool = False


# --------------------------------------------------------------------------------------
# Parsing (funções puras, cobertas por testes com saídas reais de cada sistema)
# --------------------------------------------------------------------------------------


def first_ipv4(text: str) -> str | None:
    """Retorna o primeiro IPv4 válido encontrado no texto."""
    for candidate in IPV4_RE.findall(text or ""):
        try:
            return str(ipaddress.IPv4Address(candidate))
        except ValueError:
            continue
    return None


def parse_ping_rtt(output: str) -> float | None:
    """Extrai a latência (ms) de uma resposta de eco do `ping`. Retorna `None` sem resposta.

    Só considera linhas de resposta de eco (contêm `ttl=`), ignorando mensagens de
    "TTL expirado"/"Destino inacessível" e o bloco de estatísticas.
    `tempo<1ms` do Windows é registrado como 0.5 ms.
    """
    for line in (output or "").splitlines():
        if "ttl=" not in line.lower():
            continue
        match = RTT_RE.search(line)
        if not match:
            continue
        value = float(match.group(2).replace(",", "."))
        if match.group(1) == "<":
            return value / 2
        return value
    return None


def parse_probe_responder(output: str) -> str | None:
    """Retorna o IP que respondeu a um ping com TTL limitado (roteador do salto ou o destino).

    Em todos os sistemas suportados a primeira linha é o cabeçalho e a segunda é a
    resposta (quando existe). Linhas de estatística (`---`) e timeouts não têm IP do salto.
    """
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    reply = lines[1]
    if reply.startswith("---"):
        return None
    return first_ipv4(reply)


def parse_trace_output(output: str) -> list[tuple[int, str]]:
    """Extrai lista ordenada de (salto, ip) da saída de `tracert`, `traceroute` ou `tracepath`."""
    by_ttl: dict[int, str] = {}
    for line in (output or "").splitlines():
        match = TRACE_HOP_RE.match(line)
        if not match:
            continue
        ips = []
        for candidate in IPV4_RE.findall(match.group(2)):
            try:
                ips.append(str(ipaddress.IPv4Address(candidate)))
            except ValueError:
                continue
        if not ips:
            continue
        by_ttl.setdefault(int(match.group(1)), ips[-1])
    return ordered_route(by_ttl)


# Compatibilidade com o nome usado na versão 1.
extract_hops_from_tracert = parse_trace_output


def ordered_route(responders: dict[int, str], dest_ip: str | None = None) -> list[tuple[int, str]]:
    """Ordena respostas por TTL, remove IPs repetidos e corta no destino."""
    hops: list[tuple[int, str]] = []
    seen: set[str] = set()
    for ttl in sorted(responders):
        ip = responders[ttl]
        if ip in seen:
            continue
        seen.add(ip)
        hops.append((ttl, ip))
        if dest_ip is not None and ip == dest_ip:
            break
    return hops


# --------------------------------------------------------------------------------------
# Comandos por sistema operacional
# --------------------------------------------------------------------------------------


def build_ping_command(
    ip: str,
    timeout_ms: int,
    ttl: int | None = None,
    style: str | None = None,
    fractional_wait: bool = True,
) -> list[str]:
    """Monta o comando `ping` de um único pacote para o sistema atual."""
    style = style or PING_STYLE
    timeout_ms = int(timeout_ms)
    if style == "windows":
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms)]
        if ttl is not None:
            cmd += ["-i", str(int(ttl))]
    elif style == "bsd":
        cmd = ["ping", "-n", "-c", "1", "-W", str(timeout_ms)]
        if ttl is not None:
            cmd += ["-m", str(int(ttl))]
    else:
        if fractional_wait:
            wait = f"{timeout_ms / 1000:.3f}".rstrip("0").rstrip(".")
        else:
            wait = str(max(1, math.ceil(timeout_ms / 1000)))
        cmd = ["ping", "-n", "-c", "1", "-W", wait]
        if ttl is not None:
            cmd += ["-t", str(int(ttl))]
    cmd.append(ip)
    return cmd


def system_trace_command(dest_ip: str, max_hops: int, timeout_ms: int) -> list[str] | None:
    """Comando de traceroute nativo disponível (alternativa à varredura por TTL)."""
    if WINDOWS:
        if shutil.which("tracert"):
            return ["tracert", "-d", "-h", str(max_hops), "-w", str(int(timeout_ms)), dest_ip]
        return None
    wait_s = str(max(1, math.ceil(timeout_ms / 1000)))
    if shutil.which("traceroute"):
        return ["traceroute", "-n", "-m", str(max_hops), "-q", "1", "-w", wait_s, dest_ip]
    if shutil.which("tracepath"):
        return ["tracepath", "-n", "-m", str(max_hops), dest_ip]
    return None


def _decode(data: bytes) -> str:
    if WINDOWS:
        try:
            return data.decode("oem", errors="replace")
        except LookupError:
            pass
    return data.decode("utf-8", errors="replace")


def _kill(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass


async def run_command(args: list[str], timeout_s: float) -> tuple[int, str, str]:
    """Executa um comando sem shell, com limite de tempo, e retorna (código, stdout, stderr).

    No Windows não abre janelas de console; em POSIX força locale `C`.
    """
    kwargs: dict = {}
    if WINDOWS:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["env"] = _CHILD_ENV
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **kwargs,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        _kill(process)
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        return -1, "", "timeout"
    except asyncio.CancelledError:
        _kill(process)
        raise
    code = process.returncode if process.returncode is not None else -1
    return code, _decode(stdout_b), _decode(stderr_b)


# --------------------------------------------------------------------------------------
# Destino e DNS
# --------------------------------------------------------------------------------------


def validate_target(value: str) -> str:
    """Valida o destino informado (IPv4 ou nome de host). Levanta `ValueError` com mensagem amigável."""
    target = (value or "").strip()
    if not target:
        raise ValueError("Informe um IP ou nome de host de destino.")
    if target.startswith("-"):
        raise ValueError(f"Destino inválido: {target!r}.")
    try:
        address = ipaddress.ip_address(target)
    except ValueError:
        if ":" in target:
            raise ValueError("IPv6 ainda não é suportado: informe um IPv4 ou nome de host.") from None
        if re.fullmatch(r"[\d.]+", target) or not HOSTNAME_RE.match(target):
            raise ValueError(
                f"Destino inválido: {target!r}. Use um IPv4 (ex.: 8.8.8.8) ou host (ex.: google.com)."
            ) from None
        return target
    if address.version != 4:
        raise ValueError("IPv6 ainda não é suportado: informe um IPv4 ou nome de host.")
    if address.is_unspecified or address.is_multicast:
        raise ValueError(f"Destino inválido: {target!r}.")
    return str(address)


def resolve_ipv4(target: str) -> str:
    """Resolve o destino para IPv4 (bloqueante; use em thread). Levanta `OSError` se falhar."""
    try:
        return str(ipaddress.IPv4Address(target))
    except ValueError:
        pass
    infos = socket.getaddrinfo(target, None, socket.AF_INET, socket.SOCK_STREAM)
    if not infos:
        raise OSError(f"Sem endereço IPv4 para {target}")
    return str(infos[0][4][0])


def reverse_lookup(ip: str) -> str:
    """Resolve nome reverso de um IP. Retorna '-' quando indisponível."""
    try:
        return socket.gethostbyaddr(ip)[0]
    except (socket.herror, socket.gaierror, TimeoutError, OSError, UnicodeError):
        return "-"


# Compatibilidade com o nome usado na versão 1.
resolve_hostname = reverse_lookup


def local_ipv4_addresses() -> list[str]:
    """IPs IPv4 locais úteis para acesso pela rede (sem enviar pacotes)."""
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            found.append(sock.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(str(info[4][0]))
    except OSError:
        pass
    result: list[str] = []
    for ip in found:
        if ip.startswith("127.") or ip in result:
            continue
        result.append(ip)
    return result


# --------------------------------------------------------------------------------------
# Diagnóstico do ambiente
# --------------------------------------------------------------------------------------


def install_hint(tool: str = "ping") -> str:
    """Instrução de instalação do utilitário de rede para o sistema atual."""
    if WINDOWS:
        return f"O comando '{tool}' faz parte do Windows: verifique se C:\\Windows\\System32 está no PATH."
    if sys.platform == "darwin":
        return f"O comando '{tool}' acompanha o macOS: verifique se /sbin e /usr/sbin estão no PATH."
    managers = [
        ("apt-get", "sudo apt-get install -y iputils-ping traceroute"),
        ("dnf", "sudo dnf install -y iputils traceroute"),
        ("yum", "sudo yum install -y iputils traceroute"),
        ("pacman", "sudo pacman -S --needed iputils traceroute"),
        ("zypper", "sudo zypper install -y iputils traceroute"),
        ("apk", "sudo apk add iputils"),
    ]
    for manager, command in managers:
        if shutil.which(manager):
            return f"Instale com: {command}"
    return f"Instale o pacote que fornece '{tool}' (iputils) pelo gerenciador de pacotes do sistema."


def environment_report() -> dict:
    """Resumo do ambiente local usado no `--check` e em `/api/health`."""
    trace_cmd = system_trace_command("127.0.0.1", 1, 1000)
    ping_path = shutil.which("ping")
    hints: list[str] = []
    if not ping_path:
        hints.append(install_hint("ping"))
    return {
        "os": platform.system() or sys.platform,
        "os_release": platform.release(),
        "platform": platform.platform(terse=True),
        "python": platform.python_version(),
        "ping_style": PING_STYLE,
        "ping_path": ping_path,
        "trace_tool": trace_cmd[0] if trace_cmd else None,
        "hints": hints,
    }


# --------------------------------------------------------------------------------------
# Executor de sondas
# --------------------------------------------------------------------------------------


class Prober:
    """Executa pings e descobertas de rota com o `ping` do sistema (instanciar dentro do loop asyncio)."""

    def __init__(self, timeout_ms: int, max_concurrency: int = 64) -> None:
        self.timeout_ms = int(timeout_ms)
        self.style = PING_STYLE
        self.fractional_wait = True
        self._max_concurrency = max_concurrency
        self._semaphore: asyncio.Semaphore | None = None

    def _sem(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrency)
        return self._semaphore

    def _guard_s(self) -> float:
        return self.timeout_ms / 1000 + 3.0

    def _cmd(self, ip: str, ttl: int | None = None) -> list[str]:
        return build_ping_command(ip, self.timeout_ms, ttl, self.style, self.fractional_wait)

    async def prepare(self) -> None:
        """Verifica a presença do `ping` e detecta se aceita timeout fracionário (Linux)."""
        if shutil.which("ping") is None:
            raise ProbeUnavailable(f"Comando 'ping' não encontrado. {install_hint('ping')}")
        if self.style == "linux":
            code, _, _ = await run_command(["ping", "-n", "-c", "1", "-W", "0.5", "127.0.0.1"], 4)
            if code != 0:
                fallback, _, _ = await run_command(["ping", "-n", "-c", "1", "-W", "1", "127.0.0.1"], 4)
                if fallback == 0:
                    self.fractional_wait = False

    async def ping(self, ip: str) -> float | None:
        """Um ping ICMP; retorna latência em ms ou `None` (timeout/perda)."""
        async with self._sem():
            _, stdout, _ = await run_command(self._cmd(ip), self._guard_s())
        return parse_ping_rtt(stdout)

    async def probe_hop(self, dest_ip: str, ttl: int) -> str | None:
        """Ping com TTL limitado; retorna o IP que respondeu (salto ou destino)."""
        async with self._sem():
            _, stdout, _ = await run_command(self._cmd(dest_ip, ttl), self._guard_s())
        return parse_probe_responder(stdout)

    async def discover_route(self, dest_ip: str, max_hops: int) -> RouteDiscovery:
        """Descobre a rota até o destino (varredura por TTL; traceroute nativo como alternativa)."""
        responders = await self._ttl_scan(dest_ip, max_hops)
        if responders:
            hops = ordered_route(responders, dest_ip)
            return RouteDiscovery(hops=hops, source="ttl-ping", reached=any(ip == dest_ip for _, ip in hops))

        cmd = system_trace_command(dest_ip, max_hops, self.timeout_ms)
        if cmd is None:
            return RouteDiscovery(source="none")
        guard = max_hops * 3 * max(1.0, self.timeout_ms / 1000) + 15
        _, stdout, _ = await run_command(cmd, guard)
        hops = ordered_route(dict(parse_trace_output(stdout)), dest_ip)
        return RouteDiscovery(hops=hops, source=cmd[0], reached=any(ip == dest_ip for _, ip in hops))

    async def _ttl_scan(self, dest_ip: str, max_hops: int) -> dict[int, str]:
        responders: dict[int, str] = {}
        for attempt in range(2):
            pending = [ttl for ttl in range(1, max_hops + 1) if ttl not in responders]
            if attempt > 0:
                dest_ttls = [ttl for ttl, ip in responders.items() if ip == dest_ip]
                if dest_ttls:
                    pending = [ttl for ttl in pending if ttl < min(dest_ttls)]
            if not pending:
                break
            results = await asyncio.gather(*(self.probe_hop(dest_ip, ttl) for ttl in pending))
            for ttl, ip in zip(pending, results):
                if ip:
                    responders[ttl] = ip
        return responders
