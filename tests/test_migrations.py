"""版本化 schema 迁移。

此前建表全靠 `CREATE TABLE IF NOT EXISTS`，于是 `ALTER TABLE ADD COLUMN` 这类
变更在**已存在**的库上会安静地不生效：本地新库测试全绿，线上表结构还是旧的。

现在用 `PRAGMA user_version` 记录版本，打开库时补齐待执行的迁移步骤。
"""

from __future__ import annotations

from app_review_insights.storage.agent_repository import AgentRepository
from app_review_insights.storage.migrations import (
    AGENT_MIGRATIONS,
    LATEST_AGENT_VERSION,
    LATEST_RUNS_VERSION,
    RUNS_MIGRATIONS,
    apply_migrations,
)
from app_review_insights.storage.repository import RunRepository
from app_review_insights.storage.sqlite import connect


def _user_version(path) -> int:
    connection = connect(path)
    try:
        return connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()


def _tables(path) -> set[str]:
    connection = connect(path)
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        connection.close()
    return {row["name"] for row in rows}


# ── 迁移机制本身 ─────────────────────────────────────────────────────────────


def test_apply_migrations_runs_pending_steps_in_order(tmp_path):
    path = tmp_path / "m.sqlite3"
    connection = connect(path)
    try:
        apply_migrations(connection, [(1, "CREATE TABLE t (a TEXT);")])
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1

        apply_migrations(
            connection,
            [(1, "CREATE TABLE t (a TEXT);"), (2, "ALTER TABLE t ADD COLUMN b TEXT;")],
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        connection.execute("INSERT INTO t(a, b) VALUES ('x', 'y')")
        assert connection.execute("SELECT b FROM t").fetchone()[0] == "y"
    finally:
        connection.close()


def test_apply_migrations_does_not_rerun_applied_steps(tmp_path):
    """已执行的步骤不得重跑——否则 ALTER 类脚本会二次执行并报错。"""
    path = tmp_path / "m.sqlite3"
    connection = connect(path)
    try:
        # 故意用不带 IF NOT EXISTS 的脚本：一旦重跑必然抛"表已存在"
        apply_migrations(connection, [(1, "CREATE TABLE t (a TEXT);")])
        apply_migrations(connection, [(1, "CREATE TABLE t (a TEXT);")])
    finally:
        connection.close()


def test_apply_migrations_leaves_untouched_database_at_version_zero(tmp_path):
    path = tmp_path / "m.sqlite3"
    connection = connect(path)
    try:
        apply_migrations(connection, [])
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        connection.close()


# ── 两个真实数据库 ───────────────────────────────────────────────────────────


def test_fresh_runs_database_is_stamped_with_latest_version(tmp_path):
    path = tmp_path / "runs.sqlite3"
    RunRepository(path)

    assert _user_version(path) == LATEST_RUNS_VERSION
    assert {"runs", "stage_outputs", "events", "model_usage"} <= _tables(path)


def test_fresh_agent_database_is_stamped_with_latest_version(tmp_path):
    path = tmp_path / "agent.sqlite3"
    AgentRepository(path)

    assert _user_version(path) == LATEST_AGENT_VERSION
    assert {"agent_runs", "monitor_jobs", "reports", "corpus", "embeddings"} <= _tables(path)


def test_legacy_runs_database_is_upgraded_in_place(tmp_path):
    """旧代码建的库表已存在但 user_version=0，必须平滑升级而不是重建或报错。"""
    path = tmp_path / "runs.sqlite3"
    connection = connect(path)
    connection.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()
    assert _user_version(path) == 0

    repository = RunRepository(path)

    assert _user_version(path) == LATEST_RUNS_VERSION
    assert {"stage_outputs", "events", "model_usage"} <= _tables(path)
    assert repository.list_runs() == []  # 原有数据可读，未被重建


def test_legacy_runs_database_keeps_existing_rows(tmp_path):
    path = tmp_path / "runs.sqlite3"
    connection = connect(path)
    connection.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()

    repository = RunRepository(path)
    from datetime import UTC, datetime

    from app_review_insights.models import (
        AnalysisRequest,
        RunRecord,
        RunStatus,
        SourceType,
        Stage,
    )

    repository.save_run(
        RunRecord(
            run_id="kept",
            request=AnalysisRequest(source_type=SourceType.JSON, analysis_goal="分析订阅转化"),
            current_stage=Stage.COLLECT,
            status=RunStatus.RUNNING,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )

    assert repository.get_run("kept").run_id == "kept"


# ── 迁移清单自检 ─────────────────────────────────────────────────────────────


def test_migration_versions_are_contiguous_starting_at_one():
    for name, migrations in (("runs", RUNS_MIGRATIONS), ("agent", AGENT_MIGRATIONS)):
        versions = [version for version, _ in migrations]
        assert versions == list(range(1, len(migrations) + 1)), f"{name} 迁移版本不连续：{versions}"


def test_latest_version_constants_match_the_lists():
    assert LATEST_RUNS_VERSION == RUNS_MIGRATIONS[-1][0]
    assert LATEST_AGENT_VERSION == AGENT_MIGRATIONS[-1][0]
