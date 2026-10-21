(() => {
  'use strict';
  if(!window.FramekeepIO)return;
  const api=FramekeepIO.api,box=document.createElement('aside');box.id='component-progress';box.hidden=true;box.setAttribute('aria-label','自动准备识别组件');
  box.innerHTML='<div class="component-heading"><strong id="component-title">正在准备识别组件</strong><button id="component-hide" aria-label="收起准备进度">×</button></div><p id="component-message" role="status"></p><progress id="component-meter" max="100" aria-label="组件准备进度"></progress><div class="component-footer"><span id="component-detail">首次使用自动准备，完成后继续当前操作</span><button id="component-retry" hidden>重试准备</button></div>';
  document.body.append(box);const $=id=>document.getElementById(id);
  let retry=null,requests=0,driverBusy=false,dismissed=false;
  function paint(state,title='正在准备识别组件'){
    box.hidden=false;$('component-title').textContent=title;$('component-message').textContent=state.message||'正在检查，请稍候…';
    $('component-retry').hidden=state.status!=='error';
    if(state.total){$('component-meter').value=Math.min(100,100*state.done/state.total);$('component-detail').textContent=`${(state.done/1024**2).toFixed(1)} / ${(state.total/1024**2).toFixed(1)} MB · 可保留断点重试`}
    else{$('component-meter').removeAttribute('value');$('component-detail').textContent=state.status==='error'?'重试补齐后，再次点击原识别按钮即可':'首次准备可能需要数分钟，完成后自动继续当前操作'}
    box.classList.toggle('has-error',state.status==='error');
  }
  $('component-hide').onclick=()=>{box.hidden=true;dismissed=true};
  $('component-retry').onclick=async()=>{if(!retry)return;try{await retry()}catch{}};
  const pause=()=>new Promise(resolve=>setTimeout(resolve,500));
  async function models(path,body,requested=null){
    requests++;dismissed=false;
    try{
      let state=await api('/api/components/prepare',requested?{groups:requested}:{path,body}),groups=state.groups||[];
      if(!groups.length)return;
      retry=()=>models(path,body,requested);
      while(!groups.every(g=>state.ready?.includes(g))){
        paint(state);
        if(state.status==='error')throw new Error(state.error||state.message);
        await pause();state=await api('/api/components');
      }
      if(requests===1)box.hidden=true;
    }finally{requests--}
  }
  async function driver(activate=false){
    driverBusy=true;dismissed=false;
    try{
    let state=await api('/api/mouse/driver/prepare',{activate});
    retry=()=>driver(activate);
    while(state.status==='working'||state.status==='idle'){
      paint(state,'正在准备鼠标遥控');await pause();state=await api('/api/mouse/driver');
    }
    if(!state.installed||state.status==='error'){paint({...state,status:'error'},'鼠标驱动需要处理');throw new Error(state.message||'鼠标驱动尚未就绪')}
    box.hidden=true;return state;
    }finally{driverBusy=false}
  }
  const paths=new Set(['/api/sku/parse','/api/sku/recognize','/api/parsing/ocr','/api/analysis/batches','/api/providers/test']);
  window.FramekeepComponents={driver,before:async(path,body)=>{
    if(paths.has(path)||path==='/api/sku/full-auto'&&body.enabled===true||path==='/api/parsing/configure'&&body.mode==='auto'||path==='/api/parsing/control'&&['retry','resume'].includes(body.action))await models(path,body);
  }};
  let backgroundRunning=false;
  const monitor=setInterval(async()=>{
    if(document.hidden||requests||driverBusy||backgroundRunning)return;backgroundRunning=true;
    try{
      const state=await api('/api/components');
      if(requests||driverBusy)return;
      if(['working','error'].includes(state.status)&&!dismissed){
        if(state.status==='error'&&state.groups?.length)retry=state.groups.includes('driver')?driver:()=>models('',{},state.groups);
        paint(state);
      }
      else if(state.status==='ready'&&!box.classList.contains('has-error')){box.hidden=true;dismissed=false}
    }catch{}finally{backgroundRunning=false}
  },2000);
  window.addEventListener('pagehide',()=>clearInterval(monitor),{once:true});
})();
