import json
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
from app_review_insights.rag.embeddings import cosine_similarity

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
            row = connection.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()
            return bool(row and row[0])
        finally:
            connection.close()
    except sqlite3.Error:
        return False


def build_fts_query(text: str, match_mode: str = "and") -> str:
    """把用户查询转成 FTS5 MATCH 表达式：原始词按空白切分，词内 CJK 短语相邻匹配。

    - 原始词「订阅」→ 短语 "订 阅"（要求相邻，避免误命中分散的订/阅）
    - 原始词「订阅 价格」→ "订 阅" AND "价 格"（词边界保留）
    - 「太贵了，续费」→ "太 贵 了" AND "续 费"（全角标点作为 run 边界，CJK run 同样分段）
    - 「订阅App」→ "订 阅" AND "app"（拉丁 run 独立加引号）

    match_mode="and" 时所有词 AND 连接（严格）；"or" 时任一词命中即可（宽松，
    用于严格检索无结果时的回退）。
    """
    raw_terms = [term.strip().lower() for term in re.split(r"\s+", text) if term.strip()]
    fts_terms: list[str] = []
    for term in raw_terms:
        # CJK run 与拉丁/数字 run 分开切（\w 会匹配 CJK，需显式区分，CJK 与拉丁相邻处也是边界）
        runs = re.findall(r"[\u4e00-\u9fff]+|[a-z0-9_]+", term)
        for run in runs:
            if re.fullmatch(r"[\u4e00-\u9fff]+", run):
                fts_terms.append(f'"{_cjk_segment(run)}"')
            else:
                fts_terms.append(f'"{run}"')
    if not fts_terms:
        return '"__no_match__"'
    joiner = " OR " if match_mode == "or" else " AND "
    return joiner.join(fts_terms)


def _cjk_segment(text: str) -> str:
    """在 CJK 字符之间及 CJK 与拉丁/数字边界之间插入空格。

    FTS5 unicode61 会把相邻的 CJK 与拉丁字符合并成单个 token（如「阅App」→
    「阅app」），导致索引与查询两侧不对称；显式切分后两侧 token 一致，
    中文检索结果可复现（英文单词不被拆分）。
    """
    return re.sub(
        r"(?<=[\u4e00-\u9fff])(?=[\u4e00-\u9fff])"
        r"|(?<=[\u4e00-\u9fff])(?=[^\u4e00-\u9fff\s])"
        r"|(?<=[^\u4e00-\u9fff\s])(?=[\u4e00-\u9fff])",
        " ",
        text,
    )


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
                CREATE TABLE IF NOT EXISTS embeddings (
                    review_id TEXT PRIMARY KEY,
                    vector_json TEXT NOT NULL
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
            connection.execute("DELETE FROM corpus_fts WHERE review_id = ?", (review.review_id,))
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
        """FTS5 检索：先严格 AND；无结果时回退宽松 OR，保证真实问句有召回。"""
        strict = self._run_fts_query(query, app_ids=app_ids, limit=limit, match_mode="and")
        if strict:
            return strict
        return self._run_fts_query(query, app_ids=app_ids, limit=limit, match_mode="or")

    def _run_fts_query(
        self,
        query: str,
        app_ids: list[str] | None,
        limit: int,
        match_mode: str,
    ) -> list[dict[str, Any]]:
        fts_query = build_fts_query(query, match_mode=match_mode)
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
            connection.execute(
                "DELETE FROM embeddings WHERE review_id IN "
                "(SELECT review_id FROM corpus WHERE app_id = ?)",
                (app_id,),
            )
            cursor = connection.execute("DELETE FROM corpus WHERE app_id = ?", (app_id,))
            connection.execute("DELETE FROM corpus_fts WHERE app_id = ?", (app_id,))
        return cursor.rowcount

    # ---- embeddings ----
    def upsert_embedding(self, review_id: str, vector: list[float]) -> None:
        with self._session() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO embeddings(review_id, vector_json)
                VALUES (?, ?)
                """,
                (review_id, json.dumps(vector)),
            )

    def search_embeddings(
        self,
        query_vector: list[float],
        app_ids: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self._session() as connection:
            if app_ids:
                placeholders = ",".join("?" for _ in app_ids)
                rows = connection.execute(
                    f"""
                    SELECT e.review_id, e.vector_json, c.app_id, c.content,
                           c.platform, c.source, c.region AS storefront
                    FROM embeddings e
                    JOIN corpus c ON c.review_id = e.review_id
                    WHERE c.app_id IN ({placeholders})
                    """,
                    app_ids,
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT e.review_id, e.vector_json, c.app_id, c.content,
                           c.platform, c.source, c.region AS storefront
                    FROM embeddings e
                    JOIN corpus c ON c.review_id = e.review_id
                    """
                ).fetchall()
        scored = []
        for row in rows:
            vector = json.loads(row["vector_json"])
            score = cosine_similarity(query_vector, vector)
            if score > 0:
                item = dict(row)
                item["score"] = score
                item.pop("vector_json", None)
                scored.append(item)
        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[:limit]
