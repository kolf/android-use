#!/usr/bin/env python3
"""MCP server for Android device control through adb and scrcpy.

The server intentionally has no third-party Python dependencies. It speaks the
newline-delimited JSON-RPC transport used by MCP stdio servers.
"""

from __future__ import annotations

import base64
import ast
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


SERVER_NAME = "android-use"
SERVER_VERSION = "0.1.0"
DEFAULT_TIMEOUT = 30
TMP_DIR = Path(os.environ.get("ANDROID_USE_TMP_DIR", "/tmp/android-use"))
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
LOCAL_PLATFORM_TOOLS = PLUGIN_ROOT / "tools" / "android-platform-tools" / "platform-tools"
SCREEN_DIR = PLUGIN_ROOT / ".screen"
OPENAI_BASE_URL = "https://api.openai.com/v1"

SCRCPY_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}
SCRCPY_LOCK_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}
SCREEN_VIEWER_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}
WEBRTC_VIEWER_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}

REMOTE_UI_DUMP_PATH = "/sdcard/android-use-window.xml"


class AndroidUseError(Exception):
    """User-facing plugin error."""


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


def choose_serial(serial: str | None = None) -> str:
    devices = list_devices()
    connected = [device for device in devices if device.get("state") == "device"]

    if serial:
        for device in connected:
            if device.get("serial") == serial:
                return serial
        known = ", ".join(device.get("serial", "?") for device in devices) or "none"
        raise AndroidUseError(f"Android device '{serial}' is not connected and authorized. Known devices: {known}")

    if not connected:
        raise AndroidUseError("No authorized Android device found. Run `adb devices -l` and authorize USB debugging.")
    if len(connected) > 1:
        serials = ", ".join(device["serial"] for device in connected)
        raise AndroidUseError(f"Multiple Android devices are connected. Pass one serial: {serials}")
    return str(connected[0]["serial"])


def shell(serial: str, command: str, timeout: int | float = DEFAULT_TIMEOUT) -> str:
    return decode_bytes(adb(["shell", command], serial=serial, timeout=timeout))


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


def save_png(serial: str, png: bytes, save_path: str | None = None) -> str:
    if save_path:
        path = Path(save_path).expanduser()
    else:
        safe_serial = re.sub(r"[^A-Za-z0-9_.-]+", "-", serial)
        path = TMP_DIR / f"{safe_serial}-{int(time.time() * 1000)}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return str(path)


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
    payload: dict[str, Any] = {
        "adb": {
            "command": adb_binary(),
            "path": adb_path,
            "available": shutil.which(adb_binary()) is not None or Path(adb_binary()).exists(),
        },
        "scrcpy": {
            "command": scrcpy_binary(),
            "path": scrcpy_path,
            "available": shutil.which(scrcpy_binary()) is not None or Path(scrcpy_binary()).exists(),
        },
        "vlm": {
            "provider": os.environ.get("ANDROID_USE_AGENT_PROVIDER", "openai-compatible"),
            "base_url_configured": bool(os.environ.get("ANDROID_USE_VLM_BASE_URL")),
            "api_key_configured": bool(os.environ.get("ANDROID_USE_VLM_API_KEY")),
            "model": os.environ.get("ANDROID_USE_VLM_MODEL"),
            "coordinate_mode": infer_coordinate_mode(os.environ.get("ANDROID_USE_VLM_MODEL")),
            "timeout_sec": float(os.environ.get("ANDROID_USE_VLM_TIMEOUT", "45")),
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
    nodes = observe_ui(serial, limit=300)["ui"]["nodes"]
    node = find_ui_node(nodes, query, exact=exact, include_resource_id=include_resource_id)
    if not node and exact:
        node = find_ui_node(nodes, query, exact=False, include_resource_id=include_resource_id)
    point = node_click_point(node) if node else None
    if not point:
        raise AndroidUseError(f"Could not find a tappable UI node matching text: {query!r}")
    adb(["shell", "input", "tap", str(point["x"]), str(point["y"])], serial=serial, timeout=10)
    return [
        text_content(
            action_result(
                "tap_text",
                serial,
                {
                    "text": query,
                    "x": point["x"],
                    "y": point["y"],
                    "matched_node": {
                        key: node.get(key)
                        for key in ("index", "text", "content_desc", "resource_id", "class", "bounds", "clickable")
                    },
                },
            )
        )
    ]


def tool_tap(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    x = int(args["x"])
    y = int(args["y"])
    adb(["shell", "input", "tap", str(x), str(y)], serial=serial, timeout=10)
    return [text_content(action_result("tap", serial, {"x": x, "y": y}))]


def tool_swipe(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    start_x = int(args["start_x"])
    start_y = int(args["start_y"])
    end_x = int(args["end_x"])
    end_y = int(args["end_y"])
    duration_ms = int(args.get("duration_ms", 300))
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
    return [
        text_content(
            action_result(
                "swipe",
                serial,
                {
                    "start_x": start_x,
                    "start_y": start_y,
                    "end_x": end_x,
                    "end_y": end_y,
                    "duration_ms": duration_ms,
                },
            )
        )
    ]


def tool_type_text(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    text = str(args["text"])
    if args.get("clear_first"):
        adb(["shell", "input", "keyevent", "KEYCODE_MOVE_END"], serial=serial, timeout=10)
        for _ in range(int(args.get("clear_count", 80))):
            adb(["shell", "input", "keyevent", "KEYCODE_DEL"], serial=serial, timeout=10)
    if text:
        adb(["shell", "input", "text", escape_input_text(text)], serial=serial, timeout=15)
    if args.get("enter"):
        adb(["shell", "input", "keyevent", "KEYCODE_ENTER"], serial=serial, timeout=10)
    return [
        text_content(
            action_result(
                "type_text",
                serial,
                {"chars": len(text), "clear_first": bool(args.get("clear_first")), "enter": bool(args.get("enter"))},
            )
        )
    ]


def tool_press_key(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    key = keycode(args["key"])
    adb(["shell", "input", "keyevent", key], serial=serial, timeout=10)
    return [text_content(action_result("press_key", serial, {"key": key}))]


def tool_wake_unlock(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    adb(["shell", "input", "keyevent", "KEYCODE_WAKEUP"], serial=serial, timeout=10)
    if args.get("dismiss_keyguard", True):
        adb(["shell", "wm", "dismiss-keyguard"], serial=serial, timeout=10)
    return [
        text_content(
            action_result(
                "wake_unlock",
                serial,
                {"dismiss_keyguard": bool(args.get("dismiss_keyguard", True))},
            )
        )
    ]


def tool_open_url(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    url = str(args["url"]).strip()
    if not url:
        raise AndroidUseError("url must not be empty.")
    adb(
        ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url],
        serial=serial,
        timeout=15,
    )
    return [text_content(action_result("open_url", serial, {"url": url}))]


def tool_open_app(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    package = str(args["package"]).strip()
    activity = str(args.get("activity", "")).strip()
    if not package:
        raise AndroidUseError("package must not be empty.")
    if activity:
        component = activity if "/" in activity else f"{package}/{activity}"
        adb(["shell", "am", "start", "-n", component], serial=serial, timeout=15)
        return [text_content(action_result("open_app", serial, {"package": package, "activity": activity}))]

    adb(
        ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"],
        serial=serial,
        timeout=15,
    )
    return [text_content(action_result("open_app", serial, {"package": package}))]


def tool_shell(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    timeout = min(float(args.get("timeout_sec", 20)), 120)
    command = str(args["command"])
    stdout = shell(serial, command, timeout=timeout)
    return [text_content({"serial": serial, "command": command, "stdout": stdout})]


def tool_start_scrcpy(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    command = [scrcpy_binary(), "--serial", serial]
    max_size = int(args.get("max_size", 1280))
    window_width = args.get("window_width")
    window_height = args.get("window_height")
    window_title = str(args.get("window_title") or f"Android {serial}")
    command.extend(["--window-title", window_title])
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

    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    log_path = SCREEN_DIR / "scrcpy.log"
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
    time.sleep(0.5)
    if process.poll() is not None:
        log_text = log_path.read_text(errors="replace") if log_path.exists() else ""
        raise AndroidUseError(f"scrcpy failed to start.\n{log_text[-2000:]}")
    SCRCPY_PROCESSES[process.pid] = process

    lock_pid = None
    lock_error = None
    lock_log_path = SCREEN_DIR / "scrcpy-window-lock.log"
    should_lock_size = bool(args.get("lock_window_size", True)) and bool(window_width) and bool(window_height)
    if should_lock_size and not args.get("borderless", False):
        lock_script = PLUGIN_ROOT / "scripts" / "scrcpy_window_lock.py"
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
                lock_error = lock_log_path.read_text(errors="replace")[-2000:] if lock_log_path.exists() else "lock process exited"
        except Exception as exc:
            lock_log_handle.close()
            lock_error = str(exc)

    return [
        text_content(
            {
                "ok": True,
                "serial": serial,
                "pid": process.pid,
                "window_title": window_title,
                "command": command,
                "log_path": str(log_path),
                "lock_pid": lock_pid,
                "lock_log_path": str(lock_log_path) if should_lock_size else None,
                "lock_error": lock_error,
                "display": "scrcpy is running as a detached desktop window.",
            }
        )
    ]


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
        if process and process.poll() is None:
            process.terminate()
            stopped.append(pid)
    lock_stopped: list[int] = []
    for pid, process in list(SCRCPY_LOCK_PROCESSES.items()):
        if process.poll() is None:
            process.terminate()
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
    if not action_match:
        raise AndroidUseError(f"VLM response did not include an Action: {response_text}")
    action_line = action_match.group(1).strip().splitlines()[0].strip()

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

    if mode in {"hybrid", "uiautomator", "accessibility"}:
        fast_action = fast_ui_action_from_instruction(serial, instruction)
        if fast_action:
            content = [text_content({"serial": serial, "proposed_action": fast_action, "execute": execute, "mode": mode})]
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
    content = [text_content({"serial": serial, "proposed_action": action_for_display, "execute": execute, "mode": mode})]
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

    return [text_content({"serial": serial, "dry_run": dry_run, "steps": history})]


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
        "description": "Type simple text through adb shell input text.",
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
    "android_start_scrcpy": {
        "description": "Launch scrcpy for the selected Android device. Defaults to a draggable window with explicit initial size.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "max_size": {"type": "integer", "default": 1280},
                "bit_rate": {"type": "string", "default": "8M"},
                "stay_awake": {"type": "boolean", "default": False},
                "turn_screen_off": {"type": "boolean", "default": False},
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
                "window_title": {"type": "string"},
                "extra_args": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
        "handler": tool_start_scrcpy,
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
