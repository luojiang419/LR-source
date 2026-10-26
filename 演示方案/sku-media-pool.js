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
  const menu=document.createElement('div');menu.id='sku-pool-menu';menu.className='photo-context-menu';menu.setAttribute('role','menu');menu.hidden=true;document.body.append(menu);
  let current={groups:[],organization:{bins:[],total:0,ungrouped:0,removed:0}},editing=null,context=null,mutating=false,dragKey='',treeSnapshot='',progressTimer=null;
  const watched=new Set();
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
    const sku=target.closest('[data-db-sku]'),folder=target.closest('[data-sku-bin]');
    if(sku){
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
    if(mutating)return;editing={action,item};$('sku-pool-editor-message').textContent='';
    const rename=action==='rename',move=action==='move';
    $('sku-pool-editor-title').textContent=rename?'修改 SKU 款号':move?'将 SKU 移到组':action==='rename_bin'?'重命名组':'新建 SKU 组';
    $('sku-pool-editor-help').textContent=rename?`${item.sku} · ${item.count} 张。保存后自动更新该款全部图片及已登记 RAW / JPG 的 SKU 元数据。`:move?'分组用于软件内管理，图片路径和元数据保留。':'创建用于整理 SKU 的软件内文件夹。';
    $('sku-pool-name-label').hidden=move;$('sku-pool-name').required=!move;$('sku-pool-name').maxLength=rename?80:100;$('sku-pool-name').value=rename?item.sku:action==='rename_bin'?item.name:'';$('sku-pool-name-caption').textContent=rename?'SKU 款号':'组名称';
    $('sku-pool-target-label').hidden=!move&&action!=='create_bin';
    $('sku-pool-target').innerHTML='<option value="">'+(move?'未分组':'顶层组')+'</option>'+current.organization.bins.map(b=>`<option value="${esc(b.id)}">${esc(b.name)}</option>`).join('');
    $('sku-pool-target').value=current.organization.bins.some(b=>b.id===library.selection.bin)?library.selection.bin:'';
    $('sku-pool-submit').textContent=rename?'保存并更新整款':move?'移动':'保存';dialog.showModal();if(move)$('sku-pool-target').focus();else{$('sku-pool-name').focus();$('sku-pool-name').select()}
  }
  function setBusy(value){mutating=value;$('sku-pool-submit').disabled=value;$('sku-pool-name').disabled=value;$('sku-pool-target').disabled=value}
  async function writeProgress(editInfo){
    const operations=Array.isArray(editInfo.operation_ids)?editInfo.operation_ids:JSON.parse(editInfo.operations||'[]');
    if(!operations.length)return;
    clearTimeout(progressTimer);
    try{
      const results=await Promise.all(operations.map(id=>api('/api/metadata/operations/'+encodeURIComponent(id))));
      const jobs=results.flatMap(result=>result.jobs),pending=jobs.filter(j=>['queued','running'].includes(j.status)).length,failed=jobs.filter(j=>j.status==='failed'),done=jobs.filter(j=>j.status==='applied').length;
      say(`${editInfo.sku||editInfo.new_sku} · ${editInfo.capture_count} 张 · 已写入 ${done} 个文件${pending?' · 待写入 '+pending:''}${failed.length?' · 失败 '+failed.length+'：'+failed.map(j=>j.path.split(/[\\/]/).at(-1)+' '+j.reason).join('；'):''}`,failed.length>0);
      if(pending)progressTimer=setTimeout(()=>writeProgress(editInfo),1500);
    }catch(error){say('款号已更新，写入状态读取失败：'+error.message,true)}
  }
  async function action(body){
    if(mutating)return;setBusy(true);say('正在保存…');
    try{
      const result=await api('/api/sku/library/manage',body);
      if(body.action==='rename'){
        dialog.close();watched.add(result.rename_id);await library.refresh({sku:result.normalized,clearSearch:true});say(result.unchanged?'款号未变更':`已更新 ${result.sku} · ${result.capture_count} 张，元数据写入已排队`);if(!result.unchanged)writeProgress(result);
      }else if(body.action==='create_bin'){dialog.close();await library.selectBin(result.bin_id);say('已创建组 '+result.name)}
      else if(body.action==='rename_bin'){dialog.close();await library.refresh();say('组名称已更新')}
      else if(body.action==='delete_bin'){await library.selectBin(result.bin_id||'ungrouped');say('组已移除，SKU 已移回上级')}
      else {dialog.close();await library.refresh({sku:''});say(body.action==='remove'?'SKU 已移入“已移除”，可在那里恢复；原照片和元数据保留':body.action==='restore'?'SKU 已恢复到原分组':'SKU 已移入 '+(result.bin_id?groupName(result.bin_id):'未分组'))}
      return result;
    }catch(error){$('sku-pool-editor-message').textContent=error.message;say(error.message,true)}
    finally{setBusy(false)}
  }
  library.onData=data=>{
    current=data;current.organization||={bins:[],total:0,ungrouped:0,removed:0};paintBins();
    const recent=current.organization.renames?.[0];
    if(recent&&!watched.has(recent.id)){
      if(recent.status==='queued'){watched.add(recent.id);writeProgress(recent)}
      else if(!mutating&&!dialog.open&&(recent.status==='failed'||recent.status==='running'&&Date.now()/1000-recent.updated>120)){
        watched.add(recent.id);say('上次 SKU 更新未提交完整：'+(recent.error||'任务中断，可继续提交'),true);
        const retry=document.createElement('button');retry.id='sku-pool-retry';retry.className='bordered';retry.textContent='重试更新';retry.type='button';retry.onclick=()=>action({action:'rename',sku:recent.old_key,new_sku:recent.new_sku});status.append(' ',retry);
      }
    }
  };
  paintBins();
  $('sku-bin-create').onclick=()=>edit('create_bin');
  bins.addEventListener('click',event=>{const button=event.target.closest('[data-sku-bin]');if(!button||mutating)return;closeMenu();library.selectBin(button.dataset.skuBin).catch(error=>say(error.message,true))});
  page.addEventListener('dblclick',event=>{const button=event.target.closest('[data-db-sku]');if(!button)return;event.preventDefault();if(library.selection.bin==='trash'){say('请先恢复 SKU，再修改款号');return}const row=current.groups.find(g=>g.normalized===button.dataset.dbSku);if(row)edit('rename',row)});
  page.addEventListener('contextmenu',event=>{if(!event.target.closest('[data-db-sku],[data-sku-bin]'))return;event.preventDefault();openMenu(event.target,event.clientX,event.clientY)});
  menu.addEventListener('click',event=>{
    const button=event.target.closest('[data-pool-action]');if(!button||!context||mutating)return;const chosen=context,kind=button.dataset.poolAction;closeMenu();
    if(['rename','move','rename_bin'].includes(kind)){edit(kind,chosen.item);return}
    action({action:kind,...(chosen.kind==='bin'?{bin_id:chosen.item.id}:{sku:chosen.item.sku})});
  });
  page.addEventListener('keydown',event=>{
    const button=event.target.closest('[data-db-sku],[data-sku-bin]');if(!button)return;
    if(event.key==='ContextMenu'||event.shiftKey&&event.key==='F10'){event.preventDefault();const box=button.getBoundingClientRect();openMenu(button,box.x,box.bottom)}
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
    action({action:kind,...(kind==='rename'?{sku:item.sku,new_sku:$('sku-pool-name').value.trim()}:kind==='move'?{sku:item.sku,bin_id:$('sku-pool-target').value}:kind==='rename_bin'?{bin_id:item.id,name:$('sku-pool-name').value.trim()}:{name:$('sku-pool-name').value.trim(),parent_id:$('sku-pool-target').value})});
  });
  for(const button of dialog.querySelectorAll('[data-pool-close]'))button.onclick=()=>{if(!mutating)dialog.close()};
  dialog.addEventListener('cancel',event=>{if(mutating)event.preventDefault()});
  document.addEventListener('pointerdown',event=>{if(!menu.contains(event.target))closeMenu()},true);
  document.addEventListener('framekeep:workspace',closeMenu);window.addEventListener('blur',closeMenu);
  window.addEventListener('pagehide',()=>clearTimeout(progressTimer),{once:true});
})();
