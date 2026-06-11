# Android 插件图文上手教程

这份教程给第一次使用 Android 插件的同学看。你只需要按步骤准备电脑和安卓设备，不需要理解插件内部实现。

![Android 插件快速上手](./tutorial-assets/android-use-onboarding.png)

## 1. 先拿到安装包

从维护者那里拿到 `android-use-plugins.zip`。

拿到后，把压缩包发给 Codex，然后说：

```text
请帮我解压 android-use-plugins.zip，安装 Android 插件，然后检查环境。
```

安装完成后，退出并重新打开 Codex，插件列表里应该能看到 `Android`。

## 2. 设置安卓设备

在安卓设备上按这个顺序操作：

1. 打开「设置」。
2. 进入「关于手机」「关于平板」或「关于本机」。
3. 连续点击「版本号」「构建号」或「软件版本」7 次。
4. 返回设置页，进入「开发者选项」。
5. 打开「USB 调试」。
6. 用 USB 数据线连接电脑。
7. 设备弹出授权提示时，选择「允许」。
8. 建议勾选「始终允许使用这台计算机进行调试」。

如果连接不上，重新插拔数据线，再看设备上是否有授权弹窗。

## 3. 在 Codex 里使用

常用说法：

```text
[@Android] 列出设备
[@Android] 打开设置
[@Android] 截图
[@Android] 点击“登录”
[@Android] 输入 123456
[@Android] 按返回键
```

每次让插件进入页面后，建议再说一句：

```text
[@Android] 截图
```

这样可以确认当前页面是不是已经进入成功。

## 4. 后面不想一直插数据线

Android 11 及以上设备支持无线调试。第一次配对时，需要电脑和平板在同一个 Wi-Fi 下。

插件通过 adb 的 pairing/connect 和 mDNS 服务完成 Android 11+ 无线调试二维码或配对码流程。

配对时，在安卓设备里打开：

```text
设置 -> 开发者选项 -> 无线调试
```

然后让 Codex 创建二维码：

```text
[@Android] 创建无线配对二维码
```

设备扫码后，再让 Codex 完成配对：

```text
[@Android] 完成无线配对
```

以后可以直接说：

```text
[@Android] 无线重连
```

## 5. 常见问题

### 插件列表没有 Android

重新执行安装脚本，然后重启 Codex：

```bash
cd ~/plugins/android-use-plugins
./install.sh
./doctor.sh
```

### 设备没有反应

先确认：

- 数据线能传数据，不只是充电线；
- 设备已经打开 USB 调试；
- 设备弹窗里已经点了「允许」；
- 如果使用无线连接，电脑和平板在同一个网络。

### scrcpy 没有窗口

如果不需要实时镜像，让 Codex 调用 `android_start_screen_viewer` 可以用截图时间线展示 Android Use 的操作步骤；需要实时镜像时调用 `android_start_scrcpy`。
