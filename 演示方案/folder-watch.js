(() => {
  'use strict';
  if(!window.FramekeepIO)return;
  const $=id=>document.getElementById(id),api=FramekeepIO.api;
  const button=document.createElement('button');button.id='folder-watch';button.className='bordered';button.textContent='现场监控';$('open-folder').after(button);
  const dialog=document.createElement('dialog');dialog.id='watch-dialog';dialog.setAttribute('aria-labelledby','watch-title');
  dialog.innerHTML='<div class="dialog-heading"><h2 id="watch-title">监控照片传入文件夹</h2><button id="watch-close" aria-label="关闭">×</button></div><p class="dialog-copy">选择相机联机、无线传图或其他软件保存照片的文件夹。已有照片会载入并可回看，新照片写完后按顺序接续显示。选择目录不会清空、移动或改写原照片；开启“鼠标遥控”后，按左键即可加星。</p><label class="mouse-device-label" for="watch-path">照片传入文件夹</label><div class="watch-path-row"><input id="watch-path" type="text" placeholder="填写文件夹路径，可为空目录"><button id="watch-choose" class="bordered">浏览</button></div><label class="watch-option"><input id="watch-recursive" type="checkbox" checked> 包含子文件夹</label><label class="watch-option"><input id="watch-follow" type="checkbox" checked> 自动显示最新传入照片</label><p class="dialog-copy">新照片从 0 星等待标记，原文件已有星级会保留；已标星的照片不因新照片进入而改变。RAW/JPG 先后传入会合并。可关闭自动跟随，继续浏览和标记之前的照片。</p><p id="watch-status" class="dialog-copy" role="status"></p><div class="dialog-actions"><button id="watch-stop">停止监控</button><button id="watch-start" class="primary">开始监控</button></div>';
  document.body.append(dialog);
  const tetherButton=document.createElement('button');tetherButton.id='canon-tether-open';tetherButton.className='bordered';tetherButton.textContent='联机拍摄';button.after(tetherButton);
  const tether=document.createElement('dialog');tether.id='canon-save-dialog';tether.setAttribute('aria-labelledby','canon-save-title');
  tether.innerHTML='<div class="dialog-heading"><h2 id="canon-save-title">联机拍摄 · 保存目录</h2><button id="canon-save-close" aria-label="关闭保存目录">×</button></div><p class="dialog-copy">选择本次联机拍摄的保存根目录，照片按拍摄日期自动放入 YYYY-MM-DD 子文件夹。</p><label class="mouse-device-label" for="canon-save-path">保存根目录</label><div class="watch-path-row"><input id="canon-save-path" type="text" autocomplete="off" spellcheck="false" placeholder="D:\\拍摄、Z:\\拍摄 或 \\\\192.168.1.10\\共享\\拍摄"><button id="canon-save-choose" class="bordered">浏览</button></div><p class="dialog-copy">支持本地文件夹、已连接的网络驱动器和 IP／主机名共享路径，需具备写入权限。</p><div class="canon-save-preview"><span id="canon-save-location"></span><strong>保留相机原文件名</strong><p id="canon-save-preview"></p><small>日期目录示例；实际按照片拍摄日期创建。</small></div><p id="canon-save-status" role="status" aria-live="polite" class="dialog-copy"></p><div class="dialog-actions"><button id="canon-save-cancel">取消</button><button id="canon-save-apply" class="primary">应用并进入联机</button></div>';
  document.body.append(tether);let canonSaveState=null,canonSaveBusy=false,canonPathEdit=0,canonDialogSequence=0;
  function savePreview(){
    const path=$('canon-save-path').value.trim().replace(/^"|"$/g,'').replace(/[\\/]+$/,'');
    $('canon-save-location').textContent=path?FramekeepPaths.driveOf(path):'请选择保存位置';
    $('canon-save-preview').textContent=path?path+'\\'+(canonSaveState?.date_folder||new Date().toLocaleDateString('sv-SE'))+'\\IMG_0001.JPG':'文件夹：YYYY-MM-DD　·　文件名：IMG_0001.JPG';
  }
  async function openTether(state){
    if(tether.open)return;if(dialog.open)dialog.close();tether.showModal();$('canon-save-status').textContent='正在读取保存设置…';const sequence=++canonDialogSequence,edit=canonPathEdit;
    try{const data=state||await api('/api/canon');if(!tether.open||sequence!==canonDialogSequence)return;canonSaveState=data;if(edit===canonPathEdit)$('canon-save-path').value=data.destination||data.save_folder||'';savePreview();$('canon-save-status').textContent=data.running?'EOS Utility 正在运行。确认应用后，新目录和命名规则将在关闭 EOS 后生效。':'确认应用后才启用本次联机并监控保存目录；EOS 若已运行，需要关闭后重新打开以应用设置。';if(edit===canonPathEdit)$('canon-save-path').focus()}
    catch(error){if(tether.open&&sequence===canonDialogSequence)$('canon-save-status').textContent=error.message}
  }
  tetherButton.onclick=()=>openTether();
  for(const id of ['canon-save-close','canon-save-cancel'])$(id).onclick=()=>{if(!canonSaveBusy)tether.close()};
  tether.addEventListener('cancel',event=>{if(canonSaveBusy)event.preventDefault()});
  $('canon-save-path').addEventListener('input',()=>{canonPathEdit++;savePreview()});
  $('canon-save-choose').onclick=async()=>{
    $('canon-save-choose').disabled=true;
    try{const paths=window.FramekeepDesktop?await FramekeepDesktop.chooseFolders():[(await api('/api/choose-folder',{},180000)).folder];if(paths[0]){$('canon-save-path').value=paths[0];canonPathEdit++;savePreview()}}
    catch(error){$('canon-save-status').textContent=error.message}finally{$('canon-save-choose').disabled=false}
  };
  $('canon-save-apply').onclick=async()=>{
    if(canonSaveBusy)return;canonSaveBusy=true;for(const id of ['canon-save-path','canon-save-choose','canon-save-apply','canon-save-cancel','canon-save-close'])$(id).disabled=true;$('canon-save-status').textContent='正在检查保存目录并应用联机设置…';
    try{
      await FramekeepIO.flush();const state=await api('/api/canon/destination',{folder:$('canon-save-path').value},90000);canonSaveState=state;
      if(state.destination_pending){$('canon-save-status').textContent=state.message;return}
      tether.close();await showCanon();await FramekeepIO.refreshFolders();const watch=await api('/api/watch');
      if(watch.enabled)await FramekeepIO.refreshLive(watch.session_id,null,true);wakePoll();Framekeep.notify('联机保存目录已应用，保留原文件名，按 YYYY-MM-DD 日期分文件夹');
    }catch(error){$('canon-save-status').textContent=error.message}
    finally{canonSaveBusy=false;for(const id of ['canon-save-path','canon-save-choose','canon-save-apply','canon-save-cancel','canon-save-close'])$(id).disabled=false}
  };
  $('canon-save-path').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();$('canon-save-apply').click()}});
  const canon=document.createElement('section');canon.className='watch-plan';canon.id='canon-link-panel';
  canon.innerHTML='<label class="watch-option"><input id="canon-link-enabled" type="checkbox"> Canon EOS 联机接入</label><p id="canon-link-status" class="dialog-copy" role="status" aria-live="polite">正在检测 EOS Utility…</p><p class="dialog-copy">在 EOS Utility 的「链接软件 → 记录…」中选择本软件后，自动跟随 EOS 的保存目录及新建拍摄子文件夹，无需重复添加。也可手动设置新的保存目录。</p><div class="dialog-actions"><button id="canon-link-resume" class="bordered">跟随 EOS 保存目录</button><button id="canon-link-save" class="bordered">设置联机保存目录</button></div>';
  dialog.querySelector('.dialog-heading').after(canon);
  let canonCheckedAt=0,autoGeneration=null,lastCanonEvent=null,canonRequest=0,canonChanging=false;
  async function showCanon(){const sequence=++canonRequest;if(dialog.open)$('canon-link-enabled').disabled=true;try{const state=await api('/api/canon');if(sequence!==canonRequest||canonChanging)return;const folder=state.follow_eos?state.save_folder:state.destination||state.save_folder;$('canon-link-enabled').checked=!!state.enabled;$('canon-link-status').textContent=state.message+(state.error?'：'+state.error:'')+(folder?' · '+folder:'');tetherButton.textContent=state.enabled&&state.follow_eos?'联机拍摄 · 自动跟随':'联机拍摄';tetherButton.title=state.message+(folder?' · '+folder:'');
    if(tether.open&&canonSaveState?.destination_pending&&state.destination_ready){canonSaveState=state;$('canon-save-status').textContent='保存设置已生效，可重新打开 EOS Utility 开始联机拍摄。'}
  }catch(e){if(sequence===canonRequest)$('canon-link-status').textContent=e.message}finally{if(sequence===canonRequest&&!canonChanging)$('canon-link-enabled').disabled=false}}
  $('canon-link-save').onclick=()=>openTether();
  async function setCanon(enabled){if(canonChanging)return;canonChanging=true;canonRequest++;$('canon-link-enabled').disabled=true;try{await api('/api/canon',{enabled,follow_eos:true});canonCheckedAt=0;autoGeneration=null}catch(e){$('canon-link-status').textContent=e.message}finally{canonChanging=false;await showCanon();wakePoll()}}
  $('canon-link-enabled').addEventListener('change',()=>setCanon($('canon-link-enabled').checked));
  $('canon-link-resume').addEventListener('click',()=>setCanon(true));
  const plan=document.createElement('section');plan.id='watch-plan-dropzone';plan.className='watch-plan';plan.setAttribute('aria-labelledby','watch-plan-title');plan.setAttribute('aria-describedby','watch-plan-drop-hint');
  plan.innerHTML='<div class="watch-plan-heading"><h3 id="watch-plan-title">拍摄计划文档 <small>可选</small></h3><button id="watch-plan-import" class="bordered">导入拍摄计划文档</button><input id="watch-plan-file" type="file" accept=".xlsx" hidden></div><p id="watch-plan-name">尚未导入拍摄计划</p><p id="watch-plan-summary" class="dialog-copy">导入包含款号的 .xlsx 表格，自动读取款号和各工作表的拍摄类型。</p><label class="mouse-device-label" for="watch-plan-scope">本次拍摄类型／工作表</label><select id="watch-plan-scope" aria-describedby="watch-plan-hint" disabled><option value="">导入后可选</option></select><p id="watch-plan-hint" class="dialog-copy">开始监控时，将拍摄计划绑定到上方照片传入文件夹。</p><p id="watch-plan-message" class="dialog-copy" role="status" aria-live="polite"></p>';
  $('watch-status').before(plan);
  $('watch-plan-name').insertAdjacentHTML('beforebegin','<p id="watch-plan-drop-hint" class="watch-plan-drop-hint">拖拽 .xlsx 拍摄计划到此区域，即可自动读取</p>');
  let planProject=null,importing=false;
  function renderPlan(){
    const source=window.FramekeepSku?.shootingTypes(),select=$('watch-plan-scope'),previous=select.value,names=source?.sheets||[];
    select.replaceChildren(new Option(names.length?'请选择本次拍摄类型':'导入后可选',''),...names.map(name=>new Option(name,name)));
    select.value=source?.scope||(source?.project_id===planProject&&names.includes(previous)?previous:names.length===1?names[0]:'');select.disabled=!names.length;
    $('watch-plan-name').textContent=source?.name||'尚未导入拍摄计划';$('watch-plan-name').title=source?.name||'';
    $('watch-plan-summary').textContent=source?`已读取 ${names.length} 个工作表 · ${(source.counts||[]).reduce((sum,s)=>sum+s.total,0)} 项款号。`:'导入包含款号的 .xlsx 表格，自动读取款号和各工作表的拍摄类型。';
    if(source?.project_id!==planProject)$('watch-plan-message').textContent='';planProject=source?.project_id||null;
  }
  document.addEventListener('framekeep:sku-project',renderPlan);
  $('watch-plan-scope').addEventListener('change',()=>{$('watch-plan-message').textContent=''});
  $('watch-plan-import').addEventListener('click',()=>$('watch-plan-file').click());
  async function importPlanFile(file){
    if(!file||importing||$('watch-start').disabled)return;
    importing=true;plan.setAttribute('aria-busy','true');$('watch-plan-import').disabled=true;$('watch-start').disabled=true;$('watch-plan-message').textContent='正在读取拍摄计划…';
    try{
      if(!window.FramekeepSku?.importPlan)throw new Error('拍摄计划功能尚未就绪，请稍后重试');
      await FramekeepSku.importPlan(file);renderPlan();$('watch-plan-message').textContent='已自动读取款号，请选择本次拍摄类型后开始监控。';
    }catch(error){$('watch-plan-message').textContent='导入失败：'+error.message}
    finally{importing=false;plan.removeAttribute('aria-busy');$('watch-plan-import').disabled=false;$('watch-start').disabled=false}
  }
  $('watch-plan-file').addEventListener('change',async e=>{try{await importPlanFile(e.target.files[0])}finally{e.target.value=''}});
  let dragDepth=0;
  const fileDrag=e=>Array.from(e.dataTransfer?.types||[]).includes('Files');
  function clearDrag(){dragDepth=0;plan.classList.remove('drag-over')}
  // Windows桌面启用HTML5拖放后，同时阻止文件在区域外落下时触发页面导航。
  document.addEventListener('dragover',e=>{if(fileDrag(e)){e.preventDefault();e.dataTransfer.dropEffect=dialog.open&&plan.contains(e.target)&&!$('watch-start').disabled?'copy':'none'}},true);
  document.addEventListener('drop',e=>{if(fileDrag(e)){e.preventDefault();if(!plan.contains(e.target))clearDrag()}},true);
  plan.addEventListener('dragenter',e=>{if(fileDrag(e)){e.preventDefault();dragDepth++;if(!$('watch-start').disabled)plan.classList.add('drag-over')}});
  plan.addEventListener('dragleave',e=>{if(fileDrag(e)&&--dragDepth<=0)clearDrag()});
  plan.addEventListener('drop',e=>{
    if(!fileDrag(e))return;e.preventDefault();clearDrag();
    if(importing||$('watch-start').disabled)return;
    const files=Array.from(e.dataTransfer.files);
    if(files.length!==1){$('watch-plan-message').textContent='每次请只拖入一个 .xlsx 拍摄计划文档';return}
    importPlanFile(files[0]);
  });
  document.addEventListener('dragend',clearDrag);window.addEventListener('blur',clearDrag);dialog.addEventListener('close',clearDrag);
  const badge=document.createElement('div');badge.id='watch-live-status';badge.className='watch-live-status hidden';badge.setAttribute('role','status');$('folder-activity').after(badge);
  let follow=true,lastGeneration=null,lastVersion=-1,lastArrivals=-1,lastSession=null,busy=false,timer=null,lastEvent=null,watchEnabled=false,controller=null,pollSession=null;
  let watchFormEdit=0,watchDialogSequence=0;
  $('watch-path').addEventListener('input',()=>watchFormEdit++);
  $('watch-recursive').addEventListener('change',()=>watchFormEdit++);
  try{follow=localStorage.getItem('framekeep-watch-follow')!=='false'}catch{}
  $('watch-follow').checked=follow;
  window.FramekeepWatch={setFollow(value){$('watch-follow').checked=!!value;$('watch-follow').dispatchEvent(new Event('change'))},getFollow:()=>follow};
  function show(state){
    document.dispatchEvent(new CustomEvent('framekeep:watch',{detail:{...state,follow}}));
    button.textContent=state.enabled?'现场监控 · 已开启':'现场监控';button.classList.toggle('active',!!state.enabled);button.setAttribute('aria-pressed',String(!!state.enabled));
    badge.classList.toggle('hidden',!state.enabled);badge.textContent=state.enabled?(state.error?`${state.message}：${state.error}`:state.message):'';badge.title=state.folder||'';
    if(dialog.open){$('watch-status').textContent=state.error?`${state.message}：${state.error}`:state.message;$('watch-stop').disabled=!state.enabled}
  }
  async function poll(){
    if(busy)return;busy=true;
    controller=new AbortController();pollSession=window.FRAMEKEEP_SESSION.id;
    try{
      const state=await api('/api/watch'+(watchEnabled&&lastEvent!==null?'?after='+lastEvent+'&wait=15':''),undefined,20000,controller.signal);
      lastEvent=state.event_id??null;watchEnabled=!!state.enabled;window.FramekeepWatch.initializing=!!state.initializing&&state.session_id===window.FRAMEKEEP_SESSION.id;show(state);
      if(Date.now()-canonCheckedAt>3000||state.enabled&&state.source==='canon'&&state.canon_event&&state.canon_event!==lastCanonEvent){canonCheckedAt=Date.now();showCanon()}
      if(state.enabled&&state.source==='canon'&&state.canon_event&&state.canon_event!==lastCanonEvent&&!document.querySelector('dialog[open]')&&!Framekeep.remoteTarget().paused){
        await FramekeepIO.flush();
        if(window.FramekeepWorkspace&&FramekeepWorkspace.current()!=='selection')FramekeepWorkspace.show('selection');
        if(window.FRAMEKEEP_SESSION.id!==state.session_id)autoGeneration=null;
        lastCanonEvent=state.canon_event;
      }
      if(state.enabled&&state.source==='canon'&&autoGeneration!==state.generation&&!document.querySelector('dialog[open]')&&!Framekeep.remoteTarget().paused&&(!window.FramekeepWorkspace||FramekeepWorkspace.current()==='selection')){
        await FramekeepIO.flush();await FramekeepIO.refreshFolders();
        if(await FramekeepIO.refreshLive(state.session_id,null,true))autoGeneration=state.generation;
      }
      const current=window.FRAMEKEEP_SESSION.id;
      if(state.enabled&&current===state.session_id&&!document.querySelector('dialog[open]')&&!Framekeep.remoteTarget().paused){
        const changed=lastGeneration!==state.generation||lastVersion!==state.version||lastSession!==current;
        if(changed){
          const target=follow&&state.arrivals!==lastArrivals?state.last_photo_id:null;
          if(await FramekeepIO.refreshLive(current,target,false,lastGeneration===state.generation&&lastSession===current&&lastVersion>=0?{after:lastVersion,generation:state.generation}:null)){
            lastGeneration=state.generation;lastVersion=state.version;lastArrivals=state.arrivals;lastSession=current;
            FramekeepIO.refreshFolders().catch(()=>{});
          }
        }
      }
    }catch(e){if(e.name!=='AbortError'){badge.classList.remove('hidden');badge.textContent='监控连接中断，正在重试：'+e.message;watchEnabled=false}}
    finally{busy=false;clearTimeout(timer);timer=setTimeout(poll,watchEnabled?25:600)}
  }
  function wakePoll(){lastEvent=null;if(busy)controller?.abort();else{clearTimeout(timer);timer=setTimeout(poll,0)}}
  dialog.addEventListener('close',wakePoll);
  let wasPaused=Framekeep.remoteTarget().paused;
  document.addEventListener('framekeep:selection',()=>{const paused=Framekeep.remoteTarget().paused;if(pollSession!==window.FRAMEKEEP_SESSION.id||wasPaused&&!paused)wakePoll();wasPaused=paused});
  button.addEventListener('click',async()=>{
    renderPlan();dialog.showModal();showCanon();$('watch-path').value=window.FRAMEKEEP_SESSION.root||'';
    const sequence=++watchDialogSequence,edit=watchFormEdit;
    try{const state=await api('/api/watch');if(!dialog.open||sequence!==watchDialogSequence)return;if(edit===watchFormEdit){if(state.folder)$('watch-path').value=state.folder;$('watch-recursive').checked=state.recursive!==false}show(state)}catch(e){if(dialog.open&&sequence===watchDialogSequence)$('watch-status').textContent=e.message}
  });
  $('watch-close').addEventListener('click',()=>dialog.close());
  $('watch-follow').addEventListener('change',()=>{follow=$('watch-follow').checked;try{localStorage.setItem('framekeep-watch-follow',String(follow))}catch{};lastArrivals=-1;lastVersion=-1;wakePoll()});
  $('watch-choose').addEventListener('click',async()=>{
    $('watch-choose').disabled=true;
    try{const paths=window.FramekeepDesktop?await FramekeepDesktop.chooseFolders():[(await api('/api/choose-folder',{},180000)).folder];if(paths[0]){$('watch-path').value=paths[0];watchFormEdit++}}
    catch(e){$('watch-status').textContent=e.message}finally{$('watch-choose').disabled=false}
  });
  $('watch-start').addEventListener('click',async()=>{
    $('watch-start').disabled=true;$('watch-plan-import').disabled=true;
    let started=false;
    try{
      const source=window.FramekeepSku?.shootingTypes(),scope=$('watch-plan-scope').value;
      if(source&&!scope){$('watch-plan-message').textContent='请选择本次拍摄类型／工作表';return}
      await FramekeepIO.flush();
      const state=await api('/api/watch/start',{folder:$('watch-path').value,recursive:$('watch-recursive').checked});
      started=true;
      dialog.close();await FramekeepIO.refreshFolders();
      if(!await FramekeepIO.refreshLive(state.session_id,null,true))throw new Error('监控已开启，请在左栏打开该文件夹。');
      if(source)await FramekeepSku.bindCurrent(scope,source.project_id);
      lastGeneration=null;lastVersion=-1;lastSession=null;show(state);Framekeep.notify('现场监控已开启，新照片写完后自动显示；请开启鼠标遥控');
    }catch(e){const text=started?'监控已开启，但目录切换或拍摄计划绑定未完成：'+e.message:e.message;if(dialog.open)$('watch-status').textContent=text;else Framekeep.notify(text)}
    finally{$('watch-start').disabled=false;$('watch-plan-import').disabled=false}
  });
  $('watch-stop').addEventListener('click',async()=>{try{show(await api('/api/watch/stop',{}));Framekeep.notify('监控已停止，照片和评级已保留')}catch(e){$('watch-status').textContent=e.message}});
  poll();
})();
