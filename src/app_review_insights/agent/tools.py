"""工具注册表：把确定性流水线、RAG、报告、推送暴露为可被 Agent 调用的工具。

每个工具带 Pydantic 参数模型：调用时先校验再执行，结果必须是 JSON 可序列化的 dict。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from app_review_insights.models import AnalysisRequest, SourceType
from app_review_insights.pipeline.orchestrator import AnalysisOrchestrator, PipelineServices


@dataclass
class Tool:
    name: str
    description: str
    parameters: type[BaseModel]
    func: Callable[..., Any]

    def invoke(self, **kwargs: Any) -> Any:
        params = self.parameters(**kwargs)
        return self.func(**params.model_dump())


@dataclass
class ToolRegistry:
    tools: list[Tool] = field(default_factory=list)

    def names(self) -> list[str]:
        return [tool.name for tool in self.tools]

    def get(self, name: str) -> Tool:
        for tool in self.tools:
            if tool.name == name:
                return tool
        raise KeyError(f"未知工具：{name}")

    def invoke(self, name: str, **kwargs: Any) -> Any:
        return self.get(name).invoke(**kwargs)

    def schemas(self) -> dict[str, Any]:
        return {
            tool.name: {
                "description": tool.description,
                "parameters": tool.parameters.model_json_schema(),
            }
            for tool in self.tools
        }


# ---- 工具参数模型 ----
class RunAnalysisParams(BaseModel):
    app_url: str = Field(min_length=1, description="App Store 链接")
    goal: str = Field(min_length=3, description="分析目标")
    review_limit: int = Field(default=200, ge=100, le=1000)


class QueryCorpusParams(BaseModel):
    app_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    compare_app_ids: list[str] = Field(default_factory=list)


class SendReportParams(BaseModel):
    report_id: str = Field(min_length=1)


class CollectReviewsParams(BaseModel):
    app_url: str = Field(min_length=1)
    review_limit: int = Field(default=200, ge=100, le=1000)


class GetLatestReportParams(BaseModel):
    app_url: str = Field(min_length=1)


# ---- 工具工厂 ----
def make_run_analysis_tool(pipeline_services: PipelineServices) -> Tool:
    def run_analysis(app_url: str, goal: str, review_limit: int = 200) -> dict:
        request = AnalysisRequest(
            source_type=SourceType.ONLINE,
            analysis_goal=goal,
            app_url=app_url,
            review_limit=review_limit,
        )
        run = AnalysisOrchestrator(pipeline_services).start(request)
        return {
            "run_id": run.run_id,
            "status": run.status.value,
            "current_stage": run.current_stage.value,
        }

    return Tool(
        name="run_analysis",
        description=(
            "对指定 App 运行完整分析流水线（采集→清洗→分析→证据校验→PRD/用例），返回 run_id。"
        ),
        parameters=RunAnalysisParams,
        func=run_analysis,
    )


def make_query_corpus_tool(rag_service: Any) -> Tool:
    def query_corpus(
        app_id: str,
        question: str,
        compare_app_ids: list[str] | None = None,
    ) -> dict:
        app_ids = [app_id, *(compare_app_ids or [])]
        answer = rag_service.answer(question, app_ids)
        if hasattr(answer, "model_dump"):
            return answer.model_dump(mode="json")
        return dict(answer)

    return Tool(
        name="query_corpus",
        description="对已建语料的 App 做 RAG 问答（可跨 App 对比），返回带证据引用的回答。",
        parameters=QueryCorpusParams,
        func=query_corpus,
    )


def make_send_report_tool(
    webhook_sender: Any,
    settings: Any | None = None,
    agent_repository: Any | None = None,
) -> Tool:
    def send_report(report_id: str) -> dict:
        if settings is not None and agent_repository is not None:
            delivered = webhook_sender.send_report_by_id(report_id, settings, agent_repository)
        else:
            delivered = webhook_sender.send_report_by_id(report_id)
        return {"report_id": report_id, "delivered_to": delivered}

    return Tool(
        name="send_report",
        description="把已生成的报告推送到配置的群机器人 Webhook。",
        parameters=SendReportParams,
        func=send_report,
    )


def make_collect_reviews_tool(collector: Any, pipeline_services: PipelineServices) -> Tool:
    def collect_reviews(app_url: str, review_limit: int = 200) -> dict:
        reviews = collector.collect(app_url, review_limit)
        return {"count": len(reviews), "sample_ids": [r.review_id for r in reviews[:5]]}

    return Tool(
        name="collect_reviews",
        description="仅采集并返回评论（不分析），用于了解数据规模。",
        parameters=CollectReviewsParams,
        func=collect_reviews,
    )


def make_get_latest_report_tool(agent_repository: Any) -> Tool:
    def get_latest_report(app_url: str) -> dict | None:
        report = agent_repository.latest_report(app_url)
        if report is None:
            return None
        return {
            "report_id": report.report_id,
            "summary": report.summary,
            "findings_count": report.findings_count,
            "created_at": report.created_at.isoformat(),
        }

    return Tool(
        name="get_latest_report",
        description="获取某 App 最近一次情报报告摘要。",
        parameters=GetLatestReportParams,
        func=get_latest_report,
    )
