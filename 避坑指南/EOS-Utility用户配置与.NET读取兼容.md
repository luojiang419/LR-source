# EOS Utility 用户配置与 .NET 读取兼容

## 表现

自动登记配置用 ElementTree／minidom 检查为有效 XML，但用 Windows .NET Framework `ConfigurationManager.OpenMappedExeConfiguration` 读取 `userSettings/EOSUtility.AppSettings` 时，报 `Unrecognized element 'setting'`。仅测试 XML 语法会漏掉该问题。

## 根因与边界

本机 EOS Utility 3.20.21.1 与 .NET ClientSettingsSection 的读取路径对紧凑连续的 `setting/value` 序列存在兼容性差异：同一字段集合使用紧凑序列化失败，按标准缩进／换行输出后读取成功。没有把这个现象推广为所有 XML 或所有 .NET 配置的通用约束。

用户层 `user.config` 应继承安装目录 `EXE.config` 的配置节声明，不必在用户层重复拷贝 `configSections`。测试夹具里的 `ClientSettingsSection` 类型实际位于 `System` 程序集，不能因命名空间为 System.Configuration 而误写为 System.Configuration 程序集。

## 解决与验证

- 新配置只建立 `configuration/userSettings/EOSUtility.AppSettings` 和需要覆盖的登记字段。
- 使用 `document.toprettyxml(indent='  ', encoding='utf-8')` 输出；保留原文件备份、现有其他字段和原链接字段。
- 使用实际安装的 EXE 配置作为父级，用 .NET 框架读取隔离生成的用户配置，核对 `LinkedApp`，不依赖 Python XML 检查代替目标框架兼容性验证。
- Windows PowerShell 5.1 脚本使用 UTF-8 BOM，输出显式设置 UTF-8，避免中文注释／路径被系统代码页损坏。

对应回归 `test_canon_link.CanonLinkTests.test_framework_can_read_the_new_profile` 已通过；实际登记后又用 .NET 只读检查本机用户配置，路径和开关值正确。41 项非链接首选项与登记前备份的值一致。
