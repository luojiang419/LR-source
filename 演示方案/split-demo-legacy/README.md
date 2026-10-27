# 分屏拍摄隔离演示前端

来自分支 feat/split-capture-demo 的 2290cd1。仅由根目录“启动分屏拍摄演示.cmd”使用，接口为隔离的 /api/split/。主项目默认继续使用后来正式化并持续修复的 split-capture.js/css 与 /api/reference/。

兼容最新异步首屏：在 framekeep:boot-ready 后加载覆盖层。演示复用当前主项目其余界面及业务服务，不复制生产资料库或启用模型推理。
