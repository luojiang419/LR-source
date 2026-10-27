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
  const $=id=>document.getElementById(id);
  const style=document.createElement('link');style.rel='stylesheet';style.href='desktop-lifecycle.css';document.head.append(style);
  let closing=false,dialog=null,settingsSaving=false;
  function report(error){
    const message=String(error?.message||error);
    if(dialog?.open)$('desktop-close-message').textContent=message;
    else if(window.Framekeep?.notify)Framekeep.notify(message);
    else window.alert(message);
  }
  function paintSettings(value){
    if(!$('desktop-close-behavior'))return;
    $('desktop-close-behavior').value=value.close_behavior;
    $('desktop-startup-mode').value=value.startup_mode;
  }
  function setCloseBusy(busy){
    if(!dialog)return;
    for(const control of dialog.querySelectorAll('button'))control.disabled=busy;
  }
  function dismissClose(){dialog?.close();closing=false}
  async function applyClose(action,remember=false){
    setCloseBusy(true);
    try{
      // 隐藏同样等待评级保存；失败时保留窗口和当前选择，允许重试。
      await window.FramekeepIO?.flush();
      if(action==='tray'){
        await invoke('hide_to_tray',{remember});dismissClose();
        if(remember)paintSettings(await invoke('get_desktop_preferences'));
        return;
      }
      const exited=await invoke('finish_close',{remember});
      if(!exited)dismissClose();
    }finally{setCloseBusy(false)}
  }
  function showCloseDialog(){
    if(!dialog){
      dialog=document.createElement('dialog');dialog.id='desktop-close-dialog';dialog.setAttribute('aria-labelledby','desktop-close-title');
      dialog.innerHTML='<div class="desktop-close-heading"><h2 id="desktop-close-title">关闭末帧选片</h2><button type="button" data-cancel aria-label="取消关闭">×</button></div><p class="desktop-close-copy">您希望关闭窗口后如何运行？</p><div class="desktop-close-options"><button type="button" id="desktop-close-tray"><strong>最小化到托盘</strong><span>保留后台任务，点击托盘图标可恢复窗口</span></button><button type="button" id="desktop-close-exit"><strong>退出程序</strong><span>保存当前操作并停止后台服务</span></button></div><div class="desktop-remember-row"><div><label id="desktop-remember-label" for="desktop-close-remember">不再询问</label><p>记住所选行为，可在设置中随时更改</p></div><button type="button" id="desktop-close-remember" class="desktop-switch" role="switch" aria-checked="false" aria-labelledby="desktop-remember-label"><span></span></button></div><p id="desktop-close-message" role="status"></p><div class="desktop-close-actions"><button type="button" data-cancel>取消</button></div>';
      document.body.append(dialog);
      const remember=$('desktop-close-remember');remember.onclick=()=>remember.setAttribute('aria-checked',String(remember.getAttribute('aria-checked')!=='true'));
      for(const button of dialog.querySelectorAll('[data-cancel]'))button.onclick=dismissClose;
      for(const action of ['tray','exit'])$('desktop-close-'+action).onclick=async()=>{try{await applyClose(action,remember.getAttribute('aria-checked')==='true')}catch(error){report('操作失败：'+String(error?.message||error))}};
      dialog.addEventListener('keydown',event=>event.stopPropagation());
      dialog.addEventListener('close',()=>{if(!dialog.open)closing=false});
      dialog.addEventListener('cancel',event=>{event.preventDefault();if(!$('desktop-close-tray').disabled)dismissClose()});
    }
    $('desktop-close-remember').setAttribute('aria-checked','false');$('desktop-close-message').textContent='';
    dialog.showModal();$('desktop-close-tray').focus();
  }
  window.FramekeepDesktop={
    chooseFolders:()=>invoke('choose_folders'),
    chooseCacheBackup:()=>invoke('choose_cache_backup'),
    chooseCacheFolder:()=>invoke('choose_cache_folder'),
    saveManifest:(name,data)=>invoke('save_manifest',{suggestedName:name,data}),
    saveSkuWorkbook:(projectId,suggestedName)=>invoke('save_sku_workbook',{projectId,suggestedName}),
    async initSettings(){
      if(!$('desktop-close-behavior'))return;
      try{
        paintSettings(await invoke('get_desktop_preferences'));
        const controls=[$('desktop-close-behavior'),$('desktop-startup-mode')];
        for(const control of controls){control.disabled=false;control.onchange=async()=>{
          if(settingsSaving)return;settingsSaving=true;controls.forEach(item=>item.disabled=true);
          $('desktop-preferences-message').textContent='正在保存…';
          try{
            const key=control.id==='desktop-close-behavior'?'closeBehavior':'startupMode';
            paintSettings(await invoke('set_desktop_preferences',{[key]:control.value}));
            $('desktop-preferences-message').textContent='已保存，立即生效';
          }catch(error){
            $('desktop-preferences-message').textContent='保存失败：'+String(error?.message||error);
            try{paintSettings(await invoke('get_desktop_preferences'))}catch{}
          }finally{settingsSaving=false;controls.forEach(item=>item.disabled=false)}
        }}
        $('desktop-preferences-message').textContent='设置自动保存；手动打开软件始终显示窗口。';
      }catch(error){$('desktop-preferences-message').textContent='无法读取桌面设置：'+String(error?.message||error)}
    },
    async close(forcedAction=null){
      if(closing)return;closing=true;
      try{
        const preferences=await invoke('get_desktop_preferences'),action=forcedAction||preferences.close_behavior;
        if(action==='ask'){showCloseDialog();return}
        await applyClose(action);
      }catch(error){report('关闭前操作失败，请重试：'+String(error?.message||error))}
      finally{if(!dialog?.open)closing=false}
    }
  };
  if(window.__framekeepHasPendingClose)FramekeepDesktop.close(window.__framekeepPendingClose);
  // 原生启动页也使用同一关闭流程，但工作台就绪指标仅由 HTTP 工作台上报。
  if(location.hostname!=='127.0.0.1')return;
  const ready=()=>requestAnimationFrame(()=>requestAnimationFrame(()=>{const boot=window.FramekeepBoot;invoke('desktop_ready',{timings:boot?{shell_ready_ms:boot.shellReady,data_ready_ms:boot.dataReady,workbench_ready_ms:boot.workbenchReady}:null}).catch(()=>{})}));
  if(window.FramekeepBoot?.loading)document.addEventListener('framekeep:boot-ready',ready,{once:true});
  else if(document.readyState==='loading')window.addEventListener('DOMContentLoaded',ready,{once:true});
  else ready();
})();
