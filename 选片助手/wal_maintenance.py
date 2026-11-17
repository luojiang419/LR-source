"""归档 WAL 的后台回收；前台提交和连接关闭均不执行历史检查点。"""
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import threading
import time

WAL_TARGET_BYTES = 4 * 1024 * 1024
WAL_RETAIN_BYTES = 8 * 1024 * 1024
WAL_PRESSURE_BYTES = 64 * 1024 * 1024
WAL_RECLAIM_WAIT_MS = 250


def configure(connection):
    connection.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
    connection.execute('PRAGMA wal_autocheckpoint=0')
    connection.execute(f'PRAGMA journal_size_limit={WAL_RETAIN_BYTES}')


class WalMaintenance:
    def __init__(self, database, *, interval=1):
        self.database = Path(database).resolve()
        self.interval = interval
        self.stopped = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        self.error = ''
        self.low_priority = False
        self.pressure = threading.Event()
        self._update_pressure()

    def _update_pressure(self):
        try:
            size = Path(str(self.database)+'-wal').stat().st_size
        except OSError:
            size = 0
        if size > WAL_PRESSURE_BYTES:self.pressure.set()
        else:self.pressure.clear()
        return size

    def wait_for_startup_recovery(self, *, timeout=5):
        # 历史长读事务可能让 PASSIVE 一直无法截短日志，不能无限挡住
        # 持久解码队列恢复。短暂让维护先行后继续启动，维护线程照常回收。
        deadline = time.monotonic() + timeout
        while not self.stopped.is_set() and self._update_pressure() > WAL_RETAIN_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:return
            self.stopped.wait(min(.1, remaining))

    def start(self):
        with self.lock:
            if self.stopped.is_set() or self.thread is not None:
                return
            self.thread = threading.Thread(target=self._run, name='catalog-wal-maintenance', daemon=True)
            self.thread.start()

    def checkpoint(self):
        # rw 禁止资料设备断开时创建空库；普通维护零等待，压力截短仅短暂等锁。
        with closing(sqlite3.connect(self.database.as_uri()+'?mode=rw', uri=True, timeout=0)) as c:
            configure(c)
            result = tuple(c.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone())
            wal = Path(str(self.database)+'-wal')
            size = wal.stat().st_size if wal.exists() else 0
            if size > WAL_RETAIN_BYTES:
                pressure = size > WAL_PRESSURE_BYTES
                # 持续并发提交时 PASSIVE 已追上大部分页面，但几乎没有
                # 所有读写同时空闲的瞬间。只在压力下允许维护连接短暂等锁，
                # 由 TRUNCATE 阻止新写入并等短读者收尾，再安全重置 WAL。
                # 长读者超过窗口仍返回 busy，下轮重试，不无限等待。
                if pressure or (result[0] == 0 and result[1] == result[2]):
                    if pressure:c.execute(f'PRAGMA busy_timeout={WAL_RECLAIM_WAIT_MS}')
                    result = tuple(c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())
            return result

    def _run(self):
        kernel = None
        if os.name == 'nt':
            import ctypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetCurrentThread.restype = ctypes.c_void_p
            kernel.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
            # 只降低 CPU 调度优先级，保留普通磁盘 I/O 优先级。Windows
            # BACKGROUND_BEGIN 还会把检查点的 I/O 降为 Very Low；全速
            # 解码持续写库时回收可能长期饥饿，留下数 GB WAL 拖垮下次启动。
            self.low_priority = bool(kernel.SetThreadPriority(kernel.GetCurrentThread(), -1))
        try:
            self._maintain()
        finally:
            if kernel is not None and self.low_priority:
                kernel.SetThreadPriority(kernel.GetCurrentThread(), 0)

    def _maintain(self):
        # 首屏就绪后才 start；先让出一个周期，磁盘整理绝不进入启动握手。
        ticks = 0
        while not self.stopped.wait(self.interval):
            ticks += 1
            try:
                wal = Path(str(self.database)+'-wal')
                if not wal.exists() or (wal.stat().st_size < WAL_TARGET_BYTES and ticks < 30):
                    continue
                self.checkpoint()
                self._update_pressure()
                ticks = 0
                self.error = ''
            except (OSError, sqlite3.Error) as error:
                # 忙碌、设备断开等故障不影响业务；下周期继续，不反复写日志。
                self.error = str(error)

    def close(self):
        self.stopped.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=1)
