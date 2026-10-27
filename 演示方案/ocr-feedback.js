(() => {
  'use strict';
  if(!window.FramekeepIO)return;
  const $=id=>document.getElementById(id),surface=document.querySelector('.preview-surface');
  const settings=document.createElement('div');settings.className='workspace-card';
  settings.innerHTML='<header><h4>解析动画</h4></header><div class="workspace-fields"><label for="preview-ocr-position">解析动画位置<select id="preview-ocr-position"><option value="top-right">右上角（默认）</option><option value="top-left">左上角</option><option value="bottom-right">右下角</option><option value="bottom-left">左下角</option><option value="hidden">隐藏</option></select></label><label for="preview-ocr-transparency">解析动画透明度<span class="preview-transparency-control"><input id="preview-ocr-transparency" type="range" min="0" max="100" step="1" value="35"><output id="preview-ocr-transparency-value" for="preview-ocr-transparency">35%</output></span></label><p class="workspace-caption">二星立即反馈，解析完成后在原位保留 SKU。位置与透明度同时用于动画和款号，自动保存；数值越大越透明，100% 完全透明。右栏始终显示清晰的解析状态。</p><div id="ocr-settings-preview" aria-label="解析动画效果预览"></div></div>';
  // 视觉与键盘顺序一致：先排列浮动控件，再显示缓存管理。
  const previewSettings=$('settings-preview'),floatingSettings=['preview-rating-position','preview-zoom-position','preview-histogram-position'].map(id=>$(id)?.closest('.workspace-card')).filter(Boolean);
  previewSettings.querySelector('.settings-section-heading').after(...floatingSettings,settings,$('preview-transparency-help'));
  // 三个实例共用 CSS 时间轴，不用重复 SVG ID 或虚构识别百分比。
  function card(id){
    const node=document.createElement('div');node.id=id;node.className='ocr-feedback';node.hidden=true;
    node.innerHTML='<div class="ocr-feedback-top"><svg class="ocr-scanner" viewBox="0 0 54 64" fill="none" aria-hidden="true"><rect class="ocr-scanner-field" x="7" y="6" width="40" height="52" rx="8"/><g class="ocr-scanner-label" fill="currentColor" stroke="none"><rect x="15" y="18" width="8" height="3" rx="1"/><rect x="26" y="18" width="13" height="3" rx="1"/><rect x="15" y="26" width="18" height="3" rx="1"/><rect x="36" y="26" width="3" height="3" rx="1"/><rect x="15" y="34" width="7" height="3" rx="1"/><rect x="25" y="34" width="14" height="3" rx="1"/><rect x="15" y="42" width="19" height="3" rx="1"/></g><g class="ocr-scanner-sweep"><rect x="4" y="9" width="46" height="11" rx="3" fill="currentColor" fill-opacity=".1" stroke="none"/><path d="M3 20h48"/><circle cx="50" cy="20" r="2" fill="currentColor" stroke="none"/></g></svg><span class="ocr-feedback-copy"><span class="ocr-feedback-eyebrow">OCR / 款号识别</span><strong class="ocr-feedback-title"></strong></span><span class="ocr-feedback-pulse" aria-hidden="true"><i></i><i></i><i></i></span></div><div class="ocr-feedback-note"></div><div class="ocr-feedback-progress"><div class="ocr-feedback-track" role="progressbar" aria-label="当前解析阶段进度"><span class="ocr-feedback-fill"></span></div><div class="ocr-feedback-meter"><span class="ocr-feedback-stage"></span><span class="ocr-feedback-elapsed"></span></div></div><div class="ocr-feedback-stages" aria-label="解析阶段"><span>图片就绪</span><span>模型准备</span><span>OCR 解析</span><span>完成</span></div>';
    return node;
  }
  const sidebar=card('asset-ocr-feedback'),overlay=card('preview-ocr-feedback'),sample=card('ocr-feedback-sample');
  sidebar.setAttribute('role','status');sidebar.setAttribute('aria-live','polite');sidebar.setAttribute('aria-atomic','true');
  overlay.setAttribute('aria-hidden','true');overlay.classList.add('ocr-feedback-overlay');
  $('sku-asset-panel').querySelector('.asset-panel-body').prepend(sidebar);surface.append(overlay);$('ocr-settings-preview').append(sample);
  function paint(node,status,title,note='',progress=null){
    node.hidden=!status;if(!status)return;
    node.dataset.status=status;
    node.querySelector('.ocr-feedback-title').textContent=title;
    node.querySelector('.ocr-feedback-note').textContent=note;
    node.title=[title,note].filter(Boolean).join(' · ');
    const active=['running','queued','retry'].includes(status),phase=progress?.phase||(status==='completed'?'completed':status==='queued'?'submitting':'recognizing');
    node.dataset.phase=phase;node.querySelector('.ocr-feedback-progress').hidden=!active;
    const track=node.querySelector('.ocr-feedback-track'),percent=progress?.percent;
    const measured=typeof percent==='number'&&Number.isFinite(percent)&&percent>=0&&percent<=100;
    track.classList.toggle('indeterminate',!measured);track.dataset.started=String(Date.now()-1000*(progress?.elapsed_seconds||0));
    track.dataset.measured=String(measured);
    if(measured){track.setAttribute('aria-valuenow',String(Math.round(percent)));track.setAttribute('aria-valuemin','0');track.setAttribute('aria-valuemax','100');node.querySelector('.ocr-feedback-fill').style.width=percent+'%'}
    else track.removeAttribute('aria-valuenow');
    node.querySelector('.ocr-feedback-stage').textContent=progress?.progress_label||({submitting:'提交当前照片',image:'准备图片',waiting_slot:'等待推理资源',loading_model:'模型加载中',recognizing:'解析进行中'}[phase]||'等待开始');
    if(measured)node.querySelector('.ocr-feedback-elapsed').textContent=Math.round(percent)+'%';
    const step=status==='completed'?4:['recognizing'].includes(phase)?2:['loading_model','waiting_slot'].includes(phase)?1:0;
    [...node.querySelectorAll('.ocr-feedback-stages span')].forEach((item,i)=>item.dataset.state=i<step?'done':i===step?'active':'pending');
  }
  paint(sample,'running','正在解析款号','读取标签文字，完成后显示 SKU');
  const storageKey='framekeep-ocr-feedback-v1',positions=['top-right','top-left','bottom-right','bottom-left','hidden'];
  let prefs={position:'top-right',transparency:35};
  try{const saved=JSON.parse(localStorage.getItem(storageKey));if(saved&&positions.includes(saved.position))prefs.position=saved.position;if(saved&&typeof saved.transparency==='number'&&Number.isFinite(saved.transparency)&&saved.transparency>=0&&saved.transparency<=100)prefs.transparency=Math.round(saved.transparency)}catch{}
  function applyPrefs(){
    overlay.dataset.position=prefs.position;overlay.style.opacity=sample.style.opacity=String(1-prefs.transparency/100);
    $('preview-ocr-position').value=prefs.position;$('preview-ocr-transparency').value=String(prefs.transparency);$('preview-ocr-transparency-value').value=prefs.transparency+'%';avoidControls();
  }
  function savePrefs(){applyPrefs();try{localStorage.setItem(storageKey,JSON.stringify(prefs))}catch{}}
  $('preview-ocr-position').addEventListener('change',()=>{prefs.position=$('preview-ocr-position').value;savePrefs()});
  $('preview-ocr-transparency').addEventListener('input',()=>{prefs.transparency=Number($('preview-ocr-transparency').value);savePrefs()});
  function avoidControls(){
    if(window.FramekeepPreviewOverlays){FramekeepPreviewOverlays.layout();return}
    overlay.style.setProperty('--ocr-inset','12px');if(overlay.hidden)return;
    const position=prefs.position,edge=position.split('-')[0],right=position.endsWith('right');let inset=12;
    const width=overlay.offsetWidth;
    for(const [kind,node] of [['rating',$('stars')],['zoom',surface.querySelector('.thumbnail-control')]]){
      const other=surface.dataset[kind+'Position'];if(!other||other==='hidden'||!other.startsWith(edge)||!node?.offsetWidth)continue;
      if(other===position||width+node.offsetWidth+36>surface.clientWidth){
        const rect=node.getBoundingClientRect(),box=surface.getBoundingClientRect();
        inset=Math.max(inset,edge==='top'?rect.bottom-box.top+10:box.bottom-rect.top+10);
      }
    }
    overlay.style.setProperty('--ocr-inset',inset+'px');overlay.dataset.side=right?'right':'left';
  }
  window.FramekeepPreviewOverlays?.register(overlay,{position:()=>prefs.position,enabled:()=>prefs.transparency<100});
  const resize=new ResizeObserver(avoidControls);for(const node of [surface,$('stars'),surface.querySelector('.thumbnail-control'),overlay])if(node)resize.observe(node);
  for(const id of ['preview-rating-position','preview-zoom-position'])$(id).addEventListener('change',avoidControls);
  const keyOf=()=>JSON.stringify([FRAMEKEEP_SESSION.id,Framekeep.remoteTarget().photoId]);
  let key=keyOf(),previous={...Framekeep.persistedState().overrides},sid=FRAMEKEEP_SESSION.id,pending=0,generation=0,manualPending=false,lastJob='',pendingJob='';
  function show(status,title,note,progress){for(const node of [sidebar,overlay])paint(node,status,title,note,progress);tickElapsed();avoidControls()}
  function tickElapsed(){for(const node of [sidebar,overlay,sample]){const track=node.querySelector('.ocr-feedback-track');if(!node.hidden&&track.dataset.measured==='false')node.querySelector('.ocr-feedback-elapsed').textContent='已等待 '+Math.max(0,Math.floor((Date.now()-Number(track.dataset.started))/1000))+' 秒'}}
  const elapsedTimer=setInterval(tickElapsed,1000);
  function refresh(){document.dispatchEvent(new CustomEvent('framekeep:ocr-refresh'))}
  function begin(detail){
    if(detail.session_id!==FRAMEKEEP_SESSION.id||detail.capture_id!==Framekeep.remoteTarget().photoId)return null;
    pending=++generation;manualPending=!!detail.running;pendingJob=lastJob;if(detail.running)show('running','正在解析款号','正在读取标签文字…');else show('queued','准备解析款号','正在提交当前照片…');return pending;
  }
  function end(token,error){
    if(token!==pending||!token)return;pending=0;
    if(error)show('failed','款号解析失败',error.message||String(error));else refresh();
  }
  window.FramekeepOcrFeedback={begin,end};
  document.addEventListener('framekeep:selection',()=>{
    const target=Framekeep.remoteTarget(),next=keyOf(),overrides=Framekeep.persistedState().overrides||{};
    const changed=sid===FRAMEKEEP_SESSION.id&&previous[target.photoId]!==overrides[target.photoId];
    if(next!==key){key=next;pending=0;++generation;show('','','')}
    previous={...overrides};sid=FRAMEKEEP_SESSION.id;
    if(!changed)return;
    pending=0;++generation;
    if(overrides[target.photoId]===2){
      const token=begin({session_id:sid,capture_id:target.photoId});
      FramekeepIO.flush().then(()=>end(token),error=>end(token,error));
    }else{show('','','');FramekeepIO.flush().then(refresh,()=>{})}
  });
  document.addEventListener('framekeep:capture-status',event=>{
    const data=event.detail;if(keyOf()!==key||data.session_id!==FRAMEKEEP_SESSION.id||data.capture_id!==Framekeep.remoteTarget().photoId)return;
    if(pending&&(!manualPending||!data.ocr?.job_key||data.ocr.job_key===pendingJob))return;
    lastJob=data.ocr?.job_key||'';
    const ocr=data.ocr||{},status=ocr.status,codes=[...new Set(ocr.codes?.length?ocr.codes:status==='review'?ocr.candidates||[]:[])],code=codes.join(' / ');
    if(['queued','running','retry'].includes(status)){
      const progress=ocr.progress||{},phase=progress.phase;
      const title=status==='retry'?'等待重新解析':status!=='running'?'款号已进入解析':({image:'正在准备标签图片',waiting_slot:'等待推理资源',loading_model:'正在加载 OCR 模型'}[phase]||'正在解析款号');
      const note=progress.message||(phase==='loading_model'?'正在准备模型，完成后自动解析':phase==='waiting_slot'?'等待当前模型空闲，任务会自动继续':status==='running'?'请求已发送，正在读取标签文字…':'即将读取当前照片的标签');
      show(status,title,note,progress);
    }
    else if(['completed','matched','confirmed'].includes(status)&&code)show('completed',code,status==='confirmed'?'SKU · 已确认':'SKU · 解析完成');
    else if(['review','unmatched'].includes(status))show('review',code||'款号待确认',ocr.rule_error||(code?'OCR 候选 · 请核对后应用':ocr.reason||'打开 OCR 资料选择正确款号'));
    else if(status==='waiting_model')show('waiting','等待配置 OCR 模型',ocr.reason||'请在设置中选择可用模型');
    else if(status==='paused'||status==='manual')show('waiting',status==='paused'?'OCR 解析已暂停':'等待手动开始解析',ocr.reason);
    else if(['failed','unknown'].includes(status))show('failed',status==='failed'?'款号解析失败':'解析状态暂不可用',ocr.reason||'请重试或检查模型连接');
    else if(status==='completed')show('review','未识别到款号','可查看 OCR 原文或重新解析');
    else show('','','');
  });
  for(const name of ['framekeep:ocr-result','framekeep:ocr-choice'])document.addEventListener(name,refresh);
  // 同步预加载状态供组件准备面板静默处理，不打开弹窗或切换页面。
  window.FramekeepOcrRuntime={preloading:false};let runtimePolling=false;
  async function pollRuntime(){if(runtimePolling||document.hidden)return;runtimePolling=true;try{const value=await FramekeepIO.api('/api/providers/runtime');FramekeepOcrRuntime.preloading=['preparing','loading'].includes(value.status);document.dispatchEvent(new CustomEvent('framekeep:ocr-runtime',{detail:value}))}catch{}finally{runtimePolling=false}}
  const runtimeTimer=setInterval(pollRuntime,1000);pollRuntime();
  window.addEventListener('pagehide',()=>{resize.disconnect();clearInterval(elapsedTimer);clearInterval(runtimeTimer)},{once:true});applyPrefs();tickElapsed();
})();
