import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]


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
