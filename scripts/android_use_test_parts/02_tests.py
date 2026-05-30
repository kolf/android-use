# Loaded by scripts/test_android_use_mcp.py. Keep this file below 2000 lines.

class AndroidUseMcpTestsPart2(unittest.TestCase):
    def test_xiaoluxue_dismiss_debug_overlay_presses_back(self) -> None:
        original_focus = mcp.get_focused_window
        original_adb = mcp.adb
        original_sleep = mcp.time.sleep
        commands: list[list[str]] = []
        steps: list[dict[str, object]] = []
        try:
            mcp.get_focused_window = lambda serial: (
                f"{mcp.XIAOLUXUE_STUDENT_PACKAGE}/leakcanary.internal.activity.LeakLauncherActivity"
            )
            mcp.adb = lambda args, serial=None, timeout=30: commands.append(list(args)) or b""
            mcp.time.sleep = lambda seconds: None

            dismissed = mcp.xiaoluxue_dismiss_debug_overlay_if_needed("serial", steps, 0.0)

            self.assertTrue(dismissed)
            self.assertEqual(commands, [["shell", "input", "keyevent", "BACK"]])
            self.assertEqual(steps[0]["reason"], "dismiss-leakcanary-overlay")
        finally:
            mcp.get_focused_window = original_focus
            mcp.adb = original_adb
            mcp.time.sleep = original_sleep

    def test_xiaoluxue_map_direct_practice_uses_fast_probe_defaults_without_assuming_focus(self) -> None:
        original_tap = mcp.xiaoluxue_native_tap
        original_direct = mcp.xiaoluxue_tap_lesson_direct_practice
        captured: dict[str, object] = {}
        try:
            mcp.xiaoluxue_native_tap = lambda *args, **kwargs: None

            def fake_direct(serial: str, args: dict[str, object], steps: list[dict[str, object]], started_at: float, *, default_wait_sec: float = 0.0) -> dict[str, object]:
                captured.update(args)
                return {"action": "direct_practice"}

            mcp.xiaoluxue_tap_lesson_direct_practice = fake_direct
            result = mcp.xiaoluxue_tap_study_module_entry(
                "serial",
                action_name="practise",
                args={"enter_direct_practice": True},
                steps=[],
                started_at=0.0,
                base_action_point=(1000, 420),
                window_info={"width": 2000, "height": 1200},
            )

            self.assertIsNotNone(result)
            self.assertNotIn("assume_lesson_activity", captured)
            self.assertTrue(captured["tap_direct_practice_until_answer_ready"])
            self.assertEqual(captured["answer_ready_poll_after_taps"], 2)
            self.assertEqual(captured["lesson_focus_timeout_sec"], 0.55)
        finally:
            mcp.xiaoluxue_native_tap = original_tap
            mcp.xiaoluxue_tap_lesson_direct_practice = original_direct
        self.assertEqual(
            mcp.xiaoluxue_lesson_fast_action_from_instruction("继续到下一题"),
            {
                "action": "xiaoluxue_lesson_fast_path",
                "instruction": "继续到下一题",
                "action_name": "continue_answer",
                "source": "xiaoluxue-native-lesson",
            },
        )

    def test_xiaoluxue_lesson_fast_path_finish_result_taps_finish(self) -> None:
        original_tap = mcp.xiaoluxue_native_tap
        original_sleep = mcp.time.sleep
        taps: list[tuple[tuple[int, int], str]] = []
        try:
            mcp.xiaoluxue_native_tap = lambda serial, point, info, label, steps, started_at: taps.append((point, label))
            mcp.time.sleep = lambda seconds: None

            result = mcp.run_xiaoluxue_lesson_fast_path(
                "serial",
                {"action_name": "finish_result", "after_finish_wait_sec": 0},
                record=False,
            )
        finally:
            mcp.xiaoluxue_native_tap = original_tap
            mcp.time.sleep = original_sleep

        self.assertEqual(result["finish_result"]["action"], "finish_result")
        self.assertEqual(taps, [(mcp.XIAOLUXUE_NATIVE_RESULT_FINISH, "lesson:finish-result")])
        self.assertEqual(
            result["finish_result"]["finish_point"],
            {
                "base_x": mcp.XIAOLUXUE_NATIVE_RESULT_FINISH[0],
                "base_y": mcp.XIAOLUXUE_NATIVE_RESULT_FINISH[1],
            },
        )

    def test_xiaoluxue_chinese_1_5_route_preset_defaults_to_opening_module_popup(self) -> None:
        original_tap = mcp.xiaoluxue_native_tap
        original_sleep = mcp.time.sleep
        original_wait_lesson = mcp.xiaoluxue_wait_for_lesson_activity
        taps: list[tuple[str, tuple[int, int]]] = []
        try:
            mcp.xiaoluxue_native_tap = lambda serial, point, info, label, steps, started_at: taps.append((label, point))
            mcp.time.sleep = lambda seconds: None
            mcp.xiaoluxue_wait_for_lesson_activity = lambda serial, timeout_sec: {
                "focus": mcp.XIAOLUXUE_LESSON_ACTIVITY,
                "width": 2000,
                "height": 1200,
            }

            result = mcp.xiaoluxue_run_route_preset_map_fast_path(
                "serial",
                subject_id=1,
                index="1.5",
                action_name="practise",
                args={},
                steps=[],
                started_at=0.0,
                wait_after_select=0.08,
                open_report_when_done=False,
                report_wait_sec=0.32,
                window_info={
                    "focus": mcp.XIAOLUXUE_STUDY_SUBJECT_ACTIVITY,
                    "width": 2000,
                    "height": 1200,
                },
            )

            self.assertIsNotNone(result)
            self.assertFalse(result["entered_module"])
            self.assertEqual(
                taps,
                [
                    ("index:1.5:preset", (1508, 251)),
                    ("practise:preset", (1116, 401)),
                ],
            )
            self.assertIsNone(result["module_entry"])
        finally:
            mcp.xiaoluxue_native_tap = original_tap
            mcp.time.sleep = original_sleep
            mcp.xiaoluxue_wait_for_lesson_activity = original_wait_lesson

    def test_xiaoluxue_chinese_1_5_route_preset_enters_module_when_requested(self) -> None:
        original_tap = mcp.xiaoluxue_native_tap
        original_sleep = mcp.time.sleep
        original_wait_lesson = mcp.xiaoluxue_wait_for_lesson_activity
        taps: list[tuple[str, tuple[int, int]]] = []
        try:
            mcp.xiaoluxue_native_tap = lambda serial, point, info, label, steps, started_at: taps.append((label, point))
            mcp.time.sleep = lambda seconds: None
            mcp.xiaoluxue_wait_for_lesson_activity = lambda serial, timeout_sec: {
                "focus": mcp.XIAOLUXUE_LESSON_ACTIVITY,
                "width": 2000,
                "height": 1200,
            }

            result = mcp.xiaoluxue_run_route_preset_map_fast_path(
                "serial",
                subject_id=1,
                index="1.5",
                action_name="practise",
                args={"enter_module": True},
                steps=[],
                started_at=0.0,
                wait_after_select=0.08,
                open_report_when_done=False,
                report_wait_sec=0.32,
                window_info={
                    "focus": mcp.XIAOLUXUE_STUDY_SUBJECT_ACTIVITY,
                    "width": 2000,
                    "height": 1200,
                },
            )

            self.assertIsNotNone(result)
            self.assertTrue(result["entered_module"])
            self.assertEqual(
                taps,
                [
                    ("index:1.5:preset", (1508, 251)),
                    ("practise:preset", (1116, 401)),
                    ("practise:module-enter", (1116, 674)),
                ],
            )
            self.assertEqual(result["module_entry"]["focus_after_enter"], mcp.XIAOLUXUE_LESSON_ACTIVITY)
        finally:
            mcp.xiaoluxue_native_tap = original_tap
            mcp.time.sleep = original_sleep
            mcp.xiaoluxue_wait_for_lesson_activity = original_wait_lesson

    def test_lesson_answer_ready_stats_detects_native_answer_page(self) -> None:
        ready_raw = make_raw_screenshot(
            200,
            120,
            rects=[
                (5, 6, 42, 12, (60, 60, 60, 255)),
                (105, 18, 190, 24, (70, 70, 70, 255)),
                (105, 34, 190, 40, (70, 70, 70, 255)),
                (105, 50, 190, 56, (70, 70, 70, 255)),
            ],
        )
        blank_raw = make_raw_screenshot(200, 120)

        self.assertTrue(mcp.raw_screenshot_lesson_answer_stats(ready_raw)["ready"])
        self.assertFalse(mcp.raw_screenshot_lesson_answer_stats(blank_raw)["ready"])

    def test_lesson_card_list_stats_detects_challenge_card_list(self) -> None:
        card_raw = make_raw_screenshot(
            200,
            120,
            fill=(220, 245, 252, 255),
            rects=[
                (76, 8, 125, 13, (65, 65, 65, 255)),
                (62, 42, 129, 99, (255, 255, 255, 255)),
                (65, 88, 125, 98, (48, 180, 240, 255)),
            ],
        )
        answer_raw = make_raw_screenshot(
            200,
            120,
            rects=[
                (5, 6, 42, 12, (60, 60, 60, 255)),
                (105, 18, 190, 24, (70, 70, 70, 255)),
                (105, 34, 190, 40, (70, 70, 70, 255)),
            ],
        )

        self.assertTrue(mcp.raw_screenshot_lesson_card_list_stats(card_raw)["ready"])
        self.assertFalse(mcp.raw_screenshot_lesson_card_list_stats(answer_raw)["ready"])

    def test_xiaoluxue_map_snapshot_detects_selected_index(self) -> None:
        xml = """<hierarchy rotation="0">
  <node text="" content-desc="" resource-id="" class="android.widget.FrameLayout" bounds="[0,0][2000,1200]" clickable="false" enabled="true">
    <node text="语文" content-desc="" resource-id="com.xiaoluxue.ai.student:id/txt_subject_name" class="android.widget.TextView" bounds="[104,86][184,126]" clickable="false" enabled="true" />
    <node text="必修上第一单元" content-desc="" resource-id="com.xiaoluxue.ai.student:id/chapter_name" class="android.widget.TextView" bounds="[216,74][460,138]" clickable="false" enabled="true" />
    <node text="" content-desc="" resource-id="" class="android.widget.FrameLayout" bounds="[851,453][1149,866]" clickable="true" enabled="true">
      <node text="" content-desc="" resource-id="com.xiaoluxue.ai.student:id/practiseItem" class="android.view.ViewGroup" bounds="[914,344][1086,496]" clickable="true" enabled="true" />
      <node text="题型突破" content-desc="" resource-id="com.xiaoluxue.ai.student:id/title" class="android.widget.TextView" bounds="[948,420][1052,457]" clickable="false" enabled="true" />
      <node text="" content-desc="" resource-id="com.xiaoluxue.ai.student:id/expandItem" class="android.view.ViewGroup" bounds="[1112,536][1284,688]" clickable="true" enabled="true" />
      <node text="专属精练" content-desc="" resource-id="com.xiaoluxue.ai.student:id/title" class="android.widget.TextView" bounds="[1146,612][1250,649]" clickable="false" enabled="true" />
      <node text="1.5" content-desc="" resource-id="com.xiaoluxue.ai.student:id/index" class="android.widget.TextView" bounds="[872,667][1128,705]" clickable="false" enabled="true" />
      <node text="" content-desc="" resource-id="com.xiaoluxue.ai.student:id/wrong_textbook" class="android.widget.FrameLayout" bounds="[851,781][1000,866]" clickable="true" enabled="true" />
      <node text="笔记本" content-desc="" resource-id="com.xiaoluxue.ai.student:id/textbook_text" class="android.widget.TextView" bounds="[1008,784][1141,853]" clickable="false" enabled="true" />
    </node>
  </node>
</hierarchy>"""
        nodes = mcp.parse_ui_nodes(xml)
        snapshot = mcp.xiaoluxue_map_snapshot_from_observation(
            {"state": {"focused_window": "com.xiaoluxue.ai.student/com.xiaoluxue.ai.business.launcher.study.subject.StudySubjectActivity"}, "ui": {"nodes": nodes}}
        )

        self.assertTrue(snapshot["is_map"])
        self.assertEqual(snapshot["subject"], "语文")
        self.assertEqual(snapshot["chapter"], "必修上第一单元")
        self.assertEqual(snapshot["selected_index"], "1.5")
        self.assertEqual(snapshot["visible_indexes"], ["1.5"])
        self.assertTrue(snapshot["visible_actions"]["practise"])
        self.assertTrue(snapshot["visible_actions"]["expand"])
        self.assertTrue(snapshot["visible_actions"]["wrong"])
        index_node = mcp.find_xiaoluxue_map_index_node(nodes, "1.5")
        self.assertEqual(mcp.xiaoluxue_map_predicted_action_point(index_node, "practise"), {"x": 1000, "y": 420})
        self.assertEqual(mcp.xiaoluxue_map_predicted_action_point(index_node, "expand"), {"x": 1198, "y": 612})

    def test_select_webview_page_prefers_visible_attached_page(self) -> None:
        pages = [
            {
                "id": "older",
                "type": "page",
                "title": "小鹿爱学",
                "url": "https://stu.xiaoluxue.com/course?a=1",
                "descriptionParsed": {"visible": False, "attached": False, "empty": False},
                "webSocketDebuggerUrl": "ws://127.0.0.1:9223/devtools/page/older",
            },
            {
                "id": "current",
                "type": "page",
                "title": "小鹿爱学",
                "url": "https://stu.xiaoluxue.com/course?a=1",
                "descriptionParsed": {"visible": True, "attached": True, "empty": False},
                "webSocketDebuggerUrl": "ws://127.0.0.1:9223/devtools/page/current",
            },
        ]

        page = mcp.select_webview_page(pages, url_contains="stu.xiaoluxue.com/course", title_contains="小鹿爱学")

        self.assertEqual(page["id"], "current")

    def test_select_xiaoluxue_webview_page_supports_test_env(self) -> None:
        pages = [
            {
                "id": "exercise",
                "type": "page",
                "title": "小鹿爱学",
                "url": "http://stu.test.xiaoluxue.cn/exercise?studySessionId=1",
                "descriptionParsed": {"visible": True, "attached": True, "empty": False},
                "webSocketDebuggerUrl": "ws://127.0.0.1:9223/devtools/page/exercise",
            },
            {
                "id": "course",
                "type": "page",
                "title": "小鹿爱学",
                "url": "http://stu.test.xiaoluxue.cn/course?knowledgeId=1",
                "descriptionParsed": {"visible": False, "attached": True, "empty": False},
                "webSocketDebuggerUrl": "ws://127.0.0.1:9223/devtools/page/course",
            },
        ]

        self.assertEqual(mcp.select_xiaoluxue_webview_page(pages, "exercise")["id"], "exercise")
        self.assertEqual(mcp.select_xiaoluxue_webview_page(pages, "course")["id"], "course")

    def test_scrcpy_visible_process_ignores_python_command_text(self) -> None:
        original = mcp.host_process_lines
        try:
            mcp.host_process_lines = lambda: [
                '123 Python -c "serial=\\"ANMB9X5A10G00857\\"; print(\\"scrcpy\\")"',
                "456 /usr/bin/python3 /tmp/scripts/scrcpy_supervisor.py -- scrcpy --serial ANMB9X5A10G00857",
            ]

            process = mcp.scrcpy_visible_process_for_serial("ANMB9X5A10G00857")
        finally:
            mcp.host_process_lines = original

        self.assertIsNotNone(process)
        self.assertIn("scrcpy_supervisor.py", process or "")

    def test_start_scrcpy_reuses_existing_visible_window(self) -> None:
        original_choose = mcp.choose_serial
        original_app_visible = mcp.scrcpy_app_wrapper_process_for_serial
        original_prune = mcp.prune_duplicate_scrcpy_processes
        original_system_app = mcp.ensure_system_android_launcher_app
        try:
            mcp.choose_serial = lambda _serial=None: "device-1"
            mcp.scrcpy_app_wrapper_process_for_serial = lambda _serial: "123 scrcpy --serial device-1"
            mcp.prune_duplicate_scrcpy_processes = lambda _serial: [100]
            mcp.ensure_system_android_launcher_app = lambda: {"ok": True, "skipped": "already-present"}  # type: ignore[assignment]

            content = mcp.tool_start_scrcpy({"serial": "device-1"})
        finally:
            mcp.choose_serial = original_choose
            mcp.scrcpy_app_wrapper_process_for_serial = original_app_visible
            mcp.prune_duplicate_scrcpy_processes = original_prune
            mcp.ensure_system_android_launcher_app = original_system_app  # type: ignore[assignment]

        payload = json.loads(content[0]["text"])
        self.assertEqual(payload["skipped"], "already-running")
        self.assertEqual(payload["stopped_duplicate_pids"], [100])
        self.assertEqual(payload["system_app"]["skipped"], "already-present")

    def test_start_scrcpy_launches_through_app_wrapper(self) -> None:
        original_choose = mcp.choose_serial
        original_app_visible = mcp.scrcpy_app_wrapper_process_for_serial
        original_prune = mcp.prune_duplicate_scrcpy_processes
        original_clear = mcp.clear_scrcpy_user_closed
        original_start_app = mcp.start_scrcpy_app_window
        original_system_app = mcp.ensure_system_android_launcher_app
        calls: list[dict[str, object]] = []
        try:
            mcp.choose_serial = lambda _serial=None: "device-1"
            mcp.scrcpy_app_wrapper_process_for_serial = lambda _serial: None
            mcp.prune_duplicate_scrcpy_processes = lambda _serial: []
            mcp.clear_scrcpy_user_closed = lambda _serial: None
            mcp.ensure_system_android_launcher_app = lambda: {"ok": True, "created": True}  # type: ignore[assignment]

            def fake_start_app(args: dict[str, object], serial: str) -> dict[str, object]:
                calls.append({"args": dict(args), "serial": serial})
                return {
                    "ok": True,
                    "serial": serial,
                    "launch_mode": "macos_app",
                    "command": ["scrcpy", "--serial", serial],
                }

            mcp.start_scrcpy_app_window = fake_start_app  # type: ignore[assignment]

            content = mcp.tool_start_scrcpy({"serial": "device-1", "keep_alive": True})
        finally:
            mcp.choose_serial = original_choose
            mcp.scrcpy_app_wrapper_process_for_serial = original_app_visible
            mcp.prune_duplicate_scrcpy_processes = original_prune
            mcp.clear_scrcpy_user_closed = original_clear
            mcp.start_scrcpy_app_window = original_start_app  # type: ignore[assignment]
            mcp.ensure_system_android_launcher_app = original_system_app  # type: ignore[assignment]

        payload = json.loads(content[0]["text"])
        self.assertEqual(payload["launch_mode"], "macos_app")
        self.assertFalse(payload["keep_alive"])
        self.assertTrue(payload["system_app"]["created"])
        self.assertEqual(calls[0]["serial"], "device-1")
        args = calls[0]["args"]
        self.assertEqual(args["max_size"], 0)
        self.assertEqual(args["window_scale"], 0.5)
        self.assertEqual(args["render_driver"], "software")

    def test_map_openai_computer_actions(self) -> None:
        screen = {"width": 1440, "height": 2560}
        click = mcp.map_openai_computer_action({"type": "click", "x": 100, "y": 200}, screen)
        drag = mcp.map_openai_computer_action(
            {"type": "drag", "path": [{"x": 100, "y": 900}, {"x": 100, "y": 500}]},
            screen,
        )
        scroll = mcp.map_openai_computer_action({"type": "scroll", "x": 700, "y": 1200, "scroll_y": 500}, screen)

        self.assertEqual(click["action"], "tap")
        self.assertEqual(drag["action"], "swipe")
        self.assertLess(scroll["end_y"], scroll["start_y"])

    def test_build_scrcpy_command_defaults_to_video_only(self) -> None:
        command, width, height, title = mcp.build_scrcpy_command(
            {"fixed_window": False, "window_title": "Debug"},
            "device-1",
        )

        self.assertIn("--no-audio", command)
        self.assertNotIn("--no-window", command)
        self.assertIn("--keyboard", command)
        self.assertIn("sdk", command)
        self.assertIn("--prefer-text", command)
        self.assertEqual(width, None)
        self.assertEqual(height, None)
        self.assertEqual(title, "Debug")

    def test_build_scrcpy_command_can_enable_audio(self) -> None:
        command, _width, _height, _title = mcp.build_scrcpy_command(
            {"fixed_window": False, "audio": True},
            "device-1",
        )

        self.assertNotIn("--no-audio", command)

    def test_build_scrcpy_command_can_disable_text_preference_for_hid_keyboard(self) -> None:
        command, _width, _height, _title = mcp.build_scrcpy_command(
            {"fixed_window": False, "keyboard": "uhid", "prefer_text": True},
            "device-1",
        )

        self.assertIn("uhid", command)
        self.assertNotIn("--prefer-text", command)

    def test_start_video_recording_launches_scrcpy_no_window(self) -> None:
        original_choose = mcp.choose_serial
        original_scrcpy = mcp.scrcpy_binary
        original_popen = mcp.subprocess.Popen
        original_sleep = mcp.time.sleep
        original_state_path = mcp.video_recording_state_path
        calls: list[list[str]] = []

        class FakeProcess:
            pid = 4321

            def poll(self) -> int | None:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            try:
                mcp.SCRCPY_VIDEO_RECORDINGS.clear()
                mcp.SCRCPY_VIDEO_RECORDING_PROCESSES.clear()
                mcp.choose_serial = lambda _serial=None: "device-1"
                mcp.scrcpy_binary = lambda: "scrcpy"
                mcp.time.sleep = lambda _seconds: (_ for _ in ()).throw(
                    AssertionError("start_video_recording should not use a fixed startup sleep")
                )
                mcp.video_recording_state_path = lambda _serial: tmp_path / "active.json"

                def fake_popen(command: list[str], **_kwargs: object) -> FakeProcess:
                    calls.append(command)
                    return FakeProcess()

                mcp.subprocess.Popen = fake_popen  # type: ignore[assignment]

                output_path = tmp_path / "capture.mp4"
                content = mcp.tool_start_video_recording(
                    {
                        "serial": "device-1",
                        "output_path": str(output_path),
                        "max_size": 720,
                        "bit_rate": "4M",
                        "start_marker": False,
                    }
                )
            finally:
                mcp.choose_serial = original_choose
                mcp.scrcpy_binary = original_scrcpy
                mcp.subprocess.Popen = original_popen  # type: ignore[assignment]
                mcp.time.sleep = original_sleep
                mcp.video_recording_state_path = original_state_path
                mcp.SCRCPY_VIDEO_RECORDINGS.clear()
                mcp.SCRCPY_VIDEO_RECORDING_PROCESSES.clear()

        payload = json.loads(content[0]["text"])
        self.assertEqual(payload["file_path"], str(output_path))
        self.assertEqual(payload["metadata_path"], str(output_path.with_suffix(".mp4.json")))
        self.assertFalse(payload["start_anchor"]["enabled"])
        self.assertEqual(payload["start_anchor"]["status"], "disabled")
        command = calls[0]
        self.assertIn("--no-window", command)
        self.assertIn("--record", command)
        self.assertIn(str(output_path), command)
        self.assertIn("--record-format", command)
        self.assertIn("mp4", command)
        self.assertIn("-m", command)
        self.assertIn("720", command)
        self.assertIn("-b", command)
        self.assertIn("4M", command)
        self.assertIn("--no-audio", command)

    def test_start_video_recording_schedules_start_anchor_without_blocking(self) -> None:
        original_choose = mcp.choose_serial
        original_scrcpy = mcp.scrcpy_binary
        original_popen = mcp.subprocess.Popen
        original_marker = mcp.schedule_video_recording_start_marker
        original_state_path = mcp.video_recording_state_path
        scheduled: list[tuple[str, Path, Path]] = []

        class FakeProcess:
            pid = 4321

            def poll(self) -> int | None:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            try:
                mcp.SCRCPY_VIDEO_RECORDINGS.clear()
                mcp.SCRCPY_VIDEO_RECORDING_PROCESSES.clear()
                mcp.choose_serial = lambda _serial=None: "device-1"
                mcp.scrcpy_binary = lambda: "scrcpy"
                mcp.video_recording_state_path = lambda _serial: tmp_path / "active.json"

                def fake_popen(command: list[str], **_kwargs: object) -> FakeProcess:
                    return FakeProcess()

                def fake_marker(
                    serial: str,
                    marker_path: Path,
                    metadata_path: Path,
                    **_kwargs: object,
                ) -> None:
                    scheduled.append((serial, marker_path, metadata_path))

                mcp.subprocess.Popen = fake_popen  # type: ignore[assignment]
                mcp.schedule_video_recording_start_marker = fake_marker  # type: ignore[assignment]

                output_path = tmp_path / "capture.mp4"
                content = mcp.tool_start_video_recording(
                    {"serial": "device-1", "output_path": str(output_path)}
                )
                metadata = json.loads(output_path.with_suffix(".mp4.json").read_text())
            finally:
                mcp.choose_serial = original_choose
                mcp.scrcpy_binary = original_scrcpy
                mcp.subprocess.Popen = original_popen  # type: ignore[assignment]
                mcp.schedule_video_recording_start_marker = original_marker  # type: ignore[assignment]
                mcp.video_recording_state_path = original_state_path
                mcp.SCRCPY_VIDEO_RECORDINGS.clear()
                mcp.SCRCPY_VIDEO_RECORDING_PROCESSES.clear()

        payload = json.loads(content[0]["text"])
        metadata_path = output_path.with_suffix(".mp4.json")
        marker_path = output_path.with_suffix(".mp4.start.png")
        self.assertEqual(payload["metadata_path"], str(metadata_path))
        self.assertEqual(payload["start_anchor"]["status"], "pending")
        self.assertEqual(payload["start_anchor"]["path"], str(marker_path))
        self.assertIn("startup_probe_ms", payload["timing"])
        self.assertEqual(scheduled, [("device-1", marker_path, metadata_path)])
        self.assertEqual(metadata["start_anchor"]["status"], "pending")
        self.assertEqual(metadata["start_anchor"]["path"], str(marker_path))

    def test_stop_video_recording_returns_markdown_path(self) -> None:
        original_choose = mcp.choose_serial
        original_state_path = mcp.video_recording_state_path
        original_time = mcp.time.time

        class FakeProcess:
            pid = 4321
            signal_seen: int | None = None

            def poll(self) -> int | None:
                return None

            def send_signal(self, sig: int) -> None:
                self.signal_seen = sig

            def wait(self, timeout: float | None = None) -> int:
                return 0

            def terminate(self) -> None:
                raise AssertionError("terminate should not be needed")

            def kill(self) -> None:
                raise AssertionError("kill should not be needed")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_path = tmp_path / "capture.mp4"
            metadata_path = output_path.with_suffix(".mp4.json")
            output_path.write_bytes(b"mp4")
            metadata_path.write_text(
                json.dumps(
                    {
                        "start_anchor": {
                            "status": "captured",
                            "path": str(output_path.with_suffix(".mp4.start.png")),
                        },
                        "timing": {"startup_probe_ms": 1.5},
                    }
                )
            )
            process = FakeProcess()
            try:
                mcp.SCRCPY_VIDEO_RECORDINGS.clear()
                mcp.SCRCPY_VIDEO_RECORDING_PROCESSES.clear()
                mcp.choose_serial = lambda _serial=None: "device-1"
                mcp.video_recording_state_path = lambda _serial: tmp_path / "active.json"
                mcp.time.time = lambda: 103.0
                mcp.SCRCPY_VIDEO_RECORDINGS["device-1"] = {
                    "serial": "device-1",
                    "pid": process.pid,
                    "file_path": str(output_path),
                    "metadata_path": str(metadata_path),
                    "log_path": str(tmp_path / "capture.mp4.log"),
                    "started_at_epoch": 100.0,
                }
                mcp.SCRCPY_VIDEO_RECORDING_PROCESSES["device-1"] = process  # type: ignore[assignment]

                content = mcp.tool_stop_video_recording({"serial": "device-1"})
            finally:
                mcp.choose_serial = original_choose
                mcp.video_recording_state_path = original_state_path
                mcp.time.time = original_time
                mcp.SCRCPY_VIDEO_RECORDINGS.clear()
                mcp.SCRCPY_VIDEO_RECORDING_PROCESSES.clear()

        payload = json.loads(content[0]["text"])
        self.assertTrue(payload["stopped"])
        self.assertEqual(payload["file_path"], str(output_path))
        self.assertEqual(payload["metadata_path"], str(metadata_path))
        self.assertEqual(payload["start_anchor"]["status"], "captured")
        self.assertEqual(payload["timing"]["startup_probe_ms"], 1.5)
        self.assertEqual(payload["size_bytes"], 3)
        self.assertEqual(payload["duration_sec"], 3.0)
        self.assertEqual(payload["markdown"], f"![android-video-recording]({output_path})")
        self.assertEqual(process.signal_seen, mcp.signal.SIGINT)

    def test_android_device_display_name_prefers_device_name(self) -> None:
        original_shell = mcp.shell
        original_get_prop = mcp.get_prop
        try:
            mcp.shell = lambda serial, command, timeout=30: "荣耀平板Z6" if "settings get global" in command else ""
            mcp.get_prop = lambda serial, prop: "ELN-W09"

            self.assertEqual(mcp.android_device_display_name("device-1"), "荣耀平板Z6")
        finally:
            mcp.shell = original_shell
            mcp.get_prop = original_get_prop

    def test_android_device_display_name_falls_back_to_model(self) -> None:
        original_shell = mcp.shell
        original_get_prop = mcp.get_prop
        try:
            mcp.shell = lambda serial, command, timeout=30: "null"
            mcp.get_prop = lambda serial, prop: "ELN-W09" if prop == "ro.product.model" else None

            self.assertEqual(mcp.android_device_display_name("device-1"), "ELN-W09")
        finally:
            mcp.shell = original_shell
            mcp.get_prop = original_get_prop

    def test_android_device_display_name_falls_back_to_android(self) -> None:
        original_shell = mcp.shell
        original_get_prop = mcp.get_prop
        try:
            mcp.shell = lambda serial, command, timeout=30: "null"
            mcp.get_prop = lambda serial, prop: None

            self.assertEqual(mcp.android_device_display_name("device-1"), "Android")
        finally:
            mcp.shell = original_shell
            mcp.get_prop = original_get_prop

    def test_android_scrcpy_app_path_uses_stable_launcher_name(self) -> None:
        original_env = mcp.os.environ.get("ANDROID_USE_SCRCPY_APP_PATH")
        try:
            mcp.os.environ.pop("ANDROID_USE_SCRCPY_APP_PATH", None)

            path = mcp.android_scrcpy_app_path({}, "荣耀平板Z6")
        finally:
            if original_env is None:
                mcp.os.environ.pop("ANDROID_USE_SCRCPY_APP_PATH", None)
            else:
                mcp.os.environ["ANDROID_USE_SCRCPY_APP_PATH"] = original_env

        self.assertEqual(path.name, "Android Use.app")

    def test_build_scrcpy_app_launch_command_opens_app_path_directly(self) -> None:
        command = mcp.build_scrcpy_app_launch_command(
            ["scrcpy", "--serial", "device-1"],
            Path("/tmp/Android Use.app"),
        )

        self.assertEqual(command[:3], ["open", "-n", "/tmp/Android Use.app"])
        self.assertIn("--args", command)
        self.assertIn("--scrcpy", command)

    def test_cleanup_legacy_android_scrcpy_apps_removes_same_bundle_only(self) -> None:
        original_dir = mcp.ANDROID_USE_DIR

        def write_plist(app_dir: Path, bundle_id: str) -> None:
            contents_dir = app_dir / "Contents"
            contents_dir.mkdir(parents=True)
            with (contents_dir / "Info.plist").open("wb") as file:
                mcp.plistlib.dump({"CFBundleIdentifier": bundle_id}, file)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            current_app = tmp_path / "Android Use.app"
            legacy_app = tmp_path / "荣耀平板Z6.app"
            foreign_app = tmp_path / "Other.app"
            write_plist(current_app, mcp.ANDROID_USE_BUNDLE_ID)
            write_plist(legacy_app, mcp.ANDROID_USE_BUNDLE_ID)
            write_plist(foreign_app, "com.example.other")
            try:
                mcp.ANDROID_USE_DIR = tmp_path

                removed = mcp.cleanup_legacy_android_scrcpy_apps(current_app)
            finally:
                mcp.ANDROID_USE_DIR = original_dir

            self.assertEqual(removed, [str(legacy_app)])
            self.assertTrue(current_app.exists())
            self.assertFalse(legacy_app.exists())
            self.assertTrue(foreign_app.exists())

    def test_system_android_launcher_path_defaults_to_applications(self) -> None:
        original_path = mcp.os.environ.get("ANDROID_USE_SYSTEM_ANDROID_APP_PATH")
        original_dir = mcp.os.environ.get("ANDROID_USE_SYSTEM_APPLICATIONS_DIR")
        try:
            mcp.os.environ.pop("ANDROID_USE_SYSTEM_ANDROID_APP_PATH", None)
            mcp.os.environ.pop("ANDROID_USE_SYSTEM_APPLICATIONS_DIR", None)

            path = mcp.system_android_launcher_app_path()
        finally:
            if original_path is None:
                mcp.os.environ.pop("ANDROID_USE_SYSTEM_ANDROID_APP_PATH", None)
            else:
                mcp.os.environ["ANDROID_USE_SYSTEM_ANDROID_APP_PATH"] = original_path
            if original_dir is None:
                mcp.os.environ.pop("ANDROID_USE_SYSTEM_APPLICATIONS_DIR", None)
            else:
                mcp.os.environ["ANDROID_USE_SYSTEM_APPLICATIONS_DIR"] = original_dir

        self.assertEqual(path, Path("/Applications/Android Use.app"))

    def test_ensure_system_android_launcher_app_skips_existing(self) -> None:
        original_platform = mcp.sys.platform
        original_path = mcp.os.environ.get("ANDROID_USE_SYSTEM_ANDROID_APP_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "Android Use.app"
            app_path.mkdir()
            try:
                mcp.sys.platform = "darwin"
                mcp.os.environ["ANDROID_USE_SYSTEM_ANDROID_APP_PATH"] = str(app_path)

                result = mcp.ensure_system_android_launcher_app()
            finally:
                mcp.sys.platform = original_platform
                if original_path is None:
                    mcp.os.environ.pop("ANDROID_USE_SYSTEM_ANDROID_APP_PATH", None)
                else:
                    mcp.os.environ["ANDROID_USE_SYSTEM_ANDROID_APP_PATH"] = original_path

        self.assertTrue(result["ok"])
        self.assertEqual(result["skipped"], "already-present")
        self.assertEqual(result["app_path"], str(app_path))

    def test_ensure_system_android_launcher_app_creates_missing(self) -> None:
        original_platform = mcp.sys.platform
        original_path = mcp.os.environ.get("ANDROID_USE_SYSTEM_ANDROID_APP_PATH")
        original_builder = mcp.build_android_scrcpy_app
        calls: list[tuple[Path, str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "Android Use.app"
            try:
                mcp.sys.platform = "darwin"
                mcp.os.environ["ANDROID_USE_SYSTEM_ANDROID_APP_PATH"] = str(app_path)

                def fake_builder(path: Path, app_name: str = "Android") -> Path:
                    calls.append((path, app_name))
                    return path

                mcp.build_android_scrcpy_app = fake_builder  # type: ignore[assignment]

                result = mcp.ensure_system_android_launcher_app()
            finally:
                mcp.sys.platform = original_platform
                mcp.build_android_scrcpy_app = original_builder  # type: ignore[assignment]
                if original_path is None:
                    mcp.os.environ.pop("ANDROID_USE_SYSTEM_ANDROID_APP_PATH", None)
                else:
                    mcp.os.environ["ANDROID_USE_SYSTEM_ANDROID_APP_PATH"] = original_path

        self.assertTrue(result["ok"])
        self.assertTrue(result["created"])
        self.assertEqual(calls, [(app_path, "Android Use")])

    def test_build_scrcpy_command_defaults_to_device_name_title(self) -> None:
        original_name = mcp.android_device_display_name
        try:
            mcp.android_device_display_name = lambda serial: "荣耀平板Z6"

            command, _width, _height, title = mcp.build_scrcpy_command({"fixed_window": False}, "device-1")
        finally:
            mcp.android_device_display_name = original_name

        self.assertEqual(title, "荣耀平板Z6")
        self.assertEqual(command[command.index("--window-title") + 1], "荣耀平板Z6")

    def test_build_scrcpy_command_can_half_size_initial_window_without_video_scale(self) -> None:
        original_screenshot = mcp.screenshot_png
        try:
            mcp.screenshot_png = lambda serial: make_png(2000, 1200)

            command, width, height, _title = mcp.build_scrcpy_command(
                {"max_size": 0, "window_scale": 0.5, "window_title": "Debug"},
                "device-1",
            )
        finally:
            mcp.screenshot_png = original_screenshot

        self.assertNotIn("-m", command)
        self.assertEqual(width, 1000)
        self.assertEqual(height, 600)
        self.assertEqual(command[command.index("--window-width") + 1], "1000")
        self.assertEqual(command[command.index("--window-height") + 1], "600")

    def test_start_scrcpy_app_descriptor_uses_android_use_bundle(self) -> None:
        tool = next(tool for tool in mcp.tool_descriptors() if tool["name"] == "android_start_scrcpy_app")
        properties = tool["inputSchema"]["properties"]

        self.assertEqual(properties["max_size"]["default"], 0)
        self.assertEqual(properties["window_scale"]["default"], 0.5)
        self.assertEqual(properties["render_driver"]["default"], "software")
        self.assertIn("window_title", properties)
