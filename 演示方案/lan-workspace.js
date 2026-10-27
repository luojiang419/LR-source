(() => {
  'use strict';
  const $=id=>document.getElementById(id),esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let state=null,current='progress',revision='',pollTimer=null,polling=false,typingTimer=null,progressOffset=0,progressSequence=0;
  let progressView='all',progressKey='',searchTimer=null,searchComposing=false;
  document.documentElement.dataset.lanPage='progress';
  let reference='',referenceItem='',folder='',selected=null,detailSequence=0,facetValues={},facetsSequence=0;
  const contexts={search:{rows:[],cursor:'',controller:null,sequence:0,imageData:null},archive:{rows:[],cursor:'',controller:null,sequence:0,imageData:null}};
  const referencePanel=FramekeepReferenceSearch.mount($('web-reference'),{
    inputId:'reference-file',prepare:(file,signal)=>api('/api/reference',file,signal),
    onChange:value=>{reference=value.image;referenceItem=value.itemId;referenceControls(value.active);contexts.search.imageData=null;assets('search')},
    onRefresh:()=>assets('search'),onThreshold:()=>paintImageResults()
  });
  $('web-reference').querySelector('[data-reference="clear"]').id='clear-reference';
  function message(id,text,error=false){$(id).textContent=text;$(id).classList.toggle('error',error)}
  async function api(path,body,signal){
    const binary=body instanceof Blob;
    const response=await fetch(path,{method:body===undefined?'GET':'POST',credentials:'same-origin',headers:body===undefined?{}:{'Content-Type':binary?'application/octet-stream':'application/json',...(binary?{'X-Reference-Name':encodeURIComponent(body.name||'clipboard.jpg')}:{})},...(body===undefined?{}:{body:binary?body:JSON.stringify(body)}),signal:binary?signal:signal?AbortSignal.any([signal,AbortSignal.timeout(190000)]):AbortSignal.timeout(190000)});
    const type=response.headers.get('content-type')||'';const value=type.includes('application/json')?await response.json():null;
    if(!response.ok){if(response.status===401)showLogin();throw Error(value?.error||'请求失败，请稍后重试')}
    return value;
  }
  function showLogin(){clearTimeout(pollTimer);$('login').hidden=false;$('workspace').hidden=true;state=null;$('asset-detail').close();$('progress-image').close();$('access-code').value=''}
  function options(id,rows,first,label='name'){
    const select=$(id),value=select.value,html=(first===null?'':`<option value="">${esc(first)}</option>`)+rows.map(row=>`<option value="${esc(row.id)}">${esc(row[label]||row.id)}</option>`).join('');
    if(select.innerHTML!==html){select.innerHTML=html;if([...select.options].some(option=>option.value===value))select.value=value}
  }
  async function summary(){
    state=await api('/api/state');$('logout').hidden=state.login_required===false;const priorProject=$('project').value;options('project',state.projects,null);
    if(!priorProject&&state.active_project)$('project').value=state.active_project;
    options('search-root',state.roots,'全部归档');options('archive-root',state.roots,'全部归档');
    options('dimension',Object.entries({...state.dimensions,...Object.fromEntries(state.custom_dimensions.map(key=>[key,key]))}).map(([id,name])=>({id,name})),'常用维度');
    $('search-mode').querySelector('[value=semantic]').disabled=state.visual_backend!=='clip';
  }
  function syncLabel(){const text=new Date().toLocaleTimeString('zh-CN',{hour12:false});$('sync-time').textContent='最近同步 '+text;message('connection','已连接 · 自动同步')}
  async function enter(){
    await summary();revision=state.revision;$('login').hidden=true;$('workspace').hidden=false;
    await load();await loadFacets();syncLabel();schedule();
  }
  function schedule(){clearTimeout(pollTimer);if(state)pollTimer=setTimeout(poll,3000)}
  async function poll(){
    if(!state)return;
    if(document.hidden||polling){schedule();return}
    polling=true;
    try{
      const changes=await api('/api/changes');
      if(changes.revision!==revision){
        const next=changes.revision;
        await summary();await load();if(current==='search')await loadFacets();
        if(selected&&$('asset-detail').open)await detail(selected,false);
        revision=next;
      }
      syncLabel();
    }catch(error){if(state)message('connection','连接中断，正在重连…',true)}
    finally{polling=false;schedule()}
  }
  async function load(){if(current==='progress')return progress();return assets(current)}
  function switchTab(name){
    if(!['progress','search','archive'].includes(name))return;
    current=name;document.documentElement.dataset.lanPage=name;clearTimeout(typingTimer);clearTimeout(searchTimer);contexts.search.queued=false;
    for(const key of ['progress','search','archive']){$('panel-'+key).hidden=key!==name;$('tab-'+key).setAttribute('aria-selected',String(key===name));$('tab-'+key).tabIndex=key===name?0:-1}
    load().catch(error=>message(name==='progress'?'progress-message':name+'-message',error.message,true));
    if(name==='search')loadFacets().catch(()=>{});
  }
  function progressPicture(row,url=row.preview,title=row.sku+' 款号图片'){
    return `<button type="button" class="lan-progress-picture" data-progress-image="${esc(url)}" data-image-title="${esc(title)}" title="查看 ${esc(title)}"><img src="${esc(url)}" alt="${esc(title)}" loading="lazy"><span class="lan-picture-missing" hidden>暂无图片</span></button>`;
  }
  function progressSources(row,statuses=false){
    const sheets=new Map();for(const source of row.sources||[])sheets.set(source.sheet,(sheets.get(source.sheet)||false)||source.shot);
    return [...sheets].map(([sheet,shot])=>`<button type="button" class="lan-progress-source" data-view-sheet="${esc(sheet)}"><span>${esc(sheet)}</span>${statuses?`<span class="${shot?'lan-shot':'lan-pending'}">${shot?'已拍摄':'未拍摄'}</span>`:''}</button>`).join('');
  }
  function paintProgress(data,append,offset){
    options('sheet',[...data.sheets.map(row=>({id:row.sheet,name:row.sheet})),{id:'/common/',name:'公共款号'}],'全部工作表');
    $('sheet').value=data.view==='all'?'':data.view==='common'?'/common/':data.sheet;
    const focused=$('progress-sheets').contains(document.activeElement)?document.activeElement.dataset.viewKey:null;
    const tabs=[{view:'all',key:'all',name:'全部',count:data.all_total},...data.sheets.map(row=>({view:'sheet',key:'sheet:'+row.sheet,sheet:row.sheet,name:row.sheet,count:row.shot+'/'+row.total})),{view:'common',key:'common',name:'公共',count:data.common_total}];
    const tabHtml=tabs.map(tab=>{const active=tab.view===data.view&&(tab.view!=='sheet'||tab.sheet===data.sheet);return `<button type="button" role="tab" data-view="${tab.view}" data-view-key="${esc(tab.key)}" data-sheet="${esc(tab.sheet||'')}" aria-selected="${active}" aria-controls="progress-rows" tabindex="${active?0:-1}">${esc(tab.name)} <span>${tab.count||0}</span></button>`}).join('');
    if($('progress-sheets').innerHTML!==tabHtml){$('progress-sheets').innerHTML=tabHtml;if(focused)[...$('progress-sheets').children].find(button=>button.dataset.viewKey===focused)?.focus({preventScroll:true})}
    for(const key of ['total','shot','pending'])$('count-'+key).textContent=data.counts[key];
    const ratio=data.counts.total?Math.round(data.counts.shot/data.counts.total*100):0;$('progress-ratio').textContent=ratio+'% 完成';$('progress-meter').value=ratio;
    $('progress-project-info').textContent=data.project?`${data.project.name} · 只读查阅，不改变软件端当前拍摄工作表。`:'软件端尚未导入拍摄清单，或尚无拍摄记录。';
    const title=(key,fallback)=>esc(data.headers?.[key]||fallback),query=$('progress-query').value.trim();
    $('progress-search-summary').hidden=!query;$('progress-search-summary').textContent=query?'查找款号：'+query:'';
    const columns=data.columns?.length?data.columns:[{sku:true,title:data.headers?.sku||'款号'}];
    let headings,html;
    const code=row=>`<span class="lan-progress-code">${query?`<span class="lan-progress-memberships">${progressSources(row)}</span>`:''}<strong>${esc(row.sku)}</strong></span>`;
    if(data.view==='sheet'){
      const primary=columns.filter(col=>!col.note),notes=columns.filter(col=>col.note);
      headings=[title('seq','序号'),title('row','原表行'),...primary.map(col=>esc(col.title)),title('status','拍摄状态'),title('photo','款号图片'),title('time','拍摄完成时间'),...notes.map(col=>esc(col.title))];
      const cells=(row,list)=>list.map(col=>{const images=(row.images||[]).filter(image=>image.letter===col.letter);return `<td>${images.length?images.map(image=>progressPicture(row,image.url,row.sku+' 表格图片')).join(''):col.sku?code(row):esc(row.values?.[col.letter]||'')}</td>`}).join('');
      html=data.rows.map((row,index)=>`<tr class="${row.shot?'lan-row-shot':''}" data-progress-row="${esc(row.id)}"><td class="lan-sequence">${offset+index+1}</td><td class="lan-original-row">${row.number}</td>${cells(row,primary)}<td><span class="${row.shot?'lan-shot':'lan-pending'}">${row.shot?'已拍摄':'未拍摄'}</span></td><td>${progressPicture(row)}</td><td class="lan-completed-at">${row.completed_at?(row.completion?`<button type="button" data-progress-image="/api/progress/image?row=${esc(row.id)}&amp;kind=completion&amp;v=${esc(data.revision)}" data-image-title="${esc(row.sku+' · '+row.completed_at+' · 标签照片')}">${esc(row.completed_at.replace('T',' '))}</button>`:esc(row.completed_at.replace('T',' '))):row.shot?'拍摄时间缺失':'—'}</td>${cells(row,notes)}</tr>`).join('');
    }else{
      headings=data.view==='all'?[title('seq','序号'),title('row','原表行'),title('sku','款号'),title('photo','款号图片'),title('source','所属工作表')]:[title('seq','序号'),title('sku','款号'),'所属工作表与拍摄状态',title('photo','款号图片')];
      html=data.rows.map((row,index)=>`<tr class="${row.shot?'lan-row-shot':''}" data-progress-row="${esc(row.id)}"><td class="lan-sequence">${offset+index+1}</td>${data.view==='all'?`<td class="lan-source-rows">${row.sources.map(source=>`<span>${esc(source.sheet)} · ${source.number}</span>`).join('')}</td>`:''}<td><strong>${esc(row.sku)}</strong></td>${data.view==='common'?`<td>${progressSources(row,true)}</td>`:''}<td>${progressPicture(row)}</td>${data.view==='all'?`<td>${progressSources(row,true)}</td>`:''}</tr>`).join('');
    }
    $('progress-head').innerHTML='<tr>'+headings.map(text=>`<th scope="col">${text}</th>`).join('')+'</tr>';
    if(append)$('progress-rows').insertAdjacentHTML('beforeend',html);else $('progress-rows').innerHTML=html||`<tr><td colspan="${headings.length}" class="lan-empty">${data.project?'没有符合条件的款号。':'请先在软件端导入拍摄表格或开始拍摄。'}</td></tr>`;
  }
  async function progress(append=false){
    const sequence=++progressSequence,offset=append?progressOffset:0;
    const query={project:$('project').value,sheet:progressView==='sheet'?$('sheet').value:'',view:progressView,status:$('progress-status').value,q:$('progress-query').value};
    const key=JSON.stringify(query),limit=append||key!==progressKey?60:Math.max(60,progressOffset);
    message('progress-message','正在读取进度…');$('progress-more').hidden=true;
    try{
      const data=await api('/api/progress?'+new URLSearchParams({...query,offset,limit}));
      if(sequence!==progressSequence)return;
      paintProgress(data,append,offset);progressKey=key;
      progressOffset=offset+data.rows.length;$('progress-more').hidden=!data.has_more;message('progress-message',data.project?`已显示 ${progressOffset} 行 · 自动同步`:'暂无拍摄任务');
    }catch(error){if(sequence===progressSequence)message('progress-message',error.message,true)}
  }
  function cards(name){
    const rows=contexts[name].rows;
    $(name+'-grid').innerHTML=rows.map((row,index)=>`<button type="button" class="lan-card" role="listitem" data-item="${index}" data-list="${name}" title="${esc(row.name)}"><img src="${esc(row.preview)}&size=420" alt="${esc(row.name)}" loading="lazy" decoding="async"><strong>${esc(row.sku||row.name)}</strong>${Number.isFinite(row.score)?`<span class="reference-score">相似度 ${Math.min(100,Math.max(0,row.score*100)).toFixed(1)}%</span>`:''}<small>${esc(row.sku?row.name:row.media_format)}</small><small>${esc(row.reasons?.join(' · ')||row.folder||'归档素材')}</small></button>`).join('')||'<div class="lan-empty">'+(state.roots.length?'未找到匹配资产，试试更短的 SKU、关键词或其他目录。':'工作站尚未添加归档位置。')+'</div>';
    if(name==='search'&&contexts.search.imageData&&!rows.length)$(name+'-grid').innerHTML='<div class="lan-empty">没有达到当前相似度的素材。<br>降低最低相似度，或替换参考图后重试。</div>';
  }
  async function assets(name,append=false){
    const context=contexts[name];
    if(name==='search'){
      clearTimeout(searchTimer);
      const key=JSON.stringify([$('search-query').value,$('search-mode').value,$('search-root').value,$('search-media').value,facetValues,reference,referenceItem,append?context.cursor:'']);
      if(context.loading){
        if(key!==context.activeKey||context.sequence!==context.activeSequence){++context.sequence;context.queued=true}
        return;
      }
      context.loading=true;context.activeKey=key;
    }
    const sequence=++context.sequence;if(name==='search')context.activeSequence=sequence;context.controller?.abort();context.controller=new AbortController();
    clearTimeout(typingTimer);if(name==='search')referencePanel.setBusy(!!(reference||referenceItem));message(name+'-message','正在检索…');$(name+'-more').hidden=true;
    try{
      let data;
      if(name==='search'&&(reference||referenceItem||($('search-mode').value==='semantic'&&$('search-query').value.trim()))){
        data=await api('/api/image',{...(referenceItem?{item_id:referenceItem}:reference?{image:reference}:{text:$('search-query').value}),root:$('search-root').value,threshold:0,limit:100},context.controller.signal);
      }else{
        const query=name==='search'?{q:$('search-query').value,root:$('search-root').value,media:$('search-media').value,facets:JSON.stringify(facetValues)}:{q:$('archive-query').value,root:$('archive-root').value,media:$('archive-media').value,folder};
        data=await api('/api/'+name+'?'+new URLSearchParams({...query,cursor:append?context.cursor:''}),undefined,context.controller.signal);
      }
      if(sequence!==context.sequence)return;
      if(name==='search'&&(reference||referenceItem)){context.imageData=data;paintImageResults();return}
      context.imageData=null;context.rows=append?[...context.rows,...data.rows]:data.rows;context.cursor=data.next_cursor||'';cards(name);$(name+'-more').hidden=!data.has_more;
      message(name+'-message',`${context.rows.length} 项${data.has_more?' · 还有更多':''}${data.elapsed_ms!==undefined?' · '+data.elapsed_ms+' ms':''}${data.approximate?' · 近似相似图召回':''}${data.incomplete?' · 已达查询预算，可缩小范围':''}`);
      if(name==='archive'){
        const parts=folder.split('/').filter(Boolean);$('breadcrumbs').innerHTML='<button type="button" data-folder="">全部目录</button>'+parts.map((part,index)=>`<button type="button" data-folder="${esc(parts.slice(0,index+1).join('/'))}">${esc(part)}</button>`).join('');
        $('archive-folders').innerHTML=(data.folders||[]).map(item=>`<button type="button" data-folder="${esc(item.folder)}">▸ ${esc(item.name)}</button>`).join('')+(data.folders_truncated?'<span>目录较多，请缩小位置继续查找。</span>':'');
      }
    }catch(error){if(sequence===context.sequence&&error.name!=='AbortError')message(name+'-message',error.message,true)}finally{
      if(name==='search'){
        context.loading=false;
        if(context.queued){context.queued=false;assets('search')}
        else if(sequence===context.sequence)referencePanel.setBusy(false);
      }
    }
  }
  async function loadFacets(){
    if(!state)return;const sequence=++facetsSequence;
    const data=await api('/api/facets?'+new URLSearchParams({key:$('dimension').value}));if(sequence!==facetsSequence)return;
    $('facets').innerHTML=data.rows.map(row=>`<button type="button" data-facet-key="${esc(row.key)}" data-facet-value="${esc(row.value)}" aria-pressed="${facetValues[row.key]===row.value}">${esc(row.value)}</button>`).join('')||'<span>暂无已解析维度</span>';
  }
  async function detail(item,show=true){
    const sequence=++detailSequence;selected=item;$('detail-title').textContent=item.sku||item.name;$('detail-status').textContent='正在读取资料…';
    if(show){$('detail-content').textContent='';$('detail-image').src='/api/preview?'+new URLSearchParams({item:item.id,size:2048});if(!$('asset-detail').open)$('asset-detail').showModal()}
    try{
      const data=await api('/api/details?'+new URLSearchParams({item:item.id}));if(sequence!==detailSequence||!$('asset-detail').open)return;
      const row=data.item;selected=row;$('detail-title').textContent=row.sku||row.name;$('detail-sku').disabled=!row.sku;
      const capture=Object.entries(data.capture).filter(([,value])=>typeof value==='string'||typeof value==='number');
      $('detail-content').innerHTML=`<h3>${esc(row.name)}</h3><p>${esc(row.folder||'/')} · ${esc(row.media_format)} · ${esc(row.media_type==='video'?'视频封面':'图片资料')}</p><dl><dt>SKU</dt><dd>${esc(row.sku||'待确认')}</dd><dt>尺寸</dt><dd>${row.width&&row.height?esc(row.width+' × '+row.height):'未读取'}</dd></dl>${data.parsed?data.frames.map((frame,index)=>`<section><h4>${row.media_type==='video'?'采样帧 '+(index+1)+' · '+frame.seconds+'s':'多维资料'}</h4><p>${esc(frame.result.summary)}</p><dl>${Object.entries({...frame.result.attributes,...frame.result.custom}).filter(([,value])=>value).map(([key,value])=>`<dt>${esc(state.dimensions[key]||key)}</dt><dd>${esc(value)}</dd>`).join('')}</dl>${frame.result.visible_text?'<h4>可见文字</h4><p>'+esc(frame.result.visible_text)+'</p>':''}</section>`).join(''):'<p>工作站尚未完成此素材的多维解析。</p>'}${capture.length?'<h4>拍摄资料</h4><dl>'+capture.map(([key,value])=>'<dt>'+esc(key)+'</dt><dd>'+esc(value)+'</dd>').join('')+'</dl>':''}`;
      $('detail-status').textContent=data.stale?'源文件已变化，当前显示上次资料。':'';
    }catch(error){if(sequence===detailSequence)$('detail-status').textContent=error.message}
  }
  function closeDetail(){$('asset-detail').close();detailSequence++;selected=null;$('detail-image').removeAttribute('src')}
  function paintImageResults(){
    const context=contexts.search,data=context.imageData;if(!data)return;
    context.rows=referencePanel.filter(data.rows);context.cursor='';cards('search');$('search-more').hidden=true;
    message('search-message',`${context.rows.length} / ${data.rows.length} 项候选 · 相似度 ≥ ${referencePanel.value.threshold}% · ${data.backend?.startsWith('chinese-clip')?'CLIP 语义相似':'布局与配色相似'}${data.approximate?' · 近似召回':''}${data.incomplete?' · 已达查询预算':''}`);
  }
  async function upload(file){await referencePanel.upload(file)}
  function resetReference(){reference='';referenceItem='';referencePanel.clear(false);contexts.search.imageData=null;referenceControls(false)}
  function referenceControls(active){for(const id of ['search-query','search-mode','search-media','dimension'])$(id).disabled=active;$('facets').inert=active}
  const settingsRoot=$('lan-settings'),settingsTrigger=$('lan-settings-trigger'),settingsMenu=$('lan-settings-menu');
  function openSettings(){settingsMenu.hidden=false;settingsTrigger.setAttribute('aria-expanded','true')}
  function closeSettings(focus=false){settingsMenu.hidden=true;settingsTrigger.setAttribute('aria-expanded','false');if(focus)settingsTrigger.focus()}
  settingsRoot.addEventListener('pointerenter',event=>{if(event.pointerType!=='touch')openSettings()});
  settingsRoot.addEventListener('pointerleave',()=>{if(!settingsRoot.contains(document.activeElement))closeSettings()});
  settingsRoot.addEventListener('focusout',()=>queueMicrotask(()=>{if(!settingsRoot.contains(document.activeElement))closeSettings()}));
  settingsTrigger.onclick=event=>{if(event.detail>0&&event.pointerType!=='touch'){openSettings();return}if(settingsMenu.hidden)openSettings();else closeSettings()};
  settingsTrigger.addEventListener('keydown',event=>{if(['ArrowDown','ArrowUp'].includes(event.key)){event.preventDefault();openSettings();const buttons=[...settingsMenu.querySelectorAll('button')].filter(button=>!button.hidden);buttons[event.key==='ArrowDown'?0:buttons.length-1].focus()}});
  settingsMenu.addEventListener('keydown',event=>{
    if(!['ArrowDown','ArrowUp','Home','End'].includes(event.key))return;event.preventDefault();
    const buttons=[...settingsMenu.querySelectorAll('button')].filter(button=>!button.hidden),index=buttons.indexOf(document.activeElement);
    buttons[event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(event.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length].focus();
  });
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!settingsMenu.hidden){event.preventDefault();event.stopPropagation();closeSettings(true)}});
  document.addEventListener('pointerdown',event=>{if(!settingsRoot.contains(event.target))closeSettings()});
  $('login-form').onsubmit=async event=>{event.preventDefault();$('login-submit').disabled=true;try{await api('/api/login',{code:$('access-code').value});await enter()}catch(error){message('login-message',error.message,true)}finally{$('login-submit').disabled=false}};
  $('logout').onclick=async()=>{closeSettings();try{await api('/api/logout',{})}finally{showLogin()}};
  $('refresh').onclick=async()=>{closeSettings();try{await summary();await load();syncLabel()}catch(error){message('connection',error.message,true)}};
  $('theme-cycle').onclick=()=>{closeSettings();const modes=['dark','light','pure-white','hardcore'];FramekeepTheme.set(modes[(modes.indexOf(FramekeepTheme.get())+1)%modes.length])};
  for(const name of ['progress','search','archive'])$('tab-'+name).onclick=()=>switchTab(name);
  document.querySelector('.lan-tabs').onkeydown=event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const names=['progress','search','archive'],index=names.indexOf(current);switchTab(names[event.key==='Home'?0:event.key==='End'?2:(index+(event.key==='ArrowLeft'?-1:1)+3)%3]);$('tab-'+current).focus()};
  function chooseProgress(view,sheet=''){
    progressView=view;$('sheet').value=view==='common'?'/common/':sheet;progressOffset=0;clearTimeout(typingTimer);progress();
  }
  $('project').onchange=()=>chooseProgress('all');
  $('sheet').onchange=()=>chooseProgress($('sheet').value==='/common/'?'common':$('sheet').value?'sheet':'all',$('sheet').value);
  $('progress-status').onchange=()=>{progressOffset=0;progress()};
  $('progress-query').oninput=()=>{++progressSequence;progressOffset=0;clearTimeout(typingTimer);typingTimer=setTimeout(()=>progress(),250)};
  $('progress-more').onclick=()=>progress(true);
  $('progress-sheets').onclick=event=>{const button=event.target.closest('[data-view]');if(button)chooseProgress(button.dataset.view,button.dataset.sheet)};
  $('progress-sheets').onkeydown=event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const buttons=[...$('progress-sheets').children],index=buttons.indexOf(document.activeElement),next=buttons[event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(event.key==='ArrowLeft'?-1:1)+buttons.length)%buttons.length];buttons.forEach(button=>button.tabIndex=button===next?0:-1);next?.focus()};
  const progressNarrow=matchMedia('(max-width:760px)'),progressWorkspace=document.querySelector('.lan-progress-workspace');
  function progressSidebar(open){progressWorkspace.classList.toggle('sidebar-open',open);const visible=open||!progressNarrow.matches;$('progress-sidebar').inert=!visible;$('progress-sidebar-toggle').setAttribute('aria-expanded',String(visible))}
  $('progress-sidebar-toggle').onclick=()=>progressSidebar(!progressWorkspace.classList.contains('sidebar-open'));
  $('progress-sidebar-close').onclick=()=>{progressSidebar(false);$('progress-sidebar-toggle').focus()};
  progressNarrow.addEventListener('change',()=>progressSidebar(false));progressSidebar(false);
  let progressImageOrigin=null;
  $('progress-image-close').onclick=()=>$('progress-image').close();
  $('progress-image').addEventListener('close',()=>{if(progressImageOrigin?.isConnected)progressImageOrigin.focus({preventScroll:true});progressImageOrigin=null;$('progress-image-content').removeAttribute('src')});
  $('progress-image-content').onload=()=>{$('progress-image-status').textContent=''};
  $('progress-image-content').onerror=()=>{$('progress-image-status').textContent='图片暂不可用，请检查工作站资源是否在线。'};
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&progressNarrow.matches&&progressWorkspace.classList.contains('sidebar-open')&&!document.querySelector('dialog[open]')){event.preventDefault();$('progress-sidebar-close').click()}});
  $('progress-rows').onclick=event=>{
    const sheet=event.target.closest('[data-view-sheet]');if(sheet){chooseProgress('sheet',sheet.dataset.viewSheet);return}
    const image=event.target.closest('[data-progress-image]');if(!image)return;progressImageOrigin=image;$('progress-image-title').textContent=image.dataset.imageTitle;$('progress-image-status').textContent='正在加载图片…';$('progress-image-content').hidden=false;$('progress-image-content').alt=image.dataset.imageTitle;$('progress-image').showModal();$('progress-image-content').src=image.dataset.progressImage;
  };
  function scheduleSearch(){
    clearTimeout(searchTimer);contexts.search.queued=false;++contexts.search.sequence;$('search-more').hidden=true;
    if(!searchComposing)searchTimer=setTimeout(()=>assets('search'),250);
  }
  $('search-query').oninput=event=>{if(event.isComposing){clearTimeout(searchTimer);return}scheduleSearch()};
  $('search-query').addEventListener('compositionstart',()=>{searchComposing=true;scheduleSearch()});
  $('search-query').addEventListener('compositionend',()=>{searchComposing=false;scheduleSearch()});
  $('search-query').onkeydown=event=>{if(event.key==='Enter'&&(searchComposing||event.isComposing||event.keyCode===229))event.preventDefault()};
  $('search-form').onsubmit=event=>{event.preventDefault();if(!searchComposing)assets('search')};$('archive-form').onsubmit=event=>{event.preventDefault();assets('archive')};
  for(const name of ['search','archive']){$(name+'-more').onclick=()=>assets(name,true);$(name+'-grid').onclick=event=>{const button=event.target.closest('[data-item]');if(button)detail(contexts[name].rows[Number(button.dataset.item)])}}
  for(const id of ['search-root','search-media','search-mode'])$(id).onchange=()=>assets('search');
  for(const id of ['archive-root','archive-media'])$(id).onchange=()=>{folder='';assets('archive')};
  for(const id of ['breadcrumbs','archive-folders'])$(id).onclick=event=>{const button=event.target.closest('[data-folder]');if(button){folder=button.dataset.folder;assets('archive')}};
  $('dimension').onchange=()=>loadFacets().catch(error=>message('search-message',error.message,true));
  $('facets').onclick=event=>{const button=event.target.closest('[data-facet-key]');if(!button)return;const key=button.dataset.facetKey,value=button.dataset.facetValue;if(facetValues[key]===value)delete facetValues[key];else facetValues[key]=value;$('clear-facets').hidden=!Object.keys(facetValues).length;loadFacets();assets('search')};
  $('clear-facets').onclick=()=>{facetValues={};$('clear-facets').hidden=true;loadFacets();assets('search')};
  $('panel-search').onpaste=event=>{if(event.defaultPrevented)return;const file=[...(event.clipboardData?.items||[])].find(item=>item.type.startsWith('image/'))?.getAsFile();if(file){event.preventDefault();upload(file).catch(error=>message('search-message',error.message,true))}};
  $('panel-search').ondragover=event=>{if(event.dataTransfer?.types.includes('Files'))event.preventDefault()};$('panel-search').ondrop=event=>{if(event.defaultPrevented)return;if(event.dataTransfer?.files.length){event.preventDefault();upload(event.dataTransfer.files[0]).catch(error=>message('search-message',error.message,true))}};
  $('detail-close').onclick=closeDetail;$('asset-detail').addEventListener('cancel',()=>{detailSequence++;selected=null});
  $('detail-similar').onclick=()=>{const row=selected;closeDetail();reference='';referenceItem=row.id;referencePanel.set({itemId:row.id,src:row.preview,label:row.name},false);contexts.search.imageData=null;referenceControls(true);switchTab('search')};
  $('detail-sku').onclick=()=>{const sku=selected?.sku;if(!sku)return;closeDetail();resetReference();$('search-query').value=sku;switchTab('search')};
  document.addEventListener('error',event=>{if(event.target instanceof HTMLImageElement){event.target.hidden=true;if(event.target.closest('.lan-progress-picture'))event.target.closest('.lan-progress-picture').querySelector('.lan-picture-missing').hidden=false;if(event.target.id==='detail-image')$('detail-status').textContent='预览暂不可用，仍可查阅已有资料。'}},true);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden&&state)poll()});
  $('login').hidden=true;
  enter().catch(error=>{showLogin();if(!error.message.includes('访问码'))message('login-message','请使用工作站提供的访问码登录。')});
})();
