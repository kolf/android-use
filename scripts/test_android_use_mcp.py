#!/usr/bin/env python3
"""Offline tests for the Android Use MCP server helpers."""

from __future__ import annotations

import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import android_use_mcp as mcp


def make_raw_screenshot(
    width: int,
    height: int,
    *,
    fill: tuple[int, int, int, int] = (255, 255, 255, 255),
    rects: list[tuple[int, int, int, int, tuple[int, int, int, int]]] | None = None,
) -> bytes:
    pixels = bytearray(fill * (width * height))
    for x1, y1, x2, y2, color in rects or []:
        for y in range(max(y1, 0), min(y2, height)):
            row = y * width * 4
            for x in range(max(x1, 0), min(x2, width)):
                offset = row + x * 4
                pixels[offset : offset + 4] = bytes(color)
    return struct.pack("<IIII", width, height, 1, 0) + bytes(pixels)


def make_png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )


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

    def test_parse_adb_mdns_services(self) -> None:
        output = """List of discovered mdns services
adb-ANMB._adb-tls-connect._tcp.    172.27.31.51:37123
adb-ANMB._adb-tls-pairing._tcp.    172.27.31.51:44111
"""
        services = mcp.parse_adb_mdns_services(output, host="172.27.31.51")

        self.assertEqual(services, [
            {
                "service": "adb-ANMB._adb-tls-connect._tcp.    172.27.31.51:37123",
                "host": "172.27.31.51",
                "port": 37123,
                "serial": "172.27.31.51:37123",
            }
        ])

    def test_wireless_configs_from_env_reads_multiple_devices_and_legacy(self) -> None:
        original_read = mcp.read_user_env_values
        original_env = {
            "ANDROID_USE_WIRELESS_DEVICES": mcp.os.environ.get("ANDROID_USE_WIRELESS_DEVICES"),
            "ANDROID_USE_WIRELESS_HOST": mcp.os.environ.get("ANDROID_USE_WIRELESS_HOST"),
            "ANDROID_USE_WIRELESS_PORT": mcp.os.environ.get("ANDROID_USE_WIRELESS_PORT"),
            "ANDROID_USE_SERIAL": mcp.os.environ.get("ANDROID_USE_SERIAL"),
            "ANDROID_SERIAL": mcp.os.environ.get("ANDROID_SERIAL"),
        }
        try:
            for key in original_env:
                mcp.os.environ.pop(key, None)
            mcp.read_user_env_values = lambda: {
                "ANDROID_USE_WIRELESS_DEVICES": "10.0.0.1:5555,10.0.0.2:4444",
                "ANDROID_USE_WIRELESS_HOST": "10.0.0.3",
                "ANDROID_USE_WIRELESS_PORT": "3333",
                "ANDROID_SERIAL": "10.0.0.3:3333",
            }

            configs = mcp.wireless_configs_from_env()
        finally:
            mcp.read_user_env_values = original_read
            for key, value in original_env.items():
                if value is None:
                    mcp.os.environ.pop(key, None)
                else:
                    mcp.os.environ[key] = value

        self.assertEqual(
            [(item["host"], item["port"], item["serial"]) for item in configs],
            [
                ("10.0.0.1", 5555, "10.0.0.1:5555"),
                ("10.0.0.2", 4444, "10.0.0.2:4444"),
                ("10.0.0.3", 3333, "10.0.0.3:3333"),
            ],
        )

    def test_save_wireless_config_appends_multi_device_and_resident_serials(self) -> None:
        original_read = mcp.read_user_env_values
        original_update = mcp.update_user_env_file
        original_env = {
            "ANDROID_USE_WIRELESS_DEVICES": mcp.os.environ.get("ANDROID_USE_WIRELESS_DEVICES"),
            "ANDROID_USE_SCRCPY_RESIDENT_SERIALS": mcp.os.environ.get("ANDROID_USE_SCRCPY_RESIDENT_SERIALS"),
        }
        captured: dict[str, str] = {}
        try:
            for key in original_env:
                mcp.os.environ.pop(key, None)
            mcp.read_user_env_values = lambda: {
                "ANDROID_USE_WIRELESS_DEVICES": "10.0.0.1:5555",
                "ANDROID_USE_SCRCPY_RESIDENT_SERIALS": "10.0.0.1:5555,usb-1",
            }
            mcp.update_user_env_file = lambda updates: captured.update(updates)

            mcp.save_wireless_config("10.0.0.2", 4444, "10.0.0.2:4444")
        finally:
            mcp.read_user_env_values = original_read
            mcp.update_user_env_file = original_update
            for key, value in original_env.items():
                if value is None:
                    mcp.os.environ.pop(key, None)
                else:
                    mcp.os.environ[key] = value

        self.assertEqual(captured["ANDROID_USE_WIRELESS_HOST"], "10.0.0.2")
        self.assertEqual(captured["ANDROID_USE_WIRELESS_PORT"], "4444")
        self.assertEqual(captured["ANDROID_USE_WIRELESS_DEVICES"], "10.0.0.1:5555,10.0.0.2:4444")
        self.assertEqual(captured["ANDROID_USE_SERIAL"], "10.0.0.2:4444")
        self.assertEqual(captured["ANDROID_SERIAL"], "10.0.0.2:4444")
        self.assertEqual(captured["ANDROID_USE_SCRCPY_RESIDENT_SERIALS"], "10.0.0.1:5555,usb-1,10.0.0.2:4444")

    def test_save_wireless_config_replaces_stale_port_for_same_host(self) -> None:
        original_read = mcp.read_user_env_values
        original_update = mcp.update_user_env_file
        original_env = {
            "ANDROID_USE_WIRELESS_DEVICES": mcp.os.environ.get("ANDROID_USE_WIRELESS_DEVICES"),
            "ANDROID_USE_SCRCPY_RESIDENT_SERIALS": mcp.os.environ.get("ANDROID_USE_SCRCPY_RESIDENT_SERIALS"),
        }
        captured: dict[str, str] = {}
        try:
            for key in original_env:
                mcp.os.environ.pop(key, None)
            mcp.read_user_env_values = lambda: {
                "ANDROID_USE_WIRELESS_DEVICES": "10.0.0.1:1111,10.0.0.2:2222",
                "ANDROID_USE_SCRCPY_RESIDENT_SERIALS": "10.0.0.1:1111,usb-1",
            }
            mcp.update_user_env_file = lambda updates: captured.update(updates)

            mcp.save_wireless_config("10.0.0.1", 5555, "10.0.0.1:5555")
        finally:
            mcp.read_user_env_values = original_read
            mcp.update_user_env_file = original_update
            for key, value in original_env.items():
                if value is None:
                    mcp.os.environ.pop(key, None)
                else:
                    mcp.os.environ[key] = value

        self.assertEqual(captured["ANDROID_USE_WIRELESS_DEVICES"], "10.0.0.2:2222,10.0.0.1:5555")
        self.assertEqual(captured["ANDROID_USE_SCRCPY_RESIDENT_SERIALS"], "usb-1,10.0.0.1:5555")

    def test_wireless_reconnect_all_uses_saved_configs(self) -> None:
        original_configs = mcp.wireless_configs_from_env
        original_reconnect = mcp.wireless_reconnect
        calls: list[dict[str, object]] = []
        try:
            mcp.wireless_configs_from_env = lambda: [
                {"host": "10.0.0.1", "port": 5555, "serial": "10.0.0.1:5555"},
                {"host": "10.0.0.2", "port": 4444, "serial": "10.0.0.2:4444"},
            ]

            def fake_reconnect(**kwargs: object) -> dict[str, object]:
                calls.append(kwargs)
                return {"ok": True, "serial": kwargs["serial"]}

            mcp.wireless_reconnect = fake_reconnect
            result = mcp.wireless_reconnect_all(save=False, start_scrcpy=True)
        finally:
            mcp.wireless_configs_from_env = original_configs
            mcp.wireless_reconnect = original_reconnect

        self.assertEqual(result["count"], 2)
        self.assertEqual([call["serial"] for call in calls], ["10.0.0.1:5555", "10.0.0.2:4444"])
        self.assertTrue(all(call["start_scrcpy"] for call in calls))
        self.assertTrue(all(call["save"] is False for call in calls))

    def test_choose_serial_dedupes_usb_and_wireless_same_device(self) -> None:
        original_list_devices = mcp.list_devices
        original_shell = mcp.shell
        original_auto_reconnect = mcp.auto_reconnect_wireless_if_needed
        original_env = {
            "ANDROID_USE_SERIAL": mcp.os.environ.get("ANDROID_USE_SERIAL"),
            "ANDROID_SERIAL": mcp.os.environ.get("ANDROID_SERIAL"),
        }
        try:
            mcp.os.environ.pop("ANDROID_USE_SERIAL", None)
            mcp.os.environ.pop("ANDROID_SERIAL", None)
            mcp.list_devices = lambda: [
                {"serial": "ANMB9X5A10G00857", "state": "device"},
                {"serial": "172.27.31.51:5555", "state": "device"},
            ]
            mcp.shell = lambda serial, command, timeout=30: "ANMB9X5A10G00857"
            mcp.auto_reconnect_wireless_if_needed = lambda: None

            self.assertEqual(mcp.choose_serial(), "172.27.31.51:5555")
        finally:
            mcp.list_devices = original_list_devices
            mcp.shell = original_shell
            mcp.auto_reconnect_wireless_if_needed = original_auto_reconnect
            for key, value in original_env.items():
                if value is None:
                    mcp.os.environ.pop(key, None)
                else:
                    mcp.os.environ[key] = value

    def test_parse_screen_size_prefers_override(self) -> None:
        size = mcp.parse_screen_size("Physical size: 1080x2400\nOverride size: 720x1600")

        self.assertEqual(size, {"width": 720, "height": 1600})

    def test_tool_appshot_saves_evidence_bundle(self) -> None:
        original_choose = mcp.choose_serial
        original_screenshot = mcp.screenshot_png
        original_observe = mcp.observe_ui
        png = make_png(1080, 2400)
        try:
            mcp.choose_serial = lambda _serial=None: "device-1"
            mcp.screenshot_png = lambda serial: png

            def fake_observe(serial: str, include_xml: bool = False, limit: int = 160) -> dict[str, object]:
                ui: dict[str, object] = {
                    "nodes": [
                        {
                            "index": 0,
                            "text": "登录",
                            "resource_id": "com.example:id/login",
                            "bounds": {"left": 10, "top": 20, "right": 110, "bottom": 80},
                            "center": {"x": 60, "y": 50},
                            "clickable": True,
                        }
                    ],
                    "count": 1,
                }
                if include_xml:
                    ui["xml"] = "<hierarchy />"
                return {
                    "state": {"serial": serial, "focused_window": "com.example/.MainActivity"},
                    "ui": ui,
                }

            mcp.observe_ui = fake_observe

            with tempfile.TemporaryDirectory() as tmpdir:
                content = mcp.tool_appshot({"serial": "device-1", "include_xml": True, "save_dir": tmpdir})
                payload = json.loads(content[0]["text"])
                saved_json = Path(payload["paths"]["json"])
                saved_png = Path(payload["paths"]["png"])

                self.assertEqual(content[1]["type"], "image")
                self.assertEqual(payload["kind"], "android_appshot")
                self.assertEqual(payload["screenshot"]["screen"], {"width": 1080, "height": 2400})
                self.assertEqual(payload["ui"]["count"], 1)
                self.assertEqual(payload["ui"]["xml"], "<hierarchy />")
                self.assertTrue(saved_json.exists())
                self.assertEqual(saved_png.read_bytes(), png)
                self.assertEqual(json.loads(saved_json.read_text())["kind"], "android_appshot")
        finally:
            mcp.choose_serial = original_choose
            mcp.screenshot_png = original_screenshot
            mcp.observe_ui = original_observe

    def test_tool_appshot_returns_screenshot_when_ui_dump_fails(self) -> None:
        original_choose = mcp.choose_serial
        original_screenshot = mcp.screenshot_png
        original_observe = mcp.observe_ui
        original_state = mcp.device_state
        try:
            mcp.choose_serial = lambda _serial=None: "device-1"
            mcp.screenshot_png = lambda serial: make_png(720, 1280)
            mcp.observe_ui = lambda *args, **kwargs: (_ for _ in ()).throw(mcp.AndroidUseError("dump failed"))
            mcp.device_state = lambda serial: {"serial": serial, "focused_window": "com.example/.MainActivity"}

            content = mcp.tool_appshot({"save": False, "include_image": False})
            payload = json.loads(content[0]["text"])
        finally:
            mcp.choose_serial = original_choose
            mcp.screenshot_png = original_screenshot
            mcp.observe_ui = original_observe
            mcp.device_state = original_state

        self.assertEqual(len(content), 1)
        self.assertEqual(payload["kind"], "android_appshot")
        self.assertEqual(payload["ui"]["count"], 0)
        self.assertIn("dump failed", payload["ui_error"])

    def test_keycode_normalization(self) -> None:
        self.assertEqual(mcp.keycode("back"), "KEYCODE_BACK")
        self.assertEqual(mcp.keycode("KEYCODE_HOME"), "KEYCODE_HOME")
        self.assertEqual(mcp.keycode(66), "66")
        self.assertEqual(mcp.keycode("camera"), "KEYCODE_CAMERA")

    def test_type_text_uses_adb_keyboard_switch_for_unicode(self) -> None:
        original_current_ime = mcp.current_input_method
        original_list_imes = mcp.list_input_methods
        original_set_ime = mcp.set_input_method
        original_shell = mcp.shell
        original_env = {
            "ANDROID_USE_WEBVIEW_DIRECT_INPUT": mcp.os.environ.get("ANDROID_USE_WEBVIEW_DIRECT_INPUT"),
            "ANDROID_USE_FAST_INPUT_IME": mcp.os.environ.get("ANDROID_USE_FAST_INPUT_IME"),
            "ANDROID_USE_RESTORE_IME_AFTER_TYPE": mcp.os.environ.get("ANDROID_USE_RESTORE_IME_AFTER_TYPE"),
        }
        state = {"ime": "com.google.android.inputmethod.pinyin/.PinyinIME"}
        commands: list[str] = []
        try:
            mcp.os.environ["ANDROID_USE_WEBVIEW_DIRECT_INPUT"] = "0"
            mcp.os.environ["ANDROID_USE_FAST_INPUT_IME"] = "1"
            mcp.os.environ["ANDROID_USE_RESTORE_IME_AFTER_TYPE"] = "0"
            mcp.current_input_method = lambda serial: state["ime"]
            mcp.list_input_methods = lambda serial: ["com.github.uiautomator/.AdbKeyboard"]
            mcp.set_input_method = lambda serial, ime: state.update({"ime": ime}) or commands.append(f"ime set {ime}")
            mcp.shell = lambda serial, command, timeout=30: commands.append(command) or ""

            method = mcp.type_focused_text_fast("device-1", "你好", clear_first=True, enter=True)
        finally:
            mcp.current_input_method = original_current_ime
            mcp.list_input_methods = original_list_imes
            mcp.set_input_method = original_set_ime
            mcp.shell = original_shell
            for key, value in original_env.items():
                if value is None:
                    mcp.os.environ.pop(key, None)
                else:
                    mcp.os.environ[key] = value

        self.assertEqual(method, "adb_keyboard_switch")
        self.assertEqual(state["ime"], "com.github.uiautomator/.AdbKeyboard")
        self.assertTrue(any("ADB_CLEAR_TEXT" in command for command in commands))
        self.assertTrue(any("ADB_INPUT_TEXT" in command and "你好" in command for command in commands))

    def test_type_text_batch_fallback_uses_one_shell_call_for_clear(self) -> None:
        original_broadcast = mcp.adb_keyboard_broadcast_text
        original_shell = mcp.shell
        original_env = {"ANDROID_USE_WEBVIEW_DIRECT_INPUT": mcp.os.environ.get("ANDROID_USE_WEBVIEW_DIRECT_INPUT")}
        commands: list[str] = []
        try:
            mcp.os.environ["ANDROID_USE_WEBVIEW_DIRECT_INPUT"] = "0"
            mcp.adb_keyboard_broadcast_text = lambda *args, **kwargs: False
            mcp.shell = lambda serial, command, timeout=30: commands.append(command) or ""

            method = mcp.type_focused_text_fast("device-1", "hello world", clear_first=True, clear_count=3, enter=True)
        finally:
            mcp.adb_keyboard_broadcast_text = original_broadcast
            mcp.shell = original_shell
            for key, value in original_env.items():
                if value is None:
                    mcp.os.environ.pop(key, None)
                else:
                    mcp.os.environ[key] = value

        self.assertEqual(method, "adb_shell_batch")
        self.assertEqual(len(commands), 1)
        self.assertIn("input keyevent KEYCODE_MOVE_END", commands[0])
        self.assertIn("input keyevent KEYCODE_DEL KEYCODE_DEL KEYCODE_DEL", commands[0])
        self.assertIn("input text hello%sworld", commands[0])
        self.assertIn("input keyevent KEYCODE_ENTER", commands[0])

    def test_type_text_prefers_webview_dom_input(self) -> None:
        original_type_webview = mcp.type_webview_text_fast
        original_shell_batch = mcp.adb_shell_batch_type_text
        original_env = {"ANDROID_USE_WEBVIEW_DIRECT_INPUT": mcp.os.environ.get("ANDROID_USE_WEBVIEW_DIRECT_INPUT")}
        calls: list[dict[str, object]] = []
        try:
            mcp.os.environ["ANDROID_USE_WEBVIEW_DIRECT_INPUT"] = "1"

            def fake_type_webview(serial: str, text: str, **kwargs: object) -> str:
                calls.append({"serial": serial, "text": text, **kwargs})
                return "webview_dom_dom_value"

            mcp.type_webview_text_fast = fake_type_webview
            mcp.adb_shell_batch_type_text = lambda *args, **kwargs: self.fail("keyboard fallback should not run")
            method = mcp.type_focused_text_fast("device-1", "hello", clear_first=True, enter=True)
        finally:
            mcp.type_webview_text_fast = original_type_webview
            mcp.adb_shell_batch_type_text = original_shell_batch
            for key, value in original_env.items():
                if value is None:
                    mcp.os.environ.pop(key, None)
                else:
                    mcp.os.environ[key] = value

        self.assertEqual(method, "webview_dom_dom_value")
        self.assertEqual(calls, [{"serial": "device-1", "text": "hello", "clear_first": True, "enter": True}])

    def test_recipe_type_text_uses_fast_typing_path(self) -> None:
        original_type_fast = mcp.type_focused_text_fast
        calls: list[dict[str, object]] = []
        try:
            def fake_type_fast(serial: str, text: str, **kwargs: object) -> str:
                calls.append({"serial": serial, "text": text, **kwargs})
                return "fake_fast"

            mcp.type_focused_text_fast = fake_type_fast
            result = mcp.execute_recipe_step(
                "device-1",
                {"action": "type_text", "text": "hello", "clear_first": True, "clear_count": 2, "enter": True},
            )
        finally:
            mcp.type_focused_text_fast = original_type_fast

        self.assertEqual(result["method"], "fake_fast")
        self.assertEqual(calls, [
            {"serial": "device-1", "text": "hello", "clear_first": True, "clear_count": 2, "enter": True}
        ])

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
    <node text="" content-desc="" resource-id="com.example:id/check" class="android.widget.CheckBox" bounds="[100,100][140,140]" clickable="true" enabled="true" checkable="true" checked="true" />
  </node>
</hierarchy>"""
        nodes = mcp.parse_ui_nodes(xml)
        node = mcp.find_ui_node(nodes, "发现")
        point = mcp.node_click_point(node)
        checkbox = mcp.find_node_by_selector(nodes, {"strategy": "resource_id", "value": "com.example:id/check"})

        self.assertIsNotNone(node)
        self.assertEqual(point, {"x": 1000, "y": 2460})
        self.assertEqual(mcp.find_node_by_selector(nodes, {"strategy": "resource_id", "value": "com.example:id/discover"}), node)
        self.assertTrue(checkbox["checkable"])
        self.assertTrue(checkbox["checked"])

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
        self.assertTrue(mcp.xiaoluxue_instruction_looks_like_map("进入数学的巩固练习，随便一节课"))
        self.assertFalse(mcp.xiaoluxue_instruction_looks_like_map("首页 -> 数学 1.1.11 知识讲解 2x"))
        self.assertEqual(mcp.normalize_xiaoluxue_map_index("进入 1.5 题型突破"), "1.5")
        self.assertEqual(mcp.normalize_xiaoluxue_map_action("打开 1.5 错题"), "wrong")
        self.assertEqual(mcp.normalize_xiaoluxue_map_action("打开 1.5 笔记本"), "notebook")
        self.assertEqual(mcp.normalize_xiaoluxue_map_action("看报告"), "report")
        self.assertEqual(mcp.normalize_xiaoluxue_map_action("进入数学 专属精练"), "expand")
        self.assertEqual(mcp.normalize_xiaoluxue_map_action("进入数学 巩固练习"), "expand")
        self.assertEqual(mcp.normalize_xiaoluxue_lesson_action("直接练"), "direct_practice")
        self.assertEqual(mcp.normalize_xiaoluxue_lesson_action("继续到下一题"), "continue_answer")
        self.assertEqual(mcp.normalize_xiaoluxue_lesson_action("完成返回地图"), "finish_result")
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
        self.assertEqual(
            mcp.xiaoluxue_map_fast_action_from_instruction("进入数学 1.5 题型突破 直接练"),
            {
                "action": "xiaoluxue_map_fast_path",
                "instruction": "进入数学 1.5 题型突破 直接练",
                "action_name": "practise",
                "source": "xiaoluxue-native-map",
                "index": "1.5",
                "subject_id": 2,
                "route_if_subject": True,
                "enter_direct_practice": True,
            },
        )
        self.assertEqual(
            mcp.xiaoluxue_map_fast_action_from_instruction("进入数学 专属精练"),
            {
                "action": "xiaoluxue_map_fast_path",
                "instruction": "进入数学 专属精练",
                "action_name": "expand",
                "source": "xiaoluxue-native-map",
                "subject_id": 2,
                "route_if_subject": True,
            },
        )
        self.assertEqual(
            mcp.xiaoluxue_lesson_fast_action_from_instruction("进入 直接练"),
            {
                "action": "xiaoluxue_lesson_fast_path",
                "instruction": "进入 直接练",
                "action_name": "direct_practice",
                "source": "xiaoluxue-native-lesson",
            },
        )
        self.assertEqual(
            mcp.xiaoluxue_lesson_fast_action_from_instruction("完成返回地图"),
            {
                "action": "xiaoluxue_lesson_fast_path",
                "instruction": "完成返回地图",
                "action_name": "finish_result",
                "source": "xiaoluxue-native-lesson",
            },
        )

    def test_xiaoluxue_native_course_sequence_taps_continue_quickly(self) -> None:
        original_tap = mcp.xiaoluxue_native_tap
        original_wait = mcp.xiaoluxue_wait_for_site_page
        original_sleep = mcp.time.sleep
        labels: list[str] = []
        try:
            mcp.xiaoluxue_native_tap = lambda serial, point, info, label, steps, started_at: labels.append(label)
            mcp.xiaoluxue_wait_for_site_page = lambda serial, deadline: {"id": "page", "url": "http://stu.test/course"}
            mcp.time.sleep = lambda seconds: None

            result = mcp.xiaoluxue_try_native_course_sequence(
                "serial",
                {"width": 2000, "height": 1200},
                [],
                0.0,
                999999999.0,
                label="subject_map",
                tap_math=False,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(
                labels,
                [
                    "subject_map:dismiss_progress_popup",
                    "subject_map:guide_bubble",
                    "subject_map:continue:1",
                ],
            )
        finally:
            mcp.xiaoluxue_native_tap = original_tap
            mcp.xiaoluxue_wait_for_site_page = original_wait
            mcp.time.sleep = original_sleep

    def test_xiaoluxue_open_knowledge_prefers_native_entry_before_vessel(self) -> None:
        original_any_page = mcp.xiaoluxue_any_page
        original_native_entry = mcp.xiaoluxue_open_native_course_entry
        original_vessel_entry = mcp.xiaoluxue_open_vessel_course_page
        original_cdp_eval = mcp.cdp_eval_value
        calls: list[str] = []
        target_url = str(mcp.XIAOLUXUE_KNOWLEDGE_SHORTCUTS[(2, "1111")]["targetUrl"])
        fake_page = {
            "id": "page-1",
            "url": target_url,
            "runtimeHref": target_url,
            "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/page-1",
        }
        try:
            mcp.xiaoluxue_any_page = lambda *args, **kwargs: (_ for _ in ()).throw(mcp.AndroidUseError("no current page"))

            def fake_native_entry(*args, **kwargs):
                calls.append("native")
                return {"attempted": True, "ok": True, "page": fake_page}

            def fake_vessel_entry(*args, **kwargs):
                calls.append("vessel")
                raise AssertionError("vessel should not be attempted before successful native entry")

            def fake_cdp_eval(page, expression, **kwargs):
                if expression == "location.href":
                    return target_url
                text = str(expression)
                if "readyState" in text and "targetWidget" not in text:
                    return {"ok": True, "before": {"readyState": "complete"}, "after": {"readyState": "complete"}}
                if "targetWidget" in text:
                    return {
                        "ok": True,
                        "readyState": "complete",
                        "targetWidget": {"dataName": "初识集合——集合与元素的定义", "loaded": True},
                        "videos": [{"playbackRate": 2}],
                    }
                if "turbo" in text or "video" in text:
                    return {"ok": True, "video": True}
                return {"ok": True}

            mcp.xiaoluxue_open_native_course_entry = fake_native_entry
            mcp.xiaoluxue_open_vessel_course_page = fake_vessel_entry
            mcp.cdp_eval_value = fake_cdp_eval

            result = mcp.run_xiaoluxue_open_knowledge_guide(
                "serial",
                {
                    "subject_id": 2,
                    "knowledge_index": "1.1.11",
                    "rate": 2,
                    "timeout_sec": 3,
                    "prefer_native_entry_first": True,
                },
                record=False,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(calls, ["native"])
            self.assertTrue(result["native_entry"]["ok"])
            self.assertFalse(result["vessel_entry"]["attempted"])
        finally:
            mcp.xiaoluxue_any_page = original_any_page
            mcp.xiaoluxue_open_native_course_entry = original_native_entry
            mcp.xiaoluxue_open_vessel_course_page = original_vessel_entry
            mcp.cdp_eval_value = original_cdp_eval

    def test_xiaoluxue_open_knowledge_prefers_direct_vessel_for_known_shortcut_by_default(self) -> None:
        original_any_page = mcp.xiaoluxue_any_page
        original_native_entry = mcp.xiaoluxue_open_native_course_entry
        original_vessel_entry = mcp.xiaoluxue_open_vessel_course_page
        original_cdp_eval = mcp.cdp_eval_value
        calls: list[str] = []
        captured_timeout: list[float] = []
        target_url = str(mcp.XIAOLUXUE_KNOWLEDGE_SHORTCUTS[(2, "1111")]["targetUrl"])
        fake_page = {
            "id": "page-1",
            "url": target_url,
            "runtimeHref": target_url,
            "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/page-1",
        }
        try:
            mcp.xiaoluxue_any_page = lambda *args, **kwargs: (_ for _ in ()).throw(mcp.AndroidUseError("no current page"))

            def fake_native_entry(*args, **kwargs):
                calls.append("native")
                raise AssertionError("native should not be attempted for a known shortcut by default")

            def fake_vessel_entry(*args, **kwargs):
                calls.append("vessel")
                captured_timeout.append(float(kwargs["timeout_sec"]))
                return {"attempted": True, "ok": True, "page": fake_page}

            def fake_cdp_eval(page, expression, **kwargs):
                if expression == "location.href":
                    return target_url
                text = str(expression)
                if "readyState" in text and "targetWidget" not in text:
                    return {"ok": True, "before": {"readyState": "complete"}, "after": {"readyState": "complete"}}
                if "targetWidget" in text:
                    return {
                        "ok": True,
                        "readyState": "complete",
                        "targetWidget": {"dataName": "初识集合——集合与元素的定义", "loaded": True},
                        "videos": [{"playbackRate": 2}],
                    }
                if "turbo" in text or "video" in text:
                    return {"ok": True, "video": True}
                return {"ok": True}

            mcp.xiaoluxue_open_native_course_entry = fake_native_entry
            mcp.xiaoluxue_open_vessel_course_page = fake_vessel_entry
            mcp.cdp_eval_value = fake_cdp_eval

            result = mcp.run_xiaoluxue_open_knowledge_guide(
                "serial",
                {"subject_id": 2, "knowledge_index": "1.1.11", "rate": 2, "timeout_sec": 5},
                record=False,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(calls, ["vessel"])
            self.assertGreaterEqual(captured_timeout[0], 4.0)
            self.assertTrue(result["vessel_entry"]["ok"])
            self.assertFalse(result["native_entry"]["attempted"])
            self.assertEqual(result["stop_loading"]["skipped"], "entry-opened-target-course")
            self.assertEqual(result["prefetch"]["skipped"], "entry-opened-target-course")
            self.assertEqual(result["rate_prepare"]["skipped"], "entry-opened-target-course")
        finally:
            mcp.xiaoluxue_any_page = original_any_page
            mcp.xiaoluxue_open_native_course_entry = original_native_entry
            mcp.xiaoluxue_open_vessel_course_page = original_vessel_entry
            mcp.cdp_eval_value = original_cdp_eval

    def test_xiaoluxue_hidden_course_webview_behind_native_map_is_not_foreground(self) -> None:
        original_focus = mcp.get_focused_window
        try:
            mcp.get_focused_window = lambda serial: (
                f"Window{{1 u0 {mcp.XIAOLUXUE_STUDENT_PACKAGE}/{mcp.XIAOLUXUE_STUDY_SUBJECT_ACTIVITY}}}"
            )
            with self.assertRaises(mcp.AndroidUseError):
                mcp.xiaoluxue_ensure_foreground_webview(
                    "serial",
                    {
                        "url": "https://stu.xiaoluxue.com/course?knowledgeId=3785",
                        "descriptionParsed": {"visible": False},
                    },
                )
        finally:
            mcp.get_focused_window = original_focus

    def test_xiaoluxue_hidden_course_webview_behind_leakcanary_is_not_foreground(self) -> None:
        original_focus = mcp.get_focused_window
        try:
            mcp.get_focused_window = lambda serial: (
                f"{mcp.XIAOLUXUE_STUDENT_PACKAGE}/leakcanary.internal.activity.LeakLauncherActivity"
            )
            with self.assertRaises(mcp.AndroidUseError):
                mcp.xiaoluxue_ensure_foreground_webview(
                    "serial",
                    {
                        "url": "https://stu.xiaoluxue.com/course?knowledgeId=3785",
                        "descriptionParsed": {"visible": False},
                    },
                )
        finally:
            mcp.get_focused_window = original_focus

    def test_xiaoluxue_wait_for_target_course_page_accepts_static_target_url_after_eval_race(self) -> None:
        original_discover = mcp.discover_webview_pages
        original_eval = mcp.cdp_eval_value
        original_sleep = mcp.time.sleep
        original_monotonic = mcp.time.monotonic
        calls = 0
        page = {
            "id": "page-1",
            "url": "https://stu.xiaoluxue.com/course?knowledgeId=3785&lessonId=1",
            "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/page-1",
            "descriptionParsed": {"visible": True},
        }
        try:
            mcp.discover_webview_pages = lambda serial: [page]
            mcp.cdp_eval_value = lambda *args, **kwargs: (_ for _ in ()).throw(mcp.AndroidUseError("runtime href race"))
            mcp.time.sleep = lambda seconds: None

            def fake_monotonic() -> float:
                nonlocal calls
                calls += 1
                return 0.0 if calls <= 2 else 1.0

            mcp.time.monotonic = fake_monotonic

            result = mcp.xiaoluxue_wait_for_target_course_page(
                "serial",
                0.5,
                knowledge_id=3785,
                poll_interval=0,
            )

            self.assertEqual(result["url"], page["url"])
        finally:
            mcp.discover_webview_pages = original_discover
            mcp.cdp_eval_value = original_eval
            mcp.time.sleep = original_sleep
            mcp.time.monotonic = original_monotonic

    def test_xiaoluxue_route_app_url_uses_scheme_proxy_component(self) -> None:
        original_adb = mcp.adb
        commands: list[list[str]] = []
        try:
            mcp.adb = lambda command, serial=None, timeout=30: commands.append(list(command)) or b"Starting: Intent {}"

            result = mcp.xiaoluxue_route_app_url(
                "serial",
                "https://stu.xiaoluxue.com/course?knowledgeId=3785",
                force_stop=True,
            )

            command = commands[0]
            self.assertTrue(result["ok"])
            self.assertIn("-S", command)
            self.assertIn("-n", command)
            self.assertIn(f"{mcp.XIAOLUXUE_STUDENT_PACKAGE}/{mcp.XIAOLUXUE_SCHEME_PROXY_ACTIVITY}", command)
            self.assertNotIn("-p", command)
        finally:
            mcp.adb = original_adb

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


if __name__ == "__main__":
    unittest.main()
