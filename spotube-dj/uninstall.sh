#!/usr/bin/env bash
# Remove what install.sh put on this machine. Everything it touches is under
# $HOME - it never installed system files, so there is no sudo here either.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

echo "==> removing the desktop launcher and icon"
if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"; else PY="$(command -v python3)"; fi
PYTHONPATH="$HERE" "$PY" -m spotube_dj --uninstall-desktop || echo "    (nothing to remove)"

echo "==> removing the console shim"
rm -f "$HOME/.local/bin/spotube-dj"

if [ -d ".venv" ]; then
  echo "==> removing the virtualenv (.venv, $(du -sh .venv 2>/dev/null | cut -f1))"
  rm -rf .venv
fi

echo
echo "Your data was NOT touched: ~/.spotube-dj/ holds the history, taste profile,"
echo "brain config (that file has your API key, mode 600) and spotube_dj.m3u8."
echo "To delete it too:  rm -rf ~/.spotube-dj"
