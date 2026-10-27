(() => {
  'use strict';
  const stage=document.getElementById('photo-stage');
  let entries=[],signature='',drag=null,suppressedClick=null;
  const clamp=(value,min,max)=>Math.max(min,Math.min(max,value));

  function notify(entry){document.dispatchEvent(new CustomEvent('framekeep:viewport',{detail:entry?{id:entry.key,scale:entry.scale}:null}))}
  function constrain(entry){
    const limitX=Math.max(0,(entry.fitWidth*entry.scale-entry.width)/2);
    const limitY=Math.max(0,(entry.fitHeight*entry.scale-entry.height)/2);
    entry.x=clamp(entry.x,-limitX,limitX);entry.y=clamp(entry.y,-limitY,limitY);
  }
  function paint(entry){
    constrain(entry);
    entry.image.style.transform=`translate(-50%,-50%) translate(${entry.x}px,${entry.y}px) scale(${entry.scale})`;
    entry.button.classList.toggle('is-zoomed',entry.scale>1);
  }
  function layout(entry){
    if(!entry.button.isConnected||!entry.image.naturalWidth)return;
    entry.width=entry.button.clientWidth;entry.height=entry.button.clientHeight;
    if(!entry.width||!entry.height)return;
    const fit=Math.min(entry.width/entry.image.naturalWidth,entry.height/entry.image.naturalHeight);
    entry.fitWidth=entry.image.naturalWidth*fit;entry.fitHeight=entry.image.naturalHeight*fit;
    entry.image.style.width=`${entry.fitWidth}px`;entry.image.style.height=`${entry.fitHeight}px`;
    paint(entry);
  }
  function finishDrag(){
    if(!drag)return;
    const previous=drag;drag=null;
    previous.entry.button.classList.remove('panning');
    if(previous.button===0&&previous.moved)suppressedClick={key:previous.entry.key,until:performance.now()+400};
    if(previous.entry.button.hasPointerCapture(previous.id))previous.entry.button.releasePointerCapture(previous.id);
  }
  function reset(){
    finishDrag();suppressedClick=null;
    for(const entry of entries){entry.scale=1;entry.x=0;entry.y=0;paint(entry)}
    notify();
  }
  function refresh(){
    finishDrag();
    const buttons=[...stage.querySelectorAll('[data-loupe]')];
    const next=buttons.map(button=>button.dataset.loupe+'|'+button.querySelector('img').getAttribute('src')).join('\n');
    const previous=new Map(next===signature?entries.map(entry=>[entry.key,entry]):[]);
    signature=next;
    entries=buttons.map(button=>{
      const key=button.dataset.loupe,image=button.querySelector('img'),saved=previous.get(key);
      const entry={button,image,key,scale:saved?.scale||1,x:saved?.x||0,y:saved?.y||0,width:0,height:0,fitWidth:0,fitHeight:0};
      image.draggable=false;
      image.addEventListener('load',()=>{if(entries.includes(entry))layout(entry)},{once:true});
      return entry;
    });
    entries.forEach(layout);notify();
  }
  function entryFor(target){
    const button=target.closest('[data-loupe]');
    return entries.find(entry=>entry.button===button);
  }
  function zoom(entry,next,clientX,clientY){
    if(!entry)return;
    next=clamp(next,1,16);
    // 图片尚未载入时也保留滑杆缩放，载入后的 layout 会应用它。
    if(!entry.fitWidth){entry.scale=next;notify(entry);return}
    const rect=entry.button.getBoundingClientRect();
    const anchorX=(clientX??rect.left+rect.width/2)-rect.left-entry.width/2;
    const anchorY=(clientY??rect.top+rect.height/2)-rect.top-entry.height/2;
    const ratio=next/entry.scale;
    entry.x=anchorX-(anchorX-entry.x)*ratio;entry.y=anchorY-(anchorY-entry.y)*ratio;
    entry.scale=next;paint(entry);notify(entry);
  }
  stage.addEventListener('wheel',event=>{
    const entry=entryFor(event.target);if(!entry)return;
    event.preventDefault();finishDrag();
    const delta=event.deltaY*(event.deltaMode===1?16:event.deltaMode===2?entry.height:1);
    zoom(entry,entry.scale*Math.exp(-clamp(delta,-600,600)*.002),event.clientX,event.clientY);
  },{passive:false});
  stage.addEventListener('pointerdown',event=>{
    if(drag||![0,1].includes(event.button))return;
    const entry=entryFor(event.target);if(!entry?.fitWidth)return;
    event.preventDefault();suppressedClick=null;
    drag={id:event.pointerId,button:event.button,entry,startX:event.clientX,startY:event.clientY,x:entry.x,y:entry.y,moved:false};
    entry.button.setPointerCapture(event.pointerId);entry.button.classList.add('panning');
  });
  stage.addEventListener('pointermove',event=>{
    if(!drag||drag.id!==event.pointerId)return;
    if(!(event.buttons&(drag.button===0?1:4))){finishDrag();return}
    const dx=event.clientX-drag.startX,dy=event.clientY-drag.startY;
    if(!drag.moved&&Math.hypot(dx,dy)<3)return;
    drag.moved=true;drag.entry.x=drag.x+dx;drag.entry.y=drag.y+dy;
    paint(drag.entry);event.preventDefault();
  });
  for(const name of ['pointerup','pointercancel','lostpointercapture'])stage.addEventListener(name,event=>{
    if(drag?.id===event.pointerId)finishDrag();
  });
  stage.addEventListener('click',event=>{
    const entry=entryFor(event.target);
    if(event.detail&&entry&&suppressedClick?.key===entry.key&&performance.now()<suppressedClick.until){
      suppressedClick=null;event.preventDefault();event.stopImmediatePropagation();
    }
  },true);
  stage.addEventListener('auxclick',event=>{if(event.button===1)event.preventDefault()});
  stage.addEventListener('dragstart',event=>event.preventDefault());
  window.addEventListener('blur',finishDrag);
  new ResizeObserver(()=>{finishDrag();entries.forEach(layout)}).observe(stage);
  window.FramekeepViewer={
    refresh,reset,relayout(){signature=[...stage.querySelectorAll('[data-loupe]')].map(button=>button.dataset.loupe+'|'+button.querySelector('img').getAttribute('src')).join('\n');entries.forEach(layout)},
    setScale(scale){finishDrag();for(const entry of entries)zoom(entry,scale)},
    toggle(id){const entry=entries.find(item=>item.key===id)||entries[0];if(entry)zoom(entry,entry.scale>1?1:2)},
    getScale(id){return(entries.find(item=>item.key===id)||entries[0])?.scale||1}
  };
})();
