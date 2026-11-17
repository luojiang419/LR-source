/* 单行双向滑动操作：保留垂直滚动与普通图片点击。 */
class InventoryRecordSwipes {
  constructor(root,onAction){
    this.root=root;this.onAction=onAction;this.current=null;this.gesture=null;this.suppressUntil=0;
    root.addEventListener('pointerdown',e=>this.down(e));
    root.addEventListener('pointermove',e=>this.move(e));
    root.addEventListener('pointerup',e=>this.end(e));
    root.addEventListener('pointercancel',()=>this.cancel());
    root.addEventListener('lostpointercapture',e=>{if(this.gesture&&e.target===this.gesture.row)this.cancel()});
    root.addEventListener('dragstart',e=>e.preventDefault());
    root.addEventListener('click',e=>this.click(e),true);
    root.addEventListener('keydown',e=>{
      const row=e.target.closest('.record-swipe');if(!row)return;
      if(e.key==='Escape'){this.close();row.querySelector('.record-content').focus();e.preventDefault()}
      else if(e.target.matches('.record-content')&&['ArrowLeft','ArrowRight'].includes(e.key)){
        this.reveal(row,e.key==='ArrowLeft'?-1:1);row.querySelector('.record-action:not([hidden])').focus();e.preventDefault();
      }
    });
    document.addEventListener('pointerdown',e=>{if(this.current&&!this.current.contains(e.target))this.close()});
  }
  close(){if(this.current)this.position(this.current,0);this.current=null}
  reset(){this.gesture=null;this.close()}
  position(row,x){
    row.style.setProperty('--swipe-x',x+'px');row.dataset.side=x<0?'right':x>0?'left':'';
    for(const button of row.querySelectorAll('.record-action'))button.hidden=!x||button.dataset.side!==row.dataset.side;
    row.querySelector('.record-content').setAttribute('aria-expanded',String(!!x));
  }
  reveal(row,direction){this.close();this.current=row;this.position(row,direction*76)}
  down(e){
    if(!e.isPrimary){this.cancel();return}if(e.pointerType==='mouse'&&e.button!==0)return;
    const row=e.target.closest('.record-swipe');if(!row||e.target.closest('.record-action')||row.dataset.busy==='true')return;
    this.gesture={row,id:e.pointerId,x:e.clientX,y:e.clientY,base:this.current===row?(row.dataset.side==='right'?-76:76):0,axis:''};
  }
  move(e){
    const g=this.gesture;if(!g||g.id!==e.pointerId)return;
    const dx=e.clientX-g.x,dy=e.clientY-g.y;
    if(!g.axis&&Math.max(Math.abs(dx),Math.abs(dy))>10){
      g.axis=Math.abs(dx)>Math.abs(dy)*1.2?'x':'y';
      if(g.axis==='x'){if(this.current!==g.row)this.close();g.row.classList.add('swiping');g.row.setPointerCapture(e.pointerId)}
    }
    if(g.axis!=='x')return;
    e.preventDefault();this.suppressUntil=performance.now()+400;
    this.position(g.row,Math.max(-76,Math.min(76,g.base+dx)));
  }
  end(e){
    const g=this.gesture;if(!g||g.id!==e.pointerId)return;this.gesture=null;
    g.row.classList.remove('swiping');
    if(g.axis==='x'){
      const x=Math.max(-76,Math.min(76,g.base+e.clientX-g.x));
      if(Math.abs(x)>=32)this.reveal(g.row,Math.sign(x));else this.closeRow(g.row);
      this.suppressUntil=performance.now()+400;
    }
    if(g.row.hasPointerCapture(e.pointerId))g.row.releasePointerCapture(e.pointerId);
  }
  closeRow(row){this.position(row,0);if(this.current===row)this.current=null}
  cancel(){const g=this.gesture;this.gesture=null;if(g){g.row.classList.remove('swiping');this.closeRow(g.row);this.suppressUntil=performance.now()+400}}
  click(e){
    const button=e.target.closest('.record-action'),row=e.target.closest('.record-swipe');if(!row)return;
    if(button){e.preventDefault();e.stopImmediatePropagation();if(!button.disabled&&!button.hidden)this.onAction(row.dataset.id);return}
    if(performance.now()<this.suppressUntil||this.current===row){e.preventDefault();e.stopImmediatePropagation();if(performance.now()>=this.suppressUntil)this.close()}
  }
}
