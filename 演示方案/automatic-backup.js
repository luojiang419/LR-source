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
    <section class="backup-status" aria-label="备份进度">
      <div class="backup-status-heading"><strong id="backup-stage" role="status" aria-live="polite">正在读取备份状态</strong><span id="backup-percent"></span></div>
      <progress id="backup-meter" max="100" aria-label="当前文件夹的文件处理进度" hidden></progress>
      <p id="backup-file-count" class="workspace-caption" hidden></p>
      <p id="backup-current" class="backup-current" hidden></p>
      <p id="backup-detail" class="workspace-caption" hidden></p>
      <p id="backup-progress" class="workspace-caption"></p>
    </section>
    <ul id="backup-issues" class="workspace-caption" style="overflow-wrap:anywhere;padding-left:20px"></ul>
  </form>`;
  let loaded=false,busy=false,timer,loading=false;
  const message=text=>{$('backup-message').textContent=text;$('backup-message').hidden=!text};
  const name=path=>String(path||'').replaceAll('\\','/').split('/').filter(Boolean).pop()||path;
  const phases={preparing:'准备同步',preparing_metadata:'准备补写元数据',checking_metadata:'检查元数据',scanning:'扫描文件',connecting:'连接备份文件夹',checking_source:'校验源文件',checking_target:'检查目标文件',copying:'复制文件',verifying_copy:'回读校验备份',verifying_source:'复核源文件',publishing_file:'提交当前文件',publishing:'发布随行档案',complete:'本轮同步完成',retrying:'等待自动重试'};
  const number=value=>Number(value||0).toLocaleString('zh-CN');
  function bytes(value){let size=Number(value||0),unit=0;const units=['B','KB','MB','GB','TB'];while(size>=1024&&unit<units.length-1){size/=1024;unit++}return `${size.toLocaleString('zh-CN',{maximumFractionDigits:unit?1:0})} ${units[unit]}`}
  function duration(value){const seconds=Math.floor(value||0);return seconds>=60?`${Math.floor(seconds/60)} 分 ${seconds%60} 秒`:`${seconds} 秒`}
  function line(id,text){$(id).textContent=text;$(id).hidden=!text}
  function progress(state){
    const p=state.progress||{},enabled=state.config.enabled,active=Boolean(state.active),known=p.total_files!=null;
    let stage=!enabled?'已暂停':active?(phases[p.phase]||'正在同步'):state.retrying?'等待自动重试':state.waiting?'等待元数据写入':state.pending?'等待下一轮同步':state.completed?'同步完成':'等待拍摄文件夹';
    if(enabled&&p.phase==='complete'&&state.waiting)stage='本轮同步完成，仍有照片等待元数据';
    $('backup-stage').textContent=stage;
    const show=enabled&&(active||known)&&p.phase!=='retrying';
    $('backup-meter').hidden=!show;
    // 只有已扫描出真实总数才显示百分比；按校验完成的文件数统计，复制完仍须回读与提交。
    if(known){const percent=p.total_files?Math.floor(100*p.done_files/p.total_files):(p.phase==='complete'?100:0);$('backup-meter').value=percent;$('backup-percent').textContent=show?percent+'%':''}
    else{$('backup-meter').removeAttribute('value');$('backup-percent').textContent=''}
    line('backup-file-count',known?`本轮文件：已校验 ${number(p.done_files)} / ${number(p.total_files)} 个 · 已处理 ${bytes(p.done_bytes)} / ${bytes(p.total_bytes)}`:active&&p.phase==='scanning'?`已扫描 ${number(p.scanned_files)} 个文件`:'');
    line('backup-current',p.current_file?`${active?'当前文件':'上次处理'}：${p.current_file}`:'');
    $('backup-current').title=p.current_file||'';
    const details=[];
    if(active&&p.stage_total)details.push(`${phases[p.phase]||'当前阶段'} ${bytes(p.stage_bytes)} / ${bytes(p.stage_total)}`);
    if(p.source)details.push(`文件夹：${name(p.source)}`);
    if(p.started_at)details.push(`${active?'已耗时':'本轮耗时'} ${duration(p.elapsed_seconds)}`);
    if(enabled&&!active&&state.pending){const seconds=Math.max(0,Math.ceil((Math.max(state.retry_at||0,state.next_sync||0)*1000-Date.now())/1000));details.push(seconds?`${seconds} 秒后${state.retrying?'自动重试':'再次同步'}`:'即将继续同步')}
    line('backup-detail',details.join(' · '));
    $('backup-progress').textContent=`文件夹：已同步 ${number(state.completed)} 个 · 待同步 ${number(state.pending)} 个`+(state.waiting?` · 等待元数据 ${number(state.waiting)} 张照片`:'')+(state.retrying?` · 重试 ${number(state.retrying)} 个`:'');
  }
  function preset(){const value=$('backup-interval').value;$('backup-preset').value=[...$('backup-preset').options].some(o=>o.value===value)?value:'custom'}
  function paint(state,fill=false){
    const config=state.config;
    if(fill||!loaded){$('backup-enabled').checked=config.enabled;$('backup-folder').value=config.folder;$('backup-interval').value=config.interval_seconds;preset();loaded=true}
    $('backup-save').disabled=busy;$('backup-retry').disabled=busy||!config.enabled;
    $('backup-source').textContent='拍摄文件夹：'+((state.sources||[]).map(name).join('、')||'未开启现场监控');$('backup-source').title=(state.sources||[]).join('\n');
    progress(state);
    $('backup-issues').replaceChildren();
    const issues=[...(state.issues||[])];if(state.error&&!issues.length)issues.push({reason:state.error});
    $('backup-issues').hidden=!issues.length;
    for(const issue of issues){const item=document.createElement('li');item.textContent=(issue.source?name(issue.source)+'：':'')+issue.reason;$('backup-issues').append(item)}
  }
  async function refresh(){
    if(loading||busy)return;loading=true;
    try{paint(await api('/api/automatic-backup',undefined,8000));if(readError){message('');readError=false}}
    catch(error){message('读取失败：'+error.message+'；正在重新连接，以下为上次状态');readError=true}finally{loading=false}
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
  let readError=false;
  timer=setInterval(()=>{if(visible())refresh()},1000);
  window.addEventListener('pagehide',()=>clearInterval(timer),{once:true});
  // 首屏不访问网络盘；只在设置页读取轻量本地配置与队列。
  if(visible())refresh();
})();
