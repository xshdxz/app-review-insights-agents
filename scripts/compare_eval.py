"""比较两次评测报告，输出指标差值。

单次评测的数字只能说明"现在是多少"，说明不了"改完之后是变好了还是变差了"。报告里带了
prompt 版本与文本指纹，所以"这两份是不是同一套 prompt 跑的"可判定——不同 prompt 的结果
本来就不该被当成同一条趋势线上的点，脚本会明确提示。

用法：

    python scripts/compare_eval.py <基线.json> <本次.json>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

#: 参与比较的指标。顺序即表格顺序。
METRICS = (
    "reference_recall",
    "reference_precision",
    "topic_recall",
    "topic_key_coverage",
    "hallucination_rate",
    "stability",
)

#: 越大越好的指标。没列进来的按"越小越好"处理（目前只有幻觉率）。
HIGHER_IS_BETTER = frozenset(
    {
        "topic_recall",
        "reference_precision",
        "reference_recall",
        "topic_key_coverage",
        "stability",
    }
)


def compare_reports(baseline: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    """逐指标算差值。任一侧缺该指标（例如那份报告没跑稳定性）时差值为 None。"""
    before_summary = baseline.get("summary") or {}
    after_summary = current.get("summary") or {}
    rows: list[dict[str, Any]] = []
    for metric in METRICS:
        before = before_summary.get(metric)
        after = after_summary.get(metric)
        delta = None if before is None or after is None else round(float(after) - float(before), 3)
        rows.append({"metric": metric, "before": before, "after": after, "delta": delta})
    return rows


def regressions(rows: list[dict[str, Any]], threshold: float) -> list[str]:
    """列出回退超过阈值的指标。阈值是"允许的退步幅度"，不是"要求达到的值"。"""
    breaches: list[str] = []
    for row in rows:
        delta = row["delta"]
        if delta is None:
            continue
        worse = -delta if row["metric"] in HIGHER_IS_BETTER else delta
        if worse > threshold:
            breaches.append(f"{row['metric']} 回退 {worse:.3f}（{row['before']} → {row['after']}）")
    return breaches


def _header(report: dict[str, Any], label: str) -> str:
    version = report.get("prompt_version", "未记录")
    fingerprint = report.get("prompt_fingerprint", "?")
    dataset = report.get("dataset", "?")
    return f"{label}：{version} / {fingerprint} / {dataset}"


def render(baseline: dict[str, Any], current: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [_header(baseline, "基线"), _header(current, "本次")]
    if baseline.get("prompt_fingerprint") != current.get("prompt_fingerprint"):
        lines.append(
            "注意：两次的 prompt 指纹不同，差值里混着 prompt 变化的影响，不是同一条趋势线。"
        )
    if baseline.get("dataset") != current.get("dataset"):
        lines.append("注意：两次用的数据集不同，差值不可比。")
    lines.append("")
    lines.append(f"{'指标':<22}{'基线':>10}{'本次':>10}{'差值':>10}")
    for row in rows:
        before = "—" if row["before"] is None else f"{row['before']}"
        after = "—" if row["after"] is None else f"{row['after']}"
        delta = "—" if row["delta"] is None else f"{row['delta']:+.3f}"
        lines.append(f"{row['metric']:<22}{before:>10}{after:>10}{delta:>10}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="比较两次评测报告")
    parser.add_argument("baseline", help="基线报告 JSON")
    parser.add_argument("current", help="本次报告 JSON")
    parser.add_argument(
        "--fail-on-regression",
        type=float,
        default=None,
        help="任一指标回退超过该幅度即以非零码退出（用于手动触发的评测门禁）",
    )
    args = parser.parse_args(argv)

    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    current = json.loads(Path(args.current).read_text(encoding="utf-8"))
    rows = compare_reports(baseline, current)
    print(render(baseline, current, rows))

    if args.fail_on_regression is not None:
        breaches = regressions(rows, args.fail_on_regression)
        if breaches:
            print("\n指标回退：" + "；".join(breaches), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
