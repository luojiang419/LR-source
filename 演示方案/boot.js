(() => {
  'use strict';
  const modules=['desktop.js','image-viewer.js','preview-empty.js','session-paging.js','app.js','task-status.js','formal.js','component-ui.js','assistant-settings.js','mouse-remote.js','folder-watch.js','sku-reconciliation.js','asset-metadata.js','asset-loading.js','asset-viewer.js','asset-library.js','sku-media-pool.js','asset-analysis.js','raw-reconcile.js','workbench-layout.js','sidebar-resize.js','ocr-feedback.js','reference-search.js','asset-retrieval.js','lan-sharing.js','workbench-ui.js','preview-histogram.js','update.js','selection-folders.js','split-capture.js'];
  const screen=document.getElementById('startup-screen'),status=document.getElementById('startup-status');
  const boot=window.FramekeepBoot={loading:true,started:performance.now(),shellReady:performance.now(),dataReady:null,workbenchReady:null};
  const errors=[];
  const recordError=event=>{if(event.message&&event.filename)errors.push(event.message)};
  window.addEventListener('error',recordError);
  function stage(message,step){status.textContent=message;screen.dataset.step=String(step)}
  function script(src){return new Promise((resolve,reject)=>{const element=document.createElement('script');element.src=src;element.onload=resolve;element.onerror=()=>reject(new Error('工作台模块读取失败，请重试'));document.body.append(element)})}
  for(const name of modules){const link=document.createElement('link');link.rel='preload';link.as='script';link.href=name;document.head.append(link)}
  async function start(){
    try{
      stage('正在打开上次的照片…',1);
      const response=await fetch('/api/bootstrap',{signal:AbortSignal.timeout(30000)});
      let token=null;
      if(response.status===404){await script('assets/demo-data.js')}
      else{
        if(!response.ok)throw new Error('照片资料暂时无法读取，请重试');
        const data=await response.json();window.FRAMEKEEP_SESSION=data.session;window.DEMO_PHOTOS=data.photos;token=data.request_token;
      }
      boot.dataReady=performance.now();stage('正在整理您的工作台…',2);
      for(const name of modules){await script(name);if(errors.length)throw new Error('工作台初始化失败，请重试')}
      stage('准备就绪',3);
      document.body.removeAttribute('data-boot-loading');document.querySelector('.workbench').removeAttribute('inert');
      await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
      boot.loading=false;boot.workbenchReady=performance.now();
      screen.classList.add('is-leaving');document.dispatchEvent(new CustomEvent('framekeep:boot-ready'));
      setTimeout(()=>screen.remove(),350);
      if(token)fetch('/api/boot/complete',{method:'POST',headers:{'Content-Type':'application/json','X-Framekeep-Token':token},body:'{}'}).catch(()=>{});
      window.removeEventListener('error',recordError);
    }catch(error){
      stage('工作台还没有准备好',0);document.getElementById('startup-error').textContent=error.message||'连接暂时中断，请重试';
      const retry=document.getElementById('startup-retry');retry.hidden=false;retry.onclick=()=>location.reload();
      window.removeEventListener('error',recordError);
    }
  }
  start();
})();
