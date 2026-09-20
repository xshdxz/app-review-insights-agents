import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app_review_insights.llm.usage import ModelUsage
from app_review_insights.models import RunRecord, RunStatus, Stage, StageEvent
from app_review_insights.observability import summarize
from app_review_insights.storage import lease
from app_review_insights.storage.migrations import RUNS_MIGRATIONS, apply_migrations
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

    @contextmanager
    def _immediate_session(self):
        """立即事务：进入即取写锁，供"检查 + 占用"这类不能有竞态的路径使用。

        `BEGIN IMMEDIATE` 必须是本会话的**第一条**语句：此前跑过 DML 的话 sqlite3
        已经隐式开了事务，再发它会抛 "cannot start a transaction within a transaction"
        （Step 0 的 S-1b 实测）。因此这里自开连接、不走 `_session`。
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._session() as connection:
            apply_migrations(connection, RUNS_MIGRATIONS)

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

    def acquire_run(
        self,
        run: RunRecord,
        *,
        timeout_seconds: float,
        now: datetime | None = None,
    ) -> RunRecord | None:
        """原子地"确认该 App 没有活跃运行 + 写入新运行"。

        检查与写入必须在同一个 `BEGIN IMMEDIATE` 事务里完成：此前的写法是
        `find_active_run()` 之后再 `save_run()`，两步之间的空档里另一个进程可以
        插进来，于是同一个 App 被分析两遍、模型额度烧两份。

        返回新写入的运行；已有活跃运行则返回 `None`。
        """
        moment = now or datetime.now(UTC)
        cutoff = (moment - timedelta(seconds=timeout_seconds)).isoformat()
        with self._immediate_session() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM runs WHERE updated_at >= ? ORDER BY updated_at DESC",
                (cutoff,),
            ).fetchall()
            for row in rows:
                existing = RunRecord.model_validate_json(row["payload_json"])
                if existing.request.app_url != run.request.app_url:
                    continue
                if lease.blocks_new_run(existing, moment, timeout_seconds):
                    return None
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
        return run

    def take_over(
        self,
        run_id: str,
        *,
        owner: str,
        timeout_seconds: float,
        now: datetime | None = None,
    ) -> RunRecord | None:
        """原子地把一个**无人持有**的运行接管过来；仍被活着的进程持有则返回 `None`。

        返回 `None` 是正常的竞态结果（另一个进程刚好抢先接管），调用方应当就此打住，
        绝不能继续跑——两个执行者写同一份检查点会把结果搅坏。
        """
        moment = now or datetime.now(UTC)
        with self._immediate_session() as connection:
            row = connection.execute(
                "SELECT payload_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            record = RunRecord.model_validate_json(row["payload_json"])
            if lease.is_held(record, moment, timeout_seconds):
                return None
            taken = record.model_copy(
                update={"lease_owner": owner, "heartbeat_at": moment, "updated_at": moment}
            )
            connection.execute(
                "UPDATE runs SET payload_json = ?, updated_at = ? WHERE run_id = ?",
                (taken.model_dump_json(), moment.isoformat(), run_id),
            )
        return taken

    def can_resume(
        self,
        run: RunRecord,
        *,
        timeout_seconds: float,
        now: datetime | None = None,
    ) -> bool:
        """这个运行现在能不能接管续跑（UI 的按钮条件与编排器共用同一判定）。"""
        return lease.can_resume(run, now or datetime.now(UTC), timeout_seconds)

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

    def record_stage_timing(
        self,
        run_id: str,
        stage: Stage,
        duration_ms: float,
        *,
        started_at: datetime,
        ended_at: datetime,
    ) -> None:
        """记一条阶段耗时。指标端点据此算 P50/P95——没有它，"慢在哪一段"只能靠猜。"""
        with self._session() as connection:
            connection.execute(
                """
                INSERT INTO stage_timings(run_id, stage, duration_ms, started_at, ended_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    stage.value,
                    float(duration_ms),
                    started_at.isoformat(),
                    ended_at.isoformat(),
                ),
            )

    def stage_timing_summary(
        self,
        since: datetime | None = None,
    ) -> dict[str, dict[str, float]]:
        """按阶段汇总耗时（count / p50 / p95 / max，毫秒）。"""
        since_iso = since.isoformat() if since is not None else None
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT stage, duration_ms FROM stage_timings
                WHERE (? IS NULL OR started_at >= ?)
                """,
                (since_iso, since_iso),
            ).fetchall()
        grouped: dict[str, list[float]] = {}
        for row in rows:
            grouped.setdefault(row["stage"], []).append(float(row["duration_ms"]))
        return {stage: summarize(values) for stage, values in grouped.items()}

    def model_latency_summary(
        self,
        since: datetime | None = None,
    ) -> dict[str, dict[str, float]]:
        """按阶段汇总模型调用耗时——与阶段耗时一起看，能分清"慢在模型"还是"慢在别处"。"""
        since_iso = since.isoformat() if since is not None else None
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT COALESCE(stage, 'unknown') AS stage, latency_ms FROM model_usage
                WHERE (? IS NULL OR created_at >= ?)
                """,
                (since_iso, since_iso),
            ).fetchall()
        grouped: dict[str, list[float]] = {}
        for row in rows:
            grouped.setdefault(row["stage"], []).append(float(row["latency_ms"]))
        return {stage: summarize(values) for stage, values in grouped.items()}

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

    #: 未终结的运行还可以续跑，任何保留策略都不得删除它们。
    RESUMABLE_STATUSES = (
        RunStatus.PENDING.value,
        RunStatus.RUNNING.value,
        RunStatus.WAITING.value,
    )

    def find_active_run(
        self,
        app_url: str,
        stale_after_minutes: int = 60,
        now: datetime | None = None,
    ) -> RunRecord | None:
        """返回该 App 上仍在进行中的运行；没有则返回 `None`。返回最近更新的一条。

        判定走 `lease.blocks_new_run`：有租约时看**持有者进程是否还活着**，判不出来
        才退回"最近一次更新是否在窗口内"。原先只看 `updated_at` 的写法有个语义漏洞——
        更新得早不等于没人拥有它；而一次崩溃留下的孤儿运行也确实不能永久卡死这个 App。
        """
        moment = now or datetime.now(UTC)
        timeout_seconds = stale_after_minutes * 60
        cutoff = (moment - timedelta(seconds=timeout_seconds)).isoformat()
        with self._session() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM runs WHERE updated_at >= ? ORDER BY updated_at DESC",
                (cutoff,),
            ).fetchall()
        for row in rows:
            run = RunRecord.model_validate_json(row["payload_json"])
            if run.request.app_url != app_url:
                continue
            if lease.blocks_new_run(run, moment, timeout_seconds):
                return run
        return None

    def prune_events(self, keep_per_run: int = 50) -> int:
        """每个运行只保留最近 `keep_per_run` 条事件，返回删除条数。

        events 表每推进一个阶段就写一条，是最容易无限增长的一张表。
        """
        if keep_per_run < 0:
            raise ValueError("keep_per_run must be non-negative")
        with self._session() as connection:
            cursor = connection.execute(
                """
                DELETE FROM events WHERE id IN (
                    SELECT id FROM (
                        SELECT id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY run_id ORDER BY id DESC
                               ) AS rn
                        FROM events
                    ) WHERE rn > ?
                )
                """,
                (keep_per_run,),
            )
            return cursor.rowcount

    def prune_runs(self, older_than_days: int = 90, now: datetime | None = None) -> int:
        """删除超过保留期的**终态**运行及其阶段输出、事件与用量记录。

        `pending` / `running` / `waiting_for_model` 一律保留——它们还能续跑，
        删掉等于丢掉用户还没取回的工作。
        """
        if older_than_days < 0:
            raise ValueError("older_than_days must be non-negative")
        cutoff = ((now or datetime.now(UTC)) - timedelta(days=older_than_days)).isoformat()
        with self._session() as connection:
            rows = connection.execute(
                "SELECT run_id, payload_json FROM runs WHERE updated_at < ?", (cutoff,)
            ).fetchall()
            victims = [
                row["run_id"]
                for row in rows
                if RunRecord.model_validate_json(row["payload_json"]).status.value
                not in self.RESUMABLE_STATUSES
            ]
            for run_id in victims:
                connection.execute("DELETE FROM stage_outputs WHERE run_id = ?", (run_id,))
                connection.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
                connection.execute("DELETE FROM model_usage WHERE run_id = ?", (run_id,))
                # 阶段耗时同样跟着运行走，否则它就是一个只涨不跌的表
                connection.execute("DELETE FROM stage_timings WHERE run_id = ?", (run_id,))
                connection.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        return len(victims)

    def vacuum(self) -> None:
        """回收已删除数据的磁盘空间。

        `VACUUM` 不能在事务内执行，因此这里自己开连接而不是走 `_session`。
        """
        connection = self._connect()
        try:
            connection.execute("VACUUM")
        finally:
            connection.close()

    def list_events(self, run_id: str) -> list[StageEvent]:
        with self._session() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM events WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [StageEvent.model_validate_json(row["payload_json"]) for row in rows]
