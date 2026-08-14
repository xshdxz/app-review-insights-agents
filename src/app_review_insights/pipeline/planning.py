import json
from typing import Any

from app_review_insights.llm.prompts import PLANNING_SYSTEM_PROMPT
from app_review_insights.llm.schemas import RequirementPlanResult
from app_review_insights.models import EvidenceStatus, Finding, Requirement

_COMPLEXITY_COST = {"low": 1, "medium": 2, "high": 3}


def build_requirements(
    provider: Any,
    findings: list[Finding],
    analysis_goal: str,
    total_reviews: int,
) -> list[Requirement]:
    eligible = [
        finding
        for finding in findings
        if finding.evidence_status != EvidenceStatus.REJECTED
    ]
    if not eligible:
        return []

    result = provider.generate(
        PLANNING_SYSTEM_PROMPT,
        json.dumps(
            {
                "analysis_goal": analysis_goal,
                "findings": [item.model_dump(mode="json") for item in eligible],
            },
            ensure_ascii=False,
        ),
        RequirementPlanResult,
    )
    finding_index = {finding.finding_id: finding for finding in eligible}
    requirements: list[Requirement] = []

    for draft in result.requirements:
        linked_ids = list(
            dict.fromkeys(
                finding_id
                for finding_id in draft.finding_ids
                if finding_id in finding_index
            )
        )
        linked = [finding_index[finding_id] for finding_id in linked_ids]
        if not linked:
            continue

        source_review_ids = list(
            dict.fromkeys(
                review_id
                for finding in linked
                for review_id in finding.supporting_review_ids
            )
        )
        confidence = sum(finding.confidence for finding in linked) / len(linked)
        frequency = len(source_review_ids) / max(total_reviews, 1)
        priority_score = round(
            draft.impact
            * frequency
            * confidence
            * 100
            / _COMPLEXITY_COST[draft.complexity],
            2,
        )
        has_assumption = any(
            finding.evidence_status == EvidenceStatus.ASSUMPTION
            for finding in linked
        )
        requirements.append(
            Requirement(
                requirement_id="pending",
                finding_ids=linked_ids,
                title=draft.title,
                user_problem=draft.user_problem,
                objective=draft.objective,
                scope=draft.scope,
                non_goals=draft.non_goals,
                functional_rules=draft.functional_rules,
                edge_cases=draft.edge_cases,
                acceptance_criteria=draft.acceptance_criteria,
                success_metrics=draft.success_metrics,
                impact=draft.impact,
                complexity=draft.complexity,
                priority_score=priority_score,
                target_version=(
                    "Future" if has_assumption else draft.proposed_version
                ),
                source_review_ids=source_review_ids,
                assumptions=draft.assumptions,
            )
        )

    requirements.sort(key=lambda item: item.priority_score, reverse=True)
    return [
        requirement.model_copy(
            update={"requirement_id": f"REQ-{index:03d}"}
        )
        for index, requirement in enumerate(requirements[:10], start=1)
    ]
