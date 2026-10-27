(() => {
  'use strict';
  if(!window.FramekeepIO)return;
  const api=FramekeepIO.api,esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const dialog=document.createElement('dialog');dialog.id='lan-sharing-dialog';dialog.className='retrieval-settings';dialog.setAttribute('aria-label','局域网团队工作台共享设置');document.body.append(dialog);
  const card=document.createElement('section');card.className='workspace-card';card.innerHTML='<header><h3>局域网团队工作台</h3></header><div class="workspace-fields"><p class="workspace-caption">为同事提供拍摄进度、资产检索和资产归档三个标签页，与本机数据同步。</p><button id="lan-sharing-open" class="bordered">管理 Web 工作台共享</button></div>';
  document.getElementById('settings-files')?.append(card);document.getElementById('lan-sharing-open').onclick=()=>open().catch(error=>Framekeep?.notify(error.message));
  const quick=document.createElement('button');quick.id='lan-workspace-open';quick.className='bordered';quick.textContent='Web 共享';quick.onclick=()=>open().catch(error=>Framekeep?.notify(error.message));document.querySelector('.retrieval-heading .retrieval-tools')?.append(quick);
  async function open(){
    const state=await api('/api/lan-workspace'),noCode=state.access_code_required===false;
    dialog.innerHTML=`<header><h2>局域网团队工作台</h2><button id="lan-close">关闭 ×</button></header><p>同事通过浏览器查看拍摄进度、检索和浏览资产。数据直接联动本机，无需手动同步。请保持本工作站与软件运行。</p><label><input id="lan-enabled" type="checkbox" ${state.enabled?'checked':''}> 启用局域网 Web 共享</label><label>共享端口<input id="lan-port" type="number" min="1024" max="65535" value="${state.port||8780}"></label><div class="asset-actions"><button id="lan-save" class="primary">保存共享设置</button> <button id="lan-rotate" ${noCode?'hidden':''} ${!state.enabled?'disabled':''}>更换访问码</button></div><h3>同事访问地址</h3><div id="lan-addresses">${state.enabled?state.addresses.map(address=>`<p><a href="${esc(address)}" target="_blank" rel="noopener noreferrer">${esc(address)}</a> <button data-copy-address="${esc(address)}">复制地址</button></p>`).join(''):'<p>启用共享后显示可用地址。</p>'}</div><p>将局域网 IP 地址发给同事；127.0.0.1 仅供本机测试。</p><section ${noCode?'hidden':''}><label>工作台访问码<input id="lan-code" type="password" readonly value="${esc(state.access_code||'')}" autocomplete="off"></label><div class="asset-actions"><button id="lan-reveal">显示访问码</button> <button id="lan-copy-code">复制访问码</button></div></section>${noCode?'<p>开发阶段无需访问码，打开地址即可进入工作台。</p>':''}<p>同事端只读查阅，不提供标星、修改款号、移动文件或模型配置${noCode?'。':'；更换访问码会撤销此前的登录会话。'}</p><p id="lan-sharing-message" class="retrieval-message" role="status">${esc(state.error||(state.enabled?'共享运行中 · 页面自动同步':'共享已关闭'))}</p>`;
    const $=id=>dialog.querySelector('#'+id),note=text=>$('lan-sharing-message').textContent=text;
    $('lan-close').onclick=()=>dialog.close();$('lan-reveal').onclick=()=>{const show=$('lan-code').type==='password';$('lan-code').type=show?'text':'password';$('lan-reveal').textContent=show?'隐藏访问码':'显示访问码'};
    $('lan-save').onclick=async()=>{const button=$('lan-save');button.disabled=true;try{await api('/api/lan-workspace',{enabled:$('lan-enabled').checked,port:Number($('lan-port').value)});await open()}catch(error){note(error.message)}finally{button.disabled=false}};
    $('lan-rotate').onclick=async()=>{try{await api('/api/lan-workspace',{rotate:true});await open();note('新访问码已生效，原登录会话已退出。')}catch(error){note(error.message)}};
    $('lan-copy-code').onclick=()=>navigator.clipboard.writeText($('lan-code').value).then(()=>note('访问码已复制。')).catch(()=>note('复制不可用，请显示访问码后手动复制。'));
    for(const button of dialog.querySelectorAll('[data-copy-address]'))button.onclick=()=>navigator.clipboard.writeText(button.dataset.copyAddress).then(()=>note('访问地址已复制。')).catch(()=>note('复制不可用，请手动复制地址。'));
    if(!dialog.open)dialog.showModal();
  }
})();
