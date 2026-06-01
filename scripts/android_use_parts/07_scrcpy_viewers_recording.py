# Loaded by scripts/android_use_mcp.py. Keep this file below 2000 lines.

def tool_index_source(args: dict[str, Any]) -> list[dict[str, Any]]:
    source_path = Path(str(args["source_path"])).expanduser()
    max_files = max(1, min(int(args.get("max_files", 2000)), 20000))
    app_map = index_source_tree(source_path, max_files=max_files)
    default_name = f"app-map-{slugify(source_path.name or 'source')}.json"
    output_path = Path(args["output_path"]).expanduser() if args.get("output_path") else SOURCE_MAP_DIR / default_name
    write_json(output_path, app_map)
    return [
        text_content(
            {
                "ok": True,
                "source_path": str(source_path),
                "app_map_path": str(output_path),
                "files_scanned": app_map["files_scanned"],
                "files_indexed": app_map["files_indexed"],
                "controls": len(app_map["controls"]),
            }
        )
    ]


def clean_device_title(value: str | None) -> str | None:
    cleaned = str(value or "").strip()
    if not cleaned or cleaned.lower() == "null":
        return None
    return cleaned


def get_android_setting(serial: str, namespace: str, key: str) -> str | None:
    try:
        return shell(serial, f"settings get {namespace} {key}", timeout=5)
    except AndroidUseError:
        return None


def android_device_display_name(serial: str) -> str:
    candidates = [
        clean_device_title(get_android_setting(serial, "global", "device_name")),
        clean_device_title(get_prop(serial, "ro.product.model")),
    ]
    for candidate in candidates:
        if candidate:
            return candidate
    return "Android"


def build_scrcpy_command(args: dict[str, Any], serial: str) -> tuple[list[str], int | None, int | None, str]:
    command = [scrcpy_binary(), "--serial", serial]
    max_size = int(args.get("max_size", 1280))
    window_width = args.get("window_width")
    window_height = args.get("window_height")
    window_scale = args.get("window_scale")
    window_title = str(args.get("window_title") or android_device_display_name(serial))
    keyboard_mode = str(args.get("keyboard") or "sdk").strip().lower()
    if keyboard_mode not in {"disabled", "sdk", "uhid", "aoa"}:
        raise AndroidUseError("keyboard must be one of: disabled, sdk, uhid, aoa.")
    if window_scale is not None:
        try:
            window_scale = float(window_scale)
        except (TypeError, ValueError) as exc:
            raise AndroidUseError("window_scale must be a positive number.") from exc
        if window_scale <= 0:
            raise AndroidUseError("window_scale must be a positive number.")
    command.extend(["--window-title", window_title])
    command.extend(["--keyboard", keyboard_mode])
    if args.get("prefer_text", True) and keyboard_mode == "sdk":
        command.append("--prefer-text")
    if args.get("legacy_paste", False):
        command.append("--legacy-paste")
    if not args.get("audio", False):
        command.append("--no-audio")
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
                if window_scale is not None:
                    scale *= window_scale
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
    return command, int(window_width) if window_width else None, int(window_height) if window_height else None, window_title


def macos_app_name(value: str | None) -> str:
    cleaned = re.sub(r"[\0/:]+", "-", str(value or "").strip()).strip(" .")
    return cleaned[:80] or "Android"


def system_android_launcher_app_path() -> Path:
    configured = os.environ.get("ANDROID_USE_SYSTEM_ANDROID_APP_PATH")
    if configured:
        return Path(configured).expanduser()
    applications_dir = Path(os.environ.get("ANDROID_USE_SYSTEM_APPLICATIONS_DIR", "/Applications")).expanduser()
    return applications_dir / "Android Use.app"


def ensure_system_android_launcher_app() -> dict[str, Any]:
    if sys.platform != "darwin":
        return {"ok": True, "skipped": "not-macos"}
    if not env_flag("ANDROID_USE_SYSTEM_ANDROID_APP", True):
        return {"ok": True, "skipped": "disabled-by-env"}
    app_path = system_android_launcher_app_path()
    if app_path.exists():
        return {"ok": True, "skipped": "already-present", "app_path": str(app_path)}
    try:
        built_path = build_android_scrcpy_app(app_path, "Android Use")
    except Exception as exc:
        return {"ok": False, "app_path": str(app_path), "error": str(exc)}
    return {"ok": True, "created": True, "app_path": str(built_path)}


def android_scrcpy_app_path(args: dict[str, Any] | None = None, app_name: str | None = None) -> Path:
    args = args or {}
    if args.get("app_path"):
        return Path(str(args["app_path"])).expanduser()
    configured = os.environ.get("ANDROID_USE_SCRCPY_APP_PATH")
    if configured:
        return Path(configured).expanduser()
    return ANDROID_USE_DIR / "Android Use.app"


def using_default_android_scrcpy_app_path(args: dict[str, Any] | None = None) -> bool:
    args = args or {}
    return not args.get("app_path") and not os.environ.get("ANDROID_USE_SCRCPY_APP_PATH")


def cleanup_legacy_android_scrcpy_apps(current_app_path: Path) -> list[str]:
    if current_app_path.parent != ANDROID_USE_DIR:
        return []
    removed: list[str] = []
    for app_dir in ANDROID_USE_DIR.glob("*.app"):
        if app_dir == current_app_path or not app_dir.is_dir():
            continue
        info_path = app_dir / "Contents" / "Info.plist"
        if not info_path.exists():
            continue
        try:
            with info_path.open("rb") as file:
                plist = plistlib.load(file)
        except Exception:
            continue
        if plist.get("CFBundleIdentifier") != ANDROID_USE_BUNDLE_ID:
            continue
        shutil.rmtree(app_dir)
        removed.append(str(app_dir))
    return removed


def build_android_scrcpy_app(app_path: Path, app_name: str = "Android") -> Path:
    if sys.platform != "darwin":
        raise AndroidUseError("Android Use macOS app launch is only available on macOS.")
    builder = PLUGIN_ROOT / "scripts" / "build_android_scrcpy_app.sh"
    if not builder.exists():
        raise AndroidUseError(f"Android Use app builder not found: {builder}")
    stdout, _stderr = run_command([str(builder), str(app_path), macos_app_name(app_name)], timeout=30)
    built_path = Path(decode_bytes(stdout).splitlines()[-1]).expanduser()
    return built_path


def build_scrcpy_app_launch_command(command: list[str], app_path: Path) -> list[str]:
    return ["open", "-n", str(app_path), "--args", "--scrcpy", command[0], *command[1:]]


def launch_android_scrcpy_app(command: list[str], app_path: Path) -> subprocess.CompletedProcess[bytes]:
    launch_command = build_scrcpy_app_launch_command(command, app_path)
    try:
        return subprocess.run(
            launch_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=8,
            check=False,
            env=tool_env(),
        )
    except FileNotFoundError as exc:
        raise AndroidUseError("Command not found: open") from exc
    except subprocess.TimeoutExpired as exc:
        raise AndroidUseError("Timed out launching Android Use macOS app.") from exc


def latest_scrcpy_visible_process(serial: str) -> dict[str, Any] | None:
    rows = scrcpy_app_wrapper_processes_for_serial(serial)
    if not rows:
        return None
    rows.sort(key=lambda row: int(row.get("pid") or 0), reverse=True)
    return rows[0]


def start_scrcpy_app_window(args: dict[str, Any], serial: str) -> dict[str, Any]:
    child_args = dict(args)
    child_args.setdefault("window_title", android_device_display_name(serial))
    app_name = "Android Use"
    app_path = android_scrcpy_app_path(args, app_name)
    removed_legacy_apps = cleanup_legacy_android_scrcpy_apps(app_path) if using_default_android_scrcpy_app_path(args) else []
    app_path = build_android_scrcpy_app(app_path, app_name)
    child_args.setdefault("max_size", 0)
    child_args.setdefault("window_scale", 0.5)
    child_args.setdefault("render_driver", "software")
    extra_args = list(child_args.get("extra_args") or [])
    if child_args.get("render_driver") and "--render-driver" not in extra_args:
        extra_args.extend(["--render-driver", str(child_args["render_driver"])])
    child_args["extra_args"] = extra_args
    child_args.setdefault("fixed_window", True)
    command, _window_width, _window_height, window_title = build_scrcpy_command(child_args, serial)
    launch_command = build_scrcpy_app_launch_command(command, app_path)
    result = launch_android_scrcpy_app(command, app_path)
    if result.returncode != 0:
        stderr = decode_bytes(result.stderr)
        stdout = decode_bytes(result.stdout)
        raise AndroidUseError(f"Android Use macOS app failed to launch.\n{stderr or stdout}")
    time.sleep(0.8)
    visible = latest_scrcpy_visible_process(serial)
    return {
        "ok": True,
        "serial": serial,
        "pid": visible.get("pid") if visible else None,
        "scrcpy_pid": visible.get("pid") if visible else None,
        "app_path": str(app_path),
        "bundle_id": ANDROID_USE_BUNDLE_ID,
        "app_name": app_name,
        "window_title": window_title,
        "removed_legacy_apps": removed_legacy_apps,
        "command": command,
        "launch_command": launch_command,
        "keep_alive": False,
        "launch_mode": "macos_app",
        "display": "scrcpy is running through the native macOS app wrapper.",
    }


def tool_start_scrcpy_app(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    system_app = ensure_system_android_launcher_app()
    if not bool(args.get("force", False)):
        duplicate_pids = prune_duplicate_scrcpy_processes(serial)
        existing = scrcpy_app_wrapper_process_for_serial(serial)
        if existing:
            clear_scrcpy_user_closed(serial)
            return [
                text_content(
                    {
                        "ok": True,
                        "serial": serial,
                        "skipped": "already-running",
                        "process": existing[:500],
                        "stopped_duplicate_pids": duplicate_pids,
                        "launch_mode": "macos_app",
                        "system_app": system_app,
                        "display": "scrcpy app-wrapper window is already running for this device.",
                    }
                )
            ]
    clear_scrcpy_user_closed(serial)
    payload = start_scrcpy_app_window(args, serial)
    payload["system_app"] = system_app
    return [
        text_content(payload)
    ]


def host_process_lines() -> list[str]:
    try:
        stdout, _stderr = run_command(["ps", "-axo", "pid=,command="], timeout=3)
    except AndroidUseError:
        return []
    return [line.strip() for line in decode_bytes(stdout).splitlines() if line.strip()]


def host_process_rows() -> list[dict[str, Any]]:
    try:
        stdout, _stderr = run_command(["ps", "-axo", "pid=,ppid=,command="], timeout=3)
    except AndroidUseError:
        return []
    rows: list[dict[str, Any]] = []
    for raw_line in decode_bytes(stdout).splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)", raw_line)
        if match:
            rows.append({"pid": int(match.group(1)), "ppid": int(match.group(2)), "command": match.group(3).strip()})
    return rows


def is_visible_scrcpy_command(command: str, serial: str) -> bool:
    if serial not in command:
        return False
    is_supervisor = "scrcpy_supervisor.py" in command
    is_scrcpy_binary = re.search(r"(^|\s)(?:\S*/)?scrcpy(\s|$)", command) is not None
    if not is_supervisor and not is_scrcpy_binary:
        return False
    if "--no-window" in command:
        return False
    return True


def scrcpy_visible_processes_for_serial(serial: str) -> list[dict[str, Any]]:
    return [row for row in host_process_rows() if is_visible_scrcpy_command(str(row.get("command") or ""), serial)]


def scrcpy_visible_process_for_serial(serial: str) -> str | None:
    for line in host_process_lines():
        if serial not in line:
            continue
        if is_visible_scrcpy_command(line, serial):
            return line
    return None


def macos_bundle_identifier_for_pid(pid: int) -> str | None:
    if sys.platform != "darwin":
        return None
    script = f'tell application "System Events" to get bundle identifier of first application process whose unix id is {int(pid)}'
    try:
        stdout, _stderr = run_command(["osascript", "-e", script], timeout=3)
    except AndroidUseError:
        return None
    bundle_id = decode_bytes(stdout).strip()
    if not bundle_id or bundle_id == "missing value":
        return None
    return bundle_id


def is_android_use_app_process(row: dict[str, Any]) -> bool:
    try:
        pid = int(row.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    return macos_bundle_identifier_for_pid(pid) == ANDROID_USE_BUNDLE_ID


def scrcpy_app_wrapper_processes_for_serial(serial: str) -> list[dict[str, Any]]:
    return [row for row in scrcpy_visible_processes_for_serial(serial) if is_android_use_app_process(row)]


def stale_android_use_scrcpy_processes() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in host_process_rows():
        command = str(row.get("command") or "")
        is_scrcpy_binary = re.search(r"(^|\s)(?:\S*/)?scrcpy(\s|$)", command) is not None
        if not is_scrcpy_binary or "--serial" in command or "-s " in command:
            continue
        if "--no-window" in command:
            continue
        if is_android_use_app_process(row):
            rows.append(row)
    return rows


def scrcpy_app_wrapper_process_for_serial(serial: str) -> str | None:
    rows = scrcpy_app_wrapper_processes_for_serial(serial)
    if not rows:
        return None
    rows.sort(key=lambda row: int(row.get("pid") or 0), reverse=True)
    row = rows[0]
    return f"{row.get('pid')} {row.get('command')}"


def stop_host_process_rows(rows: Iterable[dict[str, Any]]) -> list[int]:
    stopped: list[int] = []
    for row in rows:
        try:
            pid = int(row.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, 15)
            stopped.append(pid)
    if stopped:
        time.sleep(0.2)
    return stopped


def prune_duplicate_scrcpy_processes(serial: str) -> list[int]:
    stopped_stale = stop_host_process_rows(stale_android_use_scrcpy_processes())
    rows = scrcpy_visible_processes_for_serial(serial)
    if not rows:
        return stopped_stale
    app_rows = [row for row in rows if is_android_use_app_process(row)]
    if not app_rows:
        return stopped_stale + stop_host_process_rows(rows)
    app_rows.sort(key=lambda row: int(row.get("pid") or 0), reverse=True)
    keep_pid = int(app_rows[0].get("pid") or 0)
    return stopped_stale + stop_host_process_rows(row for row in rows if int(row.get("pid") or 0) != keep_pid)


def ensure_default_scrcpy_window(serial: str, args: dict[str, Any]) -> dict[str, Any]:
    if not bool(args.get("show_scrcpy", True)):
        return {"ok": True, "skipped": "disabled"}
    launch_lock = lock_path(f"scrcpy-launch-{serial}")
    with exclusive_file_lock(launch_lock, blocking=True) as locked:
        if not locked:
            return {"ok": False, "error": "could not acquire scrcpy launch lock"}
        duplicate_pids = prune_duplicate_scrcpy_processes(serial)
        existing = scrcpy_app_wrapper_process_for_serial(serial)
        if existing:
            if not bool(args.get("respect_manual_close", False)):
                clear_scrcpy_user_closed(serial)
            return {
                "ok": True,
                "skipped": "already-running",
                "process": existing[:500],
                "stopped_duplicate_pids": duplicate_pids,
                "launch_lock": str(launch_lock),
            }
        user_closed = read_scrcpy_user_closed(serial)
        if user_closed and bool(args.get("respect_manual_close", False)):
            return {
                "ok": True,
                "skipped": "user-closed",
                "user_closed": user_closed,
                "launch_lock": str(launch_lock),
            }
        clear_scrcpy_user_closed(serial)
        try:
            content = tool_start_scrcpy(
                {
                    "serial": serial,
                    "keep_alive": args.get("scrcpy_keep_alive", True),
                    "fixed_window": args.get("scrcpy_fixed_window", True),
                    "lock_window_size": args.get("scrcpy_lock_window_size", True),
                    "window_width": args.get("scrcpy_window_width"),
                    "window_height": args.get("scrcpy_window_height"),
                    "max_size": args.get("scrcpy_max_size", 0),
                    "window_scale": args.get("scrcpy_window_scale", 0.5),
                    "render_driver": args.get("scrcpy_render_driver", "software"),
                }
            )
            payload = content[0].get("text") if content else "{}"
            parsed = json.loads(str(payload)) if isinstance(payload, str) else payload
            if isinstance(parsed, dict):
                parsed["launch_lock"] = str(launch_lock)
                return parsed
            return {"ok": True, "result": parsed, "launch_lock": str(launch_lock)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "launch_lock": str(launch_lock)}


def resident_scrcpy_enabled() -> bool:
    return env_flag("ANDROID_USE_SCRCPY_RESIDENT", True)


def resident_scrcpy_interval_sec() -> float:
    return max(1.0, env_float("ANDROID_USE_SCRCPY_RESIDENT_INTERVAL_SEC", 2.0))


def connected_device_serials() -> list[str]:
    try:
        devices = [device for device in list_devices() if device.get("state") == "device"]
    except Exception:
        return []
    configured = os.environ.get("ANDROID_USE_SCRCPY_RESIDENT_SERIALS") or os.environ.get("ANDROID_USE_SERIAL") or os.environ.get("ANDROID_SERIAL")
    if configured:
        wanted = split_configured_values(configured)
        connected = {str(device.get("serial") or "") for device in devices}
        return [serial for serial in wanted if serial in connected]
    devices = dedupe_connected_devices(devices)
    physical = [str(device.get("serial") or "") for device in devices if not str(device.get("serial") or "").startswith("emulator-")]
    if physical:
        return physical[:1]
    return [str(devices[0]["serial"])] if devices else []


def ensure_resident_scrcpy_windows() -> dict[str, Any]:
    serials = connected_device_serials()
    ensured: list[dict[str, Any]] = []
    for serial in serials:
        result = ensure_default_scrcpy_window(
            serial,
            {
                "show_scrcpy": True,
                "scrcpy_keep_alive": True,
                "scrcpy_fixed_window": True,
                "scrcpy_lock_window_size": True,
                "scrcpy_max_size": 0,
                "scrcpy_window_scale": 0.5,
                "scrcpy_render_driver": "software",
                "respect_manual_close": True,
            },
        )
        ensured.append({"serial": serial, **result})
    return {"ok": True, "serials": serials, "ensured": ensured}


def update_resident_scrcpy_status(**updates: Any) -> None:
    with SCRCPY_RESIDENT_LOCK:
        SCRCPY_RESIDENT_STATUS.update(updates)


def try_acquire_resident_scrcpy_monitor_lock() -> tuple[bool, str]:
    global SCRCPY_RESIDENT_LOCK_HANDLE
    monitor_lock_path = lock_path("scrcpy-resident-monitor")
    if SCRCPY_RESIDENT_LOCK_HANDLE is not None:
        return True, str(monitor_lock_path)
    monitor_lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = monitor_lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return False, str(monitor_lock_path)
    SCRCPY_RESIDENT_LOCK_HANDLE = handle
    return True, str(monitor_lock_path)


def resident_scrcpy_loop() -> None:
    update_resident_scrcpy_status(enabled=True, running=True, last_error=None)
    while resident_scrcpy_enabled():
        try:
            result = ensure_resident_scrcpy_windows()
            update_resident_scrcpy_status(
                enabled=True,
                running=True,
                serials=result.get("serials", []),
                last_check_at=time.time(),
                last_result=result,
                last_error=None,
            )
        except Exception as exc:
            update_resident_scrcpy_status(
                enabled=True,
                running=True,
                last_check_at=time.time(),
                last_error=str(exc),
            )
        time.sleep(resident_scrcpy_interval_sec())
    update_resident_scrcpy_status(enabled=False, running=False)


def start_resident_scrcpy_monitor() -> dict[str, Any]:
    global SCRCPY_RESIDENT_THREAD
    if not resident_scrcpy_enabled():
        update_resident_scrcpy_status(enabled=False, running=False, last_error=None)
        return {"ok": True, "enabled": False, "skipped": "disabled-by-env"}
    with SCRCPY_RESIDENT_LOCK:
        if SCRCPY_RESIDENT_THREAD is not None and SCRCPY_RESIDENT_THREAD.is_alive():
            return {"ok": True, "enabled": True, "skipped": "already-running"}
        acquired, monitor_lock = try_acquire_resident_scrcpy_monitor_lock()
        if not acquired:
            SCRCPY_RESIDENT_STATUS.update(
                {
                    "enabled": True,
                    "running": False,
                    "last_error": None,
                    "monitor_lock": monitor_lock,
                    "skipped": "another-mcp-process-owns-monitor",
                }
            )
            return {"ok": True, "enabled": True, "skipped": "another-mcp-process-owns-monitor", "monitor_lock": monitor_lock}
        SCRCPY_RESIDENT_THREAD = threading.Thread(
            target=resident_scrcpy_loop,
            name="android-use-scrcpy-resident",
            daemon=True,
        )
        SCRCPY_RESIDENT_THREAD.start()
        return {"ok": True, "enabled": True, "started": True, "monitor_lock": monitor_lock}


def tool_scrcpy_resident_status(_args: dict[str, Any]) -> list[dict[str, Any]]:
    started = start_resident_scrcpy_monitor()
    with SCRCPY_RESIDENT_LOCK:
        status = dict(SCRCPY_RESIDENT_STATUS)
    return [text_content({"ok": True, "monitor": started, "status": status})]


def serials_from_arg(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return unique_ordered(str(item) for item in value)
    return unique_ordered(split_configured_values(str(value)))


def read_json_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def video_recording_state_path(serial: str) -> Path:
    return SCREEN_DIR / f"scrcpy-video-recording-{slugify(serial)}.json"


def video_recording_metadata_path(output_path: Path) -> Path:
    return output_path.with_suffix(output_path.suffix + ".json")


def video_recording_start_marker_path(output_path: Path) -> Path:
    return output_path.with_suffix(output_path.suffix + ".start.png")


def host_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def clear_video_recording_state(serial: str) -> None:
    SCRCPY_VIDEO_RECORDINGS.pop(serial, None)
    with contextlib.suppress(OSError):
        video_recording_state_path(serial).unlink()


def active_video_recording(serial: str) -> dict[str, Any] | None:
    recording = SCRCPY_VIDEO_RECORDINGS.get(serial)
    process = SCRCPY_VIDEO_RECORDING_PROCESSES.get(serial)
    if recording and process and process.poll() is None:
        return recording
    payload = read_json_file(video_recording_state_path(serial))
    if not payload:
        return None
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if host_pid_alive(pid):
        SCRCPY_VIDEO_RECORDINGS[serial] = payload
        return payload
    clear_video_recording_state(serial)
    return None


def update_video_recording_metadata(metadata_path: Path, updates: dict[str, Any]) -> None:
    payload = read_json_file(metadata_path)
    if not payload:
        payload = {}
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(payload.get(key), dict):
            merged = dict(payload[key])
            merged.update(value)
            payload[key] = merged
        else:
            payload[key] = value
    write_json(metadata_path, payload)


def capture_video_recording_start_marker(
    serial: str,
    marker_path: Path,
    metadata_path: Path,
    *,
    timeout_sec: float = 2.0,
) -> None:
    capture_started_epoch = time.time()
    capture_started_at = timestamp_iso()
    try:
        png = normalize_png_bytes(
            adb(["exec-out", "screencap", "-p"], serial=serial, timeout=timeout_sec)
        )
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_bytes(png)
        update_video_recording_metadata(
            metadata_path,
            {
                "start_anchor": {
                    "status": "captured",
                    "path": str(marker_path),
                    "captured_at": timestamp_iso(),
                    "captured_at_epoch": time.time(),
                    "capture_started_at": capture_started_at,
                    "capture_started_at_epoch": capture_started_epoch,
                    "size": png_size(png),
                }
            },
        )
    except Exception as exc:
        update_video_recording_metadata(
            metadata_path,
            {
                "start_anchor": {
                    "status": "error",
                    "path": str(marker_path),
                    "captured_at": timestamp_iso(),
                    "captured_at_epoch": time.time(),
                    "capture_started_at": capture_started_at,
                    "capture_started_at_epoch": capture_started_epoch,
                    "error": str(exc),
                }
            },
        )


def schedule_video_recording_start_marker(
    serial: str,
    marker_path: Path,
    metadata_path: Path,
    *,
    timeout_sec: float = 2.0,
) -> None:
    thread = threading.Thread(
        target=capture_video_recording_start_marker,
        args=(serial, marker_path, metadata_path),
        kwargs={"timeout_sec": timeout_sec},
        daemon=True,
    )
    thread.start()


def build_video_recording_command(args: dict[str, Any], serial: str, output_path: Path) -> list[str]:
    record_format = str(args.get("record_format") or "mp4").strip().lower()
    if record_format not in {"mp4", "mkv"}:
        raise AndroidUseError("record_format must be mp4 or mkv.")
    command = [
        scrcpy_binary(),
        "--serial",
        serial,
        "--no-window",
        "--record",
        str(output_path),
        "--record-format",
        record_format,
    ]
    max_size = int(args.get("max_size", 0) or 0)
    if max_size:
        command.extend(["-m", str(max_size)])
    bit_rate = args.get("bit_rate")
    if bit_rate:
        command.extend(["-b", str(bit_rate)])
    if not args.get("audio", False):
        command.append("--no-audio")
    extra_args = args.get("extra_args") or []
    if not isinstance(extra_args, list):
        raise AndroidUseError("extra_args must be a list of scrcpy command arguments.")
    command.extend(str(item) for item in extra_args)
    return command


def stop_video_recording_process(serial: str, recording: dict[str, Any], timeout_sec: float) -> bool:
    process = SCRCPY_VIDEO_RECORDING_PROCESSES.pop(serial, None)
    if process is not None:
        if process.poll() is not None:
            return False
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=timeout_sec)
            return True
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
            return True

    try:
        pid = int(recording.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0 or not host_pid_alive(pid):
        return False
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGINT)
    deadline = time.monotonic() + max(timeout_sec, 0.1)
    while time.monotonic() < deadline:
        if not host_pid_alive(pid):
            return True
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
    return True


def terminate_process(process: subprocess.Popen[bytes], *, timeout: float = 3) -> bool:
    if process.poll() is not None:
        return False
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        return True
    return True


def tool_start_video_recording(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    existing = active_video_recording(serial)
    if existing:
        existing_metadata_path = Path(str(existing.get("metadata_path") or "")).expanduser()
        existing_metadata = read_json_file(existing_metadata_path) if str(existing_metadata_path) != "." else {}
        return [
            text_content(
                {
                    "ok": True,
                    "serial": serial,
                    "skipped": "already-recording",
                    "pid": existing.get("pid"),
                    "file_path": existing.get("file_path"),
                    "started_at": existing.get("started_at"),
                    "metadata_path": existing.get("metadata_path"),
                    "start_anchor": existing_metadata.get("start_anchor") or existing.get("start_anchor"),
                    "timing": existing_metadata.get("timing") or existing.get("timing"),
                }
            )
        ]

    requested_at = timestamp_iso()
    requested_at_epoch = time.time()
    record_format = str(args.get("record_format") or "mp4").strip().lower()
    name = slugify(str(args.get("name") or "android-video"), "android-video")
    if args.get("output_path"):
        output_path = Path(str(args["output_path"])).expanduser()
    else:
        output_path = VIDEO_RECORDINGS_DIR / f"{time.strftime('%Y%m%d-%H%M%S', time.localtime())}-{name}.{record_format}"
    if output_path.suffix.lower() != f".{record_format}":
        output_path = output_path.with_suffix(f".{record_format}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = output_path.with_suffix(output_path.suffix + ".log")
    metadata_path = video_recording_metadata_path(output_path)
    marker_path = video_recording_start_marker_path(output_path)
    start_marker_enabled = args.get("start_marker", True) is not False
    command = build_video_recording_command(args, serial, output_path)
    try:
        with log_path.open("ab") as log_file:
            process = subprocess.Popen(
                command,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=tool_env(),
            )
    except FileNotFoundError as exc:
        raise AndroidUseError(f"Command not found: {command[0]}") from exc
    process_started_at = timestamp_iso()
    process_started_at_epoch = time.time()
    if process.poll() is not None:
        log_tail = ""
        with contextlib.suppress(OSError):
            log_tail = log_path.read_text(errors="replace")[-1200:]
        raise AndroidUseError(f"scrcpy video recording failed to start.\n{log_tail}".rstrip())

    returned_at = timestamp_iso()
    returned_at_epoch = time.time()
    timing = {
        "requested_at": requested_at,
        "requested_at_epoch": requested_at_epoch,
        "process_started_at": process_started_at,
        "process_started_at_epoch": process_started_at_epoch,
        "returned_at": returned_at,
        "returned_at_epoch": returned_at_epoch,
        "startup_probe_ms": round((returned_at_epoch - process_started_at_epoch) * 1000, 3),
    }
    start_anchor = {
        "enabled": start_marker_enabled,
        "status": "pending" if start_marker_enabled else "disabled",
        "kind": "screenshot",
        "path": str(marker_path) if start_marker_enabled else None,
        "description": (
            "Best-effort screen frame captured in the background immediately after "
            "the scrcpy recording process starts; the MP4 first encoded frame is "
            "controlled by scrcpy stream setup."
        ),
    }
    recording = {
        "ok": True,
        "serial": serial,
        "pid": process.pid,
        "file_path": str(output_path),
        "log_path": str(log_path),
        "metadata_path": str(metadata_path),
        "started_at": process_started_at,
        "started_at_epoch": process_started_at_epoch,
        "timing": timing,
        "start_anchor": start_anchor,
        "record_format": record_format,
        "command": command,
    }
    SCRCPY_VIDEO_RECORDING_PROCESSES[serial] = process
    SCRCPY_VIDEO_RECORDINGS[serial] = recording
    write_json(video_recording_state_path(serial), recording)
    write_json(metadata_path, recording)
    if start_marker_enabled:
        schedule_video_recording_start_marker(serial, marker_path, metadata_path)
    return [
        text_content(
            {
                **recording,
                "message": (
                    "scrcpy video recording started without a fixed startup wait. "
                    "Use start_anchor.path as the start-frame anchor and stop it "
                    "with android_stop_video_recording."
                ),
            }
        )
    ]


def tool_stop_video_recording(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    recording = active_video_recording(serial)
    if not recording:
        raise AndroidUseError(f"No active scrcpy video recording for device {serial}.")
    timeout_sec = max(0.1, min(float(args.get("timeout_sec", 5)), 30.0))
    stopped = stop_video_recording_process(serial, recording, timeout_sec)
    clear_video_recording_state(serial)
    output_path = Path(str(recording.get("file_path") or "")).expanduser()
    metadata_path = Path(str(recording.get("metadata_path") or video_recording_metadata_path(output_path))).expanduser()
    metadata = read_json_file(metadata_path)
    size_bytes = output_path.stat().st_size if output_path.exists() else 0
    duration_sec = round(time.time() - float(recording.get("started_at_epoch") or time.time()), 3)
    payload = {
        "ok": True,
        "serial": serial,
        "stopped": stopped,
        "pid": recording.get("pid"),
        "file_path": str(output_path),
        "log_path": recording.get("log_path"),
        "metadata_path": str(metadata_path),
        "size_bytes": size_bytes,
        "duration_sec": duration_sec,
        "markdown": f"![android-video-recording]({output_path})",
    }
    if metadata.get("start_anchor"):
        payload["start_anchor"] = metadata["start_anchor"]
    elif recording.get("start_anchor"):
        payload["start_anchor"] = recording["start_anchor"]
    if metadata.get("timing"):
        payload["timing"] = metadata["timing"]
    elif recording.get("timing"):
        payload["timing"] = recording["timing"]
    return [text_content(payload)]


def tool_start_scrcpy(args: dict[str, Any]) -> list[dict[str, Any]]:
    requested_serials = serials_from_arg(args.get("serials"))
    if requested_serials:
        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for requested_serial in requested_serials:
            child_args = {key: value for key, value in args.items() if key != "serials"}
            child_args["serial"] = requested_serial
            try:
                content = tool_start_scrcpy(child_args)
                payload = content[0].get("text") if content else "{}"
                parsed = json.loads(str(payload)) if isinstance(payload, str) else payload
                results.append(parsed if isinstance(parsed, dict) else {"result": parsed})
            except Exception as exc:
                errors.append({"serial": requested_serial, "error": str(exc)})
        if not results:
            detail = "\n".join(f"{item['serial']}: {item['error']}" for item in errors[-5:])
            raise AndroidUseError("scrcpy failed to start for all requested devices.\n" + detail)
        return [
            text_content(
                {
                    "ok": not errors,
                    "serials": requested_serials,
                    "results": results,
                    "errors": errors,
                }
            )
        ]

    serial = choose_serial(args.get("serial"))
    system_app = ensure_system_android_launcher_app()
    if not bool(args.get("force", False)):
        duplicate_pids = prune_duplicate_scrcpy_processes(serial)
        existing = scrcpy_app_wrapper_process_for_serial(serial)
        if existing:
            clear_scrcpy_user_closed(serial)
            return [
                text_content(
                    {
                        "ok": True,
                        "serial": serial,
                        "skipped": "already-running",
                        "process": existing[:500],
                        "stopped_duplicate_pids": duplicate_pids,
                        "launch_mode": "macos_app",
                        "system_app": system_app,
                        "display": "scrcpy app-wrapper window is already running for this device.",
                    }
                )
            ]
    clear_scrcpy_user_closed(serial)
    child_args = dict(args)
    child_args.setdefault("max_size", 0)
    child_args.setdefault("window_scale", 0.5)
    child_args.setdefault("render_driver", "software")
    payload = start_scrcpy_app_window(child_args, serial)
    payload["system_app"] = system_app
    payload["requested_keep_alive"] = bool(args.get("keep_alive", True))
    payload["keep_alive"] = False
    payload["keep_alive_note"] = "Visible scrcpy windows are always launched through the macOS app wrapper; resident/on-demand checks reopen the app when needed."
    return [text_content(payload)]


AUTO_SCRCPY_TOOL_NAMES = {
    "android_get_state",
    "android_screenshot",
    "android_show_screen",
    "android_appshot",
    "android_observe",
    "android_tap_text",
    "android_tap",
    "android_swipe",
    "android_type_text",
    "android_press_key",
    "android_wake_unlock",
    "android_open_url",
    "android_open_app",
    "android_webview_pages",
    "android_webview_eval",
    "android_start_recording",
    "android_record_checkpoint",
    "android_stop_recording",
    "android_replay_recipe",
}


def should_auto_show_scrcpy_for_tool(name: str, arguments: dict[str, Any]) -> bool:
    if not env_flag("ANDROID_USE_SCRCPY_ON_TOOL_CALL", True):
        return False
    if arguments.get("show_scrcpy") is False:
        return False
    if name.startswith("xiaoluxue_"):
        return True
    return name in AUTO_SCRCPY_TOOL_NAMES


def maybe_show_scrcpy_for_tool_call(name: str, arguments: dict[str, Any]) -> None:
    if not should_auto_show_scrcpy_for_tool(name, arguments):
        return
    try:
        serial = choose_serial(arguments.get("serial"))
        result = ensure_default_scrcpy_window(
            serial,
            {
                **arguments,
                "show_scrcpy": True,
                "respect_manual_close": False,
                "scrcpy_keep_alive": arguments.get("scrcpy_keep_alive", True),
                "scrcpy_fixed_window": arguments.get("scrcpy_fixed_window", True),
                "scrcpy_lock_window_size": arguments.get("scrcpy_lock_window_size", True),
                "scrcpy_max_size": arguments.get("scrcpy_max_size", 0),
                "scrcpy_window_scale": arguments.get("scrcpy_window_scale", 0.5),
                "scrcpy_render_driver": arguments.get("scrcpy_render_driver", "software"),
            },
        )
        update_resident_scrcpy_status(last_on_demand_result={"tool": name, "serial": serial, **result})
    except Exception as exc:
        update_resident_scrcpy_status(last_on_demand_error={"tool": name, "error": str(exc), "at": time.time()})


def tool_stop_scrcpy(args: dict[str, Any]) -> list[dict[str, Any]]:
    stopped: list[int] = []
    requested_pid = args.get("pid")
    if requested_pid:
        pids = [int(requested_pid)]
    elif args.get("all", True):
        pids = list(SCRCPY_PROCESSES)
        for row in host_process_rows():
            command = str(row.get("command") or "")
            is_visible = re.search(r"(^|\s)(?:\S*/)?scrcpy(\s|$)", command) is not None and "--no-window" not in command
            if is_visible:
                pids.append(int(row["pid"]))
    else:
        pids = []

    seen_pids: set[int] = set()
    for pid in pids:
        pid = int(pid)
        if pid in seen_pids:
            continue
        seen_pids.add(pid)
        process = SCRCPY_PROCESSES.pop(pid, None)
        if process and terminate_process(process):
            stopped.append(pid)
        elif not process:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, 15)
                stopped.append(pid)
    lock_stopped: list[int] = []
    for pid, process in list(SCRCPY_LOCK_PROCESSES.items()):
        if terminate_process(process):
            lock_stopped.append(pid)
        SCRCPY_LOCK_PROCESSES.pop(pid, None)
    return [text_content({"ok": True, "stopped_pids": stopped, "stopped_lock_pids": lock_stopped})]


def pick_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def screen_viewer_session_dir(serial: str, requested: str | None = None) -> Path:
    if requested:
        return Path(requested).expanduser()
    return SCREEN_DIR / "timelines" / f"{slugify(serial)}-{int(time.time() * 1000)}"


def redact_timeline_value(key: str, value: Any) -> Any:
    lowered = key.casefold()
    if any(secret in lowered for secret in ("password", "token", "secret", "api_key", "apikey", "authorization")):
        return {"redacted": True, "chars": len(str(value))}
    if lowered == "text":
        return {"redacted": True, "chars": len(str(value))}
    if isinstance(value, dict):
        return {str(child_key): redact_timeline_value(str(child_key), child_value) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [redact_timeline_value(key, item) for item in value[:20]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def redact_timeline_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {str(key): redact_timeline_value(str(key), value) for key, value in payload.items() if key != "serial"}


def append_screen_timeline_event(session: dict[str, Any], event: dict[str, Any]) -> None:
    events_path = Path(str(session["events_path"]))
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


def timeline_action_after_delay_sec() -> float:
    raw = os.environ.get("ANDROID_USE_TIMELINE_AFTER_DELAY_SEC", "0.8")
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        delay = 0.8
    return max(0.0, min(delay, 5.0))


def append_screen_timeline_action(
    serial: str,
    action: str,
    args: dict[str, Any],
    result: dict[str, Any],
    *,
    before: dict[str, Any] | None = None,
) -> None:
    session = SCREEN_VIEWER_SESSIONS.get(serial)
    if not session:
        return
    session_dir = Path(str(session["session_dir"]))
    shots_dir = session_dir / "shots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    after_delay_sec = timeline_action_after_delay_sec()
    if after_delay_sec > 0:
        time.sleep(after_delay_sec)
    png = screenshot_png(serial)
    event_id = f"{int(time.time() * 1000)}-action-{slugify(action)}"
    filename = f"{event_id}.png"
    shot_path = shots_dir / filename
    shot_path.write_bytes(png)
    arguments = redact_timeline_payload(args)
    detail = ", ".join(f"{key}={value}" for key, value in arguments.items() if value not in (None, "", {}))
    event = {
        "id": event_id,
        "kind": "action",
        "title": action,
        "detail": detail[:500],
        "timestamp": timestamp_iso(),
        "timestamp_epoch": time.time(),
        "serial": serial,
        "action": action,
        "after_delay_sec": after_delay_sec,
        "arguments": arguments,
        "focused_window_before": (before or {}).get("state", {}).get("focused_window") if isinstance(before, dict) else None,
        "screenshot_path": str(shot_path),
        "screenshot_url": f"/shots/{filename}",
        "screen": png_size(png),
        "bytes": len(png),
        "digest": hashlib.sha256(png).hexdigest()[:16],
        "result": {key: result.get(key) for key in ("ok", "action", "focused_window", "x", "y", "url", "path") if key in result},
    }
    append_screen_timeline_event(session, event)


def tool_start_screen_viewer(args: dict[str, Any]) -> list[dict[str, Any]]:
    serial = choose_serial(args.get("serial"))
    host = str(args.get("host", "127.0.0.1"))
    port = int(args.get("port") or pick_free_port(host))
    interval_ms = max(250, min(int(args.get("interval_ms", 1000)), 10000))
    max_events = max(10, min(int(args.get("max_events", 80)), 500))
    session_dir = screen_viewer_session_dir(serial, args.get("session_dir") if args.get("session_dir") else None)
    events_path = session_dir / "events.jsonl"
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
        "--session-dir",
        str(session_dir),
        "--max-events",
        str(max_events),
        "--adb",
        adb_binary(),
    ]
    log_path = session_dir / "viewer.log"
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    session_dir.mkdir(parents=True, exist_ok=True)
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

    time.sleep(0.4)
    if process.poll() is not None:
        log_text = log_path.read_text(errors="replace") if log_path.exists() else ""
        raise AndroidUseError(f"Android screen viewer failed to start.\n{log_text[-2000:]}")

    SCREEN_VIEWER_PROCESSES[process.pid] = process
    SCREEN_VIEWER_SESSIONS[serial] = {
        "pid": process.pid,
        "serial": serial,
        "session_dir": str(session_dir),
        "events_path": str(events_path),
        "url": f"http://{host}:{port}/",
    }
    url = f"http://{host}:{port}/"
    return [
        text_content(
            {
                "ok": True,
                "serial": serial,
                "pid": process.pid,
                "url": url,
                "interval_ms": interval_ms,
                "max_events": max_events,
                "session_dir": str(session_dir),
                "events_path": str(events_path),
                "log_path": str(log_path),
                "display": "Open the returned URL to view the screenshot timeline and action steps.",
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
    stopped_set = set(stopped)
    for session_serial, session in list(SCREEN_VIEWER_SESSIONS.items()):
        if int(session.get("pid") or -1) in stopped_set:
            SCREEN_VIEWER_SESSIONS.pop(session_serial, None)
    return [text_content({"ok": True, "stopped_pids": stopped})]
