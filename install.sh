#!/usr/bin/env bash
set -euo pipefail

PLUGIN_NAME="android-use-plugins"
REPO_URL="${ANDROID_USE_PLUGIN_REPO_URL:-https://gitlab.xiaoluxue.cn/shixiankang/android-use.git}"
CODEX_CONFIG_PATH="${ANDROID_USE_CODEX_CONFIG:-$HOME/.codex/config.toml}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

detect_plugin_root() {
  local detected=""
  if command -v python3 >/dev/null 2>&1 && [ -f "$CODEX_CONFIG_PATH" ]; then
    detected="$(CODEX_CONFIG_PATH="$CODEX_CONFIG_PATH" python3 - <<'PY' || true
import os
from pathlib import Path

path = Path(os.environ["CODEX_CONFIG_PATH"]).expanduser()
section = None
source = None
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
        source = value.strip().strip('"')
        break
if source:
    print(str(Path(source).expanduser() / "plugins"))
PY
)"
    if [ -n "$detected" ]; then
      printf '%s\n' "$detected"
      return
    fi
  fi
  printf '%s\n' "$HOME/plugins"
}

PLUGIN_ROOT="${ANDROID_USE_PLUGIN_ROOT:-$(detect_plugin_root)}"
INSTALL_DIR="${ANDROID_USE_PLUGIN_INSTALL_DIR:-$PLUGIN_ROOT/$PLUGIN_NAME}"
MARKETPLACE_PATH="${ANDROID_USE_PLUGIN_MARKETPLACE:-$(dirname "$PLUGIN_ROOT")/marketplace.json}"
AGENTS_PLUGIN_ROOT="${ANDROID_USE_AGENTS_PLUGIN_ROOT:-$HOME/.agents/plugins}"
AGENTS_INSTALL_DIR="${ANDROID_USE_AGENTS_PLUGIN_INSTALL_DIR:-$AGENTS_PLUGIN_ROOT/$PLUGIN_NAME}"
AGENTS_MARKETPLACE_PATH="${ANDROID_USE_AGENTS_PLUGIN_MARKETPLACE:-$AGENTS_PLUGIN_ROOT/marketplace.json}"
LOCAL_MARKETPLACE_SOURCE="$(dirname "$PLUGIN_ROOT")"
CODEX_CACHE_INSTALL_DIR="${ANDROID_USE_CODEX_PLUGIN_CACHE_DIR:-$HOME/.codex/plugins/cache/local/$PLUGIN_NAME/0.1.0}"

info() {
  printf '[android-use-plugins] %s\n' "$*"
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$1" >&2
    return 1
  fi
}

copy_bundle_contents() {
  local source_dir="$1"
  local install_dir="$2"
  mkdir -p "$install_dir"
  COPY_SOURCE_DIR="$source_dir" COPY_INSTALL_DIR="$install_dir" python3 - <<'PY'
import shutil
import os
from pathlib import Path

source = Path(os.environ["COPY_SOURCE_DIR"])
target = Path(os.environ["COPY_INSTALL_DIR"])
items = [
    ".codex-plugin",
    ".mcp.json",
    "README.md",
    "scripts",
    "skills",
    "assets",
    "docs",
    "install.sh",
    "doctor.sh",
    "package.sh",
    "marketplace-entry.json",
    "marketplace.example.json",
    ".gitignore",
]

for name in items:
    src = source / name
    if not src.exists():
        continue
    dst = target / name
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
PY
}

copy_local_bundle() {
  info "Installing from local bundle: $SOURCE_DIR"
  copy_bundle_contents "$SOURCE_DIR" "$INSTALL_DIR"
}

clear_quarantine() {
  local target_dir="$1"
  if command -v xattr >/dev/null 2>&1 && [ -e "$target_dir" ]; then
    xattr -dr com.apple.quarantine "$target_dir" 2>/dev/null || true
  fi
}

install_or_update_plugin() {
  mkdir -p "$PLUGIN_ROOT"
  if [ "$SOURCE_DIR" = "$INSTALL_DIR" ]; then
    info "Plugin is already at $INSTALL_DIR"
    clear_quarantine "$INSTALL_DIR"
    return
  fi
  if [ -f "$SOURCE_DIR/.codex-plugin/plugin.json" ]; then
    copy_local_bundle
    clear_quarantine "$INSTALL_DIR"
    return
  fi
  if [ -d "$INSTALL_DIR/.git" ]; then
    require_command git
    info "Updating $INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --ff-only
    clear_quarantine "$INSTALL_DIR"
    return
  fi
  if [ -e "$INSTALL_DIR" ]; then
    printf 'Install path already exists but is not a git checkout: %s\n' "$INSTALL_DIR" >&2
    printf 'Move it away or set ANDROID_USE_PLUGIN_INSTALL_DIR.\n' >&2
    return 1
  fi
  require_command git
  if git clone "$REPO_URL" "$INSTALL_DIR"; then
    clear_quarantine "$INSTALL_DIR"
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
  clear_quarantine "$INSTALL_DIR"
}

write_marketplace() {
  local marketplace_path="${1:-$MARKETPLACE_PATH}"
  mkdir -p "$(dirname "$marketplace_path")"
  MARKETPLACE_PATH="$marketplace_path" PLUGIN_NAME="$PLUGIN_NAME" python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["MARKETPLACE_PATH"]).expanduser()
plugin_name = os.environ["PLUGIN_NAME"]
icon_path = f"./plugins/{plugin_name}/assets/android.png"
entry = {
    "name": plugin_name,
    "displayName": "Android",
    "icon": icon_path,
    "composerIcon": icon_path,
    "logo": icon_path,
    "source": {"source": "local", "path": f"./plugins/{plugin_name}"},
    "policy": {"installation": "INSTALLED_BY_DEFAULT", "authentication": "ON_INSTALL"},
    "category": "Developer Tools",
    "interface": {
        "displayName": "Android",
        "shortDescription": "在 Codex 中控制 Android 设备",
        "icon": icon_path,
        "composerIcon": icon_path,
        "logo": icon_path,
    },
}
legacy_plugin_names = {"android-use", "xiaoluxue-android-use"}
if path.exists():
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        raise SystemExit(f"Invalid JSON in {path}")
else:
    payload = {
        "name": "local",
        "interface": {"displayName": "Local Plugins"},
        "plugins": [],
    }

if payload.get("name") in (None, "", "xiaoluxue-codex-plugins", "android-use-plugins", "[TODO: marketplace-name]"):
    payload["name"] = "local"
interface = payload.setdefault("interface", {})
if interface.get("displayName") in (None, "", "Xiaoluxue Codex Plugins", "Android", "[TODO: Marketplace Display Name]"):
    interface["displayName"] = "Local Plugins"
plugins = payload.setdefault("plugins", [])
if not isinstance(plugins, list):
    raise SystemExit(f"{path}: plugins must be a list")

replacement_index = None
migrated_installation = None
next_plugins = []
for plugin in plugins:
    if not isinstance(plugin, dict):
        next_plugins.append(plugin)
        continue
    name = plugin.get("name")
    if name == plugin_name or name in legacy_plugin_names:
        if replacement_index is None:
            replacement_index = len(next_plugins)
        policy = plugin.get("policy") if isinstance(plugin.get("policy"), dict) else {}
        if policy.get("installation") == "INSTALLED_BY_DEFAULT":
            migrated_installation = "INSTALLED_BY_DEFAULT"
        continue
    next_plugins.append(plugin)

if migrated_installation:
    entry["policy"]["installation"] = migrated_installation
if replacement_index is None:
    next_plugins.append(entry)
else:
    next_plugins.insert(replacement_index, entry)
payload["plugins"] = next_plugins

path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
print(path)
PY
}

install_agents_compat() {
  if [ "$INSTALL_DIR" = "$AGENTS_INSTALL_DIR" ]; then
    return 0
  fi
  info "Installing compatibility copy: $AGENTS_INSTALL_DIR"
  copy_bundle_contents "$INSTALL_DIR" "$AGENTS_INSTALL_DIR"
}

install_codex_cache_compat() {
  if [ "$INSTALL_DIR" = "$CODEX_CACHE_INSTALL_DIR" ]; then
    return 0
  fi
  info "Installing Codex cache copy: $CODEX_CACHE_INSTALL_DIR"
  copy_bundle_contents "$INSTALL_DIR" "$CODEX_CACHE_INSTALL_DIR"
  clear_quarantine "$CODEX_CACHE_INSTALL_DIR"
}

configure_codex_config() {
  mkdir -p "$(dirname "$CODEX_CONFIG_PATH")"
  touch "$CODEX_CONFIG_PATH"
  CODEX_CONFIG_PATH="$CODEX_CONFIG_PATH" PLUGIN_NAME="$PLUGIN_NAME" LOCAL_MARKETPLACE_SOURCE="$LOCAL_MARKETPLACE_SOURCE" python3 - <<'PY'
from datetime import datetime, timezone
import os
from pathlib import Path

path = Path(os.environ["CODEX_CONFIG_PATH"]).expanduser()
plugin_name = os.environ["PLUGIN_NAME"]
local_source = str(Path(os.environ["LOCAL_MARKETPLACE_SOURCE"]).expanduser())
text = path.read_text()
original = text
for legacy_name in ("android-use", "xiaoluxue-android-use"):
    text = text.replace(f'[plugins."{legacy_name}@local"]', f'[plugins."{plugin_name}@local"]')
    text = text.replace(f'[plugins."{legacy_name}@local".', f'[plugins."{plugin_name}@local".')

lines = text.splitlines()

def section_bounds(header):
    start = None
    for index, line in enumerate(lines):
        if line.strip() == header:
            start = index
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break
    return start, end

def ensure_key(section_header, key, value):
    bounds = section_bounds(section_header)
    if bounds is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([section_header, f'{key} = {value}'])
        return True
    start, end = bounds
    for index in range(start + 1, end):
        stripped = lines[index].strip()
        if stripped.startswith(f"{key} ") or stripped.startswith(f"{key}="):
            desired = f'{key} = {value}'
            if lines[index] != desired:
                lines[index] = desired
                return True
            return False
    lines.insert(start + 1, f'{key} = {value}')
    return True

changed = text != original
changed = ensure_key("[marketplaces.local]", "source", f'"{local_source}"') or changed
changed = ensure_key("[marketplaces.local]", "source_type", '"local"') or changed
changed = ensure_key(
    "[marketplaces.local]",
    "last_updated",
    f'"{datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}"',
) or changed
changed = ensure_key(f'[plugins."{plugin_name}@local"]', "enabled", "true") or changed

if changed:
    path.write_text("\n".join(lines).rstrip() + "\n")
print(path)
PY
}

main() {
  require_command python3
  require_command scrcpy
  if ! command -v adb >/dev/null 2>&1; then
    info "adb not found. Install Android platform tools, e.g. brew install --cask android-platform-tools"
  fi
  install_or_update_plugin
  marketplace="$(write_marketplace "$MARKETPLACE_PATH")"
  install_agents_compat
  clear_quarantine "$AGENTS_INSTALL_DIR"
  install_codex_cache_compat
  agents_marketplace="$(write_marketplace "$AGENTS_MARKETPLACE_PATH")"
  config="$(configure_codex_config || true)"
  info "Marketplace updated: $marketplace"
  if [ "$agents_marketplace" != "$marketplace" ]; then
    info "Agents marketplace updated: $agents_marketplace"
  fi
  if [ -n "${config:-}" ]; then
    info "Codex config updated: $config"
  fi
  info "Plugin path: $INSTALL_DIR"
  info "Codex cache path: $CODEX_CACHE_INSTALL_DIR"
  info "Restart Codex, then Android should appear in the plugin list."
  info "Run ./doctor.sh after restart if the plugin does not appear or cannot control a device."
}

main "$@"
