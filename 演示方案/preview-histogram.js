/* 只读预览直方图：按当前照片采样，切图失效旧请求，最多缓存 48 张。 */
(() => {
  'use strict';
  const surface=document.querySelector('.preview-surface');
  if(!surface||!window.Framekeep)return;
  const panel=document.createElement('section');panel.id='preview-histogram';panel.className='preview-histogram';panel.hidden=true;
  panel.setAttribute('aria-label','当前照片预览直方图');
  panel.innerHTML='<header><span>直方图</span><span class="histogram-legend"><i data-channel="red">R</i><i data-channel="green">G</i><i data-channel="blue">B</i></span></header><div class="histogram-plot"><svg viewBox="0 0 256 64" preserveAspectRatio="none" role="img" aria-label="当前照片 RGB 和明度分布"><path class="histogram-grid" d="M64 0V64M128 0V64M192 0V64M0 63.5H256"/><path data-channel="luma"/><path data-channel="red"/><path data-channel="green"/><path data-channel="blue"/></svg><span id="histogram-status" role="status" aria-live="polite"></span></div><footer><span>0</span><span>预览 RGB</span><span>255</span></footer>';
  surface.append(panel);
  const status=panel.querySelector('#histogram-status'),svg=panel.querySelector('svg'),paths=[...svg.querySelectorAll('path[data-channel]')];
  const scratch=document.createElement('canvas'),context=scratch.getContext('2d',{willReadFrequently:true}),cache=new Map();
  const positionKey='framekeep-preview-histogram-position-v1',transparencyKey='framekeep-preview-histogram-transparency-v1';
  const positions=['bottom-right','top-left','top-right','bottom-left','hidden'];
  const positionControl=document.getElementById('preview-histogram-position'),transparencyControl=document.getElementById('preview-histogram-transparency'),transparencyOutput=document.getElementById('preview-histogram-transparency-value');
  let position='bottom-right',transparency=0;
  try{const saved=localStorage.getItem(positionKey);if(positions.includes(saved))position=saved;const value=Number(localStorage.getItem(transparencyKey)??0);if(Number.isFinite(value)&&value>=0&&value<=100)transparency=Math.round(value)}catch{}
  let identity='',generation=0,pendingImage=null,detachLoad=null,queued=false,layoutQueued=false;
  function applyPreferences(){
    panel.dataset.position=position;panel.style.opacity=String(1-transparency/100);
    if(positionControl)positionControl.value=position;
    if(transparencyControl)transparencyControl.value=String(transparency);
    if(transparencyOutput)transparencyOutput.value=transparency+'%';
    schedule();queuePlace();
  }
  positionControl?.addEventListener('change',()=>{position=positions.includes(positionControl.value)?positionControl.value:'bottom-right';try{localStorage.setItem(positionKey,position)}catch{}applyPreferences()});
  transparencyControl?.addEventListener('input',()=>{const value=Number(transparencyControl.value);transparency=Number.isFinite(value)&&value>=0&&value<=100?Math.round(value):0;try{localStorage.setItem(transparencyKey,String(transparency))}catch{}applyPreferences()});
  function clearRequest(){if(detachLoad){detachLoad();detachLoad=null}if(pendingImage){pendingImage.onload=pendingImage.onerror=null;pendingImage.removeAttribute('src');pendingImage=null}}
  function phase(value,message=''){
    panel.dataset.state=value;status.textContent=message;
    if(value!=='ready')for(const path of paths){path.removeAttribute('d');path.removeAttribute('aria-label')}
  }
  function sample(image){
    if(!context||!image.naturalWidth||!image.naturalHeight)throw new Error('无法读取预览像素');
    const scale=Math.min(1,256/Math.max(image.naturalWidth,image.naturalHeight));
    scratch.width=Math.max(1,Math.round(image.naturalWidth*scale));scratch.height=Math.max(1,Math.round(image.naturalHeight*scale));
    context.clearRect(0,0,scratch.width,scratch.height);context.drawImage(image,0,0,scratch.width,scratch.height);
    const pixels=context.getImageData(0,0,scratch.width,scratch.height).data;
    const bins={red:new Uint32Array(256),green:new Uint32Array(256),blue:new Uint32Array(256),luma:new Uint32Array(256)};let total=0;
    for(let i=0;i<pixels.length;i+=4){if(!pixels[i+3])continue;const r=pixels[i],g=pixels[i+1],b=pixels[i+2];bins.red[r]++;bins.green[g]++;bins.blue[b]++;bins.luma[Math.round(.2126*r+.7152*g+.0722*b)]++;total++}
    if(!total)throw new Error('预览没有可统计像素');
    return {bins,total};
  }
  function paint(result){
    const highest=Math.max(1,...Object.values(result.bins).map(bins=>Math.max(...bins)));
    for(const path of paths){
      const channel=path.dataset.channel,bins=result.bins[channel],peak=bins.indexOf(Math.max(...bins));
      path.setAttribute('d','M0 64'+Array.from(bins,(count,index)=>`L${index} ${(64-count/highest*61).toFixed(2)}`).join('')+'L255 64Z');
      const names={red:'红色',green:'绿色',blue:'蓝色',luma:'明度'};
      path.setAttribute('aria-label',`${names[channel]}通道，峰值 ${peak}`);
    }
    panel.dataset.samples=String(result.total);phase('ready');
  }
  function visible(node){if(!node||node.hidden||!node.offsetWidth)return false;const css=getComputedStyle(node);return css.display!=='none'&&css.visibility!=='hidden'}
  function place(){
    layoutQueued=false;if(panel.hidden)return;
    if(window.FramekeepPreviewOverlays){FramekeepPreviewOverlays.layout();return}
    const box=surface.getBoundingClientRect(),width=panel.offsetWidth,height=panel.offsetHeight,fromTop=position.startsWith('top'),fromLeft=position.endsWith('left');
    const left=fromLeft?box.left+12:box.right-12-width,right=left+width;
    let top=fromTop?box.top+12:box.bottom-12-height,bottom=top+height;
    const obstacles=[document.getElementById('stars'),surface.querySelector('.thumbnail-control'),document.getElementById('preview-ocr-feedback')].filter(visible).map(node=>node.getBoundingClientRect());
    for(let attempt=0;attempt<obstacles.length+1;attempt++){
      const collision=obstacles.find(rect=>rect.left<right+8&&rect.right>left-8&&rect.top<bottom+8&&rect.bottom>top-8);
      if(!collision)break;
      if(fromTop){top=collision.bottom+8;bottom=top+height}else{bottom=collision.top-8;top=bottom-height}
    }
    // 极矮预览区域优先保留照片与已有操作。
    panel.classList.toggle('histogram-no-room',top<box.top+8||bottom>box.bottom-8||box.height<height+20);
    panel.style.left=fromLeft?'12px':'auto';panel.style.right=fromLeft?'auto':'12px';
    panel.style.top=fromTop?Math.max(12,top-box.top)+'px':'auto';panel.style.bottom=fromTop?'auto':Math.max(12,box.bottom-bottom)+'px';
  }
  function queuePlace(){if(!layoutQueued){layoutQueued=true;requestAnimationFrame(place)}}
  window.FramekeepPreviewOverlays?.register(panel,{position:()=>position,enabled:()=>transparency<100,optional:true});
  function refresh(){
    queued=false;
    const target=Framekeep.remoteTarget(),session=window.FRAMEKEEP_SESSION;
    const onSelection=!window.FramekeepWorkspace||FramekeepWorkspace.current()==='selection';
    const photo=onSelection&&position!=='hidden'&&transparency<100&&!target.paused&&(window.DEMO_PHOTOS||[]).find(p=>p.id===target.photoId);
    if(!photo){++generation;clearRequest();identity='';panel.hidden=true;phase('empty');return}
    panel.hidden=false;queuePlace();
    const next=JSON.stringify([session?.id,photo.id,photo.preview,photo.previewLarge]);
    if(next===identity)return;
    identity=next;const ticket=++generation;clearRequest();panel.dataset.photoId=photo.id;panel.title=(photo.name||photo.jpg||photo.raw||photo.id)+' · 根据显示预览采样，非 RAW 原始数据';
    if(cache.has(next)){const result=cache.get(next);cache.delete(next);cache.set(next,result);paint(result);return}
    phase('loading','读取中…');
    const consume=image=>{
      if(ticket!==generation||identity!==next)return;
      try{const result=sample(image);cache.set(next,result);if(cache.size>48)cache.delete(cache.keys().next().value);paint(result)}catch{identity='';phase('error','预览不可用')}
    };
    const fail=()=>{if(ticket===generation&&identity===next){identity='';phase('error','预览不可用')}};
    const loaded=[...surface.querySelectorAll('#photos [data-loupe] img,[data-focus] img')].find(img=>(img.closest('[data-loupe]')?.dataset.loupe||img.closest('[data-focus]')?.dataset.focus)===photo.id&&img.complete&&img.naturalWidth);
    if(loaded){consume(loaded);return}
    // 复用正在加载的中央大图；网格中未加载的当前图只请求小预览。
    const current=[...surface.querySelectorAll('#photos [data-loupe]')].find(node=>node.dataset.loupe===photo.id)?.querySelector('img');
    if(current&&!document.getElementById('loupe-view').classList.contains('hidden')){
      const finish=()=>{clearRequest();consume(current)};current.addEventListener('load',finish,{once:true});current.addEventListener('error',fail,{once:true});
      detachLoad=()=>{current.removeEventListener('load',finish);current.removeEventListener('error',fail)};
      if(current.complete){if(current.naturalWidth)finish();else{clearRequest();fail()}}return;
    }
    const source=photo.preview||photo.previewLarge;
    if(!source){fail();return}
    pendingImage=new Image();const image=pendingImage;image.decoding='async';image.onload=()=>{if(ticket!==generation)return;pendingImage=null;image.onload=image.onerror=null;consume(image)};image.onerror=()=>{if(ticket!==generation)return;clearRequest();fail()};image.src=source;
  }
  function schedule(){if(!queued){queued=true;requestAnimationFrame(refresh)}}
  document.addEventListener('framekeep:boot-ready',schedule,{once:true});
  const photoHost=document.getElementById('photos');photoHost?.addEventListener('load',schedule,true);
  document.addEventListener('framekeep:selection',schedule);document.addEventListener('framekeep:workspace',schedule);
  const resize=new ResizeObserver(queuePlace);for(const node of [surface,panel,document.getElementById('stars'),surface.querySelector('.thumbnail-control')])if(node)resize.observe(node);
  const observer=new MutationObserver(queuePlace);observer.observe(surface,{attributes:true,attributeFilter:['class','data-rating-position','data-zoom-position','data-rating-transparency','data-zoom-transparency']});
  const ocr=document.getElementById('preview-ocr-feedback');if(ocr){resize.observe(ocr);observer.observe(ocr,{attributes:true,attributeFilter:['hidden','style','data-position']})}
  window.addEventListener('framekeep:theme',queuePlace);window.addEventListener('resize',queuePlace);
  window.addEventListener('pagehide',()=>{++generation;clearRequest();resize.disconnect();observer.disconnect();photoHost?.removeEventListener('load',schedule,true);cache.clear()},{once:true});
  applyPreferences();
})();
