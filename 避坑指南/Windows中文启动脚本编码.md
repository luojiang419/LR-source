# Windows 中文启动脚本的编码

## 表现与根因

双击 CMD 后闪退，或 Windows PowerShell 5.1 报 ParserError、Unexpected token，错误行中的中文乱码。UTF-8 无 BOM 的 `.ps1` 在 Windows PowerShell 5.1 中可能按系统 ANSI 代码页解析；UTF-8 的 `.cmd` 在未声明代码页时也可能把中文路径读错。另一个独立问题是对 GUI 主程序指定 `-WindowStyle Hidden`，可能隐藏用户需要看到的窗口。

## 修复

- 含中文的 `.ps1` 保存为 UTF-8 BOM，文件前三字节为 `EF BB BF`。
- UTF-8 `.cmd` 使用 CRLF，在含中文的命令之前先执行 ASCII 命令 `chcp 65001 >nul`。
- 用户需要看到的桌面主程序正常启动；隐藏窗口参数只用于后台辅助进程。
- CMD 执行失败时暂停，让错误可读。

## 实际验证

2026-10-21，LR 内置模型版真实启动入口：修复前 Windows PowerShell 5.1 稳定复现 ParserError；修复后执行 `cmd /c .\启动内置模型版.cmd` 成功，真实桌面窗口可见、前端就绪 1604ms、引擎健康 busy=false。再次启动仍复用同一窗口。启动验证必须覆盖用户实际使用的 CMD/PowerShell 入口，不能用直接运行引擎的测试替代。
