/* 现场小图仅驻留本次页面内存；虚拟节点重建与布局切换共享同一份字节。 */
(() => {
  'use strict';
  const byteLimit=512*1024*1024,decodedLimit=96*1024*1024,concurrency=6;
  const entries=new Map(),decoded=new Map(),jobs=new Map(),queue=[];
  let bytes=0,decodedBytes=0,active=0,requests=0,hits=0,closed=false;
  const visible=()=>!document.hidden&&!window.FramekeepBoot?.loading&&(!window.FramekeepWorkspace||FramekeepWorkspace.current()==='selection');
  const escape=value=>String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function key(source){
    let url;try{url=new URL(source,location.href)}catch{return null}
    if(url.origin!==location.origin||!url.pathname.startsWith('/api/preview/')||!url.searchParams.has('v'))return null;
    if(Number(url.searchParams.get('size')||480)>600||url.searchParams.has('full')||url.searchParams.has('decode'))return null;
    // instant 只改变读取优先级，不改变小图像素；版本、会话、尺寸等仍参与身份。
    url.searchParams.delete('instant');url.searchParams.delete('retry');url.searchParams.sort();
    return url.pathname+'?'+url.searchParams.toString();
  }
  function touch(id){
    const entry=entries.get(id);if(!entry)return null;
    entries.delete(id);entries.set(id,entry);
    if(decoded.has(id)){const item=decoded.get(id);decoded.delete(id);decoded.set(id,item)}
    return entry;
  }
  function forget(id){
    const entry=entries.get(id);if(!entry)return;
    entries.delete(id);bytes-=entry.bytes;URL.revokeObjectURL(entry.url);
    const image=decoded.get(id);if(image){decodedBytes-=image.bytes;decoded.delete(id)}
  }
  function trim(){
    while(decodedBytes>decodedLimit&&decoded.size){const id=decoded.keys().next().value;decodedBytes-=decoded.get(id).bytes;decoded.delete(id)}
    if(bytes<=byteLimit)return;
    const used=new Set([...document.querySelectorAll('img[data-memory-thumbnail]')].map(image=>image.dataset.memoryThumbnail));
    for(const id of entries.keys()){if(bytes<=byteLimit)break;if(!used.has(id))forget(id)}
  }
  function present(image,id,entry){
    if(!image.isConnected||image.dataset.memoryThumbnail!==id)return;
    image.loading='eager';image.decoding='sync';delete image.dataset.thumbnailError;
    if(image.getAttribute('src')!==entry.url)image.src=entry.url;
  }
  function waiting(job){return [...job.images].filter(image=>image.isConnected&&image.dataset.memoryThumbnail===job.id)}
  function pump(){
    if(closed||!visible())return;
    while(active<concurrency&&queue.length){
      const job=queue.shift();if(!waiting(job).length&&!job.consumers.length){jobs.delete(job.id);continue}
      active++;requests++;
      (async()=>{
        let url;
        try{
          const response=await fetch(job.source,{cache:'no-store',signal:job.controller.signal,priority:job.source.includes('instant=1')?'high':'low'});
          if(!response.ok)throw new Error('缩略图读取失败：'+response.status);
          const blob=await response.blob();url=URL.createObjectURL(blob);
          const image=new Image();image.decoding='async';image.src=url;await image.decode();
          if(closed)return;
          const entry={url,bytes:blob.size};entries.set(job.id,entry);bytes+=blob.size;
          const size=image.naturalWidth*image.naturalHeight*4;decoded.set(job.id,{image,bytes:size});decodedBytes+=size;
          for(const target of waiting(job))present(target,job.id,entry);
          for(const resolve of job.consumers)resolve(entry.url);
          url=null;trim();
        }catch(error){
          for(const resolve of job.consumers)resolve(null);
          if(!closed&&error.name!=='AbortError')for(const image of waiting(job)){
            image.dataset.thumbnailError='true';
            const attempt=Number(image.dataset.thumbnailRetries||0)+1;
            image.dataset.thumbnailRetries=String(attempt);
            if(attempt<=3)setTimeout(()=>{if(image.isConnected&&image.dataset.memoryThumbnail===job.id)request(image,job.id)},250*attempt);
          }
        }finally{
          if(url)URL.revokeObjectURL(url);
          jobs.delete(job.id);active--;pump();
        }
      })();
    }
  }
  function request(image,id){
    if(closed||!image.isConnected||image.dataset.memoryThumbnail!==id)return;
    const entry=touch(id);if(entry){hits++;present(image,id,entry);return}
    let job=jobs.get(id);
    if(!job){
      const source=new URL(image.dataset.memoryThumbnailSource||id,location.href),attempt=Number(image.dataset.thumbnailRetries||0);
      if(attempt)source.searchParams.set('retry',String(attempt));
      job={id,source:source.href,images:new Set(),consumers:[],controller:new AbortController()};jobs.set(id,job);queue.push(job);
    }
    job.images.add(image);pump();
  }
  function load(source){
    const id=key(source);if(!id)return Promise.resolve(source);
    if(closed)return Promise.resolve(null);
    const entry=touch(id);if(entry){hits++;return Promise.resolve(entry.url)}
    let job=jobs.get(id);
    if(!job){job={id,source:new URL(source,location.href).href,images:new Set(),consumers:[],controller:new AbortController()};jobs.set(id,job);queue.push(job)}
    const promise=new Promise(resolve=>job.consumers.push(resolve));pump();return promise;
  }
  const intersection=new IntersectionObserver(records=>{
    for(const {target,isIntersecting} of records)if(isIntersecting){request(target,target.dataset.memoryThumbnail);intersection.unobserve(target)}
  },{rootMargin:'600px 0px',scrollMargin:'600px 0px'});
  function observe(image){
    const id=image.dataset.memoryThumbnail;if(!id||key(id)!==id)return;
    const entry=touch(id);if(entry){hits++;present(image,id,entry)}else intersection.observe(image);
  }
  function bind(image,source){
    const id=key(source);if(!id){image.src=source;return}
    image.dataset.memoryThumbnailSource=source;
    if(image.dataset.memoryThumbnail!==id){image.dataset.memoryThumbnail=id;image.dataset.thumbnailRetries='0';image.removeAttribute('src')}
    // 侧栏节点常在插入DOM之前配置；命中时同步赋值避免回滚黑帧。
    const entry=touch(id);if(entry){hits++;image.loading='eager';image.decoding='sync';image.src=entry.url}
    if(image.isConnected)observe(image);
  }
  function attributes(source){
    const id=key(source);if(!id)return null;
    const entry=touch(id);if(entry)hits++;
    return `data-memory-thumbnail="${escape(id)}" data-memory-thumbnail-source="${escape(source)}"${entry?' src="'+escape(entry.url)+'" decoding="sync"':''}`;
  }
  function scan(node){if(node.nodeType!==1)return;if(node.matches('img[data-memory-thumbnail]'))observe(node);node.querySelectorAll('img[data-memory-thumbnail]').forEach(observe)}
  const observer=new MutationObserver(records=>{
    for(const record of records)if(record.type==='attributes')observe(record.target);else{
      for(const node of record.removedNodes)if(node.nodeType===1){if(node.tagName==='IMG')intersection.unobserve(node);node.querySelectorAll('img').forEach(image=>intersection.unobserve(image))}
      for(const node of record.addedNodes)scan(node);
    }
  });
  observer.observe(document.documentElement,{subtree:true,childList:true,attributes:true,attributeFilter:['data-memory-thumbnail']});
  function resume(){if(!visible())return;document.querySelectorAll('img[data-memory-thumbnail]').forEach(image=>{if(!image.getAttribute('src'))intersection.observe(image)});pump()}
  for(const event of ['framekeep:workspace','framekeep:boot-ready','visibilitychange'])document.addEventListener(event,resume);
  window.addEventListener('pagehide',()=>{closed=true;observer.disconnect();intersection.disconnect();for(const job of jobs.values()){job.controller.abort();for(const resolve of job.consumers)resolve(null)}queue.length=0;jobs.clear();for(const id of entries.keys())forget(id)},{once:true});
  window.FramekeepThumbnailMemory={key,bind,attributes,load,get stats(){return {entries:entries.size,bytes,decodedBytes,active,queued:queue.length,requests,hits,byteLimit,decodedLimit}}};
})();
