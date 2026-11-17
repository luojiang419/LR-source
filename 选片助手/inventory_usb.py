"""已授权 ADB 手机的回环会话和持久增量导入；凭据只在本机内存中使用。"""
from __future__ import annotations
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request,build_opener,ProxyHandler

class InventoryUsb:
    def __init__(self,owner):
        self.owner=owner;self.lock=threading.RLock();self.closed=threading.Event();self.active=False;self.thread=None;self.adb=None;self.session=None;self.selected=''
        self.rescan=False;self.status={'phase':'idle','auto':True,'devices':[],'message':'尚未连接手机','imported':0,'last_sync':'','phone':{}}
    def state(self):
        with self.lock:return json.loads(json.dumps(self.status))
    def set(self,**values):
        with self.lock:self.status.update(values)
    def activate(self,body=None):
        body=body or {}
        if body.get('disconnect'):
            self.active=False;self.set(auto=False,phase='disconnected',message='已断开手机');return self.state()
        self.selected=str(body.get('device',''));self.active=True;self.set(auto=True)
        if body.get('rescan'):self.rescan=True
        if self.thread is None or not self.thread.is_alive():
            self.closed.clear();self.thread=threading.Thread(target=self.loop,daemon=True,name='inventory-usb');self.thread.start()
        return self.state()
    def find_adb(self):
        candidates=[shutil.which('adb')]
        roots=[Path(sys.executable).parent,Path(sys.executable).parent.parent,Path(__file__).resolve().parents[1]]
        if os.environ.get('FRAMEKEEP_APP_ROOT'):roots.insert(0,Path(os.environ['FRAMEKEEP_APP_ROOT']))
        for root in roots:
            candidates += [str(root/'runtime/adb/adb.exe'),str(root/'_internal/runtime/adb/adb.exe')]
        for key in ('ANDROID_HOME','ANDROID_SDK_ROOT'):
            if os.environ.get(key):candidates.append(str(Path(os.environ[key])/'platform-tools/adb.exe'))
        candidates.append('D:/Android/Sdk/platform-tools/adb.exe')
        found=next((p for p in candidates if p and Path(p).is_file()),None)
        if not found:raise ValueError('未找到 ADB，请安装 Android platform-tools 或使用局域网连接')
        return found
    def run(self,args):
        result=subprocess.run([self.adb,*args],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=15,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:raise ValueError('USB 调用失败，请检查手机是否已解锁并授权调试')
        return result.stdout
    def devices(self):
        result=[]
        for line in self.run(['devices','-l']).splitlines()[1:]:
            parts=line.split()
            if len(parts)<2:continue
            serial=parts[0]
            if not re.fullmatch(r'[\w.:\-]{1,160}',serial):continue
            model=next((p.split(':',1)[1].replace('_',' ') for p in parts[2:] if p.startswith('model:')),serial)
            result.append({'id':serial,'state':parts[1],'name':'小米 9' if model.upper().replace(' ','')=='MI9' else model})
        return result
    def request(self,path,data=None):
        session=self.session
        if not session:raise ValueError('手机尚未连接')
        req=Request(session['url']+path,data=json.dumps(data).encode() if data is not None else None,headers={'Authorization':'Bearer '+session['token'],'Content-Type':'application/json'})
        with build_opener(ProxyHandler({})).open(req,timeout=25) as response:
            content=response.read(20_000_001)
            if len(content)>20_000_000:raise ValueError('手机响应超过限制')
            result=json.loads(content)
            if not isinstance(result,dict):raise ValueError('手机响应格式无效')
            return result
    def connect(self,device):
        serial=device['id'];prefix=['-s',serial]
        self.set(phase='connecting',message='正在连接 '+device['name'],device=serial,name=device['name'])
        self.run(prefix+['shell','am','start','-W','-n','com.framekeep.inventory/com.framekeep.inventory.MainActivity'])
        response=self.run(prefix+['shell','content','call','--uri','content://com.framekeep.inventory.usb','--method','start'])
        token=re.search(r'\btoken=([0-9a-f]{32})\b',response);port=re.search(r'\bport=(\d+)\b',response)
        if not token or not port:raise ValueError('手机 APP 需要更新到支持自动 USB 连接的版本')
        local=self.run(prefix+['forward','tcp:0','tcp:'+port.group(1)]).strip()
        if not local.isdigit():raise ValueError('无法创建 USB 通道')
        self.session={'serial':serial,'port':local,'url':'http://127.0.0.1:'+local,'token':token.group(1),'name':device['name']}
        last=None
        for _ in range(30):
            try:
                health=self.request('/v1/health')
                if health.get('protocol')!=1 or 'delta' not in health.get('capabilities',[]):raise ValueError('手机增量协议未就绪')
                self.session['device']=health['device'];self.owner.remember_device(health['device'],device['name']);return
            except Exception as exc:last=exc;time.sleep(.2)
        raise ValueError('手机传输服务启动未完成，请重试') from last
    def sync(self):
        owner=self.owner;device=self.session['device'];health=self.request('/v1/health')
        self.set(phase='syncing',message='正在同步',phone=health.get('summary',{}))
        with closing(owner.connect()) as db:cursor=db.execute('SELECT cursor FROM devices WHERE id=?',(device,)).fetchone()[0]
        if self.rescan:cursor=0;self.rescan=False
        imported=duplicates=0
        while self.active and not self.closed.is_set():
            batch=self.request('/v1/changes?'+urlencode({'after':cursor}));rows=batch.get('records');next_cursor=batch.get('next')
            if not isinstance(rows,list) or len(rows)>100 or type(next_cursor) is not int or next_cursor<cursor:raise ValueError('手机增量游标无效')
            for metadata in rows:
                if not self.active or self.closed.is_set():return
                id=owner.identity(metadata.get('id'));known=owner.has_complete(metadata)
                if not known:
                    try:record=self.request('/v1/record?'+urlencode({'id':id}))
                    except HTTPError as exc:
                        if exc.code==404:continue
                        raise
                    receipt=owner.receive(record);imported+=not receipt['duplicate'];duplicates+=receipt['duplicate']
                else:duplicates+=1;receipt=metadata
                self.request('/v1/ack',{**{k:receipt[k] for k in ('id','product_sha','label_sha')},'revision':receipt.get('revision',0)})
            if next_cursor==cursor and not batch.get('done'):raise ValueError('手机增量未前进')
            if not self.active or self.closed.is_set():return
            with closing(owner.connect()) as db:
                with db:db.execute('UPDATE devices SET cursor=?,last_seen=? WHERE id=?',(next_cursor,datetime.now().astimezone().isoformat(),device))
            cursor=next_cursor
            if batch.get('done'):break
        if not self.active or self.closed.is_set():return
        health=self.request('/v1/health')
        if not self.active or self.closed.is_set():return
        self.set(phase='connected',message='已连接 · '+self.session['name'],phone=health.get('summary',{}),last_sync=datetime.now().astimezone().isoformat(),imported=self.state()['imported']+imported,duplicates=duplicates)
    def release(self,stop=False):
        session=self.session;self.session=None
        if session:
            if stop:
                try:self.run(['-s',session['serial'],'shell','content','call','--uri','content://com.framekeep.inventory.usb','--method','stop'])
                except Exception:pass
            try:self.run(['-s',session['serial'],'forward','--remove','tcp:'+session['port']])
            except Exception:pass
    def loop(self):
        while not self.closed.is_set():
            if not self.active:
                self.release(stop=True);self.set(phase='disconnected',message='已断开手机');self.closed.wait(1);continue
            try:
                if not self.adb:self.adb=self.find_adb()
                devices=self.devices();self.set(devices=devices)
                ready=[d for d in devices if d['state']=='device' and (not self.selected or d['id']==self.selected)]
                physical=[d for d in ready if not d['id'].startswith('emulator-')]
                if not self.selected and physical:ready=physical
                if len(ready)!=1:
                    self.release();self.set(phase='choose' if len(ready)>1 else 'disconnected',message='请选择手机' if len(ready)>1 else '等待手机连接并授权 USB 调试')
                else:
                    if not self.session or self.session['serial']!=ready[0]['id']:self.release();self.connect(ready[0])
                    self.sync()
            except Exception as exc:
                self.release();self.set(phase='error',message=str(exc) if isinstance(exc,ValueError) else '手机连接中断，正在等待重连')
            self.closed.wait(4)
        self.release(stop=True)
    def close(self):
        self.active=False;self.closed.set()
        if self.thread and self.thread.is_alive():self.thread.join(timeout=3)
