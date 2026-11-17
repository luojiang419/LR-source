# 演示协作规则

- 开发者负责启动本地服务并在可用浏览器中打开预览；能够自行启动时，不把启动任务交给用户。
- 大幅改变视觉之前，若视觉来源不清晰或已偏离当前目标，使用 Product Design 的 get-context 工作流。用户给出持久设计偏好后记录在本文件。
- 实现以选定设计图为准，覆盖布局、控件结构、密度、间距、色彩、字体、可见内容与层级。
- UI 代码位于 src/。保留 .openai/hosting.json、worker/index.js、scripts/prepare-sites-build.mjs、tests/sites-worker.test.mjs。只有在交接 Sites 托管时运行 npm run build 和 npm run test:sites，并检查对应构建产物。

## 已确认设计方向

- 用户选择第 2 版“轻量双区”，视觉基准为 design/selected-reference.png。
- 独立的以图搜图弹窗演示；不把模型配置、多维筛选、元数据详情或任务状态铺回弹窗。
- 默认象牙白磨砂主题，左侧窄参考图与更换/粘贴，右侧三列结果。筛选按需弹出，大图预览独立覆盖。
- 当前仅使用示例图库；上传图片在浏览器本地按配色排序，不声称调用正式 CLIP 检索，不访问生产数据库。
- 用户已允许用本机 Playwright 验证演示；内置浏览器接口连续两次未能分派请求。
