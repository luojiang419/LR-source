# 多模态检索的高频词与 CLIP 运行时

## 高频中文词命中越多反而越慢

表现：百万条合成库中 SKU 查询约 6 ms，但“红色”和组合维度超出 2 秒 SQL 预算。

根因：先展开高频全文词的数十万 ID，再排序；媒体视图和跨表版本验证放大成本。短词又无法使用 trigram，提前扫描路径/元数据增加耗时。

解决：添加 `(online,modified DESC,id)` 有序索引，先按顺序取当前页；FTS 同时索引资产 ID 和关键词，每个候选使用罕见的资产 ID 与高频词做交集核验。组合维度沿当前候选做同帧检查；单字中文只检查该资产的索引文本。最终页再批量取完整资料，分面建议按 `(key,normalized)` 跳到下一个不同值，避免整库计数。

验证：最终百万条库中，SKU 5.69 ms、中文词 18.65 ms、组合维度 4.25 ms（中位，五次）；无超时，quick_check=ok。复现脚本为 `选片助手/benchmark_intelligence.py`，详细条件见验证记录，不能将此数值等同包含模型、网络盘和缩略图的端到端耗时。

## 已安装 CLIP 有文件但初始化卡住

表现：旧 MaterialSearch Python 的 transformers 扫描大量无关模型模块；其他环境加载时又在 GPU `.to()` 处长时间等待。

根因：无关框架初始化、版本差异及共享显存资源争抢，不能仅凭模型文件存在判定可用。

解决：独立常驻进程，优先使用已有可用的开发 Python，模型只从本机读取；设置 `USE_TF=0`、`USE_FLAX=0` 和离线模式。默认 CPU 4 线程，与 Qwen/GLM 及媒体 GPU 队列分离；需要 GPU 时显式设置 `FRAMEKEEP_CLIP_DEVICE=cuda`。加载和推理均有超时与进程回收。

验证：图片与中文文字均实际生成 768 维向量，范数约 1，含启动约 10.52 秒；真实 HTTP 目录解析、以图搜图和语义搜索完成，参考图自相似度为 1。

## transformers 特征返回类型不统一

表现：`get_image_features()` 返回 `BaseModelOutputWithPooling`，直接 `.norm()` 报错。

解决：兼容 Tensor 和 `.pooler_output` 两种返回，之后执行单位化与维数校验。不能用任意维度的占位向量掩盖错误。

## Windows SQLite 备份句柄没有关闭

表现：测试结束清理临时目录时出现 WinError 32。

根因：`with sqlite3.connect(...)` 只管理事务，不关闭连接。

解决：备份目标使用 `contextlib.closing`，读取验证同样显式关闭。已通过在线备份、quick_check 和临时目录清理测试。
