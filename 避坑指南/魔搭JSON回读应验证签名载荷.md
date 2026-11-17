# 魔搭 JSON 回读应验证签名载荷

- 表现：上传签名 `envelope.json` 后，匿名下载的 SHA-256 与本地文件不同，相差恰好 3 字节；ZIP 的完整下载校验正常。
- 根因：本次魔搭下载接口返回的 JSON 移除了本地 UTF-8 BOM，签名 `payload` 和 `signature` 没有变化。不能因此改写已发布的不可变 JSON 或关闭签名验证。
- 处理：ZIP、EXE 等二进制仍必须逐字节验证大小和 SHA-256；JSON 使用 `utf-8-sig`/JSON 解析读取，比较 envelope 内容，并对原始 `payload` 字符串执行 Ed25519 验证，不重排或重新序列化该字符串。
- 验证：任务 241 的本地 envelope 为 2530 字节、匿名返回 2527 字节；两项 ZIP 严格哈希一致，匿名 envelope 的签名及 payload 验证通过。用于实际安装的 envelope 位于外层 ZIP 内，其字节及启动器内嵌哈希保持一致。
