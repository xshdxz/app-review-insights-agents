import inspect
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app_review_insights.batching import make_review_batches
from app_review_insights.cleaning import CleaningResult, clean_reviews
from app_review_insights.errors import (
    CollectionError,
    ConcurrentRunError,
    RecoverableModelError,
    RunDeadlineExceeded,
)
from app_review_insights.llm.schemas import (
    BatchAnalysisResult,
    ConsolidationResult,
    EvidenceAuditResult,
)
from app_review_insights.llm.usage import current_run_id, current_stage
from app_review_insights.models import (
    AnalysisRequest,
    Finding,
    Requirement,
    Review,
    RunRecord,
    RunStatus,
    SourceType,
    Stage,
    StageEvent,
    TestCase,
    ValidationReport,
)
from app_review_insights.pipeline.analyze import apply_review_summaries
from app_review_insights.pipeline.traceability import validate_traceability
from app_review_insights.pipeline.validate import validate_finding_drafts
from app_review_insights.storage.repository import RunRepository

BatchAnalyzer = Callable[[list[Review], str], BatchAnalysisResult]
Consolidator = Callable[..., ConsolidationResult]
EvidenceAuditor = Callable[[list[Any], list[Review], str], EvidenceAuditResult]
FindingValidator = Callable[..., tuple[list[Finding], ValidationReport]]
RequirementBuilder = Callable[[list[Finding], str, int], list[Requirement]]
TestCaseBuilder = Callable[[list[Requirement]], list[TestCase]]
TraceabilityValidator = Callable[
    [set[str], list[Finding], list[Requirement], list[TestCase]], ValidationReport
]
EventCallback = Callable[[StageEvent], None]


@dataclass
class PipelineServices:
    repository: RunRepository
    batch_analyzer: BatchAnalyzer
    collector: Any | None = None
    consolidator: Consolidator | None = None
    evidence_auditor: EvidenceAuditor | None = None
    finding_validator: FindingValidator | None = None
    requirement_builder: RequirementBuilder | None = None
    test_case_builder: TestCaseBuilder | None = None
    traceability_validator: TraceabilityValidator | None = None
    batch_size: int = 100
    batch_max_characters: int = 60_000


class AnalysisOrchestrator:
    def __init__(
        self,
        services: PipelineServices,
        on_event: EventCallback | None = None,
        max_duration_seconds: float | None = None,
    ) -> None:
        self.services = services
        self.repository = services.repository
        self.on_event = on_event
        #: 整轮运行的墙钟上限；None 表示不限制
        self.max_duration_seconds = max_duration_seconds
        self._deadline: float | None = None

    def start(
        self,
        request: AnalysisRequest,
        imported_reviews: list[Review] | None = None,
        allow_concurrent: bool = False,
    ) -> RunRecord:
        # 同一 App 并发分析只是把同一份结论算两遍并双倍计费
        if not allow_concurrent and request.app_url:
            active = self.repository.find_active_run(request.app_url)
            if active is not None:
                raise ConcurrentRunError(
                    f"该 App 已有进行中的运行（run_id={active.run_id}，"
                    f"状态 {active.status.value}）。并发分析会重复消耗模型额度；"
                    "请等待其结束，或先续跑/放弃该运行后再试。"
                )
        now = datetime.now(UTC)
        run = RunRecord(
            run_id=str(uuid4()),
            request=request,
            current_stage=Stage.SCOPE,
            status=RunStatus.PENDING,
            created_at=now,
            updated_at=now,
        )
        self.repository.save_run(run)
        self._add_event(run, "Analysis run created")
        return self._run_in_context(run, imported_reviews=imported_reviews)

    def resume(
        self,
        run_id: str,
        imported_reviews: list[Review] | None = None,
    ) -> RunRecord:
        """从检查点续跑。

        `imported_reviews` 用于「采集阶段尚未完成就中断」的情形（超时或首次失败
        发生在 collect 之前）：此时检查点里没有评论，必须重新提供导入文件才能继续。
        采集已完成时会直接读检查点，该参数被忽略。
        """
        run = self.repository.get_run(run_id)
        # 超时停止的运行同样可续跑——它们都停在检查点上
        if run.status not in (RunStatus.WAITING, RunStatus.TIMED_OUT):
            return run

        run = self._update_run(run, status=RunStatus.RUNNING, last_error=None)
        self._add_event(run, "Analysis run resumed")
        return self._run_in_context(run, imported_reviews=imported_reviews)

    def _run_in_context(
        self,
        run: RunRecord,
        imported_reviews: list[Review] | None = None,
    ) -> RunRecord:
        """在带 run_id / stage 上下文的范围内执行，结束后**必定还原**。

        这两个 contextvar 供 provider 归集模型用量。若不还原，值会泄漏到运行
        之外的调用（例如流水线跑完后 Agent 侧再发起的模型调用），把成本记到
        一个已经结束的运行上。
        """
        self._deadline = (
            None
            if self.max_duration_seconds is None
            else time.monotonic() + self.max_duration_seconds
        )
        run_token = current_run_id.set(run.run_id)
        stage_token = current_stage.set(None)
        try:
            return self._execute(run, imported_reviews=imported_reviews)
        except RunDeadlineExceeded as exc:
            # 停在检查点上：已完成阶段全部保留，调高上限即可续跑
            return self._stop(
                self.repository.get_run(run.run_id),
                status=RunStatus.TIMED_OUT,
                error=str(exc),
                message="Run exceeded the configured max duration",
            )
        finally:
            self._deadline = None
            current_stage.reset(stage_token)
            current_run_id.reset(run_token)

    def _execute(
        self,
        run: RunRecord,
        imported_reviews: list[Review] | None = None,
    ) -> RunRecord:
        collected_output = self.repository.get_output(run.run_id, Stage.COLLECT)
        if collected_output is None:
            run = self._begin_stage(run, Stage.COLLECT)
            try:
                reviews = self._collect_reviews(run.request, imported_reviews)
            except CollectionError as exc:
                return self._stop(
                    run,
                    status=RunStatus.FAILED,
                    error=self._safe_error(exc),
                    message="Review collection failed",
                )
            self.repository.save_output(
                run.run_id,
                Stage.COLLECT,
                {"reviews": self._dump_models(reviews)},
            )
            self._add_event(
                run,
                "Review collection completed",
                {"review_count": len(reviews)},
            )
        else:
            reviews = self._load_models(collected_output, "reviews", Review)

        cleaned_output = self.repository.get_output(run.run_id, Stage.CLEAN)
        if cleaned_output is None:
            run = self._begin_stage(run, Stage.CLEAN)
            cleaning_result = clean_reviews(reviews)
            cleaned_reviews = cleaning_result.reviews
            self.repository.save_output(
                run.run_id,
                Stage.CLEAN,
                cleaning_result.model_dump(mode="json"),
            )
            self._add_event(
                run,
                "Review cleaning completed",
                {
                    "input_count": cleaning_result.stats.input_count,
                    "output_count": cleaning_result.stats.output_count,
                },
            )
        else:
            cleaning_result = CleaningResult.model_validate(cleaned_output)
            cleaned_reviews = cleaning_result.reviews

        batch_results, run = self._analyze_batches(run, cleaned_reviews)
        cleaned_reviews = apply_review_summaries(cleaned_reviews, batch_results)
        cleaning_result = cleaning_result.model_copy(update={"reviews": cleaned_reviews})
        self.repository.save_output(
            run.run_id,
            Stage.CLEAN,
            cleaning_result.model_dump(mode="json"),
        )
        if run.status == RunStatus.WAITING:
            return run

        consolidation_output = self.repository.get_output(run.run_id, Stage.CONSOLIDATE)
        if consolidation_output is None:
            run = self._begin_stage(run, Stage.CONSOLIDATE)
            try:
                consolidated = self._consolidate(
                    batch_results,
                    run.request.analysis_goal,
                    cleaned_reviews,
                )
            except RecoverableModelError as exc:
                return self._wait(run, exc, cleaned_reviews)
            self.repository.save_output(
                run.run_id,
                Stage.CONSOLIDATE,
                consolidated.model_dump(mode="json"),
            )
            self._add_event(
                run,
                "Finding consolidation completed",
                {"finding_count": len(consolidated.findings)},
            )
        else:
            consolidated = ConsolidationResult.model_validate(consolidation_output)

        evidence_audit: EvidenceAuditResult | None = None
        if self.services.evidence_auditor is not None:
            audit_output = self.repository.get_output(
                run.run_id,
                Stage.AUDIT_EVIDENCE,
            )
            if audit_output is None:
                run = self._begin_stage(run, Stage.AUDIT_EVIDENCE)
                try:
                    evidence_audit = self._audit_evidence(
                        consolidated,
                        cleaned_reviews,
                        run.request.analysis_goal,
                    )
                except RecoverableModelError as exc:
                    return self._wait(run, exc, cleaned_reviews)
                self.repository.save_output(
                    run.run_id,
                    Stage.AUDIT_EVIDENCE,
                    evidence_audit.model_dump(mode="json"),
                )
                self._add_event(
                    run,
                    "Finding evidence audit completed",
                    {"finding_count": len(evidence_audit.findings)},
                )
            else:
                evidence_audit = EvidenceAuditResult.model_validate(audit_output)

        validation_output = self.repository.get_output(run.run_id, Stage.VALIDATE_FINDINGS)
        if validation_output is None:
            run = self._begin_stage(run, Stage.VALIDATE_FINDINGS)
            findings, finding_report = self._validate_findings(
                consolidated,
                cleaned_reviews,
                evidence_audit,
            )
            self.repository.save_output(
                run.run_id,
                Stage.VALIDATE_FINDINGS,
                {
                    "findings": self._dump_models(findings),
                    "report": finding_report.model_dump(mode="json"),
                },
            )
            self._add_event(
                run,
                "Finding validation completed",
                {
                    "finding_count": len(findings),
                    "valid": finding_report.valid,
                },
            )
        else:
            findings = self._load_models(validation_output, "findings", Finding)

        planning_output = self.repository.get_output(run.run_id, Stage.PLAN)
        if planning_output is None:
            run = self._begin_stage(run, Stage.PLAN)
            try:
                requirements = self._build_requirements(
                    findings,
                    run.request.analysis_goal,
                    len(cleaned_reviews),
                )
            except RecoverableModelError as exc:
                return self._wait(run, exc, cleaned_reviews)
            self.repository.save_output(
                run.run_id,
                Stage.PLAN,
                {
                    "requirements": self._dump_models(requirements),
                    "quantity_notice": (
                        "可验证证据不足，因此本次输出少于 5 个核心需求。"
                        if len(requirements) < 5
                        else None
                    ),
                },
            )
            self._add_event(
                run,
                "Product planning completed",
                {"requirement_count": len(requirements)},
            )
        else:
            requirements = self._load_models(
                planning_output,
                "requirements",
                Requirement,
            )

        tests_output = self.repository.get_output(run.run_id, Stage.GENERATE_TESTS)
        if tests_output is None:
            run = self._begin_stage(run, Stage.GENERATE_TESTS)
            try:
                test_cases = self._build_test_cases(requirements)
            except RecoverableModelError as exc:
                return self._wait(run, exc, cleaned_reviews)
            self.repository.save_output(
                run.run_id,
                Stage.GENERATE_TESTS,
                {"test_cases": self._dump_models(test_cases)},
            )
            self._add_event(
                run,
                "Test generation completed",
                {"test_case_count": len(test_cases)},
            )
        else:
            test_cases = self._load_models(tests_output, "test_cases", TestCase)

        traceability_output = self.repository.get_output(
            run.run_id,
            Stage.VALIDATE_TRACEABILITY,
        )
        if traceability_output is None:
            run = self._begin_stage(run, Stage.VALIDATE_TRACEABILITY)
            traceability_report = self._validate_traceability(
                {review.review_id for review in cleaned_reviews},
                findings,
                requirements,
                test_cases,
            )
            self.repository.save_output(
                run.run_id,
                Stage.VALIDATE_TRACEABILITY,
                traceability_report.model_dump(mode="json"),
            )
            self._add_event(
                run,
                "Traceability validation completed",
                {
                    "valid": traceability_report.valid,
                    "issue_count": len(traceability_report.issues),
                },
            )
        else:
            traceability_report = ValidationReport.model_validate(traceability_output)

        if not traceability_report.valid:
            run = self._update_run(
                run,
                current_stage=Stage.VALIDATE_TRACEABILITY,
                status=RunStatus.PARTIAL,
                coverage_ratio=min(run.coverage_ratio, 0.99),
                last_error=None,
            )
            self._add_event(
                run,
                "Traceability validation found unresolved issues",
                {"valid": False, "issue_count": len(traceability_report.issues)},
            )
            return run

        run = self._update_run(
            run,
            current_stage=Stage.COMPLETE,
            status=RunStatus.COMPLETED,
            coverage_ratio=1,
            last_error=None,
        )
        self._add_event(run, "Analysis run completed", {"valid": True})
        return run

    def _analyze_batches(
        self,
        run: RunRecord,
        reviews: list[Review],
    ) -> tuple[list[BatchAnalysisResult], RunRecord]:
        manifest = self.repository.get_output(
            run.run_id,
            Stage.ANALYZE_BATCHES,
        )
        if manifest is None:
            batches = make_review_batches(
                reviews,
                max_reviews=self.services.batch_size,
                max_characters=self.services.batch_max_characters,
            )
            manifest = self._make_batch_manifest(batches)
            self.repository.save_output(
                run.run_id,
                Stage.ANALYZE_BATCHES,
                manifest,
            )
        else:
            batches = self._restore_batches(manifest, reviews)

        saved_outputs = [
            self.repository.get_output(
                run.run_id,
                Stage.ANALYZE_BATCHES,
                batch_index=index,
            )
            for index in range(len(batches))
        ]
        completed_count = sum(output is not None for output in saved_outputs)
        analysis_incomplete = not manifest.get("completed", False) or completed_count < len(batches)
        if analysis_incomplete:
            run = self._begin_stage(
                run,
                Stage.ANALYZE_BATCHES,
                current_batch=self._first_missing(saved_outputs),
                total_batches=len(batches),
                coverage_ratio=self._batch_coverage(
                    completed_count,
                    len(batches),
                ),
            )
        else:
            run = self._update_run(
                run,
                current_batch=len(batches),
                total_batches=len(batches),
                coverage_ratio=self._batch_coverage(
                    completed_count,
                    len(batches),
                ),
            )

        results: list[BatchAnalysisResult] = []
        for index, batch in enumerate(batches):
            saved = saved_outputs[index]
            if saved is not None:
                results.append(BatchAnalysisResult.model_validate(saved))
                continue

            run = self._update_run(run, current_batch=index)
            try:
                if self.services.batch_analyzer is None:
                    raise RecoverableModelError(
                        "模型未配置：请先配置 DEEPSEEK_API_KEY 后从检查点继续"
                    )
                result = BatchAnalysisResult.model_validate(
                    self.services.batch_analyzer(
                        batch,
                        run.request.analysis_goal,
                    )
                )
            except RecoverableModelError as exc:
                run = self._update_run(
                    run,
                    current_stage=Stage.ANALYZE_BATCHES,
                    status=RunStatus.WAITING,
                    current_batch=index,
                    total_batches=len(batches),
                    coverage_ratio=self._batch_coverage(
                        completed_count,
                        len(batches),
                    ),
                    last_error=self._safe_error(exc, reviews),
                )
                self._add_event(
                    run,
                    "Batch analysis paused for retry",
                    {
                        "batch_index": index,
                        "completed_batches": completed_count,
                        "total_batches": len(batches),
                    },
                )
                return results, run

            self.repository.save_output(
                run.run_id,
                Stage.ANALYZE_BATCHES,
                result.model_dump(mode="json"),
                batch_index=index,
            )
            results.append(result)
            completed_count += 1
            run = self._update_run(
                run,
                current_batch=index + 1,
                total_batches=len(batches),
                coverage_ratio=self._batch_coverage(
                    completed_count,
                    len(batches),
                ),
            )
            self._add_event(
                run,
                "Batch analysis completed",
                {
                    "batch_index": index,
                    "completed_batches": completed_count,
                    "total_batches": len(batches),
                },
            )

        if analysis_incomplete:
            completed_manifest = dict(manifest)
            completed_manifest["completed"] = True
            self.repository.save_output(
                run.run_id,
                Stage.ANALYZE_BATCHES,
                completed_manifest,
            )
            self._add_event(
                run,
                "All review batches analyzed",
                {"total_batches": len(batches)},
            )
        return results, run

    def _collect_reviews(
        self,
        request: AnalysisRequest,
        imported_reviews: list[Review] | None,
    ) -> list[Review]:
        if request.source_type == SourceType.ONLINE:
            if self.services.collector is None:
                raise CollectionError("online collection requires a collector")
            if not request.app_url:
                raise CollectionError("online collection requires an app URL")
            collected = self.services.collector.collect(
                request.app_url,
                request.review_limit,
            )
            return [Review.model_validate(review) for review in collected]

        if imported_reviews is None:
            raise CollectionError("imported reviews are required for this source")
        return [Review.model_validate(review) for review in imported_reviews]

    def _consolidate(
        self,
        batch_results: list[BatchAnalysisResult],
        goal: str,
        reviews: list[Review],
    ) -> ConsolidationResult:
        if self.services.consolidator is None:
            return ConsolidationResult(
                findings=[finding for result in batch_results for finding in result.findings]
            )
        parameters = inspect.signature(self.services.consolidator).parameters
        if len(parameters) >= 3:
            result = self.services.consolidator(batch_results, goal, reviews)
        else:
            result = self.services.consolidator(batch_results, goal)
        return ConsolidationResult.model_validate(result)

    def _validate_findings(
        self,
        consolidated: ConsolidationResult,
        reviews: list[Review],
        audit: EvidenceAuditResult | None = None,
    ) -> tuple[list[Finding], ValidationReport]:
        validator = self.services.finding_validator or validate_finding_drafts
        if audit is None:
            findings, report = validator(consolidated.findings, reviews)
        else:
            findings, report = validator(consolidated.findings, reviews, audit)
        return (
            [Finding.model_validate(finding) for finding in findings],
            ValidationReport.model_validate(report),
        )

    def _audit_evidence(
        self,
        consolidated: ConsolidationResult,
        reviews: list[Review],
        goal: str,
    ) -> EvidenceAuditResult:
        if self.services.evidence_auditor is None:
            return EvidenceAuditResult()
        return EvidenceAuditResult.model_validate(
            self.services.evidence_auditor(
                consolidated.findings,
                reviews,
                goal,
            )
        )

    def _build_requirements(
        self,
        findings: list[Finding],
        goal: str,
        total_reviews: int,
    ) -> list[Requirement]:
        if self.services.requirement_builder is None:
            return []
        return [
            Requirement.model_validate(requirement)
            for requirement in self.services.requirement_builder(
                findings,
                goal,
                total_reviews,
            )
        ]

    def _build_test_cases(
        self,
        requirements: list[Requirement],
    ) -> list[TestCase]:
        if self.services.test_case_builder is None:
            return []
        return [
            TestCase.model_validate(test_case)
            for test_case in self.services.test_case_builder(requirements)
        ]

    def _validate_traceability(
        self,
        review_ids: set[str],
        findings: list[Finding],
        requirements: list[Requirement],
        test_cases: list[TestCase],
    ) -> ValidationReport:
        validator = self.services.traceability_validator or validate_traceability
        return ValidationReport.model_validate(
            validator(review_ids, findings, requirements, test_cases)
        )

    def _begin_stage(
        self,
        run: RunRecord,
        stage: Stage,
        **updates: Any,
    ) -> RunRecord:
        if self._deadline is not None and time.monotonic() > self._deadline:
            raise RunDeadlineExceeded(
                f"运行超过最长时长限制（{self.max_duration_seconds:.0f} 秒），已在检查点停止；"
                "调高 RUN_MAX_DURATION_SECONDS 后可用同一 run_id 续跑。"
            )
        run = self._update_run(
            run,
            current_stage=stage,
            status=RunStatus.RUNNING,
            last_error=None,
            **updates,
        )
        self._add_event(run, f"Stage started: {stage.value}")
        # 成本按阶段归集，便于定位开销大头
        current_stage.set(stage.value)
        return run

    def _wait(
        self,
        run: RunRecord,
        error: RecoverableModelError,
        reviews: list[Review],
    ) -> RunRecord:
        return self._stop(
            run,
            status=RunStatus.WAITING,
            error=self._safe_error(error, reviews),
            message="Model stage paused for retry",
        )

    def _stop(
        self,
        run: RunRecord,
        status: RunStatus,
        error: str,
        message: str,
    ) -> RunRecord:
        run = self._update_run(run, status=status, last_error=error)
        self._add_event(
            run,
            message,
            {"error_type": "recoverable" if status == RunStatus.WAITING else "failure"},
        )
        return run

    def _update_run(self, run: RunRecord, **updates: Any) -> RunRecord:
        updates["updated_at"] = datetime.now(UTC)
        updated = run.model_copy(update=updates)
        self.repository.save_run(updated)
        return updated

    def _add_event(
        self,
        run: RunRecord,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        event = StageEvent(
            stage=run.current_stage,
            status=run.status,
            message=message,
            payload=payload or {},
            created_at=datetime.now(UTC),
        )
        self.repository.add_event(run.run_id, event)
        if self.on_event is not None:
            self.on_event(event)

    @staticmethod
    def _dump_models(models: list[Any]) -> list[dict[str, Any]]:
        return [model.model_dump(mode="json") for model in models]

    @staticmethod
    def _load_models(
        output: dict[str, Any],
        key: str,
        model_type: type[Any],
    ) -> list[Any]:
        return [model_type.model_validate(value) for value in output.get(key, [])]

    @staticmethod
    def _first_missing(outputs: list[dict[str, Any] | None]) -> int:
        return next(
            (index for index, output in enumerate(outputs) if output is None),
            len(outputs),
        )

    @staticmethod
    def _make_batch_manifest(
        batches: list[list[Review]],
    ) -> dict[str, Any]:
        batch_review_indices: list[list[int]] = []
        next_index = 0
        for batch in batches:
            batch_review_indices.append(list(range(next_index, next_index + len(batch))))
            next_index += len(batch)
        return {
            "batch_review_indices": batch_review_indices,
            "completed": False,
        }

    @staticmethod
    def _restore_batches(
        manifest: dict[str, Any],
        reviews: list[Review],
    ) -> list[list[Review]]:
        raw_boundaries = manifest.get("batch_review_indices")
        if not isinstance(raw_boundaries, list):
            raise ValueError("saved batch manifest is invalid")

        try:
            boundaries = [[int(index) for index in batch] for batch in raw_boundaries]
        except (TypeError, ValueError) as exc:
            raise ValueError("saved batch manifest is invalid") from exc

        expected_indices = list(range(len(reviews)))
        actual_indices = [index for batch in boundaries for index in batch]
        if actual_indices != expected_indices:
            raise ValueError("saved batch manifest does not match cleaned reviews")
        return [[reviews[index] for index in batch] for batch in boundaries]

    @staticmethod
    def _batch_coverage(completed: int, total: int) -> float:
        if total == 0:
            return 0
        return min(completed / total, 0.99)

    @staticmethod
    def _safe_error(error: Exception, reviews: list[Review] | None = None) -> str:
        message = re.sub(r"[\r\n]+", " ", str(error)).strip()
        for review in reviews or []:
            if review.content_original:
                message = message.replace(review.content_original, "[REDACTED_REVIEW]")
        message = re.sub(
            r"(?i)(api[ _-]?key\s*[=:]\s*)\S+",
            r"\1[REDACTED]",
            message,
        )
        message = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", message)
        message = re.sub(r"(?i)\.env(?:\.[\w.-]+)?", "[REDACTED_ENV]", message)
        return message[:1000] or error.__class__.__name__
