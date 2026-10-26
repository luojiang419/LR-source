# Windows 下 SQLite 连接未关闭导致文件被占用

表现：测试中的临时 SQLite 文件在清理时出现 `WinError 32`；大量短请求也可能留下文件句柄。

根因：`with sqlite3.connect(...) as con` 只管理提交和回滚，不会自动调用 `con.close()`。

解决：通过 `contextlib.contextmanager` 包装连接，在事务上下文外的 `finally` 显式关闭；每个请求或后台任务使用自己的连接。

验证：`python -X utf8 -m unittest discover -s 选片助手 -p "test_*.py" -v`，17 项测试通过，临时数据库均可正常清理。实现见 `选片助手/ai_service.py` 的 `connect()`。

## 2026-10-23 冻结引擎大库验收

同一问题再次出现在验收脚本的父进程：向隔离库写入 50 万条合成元数据后，`with sqlite3.connect(...) as con` 提交了事务，变量仍持有连接；即使子引擎已经退出，临时数据库也不能删除。外部脚本改用 `with contextlib.closing(sqlite3.connect(...)) as con, con:`，完整复测退出 0。

原生索引的 WAL 生命周期连接有意保持打开，以减少批次之间的 checkpoint；它与短查询连接分开，关闭时先停止迁移、等写入任务退出，再关闭生命周期连接。对应实现为 `ReferenceCatalog.close()`、`ReferenceLibrary.close()`，验收脚本 `build/native-fast-index/verify_native_binary.py`，结果见任务 084。
