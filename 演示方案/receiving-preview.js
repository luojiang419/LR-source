(() => {
  'use strict';
  if(!window.FramekeepIO||!window.Framekeep)return;
  const $=id=>document.getElementById(id),target=()=>Framekeep.remoteTarget();
  let state=null,active=null,candidate=null,serial=0,closed=false,controller=null,retryTimer=null;
  const suppressed=new Set(),originalInert=new Map(),failures=new Map();
  const style=document.createElement('style');style.textContent='.photo-stage{position:relative}.receiving-photo-preview{position:absolute;inset:0;z-index:8;display:flex;align-items:center;justify-content:center;padding:12px;background:inherit}.receiving-photo-preview img{display:block;width:100%;height:100%;max-width:100%;max-height:100%;object-fit:contain}.receiving-photo-preview span{position:absolute;left:16px;bottom:12px;padding:5px 9px;border-radius:5px;background:var(--theme-panel);color:var(--theme-text);font-size:11px;opacity:.9}.receiving-photo-active :is(#stars,#reject,#clear-rating,#metadata,#preview-histogram,#filename,#photo-position,#loupe-name){visibility:hidden}.receiving-photo-active .right-panel{opacity:.65}';document.head.append(style);
  // 临时显示原图时借用单张视图，结束后恢复用户原有的网格或列表。
  style.textContent+='.receiving-photo-active #catalog-view{display:none!important}.receiving-photo-active #loupe-view.hidden{display:flex!important}';
  let deferred=false;
  const progress=photo=>String(photo.revision??0)+':'+String(photo.bytes??0);
  const notice=next=>next?.receiving||next?.latest;
  function rememberSuppressed(id){if(!id)return;suppressed.add(id);while(suppressed.size>16)suppressed.delete(suppressed.values().next().value)}
  function cancelCandidate(){
    serial++;clearTimeout(retryTimer);retryTimer=null;
    if(candidate){candidate.image.onload=null;candidate.image.onerror=null;candidate.image.removeAttribute('src');candidate=null}
  }
  function releaseActive(restore=true){
    const released=active;
    if(restore&&active&&$('filename').textContent===active.name)$('filename').textContent=active.previousName;
    active?.node.remove();active=null;
    for(const [element,inert] of originalInert)element.inert=inert;
    originalInert.clear();$('photo-stage').removeAttribute('aria-busy');document.body.classList.remove('receiving-photo-active');
    if(released)document.dispatchEvent(new CustomEvent('framekeep:receiving-preview',{detail:{id:released.id,active:false}}));
  }
  function clear(suppress=false,restore=true){
    if(suppress){rememberSuppressed(active?.id);rememberSuppressed(candidate?.id)}
    cancelCandidate();releaseActive(restore);
  }
  function permitted(next){return !closed&&next?.enabled&&Framekeep.canPreviewSession(next.session_id)&&FramekeepWatch?.getFollow()!==false&&!document.querySelector('dialog[open]')&&!FramekeepBoot?.loading&&(!window.FramekeepWorkspace||FramekeepWorkspace.current()==='selection')}
  function mainReady(id){
    const image=$('photos').querySelector('[data-loupe] img'),photo=(window.DEMO_PHOTOS||[]).find(item=>item.id===id);
    return !!id&&state?.session_id===window.FRAMEKEEP_SESSION.id&&!$('loupe-view').classList.contains('hidden')&&image?.closest('[data-loupe]').dataset.loupe===id&&target().photoId===id&&image.complete&&image.naturalWidth>0&&(photo?.raw&&!photo.previewFull||new URL(image.src,location.href).searchParams.get('full')==='1');
  }
  const readyFor=photo=>mainReady(photo.formal_id||photo.id);
  function current(photo){
    const latest=notice(state);
    return permitted(state)&&!suppressed.has(photo.id)&&(latest?latest.key===photo.key&&latest.id===photo.id:state.last_photo_id===photo.id);
  }
  function committed(){
    if(!active&&!candidate)return;
    if(!permitted(state)){clear();return}
    if(candidate&&readyFor(candidate))cancelCandidate();
    if(!active)return;
    const selected=target();
    if(readyFor(active)||!state.receiving&&mainReady(state.last_photo_id)){releaseActive(false);return}
    if(selected.photoId!==active.previousId&&selected.photoId!==active.id){active.previousId=selected.photoId;active.previousName=$('filename').textContent}
    $('filename').textContent=active.name;
  }
  function retry(photo){
    if(!current(photo)||!notice(state))return;
    const signature=state.generation+'/'+photo.key+'/'+progress(photo),count=(failures.get(signature)||0)+1;
    failures.set(signature,count);while(failures.size>16)failures.delete(failures.keys().next().value);
    // 写入方可能暂时独占文件；字节数不变也不能在第三次失败后永久停住。
    // 只重试当前通知，退避后最多每秒两次；新图、切目录和取消跟随都会撤销计时器。
    clearTimeout(retryTimer);retryTimer=setTimeout(()=>{retryTimer=null;if(current(photo)&&notice(state)&&!readyFor(photo))show(state)},Math.min(500,60*2**Math.min(4,count-1)));
  }
  function present(photo,image,previous){
    const old=active;releaseActive();
    const node=document.createElement('div');node.className='receiving-photo-preview';node.dataset.photo=photo.id;node.dataset.key=photo.key;node.dataset.revision=String(photo.revision??0);node.dataset.bytes=String(photo.bytes??0);
    const caption=document.createElement('span');caption.textContent='正在接收 · '+photo.name;node.append(image,caption);
    active={...photo,progress:progress(photo),sid:state.session_id,node,previousId:old?.previousId??previous.photoId,previousName:old?.previousName??$('filename').textContent};
    $('photo-stage').append(node);$('photo-stage').setAttribute('aria-busy','true');$('filename').textContent=photo.name;document.body.classList.add('receiving-photo-active');
    for(const element of [$('stars'),$('reject'),$('clear-rating'),$('undo'),$('right-panel-body'),document.querySelector('.right-panel .selection-sidebar-footer')].filter(Boolean)){originalInert.set(element,element.inert);element.inert=true}
    document.dispatchEvent(new CustomEvent('framekeep:receiving-preview',{detail:{id:photo.id,key:photo.key,revision:photo.revision??0,original:true,active:true}}));
  }
  function load(photo){
    cancelCandidate();const request=serial,previous=target(),allowed=new Set([previous.photoId,active?.id]),image=new Image();
    candidate={...photo,progress:progress(photo),image};
    image.crossOrigin='anonymous';image.decoding='async';image.fetchPriority='high';image.alt=photo.name;
    image.onload=()=>{
      if(request!==serial||candidate?.image!==image||!current(photo))return;
      image.onload=null;image.onerror=null;candidate=null;
      const selected=target();
      if(selected.sessionId===previous.sessionId&&!allowed.has(selected.photoId)&&selected.photoId!==photo.id&&selected.photoId!==notice(state)?.formal_id){rememberSuppressed(photo.id);return}
      if(readyFor(photo)){releaseActive(false);return}
      failures.delete(state.generation+'/'+photo.key+'/'+progress(photo));present(photo,image,previous);
      if(notice(state)&&progress(notice(state))!==progress(photo))show(state);
    };
    image.onerror=()=>{
      if(request!==serial||candidate?.image!==image)return;
      image.onload=null;image.onerror=null;candidate=null;retry(photo);
    };
    const url=new URL(photo.url,location.href);url.searchParams.set('rev',String(photo.revision??0));url.searchParams.set('read',String(request));image.src=url.href;
  }
  function show(next){
    if(state&&(state.generation!==next.generation||state.session_id!==next.session_id)){clear();suppressed.clear();failures.clear()}
    state=next;const photo=notice(next);Framekeep.supersedeLivePreview(photo?.formal_id||photo?.id||next?.last_photo_id);
    if(!permitted(next)){deferred=!!photo;clear();return}
    deferred=false;
    committed();
    if(!photo||suppressed.has(photo.id)||readyFor(photo))return;
    if(retryTimer&&failures.has(state.generation+'/'+photo.key+'/'+progress(photo)))return;
    if(candidate?.key===photo.key)return;
    if(active?.key===photo.key&&active.progress===progress(photo))return;
    load({...photo});
  }
  document.addEventListener('framekeep:selection',()=>{
    if(deferred&&permitted(state))show(state);
    else committed();
  });
  $('watch-follow').addEventListener('change',()=>{if($('watch-follow').checked){suppressed.clear();if(state)show(state)}else clear(true)});
  $('photos').addEventListener('load',committed,true);
  document.addEventListener('framekeep:workspace',()=>{if(permitted(state))show(state);else clear()});
  document.addEventListener('click',event=>{
    if(!active&&!candidate)return;
    if(active&&event.target.closest('.watch-direct-preview,#photo-stage,.right-panel,#stars,#reject,#clear-rating,#undo')){event.preventDefault();event.stopImmediatePropagation();return}
    if(event.target.closest('#watch-browser,#previous,#next,[data-view],[data-filter],#sort,#search,#watch-follow-toggle'))clear(true);
  },true);
  document.addEventListener('keydown',event=>{
    if((!active&&!candidate)||/INPUT|TEXTAREA|SELECT/.test(event.target.tagName))return;
    if(active&&/^[0-5x]$/i.test(event.key)){event.preventDefault();event.stopImmediatePropagation();return}
    if(['Escape','ArrowLeft','ArrowRight','ArrowUp','ArrowDown','g','l','e','c','Enter'].includes(event.key)||event.key.toLowerCase()==='z'&&(event.ctrlKey||event.metaKey))clear(true);
  },true);
  $('photo-stage').addEventListener('wheel',event=>{if(active){event.preventDefault();event.stopImmediatePropagation()}},{capture:true,passive:false});
  const observer=new MutationObserver(()=>{if((active||candidate)&&document.querySelector('dialog[open]'))clear(true)});observer.observe(document.body,{subtree:true,attributes:true,attributeFilter:['open']});
  window.FramekeepReceiving={get active(){return !!active},allowFollow:id=>!suppressed.has(id),dismiss:()=>clear(true)};
  let after=-1;
  function restartPoll(){
    controller?.abort();after=-1;deferred=false;state=null;
    clear();suppressed.clear();failures.clear();
  }
  document.addEventListener('framekeep:sync-role',restartPoll);
  document.addEventListener('framekeep:preview-channel',restartPoll);
  async function poll(){
    while(!closed){
      controller=new AbortController();
      try{
        const requested=after,started=performance.now(),config=window.FRAMEKEEP_SESSION.directPreview,path='/api/watch/direct?after='+after+'&wait=15';
        let next;
        if(config){const response=await fetch(config.origin+'/api/watch/direct?wait=15',{headers:{'X-Framekeep-Preview':config.token,'X-Framekeep-After':String(after)},signal:AbortSignal.any([controller.signal,AbortSignal.timeout(20000)])});if(!response.ok)throw new Error('图像通知暂时不可用');next=await response.json()}
        else next=await FramekeepIO.api(path,undefined,20000,controller.signal);
        if(controller.signal.aborted)continue;
        after=next.event_id??-1;show(next);if(next.event_id==null||after===requested&&performance.now()-started<100)await new Promise(resolve=>setTimeout(resolve,300));
      }
      catch(error){if(closed)return;if(controller.signal.aborted)continue;clear();after=-1;await new Promise(resolve=>setTimeout(resolve,300))}
    }
  }
  document.addEventListener('framekeep:boot-ready',()=>{if(state)show(state)},{once:true});
  window.addEventListener('pagehide',()=>{closed=true;controller?.abort();observer.disconnect();clear()},{once:true});
  poll();
})();
