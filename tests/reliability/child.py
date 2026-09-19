"""子进程运行器：可靠性用例唯一的"怎么起子进程"入口。

收敛到一处是有原因的：2026-09-19 的 $0.0594 事故正是"环境构造逻辑分散、
其中一处漏了钉死"造成的。这里同时把 stdio 钉成 UTF-8——否则子进程会按
Windows 本地代码页（cp936）输出，父进程按 UTF-8 解码得到乱码。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

CHILD_PATH = Path(__file__).resolve().parent / "_child.py"
PROJECT_ROOT = CHILD_PATH.parents[2]

#: 一次回放流水线 = 13 次模型调用 + 11 个阶段；给足余量，超时即视为挂死
CHILD_TIMEOUT_SECONDS = 300

#: 崩溃注入后的进程退出码（_child.py 用 os._exit，绕过 finally/atexit）
CRASH_EXIT_CODE = 137

#: 护栏拒绝运行时的退出码
GUARD_EXIT_CODE = 4


@dataclass
class ChildResult:
    """一次子进程运行的观察结果。"""

    returncode: int
    summary: dict | None
    stdout: str
    stderr: str

    @property
    def crashed(self) -> bool:
        return self.returncode == CRASH_EXIT_CODE

    @property
    def guarded(self) -> bool:
        return self.returncode == GUARD_EXIT_CODE

    def require_ok(self) -> dict:
        assert self.returncode == 0, (
            f"子进程未正常结束：returncode={self.returncode}\n"
            f"stdout={self.stdout[-2000:]}\nstderr={self.stderr[-2000:]}"
        )
        assert self.summary is not None, f"子进程未输出摘要：stdout={self.stdout[-2000:]}"
        return self.summary


def child_env(db_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """构造子进程环境：清掉上一次的注入参数，钉死 UTF-8 stdio。"""
    env = os.environ.copy()
    env["ARI_DB_PATH"] = str(db_path)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    stale_keys = (
        "ARI_CRASH_AFTER_STAGE",
        "ARI_CRASH_AFTER_BATCH",
        "ARI_RESUME_RUN_ID",
        "ARI_PIN_BYPASS",
    )
    for name in stale_keys:
        env.pop(name, None)
    if extra:
        env.update(extra)
    return env


def child_command() -> list[str]:
    """子进程的启动命令。

    `ARI_COVERAGE=1` 时走 `coverage run --parallel-mode`：崩溃恢复路径全部在**子进程**里
    执行，主进程的 `--cov` 统计不到它们，于是新代码会把整体覆盖率拖下去、看起来"没被测过"。
    把子进程的覆盖数据也收上来，CI 里 `coverage combine` 之后才是真实的数字。
    """
    if os.environ.get("ARI_COVERAGE"):
        return [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--parallel-mode",
            "--source=app_review_insights",
            str(CHILD_PATH),
        ]
    return [sys.executable, str(CHILD_PATH)]


def spawn(
    db_path: Path,
    *,
    crash_after_stage: str | None = None,
    crash_after_batch: int | None = None,
    resume_run_id: str | None = None,
    extra_env: dict[str, str] | None = None,
    timeout: int = CHILD_TIMEOUT_SECONDS,
) -> ChildResult:
    """在子进程里跑一次流水线（默认回放），返回观察结果。"""
    extra = dict(extra_env or {})
    if crash_after_stage is not None:
        extra["ARI_CRASH_AFTER_STAGE"] = crash_after_stage
    if crash_after_batch is not None:
        extra["ARI_CRASH_AFTER_BATCH"] = str(crash_after_batch)
    if resume_run_id is not None:
        extra["ARI_RESUME_RUN_ID"] = resume_run_id

    completed = subprocess.run(  # noqa: S603 - 固定脚本路径，无 shell
        child_command(),
        cwd=str(PROJECT_ROOT),
        env=child_env(db_path, extra),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    summary = None
    for line in reversed(completed.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                summary = json.loads(line)
            except json.JSONDecodeError:
                continue
            break
    return ChildResult(completed.returncode, summary, completed.stdout, completed.stderr)


@dataclass
class Baseline:
    """一次跑完的参照：库快照 + 请求序列 + 耗时。"""

    db: Path
    snapshot: dict
    requests: list[str]
    duration_seconds: float


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def request_keys(path: Path) -> list[str]:
    """请求日志里的 "Schema key" 行。"""
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def spawn_and_kill(
    db_path: Path,
    *,
    kill_after_saves: int,
    jitter_seconds: float,
    extra_env: dict[str, str] | None = None,
    timeout: int = CHILD_TIMEOUT_SECONDS,
) -> tuple[ChildResult, bool]:
    """起子进程跑流水线，等第 N 个检查点落盘后再随机等一小段，然后硬杀。

    用进度日志当控制通道，是为了杀在**模型调用途中**而不是阶段边界上——
    阶段边界已经被确定性矩阵覆盖，这里要覆盖的是那些没有名字的瞬间。
    跨平台：Windows 的 Popen.kill() 走 TerminateProcess，POSIX 走 SIGKILL，
    两者都不给子进程留下执行 finally/atexit 的机会。

    调用方应当同时注入 ARI_REQUEST_DELAY_MS：没有它，回放流水线只要几百毫秒，
    等到第 N 个检查点再杀时子进程往往已经跑完，"随机时刻硬杀"就退化成了空测。

    返回 (结果, 是否真的执行了硬杀)。子进程若在等待期间自己跑完，返回 (结果, False)。
    """
    env = child_env(db_path, extra_env)
    progress_value = env.get("ARI_PROGRESS_LOG")
    assert progress_value, "择时硬杀必须提供 ARI_PROGRESS_LOG"
    progress_path = Path(progress_value)

    process = subprocess.Popen(  # noqa: S603 - 固定脚本路径，无 shell
        child_command(),
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if count_lines(progress_path) >= kill_after_saves:
            break
        if process.poll() is not None:
            break
        time.sleep(0.01)

    killed = False
    if process.poll() is None:
        if jitter_seconds > 0:
            time.sleep(jitter_seconds)
        if process.poll() is None:
            process.kill()
            killed = True

    stdout, stderr = process.communicate(timeout=30)
    summary = None
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                summary = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            break
    return ChildResult(process.returncode, summary, stdout, stderr), killed
