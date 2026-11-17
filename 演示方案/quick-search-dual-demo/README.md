# 快捷搜图 · 轻量双区演示

用户选定三版视觉方案中的第 2 版。演示独立运行，不替换正式快捷搜索、不修改生产数据库或安装版。

## 体验范围

- 左侧参考图：点击更换、文件上传、拖入图片、Ctrl+V 粘贴；粘贴按钮依浏览器权限工作，失败时提供替代提示。
- 右侧三列结果：关键词/SKU 检索、归档范围、相似度阈值与排序、空态清除；单击选择、双击或 Enter 预览，左右键翻图。
- 标题旁显示当前匹配数量；大图预览可复制原图、复制真实路径、在资源管理器定位、另存原图。支持保存选择器时选择目的文件，否则下载 PNG。
- Esc 先关闭预览/菜单，再关闭搜索窗；网页 Ctrl+Shift+Space 恢复。Tab 保持在当前弹窗。
- 左下角“外观”演示四主题、背景模糊开关、模糊度与亮度，不挤占弹窗常驻区域。
- 初始相似度是示例数据。换图后仅在浏览器内对示例图库做配色排序；没有接入正式 CLIP 服务，不代表生产检索准确率，不上传图片。

## 运行与恢复

本轮已实际启动并验证 `http://127.0.0.1:8798/`，用户已在内置浏览器体验。后续如需恢复，在本目录执行：

```powershell
npm install --prefer-offline --no-audit --no-fund
npm run dev -- --host 127.0.0.1 --port 8798 --strictPort
```

`npm run build` 已通过。构建产物、依赖与日志不入 Git。未发布到外部网站。

## 设计与验证

- 源视觉：`design/selected-reference.png`。
- QA：`design-qa.md`；实际截图、并排对照与 14 组交互结果在仓库忽略目录 `build/quick-search-dual-demo/`。
- 照片与工作台背景使用内置 Image Gen 独立生成。原始竖图及重新构图方图均保留来源；正式结果使用适合槽位的方图，参考栏使用竖图。
- 图标使用 [Tabler React 图标](https://github.com/tabler/tabler-icons/blob/main/packages/icons-react/README.md)，字体优先使用本机 Segoe UI / Microsoft YaHei。
- `scripts/local-assets.mjs` 提供本机路径信息与 Explorer 定位，仅支持白名单、同源与令牌校验。外部静态部署不包含这些本机能力；界面在服务不可用时提示，不假装成功。
- `node --test tests/local-assets.test.mjs` 已通过。图片操作检查用隔离剪贴板/保存选择器验证原图字节，不触碰用户剪贴板；原图下载 SHA-256 一致，翻图后路径切换正确。没有自动唤起资源管理器来抢占用户窗口。
