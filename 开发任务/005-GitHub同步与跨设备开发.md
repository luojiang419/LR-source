# GitHub 同步与跨设备开发

[CODEX_LONG_TASK_CONTINUE_V3]

- 当前状态：2026-10-21 已按用户明确要求创建公开仓库 `luojiang419/LR-source` 并推送当前完整源码快照，保留原私有仓库 `luojiang419/LR`。
- 下一步：新设备运行 `git clone https://github.com/luojiang419/LR-source.git`，按 README 恢复开发环境；进行中功能继续按对应任务文档推进。
- TODO：本轮源码发布无剩余功能项；新设备安装与构建在目标设备执行。
- 最近验证：公开仓库匿名访问成功，默认分支 `main`；345 个文件的路径、模式及 blob hash 与本地快照逐项一致；密钥模式和本地凭据交叉检查通过，无超大文件。本轮未运行软件回归或构建。
- branch/commit：开发分支仍为 `codex/mouse-driver-hot-refresh`，基线 `e089791`；公开分支 `public/source-snapshot` 对应远端 `main`，初始快照 `fbf2138`，随后仅提交本同步文档；未切换开发分支或改动其暂存区。
- 阻塞：无。

## 公开源码交付范围

- 公开仓库：https://github.com/luojiang419/LR-source ，默认分支 `main`，本地 remote 名为 `public`；原 `origin` 保留。
- 使用独立索引采集当前工作区完整源码，包含未提交的开发文件、Python 引擎、Rust/Tauri 桌面程序、Lua 桥接插件、C# 更新安装器、前端、测试、文档和演示资源。公开快照从新的根提交开始，私有仓库历史保留在原位置。
- 按现有 `.gitignore` 排除密钥、`.env`、用户数据库、模型、依赖运行时、备份和构建缓存。根目录两个 `.spec` 文件由 `选片助手/build_release.py` 调用 PyInstaller 自动生成，继续排除。
- 本轮仅发布源码，不变更软件版本，不将进行中的功能标记为验收完成，不重复运行应用测试或构建。验收以公开可见、默认分支正确、远端提交与本地快照一致、源码树完整为准。

## 交付范围与验收

以下为私有仓库的历史同步记录。

以下为首次建库记录。2026-10-20本次同步保留原默认分支 `feature/remove-ai-selection`，没有合并或改写远端历史；最新完整源码须查看 `codex/sku-auto-toggle`。原来的5个远端分支与本轮新增21个分支合计26个。密钥、本机数据库、照片、依赖缓存和构建产物继续按忽略规则排除。

- 远程仓库：https://github.com/luojiang419/LR ，可见性为 Private，`origin` 使用 HTTPS。
- 保留并推送全部 5 个本地分支及各自历史：`feature/remove-ai-selection`、`feature/hardware-rating-demo`、`feature/multi-folder-workspace`、`feature/production-bridge`、`feature/tauri-desktop`；当前无标签。
- 默认分支设为 `feature/remove-ai-selection`，以包含移除 AI 选片后的最新功能代码；其他分支用于保留对应阶段实现。
- `README.md` 提供克隆、接力入口和环境恢复说明；`AGENTS.md` 保存用户开发协作偏好，供新设备继续遵守。
- 不上传 `api key.md`、`.env`、本机 `.framekeep/` 数据库和缓存、`.runtime/`、`build/`、`dist/` 等环境与产物；原图仍保留原位置。新设备需要重新配置密钥、照片路径并安装依赖/构建。
- 验收以 GitHub 仓库为 Private、默认分支正确、全部远端分支 hash 与本地一致、开发记录可从远端读取且工作区干净为准。
