/* 快捷搜索外观：数据库为准，localStorage 负责首帧与同源窗口即时同步。 */
(() => {
  'use strict';
  const KEY='framekeep-quick-appearance-v1',root=document.documentElement;
  const defaults={quick_mode:'full',quick_background:true,quick_blur:18,quick_brightness:75};
  const number=(v,f,max)=>Number.isFinite(Number(v))?Math.max(0,Math.min(max,Number(v))):f;
  let saved={...defaults},current;
  function normalize(value={}){return {
    quick_mode:value.quick_mode==='minimal'?'minimal':'full',quick_background:value.quick_background!==false,
    quick_blur:number(value.quick_blur,18,48),quick_brightness:number(value.quick_brightness,75,100)
  }}
  function apply(value,persist=false){
    current=normalize(value);root.dataset.quickMode=current.quick_mode;
    root.dataset.quickBackground=String(current.quick_background);
    root.style.setProperty('--quick-blur',current.quick_blur+'px');
    root.style.setProperty('--quick-brightness',current.quick_brightness/100);
    if(persist){saved={...current};try{localStorage.setItem(KEY,JSON.stringify(saved))}catch{}}
    window.dispatchEvent(new Event('framekeep:quick-appearance'));
  }
  try{saved=normalize(JSON.parse(localStorage.getItem(KEY)||'{}'))}catch{}
  apply(saved);
  window.FramekeepQuickAppearance={apply,get:()=>({...current}),reset:()=>apply(saved),
    setBackdrop(src){
      let layer=document.getElementById('quick-desktop-backdrop');
      if(!layer){layer=document.createElement('div');layer.id='quick-desktop-backdrop';layer.setAttribute('aria-hidden','true');document.body.prepend(layer)}
      layer.style.backgroundImage=src?`url("${src}")`:'';
    }
  };
  window.addEventListener('storage',event=>{if(event.key===KEY){try{saved=normalize(JSON.parse(event.newValue||'{}'));apply(saved)}catch{}}});
})();
