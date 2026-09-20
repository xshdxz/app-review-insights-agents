"""评测中心：数据驱动的模型评测与对比。

加载运行 /evals/gold-reviews.json 上的评测结果，展示指标与逐案详情；
支持从 UI 运行新评测并对比历史结果。
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

_EVAL_RESULTS_DIR = Path("output")
_GOLD_PATH = Path("evals/gold-reviews.json")


def _load_latest_results() -> list[dict]:
    """加载所有评测结果文件，按文件名排序（日期在前）。"""
    if not _EVAL_RESULTS_DIR.exists():
        return []
    results = []
    for path in sorted(_EVAL_RESULTS_DIR.glob("prompt-eval*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["_source_path"] = str(path)
            results.append(data)
        except Exception:
            continue
    return results


def _render_summary(results: list[dict]) -> None:
    """展示总体指标。"""
    if not results:
        st.info("尚未有评测结果。请运行一次评测后再查看。")
        return
    latest = results[-1]
    summary = latest.get("summary", {})
    cols = st.columns(4)
    cols[0].metric("案例数", summary.get("cases", 0))
    cols[1].metric(
        "主题词面一致率",
        f"{summary.get('topic_recall', 0):.1%}",
        help="模型选的主题键与标注键**逐字一致**的比例。它不是召回率：键是自由文本，"
        "模型常把标注的粗粒度主题切成更细的键（见下面的粒度比），语义对了词面也对不上。",
    )
    cols[2].metric("引用精确率", f"{summary.get('reference_precision', 0):.1%}")
    cols[3].metric("结构化输出成功率", f"{summary.get('structured_output_success', 0):.1%}")
    cols = st.columns(3)
    cols[0].metric(
        "主题命中率（按证据）",
        f"{summary.get('topic_found_by_evidence', 0):.1%}",
        help="标注主题的支撑评论里至少有一条被模型引用。**不依赖词表**，回答的是"
        "「这个问题被找到了吗」——与词面一致率一起看，才不会被任一个误导。",
    )
    cols[1].metric("引用召回率", f"{summary.get('reference_recall', 0):.1%}")
    cols[2].metric(
        "主题粒度比",
        f"{summary.get('topic_granularity', 1):.2f}",
        help="模型给的主题数 ÷ 标注主题数。大于 1 说明模型切得比标注细——"
        "这正是词面一致率偏低的成因。",
    )
    st.caption(
        f"模型：{latest.get('model', '?')} ({latest.get('provider', '?')}) · "
        f"数据集：{latest.get('dataset', '?')} · "
        f"来源：{latest.get('_source_path', '?')}"
    )


def _render_case_detail(case_result: dict) -> None:
    """单个案例的评测详情。"""
    case_id = case_result.get("case_id", "?")
    topic_recall = case_result.get("topic_recall", 0)
    ref_precision = case_result.get("reference_precision", 0)
    label = f"📊 {case_id} — topic_recall={topic_recall:.1%}  ref_precision={ref_precision:.1%}"
    with st.expander(label):
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**模型预测**")
            topics = case_result.get("predicted_topics", [])
            st.write(f"主题：{', '.join(topics) if topics else '(无)'}")
            review_ids = case_result.get("predicted_review_ids", [])
            st.write(f"关联评论：{', '.join(review_ids) if review_ids else '(无)'}")
            if case_result.get("error"):
                st.error(f"错误：{case_result['error']}")
        with col2:
            st.markdown("**数据集标注**")
            gold = _get_gold_case(case_id)
            if gold:
                expected = gold.get("expected_topics", [])
                st.write(f"期望主题：{', '.join(expected) if expected else '(无/应为空)'}")
                expected_ids = gold.get("expected_review_ids", [])
                st.write(f"期望关联：{', '.join(expected_ids) if expected_ids else '(无)'}")
            else:
                st.caption("（无法读取 gold dataset）")
        limitations = case_result.get("batch_limitations", [])
        if limitations:
            st.caption("局限：" + "；".join(limitations))
        # 得分可视化
        recall = case_result.get("topic_recall", 0)
        precision = case_result.get("reference_precision", 0)
        st.progress(min(recall, 1.0), text=f"主题词面一致 {recall:.0%}")
        st.progress(min(precision, 1.0), text=f"引用精确 {precision:.0%}")


def _get_gold_case(case_id: str) -> dict | None:
    try:
        gold = json.loads(_GOLD_PATH.read_text(encoding="utf-8"))
        for case in gold.get("cases", []):
            if case.get("case_id") == case_id:
                return case
    except Exception:
        pass
    return None


def _render_comparison(results: list[dict]) -> None:
    """多轮结果对比表格。"""
    if len(results) < 2:
        return
    st.subheader("历史对比", anchor=False)
    rows = []
    for r in results:
        summary = r.get("summary", {})
        rows.append(
            {
                "来源": r.get("_source_path", "?").split("/")[-1],
                "模型": r.get("model", "?"),
                "主题词面一致": f"{summary.get('topic_recall', 0):.1%}",
                "主题命中（按证据）": f"{summary.get('topic_found_by_evidence', 0):.1%}",
                "主题粒度比": f"{summary.get('topic_granularity', 1):.2f}",
                "引用精确率": f"{summary.get('reference_precision', 0):.1%}",
                "结构化成功率": f"{summary.get('structured_output_success', 0):.1%}",
                "案例数": summary.get("cases", 0),
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)


def main() -> None:
    st.set_page_config(
        page_title="评测中心",
        page_icon=":material/analytics:",
        layout="wide",
    )
    st.title("评测中心", anchor=False)
    st.caption(
        "基于标注数据集对模型进行评测：主题词面一致率（与标注选词逐字一致的比例）、"
        "主题命中率（按证据：标注主题的支撑评论有没有被用上）、引用精确率/召回率、"
        "主题粒度比与结构化输出成功率。"
    )
    results = _load_latest_results()
    _render_summary(results)
    if results:
        latest = results[-1]
        st.subheader("逐案详情", anchor=False)
        for case_result in latest.get("results", []):
            _render_case_detail(case_result)
        _render_comparison(results)
    else:
        st.info("运行 `python scripts/run_eval.py --live` 后刷新此页面。")


main()
