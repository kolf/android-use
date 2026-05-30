#!/usr/bin/env python3
"""MCP server for Android device control through adb and scrcpy.

The server intentionally has no third-party Python dependencies. It speaks the
newline-delimited JSON-RPC transport used by MCP stdio servers.
"""

# Loaded by scripts/android_use_mcp.py. Keep this file below 2000 lines.

from __future__ import annotations

import base64
import ast
import contextlib
import fcntl
import json
import os
import plistlib
import re
import signal
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable, Iterator


SERVER_NAME = "android-use"
SERVER_VERSION = "0.1.0"
ANDROID_USE_BUNDLE_ID = "com.kolf.android-use"
DEFAULT_TIMEOUT = 30
TMP_DIR = Path(os.environ.get("ANDROID_USE_TMP_DIR", "/tmp/android-use"))
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
LOCAL_PLATFORM_TOOLS = PLUGIN_ROOT / "tools" / "android-platform-tools" / "platform-tools"
ANDROID_USE_DIR = PLUGIN_ROOT / ".android-use"
RECORDINGS_DIR = ANDROID_USE_DIR / "recordings"
RECIPES_DIR = ANDROID_USE_DIR / "recipes"
SOURCE_MAP_DIR = ANDROID_USE_DIR / "app-maps"
SCREEN_DIR = PLUGIN_ROOT / ".screen"
VIDEO_RECORDINGS_DIR = SCREEN_DIR / "video-recordings"
OPENAI_BASE_URL = "https://api.openai.com/v1"

SCRCPY_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}
SCRCPY_LOCK_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}
SCREEN_VIEWER_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}
WEBRTC_VIEWER_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}
ACTIVE_RECORDINGS: dict[str, dict[str, Any]] = {}
SCRCPY_VIDEO_RECORDING_PROCESSES: dict[str, subprocess.Popen[bytes]] = {}
SCRCPY_VIDEO_RECORDINGS: dict[str, dict[str, Any]] = {}
SCRCPY_RESIDENT_THREAD: threading.Thread | None = None
SCRCPY_RESIDENT_LOCK = threading.Lock()
SCRCPY_RESIDENT_LOCK_HANDLE: Any | None = None
SCRCPY_RESIDENT_STATUS: dict[str, Any] = {
    "enabled": False,
    "running": False,
    "serials": [],
    "last_check_at": None,
    "last_error": None,
}

REMOTE_UI_DUMP_PATH = "/sdcard/android-use-window.xml"
USER_ENV_FILE = Path(os.environ.get("ANDROID_USE_ENV_FILE", "~/.config/android-use/env")).expanduser()


class AndroidUseError(Exception):
    """User-facing plugin error."""


def parse_env_assignment(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].strip()
    if "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return None
    if key not in {"OPENAI_API_KEY", "ANDROID_SERIAL"} and not key.startswith("ANDROID_USE_"):
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return key, value


def load_user_env_file(path: Path = USER_ENV_FILE) -> None:
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return
    except OSError:
        return
    for line in lines:
        assignment = parse_env_assignment(line)
        if not assignment:
            continue
        key, value = assignment
        os.environ.setdefault(key, value)


load_user_env_file()


def read_user_env_values(path: Path = USER_ENV_FILE) -> dict[str, str]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in lines:
        assignment = parse_env_assignment(line)
        if assignment:
            key, value = assignment
            values[key] = value
    return values


def quote_env_value(value: str) -> str:
    return shlex.quote(str(value))


def update_user_env_file(updates: dict[str, str], path: Path = USER_ENV_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        lines = []
    seen: set[str] = set()
    next_lines: list[str] = []
    for line in lines:
        assignment = parse_env_assignment(line)
        if not assignment:
            next_lines.append(line)
            continue
        key, _old_value = assignment
        if key in updates:
            next_lines.append(f"{key}={quote_env_value(updates[key])}")
            seen.add(key)
        else:
            next_lines.append(line)
    for key, value in updates.items():
        if key not in seen:
            next_lines.append(f"{key}={quote_env_value(value)}")
    path.write_text("\n".join(next_lines).rstrip() + "\n")
    for key, value in updates.items():
        os.environ[key] = value


def adb_binary() -> str:
    configured = os.environ.get("ANDROID_USE_ADB")
    if configured:
        return configured
    local_adb = LOCAL_PLATFORM_TOOLS / "adb"
    if local_adb.exists():
        return str(local_adb)
    return "adb"


def scrcpy_binary() -> str:
    return os.environ.get("ANDROID_USE_SCRCPY", "scrcpy")


def env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().casefold() not in {"0", "false", "no", "off", "disabled"}


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def tool_env() -> dict[str, str]:
    env = os.environ.copy()
    home = Path(env.get("ANDROID_USE_HOME", str(PLUGIN_ROOT))).expanduser()
    android_home = home / ".android"
    android_home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    env["ANDROID_USER_HOME"] = str(android_home)
    env.setdefault("ADB", adb_binary())
    return env


def decode_bytes(payload: bytes) -> str:
    return payload.decode("utf-8", errors="replace").strip()


def run_command(
    args: list[str],
    *,
    input_bytes: bytes | None = None,
    timeout: int | float = DEFAULT_TIMEOUT,
) -> tuple[bytes, bytes]:
    try:
        result = subprocess.run(
            args,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=tool_env(),
        )
    except FileNotFoundError as exc:
        raise AndroidUseError(f"Command not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AndroidUseError(f"Command timed out after {timeout}s: {' '.join(args)}") from exc

    if result.returncode != 0:
        stderr = decode_bytes(result.stderr)
        stdout = decode_bytes(result.stdout)
        detail = stderr or stdout or f"exit code {result.returncode}"
        raise AndroidUseError(f"Command failed: {' '.join(args)}\n{detail}")

    return result.stdout, result.stderr


def adb(args: list[str], serial: str | None = None, timeout: int | float = DEFAULT_TIMEOUT) -> bytes:
    command = [adb_binary()]
    if serial:
        command.extend(["-s", serial])
    command.extend(args)
    stdout, _stderr = run_command(command, timeout=timeout)
    return stdout


def parse_adb_devices(output: str) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices"):
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        serial, state = fields[0], fields[1]
        details: dict[str, str] = {}
        for token in fields[2:]:
            if ":" in token:
                key, value = token.split(":", 1)
                details[key] = value
        devices.append({"serial": serial, "state": state, "details": details})
    return devices


def list_devices() -> list[dict[str, Any]]:
    output = decode_bytes(run_command([adb_binary(), "devices", "-l"])[0])
    return parse_adb_devices(output)


def is_tcp_serial(serial: str) -> bool:
    return bool(re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}:\d+", serial)) or serial.startswith("[")


def configured_serial() -> str | None:
    return os.environ.get("ANDROID_USE_SERIAL") or os.environ.get("ANDROID_SERIAL")


def device_identity(serial: str) -> str:
    for prop in ("ro.serialno", "ro.boot.serialno"):
        try:
            value = shell(serial, f"getprop {prop}", timeout=3).strip()
        except Exception:
            value = ""
        if value:
            return value
    return serial


def dedupe_connected_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for device in devices:
        serial = str(device.get("serial") or "")
        identity = device_identity(serial) if serial else serial
        grouped.setdefault(identity, []).append(device)
    preferred_config = configured_serial()
    deduped: list[dict[str, Any]] = []
    for group in grouped.values():
        chosen = None
        if preferred_config:
            chosen = next((device for device in group if device.get("serial") == preferred_config), None)
        if chosen is None:
            chosen = next((device for device in group if is_tcp_serial(str(device.get("serial") or ""))), None)
        deduped.append(chosen or group[0])
    return deduped


def parse_host_port(value: str) -> tuple[str, int] | None:
    raw = value.strip()
    match = re.fullmatch(r"(.+):(\d+)", raw)
    if not match:
        return None
    host, port_text = match.groups()
    try:
        port = int(port_text)
    except ValueError:
        return None
    if not host or port <= 0:
        return None
    return host.strip("[]"), port


def split_configured_values(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in re.split(r"[,;\s]+", str(value)) if item.strip()]


def unique_ordered(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def comma_join(values: Iterable[str]) -> str:
    return ",".join(unique_ordered(values))


def replace_configured_wireless_serial(values: str | None, host: str, serial: str) -> str:
    items: list[str] = []
    for item in split_configured_values(values):
        parsed = parse_host_port(item)
        if parsed and parsed[0] == host:
            continue
        items.append(item)
    items.append(serial)
    return comma_join(items)


def parse_adb_mdns_services(
    output: str,
    *,
    host: str | None = None,
    service_type: str = "_adb-tls-connect._tcp",
) -> list[dict[str, Any]]:
    services: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if service_type not in line:
            continue
        endpoints = re.findall(r"((?:\d{1,3}\.){3}\d{1,3}|[A-Za-z0-9_.-]+):(\d+)", line)
        service_name = line.split(service_type, 1)[0].strip().rstrip(".")
        for endpoint_host, port_text in endpoints:
            if host and endpoint_host != host:
                continue
            services.append(
                {
                    "service": line,
                    "service_name": service_name,
                    "service_type": service_type,
                    "host": endpoint_host,
                    "port": int(port_text),
                    "serial": f"{endpoint_host}:{port_text}",
                }
            )
    return services


def adb_mdns_connect_services(host: str | None = None) -> list[dict[str, Any]]:
    try:
        stdout, _stderr = run_command([adb_binary(), "mdns", "services"], timeout=8)
    except AndroidUseError:
        return []
    return parse_adb_mdns_services(decode_bytes(stdout), host=host)


def adb_connect_serial(host: str, port: int) -> dict[str, Any]:
    target = f"{host}:{int(port)}"
    stdout, stderr = run_command([adb_binary(), "connect", target], timeout=15)
    output = "\n".join(part for part in [decode_bytes(stdout), decode_bytes(stderr)] if part)
    connected = any(device.get("serial") == target and device.get("state") == "device" for device in list_devices())
    if not connected and not re.search(r"\bconnected to\b|\balready connected to\b", output, re.I):
        raise AndroidUseError(f"adb connect did not authorize {target}\n{output}")
    return {"serial": target, "host": host, "port": int(port), "output": output, "connected": connected}


def save_wireless_config(host: str, port: int, serial: str) -> None:
    values = {**read_user_env_values(), **os.environ}
    legacy_serial = values.get("ANDROID_USE_SERIAL") or values.get("ANDROID_SERIAL")
    legacy_wireless_serial = legacy_serial if legacy_serial and parse_host_port(legacy_serial) else None
    wireless_seed = values.get("ANDROID_USE_WIRELESS_DEVICES") or legacy_wireless_serial
    resident_seed = values.get("ANDROID_USE_SCRCPY_RESIDENT_SERIALS") or legacy_wireless_serial
    wireless_devices = replace_configured_wireless_serial(wireless_seed, host, serial)
    resident_serials = replace_configured_wireless_serial(resident_seed, host, serial)
    update_user_env_file(
        {
            "ANDROID_USE_WIRELESS_HOST": host,
            "ANDROID_USE_WIRELESS_PORT": str(int(port)),
            "ANDROID_USE_WIRELESS_DEVICES": wireless_devices,
            "ANDROID_USE_SERIAL": serial,
            "ANDROID_SERIAL": serial,
            "ANDROID_USE_SCRCPY_RESIDENT_SERIALS": resident_serials,
        }
    )


def wireless_configs_from_env() -> list[dict[str, Any]]:
    values = {**read_user_env_values(), **os.environ}
    configs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_config(host: str | None, port: int | None, serial: str | None, source: str) -> None:
        candidate_host = (host or "").strip() or None
        candidate_port = int(port) if port else None
        candidate_serial = (serial or "").strip() or None
        if candidate_serial and (not candidate_host or not candidate_port):
            parsed = parse_host_port(candidate_serial)
            if parsed:
                candidate_host = candidate_host or parsed[0]
                candidate_port = candidate_port or parsed[1]
        if candidate_host and candidate_port and not candidate_serial:
            candidate_serial = f"{candidate_host}:{candidate_port}"
        if not candidate_host and not candidate_serial:
            return
        key = candidate_serial or f"{candidate_host}:{candidate_port or ''}"
        if key in seen:
            return
        seen.add(key)
        configs.append(
            {
                "host": candidate_host,
                "port": candidate_port,
                "serial": candidate_serial,
                "source": source,
            }
        )

    for token in split_configured_values(values.get("ANDROID_USE_WIRELESS_DEVICES")):
        parsed = parse_host_port(token)
        if parsed:
            add_config(parsed[0], parsed[1], token, "ANDROID_USE_WIRELESS_DEVICES")
        else:
            add_config(token, None, None, "ANDROID_USE_WIRELESS_DEVICES")

    legacy_serial = values.get("ANDROID_USE_SERIAL") or values.get("ANDROID_SERIAL")
    legacy_host = values.get("ANDROID_USE_WIRELESS_HOST")
    legacy_port_text = values.get("ANDROID_USE_WIRELESS_PORT")
    legacy_port = int(legacy_port_text) if legacy_port_text and str(legacy_port_text).isdigit() else None
    if legacy_host or (legacy_serial and parse_host_port(legacy_serial)):
        add_config(legacy_host, legacy_port, legacy_serial, "legacy")

    return configs


def wireless_config_from_env() -> tuple[str | None, int | None, str | None]:
    configs = wireless_configs_from_env()
    if not configs:
        return None, None, None
    first = configs[0]
    return first.get("host"), first.get("port"), first.get("serial")


def wireless_reconnect(
    *,
    host: str | None = None,
    port: int | None = None,
    serial: str | None = None,
    save: bool = True,
    start_scrcpy: bool = False,
) -> dict[str, Any]:
    explicit_host = host
    explicit_port = port
    explicit_serial = serial
    env_host, env_port, env_serial = wireless_config_from_env()
    host = host or env_host
    if explicit_host and explicit_port is None and explicit_serial is None:
        port = None
        serial = None
    else:
        port = int(port or env_port or 0) or None
        serial = serial or env_serial
    if serial and (not host or not port):
        parsed = parse_host_port(serial)
        if parsed:
            host = host or parsed[0]
            port = port or parsed[1]
    if not host:
        raise AndroidUseError("Wireless host is not configured. Run android_wireless_pair first or pass host.")

    candidates: list[tuple[str, int]] = []
    for service in adb_mdns_connect_services(host):
        candidate = (str(service["host"]), int(service["port"]))
        if candidate not in candidates:
            candidates.append(candidate)
    if port:
        candidate = (host, int(port))
        if candidate not in candidates:
            candidates.append(candidate)
    if not candidates:
        raise AndroidUseError(f"No wireless ADB connect port found for {host}. Enable Wireless debugging and try again.")

    errors: list[str] = []
    for candidate_host, candidate_port in candidates:
        try:
            connected = adb_connect_serial(candidate_host, candidate_port)
            if save:
                save_wireless_config(candidate_host, candidate_port, str(connected["serial"]))
            scrcpy_result = None
            if start_scrcpy:
                scrcpy_result = ensure_default_scrcpy_window(str(connected["serial"]), {"show_scrcpy": True})
            return {"ok": True, **connected, "saved": save, "scrcpy": scrcpy_result}
        except Exception as exc:
            errors.append(f"{candidate_host}:{candidate_port}: {exc}")
    raise AndroidUseError("Wireless reconnect failed.\n" + "\n".join(errors[-5:]))


def wireless_reconnect_all(*, save: bool = True, start_scrcpy: bool = False) -> dict[str, Any]:
    configs = wireless_configs_from_env()
    if not configs:
        raise AndroidUseError("No saved wireless devices. Run android_wireless_pair first or pass host/serial.")
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for config in configs:
        try:
            result = wireless_reconnect(
                host=config.get("host"),
                port=config.get("port"),
                serial=config.get("serial"),
                save=save,
                start_scrcpy=start_scrcpy,
            )
            results.append({"config": config, **result})
        except Exception as exc:
            errors.append({"config": config, "error": str(exc)})
    if not results:
        detail = "\n".join(f"{item['config']}: {item['error']}" for item in errors[-5:])
        raise AndroidUseError("Wireless reconnect failed for all saved devices.\n" + detail)
    return {
        "ok": True,
        "count": len(results),
        "configured_count": len(configs),
        "results": results,
        "errors": errors,
    }


def auto_reconnect_wireless_if_needed() -> None:
    if not env_flag("ANDROID_USE_WIRELESS_AUTO_CONNECT", True):
        return
    configs = wireless_configs_from_env()
    if not configs:
        return
    try:
        if len(configs) > 1:
            wireless_reconnect_all(save=True, start_scrcpy=False)
        else:
            config = configs[0]
            wireless_reconnect(
                host=config.get("host"),
                port=config.get("port"),
                serial=config.get("serial"),
                save=True,
                start_scrcpy=False,
            )
    except Exception:
        return


def choose_serial(serial: str | None = None) -> str:
    devices = list_devices()
    connected = [device for device in devices if device.get("state") == "device"]

    if serial:
        for device in connected:
            if device.get("serial") == serial:
                return serial
        if parse_host_port(serial):
            with contextlib.suppress(Exception):
                reconnect = wireless_reconnect(serial=serial, save=True, start_scrcpy=False)
                if reconnect.get("serial"):
                    return str(reconnect["serial"])
        known = ", ".join(device.get("serial", "?") for device in devices) or "none"
        raise AndroidUseError(f"Android device '{serial}' is not connected and authorized. Known devices: {known}")

    if not connected:
        auto_reconnect_wireless_if_needed()
        devices = list_devices()
        connected = [device for device in devices if device.get("state") == "device"]
    env_serial = configured_serial()
    if env_serial:
        for device in connected:
            if device.get("serial") == env_serial:
                return env_serial
    if connected:
        connected = dedupe_connected_devices(connected)
    if not connected:
        raise AndroidUseError(android_connection_help_text(devices))
    if len(connected) > 1:
        serials = ", ".join(device["serial"] for device in connected)
        raise AndroidUseError(f"Multiple Android devices are connected. Pass one serial: {serials}")
    return str(connected[0]["serial"])


def shell(serial: str, command: str, timeout: int | float = DEFAULT_TIMEOUT) -> str:
    return decode_bytes(adb(["shell", command], serial=serial, timeout=timeout))


WEBVIEW_SOCKET_PATTERN = re.compile(r"@?([A-Za-z0-9_.-]*webview_devtools_remote[^\s]*)")
WEBVIEW_FORWARD_CACHE: dict[tuple[str, str], int] = {}
XIAOLUXUE_PAGE_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


def parse_webview_devtools_sockets(proc_net_unix: str) -> list[str]:
    sockets: list[str] = []
    seen: set[str] = set()
    for line in proc_net_unix.splitlines():
        match = WEBVIEW_SOCKET_PATTERN.search(line)
        if not match:
            continue
        socket_name = match.group(1).lstrip("@")
        if socket_name and socket_name not in seen:
            sockets.append(socket_name)
            seen.add(socket_name)
    return sockets


def webview_devtools_sockets(serial: str) -> list[str]:
    raw = shell(serial, "cat /proc/net/unix", timeout=8)
    return parse_webview_devtools_sockets(raw)


def webview_forward_alive(port: int) -> bool:
    try:
        read_json_url(local_json_url(port), timeout=0.2)
        return True
    except Exception:
        return False


def adb_forward_webview(serial: str, socket_name: str, port: int | None = None) -> int:
    normalized = socket_name.lstrip("@")
    cache_key = (serial, normalized)
    if port is None:
        cached_port = WEBVIEW_FORWARD_CACHE.get(cache_key)
        if cached_port and webview_forward_alive(cached_port):
            return cached_port
    local_port = int(port or pick_free_port("127.0.0.1"))
    adb(["forward", f"tcp:{local_port}", f"localabstract:{normalized}"], serial=serial, timeout=10)
    if port is None:
        WEBVIEW_FORWARD_CACHE[cache_key] = local_port
    return local_port


def local_json_url(port: int, path: str = "/json") -> str:
    return f"http://127.0.0.1:{port}{path}"


def read_json_url(url: str, timeout: int | float = 5) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise AndroidUseError(f"HTTP {exc.code} from {url}\n{body}") from exc
    except urllib.error.URLError as exc:
        raise AndroidUseError(f"Could not read {url}: {exc}") from exc


def parse_devtools_description(description: Any) -> dict[str, Any]:
    if not isinstance(description, str) or not description.strip():
        return {}
    try:
        value = json.loads(description)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def discover_webview_pages(serial: str, *, port: int | None = None) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    sockets = webview_devtools_sockets(serial)
    for index, socket_name in enumerate(sockets):
        try:
            local_port = adb_forward_webview(serial, socket_name, port if len(sockets) == 1 else None)
            targets = read_json_url(local_json_url(local_port), timeout=5)
            if not isinstance(targets, list):
                raise AndroidUseError(f"Unexpected /json payload for {socket_name}: {targets!r}")
            for target in targets:
                if not isinstance(target, dict):
                    continue
                description = parse_devtools_description(target.get("description"))
                pages.append(
                    {
                        **target,
                        "serial": serial,
                        "socket": socket_name,
                        "forward": {
                            "host": "127.0.0.1",
                            "port": local_port,
                            "jsonUrl": local_json_url(local_port),
                        },
                        "descriptionParsed": description,
                    }
                )
        except Exception as exc:
            pages.append({"serial": serial, "socket": socket_name, "socketIndex": index, "error": str(exc)})
    return pages


def webview_page_score(page: dict[str, Any]) -> tuple[int, int, int, int]:
    description = page.get("descriptionParsed") if isinstance(page.get("descriptionParsed"), dict) else {}
    return (
        1 if page.get("type") == "page" else 0,
        1 if description.get("visible") is True else 0,
        1 if description.get("attached") is True else 0,
        0 if description.get("empty") is True else 1,
    )


def select_webview_page(
    pages: list[dict[str, Any]],
    *,
    page_id: str | None = None,
    url_contains: str | None = None,
    title_contains: str | None = None,
) -> dict[str, Any]:
    candidates = [page for page in pages if not page.get("error")]
    if page_id:
        candidates = [page for page in candidates if str(page.get("id", "")) == page_id]
    if url_contains:
        candidates = [page for page in candidates if url_contains in str(page.get("url", ""))]
    if title_contains:
        candidates = [page for page in candidates if title_contains in str(page.get("title", ""))]
    if not candidates:
        errors = [page for page in pages if page.get("error")]
        detail = {"page_id": page_id, "url_contains": url_contains, "title_contains": title_contains, "errors": errors}
        raise AndroidUseError(f"No matching WebView page found: {json.dumps(detail, ensure_ascii=False)[:1200]}")
    candidates.sort(key=webview_page_score, reverse=True)
    page = candidates[0]
    if not page.get("webSocketDebuggerUrl"):
        raise AndroidUseError(f"Matched WebView page has no webSocketDebuggerUrl: {page}")
    return page


def recv_exact(sock: socket.socket, byte_count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = byte_count
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise AndroidUseError("WebSocket closed while reading data.")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def websocket_frame(opcode: int, payload: bytes = b"") -> bytes:
    first = 0x80 | (opcode & 0x0F)
    mask_key = os.urandom(4)
    length = len(payload)
    if length < 126:
        header = bytes([first, 0x80 | length])
    elif length < 65536:
        header = bytes([first, 0x80 | 126]) + struct.pack("!H", length)
    else:
        header = bytes([first, 0x80 | 127]) + struct.pack("!Q", length)
    masked = bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))
    return header + mask_key + masked


def websocket_send_text(sock: socket.socket, text: str) -> None:
    sock.sendall(websocket_frame(0x1, text.encode("utf-8")))


def websocket_send_pong(sock: socket.socket, payload: bytes) -> None:
    sock.sendall(websocket_frame(0xA, payload))


def websocket_recv_text(sock: socket.socket) -> str:
    fragments: list[bytes] = []
    while True:
        first, second = recv_exact(sock, 2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", recv_exact(sock, 8))[0]
        mask_key = recv_exact(sock, 4) if masked else b""
        payload = recv_exact(sock, length) if length else b""
        if masked:
            payload = bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))
        if opcode == 0x8:
            raise AndroidUseError("WebSocket closed by remote endpoint.")
        if opcode == 0x9:
            websocket_send_pong(sock, payload)
            continue
        if opcode == 0xA:
            continue
        if opcode in (0x1, 0x0):
            fragments.append(payload)
            if fin:
                return b"".join(fragments).decode("utf-8", errors="replace")


def websocket_connect(ws_url: str, timeout: int | float = 10) -> socket.socket:
    parsed = urllib.parse.urlparse(ws_url)
    if parsed.scheme != "ws":
        raise AndroidUseError(f"Only ws:// DevTools endpoints are supported: {ws_url}")
    host = parsed.hostname or "127.0.0.1"
    port = int(parsed.port or 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(request.encode("ascii"))
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            break
        response += chunk
        if len(response) > 16384:
            break
    status_line = response.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    if " 101 " not in status_line:
        sock.close()
        raise AndroidUseError(f"WebSocket handshake failed for {ws_url}: {status_line}")
    return sock


def cdp_call(ws_url: str, method: str, params: dict[str, Any] | None = None, timeout: int | float = 10) -> dict[str, Any]:
    sock = websocket_connect(ws_url, timeout=timeout)
    request_id = 1
    try:
        websocket_send_text(sock, json.dumps({"id": request_id, "method": method, "params": params or {}}))
        deadline = time.time() + float(timeout)
        while True:
            if time.time() > deadline:
                raise AndroidUseError(f"Timed out waiting for CDP response: {method}")
            message = json.loads(websocket_recv_text(sock))
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                raise AndroidUseError(f"CDP {method} failed: {message['error']}")
            result = message.get("result")
            return result if isinstance(result, dict) else {}
    finally:
        try:
            sock.sendall(websocket_frame(0x8, b""))
        except Exception:
            pass
        sock.close()


def cdp_runtime_evaluate(
    ws_url: str,
    expression: str,
    *,
    await_promise: bool = True,
    return_by_value: bool = True,
    timeout: int | float = 10,
) -> dict[str, Any]:
    result = cdp_call(
        ws_url,
        "Runtime.evaluate",
        {
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": return_by_value,
            "userGesture": True,
        },
        timeout=timeout,
    )
    if result.get("exceptionDetails"):
        raise AndroidUseError(f"Runtime.evaluate exception: {json.dumps(result['exceptionDetails'], ensure_ascii=False)[:1200]}")
    remote = result.get("result")
    if not isinstance(remote, dict):
        return {"raw": result}
    payload: dict[str, Any] = {"type": remote.get("type")}
    if "value" in remote:
        payload["value"] = remote.get("value")
    if "unserializableValue" in remote:
        payload["unserializableValue"] = remote.get("unserializableValue")
    if "description" in remote:
        payload["description"] = remote.get("description")
    return payload
