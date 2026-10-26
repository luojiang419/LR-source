# EOS Utility 链接软件接入分析

日期：2026-10-22。范围：本机 Windows EOS Utility 3.20.21.1，静态分析、配置和注册表只读核对；未修改 Canon 程序、用户首选项、注册表或相机。

后续实现更新：任务 058 已完成自动登记、图片参数与单实例转交，使用方法见 [Canon自动联机接入](Canon自动联机接入.md)。下文保留实现前的分析与证据边界，不再代表当前版本缺少参数处理。

## 结论

**本项目可以通过 EOS Utility 官方“记录…”入口加入链接软件列表，不需要 Canon SDK、插件、伪装 DPP 或修改系统注册表。**

“出现在列表”和“自动打开刚拍的照片”需要分别处理。当前末帧 EXE 可以登记并被启动；但现有 Rust 启动代码没有接收图片路径，单实例回调也忽略参数，只显示并聚焦窗口。因此，直接登记后，仍需让末帧的现场监控指向 EOS Utility 的实际保存目录；若希望自动选择目录和定位传入照片，则需补充参数接收与转交。

佳能官方中文说明明确将“记录”列为添加第三方程序的入口，并允许选择要通知的图片类型：[EOS Utility：首选项／链接软件](https://cam.start.canon/zh/S003/manual/html/UG-05_Setting_0010.html)。本次二进制静态调用链与其一致。

## 当前即可采用的配置

1. 在 EOS Utility 打开“首选项 → 链接软件 → 记录…”。
2. 在选择窗口中浏览至稳定的桌面主程序路径。本项目当前便携运行包是 `G:\app\LR\dist\末帧选片\末帧选片.exe`。正式安装后应登记实际安装目录中的同名主程序；不要选内部 `engine/framekeep-engine.exe`。
3. 勾选实际传入的图片类型：拍摄 JPG 则勾 JPEG，拍摄 CR3 则勾 CR3；RAW+JPG 可同时勾选。确认选择窗口，再确认首选项。
4. 下拉框会新增并选中 `末帧选片.exe`。在 EOS Utility“目的文件夹”确认实际保存位置，并在末帧开启同一目录的现场监控。若 EOS 自动建立日期子目录，应监控其稳定父目录并启用递归。

登记路径需要持续存在，移动便携文件夹后应重新登记。当前构建目录通常不适合长期固定登记，正式使用宜选择稳定安装目录。

在上述监控配置下，新照片入库和缩略图/大预览缓存走现有流程；EOS 的启动通知只负责唤起窗口。静态代码表明每份符合类型的文件都会启动一次程序；当前单实例实现会聚焦已有窗口，因此连续拍摄可能反复抢占焦点，不能把这种方式当作完整的无干扰接入。

## 本机分析身份与证据

| 项目 | 结果 |
| --- | --- |
| 实际业务程序 | `C:\Program Files (x86)\Canon\EOS Utility\EU3\EOS Utility 3.exe`，顶层 EXE 是启动入口，偏好与通知代码在 EU3 内 |
| 程序集版本 | `3.20.21.1` |
| 架构 | x86 CIL，`.NETFramework,Version=v4.7.1` |
| SHA-256 | `728bcb732431855c411bf742d5f4e9c810666f5676b364f3448a6938d171d09e` |
| MVID | `8461da93-d588-4805-86ca-d96c358ecd06` |
| 用户配置 | `C:\Users\jiang\AppData\Local\CANON_INC\EOS_Utility_3.exe_StrongName_xzsdk5dlt1oydmyayonzi0iytjbnaly0\3.20.21.1\user.config` |

REA `inspect_managed_artifact` 原始 Evidence：`ev_7b5546467627672135ab495ed3cd83bea942309fa5d4fbc4f6c01f6baf52065e`；摘要 Evidence：`ev_d2e89d22e9af0649b7fb7f19ed68d7029b65c36101873bea1d88337a686bad76`。该工具确认身份和托管结构，没有执行程序或反编译 C#。

REA `inspect_managed_members` 遇到 64MiB 表示预算，原始 Evidence：`ev_fa9f2bc1fa49e6e284f4b8782114c769b302059f9d1af51bd417c9db8da11e8a`，覆盖状态 `unavailable`。不将其空结果当作不存在方法。随后使用本机已有 ILSpy `9.1.0.7988`，仅反编译以下四个明确类型；方法行为结论属于 ILSpy 的静态恢复结果，不冒充 REA 已返回的原生运行证据：

| 类型／方法 | 静态观察 |
| --- | --- |
| `EOSUtility.ApplicationSettings.btnOpenLinkedApp_Click` | 打开登记对话框；确认后将所选 EXE 的 `Path.GetFileName` 加入下拉框，并选中该项 |
| `EOSUtility.ApplicationSettings` 的加载／保存逻辑 | 从 `AppSettings.Default.LinkedApp` 加载路径；保存 `IsLinkedAppOther` 与图片通知类型，调用 `AppSettings.Default.Save()` |
| `EOSUtility.LinkedAppSelectDlg` | 使用 `OpenFileDialog` 选择文件；确认时检查 `File.Exists`，不存在则阻止关闭；提供 JPEG/CRW/CR2/CR3/TIF 类型选择 |
| `EOSUtility.LinkedApplicationController.SetupLinkedApp` | 普通第三方模式选择 `NotifyOtherApp`；DPP 等有独立处理分支 |
| `EOSUtility.LinkedApplicationController.Downloaded` | 将已下载文件路径转交 `linkedApp.FileAdded(path)` |
| `EOSUtility.NotifyOtherApp.FileAdded` | 按用户选择的扩展名过滤，确认第三方 EXE 存在后调用 `Process.Start`；参数是带双引号包围的单张图片完整路径 |

其调用形式等价于：

```text
"G:\app\LR\dist\末帧选片\末帧选片.exe" "D:\相机传入\IMG_0001.JPG"
```

没有在这个第三方分支看到 DPP 专用 mailslot、SDK 握手或逐文件回执。仅据该分支不能断言所有 EOS 模式都按同一时序调用；真实拍摄触发次数和节奏未实测。

完整 REA Evidence、反编译类型输出、目标与输出哈希及限定配置快照保存在忽略目录 `build/canon-eos-link/`。`manifest.json` 只记录链接字段，不导出完整用户配置。

## 配置与注册表核对

用户配置段 `configuration/userSettings/EOSUtility.AppSettings` 当前为 `LinkedApp` 空、`IsLinkedAppDPP=True`、`IsLinkedAppOther=False`，与截图只选择 DPP 一致。安装默认配置也定义了第三方字段。本版本使用一个 `LinkedApp` 路径槽位，代码的下拉框由 DPP、VR、一个第三方程序和“不链接”组成，不是自动枚举所有已安装应用。

现有 DPP 的注册表登记在 32 位视图 `HKLM\SOFTWARE\Canon_Inc_IC\DPP4 Shared`，对应真实位置 `HKLM\SOFTWARE\WOW6432Node\Canon_Inc_IC\DPP4 Shared`，包含 `AppName`、`ExePath`、`32bitAppsPath` 和 `Launch_DPP`。这些属于 DPP 专用识别，第三方登记无需复制或改写它们。限定 Canon 键的只读查询未找到通用第三方应用枚举表；这是限定范围的负面结果，核心依据仍是上述普通第三方程序调用链。

## 若继续开发完整接入

建议在主程序增加 EOS 图片参数入口，保留官方登记机制：

- Rust 首次启动读取并校验图片参数；单实例回调接收 `args/cwd`，将路径转交已有实例。
- 程序未就绪时先排队，启动完成后再处理；可采用本机受保护的 IPC，避免拼接脚本或 shell 命令。
- 根据图片目录接入现场监控：同目录不重复重启监控或全目录索引；新目录才建立／切换会话，并保留已有评级。图片仍走稳定写入检查、RAW/JPG 合并与缓存队列。
- 后续逐张通知不抢焦点，不打开重复窗口；仅首次唤起或用户明确要求时激活窗口。
- 针对正式安装与便携路径分别验证资料目录。现有 `data_directory` 支持安装清单、显式环境变量，以及当前项目目录下的资料定位；独立复制 EXE 而没有完整运行目录不可作为接入方式。

如需将高频启动和主程序分离，也可以制作小型 `末帧Canon接入.exe`，只负责把图片路径转交主程序。它可以成为“记录…”选择的目标，但当前任务没有实现或注册这个新程序。

后续验收应覆盖：首次启动与已运行实例、中文/空格路径、JPG 与 CR3 配对、连续拍摄、目录切换、无重复窗口、无焦点抢占、缓存自动生成和原评级保留。本次仅完成接入机制分析，未将 EOS Utility 切换到末帧，也未声称实机全链路通过。
