from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Callable
from pathlib import Path

import streamlit as st

from app_review_insights.config import Settings, load_settings
from app_review_insights.errors import (
    AppReviewInsightsError,
    ConcurrentRunError,
    InputDataError,
)
from app_review_insights.factory import (
    build_pipeline_services as _build_pipeline_services,
)
from app_review_insights.input_parsing import import_reviews, parse_app_store_url
from app_review_insights.models import (
    AnalysisRequest,
    RunRecord,
    RunStatus,
    SourceType,
    Stage,
    StageEvent,
)
from app_review_insights.pipeline.orchestrator import (
    AnalysisOrchestrator,
    PipelineServices,
)
from app_review_insights.storage import RunRepository
from app_review_insights.storage.cache import (
    DEMO_ANALYSIS_GOAL,
    SAMPLE_PATH,
    build_demo_downloads,
    build_downloads,
    load_demo_run,
)
from app_review_insights.ui.components import (
    format_event_message,
    render_model_status,
    render_provenance_legend,
    render_result_payloads,
    render_result_tabs,
    render_run_status,
)

EventWriter = Callable[[object], None]

_SOURCE_OPTIONS = ("在线采集", "JSON 导入", "CSV 导入")
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEMO_PATH = _PROJECT_ROOT / "data" / "cache" / "demo-run.json"
_UPLOAD_DIR = _PROJECT_ROOT / "data" / "uploads"


class _RecoveredUpload:
    """Upload-like object restored from a saved file after a page reload."""

    def __init__(self, path: Path):
        self._path = path
        self.name = path.name

    def getvalue(self) -> bytes:
        return self._path.read_bytes()


_RESTORED_MARK = "_restored_from_params"


def _restore_inputs_from_query_params() -> None:
    """Restore input widgets from URL query params after a browser reload.

    Runs exactly once per session (a reload creates a fresh session); later
    reruns keep the user's current input untouched. Using a marker instead of
    per-key checks guarantees the mode is forced back even if some stale
    session values survive a reload.
    """
    if st.session_state.get(_RESTORED_MARK):
        return
    st.session_state[_RESTORED_MARK] = True
    params = st.query_params
    if "url" in params:
        st.session_state["input-app-url"] = params["url"]
    if "goal" in params:
        st.session_state["input-goal"] = params["goal"]
    if "mode" in params and params["mode"] in _SOURCE_OPTIONS:
        st.session_state["source-mode"] = params["mode"]
    if "limit" in params and params["limit"].isdigit():
        mode = st.session_state.get("source-mode", "在线采集")
        st.session_state[f"review-limit-{mode}"] = int(params["limit"])
    if "upload" in params:
        saved = _UPLOAD_DIR / str(params["upload"])
        if saved.exists():
            st.session_state["restored-upload"] = str(saved)


def _sync_inputs_to_query_params(
    source_label: str,
    app_url: str,
    goal: str,
    review_limit: int,
    upload,
    *,
    demo_replay: bool = False,
) -> None:
    """把当前输入写进 URL 查询参数，用于浏览器刷新后恢复。

    三种情况下不写：
    - 演示模式：输入被锁定为样例，恢复机制没有意义；带参数进来的访客（例如别人分享
      的普通模式链接）会把参数继续挂在地址栏上外传，所以就地清干净；
    - 空值：空参数只让链接变长，url= 这类空值尤其容易被误读为「有一个地址」；
    - 条件写入的键在条件不成立时**删除**而不仅仅是不写：旧值留在键里会让用户已经
      清空的内容在刷新后复活，分享出去的链接也仍然带着他已经删掉的内容。
    """
    params = st.query_params
    if demo_replay:
        params.clear()
        return
    params["mode"] = source_label
    if source_label == "在线采集" and app_url:
        params["url"] = app_url
    else:
        params.pop("url", None)
    if goal:
        params["goal"] = goal
    else:
        params.pop("goal", None)
    params["limit"] = str(review_limit)
    restored = st.session_state.get("restored-upload")
    upload_name = Path(restored).name if restored else None
    if not upload_name and upload is not None and upload.name:
        upload_name = upload.name
    if upload_name:
        params["upload"] = upload_name
    else:
        params.pop("upload", None)


def _persist_upload(upload) -> None:
    """Save an uploaded file so it survives a browser reload."""
    if upload is None or not getattr(upload, "name", ""):
        return
    restored = st.session_state.get("restored-upload")
    if restored and Path(restored).name == upload.name:
        return
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = _UPLOAD_DIR / f"{upload.name}"
    if not target.exists():
        target.write_bytes(upload.getvalue())
    st.session_state["restored-upload"] = str(target)


def build_services(use_fake_provider: bool = False) -> PipelineServices:
    return _build_pipeline_services(load_settings(), use_fake_provider=use_fake_provider)


def _build_services_or_degrade() -> tuple[PipelineServices, str | None]:
    """装配失败不抛到 UI：退化成无模型的流水线，由页面说明原因。

    配置错误（例如 DEMO_MODE=live 却没密钥）是用户能在页面上看懂并修好的，
    不该以整页 traceback 的形式呈现——这是设计约束「降级不崩溃」的直接落实。
    """
    try:
        return build_services(), None
    except AppReviewInsightsError as exc:
        fallback = _build_pipeline_services(load_settings(), use_fake_provider=True)
        return fallback, str(exc)


def _initialize_session_state(repository: RunRepository) -> None:
    if "run_id" not in st.session_state:
        runs = repository.list_runs()
        st.session_state["run_id"] = runs[0].run_id if runs else None


def _model_config_fingerprint(settings: Settings) -> str:
    payload = "\x1f".join(
        (
            settings.effective_model_api_key,
            settings.model_provider,
            settings.model_name,
            settings.model_base_url,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _model_state(settings: Settings, session_state=None) -> str:
    state = st.session_state if session_state is None else session_state
    if not settings.model_enabled:
        return "disabled"
    if not settings.effective_model_api_key:
        return "missing"
    verified_fingerprint = state.get("model_verified_fingerprint")
    if verified_fingerprint and hmac.compare_digest(
        verified_fingerprint,
        _model_config_fingerprint(settings),
    ):
        return "verified"
    return "configured"


def _model_key_source(settings: Settings) -> str | None:
    if not settings.effective_model_api_key:
        return None
    if os.environ.get("DEEPSEEK_API_KEY"):
        return "进程环境变量"
    return "项目 .env"


def _record_model_success(
    repository: RunRepository,
    run_id: str,
    settings: Settings,
    session_state=None,
) -> None:
    if repository.get_output(run_id, Stage.ANALYZE_BATCHES, batch_index=0) is not None:
        state = st.session_state if session_state is None else session_state
        state["model_verified_fingerprint"] = _model_config_fingerprint(settings)


_MODEL_KEY_CHECK = "model_key_check"


def _validate_model_key(settings: Settings, session_state=None) -> str | None:
    """One-shot lightweight key validation (cached per session).

    Returns "valid", "invalid" (authentication failure) or "unavailable"
    (network/service issue), or None when no validation is needed.
    """
    from app_review_insights.errors import RecoverableModelError
    from app_review_insights.llm.provider import DeepSeekProvider
    from app_review_insights.llm.schemas import BatchAnalysisResult

    state = st.session_state if session_state is None else session_state
    cached = state.get(_MODEL_KEY_CHECK)
    if cached:
        return cached
    if not settings.model_enabled or not settings.effective_model_api_key:
        return None

    try:
        provider = DeepSeekProvider.from_settings(settings)
        provider.max_retries = 0
        provider.retry_delays = ()
        provider.generate(
            "你是连通性测试助手。只输出空结果，不解释。",
            "返回空结果。",
            BatchAnalysisResult,
        )
        result = "valid"
    except RecoverableModelError as exc:
        message = str(exc)
        if "401" in message or "Authentication" in message or "invalid" in message.lower():
            result = "invalid"
        else:
            result = "unavailable"
    except Exception:
        result = "unavailable"
    state[_MODEL_KEY_CHECK] = result
    return result


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
    *,
    demo_replay: bool = False,
) -> AnalysisRequest:
    if demo_replay:
        # 演示模式没有数据来源可选（表单里被锁成「演示样例」），走 JSON 分支：
        # 录制脚本就是用 SourceType.JSON 录的，评论集由 _prepare_imported_reviews
        # 直接读样例，不再经过上传校验。
        return AnalysisRequest(
            source_type=SourceType.JSON,
            app_url=None,
            analysis_goal=analysis_goal.strip(),
            review_limit=review_limit,
        )
    source_type = _source_type(source_label, upload_name)
    normalized_url = app_url.strip()
    if source_type == SourceType.ONLINE:
        if not normalized_url:
            raise InputDataError("在线采集需要填写 App Store URL。")
        parse_app_store_url(normalized_url)
    return AnalysisRequest(
        source_type=source_type,
        app_url=normalized_url or None,
        analysis_goal=analysis_goal.strip(),
        review_limit=review_limit,
    )


def _max_duration_seconds() -> float | None:
    """整轮运行上限；0 或未配置表示不限制。"""
    return load_settings().run_max_duration_seconds or None


def _run_analysis(
    services: PipelineServices,
    request: AnalysisRequest,
    imported_reviews,
    event_writer: EventWriter | None = None,
) -> RunRecord:
    return AnalysisOrchestrator(
        services,
        on_event=event_writer,
        max_duration_seconds=_max_duration_seconds(),
    ).start(
        request,
        imported_reviews=imported_reviews,
    )


def _resume_analysis(
    services: PipelineServices,
    run_id: str,
    event_writer: EventWriter | None = None,
) -> RunRecord:
    return AnalysisOrchestrator(
        services,
        on_event=event_writer,
        max_duration_seconds=_max_duration_seconds(),
    ).resume(run_id)


def _prepare_imported_reviews(
    request: AnalysisRequest,
    upload,
    *,
    demo_replay: bool = False,
):
    if demo_replay:
        # 演示模式忽略上传与 URL：录制件是在仓库样例上录的，
        # 也**不能**按 review_limit 截断——回放靠输入指纹一致才命中。
        return import_reviews(SAMPLE_PATH.read_bytes(), SAMPLE_PATH.name, app_id="demo")
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


def _render_input_form(settings: Settings, model_ready: bool, *, demo_replay: bool = False):
    with st.container(border=True):
        st.subheader("新建审阅档案", anchor=False)
        app_url = ""
        upload = None
        if demo_replay:
            # 演示模式锁死数据来源：回放命中的前提是输入与录制时逐字一致
            st.selectbox(
                "数据来源",
                ["演示样例"],
                disabled=True,
                help="演示模式回放的是样例上的运行结果；换成别的输入将无法命中录制。",
                key="source-mode-locked",
            )
            st.caption(f"样例文件：{SAMPLE_PATH.name}")
            source_label = "演示样例"
        else:
            source_label = st.segmented_control(
                "数据来源",
                _SOURCE_OPTIONS,
                default="在线采集",
                required=True,
                width="stretch",
                key="source-mode",
            )
        # 控件直接渲染（带 key 即时写入 session_state），
        # 页面刷新后输入保持，避免回到默认表单。
        if source_label == "在线采集":
            app_url = st.text_input(
                "App 地址（URL）",
                placeholder="https://apps.apple.com/us/app/example/id123456789",
                help="在线采集支持任意区 App Store（区域取自链接中的区号，如 /us/、/gb/）。",
                key="input-app-url",
            )
        if demo_replay:
            # 分析目标同样进 prompt：锁成录制时那一句，且用独立 key——
            # 否则 URL 参数（input-goal）能把它改成别的值，回放就全量未命中了。
            analysis_goal = st.text_area(
                "分析目标",
                value=DEMO_ANALYSIS_GOAL,
                height=100,
                disabled=True,
                help="演示模式回放的是这句话下的运行结果；改动目标将无法命中录制。",
                key="goal-locked",
            )
        else:
            analysis_goal = st.text_area(
                "分析目标",
                value=DEMO_ANALYSIS_GOAL,
                height=100,
                key="input-goal",
            )
        maximum_limit = 1000
        review_limit = st.slider(
            "评论数量",
            min_value=100,
            max_value=maximum_limit,
            value=min(settings.default_review_limit, maximum_limit),
            step=1,
            # 演示模式锁死：回放不按 review_limit 截断，能拖动却什么都不发生的控件
            # 比禁用的控件更糟——它会教访客「这个应用会忽略我的输入」。
            disabled=demo_replay,
            help=(
                "演示模式回放的是录制时那一次运行，评论条数不参与回放，因此锁定。"
                if demo_replay
                else (
                    "在线采集与文件导入统一支持 100–1000 条；"
                    "在线采集实际条数以 Apple 接口为准，不足部分会在运行局限中披露。"
                )
            ),
            key=f"review-limit-{source_label}",
        )
        if source_label in ("JSON 导入", "CSV 导入"):
            suffix = "json" if source_label == "JSON 导入" else "csv"
            upload = st.file_uploader(
                f"{suffix.upper()} 评论文件",
                type=[suffix],
                help="字段格式沿用项目的数据格式约定，并兼容页面导出的中文表头。",
                key=f"review-upload-{suffix}",
            )
            _persist_upload(upload)
            if upload is None and st.session_state.get("restored-upload"):
                restored = Path(st.session_state["restored-upload"])
                st.info(f"已恢复上传文件：{restored.name}")
                upload = _RecoveredUpload(restored)
        submitted = st.button(
            "开始分析",
            type="primary",
            icon=":material/play_arrow:",
            disabled=not model_ready,
            width="stretch",
            key="start-analysis",
        )
    _sync_inputs_to_query_params(
        source_label, app_url, analysis_goal, review_limit, upload, demo_replay=demo_replay
    )
    return submitted, source_label, app_url, analysis_goal, review_limit, upload


def _render_downloads(downloads: dict[str, bytes], *, key_prefix: str) -> None:
    st.subheader("下载交付物", anchor=False)
    with st.container(horizontal=True, gap="small"):
        st.download_button(
            "下载清洗评论 JSON",
            downloads["cleaned_reviews"],
            "cleaned-reviews.json",
            mime="application/json",
            icon=":material/download:",
            on_click="ignore",
            key=f"{key_prefix}-cleaned",
        )
        st.download_button(
            "下载产品需求（PRD）JSON",
            downloads["prd"],
            "prd.json",
            mime="application/json",
            icon=":material/download:",
            on_click="ignore",
            key=f"{key_prefix}-prd",
        )
        st.download_button(
            "下载测试用例 CSV",
            downloads["test_cases"],
            "test-cases.csv",
            mime="text/csv",
            icon=":material/download:",
            on_click="ignore",
            key=f"{key_prefix}-tests",
        )
        st.download_button(
            "下载证据链 CSV",
            downloads["traceability"],
            "traceability.csv",
            mime="text/csv",
            icon=":material/download:",
            on_click="ignore",
            key=f"{key_prefix}-traceability",
        )


def _render_demo_archive() -> None:
    try:
        demo = load_demo_run(_DEMO_PATH)
        run = RunRecord.model_validate(demo["run"])
        events = [StageEvent.model_validate(item) for item in demo.get("events", [])]
    except (KeyError, OSError, ValueError) as exc:
        st.error(f"无法读取历史演示缓存：{exc}", icon=":material/database_off:")
        return

    st.warning(
        "当前展示历史缓存演示，不是本次实时分析；结果仅用于离线演示界面与证据链。",
        icon=":material/history:",
    )
    left, right = st.columns([3, 1], gap="large", vertical_alignment="top")
    with left:
        with st.container(border=True):
            st.subheader("历史审阅档案", anchor=False)
            st.caption(
                f"生成时间：{demo['collected_at']} · 模型：{demo.get('model_name', '未记录')}"
            )
            st.caption("数据方式：人工样例 JSON（非 App Store 实时采集）")
            st.write(f"参考 App（仅作为演示上下文）：{demo.get('source_app_url', '未记录')}")
        render_result_payloads(run, demo["result"], "historical-demo", events)
        _render_downloads(build_demo_downloads(demo), key_prefix="demo")

    with right:
        with st.container(border=True):
            st.subheader("演示状态", anchor=False)
            st.badge("历史缓存", color="orange", icon=":material/history:")
            st.badge("非实时", color="gray", icon=":material/cloud_off:")
            st.caption(f"运行 ID：`{run.run_id}`")
            st.write("实时模型调用：`未执行`")
            st.write("缓存标签校验：`已通过`")


def main() -> None:
    st.set_page_config(
        page_title="证据审阅工作台 · App Review Insights",
        page_icon=":material/fact_check:",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    settings = load_settings()
    services, wiring_error = _build_services_or_degrade()
    # 计划书规定：按钮禁用条件 = 显式禁用 或 未配置密钥；
    # 密钥已填写（无论有效无效）即可开始，无效密钥在模型环节失败后
    # 保留检查点，换回有效密钥后同一 run_id 续跑。
    # 演示模式（回放）同样可以开始——这是无密钥部署的默认形态。
    # 装配失败（wiring_error）时流水线是无模型的降级形态：此时即使 model_available
    # 为真（例如 DEMO_MODE=replay 但录制件缺失），点下去也只会停在
    # 「请先配置 DEEPSEEK_API_KEY」，而真因在横幅里——按钮必须禁用。
    demo_replay = settings.demo_replay_active and settings.demo_replay_path.exists()
    model_ready = (settings.model_available or demo_replay) and wiring_error is None
    _initialize_session_state(services.repository)
    # 浏览器刷新会清空 session_state：从 URL 参数恢复输入，保持中断前的页面。
    _restore_inputs_from_query_params()
    model_state = _model_state(settings)

    if wiring_error:
        st.warning(f"模型装配未完成：{wiring_error}", icon=":material/key_off:")

    st.title("证据审阅工作台", anchor=False)
    st.caption(
        "编辑部档案 · 将 App Store 评论整理为可核验的问题发现、产品需求（PRD）、测试用例与证据链"
    )
    render_provenance_legend()
    render_model_status(
        model_state,
        _model_key_source(settings),
    )
    if demo_replay:
        st.warning(
            "演示模式：回放一次真实运行的模型输出，不调用外部 API，输入固定为仓库自带样例。",
            icon=":material/replay:",
        )
    demo_mode = st.toggle(
        "查看历史缓存演示",
        help="无需模型密钥；始终明确标记为历史缓存和非实时结果。",
        key="demo-mode",
    )
    if demo_mode:
        _render_demo_archive()
        return

    # 已配置密钥但未验证时，做一次轻量连通性验证：
    # 无效密钥立即提示，而不是等到分析时才暴露；按钮保持可点
    # （计划书：无效密钥进入等待恢复后可续跑）。
    if model_state == "configured":
        key_check = _validate_model_key(settings)
        if key_check == "valid":
            st.session_state["model_verified_fingerprint"] = _model_config_fingerprint(settings)
        elif key_check == "invalid":
            st.warning(
                "模型状态：DeepSeek 密钥无效（认证失败）。"
                "开始分析后流程会在模型环节暂停并保留进度；"
                "修正 .env 中的 DEEPSEEK_API_KEY 后，点击“继续分析”完成运行。",
                icon=":material/key_off:",
            )
        elif key_check == "unavailable":
            st.warning(
                "模型状态：暂时无法连接模型服务。仍可尝试分析，失败后可从检查点继续。",
                icon=":material/cloud_off:",
            )

    left, right = st.columns([3, 1], gap="large", vertical_alignment="top")
    with left:
        submitted, source_label, app_url, goal, limit, upload = _render_input_form(
            settings, model_ready, demo_replay=demo_replay
        )
        st.download_button(
            "下载样例评论 JSON",
            SAMPLE_PATH.read_bytes(),
            "reviews-sample.json",
            mime="application/json",
            icon=":material/download:",
            on_click="ignore",
            key="sample-reviews-download",
        )
        if submitted:
            if not model_ready:
                st.error("实时模型不可用，请先配置 MODEL_API_KEY。")
            else:
                try:
                    request = _build_request(
                        source_label,
                        app_url,
                        goal,
                        limit,
                        upload.name if upload else None,
                        demo_replay=demo_replay,
                    )
                    imported_reviews = _prepare_imported_reviews(
                        request, upload, demo_replay=demo_replay
                    )
                except (InputDataError, ConcurrentRunError, ValueError) as exc:
                    st.error(str(exc), icon=":material/input:")
                else:
                    live_status = st.status("正在执行分析工作流", expanded=True)
                    run = _run_analysis(
                        services,
                        request,
                        imported_reviews,
                        event_writer=lambda event: live_status.write(
                            format_event_message(event.message)
                        ),
                    )
                    st.session_state["run_id"] = run.run_id
                    _record_model_success(services.repository, run.run_id, settings)
                    _update_live_status(live_status, run)

        run_id = st.session_state.get("run_id")
        if run_id:
            run = services.repository.get_run(run_id)
            events = services.repository.list_events(run_id)
            st.subheader("当前档案", anchor=False)
            render_result_tabs(services.repository, run_id, events)
            _render_downloads(
                build_downloads(services.repository, run_id),
                key_prefix=f"run-{run_id}",
            )
        else:
            with st.container(border=True):
                st.subheader("当前档案", anchor=False)
                st.caption("尚未创建分析运行。点击“开始分析”后，各阶段输出会保存在档案库中。")

    with right:
        run_id = st.session_state.get("run_id")
        if run_id:
            run = services.repository.get_run(run_id)
            events = services.repository.list_events(run_id)
            pending = st.session_state.get("pending_resume")
            if pending == run.run_id and run.status == RunStatus.WAITING:
                # 模型问题已解决并开始恢复：不展示旧的失败状态与错误文本。
                resume_status = st.status(
                    "模型已恢复，正在从检查点继续…",
                    expanded=True,
                )
                st.caption(f"运行 ID：`{run.run_id}`")
                resumed = _resume_analysis(
                    services,
                    run.run_id,
                    event_writer=lambda event: resume_status.write(
                        format_event_message(event.message)
                    ),
                )
                st.session_state.pop("pending_resume", None)
                st.session_state["run_id"] = resumed.run_id
                _record_model_success(services.repository, resumed.run_id, settings)
                _update_live_status(resume_status, resumed)
                st.rerun()
            else:
                render_run_status(
                    run,
                    events,
                    usage=services.repository.model_usage_summary(run_id=run.run_id),
                )
                # 超时停止的运行同样停在检查点上，可以续跑
                if run.status in (RunStatus.WAITING, RunStatus.TIMED_OUT):
                    resume = st.button(
                        "继续分析",
                        type="primary",
                        icon=":material/resume:",
                        disabled=not model_ready,
                        width="stretch",
                    )
                    if not model_ready:
                        st.caption("配置模型密钥后，可沿用同一运行 ID 从检查点继续。")
                    if resume:
                        st.session_state["pending_resume"] = run.run_id
                        st.rerun()
        else:
            with st.container(border=True):
                st.subheader("运行状态", anchor=False)
                st.caption("创建运行后，这里会显示覆盖率、阶段、批次、事件和错误。")
