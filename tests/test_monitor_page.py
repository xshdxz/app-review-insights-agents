from pathlib import Path

from streamlit.testing.v1 import AppTest

PAGE = Path(__file__).resolve().parents[1] / "pages" / "2_监控任务.py"


def test_monitor_page_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "agent.sqlite3"))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))
    app = AppTest.from_file(
        str(PAGE), default_timeout=30
    )  # 30s：15s 在负载高时会假红（曾导致一次全量误报）
    app.run()
    assert not app.exception
