# Canon DPP 监控预览机制与项目优化

## 2026-10-28：主 JPG 直接呈现的深度追踪

本轮继续用 REA 追踪同一安装版本，证据与目标 SHA-256 均已核对。下述为静态调用证据，尚未测量 DPP 的真实首帧耗时；不把“本软件显示前不校验”反推为 DPP 完全无检查。

| 问题 | 观察与证据 |
| --- | --- |
| 大图是否放大 EXIF 小图 | `DSFPreviewProcess.GetDevelopParameterPreviewRatio` 在适应窗口时返回 `WindowFit=2`，100% 为 6，没有返回 `Thumbnail=1` 的分支。snapshot 的 `0x30200`、`197121`、`197122` 分别携带 ratio、目标尺寸、ROI。Evidence `ev_5fac8465da23facd23a0d5aad6071ce3b960101103baa112a98e167e6503c5ac`。 |
| 原生大图请求 | `DppMWProcessPreviewImageEx → 0x18002a1d0 → 0x180036d20 → Normal core 0x18003a7e0 → 0x18003b060`。Normal core 读取 ratio/ROI，查询 cache 或在途任务，再提交图像工厂。Evidence `ev_76c65187e01b0a0c77ac1adbff9d5fca0de536d5b28ff978a73c92cd1ad06a8b`、`ev_e339fe5e9e63ecfca3a616e68fd2d7fd41763038430329d5d02494b1be651ebb`。 |
| 可用图像何时显示 | `DAPImageController.PreviewProcessReadyImage` 使用 `TryEnterReadLock(0)`，拿不到即返回；通过 Dispatcher 核对当前 thumbnailID/snapshot，随后 `UpdateBackBuffer + Refresh`。不等 High 画质才绘制，High 另用于移除处理图标。Evidence `ev_1379748df2ba307b4644fc6767f45d73c60492669eb52fc2906bd92cfab831d6`。 |
| 写句柄探测是否阻挡显示 | 查到的 `GENERIC_WRITE/share=0` 探测已绑定 `DppMWCommitRecipe` 和 `DppMWSaveImageFileAs` 写入链，不能当作显示前等待证据。Evidence `ev_1e8aaaa58095510f65efee12d5f1d69367746520d0cd94f6dea1db87c98ef0b0`、`ev_d461c4eb6a61f63d6449d5888212f0dd62cbd827d364c93a75c72588051bd836`、`ev_db28d44b6c10bd39d2e509fafe2fa5a11146487c1947a39b3f83fef83828b41f`。 |

已观察到独立预览队列、取消旧请求及即时提交可用图像。尚未绑定普通 JPG 运行时使用的具体 codec/file-loader，通用文件打开器不能直接代表显示链；缓存内部是否使用低清图、具体 EOF/共享策略也未确定。恢复结果不复制到产品。完整 46 条 REA records、查询边界及辅助反编译记录位于本机忽略目录 `build/dpp-sharp-direct/ledger.md`、`rea-evidence-bundle.json`；原生会话已关闭。

按用户最新要求，本软件独立实现 `DirectJpegFeed`：本地文件新增/重命名或 EOS 通知直接注册原 JPG，独立状态锁和长轮询不等待 `FolderWatch` 的资料锁。HTTP 仅授权路径并传送原始压缩字节，不调用 Pillow、ExifTool、图像哈希、稳定性、排他写句柄或预览重编码。浏览器原生 load/error 决定能否呈现，文件增长时合并更新，较新图片取消旧请求。

独立 JPG 正式接续和手动查看均使用 `previewFull`，避免又切回 480/2048。RAW 和其他格式保持原处理。后台正式入库、评级写回的原片保护独立保留。浏览器能容忍某些截断 JPG，因此不能把 load 事件当作完整性校验，也不在产品重新添加 EOI/像素预检。验收在测试中核对原始字节及主图尺寸，包含写句柄持续打开、资料锁阻塞和迟到旧图。

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

2026-10-28 补充核验：REA 本机 CLI 重试成功，新原生证据 `ev_7f1d966aaa0b6bb4717d87edf981c884de16f7d1d488cb711101d28956166b58` 再次确认目录通知 0x19/0x2。管理层仅选取 `Canon.Dpp.Dsf.Image.Preview.DSFPreviewProcess` 和 `Canon.Dpp.Dsf.Image.DSFImageListManager`，新证据 `ev_d92e799c6d6a02ab877189cf254b3da01dbaa2f333fc754a1b37bb42853fb678` 覆盖 266 方法及 2036 条静态调用边；确认工作队列、Dispatcher 通知和快照有效性校验。这是静态机制证据，没有测得 DPP 实际首帧时间。新证据留在 `build/shutter-display/`，原 DLL 身份未变。

本轮按用户澄清，以本地磁盘为验收范围。独立实现了有名称的 `ReadDirectoryChangesW` 通知、后台业务通知合并队列、SKU 通知短锁、JPEG 完整性校验像素复用；全量补偿、读取后指纹检查和 RAW/JPG 配对保持。完整进度、前后数据及边界见 `开发任务/134-拍摄到显示延迟与DPP对照优化.md`。

- `folder_watch.py`：采用同类 Windows 目录通知，空闲 2 秒补偿重扫；待处理文件约 80ms 重试，完整性、写句柄和元数据读取前后指纹检查保留。缓存已知记录，避免每次等待重扫都从数据库取全部照片。
- `media_engine.py`：监控持有常驻 ExifTool，完整保留身份、子秒、方向与原有评级字段。每个请求有独立完成编号，取消、超时、关闭均释放进程及读取线程，下次可恢复。
- `server.py` / `folder-watch.js`：使用可取消的长轮询，入库立即唤醒；关闭对话框、退出交互暂停、切换会话后补刷，失败恢复重连。
- `app.js`：同一照片标星时保留中央图片节点；带正确文件指纹的预览允许浏览器私有缓存，同路径更新使用新 URL。旧版本 URL 和错误占位图不设置永久缓存。
- 保持 RAW/JPG 身份合并、评级保存、暂停跟随等现有行为；没有复制或依赖 Canon 私有组件。项目原本就按需使用 RAW 内嵌 JPEG，本次未声称新增 Canon RAW 解码算法。

## REA 连接与引擎

按用户追加授权，`rea setup --client codex --dry-run` 后执行同范围 setup；配置备份留在用户 Codex 目录。Ghidra 12.1.4 与 Temurin JDK 21.0.12.1+1 从官方发行源下载并核对 SHA-256，安装在 `C:\Users\jiang\.local\share\rea\tools`。仅在 `[mcp_servers.rea.env]` 设置 Ghidra、Java 与 provider，未改变系统 Java 或 PATH。

`rea doctor --provider ghidra --client codex` 指定检查全部通过；独立 MCP initialize / tools/list 成功返回 REA 6.5.0 与 207 个工具；实际 Canon 原生 DLL 反编译也已成功。当前运行中的 Codex 对话工具清单仍需重启/重新连接后加载，未擅自重启 Codex。

配置与完整下载摘要记录在 `build/dpp-analysis/REA引擎配置记录.md`。官方参考：[REA Windows Ghidra 支持](https://github.com/morluto/rea/blob/main/docs/windows-ghidra-p0.md)、[Ghidra 官方发行](https://github.com/NationalSecurityAgency/ghidra/releases/tag/Ghidra_12.1.4_build)、[Codex MCP 配置](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。


## 本机 HTTP 连接池与显示隔离

后续实拍反馈表明，Python 内部队列独立仍不足以保证端到端显示。业务 API、主状态长轮询和原图共享 HTTP/1.0 来源，浏览器连接达到上限时，图片会在 requestStart 之前等待。六个业务请求保持阻塞的实际浏览器探测记录了约 1513ms 的网络排队，图像响应本身约 1ms。

项目新增独立 loopback 图像来源，事件长轮询和原始 JPG 直接走该来源；主工作台显式公开本进程的来源及凭证，CSP/CORS 只开放这一来源，图像与直方图的 Image 使用 anonymous。显示不经过目录资料锁或预览缓存。最新完成图像地址在正式入库后保留，避免浏览器迟到查询漏通知。该方案是项目自己的传输隔离设计；DPP 的证据仍以原生独立队列和 backbuffer 提交为限。

压力回归覆盖业务连接占满、跨来源 Canvas、迟到查询、凭证和路径边界。完整源码服务 2100 历史照片、与布局回归并行时，模拟主 JPG 到绘制中位监控 87.55ms、EOS 72.54ms。真实快门和 DPP 同条件计时仍需独立记录。


最终本地实拍补证：独立通道版的三张真实文件到观察页面主图为 46.87/88.16/57.33ms；最后左右同步版 XSY_8106 为约 63.37ms、主 JPG 原始 2400×1600，用户原生窗口确认左右立即跟上。侧栏使用已加载主图，等正式缩略就绪后接续。最终版本 20261028130925 已发布魔搭；观察值不等于物理快门或原生像素/DPP 同条件测量，REA 静态边界保持。
