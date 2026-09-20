"""运行租约：谁在跑这次分析，以及"能不能接管"的判定。

为什么不是纯心跳超时：进程刚崩时心跳是**新鲜的**，按超时判定就得等满一个窗口
（默认 300 秒）才能续跑——对一个本地工作台来说不可接受。所以租约携带持有者身份
（host:pid:token），判定顺序是"先精确、后退避"：

  - 同主机 + 该 PID 已不存在 ⇒ **立刻**可以接管（进程崩溃的常见情形）；
  - 持有者进程还活着 ⇒ 一律不接管（哪怕心跳很旧——宁可保守，也不能出现两个执行者）；
  - 判不出来（跨主机、或没有租约信息的旧记录）⇒ 退回心跳/更新时间窗口。

PID 复用只会导致"保守地拒绝接管"，方向是安全的。
"""

from __future__ import annotations

import os
import socket
from datetime import datetime, timedelta
from uuid import uuid4

from app_review_insights.models import RunRecord, RunStatus

#: 算作"占着这个 App"的状态：能续跑的运行都占位（沿用既有语义）
MUTEX_STATUSES = (RunStatus.PENDING, RunStatus.RUNNING, RunStatus.WAITING)

#: 可能仍在执行的状态——只有这两种才需要判定"持有者是不是还活着"
EXECUTING_STATUSES = (RunStatus.PENDING, RunStatus.RUNNING)

_OWNER: str | None = None


def make_owner() -> str:
    """本进程的租约身份。

    **按进程缓存**（而不是每次 new 一个编排器就换一个）：同一个进程里的两个编排器
    是同一个持有者，否则"我自己持有的运行"会被自己判成被别人持有，正常的模型失败
    续跑会直接失效。
    """
    global _OWNER
    if _OWNER is None:
        _OWNER = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
    return _OWNER


def owner_is_alive(owner: str | None) -> bool | None:
    """租约持有者进程是否还活着；判不出来（跨主机、格式不认识）返回 None。"""
    if not owner:
        return None
    parts = owner.split(":")
    if len(parts) < 2:
        return None
    host, pid_text = parts[0], parts[1]
    if host != socket.gethostname():
        return None
    try:
        pid = int(pid_text)
    except ValueError:
        return None
    if pid == os.getpid():
        return True
    return _pid_exists(pid)


def _win_kernel32():
    """配置好参数类型的 kernel32。

    ctypes 默认把返回值当 32 位 int，句柄在 64 位进程上会被截断——句柄要传给
    WaitForSingleObject 和 CloseHandle，必须声明成指针宽度。
    """
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    return kernel32


def _pid_exists(pid: int) -> bool | None:
    """进程是否**还在运行**：True 在跑 / False 已结束 / None 判不出来。

    两个 Windows 上的坑，都实测过：

    1. **绝不能**用 os.kill(pid, 0) 做存活探测——那个调用在 Windows 上等价于
       TerminateProcess，会真的把别人的进程杀掉。
    2. OpenProcess 成功**不等于**进程还活着：刚退出的进程在 PID 被回收之前仍然打得开。
       实测子进程以 137 退出后立即探测返回 True，0.5 秒后才是 False。这几百毫秒的窗口
       足以让"崩溃后立刻续跑"被误判成"还有人在持有"而拒绝接管。所以这里用
       WaitForSingleObject 判它是否已经进入终止态。
    """
    if os.name == "nt":
        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x102
        ERROR_INVALID_PARAMETER = 87

        kernel32 = _win_kernel32()
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            # 87 = 没有这个 PID（已彻底回收）；其它错误码多半是权限不足，判不出来
            return False if kernel32.GetLastError() == ERROR_INVALID_PARAMETER else None
        try:
            # 已终止的进程立刻变成有信号态；还在跑的会超时
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但不属于当前用户
        return True
    return True


def is_held(run: RunRecord, now: datetime, timeout_seconds: float) -> bool:
    """能否判定"仍有一个活着的执行者在写这次运行"。"""
    if run.status not in EXECUTING_STATUSES:
        return False
    if run.status == RunStatus.PENDING and run.lease_owner is None:
        # 排队中：已提交、还没有执行者。没有租约就是**没人写它**——若按心跳窗口判成
        # "被持有"，任何执行者都不敢认领，队列直接死掉（见 T1 计划书）。
        return False
    alive = owner_is_alive(run.lease_owner)
    if alive is True:
        return True
    if alive is False:
        return False
    reference = run.heartbeat_at or run.updated_at
    return reference >= now - timedelta(seconds=timeout_seconds)


def blocks_new_run(run: RunRecord, now: datetime, timeout_seconds: float) -> bool:
    """这次运行是否应当阻止同一 App 的新运行（占用互斥）。"""
    if run.status not in MUTEX_STATUSES:
        return False
    if run.status in (RunStatus.PENDING, RunStatus.WAITING):
        # 排队中（还没有执行者）与停在检查点上等模型恢复的运行，都仍然占着这个 App：
        # "没人持有"不等于"同一个 App 可以再来一次"——否则会被排两次队、模型额度烧两份。
        return True
    return is_held(run, now, timeout_seconds)


def can_resume(run: RunRecord, now: datetime, timeout_seconds: float) -> bool:
    """能不能接管续跑。

    等待中的运行随时可续（进程本来就已经交还控制权）；执行中的运行必须确认
    没有活着的持有者——这正是"进程被硬杀之后无法续跑"的修复点。
    """
    if run.status in (RunStatus.WAITING, RunStatus.TIMED_OUT, RunStatus.CANCELLED):
        # 超时与取消都停在检查点上，与"等模型恢复"同构：已完成的工作不该因为
        # "停过一次"而作废，用户随时可以续跑。
        return True
    if run.status in EXECUTING_STATUSES:
        return not is_held(run, now, timeout_seconds)
    return False
