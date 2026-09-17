from pathlib import Path

from streamlit.testing.v1 import AppTest


def _app(tmp_path, monkeypatch) -> AppTest:
    app_path = Path(__file__).parents[1] / "app.py"
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))
    # 30s：15s 在负载高时会假红（曾导致一次全量误报）
    return AppTest.from_file(str(app_path)).run(timeout=30)


def test_offline_demo_reuses_result_views_and_exposes_downloads(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    demo_toggle = next(toggle for toggle in app.toggle if toggle.label == "查看历史缓存演示")
    app = demo_toggle.set_value(True).run(timeout=30)

    warning_text = " ".join(item.value for item in app.warning)
    assert "历史缓存演示" in warning_text
    assert "不是本次实时分析" in warning_text
    assert any("人工样例 JSON" in item.value for item in app.caption)
    assert any(tab.label.endswith("问题发现") for tab in app.tabs)
    assert any(tab.label.endswith("证据链") for tab in app.tabs)
    labels = {button.label for button in app.get("download_button")}
    assert labels >= {
        "下载清洗评论 JSON",
        "下载产品需求（PRD）JSON",
        "下载测试用例 CSV",
        "下载证据链 CSV",
    }


def test_sample_json_is_downloadable_without_model_key(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    labels = {button.label for button in app.get("download_button")}
    assert "下载样例评论 JSON" in labels
