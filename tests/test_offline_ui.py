from pathlib import Path

from streamlit.testing.v1 import AppTest


def _app(tmp_path, monkeypatch) -> AppTest:
    app_path = Path(__file__).parents[1] / "app.py"
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))
    # 隔离回放录制件：默认路径上一旦有录制，无密钥时每次脚本运行都会去解析它（CI 即如此）。
    # 本文件的两条用例都不走回放路径，指向一个不存在的路径即可，结果不再取决于磁盘状态。
    monkeypatch.setenv("DEMO_REPLAY_PATH", str(tmp_path / "no-recording.json"))
    # 隔离项目根 .env（CWD 相对）：只删环境变量挡不住 .env 里的 DEEPSEEK_API_KEY，
    # 那会让界面进入「已配置密钥」态并真的发一次连通性探测（provider 超时 60s >
    # 本文件的 30s AppTest 超时，网络一慢就假红）。chdir 之后是真实的无密钥状态，
    # 与用例名（..._without_model_key）相符，且界面用到的是 __file__ 推出的绝对路径。
    monkeypatch.chdir(tmp_path)
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
