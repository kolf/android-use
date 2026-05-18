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

安装完成后，重启 Codex，在插件列表里启用 `Android`。

## 需要准备什么

电脑上需要：

- macOS；
- Codex 桌面端；
- Python 3，执行 `python3 --version` 能看到版本；
- Android 调试工具 `adb`；
- 可选：`scrcpy`，用于弹出安卓设备镜像窗口；
- 一根支持数据传输的 USB 线。

安卓设备上需要：

- 已安装要调试的 App；
- 已开启开发者选项；
- 已开启 USB 调试；
- 用 USB 线连接电脑后，设备上点过「允许 USB 调试」。

## 安装电脑依赖

先检查电脑有没有 `adb`：

```bash
adb version
```

如果提示找不到命令，可以安装 Android Platform Tools：

```bash
brew install --cask android-platform-tools
```

再检查有没有 `scrcpy`：

```bash
scrcpy --version
```

如果提示找不到命令，可以安装：

```bash
brew install scrcpy
```

如果你不会安装 Homebrew 或不想碰命令行，可以直接让 Codex 做：

```text
请帮我安装 Android Platform Tools 和 scrcpy，并验证 adb version、scrcpy --version 都能执行。
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
请帮我解压 android-use-plugins.zip，进入解压后的目录，执行 ./install.sh 安装，然后执行 ./doctor.sh 检查。安装完成后提醒我重启 Codex 并启用 Android 插件。
```

如果用户自己会用终端，也可以手动：

```bash
unzip android-use-plugins.zip
cd android-use
./install.sh
./doctor.sh
```

`install.sh` 会把插件复制到：

```text
~/plugins/android-use-plugins
```

并更新：

```text
~/marketplace.json
```

## 有 Git 的安装方式

开发同学也可以从 Git 仓库安装：

```bash
git clone https://gitlab.xiaoluxue.cn/shixiankang/android-use.git ~/plugins/android-use-plugins
cd ~/plugins/android-use-plugins
./install.sh
./doctor.sh
```

完成后重启 Codex，在插件列表里启用 `Android`。

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
```

正常情况下，插件会识别到 adb 设备，并弹出一个 scrcpy 桌面窗口。

## scrcpy 窗口说明

插件默认会使用 scrcpy 打开一个桌面镜像窗口。

默认行为：

- Android 工具被调用时自动弹出；
- 同一个设备只保留一个 scrcpy 窗口；
- 不会自动启动 WebRTC；
- 窗口稳定显示后，如果用户手动关闭，插件会尊重这次关闭；
- 下一次调用 Android 插件工具时，再重新弹出窗口；
- 默认关闭音频，降低资源占用；
- 默认启用文字输入优化。

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
- `/exercise` 页优先用 `xiaoluxue_exercise_fast_path`。

## 录制和复放常用流程

重复操作建议录制成 recipe，后续会比视觉模型更快。

流程：

1. 用 `android_start_recording` 开始录制。
2. 用 `android_tap_text`、`android_tap`、`android_swipe`、`android_type_text` 等工具操作。
3. 用 `android_stop_recording` 停止录制。
4. 用 `android_create_recipe` 生成 recipe。
5. 用 `android_replay_recipe` 复放。

recipe 会优先使用 selector，找不到时才退回坐标。

## 常见问题

### 插件列表看不到 Android

先确认：

```bash
cat ~/marketplace.json
```

里面应该有：

```text
android-use-plugins
```

然后重启 Codex，再去插件列表启用 `Android`。

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

### 可以不用 USB 线吗

可以用 adb wireless，但第一次配置通常还是需要 USB 线。小白用户建议先用 USB 跑通，稳定后再让 Codex 帮你配置无线调试。

### WebRTC 要不要开

默认不要开。日常使用优先看 scrcpy 桌面窗口。只有明确需要在 Codex 内嵌页面看视频流时，再调用 `android_start_webrtc_viewer`。

## 项目边界

这个插件提供 Android 通用控制能力，也包含小鹿爱学专用快路径。模型使用说明在 `skills/android-use/SKILL.md`，该文件保持英文，方便 Codex 正确调用工具。
