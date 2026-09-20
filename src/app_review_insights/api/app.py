"""HTTP 契约层：把流水线暴露成服务。

为什么是一个**薄**层：业务逻辑一行都不在这里。它只做三件事——把 HTTP 语义翻译成
编排器与仓储的调用、把领域对象翻译成响应、在写入端点上做鉴权与背压。任何"顺手在
接口里加点逻辑"都会让服务与界面各长出一套规则，两边迟早不一致。

为什么 API 是可选 extra：Streamlit Cloud 那种单进程演示部署不需要它，而
fastapi + uvicorn 是实打实的常驻运行时依赖（还会占一个端口）。装上才有这一层，
不装则代码路径完全不存在。

端点一览（v1）：

- `GET  /healthz`                    存活探针
- `GET  /metrics`                    与 worker 同源的 Prometheus 文本指标
- `GET  /v1/queue`                   队列深度与"有没有执行者在消费"
- `GET  /v1/runs`                    最近运行列表
- `POST /v1/runs`                    **提交一次分析（需令牌）**，202 + run_id
- `GET  /v1/runs/{id}`               单条运行的状态与用量
- `GET  /v1/runs/{id}/events`        事件列表；`?stream=1` 走 SSE
- `GET  /v1/runs/{id}/result`        原始阶段产物（证据链）
- `POST /v1/runs/{id}/cancel`        **请求取消（需令牌）**——在下一个阶段边界生效
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator

from app_review_insights.config import Settings, load_settings
from app_review_insights.factory import build_pipeline_services
from app_review_insights.input_parsing import import_reviews
from app_review_insights.models import AnalysisRequest, RunRecord, RunStatus, SourceType, Stage
from app_review_insights.monitor.health import HealthState
from app_review_insights.monitor.queue_executor import HEARTBEAT_INTERVAL_SECONDS
from app_review_insights.pipeline.orchestrator import AnalysisOrchestrator, PipelineServices
from app_review_insights.storage.repository import RunRepository

logger = logging.getLogger("ari-api")

API_VERSION = "1.0"

#: 事件流最长挂多久。没有上限的 SSE 会把连接与线程一起占住，而"客户端忘了断开"
#: 是这类接口最常见的失败方式。
EVENT_STREAM_MAX_SECONDS = 1800.0
EVENT_POLL_SECONDS = 0.5

#: 仍然是"进行中"的状态。事件流跑到终态就主动结束，不必等客户端断开。
ACTIVE_STATUSES = (RunStatus.PENDING, RunStatus.RUNNING, RunStatus.WAITING, RunStatus.TIMED_OUT)

#: 结果端点暴露哪些阶段产物。刻意不做领域拼装：那等于在这里复制一份界面已有的
#: 组装逻辑，两边迟早不一致；产物形状以 docs/data-format.md 为准。
RESULT_STAGES = (
    Stage.COLLECT,
    Stage.CLEAN,
    Stage.ANALYZE_BATCHES,
    Stage.CONSOLIDATE,
    Stage.AUDIT_EVIDENCE,
    Stage.VALIDATE_FINDINGS,
    Stage.PLAN,
    Stage.GENERATE_TESTS,
    Stage.VALIDATE_TRACEABILITY,
)


class RunSubmission(BaseModel):
    """提交一次分析。至少要给一样输入：在线采集地址，或直接给评论。"""

    analysis_goal: str = Field(min_length=1, max_length=2000)
    app_url: str | None = None
    review_limit: int = Field(default=200, ge=100, le=1000)
    #: 与界面 JSON 导入同构的评论数组——复用 import_reviews 这一条校验路径，
    #: 不在这里另写一套字段解析。
    reviews: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def require_some_input(self) -> RunSubmission:
        if not self.app_url and not self.reviews:
            raise ValueError("必须提供 app_url（在线采集）或 reviews（直接给评论）之一")
        return self


class RunSummary(BaseModel):
    run_id: str
    status: str
    current_stage: str
    current_batch: int
    total_batches: int
    coverage_ratio: float
    is_live: bool
    mode: str
    cancel_requested: bool
    created_at: datetime
    updated_at: datetime
    last_error: str | None = None


class RunDetail(RunSummary):
    usage: dict[str, Any] = Field(default_factory=dict)


class QueueState(BaseModel):
    depth: int
    executor_seen: bool


def _summary(run: RunRecord, repository: RunRepository) -> RunSummary:
    return RunSummary(
        run_id=run.run_id,
        status=run.status.value,
        current_stage=run.current_stage.value,
        current_batch=run.current_batch,
        total_batches=run.total_batches,
        coverage_ratio=run.coverage_ratio,
        is_live=run.is_live,
        mode=run.mode,
        cancel_requested=repository.cancel_requested(run.run_id),
        created_at=run.created_at,
        updated_at=run.updated_at,
        last_error=run.last_error,
    )


def _run_or_404(repository: RunRepository, run_id: str) -> RunRecord:
    try:
        return repository.get_run(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"没有这条运行：{run_id}") from None


def _event_payload(event: Any) -> dict[str, Any]:
    return {
        "stage": event.stage.value,
        "status": event.status.value,
        "message": event.message,
        "created_at": event.created_at.isoformat(),
    }


def create_app(
    settings: Settings | None = None,
    services: PipelineServices | None = None,
) -> FastAPI:
    """装配 HTTP 接口。依赖注入点留着，测试用得上（不必真起端口）。"""
    settings = settings or load_settings()
    repository = RunRepository(settings.database_path)
    pipeline = services or build_pipeline_services(settings)

    def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
        """写入端点的令牌校验。

        没配令牌时**拒绝服务**而不是放行：提交一次分析是真金白银，一个能匿名花钱的
        接口不该被默认打开。错误信息里写清楚怎么开，免得使用者只能去翻源码。
        """
        if not settings.api_token:
            raise HTTPException(
                status_code=503,
                detail=(
                    "本服务未配置 API_TOKEN，写入端点已关闭。"
                    "请设置环境变量 API_TOKEN 后重启（读取端点不受影响）。"
                ),
            )
        expected = f"Bearer {settings.api_token}"
        if not secrets.compare_digest(authorization or "", expected):
            raise HTTPException(status_code=401, detail="API_TOKEN 不正确")

    app = FastAPI(
        title="App Review Insights API",
        version=API_VERSION,
        summary="把评论变成有证据支撑的产品发现、需求与测试用例",
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """存活探针：进程还在就 200。刻意不检查依赖——依赖抖动不该引发重启。"""
        return {"status": "ok"}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        """与 worker 同源的指标：阶段与模型耗时都读同一份检查点库。"""
        state = HealthState(
            duration_stats=lambda: {
                "stage": repository.stage_timing_summary(),
                "model": repository.model_latency_summary(),
            },
            queue_stats=lambda: {
                "depth": repository.queue_depth(),
                "oldest_wait_seconds": repository.oldest_pending_seconds(),
                "executor_seen": repository.executor_seen_within(HEARTBEAT_INTERVAL_SECONDS * 3),
            },
        )
        return state.render_prometheus()

    @app.get("/v1/queue", response_model=QueueState)
    def queue_state() -> QueueState:
        return QueueState(
            depth=repository.queue_depth(),
            executor_seen=repository.executor_seen_within(HEARTBEAT_INTERVAL_SECONDS * 3),
        )

    @app.get("/v1/runs", response_model=list[RunSummary])
    def list_runs(limit: Annotated[int, Query(ge=1, le=200)] = 20) -> list[RunSummary]:
        return [_summary(run, repository) for run in repository.list_runs()[:limit]]

    @app.post(
        "/v1/runs",
        status_code=202,
        response_model=RunSummary,
        dependencies=[Depends(require_token)],
    )
    def submit_run(payload: RunSubmission) -> RunSummary:
        """提交一次分析：**入队并立刻返回**，执行交给 worker。

        202 而不是 200 是诚实的：这次调用只保证"已受理"。轮询
        `GET /v1/runs/{id}` 或订阅 `/events?stream=1` 拿进度。
        """
        depth = repository.queue_depth()
        if depth >= settings.api_max_queue_depth:
            # 背压：与其让请求排到天荒地老，不如现在就如实拒绝
            raise HTTPException(
                status_code=429,
                detail=f"队列积压已达上限（{depth}/{settings.api_max_queue_depth}），请稍后再试",
            )

        request = AnalysisRequest(
            source_type=SourceType.JSON if payload.reviews else SourceType.ONLINE,
            analysis_goal=payload.analysis_goal,
            app_url=payload.app_url,
            review_limit=payload.review_limit,
        )
        imported = None
        if payload.reviews:
            raw = json.dumps(payload.reviews, ensure_ascii=False).encode("utf-8")
            imported = import_reviews(raw, "api-submission.json", app_id="api")
        orchestrator = AnalysisOrchestrator(pipeline)
        try:
            run = orchestrator.enqueue(request, imported_reviews=imported)
        except Exception as exc:  # ConcurrentRunError 等域内错误一律转成 409
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _summary(run, repository)

    @app.get("/v1/runs/{run_id}", response_model=RunDetail)
    def get_run(run_id: str) -> RunDetail:
        run = _run_or_404(repository, run_id)
        return RunDetail(
            **_summary(run, repository).model_dump(),
            usage=repository.model_usage_summary(run_id=run_id),
        )

    @app.get("/v1/runs/{run_id}/events")
    def get_events(
        run_id: str,
        stream: Annotated[bool, Query(description="为真时返回 SSE 事件流")] = False,
    ) -> Any:
        _run_or_404(repository, run_id)
        events = repository.list_events(run_id)
        if not stream:
            return [_event_payload(event) for event in events]

        def publisher() -> Iterator[str]:
            seen = 0
            deadline = time.monotonic() + EVENT_STREAM_MAX_SECONDS
            while time.monotonic() < deadline:
                for event in repository.list_events(run_id)[seen:]:
                    seen += 1
                    data = json.dumps(_event_payload(event), ensure_ascii=False)
                    yield f"event: stage\ndata: {data}\n\n"
                if repository.get_run(run_id).status not in ACTIVE_STATUSES:
                    # 跑到终态就主动收尾，不等客户端断开
                    yield "event: done\ndata: {}\n\n"
                    return
                time.sleep(EVENT_POLL_SECONDS)
            yield "event: timeout\ndata: {}\n\n"

        return StreamingResponse(publisher(), media_type="text/event-stream")

    @app.get("/v1/runs/{run_id}/result")
    def get_result(run_id: str) -> dict[str, Any]:
        _run_or_404(repository, run_id)
        return {stage.value: repository.get_output(run_id, stage) for stage in RESULT_STAGES}

    @app.post(
        "/v1/runs/{run_id}/cancel",
        status_code=202,
        dependencies=[Depends(require_token)],
    )
    def cancel_run(run_id: str) -> dict[str, Any]:
        """请求取消。**协作式**：执行者在下一个阶段边界才会停，不是立即杀进程。"""
        run = _run_or_404(repository, run_id)
        accepted = AnalysisOrchestrator(pipeline).cancel(run_id)
        if not accepted:
            raise HTTPException(
                status_code=409,
                detail=f"这条运行当前状态为 {run.status.value}，没有可打断的执行",
            )
        return {
            "run_id": run_id,
            "cancel_requested": True,
            "note": "取消会在下一个阶段边界生效；已完成的阶段全部保留，可随时续跑。",
        }

    return app
