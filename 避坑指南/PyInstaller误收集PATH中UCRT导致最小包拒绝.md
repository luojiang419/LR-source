# PyInstaller 误收集 PATH 中 UCRT 导致最小包拒绝

- 表现：`build_minimal_update.py` 报“本次最小包出现运行时或模型变化”，差异全部是 `engine/_internal` 与 `update/_internal` 下的 `api-ms-win-*.dll` 和 `ucrtbase.dll`（80 个）。
- 根因：PyInstaller 按 PATH 查找 UCRT 转发库，本机 PATH 中的 `C:\Program Files\RedHat\java-17-openjdk-*\bin` 自带另一版本 UCRT，被收集进构建，哈希与签名完整基线不同。
- 解决：确认差异仅为上述 UCRT 文件后，从签名完整基线 `build/updates/1.0.1-20261116021119/SHIYING-Windows-x64.zip` 读取相同路径文件覆盖暂存构建（逐个校验 size/sha256），再运行最小包构建与全部验收。已安装客户端本来就使用基线副本，验收的就是实际运行组合。也可在构建前从 PATH 移除 JDK `bin`。
- 验证：1.0.5 发布中恢复 80 个文件后最小包构建通过；7 个历史基线冻结 worker 更新、回滚和 730 个运行库保持通过，公网旧客户端下载验哈希通过。记录见 `build/release-1.0.5/ucrt-restore.json`。
