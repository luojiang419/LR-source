(() => {
  'use strict';
  const library=window.FramekeepSkuLibrary;if(!library||!window.FramekeepIO)return;
  const $=id=>document.getElementById(id),api=FramekeepIO.api;
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const page=$('sku-database-page'),layout=page.querySelector('.sku-database-layout'),sidebar=page.querySelector('.sku-database-sidebar');
  const bins=document.createElement('aside');bins.className='sku-bin-sidebar';bins.setAttribute('aria-label','SKU 组');
  bins.innerHTML='<header class="sku-pool-section-heading"><strong>组</strong><button id="sku-bin-create" type="button" aria-label="新建 SKU 组" title="新建组">＋</button></header><div id="sku-bin-tree" class="sku-bin-tree" role="tree" aria-label="SKU 分组"></div><p class="sku-bin-hint">将款号拖入文件夹归组</p>';
  layout.prepend(bins);sidebar.insertAdjacentHTML('afterbegin','<header class="sku-pool-section-heading"><strong>SKU 款号</strong><span>双击修改</span></header>');
  bins.id='sku-bin-sidebar';sidebar.id='sku-list-sidebar';
  const panels=document.createElement('div');panels.className='sku-panel-toggles';panels.setAttribute('role','group');panels.setAttribute('aria-label','SKU 左侧面板显示');
  panels.innerHTML='<button id="sku-toggle-bins" type="button" class="bordered" aria-controls="sku-bin-sidebar">组面板</button><button id="sku-toggle-list" type="button" class="bordered" aria-controls="sku-list-sidebar">款号面板</button>';
  page.querySelector('.catalog-heading').append(panels);
  const edge=document.createElement('button');edge.id='sku-bins-edge-toggle';edge.type='button';edge.className='panel-edge-toggle';edge.setAttribute('aria-controls',bins.id);edge.innerHTML='<span aria-hidden="true"></span>';layout.append(edge);
  for(const [kind,panel] of [['bins',bins],['list',sidebar]]){
    const button=$('sku-toggle-'+kind),key='framekeep.sku.panel.'+kind;
    function show(visible){panel.hidden=!visible;layout.classList.toggle('sku-'+kind+'-collapsed',!visible);button.setAttribute('aria-expanded',String(visible));button.title=(visible?'折叠':'展开')+(kind==='bins'?'组':'款号')+'面板';if(kind==='bins'){edge.firstElementChild.textContent=visible?'‹':'›';edge.title=button.title;edge.setAttribute('aria-label',edge.title);edge.setAttribute('aria-expanded',String(visible))}}
    let visible=true;try{visible=localStorage.getItem(key)!=='collapsed'}catch{}
    show(visible);button.onclick=()=>{show(panel.hidden);try{localStorage.setItem(key,panel.hidden?'collapsed':'expanded')}catch{}};
  }
  edge.onclick=()=>$('sku-toggle-bins').click();
  $('sku-view-flat').textContent='款号清单';$('sku-view-tree').textContent='文件路径';
  const status=document.createElement('p');status.id='sku-pool-status';status.className='sku-pool-status';status.setAttribute('role','status');page.querySelector('.catalog-heading').after(status);
  const dialog=document.createElement('dialog');dialog.id='sku-pool-editor';dialog.setAttribute('aria-labelledby','sku-pool-editor-title');
  dialog.innerHTML='<form id="sku-pool-form"><div class="dialog-heading"><h2 id="sku-pool-editor-title"></h2><button type="button" data-pool-close aria-label="关闭编辑">×</button></div><p id="sku-pool-editor-help" class="dialog-copy"></p><label id="sku-pool-name-label" class="sku-pool-field"><span id="sku-pool-name-caption">名称</span><input id="sku-pool-name" maxlength="100" autocomplete="off"></label><label id="sku-pool-target-label" class="sku-pool-field">目标组<select id="sku-pool-target"></select></label><p id="sku-pool-editor-message" role="status" class="dialog-copy"></p><div class="dialog-actions"><button type="button" data-pool-close>取消</button><button id="sku-pool-submit" class="primary" type="submit">保存</button></div></form>';
  document.body.append(dialog);
  const writeView=document.createElement('section');writeView.id='sku-write-view';writeView.hidden=true;writeView.tabIndex=-1;
  writeView.innerHTML=`<div class="sku-write-heading"><span>图片资料更新</span><span id="sku-write-elapsed" aria-hidden="true">00:00</span></div>
    <svg class="sku-write-art" viewBox="0 0 440 204" fill="none" aria-hidden="true">
      <defs><linearGradient id="sku-write-glow" x1="0" y1="0" x2="1" y2="0"><stop stop-color="currentColor" stop-opacity="0"/><stop offset=".5" stop-color="currentColor" stop-opacity=".65"/><stop offset="1" stop-color="currentColor" stop-opacity="0"/></linearGradient><clipPath id="sku-write-tag-clip"><path d="M278 53h71l38 42v58a12 12 0 0 1-12 12h-97a12 12 0 0 1-12-12V65a12 12 0 0 1 12-12Z"/></clipPath></defs>
      <circle class="sku-write-orbit" cx="325" cy="108" r="77"/><circle class="sku-write-orbit sku-write-orbit-inner" cx="325" cy="108" r="66"/>
      <path class="sku-write-route" d="M164 110h28c25 0 32-19 58-19h16"/><path class="sku-write-route sku-write-current" d="M164 110h28c25 0 32-19 58-19h16"/>
      <g class="sku-write-photos"><rect class="sku-write-paper sku-write-back" x="44" y="57" width="102" height="112" rx="12" transform="rotate(-9 95 113)"/><rect class="sku-write-paper sku-write-middle" x="54" y="51" width="102" height="112" rx="12" transform="rotate(5 105 107)"/><rect class="sku-write-paper" x="58" y="53" width="102" height="112" rx="12"/><rect class="sku-write-image" x="68" y="63" width="82" height="72" rx="6"/><circle class="sku-write-sun" cx="127" cy="81" r="8"/><path class="sku-write-mountain" d="m69 122 23-28 17 19 12-13 28 27v7H69Z"/><path class="sku-write-caption" d="M72 148h41m8 0h13"/></g>
      <path class="sku-write-tag" d="M278 53h71l38 42v58a12 12 0 0 1-12 12h-97a12 12 0 0 1-12-12V65a12 12 0 0 1 12-12Z"/>
      <path class="sku-write-fold" d="M349 53v29a13 13 0 0 0 13 13h25"/><circle class="sku-write-hole" cx="282" cy="72" r="3"/>
      <g class="sku-write-barcode"><path d="M285 103v31m7-31v31m10-31v31m6-31v31m11-31v31m9-31v31m6-31v31m11-31v31m6-31v31m10-31v31"/><path class="sku-write-caption" d="M287 146h26m8 0h38"/></g>
      <g clip-path="url(#sku-write-tag-clip)"><rect class="sku-write-scan" x="266" y="75" width="121" height="28" fill="url(#sku-write-glow)"/></g>
      <circle class="sku-write-seal" cx="365" cy="158" r="22"/><path class="sku-write-check" d="m355 158 7 7 13-15"/>
      <circle class="sku-write-spark sku-write-spark-one" cx="193" cy="75" r="3"/><circle class="sku-write-spark sku-write-spark-two" cx="225" cy="139" r="2"/>
    </svg>
    <h2 id="sku-write-title" role="status" aria-live="polite">正在准备图片</h2><p id="sku-write-target"></p><p id="sku-write-copy"></p>
    <div class="sku-write-meter"><progress id="sku-write-bar" aria-label="SKU 文件写入进度"></progress><div><span id="sku-write-count">正在核对所选图片…</span><strong id="sku-write-percent">准备中</strong></div></div>
    <ol class="sku-write-stages" aria-label="款号更新步骤"><li data-write-stage="preparing"><i>1</i>准备图片</li><li data-write-stage="writing"><i>2</i>写入款号</li><li data-write-stage="syncing"><i>3</i>同步清单</li></ol>
    <details id="sku-write-errors" hidden><summary>查看未完成的文件</summary><ul></ul></details>
    <div id="sku-write-actions" class="dialog-actions" hidden><button type="button" id="sku-write-close">关闭</button><button type="button" id="sku-write-retry" class="primary">重试</button></div>`;
  dialog.append(writeView);
  const menu=document.createElement('div');menu.id='sku-pool-menu';menu.className='photo-context-menu';menu.setAttribute('role','menu');menu.hidden=true;document.body.append(menu);
  let current={groups:[],organization:{bins:[],total:0,ungrouped:0,removed:0}},editing=null,context=null,mutating=false,dragKey='',treeSnapshot='',progressTimer=null;
  const watched=new Set();
  let activeWrite=null,elapsedTimer=null;
  const selectionNote=document.createElement('p');selectionNote.id='sku-selection-note';selectionNote.className='sku-selection-note';selectionNote.setAttribute('role','status');
  $('sku-database-title').after(selectionNote);
  function paintSelectionNote(){const count=library.selectedRows.length;selectionNote.textContent=count?`已选 ${count} 张 · Shift 点击或拖动扩展区间 · 右键修改 SKU 款号`:'点击首图，按住 Shift 点击末图或拖动选择区间';}
  $('sku-database-grid').addEventListener('sku-selection-change',paintSelectionNote);paintSelectionNote();
  const say=(text,error=false)=>{status.textContent=text;status.classList.toggle('has-error',error)};
  const groupName=id=>id==='ungrouped'?'未分组':id==='trash'?'已移除':current.organization.bins.find(b=>b.id===id)?.name||'全部 SKU';
  function binButton(id,name,count,depth=0){return `<button type="button" role="treeitem" aria-level="${depth+1}" aria-selected="${library.selection.bin===id}" data-sku-bin="${esc(id)}" style="--bin-depth:${Math.min(depth,6)}"><span class="sku-bin-name"><span aria-hidden="true">${id==='trash'?'⌫':id?'▱':'▦'}</span>${esc(name)}</span><small>${count||0}</small></button>`}
  function paintBins(){
    const org=current.organization,key=JSON.stringify([org.bins,org.total,org.ungrouped,org.removed,library.selection.bin]);if(treeSnapshot===key)return;treeSnapshot=key;
    function children(parent,depth){return org.bins.filter(b=>b.parent===parent).map(b=>binButton(b.id,b.name,b.count,depth)+children(b.id,depth+1)).join('')}
    $('sku-bin-tree').innerHTML=binButton('','全部 SKU',org.total)+binButton('ungrouped','未分组',org.ungrouped)+'<div class="sku-bin-folders">'+children('',0)+'</div>'+binButton('trash','已移除',org.removed);
  }
  function closeMenu(){menu.hidden=true;context=null}
  function openMenu(target,x,y){
    const image=target.closest('#sku-database-grid .catalog-card[data-catalog-item]'),sku=target.closest('[data-db-sku]'),folder=target.closest('[data-sku-bin]');
    if(image){
      const item=library.selectedRows.find(row=>row.id===image.dataset.catalogItem);if(!item)return;
      context={kind:'images',item};
      menu.innerHTML='<button role="menuitem" data-pool-action="reveal">打开所在路径</button><button role="menuitem" data-pool-action="correct_selection">修改 SKU 款号</button>';
    }else if(sku){
      const item=current.groups.find(g=>g.normalized===sku.dataset.dbSku);if(!item)return;
      context={kind:'sku',item};
      menu.innerHTML=library.selection.bin==='trash'?'<button role="menuitem" data-pool-action="restore">恢复 SKU</button>':'<button role="menuitem" data-pool-action="rename">修改 SKU 款号</button><button role="menuitem" data-pool-action="move">移到组…</button><button role="menuitem" data-pool-action="remove">移除 SKU</button>';
    }else if(folder&&current.organization.bins.some(b=>b.id===folder.dataset.skuBin)){
      context={kind:'bin',item:current.organization.bins.find(b=>b.id===folder.dataset.skuBin)};
      menu.innerHTML='<button role="menuitem" data-pool-action="rename_bin">重命名组</button><button role="menuitem" data-pool-action="delete_bin">移除组，SKU 移回上级</button>';
    }else return;
    menu.hidden=false;menu.style.left=Math.max(4,Math.min(x,innerWidth-menu.offsetWidth-4))+'px';menu.style.top=Math.max(4,Math.min(y,innerHeight-menu.offsetHeight-4))+'px';menu.querySelector('button').focus();
  }
  function edit(action,item=null){
    if(mutating)return;resetWriteView();editing={action,item,...(action==='correct_selection'?{items:library.selectedRows.map(row=>({id:row.id,sku:row.sku})),request_id:crypto.randomUUID()}:{})};$('sku-pool-editor-message').textContent='';
    const correction=action==='correct_selection',rename=action==='rename'||correction,move=action==='move';
    $('sku-pool-editor-title').textContent=rename?'修改 SKU 款号':move?'将 SKU 移到组':action==='rename_bin'?'重命名组':'新建 SKU 组';
    $('sku-pool-editor-help').textContent=correction?`已选 ${editing.items.length} 张。保存后将这些图片归入正确款号，款号清单和文件路径视图同步更新，并更新已登记 RAW / JPG 的 SKU 元数据。`:rename?`${item.sku} · ${item.count} 张。保存后自动更新该款全部图片及已登记 RAW / JPG 的 SKU 元数据。`:move?'分组用于软件内管理，图片路径和元数据保留。':'创建用于整理 SKU 的软件内文件夹。';
    $('sku-pool-name-label').hidden=move;$('sku-pool-name').required=!move;$('sku-pool-name').maxLength=rename?80:100;$('sku-pool-name').value=rename?item.sku:action==='rename_bin'?item.name:'';$('sku-pool-name-caption').textContent=rename?'SKU 款号':'组名称';
    $('sku-pool-target-label').hidden=!move&&action!=='create_bin';
    $('sku-pool-target').innerHTML='<option value="">'+(move?'未分组':'顶层组')+'</option>'+current.organization.bins.map(b=>`<option value="${esc(b.id)}">${esc(b.name)}</option>`).join('');
    $('sku-pool-target').value=current.organization.bins.some(b=>b.id===library.selection.bin)?library.selection.bin:'';
    $('sku-pool-submit').textContent=correction?'保存并更新所选图片':rename?'保存并更新整款':move?'移动':'保存';dialog.showModal();if(move)$('sku-pool-target').focus();else{$('sku-pool-name').focus();$('sku-pool-name').select()}
  }
  function setBusy(value){mutating=value;$('sku-pool-submit').disabled=value;$('sku-pool-name').disabled=value;$('sku-pool-target').disabled=value}
  const operationIds=result=>Array.isArray(result.operation_ids)?result.operation_ids:JSON.parse(result.operations||'[]');
  function resetWriteView(){clearInterval(elapsedTimer);writeView.hidden=true;$('sku-pool-form').hidden=false;dialog.removeAttribute('aria-busy');dialog.setAttribute('aria-labelledby','sku-pool-editor-title')}
  function writePhase(phase,title,copy){
    writeView.dataset.state=phase;$('sku-write-title').textContent=title;$('sku-write-copy').textContent=copy;
    const index=['preparing','writing','syncing','success'].indexOf(phase);
    for(const [i,step] of [...writeView.querySelectorAll('[data-write-stage]')].entries()){step.dataset.state=index>i?'done':index===i?'active':'waiting';step.toggleAttribute('aria-current',index===i)}
  }
  function showWriteView(context){
    activeWrite=context;clearTimeout(progressTimer);clearInterval(elapsedTimer);
    const recent=current.organization.corrections?.find(r=>r.id===context.body.request_id),sku=context.result?.sku||context.body.new_sku||recent?.new_sku||'正在读取款号';
    const count=context.result?.capture_count||context.body.items?.length||recent?.capture_count||current.groups.find(r=>r.sku===context.body.sku)?.count;
    $('sku-pool-form').hidden=true;writeView.hidden=false;dialog.setAttribute('aria-labelledby','sku-write-title');dialog.setAttribute('aria-busy','true');
    $('sku-write-target').textContent=sku;$('sku-write-count').textContent=count?`${count} 张图片 · 正在核对文件`:'正在核对图片';$('sku-write-percent').textContent='准备中';$('sku-write-bar').removeAttribute('value');
    $('sku-write-errors').hidden=true;$('sku-write-errors').open=false;$('sku-write-actions').hidden=true;
    writePhase('preparing','正在准备图片','正在核对图片归属与文件资料，请稍候。');
    const started=performance.now();const clock=()=>{const seconds=Math.floor((performance.now()-started)/1000);$('sku-write-elapsed').textContent=`${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`};clock();elapsedTimer=setInterval(clock,1000);
    if(!dialog.open)dialog.showModal();writeView.focus();
  }
  async function waitForWrite(context){
    const ids=operationIds(context.result);if(!ids.length)return;
    writePhase('writing','正在写入 SKU 款号','正在将正确款号写入 JPG / RAW 图片资料。');
    for(;;){
      const results=await Promise.all(ids.map(id=>api('/api/metadata/operations/'+encodeURIComponent(id))));
      const jobs=results.flatMap(result=>result.jobs),done=jobs.filter(j=>j.status==='applied').length,pending=jobs.filter(j=>['queued','running'].includes(j.status)).length,failed=jobs.filter(j=>!['queued','running','applied'].includes(j.status));
      context.jobs=jobs;context.done=done;
      if(!jobs.length)throw new Error('尚未读取到写入文件，请继续查看进度');
      $('sku-write-bar').max=jobs.length;$('sku-write-bar').value=done;$('sku-write-count').textContent=`已写入 ${done} / ${jobs.length} 个文件${failed.length?' · '+failed.length+' 个未完成':''}`;$('sku-write-percent').textContent=`${Math.floor(done/jobs.length*100)}%`;
      if(!pending){
        if(failed.length){const error=new Error(`有 ${failed.length} 个文件未完成写入`);error.jobs=failed;throw error}
        return;
      }
      await new Promise(resolve=>setTimeout(resolve,900));
    }
  }
  async function finishWrite(context){
    if(!context.written){await waitForWrite(context);context.written=true}
    writePhase('syncing','正在同步款号清单','文件资料已写入，正在更新款号清单与文件路径视图。');
    const result=context.result;
    await library.refresh({sku:result.normalized,...(result.correction_id?{bin:result.bin_id}:{}),clearSearch:true,clearSelection:true});
    writePhase('success','SKU 款号已更新',`${result.capture_count} 张图片已归入正确款号，清单已同步。`);
    $('sku-write-percent').textContent='完成';clearInterval(elapsedTimer);
    if(!matchMedia('(prefers-reduced-motion: reduce)').matches)await new Promise(resolve=>setTimeout(resolve,500));
    dialog.close();resetWriteView();activeWrite=null;
    say(result.unchanged?'款号未变更':`已更新 ${result.sku} · ${result.capture_count} 张${context.done?' · 已写入 '+context.done+' 个文件':''}，款号清单和文件路径已同步`);
  }
  function writeError(error){
    const context=activeWrite;clearInterval(elapsedTimer);dialog.setAttribute('aria-busy','false');
    writePhase('failed',context?.written?'清单暂未同步':context?.result?'图片资料尚未全部写入':'款号更新暂未完成',error.message);
    const failures=error.jobs||[];$('sku-write-errors').hidden=!failures.length;
    $('sku-write-errors').querySelector('ul').replaceChildren(...failures.map(job=>{const li=document.createElement('li');li.textContent=job.path.split(/[\\/]/).at(-1)+' · '+(job.reason||job.status);return li}));
    context.retryWrites=failures.some(job=>job.status==='failed');
    $('sku-write-retry').hidden=!!failures.length&&!context.retryWrites;
    $('sku-write-retry').textContent=context.written?'重新同步清单':context.retryWrites?'重试失败写入':context.result?'继续查看进度':'重新提交';
    $('sku-write-actions').hidden=false;say(error.message,true);$('sku-write-retry').hidden?$('sku-write-close').focus():$('sku-write-retry').focus();
  }
  $('sku-write-close').onclick=()=>{if(!mutating){dialog.close();resetWriteView()}};
  $('sku-write-retry').onclick=async()=>{
    if(mutating||!activeWrite)return;const context=activeWrite;
    if(!context.result){action(context.body);return}
    setBusy(true);showWriteView(context);
    try{if(context.retryWrites)await Promise.all(operationIds(context.result).map(id=>api('/api/metadata/retry',{operation_id:id})));await finishWrite(context)}catch(error){writeError(error)}finally{setBusy(false)}
  };
  async function writeProgress(editInfo){
    const operations=Array.isArray(editInfo.operation_ids)?editInfo.operation_ids:JSON.parse(editInfo.operations||'[]');
    if(!operations.length)return;
    clearTimeout(progressTimer);
    try{
      const results=await Promise.all(operations.map(id=>api('/api/metadata/operations/'+encodeURIComponent(id))));
      const jobs=results.flatMap(result=>result.jobs),pending=jobs.filter(j=>['queued','running'].includes(j.status)).length,failed=jobs.filter(j=>j.status==='failed'),done=jobs.filter(j=>j.status==='applied').length;
      say(`${editInfo.sku||editInfo.new_sku} · ${editInfo.capture_count} 张 · 已写入 ${done} 个文件${pending?' · 待写入 '+pending:''}${failed.length?' · 失败 '+failed.length+'：'+failed.map(j=>j.path.split(/[\\/]/).at(-1)+' '+j.reason).join('；'):''}`,failed.length>0);
      if(pending&&editInfo.id&&!editInfo.old_key&&!mutating&&!dialog.open){const resume=document.createElement('button');resume.type='button';resume.className='bordered';resume.id='sku-write-resume';resume.textContent='查看更新进度';resume.onclick=()=>action({action:'correct_selection',request_id:editInfo.id});status.append(' ',resume)}
      if(pending)progressTimer=setTimeout(()=>writeProgress(editInfo),1500);
    }catch(error){say('款号已更新，写入状态读取失败：'+error.message,true)}
  }
  async function action(body){
    if(mutating)return;setBusy(true);say('正在保存…');
    const skuUpdate=body.action==='rename'||body.action==='correct_selection';
    if(skuUpdate)showWriteView({body,result:null,written:false});
    try{
      const result=await api('/api/sku/library/manage',body,skuUpdate?120000:20000);
      if(skuUpdate){
        activeWrite.result=result;watched.add(result.rename_id||result.correction_id);await finishWrite(activeWrite);
      }else if(body.action==='create_bin'){dialog.close();await library.selectBin(result.bin_id);say('已创建组 '+result.name)}
      else if(body.action==='rename_bin'){dialog.close();await library.refresh();say('组名称已更新')}
      else if(body.action==='delete_bin'){await library.selectBin(result.bin_id||'ungrouped');say('组已移除，SKU 已移回上级')}
      else {dialog.close();await library.refresh({sku:''});say(body.action==='remove'?'SKU 已移入“已移除”，可在那里恢复；原照片和元数据保留':body.action==='restore'?'SKU 已恢复到原分组':'SKU 已移入 '+(result.bin_id?groupName(result.bin_id):'未分组'))}
      return result;
    }catch(error){if(skuUpdate)writeError(error);else{$('sku-pool-editor-message').textContent=error.message;say(error.message,true)}}
    finally{setBusy(false)}
  }
  library.onData=data=>{
    current=data;current.organization||={bins:[],total:0,ungrouped:0,removed:0};paintBins();
    const recent=[...(current.organization.renames||[]),...(current.organization.corrections||[])].sort((a,b)=>b.updated-a.updated)[0];
    if(recent&&!watched.has(recent.id)&&!mutating){
      if(recent.status==='queued'){watched.add(recent.id);writeProgress(recent)}
      else if(!mutating&&!dialog.open&&(recent.status==='failed'||recent.status==='running'&&Date.now()/1000-recent.updated>120)){
        watched.add(recent.id);say('上次 SKU 更新未提交完整：'+(recent.error||'任务中断，可继续提交'),true);
        const retry=document.createElement('button');retry.id='sku-pool-retry';retry.className='bordered';retry.textContent='重试更新';retry.type='button';retry.onclick=()=>action(recent.old_key?{action:'rename',sku:recent.old_key,new_sku:recent.new_sku}:{action:'correct_selection',request_id:recent.id});status.append(' ',retry);
      }
    }
  };
  paintBins();
  $('sku-bin-create').onclick=()=>edit('create_bin');
  bins.addEventListener('click',event=>{const button=event.target.closest('[data-sku-bin]');if(!button||mutating)return;closeMenu();library.selectBin(button.dataset.skuBin).catch(error=>say(error.message,true))});
  page.addEventListener('dblclick',event=>{const button=event.target.closest('[data-db-sku]');if(!button)return;event.preventDefault();if(library.selection.bin==='trash'){say('请先恢复 SKU，再修改款号');return}const row=current.groups.find(g=>g.normalized===button.dataset.dbSku);if(row)edit('rename',row)});
  page.addEventListener('contextmenu',async event=>{
    const image=event.target.closest('#sku-database-grid .catalog-card[data-catalog-item]');
    if(!image&&!event.target.closest('[data-db-sku],[data-sku-bin]'))return;event.preventDefault();if(mutating)return;
    try{if(image)await library.selectCard(image.dataset.catalogItem,{preserve:true});openMenu(event.target,event.clientX,event.clientY)}catch(error){say(error.message,true)}
  });
  menu.addEventListener('click',event=>{
    const button=event.target.closest('[data-pool-action]');if(!button||!context||mutating)return;const chosen=context,kind=button.dataset.poolAction;closeMenu();
    if(['rename','move','rename_bin','correct_selection'].includes(kind)){edit(kind,chosen.item);return}
    if(kind==='reveal'){const item=chosen.item,body=['catalog','portable'].includes(item.source)?{item_id:item.catalog_item_id||item.id}:{session_id:item.session_id,capture_id:item.capture_id};api('/api/reveal-file',body).catch(error=>say(error.message,true));return}
    action({action:kind,...(chosen.kind==='bin'?{bin_id:chosen.item.id}:{sku:chosen.item.sku})});
  });
  page.addEventListener('keydown',event=>{
    const button=event.target.closest('[data-db-sku],[data-sku-bin],#sku-database-grid .catalog-card[data-catalog-item]');if(!button)return;
    if(event.key==='ContextMenu'||event.shiftKey&&event.key==='F10'){event.preventDefault();const box=button.getBoundingClientRect();if(button.dataset.catalogItem)library.selectCard(button.dataset.catalogItem,{preserve:true}).then(()=>openMenu(button,box.x,box.bottom));else openMenu(button,box.x,box.bottom)}
    else if(event.key==='F2'){event.preventDefault();const row=current.groups.find(g=>g.normalized===button.dataset.dbSku),folder=current.organization.bins.find(b=>b.id===button.dataset.skuBin);if(row&&library.selection.bin!=='trash')edit('rename',row);else if(folder)edit('rename_bin',folder)}
  });
  menu.addEventListener('keydown',event=>{if(!['ArrowDown','ArrowUp','Escape'].includes(event.key))return;event.preventDefault();if(event.key==='Escape'){closeMenu();return}const buttons=[...menu.querySelectorAll('button')],index=buttons.indexOf(document.activeElement);buttons[(index+(event.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length].focus()});
  page.addEventListener('dragstart',event=>{
    const button=event.target.closest('[data-db-sku][draggable=true]');if(!button||mutating||!event.dataTransfer)return;
    dragKey=button.dataset.dbSku;event.dataTransfer.setData('application/x-framekeep-sku',dragKey);event.dataTransfer.effectAllowed='move';button.classList.add('sku-dragging');
  });
  function clearDrag(){dragKey='';page.querySelectorAll('.sku-drop-target,.sku-dragging').forEach(el=>el.classList.remove('sku-drop-target','sku-dragging'))}
  bins.addEventListener('dragover',event=>{const target=event.target.closest('[data-sku-bin]');if(!target||!dragKey||!target.dataset.skuBin||target.dataset.skuBin==='trash')return;event.preventDefault();event.dataTransfer.dropEffect='move';bins.querySelectorAll('.sku-drop-target').forEach(el=>el.classList.remove('sku-drop-target'));target.classList.add('sku-drop-target')});
  bins.addEventListener('dragleave',event=>{const target=event.target.closest('[data-sku-bin]');if(target&&!target.contains(event.relatedTarget))target.classList.remove('sku-drop-target')});
  bins.addEventListener('drop',event=>{
    const target=event.target.closest('[data-sku-bin]'),key=event.dataTransfer?.getData('application/x-framekeep-sku');if(!target||!key||!dragKey||!target.dataset.skuBin||target.dataset.skuBin==='trash')return;event.preventDefault();
    const row=current.groups.find(r=>r.normalized===key),bin=target.dataset.skuBin;clearDrag();if(row)action({action:'move',sku:row.sku,bin_id:bin==='ungrouped'?'':bin});
  });
  page.addEventListener('dragend',clearDrag);
  $('sku-pool-form').addEventListener('submit',event=>{
    event.preventDefault();if(!editing||mutating)return;const {action:kind,item}=editing;
    action({action:kind,...(kind==='correct_selection'?{items:editing.items,request_id:editing.request_id,new_sku:$('sku-pool-name').value.trim()}:kind==='rename'?{sku:item.sku,new_sku:$('sku-pool-name').value.trim()}:kind==='move'?{sku:item.sku,bin_id:$('sku-pool-target').value}:kind==='rename_bin'?{bin_id:item.id,name:$('sku-pool-name').value.trim()}:{name:$('sku-pool-name').value.trim(),parent_id:$('sku-pool-target').value})});
  });
  for(const button of dialog.querySelectorAll('[data-pool-close]'))button.onclick=()=>{if(!mutating)dialog.close()};
  dialog.addEventListener('cancel',event=>{if(mutating)event.preventDefault()});
  document.addEventListener('pointerdown',event=>{if(!menu.contains(event.target))closeMenu()},true);
  document.addEventListener('framekeep:workspace',closeMenu);window.addEventListener('blur',closeMenu);
  window.addEventListener('pagehide',()=>{clearTimeout(progressTimer);clearInterval(elapsedTimer)},{once:true});
})();
