import {useEffect,useMemo,useRef,useState} from 'react';
import {IconSearch,IconX,IconRefresh,IconClipboard,IconUpload,IconFilter,IconChevronDown,IconArrowLeft,IconArrowRight,IconPhoto,IconCheck,IconCopy,IconLink,IconFolderOpen,IconDownload} from '@tabler/icons-react';
import {performImageAction} from './image-actions.js';

const catalog=[
 {sku:'LC1234',name:'亚麻衬衫 · 正面',file:'ivory-shirt-square.png',score:98,scope:'studio',words:'米白 象牙白 亚麻 衬衫'},
 {sku:'LC5678',name:'棉质衬衫 · 正面',file:'cotton-shirt.png',score:92,scope:'studio',words:'暖白 棉质 衬衫'},
 {sku:'LC9012',name:'棕色衬衫 · 正面',file:'brown-shirt-square.png',score:89,scope:'library',words:'棕色 长袖 衬衫'},
 {sku:'LC3456',name:'休闲短外套 · 正面',file:'cream-jacket-square.png',score:87,scope:'studio',words:'奶油白 外套 双贴袋'},
 {sku:'LC7890',name:'亚麻衬衫 · 细条纹',file:'linen-shirt.png',score:85,scope:'library',words:'米色 亚麻 衬衫 细条纹'},
 {sku:'LC2468',name:'条纹衬衫 · 正面',file:'striped-shirt.png',score:82,scope:'studio',words:'白色 长袖 衬衫 条纹'},
];
const asset=file=>'/assets/'+file,themeNames={ivory:'象牙白',white:'醇白',dark:'星夜黑',hardcore:'硬核工作台'},features=new Map();
async function feature(src){
 if(features.has(src))return features.get(src);
 const image=new Image();image.src=src;await image.decode();
 const canvas=document.createElement('canvas');canvas.width=32;canvas.height=32;
 const ctx=canvas.getContext('2d',{willReadFrequently:true});ctx.drawImage(image,image.width*.2,image.height*.15,image.width*.6,image.height*.7,0,0,32,32);
 const pixels=ctx.getImageData(0,0,32,32).data,value=[0,0,0];
 for(let i=0;i<pixels.length;i+=4)for(let c=0;c<3;c++)value[c]+=pixels[i+c]/1024;
 features.set(src,value);return value;
}
function IconButton({label,children,className='',...props}){return <button type="button" className={'icon-button '+className} aria-label={label} title={label} {...props}>{children}</button>}

export function App(){
 const [opened,setOpened]=useState(true),[query,setQuery]=useState(''),[submitted,setSubmitted]=useState(''),[rows,setRows]=useState(catalog);
 const [reference,setReference]=useState({src:asset('ivory-shirt.png'),name:'象牙白亚麻衬衫'}),[scope,setScope]=useState('all');
 const [threshold,setThreshold]=useState(50),[sort,setSort]=useState('similarity'),[filters,setFilters]=useState(false),[appearance,setAppearance]=useState(false);
 const [loading,setLoading]=useState(false),[selected,setSelected]=useState(null),[preview,setPreview]=useState(null),[notice,setNotice]=useState(''),[dragging,setDragging]=useState(false);
 const [imageAction,setImageAction]=useState('');
 const [theme,setTheme]=useState('ivory'),[background,setBackground]=useState(true),[blur,setBlur]=useState(6),[brightness,setBrightness]=useState(68);
 const input=useRef(null),fileInput=useRef(null),modal=useRef(null),filterRef=useRef(null),appearanceRef=useRef(null),previewClose=useRef(null);
 const uploadUrl=useRef(null),ticket=useRef(0),timer=useRef(null),busy=useRef(false);
 const visible=useMemo(()=>rows.filter(row=>row.score>=threshold&&(scope==='all'||row.scope===scope)&&(!submitted||(row.sku+' '+row.name+' '+row.words).toLowerCase().includes(submitted.toLowerCase()))).sort((a,b)=>sort==='sku'?a.sku.localeCompare(b.sku):b.score-a.score),[rows,threshold,scope,submitted,sort]);
 function notify(text){setNotice(text);clearTimeout(timer.current);timer.current=setTimeout(()=>setNotice(''),4000)}
 async function search(event){event?.preventDefault();const current=++ticket.current;setLoading(true);setSelected(null);await new Promise(r=>setTimeout(r,280));if(current===ticket.current){setSubmitted(query.trim());setLoading(false)}}
 async function useFile(file){
  if(!file)return;if(!file.type.startsWith('image/')){notify('请选择 JPG、PNG、WebP 等图片文件');return}if(busy.current)return;busy.current=true;
  const current=++ticket.current,url=URL.createObjectURL(file);setLoading(true);
  try{
   const value=await feature(url),scores=await Promise.all(catalog.map(async row=>{const candidate=await feature(asset(row.file));const distance=Math.sqrt(candidate.reduce((sum,v,i)=>sum+(v-value[i])**2,0));return {...row,score:Math.max(50,Math.round(100-Math.min(distance/180,1)*50))}}));
   if(current!==ticket.current){features.delete(url);URL.revokeObjectURL(url);return}
   if(uploadUrl.current){features.delete(uploadUrl.current);URL.revokeObjectURL(uploadUrl.current)}uploadUrl.current=url;
   setReference({src:url,name:file.name});setRows(scores);setSelected(null);setQuery('');setSubmitted('');notify('已载入参考图片，按示例图库的配色相似度排序');
  }catch{features.delete(url);URL.revokeObjectURL(url);notify('图片无法读取，请换一张 JPG 或 PNG 图片')}
  finally{busy.current=false;if(current===ticket.current)setLoading(false);if(fileInput.current)fileInput.current.value=''}
 }
 async function paste(){try{if(!navigator.clipboard?.read)throw Error();for(const item of await navigator.clipboard.read()){const type=item.types.find(t=>t.startsWith('image/'));if(type){await useFile(new File([await item.getType(type)],'粘贴的参考图片.png',{type}));return}}notify('剪贴板里没有图片，请先复制图片，或拖入文件')}catch{notify('请在窗口内按 Ctrl+V 粘贴图片，也可以拖入图片文件')}}
 function showPreview(row){if(row){setSelected(row.sku);setPreview(row)}}
 async function actOnImage(action,row){
  if(imageAction)return;setImageAction(action);
  try{notify(await performImageAction(action,row))}catch(error){if(error.name!=='AbortError')notify(error.name==='NotAllowedError'?(action.startsWith('copy-')?'浏览器未允许写入剪贴板，请允许后重试':'浏览器未允许保存文件，请允许后重试'):error.message||'图片操作失败，请重试')}finally{setImageAction('')}
 }
 useEffect(()=>{if(preview)previewClose.current?.focus();else if(opened)input.current?.focus()},[opened,preview]);
 useEffect(()=>()=>{if(uploadUrl.current)URL.revokeObjectURL(uploadUrl.current);clearTimeout(timer.current);ticket.current++},[]);
 useEffect(()=>{function outside(e){if(filterRef.current&&!filterRef.current.contains(e.target))setFilters(false);if(appearanceRef.current&&!appearanceRef.current.contains(e.target))setAppearance(false)}document.addEventListener('pointerdown',outside);return()=>document.removeEventListener('pointerdown',outside)},[]);
 useEffect(()=>{
  function onPaste(e){if(!opened||preview)return;const file=[...(e.clipboardData?.items||[])].find(item=>item.type.startsWith('image/'))?.getAsFile();if(file){e.preventDefault();useFile(file)}}
  function onKey(e){
   if(e.ctrlKey&&e.shiftKey&&e.code==='Space'){e.preventDefault();setOpened(v=>!v);return}
   if(e.key==='Escape'){if(preview)setPreview(null);else if(filters)setFilters(false);else if(appearance)setAppearance(false);else setOpened(false);return}if(!opened)return;
   if(e.key==='Tab'){const container=preview?document.querySelector('.image-viewer'):modal.current;const focusable=[...container.querySelectorAll('button,input,select,[tabindex="0"]')].filter(el=>!el.disabled&&el.getClientRects().length);if(focusable.length&&!container.contains(document.activeElement)){e.preventDefault();(e.shiftKey?focusable.at(-1):focusable[0]).focus()}else if(e.shiftKey&&document.activeElement===focusable[0]){e.preventDefault();focusable.at(-1)?.focus()}else if(!e.shiftKey&&document.activeElement===focusable.at(-1)){e.preventDefault();focusable[0]?.focus()}}
   if(preview){if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();const i=visible.findIndex(row=>row.sku===preview.sku);setPreview(visible[(i+(e.key==='ArrowRight'?1:-1)+visible.length)%visible.length]||preview)}return}
   if(e.target.matches('select,input[type=range]'))return;
   if(['ArrowDown','ArrowUp','ArrowLeft','ArrowRight'].includes(e.key)&&visible.length){if(e.target.matches('input')&&['ArrowLeft','ArrowRight'].includes(e.key))return;e.preventDefault();const i=visible.findIndex(row=>row.sku===selected),step=e.key==='ArrowDown'?3:e.key==='ArrowUp'?-3:e.key==='ArrowRight'?1:-1,next=i<0?(step>0?0:visible.length-1):(i+step+visible.length)%visible.length;setSelected(visible[next].sku);document.querySelector(`[data-sku="${visible[next].sku}"]`)?.scrollIntoView({block:'nearest'})}
   if(e.key==='Enter'&&selected&&!e.target.closest('.filter-popover,.appearance-popover')&&(!e.target.closest('button')||e.target.closest('.result-item'))){e.preventDefault();showPreview(visible.find(row=>row.sku===selected))}
  }
  document.addEventListener('keydown',onKey);document.addEventListener('paste',onPaste);return()=>{document.removeEventListener('keydown',onKey);document.removeEventListener('paste',onPaste)};
 },[opened,selected,visible,preview,filters,appearance]);
 return <main className="demo" data-theme={theme} style={{'--scene-blur':background?`${blur}px`:'0px','--scene-brightness':background?brightness/100:1}}>
  <img className="workspace-background" src={asset('workspace-background.png')} alt="" aria-hidden="true"/>
  {opened&&<div className="modal-position" onPointerDown={e=>{if(e.target===e.currentTarget)setOpened(false)}}><section className={'search-modal'+(dragging?' is-dragging':'')} role="dialog" aria-modal="true" aria-label="以图搜图" ref={modal}
   onDragOver={e=>{if(e.dataTransfer.types.includes('Files')){e.preventDefault();setDragging(true)}}} onDragLeave={e=>{if(!e.currentTarget.contains(e.relatedTarget))setDragging(false)}} onDrop={e=>{e.preventDefault();setDragging(false);useFile(e.dataTransfer.files[0])}}>
   <header className="modal-header"><form className="query-form" onSubmit={search}><label className="query-field"><IconSearch size={20} stroke={1.6}/><input ref={input} aria-label="补充关键词或 SKU" placeholder="补充关键词或 SKU…" value={query} onChange={e=>{setQuery(e.target.value);setSelected(null)}}/>{query&&<IconButton label="清空关键词" onClick={()=>{setQuery('');setSubmitted('');input.current?.focus()}}><IconX size={15}/></IconButton>}</label><button className="search-button" type="submit" disabled={loading}>搜索</button></form><IconButton label="关闭快捷搜索" onClick={()=>setOpened(false)}><IconX size={21} stroke={1.6}/></IconButton></header>
   <div className="modal-body"><aside className="reference-column" aria-label="参考图片"><h2>参考图片</h2><button className={"reference-image"+(reference.src.startsWith("blob:")?" is-uploaded":"")} onClick={()=>fileInput.current?.click()} title={reference.name+' · 点击更换'} aria-label="更换搜索参考图片"><img src={reference.src} alt={reference.name}/></button><div className="reference-actions"><button onClick={()=>fileInput.current?.click()}><IconRefresh size={19} stroke={1.6}/>更换</button><button onClick={paste}><IconClipboard size={19} stroke={1.6}/>粘贴</button></div><button className="drop-action" onClick={()=>fileInput.current?.click()}><IconUpload size={19} stroke={1.6}/>{dragging?'松开图片，开始检索':'拖入图片也可以'}</button><input ref={fileInput} type="file" accept="image/*" hidden aria-label="上传参考图片" onChange={e=>useFile(e.target.files[0])}/></aside>
    <section className="results-column" aria-label="相似图片"><div className="results-heading"><h2>相似图片<span className="result-count" aria-label={loading?"正在搜索":`匹配 ${visible.length} 张图片`}>{loading?"搜索中…":`${visible.length} 项`}</span></h2><div className="results-actions"><label className="scope-select"><select aria-label="归档范围" value={scope} onChange={e=>{setScope(e.target.value);setSelected(null)}}><option value="all">全部归档</option><option value="studio">摄影棚</option><option value="library">服装资料</option></select><IconChevronDown size={16} stroke={1.6}/></label><div className="filter-anchor" ref={filterRef}><IconButton label="筛选相似图片" aria-expanded={filters} className={'filter-button'+(threshold!==50||sort!=='similarity'?' active':'')} onClick={()=>setFilters(v=>!v)}><IconFilter size={21} stroke={1.6}/></IconButton>{filters&&<div className="filter-popover" role="dialog" aria-label="相似图片筛选"><h3>筛选相似图片</h3><label className="range-label">最低相似度<output>{threshold}%</output><input type="range" min="50" max="100" value={threshold} onChange={e=>{setThreshold(Number(e.target.value));setSelected(null)}} aria-label="最低相似度"/></label><label>排序方式<select value={sort} onChange={e=>setSort(e.target.value)}><option value="similarity">相似度从高到低</option><option value="sku">款号顺序</option></select></label><div className="popover-footer"><button onClick={()=>{setThreshold(50);setSort('similarity')}}>重置</button><button className="done" onClick={()=>setFilters(false)}><IconCheck size={16}/>完成</button></div></div>}</div></div></div>
     <div className={'results-gallery'+(loading?' is-loading':'')} role="listbox" aria-label="搜索结果" aria-busy={loading}>{visible.map(row=><button type="button" className={'result-item'+(selected===row.sku?' selected':'')} key={row.sku} data-sku={row.sku} role="option" aria-selected={selected===row.sku} aria-label={`${row.sku}，${row.name}，相似度 ${row.score}%`} onClick={()=>setSelected(row.sku)} onDoubleClick={()=>showPreview(row)}><div className="result-image"><img src={asset(row.file)} alt={row.name}/></div><span className="result-copy"><strong>{row.sku}</strong><span>{row.score}%</span></span></button>)}{!visible.length&&<div className="empty-results"><IconPhoto size={36} stroke={1.3}/><h3>没有找到相似图片</h3><p>试试更短的关键词，或放宽筛选条件。</p><button onClick={()=>{setQuery('');setSubmitted('');setThreshold(50);setScope('all')}}>清除筛选</button></div>}</div><div className="sr-only" role="status" aria-live="polite">{loading?'正在搜索相似图片':`找到 ${visible.length} 张相似图片`}</div>
    </section></div><footer className="modal-footer"><span>Enter 预览</span><span className="footer-dot">·</span><span>Esc 关闭</span></footer>
  </section></div>}
  {!opened&&<button className="reopen-button" onClick={()=>setOpened(true)}><IconSearch size={20}/>打开快捷搜索<span>Ctrl ⇧ Space</span></button>}
  <div className="demo-note"><span>交互演示 · 示例图库</span><div className="appearance-anchor" ref={appearanceRef}><button className="appearance-trigger" onClick={()=>setAppearance(v=>!v)} aria-expanded={appearance}>外观</button>{appearance&&<div className="appearance-popover" role="dialog" aria-label="演示外观设置"><h3>外观</h3><label>软件主题<select value={theme} onChange={e=>setTheme(e.target.value)}>{Object.entries(themeNames).map(([value,name])=><option key={value} value={value}>{name}</option>)}</select></label><label className="checkbox-label"><input type="checkbox" checked={background} onChange={e=>setBackground(e.target.checked)}/>模糊屏幕背景</label><label className="range-label">模糊度<output>{blur} px</output><input aria-label="背景模糊度" type="range" min="0" max="24" disabled={!background} value={blur} onChange={e=>setBlur(Number(e.target.value))}/></label><label className="range-label">背景亮度<output>{brightness}%</output><input aria-label="背景亮度" type="range" min="20" max="100" disabled={!background} value={brightness} onChange={e=>setBrightness(Number(e.target.value))}/></label></div>}</div></div>
  {notice&&<div className={"toast"+(preview?" viewer-toast":"")} role="status">{notice}</div>}
  {preview&&<div className="viewer-backdrop" onClick={e=>{if(e.target===e.currentTarget)setPreview(null)}}><section className="image-viewer" role="dialog" aria-modal="true" aria-label={'预览 '+preview.sku}><header><div><strong>{preview.sku}</strong><span>{preview.name}</span></div><button ref={previewClose} className="icon-button" aria-label="关闭图片预览" onClick={()=>setPreview(null)}><IconX size={24} stroke={1.6}/></button></header><img src={asset(preview.file)} alt={preview.name}/><footer className="viewer-footer"><div className="viewer-actions" role="group" aria-label="图片操作">{[['copy-image','复制图片',IconCopy],['copy-path','复制路径',IconLink],['open-path','打开路径',IconFolderOpen],['save-image','另存图片',IconDownload]].map(([action,label,Icon])=><button type="button" key={action} disabled={!!imageAction} aria-busy={imageAction===action} onClick={()=>actOnImage(action,preview)}><Icon size={17} stroke={1.6}/>{label}</button>)}</div><div className="viewer-navigation" role="group" aria-label="图片浏览"><IconButton label="上一张图片" onClick={()=>{const i=visible.indexOf(preview);setPreview(visible[(i-1+visible.length)%visible.length])}}><IconArrowLeft size={20}/></IconButton><span>{visible.findIndex(row=>row.sku===preview.sku)+1} / {visible.length}</span><IconButton label="下一张图片" onClick={()=>{const i=visible.indexOf(preview);setPreview(visible[(i+1)%visible.length])}}><IconArrowRight size={20}/></IconButton></div></footer></section></div>}
 </main>;
}
