(() => {
  'use strict';
  const $=id=>document.getElementById(id),api=(path,body)=>FramekeepIO.api(path,body);
  const inspector=document.querySelector('#asset-archive-page .catalog-inspector')||document.querySelector('.catalog-inspector');
  if(!inspector)return;
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const controls=document.createElement('div');controls.className='capture-controls';
  controls.innerHTML='<button id="capture-edit" type="button">进入编辑</button><button id="capture-export" type="button" class="primary">导出拍摄文档</button><span id="capture-access" role="status">选择左侧实际文件夹</span>';
  inspector.querySelector('header').after(controls);
  const tabs=document.createElement('div');tabs.className='capture-tabs';tabs.setAttribute('role','tablist');tabs.setAttribute('aria-label','归档右侧资料');
  tabs.innerHTML='<button id="capture-photo-tab" role="tab" aria-controls="capture-photo-panel" aria-selected="true">照片设置</button><button id="capture-folder-tab" role="tab" aria-controls="capture-folder-panel" aria-selected="false" tabindex="-1">目录拍摄</button>';
  controls.after(tabs);
  const photo=document.createElement('section');photo.id='capture-photo-panel';photo.setAttribute('role','tabpanel');photo.setAttribute('aria-labelledby','capture-photo-tab');
  for(const node of [$('catalog-inspector-empty'),$('catalog-inspector-form'),inspector.querySelector('.catalog-scan-panel')])if(node)photo.append(node);
  tabs.after(photo);
  // 右栏与现场拍摄同一规范：SKU 款号输入 → OCR 解析当前图（有草稿时为“确定”）→ OCR 资料 / 选择 SKU → 说明。
  const skuInput=$('catalog-selected-sku'),skuLabel=skuInput.closest('label'),saveButton=document.createElement('button');
  saveButton.id='catalog-save-sku';saveButton.textContent='确定';
  skuLabel.classList.add('capture-sku-label');skuInput.placeholder='输入后按 Enter 或点击确定，更新当前整款 SKU';
  const ocr=document.createElement('section');ocr.className='capture-ocr';
  ocr.innerHTML='<input id="capture-correction" type="checkbox" checked hidden><input id="capture-whole-group" type="checkbox" checked hidden><div class="capture-ocr-actions"><button id="capture-ocr" type="button" title="使用已选 OCR 模型立即读取当前图，不要求二星">OCR 解析当前图</button><button id="capture-ocr-details" type="button" aria-expanded="false" aria-controls="capture-ocr-result">OCR 资料 / 选择 SKU</button></div><div id="capture-ocr-result" class="capture-ocr-result" hidden><p class="capture-ocr-heading">推荐款号 · 点击即应用整款</p><div id="capture-candidates" class="capture-candidates" role="group" aria-label="推荐款号，点击即应用整款"></div><p class="capture-ocr-heading">OCR 原文 · 点击填入输入框</p><div id="capture-ocr-text" class="capture-ocr-raw" role="group" aria-label="OCR 原文，点击填入输入框"></div></div><p id="capture-write-help" class="capture-rule">解析完成后自动写入 SKU 元数据；修正款号后点击确定，整款 RAW / JPG 同步更新；清空输入可重新进行 OCR 识别。</p><p id="capture-group-hint"></p><section id="capture-conflict" hidden></section><p id="capture-item-status" role="status"></p>';
  skuLabel.after(ocr);
  saveButton.type='button';saveButton.classList.remove('primary');saveButton.hidden=true;ocr.querySelector('.capture-ocr-actions').prepend(saveButton);
  $('capture-write-help').after($('catalog-selected-source'));
  // 二星解析动画：复用现场拍摄的 SVG 卡片与动画样式，放在星级下方，按键后立即可见。
  const feedback=window.FramekeepOcrFeedback?.card?.('capture-ocr-feedback')||null;
  if(feedback){feedback.setAttribute('role','status');feedback.setAttribute('aria-live','polite');feedback.setAttribute('aria-atomic','true');skuLabel.before(feedback)}
  const folder=document.createElement('section');folder.id='capture-folder-panel';folder.hidden=true;folder.setAttribute('role','tabpanel');folder.setAttribute('aria-labelledby','capture-folder-tab');
  folder.innerHTML='<p id="capture-folder-path" class="capture-folder-path" hidden style="display:none"></p><div class="capture-scope-fields" hidden style="display:none"><label>客户<input id="capture-customer" maxlength="100" placeholder="默认作用域"></label><label>品牌<input id="capture-brand" maxlength="100" placeholder="默认作用域"></label></div><label hidden style="display:none">拍摄日期<input id="capture-day" type="date"></label><section id="capture-summary" role="status" aria-label="目录统计"></section><section id="capture-sync" class="capture-status-section" role="status" aria-label="读取与校验"></section><section id="capture-write-status" class="capture-status-section" role="status" aria-label="元数据写入"></section><div id="capture-groups"></div><details><summary>操作历史与撤销</summary><div id="capture-history"></div></details><div id="capture-errors" role="status"></div><div class="capture-folder-actions"><button id="capture-cancel-plan" type="button" hidden>取消归档计划并恢复源写入</button><div id="capture-write-progress" class="capture-write-progress" data-phase="idle" hidden><div class="capture-write-head"><strong id="capture-write-phase"></strong><span id="capture-write-count"></span></div><div class="capture-write-track" role="progressbar" aria-label="补写元数据进度" aria-valuemin="0" aria-valuemax="100"><span id="capture-write-fill"></span></div><p id="capture-write-result" role="status"></p></div><button id="capture-write-metadata" type="button" title="补齐当前目录已确认但未写入的元数据，并重试失败写入；已验证文件跳过">补写元数据</button><button id="capture-refresh" type="button">重新读取目录</button><button id="capture-retry" type="button">重试失败任务</button></div>';
  photo.after(folder);
  // 两个入口共用同一份进度与卡片状态，照片设置内也能看见实际文件写入进展。
  for(const name of ['count','fill','result'])$('capture-write-'+name).classList.add('capture-write-'+name);
  const photoWrite=folder.querySelector('#capture-write-progress').cloneNode(true);
  for(const node of [photoWrite,...photoWrite.querySelectorAll('[id]')])node.id=node.id.replace('capture-write','capture-photo-write');
  $('capture-ocr-details').before(photoWrite);
  let writeRequest=null,writeScope='',writePolling=false,pollAbort=null,writeAbort=null,writeSequence=0,writeItems={},writeFrame=0;
  // 卡片写入状态：写入中持续微光，写完淡出；排队与失败以角标区分。
  const writeShown=new Map(),writeFading=new Map();
  const ratingQueue=new Map(),ratingConfirmed=new Map(),ratingPending=new Map(),ratingUncertain=new Map();let ratingWorker=null,ratingReload=0;
  let state=null,currentId='',selected=null,request=0,itemRequest=0,busy=false,polling=false,activeScope='',itemVersion='',groupVersion='',picked=new Set(),anchor='';
  // knownSku：当前素材已保存的款号；输入框与之不同且非空即视为草稿，主按钮变为“确定”。
  let knownSku='',detailsOpen=false,detailsAuto='',ocrSubmitting=false,manualSubmitting=false;const ocrStarted=new Map();
  const skuDraft=()=>{const value=skuInput.value.trim();return !!value&&value!==knownSku};
  const context=()=>window.FramekeepArchiveSelection?.()||{root_id:'',folder:''};
  const scopeBody=()=>{const chosen=window.FramekeepArchiveSelection?.();return chosen?{root_id:chosen.root_id||'',folder:chosen.folder||''}:selected?.scope||{root_id:'',folder:''}};
  const note=(text,error=false)=>{const node=$('capture-item-status');node.textContent=text;node.dataset.error=String(error)};
  function tab(name){
    for(const kind of ['photo','folder']){const active=kind===name;$('capture-'+kind+'-panel').hidden=!active;$('capture-'+kind+'-tab').setAttribute('aria-selected',String(active));$('capture-'+kind+'-tab').tabIndex=active?0:-1}
    try{localStorage.setItem('framekeep.archive.inspector.tab',name)}catch{}
  }
  for(const name of ['photo','folder'])$('capture-'+name+'-tab').onclick=()=>tab(name);
  tabs.onkeydown=event=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();const name=event.key==='Home'?'photo':event.key==='End'?'folder':photo.hidden?'photo':'folder';tab(name);$('capture-'+name+'-tab').focus()}};
  try{if(localStorage.getItem('framekeep.archive.inspector.tab')==='folder')tab('folder')}catch{}
  function editing(){return !!state?.editing&&!state?.freeze_job&&!state?.readonly_reason}
  function setEnabled(){
    const writable=editing(),conflicted=!!(selected?.record.metadata_conflict||selected?.record.identity_conflict);
    // 星级按队列保存：不等待素材资料返回或上一张保存完成，冲突与冻结仍由服务端最终校验。
    for(const button of $('catalog-rating-controls').querySelectorAll('button'))button.disabled=!!state?.freeze_job||!currentId||conflicted;
    $('catalog-save-sku').disabled=busy||!currentId||!selected||conflicted||!!state?.readonly_reason||!!state?.freeze_job||!!state?.loading&&!!selected?.record.boundary;$('capture-correction').disabled=busy||!!state?.freeze_job;
    // 与现场一致：输入框有草稿时显示“确定”（整款），清空后恢复“OCR 解析当前图”；OCR 不要求二星。
    const draft=skuDraft(),reading=ocrSubmitting||selected?.record.ocr_state==='queued';
    saveButton.hidden=!draft;$('capture-ocr').hidden=draft;
    saveButton.textContent=busy&&draft?'确定中…':'确定';saveButton.title=$('capture-whole-group').checked?'将输入的款号应用到当前 SKU 区间全部图片（RAW / JPG 同步）':'将输入的款号应用到当前素材';
    $('capture-ocr').textContent=reading?'正在解析当前图…':'OCR 解析当前图';
    $('capture-ocr').disabled=reading||busy||conflicted||!!state?.readonly_reason||!!state?.freeze_job||!currentId||!selected||selected.record.media_type!=='image';
    $('capture-ocr-details').disabled=!currentId;
    $('capture-retry').disabled=busy||!writable;
    const scopeKey=JSON.stringify(scopeBody()),matching=scopeKey===activeScope,current=writeScope===scopeKey?writeRequest:null;
    const waiting=['queued','preparing'].includes(current?.status),progress=current?.status==='submitted'?current.progress:null;
    const writing=!!progress&&(progress.running||0)+(progress.queued||0)>0;
    $('capture-write-metadata').disabled=busy||!scopeBody().root_id||(matching&&(!!state?.readonly_reason||!!state?.freeze_job))||waiting||writing;
    $('capture-write-metadata').title='可在目录读取期间先提交；读取完成后自动补写此文件夹内全部已确认款号区间';
    $('capture-refresh').disabled=busy||!scopeBody().root_id;
    $('capture-write-metadata').textContent=busy?'正在提交…':waiting?'等待目录读取完成…':writing?`正在补写 ${progress.done||0} / ${progress.total}…`:'补写元数据';$('capture-batch').disabled=!writable||!picked.size;
    $('capture-edit').disabled=!scopeBody().root_id||!state||busy||!!state?.freeze_job;
    $('capture-export').disabled=!state?.total||busy;
    $('capture-customer').disabled=writable;$('capture-brand').disabled=writable;
  }
  const metric=(label,value,unit='')=>`<th scope="row">${esc(label)} </th><td>${esc(value)}${unit?` <small>${esc(unit)}</small>`:''}</td>`;
  const fact=(label,value,unit='')=>`<tr>${metric(label,value,unit)}</tr>`;
  const statusTable=(rows,paired=false)=>`<table class="capture-status-table${paired?' capture-status-table-pairs':''}"><tbody>${rows}</tbody></table>`;
  function setMarkup(id,markup){if($(id).innerHTML!==markup)$(id).innerHTML=markup}
  // ---- OCR 资料 / 选择 SKU：推荐款号点击即应用整款；原文行点击填入输入框后按“确定”。
  function showDetails(open){detailsOpen=open;$('capture-ocr-result').hidden=!open;$('capture-ocr-details').setAttribute('aria-expanded',String(open))}
  function paintOcrResult(record,task){
    const codes=[...new Set((record.candidates||[]).filter(code=>typeof code==='string'&&code.trim()).map(code=>code.trim()))],chosen=record.sku||'';
    setMarkup('capture-candidates',codes.map(code=>`<button type="button" class="capture-code" data-capture-code="${esc(code)}" aria-pressed="${code===chosen}"><span>${esc(code)}</span><small>${code===chosen?'已应用 ✓':'点击应用'}</small></button>`).join('')||'<span class="capture-ocr-empty">未找到推荐款号，可从下方原文选择或直接填写 SKU。</span>');
    const lines=String(record.ocr?.text||'').split(/\r?\n/).map(line=>line.trim()).filter(Boolean);
    setMarkup('capture-ocr-text',lines.length?lines.map(line=>[...line].length<=80?`<button type="button" class="capture-ocr-line" data-capture-line="${esc(line)}">${esc(line)}</button>`:`<span class="capture-ocr-line">${esc(line)}</span>`).join(''):'<span class="capture-ocr-empty">尚无 OCR 资料，可点击“OCR 解析当前图”或直接填写款号。</span>');
    // 待核对时自动展开一次（与现场拍摄一致），用户收起后同一结果不再强制展开。
    // 规则唯一命中正在自动应用整款时不展开，避免动画与资料区同时跳动。
    const applying=record.ocr_apply_state==='waiting_interval'||!!task&&['queued','running'].includes(task.status);
    const review=!record.sku&&!record.inherited_sku&&!!record.ocr&&!applying&&(record.ocr_apply_state==='review'||record.ocr_state==='ready'||record.ocr_state==='manual');
    const key=currentId+':'+(record.ocr?.text||'')+':'+codes.join('|');
    if(review&&detailsAuto!==key){detailsAuto=key;showDetails(true)}
  }
  // ---- 解析动画状态：排队 / 解析中 / 应用整款 / 完成 / 待核对 / 失败。
  function showFeedback(status,title='',text='',phase='',id=currentId){
    if(!feedback||id!==currentId)return;
    const active=['running','queued','retry'].includes(status);
    if(active){if(!ocrStarted.has(id))ocrStarted.set(id,Date.now())}else ocrStarted.delete(id);
    const started=ocrStarted.get(id);
    FramekeepOcrFeedback.paint(feedback,status,title,text,status?{phase:phase||undefined,elapsed_seconds:started?(Date.now()-started)/1000:0}:null);
    tickFeedback();
  }
  function restartFeedback(id){ocrStarted.delete(id)}
  function tickFeedback(){
    if(!feedback||feedback.hidden)return;
    const track=feedback.querySelector('.ocr-feedback-track');
    if(track.dataset.measured==='false')feedback.querySelector('.ocr-feedback-elapsed').textContent='已等待 '+Math.max(0,Math.floor((Date.now()-Number(track.dataset.started))/1000))+' 秒';
  }
  setInterval(tickFeedback,1000);
  function ocrActive(result=selected){
    const record=result?.record,task=result?.ocr_task;if(!record)return false;
    return ocrSubmitting||record.ocr_state==='queued'||!!task&&['queued','running'].includes(task.status)||record.ocr_apply_state==='waiting_interval'&&!record.sku||!!state?.loading&&!!record.boundary&&record.rating===2&&!record.sku&&!record.inherited_sku&&!record.ocr;
  }
  function paintFeedbackFromItem(result){
    const id=currentId,record=result.record,group=result.group,task=result.ocr_task;
    // 保存队列中的星级尚未落库：保持按键时的即时反馈，不被迟到的旧资料覆盖。
    if(ratingPending.has(id)){if(ratingPending.get(id)!==2)showFeedback('');return}
    if(ocrSubmitting)return;
    if(manualSubmitting)return;
    if(record.media_type!=='image'){showFeedback('');return}
    if(writeRequest?.result?.mode==='manual'&&(writeRequest.result.item_id===id||writeItems[id])){
      const p=writeRequest.progress,code=writeRequest.result.sku;
      if(writeRequest.status==='failed'){showFeedback('failed',code,writeRequest.reason||'元数据写入未完成');return}
      if(record.rating===2)ratingStatus(id,`已保存 2 星 · SKU ${code}`);
      if(p){
        if(p.running||p.queued){showFeedback('running',code,`正在写入元数据 · 已写入 ${p.done} / ${p.total}`,'saving');return}
        if(p.failed){showFeedback('failed',code,`元数据写入未完成 · ${p.failed} 张失败，可重试`);return}
        if(p.done===p.total&&p.total){showFeedback('completed',code,`SKU · 元数据已写入 ${p.done} 张`);return}
      }
    }
    const code=record.sku||record.inherited_sku||'',taskActive=!!task&&['queued','running'].includes(task.status);
    if(taskActive&&task.kind!=='ocr_apply'||record.ocr_state==='queued'&&!code){
      if(task?.status==='running')showFeedback('running','正在解析款号','模型正在读取标签文字…','recognizing');
      else showFeedback('queued','款号已进入解析','OCR 模型空闲后立即读取当前图','waiting_slot');
      return;
    }
    if(!code&&(record.ocr_apply_state==='waiting_interval'||taskActive&&task.kind==='ocr_apply')){
      showFeedback('running',record.ocr_apply_sku||'正在应用款号',state?.loading?'款号已识别，完整区间读取完成后自动应用到整款':'款号已识别，正在自动划分 SKU 区间并应用整款','saving');return;
    }
    if(task?.status==='failed'&&!code){showFeedback('failed','款号解析失败',task.reason||record.ocr_reason||'可点击“OCR 解析当前图”重试');return}
    if(code&&(record.boundary||record.ocr)){
      const count=group?.members?.length||0;
      if(/^二星已保存|^已沿用/.test($('catalog-rating-status').textContent))ratingStatus(id,`已保存 2 星 · SKU ${code}`);
      showFeedback('completed',code,record.boundary?(count?`SKU · 已应用整款 ${count} 张（含二星）`:'SKU · 解析完成'):'SKU · 当前图');return;
    }
    if(/未配置OCR/.test(record.ocr_reason||'')){showFeedback('waiting','等待配置 OCR 模型','区间已建立，可在设置中选择 OCR 模型，或直接填写 SKU 后确定');return}
    const codes=record.candidates||[];
    if(record.ocr&&(record.ocr_apply_state==='review'||codes.length)){showFeedback('review',codes.join(' / ')||'款号待确认',record.ocr_reason||'OCR 候选 · 点击推荐款号即可应用整款');return}
    if(record.ocr_state==='manual'||record.ocr&&!codes.length){showFeedback('review','未识别到款号',record.ocr_reason||group?.reason||'可查看 OCR 原文，或直接填写 SKU 后确定');return}
    if(record.boundary&&record.rating===2&&!record.ocr){
      if(state?.loading)showFeedback('queued','二星已保存','正在读取目录并准备 SKU 区间，完成后自动解析','submitting');
      else showFeedback('waiting','等待解析款号','点击“OCR 解析当前图”立即解析');
      return;
    }
    showFeedback('');
  }
  function syncScope(){
    const chosen=scopeBody(),key=JSON.stringify(chosen);
    if(key===activeScope)return false;
    activeScope=key;pollAbort?.abort();pollAbort=null;polling=false;++request;++itemRequest;
    writeAbort?.abort();writeAbort=null;writePolling=false;++writeSequence;
    state=null;selected=null;currentId='';itemVersion='';groupVersion='';anchor='';picked.clear();
    for(const id of ['capture-summary','capture-sync','capture-write-status','capture-groups','capture-history','capture-errors','capture-group-hint','capture-candidates','capture-ocr-text','capture-item-status'])$(id).replaceChildren();
    $('capture-conflict').hidden=true;$('capture-folder-path').textContent=context().path||'';knownSku='';detailsAuto='';showFeedback('');
    $('capture-customer').value='';$('capture-brand').value='';
    $('capture-access').textContent=chosen.root_id?'正在读取当前文件夹资料…':'选择左侧实际文件夹';
    if(chosen.root_id)$('capture-sync').textContent='正在读取当前目录的统计、SKU区间与写入状态…';
    writeRequest=null;writeScope=key;writeItems={};writeShown.clear();writeFading.clear();paintWriteProgress(null);scheduleWriteCards();paintPicked();setEnabled();
    return true;
  }
  function paintState(data){
    state=data;$('capture-edit').textContent=data.editing?'结束编辑并交接':'进入编辑';
    $('capture-access').textContent=data.readonly_reason|| (data.freeze_job?'归档计划已冻结源文件，执行或取消计划后恢复写入':data.editing?'当前设备可编辑 · 仅修改此数据集':'浏览中 · 标星时自动取得当前目录编辑权');
    $('capture-cancel-plan').hidden=!data.freeze_job||data.freeze_job.startsWith('execute:');
    $('capture-folder-path').textContent=data.scope;$('capture-folder-path').title=data.root;
    for(const key of ['customer','brand'])if(document.activeElement!==$('capture-'+key))$('capture-'+key).value=data[key]||'';
    const groups=data.groups.filter(g=>!$('capture-day').value||data.records.some(r=>r.group_id===g.id&&r.shoot_date?.slice(0,10)===$('capture-day').value));
    const confirmed=groups.filter(g=>g.status==='confirmed').length;
    setMarkup('capture-summary',statusTable(`<tr>${metric('文件总数',data.total,'个')}${metric('已确认',confirmed,'款')}</tr><tr>${metric('待处理',groups.length-confirmed,'款')}${metric('未闭合',data.unclosed,'张')}</tr>`,true));
    const scan=data.scan_progress||{},readCount=scan.total?`${Math.min(scan.done||0,scan.total)} / ${scan.total}`:'正在准备…';
    const archiveStatus=data.dataset_id?(data.unpublished?`${data.unpublished} 条待同步`:'已同步'):'从图片 / XMP 读取';
    setMarkup('capture-sync',`<div class="capture-status-heading"><strong>读取与校验</strong><span class="capture-status-badge">${data.loading?'读取中':'已就绪'}</span></div>${statusTable(`${data.loading?fact('读取进度',readCount):''}${fact('文件夹档案',archiveStatus)}${fact('SKU已在文件',`${data.sku_file_verified||0}/${data.total}`)}${fact('待补写',data.sku_fill_needed||0,'张')}${fact('完整字段已验证',`${data.metadata_verified}/${data.metadata_required}`)}`)}`);
    const writes=data.metadata_tasks||{};
    setMarkup('capture-write-status',`<div class="capture-status-heading"><strong>元数据写入</strong></div>${statusTable(`<tr>${metric('排队',writes.queued||0)}${metric('写入中',writes.running||0)}</tr><tr>${metric('待现场同步',writes.waiting_native||0)}${metric('失败',writes.failed||0)}</tr>`,true)}${data.metadata_wait_reason?`<p class="capture-status-note">${esc(data.metadata_wait_reason)}</p>`:''}`);
    const markup=groups.map(g=>`<article class="capture-group"><div class="capture-group-heading"><strong>${esc(g.sku||'待填写款号')}</strong><span class="capture-status-badge">${esc(({confirmed:'已确认',pending:'待识别',manual:'待手填款号',review:'待复核'})[g.status]||g.status)}</span></div>${statusTable(fact('区间图片',g.members?.length||0,'张（含二星）'))}${g.reason?`<p class="capture-status-note">${esc(g.reason)}</p>`:''}</article>`).join('')||'<p class="catalog-empty">尚无已结束分组，直接标记二星即可分款。</p>';
    if($('capture-groups').innerHTML!==markup)$('capture-groups').innerHTML=markup;
    const history=data.history.map(h=>`<div class="capture-history-row"><span>${esc(({confirm:'款号确认',rating:'评级／标记',undo:'撤销',live_confirm:'现场确认'})[h.kind]||h.kind)}</span><button type="button" data-capture-undo="${esc(h.id)}" ${data.editing?'':'disabled'}>撤销</button></div>`).join('');
    if($('capture-history').innerHTML!==history)$('capture-history').innerHTML=history;
    $('capture-errors').textContent=[data.error,...data.tasks.filter(t=>t.status==='failed').map(t=>t.reason)].filter(Boolean).join('\n');
    setEnabled();
  }
  async function item(force=false){
    if(!currentId)return;
    const id=currentId,key=JSON.stringify(scopeBody()),version=id+':'+(state?.revision||0)+':'+(state?.view_revision||0),sequence=++itemRequest;
    if(!force&&version===itemVersion)return;
    let result;
    try{result=await api('/api/archive-capture/item?'+new URLSearchParams({item_id:id}))}
    catch(error){if(id!==currentId||sequence!==itemRequest||key!==JSON.stringify(scopeBody()))return;throw error}
    if(id!==currentId||sequence!==itemRequest||key!==JSON.stringify(scopeBody()))return;
    selected=result;itemVersion=version;
    const group=result.group,record=result.record;
    // 请求超时不会撤销后端写入；只在重新读到目标星级后确认成功，不重发写请求。
    if(ratingUncertain.has(id)&&record.rating===ratingUncertain.get(id)&&!ratingQueue.has(id)){
      ratingUncertain.delete(id);ratingPending.delete(id);ratingConfirmed.set(id,record.rating);
      ratingStatus(id,`已核对，${record.rating?'已保存 '+record.rating+' 星':'已清除星级'}`);
      document.dispatchEvent(new CustomEvent('framekeep:archive-rating-saved',{detail:{item_id:id,rating:record.rating}}));
    }
    // 保存队列中尚未落库的星级优先显示，迟到的旧资料不能把刚标的星级闪回。
    const pendingRating=ratingPending.get(id);
    document.dispatchEvent(new CustomEvent('framekeep:archive-record',{detail:{item_id:id,rating:pendingRating??(record.rating||0),sku:record.sku||record.inherited_sku||''}}));
    $('capture-group-hint').textContent=group?`当前整款：${group.members.length} 张（含二星标记） · ${group.sku||'待手填SKU'}${group.reason?' · '+group.reason:''}`:record.boundary?'二星标记已读取，正在准备完整区间':'当前素材尚未关联结束分组';
    $('capture-whole-group').disabled=!group&&!record.boundary;$('capture-whole-group').checked=!!group||!!record.boundary;
    knownSku=record.sku||record.inherited_sku||'';
    paintOcrResult(record,result.ocr_task);
    paintFeedbackFromItem(result);
    const conflict=record.metadata_conflict_data,panel=$('capture-conflict');panel.hidden=!conflict&&!record.identity_conflict;
    if(conflict){
      panel.innerHTML=`<strong>XMP与归档记录需要核对</strong><p>${esc(conflict.reason)}</p><p>当前区间 ${result.conflict_file_count} 个文件；只处理此数据集。</p><table><thead><tr><th>字段</th><th>归档确认</th><th>当前图XMP</th></tr></thead><tbody>${['sku','rating','group_id'].map(k=>`<tr><td>${esc(({sku:'SKU',rating:'星级',group_id:'分组'})[k])}</td><td>${esc(conflict.archive[k]??'未提供')}</td><td>${esc(conflict.file.patch[k]??'未提供')}</td></tr>`).join('')}</tbody></table><details><summary>查看实际文件范围（最多30项）</summary>${(result.conflict_files||[]).map(r=>`<p>${esc(r.path)} · 归档 ${esc(r.archive_sku||'未确认')} · 文件 ${esc(r.file_sku||'未提供')}</p>`).join('')}</details><button type="button" data-capture-resolve="xmp" ${!conflict.can_use_file||state?.readonly_reason?'disabled':''}>整款采用当前图的XMP款号</button><button type="button" data-capture-resolve="archive" ${!conflict.can_keep_archive||state?.readonly_reason?'disabled':''}>保留归档确认，修复冲突文件</button>`;
    }else if(record.identity_conflict)panel.textContent=record.identity_conflict+'；不能按旧身份覆盖，请重新核对文件。';
    if(record.ocr_state==='queued')note('当前图片 OCR 已排队，结果将自动显示，可继续浏览其它照片');
    else if(record.metadata_conflict)note(record.metadata_conflict,true);
    else if(!record.sku&&record.ocr_apply_state==='waiting_interval')note(state?.loading?'款号已识别，完整区间读取完成后将自动应用到整款':'款号已识别，正在自动应用到整款');
    else if(record.ocr_apply_state==='review')note(record.ocr_reason||'区间款号需要核对',true);
    else if(record.ocr_state==='manual'||group?.status==='manual')note(record.ocr_reason||'区间已保留，可直接填写SKU并应用整款');
    else if(record.inherited_sku)note('SKU来自当前区间；只补写没有SKU的文件，已有SKU不重写');
    else if(record.boundary&&!record.sku&&!record.ocr)note(state?.loading?'二星已保存，正在读取目录并准备SKU区间…':'二星标签已恢复，OCR排队中；主动标星会自动接续识别');
    else if(record.ocr&&!record.sku)note('OCR已完成，请从候选中核对并确认正确款号');
    else if(record.sku)note(record.file_sku===record.sku?'SKU已在文件中，直接显示，不重复写入。':'SKU来自归档确认，所需补写在后台进行。');
    else note('未标二星的图片作为普通成员保留，不自动识别款号。');
    setEnabled();
  }
  async function poll(force=false){
    syncScope();
    if(FramekeepWorkspace.current()!=='archive'||document.hidden||busy)return;
    if(force){pollAbort?.abort();polling=false;++request}
    if(polling)return;
    const chosen=scopeBody();if(!chosen.root_id)return;
    polling=true;
    const key=JSON.stringify(chosen),sequence=++request;
    const controller=new AbortController();pollAbort=controller;
    try{
      const data=await FramekeepIO.api('/api/archive-capture/state?'+new URLSearchParams({...chosen,capture_mode:$('capture-correction').checked?'1':'0'}),undefined,20000,controller.signal);
      if(sequence!==request||key!==JSON.stringify(scopeBody()))return;
      paintState(data);
      const version=JSON.stringify([key,data.groups.map(g=>[g.id,g.decision_id,g.sku,g.status]),data.sku_projection_pending||0]);
      if(version!==groupVersion){groupVersion=version;document.dispatchEvent(new CustomEvent('framekeep:archive-groups',{detail:{version}}))}
      // 解析进行中任务状态变化不递增版本：每轮轮询强制读取当前素材，动画及时切换阶段。
      await item(ocrActive()||ratingUncertain.has(currentId));
    }catch(error){if(sequence===request&&key===JSON.stringify(scopeBody()))$('capture-access').textContent=error.message+'；可先提交补写，或重新读取目录'}
    finally{if(sequence===request){polling=false;if(pollAbort===controller)pollAbort=null}}
  }
  async function mutate(path,body,message,{reload=true}={}){
    if(busy)return;busy=true;setEnabled();
    try{const result=await api('/api/archive-capture/'+path,{...scopeBody(),...body});note(message||'已保存，文件元数据正在后台同步');itemVersion='';++itemRequest;if(reload)document.dispatchEvent(new CustomEvent('framekeep:metadata-imported'));return result}
    catch(error){itemVersion='';note(error.message,true);throw error}
    finally{busy=false;setEnabled();poll().catch(error=>note(error.message,true))}
  }
  $('capture-edit').onclick=()=>mutate('edit',{enabled:!editing(),customer:$('capture-customer').value,brand:$('capture-brand').value},editing()?'已结束编辑，可由其它设备接续':'已取得当前数据集编辑权限').catch(()=>{});
  async function confirmSku(code){
    const id=currentId,sku=String(code||'').trim();if(!id||!sku||busy)return;
    const scope=JSON.stringify(scopeBody());manualSubmitting=true;
    writeAbort?.abort();writeAbort=null;writePolling=false;++writeSequence;
    paintWriteProgress({status:'queued',result:{mode:'manual',sku},reason:'正在提交款号，随后写入图片文件…'});
    writeItems={[id]:'queued'};scheduleWriteCards();showFeedback('running',sku,'正在提交元数据写入…','saving');
    const whole=$('capture-whole-group').checked;
    try{
      const result=await mutate('confirm',{item_id:id,sku,whole_group:whole,expected_revision:selected?.record.decision_id||''},`已确认 SKU ${sku}，${whole?'整款 RAW / JPG':'当前素材'}元数据写入已提交`,{reload:false});
      if(result?.write_request)paintWriteRequest(result.write_request,scope);
      if(id===currentId){knownSku=sku;setEnabled()}
      return result;
    }catch(error){if(scope===JSON.stringify(scopeBody()))paintWriteRequest({status:'failed',result:{mode:'manual',sku,item_id:id},reason:error.message},scope)}
    finally{manualSubmitting=false;if(selected&&id===currentId)paintFeedbackFromItem(selected);pollWriteRequest()}
  }
  saveButton.onclick=()=>confirmSku(skuInput.value);
  skuInput.addEventListener('input',setEnabled);
  skuInput.addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.isComposing&&skuDraft()&&!saveButton.disabled){event.preventDefault();confirmSku(skuInput.value)}});
  $('capture-ocr').onclick=async()=>{
    const id=currentId;if(!id||busy)return;
    // 立即解析：与现场一致，点击即显示扫描动画；服务端以最高优先级接续当前图 OCR。
    restartFeedback(id);ocrSubmitting=true;showFeedback('queued','准备解析款号','正在提交当前照片…','submitting');setEnabled();
    try{await mutate('recognize',{item_id:id},'OCR 已开始解析，可继续核对其它照片',{reload:false})}
    catch(error){if(id===currentId)showFeedback('failed','款号解析失败',error.message)}
    finally{ocrSubmitting=false;setEnabled()}
  };
  $('capture-ocr-details').onclick=()=>showDetails(!detailsOpen);
  const writeSummary=counts=>`新增 ${counts.queued||0} · 重试 ${counts.retried||0} · 已在队列 ${counts.pending||0} · 冲突或离线跳过 ${counts.skipped||0}`+(counts.unassigned?` · 无SKU保留 ${counts.unassigned} 张`:'');
  function paintWriteProgress(result){
    const status=result?.status||'idle',counts=result?.result||{},manual=counts.mode==='manual',action=manual?'写入':'补写';
    let phase='',count='',detail='',percent=null,active=false,tone='idle';
    if(status==='queued'||status==='preparing'){
      const away=/请打开此文件夹/.test(result.reason||''),scan=result.scan||{};
      active=!away;tone=away?'waiting':'reading';phase=away?'等待打开此文件夹':manual?'正在提交元数据写入':'正在读取目录';
      if(scan.total&&!away){const done=Math.min(scan.done||0,scan.total);percent=Math.round(100*done/scan.total);count=`${done} / ${scan.total}`}
      detail=result.reason||'补写请求已保存，正在等待目录读取完成';
    }else if(status==='submitted'){
      const p=result.progress;
      if(!p){active=true;tone='waiting';phase='正在读取写入进度';detail=result.progress_error||'正在恢复本次文件写入状态…'}
      else if(!p.total){
        tone='done';phase=counts.unassigned?`补写结束 · ${counts.unassigned} 张无SKU保留`:'无需补写';percent=100;count=counts.already?`${counts.already} 张已在文件中`:'';
        detail=`${result.reason||'已确认款号无需新增补写任务'}（${writeSummary(counts)}）`;
      }else{
        const finished=(p.done||0)+(p.failed||0)+(p.skipped||0);active=(p.running||0)+(p.queued||0)>0;
        percent=Math.round(100*finished/p.total);count=`${p.done||0} / ${p.total}`;
        tone=active?'writing':p.failed?'failed':'done';
        phase=active?(p.running?'正在写入元数据':'排队等待写入'):p.failed?`${action}结束 · ${p.failed} 张失败`:p.skipped?`${action}已由新款号接续`:`${action}完成`;
        detail=(manual?`SKU ${counts.sku} · 共 ${p.total} 张`:`已提交补写：${writeSummary(counts)}`)+(active?` · 写入中 ${p.running||0} · 排队 ${p.queued||0}`:'')+(p.failed?` · 失败 ${p.failed}`:'')+(p.skipped?` · 按新版本另行写入 ${p.skipped}`:'');
        if(p.failures?.length)detail+='\n'+p.failures.slice(0,3).map(f=>`${f.path}：${f.reason}`).join('\n');
      }
      if(counts.unassigned)detail+='\n无SKU照片保持原样，不参与款号补写；临时拍摄无需分配SKU。'+(counts.unassigned_examples?.length?' 例如：'+counts.unassigned_examples.join('、'):'');
    }else if(status==='failed'){tone='failed';phase=action+'未完成';detail=result.reason||action+'请求失败，可重新提交'}
    for(const prefix of ['capture-write','capture-photo-write']){
      const box=$(prefix+'-progress'),fill=$(prefix+'-fill'),track=fill.parentElement;
      box.hidden=status==='idle';box.dataset.phase=tone;track.dataset.active=String(active);
      $(prefix+'-phase').textContent=phase;$(prefix+'-count').textContent=count;
      if(percent==null){track.dataset.indeterminate=String(active);fill.style.width=active?'':'0%';track.removeAttribute('aria-valuenow')}
      else{delete track.dataset.indeterminate;fill.style.width=percent+'%';track.setAttribute('aria-valuenow',String(percent))}
      $(prefix+'-result').textContent=detail;
    }
  }
  function paintWriteCards(){
    writeFrame=0;
    const grid=$('catalog-grid'),now=performance.now();
    for(const [id,until] of writeFading)if(until<=now)writeFading.delete(id);
    for(const card of grid.querySelectorAll('[data-catalog-item]')){
      const id=card.dataset.catalogItem,next=writeItems[id],previous=writeShown.get(id);
      // 写入中/排队→已写入：保留短暂的熄灭动画与“已写入”角标，随后恢复原样。
      if(next==='done'&&previous&&previous!=='done'&&!writeFading.has(id))writeFading.set(id,now+2600);
      const shown=next==='done'?(writeFading.has(id)?'done':''):next||'';
      if(next)writeShown.set(id,next);else writeShown.delete(id);
      if((card.dataset.captureWrite||'')!==shown){if(shown)card.dataset.captureWrite=shown;else delete card.dataset.captureWrite}
    }
    if(writeFading.size)setTimeout(scheduleWriteCards,2700);
  }
  function scheduleWriteCards(){if(!writeFrame)writeFrame=requestAnimationFrame(paintWriteCards)}
  function paintWriteRequest(result,key){
    if(key!==JSON.stringify(scopeBody()))return;
    writeRequest=result;writeScope=key;
    paintWriteProgress(result);
    writeItems=result?.status==='submitted'?result.progress?.items||{}:{};
    scheduleWriteCards();setEnabled();if(selected)paintFeedbackFromItem(selected);
  }
  async function pollWriteRequest(){
    syncScope();
    if(FramekeepWorkspace.current()!=='archive'||document.hidden||manualSubmitting||writePolling||!scopeBody().root_id)return;
    writePolling=true;const chosen=scopeBody(),key=JSON.stringify(chosen),sequence=++writeSequence,controller=new AbortController();writeAbort=controller;
    try{const result=await FramekeepIO.api('/api/archive-capture/metadata-write-state?'+new URLSearchParams(chosen),undefined,5000,controller.signal);if(sequence===writeSequence)paintWriteRequest(result,key)}
    catch{}finally{if(sequence===writeSequence){writePolling=false;if(writeAbort===controller)writeAbort=null}}
  }
  $('capture-write-metadata').onclick=async()=>{
    if(busy)return;
    const chosen=JSON.stringify(scopeBody());busy=true;setEnabled();
    // 点击即显示进度区；请求只持久登记，不触发整页素材列表刷新。
    paintWriteProgress({status:'queued',reason:'补写请求已保存，正在核对目录…'});
    try{const result=await api('/api/archive-capture/write-metadata',scopeBody());paintWriteRequest(result,chosen);note('补写请求已保存，目录读取完成后自动逐张写入')}
    catch(error){if(chosen===JSON.stringify(scopeBody())){paintWriteProgress({status:'failed',reason:error.message});note(error.message,true)}}
    finally{busy=false;setEnabled();pollWriteRequest()}
  };
  $('capture-refresh').onclick=()=>{setEnabled();poll(true);pollWriteRequest()};
  $('capture-retry').onclick=()=>mutate('retry',{},'失败任务已重新入队').catch(()=>{});
  $('capture-cancel-plan').onclick=async()=>{try{await api('/api/folder-archive/cancel',{id:state.freeze_job});await poll()}catch(error){note(error.message,true)}};
  $('capture-day').onchange=()=>state&&paintState(state);
  $('capture-conflict').onclick=event=>{const button=event.target.closest('[data-capture-resolve]');if(button&&selected?.record.metadata_conflict_data)mutate('resolve',{item_id:currentId,choice:button.dataset.captureResolve,conflict_token:selected.record.metadata_conflict_data.token},'已保存来源选择，所需补写在后台执行').catch(()=>{})};
  $('capture-correction').onchange=()=>{itemVersion='';poll()};
  $('capture-candidates').onclick=event=>{const button=event.target.closest('[data-capture-code]');if(!button||busy)return;skuInput.value=button.dataset.captureCode;confirmSku(button.dataset.captureCode)};
  $('capture-ocr-text').onclick=event=>{const button=event.target.closest('[data-capture-line]');if(!button)return;skuInput.value=button.dataset.captureLine;skuInput.focus();setEnabled();note('已填入原文，核对后点击“确定”应用到当前整款')};
  $('capture-history').onclick=event=>{const button=event.target.closest('[data-capture-undo]');if(button)mutate('undo',{operation_id:button.dataset.captureUndo},'已记录撤销，文件补偿正在后台执行').catch(()=>{})};
  const exportStatus=document.createElement('p');exportStatus.id='capture-export-status';exportStatus.setAttribute('role','status');controls.append(exportStatus);
  $('capture-export').onclick=async()=>{
    if(!state||busy)return;
    const query=new URLSearchParams({...scopeBody(),day:$('capture-day').value}).toString();
    const folderName=(state.scope?.path||context().path||'拍摄记录').split(/[\\/]/).filter(Boolean).at(-1)||'拍摄记录';
    const name=(folderName+'_'+($('capture-day').value||'全部日期')+'.xlsx').replace(/[<>:"/\\|?*\x00-\x1f]/g,'_');
    busy=true;setEnabled();$('capture-export').textContent='正在导出…';exportStatus.textContent='请选择文档保存位置…';
    try{
      if(window.FramekeepDesktop?.saveArchiveDocument){
        const saved=await FramekeepDesktop.saveArchiveDocument(name,query,message=>exportStatus.textContent=message);
        exportStatus.textContent=saved?'拍摄文档已保存。':'已取消保存，可重新导出。';return;
      }
      const handle=window.showSaveFilePicker?await showSaveFilePicker({suggestedName:name,types:[{description:'Excel 拍摄文档',accept:{'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':['.xlsx']}}]}):null;
      exportStatus.textContent='正在整理拍摄记录与图片，请稍候…';
      const response=await fetch('/api/archive-capture/document?'+query,{signal:AbortSignal.timeout(600000)});
      if(!response.ok){const error=await response.json();throw new Error(error.error||'文档导出失败')}
      const blob=await response.blob();
      if(handle){const stream=await handle.createWritable();try{await stream.write(blob);await stream.close()}catch(error){await stream.abort().catch(()=>{});throw error}exportStatus.textContent='拍摄文档已保存：'+handle.name;return}
      const url=URL.createObjectURL(blob),link=document.createElement('a');
      const disposition=response.headers.get('Content-Disposition')||'',match=/filename\*=UTF-8''([^;]+)/i.exec(disposition);
      link.href=url;link.download=match?decodeURIComponent(match[1]):name;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);exportStatus.textContent='拍摄文档已生成，已开始下载。';
    }catch(error){exportStatus.textContent=error.name==='AbortError'?'已取消保存，可重新导出。':'导出失败：'+error.message}finally{busy=false;$('capture-export').textContent='导出拍摄文档';setEnabled()}
  };
  document.addEventListener('framekeep:archive-scope',()=>{if(syncScope()){poll();pollWriteRequest()}});
  document.addEventListener('framekeep:archive-selection',event=>{
    const changed=syncScope(),next=event.detail?.item_id||'',switched=next!==currentId;currentId=next;selected=null;itemVersion='';++itemRequest;
    $('capture-group-hint').textContent=currentId?'正在读取当前素材资料…':'';
    if(switched){$('capture-candidates').replaceChildren();$('capture-ocr-text').replaceChildren();detailsAuto='';showFeedback('');if(!detailsOpen)showDetails(false)}
    // select() 在派发本事件后才写入款号：等其同步完成后以输入框现值作为已保存款号。
    queueMicrotask(()=>{if(currentId===next&&!selected){knownSku=document.activeElement===skuInput?knownSku:skuInput.value.trim();setEnabled()}});
    setEnabled();if(changed){poll();pollWriteRequest()}item(true).catch(error=>note(error.message,true));
  });
  const toolbar=document.createElement('div');toolbar.className='capture-batch-tools';
  toolbar.innerHTML='<span>Ctrl 多选 · Shift 连选</span><button id="capture-select-page" type="button">全选已加载素材</button><button id="capture-clear-picks" type="button">清除选择</button><span id="capture-picked-count"></span><button id="capture-batch" type="button" disabled>批量指定 SKU</button>';
  document.querySelector('.catalog-main .catalog-toolbar').after(toolbar);
  function paintPicked(){
    const cards=[...$('catalog-grid').querySelectorAll('[data-catalog-item]')],visible=new Set(cards.map(c=>c.dataset.catalogItem));
    picked=new Set([...picked].filter(id=>visible.has(id)));
    for(const card of cards)card.classList.toggle('capture-picked',picked.has(card.dataset.catalogItem));
    $('capture-picked-count').textContent=picked.size?'已选 '+picked.size+' 项':'';setEnabled();
  }
  $('catalog-grid').addEventListener('click',event=>{
    const card=event.target.closest('[data-catalog-item]');if(!card)return;
    const id=card.dataset.catalogItem;
    if(event.shiftKey&&anchor){const cards=[...$('catalog-grid').querySelectorAll('[data-catalog-item]')].map(c=>c.dataset.catalogItem),a=cards.indexOf(anchor),b=cards.indexOf(id);if(a>=0)for(const value of cards.slice(Math.min(a,b),Math.max(a,b)+1))picked.add(value)}
    else if(event.ctrlKey||event.metaKey){if(picked.has(id))picked.delete(id);else picked.add(id)}
    else{picked.clear();picked.add(id)}
    anchor=id;paintPicked();
  },true);
  new MutationObserver(()=>{paintPicked();if(Object.keys(writeItems).length||writeFading.size)scheduleWriteCards()}).observe($('catalog-grid'),{childList:true,subtree:true});
  $('capture-select-page').onclick=()=>{picked=new Set([...$('catalog-grid').querySelectorAll('[data-catalog-item]')].map(c=>c.dataset.catalogItem));paintPicked()};
  $('capture-clear-picks').onclick=()=>{picked.clear();paintPicked()};
  const dialog=document.createElement('dialog');dialog.id='capture-batch-dialog';
  dialog.innerHTML='<header><strong>批量指定 SKU</strong><button id="capture-batch-close" type="button" aria-label="关闭">×</button></header><p>修改所选素材及同一数据集内的配对副本，以预览清单为准。</p><label>SKU 款号<input id="capture-batch-sku" maxlength="80"></label><p id="capture-batch-message" role="status"></p><div id="capture-batch-preview"></div><button id="capture-batch-submit" type="button" class="primary">预览修改</button>';
  document.body.append(dialog);let preview=null;
  $('capture-batch').onclick=()=>{preview=null;$('capture-batch-preview').replaceChildren();$('capture-batch-message').textContent=`已选 ${picked.size} 项`;$('capture-batch-submit').textContent='预览修改';$('capture-batch-sku').value='';dialog.showModal()};
  $('capture-batch-close').onclick=()=>dialog.close();$('capture-batch-sku').oninput=()=>{preview=null;$('capture-batch-preview').replaceChildren();$('capture-batch-submit').textContent='预览修改'};
  $('capture-batch-submit').onclick=async()=>{
    const button=$('capture-batch-submit');button.disabled=true;
    try{
      if(!preview){preview=await api('/api/archive-capture/batch-preview',{...scopeBody(),item_ids:[...picked],sku:$('capture-batch-sku').value});$('capture-batch-preview').innerHTML='<table><thead><tr><th>相对路径</th><th>原款号</th><th>新款号</th></tr></thead><tbody>'+preview.rows.map(row=>`<tr><td>${esc(row.path)}${row.paired?'（配对副本）':''}</td><td>${esc(row.before||'未确认')}</td><td>${esc(row.after)}</td></tr>`).join('')+'</tbody></table>';button.textContent=`确认修改 ${preview.count} 项`}
      else{await mutate('batch-apply',{preview_id:preview.preview_id});dialog.close();picked.clear();paintPicked()}
    }catch(error){$('capture-batch-message').textContent=error.message}finally{button.disabled=false}
  };
  // 标星保存队列：界面先显示新星级，请求按顺序逐个提交；同一张在提交前连续改星只保存最后一次。
  const ratingText=rating=>rating?`${rating} 星`:'清除星级';
  function ratingStatus(id,text){if(id===currentId)$('catalog-rating-status').textContent=text}
  function scheduleRatingReload(){clearTimeout(ratingReload);ratingReload=setTimeout(()=>FramekeepIO.reloadRatings().catch(()=>{}),150)}
  async function drainRatings(){
    try{
      while(ratingQueue.size){
        const [id,job]=ratingQueue.entries().next().value;ratingQueue.delete(id);
        try{
          await savingRating(id,job);
        }finally{if(!ratingQueue.has(id)&&!ratingUncertain.has(id))ratingPending.delete(id)}
      }
    }finally{ratingWorker=null;scheduleRatingReload();poll().catch(()=>{})}
  }
  async function savingRating(id,job){
    try{
      const result=await api('/api/archive-capture/rating',{...job.scope,item_id:id,rating:job.rating,correction:job.correction});
      ratingConfirmed.set(id,job.rating);
      if(!ratingQueue.has(id)){
        document.dispatchEvent(new CustomEvent('framekeep:archive-rating-saved',{detail:{item_id:id,rating:job.rating}}));
        ratingStatus(id,result?.applied_existing_sku?'已沿用已有款号并应用整款':result?.preparing?'二星已保存，正在准备SKU区间…':result?.ocr_queued?'二星已保存，OCR正在排队解析…':job.rating?`已保存 ${job.rating} 星`:'已清除星级');
        if(id===currentId){
          note(job.rating===2&&job.correction?'二星已保存，正在准备区间并识别款号':'星级已保存，文件正在后台同步');
          if(job.rating===2&&job.correction){
            if(result?.applied_existing_sku)showFeedback('running','沿用区间已有款号','正在应用到当前整款…','saving');
            else if(result?.ocr_queued)showFeedback('queued','款号已进入解析',result.preparing?'二星已保存，目录读取期间同步解析标签文字…':'二星已保存，SKU 区间已划分，正在读取标签文字…','image');
          }
          itemVersion='';item(true).catch(error=>note(error.message,true));
        }
      }
      job.resolve(result);
    }catch(error){
      if(error.name==='TimeoutError'){
        if(!ratingQueue.has(id)){
          ratingUncertain.set(id,job.rating);itemVersion='';
          ratingStatus(id,'保存结果尚未确认，正在核对；请勿重复提交');
          if(id===currentId){note('请求超时，后台可能仍在保存；正在重新读取星级核对');if(job.rating===2)showFeedback('queued','正在核对二星保存结果','后台可能仍在处理，请稍候…','submitting')}
        }
        job.reject(error);return;
      }
      if(!ratingQueue.has(id)){
        // 保存失败：恢复到最后一次确认成功的星级，并说明原因。
        const back=ratingConfirmed.get(id)??0;
        document.dispatchEvent(new CustomEvent('framekeep:archive-rating-saved',{detail:{item_id:id,rating:back}}));
        ratingStatus(id,`标记未完成，已恢复为${back?back+' 星':'无星'}：`+error.message);
        window.Framekeep?.notify?.('评级未保存：'+error.message);
        if(id===currentId){note(error.message,true);if(job.rating===2)showFeedback('failed','二星未保存',error.message)}
      }
      job.reject(error);
    }
  }
  window.FramekeepArchiveCapture={
    rate(id,rating,previous=0){
      ratingUncertain.delete(id);
      if(!ratingConfirmed.has(id))ratingConfirmed.set(id,previous||0);
      const correction=$('capture-correction').checked,prior=ratingQueue.get(id);
      // 尚未提交的旧改动被新星级取代，视为完成，不再回退界面。
      if(prior){ratingQueue.delete(id);prior.resolve({superseded:true})}
      ratingPending.set(id,rating);
      ratingStatus(id,editing()?`已标记 ${ratingText(rating)} · 正在保存…`:`已标记 ${ratingText(rating)} · 正在取得目录编辑权并保存…`);
      if(id===currentId)note(rating===2&&correction?'正在保存二星标记并准备SKU区间…':'正在保存星级…');
      // 与现场拍摄一致：按下 2 星立即出现解析动画，保存后服务端立即解析并自动划分 SKU 区间。
      if(id===currentId){if(rating===2&&correction&&(!selected||selected.record.media_type==='image')){restartFeedback(id);showFeedback('queued','准备解析款号','正在保存二星标记并划分 SKU 区间…','submitting')}else showFeedback('')}
      return new Promise((resolve,reject)=>{
        ratingQueue.set(id,{rating,correction,scope:scopeBody(),resolve,reject});
        if(!ratingWorker)ratingWorker=drainRatings();
      });
    },
    pending:id=>ratingPending.has(id),
    refresh:poll,showPhoto:()=>{tab('photo');poll()}
  };
  $('sku-database-grid').addEventListener('click',event=>{const button=event.target.closest('[data-capture-details]');if(button)FramekeepArchiveOpenItem(button.dataset.captureDetails).catch(error=>note(error.message,true))});
  setInterval(poll,1500);setInterval(pollWriteRequest,1000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll()});poll();
})();
