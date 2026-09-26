#!/usr/bin/env sh
# ZergSwarm installer for macOS and Linux:
#   curl -fsSL https://raw.githubusercontent.com/Lunarwerx/ZergSwarm/main/install.sh | sh
# It installs the latest release with pipx (which puts the `zswarm` command on your PATH), then tells you the two
# commands that connect it to Claude Code and open its console. It changes nothing else.
set -eu
REPO="Lunarwerx/ZergSwarm"

PY=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        PY="$cand"; break
    fi
done
if [ -z "$PY" ]; then
    echo "ZergSwarm needs Python 3.11 or newer (https://www.python.org/downloads/, or your package manager). Install it, then run this again." >&2
    exit 1
fi

# The newest release's wheel; with no release yet, the main branch.
SOURCE="https://github.com/$REPO/archive/refs/heads/main.zip"
WHEEL="$(curl -fsSL -H 'User-Agent: zergswarm-installer' "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
    | "$PY" -c 'import json,sys
try:
    d = json.load(sys.stdin)
    print(next((a["browser_download_url"] for a in d.get("assets", []) if a["name"].endswith(".whl")), ""))
except Exception:
    print("")' || true)"
if [ -n "$WHEEL" ]; then SOURCE="$WHEEL"; echo "Installing ZergSwarm from $WHEEL"; else echo "No release found; installing from the main branch"; fi

if ! "$PY" -m pipx --version >/dev/null 2>&1; then
    if command -v pipx >/dev/null 2>&1; then
        PIPX="pipx"
    elif "$PY" -m pip --version >/dev/null 2>&1; then
        echo "Installing pipx (it keeps ZergSwarm in its own environment)"
        "$PY" -m pip install --user --upgrade pipx || "$PY" -m pip install --user --upgrade --break-system-packages pipx
        PIPX="$PY -m pipx"
    else
        echo "ZergSwarm installs with pipx, and this Python has no pip to fetch it. Install pipx with your package manager" >&2
        echo "(sudo apt install pipx, brew install pipx, or sudo dnf install pipx), then run this again." >&2
        exit 1
    fi
else
    PIPX="$PY -m pipx"
fi
$PIPX ensurepath >/dev/null 2>&1 || true
# --python: the environment is built on the 3.11+ interpreter found above, not whatever pipx itself runs on.
$PIPX install --force --python "$(command -v "$PY")" "$SOURCE"

echo
echo "ZergSwarm is installed. Open a NEW terminal (so it sees the zswarm command), then:"
echo "  zswarm install     connect it to Claude Code (add --client all for Claude Desktop and Codex too)"
echo "  zswarm ui          open the console in your browser and add an API key"
