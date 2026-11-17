# 电脑端 CPU OCR 集成与速度实测

[CODEX_LONG_TASK_CONTINUE_V3]

- 当前状态：电脑内置 OCR 已统一切换 Medium，旧 GLM 配置迁移完成，魔搭正式发布及 latest 激活完成，临时验证副本已清理。
- 下一步：现用软件中检查更新，或下载下方新版安装器；现场电脑实际验收由后续使用覆盖，本轮未操作现场电脑。
- TODO：本轮开发和发布无剩余。
- 最近验证：本轮 46 项 OCR/提供方、3 项浏览器/升级、67 项安装更新测试共 116 项通过；真实签名包 861 文件校验与隔离替换通过；新装/旧 GLM 配置识别、公网无开发种子下载 Medium 并识别通过；远端模型、ZIP、安装器、latest 均匿名回读并校验。
- branch/commit：`feat/desktop-paddle-ocr`；实现 `14315c0`，干净发布快照 `5f6ed63`（`build/medium-release-source`），releaseId `20261109015625`，收尾见本分支发布记录提交。当日已检查，Git 仅本地提交未推送；魔搭发布已成功。
- 阻塞：无本轮阻塞。两项原有全图任务/资产浏览测试失败，使用修改前 OCR 模块复现；不扩大修改无关业务。已有手机、库存、前端、拍摄等未提交修改保持，不纳入提交。

## 目标与验收

- 用官方 Small、Medium 权重在 Windows ONNX Runtime CPU 上运行，分别记录首次加载、每张检测/识别、端到端耗时与内存，不使用 GPU。
- 与当前桌面 GLM-OCR 使用相同图像、输入哈希、输出原文与款号比对；预期款号只在推理后评分，不进入模型。
- 模型会话复用、后台预加载，沿用现有 OCR 结果字段、进度和队列；保留 GLM 与外部配置。
- 确认实际运行与必要回归通过，输出可复算报告和局限；仅以本轮样本结论选型。
- 本轮追加：电脑内置 OCR 统一切为 Medium，旧 GLM 本地卡片保持 id 迁移，保留外部 API 配置与用户数据；默认安装不下载 GLM 或 llama/CUDA 组件，已有模型文件不擅自删除。
- 发布从已提交源码的独立工作树构建，不将当前其它任务未提交代码混入更新；魔搭上传与 latest 激活按原签名/哈希校验流程执行。

## 实测结论

Windows x64、i7-14700K、ONNX Runtime 1.27.0；明确只选 `CPUExecutionProvider`。现场机器由用户提供：32GB 内存、12700 / 13900H，按用户补充的 8 核/16 核考虑，但没有远程操作现场机。

| 模型 | 线程 | 连续中位耗时 | P95 | 首次请求 | 整进程峰值工作集 | 可评分款号正确 |
| --- | --- | --- | --- | --- | --- | --- |
| Small | 4 | 197ms | 234ms | 809ms | 522MiB | 9/11 |
| Small | 8 | 189ms | 211ms | 747ms | 524MiB | 9/11 |
| Medium | 4 | 854ms | 1005ms | 1609ms | 930MiB | 10/11 |
| Medium | 8 | 747ms | 896ms | 1588ms | 931MiB | 10/11 |

- 12 张独立真实标签、每模型/线程配置 3 轮（36 次）。输入均为同一组 1600px 预览，先读入字节后计时，包含图片解码、检测、识别和业务候选提取，不包含 NAS、模型下载及元数据写入。
- 9 张印刷款号两模型均正确；2 张可确定手写，Medium 1/2、Small 0/2。`XSY_5988` 的业务库 `LC7877843-P1` 与视觉判读 `LC787843-P1` 不一致，连笔不能可靠判断字符数，排除精度评分但保留计时。不能将业务库内容当作模型提示或强行修正原图。
- Medium 能读清 `LC7832773`，后缀 `-P805` 被检测为另一行。新增只合并独立连字符后缀的候选提取，保留原始全文与行坐标；不改写 C/0、I/1 等字符，不查询正确款号表。
- 每配置独立进程采集内存。表中为整个 Python+原生库+模型的峰值工作集；Medium 4 线程私有提交约 2066MiB，与权重大小、实际常驻内存不是同一指标。Small 权重约 31MB，Medium 约 139MB。
- 2 线程前期 Medium 中位约 1.27 秒；Small 检测+Medium 识别虽较快，对本组手写未改善。额外 PP-OCRv5 server 识别器更慢、错误未稳定减少，未加入产品候选。
- 识别预处理最终读取官方 `inference.yml` 的 BGR 顺序，并按官方桌面推理至少补齐 320 宽；没有直接照搬手机 SDK 硬编码 RGB。首次手机参数试跑/批量识别试跑不是最终配置，正式数据见上表。
- 选用 Medium 4 线程，在当前机器相比 8 线程只慢约 0.11 秒，保留拍摄/界面余量。无法承诺所有手写正确；CTC 行分数仍保留在辅助信息，业务置信保持未校准、需复核。

## 接入和复现

- 新增 `paddle_ocr.py`，会话由 `LocalInference` 统一管理，预加载与切换串行，退出释放；CPU 模型不要求 llama.cpp/CUDA，不发 HTTP 推理请求。
- 组件清单固定官方 BOS 归档及每文件 SHA-256，使用原组件管理器断点下载、校验、离线种子、按需准备；只读取归档中指定普通文件，不按归档路径解压。
- 保留原 GLM 默认与外部模型卡片。选择 CPU 模型：设置 → 模型配置 → 关闭统一“使用内置模型”开关 → 添加配置 → 类别 OCR、协议“软件内置模型” → 获取列表选择 `pp-ocrv6-medium-local`，保存并选用。关闭统一开关允许独立选择卡片，所选 CPU 模型本身仍离线运行。快速模型标识为 `pp-ocrv6-small-local`。
- `asset_analysis.test_provider` 对 CPU 模型直接检查加载结果，不向不存在的 llama HTTP 接口请求模型列表。
- 依赖：`onnxruntime` CPU、`opencv-python-headless`、`PyYAML`。打包 hook 排除可选 GPU provider，模型权重不进 Git；保留 PaddleOCR Apache-2.0 许可。

```powershell
python -X utf8 选片助手/benchmark_desktop_ocr.py --fixtures build/desktop-paddle-ocr/fixtures.json --out build/desktop-paddle-ocr/cpu-final --cpu --threads 4 8 --rounds 3
python -X utf8 选片助手/build_release.py --engine-only --engine-dist build/desktop-paddle-ocr/engine-dist
python -X utf8 tools/verify_cpu_ocr_package.py --engine build/desktop-paddle-ocr/engine-dist/framekeep-engine/framekeep-engine.exe --config build/desktop-paddle-ocr/cpu-final/data/components/local-models.json --image build/desktop-paddle-ocr/XSY_3471.jpg --out build/desktop-paddle-ocr/packaged-final --expected LC7832773-P805
```

冻结引擎实际启动后，经正常请求令牌验证，通过 `/api/providers/test` 加载 Medium，再经 `/api/sku/parse` 读出真实标签 `LC7832773-P805`，辅助信息确认 CPU，照片哈希未变，测试进程已结束。隔离资料中关闭公司自动入库规则，不写生产数据。前两次验证脚本分别补齐正常请求令牌与独立引擎的 ExifTool 路径后完成；不修改生产校验逻辑。

永久结果：`技术文档/验证记录/195-CPU-OCR实测-20261109.json`，含输入哈希、每次时间、首轮原文、内存和冻结验收。图片、模型和构建输出保留在忽略目录，本轮不用发行版本号递增。

验收后清理本轮临时基线、失败验证副本、废弃高分辨率试片、重复组件副本和未采用的 v5 server 试验权重，删除约 143.62MiB，G 盘实际可用增加约 143.93MiB；保留 Small/Medium 开发权重、最终基准、必要样片和唯一已验证冻结引擎。明细存入永久验证 JSON，不删除生产资料或其它任务产物。

## 已有测试失败的核对

- `test_full_auto_ocr.FullAutoWorkflowTests.test_enabling_full_auto_preserves_ordinary_manual_mode`：期望 matched，当前任务仍 queued。
- `test_asset_ocr.ManualOcrTests.test_asset_panel_fill_candidates_edit_and_photo_switch_races`：等待“未读出”消息超时。
- 将本次修改前的 `component_setup`、`local_inference`、`model_providers`、`sku_vision` 和清单读入独立基线目录，两用例仍以相同断言失败；本次不修无关业务。最终针对相关接口的 57 项回归通过。

## 默认替换和魔搭正式发布

- 版本保持 `1.0.0`，发布号更新为 `20261109015625`。魔搭更新仓库：<https://modelscope.cn/models/jiangjiang419/framekeep-updates>。
- 新安装/统一内置模式默认 Medium / CPU / 4 线程；GLM 本地卡片原地迁移并保留 id、选用及用途绑定。旧客户端直接请求本地 GLM 标识也转到 Medium，不再启动 GLM。外部 API 配置和密钥不变，旧模型文件不自动删除。
- `DEFAULT_MODEL_IDS` 只准备 Medium；GLM 权重已从桌面组件清单移除，OCR 不引入 llama/CUDA。Qwen 运行时仍按需保留。安装文字、OCR 预置按钮、模型列表及开发部署入口同步更新。
- Small/Medium 四份模型归档在魔搭以固定 SHA-256 内容寻址发布。没有开发种子的真实下载测试只准备 Medium，下载和首次识别共约 6.94 秒，未生成 GLM 或 llama runtime 目录。
- 发布来自 `5f6ed63` 干净工作树；原有 USB、库存、拍摄等未提交代码没有进入发布包。Rust 桌面、冻结引擎、原生安装更新工具均重新构建；现用安装版保持运行，没有替换或关闭。
- ZIP 为 327,266,044 字节（约 327.27MB），861 文件、解压约 816.79MB；SHA-256：`f5c23acddaa7a7bd075755c6191f549e6229f41745f8915ec1a9b7ef58b9ecca`。安装器 14,157,824 字节。程序包不包含 OCR 权重，按需从魔搭准备。
- [新版轻量安装器](https://modelscope.cn/api/v1/models/jiangjiang419/framekeep-updates/repo?Revision=master&FilePath=releases%2F1.0.0%2F20261109015625%2FSHIYING-Setup.exe)。签名 ZIP、安装器、公钥校验保持原发布机制；latest 激活后再次独立匿名验证签名、发布号和包哈希一致。
- 真实签名包隔离安装后，新装和旧 GLM 升级场景都通过 HTTP OCR 业务接口读出 `LC7832773-P805`（分别约 1.057/1.053 秒），输入哈希保持，旧测试权重保留，CPU backend 与 GPU provider 缺席均通过。原生桌面编译成功，但为了保留现用单实例窗口，本轮没有另起同标识原生窗口；不将冻结引擎验证描述成完整原生窗口验收。
- 永久证据：`技术文档/验证记录/195-Medium默认替换与魔搭发布-20261109.json`，含签名清单、每文件哈希、安装器摘要、在线准备结果和 publication 回执。
- 发布后删除本轮隔离安装/识别副本、重复引擎及编译中间文件：逻辑约 5.21GiB，G 盘实际可用增加约 4.79GiB（包含硬链接故存在差异）。保留唯一完整发布目录、签名 ZIP、安装器、开发权重、源码和验证记录；用户资料及现用程序不动。

## 参考

- 手机模型和固定 SHA-256：`安卓出入库/tools/paddle-models.json`。
- 官方说明：<https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/algorithm/PP-OCRv6/PP-OCRv6.md>。
- 既有手机验收：`开发任务/190-手机端OCR模型择优部署与小米9验证.md`。
