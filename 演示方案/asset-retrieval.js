(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const native = !!$('quick-native-shell'), invoke = window.__TAURI__?.core?.invoke;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let token = null, state = null, roots = [], booting = null;
  async function api(path, body, signal) {
    if (body !== undefined && !token) await bootstrap();
    const binary=body instanceof Blob;
    const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST',
      headers: body === undefined ? {} : {'Content-Type':binary?'application/octet-stream':'application/json','X-Framekeep-Token':token,...(binary?{'X-Reference-Name':encodeURIComponent(body.name||'clipboard.jpg')}:{})},
      ...(body === undefined ? {} : {body:binary?body:JSON.stringify(body)}),
      signal:binary?signal:signal?AbortSignal.any([signal,AbortSignal.timeout(200000)]):AbortSignal.timeout(200000)});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || `请求失败 ${response.status}`);
    return result;
  }
  async function bootstrap() {
    if (booting) return booting;
    booting = (async () => {
      state = await api('/api/intelligence/state');token = state.request_token;
      const tree = await api('/api/catalog/tree');roots = tree.roots;
      return state;
    })().finally(() => {booting=null});
    return booting;
  }
  const labels = {queued:'等待',running:'解析中',completed:'已完成',failed:'失败',stale:'已失效',cancelled:'已取消',ready:'可归组',review:'待复核',confirmed:'已确认'};
  function shell(host, {compact=false,close=null}={}) {
    host.classList.add(compact?'quick-search-shell':'retrieval-workspace');
    const scopeFilters=`<label class="retrieval-scope"><span>归档位置</span><select class="retrieval-root" aria-label="归档位置"><option value="">全部归档</option></select></label><label class="retrieval-scope"><span>素材类型</span><select class="retrieval-media" aria-label="素材类型"><option value="">全部素材</option><option value="image">图片 / RAW</option><option value="video">视频帧</option><option value="design">设计文件</option></select></label><label class="retrieval-scope"><span>搜索方式</span><select class="retrieval-mode" aria-label="搜索方式"><option value="index">SKU / 关键词</option><option value="semantic">CLIP 语义搜索</option></select></label>`;
    host.innerHTML = `<header class="retrieval-heading catalog-heading ${compact?'':'retrieval-desktop-heading'}"><div class="retrieval-heading-title"><h2>${compact?'快捷资产搜索':'资产检索'}</h2><p>SKU · 画面描述 · 多维筛选 · 以图搜图</p></div>${compact?'':'<nav class="retrieval-navigation" aria-label="工作台导航"><button data-workspace-target="sku-open">拍摄进度</button><button aria-current="page" data-workspace-target="asset-retrieval-open">资产检索</button><button data-workspace-target="asset-archive-open">资产归档</button></nav><details class="retrieval-actions-menu"><summary aria-label="工作台设置" title="工作台设置"><svg viewBox="0 0 24 24" width="19" height="19" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5"><path d="m9.5 3-.6 2.3-2 .9-2.2-.6-2.5 4.3 1.6 1.7v2.3l-1.6 1.7 2.5 4.3 2.2-.6 2 .9.6 2.3h5l.6-2.3 2-.9 2.2.6 2.5-4.3-1.6-1.7v-2.3l1.6-1.7-2.5-4.3-2.2.6-2-.9L14.5 3z"/><circle cx="12" cy="12.8" r="3.3"/></svg></summary>'}<div class="retrieval-tools catalog-heading-actions"><button data-action="quick" ${compact?'hidden':''}>快捷搜索 <kbd>Ctrl ⇧ Space</kbd></button><button data-action="index">多维解析</button><button data-action="settings">检索设置</button>${compact?'':'<button data-action="image">以图搜图</button><button data-workspace-target="selection-tab">现场拍摄</button><button data-workspace-target="sku-database-open">SKU 数据库</button><button data-workspace-target="asset-analysis-open">解析任务</button><button data-workspace-target="app-settings-open">软件设置</button><button data-action="theme">切换主题</button>'}${close?'<button data-action="close" aria-label="关闭快捷搜索">Esc ×</button>':''}</div>${compact?'':'</details>'}</header>
      <div class="retrieval-searchbar"><input class="retrieval-query" type="search" aria-label="搜索 SKU 或画面关键词" placeholder="输入 SKU，或描述画面内容…" autocomplete="off" maxlength="300"><button data-action="search" class="primary">搜索</button>${compact?'<button data-action="image">以图搜图</button>':scopeFilters}</div>
      <div class="retrieval-reference-note"></div>
      <div class="retrieval-body"><aside class="retrieval-filters"><section class="retrieval-reference"></section><div class="retrieval-filter-scroll">${compact?'<h3>检索范围</h3>'+scopeFilters:''}<h3>多维筛选</h3><label>筛选维度<select class="retrieval-facet-key"><option value="">常用维度</option></select></label><div class="retrieval-filter-chips"></div><button data-action="clear-facets" hidden>清除筛选</button><details class="retrieval-index-info"><summary>索引状态与快捷键</summary><p class="retrieval-coverage">正在连接…</p><p>↑↓ 选择 · Enter 预览<br>Ctrl+V 粘贴参考图<br>也可将图片拖入窗口</p></details></div></aside><main class="retrieval-results-area"><div class="retrieval-result-status" role="status" aria-live="polite">正在连接资产数据库…</div><div class="retrieval-grid catalog-grid" role="listbox" aria-label="资产搜索结果"></div><button class="retrieval-more" hidden>加载更多</button></main><aside class="retrieval-detail" aria-label="资产详情" ${compact?'':'hidden'}><div class="retrieval-empty">选择一项资产<br>查看 SKU、多维资料与来源路径</div></aside></div>${compact?'<footer class="quick-search-footer"><span><kbd>↑ ↓</kbd>选择</span><span><kbd>Enter</kbd>查看</span><span><kbd>Esc</kbd>关闭</span><span>原片保持只读</span></footer>':''}`;
    const find = selector => host.querySelector(selector), action = name => find(`[data-action="${name}"]`);
    const input = find('.retrieval-query'), grid = find('.retrieval-grid'), status = find('.retrieval-result-status'), detail = find('.retrieval-detail');
    const menu=find('.retrieval-actions-menu');
    if(menu){
      for(const button of host.querySelectorAll('[data-workspace-target]'))button.onclick=()=>{$(button.dataset.workspaceTarget)?.click();menu.open=false};
      action('theme').onclick=()=>$('theme-open')?.click();
      menu.addEventListener('click',event=>{if(event.target.closest('button'))menu.open=false});
      menu.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();menu.open=false;menu.querySelector('summary').focus()}});
      document.addEventListener('pointerdown',event=>{if(!menu.contains(event.target))menu.open=false});
    }
    function closeDetail(){if(compact)return;detail.hidden=true;grid.querySelector(`[data-result="${selected}"]`)?.focus()}
    let imageData=null;
    let rows = [], selected = -1, cursor = '', image = '', imageItem = '', facets = {}, sequence = 0, controller=null, debounce=null, detailSequence=0;
    const active = () => native || (host.matches('dialog') ? host.open : !host.classList.contains('hidden'));
    const message = (text,error=false) => {status.textContent=text;status.classList.toggle('error',error)};
    function renderResults() {
      grid.innerHTML = rows.length ? rows.map((row,i) => `<button class="retrieval-card catalog-card ${i===selected?'active':''}" role="option" aria-selected="${i===selected}" data-result="${i}" title="${esc(row.path)}"><img loading="lazy" decoding="async" src="${esc(row.preview)}&size=420" alt="${esc(row.name)}"><span class="retrieval-card-copy"><strong>${esc(row.sku||row.name)}</strong><small>${esc(row.sku?row.name:row.media_format)}${row.media_type==='video'?' · 视频':''}</small>${Number.isFinite(row.score)?`<span class="reference-score">相似度 ${Math.min(100,Math.max(0,row.score*100)).toFixed(1)}%</span>`:''}<small class="retrieval-card-reason">${esc(row.reasons?.join(' · '))}</small></span></button>`).join('') : `<div class="retrieval-empty"><strong>没有找到匹配资产</strong>试试更短的 SKU 或画面关键词。<br>先在资产归档添加目录，再启用多维解析。<br>未解析素材仍可通过已有文件名和 SKU 检索。</div>`;
      if(imageData&&!rows.length)grid.innerHTML='<div class="retrieval-empty"><strong>没有达到当前相似度的素材</strong>降低左侧最低相似度，或替换参考图后重试。</div>';
      // preview 接口地址可能无查询串。
      for(const img of grid.querySelectorAll('img')) img.src=img.getAttribute('src').replace('/&','/?').replace(/(\/api\/catalog\/preview\/[^?&]+)&/,'$1?');
    }
    async function search(append=false) {
      clearTimeout(debounce);debounce=null;
      const current=++sequence;controller?.abort();controller=new AbortController();
      if(!append){imageData=null;rows=[];selected=-1;cursor='';grid.innerHTML='<div class="retrieval-empty">正在查询资产索引…</div>';detail.hidden=!compact;detail.innerHTML='<div class="retrieval-empty">选择资产查看资料</div>';detailSequence++}
      message('正在搜索…');find('.retrieval-more').hidden=true;referencePanel.setBusy(!!(image||imageItem));
      try {
        let data;
        if(image||imageItem) data=await api('/api/intelligence/image',{...(imageItem?{item_id:imageItem}:{image}),root:find('.retrieval-root').value,threshold:0,limit:100},controller.signal);
        else if(find('.retrieval-mode').value==='semantic') data=await api('/api/intelligence/image',{text:input.value,root:find('.retrieval-root').value},controller.signal);
        else data=await api('/api/intelligence/search?'+new URLSearchParams({q:input.value,facets:JSON.stringify(facets),root:find('.retrieval-root').value,media:find('.retrieval-media').value,cursor:append?cursor:''}),undefined,controller.signal);
        if(current!==sequence)return;
        if(image||imageItem){imageData=data;paintImageResults();return}
        rows=append?[...rows,...data.rows]:data.rows;cursor=data.next_cursor||'';renderResults();
        message(`${rows.length} 项${data.has_more?' · 还有更多':''} · ${data.elapsed_ms} ms${data.backend?' · '+(data.backend.startsWith('chinese-clip')?'CLIP 语义相似':'布局与配色相似'):''}${data.approximate?' · 近似召回':''}${data.incomplete?' · 已达时间预算，可缩小范围':''}`);
        find('.retrieval-more').hidden=!data.has_more;
      }catch(error){if(current!==sequence||error.name==='AbortError')return;message(error.message,true);grid.innerHTML='<div class="retrieval-empty">'+esc(error.message)+'</div>'}finally{if(current===sequence)referencePanel.setBusy(false)}
    }
    async function select(index) {
      selected=index;const row=rows[index];if(!row)return;const ticket=++detailSequence;
      for(const card of grid.querySelectorAll('[data-result]')){const active=Number(card.dataset.result)===index;card.setAttribute('aria-selected',String(active));card.classList.toggle('active',active)}
      grid.querySelector(`[data-result="${index}"]`)?.scrollIntoView({block:'nearest'});
      detail.hidden=false;
      detail.innerHTML=`${compact?'':'<button data-detail="close" class="retrieval-detail-close" aria-label="关闭资产详情">关闭 ×</button>'}<img src="/api/catalog/preview/${esc(row.id)}?size=900" alt="资产预览"><h3>${esc(row.sku||'未确认 SKU')}</h3><strong>${esc(row.name)}</strong><p class="path">${esc(row.path)}</p><div><button data-detail="preview">预览</button><button data-detail="folder">打开所在目录</button><button data-detail="similar">查找相似图片</button><button data-detail="parse">解析此素材</button><button data-detail="copy">复制路径</button></div><p class="retrieval-detail-message" role="status"></p><label>确认 SKU<input data-detail="sku-value" value="${esc(row.sku)}" maxlength="80"></label><button data-detail="assign">保存人工 SKU</button><div class="retrieval-attributes">正在读取已解析资料…</div>`;
      detail.onclick=async event=>{
        const name=event.target.closest('[data-detail]')?.dataset.detail;if(!name)return;
        if(name==='close'){closeDetail();return}
        const note=detail.querySelector('.retrieval-detail-message');
        try{
          if(name==='preview'){
            if(window.FramekeepAssetViewer)FramekeepAssetViewer.open('archive',rows,row.id,grid.querySelector(`[data-result="${index}"]`));
            else if(invoke)await invoke('search_open_asset',{itemId:row.id});
            else window.open('/api/catalog/preview/'+row.id+'?size=2400','_blank','noopener');
          }
          if(name==='folder')await api('/api/catalog/open',{root_id:row.root,folder:row.folder});
          if(name==='similar'){imageItem=row.id;image='';input.value='';reference(row.preview,row.name);await search()}
          if(name==='copy'){await navigator.clipboard.writeText(row.path);note.textContent='路径已复制'}
          if(name==='parse'){await api('/api/intelligence/enqueue',{item_ids:[row.id],force:true});note.textContent=state?.config.enabled?'已加入多维解析队列':'已排队，请在检索设置启用解析';}
          if(name==='assign'){await api('/api/catalog/assign',{item_id:row.id,sku:detail.querySelector('[data-detail="sku-value"]').value});note.textContent='人工 SKU 已保存';await search()}
        }catch(error){note.textContent=error.message}
      };
      try {
        const result=await api('/api/intelligence/details?'+new URLSearchParams({item:row.id}));
        if(ticket!==detailSequence)return;
        const html=result.parsed?result.frames.map((frame,i)=>`<section><h4>${row.media_type==='video'?'采样帧 '+(i+1)+' · '+frame.seconds+'s':'多维资料'}</h4><p>${esc(frame.result.summary)}</p><dl>${Object.entries({...frame.result.attributes,...frame.result.custom}).filter(([,v])=>v).map(([k,v])=>`<dt>${esc(state?.dimensions[k]||k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>${frame.result.visible_text?'<details><summary>可见文字证据</summary><pre>'+esc(frame.result.visible_text)+'</pre></details>':''}</section>`).join(''):'尚未完成多维解析。点击“解析此素材”后，在设置启用后台解析。';
        detail.querySelector('.retrieval-attributes').innerHTML=(result.stale?'<p class="error">源文件已变化，以下为旧资料，已从检索排除。</p>':'')+html;
      }catch(error){if(ticket===detailSequence)detail.querySelector('.retrieval-attributes').textContent=error.message}
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
      detail.hidden=!compact;
      detail.innerHTML='<div class="retrieval-empty">选择相似素材查看详情</div>';renderResults();find('.retrieval-more').hidden=true;
      message(`${rows.length} / ${imageData.rows.length} 项候选 · 相似度 ≥ ${referencePanel.value.threshold}% · ${imageData.backend?.startsWith('chinese-clip')?'CLIP 语义相似':'布局与配色相似'} · ${imageData.elapsed_ms} ms${imageData.approximate?' · 近似召回':''}${imageData.incomplete?' · 已达查询预算':''}`);
    }
    const referencePanel=FramekeepReferenceSearch.mount(find('.retrieval-reference'),{
      inputClass:'retrieval-upload',prepare:(file,signal)=>api('/api/intelligence/reference',file,signal),
      onChange:value=>{applyReference(value);imageData=null;search()},
      onRefresh:()=>search(),onThreshold:()=>paintImageResults()
    });
    find('[data-reference="clear"]').dataset.action='clear-image';
    function reference(src,label){referencePanel.set({image,itemId:imageItem,src,label},false);applyReference(referencePanel.value)}
    async function upload(file){await referencePanel.upload(file)}
    action('search').onclick=()=>search();action('image').onclick=()=>referencePanel.choose();
    action('settings').onclick=()=>settings().catch(error=>message(error.message,true));
    action('index').onclick=async()=>{try{await api('/api/intelligence/enqueue',{root:find('.retrieval-root').value});message('已标记待解析素材；在检索设置中启用模型可开始后台处理。');await settings()}catch(error){message(error.message,true)}};
    action('quick').onclick=()=>openQuick();if(close)action('close').onclick=close;
    action('clear-facets').onclick=()=>{facets={};for(const chip of find('.retrieval-filter-chips').children)chip.setAttribute('aria-pressed','false');action('clear-facets').hidden=true;search()};
    input.addEventListener('input',()=>{clearTimeout(debounce);if(find('.retrieval-mode').value==='index')debounce=setTimeout(()=>search(),240)});
    input.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();search()}});
    for(const select of host.querySelectorAll('.retrieval-root,.retrieval-media,.retrieval-mode'))select.onchange=()=>search();
    grid.onclick=event=>{const card=event.target.closest('[data-result]');if(card)select(Number(card.dataset.result))};
    grid.ondblclick=event=>{if(event.target.closest('[data-result]'))detail.querySelector('[data-detail="preview"]')?.click()};
    find('.retrieval-more').onclick=()=>search(true);
    host.addEventListener('keydown',event=>{
      if(event.key==='Escape'&&!compact&&!detail.hidden){event.preventDefault();event.stopPropagation();closeDetail();return}
      if(event.target.closest('.reference-search'))return;
      if(event.target.closest('select,textarea,.reference-actions,.retrieval-detail input,.retrieval-detail button')||!['ArrowDown','ArrowUp','Enter'].includes(event.key))return;
      if(event.key==='Enter'&&event.target===input)return;
      event.preventDefault();event.stopPropagation();
      if(event.key==='Enter')detail.querySelector('[data-detail="preview"]')?.click();
      else if(rows.length)select((selected+(event.key==='ArrowDown'?1:-1)+rows.length)%rows.length);
    });
    host.addEventListener('paste',event=>{if(event.defaultPrevented)return;const file=[...(event.clipboardData?.items||[])].find(item=>item.type.startsWith('image/'))?.getAsFile();if(file){event.preventDefault();upload(file).catch(error=>message(error.message,true))}});
    host.addEventListener('dragover',event=>{if(event.dataTransfer?.types.includes('Files'))event.preventDefault()});
    host.addEventListener('drop',event=>{if(event.defaultPrevented)return;if(event.dataTransfer?.files.length){event.preventDefault();upload(event.dataTransfer.files[0]).catch(error=>message(error.message,true))}});
    async function refresh() {
      try {
        await bootstrap();if(!active())return;
        const root=find('.retrieval-root'),value=root.value;root.innerHTML='<option value="">全部归档</option>'+roots.map(row=>`<option value="${esc(row.id)}">${esc(row.path)}</option>`).join('');root.value=value;
        find('.retrieval-coverage').textContent=`已解析 ${state.coverage} 项 · 等待 ${state.counts.queued||0} · ${state.config.enabled?(state.config.paused?'已暂停':'后台运行'):'未启用解析'}`;
        const dimension=find('.retrieval-facet-key'),oldDimension=dimension.value;dimension.innerHTML='<option value="">常用维度</option>'+Object.entries({...state.dimensions,...Object.fromEntries(state.config.custom_dimensions.map(key=>[key,key]))}).map(([key,label])=>`<option value="${esc(key)}">${esc(label)}</option>`).join('');dimension.value=oldDimension;
        async function loadFacets(){const data=await api('/api/intelligence/facets?'+new URLSearchParams({key:dimension.value}));const chips=find('.retrieval-filter-chips');
        chips.innerHTML=data.rows.map(row=>`<button data-key="${esc(row.key)}" data-value="${esc(row.value)}" aria-pressed="${facets[row.key]===row.value}">${esc(row.value)}</button>`).join('')||'<small>解析后显示实际维度</small>';
        chips.onclick=event=>{const chip=event.target.closest('[data-key]');if(!chip)return;const key=chip.dataset.key;if(facets[key]===chip.dataset.value)delete facets[key];else facets[key]=chip.dataset.value;for(const item of chips.children)item.setAttribute('aria-pressed',String(facets[item.dataset.key]===item.dataset.value));action('clear-facets').hidden=!Object.keys(facets).length;search()};}
        dimension.onchange=()=>loadFacets().catch(error=>message(error.message,true));await loadFacets();
        await search();
      }catch(error){message(error.message,true)}
    }
    return {refresh,focus:()=>input.focus(),chooseReference:()=>referencePanel.choose(),searchText(text){input.value=text;search()},similar(item){imageItem=item;image='';reference('/api/catalog/preview/'+item+'?size=420','归档所选素材');search()},cancel(){controller?.abort();sequence++;detailSequence++;if(menu)menu.open=false}};
  }

  const configDialog=document.createElement('dialog');configDialog.className='retrieval-settings';configDialog.id='retrieval-settings';document.body.append(configDialog);
  async function settings() {
    await bootstrap();const config=state.config;
    configDialog.innerHTML=`<header><h2>多模态检索设置</h2><button data-config="close">关闭 ×</button></header><div class="retrieval-settings-grid"><section><h3>解析与内容归组</h3><label><input data-config="enabled" type="checkbox" ${config.enabled?'checked':''}> 启用归档 VLM 多维解析</label><label><input data-config="auto_group" type="checkbox" ${config.auto_group?'checked':''}> 证据完整时自动确认同款 SKU</label><p>要求全目录图片均解析、单款主体、具体款式/品类/颜色一致且存在唯一有效 SKU；混款或证据不足进入复核。公司款号规则沿用“拍摄进度 → 设置”。</p><button data-config="models" ${native?'hidden':''}>配置 / 选用多模态模型</button><label>视频均匀采样帧数<input data-config="video_frames" type="number" min="1" max="8" value="${config.video_frames}"></label><p>采样结果保留时间点，维度组合只匹配同一帧。视频采样不能保证覆盖短暂画面。</p><h3>基础解析维度</h3><div class="retrieval-dimension-badges">${Object.values(state.dimensions).map(label=>'<span>'+esc(label)+'</span>').join('')}</div><label>自定义维度（每行一项，最多 20 项）<textarea data-config="custom_dimensions" rows="4">${esc(config.custom_dimensions.join('\n'))}</textarea></label></section><section><h3>以图搜图</h3><label>图像索引<select data-config="visual_backend"><option value="appearance" ${config.visual_backend==='appearance'?'selected':''}>本地外观 · 配色与布局</option><option value="clip" ${config.visual_backend==='clip'?'selected':''}>Chinese-CLIP · 图片与文字语义</option></select></label><p>外观索引无需模型，但不理解商品语义。CLIP 使用本机 MaterialSearch 的公开模型资源和独立进程，不连接其数据库。更改索引后会补充解析。</p><label>MaterialSearch 安装目录<input data-config="material_root" value="${esc(config.material_root)}"></label><p>${state.clip_available?'已发现本机 Chinese-CLIP 模型文件':'尚未发现 Chinese-CLIP 模型文件'} · 已有 CLIP 向量 ${state.vectors['chinese-clip-large-336:768']||0}</p><h3>桌面快捷搜索</h3><label>全局快捷键<select data-config="shortcut">${[['Ctrl+Shift+Space','Ctrl + Shift + 空格（推荐）'],['Alt+Space','Alt + 空格'],['Ctrl+Alt+F','Ctrl + Alt + F'],['off','关闭全局快捷键']].map(([value,label])=>`<option value="${value}" ${config.shortcut===value?'selected':''}>${label}</option>`).join('')}</select></label><p>桌面版运行期间可在其他软件中唤起；Windows 的 Win+空格为输入法保留。网页仅支持当前页面快捷键。</p><button data-config="save" class="primary">保存设置</button> <button data-config="pause">${config.paused?'恢复队列':'暂停队列'}</button> <button data-config="retry">重试失败任务</button> <button data-config="backup">备份检索数据库</button><p class="retrieval-message" role="status">${esc(state.error||'解析结果和检索索引保存在本机数据库，原片只读。')}</p></section></div><details class="retrieval-monitor" open><summary>内容归组与人工复核（${state.groups.length}）</summary><div>${state.groups.map(group=>`<article class="retrieval-group" data-group="${esc(group.id)}"><strong>${esc(group.sku||'待确认款号')} · ${esc(labels[group.status]||group.status)}</strong><p>${esc(roots.find(root=>root.id===group.root)?.path||group.root)} / ${esc(group.folder)} · 解析 ${group.evidence.parsed}/${group.evidence.total} 张</p><p>${esc(group.reason)}</p>${group.status!=='confirmed'?`<input data-group-sku value="${esc(group.sku)}" placeholder="正确 SKU" maxlength="80"><button data-group-action="confirm">确认本目录归组</button> <button data-group-action="reject">忽略此提案</button>`:''}</article>`).join('')||'<p>解析完目录图片后自动产生归组证据。</p>'}</div></details><details class="retrieval-monitor"><summary>最近任务（${Object.entries(state.counts).map(([key,n])=>`${labels[key]} ${n}`).join(' · ')}）</summary>${state.jobs.map(job=>`<div class="retrieval-job"><span>${esc(job.name||job.item)} · ${esc(labels[job.status])}<br>${esc(job.reason)}</span><button data-job="${esc(job.id)}" data-job-item="${esc(job.item)}" data-job-action="${['queued','running'].includes(job.status)?'cancel':['completed','stale'].includes(job.status)?'reparse':'retry'}">${['queued','running'].includes(job.status)?'取消':['completed','stale'].includes(job.status)?'重新解析':'重试'}</button></div>`).join('')||'<p>暂无解析任务</p>'}</details>`;
    const field=name=>configDialog.querySelector(`[data-config="${name}"]`),note=configDialog.querySelector('.retrieval-message');
    field('close').onclick=()=>configDialog.close();field('models').onclick=()=>{configDialog.close();$('assistant-settings')?.click()};
    field('save').onclick=async()=>{
      field('save').disabled=true;let shortcutApplied=false;const previousShortcut=state.config.shortcut;
      try{
        const body={enabled:field('enabled').checked,auto_group:field('auto_group').checked,custom_dimensions:field('custom_dimensions').value.split('\n').map(x=>x.trim()).filter(Boolean),visual_backend:field('visual_backend').value,material_root:field('material_root').value,video_frames:Number(field('video_frames').value),shortcut:field('shortcut').value};
        // 先注册桌面快捷键，冲突时不保存无法使用的配置。
        if(invoke){await invoke('search_shortcut',{shortcut:body.shortcut});shortcutApplied=true}
        state=await api('/api/intelligence/config',body);note.textContent='设置已保存，后台将按新版本补充解析。';
      }catch(error){if(shortcutApplied)try{await invoke('search_shortcut',{shortcut:previousShortcut})}catch{}note.textContent=error.message}finally{field('save').disabled=false}
    };
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
    if(native){await quick.refresh();if(text)quick.searchText(text);quick.focus();return}
    if(!quickDialog){quickDialog=document.createElement('dialog');quickDialog.className='quick-search-dialog';quickDialog.id='quick-search-dialog';document.body.append(quickDialog);quick=shell(quickDialog,{compact:true,close:()=>quickDialog.close()});quickDialog.addEventListener('close',()=>quick.cancel());quickDialog.addEventListener('keydown',event=>{if(event.key==='Escape'&&!configDialog.open&&!document.querySelector('#asset-image-viewer[open]')){event.preventDefault();event.stopImmediatePropagation();quickDialog.close()}},true);quickDialog.addEventListener('click',event=>{if(event.target===quickDialog){const rect=quickDialog.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)quickDialog.close()}})}
    if(!quickDialog.open)quickDialog.showModal();await quick.refresh();if(text)quick.searchText(text);quick.focus();
  }
  window.FramekeepRetrieval={openQuick,settings,async openAsset(id){if(!pageView)return;FramekeepWorkspace.show('retrieval');await pageView.refresh();pageView.similar(id)}};
  if(native){
    quick=shell($('quick-native-shell'),{compact:true,close:()=>invoke?.('search_hide')});quick.refresh().then(()=>quick.focus());
    document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!configDialog.open){event.preventDefault();invoke?.('search_hide')}});
    document.body.addEventListener('click',event=>{if(event.target===document.body)invoke?.('search_hide')});
    window.addEventListener('focus',()=>quick.focus());
    window.FramekeepRetrieval.wake=()=>{quick.refresh().then(()=>quick.focus())};
  }else if(window.FramekeepWorkspace){
    const tab=document.createElement('button');tab.id='asset-retrieval-open';tab.textContent='资产检索';tab.setAttribute('role','tab');tab.setAttribute('aria-controls','asset-retrieval-page');tab.setAttribute('aria-selected','false');tab.tabIndex=-1;
    document.querySelector('.workspace-tabs').insertBefore(tab,$('sku-open'));
    const page=document.createElement('section');page.id='asset-retrieval-page';page.className='asset-workspace hidden';page.setAttribute('role','tabpanel');page.setAttribute('aria-labelledby',tab.id);document.querySelector('.workbench').append(page);pageView=shell(page);
    FramekeepWorkspace.register('retrieval',tab,page,()=>pageView.refresh());
    const archiveButton=document.createElement('button');archiveButton.id='catalog-image-search';archiveButton.textContent='以图搜图';archiveButton.className='bordered';document.querySelector('.catalog-toolbar')?.append(archiveButton);
    archiveButton.onclick=()=>{const id=document.querySelector('#catalog-grid .catalog-card.active')?.dataset.catalogItem;FramekeepWorkspace.show('retrieval');if(id)pageView.similar(id);else pageView.chooseReference()};
    const settingsButton=document.createElement('button');settingsButton.id='intelligence-settings-open';settingsButton.textContent='多模态解析与快捷检索设置';settingsButton.onclick=()=>settings().catch(error=>Framekeep?.notify(error.message));$('model-catalog')?.append(settingsButton);
    document.addEventListener('keydown',event=>{if(event.ctrlKey&&event.shiftKey&&(event.code==='Space'||event.key===' ')){event.preventDefault();event.stopImmediatePropagation();openQuick().catch(error=>console.error(error.message))}},true);
    const original=window.FramekeepRetrieval.openAsset;
    window.FramekeepRetrieval.previewAsset=async id=>{const row=(await api('/api/catalog/items?'+new URLSearchParams({limit:1}))).rows.find(row=>row.id===id);if(row)FramekeepAssetViewer.open('archive',[row],id);else{const item=await api('/api/intelligence/item?'+new URLSearchParams({item:id}));FramekeepAssetViewer.open('archive',[item],id)}};
    document.addEventListener('framekeep:workspace',event=>{if(event.detail.page!=='retrieval')pageView.cancel()});
  }
})();
