import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {buildArchiveWorkbook} from '../server/archive-report.mjs';

const root=fileURLToPath(new URL('../../../',import.meta.url));
const saved=JSON.parse(await fs.readFile(path.join(root,'.runtime','last-archive-report.json'),'utf8'));
const result=await buildArchiveWorkbook(saved.report,{outputPath:saved.outputPath,previewDir:path.join(root,'build','sku-archive-design','xlsx-preview')});
console.log(JSON.stringify(result));
