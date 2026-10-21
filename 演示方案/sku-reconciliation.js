(() => {
  'use strict';
  if(!window.FramekeepIO)return;
  const $=id=>document.getElementById(id),api=FramekeepIO.api,esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const tabs=document.createElement('nav');tabs.className='workspace-tabs';tabs.setAttribute('role','tablist');tabs.setAttribute('aria-label','工作页面');tabs.innerHTML='<button id="selection-tab" role="tab" aria-selected="true" aria-controls="selection-page">选片</button><button id="sku-open" role="tab" aria-selected="false" aria-controls="sku-dialog" tabindex="-1">拍摄进度</button>';document.querySelector('.app-header').after(tabs);
  const button=$('sku-open');
  const bar=document.createElement('div');bar.id='sku-live-bar';bar.className='sku-live-bar hidden';bar.innerHTML='<strong>当前拍摄</strong><select id="sku-live-scope" aria-label="当前拍摄类型或工作表"></select><button id="sku-bind-current" class="bordered">绑定当前照片目录</button><span id="sku-live-count"></span><button id="sku-review-open">查看识别记录</button>';
  const currentPanel=document.createElement('section');currentPanel.id='current-shooting-panel';currentPanel.className='selection-sidebar-page hidden';
  const currentTable=document.createElement('div');currentTable.id='sku-current-table';currentTable.className='sku-current-table';currentTable.setAttribute('aria-live','polite');
  const currentViews=document.createElement('nav');currentViews.id='shooting-list-views';currentViews.className='shooting-list-views';currentViews.setAttribute('aria-label','拍摄款号分类');currentViews.setAttribute('role','tablist');currentViews.innerHTML='<button id="shooting-scope-view" role="tab" aria-controls="sku-current-table" data-current-view="scope">当前工作表</button><button id="shooting-common-view" role="tab" aria-controls="sku-current-table" data-current-view="common">公共 <span>0</span></button>';
  currentTable.setAttribute('role','tabpanel');
  const manualType=document.createElement('label');manualType.id='shooting-manual-field';manualType.className='shooting-manual-field';manualType.innerHTML='当前拍摄类型<input id="shooting-manual-type" maxlength="100" placeholder="未导入清单时填写类型">';
  const automation=document.createElement('section');automation.id='shooting-automation';automation.className='shooting-automation';automation.setAttribute('aria-label','拍摄文档与自动识别');
  automation.innerHTML='<div class="shooting-import-line"><strong>拍摄文档</strong><button id="shooting-import" class="bordered" type="button">导入表格</button><input id="shooting-plan-file" type="file" accept=".xlsx" hidden></div><p id="shooting-document" class="shooting-document">尚未导入表格文档</p><label class="shooting-auto-line"><span>全自动 OCR</span><input id="shooting-full-auto" class="toggle-switch" type="checkbox" role="switch" aria-label="右栏全自动 OCR" aria-describedby="shooting-auto-status" disabled></label><p id="shooting-auto-status" class="shooting-auto-status" role="status">导入文档并绑定当前目录后可开启</p>';
  try{manualType.querySelector('input').value=localStorage.getItem('framekeep-manual-shooting-type-v1')||''}catch{}
  manualType.querySelector('input').addEventListener('input',()=>{try{localStorage.setItem('framekeep-manual-shooting-type-v1',$('shooting-manual-type').value)}catch{}document.dispatchEvent(new CustomEvent('framekeep:shooting-type'))});
  currentPanel.append(automation,bar,manualType,currentViews,currentTable);document.querySelector('.selection-sidebar-footer').before(currentPanel);
  const dialog=document.createElement('section');dialog.id='sku-dialog';dialog.className='shooting-progress hidden';dialog.setAttribute('role','tabpanel');dialog.setAttribute('aria-labelledby','sku-open');
  dialog.innerHTML='<div class="progress-heading"><div><h2 id="sku-title">拍摄进度</h2><p class="dialog-copy">二星款号标签确认后自动记账，已拍摄款置顶；可导出全部工作表的当前拍摄记录。</p></div><div class="progress-heading-actions"><button id="sku-export" class="primary" title="导出全部工作表，包含当前已确认的拍摄状态与完成时间">导出拍摄记录</button><button id="sku-close" class="bordered">返回选片</button></div></div><div class="sku-toolbar"><button id="sku-import" class="primary">导入 Excel 建立任务</button><input id="sku-file" type="file" accept=".xlsx" hidden><select id="sku-projects" aria-label="拍摄对账任务"></select></div><div id="sku-project-info" class="dialog-copy"></div><nav id="sku-sheet-tabs" class="sku-sheet-tabs" role="tablist" aria-label="拍摄工作表"></nav><section id="sku-sheet-panel" role="tabpanel" tabindex="0" aria-label="当前拍摄工作表"><div id="sku-progress-summary" class="progress-summary" aria-live="polite"></div><div class="sku-toolbar"><label class="watch-option"><input id="sku-auto" type="checkbox"> 二星标签自动识别</label><button id="sku-recognize" class="bordered">识别当前二星标签</button><input id="sku-search" placeholder="搜索款号"><select id="sku-filter"><option value="all">全部款号</option><option value="pending">未拍摄</option><option value="shot">已拍摄</option></select></div><p id="sku-message" class="dialog-copy" role="status"></p><p class="progress-note">序号按当前列表从1连续排列，原表行号单独保留。完成时间取自标签照片拍摄信息，点击时间可查证标签；缺失拍摄时间时不使用文件时间代替。</p><div id="sku-rows" class="sku-table-wrap"></div></section><details id="sku-records"><summary>识别记录与待确认</summary><div id="sku-jobs"></div></details>';
  document.querySelector('.workbench > .statusbar').before(dialog);
  $('sku-auto').classList.add('toggle-switch');$('sku-auto').setAttribute('role','switch');
  $('sku-auto').closest('label').before($('sku-recognize'));
  dialog.prepend($('sku-sheet-tabs'));
  const taskToolbar=$('sku-projects').closest('.sku-toolbar');taskToolbar.classList.add('progress-task-fields');
  const projectField=document.createElement('label');projectField.className='progress-task-project';projectField.textContent='拍摄任务';$('sku-projects').before(projectField);projectField.append($('sku-projects'));
  const scopeRow=document.createElement('label');scopeRow.className='sku-scope';scopeRow.innerHTML='当前工作表 <select id="sku-panel-scope" aria-label="对账面板拍摄工作表"></select>';taskToolbar.append(scopeRow);
  taskToolbar.append(projectField,scopeRow,$('sku-import'),$('sku-file'));
  const recognitionToolbar=$('sku-recognize').parentElement;recognitionToolbar.classList.add('progress-tools');
  const recognition=document.createElement('div');recognition.className='progress-recognition';recognition.setAttribute('role','group');recognition.setAttribute('aria-label','标签识别');recognitionToolbar.prepend(recognition);recognition.append($('sku-recognize'),$('sku-auto').closest('label'));
  const filters=document.createElement('div');filters.className='progress-filters';filters.setAttribute('role','search');filters.setAttribute('aria-label','筛选款号');recognitionToolbar.append(filters);filters.append($('sku-search'),$('sku-filter'));$('sku-search').setAttribute('aria-label','搜索款号');$('sku-filter').setAttribute('aria-label','拍摄状态筛选');
  let state={project:null,projects:[],rows:[],jobs:[]},busy=false,timer=null,lastJobs='',lastContext='',lastProject='',lastSheet='',mutation=0,exporting=false,commonView=false,displayProject=null;
  window.FramekeepSku={context:()=>state.project?.scope&&state.project.enabled&&state.project.session_id===window.FRAMEKEEP_SESSION.id?{project_id:state.project.id,scope:state.project.scope}:null,shootingTypes:()=>state.project?{project_id:state.project.id,name:state.project.name,scope:state.project.scope,sheets:state.project.metadata.sheets.filter(s=>!s.skipped).map(s=>s.name),counts:(state.progress||[]).map(s=>({sheet:s.sheet,total:s.total}))}:null,isBusy:()=>busy,importPlan,bindCurrent:(scope,projectId)=>exclusive(async()=>{if(state.project?.id!==projectId)throw new Error('拍摄计划已变化，请重新选择');await configure(scope,true)})};
  FramekeepSku.currentType=()=>state.project?state.project.scope||'':$('shooting-manual-type').value.trim();
  const status={queued:'等待二星稳定',waiting_model:'待配置模型',running:'识别中',matched:'已自动匹配',confirmed:'人工确认',review:'待确认',failed:'识别失败',cancelled:'已撤销',ignored:'已忽略'};
  function message(text){$('sku-message').textContent=text}
  function options(select,items,value,placeholder){const next=(placeholder?`<option value="">${placeholder}</option>`:'')+items.map(([id,label])=>`<option value="${esc(id)}">${esc(label)}</option>`).join('');if(select.innerHTML!==next)select.innerHTML=next;select.value=value||''}
  function commonGroups(){
    const groups=new Map(),sheets=new Set(state.project?.metadata.sheets.filter(s=>!s.skipped).map(s=>s.name)||[]);
    for(const row of state.rows){
      if(!sheets.has(row.sheet))continue;const key=row.normalized||row.sku;
      if(!groups.has(key))groups.set(key,{sku:row.sku,sheets:new Map()});const group=groups.get(key);
      group.sheets.set(row.sheet,group.sheets.get(row.sheet)||row.status==='已拍摄');
    }
    return [...groups.values()].filter(group=>group.sheets.size>1).map(group=>({...group,shot:[...group.sheets.values()].every(Boolean)}));
  }
  function commonMemberships(group){return `<span class="sku-common-memberships">${[...group.sheets].map(([sheet,shot])=>`<button type="button" data-common-sheet="${esc(sheet)}" title="查看 ${esc(sheet)} 中的 ${esc(group.sku)}"><span>${esc(sheet)}</span><span class="sku-state ${shot?'sku-shot':''}">${shot?'已拍摄':'未拍摄'}</span></button>`).join('')}</span>`}
  function renderSheetTabs(matches){
    const p=state.project,sheetTabs=$('sku-sheet-tabs'),focused=sheetTabs.contains(document.activeElement)?document.activeElement.id:null,progress=state.progress||[],stop=focused||(commonView?'sku-common-tab':`sku-sheet-tab-${Math.max(0,progress.findIndex(s=>s.sheet===p?.scope))}`);
    const sheetButtons=progress.map((s,index)=>`<button id="sku-sheet-tab-${index}" role="tab" aria-controls="sku-sheet-panel" data-progress-sheet="${esc(s.sheet)}" aria-selected="${!commonView&&s.sheet===p?.scope}" aria-pressed="${!commonView&&s.sheet===p?.scope}" tabindex="${'sku-sheet-tab-'+index===stop?0:-1}" title="${esc(s.sheet)}：${matches?'匹配款号':'已拍摄'} ${matches?matches.get(s.sheet)?.size||0:s.shot} / ${s.total}">${esc(s.sheet)} <span>${matches?matches.get(s.sheet)?.size||0:s.shot}/${s.total}</span></button>`).join('')+(p?`<button id="sku-common-tab" role="tab" aria-controls="sku-sheet-panel" data-common-view aria-selected="${commonView}" aria-pressed="${commonView}" tabindex="${stop==='sku-common-tab'?0:-1}">公共 <span>${commonGroups().length}</span></button>`:'');
    if(sheetTabs.innerHTML!==sheetButtons){sheetTabs.innerHTML=sheetButtons;if(focused)$(focused)?.focus({preventScroll:true})}
    const activeTab=commonView?$('sku-common-tab'):[...sheetTabs.children].find(b=>b.dataset.progressSheet===p?.scope);if(activeTab)$('sku-sheet-panel').setAttribute('aria-labelledby',activeTab.id);else $('sku-sheet-panel').removeAttribute('aria-labelledby');
  }
  function renderRows(){
    const query=$('sku-search').value.trim().toUpperCase(),filter=$('sku-filter').value,p=state.project;
    const sheet=p?.metadata.sheets.find(s=>s.name===p.scope),columns=sheet?.columns||[];
    const memberships=new Map(),sheetNames=(p?.metadata.sheets||[]).filter(s=>!s.skipped).map(s=>s.name);
    if(query)for(const row of state.rows){const key=row.normalized||row.sku;if(!memberships.has(key))memberships.set(key,new Set());memberships.get(key).add(row.sheet)}
    const matchedRows=state.rows.filter(r=>(!query||r.sku.toUpperCase().includes(query))&&(filter==='all'||(filter==='shot')===(r.status==='已拍摄')));
    const matches=query?new Map(sheetNames.map(name=>[name,new Set()])):null;
    if(matches)for(const row of matchedRows)matches.get(row.sheet)?.add(row.normalized||row.sku);
    const searchSummary=$('sku-search-summary');if(searchSummary){searchSummary.hidden=!query;searchSummary.textContent=query?'款号搜索结果：'+sheetNames.map(name=>`${name} ${matches.get(name).size}张`).join('，'):''}
    renderSheetTabs(matches);
    if(commonView){
      const groups=commonGroups().filter(r=>(!query||r.sku.toUpperCase().includes(query))&&(filter==='all'||(filter==='shot')===r.shot));
      const html=groups.length?'<table><thead><tr><th scope="col" class="sku-sequence">序号</th><th scope="col">款号</th><th scope="col">所属工作表与拍摄状态</th></tr></thead><tbody>'+groups.map((r,index)=>`<tr class="${r.shot?'progress-shot':''}"><td class="sku-sequence">${index+1}</td><td><strong>${esc(r.sku)}</strong></td><td>${commonMemberships(r)}</td></tr>`).join('')+'</tbody></table>':'<p class="dialog-copy">没有符合条件的公共款号。仅汇总出现在多个工作表中的款号。</p>';
      if($('sku-rows').innerHTML!==html)$('sku-rows').innerHTML=html;return;
    }
    const rows=matchedRows.filter(r=>r.sheet===p?.scope).sort((a,b)=>(b.status==='已拍摄')-(a.status==='已拍摄')||String(b.completed_at||'').localeCompare(String(a.completed_at||''))||a.excel_row-b.excel_row);
    function images(r,list){return list.map(im=>`<button class="sku-product-image" data-sku-image="${esc(im.url)}" data-sku-name="${esc(r.sku)}" title="查看 ${esc(r.sku)} 产品图"><img src="${esc(im.url)}" alt="${esc(r.sku)} 产品图" loading="lazy"></button>`).join('')}
    function skuText(r){const sheets=memberships.get(r.normalized||r.sku);return `<span class="sku-code">${query?`<span class="sku-sheet-buttons" role="group" aria-label="${esc(r.sku)} 所属工作表">${sheetNames.filter(name=>sheets?.has(name)).map(name=>`<button class="sku-sheet-badge" data-sku-sheet="${esc(name)}" aria-pressed="${name===r.sheet}" title="查看 ${esc(name)} 工作表中的 ${esc(r.sku)}">${esc(name)}</button>`).join('')}</span>`:''}<strong>${esc(r.sku)}</strong></span>`}
    const html=rows.length?'<table><thead><tr><th scope="col" class="sku-sequence">序号</th><th scope="col">原表行</th>'+columns.map(c=>`<th>${esc(c.title)}</th>`).join('')+'<th>拍摄状态</th><th>拍摄完成时间</th></tr></thead><tbody>'+rows.map((r,index)=>`<tr data-sku-row="${esc(r.id)}" class="${r.status==='已拍摄'?'progress-shot':''}"><td class="sku-sequence" aria-label="序号 ${index+1}">${index+1}</td><td class="progress-row-number">${r.excel_row}</td>${columns.map(c=>{const imgs=(r.images||[]).filter(im=>im.cell.replace(/\d/g,'')===c.letter);return `<td class="${imgs.length?'sku-product-images':''}">${imgs.length?images(r,imgs):c.index===sheet.sku_col?skuText(r):esc(r.source_values?.[c.letter]||'')}</td>`}).join('')}<td><span class="sku-state ${r.status==='已拍摄'?'sku-shot':''}">${esc(r.status)}</span></td><td class="progress-time">${r.completed_at?`${r.completion?`<button data-completion="${esc(r.id)}">${esc(r.completed_at.replace('T',' '))}</button>`:esc(r.completed_at.replace('T',' '))}`:r.status==='已拍摄'?'<span class="time-missing">拍摄时间缺失</span>':'—'}</td></tr>`).join('')+'</tbody></table>':`<p class="dialog-copy">${p?.scope?'没有符合条件的款号':'请选择当前拍摄工作表，各页分别统计进度。'}</p>`;
    if($('sku-rows').innerHTML!==html)$('sku-rows').innerHTML=html;
  }
  const imageDialog=document.createElement('dialog');imageDialog.className='sku-image-dialog';imageDialog.innerHTML='<div class="dialog-heading"><h2></h2><button aria-label="关闭产品图">×</button></div><img alt="产品图">';document.body.append(imageDialog);
  imageDialog.querySelector('button').onclick=()=>imageDialog.close();
  $('sku-rows').addEventListener('click',event=>{const sheet=event.target.closest('[data-sku-sheet],[data-common-sheet]');if(sheet){const name=sheet.dataset.skuSheet||sheet.dataset.commonSheet;if(commonView||name!==state.project?.scope)action(()=>configure(name));return}const target=event.target.closest('[data-sku-image],[data-completion]');if(!target)return;const row=state.rows.find(r=>r.id===target.dataset.completion);imageDialog.querySelector('h2').textContent=row?`${row.sku} · ${row.completed_at||'拍摄时间缺失'} · 标签照片`:target.dataset.skuName;imageDialog.querySelector('img').src=row?`/api/preview/${encodeURIComponent(row.completion.session_id)}/${encodeURIComponent(row.completion.capture_id)}?size=1600`:target.dataset.skuImage;imageDialog.showModal()});
  function render(){
    const p=state.project;if(displayProject!==p?.id){displayProject=p?.id;commonView=false}bar.classList.toggle('hidden',!p);manualType.hidden=!!p;const common=commonGroups(),commonShot=common.filter(r=>r.shot).length,count=commonView?{total:common.length,shot:commonShot,pending:common.length-commonShot,rows:common.length}:state.progress?.find(s=>s.sheet===p?.scope),all=(state.progress||[]).reduce((sum,s)=>sum+s.shot,0);(button.querySelector('.workspace-tab-label')||button).textContent=p?`拍摄进度 · ${all} 已拍摄`:'拍摄进度';
    $('shooting-document').textContent=p?`${p.name}${p.scope?' · '+p.scope:''}`:'尚未导入表格文档';$('shooting-document').title=p?.name||'';
    const context=JSON.stringify(FramekeepSku.context());if(context!==lastContext){lastContext=context;document.dispatchEvent(new CustomEvent('framekeep:sku-context'))}
    const project=JSON.stringify(FramekeepSku.shootingTypes());if(project!==lastProject){lastProject=project;document.dispatchEvent(new CustomEvent('framekeep:sku-project'))}
    options($('sku-projects'),state.projects.map(p=>[p.id,p.name]),p?.id,'选择拍摄任务');
    const sheets=p?.metadata.sheets.filter(s=>!s.skipped)||[];
    options($('sku-live-scope'),sheets.map(s=>[s.name,`${s.name}（${state.progress?.find(p=>p.sheet===s.name)?.total||0} 款）`]),p?.scope,'先选择当前拍摄类型');
    options($('sku-panel-scope'),sheets.map(s=>[s.name,s.name]),p?.scope,'先选择当前拍摄类型');
    const bound=!!p&&p.session_id===window.FRAMEKEEP_SESSION.id;
    $('sku-bind-current').textContent=bound?'已绑定当前目录':'绑定当前照片目录';$('sku-bind-current').classList.toggle('active',bound);
    const scoped=state.rows.filter(r=>r.sheet===p?.scope),pending=state.jobs.filter(j=>['review','failed','waiting_model'].includes(j.status)).length;
    const scopeCount=state.progress?.find(s=>s.sheet===p?.scope);
    $('sku-live-count').textContent=p?.scope?`${scopeCount?.shot||0} / ${scopeCount?.total||0} 已拍摄${pending?' · '+pending+' 条待处理':''}${bound?'':' · 当前目录未绑定'}`:'请选择拍摄工作表后再标记标签';
    const grouped=new Map();
    for(const row of scoped){const key=row.normalized||row.sku,prior=grouped.get(key);if(prior)prior.shot ||= row.status==='已拍摄';else grouped.set(key,{sku:row.sku,shot:row.status==='已拍摄'})}
    currentViews.hidden=!p;$('shooting-common-view').querySelector('span').textContent=common.length;
    for(const id of ['shooting-scope-view','shooting-common-view']){const selected=(id==='shooting-common-view')===commonView;$(id).setAttribute('aria-selected',String(selected));$(id).tabIndex=selected?0:-1}
    currentTable.setAttribute('aria-labelledby',commonView?'shooting-common-view':'shooting-scope-view');
    const currentRows=commonView?common:[...grouped.values()],empty=!p?'尚未导入拍摄清单，可点击上方导入表格。':commonView?'没有跨工作表重复的公共款号。':!p.scope?'请选择当前拍摄工作表。':'当前工作表没有款号。';
    const currentHtml='<table><thead><tr><th scope="col">序号</th><th scope="col">款号</th><th scope="col">'+(commonView?'各表状态':'拍摄状态')+'</th></tr></thead><tbody>'+(currentRows.length?currentRows.map((r,index)=>`<tr><td>${index+1}</td><td>${esc(r.sku)}</td><td>${commonView?commonMemberships(r):`<span class="sku-state ${r.shot?'sku-shot':''}">${r.shot?'已拍摄':'未拍摄'}</span>`}</td></tr>`).join(''):`<tr><td colspan="3" class="current-shooting-empty">${esc(empty)}</td></tr>`)+ '</tbody></table>';
    currentTable.classList.toggle('is-common',commonView);
    if(currentTable.dataset.content!==currentHtml){currentTable.dataset.content=currentHtml;currentTable.innerHTML=currentHtml}
    $('sku-export').disabled=!p||exporting;$('sku-export').textContent=exporting?'正在导出…':'导出拍摄记录';$('sku-recognize').disabled=!p||!p.scope||!bound;$('sku-auto').disabled=!p||busy||!!window.FramekeepAutomation?.isSaving();$('sku-auto').checked=!!p?.enabled;
    if(dialog.classList.contains('hidden'))return;
    $('sku-project-info').textContent=p?`${p.name} · 当前拍摄：${p.scope||'尚未选择'} · ${bound?'已绑定当前照片目录':'需绑定当前照片目录'}。源表保持原样，工作副本自动更新。`:'先导入本次拍摄表格，各页款号分别对账。';
    if(p?.export_error)message(p.export_error);
    dialog.querySelector('.progress-note').textContent=commonView?'公共款号在多个工作表中重复出现，按款号去重，各表状态分别保留；全部所属工作表均已拍摄才计为完成。查看公共不改变当前 OCR 工作表。':'序号按当前列表从1连续排列，原表行号单独保留。完成时间取自标签照片拍摄信息，点击时间可查证标签；缺失拍摄时间时不使用文件时间代替。';
    const summary=count?`<div><span>总款数</span><strong id="progress-total">${count.total}</strong></div><div><span>已拍摄</span><strong id="progress-shot">${count.shot}</strong></div><div><span>未拍摄</span><strong id="progress-pending">${count.pending}</strong></div><div class="progress-meter"><span>${count.total?Math.round(count.shot/count.total*100):0}% 完成</span><progress aria-label="当前工作表完成进度" max="${count.total||1}" value="${count.shot}"></progress>${count.rows!==count.total?'<small>按款号去重统计，原表行全部保留</small>':''}</div>`:'<p class="progress-empty">选择工作表后显示本页拍摄进度</p>';if($('sku-progress-summary').innerHTML!==summary)$('sku-progress-summary').innerHTML=summary;
    renderRows();
    const sheetKey=p?.id+'|'+p?.scope;
    if(sheetKey!==lastSheet){lastSheet=sheetKey;const panel=$('sku-sheet-panel');panel.getAnimations().forEach(a=>a.cancel());if(!matchMedia('(prefers-reduced-motion: reduce)').matches)panel.animate([{opacity:.4,translate:'0 4px'},{opacity:1,translate:'none'}],{duration:200,easing:getComputedStyle(document.documentElement).getPropertyValue('--motion-ease').trim()||'cubic-bezier(.22,1,.36,1)'})}
    const signature=JSON.stringify(state.jobs);
    if(signature===lastJobs||document.activeElement?.matches('[data-sku-target]'))return;
    lastJobs=signature;
    $('sku-jobs').innerHTML=state.jobs.length?state.jobs.map(j=>{
      const candidates=j.result.candidates||[],choices=state.rows.filter(r=>r.sheet===j.scope||r.sheet===state.project?.metadata?.managed_sheet).sort((a,b)=>(candidates.findIndex(c=>c.row_id===a.id)+1||999)-(candidates.findIndex(c=>c.row_id===b.id)+1||999));
      const raw=(j.result.readings||[]).map(r=>`${r.model}：${r.text}${r.codes?.length?'\n读取款号：'+r.codes.join('、'):''}${r.unscored?'\n模型未提供置信度，需人工核对。':''}`).join('\n');
      const title=j.trigger==='full_auto'&&j.status==='queued'?'等待逐图扫描':status[j.status]||esc(j.status);
      return `<article class="sku-job" data-job="${j.id}" data-status="${j.status}"><img src="/api/preview/${encodeURIComponent(j.session_id)}/${encodeURIComponent(j.capture_id)}?size=480" alt="识别照片" loading="lazy"><div><strong>${title} · ${esc(j.scope)}</strong><p>${esc(j.reason)}</p>${raw?`<details><summary>识别原文与模型</summary><pre>${esc(raw)}</pre></details>`:''}${candidates.length?`<p class="sku-candidates">候选：${candidates.slice(0,4).map(c=>`${esc(c.sku)}（相似度 ${Math.round(c.score*100)}%，${esc(c.sheet)} 第 ${c.row} 行）`).join('；')}</p>`:''}<div class="sku-review-actions"><select data-sku-target aria-label="确认款号"><option value="">选择本页准确款号</option>${choices.map(r=>`<option value="${r.id}">${esc(r.sku)} · 第 ${r.excel_row} 行</option>`).join('')}</select><button data-sku-action="confirm">确认</button><button data-sku-action="retry">重试</button><button data-sku-action="ignore">${['matched','confirmed'].includes(j.status)?'撤销识别':'忽略'}</button></div></div></article>`
    }).join(''):'<p class="dialog-copy">尚无识别记录。拍摄款号标签后，用遥控鼠标标成二星并保持。</p>';
  }
  async function load(){const version=mutation,next=await api('/api/sku');if(version===mutation&&!busy){state=next;render()}}
  async function exclusive(fn){if(busy)throw new Error('正在处理拍摄任务，请稍后重试');if(window.FramekeepAutomation?.isSaving())throw new Error('正在切换全自动 OCR，请稍后重试');busy=true;mutation++;window.FramekeepAutomation?.refresh();try{return await fn()}finally{busy=false;window.FramekeepAutomation?.refresh()}}
  async function action(fn){if(busy)return;try{await exclusive(fn)}catch(e){message(e.message);Framekeep.notify(e.message)}}
  async function importPlan(file,bindCurrent=false){
    if(!file)return;
    if(!file.name.toLowerCase().endsWith('.xlsx'))throw new Error('请选择包含款号的 .xlsx 拍摄计划表格');
    if(file.size>20_000_000)throw new Error('Excel 需要小于 20MB');
    if(window.FramekeepAutomation?.isSaving())throw new Error('正在切换全自动 OCR，请稍后导入');
    return exclusive(async()=>{
      $('sku-import').disabled=$('shooting-import').disabled=true;
      try{
        const previousScope=state.project?.scope||$('shooting-manual-type').value.trim();
        await FramekeepIO.flush();const bytes=new Uint8Array(await file.arrayBuffer());let binary='';for(let i=0;i<bytes.length;i+=32768)binary+=String.fromCharCode(...bytes.subarray(i,i+32768));
        state=await api('/api/sku/import',{name:file.name,content_base64:btoa(binary)});
        if(bindCurrent){
          const sheets=state.project.metadata.sheets.filter(s=>!s.skipped),scope=sheets.find(s=>s.name===previousScope)?.name||sheets[0]?.name;
          if(scope&&window.FRAMEKEEP_SESSION.id)state=await api('/api/sku/configure',{project_id:state.project.id,session_id:window.FRAMEKEEP_SESSION.id,scope,enabled:true});
        }
        lastJobs='';render();const bound=bindCurrent&&state.project.scope&&state.project.session_id===window.FRAMEKEEP_SESSION.id;message(bound?'已导入并绑定当前目录，可直接开启全自动 OCR。':'已导入拍摄计划。请选择工作表并绑定照片目录。');return {...FramekeepSku.shootingTypes(),bound:!!bound};
      }finally{$('sku-import').disabled=$('shooting-import').disabled=false}
    });
  }
  async function configure(scope,bind=false,enabled=state.project?.enabled){
    const p=state.project;if(!p)throw new Error('请先导入 Excel');
    if(!scope)throw new Error('请选择当前拍摄类型／工作表');
    await FramekeepIO.flush();
    state=await api('/api/sku/configure',{project_id:p.id,session_id:bind?window.FRAMEKEEP_SESSION.id:p.session_id||window.FRAMEKEEP_SESSION.id,scope,enabled});commonView=false;render();message('当前拍摄：'+scope+'。新标签只更新这一页。');
  }
  async function poll(){if(!busy)try{await load()}catch{}clearTimeout(timer);timer=setTimeout(poll,1000)}
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
  window.FramekeepWorkspace={show:showWorkspace,current:()=>currentPage,register};
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
  document.addEventListener('keydown',e=>{if(currentPage!=='selection'&&!document.querySelector('dialog[open]')&&!/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)&&!e.target.isContentEditable&&!tabs.contains(e.target)&&!$('sku-sheet-tabs').contains(e.target)&&!e.target.closest('.view-menu'))e.stopPropagation()},true);

  $('sku-sheet-tabs').addEventListener('click',e=>{if(e.target.closest('[data-common-view]')){commonView=true;render();return}const target=e.target.closest('[data-progress-sheet]');if(target)action(()=>configure(target.dataset.progressSheet))});
  currentViews.addEventListener('click',e=>{const target=e.target.closest('[data-current-view]');if(target){commonView=target.dataset.currentView==='common';render()}});
  currentViews.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.stopPropagation();e.preventDefault();commonView=e.key==='Home'?false:e.key==='End'?true:!commonView;render();$(commonView?'shooting-common-view':'shooting-scope-view').focus()});
  currentTable.addEventListener('click',e=>{const target=e.target.closest('[data-common-sheet]');if(target)action(()=>configure(target.dataset.commonSheet))});
  $('sku-sheet-tabs').addEventListener('keydown',e=>{e.stopPropagation();if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const buttons=[...$('sku-sheet-tabs').querySelectorAll('[role=tab]')],index=buttons.indexOf(e.target),next=buttons[e.key==='Home'?0:e.key==='End'?buttons.length-1:(index+(e.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length];if(!next)return;buttons.forEach(b=>b.tabIndex=b===next?0:-1);next.focus()});
  $('sku-live-scope').addEventListener('change',e=>action(()=>configure(e.target.value)));
  $('sku-panel-scope').addEventListener('change',e=>action(()=>configure(e.target.value)));
  $('sku-bind-current').addEventListener('click',()=>action(()=>configure($('sku-live-scope').value,true)));
  $('sku-auto').addEventListener('change',e=>action(()=>configure(state.project?.scope,false,e.target.checked)));
  $('sku-projects').addEventListener('change',e=>action(async()=>{const projectId=e.target.value;await FramekeepIO.flush();state=await api('/api/sku/activate',{project_id:projectId});lastJobs='';render()}));
  $('sku-import').addEventListener('click',()=>$('sku-file').click());
  $('shooting-import').addEventListener('click',()=>$('shooting-plan-file').click());
  $('shooting-plan-file').addEventListener('change',async event=>{try{if(event.target.files[0]){const result=await importPlan(event.target.files[0],true);Framekeep.notify(result.bound?'表格已导入，可在右栏开启全自动 OCR':'表格已导入，请选择工作表并绑定照片目录');window.FramekeepSidebar?.show('shooting')}}catch(error){Framekeep.notify(error.message);$('shooting-auto-status').textContent=error.message}finally{event.target.value=''}});
  $('sku-file').addEventListener('change',async e=>{try{await importPlan(e.target.files[0])}catch(error){message(error.message);Framekeep.notify(error.message)}finally{e.target.value=''}});
  $('sku-export').addEventListener('click',()=>action(async()=>{
    if(!state.project)throw new Error('请先导入拍摄清单');
    const projectId=state.project.id;exporting=true;render();
    try{
      await FramekeepIO.flush();
      const output=await api('/api/sku/export',{project_id:projectId});if(state.project?.id===projectId)state.project.export_error='';
      if(window.FramekeepDesktop?.saveSkuWorkbook){const saved=await FramekeepDesktop.saveSkuWorkbook(output.project_id,output.name);message(saved?'拍摄记录已导出：'+output.name:'已取消导出');return}
      const a=document.createElement('a');a.href='/api/sku/download?project_id='+encodeURIComponent(output.project_id);a.download=output.name;document.body.append(a);a.click();a.remove();message('已开始下载拍摄记录：'+output.name);
    }finally{exporting=false;render()}
  }));
  async function recognizeCurrent(){const target=Framekeep.remoteTarget();await FramekeepIO.flush();state=await api('/api/sku/recognize',{session_id:target.sessionId,capture_id:target.photoId});render();Framekeep.notify('已提交二星标签识别，可在识别记录查看结果')}
  $('sku-recognize').addEventListener('click',()=>action(recognizeCurrent));
  $('sku-jobs').addEventListener('click',e=>{const b=e.target.closest('[data-sku-action]');if(!b)return;const card=b.closest('[data-job]');action(async()=>{const row=card.querySelector('[data-sku-target]').value;if(b.dataset.skuAction==='confirm'&&!row)throw new Error('请先选择本工作表中的准确款号');state=await api('/api/sku/review',{job_id:card.dataset.job,action:b.dataset.skuAction,row_id:row});lastJobs='';render()})});
  $('sku-search').addEventListener('input',renderRows);$('sku-filter').addEventListener('change',renderRows);
  document.addEventListener('framekeep:selection',render);
  poll();
})();
