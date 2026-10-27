/* 只迁移既有组件的位置；功能与事件全部来自当前正式页面脚本。 */
const startWorkbenchUi=()=>{
  'use strict';
  if(!window.FramekeepLayout||!window.FramekeepTheme)return;
  const $=id=>document.getElementById(id);
  const page=$('selection-page');
  $('selection-tab').textContent='现场拍摄';
  document.querySelector('.identity>span:last-child').textContent='现场拍摄';
  document.querySelector('.folders .panel-title>strong').textContent='拍摄目录';
  for(const id of ['open-folder','add-folder'])$(id).classList.add('capture-secondary-hidden');
  $('threshold').closest('details').classList.add('capture-secondary-hidden');
  document.querySelector('.right-panel').setAttribute('aria-label','现场评级和照片信息');
  const folderFooter=document.createElement('div');folderFooter.className='folder-sidebar-footer';
  folderFooter.setAttribute('role','group');folderFooter.setAttribute('aria-label','文件夹与监控操作');
  folderFooter.append($('open-folder'),$('canon-tether-open'),$('folder-watch'));
  $('add-folder-row').remove();
  document.querySelector('.left-panel').append(folderFooter);
  // 底部保留页面导航，选片筛选并入顶栏以腾出图片区高度。
  const tabs=document.querySelector('.workspace-tabs');
  const bottom=document.createElement('footer');bottom.className='workspace-bottom-bar';
  const icons={
    'asset-retrieval-open':'<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6M7 10h6M10 7v6"/>',
    'selection-tab':'<rect x="3" y="3" width="18" height="18" rx="2"/><path d="m3 16 5-5 4 4 4-6 5 7"/><circle cx="8" cy="7" r="1"/>',
    'sku-open':'<path d="M8 3h8v4H8zM8 5H5v16h14V5h-3M8 12h8M8 16h5"/>',
    'asset-archive-open':'<path d="M3 4h18v5H3zM5 9v12h14V9M9 13h6"/>',
    'asset-analysis-open':'<path d="M8 3h8l4 4v14H4V3zM15 3v5h5M8 12h8M8 16h5"/>',
    'app-settings-open':'<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/>'
  };
  tabs.append($('app-settings-open'));
  $('sku-database-open').after($('asset-retrieval-open'));
  for(const tab of tabs.querySelectorAll('[role=tab]')){
    const label=document.createElement('span');label.className='workspace-tab-label';label.textContent=tab.textContent;
    tab.innerHTML=`<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${icons[tab.id]||''}</svg>`;tab.append(label);
  }
  bottom.append(tabs);const workbench=document.querySelector('.workbench');workbench.classList.add('workspace-with-bottom-tabs');workbench.append(bottom);window.FramekeepTaskStatus?.mount();
  const navigation=document.querySelector('.workspace-navigation'),tools=$('selection-tools');
  const toolContent=document.createElement('div');toolContent.className='workspace-tool-content';
  toolContent.append(...[...tools.children].filter(node=>node.tagName!=='SUMMARY'));
  toolContent.addEventListener('wheel',event=>{
    if(event.ctrlKey||event.metaKey||Math.abs(event.deltaX)>=Math.abs(event.deltaY)||event.target.closest('input[type=range],select'))return;
    const before=toolContent.scrollLeft;toolContent.scrollLeft+=event.deltaY;
    if(toolContent.scrollLeft!==before)event.preventDefault();
  },{passive:false});
  tools.append(toolContent);document.querySelector('.identity').after(tools);
  document.querySelector('.app-header').classList.add('header-with-tools');
  function syncHeaderTools(){
    tools.classList.toggle('hidden',page.classList.contains('hidden'));
  }
  const navigationObserver=new MutationObserver(syncHeaderTools);
  navigationObserver.observe(page,{attributes:true,attributeFilter:['class']});
  syncHeaderTools();
  // 页面名称与操作复用应用顶栏，内容区仅保留实际工作面板。
  const pageHeading=document.createElement('div');pageHeading.className='workspace-header-heading';pageHeading.hidden=true;
  document.querySelector('.identity').after(pageHeading);
  const headings=new Map([
    ['retrieval',document.querySelector('#asset-retrieval-page>.retrieval-heading')],
    ['archive',document.querySelector('#asset-archive-dialog>.catalog-heading')],
    ['sku-database',document.querySelector('#sku-database-page>.catalog-heading')],
    ['analysis',document.querySelector('#analysis-dialog>.analysis-heading')]
  ]);
  for(const [name,heading] of headings){
    if(!heading)continue;
    if(name==='retrieval'){
      heading.querySelector('.retrieval-navigation')?.remove();
      heading.classList.remove('retrieval-desktop-heading');
    }
    heading.dataset.workspaceHeading=name;
    heading.hidden=true;pageHeading.append(heading);
  }
  function syncPageHeading(){
    const name=FramekeepWorkspace.current();let visible=false;
    workbench.classList.toggle('workspace-retrieval-active',name==='retrieval');
    for(const [key,heading] of headings){if(!heading)continue;heading.hidden=key!==name;visible ||= key===name;}
    pageHeading.hidden=!visible;
    document.querySelector('.identity>span:last-child').hidden=visible;
  }
  document.addEventListener('framekeep:workspace',syncPageHeading);syncPageHeading();
  const parsing=document.createElement('span');parsing.id='capture-parsing-status';parsing.className='capture-parsing-status';
  parsing.setAttribute('role','status');parsing.setAttribute('aria-live','polite');
  parsing.innerHTML='<span id="capture-ocr-status">OCR 未解析</span><span id="capture-sku-result">SKU:—</span><span id="capture-multimodal-status" data-status="unparsed"><i class="parsing-status-light" aria-hidden="true"></i><span>多模态 未解析</span></span>';
  $('loupe-name').after(parsing);
  const parsed=new Set(['completed','matched','confirmed']);
  function parsingLabel(status){return parsed.has(status)?'已解析':({queued:'排队中',retry:'等待重试',waiting_model:'等待模型',paused:'已暂停',manual:'等待手动开始',running:'解析中',review:'待核对',unmatched:'未匹配',failed:'解析失败',cancelled:'已取消',ignored:'已忽略',stale:'已失效',loading:'读取中',unknown:'状态不可用'}[status]||'未解析')}
  function paintParsing(data){
    document.dispatchEvent(new CustomEvent('framekeep:capture-status',{detail:{session_id:parsingSession,capture_id:parsingCapture,...data}}));
    const ocr=data.ocr||{},multi=data.multimodal||{};
    $('capture-ocr-status').textContent='OCR '+parsingLabel(ocr.status);
    $('capture-ocr-status').title=ocr.reason||'';
    const codes=Array.isArray(ocr.codes)?ocr.codes:[];
    $('capture-sku-result').textContent='SKU:'+(codes.length?codes.join(' / '):'—');
    $('capture-sku-result').title=codes.length>1?'OCR 读出多个候选款号，请核对：'+codes.join(' / '):codes[0]||'尚无 OCR 款号结果';
    const indicator=$('capture-multimodal-status');indicator.dataset.status=parsed.has(multi.status)?'completed':multi.status||'unparsed';
    indicator.querySelector('span').textContent='多模态 '+parsingLabel(multi.status);indicator.title=multi.reason||'多模态解析状态';
  }
  let parsingKey='',parsingRequest=null,parsingSequence=0,parsingSession='',parsingCapture='';
  async function syncParsing(force=false){
    const target=Framekeep.remoteTarget(),key=target.photoId?JSON.stringify([target.sessionId,target.photoId]):'';
    if(document.hidden||page.classList.contains('hidden')){parsingRequest?.abort();parsingRequest=null;parsingKey='';return}
    if(key===parsingKey&&(!force||parsingRequest))return;
    const changed=key!==parsingKey;parsingKey=key;parsingSession=target.sessionId;parsingCapture=target.photoId;parsingRequest?.abort();const sequence=++parsingSequence;
    if(!key){paintParsing({});return}
    if(changed)paintParsing({ocr:{status:'loading'},multimodal:{status:'loading'}});
    const controller=new AbortController();parsingRequest=controller;
    try{
      const data=await FramekeepIO.api('/api/parsing/capture?'+new URLSearchParams({session_id:target.sessionId,capture_id:target.photoId}),undefined,8000,controller.signal);
      if(sequence===parsingSequence&&!controller.signal.aborted&&data.session_id===target.sessionId&&data.capture_id===target.photoId)paintParsing(data);
    }catch(error){if(sequence===parsingSequence&&!controller.signal.aborted)paintParsing({ocr:{status:'unknown',reason:error.message},multimodal:{status:'unknown',reason:error.message}})}
    finally{if(parsingRequest===controller)parsingRequest=null}
  }
  document.addEventListener('framekeep:selection',()=>syncParsing());
  document.addEventListener('framekeep:workspace',()=>syncParsing());
  document.addEventListener('framekeep:ocr-refresh',()=>{parsingRequest?.abort();parsingRequest=null;syncParsing(true)});
  document.addEventListener('visibilitychange',()=>syncParsing(true));
  const parsingTimer=setInterval(()=>syncParsing(true),2000);
  window.addEventListener('pagehide',()=>{clearInterval(parsingTimer);parsingRequest?.abort()},{once:true});
  syncParsing();
  // 复用原折叠按钮与状态，只将入口收进顶栏的「视图」菜单。
  const layoutControls=document.querySelector('.workspace-layout-actions');
  const viewMenu=document.createElement('div');viewMenu.className='view-menu';
  const viewButton=document.createElement('button');viewButton.id='view-open';viewButton.type='button';viewButton.className='bordered';viewButton.textContent='视图';
  viewButton.setAttribute('aria-haspopup','menu');viewButton.setAttribute('aria-expanded','false');viewButton.setAttribute('aria-controls','view-options');
  const viewPanel=document.createElement('div');viewPanel.className='view-menu-panel';viewPanel.hidden=true;
  layoutControls.id='view-options';layoutControls.setAttribute('role','menu');layoutControls.setAttribute('aria-label','视图显示选项');
  const splitButton=document.createElement('button');splitButton.id='layout-split';splitButton.textContent='分屏拍摄';splitButton.setAttribute('aria-pressed','false');layoutControls.insertBefore(splitButton,$('layout-focus'));
  const viewItems=[...layoutControls.querySelectorAll('button')];
  const viewLabels={'layout-header':'顶部工具栏','layout-left':'左侧面板','layout-right':'右侧面板','layout-tools':'选片工具','layout-focus':'专注预览'};
  for(const item of viewItems){item.textContent=viewLabels[item.id]||item.textContent;item.setAttribute('aria-label',item.textContent);item.setAttribute('role','menuitemcheckbox');item.tabIndex=-1;}
  const themeButton=$('theme-open');
  viewPanel.append(layoutControls);viewMenu.append(viewButton,viewPanel);themeButton.before(viewMenu);
  navigation.remove();
  let viewCloseTimer;
  function positionViewMenu(){
    viewPanel.style.left='0px';const box=viewPanel.getBoundingClientRect();
    const edge=document.documentElement.clientWidth-12;
    const shift=box.right>edge?edge-box.right:box.left<12?12-box.left:0;
    viewPanel.style.left=shift+'px';
  }
  function showViewMenu(open){
    clearTimeout(viewCloseTimer);open=!!open&&!viewButton.disabled;
    viewPanel.hidden=!open;viewButton.setAttribute('aria-expanded',String(open));
    if(open)positionViewMenu();
  }
  function syncViewMenu(){
    for(const item of viewItems)item.setAttribute('aria-checked',String(item.getAttribute('aria-pressed')==='true'));
    viewButton.disabled=layoutControls.classList.contains('hidden');
    const headerHidden=document.querySelector('.app-header').classList.contains('hidden');
    viewMenu.classList.toggle('view-menu-detached',headerHidden);
    if(headerHidden){if(viewMenu.parentElement!==document.querySelector('.workbench'))document.querySelector('.workbench').append(viewMenu);}
    else if(viewMenu.nextElementSibling!==themeButton)themeButton.before(viewMenu);
    if(viewButton.disabled)showViewMenu(false);
    else if(!viewPanel.hidden)positionViewMenu();
  }
  viewMenu.addEventListener('pointerenter',()=>showViewMenu(true));
  viewMenu.addEventListener('pointerleave',()=>{viewCloseTimer=setTimeout(()=>showViewMenu(false),160);});
  viewButton.addEventListener('click',()=>showViewMenu(true));
  viewButton.addEventListener('keydown',event=>{
    if(event.key==='ArrowDown'||event.key==='ArrowUp'){
      event.preventDefault();showViewMenu(true);viewItems[event.key==='ArrowDown'?0:viewItems.length-1].focus();
    }
  });
  viewPanel.addEventListener('keydown',event=>{
    const index=viewItems.indexOf(document.activeElement);let next;
    if(event.key==='ArrowDown')next=(index+1)%viewItems.length;
    if(event.key==='ArrowUp')next=(index+viewItems.length-1)%viewItems.length;
    if(event.key==='Home')next=0;if(event.key==='End')next=viewItems.length-1;
    if(next!==undefined){event.preventDefault();viewItems[next].focus();}
  });
  viewMenu.addEventListener('focusout',event=>{if(!viewMenu.contains(event.relatedTarget))showViewMenu(false);});
  viewMenu.addEventListener('keydown',event=>event.stopPropagation());
  document.addEventListener('pointerdown',event=>{if(!viewMenu.contains(event.target))showViewMenu(false);});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!viewPanel.hidden){event.preventDefault();event.stopPropagation();showViewMenu(false);viewButton.focus();}},true);
  const viewStateObserver=new MutationObserver(syncViewMenu);
  for(const item of viewItems)viewStateObserver.observe(item,{attributes:true,attributeFilter:['aria-pressed']});
  viewStateObserver.observe(layoutControls,{attributes:true,attributeFilter:['class']});
  viewStateObserver.observe(document.querySelector('.app-header'),{attributes:true,attributeFilter:['class']});
  window.addEventListener('resize',()=>{if(!viewPanel.hidden)positionViewMenu();});
  syncViewMenu();
  function applyThemeLayout(){
    window.dispatchEvent(new Event('resize'));
  }
  window.addEventListener('framekeep:theme',applyThemeLayout);
  applyThemeLayout();
};
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',startWorkbenchUi,{once:true});else startWorkbenchUi();
