# Android Use Plugins 团队安装说明

`android-use-plugins` 是小鹿内部 Codex Android 控制插件，提供 adb、scrcpy、截图、WebView 调试、小鹿爱学快路径等能力。

## 安装

```bash
git clone https://gitlab.xiaoluxue.cn/shixiankang/android-use.git ~/.agents/plugins/android-use-plugins
cd ~/.agents/plugins/android-use-plugins
./install.sh
```

安装脚本会写入或更新：

```text
~/.agents/plugins/marketplace.json
```

完成后重启 Codex，在插件列表里启用 `Android Use Plugins`。

## 依赖

macOS 推荐：

```bash
brew install scrcpy
brew install --cask android-platform-tools
```

然后连接设备并授权：

```bash
adb devices -l
```

## 自检

```bash
cd ~/.agents/plugins/android-use-plugins
./doctor.sh
```

自检会检查 Python、adb、scrcpy、插件 manifest、MCP 配置、单元测试和 marketplace 条目。

## 使用

在 Codex 对话中：

```text
[@android-use] 打开并截图
[@android-use] 小鹿退出到首页
[@android-use] 进入语文 1.5 题型突破
```

小鹿爱学 H5 链接不要用浏览器打开，插件会通过小鹿爱学 App 内部 route 或 WebView vessel 打开。

## 常见问题

- 插件列表看不到：确认 `~/.agents/plugins/marketplace.json` 里有 `android-use-plugins`，然后重启 Codex。
- 设备不可控：先跑 `adb devices -l`，确认设备状态是 `device`。
- scrcpy 没窗口：跑 `scrcpy --version` 和 `./doctor.sh`，确认依赖已安装。
- 多设备场景：默认只给一个物理设备保留常驻 scrcpy 窗口。需要指定设备时设置：

```bash
export ANDROID_USE_SCRCPY_RESIDENT_SERIALS=设备序列号
```
