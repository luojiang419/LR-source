import test from 'node:test';
import assert from 'node:assert/strict';
import {reportRows} from '../server/archive-report.mjs';

const item={id:2,parent_sku:'LC78',sku:'LC7877847',source:'\\\\nas\\原片\\甲.CR3',reference:'\\\\nas\\现场\\甲.JPG',destination:'\\\\nas\\确版\\LC78\\LC7877847\\甲.CR3',xmp:'\\\\nas\\确版\\LC78\\LC7877847\\甲.xmp',archived_at:'2026-11-05T02:03:04.000Z',status:'verified'};
test('仓管报告保留母子 SKU、实际大档来源及完整目标路径',()=>{
  const row=reportRows({items:[item]})[0];
  assert.deepEqual(row.slice(0,3),['LC78','LC7877847','\\\\nas\\确版\\LC78\\LC7877847']);
  assert.equal(row[5],item.source);
  assert.notEqual(row[5],item.reference);
  assert.equal(row[6],item.destination);
  assert.equal(row[8],item.xmp);
});
test('归档日期使用记录时间并转换为上海日期，不取导出时间',()=>{
  assert.equal(reportRows({items:[item]})[0][4].toISOString(),'2026-11-05T10:03:04.000Z');
});
test('已经存在的目标原归档日期未知时留空，不补造今天日期',()=>{
  const row=reportRows({items:[{...item,status:'identical_verified_skip',archived_at:null}]})[0];
  assert.equal(row[4],null);assert.equal(row[6],item.destination);assert.match(row[9],/原归档日期未知/);
});
test('失败项不能把计划目标伪装成已确认归档后路径',()=>{
  const row=reportRows({items:[{...item,status:'verification_failed'}]})[0];
  assert.equal(row[2],'');assert.equal(row[4],null);assert.equal(row[6],'');assert.equal(row[8],'');assert.match(row[7],/失败/);assert.match(row[9],/计划目标/);
});
test('删除记录仅追溯现场来源，不生成目标、日期或配套 XMP',()=>{
  const row=reportRows({skipped_reference_records:[{...item,status:'post_deleted_skipped'}]},true)[0];
  assert.equal(row[2],'');assert.equal(row[4],null);assert.equal(row[5],item.reference);assert.equal(row[6],'');assert.equal(row[8],'');assert.match(row[7],/后期已删除/);
});
test('潜在公式前缀按字面文本保存，仓管标识不执行为公式',()=>{
  assert.equal(reportRows({items:[{...item,sku:'=1+1'}]})[0][1],"'=1+1");
});
test('归档前处理保留原大档路径，并在备注追溯整理后的实际路径',()=>{
  const record={...item,original_source:'\\\\nas\\散图\\甲.CR3',source:'\\\\nas\\原片\\LC78\\LC7877847\\甲.CR3'};
  const row=reportRows({preprocess:true,items:[record]})[0];
  assert.equal(row[5],record.original_source);
  assert.match(row[9],/归档前处理已开启/);
  assert.ok(row[9].includes(record.source));
});
