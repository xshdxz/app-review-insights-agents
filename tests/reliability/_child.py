"""可靠性用例的子进程入口：跑一次完整流水线（回放模式），可在指定崩溃点硬自杀。

以脚本方式运行：

    python tests/reliability/_child.py

环境变量：

    ARI_DB_PATH            必填，本次运行的 SQLite 路径
    ARI_RESUME_RUN_ID      非空时改为续跑该 run_id（否则新建运行）
    ARI_CRASH_AFTER_STAGE  阶段名（如 analyze_batches）；该阶段输出落盘后立即 os._exit(137)
    ARI_CRASH_AFTER_BATCH  批次序号；该批次输出落盘后立即 os._exit(137)
    ARI_REQUEST_LOG        每次模型请求追加一行 "Schema key"（供"不重复调用"断言）
    ARI_REQUEST_DELAY_MS   每次模型调用前注入的延迟，让"杀在调用途中"可定时
    ARI_PROGRESS_LOG       每次检查点落盘追加一行（供父进程择时硬杀）
    ARI_PIN_BYPASS         仅供**测试护栏本身**用：跳过环境钉死，验证自检一定会拒绝运行

三条安全底线（2026-09-19 事故后加固）：

1. `_prepare_environment` 把 DEMO_MODE 钉成 replay、DB 路径钉成 ARI_DB_PATH，并清空密钥；
2. `_require_replay_only` 在**装配之后、跑任何阶段之前**自检：不是回放、或 DB 没被接管，
   一律拒绝运行并以退出码 4 结束。护栏不依赖"记得调用"，因为它是被自检的；
3. 自检自身有测试（tests/reliability/test_child_safety.py）：用 `ARI_PIN_BYPASS` 制造
   "钉死失效"的场景，断言子进程**拒绝运行**。

设计要点：崩溃注入**不改生产代码**——在子进程内给 RunRepository.save_output 打一层包装，
在目标检查点落盘之后、下一阶段开始之前直接 os._exit(137)，绕过所有 finally / atexit，
模拟真实进程死亡（Windows 上等价于 TerminateProcess）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from app_review_insights.errors import AppReviewInsightsError  # noqa: E402

CRASH_EXIT_CODE = 137
GUARD_EXIT_CODE = 4

_TARGET_BATCH: int | None = None


def _prepare_environment(db_path: str) -> None:
    """把子进程钉死在回放模式，并保证不可能发生任何外部调用。"""
    if os.environ.get("ARI_PIN_BYPASS"):
        # 只跳过钉死，不放松下面的自检——自检仍然会把这次运行拦下来
        return
    os.environ["DEMO_MODE"] = "replay"
    os.environ["DATABASE_PATH"] = db_path
    os.environ["AGENT_DB_PATH"] = str(Path(db_path).with_name("agent.sqlite3"))
    os.environ["SCHEDULER_ENABLED"] = "false"
    # 显式清空密钥：即使有人改了 DEMO_MODE，也不会有可用模型
    os.environ["DEEPSEEK_API_KEY"] = ""
    os.environ["MODEL_API_KEY"] = ""


def _require_replay_only(settings, db_path: str, services) -> tuple[str, str] | None:
    """自检：任何一条不成立就拒绝运行。

    返回 (reason, message)：reason 是机器可读的短码（断言用它，不受控制台编码影响），
    message 是给人看的中文说明。
    """
    if not settings.demo_replay_active:
        return (
            "not_replay",
            f"拒绝运行：可靠性用例只允许回放模式。DEMO_MODE={settings.demo_mode}，"
            f"模型可用={settings.model_available}。本用例集绝不允许调用真实模型。",
        )
    if Path(settings.database_path).resolve() != Path(db_path).resolve():
        return (
            "database_not_taken_over",
            f"拒绝运行：数据库未被接管（settings={settings.database_path}，期望={db_path}）。"
            "继续跑会把结果写进真实档案库。",
        )
    if not getattr(services, "replay_run", False):
        return (
            "not_replay_pipeline",
            "拒绝运行：装配出来的流水线不是回放实例（services.replay_run=False）。",
        )
    return None


def _install_save_output_hook(
    stage_name: str | None,
    target_batch: int | None,
    progress_path: str | None,
) -> None:
    """包装 RunRepository.save_output：记进度（供父进程择时硬杀）＋ 按需自杀。

    进度日志是随机时刻硬杀的控制通道：父进程等到第 N 条进度出现后再随机等一小段，
    这样杀在**下一个阶段的模型调用途中**，而不是杀在阶段边界上——阶段边界已经被
    确定性矩阵覆盖了。
    """
    if stage_name is None and target_batch is None and not progress_path:
        return
    from app_review_insights.storage.repository import RunRepository

    original = RunRepository.save_output
    fired = False

    def patched(self, run_id, stage, payload, batch_index=-1):  # noqa: A002 - 与生产签名一致
        nonlocal fired
        original(self, run_id, stage, payload, batch_index=batch_index)
        if progress_path:
            with open(progress_path, "a", encoding="utf-8") as handle:
                handle.write(f"{stage.value} {batch_index}\n")
                handle.flush()
        if fired:
            return
        # clean 会被保存两次（清洗后 / 批次摘要回填后），"第一次命中"才是确定的崩溃点语义
        hit_stage = stage_name is not None and stage.value == stage_name
        hit_batch = target_batch is not None and batch_index == target_batch
        if hit_stage or hit_batch:
            fired = True
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(CRASH_EXIT_CODE)

    RunRepository.save_output = patched


def _install_provider_hooks(log_path: str | None, delay_ms: int) -> None:
    """包装 ReplayProvider.generate：记请求 + 可注入延迟。

    - 记请求：回放模式下 model_usage 表是空的（不经过 DeepSeekProvider），
      所以"续跑不重复调用已完成的工作"只能在这里计数。逐条 flush——进程随时可能被硬杀。
    - 注入延迟：回放没有网络往返，整个流水线只要几百毫秒，"杀在模型调用途中"根本
      没法定时。加一层可控延迟后，随机时刻硬杀才真的能落在阶段内部。
    """
    if not log_path and delay_ms <= 0:
        return
    from app_review_insights.llm.recording import ReplayProvider, recording_key

    original = ReplayProvider.generate

    def patched(self, system_prompt, user_prompt, schema):
        if log_path:
            key = recording_key(schema.__name__, system_prompt, user_prompt)
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(f"{schema.__name__} {key}\n")
                handle.flush()
        if delay_ms > 0:
            time.sleep(delay_ms / 1000)
        return original(self, system_prompt, user_prompt, schema)

    ReplayProvider.generate = patched


def main() -> int:
    db_path = os.environ.get("ARI_DB_PATH")
    if not db_path:
        print("ARI_DB_PATH 未设置", file=sys.stderr)
        return 2

    global _TARGET_BATCH
    raw_batch = os.environ.get("ARI_CRASH_AFTER_BATCH")
    _TARGET_BATCH = int(raw_batch) if raw_batch else None

    _prepare_environment(db_path)
    _install_provider_hooks(
        os.environ.get("ARI_REQUEST_LOG") or None,
        int(os.environ.get("ARI_REQUEST_DELAY_MS") or 0),
    )

    from app_review_insights.config import load_settings
    from app_review_insights.factory import build_pipeline_services
    from app_review_insights.input_parsing import import_reviews
    from app_review_insights.llm.recording import load_recording, verify_input_fingerprint
    from app_review_insights.models import AnalysisRequest, SourceType
    from app_review_insights.pipeline.orchestrator import AnalysisOrchestrator
    from app_review_insights.storage.cache import DEMO_ANALYSIS_GOAL, SAMPLE_PATH

    settings = load_settings()
    reviews = import_reviews(SAMPLE_PATH.read_bytes(), SAMPLE_PATH.name, app_id="demo")
    verify_input_fingerprint(load_recording(settings.demo_replay_path), reviews)

    services = build_pipeline_services(settings)
    refusal = _require_replay_only(settings, db_path, services)
    if refusal is not None:
        reason, message = refusal
        print(json.dumps({"error": message, "reason": reason}, ensure_ascii=False))
        return GUARD_EXIT_CODE

    _install_save_output_hook(
        os.environ.get("ARI_CRASH_AFTER_STAGE") or None,
        _TARGET_BATCH,
        os.environ.get("ARI_PROGRESS_LOG") or None,
    )

    orchestrator = AnalysisOrchestrator(services)
    resume_run_id = os.environ.get("ARI_RESUME_RUN_ID") or None

    try:
        if resume_run_id:
            run = orchestrator.resume(resume_run_id, imported_reviews=reviews)
        else:
            request = AnalysisRequest(
                source_type=SourceType.JSON,
                analysis_goal=DEMO_ANALYSIS_GOAL,
                review_limit=max(100, len(reviews)),
            )
            run = orchestrator.start(request, imported_reviews=reviews)
    except AppReviewInsightsError as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 3

    print(
        json.dumps(
            {
                "run_id": run.run_id,
                "status": run.status.value,
                "stage": run.current_stage.value,
                "coverage": run.coverage_ratio,
                "mode": run.mode,
                "is_live": run.is_live,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
