# Android 插件小白安装说明

`android-use-plugins` 是 Codex Android 通用控制插件。安装后，Codex 默认通过 adb、截图、UIAutomator 和 Playwright Android WebView 控制安卓设备；对轻原生壳、主要内容在 WebView 里的 App，会优先尝试 Playwright WebView 快路径，再回退到 UIAutomator/adb。

这份文档面向不熟悉命令行的同学。

## 最推荐的安装方式：压缩包

维护者先在插件项目目录执行：

```bash
./package.sh
```

会生成：

```text
dist/android-use-plugins.zip
```

把这个压缩包发给用户。用户不需要 Git。

用户收到压缩包后，可以直接对 Codex 说：

```text
请帮我解压 android-use-plugins.zip，进入解压后的目录，执行 ./install.sh 安装 Android 插件，然后执行 ./doctor.sh 做环境检查。
```

安装完成后，重启 Codex，插件列表里应该直接显示 `Android`。

## 电脑需要配置什么

必须：

- macOS；
- Codex 桌面端；
- Python 3；
- Android platform-tools / `adb`，用于设备传输；
- Node.js 和 npm，用于安装 Playwright Android WebView 运行依赖；
- `scrcpy` 是可选依赖，用于镜像窗口和 MP4 录屏；
- 一根能传数据的 USB 线。

执行 `./install.sh` 时，脚本会默认自动安装缺失的 Python 3、Android platform-tools、Node.js/npm、Playwright 运行依赖，并可选补装 scrcpy。自动安装依赖需要电脑上已有 Homebrew。

检查命令：

```bash
python3 --version
./doctor.sh
```

通常不需要手动安装依赖，直接执行：

```bash
./install.sh
```

安装脚本会默认静默补齐缺失依赖：

- 缺少 Python 3 时，自动执行 `brew install python`；
- 缺少 `adb` 时，自动执行 `brew install android-platform-tools`；
- 缺少 Node.js/npm 时，自动执行 `brew install node`；
- 缺少 Playwright Android 运行依赖时，在插件目录执行 `npm install --omit=dev`；
- 缺少 `scrcpy` 时，可选执行 `brew install scrcpy`；
- 安装日志写入 `/tmp/android-use-install-deps.log`，失败时才展示最近日志。

如果不希望安装脚本自动安装依赖，可以这样执行：

```bash
ANDROID_USE_AUTO_INSTALL_DEPS=0 ./install.sh
```

没有 Homebrew 时，可以直接让 Codex 先安装 Homebrew：

```text
请帮我安装 Homebrew，然后重新执行 Android 插件的 ./install.sh。
```

## 安卓设备怎么设置

1. 打开安卓设备「设置」。
2. 进入「关于手机」「关于平板」或「关于本机」。
3. 连续点击「版本号」「构建号」或「软件版本」7 次，打开开发者模式。
4. 返回设置，进入「开发者选项」。
5. 开启「USB 调试」。
6. 用 USB 数据线连接电脑。
7. 设备弹窗询问「是否允许 USB 调试」时，选择「允许」。
8. 建议勾选「始终允许使用这台计算机进行调试」。

电脑上验证：

```bash
./doctor.sh
```

依赖检查通过后，再在 Codex 里调用 `android_list_devices` 看设备是否为 `device`。

如果看到 `unauthorized`，说明设备还没有点允许；重新插拔 USB，看设备弹窗。

如果完全看不到设备，检查 USB 线是否支持数据传输、设备是否选择文件传输模式、USB 调试是否开启。

## 只配对一次，后面不用数据线

Android 11 及以上设备可以开启「无线调试」。首次配对成功后，插件会保存设备地址，后面会自动无线重连。

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

后续不用插数据线，直接说：

```text
[@Android] 无线重连
```

如果 IP 经常变，建议让网络管理员在路由器上给平板做 DHCP 保留。

## 手动安装步骤

如果用户自己会打开终端：

```bash
unzip android-use-plugins.zip
cd android-use
./install.sh
./doctor.sh
```

安装脚本会把插件安装到常规位置，并额外同步一份到兼容位置：

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

开发同学可以使用：

```bash
git clone <你的仓库地址> ~/plugins/android-use-plugins
cd ~/plugins/android-use-plugins
./install.sh
./doctor.sh
```

## 安装后怎么用

在 Codex 对话中：

```text
[@Android] 列出设备
[@Android] 打开并截图
[@Android] 显示当前 Android 屏幕
[@Android] 点击“登录”
```

需要在 Codex 里查看 Android Use 操作步骤证据时，可以使用 `android_start_screen_viewer` 打开截图时间线 Web UI；需要实时镜像时使用 `android_start_scrcpy`。

## 输入速度

可调试 WebView 能被 Playwright Android 发现时，插件默认优先走 WebView DOM：`android_observe` 先取 DOM 快照，`android_tap_text` 先做 DOM 点击，`android_type_text` 先直接给当前输入框赋值。找不到可用 WebView 或 DOM 元素时，再回退到 UIAutomator、输入法或 adb。

WebView 调试默认走常驻 Playwright worker，同一个 MCP server 生命周期内会尽量复用设备、页面和 CDP session，不需要额外安装或启动 CDP MCP。需要检查或清理 worker 状态时使用 `android_webview_runtime`；只有排查 one-shot 兼容路径时才需要关闭：

```bash
ANDROID_USE_PLAYWRIGHT_WEBVIEW_WORKER=0
```

插件会自动选择更快的输入方式：

- 可调试 WebView 页面：优先通过 Playwright Android 直接给当前输入框赋值，不走键盘输入，适合混合 App、表单页和富文本输入框；
- 中文、长文本、清空后输入：优先使用 ADB Keyboard 广播；
- 短英文：使用 adb 批量 `shell input`；
- 录制回放里的输入：也使用同一套快路径。

如果不希望插件直接写 WebView DOM，可以在环境变量里写：

```bash
ANDROID_USE_WEBVIEW_DIRECT_INPUT=0
```

如果不希望 observe、点击文字和 hybrid agent 默认优先尝试 WebView：

```bash
ANDROID_USE_WEBVIEW_FIRST=0
```

如果想调整 WebView 快路径的超时时间：

```bash
ANDROID_USE_WEBVIEW_FAST_TIMEOUT=3
```

如果不希望插件自动切换输入法，可以在环境变量里写：

```bash
ANDROID_USE_FAST_INPUT_IME=0
```

## 常见问题

### 插件列表没有 Android

确认：

```bash
cat ~/marketplace.json
cat ~/.agents/plugins/marketplace.json
```

其中至少一个文件里应该包含 `android-use-plugins`。

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

### 设备不可控

先看：

```bash
./doctor.sh
```

检查 adb 和 Playwright Android 运行依赖是否通过，并确认设备已开启 USB 调试和授权。

### scrcpy 没窗口

如果没有看到 scrcpy 窗口，需要可视证据时也可以使用：

```bash
android_start_screen_viewer
```

`android_start_scrcpy` 会通过 adb-backed scrcpy 启动或复用窗口。

### 视觉模型必须配置吗

不是必须。adb、截图、UIAutomator、Playwright WebView 和直接控制工具都可以不依赖视觉模型。

只有需要自然语言看图操作时，才配置视觉模型。配置建议写到：

```text
~/.config/android-use/env
```
