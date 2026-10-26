/* 分屏拍摄：正式选片组件、持久参考集合与只读局域网资源。 */
document.addEventListener('DOMContentLoaded', async () => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const pin = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="m15 3 6 6-3 1-3 5v3l-3-3-5 3 1-5-3-3h3l5-3zM8 16l-5 5"/></svg>';
  const icon = (name) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">${name==='folder'?'<path d="M3 7V5h6l2 2h10v13H3z"/>':name==='server'?'<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6h1M7 17h1M14 7h4M14 18h4"/>':'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 14h18M7 17h2"/>'}</svg>`;
  const api = (path, body) => FramekeepIO.api('/api/reference/'+path, body, 45000);
  const page = $('selection-page'), layout = page.querySelector('.library-layout'), surface = layout.querySelector('.preview-surface');
  let sources = [], scope = null, rows = [], offset = 0, cursors = [''], active = null, config = null, requestId = 0, submittedSearch = '', indexTimer;
  let preferencesReady=false,preferencesToken='',preferencesTimer,preferencesSaving=Promise.resolve(),preferencesSaved='';
  let referenceGeneration=0,referenceTaskReady=false,lastReferenceTask='';
  let references = [], referenceIndex = 0, referenceHovered = false, trayCollapsed = false, referenceSignature = '';
  const folderViews = new Map(), closedRoots = new Set();
  const indexRoots = new Set();
  let indexWasRunning=false;
  let searchScope='all',submittedScope='all',layoutTouched=false;
  try{searchScope=localStorage.getItem('framekeep-reference-search-scope')==='current'?'current':'all';}catch{}
  let zoom = 1, panX = 0, panY = 0, drag = null, paintedUrl = '', enabled = false, focusState = null, originalSides = null;
  let resultView='grid',gridScale=100,previewCollection='results',resultTotal=0;
  const fadeCurves={smooth:'ease-in-out',linear:'linear',easeIn:'ease-in',easeOut:'ease-out',none:null};
  let fadeCurve='smooth',displayedUrl='',displayedTransform='',fadeAnimations=[],lastBrowseWheel=-Infinity;
  const viewKey='framekeep-reference-view-v1';
  const filenameMeasure=document.createElement('canvas').getContext('2d');
  try{const saved=JSON.parse(localStorage.getItem(viewKey)||'null');if(saved){resultView=saved.view==='single'?'single':'grid';gridScale=Math.max(10,Math.min(400,Number(saved.scale)||100));}}catch{}
  try{const saved=JSON.parse(localStorage.getItem(viewKey)||'null');if(saved&&Object.hasOwn(fadeCurves,saved.curve))fadeCurve=saved.curve;}catch{}
  const cacheKey = 'framekeep-split-references-v2';
  const referenceId = item => JSON.stringify([item.device||'',item.root||'',item.relative||item.url]);
  const referenceOf = item => item ? references.findIndex(row=>referenceId(row)===referenceId(item)) : -1;
  try {
    const saved = localStorage.getItem(cacheKey);
    if(saved){const data=JSON.parse(saved);references=(Array.isArray(data.references)?data.references:[]).filter(item=>item&&typeof item.name==='string'&&typeof item.device==='string'&&typeof item.root==='string'&&typeof item.relative==='string');referenceIndex=Math.max(0,Math.min(references.length-1,Number.isInteger(data.index)?data.index:0));trayCollapsed=data.collapsed===true;}
    else {const previous=JSON.parse(localStorage.getItem('framekeep-split-reference-v1')||'null');if(previous&&!previous.url&&typeof previous.relative==='string')references=[previous];}
  } catch {}
  let videoPosterRevision='';
  document.addEventListener('framekeep:video-preview',event=>{videoPosterRevision=String(event.detail.frame_number);if(enabled){paintImage();}});
  const imageUrl = (item, size=1800) => '/api/reference/library/preview?'+new URLSearchParams({device:item.device,root:item.root,file:item.relative,size,v:item.fingerprint||'',poster_revision:videoPosterRevision,cache_generation:referenceGeneration});
  const call = async (route, params) => api('library/'+route+'?'+new URLSearchParams({...params,cache_generation:referenceGeneration}));
  const reference = document.createElement('section'); reference.id='split-reference'; reference.setAttribute('aria-label','参考图片工作区');
  reference.innerHTML=`<div class="split-resource-dock">
    <button id="split-resource-rail" class="panel-edge-toggle" type="button" aria-controls="split-resource-panel" aria-expanded="true" aria-label="收起参考资源"><span aria-hidden="true">‹</span></button>
    <aside id="split-resource-panel" class="split-tree-panel catalog-tree-panel"><header><strong>参考资源</strong><button id="split-add-root" aria-label="添加参考分区或文件夹" title="添加分区或文件夹">＋</button></header><div id="split-tree" class="catalog-tree" aria-label="参考资源目录"></div><footer><button id="split-api-settings">${icon('server')}设备与 API</button><div class="split-index-summary"><span id="split-index-status">正在连接数据库…</span><strong id="split-index-percent" hidden>0%</strong></div><progress id="split-index-progress" max="100" value="0" aria-label="后台索引进度" hidden></progress></footer></aside>
    <aside class="split-thumbnails"><header><strong>资源浏览</strong><button id="split-hide-resources" aria-label="收起参考资源面板" title="收起资源双栏">‹</button></header><form class="split-search" id="split-search-form" role="search" aria-label="参考资源搜索"><div class="split-search-controls"><input id="split-search" type="search" aria-label="搜索参考文件数据库" placeholder="输入后点击搜索"><button id="split-search-submit" type="submit" disabled>搜索</button></div><span id="split-search-scope">当前目录</span></form><div id="split-results" role="status"></div><div id="split-grid" aria-label="参考图片缩略图"></div><div class="split-pagination"><button id="split-prev-page">上一页</button><button id="split-next-page">下一页</button></div></aside>
  </div><main class="split-reference-main"><header class="split-preview-heading"><span><i></i>参考图片</span><div><button id="split-show-resources" aria-label="展开参考资源面板" title="展开参考资源">☷</button><button id="split-import" title="加载本机参考图片">加载图片</button><button id="split-reference-focus" aria-label="仅显示参考预览" title="收起或恢复参考侧模块">⛶</button></div></header>
  <div class="split-result-toolbar"><div class="split-result-views" role="group" aria-label="参考结果视图"><button id="split-view-grid" type="button" aria-controls="split-preview-grid">网格</button><button id="split-view-single" type="button" aria-controls="split-reference-stage">大图</button></div><label id="split-grid-scale-control" for="split-grid-scale">缩略图<input id="split-grid-scale" type="range" min="10" max="400" step="10" value="100" aria-label="参考缩略图显示比例"><output id="split-grid-scale-value" for="split-grid-scale">100%</output></label><div id="split-result-paging"><button id="split-result-prev" type="button" aria-label="参考结果上一页" disabled>‹</button><span id="split-result-page" role="status">0 / 0</span><button id="split-result-next" type="button" aria-label="参考结果下一页" disabled>›</button></div></div>
  <div id="split-preview-grid" class="split-preview-grid" tabindex="0" role="region" aria-label="参考搜索结果网格，滚轮浏览，Ctrl＋滚轮调整缩略图比例"><p class="split-empty">选择参考目录，或输入关键词后点击搜索</p></div>
  <div id="split-reference-stage" tabindex="0" aria-label="参考图片预览，滚轮缩放"><img id="split-reference-image" alt="参考图片" hidden><div id="split-reference-empty">选择一张参考图片<small>从资源库浏览，或加载本机图片</small></div><button id="split-pin-large" class="split-pin" aria-label="将当前大图固定为参考" aria-pressed="false" title="固定为参考">${pin}</button><span id="split-pin-state" hidden>已固定参考</span><button id="split-reference-prev" class="split-reference-switch" aria-label="上一张参考图">‹</button><button id="split-reference-next" class="split-reference-switch" aria-label="下一张参考图">›</button><button id="split-zoom-reset" title="重置参考图缩放">适应</button></div>
  <footer class="split-reference-caption"><strong id="split-reference-name">尚未选择参考</strong><span id="split-reference-source">只读浏览 · 不改动参考图片</span></footer>
  <details id="split-reference-tray" open><summary><span>参考图 <b id="split-reference-count">0</b></span><small>鼠标移入左侧后，使用 ← → 切换</small><button id="split-clear-reference" type="button" disabled title="清理当前选中的参考图及其临时缓存">清理参考图</button></summary><div id="split-reference-thumbnails" role="list" aria-label="已固定的参考图片"></div></details></main>`;
  page.prepend(reference);
  $('split-result-paging').insertAdjacentHTML('beforebegin','<label id="split-fade-control" for="split-fade-curve">叠化曲线<select id="split-fade-curve" aria-label="大图叠化曲线"><option value="smooth">平滑</option><option value="linear">线性</option><option value="easeIn">渐入</option><option value="easeOut">渐出</option><option value="none">关闭叠化</option></select></label>');
  $('split-fade-curve').value=fadeCurve;
  reference.querySelector('.split-result-toolbar').insertAdjacentHTML('afterend','<section id="split-index-overview" aria-label="参考库扫描进度" hidden><div class="split-index-summary"><span id="split-index-overview-status"></span><strong id="split-index-overview-percent"></strong></div><progress id="split-index-overview-progress" max="100" value="0" aria-label="参考库扫描进度"></progress></section>');
  $('split-reference-stage').setAttribute('aria-label','参考图片预览，滚轮切图，Ctrl＋滚轮缩放');
  $('split-reference-image').insertAdjacentHTML('beforebegin','<img id="split-reference-outgoing" alt="" aria-hidden="true" hidden>');
  $('split-search-scope').insertAdjacentHTML('beforebegin','<select id="split-search-range" aria-label="参考搜索范围"><option value="all">全部已添加目录</option><option value="current">当前目录及子目录</option></select>');
  $('split-search-range').value=searchScope;
  $('split-search-range').onchange=event=>{searchScope=event.target.value;try{localStorage.setItem('framekeep-reference-search-scope',searchScope);}catch{}};
  $('split-reference-tray').open=!trayCollapsed;
  const rightHeading = document.createElement('header'); rightHeading.className='split-live-heading';
  rightHeading.innerHTML='<span><i></i>当前拍摄</span><div><button id="split-right-folders" aria-label="折叠或展开拍摄缩略图">拍摄目录</button><button id="split-right-info" aria-label="折叠或展开当前拍摄信息">拍摄信息</button><button id="split-immersive" aria-pressed="false">沉浸对比</button><button id="split-exit" aria-label="退出分屏拍摄">×</button></div>';
  surface.before(rightHeading);
  const menu = document.createElement('div'); menu.id='split-context'; menu.className='photo-context-menu'; menu.role='menu';menu.hidden=true;
  menu.innerHTML=`<button role="menuitem" id="split-context-pin">${pin}设为参考</button><button role="menuitem" id="split-context-reveal">${icon('folder')}打开所在路径</button>`;document.body.append(menu);
  let contextItem=null;
  function report(error) { Framekeep.notify(error.message || String(error)); }
  function saveReferences() {
    try {localStorage.setItem(cacheKey,JSON.stringify(preferencesSnapshot()));} catch {}
    if(preferencesReady){clearTimeout(preferencesTimer);preferencesTimer=setTimeout(()=>flushReferencePreferences().catch(report),250);}
  }
  function preferencesSnapshot(){const saved=references.filter(item=>!item.url),current=references[referenceIndex],index=current?saved.findIndex(item=>referenceId(item)===referenceId(current)):-1;return {references:saved,index:Math.max(0,index),collapsed:!$('split-reference-tray').open,split_enabled:enabled,cache_generation:referenceGeneration};}
  function flushReferencePreferences(){clearTimeout(preferencesTimer);if(!preferencesReady)return preferencesSaving;const snapshot=preferencesSnapshot(),serialized=JSON.stringify(snapshot);if(serialized===preferencesSaved)return preferencesSaving;preferencesSaving=preferencesSaving.catch(()=>{}).then(async()=>{await api('preferences',snapshot);preferencesSaved=serialized;});return preferencesSaving;}
  const flushSelection=FramekeepIO.flush;FramekeepIO.flush=async()=>{await flushSelection();await flushReferencePreferences();};
  window.addEventListener('pagehide',()=>{clearTimeout(preferencesTimer);clearTimeout(indexTimer);if(preferencesReady&&preferencesToken)fetch('/api/reference/preferences',{method:'POST',headers:{'Content-Type':'application/json','X-Framekeep-Token':preferencesToken},body:JSON.stringify(preferencesSnapshot()),keepalive:true}).catch(()=>{});},{once:true});
  function shown() { return active || references[referenceIndex]; }
  function saveView(){try{localStorage.setItem(viewKey,JSON.stringify({view:resultView,scale:gridScale,curve:fadeCurve}));}catch{}}
  function setReferenceView(view,remember=true){
    resultView=view==='single'?'single':'grid';
    $('split-preview-grid').hidden=resultView!=='grid';$('split-reference-stage').hidden=resultView==='grid';$('split-grid-scale-control').hidden=resultView!=='grid';$('split-result-paging').hidden=resultView!=='grid';
    $('split-view-grid').setAttribute('aria-pressed',String(resultView==='grid'));$('split-view-single').setAttribute('aria-pressed',String(resultView==='single'));
    $('split-fade-control').hidden=resultView!=='single';if(resultView==='grid')clearFade();
    if(remember)saveView();paintImage();
  }
  function setGridScale(value){
    const requested=Number(value);gridScale=Math.max(10,Math.min(400,Math.round(Number.isFinite(requested)?requested:100)));
    $('split-preview-grid').style.setProperty('--split-result-size',160*gridScale/100+'px');$('split-preview-grid').classList.toggle('is-compact',gridScale<50);
    $('split-grid-scale').value=gridScale;$('split-grid-scale-value').textContent=gridScale+'%';updateFilenames();saveView();
  }
  function updateFilenames(){
    if(!filenameMeasure)return;
    for(const card of reference.querySelectorAll('[data-row]')){
      const label=card.querySelector(':scope>span');if(!label)continue;
      const style=getComputedStyle(label),box=getComputedStyle(card);filenameMeasure.font=style.font;
      const available=card.clientWidth-parseFloat(box.paddingLeft)-parseFloat(box.paddingRight)-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight);
      card.classList.toggle('is-filename-hidden',filenameMeasure.measureText(label.textContent).width>available);
    }
  }
  const filenameResize=new ResizeObserver(updateFilenames);filenameResize.observe($('split-grid'));filenameResize.observe($('split-preview-grid'));
  window.addEventListener('pagehide',()=>filenameResize.disconnect(),{once:true});
  function resultIndex(){return previewCollection==='results'&&active?rows.findIndex(row=>referenceId({device:scope?.device,root:scope?.root,...row})===referenceId(active)):-1;}
  function browsePreview(step){
    if(previewCollection==='results'){const index=resultIndex();if(index>=0)choose(Math.max(0,Math.min(rows.length-1,index+step)));}
    else switchReference(step);
  }
  $('split-view-grid').onclick=()=>setReferenceView('grid');
  $('split-view-single').onclick=()=>{if(!shown()&&rows.length)choose(0);setReferenceView('single');};
  $('split-grid-scale').oninput=event=>setGridScale(event.target.value);
  $('split-preview-grid').addEventListener('wheel',event=>{if(!event.ctrlKey||!event.deltaY)return;event.preventDefault();event.stopPropagation();setGridScale(gridScale+(event.deltaY<0?10:-10));},{passive:false});
  $('split-fade-curve').onchange=event=>{fadeCurve=event.target.value;clearFade();saveView();};
  function paintReferences() {
    const strip=$('split-reference-thumbnails'),scroll=strip.scrollLeft;
    $('split-reference-count').textContent=references.length;
    $('split-clear-reference').disabled=!references.length;
    if(!referenceTaskReady)return;
    const signature=JSON.stringify(references.map(referenceId));
    if(signature!==referenceSignature){referenceSignature=signature;strip.innerHTML=references.length?references.map((item,index)=>`<div class="split-saved-reference ${index===referenceIndex?'active':''}" role="listitem"><button data-show-reference="${index}" aria-label="查看参考图 ${esc(item.name)}" aria-pressed="${index===referenceIndex}" title="${esc(item.name+' · '+(item.source||'本机加载'))}"><img src="${esc(item.url||imageUrl(item,240))}" alt="参考图 ${esc(item.name)}"><span>${esc(item.name)}</span></button><button class="split-remove-reference" data-remove-reference="${index}" aria-label="移除参考图 ${esc(item.name)}" title="移除参考">×</button></div>`).join(''):'<p class="split-reference-tray-empty">点击图片右上角图钉，将多张图片加入参考</p>';}
    for(const [index,card] of [...strip.querySelectorAll('.split-saved-reference')].entries()){card.classList.toggle('active',index===referenceIndex);card.querySelector('[data-show-reference]').setAttribute('aria-pressed',String(index===referenceIndex));}
    strip.scrollLeft=scroll;strip.querySelector('.active')?.scrollIntoView({block:'nearest',inline:'nearest'});
  }
  function addReference(item) {const index=referenceOf(item);if(index<0){references.push({...item});referenceIndex=references.length-1;}else referenceIndex=index;$('split-reference-tray').open=true;saveReferences();paintImage();}
  function removeReference(index) {const current=references[referenceIndex],removed=references[index];if(!removed)return;references.splice(index,1);const retained=current?referenceOf(current):-1;referenceIndex=retained>=0?retained:Math.max(0,Math.min(index,references.length-1));if(previewCollection==='references'||active&&referenceId(active)===referenceId(removed)){active=references[referenceIndex]||null;previewCollection=active?'references':'results';}if(removed.url){URL.revokeObjectURL(removed.url);objectUrls.delete(removed.url);}else{const item={device:removed.device,root:removed.root,relative:removed.relative,cache_generation:referenceGeneration};preferencesSaving=preferencesSaving.catch(()=>{}).then(()=>api('clear-reference',item));preferencesSaving.catch(report);}saveReferences();paintImage();}
  function toggleReference(item) {if(!item)return;const index=referenceOf(item);if(index>=0)removeReference(index);else addReference(item);}
  function switchReference(step) {if(!references.length)return;referenceIndex=Math.max(0,Math.min(references.length-1,referenceIndex+step));previewCollection='references';active=references[referenceIndex];saveReferences();paintImage();}
  $('split-reference-prev').onclick=()=>browsePreview(-1);$('split-reference-next').onclick=()=>browsePreview(1);
  $('split-reference-thumbnails').onclick=event=>{const remove=event.target.closest('[data-remove-reference]'),show=event.target.closest('[data-show-reference]');if(remove)removeReference(Number(remove.dataset.removeReference));else if(show){referenceIndex=Number(show.dataset.showReference);previewCollection='references';active=references[referenceIndex];setReferenceView('single');saveReferences();}};
  $('split-reference-tray').addEventListener('toggle',()=>{saveReferences();window.dispatchEvent(new Event('resize'));});
  $('split-clear-reference').onclick=async event=>{event.preventDefault();event.stopPropagation();const item=references[referenceIndex];if(!item)return;removeReference(referenceIndex);try{await flushReferencePreferences();Framekeep.notify('已清理参考图：'+item.name);}catch(error){report(error);}};
  reference.addEventListener('pointerenter',()=>{referenceHovered=true;});reference.addEventListener('pointerleave',()=>{referenceHovered=false;});window.addEventListener('blur',()=>{referenceHovered=false;});
  document.addEventListener('keydown',event=>{
    if(!enabled||page.classList.contains('hidden')||(!referenceHovered&&!reference.contains(document.activeElement))||event.ctrlKey||event.metaKey||event.altKey||event.shiftKey||document.querySelector('dialog[open]')||/INPUT|TEXTAREA|SELECT/.test(event.target.tagName)||event.target.isContentEditable)return;
    if(event.key==='Escape'&&resultView==='single'&&menu.hidden){event.preventDefault();event.stopImmediatePropagation();setReferenceView('grid');const index=resultIndex();($('split-preview-grid').querySelector(`[data-row="${index}"]`)||$('split-preview-grid')).focus();}
    else if(['ArrowLeft','ArrowRight'].includes(event.key)){
      event.preventDefault();event.stopImmediatePropagation();const step=event.key==='ArrowRight'?1:-1;
      if(resultView==='single')browsePreview(step);
      else if(rows.length){const index=Math.max(0,Math.min(rows.length-1,resultIndex()+step));choose(index);$('split-preview-grid').querySelector(`[data-row="${index}"]`)?.focus();}
    }
  },true);
  function paintImage() {
    const item=shown(),pinned=referenceOf(item)>=0,image=$('split-reference-image'),nextUrl=item&&(item.url||imageUrl(item));
    if(nextUrl!==paintedUrl){paintedUrl=nextUrl;zoom=1;panX=panY=0;image.style.transform='scale(1)';}
    image.hidden=!item;$('split-reference-empty').hidden=!!item;$('split-pin-large').disabled=!item;
    if(item){if(referenceTaskReady&&resultView==='single'&&image.getAttribute('src')!==nextUrl){clearFade();const outgoing=$('split-reference-outgoing');if(displayedUrl&&displayedUrl!==nextUrl){outgoing.src=displayedUrl;outgoing.style.transform=displayedTransform;outgoing.hidden=false;outgoing.style.opacity='1';image.style.opacity='0';}image.src=nextUrl;}image.alt=item.name;$('split-reference-name').textContent=item.name;$('split-reference-source').textContent=(pinned?'固定参考 · ':'正在浏览 · ')+(item.source||'本机加载')+' · 滚轮切图 · Ctrl＋滚轮缩放';}else{clearFade();displayedUrl='';image.removeAttribute('src');}
    if(resultView==='grid'){$('split-reference-name').textContent=`${resultTotal} 张图片 · 当前页 ${rows.length} 张`;$('split-reference-source').textContent=(submittedSearch?'搜索：'+submittedSearch+' · ':'')+'双击查看大图 · 滚轮浏览 · Ctrl＋滚轮缩放 10%–400% · 图钉加入底部参考区';}
    $('split-pin-large').setAttribute('aria-pressed',String(!!pinned));$('split-pin-large').setAttribute('aria-label',pinned?'取消固定参考':'将当前大图固定为参考');$('split-pin-large').title=pinned?'取消固定参考':'固定为参考';$('split-pin-state').hidden=!pinned;$('split-zoom-reset').textContent=zoom===1?'适应':Math.round(zoom*100)+'%';
    $('split-pin-state').textContent=`参考 ${referenceOf(item)+1} / ${references.length}`;
    for(const card of reference.querySelectorAll('[data-row]')){const item=rows[Number(card.dataset.row)];if(!item)continue;const record={device:scope?.device,root:scope?.root,...item},saved=referenceOf(record)>=0;card.classList.toggle('is-pinned',saved);card.classList.toggle('active',!!active&&referenceId(record)===referenceId(active));const button=card.querySelector('[data-pin-row]');button?.setAttribute('aria-pressed',String(saved));button?.setAttribute('aria-label',(saved?'取消固定 ':'固定 ')+item.name+(saved?'':' 为参考'));}
    paintReferences();
    const index=previewCollection==='results'?resultIndex():referenceIndex,count=previewCollection==='results'?rows.length:references.length;
    $('split-reference-prev').disabled=index<=0||count<2;$('split-reference-next').disabled=index<0||index>=count-1||count<2;$('split-reference-stage').classList.toggle('has-multiple-references',count>1);
  }
  function choose(index, makePin=false) {
    const row=rows[index];if(!row||!scope&&!row.root)return;
    active={device:scope?.device,root:scope?.root,source:scope?.label,...row};
    previewCollection='results';
    if(makePin){addReference(active);}
    for(const card of $('split-grid').querySelectorAll('[data-row]'))card.classList.toggle('active',Number(card.dataset.row)===index);
    paintImage();
  }
  $('split-pin-large').onclick=()=>toggleReference(shown());
  function clearFade(){for(const animation of fadeAnimations){animation.onfinish=null;animation.cancel();}fadeAnimations=[];$('split-reference-outgoing').hidden=true;$('split-reference-outgoing').style.opacity='0';$('split-reference-image').style.opacity='1';}
  $('split-reference-image').onerror=()=>{clearFade();displayedUrl='';$('split-reference-image').hidden=true;$('split-reference-empty').hidden=false;$('split-reference-empty').textContent='参考图片不可用，请检查设备连接或重新选择';};
  $('split-reference-image').onload=()=>{
    const image=$('split-reference-image'),outgoing=$('split-reference-outgoing');
    if(image.getAttribute('src')!==paintedUrl||!image.naturalWidth)return;
    $('split-reference-empty').hidden=true;image.hidden=false;image.style.opacity='1';
    displayedUrl=image.getAttribute('src');displayedTransform=image.style.transform;
    if(resultView==='single'&&!outgoing.hidden&&fadeCurves[fadeCurve]&&!matchMedia('(prefers-reduced-motion: reduce)').matches){
      const options={duration:260,easing:fadeCurves[fadeCurve]};outgoing.style.opacity='0';
      fadeAnimations=[image.animate([{opacity:0},{opacity:1}],options),outgoing.animate([{opacity:1},{opacity:0}],options)];
      fadeAnimations[1].onfinish=()=>clearFade();
    }else clearFade();
  };
  function paintZoom(){if(zoom===1)panX=panY=0;clearFade();$('split-reference-image').style.transform=`translate(${panX}px,${panY}px) scale(${zoom})`;if($('split-reference-image').getAttribute('src')===displayedUrl)displayedTransform=$('split-reference-image').style.transform;$('split-zoom-reset').textContent=zoom===1?'适应':Math.round(zoom*100)+'%';}
  $('split-reference-stage').addEventListener('wheel',event=>{if(!shown()||!event.deltaY)return;event.preventDefault();event.stopPropagation();if(event.ctrlKey){zoom=Math.max(1,Math.min(8,zoom*(event.deltaY>0?.9:1.1)));paintZoom();}else if(event.timeStamp-lastBrowseWheel>=160){lastBrowseWheel=event.timeStamp;browsePreview(event.deltaY>0?1:-1);}},{passive:false});
  $('split-reference-stage').onpointerdown=event=>{if(zoom<=1||event.target.closest('button'))return;drag={x:event.clientX-panX,y:event.clientY-panY};event.currentTarget.setPointerCapture(event.pointerId);};
  $('split-reference-stage').onpointermove=event=>{if(!drag)return;const box=event.currentTarget.getBoundingClientRect();panX=Math.max(-box.width*(zoom-1)/2,Math.min(box.width*(zoom-1)/2,event.clientX-drag.x));panY=Math.max(-box.height*(zoom-1)/2,Math.min(box.height*(zoom-1)/2,event.clientY-drag.y));paintZoom();};
  $('split-reference-stage').onpointerup=()=>{drag=null;};$('split-reference-stage').onpointercancel=()=>{drag=null;};
  $('split-zoom-reset').onclick=()=>{zoom=1;paintZoom();};
  const input=document.createElement('input');input.type='file';input.accept='image/*';input.multiple=true;input.hidden=true;document.body.append(input);
  const objectUrls=new Set();
  $('split-import').onclick=()=>input.click();
  input.onchange=()=>{for(const file of input.files){if(!file.type.startsWith('image/'))continue;const url=URL.createObjectURL(file);objectUrls.add(url);active={name:file.name,url,source:'本机加载'};previewCollection='references';addReference(active);}setReferenceView('single');input.value='';};
  window.addEventListener('pagehide',()=>{for(const url of objectUrls)URL.revokeObjectURL(url);},{once:true});
  for(const grid of [$('split-grid'),$('split-preview-grid')]){
    grid.onclick=event=>{const card=event.target.closest('[data-row]');if(card){choose(Number(card.dataset.row));if(event.target.closest('[data-pin-row]'))toggleReference(active);}};
    grid.ondblclick=event=>{const card=event.target.closest('[data-row]');if(card&&!event.target.closest('[data-pin-row]')){choose(Number(card.dataset.row));setReferenceView('single');$('split-reference-stage').focus();}};
    grid.addEventListener('keydown',event=>{const card=event.target.closest('[data-row]');if(card&&['Enter',' '].includes(event.key)&&!event.target.closest('[data-pin-row]')){event.preventDefault();event.stopPropagation();choose(Number(card.dataset.row));if(event.key==='Enter'){setReferenceView('single');$('split-reference-stage').focus();}}});
  }
  function referenceMenu(event,item){event.preventDefault();menu.hidden=true;if(item?.device!=='local')return;contextItem=item;menu.hidden=false;menu.style.left=Math.min(innerWidth-180,event.clientX)+'px';menu.style.top=Math.min(innerHeight-80,event.clientY)+'px';$('split-context-pin').focus();}
  $('split-grid').oncontextmenu=event=>{const card=event.target.closest('[data-row]');if(card)referenceMenu(event,{device:scope?.device,root:scope?.root,source:scope?.label,...rows[Number(card.dataset.row)]});else event.preventDefault();};
  $('split-preview-grid').oncontextmenu=$('split-grid').oncontextmenu;
  $('split-reference-stage').oncontextmenu=event=>referenceMenu(event,shown());
  $('split-reference-thumbnails').oncontextmenu=event=>{const card=event.target.closest('.split-saved-reference'),button=card?.querySelector('[data-show-reference]');referenceMenu(event,button?references[Number(button.dataset.showReference)]:null);};
  $('split-context-pin').onclick=()=>{active=contextItem;previewCollection='results';addReference(active);menu.hidden=true;};
  $('split-context-reveal').onclick=async()=>{const item=contextItem;menu.hidden=true;if(!item)return;try{await api('reveal',{device:item.device,root:item.root,file:item.relative});}catch(error){report(error);}};
  document.addEventListener('pointerdown',event=>{if(!menu.contains(event.target))menu.hidden=true;});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!menu.hidden){event.stopImmediatePropagation();menu.hidden=true;}},true);
  async function loadItems() {
    const text=submittedSearch,all=!!text&&submittedScope==='all';if(!scope&&!all)return;
    const sequence=++requestId,chosen={...scope};
    $('split-results').textContent='正在检索…';
    try {
      const result=all?await api('search?'+new URLSearchParams({q:text,cursor:cursors[Math.floor(offset/60)]||'',cache_generation:referenceGeneration})):await call('items',{device:chosen.device,root:chosen.root,folder:chosen.folder,q:text,offset,cursor:cursors[Math.floor(offset/60)]||''});
      if(sequence!==requestId)return;
      const groups=new Map();
      for(const row of result.rows){
        const device=row.device||chosen.device,root=row.root||chosen.root,path=String(row.relative||row.name).replace(/\\/g,'/').split('/').slice(0,-1).join('/'),key=JSON.stringify([device,root,path]);
        if(!groups.has(key)){const source=folderLabel(device,root);groups.set(key,{name:path.split('/').pop()||source.split(' / ').pop(),title:source+(path?' / '+path:''),rows:[]});}
        groups.get(key).rows.push(row);
      }
      rows=[...groups.values()].flatMap(group=>group.rows);resultTotal=result.total;
      $('split-results').textContent=`${result.total} 张图片${['queued','scanning'].includes(result.status)?' · 后台索引中':` · ${result.elapsed_ms} ms`}${result.errors?.length?' · 部分位置不可用':''}`;
      if(result.has_more)cursors[Math.floor(offset/60)+1]=result.next_cursor;
      $('split-search-scope').textContent=text?(all?'全部已添加目录':'当前目录及子目录')+' · '+result.engine:'当前目录 · '+result.engine;
      $('split-search-scope').title=result.note;
      $('split-grid').innerHTML=rows.length?rows.map((row,index)=>`<article class="split-resource-card" data-row="${index}" tabindex="0" role="button" aria-label="浏览参考 ${esc(row.name)}" title="${esc(row.name)}"><div><img src="${esc(imageUrl({device:chosen.device,root:chosen.root,...row},420))}" alt="${esc(row.name)}" loading="lazy"><button class="split-pin" data-pin-row="${index}" aria-label="固定 ${esc(row.name)} 为参考" title="设为参考">${pin}</button><span class="split-card-pinned">已固定</span></div><span title="${esc(row.relative)}">${esc(row.name)}</span></article>`).join(''):'<p class="split-empty">'+(all?'已添加目录没有匹配图片':'此目录没有匹配图片')+'<br>可更改关键词或搜索范围</p>';
      $('split-prev-page').disabled=offset===0;$('split-next-page').disabled=!result.has_more;
      const cards=[...$('split-grid').children];let first=0;
      $('split-preview-grid').innerHTML=rows.length?[...groups.values()].map(group=>{const html=`<div class="split-folder-divider" role="heading" aria-level="3" title="${esc(group.title)}"><span>${esc(group.name)}</span><i aria-hidden="true"></i></div>`+cards.slice(first,first+group.rows.length).map(card=>card.outerHTML).join('');first+=group.rows.length;return html;}).join(''):$('split-grid').innerHTML;
      updateFilenames();
      $('split-result-prev').disabled=offset===0;$('split-result-next').disabled=!result.has_more;$('split-result-page').textContent=result.total?`${Math.floor(offset/60)+1} / ${Math.ceil(result.total/60)}`:'0 / 0';
      if(previewCollection==='results'){active=null;if(rows.length)choose(0);else paintImage();}else paintImage();
    } catch(error) { if(sequence===requestId){$('split-results').textContent='检索失败';$('split-grid').textContent=error.message;$('split-preview-grid').textContent=error.message;} }
  }
  async function selectFolder(device,root,folder,label) {
    scope={device,root,folder,label};offset=0;cursors=[''];submittedSearch='';
    $('split-search').value='';
    $('split-search-submit').disabled=false;
    for(const button of $('split-tree').querySelectorAll('[data-resource-folder]'))button.classList.toggle('active',button.dataset.device===device&&button.dataset.root===root&&button.dataset.folder===folder);
    await loadItems();
  }
  const rootKey=(device,root)=>JSON.stringify([device,root]);
  function indexInfo(root){
    const progress=root.progress||{},running=['queued','scanning'].includes(root.status),percent=root.status==='ready'?100:Math.max(0,Math.min(99,Number(progress.percent)||0));
    const estimated=running&&(progress.estimated||!progress.total),label=(estimated?'约 ':'')+percent+'%';
    const detail=root.status==='queued'?'等待扫描':`已扫描 ${progress.done||0} 个文件`+(progress.total?` / 约 ${progress.total}`:progress.directories_total?` · 目录 ${progress.directories_done||0} / 已发现 ${progress.directories_total}`:' · 正在发现目录');
    return {running,percent,label,detail,title:root.name+'：'+detail+(estimated?'（估算进度，发现更多目录后会调整）':'')};
  }
  function paintIndexProgress(){
    const roots=[...(config?.roots||sources.find(device=>device.id==='local')?.roots||[]).map(root=>({...root,device:'local'})),...sources.filter(device=>device.id!=='local').flatMap(device=>(device.roots||[]).map(root=>({...root,device:device.id})))];
    const running=roots.filter(root=>indexInfo(root).running);
    if(running.length&&!indexWasRunning)indexRoots.clear();
    for(const root of running)indexRoots.add(rootKey(root.device,root.id));
    const batch=roots.filter(root=>indexRoots.has(rootKey(root.device,root.id))),failed=batch.filter(root=>root.status==='error');
    const percent=batch.length?Math.floor(batch.reduce((sum,root)=>sum+indexInfo(root).percent,0)/batch.length):0;
    const estimated=running.some(root=>root.progress?.estimated||!root.progress?.total),label=(estimated?'约 ':'')+percent+'%';
    const title=batch.map(root=>indexInfo(root).title+(root.error?' · '+root.error:'')).join('\n');
    const status=running.length?`正在索引 ${running.length} 个位置 · 已扫描 ${batch.reduce((sum,root)=>sum+(root.progress?.done||0),0)} 个文件`+(running.some(root=>!root.progress?.total)?' · 目录估算':''):failed.length?`${failed.length} 个位置索引失败，请检查目录连接`:'索引完成';
    $('split-index-overview').hidden=!batch.length;
    $('split-index-overview-status').textContent=status;$('split-index-overview-percent').textContent=label;
    $('split-index-overview-progress').value=percent;$('split-index-overview').title=title;
    $('split-index-status').textContent=batch.length?status:config?.roots.some(root=>root.native_complete)?'本机快速索引':config?.everything.available?'Everything 已连接':'本机目录数据库';
    $('split-index-status').title=title;$('split-index-percent').hidden=!batch.length;$('split-index-percent').textContent=label;
    $('split-index-progress').hidden=!batch.length;$('split-index-progress').value=percent;$('split-index-progress').title=title;
    indexWasRunning=!!running.length;
  }
  function indexMarkup(root){const info=indexInfo(root);return info.running?`<div class="split-root-index" title="${esc(info.title)}"><span>${esc(info.detail)} · ${info.label}</span><progress max="100" value="${info.percent}" aria-label="${esc(root.name)}扫描进度"></progress></div>`:'';}
  function scheduleIndexRefresh(){
    clearTimeout(indexTimer);
    if(config?.index_storage?.job?.status==='running'||config?.roots.some(root=>indexInfo(root).running)||sources.some(device=>device.roots?.some(root=>indexInfo(root).running)))indexTimer=setTimeout(async()=>{try{await Promise.all([loadSources(),refreshSettings()]);if(config?.index_storage?.job?.status!=='running'&&scope&&!submittedSearch&&!$('split-search').value.trim())await loadItems();}catch(error){report(error);scheduleIndexRefresh();}},1500);
  }
  function folderLabel(device,root){const source=sources.find(row=>row.id===device),item=source?.roots.find(row=>row.id===root);return (source?.name||'参考资源')+' / '+(item?.name||'共享目录');}
  function paintDirectory(device,root) {
    const key=rootKey(device,root),view=folderViews.get(key),group=[...$('split-tree').querySelectorAll('[data-resource-group]')].find(node=>node.dataset.resourceGroup===key);
    if(!view||!group)return;
    const attributes=`data-device="${esc(device)}" data-root="${esc(root)}"`;
    const container=group.querySelector('.catalog-drive-folders'),html=(view.folder?`<button class="split-resource-up" data-resource-up ${attributes} title="${esc(view.folder)}" aria-label="返回 ${esc(view.folder)} 的上级目录">‹ <span>${esc(view.folder)}</span></button>`:'')+view.children.map(row=>{
      const selected=scope?.device===device&&scope?.root===root&&(scope.folder===row.folder||scope.folder.startsWith(row.folder+'/'));
      return `<button class="split-resource-folder-row ${selected?'active':''}" data-resource-folder ${attributes} data-folder="${esc(row.folder)}" title="${esc(row.folder)}" aria-selected="${selected}"><span class="split-folder-chevron">›</span><span>${esc(row.name)}</span></button>`;
    }).join('');
    if(container.dataset.content!==html){container.innerHTML=html;container.dataset.content=html;}
  }
  async function loadDirectory(device,root,folder='') {
    const key=rootKey(device,root),view=folderViews.get(key)||{folder:'',children:[],sequence:0};folderViews.set(key,view);
    const sequence=++view.sequence,result=await call('folders',{device,root,folder});
    if(view.sequence!==sequence)return;
    view.folder=folder;view.children=result.folders;paintDirectory(device,root);
  }
  async function loadSources() {
    const result=await api('sources');sources=result.devices;
    $('split-search-submit').disabled=!scope&&!sources.some(device=>device.roots?.length);
    const tree=$('split-tree'),existing=new Map([...tree.children].map(node=>[node.dataset.resourceGroup,node])),keys=new Set();
    for(const device of sources){
      for(const root of device.roots||[]){
        const key=rootKey(device.id,root.id);keys.add(key);let group=existing.get(key);
        if(!group){group=document.createElement('section');group.className='catalog-drive-group split-resource-group';group.dataset.resourceGroup=key;group.innerHTML=`<button class="catalog-drive-button" data-resource-root data-device="${esc(device.id)}" data-root="${esc(root.id)}"><span class="split-root-icon">${icon(device.remote?'server':'disk')}</span><span class="split-root-name"></span><small></small></button>${device.remote?'<small class="split-resource-origin"></small>':''}<div class="split-root-index" hidden><span></span><progress max="100" value="0" aria-label="目录扫描进度"></progress></div><div class="catalog-drive-folders"></div>`;tree.append(group);}
        const button=group.querySelector('[data-resource-root]'),open=!closedRoots.has(key);
        button.setAttribute('aria-label',root.name);button.setAttribute('aria-expanded',String(open));button.title=device.name+' / '+root.name+(device.remote?' · API 连接':'');
        group.querySelector('.split-root-name').textContent=root.name;
        const info=indexInfo(root),count=String(info.running?info.label:root.count||0),badge=button.querySelector('small');if(badge.textContent!==count)badge.textContent=count;
        const progress=group.querySelector('.split-root-index');progress.hidden=!info.running;progress.title=info.title;progress.querySelector('span').textContent=info.detail;progress.querySelector('progress').value=info.percent;
        if(device.remote)group.querySelector('.split-resource-origin').textContent='API · '+device.name;
        group.querySelector('.catalog-drive-folders').hidden=!open;
      }
      if(device.error){const key='error:'+device.id;keys.add(key);let node=existing.get(key);if(!node){node=document.createElement('p');node.className='split-empty';node.dataset.resourceGroup=key;tree.append(node);}node.textContent=device.name+' · '+device.error;}
    }
    for(const [key,node] of existing)if(!keys.has(key))node.remove();
    if(!keys.size){const empty=document.createElement('p');empty.className='split-empty';empty.textContent='点击 ＋ 添加本机参考目录，或在「设备与 API」连接其他设备';tree.replaceChildren(empty);}
    paintIndexProgress();scheduleIndexRefresh();
    await Promise.all(sources.flatMap(device=>(device.roots||[]).map(root=>loadDirectory(device.id,root.id,folderViews.get(rootKey(device.id,root.id))?.folder||'').catch(error=>{const group=[...$('split-tree').querySelectorAll('[data-resource-group]')].find(node=>node.dataset.resourceGroup===rootKey(device.id,root.id));if(group){const container=group.querySelector('.catalog-drive-folders');container.textContent=error.message;delete container.dataset.content;}}))));
  }
  $('split-tree').onclick=async event=>{
    const toggle=event.target.closest('[data-resource-root]'),select=event.target.closest('[data-resource-folder]'),up=event.target.closest('[data-resource-up]');
    try {
      if(toggle){const d=toggle.dataset,key=rootKey(d.device,d.root),open=toggle.getAttribute('aria-expanded')!=='true';toggle.setAttribute('aria-expanded',String(open));toggle.closest('.split-resource-group').querySelector('.catalog-drive-folders').hidden=!open;if(open){closedRoots.delete(key);if(scope?.device!==d.device||scope?.root!==d.root)await selectFolder(d.device,d.root,folderViews.get(key)?.folder||'',folderLabel(d.device,d.root));}else closedRoots.add(key);}
      else if(up){const d=up.dataset,folder=folderViews.get(rootKey(d.device,d.root)).folder.split('/').slice(0,-1).join('/');await Promise.all([loadDirectory(d.device,d.root,folder),selectFolder(d.device,d.root,folder,folderLabel(d.device,d.root))]);}
      else if(select){const d=select.dataset,view=folderViews.get(rootKey(d.device,d.root)),sequence=++view.sequence;await selectFolder(d.device,d.root,d.folder,folderLabel(d.device,d.root));const result=await call('folders',{device:d.device,root:d.root,folder:d.folder});if(view.sequence!==sequence)return;if(result.folders.length){view.folder=d.folder;view.children=result.folders;}paintDirectory(d.device,d.root);}
    } catch(error) { report(error); }
  };
  $('split-search-form').onsubmit=event=>{event.preventDefault();if(!scope&&searchScope!=='all')return;submittedSearch=$('split-search').value.trim();submittedScope=searchScope;offset=0;cursors=[''];previewCollection='results';setReferenceView('grid');loadItems();};
  $('split-search').onkeydown=event=>{event.stopPropagation();if(event.key==='Enter'&&event.isComposing)event.preventDefault();};
  $('split-prev-page').onclick=()=>{offset=Math.max(0,offset-60);loadItems();};$('split-next-page').onclick=()=>{offset+=60;loadItems();};
  $('split-result-prev').onclick=()=>$('split-prev-page').click();$('split-result-next').onclick=()=>$('split-next-page').click();
  setReferenceView(resultView,false);setGridScale(gridScale);
  function referenceTaskKey(){return JSON.stringify([window.FRAMEKEEP_SESSION?.id||'',window.FramekeepSku?.shootingTypes()?.project_id||'']);}
  function checkReferenceTask(){
    if(!preferencesReady)return;
    const key=referenceTaskKey();if(key===lastReferenceTask)return;lastReferenceTask=key;clearTimeout(preferencesTimer);
    preferencesSaving=preferencesSaving.catch(()=>{}).then(async()=>{
      const state=await api('task',{});referenceGeneration=state.cache_generation;
      if(!state.changed)return;
      references=[];referenceIndex=0;active=null;rows=[];resultTotal=0;scope=null;offset=0;cursors=[''];submittedSearch='';requestId++;menu.hidden=true;
      for(const url of objectUrls)URL.revokeObjectURL(url);objectUrls.clear();
      $('split-search').value='';$('split-search-submit').disabled=true;$('split-grid').replaceChildren();$('split-preview-grid').innerHTML='<p class="split-empty">新拍摄任务，请选择参考目录后搜索</p>';
      for(const id of ['split-prev-page','split-next-page','split-result-prev','split-result-next'])$(id).disabled=true;
      $('split-result-page').textContent='0 / 0';$('split-results').textContent='临时参考图已清理';referenceSignature='';previewCollection='results';
      for(const button of $('split-tree').querySelectorAll('[data-resource-root]')){button.setAttribute('aria-expanded','false');closedRoots.add(rootKey(button.dataset.device,button.dataset.root));button.closest('.split-resource-group').querySelector('.catalog-drive-folders').hidden=true;}
      setReferenceView('grid');preferencesSaved=JSON.stringify(preferencesSnapshot());try{localStorage.setItem(cacheKey,preferencesSaved);}catch{}
    });preferencesSaving.catch(report);
  }
  for(const event of ['framekeep:selection','framekeep:sku-project'])document.addEventListener(event,checkReferenceTask);
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
    if(value===enabled){syncResourceControls();return;}
    enabled=value;page.classList.toggle('split-enabled',value);$('layout-split').setAttribute('aria-pressed',String(value));$('layout-split').setAttribute('aria-checked',String(value));
    if(value){originalSides={left:page.classList.contains('left-collapsed'),right:page.classList.contains('right-collapsed')};if(!originalSides.right)$('layout-right').click();const current=Framekeep.getSnapshot().selectedId||Framekeep.photoItems()[0]?.photo.id;if(current)Framekeep.browsePhoto(current);}
    else {if(focusState)immersive();if(originalSides)for(const side of ['left','right'])if(page.classList.contains(side+'-collapsed')!==originalSides[side])$('layout-'+side).click();}
    window.dispatchEvent(new Event('resize'));
    saveReferences();
  }
  $('layout-split').onclick=()=>{layoutTouched=true;toggleSplit(!enabled);};$('split-exit').onclick=()=>{layoutTouched=true;toggleSplit(false);};
  // 将提示归属于实际预览容器；折叠、拖动栏宽和分屏时无需计算全窗中心。
  const toast=$('toast');
  function anchorToast(){const visible=!page.classList.contains('hidden');toast.classList.toggle('preview-anchored',visible);(visible?surface:document.body).append(toast);}
  new MutationObserver(anchorToast).observe(page,{attributes:true,attributeFilter:['class']});anchorToast();

  const settings=$('settings-library');
  settings.innerHTML=`<div class="settings-section-heading"><h3>文件数据库与 API</h3><p>把参考资料留在原设备，通过局域网只读检索和浏览。</p></div>
    <form id="split-sharing-form" class="split-service-note"><div><strong>允许局域网设备调用本机数据库</strong><p id="split-sharing-status">共享服务已关闭</p></div><label class="split-visibility-control"><input id="split-sharing-enabled" type="checkbox" role="switch"><span class="split-switch-track" aria-hidden="true"></span><span>启用共享</span></label><label>端口 <input id="split-sharing-port" type="number" min="1024" max="65535" value="8770" required aria-label="局域网共享端口"></label><button type="submit">应用</button></form>
    <section class="split-settings-card"><header><div><h4>本机文件数据库</h4><p>添加允许浏览的分区或文件夹，支持独立的本机快速索引。</p></div><button id="split-settings-add-root">＋ 添加分区</button></header><div id="split-shared-roots"></div><div class="split-service-note"><div><strong>调用 Everything 数据库</strong><p id="split-everything-status" class="split-muted"></p></div><label class="split-visibility-control"><input id="split-everything-enabled" type="checkbox" role="switch" aria-label="调用 Everything 数据库"><span class="split-switch-track" aria-hidden="true"></span><span>启用调用</span></label></div><p id="split-everything-message" role="status" class="split-muted"></p></section>
    <section class="split-settings-card" id="split-preview-cache"><header><div><h4>缩略图缓存</h4><p>本机监控与资产归档共用的图片缓存，包含缩略图及大图预览；同一素材的不同尺寸分别计数。</p></div></header><p id="split-preview-cache-state" role="status">正在读取缓存…</p><p class="split-muted">数量与空间自动刷新，缓存上限及迁移可在“预览与缓存”中设置。</p></section>
    <section class="split-settings-card" id="split-index-storage"><header><div><h4>文件索引数据库存储</h4><p>可迁移到 SSD 加快本机索引读写；迁移期间暂缓搜索，完成后自动恢复。</p></div></header><p>当前路径：<code id="split-index-storage-current"></code></p><label class="split-storage-label" for="split-index-storage-path">目标目录（空目录）</label><div class="split-storage-actions"><input id="split-index-storage-path" placeholder="选择高速硬盘上的空文件夹" autocomplete="off"><button id="split-index-storage-browse">选择目录</button><button id="split-index-storage-migrate" class="primary">迁移数据库</button></div><progress id="split-index-storage-progress" max="100" hidden aria-label="文件索引数据库迁移进度"></progress><p id="split-index-storage-message" role="status" class="split-muted"></p><p id="split-index-storage-previous" class="split-muted"></p><p class="split-muted">校验成功后立即切换，重启继续使用新位置，原数据库保留。迁移文件索引及倒排索引；原图和 SKU / 解析记录仍在原位置。预览缓存请在“预览与缓存”中迁移。Everything 自身数据库的位置由 Everything 管理。</p></section>
    <section class="split-settings-card"><header><div><h4>API Key</h4><p>按调用方命名和分配目录，可随时查看、复制或停用。</p></div><button id="split-create-key" class="primary">创建密钥</button></header><div class="split-endpoint"><span>本机只读接口</span><code id="split-local-endpoint"></code></div><div id="split-key-list"></div></section>
    <section class="split-settings-card"><header><div><h4>调用其他设备的文件数据库</h4><p>输入对方 IP + 端口，自动补齐前缀与接口后缀。</p></div></header><form id="split-peer-form"><div class="split-form-grid"><label>设备名称<input id="split-peer-name" placeholder="例如：摄影工作站 B" required maxlength="48"></label><label>IP + 端口<input id="split-peer-address" placeholder="192.168.1.23:8770" required></label><label class="split-full">对方 API Key<input id="split-peer-key" type="password" placeholder="fk_…" required autocomplete="off"></label></div><p id="split-address-preview" class="split-muted">http://IP:端口/api/library/v1</p><div class="split-form-actions"><span id="split-peer-message" role="status"></span><button type="submit" class="primary">连接并添加设备</button></div></form><div id="split-peer-list"></div></section>`;
  function showSettings(){ $('app-settings-open').click();document.querySelector('[data-settings-section="library"]').click();refreshSettings().catch(report); }
  $('split-api-settings').onclick=showSettings;
  const dialog=document.createElement('dialog');dialog.id='split-manage-dialog';document.body.append(dialog);
  function openDialog(title,content,handler){dialog.innerHTML=`<div class="dialog-heading"><h2>${title}</h2><button type="button" id="split-dialog-close" aria-label="关闭资源配置">×</button></div><form id="split-manage-form">${content}<p id="split-manage-message" role="status"></p><div class="dialog-actions"><button type="button" id="split-dialog-cancel">取消</button><button class="primary" type="submit">${title}</button></div></form>`;for(const id of ['split-dialog-close','split-dialog-cancel'])$(id).onclick=()=>dialog.close();$('split-manage-form').onsubmit=async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;try{await handler();}catch(error){$('split-manage-message').textContent=error.message;}finally{button.disabled=false;}};dialog.showModal();}
  function addRoot(){
    let entries=[],picking=false,submitting=false;
    const pathKey=value=>value.replace(/\\/g,'/').replace(/\/+$/,'').toLowerCase();
    const defaultName=value=>value.replace(/[\\/]+$/,'').split(/[\\/]/).pop()||value;
    openDialog('添加参考分区', '<div class="split-picker-heading"><p>选择磁盘分区或文件夹，支持一次多选，也可继续追加。</p><button type="button" id="split-browse-root" class="primary">选择分区 / 文件夹</button></div><div class="split-picker-summary"><strong>待添加位置</strong><span id="split-root-count">0 个</span></div><div id="split-root-selection" aria-live="polite"></div><details class="split-manual-path"><summary>手动输入网络或文件夹路径</summary><label for="split-root-path">每行一个路径</label><textarea id="split-root-path" rows="3" placeholder="例如：\\\\192.168.1.10\\共享\\参考"></textarea><button type="button" id="split-append-path">加入列表</button></details><p class="split-muted">后台建立只读索引，支持 RAW + JPG 合并显示。添加后可在设置中独立隐藏、显示或移除。</p>',async()=>{
      if(picking||submitting||!entries.length)return;
      submitting=true;form.setAttribute('aria-busy','true');render();
      try{
        const result=await api('add-roots',{entries});
        const added=new Set(result.added.map(row=>pathKey(row.path)));
        entries=entries.filter(row=>!added.has(pathKey(row.path)));render();
        await Promise.all([loadSources(),refreshSettings()]);
        if(result.errors.length){$('split-manage-message').textContent=`已添加 ${result.added.length} 个，${result.errors.length} 个未成功：`+result.errors.map(row=>defaultName(row.path)+' · '+row.error).join('；');return;}
        dialog.close();
        const first=result.added[0];
        if(first&&sources.find(row=>row.id==='local')?.roots.some(row=>row.id===first.root_id))await selectFolder('local',first.root_id,'',folderLabel('local',first.root_id));
      }finally{submitting=false;form.removeAttribute('aria-busy');if(dialog.open&&$('split-manage-form')===form)render();}
    });
    const form=$('split-manage-form'),selection=$('split-root-selection'),submit=form.querySelector('[type=submit]');
    function current(){return dialog.open&&$('split-manage-form')===form;}
    function render(){
      selection.innerHTML=entries.length?entries.map((row,index)=>`<div class="split-picked-root ${row.visible===false?'split-picked-hidden':''}"><span>${icon('folder')}</span><label><input data-root-name="${index}" aria-label="位置 ${index+1} 的显示名称" value="${esc(row.name)}" maxlength="48" ${submitting?'disabled':''}><small title="${esc(row.path)}">${esc(row.path)}</small></label><div class="split-picked-actions"><button type="button" data-unpick-root="${index}" aria-label="移除待添加位置 ${esc(row.name)}" title="移除" ${submitting?'disabled':''}>×</button><label class="split-visibility-control" title="${row.visible===false?'添加后隐藏，可在设置中重新显示':'添加后显示在参考资源栏'}"><input type="checkbox" role="switch" data-picked-visibility="${index}" aria-label="显示待添加位置 ${esc(row.name)}" ${row.visible===false?'':'checked'} ${submitting?'disabled':''}><span class="split-switch-track" aria-hidden="true"></span><span>${row.visible===false?'隐藏':'显示'}</span></label></div></div>`).join(''):'<p class="split-empty">尚未选择位置<br>点击上方按钮，在系统窗口中选择分区或文件夹</p>';
      $('split-root-count').textContent=entries.length+' 个';submit.textContent=entries.length?`添加 ${entries.length} 个位置`:'添加参考分区';submit.disabled=!entries.length||picking||submitting;
      for(const id of ['split-browse-root','split-append-path','split-root-path','split-dialog-close','split-dialog-cancel'])$(id).disabled=submitting||(id==='split-browse-root'&&picking);
    }
    function append(paths){
      let overflow=false;
      for(const value of paths){const path=String(value||'').trim().replace(/^"|"$/g,'');if(!path||entries.some(row=>pathKey(row.path)===pathKey(path)))continue;if(entries.length>=50){overflow=true;break;}entries.push({path,name:defaultName(path),visible:true});}
      $('split-manage-message').textContent=overflow?'每次最多添加 50 个位置，其余位置可在下一批添加。':'';render();
    }
    selection.oninput=event=>{if(event.target.matches('[data-root-name]'))entries[Number(event.target.dataset.rootName)].name=event.target.value;};
    selection.onchange=event=>{const control=event.target.closest('[data-picked-visibility]');if(!control||submitting)return;const row=entries[Number(control.dataset.pickedVisibility)];row.visible=control.checked;control.closest('.split-picked-root').classList.toggle('split-picked-hidden',!row.visible);control.parentElement.title=row.visible?'添加后显示在参考资源栏':'添加后隐藏，可在设置中重新显示';control.parentElement.lastElementChild.textContent=row.visible?'显示':'隐藏';};
    selection.onclick=event=>{const button=event.target.closest('[data-unpick-root]');if(button&&!submitting){entries.splice(Number(button.dataset.unpickRoot),1);render();}};
    $('split-append-path').onclick=()=>{append($('split-root-path').value.split(/\r?\n/));$('split-root-path').value='';};
    $('split-browse-root').onclick=async()=>{
      if(picking||submitting)return;picking=true;render();
      try{const result=window.FramekeepDesktop?await FramekeepDesktop.chooseFolders():await FramekeepIO.api('/api/choose-folder',{},180000);if(current())append(Array.isArray(result)?result:[result.folder]);}
      catch(error){if(current())$('split-manage-message').textContent=error.message;}
      finally{picking=false;if(current())render();}
    };
    dialog.addEventListener('cancel',event=>{if(current()&&submitting)event.preventDefault();},{signal:(()=>{const controller=new AbortController();dialog.addEventListener('close',()=>controller.abort(),{once:true});return controller.signal;})()});
    render();
  }
  $('split-add-root').onclick=addRoot;$('split-settings-add-root').onclick=addRoot;
  $('split-everything-enabled').onchange=async event=>{
    const control=event.target,enabled=control.checked;control.disabled=true;
    try{await api('everything',{enabled});$('split-everything-message').textContent=enabled?'已允许调用 Everything，未覆盖的位置使用本机索引':'已关闭调用，正在为需要的位置补建本机索引';await refreshSettings();}
    catch(error){control.checked=!enabled;$('split-everything-message').textContent=error.message;}
    finally{control.disabled=false;}
  };
  $('split-index-storage-browse').onclick=async()=>{
    const button=$('split-index-storage-browse');button.disabled=true;
    try{const result=window.FramekeepDesktop?await FramekeepDesktop.chooseFolders():await FramekeepIO.api('/api/choose-folder',{},180000);const path=Array.isArray(result)?result[0]:result.folder;if(path)$('split-index-storage-path').value=path;}
    catch(error){$('split-index-storage-message').textContent=error.message;}
    finally{button.disabled=false;}
  };
  $('split-index-storage-migrate').onclick=async()=>{
    const button=$('split-index-storage-migrate');button.disabled=true;
    try{await api('index-storage',{path:$('split-index-storage-path').value});await refreshSettings();}
    catch(error){$('split-index-storage-message').textContent=error.message;button.disabled=false;}
  };
  async function refreshSettings(){
    config=await api('config');
    $('split-shared-roots').innerHTML=config.roots.map(root=>`<div class="split-config-row ${root.hidden?'split-root-hidden':''}"><span>${icon('folder')}<strong>${esc(root.name)}</strong><small>${root.count||0} 张 · ${{queued:'等待索引',scanning:'正在索引',ready:'已就绪',error:'索引失败'}[root.status]||'待索引'}</small></span>${indexMarkup(root)}<code title="${esc(root.error||root.path)}">${esc(root.path)}</code><div class="split-root-actions"><button data-rescan-root="${root.id}">刷新索引</button><button class="split-remove-root" data-remove-root="${root.id}" aria-label="移除参考位置 ${esc(root.name)}" title="移除参考位置">×</button><label class="split-visibility-control"><input type="checkbox" role="switch" data-root-visibility="${root.id}" aria-label="显示参考位置 ${esc(root.name)}" ${root.hidden?'':'checked'}><span class="split-switch-track" aria-hidden="true"></span><span>${root.hidden?'已隐藏':'显示'}</span></label></div></div>`).join('')||'<p class="split-empty">添加需要浏览或共享的分区、文件夹。</p>';
    $('split-everything-enabled').checked=config.everything.enabled!==false;
    $('split-everything-status').textContent=config.everything.forced_off?'当前运行环境已停用 Everything':config.everything.enabled===false?'已关闭调用 · 使用本机快速索引':config.everything.available?'Everything 已连接 · 本机索引同样可独立搜索':'允许调用 · Everything 暂不可用，使用本机快速索引';
    const storage=config.index_storage,storageRunning=storage?.job?.status==='running';
    if(storage){
      $('split-index-storage-current').textContent=storage.path;
      $('split-index-storage-previous').textContent=storage.previous?'原数据库保留于：'+storage.previous:'';
      $('split-index-storage-message').textContent=storage.warning||storage.job.message||'选择独立空目录，迁移后立即生效。';
      $('split-index-storage-progress').hidden=!storageRunning;$('split-index-storage-progress').value=storage.job.percent||0;
      if(storageRunning)$('split-index-storage-message').textContent+=' · '+(storage.job.percent||0)+'%';
      for(const id of ['split-index-storage-path','split-index-storage-browse','split-index-storage-migrate'])$(id).disabled=storageRunning;
    }
    paintIndexProgress();
    $('split-local-endpoint').textContent=config.endpoint;
    if(!document.activeElement.closest('#split-sharing-form')){$('split-sharing-enabled').checked=config.sharing.enabled;$('split-sharing-port').value=config.sharing.port;}
    $('split-sharing-status').textContent=config.sharing.running?'共享已启动 · '+config.sharing.addresses.filter(value=>!value.includes('127.0.0.1')).join(' · '):config.sharing.error||'共享服务已关闭';
    $('split-key-list').innerHTML=config.keys.length?config.keys.map(key=>`<div class="split-key-row ${key.disabled?'disabled':''}"><div><strong>${esc(key.name)}</strong><span><code>${esc(key.prefix)}••••••</code> · ${key.disabled?'已停用':key.roots.length+' 个共享目录 · 只读'}</span></div><div><button data-assign-key="${key.id}" ${key.disabled?'disabled':''}>分配目录</button><button data-reveal-key="${key.id}" ${key.disabled?'disabled':''}>查看</button><button data-copy-key="${key.id}" ${key.disabled?'disabled':''}>复制</button><button data-disable-key="${key.id}" ${key.disabled?'disabled':''}>停用</button></div></div>`).join(''):'<p class="split-empty">尚未创建 API Key，点击右上角创建。</p>';
    $('split-peer-list').innerHTML=config.peers.map(peer=>`<div class="split-config-row"><span>${icon('server')}<strong>${esc(peer.name)}</strong></span><code>${esc(peer.address)}</code><button data-remove-peer="${peer.id}">断开</button></div>`).join('');
    scheduleIndexRefresh();
  }
  $('split-create-key').onclick=async()=>{await refreshSettings();openDialog('创建 API 密钥',`<label>分配给调用方<input id="split-key-name" maxlength="48" placeholder="例如：摄影工作站 B" required></label><p class="split-muted">可先创建密钥，稍后通过“分配目录”设置允许调用的位置。未分配目录时不开放资源访问。</p><fieldset><legend>允许调用的目录 · 只读 · 可选</legend>${config.roots.map(root=>`<label class="split-checkbox"><input type="checkbox" name="key-root" value="${root.id}" checked>${esc(root.name)}</label>`).join('')}</fieldset>`,async()=>{const result=await api('create-key',{name:$('split-key-name').value,roots:[...dialog.querySelectorAll('[name=key-root]:checked')].map(el=>el.value)});dialog.close();await refreshSettings();showKey(result.api_key);});};
  function showKey(value){dialog.innerHTML=`<div class="dialog-heading"><h2>API 密钥</h2><button id="split-key-done" aria-label="关闭密钥查看">×</button></div><p class="split-muted">可分配给指定调用方；之后仍可在本机查看。仅具有分配目录的读取权限。</p><code class="split-secret-value">${esc(value)}</code><div class="dialog-actions"><button id="split-secret-copy" class="primary">复制密钥</button></div>`;$('split-key-done').onclick=()=>dialog.close();$('split-secret-copy').onclick=async()=>{try{await navigator.clipboard.writeText(value);$('split-secret-copy').textContent='已复制';}catch{dialog.querySelector('.split-secret-value').focus();}};dialog.showModal();}
  function clearRootScope(id){if(scope?.device==='local'&&scope.root===id){scope=null;rows=[];resultTotal=0;requestId++;$('split-search-submit').disabled=true;$('split-grid').replaceChildren();$('split-preview-grid').replaceChildren();for(const id of ['split-prev-page','split-next-page','split-result-prev','split-result-next'])$(id).disabled=true;$('split-result-page').textContent='0 / 0';$('split-results').textContent='请选择参考目录';paintImage();}}
  settings.addEventListener('change',async event=>{
    const control=event.target.closest('[data-root-visibility]');if(!control)return;
    const visible=control.checked,id=control.dataset.rootVisibility;control.disabled=true;
    try{await api('root-visibility',{id,visible});if(!visible)clearRootScope(id);await Promise.all([refreshSettings(),loadSources()]);}
    catch(error){if(control.isConnected)control.checked=!visible;report(error);}
    finally{if(control.isConnected)control.disabled=false;}
  });
  settings.addEventListener('click',async event=>{const button=event.target.closest('[data-reveal-key],[data-copy-key],[data-disable-key],[data-assign-key],[data-remove-peer],[data-rescan-root],[data-remove-root]');if(!button)return;button.disabled=true;try{
    if(button.dataset.revealKey||button.dataset.copyKey){const result=await api('reveal-key',{id:button.dataset.revealKey||button.dataset.copyKey});if(button.dataset.copyKey){await navigator.clipboard.writeText(result.api_key);button.textContent='已复制';}else showKey(result.api_key);}
    else if(button.dataset.assignKey){const key=config.keys.find(key=>key.id===button.dataset.assignKey);openDialog('分配共享目录',`<p>${esc(key.name)}</p><fieldset><legend>允许调用的目录 · 只读</legend>${config.roots.map(root=>`<label class="split-checkbox"><input type="checkbox" name="key-root" value="${root.id}" ${key.roots.includes(root.id)?'checked':''}>${esc(root.name)}</label>`).join('')}</fieldset>`,async()=>{await api('assign-key',{id:key.id,roots:[...dialog.querySelectorAll('[name=key-root]:checked')].map(el=>el.value)});dialog.close();await refreshSettings();});}
    else if(button.dataset.disableKey){await api('disable-key',{id:button.dataset.disableKey});await refreshSettings();}
    else if(button.dataset.rescanRoot){await api('rescan',{id:button.dataset.rescanRoot});await refreshSettings();}
    else if(button.dataset.removeRoot){const id=button.dataset.removeRoot;await api('remove-root',{id});clearRootScope(id);await Promise.all([refreshSettings(),loadSources()]);}
    else {await api('remove-peer',{id:button.dataset.removePeer});await Promise.all([refreshSettings(),loadSources()]);}
  }catch(error){report(error);}finally{if(button.isConnected)button.disabled=false;}});
  $('split-sharing-form').onsubmit=async event=>{event.preventDefault();event.submitter.disabled=true;try{await api('sharing',{enabled:$('split-sharing-enabled').checked,port:Number($('split-sharing-port').value)});await refreshSettings();}catch(error){$('split-sharing-status').textContent=error.message;}finally{event.submitter.disabled=false;}};
  $('split-peer-address').oninput=()=>{const raw=$('split-peer-address').value.trim(),scheme=raw.startsWith('https://')?'https://':'http://';let value=raw.replace(/^https?:\/\//,'').replace(/\/(?:api(?:\/library(?:\/v1)?)?)?\/?$/,'');$('split-address-preview').textContent=value?scheme+value+(!value.includes(':')?':8770':'')+'/api/library/v1':'http://IP:端口/api/library/v1';};
  $('split-peer-form').onsubmit=async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;$('split-peer-message').textContent='正在验证密钥并读取共享目录…';try{await api('add-peer',{name:$('split-peer-name').value,address:$('split-peer-address').value,api_key:$('split-peer-key').value});$('split-peer-message').textContent='已连接，目录已加入参考资源栏';$('split-peer-key').value='';await Promise.all([refreshSettings(),loadSources()]);}catch(error){$('split-peer-message').textContent=error.message;}finally{button.disabled=false;}};
  document.querySelector('[data-settings-section="library"]').addEventListener('click',()=>refreshSettings().catch(report));
  try {
    const task=await api('task',{});referenceGeneration=task.cache_generation;referenceTaskReady=true;
    const saved=await api('preferences');preferencesToken=saved.request_token||'';
    if(saved.initialized){references=saved.references;referenceIndex=saved.index;trayCollapsed=saved.collapsed;$('split-reference-tray').open=!trayCollapsed;}
    await Promise.all([loadSources(),refreshSettings()]);
    const device=sources.find(d=>d.id==='local'),root=device?.roots[0];
    if(root&&!scope){
      if(submittedSearch||$('split-search').value.trim())scope={device:'local',root:root.id,folder:'',label:folderLabel('local',root.id)};
      else await selectFolder('local',root.id,'',folderLabel('local',root.id));
    }
    if(!layoutTouched)toggleSplit(saved.split_enabled===true);paintImage();lastReferenceTask=referenceTaskKey();preferencesReady=true;saveReferences();syncResourceControls();
  }catch(error){report(error);}
});
