# Android

`android-use-plugins` 是一个本地 Codex 插件，用于通过 adb、scrcpy、截图、输入事件和可选视觉模型来操作已连接的 Android 设备。它内置了 Agent-TARS 风格的自然语言操作循环，也包含小鹿爱学专用快路径。

## 团队安装

```bash
git clone https://gitlab.xiaoluxue.cn/shixiankang/android-use.git ~/.agents/plugins/android-use-plugins
cd ~/.agents/plugins/android-use-plugins
./install.sh
./doctor.sh
```

完成后重启 Codex，并在本地插件列表里启用 `Android`。

安装脚本会写入或更新 `~/.agents/plugins/marketplace.json` 中的 `android-use-plugins` 条目。团队安装细节见 [docs/team-install.md](docs/team-install.md)。

当前版本重点解决高频调试需求：

- 通过 `adb devices -l` 发现设备；
- 通过 `adb exec-out screencap -p` 获取截图；
- 通过 `android_show_screen` 在 Codex 内直接显示当前屏幕；
- 通过 `android_start_scrcpy` 打开桌面 scrcpy 镜像窗口；
- Android 工具被调用时默认只保留一个可见 scrcpy 窗口；
- 可选通过 `android_start_webrtc_viewer` 打开 Codex 内嵌 WebRTC 视频；
- 可选通过 `android_start_screen_viewer` 打开截图自动刷新的本地页面；
- 支持点击、滑动、按键、简单文本输入和 adb shell；
- 支持唤醒解锁、打开 URL、启动 App；
- scrcpy 启动阶段异常退出会自动重试，窗口显示稳定后如果用户手动关闭，会尊重手动关闭；下一次 Android 工具调用时再重新弹窗；
- 支持 `android_observe` 和 `android_tap_text` 的 UIAutomator 快速观察和文字点击；
- 支持 `android_webview_pages` 和 `android_webview_eval` 的 WebView DevTools 调试；
- 支持小鹿爱学 App 内 URL 打开、运行时桥接、环境切换、原生学科地图、课程页、练习页快路径；
- 支持录制动作、生成 selector 优先的 recipe、复放 recipe，以及根据源码生成静态页面/控件索引；
- 支持 `android_agent_tars_step` 和 `android_agent_tars_run` 的混合模式：先走 UI 树定位，失败时再用视觉模型。

## 环境准备

安装 Android platform tools，并确认设备已经授权：

```bash
adb devices -l
```

本项目也会查找项目内置路径：

```text
tools/android-platform-tools/platform-tools/adb
```

在 Codex 内运行 MCP 服务时，插件会把子进程的 `HOME` 设置为 Android Use 项目根目录，因此 adb key 会写入项目内 `.android`，不会污染真实的 `~/.android`。

如果需要桌面实时镜像，安装 scrcpy：

```bash
brew install scrcpy
```

可选环境变量：

```bash
export ANDROID_USE_ADB=/path/to/adb
export ANDROID_USE_SCRCPY=/path/to/scrcpy

# OpenAI 原生 provider
export OPENAI_API_KEY=...
export ANDROID_USE_AGENT_PROVIDER=openai-computer
export ANDROID_USE_OPENAI_COMPUTER_MODEL=gpt-5.5

# 也可以使用通用视觉/推理模型
# export ANDROID_USE_AGENT_PROVIDER=openai-vision
# export ANDROID_USE_OPENAI_VISION_MODEL=gpt-5.5

# OpenAI-compatible 视觉模型，例如 Seed/UI-TARS 风格接口
export ANDROID_USE_VLM_BASE_URL=https://your-provider.example/v1
export ANDROID_USE_VLM_API_KEY=...
export ANDROID_USE_VLM_MODEL=seed-1-5-vl

# 可选：absolute 或 normalized_1000。Seed/UI-TARS 风格模型默认 normalized_1000。
export ANDROID_USE_VLM_COORDINATE_MODE=normalized_1000
```

MCP 服务启动时还会读取 `~/.config/android-use/env`。在 Codex 桌面端里，插件进程不一定继承 shell 启动文件，因此 API key 建议放在这个文件中：

```bash
ANDROID_USE_AGENT_PROVIDER=openai-compatible
ANDROID_USE_VLM_BASE_URL=https://ark.cn-beijing.volces.com/api/plan/v3
ANDROID_USE_VLM_MODEL=doubao-seedream-5.0-lite
ANDROID_USE_VLM_API_KEY=your_ark_api_key
```

`ANDROID_USE_VLM_BASE_URL` 可以是基础 URL，也可以是完整的 `/chat/completions` URL。

## 本地自检

```bash
python3 scripts/smoke_test_mcp.py
python3 scripts/test_android_use_mcp.py
python3 scripts/android_use_mcp.py
```

更完整的检查建议运行：

```bash
./doctor.sh
```

正常交互时优先使用默认 scrcpy 桌面窗口。Android 工具被调用时，MCP 服务会为一个物理设备打开一个可见 scrcpy 窗口，优先使用 `ANDROID_USE_SCRCPY_RESIDENT_SERIALS`、`ANDROID_USE_SERIAL` 或 `ANDROID_SERIAL` 指定的设备。用户手动关闭已经稳定显示的 scrcpy 窗口后，常驻监控不会马上重新拉起；下一次 Android 工具调用会清除手动关闭标记并重新打开窗口。`android_agent_run` 和 `android_agent_step` 默认也会在执行动作前确保该窗口存在，除非显式传入 `show_scrcpy=false`。

要在 Codex 中查看设备画面，可以调用：

- `android_show_screen`：返回当前截图；
- `android_start_screen_viewer`：打开本地自动刷新截图页面；
- `android_start_webrtc_viewer`：仅在明确需要 Codex 内嵌视频时调用。

WebRTC 不会默认启动。它通过本地 WebRTC 服务转发 scrcpy 的 H.264 录制流，默认低延迟参数为 `max_size=960`、`bit_rate=4M`、`max_fps=30`，并依赖插件虚拟环境中的 `aiortc`、`aiohttp`、`av`：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install aiortc aiohttp av
```

MCP 服务使用基于 stdio 的 newline-delimited JSON-RPC，不需要额外 Python 三方依赖。

## scrcpy 行为

`android_start_scrcpy` 默认：

- 禁用音频，降低资源占用；
- 使用可拖动的视频窗口；
- 通过 supervisor 重试启动阶段闪退；
- 使用 `keyboard=sdk` 和 `prefer_text=true`，让 scrcpy 窗口输入文字时尽量走文本注入；
- 窗口稳定显示后，如果用户手动关闭，会尊重这次关闭；
- 下一次 Android 工具调用时再重新打开 scrcpy；
- 复用同一设备已有的可见 scrcpy 进程，避免打开多个窗口。

常用开关：

- `audio=true`：显式打开音频转发；
- `keep_alive=false`：一次性手动启动，不走常驻；
- `keyboard=uhid`：使用物理键盘行为；
- `legacy_paste=true`：设备普通粘贴失败时启用旧粘贴路径；
- `ANDROID_USE_SCRCPY_RESIDENT=0`：关闭后台常驻监控；
- `ANDROID_USE_SCRCPY_ON_TOOL_CALL=0`：禁止 Android 工具调用时自动打开 scrcpy；
- `ANDROID_USE_SCRCPY_RESIDENT_INTERVAL_SEC`：调整监控间隔；
- `android_scrcpy_resident_status`：查看监控状态和最后一次启动结果。

后台常驻监控永远不会启动 WebRTC。只有确实想要多个常驻窗口时，才设置 `ANDROID_USE_SCRCPY_RESIDENT_SERIALS` 为逗号分隔的多个设备序列号。

## 高频流程复放

经常重复的 App 流程，优先用确定性 recipe，减少视觉模型等待：

1. 用 `android_start_recording` 开始录制。
2. 用 `android_tap_text`、`android_tap`、`android_swipe`、`android_type_text`、`android_open_app`、`android_press_key` 等确定性工具操作 App。
3. 用 `android_stop_recording` 停止录制，生成 `.android-use/recordings/<id>/trace.json`。
4. 用 `android_create_recipe` 转成 recipe。
5. 用 `android_replay_recipe` 复放。

recipe 会优先保存 selector 候选，包括 `resource-id`、content description、可见文本；selector 找不到时才退回缩放后的坐标。`android_record_checkpoint` 可以在手动 scrcpy 操作后记录页面指纹，但手动手势目前还不会自动转换成 recipe 动作。

如果用户提供 Android 源码，可以调用 `android_index_source` 建静态索引。它会扫描 Kotlin、Java、XML、TypeScript/JavaScript、Dart 文件中的 activity、route、resource id、label、content description 和 test tag，并写入 `.android-use/app-maps/app-map-*.json`。

## WebView 快路径

混合 App 的 WebView 如果能在 Chrome 的 `chrome://inspect/#devices` 中看到，优先走 WebView 工具，不要先走截图或视觉模型：

- `android_webview_pages`：转发 `webview_devtools_remote*` socket，并列出 title、URL、尺寸和 `webSocketDebuggerUrl`。
- `android_webview_eval`：通过 Chrome DevTools Protocol 在目标 WebView 中执行 JavaScript。
- `android_open_url`：识别小鹿爱学 App 专用 H5 URL，包括 `stu.xiaoluxue.com` 和 `*.xiaoluxue.cn`，并通过小鹿爱学学生端打开，而不是交给普通浏览器。
- `xiaoluxue_open_app_url`：通过 App vessel WebView route 打开小鹿爱学 H5 URL，等待匹配的运行时 URL，并可一次性注入 runtime bridge。
- `xiaoluxue_runtime_status`：复用缓存的 WebView DevTools 转发，校验运行时 URL，并安装 `window.__androidUse.xiaoluxue` 辅助方法，用于快照、显示隐藏层、跳转 widget、文本点击和设置播放速度。

小鹿爱学课程页工具：

- `xiaoluxue_course_snapshot`：读取 widget 和媒体状态；
- `xiaoluxue_set_speed`：设置播放速度；
- `xiaoluxue_goto_widget`：按 widget index、名称或 `last=true` 跳转；
- `xiaoluxue_course_fast_path`：常用一键流程，必要时打开知识讲解、设置 2x、默认跳到最后一个 widget；
- `xiaoluxue_open_knowledge_guide`：默认保留当前小鹿爱学 H5 host，测试环境下会继续停留在 `stu.test.xiaoluxue.cn`，不会回落到生产 host。

小鹿爱学原生地图页工具：

- `xiaoluxue_switch_env`：打开银河智学配置 App `com.xiaoluxue.ai.config`，选择学生端 `API 环境`，例如 `test`，提交后默认重开小鹿爱学；
- `xiaoluxue_open_native_subject`：通过 App 专用 `xlx://router/study/subject` 路由进入原生学科地图；
- `xiaoluxue_map_snapshot`：读取当前学科、章节、可见 index 和可见动作；
- `xiaoluxue_map_fast_path`：一键执行 `1.5 题型突破`、`错题`、`笔记本`、`学习任务`、`薄弱知识` 等操作。传入 `subject_id` 或 `subject` 时会先路由到对应学科；已知预设如 `语文 1.5 题型突破` 会避开慢速 `uiautomator dump`。

小鹿爱学 `/exercise` 页工具：

- `xiaoluxue_exercise_snapshot`：读取题干、选项、按钮和进度；
- `xiaoluxue_exercise_action`：执行一次语义点击；
- `xiaoluxue_exercise_fast_path`：适合 “选项 -> 提交 -> 继续” 这类流程。

`xiaoluxue_goto_widget` 默认使用 `mode=reload`，通过 `redirectWidgetIndex` 让 H5 课程状态直接初始化到目标 widget。只有为了快速视觉定位时才用 `mode=scroll`，因为它不会触发课程内部的 `goto` 状态机。

## 设计参考

插件借鉴了 Agent TARS 和 UI-TARS Desktop 的高层操作循环：观察 UI/截图、模型推理、输出移动端 `Thought`/`Action`、执行小步 adb 动作、再观察反馈。支持三种模式：

- `uiautomator`：只用文本/UI 树定位，速度最快，不需要视觉模型；
- `visual-grounding`：截图加模型动作预测，可选 OpenAI Responses computer tool、OpenAI 多模态 Responses，或 OpenAI-compatible chat completions；
- `hybrid`：先走 UIAutomator，失败再走视觉模型。

本项目不会内置 Agent TARS/UI-TARS 代码。
