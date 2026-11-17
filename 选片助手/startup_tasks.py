"""首屏交互完成后恢复重后台任务；无前端时有限等待后自动恢复。"""
import threading


class StartupTasks:
    # 多数任务只是启动线程；逐项 0.15 秒间隔会让约 30 项排队数秒，
    # 保留短间隔让出 GIL/磁盘即可。无前端握手时（托盘静默）8 秒后自动恢复。
    def __init__(self,tasks,*,fallback_seconds=8,stagger_seconds=.03):
        self.tasks=tasks;self.fallback_seconds=fallback_seconds
        self.stagger_seconds=stagger_seconds
        self.foreground=threading.Event();self.closed=threading.Event()
        self.thread=threading.Thread(target=self._run,name='startup-background',daemon=True)

    def start(self):self.thread.start()

    def release(self):self.foreground.set()

    def _run(self):
        self.foreground.wait(self.fallback_seconds)
        for name,run in self.tasks:
            if self.closed.is_set():return
            try:run()
            except Exception as error:print(f'[startup background] {name}: {type(error).__name__}: {error}',flush=True)
            if self.closed.wait(self.stagger_seconds):return

    def close(self):
        self.closed.set();self.foreground.set()
        if self.thread.is_alive():self.thread.join(timeout=3)
