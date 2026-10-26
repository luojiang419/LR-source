(() => {
  'use strict';
  window.FramekeepPaths={driveOf(value){
    let path=String(value||'').replace(/\//g,'\\');
    if(/^\\\\\?\\UNC\\/i.test(path))path='\\\\'+path.slice(8);
    else if(path.startsWith('\\\\?\\'))path=path.slice(4);
    const drive=path.match(/^([A-Za-z]:)(?:\\|$)/);if(drive)return drive[1].toUpperCase();
    const network=path.match(/^\\\\([^\\]+)\\([^\\]+)/);
    return network?'\\\\'+network[1]+'\\'+network[2]:'其他位置';
  }};
  const invoke=window.__TAURI__?.core?.invoke;
  if(!invoke)return;
  let closing=false;
  window.FramekeepDesktop={
    chooseFolders:()=>invoke('choose_folders'),
    chooseCacheBackup:()=>invoke('choose_cache_backup'),
    chooseCacheFolder:()=>invoke('choose_cache_folder'),
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
