import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]

# 被验证的 `run_eval.ps1` 是 Windows 专用入口：它按 `.venv\\Scripts\\python.exe` 定位解释器
# （POSIX 上是 `.venv/bin/python`），并依赖 Windows PowerShell。因此仅在「Windows 且项目内确实
# 存在 .venv」时才具备运行条件——CI 把依赖装进 runner 自身的 Python，没有 .venv，故一并跳过。
# 跨平台入口请直接用 `python scripts/run_eval.py`。
_VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not _VENV_PYTHON.exists(),
    reason="run_eval.ps1 依赖 Windows 下的 .venv\\Scripts\\python.exe；当前环境不满足",
)


def test_root_eval_entrypoint_runs_offline_without_live_model():
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "run_eval.ps1"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["live_model_called"] is False
    assert payload["cases"] >= 1
    assert "--live" in payload["next"]


def test_root_eval_entrypoint_reports_missing_virtual_environment_in_chinese(tmp_path):
    isolated_script = tmp_path / "run_eval.ps1"
    shutil.copyfile(ROOT / "run_eval.ps1", isolated_script)

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(isolated_script),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode != 0
    assert "未找到项目虚拟环境" in result.stderr
