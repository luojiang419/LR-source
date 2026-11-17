/* 现场目录采用完整记录与虚拟滚动，不创建分页导航。 */
(() => {
  'use strict';
  const style=document.createElement('style');
  style.textContent='.virtual-spacer{pointer-events:none;grid-column:1/-1}.catalog-view{overflow-anchor:none}';
  document.head.append(style);
})();
