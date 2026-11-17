(() => {
  'use strict';
  window.FramekeepDesignPreviews={revision:'',url(item){
    const url=new URL(item.preview,location.href);
    if(item.media_type==='design'){
      url.searchParams.set('design_preview','1');
      if(this.revision)url.searchParams.set('thumbnail_revision',this.revision);
    }
    return url;
  }};
  // SVG 只使用 currentColor，不绑定渐变 ID，多个占位可独立使用。
  const icon='<svg class="asset-loading-art" viewBox="0 0 96 96" fill="none" aria-hidden="true"><circle cx="48" cy="48" r="40" stroke="currentColor" stroke-opacity=".12"/><g class="asset-loading-orbit"><path d="M48 8a40 40 0 0 1 40 40" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="88" cy="48" r="3" fill="currentColor"/></g><g class="asset-loading-photo" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="27" y="31" width="42" height="34" rx="6"/><circle cx="56" cy="41" r="3"/><path d="m28 57 12-12 13 14 7-7 8 8"/></g><path d="M19 30v-7a4 4 0 0 1 4-4h7m36 58h7a4 4 0 0 0 4-4v-7" stroke="currentColor" stroke-opacity=".3" stroke-linecap="round"/></svg>';
  const markup=(label='正在读取预览')=>`<span class="asset-loading">${icon}<span class="asset-loading-label">${label}</span></span>`;
  const grids=new Map(),active=new Map(),retries=new Map();
  const directReady=new Map();let directBytes=0;
  function sourceFor(url){const key=identity(url),hit=directReady.get(key);if(!hit)return url;directReady.delete(key);directReady.set(key,hit);return hit.data}
  function rememberDirect(img){
    // 带源指纹 v 的地址由HTTP缓存复用，不再在主线程逐张画布转码。
    const direct=new URL(img.dataset.previewSrc,location.href).searchParams;
    if(direct.get('thumbnail_source')!=='file'||direct.has('v'))return;
    if(directReady.has(identity(img.dataset.previewSrc)))return;
    try{
      const canvas=document.createElement('canvas');canvas.width=img.naturalWidth;canvas.height=img.naturalHeight;canvas.getContext('2d').drawImage(img,0,0);
      const data=canvas.toDataURL('image/jpeg',.85),bytes=data.length*2,key=identity(img.dataset.previewSrc);if(bytes>1024*1024)return;
      if(directReady.has(key))directBytes-=directReady.get(key).bytes;
      directReady.delete(key);directReady.set(key,{data,bytes});directBytes+=bytes;
      while(directBytes>32*1024*1024){const oldest=directReady.keys().next().value;directBytes-=directReady.get(oldest).bytes;directReady.delete(oldest)}
    }catch{}
  }
  let paused=false;
  const identity=value=>{const url=new URL(value,location.href);url.searchParams.delete('cached');return url.href};
  function notify(img){for(const grid of grids.values())for(const done of [...(grid.waiters.get(img)||[])])done(img)}
  function state(img,value){
    const host=img.closest('.catalog-thumb');if(!host)return;
    host.dataset.previewState=value;
    const label=host.querySelector('.asset-loading-label');if(label)label.textContent=value==='error'?'预览暂不可用':'正在读取预览';
  }
  function cancel(img){
    const retry=retries.get(img);if(retry){clearTimeout(retry);retries.delete(img)}
    delete img.dataset.previewRetrying;
    const finish=active.get(img);if(!finish)return;
    img.onload=img.onerror=null;img.removeAttribute('src');finish();
  }
  const cachedSource=new WeakMap();
  function cachedPreview(img){
    const src=img.dataset.previewSrc,known=cachedSource.get(img);
    if(known&&known[0]===src)return known[1];
    const value=src.startsWith('data:')||new URL(src,location.href).searchParams.has('cached');
    cachedSource.set(img,[src,value]);return value;
  }
  function pump(){
    if(paused)return;
    const candidates=[];let order=0;
    for(const grid of grids.values()){
      if(!grid.host.isConnected||!grid.host.getClientRects().length)continue;
      const bounds=grid.host.getBoundingClientRect();
      // 只测量观察区内（视口±400px）的图片位置；整页预取的其余图片按页面顺序排队，
      // 不为上千张图片逐次读取布局。
      for(const img of grid.visible){
        if(img.dataset.previewDone)continue;
        const rect=img.getBoundingClientRect(),visible=img.isConnected&&rect.bottom>bounds.top&&rect.top<bounds.bottom;
        // 首屏先于预读边缘，同一层内优先本地缓存，慢图不挡住已就绪图片。
        candidates.push({img,grid,visible,near:true,priority:(visible?0:2)+(cachedPreview(img)?0:1),top:rect.top,order:order++});
      }
      for(const img of grid.waiters.keys()){
        if(img.dataset.previewDone||grid.visible.has(img))continue;
        candidates.push({img,grid,visible:false,near:false,priority:4+(cachedPreview(img)?0:1),top:0,order:order++});
      }
    }
    candidates.sort((a,b)=>a.priority-b.priority||a.top-b.top||a.order-b.order);
    const shown=new Set(candidates.filter(c=>c.visible).map(c=>c.img));
    for(const img of shown)if(active.has(img))img.fetchPriority='high';
    // 滚动后把前台位置让给新视口；后台最多占两路，已完成的图片保持。
    const foreground=candidates.some(({img,visible})=>visible&&!img.dataset.previewRetrying&&!active.has(img));
    const offscreen=[...active.keys()].filter(img=>!shown.has(img));
    if(foreground)for(const img of offscreen.slice(2))cancel(img);
    let background=[...active.keys()].filter(img=>!shown.has(img)).length;
    for(const {img,grid,visible,near} of candidates){
      // 留出同源连接供目录导航和资料请求使用。
      if(active.size>=4)return;
      if((!img.isConnected&&!grid.extras.has(img))||(img.isConnected&&!img.getClientRects().length&&!grid.waiters.has(img))||img.dataset.previewDone||img.dataset.previewRetrying||active.has(img))continue;
      if(!visible&&background>=3)continue;
      if(!visible)background++;
      let done=false,timer;
      const finish=()=>{if(done)return;done=true;clearTimeout(timer);active.delete(img)};
      active.set(img,finish);
      img.onload=()=>{if(active.get(img)!==finish)return;finish();rememberDirect(img);delete img.dataset.previewAttempts;img.dataset.previewDone='ready';state(img,'ready');notify(img);pump()};
      img.onerror=()=>{
        if(active.get(img)!==finish)return;
        finish();const attempts=Number(img.dataset.previewAttempts||0)+1;
        // 视口外预取图失败不立即重试，一张慢图不能拖住整页预取；滚动到附近时再给一次机会。
        if(attempts<3&&img.isConnected&&!paused&&grid.visible.has(img)){
          img.dataset.previewAttempts=String(attempts);img.dataset.previewRetrying='1';state(img,'loading');img.removeAttribute('src');
          retries.set(img,setTimeout(()=>{retries.delete(img);delete img.dataset.previewRetrying;pump()},attempts*350));
        }else{delete img.dataset.previewRetrying;img.dataset.previewDone='error';state(img,'error');notify(img);pump()}
      };
      // 慢存储或挂起连接不能永久占满请求；超时沿用有限重试，旧回调无效。
      timer=setTimeout(()=>{if(active.get(img)===finish){const fail=img.onerror;img.onload=img.onerror=null;img.removeAttribute('src');fail()}},15000);
      img.decoding='async';img.fetchPriority=visible?'high':'low';img.src=sourceFor(img.dataset.previewSrc);
    }
  }
  function watch(host){
    const previous=grids.get(host);
    if(previous){
      const images=[...host.querySelectorAll('img[data-preview-src]')],current=new Set(images),before=new Set(previous.images);
      for(const img of previous.images)if(!current.has(img)){previous.observer.unobserve(img);previous.visible.delete(img);cancel(img);notify(img)}
      previous.images=images;for(const img of images)if(!before.has(img))previous.observer.observe(img);
      pump();return;
    }
    const grid={host,visible:new Set(),images:[...host.querySelectorAll('img[data-preview-src]')],extras:new Set(),waiters:new Map()};
    grid.observer=new IntersectionObserver(entries=>{
      for(const entry of entries){
        const img=entry.target;
        if(entry.isIntersecting){
          grid.visible.add(img);
          // 后台预取时失败的图片进入视口附近后重新尝试一次。
          if(img.dataset.previewDone==='error'&&!img.dataset.previewRevisited){img.dataset.previewRevisited='1';delete img.dataset.previewDone;delete img.dataset.previewAttempts;state(img,'loading')}
        }else{grid.visible.delete(img);if(!grid.waiters.has(img))cancel(img)}
      }
      pump();
    },{root:host,rootMargin:'400px'});
    grids.set(host,grid);for(const img of grid.images)grid.observer.observe(img);
    pump();
  }
  window.FramekeepAssetLoading={markup,watch,sourceFor,clearDirectPreviews(){directReady.clear();directBytes=0},
    prefetch(host,urls,{signal}={}){
      watch(host);const grid=grids.get(host);
      if(signal?.aborted)return Promise.resolve({ready:0,failed:0,cancelled:true});
      const known=new Map([...grid.extras,...grid.images].map(img=>[identity(img.dataset.previewSrc),img]));
      const images=[...new Set(urls.map(url=>{
        const key=identity(url);let img=known.get(key);
        if(!img){img=new Image();img.dataset.previewSrc=url;known.set(key,img);grid.extras.add(img)}
        return img;
      }))];
      return new Promise(resolve=>{
        const pending=new Set(images.filter(img=>!img.dataset.previewDone));let finished=false;
        const finish=()=>{
          if(finished)return;finished=true;signal?.removeEventListener('abort',abort);
          for(const img of images){
            const waiters=grid.waiters.get(img);waiters?.delete(done);if(!waiters?.size)grid.waiters.delete(img);
            if(grid.extras.delete(img))cancel(img);
            else if(signal?.aborted&&!grid.visible.has(img))cancel(img);
          }
          resolve({ready:images.filter(img=>img.dataset.previewDone==='ready').length,failed:images.filter(img=>img.dataset.previewDone==='error').length,cancelled:!!signal?.aborted});
          pump();
        };
        const done=img=>{pending.delete(img);if(!pending.size)finish()};
        const abort=()=>finish();
        for(const img of pending){if(!grid.waiters.has(img))grid.waiters.set(img,new Set());grid.waiters.get(img).add(done)}
        signal?.addEventListener('abort',abort,{once:true});
        if(!pending.size)finish();else pump();
      });
    },
    retry(img,url){
      if(![...grids.values()].some(grid=>grid.images.includes(img)))return false;
      cancel(img);delete img.dataset.previewDone;delete img.dataset.previewAttempts;
      img.dataset.previewSrc=url;state(img,'loading');pump();return true;
    },
    pause(){paused=true;for(const img of new Set([...active.keys(),...retries.keys()]))cancel(img)},
    resume(){paused=false;pump()},
  };
})();
