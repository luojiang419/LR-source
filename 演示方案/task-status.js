/* 导航任务灯：持久队列与当前窗口的主动操作合并，轮询本身不计作任务。 */
(() => {
  'use strict';
  const tabs={selection:'selection-tab',progress:'sku-open',archive:'asset-archive-open',database:'sku-database-open',analysis:'asset-analysis-open',settings:'app-settings-open'};
  const names={selection:'现场拍摄',progress:'拍摄进度',archive:'资产归档',database:'SKU 数据库',analysis:'解析任务',settings:'设置'};
  const descriptions={idle:'空闲',busy:'正在工作',paused:'任务已暂停',waiting:'任务等待中',unknown:'状态暂不可用'};
  const local=new Map();let backend={},started=false,timer=null,polling=false;

  function paint(){
    for(const [key,id] of Object.entries(tabs)){
      const tab=document.getElementById(id),lamp=tab?.querySelector('.workspace-task-light');if(!lamp)continue;
      const pending=[...local.values()].filter(item=>item.key===key&&item.visible);
      const remote=backend[key]||{state:'unknown',message:'正在读取任务状态'};
      const state=pending.length?'busy':remote.state;
      const messages=[...new Set([...pending.map(item=>item.label),...(remote.message&&remote.message!=='空闲'?[remote.message]:[])])];
      const text=`${names[key]}：${descriptions[state]||descriptions.unknown}${messages.length?' · '+messages.join('；'):''}`;
      lamp.dataset.state=state;tab.dataset.taskState=state;tab.title=text;
      const description=document.getElementById(id+'-task-status');
      if(description.textContent!==text)description.textContent=text;
    }
  }

  function operation(path,body){
    if(body===undefined)return null;
    if(/^\/api\/mouse\/(poll|heartbeat|target)$/.test(path))return null;
    if(path.startsWith('/api/sku/library'))return ['database','正在更新 SKU 数据库'];
    if(path.startsWith('/api/sku/'))return ['progress','正在处理拍摄任务'];
    if(/^\/api\/(analysis|parsing)\//.test(path))return ['analysis','正在处理解析任务'];
    if(/^\/api\/(catalog|assets|metadata|archive|raw)\//.test(path))return ['archive','正在处理资产任务'];
    if(/^\/api\/(components|mouse|models|model|update|assistant)\//.test(path))return ['settings','正在准备或更新设置'];
    if(/^\/api\/(session|scan|folders|watch)(\/|$)/.test(path))return ['selection','正在处理照片'];
    return null;
  }

  function begin(path,body){
    const target=operation(path,body);if(!target)return ()=>{};
    const identity=Symbol(),item={key:target[0],label:target[1],visible:false,shown:0};local.set(identity,item);
    // 短请求无需闪一下；长操作最少显示一小段时间，避免连续操作产生闪烁。
    const delay=setTimeout(()=>{item.visible=true;item.shown=performance.now();paint()},350);
    let finished=false;
    return ()=>{
      if(finished)return;finished=true;clearTimeout(delay);
      const remove=()=>{local.delete(identity);paint()};
      if(item.visible)setTimeout(remove,Math.max(0,900-(performance.now()-item.shown)));else remove();
    };
  }

  async function poll(){
    clearTimeout(timer);
    if(polling)return;
    if(document.hidden){timer=setTimeout(poll,8000);return}
    polling=true;
    try{
      const response=await fetch('/api/task-status',{cache:'no-store',signal:AbortSignal.timeout(3000)});
      if(!response.ok)throw new Error('任务状态读取失败');
      const result=await response.json();
      if(!result.modules)throw new Error('任务状态无效');
      backend=result.modules;
    }catch{backend=Object.fromEntries(Object.keys(tabs).map(key=>[key,{state:'unknown',message:'连接中断，正在重试'}]))}
    finally{polling=false;paint();timer=setTimeout(poll,document.hidden?8000:1800)}
  }

  function mount(){
    for(const id of Object.values(tabs)){
      const tab=document.getElementById(id);if(!tab||tab.querySelector('.workspace-task-light'))continue;
      const lamp=document.createElement('span');lamp.className='workspace-task-light';lamp.setAttribute('aria-hidden','true');
      const description=document.createElement('span');description.id=id+'-task-status';description.className='workspace-task-description';description.hidden=true;
      tab.prepend(lamp);tab.append(description);
      tab.setAttribute('aria-describedby',[tab.getAttribute('aria-describedby'),description.id].filter(Boolean).join(' '));
    }
    paint();
    if(started)return;started=true;
    if(window.FRAMEKEEP_SESSION?.demo){backend=Object.fromEntries(Object.keys(tabs).map(key=>[key,{state:'idle',message:'空闲'}]));paint();return}
    document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll()});
    poll();
  }
  window.FramekeepTaskStatus={begin,mount};
})();
