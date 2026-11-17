// 演示使用已提取的相机元数据；不读取文件内容、文件名或图像像素。
const text = value => String(value ?? '').trim();
const number = value => /^\d+$/.test(text(value)) ? text(value).replace(/^0+(?=\d)/, '') : text(value);

export function metadataFingerprint(metadata = {}) {
  const model = text(metadata.Model).replace(/\s+/g, ' ').toLowerCase();
  const serial = number(metadata.SerialNumber || metadata.BodySerialNumber);
  const stamp = text(metadata.SubSecDateTimeOriginal || metadata.DateTimeOriginal);
  const parsed = /^(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?/.exec(stamp);
  const sub = text(metadata.SubSecTimeOriginal ?? parsed?.[7]);
  const image = number(metadata.FileNumber || metadata.ImageNumber);
  if (!model || !serial || /^0+$/.test(serial) || !parsed || !/^\d+$/.test(sub) || !image) return '';
  const date = `${parsed[1]}-${parsed[2]}-${parsed[3]}T${parsed[4]}:${parsed[5]}:${parsed[6]}`;
  // .230 和 .23 是同一子秒；.023 与 .23 不同，保留前导零。
  const fraction = sub.replace(/0+$/, '') || '0';
  return `capture-v1:${JSON.stringify([model, serial, date, fraction, image])}`;
}

export function matchMetadata(target, references) {
  const fingerprint = metadataFingerprint(target);
  if (!fingerprint) return {status: 'unmatched', fingerprint: '', source: null, reason: '元数据身份字段不完整，需核实原文件'};
  const candidates = references.filter(record => metadataFingerprint(record.metadata) === fingerprint);
  const assignments = new Set(candidates.map(record => JSON.stringify([record.sku || '', record.scope || '', record.rating ?? null])));
  if (candidates.length && assignments.size === 1) return {status: 'matched', fingerprint, source: candidates[0], reason: '元数据唯一指纹匹配 · 序列号 / 时间与子秒 / 照片编号'};
  return {status: candidates.length ? 'review' : 'unmatched', fingerprint, source: null,
    reason: candidates.length ? '同一元数据身份对应多条记录，需核实冲突' : '未找到相同元数据身份，需核实来源记录'};
}
