# HTTPServer 绑定端口时反向 DNS 拖慢启动

## 表现
桌面版启动后长时间停在“intelligence ready”等阶段，功能迟迟就绪。`engine-startup-stacks.log` 中多个进程的主线程栈停在：
`socketserver.__init__ → http/server.py server_bind → socket.getfqdn`，调用方为 `InventoryService`、`LanCaptureSync`、`LanWorkspace/reference_api` 的局域网服务（绑定 `0.0.0.0`），单次可超过 15 秒。

## 根因
标准库 `http.server.HTTPServer.server_bind` 在绑定后执行 `socket.getfqdn(host)` 计算 `server_name`。部分 Windows 网络环境（DNS/NetBIOS 超时、断网、多网卡）下反向解析会阻塞。初始化在主线程串行进行，任何一个局域网服务绑定被阻塞都会推迟 `http ready`。本机网络正常时该调用只需毫秒，问题随网络环境间歇出现，容易被误判为数据库或磁盘慢。

## 解决
`选片助手/fast_http_bind.py` 在进程内替换 `HTTPServer.server_bind`，只做 TCP 绑定，并将 `server_name` 设为绑定主机字符串；`server.py` 最早导入它，同进程所有 `HTTPServer` 子类生效。本项目没有用 `server_name` 生成地址。

## 验证
`test_archive_preview_perf.FastBindTests`：将 `socket.getfqdn` 打桩为睡眠 5 秒，创建服务耗时小于 1 秒且未调用该函数。
