/* 正式资产归档；所有统计与结果来自后端任务，不模拟进度或文件。 */
(() => {
  'use strict';
  if(!window.FramekeepIO||!document.getElementById('catalog-analysis'))return;
  const api=FramekeepIO.api,$=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const css=document.createElement('link');css.rel='stylesheet';css.href='folder-archive.css';document.head.append(css);
  const button=document.createElement('button');button.id='catalog-folder-archive';button.type='button';button.className='bordered';button.textContent='资产归档';button.setAttribute('aria-haspopup','dialog');$('catalog-analysis').after(button);
  const dialog=document.createElement('dialog');dialog.id='folder-archive-dialog';dialog.setAttribute('aria-labelledby','folder-archive-title');
  const statusSvg=`<svg class="fa-status-art" viewBox="0 0 148 116" aria-hidden="true" focusable="false">
    <defs><linearGradient id="fa-card-fill" x2="1" y2="1"><stop stop-color="var(--theme-panel)"/><stop offset="1" stop-color="var(--theme-soft)"/></linearGradient><linearGradient id="fa-scan-fill" x2="0" y2="1"><stop stop-color="currentColor" stop-opacity="0"/><stop offset="1" stop-color="currentColor" stop-opacity=".24"/></linearGradient><clipPath id="fa-photo-clip"><rect x="23" y="24" width="57" height="65" rx="7"/></clipPath></defs>
    <circle class="fa-orbit" cx="74" cy="57" r="47" fill="none" stroke="currentColor" stroke-opacity=".09" stroke-dasharray="2 8"/>
    <path d="M20 92 C59 111 107 105 134 69" class="fa-transfer-track" fill="none" stroke="currentColor" stroke-opacity=".18" stroke-dasharray="2 5"/>
    <g class="fa-photo-back"><rect x="21" y="17" width="57" height="70" rx="8" fill="url(#fa-card-fill)" stroke="currentColor" stroke-opacity=".28" transform="rotate(-10 49 52)"/></g>
    <rect x="23" y="24" width="57" height="65" rx="7" fill="url(#fa-card-fill)" stroke="currentColor" stroke-opacity=".66"/>
    <rect x="30" y="31" width="43" height="39" rx="3" fill="currentColor" fill-opacity=".07"/>
    <circle cx="61" cy="41" r="4" fill="currentColor" fill-opacity=".56"/>
    <path d="M31 63 L43 48 L54 60 L61 52 L72 65" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" stroke-opacity=".75"/>
    <path d="M32 77 H52 M32 81 H43" stroke="currentColor" stroke-opacity=".36" stroke-linecap="round"/>
    <g clip-path="url(#fa-photo-clip)"><g class="fa-scan-beam"><rect x="23" y="24" width="57" height="22" fill="url(#fa-scan-fill)"/><path d="M24 46 H79" stroke="currentColor" stroke-opacity=".8"/></g></g>
    <g class="fa-file-traveler"><rect x="57" y="66" width="15" height="19" rx="3" fill="var(--theme-panel)" stroke="currentColor" stroke-opacity=".8"/><path d="M61 72 H68 M61 76 H66" stroke="currentColor" stroke-width="1"/></g>
    <g class="fa-folder-art"><path d="M78 64 V53 Q78 49 82 49 H95 L101 55 H128 Q133 55 133 60 V91 Q133 96 128 96 H82 Q78 96 78 92Z" fill="var(--theme-panel)" stroke="currentColor" stroke-opacity=".5"/>
      <path d="M77 65 Q77 61 82 61 H132 Q137 61 136 66 L131 93 Q130 97 126 97 H81 Q77 97 77 93Z" fill="currentColor" fill-opacity=".1" stroke="currentColor" stroke-opacity=".72"/>
      <g class="fa-folder-dots" fill="currentColor"><circle cx="97" cy="80" r="2"/><circle cx="106" cy="80" r="2"/><circle cx="115" cy="80" r="2"/></g>
      <path class="fa-success-mark" d="M96 79 L103 86 L117 72" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
      <g class="fa-pause-mark" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="M102 73 V86 M112 73 V86"/></g>
      <path class="fa-error-mark" d="M102 74 L113 85 M113 74 L102 85" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/>
    </g></svg>`;
  dialog.innerHTML=`<header class="fa-header"><div><h2 id="folder-archive-title">资产归档</h2><small id="fa-mode-title">全部下级 SKU</small></div><nav class="fa-steps" aria-label="归档步骤"><button data-fa-step="0">1 位置与规则</button><button data-fa-step="1">2 匹配与复核</button><button data-fa-step="2">3 归档预览</button><button data-fa-step="3">4 复制与校验</button></nav><dl id="fa-metrics"></dl><button id="fa-close" aria-label="收起资产归档">×</button></header>
    <div class="fa-body"><section id="fa-settings"><div class="fa-scope"><strong id="fa-scope"></strong><small>递归包含全部下级目录与 SKU，不受搜索、分页或展开状态影响</small></div><label for="fa-target">归档根目录</label><div class="fa-path"><input id="fa-target" autocomplete="off" placeholder="本机路径、映射盘或 UNC 路径"><button data-fa-browse="fa-target">选择文件夹</button></div>
      <label class="fa-preprocess"><input id="fa-preprocess" type="checkbox" role="switch" checked><span><strong>归档前处理</strong><small id="fa-preprocess-copy">仅整理本次匹配的大小档，已整理目录直接复用，再复制归档</small></span></label>
      <label class="fa-preprocess"><input id="fa-fast" type="checkbox" checked><span><strong>极速归档</strong><small>省去整批预读和重复扫描；复制时计算源摘要，目标只读回一次。关闭后使用完整校验。</small></span></label><label for="fa-route">归档方式</label><select id="fa-route"><option value="loose">现场散图 → 匹配最终大档</option><option value="folders">已有款号文件夹 → 直接归档</option></select>
      <div id="fa-final-field"><label for="fa-final">最终大档目录</label><div class="fa-path"><input id="fa-final" autocomplete="off"><button data-fa-browse="fa-final">选择文件夹</button></div><p>仅归档此目录及下级实际保留的照片，包含其中的二星标签图；不从小档补图。已整理目录可选子 SKU、母 SKU 或总目录。</p></div>
      <label class="fa-preprocess"><input id="fa-exclude-unregistered" type="checkbox" checked><span><strong>排除未入库照片</strong><small>最终照片有完整相机身份，但无可靠款号归属时视为未选用，包括已匹配到小档但尚未确认SKU的照片；原件保留，排除明细写入报告。身份缺失或归属冲突仍需复核。</small></span></label><label for="fa-parent-length">母 SKU 规则</label><div class="fa-prefix">子 SKU 前 <input id="fa-parent-length" type="number" min="2" max="6" value="4" aria-label="母SKU位数"> 位</div></section>
      <section id="fa-progress" hidden><div class="fa-progress-layout"><div class="fa-progress-visual">${statusSvg}</div><div class="fa-progress-content"><strong id="fa-status" role="status"></strong><p id="fa-file"></p><progress id="fa-bar" max="100" value="0" aria-label="当前阶段进度"></progress><small id="fa-counts"></small></div></div></section>
      <p id="fa-connection" class="fa-connection" role="status" hidden></p><p id="fa-error" class="fa-error" role="alert" hidden></p><section id="fa-results" hidden><p id="fa-plan-copy"></p><div class="fa-table-wrap"><table><thead><tr><th>母 SKU / 子 SKU</th><th>归档前来源</th><th>整理后来源</th><th>归档后路径</th><th>类型 / 状态</th></tr></thead><tbody id="fa-items"></tbody></table></div><div class="fa-pages"><button id="fa-prev">上一页</button><span id="fa-page"></span><button id="fa-next">下一页</button></div><details id="fa-excluded" hidden><summary>未入库排除记录</summary><div></div></details><details id="fa-deleted"><summary>后期删除记录</summary><div></div></details></section></div>
    <footer class="fa-footer"><span id="fa-footer-note">确认前仅匹配和预览，不整理或复制文件</span><div><button id="fa-new" hidden>新建归档</button><button id="fa-back" hidden>上一步</button><button id="fa-stop" hidden>结束任务</button><button id="fa-export" hidden>导出校验报告</button><button id="fa-primary" class="primary">匹配并预览</button></div></footer>`;
  document.body.append(dialog);
  let scope=null,job=null,step=0,offset=0,busy=false,timer=null,completedNotification='',connectionPending=false,retryDelay=700;
  const stateRequests=new Map();
  const transient=error=>['TimeoutError','AbortError'].includes(error?.name)||/超时|连接|Failed to fetch|NetworkError|network|fetch failed|database is locked|ERR_/i.test(String(error?.message||error));
  function disconnected(){connectionPending=true;retryDelay=Math.min(5000,Math.max(1500,retryDelay*1.5));paint();}
  const active=()=>['preparing','queued','running','paused'].includes(job?.status);
  const fail=error=>{const el=$('fa-error');el.textContent=error?.message||String(error);el.hidden=false;};
  const remember=()=>{try{localStorage.setItem('framekeep.folder-archive.job',job?.id||'')}catch{}};
  function settings(){
    $('fa-final-field').hidden=$('fa-route').value==='folders';
    $('fa-preprocess-copy').textContent=$('fa-preprocess').checked?'仅整理本次匹配的大小档，已整理目录直接复用，再复制归档':'大小档保持原目录，仅在归档位置创建母 SKU / 子 SKU';
  }
  function paint(){
    const status=job?.status,finished=status==='completed',working=active(),ready=status==='ready';
    $('fa-settings').hidden=step!==0||working;
    $('fa-progress').hidden=!job||step===0||step===2&&ready;
    $('fa-results').hidden=!job?.items?.length||step===0;
    $('fa-connection').hidden=!connectionPending;$('fa-connection').textContent='进度连接暂时中断，正在自动重连；当前显示上次记录。';
    $('fa-progress').dataset.state=connectionPending?'reconnecting':finished?'complete':status==='paused'?'paused':['failed','interrupted','cancelled'].includes(status)?'error':working?'working':'ready';
    $('fa-status').textContent=job?.message||(connectionPending?'正在重新连接归档任务…':'');$('fa-file').textContent=job?.path||'';
    if(working&&job?.indeterminate)$('fa-bar').removeAttribute('value');else $('fa-bar').value=job?.percent||0;
    const parallel=['sources','final_metadata','matching','copying_fast'].includes(job?.stage)&&job?.workers?` · ${job.workers} 路并行`:'';
    const elapsed=job?`${parallel} · 已用时 ${Math.floor((job.elapsed_seconds||0)/60)}分${(job.elapsed_seconds||0)%60}秒`:'';
    $('fa-counts').textContent=job?.total?`${job.done||0} / ${job.total} · ${job.percent||0}%${elapsed}`:working?(job.stage?.startsWith('scanning')?`已发现 ${job.discovered??job.done??0} 个图片文件 · 已检查 ${job.directories||0} 个目录${elapsed}`:`已检查 ${job.done||0} 项${elapsed}`):'';
    if(job?.stage==='copying_fast'&&job.bytes_total){const mb=n=>(n/1048576).toFixed(1);$('fa-counts').textContent+=` · ${mb(job.bytes_done||0)} / ${mb(job.bytes_total)} MB · ${mb(job.rate_bps||0)} MB/s`;}
    if(job?.error)fail(new Error(job.error));
    const readingSources=['scanning_sources','source_index','sources'].includes(job?.stage);
    const metrics=readingSources?[["来源图片",job.total||job.discovered||job.done||0],["已读取",job.stage==='sources'?job.done||0:0],["二星标签",job.label_file_count??job.label_count??0],["后期删除",job.deleted_count||0]]:finished?[['已验证',job.done],['XMP',job.xmp],['二星标签',job.label_file_count??job.label_count],['后期删除',job.deleted_count]]:[['归档图片',job?.expected??'—'],['已匹配',job?.matched??'—'],['二星标签',job?.label_file_count??job?.label_count??'—'],['后期删除',job?.deleted_count??'—']];
    $('fa-metrics').innerHTML=metrics.map(([name,n])=>`<div><dd>${esc(n??0)}</dd><dt>${name}</dt></div>`).join('');
    for(const tab of dialog.querySelectorAll('[data-fa-step]')){const n=Number(tab.dataset.faStep);tab.disabled=working||!job&&n>0||n>step;tab.setAttribute('aria-current',n===step?'step':'false');}
    $('fa-items').innerHTML=(job?.items||[]).map(r=>`<tr><td><strong>${esc(r.parent_sku)}</strong><br>${esc(r.sku)}</td><td>${esc(r.original_source||r.source)}</td><td>${esc(job?.input?.preprocess?(r.source===r.original_source?sourcePreview(r):r.source):r.source)}</td><td>${esc(r.destination)}${r.xmp?'<small>'+esc(r.xmp)+'</small>':''}</td><td>${r.photo_role==='label'?'二星标签图':r.raw?'RAW + XMP':'JPG 内嵌'}<small>${r.status==='verified'?'验证通过':r.status==='identical_verified_skip'?'已存在，验证后跳过':'元数据匹配确定'}</small></td></tr>`).join('');
    $('fa-plan-copy').textContent=step===2?(job?.input?.preprocess?'仅整理本次匹配的大小档并同步路径；已有母子 SKU 层级直接复用，再复制到以上目标。':'大小档保持原目录，仅复制到以上母子SKU目标。'):finished?'本次最终大档内的全部选用照片（含二星标签图）均已核验。':'按快门元数据唯一身份匹配，二星标签图包含在归档中。';
    $('fa-plan-copy').textContent+=job?.excluded_count?` 已排除 ${job.excluded_count} 个未入库文件，原件保持。`:'';
    $('fa-plan-copy').textContent+=job?.unassigned_reference_count?` 另有 ${job.unassigned_reference_count} 张未确认 SKU 的小档与本次最终大档无关，保持原样。`:'';
    $('fa-excluded').hidden=!job?.excluded_count;$('fa-excluded').querySelector('summary').textContent=`未入库排除记录 · ${job?.excluded_count||0} 个`;$('fa-excluded').querySelector('div').innerHTML=(job?.excluded_records||[]).map(r=>`<p>${esc(r.original_source||r.source)} · ${esc(r.reason)}</p>`).join('');
    $('fa-prev').disabled=offset===0;$('fa-next').disabled=!job?.has_more;$('fa-page').textContent=`${offset+1}–${offset+(job?.items?.length||0)} / ${job?.expected||0}`;
    $('fa-deleted').hidden=!job?.deleted?.length;$('fa-deleted').querySelector('div').innerHTML=(job?.deleted||[]).map(r=>`<p>${esc(r.original_reference||r.reference)} · 最终大档未选用，已跳过</p>`).join('');
    $('fa-back').hidden=!ready||step!==2;$('fa-new').hidden=!job||working||connectionPending;$('fa-stop').hidden=!job||finished||status==='cancelled';$('fa-export').hidden=!finished;
    const primary=$('fa-primary');primary.hidden=finished||status==='cancelled';primary.disabled=busy||connectionPending||(!job||step===0)&&!scope?.root_id||!status&&!!job||status==='preparing'||status==='queued';
    primary.textContent=status==='running'?'暂停归档':status==='paused'?'继续归档':['failed','interrupted'].includes(status)?'重试 / 继续':step===0?'匹配并预览':step===1?'查看归档预览':'确认归档';
    $('fa-footer-note').textContent=connectionPending?'正在恢复进度连接。':finished?'校验通过后可导出带缩略图的仓管Excel':working?'收起弹窗后任务仍在后台继续':'确认归档后开始执行目录整理与文件复制';
  }
  function sourcePreview(row){
    const root=row.origin==='onsite_label'?job.input.source:job.input.final;
    const name=row.source.split(/[\\/]/).at(-1);
    const clean=p=>p.replaceAll('/',String.fromCharCode(92)).replace(/[\\]+$/,'').toUpperCase();
    const relative=clean(row.source).slice(clean(root).length).split(String.fromCharCode(92)).filter(Boolean);
    if(relative.slice(0,-1).includes(row.sku.toUpperCase()))return row.source;
    const leaf=root.split(/[\\/]/).filter(Boolean).at(-1)?.toUpperCase();
    return [root,leaf===row.sku.toUpperCase()||leaf===row.parent_sku.toUpperCase()?'':row.parent_sku,leaf===row.sku.toUpperCase()?'':row.sku,name].filter(Boolean).join(String.fromCharCode(92));
  }
  async function load(){
    if(!job?.id)return;
    const id=job.id,requestOffset=offset,key=id+':'+requestOffset;
    if(stateRequests.has(key))return stateRequests.get(key);
    const request=(async()=>{
      let data;
      try{data=await api('/api/folder-archive/state?'+new URLSearchParams({id,offset:requestOffset}),undefined,15000);}
      catch(error){
        if(job?.id!==id||offset!==requestOffset)return;
        if(transient(error)){disconnected();return;}
        throw error;
      }
      if(job?.id!==id||offset!==requestOffset||job.updated&&data.updated<job.updated)return;
      job=data;connectionPending=false;retryDelay=700;$('fa-error').hidden=!job.error;
      if(['queued','running','paused','completed','interrupted'].includes(job.status))step=3;
      else if(['preparing','ready','failed','cancelled'].includes(job.status)&&(step===0||step===3&&job.status==='ready'))step=1;
      paint();remember();
      if(job.status==='completed'&&completedNotification!==job.id){completedNotification=job.id;document.dispatchEvent(new CustomEvent('framekeep:metadata-imported'));}
    })();
    stateRequests.set(key,request);
    try{return await request;}finally{if(stateRequests.get(key)===request)stateRequests.delete(key);}
  }
  function poll(){clearTimeout(timer);if(dialog.open&&(active()||connectionPending))timer=setTimeout(async()=>{try{await load()}catch(error){fail(error)}poll()},retryDelay);}
  async function action(fn){if(busy)return;busy=true;$('fa-error').hidden=true;paint();try{await fn()}catch(error){fail(error)}finally{busy=false;paint();poll()}}
  async function open(){
    scope=window.FramekeepArchiveSelection?.();
    if(!job){try{const id=localStorage.getItem('framekeep.folder-archive.job');if(id){job={id};step=1;await load()}}catch(error){if(transient(error))disconnected();else{job=null;step=0;}}}
    if(!active()&&!connectionPending){
      try{
        const state=await api('/api/folder-archive/state',undefined,15000);
        const current=(state.jobs||[]).find(item=>['preparing','queued','running','paused'].includes(item.status));
        if(current&&current.id!==job?.id){job={id:current.id};step=1;offset=0;remember();await load();}
      }catch(error){if(job?.id&&transient(error))disconnected();}
    }
    if(!scope?.root_id&&!job?.id){Framekeep.notify('请先在左侧目录中选择实际文件夹');return;}
    $('fa-error').hidden=!job?.error;
    $('fa-scope').textContent=job?.input?.source||scope?.path||'正在连接已保存的归档任务…';settings();dialog.showModal();
    if(!job?.id){
      busy=true;paint();
      try{scope=await api('/api/folder-archive/scope',scope);$('fa-scope').textContent=scope.path;}
      catch(error){scope=null;$('fa-scope').textContent='请重新选择实际文件夹';throw error;}
      finally{busy=false;paint();}
    }
    paint();poll();
  }
  button.onclick=()=>open().catch(fail);
  $('fa-close').onclick=()=>{dialog.close();clearTimeout(timer)};
  dialog.addEventListener('cancel',()=>clearTimeout(timer));
  $('fa-route').onchange=settings;$('fa-preprocess').onchange=settings;
  for(const browse of dialog.querySelectorAll('[data-fa-browse]'))browse.onclick=()=>action(async()=>{
    const values=window.FramekeepDesktop?.chooseFolders?await FramekeepDesktop.chooseFolders():[(await api('/api/choose-folder',{})).folder];
    const path=Array.isArray(values)?values[0]:values;if(path)$(browse.dataset.faBrowse).value=path;
  });
  $('fa-primary').onclick=()=>action(async()=>{
    if(!job||step===0){
      if(!$('fa-target').value.trim())throw new Error('请选择归档根目录');
      await FramekeepIO.flush?.();offset=0;job=await api('/api/folder-archive/prepare',{...scope,route:$('fa-route').value,final_path:$('fa-final').value.trim(),target_path:$('fa-target').value.trim(),preprocess:$('fa-preprocess').checked,fast_archive:$('fa-fast').checked,exclude_unregistered:$('fa-exclude-unregistered').checked,parent_length:Number($('fa-parent-length').value)});
      step=1;remember();return;
    }
    if(job.status==='running'){job=await api('/api/folder-archive/pause',{id:job.id});await load();return;}
    if(['paused','failed','interrupted'].includes(job.status)){job=await api('/api/folder-archive/resume',{id:job.id});await load();return;}
    if(step===1){step=2;return;}
    job=await api('/api/folder-archive/start',{id:job.id});step=3;await load();
  });
  $('fa-back').onclick=()=>{step=1;paint()};
  $('fa-new').onclick=()=>action(async()=>{job=null;step=0;offset=0;scope=null;connectionPending=false;retryDelay=700;remember();$('fa-scope').textContent='正在确认所选目录…';scope=await api('/api/folder-archive/scope',window.FramekeepArchiveSelection?.()||{});$('fa-scope').textContent=scope.path;});
  $('fa-stop').onclick=()=>action(async()=>{job=await api('/api/folder-archive/cancel',{id:job.id});await load()});
  $('fa-prev').onclick=()=>action(async()=>{offset=Math.max(0,offset-100);await load()});$('fa-next').onclick=()=>action(async()=>{offset+=100;await load()});
  for(const tab of dialog.querySelectorAll('[data-fa-step]'))tab.onclick=()=>{step=Number(tab.dataset.faStep);paint()};
  $('fa-export').onclick=()=>action(async()=>{
    if(window.FramekeepDesktop){const result=await api('/api/folder-archive/export',{id:job.id},120000);$('fa-footer-note').textContent='报表已保存：'+result.path;Framekeep.notify('报表已保存：'+result.path);}
    else{const a=document.createElement('a');a.href='/api/folder-archive/report?'+new URLSearchParams({id:job.id});document.body.append(a);a.click();a.remove();}
  });
})();
