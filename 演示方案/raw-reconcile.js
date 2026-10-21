(() => {
  'use strict';
  if(!window.FramekeepIO)return;
  const $=id=>document.getElementById(id),api=FramekeepIO.api,esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const section=document.createElement('details');section.id='archive-sync-panel';section.className='side-section';section.open=true;section.innerHTML='<summary>归档同步</summary><div class="side-body"><div class="asset-actions"><button id="raw-sync-browse">选择最终拷贝路径</button><button id="raw-sync-start" class="primary" disabled>写入元数据</button></div><p id="raw-sync-path-display" class="dialog-copy" hidden></p><p id="raw-sync-summary" role="status" hidden></p><a id="raw-sync-open" href="#" hidden>查看结果</a></div>';document.querySelector('.right-panel').append(section);
  const dialog=document.createElement('dialog');dialog.id='raw-sync-dialog';dialog.innerHTML='<div class="dialog-heading"><h2>归档同步结果</h2><button id="raw-sync-close">×</button></div><p class="dialog-copy">使用已保存的拍摄信息、人工星级和 SKU 匹配，无需保留现场 JPG。支持 JPG、CR3、ARW（含子文件夹），有歧义或已有冲突的照片在此核对。</p><p id="raw-sync-message" role="status"></p><div class="sku-toolbar"><select id="raw-sync-filter"><option value="">全部</option><option value="review">待确认</option><option value="unmatched">无匹配</option><option value="failed">失败</option><option value="applied">已写入</option></select><button id="raw-sync-prev">上一页</button><button id="raw-sync-next">下一页</button><span id="raw-sync-page"></span></div><div id="raw-sync-jobs" class="sku-table-wrap"></div>';document.body.append(dialog);
  let state={pairs:[],version:0},busy=false,lastVersion=null,lastSession=null,signature='',dirty=false,offset=0;
  let targetId='';try{targetId=localStorage.getItem('framekeep-archive-target')||''}catch{}
  const selected=()=>state.targets?.find(t=>t.id===targetId),sessionId=()=>selected()?.session||FRAMEKEEP_SESSION.id;
  const labels={queued:'等待写入',applied:'已同步',review:'待确认',unmatched:'未找到现场记录',waiting_source:'等待现场评级/SKU',failed:'写入失败',unsupported:'格式未开放',cancelled:'旧任务已取消',retrying:'重新核对中'};
  async function action(fn){if(busy)return;busy=true;try{await FramekeepIO.flush();await fn()}catch(e){$('raw-sync-message').textContent=e.message;Framekeep.notify(e.message)}finally{busy=false}}
  function render(){
    const targets=state.targets||[];if(!targets.some(t=>t.id===targetId))targetId=targets[0]?.id||'';
    const target=selected();$('raw-sync-path-display').textContent=target?.folder||'';$('raw-sync-path-display').title=target?.folder||'';$('raw-sync-path-display').hidden=!target;
    const running=target&&['indexing','matching','verifying'].includes(target.status);$('raw-sync-start').disabled=!target||running;$('raw-sync-start').textContent=running?'写入中…':'写入元数据';
    const counts={};for(const [status,n] of Object.entries(state.counts||{}))counts[labels[status]||status]=n;
    const work=state.work?.find(w=>w.session===sessionId());
    const summary=Object.entries(counts).map(([k,n])=>`${k} ${n}`).join(' / ');
    $('raw-sync-summary').textContent=target?.status==='indexing'?`扫描中 ${target.progress?.done||0} / ${target.progress?.total||0}`:target?.status==='matching'?'写入中… '+summary:target?.status==='failed'?'写入失败 · '+target.reason:target?summary:'';
    if(target&&work?.reason&&target.status!=='failed')$('raw-sync-summary').textContent+=' · '+work.reason;
    $('raw-sync-summary').hidden=!$('raw-sync-summary').textContent;$('raw-sync-open').hidden=!target||!Object.keys(state.counts||{}).some(k=>!['applied','queued'].includes(k));
    if(!dialog.open)return;$('raw-sync-page').textContent=`${offset+1}–${offset+state.pairs.length} / ${state.total||0}`;$('raw-sync-prev').disabled=offset===0;$('raw-sync-next').disabled=offset+state.pairs.length>=(state.total||0);
    const next=JSON.stringify(state.pairs);if(next===signature||document.activeElement?.matches('[data-raw-source],[data-raw-overwrite]'))return;signature=next;
    $('raw-sync-jobs').innerHTML=state.pairs.length?state.pairs.map(p=>`<article class="raw-pair" data-raw-pair="${p.id}"><strong>${esc(p.filename)} · ${esc(labels[p.status]||p.status)}</strong><p>${esc(p.reason)}</p>${p.source_capture?`<p>来源 JPG：${esc(p.source_filename||p.source_capture)} · ${p.source_rating??'未标记'} 星</p>`:''}${['review','failed','unmatched'].includes(p.status)?`<select data-raw-source aria-label="确认对应 JPG"><option value="">选择可靠对应的 JPG</option>${p.candidates.map(c=>`<option value="${esc(c.session)}|${esc(c.capture)}">${esc(c.filename||c.capture)} · ${Object.entries(c.evidence).filter(([k,v])=>v===true).map(([k])=>esc(({serial_equal:'相机序列号一致',subsecond_equal:'拍摄子秒一致',filename_equal:'原始文件名一致',number_equal:'相机编号一致',unique_id_equal:'图像标识一致'})[k]||k)).join('、')}</option>`).join('')}</select><label class="watch-option"><input type="checkbox" data-raw-overwrite>覆盖归档文件原有星级/SKU（目录人工评级仍需先核对）</label><button data-raw-review>确认来源并同步</button><button data-raw-retry>重试</button>`:''}</article>`).join(''):'<p>同步后在此查看匹配和写入结果。</p>';
  }
  async function load(){const before=targetId,sid=sessionId();const params={status:$('raw-sync-filter').value,offset};if(sid)params.session_id=sid;const data=await api('/api/raw-sync?'+new URLSearchParams(params));if(before!==targetId||sid!==sessionId())return;state=data;render();if(sid!==sessionId()){signature='';return load()}
    if((lastSession===sid&&lastVersion!==null&&lastVersion!==data.version)||(lastSession!==sid&&data.pairs.some(p=>p.status==='applied')))dirty=true;if(dirty&&sid===FRAMEKEEP_SESSION.id&&!document.querySelector('dialog[open]')){await FramekeepIO.refreshLive(sid);dirty=false}
    lastSession=sid;lastVersion=data.version;
  }
  $('raw-sync-filter').onchange=()=>action(async()=>{offset=0;signature='';await load()});$('raw-sync-prev').onclick=()=>action(async()=>{offset=Math.max(0,offset-100);signature='';await load()});$('raw-sync-next').onclick=()=>action(async()=>{offset+=100;signature='';await load()});
  $('raw-sync-open').onclick=e=>{e.preventDefault();action(async()=>{dialog.showModal();signature='';await load()})};$('raw-sync-close').onclick=()=>dialog.close();
  window.FramekeepRawSync={review(id){action(async()=>{state=await api('/api/raw-sync');targetId=id;offset=0;signature='';dialog.showModal();await load()})}};
  async function add(folder){const data=await api('/api/raw-sync/targets',{folder});state.targets=data.targets;targetId=data.target_id;offset=0;signature='';render();await load();try{localStorage.setItem('framekeep-archive-target',targetId)}catch{}}
  $('raw-sync-browse').onclick=()=>action(async()=>{const folder=window.FramekeepDesktop?(await FramekeepDesktop.chooseFolders())?.[0]:window.prompt('最终拷贝路径',selected()?.folder||'');if(folder?.trim())await add(folder)});
  $('raw-sync-start').onclick=()=>action(async()=>{if(!targetId)throw new Error('请先添加最终归档文件夹');state=await api('/api/raw-sync/sync-target',{target_id:targetId});offset=0;signature='';render();window.FramekeepMetadataTasks?.submitted(targetId);await FramekeepIO.refreshFolders()});
  $('raw-sync-jobs').onclick=e=>{const b=e.target.closest('button'),card=b?.closest('[data-raw-pair]');if(!card)return;action(async()=>{
    if(b.matches('[data-raw-review]')){const value=card.querySelector('[data-raw-source]').value;if(!value)throw new Error('请先选择对应 JPG');const [source_session,source_capture]=value.split('|');state=await api('/api/raw-sync/review',{pair_id:card.dataset.rawPair,source_session,source_capture,overwrite:card.querySelector('[data-raw-overwrite]').checked})}
    else state=await api('/api/raw-sync/retry',{pair_id:card.dataset.rawPair});signature='';render();
  })};
  async function poll(){if(!busy)try{await load()}catch{}setTimeout(poll,1300)}poll();
})();
