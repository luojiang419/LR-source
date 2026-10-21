(() => {
  'use strict';
  if(!window.FramekeepIO||!window.Framekeep)return;
  const $=id=>document.getElementById(id),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const page=$('selection-page'),main=document.querySelector('.library-main');
  const storageKey='framekeep-layout-v1';let prefs={};try{prefs=JSON.parse(localStorage.getItem(storageKey)||'{}')}catch{}
  if(!prefs||typeof prefs!=='object'||Array.isArray(prefs))prefs={};
  let mode='selection',watch={enabled:false},focusBefore=null,active=false,lastSelected=null,lastSelectedIndex=-1,signature='',items=[],context=null;
  function persist(){try{localStorage.setItem(storageKey,JSON.stringify(prefs))}catch{}}
  function convert(selector){const old=document.querySelector(selector);if(!old)return;const node=document.createElement('details');for(const a of old.attributes)node.setAttribute(a.name,a.value);node.open=true;const heading=old.querySelector('.panel-title'),summary=document.createElement('summary');summary.className='panel-title';summary.append(...heading.childNodes);heading.remove();node.append(summary,...old.childNodes);old.replaceWith(node);summary.addEventListener('click',e=>{if(e.target.closest('button'))e.preventDefault()})}
  convert('.rating-section');convert('.folders');
  for(const side of ['left','right']){
    const panel=document.querySelector('.'+side+'-panel'),footer=panel.querySelector('.selection-sidebar-footer'),body=document.createElement('div');body.className='sidebar-body';body.id=side+'-panel-body';body.append(...[...panel.childNodes].filter(node=>node!==footer));
    const toggle=document.createElement('button');toggle.id=side+'-panel-toggle';toggle.className='panel-edge';toggle.setAttribute('aria-controls',body.id);panel.append(toggle,body);if(footer)panel.append(footer);toggle.onclick=()=>setSide(side,!page.classList.contains(side+'-collapsed'));
  }
  const rightBody=$('right-panel-body'),shootingPanel=$('current-shooting-panel');
  const photoPanel=document.createElement('section');photoPanel.id='photo-settings-panel';photoPanel.className='selection-sidebar-page';
  photoPanel.append(...[...rightBody.childNodes].filter(node=>node!==shootingPanel));
  const sidebarTabs=document.createElement('nav');sidebarTabs.className='selection-sidebar-tabs';sidebarTabs.setAttribute('role','tablist');sidebarTabs.setAttribute('aria-label','右侧栏页面');
  sidebarTabs.innerHTML='<button id="photo-settings-tab" role="tab" aria-controls="photo-settings-panel">照片设置</button><button id="current-shooting-tab" role="tab" aria-controls="current-shooting-panel">当前拍摄</button>';
  rightBody.append($('shooting-automation'),sidebarTabs,photoPanel,shootingPanel);
  const sidebarPages=[['photo-settings-tab',photoPanel,'photos'],['current-shooting-tab',shootingPanel,'shooting']];
  function showSidebar(name){
    for(const [id,panel,key] of sidebarPages){const active=name===key;$(id).setAttribute('aria-selected',String(active));$(id).tabIndex=active?0:-1;panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby',id);panel.classList.toggle('hidden',!active);panel.inert=!active}
    try{localStorage.setItem('framekeep-selection-sidebar-v1',name)}catch{}
  }
  sidebarTabs.addEventListener('click',event=>{const entry=sidebarPages.find(([id])=>id===event.target.closest('[role=tab]')?.id);if(entry)showSidebar(entry[2])});
  sidebarTabs.addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.stopPropagation();event.preventDefault();const index=sidebarPages.findIndex(([id])=>id===document.activeElement.id),next=event.key==='Home'?0:event.key==='End'?1:(index+1)%2;showSidebar(sidebarPages[next][2]);$(sidebarPages[next][0]).focus()});
  let sidebarPage='photos';try{if(localStorage.getItem('framekeep-selection-sidebar-v1')==='shooting')sidebarPage='shooting'}catch{}showSidebar(sidebarPage);
  window.FramekeepSidebar={show(name){if(!['photos','shooting'].includes(name))return;showSidebar(name);if(page.classList.contains('right-collapsed'))setSide('right',false)}};
  const controls=document.createElement('div');controls.className='workspace-layout-actions';controls.setAttribute('role','group');controls.setAttribute('aria-label','折叠工作区');controls.innerHTML='<button id="layout-header">顶部</button><button id="layout-left">左栏</button><button id="layout-right">右栏</button><button id="layout-tools">工具</button><button id="layout-focus">专注预览</button>';
  const tabs=document.querySelector('.workspace-tabs'),navigation=document.createElement('div');navigation.className='workspace-navigation';tabs.before(navigation);navigation.append(controls,tabs);
  new MutationObserver(()=>controls.classList.toggle('hidden',page.classList.contains('hidden'))).observe(page,{attributes:true,attributeFilter:['class']});
  const tools=document.createElement('details');tools.id='selection-tools';tools.className='selection-tools';tools.open=true;tools.innerHTML='<summary>选片工具</summary>';main.prepend(tools);
  for(const selector of ['.filterbar']){const el=main.querySelector(selector);if(el)tools.append(el)}
  tools.querySelector('.filterbar').insertBefore(main.querySelector('.browse-status'),$('search'));
  const previewTools=main.querySelector('.loupe-heading'),viewbar=main.querySelector('.viewbar'),caption=main.querySelector('.loupe-caption');
  viewbar.insertBefore(previewTools,viewbar.querySelector('.thumbnail-control'));
  const previewSurface=document.createElement('div');previewSurface.className='preview-surface';
  const catalog=$('catalog-view'),thumbnail=viewbar.querySelector('.thumbnail-control');
  catalog.before(previewSurface);previewSurface.append(catalog,$('loupe-view'),thumbnail);
  thumbnail.setAttribute('role','group');
  caption.classList.add('preview-toolbar');caption.prepend(viewbar);main.append(caption);
  viewbar.addEventListener('wheel',event=>{
    if(event.ctrlKey||event.metaKey||Math.abs(event.deltaX)>=Math.abs(event.deltaY)||event.target.closest('input[type=range],select'))return;
    const before=viewbar.scrollLeft;viewbar.scrollLeft+=event.deltaY;if(viewbar.scrollLeft!==before)event.preventDefault();
  },{passive:false});
  function wrapProgress(nodes,id,title){const detail=document.createElement('details');detail.id=id;detail.className='progress-fold';detail.open=true;const summary=document.createElement('summary');summary.textContent=title;detail.append(summary);nodes[0].before(detail);detail.append(...nodes)}
  const summary=$('sku-progress-summary');if(summary){const nodes=[];for(let node=document.querySelector('.progress-heading').nextElementSibling;node&&node!==$('sku-sheet-panel');node=node.nextElementSibling)nodes.push(node);if(nodes.length)wrapProgress(nodes,'progress-controls','拍摄任务与工作表');wrapProgress([summary],'progress-statistics','当前页进度');const list=document.createElement('section');list.id='progress-table';list.className='progress-list';list.setAttribute('aria-label','款号清单');list.innerHTML='<h3><span>款号清单</span><span id="sku-search-summary" role="status" aria-live="polite" hidden></span></h3>';$('sku-rows').before(list);list.append($('sku-rows'))}
  if($('progress-controls')&&$('progress-statistics')){const overview=document.createElement('div');overview.className='progress-overview';document.querySelector('.progress-heading').after(overview);overview.append($('progress-controls'),$('progress-statistics'))}
  if($('progress-table'))$('progress-table').insertBefore(document.querySelector('.progress-note'),$('sku-rows'));
  if($('progress-controls')&&$('progress-statistics')){
    const workspace=document.createElement('div');workspace.className='progress-workspace';
    const content=document.createElement('div');content.className='progress-main';
    const toggle=document.createElement('button');toggle.id='progress-sidebar-toggle';toggle.className='bordered';toggle.textContent='拍摄设置';toggle.setAttribute('aria-controls','progress-sidebar');
    const sidebar=document.createElement('aside');sidebar.id='progress-sidebar';sidebar.className='progress-sidebar';sidebar.setAttribute('aria-label','拍摄进度设置与统计');
    const scroll=document.createElement('div');scroll.id='progress-sidebar-scroll';scroll.className='progress-sidebar-scroll';
    const search=document.createElement('label'),searchInput=$('sku-search');search.className='progress-search-field';search.textContent='查找款号';searchInput.type='search';searchInput.placeholder='输入款号快速查找';search.append(searchInput);
    const filters=document.querySelector('.progress-filters');filters.prepend(search);filters.classList.add('progress-sidebar-search');
    const actions=document.querySelector('.progress-heading-actions');actions.classList.add('progress-sidebar-footer');
    const close=document.createElement('button');close.className='progress-sidebar-close bordered';close.textContent='收起设置';close.setAttribute('aria-label','收起拍摄进度侧栏');
    $('sku-dialog').append(workspace);workspace.append(content,sidebar);content.append(toggle,$('sku-sheet-panel'));
    sidebar.append(close,document.querySelector('.progress-heading'),filters,scroll,actions);
    scroll.append($('sku-sheet-tabs'),document.querySelector('.progress-overview'),document.querySelector('.progress-tools'),$('sku-message'),$('sku-records'));
    const narrow=matchMedia('(max-width:760px)');
    function syncSidebar(){const open=!narrow.matches||workspace.classList.contains('sidebar-open');toggle.setAttribute('aria-expanded',String(open));sidebar.inert=!open}
    toggle.onclick=()=>{workspace.classList.toggle('sidebar-open');syncSidebar()};
    close.onclick=()=>{workspace.classList.remove('sidebar-open');syncSidebar();toggle.focus()};
    document.addEventListener('keydown',e=>{if(e.key==='Escape'&&narrow.matches&&workspace.classList.contains('sidebar-open')&&!$('sku-dialog').classList.contains('hidden')&&!document.querySelector('dialog[open]')){e.preventDefault();e.stopPropagation();close.click()}},true);
    narrow.addEventListener('change',syncSidebar);syncSidebar();
  }
  const folds=[...document.querySelectorAll('.sidebar-body details,.shooting-progress details.progress-fold,#sku-records')];
  const foldDefaults=new Map(folds.map(el=>[el,el.open]));
  folds.forEach((el,index)=>{el.dataset.foldKey=el.id||'section-'+index;el.addEventListener('toggle',()=>{profile().sections[el.dataset.foldKey]=el.open;persist()})});
  function profile(){return prefs[mode]||(prefs[mode]={header:true,left:false,right:false,tools:mode==='selection',sections:{}})}
  let resizeFrame=0;
  function resize(){if(resizeFrame)return;resizeFrame=requestAnimationFrame(()=>{resizeFrame=0;window.FramekeepViewer?.refresh();renderThumbs(true)})}
  function setSide(side,collapsed,save=true){page.classList.toggle(side+'-collapsed',collapsed);const button=$(side+'-panel-toggle');button.textContent=(collapsed?'▸':'▾')+(collapsed?'':side==='left'?' 左侧栏':' 右侧栏');button.title=(collapsed?'展开':'折叠')+(side==='left'?'左侧栏':'右侧栏');button.setAttribute('aria-expanded',String(!collapsed));$('layout-'+side).setAttribute('aria-pressed',String(!collapsed));if(save){profile()[side]=collapsed;persist()}resize()}
  function apply(){const p=profile();p.sections ||= {};document.querySelector('.app-header').classList.toggle('hidden',p.header===false);$('layout-header').setAttribute('aria-pressed',String(p.header!==false));setSide('left',!!p.left,false);setSide('right',!!p.right,false);tools.open=p.tools!==false;folds.forEach(el=>{el.open=p.sections[el.dataset.foldKey]??(mode==='monitor'&&el.closest('.right-panel')?el.classList.contains('rating-section'):foldDefaults.get(el))});$('layout-tools').setAttribute('aria-pressed',String(tools.open));resize()}
  $('layout-header').onclick=()=>{profile().header=profile().header===false;apply();persist()};$('layout-left').onclick=()=>$('left-panel-toggle').click();$('layout-right').onclick=()=>$('right-panel-toggle').click();$('layout-tools').onclick=()=>{tools.open=!tools.open};
  $('layout-focus').onclick=()=>{if(focusBefore){prefs[mode]=focusBefore;focusBefore=null}else{focusBefore=structuredClone(profile());Object.assign(profile(),{header:false,left:true,right:true,tools:false})}apply();persist();$('layout-focus').setAttribute('aria-pressed',String(!!focusBefore))};
  tools.addEventListener('toggle',()=>{profile().tools=tools.open;$('layout-tools').setAttribute('aria-pressed',String(tools.open));persist();resize()});
  const browser=document.createElement('section');browser.id='watch-browser';browser.className='watch-browser';browser.innerHTML='<div class="watch-browser-heading"><strong>实时监控</strong><span id="watch-browser-count"></span></div><p id="watch-browser-state"></p><button id="watch-follow-toggle"></button><div id="watch-thumbnails" class="watch-thumbnails" aria-label="监控照片缩略图"></div>';
  document.querySelector('.folders').after(browser);
  $('watch-follow-toggle').onclick=()=>{FramekeepWatch.setFollow(!FramekeepWatch.getFollow());paintWatch()};
  function paintWatch(){$('watch-follow-toggle').textContent=window.FramekeepWatch?.getFollow()?'跟随新图 · 已开启':'跟随已暂停 · 点击恢复';$('watch-follow-toggle').setAttribute('aria-pressed',String(!!window.FramekeepWatch?.getFollow()));$('watch-browser-state').textContent=watch.error?watch.message+'：'+watch.error:watch.message||'等待新照片稳定入库';$('watch-browser-state').title=watch.folder||''}
  function syncMode(){const next=!!watch.enabled&&watch.session_id===FRAMEKEEP_SESSION.id;if(next===active)return;active=next;mode=active?'monitor':'selection';focusBefore=null;$('layout-focus').setAttribute('aria-pressed','false');page.classList.toggle('monitoring',active);document.querySelector('.folders').classList.toggle('hidden',active);document.querySelector('.left-footer').classList.toggle('hidden',active);lastSelected=null;signature='';apply();Framekeep.setMonitoring(active)}
  const previewRatios=new Map();let thumbSession=null;
  let labelsSession='',skuLabels=new Map(),skuIntervalCount=0,labelRequest=null,labelSequence=0;
  const photoSku=photo=>labelsSession===FRAMEKEEP_SESSION.id?skuLabels.get(photo.id)||'':'';
  function ratio(photo){
    if(previewRatios.has(photo.preview))return previewRatios.get(photo.preview);
    const rotated=[5,6,7,8].includes(Number(photo.orientation)),w=Number(photo.width),h=Number(photo.height);
    return w>0&&h>0?Math.max(.2,Math.min(5,rotated?h/w:w/h)):1.5;
  }
  function renderThumbs(force=false){
    const strip=$('watch-thumbnails');
    items=Framekeep.photoItems(true);$('watch-browser-count').textContent=items.length+' 张'+(labelsSession===FRAMEKEEP_SESSION.id&&skuIntervalCount?' · '+skuIntervalCount+' 段':'');
    $('watch-browser-count').title=labelsSession===FRAMEKEEP_SESSION.id&&skuIntervalCount?`${skuIntervalCount} 个 SKU 结束区间；补识别标签后自动重算`:'';
    browser.classList.toggle('hidden',!items.length&&!active);
    browser.querySelector('strong').textContent=active?'实时监控':'照片浏览';
    strip.setAttribute('aria-label',active?'监控照片缩略图':'照片缩略图');
    $('watch-browser-state').classList.toggle('hidden',!active);$('watch-follow-toggle').classList.toggle('hidden',!active);
    page.classList.toggle('has-sidebar-photos',!!items.length);
    if(thumbSession!==FRAMEKEEP_SESSION.id){thumbSession=FRAMEKEEP_SESSION.id;lastSelected=null;signature='';strip.scrollTop=0}
    if(!strip.clientWidth)return;
    const selected=Framekeep.remoteTarget().photoId,index=items.findIndex(i=>i.photo.id===selected),width=Math.max(1,strip.clientWidth-20);
    const heights=items.map(i=>Math.round(width/ratio(i.photo))+16),offsets=[0];
    heights.forEach(h=>offsets.push(offsets.at(-1)+h));
    let desired=Math.min(strip.scrollTop,Math.max(0,offsets.at(-1)-strip.clientHeight));
    if(selected!==lastSelected||index!==lastSelectedIndex||force){lastSelected=selected;lastSelectedIndex=index;if(index>=0&&(offsets[index]<desired||offsets[index+1]>desired+strip.clientHeight))desired=Math.max(0,(offsets[index]+offsets[index+1]-strip.clientHeight)/2)}
    function at(position){let low=0,high=items.length;while(low<high){const mid=(low+high)>>1;if(offsets[mid+1]<=position)low=mid+1;else high=mid}return low}
    const start=Math.max(0,at(desired)-2),end=Math.min(items.length,at(desired+strip.clientHeight)+3),rows=items.slice(start,end);
    const key=JSON.stringify([selected,start,end,width,rows.map((i,n)=>[i.photo.id,i.photo.preview,i.rating,photoSku(i.photo),heights[start+n]])]);
    if(key===signature){strip.scrollTop=desired;return}signature=key;
    strip.innerHTML=`<div aria-hidden="true" style="height:${offsets[start]}px"></div>`+rows.map(({photo:p,rating:r},n)=>`<button class="watch-thumb ${p.id===selected?'active':''} ${window.FramekeepAssets?.selection().includes(p.id)?'asset-selected':''}" style="height:${heights[start+n]}px" data-watch-photo="${esc(p.id)}" aria-current="${p.id===selected}" title="${esc(p.jpg||p.raw||p.name)}" aria-label="${esc((p.jpg||p.raw||p.name).split(/[\/]/).at(-1))}，${r>0?r+' 星':r<0?'暂不选':'待标星'}"><img src="${esc(p.preview)}" alt="${esc(p.name||p.id)}" loading="lazy">${photoSku(p)?`<span class="watch-sku-label" title="OCR SKU: ${esc(photoSku(p))}">SKU:${esc(photoSku(p))}</span>`:''}</button>`).join('')+`<div aria-hidden="true" style="height:${Math.max(0,offsets.at(-1)-offsets[end])}px"></div>`;
    strip.scrollTop=desired;
  }
  $('watch-thumbnails').addEventListener('load',event=>{
    const image=event.target,button=image.closest('[data-watch-photo]');if(!button||!image.naturalWidth||!image.naturalHeight)return;
    const next=Math.max(.2,Math.min(5,image.naturalWidth/image.naturalHeight));
    if(Math.abs((previewRatios.get(image.getAttribute('src'))||0)-next)>.001){previewRatios.set(image.getAttribute('src'),next);requestAnimationFrame(()=>renderThumbs())}
  },true);
  let lastStripWidth=0;
  new ResizeObserver(entries=>{const width=Math.round(entries[0].contentRect.width);if(width&&width!==lastStripWidth){lastStripWidth=width;resize()}}).observe($('watch-thumbnails'));
  $('watch-thumbnails').addEventListener('scroll',()=>renderThumbs(),{passive:true});$('watch-thumbnails').addEventListener('click',e=>{const button=e.target.closest('[data-watch-photo]');if(button)Framekeep.browsePhoto(button.dataset.watchPhoto)});
  document.addEventListener('framekeep:watch',e=>{watch=e.detail;syncMode();paintWatch();renderThumbs()});
  const menu=document.createElement('div');menu.id='photo-context-menu';menu.className='photo-context-menu';menu.setAttribute('role','menu');menu.hidden=true;document.body.append(menu);
  function closeMenu(){if(menu.hidden)return;menu.hidden=true;context=null;Framekeep.setInteractionPaused(false)}
  document.addEventListener('framekeep:selection',()=>{if(context&&context.sid!==FRAMEKEEP_SESSION.id)closeMenu();syncMode();renderThumbs()});
  document.addEventListener('contextmenu',e=>{const target=e.target.closest('[data-watch-photo],[data-photo],[data-loupe]');if(!target)return;const cid=target.dataset.watchPhoto||target.dataset.photo||target.dataset.loupe,item=Framekeep.photoItems().find(i=>i.photo.id===cid);if(!item)return;e.preventDefault();context={sid:FRAMEKEEP_SESSION.id,cid};menu.innerHTML='<button role="menuitem" data-reveal="">在文件夹中显示并选中</button>'+(item.photo.raw&&item.photo.jpg?'<button role="menuitem" data-reveal="raw">选中 RAW 原文件</button>':'');menu.hidden=false;menu.style.left=Math.max(4,Math.min(e.clientX,innerWidth-menu.offsetWidth-4))+'px';menu.style.top=Math.max(4,Math.min(e.clientY,innerHeight-menu.offsetHeight-4))+'px';Framekeep.setInteractionPaused(true);menu.querySelector('button').focus()});
  menu.addEventListener('click',async e=>{const button=e.target.closest('[data-reveal]');if(!button||!context)return;const chosen={...context,component:button.dataset.reveal||null};closeMenu();try{await FramekeepIO.api('/api/reveal-file',{session_id:chosen.sid,capture_id:chosen.cid,component:chosen.component})}catch(err){Framekeep.notify(err.message)}});
  document.addEventListener('pointerdown',e=>{if(!menu.contains(e.target))closeMenu()},true);document.addEventListener('keydown',e=>{if(!menu.hidden&&e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();closeMenu()}},true);window.addEventListener('blur',closeMenu);
  window.FramekeepLayout={setMonitoring:state=>{watch=state;syncMode();paintWatch();renderThumbs()},isMonitoring:()=>active,refresh:resize};
  async function loadSkuLabels(){
    if(document.hidden||page.classList.contains('hidden'))return;
    const sid=FRAMEKEEP_SESSION.id;if(!sid)return;
    if(labelRequest&&labelRequest.sid===sid)return;
    labelRequest?.controller.abort();const controller=new AbortController(),sequence=++labelSequence;labelRequest={sid,controller};
    try{
      const result=await FramekeepIO.api('/api/sku/library/labels?'+new URLSearchParams({session_id:sid}),undefined,8000,controller.signal);
      if(sequence!==labelSequence||sid!==FRAMEKEEP_SESSION.id||result.session_id!==sid)return;
      const next=new Map(Object.entries(result.labels||{}));
      if(labelsSession!==sid||skuIntervalCount!==result.interval_count||JSON.stringify([...next])!==JSON.stringify([...skuLabels])){labelsSession=sid;skuLabels=next;skuIntervalCount=result.interval_count||0;signature='';renderThumbs()}
    }catch{}finally{if(labelRequest?.controller===controller)labelRequest=null}
  }
  document.addEventListener('framekeep:selection',()=>{if(labelsSession!==FRAMEKEEP_SESSION.id)loadSkuLabels()});
  document.addEventListener('framekeep:workspace',loadSkuLabels);
  document.addEventListener('framekeep:ocr-result',loadSkuLabels);
  const labelTimer=setInterval(loadSkuLabels,2000);
  window.addEventListener('pagehide',()=>{clearInterval(labelTimer);labelRequest?.controller.abort()},{once:true});
  apply();paintWatch();renderThumbs();loadSkuLabels();
})();
