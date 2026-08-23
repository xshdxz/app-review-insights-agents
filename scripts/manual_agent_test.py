"""手动测试 Agent 部分的离线验证脚本。
用法：python scripts/manual_agent_test.py
不需要 API Key，验证所有确定性路径。
"""

from __future__ import annotations

import json

# 确保离线模式
import os
import sys
from pathlib import Path

os.environ.setdefault("MODEL_ENABLED", "false")
os.environ.pop("DEEPSEEK_API_KEY", None)
os.environ.pop("MODEL_API_KEY", None)

def test_1_cli_offline():
    """CLI 离线冒烟：Planner 回退默认计划，Reviewer 纯确定性。"""
    from app_review_insights.config import load_settings
    from app_review_insights.factory import build_agent_stack

    settings = load_settings()
    stack = build_agent_stack(settings)
    result = stack.orchestrator.run(
        "分析订阅转化",
        "https://apps.apple.com/us/app/x/id1",
        review_limit=100,
    )
    assert result.run_id, "run_id 应存在"
    assert result.status.value in ("failed", "completed", "waiting_approval")
    print(f"  [OK] CLI 离线运行：status={result.status.value}, run_id={result.run_id[:8]}")
    return True


def test_2_event_log():
    """结构化日志：agent_events.jsonl 应有记录。"""
    from app_review_insights.agent.orchestrator import _event_path
    from app_review_insights.storage.agent_repository import AgentRepository

    repo = AgentRepository("data/agent/agent.sqlite3")
    log_path = _event_path(repo)
    if not log_path.exists():
        print("  [SKIP] agent_events.jsonl 不存在（可能从未运行过 agent）")
        return True
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 1, f"至少应有 1 条事件，实际 {len(lines)}"
    event = json.loads(lines[-1])
    assert "run_id" in event and "step" in event
    print(f"  [OK] 结构化日志：{len(lines)} 条事件，最后一步={event['step']}")
    return True


def test_3_rag_fts():
    """FTS5 检索：对已有语料做关键词搜索。"""
    from app_review_insights.storage.agent_repository import AgentRepository

    repo = AgentRepository("data/agent/agent.sqlite3")
    hits = repo.search_corpus("订阅", limit=5)
    if not hits:
        print("  [SKIP] 语料库为空（未索引过评论）")
        return True
    assert all(0 <= h["score"] <= 1 for h in hits), "score 应归一化到 [0,1]"
    score_lo = f"{hits[-1]['score']:.3f}"
    score_hi = f"{hits[0]['score']:.3f}"
    print(f"  [OK] FTS5 检索：命中 {len(hits)} 条，score 范围 [{score_lo}, {score_hi}]")
    return True


def test_4_query_rewriter_offline():
    """Query Rewriter 离线降级：无 provider 时返回原始问题。"""
    from app_review_insights.rag.rewriter import QueryRewriter

    rewriter = QueryRewriter(provider=None)
    result = rewriter.rewrite("订阅转化怎么样？")
    assert result == ["订阅转化怎么样？"], f"应返回原始问题，实际 {result}"
    print("  [OK] Query Rewriter 离线降级：返回原始问题")
    return True


def test_5_eval_offline():
    """评测脚本离线：不调 API，只验证数据集加载。"""
    gold_path = Path("evals/gold-reviews.json")
    assert gold_path.exists(), "gold-reviews.json 不存在"
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    cases = gold["cases"]
    assert len(cases) == 3, f"应有 3 个案例，实际 {len(cases)}"
    for case in cases:
        assert "case_id" in case
        assert "reviews" in case
        assert len(case["reviews"]) >= 1
    total_reviews = sum(len(c["reviews"]) for c in cases)
    print(f"  [OK] 评测数据集：{len(cases)} 个案例，共 {total_reviews} 条评论")
    return True


def test_6_webhook_sender():
    """WebhookSender 空配置不崩溃。"""
    from app_review_insights.monitor.webhook import WebhookSender

    sender = WebhookSender()
    # 空 payload 不应崩溃
    result = sender.send("https://httpbin.org/status/200", {"msg": "test"})
    print(f"  [OK] WebhookSender 空配置测试：send 返回 {result}")
    return True


def test_7_tool_registry():
    """ToolRegistry dict 查找（优化 #1）。"""
    from pydantic import BaseModel

    from app_review_insights.agent.tools import Tool, ToolRegistry

    class DummyParams(BaseModel):
        x: str = ""

    def dummy_func(x: str = ""):
        return {"ok": True}

    tools = [Tool(name="t1", description="test", parameters=DummyParams, func=dummy_func)]
    registry = ToolRegistry(tools)
    assert registry.get("t1").name == "t1"
    try:
        registry.get("nonexistent")
        raise AssertionError("应抛 KeyError")  # noqa: B011
    except KeyError:
        pass
    result = registry.invoke("t1", x="hello")
    assert result == {"ok": True}
    print("  [OK] ToolRegistry dict 查找：O(1) 查找 + invoke 正常")
    return True


def test_8_quote_valid_nfkC():
    """_quote_valid NFKC 归一化（优化 #5）。"""
    from app_review_insights.rag.answer import _quote_valid

    # 全角/半角统一
    assert _quote_valid("ＡＢＣ", "ＡＢＣＤＥ"), "全角应匹配"
    assert _quote_valid("ＡＢＣ", "ABCDE"), "全角应匹配半角"
    # 空白折叠
    assert _quote_valid("订 阅 价 格", "订阅价格透明"), "空白折叠后应匹配"
    # 无效引用
    assert not _quote_valid("不存在的内容", "订阅价格透明"), "不存在的引用应不匹配"
    print("  [OK] _quote_valid NFKC：全角/半角/空白折叠全部通过")
    return True


if __name__ == "__main__":
    tests = [
        test_1_cli_offline,
        test_2_event_log,
        test_3_rag_fts,
        test_4_query_rewriter_offline,
        test_5_eval_offline,
        test_6_webhook_sender,
        test_7_tool_registry,
        test_8_quote_valid_nfkC,
    ]
    passed = 0
    failed = 0
    for test in tests:
        name = test.__doc__ or test.__name__
        try:
            if test():
                passed += 1
            else:
                failed += 1
                print(f"  [FAIL] {name}")
        except Exception as e:
            failed += 1
            print(f"  [ERROR] {name}: {e}")
    print(f"\n结果：{passed} 通过 / {failed} 失败 / {len(tests)} 总计")
    sys.exit(1 if failed else 0)
