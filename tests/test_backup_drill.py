"""备份恢复演练：核对逻辑必须能抓到"恢复出来的库不对"。

一条永远报告"通过"的校验比没有校验更糟——它会让人以为备份可用。
所以这里既测"一致时通过"，也测"不一致时必须报出来"。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from app_review_insights.models import (
    AnalysisRequest,
    RunRecord,
    RunStatus,
    SourceType,
    Stage,
)
from app_review_insights.storage import RunRepository
from scripts.backup_restore_drill import drill, list_tables, restore, verify


def _seeded_database(path):
    """建一个带真实数据的检查点库。"""
    repository = RunRepository(path)
    now = datetime.now(UTC)
    repository.save_output("run-1", Stage.CLEAN, {"reviews": [{"review_id": "r1"}]})
    repository.record_stage_timing("run-1", Stage.CLEAN, 12.5, started_at=now, ended_at=now)
    repository.save_run(
        RunRecord(
            run_id="run-1",
            request=AnalysisRequest(source_type=SourceType.JSON, analysis_goal="分析订阅转化"),
            current_stage=Stage.VALIDATE_FINDINGS,
            status=RunStatus.RUNNING,
            created_at=now,
            updated_at=now,
        )
    )
    return path


def test_verify_passes_for_an_identical_restore(tmp_path):
    source = _seeded_database(tmp_path / "runs.sqlite3")
    restored = tmp_path / "restored.sqlite3"
    restore(source, restored)

    assert verify(source, restored) == []


def test_verify_catches_a_missing_row(tmp_path):
    """少一行就要报出来——"恢复出来是空的"正是演练最该抓到的失败。"""
    source = _seeded_database(tmp_path / "runs.sqlite3")
    restored = tmp_path / "restored.sqlite3"
    restore(source, restored)

    connection = sqlite3.connect(restored)
    try:
        with connection:
            connection.execute("DELETE FROM stage_outputs")
    finally:
        connection.close()

    problems = verify(source, restored)

    assert problems and "行数不一致" in problems[0]


def test_verify_catches_changed_content(tmp_path):
    """行数一样但内容变了，也必须报出来——只数行数的校验抓不到这个。"""
    source = _seeded_database(tmp_path / "runs.sqlite3")
    restored = tmp_path / "restored.sqlite3"
    restore(source, restored)

    connection = sqlite3.connect(restored)
    try:
        with connection:
            connection.execute("UPDATE stage_outputs SET payload_json = '{\"tampered\": true}'")
    finally:
        connection.close()

    problems = verify(source, restored)

    assert problems and "内容摘要不一致" in problems[0]


def test_verify_catches_a_missing_table(tmp_path):
    source = _seeded_database(tmp_path / "runs.sqlite3")
    restored = tmp_path / "restored.sqlite3"
    restore(source, restored)

    connection = sqlite3.connect(restored)
    try:
        with connection:
            connection.execute("DROP TABLE stage_timings")
    finally:
        connection.close()

    problems = verify(source, restored)

    assert problems and "表不一致" in problems[0]


def test_drill_backs_up_and_restores_through_the_backup_file(tmp_path):
    """演练必须**从备份恢复**，而不是从原库复制——后者证明不了备份可用。"""
    source = _seeded_database(tmp_path / "runs.sqlite3")
    workdir = tmp_path / "drill"

    result = drill(source, workdir)

    assert result["problems"] == []
    backup_path = workdir / "backup" / "runs.sqlite3"
    restored_path = workdir / "restored" / "runs.sqlite3"
    assert backup_path.exists() and restored_path.exists()
    assert list_tables(sqlite3.connect(restored_path)) == list_tables(sqlite3.connect(source))
