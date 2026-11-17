import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {archiveReportFilename} from '../src/report-filename.js';

const projectRoot = fileURLToPath(new URL('../../../', import.meta.url));
const assetsRoot = path.join(projectRoot, '演示方案', 'assets');
const headers = ['母SKU', '子SKU', '存储路径', '文件缩略图', '归档日期', '归档前路径', '归档后路径', '校验结果', '配套XMP路径', '备注', '图片类型'];
const thumbs = ['XSY_0941.jpg', 'XSY_0942.jpg', 'XSY_0943.jpg', 'XSY_0944.jpg', 'XSY_0945.jpg'];
const successful = new Set(['verified', 'identical_verified_skip']);
const literal = value => /^[=+@-]/.test(String(value ?? '')) ? `'${value}` : String(value ?? '');
const directory = value => value ? value.slice(0, value.lastIndexOf('\\')) : '';
const localDate = value => value && Number.isFinite(Date.parse(value)) ? new Date(Date.parse(value) + 8 * 3600_000) : null;
const dateText = value => localDate(value)?.toISOString().replace('T', ' ').slice(0,19) || '';

export function reportRows(report, deleted = false) {
  return (deleted ? report.skipped_reference_records : report.items).map(item => {
    const ok = successful.has(item.status), existing = item.status === 'identical_verified_skip';
    const result = deleted ? '后期已删除，自动跳过' : existing ? '已存在，校验一致' : ok ? '验证通过' : item.status === 'excluded' ? '已排除，未归档' : '校验失败，待核实';
    let notes = deleted ? '不计归档预期数量，无归档后路径。' : existing ? '原归档日期未知；本次已核验。' : ok ? report.demo ? '演示样例缩略图，不对应真实文件。' : '真实文件隔离归档验证；测试备案款号。' : `未确认归档成功。计划目标：${item.destination || '未确定'}`;
    if(item.origin==='onsite_label')notes+=' 二星标签图由现场小档补充。';
    if(report.preprocess)notes+=' 归档前处理已开启；整理后路径：'+(deleted?item.reference:item.source);
    return [literal(item.parent_sku),literal(item.sku),literal(ok ? directory(item.destination) : ''),deleted?'无最终文件':'',
      deleted || !ok || existing ? null : localDate(item.archived_at),literal(deleted ? item.original_reference||item.reference : item.original_source||item.source),
      literal(ok ? item.destination : ''),result,literal(ok ? item.xmp : ''),notes,item.photo_role==='label'?'二星标签图':'款式图'];
  });
}

async function runtimePackage(name) {
  const runtimeDir = process.env.CODEX_REPORT_RUNTIME_DIR || path.join(projectRoot, '.runtime', 'report-authoring');
  const runtimeRequire = createRequire(path.join(runtimeDir, 'package.json'));
  return import(pathToFileURL(runtimeRequire.resolve(name)).href);
}

// 公共绘图 API 只导出 oneCellAnchor；补充 Excel 的“随单元格移动和调整大小”。
async function anchorThumbnailsToRows(outputPath) {
  const {default:JSZip}=await runtimePackage('jszip');
  const zip=await JSZip.loadAsync(await fs.readFile(outputPath));
  for (const name of Object.keys(zip.files).filter(name=>/^xl\/drawings\/drawing\d+\.xml$/.test(name))) {
    const xml=await zip.file(name).async('string');
    const fixed=xml.replace(/<xdr:oneCellAnchor>([\s\S]*?)<\/xdr:oneCellAnchor>/g,(_,content)=>{
      const tag=name=>Number(new RegExp(`<xdr:${name}>(\\d+)</xdr:${name}>`).exec(content)?.[1]);
      const extent=/<xdr:ext cx="(\d+)" cy="(\d+)"\s*\/>/.exec(content);
      if(!extent)throw new Error('缩略图锚点缺少尺寸');
      const to=`<xdr:to><xdr:col>${tag('col')}</xdr:col><xdr:colOff>${tag('colOff')+Number(extent[1])}</xdr:colOff><xdr:row>${tag('row')}</xdr:row><xdr:rowOff>${tag('rowOff')+Number(extent[2])}</xdr:rowOff></xdr:to>`;
      return `<xdr:twoCellAnchor editAs="twoCell">${content.replace(extent[0],to)}</xdr:twoCellAnchor>`;
    });
    zip.file(name,fixed);
  }
  await fs.writeFile(outputPath,await zip.generateAsync({type:'nodebuffer',compression:'DEFLATE'}));
}

export async function buildArchiveWorkbook(report, {outputPath, previewDir, realValidationRoot} = {}) {
  const real = report.demo===false && report.real_validation===true && realValidationRoot;
  if ((!real && report.demo !== true) || !Array.isArray(report.items) || !report.items.length || report.items.length > 100) throw new Error('报告数据无效');
  if(real){
    const base=path.resolve(realValidationRoot)+path.sep;
    for(const item of report.items)for(const key of ['source','destination','thumbnail_path']){
      if(!item[key]||!path.resolve(item[key]).startsWith(base))throw new Error('真实验证文件必须位于指定隔离根目录内');
    }
    if(!path.resolve(outputPath).startsWith(base))throw new Error('报告必须保存到隔离目录内');
  }
  const {Workbook, SpreadsheetFile} = await runtimePackage('@oai/artifact-tool');
  const wb = Workbook.create();
  const specs = [{name:'归档明细',deleted:false}];
  if (report.skipped_reference_records?.length) specs.push({name:'后期删除记录',deleted:true});
  for (const spec of specs) {
    const sheet = wb.worksheets.add(spec.name), rows = reportRows(report,spec.deleted), end = rows.length+5;
    sheet.showGridLines = false;
    sheet.tabColor = spec.deleted ? '#89948B' : '#345244';
    sheet.getRange(`A1:K${end}`).format.font = {name:'Arial',size:10,color:'#26332B'};
    sheet.getRange(`A1:K${end}`).format.verticalAlignment = 'center';
    sheet.getRange('A2').values = [[(spec.deleted ? '后期删除记录' : '资产归档校验报告')+(real?'（真实验证）':'（演示）')]];
    sheet.getRange('A2').format.font = {name:'Arial',size:14,bold:true,color:'#263E30'};
    sheet.getRange('A3').values = [[real?'真实照片副本归档；缩略图来自对应归档文件。SKU为测试备案值。日期按 Asia/Shanghai 显示。':'演示数据；缩略图为现有样例，不对应真实文件。归档日期按 Asia/Shanghai 显示。']];
    sheet.getRange('A3').format.font = {name:'Arial',size:10,color:'#737B74'};
    sheet.getRange('A4').values = [[`本次核验完成：${dateText(report.completed_at)}。最终目录 ${report.final_expected??report.expected} 个文件，补充标签 ${report.supplemental_label_count||0} 张，共归档 ${report.expected} 张，配套 XMP ${report.expected_xmp} 个，后期删除 ${report.deleted_reference_count} 张。`]];
    sheet.getRange('A5:K5').values = [headers];
    sheet.getRange(`A6:K${end}`).values = rows;
    const table = sheet.tables.add(`A5:K${end}`,true,spec.deleted?'DeletedReferenceRecords':'ArchiveDetails');
    table.style = 'TableStyleLight1';
    table.showFilterButton = true;
    // 仓管逐行逐列核对所需的实边框，灰色填充与打印时同样可见。
    sheet.getRange(`A5:K${end}`).format.borders = {preset:'all',style:'thin',color:'#98A49B'};
    sheet.getRange('A5:K5').format = {fill:'#345244',font:{name:'Arial',size:10,bold:true,color:'#FFFFFF'},horizontalAlignment:'center',verticalAlignment:'center'};
    sheet.getRange('A5:K5').format.borders = {insideVertical:{style:'thin',color:'#FFFFFF'}};
    sheet.getRange('A5:K5').format.rowHeightPx = 32;
    sheet.getRange('A1:K1').format.rowHeightPx = 10;
    sheet.getRange('A2:K2').format.rowHeightPx = 26;
    sheet.getRange('A3:K4').format.rowHeightPx = 23;
    const widths = [86,126,290,120,158,330,360,148,360,260,110];
    widths.forEach((width,col) => {sheet.getRangeByIndexes(0,col,end,1).format.columnWidthPx = width;});
    sheet.getRange(`A6:K${end}`).format.rowHeightPx = spec.deleted&&!report.preprocess?76:132;
    sheet.getRange(`C6:C${end}`).format.wrapText = true;
    sheet.getRange(`F6:K${end}`).format.wrapText = true;
    sheet.getRange(`D6:E${end}`).format.horizontalAlignment = 'center';
    sheet.getRange(`E6:E${end}`).setNumberFormat('yyyy-mm-dd hh:mm:ss');
    sheet.getRange(`H6:H${end}`).conditionalFormats.add('containsText',{text:'失败',format:{fill:'#FBE8E4',font:{color:'#9C3028',bold:true}}});
    sheet.getRange(`H6:H${end}`).conditionalFormats.add('containsText',{text:'已排除',format:{fill:'#FFF0D6',font:{color:'#795515'}}});
    sheet.freezePanes.freezeRows(5);
    sheet.freezePanes.freezeColumns(2);
    if (!spec.deleted) {
      for (let i=0;i<rows.length;i++) {
        const item=report.items[i];
        if(real){
          const bytes=await fs.readFile(item.thumbnail_path);
          const width=item.thumbnail_width,height=item.thumbnail_height;
          if(!width||!height||width>120||height>120)throw new Error('真实缩略图尺寸无效');
          sheet.images.add({dataUrl:`data:image/jpeg;base64,${bytes.toString('base64')}`,anchor:{from:{row:i+5,col:3,rowOffsetPx:6,colOffsetPx:Math.round((120-width)/2)},extent:{widthPx:width,heightPx:height}}});
          continue;
        }
        const id = Number(item.id);
        const thumbnail = thumbs[id-1];
        if (!thumbnail) {sheet.getCell(i+5,3).values = [['缩略图不可用']];continue;}
        const bytes = await fs.readFile(path.join(assetsRoot,thumbnail));
        sheet.images.add({dataUrl:`data:image/jpeg;base64,${bytes.toString('base64')}`,anchor:{from:{row:i+5,col:3,rowOffsetPx:6,colOffsetPx:20},extent:{widthPx:80,heightPx:120}}});
      }
    }
  }
  wb.recalculate();
  await fs.mkdir(path.dirname(outputPath),{recursive:true});
  const xlsx = await SpreadsheetFile.exportXlsx(wb);
  await xlsx.save(outputPath);
  await anchorThumbnailsToRows(outputPath);
  if (previewDir) {
    await fs.mkdir(previewDir,{recursive:true});
    for (const spec of specs) {
      const end = reportRows(report,spec.deleted).length+5;
      const preview = await wb.render({sheetName:spec.name,range:`A1:K${end}`,scale:1,format:'png'});
      await fs.writeFile(path.join(previewDir,`${spec.name}.png`),new Uint8Array(await preview.arrayBuffer()));
    }
    const inspected = await wb.inspect({kind:'table',range:'归档明细!A5:K10',include:'values,formulas',tableMaxRows:6,tableMaxCols:11,maxChars:2500});
    await fs.writeFile(path.join(previewDir,'inspection.ndjson'),inspected.ndjson);
    const errors = await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:20},maxChars:1000});
    await fs.writeFile(path.join(previewDir,'errors.ndjson'),errors.ndjson);
  }
  return {outputPath,sheets:specs.map(spec=>spec.name),rows:report.items.length,deleted:report.skipped_reference_records?.length||0};
}

export function archiveReportPlugin() {
  return {name:'local-archive-report',configureServer(server) {
    server.middlewares.use('/api/demo/archive-report', async (req,res) => {
      if (req.method!=='POST') {res.statusCode=405;res.end();return;}
      try {
        let body='';
        for await (const chunk of req) {body+=chunk.toString();if(Buffer.byteLength(body)>512_000) throw new Error('演示报告数据超出范围');}
        const report=JSON.parse(body), outputRoot=path.join(projectRoot,'outputs','sku-archive-reports');
        if(report.demo!==true)throw new Error('网页演示接口不接受真实路径');
        await fs.mkdir(outputRoot,{recursive:true});
        const outputDir=await fs.mkdtemp(path.join(outputRoot,'report-'));
        const filename=archiveReportFilename();
        const outputPath=path.join(outputDir,filename);
        await buildArchiveWorkbook(report,{outputPath});
        await fs.writeFile(path.join(projectRoot,'.runtime','last-archive-report.json'),JSON.stringify({outputPath,report},null,2));
        res.setHeader('Content-Type','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
        res.setHeader('Content-Disposition',`attachment; filename="archive-report-${filename.match(/\d{12}/)[0]}.xlsx"; filename*=UTF-8''${encodeURIComponent(filename)}`);
        res.setHeader('X-Archive-Report-Filename',encodeURIComponent(filename));
        res.end(await fs.readFile(outputPath));
      } catch(error) {
        console.error('Archive report export:',error.message);
        res.statusCode=500;res.setHeader('Content-Type','application/json; charset=utf-8');
        res.end(JSON.stringify({error:'Excel 报告导出失败，请重试。'}));
      }
    });
  }};
}
