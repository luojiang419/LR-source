# CPU OCR 预处理与运行库打包

## 表现与根因

- 复用手机 ONNX 权重不等于照搬手机预处理。现有手机 SDK 识别端硬编码 RGB、按实际裁剪宽度推理；官方本次 Small/Medium `inference.yml` 声明 BGR，桌面参考实现按至少 320 宽补零。颜色和补齐方式不一致会改变识别结果，尤其是手写与短文本。
- Python 安装环境里可能残留可选 CUDA/TensorRT DLL。即使只选择 CPU provider，PyInstaller 默认 hook 仍会收集它们，增加数百 MB 并提示缺少 NVIDIA 依赖。
- 原模型连接测试默认请求 llama.cpp 的 `/v1/models`；进程内 ONNX 模型没有这个 HTTP 服务，也没有临时 API key。

## 处理

- 从所选识别模型配置读取 `DecodeImage.img_mode`，使用官方桌面补齐方式；原始行文字和坐标保留，业务候选仅拼接独立连字符后缀，不纠正 C/0 等字符。手机端实现本次未改。
- 明确 `providers=['CPUExecutionProvider']`，打包通过专用 `hook-onnxruntime.py` 排除 `providers_cuda` / `providers_tensorrt`，保留 CPU 核心和 shared DLL。
- CPU 卡片的连接测试直接加载检测/识别会话，不请求 llama 服务；推理请求仍经过原并发锁与统一结果适配。

## 验证

2026-11-09，12 张真实标签、4 个 CPU 配置共 144 次连续推理；57 项回归通过。冻结引擎 HTTP 加载测试及手写 `LC7832773-P805` 识别通过，CPU backend、GPU provider 缺席、输入哈希不变均已检查。详见 `技术文档/验证记录/195-CPU-OCR实测-20261109.json`；样本中仍有手写误读，预处理对齐不等于所有场景准确率保证。
