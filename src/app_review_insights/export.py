import csv
import io
import json
from typing import Any

from app_review_insights.models import Finding, Requirement, TestCase


def build_traceability_rows(
    findings: list[Finding],
    requirements: list[Requirement],
    test_cases: list[TestCase],
) -> list[dict[str, str]]:
    finding_index = {item.finding_id: item for item in findings}
    requirement_index = {item.requirement_id: item for item in requirements}
    rows: list[dict[str, str]] = []

    for test_case in test_cases:
        requirement = requirement_index.get(test_case.requirement_id)
        if requirement is None:
            continue
        for finding_id in requirement.finding_ids:
            finding = finding_index.get(finding_id)
            if finding is None:
                continue
            rows.append(
                {
                    "review_ids": ",".join(finding.supporting_review_ids),
                    "finding_id": finding.finding_id,
                    "requirement_id": requirement.requirement_id,
                    "test_case_id": test_case.test_case_id,
                }
            )
    return rows


def to_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def rows_to_csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    if not rows:
        return b""

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")
