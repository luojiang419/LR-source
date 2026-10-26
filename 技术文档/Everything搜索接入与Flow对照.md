# Everything 搜索接入与 Flow Launcher 对照

2026-10-23，基于本机 Flow Launcher 安装文件静态分析和本机 Everything 的实际只读查询。开发与交付状态见 [任务 083](../开发任务/083-后台索引稳定刷新与任务清理.md)。

## 结论与接入选择

末帧通过自己随包携带的官方 SDK 查询已运行的 Everything。手动添加的位置负责限定可检索目录与共享权限，不要求用户为末帧再建立一份完整文件名索引。Everything 负责文件发现，末帧数据库保存命中缓存、OCR、SKU 入库日期、人工信息和参考图状态。

| 方式 | 特点 | 本次选择 |
| --- | --- | --- |
| 官方 SDK / IPC | 直接查询运行中的 Everything，支持范围、排序、数量、分页与文件属性 | 生产入口；同 Flow 的基本接入方式 |
| 官方 `es.exe` | 命令行封装，便于手工诊断；另需维护命令行程序的分发与解析 | 可用于诊断，生产不额外依赖 |
| Everything HTTP / ETP | 适合跨设备查询，需要另外配置服务与访问控制 | 已有末帧只读 API 继续承接局域网共享，无需用户额外启用服务 |
| 自行解析 `Everything.db` | 需要自行处理文件格式、版本、更新时机及一致性；磁盘文件不等同于应用内的实时查询接口 | 不采用，不修改或重建用户 Everything 数据库 |

官方接口说明：[SDK](https://www.voidtools.com/support/everything/sdk/)、[IPC](https://www.voidtools.com/support/everything/sdk/ipc/)、[命令行](https://www.voidtools.com/support/everything/command_line_interface/)、[HTTP](https://www.voidtools.com/support/everything/http/)、[ETP](https://www.voidtools.com/support/everything/etp/)。表中取舍是末帧的工程设计选择。

## 本机 Flow 的实现证据

- 用户指定目录 `C:/Users/jiang/AppData/Local/FlowLauncher/app-2.1.3`；其中 Explorer 插件 manifest 实际版本为 `2.1.4`。
- 插件程序集 SHA-256：`a3a97664e677db8f5aff55b6e740ead9a4e5dd5bf3f9b8663af13edc7d9444e0`；MVID：`b7937457-2e65-47f2-938e-b668d5baebc7`。
- 静态调用链：按进程架构加载自己的 `EverythingSDK/x64` 或 `x86` DLL；单个 `SemaphoreSlim` 保护 SDK 状态；设置查询、Offset、Max、排序及路径匹配后调用同步 `Everything_QueryW(true)`，读取完整路径；结束时 Reset、Release。
- 有父目录时，递归搜索追加目录路径，非递归使用 `parent:`；底层支持 Offset/Max，已分析 manager 每次使用 Offset=0，没有观察到自动同步整个 Everything 数据库。
- 本程序集完整 P/Invoke 清单没有 `Everything_SetInstanceName`。取消检查不能中断已经阻塞的同步原生查询；末帧保留可终止子进程超时边界，不将原生调用放入界面线程。
- 以上是静态观察及与 CIL 绑定的反编译重建，不代表执行观察。未运行 Flow、修改其配置或复制其 DLL 作为末帧依赖。

REA 证据：程序集 `ev_d0a8ad2d8a62916a5d89c9476e343e1d103273a599213e5742ec9391dde5e226`；P/Invoke `ev_768b5e5ea6b7761dee42a3d498d684f549c1055b110158c7aa60565516b37432`；方法/CIL `ev_3f9cae2ebbee07d0582bd184698d21b71b97b98082e261aa196ecacb194081be`；重建绑定 `ev_e9f8c0302341aba8e2fa0fef953165973d53c3ab38d48016459b9684ffaf7c30`。完整证据与可读重建位于忽略目录 `build/flow-everything/`。

## 末帧的新搜索逻辑

1. 添加目录后先查询 Everything 的范围覆盖。已有记录的目录直接就绪，不再递归遍历几十万张照片，也不全量复制索引。确实不可用或无覆盖时，继续使用目录索引回退。
2. 同时处理 UNC 与本用户已有映射盘的等价路径，按目录段检查边界；结果统一回到已添加根目录的路径表示，避免相邻同名前缀目录混入。
3. 默认搜索“全部已添加目录”，包括可见的本机目录与已授权 API 资源；保留“当前目录及子目录”选项。修改文字或范围不自动提交；按钮/回车提交，翻页沿用已提交范围。
4. 文件名/路径查询直接请求 Everything；SDK 一并返回大小与修改时间，搜索不逐个探测 NAS 文件。命中集合按批次获取，缓存与 RAW/JPG 合并后每页展示 60 张，不以首次 2048 条截断整个匹配集合。
5. 全局结果携带自己的设备和根目录，预览、固定、右键定位均使用真实来源。重叠本机目录的同一路径去重，不将不同设备的同名文件误合并。
6. 真实零结果、连接故障、部分位置不可用分别处理。故障时可使用已有缓存；SDK 返回零结果不会强行拿不相符的旧缓存冒充命中。
7. 预览仍通过既有目录授权与重解析路径校验。缓存索引不授予超出已添加或已授权目录的文件读取权限；不读取照片内容做文件名搜索，不写原图。

## 根因与首次实测

本机 Everything `1.4.1.1032` 的 IPC 正常。Windows 的 `Y:` 对应 `\\192.168.0.188\摄影`，但原软件只查询规范化后的 UNC，Everything 则按 `Y:\` 返回。原 UNC 关键词查询结果为零，映射盘查询同词命中 176 个文件。

修正后首次源码实测：Y 位置覆盖 486901 个图片文件，约 0.22 秒就绪；`LC7877499` 返回 176 张，查询约 0.28 秒。X 位置同样识别到 200218 个图片文件。该时间属于本机当时状态，不是所有 NAS 的速度保证；最终安装版与全局范围验收见任务文档。

上一轮“先统计完整目录再调用 Everything”的流程也会阻挡首批结果，本次已将 Everything 覆盖判断提前。`.framekeep-write-*` 等内部临时目录及回收目录不参与回退扫描，避免短暂消失的写入临时目录中断整轮索引。
