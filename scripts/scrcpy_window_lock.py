#!/usr/bin/env python3
"""Keep a scrcpy macOS window at a fixed size while preserving dragging."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time


APPLESCRIPT = r'''
on run argv
  set processName to item 1 of argv
  set windowTitle to item 2 of argv
  set targetWidth to (item 3 of argv) as integer
  set targetHeight to (item 4 of argv) as integer
  tell application "System Events"
    if not (exists process processName) then
      return "process-not-found"
    end if
    tell process processName
      if exists window windowTitle then
        set size of window windowTitle to {targetWidth, targetHeight}
        return "ok"
      end if
      if (count of windows) is greater than 0 then
        set size of window 1 to {targetWidth, targetHeight}
        return "ok-window-1"
      end if
    end tell
  end tell
  return "window-not-found"
end run
'''


def lock_once(process_name: str, window_title: str, width: int, height: int) -> str:
    result = subprocess.run(
        [
            "osascript",
            "-e",
            APPLESCRIPT,
            process_name,
            window_title,
            str(width),
            str(height),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or f"osascript exited with {result.returncode}")
    return result.stdout.strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lock a scrcpy window size on macOS.")
    parser.add_argument("--process-name", default="scrcpy")
    parser.add_argument("--window-title", required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--interval-sec", type=float, default=0.25)
    parser.add_argument(
        "--max-successes",
        type=int,
        default=0,
        help="Stop after this many successful size applications. 0 means run forever.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    failures = 0
    print(
        f"locking {args.process_name!r} window {args.window_title!r} to {args.width}x{args.height}",
        flush=True,
    )
    successes = 0
    while True:
        try:
            status = lock_once(args.process_name, args.window_title, args.width, args.height)
            failures = 0
            if status == "process-not-found":
                print(status, flush=True)
                return 0
            successes += 1
            if args.max_successes > 0 and successes >= args.max_successes:
                print(f"max-successes-reached:{successes}", flush=True)
                return 0
        except Exception as exc:
            failures += 1
            print(f"window-lock-error: {exc}", file=sys.stderr, flush=True)
            if failures >= 3:
                return 1
        time.sleep(max(0.05, args.interval_sec))


if __name__ == "__main__":
    raise SystemExit(main())
