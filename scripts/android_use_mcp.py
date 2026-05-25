#!/usr/bin/env python3
"""MCP server for Android device control through adb and scrcpy.

The server intentionally has no third-party Python dependencies. It speaks the
newline-delimited JSON-RPC transport used by MCP stdio servers.
"""

from __future__ import annotations

import base64
import ast
import contextlib
import fcntl
import json
import os
import re
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
DEFAULT_TIMEOUT = 30
TMP_DIR = Path(os.environ.get("ANDROID_USE_TMP_DIR", "/tmp/android-use"))
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
LOCAL_PLATFORM_TOOLS = PLUGIN_ROOT / "tools" / "android-platform-tools" / "platform-tools"
ANDROID_USE_DIR = PLUGIN_ROOT / ".android-use"
RECORDINGS_DIR = ANDROID_USE_DIR / "recordings"
RECIPES_DIR = ANDROID_USE_DIR / "recipes"
SOURCE_MAP_DIR = ANDROID_USE_DIR / "app-maps"
SCREEN_DIR = PLUGIN_ROOT / ".screen"
OPENAI_BASE_URL = "https://api.openai.com/v1"

SCRCPY_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}
SCRCPY_LOCK_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}
SCREEN_VIEWER_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}
WEBRTC_VIEWER_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}
ACTIVE_RECORDINGS: dict[str, dict[str, Any]] = {}
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


def parse_adb_mdns_services(output: str, *, host: str | None = None) -> list[dict[str, Any]]:
    services: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if "_adb-tls-connect._tcp" not in line:
            continue
        endpoints = re.findall(r"((?:\d{1,3}\.){3}\d{1,3}|[A-Za-z0-9_.-]+):(\d+)", line)
        for endpoint_host, port_text in endpoints:
            if host and endpoint_host != host:
                continue
            services.append({"service": line, "host": endpoint_host, "port": int(port_text), "serial": f"{endpoint_host}:{port_text}"})
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
    env_host, env_port, env_serial = wireless_config_from_env()
    host = host or env_host
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
    if port:
        candidates.append((host, int(port)))
    for service in adb_mdns_connect_services(host):
        candidate = (str(service["host"]), int(service["port"]))
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
        raise AndroidUseError("No authorized Android device found. Run `adb devices -l` and authorize USB debugging.")
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


def get_prop(serial: str, prop: str) -> str | None:
    try:
        value = shell(serial, f"getprop {prop}", timeout=5).strip()
        return value or None
    except AndroidUseError:
        return None


def parse_screen_size(text: str) -> dict[str, int | None]:
    matches = re.findall(r"(?:Physical|Override) size:\s*(\d+)x(\d+)", text)
    if not matches:
        return {"width": None, "height": None}
    width, height = matches[-1]
    return {"width": int(width), "height": int(height)}


def get_screen_size(serial: str) -> dict[str, int | None]:
    try:
        return parse_screen_size(shell(serial, "wm size", timeout=5))
    except AndroidUseError:
        return {"width": None, "height": None}


def parse_bounds(bounds: str | None) -> dict[str, int] | None:
    if not bounds:
        return None
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds.strip())
    if not match:
        return None
    left, top, right, bottom = (int(part) for part in match.groups())
    return {"left": left, "top": top, "right": right, "bottom": bottom}


def bounds_center(bounds: dict[str, int] | None) -> dict[str, int] | None:
    if not bounds:
        return None
    return {
        "x": round((bounds["left"] + bounds["right"]) / 2),
        "y": round((bounds["top"] + bounds["bottom"]) / 2),
    }


def bool_attr(value: str | None) -> bool:
    return str(value or "").lower() == "true"


def extract_hierarchy_xml(raw: str) -> str:
    start = raw.find("<hierarchy")
    end = raw.rfind("</hierarchy>")
    if start < 0 or end < 0:
        raise AndroidUseError(f"uiautomator did not return hierarchy XML: {raw[:500]}")
    return raw[start : end + len("</hierarchy>")]


def dump_ui_xml(serial: str) -> str:
    try:
        adb(["shell", "uiautomator", "dump", REMOTE_UI_DUMP_PATH], serial=serial, timeout=12)
        raw = decode_bytes(adb(["exec-out", "cat", REMOTE_UI_DUMP_PATH], serial=serial, timeout=8))
        return extract_hierarchy_xml(raw)
    except AndroidUseError:
        # Some builds can write the hierarchy directly to stdout via /dev/tty.
        raw = shell(serial, "uiautomator dump /dev/tty", timeout=12)
        return extract_hierarchy_xml(raw)


def parse_ui_nodes(xml_text: str, *, limit: int = 300) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise AndroidUseError(f"Failed to parse uiautomator XML: {exc}") from exc

    nodes: list[dict[str, Any]] = []

    def walk(element: ET.Element, parent_click_target: dict[str, Any] | None = None, depth: int = 0) -> None:
        if len(nodes) >= limit:
            return
        attrs = element.attrib
        bounds = parse_bounds(attrs.get("bounds"))
        clickable = bool_attr(attrs.get("clickable"))
        long_clickable = bool_attr(attrs.get("long-clickable"))
        label_parts = [
            attrs.get("text") or "",
            attrs.get("content-desc") or "",
            attrs.get("resource-id") or "",
        ]
        click_target = None
        if bounds and (clickable or long_clickable):
            click_target = {"bounds": bounds, "center": bounds_center(bounds)}
        elif parent_click_target:
            click_target = parent_click_target
        node = {
            "index": len(nodes),
            "text": attrs.get("text") or "",
            "content_desc": attrs.get("content-desc") or "",
            "resource_id": attrs.get("resource-id") or "",
            "class": attrs.get("class") or "",
            "package": attrs.get("package") or "",
            "bounds": bounds,
            "center": bounds_center(bounds),
            "clickable": clickable,
            "enabled": bool_attr(attrs.get("enabled")),
            "checkable": bool_attr(attrs.get("checkable")),
            "checked": bool_attr(attrs.get("checked")),
            "selected": bool_attr(attrs.get("selected")),
            "focused": bool_attr(attrs.get("focused")),
            "long_clickable": long_clickable,
            "depth": depth,
        }
        if click_target:
            node["click_target"] = click_target
        if any(part.strip() for part in label_parts) or clickable or long_clickable:
            nodes.append(node)
        next_click_target = click_target or parent_click_target
        for child in element:
            walk(child, next_click_target, depth + 1)

    walk(root)
    return nodes


def observe_ui(serial: str, *, include_xml: bool = False, limit: int = 160) -> dict[str, Any]:
    xml_text = dump_ui_xml(serial)
    nodes = parse_ui_nodes(xml_text, limit=limit)
    observation: dict[str, Any] = {
        "state": device_state(serial),
        "ui": {
            "nodes": nodes,
            "count": len(nodes),
        },
    }
    if include_xml:
        observation["ui"]["xml"] = xml_text
    return observation


def node_labels(node: dict[str, Any]) -> list[str]:
    labels = []
    for key in ("text", "content_desc", "resource_id"):
        value = str(node.get(key) or "").strip()
        if value:
            labels.append(value)
    return labels


def node_click_point(node: dict[str, Any]) -> dict[str, int] | None:
    click_target = node.get("click_target") or {}
    center = click_target.get("center") if isinstance(click_target, dict) else None
    if center:
        return {"x": int(center["x"]), "y": int(center["y"])}
    center = node.get("center")
    if center:
        return {"x": int(center["x"]), "y": int(center["y"])}
    return None


def find_ui_node(
    nodes: list[dict[str, Any]],
    query: str,
    *,
    exact: bool = True,
    include_resource_id: bool = False,
) -> dict[str, Any] | None:
    needle = query.strip().casefold()
    if not needle:
        return None
    candidates: list[tuple[int, dict[str, Any]]] = []
    for node in nodes:
        labels = node_labels(node)
        if not include_resource_id:
            labels = labels[:2]
        for label in labels:
            haystack = label.strip().casefold()
            if not haystack:
                continue
            if haystack == needle:
                candidates.append((0, node))
                break
            if not exact and needle in haystack:
                candidates.append((1, node))
                break
            if not exact and haystack in needle and len(haystack) >= 2:
                candidates.append((2, node))
                break
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1].get("depth", 0)))
    return candidates[0][1]


def quoted_phrases(text: str) -> list[str]:
    phrases = re.findall(r"[“\"'‘「『《](.*?)[”\"'’」』》]", text)
    phrases.extend(re.findall(r"‘(.*?)’", text))
    return [phrase.strip() for phrase in phrases if phrase.strip()]


def fast_ui_action_from_instruction(serial: str, instruction: str) -> dict[str, Any] | None:
    lesson_action = xiaoluxue_lesson_fast_action_from_instruction(instruction)
    if lesson_action:
        focus = get_focused_window(serial) or ""
        if XIAOLUXUE_LESSON_ACTIVITY in focus:
            return lesson_action

    map_action = xiaoluxue_map_fast_action_from_instruction(instruction)
    if map_action:
        if map_action.get("subject_id"):
            return map_action
        focus = get_focused_window(serial) or ""
        if XIAOLUXUE_STUDY_SUBJECT_ACTIVITY in focus:
            return map_action

    observation = observe_ui(serial, limit=220)
    nodes = observation["ui"]["nodes"]
    labels: list[str] = []
    for node in nodes:
        for label in node_labels(node)[:2]:
            label = label.strip()
            if label and len(label) >= 1 and label not in labels:
                labels.append(label)

    candidates = quoted_phrases(instruction)
    candidates.extend(
        match.strip()
        for match in re.findall(r"(?:切换到|点击|点|进入|打开|选择|去|切到)\s*([^\s，。,.!！?？]+)", instruction)
        if match.strip()
    )
    for label in sorted(labels, key=len, reverse=True):
        if len(label) >= 2 and label in instruction:
            candidates.append(label)

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        node = find_ui_node(nodes, candidate, exact=True) or find_ui_node(nodes, candidate, exact=False)
        point = node_click_point(node) if node else None
        if point:
            return {
                "action": "tap",
                "x": point["x"],
                "y": point["y"],
                "source": "uiautomator",
                "target": candidate,
                "matched_node": {
                    key: node.get(key)
                    for key in ("index", "text", "content_desc", "resource_id", "class", "bounds", "clickable")
                },
            }
    return None


def get_focused_window(serial: str) -> str | None:
    try:
        dump = shell(serial, "dumpsys window", timeout=8)
    except AndroidUseError:
        return None
    patterns = [
        r"mCurrentFocus=Window\{[^ ]+ [^ ]+ ([^}]+)\}",
        r"mFocusedApp=ActivityRecord\{[^ ]+ [^ ]+ ([^}]+)\}",
    ]
    for pattern in patterns:
        match = re.search(pattern, dump)
        if match:
            return match.group(1)
    return None


def device_state(serial: str) -> dict[str, Any]:
    return {
        "serial": serial,
        "model": get_prop(serial, "ro.product.model"),
        "manufacturer": get_prop(serial, "ro.product.manufacturer"),
        "android_version": get_prop(serial, "ro.build.version.release"),
        "sdk": get_prop(serial, "ro.build.version.sdk"),
        "screen": get_screen_size(serial),
        "focused_window": get_focused_window(serial),
    }


def screenshot_png(serial: str) -> bytes:
    png = adb(["exec-out", "screencap", "-p"], serial=serial, timeout=20)
    if not png.startswith(b"\x89PNG"):
        fixed = png.replace(b"\r\n", b"\n")
        if fixed.startswith(b"\x89PNG"):
            png = fixed
    if not png.startswith(b"\x89PNG"):
        raise AndroidUseError("adb screencap did not return a PNG image.")
    return png


def png_size(png: bytes) -> dict[str, int | None]:
    if len(png) >= 24 and png.startswith(b"\x89PNG"):
        width, height = struct.unpack(">II", png[16:24])
        return {"width": int(width), "height": int(height)}
    return {"width": None, "height": None}


def screenshot_raw(serial: str) -> bytes:
    return adb(["exec-out", "screencap"], serial=serial, timeout=12)


def raw_screenshot_frame(raw: bytes) -> dict[str, Any]:
    if len(raw) < 16:
        return {"ok": False, "error": "raw screenshot too small"}
    width, height, pixel_format = struct.unpack("<III", raw[:12])
    if width <= 0 or height <= 0:
        return {"ok": False, "error": "invalid raw screenshot size"}
    expected_bytes = int(width) * int(height) * 4
    header_len = 16 if len(raw) >= 16 + expected_bytes else 12
    if len(raw) < header_len + expected_bytes:
        return {
            "ok": False,
            "width": int(width),
            "height": int(height),
            "format": int(pixel_format),
            "error": "raw screenshot payload truncated",
        }
    return {
        "ok": True,
        "width": int(width),
        "height": int(height),
        "format": int(pixel_format),
        "data_offset": header_len,
        "data": raw,
    }


def raw_screenshot_content_stats(raw: bytes) -> dict[str, Any]:
    frame = raw_screenshot_frame(raw)
    if not frame.get("ok"):
        return {"ready": False, **{key: value for key, value in frame.items() if key not in {"ok", "data"}}}
    width = int(frame["width"])
    height = int(frame["height"])
    pixel_format = int(frame["format"])
    data = frame["data"]
    data_offset = int(frame["data_offset"])
    x_step = max(int(width) // 80, 1)
    y_step = max(int(height) // 48, 1)
    y_start = min(max(int(height * 0.10), 0), int(height) - 1)
    y_end = min(max(int(height * 0.96), y_start + 1), int(height))
    sample_count = 0
    non_white_count = 0
    blue_count = 0
    dark_count = 0
    for y in range(y_start, y_end, y_step):
        row_offset = data_offset + y * int(width) * 4
        for x in range(0, int(width), x_step):
            offset = row_offset + x * 4
            r, g, b = data[offset], data[offset + 1], data[offset + 2]
            sample_count += 1
            if min(r, g, b) < 240:
                non_white_count += 1
            if b > 150 and g > 90 and r < 210:
                blue_count += 1
            if max(r, g, b) < 96:
                dark_count += 1

    if not sample_count:
        return {"ready": False, "error": "no screenshot samples"}
    non_white_ratio = non_white_count / sample_count
    blue_ratio = blue_count / sample_count
    dark_ratio = dark_count / sample_count
    ready = non_white_ratio >= 0.035 or blue_ratio >= 0.006 or dark_ratio >= 0.010
    return {
        "ready": bool(ready),
        "width": int(width),
        "height": int(height),
        "format": int(pixel_format),
        "samples": sample_count,
        "non_white_ratio": round(non_white_ratio, 4),
        "blue_ratio": round(blue_ratio, 4),
        "dark_ratio": round(dark_ratio, 4),
    }


def raw_screenshot_region_stats(frame: dict[str, Any], base_region: tuple[int, int, int, int]) -> dict[str, Any]:
    width = int(frame["width"])
    height = int(frame["height"])
    data = frame["data"]
    data_offset = int(frame["data_offset"])
    base_x1, base_y1, base_x2, base_y2 = base_region
    x1 = min(max(round(base_x1 * width / XIAOLUXUE_NATIVE_BASE_WIDTH), 0), width - 1)
    y1 = min(max(round(base_y1 * height / XIAOLUXUE_NATIVE_BASE_HEIGHT), 0), height - 1)
    x2 = min(max(round(base_x2 * width / XIAOLUXUE_NATIVE_BASE_WIDTH), x1 + 1), width)
    y2 = min(max(round(base_y2 * height / XIAOLUXUE_NATIVE_BASE_HEIGHT), y1 + 1), height)
    x_step = max((x2 - x1) // 56, 1)
    y_step = max((y2 - y1) // 28, 1)
    samples = 0
    dark = 0
    light = 0
    blue = 0
    for y in range(y1, y2, y_step):
        row_offset = data_offset + y * width * 4
        for x in range(x1, x2, x_step):
            offset = row_offset + x * 4
            r, g, b = data[offset], data[offset + 1], data[offset + 2]
            samples += 1
            if max(r, g, b) < 118:
                dark += 1
            if min(r, g, b) > 238:
                light += 1
            if b > 150 and g > 90 and r < 210:
                blue += 1
    if not samples:
        return {"samples": 0, "dark_ratio": 0.0, "light_ratio": 0.0, "blue_ratio": 0.0}
    return {
        "samples": samples,
        "dark_ratio": round(dark / samples, 4),
        "light_ratio": round(light / samples, 4),
        "blue_ratio": round(blue / samples, 4),
    }


def raw_screenshot_lesson_answer_stats(raw: bytes) -> dict[str, Any]:
    frame = raw_screenshot_frame(raw)
    if not frame.get("ok"):
        return {"ready": False, **{key: value for key, value in frame.items() if key not in {"ok", "data"}}}
    top_bar = raw_screenshot_region_stats(frame, (44, 52, 430, 135))
    answer_area = raw_screenshot_region_stats(frame, (1030, 140, 1950, 740))
    bottom_bar = raw_screenshot_region_stats(frame, (1520, 1020, 1960, 1168))
    ready = (
        top_bar["dark_ratio"] >= 0.006
        and answer_area["light_ratio"] >= 0.08
        and (answer_area["dark_ratio"] >= 0.004 or bottom_bar["blue_ratio"] >= 0.18)
    )
    return {
        "ready": bool(ready),
        "width": int(frame["width"]),
        "height": int(frame["height"]),
        "format": int(frame["format"]),
        "top_bar": top_bar,
        "answer_area": answer_area,
        "bottom_bar": bottom_bar,
    }


def raw_screenshot_lesson_card_list_stats(raw: bytes) -> dict[str, Any]:
    frame = raw_screenshot_frame(raw)
    if not frame.get("ok"):
        return {"ready": False, **{key: value for key, value in frame.items() if key not in {"ok", "data"}}}
    title_center = raw_screenshot_region_stats(frame, (760, 70, 1250, 130))
    middle_card = raw_screenshot_region_stats(frame, (610, 420, 1290, 990))
    middle_button = raw_screenshot_region_stats(frame, (650, 880, 1250, 980))
    right_options = raw_screenshot_region_stats(frame, (1030, 140, 1950, 740))
    ready = (
        title_center["dark_ratio"] >= 0.035
        and middle_card["light_ratio"] >= 0.25
        and middle_button["blue_ratio"] >= 0.20
        and right_options["dark_ratio"] <= 0.006
    )
    return {
        "ready": bool(ready),
        "width": int(frame["width"]),
        "height": int(frame["height"]),
        "format": int(frame["format"]),
        "title_center": title_center,
        "middle_card": middle_card,
        "middle_button": middle_button,
        "right_options": right_options,
    }


def save_png(serial: str, png: bytes, save_path: str | None = None) -> str:
    if save_path:
        path = Path(save_path).expanduser()
    else:
        safe_serial = re.sub(r"[^A-Za-z0-9_.-]+", "-", serial)
        path = TMP_DIR / f"{safe_serial}-{int(time.time() * 1000)}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return str(path)


def timestamp_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def slugify(value: str, default: str = "android") -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-._")
    return slug[:80] or default


def lock_path(name: str) -> Path:
    return SCREEN_DIR / f"{slugify(name, 'lock')}.lock"


def scrcpy_user_closed_path(serial: str) -> Path:
    return SCREEN_DIR / f"scrcpy-user-closed-{slugify(serial)}.json"


def read_scrcpy_user_closed(serial: str) -> dict[str, Any] | None:
    path = scrcpy_user_closed_path(serial)
    if not path.exists():
        return None
    payload = read_json_file(path)
    return payload or {"path": str(path)}


def clear_scrcpy_user_closed(serial: str) -> None:
    with contextlib.suppress(OSError):
        scrcpy_user_closed_path(serial).unlink()


@contextlib.contextmanager
def exclusive_file_lock(path: Path, *, blocking: bool = True) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    locked = False
    try:
        try:
            fcntl.flock(handle.fileno(), flags)
            locked = True
            yield True
        except BlockingIOError:
            yield False
    finally:
        if locked:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    tmp_path.replace(path)


def visible_labels(nodes: list[dict[str, Any]], limit: int = 24) -> list[str]:
    labels: list[str] = []
    for node in nodes:
        for key in ("text", "content_desc", "resource_id"):
            label = str(node.get(key) or "").strip()
            if label and label not in labels and len(label) <= 120:
                labels.append(label)
            if len(labels) >= limit:
                return labels
    return labels


def snapshot_fingerprint(snapshot: dict[str, Any]) -> dict[str, Any]:
    nodes = snapshot.get("ui", {}).get("nodes", [])
    return {
        "focused_window": snapshot.get("state", {}).get("focused_window"),
        "labels": visible_labels(nodes, limit=16) if isinstance(nodes, list) else [],
    }


def snapshot_from_observation(observation: dict[str, Any]) -> dict[str, Any]:
    snapshot = {
        "timestamp": timestamp_iso(),
        "state": observation.get("state", {}),
        "ui": observation.get("ui", {}),
    }
    snapshot["fingerprint"] = snapshot_fingerprint(snapshot)
    return snapshot


def capture_record_snapshot(
    serial: str,
    *,
    include_screenshot: bool = False,
    base_dir: Path | None = None,
    name: str = "snapshot",
) -> dict[str, Any]:
    try:
        snapshot = snapshot_from_observation(observe_ui(serial, include_xml=False, limit=320))
    except Exception as exc:
        snapshot = {"timestamp": timestamp_iso(), "error": str(exc)}
        try:
            snapshot["state"] = device_state(serial)
        except Exception:
            pass
        snapshot["fingerprint"] = snapshot_fingerprint(snapshot)
    if include_screenshot and base_dir is not None:
        try:
            png = screenshot_png(serial)
            screen_dir = base_dir / "screens"
            screen_dir.mkdir(parents=True, exist_ok=True)
            path = screen_dir / f"{slugify(name)}.png"
            path.write_bytes(png)
            snapshot["screenshot_path"] = str(path)
            snapshot["screenshot_size"] = png_size(png)
        except Exception as exc:
            snapshot["screenshot_error"] = str(exc)
    return snapshot


def compact_node(node: dict[str, Any] | None) -> dict[str, Any] | None:
    if not node:
        return None
    return {
        key: node.get(key)
        for key in ("index", "text", "content_desc", "resource_id", "class", "bounds", "center", "clickable")
        if node.get(key) not in (None, "")
    }


def selector_candidates_for_node(node: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not node:
        return []
    selectors: list[dict[str, Any]] = []
    resource_id = str(node.get("resource_id") or "").strip()
    content_desc = str(node.get("content_desc") or "").strip()
    text = str(node.get("text") or "").strip()
    if resource_id:
        selectors.append({"strategy": "resource_id", "value": resource_id})
    if content_desc:
        selectors.append({"strategy": "content_desc", "value": content_desc})
    if text:
        selectors.append({"strategy": "text", "value": text})
    return selectors


def point_in_bounds(bounds: dict[str, int] | None, x: int, y: int) -> bool:
    if not bounds:
        return False
    return bounds["left"] <= x <= bounds["right"] and bounds["top"] <= y <= bounds["bottom"]


def node_at_point(nodes: list[dict[str, Any]], x: int, y: int) -> dict[str, Any] | None:
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for node in nodes:
        bounds = node.get("bounds")
        if isinstance(bounds, dict) and point_in_bounds(bounds, x, y):
            area = max(1, (bounds["right"] - bounds["left"]) * (bounds["bottom"] - bounds["top"]))
            candidates.append((area, -int(node.get("depth", 0)), node))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def find_node_by_selector(nodes: list[dict[str, Any]], selector: dict[str, Any]) -> dict[str, Any] | None:
    strategy = str(selector.get("strategy", "")).strip()
    value = str(selector.get("value", "")).strip()
    if not strategy or not value:
        return None
    if strategy == "resource_id":
        for node in nodes:
            if str(node.get("resource_id") or "").strip() == value:
                return node
        return None
    if strategy == "content_desc":
        for node in nodes:
            if str(node.get("content_desc") or "").strip() == value:
                return node
        return None
    if strategy == "text":
        for node in nodes:
            if str(node.get("text") or "").strip() == value:
                return node
        return None
    if strategy == "text_contains":
        return find_ui_node(nodes, value, exact=False)
    return None


def coordinate_from_target(serial: str, target: dict[str, Any]) -> dict[str, int] | None:
    coordinate = target.get("coordinate")
    if not isinstance(coordinate, dict):
        return None
    x = coordinate.get("x")
    y = coordinate.get("y")
    if x is None or y is None:
        return None
    source_screen = coordinate.get("screen") if isinstance(coordinate.get("screen"), dict) else {}
    source_width = int(source_screen.get("width") or 0)
    source_height = int(source_screen.get("height") or 0)
    current = get_screen_size(serial)
    current_width = int(current.get("width") or 0)
    current_height = int(current.get("height") or 0)
    if source_width > 0 and source_height > 0 and current_width > 0 and current_height > 0:
        return {
            "x": round(int(x) * current_width / source_width),
            "y": round(int(y) * current_height / source_height),
        }
    return {"x": int(x), "y": int(y)}


def resolve_target_point(serial: str, target: dict[str, Any]) -> tuple[dict[str, int], dict[str, Any] | None]:
    observation = observe_ui(serial, limit=320)
    nodes = observation["ui"]["nodes"]
    selectors = target.get("selectors") if isinstance(target.get("selectors"), list) else []
    for selector in selectors:
        if isinstance(selector, dict):
            node = find_node_by_selector(nodes, selector)
            point = node_click_point(node) if node else None
            if point:
                return point, node
    point = coordinate_from_target(serial, target)
    if point:
        return point, None
    raise AndroidUseError(f"Could not resolve recipe target: {target}")


def action_target_from_record(record: dict[str, Any]) -> dict[str, Any]:
    args = record.get("arguments", {}) if isinstance(record.get("arguments"), dict) else {}
    result = record.get("result", {}) if isinstance(record.get("result"), dict) else {}
    before = record.get("before", {}) if isinstance(record.get("before"), dict) else {}
    nodes = before.get("ui", {}).get("nodes", [])
    matched_node = result.get("matched_node") if isinstance(result.get("matched_node"), dict) else None
    x = result.get("x", args.get("x"))
    y = result.get("y", args.get("y"))
    if matched_node is None and isinstance(nodes, list) and x is not None and y is not None:
        matched_node = node_at_point(nodes, int(x), int(y))
    screen = before.get("state", {}).get("screen") if isinstance(before.get("state"), dict) else {}
    target: dict[str, Any] = {
        "selectors": selector_candidates_for_node(matched_node),
        "matched_node": compact_node(matched_node),
    }
    if x is not None and y is not None:
        target["coordinate"] = {"x": int(x), "y": int(y), "screen": screen or {}}
    return target


def verify_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    fingerprint = snapshot.get("fingerprint") if isinstance(snapshot.get("fingerprint"), dict) else {}
    focused_window = fingerprint.get("focused_window")
    labels = fingerprint.get("labels") or []
    human_labels = [
        str(label)
        for label in labels
        if label and ":id/" not in str(label) and "/" not in str(label) and len(str(label)) <= 80
    ]
    return {
        "focused_window": focused_window,
        "labels_any": [] if focused_window else human_labels[:6],
    }


def recipe_from_trace(trace: dict[str, Any], recipe_name: str | None = None) -> dict[str, Any]:
    name = recipe_name or str(trace.get("name") or "android-recipe")
    recipe: dict[str, Any] = {
        "schema_version": 1,
        "name": name,
        "created_at": timestamp_iso(),
        "source_trace_id": trace.get("id"),
        "serial": trace.get("serial"),
        "steps": [],
    }
    for record in trace.get("steps", []):
        if not isinstance(record, dict) or record.get("kind") != "action":
            continue
        action = str(record.get("action", "")).strip()
        args = record.get("arguments", {}) if isinstance(record.get("arguments"), dict) else {}
        step: dict[str, Any] = {"action": action}
        if action in {"tap", "tap_text"}:
            step["target"] = action_target_from_record(record)
            if action == "tap_text" and args.get("text"):
                step["text"] = args.get("text")
        elif action == "swipe":
            step.update({key: args[key] for key in ("start_x", "start_y", "end_x", "end_y") if key in args})
            step["duration_ms"] = int(args.get("duration_ms", 300))
            before = record.get("before", {}) if isinstance(record.get("before"), dict) else {}
            step["screen"] = before.get("state", {}).get("screen", {})
        elif action == "type_text":
            if args.get("text_redacted"):
                step["text_redacted"] = True
            else:
                step["text"] = args.get("text", "")
            step["clear_first"] = bool(args.get("clear_first", False))
            step["clear_count"] = int(args.get("clear_count", 80))
            step["enter"] = bool(args.get("enter", False))
        elif action == "press_key":
            step["key"] = args.get("key")
        elif action == "open_url":
            step["url"] = args.get("url")
        elif action == "open_app":
            step["package"] = args.get("package")
            if args.get("activity"):
                step["activity"] = args.get("activity")
        elif action == "wake_unlock":
            step["dismiss_keyguard"] = bool(args.get("dismiss_keyguard", True))
        elif action == "xiaoluxue_set_speed":
            step["rate"] = float(args.get("rate", 2.0))
        elif action == "xiaoluxue_goto_widget":
            step["index"] = int(args.get("index", 0))
            step["mode"] = str(args.get("mode", "reload"))
        elif action == "xiaoluxue_course_fast_path":
            step.update(
                {
                    key: args[key]
                    for key in (
                        "guide_index",
                        "guide_name_contains",
                        "set_speed",
                        "rate",
                        "target_index",
                        "target_name_contains",
                        "target_last",
                        "target_mode",
                    )
                    if key in args
                }
            )
        elif action == "xiaoluxue_map_fast_path":
            step.update(
                {
                    key: args[key]
                    for key in (
                        "index",
                        "subject_id",
                        "subject",
                        "action_name",
                        "instruction",
                        "route_if_subject",
                        "route_wait_sec",
                        "close_progress_popup",
                        "close_progress_wait_sec",
                        "close_progress_taps",
                        "prefer_predicted",
                        "open_report_when_done",
                        "enter_direct_practice",
                    )
                    if key in args
                }
            )
        elif action == "xiaoluxue_lesson_fast_path":
            step.update(
                {
                    key: args[key]
                    for key in (
                        "action_name",
                        "instruction",
                        "direct_practice_wait_sec",
                        "lesson_focus_timeout_sec",
                        "after_direct_practice_wait_sec",
                        "answer_ready_timeout_sec",
                        "answer_ready_poll_sec",
                        "tap_direct_practice_until_answer_ready",
                        "direct_practice_tap_interval_sec",
                        "answer_ready_poll_after_taps",
                        "after_continue_wait_sec",
                        "after_finish_wait_sec",
                        "min_answer_ready_after_continue_sec",
                        "tap_card_direct_practice_if_needed",
                        "card_direct_practice_taps",
                        "card_direct_practice_interval_sec",
                        "transition_skip_taps",
                        "disable_system_animations",
                        "restore_system_animations",
                    )
                    if key in args
                }
            )
        elif action == "xiaoluxue_open_native_subject":
            step.update(
                {
                    key: args[key]
                    for key in (
                        "subject_id",
                        "subject",
                        "textbook_id",
                        "chapter_id",
                        "knowledge_id",
                        "go_next_knowledge",
                        "route_wait_sec",
                        "close_progress_popup",
                        "close_progress_wait_sec",
                        "close_progress_taps",
                    )
                    if key in args
                }
            )
        elif action == "xiaoluxue_open_knowledge_guide":
            step.update(
                {
                    key: args[key]
                    for key in (
                        "subject_id",
                        "knowledge_index",
                        "knowledge_id",
                        "guide_widget_index",
                        "rate",
                        "prefer_client_route",
                        "use_shortcut_url",
                        "refresh_session",
                    )
                    if key in args
                }
            )
        elif action == "xiaoluxue_login_fast_path":
            step.update({key: args[key] for key in ("account", "account_chars", "password_chars", "password_redacted", "timeout_sec") if key in args})
        elif action == "xiaoluxue_switch_env":
            step.update(
                {
                    key: args[key]
                    for key in (
                        "env",
                        "open_student",
                        "force_submit",
                        "force_stop_student",
                        "timeout_sec",
                    )
                    if key in args
                }
            )
        elif action == "xiaoluxue_exercise_action":
            step.update(
                {
                    key: args[key]
                    for key in ("action_name", "option_key", "option_index", "option_text", "answer_text", "button_text")
                    if key in args
                }
            )
        elif action == "xiaoluxue_exercise_fast_path":
            step.update(
                {
                    key: args[key]
                    for key in (
                        "option_key",
                        "option_index",
                        "option_text",
                        "answer_text",
                        "submit",
                        "continue_after_submit",
                        "action_name",
                        "button_text",
                        "after_action_wait_sec",
                        "max_steps",
                        "step_wait_sec",
                        "click_report",
                    )
                    if key in args
                }
            )
        else:
            continue
        after = record.get("after") if isinstance(record.get("after"), dict) else {}
        if after:
            step["verify"] = verify_from_snapshot(after)
        recipe["steps"].append(step)
    return recipe


def replay_coordinate(step: dict[str, Any], key: str, serial: str) -> int:
    value = int(step[key])
    source_screen = step.get("screen") if isinstance(step.get("screen"), dict) else {}
    source_width = int(source_screen.get("width") or 0)
    source_height = int(source_screen.get("height") or 0)
    current = get_screen_size(serial)
    current_width = int(current.get("width") or 0)
    current_height = int(current.get("height") or 0)
    if key.endswith("_x") and source_width > 0 and current_width > 0:
        return round(value * current_width / source_width)
    if key.endswith("_y") and source_height > 0 and current_height > 0:
        return round(value * current_height / source_height)
    return value


def execute_recipe_step(serial: str, step: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    action = str(step.get("action", "")).strip()
    if dry_run:
        return {"ok": True, "action": action, "dry_run": True}
    if action in {"tap", "tap_text"}:
        point, node = resolve_target_point(serial, step.get("target", {}))
        adb(["shell", "input", "tap", str(point["x"]), str(point["y"])], serial=serial, timeout=10)
        return {"ok": True, "action": action, "point": point, "matched_node": compact_node(node)}
    if action == "swipe":
        start_x = replay_coordinate(step, "start_x", serial)
        start_y = replay_coordinate(step, "start_y", serial)
        end_x = replay_coordinate(step, "end_x", serial)
        end_y = replay_coordinate(step, "end_y", serial)
        duration_ms = int(step.get("duration_ms", 300))
        adb(
            [
                "shell",
                "input",
                "swipe",
                str(start_x),
                str(start_y),
                str(end_x),
                str(end_y),
                str(duration_ms),
            ],
            serial=serial,
            timeout=10,
        )
        return {"ok": True, "action": "swipe"}
    if action == "type_text":
        if step.get("text_redacted"):
            raise AndroidUseError("Recipe text is redacted; edit the recipe and provide `text` before replay.")
        text = str(step.get("text", ""))
        method = type_focused_text_fast(
            serial,
            text,
            clear_first=bool(step.get("clear_first")),
            clear_count=int(step.get("clear_count", 80)),
            enter=bool(step.get("enter")),
        )
        return {"ok": True, "action": "type_text", "chars": len(text), "method": method}
    if action == "press_key":
        key = keycode(step["key"])
        adb(["shell", "input", "keyevent", key], serial=serial, timeout=10)
        return {"ok": True, "action": "press_key", "key": key}
    if action == "open_url":
        adb(["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", str(step["url"])], serial=serial, timeout=15)
        return {"ok": True, "action": "open_url", "url": step["url"]}
    if action == "open_app":
        package = str(step["package"])
        activity = str(step.get("activity", "")).strip()
        if activity:
            component = activity if "/" in activity else f"{package}/{activity}"
            adb(["shell", "am", "start", "-n", component], serial=serial, timeout=15)
        else:
            adb(["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"], serial=serial, timeout=15)
        return {"ok": True, "action": "open_app", "package": package, "activity": activity or None}
    if action == "wake_unlock":
        adb(["shell", "input", "keyevent", "KEYCODE_WAKEUP"], serial=serial, timeout=10)
        if step.get("dismiss_keyguard", True):
            adb(["shell", "wm", "dismiss-keyguard"], serial=serial, timeout=10)
        return {"ok": True, "action": "wake_unlock"}
    if action == "xiaoluxue_set_speed":
        page = xiaoluxue_page(serial)
        rate = float(step.get("rate", 2.0))
        result = cdp_eval_value(page, xiaoluxue_set_speed_expression(rate), timeout=15)
        return {"ok": True, "action": "xiaoluxue_set_speed", "rate": rate, "result": result}
    if action == "xiaoluxue_goto_widget":
        page = xiaoluxue_page(serial)
        index = int(step.get("index", 0))
        mode = str(step.get("mode", "reload"))
        result = cdp_eval_value(page, xiaoluxue_goto_widget_expression(index, mode), timeout=15)
        return {"ok": True, "action": "xiaoluxue_goto_widget", "index": index, "mode": mode, "result": result}
    if action == "xiaoluxue_course_fast_path":
        return run_xiaoluxue_course_fast_path(serial, step, record=False)
    if action == "xiaoluxue_open_native_subject":
        return run_xiaoluxue_open_native_subject(serial, step, record=False)
    if action == "xiaoluxue_map_fast_path":
        return run_xiaoluxue_map_fast_path(serial, step, record=False)
    if action == "xiaoluxue_lesson_fast_path":
        return run_xiaoluxue_lesson_fast_path(serial, step, record=False)
    if action == "xiaoluxue_open_knowledge_guide":
        return run_xiaoluxue_open_knowledge_guide(serial, step, record=False)
    if action == "xiaoluxue_login_fast_path":
        if step.get("password_redacted") and not step.get("password"):
            raise AndroidUseError("Recipe password is redacted; edit the recipe and provide `password` before replay.")
        return run_xiaoluxue_login_fast_path(serial, step, record=False)
    if action == "xiaoluxue_switch_env":
        return run_xiaoluxue_switch_env(serial, step, record=False)
    if action == "xiaoluxue_exercise_action":
        page = xiaoluxue_exercise_page(serial)
        result = cdp_eval_value(page, xiaoluxue_exercise_action_expression(step), timeout=15)
        return {"ok": True, "action": "xiaoluxue_exercise_action", "result": result}
    if action == "xiaoluxue_exercise_fast_path":
        return run_xiaoluxue_exercise_fast_path(serial, step, record=False)
    raise AndroidUseError(f"Unsupported recipe action: {action}")


def verify_recipe_step(serial: str, step: dict[str, Any]) -> dict[str, Any]:
    verify = step.get("verify") if isinstance(step.get("verify"), dict) else {}
    if not verify:
        return {"checked": False, "ok": True}
    snapshot = capture_record_snapshot(serial)
    fingerprint = snapshot.get("fingerprint", {})
    current_labels = set(fingerprint.get("labels") or [])
    expected_labels = [label for label in verify.get("labels_any", []) if label]
    matched_labels = [label for label in expected_labels if label in current_labels]
    expected_window = verify.get("focused_window")
    current_window = fingerprint.get("focused_window")
    window_ok = not expected_window or expected_window == current_window
    labels_ok = not expected_labels or bool(matched_labels)
    return {
        "checked": True,
        "ok": bool(window_ok and labels_ok),
        "window_ok": window_ok,
        "labels_ok": labels_ok,
        "matched_labels": matched_labels,
        "expected_window": expected_window,
        "current_window": current_window,
    }


def resolve_json_path(value: str, directory: Path, suffix: str = ".json") -> Path:
    path = Path(value).expanduser()
    if path.exists():
        return path
    candidate = directory / (value if value.endswith(suffix) else f"{value}{suffix}")
    if candidate.exists():
        return candidate
    raise AndroidUseError(f"JSON file not found: {value}")


SOURCE_EXTENSIONS = {".kt", ".java", ".xml", ".tsx", ".jsx", ".ts", ".js", ".dart"}


def extract_source_entry(path: Path, root: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return None
    rel = str(path.relative_to(root))
    entry: dict[str, Any] = {"file": rel, "activities": [], "routes": [], "controls": []}
    for match in re.finditer(r"class\s+([A-Za-z0-9_]*Activity)\b", text):
        entry["activities"].append(match.group(1))
    for match in re.finditer(r"<activity[^>]+android:name=[\"']([^\"']+)[\"']", text):
        entry["activities"].append(match.group(1))
    for pattern in (r"composable\s*\(\s*[\"']([^\"']+)[\"']", r"route\s*=\s*[\"']([^\"']+)[\"']", r"android:path(?:Prefix|Pattern)?=[\"']([^\"']+)[\"']"):
        for match in re.finditer(pattern, text):
            entry["routes"].append(match.group(1))
    controls: list[dict[str, Any]] = []
    for match in re.finditer(r"android:id=[\"']@\+?id/([^\"']+)[\"']", text):
        controls.append({"kind": "id", "value": match.group(1)})
    for match in re.finditer(r"\bR\.id\.([A-Za-z0-9_]+)", text):
        controls.append({"kind": "id", "value": match.group(1)})
    for match in re.finditer(r"android:(?:text|hint|contentDescription)=[\"']([^\"']{1,120})[\"']", text):
        controls.append({"kind": "label", "value": match.group(1)})
    for match in re.finditer(r"\b(?:Text|Button|ClickableText)\s*\(\s*[\"']([^\"']{1,120})[\"']", text):
        controls.append({"kind": "label", "value": match.group(1)})
    for match in re.finditer(r"\b(?:contentDescription|testTag)\s*=\s*[\"']([^\"']{1,120})[\"']", text):
        controls.append({"kind": "semantic", "value": match.group(1)})
    for match in re.finditer(r"\b(?:testTag|contentDescription)\s*\(\s*[\"']([^\"']{1,120})[\"']\s*\)", text):
        controls.append({"kind": "semantic", "value": match.group(1)})
    seen_controls: set[tuple[str, str]] = set()
    for control in controls:
        key = (control["kind"], control["value"])
        if key not in seen_controls:
            entry["controls"].append(control)
            seen_controls.add(key)
    for key in ("activities", "routes"):
        entry[key] = sorted(set(entry[key]))
    if entry["activities"] or entry["routes"] or entry["controls"]:
        return entry
    return None


def index_source_tree(root: Path, *, max_files: int = 2000) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if not root.exists():
        raise AndroidUseError(f"source_path does not exist: {root}")
    if root.is_file():
        files = [root]
        scan_root = root.parent
    else:
        scan_root = root
        files = []
        for path in root.rglob("*"):
            if len(files) >= max_files:
                break
            if path.is_file() and path.suffix in SOURCE_EXTENSIONS:
                if any(part in {".git", "build", ".gradle", "node_modules", "Pods"} for part in path.parts):
                    continue
                files.append(path)
    entries = [entry for file_path in files if (entry := extract_source_entry(file_path, scan_root))]
    controls: list[dict[str, Any]] = []
    seen_controls: set[tuple[str, str]] = set()
    for entry in entries:
        for control in entry.get("controls", []):
            key = (control["kind"], control["value"])
            if key not in seen_controls:
                controls.append(control)
                seen_controls.add(key)
    return {
        "schema_version": 1,
        "generated_at": timestamp_iso(),
        "root": str(root),
        "files_scanned": len(files),
        "files_indexed": len(entries),
        "pages": entries,
        "controls": controls,
    }


def active_recording(serial: str) -> dict[str, Any] | None:
    return ACTIVE_RECORDINGS.get(serial)


def sanitize_action_arguments(action: str, args: dict[str, Any], recording: dict[str, Any]) -> dict[str, Any]:
    sanitized = {key: value for key, value in args.items() if key != "serial"}
    if action == "type_text" and recording.get("redact_text"):
        text = str(sanitized.pop("text", ""))
        sanitized["chars"] = len(text)
        sanitized["text_redacted"] = True
    return sanitized


def append_recording_step(
    serial: str,
    action: str,
    args: dict[str, Any],
    result: dict[str, Any],
    *,
    before: dict[str, Any] | None = None,
) -> None:
    recording = active_recording(serial)
    if not recording:
        return
    try:
        index = len(recording["steps"]) + 1
        if before is None:
            before = capture_record_snapshot(
                serial,
                include_screenshot=bool(recording.get("include_screenshots")),
                base_dir=Path(recording["dir"]),
                name=f"{index:03d}-before-{action}",
            )
        delay = float(recording.get("after_delay_sec", 0.25))
        if delay > 0:
            time.sleep(min(delay, 2.0))
        after = capture_record_snapshot(
            serial,
            include_screenshot=bool(recording.get("include_screenshots")),
            base_dir=Path(recording["dir"]),
            name=f"{index:03d}-after-{action}",
        )
        recording["steps"].append(
            {
                "kind": "action",
                "index": index,
                "timestamp": timestamp_iso(),
                "action": action,
                "arguments": sanitize_action_arguments(action, args, recording),
                "result": result,
                "before": before,
                "after": after,
            }
        )
    except Exception as exc:
        recording.setdefault("errors", []).append({"timestamp": timestamp_iso(), "action": action, "error": str(exc)})


def append_recording_checkpoint(serial: str, label: str) -> dict[str, Any]:
    recording = active_recording(serial)
    if not recording:
        raise AndroidUseError("No active recording for this device. Start one with android_start_recording first.")
    index = len(recording["steps"]) + 1
    snapshot = capture_record_snapshot(
        serial,
        include_screenshot=bool(recording.get("include_screenshots")),
        base_dir=Path(recording["dir"]),
        name=f"{index:03d}-checkpoint-{label}",
    )
    step = {
        "kind": "checkpoint",
        "index": index,
        "timestamp": timestamp_iso(),
        "label": label,
        "snapshot": snapshot,
    }
    recording["steps"].append(step)
    return step


KEY_ALIASES = {
    "BACK": "KEYCODE_BACK",
    "HOME": "KEYCODE_HOME",
    "ENTER": "KEYCODE_ENTER",
    "MENU": "KEYCODE_MENU",
    "POWER": "KEYCODE_POWER",
    "RECENTS": "KEYCODE_APP_SWITCH",
    "APP_SWITCH": "KEYCODE_APP_SWITCH",
    "VOLUME_UP": "KEYCODE_VOLUME_UP",
    "VOLUME_DOWN": "KEYCODE_VOLUME_DOWN",
    "DEL": "KEYCODE_DEL",
    "DELETE": "KEYCODE_DEL",
    "TAB": "KEYCODE_TAB",
    "ESCAPE": "KEYCODE_ESCAPE",
}


def keycode(value: str | int) -> str:
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    if text.isdigit():
        return text
    upper = text.upper().replace("KEYCODE_", "")
    return KEY_ALIASES.get(upper, f"KEYCODE_{upper}")


def escape_input_text(text: str) -> str:
    escaped = text.replace("%", "%25").replace(" ", "%s")
    for char in ['"', "'", "\\", "&", "<", ">", "(", ")", "|", ";", "*", "~", "`"]:
        escaped = escaped.replace(char, "\\" + char)
    return escaped


def current_input_method(serial: str) -> str:
    try:
        return shell(serial, "settings get secure default_input_method", timeout=3).strip()
    except AndroidUseError:
        return ""


def input_method_looks_like_adb_keyboard(value: str) -> bool:
    normalized = value.casefold()
    return "adbkeyboard" in normalized or "adb_keyboard" in normalized or "adbime" in normalized


def fast_input_ime_enabled() -> bool:
    return env_flag("ANDROID_USE_FAST_INPUT_IME", True)


def restore_ime_after_fast_input() -> bool:
    return env_flag("ANDROID_USE_RESTORE_IME_AFTER_TYPE", False)


def fast_input_min_chars() -> int:
    raw = os.environ.get("ANDROID_USE_FAST_INPUT_MIN_CHARS", "24")
    try:
        return max(0, int(raw))
    except ValueError:
        return 24


def text_needs_unicode_input(text: str) -> bool:
    return any(ord(char) > 0x7F for char in text)


def should_try_fast_ime(text: str, *, clear_first: bool = False) -> bool:
    if not fast_input_ime_enabled():
        return False
    if not text and not clear_first:
        return False
    return text_needs_unicode_input(text) or "\n" in text or len(text) >= fast_input_min_chars()


def webview_direct_input_enabled() -> bool:
    return env_flag("ANDROID_USE_WEBVIEW_DIRECT_INPUT", True)


def webview_direct_input_expression(
    text: str,
    *,
    clear_first: bool = False,
    enter: bool = False,
    prefer_answer_box: bool = False,
) -> str:
    config_json = json.dumps(
        {
            "text": text,
            "clearFirst": clear_first,
            "enter": enter,
            "preferAnswerBox": prefer_answer_box,
        },
        ensure_ascii=False,
    )
    return r"""
(async () => {
  const config = __CONFIG__;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const rect = (el) => {
    const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, width: r.width, height: r.height, top: r.top, right: r.right, bottom: r.bottom, left: r.left };
  };
  const visible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && style.visibility !== "hidden" && style.display !== "none";
  };
  const textOf = (el) => (el?.innerText || el?.textContent || "").trim().replace(/\s+/g, " ");
  const editable = (el) => {
    if (!el) return false;
    const tag = String(el.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || el.isContentEditable || el.getAttribute?.("contenteditable") === "true";
  };
  const reactFiber = (el) => {
    if (!el) return null;
    const key = Object.keys(el).find((key) => key.startsWith("__reactFiber$") || key.startsWith("__reactInternalInstance$"));
    return key ? el[key] : null;
  };
  const findReactFunctionProp = (root, propName) => {
    const seen = new Set();
    const elements = [];
    if (root) elements.push(root);
    if (root?.querySelectorAll) elements.push(...root.querySelectorAll("*"));
    elements.push(document.activeElement);
    for (const startEl of elements.filter(Boolean)) {
      let fiber = reactFiber(startEl);
      let depth = 0;
      while (fiber && depth < 50) {
        if (!seen.has(fiber)) {
          seen.add(fiber);
          const props = fiber.memoizedProps || fiber.pendingProps || {};
          if (typeof props[propName] === "function") return { fiber, props, element: startEl };
        }
        fiber = fiber.return;
        depth += 1;
      }
    }
    return null;
  };
  const setNativeValue = (el, value) => {
    if (el.isContentEditable || el.getAttribute?.("contenteditable") === "true") {
      el.focus();
      el.textContent = value;
      el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return;
    }
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
    el.focus();
    if (descriptor?.set) descriptor.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  };
  const dispatchEnter = (el) => {
    if (!config.enter || !el) return;
    for (const type of ["keydown", "keypress", "keyup"]) {
      el.dispatchEvent(new KeyboardEvent(type, { bubbles: true, cancelable: true, key: "Enter", code: "Enter", keyCode: 13, which: 13 }));
    }
  };
  const answerRoots = [
    ...document.querySelectorAll(".math-field-answer-box"),
    ...document.querySelectorAll("[class*='math-field'][class*='answer']"),
    ...document.querySelectorAll("[class*='answer-box']"),
  ].filter(visible);
  const active = document.activeElement;
  const activeEditable = editable(active) ? active : null;
  const activeRoot = active?.closest?.(".math-field-answer-box,[class*='math-field'][class*='answer'],[class*='answer-box']") || null;
  const answerRoot = (activeRoot && visible(activeRoot)) ? activeRoot : answerRoots[0];
  const root = config.preferAnswerBox ? (answerRoot || activeEditable) : (activeEditable || answerRoot);
  if (!root) {
    return { ok: false, reason: "no focused DOM input or visible answer box", activeTag: active?.tagName || "" };
  }

  const latexTarget = findReactFunctionProp(root, "onLatexChange");
  if (latexTarget) {
    const current = typeof latexTarget.props.content === "string" ? latexTarget.props.content : "";
    const value = config.clearFirst ? String(config.text) : current + String(config.text);
    latexTarget.props.onLatexChange(value);
    dispatchEnter(root);
    await sleep(80);
    const box = answerRoot || root;
    return { ok: true, method: "react_onLatexChange", target: "math_answer_box", chars: String(config.text).length, value, renderedText: textOf(box).slice(0, 500), rect: box ? rect(box) : null };
  }

  const field = editable(root) ? root : root.querySelector?.("textarea,input,[contenteditable='true']");
  if (field) {
    const current = field.isContentEditable ? textOf(field) : String(field.value || "");
    const value = config.clearFirst ? String(config.text) : current + String(config.text);
    setNativeValue(field, value);
    dispatchEnter(field);
    await sleep(80);
    return { ok: true, method: "dom_value", target: String(field.tagName || "").toLowerCase(), chars: String(config.text).length, value, renderedText: textOf(root).slice(0, 500), rect: rect(field) };
  }

  const changeTarget = findReactFunctionProp(root, "onChange");
  if (changeTarget) {
    const value = String(config.text);
    changeTarget.props.onChange({ target: { value }, currentTarget: { value } });
    dispatchEnter(root);
    await sleep(80);
    return { ok: true, method: "react_onChange", target: "react_input", chars: value.length, value, renderedText: textOf(root).slice(0, 500), rect: rect(root) };
  }
  return { ok: false, reason: "DOM input target not writable", activeTag: active?.tagName || "", rootText: textOf(root).slice(0, 120) };
})()
""".replace("__CONFIG__", config_json)


def candidate_webview_pages_for_input(serial: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in (
        XIAOLUXUE_PAGE_CACHE.get((serial, "exercise")),
        XIAOLUXUE_PAGE_CACHE.get((serial, "course")),
        XIAOLUXUE_PAGE_CACHE.get((serial, "any")),
    ):
        if page and page.get("webSocketDebuggerUrl"):
            key = str(page.get("id") or page.get("webSocketDebuggerUrl") or "")
            if key not in seen:
                candidates.append(page)
                seen.add(key)
    try:
        pages = discover_webview_pages(serial)
        page = select_webview_page(pages)
        key = str(page.get("id") or page.get("webSocketDebuggerUrl") or "")
        if key not in seen:
            candidates.append(page)
    except AndroidUseError:
        pass
    return candidates


def type_webview_text_fast(
    serial: str,
    text: str,
    *,
    clear_first: bool = False,
    enter: bool = False,
    prefer_answer_box: bool = False,
) -> str | None:
    for page in candidate_webview_pages_for_input(serial):
        expression = webview_direct_input_expression(
            text,
            clear_first=clear_first,
            enter=enter,
            prefer_answer_box=prefer_answer_box or xiaoluxue_url_kind(str(page.get("url") or "")) == "exercise",
        )
        try:
            result = cdp_eval_value(page, expression, timeout=3)
        except AndroidUseError:
            continue
        if isinstance(result, dict) and result.get("ok"):
            return f"webview_dom_{result.get('method') or 'direct'}"
    return None


def list_input_methods(serial: str) -> list[str]:
    try:
        output = shell(serial, "ime list -s", timeout=5)
    except AndroidUseError:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def find_adb_keyboard_ime(serial: str) -> str | None:
    configured = os.environ.get("ANDROID_USE_ADB_KEYBOARD_IME")
    if configured:
        return configured
    for ime in list_input_methods(serial):
        if input_method_looks_like_adb_keyboard(ime):
            return ime
    return None


def set_input_method(serial: str, ime: str) -> None:
    shell(serial, f"ime set {shlex.quote(ime)} >/dev/null", timeout=5)


def adb_keyboard_broadcast_text(
    serial: str,
    text: str,
    *,
    clear_first: bool = False,
    enter: bool = False,
) -> bool:
    if not input_method_looks_like_adb_keyboard(current_input_method(serial)):
        return False
    commands: list[str] = []
    if clear_first:
        commands.append("am broadcast -a ADB_CLEAR_TEXT >/dev/null")
    if text:
        commands.append(f"am broadcast -a ADB_INPUT_TEXT --es msg {shlex.quote(text)} >/dev/null")
    if enter:
        commands.append("am broadcast -a ADB_INPUT_KEYCODE --ei code 66 >/dev/null")
    if commands:
        shell(serial, " && ".join(commands), timeout=8)
    return True


def switch_to_adb_keyboard_and_broadcast_text(
    serial: str,
    text: str,
    *,
    clear_first: bool = False,
    enter: bool = False,
) -> str | None:
    original_ime = current_input_method(serial)
    if input_method_looks_like_adb_keyboard(original_ime):
        if adb_keyboard_broadcast_text(serial, text, clear_first=clear_first, enter=enter):
            return "adb_keyboard_broadcast"
        return None

    ime = find_adb_keyboard_ime(serial)
    if not ime:
        return None

    set_input_method(serial, ime)
    try:
        if not adb_keyboard_broadcast_text(serial, text, clear_first=clear_first, enter=enter):
            return None
    finally:
        if original_ime and restore_ime_after_fast_input() and original_ime != ime:
            with contextlib.suppress(AndroidUseError):
                set_input_method(serial, original_ime)
    return "adb_keyboard_switch_restore" if restore_ime_after_fast_input() else "adb_keyboard_switch"


def input_keyevent_list(command: str, count: int) -> str | None:
    if count <= 0:
        return None
    return f"input keyevent {' '.join([command] * count)}"


def adb_shell_batch_type_text(
    serial: str,
    text: str,
    *,
    clear_first: bool = False,
    clear_count: int = 80,
    enter: bool = False,
) -> None:
    commands: list[str] = []
    if clear_first:
        commands.append("input keyevent KEYCODE_MOVE_END")
        delete_command = input_keyevent_list("KEYCODE_DEL", max(0, clear_count))
        if delete_command:
            commands.append(delete_command)
    if text:
        commands.append(f"input text {shlex.quote(escape_input_text(text))}")
    if enter:
        commands.append("input keyevent KEYCODE_ENTER")
    if commands:
        shell(serial, " && ".join(commands), timeout=15)


def type_focused_text_fast(
    serial: str,
    text: str,
    *,
    clear_first: bool = False,
    clear_count: int = 80,
    enter: bool = False,
) -> str:
    if webview_direct_input_enabled():
        with contextlib.suppress(AndroidUseError):
            method = type_webview_text_fast(
                serial,
                text,
                clear_first=clear_first,
                enter=enter,
            )
            if method:
                return method
    if should_try_fast_ime(text, clear_first=clear_first):
        with contextlib.suppress(AndroidUseError):
            method = switch_to_adb_keyboard_and_broadcast_text(
                serial,
                text,
                clear_first=clear_first,
                enter=enter,
            )
            if method:
                return method
    else:
        with contextlib.suppress(AndroidUseError):
            if adb_keyboard_broadcast_text(serial, text, clear_first=clear_first, enter=enter):
                return "adb_keyboard_broadcast"
    adb_shell_batch_type_text(
        serial,
        text,
        clear_first=clear_first,
        clear_count=clear_count,
        enter=enter,
    )
    return "adb_shell_batch"


def action_result(action: str, serial: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": True, "action": action, "serial": serial}
    if extra:
        payload.update(extra)
    return payload


def text_content(payload: Any) -> dict[str, str]:
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, indent=2, ensure_ascii=False)
    return {"type": "text", "text": text}


def image_content(png: bytes) -> dict[str, str]:
    return {
        "type": "image",
        "data": base64.b64encode(png).decode("ascii"),
        "mimeType": "image/png",
    }


def check_dependencies(_args: dict[str, Any]) -> list[dict[str, Any]]:
    adb_path = shutil.which(adb_binary()) or adb_binary()
    scrcpy_path = shutil.which(scrcpy_binary()) or scrcpy_binary()
    adb_available = shutil.which(adb_binary()) is not None or Path(adb_binary()).exists()
    scrcpy_available = shutil.which(scrcpy_binary()) is not None or Path(scrcpy_binary()).exists()
    wireless_host, wireless_port, wireless_serial = wireless_config_from_env()
    wireless_configs = wireless_configs_from_env()
    payload: dict[str, Any] = {
        "ok": adb_available and scrcpy_available,
        "adb": {
            "command": adb_binary(),
            "path": adb_path,
            "available": adb_available,
            "required": True,
        },
        "scrcpy": {
            "command": scrcpy_binary(),
            "path": scrcpy_path,
            "available": scrcpy_available,
            "required": True,
            "install_hint": "brew install scrcpy",
        },
        "vlm": {
            "provider": os.environ.get("ANDROID_USE_AGENT_PROVIDER", "openai-compatible"),
            "base_url_configured": bool(os.environ.get("ANDROID_USE_VLM_BASE_URL")),
            "api_key_configured": bool(os.environ.get("ANDROID_USE_VLM_API_KEY")),
            "model": os.environ.get("ANDROID_USE_VLM_MODEL"),
            "coordinate_mode": infer_coordinate_mode(os.environ.get("ANDROID_USE_VLM_MODEL")),
            "timeout_sec": float(os.environ.get("ANDROID_USE_VLM_TIMEOUT", "45")),
        },
        "wireless": {
            "auto_connect": env_flag("ANDROID_USE_WIRELESS_AUTO_CONNECT", True),
            "host": wireless_host,
            "port": wireless_port,
            "serial": wireless_serial,
            "devices": wireless_configs,
            "configured_count": len(wireless_configs),
            "env_file": str(USER_ENV_FILE),
            "configured": bool(wireless_configs),
        },
        "openai": {
            "api_key_configured": bool(os.environ.get("ANDROID_USE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")),
            "base_url": os.environ.get("ANDROID_USE_OPENAI_BASE_URL", OPENAI_BASE_URL),
            "computer_model": os.environ.get("ANDROID_USE_OPENAI_COMPUTER_MODEL", "gpt-5.5"),
            "vision_model": os.environ.get("ANDROID_USE_OPENAI_VISION_MODEL", os.environ.get("ANDROID_USE_OPENAI_MODEL", "gpt-5.5")),
        },
    }
    try:
        stdout, stderr = run_command([adb_binary(), "version"], timeout=5)
        version_text = decode_bytes(stdout or stderr)
        payload["adb"]["version"] = version_text.splitlines()[0] if version_text else "unknown"
    except AndroidUseError as exc:
        payload["adb"]["error"] = str(exc)
    try:
        stdout, stderr = run_command([scrcpy_binary(), "--version"], timeout=5)
        version_text = decode_bytes(stdout or stderr)
        payload["scrcpy"]["version"] = version_text.splitlines()[0] if version_text else "unknown"
    except AndroidUseError as exc:
        payload["scrcpy"]["error"] = str(exc)
    return [text_content(payload)]


def tool_list_devices(args: dict[str, Any]) -> list[dict[str, Any]]:
    include_details = bool(args.get("include_details", True))
    devices = list_devices()
    if include_details:
        for device in devices:
            if device.get("state") == "device":
                try:
                    device["state_details"] = device_state(str(device["serial"]))
                except AndroidUseError as exc:
                    device["state_error"] = str(exc)
    return [text_content({"devices": devices})]


def tool_wireless_pair(args: dict[str, Any]) -> list[dict[str, Any]]:
    host = str(args.get("host") or "").strip()
    pair_port = int(args.get("pair_port") or 0)
    code = str(args.get("code") or "").strip()
    connect_port = int(args.get("connect_port") or 0) or None
    save = bool(args.get("save", True))
    start_scrcpy = bool(args.get("start_scrcpy", True))
    if not host:
        raise AndroidUseError("host is required, for example 172.27.31.51")
    if pair_port <= 0:
        raise AndroidUseError("pair_port is required. Use the port shown beside the pairing code.")
    if not code:
        raise AndroidUseError("code is required. It is the temporary Wireless debugging pairing code.")

    target = f"{host}:{pair_port}"
    stdout, stderr = run_command([adb_binary(), "pair", target, code], timeout=30)
    pair_output = "\n".join(part for part in [decode_bytes(stdout), decode_bytes(stderr)] if part)
    reconnect_result = wireless_reconnect(
        host=host,
        port=connect_port,
        save=save,
        start_scrcpy=start_scrcpy,
    )
    return [
        text_content(
            {
                "ok": True,
                "paired": True,
                "pair_target": target,
                "pair_output": pair_output,
                "reconnect": reconnect_result,
                "env_file": str(USER_ENV_FILE) if save else None,
            }
        )
    ]


def tool_wireless_reconnect(args: dict[str, Any]) -> list[dict[str, Any]]:
    host = str(args.get("host") or "").strip() or None
    port = int(args.get("port") or 0) or None
    serial = str(args.get("serial") or "").strip() or None
    save = bool(args.get("save", True))
    start_scrcpy = bool(args.get("start_scrcpy", True))
    if bool(args.get("all", False)):
        result = wireless_reconnect_all(save=save, start_scrcpy=start_scrcpy)
        return [text_content({"ok": True, **result, "env_file": str(USER_ENV_FILE) if save else None})]
    result = wireless_reconnect(host=host, port=port, serial=serial, save=save, start_scrcpy=start_scrcpy)
    return [text_content({"ok": True, **result, "env_file": str(USER_ENV_FILE) if save else None})]


def tool_get_state(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    payload = device_state(serial)
    include_screenshot = bool(args.get("include_screenshot", False))
    content = [text_content(payload)]
    if include_screenshot:
        png = screenshot_png(serial)
        content.append(image_content(png))
    return content


def tool_screenshot(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    png = screenshot_png(serial)
    size = png_size(png)
    save_path = save_png(serial, png, args.get("save_path") if args.get("save_path") else None)
    return [
        text_content(
            {
                "serial": serial,
                "path": save_path,
                "mime_type": "image/png",
                "bytes": len(png),
                "screen": size,
            }
        ),
        image_content(png),
    ]


def tool_show_screen(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    png = screenshot_png(serial)
    size = png_size(png)
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    save_path = save_png(serial, png, str(SCREEN_DIR / "latest.png"))
    return [
        text_content(
            {
                "serial": serial,
                "path": save_path,
                "mime_type": "image/png",
                "bytes": len(png),
                "screen": size,
                "display": "Current Android screen image is attached in this tool result.",
            }
        ),
        image_content(png),
    ]


def tool_observe(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    include_screenshot = bool(args.get("include_screenshot", False))
    include_xml = bool(args.get("include_xml", False))
    limit = max(20, min(int(args.get("limit", 160)), 500))
    observation = observe_ui(serial, include_xml=include_xml, limit=limit)
    content = [text_content(observation)]
    if include_screenshot:
        png = screenshot_png(serial)
        content.append(image_content(png))
    return content


def tool_tap_text(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    query = str(args["text"]).strip()
    exact = bool(args.get("exact", True))
    include_resource_id = bool(args.get("include_resource_id", False))
    observation = observe_ui(serial, limit=300)
    before = snapshot_from_observation(observation) if active_recording(serial) else None
    nodes = observation["ui"]["nodes"]
    node = find_ui_node(nodes, query, exact=exact, include_resource_id=include_resource_id)
    if not node and exact:
        node = find_ui_node(nodes, query, exact=False, include_resource_id=include_resource_id)
    point = node_click_point(node) if node else None
    if not point:
        raise AndroidUseError(f"Could not find a tappable UI node matching text: {query!r}")
    adb(["shell", "input", "tap", str(point["x"]), str(point["y"])], serial=serial, timeout=10)
    payload = action_result(
        "tap_text",
        serial,
        {"text": query, "x": point["x"], "y": point["y"], "matched_node": compact_node(node)},
    )
    append_recording_step(
        serial,
        "tap_text",
        {"text": query, "exact": exact, "include_resource_id": include_resource_id},
        payload,
        before=before,
    )
    return [text_content(payload)]


def tool_tap(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    x = int(args["x"])
    y = int(args["y"])
    before = capture_record_snapshot(serial) if active_recording(serial) else None
    adb(["shell", "input", "tap", str(x), str(y)], serial=serial, timeout=10)
    payload = action_result("tap", serial, {"x": x, "y": y})
    append_recording_step(serial, "tap", {"x": x, "y": y}, payload, before=before)
    return [text_content(payload)]


def tool_swipe(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    start_x = int(args["start_x"])
    start_y = int(args["start_y"])
    end_x = int(args["end_x"])
    end_y = int(args["end_y"])
    duration_ms = int(args.get("duration_ms", 300))
    before = capture_record_snapshot(serial) if active_recording(serial) else None
    adb(
        [
            "shell",
            "input",
            "swipe",
            str(start_x),
            str(start_y),
            str(end_x),
            str(end_y),
            str(duration_ms),
        ],
        serial=serial,
        timeout=10,
    )
    action_args = {
        "start_x": start_x,
        "start_y": start_y,
        "end_x": end_x,
        "end_y": end_y,
        "duration_ms": duration_ms,
    }
    payload = action_result("swipe", serial, action_args)
    append_recording_step(serial, "swipe", action_args, payload, before=before)
    return [text_content(payload)]


def tool_type_text(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    text = str(args["text"])
    before = capture_record_snapshot(serial) if active_recording(serial) else None
    clear_first = bool(args.get("clear_first"))
    clear_count = int(args.get("clear_count", 80))
    enter = bool(args.get("enter"))
    method = type_focused_text_fast(
        serial,
        text,
        clear_first=clear_first,
        clear_count=clear_count,
        enter=enter,
    )
    action_args = {
        "text": text,
        "clear_first": clear_first,
        "clear_count": clear_count,
        "enter": enter,
    }
    payload = action_result(
        "type_text",
        serial,
        {
            "chars": len(text),
            "clear_first": action_args["clear_first"],
            "enter": action_args["enter"],
            "method": method,
        },
    )
    append_recording_step(serial, "type_text", action_args, payload, before=before)
    return [text_content(payload)]


def tool_press_key(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    key = keycode(args["key"])
    before = capture_record_snapshot(serial) if active_recording(serial) else None
    adb(["shell", "input", "keyevent", key], serial=serial, timeout=10)
    payload = action_result("press_key", serial, {"key": key})
    append_recording_step(serial, "press_key", {"key": key}, payload, before=before)
    return [text_content(payload)]


def tool_wake_unlock(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    before = capture_record_snapshot(serial) if active_recording(serial) else None
    adb(["shell", "input", "keyevent", "KEYCODE_WAKEUP"], serial=serial, timeout=10)
    if args.get("dismiss_keyguard", True):
        adb(["shell", "wm", "dismiss-keyguard"], serial=serial, timeout=10)
    action_args = {"dismiss_keyguard": bool(args.get("dismiss_keyguard", True))}
    payload = action_result("wake_unlock", serial, action_args)
    append_recording_step(serial, "wake_unlock", action_args, payload, before=before)
    return [text_content(payload)]


def tool_open_url(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    url = str(args["url"]).strip()
    if not url:
        raise AndroidUseError("url must not be empty.")
    before = capture_record_snapshot(serial) if active_recording(serial) else None
    if is_xiaoluxue_app_only_url(url):
        route_result = xiaoluxue_route_app_url(serial, url)
        payload = action_result("open_url", serial, {"url": url, "routed": route_result})
        append_recording_step(serial, "open_url", {"url": url, "xiaoluxue_app_route": True}, payload, before=before)
        return [text_content(payload)]
    adb(
        ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url],
        serial=serial,
        timeout=15,
    )
    payload = action_result("open_url", serial, {"url": url})
    append_recording_step(serial, "open_url", {"url": url}, payload, before=before)
    return [text_content(payload)]


def tool_open_app(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    package = str(args["package"]).strip()
    activity = str(args.get("activity", "")).strip()
    if not package:
        raise AndroidUseError("package must not be empty.")
    before = capture_record_snapshot(serial) if active_recording(serial) else None
    if activity:
        component = activity if "/" in activity else f"{package}/{activity}"
        adb(["shell", "am", "start", "-n", component], serial=serial, timeout=15)
        payload = action_result("open_app", serial, {"package": package, "activity": activity})
        append_recording_step(serial, "open_app", {"package": package, "activity": activity}, payload, before=before)
        return [text_content(payload)]

    adb(
        ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"],
        serial=serial,
        timeout=15,
    )
    payload = action_result("open_app", serial, {"package": package})
    append_recording_step(serial, "open_app", {"package": package}, payload, before=before)
    return [text_content(payload)]


def tool_shell(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    timeout = min(float(args.get("timeout_sec", 20)), 120)
    command = str(args["command"])
    stdout = shell(serial, command, timeout=timeout)
    return [text_content({"serial": serial, "command": command, "stdout": stdout})]


def tool_webview_pages(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    port = int(args["port"]) if args.get("port") is not None else None
    pages = discover_webview_pages(serial, port=port)
    return [text_content({"ok": True, "serial": serial, "pages": pages})]


def tool_webview_eval(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    pages = discover_webview_pages(serial)
    page = select_webview_page(
        pages,
        page_id=str(args.get("page_id") or "") or None,
        url_contains=str(args.get("url_contains") or "") or None,
        title_contains=str(args.get("title_contains") or "") or None,
    )
    timeout = min(float(args.get("timeout_sec", 10)), 60)
    evaluation = cdp_runtime_evaluate(
        str(page["webSocketDebuggerUrl"]),
        str(args["expression"]),
        await_promise=bool(args.get("await_promise", True)),
        return_by_value=bool(args.get("return_by_value", True)),
        timeout=timeout,
    )
    return [
        text_content(
            {
                "ok": True,
                "serial": serial,
                "page": {
                    "id": page.get("id"),
                    "title": page.get("title"),
                    "url": page.get("url"),
                    "socket": page.get("socket"),
                    "forward": page.get("forward"),
                },
                "result": evaluation,
            }
        )
    ]


XIAOLUXUE_SITE_URL_MARKER = "stu.xiaoluxue.com"
XIAOLUXUE_COURSE_URL_MARKER = "stu.xiaoluxue.com/course"
XIAOLUXUE_STUDENT_PACKAGE = "com.xiaoluxue.ai.student"
XIAOLUXUE_STUDENT_LAUNCHER_COMPONENT = "com.xiaoluxue.ai.student/com.xiaoluxue.ai.student.LauncherActivity"
XIAOLUXUE_CONFIG_PACKAGE = "com.xiaoluxue.ai.config"
XIAOLUXUE_CONFIG_LAUNCHER_COMPONENT = "com.xiaoluxue.ai.config/com.xiaoluxue.ai.config.LauncherActivity"
XIAOLUXUE_LOGIN_ACTIVITY = "com.xiaoluxue.ai.business.account.ui.LoginActivity"
XIAOLUXUE_STUDY_SUBJECT_ACTIVITY = "com.xiaoluxue.ai.business.launcher.study.subject.StudySubjectActivity"
XIAOLUXUE_LESSON_ACTIVITY = "com.xiaoluxue.ai.business.lesson.LessonActivity"
XIAOLUXUE_SCHEME_PROXY_ACTIVITY = "com.xiaoluxue.ai.infra.framework.router.SchemeProxyActivity"
XIAOLUXUE_STANDARD_BROWSER_ACTIVITY = "com.xiaoluxue.ai.infra.browser.vessel.StandardBrowserActivity"
XIAOLUXUE_LEAK_ACTIVITY_MARKER = "leakcanary.internal.activity"
XIAOLUXUE_VESSEL_WEBVIEW_ROUTE = "xlx://router/vessel/webview"
XIAOLUXUE_STUDY_SUBJECT_ROUTE = "xlx://router/study/subject"
XIAOLUXUE_GW_ORIGIN = "https://gw-stu.xiaoluxue.com"
XIAOLUXUE_NATIVE_BASE_WIDTH = 2000
XIAOLUXUE_NATIVE_BASE_HEIGHT = 1200
XIAOLUXUE_NATIVE_MATH_CARD = (690, 280)
XIAOLUXUE_NATIVE_GUIDE_BUBBLE = (770, 505)
XIAOLUXUE_NATIVE_CONTINUE_BUTTON = (780, 815)
XIAOLUXUE_NATIVE_PROGRESS_POPUP_CLOSE = (1515, 422)
XIAOLUXUE_NATIVE_MAP_CACHE_PATH = SOURCE_MAP_DIR / "xiaoluxue-native-map-cache.json"
XIAOLUXUE_NATIVE_MAP_CACHE: dict[str, Any] = {}
XIAOLUXUE_SUBJECT_ALIASES: dict[str, int] = {
    "语文": 1,
    "中文": 1,
    "数学": 2,
    "英语": 3,
    "英文": 3,
    "物理": 4,
    "化学": 5,
    "生物": 6,
}
XIAOLUXUE_NATIVE_MAP_ROUTE_PRESETS: dict[int, dict[str, dict[str, tuple[int, int]]]] = {
    1: {
        "1.5": {
            "index": (1508, 251),
            "practise": (1116, 401),
            "expand": (1314, 593),
            "wrong": (926, 820),
            "notebook": (1074, 820),
            "report": (1000, 708),
        }
    }
}
XIAOLUXUE_MAP_MODULE_ENTRY_ACTIONS = {"practise", "expand"}
XIAOLUXUE_NATIVE_SELECTED_MODULE_POINTS: dict[str, tuple[int, int]] = {
    "practise": (1000, 392),
    "expand": (1198, 501),
}
XIAOLUXUE_NATIVE_MODULE_CARD_ENTER_OFFSET_Y = 273
XIAOLUXUE_NATIVE_EXPAND_CONFIRM_ENTER = (1312, 854)
XIAOLUXUE_NATIVE_DIRECT_PRACTICE_ENTER = (468, 936)
XIAOLUXUE_NATIVE_CURRENT_CARD_DIRECT_PRACTICE_ENTER = (955, 936)
XIAOLUXUE_NATIVE_RIGHT_CARD_DIRECT_PRACTICE_ENTER = (1432, 936)
XIAOLUXUE_NATIVE_ANSWER_CONTINUE = (1845, 1108)
XIAOLUXUE_NATIVE_RESULT_FINISH = (1160, 856)
XIAOLUXUE_NATIVE_TRANSITION_START = (1000, 1055)
ANDROID_ANIMATION_SCALE_SETTINGS = (
    "window_animation_scale",
    "transition_animation_scale",
    "animator_duration_scale",
)
XIAOLUXUE_MAP_FAST_KEYWORDS = (
    "地图",
    "题型突破",
    "题型",
    "突破",
    "专属精练",
    "专属练习",
    "专属",
    "精练",
    "专练",
    "巩固练习",
    "巩固",
    "错题",
    "笔记",
    "笔记本",
    "学习任务",
    "任务",
    "薄弱知识",
    "薄弱",
    "看报告",
    "报告",
)
XIAOLUXUE_CONFIG_URL_PATTERN = re.compile(r"https://gw-stu[^\s，,;]+")
XIAOLUXUE_APP_ONLY_HOSTS = {"stu.xiaoluxue.com"}
XIAOLUXUE_APP_ONLY_SUFFIXES = (".xiaoluxue.cn",)
XIAOLUXUE_ENV_CHOICES: dict[str, dict[str, str]] = {
    "prod": {"label": "生产环境-com", "url": "https://gw-stu.xiaoluxue.com"},
    "prod-com": {"label": "生产环境-com", "url": "https://gw-stu.xiaoluxue.com"},
    "production": {"label": "生产环境-com", "url": "https://gw-stu.xiaoluxue.com"},
    "dev": {"label": "Dev环境", "url": "https://gw-stu.dev.xiaoluxue.cn/"},
    "test": {"label": "Test环境", "url": "https://gw-stu.test.xiaoluxue.cn/"},
    "test2": {"label": "Test2环境", "url": "https://gw-stu.test2.xiaoluxue.cn/"},
    "test3": {"label": "Test3环境", "url": "https://gw-stu.test3.xiaoluxue.cn/"},
    "test4": {"label": "Test4环境", "url": "https://gw-stu.test4.xiaoluxue.cn/"},
    "test5": {"label": "Test5环境", "url": "https://gw-stu.test5.xiaoluxue.cn/"},
    "test6": {"label": "Test6环境", "url": "https://gw-stu.test6.xiaoluxue.cn/"},
    "kmtest": {"label": "Kmtest环境", "url": "https://gw-stu.kmtest.xiaoluxue.cn/"},
}


def is_xiaoluxue_app_only_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").casefold()
    return is_xiaoluxue_h5_host(host)


def is_xiaoluxue_h5_host(host: str) -> bool:
    normalized = host.casefold().strip()
    return normalized in XIAOLUXUE_APP_ONLY_HOSTS or any(normalized.endswith(suffix) for suffix in XIAOLUXUE_APP_ONLY_SUFFIXES)


def xiaoluxue_url_kind(url: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return None
    host = parsed.hostname or ""
    if not is_xiaoluxue_h5_host(host):
        return None
    path = parsed.path.rstrip("/") or "/"
    if path == "/course" or path.startswith("/course/"):
        return "course"
    if path == "/exercise" or path.startswith("/exercise/"):
        return "exercise"
    return "any"


def xiaoluxue_page_matches(page: dict[str, Any], page_kind: str) -> bool:
    kind = xiaoluxue_url_kind(str(page.get("url") or ""))
    if page_kind == "any":
        return kind is not None or "小鹿爱学" in str(page.get("title") or "")
    return kind == page_kind


def select_xiaoluxue_webview_page(pages: list[dict[str, Any]], page_kind: str = "any") -> dict[str, Any]:
    candidates = [page for page in pages if not page.get("error") and xiaoluxue_page_matches(page, page_kind)]
    if not candidates:
        detail = {
            "page_kind": page_kind,
            "known": [
                {"id": page.get("id"), "title": page.get("title"), "url": page.get("url"), "error": page.get("error")}
                for page in pages[:10]
            ],
        }
        raise AndroidUseError(f"No matching Xiaoluxue WebView page found: {json.dumps(detail, ensure_ascii=False)[:1200]}")
    candidates.sort(key=webview_page_score, reverse=True)
    page = candidates[0]
    if not page.get("webSocketDebuggerUrl"):
        raise AndroidUseError(f"Matched Xiaoluxue WebView page has no webSocketDebuggerUrl: {page}")
    return page


def xiaoluxue_page_hidden_behind_native(serial: str, page: dict[str, Any]) -> bool:
    description = page.get("descriptionParsed") if isinstance(page.get("descriptionParsed"), dict) else {}
    if description.get("visible") is not False:
        return False
    try:
        focus = get_focused_window(serial) or ""
    except Exception:
        focus = ""
    return XIAOLUXUE_STUDENT_PACKAGE in focus and XIAOLUXUE_STANDARD_BROWSER_ACTIVITY not in focus


def xiaoluxue_ensure_foreground_webview(serial: str, page: dict[str, Any]) -> None:
    if xiaoluxue_page_hidden_behind_native(serial, page):
        try:
            focus = get_focused_window(serial) or ""
        except Exception:
            focus = ""
        raise AndroidUseError(f"Xiaoluxue WebView exists only in background; foreground is {focus or 'unknown'}.")


def xiaoluxue_dismiss_debug_overlay_if_needed(
    serial: str,
    steps: list[dict[str, Any]] | None = None,
    started_at: float | None = None,
) -> bool:
    try:
        focus = get_focused_window(serial) or ""
    except Exception:
        focus = ""
    if XIAOLUXUE_LEAK_ACTIVITY_MARKER not in focus:
        return False
    adb(["shell", "input", "keyevent", "BACK"], serial=serial, timeout=4)
    if steps is not None:
        steps.append(
            {
                "action": "back",
                "reason": "dismiss-leakcanary-overlay",
                "from_focus": focus,
                "at_sec": round(time.monotonic() - float(started_at or time.monotonic()), 3),
            }
        )
    time.sleep(0.18)
    return True


def xiaoluxue_vessel_webview_url(url: str) -> str:
    if not is_xiaoluxue_app_only_url(url):
        raise AndroidUseError(f"Not a Xiaoluxue app-only URL: {url}")
    return (
        f"{XIAOLUXUE_VESSEL_WEBVIEW_ROUTE}?url={urllib.parse.quote(url, safe='')}"
        "&full_screen=true&title_bar=false"
    )


def xiaoluxue_route_app_url(serial: str, url: str, *, force_stop: bool = False) -> dict[str, Any]:
    route_url = xiaoluxue_vessel_webview_url(url)
    command = [
        "shell",
        "am",
        "start",
    ]
    if force_stop:
        command.append("-S")
    command.extend(
        [
            "-n",
            f"{XIAOLUXUE_STUDENT_PACKAGE}/{XIAOLUXUE_SCHEME_PROXY_ACTIVITY}",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            route_url,
        ]
    )
    output = adb(command, serial=serial, timeout=3).decode("utf-8", errors="replace")
    return {
        "ok": True,
        "mode": "xiaoluxue-vessel-webview-route",
        "url": url,
        "route_url": route_url,
        "package": XIAOLUXUE_STUDENT_PACKAGE,
        "am_start": output,
    }


def xiaoluxue_cache_key(serial: str, page_kind: str) -> tuple[str, str]:
    return (serial, page_kind)


def xiaoluxue_remember_page(serial: str, page_kind: str, page: dict[str, Any]) -> dict[str, Any]:
    cached = dict(page)
    cached["_cachedAt"] = time.monotonic()
    cached["_cacheKind"] = page_kind
    XIAOLUXUE_PAGE_CACHE[xiaoluxue_cache_key(serial, page_kind)] = cached
    return page


def xiaoluxue_remember_inferred_page(serial: str, page: dict[str, Any]) -> dict[str, Any]:
    xiaoluxue_remember_page(serial, "any", page)
    kind = xiaoluxue_url_kind(str(page.get("url") or ""))
    if kind in {"course", "exercise"}:
        xiaoluxue_remember_page(serial, kind, page)
    return page


def xiaoluxue_cached_page(
    serial: str,
    page_kind: str,
    *,
    runtime_contains: str,
    max_age_sec: float = 12.0,
) -> dict[str, Any] | None:
    cached = XIAOLUXUE_PAGE_CACHE.get(xiaoluxue_cache_key(serial, page_kind))
    if not cached:
        return None
    if time.monotonic() - float(cached.get("_cachedAt", 0)) > max_age_sec:
        return None
    websocket = str(cached.get("webSocketDebuggerUrl") or "")
    if not websocket:
        return None
    try:
        runtime_href = cdp_eval_value(cached, "location.href", timeout=0.25)
    except Exception:
        return None
    if isinstance(runtime_href, str) and runtime_contains in runtime_href:
        return {**cached, "runtimeHref": runtime_href, "cacheHit": True}
    return None


def xiaoluxue_cached_runtime_page(
    serial: str,
    page_kind: str,
    *,
    max_age_sec: float = 12.0,
) -> dict[str, Any] | None:
    cached = XIAOLUXUE_PAGE_CACHE.get(xiaoluxue_cache_key(serial, page_kind))
    if not cached:
        return None
    if time.monotonic() - float(cached.get("_cachedAt", 0)) > max_age_sec:
        return None
    if not str(cached.get("webSocketDebuggerUrl") or ""):
        return None
    try:
        runtime_href = cdp_eval_value(cached, "location.href", timeout=0.25)
    except Exception:
        return None
    if isinstance(runtime_href, str):
        kind = xiaoluxue_url_kind(runtime_href)
        if page_kind == "any" and kind is not None:
            return {**cached, "runtimeHref": runtime_href, "cacheHit": True}
        if kind == page_kind:
            return {**cached, "runtimeHref": runtime_href, "cacheHit": True}
    return None

# Xiaoluxue's subject home displays human course indexes like "1.1.1.1".
# Users often omit one dot when saying/typing them ("1.1.11"). Store fast
# aliases by a digits-only key so both resolve without scanning the whole tree.
XIAOLUXUE_KNOWLEDGE_SHORTCUTS: dict[tuple[int, str], dict[str, Any]] = {
    (2, "1111"): {
        "knowledgeId": 3785,
        "knowledgeIndex": "1.1.1.1",
        "knowledgeName": "集合及其表示方法",
        "subjectName": "数学",
        "lessonId": 598907589657093,
        "studySessionId": 908465254772106,
	        "guideIndex": 1,
	        "guideName": "初识集合——集合与元素的定义",
	        "guideCdnUrl": "https://static.xiaoluxue.cn/lesson/video/json/66061af39a8b9c7138fcb4024ae308e7_1_guide_598907589657093_1766421186968.json",
	        "avatarUrl": "https://vod.xiaoluxue.com/a3e82dbafecb4345b81324f4a3909d60.mp4?a=0&br=265&bt=265&cd=0%7C0%7C0&ch=0&cr=0&cs=0&dr=0&ds=2&eid=v02103g10065d4256qaljhteml2gg690&er=0&l=202510311412583A8E7F4FCA9807868DCD&lr=&mime_type=video_mp4&net=0&pl=0&qs=13&rc=Mzlqb2hrb3Q4NzgzNDY0M0ApPGo1cHdtZDk1ZzkzajM1eWc2ZC9qcWdeMy9hMy1kLS9zcy0zZGliZWluMjEyLS4wLi06Yw%3D%3D&vl=&vr=",
	        "targetUrl": "https://stu.xiaoluxue.com/course?knowledgeId=3785&knowledgeName=%E9%9B%86%E5%90%88%E5%8F%8A%E5%85%B6%E8%A1%A8%E7%A4%BA%E6%96%B9%E6%B3%95&lessonId=598907589657093&lessonName=%E9%9B%86%E5%90%88%E5%8F%8A%E5%85%B6%E8%A1%A8%E7%A4%BA%E6%96%B9%E6%B3%95&phaseId=3&studySessionId=908465254772106&studyType=1&subjectId=2&redirectWidgetIndex=1",
	    },
}


def xiaoluxue_page(serial: str) -> dict[str, Any]:
    xiaoluxue_dismiss_debug_overlay_if_needed(serial)
    cached = xiaoluxue_cached_runtime_page(serial, "course")
    if cached:
        xiaoluxue_ensure_foreground_webview(serial, cached)
        return cached
    pages = discover_webview_pages(serial)
    page = select_xiaoluxue_webview_page(pages, "course")
    xiaoluxue_ensure_foreground_webview(serial, page)
    xiaoluxue_remember_inferred_page(serial, page)
    return xiaoluxue_remember_page(serial, "course", page)


def xiaoluxue_any_page(serial: str, *, open_app_if_needed: bool = True, timeout_sec: float = 5.0) -> dict[str, Any]:
    xiaoluxue_dismiss_debug_overlay_if_needed(serial)
    cached = xiaoluxue_cached_runtime_page(serial, "any")
    if cached:
        xiaoluxue_ensure_foreground_webview(serial, cached)
        return cached
    deadline = time.monotonic() + max(timeout_sec, 0.2)
    opened = False
    last_error: Exception | None = None
    while True:
        pages = discover_webview_pages(serial)
        try:
            page = select_xiaoluxue_webview_page(pages, "any")
            xiaoluxue_ensure_foreground_webview(serial, page)
            return xiaoluxue_remember_inferred_page(serial, page)
        except Exception as exc:
            last_error = exc
        if not open_app_if_needed or opened or time.monotonic() >= deadline:
            if last_error:
                raise last_error
            return select_webview_page(pages)
        adb(["shell", "monkey", "-p", XIAOLUXUE_STUDENT_PACKAGE, "-c", "android.intent.category.LAUNCHER", "1"], serial=serial, timeout=10)
        opened = True
    time.sleep(0.3)


def xiaoluxue_native_window_info(serial: str) -> dict[str, Any]:
    try:
        text = shell(serial, "dumpsys window", timeout=3)
    except AndroidUseError:
        text = ""
    focus_match = re.search(r"mCurrentFocus=([^\n]+)", text)
    bounds_match = re.search(r"boundsRect\(0,\s*0\s*-\s*(\d+),\s*(\d+)\)", text)
    width: int | None = None
    height: int | None = None
    if bounds_match:
        width = int(bounds_match.group(1))
        height = int(bounds_match.group(2))
    else:
        size = get_screen_size(serial)
        width = int(size["width"]) if size.get("width") else None
        height = int(size["height"]) if size.get("height") else None
    if width and height and width < height:
        width, height = height, width
    return {
        "focus": focus_match.group(1).strip() if focus_match else "",
        "width": width or XIAOLUXUE_NATIVE_BASE_WIDTH,
        "height": height or XIAOLUXUE_NATIVE_BASE_HEIGHT,
    }


def xiaoluxue_wait_native_app_focus(serial: str, timeout_sec: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(timeout_sec, 0.05)
    last_info = xiaoluxue_native_window_info(serial)
    while time.monotonic() < deadline:
        info = xiaoluxue_native_window_info(serial)
        last_info = info
        focus = str(info.get("focus") or "")
        if f"{XIAOLUXUE_STUDENT_PACKAGE}/" in focus:
            return info
        time.sleep(0.08)
    return last_info


def xiaoluxue_native_scaled_point(point: tuple[int, int], window_info: dict[str, Any]) -> tuple[int, int]:
    width = int(window_info.get("width") or XIAOLUXUE_NATIVE_BASE_WIDTH)
    height = int(window_info.get("height") or XIAOLUXUE_NATIVE_BASE_HEIGHT)
    x, y = point
    return round(x * width / XIAOLUXUE_NATIVE_BASE_WIDTH), round(y * height / XIAOLUXUE_NATIVE_BASE_HEIGHT)


def xiaoluxue_native_tap(
    serial: str,
    point: tuple[int, int],
    window_info: dict[str, Any],
    label: str,
    steps: list[dict[str, Any]],
    started_at: float,
) -> None:
    x, y = xiaoluxue_native_scaled_point(point, window_info)
    adb(["shell", "input", "tap", str(x), str(y)], serial=serial, timeout=2)
    steps.append({"label": label, "x": x, "y": y, "at_sec": round(time.monotonic() - started_at, 3)})


def xiaoluxue_native_back(serial: str, label: str, steps: list[dict[str, Any]], started_at: float) -> None:
    adb(["shell", "input", "keyevent", "KEYCODE_BACK"], serial=serial, timeout=2)
    steps.append({"label": label, "key": "BACK", "at_sec": round(time.monotonic() - started_at, 3)})


def xiaoluxue_wait_for_site_page(serial: str, deadline: float, *, poll_interval: float = 0.04) -> dict[str, Any]:
    cached = xiaoluxue_cached_runtime_page(serial, "any")
    if cached:
        return cached
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        pages = discover_webview_pages(serial)
        try:
            page = select_xiaoluxue_webview_page(pages, "any")
            return xiaoluxue_remember_inferred_page(serial, page)
        except Exception as exc:
            last_error = exc
        time.sleep(poll_interval)
    if last_error:
        raise last_error
    raise AndroidUseError("Could not find a Xiaoluxue WebView page after native entry.")


def xiaoluxue_wait_for_target_course_page(
    serial: str,
    deadline: float,
    *,
    knowledge_id: int,
    poll_interval: float = 0.04,
) -> dict[str, Any]:
    target_marker = f"knowledgeId={knowledge_id}"
    cached = xiaoluxue_cached_page(serial, "course", runtime_contains=target_marker)
    if cached:
        return cached
    last_course_page: dict[str, Any] | None = None
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        pages = discover_webview_pages(serial)
        candidates = [page for page in pages if not page.get("error")]
        candidates.sort(key=webview_page_score, reverse=True)
        for page in candidates:
            url = str(page.get("url") or "")
            if xiaoluxue_url_kind(url) == "course":
                last_course_page = page
                if target_marker in url:
                    checked_page = {**page, "runtimeHref": url}
                    try:
                        xiaoluxue_ensure_foreground_webview(serial, checked_page)
                        xiaoluxue_remember_inferred_page(serial, page)
                        return checked_page
                    except Exception as exc:
                        last_error = exc
                    try:
                        runtime_href = cdp_eval_value(page, "location.href", timeout=0.35)
                        if isinstance(runtime_href, str) and target_marker in runtime_href:
                            checked_page = {**page, "runtimeHref": runtime_href}
                            xiaoluxue_ensure_foreground_webview(serial, checked_page)
                            xiaoluxue_remember_inferred_page(serial, page)
                            return checked_page
                        if isinstance(runtime_href, str) and xiaoluxue_url_kind(runtime_href) == "course":
                            last_course_page = {**page, "runtimeHref": runtime_href}
                    except Exception as exc:
                        last_error = exc
        try:
            last_course_page = select_xiaoluxue_webview_page(pages, "course")
        except Exception as exc:
            last_error = exc
        time.sleep(poll_interval)
    if last_course_page is not None:
        last_href = str(last_course_page.get("runtimeHref") or last_course_page.get("url") or "")
        if target_marker in last_href:
            xiaoluxue_ensure_foreground_webview(serial, last_course_page)
            xiaoluxue_remember_inferred_page(serial, last_course_page)
            return last_course_page
        raise AndroidUseError(f"Xiaoluxue course WebView opened but target {target_marker} was not active.")
    if last_error:
        raise last_error
    raise AndroidUseError(f"Could not find target Xiaoluxue course WebView for {target_marker}.")


def xiaoluxue_open_vessel_course_page(
    serial: str,
    *,
    target_url: str,
    knowledge_id: int,
    timeout_sec: float,
    force_stop: bool = False,
    bootstrap_scripts: list[str] | None = None,
) -> dict[str, Any]:
    started_at = time.monotonic()
    deadline = started_at + max(timeout_sec, 0.5)
    bootstrap_results: list[dict[str, Any]] = []
    if bootstrap_scripts:
        for page in discover_webview_pages(serial)[:3]:
            websocket = str(page.get("webSocketDebuggerUrl") or "")
            if not websocket:
                continue
            page_result: dict[str, Any] = {"page_id": page.get("id"), "ok": True, "scripts": 0}
            try:
                cdp_call(websocket, "Page.enable", {}, timeout=0.25)
                for script_id in range(1, 21):
                    try:
                        cdp_call(
                            websocket,
                            "Page.removeScriptToEvaluateOnNewDocument",
                            {"identifier": str(script_id)},
                            timeout=0.08,
                        )
                    except Exception:
                        pass
                for script in bootstrap_scripts:
                    cdp_call(
                        websocket,
                        "Page.addScriptToEvaluateOnNewDocument",
                        {"source": script},
                        timeout=0.25,
                    )
                    page_result["scripts"] = int(page_result["scripts"]) + 1
            except Exception as exc:
                page_result["ok"] = False
                page_result["error"] = str(exc)
            bootstrap_results.append(page_result)
    route_url = (
        f"{XIAOLUXUE_VESSEL_WEBVIEW_ROUTE}?url={urllib.parse.quote(target_url, safe='')}"
        "&full_screen=true&title_bar=false"
    )
    command = [
        "shell",
        "am",
        "start",
    ]
    if force_stop:
        command.append("-S")
    command.extend(
        [
            "-n",
            f"{XIAOLUXUE_STUDENT_PACKAGE}/{XIAOLUXUE_SCHEME_PROXY_ACTIVITY}",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            route_url,
        ]
    )
    output = adb(command, serial=serial, timeout=3).decode("utf-8", errors="replace")
    page = xiaoluxue_wait_for_target_course_page(serial, deadline, knowledge_id=knowledge_id)
    xiaoluxue_ensure_foreground_webview(serial, page)
    return {
        "attempted": True,
        "ok": True,
        "mode": "vessel-webview-route",
        "elapsed_sec": round(time.monotonic() - started_at, 3),
        "route_url": route_url,
        "am_start": output,
        "bootstrap": bootstrap_results,
        "page_id": page.get("id"),
        "page_url": page.get("url"),
        "page": page,
    }


def xiaoluxue_try_native_course_sequence(
    serial: str,
    window_info: dict[str, Any],
    steps: list[dict[str, Any]],
    started_at: float,
    deadline: float,
    *,
    label: str,
    tap_math: bool,
    expected_knowledge_id: int | None = None,
    dismiss_progress_popup: bool = True,
) -> dict[str, Any]:
    attempt: dict[str, Any] = {
        "label": label,
        "tap_math": tap_math,
        "started_at_sec": round(time.monotonic() - started_at, 3),
    }
    try:
        if tap_math:
            xiaoluxue_native_tap(serial, XIAOLUXUE_NATIVE_MATH_CARD, window_info, f"{label}:math_card", steps, started_at)
            time.sleep(1.18)
        if dismiss_progress_popup:
            xiaoluxue_native_tap(
                serial,
                XIAOLUXUE_NATIVE_PROGRESS_POPUP_CLOSE,
                window_info,
                f"{label}:dismiss_progress_popup",
                steps,
                started_at,
            )
            time.sleep(0.06)
        xiaoluxue_native_tap(serial, XIAOLUXUE_NATIVE_GUIDE_BUBBLE, window_info, f"{label}:guide_bubble", steps, started_at)
        page: dict[str, Any] | None = None
        last_continue_error: Exception | None = None
        for continue_attempt in range(3):
            time.sleep(0.08 if continue_attempt == 0 else 0.07)
            xiaoluxue_native_tap(
                serial,
                XIAOLUXUE_NATIVE_CONTINUE_BUTTON,
                window_info,
                f"{label}:continue:{continue_attempt + 1}",
                steps,
                started_at,
            )
            try:
                page_deadline = min(deadline, time.monotonic() + 0.24)
                page = (
                    xiaoluxue_wait_for_target_course_page(
                        serial,
                        page_deadline,
                        knowledge_id=expected_knowledge_id,
                    )
                    if expected_knowledge_id
                    else xiaoluxue_wait_for_site_page(serial, page_deadline)
                )
                break
            except Exception as exc:
                last_continue_error = exc
        if page is None:
            try:
                page_deadline = min(deadline, time.monotonic() + 1.2)
                page = (
                    xiaoluxue_wait_for_target_course_page(
                        serial,
                        page_deadline,
                        knowledge_id=expected_knowledge_id,
                    )
                    if expected_knowledge_id
                    else xiaoluxue_wait_for_site_page(serial, page_deadline)
                )
            except Exception:
                if last_continue_error is not None:
                    raise last_continue_error
                raise
        attempt["ok"] = True
        attempt["elapsed_sec"] = round(time.monotonic() - started_at, 3)
        attempt["page_id"] = page.get("id")
        attempt["page_url"] = page.get("url")
        attempt["page"] = page
    except Exception as exc:  # Keep the native fallback cheap and observable.
        attempt["ok"] = False
        attempt["error"] = str(exc)
        attempt["elapsed_sec"] = round(time.monotonic() - started_at, 3)
    return attempt


def xiaoluxue_open_native_course_entry(
    serial: str,
    *,
    subject_id: int,
    normalized_index: str,
    timeout_sec: float,
) -> dict[str, Any]:
    started_at = time.monotonic()
    if (subject_id, normalized_index) != (2, "1111"):
        return {"attempted": False, "ok": False, "reason": "unsupported-native-shortcut"}

    deadline = started_at + max(timeout_sec, 1.0)
    shortcut = XIAOLUXUE_KNOWLEDGE_SHORTCUTS.get((subject_id, normalized_index))
    expected_knowledge_id = int(shortcut["knowledgeId"]) if shortcut and shortcut.get("knowledgeId") else None
    info = xiaoluxue_native_window_info(serial)
    focus = str(info.get("focus") or "")
    already_home = f"{XIAOLUXUE_STUDENT_PACKAGE}/com.xiaoluxue.ai.student.LauncherActivity" in focus
    already_subject_map = f"{XIAOLUXUE_STUDENT_PACKAGE}/{XIAOLUXUE_STUDY_SUBJECT_ACTIVITY}" in focus
    steps: list[dict[str, Any]] = []
    start_result = {"skipped": already_home or already_subject_map, "focus": focus}

    attempts: list[dict[str, Any]] = []
    page: dict[str, Any] | None = None

    try:
        route_info = xiaoluxue_open_native_subject_map(
            serial,
            {
                "subject_id": subject_id,
                "route_wait_sec": 1.15,
                "route_settle_sec": 0.05,
                "close_progress_popup": True,
                "close_progress_taps": 1,
                "close_progress_wait_sec": 0.05,
            },
            steps,
            started_at,
        )
        routed_info = route_info.get("window_info") if isinstance(route_info, dict) else None
        if isinstance(routed_info, dict):
            info = routed_info
        focus = str(info.get("focus") or "")
        already_home = False
        already_subject_map = True
        start_result = {"mode": "subject-route", "focus": focus, "route": route_info}
    except Exception as exc:
        start_result["subject_route_error"] = str(exc)

    if not already_home and not already_subject_map:
        adb(["shell", "am", "start", "-n", XIAOLUXUE_STUDENT_LAUNCHER_COMPONENT], serial=serial, timeout=5)
        steps.append({"label": "start_launcher", "at_sec": round(time.monotonic() - started_at, 3)})
        info = xiaoluxue_wait_native_app_focus(serial, min(max(deadline - time.monotonic(), 0.2), 1.4))
        focus = str(info.get("focus") or "")
        already_home = f"{XIAOLUXUE_STUDENT_PACKAGE}/com.xiaoluxue.ai.student.LauncherActivity" in focus
        already_subject_map = f"{XIAOLUXUE_STUDENT_PACKAGE}/{XIAOLUXUE_STUDY_SUBJECT_ACTIVITY}" in focus
        if not already_home and not already_subject_map:
            time.sleep(0.2)
    if already_home:
        attempts.append(
            xiaoluxue_try_native_course_sequence(
                serial,
                info,
                steps,
                started_at,
                deadline,
                label="home",
                tap_math=True,
                expected_knowledge_id=expected_knowledge_id,
                dismiss_progress_popup=True,
            )
        )
    else:
        attempts.append(
            xiaoluxue_try_native_course_sequence(
                serial,
                info,
                steps,
                started_at,
                deadline,
                label="subject_map",
                tap_math=False,
                expected_knowledge_id=expected_knowledge_id,
                dismiss_progress_popup=not (start_result.get("mode") == "subject-route"),
            )
        )
    if attempts and attempts[-1].get("ok"):
        page_value = attempts[-1].get("page")
        if isinstance(page_value, dict):
            page = page_value
    if page is None and time.monotonic() + 1.4 < deadline:
        xiaoluxue_native_back(serial, "fallback_back_to_home", steps, started_at)
        time.sleep(0.42)
        fallback_info = xiaoluxue_wait_native_app_focus(serial, min(max(deadline - time.monotonic(), 0.2), 0.8))
        attempts.append(
            xiaoluxue_try_native_course_sequence(
                serial,
                fallback_info,
                steps,
                started_at,
                deadline,
                label="fallback_home",
                tap_math=True,
                expected_knowledge_id=expected_knowledge_id,
                dismiss_progress_popup=True,
            )
        )
        page_value = attempts[-1].get("page")
        if isinstance(page_value, dict):
            page = page_value
    if page is None:
        last_error = attempts[-1].get("error") if attempts else "no native attempt completed"
        raise AndroidUseError(str(last_error))
    elapsed = round(time.monotonic() - started_at, 3)
    return {
        "attempted": True,
        "ok": True,
        "elapsed_sec": elapsed,
        "window": info,
        "start": start_result,
        "steps": steps,
        "attempts": [{k: v for k, v in attempt.items() if k != "page"} for attempt in attempts],
        "page": page,
    }


def normalize_xiaoluxue_knowledge_index(value: Any) -> str:
    return re.sub(r"[^0-9]", "", str(value or ""))


def xiaoluxue_snapshot_expression() -> str:
    return r"""
(() => {
  const rect = (el) => {
    const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, width: r.width, height: r.height, top: r.top, right: r.right, bottom: r.bottom, left: r.left };
  };
  const text = (el) => (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ");
  const parseWidgetName = (dataName) => {
    const match = /^widget-(\d+)-(.+)$/.exec(dataName || "");
    return match ? { index: Number(match[1]), name: match[2] } : { index: null, name: dataName || "" };
  };
  const widgets = [...document.querySelectorAll('[data-name^="widget-"]')].map((el) => {
    const parsed = parseWidgetName(el.dataset.name);
    const bounds = rect(el);
    const visibleRatio = Math.max(0, Math.min(bounds.bottom, innerHeight) - Math.max(bounds.top, 0)) / Math.max(bounds.height, 1);
    return {
      dataName: el.dataset.name,
      index: parsed.index,
      name: parsed.name,
      text: text(el).slice(0, 220),
      rect: bounds,
      visibleRatio,
      loaded: text(el).length > 0 || !!el.querySelector('[data-name="guide-player"], video, button'),
    };
  });
  const visibleWidgets = widgets.filter((widget) => widget.rect.bottom > 0 && widget.rect.top < innerHeight);
  const currentWidget = [...widgets].sort((a, b) => Math.abs(a.rect.top) - Math.abs(b.rect.top))[0] || null;
  const buttons = [...document.querySelectorAll('button,[role="button"],ol')]
    .map((el) => ({ text: text(el), dataName: el.dataset.name || "", rect: rect(el) }))
    .filter((item) => item.text)
    .slice(0, 60);
  const videos = [...document.querySelectorAll('video')].map((video) => ({
    src: video.currentSrc || video.src || "",
    currentTime: video.currentTime,
    duration: Number.isFinite(video.duration) ? video.duration : null,
    playbackRate: video.playbackRate,
    paused: video.paused,
    rect: rect(video),
  }));
  const params = Object.fromEntries(new URL(location.href).searchParams.entries());
  return {
    app: "xiaoluxue",
    page: "course",
    title: document.title,
    url: location.href,
    params,
    viewport: { width: innerWidth, height: innerHeight, devicePixelRatio },
    guidePlayerVisible: !!document.querySelector('[data-name="guide-player"]'),
    guideControlsVisible: !!document.querySelector('[data-name="guide-controller::follow"]'),
    widgetCount: widgets.length,
    currentWidget,
    visibleWidgets,
    widgets,
    buttons,
    videos,
    localProgressKeys: Object.keys(localStorage).filter((key) => key.startsWith("course.progress:")).slice(0, 30),
  };
})()
"""


def xiaoluxue_set_speed_expression(rate: float) -> str:
    rate_json = json.dumps(rate)
    return f"""
(async () => {{
  const rate = {rate_json};
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const text = (el) => (el.innerText || el.textContent || "").trim().replace(/\\s+/g, " ");
  const rect = (el) => {{
    const r = el.getBoundingClientRect();
    return {{ top: r.top, bottom: r.bottom, left: r.left, right: r.right, width: r.width, height: r.height }};
  }};
  const visible = (el) => {{
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight;
  }};
  const click = (el) => {{
    el.dispatchEvent(new MouseEvent("click", {{ bubbles: true, cancelable: true, view: window }}));
  }};
  const widgets = [...document.querySelectorAll('[data-name^="widget-"]')];
  const visibleWidget = widgets.find((el) => {{
    const r = el.getBoundingClientRect();
    return r.bottom > 0 && r.top < innerHeight;
  }});
  const player = visibleWidget?.querySelector('[data-name="guide-player"]') || document.querySelector('[data-name="guide-player"]');
  if (player) {{
    click(player);
    await sleep(160);
  }}
  const candidates = [...document.querySelectorAll('button,[role="button"],div,span,ol')].filter(visible);
  const rateButton = candidates.find((el) => text(el).includes("倍速"));
  if (rateButton) {{
    click(rateButton);
    await sleep(160);
  }}
  const labels = Array.from(new Set([`${{rate}}x`, `${{Number(rate).toFixed(1)}}x`]));
  const refreshed = [...document.querySelectorAll('button,[role="button"],div,span,ol,li')].filter(visible);
  const option = refreshed.find((el) => labels.includes(text(el)) || labels.some((label) => text(el).split(/\\s+/).includes(label)));
  if (option) {{
    click(option);
    await sleep(120);
  }}
  const videos = [...document.querySelectorAll("video")];
  for (const video of videos) {{
    try {{ video.playbackRate = rate; }} catch (_error) {{}}
  }}
  return {{
    ok: Boolean(option) || videos.length > 0,
    rate,
    playerClicked: Boolean(player),
    rateButtonClicked: Boolean(rateButton),
    optionClicked: Boolean(option),
    optionText: option ? text(option) : "",
    guideControlsVisible: Boolean(document.querySelector('[data-name="guide-controller::follow"]')),
    videos: videos.map((video) => ({{ playbackRate: video.playbackRate, paused: video.paused, currentTime: video.currentTime }})),
  }};
}})()
	"""


def xiaoluxue_fast_rate_expression(rate: float, guide_index: int | None = None, *, activate_player: bool = False) -> str:
    rate_json = json.dumps(rate)
    guide_index_json = "null" if guide_index is None else json.dumps(int(guide_index))
    activate_player_json = json.dumps(activate_player)
    return f"""
(() => {{
  const rate = {rate_json};
  const guideIndex = {guide_index_json};
  const activatePlayer = {activate_player_json};
  const text = (el) => (el?.innerText || el?.textContent || "").trim().replace(/\\s+/g, " ");
  const click = (el) => {{
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const eventInit = {{
      bubbles: true,
      cancelable: true,
      view: window,
      clientX: rect.left + rect.width / 2,
      clientY: rect.top + rect.height / 2,
    }};
    el.dispatchEvent(new PointerEvent("pointerdown", eventInit));
    el.dispatchEvent(new MouseEvent("mousedown", eventInit));
    el.dispatchEvent(new PointerEvent("pointerup", eventInit));
    el.dispatchEvent(new MouseEvent("mouseup", eventInit));
    el.dispatchEvent(new MouseEvent("click", eventInit));
    return true;
  }};
	  const apply = () => {{
	    const videos = [...document.querySelectorAll("video")];
	    for (const video of videos) {{
      try {{
        video.defaultPlaybackRate = rate;
        video.playbackRate = rate;
      }} catch (_error) {{}}
    }}
	    return videos.map((video) => ({{
	      playbackRate: video.playbackRate,
	      paused: video.paused,
	      currentTime: video.currentTime,
	    }}));
	  }};
	  const findTargetPlayer = () => {{
	    if (guideIndex != null) {{
	      return document.querySelector(`[data-name^="widget-${{guideIndex}}-"] [data-name="guide-player"]`);
	    }}
	    return document.querySelector('[data-name="guide-player"]');
	  }};
	  const targetVideos = () => {{
	    const player = findTargetPlayer();
	    const scope = player?.closest?.('[data-name^="widget-"]') || player || document;
	    return [...scope.querySelectorAll("video")];
	  }};
	  const targetVideoPlaying = () => targetVideos().some((video) => !video.paused && !video.ended);
	  const trySetControllerRate = () => {{
	    const controller = document.querySelector('[data-name="guide-controller::follow"]');
	    if (!controller) return {{ ok: false, reason: "controller-missing" }};
	    const desiredText = `${{rate}}x`;
	    const options = [...document.querySelectorAll("ol")];
	    const option = options.find((el) => text(el) === desiredText);
	    if (option) {{
	      click(option);
	      return {{ ok: true, selected: desiredText }};
	    }}
	    const rateButton = [...controller.querySelectorAll("button")].find((el) => text(el).includes("倍速"));
	    if (!rateButton) return {{ ok: false, reason: "rate-button-missing" }};
	    click(rateButton);
	    setTimeout(() => {{
	      const laterOption = [...document.querySelectorAll("ol")].find((el) => text(el) === desiredText);
	      if (laterOption) click(laterOption);
	    }}, 40);
	    return {{ ok: true, opened: true }};
	  }};
	  const tryActivate = () => {{
	    if (!window.__xiaoluxuePendingPlayerActivate) return {{ scheduled: false, player: findTargetPlayer() }};
	    const player = findTargetPlayer();
	    if (!player) return {{ scheduled: false, player: null }};
	    const key = `${{location.href}}::${{guideIndex ?? "any"}}`;
	    const now = Date.now();
	    if (window.__xiaoluxueActivatedPlayerKey === key && targetVideoPlaying()) {{
	      return {{ scheduled: false, player, rateControl: trySetControllerRate() }};
	    }}
	    if (window.__xiaoluxueActivatedPlayerKey === key && now - (window.__xiaoluxueLastPlayerClickAt || 0) < 700) {{
	      return {{ scheduled: false, waiting: true, player, rateControl: trySetControllerRate() }};
	    }}
	    window.__xiaoluxueActivatedPlayerKey = key;
	    window.__xiaoluxueLastPlayerClickAt = now;
	    window.__xiaoluxuePendingPlayerActivate = false;
	    setTimeout(() => {{
	      click(player);
	      setTimeout(() => trySetControllerRate(), 60);
	      setTimeout(() => trySetControllerRate(), 180);
	    }}, 0);
	    return {{ scheduled: true, player, rateControl: trySetControllerRate() }};
	  }};
	  window.__xiaoluxueDesiredPlaybackRate = rate;
	  if (guideIndex != null) {{
	    window.__xiaoluxuePendingGuideIndex = guideIndex;
	  }}
	  if (activatePlayer) {{
	    window.__xiaoluxuePendingPlayerActivate = true;
	  }} else if (document.getElementById("__xiaoluxue-turbo-video")) {{
	    window.__xiaoluxuePendingPlayerActivate = false;
	  }}
	  window.__xiaoluxueRateTick = () => {{
	    const videos = apply();
	    const activation = tryActivate();
	    const rateControl = trySetControllerRate();
	    return {{ videos, activation, rateControl }};
	  }};
	  const ensureObserver = () => {{
	    const root = document.documentElement || document.body;
	    if (!root) {{
	      setTimeout(ensureObserver, 30);
	      return false;
	    }}
	    if (window.__xiaoluxueRateObserver) {{
	      try {{ window.__xiaoluxueRateObserver.disconnect(); }} catch (_error) {{}}
	    }}
	    window.__xiaoluxueRateObserver = new MutationObserver(() => window.__xiaoluxueRateTick?.());
	    window.__xiaoluxueRateObserver.observe(root, {{ childList: true, subtree: true }});
	    return true;
	  }};
	  const observerReady = ensureObserver();
	  if (!window.__xiaoluxueRatePlayListenerInstalled) {{
	    window.__xiaoluxueRatePlayListenerInstalled = true;
	    document.addEventListener(
	      "play",
	      (event) => {{
        const target = event.target;
        if (target && target.tagName === "VIDEO") {{
          try {{
            target.defaultPlaybackRate = window.__xiaoluxueDesiredPlaybackRate || rate;
            target.playbackRate = window.__xiaoluxueDesiredPlaybackRate || rate;
          }} catch (_error) {{}}
        }}
      }},
	      true
	    );
	  }}
	  clearInterval(window.__xiaoluxueRateInterval);
	  window.__xiaoluxueRateInterval = setInterval(() => window.__xiaoluxueRateTick?.(), 160);
	  for (const delay of [30, 120, 350, 800, 1400]) {{
	    setTimeout(() => window.__xiaoluxueRateTick?.(), delay);
	  }}
	  const tick = window.__xiaoluxueRateTick();
	  const player = tick.activation?.player || findTargetPlayer();
	  const playerClickScheduled = Boolean(tick.activation?.scheduled);
	  const videos = tick.videos || [];
	  return {{
	    ok: true,
	    rate,
	    observerInstalled: true,
	    observerReady,
    activatePlayer,
    playerClicked: playerClickScheduled,
    playerClickScheduled,
    playerText: player ? text(player).slice(0, 80) : "",
    rateControl: tick.rateControl,
    videos,
  }};
}})()
"""


def xiaoluxue_prefetch_guide_expression(guide_url: str | None, avatar_url: str | None = None) -> str:
    guide_url_json = "null" if not guide_url else json.dumps(guide_url)
    avatar_url_json = "null" if not avatar_url else json.dumps(avatar_url)
    return f"""
(() => {{
  const guideUrl = {guide_url_json};
  const avatarUrl = {avatar_url_json};
  const prefetch = (url, options = {{}}) => {{
    if (!url) return false;
    try {{
      fetch(url, {{ cache: "force-cache", ...options }}).catch(() => null);
      return true;
    }} catch (_error) {{
      return false;
    }}
  }};
  const warmVideo = (_url) => false;
  const guideStarted = prefetch(guideUrl);
  if (guideUrl) {{
    fetch(guideUrl, {{ cache: "force-cache" }})
      .then((response) => response.clone().json())
      .then((data) => {{
        const url = data?.avatar?.url;
        if (url && !avatarUrl) warmVideo(url);
      }})
      .catch(() => null);
  }}
  const avatarStarted = false;
  return {{ ok: true, guideStarted, avatarStarted, guideUrl: Boolean(guideUrl), avatarUrl: Boolean(avatarUrl) }};
}})()
"""


def xiaoluxue_turbo_guide_player_expression(
    *,
    guide_index: int | None,
    guide_name: str | None,
    avatar_url: str | None,
    rate: float,
    enabled: bool,
) -> str:
    guide_index_json = "null" if guide_index is None else json.dumps(int(guide_index))
    guide_name_json = json.dumps(guide_name or "知识讲解")
    avatar_url_json = "null" if not avatar_url else json.dumps(avatar_url)
    rate_json = json.dumps(rate)
    enabled_json = json.dumps(enabled)
    return f"""
(() => {{
  const enabled = {enabled_json};
  const guideIndex = {guide_index_json};
  const guideName = {guide_name_json};
  const avatarUrl = {avatar_url_json};
  const rate = {rate_json};
  const rootId = "__xiaoluxue-turbo-guide";
  const videoId = "__xiaoluxue-turbo-video";
  const text = (value) => String(value || "").trim();
  const findWidget = () => {{
    if (guideIndex == null) return document.querySelector('[data-name^="widget-"]');
    return document.querySelector(`[data-name^="widget-${{guideIndex}}-"]`);
  }};
  const visible = (el) => {{
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 8 && rect.height > 8 && rect.bottom > 0 && rect.top < innerHeight;
  }};
  const nativeVideoPlaying = () =>
    [...document.querySelectorAll("video")]
      .filter((video) => video.id !== "__xiaoluxue-avatar-prewarm" && video.id !== videoId)
      .some((video) => visible(video) && !video.paused && video.readyState >= 2);
  const applyRate = () => {{
    for (const video of document.querySelectorAll("video")) {{
      try {{
        video.defaultPlaybackRate = rate;
        video.playbackRate = rate;
      }} catch (_error) {{}}
    }}
  }};
  const remove = (reason = "native-ready") => {{
    const node = document.getElementById(rootId);
    if (node) node.remove();
    return {{ ok: true, removed: true, reason }};
  }};
  const mount = () => {{
    if (!enabled || !avatarUrl) return {{ ok: true, skipped: true }};
    if (nativeVideoPlaying()) return remove();
    const widget = findWidget();
    const host = widget || document.body || document.documentElement;
    if (!host) return {{ ok: true, mounted: false, reason: "host-missing" }};
    let root = document.getElementById(rootId);
    if (!root) {{
      root = document.createElement("div");
      root.id = rootId;
      root.dataset.name = "xiaoluxue-turbo-guide-player";
      root.style.cssText = [
        widget ? "position:absolute" : "position:fixed",
        "inset:0",
        widget ? "z-index:45" : "z-index:99999",
        "background:#FAF8F6",
        "color:#312d29",
        "font-family:system-ui,-apple-system,BlinkMacSystemFont,sans-serif",
        "overflow:hidden",
        "pointer-events:none"
      ].join(";");
      root.innerHTML = `
        <div style="position:absolute;left:54px;top:44px;right:300px;font-size:28px;font-weight:700;line-height:1.25;">${{text(guideName)}}</div>
        <div style="position:absolute;left:54px;top:96px;right:330px;font-size:18px;line-height:1.8;color:#57504a;">集合与元素的定义</div>
        <video id="${{videoId}}" src="${{avatarUrl}}" autoplay muted playsinline loop
          style="position:absolute;right:24px;bottom:30px;width:190px;height:190px;border-radius:8px;background:#efe2d6;object-fit:cover;box-shadow:0 1px 4px rgba(64,43,26,.08);"></video>
      `;
      host.appendChild(root);
    }}
    const video = document.getElementById(videoId);
    if (video) {{
      try {{
        video.defaultPlaybackRate = rate;
        video.playbackRate = rate;
        video.muted = true;
        video.playsInline = true;
        video.play?.().catch(() => null);
      }} catch (_error) {{}}
    }}
    applyRate();
    if (!window.__xiaoluxueTurboRateObserverInstalled) {{
      window.__xiaoluxueTurboRateObserverInstalled = true;
      document.addEventListener("play", applyRate, true);
      const observerRoot = document.documentElement || document.body;
      if (observerRoot) {{
        const rateObserver = new MutationObserver(applyRate);
        rateObserver.observe(observerRoot, {{ childList: true, subtree: true }});
        window.__xiaoluxueTurboRateObserver = rateObserver;
      }}
    }}
    return {{
      ok: true,
      mounted: true,
      bridge: "video",
      host: widget ? "widget" : "body",
      video: Boolean(video),
    }};
  }};
  window.__xiaoluxueTurboGuideTick = mount;
  clearInterval(window.__xiaoluxueTurboGuideInterval);
  window.__xiaoluxueTurboGuideInterval = setInterval(mount, 90);
  for (const delay of [0, 60, 160, 320, 700, 1200, 2400, 4200, 7000]) {{
    setTimeout(mount, delay);
  }}
  const root = document.documentElement || document.body;
  if (root) {{
    if (window.__xiaoluxueTurboGuideObserver) {{
      try {{ window.__xiaoluxueTurboGuideObserver.disconnect(); }} catch (_error) {{}}
    }}
    window.__xiaoluxueTurboGuideObserver = new MutationObserver(mount);
    window.__xiaoluxueTurboGuideObserver.observe(root, {{ childList: true, subtree: true }});
  }}
  return mount();
}})()
"""


def xiaoluxue_course_fetch_optimizer_expression(target_guide_url: str | None) -> str:
    target_guide_url_json = "null" if not target_guide_url else json.dumps(target_guide_url)
    return f"""
(() => {{
  const targetGuideUrl = {target_guide_url_json};
  if (!targetGuideUrl) return {{ ok: true, skipped: true }};
  window.__xiaoluxueTargetGuideUrl = targetGuideUrl;
  if (!window.__xiaoluxueOriginalFetch) {{
    window.__xiaoluxueOriginalFetch = window.fetch.bind(window);
    window.fetch = (input, init) => {{
      const url = typeof input === "string" ? input : input?.url || "";
      const target = window.__xiaoluxueTargetGuideUrl;
      if (
        target &&
        typeof url === "string" &&
        url.includes("/lesson/video/json/") &&
        url !== target
      ) {{
        return new Promise(() => {{}});
      }}
      return window.__xiaoluxueOriginalFetch(input, init);
    }};
  }}
  return {{ ok: true, targetGuideUrl, patched: true }};
}})()
"""


def xiaoluxue_stop_non_course_loading_expression() -> str:
    return """
(() => {
  const before = { href: location.href, readyState: document.readyState, hasRouter: Boolean(window.next?.router) };
  const isCourse = (() => {
    try { return new URL(location.href).pathname.replace(/\/$/, "") === "/course"; } catch (_error) { return false; }
  })();
  if (document.readyState === "loading" && !isCourse) {
    try { window.stop(); } catch (_error) {}
  }
  return { ok: true, before, after: { href: location.href, readyState: document.readyState, hasRouter: Boolean(window.next?.router) } };
})()
"""


def xiaoluxue_goto_widget_expression(index: int, mode: str) -> str:
    index_json = json.dumps(index)
    mode_json = json.dumps(mode)
    return f"""
(() => {{
  const targetIndex = {index_json};
  const mode = {mode_json};
  const widget = document.querySelector(`[data-name^="widget-${{targetIndex}}-"]`);
  if (mode === "scroll") {{
    if (!widget) return {{ ok: false, mode, targetIndex, error: "widget not found" }};
    const container = widget.parentElement;
    if (container && typeof container.scrollTo === "function") {{
      container.scrollTo({{ top: widget.offsetTop, behavior: "instant" }});
    }} else {{
      widget.scrollIntoView({{ block: "start", behavior: "instant" }});
    }}
    return {{ ok: true, mode, targetIndex, dataName: widget.dataset.name || "", loaded: (widget.innerText || "").trim().length > 0 }};
  }}
  const url = new URL(location.href);
  url.searchParams.set("redirectWidgetIndex", String(targetIndex));
  location.href = url.toString();
  return {{ ok: true, mode: "reload", targetIndex, url: url.toString() }};
}})()
"""


def xiaoluxue_resolve_knowledge_guide_expression(
    *,
    subject_id: int,
    knowledge_index: str,
    knowledge_id: int | None,
    guide_widget_index: int | None,
) -> str:
    subject_id_json = json.dumps(subject_id)
    knowledge_index_json = json.dumps(knowledge_index)
    knowledge_id_json = "null" if knowledge_id is None else json.dumps(knowledge_id)
    guide_widget_index_json = "null" if guide_widget_index is None else json.dumps(guide_widget_index)
    return f"""
(async () => {{
  const subjectId = {subject_id_json};
  const requestedIndex = {knowledge_index_json};
  let knowledgeId = {knowledge_id_json};
  const requestedGuideIndex = {guide_widget_index_json};
  const gateway = {json.dumps(XIAOLUXUE_GW_ORIGIN)};
  const parse = (value) => {{
    try {{ return JSON.parse(value); }} catch (_error) {{ return null; }}
  }};
  const normalizeIndex = (value) => String(value || "").replace(/[^0-9]/g, "");
  const bridge = window.AndroidBridge;
  if (!bridge || typeof bridge.invokeSync !== "function") {{
    throw new Error("AndroidBridge is unavailable in this WebView.");
  }}
  const headers = parse(bridge.invokeSync("getNetworkHeaderParams", "{{}}"))?.data || {{}};
  const student = parse(bridge.invokeSync("getStudentUserInfo", "{{}}"))?.data || {{}};
  const fetchJson = async (url) => {{
    const response = await fetch(url, {{ headers }});
    let body;
    try {{ body = await response.json(); }} catch (_error) {{ body = await response.text(); }}
    return {{ ok: response.ok, status: response.status, body }};
  }};
  const enterFor = async (candidateKnowledgeId) => {{
    const query = new URLSearchParams({{
      subjectId: String(subjectId),
      studyType: "1",
      knowledgeId: String(candidateKnowledgeId),
      phaseId: String(student.phase || 3),
    }});
    const response = await fetchJson(`${{gateway}}/study-api/api/v1/study_session/enter?${{query.toString()}}`);
    const data = response.body?.data;
    return {{ response, data }};
  }};

  let source = knowledgeId ? "explicit" : "";
  let enter = knowledgeId ? await enterFor(knowledgeId) : null;
  if (!knowledgeId) {{
    const cardQuery = new URLSearchParams({{
      grade: String(student.classGrade || 11),
      schoolId: String(student.schoolId || 1),
      classSubjectType: String(student.classSubjectType || 1),
      phase: String(student.phase || 3),
    }});
    const cards = await fetchJson(`${{gateway}}/student-skeleton-api/api/v1/subjects/cards?${{cardQuery.toString()}}`);
    const subject = (cards.body?.data || []).find((item) => Number(item.subject) === subjectId);
    const candidate = Number(subject?.subjectPage?.studyBizTreeNode?.knowledgeNodeId || 0);
    if (candidate > 0) {{
      const candidateEnter = await enterFor(candidate);
      if (normalizeIndex(candidateEnter.data?.knowledgeIndex) === normalizeIndex(requestedIndex)) {{
        knowledgeId = candidate;
        enter = candidateEnter;
        source = "subject-card";
      }}
    }}
  }}
  if (!knowledgeId || !enter?.data?.studySessionUrl) {{
    throw new Error(`Could not resolve Xiaoluxue knowledge index ${{requestedIndex}} for subject ${{subjectId}}.`);
  }}

  const lessonQuery = new URLSearchParams({{ knowledgeId: String(knowledgeId) }});
  const lesson = await fetchJson(`${{gateway}}/study-api/api/v1/lesson/info?${{lessonQuery.toString()}}`);
  const widgets = lesson.body?.data?.lessonWidgets || [];
  const guideWidget =
    requestedGuideIndex != null
      ? widgets.find((widget) => Number(widget.widgetIndex) === Number(requestedGuideIndex)) || {{ widgetIndex: requestedGuideIndex }}
      : widgets.find((widget) => widget.widgetType === "guide" && Number(widget.widgetIndex) > 0) ||
        widgets.find((widget) => widget.widgetType === "guide") ||
        {{ widgetIndex: 0 }};
  const targetUrl = new URL(enter.data.studySessionUrl);
  targetUrl.searchParams.set("redirectWidgetIndex", String(guideWidget.widgetIndex ?? 0));
  return {{
    ok: true,
    source: source || "resolved",
    subjectId,
    requestedIndex,
    knowledgeId,
    knowledgeIndex: enter.data.knowledgeIndex,
    knowledgeName: enter.data.knowledgeName,
	    lessonId: enter.data.lessonId,
	    studySessionId: enter.data.studySessionId,
	    guideIndex: Number(guideWidget.widgetIndex ?? 0),
	    guideName: guideWidget.widgetName || "",
	    guideCdnUrl: guideWidget.cdnUrl || "",
	    targetUrl: targetUrl.toString(),
	  }};
}})()
"""


def xiaoluxue_route_course_expression(target_url: str, *, prefer_client_route: bool) -> str:
    target_url_json = json.dumps(target_url)
    prefer_client_route_json = json.dumps(prefer_client_route)
    return f"""
	(() => {{
	  const targetUrl = {target_url_json};
	  const preferClientRoute = {prefer_client_route_json};
	  const beforeUrl = location.href;
	  if (preferClientRoute) {{
	    try {{
	      const target = new URL(targetUrl);
	      if (target.origin === location.origin) {{
	        const router = window.next?.router;
	        if (router && typeof router.replace === "function") {{
	          setTimeout(() => {{
	            try {{
	              router.replace(target.toString());
	            }} catch (_error) {{
	              location.replace(target.toString());
	            }}
	          }}, 0);
	          return {{ ok: true, mode: "next-router-replace", scheduled: true, beforeUrl, url: target.toString(), href: location.href }};
	        }}
	        setTimeout(() => {{
	          history.pushState(null, "", target.toString());
	          window.dispatchEvent(new PopStateEvent("popstate", {{ state: null }}));
	          window.dispatchEvent(new Event("pushstate"));
	        }}, 0);
	        return {{ ok: true, mode: "client-route", scheduled: true, beforeUrl, url: target.toString(), href: location.href }};
	      }}
	    }} catch (_error) {{}}
	  }}
	  setTimeout(() => location.replace(targetUrl), 0);
	  return {{ ok: true, mode: "reload", scheduled: true, beforeUrl, url: targetUrl, href: location.href }};
	}})()
	"""


def xiaoluxue_course_ready_expression(knowledge_id: int, guide_index: int | None) -> str:
    knowledge_id_json = json.dumps(str(knowledge_id))
    guide_index_json = "null" if guide_index is None else json.dumps(int(guide_index))
    return f"""
(() => {{
  const knowledgeId = {knowledge_id_json};
  const guideIndex = {guide_index_json};
  const rect = (el) => {{
    const r = el.getBoundingClientRect();
    return {{ top: r.top, bottom: r.bottom, left: r.left, right: r.right, width: r.width, height: r.height }};
  }};
  const text = (el) => (el?.innerText || el?.textContent || "").trim().replace(/\\s+/g, " ");
  const visible = (el) => {{
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight;
  }};
  const widgets = [...document.querySelectorAll('[data-name^="widget-"]')];
  const visibleWidget = widgets.find(visible) || null;
  const targetWidget =
    guideIndex == null ? visibleWidget : document.querySelector(`[data-name^="widget-${{guideIndex}}-"]`);
  const videos = [...document.querySelectorAll("video")];
  const params = Object.fromEntries(new URL(location.href).searchParams.entries());
  const targetLoaded = Boolean(targetWidget && (text(targetWidget).length > 0 || targetWidget.querySelector('[data-name="guide-player"], video')));
  return {{
    ok: params.knowledgeId === knowledgeId,
    href: location.href,
    readyState: document.readyState,
    params,
    bodyTextLength: text(document.body).length,
    guideIndex,
    targetWidget: targetWidget ? {{ dataName: targetWidget.dataset.name || "", loaded: targetLoaded, rect: rect(targetWidget) }} : null,
    visibleWidget: visibleWidget ? {{ dataName: visibleWidget.dataset.name || "", text: text(visibleWidget).slice(0, 160), rect: rect(visibleWidget) }} : null,
    guidePlayerVisible: Boolean(document.querySelector('[data-name="guide-player"]')),
    videos: videos.map((video) => ({{ playbackRate: video.playbackRate, paused: video.paused, currentTime: video.currentTime }})),
  }};
}})()
"""


def cdp_eval_value(page: dict[str, Any], expression: str, *, timeout: int | float = 10) -> Any:
    evaluation = cdp_runtime_evaluate(str(page["webSocketDebuggerUrl"]), expression, timeout=timeout)
    return evaluation.get("value")


def xiaoluxue_runtime_url_matches(target_url: str, runtime_href: str) -> bool:
    try:
        target = urllib.parse.urlparse(target_url)
        current = urllib.parse.urlparse(runtime_href)
    except Exception:
        return False
    if (target.hostname or "").casefold() != (current.hostname or "").casefold():
        return False
    if target.path.rstrip("/") != current.path.rstrip("/"):
        return False
    target_query = urllib.parse.parse_qs(target.query)
    current_query = urllib.parse.parse_qs(current.query)
    for key in ("knowledgeId", "lessonId", "studySessionId"):
        if target_query.get(key) and target_query.get(key) != current_query.get(key):
            return False
    return True


def xiaoluxue_rebase_h5_url(target_url: str, runtime_href: str | None) -> str:
    if not runtime_href:
        return target_url
    try:
        target = urllib.parse.urlparse(target_url)
        current = urllib.parse.urlparse(runtime_href)
    except Exception:
        return target_url
    if not is_xiaoluxue_h5_host(target.hostname or "") or not is_xiaoluxue_h5_host(current.hostname or ""):
        return target_url
    if (target.hostname or "").casefold() == (current.hostname or "").casefold() and target.scheme == current.scheme:
        return target_url
    return urllib.parse.urlunparse(
        (
            current.scheme or target.scheme,
            current.netloc or target.netloc,
            target.path,
            target.params,
            target.query,
            target.fragment,
        )
    )


def xiaoluxue_wait_for_app_url(serial: str, target_url: str, deadline: float, *, poll_interval: float = 0.04) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(target_url)
    host = parsed.hostname or XIAOLUXUE_SITE_URL_MARKER
    page_kind = "course" if "/course" in parsed.path else "exercise" if "/exercise" in parsed.path else "any"
    cached = xiaoluxue_cached_page(serial, page_kind, runtime_contains=host)
    if cached and xiaoluxue_runtime_url_matches(target_url, str(cached.get("runtimeHref") or cached.get("url") or "")):
        return cached
    last_page: dict[str, Any] | None = None
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        pages = discover_webview_pages(serial)
        candidates = [page for page in pages if not page.get("error")]
        candidates.sort(key=webview_page_score, reverse=True)
        for page in candidates:
            if "小鹿爱学" not in str(page.get("title") or "") and "xiaoluxue" not in str(page.get("url") or ""):
                continue
            try:
                runtime_href = cdp_eval_value(page, "location.href", timeout=0.35)
            except Exception as exc:
                last_error = exc
                runtime_href = str(page.get("url") or "")
            if isinstance(runtime_href, str) and xiaoluxue_runtime_url_matches(target_url, runtime_href):
                remembered = xiaoluxue_remember_page(serial, page_kind, page)
                xiaoluxue_remember_page(serial, "any", page)
                return {**remembered, "runtimeHref": runtime_href}
            if isinstance(runtime_href, str) and "xiaoluxue" in runtime_href:
                last_page = {**page, "runtimeHref": runtime_href}
        time.sleep(poll_interval)
    if last_page is not None:
        raise AndroidUseError(f"Xiaoluxue WebView opened but target URL was not active: {target_url}")
    if last_error:
        raise last_error
    raise AndroidUseError(f"Could not find Xiaoluxue WebView for URL: {target_url}")


def xiaoluxue_runtime_bridge_expression(*, reveal_overlay: bool = False) -> str:
    reveal_overlay_json = json.dumps(reveal_overlay)
    return f"""
(() => {{
  const rect = (el) => {{
    const r = el.getBoundingClientRect();
    return {{ x: r.x, y: r.y, width: r.width, height: r.height, top: r.top, right: r.right, bottom: r.bottom, left: r.left }};
  }};
  const text = (el) => (el?.innerText || el?.textContent || "").trim().replace(/\\s+/g, " ");
  const visible = (el) => {{
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && style.visibility !== "hidden" && style.display !== "none";
  }};
  const click = (el) => {{
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const init = {{
      bubbles: true,
      cancelable: true,
      view: window,
      clientX: r.left + r.width / 2,
      clientY: r.top + r.height / 2,
      pointerType: "touch",
    }};
    el.dispatchEvent(new PointerEvent("pointerdown", init));
    el.dispatchEvent(new MouseEvent("mousedown", init));
    el.dispatchEvent(new PointerEvent("pointerup", init));
    el.dispatchEvent(new MouseEvent("mouseup", init));
    el.dispatchEvent(new MouseEvent("click", init));
    return true;
  }};
  const revealOverlays = () => {{
    const target = document.elementFromPoint(innerWidth / 2, innerHeight / 2) || document.body || document.documentElement;
    return {{ ok: click(target), target: target ? {{ tagName: target.tagName, text: text(target).slice(0, 80), rect: rect(target) }} : null }};
  }};
  const snapshot = () => {{
    const widgets = [...document.querySelectorAll('[data-name^="widget-"]')].map((el) => {{
      const match = /^widget-(\\d+)-(.+)$/.exec(el.dataset.name || "");
      return {{
        dataName: el.dataset.name || "",
        index: match ? Number(match[1]) : null,
        name: match ? match[2] : "",
        text: text(el).slice(0, 180),
        rect: rect(el),
        visible: visible(el),
      }};
    }});
    const buttons = [...document.querySelectorAll('button,[role="button"],ol')]
      .filter(visible)
      .map((el) => ({{ text: text(el), dataName: el.dataset.name || "", rect: rect(el) }}))
      .filter((item) => item.text)
      .slice(0, 80);
    const videos = [...document.querySelectorAll("video")].map((video) => ({{
      src: video.currentSrc || video.src || "",
      currentTime: video.currentTime,
      duration: Number.isFinite(video.duration) ? video.duration : null,
      playbackRate: video.playbackRate,
      paused: video.paused,
      rect: rect(video),
    }}));
    const params = Object.fromEntries(new URL(location.href).searchParams.entries());
    return {{
      ok: true,
      bridgeVersion: 1,
      title: document.title,
      href: location.href,
      readyState: document.readyState,
      params,
      viewport: {{ width: innerWidth, height: innerHeight, devicePixelRatio }},
      page: location.pathname.includes("/exercise") ? "exercise" : location.pathname.includes("/course") ? "course" : "xiaoluxue",
      guidePlayerVisible: Boolean(document.querySelector('[data-name="guide-player"]')),
      guideControlsVisible: Boolean(document.querySelector('[data-name="guide-controller::follow"]')),
      widgets,
      buttons,
      videos,
    }};
  }};
  const clickByText = (needle) => {{
    const target = [...document.querySelectorAll('button,[role="button"],ol,li,div,span')]
      .filter(visible)
      .find((el) => text(el).includes(String(needle || "")));
    return {{ ok: click(target), text: target ? text(target) : "", rect: target ? rect(target) : null }};
  }};
  const setPlaybackRate = (rate = 2) => {{
    const videos = [...document.querySelectorAll("video")];
    for (const video of videos) {{
      try {{
        video.defaultPlaybackRate = rate;
        video.playbackRate = rate;
      }} catch (_error) {{}}
    }}
    return {{ ok: true, rate, videos: videos.map((video) => ({{ playbackRate: video.playbackRate, paused: video.paused, currentTime: video.currentTime }})) }};
  }};
  const gotoCourseWidget = (index, mode = "reload") => {{
    const widgetIndex = Number(index);
    if (!Number.isFinite(widgetIndex)) return {{ ok: false, reason: "invalid-index", index }};
    if (mode === "scroll") {{
      const node = document.querySelector(`[data-name^="widget-${{widgetIndex}}-"]`);
      if (!node) return {{ ok: false, reason: "widget-not-found", index: widgetIndex }};
      node.scrollIntoView({{ block: "start", inline: "nearest", behavior: "instant" }});
      return {{ ok: true, mode, index: widgetIndex, href: location.href }};
    }}
    const url = new URL(location.href);
    url.searchParams.set("redirectWidgetIndex", String(widgetIndex));
    location.href = url.toString();
    return {{ ok: true, mode: "reload", index: widgetIndex, href: url.toString() }};
  }};
  window.__androidUse = {{
    ...(window.__androidUse || {{}}),
    xiaoluxue: {{ snapshot, revealOverlays, clickByText, setPlaybackRate, gotoCourseWidget }},
  }};
  const reveal = {reveal_overlay_json} ? revealOverlays() : {{ ok: true, skipped: true }};
  return {{ ok: true, installed: true, reveal, snapshot: snapshot() }};
}})()
"""


def xiaoluxue_runtime_page_for_kind(serial: str, page_kind: str, *, open_app_if_needed: bool = True) -> dict[str, Any]:
    if page_kind == "course":
        return xiaoluxue_page(serial)
    if page_kind == "exercise":
        return xiaoluxue_exercise_page(serial)
    return xiaoluxue_any_page(serial, open_app_if_needed=open_app_if_needed, timeout_sec=3)


def tool_xiaoluxue_runtime_status(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    started_at = time.monotonic()
    page_kind = str(args.get("page") or "any")
    if page_kind not in {"any", "course", "exercise"}:
        raise AndroidUseError("page must be one of: any, course, exercise.")
    page = xiaoluxue_runtime_page_for_kind(serial, page_kind, open_app_if_needed=bool(args.get("open_app_if_needed", True)))
    timings = {"select_page_sec": round(time.monotonic() - started_at, 3)}
    timeout = min(float(args.get("timeout_sec", 4)), 20)
    runtime: Any
    if bool(args.get("inject_bridge", True)):
        runtime = cdp_eval_value(
            page,
            xiaoluxue_runtime_bridge_expression(reveal_overlay=bool(args.get("reveal_overlay", False))),
            timeout=timeout,
        )
    else:
        runtime = cdp_eval_value(
            page,
            "window.__androidUse?.xiaoluxue?.snapshot ? window.__androidUse.xiaoluxue.snapshot() : ({ ok: true, href: location.href, title: document.title, readyState: document.readyState })",
            timeout=timeout,
        )
    timings["runtime_sec"] = round(time.monotonic() - started_at, 3)
    return [
        text_content(
            {
                "ok": True,
                "serial": serial,
                "page": {
                    "id": page.get("id"),
                    "title": page.get("title"),
                    "url": page.get("url"),
                    "runtimeHref": page.get("runtimeHref"),
                    "socket": page.get("socket"),
                    "forward": page.get("forward"),
                    "cacheHit": bool(page.get("cacheHit")),
                },
                "timings": timings,
                "runtime": runtime,
            }
        )
    ]


def tool_xiaoluxue_open_app_url(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    url = str(args["url"]).strip()
    if not url:
        raise AndroidUseError("url must not be empty.")
    if not is_xiaoluxue_app_only_url(url):
        raise AndroidUseError("xiaoluxue_open_app_url only accepts Xiaoluxue app-only URLs.")
    started_at = time.monotonic()
    timeout = min(float(args.get("timeout_sec", 5)), 30)
    before = capture_record_snapshot(serial) if active_recording(serial) else None
    route = xiaoluxue_route_app_url(serial, url, force_stop=bool(args.get("force_stop", False)))
    page: dict[str, Any] | None = None
    runtime: Any = None
    if bool(args.get("wait_for_webview", True)):
        page = xiaoluxue_wait_for_app_url(serial, url, started_at + timeout)
        if bool(args.get("inject_bridge", True)):
            runtime = cdp_eval_value(
                page,
                xiaoluxue_runtime_bridge_expression(reveal_overlay=bool(args.get("reveal_overlay", False))),
                timeout=min(max(started_at + timeout - time.monotonic(), 0.5), 3),
            )
    result = {
        "ok": True,
        "action": "xiaoluxue_open_app_url",
        "elapsed_sec": round(time.monotonic() - started_at, 3),
        "route": route,
        "page": None
        if page is None
        else {
            "id": page.get("id"),
            "title": page.get("title"),
            "url": page.get("url"),
            "runtimeHref": page.get("runtimeHref"),
            "socket": page.get("socket"),
            "forward": page.get("forward"),
            "cacheHit": bool(page.get("cacheHit")),
        },
        "runtime": runtime,
    }
    append_recording_step(
        serial,
        "xiaoluxue_open_app_url",
        {
            "url": url,
            "wait_for_webview": bool(args.get("wait_for_webview", True)),
            "inject_bridge": bool(args.get("inject_bridge", True)),
            "reveal_overlay": bool(args.get("reveal_overlay", False)),
            "force_stop": bool(args.get("force_stop", False)),
        },
        result,
        before=before,
    )
    return [text_content({"ok": True, "serial": serial, **result})]


def xiaoluxue_login_resource_id(name: str) -> str:
    return f"{XIAOLUXUE_STUDENT_PACKAGE}:id/{name}"


def xiaoluxue_login_node_by_resource(nodes: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    resource_id = xiaoluxue_login_resource_id(name)
    for node in nodes:
        if str(node.get("resource_id") or "") == resource_id:
            return node
    return None


def xiaoluxue_login_input_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    resource_id = xiaoluxue_login_resource_id("edit_input")
    inputs = [node for node in nodes if str(node.get("resource_id") or "") == resource_id and node.get("center")]
    return sorted(inputs, key=lambda node: int((node.get("bounds") or {}).get("top") or 0))


def xiaoluxue_login_click_node(serial: str, node: dict[str, Any] | None, description: str) -> dict[str, int]:
    point = node_click_point(node) if node else None
    if not point:
        raise AndroidUseError(f"Could not find Xiaoluxue login control: {description}")
    adb(["shell", "input", "tap", str(point["x"]), str(point["y"])], serial=serial, timeout=10)
    return point


def xiaoluxue_login_observe(serial: str) -> dict[str, Any]:
    observation = observe_ui(serial, limit=260)
    focus = str(observation.get("state", {}).get("focused_window") or "")
    if XIAOLUXUE_LOGIN_ACTIVITY not in focus:
        raise AndroidUseError(f"Current Xiaoluxue screen is not LoginActivity. focused_window={focus}")
    return observation


def run_xiaoluxue_login_fast_path(serial: str, args: dict[str, Any], *, record: bool) -> dict[str, Any]:
    account = str(args.get("account") or "").strip()
    password = str(args.get("password") or "")
    if not account:
        raise AndroidUseError("account is required.")
    if not password:
        raise AndroidUseError("password is required.")

    started_at = time.monotonic()
    timeout = min(float(args.get("timeout_sec", 5.0)), 30)
    deadline = started_at + timeout
    steps: list[dict[str, Any]] = []

    focus = get_focused_window(serial) or ""
    if XIAOLUXUE_LOGIN_ACTIVITY not in focus:
        if bool(args.get("open_app_if_needed", True)):
            adb(["shell", "monkey", "-p", XIAOLUXUE_STUDENT_PACKAGE, "-c", "android.intent.category.LAUNCHER", "1"], serial=serial, timeout=10)
            time.sleep(min(max(float(args.get("after_open_wait_sec", 0.25)), 0), 2))
            focus = get_focused_window(serial) or ""
        if XIAOLUXUE_LOGIN_ACTIVITY not in focus:
            already_in_student = f"{XIAOLUXUE_STUDENT_PACKAGE}/" in focus
            return {
                "ok": True,
                "action": "xiaoluxue_login_fast_path",
                "already_logged_in": already_in_student,
                "focused_window": focus,
                "elapsed_sec": round(time.monotonic() - started_at, 3),
                "steps": steps,
            }

    observation = xiaoluxue_login_observe(serial)
    nodes = observation["ui"]["nodes"]
    inputs = xiaoluxue_login_input_nodes(nodes)
    if len(inputs) < 2:
        raise AndroidUseError(f"Expected account and password fields on Xiaoluxue login page, got {len(inputs)}.")

    account_node = inputs[0]
    current_account = str(account_node.get("text") or "").strip()
    if current_account != account:
        point = xiaoluxue_login_click_node(serial, account_node, "account input")
        method = type_focused_text_fast(serial, account, clear_first=True, clear_count=40)
        steps.append({"action": "fill_account", "chars": len(account), "method": method, "point": point})
        observation = xiaoluxue_login_observe(serial)
        nodes = observation["ui"]["nodes"]
    else:
        steps.append({"action": "fill_account", "skipped": "already-filled", "chars": len(account)})

    inputs = xiaoluxue_login_input_nodes(nodes)
    if len(inputs) < 2:
        raise AndroidUseError("Password field disappeared from Xiaoluxue login page.")
    password_node = inputs[1]
    point = xiaoluxue_login_click_node(serial, password_node, "password input")
    method = type_focused_text_fast(serial, password, clear_first=True, clear_count=max(20, len(password) + 8))
    steps.append({"action": "fill_password", "chars": len(password), "method": method, "point": point})

    observation = xiaoluxue_login_observe(serial)
    nodes = observation["ui"]["nodes"]
    agreement_node = xiaoluxue_login_node_by_resource(nodes, "cb_agreement")
    if agreement_node and not bool(agreement_node.get("checked")):
        point = xiaoluxue_login_click_node(serial, agreement_node, "agreement checkbox")
        steps.append({"action": "check_agreement", "point": point})
        observation = xiaoluxue_login_observe(serial)
        nodes = observation["ui"]["nodes"]
    else:
        steps.append({"action": "check_agreement", "skipped": "already-checked" if agreement_node else "not-found"})

    login_node = xiaoluxue_login_node_by_resource(nodes, "button") or find_ui_node(nodes, "登录", exact=True)
    point = xiaoluxue_login_click_node(serial, login_node, "login button")
    submitted_at = time.monotonic()
    steps.append({"action": "submit", "point": point})

    final_focus = ""
    while time.monotonic() < deadline:
        final_focus = get_focused_window(serial) or ""
        if f"{XIAOLUXUE_STUDENT_PACKAGE}/" in final_focus and XIAOLUXUE_LOGIN_ACTIVITY not in final_focus:
            result = {
                "ok": True,
                "action": "xiaoluxue_login_fast_path",
                "already_logged_in": False,
                "focused_window": final_focus,
                "elapsed_sec": round(time.monotonic() - started_at, 3),
                "submit_to_home_sec": round(time.monotonic() - submitted_at, 3),
                "steps": steps,
            }
            if record:
                append_recording_step(
                    serial,
                    "xiaoluxue_login_fast_path",
                    {
                        "account_chars": len(account),
                        "password_chars": len(password),
                        "password_redacted": True,
                        "timeout_sec": timeout,
                    },
                    result,
                )
            return result
        time.sleep(0.1)
    raise AndroidUseError(
        f"Xiaoluxue login did not reach home within {timeout:.1f}s. focused_window={final_focus or focus}"
    )


def tool_xiaoluxue_login_fast_path(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    result = run_xiaoluxue_login_fast_path(serial, args, record=True)
    return [text_content({"ok": True, "serial": serial, **result})]


def tool_xiaoluxue_course_snapshot(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    page = xiaoluxue_page(serial)
    snapshot = cdp_eval_value(page, xiaoluxue_snapshot_expression(), timeout=min(float(args.get("timeout_sec", 10)), 60))
    return [text_content({"ok": True, "serial": serial, "pageId": page.get("id"), "socket": page.get("socket"), "snapshot": snapshot})]


def tool_xiaoluxue_set_speed(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    rate = float(args.get("rate", 2.0))
    if rate <= 0 or rate > 4:
        raise AndroidUseError("rate must be between 0 and 4.")
    page = xiaoluxue_page(serial)
    result = cdp_eval_value(page, xiaoluxue_set_speed_expression(rate), timeout=min(float(args.get("timeout_sec", 10)), 60))
    append_recording_step(
        serial,
        "xiaoluxue_set_speed",
        {"rate": rate},
        {"ok": True, "action": "xiaoluxue_set_speed", "rate": rate, "webview": True, "result": result},
    )
    return [text_content({"ok": True, "serial": serial, "pageId": page.get("id"), "socket": page.get("socket"), "result": result})]


def resolve_xiaoluxue_widget_index(snapshot: dict[str, Any], args: dict[str, Any]) -> int:
    if args.get("last"):
        indexes = [int(widget["index"]) for widget in snapshot.get("widgets", []) if isinstance(widget, dict) and widget.get("index") is not None]
        if not indexes:
            raise AndroidUseError("No widgets found on current Xiaoluxue course page.")
        return max(indexes)
    if args.get("index") is not None:
        return int(args["index"])
    name_contains = str(args.get("name_contains") or "").strip()
    if name_contains:
        for widget in snapshot.get("widgets", []):
            if isinstance(widget, dict) and name_contains in str(widget.get("name", "")):
                return int(widget["index"])
        raise AndroidUseError(f"No widget name contains: {name_contains}")
    raise AndroidUseError("Pass one of index, name_contains, or last=true.")


def tool_xiaoluxue_goto_widget(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    page = xiaoluxue_page(serial)
    snapshot = cdp_eval_value(page, xiaoluxue_snapshot_expression(), timeout=min(float(args.get("timeout_sec", 10)), 60))
    if not isinstance(snapshot, dict):
        raise AndroidUseError("Could not read Xiaoluxue course snapshot.")
    index = resolve_xiaoluxue_widget_index(snapshot, args)
    mode = str(args.get("mode") or "reload")
    if mode not in {"reload", "scroll"}:
        raise AndroidUseError("mode must be 'reload' or 'scroll'.")
    result = cdp_eval_value(page, xiaoluxue_goto_widget_expression(index, mode), timeout=min(float(args.get("timeout_sec", 10)), 60))
    append_recording_step(
        serial,
        "xiaoluxue_goto_widget",
        {"index": index, "mode": mode},
        {"ok": True, "action": "xiaoluxue_goto_widget", "index": index, "mode": mode, "webview": True, "result": result},
    )
    return [text_content({"ok": True, "serial": serial, "pageId": page.get("id"), "socket": page.get("socket"), "index": index, "mode": mode, "result": result})]


def first_xiaoluxue_widget_index(snapshot: dict[str, Any], terms: list[str]) -> int | None:
    for term in terms:
        for widget in snapshot.get("widgets", []):
            if isinstance(widget, dict) and term in str(widget.get("name", "")):
                return int(widget["index"])
    return None


def run_xiaoluxue_course_fast_path(serial: str, args: dict[str, Any], *, record: bool) -> dict[str, Any]:
    timeout = min(float(args.get("timeout_sec", 15)), 60)
    wait_sec = min(max(float(args.get("after_navigation_wait_sec", 2.0)), 0), 10)
    set_speed = bool(args.get("set_speed", True))
    rate = float(args.get("rate", 2.0))
    if rate <= 0 or rate > 4:
        raise AndroidUseError("rate must be between 0 and 4.")

    page = xiaoluxue_page(serial)
    snapshot = cdp_eval_value(page, xiaoluxue_snapshot_expression(), timeout=timeout)
    if not isinstance(snapshot, dict):
        raise AndroidUseError("Could not read Xiaoluxue course snapshot.")

    steps: list[dict[str, Any]] = []
    guide_index: int | None = None
    if args.get("guide_index") is not None:
        guide_index = int(args["guide_index"])
    elif str(args.get("guide_name_contains") or "").strip():
        guide_index = resolve_xiaoluxue_widget_index(snapshot, {"name_contains": str(args["guide_name_contains"]).strip()})
    elif set_speed and not snapshot.get("guidePlayerVisible"):
        guide_index = first_xiaoluxue_widget_index(snapshot, ["知识讲解", "讲解"])

    if guide_index is not None:
        guide_mode = str(args.get("guide_mode") or "reload")
        if guide_mode not in {"reload", "scroll"}:
            raise AndroidUseError("guide_mode must be 'reload' or 'scroll'.")
        guide_result = cdp_eval_value(page, xiaoluxue_goto_widget_expression(guide_index, guide_mode), timeout=timeout)
        steps.append({"action": "goto_guide", "index": guide_index, "mode": guide_mode, "result": guide_result})
        if guide_mode == "reload" and wait_sec:
            time.sleep(wait_sec)
            page = xiaoluxue_page(serial)

    if set_speed:
        speed_result = cdp_eval_value(page, xiaoluxue_set_speed_expression(rate), timeout=timeout)
        steps.append({"action": "set_speed", "rate": rate, "result": speed_result})

    target_args: dict[str, Any] = {}
    if args.get("target_index") is not None:
        target_args["index"] = int(args["target_index"])
    elif str(args.get("target_name_contains") or "").strip():
        target_args["name_contains"] = str(args["target_name_contains"]).strip()
    elif bool(args.get("target_last", True)):
        target_args["last"] = True

    if target_args:
        page = xiaoluxue_page(serial)
        snapshot = cdp_eval_value(page, xiaoluxue_snapshot_expression(), timeout=timeout)
        if not isinstance(snapshot, dict):
            raise AndroidUseError("Could not read Xiaoluxue course snapshot before target jump.")
        target_index = resolve_xiaoluxue_widget_index(snapshot, target_args)
        target_mode = str(args.get("target_mode") or "reload")
        if target_mode not in {"reload", "scroll"}:
            raise AndroidUseError("target_mode must be 'reload' or 'scroll'.")
        target_result = cdp_eval_value(page, xiaoluxue_goto_widget_expression(target_index, target_mode), timeout=timeout)
        steps.append({"action": "goto_target", "index": target_index, "mode": target_mode, "result": target_result})

    result = {
        "ok": True,
        "action": "xiaoluxue_course_fast_path",
        "webview": True,
        "pageId": page.get("id"),
        "socket": page.get("socket"),
        "steps": steps,
    }
    if record:
        recorded_args = {
            key: args[key]
            for key in (
                "guide_index",
                "guide_name_contains",
                "guide_mode",
                "set_speed",
                "rate",
                "target_index",
                "target_name_contains",
                "target_last",
                "target_mode",
                "after_navigation_wait_sec",
                "timeout_sec",
            )
            if key in args
        }
        append_recording_step(serial, "xiaoluxue_course_fast_path", recorded_args, result)
    return result


def tool_xiaoluxue_course_fast_path(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    result = run_xiaoluxue_course_fast_path(serial, args, record=True)
    return [text_content({"ok": True, "serial": serial, **result})]


def run_xiaoluxue_open_knowledge_guide(serial: str, args: dict[str, Any], *, record: bool) -> dict[str, Any]:
    started_at = time.monotonic()
    timeout = min(float(args.get("timeout_sec", 8)), 60)
    deadline = started_at + timeout
    subject_id = int(args.get("subject_id", 2))
    knowledge_index = str(args.get("knowledge_index") or "1.1.11")
    normalized_index = normalize_xiaoluxue_knowledge_index(knowledge_index)
    knowledge_id = args.get("knowledge_id")
    resolved_knowledge_id: int | None = int(knowledge_id) if knowledge_id is not None else None
    shortcut: dict[str, Any] | None = None
    if resolved_knowledge_id is None:
        shortcut = XIAOLUXUE_KNOWLEDGE_SHORTCUTS.get((subject_id, normalized_index))
        if shortcut:
            resolved_knowledge_id = int(shortcut["knowledgeId"])
    guide_widget_index = args.get("guide_widget_index")
    resolved_guide_index = int(guide_widget_index) if guide_widget_index is not None else None
    rate = float(args.get("rate", 2.0))
    if rate <= 0 or rate > 4:
        raise AndroidUseError("rate must be between 0 and 4.")
    prefer_client_route = bool(args.get("prefer_client_route", True))
    native_entry_if_needed = bool(args.get("native_entry_if_needed", True))

    timings: dict[str, float] = {}
    native_entry_result: Any = {"attempted": False, "reason": "webview-already-available"}
    vessel_entry_result: Any = {"attempted": False, "reason": "webview-already-available"}
    page: dict[str, Any] | None = None
    try:
        page = xiaoluxue_any_page(serial, open_app_if_needed=False, timeout_sec=0.2)
    except Exception as first_page_error:
        can_open_app = bool(args.get("open_app_if_needed", True))
        shortcut_target_url = str(shortcut.get("targetUrl") or "") if shortcut else ""
        refresh_session_for_entry = bool(args.get("refresh_session", False))
        prefer_native_entry_first = bool(
            args.get(
                "prefer_native_entry_first",
                not bool(shortcut_target_url and not refresh_session_for_entry),
            )
        )
        fallback_native_after_vessel = bool(args.get("fallback_native_after_vessel", prefer_native_entry_first))
        entry_order = (
            ("native", "vessel")
            if prefer_native_entry_first
            else ("vessel", "native")
            if fallback_native_after_vessel
            else ("vessel",)
        )
        for entry_mode in entry_order:
            if page is not None:
                break
            if entry_mode == "native":
                if not (can_open_app and native_entry_if_needed):
                    continue
                try:
                    native_entry_timeout = min(max(float(args.get("native_entry_timeout_sec", 2.6)), 1.0), 5.0)
                    native_entry_result = xiaoluxue_open_native_course_entry(
                        serial,
                        subject_id=subject_id,
                        normalized_index=normalized_index,
                        timeout_sec=min(max(deadline - time.monotonic(), 1), native_entry_timeout),
                    )
                    page = native_entry_result["page"]
                except Exception as native_exc:
                    native_entry_result = {"attempted": True, "ok": False, "error": str(native_exc)}
            elif entry_mode == "vessel":
                if not (can_open_app and shortcut_target_url and not bool(args.get("refresh_session", False))):
                    continue
                try:
                    vessel_entry_timeout = min(max(float(args.get("vessel_entry_timeout_sec", 4.8)), 0.8), 8.0)
                    vessel_entry_result = xiaoluxue_open_vessel_course_page(
                        serial,
                        target_url=shortcut_target_url,
                        knowledge_id=int(shortcut["knowledgeId"]),
                        timeout_sec=min(max(deadline - time.monotonic(), 0.5), vessel_entry_timeout),
                        force_stop=bool(args.get("force_vessel_start", True)),
                        bootstrap_scripts=(
                            [
                                xiaoluxue_fast_rate_expression(
                                    rate,
                                    int(shortcut.get("guideIndex", 0)),
                                    activate_player=True,
                                ),
                                xiaoluxue_turbo_guide_player_expression(
                                    guide_index=int(shortcut.get("guideIndex", 0)),
                                    guide_name=str(shortcut.get("guideName") or "") or None,
                                    avatar_url=str(shortcut.get("avatarUrl") or "") or None,
                                    rate=rate,
                                    enabled=bool(args.get("turbo_preview", True)),
                                ),
                                xiaoluxue_prefetch_guide_expression(
                                    str(shortcut.get("guideCdnUrl") or "") or None,
                                    str(shortcut.get("avatarUrl") or "") or None,
                                ),
                            ]
                            if bool(args.get("preinject_vessel_bootstrap", False))
                            else None
                        ),
                    )
                    page = vessel_entry_result["page"]
                except Exception as vessel_exc:
                    vessel_entry_result = {"attempted": True, "ok": False, "error": str(vessel_exc)}
        if page is None and can_open_app and native_entry_if_needed:
            try:
                page = xiaoluxue_any_page(
                    serial,
                    open_app_if_needed=can_open_app,
                    timeout_sec=min(max(deadline - time.monotonic(), 0.5), 2),
                )
            except Exception:
                if isinstance(native_entry_result, dict) and native_entry_result.get("attempted"):
                    raise AndroidUseError(str(native_entry_result.get("error") or first_page_error))
                if isinstance(vessel_entry_result, dict) and vessel_entry_result.get("attempted"):
                    raise AndroidUseError(str(vessel_entry_result.get("error") or first_page_error))
                raise
        elif page is None:
            raise first_page_error
    if page is None:
        raise AndroidUseError("Could not open Xiaoluxue knowledge guide page.")
    timings["select_page_sec"] = round(time.monotonic() - started_at, 3)
    entry_page_url = str(page.get("runtimeHref") or page.get("url") or "")
    entry_opened_target_course = bool(
        (
            isinstance(native_entry_result, dict)
            and native_entry_result.get("ok")
            or isinstance(vessel_entry_result, dict)
            and vessel_entry_result.get("ok")
        )
        and resolved_knowledge_id is not None
        and xiaoluxue_url_kind(entry_page_url) == "course"
        and f"knowledgeId={resolved_knowledge_id}" in entry_page_url
    )
    stop_loading_result: Any = None
    if entry_opened_target_course:
        stop_loading_result = {
            "ok": True,
            "skipped": "entry-opened-target-course",
            "before": {"readyState": "complete"},
            "after": {"readyState": "complete"},
        }
    else:
        try:
            stop_loading_result = cdp_eval_value(page, xiaoluxue_stop_non_course_loading_expression(), timeout=0.6)
        except Exception as exc:
            stop_loading_result = {"ok": False, "error": str(exc)}
    stop_after = stop_loading_result.get("after") if isinstance(stop_loading_result, dict) and isinstance(stop_loading_result.get("after"), dict) else {}
    stop_before = stop_loading_result.get("before") if isinstance(stop_loading_result, dict) and isinstance(stop_loading_result.get("before"), dict) else {}
    runtime_route_safe = bool(stop_loading_result.get("ok")) and stop_after.get("readyState") != "loading"
    page_was_loading = stop_before.get("readyState") == "loading" or not stop_loading_result.get("ok")

    use_shortcut_url = bool(args.get("use_shortcut_url", True))
    refresh_session = bool(args.get("refresh_session", False))
    if shortcut and shortcut.get("targetUrl") and use_shortcut_url and not refresh_session and knowledge_id is None and guide_widget_index is None:
        resolved = {
            "ok": True,
            "source": "shortcut-url",
            "subjectId": subject_id,
            "requestedIndex": knowledge_index,
            "knowledgeId": int(shortcut["knowledgeId"]),
            "knowledgeIndex": shortcut.get("knowledgeIndex"),
            "knowledgeName": shortcut.get("knowledgeName"),
            "lessonId": shortcut.get("lessonId"),
            "studySessionId": shortcut.get("studySessionId"),
            "guideIndex": int(shortcut.get("guideIndex", 0)),
            "guideName": shortcut.get("guideName", ""),
            "guideCdnUrl": shortcut.get("guideCdnUrl", ""),
            "avatarUrl": shortcut.get("avatarUrl", ""),
            "targetUrl": shortcut["targetUrl"],
        }
    else:
        remaining = max(deadline - time.monotonic(), 1)
        resolved = cdp_eval_value(
            page,
            xiaoluxue_resolve_knowledge_guide_expression(
                subject_id=subject_id,
                knowledge_index=knowledge_index,
                knowledge_id=resolved_knowledge_id,
                guide_widget_index=resolved_guide_index,
            ),
            timeout=min(remaining, 15),
        )
    if not isinstance(resolved, dict) or not resolved.get("targetUrl"):
        raise AndroidUseError("Could not resolve Xiaoluxue knowledge guide target.")
    current_h5_href = str(page.get("runtimeHref") or page.get("url") or "")
    if not entry_opened_target_course:
        try:
            runtime_href = cdp_eval_value(page, "location.href", timeout=0.3)
            if isinstance(runtime_href, str):
                current_h5_href = runtime_href
        except Exception:
            pass
    if bool(args.get("respect_current_h5_host", True)):
        rebased_target_url = xiaoluxue_rebase_h5_url(str(resolved["targetUrl"]), current_h5_href)
        if rebased_target_url != str(resolved["targetUrl"]):
            resolved = {**resolved, "originalTargetUrl": resolved["targetUrl"], "targetUrl": rebased_target_url}
    timings["resolve_sec"] = round(time.monotonic() - started_at, 3)
    guide_index = int(resolved.get("guideIndex") or 0)
    knowledge_id_int = int(resolved["knowledgeId"])
    expected_guide_name = str(resolved.get("guideName") or "").strip()
    current_page_url = current_h5_href or str(page.get("url") or "")
    current_page_kind = xiaoluxue_url_kind(current_page_url)
    effective_prefer_client_route = bool(prefer_client_route and current_page_kind == "course")
    native_course_already_open = bool(
        (
            isinstance(native_entry_result, dict)
            and native_entry_result.get("ok")
            or isinstance(vessel_entry_result, dict)
            and vessel_entry_result.get("ok")
        )
        and xiaoluxue_url_kind(current_page_url) == "course"
        and f"knowledgeId={knowledge_id_int}" in current_page_url
        and not bool(args.get("force_route_after_native", False))
    )

    rate_bootstrap_script = xiaoluxue_fast_rate_expression(rate, guide_index, activate_player=True)
    rate_bootstrap_result: Any = {"ok": True, "skipped": "current-document-client-route"}
    rate_prepare_result: Any = None
    if entry_opened_target_course:
        rate_prepare_result = {"ok": True, "skipped": "entry-opened-target-course", "rate": rate}
    elif runtime_route_safe:
        try:
            rate_prepare_result = cdp_eval_value(page, rate_bootstrap_script, timeout=1)
        except Exception as exc:
            rate_prepare_result = {"ok": False, "error": str(exc)}
    else:
        rate_prepare_result = {"ok": True, "skipped": "runtime-loading-page", "rate": rate}
    prefetch_result: Any = None
    if entry_opened_target_course:
        prefetch_result = {"ok": True, "skipped": "entry-opened-target-course"}
    else:
        try:
            prefetch_result = cdp_eval_value(
                page,
                xiaoluxue_prefetch_guide_expression(
                    str(resolved.get("guideCdnUrl") or "") or None,
                    str(resolved.get("avatarUrl") or "") or None,
                ),
                timeout=0.8,
            )
        except Exception as exc:
            prefetch_result = {"ok": False, "error": str(exc)}

    optimize_neighbor_guides = bool(args.get("optimize_neighbor_guides", False))
    fetch_optimizer_result: Any = {"ok": True, "skipped": "disabled"}
    if optimize_neighbor_guides:
        try:
            fetch_optimizer_result = cdp_eval_value(
                page,
                xiaoluxue_course_fetch_optimizer_expression(str(resolved.get("guideCdnUrl") or "") or None),
                timeout=0.4,
            )
        except Exception as exc:
            fetch_optimizer_result = {"ok": False, "error": str(exc)}

    turbo_preview = bool(args.get("turbo_preview", True))
    turbo_bootstrap_script = xiaoluxue_turbo_guide_player_expression(
        guide_index=guide_index,
        guide_name=str(resolved.get("guideName") or "") or None,
        avatar_url=str(resolved.get("avatarUrl") or "") or None,
        rate=rate,
        enabled=turbo_preview,
    )
    turbo_result: Any = None
    try:
        turbo_timeout = 0.25 if entry_opened_target_course else 0.6
        turbo_result = cdp_eval_value(page, turbo_bootstrap_script, timeout=turbo_timeout)
    except Exception as exc:
        turbo_result = {"ok": False, "error": str(exc)}

    needs_new_document_bootstrap = not native_course_already_open and (
        page_was_loading or not (runtime_route_safe and effective_prefer_client_route)
    )
    if needs_new_document_bootstrap:
        try:
            cdp_call(str(page["webSocketDebuggerUrl"]), "Page.enable", {}, timeout=1)
            for script_id in range(1, 41):
                try:
                    cdp_call(
                        str(page["webSocketDebuggerUrl"]),
                        "Page.removeScriptToEvaluateOnNewDocument",
                        {"identifier": str(script_id)},
                        timeout=0.2,
                    )
                except Exception:
                    pass
            bootstrap_scripts = [
                ("rate", rate_bootstrap_script),
                ("turbo", turbo_bootstrap_script),
                (
                    "prefetch",
                    xiaoluxue_prefetch_guide_expression(
                        str(resolved.get("guideCdnUrl") or "") or None,
                        str(resolved.get("avatarUrl") or "") or None,
                    ),
                ),
            ]
            installed_scripts: list[str] = []
            for script_name, script_source in bootstrap_scripts:
                cdp_call(
                    str(page["webSocketDebuggerUrl"]),
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": script_source},
                    timeout=1,
                )
                installed_scripts.append(script_name)
            rate_bootstrap_result = {"ok": True, "scripts": installed_scripts}
        except Exception as exc:
            rate_bootstrap_result = {"ok": False, "error": str(exc)}

    if native_course_already_open:
        route_result = {
            "ok": True,
            "mode": "direct-course-existing-page",
            "reason": "native-or-vessel-entry-opened-target-course",
            "href": current_page_url,
        }
    elif page_was_loading or not effective_prefer_client_route:
        try:
            navigate_timeout = 1 if page_was_loading else 0.45
            route_result = cdp_call(
                str(page["webSocketDebuggerUrl"]),
                "Page.navigate",
                {"url": str(resolved["targetUrl"])},
                timeout=navigate_timeout,
            )
            route_result = {
                "ok": True,
                "mode": "page-navigate",
                "cdp": route_result,
                "reason": "loading-page" if page_was_loading else "cross-page-or-non-course-source",
            }
        except Exception as exc:
            route_result = {"ok": False, "mode": "page-navigate", "error": str(exc)}
    else:
        try:
            route_result = cdp_eval_value(
                page,
                xiaoluxue_route_course_expression(str(resolved["targetUrl"]), prefer_client_route=effective_prefer_client_route),
                timeout=2,
            )
        except Exception as exc:
            try:
                route_result = cdp_call(
                    str(page["webSocketDebuggerUrl"]),
                    "Page.navigate",
                    {"url": str(resolved["targetUrl"])},
                    timeout=1,
                )
                route_result = {"ok": True, "mode": "page-navigate", "cdp": route_result, "error_before_fallback": str(exc)}
            except Exception as navigate_exc:
                route_result = {"ok": False, "error": str(exc), "navigate_error": str(navigate_exc)}
    timings["route_sec"] = round(time.monotonic() - started_at, 3)

    ready: dict[str, Any] | None = None
    last_error: str | None = None
    fallback_reload_result: Any = None
    turbo_refreshed_after_route = False
    turbo_video_available_before_ready = bool(isinstance(turbo_result, dict) and turbo_result.get("video"))
    if entry_opened_target_course:
        if not turbo_video_available_before_ready and turbo_preview:
            try:
                turbo_result = cdp_eval_value(page, turbo_bootstrap_script, timeout=0.25)
            except Exception as exc:
                turbo_result = {"ok": False, "error": str(exc)}
            turbo_video_available_before_ready = bool(isinstance(turbo_result, dict) and turbo_result.get("video"))
        ready = {
            "ok": True,
            "href": current_page_url,
            "readyState": "complete",
            "params": {"knowledgeId": str(knowledge_id_int)},
            "guideIndex": guide_index,
            "targetWidget": None,
            "visibleWidget": None,
            "guidePlayerVisible": False,
            "videos": (
                [{"playbackRate": rate, "paused": False, "currentTime": None, "source": "turbo-video"}]
                if turbo_video_available_before_ready
                else []
            ),
            "skipped": "entry-opened-target-course",
        }
    while ready is None and time.monotonic() < deadline:
        try:
            value = cdp_eval_value(page, xiaoluxue_course_ready_expression(knowledge_id_int, guide_index), timeout=1.5)
            if isinstance(value, dict):
                ready = value
                target_widget = value.get("targetWidget") if isinstance(value.get("targetWidget"), dict) else {}
                visible_widget = value.get("visibleWidget") if isinstance(value.get("visibleWidget"), dict) else {}
                target_loaded = bool(target_widget.get("loaded"))
                current_loaded = bool(visible_widget and str(visible_widget.get("text") or "").strip())
                visible_text = str(visible_widget.get("text") or "")
                target_name = str(target_widget.get("dataName") or "")
                content_matches = not expected_guide_name or expected_guide_name in target_name or expected_guide_name in visible_text
                if (
                    turbo_preview
                    and not turbo_refreshed_after_route
                    and value.get("ok")
                    and value.get("readyState") != "loading"
                ):
                    try:
                        turbo_result = cdp_eval_value(page, turbo_bootstrap_script, timeout=0.4)
                    except Exception as exc:
                        turbo_result = {"ok": False, "error": str(exc)}
                    turbo_refreshed_after_route = True
                turbo_video_available = bool(isinstance(turbo_result, dict) and turbo_result.get("video"))
                turbo_ready_enough = bool(
                    turbo_video_available
                    and value.get("ok")
                    and value.get("readyState") != "loading"
                )
                native_ready_enough = bool(
                    native_course_already_open
                    and value.get("ok")
                    and value.get("readyState") != "loading"
                    and (
                        target_widget.get("dataName")
                        or value.get("guidePlayerVisible")
                    )
                )
                if (
                    fallback_reload_result is None
                    and not native_course_already_open
                    and effective_prefer_client_route
                    and value.get("ok")
                    and not content_matches
                    and (time.monotonic() - started_at) > float(timings["route_sec"]) + 1.2
                ):
                    fallback_reload_result = cdp_eval_value(
                        page,
                        xiaoluxue_route_course_expression(str(resolved["targetUrl"]), prefer_client_route=False),
                        timeout=1,
                    )
                if (
                    turbo_ready_enough
                    or native_ready_enough
                    or (
                        value.get("ok")
                        and value.get("readyState") != "loading"
                        and content_matches
                        and (target_widget.get("dataName") or target_loaded or current_loaded or value.get("guidePlayerVisible"))
                    )
                ):
                    break
        except Exception as exc:  # The same DevTools target can briefly reject evals while navigating.
            last_error = str(exc)
        time.sleep(0.08)
    timings["ready_sec"] = round(time.monotonic() - started_at, 3)

    goto_result: Any = None
    ready_target_widget = ready.get("targetWidget") if isinstance(ready, dict) and isinstance(ready.get("targetWidget"), dict) else {}
    turbo_video_available = bool(isinstance(turbo_result, dict) and turbo_result.get("video"))
    if guide_index >= 0 and (ready_target_widget.get("dataName") or not turbo_video_available):
        try:
            goto_result = cdp_eval_value(page, xiaoluxue_goto_widget_expression(guide_index, "scroll"), timeout=2)
            time.sleep(0.08)
            if turbo_preview:
                try:
                    turbo_result = cdp_eval_value(
                        page,
                        xiaoluxue_turbo_guide_player_expression(
                            guide_index=guide_index,
                            guide_name=str(resolved.get("guideName") or "") or None,
                            avatar_url=str(resolved.get("avatarUrl") or "") or None,
                            rate=rate,
                            enabled=True,
                        ),
                        timeout=0.4,
                    )
                except Exception:
                    pass
        except Exception as exc:
            goto_result = {"ok": False, "error": str(exc)}
    elif guide_index >= 0:
        goto_result = {"ok": True, "skipped": "turbo-video-active-before-widget"}

    speed_activate_result: Any = None
    turbo_video_active = bool(isinstance(turbo_result, dict) and turbo_result.get("video"))
    activate_real_player = not turbo_video_active
    if turbo_video_active:
        ready_videos = ready.get("videos") if isinstance(ready, dict) and isinstance(ready.get("videos"), list) else []
        speed_activate_result = {
            "ok": True,
            "skipped": "turbo-video-active",
            "rate": rate,
            "activatePlayer": False,
            "videos": ready_videos
            or [{"playbackRate": rate, "paused": False, "currentTime": None, "source": "turbo-video"}],
        }
    else:
        try:
            speed_activate_result = cdp_eval_value(
                page,
                xiaoluxue_fast_rate_expression(rate, guide_index, activate_player=activate_real_player),
                timeout=min(max(deadline - time.monotonic(), 1), 0.6),
            )
        except Exception as exc:
            speed_activate_result = {"ok": False, "error": str(exc)}
    speed_verify_result: Any = speed_activate_result
    video_verify_sec = min(max(float(args.get("video_verify_sec", 0.9)), 0), 3)
    activate_videos = (
        speed_activate_result.get("videos")
        if isinstance(speed_activate_result, dict) and isinstance(speed_activate_result.get("videos"), list)
        else []
    )
    verify_until = time.monotonic() if activate_videos else min(deadline, time.monotonic() + video_verify_sec)
    while time.monotonic() < verify_until:
        try:
            speed_verify_result = cdp_eval_value(
                page,
                xiaoluxue_fast_rate_expression(rate, guide_index, activate_player=False),
                timeout=min(max(deadline - time.monotonic(), 1), 2),
            )
            if isinstance(speed_verify_result, dict) and speed_verify_result.get("ok"):
                videos = speed_verify_result.get("videos") if isinstance(speed_verify_result.get("videos"), list) else []
                if videos:
                    break
        except Exception as exc:
            speed_verify_result = {"ok": False, "error": str(exc)}
        time.sleep(0.1)
    timings["speed_sec"] = round(time.monotonic() - started_at, 3)
    speed_result: Any = {
        "ok": bool(
            isinstance(speed_activate_result, dict)
            and speed_activate_result.get("ok")
            or isinstance(speed_verify_result, dict)
            and speed_verify_result.get("ok")
        ),
        "rate": rate,
        "video_verify_sec": video_verify_sec,
        "activate": speed_activate_result,
        "verify": speed_verify_result,
    }

    final_snapshot: Any = {"ok": True, "skipped": "fast-path", "ready": ready}
    if bool(args.get("final_verify", False)):
        try:
            final_snapshot = cdp_eval_value(page, xiaoluxue_course_ready_expression(knowledge_id_int, guide_index), timeout=0.8)
        except Exception as exc:
            final_snapshot = {"ok": False, "error": str(exc)}
    elapsed = round(time.monotonic() - started_at, 3)
    result = {
        "ok": True,
        "action": "xiaoluxue_open_knowledge_guide",
        "webview": True,
        "elapsed_sec": elapsed,
        "timings": timings,
        "requested": {
            "subject_id": subject_id,
            "knowledge_index": knowledge_index,
            "normalized_index": normalized_index,
            "rate": rate,
            "current_page_kind": current_page_kind,
            "effective_prefer_client_route": effective_prefer_client_route,
        },
        "resolved": resolved,
        "stop_loading": stop_loading_result,
        "route": route_result,
        "rate_bootstrap": rate_bootstrap_result,
        "rate_prepare": rate_prepare_result,
        "vessel_entry": vessel_entry_result,
        "native_entry": native_entry_result,
        "prefetch": prefetch_result,
        "fetch_optimizer": fetch_optimizer_result,
        "turbo": turbo_result,
        "fallback_reload": fallback_reload_result,
        "ready": ready,
        "goto": goto_result,
        "speed": speed_result,
        "final": final_snapshot,
    }
    if last_error and not ready:
        result["last_ready_error"] = last_error
    if record:
        append_recording_step(
            serial,
            "xiaoluxue_open_knowledge_guide",
            {
                "subject_id": subject_id,
                "knowledge_index": knowledge_index,
                "knowledge_id": resolved_knowledge_id,
                "guide_widget_index": resolved_guide_index,
                "rate": rate,
                "prefer_client_route": prefer_client_route,
                "native_entry_if_needed": native_entry_if_needed,
                "turbo_preview": turbo_preview,
                "optimize_neighbor_guides": optimize_neighbor_guides,
                "video_verify_sec": video_verify_sec,
                "final_verify": bool(args.get("final_verify", False)),
                "preinject_vessel_bootstrap": bool(args.get("preinject_vessel_bootstrap", False)),
                "respect_current_h5_host": bool(args.get("respect_current_h5_host", True)),
                "timeout_sec": timeout,
            },
            result,
        )
    return result


def tool_xiaoluxue_open_knowledge_guide(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    result = run_xiaoluxue_open_knowledge_guide(serial, args, record=True)
    return [text_content({"ok": True, "serial": serial, **result})]


def normalize_xiaoluxue_map_index(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    match = re.search(r"(?<!\d)(\d+(?:\.\d+)+)(?!\d)", raw)
    if match:
        return match.group(1)
    match = re.search(r"(?:地图|节点|课节|第)\s*(\d+)(?:\s*(?:节|课|章|单元))?", raw)
    return match.group(1) if match else None


def normalize_xiaoluxue_map_action(value: Any) -> str:
    raw = str(value or "").strip()
    lowered = raw.casefold()
    canonical = {
        "select",
        "practise",
        "expand",
        "wrong",
        "notebook",
        "report",
        "tasks",
        "weak",
        "chapter_picker",
        "done",
        "back",
    }
    if lowered in canonical:
        return lowered
    if lowered in {"practice", "practise_item", "challenge"}:
        return "practise"
    if lowered in {"expand_item", "expansion", "exclusive", "exclusive_practice", "special", "special_practice", "train", "training"}:
        return "expand"
    if lowered in {"task"}:
        return "tasks"
    if lowered in {"chapter", "textbook", "picker"}:
        return "chapter_picker"
    if any(term in raw for term in ("错题", "错题本")):
        return "wrong"
    if any(term in raw for term in ("笔记本", "笔记")):
        return "notebook"
    if any(term in raw for term in ("看报告", "报告")):
        return "report"
    if any(term in raw for term in ("专属精练", "专属练习", "专属", "精练", "专练", "巩固练习", "巩固")):
        return "expand"
    if any(term in raw for term in ("题型突破", "题型", "突破", "章节挑战")):
        return "practise"
    if any(term in raw for term in ("学习任务", "任务")):
        return "tasks"
    if any(term in raw for term in ("薄弱知识", "薄弱")):
        return "weak"
    if any(term in raw for term in ("教材", "课本", "章节", "单元")):
        return "chapter_picker"
    if any(term in raw for term in ("完成", "收起", "关闭")):
        return "done"
    if any(term in raw for term in ("返回", "退出")) or lowered in {"back", "go back"}:
        return "back"
    return "select"


def normalize_xiaoluxue_subject_id(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        subject_id = int(value)
        return subject_id if subject_id > 0 else None
    if isinstance(value, dict):
        for key in ("subject_id", "subjectId", "subject"):
            subject_id = normalize_xiaoluxue_subject_id(value.get(key))
            if subject_id:
                return subject_id
        return None
    raw = str(value or "").strip()
    if not raw:
        return None
    match = re.search(r"(?:subject[_-]?id|subjectId)\s*[=:]\s*(\d+)", raw, flags=re.IGNORECASE)
    if match:
        subject_id = int(match.group(1))
        return subject_id if subject_id > 0 else None
    lowered = raw.casefold()
    latin_aliases = {
        "yuwen": 1,
        "chinese": 1,
        "math": 2,
        "maths": 2,
        "shuxue": 2,
        "english": 3,
        "yingyu": 3,
    }
    for alias, subject_id in latin_aliases.items():
        if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", lowered):
            return subject_id
    for alias, subject_id in sorted(XIAOLUXUE_SUBJECT_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if alias in raw:
            return subject_id
    return None


def xiaoluxue_study_subject_route_url(
    subject_id: int,
    *,
    textbook_id: Any = None,
    chapter_id: Any = None,
    knowledge_id: Any = None,
    go_next_knowledge: Any = None,
) -> str:
    params: list[tuple[str, str]] = [("subject_id", str(int(subject_id)))]
    for key, value in (
        ("textbook_id", textbook_id),
        ("chapter_id", chapter_id),
        ("knowledge_id", knowledge_id),
    ):
        if value is None or value == "":
            continue
        params.append((key, str(int(value))))
    if go_next_knowledge is not None:
        params.append(("go_next_knowledge", "true" if bool(go_next_knowledge) else "false"))
    return f"{XIAOLUXUE_STUDY_SUBJECT_ROUTE}?{urllib.parse.urlencode(params)}"


def xiaoluxue_map_fast_action_from_instruction(instruction: str) -> dict[str, Any] | None:
    if not xiaoluxue_instruction_looks_like_map(instruction):
        return None
    action_name = normalize_xiaoluxue_map_action(instruction)
    index = normalize_xiaoluxue_map_index(instruction)
    subject_id = normalize_xiaoluxue_subject_id(instruction)
    proposed: dict[str, Any] = {
        "action": "xiaoluxue_map_fast_path",
        "instruction": instruction,
        "action_name": action_name,
        "source": "xiaoluxue-native-map",
    }
    if index:
        proposed["index"] = index
    if subject_id:
        proposed["subject_id"] = subject_id
        proposed["route_if_subject"] = True
    if xiaoluxue_instruction_wants_direct_practice(instruction):
        proposed["enter_direct_practice"] = True
    return proposed


def normalize_xiaoluxue_lesson_action(value: Any) -> str:
    raw = str(value or "").strip()
    lowered = raw.casefold().replace("_", "-").replace(" ", "")
    if lowered in {"direct-practice", "direct", "practice", "practise"}:
        return "direct_practice"
    if any(keyword in raw for keyword in ("直接练", "开始练", "马上练", "做题")):
        return "direct_practice"
    if lowered in {"continue-answer", "next-answer", "next-question", "continue"}:
        return "continue_answer"
    if any(keyword in raw for keyword in ("继续答题", "继续", "下一题", "下一道", "下一问", "答题页")):
        return "continue_answer"
    if lowered in {"finish-result", "finish", "done"}:
        return "finish_result"
    if any(keyword in raw for keyword in ("完成", "结束", "返回地图")):
        return "finish_result"
    return lowered.replace("-", "_") or "direct_practice"


def xiaoluxue_instruction_wants_direct_practice(instruction: str) -> bool:
    text = str(instruction or "").strip()
    return bool(text) and any(keyword in text for keyword in ("直接练", "开始练", "马上练"))


def xiaoluxue_instruction_wants_continue_answer(instruction: str) -> bool:
    text = str(instruction or "").strip()
    return bool(text) and any(keyword in text for keyword in ("继续答题", "继续", "下一题", "下一道", "下一问"))


def xiaoluxue_instruction_wants_finish_result(instruction: str) -> bool:
    text = str(instruction or "").strip()
    return bool(text) and any(keyword in text for keyword in ("完成", "结束", "返回地图"))


def xiaoluxue_lesson_fast_action_from_instruction(instruction: str) -> dict[str, Any] | None:
    action_name: str | None = None
    if xiaoluxue_instruction_wants_direct_practice(instruction):
        action_name = "direct_practice"
    elif xiaoluxue_instruction_wants_continue_answer(instruction):
        action_name = "continue_answer"
    elif xiaoluxue_instruction_wants_finish_result(instruction):
        action_name = "finish_result"
    if not action_name:
        return None
    return {
        "action": "xiaoluxue_lesson_fast_path",
        "instruction": instruction,
        "action_name": action_name,
        "source": "xiaoluxue-native-lesson",
    }


def xiaoluxue_instruction_looks_like_map(instruction: str) -> bool:
    text = str(instruction or "").strip()
    if not text:
        return False
    if any(keyword in text for keyword in ("知识讲解", "讲解", "倍速", "2x", "2X")):
        return any(keyword in text for keyword in XIAOLUXUE_MAP_FAST_KEYWORDS)
    has_map_keyword = any(keyword in text for keyword in XIAOLUXUE_MAP_FAST_KEYWORDS)
    has_index = normalize_xiaoluxue_map_index(text) is not None
    has_subject = normalize_xiaoluxue_subject_id(text) is not None
    return has_map_keyword and (
        has_index
        or has_subject
        or any(keyword in text for keyword in ("地图", "任务", "薄弱", "完成", "返回"))
    )


def xiaoluxue_node_resource_endswith(node: dict[str, Any] | None, suffix: str) -> bool:
    if not node:
        return False
    resource_id = str(node.get("resource_id") or "")
    normalized = suffix if suffix.startswith(":id/") else f":id/{suffix.lstrip('/')}"
    return resource_id.endswith(normalized)


def find_xiaoluxue_node_by_resource_suffix(
    nodes: list[dict[str, Any]],
    suffix: str | tuple[str, ...],
) -> dict[str, Any] | None:
    suffixes = (suffix,) if isinstance(suffix, str) else suffix
    for node in nodes:
        if any(xiaoluxue_node_resource_endswith(node, item) for item in suffixes):
            return node
    return None


def find_xiaoluxue_map_index_node(nodes: list[dict[str, Any]], index: str) -> dict[str, Any] | None:
    normalized = str(index).strip()
    for node in nodes:
        if str(node.get("text") or "").strip() == normalized and xiaoluxue_node_resource_endswith(node, "index"):
            return node
    return find_ui_node(nodes, normalized, exact=True)


def xiaoluxue_map_action_node(nodes: list[dict[str, Any]], action_name: str) -> dict[str, Any] | None:
    action = normalize_xiaoluxue_map_action(action_name)
    if action == "practise":
        return find_xiaoluxue_node_by_resource_suffix(nodes, "practiseItem")
    if action == "expand":
        return find_xiaoluxue_node_by_resource_suffix(nodes, "expandItem")
    if action == "wrong":
        return find_xiaoluxue_node_by_resource_suffix(nodes, "wrong_textbook")
    if action == "notebook":
        return find_xiaoluxue_node_by_resource_suffix(nodes, "textbook")
    if action == "tasks":
        return find_xiaoluxue_node_by_resource_suffix(nodes, ("ll_task_tip", "task_tip", "task_view"))
    if action == "weak":
        return find_xiaoluxue_node_by_resource_suffix(nodes, ("ll_weak_knowledge_tip", "weak_knowledge_tip"))
    if action == "chapter_picker":
        return find_xiaoluxue_node_by_resource_suffix(nodes, ("textbook_view", "chapter_name"))
    if action == "done":
        return find_ui_node(nodes, "完成", exact=True)
    if action == "back":
        return find_xiaoluxue_node_by_resource_suffix(nodes, ("img_back", "iv_back", "back"))
    if action == "report":
        return find_ui_node(nodes, "看报告", exact=True) or find_ui_node(nodes, "报告", exact=False)
    return None


def xiaoluxue_selected_map_index(nodes: list[dict[str, Any]]) -> str | None:
    anchors = [
        node
        for node in nodes
        if xiaoluxue_node_resource_endswith(node, "wrong_textbook")
        or xiaoluxue_node_resource_endswith(node, "textbook")
        or xiaoluxue_node_resource_endswith(node, "practiseItem")
        or xiaoluxue_node_resource_endswith(node, "expandItem")
    ]
    for anchor in anchors:
        center = anchor.get("center")
        if not isinstance(center, dict):
            continue
        x = int(center.get("x", 0))
        y = int(center.get("y", 0))
        for node in nodes:
            text = str(node.get("text") or "").strip()
            if not text or not xiaoluxue_node_resource_endswith(node, "index"):
                continue
            click_target = node.get("click_target") if isinstance(node.get("click_target"), dict) else {}
            bounds = click_target.get("bounds") if isinstance(click_target, dict) else None
            if isinstance(bounds, dict) and point_in_bounds(bounds, x, y):
                return text
    selected = [node for node in nodes if xiaoluxue_node_resource_endswith(node, "index") and node.get("selected")]
    if selected:
        return str(selected[0].get("text") or "").strip() or None
    return None


def xiaoluxue_map_visible_indexes(nodes: list[dict[str, Any]]) -> list[str]:
    indexes: list[str] = []
    for node in nodes:
        text = str(node.get("text") or "").strip()
        if not text or not xiaoluxue_node_resource_endswith(node, "index"):
            continue
        if re.fullmatch(r"\d+(?:\.\d+)*", text) and text not in indexes:
            indexes.append(text)
    return indexes


def xiaoluxue_map_snapshot_from_observation(observation: dict[str, Any]) -> dict[str, Any]:
    nodes = observation.get("ui", {}).get("nodes", [])
    if not isinstance(nodes, list):
        nodes = []
    subject_node = find_xiaoluxue_node_by_resource_suffix(nodes, "txt_subject_name")
    chapter_node = find_xiaoluxue_node_by_resource_suffix(nodes, "chapter_name")
    actions: dict[str, bool] = {}
    for action_name in ("practise", "expand", "wrong", "notebook", "tasks", "weak", "chapter_picker", "done", "back", "report"):
        actions[action_name] = xiaoluxue_map_action_node(nodes, action_name) is not None
    focus = str(observation.get("state", {}).get("focused_window") or "")
    return {
        "focused_window": focus,
        "is_map": XIAOLUXUE_STUDY_SUBJECT_ACTIVITY in focus
        or bool(find_xiaoluxue_node_by_resource_suffix(nodes, "study_subject_map") or chapter_node),
        "subject": str(subject_node.get("text") or "").strip() if subject_node else None,
        "chapter": str(chapter_node.get("text") or "").strip() if chapter_node else None,
        "selected_index": xiaoluxue_selected_map_index(nodes),
        "visible_indexes": xiaoluxue_map_visible_indexes(nodes),
        "visible_actions": actions,
    }


def xiaoluxue_map_point(point: dict[str, Any] | None) -> dict[str, int] | None:
    if not isinstance(point, dict):
        return None
    try:
        return {"x": int(point["x"]), "y": int(point["y"])}
    except (KeyError, TypeError, ValueError):
        return None


def xiaoluxue_map_cache_from_nodes(serial: str, nodes: list[dict[str, Any]], snapshot: dict[str, Any]) -> dict[str, Any]:
    index_tap_points: dict[str, dict[str, int]] = {}
    index_centers: dict[str, dict[str, int]] = {}
    for node in nodes:
        text = str(node.get("text") or "").strip()
        if not text or not xiaoluxue_node_resource_endswith(node, "index"):
            continue
        tap_point = xiaoluxue_map_point(node_click_point(node))
        center = xiaoluxue_map_point(node.get("center") if isinstance(node.get("center"), dict) else None)
        if tap_point:
            index_tap_points[text] = tap_point
        if center:
            index_centers[text] = center
    action_points: dict[str, dict[str, int]] = {}
    for action_name in ("practise", "expand", "wrong", "notebook", "tasks", "weak", "chapter_picker", "done", "back", "report"):
        action_node = xiaoluxue_map_action_node(nodes, action_name)
        point = xiaoluxue_map_point(node_click_point(action_node) if action_node else None)
        if point:
            action_points[action_name] = point
    return {
        "serial": serial,
        "updated_at": time.time(),
        "snapshot": snapshot,
        "selected_index": snapshot.get("selected_index"),
        "index_tap_points": index_tap_points,
        "index_centers": index_centers,
        "action_points": action_points,
    }


def xiaoluxue_load_native_map_cache() -> dict[str, Any]:
    if XIAOLUXUE_NATIVE_MAP_CACHE:
        return XIAOLUXUE_NATIVE_MAP_CACHE
    try:
        payload = json.loads(XIAOLUXUE_NATIVE_MAP_CACHE_PATH.read_text())
        if isinstance(payload, dict):
            XIAOLUXUE_NATIVE_MAP_CACHE.update(payload)
    except (OSError, json.JSONDecodeError):
        pass
    return XIAOLUXUE_NATIVE_MAP_CACHE


def xiaoluxue_save_native_map_cache() -> None:
    try:
        write_json(XIAOLUXUE_NATIVE_MAP_CACHE_PATH, XIAOLUXUE_NATIVE_MAP_CACHE)
    except OSError:
        pass


def xiaoluxue_remember_native_map_cache(serial: str, nodes: list[dict[str, Any]], snapshot: dict[str, Any]) -> dict[str, Any]:
    cache = xiaoluxue_map_cache_from_nodes(serial, nodes, snapshot)
    xiaoluxue_load_native_map_cache()[serial] = cache
    xiaoluxue_save_native_map_cache()
    return cache


def xiaoluxue_cached_native_map(serial: str, max_age_sec: float) -> dict[str, Any] | None:
    cache = xiaoluxue_load_native_map_cache().get(serial)
    if not isinstance(cache, dict):
        return None
    try:
        age = time.time() - float(cache.get("updated_at", 0))
    except (TypeError, ValueError):
        return None
    if age < 0 or age > max_age_sec:
        return None
    return cache


def xiaoluxue_update_cached_selected_index(serial: str, index: str | None) -> None:
    if not index:
        return
    cache = xiaoluxue_load_native_map_cache().get(serial)
    if not isinstance(cache, dict):
        return
    cache["selected_index"] = index
    snapshot = cache.get("snapshot")
    if isinstance(snapshot, dict):
        snapshot["selected_index"] = index
    cache["updated_at"] = time.time()
    xiaoluxue_save_native_map_cache()


def xiaoluxue_map_predicted_point_from_center(center: dict[str, Any] | None, action_name: str) -> dict[str, int] | None:
    if not isinstance(center, dict):
        return None
    x = int(center.get("x", 0))
    y = int(center.get("y", 0))
    action = normalize_xiaoluxue_map_action(action_name)
    offsets = {
        "practise": (0, -266),
        "expand": (198, -74),
        "wrong": (-74, 138),
        "notebook": (74, 138),
    }
    if action not in offsets:
        return None
    dx, dy = offsets[action]
    return {"x": x + dx, "y": y + dy}


def xiaoluxue_map_predicted_action_point(index_node: dict[str, Any] | None, action_name: str) -> dict[str, int] | None:
    if not index_node:
        return None
    center = index_node.get("center")
    return xiaoluxue_map_predicted_point_from_center(center if isinstance(center, dict) else None, action_name)


def xiaoluxue_map_tap_point(
    serial: str,
    point: dict[str, int],
    label: str,
    steps: list[dict[str, Any]],
    started_at: float,
    matched_node: dict[str, Any] | None = None,
) -> None:
    adb(["shell", "input", "tap", str(point["x"]), str(point["y"])], serial=serial, timeout=4)
    steps.append(
        {
            "action": "tap",
            "label": label,
            "x": point["x"],
            "y": point["y"],
            "matched_node": compact_node(matched_node),
            "at_sec": round(time.monotonic() - started_at, 3),
        }
    )


def xiaoluxue_map_tap_node(
    serial: str,
    node: dict[str, Any] | None,
    label: str,
    steps: list[dict[str, Any]],
    started_at: float,
) -> dict[str, int]:
    point = node_click_point(node) if node else None
    if not point:
        raise AndroidUseError(f"Could not tap Xiaoluxue map node: {label}")
    xiaoluxue_map_tap_point(serial, point, label, steps, started_at, node)
    return point


def xiaoluxue_should_enter_study_module(
    action_name: str,
    args: dict[str, Any],
    *,
    open_report_when_done: bool = False,
) -> bool:
    if open_report_when_done:
        return False
    action = normalize_xiaoluxue_map_action(action_name)
    if action not in XIAOLUXUE_MAP_MODULE_ENTRY_ACTIONS:
        return False
    return bool(args.get("enter_module", bool(args.get("enter_direct_practice", False))))


def xiaoluxue_clamp_native_point(point: tuple[int, int]) -> tuple[int, int]:
    x, y = point
    return (
        min(max(int(x), 0), XIAOLUXUE_NATIVE_BASE_WIDTH - 1),
        min(max(int(y), 0), XIAOLUXUE_NATIVE_BASE_HEIGHT - 1),
    )


def xiaoluxue_module_entry_point_from_screen(point: dict[str, int], window_info: dict[str, Any]) -> dict[str, int]:
    width = int(window_info.get("width") or XIAOLUXUE_NATIVE_BASE_WIDTH)
    height = int(window_info.get("height") or XIAOLUXUE_NATIVE_BASE_HEIGHT)
    offset_y = round(XIAOLUXUE_NATIVE_MODULE_CARD_ENTER_OFFSET_Y * height / XIAOLUXUE_NATIVE_BASE_HEIGHT)
    return {
        "x": min(max(int(point["x"]), 0), max(width - 1, 0)),
        "y": min(max(int(point["y"]) + offset_y, 0), max(height - 1, 0)),
    }


def xiaoluxue_wait_for_lesson_activity(serial: str, timeout_sec: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(float(timeout_sec), 0.0)
    info = xiaoluxue_native_window_info(serial)
    focus = str(info.get("focus") or "")
    while XIAOLUXUE_LESSON_ACTIVITY not in focus and time.monotonic() < deadline:
        time.sleep(0.05)
        info = xiaoluxue_native_window_info(serial)
        focus = str(info.get("focus") or "")
    return info


def xiaoluxue_wait_for_lesson_content_ready(
    serial: str,
    timeout_sec: float,
    poll_sec: float,
    steps: list[dict[str, Any]],
    started_at: float,
) -> dict[str, Any]:
    wait_started_at = time.monotonic()
    timeout_sec = min(max(float(timeout_sec), 0.0), 8.0)
    poll_sec = min(max(float(poll_sec), 0.03), 0.5)
    deadline = wait_started_at + timeout_sec
    attempts = 0
    last_stats: dict[str, Any] = {"ready": False}
    while True:
        attempts += 1
        try:
            last_stats = raw_screenshot_content_stats(screenshot_raw(serial))
        except AndroidUseError as exc:
            last_stats = {"ready": False, "error": str(exc)}
        if bool(last_stats.get("ready")) or time.monotonic() >= deadline:
            break
        time.sleep(min(poll_sec, max(deadline - time.monotonic(), 0.0)))
    result = {
        **last_stats,
        "attempts": attempts,
        "wait_sec": round(time.monotonic() - wait_started_at, 3),
        "timeout_sec": timeout_sec,
    }
    steps.append(
        {
            "action": "lesson:content-ready",
            "ready": bool(result.get("ready")),
            "attempts": attempts,
            "wait_sec": result["wait_sec"],
            "at_sec": round(time.monotonic() - started_at, 3),
        }
    )
    return result


def android_get_global_settings(serial: str, keys: tuple[str, ...]) -> dict[str, str]:
    if not keys:
        return {}
    output = shell(serial, "; ".join(f"settings get global {key}" for key in keys), timeout=4)
    values = output.splitlines()
    return {key: (values[index].strip() if index < len(values) else "") for index, key in enumerate(keys)}


def android_put_global_settings(serial: str, values: dict[str, str]) -> None:
    commands: list[str] = []
    for key, value in values.items():
        if key not in ANDROID_ANIMATION_SCALE_SETTINGS:
            continue
        normalized = str(value).strip()
        if not normalized or normalized == "null":
            commands.append(f"settings delete global {key} >/dev/null 2>&1 || true")
        else:
            commands.append(f"settings put global {key} {normalized}")
    if commands:
        shell(serial, "; ".join(commands), timeout=4)


def xiaoluxue_prepare_answer_speed_settings(
    serial: str,
    args: dict[str, Any],
    steps: list[dict[str, Any]],
    started_at: float,
) -> dict[str, Any] | None:
    if not bool(args.get("disable_system_animations", True)):
        return None
    scale = str(args.get("system_animation_scale", "0")).strip() or "0"
    if not re.fullmatch(r"\d+(?:\.\d+)?", scale):
        scale = "0"
    try:
        previous = android_get_global_settings(serial, ANDROID_ANIMATION_SCALE_SETTINGS)
        updates = {
            key: scale
            for key in ANDROID_ANIMATION_SCALE_SETTINGS
            if previous.get(key) != scale
        }
        if updates:
            android_put_global_settings(serial, updates)
        state = {"previous": previous, "scale": scale, "changed": bool(updates)}
        steps.append(
            {
                "label": "lesson:disable-animations",
                "scale": scale,
                "changed": bool(updates),
                "at_sec": round(time.monotonic() - started_at, 3),
            }
        )
        return state
    except AndroidUseError as exc:
        steps.append(
            {
                "label": "lesson:disable-animations",
                "error": str(exc),
                "at_sec": round(time.monotonic() - started_at, 3),
            }
        )
        return None


def xiaoluxue_restore_answer_speed_settings(
    serial: str,
    args: dict[str, Any],
    animation_state: dict[str, Any] | None,
    steps: list[dict[str, Any]],
    started_at: float,
) -> None:
    if not animation_state or not bool(args.get("restore_system_animations", True)):
        return
    previous = animation_state.get("previous")
    if not isinstance(previous, dict) or not animation_state.get("changed"):
        return
    try:
        android_put_global_settings(serial, {str(key): str(value) for key, value in previous.items()})
        steps.append(
            {
                "label": "lesson:restore-animations",
                "at_sec": round(time.monotonic() - started_at, 3),
            }
        )
    except AndroidUseError as exc:
        steps.append(
            {
                "label": "lesson:restore-animations",
                "error": str(exc),
                "at_sec": round(time.monotonic() - started_at, 3),
            }
        )


def xiaoluxue_wait_for_lesson_answer_ready(
    serial: str,
    timeout_sec: float,
    poll_sec: float,
    steps: list[dict[str, Any]],
    started_at: float,
) -> dict[str, Any]:
    wait_started_at = time.monotonic()
    timeout_sec = min(max(float(timeout_sec), 0.0), 8.0)
    poll_sec = min(max(float(poll_sec), 0.03), 0.5)
    deadline = wait_started_at + timeout_sec
    attempts = 0
    last_stats: dict[str, Any] = {"ready": False}
    while True:
        attempts += 1
        try:
            last_stats = raw_screenshot_lesson_answer_stats(screenshot_raw(serial))
        except AndroidUseError as exc:
            last_stats = {"ready": False, "error": str(exc)}
        if bool(last_stats.get("ready")) or time.monotonic() >= deadline:
            break
        time.sleep(min(poll_sec, max(deadline - time.monotonic(), 0.0)))
    result = {
        **last_stats,
        "attempts": attempts,
        "wait_sec": round(time.monotonic() - wait_started_at, 3),
        "timeout_sec": timeout_sec,
    }
    steps.append(
        {
            "label": "lesson:answer-ready",
            "ready": bool(result.get("ready")),
            "attempts": attempts,
            "wait_sec": result["wait_sec"],
            "at_sec": round(time.monotonic() - started_at, 3),
        }
    )
    return result


def xiaoluxue_tap_lesson_direct_practice(
    serial: str,
    args: dict[str, Any],
    steps: list[dict[str, Any]],
    started_at: float,
    *,
    default_wait_sec: float = 0.0,
) -> dict[str, Any]:
    wait_sec = min(max(float(args.get("direct_practice_wait_sec", default_wait_sec)), 0.0), 2.0)
    if wait_sec:
        time.sleep(wait_sec)
    assumed_focus = bool(args.get("assume_lesson_activity", False))
    focus_timeout_sec = 0.0 if assumed_focus else min(max(float(args.get("lesson_focus_timeout_sec", 0.7)), 0.0), 2.0)
    info = (
        {
            "focus": XIAOLUXUE_LESSON_ACTIVITY,
            "width": XIAOLUXUE_NATIVE_BASE_WIDTH,
            "height": XIAOLUXUE_NATIVE_BASE_HEIGHT,
        }
        if assumed_focus
        else xiaoluxue_wait_for_lesson_activity(serial, focus_timeout_sec)
    )
    focus = str(info.get("focus") or "")
    if XIAOLUXUE_LESSON_ACTIVITY not in focus and not bool(args.get("skip_lesson_focus_check", False)):
        raise AndroidUseError(f"Current Xiaoluxue screen is not LessonActivity: {focus or 'unknown focus'}")
    ready_timeout_sec = min(max(float(args.get("lesson_ready_timeout_sec", 0.0)), 0.0), 8.0)
    tap_until_answer_ready = bool(args.get("tap_direct_practice_until_answer_ready", False))
    ready_result: dict[str, Any] | None = None
    if ready_timeout_sec and not tap_until_answer_ready:
        ready_result = xiaoluxue_wait_for_lesson_content_ready(
            serial,
            ready_timeout_sec,
            float(args.get("lesson_ready_poll_sec", 0.08)),
            steps,
            started_at,
        )
        if not bool(ready_result.get("ready")) and bool(args.get("require_lesson_ready", False)):
            raise AndroidUseError(
                "LessonActivity content is still loading; direct practice button is not ready."
            )
    animation_state = xiaoluxue_prepare_answer_speed_settings(serial, args, steps, started_at)
    answer_ready: dict[str, Any] | None = None
    direct_taps = 0
    try:
        after_wait_sec = min(max(float(args.get("after_direct_practice_wait_sec", 0.08)), 0.0), 2.0)
        answer_ready_timeout_sec = min(max(float(args.get("answer_ready_timeout_sec", 5.0)), 0.0), 8.0)
        if tap_until_answer_ready and answer_ready_timeout_sec:
            deadline = time.monotonic() + answer_ready_timeout_sec
            attempts = 0
            last_stats: dict[str, Any] = {"ready": False}
            tap_interval_sec = min(max(float(args.get("direct_practice_tap_interval_sec", 0.12)), 0.03), 0.5)
            poll_after_taps = min(max(int(args.get("answer_ready_poll_after_taps", 3)), 1), 12)
            probe_points = (
                (XIAOLUXUE_NATIVE_DIRECT_PRACTICE_ENTER, "lesson:direct-practice"),
                (XIAOLUXUE_NATIVE_CURRENT_CARD_DIRECT_PRACTICE_ENTER, "lesson:card-direct-practice"),
                (XIAOLUXUE_NATIVE_RIGHT_CARD_DIRECT_PRACTICE_ENTER, "lesson:right-card-direct-practice"),
                (XIAOLUXUE_NATIVE_TRANSITION_START, "lesson:transition-start"),
            )
            while True:
                point, label = probe_points[direct_taps % len(probe_points)]
                xiaoluxue_native_tap(serial, point, info, label, steps, started_at)
                direct_taps += 1
                if after_wait_sec:
                    time.sleep(after_wait_sec)
                if direct_taps % poll_after_taps == 0 or time.monotonic() >= deadline:
                    attempts += 1
                    try:
                        last_stats = raw_screenshot_lesson_answer_stats(screenshot_raw(serial))
                    except AndroidUseError as exc:
                        last_stats = {"ready": False, "error": str(exc)}
                    if bool(last_stats.get("ready")) or time.monotonic() >= deadline:
                        break
                time.sleep(min(tap_interval_sec, max(deadline - time.monotonic(), 0.0)))
            answer_ready = {
                **last_stats,
                "attempts": attempts,
                "direct_taps": direct_taps,
                "wait_sec": round(time.monotonic() - (deadline - answer_ready_timeout_sec), 3),
                "timeout_sec": answer_ready_timeout_sec,
            }
            steps.append(
                {
                    "label": "lesson:answer-ready",
                    "ready": bool(answer_ready.get("ready")),
                    "attempts": attempts,
                    "direct_taps": direct_taps,
                    "wait_sec": answer_ready["wait_sec"],
                    "at_sec": round(time.monotonic() - started_at, 3),
                }
            )
        else:
            xiaoluxue_native_tap(
                serial,
                XIAOLUXUE_NATIVE_DIRECT_PRACTICE_ENTER,
                info,
                "lesson:direct-practice",
                steps,
                started_at,
            )
            direct_taps += 1
            if after_wait_sec:
                time.sleep(after_wait_sec)
        if answer_ready is None and answer_ready_timeout_sec:
            answer_ready = xiaoluxue_wait_for_lesson_answer_ready(
                serial,
                answer_ready_timeout_sec,
                float(args.get("answer_ready_poll_sec", 0.12)),
                steps,
                started_at,
            )
    finally:
        xiaoluxue_restore_answer_speed_settings(serial, args, animation_state, steps, started_at)
    return {
        "action": "direct_practice",
        "wait_sec": wait_sec,
        "focus_timeout_sec": focus_timeout_sec,
        "assumed_focus": assumed_focus,
        "ready": ready_result,
        "after_wait_sec": after_wait_sec,
        "answer_ready": answer_ready,
        "direct_taps": direct_taps,
        "enter_point": {
            "base_x": XIAOLUXUE_NATIVE_DIRECT_PRACTICE_ENTER[0],
            "base_y": XIAOLUXUE_NATIVE_DIRECT_PRACTICE_ENTER[1],
        },
    }


def xiaoluxue_tap_lesson_continue_answer(
    serial: str,
    args: dict[str, Any],
    steps: list[dict[str, Any]],
    started_at: float,
) -> dict[str, Any]:
    assumed_focus = bool(args.get("assume_lesson_activity", True))
    focus_timeout_sec = 0.0 if assumed_focus else min(max(float(args.get("lesson_focus_timeout_sec", 0.7)), 0.0), 2.0)
    info = (
        {
            "focus": XIAOLUXUE_LESSON_ACTIVITY,
            "width": XIAOLUXUE_NATIVE_BASE_WIDTH,
            "height": XIAOLUXUE_NATIVE_BASE_HEIGHT,
        }
        if assumed_focus
        else xiaoluxue_wait_for_lesson_activity(serial, focus_timeout_sec)
    )
    focus = str(info.get("focus") or "")
    if XIAOLUXUE_LESSON_ACTIVITY not in focus and not bool(args.get("skip_lesson_focus_check", False)):
        raise AndroidUseError(f"Current Xiaoluxue screen is not LessonActivity: {focus or 'unknown focus'}")
    animation_state = xiaoluxue_prepare_answer_speed_settings(serial, args, steps, started_at)
    answer_ready: dict[str, Any] | None = None
    skip_taps = 0
    card_direct_taps = 0
    try:
        xiaoluxue_native_tap(
            serial,
            XIAOLUXUE_NATIVE_ANSWER_CONTINUE,
            info,
            "lesson:continue-answer",
            steps,
            started_at,
        )
        continue_tapped_at = time.monotonic()
        initial_wait_sec = min(max(float(args.get("after_continue_wait_sec", 0.18)), 0.0), 2.0)
        if initial_wait_sec:
            time.sleep(initial_wait_sec)
        timeout_sec = min(max(float(args.get("answer_ready_timeout_sec", 5.0)), 0.0), 8.0)
        poll_sec = min(max(float(args.get("answer_ready_poll_sec", 0.12)), 0.03), 0.5)
        max_skip_taps = min(max(int(args.get("transition_skip_taps", 6)), 0), 20)
        min_ready_after_continue_sec = min(max(float(args.get("min_answer_ready_after_continue_sec", 2.2)), 0.0), 4.0)
        card_direct_enabled = bool(args.get("tap_card_direct_practice_if_needed", True))
        max_card_direct_taps = min(max(int(args.get("card_direct_practice_taps", 4)), 0), 20)
        deadline = time.monotonic() + timeout_sec
        attempts = 0
        last_stats: dict[str, Any] = {"ready": False}
        last_card_stats: dict[str, Any] = {"ready": False}
        while True:
            attempts += 1
            if skip_taps < max_skip_taps:
                xiaoluxue_native_tap(
                    serial,
                    XIAOLUXUE_NATIVE_TRANSITION_START,
                    info,
                    "lesson:transition-start",
                    steps,
                    started_at,
                )
                skip_taps += 1
                time.sleep(min(max(float(args.get("transition_skip_interval_sec", 0.10)), 0.03), 0.5))
            try:
                raw = screenshot_raw(serial)
                last_stats = raw_screenshot_lesson_answer_stats(raw)
                last_card_stats = raw_screenshot_lesson_card_list_stats(raw)
            except AndroidUseError as exc:
                last_stats = {"ready": False, "error": str(exc)}
                last_card_stats = {"ready": False, "error": str(exc)}
            if (
                card_direct_enabled
                and bool(last_card_stats.get("ready"))
                and card_direct_taps < max_card_direct_taps
            ):
                card_points = (
                    (XIAOLUXUE_NATIVE_CURRENT_CARD_DIRECT_PRACTICE_ENTER, "lesson:card-direct-practice"),
                    (XIAOLUXUE_NATIVE_RIGHT_CARD_DIRECT_PRACTICE_ENTER, "lesson:right-card-direct-practice"),
                    (XIAOLUXUE_NATIVE_DIRECT_PRACTICE_ENTER, "lesson:left-card-direct-practice"),
                )
                card_point, card_label = card_points[card_direct_taps % len(card_points)]
                xiaoluxue_native_tap(
                    serial,
                    card_point,
                    info,
                    card_label,
                    steps,
                    started_at,
                )
                card_direct_taps += 1
                time.sleep(min(max(float(args.get("card_direct_practice_interval_sec", 0.12)), 0.03), 0.5))
            ready_enough = bool(last_stats.get("ready")) and (
                time.monotonic() - continue_tapped_at >= min_ready_after_continue_sec
            )
            if ready_enough or time.monotonic() >= deadline:
                break
            time.sleep(min(poll_sec, max(deadline - time.monotonic(), 0.0)))
        answer_ready = {
            **last_stats,
            "attempts": attempts,
            "skip_taps": skip_taps,
            "card_direct_taps": card_direct_taps,
            "card_list": last_card_stats,
            "wait_sec": round(time.monotonic() - (deadline - timeout_sec), 3),
            "timeout_sec": timeout_sec,
        }
        steps.append(
            {
                "label": "lesson:answer-ready",
                "ready": bool(answer_ready.get("ready")),
                "attempts": attempts,
                "skip_taps": skip_taps,
                "card_direct_taps": card_direct_taps,
                "wait_sec": answer_ready["wait_sec"],
                "at_sec": round(time.monotonic() - started_at, 3),
            }
        )
    finally:
        xiaoluxue_restore_answer_speed_settings(serial, args, animation_state, steps, started_at)
    return {
        "action": "continue_answer",
        "focus_timeout_sec": focus_timeout_sec,
        "assumed_focus": assumed_focus,
        "answer_ready": answer_ready,
        "card_direct_taps": card_direct_taps,
        "continue_point": {
            "base_x": XIAOLUXUE_NATIVE_ANSWER_CONTINUE[0],
            "base_y": XIAOLUXUE_NATIVE_ANSWER_CONTINUE[1],
        },
        "transition_start_point": {
            "base_x": XIAOLUXUE_NATIVE_TRANSITION_START[0],
            "base_y": XIAOLUXUE_NATIVE_TRANSITION_START[1],
        },
    }


def xiaoluxue_tap_lesson_finish_result(
    serial: str,
    args: dict[str, Any],
    steps: list[dict[str, Any]],
    started_at: float,
) -> dict[str, Any]:
    assumed_focus = bool(args.get("assume_lesson_activity", True))
    focus_timeout_sec = 0.0 if assumed_focus else min(max(float(args.get("lesson_focus_timeout_sec", 0.7)), 0.0), 2.0)
    info = (
        {
            "focus": XIAOLUXUE_LESSON_ACTIVITY,
            "width": XIAOLUXUE_NATIVE_BASE_WIDTH,
            "height": XIAOLUXUE_NATIVE_BASE_HEIGHT,
        }
        if assumed_focus
        else xiaoluxue_wait_for_lesson_activity(serial, focus_timeout_sec)
    )
    focus = str(info.get("focus") or "")
    if XIAOLUXUE_LESSON_ACTIVITY not in focus and not bool(args.get("skip_lesson_focus_check", False)):
        raise AndroidUseError(f"Current Xiaoluxue screen is not LessonActivity: {focus or 'unknown focus'}")
    xiaoluxue_native_tap(
        serial,
        XIAOLUXUE_NATIVE_RESULT_FINISH,
        info,
        "lesson:finish-result",
        steps,
        started_at,
    )
    wait_sec = min(max(float(args.get("after_finish_wait_sec", 0.35)), 0.0), 2.0)
    if wait_sec:
        time.sleep(wait_sec)
    return {
        "action": "finish_result",
        "focus_timeout_sec": focus_timeout_sec,
        "assumed_focus": assumed_focus,
        "finish_point": {
            "base_x": XIAOLUXUE_NATIVE_RESULT_FINISH[0],
            "base_y": XIAOLUXUE_NATIVE_RESULT_FINISH[1],
        },
    }


def xiaoluxue_tap_study_module_entry(
    serial: str,
    *,
    action_name: str,
    args: dict[str, Any],
    steps: list[dict[str, Any]],
    started_at: float,
    open_report_when_done: bool = False,
    base_action_point: tuple[int, int] | None = None,
    screen_action_point: dict[str, int] | None = None,
    window_info: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    action = normalize_xiaoluxue_map_action(action_name)
    if not xiaoluxue_should_enter_study_module(action, args, open_report_when_done=open_report_when_done):
        return None
    wait_sec = min(max(float(args.get("module_card_wait_sec", 0.16)), 0.05), 1.0)
    time.sleep(wait_sec)
    info = window_info or xiaoluxue_native_window_info(serial)
    module_entry: dict[str, Any] = {"action": action, "card_wait_sec": wait_sec}
    if base_action_point:
        entry_base = xiaoluxue_clamp_native_point(
            (base_action_point[0], base_action_point[1] + XIAOLUXUE_NATIVE_MODULE_CARD_ENTER_OFFSET_Y)
        )
        xiaoluxue_native_tap(serial, entry_base, info, f"{action}:module-enter", steps, started_at)
        module_entry["enter_point"] = {"base_x": entry_base[0], "base_y": entry_base[1]}
    elif screen_action_point:
        entry_point = xiaoluxue_module_entry_point_from_screen(screen_action_point, info)
        xiaoluxue_map_tap_point(serial, entry_point, f"{action}:module-enter", steps, started_at, None)
        module_entry["enter_point"] = entry_point
    else:
        return None

    if action == "expand" and bool(args.get("confirm_expand_enter", True)):
        confirm_wait_sec = min(max(float(args.get("confirm_wait_sec", 0.08)), 0.05), 1.5)
        confirm_started_at = time.monotonic()
        if bool(args.get("confirm_expand_focus_check", False)):
            focus_info = xiaoluxue_native_window_info(serial)
            focus = str(focus_info.get("focus") or "")
            deadline = confirm_started_at + confirm_wait_sec
            while XIAOLUXUE_STUDY_SUBJECT_ACTIVITY in focus and time.monotonic() < deadline:
                time.sleep(min(0.06, max(deadline - time.monotonic(), 0)))
                focus_info = xiaoluxue_native_window_info(serial)
                focus = str(focus_info.get("focus") or "")
            should_tap_confirm = XIAOLUXUE_STUDY_SUBJECT_ACTIVITY in focus
        else:
            if confirm_wait_sec:
                time.sleep(confirm_wait_sec)
            focus_info = info
            should_tap_confirm = True
        if should_tap_confirm:
            xiaoluxue_native_tap(
                serial,
                XIAOLUXUE_NATIVE_EXPAND_CONFIRM_ENTER,
                focus_info,
                "expand:confirm-enter",
                steps,
                started_at,
            )
            module_entry["confirm_tapped"] = True
        else:
            module_entry["confirm_tapped"] = False
        module_entry["confirm_wait_sec"] = round(time.monotonic() - confirm_started_at, 3)
    if action == "practise" and bool(args.get("enter_direct_practice", False)):
        direct_args = dict(args)
        direct_args.setdefault("tap_direct_practice_until_answer_ready", True)
        direct_args.setdefault("answer_ready_poll_after_taps", 2)
        direct_args.setdefault("lesson_focus_timeout_sec", 0.55)
        direct_args.setdefault("lesson_ready_timeout_sec", 5.5)
        direct_args.setdefault("lesson_ready_poll_sec", 0.15)
        direct_args.setdefault("require_lesson_ready", True)
        module_entry["direct_practice"] = xiaoluxue_tap_lesson_direct_practice(
            serial,
            direct_args,
            steps,
            started_at,
            default_wait_sec=0.12,
        )
    elif action in XIAOLUXUE_MAP_MODULE_ENTRY_ACTIONS and bool(args.get("verify_module_activity", True)):
        focus_timeout_sec = min(max(float(args.get("module_focus_timeout_sec", 0.9)), 0.0), 2.0)
        focus_info = xiaoluxue_wait_for_lesson_activity(serial, focus_timeout_sec)
        focus = str(focus_info.get("focus") or "")
        module_entry["focus_timeout_sec"] = focus_timeout_sec
        module_entry["focus_after_enter"] = focus
        if XIAOLUXUE_LESSON_ACTIVITY not in focus:
            raise AndroidUseError(f"Xiaoluxue module did not open LessonActivity: {focus or 'unknown focus'}")
    return module_entry


def xiaoluxue_start_scheme_route(
    serial: str,
    url: str,
    steps: list[dict[str, Any]],
    started_at: float,
) -> dict[str, Any]:
    xiaoluxue_dismiss_debug_overlay_if_needed(serial, steps, started_at)
    output = adb(
        [
            "shell",
            "am",
            "start",
            "-n",
            f"{XIAOLUXUE_STUDENT_PACKAGE}/{XIAOLUXUE_SCHEME_PROXY_ACTIVITY}",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            url,
        ],
        serial=serial,
        timeout=8,
    ).decode("utf-8", errors="replace")
    step = {
        "action": "route",
        "component": XIAOLUXUE_SCHEME_PROXY_ACTIVITY,
        "url": url,
        "at_sec": round(time.monotonic() - started_at, 3),
    }
    if output.strip():
        step["output"] = output.strip().splitlines()[-1]
    steps.append(step)
    return {"ok": True, "url": url, "output": output}


def xiaoluxue_leave_lesson_before_subject_route(
    serial: str,
    args: dict[str, Any],
    steps: list[dict[str, Any]],
    started_at: float,
) -> bool:
    if not bool(args.get("leave_lesson_before_route", True)):
        return False
    try:
        focus = str(xiaoluxue_native_window_info(serial).get("focus") or "")
    except AndroidUseError:
        focus = ""
    if XIAOLUXUE_LESSON_ACTIVITY not in focus:
        return False
    adb(["shell", "input", "keyevent", "BACK"], serial=serial, timeout=4)
    steps.append(
        {
            "action": "back",
            "reason": "leave-lesson-before-subject-route",
            "from_focus": focus,
            "at_sec": round(time.monotonic() - started_at, 3),
        }
    )
    wait_sec = min(max(float(args.get("lesson_back_wait_sec", 0.35)), 0.0), 1.2)
    if wait_sec:
        time.sleep(wait_sec)
    return True


def xiaoluxue_open_native_subject_map(
    serial: str,
    args: dict[str, Any],
    steps: list[dict[str, Any]],
    started_at: float,
) -> dict[str, Any]:
    subject_id = normalize_xiaoluxue_subject_id(
        args.get("subject_id")
        or args.get("subjectId")
        or args.get("subject")
        or args.get("instruction")
    )
    if not subject_id:
        raise AndroidUseError("Xiaoluxue native subject route needs `subject_id` or a subject name such as 语文/数学.")
    url = xiaoluxue_study_subject_route_url(
        subject_id,
        textbook_id=args.get("textbook_id") or args.get("textbookId"),
        chapter_id=args.get("chapter_id") or args.get("chapterId"),
        knowledge_id=args.get("knowledge_id") or args.get("knowledgeId"),
        go_next_knowledge=args.get("go_next_knowledge") if "go_next_knowledge" in args else args.get("goNextKnowledge"),
    )
    xiaoluxue_leave_lesson_before_subject_route(serial, args, steps, started_at)
    route = xiaoluxue_start_scheme_route(serial, url, steps, started_at)
    wait_sec = min(max(float(args.get("route_wait_sec", 0.45)), 0.0), 3.0)
    window_info: dict[str, Any] | None = None
    if wait_sec:
        wait_started = time.monotonic()
        deadline = wait_started + wait_sec
        poll_sec = min(max(float(args.get("route_focus_poll_sec", 0.08)), 0.03), 0.3)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_sec, remaining))
            try:
                window_info = xiaoluxue_native_window_info(serial)
            except AndroidUseError:
                window_info = None
            focus = str((window_info or {}).get("focus") or "")
            if XIAOLUXUE_STUDY_SUBJECT_ACTIVITY in focus:
                break
        actual_wait_sec = round(time.monotonic() - wait_started, 3)
        steps.append(
            {
                "action": "wait",
                "reason": "subject-route-render",
                "seconds": actual_wait_sec,
                "target_focus": XIAOLUXUE_STUDY_SUBJECT_ACTIVITY,
                "focus": str((window_info or {}).get("focus") or ""),
                "at_sec": round(time.monotonic() - started_at, 3),
            }
        )
    if window_info is None:
        window_info = xiaoluxue_native_window_info(serial)
    if XIAOLUXUE_STUDY_SUBJECT_ACTIVITY in str(window_info.get("focus") or ""):
        settle_sec = min(max(float(args.get("route_settle_sec", 0.0)), 0.0), 0.3)
        if settle_sec:
            time.sleep(settle_sec)
    if bool(args.get("close_progress_popup", True)):
        close_taps = min(max(int(args.get("close_progress_taps", 1)), 1), 3)
        close_wait_sec = min(max(float(args.get("close_progress_wait_sec", 0.05)), 0.0), 0.8)
        for attempt in range(close_taps):
            if attempt and close_wait_sec:
                time.sleep(close_wait_sec)
            xiaoluxue_native_tap(
                serial,
                XIAOLUXUE_NATIVE_PROGRESS_POPUP_CLOSE,
                window_info,
                f"subject-route:close-progress-popup:{attempt + 1}",
                steps,
                started_at,
            )
    return {"subject_id": subject_id, "route": route, "window_info": window_info}


def xiaoluxue_route_preset_for(subject_id: int | None, index: str | None) -> dict[str, tuple[int, int]] | None:
    if not subject_id or not index:
        return None
    subject_presets = XIAOLUXUE_NATIVE_MAP_ROUTE_PRESETS.get(int(subject_id))
    if not subject_presets:
        return None
    preset = subject_presets.get(str(index))
    return preset if isinstance(preset, dict) else None


def xiaoluxue_run_selected_module_shortcut(
    serial: str,
    *,
    subject_id: int | None,
    action_name: str,
    args: dict[str, Any],
    steps: list[dict[str, Any]],
    started_at: float,
    open_report_when_done: bool,
    window_info: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    action = normalize_xiaoluxue_map_action(action_name)
    if action not in XIAOLUXUE_MAP_MODULE_ENTRY_ACTIONS:
        return None
    if not bool(args.get("selected_module_shortcut", True)):
        return None
    window_info = window_info or xiaoluxue_native_window_info(serial)
    focus = str(window_info.get("focus") or "")
    if XIAOLUXUE_STUDY_SUBJECT_ACTIVITY not in focus:
        return None
    action_point = XIAOLUXUE_NATIVE_SELECTED_MODULE_POINTS.get(action)
    if not action_point:
        return None
    xiaoluxue_native_tap(serial, action_point, window_info, f"{action}:selected-shortcut", steps, started_at)
    module_entry = xiaoluxue_tap_study_module_entry(
        serial,
        action_name=action,
        args=args,
        steps=steps,
        started_at=started_at,
        open_report_when_done=open_report_when_done,
        base_action_point=action_point,
        window_info=window_info,
    )
    result = {
        "ok": True,
        "action": "xiaoluxue_map_fast_path",
        "map_action": action,
        "subject_id": subject_id,
        "selected_module_shortcut": True,
        "entered_module": bool(module_entry),
        "module_entry": module_entry,
        "elapsed_sec": round(time.monotonic() - started_at, 3),
        "steps": steps,
        "snapshot": {"subject_id": subject_id, "selected_index": "current", "selected_module_shortcut": True},
    }
    return result


def xiaoluxue_run_route_preset_map_fast_path(
    serial: str,
    *,
    subject_id: int,
    index: str | None,
    action_name: str,
    args: dict[str, Any],
    steps: list[dict[str, Any]],
    started_at: float,
    wait_after_select: float,
    open_report_when_done: bool,
    report_wait_sec: float,
    window_info: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    preset = xiaoluxue_route_preset_for(subject_id, index)
    if not preset:
        return None
    action = normalize_xiaoluxue_map_action(action_name)
    index_point = preset.get("index")
    if not index_point:
        return None
    window_info = window_info or xiaoluxue_native_window_info(serial)
    xiaoluxue_native_tap(serial, index_point, window_info, f"index:{index}:preset", steps, started_at)
    xiaoluxue_update_cached_selected_index(serial, str(index))
    module_entry: dict[str, Any] | None = None
    if action == "select":
        return {
            "ok": True,
            "action": "xiaoluxue_map_fast_path",
            "map_action": "select",
            "index": index,
            "subject_id": subject_id,
            "routed": True,
            "preset": True,
            "elapsed_sec": round(time.monotonic() - started_at, 3),
            "steps": steps,
            "snapshot": {"subject_id": subject_id, "selected_index": index, "preset": True},
        }
    time.sleep(wait_after_select)
    if action == "report":
        practise_point = preset.get("practise")
        report_point = preset.get("report")
        if not practise_point or not report_point:
            return None
        xiaoluxue_native_tap(serial, practise_point, window_info, "practise:preset", steps, started_at)
        time.sleep(report_wait_sec)
        xiaoluxue_native_tap(serial, report_point, window_info, "report:preset", steps, started_at)
    else:
        action_point = preset.get(action)
        if not action_point:
            return None
        xiaoluxue_native_tap(serial, action_point, window_info, f"{action}:preset", steps, started_at)
        module_entry = xiaoluxue_tap_study_module_entry(
            serial,
            action_name=action,
            args=args,
            steps=steps,
            started_at=started_at,
            open_report_when_done=open_report_when_done,
            base_action_point=action_point,
            window_info=window_info,
        )
        if open_report_when_done:
            report_point = preset.get("report")
            if report_point:
                time.sleep(report_wait_sec)
                xiaoluxue_native_tap(serial, report_point, window_info, "report:preset", steps, started_at)
    return {
        "ok": True,
        "action": "xiaoluxue_map_fast_path",
        "map_action": action,
        "index": index,
        "subject_id": subject_id,
        "routed": True,
        "preset": True,
        "entered_module": bool(module_entry),
        "module_entry": module_entry,
        "elapsed_sec": round(time.monotonic() - started_at, 3),
        "steps": steps,
        "snapshot": {"subject_id": subject_id, "selected_index": index, "preset": True},
    }


def xiaoluxue_run_cached_map_fast_path(
    serial: str,
    cache: dict[str, Any],
    *,
    index: str | None,
    action_name: str,
    args: dict[str, Any],
    steps: list[dict[str, Any]],
    started_at: float,
    wait_after_select: float,
    prefer_predicted: bool,
    open_report_when_done: bool,
) -> dict[str, Any] | None:
    action_points = cache.get("action_points") if isinstance(cache.get("action_points"), dict) else {}
    index_tap_points = cache.get("index_tap_points") if isinstance(cache.get("index_tap_points"), dict) else {}
    index_centers = cache.get("index_centers") if isinstance(cache.get("index_centers"), dict) else {}
    snapshot = cache.get("snapshot") if isinstance(cache.get("snapshot"), dict) else {}
    selected_index = str(cache.get("selected_index") or snapshot.get("selected_index") or "").strip() or None
    target_index = index or selected_index
    action = normalize_xiaoluxue_map_action(action_name)

    if action in {"tasks", "weak", "chapter_picker", "done", "back"}:
        point = xiaoluxue_map_point(action_points.get(action))
        if not point:
            return None
        xiaoluxue_map_tap_point(serial, point, action, steps, started_at, None)
        return {
            "ok": True,
            "action": "xiaoluxue_map_fast_path",
            "map_action": action,
            "cached": True,
            "elapsed_sec": round(time.monotonic() - started_at, 3),
            "steps": steps,
            "snapshot": snapshot,
        }

    if action == "report":
        point = xiaoluxue_map_point(action_points.get("report"))
        if point:
            xiaoluxue_map_tap_point(serial, point, "report", steps, started_at, None)
            return {
                "ok": True,
                "action": "xiaoluxue_map_fast_path",
                "map_action": "report",
                "cached": True,
                "elapsed_sec": round(time.monotonic() - started_at, 3),
                "steps": steps,
                "snapshot": snapshot,
            }
        action = "practise"

    if not target_index:
        return None
    target_tap = xiaoluxue_map_point(index_tap_points.get(target_index))
    if not target_tap:
        return None

    if action == "select":
        xiaoluxue_map_tap_point(serial, target_tap, f"index:{target_index}:cached", steps, started_at, None)
        xiaoluxue_update_cached_selected_index(serial, target_index)
        return {
            "ok": True,
            "action": "xiaoluxue_map_fast_path",
            "map_action": "select",
            "index": target_index,
            "cached": True,
            "elapsed_sec": round(time.monotonic() - started_at, 3),
            "steps": steps,
            "snapshot": snapshot,
        }

    if selected_index == target_index:
        point = xiaoluxue_map_point(action_points.get(action))
        if point:
            xiaoluxue_map_tap_point(serial, point, f"{action}:cached", steps, started_at, None)
            module_entry = xiaoluxue_tap_study_module_entry(
                serial,
                action_name=action,
                args=args,
                steps=steps,
                started_at=started_at,
                open_report_when_done=open_report_when_done,
                screen_action_point=point,
            )
            return {
                "ok": True,
                "action": "xiaoluxue_map_fast_path",
                "map_action": action,
                "index": target_index,
                "cached": True,
                "predicted": False,
                "entered_module": bool(module_entry),
                "module_entry": module_entry,
                "elapsed_sec": round(time.monotonic() - started_at, 3),
                "steps": steps,
                "snapshot": snapshot,
            }

    xiaoluxue_map_tap_point(serial, target_tap, f"index:{target_index}:cached", steps, started_at, None)
    time.sleep(wait_after_select)
    if not prefer_predicted:
        return None
    center = xiaoluxue_map_point(index_centers.get(target_index))
    predicted = xiaoluxue_map_predicted_point_from_center(center, action)
    if not predicted:
        return None
    xiaoluxue_map_tap_point(serial, predicted, f"{action}:predicted:cached", steps, started_at, None)
    module_entry = xiaoluxue_tap_study_module_entry(
        serial,
        action_name=action,
        args=args,
        steps=steps,
        started_at=started_at,
        open_report_when_done=open_report_when_done,
        screen_action_point=predicted,
    )
    xiaoluxue_update_cached_selected_index(serial, target_index)
    return {
        "ok": True,
        "action": "xiaoluxue_map_fast_path",
        "map_action": action,
        "index": target_index,
        "cached": True,
        "predicted": True,
        "entered_module": bool(module_entry),
        "module_entry": module_entry,
        "elapsed_sec": round(time.monotonic() - started_at, 3),
        "steps": steps,
        "snapshot": snapshot,
    }


def xiaoluxue_observe_native_map(serial: str, *, limit: int = 800, include_focus: bool = False) -> dict[str, Any]:
    xml_text = dump_ui_xml(serial)
    nodes = parse_ui_nodes(xml_text, limit=limit)
    observation: dict[str, Any] = {
        "state": {"focused_window": get_focused_window(serial) if include_focus else ""},
        "ui": {"nodes": nodes, "count": len(nodes)},
    }
    snapshot = xiaoluxue_map_snapshot_from_observation(observation)
    if not snapshot.get("is_map"):
        raise AndroidUseError("Current screen is not the Xiaoluxue native study map.")
    observation["xiaoluxue_map"] = snapshot
    return observation


def run_xiaoluxue_map_fast_path(serial: str, args: dict[str, Any], *, record: bool) -> dict[str, Any]:
    started_at = time.monotonic()
    steps: list[dict[str, Any]] = []
    action_name = normalize_xiaoluxue_map_action(args.get("action_name") or args.get("action") or args.get("instruction"))
    requested_report = action_name == "report"
    index = normalize_xiaoluxue_map_index(args.get("index") or args.get("instruction"))
    subject_id = normalize_xiaoluxue_subject_id(
        args.get("subject_id")
        or args.get("subjectId")
        or args.get("subject")
        or args.get("instruction")
    )
    prefer_predicted = bool(args.get("prefer_predicted", True))
    wait_after_select = min(max(float(args.get("after_select_wait_sec", 0.08)), 0.05), 1.0)
    report_wait_sec = min(max(float(args.get("report_wait_sec", 0.32)), 0.05), 1.0)
    open_report_when_done = bool(args.get("open_report_when_done", requested_report))
    route_subject = bool(args.get("route_subject", False) or args.get("open_subject", False) or args.get("route_if_subject", False))
    before: dict[str, Any] | None = None
    if route_subject and subject_id:
        route_args = args
        if (
            not index
            and action_name in XIAOLUXUE_MAP_MODULE_ENTRY_ACTIONS
            and "route_wait_sec" not in args
        ):
            route_args = {**args, "route_wait_sec": 0.65}
        route_info = xiaoluxue_open_native_subject_map(serial, route_args, steps, started_at)
        route_window_info = route_info.get("window_info") if isinstance(route_info, dict) else None
        preset_result = xiaoluxue_run_route_preset_map_fast_path(
            serial,
            subject_id=subject_id,
            index=index,
            action_name=action_name,
            args=args,
            steps=steps,
            started_at=started_at,
            wait_after_select=wait_after_select,
            open_report_when_done=open_report_when_done,
            report_wait_sec=report_wait_sec,
            window_info=route_window_info if isinstance(route_window_info, dict) else None,
        )
        if preset_result:
            if record:
                append_recording_step(
                    serial,
                    "xiaoluxue_map_fast_path",
                    args,
                    preset_result,
                    before={"state": {}, "ui": {"nodes": []}, "xiaoluxue_map": preset_result.get("snapshot", {})},
                )
            return preset_result
        if not index:
            shortcut_result = xiaoluxue_run_selected_module_shortcut(
                serial,
                subject_id=subject_id,
                action_name=action_name,
                args=args,
                steps=steps,
                started_at=started_at,
                open_report_when_done=open_report_when_done,
                window_info=route_window_info if isinstance(route_window_info, dict) else None,
            )
            if shortcut_result:
                shortcut_result["routed"] = True
                if record:
                    append_recording_step(
                        serial,
                        "xiaoluxue_map_fast_path",
                        args,
                        shortcut_result,
                        before={"state": {}, "ui": {"nodes": []}, "xiaoluxue_map": shortcut_result.get("snapshot", {})},
                    )
                return shortcut_result

    use_cache = bool(args.get("use_cache", True)) and not bool(args.get("force_observe", False))
    if route_subject and subject_id:
        use_cache = False
    if not index and action_name in XIAOLUXUE_MAP_MODULE_ENTRY_ACTIONS:
        use_cache = False
    if use_cache:
        cache = xiaoluxue_cached_native_map(serial, max_age_sec=float(args.get("cache_max_age_sec", 6 * 60 * 60)))
        if cache:
            cached_result = xiaoluxue_run_cached_map_fast_path(
                serial,
                cache,
                index=index,
                action_name=action_name,
                args=args,
                steps=steps,
                started_at=started_at,
                wait_after_select=wait_after_select,
                prefer_predicted=prefer_predicted,
                open_report_when_done=open_report_when_done,
            )
            if cached_result:
                if record:
                    append_recording_step(
                        serial,
                        "xiaoluxue_map_fast_path",
                        args,
                        cached_result,
                        before={"state": {}, "ui": {"nodes": []}, "xiaoluxue_map": cached_result.get("snapshot", {})},
                )
                return cached_result

    if not index:
        shortcut_result = xiaoluxue_run_selected_module_shortcut(
            serial,
            subject_id=subject_id,
            action_name=action_name,
            args=args,
            steps=steps,
            started_at=started_at,
            open_report_when_done=open_report_when_done,
        )
        if shortcut_result:
            if record:
                append_recording_step(
                    serial,
                    "xiaoluxue_map_fast_path",
                    args,
                    shortcut_result,
                    before={"state": {}, "ui": {"nodes": []}, "xiaoluxue_map": shortcut_result.get("snapshot", {})},
                )
            return shortcut_result

    before = xiaoluxue_observe_native_map(serial, limit=800)
    nodes = before["ui"]["nodes"]
    snapshot = before["xiaoluxue_map"]
    xiaoluxue_remember_native_map_cache(serial, nodes, snapshot)

    if action_name in {"tasks", "weak", "chapter_picker", "done", "back"}:
        node = xiaoluxue_map_action_node(nodes, action_name)
        xiaoluxue_map_tap_node(serial, node, action_name, steps, started_at)
        result = {
            "ok": True,
            "action": "xiaoluxue_map_fast_path",
            "map_action": action_name,
            "elapsed_sec": round(time.monotonic() - started_at, 3),
            "steps": steps,
            "snapshot": snapshot,
        }
        if record:
            append_recording_step(serial, "xiaoluxue_map_fast_path", args, result, before=before)
        return result

    if action_name == "report":
        report_node = xiaoluxue_map_action_node(nodes, "report")
        if report_node:
            xiaoluxue_map_tap_node(serial, report_node, "report", steps, started_at)
            result = {
                "ok": True,
                "action": "xiaoluxue_map_fast_path",
                "map_action": "report",
                "elapsed_sec": round(time.monotonic() - started_at, 3),
                "steps": steps,
                "snapshot": snapshot,
            }
            if record:
                append_recording_step(serial, "xiaoluxue_map_fast_path", args, result, before=before)
            return result
        action_name = "practise"

    selected_index = str(snapshot.get("selected_index") or "").strip() or None
    if not index:
        index = selected_index
    if not index:
        raise AndroidUseError("Xiaoluxue map fast path needs an index such as 1.5, or a selected map node.")

    index_node = find_xiaoluxue_map_index_node(nodes, index)
    if not index_node:
        visible = ", ".join(snapshot.get("visible_indexes") or [])
        raise AndroidUseError(f"Could not find Xiaoluxue map index {index!r}. Visible indexes: {visible}")

    if action_name == "select":
        xiaoluxue_map_tap_node(serial, index_node, f"index:{index}", steps, started_at)
        xiaoluxue_update_cached_selected_index(serial, index)
        result = {
            "ok": True,
            "action": "xiaoluxue_map_fast_path",
            "map_action": "select",
            "index": index,
            "elapsed_sec": round(time.monotonic() - started_at, 3),
            "steps": steps,
            "snapshot": snapshot,
        }
        if record:
            append_recording_step(serial, "xiaoluxue_map_fast_path", args, result, before=before)
        return result

    action_node = xiaoluxue_map_action_node(nodes, action_name)
    action_point = node_click_point(action_node) if action_node else None
    action_is_for_selected = selected_index == index and action_point is not None

    if not action_is_for_selected:
        xiaoluxue_map_tap_node(serial, index_node, f"index:{index}", steps, started_at)
        time.sleep(wait_after_select)
        if prefer_predicted:
            predicted = xiaoluxue_map_predicted_action_point(index_node, action_name)
            if predicted:
                xiaoluxue_map_tap_point(serial, predicted, f"{action_name}:predicted", steps, started_at, None)
                module_entry = xiaoluxue_tap_study_module_entry(
                    serial,
                    action_name=action_name,
                    args=args,
                    steps=steps,
                    started_at=started_at,
                    open_report_when_done=open_report_when_done,
                    screen_action_point=predicted,
                )
                xiaoluxue_update_cached_selected_index(serial, index)
                result = {
                    "ok": True,
                    "action": "xiaoluxue_map_fast_path",
                    "map_action": action_name,
                    "index": index,
                    "predicted": True,
                    "entered_module": bool(module_entry),
                    "module_entry": module_entry,
                    "elapsed_sec": round(time.monotonic() - started_at, 3),
                    "steps": steps,
                    "snapshot": snapshot,
                }
                if record:
                    append_recording_step(serial, "xiaoluxue_map_fast_path", args, result, before=before)
                return result
        after_select = xiaoluxue_observe_native_map(serial, limit=800)
        nodes = after_select["ui"]["nodes"]
        snapshot = after_select["xiaoluxue_map"]
        xiaoluxue_remember_native_map_cache(serial, nodes, snapshot)
        action_node = xiaoluxue_map_action_node(nodes, action_name)
        action_point = node_click_point(action_node) if action_node else None

    if not action_point:
        raise AndroidUseError(f"Could not find Xiaoluxue map action {action_name!r} for index {index!r}.")
    xiaoluxue_map_tap_point(serial, action_point, action_name, steps, started_at, action_node)
    module_entry = xiaoluxue_tap_study_module_entry(
        serial,
        action_name=action_name,
        args=args,
        steps=steps,
        started_at=started_at,
        open_report_when_done=open_report_when_done,
        screen_action_point=xiaoluxue_map_point(action_point),
    )

    if open_report_when_done:
        time.sleep(report_wait_sec)
        report_observation = observe_ui(serial, limit=500)
        report_node = find_ui_node(report_observation.get("ui", {}).get("nodes", []), "看报告", exact=True)
        if report_node:
            xiaoluxue_map_tap_node(serial, report_node, "看报告", steps, started_at)

    result = {
        "ok": True,
        "action": "xiaoluxue_map_fast_path",
        "map_action": action_name,
        "index": index,
        "predicted": False,
        "entered_module": bool(module_entry),
        "module_entry": module_entry,
        "elapsed_sec": round(time.monotonic() - started_at, 3),
        "steps": steps,
        "snapshot": snapshot,
    }
    if record:
        append_recording_step(serial, "xiaoluxue_map_fast_path", args, result, before=before)
    return result


def tool_xiaoluxue_map_snapshot(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    observation = xiaoluxue_observe_native_map(serial, limit=int(args.get("limit", 800)))
    return [
        text_content(
            {
                "ok": True,
                "serial": serial,
                "snapshot": observation["xiaoluxue_map"],
            }
        )
    ]


def run_xiaoluxue_open_native_subject(serial: str, args: dict[str, Any], *, record: bool) -> dict[str, Any]:
    started_at = time.monotonic()
    steps: list[dict[str, Any]] = []
    route_info = xiaoluxue_open_native_subject_map(serial, args, steps, started_at)
    result: dict[str, Any] = {
        "ok": True,
        "action": "xiaoluxue_open_native_subject",
        "subject_id": route_info["subject_id"],
        "route_url": route_info["route"]["url"],
        "elapsed_sec": round(time.monotonic() - started_at, 3),
        "steps": steps,
    }
    if bool(args.get("verify_focus", False)):
        info = xiaoluxue_wait_native_app_focus(serial, float(args.get("focus_timeout_sec", 1.5)))
        result["window"] = info
        result["elapsed_sec"] = round(time.monotonic() - started_at, 3)
    if record:
        append_recording_step(
            serial,
            "xiaoluxue_open_native_subject",
            args,
            result,
            before={"state": {}, "ui": {"nodes": []}},
        )
    return result


def tool_xiaoluxue_open_native_subject(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    result = run_xiaoluxue_open_native_subject(serial, args, record=True)
    return [text_content({"ok": True, "serial": serial, **result})]


def tool_xiaoluxue_map_fast_path(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    result = run_xiaoluxue_map_fast_path(serial, args, record=True)
    return [text_content({"ok": True, "serial": serial, **result})]


def run_xiaoluxue_lesson_fast_path(serial: str, args: dict[str, Any], *, record: bool) -> dict[str, Any]:
    started_at = time.monotonic()
    steps: list[dict[str, Any]] = []
    action_name = normalize_xiaoluxue_lesson_action(args.get("action_name") or args.get("action") or args.get("instruction"))
    if action_name not in {"direct_practice", "continue_answer", "finish_result"}:
        raise AndroidUseError(f"Unsupported Xiaoluxue lesson fast path action: {action_name!r}")
    before = (
        {"state": {}, "ui": {"nodes": []}}
        if active_recording(serial)
        else None
    )
    action_args = dict(args)
    action_args.setdefault("assume_lesson_activity", True)
    action_result_payload: dict[str, Any]
    if action_name == "direct_practice":
        action_result_payload = xiaoluxue_tap_lesson_direct_practice(
            serial,
            action_args,
            steps,
            started_at,
            default_wait_sec=0.0,
        )
    elif action_name == "continue_answer":
        action_result_payload = xiaoluxue_tap_lesson_continue_answer(
            serial,
            action_args,
            steps,
            started_at,
        )
    else:
        action_result_payload = xiaoluxue_tap_lesson_finish_result(
            serial,
            action_args,
            steps,
            started_at,
        )
    result = {
        "ok": True,
        "action": "xiaoluxue_lesson_fast_path",
        "lesson_action": action_name,
        "result": action_result_payload,
        "elapsed_sec": round(time.monotonic() - started_at, 3),
        "steps": steps,
    }
    if action_name == "direct_practice":
        result["direct_practice"] = action_result_payload
    elif action_name == "continue_answer":
        result["continue_answer"] = action_result_payload
    else:
        result["finish_result"] = action_result_payload
    if record:
        append_recording_step(serial, "xiaoluxue_lesson_fast_path", args, result, before=before)
    return result


def tool_xiaoluxue_lesson_fast_path(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    result = run_xiaoluxue_lesson_fast_path(serial, args, record=True)
    return [text_content({"ok": True, "serial": serial, **result})]


def normalize_xiaoluxue_env(env: Any) -> tuple[str, str, str]:
    raw = str(env or "test").strip()
    lowered = raw.casefold().replace("_", "-").replace(" ", "")
    aliases = {
        "": "test",
        "测试": "test",
        "测试环境": "test",
        "test环境": "test",
        "开发": "dev",
        "开发环境": "dev",
        "dev环境": "dev",
        "生产": "prod",
        "正式": "prod",
        "生产环境": "prod",
        "生产环境-com": "prod-com",
        "prodcom": "prod-com",
        "production-com": "prod-com",
    }
    key = aliases.get(lowered, lowered)
    if key.endswith("环境"):
        key = key[: -len("环境")]
    if key in XIAOLUXUE_ENV_CHOICES:
        choice = XIAOLUXUE_ENV_CHOICES[key]
        return key, choice["url"], choice["label"]
    match = XIAOLUXUE_CONFIG_URL_PATTERN.search(raw)
    if match:
        url = match.group(0)
        normalized_url = url.rstrip("/")
        for known_key, choice in XIAOLUXUE_ENV_CHOICES.items():
            if choice["url"].rstrip("/") == normalized_url:
                return known_key, choice["url"], choice["label"]
    valid = ", ".join(sorted({key for key in XIAOLUXUE_ENV_CHOICES if key != "production"}))
    raise AndroidUseError(f"Unsupported Xiaoluxue environment: {raw!r}. Expected one of: {valid}")


def xiaoluxue_config_observe(serial: str, *, limit: int = 500) -> dict[str, Any]:
    xml_text = dump_ui_xml(serial)
    nodes = parse_ui_nodes(xml_text, limit=limit)
    return {
        "state": {"focused_window": get_focused_window(serial)},
        "ui": {"nodes": nodes, "count": len(nodes)},
    }


def xiaoluxue_config_labels(nodes: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    for node in nodes:
        for label in node_labels(node)[:2]:
            value = label.strip()
            if value and value not in labels:
                labels.append(value)
    return labels


def xiaoluxue_config_current_url(nodes: list[dict[str, Any]]) -> str | None:
    marker_index: int | None = None
    for node in sorted(nodes, key=lambda item: int(item.get("index", 0))):
        labels = node_labels(node)[:2]
        for label in labels:
            match = XIAOLUXUE_CONFIG_URL_PATTERN.search(label)
            if "当前配置" in label and match:
                return match.group(0)
            if "当前配置" in label:
                marker_index = int(node.get("index", 0))
                continue
            if marker_index is not None and int(node.get("index", 0)) >= marker_index:
                if match:
                    return match.group(0)
    return None


def xiaoluxue_wait_config_screen(serial: str, timeout_sec: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(timeout_sec, 0.2)
    last_observation: dict[str, Any] | None = None
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            observation = xiaoluxue_config_observe(serial)
            last_observation = observation
            focus = str(observation.get("state", {}).get("focused_window") or "")
            nodes = observation.get("ui", {}).get("nodes", [])
            labels = xiaoluxue_config_labels(nodes) if isinstance(nodes, list) else []
            has_config_labels = any(label in labels for label in ("API 环境", "提交配置", "当前配置：", "学生端", "网络相关"))
            if has_config_labels or ("API 环境" in labels or "提交配置" in labels) or (XIAOLUXUE_CONFIG_PACKAGE in focus and labels):
                return observation
        except Exception as exc:  # noqa: PERF203 - retry until the config activity draws.
            last_error = exc
        time.sleep(0.12)
    if last_observation:
        return last_observation
    if last_error:
        raise AndroidUseError(f"Could not observe Xiaoluxue config screen: {last_error}") from last_error
    raise AndroidUseError("Could not observe Xiaoluxue config screen.")


def xiaoluxue_tap_ui_node(
    serial: str,
    node: dict[str, Any] | None,
    label: str,
    steps: list[dict[str, Any]],
    started_at: float,
) -> dict[str, int]:
    point = node_click_point(node) if node else None
    if not point:
        raise AndroidUseError(f"Could not tap Xiaoluxue config UI node: {label}")
    adb(["shell", "input", "tap", str(point["x"]), str(point["y"])], serial=serial, timeout=4)
    steps.append(
        {
            "action": "tap",
            "label": label,
            "x": point["x"],
            "y": point["y"],
            "matched_node": compact_node(node),
            "at_sec": round(time.monotonic() - started_at, 3),
        }
    )
    return point


def xiaoluxue_config_swipe(serial: str, direction: str, steps: list[dict[str, Any]], started_at: float) -> None:
    size = get_screen_size(serial)
    width = int(size.get("width") or XIAOLUXUE_NATIVE_BASE_WIDTH)
    height = int(size.get("height") or XIAOLUXUE_NATIVE_BASE_HEIGHT)
    x = round(width * 0.5)
    if direction == "down":
        start_y, end_y = round(height * 0.35), round(height * 0.75)
    else:
        start_y, end_y = round(height * 0.75), round(height * 0.35)
    adb(["shell", "input", "swipe", str(x), str(start_y), str(x), str(end_y), "180"], serial=serial, timeout=5)
    steps.append(
        {
            "action": "swipe",
            "direction": direction,
            "x": x,
            "start_y": start_y,
            "end_y": end_y,
            "at_sec": round(time.monotonic() - started_at, 3),
        }
    )


def xiaoluxue_find_config_node(
    serial: str,
    queries: list[str],
    *,
    timeout_sec: float,
    steps: list[dict[str, Any]],
    started_at: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    deadline = time.monotonic() + max(timeout_sec, 0.3)
    directions = ["down", "up", "up", "up", "up", "down"]
    direction_index = 0
    last_observation = xiaoluxue_wait_config_screen(serial, min(timeout_sec, 2))
    while True:
        nodes = last_observation.get("ui", {}).get("nodes", [])
        if isinstance(nodes, list):
            for query in queries:
                node = find_ui_node(nodes, query, exact=True) or find_ui_node(nodes, query, exact=False)
                if node:
                    return last_observation, node
        if time.monotonic() >= deadline or direction_index >= len(directions):
            break
        xiaoluxue_config_swipe(serial, directions[direction_index], steps, started_at)
        direction_index += 1
        time.sleep(0.15)
        last_observation = xiaoluxue_wait_config_screen(serial, min(max(deadline - time.monotonic(), 0.2), 1.2))
    raise AndroidUseError(f"Could not find Xiaoluxue config option: {queries}")


def xiaoluxue_wait_env_apply(serial: str, target_url: str, timeout_sec: float, steps: list[dict[str, Any]], started_at: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(timeout_sec, 0.3)
    last_payload: dict[str, Any] = {}
    confirmed_once = False
    while time.monotonic() < deadline:
        observation = xiaoluxue_config_observe(serial)
        nodes = observation.get("ui", {}).get("nodes", [])
        labels = xiaoluxue_config_labels(nodes) if isinstance(nodes, list) else []
        focus = str(observation.get("state", {}).get("focused_window") or "")
        current_url = xiaoluxue_config_current_url(nodes) if isinstance(nodes, list) else None
        last_payload = {"focus": focus, "current_url": current_url}
        if current_url == target_url:
            return {"ok": True, "reason": "current-config-updated", **last_payload}
        if XIAOLUXUE_STUDENT_PACKAGE in focus:
            return {"ok": True, "reason": "student-opened", **last_payload}
        if not confirmed_once and XIAOLUXUE_CONFIG_PACKAGE in focus and isinstance(nodes, list):
            confirm = find_ui_node(nodes, "确定", exact=True) or find_ui_node(nodes, "OK", exact=True)
            if confirm and any(label in labels for label in ("API 环境", "提交配置", "当前配置：")):
                xiaoluxue_tap_ui_node(serial, confirm, "confirm_config_dialog", steps, started_at)
                confirmed_once = True
        time.sleep(0.15)
    return {"ok": False, "reason": "timeout", **last_payload}


def xiaoluxue_launch_student_after_env(serial: str, *, force_stop: bool, timeout_sec: float) -> dict[str, Any]:
    if force_stop:
        adb(["shell", "am", "force-stop", XIAOLUXUE_STUDENT_PACKAGE], serial=serial, timeout=8)
    adb(["shell", "monkey", "-p", XIAOLUXUE_STUDENT_PACKAGE, "-c", "android.intent.category.LAUNCHER", "1"], serial=serial, timeout=10)
    return xiaoluxue_wait_native_app_focus(serial, min(max(timeout_sec, 0.5), 4))


def run_xiaoluxue_switch_env(serial: str, args: dict[str, Any], *, record: bool) -> dict[str, Any]:
    started_at = time.monotonic()
    timeout = min(float(args.get("timeout_sec", 10)), 60)
    env_key, target_url, option_label = normalize_xiaoluxue_env(args.get("env", "test"))
    force_submit = bool(args.get("force_submit", False))
    open_student = bool(args.get("open_student", True))
    steps: list[dict[str, Any]] = []

    adb(["shell", "am", "start", "-n", XIAOLUXUE_CONFIG_LAUNCHER_COMPONENT], serial=serial, timeout=10)
    steps.append({"action": "open_app", "package": XIAOLUXUE_CONFIG_PACKAGE, "at_sec": round(time.monotonic() - started_at, 3)})
    observation = xiaoluxue_wait_config_screen(serial, min(timeout, 5))
    nodes = observation.get("ui", {}).get("nodes", [])

    labels = xiaoluxue_config_labels(nodes) if isinstance(nodes, list) else []
    if isinstance(nodes, list) and "API 环境" not in labels:
        student_tab = find_ui_node(nodes, "学生端", exact=True)
        if student_tab:
            xiaoluxue_tap_ui_node(serial, student_tab, "学生端", steps, started_at)
            time.sleep(0.15)
            observation = xiaoluxue_wait_config_screen(serial, min(timeout, 3))
            nodes = observation.get("ui", {}).get("nodes", [])

    labels = xiaoluxue_config_labels(nodes) if isinstance(nodes, list) else []
    if isinstance(nodes, list) and "API 环境" not in labels:
        network_tab = find_ui_node(nodes, "网络相关", exact=True)
        if network_tab:
            xiaoluxue_tap_ui_node(serial, network_tab, "网络相关", steps, started_at)
            time.sleep(0.15)
            observation = xiaoluxue_wait_config_screen(serial, min(timeout, 3))
            nodes = observation.get("ui", {}).get("nodes", [])

    current_before = xiaoluxue_config_current_url(nodes) if isinstance(nodes, list) else None
    already_current = current_before == target_url
    submit_result: dict[str, Any]
    if already_current and not force_submit:
        submit_result = {"ok": True, "skipped": "already-current"}
    else:
        observation, target_node = xiaoluxue_find_config_node(
            serial,
            [option_label, target_url],
            timeout_sec=max(timeout - (time.monotonic() - started_at), 1),
            steps=steps,
            started_at=started_at,
        )
        xiaoluxue_tap_ui_node(serial, target_node, option_label, steps, started_at)
        time.sleep(0.15)
        _submit_observation, submit_node = xiaoluxue_find_config_node(
            serial,
            ["提交配置"],
            timeout_sec=max(timeout - (time.monotonic() - started_at), 1),
            steps=steps,
            started_at=started_at,
        )
        xiaoluxue_tap_ui_node(serial, submit_node, "提交配置", steps, started_at)
        submit_result = xiaoluxue_wait_env_apply(
            serial,
            target_url,
            timeout_sec=max(timeout - (time.monotonic() - started_at), 1),
            steps=steps,
            started_at=started_at,
        )

    if already_current and not force_submit:
        current_after = current_before
    else:
        current_after = submit_result.get("current_url")
    student_result: dict[str, Any] | None = None
    apply_ok = bool(already_current or submit_result.get("ok"))
    if open_student and apply_ok:
        force_stop_student = bool(args.get("force_stop_student", not already_current or force_submit))
        student_result = xiaoluxue_launch_student_after_env(
            serial,
            force_stop=force_stop_student,
            timeout_sec=max(timeout - (time.monotonic() - started_at), 1),
        )
        steps.append(
            {
                "action": "open_student",
                "force_stop": force_stop_student,
                "focus": student_result.get("focus"),
                "at_sec": round(time.monotonic() - started_at, 3),
            }
        )

    result = {
        "ok": apply_ok,
        "action": "xiaoluxue_switch_env",
        "elapsed_sec": round(time.monotonic() - started_at, 3),
        "env": env_key,
        "target_url": target_url,
        "option_label": option_label,
        "current_before": current_before,
        "current_after": current_after,
        "changed": bool(not already_current or force_submit),
        "submit": submit_result,
        "student": student_result,
        "steps": steps,
    }
    if record:
        recorded_args = {
            key: args[key]
            for key in ("env", "open_student", "force_submit", "force_stop_student", "timeout_sec")
            if key in args
        }
        append_recording_step(serial, "xiaoluxue_switch_env", recorded_args, result)
    return result


def tool_xiaoluxue_switch_env(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    result = run_xiaoluxue_switch_env(serial, args, record=True)
    return [text_content({"ok": True, "serial": serial, **result})]


XIAOLUXUE_EXERCISE_URL_MARKER = "stu.xiaoluxue.com/exercise"


def xiaoluxue_exercise_page(serial: str) -> dict[str, Any]:
    cached = xiaoluxue_cached_runtime_page(serial, "exercise")
    if cached:
        return cached
    pages = discover_webview_pages(serial)
    page = select_xiaoluxue_webview_page(pages, "exercise")
    xiaoluxue_remember_inferred_page(serial, page)
    return xiaoluxue_remember_page(serial, "exercise", page)


def xiaoluxue_exercise_snapshot_expression() -> str:
    return r"""
(() => {
  const rect = (el) => {
    const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, width: r.width, height: r.height, top: r.top, right: r.right, bottom: r.bottom, left: r.left };
  };
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && style.visibility !== "hidden" && style.display !== "none";
  };
  const text = (el) => (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ");
  const disabled = (el) => {
    const anyEl = el;
    return Boolean(anyEl.disabled) || el.getAttribute("aria-disabled") === "true" || el.getAttribute("data-disabled") === "true";
  };
  const optionKey = (raw) => {
    const trimmed = (raw || "").trim();
    const alpha = /^([A-Z])(?:\s|[.．、)])/.exec(trimmed);
    if (alpha) return alpha[1];
    const judge = /^(TRUE|FALSE|正确|错误)\b/i.exec(trimmed);
    return judge ? judge[1].toUpperCase() : "";
  };
  const buttons = [...document.querySelectorAll("button,[role='button']")]
    .filter(visible)
    .map((el, index) => ({
      index,
      text: text(el),
      disabled: disabled(el),
      className: String(el.className || ""),
      rect: rect(el),
    }))
    .filter((item) => item.text)
    .slice(0, 80);
  const options = [...document.querySelectorAll(".option-button")]
    .filter(visible)
    .map((el, index) => {
      const itemText = text(el);
      return {
        index: index + 1,
        key: optionKey(itemText),
        text: itemText.slice(0, 500),
        disabled: disabled(el),
        selected: /selected|#EAF8FF|rgb\(234,\s*248,\s*255\)/i.test(String(el.className || "") + " " + String(el.getAttribute("style") || "")),
        rect: rect(el),
      };
    });
  const questionSelectors = [
    ".question-main-content",
    ".question-content-left",
    ".question-content-right",
    ".fill-blank-content",
    ".choice-question-view",
    ".question-content-view",
  ];
  const questionBlocks = questionSelectors
    .flatMap((selector) => [...document.querySelectorAll(selector)])
    .filter(visible)
    .map((el) => ({ selector: questionSelectors.find((selector) => el.matches(selector)) || "", text: text(el).slice(0, 1000), rect: rect(el) }))
    .filter((item, index, arr) => item.text && arr.findIndex((other) => other.text === item.text) === index)
    .slice(0, 20);
  const audios = [...document.querySelectorAll("audio")].map((audio) => ({
    src: audio.currentSrc || audio.src || "",
    currentTime: audio.currentTime,
    duration: Number.isFinite(audio.duration) ? audio.duration : null,
    playbackRate: audio.playbackRate,
    paused: audio.paused,
    rect: rect(audio),
  }));
  const progressTexts = [...document.querySelectorAll("body *")]
    .filter((el) => visible(el) && el.children.length === 0)
    .map(text)
    .filter((value) => value && /(\d+\s*\/\s*\d+|第\s*\d+|进度|已答|未答)/.test(value))
    .slice(0, 40);
  const answerBoxes = [...document.querySelectorAll(".math-field-answer-box,[class*='answer-box']")]
    .filter(visible)
    .map((el, index) => ({
      index: index + 1,
      text: text(el).slice(0, 500),
      className: String(el.className || ""),
      rect: rect(el),
    }))
    .slice(0, 20);
  const findAction = (labels) => buttons.find((button) => !button.disabled && labels.some((label) => button.text === label || button.text.includes(label)));
  const params = Object.fromEntries(new URL(location.href).searchParams.entries());
  return {
    app: "xiaoluxue",
    page: "exercise",
    title: document.title,
    url: location.href,
    params,
    viewport: { width: innerWidth, height: innerHeight, devicePixelRatio },
    ready: options.length > 0 || buttons.length > 0 || questionBlocks.length > 0,
    questionText: questionBlocks[0]?.text || "",
    questionBlocks,
    options,
    answerBoxes,
    answerText: answerBoxes[0]?.text || "",
    buttons,
    actions: {
      canSubmit: Boolean(findAction(["提交"])),
      canContinue: Boolean(findAction(["继续", "下一题"])),
      canNextPart: Boolean(findAction(["下一空", "下一问"])),
      canGiveUp: Boolean(findAction(["放弃作答"])),
      canUncertain: Boolean(findAction(["不确定"])),
    },
    audios,
    progressTexts,
    localExerciseKeys: Object.keys(localStorage).filter((key) => /exercise|question|answer|study/i.test(key)).slice(0, 40),
  };
})()
"""


def xiaoluxue_exercise_action_expression(args: dict[str, Any]) -> str:
    config = {
        "actionName": str(args.get("action_name") or args.get("action") or "next"),
        "optionKey": str(args.get("option_key") or "").strip(),
        "optionIndex": int(args["option_index"]) if args.get("option_index") is not None else None,
        "optionText": str(args.get("option_text") or "").strip(),
        "answerText": str(args["answer_text"]) if args.get("answer_text") is not None else None,
        "buttonText": str(args.get("button_text") or "").strip(),
    }
    config_json = json.dumps(config, ensure_ascii=False)
    return f"""
(async () => {{
  const config = {config_json};
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const rect = (el) => {{
    const r = el.getBoundingClientRect();
    return {{ x: r.x, y: r.y, width: r.width, height: r.height, top: r.top, right: r.right, bottom: r.bottom, left: r.left }};
  }};
  const visible = (el) => {{
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && style.visibility !== "hidden" && style.display !== "none";
  }};
  const text = (el) => (el.innerText || el.textContent || "").trim().replace(/\\s+/g, " ");
  const disabled = (el) => {{
    return Boolean(el.disabled) || el.getAttribute("aria-disabled") === "true" || el.getAttribute("data-disabled") === "true";
  }};
  const click = (el) => {{
    el.scrollIntoView({{ block: "center", inline: "center", behavior: "instant" }});
    el.dispatchEvent(new PointerEvent("pointerdown", {{ bubbles: true, cancelable: true, pointerType: "touch" }}));
    el.dispatchEvent(new MouseEvent("mousedown", {{ bubbles: true, cancelable: true, view: window }}));
    el.dispatchEvent(new PointerEvent("pointerup", {{ bubbles: true, cancelable: true, pointerType: "touch" }}));
    el.dispatchEvent(new MouseEvent("mouseup", {{ bubbles: true, cancelable: true, view: window }}));
    el.dispatchEvent(new MouseEvent("click", {{ bubbles: true, cancelable: true, view: window }}));
  }};
  const optionKey = (raw) => {{
    const trimmed = (raw || "").trim();
    const alpha = /^([A-Z])(?:\\s|[.．、)])/.exec(trimmed);
    if (alpha) return alpha[1];
    const judge = /^(TRUE|FALSE|正确|错误)\\b/i.exec(trimmed);
    return judge ? judge[1].toUpperCase() : "";
  }};
  const pack = (el, extra = {{}}) => el ? {{ text: text(el), rect: rect(el), className: String(el.className || ""), ...extra }} : null;
  const buttons = () => [...document.querySelectorAll("button,[role='button']")].filter((el) => visible(el) && !disabled(el));
  const reactFiber = (el) => {{
    if (!el) return null;
    const key = Object.keys(el).find((key) => key.startsWith("__reactFiber$") || key.startsWith("__reactInternalInstance$"));
    return key ? el[key] : null;
  }};
  const findReactFunctionProp = (root, propName) => {{
    const seen = new Set();
    const elements = [];
    if (root) elements.push(root);
    if (root?.querySelectorAll) elements.push(...root.querySelectorAll("*"));
    elements.push(document.activeElement);
    for (const startEl of elements.filter(Boolean)) {{
      let fiber = reactFiber(startEl);
      let depth = 0;
      while (fiber && depth < 40) {{
        if (!seen.has(fiber)) {{
          seen.add(fiber);
          const props = fiber.memoizedProps || fiber.pendingProps || {{}};
          if (typeof props[propName] === "function") return {{ fiber, props, element: startEl }};
        }}
        fiber = fiber.return;
        depth += 1;
      }}
    }}
    return null;
  }};
  const unique = (items) => {{
    const seen = new Set();
    const result = [];
    for (const item of items.filter(Boolean)) {{
      if (!seen.has(item)) {{
        seen.add(item);
        result.push(item);
      }}
    }}
    return result;
  }};
  const isTextField = (el) => {{
    if (!el) return false;
    if (el instanceof HTMLTextAreaElement) return true;
    if (el instanceof HTMLInputElement) return el.type !== "hidden";
    return el.getAttribute?.("contenteditable") === "true" || el.getAttribute?.("role") === "textbox";
  }};
  const fieldValue = (el) => ("value" in el ? el.value : (el.textContent || ""));
  const textFieldCandidates = () => unique([
    document.activeElement,
    ...document.querySelectorAll("textarea.keyboard-input-textarea, textarea, input:not([type='hidden']), [contenteditable='true'], [role='textbox']"),
  ]).filter((el) => isTextField(el) && visible(el));
  const setNativeValue = (el, value) => {{
    if ("value" in el) {{
      const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
      if (descriptor?.set) descriptor.set.call(el, value);
      else el.value = value;
    }} else {{
      el.textContent = value;
    }}
    const inputEvent = typeof InputEvent === "function"
      ? new InputEvent("input", {{ bubbles: true, cancelable: true, data: value, inputType: "insertText" }})
      : new Event("input", {{ bubbles: true, cancelable: true }});
    el.dispatchEvent(inputEvent);
    el.dispatchEvent(new Event("change", {{ bubbles: true, cancelable: true }}));
  }};
  const reactPropsChain = (el) => {{
    const seen = new Set();
    const propsList = [];
    for (let startEl = el; startEl; startEl = startEl.parentElement) {{
      let fiber = reactFiber(startEl);
      let depth = 0;
      while (fiber && depth < 40) {{
        if (!seen.has(fiber)) {{
          seen.add(fiber);
          const props = fiber.memoizedProps || fiber.pendingProps || {{}};
          propsList.push({{ props, element: startEl }});
        }}
        fiber = fiber.return;
        depth += 1;
      }}
    }}
    return propsList;
  }};
  const callReactTextHandlers = (el, value) => {{
    const called = [];
    const valueHandlerNames = ["onValueChange", "onAnswerChange", "onTextChange", "onLatexChange"];
    const eventHandlerNames = ["onChange", "onInput"];
    const event = {{
      target: el,
      currentTarget: el,
      type: "change",
      bubbles: true,
      cancelable: true,
      defaultPrevented: false,
      nativeEvent: new Event("input", {{ bubbles: true, cancelable: true }}),
      preventDefault: () => {{}},
      stopPropagation: () => {{}},
    }};
    for (const {{ props }} of reactPropsChain(el)) {{
      for (const name of valueHandlerNames) {{
        if (typeof props[name] === "function") {{
          try {{
            props[name](value);
            called.push(name);
          }} catch (_err) {{}}
        }}
      }}
      for (const name of eventHandlerNames) {{
        if (typeof props[name] === "function") {{
          try {{
            props[name](event);
            called.push(name);
          }} catch (_err) {{}}
        }}
      }}
    }}
    return [...new Set(called)];
  }};
  const atomValue = (value) => {{
    if (!value) return value;
    if (value.v !== undefined) return value.v;
    if (value.value !== undefined) return value.value;
    try {{
      if (typeof value.peek === "function") return value.peek();
    }} catch (_err) {{}}
    return value;
  }};
  const findQuestionStore = () => {{
    const roots = [
      document.activeElement,
      ...document.querySelectorAll("#right-answer-area,.question-main-content,button,[data-container-id],textarea,input,[contenteditable='true'],[role='textbox']"),
    ].filter(Boolean);
    const seen = new Set();
    for (const root of roots) {{
      let fiber = reactFiber(root);
      let depth = 0;
      while (fiber && depth < 70) {{
        if (!seen.has(fiber)) {{
          seen.add(fiber);
          const props = fiber.memoizedProps || fiber.pendingProps || {{}};
          if (props.questionStore) return props.questionStore;
        }}
        fiber = fiber.return;
        depth += 1;
      }}
    }}
    return null;
  }};
  const fillQuestionStoreAnswer = async (answerText) => {{
    const store = findQuestionStore();
    if (!store || typeof store.updateUserAnswer !== "function") return null;
    const currentQuestion = atomValue(store.currentQuestion) || {{}};
    const questionId = currentQuestion.questionId || currentQuestion.questionContent?.questionId || currentQuestion.id;
    if (!questionId) return {{ ok: false, reason: "question id not found" }};
    const currentAnswers = atomValue(store.currentQuestionAnswers);
    const answerCount = Array.isArray(currentAnswers) && currentAnswers.length ? currentAnswers.length : 1;
    const activeBlankIndex = Number(atomValue(store.activeBlankIndex));
    const blankIndex = Number.isFinite(activeBlankIndex) && activeBlankIndex >= 0
      ? Math.min(activeBlankIndex, answerCount - 1)
      : 0;
    const currentInputMode = Number(atomValue(store.currentInputMode));
    const type = Number.isFinite(currentInputMode) && currentInputMode > 0 ? currentInputMode : 1;
    store.updateUserAnswer({{ questionId, blankIndex, type, content: answerText }});
    if (typeof store.updateUserAnswerType === "function") {{
      try {{ store.updateUserAnswerType(questionId, type); }} catch (_err) {{}}
    }}
    await sleep(120);
    const updatedAnswers = atomValue(store.currentQuestionAnswers);
    const userAnswerDataMap = atomValue(store.userAnswerDataMap);
    const storedAnswers = userAnswerDataMap instanceof Map ? userAnswerDataMap.get(questionId) : null;
    return {{
      ok: true,
      method: "react_question_store",
      questionId,
      blankIndex,
      type,
      chars: answerText.length,
      answerText,
      currentAnswers: Array.isArray(updatedAnswers) ? updatedAnswers : null,
      storedAnswers: Array.isArray(storedAnswers) ? storedAnswers : null,
    }};
  }};
  const ensurePlausibleAnswerDuration = (minMs = 6500) => {{
    const store = findQuestionStore();
    const timerState = store?.timerState;
    if (!timerState || !timerState.value) return null;
    const current = Number(timerState.value.currentTime || 0);
    const total = Number(timerState.value.totalTime || 0);
    const currentQuestionTotal = Number(store?.homeworkProgress?.currentQuestionTotal || 0);
    const nextCurrent = Math.max(current, currentQuestionTotal + minMs, minMs);
    timerState.value = {{
      ...timerState.value,
      currentTime: nextCurrent,
      totalTime: Math.max(total, nextCurrent),
    }};
    return {{ before: current, after: nextCurrent, currentQuestionTotal }};
  }};
  const fillAnswer = async () => {{
    if (config.answerText === null || config.answerText === undefined) {{
      return {{ ok: false, reason: "answer_text is required" }};
    }}
    const answerText = String(config.answerText);
    const directFields = textFieldCandidates();
    const directField = directFields.find((el) => String(el.className || "").includes("keyboard-input-textarea"))
      || directFields.find((el) => el === document.activeElement)
      || directFields[0];
    if (directField) {{
      const before = fieldValue(directField);
      setNativeValue(directField, answerText);
      const reactHandlers = callReactTextHandlers(directField, answerText);
      directField.blur?.();
      await sleep(80);
      return {{
        ok: true,
        method: "dom_text_field",
        chars: answerText.length,
        answerText,
        before,
        after: fieldValue(directField),
        reactHandlers,
        renderedText: text(directField.closest("[class*='answer'],[class*='question'],body") || directField).slice(0, 500),
        rect: rect(directField),
      }};
    }}
    const roots = [
      ...document.querySelectorAll(".math-field-answer-box"),
      ...document.querySelectorAll("[class*='math-field'][class*='answer']"),
      ...document.querySelectorAll("[class*='answer-box']"),
      document.activeElement,
    ].filter(Boolean);
    const root = roots.find((el) => el === document.activeElement || visible(el)) || roots[0] || document.body;
    const found = findReactFunctionProp(root, "onLatexChange");
    if (found) {{
      found.props.onLatexChange(answerText);
      await sleep(100);
      const box = document.querySelector(".math-field-answer-box") || root;
      return {{
        ok: true,
        method: "react_onLatexChange",
        chars: answerText.length,
        answerText,
        renderedText: box ? text(box).slice(0, 500) : "",
        rect: box ? rect(box) : null,
      }};
    }}
    const field = textFieldCandidates()[0] || roots.find((el) => el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement);
    if (field) {{
      setNativeValue(field, answerText);
      const reactHandlers = callReactTextHandlers(field, answerText);
      field.blur?.();
      await sleep(80);
      return {{
        ok: true,
        method: "dom_text_field_fallback",
        chars: answerText.length,
        answerText,
        reactHandlers,
        renderedText: text(root).slice(0, 500),
        rect: root ? rect(root) : null,
      }};
    }}
    const storeResult = await fillQuestionStoreAnswer(answerText);
    if (storeResult) return storeResult;
    return {{ ok: false, reason: "answer input not found" }};
  }};
  const clickButton = (labels, mode = "contains") => {{
    const candidates = buttons();
    const exact = candidates.find((el) => labels.some((label) => text(el) === label));
    const included = candidates.find((el) => labels.some((label) => text(el).includes(label)));
    const target = mode === "exact" ? exact : (exact || included);
    if (!target) return {{ ok: false, reason: "button not found", labels, visibleButtons: candidates.map((el) => text(el)).slice(0, 30) }};
    const timer =
      labels.some((label) => ["提交", "继续提交", "提交自评"].includes(label))
        ? ensurePlausibleAnswerDuration()
        : null;
    click(target);
    return {{ ok: true, clicked: pack(target), labels, timer }};
  }};
  const clickOption = () => {{
    const optionButtons = [...document.querySelectorAll(".option-button")].filter((el) => visible(el) && !disabled(el));
    let target = null;
    if (config.optionIndex != null) target = optionButtons[Number(config.optionIndex) - 1] || null;
    if (!target && config.optionKey) {{
      const key = config.optionKey.toUpperCase();
      target = optionButtons.find((el) => optionKey(text(el)).toUpperCase() === key) || null;
    }}
    if (!target && config.optionText) {{
      target = optionButtons.find((el) => text(el).includes(config.optionText)) || null;
    }}
    if (!target) return {{
      ok: false,
      reason: "option not found",
      optionKey: config.optionKey,
      optionIndex: config.optionIndex,
      optionText: config.optionText,
      visibleOptions: optionButtons.map((el, index) => ({{ index: index + 1, key: optionKey(text(el)), text: text(el).slice(0, 160) }})),
    }};
    click(target);
    return {{ ok: true, clicked: pack(target, {{ index: optionButtons.indexOf(target) + 1, key: optionKey(text(target)) }}) }};
  }};
  const actionName = String(config.actionName || "next");
  let result;
  if (actionName === "fill_answer" || actionName === "input_answer") {{
    result = {{ actionName, ...(await fillAnswer()) }};
  }} else if (actionName === "select_option" || actionName === "answer") {{
    result = {{ actionName, ...(clickOption()) }};
  }} else if (actionName === "submit") {{
    result = {{ actionName, ...(clickButton(["提交"])) }};
  }} else if (actionName === "next") {{
    result = {{ actionName, ...(clickButton(["下一空", "下一问", "下一题", "继续"])) }};
  }} else if (actionName === "continue") {{
    result = {{ actionName, ...(clickButton(["继续", "下一题"])) }};
  }} else if (actionName === "uncertain") {{
    result = {{ actionName, ...(clickButton(["不确定"])) }};
  }} else if (actionName === "give_up") {{
    result = {{ actionName, ...(clickButton(["放弃作答"])) }};
  }} else if (actionName === "button_text") {{
    result = {{ actionName, ...(clickButton([config.buttonText], "contains")) }};
  }} else {{
    result = {{ ok: false, actionName, reason: "unsupported action" }};
  }}
  await sleep(120);
  return result;
}})()
"""


def xiaoluxue_exercise_auto_answer_expression(args: dict[str, Any]) -> str:
    config = {
        "maxSteps": min(max(int(args.get("max_steps", 24)), 1), 80),
        "stepWaitMs": int(min(max(float(args.get("step_wait_sec", 0.45)), 0.2), 3.0) * 1000),
        "clickReport": bool(args.get("click_report", True)),
        "openAnswerText": str(
            args.get("open_answer_text")
            or "I can answer this question clearly. I will make a plan, review what I have learned, and ask my teacher for help when needed."
        ),
    }
    config_json = json.dumps(config, ensure_ascii=False)
    return f"""
(async () => {{
  const config = {config_json};
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const logs = [];
  const text = (el) => (el.innerText || el.textContent || "").trim().replace(/\\s+/g, " ");
  const visible = (el) => {{
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight
      && style.visibility !== "hidden" && style.display !== "none";
  }};
  const disabled = (el) => {{
    return Boolean(el.disabled) || el.getAttribute("aria-disabled") === "true" || el.getAttribute("data-disabled") === "true";
  }};
  const click = (el) => {{
    el.scrollIntoView({{ block: "center", inline: "center", behavior: "instant" }});
    el.dispatchEvent(new PointerEvent("pointerdown", {{ bubbles: true, cancelable: true, pointerType: "touch" }}));
    el.dispatchEvent(new MouseEvent("mousedown", {{ bubbles: true, cancelable: true, view: window }}));
    el.dispatchEvent(new PointerEvent("pointerup", {{ bubbles: true, cancelable: true, pointerType: "touch" }}));
    el.dispatchEvent(new MouseEvent("mouseup", {{ bubbles: true, cancelable: true, view: window }}));
    el.dispatchEvent(new MouseEvent("click", {{ bubbles: true, cancelable: true, view: window }}));
  }};
  const clickAtRatio = (xRatio, yRatio) => {{
    const x = Math.max(1, Math.min(innerWidth - 1, Math.round(innerWidth * xRatio)));
    const y = Math.max(1, Math.min(innerHeight - 1, Math.round(innerHeight * yRatio)));
    const target = document.elementFromPoint(x, y);
    if (!target) return "";
    click(target);
    return `point(${{x}},${{y}}):${{text(target).slice(0, 40)}}`;
  }};
  const buttons = () => [...document.querySelectorAll("button,[role='button']")].filter((el) => visible(el) && !disabled(el));
  const clickExact = (labels) => {{
    const target = buttons().find((el) => labels.includes(text(el)));
    if (!target) return "";
    click(target);
    return text(target);
  }};
  const clickContains = (labels) => {{
    const target = buttons().find((el) => labels.some((label) => text(el).includes(label)));
    if (!target) return "";
    click(target);
    return text(target);
  }};
  const clickByOptionKey = (key) => {{
    const normalized = String(key || "").trim().toUpperCase();
    if (!normalized) return "";
    const candidates = buttons().filter((el) => String(el.className || "").includes("option-button"));
    const target = candidates.find((el) => {{
      const optionText = text(el).toUpperCase();
      return optionText === normalized
        || optionText.startsWith(`${{normalized}} `)
        || optionText.startsWith(`${{normalized}}.`)
        || optionText.startsWith(`${{normalized}}．`)
        || optionText.startsWith(`${{normalized}}、`)
        || optionText.startsWith(`${{normalized}})`);
    }});
    if (!target) return "";
    click(target);
    return text(target);
  }};
  const reactFiber = (el) => {{
    if (!el) return null;
    const key = Object.keys(el).find((item) => item.startsWith("__reactFiber$") || item.startsWith("__reactInternalInstance$"));
    return key ? el[key] : null;
  }};
  const atomValue = (value) => {{
    if (!value) return value;
    if (value.v !== undefined) return value.v;
    if (value.value !== undefined) return value.value;
    try {{
      if (typeof value.peek === "function") return value.peek();
    }} catch (_err) {{}}
    return value;
  }};
  const findQuestionStore = () => {{
    const roots = [
      document.activeElement,
      ...document.querySelectorAll("#right-answer-area,.question-main-content,.choice-question-view,button,[data-container-id],textarea,input,[contenteditable='true'],[role='textbox']"),
    ].filter(Boolean);
    const seen = new Set();
    for (const root of roots) {{
      let fiber = reactFiber(root);
      let depth = 0;
      while (fiber && depth < 90) {{
        if (!seen.has(fiber)) {{
          seen.add(fiber);
          const props = fiber.memoizedProps || fiber.pendingProps || {{}};
          if (props.questionStore) return props.questionStore;
        }}
        fiber = fiber.return;
        depth += 1;
      }}
    }}
    return null;
  }};
  const answerRows = (answer) => {{
    const rows = answer?.answerOptionMatrix || (answer?.answerOptionList ? [answer.answerOptionList] : []);
    return rows.map((row) => Array.isArray(row) ? row[0] : row).filter(Boolean);
  }};
  const answerValue = (option) => String(option?.optionVal || option?.optionKey || "").replace(/&nbsp;/g, " ").trim();
  const answerKey = (option) => String(option?.optionKey || "").trim();
  const clickNext = () => clickExact(["继续提交", "下一空", "下一问", "下一题", "提交", "提交自评", "查看报告", "继续"]);
  const ensurePlausibleAnswerDuration = (minMs = 6500) => {{
    const store = findQuestionStore();
    const timerState = store?.timerState;
    if (!timerState || !timerState.value) return null;
    const current = Number(timerState.value.currentTime || 0);
    const total = Number(timerState.value.totalTime || 0);
    const currentQuestionTotal = Number(store?.homeworkProgress?.currentQuestionTotal || 0);
    const nextCurrent = Math.max(current, currentQuestionTotal + minMs, minMs);
    timerState.value = {{
      ...timerState.value,
      currentTime: nextCurrent,
      totalTime: Math.max(total, nextCurrent),
    }};
    return {{ before: current, after: nextCurrent, currentQuestionTotal }};
  }};
  const textFields = () => [
    ...document.querySelectorAll("textarea.keyboard-input-textarea, textarea, input:not([type='hidden']), [contenteditable='true'], [role='textbox']")
  ].filter((el) => visible(el) && !disabled(el));
  const setFieldValue = (el, value) => {{
    if ("value" in el) {{
      const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
      if (descriptor?.set) descriptor.set.call(el, value);
      else el.value = value;
    }} else {{
      el.textContent = value;
    }}
    const inputEvent = typeof InputEvent === "function"
      ? new InputEvent("input", {{ bubbles: true, cancelable: true, data: value, inputType: "insertText" }})
      : new Event("input", {{ bubbles: true, cancelable: true }});
    el.dispatchEvent(inputEvent);
    el.dispatchEvent(new Event("change", {{ bubbles: true, cancelable: true }}));
  }};
  const selfEvaluateAsCorrect = () => {{
    const body = document.body.innerText || "";
    const hasSelfEval = body.includes("提交自评") || buttons().some((el) => text(el).includes("提交自评"));
    if (!hasSelfEval) return "";
    const clickedChoice = clickExact(["我答对了"]) || clickContains(["我答对了"]) || clickAtRatio(0.585, 0.565);
    const timer = ensurePlausibleAnswerDuration();
    const clickedSubmit = clickExact(["提交自评", "查看报告", "继续"]) || clickContains(["提交自评", "查看报告", "继续"]);
    return clickedChoice || clickedSubmit ? `${{clickedChoice}} -> ${{clickedSubmit}} timer=${{timer?.after || ""}}` : "";
  }};
  for (let i = 0; i < config.maxSteps; i += 1) {{
    const guard = clickExact(["我知道了", "知道了"]);
    if (guard) {{
      logs.push({{ i, action: "dismiss", clicked: guard }});
      await sleep(config.stepWaitMs);
      continue;
    }}
    const confirmSubmit = clickExact(["继续提交"]);
    if (confirmSubmit) {{
      const timer = ensurePlausibleAnswerDuration();
      logs.push({{ i, action: "confirm-submit", clicked: confirmSubmit, timer }});
      await sleep(config.stepWaitMs);
      continue;
    }}
    const selfEval = selfEvaluateAsCorrect();
    if (selfEval) {{
      logs.push({{ i, action: "self-evaluate", clicked: selfEval }});
      await sleep(config.stepWaitMs);
      continue;
    }}
    const reportButton = buttons().find((el) => text(el) === "查看报告");
    if (reportButton) {{
      logs.push({{ i, action: "report", clicked: text(reportButton) }});
      if (config.clickReport) setTimeout(() => click(reportButton), 0);
      return {{
        ok: true,
        completed: true,
        reportScheduled: Boolean(config.clickReport),
        logs,
        text: document.body.innerText.slice(0, 800),
      }};
    }}
    const continued = clickExact(["继续"]);
    if (continued) {{
      logs.push({{ i, action: "continue", clicked: continued }});
      await sleep(config.stepWaitMs);
      continue;
    }}
    const store = findQuestionStore();
    if (!store) {{
      logs.push({{ i, action: "no-store", text: document.body.innerText.slice(0, 300) }});
      break;
    }}
    const question = atomValue(store.currentQuestion) || {{}};
    const questionId = question.questionId || question.questionContent?.questionId || question.id;
    const status = atomValue(store.questionStatus);
    if (status === "submitted") {{
      logs.push({{ i, questionId, action: "submitted" }});
      await sleep(Math.max(300, Math.floor(config.stepWaitMs / 2)));
      continue;
    }}
    const answers = answerRows(question.questionAnswer).map((option) => ({{
      key: answerKey(option),
      value: answerValue(option),
    }}));
    const currentAnswers = atomValue(store.currentQuestionAnswers);
    const activeBlank = Number(atomValue(store.activeBlankIndex));
    const answerIndex = Number.isFinite(activeBlank) && activeBlank >= 0
      ? Math.min(activeBlank, Math.max(answers.length - 1, 0))
      : 0;
    const currentAnswer = answers[answerIndex] || answers[0] || {{}};
    let selected = "";
    if (currentAnswer.key) {{
      selected = clickByOptionKey(currentAnswer.key);
    }} else if (currentAnswer.value === "true") {{
      selected = clickByOptionKey("TRUE") || clickExact(["正确"]);
    }} else if (currentAnswer.value === "false") {{
      selected = clickByOptionKey("FALSE") || clickExact(["错误"]);
    }} else {{
      const checked = buttons().find((el) => String(el.className || "").includes("option-button") && text(el).includes("✅"));
      if (checked) {{
        click(checked);
        selected = text(checked);
      }}
    }}
    if (selected) {{
      await sleep(Math.max(220, Math.floor(config.stepWaitMs * 0.55)));
      const timer = ensurePlausibleAnswerDuration();
      const next = clickNext();
      logs.push({{ i, questionId, action: "choice", answer: currentAnswer, activeBlank: answerIndex, selected, next, timer }});
      await sleep(config.stepWaitMs);
      continue;
    }}
    const field = textFields()[0];
    if (field && !answers.length) {{
      setFieldValue(field, config.openAnswerText);
      field.blur?.();
      await sleep(Math.max(160, Math.floor(config.stepWaitMs / 2)));
      const timer = ensurePlausibleAnswerDuration();
      const submit = clickExact(["提交"]);
      logs.push({{ i, questionId, action: "open-answer", chars: config.openAnswerText.length, submit, timer }});
      await sleep(config.stepWaitMs);
      continue;
    }}
    if (questionId && answers.length && typeof store.updateUserAnswer === "function") {{
      const count = Math.max(answers.length, Array.isArray(currentAnswers) ? currentAnswers.length : 1);
      for (let blankIndex = 0; blankIndex < count; blankIndex += 1) {{
        const item = answers[Math.min(blankIndex, answers.length - 1)] || {{}};
        const content = item.value || item.key || "";
        try {{
          store.updateUserAnswer({{ questionId, blankIndex, type: 1, content, selfEvaluation: 1 }});
        }} catch (_err) {{}}
        if (typeof store.updateUserAnswerType === "function") {{
          try {{ store.updateUserAnswerType(questionId, 1); }} catch (_err) {{}}
        }}
      }}
      await sleep(Math.max(250, Math.floor(config.stepWaitMs / 3)));
      const clicks = [];
      for (let blankIndex = 0; blankIndex < Math.max(1, count); blankIndex += 1) {{
        const timer = ensurePlausibleAnswerDuration();
        const next = clickNext();
        if (!next) break;
        clicks.push({{ next, timer }});
        await sleep(config.stepWaitMs);
        if (["提交", "下一问", "下一题"].includes(next)) break;
      }}
      logs.push({{ i, questionId, action: "fill", answers, clicks }});
      continue;
    }}
    const fallback = clickContains(["提交", "下一空", "下一问", "下一题", "继续"]);
    logs.push({{ i, questionId, action: "fallback", fallback, buttons: buttons().map(text).slice(0, 20) }});
    if (!fallback) break;
    await sleep(config.stepWaitMs);
  }}
  return {{
    ok: true,
    completed: false,
    logs,
    text: document.body.innerText.slice(0, 800),
    buttons: buttons().map((el) => ({{ text: text(el), disabled: disabled(el) }})).slice(0, 20),
  }};
}})()
"""


def tool_xiaoluxue_exercise_snapshot(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    page = xiaoluxue_exercise_page(serial)
    snapshot = cdp_eval_value(page, xiaoluxue_exercise_snapshot_expression(), timeout=min(float(args.get("timeout_sec", 10)), 60))
    return [text_content({"ok": True, "serial": serial, "pageId": page.get("id"), "socket": page.get("socket"), "snapshot": snapshot})]


def tool_xiaoluxue_exercise_action(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    page = xiaoluxue_exercise_page(serial)
    result = cdp_eval_value(page, xiaoluxue_exercise_action_expression(args), timeout=min(float(args.get("timeout_sec", 10)), 60))
    recorded_args = {
        key: args[key]
        for key in ("action_name", "option_key", "option_index", "option_text", "answer_text", "button_text")
        if key in args
    }
    append_recording_step(
        serial,
        "xiaoluxue_exercise_action",
        recorded_args,
        {"ok": True, "action": "xiaoluxue_exercise_action", "webview": True, "result": result},
    )
    return [text_content({"ok": True, "serial": serial, "pageId": page.get("id"), "socket": page.get("socket"), "result": result})]


def run_xiaoluxue_exercise_fast_path(serial: str, args: dict[str, Any], *, record: bool) -> dict[str, Any]:
    timeout = min(float(args.get("timeout_sec", 15)), 60)
    wait_sec = min(max(float(args.get("after_action_wait_sec", 0.4)), 0), 10)
    page = xiaoluxue_exercise_page(serial)
    steps: list[dict[str, Any]] = []
    action_name = str(args.get("action_name") or ("button_text" if args.get("button_text") else "next"))

    has_answer_text = args.get("answer_text") is not None
    has_option = any(args.get(key) is not None and str(args.get(key)).strip() for key in ("option_key", "option_index", "option_text"))
    if action_name == "auto_answer":
        auto_result = cdp_eval_value(page, xiaoluxue_exercise_auto_answer_expression(args), timeout=timeout)
        steps.append({"action": "auto_answer", "result": auto_result})
        if wait_sec:
            time.sleep(wait_sec)
    elif has_answer_text:
        answer_args = {**args, "action_name": "fill_answer"}
        answer_result = cdp_eval_value(page, xiaoluxue_exercise_action_expression(answer_args), timeout=timeout)
        steps.append({"action": "fill_answer", "result": answer_result})
        if wait_sec:
            time.sleep(wait_sec)

    if has_option:
        page = xiaoluxue_exercise_page(serial)
        select_args = {**args, "action_name": "select_option"}
        select_result = cdp_eval_value(page, xiaoluxue_exercise_action_expression(select_args), timeout=timeout)
        steps.append({"action": "select_option", "result": select_result})
        if wait_sec:
            time.sleep(wait_sec)

    if bool(args.get("submit", False)):
        page = xiaoluxue_exercise_page(serial)
        submit_result = cdp_eval_value(page, xiaoluxue_exercise_action_expression({"action_name": "submit"}), timeout=timeout)
        steps.append({"action": "submit", "result": submit_result})
        if wait_sec:
            time.sleep(wait_sec)
        if bool(args.get("continue_after_submit", False)):
            page = xiaoluxue_exercise_page(serial)
            continue_result = cdp_eval_value(page, xiaoluxue_exercise_action_expression({"action_name": "continue"}), timeout=timeout)
            steps.append({"action": "continue", "result": continue_result})
    elif not has_option and not has_answer_text:
        action_args = {**args, "action_name": action_name}
        action_result_value = cdp_eval_value(page, xiaoluxue_exercise_action_expression(action_args), timeout=timeout)
        steps.append({"action": action_name, "result": action_result_value})

    snapshot: dict[str, Any] | None = None
    page_error: str | None = None
    try:
        page = xiaoluxue_exercise_page(serial)
        snapshot = cdp_eval_value(page, xiaoluxue_exercise_snapshot_expression(), timeout=timeout)
    except AndroidUseError as exc:
        page_error = str(exc)
    result = {
        "ok": True,
        "action": "xiaoluxue_exercise_fast_path",
        "webview": True,
        "pageId": page.get("id") if snapshot is not None else None,
        "socket": page.get("socket") if snapshot is not None else None,
        "steps": steps,
        "snapshot": snapshot,
    }
    if page_error:
        result["pageError"] = page_error
    if record:
        recorded_args = {
            key: args[key]
            for key in (
                "option_key",
                "option_index",
                "option_text",
                "answer_text",
                "submit",
                "continue_after_submit",
                "action_name",
                "button_text",
                "after_action_wait_sec",
                "timeout_sec",
                "max_steps",
                "step_wait_sec",
                "click_report",
            )
            if key in args
        }
        append_recording_step(serial, "xiaoluxue_exercise_fast_path", recorded_args, result)
    return result


def tool_xiaoluxue_exercise_fast_path(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    result = run_xiaoluxue_exercise_fast_path(serial, args, record=True)
    return [text_content({"ok": True, "serial": serial, **result})]


def tool_start_recording(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    if active_recording(serial):
        raise AndroidUseError(f"Recording is already active for device {serial}. Stop it first.")
    name = str(args.get("name") or f"recording-{serial}").strip()
    recording_id = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime())}-{slugify(name, 'recording')}"
    recording_dir = RECORDINGS_DIR / recording_id
    recording_dir.mkdir(parents=True, exist_ok=True)
    recording = {
        "schema_version": 1,
        "id": recording_id,
        "name": name,
        "serial": serial,
        "created_at": timestamp_iso(),
        "dir": str(recording_dir),
        "include_screenshots": bool(args.get("include_screenshots", False)),
        "redact_text": bool(args.get("redact_text", False)),
        "after_delay_sec": float(args.get("after_delay_sec", 0.25)),
        "steps": [],
        "errors": [],
        "initial_snapshot": capture_record_snapshot(
            serial,
            include_screenshot=bool(args.get("include_screenshots", False)),
            base_dir=recording_dir,
            name="000-initial",
        ),
    }
    ACTIVE_RECORDINGS[serial] = recording
    trace_path = recording_dir / "trace.json"
    return [
        text_content(
            {
                "ok": True,
                "serial": serial,
                "recording_id": recording_id,
                "trace_path": str(trace_path),
                "message": "Recording started. Deterministic Android actions issued through this plugin will be captured.",
            }
        )
    ]


def tool_record_checkpoint(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    label = str(args.get("label") or "checkpoint").strip()
    step = append_recording_checkpoint(serial, label)
    return [
        text_content(
            {
                "ok": True,
                "serial": serial,
                "index": step["index"],
                "label": label,
                "fingerprint": step["snapshot"].get("fingerprint"),
            }
        )
    ]


def tool_stop_recording(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    recording = ACTIVE_RECORDINGS.pop(serial, None)
    if not recording:
        raise AndroidUseError(f"No active recording for device {serial}.")
    recording["ended_at"] = timestamp_iso()
    recording["final_snapshot"] = capture_record_snapshot(
        serial,
        include_screenshot=bool(recording.get("include_screenshots")),
        base_dir=Path(recording["dir"]),
        name="999-final",
    )
    trace_path = Path(recording["dir"]) / "trace.json"
    write_json(trace_path, recording)
    return [
        text_content(
            {
                "ok": True,
                "serial": serial,
                "recording_id": recording["id"],
                "trace_path": str(trace_path),
                "steps": len(recording.get("steps", [])),
                "errors": recording.get("errors", []),
            }
        )
    ]


def resolve_trace_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_dir():
        trace_path = path / "trace.json"
        if trace_path.exists():
            return trace_path
    if path.exists():
        return path
    candidates = [
        RECORDINGS_DIR / value / "trace.json",
        RECORDINGS_DIR / f"{value}.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise AndroidUseError(f"Recording trace not found: {value}")


def resolve_recipe_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.exists():
        return path
    candidates = [RECIPES_DIR / value, RECIPES_DIR / f"{value}.json"]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise AndroidUseError(f"Recipe not found: {value}")


def tool_create_recipe(args: dict[str, Any]) -> list[dict[str, Any]]:
    trace_path = resolve_trace_path(str(args["trace"]))
    trace = json.loads(trace_path.read_text())
    recipe_name = str(args.get("name") or trace.get("name") or trace_path.parent.name)
    recipe = recipe_from_trace(trace, recipe_name)
    output_path = Path(args["output_path"]).expanduser() if args.get("output_path") else RECIPES_DIR / f"{slugify(recipe_name, 'recipe')}.json"
    write_json(output_path, recipe)
    return [
        text_content(
            {
                "ok": True,
                "trace_path": str(trace_path),
                "recipe_path": str(output_path),
                "steps": len(recipe.get("steps", [])),
                "name": recipe.get("name"),
            }
        )
    ]


def tool_replay_recipe(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    recipe_path = resolve_recipe_path(str(args["recipe"]))
    recipe = json.loads(recipe_path.read_text())
    dry_run = bool(args.get("dry_run", False))
    strict_verify = bool(args.get("strict_verify", False))
    step_delay_sec = min(float(args.get("step_delay_sec", 0.25)), 5.0)
    results: list[dict[str, Any]] = []
    for index, step in enumerate(recipe.get("steps", []), start=1):
        if not isinstance(step, dict):
            continue
        step_result = execute_recipe_step(serial, step, dry_run=dry_run)
        verification = {"checked": False, "ok": True}
        if not dry_run:
            if step_delay_sec > 0:
                time.sleep(step_delay_sec)
            verification = verify_recipe_step(serial, step)
            if strict_verify and not verification.get("ok", True):
                results.append({"index": index, "result": step_result, "verification": verification})
                break
        results.append({"index": index, "result": step_result, "verification": verification})
    return [
        text_content(
            {
                "ok": all(item["verification"].get("ok", True) for item in results),
                "serial": serial,
                "recipe_path": str(recipe_path),
                "dry_run": dry_run,
                "steps": results,
            }
        )
    ]


def tool_index_source(args: dict[str, Any]) -> list[dict[str, Any]]:
    source_path = Path(str(args["source_path"])).expanduser()
    max_files = max(1, min(int(args.get("max_files", 2000)), 20000))
    app_map = index_source_tree(source_path, max_files=max_files)
    default_name = f"app-map-{slugify(source_path.name or 'source')}.json"
    output_path = Path(args["output_path"]).expanduser() if args.get("output_path") else SOURCE_MAP_DIR / default_name
    write_json(output_path, app_map)
    return [
        text_content(
            {
                "ok": True,
                "source_path": str(source_path),
                "app_map_path": str(output_path),
                "files_scanned": app_map["files_scanned"],
                "files_indexed": app_map["files_indexed"],
                "controls": len(app_map["controls"]),
            }
        )
    ]


def build_scrcpy_command(args: dict[str, Any], serial: str) -> tuple[list[str], int | None, int | None, str]:
    command = [scrcpy_binary(), "--serial", serial]
    max_size = int(args.get("max_size", 1280))
    window_width = args.get("window_width")
    window_height = args.get("window_height")
    window_title = str(args.get("window_title") or f"Android {serial}")
    keyboard_mode = str(args.get("keyboard") or "sdk").strip().lower()
    if keyboard_mode not in {"disabled", "sdk", "uhid", "aoa"}:
        raise AndroidUseError("keyboard must be one of: disabled, sdk, uhid, aoa.")
    command.extend(["--window-title", window_title])
    command.extend(["--keyboard", keyboard_mode])
    if args.get("prefer_text", True) and keyboard_mode == "sdk":
        command.append("--prefer-text")
    if args.get("legacy_paste", False):
        command.append("--legacy-paste")
    if not args.get("audio", False):
        command.append("--no-audio")
    if max_size:
        command.extend(["-m", str(max_size)])
    if args.get("bit_rate"):
        command.extend(["-b", str(args["bit_rate"])])
    if args.get("stay_awake"):
        command.append("--stay-awake")
    if args.get("turn_screen_off"):
        command.append("--turn-screen-off")
    if args.get("always_on_top"):
        command.append("--always-on-top")
    if args.get("borderless", False):
        command.append("--window-borderless")
    if args.get("fixed_window", True):
        if not window_width or not window_height:
            size = png_size(screenshot_png(serial))
            screen_width = int(size.get("width") or 0)
            screen_height = int(size.get("height") or 0)
            if screen_width > 0 and screen_height > 0:
                scale = 1.0
                longest_side = max(screen_width, screen_height)
                if max_size and longest_side > max_size:
                    scale = max_size / longest_side
                window_width = window_width or max(1, round(screen_width * scale))
                window_height = window_height or max(1, round(screen_height * scale))
        if window_width:
            command.extend(["--window-width", str(int(window_width))])
        if window_height:
            command.extend(["--window-height", str(int(window_height))])
    extra_args = args.get("extra_args") or []
    if not isinstance(extra_args, list):
        raise AndroidUseError("extra_args must be a list of scrcpy command arguments.")
    command.extend(str(item) for item in extra_args)
    return command, int(window_width) if window_width else None, int(window_height) if window_height else None, window_title


def host_process_lines() -> list[str]:
    try:
        stdout, _stderr = run_command(["ps", "-axo", "pid=,command="], timeout=3)
    except AndroidUseError:
        return []
    return [line.strip() for line in decode_bytes(stdout).splitlines() if line.strip()]


def host_process_rows() -> list[dict[str, Any]]:
    try:
        stdout, _stderr = run_command(["ps", "-axo", "pid=,ppid=,command="], timeout=3)
    except AndroidUseError:
        return []
    rows: list[dict[str, Any]] = []
    for raw_line in decode_bytes(stdout).splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)", raw_line)
        if match:
            rows.append({"pid": int(match.group(1)), "ppid": int(match.group(2)), "command": match.group(3).strip()})
    return rows


def is_visible_scrcpy_command(command: str, serial: str) -> bool:
    if serial not in command:
        return False
    is_supervisor = "scrcpy_supervisor.py" in command
    is_scrcpy_binary = re.search(r"(^|\s)(?:\S*/)?scrcpy(\s|$)", command) is not None
    if not is_supervisor and not is_scrcpy_binary:
        return False
    if "--no-window" in command:
        return False
    if "android_webrtc_viewer.py" in command:
        return False
    return True


def scrcpy_visible_processes_for_serial(serial: str) -> list[dict[str, Any]]:
    return [row for row in host_process_rows() if is_visible_scrcpy_command(str(row.get("command") or ""), serial)]


def scrcpy_visible_process_for_serial(serial: str) -> str | None:
    for line in host_process_lines():
        if serial not in line:
            continue
        if is_visible_scrcpy_command(line, serial):
            return line
    return None


def prune_duplicate_scrcpy_processes(serial: str) -> list[int]:
    rows = scrcpy_visible_processes_for_serial(serial)
    supervisors = [row for row in rows if "scrcpy_supervisor.py" in str(row.get("command") or "")]
    if len(supervisors) <= 1:
        return []
    supervisors.sort(key=lambda row: int(row["pid"]), reverse=True)
    keep_pid = int(supervisors[0]["pid"])
    stopped: list[int] = []
    for row in supervisors[1:]:
        pid = int(row["pid"])
        if pid == keep_pid:
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, 15)
            stopped.append(pid)
    if stopped:
        time.sleep(0.2)
    return stopped


def ensure_default_scrcpy_window(serial: str, args: dict[str, Any]) -> dict[str, Any]:
    if not bool(args.get("show_scrcpy", True)):
        return {"ok": True, "skipped": "disabled"}
    launch_lock = lock_path(f"scrcpy-launch-{serial}")
    with exclusive_file_lock(launch_lock, blocking=True) as locked:
        if not locked:
            return {"ok": False, "error": "could not acquire scrcpy launch lock"}
        duplicate_pids = prune_duplicate_scrcpy_processes(serial)
        existing = scrcpy_visible_process_for_serial(serial)
        if existing:
            if not bool(args.get("respect_manual_close", False)):
                clear_scrcpy_user_closed(serial)
            return {
                "ok": True,
                "skipped": "already-running",
                "process": existing[:500],
                "stopped_duplicate_pids": duplicate_pids,
                "launch_lock": str(launch_lock),
            }
        user_closed = read_scrcpy_user_closed(serial)
        if user_closed and bool(args.get("respect_manual_close", False)):
            return {
                "ok": True,
                "skipped": "user-closed",
                "user_closed": user_closed,
                "launch_lock": str(launch_lock),
            }
        clear_scrcpy_user_closed(serial)
        try:
            content = tool_start_scrcpy(
                {
                    "serial": serial,
                    "keep_alive": args.get("scrcpy_keep_alive", True),
                    "fixed_window": args.get("scrcpy_fixed_window", True),
                    "lock_window_size": args.get("scrcpy_lock_window_size", True),
                    "window_width": args.get("scrcpy_window_width"),
                    "window_height": args.get("scrcpy_window_height"),
                    "max_size": args.get("scrcpy_max_size", 1280),
                }
            )
            payload = content[0].get("text") if content else "{}"
            parsed = json.loads(str(payload)) if isinstance(payload, str) else payload
            if isinstance(parsed, dict):
                parsed["launch_lock"] = str(launch_lock)
                return parsed
            return {"ok": True, "result": parsed, "launch_lock": str(launch_lock)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "launch_lock": str(launch_lock)}


def resident_scrcpy_enabled() -> bool:
    return env_flag("ANDROID_USE_SCRCPY_RESIDENT", True)


def resident_scrcpy_interval_sec() -> float:
    return max(1.0, env_float("ANDROID_USE_SCRCPY_RESIDENT_INTERVAL_SEC", 2.0))


def connected_device_serials() -> list[str]:
    try:
        devices = [device for device in list_devices() if device.get("state") == "device"]
    except Exception:
        return []
    configured = os.environ.get("ANDROID_USE_SCRCPY_RESIDENT_SERIALS") or os.environ.get("ANDROID_USE_SERIAL") or os.environ.get("ANDROID_SERIAL")
    if configured:
        wanted = split_configured_values(configured)
        connected = {str(device.get("serial") or "") for device in devices}
        return [serial for serial in wanted if serial in connected]
    devices = dedupe_connected_devices(devices)
    physical = [str(device.get("serial") or "") for device in devices if not str(device.get("serial") or "").startswith("emulator-")]
    if physical:
        return physical[:1]
    return [str(devices[0]["serial"])] if devices else []


def ensure_resident_scrcpy_windows() -> dict[str, Any]:
    serials = connected_device_serials()
    ensured: list[dict[str, Any]] = []
    for serial in serials:
        result = ensure_default_scrcpy_window(
            serial,
            {
                "show_scrcpy": True,
                "scrcpy_keep_alive": True,
                "scrcpy_fixed_window": True,
                "scrcpy_lock_window_size": True,
                "scrcpy_max_size": 1280,
                "respect_manual_close": True,
            },
        )
        ensured.append({"serial": serial, **result})
    return {"ok": True, "serials": serials, "ensured": ensured}


def update_resident_scrcpy_status(**updates: Any) -> None:
    with SCRCPY_RESIDENT_LOCK:
        SCRCPY_RESIDENT_STATUS.update(updates)


def try_acquire_resident_scrcpy_monitor_lock() -> tuple[bool, str]:
    global SCRCPY_RESIDENT_LOCK_HANDLE
    monitor_lock_path = lock_path("scrcpy-resident-monitor")
    if SCRCPY_RESIDENT_LOCK_HANDLE is not None:
        return True, str(monitor_lock_path)
    monitor_lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = monitor_lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return False, str(monitor_lock_path)
    SCRCPY_RESIDENT_LOCK_HANDLE = handle
    return True, str(monitor_lock_path)


def resident_scrcpy_loop() -> None:
    update_resident_scrcpy_status(enabled=True, running=True, last_error=None)
    while resident_scrcpy_enabled():
        try:
            result = ensure_resident_scrcpy_windows()
            update_resident_scrcpy_status(
                enabled=True,
                running=True,
                serials=result.get("serials", []),
                last_check_at=time.time(),
                last_result=result,
                last_error=None,
            )
        except Exception as exc:
            update_resident_scrcpy_status(
                enabled=True,
                running=True,
                last_check_at=time.time(),
                last_error=str(exc),
            )
        time.sleep(resident_scrcpy_interval_sec())
    update_resident_scrcpy_status(enabled=False, running=False)


def start_resident_scrcpy_monitor() -> dict[str, Any]:
    global SCRCPY_RESIDENT_THREAD
    if not resident_scrcpy_enabled():
        update_resident_scrcpy_status(enabled=False, running=False, last_error=None)
        return {"ok": True, "enabled": False, "skipped": "disabled-by-env"}
    with SCRCPY_RESIDENT_LOCK:
        if SCRCPY_RESIDENT_THREAD is not None and SCRCPY_RESIDENT_THREAD.is_alive():
            return {"ok": True, "enabled": True, "skipped": "already-running"}
        acquired, monitor_lock = try_acquire_resident_scrcpy_monitor_lock()
        if not acquired:
            SCRCPY_RESIDENT_STATUS.update(
                {
                    "enabled": True,
                    "running": False,
                    "last_error": None,
                    "monitor_lock": monitor_lock,
                    "skipped": "another-mcp-process-owns-monitor",
                }
            )
            return {"ok": True, "enabled": True, "skipped": "another-mcp-process-owns-monitor", "monitor_lock": monitor_lock}
        SCRCPY_RESIDENT_THREAD = threading.Thread(
            target=resident_scrcpy_loop,
            name="android-use-scrcpy-resident",
            daemon=True,
        )
        SCRCPY_RESIDENT_THREAD.start()
        return {"ok": True, "enabled": True, "started": True, "monitor_lock": monitor_lock}


def tool_scrcpy_resident_status(_args: dict[str, Any]) -> list[dict[str, Any]]:
    started = start_resident_scrcpy_monitor()
    with SCRCPY_RESIDENT_LOCK:
        status = dict(SCRCPY_RESIDENT_STATUS)
    return [text_content({"ok": True, "monitor": started, "status": status})]


def serials_from_arg(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return unique_ordered(str(item) for item in value)
    return unique_ordered(split_configured_values(str(value)))


def read_json_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def terminate_process(process: subprocess.Popen[bytes], *, timeout: float = 3) -> bool:
    if process.poll() is not None:
        return False
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        return True
    return True


def tool_start_scrcpy(args: dict[str, Any]) -> list[dict[str, Any]]:
    requested_serials = serials_from_arg(args.get("serials"))
    if requested_serials:
        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for requested_serial in requested_serials:
            child_args = {key: value for key, value in args.items() if key != "serials"}
            child_args["serial"] = requested_serial
            try:
                content = tool_start_scrcpy(child_args)
                payload = content[0].get("text") if content else "{}"
                parsed = json.loads(str(payload)) if isinstance(payload, str) else payload
                results.append(parsed if isinstance(parsed, dict) else {"result": parsed})
            except Exception as exc:
                errors.append({"serial": requested_serial, "error": str(exc)})
        if not results:
            detail = "\n".join(f"{item['serial']}: {item['error']}" for item in errors[-5:])
            raise AndroidUseError("scrcpy failed to start for all requested devices.\n" + detail)
        return [
            text_content(
                {
                    "ok": not errors,
                    "serials": requested_serials,
                    "results": results,
                    "errors": errors,
                }
            )
        ]

    serial = choose_serial(args.get("serial"))
    if not bool(args.get("force", False)):
        duplicate_pids = prune_duplicate_scrcpy_processes(serial)
        existing = scrcpy_visible_process_for_serial(serial)
        if existing:
            clear_scrcpy_user_closed(serial)
            return [
                text_content(
                    {
                        "ok": True,
                        "serial": serial,
                        "skipped": "already-running",
                        "process": existing[:500],
                        "stopped_duplicate_pids": duplicate_pids,
                        "display": "scrcpy is already running for this device.",
                    }
                )
            ]
    command, window_width, window_height, window_title = build_scrcpy_command(args, serial)
    clear_scrcpy_user_closed(serial)

    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    log_path = SCREEN_DIR / "scrcpy.log"
    keep_alive = bool(args.get("keep_alive", True))
    ready_path = SCREEN_DIR / f"scrcpy-{uuid.uuid4().hex}.ready.json"
    user_closed_path = scrcpy_user_closed_path(serial)
    launch_command = command
    if keep_alive:
        supervisor_script = PLUGIN_ROOT / "scripts" / "scrcpy_supervisor.py"
        if not supervisor_script.exists():
            raise AndroidUseError(f"scrcpy supervisor script not found: {supervisor_script}")
        launch_command = [
            sys.executable,
            str(supervisor_script),
            "--ready-file",
            str(ready_path),
            "--ready-after-sec",
            "0.8",
            "--early-exit-sec",
            "2.0",
            "--manual-exit-after-sec",
            "2.0",
            "--max-early-restarts",
            "3",
            "--restart-delay-sec",
            "0.7",
            "--user-closed-file",
            str(user_closed_path),
            "--",
            *command,
        ]
    log_handle = log_path.open("ab")
    try:
        process = subprocess.Popen(
            launch_command,
            stdout=log_handle,
            stderr=log_handle,
            env=tool_env(),
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        log_handle.close()
        raise AndroidUseError(f"Command not found: {launch_command[0]}") from exc
    log_handle.close()
    startup_deadline = time.monotonic() + (4 if keep_alive else 0.8)
    while time.monotonic() < startup_deadline:
        if process.poll() is not None:
            break
        if not keep_alive or ready_path.exists():
            break
        time.sleep(0.1)
    if process.poll() is not None or (keep_alive and not ready_path.exists()):
        if process.poll() is None:
            terminate_process(process)
        log_text = log_path.read_text(errors="replace") if log_path.exists() else ""
        raise AndroidUseError(f"scrcpy failed to start.\n{log_text[-2000:]}")
    SCRCPY_PROCESSES[process.pid] = process
    ready_state = read_json_file(ready_path) if keep_alive else {}

    lock_pid = None
    lock_error = None
    lock_log_path = SCREEN_DIR / "scrcpy-window-lock.log"
    should_lock_size = bool(args.get("lock_window_size", True)) and window_width is not None and window_height is not None
    if should_lock_size and not args.get("borderless", False):
        lock_script = PLUGIN_ROOT / "scripts" / "scrcpy_window_lock.py"
        lock_continuous = bool(args.get("lock_window_continuous", False))
        lock_command = [
            sys.executable,
            str(lock_script),
            "--process-name",
            "scrcpy",
            "--window-title",
            window_title,
            "--width",
            str(int(window_width)),
            "--height",
            str(int(window_height)),
            "--max-successes",
            "0" if lock_continuous else "1",
        ]
        lock_log_handle = lock_log_path.open("ab")
        try:
            lock_process = subprocess.Popen(
                lock_command,
                stdout=lock_log_handle,
                stderr=lock_log_handle,
                env=tool_env(),
                start_new_session=True,
            )
            lock_log_handle.close()
            time.sleep(0.4)
            if lock_process.poll() is None:
                SCRCPY_LOCK_PROCESSES[lock_process.pid] = lock_process
                lock_pid = lock_process.pid
            else:
                lock_output = lock_log_path.read_text(errors="replace")[-2000:] if lock_log_path.exists() else ""
                if "max-successes-reached" not in lock_output:
                    lock_error = lock_output or "lock process exited"
        except Exception as exc:
            lock_log_handle.close()
            lock_error = str(exc)

    return [
        text_content(
            {
                "ok": True,
                "serial": serial,
                "pid": process.pid,
                "scrcpy_pid": ready_state.get("pid") if keep_alive else process.pid,
                "keep_alive": keep_alive,
                "window_title": window_title,
                "command": command,
                "launch_command": launch_command,
                "log_path": str(log_path),
                "user_closed_path": str(user_closed_path) if keep_alive else None,
                "lock_pid": lock_pid,
                "lock_log_path": str(lock_log_path) if should_lock_size else None,
                "lock_error": lock_error,
                "display": "scrcpy is running as a detached desktop window.",
            }
        )
    ]


AUTO_SCRCPY_TOOL_NAMES = {
    "android_get_state",
    "android_screenshot",
    "android_show_screen",
    "android_observe",
    "android_tap_text",
    "android_tap",
    "android_swipe",
    "android_type_text",
    "android_press_key",
    "android_wake_unlock",
    "android_open_url",
    "android_open_app",
    "android_webview_pages",
    "android_webview_eval",
    "android_start_recording",
    "android_record_checkpoint",
    "android_stop_recording",
    "android_replay_recipe",
}


def should_auto_show_scrcpy_for_tool(name: str, arguments: dict[str, Any]) -> bool:
    if not env_flag("ANDROID_USE_SCRCPY_ON_TOOL_CALL", True):
        return False
    if arguments.get("show_scrcpy") is False:
        return False
    if name.startswith("xiaoluxue_"):
        return True
    return name in AUTO_SCRCPY_TOOL_NAMES


def maybe_show_scrcpy_for_tool_call(name: str, arguments: dict[str, Any]) -> None:
    if not should_auto_show_scrcpy_for_tool(name, arguments):
        return
    try:
        serial = choose_serial(arguments.get("serial"))
        result = ensure_default_scrcpy_window(
            serial,
            {
                **arguments,
                "show_scrcpy": True,
                "respect_manual_close": False,
                "scrcpy_keep_alive": arguments.get("scrcpy_keep_alive", True),
                "scrcpy_fixed_window": arguments.get("scrcpy_fixed_window", True),
                "scrcpy_lock_window_size": arguments.get("scrcpy_lock_window_size", True),
                "scrcpy_max_size": arguments.get("scrcpy_max_size", 1280),
            },
        )
        update_resident_scrcpy_status(last_on_demand_result={"tool": name, "serial": serial, **result})
    except Exception as exc:
        update_resident_scrcpy_status(last_on_demand_error={"tool": name, "error": str(exc), "at": time.time()})


def tool_stop_scrcpy(args: dict[str, Any]) -> list[dict[str, Any]]:
    stopped: list[int] = []
    requested_pid = args.get("pid")
    if requested_pid:
        pids = [int(requested_pid)]
    elif args.get("all", True):
        pids = list(SCRCPY_PROCESSES)
    else:
        pids = []

    for pid in pids:
        process = SCRCPY_PROCESSES.pop(pid, None)
        if process and terminate_process(process):
            stopped.append(pid)
    lock_stopped: list[int] = []
    for pid, process in list(SCRCPY_LOCK_PROCESSES.items()):
        if terminate_process(process):
            lock_stopped.append(pid)
        SCRCPY_LOCK_PROCESSES.pop(pid, None)
    return [text_content({"ok": True, "stopped_pids": stopped, "stopped_lock_pids": lock_stopped})]


def pick_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def tool_start_screen_viewer(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    host = str(args.get("host", "127.0.0.1"))
    port = int(args.get("port") or pick_free_port(host))
    interval_ms = max(250, min(int(args.get("interval_ms", 1000)), 10000))
    viewer_script = PLUGIN_ROOT / "scripts" / "android_screen_viewer.py"
    if not viewer_script.exists():
        raise AndroidUseError(f"Screen viewer script not found: {viewer_script}")

    command = [
        sys.executable,
        str(viewer_script),
        "--serial",
        serial,
        "--host",
        host,
        "--port",
        str(port),
        "--interval-ms",
        str(interval_ms),
        "--adb",
        adb_binary(),
    ]
    log_path = SCREEN_DIR / "viewer.log"
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("ab")
    try:
        process = subprocess.Popen(
            command,
            stdout=log_handle,
            stderr=log_handle,
            env=tool_env(),
        )
    except FileNotFoundError as exc:
        log_handle.close()
        raise AndroidUseError(f"Command not found: {command[0]}") from exc

    time.sleep(0.4)
    if process.poll() is not None:
        log_handle.close()
        log_text = log_path.read_text(errors="replace") if log_path.exists() else ""
        raise AndroidUseError(f"Android screen viewer failed to start.\n{log_text[-2000:]}")

    SCREEN_VIEWER_PROCESSES[process.pid] = process
    url = f"http://{host}:{port}/"
    return [
        text_content(
            {
                "ok": True,
                "serial": serial,
                "pid": process.pid,
                "url": url,
                "interval_ms": interval_ms,
                "log_path": str(log_path),
            }
        )
    ]


def tool_stop_screen_viewer(args: dict[str, Any]) -> list[dict[str, Any]]:
    stopped: list[int] = []
    requested_pid = args.get("pid")
    if requested_pid:
        pids = [int(requested_pid)]
    elif args.get("all", True):
        pids = list(SCREEN_VIEWER_PROCESSES)
    else:
        pids = []

    for pid in pids:
        process = SCREEN_VIEWER_PROCESSES.pop(pid, None)
        if process and process.poll() is None:
            process.terminate()
            stopped.append(pid)
    return [text_content({"ok": True, "stopped_pids": stopped})]


def webrtc_python_binary() -> str:
    configured = os.environ.get("ANDROID_USE_WEBRTC_PYTHON")
    if configured:
        return configured
    venv_python = PLUGIN_ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def tool_start_webrtc_viewer(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    host = str(args.get("host", "127.0.0.1"))
    port = int(args.get("port") or pick_free_port(host))
    max_size = int(args.get("max_size", 960))
    bit_rate = str(args.get("bit_rate", "4M"))
    max_fps = int(args.get("max_fps", 30))
    viewer_script = PLUGIN_ROOT / "scripts" / "android_webrtc_viewer.py"
    if not viewer_script.exists():
        raise AndroidUseError(f"WebRTC viewer script not found: {viewer_script}")

    command = [
        webrtc_python_binary(),
        str(viewer_script),
        "--serial",
        serial,
        "--host",
        host,
        "--port",
        str(port),
        "--adb",
        adb_binary(),
        "--scrcpy",
        scrcpy_binary(),
        "--max-size",
        str(max_size),
        "--bit-rate",
        bit_rate,
        "--max-fps",
        str(max_fps),
    ]
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    log_path = SCREEN_DIR / "webrtc-viewer.log"
    log_handle = log_path.open("ab")
    try:
        process = subprocess.Popen(
            command,
            stdout=log_handle,
            stderr=log_handle,
            env=tool_env(),
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        log_handle.close()
        raise AndroidUseError(f"Command not found: {command[0]}") from exc
    log_handle.close()
    time.sleep(0.8)
    if process.poll() is not None:
        log_text = log_path.read_text(errors="replace") if log_path.exists() else ""
        raise AndroidUseError(f"Android WebRTC viewer failed to start.\n{log_text[-3000:]}")

    WEBRTC_VIEWER_PROCESSES[process.pid] = process
    url = f"http://{host}:{port}/"
    return [
        text_content(
            {
                "ok": True,
                "serial": serial,
                "pid": process.pid,
                "url": url,
                "log_path": str(log_path),
                "python": command[0],
                "max_size": max_size,
                "bit_rate": bit_rate,
                "max_fps": max_fps,
                "display": "Open the returned URL in Codex to view the Android screen over WebRTC.",
            }
        )
    ]


def tool_stop_webrtc_viewer(args: dict[str, Any]) -> list[dict[str, Any]]:
    stopped: list[int] = []
    requested_pid = args.get("pid")
    if requested_pid:
        pids = [int(requested_pid)]
    elif args.get("all", True):
        pids = list(WEBRTC_VIEWER_PROCESSES)
    else:
        pids = []

    for pid in pids:
        process = WEBRTC_VIEWER_PROCESSES.pop(pid, None)
        if process and process.poll() is None:
            process.terminate()
            stopped.append(pid)
    return [text_content({"ok": True, "stopped_pids": stopped})]


def vlm_endpoint() -> str:
    base_url = os.environ.get("ANDROID_USE_VLM_BASE_URL")
    if not base_url:
        raise AndroidUseError("ANDROID_USE_VLM_BASE_URL is not configured.")
    base_url = base_url.rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def openai_api_key() -> str:
    api_key = os.environ.get("ANDROID_USE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise AndroidUseError("OPENAI_API_KEY or ANDROID_USE_OPENAI_API_KEY is not configured.")
    return api_key


def openai_responses_endpoint() -> str:
    base_url = os.environ.get("ANDROID_USE_OPENAI_BASE_URL", OPENAI_BASE_URL).rstrip("/")
    if base_url.endswith("/responses"):
        return base_url
    return f"{base_url}/responses"


def post_openai_responses(payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        openai_responses_endpoint(),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {openai_api_key()}",
        },
        method="POST",
    )
    timeout = float(os.environ.get("ANDROID_USE_OPENAI_TIMEOUT", "45"))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise AndroidUseError(f"OpenAI Responses request failed: HTTP {exc.code}\n{body}") from exc
    except urllib.error.URLError as exc:
        raise AndroidUseError(f"OpenAI Responses request failed: {exc}") from exc


MOBILE_TARS_PROMPT = """You are a GUI agent controlling an Android device. You are given a task, action history, current device state, UI tree text, and a screenshot.
You need to perform the next action to complete the task.

## Output Format
Thought: ...
Action: ...

## Action Space
click(point='x y')
long_press(point='x y')
type(content='text')
scroll(point='x y', direction='down or up or right or left')
open_app(app_name='name')
drag(start_point='x1 y1', end_point='x2 y2')
press_home()
press_back()
wait()
finished(content='summary')

## Rules
- Output exactly one Thought and one Action.
- Use the screenshot for visual grounding and the UI tree for text grounding.
- Prefer direct click on visible targets.
- Coordinates are screen pixels unless the model is trained to output normalized UI-TARS coordinates.
- If the task is already complete, use finished(content='...').
"""


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            return json.loads(stripped[start : end + 1])
        raise


def clean_model_text(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:\w+)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def parse_call_string(value: str) -> str:
    value = value.strip()
    try:
        return str(ast.literal_eval(value))
    except (SyntaxError, ValueError):
        return value.strip("\"'")


def parse_tars_point(value: str) -> tuple[int, int]:
    numbers = re.findall(r"-?\d+(?:\.\d+)?", value)
    if len(numbers) < 2:
        raise AndroidUseError(f"Could not parse point from {value!r}")
    return round(float(numbers[0])), round(float(numbers[1]))


def infer_coordinate_mode(model: str | None, explicit_mode: str | None = None) -> str:
    if explicit_mode:
        return explicit_mode
    env_mode = os.environ.get("ANDROID_USE_VLM_COORDINATE_MODE")
    if env_mode:
        return env_mode
    model_name = (model or os.environ.get("ANDROID_USE_VLM_MODEL") or "").casefold()
    if any(token in model_name for token in ("ui-tars", "uitars", "seed", "doubao", "tars")):
        return "normalized_1000"
    return "absolute"


def scale_point_for_screen(
    x: int,
    y: int,
    screen: dict[str, int | None],
    coordinate_mode: str,
) -> tuple[int, int]:
    width = int(screen.get("width") or 0)
    height = int(screen.get("height") or 0)
    mode = coordinate_mode.lower()
    if mode in {"normalized_1000", "qwen25vl", "uitars", "ui-tars"} and width > 0 and height > 0:
        return round(x / 1000 * width), round(y / 1000 * height)
    return x, y


def parse_tars_action_response(
    response_text: str,
    screen: dict[str, int | None],
    *,
    coordinate_mode: str = "absolute",
) -> dict[str, Any]:
    text = clean_model_text(response_text)
    try:
        action = extract_json_object(text)
        action["_raw_model_response"] = response_text
        return action
    except json.JSONDecodeError:
        pass

    thought = ""
    thought_match = re.search(r"Thought:\s*(.*?)(?:\n\s*Action:|$)", text, flags=re.S | re.I)
    if thought_match:
        thought = thought_match.group(1).strip()
    action_match = re.search(r"Action:\s*(.*)", text, flags=re.S | re.I)
    if action_match:
        action_line = action_match.group(1).strip().splitlines()[0].strip()
    else:
        first_line = text.strip().splitlines()[0].strip() if text.strip() else ""
        if not re.match(r"^(click|long_press|type|scroll|open_app|drag|press_home|press_back|wait|finished)\(", first_line, flags=re.I):
            raise AndroidUseError(f"VLM response did not include an Action: {response_text}")
        action_line = first_line

    def point_arg(name: str = "point") -> tuple[int, int]:
        match = re.search(rf"{name}\s*=\s*('[^']*'|\"[^\"]*\"|\([^)]+\)|[^,\)]+)", action_line)
        if not match and name == "point":
            match = re.search(r"start_box\s*=\s*('[^']*'|\"[^\"]*\"|\([^)]+\)|[^,\)]+)", action_line)
        if not match:
            raise AndroidUseError(f"Missing {name}=... in action: {action_line}")
        x_raw, y_raw = parse_tars_point(match.group(1))
        return scale_point_for_screen(x_raw, y_raw, screen, coordinate_mode)

    def string_arg(name: str) -> str:
        match = re.search(rf"{name}\s*=\s*('[^']*'|\"[^\"]*\")", action_line, flags=re.S)
        if not match:
            return ""
        return parse_call_string(match.group(1))

    lower = action_line.lower()
    if lower.startswith("click("):
        x, y = point_arg("point")
        return {"action": "tap", "x": x, "y": y, "thought": thought, "_raw_model_response": response_text}
    if lower.startswith("long_press("):
        x, y = point_arg("point")
        return {
            "action": "long_press",
            "x": x,
            "y": y,
            "duration_ms": 700,
            "thought": thought,
            "_raw_model_response": response_text,
        }
    if lower.startswith("drag("):
        start_x, start_y = point_arg("start_point")
        end_x, end_y = point_arg("end_point")
        return {
            "action": "swipe",
            "start_x": start_x,
            "start_y": start_y,
            "end_x": end_x,
            "end_y": end_y,
            "duration_ms": 350,
            "thought": thought,
            "_raw_model_response": response_text,
        }
    if lower.startswith("scroll("):
        x, y = point_arg("point")
        direction = string_arg("direction").lower() or "down"
        width = int(screen.get("width") or 1080)
        height = int(screen.get("height") or 1920)
        distance = max(180, min(width, height) // 4)
        start_x = end_x = x
        start_y = end_y = y
        if direction == "down":
            end_y = max(1, y - distance)
        elif direction == "up":
            end_y = min(height - 1, y + distance)
        elif direction == "left":
            end_x = min(width - 1, x + distance)
        elif direction == "right":
            end_x = max(1, x - distance)
        return {
            "action": "swipe",
            "start_x": start_x,
            "start_y": start_y,
            "end_x": end_x,
            "end_y": end_y,
            "duration_ms": 300,
            "direction": direction,
            "thought": thought,
            "_raw_model_response": response_text,
        }
    if lower.startswith("type("):
        content = string_arg("content")
        enter = content.endswith("\n")
        if enter:
            content = content[:-1]
        return {
            "action": "type_text",
            "text": content,
            "enter": enter,
            "thought": thought,
            "_raw_model_response": response_text,
        }
    if lower.startswith("open_app("):
        app_name = string_arg("app_name")
        return {"action": "open_app_name", "app_name": app_name, "thought": thought, "_raw_model_response": response_text}
    if lower.startswith("press_home("):
        return {"action": "press_key", "key": "HOME", "thought": thought, "_raw_model_response": response_text}
    if lower.startswith("press_back("):
        return {"action": "press_key", "key": "BACK", "thought": thought, "_raw_model_response": response_text}
    if lower.startswith("wait("):
        return {"action": "wait", "seconds": 1, "thought": thought, "_raw_model_response": response_text}
    if lower.startswith("finished("):
        return {"action": "done", "summary": string_arg("content"), "thought": thought, "_raw_model_response": response_text}
    raise AndroidUseError(f"Unsupported VLM action syntax: {action_line}")


def compact_ui_for_prompt(nodes: list[dict[str, Any]], *, limit: int = 80) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for node in nodes:
        labels = node_labels(node)
        if not labels:
            continue
        point = node_click_point(node)
        compact.append(
            {
                "index": node.get("index"),
                "text": node.get("text"),
                "content_desc": node.get("content_desc"),
                "resource_id": node.get("resource_id"),
                "class": node.get("class"),
                "bounds": node.get("bounds"),
                "tap": point,
                "selected": node.get("selected"),
            }
        )
        if len(compact) >= limit:
            break
    return compact


def build_agent_user_text(
    instruction: str,
    state: dict[str, Any],
    screen: dict[str, int | None],
    *,
    history: list[dict[str, Any]] | None = None,
    ui_nodes: list[dict[str, Any]] | None = None,
    coordinate_mode: str | None = None,
) -> str:
    return (
        f"Task: {instruction}\n"
        f"Device state: {json.dumps(state, ensure_ascii=False)}\n"
        f"Action history: {json.dumps(history or [], ensure_ascii=False)}\n"
        f"Visible UI nodes: {json.dumps(compact_ui_for_prompt(ui_nodes or []), ensure_ascii=False)}\n"
        f"Screenshot size: {screen.get('width')}x{screen.get('height')}\n"
        f"Coordinate mode expected by executor: {coordinate_mode or 'absolute'}\n"
        "Return the single best next action."
    )


def extract_openai_response_text(response_payload: dict[str, Any]) -> str:
    output_text = response_payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    chunks: list[str] = []
    for item in response_payload.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content", []) or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"output_text", "text"} and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    return "\n".join(chunks).strip()


def map_openai_computer_action(action: dict[str, Any], screen: dict[str, int | None]) -> dict[str, Any]:
    action_type = str(action.get("type") or action.get("action") or "").lower()
    if action_type in {"click", "double_click"}:
        return {
            "action": "double_tap" if action_type == "double_click" else "tap",
            "x": int(round(float(action["x"]))),
            "y": int(round(float(action["y"]))),
            "button": action.get("button", "left"),
            "source": "openai-computer",
        }
    if action_type in {"drag", "drag_path"}:
        path = action.get("path") or []
        if len(path) >= 2:
            start = path[0]
            end = path[-1]
            return {
                "action": "swipe",
                "start_x": int(round(float(start["x"]))),
                "start_y": int(round(float(start["y"]))),
                "end_x": int(round(float(end["x"]))),
                "end_y": int(round(float(end["y"]))),
                "duration_ms": 350,
                "source": "openai-computer",
            }
    if action_type == "scroll":
        x = int(round(float(action.get("x", (screen.get("width") or 1080) / 2))))
        y = int(round(float(action.get("y", (screen.get("height") or 1920) / 2))))
        scroll_x = float(action.get("scroll_x", action.get("scrollX", 0)) or 0)
        scroll_y = float(action.get("scroll_y", action.get("scrollY", 0)) or 0)
        width = int(screen.get("width") or 1080)
        height = int(screen.get("height") or 1920)
        distance_x = max(120, min(width // 3, int(abs(scroll_x) or 0)))
        distance_y = max(120, min(height // 3, int(abs(scroll_y) or 0)))
        end_x = x
        end_y = y
        if abs(scroll_y) >= abs(scroll_x):
            end_y = y - distance_y if scroll_y > 0 else y + distance_y
            end_y = max(1, min(height - 1, end_y))
        else:
            end_x = x - distance_x if scroll_x > 0 else x + distance_x
            end_x = max(1, min(width - 1, end_x))
        return {
            "action": "swipe",
            "start_x": x,
            "start_y": y,
            "end_x": end_x,
            "end_y": end_y,
            "duration_ms": 300,
            "source": "openai-computer",
        }
    if action_type == "type":
        return {"action": "type_text", "text": str(action.get("text", "")), "source": "openai-computer"}
    if action_type in {"keypress", "key"}:
        keys = action.get("keys") or [action.get("key")]
        mapped_keys = [str(key).upper().replace("ARROW", "DPAD_") for key in keys if key]
        if len(mapped_keys) == 1:
            return {"action": "press_key", "key": mapped_keys[0], "source": "openai-computer"}
        return {
            "action": "batch",
            "actions": [{"action": "press_key", "key": key, "source": "openai-computer"} for key in mapped_keys],
            "source": "openai-computer",
        }
    if action_type in {"wait", "screenshot"}:
        return {"action": "wait", "seconds": 0.5, "source": "openai-computer"}
    raise AndroidUseError(f"Unsupported OpenAI computer action: {action}")


def extract_openai_computer_actions(
    response_payload: dict[str, Any],
    screen: dict[str, int | None],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    mapped: list[dict[str, Any]] = []
    raw_calls: list[dict[str, Any]] = []
    for item in response_payload.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type not in {"computer_call", "computer_use_call"}:
            continue
        raw_calls.append(item)
        actions = item.get("actions")
        if isinstance(actions, list):
            for action in actions:
                if isinstance(action, dict):
                    mapped.append(map_openai_computer_action(action, screen))
        elif isinstance(item.get("action"), dict):
            mapped.append(map_openai_computer_action(item["action"], screen))
    return mapped, raw_calls


def call_openai_vision(
    instruction: str,
    png: bytes,
    state: dict[str, Any],
    model_override: str | None = None,
    *,
    history: list[dict[str, Any]] | None = None,
    ui_nodes: list[dict[str, Any]] | None = None,
    coordinate_mode: str | None = None,
) -> dict[str, Any]:
    model = model_override or os.environ.get("ANDROID_USE_OPENAI_VISION_MODEL") or os.environ.get("ANDROID_USE_OPENAI_MODEL") or "gpt-5.5"
    screen = png_size(png)
    resolved_coordinate_mode = infer_coordinate_mode(model, coordinate_mode)
    payload: dict[str, Any] = {
        "model": model,
        "instructions": MOBILE_TARS_PROMPT,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": build_agent_user_text(
                            instruction,
                            state,
                            screen,
                            history=history,
                            ui_nodes=ui_nodes,
                            coordinate_mode=resolved_coordinate_mode,
                        ),
                    },
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
                        "detail": os.environ.get("ANDROID_USE_OPENAI_IMAGE_DETAIL", "low"),
                    },
                ],
            }
        ],
    }
    reasoning_effort = os.environ.get("ANDROID_USE_OPENAI_REASONING_EFFORT")
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}
    response_payload = post_openai_responses(payload)
    content = extract_openai_response_text(response_payload)
    if not content:
        raise AndroidUseError(f"OpenAI vision response did not include text: {json.dumps(response_payload)[:1000]}")
    return parse_tars_action_response(content, screen, coordinate_mode=resolved_coordinate_mode)


def call_openai_computer(
    instruction: str,
    png: bytes,
    state: dict[str, Any],
    model_override: str | None = None,
    *,
    history: list[dict[str, Any]] | None = None,
    ui_nodes: list[dict[str, Any]] | None = None,
    coordinate_mode: str | None = None,
) -> dict[str, Any]:
    model = model_override or os.environ.get("ANDROID_USE_OPENAI_COMPUTER_MODEL") or os.environ.get("ANDROID_USE_OPENAI_MODEL") or "gpt-5.5"
    screen = png_size(png)
    display_width = int(screen.get("width") or state.get("screen", {}).get("width") or 1080)
    display_height = int(screen.get("height") or state.get("screen", {}).get("height") or 1920)
    user_text = build_agent_user_text(
        instruction,
        state,
        screen,
        history=history,
        ui_nodes=ui_nodes,
        coordinate_mode=coordinate_mode or "absolute",
    )
    use_preview = model == "computer-use-preview" or os.environ.get("ANDROID_USE_OPENAI_COMPUTER_TOOL") == "computer_use_preview"
    if use_preview:
        tools = [
            {
                "type": "computer_use_preview",
                "display_width": display_width,
                "display_height": display_height,
                "environment": os.environ.get("ANDROID_USE_OPENAI_COMPUTER_ENVIRONMENT", "browser"),
            }
        ]
    else:
        tools = [
            {
                "type": "computer",
                "display_width": display_width,
                "display_height": display_height,
                "environment": os.environ.get("ANDROID_USE_OPENAI_COMPUTER_ENVIRONMENT", "browser"),
            }
        ]

    payload: dict[str, Any] = {
        "model": model,
        "tools": tools,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": user_text
                        + "\nYou are controlling an Android device through adb. Request a screenshot if needed, then choose the next concrete computer action.",
                    }
                ],
            }
        ],
        "truncation": "auto",
    }
    response_payload = post_openai_responses(payload)
    mapped, raw_calls = extract_openai_computer_actions(response_payload, screen)

    # GA computer models can first ask for a screenshot. Satisfy one screenshot request internally.
    if not mapped and raw_calls:
        call_id = raw_calls[-1].get("call_id") or raw_calls[-1].get("id")
        if call_id:
            response_payload = post_openai_responses(
                {
                    "model": model,
                    "tools": tools,
                    "previous_response_id": response_payload.get("id"),
                    "input": [
                        {
                            "type": "computer_call_output",
                            "call_id": call_id,
                            "output": {
                                "type": "input_image",
                                "image_url": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
                            },
                        }
                    ],
                    "truncation": "auto",
                }
            )
            mapped, raw_calls = extract_openai_computer_actions(response_payload, screen)

    if mapped:
        if len(mapped) == 1:
            mapped[0]["_raw_model_response"] = json.dumps(response_payload, ensure_ascii=False)
            return mapped[0]
        return {
            "action": "batch",
            "actions": mapped,
            "source": "openai-computer",
            "_raw_model_response": json.dumps(response_payload, ensure_ascii=False),
        }

    content = extract_openai_response_text(response_payload)
    if content:
        return parse_tars_action_response(content, screen, coordinate_mode=coordinate_mode or "absolute")
    raise AndroidUseError(f"OpenAI computer response did not include executable actions: {json.dumps(response_payload)[:1000]}")


def call_vlm(
    instruction: str,
    png: bytes,
    state: dict[str, Any],
    model_override: str | None = None,
    *,
    history: list[dict[str, Any]] | None = None,
    ui_nodes: list[dict[str, Any]] | None = None,
    coordinate_mode: str | None = None,
) -> dict[str, Any]:
    api_key = os.environ.get("ANDROID_USE_VLM_API_KEY")
    model = model_override or os.environ.get("ANDROID_USE_VLM_MODEL")
    if not api_key:
        raise AndroidUseError("ANDROID_USE_VLM_API_KEY is not configured.")
    if not model:
        raise AndroidUseError("ANDROID_USE_VLM_MODEL is not configured.")

    screen = png_size(png)
    resolved_coordinate_mode = infer_coordinate_mode(model, coordinate_mode)
    user_text = (
        f"Task: {instruction}\n"
        f"Device state: {json.dumps(state, ensure_ascii=False)}\n"
        f"Action history: {json.dumps(history or [], ensure_ascii=False)}\n"
        f"Visible UI nodes: {json.dumps(compact_ui_for_prompt(ui_nodes or []), ensure_ascii=False)}\n"
        f"Screenshot size: {screen.get('width')}x{screen.get('height')}\n"
        f"Coordinate mode expected by executor: {resolved_coordinate_mode}\n"
        "Return the single best next action."
    )
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 900,
        "messages": [
            {"role": "system", "content": MOBILE_TARS_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + base64.b64encode(png).decode("ascii")
                        },
                    },
                ],
            },
        ],
    }
    request = urllib.request.Request(
        vlm_endpoint(),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    timeout = float(os.environ.get("ANDROID_USE_VLM_TIMEOUT", "45"))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise AndroidUseError(f"VLM request failed: HTTP {exc.code}\n{body}") from exc
    except urllib.error.URLError as exc:
        raise AndroidUseError(f"VLM request failed: {exc}") from exc

    try:
        content = response_payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AndroidUseError(f"Unexpected VLM response: {json.dumps(response_payload)[:1000]}") from exc
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str):
        raise AndroidUseError(f"Unexpected VLM message content: {content!r}")
    return parse_tars_action_response(content, screen, coordinate_mode=resolved_coordinate_mode)


def resolve_agent_provider(provider: str | None = None) -> str:
    selected = (provider or os.environ.get("ANDROID_USE_AGENT_PROVIDER") or "").strip().lower()
    if selected == "auto":
        selected = ""
    if selected:
        return selected
    if os.environ.get("ANDROID_USE_VLM_BASE_URL"):
        return "openai-compatible"
    if os.environ.get("ANDROID_USE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        return "openai-computer"
    return "openai-compatible"


def call_agent_model(
    instruction: str,
    png: bytes,
    state: dict[str, Any],
    model_override: str | None = None,
    *,
    history: list[dict[str, Any]] | None = None,
    ui_nodes: list[dict[str, Any]] | None = None,
    coordinate_mode: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    resolved_provider = resolve_agent_provider(provider)
    if resolved_provider in {"openai-computer", "openai-cua", "openai-cua-preview", "computer"}:
        return call_openai_computer(
            instruction,
            png,
            state,
            model_override,
            history=history,
            ui_nodes=ui_nodes,
            coordinate_mode=coordinate_mode,
        )
    if resolved_provider in {"openai-vision", "openai-responses", "openai"}:
        return call_openai_vision(
            instruction,
            png,
            state,
            model_override,
            history=history,
            ui_nodes=ui_nodes,
            coordinate_mode=coordinate_mode,
        )
    if resolved_provider in {"openai-compatible", "chat-completions", "vlm", "seed"}:
        return call_vlm(
            instruction,
            png,
            state,
            model_override,
            history=history,
            ui_nodes=ui_nodes,
            coordinate_mode=coordinate_mode,
        )
    raise AndroidUseError(
        "Unsupported Android agent provider. Use openai-computer, openai-vision, or openai-compatible."
    )


def execute_action(serial: str, action: dict[str, Any]) -> list[dict[str, Any]]:
    action_type = str(action.get("action", "")).lower()
    if action_type == "batch":
        content: list[dict[str, Any]] = []
        for child_action in action.get("actions", []):
            if isinstance(child_action, dict):
                content.extend(execute_action(serial, child_action))
        return content or [text_content(action_result("batch", serial, {"actions": 0}))]
    if action_type == "click":
        action_type = "tap"
    if action_type == "tap":
        return tool_tap({"serial": serial, "x": action["x"], "y": action["y"]})
    if action_type == "double_tap":
        x = int(action["x"])
        y = int(action["y"])
        adb(["shell", "input", "tap", str(x), str(y)], serial=serial, timeout=10)
        time.sleep(0.08)
        adb(["shell", "input", "tap", str(x), str(y)], serial=serial, timeout=10)
        return [text_content(action_result("double_tap", serial, {"x": x, "y": y}))]
    if action_type == "long_press":
        duration_ms = int(action.get("duration_ms", 700))
        x = int(action["x"])
        y = int(action["y"])
        adb(
            ["shell", "input", "swipe", str(x), str(y), str(x), str(y), str(duration_ms)],
            serial=serial,
            timeout=10,
        )
        return [text_content(action_result("long_press", serial, {"x": x, "y": y, "duration_ms": duration_ms}))]
    if action_type == "swipe":
        return tool_swipe(
            {
                "serial": serial,
                "start_x": action["start_x"],
                "start_y": action["start_y"],
                "end_x": action["end_x"],
                "end_y": action["end_y"],
                "duration_ms": action.get("duration_ms", 300),
            }
        )
    if action_type == "type_text":
        return tool_type_text({"serial": serial, "text": action.get("text", ""), "enter": bool(action.get("enter"))})
    if action_type == "press_key":
        return tool_press_key({"serial": serial, "key": action["key"]})
    if action_type == "open_url":
        return tool_open_url({"serial": serial, "url": action["url"]})
    if action_type == "open_app":
        return tool_open_app({"serial": serial, "package": action["package"], "activity": action.get("activity", "")})
    if action_type == "open_app_name":
        app_name = str(action.get("app_name", "")).strip()
        if "." in app_name and " " not in app_name:
            return tool_open_app({"serial": serial, "package": app_name})
        raise AndroidUseError(
            f"open_app(app_name={app_name!r}) needs a package name on Android. "
            "Ask the model to click the launcher icon if it is visible, or use android_open_app with a package."
        )
    if action_type == "xiaoluxue_map_fast_path":
        result = run_xiaoluxue_map_fast_path(serial, action, record=True)
        return [text_content({"ok": True, "serial": serial, **result})]
    if action_type == "xiaoluxue_lesson_fast_path":
        result = run_xiaoluxue_lesson_fast_path(serial, action, record=True)
        return [text_content({"ok": True, "serial": serial, **result})]
    if action_type == "xiaoluxue_open_native_subject":
        result = run_xiaoluxue_open_native_subject(serial, action, record=True)
        return [text_content({"ok": True, "serial": serial, **result})]
    if action_type == "xiaoluxue_login_fast_path":
        result = run_xiaoluxue_login_fast_path(serial, action, record=True)
        return [text_content({"ok": True, "serial": serial, **result})]
    if action_type == "wait":
        seconds = min(float(action.get("seconds", 1)), 10)
        time.sleep(seconds)
        return [text_content(action_result("wait", serial, {"seconds": seconds}))]
    if action_type == "done":
        return [text_content({"ok": True, "serial": serial, "action": "done", "summary": action.get("summary")})]
    raise AndroidUseError(f"Unsupported VLM action: {action_type}")


def tool_agent_step(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    instruction = str(args["instruction"])
    execute = bool(args.get("execute", False))
    mode = str(args.get("mode", "hybrid")).lower()
    history = args.get("history") if isinstance(args.get("history"), list) else []
    scrcpy_result = ensure_default_scrcpy_window(serial, args) if execute else {"ok": True, "skipped": "not-executing"}

    if mode in {"hybrid", "uiautomator", "accessibility"}:
        fast_action = fast_ui_action_from_instruction(serial, instruction)
        if fast_action:
            content = [
                text_content(
                    {
                        "serial": serial,
                        "proposed_action": fast_action,
                        "execute": execute,
                        "mode": mode,
                        "scrcpy": scrcpy_result,
                    }
                )
            ]
            if execute:
                content.extend(execute_action(serial, fast_action))
            return content
        if mode in {"uiautomator", "accessibility"}:
            raise AndroidUseError(f"Could not satisfy instruction from Android UI tree alone: {instruction}")

    observation = observe_ui(serial, limit=220)
    state = observation["state"]
    ui_nodes = observation["ui"]["nodes"]
    png = screenshot_png(serial)
    action = call_agent_model(
        instruction,
        png,
        state,
        args.get("model"),
        history=history,
        ui_nodes=ui_nodes,
        coordinate_mode=args.get("coordinate_mode"),
        provider=args.get("provider"),
    )
    action_for_display = {key: value for key, value in action.items() if key != "_raw_model_response"}
    content = [
        text_content(
            {
                "serial": serial,
                "proposed_action": action_for_display,
                "execute": execute,
                "mode": mode,
                "scrcpy": scrcpy_result,
            }
        )
    ]
    if execute:
        content.extend(execute_action(serial, action))
    return content


def tool_agent_run(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    instruction = str(args["instruction"])
    max_steps = max(1, min(int(args.get("max_steps", 5)), 20))
    dry_run = bool(args.get("dry_run", False))
    delay_sec = min(float(args.get("delay_sec", 0.25)), 5)
    mode = str(args.get("mode", "hybrid")).lower()
    history: list[dict[str, Any]] = []
    scrcpy_result = ensure_default_scrcpy_window(serial, args) if not dry_run else {"ok": True, "skipped": "dry-run"}

    for step_index in range(max_steps):
        action: dict[str, Any] | None = None
        source = "vlm"
        if mode in {"hybrid", "uiautomator", "accessibility"}:
            action = fast_ui_action_from_instruction(serial, instruction)
            if action:
                source = "uiautomator"
            elif mode in {"uiautomator", "accessibility"}:
                raise AndroidUseError(f"Could not satisfy instruction from Android UI tree alone: {instruction}")
        if not action:
            observation = observe_ui(serial, limit=220)
            state = observation["state"]
            ui_nodes = observation["ui"]["nodes"]
            png = screenshot_png(serial)
            action = call_agent_model(
                instruction,
                png,
                state,
                args.get("model"),
                history=history,
                ui_nodes=ui_nodes,
                coordinate_mode=args.get("coordinate_mode"),
                provider=args.get("provider"),
            )
        action_for_history = {key: value for key, value in action.items() if key != "_raw_model_response"}
        history.append({"step": step_index + 1, "source": source, "action": action_for_history})
        if dry_run:
            break
        if str(action.get("action", "")).lower() == "done":
            break
        execute_action(serial, action)
        if source == "uiautomator":
            break
        time.sleep(delay_sec)

    return [text_content({"serial": serial, "dry_run": dry_run, "scrcpy": scrcpy_result, "steps": history})]


TOOLS: dict[str, dict[str, Any]] = {
    "android_check_dependencies": {
        "description": "Check local adb, scrcpy, and optional VLM environment configuration.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": check_dependencies,
    },
    "android_list_devices": {
        "description": "List Android devices known to adb, optionally with model and screen details.",
        "inputSchema": {
            "type": "object",
            "properties": {"include_details": {"type": "boolean", "default": True}},
            "additionalProperties": False,
        },
        "handler": tool_list_devices,
    },
    "android_wireless_pair": {
        "description": "Pair an Android 11+ device over Wireless debugging once, connect it, save it to the multi-device wireless list, and optionally open scrcpy.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Device IP address, for example 172.27.31.51."},
                "pair_port": {"type": "integer", "description": "Pairing port shown beside the Wireless debugging pairing code."},
                "code": {"type": "string", "description": "Temporary pairing code shown on the Android device."},
                "connect_port": {"type": "integer", "description": "Optional Wireless debugging connection port. If omitted, adb mdns services is used."},
                "save": {"type": "boolean", "default": True},
                "start_scrcpy": {"type": "boolean", "default": True},
            },
            "required": ["host", "pair_port", "code"],
            "additionalProperties": False,
        },
        "handler": tool_wireless_pair,
    },
    "android_wireless_reconnect": {
        "description": "Reconnect to saved Wireless debugging devices without USB, refreshing dynamic mDNS ports when needed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Optional device IP address. Defaults to saved ANDROID_USE_WIRELESS_HOST."},
                "port": {"type": "integer", "description": "Optional connect port. Defaults to saved port, then mDNS."},
                "serial": {"type": "string", "description": "Optional adb serial such as 172.27.31.51:5555."},
                "save": {"type": "boolean", "default": True},
                "start_scrcpy": {"type": "boolean", "default": True},
                "all": {
                    "type": "boolean",
                    "default": False,
                    "description": "Reconnect every saved entry from ANDROID_USE_WIRELESS_DEVICES and optionally start one scrcpy window per device.",
                },
            },
            "additionalProperties": False,
        },
        "handler": tool_wireless_reconnect,
    },
    "android_get_state": {
        "description": "Get state for an attached Android device, optionally including a screenshot.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "include_screenshot": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "handler": tool_get_state,
    },
    "android_screenshot": {
        "description": "Capture a PNG screenshot from the Android device.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "save_path": {"type": "string", "description": "Optional local path for the PNG."},
            },
            "additionalProperties": False,
        },
        "handler": tool_screenshot,
    },
    "android_show_screen": {
        "description": "Capture and return the current Android screen image so Codex can display it inline.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "handler": tool_show_screen,
    },
    "android_observe": {
        "description": "Observe the Android screen using UIAutomator, returning device state and visible UI nodes; optionally include screenshot/XML.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "include_screenshot": {"type": "boolean", "default": False},
                "include_xml": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 160},
            },
            "additionalProperties": False,
        },
        "handler": tool_observe,
    },
    "android_tap_text": {
        "description": "Tap a visible Android UI node by text or content description using UIAutomator, avoiding screenshot/VLM latency.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "text": {"type": "string"},
                "exact": {"type": "boolean", "default": True},
                "include_resource_id": {"type": "boolean", "default": False},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        "handler": tool_tap_text,
    },
    "android_tap": {
        "description": "Tap absolute screen coordinates on the Android device.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
            },
            "required": ["x", "y"],
            "additionalProperties": False,
        },
        "handler": tool_tap,
    },
    "android_swipe": {
        "description": "Swipe between absolute screen coordinates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "start_x": {"type": "integer"},
                "start_y": {"type": "integer"},
                "end_x": {"type": "integer"},
                "end_y": {"type": "integer"},
                "duration_ms": {"type": "integer", "default": 300},
            },
            "required": ["start_x", "start_y", "end_x", "end_y"],
            "additionalProperties": False,
        },
        "handler": tool_swipe,
    },
    "android_type_text": {
        "description": "Type text into the focused field using the fastest available path: direct WebView DOM assignment for debuggable WebView inputs, ADB Keyboard IME for Unicode/long text, or batched adb shell input for short ASCII.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "text": {"type": "string"},
                "clear_first": {"type": "boolean", "default": False},
                "clear_count": {"type": "integer", "default": 80},
                "enter": {"type": "boolean", "default": False},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        "handler": tool_type_text,
    },
    "android_press_key": {
        "description": "Press an Android key by alias, KEYCODE name, or numeric keycode.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "key": {"type": ["string", "integer"]},
            },
            "required": ["key"],
            "additionalProperties": False,
        },
        "handler": tool_press_key,
    },
    "android_wake_unlock": {
        "description": "Wake the Android device and optionally dismiss the keyguard.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "dismiss_keyguard": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
        "handler": tool_wake_unlock,
    },
    "android_open_url": {
        "description": "Open a URL on the selected Android device through an ACTION_VIEW intent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "url": {"type": "string"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "handler": tool_open_url,
    },
    "android_open_app": {
        "description": "Launch an Android app by package name, or a specific activity by component.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "package": {"type": "string"},
                "activity": {
                    "type": "string",
                    "description": "Optional activity class or package/activity component.",
                },
            },
            "required": ["package"],
            "additionalProperties": False,
        },
        "handler": tool_open_app,
    },
    "android_shell": {
        "description": "Run an adb shell command on the selected Android device.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "command": {"type": "string"},
                "timeout_sec": {"type": "number", "default": 20},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        "handler": tool_shell,
    },
    "android_webview_pages": {
        "description": "List debuggable Android WebView DevTools targets by forwarding webview_devtools_remote sockets.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "port": {"type": "integer", "description": "Optional local port when exactly one WebView socket is present."},
            },
            "additionalProperties": False,
        },
        "handler": tool_webview_pages,
    },
    "android_webview_eval": {
        "description": "Evaluate JavaScript in a debuggable Android WebView through Chrome DevTools Protocol.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "page_id": {"type": "string"},
                "url_contains": {"type": "string"},
                "title_contains": {"type": "string"},
                "expression": {"type": "string"},
                "await_promise": {"type": "boolean", "default": True},
                "return_by_value": {"type": "boolean", "default": True},
                "timeout_sec": {"type": "number", "default": 10},
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
        "handler": tool_webview_eval,
    },
    "xiaoluxue_open_app_url": {
        "description": "Open a Xiaoluxue H5 URL inside the Xiaoluxue student app through the vessel WebView route, never through a browser.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "url": {"type": "string", "description": "Xiaoluxue app-only URL, such as stu.xiaoluxue.com/course or *.xiaoluxue.cn."},
                "wait_for_webview": {"type": "boolean", "default": True},
                "inject_bridge": {"type": "boolean", "default": True},
                "reveal_overlay": {
                    "type": "boolean",
                    "default": False,
                    "description": "Dispatch a safe center-screen tap in the WebView after bridge install to reveal hidden course/player overlays.",
                },
                "force_stop": {"type": "boolean", "default": False},
                "timeout_sec": {"type": "number", "default": 5},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "handler": tool_xiaoluxue_open_app_url,
    },
    "xiaoluxue_runtime_status": {
        "description": "Attach to the current Xiaoluxue WebView, validate the cached runtime target, optionally install the Xiaoluxue JS bridge, and return a fast DOM/runtime snapshot.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "page": {"type": "string", "default": "any", "enum": ["any", "course", "exercise"]},
                "open_app_if_needed": {"type": "boolean", "default": True},
                "inject_bridge": {"type": "boolean", "default": True},
                "reveal_overlay": {
                    "type": "boolean",
                    "default": False,
                    "description": "Dispatch a safe center-screen tap in the WebView before snapshotting to reveal hidden course/player overlays.",
                },
                "timeout_sec": {"type": "number", "default": 4},
            },
            "additionalProperties": False,
        },
        "handler": tool_xiaoluxue_runtime_status,
    },
    "xiaoluxue_login_fast_path": {
        "description": "Run the Xiaoluxue native login fast path: fill account and password, ensure the agreement checkbox is checked, submit, and wait for the home Activity.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "account": {"type": "string"},
                "password": {"type": "string"},
                "open_app_if_needed": {
                    "type": "boolean",
                    "default": True,
                    "description": "Launch Xiaoluxue first when the login page is not currently focused.",
                },
                "after_open_wait_sec": {"type": "number", "default": 0.25},
                "timeout_sec": {"type": "number", "default": 5.0},
            },
            "required": ["account", "password"],
            "additionalProperties": False,
        },
        "handler": tool_xiaoluxue_login_fast_path,
    },
    "xiaoluxue_course_snapshot": {
        "description": "Read a fast DOM snapshot from the Xiaoluxue course WebView, including widgets, visible part, buttons, videos, and URL params.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "timeout_sec": {"type": "number", "default": 10},
            },
            "additionalProperties": False,
        },
        "handler": tool_xiaoluxue_course_snapshot,
    },
    "xiaoluxue_set_speed": {
        "description": "Set the current Xiaoluxue guide playback speed through WebView DOM controls, defaulting to 2x.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "rate": {"type": "number", "default": 2.0},
                "timeout_sec": {"type": "number", "default": 10},
            },
            "additionalProperties": False,
        },
        "handler": tool_xiaoluxue_set_speed,
    },
    "xiaoluxue_goto_widget": {
        "description": "Jump to a Xiaoluxue course widget by index/name or the last widget using the WebView fast path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "index": {"type": "integer"},
                "name_contains": {"type": "string"},
                "last": {"type": "boolean", "default": False},
                "mode": {
                    "type": "string",
                    "default": "reload",
                    "enum": ["reload", "scroll"],
                    "description": "reload applies redirectWidgetIndex so far widgets load; scroll only moves the current DOM container.",
                },
                "timeout_sec": {"type": "number", "default": 10},
            },
            "additionalProperties": False,
        },
        "handler": tool_xiaoluxue_goto_widget,
    },
    "xiaoluxue_course_fast_path": {
        "description": "Run the Xiaoluxue course fast path: open a guide widget if needed, set playback speed, then jump to the target widget.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "guide_index": {"type": "integer"},
                "guide_name_contains": {
                    "type": "string",
                    "description": "Guide widget name to open before setting speed. Defaults to the first widget containing 知识讲解/讲解 when a guide player is not already visible.",
                },
                "guide_mode": {
                    "type": "string",
                    "default": "reload",
                    "enum": ["reload", "scroll"],
                },
                "set_speed": {"type": "boolean", "default": True},
                "rate": {"type": "number", "default": 2.0},
                "target_index": {"type": "integer"},
                "target_name_contains": {"type": "string"},
                "target_last": {"type": "boolean", "default": True},
                "target_mode": {
                    "type": "string",
                    "default": "reload",
                    "enum": ["reload", "scroll"],
                },
                "after_navigation_wait_sec": {"type": "number", "default": 2.0},
                "timeout_sec": {"type": "number", "default": 15},
            },
            "additionalProperties": False,
        },
        "handler": tool_xiaoluxue_course_fast_path,
    },
    "xiaoluxue_open_knowledge_guide": {
        "description": "Open a Xiaoluxue subject knowledge guide directly through WebView APIs, defaulting to 首页 > 数学 > 1.1.11/1.1.1.1 知识讲解, then set playback speed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "subject_id": {
                    "type": "integer",
                    "default": 2,
                    "description": "Xiaoluxue subject id. Defaults to 2 (数学).",
                },
                "knowledge_index": {
                    "type": "string",
                    "default": "1.1.11",
                    "description": "Human course index. Dots are normalized, so 1.1.11 also matches 1.1.1.1.",
                },
                "knowledge_id": {
                    "type": "integer",
                    "description": "Optional exact knowledge id. When omitted, known shortcuts and subject-card resolution are used.",
                },
                "guide_widget_index": {
                    "type": "integer",
                    "description": "Optional course widget index to open. Defaults to the first non-intro guide widget.",
                },
                "rate": {"type": "number", "default": 2.0},
                "prefer_client_route": {
                    "type": "boolean",
                    "default": True,
                    "description": "Use same-page H5 routing before falling back to a full WebView reload.",
                },
                "use_shortcut_url": {
                    "type": "boolean",
                    "default": True,
                    "description": "Use the built-in Xiaoluxue fast URL cache for known paths such as 数学 1.1.11.",
                },
                "refresh_session": {
                    "type": "boolean",
                    "default": False,
                    "description": "Refetch study_session/enter before opening, slower but useful if a shortcut URL has gone stale.",
                },
                "respect_current_h5_host": {
                    "type": "boolean",
                    "default": True,
                    "description": "When the current Xiaoluxue WebView is on a test/staging H5 host, rebase known shortcut URLs onto that host instead of opening the production host.",
                },
                "open_app_if_needed": {
                    "type": "boolean",
                    "default": True,
                    "description": "Launch Xiaoluxue if no matching WebView target is currently available.",
                },
                "native_entry_if_needed": {
                    "type": "boolean",
                    "default": True,
                    "description": "When no WebView is present, use the Xiaoluxue native 首页 > 数学 > 知识讲解 coordinate shortcut before CDP routing.",
                },
                "prefer_native_entry_first": {
                    "type": "boolean",
                    "default": False,
                    "description": "Try native map taps before direct vessel routing. Defaults to false for known shortcut URLs to keep common guide entry under 5s.",
                },
                "fallback_native_after_vessel": {
                    "type": "boolean",
                    "default": False,
                    "description": "When direct vessel routing fails, fall back to native map taps. Disabled by default to avoid slow double attempts.",
                },
                "vessel_entry_timeout_sec": {
                    "type": "number",
                    "default": 4.8,
                    "description": "Maximum time to wait for the direct Xiaoluxue vessel WebView route.",
                },
                "turbo_preview": {
                    "type": "boolean",
                    "default": True,
                    "description": "Show a temporary fast guide video bridge while the native H5 guide player finishes loading.",
                },
                "optimize_neighbor_guides": {
                    "type": "boolean",
                    "default": False,
                    "description": "Experimental extreme mode: pause non-target guide JSON fetches so the target widget can render first.",
                },
                "video_verify_sec": {
                    "type": "number",
                    "default": 0.9,
                    "description": "Short verification window for a video node after installing the 2x rate hook.",
                },
                "final_verify": {
                    "type": "boolean",
                    "default": False,
                    "description": "Run an extra final DOM snapshot after speed setup. Disabled by default to keep the shortcut under 5s.",
                },
                "preinject_vessel_bootstrap": {
                    "type": "boolean",
                    "default": False,
                    "description": "Experimental: inject new-document hooks before opening the vessel WebView. Off by default because it can slow adb/CDP startup.",
                },
                "timeout_sec": {"type": "number", "default": 8},
            },
            "additionalProperties": False,
        },
        "handler": tool_xiaoluxue_open_knowledge_guide,
    },
    "xiaoluxue_map_snapshot": {
        "description": "Read the current Xiaoluxue native study-map state from UIAutomator without screenshots: subject, chapter, selected/visible indexes, and visible native actions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "limit": {"type": "integer", "default": 800},
            },
            "additionalProperties": False,
        },
        "handler": tool_xiaoluxue_map_snapshot,
    },
    "xiaoluxue_open_native_subject": {
        "description": "Open Xiaoluxue native study subject map through the app-only SchemeProxyActivity route, e.g. subject_id=1 for 语文, without using a browser or WebRTC.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "subject_id": {"type": "integer", "description": "Native subject id, e.g. 1=语文, 2=数学, 3=英语."},
                "subject": {"type": "string", "description": "Subject name alias such as 语文, 数学, 英语."},
                "textbook_id": {"type": "integer"},
                "chapter_id": {"type": "integer"},
                "knowledge_id": {"type": "integer"},
                "go_next_knowledge": {"type": "boolean"},
                "route_wait_sec": {"type": "number", "default": 0.45},
                "leave_lesson_before_route": {
                    "type": "boolean",
                    "default": True,
                    "description": "When LessonActivity is currently in front, press Back before opening the native subject map route so it is not left above the map.",
                },
                "lesson_back_wait_sec": {"type": "number", "default": 0.35},
                "close_progress_popup": {
                    "type": "boolean",
                    "default": True,
                    "description": "Tap the known progress popup close point after routing to the native subject map.",
                },
                "close_progress_wait_sec": {"type": "number", "default": 0.05},
                "close_progress_taps": {"type": "integer", "default": 1},
                "verify_focus": {"type": "boolean", "default": False},
                "focus_timeout_sec": {"type": "number", "default": 1.5},
            },
            "additionalProperties": False,
        },
        "handler": tool_xiaoluxue_open_native_subject,
    },
    "xiaoluxue_map_fast_path": {
        "description": "Run a Xiaoluxue native study-map action quickly. With subject_id/subject it first opens the native map via SchemeProxyActivity, then can use route presets such as 语文 1.5 题型突破 or the selected-node shortcuts for 题型突破/专属精练.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "instruction": {"type": "string", "description": "Natural-language map instruction, e.g. 进入 1.5 题型突破 or 进入数学 专属精练."},
                "index": {"type": "string", "description": "Visible map index such as 1.5."},
                "subject_id": {"type": "integer", "description": "Native subject id, e.g. 1=语文, 2=数学, 3=英语."},
                "subject": {"type": "string", "description": "Subject name alias such as 语文, 数学, 英语."},
                "route_if_subject": {
                    "type": "boolean",
                    "default": False,
                    "description": "When subject_id/subject is present, open the native subject map route before tapping.",
                },
                "route_wait_sec": {"type": "number", "default": 0.45},
                "leave_lesson_before_route": {
                    "type": "boolean",
                    "default": True,
                    "description": "When LessonActivity is currently in front, press Back before opening the native subject map route so it is not left above the map.",
                },
                "lesson_back_wait_sec": {"type": "number", "default": 0.35},
                "close_progress_popup": {
                    "type": "boolean",
                    "default": True,
                    "description": "When routing first, close the known progress popup before tapping map controls.",
                },
                "close_progress_wait_sec": {"type": "number", "default": 0.05},
                "close_progress_taps": {"type": "integer", "default": 1},
                "action_name": {
                    "type": "string",
                    "default": "select",
                    "enum": [
                        "select",
                        "practise",
                        "expand",
                        "wrong",
                        "notebook",
                        "report",
                        "tasks",
                        "weak",
                        "chapter_picker",
                        "done",
                        "back",
                    ],
                },
                "prefer_predicted": {
                    "type": "boolean",
                    "default": True,
                    "description": "After selecting a visible node, tap the expected expanded control position directly instead of doing a second UI dump.",
                },
                "selected_module_shortcut": {
                    "type": "boolean",
                    "default": True,
                    "description": "When no index is provided and the native map has a selected module visible, tap the selected-node 题型突破/专属精练 controls directly.",
                },
                "enter_module": {
                    "type": "boolean",
                    "default": True,
                    "description": "For 题型突破/专属精练, tap the module card entry button after opening the control.",
                },
                "module_card_wait_sec": {"type": "number", "default": 0.16},
                "confirm_expand_enter": {
                    "type": "boolean",
                    "default": True,
                    "description": "For 专属精练, tap the secondary 依然进入 confirmation when the map activity is still focused.",
                },
                "confirm_wait_sec": {"type": "number", "default": 0.08},
                "confirm_expand_focus_check": {"type": "boolean", "default": False},
                "use_cache": {
                    "type": "boolean",
                    "default": True,
                    "description": "Reuse the last native map layout cache so repeated map actions can complete without UIAutomator dump latency.",
                },
                "force_observe": {"type": "boolean", "default": False},
                "cache_max_age_sec": {"type": "number", "default": 21600},
                "after_select_wait_sec": {"type": "number", "default": 0.08},
                "open_report_when_done": {"type": "boolean", "default": False},
                "report_wait_sec": {"type": "number", "default": 0.32},
                "enter_direct_practice": {
                    "type": "boolean",
                    "default": False,
                    "description": "After opening 题型突破, tap the first card's 直接练 button without a UI dump.",
                },
                "direct_practice_wait_sec": {"type": "number", "default": 0.12},
                "lesson_ready_timeout_sec": {
                    "type": "number",
                    "default": 5.5,
                    "description": "For direct practice entry, wait until LessonActivity content is no longer a plain loading screen using raw screenshot sampling.",
                },
                "lesson_ready_poll_sec": {"type": "number", "default": 0.15},
                "require_lesson_ready": {"type": "boolean", "default": True},
                "after_direct_practice_wait_sec": {"type": "number", "default": 0.08},
                "answer_ready_timeout_sec": {
                    "type": "number",
                    "default": 5.0,
                    "description": "After tapping 直接练, poll a raw screenshot until the native answer page is visible instead of sleeping for fixed transition animations.",
                },
                "answer_ready_poll_sec": {"type": "number", "default": 0.12},
                "tap_direct_practice_until_answer_ready": {
                    "type": "boolean",
                    "default": True,
                    "description": "While LessonActivity is loading, alternate taps on the known 直接练 and transition-start positions until the answer page is visible.",
                },
                "direct_practice_tap_interval_sec": {"type": "number", "default": 0.12},
                "answer_ready_poll_after_taps": {"type": "integer", "default": 3},
                "disable_system_animations": {
                    "type": "boolean",
                    "default": True,
                    "description": "Temporarily set Android animation scales to 0 while opening the native answer page.",
                },
                "restore_system_animations": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
        "handler": tool_xiaoluxue_map_fast_path,
    },
    "xiaoluxue_lesson_fast_path": {
        "description": "Run a Xiaoluxue native LessonActivity action quickly, such as tapping the first 题型突破 card's 直接练 button.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "instruction": {"type": "string", "description": "Natural-language lesson instruction, e.g. 进入直接练."},
                "action_name": {
                    "type": "string",
                    "default": "direct_practice",
                    "enum": ["direct_practice", "continue_answer", "finish_result"],
                },
                "direct_practice_wait_sec": {
                    "type": "number",
                    "default": 0.0,
                    "description": "Wait before tapping. Use 0 from an already-rendered card page; map fast path uses a short wait after entering the module.",
                },
                "lesson_focus_timeout_sec": {"type": "number", "default": 0.7},
                "lesson_ready_timeout_sec": {
                    "type": "number",
                    "default": 0.0,
                    "description": "Optional raw-screenshot readiness wait before tapping, useful if LessonActivity just opened and is still loading.",
                },
                "lesson_ready_poll_sec": {"type": "number", "default": 0.08},
                "require_lesson_ready": {"type": "boolean", "default": False},
                "assume_lesson_activity": {
                    "type": "boolean",
                    "default": True,
                    "description": "Use the known 2000x1200 Xiaoluxue LessonActivity coordinate space and skip dumpsys focus probing for the fastest direct-practice tap.",
                },
                "after_direct_practice_wait_sec": {"type": "number", "default": 0.08},
                "answer_ready_timeout_sec": {
                    "type": "number",
                    "default": 5.0,
                    "description": "After opening or continuing, poll a raw screenshot until the native answer page is visible.",
                },
                "answer_ready_poll_sec": {"type": "number", "default": 0.12},
                "tap_direct_practice_until_answer_ready": {
                    "type": "boolean",
                    "default": True,
                    "description": "For direct_practice, alternate taps on the known 直接练 and transition-start positions until the answer page is visible.",
                },
                "direct_practice_tap_interval_sec": {"type": "number", "default": 0.12},
                "answer_ready_poll_after_taps": {"type": "integer", "default": 3},
                "after_continue_wait_sec": {"type": "number", "default": 0.18},
                "after_finish_wait_sec": {"type": "number", "default": 0.35},
                "min_answer_ready_after_continue_sec": {
                    "type": "number",
                    "default": 2.2,
                    "description": "Do not accept the old result page as the next answer page until this much time has passed after tapping 继续.",
                },
                "tap_card_direct_practice_if_needed": {
                    "type": "boolean",
                    "default": True,
                    "description": "If 继续 lands back on the LessonActivity card list, tap the current card's 直接练 button automatically.",
                },
                "card_direct_practice_taps": {"type": "integer", "default": 4},
                "card_direct_practice_interval_sec": {"type": "number", "default": 0.12},
                "transition_skip_taps": {
                    "type": "integer",
                    "default": 6,
                    "description": "For transition screens with a 开始 countdown, tap the known start button position while waiting.",
                },
                "transition_skip_interval_sec": {"type": "number", "default": 0.1},
                "disable_system_animations": {
                    "type": "boolean",
                    "default": True,
                    "description": "Temporarily set Android animation scales to 0 while opening the native answer page.",
                },
                "restore_system_animations": {"type": "boolean", "default": True},
                "skip_lesson_focus_check": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "handler": tool_xiaoluxue_lesson_fast_path,
    },
    "xiaoluxue_switch_env": {
        "description": "Switch Xiaoluxue student API environment through the Galaxy Zhixue config app, defaulting to Test环境, then optionally reopen the student app.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "env": {
                    "type": "string",
                    "default": "test",
                    "description": "Target environment: prod/prod-com, dev, test, test2, test3, test4, test5, test6, or kmtest.",
                },
                "open_student": {
                    "type": "boolean",
                    "default": True,
                    "description": "Open Xiaoluxue student after applying the config.",
                },
                "force_submit": {
                    "type": "boolean",
                    "default": False,
                    "description": "Submit even when the current config already matches the requested environment.",
                },
                "force_stop_student": {
                    "type": "boolean",
                    "description": "Force-stop Xiaoluxue student before reopening. Defaults to true only when the env changed or force_submit=true.",
                },
                "timeout_sec": {"type": "number", "default": 10},
            },
            "additionalProperties": False,
        },
        "handler": tool_xiaoluxue_switch_env,
    },
    "xiaoluxue_exercise_snapshot": {
        "description": "Read a fast DOM snapshot from the Xiaoluxue /exercise WebView, including question text, options, buttons, progress text, and audio state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "timeout_sec": {"type": "number", "default": 10},
            },
            "additionalProperties": False,
        },
        "handler": tool_xiaoluxue_exercise_snapshot,
    },
    "xiaoluxue_exercise_action": {
        "description": "Run one semantic action on a Xiaoluxue /exercise page: fill an answer input, select an option, submit, continue, next, uncertain, give up, or click a button by text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "action_name": {
                    "type": "string",
                    "default": "next",
                    "enum": [
                        "select_option",
                        "answer",
                        "fill_answer",
                        "input_answer",
                        "submit",
                        "next",
                        "continue",
                        "uncertain",
                        "give_up",
                        "button_text",
                    ],
                },
                "option_key": {"type": "string", "description": "Option key such as A, B, C, D, TRUE, FALSE, 正确, or 错误."},
                "option_index": {"type": "integer", "description": "1-based visible option index."},
                "option_text": {"type": "string", "description": "Substring of the option text to select."},
                "answer_text": {"type": "string", "description": "Text or LaTeX content to fill into the visible answer input box through the WebView DOM fast path."},
                "button_text": {"type": "string", "description": "Visible button text used when action_name is button_text."},
                "timeout_sec": {"type": "number", "default": 10},
            },
            "additionalProperties": False,
        },
        "handler": tool_xiaoluxue_exercise_action,
    },
    "xiaoluxue_exercise_fast_path": {
        "description": "Run the Xiaoluxue /exercise fast path. It can fill answer_text through direct WebView DOM/React assignment, select an option, auto-answer from the page store, optionally submit, optionally continue, or default to next/continue.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "option_key": {"type": "string"},
                "option_index": {"type": "integer", "description": "1-based visible option index."},
                "option_text": {"type": "string"},
                "answer_text": {"type": "string", "description": "Text or LaTeX content to fill into the visible answer input box through the WebView DOM fast path."},
                "submit": {"type": "boolean", "default": False},
                "continue_after_submit": {"type": "boolean", "default": False},
                "action_name": {
                    "type": "string",
                    "default": "next",
                    "enum": ["next", "continue", "submit", "uncertain", "give_up", "button_text", "auto_answer"],
                    "description": "Used when no option is provided.",
                },
                "button_text": {"type": "string"},
                "after_action_wait_sec": {"type": "number", "default": 0.4},
                "max_steps": {
                    "type": "integer",
                    "default": 24,
                    "description": "For action_name=auto_answer, maximum question/action steps to run.",
                },
                "step_wait_sec": {
                    "type": "number",
                    "default": 0.45,
                    "description": "For action_name=auto_answer, wait between answer actions to avoid fast-submit guards.",
                },
                "click_report": {
                    "type": "boolean",
                    "default": True,
                    "description": "For action_name=auto_answer, schedule a click on 查看报告 before returning.",
                },
                "timeout_sec": {"type": "number", "default": 15},
            },
            "additionalProperties": False,
        },
        "handler": tool_xiaoluxue_exercise_fast_path,
    },
    "android_start_recording": {
        "description": "Start recording deterministic Android actions into a trace for later recipe generation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "name": {"type": "string"},
                "include_screenshots": {"type": "boolean", "default": False},
                "redact_text": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, typed text values are replaced with character counts in the trace.",
                },
                "after_delay_sec": {"type": "number", "default": 0.25},
            },
            "additionalProperties": False,
        },
        "handler": tool_start_recording,
    },
    "android_record_checkpoint": {
        "description": "Capture a named UI checkpoint in the active Android recording, useful after manual scrcpy navigation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "label": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "handler": tool_record_checkpoint,
    },
    "android_stop_recording": {
        "description": "Stop the active Android recording and write its trace.json file.",
        "inputSchema": {
            "type": "object",
            "properties": {"serial": {"type": "string"}},
            "additionalProperties": False,
        },
        "handler": tool_stop_recording,
    },
    "android_create_recipe": {
        "description": "Convert a recorded Android trace into a selector-first replay recipe.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "trace": {"type": "string", "description": "Trace path, recording id, or recording directory."},
                "name": {"type": "string"},
                "output_path": {"type": "string"},
            },
            "required": ["trace"],
            "additionalProperties": False,
        },
        "handler": tool_create_recipe,
    },
    "android_replay_recipe": {
        "description": "Replay a selector-first Android recipe using UIAutomator selectors before coordinate fallback.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "recipe": {"type": "string", "description": "Recipe path or recipe name under .android-use/recipes."},
                "dry_run": {"type": "boolean", "default": False},
                "strict_verify": {"type": "boolean", "default": False},
                "step_delay_sec": {"type": "number", "default": 0.25},
            },
            "required": ["recipe"],
            "additionalProperties": False,
        },
        "handler": tool_replay_recipe,
    },
    "android_index_source": {
        "description": "Scan Android app source code and write an app-map JSON with activities, routes, ids, and visible labels.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string"},
                "output_path": {"type": "string"},
                "max_files": {"type": "integer", "default": 2000},
            },
            "required": ["source_path"],
            "additionalProperties": False,
        },
        "handler": tool_index_source,
    },
    "android_start_scrcpy": {
        "description": "Launch scrcpy for the selected Android device, or for multiple serials. Defaults to draggable windows with explicit initial size.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "serials": {
                    "type": ["array", "string"],
                    "items": {"type": "string"},
                    "description": "Optional list or comma-separated string of adb serials to mirror in separate scrcpy windows.",
                },
                "max_size": {"type": "integer", "default": 1280},
                "bit_rate": {"type": "string", "default": "8M"},
                "audio": {
                    "type": "boolean",
                    "default": False,
                    "description": "Enable scrcpy audio forwarding. Disabled by default for screen-control stability.",
                },
                "keyboard": {
                    "type": "string",
                    "default": "sdk",
                    "enum": ["disabled", "sdk", "uhid", "aoa"],
                    "description": "Keyboard injection mode. sdk is best for normal text entry; uhid/aoa simulate a physical keyboard and need Android keyboard layout setup.",
                },
                "prefer_text": {
                    "type": "boolean",
                    "default": True,
                    "description": "With keyboard=sdk, inject alpha characters and spaces as text events so typing in scrcpy works better.",
                },
                "legacy_paste": {
                    "type": "boolean",
                    "default": False,
                    "description": "Use scrcpy legacy paste behavior for devices where normal clipboard paste fails.",
                },
                "stay_awake": {"type": "boolean", "default": False},
                "turn_screen_off": {"type": "boolean", "default": False},
                "keep_alive": {
                    "type": "boolean",
                    "default": True,
                    "description": "Retry startup-time scrcpy exits. Manual window closes after startup are respected until the next Android tool call.",
                },
                "fixed_window": {
                    "type": "boolean",
                    "default": True,
                    "description": "Set explicit initial window width/height based on the device screen.",
                },
                "borderless": {
                    "type": "boolean",
                    "default": False,
                    "description": "Remove window decorations. This prevents normal resizing but also makes the window hard to drag on macOS.",
                },
                "window_width": {"type": "integer"},
                "window_height": {"type": "integer"},
                "always_on_top": {"type": "boolean", "default": False},
                "lock_window_size": {
                    "type": "boolean",
                    "default": True,
                    "description": "Use macOS accessibility automation to keep the draggable scrcpy window at the requested size.",
                },
                "lock_window_continuous": {
                    "type": "boolean",
                    "default": False,
                    "description": "Keep enforcing the scrcpy window size continuously. Disabled by default so the helper does not interfere with keyboard focus.",
                },
                "window_title": {"type": "string"},
                "extra_args": {"type": "array", "items": {"type": "string"}},
                "force": {
                    "type": "boolean",
                    "default": False,
                    "description": "Start a new scrcpy process even if one is already visible for the same serial.",
                },
            },
            "additionalProperties": False,
        },
        "handler": tool_start_scrcpy,
    },
    "android_scrcpy_resident_status": {
        "description": "Report and start the background monitor. It never starts WebRTC and respects manual scrcpy window closes until the next Android tool call.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "handler": tool_scrcpy_resident_status,
    },
    "android_stop_scrcpy": {
        "description": "Stop scrcpy processes launched by this MCP server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "all": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
        "handler": tool_stop_scrcpy,
    },
    "android_start_screen_viewer": {
        "description": "Start a local Codex-friendly web viewer that refreshes the Android screen image.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "description": "Optional local port. Uses a free port when omitted."},
                "interval_ms": {"type": "integer", "default": 1000},
            },
            "additionalProperties": False,
        },
        "handler": tool_start_screen_viewer,
    },
    "android_stop_screen_viewer": {
        "description": "Stop Android screen viewer processes launched by this MCP server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "all": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
        "handler": tool_stop_screen_viewer,
    },
    "android_start_webrtc_viewer": {
        "description": "Start a local WebRTC viewer that streams the Android screen from scrcpy into a Codex-openable web page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "description": "Optional local port. Uses a free port when omitted."},
                "max_size": {"type": "integer", "default": 960},
                "bit_rate": {"type": "string", "default": "4M"},
                "max_fps": {"type": "integer", "default": 30},
            },
            "additionalProperties": False,
        },
        "handler": tool_start_webrtc_viewer,
    },
    "android_stop_webrtc_viewer": {
        "description": "Stop Android WebRTC viewer processes launched by this MCP server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "all": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
        "handler": tool_stop_webrtc_viewer,
    },
    "android_agent_step": {
        "description": "Run one Agent-TARS-style Android step. Hybrid mode first tries UIAutomator text grounding, then VLM visual grounding.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "instruction": {"type": "string"},
                "execute": {"type": "boolean", "default": False},
                "model": {"type": "string"},
                "provider": {
                    "type": "string",
                    "default": "auto",
                    "enum": ["auto", "openai-computer", "openai-vision", "openai-compatible"],
                },
                "mode": {
                    "type": "string",
                    "default": "hybrid",
                    "enum": ["hybrid", "visual-grounding", "uiautomator", "accessibility"],
                },
                "coordinate_mode": {
                    "type": "string",
                    "description": "absolute or normalized_1000. Defaults from model/env.",
                },
                "history": {"type": "array", "items": {"type": "object"}},
                "show_scrcpy": {
                    "type": "boolean",
                    "default": True,
                    "description": "Ensure a visible desktop scrcpy window before executing. Reuses an existing visible scrcpy process and does not start WebRTC.",
                },
            },
            "required": ["instruction"],
            "additionalProperties": False,
        },
        "handler": tool_agent_step,
    },
    "android_agent_run": {
        "description": "Run a bounded Agent-TARS-style Android loop: observe screenshot/UI tree, reason with VLM, act, and observe again. Defaults to executing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "instruction": {"type": "string"},
                "max_steps": {"type": "integer", "default": 5},
                "dry_run": {"type": "boolean", "default": False},
                "delay_sec": {"type": "number", "default": 0.25},
                "model": {"type": "string"},
                "provider": {
                    "type": "string",
                    "default": "auto",
                    "enum": ["auto", "openai-computer", "openai-vision", "openai-compatible"],
                },
                "mode": {
                    "type": "string",
                    "default": "hybrid",
                    "enum": ["hybrid", "visual-grounding", "uiautomator", "accessibility"],
                },
                "coordinate_mode": {
                    "type": "string",
                    "description": "absolute or normalized_1000. Defaults from model/env.",
                },
                "show_scrcpy": {
                    "type": "boolean",
                    "default": True,
                    "description": "Ensure a visible desktop scrcpy window before executing. Reuses an existing visible scrcpy process and does not start WebRTC.",
                },
            },
            "required": ["instruction"],
            "additionalProperties": False,
        },
        "handler": tool_agent_run,
    },
    "android_agent_tars_step": {
        "description": "Alias for android_agent_step with Agent-TARS/UI-TARS mobile action semantics.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "instruction": {"type": "string"},
                "execute": {"type": "boolean", "default": True},
                "model": {"type": "string"},
                "provider": {
                    "type": "string",
                    "default": "auto",
                    "enum": ["auto", "openai-computer", "openai-vision", "openai-compatible"],
                },
                "mode": {
                    "type": "string",
                    "default": "hybrid",
                    "enum": ["hybrid", "visual-grounding", "uiautomator", "accessibility"],
                },
                "coordinate_mode": {"type": "string"},
                "show_scrcpy": {"type": "boolean", "default": True},
            },
            "required": ["instruction"],
            "additionalProperties": False,
        },
        "handler": lambda args: tool_agent_step({**args, "execute": args.get("execute", True)}),
    },
    "android_agent_tars_run": {
        "description": "Alias for android_agent_run. This is the preferred natural-language Android operator loop.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "instruction": {"type": "string"},
                "max_steps": {"type": "integer", "default": 5},
                "dry_run": {"type": "boolean", "default": False},
                "delay_sec": {"type": "number", "default": 0.25},
                "model": {"type": "string"},
                "provider": {
                    "type": "string",
                    "default": "auto",
                    "enum": ["auto", "openai-computer", "openai-vision", "openai-compatible"],
                },
                "mode": {
                    "type": "string",
                    "default": "hybrid",
                    "enum": ["hybrid", "visual-grounding", "uiautomator", "accessibility"],
                },
                "coordinate_mode": {"type": "string"},
                "show_scrcpy": {"type": "boolean", "default": True},
            },
            "required": ["instruction"],
            "additionalProperties": False,
        },
        "handler": tool_agent_run,
    },
}


def tool_descriptors() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": metadata["description"],
            "inputSchema": metadata["inputSchema"],
        }
        for name, metadata in TOOLS.items()
    ]


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    message_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    if method == "notifications/initialized":
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": message_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": message_id, "result": {"tools": tool_descriptors()}}

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in TOOLS:
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {"content": [text_content(f"Unknown tool: {name}")], "isError": True},
            }
        try:
            maybe_show_scrcpy_for_tool_call(name, arguments)
            content = TOOLS[name]["handler"](arguments)
            return {"jsonrpc": "2.0", "id": message_id, "result": {"content": content}}
        except Exception as exc:  # Keep MCP session alive on tool errors.
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {"content": [text_content(str(exc))], "isError": True},
            }

    if message_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    start_resident_scrcpy_monitor()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            response = handle_request(message)
            if response is not None:
                send(response)
        except Exception as exc:
            error = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Invalid request: {exc}"},
            }
            send(error)


if __name__ == "__main__":
    main()
