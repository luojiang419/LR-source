/* 缩放围绕手指中点，拖动限制在图片边缘；不改动原图片。 */
class InventoryPhotoViewer {
  constructor(dialog) {
    this.dialog=dialog;this.stage=dialog.querySelector('#photo-stage');this.image=dialog.querySelector('#full-photo');
    this.loading=dialog.querySelector('#photo-loading');this.label=dialog.querySelector('#photo-scale');
    this.minus=dialog.querySelector('#photo-zoom-out');this.plus=dialog.querySelector('#photo-zoom-in');this.fitButton=dialog.querySelector('#photo-fit');
    this.points=new Map();this.scale=1;this.x=0;this.y=0;this.ready=false;this.lastTap=null;this.lastTouch=-Infinity;
    this.minus.onclick=()=>this.zoom(this.scale/1.5);this.plus.onclick=()=>this.zoom(this.scale*1.5);this.fitButton.onclick=()=>this.fit();
    this.image.onload=()=>{if(this.dialog.open)this.layout(true)};
    this.image.onerror=()=>{this.ready=false;this.image.hidden=true;this.loading.hidden=false;this.loading.textContent='图片加载失败，请关闭后重试';this.controls()};
    this.stage.addEventListener('pointerdown',e=>this.down(e));this.stage.addEventListener('pointermove',e=>this.move(e));
    for(const name of ['pointerup','pointercancel','lostpointercapture'])this.stage.addEventListener(name,e=>this.up(e,name!=='pointerup'));
    this.stage.addEventListener('dblclick',e=>{e.preventDefault();if(e.pointerType!=='touch'&&performance.now()-this.lastTouch>600)this.toggle(this.local(e.clientX,e.clientY))});
    this.stage.addEventListener('wheel',e=>{e.preventDefault();if(this.ready)this.zoom(this.scale*Math.exp(-e.deltaY*.002),this.local(e.clientX,e.clientY))},{passive:false});
    this.dialog.addEventListener('keydown',e=>this.key(e));this.dialog.addEventListener('close',()=>{this.reset();this.image.removeAttribute('src')});
    this.resize=new ResizeObserver(()=>{if(this.dialog.open&&this.ready)this.layout(false)});this.resize.observe(this.stage);
  }
  open(src,title='图片预览') {
    this.reset();this.ready=false;this.image.hidden=true;this.loading.hidden=false;this.loading.textContent='正在加载图片…';this.controls();
    this.dialog.querySelector('#photo-title').textContent=title;this.image.alt=title;
    if(!this.dialog.open)this.dialog.showModal();this.image.src=src;
    if(this.image.complete&&this.image.naturalWidth)this.layout(true);
  }
  reset() {
    const ids=[...this.points.keys()];this.points.clear();
    for(const id of ids)try{if(this.stage.hasPointerCapture(id))this.stage.releasePointerCapture(id)}catch{}
    this.stage.classList.remove('is-interacting');this.gesture=null;this.lastTap=null;this.tapCandidate=false;this.scale=1;this.x=0;this.y=0;
    this.render();
  }
  layout(reset) {
    const w=this.stage.clientWidth,h=this.stage.clientHeight;if(!w||!h||!this.image.naturalWidth)return;
    const ratio=Math.min(w/this.image.naturalWidth,h/this.image.naturalHeight);
    this.width=this.image.naturalWidth*ratio;this.height=this.image.naturalHeight*ratio;
    Object.assign(this.image.style,{width:this.width+'px',height:this.height+'px',marginLeft:-this.width/2+'px',marginTop:-this.height/2+'px'});
    this.ready=true;this.image.hidden=false;this.loading.hidden=true;if(reset){this.scale=1;this.x=0;this.y=0}this.constrain();this.render();
  }
  local(x,y){const r=this.stage.getBoundingClientRect();return {x:x-r.left-r.width/2,y:y-r.top-r.height/2}}
  constrain(){const dx=Math.max(0,(this.width*this.scale-this.stage.clientWidth)/2),dy=Math.max(0,(this.height*this.scale-this.stage.clientHeight)/2);this.x=Math.max(-dx,Math.min(dx,this.x));this.y=Math.max(-dy,Math.min(dy,this.y))}
  controls(){this.minus.disabled=!this.ready||this.scale<=1.001;this.plus.disabled=!this.ready||this.scale>=7.999;this.fitButton.disabled=!this.ready;this.label.textContent=Math.round(this.scale*100)+'%'}
  render(){this.image.style.transform=`translate3d(${this.x}px,${this.y}px,0) scale(${this.scale})`;this.stage.classList.toggle('is-zoomed',this.scale>1.001);this.controls()}
  zoom(value,point={x:0,y:0}) {
    if(!this.ready)return;const scale=Math.max(1,Math.min(8,value)),ratio=scale/this.scale;
    this.x=point.x-(point.x-this.x)*ratio;this.y=point.y-(point.y-this.y)*ratio;this.scale=scale;this.constrain();this.render();
  }
  fit(){if(!this.ready)return;this.scale=1;this.x=0;this.y=0;this.lastTap=null;this.render()}
  toggle(point){if(this.scale>1.05)this.fit();else this.zoom(3,point)}
  begin() {
    const p=[...this.points.values()];if(p.length>=2){const a=this.local(p[0].x,p[0].y),b=this.local(p[1].x,p[1].y);this.gesture={kind:'pinch',distance:Math.max(1,Math.hypot(a.x-b.x,a.y-b.y)),scale:this.scale,anchorX:((a.x+b.x)/2-this.x)/this.scale,anchorY:((a.y+b.y)/2-this.y)/this.scale};this.tapCandidate=false;this.lastTap=null}
    else if(p.length===1)this.gesture={kind:'drag',startX:p[0].x,startY:p[0].y,x:this.x,y:this.y};else this.gesture=null;
  }
  down(e) {
    if(!this.ready||(e.pointerType==='mouse'&&e.button!==0))return;
    if(e.pointerType!=='mouse'){e.preventDefault();this.lastTouch=performance.now()}this.points.set(e.pointerId,{x:e.clientX,y:e.clientY});
    this.stage.setPointerCapture(e.pointerId);this.stage.classList.add('is-interacting');
    if(this.points.size===1){this.tapCandidate=true;this.tapStart={x:e.clientX,y:e.clientY,time:performance.now()}}
    this.begin();
  }
  move(e) {
    if(!this.points.has(e.pointerId))return;this.points.set(e.pointerId,{x:e.clientX,y:e.clientY});
    if(this.tapStart&&Math.hypot(e.clientX-this.tapStart.x,e.clientY-this.tapStart.y)>8)this.tapCandidate=false;
    const g=this.gesture;if(!g)return;
    if(g.kind==='pinch'&&this.points.size>=2){const p=[...this.points.values()],a=this.local(p[0].x,p[0].y),b=this.local(p[1].x,p[1].y);this.scale=Math.max(1,Math.min(8,g.scale*Math.hypot(a.x-b.x,a.y-b.y)/g.distance));this.x=(a.x+b.x)/2-g.anchorX*this.scale;this.y=(a.y+b.y)/2-g.anchorY*this.scale}
    else if(g.kind==='drag'){const p=[...this.points.values()][0];this.x=g.x+p.x-g.startX;this.y=g.y+p.y-g.startY}
    this.constrain();this.render();
  }
  up(e,cancelled) {
    if(!this.points.has(e.pointerId))return;
    const tap=!cancelled&&this.points.size===1&&this.tapCandidate&&e.pointerType!=='mouse'&&performance.now()-this.tapStart.time<350;
    this.points.delete(e.pointerId);try{if(this.stage.hasPointerCapture(e.pointerId))this.stage.releasePointerCapture(e.pointerId)}catch{}
    if(tap){const now=performance.now();if(this.lastTap&&now-this.lastTap.time<320&&Math.hypot(e.clientX-this.lastTap.x,e.clientY-this.lastTap.y)<40){this.toggle(this.local(e.clientX,e.clientY));this.lastTap=null}else this.lastTap={x:e.clientX,y:e.clientY,time:now}}
    if(cancelled)this.lastTap=null;if(!this.points.size){this.stage.classList.remove('is-interacting');this.tapCandidate=false}this.begin();
  }
  key(e) {
    if(!this.dialog.open||!this.ready)return;
    if(e.key==='+'||e.key==='='){e.preventDefault();this.zoom(this.scale*1.5)}
    else if(e.key==='-'){e.preventDefault();this.zoom(this.scale/1.5)}
    else if(e.key==='0'){e.preventDefault();this.fit()}
    else if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key)){e.preventDefault();this.x+=e.key==='ArrowRight'?-40:e.key==='ArrowLeft'?40:0;this.y+=e.key==='ArrowDown'?-40:e.key==='ArrowUp'?40:0;this.constrain();this.render()}
  }
}
