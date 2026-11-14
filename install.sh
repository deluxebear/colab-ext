#!/bin/sh
# Bootstrap installer for colab-ext (formerly colab-dfs-ext).
#
# One-line install (any POSIX shell, no git required):
#
#     curl -fsSL https://raw.githubusercontent.com/deluxebear/colab-ext/main/install.sh | sh
#
# or with wget:
#
#     wget -qO- https://raw.githubusercontent.com/deluxebear/colab-ext/main/install.sh | sh
#
# Env overrides:
#     COLAB_EXT_REPO=owner/repo     source repository     (default: deluxebear/colab-ext, fallback deluxebear/colab-dfs-ext)
#     COLAB_EXT_REF=main            branch or tag         (default: main)
#     (Legacy COLAB_DFS_REPO / COLAB_DFS_REF are also honored.)
#
# What it does: fetch the repo tarball, run `python3 install.py` from it. That copies the
# extension to ~/.colab-ext and drops a single .pth file (colab_ext.pth) into the colab-cli
# environment's site-packages. Nothing else on your machine is touched.
set -eu

EXPLICIT_REPO="${COLAB_EXT_REPO:-${COLAB_DFS_REPO:-}}"
REF="${COLAB_EXT_REF:-${COLAB_DFS_REF:-main}}"

say() { printf '[colab-ext] %s\n' "$*"; }
die() { printf '[colab-ext] ERROR: %s\n' "$*" >&2; exit 1; }

# --- pick a downloader ----------------------------------------------------------------------
fetch() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$1"
    else
        die "need curl or wget to download the source"
    fi
}

# --- pick a python --------------------------------------------------------------------------
PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done
[ -n "$PY" ] || die "python3 not found on PATH (https://www.python.org/)"

# --- sanity: colab should exist --------------------------------------------------------------
if ! command -v colab >/dev/null 2>&1; then
    say "WARN: 'colab' (google-colab-cli) not found on PATH."
    say "      install it first:  uv tool install google-colab-cli"
    say "      continuing anyway -- the .pth is written, it activates once colab exists."
fi

# --- download + extract ----------------------------------------------------------------------
command -v tar >/dev/null 2>&1 || die "tar not found on PATH"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/colab-ext.XXXXXX")" || die "cannot create temp dir"
trap 'rm -rf "$TMP"' EXIT INT TERM

TARBALL="${TMP}/src.tar.gz"
DOWNLOADED_REPO=""

if [ -n "$EXPLICIT_REPO" ]; then
    CANDIDATES="$EXPLICIT_REPO"
else
    CANDIDATES="deluxebear/colab-ext deluxebear/colab-dfs-ext"
fi

for candidate_repo in $CANDIDATES; do
    url="https://github.com/${candidate_repo}/archive/refs/heads/${REF}.tar.gz"
    say "downloading ${candidate_repo}@${REF} ..."
    if fetch "$url" > "$TARBALL" 2>/dev/null && [ -s "$TARBALL" ]; then
        DOWNLOADED_REPO="$candidate_repo"
        break
    fi
    rm -f "$TARBALL"
done

[ -n "$DOWNLOADED_REPO" ] || die "download failed for ref '${REF}'"
tar -xzf "$TARBALL" -C "$TMP" || die "extract failed (${TARBALL})"

SRC_DIR=""
for d in "$TMP"/*/; do
    [ -f "${d}install.py" ] && SRC_DIR="$d" && break
done
[ -n "$SRC_DIR" ] || die "install.py not found in the tarball"

# --- install ----------------------------------------------------------------------------------
cd "$SRC_DIR"
"$PY" install.py || die "install.py failed (run it again with COLAB_EXT_DEBUG=1 for details)"

# --- next steps --------------------------------------------------------------------------------
cat <<'EOF'

[colab-ext] installed. next steps:
  1. colab ext doctor            # verify the wiring & multi-account state
  2. colab account list          # view or add Colab accounts
  3. colab drive consent         # one-time Google Drive consent (browser opens once)
  4. colab drivemount -s <name>  # zero-browser Drive mount (enhanced built-in command)
[colab-ext] legacy aliases (`colab dfs ...`) continue to work.
EOF
