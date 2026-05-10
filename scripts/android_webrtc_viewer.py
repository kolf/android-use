#!/usr/bin/env python3
"""Serve an Android screen through a local WebRTC page.

The video source is scrcpy recording to a Matroska FIFO. aiortc/PyAV read that
live stream and publish it to the browser as a WebRTC video track.
"""

from __future__ import annotations

import argparse
import asyncio
import os
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

    async def start(self) -> LowLatencyVideoTrack:
        if self.video_track:
            return self.video_track
        os.mkfifo(self.fifo_path)
        command = [
            self.args.scrcpy,
            "--serial",
            self.args.serial,
            "--no-playback",
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
    app["serial"] = args.serial
    app["pcs"] = set()
    app["source"] = AndroidWebRTCSource(args)
    app["relay"] = MediaRelay()
    app.router.add_get("/", index)
    app.router.add_get("/meta", meta)
    app.router.add_post("/offer", offer)
    app.on_shutdown.append(on_shutdown)

    loop = asyncio.get_event_loop()
    for signame in ("SIGTERM", "SIGINT"):
        loop.add_signal_handler(getattr(signal, signame), lambda: asyncio.ensure_future(app.shutdown()))
    web.run_app(app, host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
