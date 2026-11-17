// 本地演示专用：仅允许定位示例结果图片，不接受任意文件路径。
import { realpath, stat } from 'node:fs/promises';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';

const allowed=new Set(['ivory-shirt-square.png','cotton-shirt.png','brown-shirt-square.png','cream-jacket-square.png','linen-shirt.png','striped-shirt.png']);
async function openInExplorer(file){
  if(process.platform!=='win32')throw Error('打开路径仅支持本机 Windows 演示');
  await new Promise((resolve,reject)=>{
    const child=spawn(path.join(process.env.SystemRoot||'C:\\Windows','explorer.exe'),[`/select,${file}`],{windowsHide:true,stdio:'ignore',detached:true});
    child.once('error',reject);child.once('spawn',()=>{child.unref();resolve()});
  });
}
export function createAssetMiddleware(root,{openFile=openInExplorer}={}){
  const token=randomUUID();
  return async function localAssets(request,response,next){
    const url=new URL(request.url,'http://127.0.0.1');
    if(!url.pathname.startsWith('/__demo_assets/'))return next();
    const reply=(status,value)=>{response.statusCode=status;response.setHeader('Content-Type','application/json; charset=utf-8');response.setHeader('Cache-Control','no-store');response.end(JSON.stringify(value))};
    const host=request.headers.host||'',origin=request.headers.origin;
    if(!/^(127\.0\.0\.1|localhost):\d+$/.test(host)||
       (origin&&origin!==`http://${host}`)||request.headers['sec-fetch-site']==='cross-site')return reply(403,{error:'只允许本机演示页面访问'});
    if(!['/__demo_assets/info','/__demo_assets/open'].includes(url.pathname))return reply(404,{error:'接口不存在'});
    const file=url.searchParams.get('file');
    if(!allowed.has(file))return reply(400,{error:'无效的示例图片'});
    try{
      const directory=await realpath(path.join(root,'public','assets'));
      const actual=await realpath(path.join(directory,file));
      if(path.dirname(actual)!==directory||!(await stat(actual)).isFile())return reply(403,{error:'图片不在示例图库中'});
      if(url.pathname.endsWith('/info')){
        if(request.method!=='GET')return reply(405,{error:'请求方法无效'});
        return reply(200,{path:actual,token});
      }
      if(request.method!=='POST')return reply(405,{error:'请求方法无效'});
      if(origin!==`http://${host}`||request.headers['x-demo-asset-token']!==token)return reply(403,{error:'页面验证已失效，请刷新后重试'});
      await openFile(actual);return reply(200,{opened:true});
    }catch(error){return reply(500,{error:error.code==='ENOENT'?'示例图片不存在':error.message||'无法定位图片'});}
  };
}
export function localAssetsPlugin(){
  let root;
  return {name:'local-demo-assets',configResolved(config){root=config.root},
    configureServer(server){server.middlewares.use(createAssetMiddleware(root))},
    configurePreviewServer(server){server.middlewares.use(createAssetMiddleware(root))}};
}
