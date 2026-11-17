(() => {
  'use strict';
  if(!window.FramekeepIO)return;
  const css=document.createElement('link');css.rel='stylesheet';css.href='shooting-export.css';document.head.append(css);
  const dialog=document.createElement('dialog');dialog.id='shooting-export-dialog';dialog.className='shooting-export-dialog';dialog.setAttribute('aria-labelledby','shooting-export-title');
  dialog.innerHTML=`<header><span class="export-eyebrow">SHIYING · DIGITAL ASSETS</span><button id="shooting-export-close" aria-label="收起导出进度" type="button">×</button></header>
    <div class="export-art" aria-hidden="true"><svg viewBox="0 0 640 290" xmlns="http://www.w3.org/2000/svg">
      <defs><linearGradient id="export-paper" x2=".8" y2="1"><stop stop-color="#f4f1e7"/><stop offset="1" stop-color="#d7dccd"/></linearGradient><linearGradient id="export-photo" x2="1" y2="1"><stop stop-color="#8aada0"/><stop offset="1" stop-color="#3e635a"/></linearGradient><radialGradient id="export-glow"><stop stop-color="#c7dfac" stop-opacity=".16"/><stop offset="1" stop-color="#c7dfac" stop-opacity="0"/></radialGradient></defs>
      <ellipse cx="340" cy="150" rx="270" ry="142" fill="url(#export-glow)"/>
      <path d="M105 144H254M160 207C206 239 255 233 282 198" fill="none" stroke="#c5d5bc" stroke-opacity=".2" stroke-dasharray="3 7"/>
      <g class="export-frame export-frame-back" transform="translate(98 78) rotate(-12 62 74)"><rect width="124" height="151" rx="13" fill="#252e29" stroke="#819080" stroke-opacity=".4"/><rect x="10" y="10" width="104" height="109" rx="7" fill="#567364"/><path d="m11 99 35-45 30 32 14-16 24 29" fill="#b4c7ac" opacity=".55"/><circle cx="83" cy="36" r="13" fill="#c7dfac" opacity=".7"/><path d="M14 134h49m8 0h27" stroke="#a9b6a4" stroke-linecap="round"/></g>
      <g class="export-frame export-frame-front" transform="translate(175 115) rotate(8 48 58)"><rect width="96" height="114" rx="11" fill="#303c32" stroke="#adbd9d" stroke-opacity=".5"/><rect x="8" y="8" width="80" height="79" rx="6" fill="url(#export-photo)"/><path d="m9 70 25-35 22 28 13-16 18 24" fill="#d1d8be" opacity=".6"/><path d="M13 101h43" stroke="#becbb2" stroke-linecap="round"/></g>
      <g class="export-sheet" transform="translate(296 32)"><path d="M14 0h153l37 37v188a14 14 0 0 1-14 14H14A14 14 0 0 1 0 225V14A14 14 0 0 1 14 0Z" fill="url(#export-paper)"/><path d="M167 0v23a14 14 0 0 0 14 14h23" fill="#bcc8b4"/><text x="22" y="37" font-size="10" letter-spacing="3" fill="#6b7865">FRAMEKEEP</text><text x="22" y="66" font-size="21" font-weight="600" fill="#293b2c">拍摄记录</text><path d="M22 81h160" stroke="#a5b49d"/><g class="export-lines" stroke="#7d9077" stroke-width="2" stroke-linecap="round"><path d="M75 108h104M75 119h63M75 148h104M75 159h79M75 188h104M75 199h58"/></g><g fill="#92a68a"><rect x="22" y="98" width="39" height="28" rx="4"/><rect x="22" y="138" width="39" height="28" rx="4"/><rect x="22" y="178" width="39" height="28" rx="4"/></g><path d="M22 222h102" stroke="#9dac94"/><circle cx="177" cy="221" r="3" fill="#8d9d80"/></g>
      <g class="export-seal" transform="translate(494 205)"><circle r="32" fill="#233c2d" stroke="#b8d59d"/><circle class="export-seal-track" r="25" fill="none" stroke="#c9e5ae" stroke-width="2" pathLength="100"/><path class="export-check" d="m-11 0 7 7L12-8" fill="none" stroke="#e2f0d4" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></g>
      <g class="export-particle" fill="#c6d7b3"><circle cx="257" cy="89" r="3"/><circle cx="555" cy="114" r="2"/><path d="M544 56v10m-5-5h10" stroke="#c6d7b3" stroke-width="1.4"/></g>
    </svg></div>
    <div class="export-copy"><p id="shooting-export-document" class="export-document"></p><h2 id="shooting-export-title">正在整理拍摄记录</h2><p id="shooting-export-status" role="status" aria-live="polite">正在准备导出…</p></div>
    <div class="export-progress-line"><span id="shooting-export-stage">准备记录</span><strong id="shooting-export-percent">0%</strong></div><progress id="shooting-export-progress" max="100" value="0" aria-label="拍摄文档导出进度"></progress>
    <div class="export-progress-meta"><span id="shooting-export-images">正在统计图片</span><span id="shooting-export-note">拍摄记录已保存</span></div>
    <ol class="export-stages" aria-label="导出步骤"><li>整理记录</li><li>处理图片</li><li>编排文档</li><li>生成完成</li></ol>
    <p id="shooting-export-warning" class="export-warning" role="status"></p><footer><span>可收起此窗口，继续拍摄</span><button id="shooting-export-save" class="primary" type="button" hidden>保存文档</button><button id="shooting-export-retry" class="primary" type="button" hidden>重新导出</button><button id="shooting-export-hide" class="bordered" type="button">收起</button></footer>`;
  document.body.append(dialog);
  const $=id=>document.getElementById(id),api=FramekeepIO.api;
  let current=null,project=null,starting=false,saving=false;
  const running=()=>starting||['queued','running'].includes(current?.status);
  function open(){if(!dialog.open)dialog.showModal()}
  function paint(task){
    dialog.dataset.status=task.status;dialog.dataset.stage=task.stage;
    $('shooting-export-document').textContent=task.document||project?.name||'';
    $('shooting-export-title').textContent=task.status==='failed'?'这次导出没有完成':task.status==='complete'?'拍摄文档已就绪':'正在整理拍摄记录';
    $('shooting-export-status').textContent=task.error||task.message||'正在准备导出…';
    $('shooting-export-stage').textContent=({queued:'等待导出',snapshot:'整理记录',images:'处理商品图片',workbook:'编排表格',labels:'处理标签图片',pack:'整理文档版式',persist:'保存文档',complete:'生成完成',failed:'导出未完成'})[task.stage]||'正在处理';
    $('shooting-export-percent').textContent=`${task.percent||0}%`;$('shooting-export-progress').value=task.percent||0;
    dialog.style.setProperty('--export-progress',String(task.percent||0));
    $('shooting-export-images').textContent=task.stage==='queued'||task.stage==='snapshot'?'正在统计图片':`图片 ${task.images_done||0} / ${task.images_total||0}`;
    $('shooting-export-note').textContent=task.status==='complete'?'文件已生成':task.status==='failed'?'拍摄记录保留':'后台处理，可继续拍摄';
    $('shooting-export-warning').textContent=task.missing_images?`${task.missing_images} 张图片暂时无法读取，文档中保留相应记录。`:'';
    const phase=task.status==='complete'?4:['workbook','pack','persist'].includes(task.stage)?3:['images','labels'].includes(task.stage)?2:1;
    [...dialog.querySelectorAll('.export-stages li')].forEach((el,i)=>{el.classList.toggle('is-done',i<phase-1||task.status==='complete');el.classList.toggle('is-active',i===phase-1)});
    $('shooting-export-save').hidden=task.status!=='complete';$('shooting-export-retry').hidden=!['complete','failed'].includes(task.status);$('shooting-export-retry').textContent=task.status==='complete'?'重新生成':'重新导出';
  }
  async function save(task){
    if(saving||task?.status!=='complete')return;saving=true;$('shooting-export-save').disabled=true;
    try{
      if(window.FramekeepDesktop?.saveSkuWorkbook){
        const saved=await FramekeepDesktop.saveSkuWorkbook(task.project_id,task.name,task.source_name);
        task.saved=!!saved;$('shooting-export-status').textContent=saved?'拍摄记录已保存。':'文档已生成，尚未保存；可再次点击保存文档。';return;
      }
      const url='/api/sku/download?'+new URLSearchParams({project_id:task.project_id,job_id:task.id});
      if(window.showSaveFilePicker){
        const handle=await showSaveFilePicker({suggestedName:task.name,types:[{description:'Excel 工作簿',accept:{'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':['.xlsx']}}]});
        const response=await fetch(url);if(!response.ok)throw new Error('文档下载失败，可再次点击保存');
        const blob=await response.blob(),stream=await handle.createWritable();try{await stream.write(blob);await stream.close()}catch(error){await stream.abort().catch(()=>{});throw error}
        task.saved=true;$('shooting-export-status').textContent='拍摄记录已保存：'+handle.name;
      }else{
        const a=document.createElement('a');a.href=url;a.download=task.name;document.body.append(a);a.click();a.remove();task.saved=true;$('shooting-export-status').textContent='文档已生成，已开始下载。';
      }
    }catch(error){$('shooting-export-status').textContent=error.name==='AbortError'?'文档已生成，尚未保存；可再次点击保存文档。':error.message}
    finally{saving=false;$('shooting-export-save').disabled=false}
  }
  async function poll(id){
    let failures=0;
    while(current?.id===id&&running()){
      try{const task=await api('/api/sku/export?'+new URLSearchParams({job_id:id}),undefined,8000);if(current?.id!==id)return;current=task;paint(task);failures=0;
        if(task.status==='complete'){document.dispatchEvent(new CustomEvent('framekeep:export-state'));if(dialog.open&&(!window.showSaveFilePicker||window.FramekeepDesktop?.saveSkuWorkbook))await save(task);else if(dialog.open)$('shooting-export-status').textContent='文档已生成，点击保存文档选择保存位置。';else Framekeep.notify('拍摄文档已就绪，点击导出拍摄记录保存');return}
        if(task.status==='failed'){document.dispatchEvent(new CustomEvent('framekeep:export-state'));return}
      }catch(error){failures++;$('shooting-export-status').textContent='暂时无法读取导出进度，正在重新连接…';if(failures>=6){current={...current,status:'failed',stage:'failed',error:'无法读取导出任务，请重新连接后重试；已确认的拍摄记录保留'};paint(current);return}}
      await new Promise(resolve=>setTimeout(resolve,450));
    }
  }
  async function start(p,flush,force=false){
    if(starting||running()||(!force&&current?.status==='complete'&&!current.saved&&current.project_id===p.id&&!dialog.open)){open();return}
    project={id:p.id,name:p.name};starting=true;current=null;paint({status:'running',stage:'snapshot',percent:0,document:p.name});open();
    try{await flush?.();current=await api('/api/sku/export',{project_id:p.id,background:true});paint(current);starting=false;document.dispatchEvent(new CustomEvent('framekeep:export-state'));poll(current.id)}
    catch(error){starting=false;current={status:'failed',stage:'failed',document:p.name,error:error.message};paint(current);throw error}
  }
  for(const id of ['shooting-export-close','shooting-export-hide'])$(id).onclick=()=>dialog.close();
  $('shooting-export-save').onclick=()=>save(current);
  $('shooting-export-retry').onclick=()=>start(project,()=>FramekeepIO.flush(),true).catch(()=>{});
  window.FramekeepShootingExport={start,running};
})();
