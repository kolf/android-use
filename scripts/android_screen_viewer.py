#!/usr/bin/env python3
"""Local Android timeline viewer backed by screenshots, not video streaming."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import struct
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


def import_android_use_mcp() -> Any:
    import importlib
    import sys

    scripts_dir = Path(__file__).resolve().parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    return importlib.import_module("android_use_mcp")


def decode_bytes(payload: bytes) -> str:
    return payload.decode("utf-8", errors="replace").strip()


def png_size(png: bytes) -> dict[str, int | None]:
    if len(png) >= 24 and png.startswith(b"\x89PNG"):
        width, height = struct.unpack(">II", png[16:24])
        return {"width": int(width), "height": int(height)}
    return {"width": None, "height": None}


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip(".-")
    return cleaned[:80] or "event"


def timestamp_ms() -> int:
    return int(time.time() * 1000)


def timestamp_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def screencap(serial: str) -> bytes:
    mcp = import_android_use_mcp()
    png = mcp.adb(["exec-out", "screencap", "-p"], serial=serial, timeout=20)
    if not png.startswith(b"\x89PNG"):
        fixed = png.replace(b"\r\n", b"\n")
        if fixed.startswith(b"\x89PNG"):
            png = fixed
    if not png.startswith(b"\x89PNG"):
        raise RuntimeError("adb screencap did not return a PNG image")
    return png


class TimelineStore:
    def __init__(self, serial: str, session_dir: Path, max_events: int) -> None:
        self.serial = serial
        self.session_dir = session_dir
        self.shots_dir = session_dir / "shots"
        self.events_path = session_dir / "events.jsonl"
        self.max_events = max(10, max_events)
        self.last_digest: str | None = None
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.shots_dir.mkdir(parents=True, exist_ok=True)

    def event_lines(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in self.events_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("kind") != "action":
                continue
            events.append(event)
        return events[-self.max_events :]

    def event_version(self) -> str:
        if not self.events_path.exists():
            return "0:0"
        stat = self.events_path.stat()
        return f"{stat.st_mtime_ns}:{stat.st_size}"

    def append_event(self, event: dict[str, Any]) -> None:
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")

    def capture_event(self, kind: str, title: str, detail: str = "", *, force: bool = False) -> dict[str, Any] | None:
        png = screencap(self.serial)
        digest = hashlib.sha256(png).hexdigest()
        if not force and digest == self.last_digest:
            return None
        self.last_digest = digest
        event_id = f"{timestamp_ms()}-{slugify(kind)}"
        filename = f"{event_id}.png"
        path = self.shots_dir / filename
        path.write_bytes(png)
        event = {
            "id": event_id,
            "kind": kind,
            "title": title,
            "detail": detail,
            "timestamp": timestamp_iso(),
            "timestamp_epoch": time.time(),
            "serial": self.serial,
            "screenshot_path": str(path),
            "screenshot_url": f"/shots/{filename}",
            "screen": png_size(png),
            "bytes": len(png),
            "digest": digest[:16],
        }
        self.append_event(event)
        return event

    def state(self) -> dict[str, Any]:
        events = self.event_lines()
        return {
            "ok": True,
            "serial": self.serial,
            "session_dir": str(self.session_dir),
            "events_path": str(self.events_path),
            "events": [
                {**event, "index": index + 1}
                for index, event in enumerate(events)
            ],
            "count": len(events),
            "timestamp": time.time(),
        }


def make_handler(store: TimelineStore, interval_ms: int) -> type[BaseHTTPRequestHandler]:
    class AndroidTimelineHandler(BaseHTTPRequestHandler):
        server_version = "AndroidTimelineViewer/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def send_bytes(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status: int, payload: dict[str, Any]) -> None:
            self.send_bytes(
                status,
                "application/json; charset=utf-8",
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/":
                self.send_bytes(200, "text/html; charset=utf-8", self.index_html().encode("utf-8"))
                return
            if path == "/favicon.ico":
                self.send_bytes(204, "image/x-icon", b"")
                return
            if path == "/screen.png":
                try:
                    self.send_bytes(200, "image/png", screencap(store.serial))
                except Exception as exc:
                    self.send_bytes(500, "text/plain; charset=utf-8", str(exc).encode("utf-8"))
                return
            if path == "/state.json" or path == "/api/state":
                try:
                    self.send_json(200, store.state())
                except Exception as exc:
                    self.send_json(500, {"ok": False, "error": str(exc), **store.state()})
                return
            if path == "/api/events":
                self.send_event_stream()
                return
            if path == "/api/checkpoint":
                query = parse_qs(parsed.query)
                label = str((query.get("label") or ["手动标记"])[0]).strip() or "手动标记"
                try:
                    event = store.capture_event("checkpoint", label, "Manual timeline checkpoint", force=True)
                    self.send_json(200, {"ok": True, "event": event, **store.state()})
                except Exception as exc:
                    self.send_json(500, {"ok": False, "error": str(exc), **store.state()})
                return
            if path.startswith("/shots/"):
                filename = Path(unquote(path[len("/shots/") :])).name
                shot_path = store.shots_dir / filename
                if shot_path.exists() and shot_path.is_file():
                    self.send_bytes(200, "image/png", shot_path.read_bytes())
                    return
                self.send_bytes(404, "text/plain; charset=utf-8", b"Shot not found")
                return
            self.send_bytes(404, "text/plain; charset=utf-8", b"Not found")

        def send_event_stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, max-age=0")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            last_version: str | None = None
            heartbeat_at = time.time()
            while True:
                try:
                    version = store.event_version()
                    if version != last_version:
                        last_version = version
                        payload = json.dumps(store.state(), ensure_ascii=False, separators=(",", ":"))
                        self.wfile.write(f"event: state\ndata: {payload}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    elif time.time() - heartbeat_at >= 15:
                        heartbeat_at = time.time()
                        self.wfile.write(b": waiting for Android Use actions\n\n")
                        self.wfile.flush()
                    time.sleep(max(0.25, min(interval_ms / 1000, 5.0)))
                except (BrokenPipeError, ConnectionResetError):
                    return

        def index_html(self) -> str:
            return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>步骤</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", sans-serif;
      background: #f6f7f9;
      color: #18212f;
      --line: #dfe4ec;
      --muted: #667085;
      --ink: #18212f;
      --accent: #0f766e;
      --accent-soft: #dff7f2;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background:
        linear-gradient(180deg, #ffffff 0, #f7f8fb 280px, #f7f8fb 100%);
    }}
    .meta, .small {{
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    input {{
      width: min(360px, 45vw);
      height: 36px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 0 10px;
      color: var(--ink);
      font: inherit;
      font-size: 13px;
    }}
    input:focus {{
      outline: 2px solid rgba(15, 118, 110, .18);
      border-color: var(--accent);
    }}
    main {{
      width: min(1120px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 22px 0 42px;
      min-height: 100vh;
    }}
    .timeline-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding-bottom: 18px;
      border-bottom: 1px solid var(--line);
      margin-bottom: 0;
    }}
    .timeline-title {{
      font-size: 22px;
      line-height: 1.2;
      font-weight: 760;
    }}
    .head-actions {{
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 12px;
      min-width: 0;
    }}
    .timeline {{
      padding: 0 0 20px;
    }}
    .event {{
      display: grid;
      grid-template-columns: 56px minmax(0, 1fr);
      gap: 18px;
      margin: 0;
      padding: 24px 0 30px;
      position: relative;
      border-bottom: 1px solid var(--line);
    }}
    .event:not(:last-child)::before {{
      content: "";
      position: absolute;
      left: 17px;
      top: 58px;
      bottom: -1px;
      width: 2px;
      background: #e7ebf1;
    }}
    .pin {{
      width: 36px;
      height: 36px;
      border-radius: 999px;
      background: #ffffff;
      border: 1px solid #ccd5e1;
      color: #344054;
      display: grid;
      place-items: center;
      font-size: 13px;
      font-weight: 760;
      margin: 0;
      position: relative;
      z-index: 1;
    }}
    .event-content {{
      display: grid;
      gap: 14px;
      min-width: 0;
    }}
    .event-heading {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      min-width: 0;
    }}
    .event-body {{
      display: grid;
      gap: 12px;
      padding: 0;
      transition: transform .16s ease;
    }}
    .event.match .pin {{
      border-color: rgba(15, 118, 110, .5);
      background: var(--accent-soft);
      color: var(--accent);
    }}
    .event.focused .event-body {{
      transform: translateY(-1px);
    }}
    .event.focused .pin {{
      border-color: var(--accent);
      background: var(--accent);
      color: #ffffff;
    }}
    .event.focused .shot {{
      outline: 3px solid rgba(15, 118, 110, .18);
      border-color: rgba(15, 118, 110, .45);
    }}
    .shot {{
      width: 100%;
      max-height: 82vh;
      object-fit: contain;
      border-radius: 4px;
      background: #ffffff;
      border: 1px solid #cfd7e3;
      display: block;
    }}
    .event-title {{
      margin: 0;
      font-size: 18px;
      font-weight: 720;
      line-height: 1.35;
      letter-spacing: 0;
      overflow-wrap: anywhere;
    }}
    .event-time {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
      white-space: nowrap;
    }}
    .event-detail {{
      display: grid;
      gap: 5px;
      padding: 2px 0 0;
    }}
    .event-detail-label {{
      color: #344054;
      font-size: 12px;
      font-weight: 720;
      line-height: 1.2;
    }}
    .event-detail-text {{
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }}
    .empty {{
      padding: 28px;
      border: 1px dashed var(--line);
      border-radius: 8px;
      color: var(--muted);
      background: rgba(255,255,255,.58);
      text-align: center;
      font-size: 13px;
    }}
    @media (max-width: 960px) {{
      main {{ width: min(100vw - 20px, 1120px); padding-top: 14px; }}
      .timeline-head {{ align-items: stretch; flex-direction: column; }}
      .head-actions {{ align-items: stretch; justify-content: flex-start; flex-direction: column; }}
      input {{ width: 100%; }}
      .event {{ grid-template-columns: 38px minmax(0, 1fr); gap: 10px; padding: 18px 0 24px; }}
      .event:not(:last-child)::before {{ left: 14px; top: 48px; }}
      .pin {{ width: 30px; height: 30px; font-size: 12px; }}
      .event-heading {{ align-items: flex-start; flex-direction: column; gap: 3px; }}
      .event-title {{ font-size: 16px; }}
    }}
  </style>
</head>
<body>
  <main>
    <div class="timeline-head">
      <strong class="timeline-title">步骤</strong>
      <div class="head-actions">
        <input id="search" type="search" placeholder="搜索步骤" aria-label="搜索步骤" />
        <div class="meta"><span id="count">0 个步骤</span> · <span id="status">等待 Android Use 操作</span></div>
      </div>
    </div>
    <div class="timeline" id="timeline"></div>
  </main>
  <script>
    const status = document.getElementById('status');
    const timeline = document.getElementById('timeline');
    const count = document.getElementById('count');
    const search = document.getElementById('search');
    let renderedEvents = [];
    let searchMatches = [];
    let activeMatch = -1;

    function kindText(kind) {{
      if (kind === 'action') return '操作';
      if (kind === 'checkpoint') return '标记';
      if (kind === 'initial') return '初始';
      return '跳转';
    }}

    function render(events) {{
      count.textContent = `${{events.length}} 个步骤`;
      if (!events.length) {{
        renderedEvents = [];
        timeline.innerHTML = '<div class="empty">等待 Android Use 操作步骤</div>';
        return;
      }}
      renderedEvents = events.slice().reverse();
      timeline.innerHTML = renderedEvents.map((event, renderIndex) => `
        <article class="event" data-render-index="${{renderIndex}}">
          <div class="pin">${{escapeHtml(event.index)}}</div>
          <div class="event-content">
            <div class="event-heading">
              <h2 class="event-title">${{escapeHtml(event.index + '. ' + (event.title || kindText(event.kind)))}}</h2>
              <time class="event-time">${{escapeHtml(formatTime(event))}}</time>
            </div>
            <div class="event-body">
              <img class="shot" src="${{event.screenshot_url}}?t=${{event.digest || event.id}}" alt="${{escapeHtml(event.title || kindText(event.kind))}}" />
              <div class="event-detail">
                <strong class="event-detail-label">步骤详情</strong>
                <p class="event-detail-text">${{escapeHtml(detailText(event))}}</p>
              </div>
            </div>
          </div>
        </article>
      `).join('');
      applySearch(false);
    }}

    function detailText(event) {{
      return event.detail || event.timestamp || kindText(event.kind);
    }}

    function formatTime(event) {{
      const millis = event.timestamp_epoch ? Number(event.timestamp_epoch) * 1000 : Date.parse(event.timestamp || '');
      if (Number.isFinite(millis)) {{
        return new Date(millis).toLocaleString();
      }}
      return event.timestamp || '';
    }}

    function eventText(event) {{
      return [
        event.index,
        event.title,
        event.detail,
        kindText(event.kind),
        event.timestamp
      ].filter(Boolean).join(' ').toLowerCase();
    }}

    function focusMatch(index, shouldScroll) {{
      document.querySelectorAll('.event.focused').forEach((node) => node.classList.remove('focused'));
      const target = searchMatches[index];
      if (!target) return;
      target.classList.add('focused');
      if (shouldScroll) {{
        target.scrollIntoView({{ block: 'center', behavior: 'smooth' }});
      }}
    }}

    function applySearch(shouldScroll) {{
      const query = search.value.trim().toLowerCase();
      const rows = Array.from(document.querySelectorAll('.event'));
      rows.forEach((row) => row.classList.remove('match', 'focused'));
      searchMatches = [];
      activeMatch = -1;
      if (!query) return;
      rows.forEach((row) => {{
        const event = renderedEvents[Number(row.dataset.renderIndex)];
        if (event && eventText(event).includes(query)) {{
          row.classList.add('match');
          searchMatches.push(row);
        }}
      }});
      if (searchMatches.length) {{
        activeMatch = 0;
        focusMatch(activeMatch, shouldScroll);
      }}
    }}

    function escapeHtml(value) {{
      return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
    }}

    async function loadState() {{
      try {{
        const response = await fetch(`/api/state?t=${{Date.now()}}`, {{ cache: 'no-store' }});
        const payload = await response.json();
        render(payload.events || []);
        status.textContent = `更新 ${{new Date().toLocaleTimeString()}}`;
      }} catch (error) {{
        status.textContent = '刷新失败';
      }}
    }}

    function connectEvents() {{
      const source = new EventSource('/api/events');
      source.addEventListener('state', (event) => {{
        const payload = JSON.parse(event.data);
        render(payload.events || []);
        status.textContent = `更新 ${{new Date().toLocaleTimeString()}}`;
      }});
      source.onerror = () => {{
        status.textContent = '等待重新连接';
      }};
    }}

    search.addEventListener('input', () => applySearch(true));
    search.addEventListener('keydown', (event) => {{
      if (event.key !== 'Enter' || !searchMatches.length) return;
      event.preventDefault();
      activeMatch = (activeMatch + 1) % searchMatches.length;
      focusMatch(activeMatch, true);
    }});
    loadState();
    connectEvents();
  </script>
</body>
</html>"""

    return AndroidTimelineHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve an Android screenshot timeline web UI.")
    parser.add_argument("--serial", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--interval-ms", type=int, default=1000)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--max-events", type=int, default=80)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    store = TimelineStore(args.serial, Path(args.session_dir).expanduser(), args.max_events)
    handler = make_handler(store, args.interval_ms)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Android timeline viewer listening on http://{args.host}:{args.port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
