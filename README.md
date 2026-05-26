# Android

`Android` 是一个 Codex 本地插件，用来在 Codex 里控制已连接的 Android 手机、平板或模拟器。

它可以做这些事：

- 打开 Android App；
- 查看和截图当前设备屏幕；
- 弹出 scrcpy 桌面镜像窗口，方便人工观察和接管；
- 点击、滑动、输入文字、按返回键、执行 adb shell；
- 调试 WebView；
- 录制常用操作并复放；
- 使用可选视觉模型做自然语言操作；
- 内置小鹿爱学 App 的课程、练习、WebView、原生地图等快路径。

## 一句话安装

如果你已经拿到了 `android-use-plugins.zip` 压缩包，可以直接把压缩包交给 Codex，然后说：

```text
请帮我解压 android-use-plugins.zip，进入解压后的目录，执行 ./install.sh 安装 Android 插件，然后运行 ./doctor.sh 检查环境。
```

安装完成后，重启 Codex，插件列表里应该直接显示 `Android`。

第一次使用的同学可以先看图文教程：[Android 插件图文上手教程](docs/android-use-tutorial.md)。

## 需要准备什么

电脑上需要：

- macOS；
- Codex 桌面端；
- Python 3，执行 `python3 --version` 能看到版本；
- Android 调试工具 `adb`；
- `scrcpy`，用于弹出安卓设备镜像窗口；
- 一根支持数据传输的 USB 线。

执行 `./install.sh` 时，脚本会默认自动安装缺失的 Python 3、adb 和 scrcpy。自动安装依赖需要电脑上已有 Homebrew。

安卓设备上需要：

- 已安装要调试的 App；
- 已开启开发者选项；
- 已开启 USB 调试；
- 用 USB 线连接电脑后，设备上点过「允许 USB 调试」。

## 安装电脑依赖

通常不需要手动安装依赖，直接执行安装脚本即可：

```bash
./install.sh
```

安装脚本会默认静默补齐缺失依赖：

- 缺少 Python 3 时，自动执行 `brew install python`；
- 缺少 `adb` 时，自动执行 `brew install --cask android-platform-tools`；
- 缺少 `scrcpy` 时，自动执行 `brew install scrcpy`；
- 安装日志写入 `/tmp/android-use-install-deps.log`，失败时才展示最近日志。

如果你想手动检查：

```bash
python3 --version
adb version
scrcpy --version
```

如果不希望安装脚本自动安装依赖，可以这样执行：

```bash
ANDROID_USE_AUTO_INSTALL_DEPS=0 ./install.sh
```

没有 Homebrew 时，可以直接让 Codex 先安装 Homebrew：

```text
请帮我安装 Homebrew，然后重新执行 Android 插件的 ./install.sh。
```

## 安卓设备怎么开启调试模式

不同 Android 品牌的菜单名称略有差异，下面是通用步骤。

1. 打开安卓设备「设置」。
2. 进入「关于手机」「关于平板」或「关于本机」。
3. 连续点击「版本号」「构建号」或「软件版本」7 次。
4. 如果要求输入锁屏密码，按提示输入。
5. 返回设置页，进入「系统和更新」「更多设置」或直接搜索「开发者选项」。
6. 打开「开发者选项」。
7. 开启「USB 调试」。
8. 如果设备有「USB 安装」「允许通过 USB 调试修改权限或模拟点击」「停用 adb 授权超时」等选项，可以按团队测试要求开启。
9. 用 USB 数据线连接电脑。
10. 设备弹出「是否允许 USB 调试」时，选择「允许」，建议勾选「始终允许使用这台计算机进行调试」。

在电脑上验证：

```bash
adb devices -l
```

正常会看到类似：

```text
List of devices attached
ANMB9X5A10G00857 device product:ELN-W09 model:ELN_W09
```

如果显示 `unauthorized`，说明设备还没有授权电脑。拔插 USB，重新看设备上的授权弹窗。

如果没有设备，优先检查 USB 线是不是数据线、设备是否开启 USB 调试、连接方式是否选择了文件传输。

## 只配对一次，后面不用数据线

Android 11 及以上设备可以使用「无线调试」。首次配对成功后，插件会保存设备地址，后续启动时自动无线重连。

第一次配对：

1. 确保电脑和平板在同一个 Wi-Fi。
2. 打开平板「设置」。
3. 进入「开发者选项」。
4. 打开「无线调试」。
5. 点「使用配对码配对设备」。
6. 把页面上的 IP、配对端口和配对码告诉 Codex，例如：

```text
[@Android] 无线配对 host=172.27.31.51 pair_port=42123 code=123456
```

插件会执行配对、自动连接，并把配置写到：

```text
~/.config/android-use/env
```

后续不用插数据线，直接让 Codex 重连：

```text
[@Android] 无线重连
```

如果平板 IP 经常变，建议在路由器里给平板做 DHCP 保留；否则插件会尽量通过 `adb mdns services` 自动发现新的连接端口。

## 没有 Git 怎么安装插件

小白用户不需要安装 Git。推荐用压缩包发给用户。

插件维护者在项目目录执行：

```bash
./package.sh
```

会生成：

```text
dist/android-use-plugins.zip
```

把这个压缩包发给用户。用户收到后，让 Codex 执行：

```text
请帮我解压 android-use-plugins.zip，进入解压后的目录，执行 ./install.sh 安装，然后执行 ./doctor.sh 检查。安装完成后提醒我重启 Codex，插件列表里应该直接显示 Android。
```

如果用户自己会用终端，也可以手动：

```bash
unzip android-use-plugins.zip
cd android-use
./install.sh
./doctor.sh
```

`install.sh` 会把插件复制到常规位置，并额外同步一份到兼容位置：

```text
~/plugins/android-use-plugins
~/.agents/plugins/android-use-plugins
~/.codex/plugins/cache/local/android-use-plugins/0.1.0
```

并更新两个 marketplace 文件：

```text
~/marketplace.json
~/.agents/plugins/marketplace.json
```

## 有 Git 的安装方式

开发同学也可以从 Git 仓库安装：

```bash
git clone https://gitlab.xiaoluxue.cn/shixiankang/android-use.git ~/plugins/android-use-plugins
cd ~/plugins/android-use-plugins
./install.sh
./doctor.sh
```

完成后重启 Codex，插件列表里应该直接显示 `Android`。

## 安装后怎么确认可用

先运行：

```bash
cd ~/plugins/android-use-plugins
./doctor.sh
```

再打开 Codex，试着问：

```text
[@Android] 列出设备
[@Android] 打开并截图
[@Android] 显示当前 Android 屏幕
[@Android] 生成当前 Android AppShot
```

正常情况下，插件会识别到 adb 设备，并弹出一个 scrcpy 桌面窗口。

## AppShot 证据快照

`android_appshot` 会一次性返回当前 Android 设备的截图、设备状态和 UIAutomator 控件树，适合给 Codex 做自动化测试、Bug 复现和验收证据。默认会把 PNG 和 JSON 保存到 `.screen/appshots/`，同时把截图作为工具结果返回给 Codex。

常用参数：

- `include_xml=true`：额外保存原始 UIAutomator XML；
- `include_image=false`：只返回 JSON，不在工具结果里附带图片；
- `save=false`：只返回本次结果，不写入 `.screen/appshots/`；
- `strict_ui=true`：UIAutomator 失败时直接报错。默认情况下即使控件树抓取失败，也会返回截图和设备状态。

## scrcpy 窗口说明

插件默认会通过原生 macOS `.app` wrapper 启动 scrcpy 桌面镜像窗口，保证 Codex Attach/AppShot 识别到稳定的 app 名称、bundle id 和 Android 图标。不要直接裸启动 scrcpy 可见窗口。

默认行为：

- Android 工具被调用时自动弹出；
- 同一个设备只保留一个 scrcpy 窗口；
- `ANDROID_USE_SCRCPY_RESIDENT_SERIALS` 写入多个序列号时，会为每台已连接设备分别保活一个 scrcpy 窗口；
- 不会自动启动 WebRTC；
- 自动弹窗、`android_start_scrcpy`、`android_start_scrcpy_app`、无线调试 `start_scrcpy=true`、resident monitor 都走同一个 `.app` wrapper 启动路径；
- 启动 `.app` wrapper 时会检查 `/Applications/Android Use.app`，已有就跳过，没有就自动创建一个系统「应用程序」里的 Android Use 启动图标；
- 默认启动时会清理 `.android-use/` 下旧的同 bundle id 设备专属 `.app`，只保留固定的 `Android Use.app`；
- 只复用 bundle id 为 `com.kolf.android-use` 的窗口；发现旧的裸 scrcpy/supervisor 窗口会先关闭再重开；
- 窗口稳定显示后，如果用户手动关闭，插件会尽量尊重这次关闭；
- 下一次调用 Android 插件工具时，再重新弹出 `.app` wrapper 窗口；
- 默认关闭音频，降低资源占用；
- 默认启用文字输入优化。

`.app` wrapper 的 bundle id 是 `com.kolf.android-use`，并使用 Android 图标与 software renderer 打开 scrcpy。macOS app 固定使用 `Android Use.app` 作为启动器，避免换设备时多个同 bundle id 的设备专属 `.app` 被 LaunchServices 缓存混淆；窗口标题仍默认使用设备名称，例如 `荣耀平板Z6`，取不到设备名称时使用型号，最后回退到 `Android`。默认初始窗口大小是当前设备截图尺寸的 1/2，例如横屏 `2000 x 1200` 会以 `1000 x 600` 打开；这只影响启动窗口大小，不降低 scrcpy 视频流分辨率，也不会持续锁定窗口。

如果不想自动创建系统「应用程序」里的 `Android Use.app`，可设置：

```bash
export ANDROID_USE_SYSTEM_ANDROID_APP=0
```

如果不想让工具调用时自动弹出 scrcpy：

```bash
export ANDROID_USE_SCRCPY_ON_TOOL_CALL=0
```

如果完全关闭后台常驻监控：

```bash
export ANDROID_USE_SCRCPY_RESIDENT=0
```

## 可选：配置视觉模型

没有视觉模型也能用 adb、截图、scrcpy、UIAutomator、WebView 和小鹿爱学快路径。

只有需要自然语言看图操作时，才需要配置视觉模型。

推荐把配置写到：

```text
~/.config/android-use/env
```

示例：

```bash
ANDROID_USE_AGENT_PROVIDER=openai-compatible
ANDROID_USE_VLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
ANDROID_USE_VLM_MODEL=doubao-seed-1-6-vision-250815
ANDROID_USE_VLM_API_KEY=你的_api_key
ANDROID_USE_VLM_COORDINATE_MODE=normalized_1000
```

写完后重启 Codex。

也可以使用 OpenAI 原生模型：

```bash
OPENAI_API_KEY=你的_openai_key
ANDROID_USE_AGENT_PROVIDER=openai-computer
ANDROID_USE_OPENAI_COMPUTER_MODEL=gpt-5.5
```

## 输入速度说明

`android_type_text` 会自动选择更快的输入方式：

- 可调试 WebView 页面会优先直接给当前输入框赋值，不走键盘输入，适合小鹿爱学答题输入框；
- 如果设备装了 ADB Keyboard，中文、长文本、清空后输入会优先走 IME 广播；
- 普通短英文会走一次性批量 `adb shell input`；
- 录制 recipe 回放里的输入也会复用同一套快路径。

如果不希望插件直接写 WebView DOM：

```bash
ANDROID_USE_WEBVIEW_DIRECT_INPUT=0
```

如果不希望插件自动切换输入法，可以在环境变量里关闭：

```bash
ANDROID_USE_FAST_INPUT_IME=0
```

如果希望每次输入后恢复原输入法：

```bash
ANDROID_USE_RESTORE_IME_AFTER_TYPE=1
```

## 常用功能

在 Codex 对话中可以这样说：

```text
[@Android] 打开并截图
[@Android] 显示当前 Android 屏幕
[@Android] 点击“登录”
[@Android] 向上滑动
[@Android] 输入 123456
[@Android] 按返回键
```

小鹿爱学常用：

```text
[@Android] 把小鹿爱学环境切到 test
[@Android] 打开小鹿爱学首页
[@Android] 进入语文 1.5 题型突破
[@Android] 打开数学 1.1.11 知识讲解，并调速到 2x
[@Android] 小鹿爱学 /exercise 选择 A 并提交
```

## 小鹿爱学 App 注意事项

- `stu.xiaoluxue.com` 和 `*.xiaoluxue.cn` 这类链接不要用普通浏览器打开；
- 这类链接应该通过小鹿爱学 App 内部 route 或 WebView vessel 打开；
- `/course` 页面点击屏幕任意位置可能会显示隐藏控制层；
- WebView 能在 Chrome `chrome://inspect/#devices` 里看到时，优先用插件的 WebView 工具，而不是视觉模型；
- 原生地图页优先用 `xiaoluxue_open_native_subject`、`xiaoluxue_map_fast_path`、`xiaoluxue_map_snapshot`；
- 课程页优先用 `xiaoluxue_course_fast_path`；
- `/exercise` 页优先用 `xiaoluxue_exercise_fast_path`，输入答案时传 `answer_text`，插件会直接写入 WebView 输入框。

## 录制和复放常用流程

重复操作建议录制成 recipe，后续会比视觉模型更快。

流程：

1. 用 `android_start_recording` 开始录制。
2. 用 `android_tap_text`、`android_tap`、`android_swipe`、`android_type_text` 等工具操作。
3. 用 `android_stop_recording` 停止录制。
4. 用 `android_create_recipe` 生成 recipe。
5. 用 `android_replay_recipe` 复放。

recipe 会优先使用 selector，找不到时才退回坐标。

## 视频录制

如果你对 Codex 说“开始录制视频”或“开始录屏”，插件应立即调用 `android_start_video_recording`，通过 scrcpy 后台录制当前安卓屏幕为 MP4，不先截图、不先分析页面。

如果你说“停止录制视频”或“停止录屏”，插件应立即调用 `android_stop_video_recording`，停止当前录制进程，并把返回的本地 MP4 路径作为视频发回给你。

默认输出目录：

```text
.screen/video-recordings/
```

这是真实视频录制，和上面的 recipe 录制不同：`android_start_recording` 记录的是可复放操作 trace，不会生成 MP4。

## 常见问题

### 插件列表看不到 Android

先确认：

```bash
cat ~/marketplace.json
cat ~/.agents/plugins/marketplace.json
```

其中至少一个文件里应该有：

```text
android-use-plugins
```

再确认 Codex 配置里已经启用：

```bash
grep -n 'android-use-plugins@local' ~/.codex/config.toml
```

如果没有输出，重新执行：

```bash
cd ~/plugins/android-use-plugins
./install.sh
./doctor.sh
```

然后重启 Codex，插件列表里应该直接显示 `Android`。

### adb 找不到设备

运行：

```bash
adb devices -l
```

如果没有设备，检查 USB 线、USB 调试、设备授权弹窗。

如果是 `unauthorized`，重新插拔 USB，并在设备上点允许。

### scrcpy 没窗口

运行：

```bash
scrcpy --version
cd ~/plugins/android-use-plugins
./doctor.sh
```

如果刚刚手动关闭过窗口，下一次调用 Android 工具时会重新弹出。

### 多台设备怎么办

如果只想控制某一台设备，可以设置：

```bash
export ANDROID_SERIAL=设备序列号
```

或：

```bash
export ANDROID_USE_SERIAL=设备序列号
```

如果需要多台设备同时投屏，可以写入逗号分隔的序列号：

```bash
export ANDROID_USE_SCRCPY_RESIDENT_SERIALS=设备1序列号,设备2序列号
```

也可以直接调用 `android_start_scrcpy`，传入 `serials` 列表，让每台设备各开一个 `.app` wrapper scrcpy 窗口。

### 可以不用 USB 线吗

可以用 adb wireless，但第一次配置通常还是需要 USB 线。小白用户建议先用 USB 跑通，稳定后再让 Codex 帮你配置无线调试。

一台设备配对成功后，`android_wireless_pair` 会把它追加到 `~/.config/android-use/env` 的 `ANDROID_USE_WIRELESS_DEVICES` 和 `ANDROID_USE_SCRCPY_RESIDENT_SERIALS`。多台设备分别配对后，可以调用：

```text
android_wireless_reconnect(all=true, start_scrcpy=true)
```

这样会批量重连已保存的无线设备，并为每台设备启动一个 `.app` wrapper scrcpy 投屏窗口。

### WebRTC 要不要开

默认不要开。日常使用优先看 scrcpy 桌面窗口。只有明确需要在 Codex 内嵌页面看视频流时，再调用 `android_start_webrtc_viewer`。

## 项目边界

这个插件提供 Android 通用控制能力，也包含小鹿爱学专用快路径。模型使用说明在 `skills/android-use/SKILL.md`，该文件保持英文，方便 Codex 正确调用工具。
