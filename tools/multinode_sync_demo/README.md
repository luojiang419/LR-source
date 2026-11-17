# 异地多机同步隔离演示

此目录实现技术概念验证，不自动接入已安装程序。A/B/C 只收到 JPG，办公室角色 O 连接独立的最终归档副本；角色授权来自配置，不固定机器字母。

在仓库根目录执行：

```powershell
python -X utf8 tools/multinode_sync_demo/verify.py
```

或双击仓库根目录 `验证多机同步演示.cmd`。脚本新建递增运行目录，不覆盖之前的验收数据；成功后生成独立 HTML 报告。输入默认沿用项目已知真实拍摄目录，也可使用 `--source` 指向包含相同样片名称的授权目录。

每轮结果在 `build/multinode-sync-demo/run-*/`，`latest.json` 指向最近一次成功结果。包含四端 `sku.sqlite3 / asset_archive.sqlite3`、独立增量检索库、实际日期 Excel、模拟 NAS、真实来源与哈希清单、原始证据 JSON、性能数据和可离线查看的 HTML。

运行目录中的 `.local-credentials.json` 仅用于恢复本轮隔离环境，不进入报告或 Git；其存储不是生产凭据保护方案。实际图片与所有数据库均位于 Git 忽略的 `build/`。重跑需要 Python 项目依赖、ExifTool、rawpy、Pillow、openpyxl、Playwright/浏览器（报告界面验收时）。

代码分工：

- `core.py`：固定事件协议、设备鉴权、持久中继、四端业务投影、角色限定的 JPG→RAW 绑定及受控写回。
- `search_index.py`：独立增量索引与本机搜索 HTTP 验证入口。
- `verify.py`：真实副本、HTTP 联动、断线/并发/误配/恢复等可复现场景。
- `performance.py`：10 万条模拟数据、不同词及同词查询、并发增量、写入锁隔离与 EXPLAIN 验证。
- `report.py / report.html`：根据实际证据生成独立交互报告，不伪造实时节点状态。

完整设计在 `技术文档/异地多机拍摄同步技术方案.md`，连续进度在任务 164。公网部署、真实 NAS、生产界面接入、全厂商 RAW 及生产大库性能需后续阶段验收。
