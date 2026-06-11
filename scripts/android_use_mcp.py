#!/usr/bin/env python3
"""MCP server for generic Android device control through adb and Playwright.

The implementation is split into domain-focused files under
``scripts/android_use_parts`` so each source file stays readable and below the
project's 2000-line limit. Parts are executed in this module namespace to keep
the public helper names and MCP entrypoint stable for existing tests, scripts,
and plugin metadata.
"""

from __future__ import annotations

import __future__
from pathlib import Path


_PARTS_DIR = Path(__file__).resolve().with_name("android_use_parts")
_PART_FILES = [
    "00_runtime.py",
    "01_wireless_qr.py",
    "01_ui_recording_recipe.py",
    "02_input_and_basic_tools.py",
    "03_playwright_webview.py",
    "07_scrcpy_viewers_recording.py",
    "08_agent_models.py",
    "09_tool_catalog.py",
    "10_protocol.py",
]


def _load_implementation_parts() -> None:
    namespace = globals()
    future_flags = __future__.annotations.compiler_flag
    for filename in _PART_FILES:
        path = _PARTS_DIR / filename
        source = path.read_text()
        exec(compile(source, str(path), "exec", flags=future_flags, dont_inherit=True), namespace)


_load_implementation_parts()
