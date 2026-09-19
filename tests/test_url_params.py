from __future__ import annotations

from streamlit.testing.v1 import AppTest

from app_review_insights.storage.cache import DEMO_ANALYSIS_GOAL
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

    # 跑了多少次：判定「清参数是否引发重跑循环」用的就是它。
    st.session_state["script-runs"] = st.session_state.get("script-runs", 0) + 1
    # 进来的参数：模拟「访客带着别人的链接进来」或「上一轮留在地址栏里的输入」。
    st.session_state["incoming-query-params"] = dict(st.query_params)

    main()

    # 快照与活性对照取自**同一次读取**：对照断言「这次读到的字典里看得见一个值为空的
    # 参数」，而快照就是这个字典本身。两者同源，不可能各自漂移——把读取换成会丢空值的
    # 写法，对照立刻为假，而不是让缺席断言安静地恒真。
    st.query_params["blank-probe"] = ""
    written = dict(st.query_params)
    st.session_state["blank-param-visible"] = written.get("blank-probe") == ""
    written.pop("blank-probe")
    del st.query_params["blank-probe"]
    st.session_state["written-query-params"] = written


def _isolate(tmp_path, monkeypatch) -> None:
    """把用例与开发机的 .env / 密钥彻底隔离。

    chdir 挡得住项目根的 .env（Settings 的 env_file 是相对路径），挡不住 shell 导出的
    环境变量；两个密钥别名都要删。少了任何一步，用例的行为就会随"这台机器有没有配密钥"
    而变——有密钥时是普通模式，没有时应用会进演示模式（输入被锁死、一个参数都不写），
    同一条用例于是**本地绿、CI 红**。

    这个函数存在，是因为这套隔离曾经被抄漏过一次：见
    test_switching_to_online_collection_drops_the_upload_param。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)


def _run_app(tmp_path, monkeypatch, *, query_params: dict | None = None) -> AppTest:
    """在隔离环境里跑一次首页，并带回查询参数的原始写入记录。

    query_params 用来模拟「访客带着别人的链接进来」或「上一轮留在地址栏里的输入」。
    """
    _isolate(tmp_path, monkeypatch)
    app = AppTest.from_function(_app_script)
    if query_params:
        app.query_params = query_params
    return app.run(timeout=30)


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


def test_demo_mode_clears_the_params_it_arrived_with(tmp_path, monkeypatch):
    """带着别人的链接进来也要清干净，而不是只清自己新写的那部分。

    演示模式只读不清的话，访客从地址栏复制出去的仍然是别人链接里那串参数——本任务
    就是来堵分享泄漏的，不能只堵「新写入」而放走「残留的旧值」。
    """
    recording = _recording_at(tmp_path / "demo-replay.json")
    monkeypatch.setenv("DEMO_MODE", "replay")
    monkeypatch.setenv("DEMO_REPLAY_PATH", str(recording))
    foreign = "https://apps.apple.com/us/app/example/id123456789"

    app = _run_app(
        tmp_path,
        monkeypatch,
        query_params={
            "mode": ["在线采集"],
            "url": [foreign],
            "goal": ["别人填的分析目标"],
            "limit": ["777"],
        },
    )

    # 前提断言：参数确实带进来了，否则这条用例测的不是它要测的东西。
    assert app.session_state["incoming-query-params"] == {
        "mode": "在线采集",
        "url": foreign,
        "goal": "别人填的分析目标",
        "limit": "777",
    }
    assert _written(app) == {}
    assert dict(app.query_params) == {}
    # 清参数不得引发重跑循环：本机实测 st.query_params.pop/clear 都不会，这里钉住。
    assert app.session_state["script-runs"] == 1


def test_empty_values_are_not_written(tmp_path, monkeypatch):
    """普通模式照旧写参数，但空值不写（键要被删掉，不能只是不写）。

    空参数只让链接变长，`url=` 这类空值尤其容易被误读为「这里有一个地址」；而旧值
    留在键里会以另一种方式泄漏——见下面那条用例。
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


def test_switching_to_online_collection_drops_the_upload_param(tmp_path, monkeypatch):
    """导入模式上传过文件、再切到在线采集：upload 键必须从地址栏里消失。

    `restored-upload` 由上传动作写进会话且从不清理，切模式后它仍在场；写参数时只看它、
    不看当前来源模式的话，文件名会被继续写回 URL，刷新还会被重新种回会话——与 Task 7
    修掉的分享链接泄漏是同一族问题，只是换了条触发路径。
    """
    monkeypatch.delenv("DEMO_MODE", raising=False)
    _isolate(tmp_path, monkeypatch)
    saved = tmp_path / "reviews.json"
    saved.write_text("[]", encoding="utf-8")

    app = AppTest.from_function(_app_script)
    app.session_state["source-mode"] = "JSON 导入"
    app.session_state["restored-upload"] = str(saved)
    app = app.run(timeout=30)

    # 前提断言：导入模式下确实会把文件名写进 URL，否则这条用例测的不是它要测的东西。
    assert _written(app)["upload"] == "reviews.json"

    # 切到在线采集：该键要**删掉**，不能只是不更新（旧值留在地址栏同样会外传）。
    app.session_state["source-mode"] = "在线采集"
    app = app.run(timeout=30)

    assert "upload" not in _written(app)
    # 反向门禁：确实切到了在线采集，且这条路径照旧写它自己的参数。
    assert _written(app)["mode"] == "在线采集"
    assert "limit" in _written(app)


def test_cleared_input_does_not_come_back_after_reload(tmp_path, monkeypatch):
    """清空字段后旧值不得复活，分享出去的地址也不该再带着它。

    条件写入的键如果不删除，旧的非空值会一直留在 URL 里：刷新会把它恢复回表单
    （用户已经清掉的内容自己回来了），复制地址栏分享时也仍然带着他已经删掉的内容。
    """
    monkeypatch.delenv("DEMO_MODE", raising=False)
    old_url = "https://apps.apple.com/us/app/example/id123456789"
    app = _run_app(
        tmp_path,
        monkeypatch,
        query_params={"mode": ["在线采集"], "url": [old_url], "goal": ["旧的分析目标"]},
    )

    # 前提断言：第一轮确实带着旧值，界面也把它写回了 URL（否则测的不是它要测的）。
    assert app.session_state["input-app-url"] == old_url
    assert _written(app)["url"] == old_url
    assert _written(app)["goal"] == "旧的分析目标"

    # 用户把两个字段都清空。
    app = app.text_input(key="input-app-url").set_value("").run(timeout=30)
    app = app.text_area(key="input-goal").set_value("").run(timeout=30)

    written = _written(app)
    assert "url" not in written
    assert "goal" not in written

    # 刷新：新会话只带清空后的地址栏参数回来。旧值不得复活——地址回到空，目标回到
    # 表单默认值，而不是用户删掉的那一句。
    reloaded = _run_app(tmp_path, monkeypatch, query_params=dict(app.query_params))
    assert reloaded.session_state["input-app-url"] == ""
    assert reloaded.session_state["input-goal"] == DEMO_ANALYSIS_GOAL
