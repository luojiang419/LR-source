# 原生快速索引与 Everything 机制对照

2026-10-23。目标是不依赖 Everything 也能完成参考资源的文件名/路径检索；保留既有 Everything 可选入口。实现与交付状态见 [任务 084](../开发任务/084-无Everything原生快速索引与搜索.md)。

## 分析证据与边界

本机 Everything `1.4.1.1032`、PE x86-64，SHA-256 `f191f756996a14a11e5445fa7103d302efd510cf2fbf920e6c0c8ed51d512e36`。REA 已实际打开、导出证据并正常关闭，没有执行目标或更改其数据库。

- REA 资源/身份证据：`ev_df7ea19d2b8cfa1e414126badbd8a31d65ff8b21b882cd8a93b998e44b3cb2b1`；PE 段表证据：`ev_f6f33cb38b64ae243a0e79e30a79bf3ef7184d87e581930de0417d8f9bcfa1a7`。
- 现有 objdump 的有界汇编确认 `DeviceIoControl` 使用 `0x900f4`、`0x900bb`，对应查询与读取 USN Journal；字符串包含 MFT 记录解析、父文件引用、日志身份变化及目录变更通知路径。这些 shell 静态证据与 REA Evidence 分开保存。
- 本机缺少深度反编译提供商，没有完整还原 Everything 的内存布局、排序/匹配算法或锁策略。不能称 Everything 使用本项目的 SQLite、FTS5 或 trigram；这些是本项目基于轻量索引、候选筛选和增量维护原则选择的实现。
- 官方说明文件名索引与文件内容分开，NTFS 可利用 USN；网络共享仍需目录枚举、通知与对账。参考 [Everything 索引](https://www.voidtools.com/support/everything/indexes/)、[文件夹索引](https://www.voidtools.com/support/everything/folder_indexing/)。

完整证据保存在忽略目录 `build/everything-native-analysis/`，包括原始 REA bundle、文件摘要、PE 导入、限定汇编与复现脚本。

## 本项目迁移的机制

| 环节 | 实现与效果 |
| --- | --- |
| 名称发现 | Windows `FindFirstFileExW` 的 Basic/LARGE_FETCH 批量枚举直接取得名称、大小、修改时间；不逐文件追加 stat，不读图、不扫描 EXIF、不全库预热预览 |
| NTFS 首批 | 已有权限及本机 NTFS 条件满足时，用只读 `FSCTL_ENUM_USN_DATA` 提供有界机会候选；预算/硬链接/重解析点边界决定它不能替代完整目录补齐；无权限自动回退，不装服务、不提权、不创建 USN 日志 |
| 建库流水线 | 单次 BFS 流式遍历，每批 128 条提交，已提交部分立即可搜；两个根目录并行，慢共享不挡另一个位置 |
| 持久检索 | 在参考目录数据库旁维护 FTS5 trigram 路径倒排索引，SQL 候选过滤后执行字面子串核对；RAW/JPG 仍归为同一拍摄资产 |
| 热路径 | 只查本机索引，不探测原目录或文件；SQLite/系统缓存复用索引页。后台迁移时完整回退，迁移提交后原子启用，重启复用已建结构 |
| 查询计划 | 文本查询用 `+root=?` 避免 SQLite 被名称排序索引误导为整根扫描；候选由 FTS 与 `catalog_pair` 驱动，普通目录浏览保留原名称索引 |
| 写入效率 | 保持一个轻量 WAL 生命周期连接，避免每个批次关闭最后连接后反复 checkpoint；关闭时停止后台迁移、等待写入退出，再释放连接 |
| 增量 | `ReadDirectoryChangesW` 同步新增、修改、删除、文件/目录重命名；溢出或重连触发补扫。重启后台对账，通知有效时每六小时兜底，无通知时每半小时兜底，热查询不等待对账 |
| 恢复 | 取消/权限错误/离线保留已提交快照，不将未读到的整库误判删除；临时子目录消失有限重试；仅完整成功的一轮清理旧 epoch |
| Everything 关系 | 已有完整本机索引优先查询；无完整快照时可用 Everything。Everything 不可用后自动补建本机索引，避免永远只剩之前的命中缓存 |

微软接口说明：[批量目录枚举](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-findfirstfileexw)、[目录变化通知](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-readdirectorychangesw)、[NTFS 名称枚举](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ni-winioctl-fsctl_enum_usn_data)。SQLite 方案见 [FTS5 trigram](https://www.sqlite.org/fts5.html#the_trigram_tokenizer)。

首次目录总量尚未发现完时，界面显示“已索引 N 个文件”和不定进度，允许立即搜索；不为计算总数再完整走一遍目录。后续参考上轮文件数显示带“约”的估计百分比，完整结束才显示 100%。

## 验证方法与结果

### 50 万条元数据的真实 HTTP 查询

构造 50 万文件记录、25 万组 RAW/JPG 的真实 `items/capture_items` 数据结构。调用实际 `/api/reference/search`，包含计数、去重、排序、分页和 JSON。明确不接入 Everything，并禁止查询阶段调用原文件 `stat`、目录枚举和路径解析；测试通过。

| 查询 | 返回拍摄资产 | 首次 HTTP | 后续中位 HTTP |
| --- | ---: | ---: | ---: |
| `LC7877499` | 100 | 11.22 ms | 10.18 ms |
| `新品/LC7877499` | 100 | 14.52 ms | 17.48 ms |
| `look123456` | 1 | 6.13 ms | 3.54 ms |
| 不存在的款号 | 0 | 2.18 ms | 1.60 ms |

这不是 50 万张实拍图片；是合成元数据上的完整 HTTP 基准，首次请求没有清空操作系统文件缓存，不能称物理冷盘。基准索引构建约 2.28 秒，不含构造元数据的时间。代码/JSON：`build/native-fast-index/benchmark_reference_api.py`、`reference-api-500k.json`。

独立 SQL 对照中，典型款号由原约 217 ms 降至约 8.6 ms，唯一文件名由约 433 ms 降至约 2.8 ms；测量边界为完整 SQL 计数和排序分页，不能与 HTTP 数字混用。

### 真实临时照片的首次建库

100 个目录、10000 张有效 24×24 JPEG，从添加位置开始计时：首批 128 条约 48 ms 后可搜索，约 1.98 秒完成持久索引。照片解码与全库预览被禁止，验证未调用。这个时间不包含创建测试照片，也不代表真实 NAS 大图库的建库保证。

单独的原生元数据枚举：10000 个临时文件约 0.196 秒；真实 G 盘 MFT 首批 20 个候选约 0.397 秒。它们只衡量发现阶段，不是完整数据库建库时间。

### 冻结引擎与数据安全

在全新隔离资料下明确停用 Everything，真实冻结引擎完成本机 JPEG 与实际 NAS 子目录建库、HTTP 搜索、原生新增/改名/删除、分页及图片预览；原图 SHA-256 保持，页面无脚本错误。最终安装路径、耗时和 50 万元数据的安装引擎结果以任务 084 与 `build/native-fast-index/native-installed-result.json` 为准。

最终安装目录实测：320 张本机 JPEG 约 0.244 秒完成建库；所选 NAS 子目录 123 组约 0.155 秒建库、HTTP 查询约 3.36 ms。另在该安装引擎的隔离库写入 50 万条合成元数据，100 命中查询首次约 7.98 ms、后续中位约 16.45 ms；这是另一组关键词与合成分布，不与上方源码基准混作同一组样本。

## 实际限制

- NAS/SMB 不支持本机 NTFS MFT 枚举；第一次完整发现仍取决于网络、目录数和权限。已有快照可立即搜索，新增未知目录需由后台发现。
- 本版用变化通知与受控对账维护，不声称完整复制 Everything 的 USN 断点恢复与私有 MFT 解析器。
- 不足三个字符的字面查询保留普通扫描；极宽关键词的大量精确计数/排序仍有成本。SQL 基准中 `.CR3` 命中 25 万组约需 2.5 秒，该宽查询没有承诺毫秒级完整 HTTP 响应。
- 已授权 API 设备的网络响应另受对方和网络影响；本机倒排查询的毫秒数字不覆盖远端传输、图片解码及显示时间。

本机持久索引属于可恢复的资料索引，不应被当作普通预览缓存周期性清空。移除位置撤销访问，不删除原图；业务 OCR、SKU 入库日期、人工评级等数据保持各自原有数据库。
