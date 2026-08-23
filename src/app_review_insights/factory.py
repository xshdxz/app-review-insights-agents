"""依赖装配：从 Settings 构建流水线服务与 Agent 栈（单一装配点）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app_review_insights.agent.orchestrator import AgentOrchestrator
from app_review_insights.agent.planner import Planner
from app_review_insights.agent.reviewer import Reviewer
from app_review_insights.agent.tools import (
    ToolRegistry,
    make_collect_reviews_tool,
    make_get_latest_report_tool,
    make_query_corpus_tool,
    make_run_analysis_tool,
    make_send_report_tool,
)
from app_review_insights.collectors import AppStoreCollector
from app_review_insights.config import Settings, load_settings
from app_review_insights.llm import DeepSeekProvider
from app_review_insights.models import Review, Stage
from app_review_insights.monitor.webhook import WebhookSender
from app_review_insights.pipeline.analyze import (
    analyze_batch,
    audit_finding_evidence,
    consolidate_findings,
)
from app_review_insights.pipeline.orchestrator import PipelineServices
from app_review_insights.pipeline.planning import build_requirements
from app_review_insights.pipeline.test_generation import generate_test_cases
from app_review_insights.pipeline.traceability import validate_traceability
from app_review_insights.pipeline.validate import validate_finding_drafts
from app_review_insights.rag.answer import RagAnswerer
from app_review_insights.rag.embeddings import EmbeddingStore
from app_review_insights.rag.indexer import CorpusIndexer
from app_review_insights.rag.retrieval import CorpusRetriever
from app_review_insights.rag.rewriter import QueryRewriter
from app_review_insights.storage import RunRepository
from app_review_insights.storage.agent_repository import AgentRepository


def build_pipeline_services(
    settings: Settings,
    use_fake_provider: bool = False,
) -> PipelineServices:
    repository = RunRepository(settings.database_path)
    common = {
        "repository": repository,
        "collector": AppStoreCollector(),
        "finding_validator": validate_finding_drafts,
        "traceability_validator": validate_traceability,
        "batch_size": settings.batch_review_limit,
        "batch_max_characters": settings.batch_max_characters,
    }
    if use_fake_provider or not settings.model_available:
        return PipelineServices(batch_analyzer=None, **common)

    provider = DeepSeekProvider.from_settings(settings)
    return PipelineServices(
        batch_analyzer=lambda reviews, goal: analyze_batch(provider, reviews, goal),
        consolidator=lambda results, goal, reviews: consolidate_findings(
            provider, results, goal, reviews
        ),
        evidence_auditor=lambda findings, reviews, goal: audit_finding_evidence(
            provider, findings, reviews, goal
        ),
        requirement_builder=lambda findings, goal, total: build_requirements(
            provider, findings, goal, total
        ),
        test_case_builder=lambda requirements: generate_test_cases(provider, requirements),
        **common,
    )


@dataclass
class AgentStack:
    """Agent 栈：装配完整的 Agent + RAG + 监控 + 采集能力。

    后半部分字段（webhook/rag/indexer/retriever/embedding_store/social）
    用 TYPE_CHECKING 守卫的协议类型做类型提示，避免运行时导入循环。
    """

    registry: ToolRegistry
    planner: Planner
    reviewer: Reviewer
    orchestrator: AgentOrchestrator
    agent_repository: AgentRepository
    webhook: Any = None
    rag: Any = None
    indexer: Any = None
    retriever: Any = None
    embedding_store: Any = None
    social: Any = None


def build_agent_stack(
    settings: Settings | None = None,
    use_fake_provider: bool = False,
) -> AgentStack:
    settings = settings or load_settings()
    pipeline_services = build_pipeline_services(settings, use_fake_provider=use_fake_provider)
    agent_repository = AgentRepository(settings.agent_db_path)

    provider = None
    if not use_fake_provider and settings.model_available:
        provider = DeepSeekProvider.from_settings(settings)

    # RAG 装配：语料索引/检索/问答；embedding 未配置时退化为纯 FTS5 检索
    # 优先本地模型（无需 API Key），其次 API，最后降级为纯 FTS5
    embedding_store = None
    if settings.embedding_enabled and settings.embedding_local_model_path:
        from app_review_insights.rag.embeddings import LocalEmbeddingStore
        embedding_store = LocalEmbeddingStore(settings.embedding_local_model_path)
    elif settings.embedding_enabled and settings.embedding_api_key:
        embedding_store = EmbeddingStore(
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            base_url=settings.embedding_base_url,
        )
    indexer = CorpusIndexer(agent_repository)
    retriever = CorpusRetriever(agent_repository, embedding_store=embedding_store)
    rewriter = QueryRewriter(provider)
    rag = RagAnswerer(provider, retriever, rewriter=rewriter)

    webhook = WebhookSender()

    from app_review_insights.collectors.social import SocialCollector

    social = SocialCollector(x_endpoint=settings.social_x_endpoint)

    tools = [
        make_run_analysis_tool(pipeline_services, indexer, embedding_store),
        make_collect_reviews_tool(pipeline_services.collector),
        make_query_corpus_tool(rag),
        make_get_latest_report_tool(agent_repository),
        make_send_report_tool(webhook, settings, agent_repository),
    ]
    registry = ToolRegistry(tools)
    planner = Planner(provider, registry)
    reviewer = Reviewer(
        provider,
        pipeline_services.repository,
        max_rounds=settings.agent_max_review_rounds,
    )
    orchestrator = AgentOrchestrator(
        planner=planner,
        registry=registry,
        reviewer=reviewer,
        agent_repository=agent_repository,
        max_review_rounds=settings.agent_max_review_rounds,
    )
    return AgentStack(
        registry=registry,
        planner=planner,
        reviewer=reviewer,
        orchestrator=orchestrator,
        agent_repository=agent_repository,
        webhook=webhook,
        rag=rag,
        indexer=indexer,
        retriever=retriever,
        embedding_store=embedding_store,
        social=social,
    )


def index_run_cleaned(
    stack: AgentStack,
    run_id: str,
    settings: Settings | None = None,
) -> int:
    """把一次已完成分析的清洗后评论写入语料库（供 RAG 检索）。

    读取的是流水线检查点库（DATABASE_PATH）中的 CLEAN 阶段输出。
    embedding 启用时同步为每条评论生成向量（批量、失败不影响语料本身）。
    """
    settings = settings or load_settings()
    run_repository = RunRepository(settings.database_path)
    cleaned = run_repository.get_output(run_id, Stage.CLEAN)
    if not cleaned:
        return 0
    reviews = [Review.model_validate(item) for item in cleaned.get("reviews", [])]
    count = stack.indexer.index_reviews(reviews)
    if stack.embedding_store is not None and reviews:
        try:
            vectors = stack.embedding_store.embed_texts(
                [review.content_original for review in reviews]
            )
            for review, vector in zip(reviews, vectors, strict=False):
                stack.agent_repository.upsert_embedding(review.review_id, vector)
        except Exception:  # noqa: BLE001 - 向量生成失败不影响 FTS 语料
            pass
    return count
