#!/usr/bin/env python3
"""Serve an Android screen through a local WebRTC page.

The video source is scrcpy recording to a Matroska FIFO. aiortc/PyAV read that
live stream and publish it to the browser as a WebRTC video track.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import signal
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer, MediaRelay
from aiortc.mediastreams import MediaStreamError


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Android WebRTC</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: #050505;
      color: #f5f5f5;
    }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto 1fr;
      background: #050505;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 10px 14px;
      border-bottom: 1px solid #242424;
      background: #111;
      font-size: 13px;
    }
    main {
      display: grid;
      place-items: center;
      overflow: hidden;
      padding: 12px;
    }
    video {
      max-width: 100%;
      max-height: calc(100vh - 64px);
      object-fit: contain;
      background: #000;
      box-shadow: 0 0 0 1px #242424;
      cursor: crosshair;
      touch-action: none;
      user-select: none;
    }
    .status {
      color: #a3e635;
      white-space: nowrap;
    }
  </style>
</head>
<body>
  <header>
    <div>Android <strong id="serial"></strong></div>
    <div class="status" id="status">connecting</div>
  </header>
  <main>
    <video id="video" autoplay playsinline muted></video>
  </main>
  <script>
    const video = document.getElementById('video');
    const statusEl = document.getElementById('status');
    const serialEl = document.getElementById('serial');
    let activePointer = null;

    function clamp(value, min, max) {
      return Math.min(max, Math.max(min, value));
    }

    function eventToVideoPoint(event) {
      const box = video.getBoundingClientRect();
      const videoWidth = video.videoWidth || box.width;
      const videoHeight = video.videoHeight || box.height;
      if (!box.width || !box.height || !videoWidth || !videoHeight) return null;

      let contentLeft = box.left;
      let contentTop = box.top;
      let contentWidth = box.width;
      let contentHeight = box.height;
      const boxRatio = box.width / box.height;
      const videoRatio = videoWidth / videoHeight;
      if (boxRatio > videoRatio) {
        contentWidth = box.height * videoRatio;
        contentLeft += (box.width - contentWidth) / 2;
      } else if (boxRatio < videoRatio) {
        contentHeight = box.width / videoRatio;
        contentTop += (box.height - contentHeight) / 2;
      }

      const x = (event.clientX - contentLeft) / contentWidth;
      const y = (event.clientY - contentTop) / contentHeight;
      if (x < 0 || x > 1 || y < 0 || y > 1) return null;
      return { x: clamp(x, 0, 1), y: clamp(y, 0, 1) };
    }

    async function sendInput(payload) {
      const response = await fetch('/input', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        statusEl.textContent = await response.text();
        return;
      }
      const result = await response.json();
      statusEl.textContent = result.action || 'input';
    }

    video.addEventListener('pointerdown', (event) => {
      const point = eventToVideoPoint(event);
      if (!point) return;
      event.preventDefault();
      video.setPointerCapture(event.pointerId);
      activePointer = {
        id: event.pointerId,
        x: point.x,
        y: point.y,
        clientX: event.clientX,
        clientY: event.clientY,
        startedAt: performance.now(),
      };
    });

    video.addEventListener('pointerup', (event) => {
      if (!activePointer || activePointer.id !== event.pointerId) return;
      const startPoint = activePointer;
      activePointer = null;
      const endPoint = eventToVideoPoint(event);
      if (!endPoint) return;
      event.preventDefault();
      const durationMs = Math.round(performance.now() - startPoint.startedAt);
      const dx = event.clientX - startPoint.clientX;
      const dy = event.clientY - startPoint.clientY;
      const movedPx = Math.hypot(dx, dy);
      if (movedPx < 8 && durationMs < 450) {
        sendInput({ type: 'tap', x: startPoint.x, y: startPoint.y });
      } else {
        sendInput({
          type: 'swipe',
          startX: startPoint.x,
          startY: startPoint.y,
          endX: endPoint.x,
          endY: endPoint.y,
          durationMs,
        });
      }
    });

    video.addEventListener('pointercancel', (event) => {
      if (activePointer && activePointer.id === event.pointerId) activePointer = null;
    });

    video.addEventListener('contextmenu', (event) => event.preventDefault());

    async function start() {
      const meta = await fetch('/meta').then((r) => r.json());
      serialEl.textContent = meta.serial;
      const pc = new RTCPeerConnection();
      pc.addTransceiver('video', { direction: 'recvonly' });
      pc.ontrack = (event) => {
        video.srcObject = event.streams[0];
        statusEl.textContent = 'live';
      };
      pc.onconnectionstatechange = () => {
        statusEl.textContent = pc.connectionState;
      };
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await new Promise((resolve) => {
        if (pc.iceGatheringState === 'complete') resolve();
        else pc.onicegatheringstatechange = () => {
          if (pc.iceGatheringState === 'complete') resolve();
        };
      });
      const response = await fetch('/offer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(pc.localDescription),
      });
      if (!response.ok) {
        statusEl.textContent = await response.text();
        return;
      }
      const answer = await response.json();
      await pc.setRemoteDescription(answer);
      window.androidPeerConnection = pc;
    }

    start().catch((error) => {
      statusEl.textContent = error.message || String(error);
      console.error(error);
    });
  </script>
</body>
</html>
"""


class LowLatencyVideoTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, source: MediaStreamTrack) -> None:
        super().__init__()
        self.source = source

    async def recv(self) -> Any:
        frame = await self.source.recv()
        queue = getattr(self.source, "_queue", None)
        if queue is not None:
            while True:
                try:
                    newer = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if newer is None:
                    self.stop()
                    raise MediaStreamError
                frame = newer
        return frame

    def stop(self) -> None:
        super().stop()
        self.source.stop()


class AndroidWebRTCSource:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.tmpdir = Path(tempfile.mkdtemp(prefix="android-use-webrtc-"))
        self.fifo_path = self.tmpdir / "scrcpy.mkv"
        self.scrcpy_process: subprocess.Popen[bytes] | None = None
        self.player: MediaPlayer | None = None
        self.video_track: LowLatencyVideoTrack | None = None

    def is_running(self) -> bool:
        return (
            self.scrcpy_process is not None
            and self.scrcpy_process.poll() is None
            and self.video_track is not None
            and self.video_track.readyState != "ended"
        )

    def reset_fifo(self) -> None:
        if self.fifo_path.exists():
            self.fifo_path.unlink()
        if not self.tmpdir.exists():
            self.tmpdir = Path(tempfile.mkdtemp(prefix="android-use-webrtc-"))
            self.fifo_path = self.tmpdir / "scrcpy.mkv"
        os.mkfifo(self.fifo_path)

    async def start(self) -> LowLatencyVideoTrack:
        if self.is_running():
            return self.video_track
        if self.video_track or self.scrcpy_process:
            await self.stop()
        self.reset_fifo()
        command = [
            self.args.scrcpy,
            "--serial",
            self.args.serial,
            "--no-window",
            "--no-audio",
            "--record",
            str(self.fifo_path),
            "--record-format",
            "mkv",
            "--video-codec",
            "h264",
            "--video-bit-rate",
            self.args.bit_rate,
            "--max-size",
            str(self.args.max_size),
            "--max-fps",
            str(self.args.max_fps),
        ]
        self.scrcpy_process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
            start_new_session=True,
        )
        await asyncio.sleep(0.35)
        if self.scrcpy_process.poll() is not None:
            output = b""
            if self.scrcpy_process.stdout:
                output = self.scrcpy_process.stdout.read() or b""
            raise RuntimeError(output.decode("utf-8", errors="replace") or "scrcpy exited")
        self.player = MediaPlayer(
            str(self.fifo_path),
            format="matroska",
            options={
                "fflags": "nobuffer",
                "flags": "low_delay",
                "analyzeduration": "0",
                "probesize": "32768",
            },
        )
        self.player._throttle_playback = False
        if self.player.video is None:
            raise RuntimeError("No video track available from scrcpy")
        self.video_track = LowLatencyVideoTrack(self.player.video)
        return self.video_track

    async def stop(self) -> None:
        if self.video_track:
            self.video_track.stop()
            self.video_track = None
        if self.player:
            if self.player.video:
                self.player.video.stop()
            if self.player.audio:
                self.player.audio.stop()
            self.player = None
        if self.scrcpy_process and self.scrcpy_process.poll() is None:
            self.scrcpy_process.terminate()
            try:
                self.scrcpy_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.scrcpy_process.kill()
        self.scrcpy_process = None
        shutil.rmtree(self.tmpdir, ignore_errors=True)


async def index(_request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html")


async def meta(request: web.Request) -> web.Response:
    return web.json_response({"serial": request.app["serial"]})


def clamp_float(value: Any, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, float(value)))


def device_size(args: argparse.Namespace) -> tuple[int, int]:
    display_result = subprocess.run(
        [args.adb, "-s", args.serial, "shell", "dumpsys", "display"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
    )
    match = re.search(r"mOverrideDisplayInfo=.*?\breal\s+(\d+)\s+x\s+(\d+)", display_result.stdout)
    if match:
        return int(match.group(1)), int(match.group(2))

    window_result = subprocess.run(
        [args.adb, "-s", args.serial, "shell", "dumpsys", "window"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
    )
    match = re.search(r"DisplayFrames\s+w=(\d+)\s+h=(\d+)", window_result.stdout)
    if match:
        return int(match.group(1)), int(match.group(2))

    result = subprocess.run(
        [args.adb, "-s", args.serial, "shell", "wm", "size"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
    )
    match = re.search(r"Physical size:\s*(\d+)x(\d+)", result.stdout)
    if not match:
        match = re.search(r"Override size:\s*(\d+)x(\d+)", result.stdout)
    if not match:
        raise RuntimeError(f"Could not read device size: {result.stdout.strip()}")
    return int(match.group(1)), int(match.group(2))


def normalized_to_device(args: argparse.Namespace, x: Any, y: Any) -> tuple[int, int]:
    width, height = device_size(args)
    nx = clamp_float(x)
    ny = clamp_float(y)
    return round(nx * (width - 1)), round(ny * (height - 1))


def run_adb_input(args: argparse.Namespace, payload: dict[str, Any]) -> dict[str, Any]:
    action_type = payload.get("type")
    if action_type == "tap":
        x, y = normalized_to_device(args, payload["x"], payload["y"])
        command = [args.adb, "-s", args.serial, "shell", "input", "tap", str(x), str(y)]
        action = "tap"
    elif action_type == "swipe":
        start_x, start_y = normalized_to_device(args, payload["startX"], payload["startY"])
        end_x, end_y = normalized_to_device(args, payload["endX"], payload["endY"])
        duration_ms = round(max(50, min(2000, float(payload.get("durationMs", 300)))))
        command = [
            args.adb,
            "-s",
            args.serial,
            "shell",
            "input",
            "swipe",
            str(start_x),
            str(start_y),
            str(end_x),
            str(end_y),
            str(duration_ms),
        ]
        action = "swipe"
        x, y = end_x, end_y
    else:
        raise RuntimeError(f"Unsupported input type: {action_type}")

    subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    return {"ok": True, "action": action, "x": x, "y": y}


async def input_event(request: web.Request) -> web.Response:
    payload = await request.json()
    args: argparse.Namespace = request.app["args"]
    try:
        result = await asyncio.to_thread(run_adb_input, args, payload)
    except Exception as exc:
        return web.Response(text=str(exc), status=400)
    return web.json_response(result)


async def offer(request: web.Request) -> web.Response:
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
    pc = RTCPeerConnection()
    request.app["pcs"].add(pc)

    @pc.on("connectionstatechange")
    async def on_connectionstatechange() -> None:
        if pc.connectionState in {"failed", "closed", "disconnected"}:
            await pc.close()
            request.app["pcs"].discard(pc)

    source: AndroidWebRTCSource = request.app["source"]
    video = await source.start()
    pc.addTrack(request.app["relay"].subscribe(video))

    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})


async def on_shutdown(app: web.Application) -> None:
    pcs = list(app["pcs"])
    await asyncio.gather(*(pc.close() for pc in pcs), return_exceptions=True)
    app["pcs"].clear()
    await app["source"].stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve Android screen over local WebRTC.")
    parser.add_argument("--serial", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--adb", required=True)
    parser.add_argument("--scrcpy", required=True)
    parser.add_argument("--max-size", type=int, default=960)
    parser.add_argument("--bit-rate", default="4M")
    parser.add_argument("--max-fps", type=int, default=30)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app = web.Application()
    app["args"] = args
    app["serial"] = args.serial
    app["pcs"] = set()
    app["source"] = AndroidWebRTCSource(args)
    app["relay"] = MediaRelay()
    app.router.add_get("/", index)
    app.router.add_get("/meta", meta)
    app.router.add_post("/input", input_event)
    app.router.add_post("/offer", offer)
    app.on_shutdown.append(on_shutdown)

    loop = asyncio.get_event_loop()
    for signame in ("SIGTERM", "SIGINT"):
        loop.add_signal_handler(getattr(signal, signame), lambda: asyncio.ensure_future(app.shutdown()))
    web.run_app(app, host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
