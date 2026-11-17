(() => {
  'use strict';
  if(!window.FramekeepIO)return;
  const $=id=>document.getElementById(id),api=FramekeepIO.api,esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function localTime(value){
    const text=String(value??'').trim(),match=text.match(/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$/);
    if(!match)return text;
    const date=new Date(text.replace(' ','T'));if(!Number.isFinite(date.getTime()))return text;
    const pad=n=>String(n).padStart(2,'0');
    return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}${match[1]||''}`;
  }
  function sourceValue(row,column){const value=row.source_values?.[column.letter]||'';return ['记录时间','入库时间','拍摄时间','拍摄完成时间'].includes(column.title.trim())?localTime(value):value}
  const tabs=document.createElement('nav');tabs.className='workspace-tabs';tabs.setAttribute('role','tablist');tabs.setAttribute('aria-label','工作页面');tabs.innerHTML='<button id="selection-tab" role="tab" aria-selected="true" aria-controls="selection-page">选片</button><button id="sku-open" role="tab" aria-selected="false" aria-controls="sku-dialog" tabindex="-1">拍摄进度</button>';document.querySelector('.app-header').after(tabs);
  const button=$('sku-open');
  let instantFeedback=null,feedbackLoading=false,feedbackVersion=0;
  const localLabels=new Map();
  function renderFeedback(){
    const sid=window.FRAMEKEEP_SESSION?.id;
    if(!instantFeedback||instantFeedback.session_id!==sid)return;
    const labels=new Map(instantFeedback.labels.map(r=>[r.capture_id,r.status]));
    for(const [id,value] of localLabels){if(value.session_id!==sid)continue;if(value.rating===2)labels.set(id,labels.get(id)||(value.saved?'queued':'saving'));else labels.delete(id)}
    if(!labels.size){
      const shot=(state.progress||[]).reduce((sum,row)=>sum+row.shot,0);
      (button.querySelector('.workspace-tab-label')||button).textContent=state.project?`拍摄进度 · ${shot} 已拍摄`:'拍摄进度';
      return; // 断连/空目录不把已保存文档的完成数清零。
    }
    const counts={};for(const value of labels.values())counts[value]=(counts[value]||0)+1;
    const pending=(counts.queued||0)+(counts.running||0)+(counts.waiting_model||0),review=(counts.review||0)+(counts.unmatched||0)+(counts.matched||0);
    const parts=[`${labels.size} 已收尾`,`${counts.confirmed||0} 已入库`];
    if(counts.saving)parts.push(`${counts.saving} 待保存`);if(pending)parts.push(`${pending} 待解析`);if(review)parts.push(`${review} 待复核`);if(counts.failed)parts.push(`${counts.failed} 失败`);
    (button.querySelector('.workspace-tab-label')||button).textContent='拍摄进度 · '+parts.join(' · ');
    button.title='已收尾为当前目录二星标签数；已入库为已确认标签数。文件写入进度见拍摄记录。';
  }
  async function loadFeedback(){
    if(feedbackLoading)return;feedbackLoading=true;const version=feedbackVersion,sid=window.FRAMEKEEP_SESSION?.id;
    try{const next=await api('/api/sku/feedback',undefined,5000);if(next.session_id===sid&&version===feedbackVersion){instantFeedback=next;const ids=new Set(next.labels.map(r=>r.capture_id));for(const [id,value] of localLabels)if(value.saved&&value.session_id===sid&&(value.rating===2?ids.has(id):!ids.has(id)))localLabels.delete(id);renderFeedback()}}catch{}finally{feedbackLoading=false}
  }
  document.addEventListener('framekeep:rating-changed',event=>{
    const value=event.detail;feedbackVersion++;localLabels.set(value.capture_id,value);
    if(!instantFeedback||instantFeedback.session_id!==value.session_id)instantFeedback={session_id:value.session_id,labels:[]};
    renderFeedback();
  });
  document.addEventListener('framekeep:ratings-saved',event=>{for(const [id,rating] of Object.entries(event.detail.overrides||{})){const value=localLabels.get(id);if(value&&value.session_id===event.detail.session_id&&value.rating===rating)value.saved=true}feedbackVersion++;renderFeedback();loadFeedback()});
  document.addEventListener('framekeep:selection',renderFeedback);
  let feedbackAt=0;setInterval(()=>{const now=Date.now(),visible=['selection','progress'].includes(window.FramekeepWorkspace?.current()||'selection');if(!document.hidden&&now-feedbackAt>=(visible?500:5000)){feedbackAt=now;loadFeedback()}},500);loadFeedback();
  const bar=document.createElement('div');bar.id='sku-live-bar';bar.className='sku-live-bar hidden';bar.innerHTML='<strong>当前拍摄</strong><select id="sku-live-scope" aria-label="当前拍摄类型或工作表"></select><button id="sku-bind-current" class="bordered">绑定当前照片目录</button><span id="sku-live-count"></span><button id="sku-review-open">查看识别记录</button>';
  const currentPanel=document.createElement('section');currentPanel.id='current-shooting-panel';currentPanel.className='selection-sidebar-page hidden';
  const currentTable=document.createElement('div');currentTable.id='sku-current-table';currentTable.className='sku-current-table';currentTable.setAttribute('aria-live','polite');
  const currentViews=document.createElement('nav');currentViews.id='shooting-list-views';currentViews.className='shooting-list-views';currentViews.setAttribute('aria-label','拍摄款号分类');currentViews.setAttribute('role','tablist');currentViews.innerHTML='<button id="shooting-scope-view" role="tab" aria-controls="sku-current-table" data-current-view="scope">当前工作表</button><button id="shooting-common-view" role="tab" aria-controls="sku-current-table" data-current-view="common">公共 <span>0</span></button>';
  currentTable.setAttribute('role','tabpanel');
  const manualType=document.createElement('label');manualType.id='shooting-manual-field';manualType.className='shooting-manual-field';manualType.innerHTML='当前拍摄类型<input id="shooting-manual-type" maxlength="100" placeholder="未导入清单时填写类型">';
  const automation=document.createElement('section');automation.id='shooting-automation';automation.className='shooting-automation';automation.setAttribute('aria-label','拍摄文档与自动识别');
  automation.innerHTML='<div class="shooting-import-line"><strong>拍摄文档</strong><button id="shooting-import" class="bordered" type="button">导入表格</button><input id="shooting-plan-file" type="file" accept=".xlsx" hidden></div><p id="shooting-document" class="shooting-document">尚未导入表格文档</p><label class="shooting-auto-line"><span>全自动 OCR</span><input id="shooting-full-auto" class="toggle-switch" type="checkbox" role="switch" aria-label="右栏全自动 OCR" aria-describedby="shooting-auto-status" disabled></label><p id="shooting-auto-status" class="shooting-auto-status" role="status">无需文档，打开照片目录即可开启</p>';
  try{manualType.querySelector('input').value=localStorage.getItem('framekeep-manual-shooting-type-v1')||''}catch{}
  manualType.querySelector('input').addEventListener('input',()=>{try{localStorage.setItem('framekeep-manual-shooting-type-v1',$('shooting-manual-type').value)}catch{}document.dispatchEvent(new CustomEvent('framekeep:shooting-type'))});
  currentPanel.append(automation,bar,manualType,currentViews,currentTable);document.querySelector('.selection-sidebar-footer').before(currentPanel);
  const dialog=document.createElement('section');dialog.id='sku-dialog';dialog.className='shooting-progress hidden';dialog.setAttribute('role','tabpanel');dialog.setAttribute('aria-labelledby','sku-open');
  dialog.innerHTML='<div class="progress-heading"><div><h2 id="sku-title">拍摄进度</h2><p class="dialog-copy">二星款号标签确认后自动记账，已拍摄款置顶；可导出全部工作表的当前拍摄记录。</p></div><div class="progress-heading-actions"><button id="sku-export" class="primary" title="导出全部工作表，包含当前已确认的拍摄状态与完成时间">导出拍摄记录</button><button id="sku-close" class="bordered">返回选片</button></div></div><div class="sku-toolbar"><button id="sku-import" class="primary">导入 Excel 建立任务</button><input id="sku-file" type="file" accept=".xlsx" hidden><select id="sku-projects" aria-label="拍摄对账任务"></select></div><div id="sku-project-info" class="dialog-copy"></div><nav id="sku-sheet-tabs" class="sku-sheet-tabs" role="tablist" aria-label="拍摄工作表"></nav><section id="sku-sheet-panel" role="tabpanel" tabindex="0" aria-label="当前拍摄工作表"><div id="sku-progress-summary" class="progress-summary" aria-live="polite"></div><div class="sku-toolbar"><input id="sku-search" placeholder="搜索款号"><select id="sku-filter"><option value="all">全部款号</option><option value="pending">未拍摄</option><option value="shot">已拍摄</option></select></div><p id="sku-message" class="dialog-copy" role="status"></p><p class="progress-note">序号按当前列表从1连续排列，原表行号单独保留。完成时间取自标签照片拍摄信息，双击标签图片可查看大图，也可点击时间查证；缺失拍摄时间时不使用文件时间代替。</p><div id="sku-rows" class="sku-table-wrap"></div></section><details id="sku-records"><summary>识别记录与待确认</summary><div id="sku-jobs"></div></details>';
  document.querySelector('.workbench > .statusbar').before(dialog);
  const readError=document.createElement('p');readError.id='sku-read-error';readError.className='dialog-copy';readError.setAttribute('role','alert');readError.hidden=true;$('sku-sheet-panel').prepend(readError);
  const syncHint='将当前监控目录（未监控时为当前打开目录）内已确认的拍摄记录同步到文档各工作表，不重新识别照片';
  const syncButton=document.createElement('button');syncButton.id='sku-sync-progress';syncButton.className='bordered';syncButton.type='button';syncButton.textContent='同步拍摄进度';syncButton.title=syncHint;
  const quickSync=syncButton.cloneNode(true);quickSync.id='shooting-sync-progress';quickSync.classList.add('shooting-sync-action');$('shooting-document').after(quickSync);
  const syncStatus=document.createElement('p');syncStatus.id='shooting-sync-status';syncStatus.className='shooting-sync-status';syncStatus.setAttribute('role','status');syncStatus.setAttribute('aria-live','polite');quickSync.after(syncStatus);
  dialog.prepend($('sku-sheet-tabs'));
  const taskToolbar=$('sku-projects').closest('.sku-toolbar');taskToolbar.classList.add('progress-task-fields');
  const projectField=document.createElement('label');projectField.className='progress-task-project';projectField.textContent='拍摄任务';$('sku-projects').before(projectField);projectField.append($('sku-projects'));
  const scopeRow=document.createElement('label');scopeRow.className='sku-scope';scopeRow.innerHTML='拍摄类型 <select id="sku-panel-scope" aria-label="对账面板拍摄工作表"></select>';taskToolbar.append(scopeRow);
  const taskActions=document.createElement('div');taskActions.className='progress-task-actions';taskActions.append($('sku-import'),syncButton);
  taskToolbar.append(projectField,scopeRow,taskActions,$('sku-file'));
  const queueButton=document.createElement('button');queueButton.id='sku-queue-open';queueButton.type='button';queueButton.className='bordered';queueButton.textContent='查看写入队列';taskActions.append(queueButton);
  const queueDialog=document.createElement('dialog');queueDialog.id='sku-queue-dialog';queueDialog.className='shooting-document-dialog';queueDialog.innerHTML='<h2>图片元数据写入队列</h2><p id="sku-queue-summary" role="status"></p><p class="dialog-copy">等待不推进时，可复制诊断信息用于定位当前文件、队列和阻塞阶段；不包含 API 密钥或图片内容。</p><textarea id="sku-queue-details" readonly aria-label="写入队列诊断信息" style="width:100%;height:300px;max-height:45vh;font-size:11px;white-space:pre;overflow:auto"></textarea><div class="dialog-actions"><button id="sku-queue-copy" type="button">复制诊断信息</button><button id="sku-queue-close" type="button">关闭</button></div>';document.body.append(queueDialog);
  let queueTimer=null,queueLoading=false;
  async function refreshQueue(){
    if(queueLoading||!queueDialog.open)return;queueLoading=true;
    try{const data=await api('/api/metadata/queue',undefined,5000),c=data.counts||{};$('sku-queue-summary').textContent=`版本 ${data.releaseId||'未知'} · 等待 ${c.queued||0} · 正在处理 ${c.running||0} · 已写入 ${c.applied||0} · 失败 ${c.failed||0}${data.database_error?' · '+data.database_error:''}`;$('sku-queue-details').value=JSON.stringify(data,null,2)}catch(error){$('sku-queue-summary').textContent='读取队列失败：'+error.message}finally{queueLoading=false;clearTimeout(queueTimer);if(queueDialog.open)queueTimer=setTimeout(refreshQueue,2000)}
  }
  queueButton.onclick=()=>{queueDialog.showModal();refreshQueue()};$('sku-queue-close').onclick=()=>queueDialog.close();queueDialog.addEventListener('close',()=>clearTimeout(queueTimer));
  $('sku-queue-copy').onclick=async()=>{try{await navigator.clipboard.writeText($('sku-queue-details').value);$('sku-queue-summary').textContent+=' · 已复制'}catch{$('sku-queue-details').focus();$('sku-queue-details').select();$('sku-queue-summary').textContent='已选中诊断信息，请按 Ctrl+C 复制'}};
  const overviewField=document.createElement('label');overviewField.className='progress-task-project';overviewField.hidden=true;overviewField.innerHTML='显示范围<select id="sku-display-range" aria-label="显示范围"><option value="today">当日全部</option><option value="history">历史全部拍摄</option></select>';taskToolbar.prepend(overviewField);
  const recognitionToolbar=$('sku-search').parentElement;recognitionToolbar.classList.add('progress-tools');
  const filters=document.createElement('div');filters.className='progress-filters';filters.setAttribute('role','search');filters.setAttribute('aria-label','筛选款号');recognitionToolbar.append(filters);filters.append($('sku-search'),$('sku-filter'));$('sku-search').setAttribute('aria-label','搜索款号');$('sku-filter').setAttribute('aria-label','拍摄状态筛选');
  let state={project:null,projects:[],rows:[],jobs:[]},busy=false,timer=null,lastJobs='',lastContext='',lastProject='',lastSheet='',mutation=0,exporting=false,commonView=false,allView=false,displayProject=null;
  let syncingProgress=false,syncWatch=null,syncMessageProject=null;
  let overview={scope:'today',rows:[],documents:[]},overviewSequence=0,overviewLoading=false,overviewError='',overviewLoadedAt=0;
  async function loadOverview(){
    const request=++overviewSequence,scope=$('sku-display-range').value;overviewLoading=true;overviewError='';
    try{const next=await api('/api/sku/documents/overview?'+new URLSearchParams({scope}),undefined,30000);if(request===overviewSequence&&scope===$('sku-display-range').value)overview=next}
    catch(error){if(request===overviewSequence)overviewError=error.message}
    finally{if(request===overviewSequence){overviewLoading=false;overviewLoadedAt=Date.now();if(allView)render()}}
  }
  $('sku-display-range').onchange=()=>{overview={scope:$('sku-display-range').value,rows:[],documents:[]};loadOverview();render()};
  function syncButtons(){
    const available=state.project&&!state.project.metadata.automatic_day;
    const source=syncWatch?.enabled?syncWatch.session_id:window.FRAMEKEEP_SESSION.id;
    for(const b of [syncButton,quickSync]){b.disabled=!available||!source||busy||!!window.FramekeepAutomation?.isSaving();b.textContent=syncingProgress?'正在同步…':'同步拍摄进度';b.title=!available?'请先导入拍摄文档':!source?'请先打开或监控拍摄文件夹':syncHint}
    if(syncMessageProject!==state.project?.id){syncMessageProject=state.project?.id;syncStatus.textContent=''}
  }
  document.addEventListener('framekeep:watch',event=>{syncWatch=event.detail;syncButtons()});
  function soften(element){if(element&&!matchMedia('(prefers-reduced-motion: reduce)').matches)element.animate([{opacity:0,transform:'translateY(12px) scale(.98)'},{opacity:1,transform:'none'}],{duration:280,easing:'cubic-bezier(.22,1,.36,1)'})}
  const comboItems=new Map();
  for(const id of ['sku-live-scope','sku-panel-scope']){
    const old=$(id),wrap=document.createElement('span');wrap.className='shooting-type-combo';
    wrap.innerHTML=`<input id="${id}" role="combobox" aria-label="拍摄类型，可选择或输入" aria-expanded="false" aria-controls="${id}-options" aria-autocomplete="list" autocomplete="off" maxlength="31" placeholder="选择或输入拍摄类型"><button class="shooting-type-arrow" type="button" aria-label="展开拍摄类型" tabindex="-1">⌄</button><span id="${id}-options" class="shooting-type-options" role="listbox" hidden></span>`;
    old.replaceWith(wrap);const input=$(id),list=$(id+'-options');
    const close=()=>{list.hidden=true;input.setAttribute('aria-expanded','false');input.removeAttribute('aria-activedescendant')};
    const open=()=>{list.hidden=false;input.setAttribute('aria-expanded','true');soften(list)};
    wrap.querySelector('button').onclick=()=>{input.focus();list.hidden?open():close()};
    input.addEventListener('input',()=>{updateTypeAction(input);close()});
    input.addEventListener('keydown',event=>{event.stopPropagation();if(event.isComposing)return;if(event.key==='Escape'){close();return}if(event.key==='Enter'){event.preventDefault();const active=list.querySelector('[aria-selected="true"]');if(!list.hidden&&active)input.value=active.dataset.type;close();updateTypeAction(input);action(()=>commitType(input))}else if(['ArrowDown','ArrowUp'].includes(event.key)){event.preventDefault();if(list.hidden)open();const items=[...list.querySelectorAll('[data-type]')],prior=items.findIndex(i=>i.getAttribute('aria-selected')==='true'),next=items[(prior+(event.key==='ArrowDown'?1:-1)+items.length)%items.length];items.forEach(i=>i.setAttribute('aria-selected',String(i===next)));if(next){input.setAttribute('aria-activedescendant',next.id);next.scrollIntoView({block:'nearest'})}}});
    list.addEventListener('click',event=>{const item=event.target.closest('[data-type]');if(item){input.value=item.dataset.type;close();updateTypeAction(input);action(()=>commitType(input))}});
    document.addEventListener('click',event=>{if(!wrap.contains(event.target))close()});
  }
  const panelTypeAction=document.createElement('button');panelTypeAction.id='sku-type-action';panelTypeAction.type='button';panelTypeAction.className='bordered';panelTypeAction.textContent='切换拍摄类型';$('sku-panel-scope').closest('label').append(panelTypeAction);panelTypeAction.onclick=()=>action(()=>commitType($('sku-panel-scope')));
  function updateTypeCombo(input,items,value){
    comboItems.set(input.id,items);const list=$(input.id+'-options');
    const html=items.map(([id,label],index)=>`<button type="button" id="${input.id}-option-${index}" role="option" aria-selected="${id===value}" data-type="${esc(id)}">${esc(label)}</button>`).join('')||'<span class="dialog-copy">首张新照片接入后自动建立文档</span>';
    if(list.innerHTML!==html)list.innerHTML=html;
    if(document.activeElement!==input)input.value=value||'';updateTypeAction(input);
  }
  function updateTypeAction(input){
    const value=input.value.trim(),items=comboItems.get(input.id)||[],known=items.some(([name])=>name.toLocaleLowerCase()===value.toLocaleLowerCase()),c=state.shooting_context;
    const pid=input.id==='sku-live-scope'?(state.shooting_project||state.project)?.id:state.project?.id;
    const current=known&&c?.project===pid&&c.scope===value,target=$(input.id==='sku-live-scope'?'sku-bind-current':'sku-type-action');
    target.textContent=value&&!known?'创建类型':current?'已用于当前拍摄':'切换拍摄类型';target.disabled=busy||!pid||!value||current;target.classList.toggle('active',current);
  }
  async function commitType(input){
    const value=input.value.trim(),p=input.id==='sku-live-scope'?(state.shooting_project||state.project):state.project;
    if(!p||!value)throw new Error('请先接入现场照片或导入文档，再选择或输入拍摄类型');
    const known=p.metadata.sheets.find(s=>s.name.toLocaleLowerCase()===value.toLocaleLowerCase());
    await FramekeepIO.flush();
    state=await api(known?'/api/sku/configure':'/api/sku/type',known?{project_id:p.id,session_id:window.FRAMEKEEP_SESSION.id,scope:known.name,enabled:true}:{project_id:p.id,session_id:window.FRAMEKEEP_SESSION.id,name:value},90000);
    input.blur();viewScope=known?.name||value;allView=commonView=false;render();message(known?'当前拍摄类型：'+known.name:'已创建类型，后续拍摄记录到「'+value+'」');
  }
  const history=document.createElement('dialog');history.id='sku-history-dialog';history.className='shooting-document-dialog';history.setAttribute('aria-labelledby','sku-history-title');
  history.innerHTML='<div class="dialog-heading"><div><h2 id="sku-history-title">历史拍摄</h2><p class="dialog-copy">双击文档重新加载清单；查看历史不改变当前拍摄绑定。</p></div><button id="sku-history-close" type="button">关闭</button></div><div class="shooting-history-tools"><input id="sku-history-search" aria-label="搜索历史文档" placeholder="搜索文档名称、日期或拍摄类型"><label><input id="sku-history-removed" type="checkbox"> 已移除文档</label></div><div id="sku-history-rows" class="sku-table-wrap"></div><p id="sku-history-status" role="status"></p>';
  document.body.append(history);$('sku-history-close').onclick=()=>history.close();$('sku-history-search').oninput=renderHistory;$('sku-history-removed').onchange=renderHistory;
  const removeButton=document.createElement('button');removeButton.id='sku-remove-document';removeButton.type='button';removeButton.className='bordered';removeButton.textContent='移除文档…';taskActions.append(removeButton);
  const historyHint=document.createElement('p');historyHint.id='sku-history-hint';historyHint.className='dialog-copy';taskToolbar.after(historyHint);
  const sideHistory=document.createElement('button');sideHistory.id='shooting-history';sideHistory.type='button';sideHistory.textContent='历史拍摄';$('shooting-document').after(sideHistory);sideHistory.onclick=openHistory;
  const removeDialog=document.createElement('dialog');removeDialog.id='sku-remove-confirm';removeDialog.className='shooting-document-dialog';removeDialog.innerHTML='<h2>移除拍摄文档</h2><p id="sku-remove-name"></p><p class="dialog-copy">文档将移出列表并解除拍摄绑定。保留照片、原始 Excel、已确认款号和拍摄记录，可从历史拍摄恢复。当前文档移除后，下一张新照片将使用当日自动文档。</p><div class="dialog-actions"><button id="sku-remove-cancel" type="button">取消</button><button id="sku-remove-accept" type="button" class="primary">移除文档</button></div>';document.body.append(removeDialog);
  let removingId='';function askRemove(pid){const p=(state.documents||[]).find(p=>p.id===pid);if(!p)return;removingId=pid;$('sku-remove-name').textContent=p.name;removeDialog.showModal();soften(removeDialog)}
  removeButton.onclick=()=>askRemove(state.project?.id);$('sku-remove-cancel').onclick=()=>removeDialog.close();$('sku-remove-accept').onclick=()=>action(async()=>{state=await api('/api/sku/document/manage',{project_id:removingId,action:'remove'});removeDialog.close();viewScope='';render();renderHistory();message('文档已移除，照片和历史记录保留')});
  async function openHistory(){try{const data=await api('/api/sku/documents');state.documents=data.documents;renderHistory();if(!history.open)history.showModal();soften(history);$('sku-history-search').focus()}catch(error){Framekeep.notify(error.message)}}
  function renderHistory(){
    if(!$('sku-history-rows'))return;const query=$('sku-history-search').value.trim().toLowerCase(),removed=$('sku-history-removed').checked;
    const rows=(state.documents||[]).filter(p=>removed?p.removed:!p.removed&&!p.today).filter(p=>[p.name,p.day,...p.types].join(' ').toLowerCase().includes(query));
    const html='<table><thead><tr><th>拍摄文档</th><th>添加日期</th><th>拍摄类型</th><th>操作</th></tr></thead><tbody>'+rows.map(p=>`<tr data-history-project="${esc(p.id)}" tabindex="0"><td><strong>${esc(p.name)}</strong><small>${p.automatic?'自动创建':'导入文档'}</small></td><td>${esc(p.day)}</td><td>${p.types.map(esc).join('、')}</td><td>${p.removed?'<button data-restore-document="'+esc(p.id)+'">恢复</button>':'<button data-load-document="'+esc(p.id)+'">加载清单</button><button data-remove-document="'+esc(p.id)+'">移除</button>'}</td></tr>`).join('')+'</tbody></table>'+(rows.length?'':'<p class="dialog-copy">暂无匹配文档</p>');
    if($('sku-history-rows').innerHTML!==html)$('sku-history-rows').innerHTML=html;
  }
  async function loadHistory(pid){state=await api('/api/sku/activate',{project_id:pid});viewScope='';lastJobs='';render();history.close();message('已加载历史文档；当前拍摄绑定保持不变')}
  history.addEventListener('dblclick',event=>{if(event.target.closest('button,input'))return;const row=event.target.closest('[data-history-project]');if(row&&!$('sku-history-removed').checked)action(()=>loadHistory(row.dataset.historyProject))});
  history.addEventListener('keydown',event=>{if(event.key==='Enter'&&event.target.matches('[data-history-project]')&&!$('sku-history-removed').checked){event.preventDefault();action(()=>loadHistory(event.target.dataset.historyProject))}});
  history.addEventListener('click',event=>{const load=event.target.closest('[data-load-document]'),remove=event.target.closest('[data-remove-document]'),restore=event.target.closest('[data-restore-document]');if(load)action(()=>loadHistory(load.dataset.loadDocument));if(remove)askRemove(remove.dataset.removeDocument);if(restore)action(async()=>{state=await api('/api/sku/document/manage',{project_id:restore.dataset.restoreDocument,action:'restore'});render();renderHistory();$('sku-history-status').textContent='已恢复，可切回历史列表加载文档'})});
  function editSku(target){
    if(busy||rowEditing)return;const row=(allView?overview.rows:state.rows).find(r=>r.id===target.dataset.editSku);if(!row)return;
    rowEditing=true;const original=row.shoot_sku||row.sku,input=document.createElement('input');input.className='sku-code-input';input.value=original;input.maxLength=80;input.setAttribute('aria-label','修改款号');target.replaceChildren(input);soften(input);input.focus();input.select();let finished=false;
    async function finish(cancel){if(finished)return;finished=true;const code=input.value.trim(),pid=row.project;rowEditing=false;if(cancel||code===original){renderRows();return}if(!code){message('款号不能为空');renderRows();return}
      await action(async()=>{try{const result=await api('/api/sku/row/rename',{project_id:pid,row_id:row.id,old_sku:row.sku,sku:code},20000);state=result.state;if(allView)await loadOverview();render();message('款号已保存')}catch(error){renderRows();throw error}});
    }
    input.onkeydown=event=>{event.stopPropagation();if(event.isComposing)return;if(event.key==='Enter'){event.preventDefault();finish(false)}if(event.key==='Escape'){event.preventDefault();finish(true)}};input.onblur=()=>finish(false);
  }
  $('sku-rows').addEventListener('dblclick',event=>{const target=event.target.closest('[data-edit-sku]');if(target&&!event.target.matches('input'))editSku(target)});
  $('sku-rows').addEventListener('keydown',event=>{if(event.key==='Enter'&&event.target.matches('[data-edit-sku]')){event.preventDefault();editSku(event.target)}});
  window.FramekeepSku={context:()=>{const c=state.shooting_context;return c?{project_id:c.project,scope:c.scope}:null},shootingTypes:()=>{const p=state.shooting_project||state.project,c=state.shooting_context;return p?{project_id:p.id,name:p.name,scope:c?.project===p.id?c.scope:p.scope,sheets:p.metadata.sheets.filter(s=>!s.skipped).map(s=>s.name),counts:p.id===state.project?.id?(state.progress||[]).map(s=>({sheet:s.sheet,total:s.total})):[]}:null},isBusy:()=>busy,importPlan,bindCurrent:(scope,projectId)=>exclusive(async()=>{if(state.project?.id!==projectId)throw new Error('拍摄计划已变化，请重新选择');await configure(scope,true)})};
  FramekeepSku.currentType=()=>state.shooting_context?.scope||state.project?.scope||'';
  const status={queued:'等待二星稳定',waiting_model:'待配置模型',running:'识别中',matched:'已自动匹配',confirmed:'人工确认',review:'待确认',failed:'识别失败',cancelled:'已撤销',ignored:'已忽略'};
  function message(text){$('sku-message').textContent=text}
  function options(select,items,value,placeholder){if(select.tagName==='INPUT'){updateTypeCombo(select,items,value);return;}const next=(placeholder?`<option value="">${placeholder}</option>`:'')+items.map(([id,label])=>`<option value="${esc(id)}">${esc(label)}</option>`).join('');if(select.innerHTML!==next)select.innerHTML=next;select.value=value||''}
  const headerTitle=(key,fallback)=>state.project?.presentation?.headers?.[key]||(fallback==='记录时间'?'入库时间':fallback);
  const columnKey=(sheet,column)=>column.index===sheet.sku_col?'sku':`column:${sheet.name}:${column.letter}`;
  const header=(key,title,extra='')=>`<th scope="col" ${extra}><button type="button" class="sku-header-label" data-header-key="${esc(key)}" title="双击修改列名称">${esc(headerTitle(key,title))}</button></th>`;
  function commonGroups(){
    const groups=new Map(),sheets=new Set(state.project?.metadata.sheets.filter(s=>!s.skipped).map(s=>s.name)||[]);
    for(const row of state.rows){
      if(!sheets.has(row.sheet))continue;const key=row.normalized||row.sku;
      if(!groups.has(key))groups.set(key,{sku:row.sku,normalized:key,sheets:new Map(),has_shots:false});const group=groups.get(key);
      group.has_shots ||= row.status==='已拍摄';
      group.sheets.set(row.sheet,group.sheets.get(row.sheet)||row.status==='已拍摄');if(!group.sku_image&&row.sku_image)group.sku_image=row.sku_image;
    }
    return [...groups.values()].filter(group=>group.sheets.size>1).map(group=>({...group,shot:[...group.sheets.values()].every(Boolean)}));
  }
  function commonMemberships(group){return `<span class="sku-common-memberships">${[...group.sheets].map(([sheet,shot])=>`<button type="button" data-common-sheet="${esc(sheet)}" title="查看 ${esc(sheet)} 中的 ${esc(group.sku)}"><span>${esc(sheet)}</span><span class="sku-state ${shot?'sku-shot':''}">${shot?'已拍摄':'未拍摄'}</span></button>`).join('')}</span>`}
  function renderSheetTabs(matches){
    const p=viewedProject(),sheetTabs=$('sku-sheet-tabs'),focused=sheetTabs.contains(document.activeElement)?document.activeElement.id:null,progress=state.progress||[],stop=focused||(allView?'sku-all-tab':commonView?'sku-common-tab':`sku-sheet-tab-${Math.max(0,progress.findIndex(s=>s.sheet===p?.scope))}`);
    const sheetButtons=`<button id="sku-all-tab" role="tab" aria-controls="sku-sheet-panel" data-all-view aria-selected="${allView}" aria-pressed="${allView}" tabindex="${stop==='sku-all-tab'?0:-1}">全部${allView?` <span>${overview.rows.length}</span>`:''}</button>`+progress.map((s,index)=>`<button id="sku-sheet-tab-${index}" role="tab" aria-controls="sku-sheet-panel" data-progress-sheet="${esc(s.sheet)}" aria-selected="${!allView&&!commonView&&s.sheet===p?.scope}" aria-pressed="${!allView&&!commonView&&s.sheet===p?.scope}" tabindex="${'sku-sheet-tab-'+index===stop?0:-1}" title="${esc(s.sheet)}：${matches?'匹配款号':'已拍摄'} ${matches?matches.get(s.sheet)?.size||0:s.shot} / ${s.total}">${esc(s.sheet)} <span>${matches?matches.get(s.sheet)?.size||0:s.shot}/${s.total}</span></button>`).join('')+(p?`<button id="sku-common-tab" role="tab" aria-controls="sku-sheet-panel" data-common-view aria-selected="${commonView}" aria-pressed="${commonView}" tabindex="${stop==='sku-common-tab'?0:-1}">公共 <span>${commonGroups().length}</span></button>`:'');
    if(sheetTabs.innerHTML!==sheetButtons){sheetTabs.innerHTML=sheetButtons;if(focused)$(focused)?.focus({preventScroll:true})}
    const activeTab=allView?$('sku-all-tab'):commonView?$('sku-common-tab'):[...sheetTabs.children].find(b=>b.dataset.progressSheet===p?.scope);if(activeTab)$('sku-sheet-panel').setAttribute('aria-labelledby',activeTab.id);else $('sku-sheet-panel').removeAttribute('aria-labelledby');
  }
  let viewScope='',rowEditing=false;
  const viewedProject=()=>state.project?{...state.project,scope:viewScope||state.project.scope}:null;
  function renderOverviewRows(query,filter){
    const rows=overview.rows.filter(r=>(!query||[r.sku,r.shoot_sku,r.document_name,r.sheet].join(' ').toUpperCase().includes(query))&&(filter==='all'||(filter==='shot')===(r.status==='已拍摄'))).sort((a,b)=>b.document_day.localeCompare(a.document_day)||a.document_name.localeCompare(b.document_name)||a.sheet.localeCompare(b.sheet)||a.excel_row-b.excel_row),days=new Map();
    for(const row of rows){if(!days.has(row.document_day))days.set(row.document_day,[]);days.get(row.document_day).push(row)}
    const html=[...days].map(([day,items])=>`<section class="sku-day-group" aria-label="${esc(day)} 拍摄清单"><h3 class="sku-day-divider">${esc(day)}<small>${items.length} 项 · ${items.filter(r=>r.status==='已拍摄').length} 已拍摄</small></h3><table><thead><tr><th>序号</th><th>款号</th><th>款号图片</th><th>标签图片</th><th>拍摄类型</th><th>拍摄状态</th><th>拍摄完成时间</th><th>来源文档</th></tr></thead><tbody>${items.map((r,index)=>`<tr data-sku-row="${esc(r.id)}" data-source-project="${esc(r.project)}" class="${r.status==='已拍摄'?'progress-shot':''}"><td>${index+1}</td><td><strong tabindex="0" role="button" data-edit-sku="${esc(r.id)}" title="双击修改款号并后台同步图片资料">${esc(r.shoot_sku||r.sku)}</strong></td><td class="sku-product-images">${reconciliationImage(r)}</td><td class="sku-product-images">${labelImage(r)}</td><td>${esc(r.sheet)}</td><td><span class="sku-state ${r.status==='已拍摄'?'sku-shot':''}">${esc(r.status)}</span>${metadataState(r)}</td><td>${r.completed_at?(r.completion?`<button data-completion="${esc(r.id)}">${esc(localTime(r.completed_at))}</button>`:esc(localTime(r.completed_at))):'—'}</td><td><button data-overview-document="${esc(r.project)}" data-overview-sheet="${esc(r.sheet)}">${esc(r.document_name)}</button></td></tr>`).join('')}</tbody></table></section>`).join('')||`<p class="dialog-copy">${esc(overviewError|| (overviewLoading?'正在读取拍摄清单…':query||filter!=='all'?'没有符合条件的拍摄任务':overview.scope==='today'?'当日暂无拍摄任务':'暂无拍摄任务'))}</p>`;
    if($('sku-rows').innerHTML!==html)$('sku-rows').innerHTML=html;
    if($('sku-search-summary'))$('sku-search-summary').hidden=true;
  }
  function renderRows(){
    if(rowEditing||$('sku-rows').querySelector('.sku-header-input'))return;
    const query=$('sku-search').value.trim().toUpperCase(),filter=$('sku-filter').value,p=viewedProject();
    if(!p&&!readError.hidden){$('sku-rows').innerHTML='<p class="dialog-copy">正在重试读取已保存的拍摄文档。</p>';return}
    if(allView){renderSheetTabs();renderOverviewRows(query,filter);return}
    const sheet=p?.metadata.sheets.find(s=>s.name===p.scope),columns=(sheet?.columns||[]).filter(c=>c.title.trim()!=='备注'),notes=(sheet?.columns||[]).filter(c=>c.title.trim()==='备注');
    const memberships=new Map(),sheetNames=(p?.metadata.sheets||[]).filter(s=>!s.skipped).map(s=>s.name);
    if(query)for(const row of state.rows){const key=row.normalized||row.sku;if(!memberships.has(key))memberships.set(key,new Set());memberships.get(key).add(row.sheet)}
    const matchedRows=state.rows.filter(r=>(!query||r.sku.toUpperCase().includes(query))&&(filter==='all'||(filter==='shot')===(r.status==='已拍摄')));
    const matches=query?new Map(sheetNames.map(name=>[name,new Set()])):null;
    if(matches)for(const row of matchedRows)matches.get(row.sheet)?.add(row.normalized||row.sku);
    const searchSummary=$('sku-search-summary');if(searchSummary){searchSummary.hidden=!query;searchSummary.textContent=query?'款号搜索结果：'+sheetNames.map(name=>`${name} ${matches.get(name).size}张`).join('，'):''}
    renderSheetTabs(matches);
    if(commonView){
      const groups=commonGroups().filter(r=>(!query||r.sku.toUpperCase().includes(query))&&(filter==='all'||(filter==='shot')===r.shot));
      const html=groups.length?'<table><thead><tr><th scope="col" class="sku-sequence">序号</th><th scope="col">款号</th><th scope="col">所属工作表与拍摄状态</th><th>款号图片</th></tr></thead><tbody>'+groups.map((r,index)=>`<tr class="${r.shot?'progress-shot':''}"><td class="sku-sequence">${index+1}</td><td><strong>${esc(r.sku)}</strong></td><td>${commonMemberships(r)}</td><td class="sku-product-images">${reconciliationImage(r)}</td></tr>`).join('')+'</tbody></table>':'<p class="dialog-copy">没有符合条件的公共款号。仅汇总出现在多个工作表中的款号。</p>';
      if($('sku-rows').innerHTML!==html)$('sku-rows').innerHTML=html;return;
    }
    const rows=matchedRows.filter(r=>allView||r.sheet===p?.scope).sort((a,b)=>(b.status==='已拍摄')-(a.status==='已拍摄')||String(b.completed_at||'').localeCompare(String(a.completed_at||''))||a.excel_row-b.excel_row);
    function images(r,list){return list.map(im=>`<button class="sku-product-image" data-sku-image="${esc(im.url)}" data-sku-name="${esc(r.sku)}" title="查看 ${esc(r.sku)} 产品图"><img src="${esc(im.url)}" alt="${esc(r.sku)} 产品图" loading="lazy"></button>`).join('')}
    function skuText(r){const sheets=memberships.get(r.normalized||r.sku);return `<span class="sku-code">${query?`<span class="sku-sheet-buttons" role="group" aria-label="${esc(r.sku)} 所属工作表">${sheetNames.filter(name=>sheets?.has(name)).map(name=>`<button class="sku-sheet-badge" data-sku-sheet="${esc(name)}" aria-pressed="${name===r.sheet}" title="查看 ${esc(name)} 工作表中的 ${esc(r.sku)}">${esc(name)}</button>`).join('')}</span>`:''}<strong tabindex="0" role="button" data-edit-sku="${esc(r.id)}" title="双击修改款号并同步图片元数据；原款号：${esc(r.sku)}">${esc(r.shoot_sku||r.sku)}</strong></span>`}
    const html=rows.length?'<table><thead><tr>'+header('seq','序号','class="sku-sequence"')+header('row','原表行')+columns.map(c=>header(columnKey(sheet,c),c.title)+(c.index===sheet.sku_col?header('photo','款号图片')+header('label','标签图片'):'')).join('')+header('type','拍摄类型')+header('status','拍摄状态')+header('time','拍摄完成时间')+notes.map(c=>header(columnKey(sheet,c),c.title)).join('')+'</tr></thead><tbody>'+rows.map((r,index)=>`<tr data-sku-row="${esc(r.id)}" data-source-project="${esc(r.project)}" class="${r.status==='已拍摄'?'progress-shot':''}"><td class="sku-sequence" aria-label="序号 ${index+1}">${index+1}</td><td class="progress-row-number">${r.excel_row}</td>${columns.map(c=>{const imgs=(r.images||[]).filter(im=>im.cell.replace(/\d/g,'')===c.letter);return `<td class="${imgs.length?'sku-product-images':''}">${imgs.length?images(r,imgs):c.index===sheet.sku_col?skuText(r):esc(sourceValue(r,c))}</td>`+(c.index===sheet.sku_col?`<td class="sku-product-images">${reconciliationImage(r)}</td><td class="sku-product-images">${labelImage(r)}</td>`:'')}).join('')}<td class="sku-type-cell">${esc(r.sheet)}</td><td><span class="sku-state ${r.status==='已拍摄'?'sku-shot':''}">${esc(r.status)}</span>${metadataState(r)}</td><td class="progress-time">${r.completed_at?`${r.completion?`<button data-completion="${esc(r.id)}">${esc(localTime(r.completed_at))}</button>`:esc(localTime(r.completed_at))}`:r.status==='已拍摄'?'<span class="time-missing">拍摄时间缺失</span>':'—'}</td>${notes.map(c=>`<td>${esc(sourceValue(r,c))}</td>`).join('')}</tr>`).join('')+'</tbody></table>':`<p class="dialog-copy">${p?.scope?'没有符合条件的款号':'请选择当前拍摄工作表，各页分别统计进度。'}</p>`;
    if($('sku-rows').innerHTML!==html)$('sku-rows').innerHTML=html;
  }
  function labelImage(row){const image=row.label_image;return image?`<button class="sku-product-image" data-sku-label data-sku-image="${esc(image.url.replace('size=240','size=2048'))}" data-sku-name="${esc(row.sku)} 标签照片" title="双击查看 ${esc(row.sku)} 标签大图"><img src="${esc(image.url)}" alt="${esc(row.sku)} 标签图片" loading="lazy"></button>`:'—'}
  function metadataState(row){
    const m=row.metadata;if(!m)return '';const labels={queued:'等待后台写入',writing:'正在写入中',failed:'款号写入未完成',missing:'款号写入任务待恢复'};
    if(!labels[m.status])return '';const reason=[m.worker_message,...(m.jobs||[]).filter(j=>j.reason).map(j=>j.path+'：'+j.reason)].filter(Boolean).join('；');
    return `<small class="sku-metadata-status" title="${esc(reason)}">${labels[m.status]} ${m.applied}/${m.total}${m.worker_message?' · '+esc(m.worker_message):''}</small>${m.status==='failed'&&m.operation_ids?.length?`<button type="button" data-sku-write-retry="${esc(JSON.stringify(m.operation_ids))}">重试写入</button>`:''}`;
  }
  $('sku-rows').addEventListener('click',event=>{const button=event.target.closest('[data-sku-write-retry]');if(button)action(async()=>{await Promise.all(JSON.parse(button.dataset.skuWriteRetry).map(id=>api('/api/metadata/retry',{operation_id:id})));await load()})});
  function reconciliationImage(row){const shot=typeof row?.status==='string'?row.status==='已拍摄':row?.has_shots||row?.shot;if(!shot)return '<span class="time-missing">—</span>';const image=row?.sku_image;return `<button class="sku-product-image sku-cover-picker" data-sku-cover="${esc(row.normalized||row.sku)}" data-sku-name="${esc(row.sku)}" title="选择 ${esc(row.sku)} 款号图片">${image?`<img src="${esc(image.url)}" alt="${esc(row.sku)} 款号图片" loading="lazy">`:'<span>选择图片</span>'}</button>`}
  function animateHeader(element){if(element&&!matchMedia('(prefers-reduced-motion: reduce)').matches)element.animate([{backgroundColor:'var(--theme-selected)',opacity:.65},{backgroundColor:'transparent',opacity:1}],{duration:200,easing:getComputedStyle(document.documentElement).getPropertyValue('--motion-ease').trim()||'cubic-bezier(.22,1,.36,1)'})}
  function editHeader(target){
    if(busy||target.querySelector('input')||!state.project)return;
    const key=target.dataset.headerKey,original=target.textContent,pid=state.project.id,input=document.createElement('input');
    input.className='sku-header-input';input.value=original;input.maxLength=40;input.setAttribute('aria-label','修改列名称');target.replaceChildren(input);input.focus();input.select();let finished=false;
    async function finish(cancel,focus=false){
      if(finished)return;finished=true;const title=input.value.trim();input.replaceWith(document.createTextNode(original));
      if(cancel||title===original){renderRows();return}
      if(!title){message('列名称不能为空，已保留原名称');renderRows();return}
      try{await exclusive(async()=>{state=await api('/api/sku/header',{project_id:pid,key,title});render();message('列名称已保存，导出时会应用到文档。')});const updated=[...$('sku-rows').querySelectorAll('[data-header-key]')].find(el=>el.dataset.headerKey===key);animateHeader(updated);if(focus)updated?.focus({preventScroll:true})}
      catch(error){renderRows();message(error.message);Framekeep.notify(error.message)}
    }
    input.addEventListener('keydown',event=>{event.stopPropagation();if(event.isComposing)return;if(event.key==='Enter'){event.preventDefault();finish(false,true)}else if(event.key==='Escape'){event.preventDefault();finish(true,true)}});
    input.addEventListener('dblclick',event=>event.stopPropagation());input.addEventListener('blur',()=>finish(false));animateHeader(input);
  }
  $('sku-rows').addEventListener('dblclick',event=>{if(event.target.closest('input,textarea,select'))return;const cell=event.target.closest('th'),target=cell?.querySelector('[data-header-key]');if(target)editHeader(target);const label=event.target.closest('[data-sku-label]');if(label)openSkuImage(label)});
  $('sku-rows').addEventListener('keydown',event=>{const target=event.target.closest('[data-header-key]');if(target&&event.target===target&&event.key==='Enter'){event.preventDefault();editHeader(target)}});
  const galleryDialog=document.createElement('dialog');galleryDialog.id='sku-cover-dialog';galleryDialog.setAttribute('aria-labelledby','sku-cover-title');
  galleryDialog.innerHTML='<div class="dialog-heading"><div><h2 id="sku-cover-title">SKU 款号组图</h2><p id="sku-cover-context" class="dialog-copy"></p></div><button id="sku-cover-close" type="button">关闭 <kbd>Esc</kbd></button></div><div class="sku-cover-controls"><label>款号<select id="sku-cover-group" aria-label="当前 SKU 组图" disabled></select></label><span id="sku-cover-count" role="status"></span></div><div id="sku-cover-groups"></div><p id="sku-cover-message" role="status"></p><footer class="sku-cover-footer"><div><button id="sku-cover-prev" type="button">上一页</button><button id="sku-cover-next" type="button">下一页</button></div><span>仅从当前款号区间内选择款号图片</span><div><button id="sku-cover-cancel" type="button">取消</button><button id="sku-cover-save" type="button" class="primary">保存选择</button></div></footer>';
  document.body.append(galleryDialog);
  let galleryProject='',gallerySku='',galleryOffset=0,galleryData=null,galleryChoices={},galleryRequest=0,galleryOrigin=null,gallerySaving=false;
  function paintGallery(){
    if(!galleryData)return;
    const grouped=new Map();for(const image of galleryData.images){if(!grouped.has(image.normalized))grouped.set(image.normalized,[]);grouped.get(image.normalized).push(image)}
    $('sku-cover-groups').innerHTML=[...grouped].map(([key,images])=>{const group=galleryData.groups.find(g=>g.normalized===key),selected=galleryChoices[key]||group.selected_key;return `<section class="sku-cover-group"><header><h3>${esc(group.sku)}</h3><span>${group.count} 张</span></header><div class="sku-cover-grid">${images.map(image=>`<button type="button" class="sku-cover-option" data-cover-key="${esc(image.key)}" data-cover-sku="${esc(key)}" aria-pressed="${selected===image.key}"><img src="${esc(image.url)}" alt="${esc(image.sku+' '+image.name)}" loading="lazy"><span>${selected===image.key?'✓ 已选 · ':''}${esc(image.name)}</span></button>`).join('')}</div></section>`}).join('')||'<p class="dialog-copy">该款号暂时没有可选图片。</p>';
    $('sku-cover-count').textContent=galleryData.total?`${galleryData.offset+1}–${Math.min(galleryData.offset+galleryData.images.length,galleryData.total)} / ${galleryData.total} 张`:'0 张';
    $('sku-cover-prev').disabled=gallerySaving||!galleryData.offset;$('sku-cover-next').disabled=gallerySaving||!galleryData.has_more;
    $('sku-cover-save').disabled=gallerySaving||!Object.keys(galleryChoices).length;
  }
  async function loadGallery(){
    const request=++galleryRequest;$('sku-cover-message').textContent='正在加载 SKU 组图…';$('sku-cover-prev').disabled=$('sku-cover-next').disabled=$('sku-cover-save').disabled=true;
    try{const data=await api('/api/sku/gallery?'+new URLSearchParams({project_id:galleryProject,sku:gallerySku,offset:galleryOffset,limit:60}));if(request!==galleryRequest||!galleryDialog.open)return;if(data.groups.some(g=>g.normalized!==gallerySku)||data.images.some(im=>im.normalized!==gallerySku))throw new Error('款号组图范围已变化，请关闭后重新打开');galleryData=data;options($('sku-cover-group'),data.groups.map(g=>[g.normalized,`${g.sku}（${g.count} 张）`]),gallerySku);paintGallery();$('sku-cover-message').textContent=''}
    catch(error){if(request===galleryRequest){$('sku-cover-message').textContent=error.message;$('sku-cover-groups').innerHTML=''}}
  }
  function openSkuGallery(target){
    if(!target.dataset.skuCover||(!state.project&&!allView))return;galleryProject=target.closest('[data-source-project]')?.dataset.sourceProject||state.project?.id;gallerySku=target.dataset.skuCover;galleryOrigin=target;galleryOffset=0;galleryData=null;galleryChoices={};gallerySaving=false;options($('sku-cover-group'),[[gallerySku,target.dataset.skuName]],gallerySku);$('sku-cover-context').textContent='当前款号：'+target.dataset.skuName+' · 仅显示该款号的拍摄区间图片';$('sku-cover-groups').innerHTML='';galleryDialog.showModal();$('sku-cover-close').focus();loadGallery();
  }
  function closeGallery(){if(!gallerySaving&&galleryDialog.open)galleryDialog.close()}
  for(const id of ['sku-cover-close','sku-cover-cancel'])$(id).onclick=closeGallery;
  galleryDialog.addEventListener('cancel',event=>{event.preventDefault();closeGallery()});galleryDialog.addEventListener('close',()=>{++galleryRequest;if(galleryOrigin?.isConnected)galleryOrigin.focus({preventScroll:true});galleryOrigin=null});
  $('sku-cover-group').onchange=()=>{galleryOffset=0;loadGallery()};$('sku-cover-prev').onclick=()=>{galleryOffset=Math.max(0,galleryOffset-60);loadGallery()};$('sku-cover-next').onclick=()=>{galleryOffset+=60;loadGallery()};
  $('sku-cover-groups').onclick=event=>{const image=event.target.closest('[data-cover-key]');if(!image||gallerySaving)return;galleryChoices[image.dataset.coverSku]=image.dataset.coverKey;paintGallery()};
  $('sku-cover-save').onclick=async()=>{
    if(gallerySaving||!Object.keys(galleryChoices).length)return;
    gallerySaving=true;paintGallery();$('sku-cover-group').disabled=true;
    try{if(!allView&&!(state.projects.find(p=>p.id===state.project?.id)?.source_ids||[state.project?.id]).includes(galleryProject))throw new Error('拍摄任务已变化，请关闭后重新选择');await exclusive(async()=>{state=await api('/api/sku/cover',{project_id:galleryProject,sku:gallerySku,choices:galleryChoices});if(allView)await loadOverview();render()});gallerySaving=false;closeGallery();message('款号图片引用已保存，各工作表与汇总已同步，导出时再嵌入图片。')}
    catch(error){$('sku-cover-message').textContent=error.message;gallerySaving=false;paintGallery()}
    finally{$('sku-cover-group').disabled=true}
  };
  document.addEventListener('framekeep:workspace',closeGallery);
  const imageDialog=document.createElement('dialog');imageDialog.className='sku-image-dialog';imageDialog.setAttribute('aria-labelledby','sku-image-title');
  imageDialog.innerHTML='<div class="dialog-heading"><h2 id="sku-image-title"></h2><button id="sku-image-close" aria-label="关闭图片">关闭 <kbd>Esc</kbd></button></div><div id="sku-image-stage" tabindex="0" role="region" aria-label="图片视口，滚轮缩放，放大后拖拽平移"><img alt="" draggable="false" hidden><p id="sku-image-status" role="status"></p></div><footer class="sku-image-toolbar"><div><button id="sku-image-minus" aria-label="缩小图片">−</button><output id="sku-image-scale" title="相对于适应窗口的缩放比例">100%</output><button id="sku-image-plus" aria-label="放大图片">＋</button><button id="sku-image-fit">适应窗口</button></div><span>滚轮缩放 · 拖拽平移 · 双击复位</span></footer>';document.body.append(imageDialog);
  const imageStage=$('sku-image-stage'),skuImage=imageStage.querySelector('img');
  let imageScale=1,imageX=0,imageY=0,imageWidth=0,imageHeight=0,imageDrag=null,imageOrigin=null,imageRequest=0;
  const clampImage=(value,min,max)=>Math.max(min,Math.min(max,value));
  function paintSkuImage(){
    const limitX=Math.max(0,(imageWidth*imageScale-imageStage.clientWidth)/2),limitY=Math.max(0,(imageHeight*imageScale-imageStage.clientHeight)/2);
    imageX=clampImage(imageX,-limitX,limitX);imageY=clampImage(imageY,-limitY,limitY);
    skuImage.style.transform=`translate(-50%,-50%) translate(${imageX}px,${imageY}px) scale(${imageScale})`;
    imageStage.classList.toggle('is-zoomed',imageScale>1);$('sku-image-scale').value=Math.round(imageScale*100)+'%';
    $('sku-image-minus').disabled=skuImage.hidden||imageScale<=1;$('sku-image-plus').disabled=skuImage.hidden||imageScale>=16;$('sku-image-fit').disabled=skuImage.hidden;
  }
  function layoutSkuImage(){
    if(!imageDialog.open||skuImage.hidden||!skuImage.naturalWidth||!imageStage.clientWidth||!imageStage.clientHeight)return;
    const fit=Math.min(imageStage.clientWidth/skuImage.naturalWidth,imageStage.clientHeight/skuImage.naturalHeight);
    imageWidth=skuImage.naturalWidth*fit;imageHeight=skuImage.naturalHeight*fit;skuImage.style.width=imageWidth+'px';skuImage.style.height=imageHeight+'px';paintSkuImage();
  }
  function endImageDrag(){if(!imageDrag)return;const id=imageDrag.id;imageDrag=null;imageStage.classList.remove('panning');if(imageStage.hasPointerCapture(id))imageStage.releasePointerCapture(id)}
  function fitSkuImage(){endImageDrag();imageScale=1;imageX=imageY=0;paintSkuImage()}
  function zoomSkuImage(next,clientX,clientY){
    if(skuImage.hidden)return;endImageDrag();next=clampImage(next,1,16);const rect=imageStage.getBoundingClientRect();
    const ax=(clientX??rect.left+rect.width/2)-rect.left-rect.width/2,ay=(clientY??rect.top+rect.height/2)-rect.top-rect.height/2;
    imageX=ax-(ax-imageX)*next/imageScale;imageY=ay-(ay-imageY)*next/imageScale;imageScale=next;paintSkuImage();
  }
  function closeSkuImage(){if(imageDialog.open)imageDialog.close()}
  $('sku-image-close').onclick=closeSkuImage;imageDialog.addEventListener('cancel',event=>{event.preventDefault();closeSkuImage()});
  imageDialog.addEventListener('close',()=>{
    ++imageRequest;endImageDrag();skuImage.onload=skuImage.onerror=null;skuImage.hidden=true;skuImage.removeAttribute('src');
    const origin=imageOrigin?.isConnected?imageOrigin:[...$('sku-rows').querySelectorAll('[data-completion]')].find(node=>node.dataset.completion===imageOrigin?.dataset.completion);
    origin?.focus({preventScroll:true});imageOrigin=null;
  });
  $('sku-image-minus').onclick=()=>zoomSkuImage(imageScale/1.25);$('sku-image-plus').onclick=()=>zoomSkuImage(imageScale*1.25);$('sku-image-fit').onclick=fitSkuImage;
  imageStage.addEventListener('wheel',event=>{event.preventDefault();const delta=event.deltaY*(event.deltaMode===1?16:event.deltaMode===2?imageStage.clientHeight:1);zoomSkuImage(imageScale*Math.exp(-clampImage(delta,-600,600)*.002),event.clientX,event.clientY)},{passive:false});
  imageStage.addEventListener('dblclick',event=>{event.preventDefault();if(imageScale>1)fitSkuImage();else zoomSkuImage(2,event.clientX,event.clientY)});
  imageStage.addEventListener('pointerdown',event=>{if(skuImage.hidden||imageScale<=1||![0,1].includes(event.button))return;event.preventDefault();endImageDrag();imageDrag={id:event.pointerId,button:event.button,startX:event.clientX,startY:event.clientY,x:imageX,y:imageY};imageStage.setPointerCapture(event.pointerId);imageStage.classList.add('panning')});
  imageStage.addEventListener('pointermove',event=>{if(!imageDrag||imageDrag.id!==event.pointerId)return;if(!(event.buttons&(imageDrag.button===0?1:4))){endImageDrag();return}imageX=imageDrag.x+event.clientX-imageDrag.startX;imageY=imageDrag.y+event.clientY-imageDrag.startY;paintSkuImage();event.preventDefault()});
  for(const name of ['pointerup','pointercancel','lostpointercapture'])imageStage.addEventListener(name,endImageDrag);
  imageStage.addEventListener('dragstart',event=>event.preventDefault());imageStage.addEventListener('auxclick',event=>{if(event.button===1)event.preventDefault()});
  window.addEventListener('blur',endImageDrag);new ResizeObserver(()=>{endImageDrag();layoutSkuImage()}).observe(imageStage);
  imageDialog.addEventListener('keydown',event=>{
    if(event.ctrlKey||event.metaKey||event.altKey)return;
    const actions={Escape:closeSkuImage,'+':()=>zoomSkuImage(imageScale*1.25),'=':()=>zoomSkuImage(imageScale*1.25),'-':()=>zoomSkuImage(imageScale/1.25),'0':fitSkuImage,Home:fitSkuImage};
    if(actions[event.key]){event.preventDefault();event.stopPropagation();actions[event.key]()}
  });
  document.addEventListener('framekeep:workspace',closeSkuImage);
  function openSkuImage(target){
    const row=(allView?overview.rows:state.rows).find(r=>r.id===target.dataset.completion),request=++imageRequest;imageOrigin=target;
    $('sku-image-title').textContent=row?`${row.sku} · ${localTime(row.completed_at)||'拍摄时间缺失'} · 标签照片`:target.dataset.skuName;
    skuImage.hidden=true;skuImage.removeAttribute('src');imageWidth=imageHeight=0;fitSkuImage();$('sku-image-status').textContent='正在加载图片…';
    skuImage.alt=row?`${row.sku} 标签照片`:`${target.dataset.skuName} 产品图`;
    skuImage.onload=()=>{if(request!==imageRequest||!imageDialog.open)return;skuImage.hidden=false;layoutSkuImage();$('sku-image-status').textContent=''};
    skuImage.onerror=()=>{if(request!==imageRequest||!imageDialog.open)return;skuImage.hidden=true;paintSkuImage();$('sku-image-status').textContent='图片无法加载，请检查文件是否在线；按 Esc 退出'};
    if(!imageDialog.open)imageDialog.showModal();$('sku-image-close').focus({preventScroll:true});
    skuImage.src=row?`/api/preview/${encodeURIComponent(row.completion.session_id)}/${encodeURIComponent(row.completion.capture_id)}?size=2048`:target.dataset.skuImage;
  }
  $('sku-rows').addEventListener('click',event=>{const documentButton=event.target.closest('[data-overview-document]');if(documentButton){action(async()=>{state=await api('/api/sku/activate',{project_id:documentButton.dataset.overviewDocument});allView=commonView=false;render();viewScope=documentButton.dataset.overviewSheet;render()});return}const cover=event.target.closest('[data-sku-cover]');if(cover){openSkuGallery(cover);return}const sheet=event.target.closest('[data-sku-sheet],[data-common-sheet]');if(sheet){const name=sheet.dataset.skuSheet||sheet.dataset.commonSheet;if(commonView||name!==state.project?.scope)action(()=>configure(name));return}const target=event.target.closest('[data-sku-image],[data-completion]');if(target&&!target.hasAttribute('data-sku-label'))openSkuImage(target)});
  $('sku-rows').addEventListener('keydown',event=>{const label=event.target.closest('[data-sku-label]');if(label&&['Enter',' '].includes(event.key)){event.preventDefault();openSkuImage(label)}});
  function render(){
    syncButtons();
    if(displayProject!==state.project?.id){displayProject=state.project?.id;commonView=false;viewScope=''}const p=viewedProject();bar.classList.toggle('hidden',!p);manualType.hidden=true;const common=commonGroups(),commonShot=common.filter(r=>r.shot).length,allGroups=overview.rows,allShot=allGroups.filter(r=>r.status==='已拍摄').length,count=allView?{total:allGroups.length,shot:allShot,pending:allGroups.length-allShot,rows:allGroups.length}:commonView?{total:common.length,shot:commonShot,pending:common.length-commonShot,rows:common.length}:state.progress?.find(s=>s.sheet===p?.scope),all=(state.progress||[]).reduce((sum,s)=>sum+s.shot,0);(button.querySelector('.workspace-tab-label')||button).textContent=p?`拍摄进度 · ${all} 已拍摄`:'拍摄进度';renderFeedback();
    $('shooting-document').textContent=state.shooting_project?`${state.shooting_project.name} · 当前拍摄：${state.shooting_context?.scope||state.shooting_project.scope}`:'自由拍摄 · 首张新照片自动建立当日文档';$('shooting-document').title=p&&!p.metadata.automatic_day?p.name:'';
    const context=JSON.stringify(FramekeepSku.context());if(context!==lastContext){lastContext=context;document.dispatchEvent(new CustomEvent('framekeep:sku-context'))}
    const project=JSON.stringify(FramekeepSku.shootingTypes());if(project!==lastProject){lastProject=project;document.dispatchEvent(new CustomEvent('framekeep:sku-project'))}
    options($('sku-projects'),[...state.projects.filter(p=>p.today).map(p=>[p.id,p.name+(state.projects.filter(other=>other.name===p.name).length>1?' · '+(p.automatic?'自动记录':'导入清单')+' '+p.id.slice(0,6):'')]),['__history__','历史拍摄…']],state.projects.some(item=>item.id===p?.id&&item.today)?p?.id:'','选择当日拍摄任务');
    projectField.hidden=allView;scopeRow.hidden=allView;overviewField.hidden=!allView;syncButton.hidden=allView;removeButton.hidden=allView;
    renderHistory();$('sku-remove-document').disabled=!p;$('sku-history-hint').textContent=p&&!state.projects.find(item=>item.id===p.id)?.today?'正在查看历史：'+p.name:'';
    const sheets=p?.metadata.sheets.filter(s=>!s.skipped)||[];
    const live=state.shooting_project||state.project,c=state.shooting_context;
    options($('sku-live-scope'),(live?.metadata.sheets||[]).filter(s=>!s.skipped).map(s=>[s.name,s.name]),c?.scope||live?.scope,'请选择或输入拍摄类型');
    options($('sku-panel-scope'),sheets.map(s=>[s.name,s.name]),state.project?.scope,'先选择当前拍摄类型');
    const bound=!!p&&!!window.FRAMEKEEP_SESSION.id&&p.session_id===window.FRAMEKEEP_SESSION.id;
    updateTypeAction($('sku-live-scope'));updateTypeAction($('sku-panel-scope'));
    const scoped=(state.shooting_rows||state.rows).filter(r=>r.sheet===(state.shooting_context?.scope||p?.scope)),pending=state.jobs.filter(j=>['review','failed','waiting_model'].includes(j.status)).length;
    const scopeCount=state.progress?.find(s=>s.sheet===p?.scope);
    $('sku-live-count').textContent=p?.scope?`${scopeCount?.shot||0} / ${scopeCount?.total||0} 已拍摄${pending?' · '+pending+' 条待处理':''}${bound?'':' · 当前目录未绑定'}`:'请选择拍摄工作表后再标记标签';
    const grouped=new Map();
    for(const row of scoped){const key=row.normalized||row.sku,prior=grouped.get(key);if(prior){prior.shot ||= row.status==='已拍摄';prior.sku_image ||= row.sku_image}else grouped.set(key,{sku:row.sku,shot:row.status==='已拍摄',sku_image:row.sku_image})}
    currentViews.hidden=!p;$('shooting-common-view').querySelector('span').textContent=common.length;
    for(const id of ['shooting-scope-view','shooting-common-view']){const selected=(id==='shooting-common-view')===commonView;$(id).setAttribute('aria-selected',String(selected));$(id).tabIndex=selected?0:-1}
    currentTable.setAttribute('aria-labelledby',commonView?'shooting-common-view':'shooting-scope-view');
    const currentRows=commonView?common:[...grouped.values()],empty=!p?'尚未导入拍摄清单，可点击上方导入表格。':commonView?'没有跨工作表重复的公共款号。':!p.scope?'请选择当前拍摄工作表。':'当前工作表没有款号。';
    const currentHtml='<table><thead><tr><th scope="col">序号</th><th scope="col">款号</th><th scope="col">'+(commonView?'各表状态':'拍摄状态')+'</th><th>款号图片</th></tr></thead><tbody>'+(currentRows.length?currentRows.map((r,index)=>`<tr><td>${index+1}</td><td>${esc(r.sku)}</td><td>${commonView?commonMemberships(r):`<span class="sku-state ${r.shot?'sku-shot':''}">${r.shot?'已拍摄':'未拍摄'}</span>`}</td><td class="sku-product-images">${reconciliationImage(r)}</td></tr>`).join(''):`<tr><td colspan="4" class="current-shooting-empty">${esc(empty)}</td></tr>`)+ '</tbody></table>';
    currentTable.classList.toggle('is-common',commonView);
    if(currentTable.dataset.content!==currentHtml){currentTable.dataset.content=currentHtml;currentTable.innerHTML=currentHtml}
    $('sku-export').disabled=!p||allView;$('sku-export').textContent=window.FramekeepShootingExport?.running()?'查看导出进度':'导出拍摄记录';
    if(dialog.classList.contains('hidden'))return;
    $('sku-project-info').textContent=p?`${p.name} · 正在查看：${p.scope||'尚未选择'} · 当前拍摄：${state.shooting_context?.scope||'未绑定'} · ${bound?'已绑定当前照片目录':'需绑定当前照片目录'}。源表保持原样，拍摄记录实时保存；导出时生成图片文档。`:'先导入本次拍摄表格，各页款号分别对账。';
    if(allView)$('sku-project-info').textContent=($('sku-display-range').value==='today'?'当日全部':'历史全部拍摄')+' · '+overview.documents.length+' 份文档；查看汇总不改变当前拍摄绑定。导出请先打开来源文档。';
    if(!p&&!readError.hidden)$('sku-project-info').textContent='拍摄文档尚未读取成功。';
    if(p?.export_error)message(p.export_error);
    dialog.querySelector('.progress-note').textContent=allView?'汇总所选范围的全部文档和拍摄类型，按日期分隔；每项保留来源文档、拍摄状态与完成证据。':commonView?'公共款号在多个工作表中重复出现，按款号去重，各表状态分别保留；全部所属工作表均已拍摄才计为完成。查看公共不改变当前 OCR 工作表。':'序号按当前列表从1连续排列，原表行号单独保留。完成时间取自标签照片拍摄信息，双击标签图片可查看大图，也可点击时间查证；缺失拍摄时间时不使用文件时间代替。';
    const folder=state.folder_progress;if(folder?.session_id===window.FRAMEKEEP_SESSION.id){const line=document.createElement('strong');line.className='progress-folder-summary';line.textContent=`当前目录：二星标签 ${folder.two_star} 张 · 已确认 ${folder.confirmed_labels} 张 / ${folder.confirmed_skus} 款 · 待确认 ${folder.pending_labels} 张`;dialog.querySelector('.progress-note').prepend(line)}
    const summary=count?`<div><span>${allView?'总任务数':'总款数'}</span><strong id="progress-total">${count.total}</strong></div><div><span>已拍摄</span><strong id="progress-shot">${count.shot}</strong></div><div><span>未拍摄</span><strong id="progress-pending">${count.pending}</strong></div><div class="progress-meter"><span>${count.total?Math.round(count.shot/count.total*100):0}% 完成</span><progress aria-label="当前工作表完成进度" max="${count.total||1}" value="${count.shot}"></progress>${count.rows!==count.total?'<small>按款号去重统计，原表行全部保留</small>':''}</div>`:'<p class="progress-empty">选择工作表后显示本页拍摄进度</p>';if($('sku-progress-summary').innerHTML!==summary)$('sku-progress-summary').innerHTML=summary;
    renderRows();
    const sheetKey=p?.id+'|'+p?.scope+'|'+(allView?'all':commonView?'common':'scope');
    if(sheetKey!==lastSheet){lastSheet=sheetKey;const panel=$('sku-sheet-panel');panel.getAnimations().forEach(a=>a.cancel());if(!matchMedia('(prefers-reduced-motion: reduce)').matches)panel.animate([{opacity:.4,translate:'0 4px'},{opacity:1,translate:'none'}],{duration:200,easing:getComputedStyle(document.documentElement).getPropertyValue('--motion-ease').trim()||'cubic-bezier(.22,1,.36,1)'})}
    const signature=JSON.stringify(state.jobs);
    if(signature===lastJobs||document.activeElement?.matches('[data-sku-target]'))return;
    lastJobs=signature;
    $('sku-jobs').innerHTML=state.jobs.length?state.jobs.map(j=>{
      const candidates=j.result.candidates||[],choices=state.rows.filter(r=>r.sheet===j.scope||r.sheet===state.project?.metadata?.managed_sheet).sort((a,b)=>(candidates.findIndex(c=>c.row_id===a.id)+1||999)-(candidates.findIndex(c=>c.row_id===b.id)+1||999));
      const raw=(j.result.readings||[]).map(r=>`${r.model}：${r.text}${r.codes?.length?'\n读取款号：'+r.codes.join('、'):''}${r.unscored?'\n模型未提供置信度，需人工核对。':''}`).join('\n');
      const title=j.trigger==='full_auto'&&j.status==='queued'?'等待逐图扫描':status[j.status]||esc(j.status);
      return `<article class="sku-job" data-job="${j.id}" data-status="${j.status}"><img src="/api/preview/${encodeURIComponent(j.session_id)}/${encodeURIComponent(j.capture_id)}?size=480" alt="识别照片" loading="lazy"><div><strong>${title} · ${esc(j.scope)}</strong><p>${esc(j.reason)}</p>${raw?`<details><summary>识别原文与模型</summary><pre>${esc(raw)}</pre></details>`:''}${candidates.length?`<p class="sku-candidates">候选：${candidates.slice(0,4).map(c=>`${esc(c.sku)}（相似度 ${Math.round(c.score*100)}%，${esc(c.sheet)} 第 ${c.row} 行）`).join('；')}</p>`:''}<div class="sku-review-actions"><button data-sku-edit-capture="${esc(j.capture_id)}" data-sku-edit-session="${esc(j.session_id)}">核对 / 填写款号</button><select data-sku-target aria-label="确认款号"><option value="">选择本页准确款号</option>${choices.map(r=>`<option value="${r.id}">${esc(r.sku)} · 第 ${r.excel_row} 行</option>`).join('')}</select><button data-sku-action="confirm">确认</button><button data-sku-action="retry">重试</button><button data-sku-action="ignore">${['matched','confirmed'].includes(j.status)?'撤销识别':'忽略'}</button></div></div></article>`
    }).join(''):'<p class="dialog-copy">尚无识别记录。拍摄款号标签后，用遥控鼠标标成二星并保持。</p>';
  }
  let loadRequest=0,loadController=null;
  async function load(){
    // abort只能停止客户端接收，不能取消已进入后端的区间计算；合并同类刷新。
    if(loadController)return;
    const controller=new AbortController();loadController=controller;const request=++loadRequest,version=mutation;
    try{
      const next=await api('/api/sku',undefined,20000,controller.signal);
      if(request===loadRequest&&version===mutation&&!busy){state=next;readError.hidden=true;readError.textContent='';render();if(allView&&!overviewLoading&&Date.now()-overviewLoadedAt>=(overview.rows.some(r=>['queued','writing'].includes(r.metadata?.status))?1000:5000))await loadOverview()}
    }catch(error){
      if(!controller.signal.aborted){
        if(request===loadRequest&&version===mutation){readError.textContent='拍摄记录读取失败，正在自动重试；当前显示可能尚未更新，请勿据此判断记录已丢失。'+error.message;readError.hidden=false;render()}
        throw error;
      }
    }finally{if(loadController===controller)loadController=null}
  }
  async function exclusive(fn){if(busy)throw new Error('正在处理拍摄任务，请稍后重试');if(window.FramekeepAutomation?.isSaving())throw new Error('正在切换全自动 OCR，请稍后重试');busy=true;mutation++;syncButtons();window.FramekeepAutomation?.refresh();try{return await fn()}finally{busy=false;syncButtons();window.FramekeepAutomation?.refresh()}}
  async function action(fn){if(busy)return;try{await exclusive(fn)}catch(e){message(e.message);Framekeep.notify(e.message)}}
  async function importPlan(file,bindCurrent=false){
    if(!file)return;
    if(!file.name.toLowerCase().endsWith('.xlsx'))throw new Error('请选择包含款号的 .xlsx 拍摄计划表格');
    if(file.size>20_000_000)throw new Error('Excel 需要小于 20MB');
    if(window.FramekeepAutomation?.isSaving())throw new Error('正在切换全自动 OCR，请稍后导入');
    return exclusive(async()=>{
      $('sku-import').disabled=$('shooting-import').disabled=true;
      try{
        const previousScope=state.project?.scope||$('shooting-manual-type').value.trim(),sessionId=bindCurrent?window.FRAMEKEEP_SESSION.id:null;
        await FramekeepIO.flush();const bytes=new Uint8Array(await file.arrayBuffer());let binary='';for(let i=0;i<bytes.length;i+=32768)binary+=String.fromCharCode(...bytes.subarray(i,i+32768));
        state=await api('/api/sku/import',{name:file.name,content_base64:btoa(binary),session_id:sessionId,preferred_scope:previousScope},90000);
        $('sku-search').value='';$('sku-filter').value='all';lastJobs='';render();
        const bound=!!sessionId&&state.project.session_id===window.FRAMEKEEP_SESSION.id,reused=!!state.already_imported;message(reused?'该文件已导入，已复用原拍摄任务并保留已有进度。':bound?'已导入并绑定当前目录，可直接开启全自动 OCR。':'已导入拍摄计划，可查看各工作表；打开照片目录后可绑定拍摄。');return {...FramekeepSku.shootingTypes(),bound,reused};
      }finally{$('sku-import').disabled=$('shooting-import').disabled=false}
    });
  }
  async function configure(scope,bind=false,enabled=state.project?.enabled){
    const p=state.project;if(!p)throw new Error('首张新照片接入后将自动建立拍摄文档，也可以导入 Excel');
    if(!scope)throw new Error('请选择当前拍摄类型／工作表');
    if(bind&&!window.FRAMEKEEP_SESSION.id)throw new Error('请先打开照片目录，再绑定拍摄计划');
    await FramekeepIO.flush();
    state=await api('/api/sku/configure',{project_id:p.id,session_id:bind?window.FRAMEKEEP_SESSION.id:p.session_id||window.FRAMEKEEP_SESSION.id,scope,enabled});commonView=false;allView=false;viewScope=scope;render();message('当前拍摄：'+scope+'。新标签只更新这一页。');
  }
  let firstPoll=true;
  async function poll(){let restoringDatabase=false;if(firstPoll){firstPoll=false;try{restoringDatabase=localStorage.getItem('framekeep.last-workspace')==='sku-database'}catch{}}if(!restoringDatabase&&!busy&&!rowEditing&&!document.hidden&&['selection','progress'].includes(currentPage))try{await load()}catch{}clearTimeout(timer);timer=setTimeout(poll,1000)}
  const pages=new Map();let currentPage='selection';
  function showWorkspace(name){
    if(!pages.has(name))return;
    currentPage=name;
    for(const [key,item] of pages){const active=key===name;item.panel.classList.toggle('hidden',!active);item.panel.toggleAttribute('open',active);if(item.tab.getAttribute('role')==='tab'){item.tab.setAttribute('aria-selected',String(active));item.tab.tabIndex=active?0:-1}else item.tab.setAttribute('aria-pressed',String(active))}
    pages.get(name).onShow?.();
    document.dispatchEvent(new CustomEvent('framekeep:workspace',{detail:{page:name}}));
    window.dispatchEvent(new Event('resize'));
  }
  function register(name,tab,panel,onShow){pages.set(name,{tab,panel,onShow});tab.onclick=()=>showWorkspace(name);}
  window.FramekeepWorkspace={show:showWorkspace,current:()=>currentPage,register,refresh:()=>pages.get(currentPage)?.onShow?.()};
  register('selection',$('selection-tab'),$('selection-page'));
  register('progress',button,dialog,()=>{lastJobs='';render();load().catch(e=>message(e.message))});
  function open(){showWorkspace('progress')}
  function openRecords(){open();const toggle=$('progress-sidebar-toggle');if(toggle?.getAttribute('aria-expanded')==='false')toggle.click();$('sku-records').open=true;$('sku-records').scrollIntoView({block:'nearest'})}
  $('sku-review-open').addEventListener('click',openRecords);$('sku-close').addEventListener('click',()=>showWorkspace('selection'));
  tabs.addEventListener('keydown',e=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){if(currentPage!=='selection')e.stopPropagation();return}e.stopPropagation();
    e.preventDefault();const names=[...tabs.querySelectorAll('[role=tab]')].map(tab=>[...pages].find(([,item])=>item.tab===tab)?.[0]).filter(Boolean),index=names.indexOf(currentPage);
    const next=names[e.key==='Home'?0:e.key==='End'?names.length-1:(index+(e.key==='ArrowRight'?1:-1)+names.length)%names.length];
    showWorkspace(next);pages.get(next).tab.focus();
  });
  document.addEventListener('keydown',e=>{if(currentPage!=='selection'&&!document.querySelector('dialog[open]')&&!/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)&&!e.target.isContentEditable&&!tabs.contains(e.target)&&!$('sku-sheet-tabs').contains(e.target)&&!e.target.closest('.view-menu,.app-settings-nav'))e.stopPropagation()},true);

  $('sku-sheet-tabs').addEventListener('click',e=>{if(e.target.closest('[data-all-view]')){allView=true;commonView=false;loadOverview();render();return}if(e.target.closest('[data-common-view]')){allView=false;commonView=true;render();return}const target=e.target.closest('[data-progress-sheet]');if(target){viewScope=target.dataset.progressSheet;allView=false;commonView=false;render()}});
  currentViews.addEventListener('click',e=>{const target=e.target.closest('[data-current-view]');if(target){allView=false;commonView=target.dataset.currentView==='common';render()}});
  currentViews.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.stopPropagation();e.preventDefault();commonView=e.key==='Home'?false:e.key==='End'?true:!commonView;render();$(commonView?'shooting-common-view':'shooting-scope-view').focus()});
  currentTable.addEventListener('click',e=>{const cover=e.target.closest('[data-sku-cover]');if(cover){openSkuGallery(cover);return}const target=e.target.closest('[data-common-sheet]');if(target){viewScope=target.dataset.commonSheet;allView=false;commonView=false;render()}else {const image=e.target.closest('[data-sku-image]');if(image)openSkuImage(image)}});
  $('sku-sheet-tabs').addEventListener('keydown',e=>{e.stopPropagation();if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const buttons=[...$('sku-sheet-tabs').querySelectorAll('[role=tab]')],index=buttons.indexOf(e.target),next=buttons[e.key==='Home'?0:e.key==='End'?buttons.length-1:(index+(e.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length];if(!next)return;buttons.forEach(b=>b.tabIndex=b===next?0:-1);next.focus()});

  $('sku-bind-current').addEventListener('click',()=>action(()=>commitType($('sku-live-scope'))));
  $('sku-projects').addEventListener('change',e=>{if(e.target.value==='__history__'){e.target.value='';openHistory();return}if(e.target.value)action(async()=>{await FramekeepIO.flush();state=await api('/api/sku/activate',{project_id:e.target.value});viewScope='';lastJobs='';render()})});
  $('sku-import').addEventListener('click',()=>$('sku-file').click());
  async function syncProgress(){
    const project=state.project;if(!project)throw new Error('请先导入拍摄文档');
    syncingProgress=true;syncButtons();syncStatus.textContent='正在同步已有拍摄进度…';
    try{
      await FramekeepIO.flush();const watch=await api('/api/watch');
      const sid=watch.enabled?watch.session_id:window.FRAMEKEEP_SESSION.id;
      if(!sid)throw new Error('请先打开或监控拍摄文件夹');
      const next=await api('/api/sku/sync-progress',{project_id:project.id,session_id:sid,watch_generation:watch.enabled?watch.generation:null},90000);
      state=next;lastJobs='';render();const report=next.progress_sync;
      const text=`已同步：文档匹配 ${report.matched_skus} 款，临时插拍 ${report.unplanned_skus} 款；${report.restored_rows?'补齐 '+report.restored_rows+' 项进度':'已有进度已核对'}。${report.pending_labels?'另有 '+report.pending_labels+' 张标签待确认，未计入进度。':''}${report.workbook_updated?'工作副本已更新。':'进度与图片引用已保存，导出文档时再压缩嵌入图片。'}`;
      syncStatus.textContent=text;syncStatus.title=report.folder;message(text);Framekeep.notify(text);
    }catch(error){syncStatus.textContent=error.message;throw error}finally{syncingProgress=false;syncButtons()}
  }
  for(const b of [syncButton,quickSync])b.addEventListener('click',()=>action(syncProgress));
  $('shooting-import').addEventListener('click',()=>$('shooting-plan-file').click());
  $('shooting-plan-file').addEventListener('change',async event=>{try{if(event.target.files[0]){const result=await importPlan(event.target.files[0],true);Framekeep.notify(result.reused?'该文件已导入，已复用原任务并保留拍摄进度':result.bound?'表格已导入，可在右栏开启全自动 OCR':'表格已导入，请选择工作表并绑定照片目录');window.FramekeepSidebar?.show('shooting')}}catch(error){Framekeep.notify(error.message);$('shooting-auto-status').textContent=error.message}finally{event.target.value=''}});
  $('sku-file').addEventListener('change',async e=>{try{await importPlan(e.target.files[0],true)}catch(error){message(error.message);Framekeep.notify(error.message)}finally{e.target.value=''}});
  $('sku-export').addEventListener('click',async()=>{
    if(!state.project){message('请先导入拍摄清单');return}
    try{await FramekeepShootingExport.start(state.project,()=>FramekeepIO.flush())}catch(error){message(error.message);Framekeep.notify(error.message)}
  });
  $('sku-jobs').addEventListener('click',e=>{const edit=e.target.closest('[data-sku-edit-capture]');if(edit){window.FramekeepOcrDetails?.open(edit.dataset.skuEditSession,edit.dataset.skuEditCapture).catch(error=>message(error.message));return}const b=e.target.closest('[data-sku-action]');if(!b)return;const card=b.closest('[data-job]');action(async()=>{const row=card.querySelector('[data-sku-target]').value;if(b.dataset.skuAction==='confirm'&&!row)throw new Error('请先选择本工作表中的准确款号');state=await api('/api/sku/review',{job_id:card.dataset.job,action:b.dataset.skuAction,row_id:row});lastJobs='';render()})});
  $('sku-search').addEventListener('input',renderRows);$('sku-filter').addEventListener('change',renderRows);
  document.addEventListener('framekeep:selection',render);
  document.addEventListener('framekeep:export-state',render);
  for(const event of ['framekeep:ratings-saved','framekeep:ocr-choice'])document.addEventListener(event,()=>{mutation++;if(['selection','progress'].includes(currentPage))load().catch(()=>{})});
  document.addEventListener('framekeep:workspace',()=>{if(!['selection','progress'].includes(currentPage))loadController?.abort();else if(!busy)load().catch(()=>{})});
  poll();
})();
