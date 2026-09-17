"""SQLite 连接配置：WAL 与 busy_timeout 是并发写不炸的前提。

web 与 worker 两个容器共享同一个库文件（docker-compose 挂载 `./data`），
因此这两项 PRAGMA 必须由统一入口设置，任何绕过它的连接都是回归。
"""

from app_review_insights.storage.sqlite import BUSY_TIMEOUT_MS, connect


def test_connect_enables_wal_busy_timeout_and_normal_sync(tmp_path):
    connection = connect(tmp_path / "runs.sqlite3")
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
        # synchronous=NORMAL(1)：WAL 下的常规取舍
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        connection.close()


def test_connect_exposes_columns_by_name(tmp_path):
    connection = connect(tmp_path / "runs.sqlite3")
    try:
        connection.execute("CREATE TABLE t (a TEXT)")
        connection.execute("INSERT INTO t VALUES ('x')")
        assert connection.execute("SELECT a FROM t").fetchone()["a"] == "x"
    finally:
        connection.close()


def test_pragma_is_persistent_across_reconnects(tmp_path):
    path = tmp_path / "runs.sqlite3"
    connect(path).close()
    again = connect(path)
    try:
        assert again.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        again.close()


def test_both_repositories_use_the_shared_connection(tmp_path):
    """两个仓库都必须走统一入口，否则并发写会退化成 database is locked。"""
    from app_review_insights.storage.agent_repository import AgentRepository
    from app_review_insights.storage.repository import RunRepository

    run_connection = RunRepository(tmp_path / "runs.sqlite3")._connect()
    agent_connection = AgentRepository(tmp_path / "agent.sqlite3")._connect()
    try:
        for connection in (run_connection, agent_connection):
            assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
    finally:
        run_connection.close()
        agent_connection.close()


def test_two_connections_can_write_alternately(tmp_path):
    """WAL 下两个连接交替提交不应抛 database is locked。"""
    path = tmp_path / "concurrent.sqlite3"
    first, second = connect(path), connect(path)
    try:
        first.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        first.commit()
        for index in range(20):
            first.execute("INSERT INTO t(v) VALUES (?)", (f"a{index}",))
            first.commit()
            second.execute("INSERT INTO t(v) VALUES (?)", (f"b{index}",))
            second.commit()
        assert second.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 40
    finally:
        first.close()
        second.close()
