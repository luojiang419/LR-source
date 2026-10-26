# Lightroom 预览速度对照分析

[CODEX_LONG_TASK_CONTINUE_V3]

- 当前状态：分析完成；已完成 REA 资源取证、Lua 字节码静态核对、当前源码调用链核对、本地持久队列采样和真实内容编码微基准。没有修改产品代码。
- 下一步：后续优化优先实现嵌入预览快速模式和按批次/目录调度，再做同一 NAS、同一 2000 张素材的冷缓存端到端对照。
- TODO：无本轮分析剩余项；速度优化、安装交付与完整批次测速不属于本轮已完成内容。
- 最近验证：Lightroom 文件版本 15.4.1；4 份 REA PE 资源证据与 1 份 objdump 证据；3 个 Lua 5.1 字节码资源完成结构解析且恰好消耗全部资源字节；本地真实预览像素编码各测 3 次中位数，源缓存哈希不变。
- branch/commit：`docs/lightroom-preview-analysis`；分析起点 `4bea408`；本任务文档单独提交，准确提交号见 Git。
- 阻塞：无分析交付阻塞。没有同一批 2000 张端到端实测、SMB 字节统计或原生函数运行跟踪，因此不宣称已证明 Lightroom 十分钟的耗时构成或优化后的整批加速倍数。

## 目标与边界

用户反馈 Lightroom 约 2000 张照片可在十分钟左右生成预览，而当前软件明显更慢。检查指定安装目录 `C:\Program Files\Adobe\Adobe Lightroom Classic`，对照当前源码和已有可安全读取的性能证据，解释可能的速度差距并给出有优先级的改进方向。

用户已确认 Lightroom 选择的是「嵌入和附属预览」，素材在 NAS。这是本轮比较的前提；不将其等同于标准预览或 1:1 渲染。

本轮不修改 Adobe 文件，不启动全库重建或删除缓存，不改动用户已有任务 036 与未跟踪 SDK。不以静态分析冒充完整反编译或运行跟踪。

## 主要结论

慢的原因已经有具体证据：两边工作量不同，当前软件保存的后台任务范围也远超过 2000 张。当前实现虽同样优先使用 RAW 内嵌图，并未默认完整解码 RAW，但还会解码、缩放并重新压缩成两份 WebP；全局小图优先策略又使大批 NAS 历史任务阻挡后台清晰图。

| 对照项 | Lightroom「嵌入和附属预览」的静态证据 | 当前软件已核实行为 |
| --- | --- | --- |
| 预览策略 | 明确区分嵌入、大缩略图、标准与最终渲染；嵌入相关分支设置 `skipRender=true`、`allowRender=false`，请求保留 JPEG 数据 | 归档固定完成 420px 与 4096px 两阶段后才算完整 ready |
| 缓存内容 | 读取嵌入图的 JPEG 数据，存在 JPEG 金字塔/分层缓存接口 | 提取/读取图像后重新生成 WebP；大图 Q86、method4 |
| 并发与调度 | 嵌入、标准、1:1 使用不同批量限制接口；存在预览优先级与取消接口 | 多根目录共享队列，严格 `ORDER BY edge,attempts,requested`，全部 420 阶段先于全部后台 4096 阶段 |
| GPU | 静态资源包含 GPU 预览开关与调用；不能据此认定此批任务实际使用 GPU | JPEG 解码/缩放可使用 GPU，WebP 编码仍在 CPU；GPU 两路会话 |

Lightroom 仍可能解析容器、解码、生成较小层级与写索引，不能将上述证据解读为完全零解码、零重编码或仅复制文件。这里能够确定的是它具有保留嵌入 JPEG、跳过标准/最终渲染的独立路径。

## Lightroom 取证

REA CLI 为 6.7.0。`doctor --client codex --json` 显示当前 Codex 注册缺失、未配置 Ghidra/Hopper/IDA/Kuna，当前会话也没有 REA MCP 工具。因此使用已安装 CLI 的无引擎 PE 资源解析继续工作，未修改 Codex 配置、未安装反编译引擎。

| REA 操作 | Evidence ID | 用途与边界 |
| --- | --- | --- |
| `inspect-pe-resources Lightroom.exe` | `ev_6372654f2fed074926636f37bd2117c5e5971d965524b8053afcde3fa3bc6537` | 确认 PE 资源、Lua 资源来源和原文件范围 |
| `inspect-pe-resources Library.lrmodule` | `ev_c8fcbec646e8eec3ae90c4c15860bb702fc7e4a76021202a048f5c88eea4b2b1` | 定位 `PREVIEWCOMMANDS.LUA`，确认四类预览入口 |
| `inspect-pe-resources LibraryToolkit.dll` | `ev_314f67121bb0fcb8f101898bc06f9e2dae11cfb7d9a8785a0c72ea038bd2f311` | 定位预览策略、构建器、缓存和队列 Lua 资源 |
| `inspect-pe-resources Import.lrmodule` | `ev_3baf7b71ff9717d1f775c70f7141177fd5ce441ac3526a58a6e6124e72af8c1c` | 定位导入预览处理、不同类型的并发批量接口 |
| `inspect-with-objdump LibraryToolkit.dll symbols` | `ev_1a9d1ed66ea8d513ac5e34ef83439d29d6d0ff878365fa7ab9b2dadb78ad0ca5` | COFF 符号表为 `no symbols`；不能当作没有导出接口或没有相关逻辑 |

`Lightroom.exe` SHA-256：`0b8563b1c185b6b698a2b3de68fb9e556004ad135cf89f0e76a52a6ece0fb718`。

`LibraryToolkit.dll` SHA-256：`66a1a7f9b3307fe7a352ddfcc73f76d812385d19214cc8d9314cc3910b5b7427`。

REA 对 Lua payload 只提供资源位置和哈希，并不解释字节码。本轮另用只读 Python 解析 Lua 5.1 头、函数原型、常量和指令，属于辅助静态证据，不标为 REA 原生反编译结果。未执行任何提取出的 Lua。

- `AGPREVIEWSTRATEGIES.LUA`（`LibraryToolkit.dll`）主函数导出 `kBigThumbnail='bthm'`、`kPreviewRender='prev'`、`kFinalRender='final'`。`main.1` 第 5–6 条指令设置 `reason='import_big_thumbnail'`、`skipRender=true`；标准预览分支 `main.2` 使用 `getPreviewRenderSize` 和 `import_preview_render`。
- `PREVIEWBUILDER.LUA` 的大嵌入缩略图分支 `main.14` 第 25–26 条指令设置 `allowRender=false`、`wantJPEGData=true`，调用 `readAgPixMapThumbnail`，随后将返回数据放入 `baseJpegData`。这证明有读取嵌入 JPEG 数据的代码路径；其原生函数内部读取范围和此批文件的实际命中率仍未知。
- `IMPORTPREVIEWHANDLING.LUA` 主函数第 39–50 条指令分别调用 `concurrentEmbeddedBatchSize`、`concurrentStandardBatchSize`、`concurrentOneToOneBatchSize`。实际值来自系统限制接口，本轮不推测固定线程数。
- `PREVIEWFILE.LUA`、`PREVIEWCACHE.LUA` 中存在 `jpegData`、`loadPyramidLevel`、`makeWithJpeg`、`savePyramidBlob`；`PREVIEWPROCESSOR.LUA` 存在分阶段优先级、嵌入预览请求和取消相关接口。以上字符串只说明实现入口存在，不能单独证明运行顺序或各阶段耗时。
- 安装包中文文本 `Resources/zh_cn/TranslatedStrings.txt:4291` 明确写有「嵌入与附属文件」，4292–4294 分别是 1:1、最小、标准；14275–14300 另有预览生成保真度、质量及标准尺寸设置。

## 当前软件与 NAS 持久队列

2026-10-26 08:53（Asia/Shanghai）只读采样 `D:/Program Files/Framekeep/data` 的本地 SQLite。采样时未发现 Framekeep/末帧进程，8769 端口拒绝连接，因此下述是最近保存的状态，不能声称程序当时仍在扫描或处理。

| 本地记录 | 数量 |
| --- | ---: |
| 两个 NAS 根位置下素材记录 | 179,308 |
| 图片记录（RAW/JPG 可分别计数） | 172,599 |
| 视频 / 设计文件 | 6,557 / 152 |
| 全部 pending 队列 | 139,744 |
| 归档等待 420 阶段 | 128,219 |
| 归档等待 4096 阶段 | 9,504 |
| 拍摄队列等待 480 阶段 | 2,021 |
| 归档缓存已有 420 / 4096 记录 | 9,587 / 1 |

根位置为两台 NAS 的摄影共享目录，`scan_epoch=7`，保存的状态均为 `scanning`。素材记录的大小合计约 3.32TB；这只是索引中的源文件大小总和，不表示已读取这些字节。队列与素材记录的计数口径不同，配对 RAW/JPG 也不能简单相加当成独立照片。

当前关键路径：

- `选片助手/preview_cache.py:503`：归档固定小图 420、大图 4096；`process_one:574` 每次只渲染一个阶段，成功小图后将同一任务更新为大图，不保存上一阶段的内存位图。
- `选片助手/preview_cache.py:578`：领取任务严格按 `edge,attempts,requested` 排序。因此任一根位置仍有 420 任务时，后台 4096 阶段会一直排在后面；9504 条大图积压与这种机制一致。前台单图请求有直接生成路径，不应表述为任何清晰图请求都必须等全库完成。
- `选片助手/asset_catalog.py:529`：有配对 JPG 时优先使用 JPG；否则提取 RAW 内嵌图。`prepare_preview_record:550` 首次补方向和小缩略图；`render_preview:614` 进入统一预览服务。
- `选片助手/media_engine.py:284`：JPEG GPU 尝试前会读入该 JPEG 的完整压缩 payload，420 小图也如此；GPU 不可用时再走 Pillow draft/CPU 路径。这里的完整读取指选中的 JPG 或提取出的 JPEG，不等于读取整份 RAW。
- `选片助手/media_engine.py:307`、`:332`：RAW 大预览按 `PreviewImage → JpgFromRaw → ThumbnailImage` 尝试，失败再走 LibRaw 内嵌缩略图，不默认 `unpack/postprocess`。两阶段可能重复打开 NAS 文件；没有 SMB 统计，不能量化实际重读字节或断言每次完整读取 RAW。
- `选片助手/media_engine.py:63`、`:127`：归档 ExifTool 使用 `stay_open` 进程池，不能归因为每张都启动新进程。
- `选片助手/preview_codec.py:29`：最终保存 WebP，常规 Q82，长边超过 1800 使用 Q86，method4；这部分不使用 GPU。`选片助手/gpu_media.py:13` GPU 并发槽为 2，与后台 worker 数不同。

本机 i7-14700K（28 逻辑处理器）、约 128GiB 内存；新建只读分析 Python 进程计算的当前 worker 预算是 28。这不是已停止安装服务的实测启动并发。CPU/内存决定线程数并不能证明 NAS/SMB 承受同样并发时吞吐最佳，需另测。

另有 1,801 条历史 `#recycle` 预览任务；素材表中对应 1920 条旧 epoch=1、仍标 online 的记录。当前目录扫描代码 `asset_catalog.py:372` 已排除回收站，`queue_previews:570` 也按当前扫描 epoch 过滤新入队内容。问题在于既有 pending 的执行前检查 `preview_source_current:597` 只核对 root/pair/online，没有复核扫描 epoch 或排除路径，历史任务仍有机会消耗工作资源。采样记录了 1290 条视频解码错误和 77 条无可用预览错误，它们是有错误信息的重试记录，不是已经最终失败的全部任务数。

安装目录引擎与当前便携发行引擎 SHA-256 相同：`f09c9b46c4006daa79c57e74c53eefdbbf2023e2fe2fdea8bcf9e3376b2821c2`。因此本轮不将问题简单归结为安装版引擎尚未同步 GPU 改动。

## 编码实测

只读取安装版已有的一张本地 4096 缓存，实际像素 2731×4096，来自索引中的 `JY000660.CR2` 内嵌预览衍生缓存；未读取 NAS 原片。先解码到同一份内存像素，再分别编码到内存，每种配置测 3 次取中位数。源缓存 SHA-256 前后相同，没有写入或重压正式缓存。

| 编码配置 | 中位耗时 | 输出字节 |
| --- | ---: | ---: |
| 当前 WebP Q86 / method4 | 1010.923ms | 1,131,176 |
| JPEG Q85 | 21.399ms | 1,420,240 |
| JPEG Q85 / optimize | 50.722ms | 1,407,418 |
| WebP Q86 / method0 | 224.499ms | 1,590,184 |

这一样本中，当前 WebP 比普通 JPEG 的编码耗时约 47.2 倍，比 optimize JPEG 约 19.9 倍；相对普通 JPEG 输出体积少约 20.4%。这是同像素、不同编码配置的局部成本比较，并非等主观画质比较，也不是完整流水线加速倍数。不能将约一秒 CPU 编码直接乘 2000 当墙钟耗时，因为实际存在并发、NAS I/O、缓存与其他阶段。

另做了本地 3000×2000 合成夹具的 420/1800/请求4096 编码微基准；请求4096的实际图像仍为3000px，没有放大冒充4096实拍。它仅作交叉检查，正式结论优先引用上述真实内容样本。

## 优化优先级与后续验收

1. 增加「嵌入预览快速模式」：优先保留/缓存可用的相机 JPEG 压缩数据，先达到可浏览；视口清晰图和 4096 按需或低优先级生成。已有 WebP 与原片保护继续兼容。RAW/JPG 配对时比较内嵌 JPEG 与独立 JPG 的实际 I/O 成本，不默认永远读大 JPG。
2. 调整队列范围与调度：当前批次、当前目录、用户正在浏览的素材优先；不同根位置公平领取，避免全库 420 阶段永久挡住某目录的大图。进度区分缩略图就绪、清晰图就绪和全库后台任务，不将十多万条历史积压混成用户所选2000张的生成进度。
3. 分离 NAS I/O、GPU 与编码并发，用小规模实测选择并发；复用一次提取的 JPEG 数据或有界中间结果，减少阶段间重新打开容器。不能仅继续增加 CPU worker。
4. 复核既有任务的路径排除规则与有效性，隔离坏视频重试；扫描 epoch 校验需兼顾仍可离线浏览的历史有效缓存，不直接删除全部旧 epoch 资料。
5. 提供速度与空间策略选择：JPEG/快速 WebP 先完成，空闲时再评估紧凑压缩；以真实照片质量验证后选择参数，不将微基准中的 JPEG Q85 直接认定为与 WebP Q86 等画质。

后续速度验收必须固定同一 NAS、同一批 2000 张、相同配对规则与预览尺寸，并记录冷/热缓存条件、图片/视频组成。分开记录扫描/元数据、提取、解码缩放、编码、写盘和排队等待，报告可浏览完成时间与清晰图完成时间。用户的十分钟参考相当于约 3.33 张/秒的整批吞吐目标；本轮未声称当前实现或建议方案已经达到该目标。

## 证据文件与交付边界

- REA 和辅助静态证据：`build/lightroom-preview-analysis/`（忽略的本机分析产物）。
- `rea-*-resources.json`：保留完整 REA Evidence，包含资源范围与原文件哈希；`rea-librarytoolkit-symbols.json` 保留符号表查询结果。
- `selected-lua-strings.json`：按 REA 资源位置读取的相关 Lua 字符串和原文件偏移，辅助证据。
- `AGPREVIEWSTRATEGIES.LUA.instructions.json`、`PREVIEWBUILDER.LUA.instructions.json`、`IMPORTPREVIEWHANDLING.LUA.instructions.json`：只读 Lua 字节码解析结果，含父 Evidence ID 与资源范围。
- `local-saved-state.json`：采样时间、本地数据库只读统计、进程/端口核对与当前预算计算。
- `local-real-content-encoding-microbenchmark.json`：真实缓存内容编码、原缓存哈希与本地索引出处。
- `local-encoding-microbenchmark.json`：合成夹具编码交叉检查。

本轮未修改运行配置、数据库、Adobe 文件、正式缓存或产品代码，未运行业务回归测试或重建安装包。验证范围是取证、只读统计和编码微基准；没有进行2000张NAS端到端测速。当天 Git 同步标记已为 `2026-10-26`，按仓库偏好只做本地文档提交，不自动 fetch/pull/push。
