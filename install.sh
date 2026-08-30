#!/usr/bin/env bash
# Spotube DJ installer - Linux / macOS
# Creates a venv, installs yt-dlp, and drops a `spotube-dj` command in ~/.local/bin
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"

echo "==> checking python"
"$PY" - <<'EOF'
import sys
if sys.version_info < (3, 10):
    sys.exit(f"python 3.10+ required, found {sys.version.split()[0]}")
print(f"    ok: {sys.version.split()[0]}")
EOF

echo "==> creating venv at $HERE/.venv"
[ -d "$HERE/.venv" ] || "$PY" -m venv "$HERE/.venv"
# shellcheck disable=SC1091
source "$HERE/.venv/bin/activate"

echo "==> installing dependencies"
pip install -q --upgrade pip
pip install -q -U yt-dlp
pip install -q google-genai 2>/dev/null || echo "    (google-genai skipped; not needed - brain.py speaks raw HTTPS)"

echo "==> the player (a browser tab, not a Tk window)"
if command -v xdg-open >/dev/null 2>&1 || command -v open >/dev/null 2>&1; then
  echo "    ok: 'spotube-dj' will open the player in your browser"
else
  echo "    no xdg-open/'open' found: the player still starts, open the URL it"
  echo "    prints (http://127.0.0.1:8766) yourself. Nothing to install for that."
fi
if command -v mpv >/dev/null 2>&1; then
  echo "    ok: mpv present -> audio plays here"
else
  echo "    mpv not found: install it for audio (apt install mpv / brew install mpv),"
  echo "    or hand playlists to Spotube with --to-spotube"
fi

echo "==> optional native tools"
have() { command -v "$1" >/dev/null 2>&1; }
for t in mpv ffmpeg playerctl; do
  if have "$t"; then echo "    ok: $t"; else
    case "$t" in
      mpv)   echo "    MISSING mpv  -> needed for local playback. Install:"
             echo "                     Debian/Ubuntu: sudo apt install mpv"
             echo "                     Arch:          sudo pacman -S mpv"
             echo "                     Fedora:        sudo dnf install mpv"
             echo "                     macOS:         brew install mpv" ;;
      ffmpeg) echo "    MISSING ffmpeg -> recommended.  sudo apt install ffmpeg" ;;
      playerctl) echo "    MISSING playerctl -> optional, only if you want 'next' to"
             echo "                         drive Spotube's own MPRIS player instead of mpv" ;;
    esac
  fi
done

echo "==> installing launcher"
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/spotube-dj" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$HERE\${PYTHONPATH:+:\$PYTHONPATH}"
exec "$HERE/.venv/bin/python" -m spotube_dj "\$@"
EOF
chmod +x "$HOME/.local/bin/spotube-dj"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *)
  echo "    note: add to your PATH ->  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

echo "==> sanity check"
PYTHONPATH="$HERE" "$HERE/.venv/bin/python" -m spotube_dj --doctor || true

echo
echo "==> desktop menu entry"
if [ -t 0 ]; then
  printf "Add \"Spotify DJ (free)\" to the desktop app menu? [Y/n] "
  read -r REPLY || REPLY=y
else
  REPLY=y          # non-interactive install: take the default, it writes nothing
                   # outside ~/.local and uninstall.sh removes it
fi
case "${REPLY:-y}" in
  [Nn]*) echo "    skipped - run  spotube-dj --install-desktop  later if you want it" ;;
  *) PYTHONPATH="$HERE" "$HERE/.venv/bin/python" -m spotube_dj --install-desktop \
       || echo "    skipped (the launcher is optional; the CLI and GUI work without it)" ;;
esac

echo
echo "done.  try:"
echo "  spotube-dj \"dark synthwave for night driving\" --list"
echo "  spotube-dj \"90s trip hop\" --daemon          # auto-DJ, then: spotube-dj next / like"
echo "  spotube-dj \"lofi\" --export --to-spotube      # hand an m3u8 to Spotube"
echo "  spotube-dj                                        # the player, in a browser tab"
echo "  spotube-dj --search \'tame impala\'                # straight to a search"
echo "  ./uninstall.sh                                 # take it all back out"
echo
echo "optional - better query planning (skip it and the offline parser still works):"
echo "  spotube-dj --set-key <your Gemini key> ; spotube-dj --test-brain"
echo "  no --set-model needed: it starts at gemini-3.5-flash and follows the API"
echo "  if that id is ever retired (2.0-flash died 2026-06-01, 2.5-flash is next"
echo "  on 2026-10-16). --set-model pins it anyway if you prefer."
echo "  (or --set-base http://localhost:11434 --set-model llama3.2 for a local model)"
echo "  --doctor prints the same brain verdict, --clear-key forgets it"
