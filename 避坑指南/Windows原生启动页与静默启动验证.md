# Windows 原生启动页与静默启动验证

- 表现：把共用桌面脚本加载到 Tauri 启动页后，工作台就绪指标出现很早的时间及 `web_timings: null`，容易误判工作台已加载。
- 根因：Windows Tauri 原生资源页可能为 `http://tauri.localhost`，仅用 `location.protocol === 'http:'` 无法区分本地资源启动页与引擎工作台。
- 解决：关闭交互共用，但工作台就绪上报明确限定 `location.hostname === '127.0.0.1'`。静默启动使用创建窗口时的 `.visible(false)`，不能等前端加载后再隐藏；普通重复启动调用同一恢复窗口函数，自动启动参数不唤醒已有窗口。
- 验证：`build/close-startup/native-result.json` 与 `installed-native-result.json` 均覆盖实际发行/安装桌面静默隐藏、重复自动启动、手动恢复及关闭行为；就绪指标包含真实 `web_timings`。原生截图辅助失败时，清楚区分 Windows 窗口/进程集成验证与浏览器主题截图，不把浏览器当成原生截图。
