"""ExifTool 官方 Perl 引擎的有界常驻内存编辑通道，保留调用方全部校验。"""
import base64
from pathlib import Path
import queue
import subprocess
import sys
import threading


class MemoryEditor:
    def __init__(self,tool,config,timeout=180):
        folder=Path(tool).parent/'exiftool_files'
        self.perl=folder/'perl.exe';self.library=folder/'lib'
        self.script=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'metadata_memory_worker.pl'
        self.config=Path(config);self.timeout=timeout
        self.lock=threading.Lock();self.process=None;self.output=None

    def available(self):return self.perl.is_file() and self.script.is_file()

    def _start(self):
        process=subprocess.Popen([str(self.perl),'-I'+str(self.library),str(self.script),self.config.name],cwd=self.config.parent,
                                 stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        output=queue.Queue()
        def receive():
            try:
                while True:
                    line=process.stdout.readline()
                    if not line:break
                    kind,size=line.decode('ascii').strip().split(' ',1);size=int(size)
                    if kind not in ('OK','ERR') or not 0<=size<=536870912:raise ValueError('非法内存响应')
                    chunks=[];remaining=size
                    while remaining:
                        chunk=process.stdout.read(min(remaining,1024*1024))
                        if not chunk:raise EOFError('内存响应不完整')
                        chunks.append(chunk);remaining-=len(chunk)
                    output.put((kind,b''.join(chunks)))
            except Exception as exc:output.put(('ERR',str(exc).encode('utf-8')))
            finally:output.put(None)
        self.process=process;self.output=output
        self.reader=threading.Thread(target=receive,name='metadata-memory-output',daemon=True);self.reader.start()

    def close(self):
        process=self.process;self.process=None
        if process is None:return
        if process.poll() is None:process.kill()  # 子进程只编辑内存，不存在原片提交中的窗口。
        process.wait();process.stdin.close();self.reader.join();process.stdout.close()

    def edit(self,args,content):
        with self.lock:
            if self.process is None:self._start()
            try:
                header=f'{len(args)} {len(content)}\n'.encode('ascii')
                arguments=b''.join(base64.b64encode(str(arg).encode('utf-8'))+b'\n' for arg in args)
                # 写管道也放入独立线程，防止子进程卡住时 stdin.write 绕过超时。
                def send():
                    try:self.process.stdin.write(header+arguments+content);self.process.stdin.flush()
                    except (OSError,ValueError,AttributeError):pass
                sender=threading.Thread(target=send,name='metadata-memory-input',daemon=True);sender.start()
                response=self.output.get(timeout=self.timeout)
                if response is None:raise RuntimeError('ExifTool 内存编辑进程已退出')
                kind,payload=response
                if kind!='OK':raise ValueError('ExifTool 内存编辑失败：'+payload.decode('utf-8',errors='replace')[-300:])
                sender.join(timeout=1)
                return payload
            except BaseException:
                self.close()
                raise
