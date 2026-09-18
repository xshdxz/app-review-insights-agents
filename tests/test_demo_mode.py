from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from app_review_insights.storage.cache import DEMO_ANALYSIS_GOAL
from tests.conftest import _recording_at

#: Streamlit 1.64 把相对路径解析为「调用 AppTest.from_file 的那个文件」所在目录
#: （streamlit/testing/v1/app_test.py:430-438），不是 CWD；而本文件的辅助函数会先
#: chdir 到 tmp_path。必须给绝对路径，否则一律 FileNotFoundError: tests/app.py。
_APP_PATH = Path(__file__).parents[1] / "app.py"

#: Task 5 的真实录制产物（真实付费运行，13 条记录覆盖全部 5 类 Schema）。
#: 演示模式的核心承诺就是这个文件：按钮可点只是第一步，点下去必须真的跑完整条流水线。
_RECORDING_PATH = Path(__file__).parents[1] / "data" / "recordings" / "demo-replay.json"


def _replay_app(tmp_path, monkeypatch) -> AppTest:
    monkeypatch.chdir(tmp_path)
    recording = _recording_at(tmp_path / "demo-replay.json")
    monkeypatch.setenv("DEMO_MODE", "replay")
    monkeypatch.setenv("DEMO_REPLAY_PATH", str(recording))
    # 两个密钥别名都要删：chdir 只挡得住项目根 .env，挡不住 shell 导出的环境变量。
    # 只要有一个在场，界面就进入「已配置密钥」态并对真实 DeepSeek 发一次连通性探测
    # （provider 超时 60s）——那既花钱又违反「测试全部离线」。
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    # 30s：默认 3s 在本机负载高时假红过一次（本计划已记录 AppTest 超时抖动），
    # 与 tests/test_offline_ui.py 的既有取值一致。
    return AppTest.from_file(_APP_PATH).run(timeout=30)


def test_demo_mode_shows_a_labeled_banner(tmp_path, monkeypatch):
    app = _replay_app(tmp_path, monkeypatch)
    messages = [block.value for block in app.warning]
    assert any("演示模式" in message for message in messages)
    assert any("不调用外部 API" in message for message in messages)


def test_demo_mode_locks_the_input_source(tmp_path, monkeypatch):
    app = _replay_app(tmp_path, monkeypatch)
    source = next(box for box in app.selectbox if box.label == "数据来源")
    assert source.disabled is True
    assert source.value == "演示样例"


def test_demo_mode_enables_the_start_button(tmp_path, monkeypatch):
    app = _replay_app(tmp_path, monkeypatch)
    assert app.button(key="start-analysis").disabled is False


def test_demo_mode_locks_the_analysis_goal(tmp_path, monkeypatch):
    """分析目标同样进 prompt：演示模式必须锁成录制时那一句，否则回放全量未命中。"""
    from app_review_insights.llm.recording import load_recording

    app = _replay_app(tmp_path, monkeypatch)
    goal = next(box for box in app.text_area if box.label == "分析目标")

    assert goal.disabled is True
    assert goal.value == DEMO_ANALYSIS_GOAL
    # 前提断言：录制件里必须逐字含这句话，否则锁住的就不是录制时那一句
    document = load_recording(_RECORDING_PATH)
    assert any(DEMO_ANALYSIS_GOAL in entry.request.user for entry in document.entries)


def test_demo_mode_locks_the_review_limit_slider(tmp_path, monkeypatch):
    """「评论数量」对回放没有影响（演示分支不按它截断，这是对的），但仍要锁：

    一个访客能拖动却什么都不会发生的控件，比禁用的控件更糟——它教给访客的是
    「这个应用会忽略我的输入」；spec 要求的是演示模式下锁死输入。
    """
    app = _replay_app(tmp_path, monkeypatch)

    slider = next(item for item in app.slider if item.label == "评论数量")

    assert slider.disabled is True


def test_normal_mode_keeps_the_review_limit_editable(tmp_path, monkeypatch):
    """反向门禁：锁只属于演示模式，普通模式的滑块必须仍然可编辑。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)

    app = AppTest.from_file(_APP_PATH).run(timeout=30)

    slider = next(item for item in app.slider if item.label == "评论数量")
    assert slider.disabled is False


def test_demo_mode_runs_the_whole_pipeline_offline(tmp_path, monkeypatch):
    """演示模式的真正门禁：点「开始分析」要跑完整条流水线，而不是弹一个输入错误。

    「按钮 disabled is False」证明不了这一点——第一版实现里按钮是可点的，
    一点就抛「请选择与导入模式对应的 JSON 或 CSV 评论文件。」，运行根本不会创建。
    """
    from app_review_insights.models import RunStatus, Stage
    from app_review_insights.storage import RunRepository

    assert _RECORDING_PATH.is_file(), "本用例依赖 Task 5 提交的真实录制件"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEMO_MODE", "replay")
    monkeypatch.setenv("DEMO_REPLAY_PATH", str(_RECORDING_PATH))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)

    app = AppTest.from_file(_APP_PATH).run(timeout=30)
    app = app.button(key="start-analysis").click().run(timeout=180)

    assert not app.exception, f"演示回放不应抛异常，实际：{app.exception}"
    assert [block.value for block in app.error] == [], "演示模式点开始分析不应出现错误提示"
    run = RunRepository(tmp_path / "runs.sqlite3").get_run(app.session_state["run_id"])
    assert run.status == RunStatus.COMPLETED
    assert run.current_stage == Stage.COMPLETE


def test_live_mode_without_key_renders_instead_of_crashing(tmp_path, monkeypatch):
    """显式选了 live 却没密钥：页面要能渲染并说明原因，不能整页报错。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEMO_MODE", "live")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)

    app = AppTest.from_file(_APP_PATH).run(timeout=30)

    assert not app.exception, f"页面不应抛异常，实际：{app.exception}"
    messages = [block.value for block in app.warning] + [block.value for block in app.error]
    assert any("密钥" in message for message in messages)


def test_missing_recording_disables_start_even_with_a_key(tmp_path, monkeypatch):
    """显式 replay + 录制件缺失 + 已配置密钥：装配已降级，按钮必须禁用。

    此时 model_available 为真（密钥在场）但流水线是无模型的降级形态，点下去只会
    停在「请先配置 DEEPSEEK_API_KEY」，而真因是录制件缺失。
    """
    import app_review_insights.ui.main as ui_main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEMO_MODE", "replay")
    monkeypatch.setenv("DEMO_REPLAY_PATH", str(tmp_path / "missing-recording.json"))
    # 只有密钥在场才会走到这个组合：model_available 为真而装配失败。
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-offline-fixture")
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    # 密钥在场会让界面做一次真实连通性探测；本用例只需要 model_available 为真，
    # 探测必须打桩，否则会打真实 DeepSeek（既花钱又违反「测试全部离线」）。
    monkeypatch.setattr(ui_main, "_validate_model_key", lambda settings, session_state=None: None)

    app = AppTest.from_file(_APP_PATH).run(timeout=30)

    # 前提断言：确实进了降级分支，否则这条用例测的不是它要测的东西。
    assert any("模型装配未完成" in block.value for block in app.warning)
    assert app.button(key="start-analysis").disabled is True
