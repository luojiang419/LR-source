# JPG 现场评级与后拷 RAW 自动匹配补写

[CODEX_LONG_TASK_CONTINUE_V3]

- 当前状态：开发、114 项回归、真实 RAW 及最终便携包验收、部署完成；已支持先 JPG 后 RAW 的跨目录自动匹配及 RAW 内嵌星级/SKU。
- 下一步：现场 JPG 正常标星/备案；把 RAW 拷到电脑后，在末帧添加并打开该目录，右侧查看自动补写结果。
- TODO：本轮开发验收无剩余项；物理相机卡传输与 Lightroom GUI 重新导入没有在本轮操作，不声明已实测；CR3/ARW 以外的 RAW 格式保留未开放状态。
- 最近验证：114 项测试通过；公开 R6 II RAW/C-RAW、Sony ARW 副本内嵌读回成功；感光数据/内嵌预览保持，CR3 全部 mdat 负载保持；最终引擎验证重启后再拷 RAW、目录选择触发、4/5 星及细节/模特类型继承、重开不重复写、改名后可读 SKU。
- branch：`codex/jpg-raw-reconcile`；基线 `68fd1d8`；本地交付提交 `feat: reconcile later RAW imports with rated JPG assets`，不推送。
- 阻塞：无交付阻塞。生产没有导入测试样片、模拟评级或补写任务，原有索引/评级/清单保持。

## 目标和规则

1. 现场 JPG 的评级、SKU、项目和拍摄类型在本机持久化；RAW 不在电脑时不阻塞现场操作。
2. 复制 RAW 后通过现有“添加文件夹/选择目录”入口索引并打开，后台自动执行跨目录配对；JPG 和 RAW 不要求相同路径。
3. 文件编号不单独作为匹配依据。优先使用可靠原始图像标识；否则联合相机型号/序列号、拍摄日期时间及子秒、原始文件编号。缺关键证据、重复候选、跨项目同名冲突均待确认。
4. 同步的是 JPG 已保存的明确星级（含人工 0 星）；不把时间规则产生的临时推荐当已标星。RAW 已有不同的明确人工评级时提示冲突，不静默覆盖。
5. 将确认的 SKU、资产身份、拍摄类型及星级写入可验证的 RAW 内嵌 XMP；保护拍摄参数、RAW 感光数据和内嵌预览。每文件复制/校验/备份/journal/替换，失败保留可重试状态。
6. 识别为同一拍摄资产后，两地目录共享资产 ID，RAW 独立文件 ID 和位置；不移动 JPG/RAW、不依赖拷贝 XMP 伴随文件。
7. 选择文件夹自动处理唯一可靠结果；歧义提供明确证据与人工选 JPG 后重试，所有动作记录数据库。

## 已落地实现

- `media_engine.py`：读取并保存 captureIdentity（型号/序列号、日期时间/子秒/时区、原始图像 ID、原始文件名/编号），与路径 ID 分开。忽略 `.framekeep-` 自有工作文件/目录，不把元数据副本加入目录监控。
- `raw_reconcile.py`：独立后台队列，复用资产库新增 raw_sources、raw_pairs、raw_session_work。首次稳定识别、来源、证据、目标签名、操作 ID、冲突和成功状态持久化；旧目录缺身份时按需批量补读 EXIF。
- 匹配先按型号/拍摄秒分桶，再核对序列号、子秒和文件/图像身份。已知不同序列号或子秒直接拒绝；仅一个可靠资产来源时自动使用，同资产多个已登记副本可归一，但评级不一致仍复核。文件名不能单独匹配，跨日同名不串款；旧 JPG 明确评级从会话 overrides 或文件真实评级取，不从时间规则推导。
- RAW 与来源 JPG 关联同一资产，各自文件 UUID/位置；已有独立备案的 RAW 不自动合并，未备案的临时资产可安全归并。不把当前拍摄工作表作为补写来源，类型继承原 JPG 备案。
- `catalog_store.py` 在索引完成和目录激活通知队列；`server.py` 在 JPG 保存评级、资产归属变化后通知队列。已有 RAW 目录再次选择时重新检查新拷入文件；后台由增量追加保持评级，不用清库重建。失败重试重新核对文件版本，已提交但未记账的操作按 journal 恢复。
- `metadata_writer.py` 开放 CR3/ARW 的内嵌 XMP，支持可选 Rating（含显式 0）。字段读回必须一致，原有非目标字段保持；容器中由 ExifTool 合法调整的已知结构偏移/长度允许变化，拍摄参数不放宽。LibRaw 完整感光数组和内嵌预览校验一致，CR3 还校验所有 mdat 字节负载/长度。
- `.framekeep-write-<uuid>/原文件名` 同卷隐藏工作目录隔离临时文件；写前保护句柄、最后哈希核对、备份、journal 和替换沿用 013。Windows 替换结束后清空自己的临时目录。RAW 内部写入失败不会静默生成 sidecar 冒充成功。
- `raw-reconcile.js`：右侧常驻状态、明细弹窗、分页、待确认/失败筛选、来源 JPG 和匹配证据、明确覆盖文件已有冲突/重试。成功后原地刷新 RAW 工作台评级，不改变本目录人工覆盖。已应用的相同目标不重复写；JPG 后续改星/改 SKU 可继续补写。
- 增加 API：`GET /api/raw-sync?session_id=...&status=...&offset=...`，`POST /api/raw-sync/retry-session|retry|review`。写操作保留本机令牌校验。构建包含 13 个正式前端文件，复用未变化的 Rust 壳。

## 样片与验证证据

1. 本机此前 X 盘 CR3 路径当前不可用。使用 raw.pixls.us 公共 CC0 样片，下载后与仓库 SHA-256 核对一致；样片原件保持，只在受管临时副本写入：
   - Canon R6 Mark II 普通全幅 RAW：`getfile.php/6402`，SHA-256 `1a93f96556aabbc4a7260750ab0a191ec7926bb978fe8428bf3465121da730d6`。
   - Canon R6 Mark II 全幅 C-RAW：`getfile.php/6409`，SHA-256 `409eb724212abde94e18fbd3170abf3264951fab13384c36cbf6fb4f22ea4201`。
   - 来源元数据/下载 URL 在忽略目录 `build/raw-reconcile/sample-provenance.json`，不把照片提交到 Git。
2. 两个 CR3 均可读回中文 SKU、拍摄类型、资产 ID 和星级；感光数组、内嵌 JPEG、全部 mdat 负载摘要保持。R6 II 普通 RAW 感光摘要 `fa67690c7bdc448e146808f54176a456af25459c42cfcccb1539649333e00b83`，C-RAW 摘要 `edb3745dec39ca3e317040625b30159ab18fc0cca296ca5440fa50b113967e01`。
3. Sony ARW 使用当前目录一张原片的临时副本验证内嵌，感光数组摘要 `9d36b5b9424c9b5ff49d5148cbff4bf239ff9f9dbbe743ee393ca65f82f7497f`、内嵌预览摘要保持；原片没有写入。允许 TIFF/Sony 结构指针重定位，所有其他已读参数严格核对。结果在 `build/raw-reconcile/arw-validation/result.json`。
4. 完整 **114 项测试通过，133.997 秒**。新增覆盖保守匹配/异机/跨日/子秒、无效时间、0 星、重复来源不写和人工确认、已有 RAW 星级冲突、失败不动文件/重新规划重试、JPG 后续改星、重复选择不改写、同路径重新拷入原始 RAW 后补写、隐藏临时文件隔离、真实 Chrome 目录选择触发/4 星刷新/来源明细/刷新恢复。
5. 最终 PyInstaller 引擎在隔离库先索引两个从公开 RAW 预览构造、保留拍摄 EXIF 的 JPG，分别设 4/5 星并备案为细节/模特。重启后再拷真实 RAW，通过真实 Chrome 的“添加文件夹”及左栏打开触发补写；不用 source 直接调用替代打包链路。验证两张 RAW 的内部字段、感光/预览/mdat 不变、无 `.xmp`，再重启重开不改文件哈希，RAW 单独复制改名仍可读 SKU 并归回原资产。页面无脚本异常，截图已检查，结果在 `build/raw-reconcile/packaged-result.json`。
6. 不将上述文件复制过程当作相机 SD 卡或现场无线传输验收；没有对 Lightroom GUI 做新写入样片的重新导入操作，标准内嵌 XMP 的读回由 ExifTool/LibRaw 实测。

## 正式部署与保护

- 已正常关闭旧桌面再更新程序，不强制终止，不操作原片；源索引 1 会话/1393 张、人工评级、监控配置、85 款清单和原资产/视觉业务行部署前后摘要一致。部署前对四个 SQLite 库执行一致性备份，结果在 `build/raw-reconcile/production-before.json`、`production-after.json`。
- 正式库 raw_pairs/raw_sources/工作队列为空；未选择用户 RAW 目录、未创建测试任务、未做生产原片写入。由用户在界面选择 RAW 目录触发。
- 发行目录 `dist/末帧选片/`，继续使用现有 `.framekeep`。13 个前端文件与源码逐项一致；桌面 PID 29492、非零窗口句柄，引擎 PID 41492，首屏 1937ms，`http://127.0.0.1:8769/api/health` 为 busy=false。进程号是本轮观测值，非固定配置。
- 版本仍为 1.0.0，模型配置/照片/数据库/备份和下载样片均不提交，不推送 GitHub。
