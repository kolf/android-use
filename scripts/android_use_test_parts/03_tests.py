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
        self.assertEqual([item["serial"] for item in payload["results"]], ["device-1", "device-2"])
        self.assertTrue(all(item["skipped"] == "already-running" for item in payload["results"]))

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

    def test_tool_descriptors_include_required_tools(self) -> None:
        tool_names = {tool["name"] for tool in mcp.tool_descriptors()}

        self.assertIn("android_wake_unlock", tool_names)
        self.assertIn("android_open_url", tool_names)
        self.assertIn("android_open_app", tool_names)
        self.assertIn("android_show_screen", tool_names)
        self.assertIn("android_appshot", tool_names)
        self.assertIn("android_observe", tool_names)
        self.assertIn("android_tap_text", tool_names)
        self.assertIn("android_start_screen_viewer", tool_names)
        self.assertIn("android_start_webrtc_viewer", tool_names)
        self.assertIn("android_agent_run", tool_names)
        self.assertIn("android_agent_tars_run", tool_names)
        self.assertIn("android_start_scrcpy_app", tool_names)

        start_scrcpy = next(tool for tool in mcp.tool_descriptors() if tool["name"] == "android_start_scrcpy")
        properties = start_scrcpy["inputSchema"]["properties"]
        self.assertIn("serials", properties)
        self.assertIn("keep_alive", properties)
        self.assertIn("audio", properties)
        self.assertIn("keyboard", properties)
        self.assertIn("prefer_text", properties)
        self.assertIn("legacy_paste", properties)
        self.assertIn("lock_window_continuous", properties)
        self.assertIn("app_path", properties)
        self.assertEqual(properties["max_size"]["default"], 0)
        self.assertEqual(properties["window_scale"]["default"], 0.5)
        self.assertEqual(properties["render_driver"]["default"], "software")

        agent_run = next(tool for tool in mcp.tool_descriptors() if tool["name"] == "android_agent_run")
        self.assertTrue(agent_run["inputSchema"]["properties"]["show_scrcpy"]["default"])

        appshot = next(tool for tool in mcp.tool_descriptors() if tool["name"] == "android_appshot")
        self.assertTrue(appshot["inputSchema"]["properties"]["include_image"]["default"])
        self.assertTrue(appshot["inputSchema"]["properties"]["save"]["default"])

        self.assertIn("android_start_recording", tool_names)
        self.assertIn("android_start_video_recording", tool_names)
        self.assertIn("android_stop_video_recording", tool_names)
        self.assertIn("android_create_recipe", tool_names)
        self.assertIn("android_replay_recipe", tool_names)
        self.assertIn("android_index_source", tool_names)
        self.assertIn("android_scrcpy_resident_status", tool_names)
        self.assertIn("android_webview_pages", tool_names)
        self.assertIn("android_webview_eval", tool_names)
        self.assertIn("android_wireless_pair_qr", tool_names)
        wireless_pair_qr = next(tool for tool in mcp.tool_descriptors() if tool["name"] == "android_wireless_pair_qr")
        self.assertIn("action", wireless_pair_qr["inputSchema"]["properties"])
        self.assertIn("session_id", wireless_pair_qr["inputSchema"]["properties"])
        wireless_reconnect = next(tool for tool in mcp.tool_descriptors() if tool["name"] == "android_wireless_reconnect")
        self.assertIn("all", wireless_reconnect["inputSchema"]["properties"])
        self.assertIn("xiaoluxue_open_app_url", tool_names)
        self.assertIn("xiaoluxue_runtime_status", tool_names)
        self.assertIn("xiaoluxue_course_snapshot", tool_names)
        self.assertIn("xiaoluxue_set_speed", tool_names)
        self.assertIn("xiaoluxue_goto_widget", tool_names)
        self.assertIn("xiaoluxue_course_fast_path", tool_names)
        self.assertIn("xiaoluxue_open_knowledge_guide", tool_names)
        self.assertIn("xiaoluxue_map_snapshot", tool_names)
        self.assertIn("xiaoluxue_open_native_subject", tool_names)
        self.assertIn("xiaoluxue_map_fast_path", tool_names)
        self.assertIn("xiaoluxue_lesson_fast_path", tool_names)
        self.assertIn("xiaoluxue_login_fast_path", tool_names)
        self.assertIn("xiaoluxue_switch_env", tool_names)
        self.assertIn("xiaoluxue_exercise_snapshot", tool_names)
        self.assertIn("xiaoluxue_exercise_action", tool_names)
        self.assertIn("xiaoluxue_exercise_fast_path", tool_names)
        open_native_subject = next(tool for tool in mcp.tool_descriptors() if tool["name"] == "xiaoluxue_open_native_subject")
        self.assertEqual(open_native_subject["inputSchema"]["properties"]["route_wait_sec"]["default"], 0.45)
        map_fast = next(tool for tool in mcp.tool_descriptors() if tool["name"] == "xiaoluxue_map_fast_path")
        map_props = map_fast["inputSchema"]["properties"]
        self.assertEqual(map_props["route_wait_sec"]["default"], 0.45)
        self.assertEqual(map_props["after_select_wait_sec"]["default"], 0.08)
        self.assertEqual(map_props["module_card_wait_sec"]["default"], 0.16)
        self.assertEqual(map_props["confirm_wait_sec"]["default"], 0.08)
        self.assertFalse(map_props["confirm_expand_focus_check"]["default"])
        lesson_fast = next(tool for tool in mcp.tool_descriptors() if tool["name"] == "xiaoluxue_lesson_fast_path")
        lesson_props = lesson_fast["inputSchema"]["properties"]
        self.assertIn("finish_result", lesson_props["action_name"]["enum"])
        self.assertIn("after_finish_wait_sec", lesson_props)
        login_fast = next(tool for tool in mcp.tool_descriptors() if tool["name"] == "xiaoluxue_login_fast_path")
        self.assertIn("account", login_fast["inputSchema"]["required"])
        self.assertIn("password", login_fast["inputSchema"]["required"])
        exercise_action = next(tool for tool in mcp.tool_descriptors() if tool["name"] == "xiaoluxue_exercise_action")
        action_props = exercise_action["inputSchema"]["properties"]
        self.assertIn("answer_text", action_props)
        self.assertIn("fill_answer", action_props["action_name"]["enum"])
        exercise_fast = next(tool for tool in mcp.tool_descriptors() if tool["name"] == "xiaoluxue_exercise_fast_path")
        self.assertIn("answer_text", exercise_fast["inputSchema"]["properties"])
        exercise_fast_props = exercise_fast["inputSchema"]["properties"]
        self.assertIn("auto_answer", exercise_fast_props["action_name"]["enum"])
        self.assertIn("max_steps", exercise_fast_props)
        self.assertIn("step_wait_sec", exercise_fast_props)
        self.assertIn("click_report", exercise_fast_props)

    def test_xiaoluxue_exercise_fast_path_fills_answer_text(self) -> None:
        original_page = mcp.xiaoluxue_exercise_page
        original_eval = mcp.cdp_eval_value
        original_record = mcp.append_recording_step
        calls: list[str] = []
        try:
            mcp.xiaoluxue_exercise_page = lambda serial: {"id": "page-1", "socket": "webview", "webSocketDebuggerUrl": "ws://test"}

            def fake_eval(page: dict[str, object], expression: str, timeout: int | float = 10) -> dict[str, object]:
                calls.append(expression)
                if '"actionName": "fill_answer"' in expression:
                    return {"ok": True, "method": "dom_text_field", "renderedText": "答案"}
                return {"ready": True, "answerText": "答案"}

            mcp.cdp_eval_value = fake_eval
            mcp.append_recording_step = lambda *args, **kwargs: None
            result = mcp.run_xiaoluxue_exercise_fast_path(
                "device-1",
                {"answer_text": "答案", "after_action_wait_sec": 0},
                record=True,
            )
        finally:
            mcp.xiaoluxue_exercise_page = original_page
            mcp.cdp_eval_value = original_eval
            mcp.append_recording_step = original_record

        self.assertEqual(result["steps"][0]["action"], "fill_answer")
        self.assertTrue(any('"answerText": "答案"' in expression for expression in calls))
        self.assertTrue(any("textarea.keyboard-input-textarea" in expression for expression in calls))
        self.assertTrue(any('"dom_text_field"' in expression for expression in calls))
        self.assertFalse(any("field.focus()" in expression for expression in calls))

    def test_xiaoluxue_exercise_fast_path_auto_answer_uses_page_store(self) -> None:
        original_page = mcp.xiaoluxue_exercise_page
        original_eval = mcp.cdp_eval_value
        original_sleep = mcp.time.sleep
        calls: list[str] = []
        try:
            mcp.xiaoluxue_exercise_page = lambda serial: {"id": "page-1", "socket": "webview", "webSocketDebuggerUrl": "ws://test"}

            def fake_eval(page: dict[str, object], expression: str, timeout: int | float = 10) -> dict[str, object]:
                calls.append(expression)
                if "findQuestionStore" in expression and '"maxSteps": 3' in expression:
                    return {"ok": True, "completed": True}
                return {"ready": True}

            mcp.cdp_eval_value = fake_eval
            mcp.time.sleep = lambda seconds: None
            result = mcp.run_xiaoluxue_exercise_fast_path(
                "device-1",
                {
                    "action_name": "auto_answer",
                    "max_steps": 3,
                    "step_wait_sec": 0.2,
                    "click_report": False,
                    "after_action_wait_sec": 0,
                },
                record=False,
            )
        finally:
            mcp.xiaoluxue_exercise_page = original_page
            mcp.cdp_eval_value = original_eval
            mcp.time.sleep = original_sleep

        self.assertEqual(result["steps"][0]["action"], "auto_answer")
        self.assertTrue(result["steps"][0]["result"]["completed"])
        self.assertEqual(result["pageId"], "page-1")
        self.assertTrue(any('"clickReport": false' in expression for expression in calls))

    def test_xiaoluxue_login_fast_path_checks_agreement_and_redacts_password(self) -> None:
        original_focus = mcp.get_focused_window
        original_observe = mcp.observe_ui
        original_adb = mcp.adb
        original_type = mcp.type_focused_text_fast
        original_record = mcp.append_recording_step
        login_focus = f"{mcp.XIAOLUXUE_STUDENT_PACKAGE}/{mcp.XIAOLUXUE_LOGIN_ACTIVITY}"
        home_focus = f"{mcp.XIAOLUXUE_STUDENT_PACKAGE}/com.xiaoluxue.ai.student.LauncherActivity"
        focus_values = [login_focus, home_focus]
        typed: list[str] = []
        taps: list[tuple[str, ...]] = []
        records: list[dict[str, object]] = []
        agreement_checked = False

        def node(resource: str, bounds: str, text: str = "", checked: bool = False) -> dict[str, object]:
            parsed = mcp.parse_bounds(bounds)
            return {
                "text": text,
                "content_desc": "",
                "resource_id": f"{mcp.XIAOLUXUE_STUDENT_PACKAGE}:id/{resource}",
                "bounds": parsed,
                "center": mcp.bounds_center(parsed),
                "clickable": True,
                "enabled": True,
                "checked": checked,
            }

        try:
            mcp.get_focused_window = lambda serial: focus_values.pop(0) if focus_values else home_focus

            def fake_observe(serial: str, include_xml: bool = False, limit: int = 160) -> dict[str, object]:
                return {
                    "state": {"focused_window": login_focus},
                    "ui": {
                        "nodes": [
                            node("edit_input", "[720,230][1280,310]", text="jvdz162974"),
                            node("edit_input", "[720,430][1280,510]"),
                            node("cb_agreement", "[690,620][730,660]", checked=agreement_checked),
                            node("button", "[720,700][1280,800]", text="登录"),
                        ]
                    },
                }

            def fake_adb(command: list[str], serial: str | None = None, timeout: int | float = 30, **kwargs: object) -> bytes:
                nonlocal agreement_checked
                if command[:3] == ["shell", "input", "tap"]:
                    taps.append(tuple(command))
                    if command[-2:] == ["710", "640"]:
                        agreement_checked = True
                return b""

            mcp.observe_ui = fake_observe
            mcp.adb = fake_adb
            mcp.type_focused_text_fast = lambda serial, text, **kwargs: typed.append(text) or "adb_keyboard_broadcast"
            mcp.append_recording_step = lambda serial, action, arguments, result, **kwargs: records.append(arguments)

            result = mcp.run_xiaoluxue_login_fast_path(
                "device-1",
                {"account": "jvdz162974", "password": "secret", "timeout_sec": 5},
                record=True,
            )
        finally:
            mcp.get_focused_window = original_focus
            mcp.observe_ui = original_observe
            mcp.adb = original_adb
            mcp.type_focused_text_fast = original_type
            mcp.append_recording_step = original_record

        self.assertTrue(result["ok"])
        self.assertEqual(result["focused_window"], home_focus)
        self.assertEqual(typed, ["secret"])
        self.assertIn(("shell", "input", "tap", "710", "640"), taps)
        self.assertTrue(records[-1]["password_redacted"])
        self.assertNotIn("secret", json.dumps(records, ensure_ascii=False))

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

    def test_recipe_from_trace_preserves_xiaoluxue_semantic_actions(self) -> None:
        trace = {
            "id": "trace-1",
            "name": "course",
            "serial": "device-1",
            "steps": [
                {
                    "kind": "action",
                    "action": "xiaoluxue_set_speed",
                    "arguments": {"rate": 2.0},
                    "result": {"ok": True},
                },
                {
                    "kind": "action",
                    "action": "xiaoluxue_goto_widget",
                    "arguments": {"index": 10, "mode": "reload"},
                    "result": {"ok": True},
                },
                {
                    "kind": "action",
                    "action": "xiaoluxue_course_fast_path",
                    "arguments": {"rate": 2.0, "target_last": True},
                    "result": {"ok": True},
                },
                {
                    "kind": "action",
                    "action": "xiaoluxue_map_fast_path",
                    "arguments": {
                        "index": "1.5",
                        "subject_id": 1,
                        "route_if_subject": True,
                        "action_name": "practise",
                        "enter_direct_practice": True,
                    },
                    "result": {"ok": True},
                },
                {
                    "kind": "action",
                    "action": "xiaoluxue_lesson_fast_path",
                    "arguments": {"action_name": "direct_practice", "after_direct_practice_wait_sec": 0.2},
                    "result": {"ok": True},
                },
                {
                    "kind": "action",
                    "action": "xiaoluxue_open_native_subject",
                    "arguments": {"subject": "语文", "route_wait_sec": 0.85},
                    "result": {"ok": True},
                },
                {
                    "kind": "action",
                    "action": "xiaoluxue_switch_env",
                    "arguments": {"env": "test", "open_student": True},
                    "result": {"ok": True},
                },
                {
                    "kind": "action",
                    "action": "xiaoluxue_exercise_action",
                    "arguments": {"action_name": "select_option", "option_key": "B"},
                    "result": {"ok": True},
                },
                {
                    "kind": "action",
                    "action": "xiaoluxue_exercise_fast_path",
                    "arguments": {"option_key": "C", "submit": True},
                    "result": {"ok": True},
                },
                {
                    "kind": "action",
                    "action": "xiaoluxue_lesson_fast_path",
                    "arguments": {"action_name": "finish_result", "after_finish_wait_sec": 0.1},
                    "result": {"ok": True},
                },
                {
                    "kind": "action",
                    "action": "xiaoluxue_exercise_fast_path",
                    "arguments": {"action_name": "auto_answer", "max_steps": 5, "step_wait_sec": 0.3, "click_report": False},
                    "result": {"ok": True},
                },
            ],
        }

        recipe = mcp.recipe_from_trace(trace)

        self.assertEqual(recipe["steps"][0], {"action": "xiaoluxue_set_speed", "rate": 2.0})
        self.assertEqual(recipe["steps"][1], {"action": "xiaoluxue_goto_widget", "index": 10, "mode": "reload"})
        self.assertEqual(recipe["steps"][2], {"action": "xiaoluxue_course_fast_path", "rate": 2.0, "target_last": True})
        self.assertEqual(
            recipe["steps"][3],
            {
                "action": "xiaoluxue_map_fast_path",
                "index": "1.5",
                "subject_id": 1,
                "action_name": "practise",
                "route_if_subject": True,
                "enter_direct_practice": True,
            },
        )
        self.assertEqual(
            recipe["steps"][4],
            {"action": "xiaoluxue_lesson_fast_path", "action_name": "direct_practice", "after_direct_practice_wait_sec": 0.2},
        )
        self.assertEqual(recipe["steps"][5], {"action": "xiaoluxue_open_native_subject", "subject": "语文", "route_wait_sec": 0.85})
        self.assertEqual(recipe["steps"][6], {"action": "xiaoluxue_switch_env", "env": "test", "open_student": True})
        self.assertEqual(recipe["steps"][7], {"action": "xiaoluxue_exercise_action", "action_name": "select_option", "option_key": "B"})
        self.assertEqual(recipe["steps"][8], {"action": "xiaoluxue_exercise_fast_path", "option_key": "C", "submit": True})
        self.assertEqual(recipe["steps"][9], {"action": "xiaoluxue_lesson_fast_path", "action_name": "finish_result", "after_finish_wait_sec": 0.1})
        self.assertEqual(
            recipe["steps"][10],
            {"action": "xiaoluxue_exercise_fast_path", "action_name": "auto_answer", "max_steps": 5, "step_wait_sec": 0.3, "click_report": False},
        )

    def test_normalize_xiaoluxue_env(self) -> None:
        key, url, label = mcp.normalize_xiaoluxue_env("测试环境")
        self.assertEqual(key, "test")
        self.assertEqual(url, "https://gw-stu.test.xiaoluxue.cn/")
        self.assertEqual(label, "Test环境")

        key, url, label = mcp.normalize_xiaoluxue_env("https://gw-stu.dev.xiaoluxue.cn/")
        self.assertEqual(key, "dev")
        self.assertEqual(url, "https://gw-stu.dev.xiaoluxue.cn/")
        self.assertEqual(label, "Dev环境")

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
