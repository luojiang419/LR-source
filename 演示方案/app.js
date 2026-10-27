(() => {
  'use strict';
  const photos = window.DEMO_PHOTOS;
  const session=window.FRAMEKEEP_SESSION||{demo:true,name:'2026-10-14',saved:{}};
  function recover(target){
    let saved=target.saved||{};
    try{const recovery=JSON.parse(localStorage.getItem('framekeep:'+target.id)||'null');if(recovery?.snapshot===target.snapshot&&recovery.revision===target.revision&&recovery.state?.analysisMode!=='ai')saved=recovery.state}catch{}
    if(target.browser?.paged){const ids=new Set(photos.map(p=>p.id));saved={...saved};for(const key of ['overrides','rejectedPrevious','skuMarkers'])saved[key]=Object.fromEntries(Object.entries(saved[key]||{}).filter(([id])=>ids.has(id)))}
    return {...saved,overrides:{...saved.overrides,...target.sharedRatings}};
  }
  let saved=recover(session),sessionLoading=false,monitoring=false,interactionPaused=false;
  let folderPrefix=saved.folderPrefix||'';
  let skuMarkers=saved.skuMarkers||{};
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const time = (p) => p.time?p.time.slice(11,19):'时间缺失';
  const seconds = (p) => p.time?new Date(p.time).getTime()/1000:Infinity;
  let keep=saved.keep||1,threshold=saved.threshold||4,protectManual=saved.protectManual!==false,overrides=saved.overrides||{},rejectedPrevious=saved.rejectedPrevious||{};
  let filter=['all','picked','unpicked'].includes(saved.filter)?saved.filter:'all',view=saved.view||'grid';
  let selectedId=saved.selectedId||photos[0]?.id||null,query=saved.query||'',sort=saved.sort||'time-asc',thumbSize=saved.thumbSize||180,compare=false,compareTail=null;
  const history=[];let modelRevision=0,modelCache=null;
  let pickStroke=null,pickPointer=null,shiftHeld=false,suppressPickClick=false;
  let toastTimer;
  const nearbyPreviews=new Map();let preloadTimer;
  const suspendedPreviews=new Map();
  const previewSource=url=>selectionVisible()?`src="${esc(url)}"`:`data-pending-preview="${esc(url)}"`;
  const selectionVisible=()=>!window.FramekeepBoot?.loading&&document.readyState!=='loading'&&(!window.FramekeepWorkspace||FramekeepWorkspace.current()==='selection');
  function syncSelectionPreviews(){
    for(const image of suspendedPreviews.keys())if(!image.isConnected)suspendedPreviews.delete(image);
    if(selectionVisible()){
      for(const image of $('selection-page').querySelectorAll('img[data-pending-preview]')){image.src=image.dataset.pendingPreview;delete image.dataset.pendingPreview}
      for(const [image,url] of suspendedPreviews){if(image.isConnected&&!image.getAttribute('src'))image.src=url}
      suspendedPreviews.clear();return;
    }
    clearTimeout(preloadTimer);
    for(const image of nearbyPreviews.values())image.removeAttribute('src');nearbyPreviews.clear();
    for(const image of $('selection-page').querySelectorAll('img[src]')){
      const url=image.getAttribute('src');
      if(url?.includes('/api/preview/')){suspendedPreviews.set(image,url);image.removeAttribute('src')}
    }
  }
  const previewRequestObserver=new MutationObserver(syncSelectionPreviews);
  previewRequestObserver.observe($('selection-page'),{subtree:true,childList:true,attributes:true,attributeFilter:['src']});
  document.addEventListener('framekeep:workspace',syncSelectionPreviews);
  document.addEventListener('DOMContentLoaded',syncSelectionPreviews,{once:true});
  document.addEventListener('framekeep:boot-ready',syncSelectionPreviews,{once:true});
  window.addEventListener('pagehide',()=>{clearTimeout(preloadTimer);previewRequestObserver.disconnect()},{once:true});
  function preloadNearby(order,index){
    clearTimeout(preloadTimer);
    if(!selectionVisible())return;
    const nearby=order.slice(Math.max(0,index-1),index+2).map(item=>item.p.previewLarge||item.p.preview);
    for(const url of nearbyPreviews.keys())if(!nearby.includes(url)){nearbyPreviews.get(url).removeAttribute('src');nearbyPreviews.delete(url)}
    // 当前图片先发起加载；相邻两张以低优先级预取并保留解码结果。
    preloadTimer=setTimeout(()=>{
      if(!selectionVisible())return;
      for(const url of nearby){
        if(!url||nearbyPreviews.has(url))continue;
        const image=new Image();image.fetchPriority='low';image.decoding='async';image.src=url;
        nearbyPreviews.set(url,image);image.decode().catch(()=>{if(nearbyPreviews.get(url)===image)nearbyPreviews.delete(url)});
      }
    },100);
  }
  function groups() {
    const result=[];
    photos.forEach((p,i)=>{if(!i || (p.browserGroup!=null?p.browserGroup!==photos[i-1].browserGroup:p.missingTime||photos[i-1].missingTime||(p.cameraKey||p.camera)!==(photos[i-1].cameraKey||photos[i-1].camera)||seconds(p)-seconds(photos[i-1])>threshold))result.push([]);result.at(-1).push(p)});
    return result;
  }
  function rating(p,g,index,position=undefined) {
    if(Object.hasOwn(overrides,p.id))return overrides[p.id];
    if(Object.hasOwn(p,'liveRating'))return p.liveRating;
    return Math.max(p.existingRating||0,p.missingTime||(position??g.indexOf(p))>=Math.max(0,g.length-keep)?1:0);
  }
  function model() {
    if(modelCache?.revision===modelRevision){const m=modelCache.value;if(!m.visible.some(i=>i.p.id===selectedId))selectedId=m.visible[0]?.p.id??null;return {...m,current:m.visible.find(i=>i.p.id===selectedId)||null}}
    const all=groups();
    const items=all.flatMap((g,localIndex)=>{const index=g[0]?.browserGroup!=null?g[0].browserGroup-1:localIndex;return g.map((p,position)=>({p,g,index,r:rating(p,g,index,position)}))});
    items.sort((a,b)=>{
      if(sort==='rating-desc'&&a.r!==b.r)return b.r-a.r;
      const ta=seconds(a.p),tb=seconds(b.p);
      if(!Number.isFinite(ta)||!Number.isFinite(tb))return Number.isFinite(ta)?-1:Number.isFinite(tb)?1:0;
      return sort==='time-desc'?tb-ta:ta-tb;
    });
    let visible=items.filter(item=>inFolder(item)&&(filter==='all'||filter==='picked'&&item.r>0||filter==='unpicked'&&item.r<=0)&&(!query||(item.p.id+' '+(item.p.raw||'')+' '+item.p.jpg).toLowerCase().includes(query.toLowerCase())));
    // 滑选中固定当前排布，避免已选/未选筛选或星级排序使照片从鼠标下跳走。
    if(pickStroke?.active){const byId=new Map(items.map(item=>[item.p.id,item]));visible=pickStroke.order.map(id=>byId.get(id)).filter(Boolean)}
    if(!visible.some(item=>item.p.id===selectedId))selectedId=visible[0]?.p.id??null;
    const value={all,items,visible,positions:new Map(photos.map((p,i)=>[p.id,i])),current:visible.find(item=>item.p.id===selectedId)||null};modelCache={revision:modelRevision,value};return value;
  }
  function inFolder(item){return !folderPrefix||[item.p.raw,item.p.jpg].some(path=>path?.replaceAll('\\','/').toLowerCase().startsWith(folderPrefix.toLowerCase()+'/'))}
  function browseItems(m){return monitoring?m.items.filter(inFolder):m.visible}
  function save(){history.push({keep,threshold,protectManual,overrides:{...overrides},rejectedPrevious:{...rejectedPrevious},skuMarkers:{...skuMarkers},filter,view,selectedId,query,sort,paged:!!session.browser?.paged,pending:!!session.browser?.paged,ratings:session.browser?.paged?Object.fromEntries(model().items.map(i=>[i.p.id,i.r])):null});if(history.length>100)history.shift()}
  function finishHistory(){const previous=history.at(-1);if(!previous?.pending)return;for(const [key,current] of Object.entries({overrides,rejectedPrevious,skuMarkers})){const old=previous[key],delta={};for(const id of new Set([...Object.keys(old),...Object.keys(current)]))if(JSON.stringify(old[id])!==JSON.stringify(current[id]))delta[id]=Object.hasOwn(old,id)?old[id]:key==='overrides'?(previous.ratings?.[id]??null):null;previous[key]=delta}previous.pending=false;delete previous.ratings}
  function applyDelta(target,delta,tombstones=false){for(const [id,value] of Object.entries(delta||{})){if(value===null&&!tombstones)delete target[id];else target[id]=value}}
  function tagSku(id,value,context=undefined){if(value===2)skuMarkers[id]=context===undefined?(window.FramekeepSku?.context()||{automatic:true}):(context||{automatic:true});else delete skuMarkers[id]}
  function notify(message){$('toast').textContent=message;$('toast').classList.add('visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').classList.remove('visible'),2300)}
  function stateLabel(item){return item.r<0?'暂不选':Object.hasOwn(overrides,item.p.id)?'已保存评级':item.p.browserAutomatic?(item.r>0?'末帧推荐':'未选'):Object.hasOwn(item.p,'liveRating')?(item.r>0?'已有评级':'待现场标记'):item.r>0?'末帧推荐':'未选'}
  function imageName(p){return(p.raw||p.jpg||p.name||p.id).split(/[\\/]/).at(-1)}
  let catalogVirtualKey='',catalogFrame=null;
  const catalogHeights=new Map();
  function catalogRange(m){
    const box=$('catalog-view'),grid=view==='grid',style=getComputedStyle($('photo-grid'));
    const columns=grid?Math.max(1,style.gridTemplateColumns.split(' ').filter(x=>x&&x!=='none').length):1;
    const metric=[view,thumbSize,columns].join(':'),height=catalogHeights.get(metric)||(grid?thumbSize*.94+72:thumbSize*.36+18);
    if(m.visible.length<=80)return {rows:m.visible,start:0,end:m.visible.length,top:0,bottom:0,columns,height,metric};
    const first=Math.max(0,Math.floor(box.scrollTop/height)-2),last=first+Math.ceil((box.clientHeight||640)/height)+5;
    const start=Math.min(m.visible.length,first*columns),end=Math.min(m.visible.length,last*columns);
    return {rows:m.visible.slice(start,end),start,end,top:Math.floor(start/columns)*height,
      bottom:Math.max(0,(Math.ceil(m.visible.length/columns)-Math.ceil(end/columns))*height),columns,height,metric};
  }
  function ensureCatalogFocus(id){
    if(view==='single')return;
    const m=model(),index=m.visible.findIndex(i=>i.p.id===id);if(index<0)return;
    const range=catalogRange(m);
    if(index<range.start||index>=range.end){$('catalog-view').scrollTop=Math.floor(index/range.columns)*range.height;catalogVirtualKey='';renderCatalog(m)}
    (view==='grid'?$('photo-grid'):$('photo-list')).querySelector(`[data-photo="${id}"]`)?.scrollIntoView({block:'nearest'});
  }
  function refreshCatalogWindow(){if(catalogFrame)return;catalogFrame=requestAnimationFrame(()=>{catalogFrame=null;renderCatalog(model())})}
  $('catalog-view').addEventListener('scroll',refreshCatalogWindow,{passive:true});
  new ResizeObserver(()=>{catalogVirtualKey='';refreshCatalogWindow()}).observe($('catalog-view'));
  function renderCatalog(m){
    const empty=!m.visible.length;
    FramekeepEmpty.render($('catalog-empty'),{loading:sessionLoading||!!window.FramekeepWatch?.initializing,monitoring,count:session.browser?.total??photos.length});
    $('catalog-view').classList.toggle('hidden',view==='single');$('loupe-view').classList.toggle('hidden',view!=='single');
    $('photo-grid').classList.toggle('hidden',view!=='grid'||empty);$('photo-list').classList.toggle('hidden',view!=='list'||empty);$('catalog-empty').classList.toggle('hidden',!empty);
    $('catalog-view').setAttribute('aria-label',view==='list'?'照片列表':'照片网格');
    document.querySelector('.loupe-heading').classList.toggle('hidden',view!=='single');
    const range=catalogRange(m),key=JSON.stringify([view,selectedId,thumbSize,range.start,range.end,range.height,range.top,range.bottom,range.rows.map(i=>[i.p.id,i.p.preview,i.r,i.index,Object.hasOwn(overrides,i.p.id)])]);
    if(key===catalogVirtualKey)return;catalogVirtualKey=key;
    $('photo-grid').innerHTML=view==='grid'?`<div class="virtual-spacer" style="height:${range.top}px" aria-hidden="true"></div>`+range.rows.map(item=>`<article class="photo-card ${item.p.id===selectedId?'focused':''} ${item.r>0?'picked':''} ${item.r<0?'excluded':''}" data-photo="${item.p.id}" aria-label="${item.p.id}"><div class="card-top"><span>${String((item.p.browserPosition??m.positions.get(item.p.id))+1).padStart(3,'0')} · 组 ${String(item.index+1).padStart(2,'0')}</span><label><input type="checkbox" data-pick="${item.p.id}" ${item.r>0?'checked':''} aria-label="保留 ${item.p.id}"></label></div><button class="grid-cover" data-focus="${item.p.id}" aria-label="选择 ${item.p.id}" aria-pressed="${item.p.id===selectedId}"><img ${previewSource(item.p.preview)} alt="${item.p.id}" loading="lazy">${item.p.raw && item.p.jpg ? '<span class="photo-format-badge">J/R</span>' : ''}</button><div class="card-name">${esc(imageName(item.p))}</div><div class="card-bottom"><span class="card-rating ${item.r<=0?'muted':''}">${item.r>0?'★'.repeat(item.r):item.r<0?'×':'—'}</span><span class="card-state">${stateLabel(item)}</span></div></article>`).join('')+`<div class="virtual-spacer" style="height:${range.bottom}px" aria-hidden="true"></div>`:'';
    $('photo-list').innerHTML=view==='list'?`<table class="list-table"><thead><tr><th class="check-col">选</th><th class="preview-col">预览</th><th class="name-col">文件名</th><th class="time-col">拍摄时间</th><th class="gap-col">间隔</th><th class="group-col">分组</th><th class="rating-col">星级</th></tr></thead><tbody><tr aria-hidden="true"><td colspan="7" style="height:${range.top}px;padding:0;border:0"></td></tr>${range.rows.map(item=>{const pos=m.positions.get(item.p.id),gap=item.p.browserGap??(pos?seconds(item.p)-seconds(photos[pos-1]):null);return `<tr class="${item.p.id===selectedId?'focused':''}" data-photo="${item.p.id}"><td class="check-col"><input type="checkbox" data-pick="${item.p.id}" ${item.r>0?'checked':''} aria-label="保留 ${item.p.id}"></td><td class="preview-col"><button class="list-preview" data-focus="${item.p.id}" aria-label="选择 ${item.p.id}"><img ${previewSource(item.p.preview)} alt="" loading="lazy">${item.p.raw && item.p.jpg ? '<span class="photo-format-badge">J/R</span>' : ''}</button></td><td class="name-col"><button class="list-name" data-focus="${item.p.id}">${esc(imageName(item.p))}</button><span class="list-subtitle">${stateLabel(item)} · ${item.p.raw?(item.p.jpg?'RAW + JPG':'RAW'):'图片'}</span></td><td class="time-col">${time(item.p)}</td><td class="gap-col">${gap===null?'—':gap+' s'}</td><td class="group-col">${String(item.index+1).padStart(2,'0')}</td><td class="rating-col"><div class="inline-stars">${[1,2,3,4,5].map(n=>`<button class="${item.r>=n?'on':''}" data-star="${n}" data-for="${item.p.id}" aria-label="${item.p.id} 标 ${n} 星">★</button>`).join('')}</div></td></tr>`}).join('')}<tr aria-hidden="true"><td colspan="7" style="height:${range.bottom}px;padding:0;border:0"></td></tr></tbody></table>`:'';
    const measured=(view==='grid'?$('photo-grid'):$('photo-list')).querySelector('[data-photo]')?.getBoundingClientRect().height;
    if(measured>0&&Math.abs((catalogHeights.get(range.metric)||0)-(measured+(view==='grid'?1:0)))>.5){catalogHeights.set(range.metric,measured+(view==='grid'?1:0));catalogVirtualKey='';refreshCatalogWindow()}
  }
  function renderLoupe(m){
    const item=m.current;
    if(!item){
      let empty=$('photos').querySelector('.preview-empty');
      if(!empty){empty=document.createElement('div');empty.className='preview-empty';empty.setAttribute('role','status');$('photos').replaceChildren(empty)}
      FramekeepEmpty.render(empty,{loading:sessionLoading,monitoring,count:session.browser?.total??photos.length});
      delete $('photos').dataset.previewKey;$('loupe-name').textContent='';$('photo-position').textContent='';$('previous').disabled=true;$('next').disabled=true;FramekeepViewer.refresh();return;
    }
    if(compare&&(item.p.browserGroupSize??item.g.length)<2)compare=false;
    const viewing=compare?(compareTail||item.g.slice(-2)):[item.p];
    const previewKey=JSON.stringify([compare,selectedId,viewing.map(p=>[p.id,p.previewLarge||p.preview,imageName(p),compare?rating(p,item.g,item.index):null])]);
    if($('photos').dataset.previewKey!==previewKey){
      $('photos').innerHTML=viewing.map(p=>`<div class="photo-frame ${compare&&p.id===selectedId?'focused':''}"><button class="image-button" data-loupe="${p.id}" aria-label="${compare?'选择':'缩放'} ${p.id}"><img ${previewSource(p.previewLarge||p.preview)} alt="${esc(imageName(p))}" fetchpriority="high">${p.raw && p.jpg ? '<span class="photo-format-badge">J/R</span>' : ''}</button>${compare?`<span class="frame-caption">${esc(imageName(p))} · ${Math.max(0,rating(p,item.g,item.index))} 星</span>`:''}</div>`).join('');
      $('photos').dataset.previewKey=previewKey;
    }
    $('loupe-name').textContent=imageName(item.p);
    const order=browseItems(m),index=order.indexOf(item);
    preloadNearby(order,index);
    const page=window.FramekeepPaging?.effective()||null;
    $('photo-position').textContent=`${(page?.offset||0)+index+1} / ${page?.matched??order.length}`;
    FramekeepViewer.refresh();
    $('previous').disabled=index<=0&&!(page?.offset>0);$('next').disabled=index>=order.length-1&&(!page||page.offset+page.limit>=page.matched);
  }
  function renderSide(m){
    const item=m.current,p=item?.p;
    $('filename').textContent=p?imageName(p):'未选择照片';
    $('current-group').textContent=item?`组 ${String(item.index+1).padStart(2,'0')}`:'';
    $('stars').innerHTML=item?[1,2,3,4,5].map(n=>`<button class="${item.r>=n?'on':''}" data-rating="${n}" aria-label="标记为 ${n} 星">★</button>`).join(''):'';
    $('reject').disabled=!item;$('clear-rating').disabled=!item;$('reject').setAttribute('aria-pressed',item?.r<0);$('pick-reason').textContent=item?stateLabel(item):'';
    if(p){const pos=photos.indexOf(p);const fields=[['文件',imageName(p)],['时间',time(p)],['前张间隔',p.browserPosition!=null?(p.browserGap!=null?`${p.browserGap} 秒`:p.browserPosition?'时间缺失':'首张'):pos?`${seconds(p)-seconds(photos[pos-1])} 秒`:'首张'],['相机',p.camera],['ISO',p.iso],['光圈',`f/${p.aperture}`],['快门',`${p.shutter} s`],['源图尺寸',`${p.width} × ${p.height}`]];$('metadata').innerHTML=fields.map(([label,value])=>`<dt>${label}</dt><dd>${esc(value)}</dd>`).join('')}else $('metadata').innerHTML='';
  }
  function render(){
    finishHistory();modelRevision++;
    finishPickStroke(false);
    const m=model(),picked=m.items.filter(i=>i.r>0).length;
    $('engine-label').textContent=photos.some(p=>p.browserLive??Object.hasOwn(p,'liveRating'))?'现场标星':'时间规则';
    const page=session.browser?.paged?session.browser:null,total=page?.total??m.items.length,globalPicked=page?page.picked+picked-page.pagePicked:picked;
    $('selected-count').textContent=globalPicked;
    const matched=window.FramekeepPaging?.effective()?.matched??page?.matched;
    $('scope-label').textContent=page?'当前页':'全部照片';$('result-count').textContent=page?`${m.visible.length} / ${matched} 张`:`${m.visible.length} 张`;
    $('filter-all-count').textContent=total;$('filter-picked-count').textContent=globalPicked;$('filter-unpicked-count').textContent=total-globalPicked;
    $('undo').disabled=!history.length;$('threshold').value=threshold;$('threshold-value').textContent=`${threshold} 秒`;$('protect').checked=protectManual;
    document.querySelectorAll('[data-keep]').forEach(b=>{b.classList.toggle('active',Number(b.dataset.keep)===keep);b.setAttribute('aria-pressed',Number(b.dataset.keep)===keep)});
    document.querySelectorAll('[data-filter]').forEach(b=>{b.classList.toggle('active',b.dataset.filter===filter);b.setAttribute('aria-pressed',b.dataset.filter===filter)});
    document.querySelectorAll('[data-view]').forEach(b=>{b.classList.toggle('active',b.dataset.view===view);b.setAttribute('aria-pressed',b.dataset.view===view)});
    $('search').value=query;$('sort').value=sort;
    renderCatalog(m);if(view==='single')renderLoupe(m);syncSizeControl();renderSide(m);
    document.dispatchEvent(new CustomEvent('framekeep:selection',{detail:{currentGroup:m.current?.index??null}}));
  }
  function focusPhoto(id,open=false){
    selectedId=id;FramekeepViewer.reset();
    if(open){view='single';compare=false;render();return}
    const m=model();
    for(const container of ['photo-grid','photo-list'])$(container).querySelectorAll('[data-photo]').forEach(el=>el.classList.toggle('focused',el.dataset.photo===selectedId));
    $('photo-grid').querySelectorAll('[data-focus]').forEach(el=>el.setAttribute('aria-pressed',el.dataset.focus===selectedId));
    renderSide(m);if(view==='single')renderLoupe(m);
    document.dispatchEvent(new CustomEvent('framekeep:selection'));
  }
  function setRating(value,id=selectedId){
    finishPickStroke();
    const m=model(),item=m.items.find(i=>i.p.id===id);if(!item)return;
    save();selectedId=id;if(value<0)rejectedPrevious[id]=Math.max(0,item.r);else delete rejectedPrevious[id];overrides[id]=value;tagSku(id,value);render();
    notify(`${imageName(item.p)} · ${value<0?'暂不选':value+' 星'}`);
  }
  async function undo(){const prev=history.pop();if(!prev)return;if(prev.paged){if(!photos.some(p=>p.id===prev.selectedId)&&prev.selectedId&&!await FramekeepPaging.goto(prev.selectedId)){history.push(prev);return;}applyDelta(overrides,prev.overrides);applyDelta(rejectedPrevious,prev.rejectedPrevious,true);applyDelta(skuMarkers,prev.skuMarkers,true);({keep,threshold,protectManual,filter,view,selectedId,query,sort}=prev)}else({keep,threshold,protectManual,overrides,rejectedPrevious,skuMarkers,filter,view,selectedId,query,sort}=prev);compare=false;FramekeepViewer.reset();render();notify('已撤销')}
  function pickCard(point){
    const node=point&&document.elementFromPoint(point.x,point.y)?.closest('#photo-grid [data-photo],#photo-list [data-photo]');
    return node&&!node.closest('[inert]')?node:null;
  }
  function canPickStroke(){
    const target=document.activeElement;
    return !sessionLoading&&!interactionPaused&&view!=='single'&&!document.querySelector('dialog[open]')&&
      !$('catalog-view').closest('[inert],.hidden')&&!target?.isContentEditable&&
      !(target?.matches('textarea,select,input:not([type=checkbox]):not([type=button])'));
  }
  function refreshPickStroke(){
    if(!pickStroke.dirty.size)return;
    const items=pickStroke.items,page=session.browser?.paged?session.browser:null,localPicked=items.filter(item=>item.r>0).length,picked=page?page.picked+localPicked-page.pagePicked:localPicked;
    $('selected-count').textContent=picked;$('filter-picked-count').textContent=picked;
    $('filter-unpicked-count').textContent=(page?.total??items.length)-picked;$('undo').disabled=!history.length;
    for(const id of pickStroke.dirty){
      const item=pickStroke.byId.get(id),node=pickStroke.nodes.get(id);if(!item||!node)continue;
      node.querySelector('[data-pick]').checked=item.r>0;
      node.classList.toggle('picked',item.r>0);node.classList.toggle('excluded',item.r<0);
      const stars=node.querySelector('.card-rating');
      if(stars){stars.textContent=item.r>0?'★'.repeat(item.r):item.r<0?'×':'—';stars.classList.toggle('muted',item.r<=0)}
      const state=node.querySelector('.card-state');if(state)state.textContent=stateLabel(item);
      node.querySelectorAll('[data-star]').forEach(b=>b.classList.toggle('on',item.r>=Number(b.dataset.star)));
      const subtitle=node.querySelector('.list-subtitle');if(subtitle)subtitle.textContent=`${stateLabel(item)} · ${item.p.raw?(item.p.jpg?'RAW + JPG':'RAW'):'图片'}`;
    }
    pickStroke.dirty.clear();
    renderSide({current:pickStroke.byId.get(selectedId)});
  }
  function paintPickCard(node){
    if(!node||pickStroke.visited.has(node.dataset.photo))return;
    const id=node.dataset.photo,item=pickStroke.byId.get(id);if(!item)return;
    pickStroke.visited.add(id);pickStroke.nodes.set(id,node);
    if(pickStroke.add==null){pickStroke.add=item.r<=0;$('catalog-view').dataset.pickStroke=pickStroke.add?'add':'remove'}
    if((item.r>0)===pickStroke.add)return;
    if(!pickStroke.changed){save();pickStroke.before=history.at(-1)}
    const value=pickStroke.add?1:0;
    overrides[id]=value;delete rejectedPrevious[id];tagSku(id,value);item.r=value;pickStroke.changed++;pickStroke.dirty.add(id);
  }
  function beginPickStroke(){
    const m=model();Object.assign(pickStroke,{active:true,items:m.items,byId:new Map(m.items.map(item=>[item.p.id,item])),order:m.visible.map(item=>item.p.id),visited:new Set(),dirty:new Set(),nodes:new Map(),changed:0,add:null});
  }
  function finishPickStroke(redraw=true,cancel=false){
    const stroke=pickStroke;pickStroke=null;delete $('catalog-view').dataset.pickStroke;
    if(!stroke?.changed)return;
    if(!cancel)shiftHeld=false;
    if(cancel){
      ({overrides,rejectedPrevious,skuMarkers}=stroke.before);history.pop();
      stroke.items.forEach(item=>{item.r=rating(item.p,item.g,item.index)});
      stroke.dirty=new Set(stroke.visited);
      pickStroke=stroke;refreshPickStroke();pickStroke=null;
    }
    if(redraw){render();if(!cancel)notify(`已${stroke.add?'勾选':'取消勾选'} ${stroke.changed} 张 · Ctrl+Z 撤销`)}
  }
  document.addEventListener('pointermove',e=>{
    const next={x:e.clientX,y:e.clientY},previous=pickPointer;pickPointer=next;
    if(!shiftHeld||!e.shiftKey||e.ctrlKey||e.metaKey||e.altKey||(e.buttons&~1)||e.pointerType==='touch'||!canPickStroke()){
      finishPickStroke();return;
    }
    if(!pickStroke)pickStroke={origin:previous||next,last:previous||next,drag:!!(e.buttons&1)};
    const stroke=pickStroke;
    if(!stroke.active){
      if(Math.hypot(next.x-stroke.origin.x,next.y-stroke.origin.y)<5)return;
      beginPickStroke();
    }
    // 采样移动线段，快速划过时也覆盖经过的卡片，不依赖鼠标事件密度。
    const from=stroke.last,steps=Math.max(1,Math.ceil(Math.hypot(next.x-from.x,next.y-from.y)/12));
    for(let i=0;i<=steps;i++)paintPickCard(pickCard({x:from.x+(next.x-from.x)*i/steps,y:from.y+(next.y-from.y)*i/steps}));
    stroke.last=next;refreshPickStroke();if(stroke.drag)e.preventDefault();
  },true);
  document.addEventListener('pointerdown',e=>{
    suppressPickClick=false;pickPointer={x:e.clientX,y:e.clientY};
    if(e.button===0&&e.shiftKey&&pickCard(pickPointer)&&canPickStroke()){
      // Shift 单击仍用于备案范围；只有实际拖动才转为滑选。
      finishPickStroke(false,true);pickStroke={origin:pickPointer,last:pickPointer,drag:true};
    }else finishPickStroke();
  },true);
  document.addEventListener('pointerup',()=>{
    if(pickStroke?.drag){suppressPickClick=!!pickStroke.changed;finishPickStroke()}
  },true);
  for(const name of ['click','dblclick'])document.addEventListener(name,e=>{
    if(suppressPickClick&&e.target.closest('#catalog-view')){e.preventDefault();e.stopImmediatePropagation()}
  },true);
  $('catalog-view').addEventListener('dragstart',e=>{if(shiftHeld)e.preventDefault()});
  $('catalog-view').addEventListener('scroll',()=>{
    if(!pickStroke?.active||!shiftHeld||!canPickStroke())return;
    paintPickCard(pickCard(pickPointer));refreshPickStroke();
  },{passive:true});
  $('catalog-view').addEventListener('wheel',e=>{
    if(!shiftHeld||!e.shiftKey||e.ctrlKey||e.metaKey||e.altKey||!canPickStroke()||!pickCard(pickPointer))return;
    const delta=Math.abs(e.deltaY)>=Math.abs(e.deltaX)?e.deltaY:e.deltaX;
    if(!delta)return;e.preventDefault();
    if(!pickStroke)pickStroke={origin:pickPointer,last:pickPointer,drag:false};
    if(!pickStroke.active){beginPickStroke();paintPickCard(pickCard(pickPointer));refreshPickStroke()}
    $('catalog-view').scrollTop+=delta*(e.deltaMode===1?36:e.deltaMode===2?$('catalog-view').clientHeight:1);
  },{passive:false});
  document.addEventListener('keydown',e=>{
    if(e.key==='Shift'&&!e.repeat&&!shiftHeld){shiftHeld=true;if(canPickStroke()&&pickPointer)pickStroke={origin:pickPointer,last:pickPointer,drag:false}}
    else if(e.key==='Escape'&&pickStroke?.active){e.preventDefault();e.stopImmediatePropagation();shiftHeld=false;finishPickStroke(true,true)}
    else if(e.key!=='Shift')finishPickStroke();
  },true);
  document.addEventListener('keyup',e=>{if(e.key==='Shift'){shiftHeld=false;finishPickStroke()}},true);
  window.addEventListener('blur',()=>{shiftHeld=false;finishPickStroke()});
  document.addEventListener('pointercancel',()=>finishPickStroke());
  document.addEventListener('visibilitychange',()=>{if(document.hidden){shiftHeld=false;finishPickStroke()}});
  function changeRule(nextKeep,nextThreshold){if(nextKeep===keep&&nextThreshold===threshold)return;save();keep=nextKeep;threshold=nextThreshold;if(!protectManual){overrides={};rejectedPrevious={}}render()}
  function setView(next){view=next;FramekeepViewer.reset();if(next!=='single')compare=false;render()}
  function syncSizeControl(scale=FramekeepViewer.getScale(selectedId)){
    const large=view==='single',slider=$('thumb-size'),label=large?'大图':'缩略图',value=large?Math.round(scale*100):thumbSize;
    slider.min=large?100:110;slider.max=large?1600:300;slider.step=large?1:10;slider.value=value;
    $('thumb-size-value').textContent=large?`${value}%`:value;
    slider.setAttribute('aria-valuetext',large?`${value}%`:`${value} 像素`);
    document.querySelector('label[for="thumb-size"]').textContent=label;
    document.querySelector('.thumbnail-control').setAttribute('aria-label',`${label}大小`);
    $('thumb-smaller').setAttribute('aria-label',`缩小${label}`);$('thumb-larger').setAttribute('aria-label',`放大${label}`);
  }
  function setThumbSize(value){thumbSize=Math.max(110,Math.min(300,Math.round(value/10)*10));document.documentElement.style.setProperty('--thumb-size',`${thumbSize}px`);syncSizeControl()}
  function setImageSize(value){if(view==='single')FramekeepViewer.setScale(value/100);else setThumbSize(value)}
  function stepImageSize(delta){setImageSize(Number($('thumb-size').value)+delta)}
  document.addEventListener('framekeep:viewport',event=>{if(view==='single')syncSizeControl(event.detail?.scale)});
  function navigate(delta){if(session.browser?.paged&&window.FramekeepPaging?.pending){FramekeepPaging.boundary(delta);return}const m=model(),order=browseItems(m),index=order.findIndex(i=>i.p.id===selectedId),item=order[index+delta];if(!item){window.FramekeepPaging?.boundary(delta);return}if(monitoring){filter='all';query='';view='single'}selectedId=item.p.id;FramekeepViewer.reset();compare=false;render();ensureCatalogFocus(selectedId)}
  async function toggleCompare(){const item=model().current;if(!item||(item.p.browserGroupSize??item.g.length)<2)return;if(session.browser?.paged&&!compare){const sid=session.id,cid=selectedId;try{await FramekeepIO.flush();const data=await FramekeepIO.api('/api/selection/group-tail?'+new URLSearchParams({session_id:sid,capture_id:cid,keep,threshold}));if(session.id!==sid||selectedId!==cid||data.photos.length<2)return;const target=data.photos.at(-1).id;if(!photos.some(p=>p.id===target)&&!await FramekeepPaging.goto(target))return;compareTail=data.photos;selectedId=target;view='single';filter='all';compare=true;FramekeepViewer.reset();render()}catch(error){notify(error.message)}return}view='single';compare=!compare;compareTail=null;if(compare){filter='all';if(!item.g.slice(-2).some(p=>p.id===selectedId))selectedId=item.g.at(-1).id}FramekeepViewer.reset();render()}
  function manifest(){const m=model(),only=$('selected-only').checked;return {schema:'framekeep.selection',version:1,demo:true,createdAt:new Date().toISOString(),source:{folderName:session.name,pathMode:'relative',sampleCaptureCount:photos.length},rules:{method:'adjacent-time-gap',thresholdSeconds:threshold,keepLast:keep,protectManual},applyPolicy:{mode:only?'selected-only':'include-unselected',protectExistingRatingAtLeast:2,clearUnlisted:false,rejectDemoManifest:true},captures:m.items.filter(item=>!only||item.r>0).map(item=>({id:item.p.id,relativePaths:[item.p.raw,item.p.jpg].filter(Boolean),capturedAtLocal:item.p.time,group:item.index+1,rating:Math.max(0,item.r),excluded:item.r<0,decision:Object.hasOwn(overrides,item.p.id)?'manual':'tail-rule'}))}}
  async function renderExport(){const sourceSession=session.id;if(sessionLoading)throw new Error('正在切换文件夹，请稍后导出。');const data=window.FramekeepIO?await window.FramekeepIO.manifest($('selected-only').checked):manifest();if(session.id!==sourceSession||sessionLoading)throw new Error('文件夹已切换，请重新导出。');$('export-count').textContent=data.captures.filter(p=>p.rating>0).length;$('export-file-count').textContent=data.captures.reduce((n,p)=>n+p.relativePaths.length,0);$('export-mode-hint').textContent=$('selected-only').checked?'导出整个会话中已选照片，不受当前浏览筛选影响。':'包含整个会话及未选项（0 星）。';$('json-preview').textContent=JSON.stringify(data,null,2);return data}
  function info(title,content){$('info-title').textContent=title;$('info-content').textContent=content;$('info-dialog').showModal()}
  document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.view)));
  document.querySelectorAll('[data-filter]').forEach(b=>b.addEventListener('click',()=>{filter=b.dataset.filter;FramekeepViewer.reset();render()}));
  document.querySelectorAll('[data-keep]').forEach(b=>b.addEventListener('click',()=>changeRule(Number(b.dataset.keep),threshold)));
  $('threshold').addEventListener('input',e=>{$('threshold-value').textContent=`${e.target.value} 秒`});$('threshold').addEventListener('change',e=>changeRule(keep,Number(e.target.value)));
  $('protect').addEventListener('change',e=>{save();protectManual=e.target.checked;render()});
  $('search').addEventListener('input',e=>{query=e.target.value;render()});$('sort').addEventListener('change',e=>{sort=e.target.value;render()});
  $('thumb-size').addEventListener('input',e=>setImageSize(Number(e.target.value)));$('thumb-smaller').addEventListener('click',()=>stepImageSize(-20));$('thumb-larger').addEventListener('click',()=>stepImageSize(20));
  $('catalog-view').addEventListener('wheel',e=>{if(e.ctrlKey){e.preventDefault();setThumbSize(thumbSize+(e.deltaY>0?-10:10))}},{passive:false});
  for(const id of ['photo-grid','photo-list']){
    $(id).addEventListener('click',e=>{const star=e.target.closest('[data-star]'),pick=e.target.closest('[data-pick]'),focus=e.target.closest('[data-focus]');if(star)setRating(Number(star.dataset.star),star.dataset.for);else if(pick)setRating(pick.checked?1:0,pick.dataset.pick);else if(focus)focusPhoto(focus.dataset.focus);else{const card=e.target.closest('[data-photo]');if(card)focusPhoto(card.dataset.photo)}});
    $(id).addEventListener('dblclick',e=>{if(e.target.closest('input,[data-star]'))return;const card=e.target.closest('[data-photo]');if(card)focusPhoto(card.dataset.photo,true)});
  }
  $('folder-all')?.addEventListener('click',()=>render());
  $('selection-page').addEventListener('click',event=>{if(event.target.closest('[data-empty-clear]')){filter='all';query='';render()}});
  $('stars').addEventListener('click',e=>{const b=e.target.closest('[data-rating]');if(b)setRating(Number(b.dataset.rating))});
  $('reject').addEventListener('click',()=>{const item=model().current;if(item)setRating(item.r<0?(rejectedPrevious[item.p.id]??0):-1)});$('clear-rating').addEventListener('click',()=>setRating(0));$('undo').addEventListener('click',undo);
  $('previous').addEventListener('click',()=>navigate(-1));$('next').addEventListener('click',()=>navigate(1));
  $('photos').addEventListener('click',async e=>{const b=e.target.closest('[data-loupe]');if(!b)return;if(compare){const target=b.dataset.loupe,tail=compareTail;if(session.browser?.paged&&!photos.some(p=>p.id===target)){if(!await FramekeepPaging.goto(target))return;compare=true;compareTail=tail;view='single'}selectedId=target;render()}else FramekeepViewer.toggle(b.dataset.loupe)});
  $('help').addEventListener('click',()=>$('help-dialog').showModal());
  $('source-info').addEventListener('click',()=>info('样片来源',`当前为 2026-10-14 目录中的 32 张 JPG 缩小预览，使用实际 EXIF 时间。
相机：Canon EOS R6 Mark II。同名 CR3/JPG 合并为一个拍摄编号。
本阶段尚未实现任意目录扫描或 RAW 解码，未修改原图。`));
  $('raw-info').addEventListener('click',()=>info('RAW 兼容状态',`当前演示使用配对 JPG，尚未验证 RAW 读取。
正式版将优先提取 RAW 内嵌预览，按需完整解码；CR3 / NEF / ARW / RAF / RW2 / ORF / DNG 等需按机型样片验证。`));
  $('lr-info').addEventListener('click',()=>info('Lightroom 交接说明',`当前只能导出演示 JSON，Lightroom 不能原生读取它。
后续桥接流程：照片已导入 LR → 选择清单和对应目录 → 预览匹配 → 写入星级 → 按星级筛选。
桥接尚未实现。`));
  $('export-open').addEventListener('click',async()=>{try{await renderExport();$('export-dialog').showModal()}catch(e){notify(e.message)}});$('selected-only').addEventListener('change',()=>renderExport().catch(e=>notify(e.message)));
  $('download').addEventListener('click',async()=>{try{const data=await renderExport();if(window.FramekeepDesktop){const saved=await FramekeepDesktop.saveManifest(`${session.name}.framekeep.json`,data);if(saved)notify('选片清单已保存');return}const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download=`${session.name}${session.demo?'-demo':''}.framekeep.json`;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);notify('选片清单已生成')}catch(e){notify(e.message)}});
  document.querySelectorAll('dialog').forEach(dialog=>{dialog.querySelectorAll('.close,.close-action').forEach(b=>b.addEventListener('click',()=>dialog.close()));dialog.addEventListener('click',e=>{if(e.target!==dialog)return;const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dialog.close()})});
  document.addEventListener('keydown',e=>{
    if(sessionLoading||interactionPaused||document.querySelector('dialog[open]'))return;
    if(e.key==='Escape'&&view==='single'){
      e.preventDefault();setView('grid');
      ensureCatalogFocus(selectedId);const current=$('photo-grid').querySelector('.focused [data-focus]');
      current?.focus({preventScroll:true});current?.scrollIntoView({block:'nearest'});return;
    }
    if(/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)||e.target.isContentEditable)return;if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='z'){e.preventDefault();undo();return}if(e.ctrlKey||e.metaKey||e.altKey)return;const key=e.key.toLowerCase();if(/^[0-5]$/.test(key)){e.preventDefault();setRating(Number(key))}else if(key==='x'){$('reject').click()}else if(key==='g')setView('grid');else if(key==='l')setView('list');else if(key==='e'||key==='enter')setView('single');else if(key==='c')toggleCompare();else if(key==='arrowright'||key==='arrowleft'||monitoring&&['arrowup','arrowdown'].includes(key)){e.preventDefault();navigate(['arrowright','arrowdown'].includes(key)?1:-1)}});
  window.Framekeep={
    updateLiveCatalog(next,records,followId=null,enter=false){
      if(sessionLoading||next.id!==session.id)return false;
      session.snapshot=next.snapshot;session.browser=next.browser||null;photos.splice(0,photos.length,...records);
      if(enter||followId){
        if(enter)folderPrefix='';
        if(followId)selectedId=followId;
        filter='all';query='';view='single';compare=false;FramekeepViewer.reset();
      }
      render();return true;
    },
    remoteTarget:()=>({sessionId:session.id,photoId:model().current?.p.id??null,paused:sessionLoading||interactionPaused}),
    setInteractionPaused(value){if(value)finishPickStroke();interactionPaused=!!value;document.dispatchEvent(new CustomEvent('framekeep:selection'))},
    setMonitoring(value){if(monitoring===!!value)return;monitoring=!!value;if(monitoring){view='single';compare=false;filter='all';query=''}render()},
    syncSharedRatings(shared){overrides={...overrides,...shared};history.length=0;session.sharedRatings=shared;render()},
    browsePhoto(id){if(monitoring){filter='all';query=''}focusPhoto(id,true)},
    navigationOrder:()=>browseItems(model()).map(i=>i.p.id),
    photoItems(visibleOnly=false){const m=model();return(visibleOnly?browseItems(m):m.items).map(i=>({photo:i.p,rating:i.r}))},
    assignRemoteRating(id,value,skuContext=undefined){
      if(sessionLoading||!Number.isInteger(value)||value<0||value>5)return false;
      const item=model().items.find(i=>i.p.id===id);if(!item)return false;
      if(item.r===value)return true;
      const current=selectedId;save();overrides[id]=value;delete rejectedPrevious[id];tagSku(id,value,skuContext);selectedId=current;render();
      notify(`${imageName(item.p)} · ${value} 星`);return true;
    },
    incrementRating(id,skuContext=undefined){
      if(sessionLoading)return false;
      const item=model().items.find(i=>i.p.id===id);if(!item)return false;
      const next=Math.min(5,Math.max(0,item.r)+1);
      if(next===item.r){notify(`${imageName(item.p)} 已是五星`);return true}
      const current=selectedId;save();overrides[id]=next;delete rejectedPrevious[id];tagSku(id,next,skuContext);selectedId=current;render();
      notify(`${imageName(item.p)} · ${next} 星`);return true;
    },
    decrementRating(id,skuContext=undefined){
      if(sessionLoading)return false;
      const item=model().items.find(i=>i.p.id===id);if(!item)return false;
      if(item.r<=0){notify(`${imageName(item.p)} 已是零星`);return true}
      const next=Math.max(0,item.r-1),current=selectedId;
      save();overrides[id]=next;delete rejectedPrevious[id];tagSku(id,next,skuContext);selectedId=current;render();
      notify(`${imageName(item.p)} · ${next} 星`);return true;
    },
    loadSession(next,records){
      finishPickStroke(false);
      monitoring=false;
      Object.assign(session,next);session.browser=next.browser||null;window.FramekeepPaging?.reset();photos.splice(0,photos.length,...records);saved=recover(session);
      folderPrefix=saved.folderPrefix||'';
      keep=saved.keep||1;threshold=saved.threshold||4;protectManual=saved.protectManual!==false;overrides=saved.overrides||{};rejectedPrevious=saved.rejectedPrevious||{};
      skuMarkers=saved.skuMarkers||{};
      filter=['all','picked','unpicked'].includes(saved.filter)?saved.filter:'all';view=saved.view||'grid';
      selectedId=saved.selectedId||photos[0]?.id||null;query=saved.query||'';sort=saved.sort||'time-asc';thumbSize=saved.thumbSize||180;compare=false;FramekeepViewer.reset();history.length=0;
      setThumbSize(thumbSize);render();$('catalog-view').scrollTop=0;
    },
    loadPage(next,records,focus=null){
      finishPickStroke(false);finishHistory();Object.assign(session,next,{saved:next.state});photos.splice(0,photos.length,...records);
      for(const [key,target] of Object.entries({overrides,rejectedPrevious,skuMarkers})){
        const fresh={...next.state[key],...(key==='overrides'?next.sharedRatings:{})};
        for(const id of Object.keys(target))delete target[id];
        Object.assign(target,fresh);
      }
      ({keep,threshold,protectManual,filter,query,sort,folderPrefix}=Object.assign({keep:1,threshold:4,protectManual:true,filter:'all',query:'',sort:'time-asc',folderPrefix:''},next.state));
      selectedId=focus||records.find(p=>p.id===selectedId)?.id||records[0]?.id||null;compare=false;FramekeepViewer.reset();$('catalog-view').scrollTop=0;render();
    },
    setSessionLoading(value){if(value)finishPickStroke();sessionLoading=value;document.querySelectorAll('.library-main,.right-panel,#watch-browser').forEach(el=>{el.inert=value});for(const id of ['catalog-view','loupe-view'])$(id).setAttribute('aria-busy',String(value));render()},
    showAll(){folderPrefix='';render()},
    setFolderPrefix(value){finishPickStroke();folderPrefix=String(value||'').replaceAll('\\','/').replace(/^\/+|\/+$/g,'');filter='all';query='';compare=false;render();notify(folderPrefix?'正在浏览：'+folderPrefix:'正在浏览全部照片')},
    getKeep:()=>keep,getCurrentGroup:()=>model().current?.index??null,
    persistedState:()=>({keep,threshold,protectManual,overrides,rejectedPrevious,skuMarkers,scope:'all',filter,view,selectedId,query,sort,thumbSize,folderPrefix}),
    archiveOrder:()=>model().visible.map(i=>i.p.id),
    browseContext:()=>({session:session.id,filter,query,sort,folderPrefix}),
    getSnapshot:()=>({view,filter,scope:'all',selectedId,thumbSize,mode:'time',count:model().visible.length}),notify
  };
  if(location.hash)historyReplace();
  function historyReplace(){window.history.replaceState(null,'',location.pathname+location.search)}
  setThumbSize(thumbSize);render();
})();
