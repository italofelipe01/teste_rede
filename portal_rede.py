"""Portal web + API do monitor de estabilidade de rede (camada de aplicação).

Uso mais simples (padrões ou `config.json`):
    python portal_rede.py
Exemplos:
    python portal_rede.py 1.1.1.1 --intervalo 0.5
    python portal_rede.py --lan          # acessível por outros dispositivos da rede
    python portal_rede.py --check        # diagnóstico do ambiente
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 9):  # noqa: UP036 - mensagem amigável para instalações antigas
    sys.exit("Python 3.9 ou superior é necessário. Baixe em https://www.python.org/downloads/")

import argparse
import asyncio
import gzip
import http.client
import ipaddress
import json
import logging
import os
import signal
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from netmon import __version__, autostart
from netmon.config import (
    DEFAULT_CONFIG_PATH,
    EDITABLE_FIELDS,
    LABELS,
    LIMITS,
    Settings,
    load_settings,
    save_user_config,
)
from netmon.engine import MonitorEngine
from netmon.probes import Prober, ProbeUnavailable, environment_report, local_ipv4_addresses, resolve_ipv4
from netmon.storage import build_zip, resolve_archive

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
STATIC_FILES = {
    "/": "dashboard.html",
    "/index.html": "dashboard.html",
    "/dashboard.html": "dashboard.html",
    "/dashboard.css": "dashboard.css",
    "/dashboard.js": "dashboard.js",
    "/favicon.svg": "favicon.svg",
}
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".csv": "text/csv; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}
MAX_BODY_BYTES = 16 * 1024
SSE_KEEPALIVE_S = 15.0

log = logging.getLogger("netmon.portal")


class PortalServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    # No Windows, SO_REUSEADDR permitiria "roubar" uma porta em uso por outro programa.
    allow_reuse_address = os.name != "nt"


class PortalHandler(BaseHTTPRequestHandler):
    """Rotas da API REST/SSE e arquivos estáticos (lista fechada, sem listagem de diretórios)."""

    server_version = f"NetMon/{__version__}"
    engine: MonitorEngine
    config_path: Path | None = None
    access_urls: list[str] = []
    stopping: threading.Event
    verbose = False

    # ------------------------------------------------------------------ infraestrutura

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - assinatura da biblioteca padrão
        if self.verbose:
            super().log_message(format, *args)

    def _send_bytes(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        if len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or "") and content_type.startswith(
            ("application/json", "text/")
        ):
            body = gzip.compress(body, compresslevel=5)
            extra = dict(extra or {}, **{"Content-Encoding": "gzip"})
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        self._send_bytes(status, body, CONTENT_TYPES[".json"])

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json({"ok": False, "error": message}, status)

    def _send_file(self, path: Path, download_name: str | None = None) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send_error_json(HTTPStatus.NOT_FOUND, "Arquivo não encontrado.")
            return
        extra = {"Content-Disposition": f'attachment; filename="{download_name}"'} if download_name else None
        self._send_bytes(HTTPStatus.OK, body, CONTENT_TYPES.get(path.suffix, "application/octet-stream"), extra)

    def _client_is_local(self) -> bool:
        try:
            address = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        mapped = getattr(address, "ipv4_mapped", None)
        return address.is_loopback or bool(mapped and mapped.is_loopback)

    def _control_allowed(self) -> bool:
        return self.engine.settings.allow_remote_control or self._client_is_local()

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urlparse(origin).netloc == (self.headers.get("Host") or "")

    def _host_is_literal(self) -> bool:
        """Host da requisição é IP ou localhost (bloqueia DNS rebinding em rotas de controle)."""
        hostname = urlparse(f"//{self.headers.get('Host') or ''}").hostname or ""
        if hostname == "localhost" or hostname.endswith(".localhost"):
            return True
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            return False
        return True

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("Tamanho de corpo inválido.")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("O corpo deve ser um objeto JSON.")
        return data

    # ------------------------------------------------------------------ roteamento

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        routes = {
            "/api/snapshot": self._api_snapshot,
            "/api/history": self._api_history,
            "/api/stream": self._api_stream,
            "/api/health": self._api_health,
            "/api/config": self._api_config,
            "/api/export": self._api_export,
            "/api/sessions": self._api_sessions,
        }
        if path in routes:
            routes[path](query)
            return
        if path.startswith("/api/sessions/") and path.endswith("/export"):
            self._api_session_export(path[len("/api/sessions/") : -len("/export")])
            return
        if path in STATIC_FILES:
            self._send_file(WEB_DIR / STATIC_FILES[path])
            return
        if path.startswith("/data/"):
            file_path = self.engine.current_files().get(path[len("/data/") :])
            if file_path is not None and file_path.is_file():
                self._send_file(file_path)
                return
        self._send_error_json(HTTPStatus.NOT_FOUND, "Recurso não encontrado.")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        routes = {
            "/api/config": self._api_update_config,
            "/api/actions/rediscover": self._api_rediscover,
            "/api/actions/reset": self._api_reset,
        }
        if path not in routes:
            self._send_error_json(HTTPStatus.NOT_FOUND, "Recurso não encontrado.")
            return
        if not self._control_allowed():
            self._send_error_json(
                HTTPStatus.FORBIDDEN,
                "Alterações só são permitidas no computador que executa o monitor. "
                "Para liberar na rede, defina \"allow_remote_control\": true no config.json.",
            )
            return
        if not self._same_origin() or not self._host_is_literal():
            self._send_error_json(
                HTTPStatus.FORBIDDEN,
                "Origem da requisição não permitida. Abra o painel pelo endereço IP (ex.: http://127.0.0.1:8000).",
            )
            return
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            self._send_error_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Envie o corpo como application/json.")
            return
        try:
            body = self._read_json()
        except (ValueError, UnicodeDecodeError) as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, f"JSON inválido: {exc}")
            return
        routes[path](body)

    # ------------------------------------------------------------------ API de leitura

    def _api_snapshot(self, _query) -> None:
        self._send_json(self.engine.snapshot())

    def _api_history(self, query) -> None:
        since, instance = _parse_cursor((query.get("since") or ["0"])[0])
        instance = (query.get("instance") or [instance or ""])[0] or None
        self._send_json(self.engine.history_since(since, instance))

    def _api_health(self, _query) -> None:
        engine = self.engine
        snapshot = engine.snapshot()
        self._send_json(
            {
                "app": "netmon",
                "version": __version__,
                "instance": engine.instance_id,
                "phase": snapshot["phase"],
                "started_at": engine.started_at.isoformat(sep=" ", timespec="seconds"),
                "environment": engine.environment,
                "access_urls": self.access_urls,
                "control_allowed": self._control_allowed(),
                "data_dir": str(engine.settings.csv_path.parent),
            }
        )

    def _api_config(self, _query) -> None:
        settings = self.engine.settings
        self._send_json(
            {
                "settings": settings.public_dict(),
                "editable": list(EDITABLE_FIELDS),
                "limits": {key: LIMITS[key] for key in EDITABLE_FIELDS if key in LIMITS},
                "labels": {key: LABELS[key] for key in EDITABLE_FIELDS},
                "control_allowed": self._control_allowed(),
                "config_path": str(self.config_path) if self.config_path else None,
            }
        )

    def _api_export(self, _query) -> None:
        try:
            name, data = self.engine.export_zip()
        except RuntimeError as exc:
            self._send_error_json(HTTPStatus.CONFLICT, str(exc))
            return
        self._send_bytes(
            HTTPStatus.OK, data, "application/zip", {"Content-Disposition": f'attachment; filename="{name}"'}
        )

    def _api_sessions(self, _query) -> None:
        self._send_json({"sessions": self.engine.list_sessions()})

    def _api_session_export(self, name: str) -> None:
        folder = resolve_archive(self.engine.settings.archive_dir, name)
        if folder is None:
            self._send_error_json(HTTPStatus.NOT_FOUND, "Sessão arquivada não encontrada.")
            return
        data = build_zip(sorted(p for p in folder.iterdir() if p.is_file()))
        self._send_bytes(
            HTTPStatus.OK,
            data,
            "application/zip",
            {"Content-Disposition": f'attachment; filename="evidencias_sessao_{folder.name}.zip"'},
        )

    def _api_stream(self, query) -> None:
        """Server-Sent Events: envia snapshot + pontos novos do histórico a cada mudança de estado."""
        engine = self.engine
        cursor = self.headers.get("Last-Event-ID") or (query.get("since") or ["0"])[0]
        since, instance = _parse_cursor(cursor)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        version = -1
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            while not self.stopping.is_set() and not engine.stopping:
                current = engine.wait_for_change(version, timeout=SSE_KEEPALIVE_S)
                if self.stopping.is_set():
                    break
                if current == version:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                version = current
                history = engine.history_since(since, instance)
                message = dict(history, snapshot=engine.snapshot())
                since, instance = history["latest_seq"], history["instance"]
                data = json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str)
                self.wfile.write(f"id: {instance}:{since}\ndata: {data}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError):
            return

    # ------------------------------------------------------------------ API de controle

    def _api_update_config(self, body: dict) -> None:
        try:
            settings, restarted = self.engine.update_settings(body)
        except ValueError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        persisted = False
        if self.config_path is not None:
            try:
                save_user_config(self.config_path, settings)
                persisted = True
            except OSError as exc:
                log.warning("Não foi possível salvar %s: %s", self.config_path, exc)
        self._send_json(
            {"ok": True, "settings": settings.public_dict(), "restarted": restarted, "persisted": persisted}
        )

    def _api_rediscover(self, _body: dict) -> None:
        self.engine.request_rediscover()
        self._send_json({"ok": True})

    def _api_reset(self, _body: dict) -> None:
        self.engine.reset_session()
        self._send_json({"ok": True})


def _parse_cursor(value: str) -> tuple[int, str | None]:
    """Cursor de histórico no formato `<instancia>:<seq>` (ou apenas `<seq>`)."""
    instance, _, seq = (value or "").rpartition(":")
    try:
        return max(0, int(seq or 0)), instance or None
    except ValueError:
        return 0, None


def make_handler(
    engine: MonitorEngine,
    config_path: Path | None,
    access_urls: list[str],
    stopping: threading.Event,
    verbose: bool = False,
) -> type[PortalHandler]:
    return type(
        "BoundPortalHandler",
        (PortalHandler,),
        {
            "engine": engine,
            "config_path": config_path,
            "access_urls": access_urls,
            "stopping": stopping,
            "verbose": verbose,
        },
    )


def bind_server(host: str, port: int, handler, attempts: int = 20) -> PortalServer:
    """Abre o servidor na porta pedida ou na próxima livre."""
    last_error: OSError | None = None
    candidates = [port] if port == 0 else range(port, min(65536, port + attempts))
    for candidate in candidates:
        try:
            return PortalServer((host, candidate), handler)
        except OSError as exc:
            last_error = exc
    raise last_error or OSError("Nenhuma porta disponível")


def find_running_instance(port: int) -> dict | None:
    """Detecta um portal já em execução nesta máquina (evita dois monitores gravando os mesmos arquivos)."""
    if port == 0:
        return None
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1.5)
    try:
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        if response.status != 200:
            return None
        data = json.loads(response.read().decode("utf-8"))
        return data if data.get("app") == "netmon" else None
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def access_urls_for(host: str, port: int) -> list[str]:
    if host in {"0.0.0.0", "", "::"}:
        return [f"http://127.0.0.1:{port}"] + [f"http://{ip}:{port}" for ip in local_ipv4_addresses()]
    return [f"http://{host}:{port}"]


def can_open_browser() -> bool:
    if os.name == "nt" or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def run_check(settings: Settings) -> int:
    """Diagnóstico do ambiente: ferramentas, ping local, DNS, ping ao destino e rota."""
    env = environment_report()
    print(f"Sistema:     {env['platform']} | Python {env['python']}")
    print(f"ping:        {env['ping_path'] or 'NÃO ENCONTRADO'} (sintaxe {env['ping_style']})")
    print(f"traceroute:  {env['trace_tool'] or 'não encontrado (opcional)'}")
    for hint in env["hints"]:
        print(f"  -> {hint}")

    async def probe() -> int:
        prober = Prober(timeout_ms=settings.timeout_ms)
        try:
            await prober.prepare()
        except ProbeUnavailable as exc:
            print(f"ERRO: {exc}")
            return 3
        local = await prober.ping("127.0.0.1")
        print(f"Ping local:  {f'OK ({local:.2f} ms)' if local is not None else 'FALHOU (permissão para ICMP?)'}")
        try:
            ip = await asyncio.get_running_loop().run_in_executor(None, resolve_ipv4, settings.target)
        except OSError as exc:
            print(f"Destino:     {settings.target} -> falha ao resolver ({exc})")
            return 1
        rtt = await prober.ping(ip)
        print(f"Destino:     {settings.target} ({ip}) -> {f'{rtt:.2f} ms' if rtt is not None else 'sem resposta'}")
        route = await prober.discover_route(ip, settings.max_hops)
        hops = " > ".join(f"H{ttl} {hop_ip}" for ttl, hop_ip in route.hops) or "nenhum salto respondeu"
        print(f"Rota ({route.source}): {hops}")
        return 0 if local is not None else 2

    code = asyncio.run(probe())
    print("Ambiente pronto." if code == 0 else "Há pendências no ambiente (veja acima).")
    return code


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Portal web + monitoramento de conectividade em tempo real (Windows, Linux e macOS).",
        epilog="Sem argumentos, usa config.json (se existir) ou os padrões: destino 8.8.8.8, porta 8000.",
    )
    parser.add_argument("destino", nargs="?", help="IP ou host destino (padrão: config.json ou 8.8.8.8)")
    parser.add_argument("--host", help="Interface do portal (padrão: 127.0.0.1)")
    parser.add_argument("--lan", action="store_true", help="Disponibiliza o portal para a rede local (host 0.0.0.0)")
    parser.add_argument("--port", type=int, help="Porta do portal (padrão: 8000; usa a próxima livre se ocupada)")
    parser.add_argument("--intervalo", type=float, help="Intervalo entre ciclos em segundos (padrão: 1.0)")
    parser.add_argument("--timeout-ms", type=int, help="Timeout do ping por salto em ms (padrão: 1000)")
    parser.add_argument("--csv", type=Path, help="CSV principal de saída (padrão: data/monitoramento_rota.csv)")
    parser.add_argument("--max-hops", type=int, help="Máximo de saltos da rota (padrão: 30)")
    parser.add_argument("--min-ciclos-queda", type=int, help="Ciclos sem resposta para confirmar queda (padrão: 3)")
    parser.add_argument("--redescobrir-min", type=float, help="Minutos entre redescobertas da rota (0 = desliga)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Arquivo de configuração JSON")
    parser.add_argument("--no-browser", action="store_true", help="Não abre o navegador automaticamente")
    parser.add_argument("--check", action="store_true", help="Verifica o ambiente e sai")
    parser.add_argument("--verbose", action="store_true", help="Mostra o log de requisições HTTP")
    parser.add_argument(
        "--instalar-autostart",
        action="store_true",
        help="Inicia o monitor automaticamente ao entrar no sistema (sem abrir o navegador)",
    )
    parser.add_argument("--remover-autostart", action="store_true", help="Remove o início automático")
    return parser.parse_args(argv)


def overrides_from_args(args: argparse.Namespace) -> dict:
    return {
        "target": args.destino,
        "host": "0.0.0.0" if args.lan else args.host,
        "port": args.port,
        "interval_sec": args.intervalo,
        "timeout_ms": args.timeout_ms,
        "csv_path": args.csv,
        "max_hops": args.max_hops,
        "outage_min_cycles": args.min_ciclos_queda,
        "route_refresh_min": args.redescobrir_min,
        "open_browser": False if args.no_browser else None,
    }


def configure_logging(settings: Settings) -> None:
    """Log no console; sem console (pythonw/início automático), grava em data/monitor.log."""
    if sys.stdout is None or sys.stderr is None:
        log_path = settings.csv_path.parent / "monitor.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(message)s",
            handlers=[logging.FileHandler(log_path, encoding="utf-8")],
        )
    else:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")


def manage_autostart(install_entry: bool) -> int:
    if install_entry:
        path = autostart.install(ROOT / "portal_rede.py", ["--no-browser"])
        print(f"Início automático configurado: {path}")
        print("O monitor iniciará sozinho no próximo login (painel em http://127.0.0.1:8000).")
        if autostart.current_platform() == "linux":
            print("Servidor sem interface gráfica? Use um serviço systemd (veja o README).")
    else:
        path = autostart.uninstall()
        print(f"Início automático removido: {path}" if path else "Nenhum início automático estava configurado.")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")  # console legado do Windows não quebra com acentos
    args = parse_args(argv)
    if args.instalar_autostart or args.remover_autostart:
        return manage_autostart(args.instalar_autostart)
    try:
        settings, warnings = load_settings(args.config, overrides_from_args(args))
    except ValueError as exc:
        print(f"Erro: {exc}")
        return 2
    configure_logging(settings)
    for warning in warnings:
        print(f"Aviso: {warning}")
        log.warning(warning)

    if args.check:
        return run_check(settings)

    running = find_running_instance(settings.port)
    if running:
        url = f"http://127.0.0.1:{settings.port}"
        print(f"O monitor já está em execução em {url} (instância {running.get('instance')}).")
        if settings.open_browser and can_open_browser():
            webbrowser.open(url)
        return 0

    stopping = threading.Event()
    engine = MonitorEngine(settings)
    access_urls: list[str] = []
    handler = make_handler(engine, args.config, access_urls, stopping, verbose=args.verbose)
    try:
        server = bind_server(settings.host, settings.port, handler)
    except OSError as exc:
        print(f"Não foi possível abrir o portal em {settings.host}:{settings.port}: {exc}")
        return 4
    port = server.server_address[1]
    access_urls.extend(access_urls_for(settings.host, port))
    engine.start()

    def handle_term(_signum, _frame):
        raise KeyboardInterrupt

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_term)

    print("=" * 64)
    print(f" Monitor de Estabilidade de Rede v{__version__}")
    print(f" Destino: {settings.target} | intervalo {settings.interval_sec:g} s | timeout {settings.timeout_ms} ms")
    for idx, url in enumerate(access_urls):
        print(f" {'Painel' if idx == 0 else 'Na rede'}: {url}")
    if port != settings.port:
        print(f" (porta {settings.port} ocupada; usando {port})")
    print(f" Evidências: {settings.csv_path.parent}")
    print(" Ctrl+C para encerrar.")
    print("=" * 64)

    if settings.open_browser and can_open_browser():
        threading.Timer(1.0, webbrowser.open, args=(access_urls[0],)).start()

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nEncerrando... finalizando evidências.")
    finally:
        stopping.set()
        engine.stop()
        server.server_close()
    print(f"Evidências salvas em: {settings.csv_path.parent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
