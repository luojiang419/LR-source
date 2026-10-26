(() => {
  'use strict';
  if(!window.FramekeepIO)return;
  const $=id=>document.getElementById(id),api=FramekeepIO.api,esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const tools=document.createElement('div');tools.className='model-local-tools';tools.innerHTML='<button id="model-lm-preset" type="button" disabled title="保留后续开发，当前不启用或下载">内置多模态 · 预留</button><button id="model-ocr-preset" type="button">添加内置 GLM-OCR</button><label>资产视觉解析模型<select id="analysis-provider"></select></label><div class="asset-actions"><button id="analysis-bind-provider" type="button">选用为资产解析模型</button><button id="analysis-test-provider" type="button">测试模型列表与图片请求</button></div><p id="analysis-provider-message" role="status"></p>';$('model-catalog').prepend(tools);
  const advanced=document.createElement('details');advanced.innerHTML='<summary>视觉请求参数</summary><div class="model-fields"><label>超时（秒）<input id="model-timeout" type="number" min="10" max="300" value="90"></label><label>输入长边<input id="model-image-size" type="number" min="480" max="2400" value="1600"></label><label>最大输出 token<input id="model-max-tokens" type="number" min="128" max="8192" value="2000"></label><label class="watch-option"><input id="model-json-mode" type="checkbox" checked>服务支持 JSON 模式</label></div>';$('model-editor').querySelector('.dialog-actions').before(advanced);
  function localPreset(ocr){$('model-add').click();$('model-name').value=ocr?'内置 GLM-OCR':'内置 Qwen2.5-VL 7B Q4';$('model-category').value=ocr?'ocr':'multimodal';$('model-protocol').value='local';$('model-protocol').dispatchEvent(new Event('change'));$('model-id').value=ocr?'glm-ocr-local':'qwen2.5-vl-7b-q4';$('model-timeout').value=300;$('model-json-mode').checked=!ocr;$('model-id').focus()}
  $('model-lm-preset').onclick=()=>localPreset(false);$('model-ocr-preset').onclick=()=>localPreset(true);
  window.FramekeepModelAdvanced={values:()=>({timeout:Number($('model-timeout').value),image_size:Number($('model-image-size').value),max_tokens:Number($('model-max-tokens').value),json_mode:$('model-json-mode').checked}),load:p=>{$('model-timeout').value=p?.timeout||90;$('model-image-size').value=p?.image_size||1600;$('model-max-tokens').value=p?.max_tokens||2000;$('model-json-mode').checked=p?.json_mode!==false}};
  async function providers(){const data=await api('/api/providers'),select=$('analysis-provider');select.innerHTML='<option value="">请选择已配置的多模态模型</option>'+data.providers.filter(p=>p.category==='multimodal'&&!p.reserved).map(p=>`<option value="${p.id}">${esc(p.name)} · ${esc(p.model)}</option>`).join('');select.value=data.bindings?.asset_analysis||'';for(const id of ['model-lm-preset','model-ocr-preset','analysis-provider','analysis-bind-provider','analysis-test-provider'])$(id).disabled=data.builtin_enabled===true||id==='model-lm-preset'}
  $('assistant-settings').addEventListener('click',()=>providers().catch(e=>$('analysis-provider-message').textContent=e.message));
  const observer=new MutationObserver(()=>providers().catch(()=>{}));observer.observe($('model-cards'),{childList:true});
  $('analysis-bind-provider').onclick=async()=>{try{await api('/api/providers/bind',{purpose:'asset_analysis',id:$('analysis-provider').value});$('analysis-provider-message').textContent='资产解析选用已保存，SKU 模型选用保持独立'}catch(e){$('analysis-provider-message').textContent=e.message}};
  $('analysis-test-provider').onclick=async()=>{const b=$('analysis-test-provider');b.disabled=true;try{$('analysis-provider-message').textContent='正在验证模型列表和视觉请求…';const r=await api('/api/providers/test',{id:$('analysis-provider').value});$('analysis-provider-message').textContent=r.message}catch(e){$('analysis-provider-message').textContent=e.message}finally{b.disabled=false}};
  const tab=document.createElement('button');tab.id='asset-analysis-open';tab.textContent='解析任务';tab.setAttribute('role','tab');tab.setAttribute('aria-selected','false');tab.setAttribute('aria-controls','analysis-dialog');tab.tabIndex=-1;document.querySelector('.workspace-tabs').append(tab);
  const dialog=document.createElement('section');dialog.id='analysis-dialog';dialog.className='asset-workspace hidden';dialog.setAttribute('role','tabpanel');dialog.setAttribute('aria-labelledby',tab.id);
  dialog.innerHTML=`<div class="asset-workspace-heading analysis-heading"><div class="analysis-heading-title"><h2>解析任务</h2><p>查看元数据写入、OCR 与多模态解析进度。</p></div><div class="analysis-heading-tools"><div class="analysis-heading-metrics" aria-label="解析任务统计"><div><span>等待解析</span><strong id="analysis-queued">0</strong></div><div><span>解析中</span><strong id="analysis-running">0</strong></div><div><span>已完成</span><strong id="analysis-completed">0</strong></div><div><span>需处理</span><strong id="analysis-failed">0</strong></div></div><div class="analysis-heading-selection"><span>本次选片</span><strong id="analysis-selection"></strong><button id="analysis-close" class="bordered">返回选片</button></div><button id="analysis-settings" class="bordered">配置解析模型</button></div></div>
  <div class="asset-workspace-grid"><main class="asset-workspace-main">
    <section id="metadata-tasks-card" class="workspace-card workspace-result-card"><header><h3 id="task-list-title">任务列表</h3><select id="metadata-batch" aria-label="写入目录" hidden></select><button id="metadata-review" hidden>处理异常</button><button id="parsing-clear-list" class="bordered" type="button" title="清空当前类型已结束的任务，保留等待及进行中的任务和解析结果" disabled>清空列表</button></header><div id="metadata-verification" class="parsing-list-note" role="status" hidden></div><div id="metadata-files" class="sku-table-wrap" hidden></div><div id="analysis-jobs" class="sku-table-wrap"></div></section>
    <section id="task-progress-panel" class="workspace-card"><div class="task-progress-controls"><h3>任务进度</h3><div class="parsing-filters"><label class="workspace-inline">类型<select id="parsing-kind"><option value="auto">自动切换</option><option value="metadata">元数据写入</option><option value="all">OCR + 多模态</option><option value="ocr">OCR</option><option value="multimodal">多模态</option></select></label><label id="analysis-filter-control" class="workspace-inline">状态<select id="analysis-filter"><option value="all">全部任务</option><option value="active">等待与进行中</option><option value="completed">已完成</option><option value="attention">需处理</option></select></label></div><div class="parsing-progress"><div id="metadata-progress" class="metadata-progress-track" role="progressbar" aria-label="任务进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><span id="metadata-progress-fill"></span></div><strong id="metadata-percent">0%</strong><span id="metadata-progress-text" role="status"></span><span id="parsing-connection" role="status"></span></div></div></section>
  </main><aside class="asset-workspace-sidebar" aria-label="解析任务控制">
    <section class="workspace-card"><header><h3>OCR 解析</h3><span id="parsing-ocr-state" class="workspace-badge"></span></header><div class="workspace-fields"><label>解析模式<select id="parsing-ocr-mode"><option value="auto">自动 · 二星标签触发</option><option value="manual">手动 · 点击时解析</option></select></label><p id="parsing-ocr-model" class="workspace-caption"></p><button id="parsing-ocr-run" class="primary">OCR 解析所选照片</button><div class="asset-actions"><button id="parsing-ocr-pause">暂停 OCR 队列</button><button id="parsing-ocr-resume">恢复 OCR 队列</button></div><p class="dialog-copy">手动 OCR 只读取文字，不要求二星或工作表；二星自动标签仍沿用拍摄对账规则。</p></div></section>
    <section class="workspace-card"><header><h3>解析方案</h3><button id="analysis-new-schema">新建方案</button></header><div class="workspace-fields"><label>当前方案<select id="analysis-schema"><option value="">先创建解析方案</option></select></label><details id="analysis-schema-editor"><summary>编辑维度与提示词</summary><label>方案名称<input id="analysis-schema-name" maxlength="100" placeholder="例如：服装材质与颜色"></label><label>解析指令<textarea id="analysis-prompt" rows="4" placeholder="填写需要提取的维度与判断要求"></textarea></label><label>JSON Schema<textarea id="analysis-json-schema" rows="8">{"type":"object","properties":{},"additionalProperties":true}</textarea></label><button id="analysis-save-schema">保存方案版本</button></details></div><footer class="workspace-submit"><button id="analysis-run" class="primary">解析所选照片</button><p id="analysis-message" role="status"></p></footer></section>
    <section class="workspace-card"><header><h3>队列控制</h3><span id="analysis-queue-state" class="workspace-badge">运行中</span></header><div class="workspace-fields"><label>本机模型并发<select id="analysis-concurrency"><option value="2">2 张</option><option value="1">1 张</option></select></label><div class="asset-actions"><button id="analysis-pause">暂停普通解析</button><button id="analysis-resume">恢复普通解析</button></div><p id="analysis-stats" class="workspace-caption" role="status"></p><p class="dialog-copy">API 并发在模型卡片中设置；本机模型按此处限制。款号标签优先处理。</p></div></section>
  </aside></div>`;document.querySelector('.workbench > .statusbar').before(dialog);
  $('analysis-schema').parentElement.before(Object.assign(document.createElement('label'),{innerHTML:'多模态解析模式<select id="parsing-multimodal-mode"><option value="manual">手动 · 点击时解析</option><option value="auto">自动 · 当前目录新照片</option></select>'}));
  $('analysis-schema').parentElement.after(Object.assign(document.createElement('p'),{id:'parsing-auto-scope',className:'workspace-caption'}));
  $('parsing-auto-scope').after(Object.assign(document.createElement('button'),{id:'parsing-apply-auto',textContent:'自动解析使用当前目录与方案'}));
  $('analysis-run').textContent='多模态解析所选照片';
  $('analysis-pause').textContent='暂停多模态队列';$('analysis-resume').textContent='恢复多模态队列';
  $('analysis-settings').onclick=()=>$('assistant-settings').click();
  const names={queued:'等待解析',retry:'等待重试',waiting_model:'等待模型',running:'解析中',completed:'已完成',matched:'已对账',confirmed:'已确认',review:'待复核',unmatched:'未匹配',ignored:'已忽略',failed:'失败',stale:'源图已变化',cancelled:'已取消'};
  const waiting=['queued','retry','waiting_model'],done=['completed','matched','confirmed'],attention=['failed','stale','review','unmatched'];
  const clearable=[...done,...attention,'ignored','cancelled'];
  function selectionSummary(){
    const ids=window.FramekeepAssets?.selection()||[];
    $('analysis-selection').textContent=ids.length?`${ids.length} 张`:'未选择';
    $('analysis-selection').title=ids.length?'沿用选片页的 Shift / Ctrl 多选范围':'返回选片页选择照片，再提交解析';
    $('analysis-run').disabled=!ids.length;
    $('parsing-ocr-run').disabled=!ids.length;
  }
  document.addEventListener('framekeep:selection',()=>{if(FramekeepWorkspace.current()==='analysis')selectionSummary()});
  let state={schemas:[],jobs:[]},ocr={mode:'auto',paused:false,jobs:[],counts:{}},metadata={batches:[]},metadataId='',selectedKey='',busy=false,polling=false,generation=0;
  let autoKind='',observedTasks={},preferredTask='';
  const metadataActive=['indexing','matching','writing','verifying'];
  $('task-progress-panel').querySelector('.task-progress-controls').insertAdjacentHTML('beforeend','<button id="parsing-clear-completed" class="bordered" type="button" title="清理所有类型的已完成任务，保留识别结果与拍摄记录">清理已完成</button>');
  function preferTask(kind){preferredTask=kind;$('parsing-kind').value='auto'}
  function taskKind(){
    const sources={metadata:{stamp:Math.max(0,...(metadata.batches||[]).map(b=>b.updated||0)),active:(metadata.batches||[]).some(b=>metadataActive.includes(b.status))}};
    for(const [kind,data] of [['ocr',ocr],['multimodal',state]])sources[kind]={stamp:Math.max(0,...data.jobs.map(j=>j.created||0)),active:[...waiting,'running'].some(s=>(data.counts?.[s]||0)>0)};
    const newest=keys=>keys.sort((a,b)=>sources[b].stamp-sources[a].stamp)[0];
    if(preferredTask){autoKind=preferredTask;preferredTask=''}
    else if(!autoKind||!sources[autoKind]?.active){
      const active=Object.keys(sources).filter(k=>sources[k].active),changed=Object.keys(sources).filter(k=>sources[k].stamp>(observedTasks[k]||0));
      autoKind=newest(active)||newest(changed)||autoKind||'all';
    }
    observedTasks=Object.fromEntries(Object.entries(sources).map(([k,v])=>[k,v.stamp]));
    return $('parsing-kind').value==='auto'?autoKind:$('parsing-kind').value;
  }
  function taskProgress(percent,text,active,label){
    const progress=$('metadata-progress');progress.setAttribute('aria-label',label);progress.setAttribute('aria-valuenow',String(percent));progress.dataset.active=String(active);
    $('metadata-progress-fill').style.width=percent+'%';$('metadata-percent').textContent=percent+'%';$('metadata-progress-text').textContent=text;$('metadata-progress-text').title=text;
    $('task-progress-panel').dataset.active=String(active);
  }
  const metadataNames={indexing:'扫描中',matching:'匹配中',writing:'写入中',verifying:'校验中',queued:'等待写入',running:'正在写入',applied:'已写入',completed:'完成',attention:'部分需处理',failed:'失败',review:'待确认',unmatched:'未匹配',cancelled:'已取消'};
  function metadataPhotoRows(files){
    const grouped=new Map(),priority={running:0,failed:1,review:2,unmatched:2,cancelled:2,queued:3,applied:4};
    for(const file of files){
      const stem=file.path.replace(/\.[^/.\\]+$/,''),key=(file.source_path+'|'+stem).replaceAll('\\','/').toLowerCase();
      if(!grouped.has(key))grouped.set(key,{...file,components:[]});grouped.get(key).components.push(file);
    }
    return [...grouped.values()].map(group=>{
      group.components.sort((a,b)=>Number(a.kind==='raw')-Number(b.kind==='raw'));
      const formats=[...new Set(group.components.map(f=>f.format||(f.kind==='raw'?'RAW':'JPG')))];group.format=formats.join('/');
      group.status=group.components.map(f=>f.status).sort((a,b)=>(priority[a]??2)-(priority[b]??2))[0];
      group.label=group.status==='failed'&&group.components.some(f=>f.status==='applied')?'部分失败':metadataNames[group.status]||group.status;
      if(formats.length>1)group.name=group.name.replace(/\.[^/.\\]+$/,'');
      group.path=group.components.map(f=>f.path).join('\n');
      const ratings=[...new Set(group.components.map(f=>f.rating).filter(r=>r!==null&&r!==undefined))],skus=[...new Set(group.components.map(f=>f.sku).filter(Boolean))];
      group.rating=ratings.length===1?ratings[0]:null;group.ratingMixed=ratings.length>1;group.sku=skus.length>1?'SKU 不一致':skus[0]||'';
      group.detail=new Set(group.components.map(f=>f.status)).size>1?group.components.map(f=>`${f.format||(f.kind==='raw'?'RAW':'JPG')} ${metadataNames[f.status]||f.status}`).join(' · '):'';
      group.reason=[...new Set(group.components.map(f=>f.reason).filter(Boolean))].join('；');return group;
    }).sort((a,b)=>(priority[a.status]??2)-(priority[b.status]??2)||a.name.localeCompare(b.name));
  }
  function renderMetadata(){
    const batches=metadata.batches||[];
    if(!batches.length){$('metadata-batch').hidden=true;$('metadata-files').dataset.content='';$('metadata-files').innerHTML='';taskProgress(0,'',false,'元数据写入进度');return}
    const activeBatch=batches.find(b=>['indexing','matching','writing','verifying'].includes(b.status));if(activeBatch)metadataId=activeBatch.id;else if(!batches.some(b=>b.id===metadataId))metadataId=batches[0].id;
    const options=batches.map(b=>`<option value="${esc(b.id)}">${esc(b.folder)}</option>`).join('');if($('metadata-batch').innerHTML!==options)$('metadata-batch').innerHTML=options;$('metadata-batch').value=metadataId;
    const batch=batches.find(b=>b.id===metadataId),counts=batch.counts||{},attention=['failed','review','unmatched','cancelled'].reduce((n,k)=>n+(counts[k]||0),0);
    const verification=batch.verification||{},active=['indexing','matching','writing','verifying'].includes(batch.status);
    $('metadata-review').hidden=!attention&&verification.status!=='attention';
    $('metadata-batch').title=batch.folder;$('metadata-tasks-card').dataset.active=String(active);
    const checked=Math.min(batch.total,verification.checked||0),percent=batch.total?Math.floor((batch.finished+checked)/(batch.total*2)*100):0;
    taskProgress(percent,(metadataNames[batch.status]||batch.status)+(batch.status==='indexing'?(batch.scan?.total?` ${batch.scan.done||0} / ${batch.scan.total}`:''):` · 写入 ${counts.applied||0} / ${batch.total} · 校验 ${checked} / ${batch.total}`)+(attention?` · 需处理 ${attention}`:'')+(batch.reason?' · '+batch.reason:''),active,'元数据写入进度');
    const issues=[...(verification.errors||[]),...(verification.missing_assets||[]).map(n=>'缺少对应照片：'+n),...(verification.missing_components||[]).map(n=>'缺少组件：'+n)];
    $('metadata-verification').hidden=!['completed','attention'].includes(verification.status);
    $('metadata-verification').textContent=verification.status==='completed'?`校验通过 · ${verification.valid} 个文件 / ${verification.source_assets} 组照片齐全`:issues.slice(0,12).join('；')+(issues.length>12?`；另有 ${issues.length-12} 项`:'');
    const files=batch.files||[],rows=metadataPhotoRows(files),html=rows.length?'<table><colgroup><col class="metadata-source-col"><col class="metadata-arrow-col"><col class="metadata-target-col"><col class="metadata-format-col"><col class="metadata-value-col"><col></colgroup><thead><tr><th>源文件</th><th class="metadata-direction" aria-label="写入方向"></th><th class="metadata-target">目标文件</th><th class="metadata-format">文件格式</th><th>星级 / SKU</th><th>写入情况</th></tr></thead><tbody>'+rows.map(f=>`<tr data-metadata-status="${esc(f.status)}"><td title="${esc(f.source_path||'')}">${esc(f.source_name||'待匹配')}<small class="metadata-file-path">${esc(f.source_path||'')}</small></td><td class="metadata-direction" aria-label="源文件写入目标文件"><span class="metadata-flow-arrow" aria-hidden="true"><svg viewBox="0 0 40 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 8H37M31 2L37 8L31 14"/></svg></span></td><td class="metadata-target" title="${esc(f.path)}">${esc(f.name)}<small class="metadata-file-path">${esc(f.path)}</small></td><td class="metadata-format"><span class="metadata-format-tag">${esc(f.format||(f.kind==='raw'?'RAW':'JPG'))}</span></td><td>${f.ratingMixed?'星级不一致':f.rating===null?'—':esc(f.rating)+' 星'}<br><small>${esc(f.sku||'—')}</small></td><td><span class="workspace-badge" data-status="${esc(f.status)}">${esc(f.label)}</span>${f.detail?`<small class="metadata-components">${esc(f.detail)}</small>`:''}${f.status==='running'?'<progress class="parsing-running" aria-label="正在写入元数据"></progress>':''}${f.reason?`<p class="workspace-caption">${esc(f.reason)}</p>`:''}</td></tr>`).join('')+'</tbody></table>'+(batch.total>files.length?`<p class="parsing-list-note">显示 ${rows.length} 组照片（${files.length} / ${batch.total} 个文件），进度包含全部文件。</p>`:''):'<p class="parsing-list-note">'+esc(batch.reason||'正在扫描最终目录…')+'</p>';
    if($('metadata-files').dataset.content!==html){$('metadata-files').dataset.content=html;$('metadata-files').innerHTML=html}
  }
  $('metadata-batch').onchange=e=>{metadataId=e.target.value;render()};
  $('metadata-review').onclick=()=>window.FramekeepRawSync?.review(metadataId);
  window.FramekeepMetadataTasks={submitted(id){metadataId=id;preferTask('metadata');FramekeepWorkspace.show('analysis');dialog.querySelector('.asset-workspace-main').scrollTop=0}};
  function updateClearListButton(kind=$('metadata-tasks-card').dataset.kind){
    const sources=kind==='ocr'?[ocr]:[];
    $('parsing-clear-list').hidden=!sources.length;
    $('parsing-clear-list').disabled=busy||!sources.some(data=>clearable.some(status=>(data.counts?.[status]||0)>0));
  }
  async function action(fn){if(busy)return;busy=true;generation++;updateClearListButton();try{await fn()}catch(e){$('analysis-message').textContent=e.message}finally{busy=false;updateClearListButton()}}
  function render(){
    const kind=taskKind(),isMetadata=kind==='metadata';
    $('metadata-tasks-card').dataset.kind=kind;$('task-progress-panel').dataset.kind=kind;
    $('task-list-title').textContent=isMetadata?'元数据写入':kind==='ocr'?'OCR 解析':kind==='multimodal'?'多模态解析':'解析任务';
    updateClearListButton(kind);
    $('metadata-batch').hidden=!isMetadata;$('metadata-review').hidden=true;$('metadata-verification').hidden=true;
    $('metadata-files').hidden=!isMetadata;$('analysis-jobs').hidden=isMetadata;$('analysis-filter-control').hidden=isMetadata;
    if(isMetadata)renderMetadata();
    const counts={...state.counts};for(const [key,n] of Object.entries(ocr.counts||{}))counts[key]=(counts[key]||0)+n;
    const sum=keys=>keys.reduce((n,k)=>n+(counts[k]||0),0);
    $('parsing-clear-completed').disabled=busy||!(sum(done)||(metadata.batches||[]).some(batch=>batch.status==='completed'&&batch.verification?.status==='completed'));
    if(!isMetadata){
      const visibleCounts=kind==='ocr'?ocr.counts||{}:kind==='multimodal'?state.counts||{}:counts;
      const total=Object.values(visibleCounts).reduce((n,x)=>n+x,0),pending=[...waiting,'running'].reduce((n,k)=>n+(visibleCounts[k]||0),0),finished=total-pending;
      taskProgress(total?Math.floor(finished/total*100):0,total?`已结束 ${finished} / ${total} · 解析中 ${visibleCounts.running||0}`:'',pending>0,'解析任务完成进度');
    }
    $('parsing-ocr-mode').value=ocr.mode;
    $('parsing-multimodal-mode').value=state.automatic?.enabled?'auto':'manual';
    $('parsing-ocr-state').textContent=ocr.paused?'已暂停':'运行中';$('parsing-ocr-pause').disabled=!!ocr.paused;$('parsing-ocr-resume').disabled=!ocr.paused;
    $('parsing-ocr-model').textContent='当前模型：'+(ocr.model||'未配置');
    $('parsing-auto-scope').textContent=state.automatic?.enabled?`自动目录：${state.automatic.folder} · 方案 v${state.automatic.version}`:'启用自动时绑定当前目录与所选方案，只解析之后新入库的照片。';
    $('parsing-apply-auto').hidden=!state.automatic?.enabled;
    const options='<option value="">选择解析方案</option>'+state.schemas.map(s=>`<option value="${s.id}:${s.version}">${esc(s.name)} · v${s.version}</option>`).join('');if($('analysis-schema').innerHTML!==options)$('analysis-schema').innerHTML=options;$('analysis-schema').value=selectedKey;
    $('analysis-stats').textContent=`运行请求 ${state.scheduler?.active||0} · 本机并发上限 ${state.scheduler?.limit||2} · `+Object.entries(state.counts||{}).map(([k,n])=>`${names[k]||k} ${n}`).join(' / ');
    if(document.activeElement!==$('analysis-concurrency'))$('analysis-concurrency').value=state.scheduler?.limit||2;
    for(const [id,count] of [['queued',sum(waiting)],['running',counts.running||0],['completed',sum(done)],['failed',sum(attention)]])$('analysis-'+id).textContent=count;
    $('analysis-queue-state').textContent=state.paused?'已暂停':'运行中';$('analysis-pause').disabled=!!state.paused;$('analysis-resume').disabled=!state.paused;
    const filter=$('analysis-filter').value;
    const jobs=[...state.jobs.map(j=>({...j,channel:'multimodal'})),...ocr.jobs.map(j=>({...j,channel:'ocr'}))].sort((a,b)=>b.created-a.created).filter(j=>(kind==='all'||j.channel===kind)&&(filter==='all'||filter==='active'&&[...waiting,'running'].includes(j.status)||filter==='completed'&&done.includes(j.status)||filter==='attention'&&attention.includes(j.status)));
    const html=jobs.length?'<table><thead><tr><th>照片</th><th>类型 / 来源</th><th>解析进度 / 结果</th><th>操作</th></tr></thead><tbody>'+jobs.map(j=>{
      const result=typeof j.result==='string'?j.result:Object.keys(j.result||{}).length?JSON.stringify(j.result,null,2):'';
      const held=waiting.includes(j.status)&&(j.channel==='ocr'?ocr.paused||ocr.mode==='manual'&&j.trigger==='auto':state.paused||!state.automatic?.enabled&&j.trigger==='auto');
      return `<tr data-parsing-channel="${j.channel}"><td><div class="analysis-asset">${j.preview?`<img class="archive-thumb" src="${esc(j.preview)}" loading="lazy" alt="解析照片">`:''}<div>${esc(j.name||j.asset||'—')}<br><small>${esc(new Date(j.created*1000).toLocaleString())}</small></div></div></td><td>${j.channel==='ocr'?(j.kind==='label'?'OCR 标签对账':'OCR 文字识别'):'多模态'} · ${j.trigger==='auto'?'自动':'手动'}<br><small>${j.channel==='multimodal'?esc(state.schemas.find(s=>s.id===j.schema_id&&s.version===j.schema_version)?.name||'解析方案')+' · v'+esc(j.schema_version||''):''}</small></td><td><span class="workspace-badge" data-status="${esc(j.status)}">${esc(held?'已暂停等待':names[j.status]||j.status)}</span>${j.status==='running'?'<progress class="parsing-running" aria-label="正在解析"></progress>':''}<p class="workspace-caption">${esc(held?'恢复队列或开启自动模式后继续':j.reason||'')}</p>${result?`<details><summary>查看解析结果</summary><pre>${esc(result)}</pre></details>`:''}${j.status==='review'?'<small>请在拍摄进度页复核款号。</small>':''}</td><td>${['failed','cancelled','stale'].includes(j.status)?`<button data-analysis-action="retry" data-job-id="${esc(j.id)}" data-channel="${j.channel}">重试</button>`:[...waiting,'running'].includes(j.status)?`<button data-analysis-action="cancel" data-job-id="${esc(j.id)}" data-channel="${j.channel}">取消</button>`:'—'}</td></tr>`;
    }).join('')+'</tbody></table>':'';
    if($('analysis-jobs').dataset.content!==html){$('analysis-jobs').dataset.content=html;$('analysis-jobs').innerHTML=html}
  }
  function receive(data){state=data.analysis;ocr=data.ocr;metadata=data.metadata||{batches:[]};if(!selectedKey&&state.automatic?.schema_id)selectedKey=`${state.automatic.schema_id}:${state.automatic.version}`;render()}
  $('parsing-clear-list').onclick=()=>{
    const channel=$('metadata-tasks-card').dataset.kind;
    action(async()=>{receive(await api('/api/parsing/clear-list',{channel}));$('analysis-message').textContent='已清空当前类型的已结束任务，等待及进行中的任务、解析结果与拍摄记录已保留';});
  };
  $('parsing-clear-completed').onclick=()=>action(async()=>{
    const button=$('parsing-clear-completed');button.disabled=true;
    try{receive(await api('/api/parsing/clear-completed',{}));$('analysis-message').textContent='已清理完成的任务，识别结果与拍摄记录已保留';}
    finally{button.disabled=!(done.some(status=>(ocr.counts?.[status]||0)+(state.counts?.[status]||0)>0)||(metadata.batches||[]).some(batch=>batch.status==='completed'&&batch.verification?.status==='completed'));}
  });
  async function load(){const request=generation,data=await api('/api/parsing');if(request!==generation)return;receive(data);$('parsing-connection').textContent=''}
  FramekeepWorkspace.register('analysis',tab,dialog,()=>{selectionSummary();action(load)});$('analysis-close').onclick=()=>FramekeepWorkspace.show('selection');
  $('analysis-filter').onchange=render;
  $('parsing-kind').onchange=render;
  for(const channel of ['ocr','multimodal'])$('parsing-'+channel+'-mode').onchange=e=>action(async()=>{
    const body={channel,mode:e.target.value};
    if(channel==='multimodal'&&body.mode==='auto'){
      if(!selectedKey){render();throw new Error('请先创建并选择多模态解析方案')}
      const [schema_id,version]=selectedKey.split(':');Object.assign(body,{session_id:FRAMEKEEP_SESSION.id,schema_id,version:Number(version)});
    }
    try{receive(await api('/api/parsing/configure',body));$('analysis-message').textContent='解析模式已保存，正在执行的请求允许完成'}catch(error){render();throw error}
  });
  $('parsing-ocr-run').onclick=()=>action(async()=>{await FramekeepIO.flush();const data=await api('/api/parsing/ocr',{session_id:FRAMEKEEP_SESSION.id,capture_ids:FramekeepAssets.selection()});preferTask('ocr');receive(data);$('analysis-message').textContent='OCR 任务已提交，可在左侧进度列表查看'});
  $('parsing-apply-auto').onclick=()=>action(async()=>{if(!selectedKey)throw new Error('请先选择多模态解析方案');const [schema_id,version]=selectedKey.split(':');receive(await api('/api/parsing/configure',{channel:'multimodal',mode:'auto',session_id:FRAMEKEEP_SESSION.id,schema_id,version:Number(version)}));$('analysis-message').textContent='自动解析目录与方案已更新'});
  for(const [id,paused] of [['parsing-ocr-pause',true],['parsing-ocr-resume',false]])$(id).onclick=()=>action(async()=>receive(await api('/api/parsing/configure',{channel:'ocr',paused})));
  $('analysis-new-schema').onclick=()=>{selectedKey='';$('analysis-schema').value='';$('analysis-schema-name').value='';$('analysis-prompt').value='';$('analysis-json-schema').value='{"type":"object","properties":{},"additionalProperties":true}';$('analysis-schema-editor').open=true};
  $('analysis-schema').onchange=e=>{selectedKey=e.target.value;const s=state.schemas.find(s=>`${s.id}:${s.version}`===selectedKey);if(s){$('analysis-schema-name').value=s.name;$('analysis-prompt').value=s.prompt;$('analysis-json-schema').value=JSON.stringify(JSON.parse(s.schema_json),null,2)}};
  $('analysis-save-schema').onclick=()=>action(async()=>{state=await api('/api/analysis/schemas',{name:$('analysis-schema-name').value,prompt:$('analysis-prompt').value,schema:JSON.parse($('analysis-json-schema').value),schema_id:selectedKey?selectedKey.split(':')[0]:null});const s=state.schemas[0];selectedKey=`${s.id}:${s.version}`;render();$('analysis-message').textContent='方案版本已保存'});
  $('analysis-run').onclick=()=>action(async()=>{if(!selectedKey)throw new Error('请先创建并选择解析方案');const assets=await FramekeepAssets.resolve(),[schema_id,version]=selectedKey.split(':');state=await api('/api/analysis/batches',{asset_ids:assets.map(a=>a.id),schema_id,version:Number(version)});preferTask('multimodal');render();$('analysis-message').textContent=`已提交 ${assets.length} 张；相同输入/方案/模型的任务自动去重`});
  for(const [id,paused] of [['analysis-pause',true],['analysis-resume',false]])$(id).onclick=()=>action(async()=>{state=await api('/api/analysis/control',{paused});render()});
  $('analysis-concurrency').onchange=e=>action(async()=>{state=await api('/api/analysis/control',{concurrency:Number(e.target.value)});render()});
  $('analysis-jobs').onclick=e=>{const b=e.target.closest('[data-analysis-action]');if(b)action(async()=>receive(await api('/api/parsing/control',{channel:b.dataset.channel,action:b.dataset.analysisAction,job_id:b.dataset.jobId})))};
  async function poll(){if(FramekeepWorkspace.current()==='analysis'&&!busy&&!polling){polling=true;try{await load()}catch{$('parsing-connection').textContent='进度连接中断，正在重试'}finally{polling=false}}setTimeout(poll,1200)}poll();
})();
