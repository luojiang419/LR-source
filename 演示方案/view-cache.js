/* 已保存的页面先恢复；真实读取独立更新，写入沿用原接口。 */
(() => {
  'use strict';
  const paths=new Set(['/api/sku','/api/sku/library','/api/sku/documents/overview','/api/parsing','/api/selection/tree','/api/folders','/api/inventory/records','/api/providers','/api/video-preview','/api/gpu-media','/api/preview-cache','/api/intelligence/config','/api/intelligence/state','/api/intelligence/search-state','/api/intelligence/facets']);
  const initial=window.FRAMEKEEP_SESSION?.viewBootstrap||{};
  let liveToken=window.FRAMEKEEP_SESSION?.requestToken,scope=window.FRAMEKEEP_SESSION?.id||'',generation=0,refreshTimer=null;
  const values=new Map(Object.entries(initial.values||{})),images=new Map(Object.entries(initial.images||{})),pending=new Map(),retries=new Set(),fetchedAt=new Map();
  const ready=window.FRAMEKEEP_SESSION?Promise.resolve():fetch('/api/view-bootstrap?scope=quick',{signal:AbortSignal.timeout(3000)}).then(r=>r.ok?r.json():null).then(data=>{if(!data)return;liveToken=data.request_token;for(const [k,v] of Object.entries(data.values||{}))values.set(k,v);for(const [k,v] of Object.entries(data.images||{}))images.set(k,v)}).catch(()=>{});
  const clone=value=>JSON.parse(JSON.stringify(value));
  function key(path){
    const url=new URL(path,location.href);if(!paths.has(url.pathname))return null;
    for(const name of ['client','sequence','background','fresh'])url.searchParams.delete(name);
    url.searchParams.sort();return url.pathname+(url.searchParams.size?'?'+url.searchParams.toString():'');
  }
  function ensureScope(){const next=window.FRAMEKEEP_SESSION?.id||'';if(next!==scope){scope=next;invalidate()}}
  function invalidate(path){
    if(path==='/api/inventory/usb')return;
    const affected=key=>{
      if(!path)return true;
      if(path.startsWith('/api/inventory/'))return key.startsWith('/api/inventory/');
      if(path.startsWith('/api/providers/'))return key==='/api/providers'||key==='/api/parsing';
      if(path.startsWith('/api/video-preview'))return key==='/api/video-preview';
      if(path.startsWith('/api/preview-cache'))return key==='/api/preview-cache';
      if(path.startsWith('/api/intelligence/'))return key.startsWith('/api/intelligence/');
      if(path.startsWith('/api/catalog/'))return key.startsWith('/api/sku/library')||key.startsWith('/api/intelligence/');
      return ['/api/sku','/api/parsing','/api/folders','/api/selection/'].some(prefix=>key.startsWith(prefix));
    };
    const keepDisplay=path?.startsWith('/api/session');
    generation++;for(const key of values.keys())if(affected(key)){if(!keepDisplay)values.delete(key);fetchedAt.delete(key)}for(const [key,job] of pending)if(affected(key)){job.invalidated=true;job.controller.abort();pending.delete(key)}
  }
  function editing(){const node=document.activeElement;return node?.matches('textarea,select,input:not([type=search]),[contenteditable=true]')}
  const owners=path=>path.startsWith('/api/sku/library')?['sku-database']:path.startsWith('/api/sku')?['selection','progress']:path.startsWith('/api/inventory')?['inventory']:path.startsWith('/api/intelligence')?['retrieval']:path==='/api/parsing'?['analysis']:path==='/api/selection/tree'||path==='/api/folders'?['selection']:['settings'];
  function updated(path){
    clearTimeout(refreshTimer);refreshTimer=setTimeout(()=>{
      if(!document.hidden&&!editing()&&owners(new URL(path,location.href).pathname).includes(window.FramekeepWorkspace?.current()))window.FramekeepWorkspace?.refresh?.();
    },32);
    document.dispatchEvent(new CustomEvent('framekeep:view-updated',{detail:{path}}));
  }
  function decorate(data,path){const result=clone(data);if(path.startsWith('/api/intelligence/'))result.request_token=window.FRAMEKEEP_SESSION?.requestToken||liveToken;return result}
  function waitFor(promise,signal){
    if(!signal)return promise;
    if(signal.aborted)return Promise.reject(signal.reason);
    // 调用方只取消自己的等待，不能中止其他页面共用的读取。
    return new Promise((resolve,reject)=>{
      const abort=()=>{signal.removeEventListener('abort',abort);reject(signal.reason)};
      signal.addEventListener('abort',abort,{once:true});
      promise.then(value=>{signal.removeEventListener('abort',abort);resolve(value)},error=>{signal.removeEventListener('abort',abort);reject(error)});
    });
  }
  async function read(path,fetchLive,signal){
    await ready;if(signal?.aborted)throw signal.reason;ensureScope();const id=key(path),force=new URL(path,location.href).searchParams.get('fresh')==='1';
    if(force){generation++;values.delete(id);fetchedAt.delete(id);const old=pending.get(id);if(old){old.invalidated=true;old.controller.abort();pending.delete(id)}}
    const version=generation,had=values.has(id);
    if(had&&Date.now()-(fetchedAt.get(id)||0)<750)return decorate(values.get(id),path);
    let job=pending.get(id);
    if(!job){
      const controller=new AbortController(),combined=controller.signal,jobScope=scope;
      job={controller,promise:null,invalidated:false};pending.set(id,job);
      job.promise=Promise.resolve().then(()=>fetchLive(combined)).then(data=>{
        if(combined.aborted)throw combined.reason;
        if(version!==generation)return data;
        const clean=clone(data);delete clean.request_token;
        if(!clean.error&&!clean.loading){const changed=JSON.stringify(values.get(id))!==JSON.stringify(clean);values.delete(id);values.set(id,clean);fetchedAt.set(id,Date.now());while(values.size>32){const old=values.keys().next().value;values.delete(old);fetchedAt.delete(old)}if(had&&changed)updated(path)}
        return data;
      }).catch(error=>{
        // 写后失效需要新结果；旧读取的订阅者接到同一份新请求，避免把内部取消当作失败。
        if(job.invalidated&&scope===jobScope){const next=new URL(path,location.href);next.searchParams.delete('fresh');return read(next.pathname+next.search,fetchLive)}
        throw error;
      }).finally(()=>{if(pending.get(id)===job)pending.delete(id)});
    }
    if(had){
      // 已有内容的后台轮询保持静默，避免顶栏状态随每次请求闪现。
      job.promise.catch(()=>{if(version===generation&&!job.controller.signal.aborted&&owners(new URL(path,location.href).pathname).includes(window.FramekeepWorkspace?.current())){const retry=setTimeout(()=>{retries.delete(retry);if(version===generation&&!job.controller.signal.aborted)read(path,fetchLive).catch(()=>{})},3000);retries.add(retry)}});return decorate(values.get(id),path);
    }
    return waitFor(job.promise,signal);
  }
  function imageKey(value){
    if(!value||value.startsWith('data:')||value.startsWith('blob:'))return null;
    let url;try{url=new URL(value,location.href)}catch{return null}
    if(url.origin!==location.origin)return null;
    if(url.pathname==='/api/inventory/image')for(const k of [...url.searchParams.keys()]){if(!['id','kind','size'].includes(k))url.searchParams.delete(k)}
    else if(url.pathname.startsWith('/api/preview/')||url.pathname.startsWith('/api/sku/library/preview/')){
      if(!url.searchParams.has('v')&&!url.searchParams.has('cached'))return null;
      if(Number(url.searchParams.get('size')||480)>600)return null;
      if(url.searchParams.has('v'))url.searchParams.delete('cached');
    }else return null;
    url.searchParams.sort();return url.pathname+'?'+url.searchParams.toString();
  }
  const fullImages=new Map();
  function upgrade(image,source){
    const previous=fullImages.get(image);
    if(previous?.dataset.viewSource===source||image.dataset.viewPreviewResolved===source||window.FramekeepWorkspace?.current()!=='selection')return;
    if(previous){previous.onload=previous.onerror=null;previous.removeAttribute('src');fullImages.delete(image)}
    const full=new Image();full.decoding='async';full.fetchPriority='high';fullImages.set(image,full);
    full.dataset.viewSource=source;
    full.onload=()=>{fullImages.delete(image);if(image.isConnected&&image.dataset.viewFullSource===source){image.dataset.viewPreviewResolved=source;image.src=source}};
    full.onerror=()=>fullImages.delete(image);full.src=source;
  }
  function fallback(source){
    let url;try{url=new URL(source,location.href)}catch{return null}
    if(!url.pathname.startsWith('/api/preview/')||!url.searchParams.has('v'))return null;
    for(const [key,payload] of images){const cached=new URL(key,location.href);if(cached.pathname===url.pathname&&cached.searchParams.get('v')===url.searchParams.get('v'))return payload}
    return null;
  }
  function hydrateImage(image){
    if(image.tagName!=='IMG')return;
    if(image.getAttribute('src')===image.dataset.viewPreviewResolved)return;
    const source=image.getAttribute('src'),cached=images.get(imageKey(source));
    if(source&&!source.startsWith('data:')&&image.dataset.viewFullSource&&source!==image.dataset.viewFullSource){
      const previous=fullImages.get(image);if(previous){previous.onload=previous.onerror=null;previous.removeAttribute('src');fullImages.delete(image)}
      delete image.dataset.viewFullSource;delete image.dataset.viewPreviewResolved;
    }
    if(cached&&source!==cached){image.dataset.viewPreviewSource=source;image.src=cached}
    else if(source&&!source.startsWith('data:')&&image.closest('#selection-page')){
      const small=fallback(source);if(small){image.dataset.viewFullSource=source;image.src=small;upgrade(image,source)}
    }
    const deferred=image.dataset.previewSrc,payload=images.get(imageKey(deferred));
    if(payload&&deferred!==payload){image.dataset.viewPreviewSource=deferred;image.dataset.previewSrc=payload}
  }
  const observer=new MutationObserver(records=>{for(const record of records){if(record.type==='attributes')hydrateImage(record.target);else for(const node of record.addedNodes)if(node.nodeType===1){hydrateImage(node);node.querySelectorAll('img').forEach(hydrateImage)}}});
  document.querySelectorAll('img').forEach(hydrateImage);
  observer.observe(document.documentElement,{subtree:true,childList:true,attributes:true,attributeFilter:['src','data-preview-src']});
  document.addEventListener('framekeep:workspace',()=>{for(const [id,job] of pending)if(!owners(new URL(id,location.href).pathname).includes(window.FramekeepWorkspace?.current())){job.controller.abort();pending.delete(id)}
    if(window.FramekeepWorkspace?.current()==='selection')document.querySelectorAll('#selection-page img[data-view-full-source]').forEach(image=>upgrade(image,image.dataset.viewFullSource));
    else{for(const full of fullImages.values()){full.onload=full.onerror=null;full.removeAttribute('src')}fullImages.clear()}
  });
  window.addEventListener('pagehide',()=>{observer.disconnect();for(const full of fullImages.values())full.removeAttribute('src');for(const retry of retries)clearTimeout(retry);for(const job of pending.values())job.controller.abort();clearTimeout(refreshTimer)},{once:true});
  window.FramekeepViewCache={key,ready,mutates:path=>!['/api/archive/selection','/api/archive/preview','/api/providers/models','/api/providers/test'].includes(path)&&/^\/api\/(sku\/|providers\/|inventory\/|session|folders|parsing\/|catalog\/|archive\/|intelligence\/config|video-preview|preview-cache)/.test(path),accepts:path=>!!key(path),read,invalidate,images};
})();
