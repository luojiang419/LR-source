// 仅由任务244验收服务器注入，产品入口不加载。
(() => {
  const events=[];let interaction=performance.now(),lastScroll=0,frameRunning=false;
  const push=(type,value={})=>events.push({type,t:performance.now(),...value});
  const flush=()=>{if(events.length)navigator.sendBeacon('/__perf244/report',JSON.stringify({href:location.pathname,events:events.splice(0),hidden:document.hidden}))};
  addEventListener('pointerdown',()=>{interaction=performance.now()},{capture:true});
  addEventListener('click',()=>{interaction=performance.now()},{capture:true});
  document.addEventListener('visibilitychange',()=>{push('visibility',{hidden:document.hidden});flush()});
  try{new PerformanceObserver(list=>{for(const item of list.getEntries())push('longtask',{duration:item.duration,start:item.startTime})}).observe({type:'longtask',buffered:true})}catch{}
  const scroll=()=>{
    lastScroll=performance.now();if(frameRunning)return;frameRunning=true;
    const frames=[];let previous=0;
    const frame=now=>{
      if(previous)frames.push(now-previous);previous=now;
      if(now-lastScroll<400&&!document.hidden)requestAnimationFrame(frame);
      else{frameRunning=false;push('scroll-frames',{frames,cardCount:document.querySelectorAll('#catalog-grid [data-catalog-item]').length});flush()}
    };
    requestAnimationFrame(frame);
  };
  addEventListener('scroll',scroll,{capture:true,passive:true});
  const attach=()=>{
    const grid=document.getElementById('catalog-grid');if(!grid)return false;
    let queued=false,previous=0,previousId='';
    new MutationObserver(()=>{
      if(queued)return;queued=true;
      requestAnimationFrame(()=>{
        queued=false;const count=grid.querySelectorAll('[data-catalog-item]').length;
        const firstId=grid.querySelector('[data-catalog-item]')?.dataset.catalogItem||'';
        if(count===previous&&firstId===previousId)return;
        push('cards',{count,first:previous===0||firstId!==previousId,interactionToFrame:performance.now()-interaction});previous=count;previousId=firstId;
        if(count===1500||count===6000)flush();
      });
    }).observe(grid,{childList:true,subtree:true});
    return true;
  };
  addEventListener('DOMContentLoaded',()=>{
    push('loaded',{webview:!!window.__TAURI__,userAgent:navigator.userAgent});
    if(!attach()){
      const observer=new MutationObserver(()=>{if(attach())observer.disconnect()});
      observer.observe(document.body,{childList:true,subtree:true});
    }
    flush();
  });
  setInterval(flush,3000);
})();
