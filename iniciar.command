#!/bin/sh
# macOS: duplo clique no Finder abre o Terminal e inicia o monitor.
cd "$(dirname "$0")" || exit 1
exec sh ./iniciar.sh "$@"
