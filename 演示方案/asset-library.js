(() => {
  'use strict';
  if (!window.FramekeepIO || !window.FramekeepWorkspace) return;
  const $ = id => document.getElementById(id);
  const api = FramekeepIO.api;
  const previewUrl = (item, size) => {
    const url = new URL(item.preview, location.href);
    url.searchParams.set('size', size);
    return url.pathname + url.search;
  };
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
      <aside id="catalog-folders" class="catalog-tree-panel"><header><strong>目录</strong><span id="catalog-count"></span></header><div id="catalog-tree" class="catalog-tree" role="tree"></div><footer class="catalog-tree-actions"><button id="catalog-add-root" class="bordered">添加归档文件夹</button><button id="catalog-scan" class="primary">刷新归档索引</button></footer></aside>
      <main class="catalog-main"><header class="catalog-toolbar"><label><span>搜索素材</span><input id="catalog-search" type="search" placeholder="文件名、SKU、标题、关键词、相机或拍摄资料"></label><label><span>类型</span><select id="catalog-media"><option value="">全部</option><option value="image">图片 / RAW</option><option value="video">视频</option><option value="design">Adobe / 设计源文件</option></select></label><span id="catalog-summary" role="status"></span></header><div id="catalog-grid" class="catalog-grid" aria-live="polite"></div><footer class="catalog-pagination"><button id="catalog-prev">上一页</button><button id="catalog-next">下一页</button></footer></main>
      <aside class="catalog-inspector"><header><strong>素材资料与 SKU</strong><span id="catalog-inspector-status"></span></header><div id="catalog-inspector-empty" class="catalog-empty">选择素材以检查与修正 SKU</div><div id="catalog-inspector-form" hidden><div class="catalog-preview-wrap"><img id="catalog-selected-preview" alt="当前归档素材预览"><span id="catalog-selected-rating" class="photo-rating-badge" hidden></span><span id="catalog-selected-format" class="photo-format-badge" hidden></span></div><p id="catalog-selected-name"></p><div id="catalog-rating-controls" class="catalog-rating-controls" role="group" aria-label="归档图片星级"><button type="button" data-catalog-rating="1" aria-label="归档图片标 1 星">★</button><button type="button" data-catalog-rating="2" aria-label="归档图片标 2 星">★</button><button type="button" data-catalog-rating="3" aria-label="归档图片标 3 星">★</button><button type="button" data-catalog-rating="4" aria-label="归档图片标 4 星">★</button><button type="button" data-catalog-rating="5" aria-label="归档图片标 5 星">★</button><button type="button" data-catalog-rating="0">清星</button></div><p id="catalog-rating-status" role="status"></p><label>SKU 款号<input id="catalog-selected-sku" maxlength="80" placeholder="待确认"></label><p id="catalog-selected-source"></p><button id="catalog-save-sku" class="primary">确认 SKU 元数据</button></div><section class="catalog-scan-panel"><header><strong>共享媒体索引</strong></header><div id="catalog-index-progress" class="catalog-index-progress" hidden><div><span id="catalog-index-status"></span><strong id="catalog-index-percent"></strong></div><progress id="catalog-index-bar" max="100" value="0" aria-label="归档素材索引进度"></progress></div><div id="catalog-preview-progress" class="catalog-index-progress" hidden><div><span id="catalog-preview-status" role="status"></span><strong id="catalog-preview-percent"></strong></div><progress id="catalog-preview-bar" max="100" value="0" aria-label="归档预览生成进度"></progress></div><p id="catalog-scan-status">归档与分屏拍摄共用同一素材数据库；目录变化会自动索引。</p></section></aside>
    </div>`;

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
    </aside><main><header id="sku-database-title" class="catalog-toolbar">选择一个 SKU</header><div id="sku-database-grid" class="catalog-grid"></div><footer class="catalog-pagination"><button id="sku-database-prev">上一页</button><button id="sku-database-next">下一页</button></footer></main></div>`;
  archive.after(skuPage);
  $('sku-database-grid').addEventListener('error',event=>{
    const image=event.target;if(image.tagName!=='IMG'||image.hidden)return;
    image.hidden=true;const hint=document.createElement('span');hint.className='sku-offline-preview';hint.textContent='原照片离线或预览不可用 · 元数据已保留';image.parentElement.append(hint);
  },true);

  let state = { roots: [], counts: {} }, selection = { root: '', folder: '', item: null }, cursors = [''], page = 0, nextCursor = '', hasMore = false, dbSku = '';
  let dbMode='flat',dbRoot='',dbFolder='',dbBin='',dbPage=0,dbRequest=0,dbActive=0,dbMore=false,dbSnapshot='',catalogRequest=0,catalogRows=new Map(),dbRows=new Map(),catalogSnapshot='',archiveActive=0;
  const message = text => { $('catalog-scan-status').textContent = text; };
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
  let catalogRootsLoaded=false,referenceProgress=new Map();
  const searching=()=>$('catalog-search').value.trim()!=='';
  const allSearchFolders=document.createElement('button');allSearchFolders.id='catalog-all-search-folders';allSearchFolders.type='button';allSearchFolders.textContent='全部匹配文件夹';allSearchFolders.hidden=true;$('catalog-summary').before(allSearchFolders);
  allSearchFolders.onclick=()=>{selection.root='';selection.folder='';paintFolderSelection();select(null);refresh({reset:true,contentsOnly:true}).catch(error=>message(error.message))};
  const nodeOpen=key=>folderStates.get(key)!==false?'open':'';
  const folderTree = folders => {
    const root = { children: new Map(), count: 0 };
    for (const row of folders) {
      const parts = row.folder === '/' ? [] : row.folder.replace(/\\/g,'/').split('/').filter(Boolean);
      let node = root; node.count += row.count;
      for (const part of parts) { if (!node.children.has(part)) node.children.set(part, { children: new Map(), count: 0 }); node = node.children.get(part); node.count += row.count; }
      node.directCount=(node.directCount||0)+row.count;node.skus=row.skus||[];
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
    return `<details ${nodeOpen(key)} data-catalog-node="${esc(key)}"><summary role="treeitem" data-${attr}root="${esc(rootId)}" data-${attr}folder="${esc(folder)}" class="${active?'active':''} ${hit?'catalog-search-match':''}" aria-selected="${active}"><span>${esc(name)}</span><small>${child.count}</small></summary>${scope==='sku'?skuButtons(child.skus,rootId,folder):''}${branch(child, rootId, folder,scope)}</details>`;
  }).join('');
  function rememberTree(host){for(const node of host.querySelectorAll('[data-catalog-node]'))folderStates.set(node.dataset.catalogNode,node.open)}
  function treeMarkup(roots,scope='catalog'){
    if(!roots.length)return '<div class="catalog-empty">'+(scope==='sku'?'尚无已识别或已备案的 SKU':searching()?'没有匹配的文件夹':'尚未添加归档文件夹')+'</div>';
    const drives=new Map(),attr=scope==='catalog'?'':'db-';
    for(const root of roots){const drive=driveOf(root.path);if(!drives.has(drive))drives.set(drive,[]);drives.get(drive).push(root)}
    return [...drives.entries()].sort(([a],[b])=>a.localeCompare(b)).map(([drive,roots],index)=>{
      const key=scope+'|'+drive,closed=collapsedDrives.has(key),id=scope+'-drive-folders-'+index;
      return `<section class="catalog-drive-group"><button type="button" class="catalog-drive-button" data-${scope==='catalog'?'catalog':'db'}-drive="${esc(drive)}" aria-expanded="${!closed}" aria-controls="${id}" title="${esc(drive)}"><svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="4" width="18" height="15" rx="2"/><path d="M3 14h18M6 17h2M10 17h2"/></svg><span>${esc(drive)}</span><small>${roots.reduce((sum,r)=>sum+r.folders.reduce((n,f)=>n+f.count,0),0)}</small></button><div id="${id}" class="catalog-drive-folders" ${closed?'hidden':''}>${roots.map(root=>{
        const rootName=root.path.split(/[\\/]/).filter(Boolean).at(-1)||root.path,node=folderTree(root.folders),hit=scope==='catalog'&&searching()&&node.directCount>0,active=scope==='catalog'?(selection.root===root.id&&!selection.folder||hit&&!selection.root):dbRoot===root.id&&!dbFolder&&!dbSku;
        return `<details ${nodeOpen(scope+'|'+root.id+'|')} data-catalog-node="${esc(scope+'|'+root.id+'|')}" class="catalog-root"><summary role="treeitem" data-${attr}root="${esc(root.id)}" data-${attr}folder="" class="${active?'active':''} ${hit?'catalog-search-match':''}" aria-selected="${active}" title="${esc(root.path)}"><span>${esc(rootName)}</span></summary>${scope==='sku'?skuButtons(node.skus,root.id,''):''}${branch(node,root.id,'',scope)}</details>`;
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
    if(skipRememberTree||snapshot!==catalogTreeSnapshot){host.innerHTML=searching()?searchFolderMarkup(state.roots):treeMarkup(state.roots);catalogTreeSnapshot=snapshot}
    skipRememberTree=false;paintFolderSelection();
  }
  function paintFolderSelection(){
    for(const node of $('catalog-tree').querySelectorAll('[data-root]')){
      const active=node.dataset.root===selection.root&&node.dataset.folder===selection.folder||searching()&&!selection.root&&node.classList.contains('catalog-search-match');
      node.classList.toggle('active',active);node.setAttribute('aria-selected',String(active));
    }
    allSearchFolders.hidden=!searching();allSearchFolders.disabled=!selection.root&&!selection.folder;
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
      return `<section class="catalog-folder-group" data-folder-group="${esc(key)}"><header><button type="button" class="catalog-folder-toggle" data-folder-toggle="${esc(key)}" aria-expanded="${!collapsed}" aria-controls="${esc(id)}" aria-label="${collapsed?'展开':'折叠'}文件夹 ${esc(name)}"><span class="catalog-folder-arrow" aria-hidden="true">${collapsed?'▸':'▾'}</span><span class="catalog-folder-heading"><strong>${esc(name)}</strong><small>${esc(path)}</small></span><span class="catalog-folder-count">本页 ${items.length} 项</span></button></header><div id="${esc(id)}" class="catalog-folder-grid" ${collapsed?'hidden':''}>${items.map(card).join('')}</div></section>`;
    }).join('');
  }
  function card(item) {
    const active = selection.item?.id === item.id;
    const thumbnail=item.offline?'<span class="sku-offline-preview">原照片离线 · 元数据已保留</span>':`<img data-preview-src="${esc(previewUrl(item,420))}" alt="${esc(item.name)}">${FramekeepAssetLoading.markup()}`;
    const format=item.has_jpg && item.has_raw?'J/R':item.media_type==='image'?'':item.media_format||item.extension?.slice(1).toUpperCase()||'';
    return `<button class="catalog-card ${active?'active':''}" data-catalog-item="${esc(item.id)}"><span class="catalog-thumb" data-preview-state="${item.offline?'offline':'loading'}">${thumbnail}${format?`<span class="photo-format-badge">${esc(format)}</span>`:''}${item.rating>0&&item.media_type==='image'?`<span class="photo-rating-badge" aria-label="${item.rating} 星">${'★'.repeat(item.rating)}</span>`:''}</span><span class="catalog-card-name">${esc(item.name)}</span></button>`;
  }
  async function items({ preserve = false } = {}) {
    const query = new URLSearchParams({ root: selection.root, folder: selection.folder, q: $('catalog-search').value, media: $('catalog-media').value, cursor: cursors[page], limit: 120 });
    const request=++catalogRequest;catalogController?.abort();catalogController=new AbortController();
    let data;
    try{data=await api('/api/catalog/items?'+query,undefined,20000,catalogController.signal)}
    catch(error){if(request!==catalogRequest)return;throw error}
    if(request!==catalogRequest)return;
    catalogLoadFailed=false;$('catalog-grid').removeAttribute('aria-busy');
    const displayRows=[...folderGroups(data.rows).values()].flat();catalogRows=new Map(displayRows.map(row=>[row.id,row]));
    if(selection.item&&catalogRows.has(selection.item.id)){selection.item={...selection.item,...catalogRows.get(selection.item.id)};paintRating(selection.item.rating||0)}
    hasMore = data.has_more;
    nextCursor = data.next_cursor;
    if (!preserve && selection.item && !data.rows.some(row => row.id === selection.item.id)) select(null);
    const snapshot=JSON.stringify([searching(),displayRows]),grid=$('catalog-grid');
    if(snapshot!==catalogSnapshot){const top=grid.scrollTop;grid.innerHTML=displayRows.length?groupedCards(displayRows):'<div class="catalog-empty">没有符合当前条件的照片</div>';if(!displayRows.length)grid.classList.remove('catalog-grouped');grid.scrollTop=top;catalogSnapshot=snapshot;FramekeepAssetLoading.watch(grid)}
    $('catalog-summary').textContent = `${data.total} 项素材 · ${data.engine||'目录数据库'}`;
    $('catalog-prev').disabled = page === 0;
    $('catalog-next').disabled = !hasMore;
  }
  function inspectorThumbnail(item){
    const previous=$('catalog-selected-preview'),url=previewUrl(item,420);
    if(previous.dataset.previewKey===url&&(!previous.complete||previous.naturalWidth))return;
    const thumbnail=[...$('catalog-grid').querySelectorAll('.catalog-thumb img')].find(image=>image.complete&&image.naturalWidth&&new URL(image.currentSrc||image.src,location.href).href===new URL(url,location.href).href);
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
    if (!item) return;
    paintRating(item.rating||0);
    inspectorThumbnail(item);
    $('catalog-selected-format').hidden = false;
    $('catalog-selected-format').textContent=item.has_jpg && item.has_raw?'J/R':item.media_format||item.extension?.slice(1).toUpperCase()||'';
    $('catalog-selected-name').textContent = item.relative;
    $('catalog-selected-sku').value = item.sku || '';
    const image=item.media_type==='image';
    $('catalog-rating-controls').hidden=!image;$('catalog-rating-status').hidden=!image;
    $('catalog-selected-source').textContent = item.source === 'manual' ? '人工确认的 SKU 指纹' : item.source === 'path_rule' ? `路径候选（未 OCR） · ${Math.round((item.confidence || 0) * 100)}%` : item.media_type==='video'?'专业视频素材 · 编码资料在打开详情时读取并缓存':item.media_type==='design'?'Adobe / 设计源文件 · 已纳入共享索引':'尚未识别，请人工填写';
  }
  let ratingBusy=false;
  function paintRating(rating){
    const badge=$('catalog-selected-rating');badge.hidden=rating<=0;badge.textContent='★'.repeat(rating);badge.setAttribute('aria-label',rating+' 星');
    for(const button of $('catalog-rating-controls').querySelectorAll('[data-catalog-rating]')){
      const value=Number(button.dataset.catalogRating);button.classList.toggle('on',value>0&&value<=rating);button.setAttribute('aria-pressed',String(value===rating));
    }
  }
  async function rate(rating){
    if(!selection.item||ratingBusy)return;
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
  function paintIndexProgress(){
    const running=state.roots.filter(root=>['queued','scanning'].includes(root.status));
    const panel=$('catalog-index-progress');
    panel.hidden=!running.length;
    if(!running.length){$('catalog-scan-status').textContent=state.roots.length?'索引已完成，所有子目录的预览会自动在后台生成。':'归档与分屏拍摄共用同一素材数据库；目录变化会自动索引。';return;}
    const percent=Math.floor(running.reduce((sum,root)=>sum+Math.max(0,Math.min(99,Number(root.progress?.percent)||0)),0)/running.length);
    const estimated=running.some(root=>root.progress?.estimated||!root.progress?.total);
    const done=running.reduce((sum,root)=>sum+(Number(root.progress?.done)||0),0);
    const detail=`正在索引 ${running.length} 个位置 · 已扫描 ${done} 个文件${estimated?' · 目录估算':''}`;
    $('catalog-index-status').textContent=detail;$('catalog-index-percent').textContent=(estimated?'约 ':'')+percent+'%';$('catalog-index-bar').value=percent;
    panel.title=running.map(root=>`${root.name||root.path}：${root.progress?.done||0} 个文件 · ${(root.progress?.estimated?'约 ':'')+(root.progress?.percent||0)}%`).join('\n');
    $('catalog-scan-status').textContent=detail;
  }
  function paintPreviewProgress(progress){
    const panel=$('catalog-preview-progress');
    panel.hidden=!progress||(!progress.total&&!progress.checking);
    if(panel.hidden)return;
    const {total,ready,pending,failed,thumbnails_ready:small,workers,checking}=progress;
    const percent=total?Math.floor((small+ready)/(total*2)*100):0;
    $('catalog-preview-status').textContent=checking?'正在检查全部目录的预览…':pending?`缩略图 ${small}/${total} · 清晰预览 ${ready}/${total} · ${workers} 路并行${failed?' · 失败 '+failed:''}`:failed?`已生成 ${ready}/${total} · ${failed} 项无法生成预览`:`全部 ${total} 项预览已就绪`;
    $('catalog-preview-percent').textContent=checking?'':percent+'%';
    $('catalog-preview-bar').value=percent;
    panel.title='添加归档后自动处理所有子目录；先完成缩略图，再生成清晰预览。';
  }
  function paintArchiveState(){
    if(!catalogRootsLoaded)return;
    $('catalog-count').textContent=`${searching()?state.roots.reduce((n,root)=>n+root.folders.reduce((sum,f)=>sum+f.count,0),0):state.counts.total||0} 项`;
    allSearchFolders.hidden=!searching();allSearchFolders.disabled=!selection.root&&!selection.folder;
    const kinds=[];if(state.counts.images)kinds.push(`${state.counts.images} 图片`);if(state.counts.videos)kinds.push(`${state.counts.videos} 视频`);if(state.counts.designs)kinds.push(`${state.counts.designs} 设计源文件`);
    $('catalog-location').textContent=state.roots.length?`${state.roots.length} 个归档位置 · ${state.counts.tagged||0} 项已建立 SKU 指纹${kinds.length?' · '+kinds.join(' / '):''}`:searching()||$('catalog-media').value?'当前筛选没有匹配的归档位置':'尚未添加归档位置';
    const metadata=state.metadata;if(metadata?.total)$('catalog-location').textContent+=` · 资料入库 ${metadata.ready}/${metadata.total}${metadata.pending?'（后台同步中）':''}${metadata.failed?' · '+metadata.failed+' 项读取失败':''}`;
    tree();paintIndexProgress();
  }
  async function refresh({ reset = false, contentsOnly = false } = {}) {
    const refreshId=++archiveRefresh,treeQuery=new URLSearchParams({q:$('catalog-search').value.trim(),media:$('catalog-media').value});
    archiveActive++;
    try{
    if (reset) { cursors = ['']; page = 0; nextCursor = ''; }
    if(contentsOnly){
      $('catalog-grid').setAttribute('aria-busy','true');$('catalog-summary').textContent='正在加载目录…';
      await items({preserve:true});return;
    }
    const results=await Promise.allSettled([
      api('/api/catalog/tree'+(searching()||$('catalog-media').value?'?'+treeQuery:'')).then(treeState=>{
        if(refreshId!==archiveRefresh)return;
        state={...state,...treeState,roots:treeState.roots.map(root=>({...root,...referenceProgress.get(root.id)}))};
        catalogRootsLoaded=true;paintArchiveState();
      }),
      items({preserve:true}).catch(error=>{if(refreshId===archiveRefresh){catalogLoadFailed=true;$('catalog-summary').textContent='素材列表读取失败，已保留归档目录；可再次点击目录重试'}throw error}),
      api('/api/catalog/state').then(totals=>{
        if(refreshId!==archiveRefresh)return;
        state.counts=totals.counts;state.metadata=totals.metadata;paintArchiveState();paintPreviewProgress(totals.previews);
      }),
      api('/api/reference/config').then(reference=>{
        if(refreshId!==archiveRefresh)return;
        referenceProgress=new Map((reference.roots||[]).map(root=>[root.id,root]));
        state.roots=state.roots.map(root=>({...root,...referenceProgress.get(root.id)}));paintIndexProgress();
      }).catch(()=>{})
    ]);
    if(refreshId!==archiveRefresh)return;
    if(results[0].status==='rejected'&&!catalogRootsLoaded){$('catalog-location').textContent='归档位置读取失败，请重试；已保存的目录未被移除';$('catalog-tree').textContent='目录暂时无法读取'}
    const failed=results.find(result=>result.status==='rejected');
    if(failed)throw failed.reason;
    }catch(error){
      if(refreshId!==archiveRefresh)return;
      if(contentsOnly){catalogLoadFailed=true;$('catalog-summary').textContent='目录读取失败，请再次点击重试'}
      throw error;
    }finally{if(refreshId===archiveRefresh)$('catalog-grid').removeAttribute('aria-busy');archiveActive--}
  }
  async function loadArchive() { try{await refresh({ reset: true })}catch(error){message('归档部分资料读取失败，已保留目录：'+error.message)} }
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
    if(drive){const key='catalog|'+drive.dataset.catalogDrive,open=drive.getAttribute('aria-expanded')!=='true';drive.setAttribute('aria-expanded',String(open));$(drive.getAttribute('aria-controls')).hidden=!open;if(open)collapsedDrives.delete(key);else collapsedDrives.add(key);return}
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
    host.addEventListener('click',event=>{const card=event.target.closest('.catalog-card[data-catalog-item]');if(card){quickSelected[mode]=card.dataset.catalogItem;if(mode==='sku-database')host.querySelectorAll('.catalog-card').forEach(row=>row.classList.toggle('active',row===card))}});
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
  locationDialog.innerHTML=`<form id="catalog-location-form"><div class="dialog-heading"><h2 id="catalog-location-title">添加归档文件夹</h2><button id="catalog-location-close" type="button" aria-label="关闭">×</button></div><label for="catalog-location-path">本地或网络文件夹路径</label><div class="catalog-location-path-row"><input id="catalog-location-path" required autocomplete="off" spellcheck="false" placeholder="D:\照片、X:\照片 或 \\192.168.0.181\共享\照片"><button id="catalog-location-browse" type="button" class="bordered">浏览</button></div><p class="dialog-copy">支持本地磁盘、已连接的网络驱动器和 IP／主机名共享路径。网络共享须先在 Windows 中具备访问权限。</p><p id="catalog-location-error" role="status"></p><div class="dialog-actions"><button id="catalog-location-cancel" type="button">取消</button><button id="catalog-location-submit" type="submit" class="primary">添加并索引</button></div></form>`;
  document.body.append(locationDialog);
  $('catalog-add-root').onclick=()=>{$('catalog-location-error').textContent='';$('catalog-location-path').value='';locationDialog.showModal();$('catalog-location-path').focus()};
  for(const id of ['catalog-location-close','catalog-location-cancel'])$(id).onclick=()=>locationDialog.close();
  $('catalog-location-browse').onclick=async()=>{try{const folder=await getFolder();if(folder)$('catalog-location-path').value=folder}catch(error){$('catalog-location-error').textContent=error.message}};
  $('catalog-location-form').onsubmit=async event=>{
    event.preventDefault();const button=$('catalog-location-submit');if(button.disabled)return;button.disabled=true;button.textContent='正在索引…';$('catalog-location-error').textContent='';
    try{const folder=$('catalog-location-path').value.trim(),result=await api('/api/catalog/add',{folder});selection.root=result.root_id;selection.folder='';select(null);await refresh({reset:true});locationDialog.close();message('已加入归档与分屏共用素材库，正在后台索引并生成所有子目录的预览。')}
    catch(error){$('catalog-location-error').textContent=error.message}
    finally{button.disabled=false;button.textContent='添加并索引'}
  };
  $('catalog-scan').onclick = async () => {
    try {
      const roots = state.roots;
      if (!roots.length) { message('请先添加归档文件夹'); return; }
      $('catalog-scan').disabled = true; $('catalog-scan').textContent = '正在排队…';
      for (const root of roots) await api('/api/catalog/scan', { root_id: root.id });
      message('已加入后台索引队列，素材可随已提交批次立即搜索。'); await refresh({ reset: true });
    } catch (error) { message(error.message); }
    finally { $('catalog-scan').disabled = false; $('catalog-scan').textContent = '刷新归档索引'; }
  };
  $('catalog-search').addEventListener('input', () => {
    if(searching()){
      if(!beforeSearch){rememberTree($('catalog-tree'));beforeSearch={root:selection.root,folder:selection.folder,folders:new Map(folderStates),drives:new Set(collapsedDrives)}}
      selection.root='';selection.folder='';
    }else if(beforeSearch){
      selection.root=beforeSearch.root;selection.folder=beforeSearch.folder;folderStates.clear();for(const [key,value] of beforeSearch.folders)folderStates.set(key,value);collapsedDrives.clear();for(const key of beforeSearch.drives)collapsedDrives.add(key);beforeSearch=null;skipRememberTree=true;
    }
    select(null);refresh({ reset: true }).catch(error => message(error.message));
  });
  $('catalog-media').addEventListener('change', () => refresh({ reset: true }).catch(error => message(error.message)));
  $('catalog-prev').onclick = () => { page = Math.max(0, page - 1); refresh({}).catch(error => message(error.message)); };
  $('catalog-next').onclick = () => { if (hasMore && nextCursor) { cursors[++page] = nextCursor; refresh({}).catch(error => message(error.message)); } };
  $('catalog-save-sku').onclick = async () => {
    if (!selection.item) return;
    try { const item = await api('/api/catalog/assign', { item_id: selection.item.id, sku: $('catalog-selected-sku').value }); select({ ...selection.item, ...item }); await refresh({}); message(`已确认 ${item.sku}${item.metadata_operation?'，文件与 XMP 写入已排队':' 的 SKU 元数据'}`); }
    catch (error) { message(error.message); }
  };

  async function renderDatabase() {
    const request=++dbRequest;
    let data;dbActive++;
    try{data=await api('/api/sku/library?'+new URLSearchParams({q:$('sku-database-search').value,sort:$('sku-database-sort').value,root:dbRoot,folder:dbFolder,sku:dbSku,bin_id:dbBin,offset:dbPage*120,limit:120}))}finally{dbActive--}
    if(request!==dbRequest)return;
    window.FramekeepSkuLibrary?.onData?.(data);
    const snapshot=JSON.stringify([dbMode,dbRoot,dbFolder,dbBin,dbSku,dbPage,$('sku-database-search').value.trim(),data]);if(snapshot===dbSnapshot)return;dbSnapshot=snapshot;dbRows=new Map(data.rows.map(row=>[row.id,row]));
    const host=$('sku-database-tree'),treeKey=JSON.stringify(data.roots);rememberTree(host);if(host.dataset.contentKey!==treeKey){host.innerHTML=treeMarkup(data.roots,'sku');host.dataset.contentKey=treeKey}
    const list=$('sku-database-list'),listKey=JSON.stringify([dbBin==='trash',data.groups]);
    if(list.dataset.contentKey!==listKey){list.innerHTML=data.groups.length?data.groups.map(row=>`<button type="button" data-db-sku="${esc(row.normalized)}" draggable="${dbBin!=='trash'}" title="双击修改 SKU 款号 · 最近入库 ${esc(skuAddedDate(row.created))}"><span class="sku-code-info"><strong>${esc(row.sku)}</strong>${skuDate(row.created)}</span><span>${row.count} 张</span></button>`).join(''):'<div class="catalog-empty">此处暂无 SKU</div>';list.dataset.contentKey=listKey}
    for(const button of skuPage.querySelectorAll('[data-db-sku]'))button.classList.toggle('active',button.dataset.dbSku===dbSku);
    $('sku-library-count').textContent=`${data.groups.length} 个 SKU · ${data.interval_count||0} 个区间`;
    const query=$('sku-database-search').value.trim(),group=data.groups.find(row=>row.normalized===dbSku),show=!!dbSku||dbMode==='tree'||!!query;
    $('sku-database-title').textContent=group?`${group.sku} · ${data.total} 张照片`:query?`搜索“${query}” · ${data.total} 张照片`:dbMode==='tree'?`${dbFolder||'所有已识别目录'} · ${data.total} 张照片`:'选择一个 SKU';
    $('sku-database-grid').innerHTML=show?(data.rows.map(row=>`<div class="ocr-library-card">${card(row)}<button class="bordered ocr-library-details" data-ocr-session="${esc(row.session_id)}" data-ocr-capture="${esc(row.capture_id)}">OCR 资料 / 选择 SKU</button></div>`).join('')||'<div class="catalog-empty">没有符合条件的 OCR 照片</div>'):'<div class="catalog-empty">选择一个 SKU 查看对应照片</div>';
    FramekeepAssetLoading.watch($('sku-database-grid'));
    dbMore=data.has_more;$('sku-database-prev').disabled=!show||dbPage===0;$('sku-database-next').disabled=!show||!dbMore;
  }
  const dbError=error=>{dbSnapshot='';$('sku-database-title').textContent='读取失败：'+error.message};
  window.FramekeepSkuLibrary={
    onData:null,
    get selection(){return {sku:dbSku,bin:dbBin}},
    async selectBin(bin){dbBin=bin;dbSku='';dbRoot='';dbFolder='';dbPage=0;dbSnapshot='';await renderDatabase()},
    async refresh(options={}){if(options.sku!==undefined)dbSku=options.sku;if(options.clearSearch)$('sku-database-search').value='';dbPage=0;dbSnapshot='';await renderDatabase()}
  };
  skuPage.addEventListener('click',event=>{
    const details=event.target.closest('[data-ocr-capture]');if(details){FramekeepOcrDetails.open(details.dataset.ocrSession,details.dataset.ocrCapture).catch(dbError);return}
    const tab=event.target.closest('[data-sku-view]');
    if(tab){dbMode=tab.dataset.skuView;dbPage=0;dbRoot='';dbFolder='';for(const button of skuPage.querySelectorAll('[data-sku-view]'))button.setAttribute('aria-selected',String(button===tab));$('sku-database-list').hidden=dbMode!=='flat';$('sku-database-tree').hidden=dbMode!=='tree';renderDatabase().catch(dbError);return}
    const drive=event.target.closest('[data-db-drive]');
    if(drive){const key='sku|'+drive.dataset.dbDrive,open=drive.getAttribute('aria-expanded')!=='true';drive.setAttribute('aria-expanded',String(open));$(drive.getAttribute('aria-controls')).hidden=!open;if(open)collapsedDrives.delete(key);else collapsedDrives.add(key);return}
    const item=event.target.closest('[data-db-sku]');
    if(item){dbSku=item.dataset.dbSku;dbRoot=item.dataset.dbRoot||'';dbFolder=item.dataset.dbFolder||'';dbPage=0;renderDatabase().catch(dbError);return}
    const folder=event.target.closest('[data-db-root]');
    if(folder){dbRoot=folder.dataset.dbRoot;dbFolder=folder.dataset.dbFolder;dbSku='';dbPage=0;renderDatabase().catch(dbError)}
  });
  $('sku-database-search').addEventListener('input',()=>{dbSku='';dbPage=0;renderDatabase().catch(dbError)});
  $('sku-database-search').placeholder='搜索 SKU、OCR 全文或辅助信息';
  $('sku-database-sort').addEventListener('change',()=>{dbPage=0;renderDatabase().catch(dbError)});
  $('sku-database-prev').onclick=()=>{dbPage=Math.max(0,dbPage-1);renderDatabase().catch(dbError)};
  $('sku-database-next').onclick=()=>{if(dbMore){dbPage++;renderDatabase().catch(dbError)}};
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
  const databaseTimer=setInterval(()=>{
    if(document.hidden||document.getElementById('asset-image-viewer')?.open)return;
    if(!dbActive&&FramekeepWorkspace.current()==='sku-database')renderDatabase().catch(dbError);
    if(!archiveActive&&FramekeepWorkspace.current()==='archive'&&!locationDialog.open&&!document.activeElement?.closest('#catalog-inspector-form input')&&!ratingBusy)refresh({}).catch(error=>message(error.message));
  },2000);
  window.addEventListener('pagehide',()=>clearInterval(databaseTimer),{once:true});
  FramekeepWorkspace.register('archive', archiveTab, archive, loadArchive);
  document.addEventListener('framekeep:metadata-imported',()=>{if(FramekeepWorkspace.current()==='archive')refresh({}).catch(error=>message(error.message))});
  FramekeepWorkspace.register('sku-database', skuTab, skuPage, renderDatabase);
})();
