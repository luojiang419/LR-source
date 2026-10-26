/* 只迁移既有组件的位置；功能与事件全部来自当前正式页面脚本。 */
document.addEventListener('DOMContentLoaded',()=>{
  'use strict';
  const root=document.documentElement,$=id=>document.getElementById(id);
  const film=document.querySelector('.film-panel'),main=document.querySelector('.library-main');
  const page=$('selection-page'),watch=$('folder-watch'),actions=document.querySelector('.header-primary-actions');
  const folderActions=document.querySelector('.folder-panel-actions');
  const folders=document.querySelector('.folders');
  // 页面切换放入顶栏，原导航栏承载既有选片工具节点。
  const header=document.querySelector('.app-header'),tabs=document.querySelector('.workspace-tabs');
  const navigation=document.querySelector('.workspace-navigation'),tools=$('selection-tools');
  const toolContent=document.createElement('div');toolContent.className='workspace-tool-content';
  toolContent.append(...[...tools.children].filter(node=>node.tagName!=='SUMMARY'));
  tools.append(toolContent);navigation.append(tools);navigation.classList.add('workspace-tool-navigation');
  function syncWorkspaceNavigation(){
    const headerHidden=header.classList.contains('hidden'),selectionVisible=!page.classList.contains('hidden');
    if(headerHidden){if(tabs.parentElement!==navigation)navigation.prepend(tabs);}
    else if(tabs.parentElement!==actions)actions.append(tabs);
    navigation.classList.toggle('workspace-tabs-detached',headerHidden);
    navigation.classList.toggle('hidden',!selectionVisible&&!headerHidden);
    tools.classList.toggle('hidden',!selectionVisible);
  }
  const navigationObserver=new MutationObserver(syncWorkspaceNavigation);
  for(const element of [header,page])navigationObserver.observe(element,{attributes:true,attributeFilter:['class']});
  syncWorkspaceNavigation();
  // 复用原折叠按钮与状态，只将入口收进顶栏的「视图」菜单。
  const layoutControls=document.querySelector('.workspace-layout-actions');
  const viewMenu=document.createElement('div');viewMenu.className='view-menu';
  const viewButton=document.createElement('button');viewButton.id='view-open';viewButton.type='button';viewButton.className='bordered';viewButton.textContent='视图';
  viewButton.setAttribute('aria-haspopup','menu');viewButton.setAttribute('aria-expanded','false');viewButton.setAttribute('aria-controls','view-options');
  const viewPanel=document.createElement('div');viewPanel.className='view-menu-panel';viewPanel.hidden=true;
  layoutControls.id='view-options';layoutControls.setAttribute('role','menu');layoutControls.setAttribute('aria-label','视图显示选项');
  const viewItems=[...layoutControls.querySelectorAll('button')];
  const viewLabels={'layout-header':'顶部工具栏','layout-left':'左侧面板','layout-right':'右侧面板','layout-tools':'选片工具','layout-film':'胶片条','layout-focus':'专注预览'};
  for(const item of viewItems){item.textContent=viewLabels[item.id]||item.textContent;item.setAttribute('aria-label',item.textContent);item.setAttribute('role','menuitemcheckbox');item.tabIndex=-1;}
  viewPanel.append(layoutControls);viewMenu.append(viewButton,viewPanel);$('mouse-remote').after(viewMenu);
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
    else if(viewMenu.previousElementSibling!==$('mouse-remote'))$('mouse-remote').after(viewMenu);
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
  function placeWatch(){
    if(!watch)return;
    const inFolder=root.dataset.hardcorePreview==='true'&&folders.open&&!page.matches('.monitoring,.left-collapsed,.hidden');
    if(inFolder){if(watch.parentElement!==folderActions)folderActions.prepend(watch);}
    else if(watch.parentElement!==actions)actions.insertBefore(watch,$('assistant-settings'));
  }
  const placement=new MutationObserver(placeWatch);
  placement.observe(page,{attributes:true,attributeFilter:['class']});
  placement.observe(folders,{attributes:true,attributeFilter:['open']});
  const dialog=$('theme-dialog'),options=dialog.querySelector('.theme-options');
  const choice=document.createElement('button');
  choice.type='button';choice.className='theme-option';choice.dataset.hardcoreChoice='true';
  choice.innerHTML='<span class="theme-swatch hardcore-swatch" aria-hidden="true"></span><strong>硬核工作台</strong><span>MiniMax 配色 · 三栏与顶部导航</span>';
  options.append(choice);
  for(const [mode,description] of Object.entries({light:'暖象牙 · 柔润磨砂',dark:'柔雾深灰 · 细腻高光','pure-white':'瓷白面板 · 轻盈投影'})){
    options.querySelector('[data-framekeep-theme-choice="'+mode+'"] span:last-child').textContent=description;
  }
  dialog.querySelector('p.dialog-copy').textContent='经典三主题沿用原布局；硬核工作台仅重排当前页面已有组件。';
  function apply(hardcore,persist=true){
    root.dataset.hardcorePreview=String(hardcore);
    if(hardcore)main.append(film);
    else page.append(film);
    placeWatch();
    choice.setAttribute('aria-pressed',String(hardcore));
    if(hardcore){
      document.querySelectorAll('[data-framekeep-theme-choice]').forEach(b=>b.setAttribute('aria-pressed','false'));
      $('theme-current').textContent='硬核工作台';
    }
    $('theme-open').textContent=hardcore?'硬核工作台':'主题';
    if(persist)try{localStorage.setItem('framekeep-hardcore-preview-ui',hardcore?'hardcore':FramekeepTheme.get());}catch{}
    window.dispatchEvent(new Event('resize'));
  }
  dialog.addEventListener('click',event=>{
    if(event.target.closest('[data-hardcore-choice]'))apply(true);
    else if(event.target.closest('[data-framekeep-theme-choice]'))apply(false);
  });
  $('service-connection').textContent='隔离布局演示';
  $('service-connection').title='复用当前页面组件；仅隔离样片评级可保存，设备和文件写入操作不执行。';
  document.title='末帧 · 最新页面硬核布局演示';
  apply(root.dataset.hardcorePreview==='true',false);
},{once:true});
