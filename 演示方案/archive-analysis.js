/* 设置与归档页共用持久解析状态；阶段顺序由后端保证。 */
(() => {
  'use strict';
  if(!window.FramekeepIO)return;
  const $=id=>document.getElementById(id),api=FramekeepIO.api;
  const css=document.createElement('link');css.rel='stylesheet';css.href='archive-analysis.css';document.head.append(css);
  const card=document.createElement('section');card.id='full-auto-archive-card';card.className='mouse-auto-card';
  card.innerHTML='<div class="mouse-auto-heading"><div><span class="mouse-auto-eyebrow">归档资料持续解析</span><h4 id="full-auto-archive-title">全自动归档解析</h4></div><input id="full-auto-archive" class="toggle-switch" type="checkbox" role="switch" aria-labelledby="full-auto-archive-title" aria-describedby="full-auto-archive-help" disabled></div><p id="full-auto-archive-help">解析全部已添加归档，新增图片和视频自动加入。开启 OCR 时先完成 OCR，再执行多维度解析。</p><p id="full-auto-archive-status" role="status">正在读取配置…</p><button id="full-auto-archive-manage" class="bordered" type="button">查看进度与手动解析</button>';
  $('full-auto-ocr-card')?.after(card);
  const button=document.createElement('button');button.id='catalog-analysis';button.className='bordered';button.textContent='资产库解析';button.type='button';button.setAttribute('aria-haspopup','dialog');
  $('catalog-image-search')?.after(button);
  const dialog=document.createElement('dialog');dialog.id='catalog-analysis-dialog';dialog.setAttribute('aria-labelledby','catalog-analysis-title');
  dialog.innerHTML=`<header class="dialog-heading"><div><h2 id="catalog-analysis-title">资产库解析</h2><p class="dialog-copy">全部已添加归档 · 不受当前搜索与分页限制</p></div><button id="catalog-analysis-close" type="button" class="close" aria-label="关闭资产库解析">×</button></header>
    <section class="archive-analysis-phase"><span id="catalog-analysis-phase">等待开始</span><p id="catalog-analysis-order"></p><progress id="catalog-analysis-progress" max="100" aria-label="归档解析进度"></progress></section>
    <label class="dialog-copy"><input id="catalog-analysis-enable-ocr" type="checkbox"> 归档 OCR：先识别图片文字，再进行视觉解析并入库</label>
    <div class="archive-analysis-metrics"><div><span>已发现</span><strong id="catalog-analysis-total">0</strong></div><div><span>OCR 完成</span><strong id="catalog-analysis-ocr">0</strong></div><div><span>多维完成</span><strong id="catalog-analysis-done">0</strong></div><div><span>需处理</span><strong id="catalog-analysis-failed">0</strong></div></div>
    <p id="catalog-analysis-models" class="dialog-copy"></p><p id="catalog-analysis-detail" class="dialog-copy"></p>
    <p class="dialog-copy">已完成的图片自动跳过；暂停或关闭软件后，下次继续未完成项。正在处理的图片会先收尾。</p>
    <p id="catalog-analysis-message" role="status"></p><div class="dialog-actions"><button id="catalog-analysis-model-settings" type="button">配置模型</button><button id="catalog-analysis-retry" type="button">重试失败</button><button id="catalog-analysis-pause" type="button">暂停</button><button id="catalog-analysis-start" type="button" class="primary">开始解析</button></div>`;
  document.body.append(dialog);
  const ocrStart=document.createElement('button');ocrStart.id='catalog-analysis-start-ocr';ocrStart.type='button';ocrStart.textContent='手动启动 OCR';ocrStart.hidden=true;
  const visionStart=document.createElement('button');visionStart.id='catalog-analysis-start-vision';visionStart.type='button';visionStart.textContent='手动启动视觉解析';visionStart.className='primary';visionStart.hidden=true;
  dialog.querySelector('.dialog-actions').append(ocrStart,visionStart);
  const decodeStart=document.createElement('button');decodeStart.id='catalog-decode-start';decodeStart.type='button';decodeStart.className='bordered';decodeStart.textContent='全速解码';decodeStart.title='补齐归档缺失缓存；已有有效缓存直接复用，暂停和重启后继续未完成部分';
  const decodePause=document.createElement('button');decodePause.id='catalog-decode-pause';decodePause.type='button';decodePause.className='bordered';decodePause.textContent='暂停解码';decodePause.disabled=true;
  const decodeActions=document.createElement('div');decodeActions.className='catalog-decode-actions';decodeActions.append(decodeStart,decodePause);
  const decodeRetry=document.createElement('button');decodeRetry.id='catalog-decode-retry';decodeRetry.type='button';decodeRetry.textContent='重试解码失败项';decodeRetry.hidden=true;
  const decodeMessage=document.createElement('p');decodeMessage.id='catalog-decode-message';decodeMessage.setAttribute('role','status');
  $('catalog-scan-status').after(decodeActions,decodeRetry,decodeMessage);
  let state=null,busy=false,version=0,desiredEnabled=null,desiredOcr=null,activeRefresh=0;
  let decodeProgress=null;
  function paintDecode(progress=decodeProgress){
    decodeProgress=progress;decodeActions.hidden=decodeRetry.hidden=decodeMessage.hidden=true;
    decodeStart.disabled=decodePause.disabled=decodeRetry.disabled=true;
  }
  let decodeUncertain=null;
  function confirmDecode(progress){
    if(!decodeUncertain||!progress)return false;
    const action=decodeUncertain;
    const confirmed=action==='pause'?progress.manual_paused===true:action==='resume'?progress.manual_paused===false:action==='start'?progress.full_speed===true&&progress.manual_paused===false:false;
    if(confirmed){decodeUncertain=null;decodeMessage.textContent=action==='pause'?'已核对：解码已暂停':action==='resume'?'已核对：解码已继续':'已核对：当前为全速解码'}
    return confirmed;
  }
  document.addEventListener('framekeep:decode-progress',event=>{paintDecode(event.detail);confirmDecode(event.detail)});
  const labels={idle:'等待开始',stopped:'已停止 · 可以继续',paused:'已暂停',decoding:'优先生成缩略图缓存',discovering:'正在发现归档',ocr:'正在执行 OCR',vision:'正在执行多维度解析',completed:'当前归档已完成',attention:'已结束 · 有未完成项',waiting_model:'等待模型配置'};
  function paint(){
    paintDecode();
    if(!state)return;
    const phase=labels[state.phase]||'等待开始',total=state.total||0;
    $('catalog-analysis-phase').textContent=phase;
    $('catalog-analysis-order').textContent=state.ocr_enabled?'顺序：OCR → 多维度解析 → 自动入库（不同阶段不同时运行）':'归档 OCR 未开启，仅执行多维度解析';
    $('catalog-analysis-enable-ocr').checked=busy&&desiredOcr!==null?desiredOcr:!!state.ocr_enabled;$('catalog-analysis-enable-ocr').disabled=busy;
    $('catalog-analysis-total').textContent=total;$('catalog-analysis-ocr').textContent=state.ocr_enabled?state.ocr_completed:'—';
    $('catalog-analysis-done').textContent=state.completed;$('catalog-analysis-failed').textContent=state.failed;
    $('catalog-analysis-models').textContent=`视觉模型：${state.models.vision||'未选用'}${state.ocr_enabled?' · OCR：'+(state.models.ocr||'未选用'):''}`;
    $('catalog-analysis-detail').textContent=`待处理 ${state.pending} · 在途 ${state.active} · 已取消 ${state.cancelled||0}${state.discovering&&state.running&&!state.paused?' · 正在分批发现文件，数量会继续增加':''}`;
    const progress=$('catalog-analysis-progress');
    if(state.discovering&&state.running&&!state.paused)progress.removeAttribute('value');
    else progress.value=total?Math.min(100,(state.completed+(state.ocr_enabled?state.ocr_completed:0))/(total*(state.ocr_enabled?2:1))*100):0;
    $('catalog-analysis-message').textContent=state.error||'';
    $('catalog-analysis-start').textContent=state.paused||state.pending?'继续解析':'开始解析';
    $('catalog-analysis-start').disabled=busy||state.running&&!state.paused&&state.phase!=='waiting_model';
    $('catalog-analysis-pause').disabled=busy||!state.running||state.paused;
    $('catalog-analysis-retry').disabled=busy||!state.failed;
    const manual=!!state.manual_stages;
    $('catalog-analysis-enable-ocr').closest('label').hidden=manual;$('catalog-analysis-start').hidden=manual;
    ocrStart.hidden=visionStart.hidden=!manual;
    const blocked=busy||state.can_start===false||state.running&&!state.paused;
    ocrStart.disabled=visionStart.disabled=blocked;
    ocrStart.title=visionStart.title=state.preview_blocker||'';
    decodeRetry.hidden=!state.decode?.failed;
    if(manual){
      $('catalog-analysis-retry').disabled=busy||!state.failed||!state.run_stage;
      $('catalog-analysis-order').textContent='缩略图缓存完成 → 用户手动启动 OCR 或视觉解析；单个阶段完成后停止。';
      $('catalog-analysis-message').textContent=state.preview_blocker||state.error||'';
      $('catalog-analysis-ocr').textContent=state.ocr_completed;
      if(state.phase==='completed')$('catalog-analysis-phase').textContent=`${state.run_stage==='ocr'?'OCR':'视觉解析'}已完成 · 等待手动操作`;
      if(!state.discovering)progress.value=total?Math.min(100,(state.run_stage==='ocr'?state.ocr_completed:state.completed)/total*100):0;
      if(state.counts_ready===false){
        for(const id of ['catalog-analysis-total','catalog-analysis-ocr','catalog-analysis-done','catalog-analysis-failed'])$(id).textContent='—';
        $('catalog-analysis-detail').textContent='模型阶段未启动；已有结果已保留，手动启动后核对数量。';
        progress.removeAttribute('value');
      }
    }
  }
  async function refresh(force=false){
    if(!force&&!dialog.open&&!['archive','analysis','settings'].includes(window.FramekeepWorkspace?.current()||'selection'))return;
    if((busy||activeRefresh)&&!force)return;const request=++version;++activeRefresh;
    try{const next=await api('/api/intelligence/pipeline');if(request!==version)return;state=next;paint()}
    catch(error){if(request===version){$('full-auto-archive-status').textContent=error.message;$('catalog-analysis-message').textContent=error.message}}
    finally{--activeRefresh}
  }
  async function action(path,body,after){
    if(busy)return;desiredEnabled=typeof body.enabled==='boolean'?body.enabled:null;desiredOcr=typeof body.ocr_enabled==='boolean'?body.ocr_enabled:null;busy=true;++version;paint();let error='';
    if(path==='/api/catalog/decode'){
      decodeUncertain=null;decodeMessage.textContent='正在提交解码操作…';
      try{const result=await api(path,body);paintDecode(result.decode);decodeMessage.textContent=''}
      catch(e){
        if(e.name==='TimeoutError'){
          decodeUncertain=body.action;
          decodeMessage.textContent='解码操作响应超时，正在核对实际状态；请勿重复点击。';
          try{const actual=await api('/api/catalog/state');paintDecode(actual.previews);confirmDecode(actual.previews)}catch{}
          if(decodeUncertain)decodeMessage.textContent='解码操作响应超时，结果尚未确认；进度恢复后自动核对。';
        }else{decodeMessage.textContent=e.message;Framekeep.notify(e.message)}
      }finally{busy=false;desiredEnabled=null;desiredOcr=null;paint();void refresh()}
      return;
    }
    try{await api(path,body);if(after)await api('/api/intelligence/control',{action:after});await refresh(true)}
    catch(e){error=e.message;await refresh(true)}
    finally{busy=false;desiredEnabled=null;desiredOcr=null;paint();if(error){$('catalog-analysis-message').textContent=error;if(path==='/api/catalog/decode')decodeMessage.textContent=error;Framekeep.notify(error)}}
  }
  const open=()=>{if(!dialog.open)dialog.showModal();refresh()};
  button.onclick=open;$('full-auto-archive-manage').onclick=open;$('catalog-analysis-close').onclick=()=>dialog.close();
  $('full-auto-archive').onchange=event=>action('/api/intelligence/config',{enabled:event.target.checked});
  $('catalog-analysis-enable-ocr').onchange=event=>action('/api/intelligence/config',{ocr_enabled:event.target.checked});
  $('catalog-analysis-start').onclick=()=>action('/api/intelligence/control',{action:state?.paused?'resume':'start'});
  $('catalog-analysis-pause').onclick=()=>action('/api/intelligence/control',{action:'pause'});
  $('catalog-analysis-retry').onclick=()=>action('/api/intelligence/control',{action:'retry_failed'},'resume');
  $('catalog-analysis-model-settings').onclick=()=>{dialog.close();$('assistant-settings').click()};
  ocrStart.onclick=()=>action('/api/intelligence/control',{action:'start_ocr'});
  visionStart.onclick=()=>action('/api/intelligence/control',{action:'start_vision'});
  decodeStart.onclick=()=>action('/api/catalog/decode',{action:'start'});
  decodePause.onclick=()=>action('/api/catalog/decode',{action:decodeProgress?.manual_paused?'resume':'pause'});
  decodeRetry.onclick=()=>action('/api/catalog/decode',{action:'retry_failed'});
  for(const name of ['framekeep:workspace','framekeep:full-auto-ocr'])document.addEventListener(name,()=>refresh());
  const timer=setInterval(()=>{if(!document.hidden&&(dialog.open||state?.running||!$('app-settings-page')?.classList.contains('hidden')))refresh()},2000);
  window.addEventListener('pagehide',()=>clearInterval(timer),{once:true});refresh();
})();
