import pytest
from pydantic import BaseModel, Field, ValidationError

from app_review_insights.agent.schemas import ToolCall
from app_review_insights.agent.tools import Tool, ToolRegistry
from app_review_insights.pipeline.orchestrator import PipelineServices
from app_review_insights.storage.repository import RunRepository


class _FakePipeline:
    def __init__(self):
        self.calls = []

    def start(self, request, imported_reviews=None):
        self.calls.append(request)
        return _FakeRun()


class _FakeRun:
    run_id = "run-1"
    status = "completed"


class _FakeRag:
    def __init__(self):
        self.calls = []

    def answer(self, question, app_ids):
        self.calls.append((question, app_ids))
        return {"answer": "ok", "citations": [], "evidence_sufficient": True, "limitation": ""}


class _FakeWebhook:
    def __init__(self):
        self.calls = []

    def send_report(self, report, settings):
        self.calls.append(report.report_id)
        return ["feishu"]


@pytest.fixture
def registry(tmp_path):
    repo = RunRepository(tmp_path / "runs.sqlite3")
    fake_pipeline = _FakePipeline()
    pipeline = PipelineServices(repository=repo, batch_analyzer=None)
    registry = ToolRegistry(
        tools=[
            Tool(
                name="run_analysis",
                description="运行标准分析流水线",
                parameters=_RunAnalysisParams,
                func=_make_run_analysis(fake_pipeline, pipeline),
            ),
            Tool(
                name="query_corpus",
                description="RAG 问答",
                parameters=_QueryCorpusParams,
                func=_make_query_corpus(_FakeRag()),
            ),
        ]
    )
    return registry


class _RunAnalysisParams(BaseModel):
    app_url: str = Field(min_length=1)
    goal: str = Field(min_length=3)
    review_limit: int = Field(default=200, ge=100, le=1000)


class _QueryCorpusParams(BaseModel):
    app_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    compare_app_ids: list[str] = Field(default_factory=list)


def _make_run_analysis(fake_pipeline, pipeline):
    def run_analysis(app_url: str, goal: str, review_limit: int = 200) -> dict:
        request = pipeline_request(app_url, goal, review_limit)
        run = fake_pipeline.start(request)
        return {"run_id": run.run_id, "status": run.status}

    return run_analysis


def _make_query_corpus(fake_rag):
    def query_corpus(
        app_id: str,
        question: str,
        compare_app_ids: list[str] | None = None,
    ) -> dict:
        app_ids = [app_id, *(compare_app_ids or [])]
        return fake_rag.answer(question, app_ids)

    return query_corpus


def pipeline_request(app_url, goal, review_limit):
    from app_review_insights.models import AnalysisRequest, SourceType

    return AnalysisRequest(
        source_type=SourceType.ONLINE,
        analysis_goal=goal,
        app_url=app_url,
        review_limit=review_limit,
    )


def test_registry_names_and_get(registry):
    assert set(registry.names()) == {"run_analysis", "query_corpus"}
    assert registry.get("run_analysis").name == "run_analysis"


def test_registry_invoke_validates_params(registry):
    result = registry.invoke(
        "run_analysis",
        app_url="https://apps.apple.com/us/app/x/id1",
        goal="分析订阅转化",
        review_limit=100,
    )
    assert result["run_id"] == "run-1"


def test_registry_invoke_query_corpus(registry):
    result = registry.invoke(
        "query_corpus",
        app_id="app-1",
        question="订阅价格怎么样？",
        compare_app_ids=["app-2"],
    )
    assert result["answer"] == "ok"
    assert result["evidence_sufficient"] is True


def test_registry_invoke_rejects_bad_params(registry):
    with pytest.raises(ValidationError):
        registry.invoke("run_analysis", app_url="", goal="ok")


def test_registry_invoke_unknown_tool(registry):
    with pytest.raises(KeyError):
        registry.invoke("nope")


def test_tool_call_schema_roundtrip():
    call = ToolCall(tool="run_analysis", arguments={"app_url": "https://x", "goal": "g"})
    assert call.tool == "run_analysis"
