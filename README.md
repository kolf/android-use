# Android Use Plugins

Android Use Plugins is a local Codex plugin package named `android-use-plugins`. It operates an attached Android device through adb and scrcpy, with an Agent-TARS-style natural-language operator loop and Xiaoluxue-specific fast paths.

## Team Install

```bash
git clone https://gitlab.xiaoluxue.cn/shixiankang/android-use.git ~/.agents/plugins/android-use-plugins
cd ~/.agents/plugins/android-use-plugins
./install.sh
./doctor.sh
```

Then restart Codex and enable `Android Use` from the Xiaoluxue local plugin marketplace.

The installer writes or updates `~/.agents/plugins/marketplace.json` with the `android-use-plugins` entry. See [docs/team-install.md](docs/team-install.md) for the Chinese team guide.

The first version is intentionally small and practical:

- device discovery through `adb devices -l`;
- screenshots through `adb exec-out screencap -p`;
- inline screen display through `android_show_screen`;
- resident visible desktop mirroring through `android_start_scrcpy`; the MCP server keeps one scrcpy window alive for one physical device by default;
- optional Codex-embedded WebRTC video through `android_start_webrtc_viewer`;
- a Codex-friendly local web viewer through `android_start_screen_viewer`;
- taps, swipes, key events, simple text input, and adb shell commands;
- wake/unlock, open URL, and launch app helpers;
- scrcpy launch/stop helpers, defaulting to a draggable video-only window with keep-alive restart and a small macOS size-lock helper when Accessibility permission is available;
- fast UIAutomator observation and text tapping through `android_observe` and `android_tap_text`;
- debuggable WebView discovery/eval through `android_webview_pages` and `android_webview_eval`;
- Xiaoluxue app-only URL routing, runtime bridge/status, environment, native subject-map routing, native map, course, and exercise fast paths through `xiaoluxue_open_app_url`, `xiaoluxue_runtime_status`, `xiaoluxue_switch_env`, `xiaoluxue_open_native_subject`, `xiaoluxue_map_fast_path`, `xiaoluxue_course_fast_path`, `xiaoluxue_exercise_fast_path`, and lower-level Xiaoluxue tools;
- action recording, selector-first recipe generation/replay, and source indexing for fast repeat workflows;
- Agent-TARS/UI-TARS-style hybrid operation through `android_agent_tars_step` and `android_agent_tars_run`: UI tree grounding first, then visual-grounding VLM fallback.

## Setup

Install Android platform tools and authorize the device:

```bash
adb devices -l
```

This local project also looks for a project-local install at:

```text
tools/android-platform-tools/platform-tools/adb
```

When running inside Codex, the MCP server sets the subprocess `HOME` to the Android Use project root, so adb can store key material in `.android` without writing to your real `~/.android`.

Install scrcpy if you want live mirroring:

```bash
brew install scrcpy
```

Optional environment variables:

```bash
export ANDROID_USE_ADB=/path/to/adb
export ANDROID_USE_SCRCPY=/path/to/scrcpy

# OpenAI native providers
export OPENAI_API_KEY=...
export ANDROID_USE_AGENT_PROVIDER=openai-computer
export ANDROID_USE_OPENAI_COMPUTER_MODEL=gpt-5.5
# Or use a general vision/reasoning model instead of the computer tool:
# export ANDROID_USE_AGENT_PROVIDER=openai-vision
# export ANDROID_USE_OPENAI_VISION_MODEL=gpt-5.5

# OpenAI-compatible VLM providers such as Seed/UI-TARS-style endpoints
export ANDROID_USE_VLM_BASE_URL=https://your-provider.example/v1
export ANDROID_USE_VLM_API_KEY=...
export ANDROID_USE_VLM_MODEL=seed-1-5-vl
# Optional: absolute or normalized_1000. Seed/UI-TARS-style model names default to normalized_1000.
export ANDROID_USE_VLM_COORDINATE_MODE=normalized_1000
```

The MCP server also loads local private settings from `~/.config/android-use/env` when it starts. Use this for API keys in the Codex desktop app, because desktop plugin processes may not inherit shell startup files:

```bash
ANDROID_USE_AGENT_PROVIDER=openai-compatible
ANDROID_USE_VLM_BASE_URL=https://ark.cn-beijing.volces.com/api/plan/v3
ANDROID_USE_VLM_MODEL=doubao-seedream-5.0-lite
ANDROID_USE_VLM_API_KEY=your_ark_api_key
```

`ANDROID_USE_VLM_BASE_URL` may be either a base URL or a complete `/chat/completions` URL.

## Local Smoke Test

```bash
python3 scripts/smoke_test_mcp.py
python3 scripts/test_android_use_mcp.py
python3 scripts/android_use_mcp.py
```

For normal interactive operation, use the default visible scrcpy desktop window. The MCP server starts a background resident monitor on startup and keeps one visible scrcpy window alive for one physical device, preferring `ANDROID_USE_SCRCPY_RESIDENT_SERIALS`, `ANDROID_USE_SERIAL`, or `ANDROID_SERIAL` when set. `android_agent_run` and `android_agent_step` also ensure that window before executing unless `show_scrcpy=false` is passed, and they reuse an existing visible scrcpy process for the same device instead of starting a duplicate.

To show the device inside Codex, call `android_show_screen` for a current screenshot or `android_start_screen_viewer` for an auto-refreshing local web page.

For Codex-embedded video, call `android_start_webrtc_viewer` explicitly and open the returned localhost URL in Codex. This is not started by default. It uses scrcpy's H.264 recording stream through a local WebRTC server with low-latency defaults (`max_size=960`, `bit_rate=4M`, `max_fps=30`, no PyAV buffering, stale-frame dropping) and requires the plugin virtualenv dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install aiortc aiohttp av
```

The MCP server uses newline-delimited JSON-RPC over stdio and has no third-party Python dependencies.

`android_start_scrcpy` disables scrcpy audio by default and launches through a small supervisor so accidental scrcpy exits are restarted. It also starts scrcpy with `keyboard=sdk` and `prefer_text=true` by default so typing in the scrcpy window uses text injection instead of fragile raw key combinations. Pass `audio=true` if you explicitly need audio forwarding, `keep_alive=false` for a one-off manual launch, `keyboard=uhid` if you need physical-keyboard behavior, or `legacy_paste=true` if normal clipboard paste fails on a device.

The resident monitor never starts WebRTC. Set `ANDROID_USE_SCRCPY_RESIDENT_SERIALS` to a comma-separated serial list only when multiple resident windows are wanted. Set `ANDROID_USE_SCRCPY_RESIDENT=0` to disable the always-on scrcpy window, or `ANDROID_USE_SCRCPY_RESIDENT_INTERVAL_SEC` to change the watchdog interval. Use `android_scrcpy_resident_status` to check the monitor and its last restart result.

## Fast Repeat Workflows

For app flows you run often, prefer deterministic recipes over repeated visual reasoning:

1. Start a recording with `android_start_recording`.
2. Drive the app through deterministic tools such as `android_tap_text`, `android_tap`, `android_swipe`, `android_type_text`, `android_open_app`, and `android_press_key`.
3. Stop with `android_stop_recording`; this writes `.android-use/recordings/<id>/trace.json`.
4. Convert the trace with `android_create_recipe`.
5. Replay with `android_replay_recipe`.

Recipes store selector candidates first (`resource-id`, content description, visible text) and keep coordinate fallback for cases where the selector is missing. `android_record_checkpoint` can capture page fingerprints after manual scrcpy navigation, but manual gestures are not yet converted into actions automatically.

To build a static app map from source code, call `android_index_source` with an Android source directory. It scans Kotlin, Java, XML, TypeScript/JavaScript, and Dart files for activities, routes, resource ids, labels, content descriptions, and test tags, then writes `.android-use/app-maps/app-map-*.json`.

## WebView Fast Path

For hybrid apps whose WebViews are visible in Chrome's `chrome://inspect/#devices`, use the WebView tools before visual reasoning:

- `android_webview_pages` forwards `webview_devtools_remote*` sockets and lists DevTools targets with title, URL, size, and `webSocketDebuggerUrl`.
- `android_webview_eval` evaluates JavaScript in the selected WebView via Chrome DevTools Protocol.
- `android_open_url` routes Xiaoluxue app-only H5 URLs (`stu.xiaoluxue.com` and `*.xiaoluxue.cn`) through the Xiaoluxue student app instead of handing them to a browser.
- `xiaoluxue_open_app_url` opens a Xiaoluxue H5 URL through the app vessel WebView route, waits for the matching runtime URL, and can install the runtime bridge in one call.
- `xiaoluxue_runtime_status` reuses cached WebView DevTools forwards when possible, validates the live runtime URL, and installs `window.__androidUse.xiaoluxue` helpers for snapshots, safe overlay reveal, widget jumps, text clicks, and playback-rate setup.
- Xiaoluxue course pages can use `xiaoluxue_course_snapshot` to read widgets and media state, `xiaoluxue_set_speed` to select a playback speed, and `xiaoluxue_goto_widget` to jump by widget index, name, or `last=true`.
- `xiaoluxue_switch_env` opens the Galaxy Zhixue config app (`com.xiaoluxue.ai.config`), selects the student `API 环境` such as `test`, submits it, and reopens Xiaoluxue student by default.
- Native Xiaoluxue study-map pages can use `xiaoluxue_open_native_subject` to jump through the app-only `xlx://router/study/subject` route, `xiaoluxue_map_snapshot` to read subject/chapter/visible indexes/actions, and `xiaoluxue_map_fast_path` for one-pass actions such as `1.5 题型突破`, `错题`, `笔记本`, `学习任务`, and `薄弱知识`. When `subject_id` or `subject` is provided, `xiaoluxue_map_fast_path` routes to the subject map first; known presets such as `语文 1.5 题型突破` skip slow `uiautomator dump` entirely.
- `xiaoluxue_course_fast_path` is the one-call shortcut for the common flow: open a guide widget when needed, set speed to 2x, then jump to the last widget by default.
- `xiaoluxue_open_knowledge_guide` defaults to preserving the current Xiaoluxue H5 host, so a known shortcut opened from a `stu.test.xiaoluxue.cn` session stays on the test H5 host instead of falling back to production.
- Xiaoluxue `/exercise` pages can use `xiaoluxue_exercise_snapshot` to read question/options/buttons/progress, `xiaoluxue_exercise_action` for one semantic click, and `xiaoluxue_exercise_fast_path` for "select option -> submit -> continue" style flows.

`xiaoluxue_goto_widget` defaults to `mode=reload`, which applies `redirectWidgetIndex` so the H5 course state initializes at the target widget. Use `mode=scroll` only for fast visual positioning because it does not trigger the course `goto` state machine.

## Design Reference

This plugin borrows the high-level operator loop used by Agent TARS and UI-TARS Desktop: UI/screenshot observation, multimodal reasoning, UI-TARS mobile `Thought`/`Action` output, small adb actions, and feedback. It supports Agent TARS-style modes:

- `uiautomator`: text/UI-tree grounding only, fastest and works without a VLM.
- `visual-grounding`: screenshot plus model action prediction. Providers: OpenAI Responses computer tool (`openai-computer`), OpenAI multimodal Responses (`openai-vision`), or OpenAI-compatible chat completions (`openai-compatible`).
- `hybrid`: UIAutomator first, then visual-grounding fallback.

It does not vendor Agent TARS/UI-TARS code.
