# SQLite 触发器计数与百万队列排序

## 表现

缓存达到百万条后，设置里的实时 COUNT、全量启动核对和待处理任务排序会占用线程；重复绑定可移植别名时，触发器中的 `INSERT OR IGNORE` 还可能意外出现主键冲突。

## 根因与解决

- SQLite 外层 UPSERT 的冲突策略会影响触发器内部插入。可移植覆盖表采用 `INSERT ... SELECT ... WHERE NOT EXISTS`，别名更新时显式维护覆盖，数量触发器与业务写入共用事务，不能只依赖内层 OR IGNORE。
- 待处理队列 `WHERE attempts<3 ORDER BY edge,attempts,requested LIMIT 1` 使用相同顺序的部分索引，并通过 `INDEXED BY pending_active_order` 避免规划器选择含失败记录的全量索引后排序。
- LRU 排序包含文件名作稳定的相同时间次序，索引必须同时覆盖 `(accessed,name)`，否则会创建临时排序。
- 迁移末尾按 `published_seq>? ORDER BY published_seq` 读取增量；不能再按名字排序后过滤发布序号，否则会扫描百万旧记录。
- 启动有限核对并保存游标，运行期按访问校验；数量读维护表，元数据与排队按固定批次释放连接和锁。

## 验证

`test_cache_storage` 覆盖重复别名绑定、计数转换、批次提交、有限启动核对、后台缩容和迁移增量查询计划。`benchmark_preview_scale.py --rows 1000000` 实测百万预览索引与百万生产排队：状态 P95 5.471 ms、领取 P95 26.080 ms、全失败空队列 P95 2.036 ms；队列和 LRU 计划无临时 B-tree。仅 64 张真实 JPEG，不外推为百万原图解码性能。

## 2026-10-23 路径倒排索引与 WAL

增加 FTS 候选后，`root=? ... ORDER BY name COLLATE NOCASE,id` 仍会让 SQLite 选择 `reference_name_page` 并扫描整根目录；50 万文件记录的款号查询仍在数百毫秒。文本查询使用 `+root=?`，让 root 条件继续过滤数据，同时排除它驱动索引选择；候选改走 FTS 虚表与 `catalog_pair`，普通无关键词浏览保留 `root=?`。

`EXPLAIN QUERY PLAN` 必须检查实际完整计数/排序分页，确认有 `VIRTUAL TABLE INDEX`、`catalog_pair` 且没有 `SCAN a` 或名称排序索引整根扫描。`test_fast_path_index` 已纳入真实 `reference_name_page`；50 万记录的 SKU SQL 查询实测从约 217 ms 降至 8.6 ms，完整 HTTP 另行计时。

批次写入的另一成本是反复关闭数据库最后连接触发 WAL checkpoint。为参考目录保留不持有事务的轻量生命周期连接，小批次继续独立提交；同一 1 万张 JPEG 流式验收由约 4.5 秒降至约 2 秒，首批可搜由约 112 ms 降至 48 ms。关闭顺序和完整测量边界见 [任务 084](../开发任务/084-无Everything原生快速索引与搜索.md)。
