(() => {
  'use strict';
  if(!window.FramekeepIO||!document.getElementById('lan-sync-controls'))return;
  const $=id=>document.getElementById(id),api=FramekeepIO.api;
  const css=document.createElement('link');css.rel='stylesheet';css.href='/lan-capture-sync.css';document.head.append(css);
  $('lan-sync-controls').classList.remove('workspace-card');
  $('lan-sync-controls').innerHTML=`<section class="workspace-card lan-sync-card lan-sync-machine" aria-label="本机设置">
    <div class="lan-sync-toggle"><label for="lan-sync-enabled">局域网同步</label><input id="lan-sync-enabled" class="toggle-switch" type="checkbox" role="switch"></div>
    <div class="workspace-fields"><label>本机名称<input id="lan-sync-name" maxlength="80" autocomplete="off"></label><label>同步密码<input id="lan-sync-password" type="password" maxlength="256" autocomplete="new-password"></label></div>
    </section><section class="workspace-card lan-sync-card lan-sync-storage" aria-label="存储位置">
    <label for="lan-sync-folder">小档存储文件夹</label><div class="lan-sync-path"><input id="lan-sync-folder" autocomplete="off" spellcheck="false"><button id="lan-sync-browse">选择文件夹</button></div>
    <div class="asset-actions"><button id="lan-sync-save" class="primary">保存设置</button></div><output id="lan-sync-status" role="status" aria-live="polite"></output>
    </section><section id="lan-sync-clients" class="workspace-card lan-sync-card" aria-labelledby="lan-sync-peers-title">
    <header><h3 id="lan-sync-peers-title">局域网客户端</h3><div class="lan-sync-client-actions"><button id="lan-sync-exit" type="button" disabled>断开同步</button><button id="lan-sync-refresh" type="button">刷新</button></div></header>
    <div id="lan-sync-connection" hidden><div><strong id="lan-sync-connection-name"></strong><span id="lan-sync-connection-state"></span></div><button id="lan-sync-connection-disconnect" type="button">断开同步</button></div>
    <p class="lan-sync-topology">主机负责拍摄和修改，多台客户端独立浏览。客户端联机后停止本机监控，照片存入各自的小档目录。</p><p id="lan-sync-current-set" role="status"></p><table id="lan-sync-client-table" hidden aria-label="接收客户端同步状态"><thead><tr><th>客户端</th><th>地址 / 版本</th><th>同步状态</th><th>操作</th></tr></thead><tbody id="lan-sync-connected-list"></tbody></table>
    <div id="lan-sync-peers" role="list" aria-label="局域网客户端"></div><output id="lan-sync-peers-status" role="status" aria-live="polite"></output>
    <menu id="lan-sync-peer-menu" role="menu" aria-label="客户端操作" hidden><li role="none"><button id="lan-sync-peer-connect" role="menuitem">联机</button></li></menu></section>`;
  const login=document.createElement('dialog');login.id='lan-sync-login';login.setAttribute('aria-labelledby','lan-sync-login-title');
  login.innerHTML=`<form><header><h2 id="lan-sync-login-title">联机</h2><button type="button" data-login-cancel aria-label="关闭">×</button></header><label for="lan-sync-code">同步码</label><input id="lan-sync-code" type="password" autocomplete="off" maxlength="256" required><label for="lan-sync-share-root">拍摄共享目录（可选）</label><input id="lan-sync-share-root" type="text" spellcheck="false" autocomplete="off" placeholder="留空自动使用主机共享"><output id="lan-sync-login-status" role="alert"></output><div class="asset-actions"><button type="button" data-login-cancel>取消</button><button id="lan-sync-login-submit" class="primary" type="submit">联机</button></div></form>`;
  document.body.append(login);
  const badge=document.createElement('div');badge.id='lan-sync-badge';badge.hidden=true;badge.innerHTML='<span></span><button type="button">断开同步</button>';(document.querySelector('.workspace-tool-content')||document.querySelector('.filterbar')||document.body).append(badge);
  let state=null,peers=[],selected=null,loading=false,editing=false,lastRevision='',lastProject='',lastPhoto='',timer;
  let discovering=false,discoveryTimer,lastDiscovery=0,peerSignature='',disconnecting=false,epoch=0;
  const notify=e=>Framekeep.notify(e.message||String(e));
  const message=text=>$('lan-sync-status').textContent=text;
  function paint(value,fill=false){
    const roleChanged=!!value.receiving!==!!state?.receiving;
    state=value;window.FramekeepLanSync={receiving:()=>!!state?.receiving};
    if(roleChanged)document.dispatchEvent(new Event('framekeep:sync-role'));
    const clients=state.connected_clients||[],connected=!!state.receiving||!!clients.length;
    document.body.classList.toggle('lan-sync-receiving',!!state.receiving);badge.hidden=!connected;
    const phase={idle:'',connecting:'正在联机',receiving:`接收 ${state.received_files||0} / ${state.total_files||0}`,fetching:'正在获取当前一套',waiting_host:'主机未开启现场监控',following:'已同步',reconnecting:'连接中断，正在重连'}[state.phase]||'';
    const host=state.host||{},shooting=state.receiving?(host.canon?.enabled?'联机拍摄中':host.watch?.enabled?'目录拍摄中':'主机待机'):'';
    const names=clients.slice(0,3).map(c=>c.name).join('、')+(clients.length>3?' 等':''),sending=state.sending_count?'正在发送 · '+state.sending_count+' 台':clients.length?'已连接 · '+(state.connected_count||0)+' 台':'';
    const current=state.receiving?state.set:state.current_set;
    $('lan-sync-current-set').textContent=current?`当前一套：${current.folder} · ${current.date} · ${current.count} 张`+(!state.receiving?` · ${state.connected_count||0} 台已连接 · ${state.synced_count||0} 台同步完成`:''):'主机未开启现场监控';
    const last=state.receiving&&state.last_sync&&state.phase==='following'?[state.latest_photo?'最新照片 '+new Date(state.latest_photo).toLocaleTimeString():'',`最近同步 ${new Date(state.last_sync*1000).toLocaleTimeString()}`,state.skipped?.length?'跳过 '+state.skipped.length+' 张':''].filter(Boolean).join(' · '):'';
    $('lan-sync-status').title=(state.skipped||[]).map(r=>(r.name||'照片')+'：'+r.reason).join('\n');
    const text=[state.peer?.name,shooting,phase,state.receiving&&state.transport==='SMB'?'共享直读':'',sending,state.detail,last,state.error||state.discovery_error].filter(Boolean).join(' · ');
    badge.title=text;badge.querySelector('span').textContent=text;message(text);
    for(const button of [$('lan-sync-exit'),$('lan-sync-connection-disconnect'),badge.querySelector('button')]){button.disabled=disconnecting||!connected;button.textContent=clients.length>1?'全部断开':'断开同步'}
    $('lan-sync-connection').hidden=!connected;
    $('lan-sync-connection-name').textContent=[state.receiving?state.peer?.name:'',names].filter(Boolean).join('、');
    $('lan-sync-connection-state').textContent=[state.receiving?phase:'',sending].filter(Boolean).join(' · ');
    $('lan-sync-client-table').hidden=!clients.length;
    const connectedList=$('lan-sync-connected-list'),existing=new Map([...connectedList.children].map(node=>[node.dataset.clientDevice,node]));
    for(const client of clients){
      let item=existing.get(client.device);
      if(!item){
        item=document.createElement('tr');item.className='lan-sync-connected-client';item.dataset.clientDevice=client.device;
        const info=document.createElement('td');info.append(document.createElement('strong'));
        const address=document.createElement('td');address.className='lan-sync-client-address';
        const progress=document.createElement('td');progress.className='lan-sync-client-progress';
        const action=document.createElement('td');
        const button=document.createElement('button');button.type='button';button.textContent='断开此台';
        button.onclick=async()=>{button.disabled=true;try{paint(await api('/api/lan-sync/disconnect-client',{device:client.device}));message('已断开此客户端，其它客户端继续同步')}catch(error){notify(error)}finally{button.disabled=false}};
        action.append(button);item.append(info,address,progress,action);
      }
      item.querySelector('strong').textContent=client.name;item.querySelector('.lan-sync-client-address').textContent=[client.address,client.version,client.local_instance?'本机另一实例':''].filter(Boolean).join(' · ');item.querySelector('.lan-sync-client-progress').textContent=[client.status,client.current_file].filter(Boolean).join(' · ');item.title=(client.skipped||[]).map(r=>(r.name||'照片')+'：'+r.reason).join('\n');item.querySelector('button').setAttribute('aria-label','断开 '+client.name);
      connectedList.append(item);existing.delete(client.device);
    }
    for(const item of existing.values())item.remove();
    $('lan-sync-refresh').disabled=!state.enabled||discovering;
    $('lan-sync-folder').disabled=!!state.receiving;$('lan-sync-browse').disabled=!!state.receiving;
    if(fill||!editing){$('lan-sync-enabled').checked=!!state.enabled;$('lan-sync-name').value=state.name||'';$('lan-sync-folder').value=state.folder||'';$('lan-sync-password').placeholder=state.password_set?'已设置，留空保留':''}
    if(!state.enabled){peers=[];peerSignature='';lastDiscovery=0;$('lan-sync-peers').replaceChildren();$('lan-sync-peer-menu').hidden=true;$('lan-sync-peers-status').textContent='同步已关闭'}
    scheduleDiscovery();
  }
  for(const id of ['lan-sync-name','lan-sync-password','lan-sync-folder'])$(id).addEventListener('input',()=>editing=true);
  async function save(){
    const next=await api('/api/lan-sync/configure',{enabled:$('lan-sync-enabled').checked,name:$('lan-sync-name').value,password:$('lan-sync-password').value,...($('lan-sync-folder').value?{folder:$('lan-sync-folder').value}:{})});
    editing=false;$('lan-sync-password').value='';paint(next,true);message('已保存');
  }
  $('lan-sync-save').onclick=()=>save().catch(e=>message(e.message));
  $('lan-sync-enabled').onchange=async()=>{try{await save()}catch(e){$('lan-sync-enabled').checked=!!state?.enabled;message(e.message)}};
  $('lan-sync-browse').onclick=async()=>{try{if(window.FramekeepDesktop?.chooseFolders){const folders=await FramekeepDesktop.chooseFolders();if(folders?.[0]){$('lan-sync-folder').value=folders[0];editing=true}}else $('lan-sync-folder').focus()}catch(e){message(e.message)}};
  async function exit(){
    if(disconnecting)return;disconnecting=true;epoch++;
    for(const button of [$('lan-sync-exit'),$('lan-sync-connection-disconnect'),badge.querySelector('button')])button.disabled=true;
    try{if(!state?.receiving)await FramekeepIO.flush();const next=await api('/api/lan-sync/disconnect',{});lastRevision='';lastProject='';lastDiscovery=0;peerSignature='';paint(next,true);await FramekeepIO.refreshFolders();message('已断开同步，文件和拍摄进度已保留')}
    finally{disconnecting=false;for(const button of [$('lan-sync-exit'),$('lan-sync-connection-disconnect'),badge.querySelector('button')])button.disabled=!state?.receiving&&!state?.connected_clients?.length}
  }
  for(const button of [$('lan-sync-exit'),$('lan-sync-connection-disconnect'),badge.querySelector('button')])button.onclick=()=>exit().catch(notify);
  function choose(peer){if(!peer||peer.receiving||state?.receiving)return;selected=peer;$('lan-sync-login-title').textContent='联机 · '+peer.name;$('lan-sync-code').value='';$('lan-sync-share-root').value=state?.shared_folders?.[peer.device]||'';$('lan-sync-login-status').textContent='';login.showModal();$('lan-sync-code').focus()}
  const clientsVisible=()=>!document.hidden&&window.FramekeepWorkspace?.current()==='settings'&&!$('settings-sync').hidden;
  function scheduleDiscovery(){
    clearTimeout(discoveryTimer);
    if(!state?.enabled||!clientsVisible()||discovering)return;
    discoveryTimer=setTimeout(discover,Math.max(0,8000-(Date.now()-lastDiscovery)));
  }
  async function discover(){
    if(discovering||!state?.enabled)return;
    discovering=true;$('lan-sync-refresh').disabled=true;$('lan-sync-peers').setAttribute('aria-busy','true');
    try{
      const result=await api('/api/lan-sync/discover',{},10000);
      if(!state?.enabled)return;
      peers=result.peers;
      const nextSignature=JSON.stringify([!!state.receiving,state.peer?.device,peers.map(p=>[p.device,p.name,p.address,p.receiving])]);
      if(nextSignature!==peerSignature){
        const focused=document.activeElement?.dataset.syncDevice;
        $('lan-sync-peer-menu').hidden=true;$('lan-sync-peers').replaceChildren();
        for(const peer of peers){
          const item=document.createElement('div');item.setAttribute('role','listitem');
          const button=document.createElement('button');button.type='button';button.className='lan-sync-peer';button.dataset.syncDevice=peer.device;button.disabled=peer.receiving||state.receiving;
          const name=document.createElement('strong'),address=document.createElement('span');name.textContent=peer.name;address.textContent=peer.address.replace('http://','')+(state.peer?.device===peer.device?' · 已联机':peer.receiving?' · 接收中':'');button.append(name,address);
          button.onclick=()=>{selected=peer};button.ondblclick=()=>{if(!login.open)choose(peer)};button.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();choose(peer)}};
          button.oncontextmenu=e=>{e.preventDefault();selected=peer;const menu=$('lan-sync-peer-menu');menu.hidden=false;menu.style.left=Math.max(8,Math.min(e.clientX,innerWidth-168))+'px';menu.style.top=Math.max(8,Math.min(e.clientY,innerHeight-60))+'px';$('lan-sync-peer-connect').focus()};item.append(button);$('lan-sync-peers').append(item);
          if(focused===peer.device)button.focus();
        }
        peerSignature=nextSignature;
      }
      $('lan-sync-peers-status').textContent=result.error||(peers.length?'':'未发现客户端');
    }catch(e){$('lan-sync-peers-status').textContent=e.message}finally{$('lan-sync-peers').removeAttribute('aria-busy');discovering=false;lastDiscovery=Date.now();$('lan-sync-refresh').disabled=!state?.enabled;scheduleDiscovery()}
  }
  $('lan-sync-refresh').onclick=discover;
  $('lan-sync-peer-connect').onclick=()=>{$('lan-sync-peer-menu').hidden=true;choose(selected)};
  document.addEventListener('click',e=>{if(!e.target.closest('#lan-sync-peer-menu'))$('lan-sync-peer-menu').hidden=true});
  $('lan-sync-peer-menu').addEventListener('keydown',e=>{e.stopPropagation();if(e.key==='Escape'){e.preventDefault();$('lan-sync-peer-menu').hidden=true}});
  for(const b of login.querySelectorAll('[data-login-cancel]'))b.onclick=()=>login.close();
  login.addEventListener('keydown',e=>e.stopPropagation());
  login.querySelector('form').onsubmit=async e=>{
    e.preventDefault();$('lan-sync-login-submit').disabled=true;
    try{await FramekeepIO.flush();paint(await api('/api/lan-sync/connect',{device:selected.device,password:$('lan-sync-code').value,share_root:$('lan-sync-share-root').value},20000),true);login.close();$('lan-sync-code').value='';lastRevision='';FramekeepWorkspace.show('progress');await poll()}
    catch(error){$('lan-sync-login-status').textContent=error.message}finally{$('lan-sync-login-submit').disabled=false}
  };
  const writeControls='#send-lr,#asset-preview,#asset-parse-sku,#sku-import,#sku-bind-current,#sku-sync-progress,#shooting-import,#shooting-full-auto,#full-auto-ocr,#watch-start,#canon-toggle,#stars,[data-pick],[data-sku-edit],[data-rating],[data-star]';
  document.addEventListener('click',e=>{if(state?.receiving&&e.target.closest(writeControls)){e.preventDefault();e.stopImmediatePropagation();Framekeep.notify('请先退出同步再修改')}},true);
  window.addEventListener('keydown',e=>{if(state?.receiving&&!e.target.closest('input,textarea,select,dialog')&&(/^\d$/.test(e.key)||['Delete','Backspace'].includes(e.key))){e.preventDefault();e.stopImmediatePropagation()}},true);
  async function poll(){
    if(loading)return;loading=true;const observed=epoch;
    try{
      const next=await api('/api/lan-sync');if(observed!==epoch)return;paint(next);
      if(next.receiving&&next.revision&&next.revision!==lastRevision){
        if(next.project_id&&next.project_id!==lastProject){await api('/api/sku/activate',{project_id:next.project_id});lastProject=next.project_id}
        const follow=next.latest_photo&&next.latest_photo!==lastPhoto&&window.FramekeepWatch?.getFollow()!==false&&!Framekeep.remoteTarget().paused&&!document.querySelector('dialog[open]')&&!Framekeep.persistedState().query&&window.FramekeepWorkspace?.current()==='selection'?next.latest_capture_id:null;
        if(await FramekeepIO.followSync(next.session_id,follow)===false)return;await FramekeepIO.refreshFolders();
        document.dispatchEvent(new CustomEvent('framekeep:ocr-choice',{detail:{session_id:next.session_id}}));FramekeepWorkspace.refresh();lastRevision=next.revision;lastPhoto=next.latest_photo||lastPhoto;
      }
    }catch(e){if(state?.receiving)message(e.message)}finally{loading=false;clearTimeout(timer);timer=setTimeout(poll,state?.receiving?250:5000)}
  }
  document.querySelector('[data-settings-section="sync"]').addEventListener('click',poll);
  document.addEventListener('framekeep:workspace',()=>{scheduleDiscovery();if(clientsVisible())poll()});
  document.addEventListener('visibilitychange',scheduleDiscovery);
  window.addEventListener('pagehide',()=>{clearTimeout(timer);clearTimeout(discoveryTimer)},{once:true});poll();
})();
