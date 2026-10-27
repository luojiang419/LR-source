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
  automation.innerHTML='<div class="shooting-import-line"><strong>拍摄文档</strong><button id="shooting-import" class="bordered" type="button">导入表格</button><input id="shooting-plan-file" type="file" accept=".xlsx" hidden></div><p id="shooting-document" class="shooting-document">尚未导入表格文档</p><label class="shooting-auto-line"><span>全自动 OCR</span><input id="shooting-full-auto" class="toggle-switch" type="checkbox" role="switch" aria-label="右栏全自动 OCR" aria-describedby="shooting-auto-status" disabled></label><p id="shooting-auto-status" class="shooting-auto-status" role="status">无需文档，打开照片目录即可开启</p>';
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
  let state={project:null,projects:[],rows:[],jobs:[]},busy=false,timer=null,lastJobs='',lastContext='',lastProject='',lastSheet='',mutation=0,exporting=false,commonView=false,allView=false,displayProject=null;
  window.FramekeepSku={context:()=>window.FRAMEKEEP_SESSION.id&&state.project?.scope&&state.project.enabled&&state.project.session_id===window.FRAMEKEEP_SESSION.id?{project_id:state.project.id,scope:state.project.scope}:null,shootingTypes:()=>state.project?{project_id:state.project.id,name:state.project.name,scope:state.project.scope,sheets:state.project.metadata.sheets.filter(s=>!s.skipped).map(s=>s.name),counts:(state.progress||[]).map(s=>({sheet:s.sheet,total:s.total}))}:null,isBusy:()=>busy,importPlan,bindCurrent:(scope,projectId)=>exclusive(async()=>{if(state.project?.id!==projectId)throw new Error('拍摄计划已变化，请重新选择');await configure(scope,true)})};
  FramekeepSku.currentType=()=>state.project?state.project.scope||'':$('shooting-manual-type').value.trim();
  const status={queued:'等待二星稳定',waiting_model:'待配置模型',running:'识别中',matched:'已自动匹配',confirmed:'人工确认',review:'待确认',failed:'识别失败',cancelled:'已撤销',ignored:'已忽略'};
  function message(text){$('sku-message').textContent=text}
  function options(select,items,value,placeholder){const next=(placeholder?`<option value="">${placeholder}</option>`:'')+items.map(([id,label])=>`<option value="${esc(id)}">${esc(label)}</option>`).join('');if(select.innerHTML!==next)select.innerHTML=next;select.value=value||''}
  const headerTitle=(key,fallback)=>state.project?.presentation?.headers?.[key]||fallback;
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
    const p=state.project,sheetTabs=$('sku-sheet-tabs'),focused=sheetTabs.contains(document.activeElement)?document.activeElement.id:null,progress=state.progress||[],stop=focused||(allView?'sku-all-tab':commonView?'sku-common-tab':`sku-sheet-tab-${Math.max(0,progress.findIndex(s=>s.sheet===p?.scope))}`);
    const sheetButtons=(p?`<button id="sku-all-tab" role="tab" aria-controls="sku-sheet-panel" data-all-view aria-selected="${allView}" aria-pressed="${allView}" tabindex="${stop==='sku-all-tab'?0:-1}">全部 <span>${state.all_rows?.length||0}</span></button>`:'')+progress.map((s,index)=>`<button id="sku-sheet-tab-${index}" role="tab" aria-controls="sku-sheet-panel" data-progress-sheet="${esc(s.sheet)}" aria-selected="${!allView&&!commonView&&s.sheet===p?.scope}" aria-pressed="${!allView&&!commonView&&s.sheet===p?.scope}" tabindex="${'sku-sheet-tab-'+index===stop?0:-1}" title="${esc(s.sheet)}：${matches?'匹配款号':'已拍摄'} ${matches?matches.get(s.sheet)?.size||0:s.shot} / ${s.total}">${esc(s.sheet)} <span>${matches?matches.get(s.sheet)?.size||0:s.shot}/${s.total}</span></button>`).join('')+(p?`<button id="sku-common-tab" role="tab" aria-controls="sku-sheet-panel" data-common-view aria-selected="${commonView}" aria-pressed="${commonView}" tabindex="${stop==='sku-common-tab'?0:-1}">公共 <span>${commonGroups().length}</span></button>`:'');
    if(sheetTabs.innerHTML!==sheetButtons){sheetTabs.innerHTML=sheetButtons;if(focused)$(focused)?.focus({preventScroll:true})}
    const activeTab=allView?$('sku-all-tab'):commonView?$('sku-common-tab'):[...sheetTabs.children].find(b=>b.dataset.progressSheet===p?.scope);if(activeTab)$('sku-sheet-panel').setAttribute('aria-labelledby',activeTab.id);else $('sku-sheet-panel').removeAttribute('aria-labelledby');
  }
  function renderRows(){
    if($('sku-rows').querySelector('.sku-header-input'))return;
    const query=$('sku-search').value.trim().toUpperCase(),filter=$('sku-filter').value,p=state.project;
    const sheet=p?.metadata.sheets.find(s=>s.name===p.scope),columns=(sheet?.columns||[]).filter(c=>c.title.trim()!=='备注'),notes=(sheet?.columns||[]).filter(c=>c.title.trim()==='备注');
    const memberships=new Map(),sheetNames=(p?.metadata.sheets||[]).filter(s=>!s.skipped).map(s=>s.name);
    if(query)for(const row of state.rows){const key=row.normalized||row.sku;if(!memberships.has(key))memberships.set(key,new Set());memberships.get(key).add(row.sheet)}
    const matchedRows=state.rows.filter(r=>(!query||r.sku.toUpperCase().includes(query))&&(filter==='all'||(filter==='shot')===(r.status==='已拍摄')));
    const matches=query?new Map(sheetNames.map(name=>[name,new Set()])):null;
    if(matches)for(const row of matchedRows)matches.get(row.sheet)?.add(row.normalized||row.sku);
    const searchSummary=$('sku-search-summary');if(searchSummary){searchSummary.hidden=!query;searchSummary.textContent=query?'款号搜索结果：'+sheetNames.map(name=>`${name} ${matches.get(name).size}张`).join('，'):''}
    renderSheetTabs(matches);
    if(allView){
      const rows=(state.all_rows||[]).filter(r=>(!query||r.sku.toUpperCase().includes(query))&&(filter==='all'||(filter==='shot')===r.shot));
      const html='<table><thead><tr>'+header('seq','序号','class="sku-sequence"')+header('row','原表行')+header('sku','款号')+header('photo','图片')+header('source','所属工作表')+'</tr></thead><tbody>'+rows.map((r,index)=>`<tr data-all-sku="${esc(r.normalized)}" class="${r.shot?'progress-shot':''}"><td class="sku-sequence">${index+1}</td><td class="sku-all-source">${r.rows.map(source=>`<span>${esc(source.sheet)} · ${source.excel_row}</span>`).join('')}</td><td><strong>${esc(r.sku)}</strong></td><td class="sku-product-images">${reconciliationImage(r)}</td><td class="sku-all-source">${Object.keys(r.memberships).map(name=>`<span>${esc(name)}</span>`).join('')}</td></tr>`).join('')+'</tbody></table>'+(rows.length?'':'<p class="dialog-copy">没有符合条件的款号。</p>');
      if($('sku-rows').innerHTML!==html)$('sku-rows').innerHTML=html;return;
    }
    if(commonView){
      const groups=commonGroups().filter(r=>(!query||r.sku.toUpperCase().includes(query))&&(filter==='all'||(filter==='shot')===r.shot));
      const html=groups.length?'<table><thead><tr><th scope="col" class="sku-sequence">序号</th><th scope="col">款号</th><th scope="col">所属工作表与拍摄状态</th><th>款号图片</th></tr></thead><tbody>'+groups.map((r,index)=>`<tr class="${r.shot?'progress-shot':''}"><td class="sku-sequence">${index+1}</td><td><strong>${esc(r.sku)}</strong></td><td>${commonMemberships(r)}</td><td class="sku-product-images">${reconciliationImage(r)}</td></tr>`).join('')+'</tbody></table>':'<p class="dialog-copy">没有符合条件的公共款号。仅汇总出现在多个工作表中的款号。</p>';
      if($('sku-rows').innerHTML!==html)$('sku-rows').innerHTML=html;return;
    }
    const rows=matchedRows.filter(r=>r.sheet===p?.scope).sort((a,b)=>(b.status==='已拍摄')-(a.status==='已拍摄')||String(b.completed_at||'').localeCompare(String(a.completed_at||''))||a.excel_row-b.excel_row);
    function images(r,list){return list.map(im=>`<button class="sku-product-image" data-sku-image="${esc(im.url)}" data-sku-name="${esc(r.sku)}" title="查看 ${esc(r.sku)} 产品图"><img src="${esc(im.url)}" alt="${esc(r.sku)} 产品图" loading="lazy"></button>`).join('')}
    function skuText(r){const sheets=memberships.get(r.normalized||r.sku);return `<span class="sku-code">${query?`<span class="sku-sheet-buttons" role="group" aria-label="${esc(r.sku)} 所属工作表">${sheetNames.filter(name=>sheets?.has(name)).map(name=>`<button class="sku-sheet-badge" data-sku-sheet="${esc(name)}" aria-pressed="${name===r.sheet}" title="查看 ${esc(name)} 工作表中的 ${esc(r.sku)}">${esc(name)}</button>`).join('')}</span>`:''}<strong title="原款号：${esc(r.sku)}">${esc(r.shoot_sku||r.sku)}</strong></span>`}
    const html=rows.length?'<table><thead><tr>'+header('seq','序号','class="sku-sequence"')+header('row','原表行')+columns.map(c=>header(columnKey(sheet,c),c.title)).join('')+header('status','拍摄状态')+header('photo','款号图片')+header('time','拍摄完成时间')+notes.map(c=>header(columnKey(sheet,c),c.title)).join('')+'</tr></thead><tbody>'+rows.map((r,index)=>`<tr data-sku-row="${esc(r.id)}" class="${r.status==='已拍摄'?'progress-shot':''}"><td class="sku-sequence" aria-label="序号 ${index+1}">${index+1}</td><td class="progress-row-number">${r.excel_row}</td>${columns.map(c=>{const imgs=(r.images||[]).filter(im=>im.cell.replace(/\d/g,'')===c.letter);return `<td class="${imgs.length?'sku-product-images':''}">${imgs.length?images(r,imgs):c.index===sheet.sku_col?skuText(r):esc(r.source_values?.[c.letter]||'')}</td>`}).join('')}<td><span class="sku-state ${r.status==='已拍摄'?'sku-shot':''}">${esc(r.status)}</span></td><td class="sku-product-images">${reconciliationImage(r)}</td><td class="progress-time">${r.completed_at?`${r.completion?`<button data-completion="${esc(r.id)}">${esc(r.completed_at.replace('T',' '))}</button>`:esc(r.completed_at.replace('T',' '))}`:r.status==='已拍摄'?'<span class="time-missing">拍摄时间缺失</span>':'—'}</td>${notes.map(c=>`<td>${esc(r.source_values?.[c.letter]||'')}</td>`).join('')}</tr>`).join('')+'</tbody></table>':`<p class="dialog-copy">${p?.scope?'没有符合条件的款号':'请选择当前拍摄工作表，各页分别统计进度。'}</p>`;
    if($('sku-rows').innerHTML!==html)$('sku-rows').innerHTML=html;
  }
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
      try{await exclusive(async()=>{state=await api('/api/sku/header',{project_id:pid,key,title});render();message('列名称已保存，Excel 工作副本与导出已同步更新。')});const updated=[...$('sku-rows').querySelectorAll('[data-header-key]')].find(el=>el.dataset.headerKey===key);animateHeader(updated);if(focus)updated?.focus({preventScroll:true})}
      catch(error){renderRows();message(error.message);Framekeep.notify(error.message)}
    }
    input.addEventListener('keydown',event=>{event.stopPropagation();if(event.isComposing)return;if(event.key==='Enter'){event.preventDefault();finish(false,true)}else if(event.key==='Escape'){event.preventDefault();finish(true,true)}});
    input.addEventListener('dblclick',event=>event.stopPropagation());input.addEventListener('blur',()=>finish(false));animateHeader(input);
  }
  $('sku-rows').addEventListener('dblclick',event=>{if(event.target.closest('input,textarea,select'))return;const cell=event.target.closest('th'),target=cell?.querySelector('[data-header-key]');if(target)editHeader(target)});
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
    if(!state.project||!target.dataset.skuCover)return;galleryProject=state.project.id;gallerySku=target.dataset.skuCover;galleryOrigin=target;galleryOffset=0;galleryData=null;galleryChoices={};gallerySaving=false;options($('sku-cover-group'),[[gallerySku,target.dataset.skuName]],gallerySku);$('sku-cover-context').textContent='当前款号：'+target.dataset.skuName+' · 仅显示该款号的拍摄区间图片';$('sku-cover-groups').innerHTML='';galleryDialog.showModal();$('sku-cover-close').focus();loadGallery();
  }
  function closeGallery(){if(!gallerySaving&&galleryDialog.open)galleryDialog.close()}
  for(const id of ['sku-cover-close','sku-cover-cancel'])$(id).onclick=closeGallery;
  galleryDialog.addEventListener('cancel',event=>{event.preventDefault();closeGallery()});galleryDialog.addEventListener('close',()=>{++galleryRequest;if(galleryOrigin?.isConnected)galleryOrigin.focus({preventScroll:true});galleryOrigin=null});
  $('sku-cover-group').onchange=()=>{galleryOffset=0;loadGallery()};$('sku-cover-prev').onclick=()=>{galleryOffset=Math.max(0,galleryOffset-60);loadGallery()};$('sku-cover-next').onclick=()=>{galleryOffset+=60;loadGallery()};
  $('sku-cover-groups').onclick=event=>{const image=event.target.closest('[data-cover-key]');if(!image||gallerySaving)return;galleryChoices[image.dataset.coverSku]=image.dataset.coverKey;paintGallery()};
  $('sku-cover-save').onclick=async()=>{
    if(gallerySaving||!Object.keys(galleryChoices).length)return;
    gallerySaving=true;paintGallery();$('sku-cover-group').disabled=true;
    try{if(galleryProject!==state.project?.id)throw new Error('拍摄任务已变化，请关闭后重新选择');await exclusive(async()=>{state=await api('/api/sku/cover',{project_id:galleryProject,sku:gallerySku,choices:galleryChoices});render()});gallerySaving=false;closeGallery();message('款号图片已保存，各工作表、全部汇总和 Excel 已同步更新。')}
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
  imageDialog.addEventListener('close',()=>{++imageRequest;endImageDrag();skuImage.onload=skuImage.onerror=null;skuImage.hidden=true;skuImage.removeAttribute('src');if(imageOrigin?.isConnected)imageOrigin.focus({preventScroll:true});imageOrigin=null});
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
    const row=state.rows.find(r=>r.id===target.dataset.completion),request=++imageRequest;imageOrigin=target;
    $('sku-image-title').textContent=row?`${row.sku} · ${row.completed_at||'拍摄时间缺失'} · 标签照片`:target.dataset.skuName;
    skuImage.hidden=true;skuImage.removeAttribute('src');imageWidth=imageHeight=0;fitSkuImage();$('sku-image-status').textContent='正在加载图片…';
    skuImage.alt=row?`${row.sku} 标签照片`:`${target.dataset.skuName} 产品图`;
    skuImage.onload=()=>{if(request!==imageRequest||!imageDialog.open)return;skuImage.hidden=false;layoutSkuImage();$('sku-image-status').textContent=''};
    skuImage.onerror=()=>{if(request!==imageRequest||!imageDialog.open)return;skuImage.hidden=true;paintSkuImage();$('sku-image-status').textContent='图片无法加载，请检查文件是否在线；按 Esc 退出'};
    if(!imageDialog.open)imageDialog.showModal();$('sku-image-close').focus({preventScroll:true});
    skuImage.src=row?`/api/preview/${encodeURIComponent(row.completion.session_id)}/${encodeURIComponent(row.completion.capture_id)}?size=1600`:target.dataset.skuImage;
  }
  $('sku-rows').addEventListener('click',event=>{const cover=event.target.closest('[data-sku-cover]');if(cover){openSkuGallery(cover);return}const sheet=event.target.closest('[data-sku-sheet],[data-common-sheet]');if(sheet){const name=sheet.dataset.skuSheet||sheet.dataset.commonSheet;if(commonView||name!==state.project?.scope)action(()=>configure(name));return}const target=event.target.closest('[data-sku-image],[data-completion]');if(target)openSkuImage(target)});
  function render(){
    const p=state.project;if(displayProject!==p?.id){displayProject=p?.id;commonView=false;allView=false}bar.classList.toggle('hidden',!p);manualType.hidden=!!p;const common=commonGroups(),commonShot=common.filter(r=>r.shot).length,allGroups=state.all_rows||[],allShot=allGroups.filter(r=>r.shot).length,count=allView?{total:allGroups.length,shot:allShot,pending:allGroups.length-allShot,rows:allGroups.length}:commonView?{total:common.length,shot:commonShot,pending:common.length-commonShot,rows:common.length}:state.progress?.find(s=>s.sheet===p?.scope),all=(state.progress||[]).reduce((sum,s)=>sum+s.shot,0);(button.querySelector('.workspace-tab-label')||button).textContent=p?`拍摄进度 · ${all} 已拍摄`:'拍摄进度';
    $('shooting-document').textContent=p&&!p.metadata.automatic_day?`${p.name}${p.scope?' · '+p.scope:''}`:'自由拍摄 · 无需导入表格';$('shooting-document').title=p&&!p.metadata.automatic_day?p.name:'';
    const context=JSON.stringify(FramekeepSku.context());if(context!==lastContext){lastContext=context;document.dispatchEvent(new CustomEvent('framekeep:sku-context'))}
    const project=JSON.stringify(FramekeepSku.shootingTypes());if(project!==lastProject){lastProject=project;document.dispatchEvent(new CustomEvent('framekeep:sku-project'))}
    options($('sku-projects'),state.projects.map(p=>[p.id,p.name]),p?.id,'选择拍摄任务');
    const sheets=p?.metadata.sheets.filter(s=>!s.skipped)||[];
    options($('sku-live-scope'),sheets.map(s=>[s.name,`${s.name}（${state.progress?.find(p=>p.sheet===s.name)?.total||0} 款）`]),p?.scope,'先选择当前拍摄类型');
    options($('sku-panel-scope'),[...(p?[['__framekeep_all__','全部（跨工作表汇总）']]:[]),...sheets.map(s=>[s.name,s.name])],allView?'__framekeep_all__':p?.scope,'先选择当前拍摄类型');
    const bound=!!p&&!!window.FRAMEKEEP_SESSION.id&&p.session_id===window.FRAMEKEEP_SESSION.id;
    $('sku-bind-current').textContent=bound?'已绑定当前目录':'绑定当前照片目录';$('sku-bind-current').classList.toggle('active',bound);
    const scoped=state.rows.filter(r=>r.sheet===p?.scope),pending=state.jobs.filter(j=>['review','failed','waiting_model'].includes(j.status)).length;
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
    $('sku-export').disabled=!p||exporting;$('sku-export').textContent=exporting?'正在导出…':'导出拍摄记录';$('sku-recognize').disabled=!p||!p.scope||!bound;$('sku-auto').disabled=!p||busy||!!window.FramekeepAutomation?.isSaving();$('sku-auto').checked=!!p?.enabled;
    if(dialog.classList.contains('hidden'))return;
    $('sku-project-info').textContent=p?`${p.name} · 当前拍摄：${p.scope||'尚未选择'} · ${bound?'已绑定当前照片目录':'需绑定当前照片目录'}。源表保持原样，工作副本自动更新。`:'先导入本次拍摄表格，各页款号分别对账。';
    if(p?.export_error)message(p.export_error);
    dialog.querySelector('.progress-note').textContent=allView?'全部工作表按款号去重，原表行按来源工作表分别列出；查看汇总不改变当前 OCR 工作表。双击表头修改列名称。':commonView?'公共款号在多个工作表中重复出现，按款号去重，各表状态分别保留；全部所属工作表均已拍摄才计为完成。查看公共不改变当前 OCR 工作表。':'序号按当前列表从1连续排列，原表行号单独保留。完成时间取自标签照片拍摄信息，点击时间可查证标签；缺失拍摄时间时不使用文件时间代替。';
    const summary=count?`<div><span>总款数</span><strong id="progress-total">${count.total}</strong></div><div><span>已拍摄</span><strong id="progress-shot">${count.shot}</strong></div><div><span>未拍摄</span><strong id="progress-pending">${count.pending}</strong></div><div class="progress-meter"><span>${count.total?Math.round(count.shot/count.total*100):0}% 完成</span><progress aria-label="当前工作表完成进度" max="${count.total||1}" value="${count.shot}"></progress>${count.rows!==count.total?'<small>按款号去重统计，原表行全部保留</small>':''}</div>`:'<p class="progress-empty">选择工作表后显示本页拍摄进度</p>';if($('sku-progress-summary').innerHTML!==summary)$('sku-progress-summary').innerHTML=summary;
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
      return `<article class="sku-job" data-job="${j.id}" data-status="${j.status}"><img src="/api/preview/${encodeURIComponent(j.session_id)}/${encodeURIComponent(j.capture_id)}?size=480" alt="识别照片" loading="lazy"><div><strong>${title} · ${esc(j.scope)}</strong><p>${esc(j.reason)}</p>${raw?`<details><summary>识别原文与模型</summary><pre>${esc(raw)}</pre></details>`:''}${candidates.length?`<p class="sku-candidates">候选：${candidates.slice(0,4).map(c=>`${esc(c.sku)}（相似度 ${Math.round(c.score*100)}%，${esc(c.sheet)} 第 ${c.row} 行）`).join('；')}</p>`:''}<div class="sku-review-actions"><select data-sku-target aria-label="确认款号"><option value="">选择本页准确款号</option>${choices.map(r=>`<option value="${r.id}">${esc(r.sku)} · 第 ${r.excel_row} 行</option>`).join('')}</select><button data-sku-action="confirm">确认</button><button data-sku-action="retry">重试</button><button data-sku-action="ignore">${['matched','confirmed'].includes(j.status)?'撤销识别':'忽略'}</button></div></div></article>`
    }).join(''):'<p class="dialog-copy">尚无识别记录。拍摄款号标签后，用遥控鼠标标成二星并保持。</p>';
  }
  let loadRequest=0;
  async function load(){const request=++loadRequest,version=mutation,next=await api('/api/sku');if(request===loadRequest&&version===mutation&&!busy){state=next;render()}}
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
        const previousScope=state.project?.scope||$('shooting-manual-type').value.trim(),sessionId=bindCurrent?window.FRAMEKEEP_SESSION.id:null;
        await FramekeepIO.flush();const bytes=new Uint8Array(await file.arrayBuffer());let binary='';for(let i=0;i<bytes.length;i+=32768)binary+=String.fromCharCode(...bytes.subarray(i,i+32768));
        state=await api('/api/sku/import',{name:file.name,content_base64:btoa(binary),session_id:sessionId,preferred_scope:previousScope},90000);
        $('sku-search').value='';$('sku-filter').value='all';lastJobs='';render();
        const bound=!!sessionId&&state.project.session_id===window.FRAMEKEEP_SESSION.id,reused=!!state.already_imported;message(reused?'该文件已导入，已复用原拍摄任务并保留已有进度。':bound?'已导入并绑定当前目录，可直接开启全自动 OCR。':'已导入拍摄计划，可查看各工作表；打开照片目录后可绑定拍摄。');return {...FramekeepSku.shootingTypes(),bound,reused};
      }finally{$('sku-import').disabled=$('shooting-import').disabled=false}
    });
  }
  async function configure(scope,bind=false,enabled=state.project?.enabled){
    const p=state.project;if(!p)throw new Error('请先导入 Excel');
    if(!scope)throw new Error('请选择当前拍摄类型／工作表');
    if(bind&&!window.FRAMEKEEP_SESSION.id)throw new Error('请先打开照片目录，再绑定拍摄计划');
    await FramekeepIO.flush();
    state=await api('/api/sku/configure',{project_id:p.id,session_id:bind?window.FRAMEKEEP_SESSION.id:p.session_id||window.FRAMEKEEP_SESSION.id,scope,enabled});commonView=false;allView=false;render();message('当前拍摄：'+scope+'。新标签只更新这一页。');
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
  document.addEventListener('keydown',e=>{if(currentPage!=='selection'&&!document.querySelector('dialog[open]')&&!/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)&&!e.target.isContentEditable&&!tabs.contains(e.target)&&!$('sku-sheet-tabs').contains(e.target)&&!e.target.closest('.view-menu,.app-settings-nav'))e.stopPropagation()},true);

  $('sku-sheet-tabs').addEventListener('click',e=>{if(e.target.closest('[data-all-view]')){allView=true;commonView=false;render();return}if(e.target.closest('[data-common-view]')){allView=false;commonView=true;render();return}const target=e.target.closest('[data-progress-sheet]');if(target)action(()=>configure(target.dataset.progressSheet))});
  currentViews.addEventListener('click',e=>{const target=e.target.closest('[data-current-view]');if(target){allView=false;commonView=target.dataset.currentView==='common';render()}});
  currentViews.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.stopPropagation();e.preventDefault();commonView=e.key==='Home'?false:e.key==='End'?true:!commonView;render();$(commonView?'shooting-common-view':'shooting-scope-view').focus()});
  currentTable.addEventListener('click',e=>{const cover=e.target.closest('[data-sku-cover]');if(cover){openSkuGallery(cover);return}const target=e.target.closest('[data-common-sheet]');if(target)action(()=>configure(target.dataset.commonSheet));else {const image=e.target.closest('[data-sku-image]');if(image)openSkuImage(image)}});
  $('sku-sheet-tabs').addEventListener('keydown',e=>{e.stopPropagation();if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const buttons=[...$('sku-sheet-tabs').querySelectorAll('[role=tab]')],index=buttons.indexOf(e.target),next=buttons[e.key==='Home'?0:e.key==='End'?buttons.length-1:(index+(e.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length];if(!next)return;buttons.forEach(b=>b.tabIndex=b===next?0:-1);next.focus()});
  $('sku-live-scope').addEventListener('change',e=>action(()=>configure(e.target.value)));
  $('sku-panel-scope').addEventListener('change',e=>{if(e.target.value==='__framekeep_all__'){allView=true;commonView=false;render()}else action(()=>configure(e.target.value))});
  $('sku-bind-current').addEventListener('click',()=>action(()=>configure($('sku-live-scope').value,true)));
  $('sku-auto').addEventListener('change',e=>action(()=>configure(state.project?.scope,false,e.target.checked)));
  $('sku-projects').addEventListener('change',e=>action(async()=>{const projectId=e.target.value;await FramekeepIO.flush();state=await api('/api/sku/activate',{project_id:projectId});lastJobs='';render()}));
  $('sku-import').addEventListener('click',()=>$('sku-file').click());
  $('shooting-import').addEventListener('click',()=>$('shooting-plan-file').click());
  $('shooting-plan-file').addEventListener('change',async event=>{try{if(event.target.files[0]){const result=await importPlan(event.target.files[0],true);Framekeep.notify(result.reused?'该文件已导入，已复用原任务并保留拍摄进度':result.bound?'表格已导入，可在右栏开启全自动 OCR':'表格已导入，请选择工作表并绑定照片目录');window.FramekeepSidebar?.show('shooting')}}catch(error){Framekeep.notify(error.message);$('shooting-auto-status').textContent=error.message}finally{event.target.value=''}});
  $('sku-file').addEventListener('change',async e=>{try{await importPlan(e.target.files[0],true)}catch(error){message(error.message);Framekeep.notify(error.message)}finally{e.target.value=''}});
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
  for(const event of ['framekeep:ratings-saved','framekeep:ocr-choice'])document.addEventListener(event,()=>{mutation++;load().catch(()=>{})});
  poll();
})();
