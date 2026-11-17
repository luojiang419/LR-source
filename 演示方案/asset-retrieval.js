(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const native = !!$('quick-native-shell'), invoke = window.__TAURI__?.core?.invoke;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let token = null, state = null, roots = [], booting = null;
  async function api(path, body, signal, fresh=false) {
    if(body===undefined&&!fresh&&window.FramekeepViewCache?.accepts(path))return FramekeepViewCache.read(path,nextSignal=>api(path,undefined,nextSignal,true),signal);
    if (body !== undefined && !token) await bootstrap();
    const binary=body instanceof Blob;
    const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST',
      headers: body === undefined ? {'X-Framekeep-View-Session':window.FRAMEKEEP_SESSION?.id||''} : {'Content-Type':binary?'application/octet-stream':'application/json','X-Framekeep-Token':token,...(binary?{'X-Reference-Name':encodeURIComponent(body.name||'clipboard.jpg')}:{})},
      ...(body === undefined ? {} : {body:binary?body:JSON.stringify(body)}),
      signal:binary?signal:signal?AbortSignal.any([signal,AbortSignal.timeout(200000)]):AbortSignal.timeout(200000)});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || `请求失败 ${response.status}`);
    if(body!==undefined&&window.FramekeepViewCache?.mutates(path))FramekeepViewCache.invalidate(path);
    return result;
  }
  let fullBooting=null;
  async function bootstrap(full=false) {
    if(full){
      await bootstrap();
      if(fullBooting)return fullBooting;
      fullBooting=api('/api/intelligence/state').then(result=>{state={...state,...result};token=state.request_token;return state}).finally(()=>{fullBooting=null});
      return fullBooting;
    }
    if(booting)return booting;
    booting=api('/api/intelligence/search-state').then(result=>{
      state={...state,...result};token=state.request_token;roots=result.roots;
      window.FramekeepQuickAppearance?.apply(state.config,true);return state;
    }).finally(()=>{booting=null});
    return booting;
  }
  const labels = {queued:'等待',running:'解析中',completed:'已完成',failed:'失败',stale:'已失效',cancelled:'已取消',ready:'可归组',review:'待复核',confirmed:'已确认'};
  function shell(host, {compact=false,close=null}={}) {
    const clientId='retrieval-'+(crypto.randomUUID?.()||Date.now().toString(36)+Math.random().toString(36).slice(2));
    host.classList.add(compact?'quick-search-shell':'retrieval-workspace','retrieval-layout');
    const scopeFilters=`<label class="retrieval-scope"><span>归档位置</span><select class="retrieval-root" aria-label="归档位置"><option value="">全部归档</option></select></label><label class="retrieval-scope"><span>素材类型</span><select class="retrieval-media" aria-label="素材类型"><option value="">全部素材</option><option value="image">图片 / RAW</option><option value="video">视频帧</option><option value="design">设计文件</option></select></label><label class="retrieval-scope"><span>搜索方式</span><select class="retrieval-mode" aria-label="搜索方式"><option value="index">智能搜索 / SKU</option><option value="semantic">CLIP 语义搜索</option></select></label>`;
    host.innerHTML = `<header class="retrieval-heading catalog-heading ${compact?'':'retrieval-desktop-heading'}"><div class="retrieval-heading-title"><h2>${compact?'快捷资产搜索':'资产检索'}</h2><p>SKU · 画面描述 · 多维筛选 · 以图搜图</p></div>${compact?'':'<nav class="retrieval-navigation" aria-label="工作台导航"><button data-workspace-target="sku-open">拍摄进度</button><button aria-current="page" data-workspace-target="asset-retrieval-open">资产检索</button><button data-workspace-target="asset-archive-open">资产归档</button></nav><details class="retrieval-actions-menu"><summary aria-label="工作台设置" title="工作台设置"><svg viewBox="0 0 24 24" width="19" height="19" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5"><path d="m9.5 3-.6 2.3-2 .9-2.2-.6-2.5 4.3 1.6 1.7v2.3l-1.6 1.7 2.5 4.3 2.2-.6 2 .9.6 2.3h5l.6-2.3 2-.9 2.2.6 2.5-4.3-1.6-1.7v-2.3l1.6-1.7-2.5-4.3-2.2.6-2-.9L14.5 3z"/><circle cx="12" cy="12.8" r="3.3"/></svg></summary>'}<div class="retrieval-tools catalog-heading-actions"><button data-action="quick" ${compact?'hidden':''}>快捷搜索 <kbd>Ctrl ⇧ Space</kbd></button><button data-action="index">多维解析</button><button data-action="settings">检索设置</button>${compact?'<button data-action="image">以图搜图</button>':'<button data-action="image">以图搜图</button><button data-workspace-target="selection-tab">现场拍摄</button><button data-workspace-target="sku-database-open">SKU 数据库</button><button data-workspace-target="app-settings-open">软件设置</button><button data-action="theme">切换主题</button>'}${close?'<button data-action="close" aria-label="关闭快捷搜索">Esc ×</button>':''}</div>${compact?'':'</details>'}</header>
      <div class="retrieval-searchbar"><input class="retrieval-query" type="search" aria-label="搜索 SKU 或画面关键词" placeholder="输入 SKU、昨天的图片，或描述画面…" autocomplete="off" maxlength="300"><button data-action="search" class="primary">搜索</button>${scopeFilters}</div>
      ${compact?'<header class="quick-query-heading" hidden><p class="quick-query-eyebrow">搜索结果</p><h1 class="quick-query-text"></h1><p class="quick-query-summary" role="status" aria-live="polite"></p></header>':''}
      <div class="retrieval-reference-note"></div><div class="retrieval-intent" role="status" hidden></div>
      <div class="retrieval-body"><aside class="retrieval-filters"><section class="retrieval-reference"></section><div class="retrieval-filter-scroll"><h3>多维筛选</h3><label>筛选维度<select class="retrieval-facet-key"><option value="">常用维度</option></select></label><div class="retrieval-filter-chips"></div><button data-action="clear-facets" hidden>清除筛选</button><details class="retrieval-index-info"><summary>索引状态与快捷键</summary><p class="retrieval-coverage">正在连接…</p><p>↑↓ 选择 · Enter 预览<br>Ctrl+V 粘贴参考图<br>也可将图片拖入窗口</p></details></div></aside><main class="retrieval-results-area"><div class="retrieval-result-status" role="status" aria-live="polite">正在连接资产数据库…</div><div class="retrieval-grid catalog-grid" role="listbox" aria-label="资产搜索结果"></div><button class="retrieval-more" hidden>加载更多</button></main><aside class="retrieval-detail" aria-label="资产详情" hidden><div class="retrieval-empty">选择一项资产<br>查看 SKU、多维资料与来源路径</div></aside></div>${compact?'<footer class="quick-search-footer"><span><kbd>↑ ↓</kbd>选择</span><span><kbd>Enter</kbd>查看</span><span><kbd>Esc</kbd>关闭</span><span>原片保持只读</span></footer>':''}`;
    const find = selector => host.querySelector(selector), action = name => find(`[data-action="${name}"]`);
    const input = find('.retrieval-query'), grid = find('.retrieval-grid'), status = find('.retrieval-result-status'), detail = find('.retrieval-detail');
    const detailPreference='framekeep.retrieval.details';
    let detailExpanded=true,detailContent=detail,detailToggle=null;
    if(!compact){
      try{detailExpanded=localStorage.getItem(detailPreference)!=='collapsed'}catch{}
      detail.id=clientId+'-detail';
      detail.innerHTML='<div class="retrieval-detail-heading"><strong>资产详情</strong><button type="button" data-detail="close" class="retrieval-detail-close" aria-label="收起资产详情">收起 ›</button></div><div class="retrieval-detail-content"><div class="retrieval-empty">选择一项资产<br>查看 SKU、多维资料与来源路径</div></div>';
      detailContent=find('.retrieval-detail-content');
      detailToggle=document.createElement('button');detailToggle.type='button';detailToggle.className='retrieval-detail-toggle';
      detailToggle.setAttribute('aria-label','展开资产详情');detailToggle.setAttribute('aria-controls',detail.id);
      detailToggle.innerHTML='<span aria-hidden="true">‹</span><span>资产详情</span>';detail.before(detailToggle);
      detailToggle.onclick=()=>{setDetailExpanded(true);if(selected>=0)select(selected,{scroll:false})};
      detail.querySelector('[data-detail="close"]').onclick=event=>{event.stopPropagation();closeDetail()};
      setDetailExpanded(detailExpanded,false);
    }
    if(compact){input.placeholder='搜索文件名、SKU，或输入自然语言条件';input.setAttribute('aria-label','搜索文件名、SKU 或自然语言条件')}
    grid.id=clientId+'-results';input.setAttribute('aria-controls',grid.id);
    const menu=find('.retrieval-actions-menu');
    if(menu){
      for(const button of host.querySelectorAll('[data-workspace-target]'))button.onclick=()=>{$(button.dataset.workspaceTarget)?.click();menu.open=false};
      action('theme').onclick=()=>$('theme-open')?.click();
      menu.addEventListener('click',event=>{if(event.target.closest('button'))menu.open=false});
      menu.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();menu.open=false;menu.querySelector('summary').focus()}});
      document.addEventListener('pointerdown',event=>{if(!menu.contains(event.target))menu.open=false});
    }
    function setDetailExpanded(expanded,persist=true){
      detailExpanded=expanded;detail.hidden=!expanded;detailToggle.hidden=expanded;
      detailToggle.setAttribute('aria-expanded',String(expanded));
      detail.parentElement.classList.toggle('detail-expanded',expanded);
      if(persist)try{localStorage.setItem(detailPreference,expanded?'expanded':'collapsed')}catch{}
    }
    function resetDetail(message){if(compact)detail.hidden=true;detailContent.innerHTML='<div class="retrieval-empty">'+message+'</div>'}
    function closeDetail(){if(detail.hidden||(compact&&document.documentElement.dataset.quickMode==='minimal'))return false;if(compact)detail.hidden=true;else setDetailExpanded(false);(grid.querySelector(`[data-result="${selected}"]`)||detailToggle)?.focus();return true}
    let imageData=null,loading=false,hasMore=false,appendFailed=false;
    const resultsPane=find('.retrieval-results-area'),more=find('.retrieval-more');
    const loadStatus=document.createElement('div');loadStatus.className='retrieval-load-status';loadStatus.setAttribute('role','status');loadStatus.hidden=true;more.before(loadStatus);
    let rows = [], selected = -1, cursor = '', image = '', imageItem = '', facets = {}, sequence = 0, controller=null, debounce=null, detailSequence=0, facetSequence=0;
    const minimalGrid=()=>compact&&document.documentElement.dataset.quickMode==='minimal';
    function syncQueryHeading(){
      if(!compact)return;
      const query=input.value.trim(),hasQuery=!!(query||image||imageItem),heading=find('.quick-query-heading');
      host.classList.toggle('quick-has-query',hasQuery);heading.hidden=!minimalGrid()||!hasQuery;
      status.setAttribute('aria-live',minimalGrid()?'off':'polite');
      const word=find('.quick-query-text');word.textContent=query||'以图搜图';word.title=word.textContent;
    }
    function pageSize(){
      if(!minimalGrid())return 60;
      const style=getComputedStyle(grid),tracks=style.gridTemplateColumns.split(' ').filter(Boolean),columns=Math.max(1,tracks.length);
      const cardWidth=parseFloat(tracks[0])||300,gap=parseFloat(style.rowGap)||32;
      const height=find('.retrieval-results-area').clientHeight||innerHeight;
      return Math.min(100,Math.max(20,columns*Math.ceil(height/(cardWidth*9/16+88+gap))));
    }
    if(compact)window.addEventListener('framekeep:quick-appearance',syncQueryHeading);
    const active = () => native || (host.matches('dialog') ? host.open : !host.classList.contains('hidden'));
    const message = (text,error=false,summary=text) => {status.textContent=text;status.classList.toggle('error',error);if(compact){find('.quick-query-summary').textContent=summary;find('.quick-query-heading').classList.toggle('error',error)}};
    const cancelResultPreviews=()=>{for(const img of host.querySelectorAll('.retrieval-card img,.retrieval-detail img'))img.removeAttribute('src')};
    function thumbnailUrl(row,size=420){const url=window.FramekeepDesignPreviews?.url(row)||new URL(row.preview,location.href);url.searchParams.set('size',size);return url.pathname+url.search}
    function renderResults(start=0) {
      if(!start)cancelResultPreviews();
      const scanner=`<svg class="retrieval-preview-scanner" viewBox="0 0 160 100" preserveAspectRatio="none" aria-hidden="true"><rect class="retrieval-scanner-wash" width="160" height="100"/><g class="retrieval-scanner-grid"><path d="M0 25H160M0 50H160M0 75H160M40 0V100M80 0V100M120 0V100"/></g><g class="retrieval-scanner-corners"><path d="M18 34V20H38M122 20H142V34M142 66V80H122M38 80H18V66"/></g><rect class="retrieval-scanner-frame" x="43" y="24" width="74" height="52" rx="3"/><g class="retrieval-scanner-shutter"><path d="M80 35 93 43 88 58 72 58 67 43Z"/><path d="M80 35v30M67 43l26 15M93 43 67 58"/></g><path class="retrieval-scanner-beam" d="M30 50H130"/><g class="retrieval-scanner-ticks"><path d="M56 87h8M68 87h8M80 87h8M92 87h8"/></g></svg>`;
      const markup = rows.length ? rows.slice(start).map((row,offset) => {const i=start+offset,cover=row.preview?`<span class="retrieval-preview" data-preview-state="loading">${scanner}<span class="retrieval-preview-error">预览暂不可用</span><img loading="eager" fetchpriority="high" decoding="async" src="${esc(thumbnailUrl(row))}" alt="${esc(row.name)}"></span>`:`<span class="retrieval-card-placeholder" aria-hidden="true">${esc((row.extension||'文件').replace('.','').toUpperCase())}</span>`,identity=row.sku?`<strong class="retrieval-card-sku">${esc(row.sku)}</strong><small class="retrieval-card-filename">${esc(row.name)}</small>`:`<strong>${esc(row.name)}</strong>`;return `<button id="${clientId}-result-${i}" class="retrieval-card catalog-card ${i===selected?'active':''}" role="option" aria-selected="${i===selected}" data-result="${i}" data-format="${esc((row.media_format||row.extension?.replace('.','')||'图片').toUpperCase())}" title="${esc(row.path)}">${cover}<span class="retrieval-card-copy">${identity}${Number.isFinite(row.score)?`<span class="reference-score">${row.match_mode==='frame'?'画面相似度':'内容相似度'} ${Math.min(100,Math.max(0,row.score*100)).toFixed(1)}%</span>`:''}<small class="retrieval-card-location" title="${esc(row.path)}">${esc(row.path)}</small><small class="retrieval-card-reason">${esc(row.reasons?.join(' · '))}</small></span></button>`}).join('') : `<div class="retrieval-empty"><strong>没有找到匹配资产</strong>试试更短的 SKU 或画面关键词。<br>先在资产归档添加目录，再在检索设置开启自动建立图搜图库。<br>未解析素材仍可通过已有文件名和 SKU 检索。</div>`;
      if(start)grid.insertAdjacentHTML('beforeend',markup);else grid.innerHTML=markup;
      for(const image of grid.querySelectorAll('.retrieval-preview img')){
        const preview=image.closest('.retrieval-preview');
        image.addEventListener('load',()=>preview.dataset.previewState='ready',{once:true});
        image.addEventListener('error',()=>preview.dataset.previewState='error',{once:true});
        if(image.complete&&image.naturalWidth)preview.dataset.previewState='ready';
      }
      if(imageData&&!rows.length)grid.innerHTML='<div class="retrieval-empty"><strong>没有达到当前相似度的素材</strong>降低左侧最低相似度，或替换参考图后重试。</div>';
    }
    function pagingState(){
      more.hidden=!(hasMore||appendFailed);more.disabled=loading;more.textContent=appendFailed?'重试加载':'加载更多';
      loadStatus.hidden=!compact||!loading||!rows.length;loadStatus.textContent='正在加载更多结果…';
      grid.setAttribute('aria-busy',String(loading));
    }
    function maybeLoadMore(){
      if(!compact||!active()||loading||!hasMore||appendFailed||!cursor||document.querySelector('#asset-image-viewer[open]'))return;
      const pane=getComputedStyle(resultsPane).overflowY==='visible'?find('.retrieval-body'):resultsPane;
      if(pane.clientHeight&&pane.getClientRects().length&&pane.scrollHeight-pane.scrollTop-pane.clientHeight<=Math.max(320,pane.clientHeight*.75))search(true);
    }
    if(compact){
      resultsPane.addEventListener('scroll',maybeLoadMore,{passive:true});
      find('.retrieval-body').addEventListener('scroll',maybeLoadMore,{passive:true});
      new ResizeObserver(maybeLoadMore).observe(resultsPane);
    }
    document.addEventListener('framekeep:design-previews-repaired',()=>{
      for(const img of grid.querySelectorAll('.retrieval-card img')){
        const row=rows[Number(img.closest('[data-result]').dataset.result)];
        if(row?.media_type==='design')img.src=thumbnailUrl(row);
      }
    });
    async function search(append=false) {
      if(append&&(loading||!hasMore||!cursor||!active()))return;
      clearTimeout(debounce);debounce=null;
      syncQueryHeading();
      if(!append)input.removeAttribute('aria-activedescendant');
      // 空搜索不浏览全库；同时使未完成的搜索与详情失效，避免旧结果回填。
      if(!input.value.trim()&&!image&&!imageItem){controller?.abort();sequence++;detailSequence++;imageData=null;rows=[];selected=-1;cursor='';hasMore=false;loading=false;appendFailed=false;cancelResultPreviews();grid.innerHTML='';resultsPane.scrollTop=0;resetDetail('');pagingState();referencePanel.setBusy(false);find('.retrieval-intent').hidden=true;message('');return}
      const current=++sequence;controller?.abort();controller=new AbortController();
      const requestedCursor=append?cursor:'';
      if(!append){imageData=null;rows=[];selected=-1;cursor='';hasMore=false;cancelResultPreviews();grid.innerHTML='<div class="retrieval-empty">正在查询资产索引…</div>';resultsPane.scrollTop=0;resetDetail('选择资产查看资料');detailSequence++}
      loading=true;appendFailed=false;pagingState();
      if(!append){find('.retrieval-intent').hidden=true;message('正在搜索…')}
      referencePanel.setBusy(!!(image||imageItem));
      try {
        let data;
        if(image||imageItem) data=await api('/api/intelligence/image',{...(imageItem?{item_id:imageItem}:{image}),root:find('.retrieval-root').value,media:find('.retrieval-media').value,threshold:referencePanel.value.threshold/100,match_mode:referencePanel.value.matchMode,limit:100},controller.signal);
        else if(find('.retrieval-mode').value==='semantic') data=await api('/api/intelligence/image',{text:input.value,root:find('.retrieval-root').value},controller.signal);
        else data=await api('/api/intelligence/search?'+new URLSearchParams({q:input.value,quick:compact?'1':'0',limit:pageSize(),client:clientId,sequence:current,facets:JSON.stringify(facets),root:find('.retrieval-root').value,media:find('.retrieval-media').value,cursor:append?cursor:''}),undefined,controller.signal);
        if(current!==sequence)return;
        if(data.indexing){
          if(append){appendFailed=true;message('检索索引正在更新，已保留现有结果，请稍后重试',true);return}
          message('正在准备快捷检索索引…');grid.innerHTML='<div class="retrieval-empty">正在准备快捷检索索引…</div>';
          setTimeout(()=>{if(current===sequence)search()},350);return;
        }
        if(data.pending){
          // 快捷索引在 SSD 忙碌时主动缩短本次读预算。保留当前结果并续查，
          // 不把内部 SQLite 中断展示成“暂时繁忙”或失败页。
          message(append?'正在继续加载结果…':'正在读取快捷索引…');
          setTimeout(()=>{if(current===sequence&&active())search(append)},Math.max(16,Math.min(120,Number(data.retry_after_ms)||32)));
          return;
        }
        if(image||imageItem){imageData=data;paintImageResults();return}
        const start=append?rows.length:0,known=new Set(append?rows.map(row=>row.result_id||row.id):[]);
        const added=data.rows.filter(row=>{const key=row.result_id||row.id;if(known.has(key))return false;known.add(key);return true});
        rows=append?[...rows,...added]:added;cursor=data.next_cursor||'';
        hasMore=!!(data.has_more&&cursor&&cursor!==requestedCursor&&added.length);renderResults(start);
        const understanding=find('.retrieval-intent');understanding.textContent=[...(data.interpretation?.explanation||[]),...(data.index_status?.ready===false?['旧资料索引补齐中，结果可能尚不完整']:[])].join(' · ');understanding.hidden=!understanding.textContent;
        message(`${rows.length} 项${hasMore?' · 还有更多':''}${data.scope==='files'?' · 文件名 / SKU':''} · ${data.elapsed_ms} ms${data.backend?' · '+(data.backend.startsWith('chinese-clip')?'CLIP 语义相似':'布局与配色相似'):''}${data.approximate?' · 近似召回':''}${data.incomplete?' · 已达时间预算，可缩小范围':''}`,false,`${hasMore?'已显示 ':''}${rows.length} 项匹配素材${hasMore?' · 还有更多':''}${data.incomplete?' · 可缩小范围':''}`);
      }catch(error){if(current!==sequence||error.name==='AbortError')return;message(append?'后续结果加载失败，已保留现有结果：'+error.message:error.message,true);if(append)appendFailed=true;else grid.innerHTML='<div class="retrieval-empty">'+esc(error.message)+'</div>'}
      finally{if(current===sequence){loading=false;pagingState();referencePanel.setBusy(false);requestAnimationFrame(maybeLoadMore)}}
    }
    async function select(index,{scroll=true}={}) {
      selected=index;const row=rows[index];if(!row)return;const ticket=++detailSequence;
      for(const card of grid.querySelectorAll('[data-result]')){const active=Number(card.dataset.result)===index;card.setAttribute('aria-selected',String(active));card.classList.toggle('active',active)}
      if(scroll)grid.querySelector(`[data-result="${index}"]`)?.scrollIntoView({block:'nearest'});
      input.setAttribute('aria-activedescendant',`${clientId}-result-${index}`);
      if(minimalGrid()){detail.hidden=true;return}
      if(!compact&&!detailExpanded)return;
      if(compact)detail.hidden=false;
      detailContent.innerHTML=`${compact?'<button data-detail="close" class="retrieval-detail-close" aria-label="关闭资产详情">关闭 ×</button>':''}<img src="${esc(thumbnailUrl(row,900))}" alt="资产预览"><h3>${esc(row.sku||'未确认 SKU')}</h3><strong>${esc(row.name)}</strong><p class="path">${esc(row.path)}</p><div><button data-detail="preview">预览</button><button data-detail="folder">打开所在目录</button><button data-detail="similar">查找相似图片</button><button data-detail="parse">解析此素材</button><button data-detail="copy">复制路径</button></div><p class="retrieval-detail-message" role="status"></p><label>确认 SKU<input data-detail="sku-value" value="${esc(row.sku)}" maxlength="80"></label><button data-detail="assign">保存人工 SKU</button><div class="retrieval-attributes">正在读取已解析资料…</div>`;
      detail.onclick=async event=>{
        const name=event.target.closest('[data-detail]')?.dataset.detail;if(!name)return;
        if(name==='close'){closeDetail();return}
        const note=detail.querySelector('.retrieval-detail-message');
        try{
          if(name==='preview')await openResult(index);
          if(name==='folder')await api('/api/catalog/open',{root_id:row.root,folder:row.folder});
          if(name==='similar'){imageItem=row.id;image='';input.value='';reference(row.preview,row.name);await search()}
          if(name==='copy'){await navigator.clipboard.writeText(row.path);note.textContent='路径已复制'}
          if(name==='parse'){await api('/api/intelligence/enqueue',{item_ids:[row.id],force:true});note.textContent=state?.config.enabled?'已加入多维解析队列':'已排队，请在检索设置启用解析';}
          if(name==='assign'){await api('/api/catalog/assign',{item_id:row.id,sku:detail.querySelector('[data-detail="sku-value"]').value});note.textContent='人工 SKU 已保存';await search()}
        }catch(error){note.textContent=error.message}
      };
      try {
        const result=await api('/api/intelligence/details?'+new URLSearchParams({item:row.id,...(row.frame_index!==undefined?{frame:row.frame_index}:{})}));
        if(ticket!==detailSequence)return;
        const html=result.parsed?result.frames.map((frame,i)=>`<section><h4>${row.media_type==='video'?'采样帧 '+(i+1)+' · '+frame.seconds+'s':'多维资料'}</h4><p>${esc(frame.result.summary)}</p><dl>${Object.entries({...frame.result.attributes,...frame.result.custom}).filter(([,v])=>v).map(([k,v])=>`<dt>${esc(state?.dimensions[k]||k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>${frame.result.visible_text?'<details><summary>可见文字证据</summary><pre>'+esc(frame.result.visible_text)+'</pre></details>':''}</section>`).join(''):'尚未完成多维解析。点击“解析此素材”后，在设置启用后台解析。';
        const ocr=result.ocr?`<section><h4>OCR 文本与 SKU 核验</h4><p>${esc(result.ocr.validation?.reason||'等待目录核验')}</p><pre>${esc(result.ocr.text||'未识别到可见文字')}</pre></section>`:'';
        detail.querySelector('.retrieval-attributes').innerHTML=ocr+(result.stale?'<p class="error">源文件已变化，以下为旧资料，已从检索排除。</p>':'')+html;
      }catch(error){if(ticket===detailSequence)detail.querySelector('.retrieval-attributes').textContent=error.message}
    }
    async function openResult(index){
      const row=rows[index];if(!row)return;
      try{
        if(window.FramekeepAssetViewer){
          const pane=find('.retrieval-results-area'),top=pane.scrollTop;
          const pending=compact?[...grid.querySelectorAll('img[src]')].filter(img=>!img.complete).map(img=>[img,img.getAttribute('src')]):[];
          for(const [img] of pending)img.removeAttribute('src');
          if(compact)document.getElementById('asset-image-viewer')?.addEventListener('close',()=>{
            pane.scrollTop=top;requestAnimationFrame(()=>{if(!document.querySelector('#asset-image-viewer[open]'))pane.scrollTop=top});
            for(const [img,src] of pending)if(img.isConnected&&!img.getAttribute('src'))img.src=src;
            requestAnimationFrame(maybeLoadMore);
          },{once:true});
          FramekeepAssetViewer.open('archive',rows,row.result_id||row.id,grid.querySelector(`[data-result="${index}"]`));
        }
        else if(compact)message('预览组件正在准备，请稍后重试',true);
        else if(invoke)await invoke('search_open_asset',{itemId:row.id,frame:row.frame_index??null});
        else window.open('/api/catalog/preview/'+row.id+'?size=2400','_blank','noopener');
      }catch(error){message(error.message,true)}
    }
    function applyReference(value){
      image=value.image;imageItem=value.itemId;input.disabled=value.active;
      for(const control of host.querySelectorAll('.retrieval-media,.retrieval-mode,.retrieval-facet-key'))control.disabled=value.active;
      find('.retrieval-filter-chips').inert=value.active;
      find('.retrieval-reference-note').textContent=value.active?'参考图已载入，正在查询图像索引；文字和维度筛选暂不叠加。':'';
    }
    function paintImageResults(){
      if(!imageData)return;
      rows=referencePanel.filter(imageData.rows);selected=-1;detailSequence++;
      resetDetail('选择相似素材查看详情');renderResults();find('.retrieval-more').hidden=true;
      message(`${rows.length} / ${imageData.rows.length} 项候选 · 相似度 ≥ ${referencePanel.value.threshold}% · ${imageData.match_mode==='frame'?'画面细节相似':imageData.backend?.startsWith('chinese-clip')?'CLIP 内容相似':'布局与配色相似'} · ${imageData.elapsed_ms} ms${imageData.approximate?' · 近似召回':''}${imageData.incomplete?' · 部分候选尚未完成核验':''}`);
    }
    const referencePanel=FramekeepReferenceSearch.mount(find('.retrieval-reference'),{
      inputClass:'retrieval-upload',prepare:(file,signal)=>api('/api/intelligence/reference',file,signal),
      onChange:value=>{applyReference(value);imageData=null;search()},
      onRefresh:()=>search(),onThreshold:()=>{paintImageResults();clearTimeout(debounce);debounce=setTimeout(()=>search(),220)}
    });
    find('[data-reference="clear"]').dataset.action='clear-image';
    function reference(src,label){referencePanel.set({image,itemId:imageItem,src,label},false);applyReference(referencePanel.value)}
    async function upload(file){await referencePanel.upload(file)}
    action('search').onclick=()=>search();action('image').onclick=()=>referencePanel.choose();
    action('settings').onclick=()=>settings().catch(error=>message(error.message,true));
    action('index').onclick=async()=>{try{await api('/api/intelligence/enqueue',{root:find('.retrieval-root').value});message('已标记待解析素材；在检索设置中启用模型可开始后台处理。');await settings()}catch(error){message(error.message,true)}};
    action('quick').onclick=()=>openQuick();if(close)action('close').onclick=close;
    action('clear-facets').onclick=()=>{facets={};for(const chip of find('.retrieval-filter-chips').children)chip.setAttribute('aria-pressed','false');action('clear-facets').hidden=true;search()};
    input.addEventListener('input',()=>{hasMore=false;syncQueryHeading();clearTimeout(debounce);controller?.abort();sequence++;cancelResultPreviews();if(!input.value.trim()&&!image&&!imageItem){search();return}if(find('.retrieval-mode').value==='index')debounce=setTimeout(()=>search(),30)});
    input.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();if(minimalGrid()&&selected>=0)openResult(selected);else search()}});
    for(const select of host.querySelectorAll('.retrieval-root,.retrieval-media,.retrieval-mode'))select.onchange=()=>search();
    grid.onclick=event=>{const card=event.target.closest('[data-result]');if(card){const index=Number(card.dataset.result);if(!rows[index])return;select(index,{scroll:!minimalGrid()});if(!compact&&rows[index].frame_index!==undefined)openResult(index)}};
    grid.ondblclick=event=>{if(event.target.closest('[data-result]'))openResult(selected)};
    find('.retrieval-more').onclick=()=>search(true);
    host.addEventListener('keydown',event=>{
      if(event.key==='Escape'&&!compact&&!detail.hidden){event.preventDefault();event.stopPropagation();closeDetail();return}
      if(event.target.closest('.reference-search'))return;
      if(event.target.closest('select,textarea,.reference-actions,.retrieval-detail input,.retrieval-detail button,.retrieval-detail-toggle')||!['ArrowDown','ArrowUp','ArrowLeft','ArrowRight','Enter'].includes(event.key))return;
      if(['ArrowLeft','ArrowRight'].includes(event.key)&&(event.target===input||!minimalGrid()))return;
      if(event.key==='Enter'&&event.target===input)return;
      event.preventDefault();event.stopPropagation();
      if(event.key==='Enter')openResult(selected);
      else if(rows.length){
        const columns=minimalGrid()?getComputedStyle(grid).gridTemplateColumns.split(' ').filter(Boolean).length:1;
        const delta=event.key==='ArrowDown'?columns:event.key==='ArrowUp'?-columns:event.key==='ArrowRight'?1:-1;
        select(selected<0?(delta>0?0:rows.length-1):minimalGrid()?Math.max(0,Math.min(rows.length-1,selected+delta)):(selected+delta+rows.length)%rows.length);
      }
    });
    host.addEventListener('paste',event=>{if(event.defaultPrevented)return;const file=[...(event.clipboardData?.items||[])].find(item=>item.type.startsWith('image/'))?.getAsFile();if(file){event.preventDefault();upload(file).catch(error=>message(error.message,true))}});
    host.addEventListener('dragover',event=>{if(event.dataTransfer?.types.includes('Files'))event.preventDefault()});
    host.addEventListener('drop',event=>{if(event.defaultPrevented)return;if(event.dataTransfer?.files.length){event.preventDefault();upload(event.dataTransfer.files[0]).catch(error=>message(error.message,true))}});
    async function refresh() {
      const started=sequence;
      try {
        try{await bootstrap()}catch(error){if(error.name!=='AbortError'||!active()||started!==sequence)throw error;await bootstrap()}
        if(!active()||started!==sequence)return;
        const root=find('.retrieval-root'),value=root.value;root.innerHTML='<option value="">全部归档</option>'+roots.map(row=>`<option value="${esc(row.id)}">${esc(row.path)}</option>`).join('');root.value=value;
        find('.retrieval-coverage').textContent=state.lightweight?'解析详情请查看检索设置':`已解析 ${state.coverage} 项 · 等待 ${state.counts?.queued||0} · ${state.config.enabled?(state.config.paused?'已暂停':'后台运行'):'未启用解析'}`;
        const dimension=find('.retrieval-facet-key'),oldDimension=dimension.value;dimension.innerHTML='<option value="">常用维度</option>'+Object.entries({...state.dimensions,...Object.fromEntries(state.config.custom_dimensions.map(key=>[key,key]))}).map(([key,label])=>`<option value="${esc(key)}">${esc(label)}</option>`).join('');dimension.value=oldDimension;
        async function loadFacets(){const ticket=++facetSequence;const data=await api('/api/intelligence/facets?'+new URLSearchParams({key:dimension.value}));if(ticket!==facetSequence||!active())return;const chips=find('.retrieval-filter-chips');
        chips.innerHTML=data.rows.map(row=>`<button data-key="${esc(row.key)}" data-value="${esc(row.value)}" aria-pressed="${facets[row.key]===row.value}">${esc(row.value)}</button>`).join('')||'<small>解析后显示实际维度</small>';
        chips.onclick=event=>{const chip=event.target.closest('[data-key]');if(!chip)return;const key=chip.dataset.key;if(facets[key]===chip.dataset.value)delete facets[key];else facets[key]=chip.dataset.value;for(const item of chips.children)item.setAttribute('aria-pressed',String(facets[item.dataset.key]===item.dataset.value));action('clear-facets').hidden=!Object.keys(facets).length;search()};}
        const facetError=error=>{find('.retrieval-filter-chips').textContent='筛选建议暂不可用，请重新选择维度重试。'};
        dimension.onchange=()=>loadFacets().catch(facetError);if(!minimalGrid())loadFacets().catch(facetError);
        if(sequence===started)await search();
      }catch(error){if(active()&&started===sequence&&error.name!=='AbortError')message(error.message,true)}
    }
    return {refresh,closeDetail,focus:()=>input.focus(),chooseReference:()=>referencePanel.choose(),searchText(text){input.value=text;search()},similar(item){imageItem=item;image='';reference('/api/catalog/preview/'+item+'?size=420','归档所选素材');search()},cancel(){controller?.abort();cancelResultPreviews();sequence++;detailSequence++;loading=false;hasMore=false;appendFailed=false;pagingState();if(menu)menu.open=false}};
  }

  const quickFields=config=>`<label>搜索样式<select data-config="quick_mode"><option value="minimal" ${config.quick_mode==='minimal'?'selected':''}>极简搜索 · 居中搜索框</option><option value="full" ${config.quick_mode!=='minimal'?'selected':''}>全能搜索 · 筛选与资产详情</option></select></label><label><input data-config="quick_background" type="checkbox" ${config.quick_background!==false?'checked':''}> 模糊屏幕背景</label><p>关闭后直接浮在屏幕上，不模糊、不暗化背景。极简模式输入后，顶部显示搜索词和大卡片结果，输入框停靠下方并保留底部留白。</p><label class="quick-range">模糊度 <output data-quick-output="quick_blur"></output><input data-config="quick_blur" type="range" min="0" max="48" step="1" value="${config.quick_blur??18}"></label><label class="quick-range">背景亮度 <output data-quick-output="quick_brightness"></output><input data-config="quick_brightness" type="range" min="0" max="100" step="1" value="${config.quick_brightness??75}"></label><p>拖动可即时预览，点击保存后生效。桌面背景使用本次唤起时的画面，下次唤起更新。</p>`;
  const configDialog=document.createElement('dialog');configDialog.className='retrieval-settings';configDialog.id='retrieval-settings';document.body.append(configDialog);configDialog.addEventListener('close',()=>window.FramekeepQuickAppearance?.reset());
  async function settings() {
    await bootstrap(true);const config=state.config;
    configDialog.innerHTML=`<header><h2>多模态检索设置</h2><button data-config="close">关闭 ×</button></header><div class="retrieval-settings-grid"><section><h3>解析与内容归组</h3><label><input data-config="enabled" type="checkbox" ${config.enabled?'checked':''}> 启用归档 VLM 多维解析</label><label><input data-config="auto_group" type="checkbox" ${config.auto_group?'checked':''}> 证据完整时自动确认同款 SKU</label><p>要求全目录图片均解析、单款主体、具体款式/品类/颜色一致且存在唯一有效 SKU；混款或证据不足进入复核。公司款号规则沿用“拍摄进度 → 设置”。</p><button data-config="models" ${native?'hidden':''}>配置 / 选用多模态模型</button><label>均匀覆盖补充帧数<input data-config="video_frames" type="number" min="1" max="8" value="${config.video_frames}"></label><label>视频抽帧间隔（秒）<input data-config="video_interval_ms" type="number" min="0.1" max="240" step="0.1" value="${config.video_interval_ms/1000}"></label><label>每视频最大采样数<input data-config="video_max_frames" type="number" min="24" max="20000" value="${config.video_max_frames}"></label><p>归档视频自动按间隔抽帧，合并均匀取样并覆盖首尾。长片超过预算会增大间隔。逐帧保存，重试只补失败帧；点击命中帧预览前后各 1.5 秒。抽样无法保证捕获所有瞬时画面。</p><h3>基础解析维度</h3><div class="retrieval-dimension-badges">${Object.values(state.dimensions).map(label=>'<span>'+esc(label)+'</span>').join('')}</div><label>自定义维度（每行一项，最多 20 项）<textarea data-config="custom_dimensions" rows="4">${esc(config.custom_dimensions.join('\n'))}</textarea></label></section><section><h3>以图搜图</h3><input type="hidden" data-config="visual_backend" value="clip"><label><input data-config="image_index_enabled" type="checkbox" ${config.image_index_enabled?'checked':''}> 自动建立图搜图库</label><p>自动处理全部已添加目录及子目录，新照片增量补齐；图片描述解析可单独运行。</p><label>MaterialSearch 模型目录<input data-config="material_root" value="${esc(config.material_root)}"></label><p data-image-index-status role="status"></p><button data-config="image_refresh">开始 / 刷新建库</button> <button data-config="image_pause">暂停建库</button> <button data-config="image_retry">重试失败图片</button><h3>桌面快捷搜索</h3>${quickFields(config)}<label>全局快捷键<select data-config="shortcut">${[['Ctrl+Shift+Space','Ctrl + Shift + 空格（推荐）'],['Alt+Space','Alt + 空格'],['Ctrl+Alt+F','Ctrl + Alt + F'],['off','关闭全局快捷键']].map(([value,label])=>`<option value="${value}" ${config.shortcut===value?'selected':''}>${label}</option>`).join('')}</select></label><p>桌面版运行期间可在其他软件中唤起；Windows 的 Win+空格为输入法保留。网页仅支持当前页面快捷键。</p><button data-config="save" class="primary">保存设置</button> <button data-config="pause">${config.paused?'恢复队列':'暂停队列'}</button> <button data-config="retry">重试失败任务</button> <button data-config="backup">备份检索数据库</button><p class="retrieval-message" role="status">${esc(state.error||'解析证据保存在本机数据库，已确认 SKU 自动后台写入元数据。')}</p></section></div><details class="retrieval-monitor" open><summary>内容归组与人工复核（${state.groups.length}）</summary><div>${state.groups.map(group=>`<article class="retrieval-group" data-group="${esc(group.id)}"><strong>${esc(group.sku||'待确认款号')} · ${esc(labels[group.status]||group.status)}</strong><p>${esc(roots.find(root=>root.id===group.root)?.path||group.root)} / ${esc(group.folder)} · 解析 ${group.evidence.parsed}/${group.evidence.total} 张</p><p>${esc(group.reason)}</p>${group.status!=='confirmed'?`<input data-group-sku value="${esc(group.sku)}" placeholder="正确 SKU" maxlength="80"><button data-group-action="confirm">确认本目录归组</button> <button data-group-action="reject">忽略此提案</button>`:''}</article>`).join('')||'<p>解析完目录图片后自动产生归组证据。</p>'}</div></details><details class="retrieval-monitor"><summary>最近任务（${Object.entries(state.counts).map(([key,n])=>`${labels[key]} ${n}`).join(' · ')}）</summary>${state.jobs.map(job=>`<div class="retrieval-job"><span>${esc(job.name||job.item)} · ${esc(labels[job.status])}<br>${esc(job.reason)}</span><button data-job="${esc(job.id)}" data-job-item="${esc(job.item)}" data-job-action="${['queued','running'].includes(job.status)?'cancel':['completed','stale'].includes(job.status)?'reparse':'retry'}">${['queued','running'].includes(job.status)?'取消':['completed','stale'].includes(job.status)?'重新解析':'重试'}</button></div>`).join('')||'<p>暂无解析任务</p>'}</details>`;
    const field=name=>configDialog.querySelector(`[data-config="${name}"]`),note=configDialog.querySelector('.retrieval-message');
    const appearance=()=>({quick_mode:field('quick_mode').value,quick_background:field('quick_background').checked,quick_blur:Number(field('quick_blur').value),quick_brightness:Number(field('quick_brightness').value)});
    const preview=()=>{const value=appearance();window.FramekeepQuickAppearance?.apply(value);for(const name of ['quick_blur','quick_brightness']){field(name).disabled=!value.quick_background;configDialog.querySelector(`[data-quick-output="${name}"]`).textContent=value[name]+(name==='quick_blur'?' px':'%')}};
    for(const name of ['quick_mode','quick_background','quick_blur','quick_brightness'])field(name).oninput=preview;
    preview();
    field('close').onclick=()=>configDialog.close();field('models').onclick=()=>{configDialog.close();$('assistant-settings')?.click()};
    field('save').onclick=async()=>{
      field('save').disabled=true;let shortcutApplied=false;const previousShortcut=state.config.shortcut;
      try{
        const body={...appearance(),enabled:field('enabled').checked,auto_group:field('auto_group').checked,custom_dimensions:field('custom_dimensions').value.split('\n').map(x=>x.trim()).filter(Boolean),visual_backend:field('visual_backend').value,image_index_enabled:field('image_index_enabled').checked,material_root:field('material_root').value,video_frames:Number(field('video_frames').value),video_interval_ms:Math.round(Number(field('video_interval_ms').value)*1000),video_max_frames:Number(field('video_max_frames').value),shortcut:field('shortcut').value};
        // 先注册桌面快捷键，冲突时不保存无法使用的配置。
        if(invoke){await invoke('search_shortcut',{shortcut:body.shortcut});shortcutApplied=true}
        state=await api('/api/intelligence/config',body);window.FramekeepQuickAppearance?.apply(state.config,true);note.textContent='设置已保存，后台将按新版本补充解析。';
      }catch(error){if(shortcutApplied)try{await invoke('search_shortcut',{shortcut:previousShortcut})}catch{}note.textContent=error.message}finally{field('save').disabled=false}
    };
    let imageState=state.image_index||{};
    const paintImageIndex=()=>{
      const phases={disabled:'未开启',loading:'正在准备模型',discovering:'正在发现照片',indexing:'正在建库',paused:'已暂停',idle:'等待新照片',failed:'建库异常'};
      const node=configDialog.querySelector('[data-image-index-status]');
      node.textContent=`${phases[imageState.phase]||'等待建库'} · 已建库 ${imageState.indexed||0} · 本轮 ${imageState.done||0}/${imageState.total||0} · 失败 ${imageState.failed||0}${imageState.offline?.length?' · 有目录离线':''}${imageState.error?' · '+imageState.error:''}`;
      field('image_pause').textContent=imageState.paused?'继续建库':'暂停建库';
    };
    const imageControl=async body=>{try{const value=await api('/api/intelligence/config',body);imageState=value.image_index||{};field('image_index_enabled').checked=value.config.image_index_enabled;paintImageIndex()}catch(error){note.textContent=error.message}};
    field('image_refresh').onclick=()=>imageControl({image_index_enabled:true,image_index_action:'refresh',material_root:field('material_root').value});
    field('image_pause').onclick=()=>imageControl({image_index_paused:!imageState.paused});
    field('image_retry').onclick=()=>imageControl({image_index_action:'retry'});
    paintImageIndex();
    clearInterval(configDialog.imagePoll);
    configDialog.imagePoll=setInterval(async()=>{if(!configDialog.open)return;try{const value=await api('/api/intelligence/search-state',undefined,undefined,true);imageState=value.image_index||{};paintImageIndex()}catch{}},1200);
    configDialog.addEventListener('close',()=>clearInterval(configDialog.imagePoll),{once:true});
    field('pause').onclick=async()=>{try{state=await api('/api/intelligence/control',{action:config.paused?'resume':'pause'});await settings()}catch(error){note.textContent=error.message}};
    field('retry').onclick=async()=>{try{await api('/api/intelligence/control',{action:'retry'});note.textContent='失败任务已重新排队'}catch(error){note.textContent=error.message}};
    field('backup').onclick=async()=>{try{field('backup').disabled=true;const result=await api('/api/intelligence/backup',{});note.textContent='已校验数据库备份：'+result.path}catch(error){note.textContent=error.message}finally{field('backup').disabled=false}};
    configDialog.onclick=async event=>{
      const groupButton=event.target.closest('[data-group-action]'),jobButton=event.target.closest('[data-job]');
      if(!groupButton&&!jobButton)return;
      try{
        if(groupButton){const group=state.groups.find(row=>row.id===groupButton.closest('[data-group]').dataset.group);await api('/api/intelligence/group',{id:group.id,signature:group.signature,sku:groupButton.closest('[data-group]').querySelector('input').value,reject:groupButton.dataset.groupAction==='reject'})}
        else if(jobButton.dataset.jobAction==='reparse')await api('/api/intelligence/enqueue',{item_ids:[jobButton.dataset.jobItem],force:true});
        else await api('/api/intelligence/control',{action:jobButton.dataset.jobAction,job_id:jobButton.dataset.job});
        await settings();
      }catch(error){note.textContent=error.message}
    };
    if(!configDialog.open)configDialog.showModal();
  }
  let quick,quickDialog,pageView;
  async function openQuick(text='') {
    if(invoke&&!native){try{await invoke('search_show');return}catch(error){console.warn(error.message)}}
    if(native){quick.focus();if(text)quick.searchText(text);await quick.refresh();return}
    if(!quickDialog){quickDialog=document.createElement('dialog');quickDialog.className='quick-search-dialog';quickDialog.id='quick-search-dialog';document.body.append(quickDialog);quick=shell(quickDialog,{compact:true,close:()=>quickDialog.close()});quickDialog.addEventListener('close',()=>quick.cancel());quickDialog.addEventListener('keydown',event=>{if(event.key==='Escape'&&!configDialog.open&&!document.querySelector('#asset-image-viewer[open]')){event.preventDefault();event.stopImmediatePropagation();if(!quick.closeDetail())quickDialog.close()}},true);quickDialog.addEventListener('click',event=>{if(event.target===quickDialog){const rect=quickDialog.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)quickDialog.close()}})}
    if(!quickDialog.open)quickDialog.showModal();quick.focus();if(text)quick.searchText(text);await quick.refresh();
  }
  window.FramekeepRetrieval={openQuick,settings,async openAsset(id){if(!pageView)return;FramekeepWorkspace.show('retrieval');await pageView.refresh();pageView.similar(id)}};
  if(native){
    if(!window.FramekeepIO)window.FramekeepIO={api:(path,body,timeout,signal)=>api(path,body,signal)};
    const hideQuick=()=>{quick.cancel();invoke?.('search_hide')};
    quick=shell($('quick-native-shell'),{compact:true,close:hideQuick});quick.focus();quick.refresh();
    document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!configDialog.open&&!document.querySelector('#asset-image-viewer[open]')){event.preventDefault();if(!quick.closeDetail())hideQuick()}});
    document.body.addEventListener('click',event=>{if(event.target===document.body)hideQuick()});
    window.addEventListener('focus',()=>quick.focus());
    window.FramekeepRetrieval.wake=()=>{quick.focus();quick.refresh()};
  }else if(window.FramekeepWorkspace){
    const tab=document.createElement('button');tab.id='asset-retrieval-open';tab.textContent='资产检索';tab.setAttribute('role','tab');tab.setAttribute('aria-controls','asset-retrieval-page');tab.setAttribute('aria-selected','false');tab.tabIndex=-1;
    document.querySelector('.workspace-tabs').insertBefore(tab,$('sku-open'));
    const page=document.createElement('section');page.id='asset-retrieval-page';page.className='asset-workspace hidden';page.setAttribute('role','tabpanel');page.setAttribute('aria-labelledby',tab.id);document.querySelector('.workbench').append(page);pageView=shell(page);
    FramekeepWorkspace.register('retrieval',tab,page,()=>pageView.refresh());
    const archiveButton=document.createElement('button');archiveButton.id='catalog-image-search';archiveButton.textContent='以图搜图';archiveButton.className='bordered';document.querySelector('.catalog-toolbar')?.append(archiveButton);
    archiveButton.onclick=()=>{const id=document.querySelector('#catalog-grid .catalog-card.active')?.dataset.catalogItem;FramekeepWorkspace.show('retrieval');if(id)pageView.similar(id);else pageView.chooseReference()};
    const quickSettings=$('quick-appearance-controls');
    let quickSettingsSequence=0;
    async function renderQuickSettings(){
      if(!quickSettings)return;
      const ticket=++quickSettingsSequence;quickSettings.textContent='正在读取快捷搜索设置…';
      try{
        const result=await api('/api/intelligence/config',undefined,AbortSignal.timeout(8000));
        if(ticket!==quickSettingsSequence)return;
        state={...state,...result};token=result.request_token;
        quickSettings.innerHTML=quickFields(state.config)+'<button data-quick-save class="primary">保存快捷搜索设置</button><p class="retrieval-message" role="status"></p>';
        const field=name=>quickSettings.querySelector(`[data-config="${name}"]`),note=quickSettings.querySelector('.retrieval-message');
        const value=()=>({quick_mode:field('quick_mode').value,quick_background:field('quick_background').checked,quick_blur:Number(field('quick_blur').value),quick_brightness:Number(field('quick_brightness').value)});
        const preview=()=>{const v=value();window.FramekeepQuickAppearance?.apply(v);for(const name of ['quick_blur','quick_brightness']){field(name).disabled=!v.quick_background;quickSettings.querySelector(`[data-quick-output="${name}"]`).textContent=v[name]+(name==='quick_blur'?' px':'%')}};
        for(const name of ['quick_mode','quick_background','quick_blur','quick_brightness'])field(name).oninput=preview;
        preview();quickSettings.querySelector('[data-quick-save]').onclick=async event=>{const button=event.currentTarget;button.disabled=true;try{state={...state,...await api('/api/intelligence/config',value(),AbortSignal.timeout(8000))};window.FramekeepQuickAppearance?.apply(state.config,true);note.textContent='快捷搜索设置已保存，下一次唤起立即使用。'}catch(error){note.textContent=error.name==='TimeoutError'?'保存设置超时，请重试。':error.message}finally{button.disabled=false}};
      }catch(error){
        if(ticket!==quickSettingsSequence)return;
        quickSettings.textContent=error.name==='TimeoutError'?'读取设置超时，请重试。':'读取设置失败：'+error.message;
        const retry=document.createElement('button');retry.type='button';retry.textContent='重新读取';retry.onclick=renderQuickSettings;quickSettings.append(retry);
      }
    }
    document.querySelector('[data-settings-section="quick"]')?.addEventListener('click',renderQuickSettings);
    document.addEventListener('framekeep:workspace',event=>{
      if(event.detail.page==='settings'&&document.querySelector('[data-settings-section="quick"][aria-selected="true"]'))renderQuickSettings();
      else if(event.detail.page!=='settings')window.FramekeepQuickAppearance?.reset();
    });
    const settingsButton=document.createElement('button');settingsButton.id='intelligence-settings-open';settingsButton.textContent='多模态解析与快捷检索设置';settingsButton.onclick=()=>settings().catch(error=>Framekeep?.notify(error.message));$('model-catalog')?.append(settingsButton);
    document.addEventListener('keydown',event=>{if(event.ctrlKey&&event.shiftKey&&(event.code==='Space'||event.key===' ')){event.preventDefault();event.stopImmediatePropagation();openQuick().catch(error=>console.error(error.message))}},true);
    const original=window.FramekeepRetrieval.openAsset;
    window.FramekeepRetrieval.previewAsset=async(id,frame=null)=>{if(frame!==null){const item=await api('/api/intelligence/frame-info?'+new URLSearchParams({item:id,frame}));FramekeepAssetViewer.open('archive',[item],item.result_id);return}const row=(await api('/api/catalog/items?'+new URLSearchParams({limit:1}))).rows.find(row=>row.id===id);if(row)FramekeepAssetViewer.open('archive',[row],id);else{const item=await api('/api/intelligence/item?'+new URLSearchParams({item:id}));FramekeepAssetViewer.open('archive',[item],id)}};
    document.addEventListener('framekeep:workspace',event=>{if(event.detail.page!=='retrieval')pageView.cancel()});
  }
})();
