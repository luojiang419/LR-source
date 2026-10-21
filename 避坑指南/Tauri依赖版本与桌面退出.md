# Tauri依赖版本与桌面退出

表现：Tauri 2.11.5被Cargo解析到更新的宏/运行时后，编译出现`UnexpectedMenuKind`不存在等库内部错误。根因是未锁定的内部依赖组合不一致。本次统一到Tauri 2.12.4、tauri-build 2.7.3并提交Cargo.lock，使用`cargo build --locked`后成功；不要在crate缓存源码里打补丁。

关闭窗口时先阻止关闭，通过前端`FramekeepIO.flush()`保存，再调用Rust退出；Rust持有后台Child，只终止自己启动的引擎，并用每实例令牌发出正常关闭请求。引擎同时观察父进程退出，避免主窗口异常结束后残留后台。隔离窗口Alt+F4与引擎8776端口消失已验证。

截图限制：本机Windows窗口截图两次返回`window capture timed out`，UIA仍能读取完整WebView2文档。此时不能凭可访问性树宣称原生目录/保存对话框已点击成功；明确记录未验证项。
