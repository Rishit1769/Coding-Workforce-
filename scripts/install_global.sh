#!/bin/sh
# Make `workforce` available on PATH without activating the engine's virtualenv.
# The engine (code, central config and state) stays in its installation directory;
# ~/.local/bin/workforce is only a symlink to the venv entry point.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="$ROOT/.venv/bin/workforce"
BIN_DIR="${WORKFORCE_BIN_DIR:-$HOME/.local/bin}"
[ -x "$TARGET" ] || { echo "missing $TARGET - run: uv venv --python 3.12 && uv pip install -e '.[dev]'" >&2; exit 1; }
mkdir -p "$BIN_DIR"
if [ -e "$BIN_DIR/workforce" ] && [ ! -L "$BIN_DIR/workforce" ]; then
  echo "$BIN_DIR/workforce exists and is not a symlink; refusing to overwrite" >&2; exit 1
fi
ln -sfn "$TARGET" "$BIN_DIR/workforce"
echo "linked $BIN_DIR/workforce -> $TARGET"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "note: add $BIN_DIR to your PATH" ;; esac
