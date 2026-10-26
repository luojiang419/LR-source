(() => {
  'use strict';
  // SVG 只使用 currentColor，不绑定渐变 ID，多个占位可独立使用。
  const icon='<svg class="asset-loading-art" viewBox="0 0 96 96" fill="none" aria-hidden="true"><circle cx="48" cy="48" r="40" stroke="currentColor" stroke-opacity=".12"/><g class="asset-loading-orbit"><path d="M48 8a40 40 0 0 1 40 40" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="88" cy="48" r="3" fill="currentColor"/></g><g class="asset-loading-photo" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="27" y="31" width="42" height="34" rx="6"/><circle cx="56" cy="41" r="3"/><path d="m28 57 12-12 13 14 7-7 8 8"/></g><path d="M19 30v-7a4 4 0 0 1 4-4h7m36 58h7a4 4 0 0 0 4-4v-7" stroke="currentColor" stroke-opacity=".3" stroke-linecap="round"/></svg>';
  const markup=(label='正在准备预览')=>`<span class="asset-loading">${icon}<span class="asset-loading-label">${label}</span></span>`;
  const grids=new Map(),active=new Map(),retries=new Map();
  let paused=false;
  function state(img,value){
    const host=img.closest('.catalog-thumb');if(!host)return;
    host.dataset.previewState=value;
    const label=host.querySelector('.asset-loading-label');if(label)label.textContent=value==='error'?'预览暂不可用':'正在准备预览';
  }
  function cancel(img){
    const retry=retries.get(img);if(retry){clearTimeout(retry);retries.delete(img)}
    const finish=active.get(img);if(!finish)return;
    img.onload=img.onerror=null;img.removeAttribute('src');finish();
  }
  function pump(){
    if(paused)return;
    for(const grid of grids.values())for(const img of grid.visible){
      if(active.size>=3)return;
      if(!img.isConnected||!img.getClientRects().length||img.dataset.previewDone||img.dataset.previewRetrying||active.has(img))continue;
      let done=false;
      const finish=()=>{if(done)return;done=true;active.delete(img)};
      active.set(img,finish);
      img.onload=()=>{finish();delete img.dataset.previewAttempts;img.dataset.previewDone='ready';state(img,'ready');pump()};
      img.onerror=()=>{
        finish();const attempts=Number(img.dataset.previewAttempts||0)+1;
        if(attempts<3&&img.isConnected&&!paused){
          img.dataset.previewAttempts=String(attempts);img.dataset.previewRetrying='1';state(img,'loading');img.removeAttribute('src');
          retries.set(img,setTimeout(()=>{retries.delete(img);delete img.dataset.previewRetrying;pump()},attempts*350));
        }else{delete img.dataset.previewRetrying;img.dataset.previewDone='error';state(img,'error');pump()}
      };
      img.decoding='async';img.fetchPriority='low';img.src=img.dataset.previewSrc;
    }
  }
  function watch(host){
    const previous=grids.get(host);
    if(previous){previous.observer.disconnect();for(const img of previous.images)cancel(img)}
    const grid={visible:new Set(),images:[...host.querySelectorAll('img[data-preview-src]')]};
    grid.observer=new IntersectionObserver(entries=>{
      for(const entry of entries){if(entry.isIntersecting)grid.visible.add(entry.target);else grid.visible.delete(entry.target)}
      pump();
    },{root:host,rootMargin:'120px'});
    grids.set(host,grid);for(const img of grid.images)grid.observer.observe(img);
    pump();
  }
  window.FramekeepAssetLoading={markup,watch,
    pause(){paused=true;for(const img of new Set([...active.keys(),...retries.keys()]))cancel(img)},
    resume(){paused=false;pump()},
  };
})();
