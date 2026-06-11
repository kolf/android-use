#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PARENT="$(dirname "$ROOT")"
NAME="$(basename "$ROOT")"
OUT_DIR="$ROOT/dist"
OUT="$OUT_DIR/android-use-plugins.zip"

if ! command -v zip >/dev/null 2>&1; then
  echo "未找到 zip 命令。"
  exit 1
fi

mkdir -p "$OUT_DIR"
rm -f "$OUT"

cd "$PARENT"
zip -r "$OUT" "$NAME" \
  -x "$NAME/.git/*" \
  -x "$NAME/.android/*" \
  -x "$NAME/.android-use/*" \
  -x "$NAME/.venv/*" \
  -x "$NAME/.screen/*" \
  -x "$NAME/Library/*" \
  -x "$NAME/tools/*" \
  -x "$NAME/node_modules/*" \
  -x "$NAME/.codex/*" \
  -x "$NAME/__pycache__/*" \
  -x "$NAME/scripts/__pycache__" \
  -x "$NAME/scripts/__pycache__/*" \
  -x "$NAME/scripts/*/__pycache__" \
  -x "$NAME/scripts/*/__pycache__/*" \
  -x "$NAME/scripts/*/*/__pycache__" \
  -x "$NAME/scripts/*/*/__pycache__/*" \
  -x "$NAME/skills/*/__pycache__/*" \
  -x "$NAME/dist/*" \
  -x "$NAME/.DS_Store" \
  -x "$NAME/*/.DS_Store" \
  -x "$NAME/*/*/.DS_Store" \
  -x "$NAME/*/*/*/.DS_Store" \
  >/dev/null

echo "已生成：$OUT"
