"""SQLite 连接的统一入口。

web 与 worker 是两个独立容器，却共享同一个数据库文件（docker-compose 把
`./data` 挂到两边）。默认的 rollback journal 下并发写会立刻抛
`sqlite3.OperationalError: database is locked`，因此连接必须显式开启
WAL 与 `busy_timeout`。

**所有仓库都应经由 :func:`connect` 建连接，不要在别处直接调 `sqlite3.connect`。**
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

#: 写锁竞争时的等待上限（毫秒）；超过才抛 database is locked。
BUSY_TIMEOUT_MS = 5000


def connect(path: Path | str) -> sqlite3.Connection:
    """打开一个已应用生产必需 PRAGMA 的 SQLite 连接。

    - `journal_mode=WAL`：读写不再互相阻塞，web 与 worker 可并发访问。
    - `busy_timeout`：遇到写锁先等待，而不是立刻失败。
    - `synchronous=NORMAL`：WAL 下的常规取舍，兼顾吞吐与耐久性。
    - `row_factory=Row`：支持按列名取值。
    """
    connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection
