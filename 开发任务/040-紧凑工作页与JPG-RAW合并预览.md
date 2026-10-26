# 紧凑工作页与 JPG/RAW 合并预览

[CODEX_LONG_TASK_CONTINUE_V3]

- 当前状态：方向识别与旧缓存修复完成，普通／内置模型版已更新，桌面运行中。
- 下一步：用户在已更新桌面中检查归档、SKU 数据库和解析任务。
- TODO：无。
- 最近验证：方向相关 21 项测试通过；发行版真实 EXIF=8 照片网格 280×420、右栏 600×900 均正确竖向，原图 SHA-256 不变。
- branch/commit：`codex/compact-paired-photo-workspaces`，本日已完成 GitHub 检查，本轮只本地提交。
- 阻塞：无。

## 验收要求

- 底部导航上方不再显示本机状态条。
- 资产归档、SKU 数据库、解析任务标题显示在应用最顶栏，原位置不再重复大标题。
- 已选择照片的右栏预览紧贴右栏标题，不保留空提示占位。
- 同目录同名 JPG/RAW 只显示一个拍摄项，数量、分页、SKU 归组保持一致；源文件和原索引记录保留，人工确认同步配对项。
- 按用户最新反馈，大图保证清晰度：单张、监控和对比使用 1800px 预览，右栏 900px，网格按需使用缩略图；保留配对图右下角半透明 J/R。隔离测试不修改用户原片。

## 实现与验证

- `asset_catalog.py` 增加可迁移、带索引的同目录同名配对键与拍摄项视图，不删除原文件索引。目录、统计、分页、SKU 分组统一读取拍摄项；已有人工修正优先保留，人工确认同时更新配对记录并分别记录审计。
- 缩略图优先读取配对 JPG，使用已有媒体引擎在 JPEG 解码阶段缩小；只有单独 RAW 才提取内嵌预览，不完整解码。归档网格请求 320px，右栏最大 480px；选片网格、列表、单张与对比统一使用既有 480px 小图。
- `asset-library.js` 与 `app.js` 在配对图右下角显示半透明 J/R；`asset-workspace.css` 修复右栏隐藏提示被 display:grid 覆盖的问题，移除底部状态条；`workbench-ui.js` 将三页标题及操作移入应用顶栏，保持底部导航贴底。
- `test_asset_catalog`、`test_asset_catalog_browser` 共 6 项通过，含配对计数／分页／人工同步／重扫／单文件离线及小预览验证；浏览器检查四主题和三种桌面尺寸。`test_bottom_tabs_browser`、`test_parsing_tasks`、`test_raw_preview` 共 20 项通过；`test_preview_size_browser` 1 项通过，并断言所有图片请求为 480px、无完整解码请求。三个 JS 文件语法检查及 diff 空白检查通过。
- Python 引擎已重新构建并同步 `dist/末帧选片/engine` 与 `dist/末帧选片-内置模型/engine`；本次四个前端资源 SHA-256 与源码一致。桌面经正常关闭后重启，未强杀进程，未修改正式版本号。
- 部署前在线备份归档 SQLite 到 `build/compact-paired-workspaces/catalog-before.sqlite3`。正式库仍有全部 729 条文件索引，逐条比对 id、path、relative、sku、normalized、source、confidence 一致；界面合并为 425 个拍摄项。真实右栏预览间距 12px、图像宽 480px，J/R 与三页顶栏均正常，无页面错误。
- 已查看隔离截图及正式图库截图；实际验收证据：`build/compact-paired-workspaces/deployed-result.json`、`deployed-archive.png`。发行桌面进程保持运行。工作区原有未提交修改均保留，不纳入本次提交；今日不重复联网或推送。

## 2026-10-21 大图清晰度修正

- 用户反馈单张监控大图模糊，根因是上一轮让单张与对比也直接使用 480px 缩略图，放大后丢失细节。
- 恢复单张／监控／对比的 `previewLarge`（1800px），右栏恢复 900px，归档网格恢复 420px，后端预览上限恢复 1200px。大图优先配对 JPG 或 RAW 内嵌大预览，保留原配对与 J/R 标记。
- 索引及归档浏览器 6 项、实际缩放浏览器 1 项通过；验证单张请求 1800px，实际图像长边大于 480px，无完整 RAW 解码请求。
- 已重新构建并同步普通／内置模型版引擎，桌面正常关闭并重启。真实发行版请求 `size=1800`，实际图像 1200×1800，无页面脚本错误；截图 `build/compact-paired-workspaces/deployed-clear-preview.png` 已查看，证据 `clear-preview-result.json`。本轮提交见 `fix: restore sharp previews in single and comparison views`。

## 2026-10-21 图片方向与旧缩略图缓存

- 用户截图中归档网格横向、右侧竖向，直接读取同一原片 `XSY_5442.JPG` 为 2400×1600、EXIF=8；当前后端 420px 和 900px 均已正确生成竖图。原因是归档预览接口为一年 immutable 缓存，但地址没有文件指纹和处理版本，浏览器继续使用早期未旋转的缩略图。
- 归档与 SKU 图片地址统一加入文件指纹及方向处理版本，前端通过 URLSearchParams 设置尺寸，保留缓存版本。源文件变动与方向处理升级都可切换地址，无需清理用户浏览器缓存。
- 补充单独 RAW 的 Orientation 元数据读取与文件版本缓存，网格和右栏共用方向，不解码完整 RAW；已有 JPEG EXIF 1–8 处理和防重复旋转规则沿用媒体引擎。
- `test_asset_catalog`、`test_asset_catalog_browser`、`test_raw_preview` 与 `test_live_layout.OrientationTests` 共 21 项通过，JS 语法及 diff 检查通过。重新构建并同步两个发行目录，实际运行验证 `XSY_5442.JPG` 的网格 280×420、右栏 600×900，两个地址均包含相同文件版本，无脚本错误；原图 SHA-256 不变。截图 `build/compact-paired-workspaces/deployed-orientation.png` 已查看，证据 `orientation-result.json`。本轮提交见 `fix: honor photo orientation and invalidate stale preview URLs`。
