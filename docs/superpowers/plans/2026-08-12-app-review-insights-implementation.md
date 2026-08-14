# App Review Insights 实施计划

> **供执行者使用：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，逐任务执行本计划。所有步骤使用复选框（`- [ ]`）跟踪进度。

**目标：** 构建一个可在本地运行的 Streamlit 应用，将美国区 App Store 评论转化为有证据支撑的产品发现、版本计划、PRD 需求和可追溯测试用例，并支持检查点恢复。

**架构：** Streamlit 仅作为轻量 UI，核心能力拆分到职责单一的 Python 模块中。同步状态机编排器依次调用采集/导入、确定性清洗、DeepSeek 结构化语义分析、确定性证据校验、规划、测试生成、追溯校验和 SQLite 检查点存储。每个阶段都会持久化输出，使模型批次失败后能够续跑，而无需重复已完成工作。

**技术栈：** Python 3.11+、Streamlit、Pydantic v2、pydantic-settings、兼容 OpenAI 协议的 DeepSeek 客户端、httpx、Apple App Store RSS JSON、pandas、rapidfuzz、langdetect、SQLite、pytest、Ruff。

---

## 0. 固定文件结构

创建以下职责清晰的目录结构。不要把业务逻辑放入 `app.py`，也不要集中到单个过大的 Streamlit 文件中。

```text
app.py
pyproject.toml
.env.example
README.md
README.zh-CN.md
src/app_review_insights/
  __init__.py
  config.py
  errors.py
  models.py
  input_parsing.py
  cleaning.py
  batching.py
  collectors/
    __init__.py
    app_store.py
  llm/
    __init__.py
    provider.py
    prompts.py
    schemas.py
  pipeline/
    __init__.py
    analyze.py
    validate.py
    planning.py
    test_generation.py
    traceability.py
    orchestrator.py
  storage/
    __init__.py
    repository.py
    cache.py
  ui/
    __init__.py
    main.py
    components.py
  export.py
data/samples/reviews-sample.json
data/cache/demo-run.json
docs/data-format.md
docs/model-and-prompts.md
evals/gold-reviews.json
scripts/run_eval.py
tests/
  conftest.py
  test_config.py
  test_models.py
  test_input_parsing.py
  test_cleaning.py
  test_collector.py
  test_repository.py
  test_provider.py
  test_analysis.py
  test_validation.py
  test_planning.py
  test_test_generation.py
  test_traceability.py
  test_orchestrator.py
  test_export.py
  test_app_smoke.py
```

## 任务 1：初始化 Python 项目与配置

**文件：**
- 创建： `pyproject.toml`
- 创建： `.env.example`
- 创建： `app.py`
- 创建： `src/app_review_insights/__init__.py`
- 创建： `src/app_review_insights/config.py`
- 测试： `tests/test_config.py`

- [x] **步骤 1：编写失败的配置测试**

```python
# tests/test_config.py
from app_review_insights.config import Settings


def test_settings_default_to_deepseek(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    settings = Settings(database_path=tmp_path / "runs.sqlite3")

    assert settings.model_provider == "deepseek"
    assert settings.model_name == "deepseek-chat"
    assert settings.model_base_url == "https://api.deepseek.com"
    assert settings.default_review_limit == 500
    assert settings.batch_review_limit == 100
```

- [x] **步骤 2：运行测试并确认包尚不存在**

运行：`python -m pytest tests/test_config.py -v`

预期：失败，并显示 `ModuleNotFoundError: No module named 'app_review_insights'`.

- [x] **步骤 3：添加项目元数据与依赖**

```toml
# pyproject.toml
[build-system]
requires = ["setuptools>=75", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "app-review-insights"
version = "0.1.0"
description = "Evidence-grounded App Store review analysis and product planning"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
  "streamlit>=1.48,<2",
  "pydantic>=2.11,<3",
  "pydantic-settings>=2.10,<3",
  "openai>=1.99,<2",
  "httpx>=0.28,<1",
  "pandas>=2.3,<3",
  "rapidfuzz>=3.13,<4",
  "langdetect>=1.0.9,<2",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.4,<9",
  "pytest-cov>=6.2,<7",
  "ruff>=0.12,<1",
]

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
addopts = "-q"

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
```

```dotenv
# .env.example
DEEPSEEK_API_KEY=
MODEL_PROVIDER=deepseek
MODEL_NAME=deepseek-chat
MODEL_BASE_URL=https://api.deepseek.com
MODEL_TIMEOUT_SECONDS=60
MODEL_MAX_RETRIES=2
DATABASE_PATH=data/runs/runs.sqlite3
DEFAULT_REVIEW_LIMIT=500
BATCH_REVIEW_LIMIT=100
BATCH_MAX_CHARACTERS=60000
```

- [x] **步骤 4：实现类型化配置与 Streamlit 入口**

```python
# src/app_review_insights/config.py
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    model_provider: str = Field(default="deepseek", alias="MODEL_PROVIDER")
    model_name: str = Field(default="deepseek-chat", alias="MODEL_NAME")
    model_base_url: str = Field(default="https://api.deepseek.com", alias="MODEL_BASE_URL")
    model_timeout_seconds: float = Field(default=60, alias="MODEL_TIMEOUT_SECONDS")
    model_max_retries: int = Field(default=2, alias="MODEL_MAX_RETRIES")
    database_path: Path = Field(default=Path("data/runs/runs.sqlite3"), alias="DATABASE_PATH")
    default_review_limit: int = Field(default=500, alias="DEFAULT_REVIEW_LIMIT")
    batch_review_limit: int = Field(default=100, alias="BATCH_REVIEW_LIMIT")
    batch_max_characters: int = Field(default=60000, alias="BATCH_MAX_CHARACTERS")


def load_settings() -> Settings:
    return Settings()
```

```python
# src/app_review_insights/__init__.py
__all__ = ["__version__"]
__version__ = "0.1.0"
```

```python
# app.py
from app_review_insights.ui.main import main


if __name__ == "__main__":
    main()
```

- [x] **步骤 5：安装项目并确认测试通过**

运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest tests/test_config.py -v
```

预期：`1 passed`.

- [x] **步骤 6：提交项目初始化**

```powershell
git add pyproject.toml .env.example app.py src/app_review_insights tests/test_config.py
git commit -m "chore: bootstrap app review insights project"
```

## 任务 2：定义领域模型与流水线状态

**文件：**
- 创建： `src/app_review_insights/models.py`
- 创建： `src/app_review_insights/errors.py`
- 测试： `tests/test_models.py`

- [x] **步骤 1：编写模型校验测试**

```python
# tests/test_models.py
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app_review_insights.models import EvidenceStatus, Review, Stage, TestCase


def test_review_rejects_out_of_range_rating():
    with pytest.raises(ValidationError):
        Review(
            review_id="r-1",
            app_id="839285684",
            content_original="Useful but crashes.",
            rating=6,
            published_at=datetime.now(UTC),
            source="fixture",
        )


def test_test_case_requires_traceability_fields():
    case = TestCase(
        test_case_id="TC-001",
        requirement_id="REQ-001",
        title="Show renewal date",
        preconditions=["User has an active trial"],
        steps=["Open subscription screen"],
        expected_result="Renewal date is visible before confirmation.",
        case_type="normal",
        source_review_ids=["r-1"],
    )

    assert case.requirement_id == "REQ-001"
    assert case.source_review_ids == ["r-1"]
    assert EvidenceStatus.VALIDATED.value == "validated"
    assert Stage.COMPLETE.value == "complete"
```

- [x] **步骤 2：运行测试并确认失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_models.py -v`

预期：失败，因为 `models.py` 尚不存在.

- [x] **步骤 3：实现枚举与 Pydantic 模型**

```python
# src/app_review_insights/models.py
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class SourceType(StrEnum):
    ONLINE = "online"
    JSON = "json"
    CSV = "csv"
    CACHE = "cache"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PARTIAL = "partial"
    WAITING = "waiting_for_model"
    COMPLETED = "completed"
    FAILED = "failed"


class Stage(StrEnum):
    SCOPE = "scope"
    COLLECT = "collect"
    CLEAN = "clean"
    ANALYZE_BATCHES = "analyze_batches"
    CONSOLIDATE = "consolidate"
    VALIDATE_FINDINGS = "validate_findings"
    PLAN = "plan"
    GENERATE_TESTS = "generate_tests"
    VALIDATE_TRACEABILITY = "validate_traceability"
    COMPLETE = "complete"


class EvidenceStatus(StrEnum):
    VALIDATED = "validated"
    ASSUMPTION = "assumption"
    REJECTED = "rejected"


class Review(BaseModel):
    review_id: str
    app_id: str
    storefront: str = "us"
    title: str = ""
    content_original: str = Field(min_length=1)
    content_summary_zh: str | None = None
    rating: int = Field(ge=1, le=5)
    app_version: str | None = None
    author: str | None = None
    published_at: datetime
    language: str | None = None
    source: str
    source_page: int | None = None
    content_hash: str = ""


class AnalysisRequest(BaseModel):
    source_type: SourceType
    analysis_goal: str = Field(min_length=3)
    app_url: str | None = None
    review_limit: int = Field(default=500, ge=100, le=1000)
    ratings: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])
    app_versions: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    finding_id: str
    title: str
    problem_statement: str
    topic_label: str
    supporting_review_ids: list[str]
    conflicting_review_ids: list[str] = Field(default_factory=list)
    support_count: int = 0
    conflict_count: int = 0
    confidence: float = Field(ge=0, le=1)
    evidence_status: EvidenceStatus
    model_reasoning_summary: str
    limitations: list[str] = Field(default_factory=list)


class Requirement(BaseModel):
    requirement_id: str
    finding_ids: list[str]
    title: str
    user_problem: str
    objective: str
    scope: list[str]
    non_goals: list[str]
    functional_rules: list[str]
    edge_cases: list[str]
    acceptance_criteria: list[str]
    success_metrics: list[str]
    impact: int = Field(ge=1, le=5)
    complexity: Literal["low", "medium", "high"]
    priority_score: float = 0
    target_version: Literal["V1.0", "V1.1", "Future"]
    source_review_ids: list[str]
    assumptions: list[str] = Field(default_factory=list)


class TestCase(BaseModel):
    test_case_id: str
    requirement_id: str
    title: str
    preconditions: list[str]
    steps: list[str]
    expected_result: str
    case_type: Literal["normal", "exception", "boundary", "regression"]
    source_review_ids: list[str]


class ValidationIssue(BaseModel):
    entity_type: str
    entity_id: str
    rule: str
    severity: Literal["warning", "error"]
    message: str
    revision_action: str | None = None


class ValidationReport(BaseModel):
    valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)


class StageEvent(BaseModel):
    stage: Stage
    status: RunStatus
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class RunRecord(BaseModel):
    run_id: str
    request: AnalysisRequest
    current_stage: Stage
    status: RunStatus
    current_batch: int = 0
    total_batches: int = 0
    coverage_ratio: float = Field(default=0, ge=0, le=1)
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def completed_run_must_be_complete_stage(self):
        if self.status == RunStatus.COMPLETED and self.current_stage != Stage.COMPLETE:
            raise ValueError("completed runs must use the complete stage")
        return self
```

```python
# src/app_review_insights/errors.py
class AppReviewInsightsError(Exception):
    """Base application error."""


class InputDataError(AppReviewInsightsError):
    """Input URL or imported data is invalid."""


class CollectionError(AppReviewInsightsError):
    """Online collection failed or returned unusable data."""


class RecoverableModelError(AppReviewInsightsError):
    """Model stage can be resumed from the saved checkpoint."""
```

- [x] **步骤 4：运行模型测试**

运行：`.\.venv\Scripts\python -m pytest tests/test_models.py -v`

预期：`2 passed`.

- [x] **步骤 5：提交领域层**

```powershell
git add src/app_review_insights/models.py src/app_review_insights/errors.py tests/test_models.py
git commit -m "feat: define analysis domain models"
```

## 任务 3：解析 App 链接并导入 JSON/CSV 评论

**文件：**
- 创建： `src/app_review_insights/input_parsing.py`
- 测试： `tests/test_input_parsing.py`
- 创建： `docs/data-format.md`

- [x] **步骤 1：编写 URL 与导入测试**

```python
# tests/test_input_parsing.py
import json

from app_review_insights.input_parsing import import_reviews, parse_app_store_url


def test_parse_us_app_store_url():
    parsed = parse_app_store_url(
        "https://apps.apple.com/us/app/workout-for-women-home-gym/id839285684"
    )
    assert parsed.app_id == "839285684"
    assert parsed.country == "us"
    assert parsed.slug == "workout-for-women-home-gym"


def test_import_json_normalizes_aliases():
    payload = json.dumps(
        [
            {
                "id": "r-1",
                "content": "The trial price is unclear.",
                "rating": 2,
                "date": "2026-08-01T10:00:00Z",
                "version": "8.5.0",
            }
        ]
    ).encode()

    reviews = import_reviews(payload, "reviews.json", app_id="imported-app")
    assert reviews[0].review_id == "r-1"
    assert reviews[0].content_original == "The trial price is unclear."
    assert reviews[0].app_version == "8.5.0"


def test_import_csv_accepts_documented_columns():
    payload = (
        "review_id,content,rating,published_at,title\n"
        "r-2,Workout timer freezes,1,2026-08-02T10:00:00Z,Timer bug\n"
    ).encode()

    reviews = import_reviews(payload, "reviews.csv", app_id="imported-app")
    assert reviews[0].title == "Timer bug"
```

- [x] **步骤 2：运行测试并确认失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_input_parsing.py -v`

预期：失败，因为 解析函数尚不存在.

- [x] **步骤 3：实现解析与字段别名规范化**

```python
# src/app_review_insights/input_parsing.py
from __future__ import annotations

import io
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from pydantic import BaseModel

from app_review_insights.errors import InputDataError
from app_review_insights.models import Review


class ParsedAppUrl(BaseModel):
    country: str
    slug: str
    app_id: str


_APP_URL = re.compile(
    r"^https://apps\.apple\.com/(?P<country>[a-z]{2})/app/(?P<slug>[^/]+)/id(?P<app_id>\d+)(?:[/?].*)?$",
    re.IGNORECASE,
)


def parse_app_store_url(url: str) -> ParsedAppUrl:
    match = _APP_URL.match(url.strip())
    if not match:
        raise InputDataError("请输入有效的 App Store 应用链接")
    parsed = ParsedAppUrl(**match.groupdict())
    if parsed.country.lower() != "us":
        raise InputDataError("在线评论分析仅接受美国区 App Store 链接")
    return parsed


def _records_from_bytes(data: bytes, filename: str) -> list[dict]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".json":
        decoded = json.loads(data.decode("utf-8-sig"))
        records = decoded.get("reviews", []) if isinstance(decoded, dict) else decoded
        if not isinstance(records, list):
            raise InputDataError("JSON 顶层必须是数组或包含 reviews 数组")
        return records
    if suffix == ".csv":
        return pd.read_csv(io.BytesIO(data)).where(pd.notna, None).to_dict("records")
    raise InputDataError("仅支持 .json 和 .csv 文件")


def _first(record: dict, *keys: str, default=None):
    for key in keys:
        if record.get(key) not in (None, ""):
            return record[key]
    return default


def import_reviews(data: bytes, filename: str, app_id: str) -> list[Review]:
    reviews: list[Review] = []
    for index, record in enumerate(_records_from_bytes(data, filename), start=1):
        published = _first(record, "published_at", "date", "updated")
        if not published:
            raise InputDataError(f"第 {index} 条评论缺少 published_at/date")
        reviews.append(
            Review(
                review_id=str(_first(record, "review_id", "id", default=f"import-{index}")),
                app_id=app_id,
                storefront=str(_first(record, "storefront", default="us")),
                title=str(_first(record, "title", default="")),
                content_original=str(_first(record, "content_original", "content", "review")),
                rating=int(_first(record, "rating")),
                app_version=_first(record, "app_version", "version"),
                author=_first(record, "author", "userName", "username"),
                published_at=datetime.fromisoformat(str(published).replace("Z", "+00:00")).astimezone(UTC),
                language=_first(record, "language"),
                source=f"import:{Path(filename).suffix.lower()[1:]}",
            )
        )
    if not reviews:
        raise InputDataError("导入文件中没有评论")
    return reviews
```

- [x] **步骤 4：记录明确的导入格式约定**

```markdown
<!-- docs/data-format.md -->
# Review Import Format

Required semantic fields:

- `content` or `content_original`
- `rating` from 1 to 5
- `published_at` or `date` as ISO 8601

Recommended fields:

- `review_id` or `id`
- `title`
- `app_version` or `version`
- `author`
- `language`
- `storefront`

JSON may be a top-level array or `{ "reviews": [...] }`. CSV uses the same field names as columns.
```

- [x] **步骤 5：运行导入测试并提交**

运行：`.\.venv\Scripts\python -m pytest tests/test_input_parsing.py -v`

预期：`3 passed`.

```powershell
git add src/app_review_insights/input_parsing.py tests/test_input_parsing.py docs/data-format.md
git commit -m "feat: add app url parsing and review imports"
```

## 任务 4：清洗、去重并结构化评论

**文件：**
- 创建： `src/app_review_insights/cleaning.py`
- 测试： `tests/test_cleaning.py`

- [x] **步骤 1：编写确定性清洗测试**

```python
# tests/test_cleaning.py
from datetime import UTC, datetime

from app_review_insights.cleaning import clean_reviews
from app_review_insights.models import Review


def review(review_id: str, content: str, rating: int = 1) -> Review:
    return Review(
        review_id=review_id,
        app_id="app-1",
        content_original=content,
        rating=rating,
        published_at=datetime.now(UTC),
        source="fixture",
    )


def test_clean_reviews_removes_exact_and_near_duplicates():
    result = clean_reviews(
        [
            review("r-1", "  Timer   freezes after pause. "),
            review("r-2", "Timer freezes after pause."),
            review("r-3", "Timer freezes after pausing!"),
            review("r-4", "Subscription price is unclear.", rating=2),
        ],
        near_duplicate_threshold=94,
    )

    assert len(result.reviews) == 2
    assert result.stats.input_count == 4
    assert result.stats.exact_duplicates == 1
    assert result.stats.near_duplicates == 1
    assert all(item.content_hash for item in result.reviews)
```

- [x] **步骤 2：运行测试并确认失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_cleaning.py -v`

预期：失败，因为 `clean_reviews` 尚不存在.

- [x] **步骤 3：实现文本规范化、语言检测和去重**

```python
# src/app_review_insights/cleaning.py
import hashlib
import re

from langdetect import DetectorFactory, LangDetectException, detect
from pydantic import BaseModel
from rapidfuzz.fuzz import ratio

from app_review_insights.models import Review

DetectorFactory.seed = 0


class CleaningStats(BaseModel):
    input_count: int
    output_count: int
    exact_duplicates: int
    near_duplicates: int
    empty_removed: int


class CleaningResult(BaseModel):
    reviews: list[Review]
    stats: CleaningStats


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _language(value: str) -> str | None:
    try:
        return detect(value) if len(value) >= 12 else None
    except LangDetectException:
        return None


def clean_reviews(
    reviews: list[Review], near_duplicate_threshold: int = 96
) -> CleaningResult:
    kept: list[Review] = []
    hashes: set[str] = set()
    exact_duplicates = near_duplicates = empty_removed = 0

    for incoming in reviews:
        normalized = normalize_text(incoming.content_original)
        if not normalized:
            empty_removed += 1
            continue
        content_hash = hashlib.sha256(
            f"{incoming.rating}|{normalized.casefold()}".encode("utf-8")
        ).hexdigest()[:20]
        if content_hash in hashes:
            exact_duplicates += 1
            continue
        if any(
            previous.rating == incoming.rating
            and ratio(previous.content_original.casefold(), normalized.casefold())
            >= near_duplicate_threshold
            for previous in kept
        ):
            near_duplicates += 1
            continue
        hashes.add(content_hash)
        kept.append(
            incoming.model_copy(
                update={
                    "content_original": normalized,
                    "content_hash": content_hash,
                    "language": incoming.language or _language(normalized),
                }
            )
        )

    return CleaningResult(
        reviews=kept,
        stats=CleaningStats(
            input_count=len(reviews),
            output_count=len(kept),
            exact_duplicates=exact_duplicates,
            near_duplicates=near_duplicates,
            empty_removed=empty_removed,
        ),
    )
```

- [x] **步骤 4：运行清洗测试并提交**

运行：`.\.venv\Scripts\python -m pytest tests/test_cleaning.py -v`

实际：`5 passed`，提交为 `b4bf1ed`。

```powershell
git add src/app_review_insights/cleaning.py tests/test_cleaning.py
git commit -m "feat: clean and deduplicate review data"
```

## 任务 5：通过适配器采集美国区 App Store 评论

**文件：**
- 创建： `src/app_review_insights/collectors/__init__.py`
- 创建： `src/app_review_insights/collectors/app_store.py`
- 测试： `tests/test_collector.py`

- [x] **步骤 1：使用假 HTTP 客户端编写适配器测试**

```python
# tests/test_collector.py
from app_review_insights.collectors.app_store import AppStoreCollector


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "feed": {
                "entry": [{
                    "id": {"label": "123"},
                    "title": {"label": "Pricing"},
                    "content": {"label": "The free trial renewal date is unclear."},
                    "im:rating": {"label": "2"},
                    "updated": {"label": "2026-08-01T10:00:00-07:00"},
                    "im:version": {"label": "8.5.0"},
                }]
            }
        }


class FakeHttpClient:
    def get(self, url):
        return FakeResponse()


def test_collector_maps_rss_data_to_reviews():
    collector = AppStoreCollector(client=FakeHttpClient())
    reviews = collector.collect(
        "https://apps.apple.com/us/app/workout-for-women-home-gym/id839285684",
        limit=100,
    )

    assert reviews[0].review_id == "123"
    assert reviews[0].storefront == "us"
    assert reviews[0].app_version == "8.5.0"
    assert reviews[0].source == "apple-rss:us"
```

- [x] **步骤 2：运行测试并确认失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_collector.py -v`

预期：失败，因为 采集器尚不存在.

- [x] **步骤 3：实现采集适配器并明确数据限制**

```python
# src/app_review_insights/collectors/app_store.py
from math import ceil

import httpx

from app_review_insights.errors import CollectionError
from app_review_insights.input_parsing import parse_app_store_url
from app_review_insights.models import Review


_RSS_URL = (
    "https://itunes.apple.com/us/rss/customerreviews/page={page}/"
    "id={app_id}/sortby=mostrecent/json"
)
_REVIEWS_PER_PAGE = 50
_MAX_PAGES = 10


class AppStoreCollector:
    def __init__(self, client=None, timeout_seconds: float = 20):
        self.client = client or httpx.Client(
            timeout=timeout_seconds,
            headers={"User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/139"},
            follow_redirects=True,
        )

    def collect(self, app_url: str, limit: int) -> list[Review]:
        parsed = parse_app_store_url(app_url)
        try:
            bounded_limit = max(1, min(limit, _REVIEWS_PER_PAGE * _MAX_PAGES))
            reviews = []
            for page in range(1, min(ceil(bounded_limit / 50), _MAX_PAGES) + 1):
                response = self.client.get(
                    _RSS_URL.format(page=page, app_id=parsed.app_id)
                )
                response.raise_for_status()
                entries = response.json().get("feed", {}).get("entry", []) or []
                for index, item in enumerate(entry for entry in entries if "im:rating" in entry):
                    reviews.append(self._map(item, parsed.app_id, page, index))
                    if len(reviews) >= bounded_limit:
                        return reviews
        except Exception as exc:
            raise CollectionError(
                f"美国区评论采集失败，请稍后重试或改用 JSON/CSV 导入：{exc}"
            ) from exc

        if not reviews:
            raise CollectionError("评论源返回 0 条数据，请改用 JSON/CSV 导入或稍后重试")
        return reviews[:bounded_limit]

    @staticmethod
    def _map(item: dict, app_id: str, page: int, index: int) -> Review:
        def label(key, default=None):
            value = item.get(key, default)
            return value.get("label", default) if isinstance(value, dict) else value

        return Review(
            review_id=str(label("id", f"apple-{page}-{index}")),
            app_id=app_id,
            storefront="us",
            title=str(label("title", "")),
            content_original=str(label("content", "")),
            rating=int(label("im:rating")),
            app_version=label("im:version"),
            published_at=label("updated"),
            source="apple-rss:us",
            source_page=page,
        )
```

```python
# src/app_review_insights/collectors/__init__.py
from .app_store import AppStoreCollector

__all__ = ["AppStoreCollector"]
```

- [x] **步骤 4：运行单元测试**

运行：`.\.venv\Scripts\python -m pytest tests/test_collector.py -v`

实际：`5 passed`。

- [x] **步骤 5：执行一次 20 条评论的手工采集探测，仅保留结果数量、地区和来源**

运行：

```powershell
.\.venv\Scripts\python -c "from app_review_insights.collectors import AppStoreCollector; print(len(AppStoreCollector().collect('https://apps.apple.com/us/app/workout-for-women-home-gym/id839285684', 20)))"
```

实际：返回 `COUNT=20`、`STOREFRONT=us`、`SOURCE=apple-rss:us`。Apple RSS 每页最多 50 条、最多 10 页，因此在线采集上限为 500 条；超过上限会明确提示改用 JSON/CSV 导入，后续页失败时会返回已完成页的评论，单条坏数据不会清空整批结果。

- [x] **步骤 6：提交采集器**

```powershell
git add src/app_review_insights/collectors tests/test_collector.py
git commit -m "feat: collect us app store reviews"
```

提交为 `26e04b1`。

## 任务 6：使用 SQLite 持久化运行、事件和阶段检查点

**文件：**
- 创建： `src/app_review_insights/storage/__init__.py`
- 创建： `src/app_review_insights/storage/repository.py`
- 测试： `tests/test_repository.py`

- [x] **步骤 1：编写仓库存取往返与检查点测试**

```python
# tests/test_repository.py
from datetime import UTC, datetime

from app_review_insights.models import (
    AnalysisRequest,
    RunRecord,
    RunStatus,
    SourceType,
    Stage,
    StageEvent,
)
from app_review_insights.storage.repository import RunRepository


def test_repository_round_trips_run_and_batch_checkpoint(tmp_path):
    repo = RunRepository(tmp_path / "runs.sqlite3")
    now = datetime.now(UTC)
    run = RunRecord(
        run_id="run-1",
        request=AnalysisRequest(source_type=SourceType.ONLINE, analysis_goal="订阅转化"),
        current_stage=Stage.ANALYZE_BATCHES,
        status=RunStatus.RUNNING,
        current_batch=2,
        total_batches=5,
        coverage_ratio=0.4,
        created_at=now,
        updated_at=now,
    )

    repo.save_run(run)
    repo.save_output("run-1", Stage.ANALYZE_BATCHES, {"findings": ["F-1"]}, batch_index=1)
    repo.add_event(
        "run-1",
        StageEvent(
            stage=Stage.ANALYZE_BATCHES,
            status=RunStatus.RUNNING,
            message="完成第 2 批",
            created_at=now,
        ),
    )

    assert repo.get_run("run-1").coverage_ratio == 0.4
    assert repo.get_output("run-1", Stage.ANALYZE_BATCHES, batch_index=1)["findings"] == ["F-1"]
    assert repo.list_events("run-1")[0].message == "完成第 2 批"
```

- [x] **步骤 2：运行测试并确认失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_repository.py -v`

预期：失败，因为 `RunRepository` 尚不存在.

- [x] **步骤 3：实现基于 SQLite 的 JSON 仓库**

```python
# src/app_review_insights/storage/repository.py
import json
import sqlite3
from pathlib import Path

from app_review_insights.models import RunRecord, Stage, StageEvent


class RunRepository:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stage_outputs (
                    run_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    batch_index INTEGER NOT NULL DEFAULT -1,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, stage, batch_index)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                """
            )

    def save_run(self, run: RunRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO runs(run_id, payload_json, updated_at) VALUES (?, ?, ?)",
                (run.run_id, run.model_dump_json(), run.updated_at.isoformat()),
            )

    def get_run(self, run_id: str) -> RunRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return RunRecord.model_validate_json(row["payload_json"])

    def list_runs(self) -> list[RunRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM runs ORDER BY updated_at DESC"
            ).fetchall()
        return [RunRecord.model_validate_json(row["payload_json"]) for row in rows]

    def save_output(
        self, run_id: str, stage: Stage, payload: dict, batch_index: int = -1
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO stage_outputs VALUES (?, ?, ?, ?)",
                (run_id, stage.value, batch_index, json.dumps(payload, ensure_ascii=False)),
            )

    def get_output(self, run_id: str, stage: Stage, batch_index: int = -1) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM stage_outputs WHERE run_id = ? AND stage = ? AND batch_index = ?",
                (run_id, stage.value, batch_index),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def add_event(self, run_id: str, event: StageEvent) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO events(run_id, payload_json) VALUES (?, ?)",
                (run_id, event.model_dump_json()),
            )

    def list_events(self, run_id: str) -> list[StageEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM events WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [StageEvent.model_validate_json(row["payload_json"]) for row in rows]
```

```python
# src/app_review_insights/storage/__init__.py
from .repository import RunRepository

__all__ = ["RunRepository"]
```

- [x] **步骤 4：运行仓库测试并提交**

运行：`.\.venv\Scripts\python -m pytest tests/test_repository.py -v`

实际：`4 passed`，提交为 `60be003`。

```powershell
git add src/app_review_insights/storage tests/test_repository.py
git commit -m "feat: persist pipeline checkpoints"
```

## 任务 7：添加结构化 DeepSeek 调用与字符数感知分批

**文件：**
- 创建： `src/app_review_insights/batching.py`
- 创建： `src/app_review_insights/llm/__init__.py`
- 创建： `src/app_review_insights/llm/provider.py`
- 创建： `src/app_review_insights/llm/schemas.py`
- 测试： `tests/test_provider.py`

- [x] **步骤 1：编写分批与结构化模型供应商测试**

```python
# tests/test_provider.py
from datetime import UTC, datetime

from app_review_insights.batching import make_review_batches
from app_review_insights.llm.provider import DeepSeekProvider
from app_review_insights.llm.schemas import BatchAnalysisResult
from app_review_insights.models import Review


def test_batching_respects_count_and_character_limits():
    reviews = [
        Review(
            review_id=f"r-{index}",
            app_id="app-1",
            content_original="x" * 700,
            rating=1,
            published_at=datetime.now(UTC),
            source="fixture",
        )
        for index in range(5)
    ]
    batches = make_review_batches(reviews, max_reviews=100, max_characters=1500)
    assert [len(batch) for batch in batches] == [2, 2, 1]


class FakeCompletions:
    def create(self, **kwargs):
        message = type("Message", (), {"content": '{"findings": [], "batch_limitations": []}'})
        choice = type("Choice", (), {"message": message})
        return type("Response", (), {"choices": [choice]})


def test_provider_validates_json_against_schema():
    fake_client = type(
        "Client", (), {"chat": type("Chat", (), {"completions": FakeCompletions()})()}
    )()
    provider = DeepSeekProvider(client=fake_client, model="deepseek-chat", max_retries=0)

    result = provider.generate("system", "user", BatchAnalysisResult)
    assert result.findings == []
```

- [x] **步骤 2：运行测试并确认失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_provider.py -v`

预期：失败，因为 分批与模型供应商模块尚不存在.

- [x] **步骤 3：定义模型专用草稿 Schema**

```python
# src/app_review_insights/llm/schemas.py
from typing import Literal

from pydantic import BaseModel, Field


class FindingDraft(BaseModel):
    title: str
    problem_statement: str
    topic_label: str
    supporting_review_ids: list[str]
    conflicting_review_ids: list[str] = Field(default_factory=list)
    reasoning_summary: str
    limitations: list[str] = Field(default_factory=list)


class BatchAnalysisResult(BaseModel):
    findings: list[FindingDraft]
    batch_limitations: list[str] = Field(default_factory=list)


class ConsolidationResult(BaseModel):
    findings: list[FindingDraft]


class RequirementDraft(BaseModel):
    finding_ids: list[str]
    title: str
    user_problem: str
    objective: str
    scope: list[str]
    non_goals: list[str]
    functional_rules: list[str]
    edge_cases: list[str]
    acceptance_criteria: list[str]
    success_metrics: list[str]
    impact: int = Field(ge=1, le=5)
    complexity: Literal["low", "medium", "high"]
    proposed_version: Literal["V1.0", "V1.1", "Future"]
    assumptions: list[str] = Field(default_factory=list)


class RequirementPlanResult(BaseModel):
    requirements: list[RequirementDraft] = Field(default_factory=list, max_length=10)


class TestCaseDraft(BaseModel):
    requirement_id: str
    title: str
    preconditions: list[str]
    steps: list[str]
    expected_result: str
    case_type: Literal["normal", "exception", "boundary", "regression"]


class TestCasePlanResult(BaseModel):
    test_cases: list[TestCaseDraft]
```

- [x] **步骤 4：实现字符数感知分批**

```python
# src/app_review_insights/batching.py
from app_review_insights.models import Review


def make_review_batches(
    reviews: list[Review], max_reviews: int, max_characters: int
) -> list[list[Review]]:
    batches: list[list[Review]] = []
    current: list[Review] = []
    current_characters = 0
    for review in reviews:
        review_characters = len(review.content_original)
        would_overflow = current and (
            len(current) >= max_reviews
            or current_characters + review_characters > max_characters
        )
        if would_overflow:
            batches.append(current)
            current = []
            current_characters = 0
        current.append(review)
        current_characters += review_characters
    if current:
        batches.append(current)
    return batches
```

- [x] **步骤 5：实现带有限重试的模型供应商**

```python
# src/app_review_insights/llm/provider.py
import time
from typing import TypeVar

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from app_review_insights.errors import RecoverableModelError

T = TypeVar("T", bound=BaseModel)


class DeepSeekProvider:
    def __init__(
        self,
        client: OpenAI,
        model: str,
        max_retries: int = 2,
    ):
        self.client = client
        self.model = model
        self.max_retries = max_retries

    @classmethod
    def from_settings(cls, settings):
        return cls(
            client=OpenAI(
                api_key=settings.deepseek_api_key,
                base_url=settings.model_base_url,
                timeout=settings.model_timeout_seconds,
            ),
            model=settings.model_name,
            max_retries=settings.model_max_retries,
        )

    def generate(self, system_prompt: str, user_prompt: str, schema: type[T]) -> T:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    temperature=0.1,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                )
                content = response.choices[0].message.content or "{}"
                return schema.model_validate_json(content)
            except (Exception, ValidationError) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(2**attempt)
        raise RecoverableModelError(f"模型调用失败，可从检查点继续: {last_error}")
```

```python
# src/app_review_insights/llm/__init__.py
from .provider import DeepSeekProvider

__all__ = ["DeepSeekProvider"]
```

- [x] **步骤 6：运行模型供应商测试并提交**

运行：`.\.venv\Scripts\python -m pytest tests/test_provider.py -v`

实际：结构化调用、字符数分批和重试边界均已覆盖；OpenAI SDK 内层重试关闭，由应用层统一控制有限重试预算。初始实现提交为 `9b8f924`，审查修复提交为 `cb22e41`、`7761c40`。

```powershell
git add src/app_review_insights/batching.py src/app_review_insights/llm tests/test_provider.py
git commit -m "feat: add structured deepseek provider"
```

## 任务 8：分析评论批次并归并动态发现

**文件：**
- 创建： `src/app_review_insights/llm/prompts.py`
- 创建： `src/app_review_insights/pipeline/__init__.py`
- 创建： `src/app_review_insights/pipeline/analyze.py`
- 测试： `tests/test_analysis.py`

- [x] **步骤 1：使用假模型供应商编写分析服务测试**

```python
# tests/test_analysis.py
from datetime import UTC, datetime

from app_review_insights.llm.schemas import BatchAnalysisResult, ConsolidationResult, FindingDraft
from app_review_insights.models import Review
from app_review_insights.pipeline.analyze import analyze_batch, consolidate_findings


class QueueProvider:
    def __init__(self, results):
        self.results = list(results)

    def generate(self, system_prompt, user_prompt, schema):
        return self.results.pop(0)


def test_analysis_preserves_review_ids_and_goal():
    provider = QueueProvider(
        [
            BatchAnalysisResult(
                findings=[
                    FindingDraft(
                        title="Trial terms unclear",
                        problem_statement="Users cannot see renewal terms before purchase.",
                        topic_label="subscription transparency",
                        supporting_review_ids=["r-1"],
                        reasoning_summary="The review explicitly mentions renewal terms.",
                    )
                ]
            )
        ]
    )
    reviews = [
        Review(
            review_id="r-1",
            app_id="app-1",
            content_original="I could not see when the free trial renews.",
            rating=2,
            published_at=datetime.now(UTC),
            source="fixture",
        )
    ]

    result = analyze_batch(provider, reviews, "重点分析订阅转化")
    assert result.findings[0].supporting_review_ids == ["r-1"]


def test_consolidation_returns_cross_batch_topics():
    provider = QueueProvider([ConsolidationResult(findings=[])])
    result = consolidate_findings(provider, [], "订阅转化")
    assert result.findings == []
```

- [x] **步骤 2：运行测试并确认失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_analysis.py -v`

预期：失败，因为 分析函数尚不存在.

- [x] **步骤 3：添加禁止无依据统计的 Prompt**

```python
# src/app_review_insights/llm/prompts.py
BATCH_SYSTEM_PROMPT = """你是证据约束的产品研究分析师。
只允许根据提供的评论归纳问题。每个结论必须引用 review_id。
不得编造数量、比例、版本信息或用户动机；不得使用固定关键词分类表。
识别与分析目标相关的动态主题、具体问题、支持证据、冲突证据和局限。
输出必须满足提供的 JSON schema。"""

CONSOLIDATE_SYSTEM_PROMPT = """你负责合并多个评论批次的候选发现。
合并同义问题但不要过度合并不同用户场景。保留所有有效 review_id，显式保留冲突证据。
不得新增输入中不存在的 review_id、统计数字或产品事实。输出必须满足 JSON schema。"""


def render_reviews(reviews) -> str:
    lines = []
    for review in reviews:
        lines.append(
            f"review_id={review.review_id} | rating={review.rating} | "
            f"version={review.app_version or 'unknown'} | text={review.content_original}"
        )
    return "\n".join(lines)
```

- [x] **步骤 4：实现批次分析与归并**

```python
# src/app_review_insights/pipeline/analyze.py
import json

from app_review_insights.llm.prompts import (
    BATCH_SYSTEM_PROMPT,
    CONSOLIDATE_SYSTEM_PROMPT,
    render_reviews,
)
from app_review_insights.llm.schemas import BatchAnalysisResult, ConsolidationResult


def analyze_batch(provider, reviews, analysis_goal: str) -> BatchAnalysisResult:
    prompt = (
        f"分析目标：{analysis_goal}\n\n"
        "评论数据：\n"
        f"{render_reviews(reviews)}\n\n"
        "请动态发现具体用户问题，并仅引用以上 review_id。"
    )
    return provider.generate(BATCH_SYSTEM_PROMPT, prompt, BatchAnalysisResult)


def consolidate_findings(provider, batch_results, analysis_goal: str) -> ConsolidationResult:
    payload = [result.model_dump() for result in batch_results]
    prompt = (
        f"分析目标：{analysis_goal}\n"
        f"候选发现：{json.dumps(payload, ensure_ascii=False)}"
    )
    return provider.generate(CONSOLIDATE_SYSTEM_PROMPT, prompt, ConsolidationResult)
```

```python
# src/app_review_insights/pipeline/__init__.py
"""Evidence-grounded analysis pipeline."""
```

- [x] **步骤 5：运行分析测试并提交**

运行：`.\.venv\Scripts\python -m pytest tests/test_analysis.py -v`

预期：`2 passed`.

```powershell
git add src/app_review_insights/llm/prompts.py src/app_review_insights/pipeline tests/test_analysis.py
git commit -m "feat: analyze and consolidate review findings"
```

## 任务 9：校验证据并确定性重算置信度

**文件：**
- 创建： `src/app_review_insights/pipeline/validate.py`
- 测试： `tests/test_validation.py`

- [x] **步骤 1：编写虚构 ID 与自适应证据状态测试**

```python
# tests/test_validation.py
from datetime import UTC, datetime

from app_review_insights.llm.schemas import FindingDraft
from app_review_insights.models import EvidenceStatus, Review
from app_review_insights.pipeline.validate import validate_finding_drafts


def test_validation_removes_invented_ids_and_recomputes_counts():
    reviews = [
        Review(
            review_id="r-1",
            app_id="app-1",
            content_original="Renewal date is unclear.",
            rating=2,
            published_at=datetime.now(UTC),
            source="fixture",
        ),
        Review(
            review_id="r-2",
            app_id="app-1",
            content_original="The trial explanation was clear.",
            rating=5,
            published_at=datetime.now(UTC),
            source="fixture",
        ),
    ]
    drafts = [
        FindingDraft(
            title="Trial clarity",
            problem_statement="Some users cannot understand renewal timing.",
            topic_label="subscription",
            supporting_review_ids=["r-1", "invented"],
            conflicting_review_ids=["r-2"],
            reasoning_summary="Explicit opposing feedback exists.",
        )
    ]

    findings, report = validate_finding_drafts(drafts, reviews)
    assert findings[0].support_count == 1
    assert findings[0].conflict_count == 1
    assert findings[0].supporting_review_ids == ["r-1"]
    assert findings[0].evidence_status == EvidenceStatus.ASSUMPTION
    assert report.valid is False
```

- [x] **步骤 2：运行测试并确认失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_validation.py -v`

预期：失败，因为 校验模块尚不存在.

- [x] **步骤 3：实现确定性引用与置信度校验**

```python
# src/app_review_insights/pipeline/validate.py
import math

from app_review_insights.llm.schemas import FindingDraft
from app_review_insights.models import (
    EvidenceStatus,
    Finding,
    Review,
    ValidationIssue,
    ValidationReport,
)


def _confidence(support: int, conflicts: int) -> float:
    volume = min(1.0, math.log1p(support) / math.log1p(10))
    conflict_ratio = conflicts / max(1, support + conflicts)
    return round(max(0.0, volume * (1 - 0.5 * conflict_ratio)), 2)


def validate_finding_drafts(
    drafts: list[FindingDraft], reviews: list[Review]
) -> tuple[list[Finding], ValidationReport]:
    review_ids = {review.review_id for review in reviews}
    min_support = 1 if len(reviews) < 20 else 2
    findings: list[Finding] = []
    issues: list[ValidationIssue] = []

    for index, draft in enumerate(drafts, start=1):
        valid_support = list(dict.fromkeys(r for r in draft.supporting_review_ids if r in review_ids))
        valid_conflicts = list(
            dict.fromkeys(r for r in draft.conflicting_review_ids if r in review_ids and r not in valid_support)
        )
        invalid = (set(draft.supporting_review_ids) | set(draft.conflicting_review_ids)) - review_ids
        if invalid:
            issues.append(
                ValidationIssue(
                    entity_type="finding",
                    entity_id=f"F-{index:03d}",
                    rule="review_reference_exists",
                    severity="error",
                    message=f"删除不存在的评论引用: {sorted(invalid)}",
                    revision_action="remove_invalid_references",
                )
            )
        status = (
            EvidenceStatus.VALIDATED
            if len(valid_support) >= min_support
            else EvidenceStatus.ASSUMPTION
        )
        if not valid_support:
            status = EvidenceStatus.REJECTED
        findings.append(
            Finding(
                finding_id=f"F-{index:03d}",
                title=draft.title,
                problem_statement=draft.problem_statement,
                topic_label=draft.topic_label,
                supporting_review_ids=valid_support,
                conflicting_review_ids=valid_conflicts,
                support_count=len(valid_support),
                conflict_count=len(valid_conflicts),
                confidence=_confidence(len(valid_support), len(valid_conflicts)),
                evidence_status=status,
                model_reasoning_summary=draft.reasoning_summary,
                limitations=draft.limitations,
            )
        )
    return findings, ValidationReport(valid=not any(i.severity == "error" for i in issues), issues=issues)
```

- [x] **步骤 4：运行校验测试并提交**

运行：`.\.venv\Scripts\python -m pytest tests/test_validation.py -v`

预期：`1 passed`.

```powershell
git add src/app_review_insights/pipeline/validate.py tests/test_validation.py
git commit -m "feat: validate review evidence"
```

## 任务 10：生成分版本 PRD 需求和可追溯测试用例

**文件：**
- 创建： `src/app_review_insights/pipeline/planning.py`
- 创建： `src/app_review_insights/pipeline/test_generation.py`
- 测试： `tests/test_planning.py`
- 测试： `tests/test_test_generation.py`

- [x] **步骤 1：编写规划与测试生成测试**

```python
# tests/test_planning.py
from app_review_insights.llm.schemas import RequirementDraft, RequirementPlanResult
from app_review_insights.models import EvidenceStatus, Finding
from app_review_insights.pipeline.planning import build_requirements


class Provider:
    def generate(self, system_prompt, user_prompt, schema):
        return RequirementPlanResult(
            requirements=[
                RequirementDraft(
                    finding_ids=["F-001"],
                    title="Show renewal terms",
                    user_problem="Users cannot see trial renewal timing.",
                    objective="Make purchase terms understandable before confirmation.",
                    scope=["Show renewal date and price"],
                    non_goals=["Redesign all account settings"],
                    functional_rules=["Render terms before payment confirmation"],
                    edge_cases=["Store price is temporarily unavailable"],
                    acceptance_criteria=["Terms are visible without opening another page"],
                    success_metrics=["Reduce related low-rating reviews"],
                    impact=5,
                    complexity="low",
                    proposed_version="V1.0",
                )
            ]
        )


def test_build_requirements_attaches_reviews_and_priority():
    finding = Finding(
        finding_id="F-001",
        title="Trial terms unclear",
        problem_statement="Users cannot see renewal timing.",
        topic_label="subscription",
        supporting_review_ids=["r-1", "r-2"],
        support_count=2,
        confidence=0.8,
        evidence_status=EvidenceStatus.VALIDATED,
        model_reasoning_summary="Direct evidence",
    )
    requirements = build_requirements(Provider(), [finding], "订阅转化", total_reviews=100)
    assert requirements[0].source_review_ids == ["r-1", "r-2"]
    assert requirements[0].priority_score > 0
    assert requirements[0].target_version == "V1.0"
```

```python
# tests/test_test_generation.py
from app_review_insights.llm.schemas import TestCaseDraft, TestCasePlanResult
from app_review_insights.models import Requirement
from app_review_insights.pipeline.test_generation import generate_test_cases


class Provider:
    def generate(self, system_prompt, user_prompt, schema):
        return TestCasePlanResult(
            test_cases=[
                TestCaseDraft(
                    requirement_id="REQ-001",
                    title="Renewal date is visible",
                    preconditions=["Trial is available"],
                    steps=["Open paywall"],
                    expected_result="Price and renewal date are shown.",
                    case_type="normal",
                )
            ]
        )


def test_test_cases_inherit_requirement_review_ids():
    requirement = Requirement(
        requirement_id="REQ-001",
        finding_ids=["F-001"],
        title="Show terms",
        user_problem="Terms are unclear",
        objective="Improve clarity",
        scope=["Show terms"],
        non_goals=[],
        functional_rules=["Show before confirmation"],
        edge_cases=[],
        acceptance_criteria=["Visible"],
        success_metrics=["Fewer complaints"],
        impact=5,
        complexity="low",
        priority_score=8.0,
        target_version="V1.0",
        source_review_ids=["r-1"],
    )
    cases = generate_test_cases(Provider(), [requirement])
    assert cases[0].source_review_ids == ["r-1"]
```

- [x] **步骤 2：运行测试并确认失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_planning.py tests/test_test_generation.py -v`

预期：失败，因为 规划模块尚不存在.

- [x] **步骤 3：实现需求生成与确定性优先级评分**

```python
# src/app_review_insights/pipeline/planning.py
import json

from app_review_insights.llm.schemas import RequirementPlanResult
from app_review_insights.models import EvidenceStatus, Finding, Requirement

_COMPLEXITY_COST = {"low": 1, "medium": 2, "high": 3}


def build_requirements(provider, findings: list[Finding], analysis_goal: str, total_reviews: int):
    eligible = [finding for finding in findings if finding.evidence_status != EvidenceStatus.REJECTED]
    if not eligible:
        return []
    result = provider.generate(
        "你是产品经理。只基于输入 Findings 生成 5-10 个可实施、可测试的需求；不要新增评论事实。",
        json.dumps(
            {"goal": analysis_goal, "findings": [f.model_dump() for f in eligible]},
            ensure_ascii=False,
        ),
        RequirementPlanResult,
    )
    finding_index = {finding.finding_id: finding for finding in eligible}
    requirements: list[Requirement] = []
    for index, draft in enumerate(result.requirements, start=1):
        linked = [finding_index[item] for item in draft.finding_ids if item in finding_index]
        if not linked:
            continue
        source_ids = list(dict.fromkeys(r for finding in linked for r in finding.supporting_review_ids))
        support = len(source_ids)
        confidence = sum(f.confidence for f in linked) / len(linked)
        frequency = support / max(total_reviews, 1)
        score = round(draft.impact * frequency * confidence * 100 / _COMPLEXITY_COST[draft.complexity], 2)
        has_assumption = any(f.evidence_status == EvidenceStatus.ASSUMPTION for f in linked)
        target_version = "Future" if has_assumption else draft.proposed_version
        requirements.append(
            Requirement(
                requirement_id=f"REQ-{index:03d}",
                finding_ids=[finding.finding_id for finding in linked],
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
                priority_score=score,
                target_version=target_version,
                source_review_ids=source_ids,
                assumptions=draft.assumptions,
            )
        )
    return sorted(requirements, key=lambda item: item.priority_score, reverse=True)
```

- [x] **步骤 4：实现测试用例生成与继承式追溯**

```python
# src/app_review_insights/pipeline/test_generation.py
import json

from app_review_insights.llm.schemas import TestCasePlanResult
from app_review_insights.models import Requirement, TestCase


def generate_test_cases(provider, requirements: list[Requirement]) -> list[TestCase]:
    result = provider.generate(
        "你是 QA。每个需求生成 2-4 条可执行用例，覆盖正常、异常、边界或回归场景。不得新增需求。",
        json.dumps([item.model_dump() for item in requirements], ensure_ascii=False),
        TestCasePlanResult,
    )
    requirement_index = {item.requirement_id: item for item in requirements}
    cases: list[TestCase] = []
    for index, draft in enumerate(result.test_cases, start=1):
        requirement = requirement_index.get(draft.requirement_id)
        if requirement is None:
            continue
        cases.append(
            TestCase(
                test_case_id=f"TC-{index:03d}",
                requirement_id=requirement.requirement_id,
                title=draft.title,
                preconditions=draft.preconditions,
                steps=draft.steps,
                expected_result=draft.expected_result,
                case_type=draft.case_type,
                source_review_ids=requirement.source_review_ids,
            )
        )
    return cases
```

- [x] **步骤 5：运行测试并提交**

运行：`.\.venv\Scripts\python -m pytest tests/test_planning.py tests/test_test_generation.py -v`

预期：`2 passed`.

```powershell
git add src/app_review_insights/pipeline/planning.py src/app_review_insights/pipeline/test_generation.py tests/test_planning.py tests/test_test_generation.py
git commit -m "feat: generate versioned prd and test cases"
```

## 任务 11：强制校验完整追溯链并导出产物

**文件：**
- 创建： `src/app_review_insights/pipeline/traceability.py`
- 创建： `src/app_review_insights/export.py`
- 测试： `tests/test_traceability.py`
- 测试： `tests/test_export.py`

- [x] **步骤 1：编写追溯与导出测试**

```python
# tests/test_traceability.py
from app_review_insights.models import EvidenceStatus, Finding, Requirement, TestCase
from app_review_insights.pipeline.traceability import validate_traceability


def test_traceability_rejects_test_case_without_valid_review_path():
    finding = Finding(
        finding_id="F-001",
        title="Problem",
        problem_statement="Problem",
        topic_label="topic",
        supporting_review_ids=["r-1"],
        support_count=1,
        confidence=0.5,
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
        source_review_ids=["r-1"],
    )
    case = TestCase(
        test_case_id="TC-001",
        requirement_id="REQ-001",
        title="Case",
        preconditions=[],
        steps=["Act"],
        expected_result="Result",
        case_type="normal",
        source_review_ids=["invented"],
    )
    report = validate_traceability({"r-1"}, [finding], [requirement], [case])
    assert report.valid is False
```

```python
# tests/test_export.py
from app_review_insights.export import build_traceability_rows


def test_traceability_export_contains_all_entity_ids():
    rows = build_traceability_rows(
        findings={"F-1": ["r-1"]},
        requirements={"REQ-1": ["F-1"]},
        test_cases={"TC-1": "REQ-1"},
    )
    assert rows == [
        {
            "review_ids": "r-1",
            "finding_id": "F-1",
            "requirement_id": "REQ-1",
            "test_case_id": "TC-1",
        }
    ]
```

- [x] **步骤 2：运行测试并确认失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_traceability.py tests/test_export.py -v`

预期：失败，因为 追溯与导出模块尚不存在.

- [x] **步骤 3：实现完整链路校验**

```python
# src/app_review_insights/pipeline/traceability.py
from app_review_insights.models import ValidationIssue, ValidationReport


def validate_traceability(review_ids, findings, requirements, test_cases) -> ValidationReport:
    issues: list[ValidationIssue] = []
    finding_index = {item.finding_id: item for item in findings}
    requirement_index = {item.requirement_id: item for item in requirements}

    for finding in findings:
        if not set(finding.supporting_review_ids).issubset(review_ids):
            issues.append(ValidationIssue(
                entity_type="finding", entity_id=finding.finding_id,
                rule="review_to_finding", severity="error",
                message="Finding 引用了不存在的评论。"
            ))
    for requirement in requirements:
        if not requirement.finding_ids or any(item not in finding_index for item in requirement.finding_ids):
            issues.append(ValidationIssue(
                entity_type="requirement", entity_id=requirement.requirement_id,
                rule="finding_to_requirement", severity="error",
                message="Requirement 缺少有效 Finding。"
            ))
        expected_reviews = {
            review_id
            for finding_id in requirement.finding_ids
            if finding_id in finding_index
            for review_id in finding_index[finding_id].supporting_review_ids
        }
        if not set(requirement.source_review_ids).issubset(expected_reviews):
            issues.append(ValidationIssue(
                entity_type="requirement", entity_id=requirement.requirement_id,
                rule="requirement_reviews_inherit_findings", severity="error",
                message="Requirement 评论来源不能回溯到 Finding。"
            ))
    for case in test_cases:
        requirement = requirement_index.get(case.requirement_id)
        if requirement is None or not set(case.source_review_ids).issubset(set(requirement.source_review_ids)):
            issues.append(ValidationIssue(
                entity_type="test_case", entity_id=case.test_case_id,
                rule="requirement_to_test_case", severity="error",
                message="TestCase 缺少有效 Requirement/Review 路径。"
            ))
    return ValidationReport(valid=not issues, issues=issues)
```

- [x] **步骤 4：实现导出行构造器**

```python
# src/app_review_insights/export.py
import csv
import io
import json


def build_traceability_rows(findings, requirements, test_cases):
    rows = []
    for test_case_id, requirement_id in test_cases.items():
        for finding_id in requirements.get(requirement_id, []):
            rows.append({
                "review_ids": ",".join(findings.get(finding_id, [])),
                "finding_id": finding_id,
                "requirement_id": requirement_id,
                "test_case_id": test_case_id,
            })
    return rows


def to_json_bytes(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def rows_to_csv_bytes(rows: list[dict]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]) if rows else [])
    if rows:
        writer.writeheader()
        writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")
```

- [x] **步骤 5：运行测试并提交**

运行：`.\.venv\Scripts\python -m pytest tests/test_traceability.py tests/test_export.py -v`

预期：`2 passed`.

```powershell
git add src/app_review_insights/pipeline/traceability.py src/app_review_insights/export.py tests/test_traceability.py tests/test_export.py
git commit -m "feat: validate and export traceability chain"
```

## 任务 12：构建带检查点的编排器与续跑流程

**文件：**
- 创建： `src/app_review_insights/pipeline/orchestrator.py`
- 测试： `tests/test_orchestrator.py`

- [x] **步骤 1：编写检查点、同一运行续跑和完成测试**

```python
# tests/test_orchestrator.py
from datetime import UTC, datetime

from app_review_insights.errors import CollectionError, RecoverableModelError
from app_review_insights.llm.schemas import BatchAnalysisResult, ConsolidationResult
from app_review_insights.models import (
    AnalysisRequest,
    RunStatus,
    Review,
    SourceType,
    Stage,
    ValidationReport,
)
from app_review_insights.pipeline.orchestrator import AnalysisOrchestrator, PipelineServices
from app_review_insights.storage.repository import RunRepository


class FailOnceOnSecondBatch:
    def __init__(self):
        self.calls = 0
        self.failed = False

    def __call__(self, reviews, goal):
        self.calls += 1
        if self.calls == 2 and not self.failed:
            self.failed = True
            raise RecoverableModelError("temporary failure")
        return BatchAnalysisResult(findings=[], batch_limitations=[])


def make_reviews(count: int) -> list[Review]:
    return [
        Review(
            review_id=f"r-{index}",
            app_id="app",
            content_original=f"review content {index}",
            rating=1,
            published_at=datetime.now(UTC),
            source="fixture",
        )
        for index in range(count)
    ]


def make_services(repo, analyzer):
    return PipelineServices(
        repository=repo,
        batch_analyzer=analyzer,
        consolidator=lambda results, goal: ConsolidationResult(findings=[]),
        finding_validator=lambda drafts, reviews: ([], ValidationReport(valid=True)),
        requirement_builder=lambda findings, goal, total: [],
        test_case_builder=lambda requirements: [],
        traceability_validator=lambda review_ids, findings, requirements, cases: ValidationReport(
            valid=True
        ),
        batch_size=2,
        batch_max_characters=10000,
    )


def test_orchestrator_resumes_same_run_without_repeating_completed_batch(tmp_path):
    repo = RunRepository(tmp_path / "runs.sqlite3")
    analyzer = FailOnceOnSecondBatch()
    orchestrator = AnalysisOrchestrator(make_services(repo, analyzer))
    request = AnalysisRequest(source_type=SourceType.JSON, analysis_goal="查找问题")

    waiting = orchestrator.start(request, imported_reviews=make_reviews(4))

    assert waiting.status == RunStatus.WAITING
    assert waiting.current_stage == Stage.ANALYZE_BATCHES
    assert waiting.current_batch == 1
    assert repo.get_output(waiting.run_id, Stage.ANALYZE_BATCHES, batch_index=0) is not None

    completed = orchestrator.resume(waiting.run_id)

    assert completed.run_id == waiting.run_id
    assert completed.status == RunStatus.COMPLETED
    assert completed.current_stage == Stage.COMPLETE
    assert analyzer.calls == 3
    assert len(repo.list_events(waiting.run_id)) >= 5


def test_orchestrator_persists_raw_and_cleaned_reviews(tmp_path):
    repo = RunRepository(tmp_path / "runs.sqlite3")
    analyzer = FailOnceOnSecondBatch()
    analyzer.failed = True
    orchestrator = AnalysisOrchestrator(make_services(repo, analyzer))

    completed = orchestrator.start(
        AnalysisRequest(source_type=SourceType.JSON, analysis_goal="查找问题"),
        imported_reviews=make_reviews(2),
    )

    assert completed.status == RunStatus.COMPLETED
    assert len(repo.get_output(completed.run_id, Stage.COLLECT)["reviews"]) == 2
    assert len(repo.get_output(completed.run_id, Stage.CLEAN)["reviews"]) == 2
```

- [x] **步骤 2：运行测试并确认失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_orchestrator.py -v`

预期：失败，因为 编排器尚不存在.

- [x] **步骤 3：实现完整的可续跑编排器**

```python
# src/app_review_insights/pipeline/orchestrator.py
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable
from uuid import uuid4

from app_review_insights.batching import make_review_batches
from app_review_insights.cleaning import clean_reviews
from app_review_insights.errors import RecoverableModelError
from app_review_insights.llm.schemas import BatchAnalysisResult, ConsolidationResult
from app_review_insights.models import (
    AnalysisRequest,
    Finding,
    Requirement,
    Review,
    RunRecord,
    RunStatus,
    Stage,
    StageEvent,
    TestCase,
    ValidationReport,
)


@dataclass
class PipelineServices:
    repository: object
    batch_analyzer: Callable
    collector: object | None = None
    consolidator: Callable | None = None
    finding_validator: Callable | None = None
    requirement_builder: Callable | None = None
    test_case_builder: Callable | None = None
    traceability_validator: Callable | None = None
    batch_size: int = 100
    batch_max_characters: int = 60000


class AnalysisOrchestrator:
    def __init__(self, services: PipelineServices, on_event: Callable | None = None):
        self.services = services
        self.on_event = on_event or (lambda event: None)

    @property
    def repository(self):
        return self.services.repository

    def _save(self, run: RunRecord, message: str) -> RunRecord:
        run.updated_at = datetime.now(UTC)
        self.repository.save_run(run)
        event = StageEvent(
            stage=run.current_stage,
            status=run.status,
            message=message,
            created_at=run.updated_at,
        )
        self.repository.add_event(run.run_id, event)
        self.on_event(event)
        return run

    def _wait(self, run: RunRecord, stage: Stage, exc: Exception) -> RunRecord:
        run.current_stage = stage
        run.status = RunStatus.WAITING
        run.last_error = str(exc)
        return self._save(run, "模型暂时不可用，已保存当前检查点")

    def start(
        self,
        request: AnalysisRequest,
        imported_reviews: list[Review] | None = None,
    ) -> RunRecord:
        now = datetime.now(UTC)
        run = RunRecord(
            run_id=str(uuid4()),
            request=request,
            current_stage=Stage.SCOPE,
            status=RunStatus.RUNNING,
            created_at=now,
            updated_at=now,
        )
        self._save(run, "已确定分析目标和输入范围")
        return self._execute(run, imported_reviews)

    def resume(self, run_id: str) -> RunRecord:
        run = self.repository.get_run(run_id)
        if run.status != RunStatus.WAITING:
            return run
        run.status = RunStatus.RUNNING
        run.last_error = None
        self._save(run, "继续执行未完成阶段")
        return self._execute(run, imported_reviews=None)

    def _execute(
        self,
        run: RunRecord,
        imported_reviews: list[Review] | None,
    ) -> RunRecord:
        collected_payload = self.repository.get_output(run.run_id, Stage.COLLECT)
        if collected_payload is None:
            reviews = imported_reviews
            if reviews is None:
                if not run.request.app_url or self.services.collector is None:
                    raise ValueError("online runs require app_url and collector")
                try:
                    reviews = self.services.collector.collect(
                        run.request.app_url, run.request.review_limit
                    )
                except CollectionError as exc:
                    run.current_stage = Stage.COLLECT
                    run.status = RunStatus.FAILED
                    run.last_error = str(exc)
                    return self._save(run, "在线评论采集失败，可改用导入数据或稍后重试")
            self.repository.save_output(
                run.run_id,
                Stage.COLLECT,
                {"reviews": [item.model_dump(mode="json") for item in reviews]},
            )
            run.current_stage = Stage.COLLECT
            self._save(run, f"获得 {len(reviews)} 条原始评论")
        else:
            reviews = [Review.model_validate(item) for item in collected_payload["reviews"]]

        cleaned_payload = self.repository.get_output(run.run_id, Stage.CLEAN)
        if cleaned_payload is None:
            cleaned = clean_reviews(reviews)
            cleaned_payload = cleaned.model_dump(mode="json")
            self.repository.save_output(run.run_id, Stage.CLEAN, cleaned_payload)
            run.current_stage = Stage.CLEAN
            self._save(run, f"清洗完成，保留 {len(cleaned.reviews)} 条评论")
        clean_reviews_list = [Review.model_validate(item) for item in cleaned_payload["reviews"]]

        batches = make_review_batches(
            clean_reviews_list,
            self.services.batch_size,
            self.services.batch_max_characters,
        )
        run.total_batches = len(batches)
        batch_results: list[BatchAnalysisResult] = []
        for index, batch in enumerate(batches):
            payload = self.repository.get_output(
                run.run_id, Stage.ANALYZE_BATCHES, batch_index=index
            )
            if payload is None:
                try:
                    result = self.services.batch_analyzer(batch, run.request.analysis_goal)
                except RecoverableModelError as exc:
                    run.current_batch = index
                    run.coverage_ratio = index / max(1, len(batches))
                    return self._wait(run, Stage.ANALYZE_BATCHES, exc)
                payload = result.model_dump(mode="json")
                self.repository.save_output(
                    run.run_id, Stage.ANALYZE_BATCHES, payload, batch_index=index
                )
                run.current_batch = index + 1
                run.coverage_ratio = (index + 1) / max(1, len(batches))
                run.current_stage = Stage.ANALYZE_BATCHES
                self._save(run, f"完成评论分析批次 {index + 1}/{len(batches)}")
            batch_results.append(BatchAnalysisResult.model_validate(payload))

        consolidated_payload = self.repository.get_output(run.run_id, Stage.CONSOLIDATE)
        if consolidated_payload is None:
            try:
                consolidated = self.services.consolidator(
                    batch_results, run.request.analysis_goal
                )
            except RecoverableModelError as exc:
                return self._wait(run, Stage.CONSOLIDATE, exc)
            consolidated_payload = consolidated.model_dump(mode="json")
            self.repository.save_output(run.run_id, Stage.CONSOLIDATE, consolidated_payload)
            run.current_stage = Stage.CONSOLIDATE
            self._save(run, "候选问题跨批次归并完成")
        consolidated = ConsolidationResult.model_validate(consolidated_payload)

        finding_payload = self.repository.get_output(run.run_id, Stage.VALIDATE_FINDINGS)
        if finding_payload is None:
            findings, finding_report = self.services.finding_validator(
                consolidated.findings, clean_reviews_list
            )
            finding_payload = {
                "findings": [item.model_dump(mode="json") for item in findings],
                "validation": finding_report.model_dump(mode="json"),
            }
            self.repository.save_output(run.run_id, Stage.VALIDATE_FINDINGS, finding_payload)
            run.current_stage = Stage.VALIDATE_FINDINGS
            self._save(run, "发现证据校验完成")
        findings = [Finding.model_validate(item) for item in finding_payload["findings"]]

        plan_payload = self.repository.get_output(run.run_id, Stage.PLAN)
        if plan_payload is None:
            try:
                requirements = self.services.requirement_builder(
                    findings, run.request.analysis_goal, len(clean_reviews_list)
                )
            except RecoverableModelError as exc:
                return self._wait(run, Stage.PLAN, exc)
            plan_payload = {
                "requirements": [item.model_dump(mode="json") for item in requirements],
                "quantity_notice": (
                    "可验证证据不足，因此本次输出少于 5 个核心需求。"
                    if len(requirements) < 5
                    else None
                ),
            }
            self.repository.save_output(run.run_id, Stage.PLAN, plan_payload)
            run.current_stage = Stage.PLAN
            self._save(run, "版本规划与 PRD 生成完成")
        requirements = [Requirement.model_validate(item) for item in plan_payload["requirements"]]

        test_payload = self.repository.get_output(run.run_id, Stage.GENERATE_TESTS)
        if test_payload is None:
            try:
                test_cases = self.services.test_case_builder(requirements)
            except RecoverableModelError as exc:
                return self._wait(run, Stage.GENERATE_TESTS, exc)
            test_payload = {
                "test_cases": [item.model_dump(mode="json") for item in test_cases]
            }
            self.repository.save_output(run.run_id, Stage.GENERATE_TESTS, test_payload)
            run.current_stage = Stage.GENERATE_TESTS
            self._save(run, "测试用例生成完成")
        test_cases = [TestCase.model_validate(item) for item in test_payload["test_cases"]]

        trace_payload = self.repository.get_output(run.run_id, Stage.VALIDATE_TRACEABILITY)
        if trace_payload is None:
            trace_report = self.services.traceability_validator(
                {item.review_id for item in clean_reviews_list},
                findings,
                requirements,
                test_cases,
            )
            trace_payload = trace_report.model_dump(mode="json")
            self.repository.save_output(run.run_id, Stage.VALIDATE_TRACEABILITY, trace_payload)
        trace_report = ValidationReport.model_validate(trace_payload)
        run.current_stage = Stage.VALIDATE_TRACEABILITY
        if not trace_report.valid:
            run.status = RunStatus.PARTIAL
            return self._save(run, "追溯链校验未通过，结果不可发布")

        run.current_stage = Stage.COMPLETE
        run.status = RunStatus.COMPLETED
        run.coverage_ratio = 1
        run.last_error = None
        return self._save(run, "分析完成")
```

- [x] **步骤 4：运行编排器测试并提交**

运行：`.\.venv\Scripts\python -m pytest tests/test_orchestrator.py -v`

预期：检查点、同一运行续跑、成功完成和采集失败测试均通过；续跑保持原始 `run_id`，且不会再次调用第 1 批。

```powershell
git add src/app_review_insights/pipeline/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: orchestrate resumable analysis runs"
```

## 任务 13：构建 Streamlit 单页工作台

**文件：**
- 创建： `src/app_review_insights/ui/__init__.py`
- 创建： `src/app_review_insights/ui/components.py`
- 创建： `src/app_review_insights/ui/main.py`
- 测试： `tests/test_app_smoke.py`

- [ ] **步骤 1：编写 UI 可导入构建的冒烟测试**

```python
# tests/test_app_smoke.py
from app_review_insights.ui.main import build_services


def test_build_services_uses_configured_database(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))
    services = build_services(use_fake_provider=True)
    assert services.repository.path == tmp_path / "runs.sqlite3"
    assert services.batch_analyzer is None


def test_build_services_without_key_keeps_demo_mode_available(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))
    services = build_services()
    assert services.batch_analyzer is None
    assert services.repository.path.exists()
```

- [ ] **步骤 2：运行测试并确认失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_app_smoke.py -v`

预期：失败，因为 UI 模块尚不存在.

- [ ] **步骤 3：实现可复用展示组件**

```python
# src/app_review_insights/ui/components.py
import pandas as pd
import streamlit as st


STATUS_LABELS = {
    "running": "运行中",
    "waiting_for_model": "等待模型恢复",
    "partial": "部分完成",
    "completed": "已完成",
    "failed": "失败",
}


def render_run_status(run, events):
    st.subheader("执行进度")
    st.progress(run.coverage_ratio, text=f"分析覆盖率 {run.coverage_ratio:.0%}")
    st.write(f"状态：{STATUS_LABELS.get(run.status.value, run.status.value)}")
    st.write(f"阶段：`{run.current_stage.value}`")
    if run.last_error:
        st.warning(run.last_error)
    st.caption("最近事件")
    for event in events[-6:][::-1]:
        st.write(f"- {event.message}")


def render_reviews(reviews):
    st.dataframe(pd.DataFrame([item.model_dump(mode="json") for item in reviews]), use_container_width=True)


def render_badge(label: str, kind: str):
    colors = {
        "deterministic": "blue",
        "ai": "orange",
        "validated": "green",
        "assumption": "gray",
    }
    st.markdown(f":{colors[kind]}-badge[{label}]")


def render_records(records: list[dict], empty_message: str):
    if not records:
        st.info(empty_message)
        return
    st.dataframe(pd.DataFrame(records), use_container_width=True, hide_index=True)


def render_result_tabs(repository, run_id: str):
    from app_review_insights.models import Stage

    clean = repository.get_output(run_id, Stage.CLEAN) or {"reviews": [], "stats": {}}
    findings = repository.get_output(run_id, Stage.VALIDATE_FINDINGS) or {
        "findings": [], "validation": {}
    }
    plan = repository.get_output(run_id, Stage.PLAN) or {"requirements": []}
    tests = repository.get_output(run_id, Stage.GENERATE_TESTS) or {"test_cases": []}
    trace = repository.get_output(run_id, Stage.VALIDATE_TRACEABILITY) or {
        "valid": False, "issues": []
    }

    overview, reviews_tab, findings_tab, plan_tab, tests_tab, trace_tab = st.tabs(
        ["总览", "评论数据", "主题与发现", "版本与 PRD", "测试用例", "证据链"]
    )
    with overview:
        st.caption("Deterministic · 程序计算")
        st.json(clean.get("stats", {}), expanded=True)
        st.caption("AI-generated · 模型归纳；Validated · 已通过证据校验")
        st.metric("有效发现", len(findings["findings"]))
        st.metric("PRD 需求", len(plan["requirements"]))
        st.metric("测试用例", len(tests["test_cases"]))
    with reviews_tab:
        st.caption("原始评论保留原文，中文摘要不能替代证据。")
        render_records(clean["reviews"], "尚无清洗评论。")
    with findings_tab:
        for finding in findings["findings"]:
            label = "Assumption" if finding["evidence_status"] == "assumption" else "Validated"
            with st.expander(f"{finding['finding_id']} · {finding['title']} · {label}"):
                st.write(finding["problem_statement"])
                st.write({
                    "support_count": finding["support_count"],
                    "conflict_count": finding["conflict_count"],
                    "confidence": finding["confidence"],
                    "supporting_review_ids": finding["supporting_review_ids"],
                    "conflicting_review_ids": finding["conflicting_review_ids"],
                    "limitations": finding["limitations"],
                })
    with plan_tab:
        if plan.get("quantity_notice"):
            st.info(plan["quantity_notice"])
        for requirement in plan["requirements"]:
            with st.expander(
                f"{requirement['requirement_id']} · {requirement['target_version']} · "
                f"{requirement['title']}"
            ):
                st.json(requirement, expanded=True)
    with tests_tab:
        render_records(tests["test_cases"], "尚无测试用例。")
    with trace_tab:
        if trace["valid"]:
            st.success("Review → Finding → Requirement → TestCase 追溯链完整。")
        else:
            st.error("追溯链未通过，结果不可作为正式交付物。")
        render_records(trace["issues"], "没有追溯问题。")
```

- [ ] **步骤 4：实现服务装配与单页布局**

```python
# src/app_review_insights/ui/main.py
import streamlit as st

from app_review_insights.collectors import AppStoreCollector
from app_review_insights.config import load_settings
from app_review_insights.input_parsing import import_reviews
from app_review_insights.llm import DeepSeekProvider
from app_review_insights.models import AnalysisRequest, SourceType
from app_review_insights.pipeline.analyze import analyze_batch, consolidate_findings
from app_review_insights.pipeline.orchestrator import AnalysisOrchestrator, PipelineServices
from app_review_insights.pipeline.planning import build_requirements
from app_review_insights.pipeline.test_generation import generate_test_cases
from app_review_insights.pipeline.traceability import validate_traceability
from app_review_insights.pipeline.validate import validate_finding_drafts
from app_review_insights.storage import RunRepository
from app_review_insights.ui.components import render_result_tabs, render_run_status


def build_services(use_fake_provider: bool = False) -> PipelineServices:
    settings = load_settings()
    repository = RunRepository(settings.database_path)
    if use_fake_provider or not settings.deepseek_api_key:
        return PipelineServices(
            repository=repository,
            batch_analyzer=None,
            collector=AppStoreCollector(),
            batch_size=settings.batch_review_limit,
            batch_max_characters=settings.batch_max_characters,
        )
    provider = DeepSeekProvider.from_settings(settings)
    return PipelineServices(
        repository=repository,
        collector=AppStoreCollector(),
        batch_analyzer=(lambda reviews, goal: analyze_batch(provider, reviews, goal)),
        consolidator=(lambda results, goal: consolidate_findings(provider, results, goal)),
        finding_validator=validate_finding_drafts,
        requirement_builder=(
            lambda findings, goal, total: build_requirements(provider, findings, goal, total)
        ),
        test_case_builder=(lambda requirements: generate_test_cases(provider, requirements)),
        traceability_validator=validate_traceability,
        batch_size=settings.batch_review_limit,
        batch_max_characters=settings.batch_max_characters,
    )


def main():
    st.set_page_config(page_title="App Review Insights", page_icon="🔎", layout="wide")
    st.title("App Review Insights")
    st.caption("从真实评论到可执行 PRD 与测试用例")

    services = build_services()
    model_ready = services.batch_analyzer is not None
    if model_ready:
        st.success("DeepSeek 已配置，可执行实时语义分析。")
    else:
        st.warning("未配置 DeepSeek API Key；仍可查看历史缓存演示，但不能启动实时 AI 分析。")
    left, right = st.columns([3, 1])
    with left:
        app_url = st.text_input("美国区 App Store 链接")
        goal = st.text_area("分析目标", value="识别影响用户体验和产品增长的核心问题")
        source_label = st.radio("数据来源", ["在线采集", "导入 JSON/CSV"], horizontal=True)
        review_limit = st.slider("评论数量", 100, 1000, 500, 100)
        upload = st.file_uploader("评论文件", type=["json", "csv"], disabled=source_label == "在线采集")
        start = st.button(
            "开始分析",
            type="primary",
            use_container_width=True,
            disabled=not model_ready,
        )

        if start:
            if source_label == "导入 JSON/CSV" and upload is None:
                st.error("请选择 JSON 或 CSV 评论文件。")
                st.stop()
            source_type = SourceType.ONLINE if source_label == "在线采集" else (
                SourceType.JSON if upload and upload.name.endswith(".json") else SourceType.CSV
            )
            request = AnalysisRequest(
                source_type=source_type,
                app_url=app_url or None,
                analysis_goal=goal,
                review_limit=review_limit,
            )
            imported = import_reviews(upload.getvalue(), upload.name, "imported") if upload else None
            live_status = st.status("正在执行分析工作流", expanded=True)
            orchestrator = AnalysisOrchestrator(
                services,
                on_event=lambda event: live_status.write(event.message),
            )
            run = orchestrator.start(request, imported_reviews=imported)
            if run.status.value == "completed":
                live_status.update(label="分析完成", state="complete", expanded=False)
            elif run.status.value == "waiting_for_model":
                live_status.update(label="已保存进度，等待模型恢复", state="error")
            st.session_state["run_id"] = run.run_id

        run_id = st.session_state.get("run_id")
        if run_id:
            run = services.repository.get_run(run_id)
            render_result_tabs(services.repository, run_id)

    with right:
        if st.session_state.get("run_id"):
            run = services.repository.get_run(st.session_state["run_id"])
            render_run_status(run, services.repository.list_events(run.run_id))
            if run.status.value == "waiting_for_model" and st.button("继续分析"):
                AnalysisOrchestrator(services).resume(run.run_id)
                st.rerun()
```

```python
# src/app_review_insights/ui/__init__.py
"""Streamlit user interface."""
```

- [ ] **步骤 5：运行冒烟测试并手工启动应用**

运行：

```powershell
.\.venv\Scripts\python -m pytest tests/test_app_smoke.py -v
.\.venv\Scripts\streamlit run app.py
```

预期：测试通过；Streamlit 启动时没有导入异常；桌面与窄屏宽度下均能看到输入面板、结果标签页和右侧状态面板.

- [ ] **步骤 6：提交 UI**

```powershell
git add src/app_review_insights/ui app.py tests/test_app_smoke.py
git commit -m "feat: add streamlit analysis workbench"
```

## 任务 14：添加离线缓存、样例数据和结果下载

**文件：**
- 创建： `src/app_review_insights/storage/cache.py`
- 创建： `data/samples/reviews-sample.json`
- 创建： `data/cache/demo-run.json`
- 修改： `src/app_review_insights/ui/main.py`
- 测试： `tests/test_export.py`

- [ ] **步骤 1：添加失败的缓存标记测试**

```python
# append to tests/test_export.py
from app_review_insights.storage.cache import load_demo_run


def test_demo_cache_is_explicitly_labeled():
    demo = load_demo_run("data/cache/demo-run.json")
    assert demo["mode"] == "historical_cache_demo"
    assert demo["is_live"] is False
    assert demo["collected_at"]
```

- [ ] **步骤 2：运行测试并确认失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_export.py::test_demo_cache_is_explicitly_labeled -v`

预期：失败，因为 缓存加载模块和样例文件尚不存在.

- [ ] **步骤 3：实现严格的演示缓存加载**

```python
# src/app_review_insights/storage/cache.py
import json
from pathlib import Path

from app_review_insights.export import build_traceability_rows, rows_to_csv_bytes, to_json_bytes
from app_review_insights.models import Stage


def load_demo_run(path: str | Path) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("mode") != "historical_cache_demo" or payload.get("is_live") is not False:
        raise ValueError("demo cache must be explicitly labeled as non-live")
    return payload


def build_downloads(repository, run_id: str) -> dict[str, bytes]:
    clean = repository.get_output(run_id, Stage.CLEAN) or {"reviews": []}
    plan = repository.get_output(run_id, Stage.PLAN) or {"requirements": []}
    test_payload = repository.get_output(run_id, Stage.GENERATE_TESTS) or {"test_cases": []}
    finding_payload = repository.get_output(run_id, Stage.VALIDATE_FINDINGS) or {"findings": []}
    findings = {
        item["finding_id"]: item["supporting_review_ids"]
        for item in finding_payload["findings"]
    }
    requirements = {
        item["requirement_id"]: item["finding_ids"] for item in plan["requirements"]
    }
    test_cases = {
        item["test_case_id"]: item["requirement_id"] for item in test_payload["test_cases"]
    }
    return {
        "cleaned_reviews": to_json_bytes(clean["reviews"]),
        "prd": to_json_bytes(plan),
        "test_cases": rows_to_csv_bytes(test_payload["test_cases"]),
        "traceability": rows_to_csv_bytes(
            build_traceability_rows(findings, requirements, test_cases)
        ),
    }
```

- [ ] **步骤 4：创建小型人工导入样例，并通过真实流水线生成完整演示缓存**

```json
{
  "reviews": [
    {"review_id":"sample-001","content":"The free trial renewal date was not clear before I subscribed.","rating":2,"published_at":"2026-07-01T10:00:00Z","app_version":"8.4.0","language":"en"},
    {"review_id":"sample-002","content":"I did not see the monthly price until the final confirmation.","rating":1,"published_at":"2026-07-02T10:00:00Z","app_version":"8.4.0","language":"en"},
    {"review_id":"sample-003","content":"The subscription terms were clear and easy to understand.","rating":5,"published_at":"2026-07-03T10:00:00Z","app_version":"8.4.0","language":"en"},
    {"review_id":"sample-004","content":"暂停训练以后计时器不会继续。","rating":2,"published_at":"2026-07-04T10:00:00Z","app_version":"8.4.1","language":"zh-cn"},
    {"review_id":"sample-005","content":"暂停训练后计时器有时无法继续。","rating":2,"published_at":"2026-07-05T10:00:00Z","app_version":"8.4.1","language":"zh-cn"},
    {"review_id":"sample-006","content":"Voice instructions are too quiet while music is playing.","rating":3,"published_at":"2026-07-06T10:00:00Z","app_version":"8.4.1","language":"en"},
    {"review_id":"sample-007","content":"I love the voice guidance and workout pacing.","rating":5,"published_at":"2026-07-07T10:00:00Z","app_version":"8.4.1","language":"en"},
    {"review_id":"sample-008","content":"The exercise preview is too short for unfamiliar movements.","rating":3,"published_at":"2026-07-08T10:00:00Z","app_version":"8.4.1","language":"en"},
    {"review_id":"sample-009","content":"希望动作开始前能有更长的示范时间。","rating":3,"published_at":"2026-07-09T10:00:00Z","app_version":"8.4.1","language":"zh-cn"},
    {"review_id":"sample-010","content":"The app crashed after I changed my weekly plan.","rating":1,"published_at":"2026-07-10T10:00:00Z","app_version":"8.4.2","language":"en"},
    {"review_id":"sample-011","content":"Changing my weekly plan worked perfectly.","rating":5,"published_at":"2026-07-11T10:00:00Z","app_version":"8.4.2","language":"en"},
    {"review_id":"sample-012","content":"Please let me export a weekly workout summary.","rating":4,"published_at":"2026-07-12T10:00:00Z","app_version":null,"language":"en"},
    {"review_id":"sample-013","content":"希望可以导出每周训练记录。","rating":4,"published_at":"2026-07-13T10:00:00Z","app_version":null,"language":"zh-cn"},
    {"review_id":"sample-014","content":"The free trial renewal date was not clear before I subscribed.","rating":2,"published_at":"2026-07-14T10:00:00Z","app_version":"8.4.0","language":"en"},
    {"review_id":"sample-015","content":"Workouts are effective but the rest timer feels too short.","rating":3,"published_at":"2026-07-15T10:00:00Z","app_version":"8.4.2","language":"en"},
    {"review_id":"sample-016","content":"休息时间太短，希望可以自己调整。","rating":3,"published_at":"2026-07-16T10:00:00Z","app_version":"8.4.2","language":"zh-cn"},
    {"review_id":"sample-017","content":"The adjustable difficulty is excellent.","rating":5,"published_at":"2026-07-17T10:00:00Z","app_version":"8.4.2","language":"en"},
    {"review_id":"sample-018","content":"Beginner workouts become difficult too quickly.","rating":2,"published_at":"2026-07-18T10:00:00Z","app_version":"8.4.2","language":"en"},
    {"review_id":"sample-019","content":"Notifications arrive during my configured quiet hours.","rating":2,"published_at":"2026-07-19T10:00:00Z","app_version":"8.4.2","language":"en"},
    {"review_id":"sample-020","content":"Great workouts and a clean interface.","rating":5,"published_at":"2026-07-20T10:00:00Z","app_version":"8.4.2","language":"en"}
  ]
}
```

向 `src/app_review_insights/storage/cache.py` 添加 `export_demo_run`：

```python
from datetime import UTC, datetime


def export_demo_run(repository, run_id: str, destination: str | Path, source_app_url: str) -> None:
    run = repository.get_run(run_id)
    result = {
        stage.value: repository.get_output(run_id, stage)
        for stage in (
            Stage.CLEAN,
            Stage.VALIDATE_FINDINGS,
            Stage.PLAN,
            Stage.GENERATE_TESTS,
            Stage.VALIDATE_TRACEABILITY,
        )
    }
    payload = {
        "mode": "historical_cache_demo",
        "is_live": False,
        "collected_at": datetime.now(UTC).isoformat(),
        "source_app_url": source_app_url,
        "model_provider": "deepseek",
        "model_name": "deepseek-chat",
        "result": result,
    }
    Path(destination).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
```

在缓存模块中导入 `from app_review_insights.models import Stage`。真实流水线完成后，使用以下命令导出运行：

```powershell
.\.venv\Scripts\python -c "from app_review_insights.config import load_settings; from app_review_insights.storage import RunRepository; from app_review_insights.storage.cache import export_demo_run; s=load_settings(); r=RunRepository(s.database_path); run_id=r.list_runs()[0].run_id; export_demo_run(r, run_id, 'data/cache/demo-run.json', 'https://apps.apple.com/us/app/workout-for-women-home-gym/id839285684')"
```

不要手工向演示缓存填写结论。必须通过同一条流水线生成，并保留原始评论 ID。

- [ ] **步骤 5：为 Streamlit 添加明确的演示模式和下载功能**

```python
from app_review_insights.storage.cache import build_downloads, load_demo_run

demo_mode = st.toggle("查看历史缓存演示")
if demo_mode:
    demo = load_demo_run("data/cache/demo-run.json")
    st.warning("当前展示历史缓存结果，不是本次实时分析。")
    st.json(demo["result"], expanded=False)

if st.session_state.get("run_id"):
    downloads = build_downloads(services.repository, st.session_state["run_id"])
    st.download_button(
        "下载清洗评论 JSON", downloads["cleaned_reviews"], "cleaned-reviews.json"
    )
    st.download_button("下载 PRD JSON", downloads["prd"], "prd.json")
    st.download_button(
        "下载测试用例 CSV", downloads["test_cases"], "test-cases.csv"
    )
    st.download_button(
        "下载证据链 CSV", downloads["traceability"], "traceability.csv"
    )
```

- [ ] **步骤 6：运行测试并提交**

运行：`.\.venv\Scripts\python -m pytest tests/test_export.py -v`

预期：所有导出与缓存测试均通过.

```powershell
git add src/app_review_insights/storage/cache.py src/app_review_insights/ui/main.py data/samples data/cache tests/test_export.py
git commit -m "feat: add offline demo and result exports"
```

## 任务 15：添加小型 Prompt 评测工具

**文件：**
- 创建： `evals/gold-reviews.json`
- 创建： `scripts/run_eval.py`
- 创建： `docs/model-and-prompts.md`
- 测试： `tests/test_analysis.py`

- [ ] **步骤 1：添加确定性评分测试**

```python
# append to tests/test_analysis.py
from scripts.run_eval import score_predictions


def test_eval_scores_reference_precision_and_topic_recall():
    score = score_predictions(
        expected_topics={"subscription", "timer"},
        expected_review_ids={"r-1", "r-2"},
        predicted_topics={"subscription"},
        predicted_review_ids={"r-1", "invented"},
    )
    assert score["topic_recall"] == 0.5
    assert score["reference_precision"] == 0.5
```

- [ ] **步骤 2：运行测试并确认失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_analysis.py::test_eval_scores_reference_precision_and_topic_recall -v`

预期：失败，因为 评测脚本尚不存在.

- [ ] **步骤 3：实现确定性评测评分与 CLI 输出**

```python
# scripts/run_eval.py
import json
from pathlib import Path


def score_predictions(
    expected_topics: set[str],
    expected_review_ids: set[str],
    predicted_topics: set[str],
    predicted_review_ids: set[str],
) -> dict[str, float]:
    topic_recall = len(expected_topics & predicted_topics) / max(1, len(expected_topics))
    reference_precision = len(expected_review_ids & predicted_review_ids) / max(
        1, len(predicted_review_ids)
    )
    return {
        "topic_recall": round(topic_recall, 3),
        "reference_precision": round(reference_precision, 3),
    }


def main():
    gold = json.loads(Path("evals/gold-reviews.json").read_text(encoding="utf-8"))
    print(json.dumps({"cases": len(gold["cases"])}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **步骤 4：创建人工标注黄金数据集**

```json
{
  "cases": [
    {
      "case_id": "eval-subscription-01",
      "analysis_goal": "重点分析订阅转化",
      "expected_topics": ["subscription transparency"],
      "expected_review_ids": ["eval-r-001", "eval-r-002"],
      "reviews": [
        {"review_id":"eval-r-001","content":"The free trial renewal date was hidden until the last screen.","rating":2,"published_at":"2026-06-01T10:00:00Z"},
        {"review_id":"eval-r-002","content":"I wanted to know the monthly price before starting the trial.","rating":1,"published_at":"2026-06-02T10:00:00Z"},
        {"review_id":"eval-r-003","content":"The subscription price and renewal terms were clear to me.","rating":5,"published_at":"2026-06-03T10:00:00Z"},
        {"review_id":"eval-r-004","content":"Great workout selection.","rating":5,"published_at":"2026-06-04T10:00:00Z"}
      ]
    },
    {
      "case_id": "eval-timer-01",
      "analysis_goal": "重点分析训练可用性",
      "expected_topics": ["workout timer reliability", "audio guidance"],
      "expected_review_ids": ["eval-r-005", "eval-r-006", "eval-r-007"],
      "reviews": [
        {"review_id":"eval-r-005","content":"The timer freezes when I resume after a pause.","rating":1,"published_at":"2026-06-05T10:00:00Z"},
        {"review_id":"eval-r-006","content":"暂停以后计时器不会继续。","rating":2,"published_at":"2026-06-06T10:00:00Z"},
        {"review_id":"eval-r-007","content":"Voice instructions are too quiet while music is playing.","rating":3,"published_at":"2026-06-07T10:00:00Z"},
        {"review_id":"eval-r-008","content":"The timer freezes when I resume after a pause.","rating":1,"published_at":"2026-06-08T10:00:00Z"}
      ]
    },
    {
      "case_id": "eval-insufficient-01",
      "analysis_goal": "识别下一版本高优先级问题",
      "expected_topics": [],
      "expected_review_ids": [],
      "expected_assumption_review_ids": ["eval-r-009"],
      "reviews": [
        {"review_id":"eval-r-009","content":"The app crashed once after I changed my plan.","rating":3,"published_at":"2026-06-09T10:00:00Z"},
        {"review_id":"eval-r-010","content":"I love the new workout plan.","rating":5,"published_at":"2026-06-10T10:00:00Z"},
        {"review_id":"eval-r-011","content":"Please add more stretching routines.","rating":4,"published_at":"2026-06-11T10:00:00Z"},
        {"review_id":"eval-r-012","content":"The interface is easy to use.","rating":5,"published_at":"2026-06-12T10:00:00Z"}
      ]
    }
  ]
}
```

使用下表创建 `docs/model-and-prompts.md`，每次有意义的实验追加一行：

```markdown
# Model and Prompt Notes

Default provider: DeepSeek. Default model: `deepseek-chat`. Temperature: `0.1`.

The model performs dynamic topic discovery, consolidation, requirement drafting, and test drafting. Python performs collection, cleaning, counting, reference validation, confidence calculation, and traceability validation.

| Date | Prompt/schema version | Dataset | Topic recall | Reference precision | Structured output success | Failure example | Change and reason |
|---|---|---|---:|---:|---:|---|---|
| 2026-08-15 | batch-v1 / FindingDraft-v1 | gold-reviews | record after first run | record after first run | record after first run | record one observed error | describe the next concrete prompt change |
```

- [ ] **步骤 5：运行测试并提交**

运行：

```powershell
.\.venv\Scripts\python -m pytest tests/test_analysis.py -v
.\.venv\Scripts\python scripts/run_eval.py
```

预期：测试通过，脚本打印标注用例数量.

```powershell
git add evals scripts/run_eval.py docs/model-and-prompts.md tests/test_analysis.py
git commit -m "test: add prompt evaluation harness"
```

## 任务 16：完成集成测试和泛化检查

**文件：**
- 创建： `tests/conftest.py`
- 修改： `tests/test_orchestrator.py`
- 修改： `tests/test_app_smoke.py`
- 创建： `tests/fixtures/mixed-reviews.json`

- [ ] **步骤 1：添加可复用假模型供应商与混合数据夹具**

```python
# tests/conftest.py
import pytest


class SchemaFakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)

    def generate(self, system_prompt, user_prompt, schema):
        value = self.responses.pop(0)
        return schema.model_validate(value)


@pytest.fixture
def fake_provider_factory():
    return SchemaFakeProvider
```

```json
{
  "reviews": [
    {"review_id":"mix-001","content":"The renewal date was not shown clearly before I started the free trial.","rating":2,"published_at":"2026-07-01T10:00:00Z","app_version":"8.4.0","language":"en"},
    {"review_id":"mix-002","content":"I could not find the subscription price until the final confirmation.","rating":1,"published_at":"2026-07-02T10:00:00Z","app_version":"8.4.0","language":"en"},
    {"review_id":"mix-003","content":"The subscription terms were clear and easy to understand.","rating":5,"published_at":"2026-07-03T10:00:00Z","app_version":"8.4.0","language":"en"},
    {"review_id":"mix-004","content":"暂停训练后计时器有时不会继续。","rating":2,"published_at":"2026-07-04T10:00:00Z","app_version":"8.4.1","language":"zh-cn"},
    {"review_id":"mix-005","content":"暂停训练以后，计时器偶尔无法继续。","rating":2,"published_at":"2026-07-05T10:00:00Z","app_version":"8.4.1","language":"zh-cn"},
    {"review_id":"mix-006","content":"The renewal date was not shown clearly before I started the free trial.","rating":2,"published_at":"2026-07-06T10:00:00Z","app_version":"8.4.0","language":"en"},
    {"review_id":"mix-007","content":"Voice instructions are too quiet when music is playing.","rating":3,"published_at":"2026-07-07T10:00:00Z","app_version":null,"language":"en"},
    {"review_id":"mix-008","content":"希望可以导出每周训练记录。","rating":4,"published_at":"2026-07-08T10:00:00Z","app_version":null,"language":"zh-cn"},
    {"review_id":"mix-009","content":"Great workouts and clear instructions.","rating":5,"published_at":"2026-07-09T10:00:00Z","app_version":"8.4.1","language":"en"},
    {"review_id":"mix-010","content":"The app crashed once after I changed the workout plan.","rating":3,"published_at":"2026-07-10T10:00:00Z","app_version":"8.4.1","language":"en"}
  ]
}
```

该夹具有意包含中英文评论、精确重复、近似重复、相互冲突的订阅反馈、缺失版本，以及一条应保留为假设的孤立崩溃评论。

- [ ] **步骤 2：添加完整成功的导入数据流水线测试**

```python
# append to tests/test_orchestrator.py
from pathlib import Path

from app_review_insights.input_parsing import import_reviews
from app_review_insights.llm.schemas import FindingDraft
from app_review_insights.models import EvidenceStatus, Requirement, TestCase
from app_review_insights.pipeline.traceability import validate_traceability
from app_review_insights.pipeline.validate import validate_finding_drafts


def test_full_imported_pipeline_completes_with_traceability(tmp_path):
    fixture = Path("tests/fixtures/mixed-reviews.json").read_bytes()
    reviews = import_reviews(fixture, "mixed-reviews.json", app_id="mixed-app")
    repo = RunRepository(tmp_path / "runs.sqlite3")

    finding_draft = FindingDraft(
        title="Subscription terms are unclear",
        problem_statement="Some users cannot see price and renewal timing before subscribing.",
        topic_label="subscription transparency",
        supporting_review_ids=["mix-001", "mix-002"],
        conflicting_review_ids=["mix-003"],
        reasoning_summary="Two complaints and one explicit opposing review are present.",
    )

    def requirement_builder(findings, goal, total_reviews):
        finding = findings[0]
        assert finding.evidence_status == EvidenceStatus.VALIDATED
        return [
            Requirement(
                requirement_id="REQ-001",
                finding_ids=[finding.finding_id],
                title="Show price and renewal terms before confirmation",
                user_problem=finding.problem_statement,
                objective="Make subscription terms understandable before purchase.",
                scope=["Show price, trial duration, and renewal date"],
                non_goals=["Redesign account settings"],
                functional_rules=["Display terms before the confirmation action"],
                edge_cases=["Store price lookup is temporarily unavailable"],
                acceptance_criteria=["Terms are visible without opening another page"],
                success_metrics=["Reduce related low-rating complaints"],
                impact=5,
                complexity="low",
                priority_score=8.0,
                target_version="V1.0",
                source_review_ids=finding.supporting_review_ids,
            )
        ]

    def test_case_builder(requirements):
        requirement = requirements[0]
        return [
            TestCase(
                test_case_id="TC-001",
                requirement_id=requirement.requirement_id,
                title="Subscription terms are visible before confirmation",
                preconditions=["A free trial is available"],
                steps=["Open the subscription offer"],
                expected_result="Price, trial duration, and renewal date are visible.",
                case_type="normal",
                source_review_ids=requirement.source_review_ids,
            )
        ]

    services = PipelineServices(
        repository=repo,
        batch_analyzer=lambda batch, goal: BatchAnalysisResult(
            findings=[finding_draft], batch_limitations=[]
        ),
        consolidator=lambda results, goal: ConsolidationResult(findings=[finding_draft]),
        finding_validator=validate_finding_drafts,
        requirement_builder=requirement_builder,
        test_case_builder=test_case_builder,
        traceability_validator=validate_traceability,
        batch_size=100,
    )

    run = AnalysisOrchestrator(services).start(
        AnalysisRequest(source_type=SourceType.JSON, analysis_goal="重点分析订阅转化"),
        imported_reviews=reviews,
    )

    assert run.status == RunStatus.COMPLETED
    test_payload = repo.get_output(run.run_id, Stage.GENERATE_TESTS)
    source_ids = {review.review_id for review in reviews}
    assert set(test_payload["test_cases"][0]["source_review_ids"]).issubset(source_ids)
    assert repo.get_output(run.run_id, Stage.VALIDATE_TRACEABILITY)["valid"] is True
```

- [ ] **步骤 3：保留任务 12 的精确故障恢复断言**

运行指定的回归测试：

```powershell
.\.venv\Scripts\python -m pytest tests/test_orchestrator.py::test_orchestrator_resumes_same_run_without_repeating_completed_batch -v
```

预期：通过；原始 `run_id` 最终完成，分析器调用次数等于 `3`。

- [ ] **步骤 4：运行完整自动化测试套件与覆盖率**

运行：

```powershell
.\.venv\Scripts\python -m pytest --cov=app_review_insights --cov-report=term-missing
.\.venv\Scripts\ruff check .
```

预期：

- 所有测试通过。
- 核心模块 `cleaning`、`validate`、`traceability` 和 `repository` 的行覆盖率均至少为 90%。
- 没有 Ruff 错误。

- [ ] **步骤 5：执行两次真实输入泛化检查**

使用以下输入运行 Streamlit 应用：

1. 指定的 Workout for Women App，加上订阅转化分析目标。
2. 另一个美国区 App Store 应用，或混合评论夹具，并使用不同的分析目标。

每次运行都要验证：

- 除非输入评论提供证据，否则不得出现应用专属分类。
- 每个主要 Finding 都具有来源 ID、数量、置信度、冲突证据和局限字段。
- 需求数量保持在 5–10 个；若证据不足，UI 必须明确解释。
- 每条测试用例都能追溯到需求和来源评论。
- 模型生成结果和确定性结果在视觉上可区分。

- [ ] **步骤 6：提交集成测试覆盖**

```powershell
git add tests
git commit -m "test: verify full analysis workflow"
```

## 任务 17：编写交付文档并验证全新安装

**文件：**
- 创建： `README.md`
- 创建： `README.zh-CN.md`
- 修改： `docs/data-format.md`
- 修改： `docs/model-and-prompts.md`
- 创建： `docs/architecture.md`

- [ ] **步骤 1：编写包含精确启动说明的英文 README**

将 `xshdxz` 替换为用户的 GitHub 账号名，并写入替换后的命令：

```powershell
git clone https://github.com/xshdxz/app-review-insights-agents.git
cd app-review-insights
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\streamlit run app.py
```

还必须说明：

- 项目目标与证据链。
- 美国区商店的数据来源与限制。
- 在线、导入与缓存模式。
- DeepSeek 配置与密钥处理。
- 规则与模型的职责选择。
- 检查点恢复行为。
- 测试与评测命令。
- 项目为何采用轻量编排器，而不是多个智能体。

- [ ] **步骤 2：编写中文 README**

`README.zh-CN.md` 必须包含相同的运行事实，并显著链接回 `README.md`。中英文版本不得宣称不同的功能范围。

- [ ] **步骤 3：添加架构文档**

```markdown
# 架构

```text
Streamlit 用户界面
  → 编排器 / RunRepository
  → 采集器或导入器
  → 清洗 / 分批
  → DeepSeek 结构化分析
  → 确定性证据校验
  → PRD / 测试生成
  → 追溯校验 / 导出
```

每个阶段都会在下一阶段开始前持久化输出。UI 重新运行时只读取已持久化的输出，绝不隐式重复调用模型。
```

- [ ] **步骤 4：验证密钥与仓库卫生**

运行：

```powershell
git status --short --ignored
git grep -n -I -E "(sk-[A-Za-z0-9_-]{12,}|DEEPSEEK_API_KEY=.+)" -- . ':!.env.example'
```

预期：

- `.env`、`data/runs/`、PDF、JD、`.planning/`、`.superpowers/` 和 `tmp/` 均被忽略。
- 密钥扫描不输出任何匹配。

- [ ] **步骤 5：在全新克隆目录中验证**

将本地仓库克隆到一个新的同级目录，然后只执行 README 中的命令。确认：

- 应用能够启动。
- 没有模型密钥时，演示模式仍可使用。
- 导入模式能够接受 `data/samples/reviews-sample.json`。
- 在线/模型模式会清楚说明缺失配置，而不是崩溃。

- [ ] **步骤 6：运行最终检查并提交文档**

运行：

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\ruff check .
git diff --check
```

预期：所有命令均以退出码 0 结束.

```powershell
git add README.md README.zh-CN.md docs
git commit -m "docs: add setup architecture and model notes"
```

## 任务 18：候选版本收尾

**文件：**
- 只修改已验证缺陷所必需的文件。
- 进入收尾阶段后不要增加新功能。

- [ ] **步骤 1：执行最终验收矩阵**

验证并记录以下结果：

```text
[ ] 指定应用 + 订阅目标能够完成分析
[ ] 未见过的数据集 + 不同目标能够完成分析
[ ] JSON 导入能够完成
[ ] CSV 导入能够完成
[ ] 混合语言评论仍可追溯
[ ] 重复评论已删除并计数
[ ] 冲突评论清晰可见
[ ] 证据不足时标记为假设（Assumption）
[ ] DeepSeek 失败时保留进度
[ ] 续跑沿用原始 run_id 并跳过已完成批次
[ ] 缓存被明确标记为历史数据/非实时数据
[ ] 下载文件包含 ID 和 UTF-8 文本
[ ] 全新克隆后可按 README 完成启动
[ ] 仓库未跟踪密钥或私有文件
```

- [ ] **步骤 2：截取最终演示证据**

截取：

- 输入与运行进度页面。
- 一个同时包含支持与冲突评论的已验证发现（Finding）。
- 一个带验收标准的 PRD 需求。
- 一条测试用例及其追溯路径。
- 一次部分运行中的模型失败和续跑状态。

公开截图存放在 `docs/images/`。如果评论作者用户名可能识别个人，必须进行脱敏。

- [ ] **步骤 3：运行最终验证命令**

运行：

```powershell
.\.venv\Scripts\python -m pytest --cov=app_review_insights
.\.venv\Scripts\ruff check .
git diff --check
git status --short
```

预期：测试与代码检查通过；不存在非预期的未跟踪文件。

- [ ] **步骤 4：仅提交已验证修复与截图**

```powershell
git add src tests docs README.md README.zh-CN.md data/samples data/cache
git commit -m "chore: prepare release candidate"
```

- [ ] **步骤 5：停止新增功能**

进入收尾阶段后，只接受解决无法启动、证据损坏、必需输入模式失效或密钥泄露的问题；所有外观和可选增强均延期。
