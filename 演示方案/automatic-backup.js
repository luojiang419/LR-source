(() => {
  'use strict';
  const host=document.getElementById('automatic-backup-controls');
  if(!host||!window.FramekeepIO)return;
  const $=id=>document.getElementById(id),api=FramekeepIO.api;
  const css=document.createElement('link');css.rel='stylesheet';css.href='/automatic-backup.css';document.head.append(css);
  host.innerHTML=`<form id="automatic-backup-form">
    <label class="backup-toggle"><input id="backup-enabled" type="checkbox" role="switch"><span>启用自动备份</span></label>
    <p id="backup-source" class="workspace-caption" style="overflow-wrap:anywhere">来源：自动跟随现场监控的拍摄文件夹</p>
    <div class="backup-timing"><label>同步间隔（秒）<input id="backup-interval" type="number" min="1" max="86400" step="1" value="5" required inputmode="numeric"></label>
    <label>常用间隔<select id="backup-preset"><option value="custom">自定义</option><option value="1">1 秒</option><option value="3">3 秒</option><option value="5" selected>5 秒</option><option value="10">10 秒</option><option value="30">30 秒</option><option value="60">60 秒</option><option value="300">300 秒</option></select></label></div>
    <label for="backup-folder">备份文件夹</label><div class="folder-input-row"><input id="backup-folder" autocomplete="off" spellcheck="false" placeholder="D:\\照片备份 或 \\\\NAS\\共享\\拍摄备份" style="min-width:0;width:100%"><button id="backup-browse" type="button" class="bordered">选择文件夹</button></div>
    <div class="asset-actions"><button id="backup-save" class="primary" type="submit" disabled>保存设置</button><button id="backup-retry" class="bordered" type="button" disabled>立即同步／重试</button></div>
    <p id="backup-message" class="workspace-caption" role="status" aria-live="polite" hidden></p>
    <p id="backup-progress" class="workspace-caption" role="status" aria-live="polite" style="overflow-wrap:anywhere"></p>
    <ul id="backup-issues" class="workspace-caption" style="overflow-wrap:anywhere;padding-left:20px"></ul>
  </form>`;
  let loaded=false,busy=false,timer,loading=false;
  const message=text=>{$('backup-message').textContent=text;$('backup-message').hidden=!text};
  const name=path=>String(path||'').replaceAll('\\','/').split('/').filter(Boolean).pop()||path;
  function preset(){const value=$('backup-interval').value;$('backup-preset').value=[...$('backup-preset').options].some(o=>o.value===value)?value:'custom'}
  function paint(state,fill=false){
    const config=state.config;
    if(fill||!loaded){$('backup-enabled').checked=config.enabled;$('backup-folder').value=config.folder;$('backup-interval').value=config.interval_seconds;preset();loaded=true}
    $('backup-save').disabled=busy;$('backup-retry').disabled=busy||!config.enabled;
    $('backup-source').textContent='拍摄文件夹：'+((state.sources||[]).map(name).join('、')||'未开启现场监控');$('backup-source').title=(state.sources||[]).join('\n');
    $('backup-progress').textContent=(config.enabled?'':'已暂停 · ')+`已同步 ${state.completed} · 待同步 ${state.pending}`+(state.waiting?' · 等待元数据 '+state.waiting:'')+(state.retrying?' · 重试 '+state.retrying:'')+(state.active?' · 传输中':'');
    $('backup-progress').title=state.last_output||'';
    $('backup-issues').replaceChildren();
    $('backup-issues').hidden=!(state.issues||[]).length;
    for(const issue of state.issues||[]){const item=document.createElement('li');item.textContent=name(issue.source)+'：'+issue.reason;$('backup-issues').append(item)}
    if(state.error&&!(state.issues||[]).length)message(state.error);
  }
  async function refresh(){
    if(loading||busy)return;loading=true;
    try{paint(await api('/api/automatic-backup',undefined,8000))}
    catch(error){message('读取失败：'+error.message)}finally{loading=false}
  }
  $('backup-preset').onchange=()=>{if($('backup-preset').value!=='custom')$('backup-interval').value=$('backup-preset').value};
  $('backup-interval').oninput=preset;
  $('automatic-backup-form').oninput=()=>{if(loaded&&!busy)message('')};
  $('backup-browse').onclick=async()=>{
    $('backup-browse').disabled=true;
    try{const folders=window.FramekeepDesktop?.chooseFolders?await FramekeepDesktop.chooseFolders():[(await api('/api/choose-folder',{purpose:'backup'},300000)).folder];if(folders?.[0]){$('backup-folder').value=folders[0];message('')}}
    catch(error){message('选择失败：'+error.message)}finally{$('backup-browse').disabled=false}
  };
  async function action(run){
    if(busy||!loaded)return;busy=true;
    const controls=[...host.querySelectorAll('input,select,button')];controls.forEach(e=>e.disabled=true);
    try{const state=await run();paint(state,true)}catch(error){message('操作失败：'+error.message)}
    finally{busy=false;controls.forEach(e=>e.disabled=false);refresh()}
  }
  $('automatic-backup-form').onsubmit=event=>{
    event.preventDefault();
    if(!$('automatic-backup-form').reportValidity())return;
    const config={enabled:$('backup-enabled').checked,folder:$('backup-folder').value,interval_seconds:Number($('backup-interval').value)};
    action(async()=>{const state=await api('/api/automatic-backup/configure',config);message('');return state});
  };
  $('backup-retry').onclick=()=>action(async()=>{const state=await api('/api/automatic-backup/retry',{});message('');return state});
  function visible(){return !document.hidden&&!$('app-settings-page').classList.contains('hidden')&&!$('settings-backup').hidden}
  document.addEventListener('framekeep:workspace',()=>{if(visible())refresh()});
  $('settings-tab-backup')?.addEventListener('click',refresh);
  timer=setInterval(()=>{if(visible())refresh()},2000);
  window.addEventListener('pagehide',()=>clearInterval(timer),{once:true});
  // 首屏不访问网络盘；只在设置页读取轻量本地配置与队列。
  if(visible())refresh();
})();
