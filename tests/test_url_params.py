from __future__ import annotations

import functools
from pathlib import Path
from urllib import parse

import streamlit.testing.v1.app_test as app_test_module
from streamlit.testing.v1 import AppTest

from tests.conftest import _recording_at

#: 与 tests/test_demo_mode.py 同因：Streamlit 1.64 把相对路径解析为「调用
#: AppTest.from_file 的那个文件」所在目录（streamlit/testing/v1/app_test.py:426-432），
#: 本文件又会先 chdir 到 tmp_path，因此必须给绝对路径，否则一律 FileNotFoundError: tests/app.py。
_APP_PATH = Path(__file__).parents[1] / "app.py"


class _KeepBlankValues:
    """给 AppTest 收尾解析用的 parse 补上 keep_blank_values。

    AppTest 以 parse.parse_qs(query_string) 的默认参数收尾（app_test.py:518），空值参数
    会被丢掉：修复前界面确实往 URL 写了 url=（原始查询串见任务报告），但 app.query_params
    里看不到 url，"url" not in app.query_params 于是在修复前也恒真、证明不了任何事。
    补上这个参数才能看见界面写进 URL 的东西——这正是本用例要断言的对象。
    作用域仅限 AppTest 自己的解析：该模块只用这一处 parse（app_test.py:518）。
    """

    parse_qs = staticmethod(functools.partial(parse.parse_qs, keep_blank_values=True))


def _run_app(tmp_path, monkeypatch) -> AppTest:
    """在隔离环境里跑一次首页，并保留 URL 中的空值参数。"""
    monkeypatch.chdir(tmp_path)
    # 两个密钥别名都要删：chdir 只挡得住项目根 .env，挡不住 shell 导出的环境变量；
    # 只要有一个在场，界面就进入「已配置密钥」态并对真实 DeepSeek 发一次连通性探测。
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    with monkeypatch.context() as patch:
        patch.setattr(app_test_module, "parse", _KeepBlankValues)
        return AppTest.from_file(_APP_PATH).run(timeout=30)


def test_demo_mode_writes_no_query_params(tmp_path, monkeypatch):
    """演示模式不写任何查询参数。

    演示模式的输入被锁死，刷新恢复没有意义；而分享链接会把填写者当次的状态一并带
    出去，在公开演示上既是噪音也是小范围隐私泄露。
    """
    recording = _recording_at(tmp_path / "demo-replay.json")
    monkeypatch.setenv("DEMO_MODE", "replay")
    monkeypatch.setenv("DEMO_REPLAY_PATH", str(recording))

    app = _run_app(tmp_path, monkeypatch)

    assert dict(app.query_params) == {}


def test_empty_values_are_not_written(tmp_path, monkeypatch):
    """普通模式照旧写参数，但空值不写。

    空参数只让链接变长，url= 这类空值尤其容易被误读为「这里有一个地址」。
    """
    monkeypatch.delenv("DEMO_MODE", raising=False)

    app = _run_app(tmp_path, monkeypatch)

    assert "url" not in app.query_params
    assert "upload" not in app.query_params
    # 反向门禁：普通模式必须照旧写入，否则「一条都不写」也能让上面两条恒真。
    assert app.query_params["mode"] == ["在线采集"]
    assert "limit" in app.query_params
