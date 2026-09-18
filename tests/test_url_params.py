from __future__ import annotations

from streamlit.testing.v1 import AppTest

from tests.conftest import _recording_at


def _app_script() -> None:
    """跑一次真实首页，并记下它究竟往查询参数里写了什么。

    为什么不直接读 AppTest 暴露的 `app.query_params`：那是它收尾时用
    `parse.parse_qs(query_string)` 的**默认参数**解析出来的（app_test.py:518），空值参数
    在交给用例之前就被丢掉了——`url=` 明明写进了 URL，`"url" not in app.query_params`
    却恒真。这里读的是界面写入的那个对象本身（`st.query_params`），空值不丢。

    必须自备入口脚本：只有脚本运行内部才拿得到这个对象。`app.py` 只是「导入 main 并
    调用」的入口，仍由 tests/test_demo_mode.py、test_offline_ui.py、test_app_smoke.py
    以 `AppTest.from_file(app.py)` 覆盖。
    """
    import streamlit as st

    from app_review_insights.ui.main import main

    main()
    written = dict(st.query_params)
    # 活性对照：本通道必须看得见「值为空」的参数。下面两条用例的缺席断言
    # （url/upload 不在写入记录里）全都依赖这一点；通道哪天丢了空值，那些断言会
    # 静默变回恒真，所以把「看得见空值」本身也断言下来。
    st.query_params["blank-probe"] = ""
    st.session_state["blank-param-visible"] = dict(st.query_params).get("blank-probe") == ""
    del st.query_params["blank-probe"]
    st.session_state["written-query-params"] = written


def _run_app(tmp_path, monkeypatch) -> AppTest:
    """在隔离环境里跑一次首页，并带回查询参数的原始写入记录。"""
    monkeypatch.chdir(tmp_path)
    # 两个密钥别名都要删：chdir 只挡得住项目根 .env，挡不住 shell 导出的环境变量；
    # 只要有一个在场，界面就进入「已配置密钥」态并对真实 DeepSeek 发一次连通性探测。
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    return AppTest.from_function(_app_script).run(timeout=30)


def _written(app: AppTest) -> dict[str, str]:
    """界面本次真正写进 `st.query_params` 的键值（空值不丢）。"""
    return app.session_state["written-query-params"]


def test_demo_mode_writes_no_query_params(tmp_path, monkeypatch):
    """演示模式不写任何查询参数。

    演示模式的输入被锁死，刷新恢复没有意义；而分享链接会把填写者当次的状态一并带
    出去，在公开演示上既是噪音也是小范围隐私泄露。
    """
    recording = _recording_at(tmp_path / "demo-replay.json")
    monkeypatch.setenv("DEMO_MODE", "replay")
    monkeypatch.setenv("DEMO_REPLAY_PATH", str(recording))

    app = _run_app(tmp_path, monkeypatch)

    # 写入源头：一个键都没有。
    assert _written(app) == {}
    # URL 层：AppTest 收尾解析出来的查询参数同样是空的（浏览器分享出去的就是这一个）。
    assert dict(app.query_params) == {}


def test_empty_values_are_not_written(tmp_path, monkeypatch):
    """普通模式照旧写参数，但空值不写。

    空参数只让链接变长，`url=` 这类空值尤其容易被误读为「这里有一个地址」。
    """
    monkeypatch.delenv("DEMO_MODE", raising=False)

    app = _run_app(tmp_path, monkeypatch)
    written = _written(app)

    assert "url" not in written
    assert "upload" not in written
    # 反向门禁：普通模式必须照旧写入，否则「一条都不写」也能让上面两条恒真。
    assert written["mode"] == "在线采集"
    assert "limit" in written
    # 观察通道的活性对照（对照脚本见 _app_script）：看不见空值的通道会让上面两条
    # 缺席断言静默恒真，因此这里把它也断言下来。
    assert app.session_state["blank-param-visible"] is True
    # URL 层的弱确认：AppTest 的收尾解析会丢空值，判断以 written 为准。
    assert "url" not in app.query_params
    assert "upload" not in app.query_params
