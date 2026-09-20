"""评测历史：跨版本比较的口径与门禁。

单次评测的数字只能说明"现在是多少"。要回答"这次改动是变好还是变差"，需要两份报告 +
"它们是不是同一套 prompt / 同一个数据集"的判定——否则差值里混着变量，读出来的结论是错的。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from scripts.compare_eval import compare_reports, regressions, render
from scripts.compare_eval import main as compare_main
from scripts.run_eval import history_path


def _report(**summary: object) -> dict:
    return {
        "prompt_version": "batch-v1",
        "prompt_fingerprint": "fp-a",
        "dataset": "evals/gold-reviews.json",
        "summary": summary,
    }


def test_compare_reports_computes_signed_deltas():
    rows = compare_reports(
        _report(topic_recall=0.4, hallucination_rate=0.2),
        _report(topic_recall=0.6, hallucination_rate=0.1),
    )
    by_metric = {row["metric"]: row for row in rows}

    assert by_metric["topic_recall"]["delta"] == 0.2
    assert by_metric["hallucination_rate"]["delta"] == -0.1


def test_compare_reports_tolerates_metrics_absent_from_one_side():
    """一份报告没跑稳定性时，差值是"未知"而不是 0——0 会被读成"没有变化"。"""
    rows = compare_reports(_report(topic_recall=0.4, stability=None), _report(topic_recall=0.4))
    stability = next(row for row in rows if row["metric"] == "stability")

    assert stability["delta"] is None


@pytest.mark.parametrize(
    ("metric", "before", "after"),
    [
        ("topic_recall", 0.6, 0.4),
        ("reference_precision", 0.8, 0.5),
        ("hallucination_rate", 0.1, 0.3),
    ],
)
def test_regressions_flags_worse_direction_per_metric(metric, before, after):
    rows = compare_reports(_report(**{metric: before}), _report(**{metric: after}))

    breaches = regressions(rows, threshold=0.1)

    assert any(metric in breach for breach in breaches), f"{metric} 的方向判定错了"


def test_regressions_ignores_improvements_and_small_moves():
    rows = compare_reports(
        _report(topic_recall=0.4, hallucination_rate=0.3),
        _report(topic_recall=0.6, hallucination_rate=0.1),
    )

    assert regressions(rows, threshold=0.1) == []


def test_render_warns_when_prompt_fingerprints_differ():
    baseline = _report(topic_recall=0.4)
    current = _report(topic_recall=0.5) | {"prompt_fingerprint": "fp-b"}

    text = render(baseline, current, compare_reports(baseline, current))

    assert "prompt 指纹不同" in text


def test_history_path_carries_date_and_prompt_fingerprint():
    path = history_path(
        {"prompt_fingerprint": "9e70a08b5eef"}, Path("evals/history"), date(2026, 9, 20)
    )

    assert path.name == "2026-09-20-9e70a08b5eef.json"


def test_compare_main_exits_nonzero_only_when_asked(tmp_path):
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    baseline.write_text(json.dumps(_report(topic_recall=0.9)), encoding="utf-8")
    current.write_text(json.dumps(_report(topic_recall=0.1)), encoding="utf-8")

    assert compare_main([str(baseline), str(current)]) == 0
    assert compare_main([str(baseline), str(current), "--fail-on-regression", "0.1"]) == 1
    # 阈值放宽到覆盖这次回退时不该拦
    assert compare_main([str(baseline), str(current), "--fail-on-regression", "0.9"]) == 0
