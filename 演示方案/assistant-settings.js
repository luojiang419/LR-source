(() => {
  'use strict';
  const $=id=>document.getElementById(id),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  if(!window.FramekeepIO){$('assistant-settings').disabled=true;return}
  const api=FramekeepIO.api,dialog=$('assistant-settings-dialog');
  const labels={multimodal:'多模态',ocr:'OCR',text:'文本'},protocols={local:'软件内置模型',responses:'OpenAI Responses',chat:'OpenAI Chat Completions',ocr_json:'OCR JSON',paddleocr:'PaddleOCR 服务'};
  let state={providers:[],selected:{}},category='all',editing=null,chain=Promise.resolve(),discovered=[],discoveryVersion=0,fetching=false;
  dialog.innerHTML='<div class="dialog-heading"><h2 id="assistant-settings-title">AI 助手 · 模型配置</h2><button id="models-close" aria-label="关闭">×</button></div><div id="model-catalog"><section class="model-builtin-switch"><label><input id="model-builtin-enabled" type="checkbox" role="switch">使用内置模型</label><p id="model-builtin-status" class="dialog-copy"></p></section><p class="dialog-copy">按类别添加配置，点击卡片设为当前选用。内置模式当前使用 OCR；多模态作为后续开发预留，外部 API 配置独立保留。</p><div class="model-tabs">'+Object.entries({all:'全部',...labels}).map(([k,v])=>`<button data-model-category="${k}">${v}</button>`).join('')+'</div><div id="model-cards" class="model-cards"></div><details class="model-legacy"><summary>从原 Markdown 配置导入卡片</summary><div class="watch-path-row"><input id="model-legacy-path" placeholder="原 API 配置文件完整路径"><button id="model-legacy-import">导入</button></div></details></div><form id="model-editor" class="hidden"><h3 id="model-editor-title">添加配置</h3><div class="model-fields"><label>配置名称<input id="model-name" required maxlength="100"></label><label>类别<select id="model-category"><option value="multimodal">多模态</option><option value="ocr">OCR</option><option value="text">文本</option></select></label><label>接口协议<select id="model-protocol">'+Object.entries(protocols).map(([k,v])=>`<option value="${k}">${v}</option>`).join('')+'</select></label><label>模型标识<input id="model-id" required placeholder="完整模型名称"></label><label class="model-wide">接口地址<input id="model-url" required type="url" placeholder="https://服务地址/v1"></label><label class="model-wide">API 密钥<input id="model-key" type="password" autocomplete="new-password" placeholder="仅保存到本机，留空保持原密钥"></label></div><p class="dialog-copy">OCR JSON 使用完整接口地址，发送 image_base64/model/prompt，返回 text、codes、confidence、is_label；PaddleOCR 使用 file/fileType 请求。选择与你服务一致的协议。</p><section class="model-discovery" aria-label="获取模型列表"><div class="model-discovery-heading"><strong>可用模型</strong><button id="model-fetch" type="button">获取模型列表</button></div><p class="dialog-copy">填写接口地址和密钥后获取列表，选择模型会填入上方标识；也可手动输入。列表不区分视觉能力，请按服务说明选择。</p><input id="model-search" aria-label="搜索模型" placeholder="搜索模型名称" type="search"><select id="model-discovered" aria-label="可用模型列表" size="5" disabled></select><p id="model-discovery-message" role="status"></p></section><div class="dialog-actions"><button id="model-cancel" type="button">取消</button><button id="model-save" type="submit">保存卡片</button><button id="model-save-select" class="primary" type="submit">保存并选用</button></div></form><p id="model-message" role="status" class="dialog-copy"></p>';
  function render(){
    const locked=state.builtin_enabled===true;$('model-builtin-enabled').checked=locked;
    $('model-builtin-status').textContent=locked?'已使用内置 GLM-OCR；多模态预留，当前不启用或下载。关闭内置模式后可切换外部 API。':'已关闭内置模式，可选择外部 API；内置多模态保留后续开发。';
    document.querySelectorAll('[data-model-category]').forEach(b=>b.classList.toggle('active',b.dataset.modelCategory===category));
    $('model-cards').innerHTML=state.providers.filter(p=>category==='all'||p.category===category).map(p=>`<article class="model-card ${state.selected[p.category]===p.id?'selected':''}" tabindex="${locked||p.reserved?-1:0}" role="button" aria-disabled="${locked||p.reserved}" aria-pressed="${state.selected[p.category]===p.id}" data-model-card="${esc(p.id)}" data-reserved="${!!p.reserved}"><div><strong>${esc(p.name)}</strong><span>${labels[p.category]}</span></div><p>${esc(p.model)}</p><small>${p.reserved?'后续开发预留 · 当前未启用':p.protocol==='local'?'软件直接加载 · 离线运行':esc(p.url)}</small><footer><span>${p.reserved?'预留配置已保留':locked?(state.selected[p.category]===p.id?'内置模式已选用':'内置模式已锁定'):(state.selected[p.category]===p.id?'当前选用':'点击卡片选用')} · ${protocols[p.protocol]}</span><div><button data-model-edit="${esc(p.id)}">编辑</button><button data-model-delete="${esc(p.id)}">删除</button></div></footer></article>`).join('')+'<button id="model-add" class="model-card model-add" type="button">＋ 添加配置</button>';
    for(const button of $('model-cards').querySelectorAll('button'))button.disabled=locked||button.closest('[data-reserved=true]')!==null;
    for(const id of ['model-legacy-import','model-legacy-path','model-lm-preset','model-ocr-preset','analysis-provider','analysis-bind-provider','analysis-test-provider'])if($(id))$(id).disabled=locked||id==='model-lm-preset';
    $('model-add').addEventListener('click',()=>{if(!state.builtin_enabled)editor(null)});
  }
  $('model-builtin-enabled').addEventListener('change',async()=>{const toggle=$('model-builtin-enabled');toggle.disabled=true;try{state=await api('/api/providers/builtin',{enabled:toggle.checked});render();report(state.builtin_enabled?'已开启内置模型':'已关闭内置模型，已恢复原配置选择')}catch(error){toggle.checked=state.builtin_enabled===true;report(error)}finally{toggle.disabled=false}});
  async function load(){state=await api('/api/providers');render()}
  function editor(id){
    if(state.builtin_enabled)return;
    resetDiscovery();report('');editing=state.providers.find(p=>p.id===id)||null;$('model-catalog').classList.add('hidden');$('model-editor').classList.remove('hidden');
    $('model-editor-title').textContent=editing?'编辑配置':'添加配置';
    $('model-name').value=editing?.name||'';$('model-category').value=editing?.category||(category==='all'?'multimodal':category);$('model-category').disabled=!!editing;
    $('model-protocol').value=editing?.protocol||'chat';$('model-id').value=editing?.model||'';$('model-url').value=editing?.url||'';$('model-key').value='';$('model-name').focus();
    window.FramekeepModelAdvanced?.load(editing);localFields();
  }
  function localFields(){
    const local=$('model-protocol').value==='local';
    for(const id of ['model-url','model-key']){$(id).disabled=local;$(id).closest('label').hidden=local}
    $('model-url').required=!local;
    if(local){$('model-url').value='http://127.0.0.1';$('model-id').placeholder=$('model-category').value==='ocr'?'glm-ocr-local':'qwen2.5-vl-7b-q4'}
  }
  $('model-protocol').addEventListener('change',localFields);
  function catalog(){resetDiscovery();$('model-editor').classList.add('hidden');$('model-catalog').classList.remove('hidden');$('model-key').value=''}
  function report(error){$('model-message').textContent=error.message||error}
  $('assistant-settings').addEventListener('click',()=>{catalog();dialog.showModal();$('model-message').textContent='';load().catch(report)});
  $('models-close').addEventListener('click',()=>{dialog.close();$('model-key').value=''});
  dialog.addEventListener('close',()=>{resetDiscovery();$('model-key').value=''});
  document.querySelectorAll('[data-model-category]').forEach(b=>b.addEventListener('click',()=>{category=b.dataset.modelCategory;render()}));
  $('model-cancel').addEventListener('click',catalog);
  $('model-editor').addEventListener('submit',async e=>{
    e.preventDefault();const use=e.submitter?.id==='model-save-select';$('model-save').disabled=true;$('model-save-select').disabled=true;
    try{state=await api('/api/providers/save',{...(editing?{id:editing.id}:{}),...window.FramekeepModelAdvanced?.values(),select:use,name:$('model-name').value,category:$('model-category').value,protocol:$('model-protocol').value,model:$('model-id').value,url:$('model-url').value,key:$('model-key').value});catalog();render();report(use?'已保存并设为当前类别选用':'已保存；点击卡片设为选用')}
    catch(error){report(error)}finally{$('model-save').disabled=false;$('model-save-select').disabled=false}
  });
  function drawModels(){
    const query=$('model-search').value.trim().toLowerCase(),rows=discovered.filter(id=>id.toLowerCase().includes(query)),list=$('model-discovered');
    list.replaceChildren();for(const id of rows){const option=document.createElement('option');option.value=id;option.textContent=id;list.append(option)}
    list.disabled=!rows.length;list.value=$('model-id').value;
    if(!rows.length){const option=document.createElement('option');option.textContent=discovered.length?'没有匹配的模型':'获取列表后在此选择模型';list.append(option)}
  }
  function resetDiscovery(){discoveryVersion++;fetching=false;discovered=[];$('model-fetch').disabled=false;$('model-search').value='';$('model-discovery-message').textContent='';drawModels()}
  for(const id of ['model-url','model-key','model-protocol'])$(id).addEventListener(id==='model-protocol'?'change':'input',resetDiscovery);
  $('model-search').addEventListener('input',drawModels);
  $('model-discovered').addEventListener('change',()=>{const id=$('model-discovered').value;if(!discovered.includes(id))return;$('model-id').value=id;if(!$('model-name').value.trim())$('model-name').value=id.slice(0,100);$('model-discovery-message').textContent='已填入模型，保存卡片或保存并选用即可'});
  $('model-fetch').addEventListener('click',async()=>{
    if(fetching)return;const version=++discoveryVersion;fetching=true;$('model-fetch').disabled=true;$('model-discovery-message').textContent='正在获取模型列表…';
    try{const result=await api('/api/providers/models',{...(editing?{id:editing.id}:{}),protocol:$('model-protocol').value,url:$('model-url').value,key:$('model-key').value});if(version!==discoveryVersion)return;discovered=result.models;$('model-search').value='';drawModels();$('model-discovery-message').textContent=result.message}
    catch(error){if(version===discoveryVersion)$('model-discovery-message').textContent=error.message}
    finally{if(version===discoveryVersion){fetching=false;$('model-fetch').disabled=false}}
  });
  function select(id){if(state.builtin_enabled||state.providers.find(p=>p.id===id)?.reserved)return;chain=chain.catch(()=>{}).then(async()=>{if(state.builtin_enabled)return;state=await api('/api/providers/select',{id});render();report('选用已保存')}).catch(report)}
  $('model-cards').addEventListener('click',async e=>{
    if(state.builtin_enabled)return;
    const edit=e.target.closest('[data-model-edit]'),remove=e.target.closest('[data-model-delete]'),card=e.target.closest('[data-model-card]');
    if(edit){editor(edit.dataset.modelEdit);return}
    if(remove){try{state=await api('/api/providers/delete',{id:remove.dataset.modelDelete});render();report('已删除；未自动改选其他配置')}catch(error){report(error)}return}
    if(card)select(card.dataset.modelCard);
  });
  $('model-cards').addEventListener('keydown',e=>{if(state.builtin_enabled)return;if(e.target.closest('button'))return;const card=e.target.closest('[data-model-card]');if(card&&(e.key==='Enter'||e.key===' ')){e.preventDefault();select(card.dataset.modelCard)}});
  $('model-legacy-import').addEventListener('click',async()=>{try{state=await api('/api/providers/import-legacy',{path:$('model-legacy-path').value});render();report('已导入卡片，请点击设为选用')}catch(error){report(error)}});
})();
