"""检索质量评测：把「检索好不好」从感觉变成可回归的数字。

为什么单独一个脚本：RAG 这条线此前**只有实现、没有度量**——retrieval.py 里的 0.6/0.4 融合权重、
「查询改写能提升召回」这些说法都没有数字支撑。这里补的是**尺子**，不是算法。

三种配置：

- fts           纯 FTS5（离线、确定性、进 CI）
- fts+rewrite   查询改写后多路检索合并（需要密钥，不足 1 分钱/次；仅 --live-rewrite 时跑）
- hybrid        需要 embedding provider；**本机没有**，脚本会如实标成「未测」而不是跳过不提

用法：

    python scripts/run_retrieval_eval.py                     # 离线配置 + 汇总
    python scripts/run_retrieval_eval.py --validate-only     # 只校验数据集（零成本，CI 用）
    python scripts/run_retrieval_eval.py --live-rewrite      # 追加改写配置
    python scripts/run_retrieval_eval.py --fail-under-recall-at-3 0.6
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app_review_insights.config import load_settings
from app_review_insights.llm.provider import DeepSeekProvider
from app_review_insights.models import Review
from app_review_insights.rag.indexer import CorpusIndexer
from app_review_insights.rag.metrics import precision_at_k, recall_at_k, reciprocal_rank
from app_review_insights.rag.retrieval import CorpusRetriever
from app_review_insights.rag.rewriter import QueryRewriter
from app_review_insights.storage.agent_repository import AgentRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = PROJECT_ROOT / "evals" / "gold-retrieval.json"
GOLD_REVIEWS = PROJECT_ROOT / "evals" / "gold-reviews.json"
#: 评测语料统一挂在一个 App 下。多 App 的合并语义（search_many 的每 App 取 top）
#: 需要多 App 语料才谈得上，本评测不覆盖——这是**范围**，不是遗漏。
CORPUS_APP_ID = "eval-corpus"
CUTOFFS = (1, 3, 5)


def load_corpus() -> tuple[list[Review], set[str]]:
    """评测语料：直接复用黄金集的 120 条评论，不另造一套。"""
    gold = json.loads(GOLD_REVIEWS.read_text(encoding="utf-8"))
    now = datetime.now(UTC)
    reviews: list[Review] = []
    for case in gold["cases"]:
        for raw in case["reviews"]:
            reviews.append(
                Review(
                    review_id=str(raw["review_id"]),
                    app_id=CORPUS_APP_ID,
                    content_original=str(raw["content"]),
                    rating=int(raw["rating"]),
                    published_at=now,
                    source="eval-corpus",
                )
            )
    return reviews, {review.review_id for review in reviews}


def validate_dataset(dataset: dict[str, Any], corpus_ids: set[str]) -> list[str]:
    """校验查询集完整性，返回问题列表（空表示干净）。

    评测集坏掉是最隐蔽的失败：跑出来的数字毫无意义却没人发现（与 run_eval.py 的
    validate_dataset 同一考虑）。所以这些检查也进 CI。
    """
    problems: list[str] = []
    queries = dataset.get("queries")
    if not isinstance(queries, list) or not queries:
        return ["查询集缺少 queries 列表或列表为空"]
    seen: set[str] = set()
    for index, query in enumerate(queries):
        label = query.get("query_id") or f"第 {index + 1} 条查询"
        query_id = query.get("query_id")
        if not query_id:
            problems.append(f"{label}：缺少 query_id")
        elif query_id in seen:
            problems.append(f"{label}：query_id 重复")
        else:
            seen.add(query_id)
        text = str(query.get("query", "")).strip()
        if len(text) < 5:
            problems.append(f"{label}：查询过短")
        if query.get("language") not in {"zh", "en"}:
            problems.append(f"{label}：language 必须是 zh 或 en")
        expected = query.get("expected_review_ids")
        if not isinstance(expected, list) or not expected:
            problems.append(f"{label}：没有期望评论——检索评测里每条查询都必须有答案")
            continue
        for review_id in expected:
            if str(review_id) not in corpus_ids:
                problems.append(f"{label}：期望评论 {review_id} 不在语料里")
    return problems


def build_retriever(database_path: Path) -> CorpusRetriever:
    """在临时库上建索引——评测绝不碰 data/ 下的真实档案。"""
    repository = AgentRepository(database_path)
    reviews, _ = load_corpus()
    CorpusIndexer(repository).index_reviews(reviews)
    return CorpusRetriever(repository)


def retrieve(
    retriever: CorpusRetriever,
    query: str,
    *,
    top_k: int,
    variants: Callable[[str], list[str]] | None = None,
) -> list[str]:
    """按**应用里那条路径**取结果：改写出的多条查询各取一轮，去重后按分排序截断。

    与 RagAnswerer.answer 的顺序一致（先 search_many 再去重排序），否则量出来的就不是
    上线时跑的那条路。
    """
    queries = variants(query) if variants else [query]
    merged: list[Any] = []
    seen: set[str] = set()
    for one in queries:
        for chunk in retriever.search_many(one, [CORPUS_APP_ID], top_k=top_k):
            if chunk.review_id not in seen:
                seen.add(chunk.review_id)
                merged.append(chunk)
    merged.sort(key=lambda chunk: chunk.score, reverse=True)
    return [chunk.review_id for chunk in merged[:top_k]]


def score_query(query: dict[str, Any], retrieved: list[str]) -> dict[str, Any]:
    expected = [str(item) for item in query["expected_review_ids"]]
    row: dict[str, Any] = {
        "query_id": query["query_id"],
        "language": query["language"],
        "query": query["query"],
        "expected": expected,
        "retrieved": retrieved,
        "mrr": round(reciprocal_rank(expected, retrieved), 3),
        "precision@5": round(precision_at_k(expected, retrieved, k=5), 3),
    }
    for cutoff in CUTOFFS:
        row[f"recall@{cutoff}"] = round(recall_at_k(expected, retrieved, k=cutoff), 3)
    row["ranks"] = {
        review_id: (retrieved.index(review_id) + 1 if review_id in retrieved else None)
        for review_id in expected
    }
    return row


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    return round(sum(items) / len(items), 3) if items else 0.0


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = [*[f"recall@{cutoff}" for cutoff in CUTOFFS], "mrr", "precision@5"]
    summary: dict[str, Any] = {"queries": len(rows)}
    for metric in metrics:
        summary[metric] = _mean(float(row[metric]) for row in rows)
    for language in ("en", "zh"):
        group = [row for row in rows if row["language"] == language]
        if group:
            summary[f"recall@3[{language}]"] = _mean(float(row["recall@3"]) for row in group)
    return summary


def _render(rows: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = ["", "逐条结果（ranks 是期望评论在结果里的名次，None = 没进前 5）："]
    for row in rows:
        lines.append(
            f"  [{row['language']}] {row['query_id']:<20} recall@3={row['recall@3']:.2f} "
            f"mrr={row['mrr']:.2f}  {row['ranks']}"
        )
    lines.append("")
    lines.append("汇总：")
    for key, value in summary.items():
        lines.append(f"  {key} = {value}")
    return "\n".join(lines)


def _build_variants(settings: Any) -> Callable[[str], list[str]] | None:
    """改写的 provider；没密钥就返回 None（调用方据此跳过该配置并**如实说明**）。"""
    if not settings.model_available:
        return None
    provider = DeepSeekProvider.from_settings(settings)
    rewriter = QueryRewriter(provider)
    return lambda query: rewriter.rewrite(query)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检索质量评测")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--output", default=None)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--live-rewrite", action="store_true")
    parser.add_argument("--fail-under-recall-at-3", type=float, default=None)
    args = parser.parse_args(argv)

    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    _, corpus_ids = load_corpus()
    problems = validate_dataset(dataset, corpus_ids)
    if problems:
        for problem in problems:
            print(f"查询集问题：{problem}", file=sys.stderr)
        return 1
    if args.validate_only:
        print(f"查询集校验通过：{len(dataset['queries'])} 条查询，语料 {len(corpus_ids)} 条评论")
        return 0

    with tempfile.TemporaryDirectory(prefix="ari-retrieval-eval-") as tmp:
        retriever = build_retriever(Path(tmp) / "agent.sqlite3")
        settings = load_settings()
        configs: dict[str, Callable[[str], list[str]] | None] = {"fts": None}
        skipped: list[str] = []
        if args.live_rewrite:
            variants = _build_variants(settings)
            if variants is None:
                skipped.append("fts+rewrite：未配置可用的模型密钥")
            else:
                configs["fts+rewrite"] = variants
        skipped.append("hybrid：本机没有 embedding provider（EMBEDDING_ENABLED=false），未测")

        report: dict[str, Any] = {
            "dataset": args.dataset,
            "corpus_reviews": len(corpus_ids),
            "top_k": args.top_k,
            "configs": {},
            "skipped": skipped,
        }
        for name, variants in configs.items():
            rows = [
                score_query(
                    query, retrieve(retriever, query["query"], top_k=args.top_k, variants=variants)
                )
                for query in dataset["queries"]
            ]
            report["configs"][name] = {"summary": summarize(rows), "cases": rows}

    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    for name in report["configs"]:
        print(f"=== 配置 {name} ===", file=sys.stderr)
        print(
            _render(report["configs"][name]["cases"], report["configs"][name]["summary"]),
            file=sys.stderr,
        )
    for note in skipped:
        print(f"未测：{note}", file=sys.stderr)
    print(rendered)

    if args.fail_under_recall_at_3 is not None:
        baseline = report["configs"]["fts"]["summary"]["recall@3"]
        if baseline < args.fail_under_recall_at_3:
            print(
                f"检索门禁未通过：fts recall@3={baseline} < {args.fail_under_recall_at_3}",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
