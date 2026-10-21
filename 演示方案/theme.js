/* 界面主题独立保存，切换不访问业务接口，不触发评级或布局重建。 */
(() => {
  'use strict';
  const KEY='framekeep-theme-v1';
  const MODES=['light','dark','pure-white','hardcore'];
  const root=document.documentElement;
  const normalize=value=>MODES.includes(value)?value:'dark';
  let current='dark';
  try{current=normalize(localStorage.getItem(KEY));}catch{}
  function apply(value,persist=false){
    current=normalize(value);root.dataset.framekeepTheme=current;
    root.dataset.hardcoreLayout=String(current==='hardcore');
    for(const button of document.querySelectorAll('[data-framekeep-theme-choice]'))button.setAttribute('aria-pressed',String(button.dataset.framekeepThemeChoice===current));
    const label=document.getElementById('theme-current');if(label)label.textContent=({light:'象牙白',dark:'星夜黑','pure-white':'醇白',hardcore:'硬核工作台'})[current];
    if(persist)try{localStorage.setItem(KEY,current);}catch{}
    window.dispatchEvent(new Event('framekeep:theme'));
  }
  apply(current);
  window.FramekeepTheme={get:()=>current,set:value=>apply(value,true)};
  window.addEventListener('storage',event=>{if(event.key===KEY)apply(event.newValue);});
  document.addEventListener('DOMContentLoaded',()=>{
    const actions=document.querySelector('.header-actions');if(!actions)return;
    const button=document.createElement('button');button.id='theme-open';button.className='bordered';button.textContent='主题';button.type='button';button.setAttribute('aria-label','切换界面主题');button.setAttribute('aria-haspopup','dialog');
    actions.insertBefore(button,document.getElementById('help'));
    const dialog=document.createElement('dialog');dialog.id='theme-dialog';dialog.setAttribute('aria-labelledby','theme-title');
    dialog.innerHTML='<div class="dialog-heading"><h2 id="theme-title">界面主题</h2><button type="button" class="close" aria-label="关闭主题设置">×</button></div><p class="dialog-copy">选择适合当前工作环境的色彩。现有页面布局和选片设置保持不变。</p><div class="theme-options" role="group" aria-label="选择界面主题"><button type="button" class="theme-option" data-framekeep-theme-choice="light"><span class="theme-swatch" data-swatch="light" aria-hidden="true"></span><strong>象牙白</strong><span>温润米白 · 暖棕控件</span></button><button type="button" class="theme-option" data-framekeep-theme-choice="dark"><span class="theme-swatch" data-swatch="dark" aria-hidden="true"></span><strong>星夜黑</strong><span>中性深灰 · 柔和层次</span></button><button type="button" class="theme-option" data-framekeep-theme-choice="pure-white"><span class="theme-swatch" data-swatch="pure-white" aria-hidden="true"></span><strong>醇白</strong><span>纯净白底 · 黑灰控件</span></button></div><p class="dialog-copy">当前主题：<span id="theme-current"></span> · 选择后立即生效，自动记住。</p><div class="dialog-actions"><button type="button" class="primary" data-theme-done>完成</button></div>';
    dialog.querySelector('p.dialog-copy').textContent='选择界面主题，立即生效并自动保存。';
    const descriptions={light:'暖象牙 · 柔润磨砂',dark:'柔雾深灰 · 细腻高光','pure-white':'瓷白面板 · 轻盈投影'};
    for(const choice of dialog.querySelectorAll('[data-framekeep-theme-choice]'))choice.lastElementChild.textContent=descriptions[choice.dataset.framekeepThemeChoice];
    const hardcore=document.createElement('button');hardcore.type='button';hardcore.className='theme-option';hardcore.dataset.framekeepThemeChoice='hardcore';
    hardcore.innerHTML='<span class="theme-swatch hardcore-swatch" aria-hidden="true"></span><strong>硬核工作台</strong><span>中性深色 · 紧凑三栏</span>';
    dialog.querySelector('.theme-options').append(hardcore);
    document.body.append(dialog);apply(current);
    button.addEventListener('click',()=>dialog.showModal());
    dialog.addEventListener('click',event=>{
      const choice=event.target.closest('[data-framekeep-theme-choice]');if(choice)apply(choice.dataset.framekeepThemeChoice,true);
      if(event.target.closest('.close,[data-theme-done]'))dialog.close();
    });
  },{once:true});
})();
