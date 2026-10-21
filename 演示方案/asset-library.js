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
  const archive = $('asset-archive-dialog');
  const archiveTab = $('asset-archive-open');
  if (!archive || !archiveTab) return;

  archive.innerHTML = `
    <div class="catalog-heading"><div><h2>资产归档</h2><p id="catalog-location">尚未添加归档位置</p></div><div class="catalog-heading-actions"><button id="catalog-add-root" class="bordered">添加归档文件夹</button><button id="catalog-scan" class="primary">刷新归档索引</button></div></div>
    <div class="catalog-workspace">
      <aside class="catalog-tree-panel"><header><strong>目录</strong><span id="catalog-count"></span></header><div id="catalog-tree" class="catalog-tree" role="tree"></div></aside>
      <main class="catalog-main"><header class="catalog-toolbar"><label><span>搜索 SKU</span><input id="catalog-search" type="search" placeholder="输入款号快速查找"></label><span id="catalog-summary" role="status"></span></header><div id="catalog-grid" class="catalog-grid" aria-live="polite"></div><footer class="catalog-pagination"><button id="catalog-prev">上一页</button><button id="catalog-next">下一页</button></footer></main>
      <aside class="catalog-inspector"><header><strong>照片评级与 SKU</strong><span id="catalog-inspector-status"></span></header><div id="catalog-inspector-empty" class="catalog-empty">选择照片以检查与修正 SKU</div><div id="catalog-inspector-form" hidden><div class="catalog-preview-wrap"><img id="catalog-selected-preview" alt="当前归档照片预览"><span id="catalog-selected-rating" class="photo-rating-badge" hidden></span><span id="catalog-selected-format" class="photo-format-badge" hidden>J/R</span></div><p id="catalog-selected-name"></p><div id="catalog-rating-controls" class="catalog-rating-controls" role="group" aria-label="归档照片星级"><button type="button" data-catalog-rating="1" aria-label="归档照片标 1 星">★</button><button type="button" data-catalog-rating="2" aria-label="归档照片标 2 星">★</button><button type="button" data-catalog-rating="3" aria-label="归档照片标 3 星">★</button><button type="button" data-catalog-rating="4" aria-label="归档照片标 4 星">★</button><button type="button" data-catalog-rating="5" aria-label="归档照片标 5 星">★</button><button type="button" data-catalog-rating="0">清星</button></div><p id="catalog-rating-status" role="status"></p><label>SKU 款号<input id="catalog-selected-sku" maxlength="80" placeholder="待确认"></label><p id="catalog-selected-source"></p><button id="catalog-save-sku" class="primary">确认 SKU 元数据</button></div><section class="catalog-scan-panel"><header><strong>刷新归档索引</strong></header><p id="catalog-scan-status">自动监控归档目录变化；匹配现场记录后同步 SKU 元数据。</p></section></aside>
    </div>`;

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

  let state = { roots: [], counts: {} }, selection = { root: '', folder: '', item: null }, cursors = [''], page = 0, nextCursor = '', hasMore = false, dbSku = '';
  let dbMode='flat',dbRoot='',dbFolder='',dbPage=0,dbRequest=0,dbActive=0,dbMore=false,dbSnapshot='',catalogRequest=0,catalogRows=new Map(),catalogSnapshot='',archiveActive=0;
  const message = text => { $('catalog-scan-status').textContent = text; };
  const getFolder = async () => {
    if (window.FramekeepDesktop?.chooseFolders) {
      const values = await FramekeepDesktop.chooseFolders();
      return Array.isArray(values) ? values[0] : values;
    }
    return (await api('/api/choose-folder')).folder;
  };
  const driveOf = value => {
    let path=String(value||'').replace(/\//g,'\\');
    if(/^\\\\\?\\UNC\\/i.test(path))path='\\\\'+path.slice(8);
    else if(path.startsWith('\\\\?\\'))path=path.slice(4);
    const drive=path.match(/^([A-Za-z]:)(?:\\|$)/);
    if(drive)return drive[1].toUpperCase();
    const network=path.match(/^\\\\([^\\]+)\\([^\\]+)/);
    return network?'\\\\'+network[1]+'\\'+network[2]:'其他位置';
  };
  const collapsedDrives=new Set(),folderStates=new Map();
  const nodeOpen=key=>folderStates.get(key)!==false?'open':'';
  const folderTree = folders => {
    const root = { children: new Map(), count: 0 };
    for (const row of folders) {
      const parts = row.folder === '/' ? [] : row.folder.replace(/\\/g,'/').split('/').filter(Boolean);
      let node = root; node.count += row.count;
      for (const part of parts) { if (!node.children.has(part)) node.children.set(part, { children: new Map(), count: 0 }); node = node.children.get(part); node.count += row.count; }
      node.skus=row.skus||[];
    }
    return root;
  };
  const skuButtons=(rows,root,folder)=> (rows||[]).map(row=>`<button class="sku-tree-code ${dbSku===row.normalized&&dbRoot===root&&dbFolder===folder?'active':''}" data-db-sku="${esc(row.normalized)}" data-db-root="${esc(root)}" data-db-folder="${esc(folder)}"><span>${esc(row.sku)}</span><small>${row.count}</small></button>`).join('');
  const branch = (node, rootId, prefix = '', scope='catalog') => [...node.children.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([name, child]) => {
    const folder = prefix ? prefix + '/' + name : name;
    const active = scope==='catalog'?selection.root === rootId && selection.folder === folder:dbRoot===rootId&&dbFolder===folder&&!dbSku;
    const key=scope+'|'+rootId+'|'+folder,attr=scope==='catalog'?'':'db-';
    return `<details ${nodeOpen(key)} data-catalog-node="${esc(key)}"><summary role="treeitem" data-${attr}root="${esc(rootId)}" data-${attr}folder="${esc(folder)}" class="${active?'active':''}"><span>${esc(name)}</span><small>${child.count}</small></summary>${scope==='sku'?skuButtons(child.skus,rootId,folder):''}${branch(child, rootId, folder,scope)}</details>`;
  }).join('');
  function rememberTree(host){for(const node of host.querySelectorAll('[data-catalog-node]'))folderStates.set(node.dataset.catalogNode,node.open)}
  function treeMarkup(roots,scope='catalog'){
    if(!roots.length)return '<div class="catalog-empty">'+(scope==='sku'?'尚无 OCR 识别的 SKU':'尚未添加归档文件夹')+'</div>';
    const drives=new Map(),attr=scope==='catalog'?'':'db-';
    for(const root of roots){const drive=driveOf(root.path);if(!drives.has(drive))drives.set(drive,[]);drives.get(drive).push(root)}
    return [...drives.entries()].sort(([a],[b])=>a.localeCompare(b)).map(([drive,roots],index)=>{
      const key=scope+'|'+drive,closed=collapsedDrives.has(key),id=scope+'-drive-folders-'+index;
      return `<section class="catalog-drive-group"><button type="button" class="catalog-drive-button" data-${scope==='catalog'?'catalog':'db'}-drive="${esc(drive)}" aria-expanded="${!closed}" aria-controls="${id}" title="${esc(drive)}"><svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="4" width="18" height="15" rx="2"/><path d="M3 14h18M6 17h2M10 17h2"/></svg><span>${esc(drive)}</span><small>${roots.reduce((sum,r)=>sum+r.folders.reduce((n,f)=>n+f.count,0),0)}</small></button><div id="${id}" class="catalog-drive-folders" ${closed?'hidden':''}>${roots.map(root=>{
        const rootName=root.path.split(/[\\/]/).filter(Boolean).at(-1)||root.path,node=folderTree(root.folders),active=scope==='catalog'?selection.root===root.id&&!selection.folder:dbRoot===root.id&&!dbFolder&&!dbSku;
        return `<details ${nodeOpen(scope+'|'+root.id+'|')} data-catalog-node="${esc(scope+'|'+root.id+'|')}" class="catalog-root"><summary role="treeitem" data-${attr}root="${esc(root.id)}" data-${attr}folder="" class="${active?'active':''}" title="${esc(root.path)}"><span>${esc(rootName)}</span></summary>${scope==='sku'?skuButtons(node.skus,root.id,''):''}${branch(node,root.id,'',scope)}</details>`;
      }).join('')}</div></section>`;
    }).join('');
  }
  function tree(){const host=$('catalog-tree');rememberTree(host);host.innerHTML=treeMarkup(state.roots)}
  function card(item) {
    const active = selection.item?.id === item.id;
    return `<button class="catalog-card ${active?'active':''}" data-catalog-item="${esc(item.id)}"><span class="catalog-thumb"><img src="${esc(previewUrl(item, 420))}" loading="lazy" alt="${esc(item.name)}">${item.has_jpg && item.has_raw ? '<span class="photo-format-badge">J/R</span>' : ''}${item.rating>0?`<span class="photo-rating-badge" aria-label="${item.rating} 星">${'★'.repeat(item.rating)}</span>`:''}</span><span class="catalog-card-name">${esc(item.name)}</span><span class="catalog-card-sku">${esc(item.source==='path_rule'&&item.sku?'路径候选: '+item.sku:item.sku || (item.source === 'review' ? '待复核' : '未识别'))}</span>${item.provenance_label?`<span class="sku-provenance" data-provenance="${esc(item.provenance)}">${esc(item.provenance_label)}</span>`:''}</button>`;
  }
  async function items({ preserve = false } = {}) {
    const query = new URLSearchParams({ root: selection.root, folder: selection.folder, sku: $('catalog-search').value, cursor: cursors[page], limit: 120 });
    const request=++catalogRequest,data = await api('/api/catalog/items?' + query);
    if(request!==catalogRequest)return;
    catalogRows=new Map(data.rows.map(row=>[row.id,row]));
    if(selection.item&&catalogRows.has(selection.item.id)){selection.item={...selection.item,...catalogRows.get(selection.item.id)};paintRating(selection.item.rating||0)}
    hasMore = data.has_more;
    nextCursor = data.next_cursor;
    if (!preserve && selection.item && !data.rows.some(row => row.id === selection.item.id)) select(null);
    const snapshot=JSON.stringify(data.rows),grid=$('catalog-grid');
    if(snapshot!==catalogSnapshot){const top=grid.scrollTop;grid.innerHTML=data.rows.length?data.rows.map(card).join(''):'<div class="catalog-empty">没有符合当前条件的照片</div>';grid.scrollTop=top;catalogSnapshot=snapshot}
    $('catalog-summary').textContent = `${data.total} 张照片`;
    $('catalog-prev').disabled = page === 0;
    $('catalog-next').disabled = !hasMore;
  }
  function select(item) {
    if(selection.item?.id!==item?.id)$('catalog-rating-status').textContent='';
    selection.item = item;
    $('catalog-inspector-form').hidden = !item;
    $('catalog-inspector-empty').hidden = !!item;
    $('catalog-inspector-status').textContent = item?.source === 'manual' ? '已确认' : item?.source === 'path_rule' ? '路径候选' : item?.source === 'review' ? '待复核' : '';
    if (!item) return;
    paintRating(item.rating||0);
    $('catalog-selected-preview').src = previewUrl(item, 900);
    $('catalog-selected-format').hidden = !(item.has_jpg && item.has_raw);
    $('catalog-selected-name').textContent = item.relative;
    $('catalog-selected-sku').value = item.sku || '';
    $('catalog-selected-source').textContent = item.source === 'manual' ? '人工确认的 SKU 指纹' : item.source === 'path_rule' ? `路径候选（未 OCR） · ${Math.round((item.confidence || 0) * 100)}%` : '尚未识别，请人工填写';
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
  async function refresh({ reset = false } = {}) {
    archiveActive++;
    try{
    if (reset) { cursors = ['']; page = 0; nextCursor = ''; }
    [state,] = await Promise.all([api('/api/catalog/tree'), items({ preserve: true })]);
    const totals = await api('/api/catalog/state');
    state.counts = totals.counts;
    $('catalog-count').textContent = `${state.counts.total || 0} 张`;
    $('catalog-location').textContent = state.roots.length ? `${state.roots.length} 个归档位置 · ${state.counts.tagged || 0} 张已建立 SKU 指纹` : '尚未添加归档位置';
    tree();
    }finally{archiveActive--}
  }
  async function loadArchive() { await refresh({ reset: true }); }
  archive.addEventListener('click', async event => {
    const drive=event.target.closest('[data-catalog-drive]');
    if(drive){const key='catalog|'+drive.dataset.catalogDrive,open=drive.getAttribute('aria-expanded')!=='true';drive.setAttribute('aria-expanded',String(open));$(drive.getAttribute('aria-controls')).hidden=!open;if(open)collapsedDrives.delete(key);else collapsedDrives.add(key);return}
    const folder = event.target.closest('[data-root]');
    if (folder) { selection.root = folder.dataset.root; selection.folder = folder.dataset.folder; select(null); await refresh({ reset: true }); return; }
    const image = event.target.closest('[data-catalog-item]');
    if (image) { select(catalogRows.get(image.dataset.catalogItem)||null); $('catalog-grid').querySelectorAll('.catalog-card').forEach(node => node.classList.toggle('active', node.dataset.catalogItem === image.dataset.catalogItem)); }
  });
  const locationDialog=document.createElement('dialog');locationDialog.id='catalog-location-dialog';locationDialog.setAttribute('aria-labelledby','catalog-location-title');
  locationDialog.innerHTML=`<form id="catalog-location-form"><div class="dialog-heading"><h2 id="catalog-location-title">添加归档文件夹</h2><button id="catalog-location-close" type="button" aria-label="关闭">×</button></div><label for="catalog-location-path">本地或网络文件夹路径</label><div class="catalog-location-path-row"><input id="catalog-location-path" required autocomplete="off" spellcheck="false" placeholder="D:\照片、X:\照片 或 \\192.168.0.181\共享\照片"><button id="catalog-location-browse" type="button" class="bordered">浏览</button></div><p class="dialog-copy">支持本地磁盘、已连接的网络驱动器和 IP／主机名共享路径。网络共享须先在 Windows 中具备访问权限。</p><p id="catalog-location-error" role="status"></p><div class="dialog-actions"><button id="catalog-location-cancel" type="button">取消</button><button id="catalog-location-submit" type="submit" class="primary">添加并索引</button></div></form>`;
  document.body.append(locationDialog);
  $('catalog-add-root').onclick=()=>{$('catalog-location-error').textContent='';$('catalog-location-path').value='';locationDialog.showModal();$('catalog-location-path').focus()};
  for(const id of ['catalog-location-close','catalog-location-cancel'])$(id).onclick=()=>locationDialog.close();
  $('catalog-location-browse').onclick=async()=>{try{const folder=await getFolder();if(folder)$('catalog-location-path').value=folder}catch(error){$('catalog-location-error').textContent=error.message}};
  $('catalog-location-form').onsubmit=async event=>{
    event.preventDefault();const button=$('catalog-location-submit');if(button.disabled)return;button.disabled=true;button.textContent='正在索引…';$('catalog-location-error').textContent='';
    try{const folder=$('catalog-location-path').value.trim(),result=await api('/api/catalog/add',{folder});selection.root=result.root_id;selection.folder='';select(null);await refresh({reset:true});locationDialog.close();message(`已索引 ${result.indexed} 张照片，识别 ${result.detected} 个 SKU 候选`)}
    catch(error){$('catalog-location-error').textContent=error.message}
    finally{button.disabled=false;button.textContent='添加并索引'}
  };
  $('catalog-scan').onclick = async () => {
    try {
      const roots = state.roots;
      if (!roots.length) { message('请先添加归档文件夹'); return; }
      $('catalog-scan').disabled = true; $('catalog-scan').textContent = '解析中…';
      let indexed = 0, detected = 0, review = 0;
      for (const root of roots) { const result = await api('/api/catalog/scan', { root_id: root.id }); indexed += result.indexed; detected += result.detected; review += result.review; }
      message(`已扫描 ${indexed} 张 · 识别 ${detected} 个候选 · ${review} 项待复核`); await refresh({ reset: true });
    } catch (error) { message(error.message); }
    finally { $('catalog-scan').disabled = false; $('catalog-scan').textContent = '刷新归档索引'; }
  };
  $('catalog-search').addEventListener('input', () => refresh({ reset: true }).catch(error => message(error.message)));
  $('catalog-prev').onclick = () => { page = Math.max(0, page - 1); refresh({}).catch(error => message(error.message)); };
  $('catalog-next').onclick = () => { if (hasMore && nextCursor) { cursors[++page] = nextCursor; refresh({}).catch(error => message(error.message)); } };
  $('catalog-save-sku').onclick = async () => {
    if (!selection.item) return;
    try { const item = await api('/api/catalog/assign', { item_id: selection.item.id, sku: $('catalog-selected-sku').value }); select({ ...selection.item, ...item }); message(`已确认 ${item.sku} 的 SKU 元数据`); await refresh({}); }
    catch (error) { message(error.message); }
  };

  async function renderDatabase() {
    const request=++dbRequest;
    let data;dbActive++;
    try{data=await api('/api/sku/library?'+new URLSearchParams({q:$('sku-database-search').value,sort:$('sku-database-sort').value,root:dbRoot,folder:dbFolder,sku:dbSku,offset:dbPage*120,limit:120}))}finally{dbActive--}
    if(request!==dbRequest)return;
    const snapshot=JSON.stringify([dbMode,dbRoot,dbFolder,dbSku,dbPage,data]);if(snapshot===dbSnapshot)return;dbSnapshot=snapshot;
    const host=$('sku-database-tree');rememberTree(host);host.innerHTML=treeMarkup(data.roots,'sku');
    $('sku-database-list').innerHTML=data.groups.length?data.groups.map(row=>`<button data-db-sku="${esc(row.normalized)}" class="${dbSku===row.normalized?'active':''}"><strong>${esc(row.sku)}</strong><span>${row.count} 张 · OCR 已识别</span></button>`).join(''):'<div class="catalog-empty">尚无 OCR 识别的 SKU</div>';
    $('sku-library-count').textContent=`${data.groups.length} 个已识别 SKU · ${data.interval_count||0} 个区间`;
    const group=data.groups.find(row=>row.normalized===dbSku),show=!!dbSku||dbMode==='tree';
    $('sku-database-title').textContent=group?`${group.sku} · ${data.total} 张照片`:dbMode==='tree'?`${dbFolder||'所有已识别目录'} · ${data.total} 张照片`:'选择一个 SKU';
    $('sku-database-grid').innerHTML=show?(data.rows.map(card).join('')||'<div class="catalog-empty">没有符合条件的 OCR 照片</div>'):'<div class="catalog-empty">选择一个 SKU 查看对应照片</div>';
    dbMore=data.has_more;$('sku-database-prev').disabled=!show||dbPage===0;$('sku-database-next').disabled=!show||!dbMore;
  }
  const dbError=error=>{dbSnapshot='';$('sku-database-title').textContent='读取失败：'+error.message};
  skuPage.addEventListener('click',event=>{
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
    if(document.hidden)return;
    if(!dbActive&&FramekeepWorkspace.current()==='sku-database')renderDatabase().catch(dbError);
    if(!archiveActive&&FramekeepWorkspace.current()==='archive'&&!locationDialog.open&&!document.activeElement?.closest('#catalog-inspector-form input')&&!ratingBusy)refresh({}).catch(error=>message(error.message));
  },2000);
  window.addEventListener('pagehide',()=>clearInterval(databaseTimer),{once:true});
  FramekeepWorkspace.register('archive', archiveTab, archive, loadArchive);
  FramekeepWorkspace.register('sku-database', skuTab, skuPage, renderDatabase);
})();
