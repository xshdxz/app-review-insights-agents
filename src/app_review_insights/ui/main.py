from __future__ import annotations

from collections.abc import Callable

import streamlit as st

from app_review_insights.collectors import AppStoreCollector
from app_review_insights.config import Settings, load_settings
from app_review_insights.errors import InputDataError
from app_review_insights.input_parsing import import_reviews, parse_app_store_url
from app_review_insights.llm import DeepSeekProvider
from app_review_insights.models import AnalysisRequest, RunRecord, RunStatus, SourceType
from app_review_insights.pipeline.analyze import analyze_batch, consolidate_findings
from app_review_insights.pipeline.orchestrator import (
    AnalysisOrchestrator,
    PipelineServices,
)
from app_review_insights.pipeline.planning import build_requirements
from app_review_insights.pipeline.test_generation import generate_test_cases
from app_review_insights.pipeline.traceability import validate_traceability
from app_review_insights.pipeline.validate import validate_finding_drafts
from app_review_insights.storage import RunRepository
from app_review_insights.ui.components import (
    render_model_status,
    render_provenance_legend,
    render_result_tabs,
    render_run_status,
)

EventWriter = Callable[[object], None]

_SOURCE_OPTIONS = ("在线采集", "JSON 导入", "CSV 导入")


def build_services(use_fake_provider: bool = False) -> PipelineServices:
    settings = load_settings()
    repository = RunRepository(settings.database_path)
    common = {
        "repository": repository,
        "collector": AppStoreCollector(),
        "finding_validator": validate_finding_drafts,
        "traceability_validator": validate_traceability,
        "batch_size": settings.batch_review_limit,
        "batch_max_characters": settings.batch_max_characters,
    }
    if use_fake_provider or not settings.deepseek_api_key:
        return PipelineServices(batch_analyzer=None, **common)

    provider = DeepSeekProvider.from_settings(settings)
    return PipelineServices(
        batch_analyzer=lambda reviews, goal: analyze_batch(provider, reviews, goal),
        consolidator=lambda results, goal: consolidate_findings(provider, results, goal),
        requirement_builder=lambda findings, goal, total: build_requirements(
            provider, findings, goal, total
        ),
        test_case_builder=lambda requirements: generate_test_cases(provider, requirements),
        **common,
    )


def _initialize_session_state(repository: RunRepository) -> None:
    if "run_id" not in st.session_state:
        runs = repository.list_runs()
        st.session_state["run_id"] = runs[0].run_id if runs else None


def _source_type(source_label: str, upload_name: str | None) -> SourceType:
    if source_label == "在线采集":
        return SourceType.ONLINE
    if upload_name is None:
        raise InputDataError("请选择与导入模式对应的 JSON 或 CSV 评论文件。")

    suffix = upload_name.lower().rsplit(".", maxsplit=1)[-1]
    expected_suffix = "json" if source_label == "JSON 导入" else "csv"
    if suffix != expected_suffix:
        raise InputDataError(f"当前选择了 {source_label}，请上传 .{expected_suffix} 文件。")
    return SourceType.JSON if suffix == "json" else SourceType.CSV


def _build_request(
    source_label: str,
    app_url: str,
    analysis_goal: str,
    review_limit: int,
    upload_name: str | None,
) -> AnalysisRequest:
    source_type = _source_type(source_label, upload_name)
    normalized_url = app_url.strip()
    if source_type == SourceType.ONLINE:
        if not normalized_url:
            raise InputDataError("在线采集需要填写美国区 App Store URL。")
        parse_app_store_url(normalized_url)
        if review_limit > 500:
            raise InputDataError("在线采集最多支持 500 条评论，请降低数量或改用文件导入。")
    return AnalysisRequest(
        source_type=source_type,
        app_url=normalized_url or None,
        analysis_goal=analysis_goal.strip(),
        review_limit=review_limit,
    )


def _run_analysis(
    services: PipelineServices,
    request: AnalysisRequest,
    imported_reviews,
    event_writer: EventWriter | None = None,
) -> RunRecord:
    return AnalysisOrchestrator(services, on_event=event_writer).start(
        request,
        imported_reviews=imported_reviews,
    )


def _resume_analysis(
    services: PipelineServices,
    run_id: str,
    event_writer: EventWriter | None = None,
) -> RunRecord:
    return AnalysisOrchestrator(services, on_event=event_writer).resume(run_id)


def _prepare_imported_reviews(request: AnalysisRequest, upload):
    if request.source_type == SourceType.ONLINE:
        return None
    if upload is None:
        raise InputDataError("请选择与导入模式对应的评论文件。")
    reviews = import_reviews(upload.getvalue(), upload.name, app_id="imported")
    return reviews[: request.review_limit]


def _update_live_status(status, run: RunRecord) -> None:
    if run.status == RunStatus.COMPLETED:
        status.update(label="分析完成", state="complete", expanded=False)
    elif run.status == RunStatus.WAITING:
        status.update(label="进度已保存，等待模型恢复", state="error", expanded=True)
    elif run.status in (RunStatus.FAILED, RunStatus.PARTIAL):
        status.update(label="运行未完整完成，请查看状态与限制", state="error")
    else:
        status.update(label="运行已保存", state="complete", expanded=False)


def _render_input_form(settings: Settings, model_ready: bool):
    with st.form("analysis-input", border=True, enter_to_submit=False):
        st.subheader("新建审阅档案", anchor=False)
        source_label = st.segmented_control(
            "数据来源",
            _SOURCE_OPTIONS,
            default="在线采集",
            required=True,
            width="stretch",
            key="source-mode",
        )
        app_url = st.text_input(
            "App URL",
            placeholder="https://apps.apple.com/us/app/example/id123456789",
            help="在线采集仅支持美国区 App Store；文件导入时可留空。",
        )
        analysis_goal = st.text_area(
            "分析目标",
            value="识别影响用户体验与产品增长的核心问题，并形成可追溯需求",
            height=100,
        )
        review_limit = st.slider(
            "评论数量",
            min_value=100,
            max_value=1000,
            value=settings.default_review_limit,
            step=100,
            help="Apple RSS 在线采集最多 500 条；文件导入可分析至 1000 条。",
        )
        upload = st.file_uploader(
            "评论文件",
            type=["json", "csv"],
            help="选择 JSON/CSV 导入时必填；字段格式沿用项目的数据格式约定。",
        )
        submitted = st.form_submit_button(
            "开始分析",
            type="primary",
            icon=":material/play_arrow:",
            disabled=not model_ready,
            width="stretch",
        )
    return submitted, source_label, app_url, analysis_goal, review_limit, upload


def main() -> None:
    st.set_page_config(
        page_title="证据审阅工作台 · App Review Insights",
        page_icon=":material/fact_check:",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    settings = load_settings()
    services = build_services()
    model_ready = services.batch_analyzer is not None
    _initialize_session_state(services.repository)

    st.title("证据审阅工作台", anchor=False)
    st.caption("编辑部档案 · 将 App Store 评论整理为可核验的 Findings、PRD、测试用例与证据链")
    render_provenance_legend()
    render_model_status(model_ready, settings.model_name)

    left, right = st.columns([3, 1], gap="large", vertical_alignment="top")
    with left:
        submitted, source_label, app_url, goal, limit, upload = _render_input_form(
            settings, model_ready
        )
        if submitted:
            if not model_ready:
                st.error("实时模型不可用，请先配置 DEEPSEEK_API_KEY。")
            else:
                try:
                    request = _build_request(
                        source_label,
                        app_url,
                        goal,
                        limit,
                        upload.name if upload else None,
                    )
                    imported_reviews = _prepare_imported_reviews(request, upload)
                except (InputDataError, ValueError) as exc:
                    st.error(str(exc), icon=":material/input:")
                else:
                    live_status = st.status("正在执行分析工作流", expanded=True)
                    run = _run_analysis(
                        services,
                        request,
                        imported_reviews,
                        event_writer=lambda event: live_status.write(event.message),
                    )
                    st.session_state["run_id"] = run.run_id
                    _update_live_status(live_status, run)

        run_id = st.session_state.get("run_id")
        if run_id:
            run = services.repository.get_run(run_id)
            events = services.repository.list_events(run_id)
            st.subheader("当前档案", anchor=False)
            render_result_tabs(services.repository, run_id, events)
        else:
            with st.container(border=True):
                st.subheader("当前档案", anchor=False)
                st.caption("尚未创建分析运行。提交表单后，各阶段输出会保存在档案库中。")

    with right:
        run_id = st.session_state.get("run_id")
        if run_id:
            run = services.repository.get_run(run_id)
            events = services.repository.list_events(run_id)
            render_run_status(run, events)
            if run.status == RunStatus.WAITING:
                resume = st.button(
                    "继续分析",
                    type="primary",
                    icon=":material/resume:",
                    disabled=not model_ready,
                    width="stretch",
                )
                if not model_ready:
                    st.caption("配置模型密钥后，可沿用同一 Run ID 从检查点继续。")
                if resume:
                    resume_status = st.status("正在从检查点继续", expanded=True)
                    resumed = _resume_analysis(
                        services,
                        run.run_id,
                        event_writer=lambda event: resume_status.write(event.message),
                    )
                    st.session_state["run_id"] = resumed.run_id
                    _update_live_status(resume_status, resumed)
                    st.rerun()
        else:
            with st.container(border=True):
                st.subheader("运行状态", anchor=False)
                st.caption("创建运行后，这里会显示覆盖率、stage、batch、事件和错误。")
