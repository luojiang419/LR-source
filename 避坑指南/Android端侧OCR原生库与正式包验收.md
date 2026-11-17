# Android 端侧 OCR 原生库与正式包验收

## OpenCV 在小米 9 无法加载

- 表现：PaddleOCR SDK 调用返回初始化失败，展开原始链接异常为 `cannot locate symbol "__sfp_handle_exceptions"`，来自 QuickBird `opencv:4.5.3` 的 `libopencv_java4.so`。
- 根因：旧第三方预编译库引用了当前 Android 系统不提供的原生符号，与 OCR 权重是否存在无关。
- 解决：改用 OpenCV 官方 Maven 包 `org.opencv:opencv:4.11.0`，保持上游图像处理 API；保留原始 `UnsatisfiedLinkError` 便于定位。推理失败可回退，但验证时必须检查记录的实际 `source`，不能把回退成功当作新模型正常。
- 验证：MI 9 / Android 11，PP-OCRv6 small/tiny/medium 和 v5 mobile 四个检测、识别组合共 40 次推理完成，含真实标签及手写参考图。

## 正式旧 APK 与新测试 APK 的 R8 映射不一致

- 表现：用新 release 的 AndroidJUnitRunner 测试 APK 访问旧正式包时，在 `AppComponentFactoryRegistry` 抛出新映射类名的 `NoClassDefFoundError`，测试未进入业务代码；应用本身仍能正常启动。
- 根因：测试 runner 的 AndroidX 实现引用了新目标包的混淆类，旧 APK 不包含相同映射。
- 解决：更新前备份使用只依赖 Android 框架、JSON 和 SQLite 的自定义 `Instrumentation`，不引用目标应用的实现类；保持同签名和固定目标包。`app/src/androidTest/java/com/framekeep/inventory/InventoryDeviceHarness.java` 实现只备份/检查，以及清理明确列出的本轮新增 id，禁止删除测试前已有 id。
- 构建时设置 `-PinventoryTestRunner=com.framekeep.inventory.InventoryDeviceHarness -PinventoryTestBuildType=release`。仅改 androidTest Manifest 中的 runner 名称会被 AGP 的 `testInstrumentationRunner` 覆盖。
- 验证：在旧正式包更新前成功备份账本与图片，ZIP CRC 校验通过，未卸载正式包。

## Android 11 与 JVM / Android 14 测试 API 不同

- 表现：原先在 API 34 通过的 ZIP 内容测试，在手机 API 30 抛出 `InputStream.readAllBytes()` 的 `NoSuchMethodError`。
- 解决：测试中也使用缓冲读取循环。不要和已支持的 `Files.readAllBytes(Path)` 混淆。
- 验证：API 30 手机专项复验通过。应以手机实际 API 执行为准，不能仅根据 Java 17 编译通过推断 Android 运行支持。
