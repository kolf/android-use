# Loaded by scripts/android_use_mcp.py. Keep this file below 2000 lines.

TOOLS: dict[str, dict[str, Any]] = {
    "android_check_dependencies": {
        "description": "Check adb, Playwright Android WebView, optional scrcpy, and optional VLM environment configuration.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": check_dependencies,
    },
    "android_list_devices": {
        "description": "List Android devices known to adb, optionally with model and screen details.",
        "inputSchema": {
            "type": "object",
            "properties": {"include_details": {"type": "boolean", "default": True}},
            "additionalProperties": False,
        },
        "handler": tool_list_devices,
    },
    "android_wireless_pair": {
        "description": "Pair an Android 11+ device over Wireless debugging through adb pair, then reconnect with adb connect.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Device IP address, for example 172.27.31.51."},
                "pair_port": {"type": "integer", "description": "Pairing port shown beside the Wireless debugging pairing code."},
                "code": {"type": "string", "description": "Temporary pairing code shown on the Android device."},
                "connect_port": {"type": "integer", "description": "Optional Wireless debugging connection port. If omitted, adb mDNS service discovery is used."},
                "save": {"type": "boolean", "default": True},
                "start_scrcpy": {"type": "boolean", "default": True},
            },
            "required": ["host", "pair_port", "code"],
            "additionalProperties": False,
        },
        "handler": tool_wireless_pair,
    },
    "android_wireless_pair_qr": {
        "description": "Create or complete an Android Wireless debugging QR-code pairing session. Use create first, ask the user to scan the returned QR code, then use complete with the returned session_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "default": "create", "enum": ["create", "complete"]},
                "session_id": {"type": "string", "description": "Session id returned by action=create. Required for action=complete."},
                "timeout_sec": {"type": "number", "default": 60, "description": "How long action=complete waits for the scanned QR pairing service to appear via adb mDNS."},
                "save": {"type": "boolean", "default": True},
                "start_scrcpy": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
        "handler": tool_wireless_pair_qr,
    },
    "android_wireless_reconnect": {
        "description": "Reconnect to saved Wireless debugging devices without USB, refreshing dynamic mDNS ports when needed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Optional device IP address. Defaults to saved ANDROID_USE_WIRELESS_HOST."},
                "port": {"type": "integer", "description": "Optional connect port. Defaults to saved port, then mDNS."},
                "serial": {"type": "string", "description": "Optional adb serial such as 172.27.31.51:5555."},
                "save": {"type": "boolean", "default": True},
                "start_scrcpy": {"type": "boolean", "default": True},
                "all": {
                    "type": "boolean",
                    "default": False,
                    "description": "Reconnect every saved entry from ANDROID_USE_WIRELESS_DEVICES.",
                },
            },
            "additionalProperties": False,
        },
        "handler": tool_wireless_reconnect,
    },
    "android_get_state": {
        "description": "Get state for an attached Android device, optionally including a screenshot.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "include_screenshot": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "handler": tool_get_state,
    },
    "android_screenshot": {
        "description": "Capture a PNG screenshot from the Android device.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "save_path": {"type": "string", "description": "Optional local path for the PNG."},
            },
            "additionalProperties": False,
        },
        "handler": tool_screenshot,
    },
    "android_show_screen": {
        "description": "Capture and return the current Android screen image so Codex can display it inline.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "handler": tool_show_screen,
    },
    "android_appshot": {
        "description": "Capture a Codex-friendly Android appshot: screenshot, device state, and UIAutomator nodes in one evidence bundle.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "include_xml": {"type": "boolean", "default": False},
                "include_image": {
                    "type": "boolean",
                    "default": True,
                    "description": "Attach the PNG image inline in the tool result.",
                },
                "save": {
                    "type": "boolean",
                    "default": True,
                    "description": "Save the appshot JSON and PNG under .screen/appshots or save_dir.",
                },
                "save_dir": {"type": "string", "description": "Optional directory for saved appshot JSON and PNG files."},
                "strict_ui": {
                    "type": "boolean",
                    "default": False,
                    "description": "Fail the tool if UIAutomator cannot return nodes. By default the screenshot is still returned.",
                },
                "limit": {"type": "integer", "default": 220},
            },
            "additionalProperties": False,
        },
        "handler": tool_appshot,
    },
    "android_observe": {
        "description": "Observe the Android screen. By default this tries a fast Playwright WebView DOM snapshot first, then falls back to UIAutomator; optionally include screenshot/XML.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "include_screenshot": {"type": "boolean", "default": False},
                "include_xml": {"type": "boolean", "default": False},
                "prefer_webview": {
                    "type": "boolean",
                    "default": True,
                    "description": "Prefer a fast Playwright WebView DOM snapshot when a debuggable WebView is available. Set false to force UIAutomator.",
                },
                "include_webview": {
                    "type": "boolean",
                    "description": "Attach a Playwright WebView DOM snapshot to a UIAutomator observation when available. When omitted, follows prefer_webview.",
                },
                "limit": {"type": "integer", "default": 160},
            },
            "additionalProperties": False,
        },
        "handler": tool_observe,
    },
    "android_tap_text": {
        "description": "Tap visible text. For debuggable WebViews this uses a fast Playwright DOM click first, then falls back to UIAutomator, avoiding screenshot/VLM latency.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "text": {"type": "string"},
                "exact": {"type": "boolean", "default": True},
                "include_resource_id": {"type": "boolean", "default": False},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        "handler": tool_tap_text,
    },
    "android_tap": {
        "description": "Tap absolute screen coordinates on the Android device.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
            },
            "required": ["x", "y"],
            "additionalProperties": False,
        },
        "handler": tool_tap,
    },
    "android_swipe": {
        "description": "Swipe between absolute screen coordinates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "start_x": {"type": "integer"},
                "start_y": {"type": "integer"},
                "end_x": {"type": "integer"},
                "end_y": {"type": "integer"},
                "duration_ms": {"type": "integer", "default": 300},
            },
            "required": ["start_x", "start_y", "end_x", "end_y"],
            "additionalProperties": False,
        },
        "handler": tool_swipe,
    },
    "android_type_text": {
        "description": "Type text into the focused field using the fastest available path: Playwright WebView DOM assignment, ADB Keyboard IME, or batched adb shell input.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "text": {"type": "string"},
                "clear_first": {"type": "boolean", "default": False},
                "clear_count": {"type": "integer", "default": 80},
                "enter": {"type": "boolean", "default": False},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        "handler": tool_type_text,
    },
    "android_press_key": {
        "description": "Press an Android key by alias, KEYCODE name, or numeric keycode.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "key": {"type": ["string", "integer"]},
            },
            "required": ["key"],
            "additionalProperties": False,
        },
        "handler": tool_press_key,
    },
    "android_wake_unlock": {
        "description": "Wake the Android device and optionally dismiss the keyguard.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "dismiss_keyguard": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
        "handler": tool_wake_unlock,
    },
    "android_open_url": {
        "description": "Open a URL on the selected Android device through an ACTION_VIEW intent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "url": {"type": "string"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "handler": tool_open_url,
    },
    "android_open_app": {
        "description": "Launch an Android app by package name, or a specific activity by component.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "package": {"type": "string"},
                "activity": {
                    "type": "string",
                    "description": "Optional activity class or package/activity component.",
                },
            },
            "required": ["package"],
            "additionalProperties": False,
        },
        "handler": tool_open_app,
    },
    "android_shell": {
        "description": "Run a shell command on the selected Android device through adb.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "command": {"type": "string"},
                "timeout_sec": {"type": "number", "default": 20},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        "handler": tool_shell,
    },
    "android_webview_pages": {
        "description": "List debuggable Android WebViews through Playwright Android.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "timeout_sec": {"type": "number", "default": 10},
            },
            "additionalProperties": False,
        },
        "handler": tool_webview_pages,
    },
    "android_webview_runtime": {
        "description": "Inspect or manage the persistent Playwright Android WebView worker and caches.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "clear", "close"],
                    "default": "status",
                    "description": "status inspects caches, clear drops cached pages/devices, close stops the worker process.",
                },
                "serial": {
                    "type": "string",
                    "description": "Optional serial to scope status or clear to one Android device.",
                },
            },
            "additionalProperties": False,
        },
        "handler": tool_webview_runtime,
    },
    "android_webview_eval": {
        "description": "Evaluate JavaScript in a debuggable Android WebView through Playwright Android.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "page_id": {"type": "string"},
                "package": {"type": "string", "description": "Optional Android package id for selecting a WebView."},
                "socket_name": {"type": "string", "description": "Optional WebView DevTools socket name for selecting a WebView."},
                "url_contains": {"type": "string"},
                "title_contains": {"type": "string"},
                "expression": {"type": "string"},
                "await_promise": {"type": "boolean", "default": True},
                "return_by_value": {"type": "boolean", "default": True},
                "max_result_chars": {
                    "type": "integer",
                    "default": 500,
                    "description": "Maximum characters for each string in the returned result. Use 0 to disable truncation.",
                },
                "timeout_sec": {"type": "number", "default": 10},
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
        "handler": tool_webview_eval,
    },
    "android_webview_cdp": {
        "description": "Send a raw Chrome DevTools Protocol command to a debuggable Android WebView through the persistent Playwright worker.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "page_id": {"type": "string"},
                "package": {"type": "string", "description": "Optional Android package id for selecting a WebView."},
                "socket_name": {"type": "string", "description": "Optional WebView DevTools socket name for selecting a WebView."},
                "url_contains": {"type": "string"},
                "title_contains": {"type": "string"},
                "method": {
                    "type": "string",
                    "description": "CDP method name, for example Runtime.evaluate or Network.enable.",
                },
                "params": {
                    "type": "object",
                    "description": "CDP command parameters.",
                    "additionalProperties": True,
                },
                "max_result_chars": {
                    "type": "integer",
                    "default": 500,
                    "description": "Maximum characters for each string in the returned result. Use 0 to disable truncation.",
                },
                "timeout_sec": {"type": "number", "default": 10},
            },
            "required": ["method"],
            "additionalProperties": False,
        },
        "handler": tool_webview_cdp,
    },
    "android_start_recording": {
        "description": "Start recording deterministic Android actions into a trace for later recipe generation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "name": {"type": "string"},
                "include_screenshots": {"type": "boolean", "default": False},
                "redact_text": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, typed text values are replaced with character counts in the trace.",
                },
                "after_delay_sec": {"type": "number", "default": 0.25},
            },
            "additionalProperties": False,
        },
        "handler": tool_start_recording,
    },
    "android_record_checkpoint": {
        "description": "Capture a named UI checkpoint in the active Android recording, useful after manual scrcpy navigation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "label": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "handler": tool_record_checkpoint,
    },
    "android_stop_recording": {
        "description": "Stop the active Android recording and write its trace.json file.",
        "inputSchema": {
            "type": "object",
            "properties": {"serial": {"type": "string"}},
            "additionalProperties": False,
        },
        "handler": tool_stop_recording,
    },
    "android_create_recipe": {
        "description": "Convert a recorded Android trace into a selector-first replay recipe.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "trace": {"type": "string", "description": "Trace path, recording id, or recording directory."},
                "name": {"type": "string"},
                "output_path": {"type": "string"},
            },
            "required": ["trace"],
            "additionalProperties": False,
        },
        "handler": tool_create_recipe,
    },
    "android_replay_recipe": {
        "description": "Replay a selector-first Android recipe using UIAutomator selectors before coordinate fallback.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "recipe": {"type": "string", "description": "Recipe path or recipe name under .android-use/recipes."},
                "dry_run": {"type": "boolean", "default": False},
                "strict_verify": {"type": "boolean", "default": False},
                "step_delay_sec": {"type": "number", "default": 0.25},
            },
            "required": ["recipe"],
            "additionalProperties": False,
        },
        "handler": tool_replay_recipe,
    },
    "android_start_video_recording": {
        "description": "Start MP4 screen recording through scrcpy.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "name": {"type": "string", "description": "Optional file name slug for the generated recording."},
                "output_path": {"type": "string", "description": "Optional local output path. Defaults under .screen/video-recordings/."},
                "record_format": {
                    "type": "string",
                    "default": "mp4",
                    "enum": ["mp4", "mkv"],
                    "description": "scrcpy recording container format. Use mp4 for user-facing video.",
                },
                "max_size": {
                    "type": "integer",
                    "default": 0,
                    "description": "scrcpy max video size. 0 keeps the device stream at native size.",
                },
                "bit_rate": {"type": "string", "default": "8M"},
                "audio": {
                    "type": "boolean",
                    "default": False,
                    "description": "Enable audio capture/forwarding. Disabled by default for reliability.",
                },
                "start_marker": {
                    "type": "boolean",
                    "default": True,
                    "description": "Capture a best-effort start-anchor screenshot in the background without delaying tool return.",
                },
                "extra_args": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
        "handler": tool_start_video_recording,
    },
    "android_stop_video_recording": {
        "description": "Stop an active scrcpy video recording if one exists.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "timeout_sec": {"type": "number", "default": 5},
            },
            "additionalProperties": False,
        },
        "handler": tool_stop_video_recording,
    },
    "android_index_source": {
        "description": "Scan Android app source code and write an app-map JSON with activities, routes, ids, and visible labels.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string"},
                "output_path": {"type": "string"},
                "max_files": {"type": "integer", "default": 2000},
            },
            "required": ["source_path"],
            "additionalProperties": False,
        },
        "handler": tool_index_source,
    },
    "android_start_scrcpy": {
        "description": "Start or reuse a visible scrcpy window through the native macOS Android Use.app wrapper.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "serials": {
                    "type": ["array", "string"],
                    "items": {"type": "string"},
                    "description": "Optional list or comma-separated string of serials to mirror.",
                },
                "app_path": {"type": "string", "description": "Optional output path for the generated Android app wrapper."},
                "max_size": {
                    "type": "integer",
                    "default": 0,
                    "description": "scrcpy max video size. 0 keeps the device stream at its native size.",
                },
                "bit_rate": {"type": "string", "default": "8M"},
                "audio": {
                    "type": "boolean",
                    "default": False,
                    "description": "Enable scrcpy audio forwarding. Disabled by default for screen-control stability.",
                },
                "keyboard": {
                    "type": "string",
                    "default": "sdk",
                    "enum": ["disabled", "sdk", "uhid", "aoa"],
                    "description": "Keyboard injection mode. sdk is best for normal text entry; uhid/aoa simulate a physical keyboard and need Android keyboard layout setup.",
                },
                "prefer_text": {
                    "type": "boolean",
                    "default": True,
                    "description": "With keyboard=sdk, inject alpha characters and spaces as text events so typing in scrcpy works better.",
                },
                "legacy_paste": {
                    "type": "boolean",
                    "default": False,
                    "description": "Use scrcpy legacy paste behavior for devices where normal clipboard paste fails.",
                },
                "stay_awake": {"type": "boolean", "default": False},
                "turn_screen_off": {"type": "boolean", "default": False},
                "keep_alive": {
                    "type": "boolean",
                    "default": True,
                    "description": "Accepted for compatibility. Visible windows are launched through the macOS app wrapper; resident/on-demand checks reopen the app when needed.",
                },
                "fixed_window": {
                    "type": "boolean",
                    "default": True,
                    "description": "Set explicit initial window width/height based on the device screen.",
                },
                "borderless": {
                    "type": "boolean",
                    "default": False,
                    "description": "Remove window decorations. This prevents normal resizing but also makes the window hard to drag on macOS.",
                },
                "window_width": {"type": "integer"},
                "window_height": {"type": "integer"},
                "always_on_top": {"type": "boolean", "default": False},
                "lock_window_size": {
                    "type": "boolean",
                    "default": True,
                    "description": "Accepted for compatibility. App-wrapper windows use scrcpy's initial window size instead of a separate lock helper.",
                },
                "lock_window_continuous": {
                    "type": "boolean",
                    "default": False,
                    "description": "Keep enforcing the scrcpy window size continuously. Disabled by default so the helper does not interfere with keyboard focus.",
                },
                "window_title": {"type": "string"},
                "window_scale": {
                    "type": "number",
                    "default": 0.5,
                    "description": "Initial window scale when window_width/window_height are not provided. Does not resize the scrcpy video stream.",
                },
                "render_driver": {
                    "type": "string",
                    "default": "software",
                    "description": "scrcpy SDL renderer. software is preferred for Codex AppShot compatibility.",
                },
                "extra_args": {"type": "array", "items": {"type": "string"}},
                "force": {
                    "type": "boolean",
                    "default": False,
                    "description": "Start a new scrcpy process even if one is already visible for the same serial.",
                },
            },
            "additionalProperties": False,
        },
        "handler": tool_start_scrcpy,
    },
    "android_start_scrcpy_app": {
        "description": "Start or reuse scrcpy through the native macOS Android Use.app wrapper.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "app_path": {"type": "string", "description": "Optional output path for the generated Android Use.app."},
                "max_size": {
                    "type": "integer",
                    "default": 0,
                    "description": "scrcpy max video size. 0 keeps the device stream at its native size.",
                },
                "bit_rate": {"type": "string", "default": "8M"},
                "audio": {"type": "boolean", "default": False},
                "keyboard": {
                    "type": "string",
                    "default": "sdk",
                    "enum": ["disabled", "sdk", "uhid", "aoa"],
                },
                "prefer_text": {"type": "boolean", "default": True},
                "legacy_paste": {"type": "boolean", "default": False},
                "stay_awake": {"type": "boolean", "default": False},
                "turn_screen_off": {"type": "boolean", "default": False},
                "window_width": {"type": "integer"},
                "window_height": {"type": "integer"},
                "window_title": {
                    "type": "string",
                    "description": "Optional title. Defaults to Android device name, then model, then Android.",
                },
                "window_scale": {
                    "type": "number",
                    "default": 0.5,
                    "description": "Initial window scale when window_width/window_height are not provided. Does not resize the scrcpy video stream.",
                },
                "render_driver": {
                    "type": "string",
                    "default": "software",
                    "description": "scrcpy SDL renderer. software is preferred for Codex AppShot compatibility.",
                },
                "extra_args": {"type": "array", "items": {"type": "string"}},
                "force": {
                    "type": "boolean",
                    "default": False,
                    "description": "Start a new app-wrapper scrcpy window even if one is already visible for the same serial.",
                },
            },
            "additionalProperties": False,
        },
        "handler": tool_start_scrcpy_app,
    },
    "android_scrcpy_resident_status": {
        "description": "Report the scrcpy resident monitor.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "handler": tool_scrcpy_resident_status,
    },
    "android_stop_scrcpy": {
        "description": "Stop scrcpy processes launched by this MCP server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "all": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
        "handler": tool_stop_scrcpy,
    },
    "android_start_screen_viewer": {
        "description": "Start a local Codex-friendly Android action timeline web UI backed by screenshots. It records only Android Use tool action steps, without video streaming or periodic screen polling.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "description": "Optional local port. Uses a free port when omitted."},
                "interval_ms": {
                    "type": "integer",
                    "default": 1000,
                    "description": "Local event-stream file check interval. It does not capture screenshots.",
                },
                "session_dir": {"type": "string", "description": "Optional directory for timeline events and screenshots."},
                "max_events": {"type": "integer", "default": 80},
            },
            "additionalProperties": False,
        },
        "handler": tool_start_screen_viewer,
    },
    "android_stop_screen_viewer": {
        "description": "Stop Android screen viewer processes launched by this MCP server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "all": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
        "handler": tool_stop_screen_viewer,
    },
    "android_agent_step": {
        "description": "Run one Agent-TARS-style Android step. Hybrid mode first tries UIAutomator text grounding, then VLM visual grounding.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "instruction": {"type": "string"},
                "execute": {"type": "boolean", "default": False},
                "model": {"type": "string"},
                "provider": {
                    "type": "string",
                    "default": "auto",
                    "enum": ["auto", "openai-computer", "openai-vision", "openai-compatible"],
                },
                "mode": {
                    "type": "string",
                    "default": "hybrid",
                    "enum": ["hybrid", "visual-grounding", "uiautomator", "accessibility"],
                },
                "coordinate_mode": {
                    "type": "string",
                    "description": "absolute or normalized_1000. Defaults from model/env.",
                },
                "history": {"type": "array", "items": {"type": "object"}},
                "show_scrcpy": {
                    "type": "boolean",
                    "default": True,
                    "description": "Ensure a visible desktop scrcpy window before executing. Reuses an existing visible scrcpy process.",
                },
            },
            "required": ["instruction"],
            "additionalProperties": False,
        },
        "handler": tool_agent_step,
    },
    "android_agent_run": {
        "description": "Run a bounded Agent-TARS-style Android loop: observe screenshot/UI tree, reason with VLM, act, and observe again. Defaults to executing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "instruction": {"type": "string"},
                "max_steps": {"type": "integer", "default": 5},
                "dry_run": {"type": "boolean", "default": False},
                "delay_sec": {"type": "number", "default": 0.25},
                "model": {"type": "string"},
                "provider": {
                    "type": "string",
                    "default": "auto",
                    "enum": ["auto", "openai-computer", "openai-vision", "openai-compatible"],
                },
                "mode": {
                    "type": "string",
                    "default": "hybrid",
                    "enum": ["hybrid", "visual-grounding", "uiautomator", "accessibility"],
                },
                "coordinate_mode": {
                    "type": "string",
                    "description": "absolute or normalized_1000. Defaults from model/env.",
                },
                "show_scrcpy": {
                    "type": "boolean",
                    "default": True,
                    "description": "Ensure a visible desktop scrcpy window before executing. Reuses an existing visible scrcpy process.",
                },
            },
            "required": ["instruction"],
            "additionalProperties": False,
        },
        "handler": tool_agent_run,
    },
    "android_agent_tars_step": {
        "description": "Alias for android_agent_step with Agent-TARS/UI-TARS mobile action semantics.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "instruction": {"type": "string"},
                "execute": {"type": "boolean", "default": True},
                "model": {"type": "string"},
                "provider": {
                    "type": "string",
                    "default": "auto",
                    "enum": ["auto", "openai-computer", "openai-vision", "openai-compatible"],
                },
                "mode": {
                    "type": "string",
                    "default": "hybrid",
                    "enum": ["hybrid", "visual-grounding", "uiautomator", "accessibility"],
                },
                "coordinate_mode": {"type": "string"},
                "show_scrcpy": {"type": "boolean", "default": True},
            },
            "required": ["instruction"],
            "additionalProperties": False,
        },
        "handler": lambda args: tool_agent_step({**args, "execute": args.get("execute", True)}),
    },
    "android_agent_tars_run": {
        "description": "Alias for android_agent_run. This is the preferred natural-language Android operator loop.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string"},
                "instruction": {"type": "string"},
                "max_steps": {"type": "integer", "default": 5},
                "dry_run": {"type": "boolean", "default": False},
                "delay_sec": {"type": "number", "default": 0.25},
                "model": {"type": "string"},
                "provider": {
                    "type": "string",
                    "default": "auto",
                    "enum": ["auto", "openai-computer", "openai-vision", "openai-compatible"],
                },
                "mode": {
                    "type": "string",
                    "default": "hybrid",
                    "enum": ["hybrid", "visual-grounding", "uiautomator", "accessibility"],
                },
                "coordinate_mode": {"type": "string"},
                "show_scrcpy": {"type": "boolean", "default": True},
            },
            "required": ["instruction"],
            "additionalProperties": False,
        },
        "handler": tool_agent_run,
    },
}
