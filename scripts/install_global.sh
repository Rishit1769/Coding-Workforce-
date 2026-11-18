#!/bin/sh
# Make `workforce` and `force` available on PATH without activating the engine's virtualenv.
# The engine (code, central config and state) stays in its installation directory;
# ~/.local/bin/{workforce,force} are only symlinks to the venv entry points.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN_DIR="${WORKFORCE_BIN_DIR:-$HOME/.local/bin}"
mkdir -p "$BIN_DIR"
for cmd in workforce force; do
  TARGET="$ROOT/.venv/bin/$cmd"
  [ -x "$TARGET" ] || { echo "missing $TARGET - run: uv venv --python 3.12 && uv pip install -e '.[dev]'" >&2; exit 1; }
  LINK="$BIN_DIR/$cmd"
  if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
    echo "$LINK exists and is not a symlink; refusing to overwrite" >&2; exit 1
  fi
  if [ -L "$LINK" ] && [ "$(readlink "$LINK")" != "$TARGET" ]; then
    echo "$LINK already points to $(readlink "$LINK"); refusing to overwrite" >&2; exit 1
  fi
  ln -sfn "$TARGET" "$LINK"
  echo "linked $LINK -> $TARGET"
done
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "note: add $BIN_DIR to your PATH" ;; esac
