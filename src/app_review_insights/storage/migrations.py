"""版本化 schema 迁移。

此前建表全靠 `CREATE TABLE IF NOT EXISTS`，于是 `ALTER TABLE ADD COLUMN` 这类
变更在**已存在**的库上会安静地不生效：本地新库测试全绿，线上表结构还是旧的。

现在用 `PRAGMA user_version` 记录版本，打开库时补齐所有待执行的迁移步骤。

新增迁移的规矩：

1. 版本号从 1 开始连续递增，直接追加到列表末尾；
2. **永远不要修改已发布的迁移脚本**——它在别人的库上已经跑过了，改了也不会重跑，
   只会让新库和老库的 schema 悄悄分叉；
3. 建表语句保留 `IF NOT EXISTS`，这样旧代码建出来的库能平滑升级。
"""

from __future__ import annotations

import sqlite3

#: (版本号, DDL 脚本)
Migration = tuple[int, str]

#: 流水线检查点库（DATABASE_PATH）
RUNS_MIGRATIONS: list[Migration] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stage_outputs (
            run_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            batch_index INTEGER NOT NULL DEFAULT -1,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (run_id, stage, batch_index)
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS model_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            stage TEXT,
            model TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL,
            total_tokens INTEGER NOT NULL,
            latency_ms REAL NOT NULL,
            estimated_cost_usd REAL NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_model_usage_run ON model_usage(run_id);
        """,
    ),
    (
        2,
        """
        CREATE TABLE IF NOT EXISTS stage_timings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            duration_ms REAL NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_stage_timings_stage ON stage_timings(stage);
        CREATE INDEX IF NOT EXISTS idx_stage_timings_run ON stage_timings(run_id);
        """,
    ),
    (
        3,
        """
        CREATE TABLE IF NOT EXISTS run_inputs (
            run_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS run_cancellations (
            run_id TEXT PRIMARY KEY,
            requested_at TEXT NOT NULL
        );
        """,
    ),
    # v3 已随提交发布：之后的迁移只能往后追加，已发布的版本号不能改语义
    (
        4,
        """
        CREATE TABLE IF NOT EXISTS queue_executor_heartbeats (
            owner TEXT PRIMARY KEY,
            heartbeat_at TEXT NOT NULL
        );
        """,
    ),
]

#: Agent / 监控 / 语料库（AGENT_DB_PATH）
AGENT_MIGRATIONS: list[Migration] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS agent_runs (
            run_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS monitor_jobs (
            job_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reports (
            report_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS corpus (
            review_id TEXT PRIMARY KEY,
            app_id TEXT NOT NULL,
            content TEXT NOT NULL,
            language TEXT,
            platform TEXT,
            region TEXT,
            source TEXT,
            published_at TEXT
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS corpus_fts USING fts5(
            review_id UNINDEXED,
            app_id UNINDEXED,
            content,
            tokenize='unicode61'
        );
        CREATE TABLE IF NOT EXISTS embeddings (
            review_id TEXT PRIMARY KEY,
            vector_json TEXT NOT NULL
        );
        """,
    ),
]

LATEST_RUNS_VERSION = RUNS_MIGRATIONS[-1][0]
LATEST_AGENT_VERSION = AGENT_MIGRATIONS[-1][0]


def apply_migrations(connection: sqlite3.Connection, migrations: list[Migration]) -> int:
    """执行所有高于当前 `user_version` 的迁移步骤，返回最终版本。

    已执行过的步骤会被跳过——这正是「改表结构不生效」问题的解药。
    """
    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    for version, script in migrations:
        if version <= current:
            continue
        connection.executescript(script)
        # PRAGMA 不支持参数绑定；version 来自代码内常量，不是外部输入
        connection.execute("PRAGMA user_version = " + str(int(version)))
        current = version
    return current
