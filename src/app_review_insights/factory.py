"""依赖装配：从 Settings 构建流水线服务与 Agent 栈（单一装配点）。"""

from __future__ import annotations

from app_review_insights.collectors import AppStoreCollector
from app_review_insights.config import Settings
from app_review_insights.llm import DeepSeekProvider
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
from app_review_insights.storage import RunRepository


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
