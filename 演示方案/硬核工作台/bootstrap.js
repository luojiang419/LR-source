(() => {
  let mode='hardcore';
  try{mode=localStorage.getItem('framekeep-hardcore-preview-ui')||mode;}catch{}
  document.documentElement.dataset.hardcorePreview=String(mode==='hardcore');
})();
