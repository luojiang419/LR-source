# Canon 自动登记与联机照片接入

[CODEX_LONG_TASK_CONTINUE_V3]

- 当前状态：实现、50 项验证、发行部署、本机自动登记和新版桌面启动全部完成。
- 下一步：用户可直接用 EOS 传图，末帧自动识别照片目录。
- TODO：无功能实现遗留。实机长时间连拍不在已完成的软件通知验收范围内。
- 最近验证：44 项 Python／浏览器回归、5 项 Rust 测试及 1 项发行桌面集成通过；本机 .NET 读取确认自动登记成功，41 项其他首选项不变；桌面 PID 58192、引擎 PID 2860，首屏 1859ms、健康 busy=false。
- branch/commit：开发期间共享工作区曾切换到导航灯分支，现以保留其提交的 `feat/canon-eos-integration` 交付；交付提交标题 `feat: automatically register Canon EOS and receive tethered photos`，精确提交见 Git；2026-10-22 当日同步已完成，本轮仅本地提交，未推送。
- 阻塞：无。

## 目标与验收

- 新电脑启动末帧时检测已安装的 EOS Utility，登记当前实际主程序路径，支持不同安装盘符、版本、用户目录与无既有用户配置。
- 保留 Canon 其他首选项与原链接设置的可恢复记录，不修改 Canon 二进制、系统注册表或相机控制；不关闭运行中的 EOS。
- EOS 每张图片的完整路径通知可在首次启动和已运行实例中接收，自动建立/切换照片目录，无须手动选择监控文件夹；同目录不重置监控，RAW/JPG 合并、评级和缓存保持。
- 连续拍摄不打开重复窗口、不逐张抢焦点；启动尚未就绪时保留待接入通知。
- EOS 已运行导致设置缓存时，明确展示生效状态，并尽可能读取其保存目录自动接入；不声称磁盘配置写入能立即改变运行中的 EOS 内存设置。
- 默认自动接入，可在现场监控面板关闭并恢复原链接设置。使用隔离配置、照片及数据库验证，打包运行后核对实际自动登记与接口；实机未测的部分如实标注。

## 交付与验证

实现及使用方法见 [Canon自动联机接入](../技术文档/Canon自动联机接入.md)。主要文件为 `canon_link.py`、`canon_probe.ps1`、Rust 主程序、监控前后端及发行脚本；沿用既有目录稳定检查、RAW/JPG 配对、评级和预览缓存。

最终 Python 回归：`test_canon_link test_canon_browser test_folder_watch test_engine_startup test_monitor_latency test_live_layout_browser test_watch_plan_browser test_preview_cache`，44 项通过（36.943 秒）；Rust `cargo test --locked` 5 项通过；以最终发行桌面指定 `FRAMEKEEP_TEST_DESKTOP` 的 `test_canon_desktop` 通过（3.764 秒）。

实际自动登记前保留原配置和字段快照；登记后 .NET 读取成功，非链接首选项值比较为 41 项一致。旧版通过正常关闭保存流程退出，没有强制终止用户进程，也未关闭 EOS 相机软件。新版部署至 `dist/末帧选片` 并保持运行，发行校验通过，版本不递增。

已修复目标 .NET 框架读取紧凑 XML 的兼容问题，经验见 [避坑指南](../避坑指南/EOS-Utility用户配置与.NET读取兼容.md)。截图／日志／结构化结果保留在忽略目录 `build/canon-link-acceptance/`。
