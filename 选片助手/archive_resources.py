"""归档共用的硬件预算；启动时探测一次，不为每个位置重复扩容。"""
import ctypes
import os


GiB=1024**3
PREVIEW_TASK_BYTES=512*1024**2


def available_cpus():
    count=getattr(os,'process_cpu_count',os.cpu_count)() or 1
    try:
        if hasattr(os,'sched_getaffinity'):
            count=min(count,len(os.sched_getaffinity(0)))
        elif os.name=='nt':
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.GetCurrentProcess.restype=ctypes.c_void_p
            kernel.GetProcessAffinityMask.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_size_t),ctypes.POINTER(ctypes.c_size_t)]
            process,system=ctypes.c_size_t(),ctypes.c_size_t()
            if kernel.GetProcessAffinityMask(kernel.GetCurrentProcess(),ctypes.byref(process),ctypes.byref(system)) and process.value:
                # 多处理器组机器的单组掩码不能代表全机容量。
                if count<=ctypes.sizeof(ctypes.c_size_t)*8:count=min(count,process.value.bit_count())
    except (AttributeError,OSError):pass
    return max(1,count)


def physical_memory():
    """返回可用与总物理内存；探测失败由 CPU 预算兜底。"""
    try:
        if os.name=='nt':
            class MemoryStatus(ctypes.Structure):
                _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong)]+[(key,ctypes.c_ulonglong) for key in
                    ('total','available','page_total','page_available','virtual_total','virtual_available','extended')]
            status=MemoryStatus();status.length=ctypes.sizeof(status)
            if ctypes.WinDLL('kernel32',use_last_error=True).GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.available,status.total
        elif hasattr(os,'sysconf'):
            page=os.sysconf('SC_PAGE_SIZE')
            return os.sysconf('SC_AVPHYS_PAGES')*page,os.sysconf('SC_PHYS_PAGES')*page
    except (AttributeError,OSError,ValueError):pass
    return None,None


def worker_budget(cpus,available=None,total=None):
    cpus=max(1,int(cpus or 1))
    if available is None:return cpus
    reserve=max(GiB,int((total or available)*.1))
    return max(1,min(cpus,(max(0,available-reserve))//PREVIEW_TASK_BYTES))


CPU_THREADS=available_cpus()
AVAILABLE_MEMORY,TOTAL_MEMORY=physical_memory()
PREVIEW_WORKERS=worker_budget(CPU_THREADS,AVAILABLE_MEMORY,TOTAL_MEMORY)
# 元数据枚举不展开图片；同样限制在本机预算内，所有位置共享一个目录池。
INDEX_WORKERS=PREVIEW_WORKERS
