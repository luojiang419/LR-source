# PaddleOCR Android SDK 来源

- 上游：<https://github.com/PaddlePaddle/PaddleOCR/tree/dab3fe35379033fdcb2d0e9572fac0b36c9a9ebf/deploy/ppocr-android/ppocr-sdk>
- 固定 commit：`dab3fe35379033fdcb2d0e9572fac0b36c9a9ebf`。
- 许可：Apache-2.0，见同目录 `LICENSE`；源码版权头保留。
- 本地适配：Groovy 构建配置与 ASCII 产物路径；推理模型/YAML 支持应用私有目录中的绝对路径供真机统一对比；ONNX 会话使用文件路径避免 Java 堆复制大权重，随安装版本管理模型缓存；同步 Java 桥接在后台执行并限制两线程、结束释放会话。其余检测、识别、文本框后处理与解码沿用上游。
- ONNX Runtime 1.21.1 沿用上游。上游 QuickBird OpenCV 4.5.3 在小米 9 / Android 11 实测缺少 `__sfp_handle_exceptions` 而无法加载，改用 OpenCV 官方 Maven 包 `org.opencv:opencv:4.11.0`。
- 模型从 PaddlePaddle 官方 BOS 下载，校验值见 `../tools/paddle-models.json`。
