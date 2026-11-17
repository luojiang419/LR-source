import test from 'node:test';
import assert from 'node:assert/strict';
import {archiveReportFilename} from '../src/report-filename.js';

test('导出名按北京时间包含年月日时分，不包含秒',()=>{
  assert.equal(archiveReportFilename(new Date('2026-11-06T01:05:59Z')),'归档校验报表-202611060905.xlsx');
});
test('北京时间跨日午夜用新日期及0000，不依赖系统时区',()=>{
  assert.equal(archiveReportFilename(new Date('2026-11-06T16:00:00Z')),'归档校验报表-202611070000.xlsx');
});
test('无效日期不会导出不完整名称',()=>{
  assert.throws(()=>archiveReportFilename(new Date('invalid')),RangeError);
});
