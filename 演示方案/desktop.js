(() => {
  'use strict';
  const invoke=window.__TAURI__?.core?.invoke;
  if(!invoke)return;
  let closing=false;
  window.FramekeepDesktop={
    chooseFolders:()=>invoke('choose_folders'),
    saveManifest:(name,data)=>invoke('save_manifest',{suggestedName:name,data}),
    saveSkuWorkbook:(projectId,suggestedName)=>invoke('save_sku_workbook',{projectId,suggestedName}),
    async close(){
      if(closing)return;closing=true;
      try{
        await window.FramekeepIO?.flush();
        if(!await invoke('finish_close'))closing=false;
      }catch(error){closing=false;window.Framekeep?.notify('关闭前保存失败，请重试：'+error.message)}
    }
  };
  window.addEventListener('DOMContentLoaded',()=>requestAnimationFrame(()=>requestAnimationFrame(()=>invoke('desktop_ready').catch(()=>{}))),{once:true});
})();
