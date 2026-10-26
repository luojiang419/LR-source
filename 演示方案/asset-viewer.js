(() => {
  'use strict';
  if(!window.FramekeepIO)return;
  const $=id=>document.getElementById(id),esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const dialog=document.createElement('dialog');dialog.id='asset-image-viewer';dialog.setAttribute('aria-labelledby','asset-viewer-title');
  dialog.innerHTML='<header class="asset-viewer-heading"><div><span id="asset-viewer-kind"></span><h2 id="asset-viewer-title"></h2></div><button id="asset-viewer-close" aria-label="退出全屏浏览">关闭 <kbd>Esc</kbd></button></header><div class="asset-viewer-body"><div id="asset-viewer-stage" tabindex="0" role="region" aria-label="图片视口，滚轮缩放，放大后拖拽平移"><img id="asset-viewer-thumbnail" alt="" draggable="false" hidden><img id="asset-viewer-image" alt="" draggable="false" hidden><span id="asset-viewer-loading" aria-hidden="true"></span><p id="asset-viewer-image-status" role="status"></p></div><aside class="asset-viewer-info" aria-labelledby="asset-viewer-info-title"><div class="asset-viewer-info-heading"><span>PHOTO DETAILS</span><h3 id="asset-viewer-info-title"></h3></div><div id="asset-viewer-fields"></div><p id="asset-viewer-info-status" role="status"></p></aside></div><footer class="asset-viewer-toolbar"><div class="asset-viewer-navigation"><button id="asset-viewer-prev" aria-label="上一张图片">←</button><span id="asset-viewer-position"></span><button id="asset-viewer-next" aria-label="下一张图片">→</button></div><div class="asset-viewer-zoom"><button id="asset-viewer-minus" aria-label="缩小图片">−</button><output id="asset-viewer-scale" title="相对于适应窗口的缩放比例">100%</output><button id="asset-viewer-plus" aria-label="放大图片">＋</button><button id="asset-viewer-fit">适应窗口</button></div><p>滚轮缩放 · 拖拽平移</p></footer>';
  document.body.append(dialog);
  const stage=$('asset-viewer-stage');
  const playback=document.createElement('div');playback.className='asset-viewer-playback';playback.hidden=true;
  playback.innerHTML='<button id="asset-video-play" type="button" aria-label="暂停视频">暂停</button><output id="asset-video-time">0:00 / --:--</output><input id="asset-video-seek" type="range" min="0" max="0" step="0.01" value="0" aria-label="视频播放进度" disabled><button id="asset-video-mute" type="button" aria-label="静音">静音</button>';
  dialog.querySelector('.asset-viewer-zoom').after(playback);
  const viewerHint=dialog.querySelector('.asset-viewer-toolbar>p');
  let videoContext=null;
  const clock=seconds=>{seconds=Math.max(0,Math.floor(seconds||0));return (seconds>=3600?Math.floor(seconds/3600)+':':'')+String(Math.floor(seconds/60)%60).padStart(seconds>=3600?2:1,'0')+':'+String(seconds%60).padStart(2,'0')};
  const videoValid=context=>videoContext===context&&dialog.open&&context.request===sequence;
  function releaseVideo(context){
    if(!context)return;clearTimeout(context.seekTimer);context.video.pause();context.video.removeAttribute('src');context.video.load();context.video.remove();
    FramekeepIO.api('/api/catalog/video/stop',{session:context.session}).catch(()=>{});
  }
  function stopVideo(){const previous=videoContext;videoContext=null;releaseVideo(previous)}
  function videoPosition(context){return context.seekTarget??(context.offset+(context.video.currentTime||0))}
  function paintVideo(context){
    if(!videoValid(context))return;
    const position=videoPosition(context),total=context.total;
    $('asset-video-time').value=clock(position)+' / '+(total?clock(total):'--:--');
    const slider=$('asset-video-seek');slider.disabled=!total;slider.max=String(total||0);
    if(!context.dragging)slider.value=String(Math.min(total||0,position));
    $('asset-video-play').textContent=context.wanted?'暂停':'播放';$('asset-video-play').setAttribute('aria-label',context.wanted?'暂停视频':'播放视频');
    $('asset-video-mute').textContent=context.video.muted?'开启声音':'静音';$('asset-video-mute').setAttribute('aria-label',context.video.muted?'开启声音':'静音');
  }
  function videoMessage(context,text,state='loading'){
    if(!videoValid(context))return;
    stage.dataset.previewState=state;stage.setAttribute('aria-busy',String(state==='loading'));
    $('asset-viewer-loading').hidden=state!=='loading';$('asset-viewer-image-status').textContent=text;
  }
  async function playVideo(context){
    if(!videoValid(context))return;
    context.wanted=true;paintVideo(context);
    try{await context.video.play();if(!videoValid(context)||!context.wanted)context.video.pause()}
    catch(error){if(videoValid(context)&&error.name!=='AbortError'){context.wanted=false;paintVideo(context);videoMessage(context,'点击播放按钮或按空格开始播放','preview')}}
  }
  function loadVideo(item,request,offset=0,wanted=true,previous=null){
    stopVideo();clearImages();FramekeepAssetLoading.pause();
    const video=document.createElement('video');video.id='asset-viewer-video';video.playsInline=true;video.preload='auto';video.poster=item.preview;
    video.muted=previous?.muted||false;
    const context={item,request,video,session:crypto.randomUUID(),offset,wanted,total:previous?.total||Number(item.duration_ms||0)/1000,dragging:false,seekTimer:null,seekTarget:null};videoContext=context;
    stage.append(video);stage.classList.remove('is-zoomed');
    videoMessage(context,'正在准备视频，可继续查看右侧资料…');paintVideo(context);
    video.onloadeddata=()=>{if(!videoValid(context))return;videoMessage(context,'','ready');if(context.wanted)playVideo(context)};
    video.onplaying=()=>videoMessage(context,'','ready');
    video.onwaiting=()=>videoMessage(context,'正在缓冲视频…');
    video.ontimeupdate=()=>paintVideo(context);
    video.onended=()=>{if(!videoValid(context))return;context.wanted=false;context.total=context.total||(context.offset+video.currentTime);paintVideo(context);videoMessage(context,'播放结束，按空格重新播放','preview')};
    video.onerror=async()=>{
      if(!videoValid(context))return;
      context.wanted=false;paintVideo(context);
      let reason='视频无法播放，请检查原文件是否在线或编码是否受支持';
      try{const state=await FramekeepIO.api('/api/catalog/video/state?'+new URLSearchParams({session:context.session}));reason=state.error||reason}catch{}
      videoMessage(context,reason+'；可切换素材或重新点击播放。','error');
    };
    video.src='/api/catalog/video/stream/'+encodeURIComponent(item.id)+'?'+new URLSearchParams({session:context.session,start:offset.toFixed(3)});
    if(wanted)playVideo(context);else video.load();
  }
  function seekVideo(target,delay=0){
    const context=videoContext;if(!context||!context.total)return;
    target=clamp(target,0,Math.max(0,context.total-.05));context.seekTarget=target;paintVideo(context);clearTimeout(context.seekTimer);
    context.seekTimer=setTimeout(()=>{if(videoValid(context))loadVideo(context.item,context.request,target,context.wanted,{total:context.total,muted:context.video.muted})},delay);
  }
  function toggleVideo(){
    const context=videoContext;if(!context)return;
    if(context.wanted){context.wanted=false;context.video.pause();paintVideo(context)}
    else if(context.video.ended||context.video.error)loadVideo(context.item,context.request,context.video.ended?0:videoPosition(context),true,{total:context.total,muted:context.video.muted});
    else playVideo(context);
  }
  $('asset-video-play').onclick=toggleVideo;
  $('asset-video-mute').onclick=()=>{if(videoContext){videoContext.video.muted=!videoContext.video.muted;paintVideo(videoContext)}};
  $('asset-video-seek').oninput=event=>{if(videoContext){videoContext.dragging=true;$('asset-video-time').value=clock(Number(event.target.value))+' / '+clock(videoContext.total)}};
  $('asset-video-seek').onchange=event=>{if(videoContext){videoContext.dragging=false;seekVideo(Number(event.target.value))}};
  let image=$('asset-viewer-image'),thumbnail=$('asset-viewer-thumbnail'),replaceTimer;
  $('asset-viewer-loading').innerHTML=FramekeepAssetLoading.markup('');
  let rows=[],index=0,kind='archive',origin=null,sequence=0,controller=null,drag=null,requestSharpPreview=null;
  let metadataEditor=null,detailsTimer=null;
  const metadataWrites=new Map();
  let scale=1,x=0,y=0,fitWidth=0,fitHeight=0;
  const clamp=(value,min,max)=>Math.max(min,Math.min(max,value));
  const dateText=value=>value?String(value).replace('T',' '):'未提供';
  const bytes=value=>value!==null&&value!==undefined&&value!==''&&Number.isFinite(Number(value))&&Number(value)>=0?Number(value)>=1048576?(Number(value)/1048576).toFixed(2)+' MB':Math.round(Number(value)/1024)+' KB':'未提供';
  const duration=value=>Number.isFinite(Number(value))?`${Math.floor(Number(value)/60000)}:${String(Math.floor(Number(value)%60000/1000)).padStart(2,'0')}`:'未提供';
  const field=(name,value)=>`<div class="asset-viewer-field"><dt>${esc(name)}</dt><dd>${esc(value??'未提供')}</dd></div>`;
  function baseFields(item){return '<dl>'+field('文件名',item.name)+(kind==='archive'?field('文件大小',bytes(item.size)):'')+field('拍摄时间',dateText(item.time))+field('SKU 款号',item.sku||'未确认')+'</dl>'}
  function info(item,data){
    if(kind==='sku-database'){
      $('asset-viewer-fields').innerHTML=baseFields({...item,sku:data.selected_sku||item.sku})+`<section class="asset-viewer-ocr"><h4>OCR 原文本</h4>${data.target_capture_id&&data.target_capture_id!==data.capture_id?`<p class="asset-viewer-ocr-source">区间标签来源：${esc(data.source_name)}</p>`:''}<pre>${esc(data.text||'暂无 OCR 原文')}</pre></section>`;
      return;
    }
    const row={...item,...data.item},capture=data.capture||{},media=data.media||{},sources={manual:'人工确认',embedded:'文件内嵌元数据',path_rule:'路径候选（未 OCR）',review:'待复核',unmatched:'未识别',archive:'最终归档'};
    if(videoContext&&videoContext.item.id===row.id){videoContext.total=Number(media.duration_ms||row.duration_ms||0)/1000;paintVideo(videoContext)}
    const sourceFile=(data.files||[]).find(file=>file.path===row.path);
    const tag=(...names)=>Object.entries(data.tags||{}).find(([key])=>names.includes(key.split(':').pop()))?.[1];
    const fps=tag('VideoFrameRate','FrameRate'),audio=tag('AudioFormat','AudioCodecID','AudioEncoding');
    const technical=row.media_type==='video'?field('容器格式',media.media_format||row.media_format)+field('视频编码',media.codec||row.codec||'等待本机读取')+field('时长',duration(media.duration_ms??row.duration_ms))+field('帧率',fps?fps+' fps':null)+field('音频编码',audio)+field('音频声道',tag('AudioChannels','ChannelCount'))+field('音频采样率',tag('AudioSampleRate','AudioSamplingRate')):row.media_type==='design'?field('设计格式',row.media_format||row.extension):'';
    $('asset-viewer-fields').innerHTML='<dl>'+field('文件名',row.name)+field('文件大小',bytes(sourceFile?.size??row.size))+field('素材类型',row.media_type==='video'?'专业视频':row.media_type==='design'?'Adobe / 设计源文件':'图片 / RAW')+field('拍摄时间',dateText(capture.time))+field('SKU 款号',row.sku||'未确认')+(row.media_type==='image'?field('星级',row.rating>0?'★'.repeat(row.rating):'未标星'):'')+field('尺寸',capture.width&&capture.height?`${capture.width} × ${capture.height}`:'未提供')+technical+field('相机',[capture.make,capture.camera].filter(Boolean).join(' ')||'未提供')+field('ISO',capture.iso)+field('光圈',capture.aperture?'f/'+capture.aperture:null)+field('快门',capture.shutter?`${capture.shutter<1?'1/'+Math.round(1/capture.shutter):capture.shutter} 秒`:null)+field('款号来源',sources[row.source]||'其他记录')+field('原图路径',row.path||row.relative)+'</dl><section class="asset-viewer-files"><h4>素材文件</h4>'+(data.files||[]).map(file=>`<div><strong>${esc(file.name)}</strong><span>${esc(bytes(file.size))} · ${file.online?'在线':'离线'}</span><small>${esc(file.path)}</small><small>文件修改时间：${esc(file.modified?new Date(file.modified*1000).toLocaleString('zh-CN'):'未提供')}</small></div>`).join('')+'</section>';
    const description=data.description||{};
    const extra=document.createElement('section');extra.className='asset-viewer-files';
    extra.innerHTML='<h4>已入库资料</h4><dl>'+field('标题',description.title||'未提供')+field('描述',description.description||'未提供')+field('关键词',(description.keywords||[]).join('、')||'未提供')+field('颜色标签',description.label||'未提供')+field('镜头',capture.lens)+field('焦距',capture.focal_length?capture.focal_length+' mm':null)+'</dl>';
    $('asset-viewer-fields').append(extra);
    if(data.tags&&Object.keys(data.tags).length){
      const tags=document.createElement('details');tags.className='asset-viewer-files';
      tags.innerHTML='<summary>全部已读取资料</summary><dl>'+Object.entries(data.tags).map(([key,value])=>field(key,typeof value==='object'?JSON.stringify(value):value)).join('')+'</dl>';
      $('asset-viewer-fields').append(tags);
    }
    $('asset-viewer-info-status').textContent=data.metadata_error||(data.metadata_status&&data.metadata_status!=='ready'?'资料正在后台入库，完成后自动显示；可继续浏览。':'');
    if(data.metadata_status==='failed'){
      const retry=document.createElement('button');retry.type='button';retry.textContent='重新读取资料';
      retry.onclick=()=>readDetails(item,sequence,true);$('asset-viewer-info-status').append(' ',retry);
    }
    if(row.media_type==='image'&&(!data.metadata_status||data.metadata_status==='ready'))loadMetadataEditor(row,sequence);
  }
  async function metadataValue(item,refresh=false,background=false){
    let value=await FramekeepIO.api('/api/catalog/metadata/'+encodeURIComponent(item.id)+(refresh?'?refresh=1':''));
    for(let attempt=0;['queued','running'].includes(value.metadata_status)&&attempt<120;attempt++){
      await new Promise(resolve=>setTimeout(resolve,700));
      if(!background&&(!dialog.open||rows[index]?.id!==item.id))throw new Error('图片已切换');
      value=await FramekeepIO.api('/api/catalog/metadata/'+encodeURIComponent(item.id));
    }
    if(value.metadata_status&&value.metadata_status!=='ready')throw new Error(value.metadata_error||'后台资料尚未完成，请稍后重新读取');
    return value;
  }
  async function loadMetadataEditor(item,request,refresh=false){
    const section=document.createElement('section');section.className='asset-viewer-metadata';
    section.innerHTML='<h4>图片元数据</h4><p role="status">正在读取图片与 XMP…</p>';$('asset-viewer-fields').append(section);
    try{
      await metadataWrites.get(item.id)?.catch(()=>{});
      const value=await metadataValue(item,refresh);
      if(request!==sequence||!section.isConnected)return;
      section.innerHTML=`<h4>图片元数据</h4><form><label>标题<input name="title" maxlength="1024" value="${esc(value.title)}"></label><label>描述<textarea name="description" maxlength="16000" rows="3">${esc(value.description)}</textarea></label><label>关键词<textarea name="keywords" rows="2" placeholder="每行一个，或用逗号分隔">${esc(value.keywords.join('\n'))}</textarea></label><label>颜色标签<input name="label" maxlength="100" list="asset-metadata-colors" value="${esc(value.label)}" placeholder="选择或输入标签"><datalist id="asset-metadata-colors"><option value="红色"><option value="黄色"><option value="绿色"><option value="蓝色"><option value="紫色"></datalist></label><p class="workspace-caption">编辑后自动保存，保留原有调色和其他元数据。</p><p class="workspace-caption">${value.sidecars.length?'已读取 XMP：'+value.sidecars.map(esc).join('、'):'保存时自动创建 XMP 侧车'}</p><div class="asset-actions"><button type="submit" class="bordered">立即保存</button><button type="button" data-reload class="bordered">重新读取</button></div><p data-status role="status">${value.writable?'元数据已加载':'此格式暂不支持写入'}</p></form>`;
      const form=section.querySelector('form'),status=section.querySelector('[data-status]');
      const context={item,section,form,status,token:value.token,version:0,saved:0,timer:null,busy:false};metadataEditor=context;
      if(!value.writable)for(const control of form.elements)control.disabled=true;
      form.oninput=()=>{context.version++;status.textContent='已修改，正在等待自动保存…';clearTimeout(context.timer);context.timer=setTimeout(()=>saveMetadataEditor(context),900)};
      form.onsubmit=event=>{event.preventDefault();saveMetadataEditor(context)};
      form.onkeydown=event=>{if(event.key!=='Escape')event.stopPropagation()};
      section.querySelector('[data-reload]').onclick=()=>{clearTimeout(context.timer);if(context.busy)return;context.saved=context.version;section.remove();loadMetadataEditor(item,request,true)};
    }catch(error){if(request===sequence&&section.isConnected)section.querySelector('[role=status]').textContent='元数据读取失败：'+error.message}
  }
  async function saveMetadataEditor(context){
    clearTimeout(context.timer);
    if(context.busy||context.version===context.saved)return;
    context.busy=true;
    const work=(async()=>{
      try{
        while(context.version!==context.saved){
          const version=context.version,elements=context.form.elements;
          const metadata={title:elements.title.value,description:elements.description.value,keywords:elements.keywords.value.split(/[\n,，;；]/).map(value=>value.trim()).filter(Boolean),label:elements.label.value};
          context.status.textContent='正在保存元数据…';
          const result=await FramekeepIO.api('/api/catalog/metadata',{item_id:context.item.id,metadata,token:context.token},120000);
          let operation=result;
          while(operation.jobs.some(job=>['queued','running'].includes(job.status))){
            await new Promise(resolve=>setTimeout(resolve,500));
            operation=await FramekeepIO.api('/api/metadata/operations/'+encodeURIComponent(result.operation.id));
          }
          const failed=operation.jobs.find(job=>job.status!=='applied');
          if(failed)throw new Error(failed.reason||'元数据未完成写入');
          const fresh=await metadataValue(context.item,true,true);
          context.token=fresh.token;context.saved=version;context.status.textContent='已自动保存元数据';
        }
      }catch(error){context.status.textContent='保存失败：'+error.message}
      finally{context.busy=false;metadataWrites.delete(context.item.id)}
    })();
    metadataWrites.set(context.item.id,work);await work;
  }
  function flushMetadataEditor(){if(metadataEditor){saveMetadataEditor(metadataEditor);metadataEditor=null}}
  function paint(){
    const limitX=Math.max(0,(fitWidth*scale-stage.clientWidth)/2),limitY=Math.max(0,(fitHeight*scale-stage.clientHeight)/2);
    x=clamp(x,-limitX,limitX);y=clamp(y,-limitY,limitY);
    for(const layer of [thumbnail,image])layer.style.transform=`translate(-50%,-50%) translate(${x}px,${y}px) scale(${scale})`;
    stage.classList.toggle('is-zoomed',scale>1);$('asset-viewer-scale').value=Math.round(scale*100)+'%';
    $('asset-viewer-minus').disabled=scale<=1;$('asset-viewer-plus').disabled=scale>=16;
  }
  function layout(){
    if(!dialog.open||!stage.clientWidth||!stage.clientHeight)return;
    const current=!image.hidden&&image.naturalWidth?image:thumbnail;
    if(!current.naturalWidth)return;
    for(const layer of [thumbnail,image]){
      if(!layer.naturalWidth)continue;
      const fit=Math.min(stage.clientWidth/layer.naturalWidth,stage.clientHeight/layer.naturalHeight);
      layer.style.width=layer.naturalWidth*fit+'px';layer.style.height=layer.naturalHeight*fit+'px';
    }
    const fit=Math.min(stage.clientWidth/current.naturalWidth,stage.clientHeight/current.naturalHeight);
    fitWidth=current.naturalWidth*fit;fitHeight=current.naturalHeight*fit;paint();
  }
  function clearImages(){
    clearTimeout(replaceTimer);
    requestSharpPreview=null;
    for(const layer of [thumbnail,image]){layer.onload=layer.onerror=null;layer.removeAttribute('src');layer.hidden=true}
  }
  function viewerPreviewEdge(){
    return 2048;
  }
  function loadImages(item,request){
    const small=new URL(item.preview,location.href);small.searchParams.set('size','420');
    const edge=viewerPreviewEdge();
    const large=new URL(item.preview,location.href);large.searchParams.set('size',String(edge));
    // 只复用相同带版本号 URL 的已显示小图，避免读取到另一张图片。
    const cached=[...document.querySelectorAll('.catalog-thumb img')].find(img=>img.complete&&img.naturalWidth&&new URL(img.currentSrc,location.href).href===small.href);
    FramekeepAssetLoading.pause();clearImages();
    const fresh=(old,id)=>{const next=new Image();next.id=id;next.alt=item.name;next.draggable=false;next.hidden=true;next.decoding='async';next.fetchPriority='high';old.replaceWith(next);return next};
    thumbnail=fresh(thumbnail,'asset-viewer-thumbnail');image=fresh(image,'asset-viewer-image');
    const low=thumbnail,full=image,valid=()=>request===sequence&&dialog.open;
    let ready=false,failed=false,lowReady=false,lowFailed=false;
    const status=()=>{
      if(!valid())return;
      const unavailable=failed&&(lowReady||lowFailed);
      stage.dataset.previewState=ready?'ready':unavailable?'error':lowReady?'preview':'loading';
      stage.setAttribute('aria-busy',String(!ready&&!unavailable));
      $('asset-viewer-loading').hidden=ready||lowReady||unavailable;
      $('asset-viewer-image-status').textContent=ready?'':unavailable?(lowReady?'大图无法加载，已保留缩略图；可继续切图或稍后重试':'图片无法加载，请检查文件是否在线；可继续切图或按 Esc 退出'):lowReady?'预览已显示 · 正在载入清晰大图…':'正在准备预览，首次读取可能需要一点时间…';
    };
    low.onload=()=>{if(!valid()||ready)return;lowReady=true;low.hidden=false;layout();status()};
    low.onerror=()=>{if(valid()&&!ready){lowFailed=true;status()}};
    full.onload=async()=>{
      try{await full.decode()}catch{if(valid())full.onerror();return}
      if(!valid())return;
      ready=true;full.hidden=false;layout();status();
      requestSharpPreview?.();
      if(matchMedia('(prefers-reduced-motion: reduce)').matches)low.hidden=true;
      else replaceTimer=setTimeout(()=>{if(valid())low.hidden=true},200);
    };
    full.onerror=()=>{if(!valid()||ready)return;failed=true;status()};
    requestSharpPreview=()=>{
      if(!valid()||!ready||scale<=1||edge>=2048||full.dataset.sharp==='2048')return;
      const sharp=new URL(item.preview,location.href);sharp.searchParams.set('size','2048');
      full.dataset.sharp='2048';
      const candidate=new Image();candidate.decoding='async';candidate.fetchPriority='high';
      candidate.onload=async()=>{
        try{await candidate.decode()}catch{return}
        if(valid())full.src=sharp.href;
      };
      candidate.onerror=()=>{if(valid())delete full.dataset.sharp};candidate.src=sharp.href;
    };
    status();
    let smallSource=small.href;
    if(cached){
      // 复制已经解码的像素，即使禁用 HTTP 缓存也不再次等待缩略图请求。
      try{const canvas=document.createElement('canvas');canvas.width=cached.naturalWidth;canvas.height=cached.naturalHeight;canvas.getContext('2d').drawImage(cached,0,0);smallSource=canvas.toDataURL('image/jpeg',.94)}catch{}
    }
    low.src=smallSource;
    if(low.complete&&low.naturalWidth)low.onload();
    full.src=large.href;
  }
  function endDrag(){if(!drag)return;const id=drag.id;drag=null;stage.classList.remove('panning');if(stage.hasPointerCapture(id))stage.releasePointerCapture(id)}
  function fit(){endDrag();scale=1;x=0;y=0;paint()}
  function zoom(next,clientX,clientY){
    endDrag();next=clamp(next,1,16);const rect=stage.getBoundingClientRect();
    const ax=(clientX??rect.left+rect.width/2)-rect.left-rect.width/2,ay=(clientY??rect.top+rect.height/2)-rect.top-rect.height/2;
    x=ax-(ax-x)*next/scale;y=ay-(ay-y)*next/scale;scale=next;paint();requestSharpPreview?.();
  }
  async function show(next){
    clearTimeout(detailsTimer);
    flushMetadataEditor();
    index=clamp(next,0,rows.length-1);const item=rows[index],request=++sequence;controller?.abort();controller=new AbortController();
    endDrag();fitWidth=fitHeight=0;fit();
    const isVideo=item.media_type==='video';playback.hidden=!isVideo;dialog.querySelector('.asset-viewer-zoom').hidden=isVideo;
    stage.setAttribute('aria-label',isVideo?'视频播放器，空格播放或暂停，左右键前后定位 5 秒':'图片视口，滚轮缩放，放大后拖拽平移');
    viewerHint.textContent=isVideo?'空格播放 / 暂停 · ← / → 前后 5 秒':'滚轮缩放 · 拖拽平移';
    $('asset-viewer-title').textContent=item.name;$('asset-viewer-kind').textContent=kind==='archive'?'资产归档':'SKU 数据库';$('asset-viewer-info-title').textContent=isVideo?'视频详细信息':kind==='archive'?'图片详细信息':'拍摄与 OCR 资料';
    dialog.querySelector('.asset-viewer-info-heading>span').textContent=isVideo?'VIDEO DETAILS':'PHOTO DETAILS';
    $('asset-viewer-position').textContent=`${index+1} / ${rows.length}`;$('asset-viewer-prev').disabled=index===0;$('asset-viewer-next').disabled=index===rows.length-1;
    $('asset-viewer-fields').innerHTML=baseFields(item)+(kind==='sku-database'?'<section class="asset-viewer-ocr"><h4>OCR 原文本</h4><pre>正在读取…</pre></section>':'');
    $('asset-viewer-info-status').textContent=kind==='archive'?'正在同步已入库资料…':'正在读取图片资料…';
    if(isVideo)loadVideo(item,request);else{stopVideo();loadImages(item,request)}
    await readDetails(item,request);
  }
  async function readDetails(item,request,refresh=false){
    clearTimeout(detailsTimer);
    const path=kind==='archive'?'/api/catalog/details/'+encodeURIComponent(item.id)+(refresh?'?refresh=1':''):'/api/sku/ocr/details?'+new URLSearchParams({session_id:item.session_id,capture_id:item.capture_id});
    try{
      const data=await FramekeepIO.api(path,undefined,20000,controller.signal);
      if(request!==sequence||!dialog.open)return;
      $('asset-viewer-info-status').textContent='';info(item,data);
      if(kind==='archive'&&['queued','running'].includes(data.metadata_status))detailsTimer=setTimeout(()=>readDetails(item,request),700);
    }
    catch(error){if(request===sequence&&dialog.open){$('asset-viewer-info-status').textContent='详细资料读取失败：'+error.message;if(kind==='sku-database')$('asset-viewer-fields').querySelector('pre').textContent='OCR 原文暂时无法读取'}}
  }
  let viewerActive=false;
  function cleanup(){if(!viewerActive)return;viewerActive=false;++sequence;clearTimeout(detailsTimer);controller?.abort();stopVideo();endDrag();clearImages();FramekeepAssetLoading.resume();stage.setAttribute('aria-busy','false');rows=[];if(origin?.isConnected)origin.focus({preventScroll:true});origin=null}
  function close(){flushMetadataEditor();if(dialog.open)dialog.close();cleanup()}
  $('asset-viewer-close').onclick=close;dialog.addEventListener('cancel',event=>{event.preventDefault();close()});
  dialog.addEventListener('close',()=>{if(!dialog.open)cleanup()});
  $('asset-viewer-prev').onclick=()=>show(index-1);$('asset-viewer-next').onclick=()=>show(index+1);
  $('asset-viewer-minus').onclick=()=>zoom(scale/1.25);$('asset-viewer-plus').onclick=()=>zoom(scale*1.25);$('asset-viewer-fit').onclick=fit;
  stage.addEventListener('wheel',event=>{if(videoContext)return;event.preventDefault();const delta=event.deltaY*(event.deltaMode===1?16:event.deltaMode===2?stage.clientHeight:1);zoom(scale*Math.exp(-clamp(delta,-600,600)*.002),event.clientX,event.clientY)},{passive:false});
  stage.addEventListener('dblclick',event=>{event.preventDefault();if(videoContext)toggleVideo();else zoom(scale>1?1:2,event.clientX,event.clientY)});
  stage.addEventListener('pointerdown',event=>{if(videoContext||scale<=1||![0,1].includes(event.button))return;event.preventDefault();endDrag();drag={id:event.pointerId,button:event.button,startX:event.clientX,startY:event.clientY,x,y};stage.setPointerCapture(event.pointerId);stage.classList.add('panning')});
  stage.addEventListener('pointermove',event=>{if(!drag||drag.id!==event.pointerId)return;if(!(event.buttons&(drag.button===0?1:4))){endDrag();return}x=drag.x+event.clientX-drag.startX;y=drag.y+event.clientY-drag.startY;paint();event.preventDefault()});
  for(const name of ['pointerup','pointercancel','lostpointercapture'])stage.addEventListener(name,endDrag);
  stage.addEventListener('dragstart',event=>event.preventDefault());stage.addEventListener('auxclick',event=>{if(event.button===1)event.preventDefault()});
  window.addEventListener('blur',endDrag);new ResizeObserver(()=>{endDrag();layout()}).observe(stage);
  dialog.addEventListener('keydown',event=>{
    if(event.target.closest('input:not([type=range]),textarea,select,[contenteditable=true]')||event.ctrlKey||event.metaKey||event.altKey)return;
    if(videoContext&&[' ','ArrowLeft','ArrowRight','Home'].includes(event.key)){
      event.preventDefault();event.stopImmediatePropagation();
      if(event.key===' '){if(!event.repeat)toggleVideo()}
      else seekVideo(event.key==='Home'?0:videoPosition(videoContext)+(event.key==='ArrowRight'?5:-5),100);
      return;
    }
    if(!videoContext&&event.key===' '){event.preventDefault();event.stopImmediatePropagation();if(!event.repeat)close();return}
    const actions={Escape:close,ArrowLeft:()=>show(index-1),ArrowRight:()=>show(index+1),'+':()=>zoom(scale*1.25),'=':()=>zoom(scale*1.25),'-':()=>zoom(scale/1.25),'0':fit,Home:fit};
    if(actions[event.key]){event.preventDefault();event.stopPropagation();actions[event.key]()}
  },true);
  window.addEventListener('pagehide',stopVideo);
  document.addEventListener('framekeep:workspace',close);
  window.FramekeepAssetViewer={open(mode,items,id,trigger){
    const selected=items.findIndex(item=>item.id===id);if(selected<0)return;kind=mode;rows=items.map(item=>({...item}));origin=trigger||document.activeElement;
    viewerActive=true;if(!dialog.open)dialog.showModal();show(selected);$('asset-viewer-close').focus({preventScroll:true});
  }};
})();
