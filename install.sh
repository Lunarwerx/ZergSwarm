#!/usr/bin/env sh
# ZergSwarm installer for macOS and Linux:
#   curl -fsSL https://raw.githubusercontent.com/Lunarwerx/ZergSwarm/main/install.sh | sh
# It installs the newest release's wheel (or the main branch, with no release). It prefers uv; with no
# uv and a Python 3.11+ present it uses pipx; with neither it installs uv, which brings its own Python.
# Then it runs `zswarm setup`, which connects ZergSwarm to Claude Code, Claude Desktop, and Codex, and
# opens its console. Set ZERGSWARM_SOURCE to a wheel path or URL to install that instead, and
# ZSWARM_NO_SETUP (or the older ZERGSWARM_NO_SETUP) to skip `zswarm setup`. It changes nothing else.
set -eu
REPO="Lunarwerx/ZergSwarm"

PY=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        PY="$cand"; break
    fi
done

install_with_pipx() {
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
    # --python: the environment is built on the 3.11+ interpreter found above, not whatever pipx runs on.
    $PIPX install --force --python "$(command -v "$PY")" "$SOURCE"
    BIN="$($PIPX environment --value PIPX_BIN_DIR)"
}

install_with_uv() {
    uv tool install --force --python '>=3.11' "$SOURCE"
    uv tool update-shell >/dev/null 2>&1 || true
    BIN="$(uv tool dir --bin)"
}

if [ -n "${ZERGSWARM_SOURCE:-}" ]; then
    SOURCE="$ZERGSWARM_SOURCE"
    echo "Using ZERGSWARM_SOURCE: $SOURCE"
else
    API="https://api.github.com/repos/$REPO/releases/latest"
    if [ -n "$PY" ]; then
        WHEEL="$(curl -fsSL -H 'User-Agent: zergswarm-installer' "$API" 2>/dev/null \
            | "$PY" -c 'import json,sys
try:
    d = json.load(sys.stdin)
    print(next((a["browser_download_url"] for a in d.get("assets", []) if a["name"].endswith(".whl")), ""))
except Exception:
    print("")' || true)"
    else
        WHEEL="$(curl -fsSL -H 'User-Agent: zergswarm-installer' "$API" 2>/dev/null \
            | grep '"browser_download_url"' | grep '\.whl' \
            | sed -n 's/.*"browser_download_url"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
            | head -n 1 || true)"
    fi
    if [ -n "$WHEEL" ]; then SOURCE="$WHEEL"; else SOURCE="https://github.com/$REPO/archive/refs/heads/main.zip"; fi
    echo "Installing ZergSwarm from $SOURCE"
fi

if command -v uv >/dev/null 2>&1; then
    install_with_uv
elif [ -n "$PY" ]; then
    install_with_pipx
else
    echo "No Python 3.11 or newer here, so ZergSwarm installs with uv, which brings its own Python (https://docs.astral.sh/uv/)."
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        echo "This machine has no curl or wget to fetch uv. Install Python from https://www.python.org/downloads/ or your package manager" >&2
        echo "(sudo apt install python3, brew install python), then run this again." >&2
        exit 1
    fi
    PATH="$HOME/.local/bin:$PATH"
    export PATH
    if ! command -v uv >/dev/null 2>&1; then
        echo "uv could not be installed. Install Python from https://www.python.org/downloads/ or your package manager" >&2
        echo "(sudo apt install python3, brew install python), then run this again." >&2
        exit 1
    fi
    install_with_uv
fi
PATH="$BIN:$PATH"
export PATH

echo
echo "ZergSwarm is installed. Open a new terminal to use the zswarm command yourself."
if [ -n "${ZSWARM_NO_SETUP:-}${ZERGSWARM_NO_SETUP:-}" ]; then  # ZSWARM_ like every other setting; the old name still works
    echo "Run zswarm setup to connect your assistants and open the console."
# setup says what is left (a key, then a first ask), so nothing is repeated after it.
elif ! zswarm setup </dev/null; then
    echo "Setup did not finish. Run it again any time: zswarm setup"
fi