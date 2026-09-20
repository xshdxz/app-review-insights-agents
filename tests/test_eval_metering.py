"""评测计量：`--live` 必须记账，花费上限要真的拦得住。

背景：`run_eval.py --live` 此前不记录任何用量——"这次评测花了多少钱"无从回答，
而"扩评测集 / 跑稳定性要花多少"恰恰是最该先知道的事。
"""

from __future__ import annotations

import json
import sys

import pytest

import scripts.run_eval as run_eval
from app_review_insights.config import Settings
from app_review_insights.llm.usage import build_usage
from app_review_insights.storage import RunRepository

#: 单次假调用 = 1000 输入 + 1000 输出 token，按 deepseek-chat 价格约 0.00137 美元
_CALL_COST_APPROX = 0.00137

_BATCH_PAYLOAD = {
    "findings": [
        {
            "title": "续费价格不透明",
            "problem_statement": "用户直到最后一屏才看到续费价格。",
            "topic_label": "订阅透明度",
            "topic_key": "subscription_transparency",
            "supporting_review_ids": ["r1"],
            "conflicting_review_ids": [],
            "reasoning_summary": "原文直接说明价格未提前披露。",
            "limitations": [],
        }
    ],
    "review_summaries": [{"review_id": "r1", "summary_zh": "续费价格直到最后才显示。"}],
    "batch_limitations": [],
}


class _FakeProvider:
    """替掉真实 provider：不联网，但**照常上报用量**，这样计量链路才真的被走到。"""

    def __init__(self, usage_recorder):
        self._usage_recorder = usage_recorder

    def generate(self, system_prompt, user_prompt, schema):
        if self._usage_recorder is not None:
            self._usage_recorder(
                build_usage(
                    model="deepseek-chat",
                    prompt_tokens=1000,
                    completion_tokens=1000,
                    latency_ms=1.0,
                )
            )
        return schema.model_validate(_BATCH_PAYLOAD)


def _case(case_id: str) -> dict:
    return {
        "case_id": case_id,
        "analysis_goal": "分析订阅与续费透明度",
        "expected_topics": ["subscription_transparency"],
        "expected_review_ids": ["r1"],
        "reviews": [
            {
                "review_id": "r1",
                "content": "The renewal price was hidden until the last screen.",
                "rating": 2,
                "language": "en",
                "published_at": "2026-06-01T10:00:00Z",
            }
        ],
    }


@pytest.fixture
def eval_env(tmp_path, monkeypatch):
    dataset = tmp_path / "gold.json"
    dataset.write_text(
        json.dumps({"cases": [_case("case-1"), _case("case-2")]}, ensure_ascii=False),
        encoding="utf-8",
    )
    settings = Settings(
        DEEPSEEK_API_KEY="sk-eval-fixture",
        DATABASE_PATH=str(tmp_path / "runs.sqlite3"),
    )
    monkeypatch.setattr(run_eval, "load_settings", lambda: settings)
    monkeypatch.setattr(
        run_eval.DeepSeekProvider,
        "from_settings",
        staticmethod(
            lambda settings, usage_recorder=None, budget_check=None: _FakeProvider(usage_recorder)
        ),
    )
    return dataset, settings


def _run_main(monkeypatch, dataset, *extra: str) -> dict:
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_eval.py", "--live", "--dataset", str(dataset), *extra],
    )
    run_eval.main()


def test_live_eval_reports_actual_cost_and_meters_usage(eval_env, monkeypatch, capsys):
    dataset, settings = eval_env

    _run_main(monkeypatch, dataset, "--max-cost-usd", "0")
    report = json.loads(capsys.readouterr().out)

    usage = report["usage"]
    assert usage["cases_completed"] == 2
    assert usage["cases_total"] == 2
    assert usage["budget_exceeded"] is False
    assert usage["estimated_cost_usd"] == pytest.approx(2 * _CALL_COST_APPROX, rel=0.05)

    # 用量必须落进与流水线同一张表，并且标着 stage="eval" —— 否则日预算看不到评测花的钱
    repository = RunRepository(settings.database_path)
    assert repository.model_usage_summary()["calls"] == 2
    assert repository.model_usage_by_stage()["eval"]["calls"] == 2


def test_cost_cap_stops_the_run_instead_of_silently_overspending(eval_env, monkeypatch, capsys):
    dataset, settings = eval_env

    _run_main(monkeypatch, dataset, "--max-cost-usd", "0.0005")
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert report["usage"]["budget_exceeded"] is True, "超过上限必须如实标注，不能装作跑完了"
    assert report["usage"]["cases_completed"] == 1, "上限在第一个用例之后就该生效"
    assert "已达花费上限" in captured.err
    # 停止之后不得再有新的模型调用
    assert RunRepository(settings.database_path).model_usage_summary()["calls"] == 1
