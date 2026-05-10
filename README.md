# Android Use

Android Use is a local Codex plugin for operating an attached Android device through adb and scrcpy, with an Agent-TARS-style natural-language operator loop.

The first version is intentionally small and practical:

- device discovery through `adb devices -l`;
- screenshots through `adb exec-out screencap -p`;
- inline screen display through `android_show_screen`;
- Codex-embedded WebRTC video through `android_start_webrtc_viewer`;
- a Codex-friendly local web viewer through `android_start_screen_viewer`;
- taps, swipes, key events, simple text input, and adb shell commands;
- wake/unlock, open URL, and launch app helpers;
- scrcpy launch/stop helpers, defaulting to a draggable window whose size is locked by a small macOS helper when Accessibility permission is available;
- fast UIAutomator observation and text tapping through `android_observe` and `android_tap_text`;
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

`ANDROID_USE_VLM_BASE_URL` may be either a base URL or a complete `/chat/completions` URL.

## Local Smoke Test

```bash
python3 scripts/smoke_test_mcp.py
python3 scripts/test_android_use_mcp.py
python3 scripts/android_use_mcp.py
```

To show the device inside Codex, call `android_show_screen` for a current screenshot or `android_start_screen_viewer` for an auto-refreshing local web page.

For smoother Codex-embedded video, call `android_start_webrtc_viewer` and open the returned localhost URL in Codex. This uses scrcpy's H.264 recording stream through a local WebRTC server with low-latency defaults (`max_size=960`, `bit_rate=4M`, `max_fps=30`, no PyAV buffering, stale-frame dropping) and requires the plugin virtualenv dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install aiortc aiohttp av
```

The MCP server uses newline-delimited JSON-RPC over stdio and has no third-party Python dependencies.

## Design Reference

This plugin borrows the high-level operator loop used by Agent TARS and UI-TARS Desktop: UI/screenshot observation, multimodal reasoning, UI-TARS mobile `Thought`/`Action` output, small adb actions, and feedback. It supports Agent TARS-style modes:

- `uiautomator`: text/UI-tree grounding only, fastest and works without a VLM.
- `visual-grounding`: screenshot plus model action prediction. Providers: OpenAI Responses computer tool (`openai-computer`), OpenAI multimodal Responses (`openai-vision`), or OpenAI-compatible chat completions (`openai-compatible`).
- `hybrid`: UIAutomator first, then visual-grounding fallback.

It does not vendor Agent TARS/UI-TARS code.
