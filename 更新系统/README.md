# 末帧更新与安装

改编自 VoxCPM 独立更新事务与原生 SharedTheme。保留签名清单、断点续传、SHA-256、白名单、逐文件备份、journal、启动健康验证及回滚。安装器和更新器均为原生 .NET Framework 窗口，独立冻结事务环境，不依赖选片引擎或用户安装 Python。

`assets/photo-flow.svg` 是重新设计的照片筛选动画。构建时在 Chromium 中按 SVG 时间轴渲染透明帧，原生窗口循环播放；避免依赖 IE/SVG 插件或将完整浏览器捆入安装器。

签名私钥只存放于忽略的 `.update-private/signing-key`。不要删除或上传。客户端公钥首次发行后不得无迁移替换。

安装器以当前用户运行，优先选择 D、E 等空间充足且可写的固定分区；无合适分区时回退用户 Programs 目录。系统集成使用 HKCU 卸载注册、桌面和开始菜单快捷方式，独立卸载器先复制到用户临时目录再移除程序文件。安装标记保存资料路径与受管文件哈希，卸载保留资料、模型、未知或已修改文件。

首次安装和修复均允许选择非空目录，即使主程序缺失也可直接覆盖安装。仅替换签名清单内的程序文件，保留照片、设置和无关文件；覆盖前逐文件备份，安装失败自动回滚。安装目录及其父路径仍禁止链接，程序文件目标不能被目录或链接占用。

2026-10-21 覆盖安装修复验收：`python -X utf8 -m unittest discover -s 更新系统/tests -v` 的 36 项测试通过；重建 `dist/末帧选片安装器.exe` 后，真实安装器在非空且缺少主程序的隔离目录安装成功，768 个程序文件哈希一致，覆盖前旧文件备份及照片／无关文件保留通过，工作台与引擎健康检查通过。证据为 `build/installer-overwrite/result.json`；本轮未请求模型或驱动安装、未注册测试应用，未发布远端安装器，正式版本与发布号保持不变。

构建顺序：`python -X utf8 更新系统/scripts/release.py prepare` → `python -X utf8 更新系统/scripts/build_tools.py` → `python -X utf8 选片助手/build_release.py --stage-only` → `python -X utf8 更新系统/scripts/release.py build --notes "更新说明"`。通过隔离验收后再用 `release.py publish --snapshot 完整快照目录` 发布。

验收记录见 `开发任务/047-正式安装注册与安全卸载.md`；既有签名更新设计见 `开发任务/035-自动升级与轻量安装器.md`。
