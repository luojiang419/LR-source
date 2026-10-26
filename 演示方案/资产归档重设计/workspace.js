(() => {
  'use strict';
  if (!window.FramekeepIO || !window.FramekeepWorkspace) return;
  const $ = id => document.getElementById(id), api = FramekeepIO.api;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const endpoint = (name, data) => api('/api/archive-v2/' + name, data);
  const host = $('asset-archive-dialog');
  host.className = 'asset-workspace v2 hidden';
  host.innerHTML = `
    <header class="v2-header">
      <div class="v2-brand"><span class="v2-mark">F.</span><div><strong>末帧 <i>Archive</i></strong><small>拍摄资产工作台</small></div></div>
      <nav class="v2-nav" aria-label="资产工作页"><button data-page="archive" class="on">资产归档</button><button data-page="database">SKU 数据库 <span id="v2-sku-count">0</span></button><button data-page="review">复核中心 <span id="v2-review-count">0</span></button></nav>
      <div class="v2-header-end"><span class="v2-lab">隔离演示 · V2</span><button id="v2-design">架构与方案 ↗</button><button id="v2-exit">返回选片</button></div>
    </header>
    <div class="v2-body">
      <aside class="v2-left">
        <div class="v2-left-heading"><span id="v2-side-title">资源库</span><button id="v2-add" title="添加归档文件夹" aria-label="添加归档文件夹">＋</button></div>
        <div class="v2-collection"><button data-filter="all" class="on"><span>▦　所有照片</span><b id="v2-total">0</b></button><button data-filter="unassigned"><span>◌　待归属照片</span><b id="v2-unassigned">0</b></button><button data-filter="review"><span>◇　标签待复核</span><b id="v2-pending">0</b></button></div>
        <div class="v2-section-label" id="v2-tree-label">磁盘与文件夹 <span>路径引用</span></div>
        <div id="v2-tree" class="v2-tree"></div>
        <div id="v2-sku-list" class="v2-sku-list" hidden></div>
        <div class="v2-left-foot"><span class="v2-dot"></span><span>原位置引用 · 独立索引库</span><small id="v2-root-count"></small></div>
      </aside>
      <main class="v2-center">
        <div class="v2-searchline"><div class="v2-searchbox"><span>⌕</span><input id="v2-search" type="search" placeholder="搜索 SKU，查找分散在不同目录的照片" aria-label="搜索 SKU"><kbd>SKU</kbd></div><button id="v2-global" title="清除目录与款号限制，搜索全部归档">全库搜索</button></div>
        <header class="v2-gallery-heading"><div><div id="v2-breadcrumb" class="v2-breadcrumb">资源库 / 所有照片</div><h2 id="v2-title">所有照片 <small id="v2-gallery-count"></small></h2></div><div class="v2-viewtools"><span id="v2-mode-hint">拍摄资产</span><button id="v2-grid-view" class="on" title="网格视图">▦</button><button id="v2-loupe-view" title="大图视图">▣</button></div></header>
        <div id="v2-review-banner" class="v2-review-banner" hidden><strong>先核对标签，再确认整款。</strong><span>结束标签属于前一款；最后未封口照片保留待归属。OCR 原文和区间成员可在右侧检查。</span></div>
        <div id="v2-grid" class="v2-grid" role="list" aria-label="归档照片"></div>
        <div id="v2-loupe" class="v2-loupe" hidden><div id="v2-image-stage"><img id="v2-large" alt="所选照片大图"></div><div id="v2-filmstrip" class="v2-filmstrip"></div></div>
        <footer class="v2-gallery-footer"><span id="v2-page-summary">照片保持在原来的文件夹</span><div><button id="v2-prev">上一页</button><button id="v2-next">下一页</button><span>缩略图</span><input id="v2-size" type="range" min="135" max="300" value="190" aria-label="缩略图尺寸"></div></footer>
      </main>
      <aside class="v2-right">
        <section class="v2-scan-panel"><div class="v2-eyebrow">SMART INDEX <span class="v2-local">本机 OCR</span></div><h3>自动解析 SKU</h3><p>逐张读图 → 识别结束标签 → 归纳款号区间</p><button id="v2-scan" class="v2-primary">开始扫描所有照片 <span>↗</span></button><div id="v2-progress-wrap" hidden><progress id="v2-progress" max="100" value="0"></progress><div><span id="v2-progress-text"></span><button id="v2-cancel">停止</button></div><small id="v2-current"></small></div><p id="v2-job-status" class="v2-subtle">首次扫描后，在复核中心确认 SKU 与区间。</p></section>
        <div class="v2-inspector-tabs"><button data-inspector="metadata" class="on">SKU 元数据</button><button data-inspector="evidence">识别证据</button><button data-inspector="history">操作记录</button></div>
        <div id="v2-inspector" class="v2-inspector"><div class="v2-empty"><span>▧</span><h3>选择一张照片</h3><p>检查款号、结束标签和资产指纹。<br>双击照片进入大图。</p></div></div>
        <div id="v2-message" class="v2-message" role="status" aria-live="polite">准备就绪</div>
      </aside>
    </div>
    <footer class="v2-statusbar"><span><i></i>归档引擎已连接</span><span id="v2-status-summary"></span><span>照片引用 / SKU 索引 / 人工复核</span></footer>
    <dialog id="v2-add-dialog"><form method="dialog"><div class="v2-dialog-top"><h2>添加归档位置</h2><button value="cancel">×</button></div><p>按硬盘自动归类，包含所选文件夹的子目录。只登记照片位置。</p><label>文件夹路径<input id="v2-path" placeholder="例如 X:\\摄影\\2026"></label><div class="v2-dialog-actions"><button id="v2-browse" type="button">浏览文件夹</button><button id="v2-add-confirm" type="button" class="v2-primary">添加并建立目录索引</button></div><p id="v2-add-status"></p></form></dialog>
    <dialog id="v2-design-dialog"><div class="v2-dialog-top"><div><span class="v2-eyebrow">ARCHITECTURE / 02</span><h2>以照片身份为核心的资产系统</h2></div><button id="v2-design-close">×</button></div><p>SKU 是可校正的业务属性；永久资产 ID、文件内容校验和存储位置分别管理。</p><div class="v2-architecture"><div>磁盘 / NAS<small>原文件路径引用</small></div><b>→</b><div>流式索引<small>身份 · 位置 · 内容哈希</small></div><b>→</b><div>本地 OCR<small>文字证据 · 模型缓存</small></div><b>↓</b><div>统一搜索<small>SKU → 所有位置</small></div><b>←</b><div>元数据写回<small>快照 · 校验 · 读回日志</small></div><b>←</b><div>区间复核<small>结束标签 · 人工锁定</small></div></div><div class="v2-design-notes"><article><h3>① 可靠的归属</h3><p>按文件夹和相机拆拍摄流；标签封闭前一款，失败标签仍阻断区间。无时间依据时核对文件名顺序。</p></article><article><h3>② 可追溯的识别</h3><p>保留 OCR 原文、模型、SHA-256、区间成员和人审记录。低置信结果进入复核，不伪造分数。</p></article><article><h3>③ 生产扩展建议</h3><p>RAW/JPG 组件配对、离线盘重定位、批量事务写回、FTS 搜索与虚拟网格分阶段接入。</p></article></div><p class="v2-subtle">此次演示复用最新末帧源码；Jcam 的相册树、大图与属性侧栏按现有 Web 工作台适配。示例照片是实拍预览组成的流程样片，第二张标签是生成测试标签。</p></dialog>`;

  let state = {}, rows = [], selected = null, page = 'archive', filter = 'all', root = '', folder = '', sku = '', inspector = 'metadata';
  let cursors = [''], pageIndex = 0, nextCursor = '', more = false, large = false, requestId = 0, lastState = '', scanning = false, saving = false;
  const folds = new Set();
  const notify = text => { $('v2-message').textContent = text; };
  const action = async fn => { try { await fn(); } catch (e) { notify(e.message); } };
  const resetPage = () => { cursors = ['']; pageIndex = 0; };
  const sources = {manual:'人工已确认', path_rule:'路径候选', unmatched:'等待解析', review:'等待复核'};
  const status = r => r.sku ? '已确认' : r.role === 'label' ? '标签 · 待复核' : r.role === 'unclear_label' ? '模糊标签' : r.role === 'error' ? '解析失败' : '待归属';
  const count = value => Number(value || 0).toLocaleString('zh-CN');

  function renderTree() {
    const drives = new Map();
    for (const r of state.tree?.roots || []) {
      const drive = r.path.match(/^[A-Za-z]:/)?.[0] || (r.path.startsWith('\\\\') ? r.path.split('\\').slice(0, 4).join('\\') : '其他位置');
      if (!drives.has(drive)) drives.set(drive, []);
      drives.get(drive).push(r);
    }
    const draw = (node, rid, prefix = '') => [...node.entries()].map(([name, data]) => {
      const path = prefix ? prefix + '/' + name : name, key = rid + path;
      return `<details data-fold="${esc(key)}" ${folds.has(key) ? '' : 'open'}><summary><button data-root="${esc(rid)}" data-folder="${esc(path)}" class="${root===rid&&folder===path?'on':''}"><span>▱ ${esc(name)}</span><small>${data.count}</small></button></summary>${draw(data.children, rid, path)}</details>`;
    }).join('');
    $('v2-tree').innerHTML = [...drives].map(([drive, roots]) => `<details open class="v2-drive"><summary><span class="v2-drive-icon">▰</span><strong>${esc(drive)}</strong><small>在线</small></summary>${roots.map(r => {
      const nodes = new Map(); let total = 0;
      for (const f of r.folders) {
        total += f.count; let parent = nodes;
        for (const part of f.folder === '/' ? [] : f.folder.split(/[\\/]/).filter(Boolean)) {
          if (!parent.has(part)) parent.set(part, {count: 0, children: new Map()});
          const node = parent.get(part); node.count += f.count; parent = node.children;
        }
      }
      return `<details open class="v2-root"><summary><button data-root="${esc(r.id)}" data-folder="" title="${esc(r.path)}" class="${root===r.id&&!folder?'on':''}"><span>▱ ${esc(r.path.split(/[\\/]/).at(-1))}</span><small>${total}</small></button></summary>${draw(nodes, r.id)}</details>`;
    }).join('')}</details>`).join('') || '<div class="v2-empty"><p>添加文件夹以开始归档</p></div>';
    $('v2-tree').querySelectorAll('[data-fold]').forEach(d => d.addEventListener('toggle', () => { if (d.open) folds.delete(d.dataset.fold); else folds.add(d.dataset.fold); }));
  }
  function renderSkuList() {
    const query = $('v2-search').value.toUpperCase().replace(/[^A-Z0-9]/g, '');
    $('v2-sku-list').innerHTML = (state.skus || []).filter(s => !query || s.normalized.includes(query)).map(s => `<button data-sku="${esc(s.normalized)}" class="${sku===s.normalized?'on':''}"><span class="v2-sku-icon">#</span><span><strong>${esc(s.sku)}</strong><small>${s.count} 张照片 · ${s.corrected} 已确认</small></span><b>›</b></button>`).join('') || '<div class="v2-empty"><p>确认款号后会自动汇入此处</p></div>';
  }
  function renderState() {
    const c = state.counts || {};
    $('v2-total').textContent = count(c.total);
    $('v2-unassigned').textContent = count(c.total - c.tagged);
    $('v2-pending').textContent = count(state.pending_labels);
    $('v2-sku-count').textContent = count(state.skus?.length);
    $('v2-review-count').textContent = count(state.pending_labels);
    $('v2-root-count').textContent = `${state.roots?.length || 0} 个归档位置`;
    $('v2-status-summary').textContent = `${count(c.total)} 张照片　·　${count(c.tagged)} 张已归属　·　${count(state.skus?.length)} 个 SKU`;
    const job = state.job || {};
    scanning = ['running', 'indexing'].includes(job.status);
    $('v2-scan').disabled = scanning;
    $('v2-scan').innerHTML = scanning ? '正在扫描照片…' : '开始扫描所有照片 <span>↗</span>';
    $('v2-progress-wrap').hidden = !job.total && !scanning;
    $('v2-progress').max = job.total || 1; $('v2-progress').value = job.done || 0;
    $('v2-progress-text').textContent = `${job.done || 0} / ${job.total || 0} 张`;
    $('v2-current').textContent = job.current || '';
    $('v2-cancel').hidden = !scanning;
    $('v2-job-status').textContent = job.status === 'completed' ? `扫描完成 · ${job.errors || 0} 张失败 · ${state.pending_labels} 张标签待复核` : job.status === 'cancelled' ? '扫描已停止；再次扫描将复用已完成的 OCR。' : job.status === 'failed' ? '扫描失败：' + job.current : scanning ? '正在读取真实图像文字。首次加载模型需稍候。' : '首次扫描后，在复核中心确认 SKU 与区间。';
    renderTree(); renderSkuList();
  }
  async function loadRows() {
    const seq = ++requestId;
    const query = new URLSearchParams({root, folder, q: $('v2-search').value, exact: sku, filter: page==='review'?'review':filter, cursor:cursors[pageIndex]});
    const result = await endpoint('items?' + query);
    if (seq !== requestId) return;
    rows = result.rows; nextCursor = result.next_cursor; more = result.has_more;
    $('v2-prev').disabled = !pageIndex; $('v2-next').disabled = !more;
    const rootName = state.roots?.find(r => r.id === root)?.path.split(/[\\/]/).at(-1);
    const skuName = state.skus?.find(r => r.normalized === sku)?.sku;
    $('v2-breadcrumb').textContent = page==='database' ? `SKU 数据库 / ${skuName || '所有款号'}` : page==='review' ? '复核中心 / 结束标签与区间' : root ? `资源库 / ${rootName} / ${folder}` : '资源库 / 所有照片';
    $('v2-title').innerHTML = `${esc(page==='review'?'标签待复核':page==='database'?skuName||'按款浏览':folder.split('/').at(-1)||rootName||(filter==='unassigned'?'待归属照片':filter==='review'?'标签待复核':'所有照片'))} <small>${rows.length} 张${result.has_more?' · 当前页':''}</small>`;
    $('v2-page-summary').textContent = skuName ? `款号 ${skuName} · 引用原文件位置` : `第 ${pageIndex+1} 页 · ${rows.length} 张 · 按文件索引显示`;
    if (selected) selected = rows.find(r => r.id === selected.id) || null;
    renderPhotos(); renderInspector();
  }
  function photo(r, mini = false) {
    return `<button class="v2-photo ${selected?.id===r.id?'selected':''} ${mini?'mini':''}" data-item="${esc(r.id)}" role="listitem" title="${esc(r.path)}"><span class="v2-image"><img src="${r.preview}?size=${mini?220:620}" loading="lazy" alt="${esc(r.name)}">${r.role!=='photo'?`<span class="v2-photo-label">${r.role==='error'?'!':'SKU'}</span>`:''}</span>${mini?'':`<span class="v2-photo-name">${esc(r.name)}</span><span class="v2-photo-meta"><b>${esc(r.sku || r.candidate || '—')}</b><i class="${r.sku?'confirmed':''}">${status(r)}</i></span>`}</button>`;
  }
  function renderPhotos() {
    $('v2-grid').innerHTML = rows.map(r => photo(r)).join('') || `<div class="v2-empty"><span>▦</span><h3>${page==='review'?'暂无待复核标签':'没有符合条件的照片'}</h3><p>${page==='review'?'开始扫描后，检测到的结束标签会列在这里。':'添加文件夹或清除搜索条件。'}</p></div>`;
    $('v2-filmstrip').innerHTML = rows.map(r => photo(r, true)).join('');
    $('v2-grid').hidden = large; $('v2-loupe').hidden = !large;
    $('v2-grid-view').classList.toggle('on', !large); $('v2-loupe-view').classList.toggle('on', large);
    if (selected && large) $('v2-large').src = selected.preview + '?size=2000';
  }
  function renderInspector() {
    if (saving) return;
    const panel = $('v2-inspector');
    if (inspector === 'history') {
      panel.innerHTML = `<div class="v2-inspector-heading"><h3>最近操作</h3><small>持久化审计</small></div>` + (state.events || []).map(e => {
        let d = {}; try { d = JSON.parse(e.detail); } catch {}
        return `<article class="v2-event"><small>${new Date(e.created*1000).toLocaleTimeString('zh-CN')} · ${esc(e.kind)}</small><strong>${esc(d.file || d.sku || d.status || '归档操作')}</strong><p>${esc(d.codes?.join(' / ') || d.error || (d.members ? d.members.length+' 张照片' : '已记录'))}</p></article>`;
      }).join(''); return;
    }
    if (!selected) { panel.innerHTML = '<div class="v2-empty"><span>▧</span><h3>选择一张照片</h3><p>检查款号、结束标签和资产指纹。<br>双击照片进入大图。</p></div>'; return; }
    const r = selected, evidence = r.evidence || {}, interval = r.interval;
    const top = `<div class="v2-selected-heading"><span>${r.extension.slice(1).toUpperCase()}</span><div><strong>${esc(r.name)}</strong><small>${(r.size/1024/1024).toFixed(2)} MB · ${esc(status(r))}</small></div></div>`;
    if (inspector === 'evidence') {
      panel.innerHTML = top + `<section class="v2-field-section"><h4>OCR 原始文字</h4><pre>${esc(evidence.text || evidence.error || '尚未扫描')}</pre><h4>识别候选</h4><p>${esc(evidence.codes?.join(' / ') || '无候选')}</p><h4>模型与置信依据</h4><p>${esc(evidence.model || '未调用')}<br>${evidence.unscored?'模型未提供校准分数，必须人工复核。':evidence.confidence!=null?'模型自报值 '+evidence.confidence+'，仍需核对。':'未提供置信分数。'}</p><h4>排序依据</h4><p>${esc(r.ordering || '尚未建立')}</p><h4>内容 SHA-256</h4><code>${esc(r.content_hash || '扫描后生成')}</code></section>`;
      return;
    }
    panel.innerHTML = top + `<section class="v2-field-section"><div class="v2-field-heading"><h4>款号归属</h4><span class="v2-badge">${r.sku?'已确认':'待复核'}</span></div><label>SKU 款号<input id="v2-edit-sku" value="${esc(r.sku || r.candidate)}" placeholder="填写或校正款号" maxlength="80"></label>${interval?`<div class="v2-interval"><strong>结束标签 → 前一款</strong><p>${interval.members.length} 张（含当前标签） · ${esc(interval.ordering)}</p><div class="v2-interval-photos">${interval.members.map(id => `<img src="/api/catalog/preview/${esc(id)}?size=180" alt="区间成员照片">`).join('')}</div><small>状态：${esc(interval.status==='confirmed'?'已确认并锁定':interval.status==='changed'?'成员发生变化，需重新核对':'请核对照片与 SKU')}</small></div><label class="v2-check"><input id="v2-apply-interval" type="checkbox" checked>将款号应用到此区间全部照片</label>`:''}<label class="v2-check"><input id="v2-overwrite" type="checkbox">明确覆盖已确认的其他款号</label><button id="v2-save" class="v2-primary">确认并保存 SKU <span>✓</span></button>${!interval?'<button id="v2-boundary" class="v2-secondary">将此图标记为一款的结束标签</button>':''}<p class="v2-subtle">${r.source==='manual'?'人工归属已锁定，自动重扫不会覆盖。':'OCR 候选不会直接当成正确款号。'}</p></section><section class="v2-field-section"><h4>照片指纹</h4><dl><dt>资产 ID</dt><dd>${esc(r.asset_id)}</dd><dt>文件 ID</dt><dd>${esc(r.file_id)}</dd><dt>原始位置</dt><dd>${esc(r.path)}</dd><dt>元数据状态</dt><dd>${r.written_sku&&r.written_sku!==r.sku?'SKU 已修改，文件待更新':esc(r.write_status)}</dd></dl><button id="v2-write" class="v2-secondary" ${!r.writable||!r.sku?'disabled':''}>将 SKU 写入此样片并验证读回</button><p class="v2-subtle">${r.writable?'仅改演示副本；保留照片像素与已有非目标元数据。':'外部原图引用：本演示可校正索引，文件写回尚未开放。'}</p></section>`;
    $('v2-save').onclick = () => action(async () => {
      const value = $('v2-edit-sku').value, scope = $('v2-apply-interval')?.checked ? 'interval' : 'photo', overwrite = $('v2-overwrite').checked, id = selected.id;
      saving = true;
      try { const result = await endpoint('assign', {id, sku:value, scope, overwrite}); selected = result.item; await refresh(true); notify(`已确认 ${value}，更新 ${result.count} 张照片；SKU 数据库已同步。`); }
      finally { saving = false; renderInspector(); }
    });
    if ($('v2-boundary')) $('v2-boundary').onclick = () => action(async () => { await endpoint('boundary', {id:r.id, sku:$('v2-edit-sku').value}); await refresh(true); notify('结束标签已标记，请核对区间成员后确认款号。'); });
    $('v2-write').onclick = () => action(async () => { const button=$('v2-write'); button.disabled=true; try { const result = await endpoint('write', {id:r.id}); selected=result.item; await refresh(true); notify('SKU、资产 ID 与文件 ID 已内嵌，读回和像素校验通过。'); } finally { button.disabled=false; } });
  }
  async function refresh(force = false) {
    const next = await endpoint('state');
    const signature = JSON.stringify([next.counts,next.job,next.events?.[0]?.id,next.skus]);
    state = next; renderState();
    if (force || signature!==lastState) { lastState=signature; await loadRows(); }
  }
  async function changePage(next) {
    page=next; root=''; folder=''; sku=''; filter='all'; selected=null; resetPage();
    host.querySelectorAll('[data-page]').forEach(b => b.classList.toggle('on',b.dataset.page===page));
    $('v2-tree').hidden=page==='database'; $('v2-sku-list').hidden=page!=='database';
    $('v2-side-title').textContent=page==='database'?'款号集合':'资源库';
    $('v2-tree-label').innerHTML=page==='database'?'SKU 款号 <span>跨目录聚合</span>':'磁盘与文件夹 <span>路径引用</span>';
    $('v2-review-banner').hidden=page!=='review';
    await refresh(true);
  }
  host.addEventListener('click', e => action(async () => {
    const nav=e.target.closest('[data-page]'); if(nav) { await changePage(nav.dataset.page); return; }
    const f=e.target.closest('[data-filter]'); if(f) { filter=f.dataset.filter; page='archive'; root=''; folder=''; sku=''; resetPage(); host.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('on',b===f)); await loadRows(); return; }
    const dir=e.target.closest('[data-root]'); if(dir) { root=dir.dataset.root; folder=dir.dataset.folder; sku=''; filter='all'; resetPage(); renderTree(); await loadRows(); return; }
    const s=e.target.closest('[data-sku]'); if(s) { sku=s.dataset.sku; root=''; folder=''; resetPage(); await loadRows(); renderSkuList(); return; }
    const p=e.target.closest('[data-item]'); if(p) { selected=rows.find(r=>r.id===p.dataset.item); renderPhotos(); renderInspector(); return; }
    const tab=e.target.closest('[data-inspector]'); if(tab) { inspector=tab.dataset.inspector; host.querySelectorAll('[data-inspector]').forEach(b=>b.classList.toggle('on',b===tab)); renderInspector(); }
  }));
  host.addEventListener('dblclick', e => { if(e.target.closest('[data-item]')) { large=true; renderPhotos(); } });
  $('v2-grid-view').onclick=()=>{large=false;renderPhotos();};
  $('v2-loupe-view').onclick=()=>{if(!selected)selected=rows[0];large=true;renderPhotos();renderInspector();};
  $('v2-size').oninput=e=>host.style.setProperty('--v2-thumb',e.target.value+'px');
  let searchTimer;
  $('v2-search').oninput=()=>{ clearTimeout(searchTimer); searchTimer=setTimeout(()=>action(async()=>{sku='';resetPage();renderSkuList();await loadRows();}),220); };
  $('v2-global').onclick=()=>action(async()=>{root='';folder='';sku='';filter='all';resetPage();await loadRows();renderTree();});
  $('v2-prev').onclick=()=>action(async()=>{pageIndex=Math.max(0,pageIndex-1);await loadRows();});
  $('v2-next').onclick=()=>action(async()=>{if(more){cursors[++pageIndex]=nextCursor;await loadRows();}});
  $('v2-scan').onclick=()=>action(async()=>{await endpoint('scan',{});notify('扫描已启动，逐张读取图片文字与已有元数据。');await refresh(true);});
  $('v2-cancel').onclick=()=>action(async()=>{await endpoint('cancel',{});notify('已请求停止，当前图片完成后保留结果。');});
  $('v2-add').onclick=()=>$('v2-add-dialog').showModal();
  $('v2-browse').onclick=()=>action(async()=>{const r=await api('/api/choose-folder');if(r.folder)$('v2-path').value=r.folder;});
  $('v2-add-confirm').onclick=()=>action(async()=>{const button=$('v2-add-confirm');button.disabled=true;$('v2-add-status').textContent='正在建立照片位置索引…';try{const r=await endpoint('add',{folder:$('v2-path').value});$('v2-add-dialog').close();await refresh(true);notify(`已登记 ${r.indexed} 张照片，点击自动解析以读取款号。`);}catch(e){$('v2-add-status').textContent=e.message;}finally{button.disabled=false;}});
  $('v2-design').onclick=()=>$('v2-design-dialog').showModal();
  $('v2-design-close').onclick=()=>$('v2-design-dialog').close();
  $('v2-exit').onclick=()=>FramekeepWorkspace.show('selection');
  document.addEventListener('framekeep:workspace', e=>document.documentElement.classList.toggle('archive-v2-active',e.detail.page==='archive'));
  FramekeepWorkspace.register('archive',$('asset-archive-open'),host,()=>action(()=>refresh(true)));
  window.addEventListener('load',()=>FramekeepWorkspace.show('archive'),{once:true});
  setInterval(()=>{if(FramekeepWorkspace.current()==='archive')action(()=>refresh());},1700);
})();
