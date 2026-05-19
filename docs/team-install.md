# Android 插件小白安装说明

`android-use-plugins` 是小鹿内部 Codex Android 控制插件。安装后，Codex 可以通过 adb、scrcpy、截图、WebView 调试和小鹿爱学快路径控制安卓设备。

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

安装完成后，重启 Codex，在插件列表启用 `Android`。

## 电脑需要配置什么

必须：

- macOS；
- Codex 桌面端；
- Python 3；
- Android Platform Tools，也就是 `adb`；
- `scrcpy`，用于显示安卓设备镜像窗口；
- 一根能传数据的 USB 线。

检查命令：

```bash
python3 --version
adb version
scrcpy --version
```

缺少 `adb` 或 `scrcpy` 时，可以安装：

```bash
brew install --cask android-platform-tools
brew install scrcpy
```

不会安装时，直接让 Codex 做：

```text
请帮我安装 Android Platform Tools 和 scrcpy，并验证 adb version、scrcpy --version。
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
adb devices -l
```

看到 `device` 就是授权成功。

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
```

并更新两个 marketplace 文件：

```text
~/marketplace.json
~/.agents/plugins/marketplace.json
```

## 有 Git 的安装方式

开发同学可以使用：

```bash
git clone https://gitlab.xiaoluxue.cn/shixiankang/android-use.git ~/plugins/android-use-plugins
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
[@Android] 进入语文 1.5 题型突破
```

默认会弹出一个 scrcpy 桌面窗口，方便人工观察和接管。插件默认只保留一个窗口，不会自动启动 WebRTC。

## 常见问题

### 插件列表没有 Android

确认：

```bash
cat ~/marketplace.json
cat ~/.agents/plugins/marketplace.json
```

其中至少一个文件里应该包含 `android-use-plugins`。确认后重启 Codex。

### 设备不可控

先看：

```bash
adb devices -l
```

设备状态必须是 `device`。

### scrcpy 没窗口

检查：

```bash
scrcpy --version
cd ~/plugins/android-use-plugins
./doctor.sh
```

如果之前手动关过窗口，下一次调用 Android 工具会重新打开。

### 小鹿爱学链接打不开

`stu.xiaoluxue.com` 和 `*.xiaoluxue.cn` 不要用普通浏览器打开。插件会通过小鹿爱学 App 内部 route 或 WebView 打开。

### 视觉模型必须配置吗

不是必须。adb、scrcpy、截图、UIAutomator、WebView 和小鹿爱学快路径都可以不依赖视觉模型。

只有需要自然语言看图操作时，才配置视觉模型。配置建议写到：

```text
~/.config/android-use/env
```
