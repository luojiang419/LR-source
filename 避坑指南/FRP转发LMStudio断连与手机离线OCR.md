# FRP 转发 LM Studio 断连与手机离线 OCR

## 表现

手机 OCR 指定公网 HTTP 地址；电脑请求 `/v1/models`，直连 `RemoteDisconnected`，经过 Windows 系统代理返回 502，尝试 HTTPS 出现 EOF。FRP 客户端本身仍显示隧道启动成功。

## 已验证根因

2026-11-05 的 FRP 本机日志多次记录 `connect to local service [127.0.0.1:1234] ... actively refused`。隧道运行时，本地 LM Studio API 曾未监听该端口。恢复监听后，同一配置及原密钥访问 `/v1/models` 成功，真实标签 `LC7877529-P105` 经 `/v1/chat/completions` 约 1.53 秒正确识别。不能将代理返回的 502 直接归因为远程服务器故障，也不能把 HTTP 隧道改为 HTTPS 而忽略其实际传输协议。

## 排查与解决

1. 读取用户指定文件，密钥仅进请求 Header，不输出。
2. 分别检查直连和系统代理影响。Windows Python `urllib` 可能自动读取系统代理，即使环境变量没有 `HTTP_PROXY`。
3. 通过 `lms server status`、`lms ps` 和监听端口核对本地服务，再查 FRP 对应隧道的本地服务连接日志；不要修改其他隧道。
4. 本地接口已监听并加载指定模型后，以原公网配置执行真实图片请求。
5. APP 提供健康检测、短暂断线重试与随包离线 OCR。模型名称存在不等于推理通过，仍需检查真实标签结果。

## 安卓实测

小米 9 使用 APK 内置 ML Kit 模型、关闭云识别后，真实 SKU 标签正确读为 `LC7877529-P105`。双图入库、多个候选人工选择、可编辑确认出库均通过；实际手机导出 XLSX 回读为 2 条记录、4 张内嵌图片。详细证据在 `build/inventory-verification/`，业务开发进度见任务 185。

MIUI USB 安装受限时，用户授权改用文件管理器打开 APK，通过正常系统安装流程完成；没有修改设备安装安全开关。
