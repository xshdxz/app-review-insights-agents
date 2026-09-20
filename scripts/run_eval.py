import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app_review_insights.cleaning import clean_reviews
from app_review_insights.config import load_settings
from app_review_insights.errors import RecoverableModelError
from app_review_insights.llm import DeepSeekProvider
from app_review_insights.llm.prompts import prompt_fingerprint, prompt_version
from app_review_insights.llm.schemas import normalize_topic_key
from app_review_insights.llm.usage import current_stage
from app_review_insights.models import Review
from app_review_insights.pipeline.analyze import analyze_batch
from app_review_insights.storage import RunRepository


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
                    f"{label}：主题键 {topic!r} 不是可比的 ascii 主题键"
                    "（应形如 subscription_transparency）"
                )

    return problems


def _normalize_topic(value: str) -> str:
    """比对前统一形态：大小写、分隔符、首尾空白都不应影响判定。

    直接复用 Schema 的 `normalize_topic_key`。这里原本有一份自己的实现，两份逻辑一旦分叉，
    "什么算同一个主题"就会有两套答案——规范化口径只能有一处。
    """
    return normalize_topic_key(value)


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
        # 引用召回：标注认为相关的评论，模型覆盖了多少。
        # 它是**不依赖词表**的口径——topic_recall 比的是双方选的 key 是否逐字一致，
        # 而 key 是自由文本：2026-09-20 的实测显示模型给出的键语义正确但粒度更细
        # （gold 的 subscription_transparency ↔ 模型的 trial_renewal_disclosure），
        # 于是精确匹配恒低。要判断"模型有没有找到同一批问题"，看它引用了哪些评论更可靠。
        "reference_recall": round(
            _set_score(
                expected_review_ids,
                predicted_review_ids,
                precision=False,
            ),
            3,
        ),
    }


def history_path(report: dict[str, Any], directory: Path, today: date, *, moment=None) -> Path:
    """评测历史的文件名：`<日期>T<时分>-<prompt 指纹>.json`。

    指纹进文件名，是为了让"这两份报告是不是同一套 prompt 跑出来的"在文件系统层面就看得出来——
    不同 prompt 的结果不该被摆在同一条趋势线上比较。

    带上时分是为了**同一天同一 prompt 跑两次不会互相覆盖**：重复运行本身是有价值的信息
    （两次数字差多少，就是这套指标自身的运行间波动），覆盖掉就看不到了。
    """
    fingerprint = str(report.get("prompt_fingerprint") or "unknown")
    stamp = today.isoformat()
    if moment is not None:
        stamp = f"{stamp}T{moment.strftime('%H%M')}"
    return Path(directory) / f"{stamp}-{fingerprint}.json"


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


#: 逐用例的指标名。**加指标时必须同时加到这里。**
#:
#: 为什么要有这个常量：`evaluate_case` 是显式列字段返回的，`score_predictions` 里算了、
#: 却没列进来的指标会被丢掉；而 `_aggregate` 用 `.get(指标, 0)` 取值，于是汇总里出现一个
#: **看起来很合理的 0.0**。2026-09-20 就发生过：`reference_recall` 汇总恒为 0.0，
#: 差一点被当成"模型一条标注评论都没覆盖"写进文档——之所以没写错，是因为发现
#: `reference_precision=0.798` 与 `reference_recall=0.0` 在数学上不可能共存。
#: `tests/test_eval_scoring.py` 里有一条结构性测试守着这个一致性。
CASE_METRICS = (
    "topic_recall",
    "reference_precision",
    "reference_recall",
    "hallucination_rate",
    "topic_key_coverage",
    # D-11 补的两条：词面一致率之外，回答"主题到底有没有被找到"以及"为什么词面对不上"
    "topic_found_by_evidence",
    "topic_granularity",
)


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def topic_set_stability(topic_sets: list[set[str]]) -> float:
    """同输入多次运行的主题集合一致度：所有两两组合的 Jaccard 均值。

    比的是 topic_key 集合，不是 Finding 的措辞——措辞天然会变，要回答的问题是
    "模型有没有稳定地识别出同一批问题"。
    """
    if len(topic_sets) < 2:
        return 1.0
    pairs = [
        _jaccard(topic_sets[i], topic_sets[j])
        for i in range(len(topic_sets))
        for j in range(i + 1, len(topic_sets))
    ]
    return round(sum(pairs) / len(pairs), 3)


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def _score_prediction(
    case: dict[str, Any], reviews: list[Review], prediction: Any
) -> dict[str, Any]:
    """单次运行的指标。稳定性不在这里——那是"多次运行之间"才谈得上的事。"""
    # 只比 topic_key。topic_label 按 prompt 的约定是**中文**，与黄金集里的 ascii 键不可比；
    # 取 label 会让 predicted_topics 恒为空集 —— 主题召回结构性恒为 0（2026-09-20 修）。
    predicted_topics = {finding.topic_key for finding in prediction.findings}
    expected_topics = set(case.get("expected_topics", []))
    expected_review_ids = set(case.get("expected_review_ids", []))
    findings_total = len(prediction.findings)
    key_covered = sum(1 for finding in prediction.findings if finding.topic_key)
    predicted_review_ids = {
        review_id for finding in prediction.findings for review_id in finding.supporting_review_ids
    }
    existing_ids = {review.review_id for review in reviews}
    hallucinated = sorted(predicted_review_ids - existing_ids)
    score = score_predictions(
        set(case.get("expected_topics", [])),
        set(case.get("expected_review_ids", [])),
        predicted_topics,
        predicted_review_ids,
    )
    return {
        **score,
        "predicted_topics": sorted(predicted_topics),
        "predicted_review_ids": sorted(predicted_review_ids),
        "hallucinated_review_ids": hallucinated,
        # 模型给了中文键（规范化后为空）时的缺口。没有 finding 时不算缺口——那是"证据不足"
        # 这类用例的正常结果，召回率会如实反映它。
        "topic_key_coverage": round(key_covered / findings_total, 3) if findings_total else 1.0,
        # 幻觉率：引用了输入里**根本不存在**的评论。与 reference_precision 的区别在于口径——
        # precision 比的是"与人工标注是否一致"，幻觉率比的是"这条评论到底存不存在"，
        # 后者是事实，不需要任何标注。
        "hallucination_rate": (
            round(len(hallucinated) / len(predicted_review_ids), 3) if predicted_review_ids else 0.0
        ),
        # D-11：不依赖词表的主题级口径——"这个主题被找到了吗"，判据是**它的支撑评论
        # 有没有被用上**，而不是模型有没有恰好选中同一个词。标注里没有主题（或没有
        # 期望评论）时记 1.0：没有东西要找，就不该扣分。
        # 这个指标之所以总有定义，靠的是另一条不变量：Schema 要求每个 finding **至少
        # 引用一条评论**——不存在"给了主题却零证据"的状态。
        "topic_found_by_evidence": (
            1.0 if not expected_review_ids or (expected_review_ids & predicted_review_ids) else 0.0
        ),
        # 粒度比：模型给的主题数 ÷ 标注主题数。它解释了词面一致率为什么低——
        # 2026-09-20 的实测里模型把粗粒度标注切成了更细的键（一个标注主题对应好几个
        # 预测键），两边因此永远对不上。标注为空时记 1.0（无可比，不参与解释）。
        "topic_granularity": (
            round(len(predicted_topics) / len(expected_topics), 3) if expected_topics else 1.0
        ),
        "batch_limitations": prediction.batch_limitations,
    }


def evaluate_case(provider, case: dict[str, Any], *, repeats: int = 1) -> dict[str, Any]:
    """跑一个用例并打分；`repeats > 1` 时重复运行并给出稳定性。"""
    if repeats < 1:
        raise ValueError("repeats 必须 >= 1")
    reviews = clean_reviews(_case_reviews(case)).reviews
    scored = [
        _score_prediction(
            case, reviews, analyze_batch(provider, reviews, str(case["analysis_goal"]))
        )
        for _ in range(repeats)
    ]
    first = scored[0]
    # 逐指标取均值（多次运行时单次抖动不该主导指标）。用 CASE_METRICS 而不是手写字段：
    # 手写清单漏掉一个指标，它就会被静默丢掉并在汇总里变成 0.0。
    averaged = {metric: _mean([item[metric] for item in scored]) for metric in CASE_METRICS}
    return {
        "case_id": case["case_id"],
        **averaged,
        "structured_output_success": True,
        "runs": repeats,
        # 只跑一次就不谈稳定性：给 None 而不是 1.0，免得报告上出现一个**没测过**的"稳定"。
        "stability": (
            topic_set_stability([set(item["predicted_topics"]) for item in scored])
            if repeats > 1
            else None
        ),
        "predicted_topics": first["predicted_topics"],
        "predicted_review_ids": first["predicted_review_ids"],
        "hallucinated_review_ids": first["hallucinated_review_ids"],
        "batch_limitations": first["batch_limitations"],
        "error": None,
    }


def _aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = max(1, len(results))
    stability_values = [
        float(item["stability"]) for item in results if item.get("stability") is not None
    ]
    averaged = {
        metric: round(sum(float(item.get(metric, 0)) for item in results) / total, 3)
        for metric in CASE_METRICS
    }
    return {
        "cases": len(results),
        **averaged,
        "structured_output_success": round(
            sum(bool(item.get("structured_output_success")) for item in results) / total,
            3,
        ),
        # 只跑一次时这里是 None —— 报告要如实反映"这一项没测"，而不是给一个 1.0。
        "stability": (
            round(sum(stability_values) / len(stability_values), 3) if stability_values else None
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 App Review Insights Prompt 小型评测")
    parser.add_argument("--dataset", default="evals/gold-reviews.json")
    parser.add_argument("--live", action="store_true", help="调用当前 DeepSeek 配置")
    parser.add_argument("--output", help="可选的 JSON 结果输出路径")
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=1.0,
        help="评测的花费上限（美元，0 = 不限制）。默认 1.0：评测是最容易一不小心"
        "烧钱的地方，给一个安全上限比事后对账便宜",
    )
    parser.add_argument(
        "--save-history",
        action="store_true",
        help="把本次报告存进 evals/history/（文件名带 prompt 指纹），供跨版本比较",
    )
    parser.add_argument(
        "--history-dir",
        default="evals/history",
        help="评测历史的存放目录",
    )
    parser.add_argument(
        "--stability",
        type=int,
        default=1,
        help="每个用例重复运行次数，用于计算主题集合一致度（稳定性）。"
        ">1 时成本约为单次的 N 倍，受 --max-cost-usd 约束",
    )
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
    # 评测也要记账：此前 --live 不记录任何用量，"这次评测花了多少钱"无从回答——而
    # "扩评测集 / 跑稳定性要花多少"恰恰是最该先知道的事。用量落进与流水线同一张表
    # （stage="eval"），日预算因此把评测花费也算进去，符合"这是真花掉的钱"。
    repository = RunRepository(settings.database_path)
    spent_usd = 0.0

    def _record_usage(usage) -> None:
        nonlocal spent_usd
        spent_usd += usage.estimated_cost_usd
        repository.record_model_usage(usage)

    if args.stability > 1:
        print(
            f"稳定性模式：每个用例重复 {args.stability} 次，成本约为单次的 {args.stability} 倍"
            "（仍受 --max-cost-usd 约束）。",
            file=sys.stderr,
        )

    results: list[dict[str, Any]] = []
    budget_exceeded = False
    stage_token = current_stage.set("eval")
    try:
        provider = DeepSeekProvider.from_settings(settings, usage_recorder=_record_usage)
        for case in cases:
            if args.max_cost_usd and spent_usd >= args.max_cost_usd:
                budget_exceeded = True
                break
            try:
                results.append(evaluate_case(provider, case, repeats=args.stability))
            except RecoverableModelError as exc:
                results.append(
                    {
                        "case_id": case["case_id"],
                        "topic_recall": 0.0,
                        "reference_precision": 0.0,
                        "structured_output_success": False,
                        "topic_key_coverage": 0.0,
                        "hallucination_rate": 0.0,
                        "runs": args.stability,
                        "stability": None,
                        "predicted_topics": [],
                        "predicted_review_ids": [],
                        "batch_limitations": [],
                        "error": str(exc),
                    }
                )
    finally:
        current_stage.reset(stage_token)

    if budget_exceeded:
        print(
            f"已达花费上限（{args.max_cost_usd:.4f} 美元），在完成 "
            f"{len(results)}/{len(cases)} 个用例后停止；调高 --max-cost-usd 可继续。",
            file=sys.stderr,
        )

    report = {
        "provider": settings.model_provider,
        "model": settings.model_name,
        # prompt 版本 + 文本指纹：跨版本比较指标时，"是不是同一套 prompt 跑出来的"
        # 必须写在报告里，否则趋势图会把两次不同 prompt 的结果混成一条线。
        "prompt_version": prompt_version(),
        "prompt_fingerprint": prompt_fingerprint(),
        "dataset": str(dataset_path),
        "usage": {
            # 实际花掉的钱。刻意不叫 saved 之类——评测没有缓存，就是实付。
            "estimated_cost_usd": round(spent_usd, 6),
            "cases_completed": len(results),
            "cases_total": len(cases),
            "budget_usd": args.max_cost_usd,
            "budget_exceeded": budget_exceeded,
        },
        "summary": _aggregate(results),
        "results": results,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")

    if args.save_history:
        now = datetime.now(UTC)
        saved_path = history_path(report, Path(args.history_dir), now.date(), moment=now)
        saved_path.parent.mkdir(parents=True, exist_ok=True)
        saved_path.write_text(rendered, encoding="utf-8")
        print(f"评测历史已保存：{saved_path}", file=sys.stderr)

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
