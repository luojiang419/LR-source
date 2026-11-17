import test from 'node:test';
import assert from 'node:assert/strict';
import {metadataFingerprint, matchMetadata} from '../src/metadata-identity.js';

const jpg = {Model: 'Canon EOS R6 Mark II', SerialNumber: '00123456', DateTimeOriginal: '2026:11:05 10:32:18', SubSecTimeOriginal: '230', FileNumber: '100-6021'};
const raw = {Model: 'CANON EOS R6 MARK II', BodySerialNumber: '123456', SubSecDateTimeOriginal: '2026-11-05T10:32:18.23', ImageNumber: '100-6021'};

test('JPG 与 RAW 按相同快门元数据自动对应，不要求文件内容或名称相同', () => {
  const reference = {id: 1, name: 'small-copy.JPG', metadata: jpg, sku: 'LC7979103'};
  assert.equal(metadataFingerprint(jpg), metadataFingerprint(raw));
  assert.equal(matchMetadata({...raw, FileName: 'renamed-original.CR3'}, [reference]).source, reference);
});
test('连拍同秒但子秒和原始照片编号不同，分别自动匹配', () => {
  const next = {...jpg, SubSecTimeOriginal: '240', FileNumber: '100-6022'};
  const references = [{id: 1, metadata: jpg}, {id: 2, metadata: next}];
  assert.equal(matchMetadata(raw, references).source.id, 1);
  assert.equal(matchMetadata(next, references).source.id, 2);
});
test('不同相机的同时间同编号不能混配', () => {
  assert.equal(matchMetadata({...raw, BodySerialNumber: '654321'}, [{metadata: jpg}]).status, 'unmatched');
});
test('缺少身份字段不会猜配或改用文件名、画面相似度', () => {
  const {SubSecTimeOriginal, ...incomplete} = jpg;
  assert.equal(metadataFingerprint(incomplete), '');
  assert.equal(matchMetadata(incomplete, [{metadata: jpg}]).status, 'unmatched');
});
test('子秒前导零保留，.023 与 .23 不视作同一次快门', () => {
  assert.notEqual(metadataFingerprint(jpg), metadataFingerprint({...jpg, SubSecTimeOriginal: '023'}));
});
test('元数据完全冲突的多条记录保持待复核，不自动取首条', () => {
  const conflict = matchMetadata(raw, [{id: 1, metadata: jpg, sku: 'LC7877847'}, {id: 2, metadata: jpg, sku: 'LC7979103'}]);
  assert.equal(conflict.status, 'review');
  assert.equal(conflict.source, null);
});
test('同一次快门的重复记录且款号等信息一致时自动合并', () => {
  const duplicate = matchMetadata(raw, [{id: 1, metadata: jpg, sku: 'LC7979103', rating: 3}, {id: 2, metadata: jpg, sku: 'LC7979103', rating: 3}]);
  assert.equal(duplicate.status, 'matched');
});
