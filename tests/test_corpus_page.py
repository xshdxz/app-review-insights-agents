"""语料库管理页面冒烟测试。"""

from pathlib import Path

from streamlit.testing.v1 import AppTest

PAGE = Path(__file__).resolve().parents[1] / "pages" / "4_语料库管理.py"


def test_corpus_page_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "agent.sqlite3"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    app = AppTest.from_file(
        str(PAGE), default_timeout=30
    )  # 30s：15s 在负载高时会假红（曾导致一次全量误报）
    app.run()
    assert not app.exception
