(() => {
  'use strict';
  const session=window.FRAMEKEEP_SESSION,$=id=>document.getElementById(id);
  const bar=document.createElement('nav');bar.className='session-batchbar';bar.hidden=true;bar.setAttribute('aria-label','大目录分批浏览');
  bar.innerHTML='<span data-batch-range></span><button type="button" data-batch-prev>上一页</button><label>第 <input type="number" min="1" step="1" aria-label="跳转到页码"> 页</label><button type="button" data-batch-jump>跳转</button><button type="button" data-batch-next>下一页</button>';
  $('catalog-view').before(bar);
  let busy=false,timer=null,lastKey='',idleWaiters=[];
  const info=()=>session?.browser?.paged?session.browser:null;
  const options=()=>{const s=window.Framekeep?.persistedState()||session?.saved||{};return {filter:s.filter||'all',sort:s.sort||'time-asc',q:s.query||'',folder:s.folderPrefix||'',keep:s.keep||1,threshold:s.threshold||4}};
  function query(extra={}){return new URLSearchParams({session_id:session.id,...options(),offset:info()?.offset||0,...extra}).toString()}
  const key=()=>JSON.stringify(options());
  function effective(){const page=info();if(!page||!window.Framekeep)return page;const delta=Framekeep.photoItems().filter(i=>i.rating>0).length-page.pagePicked;return {...page,matched:Math.max(0,page.matched+(page.filter==='picked'?delta:page.filter==='unpicked'?-delta:0))}}
  function paint(){
    const page=effective();bar.hidden=!page;if(!page)return;
    const pending=busy||timer!==null,visible=window.Framekeep?Framekeep.photoItems(true).length:window.DEMO_PHOTOS.length;
    const first=page.matched?page.offset+1:0,last=Math.min(page.offset+visible,page.matched);
    bar.querySelector('[data-batch-range]').textContent=`${pending?'读取中… · ':''}${first}–${last} / ${page.matched} 张${page.matched!==page.total?' · 全目录 '+page.total+' 张':''}`;
    bar.querySelector('[data-batch-prev]').disabled=pending||page.offset===0;
    bar.querySelector('[data-batch-next]').disabled=pending||page.offset+page.limit>=page.matched;
    const input=bar.querySelector('input');if(document.activeElement!==input)input.value=Math.floor(page.offset/page.limit)+1;
    input.max=Math.max(1,Math.ceil(page.matched/page.limit));input.disabled=pending;bar.querySelector('[data-batch-jump]').disabled=pending;
  }
  async function load(extra={},focus=null){
    clearTimeout(timer);timer=null;
    if(!info()||busy||!window.FramekeepIO)return false;
    busy=true;paint();
    try{
      Framekeep.setSessionLoading(true);await FramekeepIO.flush();
      const data=await FramekeepIO.api('/api/state?'+query(extra));
      if(data.session?.id!==session.id)throw new Error('文件夹已切换，请重试');
      const target=focus==='step'?data.session.browser?.focusId||data.catalog[0]?.id:focus==='last'?data.catalog.at(-1)?.id:focus==='first'?data.catalog[0]?.id:focus;
      Framekeep.loadPage(data.session,data.catalog,target);FramekeepIO.adoptPage(data.session);lastKey=key();return true;
    }catch(error){Framekeep.notify('读取照片失败：'+error.message);return false}
    finally{Framekeep.setSessionLoading(false);busy=false;paint();const finished=idleWaiters;idleWaiters=[];finished.forEach(resolve=>resolve());FramekeepIO.flush().catch(error=>Framekeep.notify(error.message))}
  }
  function schedule(){
    paint();if(!info()||busy||!window.FramekeepIO)return;
    if(!lastKey)lastKey=JSON.stringify({filter:info().filter,sort:info().sort,q:info().query,folder:info().folderPrefix,keep:session.saved?.keep||1,threshold:session.saved?.threshold||4});
    if(key()!==lastKey){clearTimeout(timer);timer=setTimeout(()=>load({offset:0},'first'),180);paint()}
  }
  bar.querySelector('[data-batch-prev]').onclick=()=>load({offset:Math.max(0,info().offset-info().limit)},'first');
  bar.querySelector('[data-batch-next]').onclick=()=>load({offset:info().offset+info().limit},'first');
  function jump(){const n=Number(bar.querySelector('input').value);if(Number.isInteger(n)&&n>0)load({offset:(n-1)*info().limit},'first')}
  bar.querySelector('[data-batch-jump]').onclick=jump;bar.querySelector('input').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();jump()}};
  document.addEventListener('framekeep:selection',schedule);
  window.FramekeepPaging={info,effective,query,paint,get busy(){return busy},get pending(){return timer!==null},
    whenIdle(){clearTimeout(timer);timer=null;return busy?new Promise(resolve=>idleWaiters.push(resolve)):Promise.resolve()},
    saved(previous,current){if(!info()||busy||info().filter==='all'&&info().sort!=='rating-desc'||JSON.stringify(previous.overrides)===JSON.stringify(current.overrides))return;clearTimeout(timer);timer=setTimeout(()=>load({around:Framekeep.remoteTarget().photoId||''}),160);paint()},
    boundary(delta){const p=effective();if(!p)return false;load({around:Framekeep.remoteTarget().photoId||'',step:delta},'step');return true},
    goto(id){return load({around:id,filter:'all',q:'',folder:''},id)},
    reset(){clearTimeout(timer);timer=null;lastKey='';paint()}
  };
  const style=document.createElement('style');style.textContent='.session-batchbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:6px 10px;border-bottom:1px solid var(--theme-border);font-size:11px;background:var(--theme-panel)}.session-batchbar[hidden]{display:none}.session-batchbar>span{flex:1}.session-batchbar button{font-size:11px;border:1px solid var(--theme-border);padding:4px 7px}.session-batchbar input{width:65px;color:var(--theme-text);background:var(--theme-control);border:1px solid var(--theme-border);padding:3px}.virtual-spacer{pointer-events:none;grid-column:1/-1}.catalog-view{overflow-anchor:none}';document.head.append(style);
  paint();
})();
