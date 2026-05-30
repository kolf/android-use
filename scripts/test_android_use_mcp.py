#!/usr/bin/env python3
"""Offline tests for the Android Use MCP server helpers."""

from __future__ import annotations

import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import android_use_mcp as mcp


def make_raw_screenshot(
    width: int,
    height: int,
    *,
    fill: tuple[int, int, int, int] = (255, 255, 255, 255),
    rects: list[tuple[int, int, int, int, tuple[int, int, int, int]]] | None = None,
) -> bytes:
    pixels = bytearray(fill * (width * height))
    for x1, y1, x2, y2, color in rects or []:
        for y in range(max(y1, 0), min(y2, height)):
            row = y * width * 4
            for x in range(max(x1, 0), min(x2, width)):
                offset = row + x * 4
                pixels[offset : offset + 4] = bytes(color)
    return struct.pack("<IIII", width, height, 1, 0) + bytes(pixels)


def make_png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )



_TEST_PARTS_DIR = Path(__file__).resolve().with_name("android_use_test_parts")
_TEST_PART_FILES = [
    "01_tests.py",
    "02_tests.py",
    "03_tests.py",
]


def _load_test_parts() -> None:
    namespace = globals()
    for filename in _TEST_PART_FILES:
        path = _TEST_PARTS_DIR / filename
        source = path.read_text()
        exec(compile(source, str(path), "exec"), namespace)


_load_test_parts()


if __name__ == "__main__":
    unittest.main()
