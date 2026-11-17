(() => {
  'use strict';
  const browser=window.FramekeepArchiveBrowser;if(!browser)return;
  const $=id=>document.getElementById(id),api=FramekeepIO.api;
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const host=$('asset-archive-dialog'),grid=$('catalog-grid');
  const bar=document.createElement('div');bar.className='explorer-bar';
  bar.innerHTML='<nav aria-label="文件夹导航"><button id="explorer-back" title="后退 (Alt+左)">←</button><button id="explorer-forward" title="前进 (Alt+右)">→</button><button id="explorer-up" title="上一级 (Alt+上)">↑</button><button id="explorer-refresh" title="刷新 (F5)">↻</button><form id="explorer-address-form"><input id="explorer-address" aria-label="当前文件夹路径" placeholder="选择位置或输入已添加位置内的路径后回车"></form><button id="explorer-system">系统打开</button></nav><div class="explorer-tools"><button data-file-action="mkdir">新建文件夹</button><button data-file-action="rename">重命名</button><button data-file-action="copy">复制</button><button data-file-action="cut">剪切</button><button data-file-action="paste">粘贴</button><button data-file-action="reveal">定位文件</button><span id="explorer-selected" role="status">单击选择，双击打开 · Ctrl 多选</span></div><p id="explorer-error" role="status" hidden></p>';
  host.querySelector('.catalog-main').prepend(bar);
  bar.hidden=true;
  const view=document.createElement('button');view.id='explorer-view';view.textContent='详细列表';view.type='button';bar.querySelector('.explorer-tools').append(view);
  const toolbar=host.querySelector('.catalog-main>.catalog-toolbar');toolbar.classList.add('explorer-catalog-toolbar');
  const inlineActions=document.createElement('div');inlineActions.className='explorer-inline-actions';toolbar.append(inlineActions);
  const advanced=document.createElement('details');advanced.className='explorer-advanced';advanced.innerHTML='<summary>更多素材工具</summary><div></div>';toolbar.append(advanced);
  const style=document.createElement('style');style.textContent=`
    #asset-archive-dialog .catalog-scan-panel{display:none!important}
    .explorer-bar{padding:8px 12px;border-bottom:1px solid var(--theme-border);background:var(--theme-panel)}
    .explorer-bar nav,.explorer-tools{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.explorer-tools{margin-top:7px}
    .explorer-catalog-toolbar{flex-wrap:nowrap!important;overflow-x:auto;gap:8px;white-space:nowrap}.explorer-catalog-toolbar>*,.explorer-catalog-toolbar label{flex-shrink:0}
    .explorer-inline-actions{display:flex;align-items:center;gap:6px}
    .explorer-advanced{margin:0;font-size:11px;color:var(--theme-muted);flex-shrink:0}.explorer-advanced[open]{display:flex;align-items:center;gap:8px}.explorer-advanced>summary{cursor:pointer;white-space:nowrap}.explorer-advanced>div{display:flex;align-items:center;gap:6px;flex-wrap:nowrap}.explorer-advanced:not([open])>div{display:none}.explorer-advanced .capture-batch-tools{width:auto;padding:0;border:0;margin:0;flex-wrap:nowrap}
    .explorer-bar button{padding:5px 9px;border:1px solid var(--theme-border);border-radius:4px;color:var(--theme-text);background:var(--theme-control);font:inherit;font-size:12px;cursor:pointer}.explorer-bar button:disabled{opacity:.4;cursor:default}
    #explorer-address-form{flex:1;min-width:140px}#explorer-address{box-sizing:border-box;width:100%;padding:6px 8px;border:1px solid var(--theme-border);background:var(--theme-control);color:var(--theme-text);border-radius:4px;font:inherit;font-size:12px}
    #explorer-selected{font-size:11px;color:var(--theme-muted);overflow-wrap:anywhere}#explorer-error{margin:8px 0 0;color:var(--theme-text);overflow-wrap:anywhere}
    .explorer-entries{grid-column:1/-1;display:grid;grid-template-columns:repeat(auto-fill,minmax(145px,1fr));gap:8px;align-content:start}
    .explorer-entry{display:flex;align-items:center;gap:9px;min-width:0;padding:14px 10px;text-align:left;border:1px solid var(--theme-border);border-radius:5px;color:var(--theme-text);background:var(--theme-panel);cursor:pointer}
    .explorer-entry>span{font-size:28px;color:var(--theme-accent)}.explorer-entry div{min-width:0}.explorer-entry strong{display:block;font-size:12px;overflow-wrap:anywhere}.explorer-entry small{display:block;font-size:10px;color:var(--theme-muted);margin-top:5px}
    #catalog-grid .explorer-picked{outline:2px solid var(--theme-selected-edge,var(--theme-accent));outline-offset:-2px;background:var(--theme-selected)}
    #catalog-grid.explorer-details>.catalog-card,#catalog-grid.explorer-details>.catalog-folder-group{display:none}
    .explorer-details .explorer-entries{display:flex;flex-direction:column;gap:1px}.explorer-details .explorer-entry{display:grid;grid-template-columns:28px minmax(100px,1fr);padding:8px 12px}.explorer-details .explorer-entry>span{font-size:18px}.explorer-details .explorer-entry>div{display:grid;grid-template-columns:minmax(100px,1fr) minmax(100px,180px);gap:10px;align-items:center}.explorer-details .explorer-entry small{margin:0}
    #explorer-name-dialog{max-width:400px;width:calc(100vw - 64px);padding:20px;color:var(--theme-text);background:var(--theme-panel);border:1px solid var(--theme-border);border-radius:8px}#explorer-name-dialog input{box-sizing:border-box;width:100%;padding:8px;margin:10px 0;color:var(--theme-text);background:var(--theme-control);border:1px solid var(--theme-border)}#explorer-name-dialog footer{display:flex;gap:8px;justify-content:flex-end}
  `;document.head.append(style);
  const dialog=document.createElement('dialog');dialog.id='explorer-name-dialog';dialog.innerHTML='<form><strong id="explorer-name-title"></strong><input id="explorer-name" required autocomplete="off" aria-label="名称"><footer><button type="button" id="explorer-name-cancel">取消</button><button type="submit">确定</button></footer></form>';document.body.append(dialog);
  let nameResolve=null;dialog.querySelector('form').onsubmit=e=>{e.preventDefault();nameResolve?.($('explorer-name').value);nameResolve=null;dialog.close()};$('explorer-name-cancel').onclick=()=>dialog.close();dialog.addEventListener('close',()=>{nameResolve?.(null);nameResolve=null});
  function askName(title,value){$('explorer-name-title').textContent=title;$('explorer-name').value=value;dialog.showModal();$('explorer-name').focus();$('explorer-name').select();return new Promise(resolve=>nameResolve=resolve)}
  let data=null,sequence=0,lastScope='',history=[],position=-1,traversing=false,clipboard=null,busy=false,details=false,visibleLimit=240;
  try{details=localStorage.getItem('framekeep.archive.files.view')==='details'}catch{}
  view.onclick=()=>{details=!details;try{localStorage.setItem('framekeep.archive.files.view',details?'details':'thumbnails')}catch{}paint()};
  const chosen=new Map();
  const error=message=>{$('explorer-error').hidden=!message;$('explorer-error').textContent=message};
  const key=scope=>JSON.stringify([scope.root,scope.folder]);
  const bytes=size=>size<1024?size+' B':size<1048576?(size/1024).toFixed(1)+' KB':(size/1048576).toFixed(1)+' MB';
  function selectedRow(node){if(node.dataset.fileIndex!==undefined)return data?.rows[Number(node.dataset.fileIndex)];const row=browser.rows().find(r=>r.id===node.dataset.catalogItem);return row?{name:row.name,folder:(row.folder==='/'?'':row.folder+'/')+row.name,directory:false}:null}
  function selection(){for(const node of grid.querySelectorAll('[data-file-index],[data-catalog-item]')){const row=selectedRow(node);node.classList.toggle('explorer-picked',!!row&&chosen.has(row.folder))}$('explorer-selected').textContent=chosen.size?`已选择 ${chosen.size} 项`:'单击选择，双击打开 · Ctrl 多选';for(const button of bar.querySelectorAll('[data-file-action]'))button.disabled=busy||(['rename','reveal'].includes(button.dataset.fileAction)?chosen.size!==1:['copy','cut'].includes(button.dataset.fileAction)?!chosen.size:button.dataset.fileAction==='paste'?!clipboard:!browser.scope().root)}
  function paint(){
    bar.hidden=!browser.enabled();if(bar.hidden)return;
    for(const node of host.querySelectorAll('#catalog-analysis,#catalog-folder-archive,.capture-batch-tools')){const destination=node.classList.contains('capture-batch-tools')?advanced.lastElementChild:inlineActions;if(!destination.contains(node))destination.append(node)}
    for(const id of ['catalog-decode-start','catalog-decode-pause'])if($(id))$(id).hidden=true;
    grid.classList.toggle('explorer-details',details);view.textContent=details?'缩略图':'详细列表';view.setAttribute('aria-pressed',String(details));
    const scope=browser.scope();$('explorer-back').disabled=position<=0;$('explorer-forward').disabled=position>=history.length-1;$('explorer-up').disabled=!scope.folder;$('explorer-system').disabled=!scope.root;
    grid.querySelector('.explorer-entries')?.remove();
    if(!data||key(scope)!==key(data))return;
    if(document.activeElement!==$('explorer-address'))$('explorer-address').value=data.path;
    {
      const query=$('catalog-search').value.trim().toLocaleLowerCase();
      const matches=data.rows.map((row,i)=>({...row,i})).filter(r=>(!query||r.name.toLocaleLowerCase().includes(query))&&(r.directory||details&&r.media_type&&(!$('catalog-media').value||r.media_type===$('catalog-media').value)));
      const rows=matches.slice(0,visibleLimit);
      if(rows.length){grid.querySelector('.catalog-empty')?.remove();const section=document.createElement('div');section.className='explorer-entries';section.innerHTML=rows.map(row=>`<button type="button" class="explorer-entry" data-file-index="${row.i}" title="${esc(row.name)}"><span aria-hidden="true">${row.directory?'▰':'▤'}</span><div><strong>${esc(row.name)}</strong><small>${row.directory?'文件夹':bytes(row.size)} · ${esc(new Date(row.modified*1000).toLocaleDateString())}</small></div></button>`).join('');grid.prepend(section)}
      else if(!data.total)grid.innerHTML='<div class="catalog-empty">此文件夹为空</div>';
      if(matches.length>visibleLimit){const more=document.createElement('button');more.textContent=`显示更多文件（${visibleLimit} / ${matches.length}）`;more.onclick=()=>{visibleLimit+=240;paint()};grid.querySelector('.explorer-entries')?.append(more)}
      $('catalog-summary').textContent=query?`当前文件夹 · ${matches.length} 项文件名匹配 · ${browser.rows().length} 项媒体资料匹配`:`${data.folders} 个文件夹 · ${data.files} 个文件${$('catalog-media').value?' · 已筛选媒体类型':''}`;
      $('catalog-count').textContent=data.total+' 项';$('catalog-location').textContent=data.path;
    }
    selection();
  }
  async function load(){
    if(!browser.enabled())return;
    const scope=browser.scope(),id=++sequence,next=key(scope);
    if(next!==lastScope){chosen.clear();visibleLimit=240;data=null;lastScope=next;error('');if(!traversing){history=history.slice(0,position+1);history.push({root:scope.root,folder:scope.folder});position=history.length-1}}
    if(!scope.root){$('explorer-address').value='';paint();return}
    try{const result=await api('/api/catalog/browse?'+new URLSearchParams({root:scope.root,folder:scope.folder}));if(id!==sequence||key(browser.scope())!==next)return;data=result;error('');paint()}
    catch(e){if(id!==sequence)return;data=null;grid.querySelector('.explorer-entries')?.remove();error('无法读取此位置：'+e.message);$('catalog-summary').textContent='位置不可访问，未将其显示为空文件夹';throw e}
  }
  async function navigate(root,folder){try{await browser.navigate(root,folder)}catch(e){error(e.message)}}
  async function traverse(delta){const target=position+delta;if(target<0||target>=history.length)return;position=target;traversing=true;try{await navigate(history[position].root,history[position].folder)}finally{traversing=false;paint()}}
  $('explorer-back').onclick=()=>traverse(-1);$('explorer-forward').onclick=()=>traverse(1);
  $('explorer-up').onclick=()=>{const s=browser.scope();navigate(s.root,s.folder.split('/').slice(0,-1).join('/'))};
  async function refresh(){const s=browser.scope();if(!s.root)return;await api('/api/catalog/visit',{root:s.root,folder:s.folder,refresh:true});await browser.refresh()}
  $('explorer-refresh').onclick=()=>refresh().catch(e=>error(e.message));$('explorer-system').onclick=()=>{const s=browser.scope();api('/api/catalog/open',{root_id:s.root,folder:s.folder}).catch(e=>error(e.message))};
  $('explorer-address-form').onsubmit=e=>{e.preventDefault();const path=$('explorer-address').value.trim().replace(/\\/g,'/').replace(/\/+$/,'');const roots=[...browser.roots()].sort((a,b)=>b.path.length-a.path.length);for(const root of roots){const base=root.path.replace(/\\/g,'/').replace(/\/+$/,'');if(path.toLowerCase()===base.toLowerCase()||path.toLowerCase().startsWith(base.toLowerCase()+'/')){navigate(root.id,path.slice(base.length).replace(/^\//,''));return}}error('请输入已添加归档位置内的路径；其他位置请先添加归档文件夹。')};
  grid.addEventListener('click',event=>{const node=event.target.closest('[data-file-index],[data-catalog-item]');if(!node)return;const row=selectedRow(node);if(!row)return;if(!event.ctrlKey&&!event.metaKey)chosen.clear();if(chosen.has(row.folder))chosen.delete(row.folder);else chosen.set(row.folder,row);selection()});
  grid.addEventListener('dblclick',event=>{const node=event.target.closest('[data-file-index]');if(!node)return;const row=selectedRow(node),s=browser.scope();if(row.directory)navigate(s.root,row.folder);else api('/api/catalog/file-open',{root:s.root,folder:row.folder}).catch(e=>error(e.message))});
  async function action(type){
    if(busy)return;const scope=browser.scope(),rows=[...chosen.values()];error('');
    if(type==='copy'||type==='cut'){if(rows.length){clipboard={root:scope.root,rows:rows.filter(r=>!r.name.toLowerCase().endsWith('.xmp')||!rows.some(other=>other!==r&&other.name.replace(/\.[^.]+$/,'').toLowerCase()===r.name.slice(0,-4).toLowerCase())),cut:type==='cut'};$('explorer-selected').textContent=`已${type==='cut'?'剪切':'复制'} ${rows.length} 项，请进入目标文件夹后粘贴`;selection()}return}
    if(type==='reveal'){if(rows.length===1)await api('/api/catalog/file-open',{root:scope.root,folder:rows[0].folder,reveal:true});return}
    let name;if(type==='mkdir'||type==='rename'){if(type==='rename'&&rows.length!==1)return;name=await askName(type==='mkdir'?'新建文件夹':'重命名',type==='mkdir'?'新建文件夹':rows[0].name);if(name===null)return}
    busy=true;selection();let completed=0;
    async function perform(body){
      const submitted=await api('/api/catalog/file-operation',body);
      while(true){
        $('explorer-selected').textContent=`正在${type==='paste'?(clipboard?.cut?'移动':'复制'):type==='mkdir'?'新建':'重命名'} · 已完成 ${completed} 项，请等待…`;
        const state=await api('/api/catalog/file-job?'+new URLSearchParams({id:submitted.job}));
        if(state.status==='complete')return;
        if(state.status==='failed')throw new Error(state.error);
        await new Promise(resolve=>setTimeout(resolve,500));
      }
    }
    try{
      if(type==='mkdir'||type==='rename'){await perform({action:type,root:scope.root,folder:scope.folder,name,source:rows[0]?.folder});completed=1}
      else if(type==='paste'&&clipboard){while(clipboard.rows.length){const row=clipboard.rows[0];await perform({action:clipboard.cut?'move':'copy',root:scope.root,folder:scope.folder,source_root:clipboard.root,source:row.folder});clipboard.rows.shift();completed++}clipboard=null}
      chosen.clear();await refresh();$('explorer-selected').textContent=`已完成 ${completed} 项操作`;
    }catch(e){if(completed)await refresh().catch(()=>{});error((completed?`已完成 ${completed} 项；其余未完成：`:'')+e.message)}finally{busy=false;selection()}
  }
  bar.addEventListener('click',event=>{const button=event.target.closest('[data-file-action]');if(button)action(button.dataset.fileAction).catch(e=>error(e.message))});
  host.addEventListener('keydown',event=>{if(event.target.closest('input,textarea,select,[contenteditable="true"]'))return;let run;if(event.altKey&&event.key==='ArrowLeft')run=()=>traverse(-1);else if(event.altKey&&event.key==='ArrowRight')run=()=>traverse(1);else if(event.altKey&&event.key==='ArrowUp')run=()=>$('explorer-up').click();else if(event.key==='F5')run=refresh;else if(event.key==='F2')run=()=>action('rename');else if(event.ctrlKey&&['c','x','v'].includes(event.key.toLowerCase()))run=()=>action(({c:'copy',x:'cut',v:'paste'})[event.key.toLowerCase()]);if(run){event.preventDefault();Promise.resolve(run()).catch(e=>error(e.message))}});
  window.FramekeepArchiveFiles={load,paint};
})();
