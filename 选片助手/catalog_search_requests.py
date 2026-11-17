"""归档只读查询的页面隔离、代次取消和断线检测。"""
from contextlib import contextmanager
from dataclasses import dataclass
import re
import select
import socket
import threading
import time


class SearchCancelled(Exception):
    pass


class SearchBusy(Exception):
    pass


class SearchCancellation:
    def __init__(self, connection=None):
        self.event = threading.Event()
        self.connection = connection
        self.next_check = 0

    def __call__(self):
        if self.event.is_set():return True
        now = time.monotonic()
        if self.connection is not None and now >= self.next_check:
            self.next_check = now + .05
            try:
                readable, _, _ = select.select([self.connection], [], [], 0)
                if readable and not self.connection.recv(1, socket.MSG_PEEK):self.event.set()
            except (OSError, ValueError):
                self.event.set()
        return self.event.is_set()


@dataclass
class _Request:
    sequence: int
    cancel: SearchCancellation
    touched: float
    active: bool = True


class CatalogSearchRequests:
    def __init__(self, maximum=64, ttl=120):
        self.maximum = maximum
        self.ttl = ttl
        self.lock = threading.Lock()
        self.requests = {}

    @contextmanager
    def begin(self, client='', sequence='0', connection=None):
        cancel = SearchCancellation(connection)
        if not client:
            yield cancel
            return
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', client):raise ValueError('搜索页面标识无效')
        try:sequence = int(sequence)
        except (TypeError, ValueError):raise ValueError('搜索请求序号无效') from None
        if not 0 <= sequence <= 2**53-1:raise ValueError('搜索请求序号无效')
        now = time.monotonic()
        with self.lock:
            for key, value in list(self.requests.items()):
                if not value.active and now-value.touched > self.ttl:del self.requests[key]
            previous = self.requests.get(client)
            if previous and sequence <= previous.sequence:raise SearchCancelled()
            if previous is None and len(self.requests) >= self.maximum:
                idle = [(value.touched, key) for key, value in self.requests.items() if not value.active]
                if not idle:raise SearchBusy()
                del self.requests[min(idle)[1]]
            if previous:previous.cancel.event.set()
            entry = _Request(sequence, cancel, now)
            self.requests[client] = entry
        try:
            yield cancel
        finally:
            with self.lock:
                if self.requests.get(client) is entry:
                    entry.active = False
                    entry.touched = time.monotonic()
