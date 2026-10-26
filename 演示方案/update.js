/* 启动检查与原生独立更新器交接。保存成功且后台空闲后才退出。 */
document.addEventListener('DOMContentLoaded',()=>{
  'use strict';
  if(!window.FramekeepIO)return;
  const api=(path,body)=>window.FramekeepIO.api(path,body);
  const button=document.createElement('button');button.id='check-update';button.type='button';
  button.className='bordered';button.textContent='检查更新';
  document.querySelector('.header-actions')?.append(button);
  let prompted=false,handingOff=false,checking=false;
  async function poll(){
    try{
      const state=await api('/api/update');
      if(!state.ready&&state.transaction)handingOff=false;
      button.title=`当前 ${state.currentVersion} · ${state.currentReleaseId}`;
      if(state.status==='checking'){setTimeout(poll,1000);return;}
      if(checking){checking=false;button.disabled=false;button.textContent=state.available?'发现新版':'检查更新';
        window.Framekeep?.notify(state.message);}
      if(state.available&&window.FramekeepDesktop&&!prompted){prompted=true;await api('/api/update/open',{});}
      if(state.ready&&!handingOff&&window.__TAURI__){
        handingOff=true;
        try{await window.FramekeepIO.flush();await window.__TAURI__.core.invoke('close_for_update');}
        catch(error){await api('/api/update/cancel',{});window.Framekeep?.notify('更新已取消：'+(error.message||error));}
      }
      if(state.available)setTimeout(poll,1000);
    }catch(error){checking=false;button.disabled=false;button.textContent='检查更新';window.Framekeep?.notify(error.message);}
  }
  async function check(){
    if(checking)return;checking=true;prompted=false;handingOff=false;
    button.disabled=true;button.textContent='正在检查…';
    try{await api('/api/update/check',{});setTimeout(poll,500);}
    catch(error){checking=false;button.disabled=false;button.textContent='检查更新';window.Framekeep?.notify(error.message);}
  }
  button.addEventListener('click',check);
  if(window.FramekeepDesktop)setTimeout(check,2500);
});
