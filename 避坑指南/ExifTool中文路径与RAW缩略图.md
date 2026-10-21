# ExifTool 中文路径与 RAW 缩略图

## 表现与根因

Windows 上将中文目录直接放进 ExifTool 命令行，即使指定 `-charset filename=UTF8`，仍可能返回 `File not found`。2026-10-19 在 Sony ILCE-7M4 ARW 上实际复现：直接 argv 返回码 1、图像数据 0 字节；预览代码随后回退至 LibRaw，代表样本读出 4,898,365 字节、7008×4672 的内嵌 JPEG，再完整解码缩小。

`-charset` 不能修复进入程序前已损坏的 argv。原有批量元数据读取已使用 UTF-8 stdin 参数文件，预览提取需要采用同样方式。

## 解决方法

- 使用 `-charset filename=UTF8 -b -ThumbnailImage -@ -`，将完整路径加换行编码为 UTF-8，作为 `subprocess.run(input=...)` 发送；不通过 shell 传递路径。
- 480px 及以下浏览预览优先 `ThumbnailImage`，缺失或损坏再尝试配对 JPG、大内嵌图和 LibRaw 内嵌图。大图浏览单独选择较清晰预览。
- JPEG 在转 RGB 之前调用 `draft`；同时最多生成 2 个预览，已有缓存直接返回。仅显式 RAW 解码请求调用 `postprocess`。
- 本机 rawpy 0.27.1 已延迟展开 RAW；不能将 `imread()` 本身描述为必然执行全图解码。

## 验证

同一中文路径改为 UTF-8 参数流后，ExifTool 返回码 0、`ThumbnailImage` 8,570 字节、160×120；6 张真实 ARW 均命中小图，含一张竖向照片。两轮空预览缓存、顺序交替测试：旧版 2.2668/2.2458 秒，新版 1.4608/1.4538 秒；操作系统文件缓存未控制，因此不是磁盘冷启动或全目录性能承诺。原片大小和修改时间不变。

56 项 Python 回归通过，覆盖中文参数流、小图优先、缺失/损坏/超时回退、方向、缓存及显式解码边界。实测明细在本机忽略目录 `build/raw-preview-acceptance/benchmark.json`。
