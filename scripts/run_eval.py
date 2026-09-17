import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from app_review_insights.cleaning import clean_reviews
from app_review_insights.config import load_settings
from app_review_insights.errors import RecoverableModelError
from app_review_insights.llm import DeepSeekProvider
from app_review_insights.llm.schemas import normalize_topic_key
from app_review_insights.models import Review
from app_review_insights.pipeline.analyze import analyze_batch


def validate_dataset(gold: dict[str, Any]) -> list[str]:
    """校验评测集完整性，返回问题列表（空列表表示干净）。

    评测集本身坏掉是最隐蔽的失败：跑出来的数字毫无意义，却没人发现。
    所以这些检查要在 CI 里跑，而不是等看指标时才起疑。
    """
    problems: list[str] = []
    cases = gold.get("cases")
    if not isinstance(cases, list) or not cases:
        return ["评测集缺少顶层 cases 列表或列表为空"]

    seen_cases: set[str] = set()
    for index, case in enumerate(cases):
        label = case.get("case_id") or f"第 {index + 1} 个用例"
        case_id = case.get("case_id")
        if not case_id:
            problems.append(f"{label}：缺少 case_id")
        elif case_id in seen_cases:
            problems.append(f"{label}：case_id 重复")
        else:
            seen_cases.add(case_id)

        goal = case.get("analysis_goal")
        if not goal or len(str(goal)) < 3:
            problems.append(f"{label}：analysis_goal 缺失或过短")

        reviews = case.get("reviews")
        if not isinstance(reviews, list) or not reviews:
            problems.append(f"{label}：没有评论")
            continue

        review_ids: set[str] = set()
        for review in reviews:
            review_id = str(review.get("review_id", ""))
            if not review_id:
                problems.append(f"{label}：存在缺少 review_id 的评论")
                continue
            if review_id in review_ids:
                problems.append(f"{label}：评论 ID 重复 {review_id}")
            review_ids.add(review_id)
            rating = review.get("rating")
            if not isinstance(rating, int) or not 1 <= rating <= 5:
                problems.append(f"{label}：{review_id} 的 rating 必须是 1-5 的整数")
            if not str(review.get("content", "")).strip():
                problems.append(f"{label}：{review_id} 的 content 为空")

        for expected_id in case.get("expected_review_ids", []):
            if str(expected_id) not in review_ids:
                problems.append(f"{label}：期望评论 {expected_id} 不在本用例的评论里")

        for topic in case.get("expected_topics", []):
            if not normalize_topic_key(str(topic)):
                problems.append(
                    f"{label}：主题键 {topic!r} 不是可比的 ascii 主题键（应形如 subscription_transparency）"
                )

    return problems


def _normalize_topic(value: str) -> str:
    """比对前统一形态：大小写、分隔符、首尾空白都不应影响判定。

    这让 "Subscription Transparency" 与 "subscription_transparency" 视为同一个主题。
    """
    return re.sub(r"[^0-9a-zA-Z]+", "_", value).strip("_").casefold()


def _set_score(expected: set[str], predicted: set[str], *, precision: bool) -> float:
    if not expected and not predicted:
        return 1.0
    denominator = len(predicted) if precision else len(expected)
    if denominator == 0:
        return 0.0
    return len(expected & predicted) / denominator


def score_predictions(
    expected_topics: set[str],
    expected_review_ids: set[str],
    predicted_topics: set[str],
    predicted_review_ids: set[str],
) -> dict[str, float]:
    normalized_expected_topics = {_normalize_topic(item) for item in expected_topics} - {""}
    normalized_predicted_topics = {_normalize_topic(item) for item in predicted_topics} - {""}
    return {
        "topic_recall": round(
            _set_score(
                normalized_expected_topics,
                normalized_predicted_topics,
                precision=False,
            ),
            3,
        ),
        "reference_precision": round(
            _set_score(
                expected_review_ids,
                predicted_review_ids,
                precision=True,
            ),
            3,
        ),
    }


def _case_reviews(case: dict[str, Any]) -> list[Review]:
    return [
        Review(
            review_id=str(item["review_id"]),
            app_id=str(case["case_id"]),
            content_original=str(item["content"]),
            rating=int(item["rating"]),
            published_at=datetime.fromisoformat(str(item["published_at"]).replace("Z", "+00:00")),
            app_version=item.get("app_version"),
            language=item.get("language"),
            source="prompt-eval",
        )
        for item in case["reviews"]
    ]


def evaluate_case(provider, case: dict[str, Any]) -> dict[str, Any]:
    reviews = clean_reviews(_case_reviews(case)).reviews
    prediction = analyze_batch(provider, reviews, str(case["analysis_goal"]))
    predicted_topics = {finding.topic_label for finding in prediction.findings}
    predicted_review_ids = {
        review_id for finding in prediction.findings for review_id in finding.supporting_review_ids
    }
    score = score_predictions(
        set(case.get("expected_topics", [])),
        set(case.get("expected_review_ids", [])),
        predicted_topics,
        predicted_review_ids,
    )
    return {
        "case_id": case["case_id"],
        **score,
        "structured_output_success": True,
        "predicted_topics": sorted(predicted_topics),
        "predicted_review_ids": sorted(predicted_review_ids),
        "batch_limitations": prediction.batch_limitations,
        "error": None,
    }


def _aggregate(results: list[dict[str, Any]]) -> dict[str, float | int]:
    total = max(1, len(results))
    return {
        "cases": len(results),
        "topic_recall": round(
            sum(float(item.get("topic_recall", 0)) for item in results) / total,
            3,
        ),
        "reference_precision": round(
            sum(float(item.get("reference_precision", 0)) for item in results) / total,
            3,
        ),
        "structured_output_success": round(
            sum(bool(item.get("structured_output_success")) for item in results) / total,
            3,
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 App Review Insights Prompt 小型评测")
    parser.add_argument("--dataset", default="evals/gold-reviews.json")
    parser.add_argument("--live", action="store_true", help="调用当前 DeepSeek 配置")
    parser.add_argument("--output", help="可选的 JSON 结果输出路径")
    parser.add_argument(
        "--fail-under-topic-recall",
        type=float,
        default=None,
        help="主题召回低于该值时以非零码退出（用于 CI 回归门禁）",
    )
    parser.add_argument(
        "--fail-under-reference-precision",
        type=float,
        default=None,
        help="引用精确率低于该值时以非零码退出（用于 CI 回归门禁）",
    )
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    gold = json.loads(dataset_path.read_text(encoding="utf-8"))

    # 数据集完整性：坏掉的评测集跑出来的数字毫无意义，必须先拦下
    problems = validate_dataset(gold)
    if problems:
        for problem in problems:
            print(f"评测集问题：{problem}", file=sys.stderr)
        raise SystemExit(f"评测集校验失败（{len(problems)} 个问题）")

    cases = gold["cases"]
    if not args.live:
        print(
            json.dumps(
                {
                    "dataset": str(dataset_path),
                    "cases": len(cases),
                    "reviews": sum(len(case["reviews"]) for case in cases),
                    "dataset_valid": True,
                    "live_model_called": False,
                    "next": "使用 --live 调用 DeepSeek 并计算指标",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    settings = load_settings()
    if not settings.deepseek_api_key:
        raise SystemExit("未配置 DEEPSEEK_API_KEY，无法运行实时 Prompt 评测。")
    provider = DeepSeekProvider.from_settings(settings)
    results: list[dict[str, Any]] = []
    for case in cases:
        try:
            results.append(evaluate_case(provider, case))
        except RecoverableModelError as exc:
            results.append(
                {
                    "case_id": case["case_id"],
                    "topic_recall": 0.0,
                    "reference_precision": 0.0,
                    "structured_output_success": False,
                    "predicted_topics": [],
                    "predicted_review_ids": [],
                    "batch_limitations": [],
                    "error": str(exc),
                }
            )

    report = {
        "provider": settings.model_provider,
        "model": settings.model_name,
        "dataset": str(dataset_path),
        "summary": _aggregate(results),
        "results": results,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")

    # 回归门禁：指标跌破阈值即非零退出，让 CI 拦下来
    summary = report["summary"]
    breaches: list[str] = []
    thresholds = (
        ("topic_recall", args.fail_under_topic_recall),
        ("reference_precision", args.fail_under_reference_precision),
    )
    for metric, floor in thresholds:
        if floor is not None and float(summary[metric]) < floor:
            breaches.append(f"{metric}={summary[metric]} < {floor}")
    if breaches:
        raise SystemExit("指标低于阈值：" + "；".join(breaches))


if __name__ == "__main__":
    main()
