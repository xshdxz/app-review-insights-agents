"""评测页面冒烟测试。"""

from pathlib import Path

from streamlit.testing.v1 import AppTest

PAGE = Path(__file__).resolve().parents[1] / "pages" / "3_评测中心.py"


def test_eval_page_loads(tmp_path, monkeypatch):
    """评测页面能正常加载（无结果时显示 info 提示，无崩溃）。"""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "agent.sqlite3"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    app = AppTest.from_file(str(PAGE), default_timeout=15)
    app.run()
    assert not app.exception
