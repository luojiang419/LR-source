(() => {
  'use strict';
  if(!window.FramekeepLayout)return;
  const page=document.getElementById('selection-page'),layout=page.querySelector('.library-layout');
  const storageKey='framekeep-sidebar-widths-v1',minimum={left:160,right:220},maximum=440;
  let widths={},drag=null,frame=0;
  try{const saved=JSON.parse(localStorage.getItem(storageKey)||'{}');for(const side of ['left','right'])if(Number.isFinite(saved?.[side]))widths[side]=Math.max(minimum[side],Math.min(maximum,saved[side]))}catch{}
  const panels={},handles={},actual={},limits={};
  function persist(){try{localStorage.setItem(storageKey,JSON.stringify(widths))}catch{}}
  function collapsed(side){return page.classList.contains(side+'-collapsed')}
  function position(){
    const origin=layout.getBoundingClientRect(),style=getComputedStyle(layout),gap=parseFloat(style.columnGap)||0;
    for(const side of ['left','right']){
      const box=panels[side].getBoundingClientRect(),handle=handles[side];
      const x=side==='left'?box.right-origin.left+gap/2:box.left-origin.left-gap/2;
      handle.style.left=`clamp(8px, ${x}px, calc(100% - 8px))`;
      handle.style.top=(box.top-origin.top)+'px';handle.style.height=box.height+'px';
    }
  }
  function apply(priority){
    const style=getComputedStyle(layout),grid=style.display==='grid'&&!page.classList.contains('hidden');
    for(const side of ['left','right'])handles[side].hidden=!grid||collapsed(side);
    if(!grid)return;
    const space=layout.clientWidth-(parseFloat(style.paddingLeft)||0)-(parseFloat(style.paddingRight)||0)-2*(parseFloat(style.columnGap)||0);
    const min={},size={},pageStyle=getComputedStyle(page);
    for(const side of ['left','right']){
      min[side]=collapsed(side)?24:minimum[side];
      size[side]=collapsed(side)?24:Math.max(min[side],Math.min(maximum,widths[side]??(parseFloat(pageStyle.getPropertyValue('--'+side+'-column'))||minimum[side])));
    }
    const budget=Math.max(min.left+min.right,space-320);
    if(size.left+size.right>budget){
      if(priority){
        const other=priority==='left'?'right':'left';
        size[other]=Math.min(size[other],budget-min[priority]);
        size[priority]=Math.max(min[priority],budget-size[other]);
      }else{
        const room=Math.max(0,budget-min.left-min.right),extra=size.left+size.right-min.left-min.right;
        for(const side of ['left','right'])size[side]=min[side]+(size[side]-min[side])*room/extra;
      }
    }
    for(const side of ['left','right']){
      actual[side]=Math.round(size[side]);page.style.setProperty('--sidebar-'+side+'-size',actual[side]+'px');
      limits[side]=Math.max(minimum[side],Math.min(maximum,budget-size[side==='left'?'right':'left']));
      handles[side].setAttribute('aria-valuenow',String(actual[side]));
      handles[side].setAttribute('aria-valuemax',String(Math.floor(limits[side])));
      handles[side].setAttribute('aria-valuetext',actual[side]+' 像素');
    }
    position();FramekeepLayout.refresh();
  }
  function setWidth(side,value){
    widths[side]=Math.max(minimum[side],Math.min(limits[side]||maximum,Math.round(value)));
    apply(side);
  }
  function finish(cancel=false){
    if(!drag)return;
    const previous=drag;drag=null;cancelAnimationFrame(frame);frame=0;
    if(cancel){widths=previous.before;apply()}else persist();
    document.body.classList.remove('sidebar-resizing');previous.handle.classList.remove('dragging');
    if(previous.handle.hasPointerCapture(previous.pointer))previous.handle.releasePointerCapture(previous.pointer);
  }
  for(const side of ['left','right']){
    panels[side]=layout.querySelector('.'+side+'-panel');
    const handle=document.createElement('div');handle.id=side+'-sidebar-resizer';handle.className='sidebar-resizer';handle.tabIndex=0;
    handle.setAttribute('role','separator');handle.setAttribute('aria-orientation','vertical');
    handle.setAttribute('aria-label','调节'+(side==='left'?'左':'右')+'侧栏宽度');
    handle.setAttribute('aria-controls',side+'-panel-body');handle.setAttribute('aria-valuemin',String(minimum[side]));
    handle.title='拖拽调节宽度 · 双击恢复默认 · 方向键微调';handles[side]=handle;layout.append(handle);
    handle.addEventListener('pointerdown',event=>{
      if(event.button!==0||handle.hidden||drag)return;
      event.preventDefault();handle.focus({preventScroll:true});
      drag={side,handle,pointer:event.pointerId,x:event.clientX,width:panels[side].getBoundingClientRect().width,before:{...widths},next:event.clientX};
      handle.setPointerCapture(event.pointerId);document.body.classList.add('sidebar-resizing');handle.classList.add('dragging');
    });
    handle.addEventListener('pointermove',event=>{
      if(!drag||event.pointerId!==drag.pointer)return;
      drag.next=event.clientX;if(frame)return;
      frame=requestAnimationFrame(()=>{frame=0;if(drag)setWidth(side,drag.width+(drag.next-drag.x)*(side==='left'?1:-1))});
    });
    handle.addEventListener('pointerup',event=>{
      if(!drag||event.pointerId!==drag.pointer)return;
      setWidth(side,drag.width+(event.clientX-drag.x)*(side==='left'?1:-1));finish();
    });
    handle.addEventListener('pointercancel',()=>finish(true));handle.addEventListener('lostpointercapture',()=>finish());
    handle.addEventListener('dblclick',()=>{delete widths[side];apply();persist()});
    handle.addEventListener('keydown',event=>{
      event.stopPropagation();if(!['ArrowLeft','ArrowRight','Home'].includes(event.key))return;
      event.preventDefault();
      if(event.key==='Home')delete widths[side];
      else setWidth(side,actual[side]+(event.key==='ArrowRight'?1:-1)*(side==='left'?1:-1)*(event.shiftKey?32:12));
      apply(side);persist();
    });
  }
  document.addEventListener('keydown',event=>{if(drag&&event.key==='Escape'){event.preventDefault();finish(true)}},true);
  window.addEventListener('blur',()=>finish());window.addEventListener('resize',()=>{finish();apply()});
  window.addEventListener('framekeep:theme',()=>apply());
  document.addEventListener('framekeep:workspace',()=>{finish();apply()});
  new MutationObserver(()=>{finish();apply()}).observe(page,{attributes:true,attributeFilter:['class']});
  const placement=new ResizeObserver(position);
  for(const element of [layout,panels.left,panels.right])placement.observe(element);
  apply();
})();
