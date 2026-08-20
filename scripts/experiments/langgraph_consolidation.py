"""实验：用 LangGraph 实现「归并」子流程，与原生 consolidate_findings 对比。

运行：python scripts/experiments/langgraph_consolidation.py --fixture tests/fixtures/xxx.json
结论写入 docs/experiments/langgraph-vs-native.md（人工/模型总结）。
"""

from __future__ import annotations

import argparse
import json
import time
from typing import TypedDict

from langgraph.graph import END, StateGraph


class ConsolidationState(TypedDict):
    findings: list[dict]
    goal: str
    output: list[dict]


def _consolidate_node(state: ConsolidationState) -> dict:
    # 占位实现：无模型去重归并；真实实验时替换为 DeepSeek 调用
    output = []
    seen = set()
    for finding in state["findings"]:
        key = finding.get("title", "")
        if key not in seen:
            seen.add(key)
            output.append(finding)
    return {"output": output}


def _validate_node(state: ConsolidationState) -> dict:
    # 确定性校验：必须保留支持证据
    valid = [f for f in state["output"] if f.get("supporting_review_ids")]
    return {"output": valid}


def build_graph():
    graph = StateGraph(ConsolidationState)
    graph.add_node("consolidate", _consolidate_node)
    graph.add_node("validate", _validate_node)
    graph.set_entry_point("consolidate")
    graph.add_edge("consolidate", "validate")
    graph.add_edge("validate", END)
    return graph.compile()


def run_experiment(fixture_path: str) -> dict:
    payload = json.loads(open(fixture_path, encoding="utf-8").read())
    graph = build_graph()
    started = time.perf_counter()
    result = graph.invoke({"findings": payload.get("findings", []), "goal": "g", "output": []})
    elapsed = time.perf_counter() - started
    return {"output_count": len(result["output"]), "elapsed_ms": round(elapsed * 1000, 2)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True)
    args = parser.parse_args()
    print(json.dumps(run_experiment(args.fixture), ensure_ascii=False))


if __name__ == "__main__":
    main()
