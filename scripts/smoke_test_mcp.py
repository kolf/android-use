#!/usr/bin/env python3
"""Smoke-test the Android Use MCP server without requiring adb devices."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "scripts" / "android_use_mcp.py"


def request(process: subprocess.Popen[str], message: dict) -> dict:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()
    line = process.stdout.readline()
    if not line:
        raise RuntimeError("MCP server exited before responding")
    return json.loads(line)


def main() -> int:
    process = subprocess.Popen(
        [sys.executable, str(SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        initialized = request(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            },
        )
        tools = request(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tool_names = [tool["name"] for tool in tools["result"]["tools"]]
        required = {
            "android_list_devices",
            "android_screenshot",
            "android_show_screen",
            "android_appshot",
            "android_start_screen_viewer",
            "android_start_webrtc_viewer",
            "android_agent_step",
            "android_start_scrcpy",
            "android_start_scrcpy_app",
            "android_wireless_pair",
            "android_wireless_reconnect",
        }
        missing = sorted(required.difference(tool_names))
        if missing:
            raise RuntimeError(f"Missing tools: {missing}")
        print(
            json.dumps(
                {
                    "server": initialized["result"]["serverInfo"],
                    "tool_count": len(tool_names),
                    "sample_tools": tool_names[:6],
                },
                indent=2,
            )
        )
        return 0
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
