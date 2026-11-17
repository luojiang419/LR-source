# XMP 关键词替换与侧车写回恢复

## 表现与根因

ExifTool 对已有列表同时执行 `-XMP-dc:Subject=` 和多次 `-XMP-dc:Subject+=值`，可能保留旧列表再追加，导致旧关键词或旧 SKU 标签残留。JPEG 原来没有 Subject 时不容易发现，已有 XMP 侧车会暴露问题。

ExifTool 能把部分损坏 `.xmp` 当成普通文本读出文件属性，进程返回成功不代表它是可安全合并的 XMP。另一个风险是 JPEG 已提交而伴随侧车尚未提交时进程退出，仅检查 JPEG 成功会漏掉侧车。

## 解决方法

- 替换 Subject 时对每个目标值使用 `-XMP-dc:Subject=值`，空列表只传一次清空；保留由本软件管理的当前 SKU／拍摄类型关键词，单独替换用户关键词。
- 读写侧车前校验 XML、大小和实体声明；损坏文件拒绝覆盖。标题和描述只改 `x-default`，保留其他语言；多行文本通过同卷 UTF-8 参数文件导入，避免参数文件换行变成独立命令。
- 主图与侧车先分别准备、读回和校验，再持久保存各目标前后哈希及临时路径。提交时复核来源；重试或重启跳过已提交项，继续未提交侧车。外部竞争变化拒绝覆盖。
- 照片内容身份与侧车变更身份分开，目录监控能发现只修改侧车的情况，又不使相同原图的缩略图缓存失效。
- 标星和款号确认的写入顺序不固定。JPG 先写入款号、后新建评级侧车时，评级写入必须从 JPG 内嵌款号（或已有侧车）补齐缺失的 `XMP-fk:SKU` 和 `XMP-dc:Subject` 中的 `SKU:款号`。即使星级相同也要检查缺失款号；保留其他关键词、描述、调色及侧车已有的独立款号，并在轻量日志中保存本次补写字段的前后值。

## 验证

`python -X utf8 -m unittest test_xmp_sidecar_sync -v` 覆盖 JPEG、PNG、公开 CR3 副本、两个侧车、中文多行、清空、关键词替换、SKU 修改／撤销、四主题浏览器、切图保存、外部竞争及主图已提交后的侧车恢复。使用隔离临时素材；原始 RAW／PNG 逐字节哈希、JPG 像素及 XMP 调色与其他语言字段均校验保留。

二星款号侧车回归：`test_live_sku_metadata` 新增先款号后标星、先标星后款号及再次改号的真实队列验收；`test_selection_folders_xmp` 新增相同星级补号、原有元数据保留、重复写入幂等及 RAW 完整文件名侧车的款号继承验收。

2026-11-09 执行 `python -X utf8 -m unittest test_selection_folders_xmp test_live_sku_metadata test_photo_file_safety test_xmp_sidecar_sync -v`：65 项中 62 项通过、2 项因真实 RAW 样本未配置而跳过、1 项既有浏览器用例超时。超时位于 `test_xmp_sidecar_sync.py:209`，`#asset-viewer-prev` 不可见；在独立进程中替换回基线 `733327d` 的 `MetadataWriter.write_rating` 后，同一用例在同一位置复现。未修改该界面或用例，二星侧车相关新增用例全部通过。
