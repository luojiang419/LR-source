# SHIYING 库存管理 Android APP

正式显示名、应用图标和导出文件名已统一为 SHIYING 品牌。包名仍为 `com.framekeep.inventory`，沿用原签名，可覆盖更新保留账本；桌面配套软件为「SHIYING 数字资产」。本轮交付 APK 位于仓库 `dist/SHIYING 库存管理.apk`。构建被其他任务占用时，可通过 `'-PinventoryBuildDir=../build/shiying-android/app'` 指定独立产物目录。

原生 Java / CameraX 相机与玻璃质感 WebView 界面；主题沿用电脑的星夜黑、象牙白、醇白和硬核工作台。页面、弹窗、按钮、拍摄步骤及成功反馈使用贝塞尔曲线过渡，可在设置减少动态效果。

## 使用

1. 选择扫描入库或出库。先拍产品外观，再拍 SKU 标签；右上角可手动开关常亮补光灯。也可从相册分别选择两张实拍图。
   相机页的任意非控件区域均可轻点拍照，快门按钮仍可使用。拍摄接受后短促闪白并播放离线「拍摄」语音；按钮、补光灯、相册和返回保持各自操作，快速连点不会重复拍摄。
2. 双图拍完即保存记录和原图、提示入库/出库成功，不等待 OCR。记录页显示「解析中」；后台解析唯一匹配款号后自动更新原记录，多个候选、不匹配、重组存在歧义或解析失败时标「存疑」。点击存疑行打开双图和优选候选，可选择后手动调整并保存款号，或重新解析；不会重复计数。默认规则包含 `LC`，可修改为逗号分隔的文字。
   记录中的产品图和标签图可点开全屏浏览，支持双指缩放、双击放大/还原、放大后拖动、缩放按钮及「适合屏幕」；最高放大至适配尺寸的 8 倍。切换图片自动还原，系统返回键可关闭。
   尚未拍完的草稿可从首页放弃；已保存的记录使用记录页滑动删除进入回收站，迟到的解析结果不会覆盖手动核对、复活删除记录或恢复已清空的数据。
3. 记录页可按出入库筛选或搜索。点击款号打开编辑窗口，预填当前款号，可对照双图修改后点「保存款号」；解析中也可手动填写，回收站仅支持还原。关闭不保存，空款号与非法字符会提示；修改保留原时间、类型和双图，不重复计数。已同步记录修改后会重新待同步；电脑配套源码已支持按修订更新同一记录，旧版电脑需更新后才能接收已修改款号。
   任意记录左滑或右滑显示「删除」，点击立即移入回收站；在记录页切换「回收站」，同样双向滑动显示「还原」，点击直接恢复，无确认弹窗。删除后的记录和双图保留 30×24 小时，满期不可还原；应用启动/返回前台或进入回收站时接续清理到期记录与双图。还原保留原记录时间和同步状态。
   导出 Excel 包含序号、款号、产品图、标签图、模式及带时区的出入库时间，两张图片直接以手机保存的原文件嵌入 XLSX，保留原分辨率及文件内容，不缩图或二次压缩；表格内按比例显示，无需原图路径。尚未确定款号的行显示「解析中」或「存疑」。
   导出时可选择「全部导出」或「按日期范围」；日期范围包含起止日全天，并可选择仅入库、仅出库或两者。全部导出包含本机未删除的完整账本；回收站不计入统计、导出或手机发送/供电脑读取的记录。已同步电脑的副本保持原状。
   选择范围后，点击「保存到本地」可在系统文件选择器中选择下载目录等位置并修改文件名；写入完成后提示保存成功。取消不会保存或提示成功，失败会提示重试。也可选择「分享给其他应用」。保存到外部目录的表格不受应用内记录清理影响；无需申请整个存储空间访问权限。
4. 记录与原图保存在 APP 私有目录，未完成的双图拍摄可继续，重启保留。保存后播放提示音及离线中文「入库成功／出库成功」语音。入库成功的语音播放完成后自动进入下一件产品拍摄，无需点击下一件；关闭声音时也会自动继续。点击返回可结束连续拍摄，切到后台时等待返回 APP 后接续。
5. 设置页「清理扫描记录」经原生二次确认后，清空本机全部记录、产品与标签图片、草稿、应用内导出及图片预览缓存；主题、识别和连接设置保留。已同步的电脑资料和另存到其他位置的表格保留。清理中断后会接续，未完成时暂停新扫描并提示重试。

## 离线识别与指定 API

- 随 APK 内置 PP-OCRv6 Small/Tiny/Medium、PP-OCRv5 Mobile 的检测和识别模型，以及 ML Kit 中文/Latin 两引擎，无需另行下载。设置页「识别与反馈 → 离线 OCR 模型」下拉选择，立即保存并跨重启保留；默认 PP-OCRv6 Small。
- 每条记录保存入队时的模型选择，后续切换只影响新任务；在存疑窗口点击「重新解析」时使用当前选择。所选引擎直接参与离线解析；Paddle 原生推理无法启动时可使用备用引擎，实际来源保存在记录中。
- 按文本框基线和距离重组同行文字，再处理 SKU 前缀、数字、连字符和后缀，避免款号被拆成若干候选；缺失分隔符、数字分段或模型置信度不足时标存疑，不自动保存截断款号。
- 优先离线解析；仍有疑问且已启用联网增强时，按 `DOC/OCR.txt` 调用 `glm-ocr` 的 OpenAI 兼容接口。使用 `/v1/models` 检测、`/v1/chat/completions` 识别，支持填写基础 URL 或完整接口路径。临时通信失败重试一次，保留离线候选；设置中可关闭云识别或手动检测连接。
- OCR 使用 WorkManager 持久队列，独立于相机保存和网络同步线程。切后台仍可处理，进程中断后保留队列并在系统调度/下次启动时接续；Android 强行停止应用后需重新打开才能继续。重启恢复不重复入库。
- FRP 正常连通不代表本机 LM Studio API 已启动。需要 LM Studio 监听 FRP 对应本地端口且加载 `glm-ocr`。2026-11-05 已用原配置及真实标签通过公网识别；此前 FRP 日志显示本机 1234 曾拒绝连接。
- 凭据从忽略的本机配置构建注入，不写入 Git、界面或诊断日志；首次使用后通过 Android Keystore 加密保存。任何客户端内置共享 API 密钥都可能被逆向提取，长期对外分发应换成工作台代理或每设备短期令牌。现有用户授权的私有内置配置用于本机交付。

## 与工作台通信

电脑底部「资产归档」右侧为「出入库管理」。账本独立于拍摄归档。本机双图保存后立即计入入/出库数量；仅 SKU 已确认的记录进入电脑同步协议，解析中和存疑行保留在手机，确认后再补传。

- **手机主动上传**：电脑「手机连接设置」开启独立端口（默认 8791），在手机连接页填地址与令牌。支持同网段地址和公网 HTTPS 反代路径。后台确定或手动确认 SKU 后自动补传，也可手动同步。电脑每收到一条完整双图后返回三项校验回执，手机才标记已同步。
- **电脑主动读取**：手机连接页开启传输服务（8792），电脑点「连接手机」，填写手机地址与显示的令牌。请保持 APP 前台。重复读取自动去重。
- **自动 USB 同步**：手机开启并授权 USB 调试后，用数据线连接电脑，打开桌面「出入库管理」即可自动启动手机 APP 和 USB 通道，无需填写 IP 或令牌。桌面自带 ADB，手机 USB 服务只监听回环地址并显示同步通知；连接页显示「USB 已连接电脑」。已确认 SKU 的记录持续增量同步，断线恢复不重复计数；解析中和存疑数量显示在桌面连接栏。其他网络连接仍可手动使用原入口。
- 切换到另一工作台会重新补传全部已确定 SKU 的本机记录；同一工作台只上传尚未获得同步回执的记录。记录标识及图片 SHA-256 保证重试不重复计数，内容冲突保留电脑原记录并提示。
- 电脑配置默认关闭网络入口。本机账本与双图保存在资料目录 `inventory/`。网络入口只提供带 Bearer 令牌的健康检测和库存接收，不暴露模型或其他业务 API。

## 本机构建

需要 JDK 17、Android SDK 35，SDK 路径由 `local.properties` 指定。

```powershell
python -X utf8 prepare_build.py
.\gradlew.bat :app:assembleDebug :app:lintDebug
# 内部交付：非调试、R8 混淆，沿用本机证书以保留既有数据
.\gradlew.bat :app:assembleRelease :app:lintRelease
# 日期边界单测；独立验证包拥有独立数据目录
.\gradlew.bat :app:testDebugUnitTest :app:assembleVerification
```

`prepare_build.py` 读取仓库 `DOC/OCR.txt`，写入被忽略的 `ocr.local.properties`；SDK 默认使用 `D:/Android/Sdk`。Windows JDK 17 在中文项目路径下的测试类路径解析有兼容问题，产物统一放在仓库 `build/inventory-android/app/`，其中调试包为 `outputs/apk/debug/app-debug.apk`，交付包为 `outputs/apk/release/app-release.apk`。独立验证包为 `outputs/apk/verification/app-verification.apk`，包名 `com.framekeep.inventory.verification`，不会覆盖正式应用或共用其资料。当前沿用本机内部开发签名，未经应用商店发布。

小米 MIUI 可能限制 USB 安装，可把 APK 复制到手机下载目录，在文件管理器中打开并通过系统安装界面安装；不用更改 USB 安装安全设置。

## 最小本机安卓测试环境

按用户要求，后续验证使用 AOSP Android 14 模拟器，避免占用手机。已创建 `FramekeepMinApi34`：1.5 GB RAM、2 核、480×854 / 160 dpi、512 MB 数据分区，无 Google 服务，WHPX 加速。SDK 位于 `D:/Android/Sdk`，AVD 位于 `D:/Android/avd/FramekeepMinApi34.avd`。

运行 `启动最小模拟器.ps1` 可无窗口启动，添加 `-Visible` 可显示模拟器。所有测试 ADB 命令必须指定 `-s emulator-5556`；不使用默认设备选择，也不重新连接用户手机。

安装包验证示例：`adb -s emulator-5556 install -r -g build/inventory-android/app/outputs/apk/release/app-release.apk`。关闭测试运行用 `adb -s emulator-5556 emu kill`，AVD 配置保留供下次复用。

模拟器自带 WebView 113，连接 DevTools 时使用 Playwright `connect_over_cdp(..., no_defaults=True)`；该版本不支持浏览器上下文下载管理，直接使用默认连接选项会报 `Browser context management is not supported`。

## 开源依赖

- [Android CameraX 官方样例](https://github.com/android/camera-samples)：相机生命周期与拍照实践。
- [Google ML Kit 样例](https://github.com/googlesamples/mlkit)：随包端侧文字识别。
- [NanoHTTPD](https://github.com/NanoHttpd/nanohttpd)：BSD 许可的手机 HTTP 传输服务。
- AndroidX / OpenJDK 及依赖许可遵循各自发行声明；业务代码为本项目实现。XLSX 通过标准 Open XML ZIP 部件生成。

端侧推理基于 PaddleOCR 官方 Android SDK、ONNX Runtime 1.21.1 与 OpenCV 官方 4.11.0。上游来源、许可和本地适配见 `paddle-ocr/UPSTREAM.md`；固定权重哈希和构建准备脚本位于 `tools/`。首次构建需要下载权重，生成文件放在仓库 `build/`，不提交到 Git；运行时全部本地可用。

MI 9 / Android 11 的统一样本对比：Small 和 Medium 在本组 9 个 SKU 用例中均完整正确，Small 中位耗时约 1.35 秒、Medium 约 5.21 秒，因此默认 Small。样本含 1 张真实标签及其 4 种受控变体、4 张合成印刷 SKU 和 1 张真实中文手写参考图；不能据此推断所有用户手写 SKU 的准确率。完整对比见 `开发任务/190-手机端OCR模型择优部署与小米9验证.md`。

小米 9 的交付使用 `-PinventoryAbi=arm64-v8a` 生成 ARM64 包；不传该参数时沿用多架构构建。Paddle 权重合计约 197.74 MB（含字典配置），全模型 ARM64 APK 约 208 MB。

图片查看器浏览器手势回归：`python -X utf8 安卓出入库/tests/test_photo_viewer.py -v`（在仓库根运行，需要本机 Chrome 和 Playwright）。

记录与回收站手势回归：`python -X utf8 安卓出入库/tests/test_record_trash.py -v`。完整页面回归：`python -X utf8 -m unittest discover -s 安卓出入库/tests -p 'test_*.py' -v`。

数据库迁移、30 天边界、重启、双图、导出及分页测试在独立验证包执行：在 `安卓出入库` 构建 `./gradlew.bat -PinventoryTestBuildType=verification :app:assembleVerification :app:assembleVerificationAndroidTest`，仅向 `emulator-5556` 安装验证 APK 和 androidTest APK，然后运行 `adb -s emulator-5556 shell am instrument -w com.framekeep.inventory.verification.test/androidx.test.runner.AndroidJUnitRunner`。测试使用 UUID 命名的独立数据库与图片目录，结束后清除自身测试数据。

桌面 USB 增量协议使用 schema v4 的 `sync_changes` 日志；在记录变成已确认、还原已确认记录或手动修改已确认款号时产生事件，旧记录升级时回填。按日志序号而非拍摄时间续传，迟到 OCR 不会遗漏。手机清理/本地回收站不删除电脑已有副本；双图保存及校验后才返回回执，手机记录即时更新为已同步。
