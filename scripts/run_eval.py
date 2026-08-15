import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app_review_insights.cleaning import clean_reviews
from app_review_insights.config import load_settings
from app_review_insights.errors import RecoverableModelError
from app_review_insights.llm import DeepSeekProvider
from app_review_insights.models import Review
from app_review_insights.pipeline.analyze import analyze_batch


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
    normalized_expected_topics = {item.strip().casefold() for item in expected_topics}
    normalized_predicted_topics = {item.strip().casefold() for item in predicted_topics}
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
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    gold = json.loads(dataset_path.read_text(encoding="utf-8"))
    cases = gold["cases"]
    if not args.live:
        print(
            json.dumps(
                {
                    "dataset": str(dataset_path),
                    "cases": len(cases),
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


if __name__ == "__main__":
    main()
