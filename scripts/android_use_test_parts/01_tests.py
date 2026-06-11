# Loaded by scripts/test_android_use_mcp.py. Keep this file below 2000 lines.

class AndroidUseMcpTestsPart1(unittest.TestCase):
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
                "service_name": "adb-ANMB",
                "service_type": "_adb-tls-connect._tcp",
                "host": "172.27.31.51",
                "port": 37123,
                "serial": "172.27.31.51:37123",
            }
        ])

    def test_parse_adb_mdns_pairing_services_matches_qr_session(self) -> None:
        output = """List of discovered mdns services
studio-AbC123xYz9          _adb-tls-pairing._tcp  192.168.86.39:55861
adb-ANMB9X5A10G00857      _adb-tls-connect._tcp  192.168.86.39:37123
"""
        services = mcp.parse_adb_mdns_pairing_services(output, service_name="studio-AbC123xYz9")

        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["service_name"], "studio-AbC123xYz9")
        self.assertEqual(services[0]["service_type"], "_adb-tls-pairing._tcp")
        self.assertEqual(services[0]["host"], "192.168.86.39")
        self.assertEqual(services[0]["port"], 55861)

    def test_choose_serial_no_device_guidance_mentions_wired_wireless_and_qr(self) -> None:
        original_list_devices = mcp.list_devices
        original_auto_reconnect = mcp.auto_reconnect_wireless_if_needed
        try:
            mcp.list_devices = lambda: []
            mcp.auto_reconnect_wireless_if_needed = lambda: None

            with self.assertRaises(mcp.AndroidUseError) as context:
                mcp.choose_serial()
        finally:
            mcp.list_devices = original_list_devices
            mcp.auto_reconnect_wireless_if_needed = original_auto_reconnect

        message = str(context.exception)
        self.assertIn("有线", message)
        self.assertIn("无线", message)
        self.assertIn("android_wireless_pair_qr", message)

    def test_tool_list_devices_returns_connection_help_when_empty(self) -> None:
        original_list_devices = mcp.list_devices
        try:
            mcp.list_devices = lambda: []

            content = mcp.tool_list_devices({"include_details": False})
        finally:
            mcp.list_devices = original_list_devices

        payload = json.loads(content[0]["text"])
        self.assertEqual(payload["devices"], [])
        self.assertEqual(payload["connection_help"]["methods"][0]["name"], "有线连接")
        self.assertEqual(payload["connection_help"]["methods"][1]["name"], "无线连接")

    def test_wireless_pair_qr_create_returns_session_and_png(self) -> None:
        original_android_dir = mcp.ANDROID_USE_DIR
        original_screen_dir = mcp.SCREEN_DIR
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                mcp.ANDROID_USE_DIR = Path(tmpdir) / ".android-use"
                mcp.SCREEN_DIR = Path(tmpdir) / ".screen"

                content = mcp.tool_wireless_pair_qr({"action": "create"})
                payload = json.loads(content[0]["text"])
                png = mcp.base64.b64decode(content[1]["data"])

                self.assertEqual(content[1]["type"], "image")
                self.assertEqual(content[1]["mimeType"], "image/png")
                self.assertTrue(payload["session_id"])
                self.assertTrue(payload["service_name"].startswith("studio-"))
                self.assertTrue(payload["qr_payload"].startswith("WIFI:T:ADB;S:studio-"))
                self.assertTrue(Path(payload["qr_path"]).exists())
                self.assertEqual(mcp.png_size(png), {"width": 328, "height": 328})
        finally:
            mcp.ANDROID_USE_DIR = original_android_dir
            mcp.SCREEN_DIR = original_screen_dir

    def test_wireless_pair_qr_complete_pairs_discovered_service(self) -> None:
        original_android_dir = mcp.ANDROID_USE_DIR
        original_pairing_services = mcp.adb_mdns_pairing_services
        original_pair = mcp.adb_pair_wireless
        original_reconnect = mcp.wireless_reconnect
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                mcp.ANDROID_USE_DIR = Path(tmpdir) / ".android-use"
                mcp.save_wireless_qr_session(
                    {
                        "session_id": "session-1",
                        "service_name": "studio-AbC123xYz9",
                        "password": "Pass12345678",
                        "qr_payload": "WIFI:T:ADB;S:studio-AbC123xYz9;P:Pass12345678;;",
                        "qr_path": str(Path(tmpdir) / "qr.png"),
                        "created_at": "2026-05-30T00:00:00Z",
                        "completed_at": None,
                    }
                )

                def fake_pairing_services(**kwargs: object) -> list[dict[str, object]]:
                    self.assertEqual(kwargs["service_name"], "studio-AbC123xYz9")
                    return [
                        {
                            "service_name": "studio-AbC123xYz9",
                            "service_type": "_adb-tls-pairing._tcp",
                            "host": "192.168.86.39",
                            "port": 55861,
                            "serial": "192.168.86.39:55861",
                        }
                    ]

                def fake_pair(host: str, port: int, password: str, **kwargs: object) -> dict[str, object]:
                    self.assertEqual(host, "192.168.86.39")
                    self.assertEqual(port, 55861)
                    self.assertEqual(password, "Pass12345678")
                    self.assertEqual(kwargs["service_name"], "studio-AbC123xYz9")
                    return {"ok": True, "output": "Successfully paired"}

                def fake_reconnect(**kwargs: object) -> dict[str, object]:
                    self.assertEqual(kwargs["host"], "192.168.86.39")
                    self.assertTrue(kwargs["save"])
                    self.assertTrue(kwargs["start_scrcpy"])
                    return {"ok": True, "serial": "192.168.86.39:37123"}

                mcp.adb_mdns_pairing_services = fake_pairing_services  # type: ignore[assignment]
                mcp.adb_pair_wireless = fake_pair  # type: ignore[assignment]
                mcp.wireless_reconnect = fake_reconnect

                content = mcp.tool_wireless_pair_qr(
                    {"action": "complete", "session_id": "session-1", "timeout_sec": 1}
                )
                payload = json.loads(content[0]["text"])
        finally:
            mcp.ANDROID_USE_DIR = original_android_dir
            mcp.adb_mdns_pairing_services = original_pairing_services
            mcp.adb_pair_wireless = original_pair
            mcp.wireless_reconnect = original_reconnect

        self.assertEqual(payload["pair_target"], "192.168.86.39:55861")
        self.assertEqual(payload["reconnect"]["serial"], "192.168.86.39:37123")

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

    def test_wireless_reconnect_explicit_host_ignores_stale_saved_port(self) -> None:
        original_config = mcp.wireless_config_from_env
        original_services = mcp.adb_mdns_connect_services
        original_connect = mcp.adb_connect_serial
        original_save = mcp.save_wireless_config
        calls: list[tuple[str, int]] = []
        try:
            mcp.wireless_config_from_env = lambda: ("172.27.31.51", 40691, "172.27.31.51:40691")
            mcp.adb_mdns_connect_services = lambda host=None: [
                {
                    "host": "172.27.31.51",
                    "port": 37779,
                    "serial": "172.27.31.51:37779",
                }
            ]

            def fake_connect(host: str, port: int) -> dict[str, object]:
                calls.append((host, port))
                return {"connected": True, "serial": f"{host}:{port}", "output": "connected"}

            mcp.adb_connect_serial = fake_connect
            mcp.save_wireless_config = lambda *_args, **_kwargs: None

            result = mcp.wireless_reconnect(host="172.27.31.51", save=True, start_scrcpy=False)
        finally:
            mcp.wireless_config_from_env = original_config
            mcp.adb_mdns_connect_services = original_services
            mcp.adb_connect_serial = original_connect
            mcp.save_wireless_config = original_save

        self.assertEqual(calls, [("172.27.31.51", 37779)])
        self.assertEqual(result["serial"], "172.27.31.51:37779")

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
