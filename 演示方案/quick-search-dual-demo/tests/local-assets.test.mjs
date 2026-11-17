import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {mkdtemp,mkdir,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {createAssetMiddleware} from '../scripts/local-assets.mjs';

test('只能由同源页面定位当前白名单图片，不能执行任意路径或跨站请求',async()=>{
 const root=await mkdtemp(path.join(tmpdir(),'quick-image-actions-'));
 const assets=path.join(root,'public','assets');await mkdir(assets,{recursive:true});
 const file='ivory-shirt-square.png',actual=path.join(assets,file);await writeFile(actual,'fixture');
 const opened=[],middleware=createAssetMiddleware(root,{openFile:async target=>opened.push(target)});
 const server=createServer((req,res)=>middleware(req,res,()=>{res.statusCode=404;res.end()}));
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const base=`http://127.0.0.1:${server.address().port}`;
 try{
  const info=await fetch(base+'/__demo_assets/info?file='+file);assert.equal(info.status,200);
  const value=await info.json();assert.equal(path.resolve(value.path),path.resolve(actual));
  assert.equal((await fetch(base+'/__demo_assets/open?file='+file)).status,405);
  assert.equal((await fetch(base+'/__demo_assets/open?file='+file,{method:'POST',headers:{Origin:base}})).status,403);
  assert.equal((await fetch(base+'/__demo_assets/open?file='+file,{method:'POST',headers:{Origin:'https://evil.example','X-Demo-Asset-Token':value.token}})).status,403);
  assert.equal((await fetch(base+'/__demo_assets/info?file='+encodeURIComponent('../package.json'))).status,400);
  assert.equal((await fetch(base+'/__demo_assets/info?file='+encodeURIComponent(actual))).status,400);
  assert.deepEqual(opened,[]);
  const result=await fetch(base+'/__demo_assets/open?file='+file,{method:'POST',headers:{Origin:base,'X-Demo-Asset-Token':value.token}});
  assert.equal(result.status,200);assert.equal((await result.json()).opened,true);
  assert.deepEqual(opened,[value.path]);
 }finally{
  await new Promise(resolve=>server.close(resolve));
  assert.equal(path.dirname(path.resolve(root)),path.resolve(tmpdir()));
  assert.ok(path.basename(root).startsWith('quick-image-actions-'));
  await rm(root,{recursive:true,force:true});
 }
});
