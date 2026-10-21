# GLM-OCR 格式参数与文本置信度

## 表现与根因

本机 LM Studio 已加载 `glm-ocr`，`/v1/models` 成功，但图片识别返回 HTTP 400：`response_format.type` 仅支持 `json_schema` 或 `text`，不能接收 `json_object`。去掉格式参数后正常返回图片全文，但可能只有 `text`，没有提示词要求的 codes/confidence/is_label 字段。

## 处理

`sku_vision.py` 尊重模型卡片的 JSON 模式开关；明确的格式参数 400 才移除参数重试一次，鉴权失败及无关 400 不重试。纯文本或只有 text 的 JSON 保留原文、提取可读候选，缺少 confidence 时标记 unscored，不能伪造高置信度自动记账。

资产备案的手动解析只填入可编辑草稿，多款号供点选，核对后仍走预览和确认写入。请求期间手动改字或切换图片时，旧结果不得覆盖新输入。

## 验证

`test_sku_vision.py` 验证参数降级、关闭 JSON、鉴权和无关 400；`test_asset_ocr.py` 验证草稿填入与异步保护。真实 `XSY_5232` 标签和发行引擎已调用本机模型读出 `LCJ81166`。该手写读数未获得模型置信度，不能据此宣布精确款号或已拍摄。
