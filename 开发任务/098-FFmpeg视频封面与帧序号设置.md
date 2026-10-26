# FFmpeg 视频封面与帧序号设置

[CODEX_LONG_TASK_CONTINUE_V3]

- 当前状态：全部完成，已更新现有安装版；真实 NAS 全量缩略图验证及安装版缓存覆盖验证通过。
- 下一步：用户重新打开 `dist/末帧选片/末帧选片.exe` 使用；也可在 `http://127.0.0.1:8786` 检查同一 NAS 素材与新设置。
- TODO：无开发遗留。
- 最近验证：75 项相关回归通过（60.253 秒）；视频 775/775、图片／RAW 1691/1691 实际解码成功；安装版 2466/2466 默认缩略图已就绪。安装版引擎 264 文件逐一校验，去除开发环境 ExifTool 覆盖后仍能新解码 NAS RAW／JPG／MOV。
- branch/commit：`codex/ffmpeg-video-posters`；实现与本文同次提交，基线 `dfa3790`，未推送。
- 阻塞：无。

## 方案与验收

- 独立运行时放 `.runtime/ffmpeg/`，部署脚本固定版本及 SHA-256，保留许可证和来源；安装版打包到 `engine/_internal/runtime/ffmpeg/`，统一发现二进制。
- FFmpeg 自动识别容器与编码，默认提取第 3 帧（用户从 1 计数），不播放整段视频，不改写原文件。小图与大图使用同一帧，复用现有受容量限制的预览缓存。
- 设置 → 预览显示增加“视频封面”，允许选择帧序号并保存；重启保持，改变帧数后更新 URL 与缓存标识，归档和共享分屏预览一致。
- 首次添加仍先索引文件名和属性，封面按需生成并有界预热，沿用主题 SVG 加载占位。短视频帧数不足时回退到可用帧；损坏或不支持素材明确失败，不把格式占位图长期缓存为成功封面。
- 使用生成的首两帧黑色、第三帧彩色视频验证精确取帧；检查 H.264／H.265／ProRes／DNxHR 等实际可编码样片、帧设置持久化、旧缓存不复用、失败可退出、设置页面和归档实际封面。

## NAS 实测与优化

- 用户明确授权使用已选择的真实 NAS 目录验证。读取 `dist/末帧选片/data/asset_catalog.sqlite3` 的一致性快照，实际素材仍来自 NAS，不复制原片、不写评级或改动原片；验证索引和缓存位于 `build/nas-video-validation/data/`。已选目录包含 775 视频、1691 图片／RAW，含 779 ARW 和 64 DNG。
- 第一次抽查 3.2 GB MOV 有 30 秒超时；限制 FFmpeg 探测为 1 MiB、分析 1 秒并跳过帧率探测后，该文件新缩略图约 1273 ms，另 5 个 1.4–1.6 GB 视频约 496–981 ms。正常取第 3 帧不遍历整段视频。
- 所有 775 个视频首轮生成成功，386.4 秒完成（2 并发），中位 805.8 ms、95 分位 2453.5 ms。全量缓存复测 71.1 秒，中位 178.9 ms，95 分位 269.3 ms；不同并发与浏览器后台负载不视作同一条件的性能对照。
- 打开视频复用已有缩略图，已实测约 84–94 ms 可见；视频大预览按视口与像素密度选取 1800–4096 尺寸。详细资料探测也限制读取，并允许重新探测此前缺失 ffprobe 时留下的空结果。
- RAW 首次预览原先反复启动 ExifTool，并分别读取方向、小图。现用 3 个可复用会话一次读取方向与内嵌小图，预览不展开 RAW 像素；常驻二进制协议已用真实 JPEG 字节往返测试，中文路径、方向、取消和中断恢复均覆盖。
- 安装目录已更新 `dist/末帧选片/engine/`；旧引擎备份在 `build/deployment-rollback/engine-before-video-posters/`，原安装清单同时保存并更新。验证服务暂在 `http://127.0.0.1:8786`，使用隔离业务数据，读取真实 NAS 素材。
- 两个原有参考库测试替身沿用旧扫描方法签名，导致测试后台线程 TypeError；已按当前 `expected_total`、目录进度参数修正替身，进度计数与中断重启的原验收断言保持，复测通过。

## 最终交付与性能边界

- 视频共 775 个，含最大约 3.2 GB MOV，逐个通过实际 FFmpeg 提帧及图片解码验证。图片／RAW 共 1691 张逐个通过实际预览读取及图片解码验证，全量图片阶段 926.2 秒，包含前期已有缓存和新解码，不称作纯冷缓存基准。未执行原片评级、元数据写回或原片复制。
- 缓存通过现有 `PreviewCache.publish` 登记 API 合并到真实安装版，保留已有缓存与未知文件；两轮分别新增 1716 和 565 份预览，零冲突。安装版对当前 2466 个素材逐一计算默认 420 缩略图身份并检查文件，2466 就绪、0 缺失；包括验证中的额外尺寸缓存，不把缓存文件数冒充素材数。
- 最终空闲状态的安装版引擎实际页面：视频列表查询中位 13.5 ms，归档首张可见 791.3 ms，双击后预览可见 154.7 ms，清晰封面 364.6 ms，下一视频清晰封面 54.4 ms，默认设置 3，页面脚本错误 0。该组使用已就绪缓存及新浏览器上下文；首次读取新素材仍受 NAS 带宽、码流和尺寸影响，不承诺首次解码瞬时完成。
- 75 项命令：`python -m unittest test_video_preview test_video_preview_browser test_exiftool_session test_raw_preview test_preview_cache test_asset_catalog test_shared_archive_index test_asset_viewer test_asset_catalog_browser test_progressive_archive_browser test_reference_library test_reference_cache`。另一个旧 `test_workbench_settings` 沿用已废弃的 `embedded` 单策略和 `sku-panel-scope` 控件断言，本轮未扩展修改无关工作流；新视频设置由专门的浏览器测试覆盖保存、重启、四主题和窄屏。
- 已确认安装版使用 `dist/末帧选片/engine/_internal/runtime/ffmpeg/` 内的 FFmpeg／ffprobe，而非系统 ImageMagick 旧版本；RAW 不依赖 `FRAMEKEEP_EXIFTOOL` 开发环境覆盖，能自动从 `FRAMEKEEP_APP_ROOT/runtime/exiftool` 发现现有组件。
- 验证结果与截图：`build/nas-video-validation/all-video-first-generation-result.json`、`all-video-result.json`、`all-image-result.json`、`browser-benchmark.json`、`installed-cache-coverage.json`、`cache-merge-result.json`；合成片及设置截图在 `build/video-preview/`。验证服务使用安装版实际引擎、隔离业务索引快照和真实 NAS 原文件，保留运行供检查；未替换用户业务数据库。
- 实际解码已覆盖 H.264、HEVC、ProRes、DNxHR（MXF）。BRAW 等专有格式只保证索引收录，不保证 FFmpeg 能解码；失败会停止加载并明确提示，不生成持久的假成功占位封面。
