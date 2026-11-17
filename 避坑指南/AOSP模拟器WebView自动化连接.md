# AOSP 模拟器 WebView 自动化连接

## 表现与根因

本机 AOSP Android 14 模拟器使用 WebView 113。新版本机 Playwright 通过 CDP 连接时，默认初始化会调用该运行时不支持的浏览器上下文下载管理接口，返回 `Browser context management is not supported`，但 APP 本身可正常显示。

## 已验证处理

当前本机 Playwright 支持 `connect_over_cdp(url, no_defaults=True)`，关闭默认上下文配置后可以直接复用 WebView 页面、发出真实触控并读取业务回调。先用 Python `inspect.signature` 确认所用版本支持该参数。

连接、安装及 ADB 命令明确指定 `emulator-5556`，避免用户手机重连后误选设备。不要为解决自动化连接去降低 APP 的 CSP、打开正式包调试或改动用户手机。

## 验证与限制

`FramekeepMinApi34`（1536MB RAM、2 核、480×854、WHPX）已实测相机实际保存两张 JPEG、点击去重、原生闪白及离线语音播放。正式包在该旧 WebView 下的 UIAutomator 树可能只有 WebView 外壳；用独立验证包检查数据，再用正式包截图和录屏核对实际效果，不能将空树直接判断为 APP 白屏。

原生闪白验证应取不含动态相机画面的固定工具栏区域比较。首次取景区包含本来就很亮的模拟画面，亮度差不能代表是否闪屏；改为固定区域后亮度从 117 升至 230，正式包从 11 升至 230。
