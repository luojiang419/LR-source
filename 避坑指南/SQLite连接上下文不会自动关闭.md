# Windows 下 SQLite 连接未关闭导致文件被占用

表现：测试中的临时 SQLite 文件在清理时出现 `WinError 32`；大量短请求也可能留下文件句柄。

根因：`with sqlite3.connect(...) as con` 只管理提交和回滚，不会自动调用 `con.close()`。

解决：通过 `contextlib.contextmanager` 包装连接，在事务上下文外的 `finally` 显式关闭；每个请求或后台任务使用自己的连接。

验证：`python -X utf8 -m unittest discover -s 选片助手 -p "test_*.py" -v`，17 项测试通过，临时数据库均可正常清理。实现见 `选片助手/ai_service.py` 的 `connect()`。
