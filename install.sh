#!/usr/bin/env bash
set -euo pipefail

PLUGIN_NAME="android-use-plugins"
REPO_URL="${ANDROID_USE_PLUGIN_REPO_URL:-https://gitlab.xiaoluxue.cn/shixiankang/android-use.git}"
PLUGIN_ROOT="${ANDROID_USE_PLUGIN_ROOT:-$HOME/.agents/plugins}"
INSTALL_DIR="${ANDROID_USE_PLUGIN_INSTALL_DIR:-$PLUGIN_ROOT/$PLUGIN_NAME}"
MARKETPLACE_PATH="${ANDROID_USE_PLUGIN_MARKETPLACE:-$HOME/.agents/plugins/marketplace.json}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

info() {
  printf '[android-use-plugins] %s\n' "$*"
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$1" >&2
    return 1
  fi
}

install_or_update_plugin() {
  mkdir -p "$PLUGIN_ROOT"
  if [ "$SOURCE_DIR" = "$INSTALL_DIR" ]; then
    info "Plugin is already at $INSTALL_DIR"
    return
  fi
  if [ -d "$INSTALL_DIR/.git" ]; then
    info "Updating $INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --ff-only
    return
  fi
  if [ -e "$INSTALL_DIR" ]; then
    printf 'Install path already exists but is not a git checkout: %s\n' "$INSTALL_DIR" >&2
    printf 'Move it away or set ANDROID_USE_PLUGIN_INSTALL_DIR.\n' >&2
    return 1
  fi
  if git clone "$REPO_URL" "$INSTALL_DIR"; then
    return
  fi
  info "Git clone failed, installing from local checkout"
  mkdir -p "$INSTALL_DIR"
  for item in .codex-plugin .mcp.json README.md scripts skills assets docs install.sh doctor.sh marketplace-entry.json marketplace.example.json .gitignore; do
    if [ -e "$SOURCE_DIR/$item" ]; then
      cp -R "$SOURCE_DIR/$item" "$INSTALL_DIR/"
    fi
  done
  if [ ! -f "$INSTALL_DIR/.codex-plugin/plugin.json" ]; then
    info "Cloning $REPO_URL to $INSTALL_DIR"
    git clone "$REPO_URL" "$INSTALL_DIR"
  fi
}

write_marketplace() {
  mkdir -p "$(dirname "$MARKETPLACE_PATH")"
  MARKETPLACE_PATH="$MARKETPLACE_PATH" PLUGIN_NAME="$PLUGIN_NAME" python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["MARKETPLACE_PATH"]).expanduser()
plugin_name = os.environ["PLUGIN_NAME"]
entry = {
    "name": plugin_name,
    "source": {"source": "local", "path": f"./plugins/{plugin_name}"},
    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
    "category": "Developer Tools",
}
if path.exists():
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        raise SystemExit(f"Invalid JSON in {path}")
else:
    payload = {
        "name": "android-use-plugins",
        "interface": {"displayName": "Android Use Plugins"},
        "plugins": [],
    }

if payload.get("name") in (None, "", "xiaoluxue-codex-plugins", "[TODO: marketplace-name]"):
    payload["name"] = "android-use-plugins"
interface = payload.setdefault("interface", {})
if interface.get("displayName") in (None, "", "Xiaoluxue Codex Plugins", "[TODO: Marketplace Display Name]"):
    interface["displayName"] = "Android Use Plugins"
plugins = payload.setdefault("plugins", [])
if not isinstance(plugins, list):
    raise SystemExit(f"{path}: plugins must be a list")
for index, plugin in enumerate(plugins):
    if isinstance(plugin, dict) and plugin.get("name") == plugin_name:
        plugins[index] = entry
        break
else:
    plugins.append(entry)

path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
print(path)
PY
}

main() {
  require_command git
  require_command python3
  if ! command -v adb >/dev/null 2>&1; then
    info "adb not found. Install Android platform tools, e.g. brew install --cask android-platform-tools"
  fi
  if ! command -v scrcpy >/dev/null 2>&1; then
    info "scrcpy not found. Install it, e.g. brew install scrcpy"
  fi
  install_or_update_plugin
  marketplace="$(write_marketplace)"
  info "Marketplace updated: $marketplace"
  info "Plugin path: $INSTALL_DIR"
  info "Restart Codex, then enable Android Use Plugins from the local plugin marketplace."
  info "Run ./doctor.sh after restart if the plugin does not appear or cannot control a device."
}

main "$@"
