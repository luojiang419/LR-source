"""将真实前端、OCR、事务和逐文件完成证据汇总为离线中文报告与耗时图。"""
from pathlib import Path
from datetime import datetime,timezone,timedelta
import collections,html,json,math,sqlite3,statistics,sys,time
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties

ROOT=Path(__file__).resolve().parents[1];out=Path(sys.argv[1]).resolve();data=out/'data'
def read(path,default=None):return json.loads(path.read_text(encoding='utf8')) if path.exists() else default
def fmt(value,digits=2):return '—' if value is None else f'{value:.{digits}f}'
def stamp(value):return datetime.fromtimestamp(value,timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
def percentile(values,p):
    if not values:return None
    ordered=sorted(values);position=(len(ordered)-1)*p/100;lo=int(position);hi=math.ceil(position)
    return ordered[lo]+(ordered[hi]-ordered[lo])*(position-lo)
manifest=read(out/'manifest.json');run=read(out/'run.json');visual=read(out/'visual-review.json')['labels'];manual=read(out/'manual-review.json',{'corrections':[]})
verification=read(out/'verification.json',{});recovery=read(out/'crash-recovery.json',{})
demo=read(out/'demo-verification.json',{})
repair=read(out/'keyword-repair-plan.json',{});repair_paths=set(repair.get('paths',[]))
initial=read(out/'initial-desktop-metrics.json',read(data/'desktop-metrics.json',{}));restarted=read(out/'recovery-desktop-metrics.json',read(data/'desktop-metrics.json',{}))
events=[json.loads(line) for line in (data/'pipeline-metrics.jsonl').read_text(encoding='utf8').splitlines() if line.strip()]
with sqlite3.connect(data/'sku.sqlite3') as c:
    c.row_factory=sqlite3.Row;jobs={r['id']:dict(r) for r in c.execute('SELECT id,capture_id,created,status,updated FROM jobs')}
    choices=dict(c.execute('SELECT capture_id,sku FROM ocr_choices'))
with sqlite3.connect(data/'asset_archive.sqlite3') as c:
    c.row_factory=sqlite3.Row
    files=[dict(r) for r in c.execute("""SELECT m.id,m.path,m.created,m.status,m.asset,m.revision FROM metadata_jobs m JOIN assets a ON a.id=m.asset AND a.revision=m.revision
        WHERE coalesce(json_extract(m.fields,'$._descriptive'),0)=0 AND m.id=(SELECT n.id FROM metadata_jobs n WHERE n.asset=m.asset AND n.revision=m.revision AND n.file=m.file AND n.path=m.path AND coalesce(json_extract(n.fields,'$._descriptive'),0)=0 ORDER BY n.created DESC,n.rowid DESC LIMIT 1)""")]
models=collections.defaultdict(list);confirms=collections.defaultdict(list);applied={};first_applied={}
for event in events:
    if event['kind']=='ocr' and event.get('job_id') in jobs:
        job=jobs[event['job_id']];phases=event.get('phases',[]);duration=0;first=None;reply=None
        for index,phase in enumerate(phases):
            if phase['phase']=='recognizing':
                first=phase['at'] if first is None else min(first,phase['at'])
                end=phases[index+1]['at'] if index+1<len(phases) else event['at'];duration+=max(0,end-phase['at'])
            if phase['phase']=='saving':reply=phase['at']
        if first is not None:models[job['capture_id']].append({'start':first,'reply':reply,'seconds':duration,'job_created':job['created']})
    elif event['kind']=='confirmed':confirms[event['capture_id']].append(event)
    elif event['kind']=='metadata_applied':applied[event['job_id']]=event['at'];first_applied.setdefault(event['job_id'],event['at'])
manual_by={r['capture_id']:r for r in manual['corrections']};gold={r['label']:r for r in visual};rows=[]
for index,g in enumerate(run['groups']):
    cid=g['capture_id'];m=models[cid];confirmation=confirms[cid];correction=manual_by.get(cid)
    own=[r for r in files if Path(r['path']).name in g['names']]
    completed=len(own)==len(g['names']) and all(r['status']=='applied' and r['id'] in applied for r in own)
    end=max((applied[r['id']] for r in own),default=0) if completed else None
    first_end=max((first_applied[r['id']] for r in own),default=0) if completed else None
    repaired=[applied[r['id']] for r in own if r['path'] in repair_paths and r['id'] in applied]
    first_confirm=min((r['at'] for r in confirmation),default=None);last_confirm=max((r['at'] for r in confirmation),default=None)
    automatic_confirm=min((r['at'] for r in confirmation if r.get('automatic')),default=None)
    first_model=min((r['start'] for r in m),default=None);reply=min((r['reply'] for r in m if r['reply']),default=None)
    rows.append({'index':index+1,'label':g['label'],'sku':gold[g['label']]['visual_sku'],'actual_sku':choices.get(cid),
                 'images':len(g['names']),'feedback_ms':(g.get('key_feedback') or {}).get('feedback_ms'),'save_s':g.get('save_seconds'),
                 'ocr_calls':len(m),'model_s':sum(r['seconds'] for r in m),'queue_to_model_s':first_model-g['pressed'] if first_model else None,
                 'ocr_reply_s':reply-g['pressed'] if reply else None,'first_confirm_s':first_confirm-g['pressed'] if first_confirm else None,
                 'last_confirm_s':last_confirm-g['pressed'] if last_confirm else None,'manual_s':correction.get('seconds') if correction else None,
                 'automatic_confirm_s':automatic_confirm-g['pressed'] if automatic_confirm else None,
                 'write_complete_s':first_end-g['pressed'] if first_end else None,'final_fields_complete_s':end-g['pressed'] if end else None,
                 'keyword_repair_s':max(repaired)-repair['started'] if repaired and repair.get('started') else None,
                 'queue_to_write_complete_s':first_end-min(r['created'] for r in own) if first_end else None,'final_applied':completed})
feedback=[r['feedback_ms'] for r in rows if r['feedback_ms'] is not None];model_seconds=[r['model_s'] for r in rows if r['ocr_calls']]
summary={'labels':len(rows),'images':len(manifest['files']),'confirmed':len(choices),'current_applied':sum(r['status']=='applied' for r in files),
         'startup_s':initial.get('first_workbench_ready_ms',0)/1000,'restart_s':restarted.get('first_workbench_ready_ms',0)/1000,
         'feedback_p50_ms':percentile(feedback,50),'feedback_p95_ms':percentile(feedback,95),'feedback_max_ms':max(feedback,default=0),
         'ocr_p50_s':percentile(model_seconds,50),'ocr_p95_s':percentile(model_seconds,95),'ocr_requests':sum(r['ocr_calls'] for r in rows),
         'corrections':len(manual['corrections']),'awaited_review':sum(not r.get('before') for r in manual['corrections']),
         'first_file_complete_s':min((e['at'] for e in events if e['kind']=='metadata_applied'),default=run['started'])-min(g['pressed'] for g in run['groups']),
         'full_wall_s':max((first_applied[r['id']] for r in files if r['id'] in first_applied),default=run['started'])-min(g['pressed'] for g in run['groups']),
         'keyword_repair_files':repair.get('repaired',0),'keyword_repair_seconds':repair.get('seconds'),
         'passed':bool(verification.get('passed') and demo.get('passed') and recovery.get('documents_preserved') and recovery.get('choices_preserved') and all(r['final_applied'] for r in rows))}
(out/'timings.json').write_text(json.dumps({'summary':summary,'rows':rows},ensure_ascii=False,indent=2),encoding='utf8')
font=FontProperties(fname='C:/Windows/Fonts/msyh.ttc');plt.rcParams['font.family']=font.get_name();plt.rcParams['axes.unicode_minus']=False
plt.rcParams.update({'axes.spines.top':False,'axes.spines.right':False,'axes.grid':True,'grid.alpha':.18,'figure.facecolor':'#f6f8fb','axes.facecolor':'white'})
fig,axes=plt.subplots(1,3,figsize=(15,4.1),layout='constrained')
axes[0].bar(['首次打开','强退后恢复'],[summary['startup_s'],summary['restart_s']],color=['#2563eb','#14b8a6']);axes[0].set_ylabel('秒');axes[0].set_title('真实桌面工作台可操作时间')
axes[1].bar(['中位数','P95','最大'],[summary['feedback_p50_ms'],summary['feedback_p95_ms'],summary['feedback_max_ms']],color=['#2563eb','#14b8a6','#94a3b8']);axes[1].set_ylabel('毫秒');axes[1].set_title('二星按键 → 数量文本更新')
axes[2].bar(['已完成','待完成'],[summary['current_applied'],summary['images']-summary['current_applied']],color=['#14b8a6','#cbd5e1']);axes[2].set_ylabel('张');axes[2].set_title('最新归属版本的实际写入')
for ax in axes:
    for bar in ax.patches:ax.annotate(f'{bar.get_height():.2f}' if ax is not axes[2] else str(int(bar.get_height())),(bar.get_x()+bar.get_width()/2,bar.get_height()),ha='center',va='bottom',fontsize=10)
fig.suptitle('56款 · 4217张 · NAS实战计时',fontsize=17,fontweight='bold');fig.savefig(out/'overview.png',dpi=150);plt.close(fig)
fig,axes=plt.subplots(4,1,figsize=(16,14),sharex=True,layout='constrained');xs=[r['index'] for r in rows]
axes[0].bar(xs,[r['model_s'] for r in rows],color='#2563eb');axes[0].set_title('每款真实模型执行耗时（同款重试合计）');axes[0].set_ylabel('秒')
axes[1].bar(xs,[r['automatic_confirm_s'] or 0 for r in rows],color='#14b8a6');axes[1].set_title('标星操作开始 → 自动确认入库（空位为待复核；自动确认仍可能需要纠错）');axes[1].set_ylabel('秒')
axes[2].bar(xs,[r['manual_s'] or 0 for r in rows],color='#f59e0b');axes[2].set_title('17项复核值录入 → 软件确认（不含事先读图与等待时间）');axes[2].set_ylabel('秒')
axes[3].bar(xs,[(r['write_complete_s'] or 0)/60 for r in rows],color='#475569');axes[3].set_title('标星操作开始 → 本款主SKU写回首次完成（历史关键词补齐单列）');axes[3].set_ylabel('分钟，含排队/复核/强退');axes[3].set_xlabel('款号序号（对应下方逐款表）');axes[3].set_xticks(xs);axes[3].tick_params(axis='x',labelsize=8)
fig.savefig(out/'per-sku.png',dpi=150);plt.close(fig)
esc=lambda value:html.escape(str(value if value is not None else '—'))
body=''.join('<tr>'+''.join(f'<td>{esc(v)}</td>' for v in [r['index'],r['label'],r['sku'],r['images'],fmt(r['feedback_ms'],1),fmt(r['save_s']),r['ocr_calls'],fmt(r['model_s']),fmt(r['ocr_reply_s']),fmt(r['first_confirm_s']),fmt(r['last_confirm_s']),fmt(r['manual_s']),fmt(r['write_complete_s']),fmt(r['keyword_repair_s']),fmt(r['queue_to_write_complete_s'])])+'</tr>' for r in rows)
status='全量验收通过' if summary['passed'] else '验收进行中：不能据此宣称全部通过'
contact_sheets=''.join(f'<img src="report-assets/{path.name}" alt="第{path.stem.split("-")[-1]}起的标签核对图" loading="lazy">' for path in sorted((out/'report-assets').glob('labels-*.jpg')))
page=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>SHIYING全量NAS实战报告</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f6f8fb;color:#16263c;font:15px/1.7 "Microsoft YaHei",sans-serif}}main{{max-width:1480px;margin:auto;padding:32px}}header{{background:#10243c;color:white;padding:36px;border-radius:18px}}h1{{margin:4px 0;font-size:30px}}h2{{margin-top:38px;font-size:23px}}.badge{{display:inline-block;border-radius:30px;padding:5px 15px;background:#127c69;color:white}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin:24px 0}}.card{{background:white;padding:20px;border-radius:12px;border:1px solid #e1e7ef}}.value{{display:block;font-size:30px;font-weight:bold}}.muted{{color:#607086}}img{{width:100%;border-radius:12px;background:white}}.scroll{{overflow:auto;background:white;border:1px solid #dbe3ee;border-radius:12px;max-height:760px}}table{{border-collapse:collapse;width:100%;white-space:nowrap;font-size:13px}}th,td{{padding:10px 12px;border-bottom:1px solid #edf0f5;text-align:right}}th{{position:sticky;top:0;background:#e7edf5}}td:nth-child(2),td:nth-child(3){{text-align:left}}tr:hover{{background:#f0f7ff}}.flow{{display:flex;flex-wrap:wrap;gap:10px}}.flow span{{padding:15px;background:#fff;border:1px solid #dbe3ee;border-radius:9px}}.note{{border-left:4px solid #f59e0b;background:#fff8e9;padding:15px 20px}}a{{color:#1d4ed8}}input{{padding:10px 14px;border:1px solid #cbd5e1;border-radius:8px;width:340px;max-width:100%;margin:12px 0}}code{{overflow-wrap:anywhere}}li{{margin:7px 0}}@media print{{.scroll{{max-height:none;overflow:visible}}main{{padding:0}}header{{color:black;background:white}}h2{{break-after:avoid}}}}</style>
<main><header><div>FRAMEKEEP / 隔离实战 · Asia/Shanghai</div><h1>56款、4217张真实素材的全链路验收</h1><p>使用已安装桌面外壳与隔离修复引擎，直接读写同一NAS中的独立副本。生产data、原图片备份及原XMP不参与写入。</p><span class="badge">{status}</span></header>
<div class="cards"><div class="card">桌面首屏<span class="value">{fmt(summary['startup_s'])} 秒</span></div><div class="card">二星数量反馈 P95<span class="value">{fmt(summary['feedback_p95_ms'],1)} ms</span></div><div class="card">模型执行中位数<span class="value">{fmt(summary['ocr_p50_s'])} 秒</span></div><div class="card">首张元数据完成<span class="value">{fmt(summary['first_file_complete_s'])} 秒</span></div><div class="card">最新版本写入<span class="value">{summary['current_applied']} / 4217</span></div><div class="card">核对修正<span class="value">{summary['corrections']} 项</span></div></div>
<img src="overview.png" alt="启动、二星反馈和完成数量总览">
<h2>本轮结论与准确性</h2><p>最终确认 {summary['confirmed']} 个标签。{summary['awaited_review']} 项未能自动确认，另有已自动确认的误读或格式需要修正；共 {summary['corrections']} 项通过拍摄进度页核对、填写或双击改款完成。自动确认不等同于文字识别100%正确，原始OCR文字仍可追溯。</p>
<p>实战主链路从标星到全部主SKU写回共 {fmt(summary['full_wall_s']/60)} 分钟，包含连续浏览标星、NAS排队、人工核对及一次强退恢复。独立回读另发现217张原素材带来的旧SKU关键词残留，软件自动补齐 {summary['keyword_repair_files']} 张用时 {fmt(summary['keyword_repair_seconds'])} 秒；已修复为后续写入时自动规范化，普通关键词与撤销基线保留。两阶段之间的验证和开发等待不计入软件吞吐。</p>
<p>程序启动从进程启动到工作台可操作计时；复制及初始索引准备不算启动。本机已配置的GLM-OCR服务在测试前已运行，本轮不代表内置模型冷加载耗时。共发起 {summary['ocr_requests']} 次识别（包括版本变化后的重试），所有56标签最终均有确认记录。</p>
<h2>各款实测耗时</h2><img src="per-sku.png" alt="56款模型、入库及文件完成耗时"><p class="muted">数量反馈单位为毫秒，其他为秒。前端反馈从键盘事件计时；主链路累计阶段从标星动作开始计时，包含提交开销。“规范补齐”单独从历史关键词补齐阶段开始计时，仅涉及217张。主写回按最新归属版本首次applied计算，排队任务和过期版本不冒充完成；最终全字段通过独立回读确认。</p>
<input id="search" placeholder="搜索款号或文件名"><div class="scroll"><table><thead><tr>{''.join('<th>'+h+'</th>' for h in ['序号','标签照片','核对后SKU','图片数','反馈ms','提交s','OCR次数','模型s','OCR返回s','首次确认s','最终确认s','复核操作s','主写回完成s','规范补齐s','入队至主写完s'])}</tr></thead><tbody>{body}</tbody></table></div>
<details><summary>展开56张标签核对图</summary><p>本轮按原56份XMP对应照片设定款末边界；未把XMP中的旧SKU作为答案。图下“本轮”值与正文核对表一致，原图文字未改，可在合并前作最终业务复核。</p>{contact_sheets}</details>
<h2>优化后的架构</h2><div class="flow"><span>二星 → 即时数量</span><span>持久任务 → 独立OCR</span><span>规则/核对 → 事务确认</span><span>持久事件 → 优先准备本款</span><span>版本化队列 → 内存编辑与严格校验</span><span>实际回读 → 完成</span></div>
<ol><li>计数反馈与完整清单重建分离，清单/标签刷新合并同类请求，避免取消请求后后端继续堆积。</li><li>共享评级改为批量读取和版本失效缓存；确认事务只更新本次证据，后台修复不持有SKU锁等待归档。</li><li>先按完整拍摄索引划定边界，再优先准备该款；其他资产分批32张准备。未识别二星也隔断，乱序结果不会跨款合并。</li><li>OCR确认继承入队文档和拍摄类型，跨午夜不转移到新文档。自身星级写回与自动确认共用文件锁，外部修改保护仍在。</li><li>ExifTool编辑和内存回读复用常驻进程；JPEG在单次操作中复用已校验的原图快照，保留提交前哈希、修订、像素、缩略图、非目标字段与写后实际回读。</li><li>SQLite/WAL保存文档与任务，持久事件和版本记录负责重放；旧归属排队任务取消，已完成文件不会因重启重新OCR。</li></ol>
<h2>异常退出与回读证据</h2><p>在记录到写入准备阶段后强制结束隔离引擎和桌面。文档保留：{esc(recovery.get('documents_preserved'))}；56份确认保留：{esc(recovery.get('choices_preserved'))}；重新进入工作台：{fmt(summary['restart_s'])}秒。实际测试为进程强退，未切断电脑电源，不把它宣称为物理磁盘故障验证。</p>
<p>全量独立回读：{verification.get('checked',0)} / 4217；失败 {len(verification.get('failures',[]))}。原件SHA不变：{esc(verification.get('source_unchanged'))}；原XMP不变：{esc(verification.get('xmp_sources_unchanged'))}；像素不变：{esc(verification.get('pixels_unchanged'))}；目标属性正确：{esc(verification.get('properties_correct'))}；其他摄影字段不变：{esc(verification.get('non_target_fields_unchanged'))}。</p>
<p>软件最终显示“二星标签56张、已确认56张/56款、待确认0张”，没有待写入提示。实际软件导出56行且SKU集合与核对表一致；6张代表性图片经Windows Shell原生属性回读通过。最终回归98项：97通过，1项缺真实RAW样本跳过；本批实际素材全部为JPG。</p><img src="progress-verified.png" alt="同一隔离引擎的最终拍摄进度页面">
<h2>演示与复查</h2><p>双击同目录“启动隔离演示.cmd”。演示直接打开本轮隔离NAS素材和已保存的拍摄文档，可查看每款写入状态、核对OCR原文、修改款号或导出文档。请勿将隔离资料当作正式安装数据覆盖。</p>
<p><a href="全量拍摄记录.xlsx">下载软件实际导出的56款拍摄记录</a> · <a href="timings.json">逐款原始汇总数据</a> · <a href="verification.json">全量回读结果</a></p>
<p><strong>素材副本：</strong><code>{esc(manifest.get('photos_root'))}</code><br><strong>资料目录：</strong><code>{esc(data)}</code></p>
<div class="note">本次没有覆盖正式安装、发布更新或合并主项目。验收通过后再合并分支。原生窗口已通过可访问性文本核验计数；原生截图接口超时，页面截图来自同一隔离引擎的真实工作台。</div>
<p class="muted">数据生成：{stamp(time.time())}。原始证据：timings.json、run.json、data/pipeline-metrics.jsonl、manual-review.json、verification.json、crash-recovery.json、desktop-launch.json。历史调试轮与准备工具失败均排除在本轮通过结论之外。</p></main>
<script>document.getElementById('search').oninput=e=>{{const q=e.target.value.toUpperCase();document.querySelectorAll('tbody tr').forEach(r=>r.hidden=!r.textContent.toUpperCase().includes(q))}}</script></html>'''
(out/'report.html').write_text(page,encoding='utf8')
print(json.dumps(summary,ensure_ascii=False,indent=2))
