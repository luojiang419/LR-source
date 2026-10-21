# Canon DPP 监控预览机制与项目优化

2026-10-20；目标是用户指定的 `C:\Program Files\Canon\Digital Photo Professional 4`。只读分析，无注入、修改安装文件或复制 Canon DLL。REA 6.5.0、ILSpy 9.1 与 Ghidra 12.1.4 的恢复结果用于理解机制，不是 Canon 原始源码。

## 已取得的证据

| 机制 | 证据与边界 |
| --- | --- |
| Windows 目录事件 | Ghidra 实际反编译 `DppMWare.dll!0x180062270`，出现 `FindFirstChangeNotificationW`；文件过滤掩码为 `0x19`（文件名、大小、最后写入），第二个目录掩码为 `0x2`（目录名），均不递归。另有已核对 IAT 的 `FindNextChangeNotification` 与 `WaitForMultipleObjects` 调用。 |
| 增量通知 | `DSFImageListManager.ThumbnailProcessEvent` 构造新增图片集合，经 `NotifyAddThumbnailList`、`Dispatcher.BeginInvoke` 更新 UI。 |
| 异步预览 | `DSFPreviewProcess.CreatePreviewImage` 调用 `DppMWProcessPreviewImageEx`，回调经过工作队列、快照有效性验证与 `EventPreviewReadyImage`；UI 提交前再次核对当前 thumbnailID。 |
| 分级与缓存 | 管理层支持 Half / Low / Normal / High 画质，存在 `DppMWEnablePreviewImageCache` 和缩略图 generation 校验。不能据此断言每张照片都按固定顺序逐级输出。 |
| 控制工作量 | 缩略图可设置可见范围；预览计算目标视口、Fit 与缩放区域。 |

原生函数 REA Evidence：`ev_066c9be325e4d0f9ad218d0979115ddee3eed21f991a953e4fb7df870868ef0e`，操作 `procedure_pseudo_code`，Ghidra 12.1.4，首次冷分析 122441ms。

管理层完整成员 Evidence：`ev_1c31e3cebe471b8cc88c73dcbe493b49b77e5cf1068620fa238ed5c7a83e440b`；P/Invoke 边界 Evidence：`ev_c6a05e685b9efdfe486523546dc9355b8018eca9f2660c79ca85ed0bc5a43041`。

目标身份：

- `Dpp4Main.exe`：.NET Framework 4.7.1、版本 4.21.30.0，SHA-256 `925be8362b11ef40a73f136b97041e4d54a94f68d8c12d27496990b4b43b3bf6`。
- `DppMWare.dll`：x86-64 PE，SHA-256 `a8a6faf24e5f2ce983972d346a1f9fb87c9284106c6cd834eb99ed63aea8517f`。
- `DapServiceFramework.dll`：SHA-256 `1740fc11e251f638fa31338eb6509ad69ac46dcebed40c5300ff595ea365b030`。

完整 REA JSON、反编译辅助结果与证据索引留在本机忽略目录 `build/dpp-analysis/`，不将 Canon 恢复代码纳入项目源码。Dpp4Main 全成员解析触及 REA 64 MiB 表示预算，已保留 unavailable 结果；较小的服务程序集解析成功。没有测量 DPP 从相机拍摄到实际显示的时间，也没有证实所有 RAW 都先用内嵌 JPEG、所有模式完全无轮询。

## 本项目独立实现

- `folder_watch.py`：采用同类 Windows 目录通知，空闲 2 秒补偿重扫；待处理文件约 80ms 重试，完整性、写句柄和元数据读取前后指纹检查保留。缓存已知记录，避免每次等待重扫都从数据库取全部照片。
- `media_engine.py`：监控持有常驻 ExifTool，完整保留身份、子秒、方向与原有评级字段。每个请求有独立完成编号，取消、超时、关闭均释放进程及读取线程，下次可恢复。
- `server.py` / `folder-watch.js`：使用可取消的长轮询，入库立即唤醒；关闭对话框、退出交互暂停、切换会话后补刷，失败恢复重连。
- `app.js`：同一照片标星时保留中央图片节点；带正确文件指纹的预览允许浏览器私有缓存，同路径更新使用新 URL。旧版本 URL 和错误占位图不设置永久缓存。
- 保持 RAW/JPG 身份合并、评级保存、暂停跟随等现有行为；没有复制或依赖 Canon 私有组件。项目原本就按需使用 RAW 内嵌 JPEG，本次未声称新增 Canon RAW 解码算法。

## REA 连接与引擎

按用户追加授权，`rea setup --client codex --dry-run` 后执行同范围 setup；配置备份留在用户 Codex 目录。Ghidra 12.1.4 与 Temurin JDK 21.0.12.1+1 从官方发行源下载并核对 SHA-256，安装在 `C:\Users\jiang\.local\share\rea\tools`。仅在 `[mcp_servers.rea.env]` 设置 Ghidra、Java 与 provider，未改变系统 Java 或 PATH。

`rea doctor --provider ghidra --client codex` 指定检查全部通过；独立 MCP initialize / tools/list 成功返回 REA 6.5.0 与 207 个工具；实际 Canon 原生 DLL 反编译也已成功。当前运行中的 Codex 对话工具清单仍需重启/重新连接后加载，未擅自重启 Codex。

配置与完整下载摘要记录在 `build/dpp-analysis/REA引擎配置记录.md`。官方参考：[REA Windows Ghidra 支持](https://github.com/morluto/rea/blob/main/docs/windows-ghidra-p0.md)、[Ghidra 官方发行](https://github.com/NationalSecurityAgency/ghidra/releases/tag/Ghidra_12.1.4_build)、[Codex MCP 配置](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。
