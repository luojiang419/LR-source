(() => {
  'use strict';
  if (!window.FramekeepIO || !window.FramekeepWorkspace) return;
  const $ = id => document.getElementById(id);
  const api = FramekeepIO.api;
  const catalogSearchClient=crypto.randomUUID();
  const startupPreviews=new Map();
  window.FramekeepArchiveThumbnailCacheEnabled=false;
  let visitedThumbnailMode=window.FRAMEKEEP_SESSION?.archiveThumbnailSettings?.visited_mode===true;
  const previewUrl = (item, size) => {
    const url=window.FramekeepDesignPreviews?.url(item)||new URL(item.preview,location.href);
    url.searchParams.set('size',size);url.searchParams.set('preview_source','file');
    if(url.pathname.startsWith('/api/catalog/preview/'))url.searchParams.set('thumbnail_source','file');
    url.searchParams.delete('cached');url.searchParams.delete('bucket');
    return url.pathname+url.search;
  };
  const previewIdentity=value=>{const url=new URL(value,location.href);url.searchParams.delete('cached');return url.pathname+url.search};
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const ocrDialog=document.createElement('dialog');ocrDialog.id='ocr-metadata-dialog';ocrDialog.className='ocr-metadata-dialog';
  ocrDialog.innerHTML='<div class="dialog-heading"><h2 id="ocr-metadata-title">OCR 资料与 SKU</h2><button id="ocr-metadata-close" aria-label="关闭 OCR 资料">×</button></div><p>选择或填写正确的主款号。其他识别文字保留为辅助资料，可在 SKU 数据库搜索。</p><div id="ocr-metadata-candidates" class="asset-actions" role="group" aria-label="推荐款号"></div><label>正确 SKU 款号<input id="ocr-metadata-sku" maxlength="80" autocomplete="off" placeholder="点击候选，或手动输入正确款号"></label><button id="ocr-metadata-save" class="primary">确认 SKU 款号</button><p id="ocr-metadata-message" role="status"></p><details open><summary>OCR 全文</summary><div id="ocr-metadata-text" class="asset-ocr-raw" role="group" aria-label="原文识别项"></div></details><details><summary>其他识别字段</summary><pre id="ocr-metadata-fields"></pre></details>';
  document.body.append(ocrDialog);let ocrDetails=null,ocrSequence=0;
  $('ocr-metadata-close').onclick=()=>ocrDialog.close();
  window.FramekeepOcrDetails={async open(sid,cid){
    const sequence=++ocrSequence;ocrDetails=null;$('ocr-metadata-sku').value='';$('ocr-metadata-candidates').replaceChildren();$('ocr-metadata-text').textContent='';$('ocr-metadata-fields').textContent='';$('ocr-metadata-message').textContent='正在读取已保存资料…';$('ocr-metadata-save').disabled=true;if(!ocrDialog.open)ocrDialog.showModal();
    const data=await api('/api/sku/ocr/details?'+new URLSearchParams({session_id:sid,capture_id:cid}));
    if(sequence!==ocrSequence||!ocrDialog.open)return;ocrDetails=data;$('ocr-metadata-title').textContent=data.name+' · OCR 资料'+(data.target_capture_id!==data.capture_id?'（来自标签 '+data.source_name+'）':'');
    $('ocr-metadata-sku').value=data.selected_sku||(data.recommended.length===1?data.recommended[0]:'');
    $('ocr-metadata-candidates').innerHTML=FramekeepOcrUI.codeButtons(data.recommended,false,true);
    $('ocr-metadata-text').innerHTML=FramekeepOcrUI.codeButtons(FramekeepOcrUI.rawLines(data.text),true,true)||'尚无识别文字，请先识别当前图片';
    $('ocr-metadata-fields').textContent=JSON.stringify(data.readings.map(r=>r.auxiliary||{}),null,2);
    $('ocr-metadata-message').textContent=data.selected_sku?'已确认款号：'+data.selected_sku:data.has_ocr?'请核对后确认主款号':'尚无 OCR 资料';$('ocr-metadata-save').disabled=!data.has_ocr;
  }};
  ocrDialog.addEventListener('click',event=>{const button=event.target.closest('[data-ocr-pick]');if(!button)return;const code=button.dataset.ocrPick;if([...code].length>80){$('ocr-metadata-message').textContent='这条原文超过 80 字符，请手动填写其中的款号';return}$('ocr-metadata-sku').value=code;for(const item of ocrDialog.querySelectorAll('[data-ocr-pick]'))item.setAttribute('aria-pressed',String(item.dataset.ocrPick===code))});
  $('ocr-metadata-save').onclick=async()=>{
    if(!ocrDetails)return;const data=ocrDetails,button=$('ocr-metadata-save');button.disabled=true;
    try{const result=await api('/api/sku/ocr/choose',{session_id:data.session_id,capture_id:data.capture_id,sku:$('ocr-metadata-sku').value.trim(),reading_key:data.reading_key,image_key:data.image_key});
      document.dispatchEvent(new CustomEvent('framekeep:ocr-choice',{detail:result}));document.dispatchEvent(new CustomEvent('framekeep:ocr-result',{detail:result}));dbSnapshot='';ocrDialog.close();if(FramekeepWorkspace.current()==='sku-database')await renderDatabase();
    }catch(error){$('ocr-metadata-message').textContent=error.message}finally{button.disabled=false}
  };
  const archive = $('asset-archive-dialog');
  const archiveTab = $('asset-archive-open');
  if (!archive || !archiveTab) return;

  archive.innerHTML = `
    <div class="catalog-heading"><div><h2>资产归档</h2><p id="catalog-location">正在读取已保存的归档位置…</p></div></div>
    <div class="catalog-workspace">
      <button id="catalog-folders-toggle" type="button" class="panel-edge-toggle" aria-controls="catalog-folders"><span aria-hidden="true"></span></button>
      <aside id="catalog-folders" class="catalog-tree-panel"><header><strong>目录</strong><span id="catalog-count"></span></header><div id="catalog-tree" class="catalog-tree" role="tree"></div><footer class="catalog-tree-actions"><button id="catalog-add-root" class="bordered">添加归档文件夹</button><button id="catalog-scan" class="primary">刷新当前文件夹</button></footer></aside>
      <main class="catalog-main"><header class="catalog-toolbar" style="flex-wrap:wrap"><label><span>搜索素材</span><input id="catalog-search" type="search" placeholder="文件名、SKU、标题、关键词、相机或拍摄资料"></label><label><span>类型</span><select id="catalog-media"><option value="">全部</option><option value="image">图片 / RAW</option><option value="video">视频</option><option value="design">Adobe / 设计源文件</option></select></label><label><span>顺序</span><select id="catalog-sort"><option value="original">原顺序（文件名升序）</option><option value="modified">最近修改优先</option></select></label><span id="catalog-summary" role="status"></span></header><div id="catalog-grid" class="catalog-grid" aria-live="polite"></div><footer class="catalog-continuous-status" id="catalog-continuous-status" role="status" style="padding:6px 12px;font-size:11px;color:var(--theme-muted)"></footer></main>
      <aside class="catalog-inspector"><header><strong>素材资料与 SKU</strong><span id="catalog-inspector-status"></span></header><div id="catalog-inspector-empty" class="catalog-empty">选择素材以检查与修正 SKU</div><div id="catalog-inspector-form" hidden><div class="catalog-preview-wrap"><img id="catalog-selected-preview" alt="当前归档素材预览"><span id="catalog-selected-rating" class="photo-rating-badge" hidden></span><span id="catalog-selected-format" class="photo-format-badge" hidden></span></div><p id="catalog-selected-name"></p><div id="catalog-rating-controls" class="catalog-rating-controls" role="group" aria-label="归档图片星级"><button type="button" data-catalog-rating="1" aria-label="归档图片标 1 星">★</button><button type="button" data-catalog-rating="2" aria-label="归档图片标 2 星">★</button><button type="button" data-catalog-rating="3" aria-label="归档图片标 3 星">★</button><button type="button" data-catalog-rating="4" aria-label="归档图片标 4 星">★</button><button type="button" data-catalog-rating="5" aria-label="归档图片标 5 星">★</button><button type="button" data-catalog-rating="0">清星</button></div><p id="catalog-rating-status" role="status"></p><label>SKU 款号<input id="catalog-selected-sku" maxlength="80" placeholder="待确认"></label><p id="catalog-selected-source"></p></div><section class="catalog-scan-panel"><header><strong>素材解码缓存</strong></header><div id="catalog-preview-progress" class="catalog-index-progress" hidden><div><span id="catalog-preview-status" role="status"></span><strong id="catalog-preview-percent"></strong></div><progress id="catalog-preview-bar" max="100" value="0" aria-label="当前素材缓存覆盖率"></progress><p id="catalog-preview-eta" role="status"></p></div><p id="catalog-scan-status">缩略图与清晰预览均已缓存后，计为已解码。</p></section></aside>
    </div>`;

  const runtimeProgress=document.createElement('p');runtimeProgress.id='catalog-preview-runtime';$('catalog-preview-eta').after(runtimeProgress);
  function paintRuntimeProgress(progress){
    if(progress?.completed_this_run===undefined)return;
    runtimeProgress.textContent=`本次完成 ${progress.completed_this_run} 项 · ${progress.manual_paused?`已暂停${progress.active_workers?' · 正在收尾 '+progress.active_workers+' 项':''}`:`在途 ${progress.active_workers}/${progress.effective_workers} · ${progress.full_speed?'全速':'后台保护'}`}`;
    if(progress.fill_scan?.active)runtimeProgress.textContent+=` · 补齐准备：已检查 ${progress.fill_scan.checked} 次，新增入队 ${progress.fill_scan.queued} 项`;
  }
  const archiveLayout=archive.querySelector('.catalog-workspace'),archiveToggle=$('catalog-folders-toggle');
  function showArchiveFolders(visible){
    $('catalog-folders').hidden=!visible;archiveLayout.classList.toggle('catalog-folders-collapsed',!visible);
    archiveToggle.firstElementChild.textContent=visible?'‹':'›';archiveToggle.title=(visible?'收起':'展开')+'归档目录';archiveToggle.setAttribute('aria-label',archiveToggle.title);archiveToggle.setAttribute('aria-expanded',String(visible));
  }
  let archiveFoldersVisible=true;try{archiveFoldersVisible=localStorage.getItem('framekeep.archive.folders')!=='collapsed'}catch{}
  showArchiveFolders(archiveFoldersVisible);archiveToggle.onclick=()=>{showArchiveFolders($('catalog-folders').hidden);try{localStorage.setItem('framekeep.archive.folders',$('catalog-folders').hidden?'collapsed':'expanded')}catch{}};

  const skuTab = document.createElement('button');
  skuTab.id = 'sku-database-open';
  skuTab.textContent = 'SKU 数据库';
  skuTab.setAttribute('role', 'tab');
  skuTab.setAttribute('aria-controls', 'sku-database-page');
  skuTab.setAttribute('aria-selected', 'false');
  skuTab.tabIndex = -1;
  archiveTab.after(skuTab);
  const skuPage = document.createElement('section');
  skuPage.id = 'sku-database-page';
  skuPage.className = 'asset-workspace catalog-database hidden';
  skuPage.setAttribute('role', 'tabpanel');
  skuPage.setAttribute('aria-labelledby', skuTab.id);
  skuPage.innerHTML = `<div class="catalog-heading"><div><h2>SKU 数据库</h2></div></div>
    <div class="sku-database-layout"><aside class="sku-database-sidebar">
      <label class="sku-library-search">搜索 SKU<input id="sku-database-search" type="search" placeholder="输入已识别款号"></label>
      <div class="sku-library-tabs" role="tablist" aria-label="SKU 浏览方式"><button id="sku-view-flat" role="tab" aria-selected="true" aria-controls="sku-database-list" data-sku-view="flat">平铺浏览</button><button id="sku-view-tree" role="tab" aria-selected="false" aria-controls="sku-database-tree" data-sku-view="tree">树形浏览</button></div>
      <label class="sku-library-sort">显示顺序<select id="sku-database-sort"><option value="name">文件名 A → Z</option><option value="name_desc">文件名 Z → A</option><option value="added">添加时间 · 最新优先</option><option value="added_asc">添加时间 · 最早优先</option><option value="captured">拍摄时间 · 最新优先</option><option value="captured_asc">拍摄时间 · 最早优先</option></select></label>
      <div id="sku-database-list" class="sku-database-list" role="tabpanel" aria-labelledby="sku-view-flat"></div>
      <div id="sku-database-tree" class="catalog-tree" role="tabpanel" aria-labelledby="sku-view-tree" hidden></div>
      <p id="sku-library-count" class="sku-library-note"></p>
    </aside><main><header id="sku-database-title" class="catalog-toolbar">选择一个 SKU</header><div id="sku-database-grid" class="catalog-grid"></div></main></div>`;
  archive.after(skuPage);
  $('sku-database-grid').addEventListener('error',event=>{
    const image=event.target;if(image.tagName!=='IMG'||image.hidden)return;
    image.hidden=true;const hint=document.createElement('span');hint.className='sku-offline-preview';hint.textContent='原照片离线或预览不可用 · 元数据已保留';image.parentElement.append(hint);
  },true);

  let state = { roots: [], counts: {} }, selection = { root: '', folder: '', item: null }, nextCursor = '', hasMore = false, dbSku = '';
  window.FramekeepArchiveSelection=()=>({root_id:selection.root,folder:selection.folder,path:[state.roots.find(r=>r.id===selection.root)?.path||'',selection.folder].filter(Boolean).join('\\')});
  window.FramekeepArchiveOpenItem=async itemId=>{
    const detail=await api('/api/archive-capture/item?'+new URLSearchParams({item_id:itemId}));
    selection.root=detail.scope.root_id;selection.folder=detail.scope.folder;FramekeepWorkspace.show('archive');await refresh({reset:true});
    select(catalogRows.get(itemId)||{...detail.item,rating:detail.record.rating,preview:'/api/catalog/preview/'+encodeURIComponent(itemId)+'?size=420'});
    window.FramekeepArchiveCapture?.showPhoto();
  };
  try{dbSku=localStorage.getItem('framekeep.sku.last')||''}catch{}
  let dbMode='flat',dbRoot='',dbFolder='',dbBin='',dbRequest=0,dbActive=0,dbSnapshot='',catalogRequest=0,catalogRows=new Map(),dbRows=new Map(),catalogSnapshot='',archiveActive=0,catalogRatingController=null;
  const message = text => { $('catalog-scan-status').textContent = text; };
  let refreshError='';
  function reportRefreshError(error){
    refreshError=['TimeoutError','AbortError'].includes(error.name)?'归档进度暂时未返回，正在自动重试；后台解码继续运行。':'归档资料暂时读取失败，正在自动重试：'+error.message;
    message(refreshError);
  }
  const getFolder = async () => {
    if (window.FramekeepDesktop?.chooseFolders) {
      const values = await FramekeepDesktop.chooseFolders();
      return Array.isArray(values) ? values[0] : values;
    }
    return (await api('/api/choose-folder')).folder;
  };
  const driveOf = FramekeepPaths.driveOf;
  const collapsedDrives=new Set(),folderStates=new Map();
  const collapsedResultFolders=new Set();
  try{for(const key of JSON.parse(localStorage.getItem('framekeep-catalog-collapsed-results-v1')||'[]'))if(typeof key==='string')collapsedResultFolders.add(key)}catch{}
  let beforeSearch=null,skipRememberTree=false,archiveRefresh=0,catalogTreeSnapshot='',catalogController=null,catalogLoadFailed=false;
  let catalogAppending=false,catalogLoadedScope='',catalogTotal=null;
  let catalogSummaryController=null,catalogSummaryKey='',catalogSummaryData=null,catalogSummarySequence=0;
  let catalogRootsLoaded=false,catalogTreeController=null,referenceProgress=new Map();
  const searching=()=>$('catalog-search').value.trim()!=='';
  const allSearchFolders=document.createElement('button');allSearchFolders.id='catalog-all-search-folders';allSearchFolders.type='button';allSearchFolders.textContent='全部匹配文件夹';allSearchFolders.hidden=true;$('catalog-summary').before(allSearchFolders);
  allSearchFolders.onclick=()=>{selection.root='';selection.folder='';paintFolderSelection();select(null);refresh({reset:true,contentsOnly:true}).catch(error=>message(error.message))};
  const nodeOpen=key=>folderStates.get(key)!==false?'open':'';
  const catalogBranches=new Map();
  const catalogNodeOpen=(root,folder)=>{
    const key='catalog|'+root+'|'+folder;
    if(folderStates.has(key))return folderStates.get(key);
    return !folder||selection.root===root&&(selection.folder===folder||selection.folder.startsWith(folder+'/'));
  };
  const folderTree = folders => {
    const root = { children: new Map(), count: 0 };
    for (const row of folders) {
      const parts = row.folder === '/' ? [] : row.folder.replace(/\\/g,'/').split('/').filter(Boolean);
      let node = root; node.count += row.count;
      for (const part of parts) { if (!node.children.has(part)) node.children.set(part, { children: new Map(), count: 0 }); node = node.children.get(part); node.count += row.count; }
      node.directCount=(node.directCount||0)+row.count;node.known=row.known!==false;node.skus=row.skus||[];
    }
    return root;
  };
  const skuAddedDate=created=>{const date=new Date(Number(created)*1000);return created&&Number.isFinite(date.getTime())?`${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`:'日期未知';};
  const skuDate=created=>`<small class="sku-added-date">入库 ${esc(skuAddedDate(created))}</small>`;
  const skuButtons=(rows,root,folder)=> (rows||[]).map(row=>`<button class="sku-tree-code ${dbSku===row.normalized&&dbRoot===root&&dbFolder===folder?'active':''}" data-db-sku="${esc(row.normalized)}" data-db-root="${esc(root)}" data-db-folder="${esc(folder)}" title="最近入库 ${esc(skuAddedDate(row.created))}"><span class="sku-code-info"><span>${esc(row.sku)}</span>${skuDate(row.created)}</span><small>${row.count}</small></button>`).join('');
  const branch = (node, rootId, prefix = '', scope='catalog') => [...node.children.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([name, child]) => {
    const folder = prefix ? prefix + '/' + name : name;
    const hit=scope==='catalog'&&searching()&&child.directCount>0;
    const active = scope==='catalog'?(selection.root === rootId && selection.folder === folder||hit&&!selection.root):dbRoot===rootId&&dbFolder===folder&&!dbSku;
    const key=scope+'|'+rootId+'|'+folder,attr=scope==='catalog'?'':'db-';
    const open=scope==='catalog'?catalogNodeOpen(rootId,folder):!!nodeOpen(key);
    if(scope==='catalog')catalogBranches.set(key,{node:child,rootId,folder});
    const children=scope==='sku'||open?branch(child,rootId,folder,scope):'';
    return `<details ${open?'open':''} data-catalog-node="${esc(key)}" ${scope==='catalog'?`data-catalog-children="${open||!child.children.size?'ready':'pending'}"`:''}><summary role="treeitem" data-${attr}root="${esc(rootId)}" data-${attr}folder="${esc(folder)}" class="${active?'active':''} ${hit?'catalog-search-match':''}" aria-selected="${active}"><span>${esc(name)}</span><small>${scope==='catalog'&&state.browser?(child.known?child.directCount+' 文件':'—'):child.count}</small></summary>${scope==='sku'?skuButtons(child.skus,rootId,folder):''}${children}</details>`;
  }).join('');
  function rememberTree(host){for(const node of host.querySelectorAll('[data-catalog-node]'))folderStates.set(node.dataset.catalogNode,node.open)}
  function treeMarkup(roots,scope='catalog'){
    if(!roots.length)return '<div class="catalog-empty">'+(scope==='sku'?'尚无已识别或已备案的 SKU':searching()?'没有匹配的文件夹':'尚未添加归档文件夹')+'</div>';
    const drives=new Map(),attr=scope==='catalog'?'':'db-';
    for(const root of roots){const drive=driveOf(root.path);if(!drives.has(drive))drives.set(drive,[]);drives.get(drive).push(root)}
    return [...drives.entries()].sort(([a],[b])=>a.localeCompare(b)).map(([drive,roots],index)=>{
      const key=scope+'|'+drive,closed=collapsedDrives.has(key),id=scope+'-drive-folders-'+index;
      return `<section class="catalog-drive-group"><button type="button" class="catalog-drive-button" data-${scope==='catalog'?'catalog':'db'}-drive="${esc(drive)}" aria-expanded="${!closed}" aria-controls="${id}" title="${esc(drive)}"><svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="4" width="18" height="15" rx="2"/><path d="M3 14h18M6 17h2M10 17h2"/></svg><span>${esc(drive)}</span><small>${scope==='catalog'&&state.browser?roots.length+' 个位置':roots.reduce((sum,r)=>sum+r.folders.reduce((n,f)=>n+f.count,0),0)}</small></button><div id="${id}" class="catalog-drive-folders" ${closed?'hidden':''}>${roots.map(root=>{
        const rootName=root.path.split(/[\\/]/).filter(Boolean).at(-1)||root.path,node=folderTree(root.folders),hit=scope==='catalog'&&searching()&&node.directCount>0,active=scope==='catalog'?(selection.root===root.id&&!selection.folder||hit&&!selection.root):dbRoot===root.id&&!dbFolder&&!dbSku;
        const key=scope+'|'+root.id+'|',open=scope==='catalog'?catalogNodeOpen(root.id,''):!!nodeOpen(key),loaded=scope==='sku'||open&&!closed;
        if(scope==='catalog')catalogBranches.set(key,{node,rootId:root.id,folder:''});
        return `<details ${open?'open':''} data-catalog-node="${esc(key)}" ${scope==='catalog'?`data-catalog-children="${loaded?'ready':'pending'}"`:''} class="catalog-root"><summary role="treeitem" data-${attr}root="${esc(root.id)}" data-${attr}folder="" class="${active?'active':''} ${hit?'catalog-search-match':''}" aria-selected="${active}" title="${esc(root.path)}"><span>${esc(rootName)}</span></summary>${scope==='sku'?skuButtons(node.skus,root.id,''):''}${loaded?branch(node,root.id,'',scope):''}</details>`;
      }).join('')}</div></section>`;
    }).join('');
  }
  function searchFolderMarkup(roots){
    if(!roots.length)return '<div class="catalog-empty">没有匹配的文件夹</div>';
    return roots.map(root=>{
      const rootName=root.path.split(/[\\/]/).filter(Boolean).at(-1)||root.path;
      const folders=[...root.folders].sort((a,b)=>a.folder.localeCompare(b.folder));
      return `<section class="catalog-search-root"><p title="${esc(root.path)}">${esc(rootName)}</p>${folders.map(row=>{
        const folder=row.folder||'',name=folder==='/'?rootName:folder.split('/').filter(Boolean).at(-1)||folder;
        return `<button type="button" class="catalog-search-result catalog-search-match" data-root="${esc(root.id)}" data-folder="${esc(folder)}" aria-selected="false" title="${esc(folder==='/'?root.path:root.path.replace(/[\\/]+$/,'')+'/'+folder)}"><span><strong>${esc(name)}</strong>${folder!=='/'?`<small>${esc(folder)}</small>`:''}</span><small>${row.count}</small></button>`;
      }).join('')}</section>`;
    }).join('');
  }
  function tree(){
    const host=$('catalog-tree'),snapshot=JSON.stringify([searching(),state.roots]);
    if(!skipRememberTree)rememberTree(host);
    if(skipRememberTree||snapshot!==catalogTreeSnapshot){catalogBranches.clear();host.innerHTML=searching()&&!state.browser?searchFolderMarkup(state.roots):treeMarkup(state.roots);catalogTreeSnapshot=snapshot}
    skipRememberTree=false;paintFolderSelection();rememberArchiveView();
  }
  function hydrateCatalogBranch(details){
    if(!details.open||details.dataset.catalogChildren!=='pending'||details.closest('.catalog-drive-folders')?.hidden)return;
    const entry=catalogBranches.get(details.dataset.catalogNode);if(!entry)return;
    details.dataset.catalogChildren='ready';
    details.insertAdjacentHTML('beforeend',branch(entry.node,entry.rootId,entry.folder));
    paintFolderSelection();
  }
  $('catalog-tree').addEventListener('toggle',event=>{
    const details=event.target;if(!details.matches('details[data-catalog-node]')||!details.isConnected)return;
    folderStates.set(details.dataset.catalogNode,details.open);hydrateCatalogBranch(details);rememberArchiveView();
  },true);
  function paintFolderSelection(){
    document.dispatchEvent(new CustomEvent('framekeep:archive-scope',{detail:window.FramekeepArchiveSelection()}));
    for(const node of $('catalog-tree').querySelectorAll('[data-root]')){
      const active=node.dataset.root===selection.root&&node.dataset.folder===selection.folder||searching()&&!selection.root&&node.classList.contains('catalog-search-match');
      node.classList.toggle('active',active);node.setAttribute('aria-selected',String(active));
    }
    allSearchFolders.hidden=state.browser||!searching();allSearchFolders.disabled=!selection.root&&!selection.folder;
  }
  function folderGroups(rows){
    const groups=new Map();
    for(const row of rows){const key=JSON.stringify([row.root,row.folder]);if(!groups.has(key))groups.set(key,[]);groups.get(key).push(row)}
    return groups;
  }
  function groupedCards(rows){
    const groups=folderGroups(rows);
    const grouped=groups.size>1||searching();$('catalog-grid').classList.toggle('catalog-grouped',grouped);
    if(!grouped)return rows.map(card).join('');
    return [...groups.values()].map(items=>{
      const path=items[0].path.replace(/\\/g,'/').split('/').slice(0,-1).join('/'),name=path.split('/').filter(Boolean).at(-1)||path;
      const key=JSON.stringify([items[0].root,items[0].folder]),collapsed=collapsedResultFolders.has(key),id='catalog-result-folder-'+items[0].id;
      return `<section class="catalog-folder-group" data-folder-group="${esc(key)}"><header><button type="button" class="catalog-folder-toggle" data-folder-toggle="${esc(key)}" aria-expanded="${!collapsed}" aria-controls="${esc(id)}" aria-label="${collapsed?'展开':'折叠'}文件夹 ${esc(name)}"><span class="catalog-folder-arrow" aria-hidden="true">${collapsed?'▸':'▾'}</span><span class="catalog-folder-heading"><strong>${esc(name)}</strong><small>${esc(path)}</small></span><span class="catalog-folder-count">已加载 ${items.length} 项</span></button></header><div id="${esc(id)}" class="catalog-folder-grid" ${collapsed?'hidden':''}>${items.map(card).join('')}</div></section>`;
    }).join('');
  }
  function card(item) {
    const active = selection.item?.id === item.id;
    const thumbnail=item.offline?'<span class="sku-offline-preview">原照片离线 · 元数据已保留</span>':`<img data-preview-src="${esc(previewUrl(item,420))}" alt="${esc(item.name)}">${FramekeepAssetLoading.markup()}`;
    const format=item.has_jpg && item.has_raw?'J/R':item.media_type==='image'?'':item.media_format||item.extension?.slice(1).toUpperCase()||'';
    const sku=String(item.sku||'').trim(),skuLabel=sku?`${item.source==='path_rule'?'SKU（待确认）':'SKU'}：${sku}`:'SKU：未确认';
    return `<button class="catalog-card ${active?'active':''}" data-catalog-item="${esc(item.id)}" data-provenance="${esc(item.provenance||'')}" title="${esc([item.name,skuLabel,item.provenance_label].filter(Boolean).join('\n'))}"><span class="catalog-thumb" data-preview-state="${item.offline?'offline':'loading'}">${thumbnail}${format?`<span class="photo-format-badge">${esc(format)}</span>`:''}${item.rating>0&&['image','video'].includes(item.media_type)?`<span class="photo-rating-badge" aria-label="${item.rating} 星">${'★'.repeat(item.rating)}</span>`:''}</span><span class="catalog-card-name" title="${esc(item.name)}">${esc(item.name)}</span><span class="catalog-card-sku${sku?'':' is-unassigned'}" title="${esc(skuLabel)}">${esc(skuLabel)}</span></button>`;
  }
  function paintCardRating(itemId,rating,knownCard=null){
    const card=knownCard||[...$('catalog-grid').querySelectorAll('[data-catalog-item]')].find(node=>node.dataset.catalogItem===itemId);if(!card)return;
    const thumb=card.querySelector('.catalog-thumb'),previous=thumb.querySelector('.photo-rating-badge');
    if(!rating){previous?.remove();return}
    const badge=previous||document.createElement('span');badge.className='photo-rating-badge';badge.textContent='★'.repeat(rating);badge.setAttribute('aria-label',rating+' 星');if(!previous)thumb.append(badge);
  }
  document.addEventListener('framekeep:archive-record',event=>{
    const {item_id:id,rating,sku}=event.detail||{},row=catalogRows.get(id);
    if(row){row.rating=rating;row.sku=sku;row.source='portable';paintCardRating(id,rating);
      const card=[...$('catalog-grid').querySelectorAll('[data-catalog-item]')].find(node=>node.dataset.catalogItem===id),label=card?.querySelector('.catalog-card-sku');
      if(label){label.textContent=sku?'SKU：'+sku:'SKU：未确认';label.title=label.textContent;label.classList.toggle('is-unassigned',!sku)}
    }
    if(selection.item?.id===id){const previous=selection.item.sku||'';selection.item={...selection.item,rating,sku,source:'portable'};paintRating(rating);if(document.activeElement!==$('catalog-selected-sku')&&(!$('catalog-selected-sku').value||$('catalog-selected-sku').value===previous))$('catalog-selected-sku').value=sku}
  });
  // 本机刚标星的素材：在此之前发出的批量读取结果不能把新星级覆盖回旧值。
  const ratingTouched=new Map();
  function paintCatalogValues(data,requestedAt=null){
    const cards=new Map([...$('catalog-grid').querySelectorAll('[data-catalog-item]')].map(node=>[node.dataset.catalogItem,node]));
    for(const [id,value] of Object.entries(data.ratings||{})){
      const row=catalogRows.get(id);if(!row)continue;
      const stale=requestedAt!=null&&(ratingTouched.get(id)||0)>=requestedAt||requestedAt!=null&&window.FramekeepArchiveCapture?.pending?.(id);
      if(!stale){row.rating=Number(value)||0;paintCardRating(id,row.rating,cards.get(id))}
      const fields=data.items?.[id];
      if(fields){
        const previous=row.sku||'';row.sku=fields.sku||'';row.source=fields.source;row.metadata_status=fields.metadata_status||'ready';
        const label=cards.get(id)?.querySelector('.catalog-card-sku');
        if(label){label.textContent=row.sku?`${row.source==='path_rule'?'SKU（待确认）':'SKU'}：${row.sku}`:'SKU：未确认';label.title=label.textContent;label.classList.toggle('is-unassigned',!row.sku)}
        if(selection.item?.id===id){const input=$('catalog-selected-sku');if(document.activeElement!==input&&(!input.value||input.value===previous))input.value=row.sku;selection.item={...selection.item,sku:row.sku,source:row.source}}
      }
      if(selection.item?.id===id){selection.item={...selection.item,rating:row.rating};paintRating(row.rating)}
    }
  }
  let groupRefresh=0;
  document.addEventListener('framekeep:archive-rating-saved',event=>{
    const {item_id:id,rating}=event.detail||{};catalogRatingController?.abort();++groupRefresh;
    ratingTouched.set(id,performance.now());
    paintCatalogValues({ratings:{[id]:rating}});
  });
  document.addEventListener('framekeep:archive-groups',async()=>{
    const sequence=++groupRefresh,scope=catalogLoadedScope,ids=[...catalogRows.keys()];
    try{
      for(let start=0;start<ids.length;start+=200){
        const query=new URLSearchParams();for(const id of ids.slice(start,start+200))query.append('id',id);
        const requestedAt=performance.now(),data=await api('/api/catalog/ratings?'+query);
        if(sequence!==groupRefresh||scope!==catalogLoadedScope)return;
        paintCatalogValues(data,requestedAt);
      }
    }catch(error){if(sequence===groupRefresh)$('catalog-continuous-status').textContent='区间已保存，款号显示正在刷新：'+error.message}
  });
  function hydrateCatalogRatings(rows,request){
    const ids=rows.map(row=>row.id);if(!ids.length)return;
    catalogRatingController?.abort();const controller=new AbortController();catalogRatingController=controller;
    const query=new URLSearchParams();for(const id of ids)query.append('id',id);
    const requestedAt=performance.now();
    api('/api/catalog/ratings?'+query,undefined,20000,controller.signal).then(data=>{
      if(controller.signal.aborted||request!==catalogRequest)return;
      paintCatalogValues(data,requestedAt);
    }).catch(()=>{}).finally(()=>{if(catalogRatingController===controller)catalogRatingController=null});
  }
  let ratingQueueIds=[],ratingQueueScope='',ratingQueueRunning=false;
  function queueCatalogRatings(rows){
    if(ratingQueueScope!==catalogLoadedScope){ratingQueueIds=[];ratingQueueScope=catalogLoadedScope}
    ratingQueueIds.push(...rows.map(row=>row.id));
    if(!ratingQueueRunning)drainCatalogRatings().catch(()=>{});
  }
  async function drainCatalogRatings(){
    ratingQueueRunning=true;
    try{
      while(ratingQueueIds.length&&ratingQueueScope===catalogLoadedScope){
        const ids=ratingQueueIds.splice(0,240),scope=ratingQueueScope,query=new URLSearchParams();
        for(const id of ids)query.append('id',id);
        const requestedAt=performance.now();
        try{const data=await api('/api/catalog/ratings?'+query,undefined,20000);if(scope===catalogLoadedScope)paintCatalogValues(data,requestedAt)}catch{}
      }
    }finally{ratingQueueRunning=false}
  }
  function patchCatalogCard(current,next){
    // 后台款号和星级更新只改字段；已加载的图片、焦点和多选状态留在原位置。
    for(const name of ['title','data-provenance'])if(current.getAttribute(name)!==next.getAttribute(name))current.setAttribute(name,next.getAttribute(name));
    current.classList.toggle('active',next.classList.contains('active'));
    const image=current.querySelector('img[data-preview-src]'),replacement=next.querySelector('img[data-preview-src]');
    if(!image||!replacement||previewIdentity(image.dataset.previewSrc)!==previewIdentity(replacement.dataset.previewSrc)){
      if(current.querySelector('.catalog-thumb').outerHTML!==next.querySelector('.catalog-thumb').outerHTML)current.querySelector('.catalog-thumb').replaceWith(next.querySelector('.catalog-thumb'));
    }else{
      for(const selector of ['.photo-format-badge','.photo-rating-badge']){
        const previous=current.querySelector(selector),value=next.querySelector(selector);
        if(previous?.outerHTML===value?.outerHTML)continue;
        if(previous){if(value)previous.replaceWith(value);else previous.remove()}
        else if(value)current.querySelector('.catalog-thumb').append(value);
      }
    }
    for(const selector of ['.catalog-card-name','.catalog-card-sku']){
      const previous=current.querySelector(selector),value=next.querySelector(selector);
      if(previous.outerHTML!==value.outerHTML)previous.replaceWith(value);
    }
  }
  function paintCatalogItems(data,{preserve=true,request=catalogRequest,cached=false,append=false,hydrate=true}={}){
    const oldRows=[...catalogRows.values()],preserveTail=oldRows.length>120&&!append;
    const incoming=new Set(data.rows.map(row=>row.id));
    const merged=append?[...oldRows.filter(row=>!incoming.has(row.id)),...data.rows]:preserveTail?[...data.rows,...oldRows.filter(row=>!incoming.has(row.id))]:data.rows;
    const unique=[...new Map(merged.map(row=>[row.id,row])).values()];
    const displayRows=[...folderGroups(unique).values()].flat();catalogRows=new Map(displayRows.map(row=>[row.id,row]));
    if(selection.item&&catalogRows.has(selection.item.id)){selection.item={...selection.item,...catalogRows.get(selection.item.id)};paintRating(selection.item.rating||0)}
    if(append||!preserveTail){hasMore = data.has_more;nextCursor = data.next_cursor;}
    if (!preserve && selection.item && !unique.some(row => row.id === selection.item.id)) select(null);
    const grid=$('catalog-grid');
    // 单目录连续追加只解析新卡片，不重新序列化和重建已显示的整页卡片。
    const fastAppend=append&&!searching()&&!grid.classList.contains('catalog-grouped')&&folderGroups(unique).size<=1&&grid.querySelector(':scope>[data-catalog-item]');
    if(fastAppend){
      const known=new Set(oldRows.map(row=>row.id)),added=displayRows.filter(row=>!known.has(row.id));
      if(added.length){const next=document.createElement('template');next.innerHTML=added.map(card).join('');grid.append(next.content);FramekeepAssetLoading.watch(grid)}
      catalogSnapshot='';
    }
    // 资料入库时间等后台字段变化不重画卡片；返回页面时保留已解码的图片节点。
    const snapshot=fastAppend?'':JSON.stringify([searching(),displayRows.map(row=>[row.id,card({...row,preview:previewIdentity(row.preview)}),row.root,row.folder])]);
    if(!fastAppend&&snapshot!==catalogSnapshot){
      const top=grid.scrollTop,existing=new Map([...grid.querySelectorAll('[data-catalog-item]')].map(node=>[node.dataset.catalogItem,node]));
      const next=document.createElement('template');next.innerHTML=displayRows.length?groupedCards(displayRows):'<div class="catalog-empty">没有符合当前条件的照片</div>';
      const nodes=[...next.content.querySelectorAll('[data-catalog-item]')];
      const sameLayout=nodes.length>0&&JSON.stringify([...existing.keys()])===JSON.stringify(nodes.map(node=>node.dataset.catalogItem))&&JSON.stringify([...grid.querySelectorAll('[data-folder-group]')].map(node=>node.dataset.folderGroup))===JSON.stringify([...next.content.querySelectorAll('[data-folder-group]')].map(node=>node.dataset.folderGroup));
      if(sameLayout){
        for(const node of nodes)patchCatalogCard(existing.get(node.dataset.catalogItem),node);
      }else{
        for(const node of nodes){
          const previous=existing.get(node.dataset.catalogItem);if(!previous)continue;
          patchCatalogCard(previous,node);node.replaceWith(previous);
        }
        grid.replaceChildren(next.content);if(!displayRows.length)grid.classList.remove('catalog-grouped');
      }
      grid.scrollTop=top;catalogSnapshot=snapshot;FramekeepAssetLoading.watch(grid);
    }
    // 连续追加的各页按顺序补齐星级与款号，不能被下一页的请求取消。
    if(!cached&&hydrate){if(append)queueCatalogRatings(data.rows);else hydrateCatalogRatings(selection.item&&!data.rows.some(row=>row.id===selection.item.id)?[...data.rows,selection.item]:data.rows,request)}
    if(!append&&data.total!=null)catalogTotal=data.total;
    const knownTotal=catalogTotal??(!searching()&&!selection.root&&!selection.folder&&!$('catalog-media').value?state.counts.total:null);
    $('catalog-summary').textContent = knownTotal==null?`已显示 ${displayRows.length} 项${hasMore?' · 还有更多':''} · ${data.tree_deferred?'正在统计全部命中':'连续浏览'}`:`${knownTotal} 项素材 · ${data.engine||'目录数据库'}`;
    window.FramekeepArchiveFiles?.paint();
    if(cached)$('catalog-summary').textContent+=' · 上次浏览结果，正在更新…';
    if(catalogListing)scheduleCatalogLoading();
    else $('catalog-continuous-status').textContent=hasMore?'向下滚动继续浏览':'已显示当前范围全部素材';
  }
  let catalogPrefetchController=null,catalogPrefetchKey='',catalogPrefetchSequence=0,catalogPrefetchRetry=null,catalogListing=null,visitRunning=false;
  // 列表分页与缩略图解耦：本地索引连续读完并立即生成卡片；超过上限后仍可滚动续载。
  const CATALOG_AUTO_APPEND=6000;
  function cancelCatalogPrefetch(){clearTimeout(catalogPrefetchRetry);catalogPrefetchRetry=null;catalogPrefetchController?.abort();catalogPrefetchController=null;catalogPrefetchKey='';catalogListing=null}
  let catalogLoadingTimer=0;
  function paintCatalogLoading(){
    catalogLoadingTimer=0;
    if(!catalogListing)return;
    let ready=0,failed=0,images=0;
    for(const node of $('catalog-grid').querySelectorAll('.catalog-thumb[data-preview-state]')){
      const value=node.dataset.previewState;if(value==='offline')continue;
      images++;if(value==='ready')ready++;else if(value==='error')failed++;
    }
    const shown=catalogRows.size,total=catalogTotal??null,parts=[];
    if(catalogListing.listing)parts.push(`正在载入列表 ${shown}${total!=null?' / '+total:''}`);
    else if(!catalogListing.capped&&!hasMore)parts.push(`已显示全部 ${shown} 项`);
    parts.push(`缩略图已准备 ${ready} / ${images}`);
    if(failed)parts.push(`${failed} 项暂不可用`);
    if(catalogListing.capped&&hasMore)parts.push('向下滚动继续浏览');
    if(visitRunning)parts.push('目录读取中，新增素材会自动出现');
    $('catalog-continuous-status').textContent=parts.join(' · ');
  }
  // 每张缩略图完成即更新计数（节流），不再等整页完成才累加。
  function scheduleCatalogLoading(){if(!catalogLoadingTimer)catalogLoadingTimer=setTimeout(paintCatalogLoading,120)}
  new MutationObserver(()=>{if(catalogListing)scheduleCatalogLoading()}).observe($('catalog-grid'),{subtree:true,attributes:true,attributeFilter:['data-preview-state']});
  function restoreCatalogOrder(ordered){
    // 首次读取期间行按入库先后追加；读完后按服务端顺序复位一次，沿用已加载的卡片与图片。
    // 与页面上实际卡片顺序比较（追加页不移动已有节点，内存行顺序不能代表界面顺序）。
    const present=[...$('catalog-grid').querySelectorAll('[data-catalog-item]')].map(node=>node.dataset.catalogItem);
    const wanted=[...new Map(ordered.map(row=>[row.id,row])).keys()].filter(id=>catalogRows.has(id)),seen=new Set(wanted);
    const expected=[...wanted,...present.filter(id=>!seen.has(id))];
    if(expected.every((id,index)=>present[index]===id))return;
    paintCatalogItems({rows:expected.map(id=>catalogRows.get(id)),has_more:hasMore,next_cursor:nextCursor,total:catalogTotal},{preserve:true,hydrate:false});
  }
  function prefetchCatalogFolder(first,query,scope,attempt=0){
    if(!(state.browser||visitedThumbnailMode)||!selection.root||searching()||document.hidden||FramekeepWorkspace.current()!=='archive')return;
    const key=JSON.stringify([scope,first.total,first.rows.map(row=>[row.id,previewIdentity(row.preview)])]);
    if(key===catalogPrefetchKey)return;
    cancelCatalogPrefetch();catalogPrefetchKey=key;
    const controller=new AbortController();catalogPrefetchController=controller;
    const current=()=>!controller.signal.aborted&&scope===catalogLoadedScope&&FramekeepWorkspace.current()==='archive';
    const grid=$('catalog-grid'),urls=rows=>rows.filter(row=>!row.offline).map(row=>previewUrl(row,420));
    const listing={listing:true,capped:false};catalogListing=listing;scheduleCatalogLoading();
    // 缩略图按视口优先在后台逐张加载，列表分页不等待图片完成。
    FramekeepAssetLoading.prefetch(grid,urls(first.rows),{signal:controller.signal}).then(scheduleCatalogLoading);
    (async()=>{
      const cursors=new Set(),pause=ms=>new Promise(resolve=>setTimeout(resolve,ms)),ordered=[...first.rows];
      // 续载游标取自本轮首页：后台重读首页时全局游标保持旧值（已到末尾），不能据此停止。
      let cursor=first.next_cursor,more=!!first.has_more;
      while(current()&&more&&cursor&&!cursors.has(cursor)){
        if(catalogRows.size>=CATALOG_AUTO_APPEND){listing.capped=true;break}
        // 后台刷新只短暂让路，不能让整页预取停住。
        for(let waited=0;(catalogAppending||archiveActive)&&waited<1500;waited+=50){await pause(50);if(!current())return}
        if(catalogAppending){await pause(50);continue}
        cursors.add(cursor);catalogAppending=true;
        try{
          const next=new URLSearchParams(query);next.set('cursor',cursor);next.set('tree','0');next.set('background','1');next.set('limit','240');
          next.set('client',catalogSearchClient+'_prefetch');next.set('sequence',++catalogPrefetchSequence);
          const data=await api('/api/catalog/items?'+next,undefined,20000,controller.signal);
          if(!current())return;
          cursor=data.next_cursor;more=!!data.has_more;
          paintCatalogItems(data,{append:true,request:catalogRequest});
          ordered.push(...data.rows);
          // 整页登记（含上一轮已追加的卡片）：目录读取中重新续载时，旧轮次取消的图片要重新排队。
          FramekeepAssetLoading.prefetch(grid,urls(data.rows),{signal:controller.signal}).then(scheduleCatalogLoading);
        }finally{catalogAppending=false}
        scheduleCatalogLoading();
      }
      if(!current())return;
      listing.listing=false;
      if(!visitRunning&&!listing.capped&&!more)restoreCatalogOrder(ordered);
      scheduleCatalogLoading();
    })().catch(error=>{
      if(!current())return;
      listing.listing=false;catalogListing=null;
      $('catalog-continuous-status').textContent=attempt<2?'列表续载暂未完成，稍后自动重试':'列表续载暂未完成，可刷新当前文件夹重试';
      if(attempt<2){catalogPrefetchKey='';catalogPrefetchRetry=setTimeout(()=>{if(current())prefetchCatalogFolder(first,query,scope,attempt+1)},3000*(attempt+1))}
    });
  }
  // 新目录的浅层核对在后台进行；完成后立即重读列表，不等待2秒轮询或下一次刷新。
  let visitFollow=0;
  async function followVisit(scope,revision){
    const id=++visitFollow,started=Date.now();
    while(id===visitFollow&&scope===catalogLoadedScope&&FramekeepWorkspace.current()==='archive'&&Date.now()-started<180000){
      await new Promise(resolve=>setTimeout(resolve,300));
      if(id!==visitFollow||scope!==catalogLoadedScope)return;
      let visit;try{visit=(await api('/api/catalog/state',undefined,3000)).visits}catch{continue}
      if(id!==visitFollow||scope!==catalogLoadedScope||!visit||visit.root!==selection.root||visit.folder!==String(selection.folder||'').replace(/\\/g,'/').replace(/^\/+|\/+$/g,''))return;
      visitRunning=!!visit.running;
      if(visit.revision!==revision){
        revision=visit.revision;
        while(archiveActive||catalogAppending){await new Promise(resolve=>setTimeout(resolve,100));if(id!==visitFollow||scope!==catalogLoadedScope)return}
        // 目录读取结束：重新完整续载一次并按服务端顺序复位卡片。
        if(!visit.running)catalogPrefetchKey='';
        await items({preserve:true,background:true,files:false}).catch(()=>{});
      }
      if(!visit.running)return;
    }
  }
  async function items({ preserve = false, withTree = false, refreshId = archiveRefresh, background = false,append = false,files = true } = {}) {
    // 访问登记与列表读取并行：列表直接读本机索引，不等访问请求往返。
    const visitRequest=!background&&!append?api('/api/catalog/visit',{root:selection.root,folder:selection.folder}).catch(()=>null):null;
    // 普通文件枚举独立回显，不能让NAS目录工具阻挡照片列表和缩略图请求。
    // 访问跟进只重读照片列表；文件区由目录操作自身刷新，避免打断正在选择的文件。
    if(files)window.FramekeepArchiveFiles?.load().catch(()=>{});
    const scope=JSON.stringify([selection.root,selection.folder,$('catalog-search').value.trim(),$('catalog-media').value,$('catalog-sort').value]);
    if(scope!==catalogLoadedScope){cancelCatalogPrefetch();FramekeepAssetLoading.clearDirectPreviews();catalogLoadedScope=scope;catalogRows.clear();catalogTotal=null;nextCursor='';hasMore=false}
    const request=++catalogRequest;
    const query = new URLSearchParams({ root: selection.root, folder: selection.folder, q: $('catalog-search').value.trim(), media: $('catalog-media').value, sort:$('catalog-sort').value, cursor: append?nextCursor:'', limit: 120,tree:withTree?'1':'0',quick:'1',visual_only:'1',client:catalogSearchClient,sequence:request,background:background?'1':'0' });
    const summaryKey=JSON.stringify(['root','folder','q','media','sort'].map(key=>query.get(key)));
    if(summaryKey!==catalogSummaryKey){catalogSummaryController?.abort();catalogSummaryController=null;catalogSummaryKey=summaryKey;catalogSummaryData=null}
    catalogController?.abort();catalogController=new AbortController();
    let data;
    try{data=await api('/api/catalog/items?'+query,undefined,20000,catalogController.signal)}
    catch(error){
      if(request!==catalogRequest)return;
      catalogLoadFailed=true;$('catalog-grid').removeAttribute('aria-busy');
      $('catalog-summary').textContent='素材列表读取失败，已保留归档目录；可再次点击目录重试';throw error;
    }
    if(request!==catalogRequest)return;
    catalogLoadFailed=false;$('catalog-grid').removeAttribute('aria-busy');
    paintCatalogItems(data,{preserve,request,append});
    const visitState=visitRequest?await visitRequest:null;
    if(request!==catalogRequest)return;
    if(visitState?.mode==='visited')visitRunning=!!visitState.running;
    if(visitState?.mode==='visited'&&visitState.running){
      if(!data.rows.length){const empty=$('catalog-grid').querySelector(':scope>.catalog-empty');if(empty)empty.textContent='正在读取文件夹内容…'}
      followVisit(scope,visitState.revision);
    }
    if(!append)prefetchCatalogFolder(data,query,scope);
    else FramekeepAssetLoading.prefetch($('catalog-grid'),data.rows.filter(row=>!row.offline).map(row=>previewUrl(row,420)),{signal:catalogPrefetchController?.signal});
    requestAnimationFrame(loadMoreCatalog);
    if(withTree&&data.tree&&refreshId===archiveRefresh){
      state={...state,...data.tree,roots:data.tree.roots.map(root=>({...root,...referenceProgress.get(root.id)}))};
      catalogRootsLoaded=true;paintArchiveState();
    }
    if(data.tree_deferred){
      const paintSummary=summary=>{
        $('catalog-summary').textContent=`${summary.total} 项素材 · ${summary.engine||'目录数据库'}`;
        if(summary.tree){state={...state,...summary.tree,roots:summary.tree.roots.map(root=>({...root,...referenceProgress.get(root.id)}))};catalogRootsLoaded=true;paintArchiveState()}
      };
      if(catalogSummaryData&&Date.now()-catalogSummaryData.created<5000){paintSummary(catalogSummaryData);return}
      if(catalogSummaryController)return;
      const summaryQuery=new URLSearchParams(query);
      summaryQuery.set('quick','0');summaryQuery.set('tree','1');summaryQuery.set('limit','1');summaryQuery.set('cursor','');
      summaryQuery.set('client',catalogSearchClient+'_stats');summaryQuery.set('sequence',++catalogSummarySequence);summaryQuery.set('background','1');
      const summaryController=new AbortController();catalogSummaryController=summaryController;
      api('/api/catalog/items?'+summaryQuery,undefined,20000,summaryController.signal).then(summary=>{
        if(summaryKey!==catalogSummaryKey||summaryController.signal.aborted)return;
        catalogSummaryData={...summary,created:Date.now()};paintSummary(summary);
      }).catch(error=>{
        if(summaryKey!==catalogSummaryKey||summaryController.signal.aborted||error.name==='AbortError')return;
        if(data.total===null)$('catalog-summary').textContent=`已显示 ${displayRows.length} 项${data.has_more?' · 还有更多':''} · 完整统计暂不可用`;
      }).finally(()=>{if(catalogSummaryController===summaryController)catalogSummaryController=null});
    }
  }
  function inspectorThumbnail(item){
    const previous=$('catalog-selected-preview'),url=previewUrl(item,420);
    if(previous.dataset.previewKey&&previewIdentity(previous.dataset.previewKey)===previewIdentity(url)&&(!previous.complete||previous.naturalWidth))return;
    const thumbnail=[...$('catalog-grid').querySelectorAll('.catalog-thumb img')].find(image=>image.complete&&image.naturalWidth&&previewIdentity(image.currentSrc||image.src)===previewIdentity(url));
    const image=new Image();image.id=previous.id;image.alt=previous.alt;image.decoding='async';image.fetchPriority='high';image.dataset.previewKey=url;
    previous.removeAttribute('src');previous.replaceWith(image);
    let source=url;
    if(thumbnail){
      try{
        const canvas=document.createElement('canvas');canvas.width=thumbnail.naturalWidth;canvas.height=thumbnail.naturalHeight;
        canvas.getContext('2d').drawImage(thumbnail,0,0);source=canvas.toDataURL('image/jpeg',.94);
      }catch{}
    }
    image.src=source;
  }
  function select(item) {
    if(selection.item?.id!==item?.id)$('catalog-rating-status').textContent='';
    selection.item = item;
    $('catalog-inspector-form').hidden = !item;
    $('catalog-inspector-empty').hidden = !!item;
    $('catalog-inspector-status').textContent = item?.source === 'manual' ? '已确认' : item?.source === 'path_rule' ? '路径候选' : item?.source === 'review' ? '待复核' : '';
    document.dispatchEvent(new CustomEvent('framekeep:archive-selection',{detail:item?{item_id:item.id}:null}));
    if (!item) return;
    paintRating(item.rating||0);
    inspectorThumbnail(item);
    $('catalog-selected-format').hidden = false;
    $('catalog-selected-format').textContent=item.has_jpg && item.has_raw?'J/R':item.media_format||item.extension?.slice(1).toUpperCase()||'';
    $('catalog-selected-name').textContent = item.relative;
    if(document.activeElement!==$('catalog-selected-sku'))$('catalog-selected-sku').value = item.sku || '';
    const image=item.media_type==='image';
    $('catalog-rating-controls').hidden=!image;$('catalog-rating-status').hidden=!image;
    $('catalog-selected-source').textContent = item.source === 'manual' ? '人工确认的 SKU 指纹' : item.source === 'portable' ? (item.sku?'目录档案 / 文件元数据中的款号':'当前数据集尚未确认款号') : item.source === 'path_rule' ? `路径候选（未 OCR） · ${Math.round((item.confidence || 0) * 100)}%` : item.media_type==='video'?'专业视频素材 · 编码资料在打开详情时读取并缓存':item.media_type==='design'?'Adobe / 设计源文件 · 已纳入共享索引':'尚未识别，请人工填写';
  }
  let ratingBusy=false;
  function paintRating(rating){
    const badge=$('catalog-selected-rating');badge.hidden=rating<=0;badge.textContent='★'.repeat(rating);badge.setAttribute('aria-label',rating+' 星');
    for(const button of $('catalog-rating-controls').querySelectorAll('[data-catalog-rating]')){
      const value=Number(button.dataset.catalogRating);button.classList.toggle('on',value>0&&value<=rating);button.setAttribute('aria-pressed',String(value===rating));
    }
  }
  async function rate(rating){
    if(!selection.item)return;
    if(window.FramekeepArchiveCapture){
      // 先更新卡片角标与右栏星级（乐观显示），保存失败时由保存队列回退到确认值。
      const id=selection.item.id,previous=catalogRows.get(id)?.rating??selection.item.rating??0;
      ratingTouched.set(id,performance.now());
      if(catalogRows.has(id))paintCatalogValues({ratings:{[id]:rating}});
      else{selection.item={...selection.item,rating};paintRating(rating)}
      try{await window.FramekeepArchiveCapture.rate(id,rating,previous)}catch{/* 队列已显示原因并回退 */}
      return;
    }
    if(ratingBusy)return;
    const id=selection.item.id;let saved=false;ratingBusy=true;
    $('catalog-rating-controls').setAttribute('aria-busy','true');
    for(const button of $('catalog-rating-controls').querySelectorAll('button'))button.disabled=true;
    $('catalog-rating-status').textContent='正在保存星级…';
    try{
      await FramekeepIO.flush();
      await api('/api/catalog/rating',{item_id:id,rating});
      saved=true;
      if(selection.item?.id===id){selection.item.rating=rating;paintRating(rating)}
      await FramekeepIO.reloadRatings();await refresh({});
      if(selection.item?.id===id)$('catalog-rating-status').textContent=rating?`已保存 ${rating} 星`:'已清除星级';
    }catch(error){if(selection.item?.id===id)$('catalog-rating-status').textContent=(saved?'星级已保存，刷新失败：':'保存失败：')+error.message}
    finally{ratingBusy=false;$('catalog-rating-controls').removeAttribute('aria-busy');for(const button of $('catalog-rating-controls').querySelectorAll('button'))button.disabled=false}
  }
  $('catalog-rating-controls').addEventListener('click',event=>{const button=event.target.closest('[data-catalog-rating]');if(button)rate(Number(button.dataset.catalogRating))});
  document.addEventListener('keydown',event=>{
    if(FramekeepWorkspace.current()!=='archive'||event.ctrlKey||event.metaKey||event.altKey||event.target.closest('input,textarea,select,[contenteditable="true"]')||document.querySelector('dialog[open]'))return;
    if(/^[0-5]$/.test(event.key)){event.preventDefault();event.stopPropagation();rate(Number(event.key))}
  },true);
  let previewProgress=null;
  function remainingTime(seconds){
    if(seconds<60)return `${Math.max(1,Math.ceil(seconds))} 秒`;
    const minutes=Math.ceil(seconds/60);
    if(minutes<60)return `${minutes} 分钟`;
    const hours=Math.floor(minutes/60),rest=minutes%60;
    return `${hours} 小时${rest?' '+rest+' 分钟':''}`;
  }
  function paintPreviewProgress(progress){
    if(progress?.mode==='visited'){
      if(progress.thumbnail_cache_enabled===false){$('catalog-preview-status').textContent='直接读取文件缩略图';$('catalog-preview-percent').textContent='直读模式';$('catalog-preview-eta').textContent=progress.error||'不使用缩略图磁盘缓存';$('catalog-scan-status').textContent='当前文件夹直接读取文件内嵌缩略图';runtimeProgress.textContent='';return}
      $('catalog-preview-progress').hidden=false;$('catalog-preview-status').textContent=`当前文件夹缩略图 ${progress.cached} / ${progress.total}`+(progress.running?' · 正在缓存':progress.failed?` · ${progress.failed} 项暂不可用`:' · 已就绪');
      $('catalog-preview-percent').textContent='按访问加载';$('catalog-preview-bar').value=progress.total?100*progress.cached/progress.total:0;
      $('catalog-preview-eta').textContent=progress.error||'再次进入时核对更新，未变化的缩略图直接复用';
      $('catalog-scan-status').textContent='仅加载当前文件夹，不扫描或预缓存其他目录';runtimeProgress.textContent='';return;
    }
    paintRuntimeProgress(progress);
    previewProgress=progress;
    const discovering=state.roots.some(root=>['queued','scanning'].includes(root.status))||!!progress?.checking;
    const panel=$('catalog-preview-progress');
    if(progress?.coverage_initialized===false){panel.hidden=false;$('catalog-preview-status').textContent=`正在核对当前实际缓存…（历史登记 ${progress.cache_audit_checked||0} / ${progress.cache_audit_total||0}）`;$('catalog-preview-percent').textContent='';$('catalog-preview-bar').removeAttribute('value');$('catalog-preview-eta').textContent=progress.cache_audit_error||'核对期间仍可浏览，不会自动重建已释放缓存';return}
    panel.hidden=!discovering&&!progress?.total;
    if(panel.hidden)return;
    if(!progress){$('catalog-preview-status').textContent='正在核对解码总量…';$('catalog-preview-percent').textContent='';$('catalog-preview-bar').removeAttribute('value');$('catalog-preview-eta').textContent='总量核对中，本次完成数继续实时更新';return}
    const {total=0,ready=0,pending=0,failed=0,ignored=0,uncached=0,thumbnails_ready:small=0,eta_seconds:eta,manual_paused=false}=progress||{};
    const percent=total>ignored?Math.floor(ready/(total-ignored)*100):total?100:0;
    const queueComplete=total>0&&!pending&&!failed&&!progress.active_workers&&ready+ignored+uncached===total,complete=!discovering&&!uncached&&queueComplete;
    const skipped=ignored?` · 已跳过 ${ignored} 项`:'';
    $('catalog-preview-status').textContent=complete?`当前已缓存 ${ready} / ${total} 项${skipped} · 全部完成`:uncached&&!pending&&!failed&&!progress.active_workers?`当前已缓存 ${ready} / ${total} 项${skipped} · 未缓存 ${uncached} 项` :queueComplete?`当前已缓存 ${ready} / ${total} 项${skipped} · 当前队列已完成 · 正在检查新增或修改素材`:`当前已缓存 ${ready} / ${total} 项${skipped}${discovering?' · 总量更新中':''}${uncached?' · 未缓存 '+uncached+' 项':''}${failed?' · 失败 '+failed+' 项':''}`;
    $('catalog-preview-percent').textContent=total?percent+'%':'';
    const bar=$('catalog-preview-bar');
    if(discovering&&!total)bar.removeAttribute('value');else bar.value=percent;
    const remaining=$('catalog-preview-eta');
    remaining.textContent=progress.reconcile_error?`后台任务暂时受阻，将自动重试：${progress.reconcile_error}`:progress.fill_scan?.active?progress.manual_paused?'补齐已暂停，继续后从断点核对和解码':`正在准备缺失缓存：已检查 ${progress.fill_scan.checked} 次、新增入队 ${progress.fill_scan.queued} 项，后台同步解码…`:!pending&&!failed&&!progress.active_workers&&uncached?`当前队列无待处理任务；${uncached} 项缓存缺失或已释放，浏览时按需生成`:queueComplete?`预计剩余：0 秒${discovering?'（当前队列，目录变化将自动接续）':''}`:manual_paused?'解码已暂停，继续解码后估算剩余时间':!pending?discovering?'预计剩余：等待待解码素材确认…':failed?'解码已结束，失败项未完成缓存':'预计剩余：正在估算…':Number.isFinite(eta)&&eta>0?`${discovering?'当前队列预计剩余':'预计剩余'}：约 ${remainingTime(eta)}`:'预计剩余：正在估算…';
    panel.title=`缩略图已缓存 ${small} / ${total} 项；缩略图与清晰预览均完成后计为已解码。剩余时间按最近一分钟的实际完成速度估算${discovering?'，总量仍在更新，仅针对当前队列':''}。`;
  }
  function paintArchiveState(){
    if(!catalogRootsLoaded)return;
    $('catalog-count').textContent=`${searching()?state.roots.reduce((n,root)=>n+root.folders.reduce((sum,f)=>sum+f.count,0),0):state.counts.total||0} 项`;
    allSearchFolders.hidden=state.browser||!searching();allSearchFolders.disabled=!selection.root&&!selection.folder;
    const kinds=[];if(state.counts.images)kinds.push(`${state.counts.images} 图片`);if(state.counts.videos)kinds.push(`${state.counts.videos} 视频`);if(state.counts.designs)kinds.push(`${state.counts.designs} 设计源文件`);
    $('catalog-location').textContent=state.roots.length?`${state.roots.length} 个归档位置 · ${state.counts.tagged||0} 项已建立 SKU 指纹${kinds.length?' · '+kinds.join(' / '):''}`:searching()||$('catalog-media').value?'当前筛选没有匹配的归档位置':'尚未添加归档位置';
    const metadata=state.metadata;if(metadata?.total)$('catalog-location').textContent+=` · 资料入库 ${metadata.ready}/${metadata.total}${metadata.pending?'（后台同步中）':''}${metadata.failed?' · '+metadata.failed+' 项读取失败':''}`;
    if(state.browser){$('catalog-count').textContent=state.roots.length+' 个位置';$('catalog-location').textContent=selection.root?window.FramekeepArchiveSelection().path:'选择归档位置，浏览当前文件夹';}
    tree();paintPreviewProgress(previewProgress);window.FramekeepArchiveFiles?.paint();
  }
  async function refresh({ reset = false, contentsOnly = false, background = false } = {}) {
    document.dispatchEvent(new CustomEvent('framekeep:archive-scope',{detail:window.FramekeepArchiveSelection()}));
    const refreshId=++archiveRefresh,treeQuery=new URLSearchParams({q:$('catalog-search').value.trim(),media:$('catalog-media').value});
    archiveActive++;
    try{
    if(!catalogRows.size){catalogSnapshot='';$('catalog-grid').setAttribute('aria-busy','true');$('catalog-grid').innerHTML='<div class="catalog-empty" role="status">正在读取已保存的素材列表…</div>'}
    rememberArchiveView();
    if (reset) { cancelCatalogPrefetch();catalogRows.clear();catalogLoadedScope='';catalogTotal=null;nextCursor='';hasMore=false;catalogSnapshot='';$('catalog-grid').scrollTop=0; }
    if(contentsOnly){
      $('catalog-grid').setAttribute('aria-busy','true');$('catalog-summary').textContent=searching()?'正在搜索素材…':'正在加载目录…';
      await items({preserve:true,withTree:searching(),refreshId,background});return;
    }
    // 素材页先发出，目录在同一轮事件循环并发；统计仍延后，让出首屏资源。
    const firstPaint=new Promise(resolve=>setTimeout(resolve,150));
    const auxiliary=read=>firstPaint.then(()=>refreshId===archiveRefresh?read():undefined);
    catalogTreeController?.abort();catalogTreeController=new AbortController();
    const treeController=catalogTreeController;
    treeQuery.set('client',catalogSearchClient+'_tree');treeQuery.set('sequence',refreshId);
    const results=await Promise.allSettled([
      searching()?Promise.resolve():Promise.resolve().then(()=>api('/api/catalog/tree?'+treeQuery,undefined,20000,treeController.signal)).then(treeState=>{
        if(!treeState)return;
        if(refreshId!==archiveRefresh)return;
        state={...state,...treeState,roots:treeState.roots.map(root=>({...root,...referenceProgress.get(root.id)}))};
        catalogRootsLoaded=true;paintArchiveState();
      }),
      items({preserve:true,withTree:searching(),refreshId,background}),
      auxiliary(()=>api('/api/catalog/state')).then(totals=>{
        if(!totals)return;
        if(refreshId!==archiveRefresh)return;
        paintRuntimeProgress(totals.previews);
        document.dispatchEvent(new CustomEvent('framekeep:decode-progress',{detail:totals.previews}));
        if(totals.previews_initialized!==false||totals.previews?.coverage_initialized===false)paintPreviewProgress(totals.previews);
        if(totals.status_error)throw new Error(totals.status_error);
        if(totals.initialized===false){
          if(!previewProgress){$('catalog-preview-progress').hidden=false;$('catalog-preview-status').textContent='正在读取解码进度…';$('catalog-preview-bar').removeAttribute('value');$('catalog-preview-eta').textContent='后台正在核对缓存，完成后自动更新'}
          return;
        }
        state.counts=totals.counts;state.metadata=totals.metadata;paintArchiveState();if(totals.previews_initialized!==false||totals.previews?.coverage_initialized===false)paintPreviewProgress(totals.previews);
      }),
      auxiliary(()=>api('/api/reference/config')).then(reference=>{
        if(!reference)return;
        if(refreshId!==archiveRefresh)return;
        referenceProgress=new Map((reference.roots||[]).map(root=>[root.id,root]));
        state.roots=state.roots.map(root=>({...root,...referenceProgress.get(root.id)}));paintPreviewProgress(previewProgress);
      }).catch(()=>{})
    ]);
    if(refreshId!==archiveRefresh)return;
    if(results[0].status==='rejected'&&!catalogRootsLoaded){$('catalog-location').textContent='归档位置读取失败，请重试；已保存的目录未被移除';$('catalog-tree').textContent='目录暂时无法读取'}
    const failed=results.find(result=>result.status==='rejected');
    if(failed)throw failed.reason;
    if(refreshError){if($('catalog-scan-status').textContent===refreshError)message('缩略图与清晰预览均已缓存后，计为已解码。');refreshError=''}
    }catch(error){
      if(refreshId!==archiveRefresh)return;
      if(contentsOnly){catalogLoadFailed=true;$('catalog-summary').textContent='目录读取失败，请再次点击重试'}
      throw error;
    }finally{if(refreshId===archiveRefresh)$('catalog-grid').removeAttribute('aria-busy');archiveActive--;requestAnimationFrame(loadMoreCatalog)}
  }
  let archiveVisited=false,archiveRevision='',revisionActive=false;
  function setArchiveThumbnailSettings(value,reload=true){
    if(typeof value?.cache_enabled!=='boolean')return;
    if(typeof value.visited_mode==='boolean')visitedThumbnailMode=value.visited_mode;
    const changed=window.FramekeepArchiveThumbnailCacheEnabled!==value.cache_enabled;window.FramekeepArchiveThumbnailCacheEnabled=value.cache_enabled;
    if(changed){startupPreviews.clear();FramekeepAssetLoading.clearDirectPreviews();cancelCatalogPrefetch();catalogSnapshot='';if(reload&&FramekeepWorkspace.current()==='archive')refresh({reset:true,background:true}).catch(()=>{})}
  }
  document.addEventListener('framekeep:archive-thumbnail-settings',event=>setArchiveThumbnailSettings(event.detail));
  function rememberArchiveView(){
    if(searching())return;
    try{localStorage.setItem('framekeep.archive.view',JSON.stringify({root:selection.root,folder:selection.folder,media:$('catalog-media').value,folders:[...folderStates],drives:[...collapsedDrives]}))}catch{}
  }
  async function loadArchive() {
    // 缩略图模式与目录快照并行读取，首屏不串行等待两次往返。
    const settings=api('/api/catalog/thumbnail-settings',undefined,3000).then(value=>setArchiveThumbnailSettings(value,false)).catch(()=>{});
    const background=archiveVisited;archiveVisited=true;
    if(!background){
      try{
        const saved=JSON.parse(localStorage.getItem('framekeep.archive.view')||'null');
        if(saved){selection.root=String(saved.root||'');selection.folder=String(saved.folder||'');$('catalog-media').value=saved.media||'';for(const [key,open] of saved.folders||[])folderStates.set(key,!!open);for(const key of saved.drives||[])collapsedDrives.add(key)}
      }catch{}
      const request=++archiveRefresh;
      try{
        const boot=window.FRAMEKEEP_SESSION?.archiveBootstrap;
        const sameView=boot&&JSON.stringify(boot.view)===JSON.stringify([selection.root,selection.folder,$('catalog-media').value]);
        const data=sameView?boot:await api('/api/catalog/bootstrap?'+new URLSearchParams({root:selection.root,folder:selection.folder,media:$('catalog-media').value,limit:120}),undefined,1500);
        await settings;
        if(request!==archiveRefresh||FramekeepWorkspace.current()!=='archive')return;
        for(const [url,payload] of Object.entries(data.thumbnails||{}))startupPreviews.set(previewIdentity(url),payload);
        if(selection.root&&!data.tree.roots.some(root=>root.id===selection.root)){selection.root='';selection.folder=''}
        state={...state,...data.tree};catalogRootsLoaded=true;paintArchiveState();
        if(data.items&&$('catalog-sort').value==='modified'){paintCatalogItems(data.items,{cached:true});catalogLoadFailed=true}
      }catch{}
      if(request!==archiveRefresh||FramekeepWorkspace.current()!=='archive')return;
    }
    await settings;
    try{await refresh({ background })}catch(error){reportRefreshError(error)}
  }
  async function refreshArchiveChanges(){
    if(revisionActive||archiveActive)return;revisionActive=true;
    try{
      const data=await api('/api/catalog/revision',undefined,3000);
      if(FramekeepWorkspace.current()!=='archive'||archiveActive)return;
      const revision=JSON.stringify(data.revision);
      if(revision!==archiveRevision||catalogLoadFailed||!catalogRootsLoaded){
        await refresh({background:true});archiveRevision=revision;
      }
    }catch(error){if(FramekeepWorkspace.current()==='archive')reportRefreshError(error)}finally{revisionActive=false}
  }
  archive.addEventListener('click', async event => {
    const toggle=event.target.closest('[data-folder-toggle]');
    if(toggle){
      const key=toggle.dataset.folderToggle,collapsed=toggle.getAttribute('aria-expanded')==='true';
      if(collapsed)collapsedResultFolders.add(key);else collapsedResultFolders.delete(key);
      toggle.setAttribute('aria-expanded',String(!collapsed));toggle.querySelector('.catalog-folder-arrow').textContent=collapsed?'▸':'▾';
      const title=toggle.querySelector('strong').textContent;toggle.setAttribute('aria-label',(collapsed?'展开':'折叠')+'文件夹 '+title);
      $(toggle.getAttribute('aria-controls')).hidden=collapsed;
      try{localStorage.setItem('framekeep-catalog-collapsed-results-v1',JSON.stringify([...collapsedResultFolders]))}catch{}
      return;
    }
    const drive=event.target.closest('[data-catalog-drive]');
    if(drive){const key='catalog|'+drive.dataset.catalogDrive,open=drive.getAttribute('aria-expanded')!=='true',host=$(drive.getAttribute('aria-controls'));drive.setAttribute('aria-expanded',String(open));host.hidden=!open;if(open){collapsedDrives.delete(key);for(const node of host.querySelectorAll('details[open]'))hydrateCatalogBranch(node)}else collapsedDrives.add(key);rememberArchiveView();return}
    const folder = event.target.closest('[data-root]');
    if (folder) {
      if(selection.root===folder.dataset.root&&selection.folder===folder.dataset.folder&&!catalogLoadFailed)return;
      selection.root=folder.dataset.root;selection.folder=folder.dataset.folder;paintFolderSelection();select(null);
      try{await refresh({reset:true,contentsOnly:true})}catch(error){message(error.message)}
      return;
    }
    const image = event.target.closest('[data-catalog-item]');
    if (image) { select(catalogRows.get(image.dataset.catalogItem)||null); $('catalog-grid').querySelectorAll('.catalog-card').forEach(node => node.classList.toggle('active', node.dataset.catalogItem === image.dataset.catalogItem)); }
  });
  function openImage(event,mode){
    const button=event.target.closest('.catalog-card[data-catalog-item]'),inspector=mode==='archive'&&event.target.id==='catalog-selected-preview';
    const row=button?(mode==='archive'?catalogRows:dbRows).get(button.dataset.catalogItem):inspector?selection.item:null;if(!row)return;
    event.preventDefault();event.stopPropagation();let rows=[...(mode==='archive'?catalogRows:dbRows).values()];
    if(mode==='archive'&&$('catalog-grid').classList.contains('catalog-grouped'))rows=rows.filter(item=>!collapsedResultFolders.has(JSON.stringify([item.root,item.folder])));
    FramekeepAssetViewer.open(mode,rows.some(item=>item.id===row.id)?rows:[row],row.id,button||event.target);
  }
  const quickSelected={archive:null,'sku-database':null};
  for(const [host,mode] of [[archive,'archive'],[skuPage,'sku-database']]){
    host.addEventListener('click',event=>{const card=event.target.closest('.catalog-card[data-catalog-item]');if(card){quickSelected[mode]=card.dataset.catalogItem;if(mode==='sku-database'){if(dbDragClick){dbDragClick=false;return}chooseDatabaseCard(card.dataset.catalogItem,event).catch(dbError)}}});
    host.addEventListener('dblclick',event=>openImage(event,mode));
    host.addEventListener('click',event=>{if(event.detail===0&&event.target.closest('.catalog-card[data-catalog-item]'))openImage(event,mode)});
  }
  document.addEventListener('keydown',event=>{
    const mode=FramekeepWorkspace.current();
    if(event.key!==' '||event.repeat||!['archive','sku-database'].includes(mode)||event.ctrlKey||event.metaKey||event.altKey||event.isComposing||event.target.closest('input,textarea,select,[contenteditable=true]')||document.querySelector('dialog[open]'))return;
    if(event.target.closest('button,a,[role=button]')&&!event.target.closest('.catalog-card'))return;
    const host=mode==='archive'?archive:skuPage;
    const id=mode==='archive'?selection.item?.id:quickSelected[mode];
    const selected=event.target.closest('.catalog-card[data-catalog-item]')||host.querySelector('.catalog-card.active')||(id?host.querySelector(`[data-catalog-item="${CSS.escape(id)}"]`):null);
    if(!selected)return;
    event.preventDefault();event.stopImmediatePropagation();
    openImage({target:selected,preventDefault(){},stopPropagation(){}},mode);
  },true);
  const locationDialog=document.createElement('dialog');locationDialog.id='catalog-location-dialog';locationDialog.setAttribute('aria-labelledby','catalog-location-title');
  locationDialog.innerHTML=`<form id="catalog-location-form"><div class="dialog-heading"><h2 id="catalog-location-title">添加归档文件夹</h2><button id="catalog-location-close" type="button" aria-label="关闭">×</button></div><label for="catalog-location-path">本地或网络文件夹路径</label><div class="catalog-location-path-row"><input id="catalog-location-path" required autocomplete="off" spellcheck="false" placeholder="D:\照片、X:\照片 或 \\192.168.0.181\共享\照片"><button id="catalog-location-browse" type="button" class="bordered">浏览</button></div><p class="dialog-copy">支持本地磁盘、已连接的网络驱动器和 IP／主机名共享路径。网络共享须先在 Windows 中具备访问权限。</p><p id="catalog-location-error" role="status"></p><div class="dialog-actions"><button id="catalog-location-cancel" type="button">取消</button><button id="catalog-location-submit" type="submit" class="primary">添加文件夹</button></div></form>`;
  document.body.append(locationDialog);
  $('catalog-add-root').onclick=()=>{$('catalog-location-error').textContent='';$('catalog-location-path').value='';locationDialog.showModal();$('catalog-location-path').focus()};
  for(const id of ['catalog-location-close','catalog-location-cancel'])$(id).onclick=()=>locationDialog.close();
  $('catalog-location-browse').onclick=async()=>{try{const folder=await getFolder();if(folder)$('catalog-location-path').value=folder}catch(error){$('catalog-location-error').textContent=error.message}};
  $('catalog-location-form').onsubmit=async event=>{
    event.preventDefault();const button=$('catalog-location-submit');if(button.disabled)return;button.disabled=true;button.textContent='正在索引…';$('catalog-location-error').textContent='';
    try{const folder=$('catalog-location-path').value.trim(),result=await api('/api/catalog/add',{folder});selection.root=result.root_id;selection.folder='';select(null);await refresh({reset:true});locationDialog.close();message('已添加归档位置，仅加载当前文件夹；进入子目录时再读取。')}
    catch(error){$('catalog-location-error').textContent=error.message}
    finally{button.disabled=false;button.textContent='添加文件夹'}
  };
  $('catalog-scan').onclick = async () => {
    try {
      const roots = state.roots;
      if (!roots.length) { message('请先添加归档文件夹'); return; }
      $('catalog-scan').disabled = true; $('catalog-scan').textContent = '正在排队…';
      if(!selection.root){message('请先选择要刷新的文件夹');return}
      await api('/api/catalog/visit',{root:selection.root,folder:selection.folder,refresh:true});
      message('正在核对当前文件夹的新增、修改和删除。'); await refresh({ reset: true });
    } catch (error) { message(error.message); }
    finally { $('catalog-scan').disabled = false; $('catalog-scan').textContent = '刷新当前文件夹'; }
  };
  $('catalog-search').addEventListener('input', () => {
    if(state.browser){select(null);refresh({reset:true,contentsOnly:true}).catch(error=>message(error.message));return}
    if(searching()){
      if(!beforeSearch){rememberTree($('catalog-tree'));beforeSearch={root:selection.root,folder:selection.folder,folders:new Map(folderStates),drives:new Set(collapsedDrives)}}
      selection.root='';selection.folder='';
    }else if(beforeSearch){
      selection.root=beforeSearch.root;selection.folder=beforeSearch.folder;folderStates.clear();for(const [key,value] of beforeSearch.folders)folderStates.set(key,value);collapsedDrives.clear();for(const key of beforeSearch.drives)collapsedDrives.add(key);beforeSearch=null;skipRememberTree=true;
    }
    // 改词只更新匹配列表与目录；全库统计和预览进度由正常后台刷新更新，
    // 不在每次输入时重复发起全库资料统计。清空时恢复完整目录状态。
    select(null);refresh({ reset: true, contentsOnly: searching() }).catch(error => message(error.message));
  });
  $('catalog-sort').addEventListener('change',()=>refresh({reset:true}).catch(error=>message(error.message)));
  $('catalog-media').addEventListener('change', () => refresh({ reset: true }).catch(error => message(error.message)));
  async function loadMoreCatalog(){
    const grid=$('catalog-grid');
    if(FramekeepWorkspace.current()!=='archive'||catalogAppending||archiveActive||!hasMore||!nextCursor||grid.scrollHeight-grid.scrollTop-grid.clientHeight>600)return;
    catalogAppending=true;$('catalog-continuous-status').textContent='正在加载更多素材…';
    try{await items({append:true,background:true})}
    catch(error){if(error.name!=='AbortError')$('catalog-continuous-status').textContent='加载暂未完成，继续滚动重试：'+error.message}
    finally{catalogAppending=false;requestAnimationFrame(loadMoreCatalog)}
  }
  $('catalog-grid').addEventListener('scroll',()=>loadMoreCatalog(),{passive:true});
  const dbPicked=new Map(),dbOrdered=new Map();
  let dbPickScope='',dbAnchor=null,dbSelectionVersion=0,dbDrag=null,dbDragClick=false,dbDragFrame=0;
  const dbScope=()=>JSON.stringify([dbMode,dbRoot,dbFolder,dbBin,dbSku,$('sku-database-search').value,$('sku-database-sort').value]);
  const dbParams=()=>({q:$('sku-database-search').value,sort:$('sku-database-sort').value,root:dbRoot,folder:dbFolder,sku:dbSku,bin_id:dbBin});
  const dbIndex=id=>[...dbOrdered].find(([,row])=>row.id===id)?.[0];
  function paintDatabaseSelection(){
    for(const card of $('sku-database-grid').querySelectorAll('[data-catalog-item]')){
      const picked=dbPicked.has(card.dataset.catalogItem);card.classList.toggle('active',picked);card.setAttribute('aria-pressed',String(picked));
    }
    $('sku-database-grid').dispatchEvent(new CustomEvent('sku-selection-change',{detail:dbPicked.size}));
  }
  function clearDatabaseSelection(){dbPicked.clear();dbOrdered.clear();dbAnchor=null;dbSelectionVersion++;paintDatabaseSelection()}
  async function chooseDatabaseCard(id,event={}){
    const index=dbIndex(id);if(index===undefined)return;
    const version=++dbSelectionVersion,scope=dbPickScope;
    if(event.shiftKey&&dbAnchor!==null){
      const start=Math.min(index,dbAnchor),end=Math.max(index,dbAnchor);
      if(end-start>=2000)throw new Error('一次最多选择 2000 张照片，请缩小区间');
      for(let offset=start;offset<=end;){
        if(dbOrdered.has(offset)){offset++;continue}
        const data=await api('/api/sku/library?'+new URLSearchParams({...dbParams(),offset,limit:Math.min(240,end-offset+1)}));
        if(version!==dbSelectionVersion||scope!==dbScope())return;
        if(!data.rows.length)throw new Error('图片列表已变化，请重新选择');
        data.rows.forEach((row,i)=>dbOrdered.set(offset+i,row));offset+=data.rows.length;
      }
      dbPicked.clear();for(let i=start;i<=end;i++){const row=dbOrdered.get(i);dbPicked.set(row.id,row)}
    }else if(event.ctrlKey||event.metaKey){
      if(dbPicked.has(id))dbPicked.delete(id);else dbPicked.set(id,dbOrdered.get(index));dbAnchor=index;
    }else{dbPicked.clear();dbPicked.set(id,dbOrdered.get(index));dbAnchor=index}
    paintDatabaseSelection();
  }
  const dbGrid=$('sku-database-grid');
  dbGrid.addEventListener('dragstart',event=>event.preventDefault());
  dbGrid.addEventListener('pointerdown',event=>{
    dbDragClick=false;const card=event.target.closest('[data-catalog-item]');if(event.button!==0||!event.shiftKey||!card)return;
    event.preventDefault();dbDragClick=false;dbDrag={id:card.dataset.catalogItem,x:event.clientX,y:event.clientY,moved:false};
    if(dbAnchor===null)dbAnchor=dbIndex(dbDrag.id);
    chooseDatabaseCard(dbDrag.id,{shiftKey:true}).catch(dbError);
    function step(){
      if(!dbDrag)return;const box=dbGrid.getBoundingClientRect(),margin=45;
      const speed=dbDrag.y>box.bottom-margin?18:dbDrag.y<box.top+margin?-18:0;
      if(speed){dbGrid.scrollTop+=speed;extendDrag()}
      dbDragFrame=requestAnimationFrame(step);
    }
    dbDragFrame=requestAnimationFrame(step);
  });
  function extendDrag(){
    if(!dbDrag)return;
    const box=dbGrid.getBoundingClientRect(),card=document.elementFromPoint(dbDrag.x,Math.max(box.top+4,Math.min(dbDrag.y,box.bottom-4)))?.closest('#sku-database-grid [data-catalog-item]');
    if(card&&card.dataset.catalogItem!==dbDrag.id){dbDrag.id=card.dataset.catalogItem;dbDrag.moved=true;chooseDatabaseCard(dbDrag.id,{shiftKey:true}).catch(dbError)}
  }
  document.addEventListener('pointermove',event=>{if(!dbDrag)return;dbDrag.x=event.clientX;dbDrag.y=event.clientY;extendDrag()});
  function stopDatabaseDrag(){if(dbDrag){dbDragClick=dbDrag.moved;dbDrag=null}cancelAnimationFrame(dbDragFrame)}
  document.addEventListener('pointerup',stopDatabaseDrag);document.addEventListener('pointercancel',stopDatabaseDrag);window.addEventListener('blur',stopDatabaseDrag);
  dbGrid.addEventListener('scroll',extendDrag,{passive:true});
  let dbBatchKey='',dbBatchToken=0,dbLoadedRows=[];
  const databaseCards=rows=>rows.map(row=>`<div class="ocr-library-card">${card(row)}<button class="bordered ocr-library-details" ${['portable','catalog'].includes(row.source)?`data-capture-details="${esc(row.catalog_item_id||row.id)}"`:`data-ocr-session="${esc(row.session_id)}" data-ocr-capture="${esc(row.capture_id)}"`}>OCR 资料 / 选择 SKU</button></div>`).join('');
  const databaseGridKey=(scope,show,loading,rows)=>JSON.stringify([scope,show,!!loading,rows]);
  async function fillDatabase(scope,token,revision,total){
    dbActive++;
    try{
      while(dbLoadedRows.length<total){
        const offset=dbLoadedRows.length;
        const next=await api('/api/sku/library?'+new URLSearchParams({...dbParams(),offset,limit:240}));
        if(token!==dbBatchToken||scope!==dbScope())return;
        if(next.loading||next.revision!==revision){dbBatchKey='';renderDatabase().catch(dbError);return}
        if(!next.rows.length)throw new Error('图片列表已变化，正在重新读取');
        dbLoadedRows.push(...next.rows);
        next.rows.forEach((row,i)=>{dbOrdered.set(offset+i,row);dbRows.set(row.id,row)});
        dbGrid.insertAdjacentHTML('beforeend',databaseCards(next.rows));
        dbGrid.dataset.contentKey=databaseGridKey(scope,true,false,dbLoadedRows);
        paintDatabaseSelection();FramekeepAssetLoading.watch(dbGrid);dbSnapshot='';
        if(!next.has_more)break;
      }
    }catch(error){if(token===dbBatchToken&&scope===dbScope()){dbBatchKey='';dbError(error)}}
    finally{dbActive--}
  }
  async function renderDatabase(options={}) {
    const scope=dbScope();if(scope!==dbPickScope){dbPickScope=scope;clearDatabaseSelection()}
    const request=++dbRequest;
    let data;dbActive++;
    try{data=await api('/api/sku/library?'+new URLSearchParams({...dbParams(),offset:0,limit:240,fresh:options.fresh?'1':'0'}))}finally{dbActive--}
    if(request!==dbRequest)return;
    if(!data.loading&&data.groups.length&&dbMode==='flat'&&!$('sku-database-search').value.trim()&&!data.groups.some(row=>row.normalized===dbSku)){
      dbSku=data.groups[0].normalized;return renderDatabase();
    }
    const batchKey=JSON.stringify([scope,data.revision,data.total,!!data.loading,data.rows]);
    const newBatch=batchKey!==dbBatchKey;
    if(newBatch){dbBatchKey=batchKey;dbBatchToken++;dbLoadedRows=[...data.rows]}
    data={...data,rows:dbLoadedRows};
    if(dbSku)try{localStorage.setItem('framekeep.sku.last',dbSku)}catch{}
    data.rows.forEach((row,index)=>dbOrdered.set(data.offset+index,row));
    for(const row of data.rows)if(dbPicked.has(row.id)&&dbPicked.get(row.id).sku!==row.sku){dbPicked.clear();dbAnchor=null;dbSelectionVersion++;break}
    window.FramekeepSkuLibrary?.onData?.(data);
    const snapshot=JSON.stringify([dbMode,dbRoot,dbFolder,dbBin,dbSku,$('sku-database-search').value.trim(),data]);if(snapshot===dbSnapshot)return;dbSnapshot=snapshot;dbRows=new Map(data.rows.map(row=>[row.id,row]));
    const host=$('sku-database-tree'),treeKey=JSON.stringify(data.roots);rememberTree(host);if(host.dataset.contentKey!==treeKey){host.innerHTML=treeMarkup(data.roots,'sku');host.dataset.contentKey=treeKey}
    const list=$('sku-database-list'),listKey=JSON.stringify([dbBin==='trash',!!data.loading,data.groups]);
    if(list.dataset.contentKey!==listKey||data.loading){list.innerHTML=data.groups.length?data.groups.map(row=>`<button type="button" data-db-sku="${esc(row.normalized)}" draggable="${dbBin!=='trash'}" title="双击修改 SKU 款号 · 最近入库 ${esc(skuAddedDate(row.created))}"><span class="sku-code-info"><strong>${esc(row.sku)}</strong>${row.customer||row.brand?`<small>${esc([row.customer,row.brand].filter(Boolean).join(" / "))}</small>`:''}${skuDate(row.created)}</span><span>${row.count} 张</span></button>`).join(''):`<div class="catalog-empty">${data.loading?'正在读取已入库 SKU…':'此处暂无 SKU'}</div>`;list.dataset.contentKey=listKey}
    for(const button of skuPage.querySelectorAll('[data-db-sku]'))button.classList.toggle('active',button.dataset.dbSku===dbSku);
    $('sku-library-count').textContent=data.loading?'正在读取已入库款号…':`${data.groups.length} 个 SKU · ${data.interval_count||0} 个区间`;
    const query=$('sku-database-search').value.trim(),group=data.groups.find(row=>row.normalized===dbSku),show=!!dbSku||dbMode==='tree'||!!query;
    $('sku-database-title').textContent=group?`${group.sku} · ${data.total} 张照片`:query?`搜索“${query}” · ${data.total} 张照片`:dbMode==='tree'?`${dbFolder||'所有已识别目录'} · ${data.total} 张照片`:'选择一个 SKU';
    if(data.loading)$('sku-database-title').textContent='正在读取已入库 SKU…';else if(data.refreshing)$('sku-database-title').textContent+=' · 正在同步更新';
    const grid=$('sku-database-grid'),gridKey=databaseGridKey(scope,show,data.loading,data.rows);
    if(grid.dataset.contentKey!==gridKey){
      grid.innerHTML=data.loading?'<div class="catalog-empty">正在读取已入库图片资料…</div>':show?(databaseCards(data.rows)||'<div class="catalog-empty">没有符合条件的 OCR 照片</div>'):'<div class="catalog-empty">选择一个 SKU 查看对应照片</div>';
      grid.dataset.contentKey=gridKey;FramekeepAssetLoading.watch(grid);
    }
    paintDatabaseSelection();
    if(newBatch&&show&&!data.loading&&data.has_more)fillDatabase(scope,dbBatchToken,data.revision,data.total);
  }
  const dbError=error=>{dbSnapshot='';$('sku-database-title').textContent='读取失败：'+error.message};
  window.FramekeepSkuLibrary={
    onData:null,
    get selection(){return {sku:dbSku,bin:dbBin}},
    get selectedRows(){return [...dbPicked.values()]},
    get selecting(){return !!dbDrag},
    async selectCard(id,options={}){if(!dbPicked.has(id)||!options.preserve)await chooseDatabaseCard(id,options);return dbRows.get(id)},
    async selectBin(bin){dbBin=bin;dbSku='';dbRoot='';dbFolder='';dbSnapshot='';await renderDatabase()},
    async refresh(options={}){if(options.sku!==undefined)dbSku=options.sku;if(options.bin!==undefined)dbBin=options.bin;if(options.clearSearch)$('sku-database-search').value='';if(options.clearSelection)clearDatabaseSelection();dbSnapshot='';await renderDatabase({fresh:true})}
  };
  skuPage.addEventListener('click',event=>{
    const details=event.target.closest('[data-ocr-capture]');if(details){FramekeepOcrDetails.open(details.dataset.ocrSession,details.dataset.ocrCapture).catch(dbError);return}
    const tab=event.target.closest('[data-sku-view]');
    if(tab){dbMode=tab.dataset.skuView;dbRoot='';dbFolder='';for(const button of skuPage.querySelectorAll('[data-sku-view]'))button.setAttribute('aria-selected',String(button===tab));$('sku-database-list').hidden=dbMode!=='flat';$('sku-database-tree').hidden=dbMode!=='tree';renderDatabase().catch(dbError);return}
    const drive=event.target.closest('[data-db-drive]');
    if(drive){const key='sku|'+drive.dataset.dbDrive,open=drive.getAttribute('aria-expanded')!=='true';drive.setAttribute('aria-expanded',String(open));$(drive.getAttribute('aria-controls')).hidden=!open;if(open)collapsedDrives.delete(key);else collapsedDrives.add(key);return}
    const item=event.target.closest('[data-db-sku]');
    if(item){dbSku=item.dataset.dbSku;dbRoot=item.dataset.dbRoot||'';dbFolder=item.dataset.dbFolder||'';renderDatabase().catch(dbError);return}
    const folder=event.target.closest('[data-db-root]');
    if(folder){dbRoot=folder.dataset.dbRoot;dbFolder=folder.dataset.dbFolder;dbSku='';renderDatabase().catch(dbError)}
  });
  $('sku-database-search').addEventListener('input',()=>{dbSku='';renderDatabase().catch(dbError)});
  $('sku-database-search').placeholder='搜索 SKU、OCR 全文或辅助信息';
  $('sku-database-sort').addEventListener('change',()=>renderDatabase().catch(dbError));
  const contextMenu=document.createElement('div');contextMenu.id='catalog-context-menu';contextMenu.className='photo-context-menu';contextMenu.setAttribute('role','menu');contextMenu.hidden=true;
  contextMenu.innerHTML='<button role="menuitem" data-catalog-action="open">打开路径</button><button role="menuitem" data-catalog-action="remove">移出归档</button>';document.body.append(contextMenu);
  let context=null;
  function closeContext(){contextMenu.hidden=true;context=null}
  $('catalog-tree').addEventListener('contextmenu',event=>{
    const folder=event.target.closest('[data-root]'),drive=event.target.closest('[data-catalog-drive]');if(!folder&&!drive)return;
    event.preventDefault();context=folder?{roots:[folder.dataset.root],folder:folder.dataset.folder||''}:{roots:state.roots.filter(root=>driveOf(root.path)===drive.dataset.catalogDrive).map(root=>root.id),folder:''};
    contextMenu.hidden=false;contextMenu.style.left=Math.max(4,Math.min(event.clientX,innerWidth-contextMenu.offsetWidth-4))+'px';contextMenu.style.top=Math.max(4,Math.min(event.clientY,innerHeight-contextMenu.offsetHeight-4))+'px';contextMenu.querySelector('button').focus();
  });
  contextMenu.addEventListener('click',async event=>{
    const button=event.target.closest('[data-catalog-action]');if(!button||!context)return;
    const chosen=context,action=button.dataset.catalogAction;closeContext();
    try{
      if(action==='open')await api('/api/catalog/open',{root_id:chosen.roots[0],folder:chosen.folder});
      else {for(const root of chosen.roots)await api('/api/catalog/remove',{root_id:root,folder:chosen.folder});if(chosen.roots.includes(selection.root)){selection.root='';selection.folder=''}select(null);await refresh({reset:true});message('已移出归档，磁盘原文件保持不变。可重新添加归档位置恢复。')}
    }catch(error){message(error.message)}
  });
  document.addEventListener('pointerdown',event=>{if(!contextMenu.contains(event.target))closeContext()},true);
  document.addEventListener('keydown',event=>{if(!contextMenu.hidden&&event.key==='Escape'){event.preventDefault();closeContext()}});
  document.addEventListener('framekeep:workspace',closeContext);window.addEventListener('blur',closeContext);
  let progressActive=false,visitTreeRevision='';
  async function refreshDecodeProgress(){
    if(progressActive)return;progressActive=true;
    try{
      const totals=await api('/api/catalog/state');
      if(FramekeepWorkspace.current()!=='archive')return;
      if(totals.visits){
        const key=JSON.stringify([totals.visits.root,totals.visits.folder,totals.visits.revision]);
        if(key!==visitTreeRevision){visitTreeRevision=key;const tree=await api('/api/catalog/tree');state={...state,...tree};paintArchiveState()}
        paintPreviewProgress(totals.visits);return;
      }
      if(totals.initialized!==false){state.counts=totals.counts;state.metadata=totals.metadata;paintArchiveState()}
      if(!totals.status_error&&refreshError){if($('catalog-scan-status').textContent===refreshError)message('缩略图与清晰预览均已缓存后，计为已解码。');refreshError=''}
      if(totals.previews_initialized===false){paintRuntimeProgress(totals.previews);return}
      paintPreviewProgress(totals.previews);
      document.dispatchEvent(new CustomEvent('framekeep:decode-progress',{detail:totals.previews}));
    }catch{}finally{progressActive=false}
  }
  const databaseTimer=setInterval(()=>{
    if(!document.hidden&&FramekeepWorkspace.current()==='archive')api('/api/catalog/visit',{root:selection.root,folder:selection.folder}).catch(()=>{});
    if(document.hidden||document.getElementById('asset-image-viewer')?.open)return;
    if(!dbActive&&FramekeepWorkspace.current()==='sku-database')renderDatabase().catch(dbError);
    // 保留浏览中的素材列表，仅更新独立进度；列表由切回页面或显式操作更新。
    if(FramekeepWorkspace.current()==='archive'){
      if(!catalogRatingController){
        const pending=[...catalogRows.values()].filter(row=>!row.metadata_status||['queued','running'].includes(row.metadata_status)).slice(0,200);
        if(pending.length)hydrateCatalogRatings(pending,catalogRequest);
      }
      refreshDecodeProgress();
      refreshArchiveChanges();
    }
  },2000);
  function cancelCatalogSearch(){cancelCatalogPrefetch();++catalogRequest;++archiveRefresh;catalogController?.abort();catalogTreeController?.abort();catalogTreeController=null;catalogRatingController?.abort();catalogRatingController=null;catalogSummaryController?.abort();catalogSummaryController=null;catalogSummaryKey='';catalogSummaryData=null}
  document.addEventListener('framekeep:workspace',()=>{if(FramekeepWorkspace.current()!=='archive'){cancelCatalogSearch();api('/api/catalog/visit',{root:''}).catch(()=>{})}});
  document.addEventListener('visibilitychange',()=>{if(FramekeepWorkspace.current()==='archive'){if(document.hidden)cancelCatalogPrefetch();else refresh({background:true}).catch(()=>{});api('/api/catalog/visit',{root:document.hidden?'':selection.root,folder:selection.folder,refresh:!document.hidden}).catch(()=>{})}});
  window.addEventListener('pagehide',()=>{clearInterval(databaseTimer);cancelCatalogSearch()},{once:true});
  window.FramekeepArchiveBrowser={enabled:()=>!!state.browser,scope:()=>({...selection}),rows:()=>[...catalogRows.values()],roots:()=>state.roots,refresh:()=>refresh({reset:true}),async navigate(root,folder){selection.root=root;selection.folder=folder;for(const part of ['',...folder.split('/').map((_,i,a)=>a.slice(0,i+1).join('/'))])folderStates.set('catalog|'+root+'|'+part,true);select(null);paintFolderSelection();await refresh({reset:true})}};
  FramekeepWorkspace.register('archive', archiveTab, archive, loadArchive);
  document.addEventListener('framekeep:metadata-imported',()=>{if(FramekeepWorkspace.current()==='archive')refresh({}).catch(error=>message(error.message))});
  document.addEventListener('framekeep:design-previews-repaired',()=>{
    for(const image of $('catalog-grid').querySelectorAll('img[data-preview-src]')){
      const row=catalogRows.get(image.closest('[data-catalog-item]')?.dataset.catalogItem);
      if(row?.media_type==='design')FramekeepAssetLoading.retry(image,previewUrl(row,420));
    }
    if(selection.item?.media_type==='design'){$('catalog-selected-preview').dataset.previewKey='';inspectorThumbnail(selection.item)}
  });
  FramekeepWorkspace.register('sku-database', skuTab, skuPage, renderDatabase);
  document.addEventListener('framekeep:workspace',()=>{try{localStorage.setItem('framekeep.last-workspace',FramekeepWorkspace.current())}catch{}});
  try{const last=localStorage.getItem('framekeep.last-workspace');if(['sku-database','archive'].includes(last))FramekeepWorkspace.show(last)}catch{}
})();
