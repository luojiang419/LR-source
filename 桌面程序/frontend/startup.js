(() => {
  const invoke=window.__TAURI__?.core?.invoke;
  if(!invoke)return;
  requestAnimationFrame(()=>requestAnimationFrame(()=>invoke('startup_visible').catch(()=>{})));
  async function update(){
    try{
      const state=await invoke('startup_status');
      document.getElementById('startup-status').textContent=state.message;
      const steps={'initializing':0,'catalog ready':1,'preview queue ready':1,'ratings ready':2,'reference library ready':2,'http ready':3};
      document.getElementById('startup-screen').dataset.step=String(steps[state.stage]??0);
    }catch(error){/* 启动错误由桌面原生对话框显示。 */}
    setTimeout(update,500);
  }
  update();
})();
