import json
import sqlite3
from pathlib import Path
from typing import Any

from app_review_insights.models import RunRecord, Stage, StageEvent


class RunRepository:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
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
                """
            )

    def save_run(self, run: RunRecord) -> None:
        with self._connect() as connection:
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
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return RunRecord.model_validate_json(row["payload_json"])

    def list_runs(self) -> list[RunRecord]:
        with self._connect() as connection:
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
        with self._connect() as connection:
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
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM stage_outputs
                WHERE run_id = ? AND stage = ? AND batch_index = ?
                """,
                (run_id, stage.value, batch_index),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def add_event(self, run_id: str, event: StageEvent) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO events(run_id, payload_json) VALUES (?, ?)",
                (run_id, event.model_dump_json()),
            )

    def list_events(self, run_id: str) -> list[StageEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM events WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [StageEvent.model_validate_json(row["payload_json"]) for row in rows]
