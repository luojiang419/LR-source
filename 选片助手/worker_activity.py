"""持久队列提交后唤醒消费者；只读轮询不广播，超时仍可恢复外部写入。"""
from contextlib import contextmanager
import threading
import time

_condition = threading.Condition()
_revision = 0


def revision():
    with _condition:
        return _revision


def notify():
    global _revision
    with _condition:
        _revision += 1
        _condition.notify_all()


@contextmanager
def transaction(connection):
    before = connection.total_changes
    with connection:
        yield connection
    if connection.total_changes != before:
        notify()


def wait(closed, observed, *, wake=None, timeout=10):
    # 代际在执行 tick 前取得，提交发生在检查与等待之间也不会丢失。
    # Event 没有跨对象等待接口；短时条件等待仅检查退出/显式事件，不访问磁盘。
    deadline = time.monotonic() + timeout
    with _condition:
        while not closed.is_set() and _revision == observed:
            if wake is not None and wake.is_set():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            _condition.wait(min(.1 if wake is not None else .25, remaining))
