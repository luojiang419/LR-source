# Excel 内嵌图片关系文件编码

表现：ZIP 包内 JPEG 可正常解码、图片锚点也能被 Excel 计数，但 Excel 显示白框／无法显示图片，Artifact Tool 的 .NET 导入器甚至触发 WASM 断言。

根因：关系文件由 `ElementTree.tostring(..., encoding='utf8', xml_declaration=True)` 生成，声明中的 `encoding='utf8'` 不是这些读取器接受的标准编码写法。Python XML 解析通过并不能证明 Excel 可正确读取图片关系。

解决：使用 `encoding='utf-8'` 生成带声明的 XML。仅调整关系文件编码声明后，Artifact Tool 可导入并渲染同一个工作簿，商品缩略图恢复；本机 Excel 仍需检查实际渲染，不能只看 `Shapes.Count`。

验证：任务 066 的重导入测试断言标准编码；安装引擎导出的隔离对账件经 Artifact Tool 渲染和 Microsoft Excel 只读打开、PDF 导出检查。图片实际显示，工作表列序、状态、时间和备注内容一致。
