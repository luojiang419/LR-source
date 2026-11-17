# 安装器 onefile 运行库解压失败与全量包

## 表现与边界

客户 `updater.log` 在业务 Python 启动前两次报 `Failed to extract python312.dll: decompression resulted in return code -3`。收到的 EXE 与发布 SHA-256 一致，内嵌 worker 和 DLL 在本机可完整提取、启动。因此只能定位到客户机 onefile 解压阶段，不能据此认定缺 Python、传输损坏、安全软件或硬件故障。

## 处理

- 更新事务改为 PyInstaller onedir，Python DLL 单独加载；原生单个 EXE 内置 ZIP_STORED 文件载荷，使用 .NET 流式复制与 SHA-256 校验后再启动 worker，不调用 onefile 自解压。
- 安装器和卸载器包含完整运行时；软件内更新先校验并复制 worker 与 `_internal` 到独立会话，避免替换正在使用的安装目录运行库。保留旧单 worker 交接兼容。
- 全量单 EXE 另含签名软件包、Medium/Small OCR、WebView2 和必要驱动/VC 依赖，排除 Qwen/GLM 权重。内部载荷与签名软件包不同，前者由原生 SHA 校验，后者仍执行 Ed25519 和逐文件验证。
- 包含内置包不代表端到端安装已在客户机成功。使用 `--verify-runtime` / `--verify-payload` 可不安装软件地诊断真实 EXE；具体客户机仍需实装确认。

## 验证

2026-11-09：71 项安装更新回归通过；真实 EXE 的 ASCII/中文空格路径启动、损坏 DLL 缓存修复、损坏归档启动前阻断通过；746,219,008 字节全量 EXE 的 75 个运行文件、19 个载荷文件校验通过，禁用网络下载仍可准备 OCR/WebView2/驱动组件；签名软件包 936 文件安装事务和新装/旧 GLM 迁移识别通过。永久记录见 `技术文档/验证记录/196-全量安装器与运行时修复-20261109.json`。
