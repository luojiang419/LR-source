(() => {
  'use strict';
  if(!window.FramekeepIO)return;
  const $=id=>document.getElementById(id),api=FramekeepIO.api,esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const panel=document.createElement('details');panel.className='side-section asset-panel';panel.open=true;
  panel.innerHTML='<summary>资产备案 <span id="asset-count"></span></summary><div class="side-body asset-panel-body"><p class="dialog-copy">Shift 连选 · Ctrl 加选，与照片星级独立</p><label>SKU 款号<input id="asset-sku" maxlength="80" placeholder="识别后自动填入，也可批量修改"></label><button id="asset-preview" class="primary">预览批量写入</button><p id="asset-message" role="status" class="dialog-copy"></p></div>';
  document.querySelector('.right-panel').append(panel);
  const ocrControls=document.createElement('div');ocrControls.className='asset-ocr-controls';ocrControls.innerHTML='<button id="asset-parse-sku" class="bordered" title="使用已选 OCR 模型读取当前图，不要求二星">OCR 解析当前图</button><div id="asset-ocr-result" class="asset-ocr-result" hidden><div id="asset-ocr-codes" class="asset-actions"></div><details><summary>OCR 原文</summary><pre id="asset-ocr-text"></pre></details></div>';$('asset-sku').parentElement.after(ocrControls);
  const settingsButton=document.createElement('button');settingsButton.id='app-settings-open';settingsButton.textContent='设置';settingsButton.setAttribute('role','tab');settingsButton.setAttribute('aria-controls','app-settings-page');settingsButton.setAttribute('aria-selected','false');settingsButton.tabIndex=-1;document.querySelector('.workspace-tabs').append(settingsButton);
  const settings=document.createElement('section');settings.id='app-settings-page';settings.className='asset-workspace hidden';settings.setAttribute('role','tabpanel');settings.setAttribute('aria-labelledby','app-settings-open');
  settings.innerHTML=`<div class="asset-workspace-heading"><div><h2 id="app-settings-title">设置</h2><p>按功能管理模型、设备与拍摄工作流。</p></div><button id="app-settings-close" class="bordered">返回工作台</button></div><p id="app-settings-message" role="status" class="workspace-caption"></p>
  <div class="app-settings-layout"><nav class="app-settings-nav" aria-label="设置分类">
    <button type="button" data-settings-section="models" aria-controls="settings-models" aria-current="location"><span>01</span>AI 与模型</button>
    <button type="button" data-settings-section="remote" aria-controls="settings-remote"><span>02</span>鼠标遥控</button>
    <button type="button" data-settings-section="files" aria-controls="settings-files"><span>03</span>文件与备案</button>
    <button type="button" data-settings-section="streams" aria-controls="settings-streams"><span>04</span>SKU 自动区间</button>
  </nav><div class="app-settings-grid">
  <section id="settings-models" class="app-settings-section" aria-labelledby="settings-models-title"><div class="settings-section-heading"><h3 id="settings-models-title">AI 与模型</h3><p>管理识别能力与模型服务连接。</p></div>
    <div id="full-auto-ocr-card" class="mouse-auto-card"><div class="mouse-auto-heading"><div><span class="mouse-auto-eyebrow">逐图识别与入库</span><h4 id="full-auto-ocr-title">全自动 OCR 模式</h4></div><input id="full-auto-ocr" class="toggle-switch" type="checkbox" role="switch" aria-labelledby="full-auto-ocr-title" aria-describedby="full-auto-ocr-help" disabled></div><p id="full-auto-ocr-help">导入 SKU 款号文档并绑定当前目录后可启用。</p><div class="mouse-auto-footer"><span id="full-auto-ocr-status" role="status">正在读取配置…</span><button id="full-auto-open-plan" class="bordered">管理拍摄文档</button></div></div>
    <div class="workspace-card settings-service-card"><div class="settings-service-copy"><h4>AI 助手配置</h4><p>分别配置 OCR、多模态与文本模型，管理接口、密钥和当前选用的模型。</p><div class="settings-category-tags"><span>OCR</span><span>多模态</span><span>文本</span></div></div><div id="settings-models-action" class="settings-service-action"></div></div>
  </section>
  <section id="settings-remote" class="app-settings-section" aria-labelledby="settings-remote-title"><div class="settings-section-heading"><h3 id="settings-remote-title">鼠标遥控</h3><p>将无线鼠标用作拍摄现场的选片遥控器。</p></div>
    <div class="workspace-card settings-service-card"><div class="settings-service-copy"><h4>设备与按键功能</h4><p>选择接收器、启停遥控，设置右键减星与滚轮浏览。开启后自动返回选片页。</p><p class="settings-service-hint">紧急恢复：Ctrl + Alt + F11</p></div><div id="settings-remote-action" class="settings-service-action"></div></div>
  </section>
  <section id="settings-files" class="app-settings-section" aria-labelledby="settings-files-title"><div class="settings-section-heading"><h3 id="settings-files-title">文件与备案</h3><p>确认本次拍摄上下文，设置元数据写入及已有归属的处理方式。</p></div>
    <div class="settings-file-grid"><section class="workspace-card settings-context-card"><header><h4>当前拍摄</h4></header><div class="workspace-fields"><p id="settings-current-context" class="workspace-caption"></p><button id="settings-current-shooting" class="bordered">切换当前拍摄</button></div></section>
    <section class="workspace-card"><header><h4>元数据与归属</h4></header><div class="workspace-fields"><label>文件策略<select id="asset-mode"><option value="embedded">RAW + JPG 都写内部元数据</option></select></label><label class="watch-option"><input id="asset-overwrite" type="checkbox">明确覆盖已有不同归属</label><button id="asset-read" class="bordered">读取所选备案</button></div></section></div>
  </section>
  <section id="settings-streams" class="app-settings-section" aria-labelledby="settings-streams-title"><div class="settings-section-heading"><h3 id="settings-streams-title">SKU 自动区间</h3><p>按当前拍摄目录接续区间，或使用指定款号手工封口。</p></div>
    <div class="workspace-card"><div class="workspace-fields settings-stream-fields"><div class="settings-stream-start"><h4>接续当前目录</h4><p>继续本次拍摄的自动归组流程。</p><button id="asset-start" class="bordered">接续自动区间</button></div><div class="settings-stream-close"><label>手工封口款号<input id="asset-stream-sku" maxlength="80" placeholder="填写 SKU，或沿用选片页的款号"></label><button id="asset-close-stream" class="bordered">按所填 SKU 手工结束</button></div><p id="asset-stream-status" class="dialog-copy"></p></div></div>
  </section></div></div>`;
  document.querySelector('.workbench > .statusbar').before(settings);
  $('settings-models-action').append($('assistant-settings'));
  $('settings-remote-action').append($('mouse-remote'));
  $('settings-remote').querySelector('.settings-section-heading').after($('mouse-auto-card'));
  let fullAutoState=null,fullAutoSaving=false,fullAutoRequest=0,fullAutoDesired=false;
  function autoMessage(text){for(const id of ['full-auto-ocr-status','shooting-auto-status'])$(id).textContent=text}
  function paintFullAuto(state){
    fullAutoState=state;const bound=state.session_id===FRAMEKEEP_SESSION.id,busy=window.FramekeepSku?.isBusy();
    for(const id of ['full-auto-ocr','shooting-full-auto']){$(id).checked=fullAutoSaving?fullAutoDesired:state.enabled===true;$(id).disabled=fullAutoSaving||busy||(!state.can_enable||!bound)&&!state.enabled}
    for(const id of ['sku-import','shooting-import','sku-projects','sku-live-scope','sku-panel-scope','sku-bind-current','sku-auto'])$(id).disabled=fullAutoSaving||busy||(id==='sku-auto'&&!state.project_id);
    $('full-auto-ocr-card').classList.toggle('is-enabled',state.enabled===true);
    $('shooting-automation').classList.toggle('is-enabled',state.enabled===true);
    $('full-auto-ocr-help').textContent=state.reason||(!bound?'请先将拍摄文档绑定当前照片目录。':`${state.document} · ${state.scope}。逐张扫描已有和后续图片，精确或唯一高相似度匹配后自动入库；候选冲突和已有归属保留复核，照片星级保持。`);
    const counts=state.counts||{},done=(counts.matched||0)+(counts.confirmed||0),pending=(counts.queued||0)+(counts.running||0)+(counts.waiting_model||0);
    $('full-auto-ocr-status').textContent=state.enabled?`${state.paused?'队列已暂停':'全自动已开启'} · 已入库 ${done} · 待扫描 ${pending} · 待复核 ${counts.review||0} · 失败 ${counts.failed||0}`:'全自动已关闭 · 导入文档后可开启';
    $('shooting-auto-status').textContent=state.enabled?`${state.paused?'队列已暂停':'全自动已开启'} · 已入库 ${done} · 待扫描 ${pending} · 待复核 ${counts.review||0} · 失败 ${counts.failed||0}`:state.reason||(!bound?'请先绑定当前照片目录':'全自动已关闭 · 开启后逐图识别并入库');
    if(fullAutoSaving)autoMessage('正在检查模型并切换全自动 OCR…');
  }
  async function loadFullAuto(){
    if(fullAutoState)paintFullAuto(fullAutoState);if(fullAutoSaving||window.FramekeepSku?.isBusy())return;
    const request=++fullAutoRequest;try{const next=await api('/api/sku/full-auto');if(request===fullAutoRequest)paintFullAuto(next)}catch(error){if(request===fullAutoRequest)autoMessage(error.message)}
  }
  async function changeFullAuto(event){
    if(fullAutoSaving||window.FramekeepSku?.isBusy()){if(fullAutoState)paintFullAuto(fullAutoState);return}
    const enabled=event.target.checked,context={project_id:fullAutoState?.project_id,scope:fullAutoState?.scope,session_id:FRAMEKEEP_SESSION.id};
    fullAutoDesired=enabled;fullAutoSaving=true;++fullAutoRequest;if(fullAutoState)paintFullAuto(fullAutoState);let failure='';
    try{paintFullAuto(await api('/api/sku/full-auto',{enabled,...context}));document.dispatchEvent(new CustomEvent('framekeep:full-auto-ocr'))}
    catch(error){failure=error.message;Framekeep.notify(failure)}
    finally{fullAutoSaving=false;await loadFullAuto();if(failure)autoMessage(failure)}
  }
  for(const id of ['full-auto-ocr','shooting-full-auto'])$(id).onchange=changeFullAuto;
  window.FramekeepAutomation={refresh:loadFullAuto,isSaving:()=>fullAutoSaving};
  $('full-auto-open-plan').onclick=()=>FramekeepWorkspace.show('progress');
  for(const name of ['framekeep:workspace','framekeep:sku-project','framekeep:sku-context'])document.addEventListener(name,loadFullAuto);
  const fullAutoTimer=setInterval(()=>{if(!document.hidden&&(!settings.classList.contains('hidden')||!$('selection-page').classList.contains('hidden')))loadFullAuto()},2000);
  window.addEventListener('pagehide',()=>clearInterval(fullAutoTimer),{once:true});loadFullAuto();
  const settingsScroll=settings.querySelector('.app-settings-grid'),categoryButtons=[...settings.querySelectorAll('[data-settings-section]')];
  function markSettingsCategory(id){for(const button of categoryButtons){if(button.dataset.settingsSection===id)button.setAttribute('aria-current','location');else button.removeAttribute('aria-current')}}
  for(const button of categoryButtons)button.addEventListener('click',()=>{const target=$('settings-'+button.dataset.settingsSection);settingsScroll.scrollTo({top:settingsScroll.scrollTop+target.getBoundingClientRect().top-settingsScroll.getBoundingClientRect().top,behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});markSettingsCategory(button.dataset.settingsSection)});
  settingsScroll.addEventListener('scroll',()=>{const top=settingsScroll.getBoundingClientRect().top+24;let current='models';for(const button of categoryButtons){if($('settings-'+button.dataset.settingsSection).getBoundingClientRect().top<=top)current=button.dataset.settingsSection}if(settingsScroll.scrollTop>0&&settingsScroll.scrollTop+settingsScroll.clientHeight>=settingsScroll.scrollHeight-2)current='streams';markSettingsCategory(current)},{passive:true});
  let settingsReturn='selection';
  document.addEventListener('framekeep:workspace',event=>{if(event.detail.page!=='settings')settingsReturn=event.detail.page});
  function settingsContext(){const type=window.FramekeepSku?.currentType()||'尚未选择';$('settings-current-context').textContent=`当前拍摄：${type} · 所选 ${selection().length} 张\n${FRAMEKEEP_SESSION.root||'尚未打开照片目录'}`}
  function syncShootingTypes(){previewId=null;if(preview.open)preview.close();settingsContext()}
  document.addEventListener('framekeep:sku-project',syncShootingTypes);
  document.addEventListener('framekeep:shooting-type',syncShootingTypes);
  $('app-settings-close').onclick=()=>FramekeepWorkspace.show(settingsReturn);
  $('settings-current-shooting').onclick=()=>{FramekeepWorkspace.show('selection');window.FramekeepSidebar?.show('shooting')};
  const preview=document.createElement('dialog');preview.id='asset-preview-dialog';preview.innerHTML='<div class="dialog-heading"><h2>批量备案变更预览</h2><button data-close>×</button></div><div id="asset-preview-content"></div><button id="asset-apply" class="primary">确认备案并写入 RAW / JPG</button>';document.body.append(preview);
  const archiveTab=document.createElement('button');archiveTab.id='asset-archive-open';archiveTab.textContent='资产归档';archiveTab.setAttribute('role','tab');archiveTab.setAttribute('aria-selected','false');archiveTab.setAttribute('aria-controls','asset-archive-dialog');archiveTab.tabIndex=-1;document.querySelector('.workspace-tabs').append(archiveTab);
  const archive=document.createElement('section');archive.id='asset-archive-dialog';archive.className='asset-workspace hidden';archive.setAttribute('role','tabpanel');archive.setAttribute('aria-labelledby',archiveTab.id);
  archive.innerHTML=`<div class="asset-workspace-heading"><div><h2>资产归档</h2><p>按款号整理拍摄资产，追踪文件位置与元数据写入。</p></div><button id="archive-export" class="bordered">导出当前页 JSON</button></div>
  <div class="asset-workspace-grid"><main class="asset-workspace-main">
    <div class="workspace-metrics"><div><span>本页资产</span><strong id="archive-total">0</strong></div><div><span>最近区间</span><strong id="archive-sealed">0</strong></div><div><span>迟到待确认</span><strong id="archive-late">0</strong></div><div><span>最近写入批次</span><strong id="archive-batches">0</strong></div></div>
    <section class="workspace-card workspace-result-card"><header><h3>资产清单</h3><span id="archive-page" class="workspace-caption"></span></header><div id="archive-results" class="sku-table-wrap"></div><footer class="workspace-pagination"><button id="archive-prev">上一页</button><button id="archive-next">下一页</button></footer></section>
    <section id="archive-inspector" class="workspace-card" hidden><header><h3>资产与写入详情</h3><button id="archive-detail-close" aria-label="收起详情">×</button></header><div id="archive-detail-summary" class="workspace-detail"></div><details class="workspace-raw"><summary>完整记录</summary><pre id="archive-detail"></pre></details></section>
  </main><aside class="asset-workspace-sidebar" aria-label="资产检索与归档工具">
    <section class="workspace-card"><header><h3>检索资产</h3><span class="workspace-caption">SKU / 类型</span></header><div class="workspace-fields"><label>SKU 款号<input id="archive-sku" type="search" placeholder="输入款号查找"></label><div class="workspace-field-pair"><label>匹配方式<select id="archive-match"><option value="prefix">款号前缀</option><option value="exact">精确款号</option></select></label><label>拍摄类型<input id="archive-scope" placeholder="完整类型名称"></label></div><details><summary>项目筛选</summary><label>项目 ID<input id="archive-project" placeholder="可选"></label></details><button id="archive-search" class="primary">查询资产</button><p id="archive-message" role="status"></p></div></section>
    <details class="workspace-card"><summary>文件定位与重新归档</summary><div class="workspace-fields"><p id="everything-status" class="dialog-copy">正在检查文件搜索服务…</p><label>文件名或路径<input id="file-query" placeholder="查找已索引的图片位置"></label><div class="asset-actions"><button id="file-search">查找文件</button><button id="file-next" disabled>更多文件</button></div><p id="file-search-message" role="status"></p><div id="file-search-results" class="sku-table-wrap"></div><label>移动后的目录<input id="archive-folder" placeholder="粘贴目录完整路径"></label><button id="archive-relocate">扫描并重新定位</button></div></details>
    <details class="workspace-card" open><summary>SKU 区间</summary><div id="archive-segments" class="workspace-activity"></div></details>
    <details class="workspace-card"><summary>元数据写入批次</summary><div id="archive-operations" class="workspace-activity"></div></details>
  </aside></div>`;document.querySelector('.workbench > .statusbar').before(archive);
  $('archive-detail-close').onclick=()=>{$('archive-inspector').hidden=true};
  const visible=()=>window.FramekeepWorkspace?.current()==='archive';
  const statusName=value=>({confirmed:'已确认',waiting_sku:'等待款号',needs_review:'待复核',cancelled:'已撤销',ready:'待写入',queued:'排队中',running:'写入中',completed:'已完成',finished:'已结束',applied:'已写入',failed:'失败',partial:'部分完成',assign:'备案写入',revert:'撤销写入',online:'在线',product:'产品图',sku_label:'款号标签'}[value]||value);
  function empty(title,copy){return `<div class="workspace-empty"><strong>${title}</strong><p>${copy}</p></div>`}
  function detail(data){
    $('archive-inspector').hidden=false;$('archive-detail').textContent=JSON.stringify(data,null,2);
    $('archive-detail-summary').innerHTML=data.sku!==undefined?`<strong>${esc(data.sku||'待备案')}</strong><p>${esc(data.scope||'未填写类型')} · ${esc(statusName(data.role))}</p>${(data.files||[]).map(f=>`<p>${esc(f.path)}<br><small>${esc(f.component)}</small></p>`).join('')}`:`<strong>${esc(statusName(data.operation?.status||data.status||''))}</strong><p>${(data.jobs||[]).length} 个文件任务</p>${(data.jobs||[]).map(j=>`<p>${esc(statusName(j.status))} · ${esc(j.path)}${j.reason?'<br>'+esc(j.reason):''}</p>`).join('')}`;
  }
  let selected=[],anchor=null,lastFocus=null,lastContext='',previewId=null,operation=null,stream=null,busy=false,queryRows=[];
  let skuEdited=false,selectionRead='',archiveVersion='',readSequence=0,skuEditVersion=0,parsing=false,ocrContext='';
  $('asset-sku').addEventListener('input',()=>{skuEdited=true;skuEditVersion++;previewId=null});
  syncShootingTypes();
  let cursors=[''],pageIndex=0,nextCursor='',fileOffset=0,fileText='';
  function resetPage(){cursors=[''];pageIndex=0;nextCursor=''}
  const msg=text=>{$('asset-message').textContent=text;$('app-settings-message').textContent=text};
  async function act(fn){if(busy)return;busy=true;try{await fn()}catch(e){msg(e.message);const target=$('archive-message');if(target)target.textContent=e.message;Framekeep.notify(e.message)}finally{busy=false}}
  function current(){return Framekeep.remoteTarget().photoId}
  function selection(){return selected.length?selected:(current()?[current()]:[])}
  function paint(){document.querySelectorAll('[data-photo],[data-watch-photo]').forEach(el=>el.classList.toggle('asset-selected',selection().includes(el.dataset.photo||el.dataset.watchPhoto)));$('asset-count').textContent=`${selection().length} 张`;$('asset-parse-sku').disabled=parsing||!current()||Framekeep.remoteTarget().paused;$('asset-parse-sku').textContent=parsing?'OCR 解析中…':'OCR 解析当前图'}
  function changed(){
    const context=JSON.stringify(Framekeep.browseContext()),focus=current();
    if(context!==lastContext){selected=focus?[focus]:[];anchor=focus;lastContext=context;previewId=null;resetSelection()}
    else if(lastFocus!==focus){selected=focus?[focus]:[];anchor=focus;previewId=null;resetSelection()}
    lastFocus=focus;paint();
  }
  $('selection-page').addEventListener('click',event=>{
    if(event.target.closest('input,[data-star]'))return;
    const node=event.target.closest('[data-photo],[data-watch-photo]');if(!node)return;
    const target=node.dataset.photo||node.dataset.watchPhoto,order=Framekeep.archiveOrder();
    if(event.shiftKey&&anchor&&order.includes(anchor)){
      const a=order.indexOf(anchor),b=order.indexOf(target);selected=order.slice(Math.min(a,b),Math.max(a,b)+1);
    }else if(event.ctrlKey||event.metaKey){selected=selection().includes(target)?selection().filter(x=>x!==target):[...selection(),target];anchor=target}
    else {selected=[target];anchor=target}
    lastFocus=target;previewId=null;resetSelection();
  },true);
  document.addEventListener('framekeep:selection',changed);changed();
  window.FramekeepAssets={selection:()=>selection().slice(),resolve:async()=>{
    const ids=selection().slice(),sid=FRAMEKEEP_SESSION.id;if(!ids.length)throw new Error('请选择照片');
    return (await api('/api/archive/selection',{session_id:sid,capture_ids:ids})).assets;
  }};
  function resetSelection(){
    skuEdited=false;selectionRead='';readSequence++;skuEditVersion++;$('asset-sku').value='';ocrContext='';$('asset-ocr-result').hidden=true;$('asset-ocr-codes').replaceChildren();$('asset-ocr-text').textContent='';
    $('asset-sku').placeholder='识别后自动填入，也可批量修改';
  }
  async function fillSelection(force=false){
    const key=JSON.stringify([FRAMEKEEP_SESSION.id,selection(),archiveVersion]);
    if(parsing||!selection().length||(!force&&(skuEdited||selectionRead===key)))return;
    const request=++readSequence,assets=await FramekeepAssets.resolve();
    if(request!==readSequence||key!==JSON.stringify([FRAMEKEEP_SESSION.id,selection(),archiveVersion])||(!force&&skuEdited))return;
    const skus=[...new Set(assets.map(a=>a.sku))],scopes=[...new Set(assets.map(a=>a.scope))];
    $('asset-sku').value=skus.length===1?skus[0]:'';
    $('asset-sku').placeholder=skus.length>1?'所选款号不同，输入后可批量修改':'识别后自动填入，也可批量修改';
    if(force)msg(`所选 ${assets.length} 张：SKU ${skus.length===1?(skus[0]||'未备案'):'混合值'}；类型 ${scopes.length===1?(scopes[0]||'未填写'):'混合值'}`);
    skuEdited=false;selectionRead=key;previewId=null;
  }
  $('asset-read').onclick=()=>act(()=>fillSelection(true));
  const parseContext=()=>JSON.stringify([FRAMEKEEP_SESSION.id,current(),selection()]);
  function useOcrCode(code){$('asset-sku').value=code;$('asset-sku').dispatchEvent(new Event('input',{bubbles:true}))}
  $('asset-ocr-codes').onclick=event=>{const button=event.target.closest('[data-ocr-code]');if(!button)return;if(ocrContext!==parseContext()){msg('当前图片已变化，请重新解析');return}useOcrCode(button.dataset.ocrCode);msg('已填入款号 '+button.dataset.ocrCode+'，请核对后预览写入')};
  $('asset-parse-sku').onclick=()=>act(async()=>{
    const sid=FRAMEKEEP_SESSION.id,cid=current(),context=parseContext(),edit=skuEditVersion;
    if(!cid)throw new Error('请先选择要解析的当前照片');
    parsing=true;readSequence++;paint();msg('正在用已选模型解析当前图的 SKU 款号…');$('asset-ocr-result').hidden=true;
    try{
      const result=await api('/api/sku/parse',{session_id:sid,capture_id:cid},660000);
      document.dispatchEvent(new CustomEvent('framekeep:ocr-result',{detail:{session_id:sid,capture_id:cid}}));
      if(context!==parseContext()||result.session_id!==sid||result.capture_id!==cid){msg('图片选择已变化，本次结果未填入，请重新解析');return}
      const reading=result.reading||{},codes=[...new Set((reading.codes||[]).filter(code=>typeof code==='string'&&code.trim()&&code.length<=80&&!/[\x00-\x1f]/.test(code)).map(code=>code.trim()))];
      ocrContext=context;$('asset-ocr-text').textContent=(reading.model?reading.model+'\n':'')+(reading.text||'未返回可见文本');$('asset-ocr-result').hidden=false;
      $('asset-ocr-codes').innerHTML=codes.map(code=>`<button data-ocr-code="${esc(code)}" title="将此文本填入 SKU 款号">${esc(code)}</button>`).join('');
      if(codes.length===1&&edit===skuEditVersion){useOcrCode(codes[0]);msg(`已读取 ${codes[0]}${reading.unscored?'（模型未提供置信度）':''}，请核对后预览写入`)}
      else if(codes.length){msg(edit!==skuEditVersion?'解析完成；款号已手动修改，请点击候选文本后填入':'读出多个款号，请点击正确文本填入，再核对写入')}
      else msg('未读出可用 SKU 款号，请展开 OCR 原文核对或手动填写');
    }finally{parsing=false;paint()}
  });
  $('asset-preview').onclick=()=>act(async()=>{
    const source=window.FramekeepSku?.shootingTypes(),scope=window.FramekeepSku?.currentType();
    if(!scope)throw new Error('请在当前拍摄中选择或填写拍摄类型');
    const context=()=>JSON.stringify([FRAMEKEEP_SESSION.id,selection(),FramekeepSku.currentType(),FramekeepSku.shootingTypes()?.project_id,$('asset-sku').value.trim(),$('asset-mode').value,$('asset-overwrite').checked]),before=context();
    const data=await api('/api/metadata/preview',{session_id:FRAMEKEEP_SESSION.id,capture_ids:selection(),sku:$('asset-sku').value.trim(),scope,project:source?.project_id||'',mode:$('asset-mode').value,overwrite:$('asset-overwrite').checked});
    if(before!==context())throw new Error('当前拍摄或所选照片已变化，请重新预览');previewId=data.preview_id;
    $('asset-preview-content').innerHTML=`<p>SKU：${esc(data.sku)} · 类型：${esc(data.scope)} · ${data.targets.length} 张 / ${data.targets.reduce((n,t)=>n+t.files.length,0)} 个文件</p><div class="sku-table-wrap"><table><thead><tr><th>资产</th><th>原归属</th><th>处理</th><th>写入文件</th></tr></thead><tbody>${data.targets.map(t=>`<tr><td>${esc(t.files[0]?.path.split(/[\\/]/).at(-1)||t.id)}</td><td>${esc(t.before.sku||'未备案')} / ${esc(t.before.scope)}</td><td>${t.apply?'将备案':'冲突，跳过'}</td><td>${t.files.map(f=>`<div>${esc(f.path.split(/[\\/]/).at(-1))} · ${f.capability==='embedded'?'文件内部元数据':'此格式暂不支持内嵌'}${f.online?'':' · 离线'}</div>`).join('')}</td></tr>`).join('')}</tbody></table></div><p>确认后将所列 RAW 和 JPG 分别写入文件内部的 SKU 与备案字段，保留原有星级和图像数据。暂不支持的格式或离线文件会单独记录失败。</p>`;
    $('asset-apply').disabled=!data.targets.some(t=>t.apply);preview.showModal();
  });
  $('asset-apply').onclick=()=>act(async()=>{if(!previewId)throw new Error('选择已变化，请重新预览');const result=await api('/api/metadata/apply',{preview_id:previewId});operation=result.operation.id;preview.close();previewId=null;skuEdited=false;selectionRead='';msg('数据库已备案，RAW / JPG 内部元数据写入已排队')});
  $('asset-start').onclick=()=>act(async()=>{const s=await api('/api/archive/start',{session_id:FRAMEKEEP_SESSION.id,mode:$('asset-mode').value});stream=s.streams.find(s=>s.state==='active'&&s.session===FRAMEKEEP_SESSION.id);msg('已接续未归组照片，等待款末标签')});
  $('asset-mode').onchange=()=>act(async()=>{previewId=null;if(stream)await api('/api/archive/configure',{stream_id:stream.id,mode:$('asset-mode').value})});
  $('asset-close-stream').onclick=()=>act(async()=>{const sid=FRAMEKEEP_SESSION.id,sku=$('asset-stream-sku').value.trim()||$('asset-sku').value.trim();await history();if(sid!==FRAMEKEEP_SESSION.id)throw new Error('当前目录已变化，请重新操作');if(!stream)throw new Error('请先开始拍摄段');await api('/api/archive/close',{stream_id:stream.id,sku});msg('当前未结束图片已封口，请在归档库查看区间')});
  async function search(){const data=await api('/api/assets/search?'+new URLSearchParams({sku:$('archive-sku').value,scope:$('archive-scope').value,project:$('archive-project').value,mode:$('archive-match').value,cursor:cursors[pageIndex]}));queryRows=data.rows;$('archive-total').textContent=data.rows.length;nextCursor=data.next_cursor;$('archive-page').textContent=`第 ${pageIndex+1} 页 · ${data.rows.length} 项 · ${data.elapsed_ms} ms`;$('archive-prev').disabled=pageIndex===0;$('archive-next').disabled=!data.has_more;
    $('archive-results').innerHTML=data.rows.length?'<table><thead><tr><th>图片</th><th>SKU / 类型</th><th>位置</th><th>操作</th></tr></thead><tbody>'+data.rows.map(r=>`<tr><td>${r.preview?`<img class="archive-thumb" src="${esc(r.preview)}" loading="lazy" alt="资产缩略图">`:''}</td><td>${esc(r.sku||'待备案')}<br>${esc(r.scope)}<br><small>${esc(statusName(r.role))}</small></td><td>${r.locations.map(l=>esc(l.path)+' · '+esc(statusName(l.status))).join('<br>')}</td><td><button data-asset-detail="${r.id}">详情</button></td></tr>`).join('')+'</tbody></table>':empty('暂无匹配资产','调整检索条件，或在选片页完成 SKU 备案后查看。');
  }
  async function history(){const s=await api('/api/archive/streams');stream=s.streams.find(s=>s.state==='active'&&s.session===FRAMEKEEP_SESSION.id)||null;
    $('asset-stream-status').textContent=stream?`${stream.scope} · 待封口 ${stream.pending||0} 张 · 已归组 ${s.segments.filter(x=>x.stream===stream.id).length} 款`:'尚未开始当前目录拍摄段';
    archiveVersion=JSON.stringify(s.segments.map(x=>[x.id,x.sku,x.status,x.operation]));await fillSelection();
    if(!visible())return;
    $('archive-sealed').textContent=s.segments.length;$('archive-late').textContent=s.late.length;
    $('archive-segments').innerHTML=s.segments.length?s.segments.map(x=>`<article><div><strong>${esc(x.sku||'待识别 SKU')}</strong><span class="workspace-badge">${esc(statusName(x.status))}</span></div><p>${x.members.length} 张（含标签）${x.reason?' · '+esc(x.reason):''}</p>${x.status==='ready'?`<button data-segment-apply="${x.id}">备案并写入</button>`:''}</article>`).join(''):empty('暂无结束区间','款末标签确认后，自动显示本款归组记录。');
    const ops=await api('/api/metadata/operations');$('archive-batches').textContent=ops.operations.length;
    $('archive-operations').innerHTML=ops.operations.length?ops.operations.map(o=>`<article><div><strong>${esc(statusName(o.kind))}</strong><span class="workspace-badge">${esc(statusName(o.status))}</span></div><p>${new Date(o.created*1000).toLocaleString()}</p><div class="asset-actions"><button data-operation="${o.id}">详情</button><button data-retry="${o.id}">重试失败</button>${o.kind==='assign'?`<button data-revert="${o.id}">撤销批次</button>`:''}</div></article>`).join(''):empty('暂无写入批次','备案写入和撤销结果会记录在这里。');
  }
  FramekeepWorkspace.register('archive',archiveTab,archive,()=>act(async()=>{await search();await history();api('/api/search/status').then(s=>{$('everything-status').textContent=s.available?`Everything ${s.version} 已连接`:s.message}).catch(e=>{$('everything-status').textContent=e.message})}));
  FramekeepWorkspace.register('settings',settingsButton,settings,()=>{settingsContext();act(history)});
  $('archive-search').onclick=()=>act(async()=>{resetPage();await search()});$('archive-prev').onclick=()=>act(async()=>{pageIndex=Math.max(0,pageIndex-1);await search()});$('archive-next').onclick=()=>act(async()=>{if(!nextCursor)return;cursors[++pageIndex]=nextCursor;await search()});
  for(const id of ['archive-sku','archive-scope','archive-project','archive-match']){
    $(id).addEventListener('change',()=>{resetPage();$('archive-prev').disabled=true;$('archive-next').disabled=true});
    $(id).addEventListener('keydown',e=>{if(e.key==='Enter')$('archive-search').click()});
  }
  async function fileSearch(){const data=await api('/api/search/files?'+new URLSearchParams({q:fileText,offset:fileOffset}));$('file-search-message').textContent=`${data.source==='everything'?'Everything':'备案库回退'} · ${data.rows.length} 项 · ${data.elapsed_ms} ms · ${data.coverage}${data.message?' · '+data.message:''}`;$('file-next').disabled=!data.has_more||fileOffset>=9960;
    $('file-search-results').innerHTML='<table><thead><tr><th>文件位置</th><th>已有备案</th></tr></thead><tbody>'+data.rows.map(r=>`<tr><td>${esc(r.path)}</td><td>${r.asset?`${esc(r.asset.sku||'待备案')} / ${esc(r.asset.scope)} <button data-asset-detail="${esc(r.asset.id)}">详情</button>`:'未备案候选'}</td></tr>`).join('')+'</tbody></table>';
  }
  $('file-search').onclick=()=>act(async()=>{fileOffset=0;fileText=$('file-query').value;await fileSearch()});$('file-next').onclick=()=>act(async()=>{fileOffset+=60;await fileSearch()});$('file-query').onkeydown=e=>{if(e.key==='Enter')$('file-search').click()};
  $('archive-relocate').onclick=()=>act(async()=>{const r=await api('/api/assets/relocate',{folder:$('archive-folder').value});$('archive-message').textContent=`已扫描 ${r.assets} 个资产`;await search()});
  archive.addEventListener('click',e=>act(async()=>{const b=e.target.closest('button');if(!b)return;
    if(b.dataset.assetDetail){const data=await api('/api/assets/'+b.dataset.assetDetail);detail(data)}
    if(b.dataset.operation){detail(await api('/api/metadata/operations/'+b.dataset.operation))}
    for(const [attr,path] of [['retry','retry'],['revert','revert']])if(b.dataset[attr]){const r=await api('/api/metadata/'+path,{operation_id:b.dataset[attr]});detail(r);await search()}
    if(b.dataset.segmentApply){await api('/api/archive/segment-apply',{segment_id:b.dataset.segmentApply});await history();await search()}
  }));
  $('archive-export').onclick=()=>{const blob=new Blob([JSON.stringify(queryRows,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='资产归档.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
  preview.querySelector('[data-close]').onclick=()=>preview.close();
  async function poll(){
    try{if(!busy){
      await history();
      if(operation){
        const op=await api('/api/metadata/operations/'+operation),counts={};let raw=0,jpg=0;
        op.jobs.forEach(j=>{const key=j.status==='applied'?(j.storage==='embedded'?'已内嵌':'仅伴随文件'):statusName(j.status);counts[key]=(counts[key]||0)+1;if(j.status==='applied'&&j.storage==='embedded'){if(/\.jpe?g$/i.test(j.path))jpg++;else if(/\.(cr3|arw)$/i.test(j.path))raw++}});
        const failed=op.jobs.filter(j=>j.status==='failed'),pending=op.jobs.some(j=>['queued','running'].includes(j.status));
        msg((failed.length&&!pending?'部分文件未写入 · ':'文件状态：')+Object.entries(counts).map(([k,n])=>`${k} ${n}${k==='已内嵌'?`（RAW ${raw} / JPG ${jpg}）`:''}`).join(' / ')+(failed.length?'；'+failed.map(j=>j.path.split(/[\\/]/).at(-1)+'：'+j.reason).join('；'):''));
        if(!pending)operation=null;
      }
    }}catch{}setTimeout(poll,1800)
  }poll();
})();
