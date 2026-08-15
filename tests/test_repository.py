from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app_review_insights.models import (
    AnalysisRequest,
    RunRecord,
    RunStatus,
    SourceType,
    Stage,
    StageEvent,
)
from app_review_insights.storage.repository import RunRepository


def make_run(run_id: str = "run-1", updated_offset: int = 0) -> RunRecord:
    now = datetime.now(UTC) + timedelta(seconds=updated_offset)
    return RunRecord(
        run_id=run_id,
        request=AnalysisRequest(
            source_type=SourceType.ONLINE,
            analysis_goal="分析订阅转化",
        ),
        current_stage=Stage.ANALYZE_BATCHES,
        status=RunStatus.RUNNING,
        current_batch=2,
        total_batches=5,
        coverage_ratio=0.4,
        created_at=now,
        updated_at=now,
    )


def test_repository_round_trips_run_output_and_events(tmp_path):
    repo = RunRepository(tmp_path / "runs.sqlite3")
    run = make_run()

    repo.save_run(run)
    repo.save_output(
        "run-1",
        Stage.ANALYZE_BATCHES,
        {"findings": ["发现-1"]},
        batch_index=1,
    )
    repo.add_event(
        "run-1",
        StageEvent(
            stage=Stage.ANALYZE_BATCHES,
            status=RunStatus.RUNNING,
            message="完成第 2 批",
            created_at=run.updated_at,
        ),
    )

    assert repo.get_run("run-1").coverage_ratio == 0.4
    assert repo.get_output("run-1", Stage.ANALYZE_BATCHES, batch_index=1)["findings"] == ["发现-1"]
    assert repo.list_events("run-1")[0].message == "完成第 2 批"


def test_save_output_replaces_the_same_stage_and_batch_checkpoint(tmp_path):
    repo = RunRepository(tmp_path / "runs.sqlite3")
    repo.save_output("run-1", Stage.ANALYZE_BATCHES, {"version": 1}, batch_index=3)
    repo.save_output("run-1", Stage.ANALYZE_BATCHES, {"version": 2}, batch_index=3)

    assert repo.get_output("run-1", Stage.ANALYZE_BATCHES, batch_index=3) == {"version": 2}


def test_list_runs_orders_most_recent_update_first(tmp_path):
    repo = RunRepository(tmp_path / "runs.sqlite3")
    repo.save_run(make_run("older", updated_offset=0))
    repo.save_run(make_run("newer", updated_offset=10))

    assert [run.run_id for run in repo.list_runs()] == ["newer", "older"]


def test_get_run_raises_key_error_for_unknown_run(tmp_path):
    repo = RunRepository(tmp_path / "runs.sqlite3")

    with pytest.raises(KeyError, match="missing"):
        repo.get_run("missing")


def test_repository_closes_connections_after_operations(tmp_path):
    import os
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent(
        """
        import gc
        import sys
        from datetime import UTC, datetime
        from pathlib import Path

        from app_review_insights.models import (
            AnalysisRequest,
            RunRecord,
            RunStatus,
            SourceType,
            Stage,
            StageEvent,
        )
        from app_review_insights.storage.repository import RunRepository

        database = Path(sys.argv[1])
        repo = RunRepository(database)
        now = datetime.now(UTC)
        run = RunRecord(
            run_id="run-1",
            request=AnalysisRequest(source_type=SourceType.JSON, analysis_goal="分析目标"),
            current_stage=Stage.ANALYZE_BATCHES,
            status=RunStatus.RUNNING,
            created_at=now,
            updated_at=now,
        )
        repo.save_run(run)
        repo.save_output("run-1", Stage.ANALYZE_BATCHES, {"findings": []}, batch_index=1)
        repo.add_event(
            "run-1",
            StageEvent(
                stage=Stage.ANALYZE_BATCHES,
                status=RunStatus.RUNNING,
                message="完成批次",
                created_at=now,
            ),
        )
        repo.get_run("run-1")
        repo.get_output("run-1", Stage.ANALYZE_BATCHES, batch_index=1)
        repo.list_events("run-1")
        repo.list_runs()
        gc.collect()
        """
    )

    env = dict(os.environ)
    env["PYTHONPATH"] = "src"
    result = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::ResourceWarning",
            "-c",
            code,
            str(tmp_path / "runs.sqlite3"),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).parents[1],
    )

    assert "unclosed database" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
