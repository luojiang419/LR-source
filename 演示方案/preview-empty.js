(() => {
  'use strict';
  const copy={
    loading:['正在加载照片','正在读取文件夹，照片准备好后将在这里显示'],
    waiting:['等待下一张精彩','正在监控照片传入目录，新照片就绪后自动显示'],
    folder:['从第一张照片开始','此文件夹暂无照片，可导入照片或开启联机拍摄'],
    filtered:['没有符合条件的照片','试试清除筛选，找回当前文件夹的照片']
  };
  window.FramekeepEmpty={
    render(host,{loading=false,monitoring=false,count=0}={}){
      const state=loading?'loading':count?'filtered':monitoring?'waiting':'folder';
      if(host.dataset.emptyState===state)return;
      host.dataset.emptyState=state;
      const [title,description]=copy[state];
      host.innerHTML=`<div class="preview-empty-content"><picture><source media="(prefers-reduced-motion:reduce)" srcset="assets/camera-transfer-still.svg"><img class="preview-empty-art" src="assets/camera-transfer.svg" alt="" aria-hidden="true"></picture><div class="preview-empty-status"><span aria-hidden="true"></span>${state==='waiting'?'照片目录监控中':state==='loading'?'正在准备':state==='filtered'?'调整筛选':'照片工作区'}</div><h2>${title}</h2><p>${description}</p>${state==='filtered'?'<button type="button" data-empty-clear>清除筛选</button>':''}</div>`;
    }
  };
})();
