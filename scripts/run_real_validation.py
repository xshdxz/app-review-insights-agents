"""Run a real end-to-end analysis and verify the evidence chain.

Uses the same service wiring as the Streamlit workbench (`ui.main.build_services`)
with the real DeepSeek provider, so results match what the UI would produce.

Usage:
  python scripts/run_real_validation.py ^
      --app-url https://apps.apple.com/us/app/.../idXXXX ^
      --goal "重点分析订阅转化" --limit 200 --out output/real-run-name.json
  python scripts/run_real_validation.py ^
      --file data/samples/reviews-sample.json --goal "识别易用性问题" --out output/import-run.json

Exit code 0 when every verification check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app_review_insights.input_parsing import import_reviews
from app_review_insights.models import (
    AnalysisRequest,
    EvidenceStatus,
    RunStatus,
    SourceType,
    Stage,
)
from app_review_insights.ui.main import build_services


def _run_checks(payload: dict) -> list[dict]:
    checks: list[dict] = []
    run = payload["run"]

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    check("run_completed", run["status"] == RunStatus.COMPLETED.value, run["status"])
    if run["status"] != RunStatus.COMPLETED.value:
        return checks

    clean = payload["clean"]
    finding_payload = payload["findings"]
    plan = payload["plan"]
    test_payload = payload["tests"]
    trace = payload["trace"]

    cleaned_ids = {review["review_id"] for review in clean["reviews"]}
    check("cleaned_reviews_nonempty", len(cleaned_ids) > 0, str(clean["stats"]))
    check(
        "summaries_cover_cleaned_reviews",
        all(review.get("content_summary_zh") for review in clean["reviews"]),
        (
            f"{sum(bool(r.get('content_summary_zh')) for r in clean['reviews'])}"
            f"/{len(clean['reviews'])}"
        ),
    )

    findings = finding_payload["findings"]
    check("findings_exist", len(findings) > 0, f"{len(findings)} findings")
    for finding in findings:
        cited = set(finding["supporting_review_ids"]) | set(finding["conflicting_review_ids"])
        check(
            f"finding_{finding['finding_id']}_references_exist",
            cited.issubset(cleaned_ids),
            (
                f"support={finding['supporting_review_ids']} "
                f"conflict={finding['conflicting_review_ids']}"
            ),
        )
        check(
            f"finding_{finding['finding_id']}_counts_match",
            finding["support_count"] == len(finding["supporting_review_ids"])
            and finding["conflict_count"] == len(finding["conflicting_review_ids"]),
            f"support_count={finding['support_count']} conflict_count={finding['conflict_count']}",
        )
        check(
            f"finding_{finding['finding_id']}_confidence_range",
            0 < finding["confidence"] <= 1,
            str(finding["confidence"]),
        )
        check(
            f"finding_{finding['finding_id']}_status_allowed",
            finding["evidence_status"]
            in (EvidenceStatus.VALIDATED.value, EvidenceStatus.ASSUMPTION.value),
            finding["evidence_status"],
        )

    requirements = plan["requirements"]
    check(
        "requirement_count_in_range_or_notice",
        5 <= len(requirements) <= 10 or plan.get("quantity_notice"),
        f"{len(requirements)} requirements; notice={plan.get('quantity_notice')}",
    )
    finding_by_id = {finding["finding_id"]: finding for finding in findings}
    for requirement in requirements:
        linked = [finding_by_id[fid] for fid in requirement["finding_ids"] if fid in finding_by_id]
        expected = {
            review_id for finding in linked for review_id in finding["supporting_review_ids"]
        }
        check(
            f"requirement_{requirement['requirement_id']}_inherits_reviews",
            set(requirement["source_review_ids"]) == expected,
            f"source={requirement['source_review_ids']} expected={sorted(expected)}",
        )

    test_cases = test_payload["test_cases"]
    requirement_by_id = {requirement["requirement_id"]: requirement for requirement in requirements}
    cases_by_requirement: dict[str, list[dict]] = {}
    for test_case in test_cases:
        requirement = requirement_by_id.get(test_case["requirement_id"])
        check(
            f"test_{test_case['test_case_id']}_links_requirement",
            requirement is not None,
            test_case["requirement_id"],
        )
        if requirement is not None:
            cases_by_requirement.setdefault(requirement["requirement_id"], []).append(test_case)
            check(
                f"test_{test_case['test_case_id']}_inherits_reviews",
                set(test_case["source_review_ids"]) == set(requirement["source_review_ids"]),
                f"source={test_case['source_review_ids']}",
            )
    check(
        "test_case_count_2_to_4_per_requirement",
        all(2 <= len(cases) <= 4 for cases in cases_by_requirement.values()),
        {key: len(value) for key, value in cases_by_requirement.items()},
    )

    check("traceability_valid", trace["valid"] is True, f"{len(trace.get('issues', []))} issues")
    return checks


def _build_payload(repository, run) -> dict:
    return {
        "request": run.request.model_dump(mode="json"),
        "run": run.model_dump(mode="json"),
        "events": [event.model_dump(mode="json") for event in repository.list_events(run.run_id)],
        "clean": repository.get_output(run.run_id, Stage.CLEAN) or {},
        "findings": repository.get_output(run.run_id, Stage.VALIDATE_FINDINGS) or {},
        "plan": repository.get_output(run.run_id, Stage.PLAN) or {},
        "tests": repository.get_output(run.run_id, Stage.GENERATE_TESTS) or {},
        "trace": repository.get_output(run.run_id, Stage.VALIDATE_TRACEABILITY) or {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--app-url", help="US App Store URL for online collection")
    source.add_argument("--file", help="JSON/CSV review file to import")
    source.add_argument(
        "--resume",
        help="resume an existing WAITING run by its run_id (same checkpoint semantics)",
    )
    parser.add_argument("--goal", required=True, help="analysis goal")
    parser.add_argument("--limit", type=int, default=200, help="review limit")
    parser.add_argument("--out", required=True, help="output JSON summary path")
    args = parser.parse_args()

    services = build_services()
    if services.batch_analyzer is None:
        print("ERROR: real model provider is not configured (key/model disabled)", file=sys.stderr)
        return 2

    if args.resume:
        orchestrator = __import__(
            "app_review_insights.pipeline.orchestrator",
            fromlist=["AnalysisOrchestrator"],
        ).AnalysisOrchestrator(services)
        run = orchestrator.resume(args.resume)
    else:
        if args.app_url:
            source_type = SourceType.ONLINE
            imported = None
        else:
            path = Path(args.file)
            source_type = SourceType.JSON if path.suffix.lower() == ".json" else SourceType.CSV
            imported = import_reviews(path.read_bytes(), path.name, app_id="imported")[: args.limit]

        request = AnalysisRequest(
            source_type=source_type,
            app_url=args.app_url,
            analysis_goal=args.goal,
            review_limit=args.limit,
        )
        run = (
            __import__(
                "app_review_insights.pipeline.orchestrator",
                fromlist=["AnalysisOrchestrator"],
            )
            .AnalysisOrchestrator(services)
            .start(request, imported_reviews=imported)
        )

    payload = _build_payload(services.repository, run)
    checks = _run_checks(payload)
    summary = {
        "run_id": run.run_id,
        "status": run.status.value,
        "checks": checks,
        "all_passed": all(item["ok"] for item in checks),
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
