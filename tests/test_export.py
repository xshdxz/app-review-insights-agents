import hashlib
import json
from datetime import UTC, datetime

import pytest

from app_review_insights.export import (
    build_traceability_rows,
    rows_to_csv_bytes,
    to_json_bytes,
)
from app_review_insights.llm.recording import RECORDING_MODE
from app_review_insights.models import (
    LIVE_RUN_MODE,
    AnalysisRequest,
    EvidenceStatus,
    Finding,
    Requirement,
    RunRecord,
    RunStatus,
    SourceType,
    Stage,
)
from app_review_insights.models import (
    TestCase as DomainTestCase,
)
from app_review_insights.storage import RunRepository
from app_review_insights.storage.cache import (
    build_demo_downloads,
    build_downloads,
    export_demo_run,
    load_demo_run,
)


def test_traceability_export_contains_complete_entity_chain():
    finding = Finding(
        finding_id="F-001",
        title="Problem",
        problem_statement="Problem",
        topic_label="topic",
        supporting_review_ids=["r-1", "r-2"],
        support_count=2,
        confidence=0.8,
        evidence_status=EvidenceStatus.VALIDATED,
        model_reasoning_summary="reason",
    )
    requirement = Requirement(
        requirement_id="REQ-001",
        finding_ids=["F-001"],
        title="Requirement",
        user_problem="Problem",
        objective="Objective",
        scope=["Scope"],
        non_goals=[],
        functional_rules=["Rule"],
        edge_cases=[],
        acceptance_criteria=["Criterion"],
        success_metrics=["Metric"],
        impact=3,
        complexity="low",
        target_version="V1.0",
        source_review_ids=["r-1", "r-2"],
    )
    test_case = DomainTestCase(
        test_case_id="TC-001",
        requirement_id="REQ-001",
        title="Case",
        preconditions=[],
        steps=["Act"],
        expected_result="Result",
        case_type="normal",
        source_review_ids=["r-1", "r-2"],
    )

    rows = build_traceability_rows([finding], [requirement], [test_case])

    assert rows == [
        {
            "review_ids": "r-1,r-2",
            "finding_id": "F-001",
            "requirement_id": "REQ-001",
            "test_case_id": "TC-001",
        }
    ]


def test_json_and_csv_exports_are_utf8_compatible():
    payload = {"title": "订阅说明"}
    rows = [
        {
            "review_ids": "r-1",
            "finding_id": "F-001",
            "requirement_id": "REQ-001",
            "test_case_id": "TC-001",
        }
    ]

    assert json.loads(to_json_bytes(payload).decode("utf-8")) == payload
    csv_bytes = rows_to_csv_bytes(rows)
    assert csv_bytes.startswith(b"\xef\xbb\xbf")
    assert "REQ-001" in csv_bytes.decode("utf-8-sig")


def test_empty_traceability_rows_export_as_empty_bytes():
    assert rows_to_csv_bytes([]) == b""


def _finding() -> Finding:
    return Finding(
        finding_id="F-001",
        title="Timer resume failure",
        problem_statement="Timer freezes after pause.",
        topic_label="timer reliability",
        supporting_review_ids=["r-1", "r-2"],
        support_count=2,
        confidence=0.9,
        evidence_status=EvidenceStatus.VALIDATED,
        model_reasoning_summary="Two reviews describe the same failure.",
    )


def _requirement() -> Requirement:
    return Requirement(
        requirement_id="REQ-001",
        finding_ids=["F-001"],
        title="Restore timer after pause",
        user_problem="Paused workouts cannot reliably resume.",
        objective="Keep the workout timer continuous after resume.",
        scope=["Pause and resume timer state"],
        non_goals=[],
        functional_rules=["Resume from the saved elapsed time"],
        edge_cases=["App returns from the background while paused"],
        acceptance_criteria=["Timer advances within one second after resume"],
        success_metrics=["Resume failure rate"],
        impact=5,
        complexity="medium",
        target_version="V1.0",
        source_review_ids=["r-1", "r-2"],
    )


def _test_case() -> DomainTestCase:
    return DomainTestCase(
        test_case_id="TC-001",
        requirement_id="REQ-001",
        title="Resume a paused workout",
        preconditions=["Workout timer is running"],
        steps=["Pause the workout", "Resume the workout"],
        expected_result="Timer continues from the saved elapsed time.",
        case_type="regression",
        source_review_ids=["r-1", "r-2"],
    )


def _save_export_outputs(repository: RunRepository, run_id: str) -> None:
    repository.save_output(
        run_id,
        Stage.CLEAN,
        {"reviews": [{"review_id": "r-1", "content_original": "Timer freezes."}]},
    )
    repository.save_output(
        run_id,
        Stage.VALIDATE_FINDINGS,
        {"findings": [_finding().model_dump(mode="json")]},
    )
    repository.save_output(
        run_id,
        Stage.PLAN,
        {"requirements": [_requirement().model_dump(mode="json")]},
    )
    repository.save_output(
        run_id,
        Stage.GENERATE_TESTS,
        {"test_cases": [_test_case().model_dump(mode="json")]},
    )
    repository.save_output(
        run_id,
        Stage.VALIDATE_TRACEABILITY,
        {"valid": True, "issues": []},
    )


def test_demo_cache_is_explicitly_labeled(tmp_path):
    path = tmp_path / "demo.json"
    path.write_text(
        json.dumps(
            {
                "mode": "historical_cache_demo",
                "is_live": False,
                "collected_at": "2026-08-15T00:00:00+00:00",
                "result": {},
            }
        ),
        encoding="utf-8",
    )

    demo = load_demo_run(path)

    assert demo["mode"] == "historical_cache_demo"
    assert demo["is_live"] is False
    assert demo["collected_at"]


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "live", "is_live": False, "collected_at": "now", "result": {}},
        {
            "mode": "historical_cache_demo",
            "is_live": True,
            "collected_at": "now",
            "result": {},
        },
    ],
)
def test_demo_cache_rejects_live_or_ambiguous_labels(tmp_path, payload):
    path = tmp_path / "invalid-demo.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="non-live"):
        load_demo_run(path)


def test_build_downloads_restores_domain_models_for_traceability(tmp_path):
    repository = RunRepository(tmp_path / "runs.sqlite3")
    _save_export_outputs(repository, "run-1")

    downloads = build_downloads(repository, "run-1")

    assert json.loads(downloads["cleaned_reviews"].decode("utf-8"))[0]["review_id"] == "r-1"
    assert (
        json.loads(downloads["prd"].decode("utf-8"))["requirements"][0]["requirement_id"]
        == "REQ-001"
    )
    assert "TC-001" in downloads["test_cases"].decode("utf-8-sig")
    traceability = downloads["traceability"].decode("utf-8-sig")
    assert "r-1,r-2" in traceability
    assert "F-001,REQ-001,TC-001" in traceability


def _save_run(repository: RunRepository, run_id: str, *, mode: str, is_live: bool) -> None:
    """存一条带来源标注的运行；mode/is_live 就是下载标注的唯一来源。"""
    now = datetime.now(UTC)
    repository.save_run(
        RunRecord(
            run_id=run_id,
            request=AnalysisRequest(
                source_type=SourceType.JSON,
                analysis_goal="Labeled downloads",
                review_limit=100,
            ),
            current_stage=Stage.COMPLETE,
            status=RunStatus.COMPLETED,
            mode=mode,
            is_live=is_live,
            created_at=now,
            updated_at=now,
        )
    )


def _csv_header(payload: bytes) -> str:
    return payload.decode("utf-8-sig").splitlines()[0]


#: 加标注之前四个实时下载的 sha256（收尾报告里给出的基线，脚本见报告）。实时产物
#: 必须与改动前**逐字节相同**，所以这里连字节一起钉住，而不只是「不含标注」。
_LIVE_GOLDEN_SHA256 = {
    "cleaned_reviews": "285f54d342ce13c3a20fc24ae4d1fd0b4dad6ddb535d4ff81bcae5cc79acf906",
    "prd": "a9f4833caefd1b8af611521f6179b427183f3709027ac065fb80698b2cbc022a",
    "test_cases": "001f65f4d91703b4ba4c70cf6eb1ea82e086db108f99539d83f23f8ed87a376a",
    "traceability": "c05839ade32750fd0c55f221c38653036f7ab8ab9b3f04c166457d0a1f21b821",
}


def test_replay_run_downloads_are_labeled_non_live(tmp_path):
    """回放跑出来的四个下载都要能自证不是实时结果。

    页面横幅不算数：横幅留在页面里，文件会离开页面。所以标注必须进**产物本身**——
    JSON 内嵌 mode/is_live 两个键，CSV 内嵌不了字段就各加同名的两列。四个下载一个不落。
    """
    repository = RunRepository(tmp_path / "runs.sqlite3")
    _save_run(repository, "run-replay", mode=RECORDING_MODE, is_live=False)
    _save_export_outputs(repository, "run-replay")

    downloads = build_downloads(repository, "run-replay")

    for name in ("cleaned_reviews", "prd"):
        payload = json.loads(downloads[name].decode("utf-8"))
        assert payload["mode"] == RECORDING_MODE, f"{name} 缺 mode 标注"
        assert payload["is_live"] is False, f"{name} 缺 is_live 标注"
        assert not downloads[name].startswith(b"["), f"{name} 应套信封以承载标注"

    # 清洗评论套了信封之后载荷本身没被动过（真实的往返导入在 test_demo_mode 的
    # 端到端用例里验：那边是完整评论记录，能真正走一遍 import_reviews）
    cleaned = json.loads(downloads["cleaned_reviews"].decode("utf-8"))
    assert [item["review_id"] for item in cleaned["reviews"]] == ["r-1"]

    for name in ("test_cases", "traceability"):
        header = _csv_header(downloads[name])
        assert header.endswith(",mode,is_live"), f"{name} 缺少标注列：{header}"
        # 列名在表头，取值在数据行：两处都要有，否则只标了半个
        body = downloads[name].decode("utf-8-sig")
        assert RECORDING_MODE in body and "False" in body, f"{name} 的数据行也要带标注"


def test_live_run_downloads_carry_no_non_live_label(tmp_path):
    """反向门禁：实时运行的产物不许被盖上任何非实时标注，且与改动前逐字节相同。

    没有这条，把「所有产物一律加标注」写成实现也能让上面那条用例通过——那等于给实时
    结果盖上回放的章，是本轮要修的问题的镜像。
    """
    repository = RunRepository(tmp_path / "runs.sqlite3")
    _save_run(repository, "run-live", mode=LIVE_RUN_MODE, is_live=True)
    _save_export_outputs(repository, "run-live")

    downloads = build_downloads(repository, "run-live")

    # 形状未变：清洗评论仍是顶层数组，两个 JSON 里都没有 mode/is_live 两个键
    assert isinstance(json.loads(downloads["cleaned_reviews"].decode("utf-8")), list)
    for name in ("cleaned_reviews", "prd"):
        payload = json.loads(downloads[name].decode("utf-8"))
        assert "mode" not in payload and "is_live" not in payload, name
    # CSV 表头逐列钉住：实时产物的列集合与顺序都不许变（标注列只在非实时路径出现）
    assert _csv_header(downloads["test_cases"]) == (
        "test_case_id,requirement_id,title,preconditions,steps,"
        "expected_result,case_type,source_review_ids"
    )
    assert _csv_header(downloads["traceability"]) == (
        "review_ids,finding_id,requirement_id,test_case_id"
    )
    # 字节级：与加标注之前的基线一致（见 _LIVE_GOLDEN_SHA256）
    for name, expected in _LIVE_GOLDEN_SHA256.items():
        assert hashlib.sha256(downloads[name]).hexdigest() == expected, f"{name} 字节发生变化"


def test_demo_archive_downloads_are_labeled_non_live(tmp_path):
    """历史档案这条路径的下载同样要带标注。

    此前它只标注了档案信封与页面徽章，访客点的那四个下载是裸的——约束「离线演示不能
    撒谎」管的是产物，所以这里一并补上，让「非实时产物一律带标注」真正成立。
    """
    repository = RunRepository(tmp_path / "runs.sqlite3")
    _save_run(repository, "run-1", mode=LIVE_RUN_MODE, is_live=True)
    _save_export_outputs(repository, "run-1")
    destination = tmp_path / "demo-run.json"
    export_demo_run(repository, "run-1", destination, "https://example.test/app")

    downloads = build_demo_downloads(load_demo_run(destination))

    assert json.loads(downloads["prd"].decode("utf-8"))["mode"] == "historical_cache_demo"
    assert json.loads(downloads["cleaned_reviews"].decode("utf-8"))["is_live"] is False
    assert _csv_header(downloads["traceability"]).endswith(",mode,is_live")
    # 第 4 个下载（测试用例 CSV）不能漏：表头要有标注列，数据行也要有取值
    assert _csv_header(downloads["test_cases"]).endswith(",mode,is_live"), "test_cases 缺标注列"
    assert "historical_cache_demo" in downloads["test_cases"].decode("utf-8-sig"), (
        "test_cases 数据行缺标注取值"
    )


def test_export_demo_run_writes_non_live_pipeline_outputs(tmp_path):
    repository = RunRepository(tmp_path / "runs.sqlite3")
    now = datetime.now(UTC)
    repository.save_run(
        RunRecord(
            run_id="run-1",
            request=AnalysisRequest(
                source_type=SourceType.JSON,
                analysis_goal="Identify reliability problems",
                review_limit=100,
            ),
            current_stage=Stage.COMPLETE,
            status=RunStatus.COMPLETED,
            coverage_ratio=1,
            created_at=now,
            updated_at=now,
        )
    )
    _save_export_outputs(repository, "run-1")
    destination = tmp_path / "demo-run.json"

    export_demo_run(
        repository,
        "run-1",
        destination,
        "https://apps.apple.com/us/app/example/id123456789",
    )

    demo = load_demo_run(destination)
    assert demo["run"]["run_id"] == "run-1"
    assert demo["source_app_url"].endswith("id123456789")
    assert demo["result"]["clean"]["reviews"][0]["review_id"] == "r-1"
    assert demo["result"]["validate_traceability"]["valid"] is True

    downloads = build_demo_downloads(demo)
    assert "REQ-001" in downloads["prd"].decode("utf-8")
    assert "TC-001" in downloads["traceability"].decode("utf-8-sig")
