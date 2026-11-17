/* Web 快捷搜索：沿用快捷窗口的居中输入、底部停靠与键盘选择。 */
(() => {
  'use strict';
  const esc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  window.FramekeepLanQuickSearch={mount(root,{api,isActive,open}){
    root.classList.add('lan-quick');
    root.innerHTML=`<div class="lan-quick-backdrop" aria-hidden="true"><div class="lan-quick-background"><img alt=""></div><div class="lan-quick-background"><img alt=""></div></div><div class="lan-quick-wash" aria-hidden="true"></div>
      <header id="quick-heading" class="lan-quick-heading" hidden><p>搜索结果</p><h1 id="quick-query-text"></h1><div id="quick-status" role="status" aria-live="polite"></div></header>
      <div id="quick-results" class="lan-quick-results" hidden><div id="quick-grid" class="lan-quick-grid" role="listbox" aria-label="快捷搜索结果"></div><button id="quick-more" class="bordered" type="button" hidden>加载更多</button></div>
      <form id="quick-form" class="lan-quick-form" role="search" aria-label="快捷资产搜索"><svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/></svg><input id="quick-query" type="search" maxlength="300" autocomplete="off" placeholder="搜索文件名、SKU，或输入自然语言条件" aria-label="快捷搜索文件名、SKU 或画面条件" aria-controls="quick-grid"><button id="quick-clear" type="button" aria-label="清空快捷搜索" hidden>×</button><button id="quick-submit" type="submit" aria-label="执行快捷搜索">搜索</button></form>
      <p class="lan-quick-hint">↑ ↓ 选择 · Enter / 双击查看 · Esc 清空</p>`;
    const find=id=>root.querySelector('#'+id),input=find('quick-query'),grid=find('quick-grid'),results=find('quick-results'),status=find('quick-status');
    const backgrounds=[...root.querySelectorAll('.lan-quick-background')];
    let rows=[],cursor='',hasMore=false,selected=-1,sequence=0,controller=null,timer=null,retry=null,composing=false,loading=false,submitted='',backgroundKey='',backgroundSequence=0,backgroundSlot=0;
    function heading(){const query=input.value.trim();root.classList.toggle('has-query',!!query);find('quick-heading').hidden=!query;results.hidden=!query;find('quick-clear').hidden=!query;find('quick-query-text').textContent=query;find('quick-query-text').title=query;}
    function note(text,error=false){status.textContent=text;status.classList.toggle('error',error);}
    function cancel(){clearTimeout(timer);clearTimeout(retry);++sequence;controller?.abort();controller=null;loading=false;root.removeAttribute('aria-busy');}
    function clearBackground(){++backgroundSequence;backgroundKey='';for(const layer of backgrounds){layer.classList.remove('is-active');layer.firstElementChild.removeAttribute('src');}}
    function background(preserve=false){
      if(!rows.length){clearBackground();return;}
      if(preserve&&rows.some(row=>row.id===backgroundKey))return;
      const candidates=rows.filter(row=>row.preview),start=Math.floor(Math.random()*Math.max(1,candidates.length)),request=++backgroundSequence;
      function attempt(offset){
        if(request!==backgroundSequence||offset>=Math.min(3,candidates.length)){if(request===backgroundSequence&&!preserve)clearBackground();return;}
        const item=candidates[(start+offset)%candidates.length],image=new Image(),url=new URL(item.preview,location.href);url.searchParams.set('size','1280');
        image.onload=()=>{if(request!==backgroundSequence)return;const next=1-backgroundSlot,layer=backgrounds[next],target=layer.firstElementChild;target.src=image.src;target.hidden=false;layer.dataset.item=item.id;layer.classList.add('is-active');backgrounds[backgroundSlot].classList.remove('is-active');backgroundSlot=next;backgroundKey=item.id;};
        image.onerror=()=>{if(request===backgroundSequence)attempt(offset+1)};image.src=url.href;
      }
      attempt(0);
    }
    function select(index,{scroll=true}={}){
      if(!rows.length){selected=-1;input.removeAttribute('aria-activedescendant');return;}
      selected=Math.max(0,Math.min(rows.length-1,index));
      for(const card of grid.children){const active=Number(card.dataset.quickResult)===selected;card.classList.toggle('active',active);card.setAttribute('aria-selected',String(active));card.tabIndex=active?0:-1;}
      const card=grid.children[selected];input.setAttribute('aria-activedescendant',card.id);if(scroll)card.scrollIntoView({block:'nearest',behavior:'instant'});
    }
    function paint(){
      grid.innerHTML=rows.map((row,index)=>`<button type="button" id="quick-result-${index}" class="lan-quick-card" role="option" aria-selected="false" tabindex="-1" data-quick-result="${index}" title="双击查看 ${esc(row.name)}"><span class="lan-quick-preview"><img src="${esc(row.preview)}&amp;size=420" alt="${esc(row.name)}" loading="lazy" decoding="async"><span class="lan-quick-format">${esc(row.media_format||'图片')}</span></span><span class="lan-quick-copy">${row.sku?`<strong>${esc(row.sku)}</strong>`:''}<span>${esc(row.name)}</span><small title="${esc(row.folder||'')}">${esc(row.folder||'归档素材')}</small></span></button>`).join('');
      if(!rows.length)grid.innerHTML='<p class="lan-quick-empty">没有找到匹配素材，试试其他款号或关键词。</p>';
      find('quick-more').hidden=!hasMore;select(selected<0?0:selected,{scroll:false});
    }
    async function search(append=false,{preserve=false}={}){
      clearTimeout(timer);clearTimeout(retry);if(composing||!isActive())return;
      const query=input.value.trim();heading();if(!query){reset();return;}
      if(append&&(loading||!hasMore||query!==submitted))return;
      controller?.abort();controller=new AbortController();const request=++sequence;loading=true;root.setAttribute('aria-busy','true');note(append?'正在加载更多…':'正在检索…');find('quick-more').hidden=true;
      if(!append&&query!==submitted){rows=[];selected=-1;grid.replaceChildren();results.scrollTop=0;}
      try{
        const data=await api('/api/search?'+new URLSearchParams({q:query,quick:'1',cursor:append?cursor:''}),undefined,controller.signal);
        if(request!==sequence||query!==input.value.trim()||!isActive())return;
        rows=append?[...rows,...data.rows.filter(row=>!rows.some(prior=>prior.id===row.id))]:data.rows;cursor=data.next_cursor||'';hasMore=!!data.has_more;submitted=query;paint();
        note(data.indexing?'正在准备索引，结果就绪后自动显示…':`${rows.length} 项${hasMore?' · 还有更多':''}${data.elapsed_ms!==undefined?' · '+data.elapsed_ms+' ms':''}${data.incomplete?' · 可缩小关键词范围':''}`);
        if(data.indexing)grid.innerHTML='<p class="lan-quick-empty">正在准备检索索引…</p>';
        if(!append)background(preserve);
        if(data.indexing)retry=setTimeout(()=>search(false,{preserve:true}),600);
      }catch(error){if(request===sequence&&error.name!=='AbortError'){note(error.name==='TimeoutError'?'检索超时，请按 Enter 重试':error.message,true);}}
      finally{if(request===sequence){loading=false;root.removeAttribute('aria-busy');find('quick-more').hidden=!hasMore;}}
    }
    function reset(){cancel();input.value='';rows=[];cursor='';hasMore=false;selected=-1;submitted='';grid.replaceChildren();input.removeAttribute('aria-activedescendant');find('quick-more').hidden=true;note('');heading();}
    function changed(){cancel();heading();if(!input.value.trim()){reset();return;}rows=[];selected=-1;hasMore=false;input.removeAttribute('aria-activedescendant');grid.replaceChildren();note('正在检索…');if(!composing)timer=setTimeout(()=>search(),90);}
    input.addEventListener('input',event=>{if(!event.isComposing&&!composing)changed()});
    input.addEventListener('compositionstart',()=>{composing=true;cancel()});input.addEventListener('compositionend',()=>{composing=false;changed()});
    find('quick-form').onsubmit=event=>{event.preventDefault();if(composing)return;if(selected>=0&&submitted===input.value.trim())open(rows[selected]);else search();};
    find('quick-clear').onclick=()=>{reset();input.focus()};find('quick-more').onclick=()=>search(true);
    grid.onclick=event=>{const card=event.target.closest('[data-quick-result]');if(card){select(Number(card.dataset.quickResult),{scroll:false});if(event.pointerType==='touch')open(rows[selected]);}};
    grid.ondblclick=event=>{const card=event.target.closest('[data-quick-result]');if(card)open(rows[Number(card.dataset.quickResult)]);};
    root.addEventListener('keydown',event=>{
      if(composing||event.isComposing)return;
      if(event.key==='Escape'){event.preventDefault();reset();input.focus();return;}
      if(event.key==='Enter'&&event.target!==input&&event.target.closest('[data-quick-result]')){event.preventDefault();open(rows[Number(event.target.closest('[data-quick-result]').dataset.quickResult)]);return;}
      if(!['ArrowDown','ArrowUp','ArrowLeft','ArrowRight'].includes(event.key)||!rows.length||event.target===input&&['ArrowLeft','ArrowRight'].includes(event.key))return;
      event.preventDefault();const columns=getComputedStyle(grid).gridTemplateColumns.split(' ').filter(Boolean).length||1;
      select(selected+(event.key==='ArrowDown'?columns:event.key==='ArrowUp'?-columns:event.key==='ArrowRight'?1:-1));
    });
    results.addEventListener('scroll',()=>{if(hasMore&&!loading&&results.scrollHeight-results.scrollTop-results.clientHeight<160)search(true)});
    window.addEventListener('pagehide',()=>{cancel();clearBackground()},{once:true});
    return {activate(){heading();requestAnimationFrame(()=>{if(isActive())input.focus({preventScroll:true})})},refresh(){if(input.value.trim())return search(false,{preserve:true})},suspend(){cancel()},reset(){reset();clearBackground()}};
  }};
})();
