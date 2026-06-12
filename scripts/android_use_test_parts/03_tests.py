# Loaded by scripts/test_android_use_mcp.py. Keep this file below 2000 lines.

class AndroidUseMcpTestsPart3(unittest.TestCase):
    def test_prune_scrcpy_processes_removes_bare_windows(self) -> None:
        original_rows = mcp.host_process_rows
        original_bundle = mcp.macos_bundle_identifier_for_pid
        original_kill = mcp.os.kill
        killed: list[int] = []
        try:
            mcp.host_process_rows = lambda: [
                {"pid": 10, "ppid": 1, "command": "scrcpy --serial device-1 --window-title Android device-1"},
            ]
            mcp.macos_bundle_identifier_for_pid = lambda pid: None
            mcp.os.kill = lambda pid, signal: killed.append(pid)  # type: ignore[assignment]

            stopped = mcp.prune_duplicate_scrcpy_processes("device-1")
        finally:
            mcp.host_process_rows = original_rows
            mcp.macos_bundle_identifier_for_pid = original_bundle
            mcp.os.kill = original_kill  # type: ignore[assignment]

        self.assertEqual(stopped, [10])
        self.assertEqual(killed, [10])

    def test_prune_scrcpy_processes_keeps_newest_app_wrapper(self) -> None:
        original_rows = mcp.host_process_rows
        original_bundle = mcp.macos_bundle_identifier_for_pid
        original_kill = mcp.os.kill
        killed: list[int] = []
        try:
            mcp.host_process_rows = lambda: [
                {"pid": 10, "ppid": 1, "command": "scrcpy --serial device-1 --window-title Android device-1"},
                {"pid": 20, "ppid": 1, "command": "scrcpy --serial device-1 --window-title 荣耀平板Z6"},
                {"pid": 15, "ppid": 1, "command": "scrcpy --serial device-1 --window-title 荣耀平板Z6"},
            ]
            mcp.macos_bundle_identifier_for_pid = lambda pid: mcp.ANDROID_USE_BUNDLE_ID if pid in {15, 20} else None
            mcp.os.kill = lambda pid, signal: killed.append(pid)  # type: ignore[assignment]

            stopped = mcp.prune_duplicate_scrcpy_processes("device-1")
        finally:
            mcp.host_process_rows = original_rows
            mcp.macos_bundle_identifier_for_pid = original_bundle
            mcp.os.kill = original_kill  # type: ignore[assignment]

        self.assertEqual(stopped, [10, 15])
        self.assertEqual(killed, [10, 15])

    def test_prune_scrcpy_processes_removes_legacy_unattributed_app_wrapper(self) -> None:
        original_rows = mcp.host_process_rows
        original_bundle = mcp.macos_bundle_identifier_for_pid
        original_kill = mcp.os.kill
        original_sleep = mcp.time.sleep
        killed: list[int] = []
        try:
            mcp.host_process_rows = lambda: [
                {"pid": 10, "ppid": 1, "command": "/opt/homebrew/bin/scrcpy"},
                {"pid": 20, "ppid": 1, "command": "scrcpy --serial device-1 --window-title 荣耀平板Z6"},
            ]
            mcp.macos_bundle_identifier_for_pid = (
                lambda pid: mcp.ANDROID_USE_BUNDLE_ID if pid in {10, 20} else None
            )
            mcp.os.kill = lambda pid, signal: killed.append(pid)  # type: ignore[assignment]
            mcp.time.sleep = lambda _seconds: None

            stopped = mcp.prune_duplicate_scrcpy_processes("device-1")
        finally:
            mcp.host_process_rows = original_rows
            mcp.macos_bundle_identifier_for_pid = original_bundle
            mcp.os.kill = original_kill  # type: ignore[assignment]
            mcp.time.sleep = original_sleep

        self.assertEqual(stopped, [10])
        self.assertEqual(killed, [10])

    def test_connected_device_serials_prefers_one_physical_device(self) -> None:
        original_list_devices = mcp.list_devices
        original_shell = mcp.shell
        original_env = {
            "ANDROID_USE_SCRCPY_RESIDENT_SERIALS": mcp.os.environ.get("ANDROID_USE_SCRCPY_RESIDENT_SERIALS"),
            "ANDROID_USE_SERIAL": mcp.os.environ.get("ANDROID_USE_SERIAL"),
            "ANDROID_SERIAL": mcp.os.environ.get("ANDROID_SERIAL"),
        }
        try:
            for key in original_env:
                mcp.os.environ.pop(key, None)
            mcp.list_devices = lambda: [
                {"serial": "ANMB9X5A10G00857", "state": "device"},
                {"serial": "emulator-5554", "state": "device"},
            ]
            mcp.shell = lambda serial, command, timeout=30: serial

            self.assertEqual(mcp.connected_device_serials(), ["ANMB9X5A10G00857"])

            mcp.os.environ["ANDROID_USE_SCRCPY_RESIDENT_SERIALS"] = "emulator-5554,missing"
            self.assertEqual(mcp.connected_device_serials(), ["emulator-5554"])
        finally:
            mcp.list_devices = original_list_devices
            mcp.shell = original_shell
            for key, value in original_env.items():
                if value is None:
                    mcp.os.environ.pop(key, None)
                else:
                    mcp.os.environ[key] = value

    def test_start_scrcpy_accepts_multiple_serials(self) -> None:
        original_choose = mcp.choose_serial
        original_prune = mcp.prune_duplicate_scrcpy_processes
        original_app_visible = mcp.scrcpy_app_wrapper_process_for_serial
        original_clear = mcp.clear_scrcpy_user_closed
        original_system_app = mcp.ensure_system_android_launcher_app
        try:
            mcp.choose_serial = lambda serial=None: str(serial)
            mcp.prune_duplicate_scrcpy_processes = lambda serial: []
            mcp.scrcpy_app_wrapper_process_for_serial = lambda serial: f"scrcpy {serial}"
            mcp.clear_scrcpy_user_closed = lambda serial: None
            mcp.ensure_system_android_launcher_app = lambda: {"ok": True, "skipped": "already-present"}  # type: ignore[assignment]

            content = mcp.tool_start_scrcpy({"serials": ["device-1", "device-2"]})
        finally:
            mcp.choose_serial = original_choose
            mcp.prune_duplicate_scrcpy_processes = original_prune
            mcp.scrcpy_app_wrapper_process_for_serial = original_app_visible
            mcp.clear_scrcpy_user_closed = original_clear
            mcp.ensure_system_android_launcher_app = original_system_app  # type: ignore[assignment]

        payload = json.loads(content[0]["text"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["serials"], ["device-1", "device-2"])
        self.assertEqual(len(payload["results"]), 2)
        self.assertEqual(payload["errors"], [])

    def test_scrcpy_manual_close_marker_blocks_resident_reopen_until_tool_call(self) -> None:
        original_screen_dir = mcp.SCREEN_DIR
        original_app_visible = mcp.scrcpy_app_wrapper_process_for_serial
        original_start = mcp.tool_start_scrcpy
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                mcp.SCREEN_DIR = Path(tmpdir)
                mcp.scrcpy_user_closed_path("device-1").write_text(json.dumps({"closed_at": 1, "runtime_sec": 3}))
                mcp.scrcpy_app_wrapper_process_for_serial = lambda _serial: None
                calls: list[dict[str, object]] = []

                def fake_start(args: dict[str, object]) -> list[dict[str, str]]:
                    calls.append(args)
                    return [mcp.text_content({"ok": True, "serial": args.get("serial")})]

                mcp.tool_start_scrcpy = fake_start  # type: ignore[assignment]

                resident_result = mcp.ensure_default_scrcpy_window(
                    "device-1",
                    {"show_scrcpy": True, "respect_manual_close": True},
                )
                self.assertEqual(resident_result["skipped"], "user-closed")
                self.assertEqual(calls, [])

                tool_result = mcp.ensure_default_scrcpy_window(
                    "device-1",
                    {"show_scrcpy": True, "respect_manual_close": False},
                )
                self.assertTrue(tool_result["ok"])
                self.assertEqual(len(calls), 1)
                self.assertFalse(mcp.scrcpy_user_closed_path("device-1").exists())
        finally:
            mcp.SCREEN_DIR = original_screen_dir
            mcp.scrcpy_app_wrapper_process_for_serial = original_app_visible
            mcp.tool_start_scrcpy = original_start  # type: ignore[assignment]

    def test_scrcpy_supervisor_treats_late_exit_as_manual_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            marker = Path(tmpdir) / "user-closed.json"
            ready = Path(tmpdir) / "ready.json"
            supervisor = Path(__file__).with_name("scrcpy_supervisor.py")
            result = subprocess.run(
                [
                    sys.executable,
                    str(supervisor),
                    "--ready-file",
                    str(ready),
                    "--ready-after-sec",
                    "0.05",
                    "--early-exit-sec",
                    "0.05",
                    "--manual-exit-after-sec",
                    "0.1",
                    "--user-closed-file",
                    str(marker),
                    "--",
                    sys.executable,
                    "-c",
                    "import time; time.sleep(0.2)",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertTrue(marker.exists(), result.stdout)
            payload = json.loads(marker.read_text())
            self.assertGreaterEqual(payload["runtime_sec"], 0.1)
            self.assertIn("not restarting", result.stdout)

    def test_tool_descriptors_include_only_generic_android_tools(self) -> None:
        tools = mcp.tool_descriptors()
        tool_names = {tool["name"] for tool in tools}

        self.assertFalse([name for name in tool_names if not name.startswith("android_")])
        self.assertIn("android_check_dependencies", tool_names)
        self.assertIn("android_list_devices", tool_names)
        self.assertIn("android_open_url", tool_names)
        self.assertIn("android_webview_pages", tool_names)
        self.assertIn("android_webview_eval", tool_names)
        self.assertIn("android_start_recording", tool_names)
        self.assertIn("android_replay_recipe", tool_names)
        self.assertIn("android_start_screen_viewer", tool_names)
        self.assertIn("android_start_video_recording", tool_names)
        self.assertIn("android_wireless_pair_qr", tool_names)

    def test_playwright_webview_worker_disabled_uses_one_shot(self) -> None:
        original_env = mcp.os.environ.get("ANDROID_USE_PLAYWRIGHT_WEBVIEW_WORKER")
        original_worker = mcp.run_playwright_android_webview_worker
        original_once = mcp.run_playwright_android_webview_once
        calls: list[str] = []
        try:
            mcp.os.environ.pop("ANDROID_USE_PLAYWRIGHT_WEBVIEW_WORKER", None)
            mcp.run_playwright_android_webview_worker = lambda *args, **kwargs: self.fail("worker should be disabled")

            def fake_once(payload: dict[str, object], timeout: float = 10) -> dict[str, object]:
                calls.append(str(payload["action"]))
                return {"ok": True, "backend": "playwright-android", "pages": []}

            mcp.run_playwright_android_webview_once = fake_once  # type: ignore[assignment]

            result = mcp.run_playwright_android_webview({"action": "list"}, timeout=2)
        finally:
            mcp.run_playwright_android_webview_worker = original_worker  # type: ignore[assignment]
            mcp.run_playwright_android_webview_once = original_once  # type: ignore[assignment]
            if original_env is None:
                mcp.os.environ.pop("ANDROID_USE_PLAYWRIGHT_WEBVIEW_WORKER", None)
            else:
                mcp.os.environ["ANDROID_USE_PLAYWRIGHT_WEBVIEW_WORKER"] = original_env

        self.assertTrue(result["ok"])
        self.assertEqual(calls, ["list"])

    def test_playwright_webview_worker_enabled_uses_worker(self) -> None:
        original_env = mcp.os.environ.get("ANDROID_USE_PLAYWRIGHT_WEBVIEW_WORKER")
        original_worker = mcp.run_playwright_android_webview_worker
        original_once = mcp.run_playwright_android_webview_once
        calls: list[str] = []
        try:
            mcp.os.environ["ANDROID_USE_PLAYWRIGHT_WEBVIEW_WORKER"] = "1"

            def fake_worker(payload: dict[str, object], timeout: float = 10) -> dict[str, object]:
                calls.append(str(payload["action"]))
                return {"ok": True, "backend": "playwright-android", "worker": True, "pages": []}

            mcp.run_playwright_android_webview_worker = fake_worker  # type: ignore[assignment]
            mcp.run_playwright_android_webview_once = lambda *args, **kwargs: self.fail("one-shot should not be used")

            result = mcp.run_playwright_android_webview({"action": "list"}, timeout=2)
        finally:
            mcp.run_playwright_android_webview_worker = original_worker  # type: ignore[assignment]
            mcp.run_playwright_android_webview_once = original_once  # type: ignore[assignment]
            if original_env is None:
                mcp.os.environ.pop("ANDROID_USE_PLAYWRIGHT_WEBVIEW_WORKER", None)
            else:
                mcp.os.environ["ANDROID_USE_PLAYWRIGHT_WEBVIEW_WORKER"] = original_env

        self.assertTrue(result["worker"])
        self.assertEqual(calls, ["list"])

    def test_playwright_webview_worker_failure_falls_back_to_one_shot(self) -> None:
        original_env = mcp.os.environ.get("ANDROID_USE_PLAYWRIGHT_WEBVIEW_WORKER")
        original_worker = mcp.run_playwright_android_webview_worker
        original_once = mcp.run_playwright_android_webview_once
        calls: list[str] = []
        try:
            mcp.os.environ["ANDROID_USE_PLAYWRIGHT_WEBVIEW_WORKER"] = "1"

            def fake_worker(_payload: dict[str, object], timeout: float = 10) -> dict[str, object]:
                calls.append("worker")
                raise mcp.AndroidUseError("worker unavailable")

            def fake_once(payload: dict[str, object], timeout: float = 10) -> dict[str, object]:
                calls.append("one-shot")
                return {"ok": True, "backend": "playwright-android", "pages": [{"id": "fallback"}]}

            mcp.run_playwright_android_webview_worker = fake_worker  # type: ignore[assignment]
            mcp.run_playwright_android_webview_once = fake_once  # type: ignore[assignment]

            result = mcp.run_playwright_android_webview({"action": "list"}, timeout=2)
        finally:
            mcp.run_playwright_android_webview_worker = original_worker  # type: ignore[assignment]
            mcp.run_playwright_android_webview_once = original_once  # type: ignore[assignment]
            if original_env is None:
                mcp.os.environ.pop("ANDROID_USE_PLAYWRIGHT_WEBVIEW_WORKER", None)
            else:
                mcp.os.environ["ANDROID_USE_PLAYWRIGHT_WEBVIEW_WORKER"] = original_env

        self.assertEqual(calls, ["worker", "one-shot"])
        self.assertEqual(result["pages"][0]["id"], "fallback")

    def test_webview_pages_uses_playwright_android(self) -> None:
        original_choose = mcp.choose_serial
        original_pages = mcp.playwright_android_webview_pages
        calls: list[tuple[str, float]] = []
        try:
            mcp.choose_serial = lambda serial=None: str(serial or "device-1")

            def fake_pages(serial: str, *, timeout: float = 10) -> list[dict[str, object]]:
                calls.append((serial, timeout))
                return [{"id": "com.example:123", "pkg": "com.example", "url": "https://example.test"}]

            mcp.playwright_android_webview_pages = fake_pages  # type: ignore[assignment]
            content = mcp.tool_webview_pages({"serial": "device-1", "timeout_sec": 4})
        finally:
            mcp.choose_serial = original_choose
            mcp.playwright_android_webview_pages = original_pages  # type: ignore[assignment]

        payload = json.loads(content[0]["text"])
        self.assertEqual(payload["backend"], "playwright-android")
        self.assertEqual(payload["pages"][0]["pkg"], "com.example")
        self.assertEqual(calls, [("device-1", 4.0)])

    def test_webview_pages_reuses_recent_cache_until_ttl(self) -> None:
        original_run = mcp.run_playwright_android_webview
        original_monotonic = mcp.time.monotonic
        original_env = mcp.os.environ.get("ANDROID_USE_WEBVIEW_PAGE_CACHE_TTL")
        now = [100.0]
        calls: list[dict[str, object]] = []
        try:
            mcp.clear_webview_page_cache()
            mcp.os.environ["ANDROID_USE_WEBVIEW_PAGE_CACHE_TTL"] = "2"
            mcp.time.monotonic = lambda: now[0]

            def fake_run(payload: dict[str, object], timeout: float = 10) -> dict[str, object]:
                calls.append({"payload": dict(payload), "timeout": timeout})
                return {
                    "ok": True,
                    "pages": [{"id": f"page-{len(calls)}", "ok": True, "url": "https://example.test"}],
                }

            mcp.run_playwright_android_webview = fake_run  # type: ignore[assignment]

            first = mcp.playwright_android_webview_pages("device-1", timeout=3)
            first[0]["id"] = "mutated"
            second = mcp.playwright_android_webview_pages("device-1", timeout=3)
            now[0] = 102.01
            third = mcp.playwright_android_webview_pages("device-1", timeout=3)
        finally:
            mcp.run_playwright_android_webview = original_run
            mcp.time.monotonic = original_monotonic
            mcp.clear_webview_page_cache()
            if original_env is None:
                mcp.os.environ.pop("ANDROID_USE_WEBVIEW_PAGE_CACHE_TTL", None)
            else:
                mcp.os.environ["ANDROID_USE_WEBVIEW_PAGE_CACHE_TTL"] = original_env

        self.assertEqual(second[0]["id"], "page-1")
        self.assertEqual(third[0]["id"], "page-2")
        self.assertEqual(len(calls), 2)

    def test_webview_pages_failure_clears_stale_cache(self) -> None:
        original_run = mcp.run_playwright_android_webview
        original_monotonic = mcp.time.monotonic
        original_env = mcp.os.environ.get("ANDROID_USE_WEBVIEW_PAGE_CACHE_TTL")
        now = [100.0]
        calls: list[dict[str, object]] = []
        try:
            mcp.clear_webview_page_cache()
            mcp.os.environ["ANDROID_USE_WEBVIEW_PAGE_CACHE_TTL"] = "2"
            mcp.time.monotonic = lambda: now[0]

            def fake_run(payload: dict[str, object], timeout: float = 10) -> dict[str, object]:
                calls.append({"payload": dict(payload), "timeout": timeout})
                if len(calls) == 2:
                    raise mcp.AndroidUseError("discovery failed")
                return {
                    "ok": True,
                    "pages": [{"id": f"page-{len(calls)}", "ok": True, "url": "https://example.test"}],
                }

            mcp.run_playwright_android_webview = fake_run  # type: ignore[assignment]

            first = mcp.playwright_android_webview_pages("device-1", timeout=3)
            now[0] = 103.0
            with self.assertRaises(mcp.AndroidUseError):
                mcp.playwright_android_webview_pages("device-1", timeout=3)
            now[0] = 103.1
            fresh = mcp.playwright_android_webview_pages("device-1", timeout=3)
        finally:
            mcp.run_playwright_android_webview = original_run
            mcp.time.monotonic = original_monotonic
            mcp.clear_webview_page_cache()
            if original_env is None:
                mcp.os.environ.pop("ANDROID_USE_WEBVIEW_PAGE_CACHE_TTL", None)
            else:
                mcp.os.environ["ANDROID_USE_WEBVIEW_PAGE_CACHE_TTL"] = original_env

        self.assertEqual(first[0]["id"], "page-1")
        self.assertEqual(fresh[0]["id"], "page-3")
        self.assertEqual(len(calls), 3)

    def test_webview_snapshot_eval_failure_clears_cached_pages(self) -> None:
        original_run = mcp.run_playwright_android_webview
        original_eval = mcp.playwright_android_webview_eval
        original_monotonic = mcp.time.monotonic
        original_env = mcp.os.environ.get("ANDROID_USE_WEBVIEW_PAGE_CACHE_TTL")
        now = [100.0]
        list_calls: list[dict[str, object]] = []
        eval_calls: list[str | None] = []
        try:
            mcp.clear_webview_page_cache()
            mcp.os.environ["ANDROID_USE_WEBVIEW_PAGE_CACHE_TTL"] = "10"
            mcp.time.monotonic = lambda: now[0]

            def fake_run(payload: dict[str, object], timeout: float = 10) -> dict[str, object]:
                list_calls.append({"payload": dict(payload), "timeout": timeout})
                return {
                    "ok": True,
                    "pages": [
                        {
                            "id": f"page-{len(list_calls)}",
                            "ok": True,
                            "url": "https://example.test",
                        }
                    ],
                }

            def fake_eval(serial: str, expression: str, **kwargs: object) -> dict[str, object]:
                page_id = kwargs.get("page_id")
                eval_calls.append(str(page_id) if page_id is not None else None)
                if len(eval_calls) == 1:
                    raise mcp.AndroidUseError("stale page")
                return {
                    "backend": "playwright-android",
                    "page": {"id": page_id, "url": "https://example.test"},
                    "result": {
                        "type": "object",
                        "value": {
                            "ok": True,
                            "url": "https://example.test",
                            "title": "Example",
                            "elements": [],
                        },
                    },
                }

            mcp.run_playwright_android_webview = fake_run  # type: ignore[assignment]
            mcp.playwright_android_webview_eval = fake_eval  # type: ignore[assignment]

            missed = mcp.webview_snapshot_fast("device-1", limit=3)
            recovered = mcp.webview_snapshot_fast("device-1", limit=3)
        finally:
            mcp.run_playwright_android_webview = original_run
            mcp.playwright_android_webview_eval = original_eval
            mcp.time.monotonic = original_monotonic
            mcp.clear_webview_page_cache()
            if original_env is None:
                mcp.os.environ.pop("ANDROID_USE_WEBVIEW_PAGE_CACHE_TTL", None)
            else:
                mcp.os.environ["ANDROID_USE_WEBVIEW_PAGE_CACHE_TTL"] = original_env

        self.assertIsNone(missed)
        self.assertIsNotNone(recovered)
        self.assertEqual(eval_calls, ["page-1", "page-2"])
        self.assertEqual(len(list_calls), 2)

    def test_webview_eval_uses_playwright_android(self) -> None:
        original_choose = mcp.choose_serial
        original_eval = mcp.playwright_android_webview_eval
        captured: dict[str, object] = {}
        try:
            mcp.choose_serial = lambda serial=None: str(serial or "device-1")

            def fake_eval(serial: str, expression: str, **kwargs: object) -> dict[str, object]:
                captured.update({"serial": serial, "expression": expression, **kwargs})
                return {
                    "backend": "playwright-android",
                    "page": {"id": "com.example:123", "url": "https://example.test"},
                    "result": {"type": "number", "value": 42},
                }

            mcp.playwright_android_webview_eval = fake_eval  # type: ignore[assignment]
            content = mcp.tool_webview_eval(
                {
                    "serial": "device-1",
                    "package": "com.example",
                    "expression": "21 * 2",
                    "timeout_sec": 3,
                }
            )
        finally:
            mcp.choose_serial = original_choose
            mcp.playwright_android_webview_eval = original_eval  # type: ignore[assignment]

        payload = json.loads(content[0]["text"])
        self.assertEqual(payload["backend"], "playwright-android")
        self.assertEqual(payload["result"]["value"], 42)
        self.assertEqual(captured["serial"], "device-1")
        self.assertEqual(captured["package"], "com.example")
        self.assertEqual(captured["expression"], "21 * 2")

    def test_observe_prefers_webview_snapshot(self) -> None:
        original_choose = mcp.choose_serial
        original_snapshot = mcp.webview_snapshot_fast
        original_observe_ui = mcp.observe_ui
        original_device_state = mcp.device_state
        try:
            mcp.choose_serial = lambda serial=None: str(serial or "device-1")
            mcp.device_state = lambda serial: {"serial": serial, "screen": {"width": 400, "height": 800}}
            mcp.webview_snapshot_fast = lambda serial, *, limit=80, timeout=None: {
                "backend": "playwright-android",
                "page": {"id": "webview-1", "pkg": "com.example"},
                "url": "https://example.test",
                "title": "Example",
                "elements": [
                    {
                        "tag": "button",
                        "text": "继续",
                        "aria": "",
                        "id": "next",
                        "interactive": True,
                        "rect": {"left": 10, "top": 20, "right": 70, "bottom": 50, "x": 40, "y": 35},
                    }
                ],
            }
            mcp.observe_ui = lambda *args, **kwargs: self.fail("UIAutomator should not be used")

            content = mcp.tool_observe({"serial": "device-1", "prefer_webview": True})
        finally:
            mcp.choose_serial = original_choose
            mcp.webview_snapshot_fast = original_snapshot
            mcp.observe_ui = original_observe_ui
            mcp.device_state = original_device_state

        payload = json.loads(content[0]["text"])
        self.assertEqual(payload["source"], "webview_dom")
        self.assertEqual(payload["ui"]["source"], "webview_dom")
        self.assertEqual(payload["ui"]["coordinate_space"], "css_pixels")
        self.assertEqual(payload["ui"]["nodes"][0]["text"], "继续")
        self.assertEqual(payload["state"]["webview_url"], "https://example.test")

    def test_tap_text_prefers_webview_dom_click(self) -> None:
        original_choose = mcp.choose_serial
        original_tap_webview = mcp.tap_webview_text_fast
        original_observe_ui = mcp.observe_ui
        original_active = mcp.active_recording
        calls: list[tuple[str, str, bool]] = []
        try:
            mcp.choose_serial = lambda serial=None: str(serial or "device-1")
            mcp.active_recording = lambda serial: None

            def fake_tap(serial: str, query: str, *, exact: bool = True, timeout=None) -> dict[str, object]:
                calls.append((serial, query, exact))
                return {
                    "backend": "playwright-android",
                    "page": {"url": "https://example.test"},
                    "match": {"text": "继续", "rect": {"x": 40, "y": 35}},
                }

            mcp.tap_webview_text_fast = fake_tap  # type: ignore[assignment]
            mcp.observe_ui = lambda *args, **kwargs: self.fail("UIAutomator should not be used")

            content = mcp.tool_tap_text({"serial": "device-1", "text": "继续"})
        finally:
            mcp.choose_serial = original_choose
            mcp.tap_webview_text_fast = original_tap_webview
            mcp.observe_ui = original_observe_ui
            mcp.active_recording = original_active

        payload = json.loads(content[0]["text"])
        self.assertEqual(payload["source"], "webview_dom")
        self.assertEqual(payload["backend"], "playwright-android")
        self.assertEqual(payload["x"], 40)
        self.assertEqual(calls, [("device-1", "继续", True)])

    def test_tap_text_falls_back_to_uiautomator_when_webview_misses(self) -> None:
        original_choose = mcp.choose_serial
        original_tap_webview = mcp.tap_webview_text_fast
        original_observe_ui = mcp.observe_ui
        original_adb = mcp.adb
        original_active = mcp.active_recording
        adb_calls: list[list[str]] = []
        try:
            mcp.choose_serial = lambda serial=None: str(serial or "device-1")
            mcp.active_recording = lambda serial: None
            mcp.tap_webview_text_fast = lambda *args, **kwargs: None
            mcp.observe_ui = lambda serial, **kwargs: {
                "state": {"serial": serial},
                "ui": {
                    "nodes": [
                        {
                            "text": "继续",
                            "content_desc": "",
                            "resource_id": "",
                            "center": {"x": 40, "y": 35},
                        }
                    ]
                },
            }
            mcp.adb = lambda args, **kwargs: adb_calls.append(list(args)) or ""

            content = mcp.tool_tap_text({"serial": "device-1", "text": "继续"})
        finally:
            mcp.choose_serial = original_choose
            mcp.tap_webview_text_fast = original_tap_webview
            mcp.observe_ui = original_observe_ui
            mcp.adb = original_adb
            mcp.active_recording = original_active

        payload = json.loads(content[0]["text"])
        self.assertEqual(payload["matched_node"]["text"], "继续")
        self.assertEqual(adb_calls, [["shell", "input", "tap", "40", "35"]])

    def test_agent_step_prefers_webview_action_in_hybrid(self) -> None:
        original_choose = mcp.choose_serial
        original_scrcpy = mcp.ensure_default_scrcpy_window
        original_webview = mcp.fast_webview_action_from_instruction
        original_uiautomator = mcp.fast_ui_action_from_instruction
        original_observe_ui = mcp.observe_ui
        try:
            mcp.choose_serial = lambda serial=None: str(serial or "device-1")
            mcp.ensure_default_scrcpy_window = lambda serial, args: {"ok": True, "skipped": "not-executing"}
            mcp.fast_webview_action_from_instruction = lambda serial, instruction: {
                "action": "tap_text",
                "text": "继续",
                "source": "webview_dom",
            }
            mcp.fast_ui_action_from_instruction = lambda *args, **kwargs: self.fail("UIAutomator should not be used")
            mcp.observe_ui = lambda *args, **kwargs: self.fail("VLM grounding should not be used")

            content = mcp.tool_agent_step({"serial": "device-1", "instruction": "点击继续", "execute": False})
        finally:
            mcp.choose_serial = original_choose
            mcp.ensure_default_scrcpy_window = original_scrcpy
            mcp.fast_webview_action_from_instruction = original_webview
            mcp.fast_ui_action_from_instruction = original_uiautomator
            mcp.observe_ui = original_observe_ui

        payload = json.loads(content[0]["text"])
        self.assertEqual(payload["proposed_action"]["source"], "webview_dom")
        self.assertEqual(payload["proposed_action"]["text"], "继续")

    def test_agent_run_stops_after_webview_fast_action(self) -> None:
        original_choose = mcp.choose_serial
        original_scrcpy = mcp.ensure_default_scrcpy_window
        original_webview = mcp.fast_webview_action_from_instruction
        original_execute = mcp.execute_action
        calls: list[str] = []
        try:
            mcp.choose_serial = lambda serial=None: str(serial or "device-1")
            mcp.ensure_default_scrcpy_window = lambda serial, args: {"ok": True, "skipped": "test"}

            def fake_webview(serial: str, instruction: str) -> dict[str, object]:
                calls.append(instruction)
                return {"action": "tap_text", "text": "继续", "source": "webview_dom"}

            mcp.fast_webview_action_from_instruction = fake_webview  # type: ignore[assignment]
            mcp.execute_action = lambda serial, action: [mcp.text_content({"ok": True})]

            content = mcp.tool_agent_run(
                {"serial": "device-1", "instruction": "点击继续", "max_steps": 3, "delay_sec": 0}
            )
        finally:
            mcp.choose_serial = original_choose
            mcp.ensure_default_scrcpy_window = original_scrcpy
            mcp.fast_webview_action_from_instruction = original_webview
            mcp.execute_action = original_execute

        payload = json.loads(content[0]["text"])
        self.assertEqual(len(payload["steps"]), 1)
        self.assertEqual(payload["steps"][0]["source"], "webview")
        self.assertEqual(calls, ["点击继续"])

    def test_recipe_from_trace_prefers_selectors(self) -> None:
        trace = {
            "id": "trace-1",
            "name": "login",
            "serial": "device-1",
            "steps": [
                {
                    "kind": "action",
                    "action": "tap",
                    "arguments": {"x": 100, "y": 200},
                    "result": {"x": 100, "y": 200},
                    "before": {
                        "state": {"screen": {"width": 400, "height": 800}},
                        "ui": {
                            "nodes": [
                                {
                                    "text": "登录",
                                    "resource_id": "com.example:id/login",
                                    "bounds": {"left": 80, "top": 180, "right": 160, "bottom": 230},
                                    "center": {"x": 120, "y": 205},
                                }
                            ]
                        },
                    },
                    "after": {"fingerprint": {"focused_window": "LoginActivity", "labels": ["验证码"]}},
                }
            ],
        }

        recipe = mcp.recipe_from_trace(trace)
        step = recipe["steps"][0]

        self.assertEqual(step["action"], "tap")
        self.assertEqual(step["target"]["selectors"][0], {"strategy": "resource_id", "value": "com.example:id/login"})
        self.assertEqual(step["verify"]["focused_window"], "LoginActivity")
        self.assertEqual(step["verify"]["labels_any"], [])

    def test_parse_env_assignment_allows_android_use_and_openai_keys(self) -> None:
        self.assertEqual(
            mcp.parse_env_assignment('export ANDROID_USE_VLM_MODEL="doubao-seedream-5.0-lite"'),
            ("ANDROID_USE_VLM_MODEL", "doubao-seedream-5.0-lite"),
        )
        self.assertEqual(
            mcp.parse_env_assignment("OPENAI_API_KEY='sk-test'"),
            ("OPENAI_API_KEY", "sk-test"),
        )
        self.assertIsNone(mcp.parse_env_assignment("PATH=/tmp"))
        self.assertIsNone(mcp.parse_env_assignment("# ANDROID_USE_VLM_API_KEY=secret"))

    def test_parse_tars_action_response_accepts_bare_action(self) -> None:
        action = mcp.parse_tars_action_response("finished(content='当前屏幕是答题页')", {"width": 2000, "height": 1200})

        self.assertEqual(action["action"], "done")
        self.assertEqual(action["summary"], "当前屏幕是答题页")

    def test_index_source_tree_extracts_android_clues(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "MainActivity.kt").write_text(
                'class MainActivity : Activity() { fun Ui() { Text("首页"); Button("登录"); Modifier.testTag("login_button") } }'
            )
            (root / "res.xml").write_text(
                '<TextView android:id="@+id/title" android:text="欢迎" android:contentDescription="标题" />'
            )

            app_map = mcp.index_source_tree(root)

        control_values = {control["value"] for control in app_map["controls"]}
        self.assertIn("首页", control_values)
        self.assertIn("login_button", control_values)
        self.assertIn("title", control_values)
        self.assertEqual(app_map["files_indexed"], 2)

    def test_tools_list_request_shape(self) -> None:
        response = mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})

        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], 1)
        json.dumps(response["result"]["tools"])
