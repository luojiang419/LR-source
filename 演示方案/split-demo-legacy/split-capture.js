/* 隔离演示覆盖层：右侧使用正式选片组件，左侧只读参考资料。 */
const startLegacySplitDemo = async () => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const pin = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="m15 3 6 6-3 1-3 5v3l-3-3-5 3 1-5-3-3h3l5-3zM8 16l-5 5"/></svg>';
  const icon = (name) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">${name==='folder'?'<path d="M3 7V5h6l2 2h10v13H3z"/>':name==='server'?'<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6h1M7 17h1M14 7h4M14 18h4"/>':'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 14h18M7 17h2"/>'}</svg>`;
  const api = (path, body) => FramekeepIO.api('/api/split/'+path, body, 45000);
  const page = $('selection-page'), layout = page.querySelector('.library-layout'), surface = layout.querySelector('.preview-surface');
  let sources = [], scope = null, rows = [], offset = 0, active = null, config = null, requestId = 0, searchTimer;
  let references = [], referenceIndex = 0, referenceHovered = false, trayCollapsed = false, referenceSignature = '';
  const folderViews = new Map(), closedRoots = new Set();
  let zoom = 1, panX = 0, panY = 0, drag = null, paintedUrl = '', enabled = false, focusState = null, originalSides = null;
  const cacheKey = 'framekeep-split-references-v2';
  const referenceId = item => JSON.stringify([item.device||'',item.root||'',item.relative||item.url]);
  const referenceOf = item => item ? references.findIndex(row=>referenceId(row)===referenceId(item)) : -1;
  try {
    const saved = localStorage.getItem(cacheKey);
    if(saved){const data=JSON.parse(saved);references=(Array.isArray(data.references)?data.references:[]).filter(item=>item&&typeof item.name==='string'&&typeof item.device==='string'&&typeof item.root==='string'&&typeof item.relative==='string');referenceIndex=Math.max(0,Math.min(references.length-1,Number.isInteger(data.index)?data.index:0));trayCollapsed=data.collapsed===true;}
    else {const previous=JSON.parse(localStorage.getItem('framekeep-split-reference-v1')||'null');if(previous&&!previous.url&&typeof previous.relative==='string')references=[previous];}
  } catch {}
  const imageUrl = (item, size=1800) => '/api/split/library/preview?'+new URLSearchParams({device:item.device,root:item.root,file:item.relative,size});
  const call = async (route, params) => api('library/'+route+'?'+new URLSearchParams(params));
  const reference = document.createElement('section'); reference.id='split-reference'; reference.setAttribute('aria-label','参考图片工作区');
  reference.innerHTML=`<div class="split-resource-dock">
    <button id="split-resource-rail" class="panel-edge-toggle" type="button" aria-controls="split-resource-panel" aria-expanded="true" aria-label="收起参考资源"><span aria-hidden="true">‹</span></button>
    <aside id="split-resource-panel" class="split-tree-panel catalog-tree-panel"><header><strong>参考资源</strong><button id="split-add-root" aria-label="添加参考分区或文件夹" title="添加分区或文件夹">＋</button></header><div id="split-tree" class="catalog-tree" aria-label="参考资源目录"></div><footer><button id="split-api-settings">${icon('server')}设备与 API</button><span id="split-index-status">正在连接数据库…</span></footer></aside>
    <aside class="split-thumbnails"><header><strong>资源浏览</strong><button id="split-hide-resources" aria-label="收起参考资源面板" title="收起资源双栏">‹</button></header><div class="split-search"><input id="split-search" type="search" aria-label="搜索参考文件数据库" placeholder="搜索文件名或路径"><span id="split-search-scope">当前目录</span></div><div id="split-results" role="status"></div><div id="split-grid" aria-label="参考图片缩略图"></div><div class="split-pagination"><button id="split-prev-page">上一页</button><button id="split-next-page">下一页</button></div></aside>
  </div><main class="split-reference-main"><header class="split-preview-heading"><span><i></i>参考图片</span><div><button id="split-show-resources" aria-label="展开参考资源面板" title="展开参考资源">☷</button><button id="split-import" title="加载本机参考图片">加载图片</button><button id="split-reference-focus" aria-label="仅显示参考预览" title="收起或恢复参考侧模块">⛶</button></div></header>
  <div id="split-reference-stage" tabindex="0" aria-label="参考图片预览，滚轮缩放"><img id="split-reference-image" alt="参考图片" hidden><div id="split-reference-empty">选择一张参考图片<small>从资源库浏览，或加载本机图片</small></div><button id="split-pin-large" class="split-pin" aria-label="将当前大图固定为参考" aria-pressed="false" title="固定为参考">${pin}</button><span id="split-pin-state" hidden>已固定参考</span><button id="split-reference-prev" class="split-reference-switch" aria-label="上一张参考图">‹</button><button id="split-reference-next" class="split-reference-switch" aria-label="下一张参考图">›</button><button id="split-zoom-reset" title="重置参考图缩放">适应</button></div>
  <footer class="split-reference-caption"><strong id="split-reference-name">尚未选择参考</strong><span id="split-reference-source">只读浏览 · 不改动参考图片</span></footer>
  <details id="split-reference-tray" open><summary><span>参考图 <b id="split-reference-count">0</b></span><small>鼠标移入左侧后，使用 ← → 切换</small></summary><div id="split-reference-thumbnails" role="list" aria-label="已固定的参考图片"></div></details></main>`;
  page.prepend(reference);
  $('split-reference-tray').open=!trayCollapsed;
  const rightHeading = document.createElement('header'); rightHeading.className='split-live-heading';
  rightHeading.innerHTML='<span><i></i>当前拍摄</span><div><button id="split-right-folders" aria-label="折叠或展开拍摄缩略图">拍摄目录</button><button id="split-right-info" aria-label="折叠或展开当前拍摄信息">拍摄信息</button><button id="split-immersive" aria-pressed="false">沉浸对比</button><button id="split-exit" aria-label="退出分屏拍摄">×</button></div>';
  surface.before(rightHeading);
  const badge=document.createElement('span');badge.className='split-demo-badge';badge.textContent='隔离演示';document.querySelector('.identity').append(badge);
  const menu = document.createElement('div'); menu.id='split-context'; menu.className='photo-context-menu'; menu.role='menu';menu.hidden=true;
  menu.innerHTML=`<button role="menuitem" id="split-context-pin">${pin}设为参考</button>`;document.body.append(menu);
  let contextItem=null;
  function report(error) { Framekeep.notify(error.message || String(error)); }
  function saveReferences() {
    try {const saved=references.filter(item=>!item.url);const current=references[referenceIndex];const index=current?saved.findIndex(item=>referenceId(item)===referenceId(current)):-1;localStorage.setItem(cacheKey,JSON.stringify({references:saved,index:Math.max(0,index),collapsed:!$('split-reference-tray').open}));} catch {}
  }
  function shown() { return references[referenceIndex] || active; }
  function paintReferences() {
    const strip=$('split-reference-thumbnails'),scroll=strip.scrollLeft;
    $('split-reference-count').textContent=references.length;
    const signature=JSON.stringify(references.map(referenceId));
    if(signature!==referenceSignature){referenceSignature=signature;strip.innerHTML=references.length?references.map((item,index)=>`<div class="split-saved-reference ${index===referenceIndex?'active':''}" role="listitem"><button data-show-reference="${index}" aria-label="查看参考图 ${esc(item.name)}" aria-pressed="${index===referenceIndex}" title="${esc(item.name+' · '+(item.source||'本机加载'))}"><img src="${esc(item.url||imageUrl(item,240))}" alt="参考图 ${esc(item.name)}"><span>${esc(item.name)}</span></button><button class="split-remove-reference" data-remove-reference="${index}" aria-label="移除参考图 ${esc(item.name)}" title="移除参考">×</button></div>`).join(''):'<p class="split-reference-tray-empty">点击图片右上角图钉，将多张图片加入参考</p>';}
    for(const [index,card] of [...strip.querySelectorAll('.split-saved-reference')].entries()){card.classList.toggle('active',index===referenceIndex);card.querySelector('[data-show-reference]').setAttribute('aria-pressed',String(index===referenceIndex));}
    strip.scrollLeft=scroll;strip.querySelector('.active')?.scrollIntoView({block:'nearest',inline:'nearest'});
    $('split-reference-prev').disabled=referenceIndex<=0||references.length<2;
    $('split-reference-next').disabled=referenceIndex>=references.length-1||references.length<2;
    $('split-reference-stage').classList.toggle('has-multiple-references',references.length>1);
  }
  function addReference(item) {const index=referenceOf(item);if(index<0){references.push({...item});referenceIndex=references.length-1;}else referenceIndex=index;saveReferences();paintImage();}
  function removeReference(index) {const current=references[referenceIndex];references.splice(index,1);const retained=current?referenceOf(current):-1;referenceIndex=retained>=0?retained:Math.max(0,Math.min(index,references.length-1));saveReferences();paintImage();}
  function toggleReference(item) {if(!item)return;const index=referenceOf(item);if(index>=0)removeReference(index);else addReference(item);}
  function switchReference(step) {if(!references.length)return;referenceIndex=Math.max(0,Math.min(references.length-1,referenceIndex+step));saveReferences();paintImage();}
  $('split-reference-prev').onclick=()=>switchReference(-1);$('split-reference-next').onclick=()=>switchReference(1);
  $('split-reference-thumbnails').onclick=event=>{const remove=event.target.closest('[data-remove-reference]'),show=event.target.closest('[data-show-reference]');if(remove)removeReference(Number(remove.dataset.removeReference));else if(show){referenceIndex=Number(show.dataset.showReference);saveReferences();paintImage();}};
  $('split-reference-tray').addEventListener('toggle',()=>{saveReferences();window.dispatchEvent(new Event('resize'));});
  reference.addEventListener('pointerenter',()=>{referenceHovered=true;});reference.addEventListener('pointerleave',()=>{referenceHovered=false;});window.addEventListener('blur',()=>{referenceHovered=false;});
  document.addEventListener('keydown',event=>{
    if(!enabled||page.classList.contains('hidden')||!referenceHovered||!references.length||event.ctrlKey||event.metaKey||event.altKey||event.shiftKey||document.querySelector('dialog[open]')||/INPUT|TEXTAREA|SELECT/.test(event.target.tagName)||event.target.isContentEditable||!['ArrowLeft','ArrowRight'].includes(event.key))return;
    event.preventDefault();event.stopImmediatePropagation();switchReference(event.key==='ArrowRight'?1:-1);
  },true);
  function paintImage() {
    const item=shown(),pinned=referenceOf(item)>=0,image=$('split-reference-image'),nextUrl=item&&(item.url||imageUrl(item));
    if(nextUrl!==paintedUrl){paintedUrl=nextUrl;zoom=1;panX=panY=0;image.style.transform='scale(1)';}
    image.hidden=!item;$('split-reference-empty').hidden=!!item;$('split-pin-large').disabled=!item;
    if(item){if(image.getAttribute('src')!==nextUrl)image.src=nextUrl;image.alt=item.name;$('split-reference-name').textContent=item.name;$('split-reference-source').textContent=(pinned?'固定参考 · ':'正在浏览 · ')+(item.source||'本机加载');}
    $('split-pin-large').setAttribute('aria-pressed',String(!!pinned));$('split-pin-large').setAttribute('aria-label',pinned?'取消固定参考':'将当前大图固定为参考');$('split-pin-large').title=pinned?'取消固定参考':'固定为参考';$('split-pin-state').hidden=!pinned;$('split-zoom-reset').textContent=zoom===1?'适应':Math.round(zoom*100)+'%';
    $('split-pin-state').textContent=`参考 ${referenceIndex+1} / ${references.length}`;
    for(const card of $('split-grid').querySelectorAll('[data-row]')){const item=rows[Number(card.dataset.row)],saved=referenceOf({...item,device:scope?.device,root:scope?.root})>=0;card.classList.toggle('is-pinned',saved);const button=card.querySelector('[data-pin-row]');button?.setAttribute('aria-pressed',String(saved));button?.setAttribute('aria-label',(saved?'取消固定 ':'固定 ')+item.name+(saved?'':' 为参考'));}
    paintReferences();
  }
  function choose(index, makePin=false) {
    const row=rows[index];if(!row||!scope)return;
    active={...row,device:scope.device,root:scope.root,source:scope.label};
    if(makePin){addReference(active);}
    for(const card of $('split-grid').querySelectorAll('[data-row]'))card.classList.toggle('active',Number(card.dataset.row)===index);
    paintImage();
  }
  $('split-pin-large').onclick=()=>toggleReference(shown());
  $('split-reference-image').onerror=()=>{$('split-reference-empty').hidden=false;$('split-reference-empty').textContent='参考图片不可用，请检查设备连接或重新选择';};
  $('split-reference-image').onload=()=>{$('split-reference-empty').hidden=true;};
  function paintZoom(){if(zoom===1)panX=panY=0;$('split-reference-image').style.transform=`translate(${panX}px,${panY}px) scale(${zoom})`;$('split-zoom-reset').textContent=zoom===1?'适应':Math.round(zoom*100)+'%';}
  $('split-reference-stage').addEventListener('wheel',event=>{if(!shown())return;event.preventDefault();zoom=Math.max(1,Math.min(8,zoom*(event.deltaY>0?.9:1.1)));paintZoom();},{passive:false});
  $('split-reference-stage').onpointerdown=event=>{if(zoom<=1||event.target.closest('button'))return;drag={x:event.clientX-panX,y:event.clientY-panY};event.currentTarget.setPointerCapture(event.pointerId);};
  $('split-reference-stage').onpointermove=event=>{if(!drag)return;const box=event.currentTarget.getBoundingClientRect();panX=Math.max(-box.width*(zoom-1)/2,Math.min(box.width*(zoom-1)/2,event.clientX-drag.x));panY=Math.max(-box.height*(zoom-1)/2,Math.min(box.height*(zoom-1)/2,event.clientY-drag.y));paintZoom();};
  $('split-reference-stage').onpointerup=()=>{drag=null;};$('split-reference-stage').onpointercancel=()=>{drag=null;};
  $('split-zoom-reset').onclick=()=>{zoom=1;paintZoom();};
  const input=document.createElement('input');input.type='file';input.accept='image/*';input.multiple=true;input.hidden=true;document.body.append(input);
  const objectUrls=new Set();
  $('split-import').onclick=()=>input.click();
  input.onchange=()=>{for(const file of input.files){if(!file.type.startsWith('image/'))continue;const url=URL.createObjectURL(file);objectUrls.add(url);active={name:file.name,url,source:'本机加载'};addReference(active);}input.value='';};
  window.addEventListener('pagehide',()=>{for(const url of objectUrls)URL.revokeObjectURL(url);},{once:true});
  $('split-grid').onclick=event=>{const card=event.target.closest('[data-row]');if(card){choose(Number(card.dataset.row));if(event.target.closest('[data-pin-row]'))toggleReference(active);}};
  $('split-grid').addEventListener('keydown',event=>{const card=event.target.closest('[data-row]');if(card&&['Enter',' '].includes(event.key)&&!event.target.closest('[data-pin-row]')){event.preventDefault();choose(Number(card.dataset.row));}});
  $('split-grid').oncontextmenu=event=>{const card=event.target.closest('[data-row]');if(!card)return;event.preventDefault();contextItem=Number(card.dataset.row);menu.hidden=false;menu.style.left=Math.min(innerWidth-180,event.clientX)+'px';menu.style.top=Math.min(innerHeight-50,event.clientY)+'px';$('split-context-pin').focus();};
  $('split-context-pin').onclick=()=>{choose(contextItem,true);menu.hidden=true;};
  document.addEventListener('pointerdown',event=>{if(!menu.contains(event.target))menu.hidden=true;});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!menu.hidden){event.stopImmediatePropagation();menu.hidden=true;}},true);
  async function loadItems() {
    if(!scope)return;
    const sequence=++requestId,chosen={...scope},text=$('split-search').value.trim();
    $('split-results').textContent='正在检索…';
    try {
      const result=await call('items',{device:chosen.device,root:chosen.root,folder:chosen.folder,q:text,offset});
      if(sequence!==requestId)return;
      rows=result.rows;
      $('split-results').textContent=`${result.total} 张图片 · ${result.elapsed_ms} ms`;
      $('split-search-scope').textContent=text?'当前目录及子目录 · '+result.engine:'当前目录 · '+result.engine;
      $('split-search-scope').title=result.note;
      $('split-grid').innerHTML=rows.length?rows.map((row,index)=>`<article class="split-resource-card" data-row="${index}" tabindex="0" role="button" aria-label="浏览参考 ${esc(row.name)}"><div><img src="${esc(imageUrl({...row,device:chosen.device,root:chosen.root},420))}" alt="${esc(row.name)}" loading="lazy"><button class="split-pin" data-pin-row="${index}" aria-label="固定 ${esc(row.name)} 为参考" title="设为参考">${pin}</button><span class="split-card-pinned">已固定</span></div><span title="${esc(row.relative)}">${esc(row.name)}</span></article>`).join(''):'<p class="split-empty">此目录没有匹配图片<br>可展开下级目录，或更改搜索</p>';
      $('split-prev-page').disabled=offset===0;$('split-next-page').disabled=!result.has_more;
      if(!active&&rows.length)choose(0);else paintImage();
    } catch(error) { if(sequence===requestId){$('split-results').textContent='检索失败';$('split-grid').textContent=error.message;} }
  }
  async function selectFolder(device,root,folder,label) {
    scope={device,root,folder,label};offset=0;
    $('split-search').value='';
    for(const button of $('split-tree').querySelectorAll('[data-resource-folder]'))button.classList.toggle('active',button.dataset.device===device&&button.dataset.root===root&&button.dataset.folder===folder);
    await loadItems();
  }
  const rootKey=(device,root)=>JSON.stringify([device,root]);
  function folderLabel(device,root){const source=sources.find(row=>row.id===device),item=source?.roots.find(row=>row.id===root);return (source?.name||'参考资源')+' / '+(item?.name||'共享目录');}
  function paintDirectory(device,root) {
    const key=rootKey(device,root),view=folderViews.get(key),group=[...$('split-tree').querySelectorAll('[data-resource-group]')].find(node=>node.dataset.resourceGroup===key);
    if(!view||!group)return;
    const attributes=`data-device="${esc(device)}" data-root="${esc(root)}"`;
    group.querySelector('.catalog-drive-folders').innerHTML=(view.folder?`<button class="split-resource-up" data-resource-up ${attributes} title="${esc(view.folder)}" aria-label="返回 ${esc(view.folder)} 的上级目录">‹ <span>${esc(view.folder)}</span></button>`:'')+view.children.map(row=>{
      const selected=scope?.device===device&&scope?.root===root&&(scope.folder===row.folder||scope.folder.startsWith(row.folder+'/'));
      return `<button class="split-resource-folder-row ${selected?'active':''}" data-resource-folder ${attributes} data-folder="${esc(row.folder)}" title="${esc(row.folder)}" aria-selected="${selected}"><span class="split-folder-chevron">›</span><span>${esc(row.name)}</span></button>`;
    }).join('');
  }
  async function loadDirectory(device,root,folder='') {
    const key=rootKey(device,root),view=folderViews.get(key)||{folder:'',children:[],sequence:0};folderViews.set(key,view);
    const sequence=++view.sequence,result=await call('folders',{device,root,folder});
    if(view.sequence!==sequence)return;
    view.folder=folder;view.children=result.folders;paintDirectory(device,root);
  }
  async function loadSources() {
    const result=await api('sources');sources=result.devices;
    $('split-tree').innerHTML=sources.map(device=>device.error?`<p class="split-empty">${esc(device.name+' · '+device.error)}</p>`:device.roots.map(root=>{const key=rootKey(device.id,root.id),open=!closedRoots.has(key);return `<section class="catalog-drive-group split-resource-group" data-resource-group="${esc(key)}"><button class="catalog-drive-button" data-resource-root data-device="${esc(device.id)}" data-root="${esc(root.id)}" aria-label="${esc(root.name)}" aria-expanded="${open}" title="${esc(device.name+' / '+root.name+(device.remote?' · API 连接':''))}">${icon(device.remote?'server':'disk')}<span>${esc(root.name)}</span><small>${root.count||0}</small></button>${device.remote?`<small class="split-resource-origin">API · ${esc(device.name)}</small>`:''}<div class="catalog-drive-folders" ${open?'':'hidden'}></div></section>`;}).join('')).join('');
    await Promise.all(sources.flatMap(device=>(device.roots||[]).map(root=>loadDirectory(device.id,root.id,folderViews.get(rootKey(device.id,root.id))?.folder||'').catch(error=>{const group=[...$('split-tree').querySelectorAll('[data-resource-group]')].find(node=>node.dataset.resourceGroup===rootKey(device.id,root.id));if(group)group.querySelector('.catalog-drive-folders').textContent=error.message;}))));
  }
  $('split-tree').onclick=async event=>{
    const toggle=event.target.closest('[data-resource-root]'),select=event.target.closest('[data-resource-folder]'),up=event.target.closest('[data-resource-up]');
    try {
      if(toggle){const d=toggle.dataset,key=rootKey(d.device,d.root),open=toggle.getAttribute('aria-expanded')!=='true';toggle.setAttribute('aria-expanded',String(open));toggle.closest('.split-resource-group').querySelector('.catalog-drive-folders').hidden=!open;if(open){closedRoots.delete(key);if(scope?.device!==d.device||scope?.root!==d.root)await selectFolder(d.device,d.root,folderViews.get(key)?.folder||'',folderLabel(d.device,d.root));}else closedRoots.add(key);}
      else if(up){const d=up.dataset,folder=folderViews.get(rootKey(d.device,d.root)).folder.split('/').slice(0,-1).join('/');await Promise.all([loadDirectory(d.device,d.root,folder),selectFolder(d.device,d.root,folder,folderLabel(d.device,d.root))]);}
      else if(select){const d=select.dataset,view=folderViews.get(rootKey(d.device,d.root)),sequence=++view.sequence;await selectFolder(d.device,d.root,d.folder,folderLabel(d.device,d.root));const result=await call('folders',{device:d.device,root:d.root,folder:d.folder});if(view.sequence!==sequence)return;if(result.folders.length){view.folder=d.folder;view.children=result.folders;}paintDirectory(d.device,d.root);}
    } catch(error) { report(error); }
  };
  $('split-search').oninput=()=>{offset=0;clearTimeout(searchTimer);searchTimer=setTimeout(loadItems,180);};
  $('split-search').onkeydown=event=>{event.stopPropagation();if(event.key==='Enter'){clearTimeout(searchTimer);loadItems();}};
  $('split-prev-page').onclick=()=>{offset=Math.max(0,offset-60);loadItems();};$('split-next-page').onclick=()=>{offset+=60;loadItems();};
  function syncResourceControls(){const open=!reference.classList.contains('resources-collapsed')&&!reference.classList.contains('tree-collapsed'),rail=$('split-resource-rail');rail.setAttribute('aria-expanded',String(open));rail.setAttribute('aria-label',open?'收起参考资源':'展开参考资源');rail.title=open?'收起参考资源':'展开参考资源';rail.firstElementChild.textContent=open?'‹':'›';$('split-show-resources').setAttribute('aria-expanded',String(!reference.classList.contains('resources-collapsed')));window.dispatchEvent(new Event('resize'));}
  function resources(open) {reference.classList.toggle('resources-collapsed',!open);if(open)reference.classList.remove('tree-collapsed','thumbs-collapsed');syncResourceControls();}
  for(const [name,label] of [['tree','目录'],['thumbs','素材']]){const button=document.createElement('button');button.textContent=label;button.id='split-toggle-'+name;button.setAttribute('aria-label','折叠或展开参考'+label);$('split-show-resources').before(button);button.onclick=()=>{if(reference.classList.contains('resources-collapsed'))resources(true);else {reference.classList.toggle(name+'-collapsed');syncResourceControls();}};}
  $('split-resource-rail').onclick=()=>$('split-toggle-tree').click();
  $('split-hide-resources').onclick=()=>resources(false);$('split-show-resources').onclick=()=>resources(reference.classList.contains('resources-collapsed'));
  $('split-reference-focus').onclick=()=>resources(reference.classList.contains('resources-collapsed'));
  $('split-right-folders').onclick=()=>$('layout-left').click();$('split-right-info').onclick=()=>$('layout-right').click();
  function immersive() {
    if(focusState){for(const flag of ['resources-collapsed','tree-collapsed','thumbs-collapsed'])reference.classList.toggle(flag,focusState.flags.includes(flag));$('split-reference-tray').open=focusState.tray;syncResourceControls();for(const side of ['left','right'])if(page.classList.contains(side+'-collapsed')!==focusState[side])$('layout-'+side).click();focusState=null;}
    else {focusState={flags:[...reference.classList].filter(flag=>flag.endsWith('-collapsed')),tray:$('split-reference-tray').open,left:page.classList.contains('left-collapsed'),right:page.classList.contains('right-collapsed')};resources(false);$('split-reference-tray').open=false;for(const side of ['left','right'])if(!page.classList.contains(side+'-collapsed'))$('layout-'+side).click();}
    $('split-immersive').setAttribute('aria-pressed',String(!!focusState));$('split-immersive').textContent=focusState?'恢复面板':'沉浸对比';window.dispatchEvent(new Event('resize'));
  }
  $('split-immersive').onclick=immersive;
  function toggleSplit(value) {
    enabled=value;page.classList.toggle('split-enabled',value);$('layout-split').setAttribute('aria-pressed',String(value));$('layout-split').setAttribute('aria-checked',String(value));
    if(value){originalSides={left:page.classList.contains('left-collapsed'),right:page.classList.contains('right-collapsed')};if(!originalSides.right)$('layout-right').click();const first=Framekeep.photoItems()[0]?.photo;if(first)Framekeep.browsePhoto(first.id);}
    else {if(focusState)immersive();if(originalSides)for(const side of ['left','right'])if(page.classList.contains(side+'-collapsed')!==originalSides[side])$('layout-'+side).click();}
    window.dispatchEvent(new Event('resize'));
  }
  $('layout-split').onclick=()=>toggleSplit(!enabled);$('split-exit').onclick=()=>toggleSplit(false);
  // 将提示归属于实际预览容器；折叠、拖动栏宽和分屏时无需计算全窗中心。
  const toast=$('toast');
  function anchorToast(){const visible=!page.classList.contains('hidden');toast.classList.toggle('preview-anchored',visible);(visible?surface:document.body).append(toast);}
  new MutationObserver(anchorToast).observe(page,{attributes:true,attributeFilter:['class']});anchorToast();

  const settings=$('settings-library');
  settings.innerHTML=`<div class="settings-section-heading"><h3>文件数据库与 API</h3><p>把参考资料留在原设备，通过局域网只读检索和浏览。</p></div>
    <div class="split-service-note"><span class="split-online-dot"></span><div><strong>真实双服务演示</strong><p>两套独立数据库通过 API Key 通信；当前仅监听本机回环地址。</p></div></div>
    <section class="split-settings-card"><header><div><h4>本机文件数据库</h4><p>添加允许浏览的分区或文件夹，自动尝试 Everything 索引。</p></div><button id="split-settings-add-root">＋ 添加分区</button></header><div id="split-shared-roots"></div><p id="split-everything-status" class="split-muted"></p></section>
    <section class="split-settings-card"><header><div><h4>API Key</h4><p>按调用方命名和分配目录，可随时查看、复制或停用。</p></div><button id="split-create-key" class="primary">创建密钥</button></header><div class="split-endpoint"><span>本机只读接口</span><code id="split-local-endpoint"></code></div><div id="split-key-list"></div></section>
    <section class="split-settings-card"><header><div><h4>调用其他设备的文件数据库</h4><p>输入对方 IP + 端口，自动补齐前缀与接口后缀。</p></div></header><form id="split-peer-form"><div class="split-form-grid"><label>设备名称<input id="split-peer-name" placeholder="例如：摄影工作站 B" required maxlength="48"></label><label>IP + 端口<input id="split-peer-address" placeholder="192.168.1.23:8793" required></label><label class="split-full">对方 API Key<input id="split-peer-key" type="password" placeholder="fk_…" required autocomplete="off"></label></div><p id="split-address-preview" class="split-muted">http://IP:端口/api/library/v1</p><div class="split-form-actions"><span id="split-peer-message" role="status"></span><button type="submit" class="primary">连接并添加设备</button></div></form><div id="split-peer-list"></div></section>`;
  function showSettings(){ $('app-settings-open').click();document.querySelector('[data-settings-section="library"]').click();refreshSettings().catch(report); }
  $('split-api-settings').onclick=showSettings;
  const dialog=document.createElement('dialog');dialog.id='split-manage-dialog';document.body.append(dialog);
  function openDialog(title,content,handler){dialog.innerHTML=`<div class="dialog-heading"><h2>${title}</h2><button type="button" id="split-dialog-close" aria-label="关闭资源配置">×</button></div><form id="split-manage-form">${content}<p id="split-manage-message" role="status"></p><div class="dialog-actions"><button type="button" id="split-dialog-cancel">取消</button><button class="primary" type="submit">${title}</button></div></form>`;for(const id of ['split-dialog-close','split-dialog-cancel'])$(id).onclick=()=>dialog.close();$('split-manage-form').onsubmit=async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;try{await handler();}catch(error){$('split-manage-message').textContent=error.message;}finally{button.disabled=false;}};dialog.showModal();}
  function addRoot(){openDialog('添加参考分区', '<label>显示名称<input id="split-root-name" placeholder="例如：秋冬参考盘" maxlength="48"></label><label>分区或文件夹路径<input id="split-root-path" placeholder="例如：G:\\参考资料" required></label><p class="split-muted">只读索引此目录；Everything 不可用时使用目录数据库。本演示最多索引 20,000 张图片。</p>',async()=>{await api('add-root',{path:$('split-root-path').value,name:$('split-root-name').value});dialog.close();await Promise.all([loadSources(),refreshSettings()]);});}
  $('split-add-root').onclick=addRoot;$('split-settings-add-root').onclick=addRoot;
  async function refreshSettings(){
    config=await api('config');
    $('split-shared-roots').innerHTML=config.roots.map(root=>`<div class="split-config-row"><span>${icon('folder')}<strong>${esc(root.name)}</strong><small>${root.count||0} 张${root.limited?' · 已达演示上限':''}</small></span><code>${esc(root.path)}</code></div>`).join('');
    $('split-everything-status').textContent=config.everything.available?'Everything 已连接 · 添加目录和检索时自动调用':'Everything 未就绪 · '+config.everything.message;
    $('split-index-status').textContent=config.everything.available?'Everything 已连接':'目录索引已就绪';
    $('split-local-endpoint').textContent=config.endpoint;
    $('split-key-list').innerHTML=config.keys.length?config.keys.map(key=>`<div class="split-key-row ${key.disabled?'disabled':''}"><div><strong>${esc(key.name)}</strong><span><code>${esc(key.prefix)}••••••</code> · ${key.disabled?'已停用':key.roots.length+' 个共享目录 · 只读'}</span></div><div><button data-reveal-key="${key.id}" ${key.disabled?'disabled':''}>查看</button><button data-copy-key="${key.id}" ${key.disabled?'disabled':''}>复制</button><button data-disable-key="${key.id}" ${key.disabled?'disabled':''}>停用</button></div></div>`).join(''):'<p class="split-empty">尚未创建 API Key，点击右上角创建。</p>';
    $('split-peer-list').innerHTML=config.peers.map(peer=>`<div class="split-config-row"><span>${icon('server')}<strong>${esc(peer.name)}</strong></span><code>${esc(peer.address)}</code><button data-remove-peer="${peer.id}">断开</button></div>`).join('');
  }
  $('split-create-key').onclick=async()=>{await refreshSettings();openDialog('创建 API 密钥',`<label>分配给调用方<input id="split-key-name" maxlength="48" placeholder="例如：摄影工作站 B" required></label><fieldset><legend>允许调用的目录 · 只读</legend>${config.roots.map(root=>`<label class="split-checkbox"><input type="checkbox" name="key-root" value="${root.id}" checked>${esc(root.name)}</label>`).join('')}</fieldset>`,async()=>{const result=await api('create-key',{name:$('split-key-name').value,roots:[...dialog.querySelectorAll('[name=key-root]:checked')].map(el=>el.value)});dialog.close();await refreshSettings();showKey(result.api_key);});};
  function showKey(value){dialog.innerHTML=`<div class="dialog-heading"><h2>API 密钥</h2><button id="split-key-done" aria-label="关闭密钥查看">×</button></div><p class="split-muted">可分配给指定调用方；之后仍可在本机查看。仅具有分配目录的读取权限。</p><code class="split-secret-value">${esc(value)}</code><div class="dialog-actions"><button id="split-secret-copy" class="primary">复制密钥</button></div>`;$('split-key-done').onclick=()=>dialog.close();$('split-secret-copy').onclick=async()=>{try{await navigator.clipboard.writeText(value);$('split-secret-copy').textContent='已复制';}catch{dialog.querySelector('.split-secret-value').focus();}};dialog.showModal();}
  settings.addEventListener('click',async event=>{const button=event.target.closest('[data-reveal-key],[data-copy-key],[data-disable-key],[data-remove-peer]');if(!button)return;button.disabled=true;try{
    if(button.dataset.revealKey||button.dataset.copyKey){const result=await api('reveal-key',{id:button.dataset.revealKey||button.dataset.copyKey});if(button.dataset.copyKey){await navigator.clipboard.writeText(result.api_key);button.textContent='已复制';}else showKey(result.api_key);}
    else if(button.dataset.disableKey){await api('disable-key',{id:button.dataset.disableKey});await refreshSettings();}
    else {await api('remove-peer',{id:button.dataset.removePeer});await Promise.all([refreshSettings(),loadSources()]);}
  }catch(error){report(error);}finally{if(button.isConnected)button.disabled=false;}});
  $('split-peer-address').oninput=()=>{let value=$('split-peer-address').value.trim().replace(/^https?:\/\//,'').replace(/\/api\/library\/v1\/?$/,'').replace(/\/$/,'');$('split-address-preview').textContent=value?'http://'+value+(!value.includes(':')?':8793':'')+'/api/library/v1':'http://IP:端口/api/library/v1';};
  $('split-peer-form').onsubmit=async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;$('split-peer-message').textContent='正在验证密钥并读取共享目录…';try{await api('add-peer',{name:$('split-peer-name').value,address:$('split-peer-address').value,api_key:$('split-peer-key').value});$('split-peer-message').textContent='已连接，目录已加入参考资源栏';$('split-peer-key').value='';await Promise.all([refreshSettings(),loadSources()]);}catch(error){$('split-peer-message').textContent=error.message;}finally{button.disabled=false;}};
  document.querySelector('[data-settings-section="library"]').addEventListener('click',()=>refreshSettings().catch(report));
  try {
    await Promise.all([loadSources(),refreshSettings()]);
    const device=sources.find(d=>d.id==='local'),root=device?.roots[0];
    if(root){await selectFolder('local',root.id,'秋冬棚拍/造型参考','本机参考盘 / 秋冬棚拍 / 造型参考');await loadDirectory('local',root.id,'秋冬棚拍');}
    toggleSplit(true);paintImage();saveReferences();syncResourceControls();
  }catch(error){report(error);}
 };
if(window.FramekeepBoot?.loading)document.addEventListener('framekeep:boot-ready',startLegacySplitDemo,{once:true});
else if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',startLegacySplitDemo,{once:true});
else startLegacySplitDemo();
