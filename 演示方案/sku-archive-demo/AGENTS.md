# Prototype Instructions

用户已确认的界面偏好：所有归档步骤直接展示配置、统计、预览或执行状态，不显示步骤下方重复的英文引导标签、宣传式主标题及其说明段落；A/B 布局一致遵循。完成、部分归档、暂停和失败等必要状态在结果卡片内表达。

步骤导航与四项统计统一并入顶部标题栏，所有步骤复用同一顶栏；正文不再显示独立步骤条或大块统计卡片。桌面宽度单行展示，窄窗口仅在顶栏内紧凑换行，保留全部步骤和统计。

最终大档目录的完整文件清单是归档和校验的权威基准。现场存在但最终目录已没有的文件视为后期已删除/不选用，自动跳过，不计待处理、不计遗漏、不导致部分归档；仅在最终目录仍存在但匹配不明确的文件需要复核。已删除现场记录单列审计，禁止伪造目标路径或计入预期归档文件。

跨格式匹配使用同一次快门的元数据身份指纹（相机型号/序列号、原始拍摄时间与子秒、原始照片编号等），唯一对应自动通过。禁止用文件内容哈希或视觉相似度作为此处匹配依据，禁止预设“连拍必须人工确认”。文件哈希仅用于复制阶段完整性校验；真正缺字段或身份记录冲突才复核。

面向仓管的校验报告导出 `.xlsx`：必含母SKU、子SKU、存储路径、文件缩略图、归档日期、归档前路径、归档后路径。缩略图嵌入并按行绑定，路径完整保存；归档日期来自实际作业记录，未知的原归档日期留空，不能以导出时间补造。废片单独追溯，失败项不把计划目标当作已归档路径。演示表必须标明模拟数据和样例缩略图；正式报表不得以别张图片代替未获取的缩略图。

“归档前处理”默认勾选。开启时只整理本次匹配的大小档，XMP随RAW移动并同步数据库路径，再拷贝归档；已整理母子SKU层级直接复用，选子SKU、母SKU或总目录均递归识别，不重复嵌套；关闭时来源目录保持不变。预览显示两种来源的原路径和整理后路径，报告保留最初来源并注明整理后的实际位置。大小档原片内容均保持。仓管Excel的表头及每个数据单元格必须有可见的横纵细边框，包含删除记录表，便于屏幕与打印逐项核对。

二星标签图以最终大档为准：最终目录存在则随款归档，不存在则不从小档补充。小档只提供指纹与SKU依据，不扩大最终归档范围。报告沿用“文件缩略图”列，图片类型标注“二星标签图”。用户已取消独立“标签图片”列以及“SKU预览图”改名，不再实现同SKU并排标签预览列。

导出文件名统一为“归档校验报表-YYYYMMDDHHmm.xlsx”，使用导出时的Asia/Shanghai年月日时分，不含秒，也不以归档完成时间替代导出时间；前端下载、服务响应和真实验证脚本遵循同一规则。

Run the local server yourself and open the preview in the browser available to this environment. Do not give the user server-start instructions when you can run it.

Before making substantial visual changes, use the Product Design plugin's `get-context` skill when the visual source is unclear or no longer matches the current goal. When the user gives durable prototype-specific design feedback, preferences, or decisions, record them in `AGENTS.md`.

When implementing from a selected generated mock, treat that image as the source of truth for layout, component anatomy, density, spacing, color, typography, visible content, and hierarchy.

Build app UI in `src/`. Keep `.openai/hosting.json`, `worker/index.js`, `scripts/prepare-sites-build.mjs`, and `tests/sites-worker.test.mjs` intact so the same local prototype can be handed to Sites. Before a Sites handoff, run `npm run build` and `npm run test:sites`; the build must leave `dist/client/index.html`, `dist/server/index.js`, and `dist/.openai/hosting.json`.
