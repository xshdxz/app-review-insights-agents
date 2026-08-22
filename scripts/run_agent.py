"""命令行运行 Agent 编排：目标 → 计划 → 分析 → 复核 → 报告。

用法：
    python scripts/run_agent.py --app-url <URL> --goal "分析订阅转化" --out output/agent-run.json
    python scripts/run_agent.py --app-url <URL> --goal "..." --require-approval
    python scripts/run_agent.py --app-url <URL> --goal "..." --webhook-test
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app_review_insights.config import load_settings
from app_review_insights.factory import build_agent_stack
from app_review_insights.models import AgentRunStatus


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行产品情报 Agent")
    parser.add_argument("--app-url", required=True, help="App Store 链接")
    parser.add_argument("--goal", required=True, help="分析目标")
    parser.add_argument("--limit", type=int, default=200, help="评论数量（100–1000）")
    parser.add_argument("--out", type=Path, default=None, help="结果 JSON 输出路径")
    parser.add_argument("--require-approval", action="store_true", help="完成后停在待审批")
    parser.add_argument(
        "--webhook-test", action="store_true", help="只校验 Webhook payload 格式，不发送"
    )
    return parser.parse_args()


def _on_progress(event) -> None:
    """CLI 进度回调：把 AgentEvent 实时打印到终端。"""
    step = event.step
    detail = event.detail
    duration = f" [{event.duration_s}s]" if event.duration_s is not None else ""
    if step == "plan":
        tools = ", ".join(detail.get("tools", []))
        print(f"📋 规划完成：{detail.get('rationale', '')[:60]} → {tools}{duration}")
    elif step == "tool_call":
        tool = detail.get("tool", "?")
        print(f"🔧 执行工具：{tool}{duration}")
        summary = detail.get("result_summary", {})
        if isinstance(summary, dict):
            status = summary.get("status", "")
            run_id = summary.get("run_id", "")
            if status:
                print(f"   → 状态: {status}" + (f", run_id={run_id}" if run_id else ""))
    elif step == "review":
        approved = detail.get("approved", False)
        feedback = detail.get("feedback", "")[:80]
        label = "通过" if approved else "未通过"
        rnd = detail.get("round", "?")
        print(f"👁️ Reviewer {label}（第 {rnd} 轮）{duration}")
        if feedback:
            print(f"   → {feedback}")
    elif step == "redo":
        print(f"🔄 重做：{detail.get('feedback', '')[:80]}")
    elif step == "finalize":
        outcome = detail.get("outcome", "")
        print(f"🏁 {'完成' if outcome == 'approved' else '失败'}{duration}")


def main() -> int:
    args = parse_args()
    settings = load_settings()
    stack = build_agent_stack(settings)

    stack.orchestrator.on_event = _on_progress

    agent_run = stack.orchestrator.run(
        args.goal,
        args.app_url,
        require_approval=args.require_approval,
        review_limit=args.limit,
    )

    result = {
        "agent_run_id": agent_run.run_id,
        "status": agent_run.status.value,
        "plan_summary": agent_run.plan_summary,
        "analysis_run_id": agent_run.analysis_run_id,
        "review_rounds": agent_run.review_rounds,
        "feedback": agent_run.feedback,
        "error": agent_run.error,
    }

    if args.webhook_test and settings.webhook_urls:
        try:
            from app_review_insights.monitor.webhook import WebhookSender

            sender = WebhookSender()
            result["webhook_payloads"] = sender.build_payloads_for_test(settings)
        except ImportError:
            # webhook 模块在后续任务接入；当前仅优雅降级，不中断命令
            result["webhook_payloads"] = []
            result["webhook_note"] = "webhook 模块尚未接入（后续版本提供）"

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入 {args.out}")

    if agent_run.status in (AgentRunStatus.COMPLETED, AgentRunStatus.WAITING_APPROVAL):
        print(f"[ok] agent_run={agent_run.run_id} status={agent_run.status.value}")
        return 0
    print(
        f"[error] agent_run={agent_run.run_id} status={agent_run.status.value} "
        f"error={agent_run.error}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
