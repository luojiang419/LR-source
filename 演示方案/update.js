/* 启动静默检查与下载，下载完成后由用户打开独立更新器。 */
const startUpdater=()=>{
  'use strict';
  if(!window.FramekeepIO)return;
  const api=(path,body)=>window.FramekeepIO.api(path,body);
  const button=document.createElement('button');button.id='check-update';button.type='button';
  button.className='bordered';button.textContent='检查更新';
  document.querySelector('.header-actions')?.append(button);
  let promptedRelease='',handingOff=false,checking=false,polling=false,opened=false,latest={},pollTimer;
  const schedule=(delay=1000)=>{clearTimeout(pollTimer);pollTimer=setTimeout(poll,delay)};
  async function prepareUpdate(){
    if(handingOff||!window.__TAURI__)return;
    handingOff=true;
    try{
      await window.FramekeepIO.flush();
      await window.FramekeepAssetViewer?.flush();
      button.textContent='正在退出并更新…';
      await window.__TAURI__.core.invoke('close_for_update');
    }catch(error){
      handingOff=false;
      if(String(error?.message||error).includes('UPDATE_WAIT:')){
        button.textContent='等待任务收尾…';schedule();return;
      }
      try{await api('/api/update/cancel',{})}catch{}
      opened=false;button.disabled=false;button.textContent=latest.downloaded?'立即更新':'检查更新';
      window.Framekeep?.notify('更新已取消：'+(error.message||error));
    }
  }
  window.FramekeepUpdate={prepare:prepareUpdate};
  async function poll(){
    if(polling)return;polling=true;
    try{
      const state=await api('/api/update');
      latest=state;
      if(!state.ready&&state.transaction)handingOff=false;
      button.title=`当前 ${state.currentVersion} · ${state.currentReleaseId}`;
      if(state.status==='checking'){schedule();return;}
      if(checking){checking=false;
        if(state.status!=='downloading'&&!state.downloaded)window.Framekeep?.notify(state.message);}
      if(opened&&['error','rolled-back','complete'].includes(state.transaction?.status)){
        opened=false;
        window.Framekeep?.notify(state.transaction.message||'更新流程已结束');
      }
      if(opened&&state.updaterOpen===false&&!state.ready)opened=false;
      if(!opened&&!handingOff){button.disabled=false;button.textContent=state.downloaded?'立即更新':'检查更新';}
      const release=`${state.version}/${state.releaseId}`;
      if(state.downloaded&&window.FramekeepDesktop&&promptedRelease!==release){
        promptedRelease=release;window.Framekeep?.notify('新版已下载完成，点击“立即更新”安装');
      }
      if(state.ready)await prepareUpdate();
      if(state.status==='downloading'||opened||state.ready)schedule();
    }catch(error){
      if(!handingOff){button.title='连接暂时中断，正在重试更新交接';schedule(2000)}
    }finally{polling=false}
  }
  async function check(silent=false){
    if(checking||opened||handingOff)return;
    checking=!silent;
    if(!silent){button.disabled=true;button.textContent='正在检查…';}
    try{await api('/api/update/check',{});schedule(500);}
    catch(error){checking=false;button.disabled=false;button.textContent='检查更新';if(!silent)window.Framekeep?.notify(error.message);}
  }
  button.addEventListener('click',async()=>{
    if(!latest.downloaded){await check();return;}
    if(opened||handingOff)return;
    opened=true;button.disabled=true;button.textContent='更新器已打开';
    try{await api('/api/update/open',{});schedule(500)}
    catch(error){opened=false;button.disabled=false;button.textContent='立即更新';window.Framekeep?.notify(error.message)}
  });
  if(window.FramekeepDesktop)setTimeout(()=>check(true),2500);
};
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',startUpdater,{once:true});else startUpdater();
