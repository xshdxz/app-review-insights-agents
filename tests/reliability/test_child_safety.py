"""子进程护栏：可靠性用例绝不允许打真实模型。

2026-09-19 事故：_child.py 里 `_prepare_environment()` 定义后**没有被调用**，
子进程于是读到 .env 的真实密钥，按 DEMO_MODE=auto 走了实时模型路径——
3 次运行、39 次真实调用、$0.0594。

教训与修法：护栏不能靠"记得调用"。现在环境钉死之后有一道**装配后的自检**，
并且这道自检本身有测试（本文件）——用 ARI_PIN_BYPASS 制造"钉死失效"的场景，
断言子进程**拒绝运行**而不是继续跑。
"""

from __future__ import annotations

import child
import pytest

#: 钉死失效时自检可能给出的拒绝理由。**不能只断言其中一个**：没有密钥时
#: demo_replay_active 本来就是真（auto + 无密钥），于是拦下它的是数据库路径那一项，
#: 而不是"不是回放模式"那一项——只认一种理由会让这条用例依赖开发机的 .env，
#: 在 CI（没有 .env）上变红。
PINNING_FAILURE_REASONS = frozenset(
    {"not_replay", "database_not_taken_over", "not_replay_pipeline"}
)

_KEY_ENVIRONMENTS = [
    pytest.param(
        {"DEEPSEEK_API_KEY": "sk-fake-key-for-pinning-test", "MODEL_API_KEY": ""},
        id="with-key",
    ),
    pytest.param({"DEEPSEEK_API_KEY": "", "MODEL_API_KEY": ""}, id="without-key"),
]


@pytest.mark.reliability
@pytest.mark.parametrize("key_env", _KEY_ENVIRONMENTS)
def test_child_refuses_to_run_when_replay_pinning_is_bypassed(tmp_path, key_env):
    """钉死失效时子进程必须拒绝运行——放行会造成真实付费调用。

    两种密钥环境都要覆盖：有密钥时自检失败于"不是回放模式"，没有密钥时失败于
    "数据库没被接管"。要守的性质是**拒绝运行**，不是某个具体理由。
    """
    result = child.spawn(
        tmp_path / "runs.sqlite3",
        extra_env={"ARI_PIN_BYPASS": "1", **key_env},
    )

    assert result.guarded, (
        f"护栏未生效：returncode={result.returncode}\n"
        f"stdout={result.stdout[-1500:]}\nstderr={result.stderr[-1500:]}"
    )
    assert result.summary is not None
    assert result.summary["reason"] in PINNING_FAILURE_REASONS, (
        f"拒绝理由不在预期集合内：{result.summary['reason']}"
    )
    assert result.summary["error"].strip(), "护栏必须给出给人看的说明"


@pytest.mark.reliability
def test_child_pins_replay_even_when_a_real_key_is_present(tmp_path):
    """即使调用方环境里带着真实密钥，子进程也必须把它压下去并走回放。"""
    db_path = tmp_path / "pinned.sqlite3"
    result = child.spawn(
        db_path,
        extra_env={"DEEPSEEK_API_KEY": "sk-must-be-ignored", "DEMO_MODE": "auto"},
    )

    summary = result.require_ok()
    assert summary["mode"] == "recorded_live_run"
    assert summary["is_live"] is False
    assert db_path.exists(), "子进程必须把结果写进 ARI_DB_PATH 指定的库"
