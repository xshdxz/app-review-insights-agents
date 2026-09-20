"""模型响应缓存：内容寻址，命中即不再发起调用。

为什么放在 provider 边界：这里是"一次完整请求"的唯一入口，决定响应的全部因素
（模型、温度、输出上限、Schema、system、user）在这一层都可见。

**键必须覆盖全部决定因素**。漏掉模型或温度就不是"缓存"，而是"把别的请求的答案返回给你"——
那是正确性事故，不是性能优化。所以键里显式包含这六项；prompt 文本既然已经在键里，
就不再单独放 prompt 指纹，那是冗余的。

与录制回放（llm/recording.py）的关系：两者都是内容寻址，但目的不同——录制是为了
**可复现**（离线演示、回归），缓存是为了**省成本**（同一次实验重复跑）。缓存有 TTL、
可随时清空；录制件是必须长期保留的素材。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app_review_insights.storage.sqlite import connect

logger = logging.getLogger("ari-llm")

T = TypeVar("T", bound=BaseModel)

#: 键各段之间的分隔符：避免"前一段结尾拼上后一段开头"撞出同一个键。
_FIELD_SEP = chr(0x1F)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_cache (
    key TEXT PRIMARY KEY,
    schema_name TEXT NOT NULL,
    model TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_llm_cache_created_at ON llm_cache(created_at);
CREATE TABLE IF NOT EXISTS llm_cache_stats (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


def cache_key(
    *,
    model: str,
    temperature: float,
    max_tokens: int,
    schema_name: str,
    system: str,
    user: str,
) -> str:
    """按请求的全部决定因素推导缓存键。

    任何一个字段变了，键就变——这正是"换模型不会命中旧响应"的保证。
    """
    payload = _FIELD_SEP.join(
        (
            model,
            repr(float(temperature)),
            str(int(max_tokens)),
            schema_name,
            system,
            user,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CacheStats:
    entries: int
    hits: int
    misses: int

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 3) if total else 0.0


class ResponseCache:
    """SQLite 支撑的响应缓存。过期条目按 TTL 视为未命中，可被 prune 清掉。"""

    def __init__(self, path: Path | str, ttl_days: int = 7) -> None:
        if ttl_days < 0:
            raise ValueError("ttl_days 不能为负")
        self.path = Path(path)
        self.ttl_days = ttl_days
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect(self.path)

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            with connection:
                connection.executescript(_SCHEMA)
        finally:
            connection.close()

    def _cutoff(self, now: datetime) -> str:
        return (now - timedelta(days=self.ttl_days)).isoformat()

    def get(self, key: str, now: datetime | None = None) -> dict[str, Any] | None:
        """取一条未过期的缓存；命中会把该条目的命中计数 +1。"""
        moment = now or datetime.now(UTC)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT response_json FROM llm_cache WHERE key = ? AND created_at >= ?",
                (key, self._cutoff(moment)),
            ).fetchone()
            if row is None:
                return None
            with connection:
                connection.execute("UPDATE llm_cache SET hits = hits + 1 WHERE key = ?", (key,))
            return json.loads(row["response_json"])
        finally:
            connection.close()

    def put(
        self,
        key: str,
        *,
        schema_name: str,
        model: str,
        response: dict[str, Any],
        now: datetime | None = None,
    ) -> None:
        moment = now or datetime.now(UTC)
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO llm_cache(key, schema_name, model, response_json, created_at, hits)
                    VALUES (?, ?, ?, ?, ?, 0)
                    ON CONFLICT(key) DO UPDATE SET
                        response_json = excluded.response_json,
                        created_at = excluded.created_at,
                        schema_name = excluded.schema_name,
                        model = excluded.model
                    """,
                    (
                        key,
                        schema_name,
                        model,
                        json.dumps(response, ensure_ascii=False),
                        moment.isoformat(),
                    ),
                )
        finally:
            connection.close()

    def note_miss(self) -> None:
        """记一次未命中。未命中率与命中率是一体两面，缺一半就没法算。"""
        self._bump("misses")

    def _bump(self, name: str, amount: int = 1) -> None:
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO llm_cache_stats(name, value) VALUES (?, ?)
                    ON CONFLICT(name) DO UPDATE SET value = value + excluded.value
                    """,
                    (name, amount),
                )
        finally:
            connection.close()

    def _counter(self, name: str) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT value FROM llm_cache_stats WHERE name = ?", (name,)
            ).fetchone()
            return int(row["value"]) if row else 0
        finally:
            connection.close()

    def stats(self) -> CacheStats:
        connection = self._connect()
        try:
            entries = connection.execute("SELECT COUNT(*) AS n FROM llm_cache").fetchone()["n"]
            hits = connection.execute(
                "SELECT COALESCE(SUM(hits), 0) AS n FROM llm_cache"
            ).fetchone()["n"]
        finally:
            connection.close()
        return CacheStats(entries=int(entries), hits=int(hits), misses=self._counter("misses"))

    def vacuum(self) -> None:
        """回收已删除条目的磁盘空间。`VACUUM` 不能在事务里跑，所以自开连接。"""
        connection = self._connect()
        try:
            connection.execute("VACUUM")
        finally:
            connection.close()

    def prune(self, now: datetime | None = None) -> int:
        """删掉过期条目，返回删除条数。"""
        moment = now or datetime.now(UTC)
        connection = self._connect()
        try:
            with connection:
                cursor = connection.execute(
                    "DELETE FROM llm_cache WHERE created_at < ?", (self._cutoff(moment),)
                )
            return int(cursor.rowcount)
        finally:
            connection.close()


class CachingProvider:
    """把 provider 包一层：命中就返回缓存，不再调用内层。

    刻意**不**记录 `model_usage`：命中不是一次模型调用，混进"调用次数"会让面板上的
    成本与调用数都失真。命中率由 :meth:`ResponseCache.stats` 单独给出；
    "省了多少钱"用同一输入的两次真实运行对比来测，不靠估算。
    """

    def __init__(self, inner: Any, cache: ResponseCache) -> None:
        self.inner = inner
        self.cache = cache

    @property
    def model(self) -> str:
        return getattr(self.inner, "model", "")

    @property
    def temperature(self) -> float:
        return float(getattr(self.inner, "temperature", 0.0))

    @property
    def max_tokens(self) -> int:
        return int(getattr(self.inner, "max_tokens", 0))

    def generate(self, system_prompt: str, user_prompt: str, schema: type[T]) -> T:
        key = cache_key(
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            schema_name=schema.__name__,
            system=system_prompt,
            user=user_prompt,
        )
        cached = self.cache.get(key)
        if cached is not None:
            try:
                return schema.model_validate(cached)
            except ValidationError:
                # 缓存条目与本 Schema 对不上（Schema 改过而键没变就会这样）。
                # 按未命中处理并重新调用——绝不能把坏数据交给调用方。
                logger.warning("缓存条目与 %s 不匹配，按未命中处理", schema.__name__)
        self.cache.note_miss()
        result = self.inner.generate(system_prompt, user_prompt, schema)
        self.cache.put(
            key,
            schema_name=schema.__name__,
            model=self.model,
            response=result.model_dump(mode="json"),
        )
        return result
