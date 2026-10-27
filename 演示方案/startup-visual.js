/* 内联同一份可信本地 SVG，使动画与减少动态效果偏好遵循当前窗口。 */
(() => {
  'use strict';
  const image=document.querySelector('.startup-art');
  if(!image)return;
  fetch('startup.svg').then(response=>response.ok?response.text():Promise.reject()).then(source=>{
    const svg=new DOMParser().parseFromString(source,'image/svg+xml').documentElement;
    if(svg.localName!=='svg')return;
    const artwork=document.importNode(svg,true);artwork.classList.add('startup-art');artwork.setAttribute('aria-hidden','true');
    image.replaceWith(artwork);
  }).catch(()=>{});
})();
