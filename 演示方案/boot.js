(() => {
  'use strict';
  const modules=['desktop.js','image-viewer.js','preview-empty.js','session-paging.js','thumbnail-memory.js','app.js','task-status.js','view-cache.js','formal.js','component-ui.js','assistant-settings.js','mouse-remote.js','folder-watch.js','shooting-export.js','sku-reconciliation.js','asset-metadata.js','asset-loading.js','asset-viewer.js','asset-library.js','archive-explorer.js','sku-media-pool.js','model-advanced.js','raw-reconcile.js','workbench-layout.js','sidebar-resize.js','ocr-feedback.js','reference-search.js','asset-retrieval.js','archive-analysis.js','folder-archive.js','multidimensional-workflow.js','lan-sharing.js','workbench-ui.js','preview-histogram.js','update.js','selection-folders.js','split-capture.js','receiving-preview.js'];
  modules.push('inventory.js');
  modules.push('lan-capture-sync.js');
  modules.push('archive-capture.js');
  modules.push('automatic-backup.js');
  const screen=document.getElementById('startup-screen'),status=document.getElementById('startup-status');
  const boot=window.FramekeepBoot={loading:true,started:performance.now(),shellReady:performance.now(),dataReady:null,workbenchReady:null};
  const errors=[];
  const recordError=event=>{if(event.message&&event.filename)errors.push(event.message)};
  window.addEventListener('error',recordError);
  function stage(message,step){status.textContent=message;screen.dataset.step=String(step)}
  function script(src){return new Promise((resolve,reject)=>{const element=document.createElement('script');element.src=src;element.onload=resolve;element.onerror=()=>reject(new Error('工作台模块读取失败，请重试'));document.body.append(element)})}
  const bundle=document.createElement('link');bundle.rel='preload';bundle.as='script';bundle.href='/workbench.js';document.head.append(bundle);
  let launchPhoto=null,arrival=null,arrivalImage=null,arrivalTimer=null,disposed=false;
  let servicesResolve=null,captureResolve=null;
  function firstPhoto(){if(boot.firstPhotoReady==null&&!boot.loading){boot.firstPhotoReady=performance.now();document.dispatchEvent(new CustomEvent('framekeep:first-photo'))}}
  const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  function dismissArrival(){arrival?.remove();arrival=null;arrivalImage=null}
  function showArrival(){
    if(!launchPhoto||boot.loading||window.FramekeepWorkspace?.current()!=='selection')return;
    const current=window.FRAMEKEEP_SESSION;
    if(current?.id&&!current.restoring&&current.root!==launchPhoto.folder){dismissArrival();return}
    const matched=window.DEMO_PHOTOS?.find(photo=>[photo.jpg,photo.raw].some(path=>path?.replaceAll('\\','/').split('/').pop()===launchPhoto.name));
    if(matched){dismissArrival();return}
    if(!arrival){
      arrival=document.createElement('div');arrival.id='startup-arrival';arrival.setAttribute('role','status');
      arrival.style.cssText='position:absolute;inset:76px 0 52px;z-index:8;display:flex;flex-direction:column;align-items:center;justify-content:center;background:var(--bg,#151b18);padding:16px;pointer-events:none';
      arrivalImage=document.createElement('img');arrivalImage.alt=launchPhoto.name;arrivalImage.style.cssText='min-height:0;max-height:calc(100% - 32px);max-width:100%;object-fit:contain';
      arrivalImage.onload=firstPhoto;
      const caption=document.createElement('span');caption.textContent='照片已接入，正在开启现场监控…';caption.style.marginTop='12px';arrival.append(arrivalImage,caption);
      const host=document.querySelector('.library-main');if(getComputedStyle(host).position==='static')host.style.position='relative';host.append(arrival);
    }
    if(arrivalImage.dataset.identity!==launchPhoto.id||(!arrivalImage.naturalWidth&&arrivalImage.complete)){
      arrivalImage.dataset.identity=launchPhoto.id;arrivalImage.src=launchPhoto.preview;
    }
  }
  async function pollArrival(){
    if(disposed)return;
    try{
      const response=await fetch('/api/startup',{signal:AbortSignal.timeout(3000)});
      if(!response.ok)return;
      const state=await response.json();if(state.photo)launchPhoto=state.photo;
      if(state.capture_ready||state.ready){boot.captureLoaded=true;captureResolve?.(true);captureResolve=null}
      if(state.ready&&!boot.servicesLoaded){boot.servicesLoaded=true;servicesResolve?.(true);servicesResolve=null;document.dispatchEvent(new CustomEvent('framekeep:services-ready'));complete(window.FRAMEKEEP_SESSION?.requestToken)}
      if(state.failed){captureResolve?.(false);servicesResolve?.(false);captureResolve=servicesResolve=null;window.Framekeep?.notify(state.error);return}
      showArrival();
      if(boot.servicesLoaded&&!boot.restoring&&!arrival)return;
    }catch{/* 传输中的照片由下一次轮询重试，不能阻挡工作面。 */}
    arrivalTimer=setTimeout(pollArrival,250);
  }
  async function restore(){
    try{
      let data;
      do{
        const response=await fetch('/api/bootstrap',{signal:AbortSignal.timeout(180000)});
        if(!response.ok)throw new Error('照片恢复失败，请重新打开文件夹或重启软件');
        data=await response.json();if(data.pending)await pause(100);
      }while(data.pending&&!disposed);
      if(disposed)return;
      window.FramekeepIO.restoreStartup(data);
      boot.dataReady=performance.now();boot.restoring=false;showArrival();
      document.dispatchEvent(new CustomEvent('framekeep:restore-ready'));
      if(boot.servicesLoaded)complete(data.request_token);
    }catch(error){
      boot.restoring=false;document.querySelector('.session-title').textContent='照片恢复暂未完成';
      window.Framekeep?.notify(error.message||'照片恢复失败，请重试');
    }
  }
  function complete(token){if(token)fetch('/api/boot/complete',{method:'POST',headers:{'Content-Type':'application/json','X-Framekeep-Token':token},body:'{}'}).catch(()=>{})}
  document.addEventListener('framekeep:selection',()=>{if(!boot.loading)showArrival()});
  document.addEventListener('framekeep:receiving-preview',event=>{if(event.detail.active){firstPhoto();dismissArrival();launchPhoto=null}});
  document.addEventListener('framekeep:watch',event=>{if(event.detail.enabled&&boot.monitorReady==null){boot.monitorReady=performance.now();document.dispatchEvent(new CustomEvent('framekeep:monitor-ready'))}});
  document.getElementById('photos').addEventListener('load',event=>{if(event.target.naturalWidth&&event.target.getBoundingClientRect().height>0)firstPhoto()},true);
  document.addEventListener('framekeep:workspace',()=>{if(window.FramekeepWorkspace?.current()!=='selection')dismissArrival();else showArrival()});
  window.addEventListener('pagehide',()=>{disposed=true;captureResolve?.(false);servicesResolve?.(false);clearTimeout(arrivalTimer);dismissArrival()},{once:true});
  async function start(){
    try{
      stage('正在打开工作台…',1);
      const response=await fetch('/api/bootstrap?shell=1',{signal:AbortSignal.timeout(30000)});
      let token=null,bundled=false,progressive=false;
      if(response.status===404){await script('assets/demo-data.js')}
      else{
        if(!response.ok)throw new Error('照片资料暂时无法读取，请重试');
        const data=await response.json();window.FRAMEKEEP_SESSION=data.session;window.DEMO_PHOTOS=data.photos;token=data.request_token;bundled=data.workbench_bundle===true;
        progressive=data.progressive===true;boot.restoring=progressive;
      }
      if(!progressive)boot.dataReady=performance.now();else{boot.servicesLoaded=false;boot.servicesReady=new Promise(resolve=>{servicesResolve=resolve});boot.captureReady=new Promise(resolve=>{captureResolve=resolve});pollArrival()}stage('正在整理您的工作台…',2);
      if(bundled)await script('/workbench.js');else for(const name of modules)await script(name);
      if(errors.length)throw new Error('工作台初始化失败，请重试');
      try{const last=localStorage.getItem('framekeep.last-workspace');if(['selection','progress','archive','inventory','sku-database','retrieval','settings'].includes(last)&&window.FramekeepWorkspace?.current()!==last)window.FramekeepWorkspace?.show(last)}catch{}
      stage('准备就绪',3);
      document.body.removeAttribute('data-boot-loading');document.querySelector('.workbench').removeAttribute('inert');
      await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
      boot.loading=false;boot.workbenchReady=performance.now();
      if(launchPhoto)window.FramekeepWorkspace?.show('selection');showArrival();
      screen.classList.add('is-leaving');document.dispatchEvent(new CustomEvent('framekeep:boot-ready'));
      setTimeout(()=>screen.remove(),350);
      if(progressive)restore();else complete(token);
      window.removeEventListener('error',recordError);
    }catch(error){
      stage('工作台还没有准备好',0);document.getElementById('startup-error').textContent=error.message||'连接暂时中断，请重试';
      const retry=document.getElementById('startup-retry');retry.hidden=false;retry.onclick=()=>location.reload();
      window.removeEventListener('error',recordError);
    }
  }
  start();
})();
