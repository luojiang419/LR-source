(() => {
  'use strict';
  if(!window.FramekeepIO)return;
  const $=id=>document.getElementById(id),api=FramekeepIO.api,client=crypto.randomUUID();
  const button=document.createElement('button');button.id='mouse-remote';button.className='bordered';button.textContent='鼠标遥控';
  $('assistant-settings').after(button);
  const dialog=document.createElement('dialog');dialog.id='mouse-dialog';dialog.setAttribute('aria-labelledby','mouse-title');
  dialog.innerHTML='<div class="dialog-heading"><h2 id="mouse-title">无线鼠标遥控标星</h2><button id="mouse-close" aria-label="关闭">×</button></div><p class="dialog-copy">选择适合现场操作的标星方案，长按只计一次。可选开启滚轮浏览；接管后该鼠标不控制系统指针，另一只鼠标照常使用。</p><p class="dialog-copy">作用于末帧当前定位的照片，切图后独立标星，支持撤销与自动保存；同步到 Lightroom 仍使用“发送 LR”。</p><label class="mouse-device-label" for="mouse-device">已确认的无线接收器</label><select id="mouse-device" aria-label="无线鼠标接收器"></select><div class="mouse-functions"><label class="mouse-device-label" for="mouse-rating-mode">标星方案</label><select id="mouse-rating-mode"><option value="incremental">累加标星 · 左键加一星</option><option value="fixed">固定标星 · 左键一星 / 右键两星 / 双击取消</option></select><p id="mouse-rating-mode-help" class="dialog-copy"></p><div id="mouse-right-decrease-row"><button id="mouse-right-decrease" type="button" role="switch" aria-checked="false">右键减星：关闭</button><span>每次按下减一星，最低零星，长按不连发</span></div><div><button id="mouse-wheel-browse" type="button" role="switch" aria-checked="false">滚轮浏览：关闭</button><span>向上上一张，向下下一张，首尾不循环</span></div><p id="mouse-options-status" role="status"></p></div><p id="mouse-status" class="dialog-copy" role="status"></p><p class="dialog-copy">紧急恢复：<kbd>Ctrl + Alt + F11</kbd>。关闭程序、工作台失联超过 10 秒会恢复普通鼠标。启用后关闭此面板即可选片；其他对话框打开及切换文件夹期间暂停标星。</p><div class="dialog-actions"><button id="mouse-refresh">刷新设备</button><button id="mouse-stop">停止遥控</button><button id="mouse-start" class="primary">启用遥控</button></div>';
  document.body.append(dialog);
  const autoCard=document.createElement('div');autoCard.id='mouse-auto-card';autoCard.className='mouse-auto-card';
  autoCard.innerHTML='<div class="mouse-auto-heading"><div><span class="mouse-auto-eyebrow">自动连接</span><h4 id="mouse-auto-title">全自动切换</h4></div><button id="mouse-auto-enable" class="mouse-auto-switch" type="button" role="switch" aria-checked="false" aria-labelledby="mouse-auto-title" aria-describedby="mouse-auto-help" disabled><span aria-hidden="true"></span></button></div><p id="mouse-auto-help">开启即启用鼠标遥控，以后每次运行自动连接上次使用的接收器。</p><div class="mouse-auto-footer"><span id="mouse-auto-badge">手动启用</span><span id="mouse-auto-status" role="status">正在读取设置…</span></div>';
  dialog.before(autoCard);
  let generation=null,after=0,timer=null,running=false,starting=false;
  let lastFixed=null;
  let timeline=[],effects=[],remoteApplying=false,options={right_decrease:false,wheel_browse:false,rating_mode:'incremental',auto_enable:false},savingOptions=false,otherActive=false;
  const timestamp=()=>performance.timeOrigin+performance.now();
  function remember(){
    if(remoteApplying)return;
    const target=Framekeep.remoteTarget();target.skuContext=window.FramekeepSku?.context()||null;target.paused ||= !!document.querySelector('dialog[open]')||!!window.FramekeepWorkspace&&FramekeepWorkspace.current()!=='selection';target.catalog=window.FRAMEKEEP_SESSION.snapshot;target.browse=Framekeep.browseContext();
    const previous=timeline.at(-1),order=Framekeep.navigationOrder();
    if(!previous||JSON.stringify(previous.target)!==JSON.stringify(target)||JSON.stringify(previous.order)!==JSON.stringify(order))timeline.push({time:timestamp(),target,order});
    // 事件最长只有 10 秒的租约；留足历史处理请求延迟。
    const cutoff=timestamp()-60000;while(timeline.length>1&&timeline[1].time<cutoff)timeline.shift();effects=effects.filter(e=>e.time>=cutoff);
  }
  document.addEventListener('framekeep:selection',remember);
  document.addEventListener('framekeep:sku-context',remember);
  document.addEventListener('framekeep:workspace',remember);
  new MutationObserver(remember).observe(document.body,{subtree:true,attributes:true,attributeFilter:['open']});
  remember();
  function label(active){button.textContent=active?'鼠标遥控 · 已开启':'鼠标遥控';button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active))}
  function paintOptions(next,active=!!generation){
    if(next){options={right_decrease:next.right_decrease===true,wheel_browse:next.wheel_browse===true,rating_mode:next.rating_mode==='fixed'?'fixed':'incremental',auto_enable:next.auto_enable===true};otherActive=active&&!generation}
    $('mouse-auto-enable').setAttribute('aria-checked',String(options.auto_enable));$('mouse-auto-enable').disabled=savingOptions||starting||otherActive;
    autoCard.classList.toggle('is-enabled',options.auto_enable);$('mouse-auto-badge').textContent=options.auto_enable?'自动连接已开启':'手动启用';
    $('mouse-rating-mode').value=options.rating_mode;$('mouse-rating-mode').disabled=savingOptions||active&&!generation;
    $('mouse-right-decrease-row').hidden=options.rating_mode==='fixed';
    $('mouse-rating-mode-help').textContent=options.rating_mode==='fixed'?'左键直接设为一星，右键直接设为两星；同键 500 毫秒内双击取消星级，间隔单击保持指定星级，左右键可随时切换。':'左键每次增加一星，最多五星；未选或暂不选从一星开始，可另选右键减星。';
    for(const [key,id,name] of [['right_decrease','mouse-right-decrease','右键减星'],['wheel_browse','mouse-wheel-browse','滚轮浏览']]){const el=$(id);el.setAttribute('aria-checked',String(options[key]));el.textContent=name+'：'+(options[key]?'开启':'关闭');el.disabled=savingOptions||active&&!generation}
  }
  async function saveOptions(change){
    if(savingOptions)return;lastFixed=null;savingOptions=true;paintOptions();
    try{const state=await api('/api/mouse/options',{client,options:change});paintOptions(state.options,state.active);$('mouse-options-status').textContent='功能设置已保存，立即生效'}catch(e){$('mouse-options-status').textContent=e.message}finally{savingOptions=false;paintOptions()}
  }
  for(const [key,id] of [['right_decrease','mouse-right-decrease'],['wheel_browse','mouse-wheel-browse']])$(id).onclick=()=>saveOptions({[key]:!options[key]});
  $('mouse-rating-mode').onchange=()=>saveOptions({rating_mode:$('mouse-rating-mode').value});
  function adopt(state){
    lastFixed=null;generation=state.generation;after=state.sequence;timeline=[];effects=[];remember();label(true);paintOptions(state.options,true);poll();
  }
  async function autoStart(){
    if(starting||generation)return;starting=true;paintOptions();
    try{
      await window.FramekeepComponents?.driver(true);
      const state=await api('/api/mouse/auto-start',{client});
      if(state.generation){adopt(state);$('mouse-auto-status').textContent='遥控已就绪 · 退出软件自动恢复普通鼠标'}
    }catch(e){$('mouse-auto-status').textContent=e.message;Framekeep.notify(e.message)}
    finally{starting=false;paintOptions()}
  }
  $('mouse-auto-enable').onclick=async()=>{
    if(savingOptions||starting)return;savingOptions=true;paintOptions();
    try{
      const state=await api('/api/mouse/options',{client,options:{auto_enable:!options.auto_enable}});
      if(!state.active){generation=null;clearTimeout(timer);label(false)}
      paintOptions(state.options,state.active);$('mouse-auto-status').textContent=options.auto_enable?'设置已保存，正在自动连接…':'已关闭 · 无线鼠标恢复普通使用';
      if(options.auto_enable)await autoStart();
    }catch(e){$('mouse-auto-status').textContent=e.message}
    finally{savingOptions=false;paintOptions()}
  };
  async function refresh(){
    await window.FramekeepComponents?.driver();
    const previous=$('mouse-device').selectedOptions[0]?.dataset.hardware;
    let state=await api('/api/mouse'),activation=null;
    if(!state.active&&!state.devices.length&&!state.error){activation=await window.FramekeepComponents?.driver(true);state=await api('/api/mouse')}
    paintOptions(state.options,state.active);$('mouse-device').replaceChildren();
    for(const device of state.devices){const option=document.createElement('option');option.value=device.driver_id;option.dataset.hardware=device.hardware;option.dataset.profile=device.profile||'';option.textContent=`${device.label||'原无线鼠标'} · ${device.hardware} · ${device.id}`;option.selected=device.hardware===previous;$('mouse-device').append(option)}
    $('mouse-status').textContent=state.error||state.message||'未找到目标接收器，请插入后刷新。';
    if(!state.devices.length&&!state.error)$('mouse-status').textContent=activation?.message||'未找到可用接收器，请检查是否插入或被其他工具占用。首次安装驱动后，请重新插入接收器；仍未识别时请重启电脑。';
    $('mouse-start').disabled=state.active||state.devices.length===0||starting;$('mouse-stop').disabled=!state.active;
    label(state.active);
  }
  async function poll(){
    if(!generation||running)return;
    running=true;const source=generation;
    try{
      remember();const result=await api('/api/mouse/poll',{client,generation:source,after});
      if(generation!==source)return;
      if(!result.active){generation=null;label(false);$('mouse-auto-status').textContent=result.message+'；本次运行不自动重启';Framekeep.notify(result.message);if(dialog.open)await refresh();return}
      remoteApplying=true;
      try{for(const event of result.events){
        if(event.sequence<=after)continue;
        const base=timeline.findLast(item=>item.time<=event.time_ms),effect=base&&effects.findLast(item=>item.time<=event.time_ms&&item.time>base.time&&item.target.sessionId===base.target.sessionId),entry=effect||base,action=event.action||'increase';
        // 先按接收顺序构造逻辑切图，再将后续加/减星绑定到该照片；目录/弹窗变化重置该链。
        if(entry&&!entry.target.paused&&entry.target.sessionId===window.FRAMEKEEP_SESSION.id&&entry.target.photoId){
          const target=entry.target;
          if(['set_one','set_two'].includes(action)&&options.rating_mode==='fixed'){
            const clickKey=JSON.stringify([generation,action,target.sessionId,target.photoId,entry.time]);
            const double=lastFixed?.key===clickKey&&event.time_ms-lastFixed.time>=0&&event.time_ms-lastFixed.time<=500;
            Framekeep.assignRemoteRating(target.photoId,double?0:action==='set_one'?1:2,target.skuContext);
            lastFixed=double?null:{key:clickKey,time:event.time_ms};
          }
          else if(action==='increase'&&options.rating_mode==='incremental')Framekeep.incrementRating(target.photoId,target.skuContext);
          else if(action==='decrease'&&options.rating_mode==='incremental'&&options.right_decrease)Framekeep.decrementRating(target.photoId,target.skuContext);
          else if(['previous','next'].includes(action)&&options.wheel_browse){
            lastFixed=null;
            const index=entry.order.indexOf(target.photoId),id=index<0?null:entry.order[index+(action==='previous'?-1:1)];
            if(id){const next={time:event.time_ms,target:{...target,photoId:id},order:entry.order};effects.push(next);
              if(!timeline.some(item=>item.time>event.time_ms)&&!Framekeep.remoteTarget().paused)Framekeep.browsePhoto(id);
            }
          }
        }
        after=event.sequence;
      }}finally{remoteApplying=false;remember()}
    }catch(e){if(dialog.open)$('mouse-status').textContent='连接中断：'+e.message}
    finally{running=false;clearTimeout(timer);if(generation)timer=setTimeout(poll,120)}
  }
  button.addEventListener('click',()=>{dialog.showModal();remember();refresh().catch(e=>$('mouse-status').textContent=e.message)});
  $('mouse-close').addEventListener('click',()=>{dialog.close();remember()});
  $('mouse-refresh').addEventListener('click',()=>refresh().catch(e=>$('mouse-status').textContent=e.message));
  $('mouse-start').addEventListener('click',async()=>{
    if(starting)return;starting=true;paintOptions();$('mouse-start').disabled=true;
    try{
      await FramekeepIO.flush();
      await window.FramekeepComponents?.driver();
      const option=$('mouse-device').selectedOptions[0];
      const device=option?.dataset.profile?{driver_id:Number(option.value),hardware:option.dataset.hardware,profile:option.dataset.profile}:Number(option?.value);
      const state=await api('/api/mouse/start',{client,device});
      dialog.close();window.FramekeepWorkspace?.show('selection');adopt(state);$('mouse-auto-status').textContent='遥控已就绪 · Ctrl+Alt+F11 恢复普通鼠标';
      Framekeep.notify(options.rating_mode==='fixed'?'鼠标遥控已开启，左键一星、右键两星；Ctrl+Alt+F11 恢复':'鼠标遥控已开启，左键加一星；Ctrl+Alt+F11 恢复');
    }catch(e){$('mouse-status').textContent=e.message}
    finally{starting=false;paintOptions();if(dialog.open)refresh().catch(()=>{})}
  });
  $('mouse-stop').addEventListener('click',async()=>{
    try{await api('/api/mouse/stop',{});generation=null;clearTimeout(timer);label(false);$('mouse-auto-status').textContent='已停止 · 本次运行不自动重启';await refresh()}
    catch(e){$('mouse-status').textContent=e.message}
  });
  // 页面关闭后由租约释放，避免异步请求误停刚转交的新会话。
  api('/api/mouse/options').then(async state=>{
    paintOptions(state.options,state.active);$('mouse-auto-status').textContent=options.auto_enable?'正在自动连接…':'开启后无需每次手动启用';
    if(options.auto_enable)await autoStart();
  }).catch(e=>{$('mouse-auto-status').textContent=e.message});
})();
