/* 桌面、快捷窗与 Web 共用的参考图输入和相似度筛选。 */
(() => {
  'use strict';
  function mount(host,{prepare=null,onChange=()=>{},onRefresh=()=>{},onThreshold=()=>{},inputId='',inputClass=''}={}){
    host.classList.add('reference-search');
    host.innerHTML=`<header><strong>参考图检索</strong><span>上传后自动搜索</span></header><button type="button" class="reference-dropzone" aria-label="点击上传或拖入搜索参考图"><img class="reference-preview" alt="搜索参考图预览" hidden><span class="reference-empty"><svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="m3 16 5-5 4 4 4-6 5 7M12 7v6M9 10h6"/></svg><strong>拖拽图片到这里</strong><small>或点击选择图片 · Ctrl+V 粘贴</small></span></button><input type="file" class="reference-file ${inputClass}" ${inputId?'id="'+inputId+'"':''} accept="image/*,.cr3,.cr2,.nef,.nrw,.arw,.srf,.sr2,.raf,.rw2,.orf,.dng,.pef,.3fr,.fff,.rwl,.iiq,.srw" hidden><p class="reference-name">支持图片及 RAW，不限制文件大小</p><div class="reference-actions"><button type="button" data-reference="choose">选择图片</button><button type="button" data-reference="paste">粘贴图片</button><button type="button" data-reference="clear" disabled>移除</button></div><button type="button" data-reference="generate" class="primary" disabled>生成检索结果</button><label class="reference-threshold-label"><span>最低相似度</span><output>50%</output><input class="reference-threshold" type="range" min="0" max="100" value="50" step="1" aria-label="最低相似度" disabled></label><p class="reference-help" role="status">相似度越高，匹配越严格。</p>`;
    const find=selector=>host.querySelector(selector),action=name=>find(`[data-reference="${name}"]`),file=find('.reference-file'),zone=find('.reference-dropzone');
    const value={image:'',itemId:'',src:'',label:'',threshold:50};let generation=0,busy=false,uploadController=null;
    const active=()=>!!(value.image||value.itemId),snapshot=()=>({...value,active:active()});
    function render(){
      const ready=active(),preview=find('.reference-preview');host.classList.toggle('has-reference',ready);
      preview.hidden=!ready;find('.reference-empty').hidden=ready;if(ready){preview.src=value.src;preview.alt=value.label||'搜索参考图'}else preview.removeAttribute('src');
      find('.reference-name').textContent=ready?value.label:'支持图片及 RAW，不限制文件大小';
      action('clear').disabled=!ready;action('generate').disabled=!ready||busy;action('generate').textContent=busy&&ready?'正在检索…':'生成检索结果';find('.reference-threshold').disabled=!ready;
      find('.reference-help').textContent=ready?'拖动滑块即时筛选当前候选，分数不代表同款概率。':'相似度越高，匹配越严格。';
    }
    function set(next,notify=true){generation++;uploadController?.abort();Object.assign(value,{image:'',itemId:'',src:'',label:''},next);render();if(notify)onChange(snapshot())}
    const supported=selected=>selected.type.startsWith('image/')||/\.(cr3|cr2|nef|nrw|arw|srf|sr2|raf|rw2|orf|dng|pef|3fr|fff|rwl|iiq|srw|jpe?g|png|webp|tiff?|bmp|gif|hei[cf])$/i.test(selected.name||'');
    async function upload(selected){
      if(!selected)return;
      const ticket=++generation;uploadController?.abort();uploadController=new AbortController();
      busy=true;action('generate').disabled=true;find('.reference-help').textContent='正在上传并提取检索预览…';
      try{
        if(!supported(selected))throw Error('请上传图片或相机 RAW 文件');
        let image;
        if(prepare){
          const prepared=await prepare(selected,uploadController.signal);image=prepared.image;
        }else image=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(Error('图片读取失败'));reader.readAsDataURL(selected)});
        const preview=new Image();preview.src=image;await preview.decode();
        if(ticket!==generation)return;
        busy=false;set({image,src:image,label:selected.name||'剪贴板图片'});
      }catch(error){
        if(ticket===generation){busy=false;action('generate').disabled=!active();action('generate').textContent='生成检索结果';find('.reference-help').textContent=error.message||'图片无法读取，请换一张重试'}
      }
    }
    function handlePaste(event){
      if(event.defaultPrevented)return;
      for(const item of event.clipboardData?.items||[]){
        const selected=item.getAsFile?.();if(selected&&supported(selected)){event.preventDefault();upload(selected);return}
      }
    }
    async function paste(){
      if(navigator.clipboard?.read){
        try{
          for(const item of await navigator.clipboard.read()){
            const type=item.types.find(type=>type.startsWith('image/'));
            if(type){await upload(new File([await item.getType(type)],'剪贴板图片',{type}));return}
          }
        }catch{}
      }
      find('.reference-help').textContent='请点击此区域后按 Ctrl+V 粘贴图片。';zone.focus();
    }
    zone.onclick=()=>file.click();action('choose').onclick=()=>file.click();action('paste').onclick=paste;
    action('clear').onclick=()=>set({});action('generate').onclick=()=>onRefresh(snapshot());
    file.onchange=event=>{upload(event.target.files[0]);file.value=''};
    host.addEventListener('paste',handlePaste);
    host.addEventListener('dragover',event=>{if(event.dataTransfer?.types.includes('Files')){event.preventDefault();host.classList.add('is-dragging')}});
    host.addEventListener('dragleave',event=>{if(!host.contains(event.relatedTarget))host.classList.remove('is-dragging')});
    host.addEventListener('drop',event=>{host.classList.remove('is-dragging');if(event.dataTransfer?.files.length){event.preventDefault();upload(event.dataTransfer.files[0])}});
    find('.reference-threshold').oninput=event=>{value.threshold=Math.max(0,Math.min(100,Number(event.target.value)||0));find('output').value=value.threshold+'%';onThreshold(snapshot())};
    render();
    return {set,setBusy:value=>{busy=!!value;action('generate').disabled=!active()||busy;action('generate').textContent=busy&&active()?'正在检索…':'生成检索结果'},clear:(notify=true)=>set({},notify),upload,handlePaste,choose:()=>file.click(),get value(){return snapshot()},
      filter:rows=>active()?rows.filter(row=>Number.isFinite(Number(row.score))&&Number(row.score)+.00001>=value.threshold/100):rows};
  }
  window.FramekeepReferenceSearch={mount};
})();
