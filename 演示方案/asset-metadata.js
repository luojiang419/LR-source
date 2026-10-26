(() => {
  'use strict';
  if(!window.FramekeepIO)return;
  const $=id=>document.getElementById(id),api=FramekeepIO.api,esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const codeButtons=(codes,compact=false,pick=false)=>codes.map(code=>`<button type="button" class="asset-ocr-code${compact?' asset-ocr-code-compact':''}" ${pick?'data-ocr-pick':'data-ocr-code'}="${esc(code)}" title="${pick?'填入款号':'点击即应用款号'} ${esc(code)}" aria-pressed="false"><span class="asset-ocr-code-label">${esc(code)}</span><span class="asset-ocr-code-action">${pick?'填入款号':'点击应用'}</span></button>`).join('');
  const rawLines=text=>String(text||'').split(/\r?\n/).map(line=>line.trim()).filter(Boolean);
  window.FramekeepOcrUI={codeButtons,rawLines};
  const panel=document.createElement('details');panel.id='sku-asset-panel';panel.className='side-section asset-panel';panel.open=true;
  panel.innerHTML='<summary>款号备案 <span id="asset-count"></span></summary><div class="side-body asset-panel-body"><p class="dialog-copy">Shift 连选 · Ctrl 加选，与照片星级独立</p><label>SKU 款号<input id="asset-sku" maxlength="80" placeholder="识别后自动填入，也可批量修改"></label><button id="asset-preview" class="primary" aria-describedby="asset-write-help">写入 SKU 元数据</button><p id="asset-write-help" class="dialog-copy">推荐款号点击即写入整款；需要修改时，可在此编辑并更新整款 RAW / JPG。</p><p id="asset-message" role="status" class="dialog-copy"></p></div>';
  document.querySelector('.right-panel').append(panel);
  const ocrControls=document.createElement('div');ocrControls.className='asset-ocr-controls';ocrControls.innerHTML='<button id="asset-parse-sku" class="bordered" title="使用已选 OCR 模型读取当前图，不要求二星">OCR 解析当前图</button><div id="asset-ocr-result" class="asset-ocr-result" hidden><div id="asset-ocr-codes" class="asset-actions" role="group" aria-label="推荐款号，点击即应用"></div><details open><summary>OCR 原文</summary><div id="asset-ocr-text" class="asset-ocr-raw" role="group" aria-label="原文识别项，点击即应用"></div></details></div>';$('asset-sku').parentElement.after(ocrControls);
  const settingsButton=document.createElement('button');settingsButton.id='app-settings-open';settingsButton.textContent='设置';settingsButton.setAttribute('role','tab');settingsButton.setAttribute('aria-controls','app-settings-page');settingsButton.setAttribute('aria-selected','false');settingsButton.tabIndex=-1;document.querySelector('.workspace-tabs').append(settingsButton);
  const settings=document.createElement('section');settings.id='app-settings-page';settings.className='asset-workspace hidden';settings.setAttribute('role','tabpanel');settings.setAttribute('aria-labelledby','app-settings-open');
  settings.innerHTML=`<div class="asset-workspace-heading"><div><h2 id="app-settings-title">设置</h2><p>按功能管理模型、设备与拍摄工作流。</p></div><button id="app-settings-close" class="bordered">返回工作台</button></div><p id="app-settings-message" role="status" class="workspace-caption"></p>
  <div class="app-settings-layout"><nav class="app-settings-nav" role="tablist" aria-label="设置分类" aria-orientation="vertical">
    <span class="settings-nav-heading">识别与归档</span>
    <button type="button" data-settings-section="models" aria-controls="settings-models">AI 与模型</button>
    <button type="button" data-settings-section="sku-rules" aria-controls="settings-sku-rules">款号匹配规则</button>
    <button type="button" data-settings-section="files" aria-controls="settings-files">文件与备案</button>
    <button type="button" data-settings-section="streams" aria-controls="settings-streams">SKU 自动区间</button>
    <span class="settings-nav-heading">操作与显示</span>
    <button type="button" data-settings-section="remote" aria-controls="settings-remote">鼠标遥控</button>
    <button type="button" data-settings-section="preview" aria-controls="settings-preview">预览显示</button>
    <button type="button" data-settings-section="table" aria-controls="settings-table">表格显示</button>
    <span class="settings-nav-heading">数据与迁移</span>
    <button type="button" data-settings-section="library" aria-controls="settings-library">文件数据库与 API</button>
    <button type="button" data-settings-section="database" aria-controls="settings-database">数据库管理</button>
  </nav><div class="app-settings-grid">
  <section id="settings-library" class="app-settings-section" aria-label="文件数据库与 API"></section>
  <section id="settings-models" class="app-settings-section" aria-labelledby="settings-models-title"><div class="settings-section-heading"><h3 id="settings-models-title">AI 与模型</h3><p>管理识别能力与模型服务连接。</p></div>
    <div id="full-auto-ocr-card" class="mouse-auto-card"><div class="mouse-auto-heading"><div><span class="mouse-auto-eyebrow">逐图识别与入库</span><h4 id="full-auto-ocr-title">全自动 OCR 模式</h4></div><input id="full-auto-ocr" class="toggle-switch" type="checkbox" role="switch" aria-labelledby="full-auto-ocr-title" aria-describedby="full-auto-ocr-help" disabled></div><p id="full-auto-ocr-help">无需导入文档，打开照片目录即可启用。</p><div class="mouse-auto-footer"><span id="full-auto-ocr-status" role="status">正在读取配置…</span></div></div>
    <div class="workspace-card settings-service-card"><div class="settings-service-copy"><h4>AI 助手配置</h4><p>分别配置 OCR、多模态与文本模型，管理接口、密钥和当前选用的模型。</p><div class="settings-category-tags"><span>OCR</span><span>多模态</span><span>文本</span></div></div><div id="settings-models-action" class="settings-service-action"></div></div>
  </section>
  <section id="settings-remote" class="app-settings-section" aria-labelledby="settings-remote-title"><div class="settings-section-heading"><h3 id="settings-remote-title">鼠标遥控</h3><p>将无线鼠标用作拍摄现场的选片遥控器。</p></div>
    <div class="workspace-card settings-service-card"><div class="settings-service-copy"><h4>设备与按键功能</h4><p>选择接收器、启停遥控，设置右键减星与滚轮浏览。开启后自动返回选片页。</p><p class="settings-service-hint">紧急恢复：Ctrl + Alt + F11</p></div><div id="settings-remote-action" class="settings-service-action"></div></div>
  </section>
  <section id="settings-files" class="app-settings-section" aria-labelledby="settings-files-title"><div class="settings-section-heading"><h3 id="settings-files-title">文件与备案</h3><p>确认本次拍摄上下文，设置元数据写入及已有归属的处理方式。</p></div>
    <div class="workspace-card settings-service-card"><div class="settings-service-copy"><h4>拍摄文档</h4><p>导入和管理拍摄文档，绑定当前照片目录，查看各工作表的拍摄进度。</p></div><div class="settings-service-action"><button id="full-auto-open-plan" type="button" class="bordered">管理拍摄文档</button></div></div>
    <div class="settings-file-grid"><section class="workspace-card settings-context-card"><header><h4>当前拍摄</h4></header><div class="workspace-fields"><p id="settings-current-context" class="workspace-caption"></p><button id="settings-current-shooting" class="bordered">切换当前拍摄</button></div></section>
    <section class="workspace-card"><header><h4>元数据与归属</h4></header><div class="workspace-fields"><label>文件策略<select id="asset-mode"><option value="sidecar">RAW 使用 XMP 侧车，JPG 写入元数据</option></select></label><label class="watch-option"><input id="asset-overwrite" type="checkbox">明确覆盖已有不同归属</label><button id="asset-read" class="bordered">读取所选备案</button></div></section></div>
  </section>
  <section id="settings-streams" class="app-settings-section" aria-labelledby="settings-streams-title"><div class="settings-section-heading"><h3 id="settings-streams-title">SKU 自动区间</h3><p>按当前拍摄目录接续区间，或使用指定款号手工封口。</p></div>
    <div class="workspace-card"><div class="workspace-fields settings-stream-fields"><div class="settings-stream-start"><h4>接续当前目录</h4><p>继续本次拍摄的自动归组流程。</p><button id="asset-start" class="bordered">接续自动区间</button></div><div class="settings-stream-close"><label>手工封口款号<input id="asset-stream-sku" maxlength="80" placeholder="填写 SKU，或沿用选片页的款号"></label><button id="asset-close-stream" class="bordered">按所填 SKU 手工结束</button></div><p id="asset-stream-status" class="dialog-copy"></p></div></div>
  </section>
  <section id="settings-preview" class="app-settings-section" aria-labelledby="settings-preview-title"><div class="settings-section-heading"><h3 id="settings-preview-title">预览显示</h3><p>设置照片预览区的浮动控件。</p></div>
    <div class="workspace-card"><header><h4>视频封面</h4></header><form id="video-preview-form" class="workspace-fields"><label for="video-preview-frame">封面帧序号<input id="video-preview-frame" type="number" min="1" max="1000" step="1" value="3" required disabled aria-describedby="video-preview-help"></label><p id="video-preview-help" class="workspace-caption">默认使用第 3 帧，避开素材开头的黑屏；第 1 帧表示视频开头第一幅画面。支持 1–1000 帧，短于所选帧数时使用最后可解码帧。保存后重新打开素材列表即可更新封面，原视频保持不变。</p><button id="video-preview-save" class="bordered" type="submit" disabled>保存视频封面设置</button><p id="video-preview-status" role="status" class="workspace-caption">正在检查视频解码组件…</p><p id="video-preview-message" role="status" class="workspace-caption"></p></form></div>
    <div class="workspace-card"><header><h4>图片缓存</h4></header><form id="preview-cache-form" class="workspace-fields"><label for="preview-cache-limit">图片缓存上限（GB）<input id="preview-cache-limit" type="number" min="1" max="256" step="1" value="10" required aria-describedby="preview-cache-help" disabled></label><p id="preview-cache-help" class="workspace-caption">可设置 1–256 GB，监控与资产归档共用总额度。添加文件夹后并行读取或生成缩略图，重启后继续复用；容量不足时淘汰最久未使用的预览。</p><button id="preview-cache-save" class="bordered" type="submit" disabled>保存缓存上限</button><p id="preview-cache-state" class="workspace-caption" role="status">正在读取缓存…</p><p id="preview-cache-message" class="workspace-caption" role="status"></p></form></div>
    <div id="cache-storage-card" class="workspace-card"><header><h4>缓存数据库与迁移</h4></header><div class="workspace-fields"><p id="cache-storage-current" class="workspace-caption"></p><label for="cache-storage-path">缓存目标路径<input id="cache-storage-path" placeholder="选择独立的空文件夹，例如 E:\\FramekeepCache" spellcheck="false"></label><div class="asset-actions"><button id="cache-storage-browse" type="button" class="bordered">选择文件夹</button><button id="cache-storage-migrate" type="button" class="bordered">迁移缓存并切换路径</button><button id="cache-storage-clean" type="button" class="bordered" disabled>清理旧位置图片缓存</button></div><p class="workspace-caption">先复制并校验数据库与图片，成功后切换，重启保持；原位置保留恢复副本。建议使用本机高速 SSD，目标须为空且与照片、软件资料目录独立。确认迁移结果后可清理旧位置已登记的缓存图片；评级数据库、原照片与未知文件保留。</p><label for="cache-backup-folder">备份保存文件夹<input id="cache-backup-folder" placeholder="备份磁盘中的已有文件夹" spellcheck="false"></label><div class="asset-actions"><button id="cache-backup-browse" type="button" class="bordered">选择备份文件夹</button><button id="cache-backup-export" type="button" class="bordered">导出缓存数据库与图片</button></div><label for="cache-backup-file">缓存备份文件<input id="cache-backup-file" placeholder="末帧图片缓存-日期.framekeep-cache.zip" spellcheck="false"></label><div class="asset-actions"><button id="cache-backup-pick" type="button" class="bordered">选择备份文件</button><button id="cache-backup-import" type="button" class="bordered">导入并合并缓存</button><button id="cache-storage-prepare" type="button" class="bordered">补齐已有缓存的迁移指纹</button></div><p class="workspace-caption">换机时先导入缓存备份，再添加资产归档；也可先添加归档再导入缓存。按完整文件内容 SHA-256 匹配，不依赖盘符、目录或文件名；首次匹配需要读取文件内容，无需重新解码已匹配的预览。备份包含缩略图、预览及数据库清单，不包含原照片、评级、模型或密钥；业务元数据请在“数据库管理”另行备份。</p><p class="workspace-caption">文件规范：previews / catalog_previews 分类保存，64 位哈希文件名，数据库登记生成参数与 SHA-256；容量由总额度统一管理，未知文件不会自动删除。</p><p id="cache-storage-status" class="workspace-caption" role="status"></p></div></div>
    <div class="workspace-card"><div class="workspace-fields"><label for="preview-rating-position">星标位置<select id="preview-rating-position" aria-describedby="preview-rating-position-help"><option value="bottom-left">左下角（默认）</option><option value="top-left">左上角</option><option value="top-right">右上角</option><option value="bottom-right">右下角</option><option value="hidden">隐藏</option></select></label><p id="preview-rating-position-help" class="workspace-caption">调整大预览区星级控件的位置，立即生效并记忆。与缩放滑杆靠近时自动避让；隐藏后仍可用数字键标星。</p><label for="preview-rating-transparency">星标透明度<span class="preview-transparency-control"><input id="preview-rating-transparency" type="range" min="0" max="100" step="1" value="0" aria-describedby="preview-transparency-help"><output id="preview-rating-transparency-value" for="preview-rating-transparency">0%</output></span></label></div></div>
    <div class="workspace-card"><div class="workspace-fields"><label for="preview-zoom-position">缩放滑杆显示位置<select id="preview-zoom-position" aria-describedby="preview-zoom-position-help"><option value="hidden">隐藏（默认）</option><option value="top-left">左上角</option><option value="top-right">右上角</option><option value="bottom-left">左下角</option><option value="bottom-right">右下角</option></select></label><p id="preview-zoom-position-help" class="workspace-caption">选择位置后，移入照片预览区时显示。隐藏滑杆后仍可用滚轮缩放大图。</p><label for="preview-zoom-transparency">缩放滑杆透明度<span class="preview-transparency-control"><input id="preview-zoom-transparency" type="range" min="0" max="100" step="1" value="0" aria-describedby="preview-transparency-help"><output id="preview-zoom-transparency-value" for="preview-zoom-transparency">0%</output></span></label></div></div>
    <div class="workspace-card"><div class="workspace-fields"><label for="preview-histogram-position">直方图位置<select id="preview-histogram-position" aria-describedby="preview-histogram-position-help"><option value="bottom-right">右下角（默认）</option><option value="top-left">左上角</option><option value="top-right">右上角</option><option value="bottom-left">左下角</option><option value="hidden">隐藏</option></select></label><p id="preview-histogram-position-help" class="workspace-caption">选择直方图的位置，与星标、缩放滑杆及解析提示靠近时自动避让；设置立即生效并自动保存。</p><label for="preview-histogram-transparency">直方图透明度<span class="preview-transparency-control"><input id="preview-histogram-transparency" type="range" min="0" max="100" step="1" value="0" aria-describedby="preview-transparency-help"><output id="preview-histogram-transparency-value" for="preview-histogram-transparency">0%</output></span></label></div></div>
    <p id="preview-transparency-help" class="workspace-caption">数值越大越透明：0% 保持原有半透明外观，100% 完全透明。各项独立调整并自动保存，仅影响控件，不影响照片。</p>
  </section>
  <section id="settings-table" class="app-settings-section" aria-labelledby="settings-table-title"><div class="settings-section-heading"><h3 id="settings-table-title">表格显示</h3><p>设置拍摄进度与当前拍摄表格的线框，立即生效并自动保存。</p></div>
    <div class="workspace-card"><div class="workspace-fields"><label class="table-lines-switch" for="table-lines-visible"><span>显示表格线框</span><input id="table-lines-visible" class="toggle-switch" type="checkbox" role="switch" checked aria-describedby="table-lines-help"></label><label for="table-lines-transparency">表格线框透明度<span class="preview-transparency-control"><input id="table-lines-transparency" type="range" min="0" max="100" step="1" value="78" aria-describedby="table-lines-help"><output id="table-lines-transparency-value" for="table-lines-transparency">78%</output></span></label><p id="table-lines-help" class="workspace-caption">0% 为实线，100% 完全透明，仅改变线框，不影响文字、图片和状态底色。关闭线框后仍保留透明度设置。</p></div></div>
    <div class="workspace-card table-preview-card"><header><h4>效果预览</h4><p id="table-display-summary" class="workspace-caption" role="status"></p></header><div id="table-display-preview" tabindex="0" role="region" aria-label="表格线框效果预览"><table><thead><tr><th scope="col">序号</th><th scope="col">款号</th><th scope="col">拍摄状态</th><th scope="col">款号图片</th><th scope="col">拍摄完成时间</th></tr></thead><tbody><tr class="progress-shot"><td>1</td><td>SKU-001</td><td><span class="sku-state sku-shot">已拍摄</span></td><td><span class="table-preview-image">图片</span></td><td>2026-10-22 10:22</td></tr><tr><td>2</td><td>SKU-002</td><td><span class="sku-state">未拍摄</span></td><td>—</td><td>—</td></tr><tr><td>3</td><td>SKU-003</td><td><span class="sku-state">未拍摄</span></td><td>—</td><td>—</td></tr></tbody></table></div><p class="workspace-caption">示例数据 · 拖动上方滑杆查看线框变化</p></div>
  </section>
  <section id="settings-database" class="app-settings-section" aria-labelledby="settings-database-title"><div class="settings-section-heading"><h3 id="settings-database-title">数据库管理</h3><p>换机时先导入资产归档，再导入 SKU 数据库，自动同步元数据。</p></div>
    <div class="workspace-card sku-metadata-card"><p class="workspace-caption">包含全部款号、分组、已移除记录、OCR 资料、拍摄区间、照片索引、人工星级及拍摄文档，不受搜索和分页影响。原照片与同目录 XMP 文件请一起迁移；照片离线时仍保留款号、星级和元数据。更新星级后自动同步 XMP，JPG 同时写入内嵌评级。</p><div class="asset-actions"><button id="sku-metadata-import" type="button" class="bordered">导入元数据</button><button id="sku-metadata-export" type="button" class="bordered">导出元数据</button><input id="sku-metadata-file" type="file" accept=".json,application/json" hidden></div><p class="workspace-caption">在“资产归档”添加新机器照片目录，再导入备份。系统自动匹配照片，恢复款号、拍摄类型、项目、区间、星级和 OCR 资料，并同步写入照片。盘符和目录可以变化；不可靠匹配、不同人工修改和缺少的照片会列为异常。重复导入不重复添加，可继续同步新增归档。</p><p id="sku-metadata-message" class="workspace-caption" role="status" aria-live="polite"></p><p id="sku-metadata-sync-status" class="workspace-caption" role="status" aria-live="polite"></p><ul id="sku-metadata-sync-issues" class="workspace-caption"></ul></div>
  </section>
  <section id="settings-sku-rules" class="app-settings-section" aria-labelledby="settings-sku-rules-title"><div class="settings-section-heading"><h3 id="settings-sku-rules-title">款号匹配规则</h3><p>自动识别当前公司的款号，默认使用 LC 前缀。</p></div><div class="workspace-card"><p class="workspace-caption">前缀、后缀唯一命中后直接应用，无需点击款号。模糊包含用于快捷推荐；多个款号命中时保留人工选择。忽略大小写及全半角差异，款号需含数字；已有确认不会被覆盖。</p><div id="sku-rules-list"></div><div class="asset-actions"><button id="sku-rule-add" type="button">添加规则</button><button id="sku-rule-default" type="button">恢复 LC 默认规则</button><button id="sku-rule-save" type="button" class="primary">保存规则</button></div><p id="sku-rule-message" role="status" class="workspace-caption">正在读取规则…</p></div></section></div></div>`;
  document.querySelector('.workbench > .statusbar').before(settings);
  const lengthSettings=document.createElement('div');lengthSettings.className='sku-length-settings';
  lengthSettings.innerHTML='<h4>推荐款号字符数</h4><p class="workspace-caption">只推荐符合长度的款号。启用后，原文中的含数字编码也参与筛选；不符合的内容仅留在原文，可手动点击应用，也不会通过前后缀自动应用。</p><div class="workspace-fields"><label>字符数大于<input id="sku-length-min" type="number" min="0" max="79" step="1" placeholder="不限"></label><label>字符数小于<input id="sku-length-max" type="number" min="2" max="81" step="1" placeholder="不限"></label></div><p class="workspace-caption">两项同时填写时需同时满足。例：大于 5、小于 12，即 6–11 个字符。去除首尾空白、统一全半角后计数，内部连字符计入；留空表示不限。</p><button id="sku-length-clear" type="button" class="bordered">清除长度限制</button>';
  $('sku-rules-list').before(lengthSettings);
  function renderLengthFilter(value={}){$('sku-length-min').value=value.min_exclusive??'';$('sku-length-max').value=value.max_exclusive??''}
  lengthSettings.oninput=()=>{$('sku-rule-message').textContent='长度规则已修改，请保存'};
  $('sku-length-clear').onclick=()=>{renderLengthFilter();$('sku-rule-message').textContent='已清除长度限制草稿，请保存'};
  function renderSkuRules(rules){
    $('sku-rules-list').replaceChildren();
    for(const rule of rules)addSkuRule(rule);
  }
  function addSkuRule(rule={kind:'prefix',value:''}){
    const row=document.createElement('div');row.className='workspace-fields';
    row.innerHTML='<label>匹配方式<select aria-label="款号匹配方式"><option value="prefix">前缀 · 自动应用</option><option value="suffix">后缀 · 自动应用</option><option value="contains">模糊包含 · 推荐</option></select></label><label>匹配文字<input aria-label="款号匹配文字" maxlength="40" placeholder="例如 LC、P905"></label><button type="button" class="bordered">删除规则</button>';
    row.querySelector('select').value=rule.kind;row.querySelector('input').value=rule.value;
    row.querySelector('button').onclick=()=>{row.remove();$('sku-rule-message').textContent='规则已修改，请保存'};
    row.oninput=()=>{$('sku-rule-message').textContent='规则已修改，请保存'};$('sku-rules-list').append(row);
  }
  $('sku-rule-add').onclick=()=>addSkuRule();
  $('sku-rule-default').onclick=()=>{renderSkuRules([{kind:'prefix',value:'LC'}]);$('sku-rule-message').textContent='已恢复默认草稿，请保存'};
  $('sku-rule-save').onclick=async()=>{
    const controls=[...$('settings-sku-rules').querySelectorAll('button,input,select')];controls.forEach(e=>e.disabled=true);
    try{const rules=[...$('sku-rules-list').children].map(row=>({kind:row.querySelector('select').value,value:row.querySelector('input').value}));const limit=id=>{const input=$(id);if(input.validity.badInput||input.validity.stepMismatch)throw new Error('字符数须为整数');return input.value===''?null:Number(input.value)};const length_filter={min_exclusive:limit('sku-length-min'),max_exclusive:limit('sku-length-max')};const result=await api('/api/sku/rules',{rules,length_filter});renderSkuRules(result.rules);renderLengthFilter(result.length_filter);$('sku-rule-message').textContent='已保存；推荐立即按长度筛选，符合长度的前后缀唯一命中自动应用';try{await refreshOcrRecommendations()}catch(error){$('sku-rule-message').textContent='规则已保存；当前推荐刷新失败，请重新解析或打开 OCR 资料：'+error.message}}
    catch(error){$('sku-rule-message').textContent='保存失败：'+error.message}
    finally{controls.forEach(e=>e.disabled=false)}
  };
  $('sku-rule-save').disabled=true;
  api('/api/sku/rules').then(result=>{renderSkuRules(result.rules);renderLengthFilter(result.length_filter);$('sku-rule-save').disabled=false;$('sku-rule-message').textContent='规则已加载；删除全部前后缀规则可关闭自动匹配，长度筛选独立保存'}).catch(error=>{$('sku-rule-message').textContent='读取失败：'+error.message});
  let cacheSaving=false,cacheLoaded=false,cacheLoading=false;
  let videoPreviewSaving=false,videoPreviewLoaded=false;
  async function loadVideoPreview(){
    if(videoPreviewSaving)return;
    try{
      const result=await api('/api/video-preview');
      if(!videoPreviewLoaded){$('video-preview-frame').value=String(result.frame_number);videoPreviewLoaded=true}
      $('video-preview-status').textContent=result.message;
      $('video-preview-frame').disabled=false;$('video-preview-save').disabled=false;
    }catch(error){$('video-preview-message').textContent='读取视频封面设置失败：'+error.message}
  }
  $('video-preview-form').addEventListener('submit',async event=>{
    event.preventDefault();const input=$('video-preview-frame');if(videoPreviewSaving||!input.reportValidity())return;
    videoPreviewSaving=true;input.disabled=true;$('video-preview-save').disabled=true;
    try{
      const result=await api('/api/video-preview',{frame_number:Number(input.value)});
      input.value=String(result.frame_number);$('video-preview-message').textContent='已保存：使用第 '+result.frame_number+' 帧，重新打开素材列表后更新封面，重启后保持';
      $('video-preview-status').textContent=result.message;
      document.dispatchEvent(new CustomEvent('framekeep:video-preview',{detail:result}));
    }catch(error){$('video-preview-message').textContent='保存失败：'+error.message}
    finally{videoPreviewSaving=false;input.disabled=false;$('video-preview-save').disabled=false}
  });
  loadVideoPreview();
  document.addEventListener('framekeep:workspace',()=>{if(FramekeepWorkspace.current()==='settings')loadVideoPreview()});
  const discoveryStatus=document.createElement('p');discoveryStatus.id='preview-index-source';discoveryStatus.className='workspace-caption';discoveryStatus.setAttribute('role','status');$('preview-cache-state').after(discoveryStatus);
  const gpuStatus=document.createElement('p');gpuStatus.id='gpu-media-state';gpuStatus.className='workspace-caption';gpuStatus.setAttribute('role','status');
  const gpuCard=document.createElement('section');gpuCard.className='workspace-card';gpuCard.innerHTML='<header><h4>GPU 媒体加速</h4></header>';gpuCard.append(gpuStatus);$('settings-preview').querySelector('.settings-section-heading').after(gpuCard);
  let gpuChecked=0;
  async function loadGpuStatus(){
    try{
      const result=await api('/api/gpu-media'),image=result.images||{},video=result.video||{};
      const photos=image.ready?`照片 GPU 已就绪${image.device?' · '+image.device:''} · 已解码 ${image.jpeg_gpu||0} 张、缩放 ${image.resize_gpu||0} 张`:'照片使用 CPU 兼容处理';
      gpuStatus.textContent=photos+'；'+(video.ready?'视频 GPU 已就绪':'视频使用 CPU 兼容处理')+'。默认使用嵌入 JPEG 极速预览，最大 2048 像素。';
      gpuStatus.title=[image.last_error,video.error].filter(Boolean).join('\n');
    }catch{gpuStatus.textContent='GPU 状态暂不可读取，已保存的预览仍可使用。'}
  }
  let discoveryChecked=0;
  function cacheBytes(bytes){
    const units=['B','KB','MB','GB','TB'];let unit=0;
    while(bytes>=1024&&unit<units.length-1){bytes/=1024;unit++}
    return `${bytes.toFixed(unit?2:0)} ${units[unit]}`;
  }
  function paintCache(state){
    if(!cacheLoaded){$('preview-cache-limit').value=String(state.limit_gb);cacheLoaded=true}
    $('preview-cache-state').textContent=`已用 ${(state.bytes/1024**3).toFixed(2)} / ${state.limit_gb} GB · ${state.images} 份预览 · 待生成 ${state.pending}${state.failed?' · 失败 '+state.failed:''}`;
    if($('split-preview-cache-state'))$('split-preview-cache-state').textContent=`当前缓存：${state.images.toLocaleString('zh-CN')} 份图片 · 占用空间：${cacheBytes(state.bytes)}`;
    $('preview-cache-limit').disabled=cacheSaving;$('preview-cache-save').disabled=cacheSaving;
    paintCacheStorage(state);
  }
  let storageStarting=false,storageLastJob='',storageTracking=false;
  function paintCacheStorage(state){
    $('cache-storage-current').textContent=state.path?`当前缓存位置：${state.path} · 已登记迁移指纹 ${state.portable_images||0} 份预览${state.warning?' · '+state.warning:''}`:'当前服务未启用缓存迁移';
    const job=state.job||{},busy=storageStarting||job.status==='running';
    $('preview-cache-limit').disabled=busy||cacheSaving;$('preview-cache-save').disabled=busy||cacheSaving;
    if(job.action==='resize'&&job.status==='completed'&&!cacheSaving)$('preview-cache-limit').value=String(state.limit_gb);
    storageTracking=job.status==='running';
    for(const element of $('cache-storage-card').querySelectorAll('button,input'))element.disabled=busy||!state.path;
    $('cache-storage-clean').disabled=busy||!state.previous_path;
    if(job.id){
      const detail=job.file?` · 备份：${job.file}`:job.path?` · 新位置：${job.path}`:'';
      const counts=job.imported!==undefined?` · 已合并 ${job.imported}，内容复用 ${job.reused||0}，容量跳过 ${job.skipped||0}，文件冲突 ${job.conflicts||0}`:job.done?` · ${job.done}${job.total?' / '+job.total:''}`:'';
      $('cache-storage-status').textContent=(job.status==='failed'?'操作未完成：':'')+job.message+counts+detail+(job.unavailable?` · ${job.unavailable} 份缓存原图不可校验，已保留原缓存`: '');
      if(job.status==='completed'&&job.id!==storageLastJob){storageLastJob=job.id;document.dispatchEvent(new CustomEvent('framekeep:cache-imported'))}
    }
  }
  async function cacheOperation(action,path=''){
    if(storageStarting)return;storageStarting=true;$('cache-storage-status').textContent='正在启动缓存任务…';
    for(const element of $('cache-storage-card').querySelectorAll('button,input'))element.disabled=true;
    try{const state=await api('/api/preview-cache/manage',{action,path},120000);storageStarting=false;paintCacheStorage(state)}
    catch(error){storageStarting=false;$('cache-storage-status').textContent='操作失败：'+error.message;for(const element of $('cache-storage-card').querySelectorAll('button,input'))element.disabled=false}
  }
  async function chooseCacheFolder(id){
    try{if(!window.FramekeepDesktop?.chooseCacheFolder){$(id).focus();return}const path=await FramekeepDesktop.chooseCacheFolder();if(path)$(id).value=path}
    catch(error){$('cache-storage-status').textContent=error.message}
  }
  $('cache-storage-browse').onclick=()=>chooseCacheFolder('cache-storage-path');$('cache-backup-browse').onclick=()=>chooseCacheFolder('cache-backup-folder');
  $('cache-backup-pick').onclick=async()=>{try{if(!window.FramekeepDesktop?.chooseCacheBackup){$('cache-backup-file').focus();return}const file=await FramekeepDesktop.chooseCacheBackup();if(file)$('cache-backup-file').value=file}catch(error){$('cache-storage-status').textContent=error.message}};
  $('cache-storage-migrate').onclick=()=>cacheOperation('migrate',$('cache-storage-path').value);
  $('cache-backup-export').onclick=()=>cacheOperation('export',$('cache-backup-folder').value);
  $('cache-backup-import').onclick=()=>cacheOperation('import',$('cache-backup-file').value);
  $('cache-storage-prepare').onclick=()=>cacheOperation('prepare');
  $('cache-storage-clean').onclick=()=>cacheOperation('clean_previous');
  async function loadCache(){
    if(cacheSaving||cacheLoading)return;cacheLoading=true;
    try{const state=await api('/api/preview-cache');if(!cacheSaving)paintCache(state);
      if(Date.now()-gpuChecked>5000){gpuChecked=Date.now();loadGpuStatus()}
      if(Date.now()-discoveryChecked>15000){discoveryChecked=Date.now();api('/api/search/status').then(s=>{discoveryStatus.textContent=s.available?`Everything ${s.version} 已自动连接 · 新增文件夹优先加载索引候选，再扫描补齐`:`目录扫描模式 · ${s.message||'Everything 暂不可用'}；检测到可用索引后自动加速`}).catch(()=>{discoveryStatus.textContent='目录扫描模式 · 正常读取照片与已保存缓存'})}
    }
    catch(error){$('preview-cache-message').textContent='读取缓存失败：'+error.message;if($('split-preview-cache-state'))$('split-preview-cache-state').textContent='读取缓存失败：'+error.message}
    finally{cacheLoading=false}
  }
  $('preview-cache-form').addEventListener('submit',async event=>{
    event.preventDefault();const input=$('preview-cache-limit');if(cacheSaving||!input.reportValidity())return;
    cacheSaving=true;input.disabled=true;$('preview-cache-save').disabled=true;$('preview-cache-message').textContent='正在保存并整理缓存…';
    try{const result=await api('/api/preview-cache',{limit_gb:Number(input.value)},120000);const resizing=result.job?.action==='resize'&&result.job.status==='running';if(!resizing)input.value=String(result.limit_gb);paintCache(result);$('preview-cache-message').textContent=resizing?'正在后台分批整理，完成后应用新上限，其他缓存读取继续工作':'已保存，重启后保持'}
    catch(error){$('preview-cache-message').textContent='保存失败：'+error.message}
    finally{cacheSaving=false;input.disabled=false;$('preview-cache-save').disabled=false;loadCache()}
  });
  document.addEventListener('framekeep:cache-imported',()=>{
    for(const image of document.querySelectorAll('img[src*="/api/preview/"],img[src*="/api/catalog/preview/"]')){
      if(image.naturalWidth)continue;const url=new URL(image.src,location.href);url.searchParams.set('cache_revision',String(Date.now()));image.src=url.href;
    }
  });
  document.querySelector('[data-settings-section="library"]').addEventListener('click',loadCache);
  document.addEventListener('framekeep:workspace',()=>{if(FramekeepWorkspace.current()==='settings')loadCache()});
  const cacheTimer=setInterval(()=>{if(!document.hidden&&(storageTracking||!settings.classList.contains('hidden')&&(!$('settings-preview').hidden||!$('settings-library').hidden)))loadCache()},2000);
  window.addEventListener('pagehide',()=>clearInterval(cacheTimer),{once:true});loadCache();
  const tableDisplayKey='framekeep-table-display-v1',tableLines=$('table-lines-visible'),tableTransparency=$('table-lines-transparency');
  function applyTableDisplay(value){
    const transparency=value?.transparency,percent=typeof transparency==='number'&&Number.isFinite(transparency)&&transparency>=0&&transparency<=100?Math.round(transparency):78;
    tableLines.checked=typeof value?.visible==='boolean'?value.visible:true;tableTransparency.value=String(percent);$('table-lines-transparency-value').value=percent+'%';
    document.documentElement.style.setProperty('--shooting-grid-opacity',(tableLines.checked?100-percent:0)+'%');
    $('table-display-summary').textContent=tableLines.checked?`线框已显示 · 透明度 ${percent}%`:`线框已隐藏 · 保留透明度 ${percent}%`;
  }
  let tableDisplay;try{tableDisplay=JSON.parse(localStorage.getItem(tableDisplayKey))}catch{}applyTableDisplay(tableDisplay);
  function saveTableDisplay(){const value={visible:tableLines.checked,transparency:Number(tableTransparency.value)};applyTableDisplay(value);try{localStorage.setItem(tableDisplayKey,JSON.stringify(value))}catch{}}
  tableLines.addEventListener('change',saveTableDisplay);tableTransparency.addEventListener('input',saveTableDisplay);
  $('settings-models-action').append($('assistant-settings'));
  $('settings-remote-action').append($('mouse-remote'));
  $('settings-remote').querySelector('.settings-section-heading').after($('mouse-auto-card'));
  let fullAutoState=null,fullAutoSaving=false,fullAutoRequest=0,fullAutoDesired=false;
  function autoMessage(text){for(const id of ['full-auto-ocr-status','shooting-auto-status'])$(id).textContent=text}
  function paintFullAuto(state){
    fullAutoState=state;const bound=state.session_id===FRAMEKEEP_SESSION.id,busy=window.FramekeepSku?.isBusy();
    for(const id of ['full-auto-ocr','shooting-full-auto']){$(id).checked=fullAutoSaving?fullAutoDesired:state.enabled===true;$(id).disabled=fullAutoSaving||busy||(!state.can_enable||!bound)&&!state.enabled}
    for(const id of ['sku-import','shooting-import','sku-projects','sku-live-scope','sku-panel-scope','sku-bind-current','sku-auto'])$(id).disabled=fullAutoSaving||busy||(id==='sku-auto'&&!state.project_id);
    $('full-auto-ocr-card').classList.toggle('is-enabled',state.enabled===true);
    $('shooting-automation').classList.toggle('is-enabled',state.enabled===true);
    $('full-auto-ocr-help').textContent=state.reason||(!bound?'请先打开当前照片目录。':`${state.model_message?state.model_message+'。':''}${state.document} · ${state.scope}。${state.requires_sku_choice||state.without_document?"逐张保存 OCR 资料，无需导入文档；公司前后缀规则唯一命中自动应用，其他款号保留选择，辅助文字可搜索。":"逐张扫描已有和后续图片，精确或唯一高相似度匹配后自动入库；候选冲突和已有归属保留复核，照片星级保持。"}`);
    const reviewLabel=state.requires_sku_choice?'待选SKU':'待复核';const counts=state.counts||{},done=(counts.matched||0)+(counts.confirmed||0),pending=(counts.queued||0)+(counts.running||0)+(counts.waiting_model||0);
    $('full-auto-ocr-status').textContent=state.enabled?`${state.paused?'队列已暂停':state.model_ready===false?'等待选用模型':'全自动已开启'} · 已入库 ${done} · 待扫描 ${pending} · ${reviewLabel} ${counts.review||0} · 失败 ${counts.failed||0}`:state.reason||'全自动已关闭 · 无需文档即可开启';
    $('shooting-auto-status').textContent=state.enabled?`${state.paused?'队列已暂停':state.model_ready===false?'等待选用模型':'全自动已开启'} · 已入库 ${done} · 待扫描 ${pending} · ${reviewLabel} ${counts.review||0} · 失败 ${counts.failed||0}`:state.reason||(!bound?'请先绑定当前照片目录':'全自动已关闭 · 开启后逐图识别');
    if(fullAutoSaving)autoMessage('正在检查模型并切换全自动 OCR…');
  }
  async function loadFullAuto(){
    if(fullAutoState)paintFullAuto(fullAutoState);if(fullAutoSaving||window.FramekeepSku?.isBusy())return;
    const request=++fullAutoRequest;try{const next=await api('/api/sku/full-auto?'+new URLSearchParams({session_id:FRAMEKEEP_SESSION.id}));if(request===fullAutoRequest)paintFullAuto(next)}catch(error){if(request===fullAutoRequest)autoMessage(error.message)}
  }
  async function changeFullAuto(event){
    if(fullAutoSaving||window.FramekeepSku?.isBusy()){if(fullAutoState)paintFullAuto(fullAutoState);return}
    const enabled=event.target.checked,context={project_id:fullAutoState?.project_id,scope:fullAutoState?.scope,session_id:FRAMEKEEP_SESSION.id};
    fullAutoDesired=enabled;fullAutoSaving=true;++fullAutoRequest;if(fullAutoState)paintFullAuto(fullAutoState);let failure='';
    try{paintFullAuto(await api('/api/sku/full-auto',{enabled,...context}));document.dispatchEvent(new CustomEvent('framekeep:full-auto-ocr'))}
    catch(error){failure=error.message;Framekeep.notify(failure)}
    finally{fullAutoSaving=false;await loadFullAuto();if(failure)autoMessage(failure)}
  }
  for(const id of ['full-auto-ocr','shooting-full-auto'])$(id).onchange=changeFullAuto;
  window.FramekeepAutomation={refresh:loadFullAuto,isSaving:()=>fullAutoSaving};
  $('full-auto-open-plan').onclick=()=>FramekeepWorkspace.show('progress');
  for(const name of ['framekeep:workspace','framekeep:sku-project','framekeep:sku-context'])document.addEventListener(name,loadFullAuto);
  const fullAutoTimer=setInterval(()=>{if(!document.hidden&&(!settings.classList.contains('hidden')||!$('selection-page').classList.contains('hidden')))loadFullAuto()},2000);
  window.addEventListener('pagehide',()=>clearInterval(fullAutoTimer),{once:true});loadFullAuto();
  const settingsScroll=settings.querySelector('.app-settings-grid'),categoryButtons=[...settings.querySelectorAll('[data-settings-section]')];
  let metadataBusy=false;
  const metadataMessage=text=>{$('sku-metadata-message').textContent=text};
  let metadataSyncKey='';
  function paintMetadataSync(sync){
    const status=$('sku-metadata-sync-status'),issues=$('sku-metadata-sync-issues');
    if(!sync){status.textContent='';issues.replaceChildren();return}
    const r=sync.report||{},labels={queued:'准备同步归档元数据',running:'正在同步归档元数据',completed:'归档元数据已完整同步',attention:'归档同步有待处理项',waiting_archive:'请先导入资产归档，再重新导入同一元数据文件'};
    status.textContent=(labels[sync.status]||'归档同步')+(r.total!==undefined?` · 已同步 ${r.applied||0} / ${r.total} 张${r.queued?' · 待写入 '+r.queued:''}${r.review?' · 需复核 '+r.review:''}${r.failed?' · 失败 '+r.failed:''}${r.unmatched?' · 未匹配 '+r.unmatched:''}${r.missing_assets?' · 备份中 '+r.missing_assets+' 张尚未找到归档照片':''}`:'')+(r.error?'：'+r.error:'');
    issues.replaceChildren();issues.hidden=!(r.issues||[]).length;for(const issue of r.issues||[]){const line=document.createElement('li');line.textContent=issue.path+'：'+issue.reason;issues.append(line)}
    const key=sync.id+'|'+sync.status;
    if(metadataSyncKey!==key&&['completed','attention'].includes(sync.status)){FramekeepIO.refreshFolders().catch(()=>{});FramekeepIO.reloadRatings().catch(()=>{});window.FramekeepSkuLibrary?.refresh().catch(()=>{});document.dispatchEvent(new CustomEvent('framekeep:metadata-imported'))}
    metadataSyncKey=key;
  }
  async function loadMetadataSync(){try{const result=await api('/api/sku/metadata/sync',undefined,8000);paintMetadataSync(result.sync)}catch{}}
  const metadataSyncTimer=setInterval(()=>{if(!document.hidden&&!settings.classList.contains('hidden'))loadMetadataSync()},1500);
  window.addEventListener('pagehide',()=>clearInterval(metadataSyncTimer),{once:true});
  document.addEventListener('framekeep:workspace',()=>{if(!settings.classList.contains('hidden'))loadMetadataSync()});
  async function metadataAction(run){
    if(metadataBusy)return;metadataBusy=true;
    $('sku-metadata-import').disabled=$('sku-metadata-export').disabled=true;
    try{await FramekeepIO.flush();await run()}catch(error){metadataMessage('操作失败：'+error.message)}
    finally{metadataBusy=false;$('sku-metadata-import').disabled=$('sku-metadata-export').disabled=false}
  }
  $('sku-metadata-export').onclick=()=>metadataAction(async()=>{
    await FramekeepIO.flush();
    metadataMessage('正在导出全部 SKU 元数据…');
    const output=await api('/api/sku/metadata/export',{},120000);
    if(window.FramekeepDesktop?.saveManifest){
      const saved=await FramekeepDesktop.saveManifest(output.name,output.backup);
      metadataMessage(saved?'元数据已导出：'+output.name:'已取消导出');return;
    }
    const url=URL.createObjectURL(new Blob([JSON.stringify(output.backup)],{type:'application/json'})),link=document.createElement('a');
    link.href=url;link.download=output.name;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);
    metadataMessage('元数据已导出：'+output.name);
  });
  $('sku-metadata-import').onclick=()=>{if(!metadataBusy)$('sku-metadata-file').click()};
  $('sku-metadata-file').onchange=event=>{
    const file=event.target.files[0];event.target.value='';if(!file)return;
    metadataAction(async()=>{
      await FramekeepIO.flush();
      if(file.size>200000000)throw new Error('元数据文件不能超过 200 MB');
      metadataMessage('正在校验并导入元数据…');
      const content=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=()=>reject(new Error('无法读取元数据文件'));reader.readAsDataURL(file)});
      const result=await api('/api/sku/metadata/import',{content_base64:content},120000);
      metadataMessage(result.already_imported?'该备份已经导入，无需重复添加':`元数据导入完成：新增 ${result.records} 条记录、${result.projects} 份拍摄文档。可打开 SKU 数据库查看。`);
      if(result.sync){paintMetadataSync(result.sync);metadataMessage(result.already_imported?'数据库已加载，继续核对本机归档同步':'数据库导入完成，已开始自动同步本机资产归档')}
      FramekeepIO.refreshFolders().catch(()=>{});window.FramekeepSkuLibrary?.refresh().catch(()=>{});
      document.dispatchEvent(new CustomEvent('framekeep:metadata-imported'));
    });
  };
  const settingsCategoryKey='framekeep-settings-category-v1';
  let activeSettingsCategory='';
  function showSettingsCategory(id){
    if(!categoryButtons.some(button=>button.dataset.settingsSection===id))id='models';
    if(id!==activeSettingsCategory)$('app-settings-message').textContent='';
    activeSettingsCategory=id;
    for(const button of categoryButtons){
      const selected=button.dataset.settingsSection===id,panel=$(button.getAttribute('aria-controls'));
      button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;panel.hidden=!selected;
      if(selected)button.setAttribute('aria-current','location');else button.removeAttribute('aria-current');
    }
    settingsScroll.scrollTop=0;
    try{localStorage.setItem(settingsCategoryKey,id)}catch{}
  }
  for(const [index,button] of categoryButtons.entries()){
    const id=button.dataset.settingsSection,panel=$('settings-'+id);button.id='settings-tab-'+id;button.setAttribute('role','tab');
    panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby',button.id);panel.tabIndex=0;
    button.addEventListener('click',()=>showSettingsCategory(id));
    button.addEventListener('keydown',event=>{
      const step={ArrowDown:1,ArrowRight:1,ArrowUp:-1,ArrowLeft:-1}[event.key];
      if(step===undefined&&!['Home','End'].includes(event.key))return;
      event.preventDefault();event.stopPropagation();
      const next=event.key==='Home'?0:event.key==='End'?categoryButtons.length-1:(index+step+categoryButtons.length)%categoryButtons.length;
      categoryButtons[next].click();categoryButtons[next].focus();
    });
  }
  let savedSettingsCategory;try{savedSettingsCategory=localStorage.getItem(settingsCategoryKey)}catch{}showSettingsCategory(savedSettingsCategory);
  const settingsNarrow=matchMedia('(max-width:700px)'),settingsOrientation=()=>settings.querySelector('.app-settings-nav').setAttribute('aria-orientation',settingsNarrow.matches?'horizontal':'vertical');
  settingsNarrow.addEventListener('change',settingsOrientation);settingsOrientation();
  let settingsReturn='selection';
  document.addEventListener('framekeep:workspace',event=>{if(event.detail.page!=='settings')settingsReturn=event.detail.page});
  function settingsContext(){const type=window.FramekeepSku?.currentType()||'尚未选择';$('settings-current-context').textContent=`当前拍摄：${type} · 所选 ${selection().length} 张\n${FRAMEKEEP_SESSION.root||'尚未打开照片目录'}`}
  function syncShootingTypes(){previewId=null;if(preview.open)preview.close();settingsContext()}
  document.addEventListener('framekeep:sku-project',syncShootingTypes);
  document.addEventListener('framekeep:shooting-type',syncShootingTypes);
  $('app-settings-close').onclick=()=>FramekeepWorkspace.show(settingsReturn);
  $('settings-current-shooting').onclick=()=>{FramekeepWorkspace.show('selection');window.FramekeepSidebar?.show('shooting')};
  const preview=document.createElement('dialog');preview.id='asset-preview-dialog';preview.setAttribute('aria-labelledby','asset-write-title');preview.innerHTML='<div class="dialog-heading"><h2 id="asset-write-title">修改整款 SKU 元数据</h2><button data-close aria-label="关闭编辑">×</button></div><p class="dialog-copy">可直接修改款号，更新后写入下方整款图片的 RAW / JPG，无需再次确认。</p><label class="field-label" for="asset-group-sku">SKU 款号</label><input id="asset-group-sku" maxlength="80" autocomplete="off" placeholder="输入正确的 SKU 款号"><div id="asset-preview-content"></div><p id="asset-group-message" class="dialog-copy" role="status"></p><div class="dialog-actions"><button data-close>取消</button><button id="asset-apply" class="primary">更新整款元数据</button></div>';document.body.append(preview);let groupEdit=null,groupWriting=false;
  const archiveTab=document.createElement('button');archiveTab.id='asset-archive-open';archiveTab.textContent='资产归档';archiveTab.setAttribute('role','tab');archiveTab.setAttribute('aria-selected','false');archiveTab.setAttribute('aria-controls','asset-archive-dialog');archiveTab.tabIndex=-1;document.querySelector('.workspace-tabs').append(archiveTab);
  const archive=document.createElement('section');archive.id='asset-archive-dialog';archive.className='asset-workspace hidden';archive.setAttribute('role','tabpanel');archive.setAttribute('aria-labelledby',archiveTab.id);
  archive.innerHTML=`<div class="asset-workspace-heading"><div><h2>资产归档</h2><p>按款号整理拍摄资产，追踪文件位置与元数据写入。</p></div><button id="archive-export" class="bordered">导出当前页 JSON</button></div>
  <div class="asset-workspace-grid"><main class="asset-workspace-main">
    <div class="workspace-metrics"><div><span>本页资产</span><strong id="archive-total">0</strong></div><div><span>最近区间</span><strong id="archive-sealed">0</strong></div><div><span>迟到待确认</span><strong id="archive-late">0</strong></div><div><span>最近写入批次</span><strong id="archive-batches">0</strong></div></div>
    <section class="workspace-card workspace-result-card"><header><h3>资产清单</h3><span id="archive-page" class="workspace-caption"></span></header><div id="archive-results" class="sku-table-wrap"></div><footer class="workspace-pagination"><button id="archive-prev">上一页</button><button id="archive-next">下一页</button></footer></section>
    <section id="archive-inspector" class="workspace-card" hidden><header><h3>资产与写入详情</h3><button id="archive-detail-close" aria-label="收起详情">×</button></header><div id="archive-detail-summary" class="workspace-detail"></div><details class="workspace-raw"><summary>完整记录</summary><pre id="archive-detail"></pre></details></section>
  </main><aside class="asset-workspace-sidebar" aria-label="资产检索与归档工具">
    <section class="workspace-card"><header><h3>检索资产</h3><span class="workspace-caption">SKU / 类型</span></header><div class="workspace-fields"><label>SKU 款号<input id="archive-sku" type="search" placeholder="输入款号查找"></label><div class="workspace-field-pair"><label>匹配方式<select id="archive-match"><option value="prefix">款号前缀</option><option value="exact">精确款号</option></select></label><label>拍摄类型<input id="archive-scope" placeholder="完整类型名称"></label></div><details><summary>项目筛选</summary><label>项目 ID<input id="archive-project" placeholder="可选"></label></details><button id="archive-search" class="primary">查询资产</button><p id="archive-message" role="status"></p></div></section>
    <details class="workspace-card"><summary>文件定位与重新归档</summary><div class="workspace-fields"><p id="everything-status" class="dialog-copy">正在检查文件搜索服务…</p><label>文件名或路径<input id="file-query" placeholder="查找已索引的图片位置"></label><div class="asset-actions"><button id="file-search">查找文件</button><button id="file-next" disabled>更多文件</button></div><p id="file-search-message" role="status"></p><div id="file-search-results" class="sku-table-wrap"></div><label>移动后的目录<input id="archive-folder" placeholder="粘贴目录完整路径"></label><button id="archive-relocate">扫描并重新定位</button></div></details>
    <details class="workspace-card" open><summary>SKU 区间</summary><div id="archive-segments" class="workspace-activity"></div></details>
    <details class="workspace-card"><summary>元数据写入批次</summary><div id="archive-operations" class="workspace-activity"></div></details>
  </aside></div>`;document.querySelector('.workbench > .statusbar').before(archive);
  $('archive-detail-close').onclick=()=>{$('archive-inspector').hidden=true};
  const visible=()=>window.FramekeepWorkspace?.current()==='archive';
  const statusName=value=>({confirmed:'已确认',waiting_sku:'等待款号',needs_review:'待复核',cancelled:'已撤销',ready:'待写入',queued:'排队中',running:'写入中',completed:'已完成',finished:'已结束',applied:'已写入',failed:'失败',partial:'部分完成',assign:'备案写入',revert:'撤销写入',online:'在线',product:'产品图',sku_label:'款号标签'}[value]||value);
  function empty(title,copy){return `<div class="workspace-empty"><strong>${title}</strong><p>${copy}</p></div>`}
  function detail(data){
    $('archive-inspector').hidden=false;$('archive-detail').textContent=JSON.stringify(data,null,2);
    $('archive-detail-summary').innerHTML=data.sku!==undefined?`<strong>${esc(data.sku||'待备案')}</strong><p>${esc(data.scope||'未填写类型')} · ${esc(statusName(data.role))}</p>${(data.files||[]).map(f=>`<p>${esc(f.path)}<br><small>${esc(f.component)}</small></p>`).join('')}`:`<strong>${esc(statusName(data.operation?.status||data.status||''))}</strong><p>${(data.jobs||[]).length} 个文件任务</p>${(data.jobs||[]).map(j=>`<p>${esc(statusName(j.status))} · ${esc(j.path)}${j.reason?'<br>'+esc(j.reason):''}</p>`).join('')}`;
  }
  let selected=[],anchor=null,lastFocus=null,lastContext='',previewId=null,operation=null,stream=null,busy=false,queryRows=[];
  let skuEdited=false,selectionRead='',archiveVersion='',readSequence=0,skuEditVersion=0,parsing=false,ocrContext='',ocrSnapshot=null,ocrApplying=false;
  let reviewKey='',reviewRequest=null;
  $('asset-sku').addEventListener('input',()=>{skuEdited=true;skuEditVersion++;previewId=null});
  syncShootingTypes();
  let cursors=[''],pageIndex=0,nextCursor='',fileOffset=0,fileText='';
  function resetPage(){cursors=[''];pageIndex=0;nextCursor=''}
  const msg=text=>{$('asset-message').textContent=text;$('app-settings-message').textContent=text};
  async function act(fn){if(busy)return;busy=true;try{await fn()}catch(e){msg(e.message);const target=$('archive-message');if(target)target.textContent=e.message;Framekeep.notify(e.message)}finally{busy=false}}
  function current(){return Framekeep.remoteTarget().photoId}
  function selection(){return selected.length?selected:(current()?[current()]:[])}
  function paint(){document.querySelectorAll('[data-photo],[data-watch-photo]').forEach(el=>el.classList.toggle('asset-selected',selection().includes(el.dataset.photo||el.dataset.watchPhoto)));$('asset-count').textContent=`${selection().length} 张`;$('asset-parse-sku').disabled=parsing||!current()||Framekeep.remoteTarget().paused;$('asset-parse-sku').textContent=parsing?'OCR 解析中…':'OCR 解析当前图'}
  function changed(){
    const context=JSON.stringify(Framekeep.browseContext()),focus=current();
    if(context!==lastContext){selected=focus?[focus]:[];anchor=focus;lastContext=context;previewId=null;resetSelection()}
    else if(lastFocus!==focus){selected=focus?[focus]:[];anchor=focus;previewId=null;resetSelection()}
    lastFocus=focus;paint();
  }
  $('selection-page').addEventListener('click',event=>{
    if(event.target.closest('input,[data-star]'))return;
    const node=event.target.closest('[data-photo],[data-watch-photo]');if(!node)return;
    const target=node.dataset.photo||node.dataset.watchPhoto,order=Framekeep.archiveOrder();
    if(event.shiftKey&&anchor&&order.includes(anchor)){
      const a=order.indexOf(anchor),b=order.indexOf(target);selected=order.slice(Math.min(a,b),Math.max(a,b)+1);
    }else if(event.ctrlKey||event.metaKey){selected=selection().includes(target)?selection().filter(x=>x!==target):[...selection(),target];anchor=target}
    else {selected=[target];anchor=target}
    lastFocus=target;previewId=null;resetSelection();
  },true);
  document.addEventListener('framekeep:selection',changed);changed();
  window.FramekeepAssets={selection:()=>selection().slice(),resolve:async()=>{
    const ids=selection().slice(),sid=FRAMEKEEP_SESSION.id;if(!ids.length)throw new Error('请选择照片');
    return (await api('/api/archive/selection',{session_id:sid,capture_ids:ids})).assets;
  }};
  function resetSelection(){
    reviewKey='';reviewRequest?.controller.abort();reviewRequest=null;
    skuEdited=false;selectionRead='';readSequence++;skuEditVersion++;$('asset-sku').value='';ocrContext='';ocrSnapshot=null;$('asset-ocr-result').hidden=true;$('asset-ocr-codes').replaceChildren();$('asset-ocr-text').textContent='';
    $('asset-sku').placeholder='识别后自动填入，也可批量修改';
  }
  async function fillSelection(force=false){
    const key=JSON.stringify([FRAMEKEEP_SESSION.id,selection(),archiveVersion]);
    if(parsing||!selection().length||(!force&&(skuEdited||selectionRead===key)))return;
    const request=++readSequence,assets=await FramekeepAssets.resolve();
    if(request!==readSequence||key!==JSON.stringify([FRAMEKEEP_SESSION.id,selection(),archiveVersion])||(!force&&skuEdited))return;
    const skus=[...new Set(assets.map(a=>a.sku))],scopes=[...new Set(assets.map(a=>a.scope))];
    $('asset-sku').value=skus.length===1?skus[0]:'';
    $('asset-sku').placeholder=skus.length>1?'所选款号不同，输入后可批量修改':'识别后自动填入，也可批量修改';
    if(force)msg(`所选 ${assets.length} 张：SKU ${skus.length===1?(skus[0]||'未备案'):'混合值'}；类型 ${scopes.length===1?(scopes[0]||'未填写'):'混合值'}`);
    skuEdited=false;selectionRead=key;previewId=null;
  }
  $('asset-read').onclick=()=>act(()=>fillSelection(true));
  const parseContext=()=>JSON.stringify([FRAMEKEEP_SESSION.id,current(),selection()]);
  // 自动二星识别沿用手动解析的候选按钮与确认接口，不自动替用户应用款号。
  document.addEventListener('framekeep:capture-status',async event=>{
    const data=event.detail,context=parseContext();
    if(data.session_id!==FRAMEKEEP_SESSION.id||data.capture_id!==current()||parsing||ocrApplying)return;
    if(data.ocr?.status!=='review'){
      reviewRequest?.controller.abort();reviewRequest=null;
      if(reviewKey&&['queued','running','retry','cancelled','stale'].includes(data.ocr?.status)){
        reviewKey='';ocrSnapshot=null;ocrContext='';$('asset-ocr-result').hidden=true;
      }
      return;
    }
    const key=JSON.stringify([context,data.ocr.job_key,data.ocr.candidates]);
    if(reviewKey===key||reviewRequest?.key===key)return;
    reviewRequest?.controller.abort();const request={key,controller:new AbortController()};reviewRequest=request;
    try{
      const snapshot=await api('/api/sku/ocr/details?'+new URLSearchParams({session_id:data.session_id,capture_id:data.capture_id}),undefined,8000,request.controller.signal);
      if(reviewRequest!==request||context!==parseContext()||parsing||ocrApplying||snapshot.session_id!==data.session_id||snapshot.capture_id!==data.capture_id)return;
      ocrContext=context;ocrSnapshot=snapshot;reviewKey=key;
      const codes=snapshot.latest_recommended??snapshot.recommended??[];
      $('asset-ocr-codes').innerHTML=codeButtons(codes)||'<p class="dialog-copy">未找到推荐款号，可选择下方原文或打开 OCR 资料填写正确款号。</p>';
      $('asset-ocr-text').innerHTML=codeButtons(rawLines(snapshot.text),true)||'<span>未返回可见文本</span>';
      $('asset-ocr-result').querySelector('details').open=true;$('asset-ocr-result').hidden=false;markOcrCode(snapshot.selected_sku);
      msg(codes.length?'SKU 待核对：点击正确款号即可应用到整款图片':'SKU 待核对：请查看 OCR 原文或打开 OCR 资料自行选择');
      window.FramekeepSidebar?.show('photos');panel.open=true;
      requestAnimationFrame(()=>{if(context===parseContext()&&ocrSnapshot===snapshot)$('asset-ocr-result').scrollIntoView({block:'nearest'})});
    }catch(error){if(error.name!=='AbortError'&&reviewRequest===request&&context===parseContext())msg('待核对款号读取失败：'+error.message+'；可打开 OCR 资料重试')}
    finally{if(reviewRequest===request)reviewRequest=null}
  });
  function useOcrCode(code){$('asset-sku').value=code;$('asset-sku').dispatchEvent(new Event('input',{bubbles:true}))}
  function markOcrCode(code){
    for(const button of $('asset-ocr-result').querySelectorAll('[data-ocr-code]')){const chosen=button.dataset.ocrCode===code;button.setAttribute('aria-pressed',String(chosen));button.querySelector('.asset-ocr-code-action').textContent=chosen?'已应用 ✓':'点击应用'}
  }
  async function refreshOcrRecommendations(){
    if(!ocrSnapshot||ocrContext!==parseContext())return;
    const before=ocrSnapshot,context=ocrContext;
    const data=await api('/api/sku/ocr/details?'+new URLSearchParams({session_id:before.session_id,capture_id:before.capture_id}));
    if(ocrSnapshot!==before||context!==parseContext()||ocrApplying)return;
    ocrSnapshot=data;$('asset-ocr-codes').innerHTML=codeButtons(data.latest_recommended??data.recommended);markOcrCode(data.selected_sku);msg('推荐已按新规则刷新；原文仍可点击，已确认款号与输入内容保留');
  }
  $('asset-ocr-result').onclick=event=>{
    const button=event.target.closest('[data-ocr-code]');if(!button||button.disabled)return;
    act(async()=>{
      if(ocrContext!==parseContext()||!ocrSnapshot)throw new Error('当前图片已变化，请重新解析');
      const data=ocrSnapshot,context=ocrContext,edit=skuEditVersion,code=button.dataset.ocrCode,buttons=[...$('asset-ocr-result').querySelectorAll('button')];
      if([...code].length>80)throw new Error('这条原文超过 80 字符，请在 SKU 输入框中填写其中的款号');
      ocrApplying=true;readSequence++;buttons.forEach(b=>b.disabled=true);$('asset-ocr-result').setAttribute('aria-busy','true');button.querySelector('.asset-ocr-code-action').textContent='应用中…';msg('正在应用 SKU '+code+'…');
      try{
        const result=await api('/api/sku/ocr/choose',{session_id:data.session_id,capture_id:data.capture_id,sku:code,reading_key:data.reading_key,image_key:data.image_key});
        if(result.operation_id)operation=result.operation_id;
        document.dispatchEvent(new CustomEvent('framekeep:ocr-choice',{detail:result}));document.dispatchEvent(new CustomEvent('framekeep:ocr-result',{detail:result}));
        if(context===parseContext()&&ocrSnapshot===data){
          ocrSnapshot.selected_sku=result.sku;markOcrCode(result.sku);
          const edited=edit!==skuEditVersion;if(!edited)useOcrCode(result.sku);
          msg('已应用 SKU '+result.sku+`，整款 ${result.capture_count||1} 张`+(edited?'；保留应用期间手动修改的输入':'，元数据写入已提交，其他 OCR 资料保留供搜索'));
        }
      }finally{ocrApplying=false;buttons.forEach(b=>b.disabled=false);$('asset-ocr-result').removeAttribute('aria-busy');if(context===parseContext()&&ocrSnapshot===data)markOcrCode(data.selected_sku)}
    });
  };
  const ocrDetailsButton=document.createElement('button');ocrDetailsButton.id='asset-ocr-details';ocrDetailsButton.className='bordered';ocrDetailsButton.textContent='OCR 资料 / 选择 SKU';$('asset-parse-sku').after(ocrDetailsButton);
  ocrDetailsButton.onclick=()=>{if(current())window.FramekeepOcrDetails?.open(FRAMEKEEP_SESSION.id,current()).catch(error=>msg(error.message));else msg('请先选择照片')};
  document.addEventListener('framekeep:ocr-choice',event=>{if(!ocrApplying&&event.detail.session_id===FRAMEKEEP_SESSION.id&&event.detail.capture_id===current()){useOcrCode(event.detail.sku);if(ocrContext===parseContext()&&ocrSnapshot){ocrSnapshot.selected_sku=event.detail.sku;markOcrCode(event.detail.sku)}msg('已确认 SKU '+event.detail.sku+'，其他 OCR 资料保留供搜索')}});
  $('asset-parse-sku').onclick=()=>act(async()=>{
    const sid=FRAMEKEEP_SESSION.id,cid=current(),context=parseContext(),edit=skuEditVersion;
    if(!cid)throw new Error('请先选择要解析的当前照片');
    reviewKey='';reviewRequest?.controller.abort();reviewRequest=null;
    const feedback=window.FramekeepOcrFeedback?.begin({session_id:sid,capture_id:cid,running:true});let feedbackError=null;
    parsing=true;readSequence++;paint();msg('正在用已选模型解析当前图的 SKU 款号…');$('asset-ocr-result').hidden=true;
    try{
      const result=await api('/api/sku/parse',{session_id:sid,capture_id:cid},660000);
      document.dispatchEvent(new CustomEvent('framekeep:ocr-result',{detail:{session_id:sid,capture_id:cid}}));
      if(context!==parseContext()||result.session_id!==sid||result.capture_id!==cid){msg('图片选择已变化，本次结果未填入，请重新解析');return}
      const snapshot=await api('/api/sku/ocr/details?'+new URLSearchParams({session_id:sid,capture_id:cid}));
      if(context!==parseContext()){msg('图片选择已变化，本次结果未填入，请重新解析');return}
      const reading=result.reading||{},codes=[...new Set((result.recommended||reading.codes||[]).filter(code=>typeof code==='string'&&code.trim()&&code.length<=80&&!/[\x00-\x1f]/.test(code)).map(code=>code.trim()))];
      ocrContext=context;ocrSnapshot=snapshot;$('asset-ocr-text').innerHTML=(reading.model?`<span class="asset-ocr-source">${esc(reading.model)}</span>`:'')+(codeButtons(rawLines(reading.text),true)||'<span>未返回可见文本</span>');$('asset-ocr-result').querySelector('details').open=true;$('asset-ocr-result').hidden=false;
      $('asset-ocr-codes').innerHTML=codeButtons(codes);markOcrCode(snapshot.selected_sku);
      if(result.auto_applied){if(edit===skuEditVersion)useOcrCode(result.auto_applied.sku);msg(`规则已自动应用 ${result.auto_applied.sku}，整款 ${result.auto_applied.capture_count} 张，元数据写入已提交`)}
      else if(codes.length===1&&edit===skuEditVersion){useOcrCode(codes[0]);msg(`已读取 ${codes[0]}，点击款号按钮即可应用；有误可打开 OCR 资料自行选择`)}
      else if(codes.length){msg(edit!==skuEditVersion?'解析完成；保留手动修改的输入，点击推荐款号即可应用':'读出多个款号，点击正确款号即可应用；有误可打开 OCR 资料自行选择')}
      else msg('未读出可用 SKU 款号，请查看 OCR 原文或打开 OCR 资料自行选择');
    }catch(error){feedbackError=error;throw error}finally{parsing=false;paint();window.FramekeepOcrFeedback?.end(feedback,feedbackError)}
  });
  $('asset-preview').onclick=()=>act(async()=>{
    const source=window.FramekeepSku?.shootingTypes(),scope=window.FramekeepSku?.currentType();
    const context=()=>JSON.stringify([FRAMEKEEP_SESSION.id,selection(),FramekeepSku.currentType(),FramekeepSku.shootingTypes()?.project_id,$('asset-sku').value.trim(),$('asset-mode').value,$('asset-overwrite').checked]),before=context();
    const data=await api('/api/metadata/group/preview',{session_id:FRAMEKEEP_SESSION.id,capture_id:current(),capture_ids:selection(),sku:$('asset-sku').value.trim(),scope,project:source?.project_id||''});
    if(before!==context())throw new Error('当前拍摄或所选照片已变化，请重新打开编辑');groupEdit=data;previewId=data.preview_id;$('asset-group-sku').value=data.sku;$('asset-group-message').textContent='';
    $('asset-preview-content').innerHTML=`<p>类型：${esc(data.scope)} · ${data.targets.length} 张 / ${data.targets.reduce((n,t)=>n+t.files.length,0)} 个文件</p><div class="sku-table-wrap"><table><thead><tr><th>资产</th><th>原归属</th><th>处理</th><th>写入文件</th></tr></thead><tbody>${data.targets.map(t=>`<tr><td>${esc(t.files[0]?.path.split(/[\\/]/).at(-1)||t.id)}</td><td>${esc(t.before.sku||'未备案')} / ${esc(t.before.scope)}</td><td>${t.apply?'将备案':'冲突，跳过'}</td><td>${t.files.map(f=>`<div>${esc(f.path.split(/[\\/]/).at(-1))} · ${f.capability==='embedded'?'文件内部元数据':f.capability==='sidecar'?'XMP 侧车档案':'此格式暂不支持'}${f.online?'':' · 离线'}</div>`).join('')}</td></tr>`).join('')}</tbody></table></div><p>RAW 的 SKU 与备案字段保存到同名 XMP 小档案，JPG 更新文件元数据；保留原有星级和图像数据。撤销只记录字段，不长期复制整张照片。离线文件会单独记录失败。</p>`;
    const summary=document.createElement('p');summary.className='dialog-copy';summary.textContent=data.group.kind==='interval'?`已定位完整拍摄区间，共 ${data.targets.length} 张，包含款末标签；修改将应用到整款。`:`尚无款末区间，本次使用手动选择的 ${data.targets.length} 张照片。`;$('asset-preview-content').prepend(summary);
    $('asset-write-title').textContent=data.group.kind==='interval'?'修改整款 SKU 元数据':'修改所选 SKU 元数据';$('asset-apply').textContent=data.group.kind==='interval'?'更新整款元数据':'更新所选元数据';
    $('asset-apply').disabled=!data.targets.length;preview.showModal();
  });
  $('asset-apply').onclick=async()=>{
    if(groupWriting||!groupEdit)return;const edit=groupEdit,code=$('asset-group-sku').value.trim();groupWriting=true;$('asset-apply').disabled=true;$('asset-group-sku').disabled=true;$('asset-group-message').textContent='正在更新整款 SKU 并提交元数据写入…';
    try{
      if(edit.group.session_id!==FRAMEKEEP_SESSION.id||edit.group.capture_id!==current())throw new Error('当前照片已变化，请重新打开编辑');
      const result=await api('/api/metadata/group/apply',{preview_id:edit.preview_id,sku:code});operation=result.operation.id;
      document.dispatchEvent(new CustomEvent('framekeep:ocr-choice',{detail:result}));document.dispatchEvent(new CustomEvent('framekeep:ocr-result',{detail:result}));
      if(edit.group.session_id===FRAMEKEEP_SESSION.id&&edit.group.capture_id===current())useOcrCode(result.sku);
      preview.close();groupEdit=null;previewId=null;skuEdited=false;selectionRead='';msg(`已更新 SKU ${result.sku}，${result.capture_count} 张照片的 RAW / JPG 元数据写入已排队`);
    }catch(error){$('asset-group-message').textContent=error.message;msg(error.message)}
    finally{groupWriting=false;$('asset-apply').disabled=false;$('asset-group-sku').disabled=false}
  };
  preview.addEventListener('cancel',event=>{if(groupWriting)event.preventDefault()});
  $('asset-start').onclick=()=>act(async()=>{const s=await api('/api/archive/start',{session_id:FRAMEKEEP_SESSION.id,mode:$('asset-mode').value});stream=s.streams.find(s=>s.state==='active'&&s.session===FRAMEKEEP_SESSION.id);msg('已接续未归组照片，等待款末标签')});
  $('asset-mode').onchange=()=>act(async()=>{previewId=null;if(stream)await api('/api/archive/configure',{stream_id:stream.id,mode:$('asset-mode').value})});
  $('asset-close-stream').onclick=()=>act(async()=>{const sid=FRAMEKEEP_SESSION.id,sku=$('asset-stream-sku').value.trim()||$('asset-sku').value.trim();await history();if(sid!==FRAMEKEEP_SESSION.id)throw new Error('当前目录已变化，请重新操作');if(!stream)throw new Error('请先开始拍摄段');await api('/api/archive/close',{stream_id:stream.id,sku});msg('当前未结束图片已封口，请在归档库查看区间')});
  async function search(){const data=await api('/api/assets/search?'+new URLSearchParams({sku:$('archive-sku').value,scope:$('archive-scope').value,project:$('archive-project').value,mode:$('archive-match').value,cursor:cursors[pageIndex]}));queryRows=data.rows;$('archive-total').textContent=data.rows.length;nextCursor=data.next_cursor;$('archive-page').textContent=`第 ${pageIndex+1} 页 · ${data.rows.length} 项 · ${data.elapsed_ms} ms`;$('archive-prev').disabled=pageIndex===0;$('archive-next').disabled=!data.has_more;
    $('archive-results').innerHTML=data.rows.length?'<table><thead><tr><th>图片</th><th>SKU / 类型</th><th>位置</th><th>操作</th></tr></thead><tbody>'+data.rows.map(r=>`<tr><td>${r.preview?`<img class="archive-thumb" src="${esc(r.preview)}" loading="lazy" alt="资产缩略图">`:''}</td><td>${esc(r.sku||'待备案')}<br>${esc(r.scope)}<br><small>${esc(statusName(r.role))}</small></td><td>${r.locations.map(l=>esc(l.path)+' · '+esc(statusName(l.status))).join('<br>')}</td><td><button data-asset-detail="${r.id}">详情</button></td></tr>`).join('')+'</tbody></table>':empty('暂无匹配资产','调整检索条件，或在选片页完成 SKU 备案后查看。');
  }
  async function history(){const s=await api('/api/archive/streams');stream=s.streams.find(s=>s.state==='active'&&s.session===FRAMEKEEP_SESSION.id)||null;
    $('asset-stream-status').textContent=stream?`${stream.scope} · 待封口 ${stream.pending||0} 张 · 已归组 ${s.segments.filter(x=>x.stream===stream.id).length} 款`:'尚未开始当前目录拍摄段';
    archiveVersion=JSON.stringify(s.segments.map(x=>[x.id,x.sku,x.status,x.operation]));await fillSelection();
    if(!visible())return;
    $('archive-sealed').textContent=s.segments.length;$('archive-late').textContent=s.late.length;
    $('archive-segments').innerHTML=s.segments.length?s.segments.map(x=>`<article><div><strong>${esc(x.sku||'待识别 SKU')}</strong><span class="workspace-badge">${esc(statusName(x.status))}</span></div><p>${x.members.length} 张（含标签）${x.reason?' · '+esc(x.reason):''}</p>${x.status==='ready'?`<button data-segment-apply="${x.id}">备案并写入</button>`:''}</article>`).join(''):empty('暂无结束区间','款末标签确认后，自动显示本款归组记录。');
    const ops=await api('/api/metadata/operations');$('archive-batches').textContent=ops.operations.length;
    $('archive-operations').innerHTML=ops.operations.length?ops.operations.map(o=>`<article><div><strong>${esc(statusName(o.kind))}</strong><span class="workspace-badge">${esc(statusName(o.status))}</span></div><p>${new Date(o.created*1000).toLocaleString()}</p><div class="asset-actions"><button data-operation="${o.id}">详情</button><button data-retry="${o.id}">重试失败</button>${o.kind==='assign'?`<button data-revert="${o.id}">撤销批次</button>`:''}</div></article>`).join(''):empty('暂无写入批次','备案写入和撤销结果会记录在这里。');
  }
  FramekeepWorkspace.register('archive',archiveTab,archive,()=>act(async()=>{await search();await history();api('/api/search/status').then(s=>{$('everything-status').textContent=s.available?`Everything ${s.version} 已连接`:s.message}).catch(e=>{$('everything-status').textContent=e.message})}));
  FramekeepWorkspace.register('settings',settingsButton,settings,()=>{settingsContext();act(history)});
  $('archive-search').onclick=()=>act(async()=>{resetPage();await search()});$('archive-prev').onclick=()=>act(async()=>{pageIndex=Math.max(0,pageIndex-1);await search()});$('archive-next').onclick=()=>act(async()=>{if(!nextCursor)return;cursors[++pageIndex]=nextCursor;await search()});
  for(const id of ['archive-sku','archive-scope','archive-project','archive-match']){
    $(id).addEventListener('change',()=>{resetPage();$('archive-prev').disabled=true;$('archive-next').disabled=true});
    $(id).addEventListener('keydown',e=>{if(e.key==='Enter')$('archive-search').click()});
  }
  async function fileSearch(){const data=await api('/api/search/files?'+new URLSearchParams({q:fileText,offset:fileOffset}));$('file-search-message').textContent=`${data.source==='everything'?'Everything':'备案库回退'} · ${data.rows.length} 项 · ${data.elapsed_ms} ms · ${data.coverage}${data.message?' · '+data.message:''}`;$('file-next').disabled=!data.has_more||fileOffset>=9960;
    $('file-search-results').innerHTML='<table><thead><tr><th>文件位置</th><th>已有备案</th></tr></thead><tbody>'+data.rows.map(r=>`<tr><td>${esc(r.path)}</td><td>${r.asset?`${esc(r.asset.sku||'待备案')} / ${esc(r.asset.scope)} <button data-asset-detail="${esc(r.asset.id)}">详情</button>`:'未备案候选'}</td></tr>`).join('')+'</tbody></table>';
  }
  $('file-search').onclick=()=>act(async()=>{fileOffset=0;fileText=$('file-query').value;await fileSearch()});$('file-next').onclick=()=>act(async()=>{fileOffset+=60;await fileSearch()});$('file-query').onkeydown=e=>{if(e.key==='Enter')$('file-search').click()};
  $('archive-relocate').onclick=()=>act(async()=>{const r=await api('/api/assets/relocate',{folder:$('archive-folder').value});$('archive-message').textContent=`已扫描 ${r.assets} 个资产`;await search()});
  archive.addEventListener('click',e=>act(async()=>{const b=e.target.closest('button');if(!b)return;
    if(b.dataset.assetDetail){const data=await api('/api/assets/'+b.dataset.assetDetail);detail(data)}
    if(b.dataset.operation){detail(await api('/api/metadata/operations/'+b.dataset.operation))}
    for(const [attr,path] of [['retry','retry'],['revert','revert']])if(b.dataset[attr]){const r=await api('/api/metadata/'+path,{operation_id:b.dataset[attr]});detail(r);await search()}
    if(b.dataset.segmentApply){await api('/api/archive/segment-apply',{segment_id:b.dataset.segmentApply});await history();await search()}
  }));
  $('archive-export').onclick=()=>{const blob=new Blob([JSON.stringify(queryRows,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='资产归档.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
  preview.querySelectorAll('[data-close]').forEach(button=>button.onclick=()=>{if(!groupWriting)preview.close()});
  async function poll(){
    try{if(!busy){
      await history();
      if(operation){
        const op=await api('/api/metadata/operations/'+operation),counts={};let raw=0,jpg=0;
        op.jobs.forEach(j=>{const key=j.status==='applied'?(j.storage==='embedded'?'已内嵌':'仅伴随文件'):statusName(j.status);counts[key]=(counts[key]||0)+1;if(j.status==='applied'&&j.storage==='embedded'){if(/\.jpe?g$/i.test(j.path))jpg++;else if(/\.(cr3|arw)$/i.test(j.path))raw++}});
        const failed=op.jobs.filter(j=>j.status==='failed'),pending=op.jobs.some(j=>['queued','running'].includes(j.status));
        msg((failed.length&&!pending?'部分文件未写入 · ':'文件状态：')+Object.entries(counts).map(([k,n])=>`${k} ${n}${k==='已内嵌'?`（RAW ${raw} / JPG ${jpg}）`:''}`).join(' / ')+(failed.length?'；'+failed.map(j=>j.path.split(/[\\/]/).at(-1)+'：'+j.reason).join('；'):''));
        if(!pending)operation=null;
      }
    }}catch{}setTimeout(poll,1800)
  }poll();
})();
