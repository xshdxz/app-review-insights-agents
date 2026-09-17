import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from app_review_insights.llm.usage import ModelUsage
from app_review_insights.models import RunRecord, Stage, StageEvent
from app_review_insights.storage.sqlite import connect


class RunRepository:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect(self.path)

    @contextmanager
    def _session(self):
        """Yield a connection that commits on success and always closes."""
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._session() as connection:
            connection.executescript(
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
                """
            )

    def save_run(self, run: RunRecord) -> None:
        with self._session() as connection:
            connection.execute(
                """
                INSERT INTO runs(run_id, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (run.run_id, run.model_dump_json(), run.updated_at.isoformat()),
            )

    def get_run(self, run_id: str) -> RunRecord:
        with self._session() as connection:
            row = connection.execute(
                "SELECT payload_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return RunRecord.model_validate_json(row["payload_json"])

    def list_runs(self) -> list[RunRecord]:
        with self._session() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM runs ORDER BY updated_at DESC"
            ).fetchall()
        return [RunRecord.model_validate_json(row["payload_json"]) for row in rows]

    def save_output(
        self,
        run_id: str,
        stage: Stage,
        payload: dict[str, Any],
        batch_index: int = -1,
    ) -> None:
        payload_json = json.dumps(payload, ensure_ascii=False)
        with self._session() as connection:
            connection.execute(
                """
                INSERT INTO stage_outputs(run_id, stage, batch_index, payload_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id, stage, batch_index) DO UPDATE SET
                    payload_json = excluded.payload_json
                """,
                (run_id, stage.value, batch_index, payload_json),
            )

    def get_output(self, run_id: str, stage: Stage, batch_index: int = -1) -> dict[str, Any] | None:
        with self._session() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM stage_outputs
                WHERE run_id = ? AND stage = ? AND batch_index = ?
                """,
                (run_id, stage.value, batch_index),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def add_event(self, run_id: str, event: StageEvent) -> None:
        with self._session() as connection:
            connection.execute(
                "INSERT INTO events(run_id, payload_json) VALUES (?, ?)",
                (run_id, event.model_dump_json()),
            )

    def record_model_usage(self, usage: ModelUsage) -> None:
        """记一笔模型调用账目（token 与估算费用）。"""
        with self._session() as connection:
            connection.execute(
                """
                INSERT INTO model_usage(
                    run_id, stage, model, prompt_tokens, completion_tokens,
                    total_tokens, latency_ms, estimated_cost_usd, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    usage.run_id,
                    usage.stage,
                    usage.model,
                    usage.prompt_tokens,
                    usage.completion_tokens,
                    usage.total_tokens,
                    usage.latency_ms,
                    usage.estimated_cost_usd,
                    usage.created_at.isoformat(),
                ),
            )

    def model_usage_summary(
        self,
        run_id: str | None = None,
        since: datetime | None = None,
    ) -> dict[str, Any]:
        """汇总调用次数、token 与估算费用。

        `run_id` 为空时不限运行；`since` 为空时不限时间（日预算传当日 00:00 UTC）。
        """
        since_iso = since.isoformat() if since is not None else None
        with self._session() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(estimated_cost_usd), 0.0) AS estimated_cost_usd
                FROM model_usage
                WHERE (? IS NULL OR run_id = ?)
                  AND (? IS NULL OR created_at >= ?)
                """,
                (run_id, run_id, since_iso, since_iso),
            ).fetchone()
        return {
            "calls": row["calls"],
            "prompt_tokens": row["prompt_tokens"],
            "completion_tokens": row["completion_tokens"],
            "total_tokens": row["total_tokens"],
            "estimated_cost_usd": row["estimated_cost_usd"],
        }

    def model_usage_by_stage(self, run_id: str | None = None) -> dict[str, dict[str, Any]]:
        """按阶段拆解开销，用于定位成本大头。"""
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT COALESCE(stage, 'unknown') AS stage,
                       COUNT(*) AS calls,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(estimated_cost_usd), 0.0) AS estimated_cost_usd
                FROM model_usage
                WHERE (? IS NULL OR run_id = ?)
                GROUP BY COALESCE(stage, 'unknown')
                """,
                (run_id, run_id),
            ).fetchall()
        return {
            row["stage"]: {
                "calls": row["calls"],
                "total_tokens": row["total_tokens"],
                "estimated_cost_usd": row["estimated_cost_usd"],
            }
            for row in rows
        }

    def list_events(self, run_id: str) -> list[StageEvent]:
        with self._session() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM events WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [StageEvent.model_validate_json(row["payload_json"]) for row in rows]
