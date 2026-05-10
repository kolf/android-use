#!/usr/bin/env python3
"""Offline tests for the Android Use MCP server helpers."""

from __future__ import annotations

import json
import unittest

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

    def test_tools_list_request_shape(self) -> None:
        response = mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})

        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], 1)
        json.dumps(response["result"]["tools"])


if __name__ == "__main__":
    unittest.main()
