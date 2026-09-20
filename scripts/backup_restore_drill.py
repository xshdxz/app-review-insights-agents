"""备份 / 恢复演练：证明"能恢复"，而不是"应该能恢复"。

为什么不用复制文件：SQLite 在 WAL 模式下直接复制可能拿到**不一致的快照**（主库 + wal + shm
三个文件的时序对不上）。这里用 SQLite 自己的在线备份 API，它不锁库、不中断写入，
拿到的是一致快照。

演练做三件事，缺一不可：

1. 在线备份到目标目录；
2. 从**备份**恢复到另一个目录（不是从原库复制——那证明不了备份可用）；
3. 逐表核对行数与内容摘要，并跑 `PRAGMA integrity_check`。

用法：

    python scripts/backup_restore_drill.py [--workdir output/backup-drill]
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from app_review_insights.config import Settings, load_settings  # noqa: E402

#: 一次核对多少行。表可能很大，全量哈希没必要——抽样能抓住"恢复出来的库是空的/是旧的"。
SAMPLE_ROWS = 500


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def list_tables(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    # 用位置取值而不是列名：调用方传进来的连接未必设了 row_factory，
    # 而这个函数不该对连接的构造方式挑三拣四。
    return [row[0] for row in rows]


def table_digest(connection: sqlite3.Connection, table: str, limit: int = SAMPLE_ROWS) -> str:
    rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid LIMIT {limit}").fetchall()
    payload = "\x1f".join(",".join(str(value) for value in row) for row in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def verify(source: Path, restored: Path) -> list[str]:
    """核对恢复出来的库与源库一致；返回问题列表（空 = 通过）。"""
    problems: list[str] = []
    source_connection = _connect(source)
    restored_connection = _connect(restored)
    try:
        integrity = restored_connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            problems.append(f"{restored.name}: integrity_check={integrity}")

        source_tables = list_tables(source_connection)
        restored_tables = list_tables(restored_connection)
        if source_tables != restored_tables:
            problems.append(
                f"{restored.name}: 表不一致（源 {source_tables} / 恢复 {restored_tables}）"
            )
            return problems

        for table in source_tables:
            source_count = source_connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            restored_count = restored_connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            if source_count != restored_count:
                problems.append(
                    f"{restored.name}.{table}: 行数不一致"
                    f"（源 {source_count} / 恢复 {restored_count}）"
                )
                continue
            if table_digest(source_connection, table) != table_digest(restored_connection, table):
                problems.append(f"{restored.name}.{table}: 内容摘要不一致")
    finally:
        source_connection.close()
        restored_connection.close()
    return problems


def backup(source: Path, destination: Path) -> None:
    """在线备份（不锁库、不中断写入）。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = _connect(source)
    target_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()


def restore(backup_path: Path, destination: Path) -> None:
    """从备份恢复到目标路径——**从备份**，不是从原库复制：后者证明不了备份可用。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    backup(source=backup_path, destination=destination)


def drill(source: Path, workdir: Path) -> dict:
    """对单个库跑完整演练。"""
    backup_path = workdir / "backup" / source.name
    restored_path = workdir / "restored" / source.name
    backup(source, backup_path)
    restore(backup_path, restored_path)
    problems = verify(source, restored_path)
    return {
        "database": str(source),
        "size_bytes": source.stat().st_size,
        "backup_path": str(backup_path),
        "restored_path": str(restored_path),
        "problems": problems,
    }


def configured_databases(settings: Settings) -> list[Path]:
    """需要备份的库：检查点库是资产，缓存库是可再生的派生数据（也演练，但优先级最低）。"""
    candidates = [settings.database_path, settings.agent_db_path, settings.model_cache_path]
    seen: list[Path] = []
    for path in candidates:
        resolved = Path(path)
        if resolved.exists() and resolved not in seen:
            seen.append(resolved)
    return seen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="备份 / 恢复演练")
    parser.add_argument("--workdir", default="output/backup-drill", help="演练的工作目录")
    args = parser.parse_args(argv)

    settings = load_settings()
    databases = configured_databases(settings)
    if not databases:
        print("没有找到任何数据库——先跑一次分析再演练。")
        return 0

    workdir = Path(args.workdir)
    failures = 0
    for database in databases:
        result = drill(database, workdir)
        status = "OK" if not result["problems"] else "FAILED"
        print(f"[{status}] {result['database']}  {result['size_bytes']} 字节")
        for problem in result["problems"]:
            print(f"    - {problem}")
        failures += len(result["problems"])

    print()
    if failures:
        print(f"演练失败：{failures} 个问题。恢复出来的库与源库不一致。")
        return 1
    print(f"演练通过：{len(databases)} 个库全部备份并可恢复，逐表行数与内容摘要一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
