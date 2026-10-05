#!/bin/sh
# Inicia o Monitor de Estabilidade de Rede (Linux/macOS).
# Uso: ./iniciar.sh [opcoes]   ex.: ./iniciar.sh --lan   |   ./iniciar.sh 1.1.1.1   |   ./iniciar.sh --check
cd "$(dirname "$0")" || exit 1
PATH="$PATH:/sbin:/usr/sbin:/bin:/usr/bin"
export PATH

install_cmd() {
  # $1 = pacotes para apt; $2 = pacotes para dnf/yum/zypper/pacman
  if command -v apt-get >/dev/null 2>&1; then echo "sudo apt-get install -y $1"
  elif command -v dnf >/dev/null 2>&1; then echo "sudo dnf install -y $2"
  elif command -v yum >/dev/null 2>&1; then echo "sudo yum install -y $2"
  elif command -v zypper >/dev/null 2>&1; then echo "sudo zypper install -y $2"
  elif command -v pacman >/dev/null 2>&1; then echo "sudo pacman -S --needed $2"
  fi
}

offer_install() {
  # $1 = descrição; $2 = comando sugerido
  echo "$1"
  [ -z "$2" ] && return 1
  if [ -t 0 ]; then
    printf "Instalar agora com '%s'? [S/n] " "$2"
    read -r answer
    case "$answer" in
      n|N) ;;
      *) sh -c "$2" && return 0 ;;
    esac
  fi
  echo "Instale manualmente com: $2"
  return 1
}

find_python() {
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
      "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

PY="$(find_python)"
if [ -z "$PY" ]; then
  if [ "$(uname -s)" = "Darwin" ]; then
    echo "Python 3.9+ não encontrado. Instale com: xcode-select --install  (ou: brew install python)"
    exit 1
  fi
  offer_install "Python 3.9+ não encontrado." "$(install_cmd python3 python3)" || exit 1
  PY="$(find_python)" || exit 1
fi

if ! command -v ping >/dev/null 2>&1; then
  offer_install "Comando 'ping' não encontrado." "$(install_cmd 'iputils-ping traceroute' 'iputils traceroute')" || exit 1
fi

exec "$PY" portal_rede.py "$@"
