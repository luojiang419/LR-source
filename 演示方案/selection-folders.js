(() => {
  'use strict';
  if(!window.FramekeepIO)return;
  const $=id=>document.getElementById(id),page=$('selection-page'),layout=page.querySelector('.library-layout');
  const panel=document.createElement('aside');panel.id='selection-folders';panel.className='catalog-tree-panel';panel.setAttribute('aria-label','拍摄文件夹目录');
  panel.innerHTML='<header><strong>拍摄文件夹</strong><span id="selection-tree-count"></span></header><div id="selection-folder-tree" class="catalog-tree" role="tree" aria-label="磁盘与拍摄文件夹"></div><section class="catalog-scan-panel"><p id="rating-file-status" role="status" aria-live="polite"></p><div class="asset-actions"><button id="selection-tree-refresh" class="bordered" aria-label="刷新拍摄目录">刷新目录</button><button id="rating-file-retry" class="bordered" hidden>重试星级同步</button></div></section>';
  layout.before(panel);
  const toggle=document.createElement('button');toggle.id='selection-folders-toggle';toggle.type='button';toggle.className='panel-edge-toggle';toggle.setAttribute('aria-controls',panel.id);toggle.innerHTML='<span aria-hidden="true"></span>';page.append(toggle);
  let opened=false,roots=[],signature='',loading=false,selected='',syncTimer;
  const expanded=new Set(),closedDrives=new Set();
  function setOpen(value){opened=value;page.classList.toggle('folder-tree-open',value);panel.hidden=!value;toggle.firstElementChild.textContent=value?'‹':'›';toggle.title=value?'收起拍摄目录':'展开拍摄目录';toggle.setAttribute('aria-label',toggle.title);toggle.setAttribute('aria-expanded',String(value));window.dispatchEvent(new Event('resize'));if(value)refresh()}
  toggle.onclick=()=>setOpen(!opened);setOpen(false);
  const key=path=>path.replaceAll('\\','/').replace(/\/$/,'').toLocaleLowerCase();
  function build(){
    const drives=new Map();
    for(const root of roots){
      const normalized=root.root.replaceAll('\\','/'),drive=FramekeepPaths.driveOf(root.root);
      if(!drives.has(drive))drives.set(drive,{name:drive,children:new Map(),path:drive});
      const base={name:normalized.split('/').filter(Boolean).at(-1)||drive,path:normalized,children:new Map()};
      drives.get(drive).children.set(root.id,base);
      function child(parent,name){if(!parent.children.has(name))parent.children.set(name,{name,path:parent.path+'/'+name,children:new Map()});return parent.children.get(name)}
      for(const [folder,count] of Object.entries(root.folders)){
        let node=base;for(const name of folder.split('/').filter(Boolean))node=child(node,name);
        // 同路径同时添加父/子目录时优先精确根会话，避免展示重复文件。
        if(!node.source||folder==='')node.source={sid:root.id,folder,count};
      }
    }
    const host=$('selection-folder-tree');host.replaceChildren();$('selection-tree-count').textContent=roots.reduce((n,r)=>n+r.capture_count,0)+' 张';
    function append(parent,node,isRoot=false){
      const details=document.createElement('details'),summary=document.createElement('summary');details.dataset.path=key(node.path);details.open=expanded.has(details.dataset.path);summary.setAttribute('role','treeitem');
      const label=document.createElement('span'),count=document.createElement('small');label.textContent=node.name;count.textContent=node.source?.count??'';summary.title=node.path;summary.setAttribute('aria-label',node.name+(node.source?` · ${node.source.count}`:''));summary.classList.toggle('active',key(node.path)===selected);summary.setAttribute('aria-selected',String(key(node.path)===selected));summary.append(label);if(!isRoot)summary.append(count);details.append(summary);
      details.addEventListener('toggle',()=>{if(details.open)expanded.add(details.dataset.path);else expanded.delete(details.dataset.path)});
      summary.onclick=async event=>{event.preventDefault();try{if(node.source){await FramekeepIO.openSession(node.source.sid);if(window.FRAMEKEEP_SESSION.id!==node.source.sid)return;Framekeep.setFolderPrefix(node.source.folder);selected=key(node.path);host.querySelectorAll('summary').forEach(b=>{const active=b===summary;b.classList.toggle('active',active);b.setAttribute('aria-selected',String(active))})}details.open=!details.open}catch(e){Framekeep.notify(e.message)}};
      for(const child of [...node.children.values()].sort((a,b)=>a.name.localeCompare(b.name,undefined,{numeric:true})))append(details,child);
      parent.append(details);
    }
    for(const node of [...drives.values()].sort((a,b)=>a.name.localeCompare(b.name))){
      const group=document.createElement('section');group.className='catalog-drive-group';
      const button=document.createElement('button');button.className='catalog-drive-button';button.title=node.path;button.innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="4" width="18" height="15" rx="2"/><path d="M3 14h18M6 17h2M10 17h2"/></svg>';
      const label=document.createElement('span'),count=document.createElement('small');label.textContent=node.name;count.textContent=[...node.children.values()].reduce((n,r)=>n+r.source.count,0);button.append(label,count);
      const folders=document.createElement('div');folders.className='catalog-drive-folders';folders.hidden=closedDrives.has(node.path);button.setAttribute('aria-expanded',String(!folders.hidden));button.onclick=()=>{folders.hidden=!folders.hidden;button.setAttribute('aria-expanded',String(!folders.hidden));if(folders.hidden)closedDrives.add(node.path);else closedDrives.delete(node.path)};
      for(const child of node.children.values()){append(folders,child,true);folders.lastElementChild.classList.add('catalog-root')}
      group.append(button,folders);host.append(group);
    }
    if(!roots.length){const empty=document.createElement('div');empty.className='catalog-empty';empty.textContent='添加文件夹或开始联机拍摄后显示目录';host.append(empty)}
    markSelection();
  }
  function markSelection(){
    const root=roots.find(r=>r.id===window.FRAMEKEEP_SESSION.id);if(!root)return;
    const state=Framekeep.persistedState();selected=key(root.root+(state.folderPrefix?'/'+state.folderPrefix:''));
    for(const details of $('selection-folder-tree').querySelectorAll('details')){const summary=details.querySelector(':scope > summary'),active=details.dataset.path===selected;summary.classList.toggle('active',active);summary.setAttribute('aria-selected',String(active))}
  }
  async function refresh(){if(loading||!opened)return;loading=true;try{const data=await FramekeepIO.api('/api/selection/tree');const next=JSON.stringify(data.roots);if(next!==signature){roots=data.roots;signature=next;build()}}catch(e){$('selection-folder-tree').textContent='目录读取失败：'+e.message}finally{loading=false}}
  $('selection-tree-refresh').onclick=()=>{signature='';refresh()};
  document.addEventListener('framekeep:folders',refresh);document.addEventListener('framekeep:metadata-imported',refresh);
  document.addEventListener('framekeep:selection',markSelection);
  async function syncStatus(){clearTimeout(syncTimer);try{const data=await FramekeepIO.api('/api/ratings/sync');$('rating-file-status').textContent=data.failed?`星级已入库，${data.failed} 个文件同步失败`:data.pending?`星级写入 XMP：${data.pending} 个待同步`:'星级自动保存到数据库与 XMP';$('rating-file-status').title=data.issues.map(i=>i.path+'：'+i.reason).join('\n');$('rating-file-retry').hidden=!data.failed}catch{}finally{syncTimer=setTimeout(syncStatus,3000)}}
  $('rating-file-retry').onclick=async()=>{await FramekeepIO.api('/api/ratings/retry',{});syncStatus()};syncStatus();
})();
