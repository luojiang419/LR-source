"""只读、按播放位置解码的视频流；不创建整段转码文件。"""
from __future__ import annotations

from collections import deque
import math
from pathlib import Path
import re
import select
import socket
import subprocess
import threading
import time

from video_preview import find_tool


class VideoPlayback:
    def __init__(self, catalog):
        self.catalog = catalog
        self.lock = threading.RLock()
        self.jobs = {}
        self.closed = False

    def state(self, session):
        with self.lock:
            job = self.jobs.get(session)
            return {'status':job['status'],'error':job.get('error',''),'backend':job.get('backend','cpu'),
                    'fallbacks':job.get('fallbacks',0)} if job else {'status':'missing','error':''}

    def active_count(self):
        with self.lock:
            return sum((job['status']=='starting' and not job['cancel'].is_set()) or
                       (job.get('process') is not None and job['process'].poll() is None) for job in self.jobs.values())

    def _trim(self):
        for key in list(self.jobs):
            if len(self.jobs)<32:
                break
            process=self.jobs[key].get('process')
            if self.jobs[key]['status'] not in ('starting','playing') and (process is None or process.poll() is not None):
                del self.jobs[key]

    def stop(self, session):
        with self.lock:
            job = self.jobs.get(session)
            if not job:
                return
            job['cancel'].set()
            if job['status'] not in ('ended','failed'):
                job['status'] = 'stopped'
            process = job.get('process')
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill();process.wait(timeout=2)
            except OSError:
                pass

    def close(self):
        with self.lock:
            self.closed = True
            sessions = list(self.jobs)
        for session in sessions:
            self.stop(session)

    def _start(self, item_id, session, start):
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',str(session)):
            raise ValueError('视频播放标识无效')
        try:
            start = float(start)
        except (TypeError,ValueError):
            raise ValueError('视频播放位置无效') from None
        if not math.isfinite(start) or not 0<=start<=864000:
            raise ValueError('视频播放位置无效')
        row = self.catalog.item(item_id)
        root = Path(self.catalog._root(row['root'])['path'])
        path = Path(row['path'])
        if row['media_type']!='video':
            raise ValueError('所选素材不是视频')
        if not row['online'] or not path.is_file():
            raise ValueError('视频已离线或不可访问，请检查原文件及网络连接')
        try:
            path.resolve(strict=True).relative_to(root.resolve(strict=True))
        except (ValueError,OSError):
            raise ValueError('视频不在已授权归档位置内') from None
        ffmpeg = find_tool('ffmpeg')
        if not ffmpeg:
            raise ValueError('视频解码组件未就绪，请重新安装视频组件')
        from gpu_video import playback_candidates
        candidates=playback_candidates(path,start,ffmpeg,find_tool('ffprobe'))
        with self.lock:
            if self.closed:
                raise ValueError('视频播放服务已关闭')
            if session in self.jobs:
                raise ValueError('视频播放标识已使用，请重新打开视频')
            if self.active_count()>=2:
                raise ValueError('已有视频正在准备，请关闭其他预览后重试')
            # 仅保留少量终态供播放器查询具体失败原因。
            self._trim()
            job = {'status':'starting','cancel':threading.Event(),'error':'','stderr':deque(maxlen=12),
                   'started':time.monotonic(),'output':False,'process':None,'backend':'cpu','fallbacks':0,'first_chunk':b''}
            self.jobs[session] = job
        # 在发送 MP4 响应前确认首段可读；硬件初始化失败时仍能换用兼容路径。
        for number,(tool,inputs,filters,encoder,backend) in enumerate(candidates):
            command=[tool,'-nostdin','-hide_banner','-loglevel','error',*inputs,
                     '-map','0:v:0','-map','0:a:0?','-sn','-dn',*filters,*encoder,'-fpsmax','30','-g','30',
                     '-c:a','aac','-ac','2','-b:a','160k','-max_muxing_queue_size','1024',
                     '-movflags','+frag_keyframe+empty_moov+default_base_moof','-frag_duration','500000',
                     '-avoid_negative_ts','make_zero','-flush_packets','1','-f','mp4','pipe:1']
            with self.lock:
                if job['cancel'].is_set():return job
                try:
                    process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                             creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    job.update(process=process,backend=backend)
                except OSError as error:
                    job['stderr'].append(str(error));job['fallbacks']+=1;continue
            timer=threading.Timer(12,lambda p=process:p.kill() if p.poll() is None else None);timer.daemon=True;timer.start()
            try:chunk=process.stdout.read1(65536)
            except OSError:chunk=b''
            finally:timer.cancel()
            if chunk:
                job['first_chunk']=chunk
                return job
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)
            job['stderr'].append(process.stderr.read(16384).decode('utf8',errors='replace')[-700:])
            process.stdout.close();process.stderr.close()
            with self.lock:job['process']=None
            job['fallbacks']+=1
        if not job['cancel'].is_set():
            job.update(status='failed',error='视频无法解码，文件可能损坏或编码暂不受支持。'+''.join(job['stderr'])[-700:])
        return job

    def stream(self, handler, item_id, session, start=0):
        try:
            job = self._start(item_id,session,start)
        except ValueError as error:
            with self.lock:
                if re.fullmatch(r'[A-Za-z0-9_-]{8,80}',str(session)) and session not in self.jobs:
                    self._trim()
                    self.jobs[session]={'status':'failed','error':str(error),'cancel':threading.Event(),'process':None}
            return handler.reply({'error':str(error)},400)
        process = job['process']
        if process is None:
            handler.send_response(200);handler.send_header('Content-Type','video/mp4')
            handler.send_header('Content-Length','0');handler.end_headers();return
        done = threading.Event()
        def errors():
            try:
                for line in iter(process.stderr.readline,b''):
                    job['stderr'].append(line.decode('utf8',errors='replace')[-500:])
            except (OSError,ValueError):pass
        def watch():
            while not done.wait(.2):
                if not job['output'] and time.monotonic()-job['started']>25:
                    job.update(status='failed',error='视频准备超时，请检查网络或重新打开视频')
                    self.stop(session);return
                try:
                    ready,_,_=select.select([handler.connection],[],[],0)
                    if ready and handler.connection.recv(1,socket.MSG_PEEK)==b'':
                        self.stop(session);return
                except OSError:
                    self.stop(session);return
        error_thread = threading.Thread(target=errors,daemon=True,name='video-playback-errors');error_thread.start()
        watcher = threading.Thread(target=watch,daemon=True,name='video-playback-watch');watcher.start()
        handler.close_connection = True
        try:
            handler.send_response(200)
            handler.send_header('Content-Type','video/mp4')
            handler.send_header('Cache-Control','no-store')
            handler.send_header('Connection','close')
            handler.end_headers()
            if not job['cancel'].is_set():
                handler.wfile.write(job.pop('first_chunk'));handler.wfile.flush()
                job['output']=True;job['status']='playing'
            while not job['cancel'].is_set():
                chunk = process.stdout.read1(65536)
                if not chunk:
                    break
                job['output'] = True
                job['status'] = 'playing'
                handler.wfile.write(chunk);handler.wfile.flush()
            code = process.wait(timeout=5)
            error_thread.join(timeout=1)
            if not job['cancel'].is_set():
                if code:
                    detail = ''.join(job['stderr']).strip()[-700:]
                    job.update(status='failed',error='视频无法解码，文件可能损坏或编码暂不受支持。'+detail)
                else:
                    job['status'] = 'ended'
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError,OSError,subprocess.TimeoutExpired):
            self.stop(session)
        finally:
            done.set()
            if process.poll() is None:
                self.stop(session)
            watcher.join(timeout=1);error_thread.join(timeout=1)
            process.stdout.close();process.stderr.close()
