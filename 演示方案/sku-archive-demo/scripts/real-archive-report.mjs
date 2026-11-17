import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {buildArchiveWorkbook} from '../server/archive-report.mjs';
import {archiveReportFilename} from '../src/report-filename.js';

const root=fileURLToPath(new URL('../../../',import.meta.url));
const testRoot=path.resolve(process.argv[2]||path.join(root,'测试文件夹'));
const report=JSON.parse(await fs.readFile(path.join(testRoot,'sku库','归档结果.json'),'utf8'));
const filename=archiveReportFilename();
const result=await buildArchiveWorkbook(report,{outputPath:path.join(testRoot,'sku库',filename),previewDir:path.join(root,'build','sku-archive-real-report'),realValidationRoot:testRoot});
console.log(JSON.stringify(result));
