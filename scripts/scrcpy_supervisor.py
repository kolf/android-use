#!/usr/bin/env python3
"""Keep scrcpy alive through startup flakiness, but respect manual window closes."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path


STOPPING = False
STOP_REQUESTED_AT: float | None = None
CHILD: subprocess.Popen[bytes] | None = None


def write_ready_file(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2))
    tmp_path.replace(path)


def write_user_closed_file(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2))
    tmp_path.replace(path)


def terminate_child(signum: int = signal.SIGTERM) -> None:
    global CHILD
    if CHILD is None or CHILD.poll() is not None:
        return
    try:
        os.killpg(CHILD.pid, signum)
    except ProcessLookupError:
        return
    except Exception:
        CHILD.terminate()


def handle_stop(signum: int, _frame: object) -> None:
    global STOPPING, STOP_REQUESTED_AT
    STOPPING = True
    STOP_REQUESTED_AT = time.monotonic()
    terminate_child(signum)


def run_child(command: list[str], args: argparse.Namespace) -> tuple[int, float]:
    global CHILD
    print(f"scrcpy-supervisor: starting {' '.join(shlex.quote(part) for part in command)}", flush=True)
    CHILD = subprocess.Popen(command, env=os.environ.copy(), start_new_session=True)
    started_at = time.monotonic()
    ready_written = False
    while True:
        if STOPPING:
            if STOP_REQUESTED_AT is not None and time.monotonic() - STOP_REQUESTED_AT > 2.5:
                terminate_child(signal.SIGKILL)
            else:
                terminate_child()
        returncode = CHILD.poll()
        runtime = time.monotonic() - started_at
        if returncode is not None:
            print(
                f"scrcpy-supervisor: child pid={CHILD.pid} exited rc={returncode} runtime={runtime:.2f}s",
                flush=True,
            )
            return returncode, runtime
        if not ready_written and args.ready_file and runtime >= args.ready_after_sec:
            write_ready_file(
                Path(args.ready_file),
                {
                    "pid": CHILD.pid,
                    "started_at": time.time(),
                    "command": command,
                },
            )
            ready_written = True
        time.sleep(0.1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Restart scrcpy when it exits unexpectedly.")
    parser.add_argument("--ready-file")
    parser.add_argument("--ready-after-sec", type=float, default=0.8)
    parser.add_argument("--early-exit-sec", type=float, default=2.0)
    parser.add_argument("--manual-exit-after-sec", type=float, default=2.0)
    parser.add_argument("--max-early-restarts", type=int, default=3)
    parser.add_argument("--restart-delay-sec", type=float, default=0.7)
    parser.add_argument("--user-closed-file")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("missing scrcpy command after --")
    return args


def main() -> int:
    for signame in ("SIGTERM", "SIGINT"):
        signal.signal(getattr(signal, signame), handle_stop)

    args = parse_args()
    early_restarts = 0
    while not STOPPING:
        returncode, runtime = run_child(args.command, args)
        if STOPPING:
            break
        if args.manual_exit_after_sec >= 0 and runtime >= args.manual_exit_after_sec:
            if args.user_closed_file:
                write_user_closed_file(
                    Path(args.user_closed_file),
                    {
                        "closed_at": time.time(),
                        "runtime_sec": runtime,
                        "returncode": returncode,
                        "command": args.command,
                    },
                )
            print(
                "scrcpy-supervisor: child exited after manual-close threshold; not restarting",
                flush=True,
            )
            return returncode or 0
        if runtime < args.early_exit_sec:
            early_restarts += 1
            if 0 <= args.max_early_restarts < early_restarts:
                print(
                    "scrcpy-supervisor: too many early exits; giving up",
                    file=sys.stderr,
                    flush=True,
                )
                return returncode or 1
        else:
            early_restarts = 0
        time.sleep(max(0.0, args.restart_delay_sec))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
