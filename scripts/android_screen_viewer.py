#!/usr/bin/env python3
"""Tiny local web viewer for an attached Android screen."""

from __future__ import annotations

import argparse
import html
import json
import os
import struct
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


def decode_bytes(payload: bytes) -> str:
    return payload.decode("utf-8", errors="replace").strip()


def png_size(png: bytes) -> dict[str, int | None]:
    if len(png) >= 24 and png.startswith(b"\x89PNG"):
        width, height = struct.unpack(">II", png[16:24])
        return {"width": int(width), "height": int(height)}
    return {"width": None, "height": None}


def screencap(adb: str, serial: str) -> bytes:
    result = subprocess.run(
        [adb, "-s", serial, "exec-out", "screencap", "-p"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(decode_bytes(result.stderr) or decode_bytes(result.stdout))
    png = result.stdout
    if not png.startswith(b"\x89PNG"):
        fixed = png.replace(b"\r\n", b"\n")
        if fixed.startswith(b"\x89PNG"):
            png = fixed
    if not png.startswith(b"\x89PNG"):
        raise RuntimeError("adb screencap did not return a PNG image")
    return png


def make_handler(adb: str, serial: str, interval_ms: int) -> type[BaseHTTPRequestHandler]:
    class AndroidScreenHandler(BaseHTTPRequestHandler):
        server_version = "AndroidScreenViewer/0.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def send_bytes(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/":
                self.send_bytes(200, "text/html; charset=utf-8", self.index_html().encode("utf-8"))
                return
            if path == "/screen.png":
                try:
                    self.send_bytes(200, "image/png", screencap(adb, serial))
                except Exception as exc:
                    self.send_bytes(500, "text/plain; charset=utf-8", str(exc).encode("utf-8"))
                return
            if path == "/state.json":
                try:
                    png = screencap(adb, serial)
                    payload = {
                        "serial": serial,
                        "screen": png_size(png),
                        "bytes": len(png),
                        "timestamp": time.time(),
                    }
                    self.send_bytes(
                        200,
                        "application/json; charset=utf-8",
                        json.dumps(payload).encode("utf-8"),
                    )
                except Exception as exc:
                    self.send_bytes(500, "application/json; charset=utf-8", json.dumps({"error": str(exc)}).encode("utf-8"))
                return
            self.send_bytes(404, "text/plain; charset=utf-8", b"Not found")

        def index_html(self) -> str:
            safe_serial = html.escape(serial)
            return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Android Screen</title>
  <style>
    :root {{
      color-scheme: dark;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: #050505;
      color: #f5f5f5;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto 1fr;
      background: #050505;
    }}
    header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 10px 14px;
      border-bottom: 1px solid #242424;
      background: #111;
      font-size: 13px;
    }}
    .status {{
      color: #a3e635;
      white-space: nowrap;
    }}
    main {{
      display: grid;
      place-items: center;
      overflow: hidden;
      padding: 12px;
    }}
    img {{
      max-width: 100%;
      max-height: calc(100vh - 64px);
      object-fit: contain;
      background: #000;
      box-shadow: 0 0 0 1px #242424;
    }}
  </style>
</head>
<body>
  <header>
    <div>Android <strong>{safe_serial}</strong></div>
    <div class="status" id="status">refreshing every {interval_ms} ms</div>
  </header>
  <main>
    <img id="screen" alt="Android device screen" />
  </main>
  <script>
    const img = document.getElementById('screen');
    const status = document.getElementById('status');
    async function refresh() {{
      const now = Date.now();
      img.src = `/screen.png?t=${{now}}`;
      status.textContent = `updated ${{new Date().toLocaleTimeString()}}`;
    }}
    img.addEventListener('error', () => {{
      status.textContent = 'screen refresh failed';
    }});
    refresh();
    setInterval(refresh, {interval_ms});
  </script>
</body>
</html>"""

    return AndroidScreenHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve an Android screen as an auto-refreshing web page.")
    parser.add_argument("--adb", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--interval-ms", type=int, default=1000)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    handler = make_handler(args.adb, args.serial, args.interval_ms)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Android screen viewer listening on http://{args.host}:{args.port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
