/* 自动范围与按次执行独立；百分比和耗时共用后端持久记录。 */
(() => {
  'use strict';
  if(!window.FramekeepIO)return;
  const $=id=>document.getElementById(id),api=FramekeepIO.api;
  const ocrLine=$('shooting-full-auto').closest('label'),switches=document.createElement('div');
  switches.className='shooting-automation-switches';ocrLine.before(switches);switches.append(ocrLine);
  const live=document.createElement('label');live.className='shooting-auto-line';
  live.innerHTML='<span>多维度解析</span><input id="shooting-multidimensional" class="toggle-switch" type="checkbox" role="switch" aria-label="现场多维度解析" disabled>';
  switches.append(live);
  const liveStatus=document.createElement('p');liveStatus.id='shooting-multidimensional-status';liveStatus.className='shooting-auto-status';liveStatus.setAttribute('role','status');$('shooting-auto-status').after(liveStatus);
  $('full-auto-archive-title').textContent='多维度解析';
  $('full-auto-ocr-title').textContent='OCR 识别';
  $('full-auto-archive-card').querySelector('.mouse-auto-eyebrow').textContent='图片资料自动解析与入库';
  $('full-auto-archive-help').textContent='开启后自动解析监控文件夹及资产归档中的图片。确认的 SKU 后台写入 JPG 内嵌元数据或 RAW 的 XMP 侧车，OCR 原文与视觉证据保存在数据库。仅开启选片页开关时，只解析随后新拍的图片。';
  const buttons={},stopButtons={},notes={};
  for(const [kind,toggle,parent] of [['ocr','full-auto-ocr','full-auto-ocr-card'],['vision','full-auto-archive','full-auto-archive-card']]){
    const actions=document.createElement('div');actions.className='workflow-settings-actions';$(toggle).before(actions);actions.append($(toggle));
    const button=document.createElement('button');button.type='button';button.id='workflow-'+kind+'-execute';button.className='bordered workflow-execute';button.textContent='手动执行';button.title='按次处理当前监控目录和资产归档图片，仅执行本项识别';actions.append(button);buttons[kind]=button;
    const stop=document.createElement('button');stop.type='button';stop.id='workflow-'+kind+'-stop';stop.className='bordered workflow-stop';stop.textContent='停止';stop.disabled=true;stop.title='停止派发新请求，保留已完成结果';stop.setAttribute('aria-label',kind==='ocr'?'停止 OCR':'停止多维度解析');actions.append(stop);stopButtons[kind]=stop;
    const note=document.createElement('p');note.id='workflow-'+kind+'-last';note.className='workflow-last-run';note.setAttribute('role','status');$(parent).append(note);notes[kind]=note;
    button.onclick=()=>action('/api/workflow/execute',{kind,session_id:window.FRAMEKEEP_SESSION?.id});
    stop.onclick=()=>action('/api/workflow/stop',{kind});
  }
  const taskProgress=document.createElement('section');taskProgress.id='workflow-run-progress';taskProgress.className='workflow-run-progress';taskProgress.hidden=true;taskProgress.setAttribute('aria-live','polite');
  const writebackNote=document.createElement('p');writebackNote.id='workflow-sku-writeback';writebackNote.className='workflow-last-run';$('full-auto-ocr-card').append(writebackNote);
  const writebackRetry=document.createElement('button');writebackRetry.type='button';writebackRetry.className='bordered';writebackRetry.textContent='重试 SKU 元数据写入';writebackRetry.hidden=true;writebackNote.after(writebackRetry);writebackRetry.onclick=()=>action('/api/workflow/sku-writeback/retry',{kind:'ocr'});
  $('full-auto-archive-card').append(taskProgress);
  let state=null,busy=false,loading=false,revision=0;
  const elapsed=value=>{const n=Math.max(0,Math.round(value||0));return n>=3600?`${Math.floor(n/3600)} 小时 ${Math.floor(n%3600/60)} 分钟`:n>=60?`${Math.floor(n/60)} 分 ${n%60} 秒`:`${n} 秒`};
  function paint(){
    if(!state)return;
    const config=state.config||{},available=state.available!==false;
    $('shooting-multidimensional').checked=!!(config.live_enabled||config.global_enabled);$('shooting-multidimensional').disabled=busy||!available||!!config.global_enabled;
    $('shooting-multidimensional').title=config.global_enabled?'全局自动已开启；在设置关闭后可单独控制现场新图':'仅解析开关开启后新拍或新接入监控的图片';
    $('full-auto-archive').checked=!!config.global_enabled;$('full-auto-archive').disabled=busy||!available;
    $('full-auto-archive-card').classList.toggle('is-enabled',!!config.global_enabled);
    liveStatus.textContent=config.global_enabled?'多维度：全局自动已开启，监控与归档图片持续解析':config.live_enabled?'多维度：现场已开启，仅解析随后新拍的图片':'多维度：已关闭';
    $('full-auto-archive-status').textContent=state.error|| (config.global_enabled?'自动已开启 · 监控目录与资产归档':'全局自动已关闭 · 现场开关可独立使用');
    const lines=[];
    const phases={image:'准备模型输入',cache_lookup:'读取本地输入缓存',cached_input:'直接复用已缓存图像',source_input:'读取源图供 OCR 识别',cached_result:'复用已返回的 OCR 文本',registering:'登记资产资料',validating:'校验图片版本',waiting_content:'等待相同图片结果',waiting_model:'等待模型空位',waiting_stage:'等待在途解码或其他解析收尾',inference:'模型识别中',saving_response:'保存模型返回',saving:'保存文本与核验资料'};
    for(const kind of ['ocr','vision']){
      const run=state.runs?.[kind],running=run?.status==='running',label=kind==='ocr'?'OCR':'多维度解析',button=buttons[kind];
      const active=(state.active||[]).filter(item=>item.kind===kind).sort((a,b)=>b.elapsed_seconds-a.elapsed_seconds),current=active[0];
      const stopping=!!config[kind+'_paused']&&active.length>0;
      button.disabled=busy||running||stopping||!available;button.classList.toggle('is-running',running&&!config[kind+'_paused']);button.classList.toggle('is-complete',run?.status==='completed');button.classList.toggle('has-failures',!!run?.failed);
      button.style.setProperty('--task-progress',(run?.percent||0)+'%');button.textContent=stopping?'正在停止':running?`执行中 ${run.percent}%`:run?.status==='stopped'?'继续执行':'手动执行';button.setAttribute('aria-label',`${label} ${button.textContent}`);button.setAttribute('aria-busy',String(running||stopping));
      stopButtons[kind].disabled=busy||!available||stopping||(!running&&!active.length);
      const inferring=active.filter(item=>item.phase==='inference').length;
      const detail=config[kind+'_paused']?' · 已暂停':current?` · 推理 ${inferring} · 准备/保存 ${active.length-inferring} · ${phases[current.phase]||'处理中'} ${elapsed(current.elapsed_seconds)}`:' · 等待处理';
      notes[kind].textContent=stopping?`${label}：正在停止 · 等待在途 ${active.length} 项收尾并保存结果`:run?(running?`${label}：已处理 ${run.done}/${run.total}（已入队）${run.discovered?'':' · 边识别边补充队列'}${detail}${run.failed?' · 失败 '+run.failed+' 项':''} · 已用 ${elapsed(run.elapsed_seconds)}`:`最近一次 ${label} ${run.status==='completed'?'完成':'已停止'} · 耗时 ${elapsed(run.elapsed_seconds)} · ${run.done}/${run.total} 项${run.failed?' · 失败 '+run.failed+' 项':''}`):'手动执行范围：当前监控目录和资产归档图片；仅执行本项，已有同版本结果自动跳过。';
      notes[kind].title=current?.name||'';
      if(run)lines.push(`${label} ${stopping?'正在停止':running?run.percent+'%':run.status==='completed'?'已完成':'已停止'} · ${run.done}/${run.total} 项 · ${elapsed(run.elapsed_seconds)}`);
    }
    const written=state.sku_writeback?.counts||{};
    writebackNote.textContent=`SKU 元数据${state.sku_writeback?.paused?' · 导入后已暂停':''} · 已写入 ${written.applied||0} · 排队 ${written.queued||0} · 写入中 ${written.writing||0} · 失败 ${written.failed||0}`;
    if(state.sku_writeback?.loading)writebackNote.textContent='SKU 元数据 · 正在读取队列';
    writebackNote.title=state.sku_writeback?.failed?.map(item=>item.sku+' '+item.reason).join('\n')||'';
    writebackRetry.hidden=!written.failed&&!state.sku_writeback?.paused;writebackRetry.disabled=busy;writebackRetry.textContent=state.sku_writeback?.paused?'启用 SKU 元数据补写':'重试 SKU 元数据写入';
    taskProgress.hidden=!lines.length;taskProgress.textContent=lines.join('　｜　');
  }
  async function refresh(){if(!['selection','progress','archive','settings'].includes(window.FramekeepWorkspace?.current()||'selection'))return;if(loading||busy)return;loading=true;const ticket=revision;try{const next=await api('/api/workflow');if(ticket===revision&&!busy){state=next;paint()}}catch{}finally{loading=false}}
  async function action(path,body){
    if(busy)return;revision++;busy=true;paint();
    try{state=await api(path,body);paint();document.dispatchEvent(new CustomEvent('framekeep:workflow-progress',{detail:state}))}
    catch(error){Framekeep.notify(error.message);notes[body.kind||'vision'].textContent=error.message}
    finally{busy=false;paint()}
  }
  $('shooting-multidimensional').onchange=e=>action('/api/workflow/config',{live_enabled:e.target.checked,session_id:window.FRAMEKEEP_SESSION?.id});
  $('full-auto-archive').onchange=e=>action('/api/workflow/config',{global_enabled:e.target.checked,session_id:window.FRAMEKEEP_SESSION?.id});
  document.addEventListener('framekeep:workspace',refresh);
  document.addEventListener('framekeep:workflow-progress',event=>{if(!busy){state=event.detail;paint()}});
  const timer=setInterval(()=>{if(!document.hidden)refresh()},1000);window.addEventListener('pagehide',()=>clearInterval(timer),{once:true});refresh();
})();
