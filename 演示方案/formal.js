(() => {
  'use strict';
  const session=window.FRAMEKEEP_SESSION;
  const $=id=>document.getElementById(id);
  if(!session||session.demo){for(const id of ['open-folder','add-folder','add-folder-row','send-lr'])$(id).disabled=true;return}
  function canonical(value){if(Array.isArray(value))return'['+value.map(canonical).join(',')+']';if(value&&typeof value==='object')return'{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonical(value[k])).join(',')+'}';return JSON.stringify(value)}
  const sharedState=(saved,shared)=>({...saved,overrides:{...saved?.overrides,...shared}});
  function changes(next,previous){
    const result={};
    for(const [key,value] of Object.entries(next)){
      if(['overrides','rejectedPrevious','skuMarkers'].includes(key)){
        const delta={};for(const id of new Set([...Object.keys(previous[key]||{}),...Object.keys(value||{})]))if(canonical(value?.[id])!==canonical(previous[key]?.[id]))delta[id]=Object.hasOwn(value||{},id)?value[id]:null;
        if(!next.protectManual&&(next.keep!==previous.keep||next.threshold!==previous.threshold)&&['overrides','rejectedPrevious'].includes(key))for(const id of Object.keys(delta))if(delta[id]===null)delete delta[id];
        if(Object.keys(delta).length)result[key]=delta;
      }else if(canonical(value)!==canonical(previous[key]))result[key]=value;
    }
    return result;
  }
  let token=session.requestToken||null,revision=session.revision,timer=null,conflicted=false,lastSaved=canonical(sharedState(session.saved,session.sharedRatings)),saving=Promise.resolve(),bridgeTimer=null;
  async function api(path,body,timeout=20000,signal=null){
    const finish=window.FramekeepTaskStatus?.begin(path,body);
    try{
    if(body!==undefined)await window.FramekeepComponents?.before(path,body);
    if(body!==undefined)body={session_id:session.id,...body};
    if(body!==undefined&&!token){const response=await fetch('/api/state',{signal:AbortSignal.timeout(timeout)});if(!response.ok)throw new Error('无法获取写入凭证，请稍后重试');const state=await response.json();token=state.request_token}
    const response=await fetch(path,{...(body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Framekeep-Token':token},body:JSON.stringify(body)}),signal:signal?AbortSignal.any([signal,AbortSignal.timeout(timeout)]):AbortSignal.timeout(timeout)});const data=await response.json();if(!response.ok){if(response.status===409&&['/api/session','/api/session/patch'].includes(path))conflicted=true;throw new Error(data.error||`请求失败 ${response.status}`)}return data;
    }finally{finish?.()}
  }
  function stash(){if(!session.id)return;try{localStorage.setItem('framekeep:'+session.id,JSON.stringify({snapshot:session.snapshot,revision,state:Framekeep.persistedState()}))}catch{}}
  function partialBody(state){
    const previous=JSON.parse(lastSaved),delta=changes(state,previous),baseline={},stateBaseline={};
    for(const [key,value] of Object.entries(delta)){
      stateBaseline[key]=['overrides','rejectedPrevious','skuMarkers'].includes(key)?Object.fromEntries(Object.keys(value).map(id=>[id,previous[key]?.[id]??null])):previous[key]??null;
    }
    for(const id of Object.keys(delta.overrides||{}))if(Object.hasOwn(previous.overrides||{},id))baseline[id]=previous.overrides[id];
    return {session_id:session.id,revision,changes:delta,state_baseline:stateBaseline,rating_baseline:baseline};
  }
  async function flush(){
    clearTimeout(timer);if(!session.id)return;
    // 入队时冻结本次操作，避免保存中的快速 2→0→2 被合并为重复二星。
    const serialized=canonical(Framekeep.persistedState()),state=JSON.parse(serialized);
    saving=saving.catch(()=>{}).then(async()=>{if(serialized===lastSaved)return;$('save-status').textContent='保存中…';const paged=!!session.browser?.paged,body=partialBody(state);if(!paged)body.state=state;const result=await api(paged?'/api/session/patch':'/api/session',body);revision=result.revision;conflicted=false;const previous=JSON.parse(lastSaved);lastSaved=serialized;stash();$('save-status').textContent='已保存';window.FramekeepPaging?.saved(previous,state);if(canonical(previous.overrides||{})!==canonical(state.overrides||{}))document.dispatchEvent(new CustomEvent('framekeep:ratings-saved',{detail:{session_id:session.id}}));if(result.sku_error)Framekeep.notify(result.sku_error)});
    return saving;
  }
  function schedule(){if(switching||window.FramekeepPaging?.busy)return;stash();clearTimeout(timer);const save=()=>flush().catch(e=>{$('save-status').textContent=e.message});if(canonical(Framekeep.persistedState().overrides||{})!==canonical(JSON.parse(lastSaved).overrides||{}))save();else timer=setTimeout(save,450)}
  document.addEventListener('framekeep:selection',schedule);$('thumb-size').addEventListener('input',schedule);$('thumb-smaller').addEventListener('click',schedule);$('thumb-larger').addEventListener('click',schedule);$('catalog-view').addEventListener('wheel',e=>{if(e.ctrlKey)schedule()},{passive:true});
  window.addEventListener('pagehide',()=>{stash();const state=Framekeep.persistedState();if(token&&session.id&&!conflicted&&canonical(state)!==lastSaved){const paged=!!session.browser?.paged,body=partialBody(state);if(!paged)body.state=state;fetch(paged?'/api/session/patch':'/api/session',{method:'POST',headers:{'Content-Type':'application/json','X-Framekeep-Token':token},body:JSON.stringify(body),keepalive:true}).catch(()=>{})}});
  async function reloadRatings(){
    await window.FramekeepPaging?.whenIdle();
    if(!session.id||switching)return;
    await flush();
    const next=await api('/api/state?'+(session.browser?.paged?FramekeepPaging.query({around:Framekeep.remoteTarget().photoId||''}):'session_id='+encodeURIComponent(session.id)));
    if(next.session?.id!==session.id)return;
    revision=next.session.revision;conflicted=false;
    if(next.session.browser?.paged)Framekeep.loadPage(next.session,next.catalog,Framekeep.remoteTarget().photoId);
    else Framekeep.syncSharedRatings(next.session.sharedRatings||{});
    lastSaved=canonical(Framekeep.persistedState());stash();sessionDetails();
  }
  window.FramekeepIO={flush,api,openSession,refreshFolders,refreshLive,reloadRatings,
    adoptPage(next){revision=next.revision;conflicted=false;lastSaved=canonical({...Framekeep.persistedState(),...next.browser?.storedOptions,selectedId:next.state.selectedId??null});stash();sessionDetails()},
    manifest:async selected=>{await flush();return api('/api/export',{selected_only:selected})}};
  let count=session.browser?.total??window.DEMO_PHOTOS.length;
  document.querySelector('.session-title').textContent=session.name+' · '+count+' 张';
  document.querySelector('.source-caption').textContent='RAW / JPG 配对 · 支持备案补写';$('source-status').textContent=`${count} 个拍摄编号 · RAW 内嵌预览 / 配对 JPG`;
  $('folder-path').value='';
  const summary=document.querySelector('.summary-line').firstElementChild;
  function sessionDetails(){
    count=session.browser?.total??window.DEMO_PHOTOS.length;document.querySelector('.session-title').textContent=session.name+' · '+count+' 张';window.FramekeepPaging?.paint();
    $('source-status').textContent=`${count} 个拍摄编号 · RAW 内嵌预览 / 配对 JPG`;summary.innerHTML=`已选 <strong id="selected-count">${$('selected-count').textContent}</strong> / ${count}`;
    $('send-lr').disabled=!count;
  }
  async function refreshLive(sid,followId=null,enter=false){
    await window.FramekeepPaging?.whenIdle();
    if(switching)return false;
    if(session.id!==sid){if(!enter)return false;await openSession(sid)}
    if(session.id!==sid||switching)return false;
    const next=await api('/api/state?'+(session.browser?.paged?FramekeepPaging.query({around:followId||Framekeep.remoteTarget().photoId||''}):'session_id='+encodeURIComponent(sid)));
    if(session.id!==sid||switching)return false;
    // 保留本地人工覆盖、保存队列及鼠标会话；新增目录不替换会话 revision。
    if(!Framekeep.updateLiveCatalog(next.session,next.catalog,followId,enter))return false;
    sessionDetails();schedule();return true;
  }
  function explain(title,text){$('info-title').textContent=title;$('info-content').textContent=text;$('info-dialog').showModal()}
  // 覆盖原型中的说明入口，防止显示演示限制文案。
  for(const [id,title,text] of [['source-info','照片来源',`${session.root}\n${count} 个拍摄编号。时间来自 EXIF（优先 RAW 子秒），未提供时间的照片单独保留。`],['raw-info','RAW 读取',`ExifTool 13.59 提取元数据及内嵌 JPEG，LibRaw 0.22.1 按需解码。当前 Canon R6 Mark II CR3 可实际读取；其他机型以样片验证为准。`],['lr-info','Lightroom 桥接','在 LR 桥接插件连接工作台后，点击“发送到 LR”同步当前已选照片的星级。LR 会先预览匹配结果，确认后写入星级。已有 2–5 星受保护，未列出的照片不改动。']]){
    const old=$(id),button=old.cloneNode(true);old.replaceWith(button);button.addEventListener('click',()=>explain(title,id==='source-info'?`${session.root}\n${count} 个拍摄编号。时间来自 EXIF（优先 RAW 子秒），未提供时间的照片单独保留。`:text));
  }
  let folderState={sessions:[],scans:[]},folderTimer=null,folderSignature='',switching=false,folderRequest=0;
  const busy=job=>['queued','running','cancelling'].includes(job.status);
  const pathKey=path=>path.replaceAll('\\','/').replace(/\/$/,'').toLowerCase();
  const labels={queued:'等待索引',discovering:'正在查找照片',indexed_previews:'正在加载索引照片预览',metadata:'正在读取拍摄时间',saving:'正在保存索引',cancelled:'已停止',cancelling:'正在停止',failed:'索引失败'};
  function folderSelection(){
    for(const button of $('folder-list').querySelectorAll('[data-session]')){
      const selected=button.dataset.session===session.id;button.classList.toggle('active',selected);button.classList.toggle('loading',switching&&button.dataset.session===pendingSession);
      if(selected)button.setAttribute('aria-current','true');else button.removeAttribute('aria-current');
      button.firstElementChild.textContent=(selected?'▾ ':'▸ ')+button.dataset.name;
    }
  }
  let pendingSession=null;
  async function openSession(id){
    if(switching)return;
    await window.FramekeepPaging?.whenIdle();if(switching)return;
    if(id===session.id){await flush();await api('/api/activate',{session_id:id});Framekeep.showAll();return}
    switching=true;pendingSession=id;Framekeep.setSessionLoading(true);document.querySelector('.header-actions').inert=true;folderSelection();
    $('folder-activity').classList.remove('hidden');$('folder-activity').textContent='正在加载文件夹…';
    try{
      await flush();
      const next=await api('/api/state?session_id='+encodeURIComponent(id));
      if(next.session?.id!==id)throw new Error('文件夹数据不匹配，请重试。');
      const saved=next.session.state||{};
      await api('/api/activate',{session_id:id});
      revision=next.session.revision;lastSaved=canonical(sharedState(saved,next.session.sharedRatings));conflicted=false;$('save-status').textContent='已保存';
      Framekeep.loadSession({...next.session,saved,demo:false},next.catalog);
      sessionDetails();folderSelection();
    }catch(e){Framekeep.notify('切换失败，仍在当前文件夹：'+e.message)}
    finally{switching=false;pendingSession=null;Framekeep.setSessionLoading(false);document.querySelector('.header-actions').inert=false;folderSelection();folderActivity();schedule()}
  }
  const folderEntries=new Map();
  function reconcile(parent,nodes){
    nodes.forEach((node,index)=>{if(parent.children[index]!==node)parent.insertBefore(node,parent.children[index]||null)});
    while(parent.children.length>nodes.length)parent.lastElementChild.remove();
  }
  function folderActivity(){
    if(switching)return;
    const pending=folderState.scans.filter(busy);$('folder-activity').classList.toggle('hidden',!pending.length);$('folder-activity').textContent=pending.length?`后台索引 ${pending.length} 个文件夹 · 可继续选片`:'';
  }
  function renderFolders(){
    const signature=JSON.stringify(folderState);if(signature===folderSignature){folderActivity();return}folderSignature=signature;
    const list=$('folder-list'),entries=[],shortcuts=[];
    const latest=new Map();for(const job of folderState.scans)latest.set(pathKey(job.folder),job);
    function progress(job){
      const row=document.createElement('div');row.className='folder-job '+job.status;row.title=job.folder;
      const text=document.createElement('div');text.className='folder-job-copy';
      const label=document.createElement('span');label.textContent=labels[job.status]||labels[job.stage]||job.status;
      if(job.total)label.textContent+=` · ${job.done} / ${job.total}`;text.append(label);
      if(job.error){const error=document.createElement('small');error.textContent=job.error;error.title=job.error;text.append(error)}
      if(job.status==='running'){
        const bar=document.createElement('progress');bar.max=job.total||1;if(job.total)bar.value=job.done;bar.setAttribute('aria-label',job.name+'索引进度');text.append(bar);
        if(job.current){const current=document.createElement('small');current.textContent=job.current;text.append(current)}
      }
      row.append(text);
      if(job.status!=='cancelling'){
        const action=document.createElement('button');action.textContent=busy(job)?'停止':'重试';action.setAttribute('aria-label',action.textContent+'索引 '+job.name);
        action.addEventListener('click',async()=>{action.disabled=true;try{if(busy(job))await api('/api/scan/cancel',{scan_id:job.id});else await api('/api/folders',{folders:[job.folder],recursive:job.recursive});await refreshFolders()}catch(e){Framekeep.notify(e.message);action.disabled=false}});row.append(action);
      }
      return row;
    }
    const existing=new Set();
    for(const item of folderState.sessions.filter(item=>!(folderState.archive_sessions||[]).includes(item.id))){
      existing.add(item.id);
      let cached=folderEntries.get(item.id);
      if(!cached){
        const entry=document.createElement('div');entry.className='folder-entry';entry.dataset.folderSession=item.id;
        const button=document.createElement('button');button.className='folder-row';button.dataset.session=item.id;button.append(document.createElement('span'),document.createElement('span'));button.addEventListener('click',()=>openSession(item.id));
        const parent=document.createElement('div');parent.className='folder-parent';const status=document.createElement('div');entry.append(button,parent,status);
        const shortcut=document.createElement('button');shortcut.className='session-history-item';shortcut.addEventListener('click',()=>{$('folder-dialog').close();openSession(item.id)});
        cached={entry,button,parent,status,shortcut};folderEntries.set(item.id,cached);
      }
      const {entry,button,parent,status,shortcut}=cached;button.dataset.name=item.name;button.title=item.root;button.lastElementChild.textContent=item.capture_count;
      parent.textContent=item.root.replace(/[\\/][^\\/]+[\\/]?$/,'');parent.title=item.root;
      shortcut.textContent=`${item.name} · ${item.capture_count} 张 — ${item.root}`;shortcuts.push(shortcut);
      const job=latest.get(pathKey(item.root));status.replaceChildren();if(job&&job.status!=='completed')status.append(progress(job));entries.push(entry);latest.delete(pathKey(item.root));
    }
    for(const id of folderEntries.keys())if(!existing.has(id))folderEntries.delete(id);
    for(const job of latest.values()){
      const entry=document.createElement('div');entry.className='folder-entry';const name=document.createElement('div');name.className='folder-pending-name';name.textContent='▸ '+job.name;name.title=job.folder;entry.append(name,progress(job));entries.push(entry);
    }
    if(!entries.length){const empty=document.createElement('p');empty.className='folder-empty';empty.textContent='添加文件夹，开始标星选片';entries.push(empty)}
    reconcile(list,entries);reconcile($('session-history'),shortcuts);folderSelection();folderActivity();document.dispatchEvent(new CustomEvent('framekeep:folders'));
  }
  async function refreshFolders(){
    clearTimeout(folderTimer);const request=++folderRequest;
    try{const previous=new Map(folderState.scans.map(job=>[job.id,job.status]));const next=await api('/api/folders');if(request!==folderRequest)return;folderState=next;renderFolders();const ready=folderState.scans.filter(job=>job.status==='completed'&&previous.has(job.id)&&previous.get(job.id)!=='completed');if(ready.length)Framekeep.notify(ready.map(job=>job.name).join('、')+' 索引完成，可在左栏打开')}
    catch(e){$('folder-activity').classList.remove('hidden');$('folder-activity').textContent='文件夹列表暂不可用，正在重新连接';throw e}
    finally{if(request===folderRequest)folderTimer=setTimeout(()=>refreshFolders().catch(()=>{}),folderState.scans.some(busy)?1000:5000)}
  }
  const folderMenu=document.createElement('div');folderMenu.id='folder-context-menu';folderMenu.className='photo-context-menu';folderMenu.setAttribute('role','menu');folderMenu.hidden=true;
  folderMenu.innerHTML='<button role="menuitem" data-folder-action="open">打开路径</button><button role="menuitem" data-folder-action="remove" title="只从软件移除，保留磁盘照片和内部评级记录">从软件移除</button>';document.body.append(folderMenu);
  let menuFolder=null,folderFocus=null;
  function closeFolderMenu(restore=false){if(folderMenu.hidden)return;folderMenu.hidden=true;menuFolder=null;Framekeep.setInteractionPaused(false);if(restore&&folderFocus?.isConnected)folderFocus.focus({preventScroll:true})}
  function showFolderMenu(entry,x,y){
    if(switching)return;const item=folderState.sessions.find(s=>s.id===entry.dataset.folderSession);if(!item)return;
    menuFolder=item;folderFocus=entry.querySelector('.folder-row');folderMenu.setAttribute('aria-label','文件夹操作：'+item.name);folderMenu.hidden=false;
    folderMenu.style.left=Math.max(4,Math.min(x,innerWidth-folderMenu.offsetWidth-4))+'px';folderMenu.style.top=Math.max(4,Math.min(y,innerHeight-folderMenu.offsetHeight-4))+'px';Framekeep.setInteractionPaused(true);folderMenu.querySelector('button').focus();
  }
  $('folder-list').addEventListener('contextmenu',e=>{const entry=e.target.closest('.folder-entry[data-folder-session]');if(!entry)return;e.preventDefault();const r=entry.getBoundingClientRect();showFolderMenu(entry,e.clientX||r.left+12,e.clientY||r.bottom)});
  $('folder-list').addEventListener('keydown',e=>{if(e.key!=='ContextMenu'&&!(e.shiftKey&&e.key==='F10'))return;const entry=e.target.closest('.folder-entry[data-folder-session]');if(!entry)return;e.preventDefault();const r=entry.getBoundingClientRect();showFolderMenu(entry,r.left+12,r.bottom)});
  document.addEventListener('pointerdown',e=>{if(!folderMenu.contains(e.target))closeFolderMenu()},true);
  document.addEventListener('keydown',e=>{
    if(folderMenu.hidden)return;
    if(e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();closeFolderMenu(true)}
    else if(['ArrowDown','ArrowUp','Home','End'].includes(e.key)){e.preventDefault();e.stopImmediatePropagation();const buttons=[...folderMenu.querySelectorAll('button')],i=buttons.indexOf(document.activeElement);buttons[e.key==='Home'?0:e.key==='End'?buttons.length-1:(i+(e.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length].focus()}
    else if(e.key==='Tab')closeFolderMenu();
  },true);
  window.addEventListener('blur',()=>closeFolderMenu());
  folderMenu.addEventListener('click',async e=>{
    const button=e.target.closest('[data-folder-action]');if(!button||!menuFolder||switching)return;const item=menuFolder;closeFolderMenu(true);
    if(button.dataset.folderAction==='open'){try{await api('/api/folders/open',{session_id:item.id})}catch(err){Framekeep.notify(err.message)}return}
    switching=true;++folderRequest;clearTimeout(folderTimer);Framekeep.setSessionLoading(true);document.querySelector('.header-actions').inert=true;
    try{
      await flush();const removedCurrent=session.id===item.id,result=await api('/api/folders/remove',{session_id:item.id});
      if(result.watch)document.dispatchEvent(new CustomEvent('framekeep:watch',{detail:result.watch}));
      if(removedCurrent){
        const data=await api('/api/state'),next=data.session||{id:null,name:'未打开文件夹',root:'',snapshot:'',revision:0,state:{}};
        revision=next.revision;lastSaved=canonical(next.state||{});conflicted=false;$('save-status').textContent=next.id?'已保存':'';
        Framekeep.loadSession({...next,saved:next.state||{},demo:false},data.catalog);sessionDetails();
      }
      await refreshFolders();Framekeep.notify('已从软件移除 '+item.name+'，磁盘照片保留');
    }catch(err){Framekeep.notify('移除失败：'+err.message)}
    finally{switching=false;Framekeep.setSessionLoading(false);document.querySelector('.header-actions').inert=false;folderSelection();folderActivity();schedule();if(!folderTimer)refreshFolders().catch(()=>{})}
  });
  function showAddFolder(){$('scan-status').textContent='索引在后台进行，可继续处理其他文件夹。';$('folder-switcher').open=window.innerWidth<=580;$('folder-dialog').showModal();$('folder-path').focus()}
  for(const id of ['open-folder','add-folder','add-folder-row'])$(id).addEventListener('click',showAddFolder);
  $('choose-folder').addEventListener('click',async()=>{try{$('choose-folder').disabled=true;const folders=window.FramekeepDesktop?await FramekeepDesktop.chooseFolders():[(await api('/api/choose-folder',{},180000)).folder].filter(Boolean);if(folders.length)$('folder-path').value=[$('folder-path').value.trim(),...folders].filter(Boolean).join('\n')}catch(e){$('scan-status').textContent=e.message}finally{$('choose-folder').disabled=false}});
  $('scan-folder').addEventListener('click',async()=>{
    try{
      const folders=[...new Set($('folder-path').value.split(/\r?\n/).map(s=>s.trim().replace(/^"|"$/g,'')).filter(Boolean))];
      if(!folders.length)throw new Error('请填写文件夹路径，或点击“浏览”选择。');
      $('scan-folder').disabled=true;await flush();
      const existing=folders.map(path=>folderState.sessions.find(item=>pathKey(item.root)===pathKey(path)));
      const added=folders.filter((_,i)=>!existing[i]);
      if(added.length)await api('/api/folders',{folders:added,recursive:$('recursive').checked});
      $('folder-dialog').close();$('folder-path').value='';
      if(!added.length&&folders.length===1)await openSession(existing[0].id);
      else{await refreshFolders();Framekeep.notify(added.length?'已添加到左栏，索引完成后点击文件夹开始选片':'这些文件夹已在左栏，可直接切换')}
    }catch(e){$('scan-status').textContent=e.message}finally{$('scan-folder').disabled=false}
  });
  async function bridgeStatus(){const status=await api('/api/bridge/status');let text=status.connected?'LR 桥接已连接':status.last_seen?'LR 最近联系：'+new Date(status.last_seen).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})+'（等待恢复或 LR 对话框操作）':'尚未连接 LR 桥接插件';if(status.latest_job){const job=status.latest_job;text+='\n最近同步：'+({pending:'等待 LR 预览确认',applied:'已写入星级',failed:'失败',cancelled:'已取消',superseded:'已被后续任务替代'}[job.status]||job.status);if(job.result)text+='\n'+JSON.stringify(job.result)}if(status.diagnostic?.directories?.length)text+='\nLR 实际对应目录：\n'+status.diagnostic.directories.map(d=>`${d.path}（${d.count} 张，当前清单路径匹配 ${d.matchedPaths}）`).join('\n');$('bridge-status').textContent=text;return status}
  $('send-lr').addEventListener('click',()=>{$('bridge-dialog').showModal();bridgeStatus().catch(e=>Framekeep.notify(e.message));clearInterval(bridgeTimer);bridgeTimer=setInterval(()=>{if($('bridge-dialog').open)bridgeStatus().catch(()=>{});else clearInterval(bridgeTimer)},2000)});
  $('queue-bridge').addEventListener('click',async()=>{try{await flush();await api('/api/bridge',{});await bridgeStatus();Framekeep.notify('已发送，等待 LR 中预览确认')}catch(e){Framekeep.notify(e.message)}});
  $('install-bridge').addEventListener('click',async()=>{try{const result=await api('/api/bridge/install',{});$('bridge-status').textContent=`已安装到 ${result.path}\n请在 LR 增效工具管理器添加/重新加载插件，再运行“连接末帧工作台”。`}catch(e){Framekeep.notify(e.message)}});
  refreshFolders().catch(()=>{});
  sessionDetails();
  schedule();
})();
