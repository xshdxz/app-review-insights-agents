"""监控任务：定时任务 CRUD、人工审批、报告查看与推送，手动触发 Agent 运行并实时展示推理过程。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import streamlit as st

from app_review_insights.agent.human_in_loop import approve_run, reject_run
from app_review_insights.agent.schemas import AgentEvent
from app_review_insights.config import load_settings
from app_review_insights.factory import build_agent_stack
from app_review_insights.models import AgentRunStatus, MonitorJob


def _render_job_form(stack, settings) -> None:
    with st.container(border=True):
        st.subheader("新建监控任务", anchor=False)
        name = st.text_input("任务名称", placeholder="每日竞品监控", key="job-name")
        app_url = st.text_input(
            "App 链接",
            placeholder="https://apps.apple.com/us/app/...",
            key="job-url",
        )
        goal = st.text_input("分析目标", placeholder="监控订阅转化与差评趋势", key="job-goal")
        cron = st.text_input("cron 表达式（UTC）", value="0 9 * * *", key="job-cron")
        review_limit = st.slider("评论数量", 100, 1000, 200, key="job-limit")
        require_approval = st.checkbox("推送前需人工审批", key="job-approval")
        if st.button("创建任务", type="primary", key="job-create"):
            if not name or not app_url or not goal:
                st.error("任务名称、App 链接、分析目标必填")
            else:
                now = datetime.now(UTC)
                stack.agent_repository.save_job(
                    MonitorJob(
                        job_id=str(uuid4()),
                        name=name.strip(),
                        app_url=app_url.strip(),
                        goal=goal.strip(),
                        cron=cron.strip(),
                        review_limit=review_limit,
                        require_approval=require_approval,
                        created_at=now,
                        updated_at=now,
                    )
                )
                st.success(f"任务「{name}」已创建")


def _render_event(status_ctx, event: AgentEvent) -> None:
    """把 AgentEvent 实时写入 st.status 容器。"""
    step = event.step
    detail = event.detail
    duration = f" [{event.duration_s}s]" if event.duration_s else ""
    if step == "plan":
        tools = ", ".join(detail.get("tools", []))
        status_ctx.write(f"📋 规划：{detail.get('rationale', '')[:50]} → {tools}{duration}")
    elif step == "tool_call":
        tool = detail.get("tool", "?")
        status_ctx.write(f"🔧 执行：{tool}{duration}")
        summary = detail.get("result_summary", {})
        if isinstance(summary, dict) and summary.get("status"):
            status_ctx.write(f"   → {summary['status']}")
    elif step == "review":
        label = "✅ 通过" if detail.get("approved") else "❌ 未通过"
        status_ctx.write(f"👁️ 复核（第 {detail.get('round', '?')} 轮）：{label}{duration}")
    elif step == "redo":
        status_ctx.write(f"🔄 重做：{detail.get('feedback', '')[:60]}")
    elif step == "finalize":
        outcome = detail.get("outcome", "")
        status_ctx.write(f"🏁 {'完成' if outcome == 'approved' else '失败'}")


def _render_job_list(stack) -> None:
    st.subheader("任务列表", anchor=False)
    jobs = stack.agent_repository.list_jobs()
    if not jobs:
        st.caption("暂无任务")
        return
    for job in jobs:
        with st.container(border=True):
            cols = st.columns([2, 1, 1, 1, 1])
            cols[0].markdown(f"**{job.name}**  \n`{job.cron}` · {job.app_url}")
            cols[1].caption(f"上次：{job.last_status or '未运行'}")
            enabled = cols[2].toggle("启用", value=job.enabled, key=f"job-en-{job.job_id}")
            if enabled != job.enabled:
                updated = job.model_copy(
                    update={
                        "enabled": enabled,
                        "updated_at": datetime.now(UTC),
                    }
                )
                stack.agent_repository.save_job(updated)
                st.rerun()
            if cols[3].button("手动运行", key=f"run-{job.job_id}", type="primary"):
                status = st.status(f"运行任务「{job.name}」", expanded=True)
                status.write(f"目标：{job.goal}\nApp：{job.app_url}")
                # 挂载实时推理回调（default 参数捕获当前 status 变量）
                stack.orchestrator.on_event = lambda e, s=status: _render_event(s, e)
                agent_run = stack.orchestrator.run(
                    job.goal, job.app_url,
                    require_approval=job.require_approval,
                    review_limit=job.review_limit,
                )
                label = f"任务「{job.name}」→ {agent_run.status.value}"
                state = "complete" if agent_run.status == AgentRunStatus.COMPLETED else "error"
                status.update(label=label, state=state)
                # 刷新任务状态
                refreshed = job.model_copy(update={
                    "last_run_at": datetime.now(UTC),
                    "last_status": agent_run.status.value,
                    "updated_at": datetime.now(UTC),
                })
                stack.agent_repository.save_job(refreshed)
                st.rerun()
            if cols[4].button("删除", key=f"del-{job.job_id}"):
                stack.agent_repository.delete_job(job.job_id)
                st.rerun()


def _render_approvals(stack) -> None:
    st.subheader("待审批", anchor=False)
    waiting = stack.agent_repository.list_agent_runs(status=AgentRunStatus.WAITING_APPROVAL)
    if not waiting:
        st.caption("无待审批运行")
        return
    for run in waiting:
        with st.container(border=True):
            st.markdown(f"**{run.goal}**  \n{run.app_url} · 轮次 {run.review_rounds}")
            st.caption(run.plan_summary or "")
            left, right = st.columns(2)
            if left.button("批准并推送", key=f"ok-{run.run_id}"):
                approve_run(stack.agent_repository, run.run_id)
                st.rerun()
            reason = right.text_input("驳回原因", key=f"reason-{run.run_id}")
            if right.button("驳回", key=f"no-{run.run_id}"):
                reject_run(stack.agent_repository, run.run_id, reason or "未填写原因")
                st.rerun()


def _render_reports(stack, settings) -> None:
    st.subheader("最近报告", anchor=False)
    reports = stack.agent_repository.list_reports(limit=20)
    if not reports:
        st.caption("暂无报告")
        return
    for report in reports:
        with st.expander(f"{report.created_at:%Y-%m-%d %H:%M} · {report.goal}"):
            st.write(report.summary)
            if report.changes:
                st.caption("变化：" + "；".join(report.changes))
            st.download_button(
                "下载 Markdown",
                report.markdown,
                file_name=f"report-{report.report_id}.md",
                mime="text/markdown",
                key=f"dl-{report.report_id}",
            )
            if st.button("立即推送", key=f"push-{report.report_id}"):
                from app_review_insights.monitor.webhook import WebhookSender

                delivered = WebhookSender().send_report_by_id(
                    report.report_id, settings, stack.agent_repository
                )
                if delivered:
                    st.success(f"已推送到 {len(delivered)} 个地址")
                else:
                    st.warning("未配置 Webhook 或推送失败")


def main() -> None:
    st.set_page_config(
        page_title="监控任务",
        page_icon=":material/schedule:",
        layout="wide",
    )
    st.title("监控任务", anchor=False)
    settings = load_settings()
    stack = build_agent_stack(settings)
    st.caption(
        "定时调度由 worker 进程执行（SCHEDULER_ENABLED=true 时）。"
        "本页的创建/启停/审批即时生效于下一次调度刷新。"
    )
    _render_job_form(stack, settings)
    _render_job_list(stack)
    _render_approvals(stack)
    _render_reports(stack, settings)


main()
