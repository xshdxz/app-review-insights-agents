import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app_review_insights.models import (
    AgentRun,
    AgentRunStatus,
    MonitorJob,
    MonitorReport,
    Review,
)

_CORPUS_FTS_QUERY = (
    "SELECT c.review_id, c.app_id, c.content, c.platform, c.source, c.region AS storefront, "
    "       -bm25(corpus_fts) AS score "
    "FROM corpus_fts JOIN corpus c ON c.review_id = corpus_fts.review_id "
    "WHERE corpus_fts MATCH ? {app_filter} "
    "ORDER BY score DESC LIMIT ?"
)


def fts5_available(path: Path | None = None) -> bool:
    """探测当前 SQLite 是否编译了 FTS5（用 :memory: 连接，不落盘）。"""
    try:
        connection = sqlite3.connect(path if path is not None else ":memory:")
        try:
            row = connection.execute(
                "SELECT sqlite_compileoption_used('ENABLE_FTS5')"
            ).fetchone()
            return bool(row and row[0])
        finally:
            connection.close()
    except sqlite3.Error:
        return False


def build_fts_query(text: str) -> str:
    """把用户查询转成 FTS5 MATCH 表达式：CJK 短语按字符相邻匹配、英文单词独立匹配。

    CJK 字符之间插入空格（与索引侧 _cjk_segment 对称），连续 CJK 字符组成
    一个带引号的短语（如 "订 阅"），保证「订阅」不误命中「我订了酒店，阅读体验不错」。
    """
    segmented = _cjk_segment(text)
    tokens = re.findall(r"[\w\u4e00-\u9fff]+", segmented.lower())
    if not tokens:
        return '"__no_match__"'
    terms: list[str] = []
    cjk_run: list[str] = []
    for token in tokens:
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            # _cjk_segment 已把 CJK 拆成单字符 token；相邻 CJK 收进同一短语
            cjk_run.append(" ".join(token))
        else:
            if cjk_run:
                terms.append(f'"{ " ".join(cjk_run) }"')
                cjk_run = []
            terms.append(f'"{token}"')
    if cjk_run:
        terms.append(f'"{ " ".join(cjk_run) }"')
    return " AND ".join(terms)


def _cjk_segment(text: str) -> str:
    """在相邻 CJK 字符之间插入空格（不拆分英文单词）。

    FTS5 unicode61 对连续 CJK 的切分行为依赖 SQLite 版本；显式切分后
    索引与查询两侧行为一致，中文检索结果可复现。
    """
    return re.sub(r"(?<=[\u4e00-\u9fff])(?=[\u4e00-\u9fff])", " ", text)


class AgentRepository:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _session(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        if not fts5_available():
            raise RuntimeError(
                "当前 Python 的 SQLite 未启用 FTS5（ENABLE_FTS5），无法创建评论语料索引"
            )
        with self._session() as connection:
            connection.executescript(
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
                """
            )

    # ---- agent runs ----
    def save_agent_run(self, run: AgentRun) -> None:
        with self._session() as connection:
            connection.execute(
                """
                INSERT INTO agent_runs(run_id, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (run.run_id, run.model_dump_json(), run.updated_at.isoformat()),
            )

    def get_agent_run(self, run_id: str) -> AgentRun:
        with self._session() as connection:
            row = connection.execute(
                "SELECT payload_json FROM agent_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return AgentRun.model_validate_json(row["payload_json"])

    def list_agent_runs(
        self, status: AgentRunStatus | None = None, limit: int = 100
    ) -> list[AgentRun]:
        with self._session() as connection:
            if status is None:
                rows = connection.execute(
                    "SELECT payload_json FROM agent_runs ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT payload_json FROM agent_runs WHERE ? = "
                    "json_extract(payload_json, '$.status') "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (status.value, limit),
                ).fetchall()
        return [AgentRun.model_validate_json(row["payload_json"]) for row in rows]

    # ---- monitor jobs ----
    def save_job(self, job: MonitorJob) -> None:
        with self._session() as connection:
            connection.execute(
                """
                INSERT INTO monitor_jobs(job_id, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (job.job_id, job.model_dump_json(), job.updated_at.isoformat()),
            )

    def get_job(self, job_id: str) -> MonitorJob:
        with self._session() as connection:
            row = connection.execute(
                "SELECT payload_json FROM monitor_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return MonitorJob.model_validate_json(row["payload_json"])

    def list_jobs(self) -> list[MonitorJob]:
        with self._session() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM monitor_jobs ORDER BY updated_at DESC"
            ).fetchall()
        return [MonitorJob.model_validate_json(row["payload_json"]) for row in rows]

    def delete_job(self, job_id: str) -> None:
        with self._session() as connection:
            connection.execute("DELETE FROM monitor_jobs WHERE job_id = ?", (job_id,))

    # ---- reports ----
    def save_report(self, report: MonitorReport) -> None:
        with self._session() as connection:
            connection.execute(
                """
                INSERT INTO reports(report_id, payload_json, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(report_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    created_at = excluded.created_at
                """,
                (report.report_id, report.model_dump_json(), report.created_at.isoformat()),
            )

    def get_report(self, report_id: str) -> MonitorReport:
        with self._session() as connection:
            row = connection.execute(
                "SELECT payload_json FROM reports WHERE report_id = ?", (report_id,)
            ).fetchone()
        if row is None:
            raise KeyError(report_id)
        return MonitorReport.model_validate_json(row["payload_json"])

    def list_reports(self, limit: int = 50) -> list[MonitorReport]:
        with self._session() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM reports ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [MonitorReport.model_validate_json(row["payload_json"]) for row in rows]

    def latest_report(self, app_url: str) -> MonitorReport | None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT payload_json FROM reports WHERE "
                "json_extract(payload_json, '$.app_url') = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (app_url,),
            ).fetchone()
        return MonitorReport.model_validate_json(row["payload_json"]) if row else None

    # ---- corpus ----
    def upsert_corpus(self, review: Review) -> None:
        with self._session() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO corpus(
                    review_id, app_id, content, language, platform, region, source, published_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review.review_id,
                    review.app_id,
                    review.content_original,
                    review.language,
                    review.platform,
                    review.storefront,
                    review.source,
                    review.published_at.isoformat(),
                ),
            )
            connection.execute(
                "DELETE FROM corpus_fts WHERE review_id = ?", (review.review_id,)
            )
            connection.execute(
                "INSERT INTO corpus_fts(review_id, app_id, content) VALUES (?, ?, ?)",
                (review.review_id, review.app_id, _cjk_segment(review.content_original)),
            )

    def search_corpus(
        self,
        query: str,
        app_ids: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        fts_query = build_fts_query(query)
        app_filter = ""
        params: list[Any] = [fts_query]
        if app_ids:
            placeholders = ",".join("?" for _ in app_ids)
            app_filter = f"AND c.app_id IN ({placeholders})"
            params.extend(app_ids)
        params.append(limit)
        with self._session() as connection:
            rows = connection.execute(
                _CORPUS_FTS_QUERY.format(app_filter=app_filter),
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def app_ids(self) -> list[str]:
        with self._session() as connection:
            rows = connection.execute(
                "SELECT DISTINCT app_id FROM corpus ORDER BY app_id"
            ).fetchall()
        return [row["app_id"] for row in rows]

    def delete_app(self, app_id: str) -> int:
        with self._session() as connection:
            cursor = connection.execute("DELETE FROM corpus WHERE app_id = ?", (app_id,))
            connection.execute("DELETE FROM corpus_fts WHERE app_id = ?", (app_id,))
        return cursor.rowcount
