const metadata=new Map();
async function info(file){
  const response=await fetch('/__demo_assets/info?'+new URLSearchParams({file}),{cache:'no-store'});
  let result;try{result=await response.json()}catch{throw Error('请在本机演示服务中使用文件路径操作')}
  if(!response.ok)throw Error(result.error||'无法读取图片路径');
  metadata.set(file,result);return result;
}
async function imageBlob(file){
  const response=await fetch('/assets/'+encodeURIComponent(file));
  if(!response.ok)throw Error('图片读取失败，请刷新后重试');
  return new Blob([await response.arrayBuffer()],{type:'image/png'});
}
export async function performImageAction(action,row){
  if(action==='copy-image'){
    if(!navigator.clipboard?.write||typeof ClipboardItem==='undefined')throw Error('当前浏览器不支持复制图片，请使用另存图片');
    // 在点击事件内启动 write，PNG 数据通过 Promise 提供，保留用户激活。
    await navigator.clipboard.write([new ClipboardItem({'image/png':imageBlob(row.file)})]);
    return `${row.sku} 图片已复制`;
  }
  if(action==='copy-path'){
    if(!navigator.clipboard?.writeText)throw Error('当前浏览器不支持复制路径，请使用本机浏览器');
    const value=await info(row.file);await navigator.clipboard.writeText(value.path);
    return `${row.sku} 路径已复制`;
  }
  if(action==='open-path'){
    const value=metadata.get(row.file)||await info(row.file);
    const response=await fetch('/__demo_assets/open?'+new URLSearchParams({file:row.file}),{method:'POST',headers:{'X-Demo-Asset-Token':value.token}});
    const result=await response.json();if(!response.ok)throw Error(result.error||'无法打开图片路径');
    return `已在资源管理器中定位 ${row.sku}`;
  }
  if(action==='save-image'){
    const options={suggestedName:row.sku+'.png',types:[{description:'PNG 图片',accept:{'image/png':['.png']}}]};
    // 支持的浏览器提供真正的另存对话框；其他浏览器走原图下载。
    const handle=window.showSaveFilePicker?await window.showSaveFilePicker(options):null;
    const blob=await imageBlob(row.file);
    if(handle){const writer=await handle.createWritable();try{await writer.write(blob);await writer.close()}catch(error){try{await writer.abort()}catch{}throw error}return `${row.sku} 图片已保存`}
    const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=options.suggestedName;
    document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);
    return `已开始下载 ${options.suggestedName}`;
  }
  throw Error('不支持的图片操作');
}
