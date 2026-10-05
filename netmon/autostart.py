"""Início automático com o sistema (opcional), sem privilégios de administrador.

- Windows: atalho `.cmd` na pasta "Inicializar" do usuário (usa `pythonw`, sem janela).
- macOS: LaunchAgent em `~/Library/LaunchAgents`.
- Linux: entrada de autostart XDG em `~/.config/autostart` (sessões gráficas).
"""

from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

APP_ID = "netmon-monitor-rede"
APP_NAME = "Monitor de Estabilidade de Rede"


def current_platform() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def background_python(platform_name: str | None = None) -> str:
    """Interpretador para rodar em segundo plano (pythonw no Windows evita a janela de console)."""
    executable = Path(sys.executable)
    if (platform_name or current_platform()) == "windows":
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.exists():
            return str(pythonw)
    return str(executable)


def entry_path(platform_name: str | None = None, home: Path | None = None, env: dict | None = None) -> Path:
    platform_name = platform_name or current_platform()
    home = home or Path.home()
    env = os.environ if env is None else env
    if platform_name == "windows":
        appdata = Path(env.get("APPDATA") or home / "AppData" / "Roaming")
        return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / f"{APP_ID}.cmd"
    if platform_name == "macos":
        return home / "Library" / "LaunchAgents" / f"com.{APP_ID}.plist"
    config_home = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    return config_home / "autostart" / f"{APP_ID}.desktop"


def _desktop_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return f'"{escaped}"'


def build_entry(platform_name: str, python: str, script: Path, args: list[str]) -> bytes:
    """Conteúdo do arquivo de início automático para a plataforma."""
    workdir = script.parent
    if platform_name == "windows":
        command = " ".join(f'"{part}"' for part in [python, str(script), *args])
        text = f'@echo off\r\ncd /d "{workdir}"\r\nstart "" {command}\r\n'
        try:
            return text.encode("oem")
        except (LookupError, UnicodeEncodeError):
            return ("@chcp 65001 >nul\r\n" + text).encode("utf-8")
    if platform_name == "macos":
        log_path = str(workdir / "data" / "monitor.log")
        return plistlib.dumps(
            {
                "Label": f"com.{APP_ID}",
                "ProgramArguments": [python, str(script), *args],
                "WorkingDirectory": str(workdir),
                "RunAtLoad": True,
                "StandardOutPath": log_path,
                "StandardErrorPath": log_path,
            }
        )
    exec_line = " ".join(_desktop_quote(part) for part in [python, str(script), *args])
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=Monitora a estabilidade da conexão e registra evidências\n"
        f"Exec={exec_line}\n"
        f"Path={workdir}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    ).encode()


def install(script: Path, args: list[str] | None = None, home: Path | None = None) -> Path:
    """Cria (ou atualiza) a entrada de início automático e retorna o caminho do arquivo."""
    platform_name = current_platform()
    path = entry_path(platform_name, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_entry(platform_name, background_python(platform_name), script.resolve(), args or []))
    return path


def uninstall(home: Path | None = None) -> Path | None:
    """Remove a entrada de início automático. Retorna o caminho removido (ou `None`)."""
    path = entry_path(current_platform(), home)
    if path.exists():
        path.unlink()
        return path
    return None
