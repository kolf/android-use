#!/usr/bin/env python3
"""Offline tests for the Android Use MCP server helpers."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import android_use_mcp as mcp


class AndroidUseMcpTests(unittest.TestCase):
    def test_parse_adb_devices(self) -> None:
        output = """List of devices attached
emulator-5554 device product:sdk_gphone64_arm64 model:sdk_gphone64_arm64 device:emu64a transport_id:1
abc123 unauthorized usb:336592896X
"""
        devices = mcp.parse_adb_devices(output)

        self.assertEqual(devices[0]["serial"], "emulator-5554")
        self.assertEqual(devices[0]["state"], "device")
        self.assertEqual(devices[0]["details"]["model"], "sdk_gphone64_arm64")
        self.assertEqual(devices[1]["state"], "unauthorized")

    def test_parse_screen_size_prefers_override(self) -> None:
        size = mcp.parse_screen_size("Physical size: 1080x2400\nOverride size: 720x1600")

        self.assertEqual(size, {"width": 720, "height": 1600})

    def test_keycode_normalization(self) -> None:
        self.assertEqual(mcp.keycode("back"), "KEYCODE_BACK")
        self.assertEqual(mcp.keycode("KEYCODE_HOME"), "KEYCODE_HOME")
        self.assertEqual(mcp.keycode(66), "66")
        self.assertEqual(mcp.keycode("camera"), "KEYCODE_CAMERA")

    def test_extract_json_object_from_markdown(self) -> None:
        action = mcp.extract_json_object('```json\n{"action":"tap","x":1,"y":2}\n```')

        self.assertEqual(action["action"], "tap")
        self.assertEqual(action["x"], 1)

    def test_parse_tars_click_absolute(self) -> None:
        action = mcp.parse_tars_action_response(
            "Thought: tap the tab\nAction: click(point='430 2465')",
            {"width": 1440, "height": 2560},
            coordinate_mode="absolute",
        )

        self.assertEqual(action["action"], "tap")
        self.assertEqual(action["x"], 430)
        self.assertEqual(action["y"], 2465)

    def test_parse_tars_click_normalized(self) -> None:
        action = mcp.parse_tars_action_response(
            "Thought: tap the tab\nAction: click(point='500 900')",
            {"width": 1440, "height": 2560},
            coordinate_mode="normalized_1000",
        )

        self.assertEqual(action["action"], "tap")
        self.assertEqual(action["x"], 720)
        self.assertEqual(action["y"], 2304)

    def test_parse_tars_mobile_actions(self) -> None:
        typed = mcp.parse_tars_action_response(
            "Thought: submit\nAction: type(content='hello\\n')",
            {"width": 1440, "height": 2560},
        )
        back = mcp.parse_tars_action_response("Thought: go back\nAction: press_back()", {"width": 1, "height": 1})
        finished = mcp.parse_tars_action_response(
            "Thought: done\nAction: finished(content='完成')",
            {"width": 1, "height": 1},
        )

        self.assertEqual(typed["action"], "type_text")
        self.assertTrue(typed["enter"])
        self.assertEqual(back["key"], "BACK")
        self.assertEqual(finished["action"], "done")

    def test_parse_ui_nodes_and_find_text(self) -> None:
        xml = """<hierarchy rotation="0">
  <node text="" content-desc="" resource-id="" class="android.widget.FrameLayout" bounds="[0,0][1440,2560]" clickable="false" enabled="true">
    <node text="首页" content-desc="" resource-id="com.example:id/home" class="android.widget.TextView" bounds="[80,2400][220,2520]" clickable="true" enabled="true" selected="true" />
    <node text="发现" content-desc="" resource-id="com.example:id/discover" class="android.widget.TextView" bounds="[920,2400][1080,2520]" clickable="true" enabled="true" selected="false" />
  </node>
</hierarchy>"""
        nodes = mcp.parse_ui_nodes(xml)
        node = mcp.find_ui_node(nodes, "发现")
        point = mcp.node_click_point(node)

        self.assertIsNotNone(node)
        self.assertEqual(point, {"x": 1000, "y": 2460})
        self.assertEqual(mcp.find_node_by_selector(nodes, {"strategy": "resource_id", "value": "com.example:id/discover"}), node)

    def test_parse_webview_devtools_sockets_deduplicates(self) -> None:
        proc_net_unix = """
0000000000000000: 00000002 00000000 00010000 0001 01 30085153 @webview_devtools_remote_twe_32675
0000000000000000: 00000003 00000000 00000000 0001 03 30856838 @webview_devtools_remote_twe_32675
0000000000000000: 00000002 00000000 00010000 0001 01 30085154 @webview_devtools_remote_123
"""

        sockets = mcp.parse_webview_devtools_sockets(proc_net_unix)

        self.assertEqual(sockets, ["webview_devtools_remote_twe_32675", "webview_devtools_remote_123"])

    def test_normalize_xiaoluxue_knowledge_index(self) -> None:
        self.assertEqual(mcp.normalize_xiaoluxue_knowledge_index("1.1.11"), "1111")
        self.assertEqual(mcp.normalize_xiaoluxue_knowledge_index("1.1.1.1"), "1111")

    def test_xiaoluxue_app_only_url_detection(self) -> None:
        self.assertTrue(mcp.is_xiaoluxue_app_only_url("https://stu.xiaoluxue.com/course?knowledgeId=3785"))
        self.assertTrue(mcp.is_xiaoluxue_app_only_url("http://stu.test.xiaoluxue.cn/exercise?studySessionId=1"))
        self.assertTrue(mcp.is_xiaoluxue_app_only_url("https://gw-stu.test.xiaoluxue.cn/path"))
        self.assertFalse(mcp.is_xiaoluxue_app_only_url("https://example.com/course"))
        self.assertFalse(mcp.is_xiaoluxue_app_only_url("xlx://router/vessel/webview"))

    def test_xiaoluxue_url_kind_supports_test_hosts(self) -> None:
        self.assertEqual(mcp.xiaoluxue_url_kind("http://stu.test.xiaoluxue.cn/course?knowledgeId=1"), "course")
        self.assertEqual(mcp.xiaoluxue_url_kind("http://stu.test.xiaoluxue.cn/exercise?studySessionId=1"), "exercise")
        self.assertEqual(mcp.xiaoluxue_url_kind("https://stu.xiaoluxue.com/"), "any")
        self.assertIsNone(mcp.xiaoluxue_url_kind("https://example.com/course"))

    def test_xiaoluxue_vessel_route_quotes_target(self) -> None:
        route = mcp.xiaoluxue_vessel_webview_url("https://gw-stu.test.xiaoluxue.cn/course?a=1&b=数学")

        self.assertTrue(route.startswith("xlx://router/vessel/webview?url="))
        self.assertIn("gw-stu.test.xiaoluxue.cn%2Fcourse", route)
        self.assertIn("full_screen=true", route)
        self.assertIn("title_bar=false", route)

    def test_xiaoluxue_runtime_url_matches_course_identity(self) -> None:
        target = "https://stu.xiaoluxue.com/course?knowledgeId=3785&lessonId=1"
        current = "https://stu.xiaoluxue.com/course?lessonId=1&knowledgeId=3785&redirectWidgetIndex=4"

        self.assertTrue(mcp.xiaoluxue_runtime_url_matches(target, current))
        self.assertFalse(mcp.xiaoluxue_runtime_url_matches(target, "https://stu.xiaoluxue.com/course?knowledgeId=9999&lessonId=1"))

    def test_xiaoluxue_rebase_h5_url_uses_current_test_host(self) -> None:
        target = "https://stu.xiaoluxue.com/course?knowledgeId=3785&lessonId=1"
        current = "http://stu.test.xiaoluxue.cn/exercise?studySessionId=1"

        self.assertEqual(
            mcp.xiaoluxue_rebase_h5_url(target, current),
            "http://stu.test.xiaoluxue.cn/course?knowledgeId=3785&lessonId=1",
        )

    def test_xiaoluxue_native_scaled_point(self) -> None:
        self.assertEqual(
            mcp.xiaoluxue_native_scaled_point((690, 280), {"width": 2000, "height": 1200}),
            (690, 280),
        )
        self.assertEqual(
            mcp.xiaoluxue_native_scaled_point((690, 280), {"width": 1000, "height": 600}),
            (345, 140),
        )

    def test_xiaoluxue_map_instruction_and_action_normalization(self) -> None:
        self.assertTrue(mcp.xiaoluxue_instruction_looks_like_map("进入 1.5 题型突破"))
        self.assertFalse(mcp.xiaoluxue_instruction_looks_like_map("首页 -> 数学 1.1.11 知识讲解 2x"))
        self.assertEqual(mcp.normalize_xiaoluxue_map_index("进入 1.5 题型突破"), "1.5")
        self.assertEqual(mcp.normalize_xiaoluxue_map_action("打开 1.5 错题"), "wrong")
        self.assertEqual(mcp.normalize_xiaoluxue_map_action("打开 1.5 笔记本"), "notebook")
        self.assertEqual(mcp.normalize_xiaoluxue_map_action("看报告"), "report")
        self.assertEqual(mcp.normalize_xiaoluxue_subject_id("进入语文 1.5 题型突破"), 1)
        self.assertEqual(mcp.normalize_xiaoluxue_subject_id("打开 subject_id=2 地图"), 2)
        self.assertEqual(
            mcp.xiaoluxue_study_subject_route_url(1),
            "xlx://router/study/subject?subject_id=1",
        )
        self.assertEqual(
            mcp.xiaoluxue_map_fast_action_from_instruction("进入语文 1.5 题型突破"),
            {
                "action": "xiaoluxue_map_fast_path",
                "instruction": "进入语文 1.5 题型突破",
                "action_name": "practise",
                "source": "xiaoluxue-native-map",
                "index": "1.5",
                "subject_id": 1,
                "route_if_subject": True,
            },
        )

    def test_xiaoluxue_map_snapshot_detects_selected_index(self) -> None:
        xml = """<hierarchy rotation="0">
  <node text="" content-desc="" resource-id="" class="android.widget.FrameLayout" bounds="[0,0][2000,1200]" clickable="false" enabled="true">
    <node text="语文" content-desc="" resource-id="com.xiaoluxue.ai.student:id/txt_subject_name" class="android.widget.TextView" bounds="[104,86][184,126]" clickable="false" enabled="true" />
    <node text="必修上第一单元" content-desc="" resource-id="com.xiaoluxue.ai.student:id/chapter_name" class="android.widget.TextView" bounds="[216,74][460,138]" clickable="false" enabled="true" />
    <node text="" content-desc="" resource-id="" class="android.widget.FrameLayout" bounds="[851,453][1149,866]" clickable="true" enabled="true">
      <node text="" content-desc="" resource-id="com.xiaoluxue.ai.student:id/practiseItem" class="android.view.ViewGroup" bounds="[914,344][1086,496]" clickable="true" enabled="true" />
      <node text="题型突破" content-desc="" resource-id="com.xiaoluxue.ai.student:id/title" class="android.widget.TextView" bounds="[948,420][1052,457]" clickable="false" enabled="true" />
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
        self.assertTrue(snapshot["visible_actions"]["wrong"])
        index_node = mcp.find_xiaoluxue_map_index_node(nodes, "1.5")
        self.assertEqual(mcp.xiaoluxue_map_predicted_action_point(index_node, "practise"), {"x": 1000, "y": 420})

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

    def test_connected_device_serials_prefers_one_physical_device(self) -> None:
        original_list_devices = mcp.list_devices
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

            self.assertEqual(mcp.connected_device_serials(), ["ANMB9X5A10G00857"])

            mcp.os.environ["ANDROID_USE_SCRCPY_RESIDENT_SERIALS"] = "emulator-5554,missing"
            self.assertEqual(mcp.connected_device_serials(), ["emulator-5554"])
        finally:
            mcp.list_devices = original_list_devices
            for key, value in original_env.items():
                if value is None:
                    mcp.os.environ.pop(key, None)
                else:
                    mcp.os.environ[key] = value

    def test_tool_descriptors_include_required_tools(self) -> None:
        tool_names = {tool["name"] for tool in mcp.tool_descriptors()}

        self.assertIn("android_wake_unlock", tool_names)
        self.assertIn("android_open_url", tool_names)
        self.assertIn("android_open_app", tool_names)
        self.assertIn("android_show_screen", tool_names)
        self.assertIn("android_observe", tool_names)
        self.assertIn("android_tap_text", tool_names)
        self.assertIn("android_start_screen_viewer", tool_names)
        self.assertIn("android_start_webrtc_viewer", tool_names)
        self.assertIn("android_agent_run", tool_names)
        self.assertIn("android_agent_tars_run", tool_names)

        start_scrcpy = next(tool for tool in mcp.tool_descriptors() if tool["name"] == "android_start_scrcpy")
        properties = start_scrcpy["inputSchema"]["properties"]
        self.assertIn("keep_alive", properties)
        self.assertIn("audio", properties)
        self.assertIn("keyboard", properties)
        self.assertIn("prefer_text", properties)
        self.assertIn("legacy_paste", properties)
        self.assertIn("lock_window_continuous", properties)

        agent_run = next(tool for tool in mcp.tool_descriptors() if tool["name"] == "android_agent_run")
        self.assertTrue(agent_run["inputSchema"]["properties"]["show_scrcpy"]["default"])

        self.assertIn("android_start_recording", tool_names)
        self.assertIn("android_create_recipe", tool_names)
        self.assertIn("android_replay_recipe", tool_names)
        self.assertIn("android_index_source", tool_names)
        self.assertIn("android_scrcpy_resident_status", tool_names)
        self.assertIn("android_webview_pages", tool_names)
        self.assertIn("android_webview_eval", tool_names)
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
        self.assertIn("xiaoluxue_switch_env", tool_names)
        self.assertIn("xiaoluxue_exercise_snapshot", tool_names)
        self.assertIn("xiaoluxue_exercise_action", tool_names)
        self.assertIn("xiaoluxue_exercise_fast_path", tool_names)

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
                    "arguments": {"index": "1.5", "subject_id": 1, "route_if_subject": True, "action_name": "practise"},
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
            },
        )
        self.assertEqual(recipe["steps"][4], {"action": "xiaoluxue_open_native_subject", "subject": "语文", "route_wait_sec": 0.85})
        self.assertEqual(recipe["steps"][5], {"action": "xiaoluxue_switch_env", "env": "test", "open_student": True})
        self.assertEqual(recipe["steps"][6], {"action": "xiaoluxue_exercise_action", "action_name": "select_option", "option_key": "B"})
        self.assertEqual(recipe["steps"][7], {"action": "xiaoluxue_exercise_fast_path", "option_key": "C", "submit": True})

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


if __name__ == "__main__":
    unittest.main()
