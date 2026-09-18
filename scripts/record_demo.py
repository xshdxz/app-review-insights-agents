"""用真实模型跑一次样例分析，产出可提交的录制文件。

用法（需要 .env 里有可用的 DEEPSEEK_API_KEY）：

    python scripts/record_demo.py

产出 data/recordings/demo-replay.json，演示模式据此回放整条流水线。
录制只影响写入，不改变任何调用语义。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from app_review_insights.config import Settings, load_settings
from app_review_insights.errors import InputDataError
from app_review_insights.factory import build_pipeline_services
from app_review_insights.input_parsing import import_reviews
from app_review_insights.llm.recording import input_fingerprint
from app_review_insights.models import AnalysisRequest, RunRecord, SourceType
from app_review_insights.pipeline.orchestrator import AnalysisOrchestrator, PipelineServices
from app_review_insights.storage.cache import SAMPLE_PATH

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DESTINATION = PROJECT_ROOT / "data" / "recordings" / "demo-replay.json"

ANALYSIS_GOAL = "识别影响用户体验与产品增长的核心问题，并形成可追溯需求"


def _recording_settings(settings: Settings, destination: Path) -> Settings:
    """录制必须走真实模型，因此强制 live 并指定输出路径。"""
    return settings.model_copy(update={"demo_mode": "live", "model_record_path": destination})


def record_demo(
    *,
    sample_path: Path = SAMPLE_PATH,
    destination: Path = DEFAULT_DESTINATION,
    settings: Settings | None = None,
    services_factory: Callable[..., PipelineServices] | None = None,
) -> RunRecord:
    """在样例上跑一次分析并把模型调用录下来。"""
    base = settings or load_settings()
    try:
        raw = sample_path.read_bytes()
    except OSError as exc:
        # 与 load_recording 同一套处理：素材读不出来属于域内配置/素材问题，一律抛
        # 中文 InputDataError，不让 FileNotFoundError 的英文 traceback 冒到调用方
        raise InputDataError(f"样例文件无法读取：{sample_path}（{exc}）") from exc
    reviews = import_reviews(raw, sample_path.name, app_id="demo")
    recording_settings = _recording_settings(base, destination)
    fingerprint = input_fingerprint(reviews)

    if services_factory is None:
        services = build_pipeline_services(recording_settings, input_fingerprint=fingerprint)
    else:
        services = services_factory(recording_settings, input_fingerprint=fingerprint)

    request = AnalysisRequest(
        source_type=SourceType.JSON,
        analysis_goal=ANALYSIS_GOAL,
        # 样例只有 20 条，而 review_limit 的下限是 100
        review_limit=max(100, len(reviews)),
    )
    return AnalysisOrchestrator(services).start(request, imported_reviews=reviews)


def main(argv: list[str] | None = None) -> int:
    if not load_settings().model_available:
        print("未配置可用的模型密钥，无法录制。请先在 .env 设置 DEEPSEEK_API_KEY。")
        return 1
    run = record_demo()
    print(f"录制完成，run_id={run.run_id}，输出 {DEFAULT_DESTINATION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
