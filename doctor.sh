#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_NAME="android-use-plugins"
MARKETPLACE_PATH="${ANDROID_USE_PLUGIN_MARKETPLACE:-$HOME/.agents/plugins/marketplace.json}"
cd "$ROOT"

ok() {
  printf 'ok   %s\n' "$*"
}

warn() {
  printf 'warn %s\n' "$*" >&2
}

fail() {
  printf 'fail %s\n' "$*" >&2
  exit 1
}

command -v python3 >/dev/null 2>&1 && ok "python3: $(python3 --version 2>&1)" || fail "python3 not found"
command -v git >/dev/null 2>&1 && ok "git: $(git --version)" || fail "git not found"

if command -v adb >/dev/null 2>&1; then
  ok "adb: $(command -v adb)"
  adb devices -l || warn "adb devices failed"
else
  warn "adb not found. Install Android platform tools."
fi

if command -v scrcpy >/dev/null 2>&1; then
  ok "scrcpy: $(scrcpy --version | head -1)"
else
  warn "scrcpy not found. Install with: brew install scrcpy"
fi

[ -f "$ROOT/.codex-plugin/plugin.json" ] || fail ".codex-plugin/plugin.json missing"
[ -f "$ROOT/.mcp.json" ] || fail ".mcp.json missing"
[ -f "$ROOT/scripts/android_use_mcp.py" ] || fail "scripts/android_use_mcp.py missing"

python3 - <<'PY'
import json
from pathlib import Path
plugin = json.loads(Path(".codex-plugin/plugin.json").read_text())
assert plugin["name"] == "android-use-plugins", plugin["name"]
assert plugin["interface"]["displayName"] == "Android Use Plugins", plugin["interface"]["displayName"]
mcp = json.loads(Path(".mcp.json").read_text())
assert "android-use" in mcp["mcpServers"], mcp
print("ok   plugin manifest and mcp config")
PY

python3 -m py_compile "$ROOT/scripts/android_use_mcp.py" "$ROOT/scripts/test_android_use_mcp.py"
python3 "$ROOT/scripts/test_android_use_mcp.py"

if [ -f "$MARKETPLACE_PATH" ]; then
  MARKETPLACE_PATH="$MARKETPLACE_PATH" PLUGIN_NAME="$PLUGIN_NAME" python3 - <<'PY'
import json
import os
from pathlib import Path
path = Path(os.environ["MARKETPLACE_PATH"]).expanduser()
name = os.environ["PLUGIN_NAME"]
payload = json.loads(path.read_text())
assert payload.get("name") == name, payload.get("name")
assert payload.get("interface", {}).get("displayName") == "Android Use Plugins", payload.get("interface")
plugins = payload.get("plugins", [])
assert any(isinstance(item, dict) and item.get("name") == name for item in plugins), f"{name} not found in {path}"
print(f"ok   marketplace entry: {path}")
PY
else
  warn "marketplace not found: $MARKETPLACE_PATH"
fi

ok "doctor finished"
