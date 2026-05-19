#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_NAME="android-use-plugins"
CODEX_CONFIG_PATH="${ANDROID_USE_CODEX_CONFIG:-$HOME/.codex/config.toml}"
MARKETPLACE_PATH="${ANDROID_USE_PLUGIN_MARKETPLACE:-$HOME/marketplace.json}"
AGENTS_MARKETPLACE_PATH="${ANDROID_USE_AGENTS_PLUGIN_MARKETPLACE:-$HOME/.agents/plugins/marketplace.json}"
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
  fail "scrcpy not found. Install with: brew install scrcpy"
fi

ENV_FILE="${ANDROID_USE_ENV_FILE:-$HOME/.config/android-use/env}"
if [ -f "$ENV_FILE" ]; then
  if grep -Eq '^(export[[:space:]]+)?ANDROID_USE_WIRELESS_HOST=' "$ENV_FILE" || grep -Eq '^(export[[:space:]]+)?ANDROID_USE_SERIAL=.*:' "$ENV_FILE"; then
    ok "wireless adb config: $ENV_FILE"
  else
    warn "android-use env exists but no wireless adb config: $ENV_FILE"
  fi
else
  warn "wireless adb config not found; run android_wireless_pair once if you want cable-free use"
fi

[ -f "$ROOT/.codex-plugin/plugin.json" ] || fail ".codex-plugin/plugin.json missing"
[ -f "$ROOT/.mcp.json" ] || fail ".mcp.json missing"
[ -f "$ROOT/scripts/android_use_mcp.py" ] || fail "scripts/android_use_mcp.py missing"

python3 - <<'PY'
import json
from pathlib import Path
plugin = json.loads(Path(".codex-plugin/plugin.json").read_text())
assert plugin["name"] == "android-use-plugins", plugin["name"]
assert plugin["interface"]["displayName"] == "Android", plugin["interface"]["displayName"]
assert plugin.get("icon") == "./assets/android.png", plugin.get("icon")
assert plugin["interface"]["icon"] == "./assets/android.png", plugin["interface"].get("icon")
assert plugin["interface"]["composerIcon"] == "./assets/android.png", plugin["interface"].get("composerIcon")
assert plugin["interface"]["logo"] == "./assets/android.png", plugin["interface"].get("logo")
mcp = json.loads(Path(".mcp.json").read_text())
assert "android-use" in mcp["mcpServers"], mcp
print("ok   plugin manifest and mcp config")
PY

python3 -m py_compile "$ROOT/scripts/android_use_mcp.py" "$ROOT/scripts/test_android_use_mcp.py"
python3 "$ROOT/scripts/test_android_use_mcp.py"

check_marketplace() {
  local marketplace_path="$1"
  if [ -f "$marketplace_path" ]; then
    MARKETPLACE_PATH="$marketplace_path" PLUGIN_NAME="$PLUGIN_NAME" python3 - <<'PY'
import json
import os
from pathlib import Path
path = Path(os.environ["MARKETPLACE_PATH"]).expanduser()
name = os.environ["PLUGIN_NAME"]
payload = json.loads(path.read_text())
plugins = payload.get("plugins", [])
matches = [item for item in plugins if isinstance(item, dict) and item.get("name") == name]
assert matches, f"{name} not found in {path}"
legacy = [item.get("name") for item in plugins if isinstance(item, dict) and item.get("name") in {"android-use", "xiaoluxue-android-use"}]
assert not legacy, f"legacy android plugin entries still present in {path}: {legacy}"
entry = matches[-1]
assert entry.get("source", {}).get("path") == f"./plugins/{name}", entry
expected_icon = f"./plugins/{name}/assets/android.png"
assert entry.get("displayName") == "Android", entry
assert entry.get("icon") == expected_icon, entry
assert entry.get("interface", {}).get("displayName") == "Android", entry
assert entry.get("interface", {}).get("icon") == expected_icon, entry
assert entry.get("interface", {}).get("composerIcon") == expected_icon, entry
assert entry.get("interface", {}).get("logo") == expected_icon, entry
plugin_candidates = [
    path.parent / "plugins" / name,
    path.parent / name,
]
assert any((candidate / ".codex-plugin" / "plugin.json").exists() for candidate in plugin_candidates), (
    f"{name} plugin directory not found for {path}; checked: {plugin_candidates}"
)
print(f"ok   marketplace entry: {path}")
PY
  else
    warn "marketplace not found: $marketplace_path"
  fi
}

check_marketplace "$MARKETPLACE_PATH"
if [ "$AGENTS_MARKETPLACE_PATH" != "$MARKETPLACE_PATH" ]; then
  check_marketplace "$AGENTS_MARKETPLACE_PATH"
fi

if [ -f "$CODEX_CONFIG_PATH" ]; then
  CODEX_CONFIG_PATH="$CODEX_CONFIG_PATH" PLUGIN_NAME="$PLUGIN_NAME" python3 - <<'PY'
import os
from pathlib import Path

path = Path(os.environ["CODEX_CONFIG_PATH"]).expanduser()
name = os.environ["PLUGIN_NAME"]
section = None
local_source = None
enabled = False
for raw in path.read_text().splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    if line.startswith("[") and line.endswith("]"):
        section = line.strip("[]").strip()
        continue
    if section == "marketplaces.local" and "=" in line:
        key, value = line.split("=", 1)
        if key.strip() != "source":
            continue
        local_source = value.strip().strip('"')
    if section == f'plugins."{name}@local"' and line.startswith("enabled"):
        _, value = line.split("=", 1)
        enabled = value.strip().lower() == "true"

if enabled:
    assert local_source, f"{path}: marketplaces.local.source is missing"
    plugin_dir = Path(local_source).expanduser() / "plugins" / name
    manifest_path = plugin_dir / ".codex-plugin" / "plugin.json"
    assert manifest_path.exists(), f"enabled plugin is not installed where Codex reads it: {manifest_path}"
    print(f"ok   Codex local plugin path: {plugin_dir}")
else:
    print(f"warn {name}@local is not enabled in {path}")
PY
else
  warn "Codex config not found: $CODEX_CONFIG_PATH"
fi

ok "doctor finished"
