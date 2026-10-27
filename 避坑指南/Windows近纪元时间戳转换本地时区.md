# Windows 近纪元时间戳转换本地时区

- 表现：模拟时钟为 `1000` 秒时，对 `datetime.fromtimestamp(stamp).astimezone()` 调用可抛出 `[Errno 22] Invalid argument`，导致现场记录失败。
- 原因：naive datetime 转本地时区会经过平台本地时间推断，接近 1970 年时 Windows CRT 的边界可能报错。
- 解决：需保留时区的记录使用 `datetime.fromtimestamp(stamp, timezone.utc).astimezone()`；分组日期直接用 `datetime.fromtimestamp(stamp).strftime('%Y-%m-%d')`。不写死 UTC+8，不丢弃原始 EXIF 偏移。
- 验证：本地时区回归和 SKU 原有模拟时钟工作流通过，包含原来失败的双模型识别记录场景。跨时区浏览器覆盖上海、洛杉矶和 UTC，已有证据不回写。
