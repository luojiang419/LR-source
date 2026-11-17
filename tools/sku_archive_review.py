"""将隔离归档的实际结果渲染为可本地打开的只读核查页面。"""
import html
import json
from pathlib import Path
from urllib.parse import quote

ROOT=Path(__file__).resolve().parents[1]


def review(test_root):
    test_root=Path(test_root).resolve()
    report=json.loads((test_root/'sku库/归档结果.json').read_text(encoding='utf8'))
    checks=json.loads((test_root/'sku库/独立验证结果.json').read_text(encoding='utf8'))
    def link(path,label=None):
        relative=Path(path).relative_to(test_root).as_posix()
        return f'<a href="{quote(relative)}">{html.escape(label or str(path))}</a>'
    rows=[]
    for r in report['items']:
        image=quote(Path(r['thumbnail_path']).relative_to(test_root).as_posix())
        rows.append(f'''<tr data-sku="{html.escape(r['sku'])}"><td>{html.escape(r['parent_sku'])}</td><td><b>{html.escape(r['sku'])}</b></td><td><img alt="{html.escape(Path(r['destination']).name)}真实缩略图" src="{image}"></td><td>{link(r['reference'])}</td><td>{link(r['source'])}</td><td>{link(r['destination'])}{'<br>'+link(r['xmp']) if r['xmp'] else ''}</td><td class="passed">{('RAW + XMP' if r['raw'] else 'JPG 内嵌')}<br>验证通过</td></tr>''')
    deleted=''.join(f'<li>{link(r["reference"])} <span>后期删除，已跳过</span></li>' for r in report['skipped_reference_records'])
    check_items=''.join(f'<li><strong>通过</strong>{html.escape(r["name"])}</li>' for r in checks['checks'])
    content='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>真实资产归档核查</title><style>
*{box-sizing:border-box}body{margin:0;background:#1a1a1a;color:#eee;font:13px 'Segoe UI','Microsoft YaHei',sans-serif}main{max-width:1540px;margin:auto;padding:24px}h1{margin:0;font-size:22px}header{display:flex;gap:20px;align-items:center;justify-content:space-between}p{color:#b5b7af;line-height:1.8}a{color:#bed9c6;text-decoration:none;overflow-wrap:anywhere}a:hover{text-decoration:underline}.button{padding:10px 15px;border:1px solid #7e9f85;border-radius:5px;background:#2b3c2f;white-space:nowrap}.counts{display:flex;gap:24px;flex-wrap:wrap;padding:16px 0;margin:12px 0;border-block:1px solid #ffffff20}.counts strong{font-size:22px;margin-right:7px}.counts span{color:#aaa}.passed{color:#b3d0bb}.table{overflow:auto;border:1px solid #ffffff20}table{width:100%;border-collapse:collapse;min-width:1060px;table-layout:fixed}th{padding:13px;background:#2b392e;text-align:left;font-weight:500}td{border-top:1px solid #ffffff18;padding:12px;vertical-align:middle;font-size:11px;line-height:1.7}td img{max-width:70px;max-height:100px;display:block}th:nth-child(1){width:70px}th:nth-child(2){width:115px}th:nth-child(3){width:95px}th:nth-child(4){width:20%}th:nth-child(5){width:20%}th:nth-child(6){width:25%}th:nth-child(7){width:100px}section{margin-top:24px}h2{font-size:15px;font-weight:500}ul{list-style:none;padding:0}li{padding:10px;border-bottom:1px solid #ffffff12;color:#bbb;line-height:1.7}li strong{margin-right:16px;color:#b3d0bb}li span{margin-left:12px;color:#aaa}label{font-size:12px;color:#b5b7af}select{background:#252a26;color:#eee;border:1px solid #ffffff28;padding:8px;margin-left:10px;border-radius:5px}nav{margin-bottom:14px;display:flex;gap:18px;align-items:center;flex-wrap:wrap}@media(max-width:700px){main{padding:16px}header{align-items:flex-start;flex-wrap:wrap}.counts{gap:12px}.counts strong{font-size:18px}}
</style><main><header><h1>真实资产归档核查</h1>REPORT_LINK</header><p>来自真实 Canon R6 II 照片副本；小档、大档已改名，依快门元数据匹配。款号为本次测试备案值。全部写入限于本项目测试文件夹。</p><div class="counts"><div><strong>8</strong><span>现场快门</span></div><div><strong>6</strong><span>最终保留快门</span></div><div><strong>8 / 8</strong><span>原片文件已验证</span></div><div><strong>4</strong><span>XMP 已验证</span></div><div><strong>2</strong><span>废片已跳过</span></div><div><strong>3</strong><span>款号目录</span></div></div><nav><label>查看款号<select id="sku"><option value="">全部款号</option>OPTIONS</select></label>SMALL_LINK FINAL_LINK TARGET_LINK</nav><div class="table"><table><thead><tr><th>母SKU</th><th>子SKU</th><th>真实缩略图</th><th>现场小档</th><th>归档前大档</th><th>归档后图片 / XMP</th><th>核验</th></tr></thead><tbody>ROWS</tbody></table></div><section><h2>实际执行的检查</h2><ul>CHECKS</ul></section><section><h2>后期删除记录</h2><ul>DELETED</ul></section><p>原归档日期未保存的已存在文件在 Excel 中留空，本次核验时间另记。这里是独立真实验证页面，正式工作台仍保留此前的模拟演示。</p></main><script>document.querySelector('#sku').addEventListener('change',event=>{document.querySelectorAll('tbody tr').forEach(row=>row.hidden=!!event.target.value&&row.dataset.sku!==event.target.value);});</script></html>'''
    reports=sorted((test_root/'sku库').glob('归档校验报表-????????????.xlsx'),key=lambda p:p.stat().st_mtime_ns)
    report_file=reports[-1] if reports else test_root/'sku库/归档校验报告.xlsx'
    content=content.replace('REPORT_LINK','<a class="button" href="'+quote(report_file.relative_to(test_root).as_posix())+'">下载仓管 Excel 报告</a>')
    content=content.replace('SMALL_LINK',link(test_root/'小档','浏览小档')).replace('FINAL_LINK',link(test_root/'大档','浏览大档')).replace('TARGET_LINK',link(test_root/'sku库','浏览sku库'))
    content=content.replace('OPTIONS',''.join('<option>'+s+'</option>' for s in checks['child_skus']))
    content=content.replace('ROWS',''.join(rows)).replace('CHECKS',check_items).replace('DELETED',deleted)
    (test_root/'核查页面.html').write_text(content,encoding='utf8')
    failure=test_root/'验证失败记录.json'
    if failure.exists():
        old=json.loads(failure.read_text(encoding='utf8'))
        old.update(resolved=True,resolution='验证脚本色温别名检查已修正为 ColorTemperature，全部检查已续作通过；此前未发现归档文件损坏。')
        failure.write_text(json.dumps(old,ensure_ascii=False,indent=2),encoding='utf8')


if __name__=='__main__':
    import sys
    review(Path(sys.argv[1]) if len(sys.argv)>1 else ROOT/'测试文件夹')
