# Real-Data Regression Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复真实 CSV、当前 Apple 评论接口、证据语义、中文摘要、模型状态和评测入口问题，并通过真实端到端验收。

**Architecture:** 保留现有 Streamlit 单页工作台与检查点流水线。输入、采集、摘要、归并、证据语义复核和展示各自使用明确接口；模型输出始终先通过 Pydantic Schema，再由 Python 校验 ID、角色、证据阈值和追溯链。新增一次全局证据审计调用，不为每条评论增加独立调用。

**Tech Stack:** Python 3.11+、Streamlit 1.61、Pydantic 2、DeepSeek OpenAI-compatible API、httpx、ElementTree、pandas、pytest、Ruff、PowerShell。

---

## 文件职责

- `src/app_review_insights/config.py`：模型启用状态与现有运行配置。
- `src/app_review_insights/input_parsing.py`：JSON/CSV 别名、编码和字段规范化。
- `src/app_review_insights/collectors/app_store.py`：首批 JSON、后续 XML next 链接采集与安全分页。
- `src/app_review_insights/llm/schemas.py`：评论摘要和证据语义审计的结构化输出 Schema。
- `src/app_review_insights/llm/prompts.py`：摘要、归并和证据审计约束。
- `src/app_review_insights/pipeline/analyze.py`：评论摘要合并、携带原文归并、证据审计调用。
- `src/app_review_insights/pipeline/validate.py`：结构、引用和语义角色的确定性验证。
- `src/app_review_insights/pipeline/orchestrator.py`：审计检查点、失败续跑和摘要回写。
- `src/app_review_insights/models.py`：审计结果、Finding 校验状态与新流水线阶段。
- `src/app_review_insights/ui/main.py`：动态输入模式、模型状态和按钮可用性。
- `src/app_review_insights/ui/components.py`：中文摘要、证据详情、Schema/语义/局限和 PRD 元数据展示。
- `run_eval.ps1`：根目录 Prompt 评测入口。
- `tests/`：逐项回归和端到端检查。

### Task 1: 中文 CSV 导入兼容

**Files:**
- Modify: `tests/test_input_parsing.py`
- Modify: `src/app_review_insights/input_parsing.py`

- [ ] **Step 1: 写入失败测试**

```python
def test_import_csv_accepts_chinese_export_columns():
    payload = (
        "评论 ID,评分,App 版本,发布时间,语言,评论原文,数据来源\n"
        "r-zh-1,1,8.5.0,2026-08-13T21:59:17Z,en,Paywall blocks workouts,Apple RSS（美国区）\n"
    ).encode("utf-8-sig")

    reviews = import_reviews(payload, "评论.csv", app_id="imported-app")

    assert reviews[0].review_id == "r-zh-1"
    assert reviews[0].content_original == "Paywall blocks workouts"
    assert reviews[0].app_version == "8.5.0"
    assert reviews[0].language == "en"
```

- [ ] **Step 2: 验证 RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_input_parsing.py::test_import_csv_accepts_chinese_export_columns -q`

Expected: FAIL，提示第 1 条评论缺少 `content`。

- [ ] **Step 3: 实现最小别名扩展**

```python
content = _require(
    record,
    index,
    "content",
    "content_original",
    "content",
    "review",
    "评论原文",
)
rating = _require(record, index, "rating", "rating", "评分")
published = _require(
    record,
    index,
    "published_at/date",
    "published_at",
    "date",
    "updated",
    "发布时间",
)
```

同时为 `review_id`、`app_version`、`language`、`author` 增加设计文档中的中文别名。

- [ ] **Step 4: 验证 GREEN 与用户文件**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_input_parsing.py -q`

Run: 使用 `import_reviews` 读取 `C:\Users\user\Desktop\评论.csv`，只输出行数、首尾 ID 和字段完整性，不打印完整文件。

Expected: 测试通过，用户 CSV 解析为 100 条评论。

- [ ] **Step 5: 提交**

```powershell
git add tests/test_input_parsing.py src/app_review_insights/input_parsing.py
git commit -m "fix: import localized review csv files"
```

### Task 2: 当前 Apple RSS 协议采集

**Files:**
- Modify: `tests/test_collector.py`
- Modify: `src/app_review_insights/collectors/app_store.py`

- [ ] **Step 1: 写首批 JSON 与 XML next 分页失败测试**

测试首批 URL 必须是：

```python
"https://itunes.apple.com/us/rss/customerreviews/id=839285684/json"
```

首批 JSON 包含 `rel=next`；第二批使用最小 Atom XML，包含一个重复 ID 和一个新 ID。断言采集器跟随 next、解析 XML、去重并返回 51 条。

- [ ] **Step 2: 写不可信 next URL 失败测试**

```python
def test_collector_rejects_non_apple_next_link():
    page_one = [rss_review(f"r-{index}", f"Review {index}") for index in range(50)]
    payload = rss_payload(
        *page_one,
        next_url="https://example.com/reviews.xml",
    )
    client = FakeHttpClient([FakeResponse(payload)])

    reviews = AppStoreCollector(client=client).collect(
        "https://apps.apple.com/us/app/example/id839285684",
        limit=100,
    )

    assert len(reviews) == 50
    assert client.requested_urls == [
        "https://itunes.apple.com/us/rss/customerreviews/id=839285684/json"
    ]
```

- [ ] **Step 3: 验证 RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_collector.py -q`

Expected: 旧分页 URL 断言和 XML 解析测试失败。

- [ ] **Step 4: 实现协议适配**

```python
_FIRST_PAGE_URL = "https://itunes.apple.com/us/rss/customerreviews/id={app_id}/json"

def _safe_next_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != "itunes.apple.com":
        return None
    return value
```

实现 JSON/Atom XML 两种页面解析器；XML 使用 `xml.etree.ElementTree`，映射 `id`、`title`、`content`、`im:rating`、`updated`、`author/name`、`im:version`。后续页异常时返回已采集数据，首批异常时抛出 `CollectionError`。

- [ ] **Step 5: 验证 GREEN 与真实 URL**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_collector.py -q`

Run: 真实采集 `https://apps.apple.com/us/app/workout-for-women-home-gym/id839285684` 的 20 条和 100 条评论，输出数量、唯一 ID 数和页码分布。

Expected: 20 条场景成功；100 条场景获得两页且 ID 唯一，或在上游后续页暂时失败时至少保留首批并明确数量。

- [ ] **Step 6: 提交**

```powershell
git add tests/test_collector.py src/app_review_insights/collectors/app_store.py
git commit -m "fix: follow current apple review feed pagination"
```

### Task 3: 动态数据来源与模型禁用状态

**Files:**
- Modify: `.env.example`
- Modify: `tests/test_config.py`
- Modify: `tests/test_app_smoke.py`
- Modify: `src/app_review_insights/config.py`
- Modify: `src/app_review_insights/ui/main.py`

- [ ] **Step 1: 写动态输入失败测试**

AppTest 分别选择三种模式并断言：

```python
assert [item.label for item in online.text_input] == ["App 地址（URL）"]
assert [item.label for item in online.file_uploader] == []
assert [item.label for item in json_mode.text_input] == []
assert json_mode.file_uploader[0].label == "JSON 评论文件"
assert csv_mode.file_uploader[0].label == "CSV 评论文件"
```

同时断言在线评论数量最大值为 500，文件导入最大值为 1000，步长为 1。

- [ ] **Step 2: 写显式禁用失败测试**

```python
app_path = Path(__file__).parents[1] / "app.py"
monkeypatch.setenv("MODEL_ENABLED", "false")
monkeypatch.setenv("DEEPSEEK_API_KEY", "configured-but-disabled")
monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))

app = AppTest.from_file(str(app_path)).run(timeout=10)
start_button = next(button for button in app.button if button.label == "开始分析")

assert start_button.disabled is True
assert any("已显式禁用" in item.value for item in app.warning)
```

- [ ] **Step 3: 验证 RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_config.py tests/test_app_smoke.py -q`

Expected: 模式专属控件和 `MODEL_ENABLED` 测试失败。

- [ ] **Step 4: 实现模式外置和配置状态**

```python
model_enabled: bool = Field(default=True, alias="MODEL_ENABLED")
```

在 `main()` 中先渲染 `st.segmented_control(key="source-mode")`，再把 `source_label` 传入 `_render_input_form`。表单只渲染当前模式字段；按钮禁用条件是 `not settings.model_enabled or not settings.deepseek_api_key`。

状态文案使用“未配置 / 已填写待验证 / 已验证可调用 / 已显式禁用”，不把非空密钥直接声明为可用。

- [ ] **Step 5: 验证 GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_config.py tests/test_app_smoke.py -q`

Expected: 全部通过；AppTest 中 CSV 模式不再显示 App URL，在线模式不再显示上传控件。

- [ ] **Step 6: 提交**

```powershell
git add .env.example tests/test_config.py tests/test_app_smoke.py src/app_review_insights/config.py src/app_review_insights/ui/main.py
git commit -m "fix: separate input modes and model availability"
```

### Task 4: 批次中文摘要

**Files:**
- Modify: `tests/test_analysis.py`
- Modify: `tests/test_orchestrator.py`
- Modify: `src/app_review_insights/llm/schemas.py`
- Modify: `src/app_review_insights/llm/prompts.py`
- Modify: `src/app_review_insights/pipeline/analyze.py`
- Modify: `src/app_review_insights/pipeline/orchestrator.py`

- [ ] **Step 1: 写摘要 Schema 与过滤失败测试**

```python
ReviewSummaryDraft(review_id="r-1", summary_zh="用户反馈续费日期不清晰")
```

断言 `analyze_batch` Prompt 要求对输入 ID 返回忠实中文摘要；`apply_review_summaries` 删除未知/重复 ID，并只更新合法评论。

- [ ] **Step 2: 写摘要检查点失败测试**

在 orchestrator 测试中令批次结果返回两条摘要，断言最终 `Stage.CLEAN` 输出的评论包含 `content_summary_zh`；模型中断前已保存的清洗评论仍存在。

- [ ] **Step 3: 验证 RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_analysis.py tests/test_orchestrator.py -q`

Expected: `ReviewSummaryDraft` 或摘要合并函数不存在。

- [ ] **Step 4: 实现摘要 Schema 与回写**

```python
class ReviewSummaryDraft(BaseModel):
    review_id: str
    summary_zh: str = Field(min_length=1)

class BatchAnalysisResult(BaseModel):
    findings: list[FindingDraft]
    review_summaries: list[ReviewSummaryDraft] = Field(default_factory=list)
    batch_limitations: list[str] = Field(default_factory=list)
```

批次全部完成后调用 `apply_review_summaries(cleaned_reviews, batch_results)`，将摘要写回同一 `Stage.CLEAN` 输出并保留原清洗统计。

- [ ] **Step 5: 验证 GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_analysis.py tests/test_orchestrator.py -q`

Expected: 摘要过滤、回写和模型失败保留数据测试通过。

- [ ] **Step 6: 提交**

```powershell
git add tests/test_analysis.py tests/test_orchestrator.py src/app_review_insights/llm/schemas.py src/app_review_insights/llm/prompts.py src/app_review_insights/pipeline/analyze.py src/app_review_insights/pipeline/orchestrator.py
git commit -m "feat: persist traceable chinese review summaries"
```

### Task 5: 证据语义审计与确定性降级

**Files:**
- Modify: `tests/test_analysis.py`
- Modify: `tests/test_validation.py`
- Modify: `tests/test_orchestrator.py`
- Modify: `src/app_review_insights/models.py`
- Modify: `src/app_review_insights/llm/schemas.py`
- Modify: `src/app_review_insights/llm/prompts.py`
- Modify: `src/app_review_insights/pipeline/analyze.py`
- Modify: `src/app_review_insights/pipeline/validate.py`
- Modify: `src/app_review_insights/pipeline/orchestrator.py`
- Modify: `src/app_review_insights/ui/main.py`

- [ ] **Step 1: 写携带正文的归并失败测试**

断言 `consolidate_findings(provider, batch_results, analysis_goal, reviews)` 的用户 Prompt 包含候选引用评论的真实 `content_original`，不包含未引用评论。

- [ ] **Step 2: 写审计 Schema 和判定失败测试**

```python
EvidenceAssessmentDraft(
    review_id="r-1",
    role="supporting",
    rationale_zh="评论明确指出免费试用后立即扣费",
)
```

验证以下行为：

- 无关支持证据被移除；
- 原支持证据可被改判为冲突；
- 未审计或未知 ID 产生 ValidationIssue；
- 无支持证据的 Finding 被拒绝；
- 支持不足降级为 `ASSUMPTION`；
- 全部引用完成审计且达到阈值才为 `VALIDATED`。

- [ ] **Step 3: 验证 RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_analysis.py tests/test_validation.py tests/test_orchestrator.py -q`

Expected: 审计类型、阶段或新签名不存在。

- [ ] **Step 4: 实现审计模型与阶段**

在 `Stage` 增加 `AUDIT_EVIDENCE = "audit_evidence"`；新增领域 `EvidenceAssessment` 和 Finding 字段：

```python
schema_validated: bool = True
reference_validated: bool = False
semantic_validated: bool = False
evidence_assessments: list[EvidenceAssessment] = Field(default_factory=list)
```

新增 `audit_finding_evidence`，一次调用审计全部候选 Finding。orchestrator 将输出保存在 `Stage.AUDIT_EVIDENCE`，模型异常时进入 `waiting_for_model` 并保留清洗、批次和归并检查点。

- [ ] **Step 5: 实现确定性验证**

`validate_finding_drafts(drafts, reviews, audit)` 以审计后的 `supporting/conflicting/irrelevant` 角色重建证据列表，计算计数、置信度和局限。缺失审计不得显示为语义已复核。

- [ ] **Step 6: 验证 GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_analysis.py tests/test_validation.py tests/test_orchestrator.py tests/test_traceability.py -q`

Expected: 全部通过，模型失败续跑检查点保持不变。

- [ ] **Step 7: 提交**

```powershell
git add tests/test_analysis.py tests/test_validation.py tests/test_orchestrator.py src/app_review_insights/models.py src/app_review_insights/llm/schemas.py src/app_review_insights/llm/prompts.py src/app_review_insights/pipeline/analyze.py src/app_review_insights/pipeline/validate.py src/app_review_insights/pipeline/orchestrator.py src/app_review_insights/ui/main.py
git commit -m "feat: audit evidence semantics before planning"
```

### Task 6: 清晰展示 Schema、证据、局限和目标版本

**Files:**
- Modify: `tests/test_ui_localization.py`
- Modify: `tests/test_app_smoke.py`
- Modify: `src/app_review_insights/ui/components.py`

- [ ] **Step 1: 写展示失败测试**

测试 `_display_records_frame` 在摘要全空时仍保留“中文摘要”列并显示“待生成”。使用 AppTest/可测试辅助函数断言：

- 问题发现包含“Schema 校验”“引用存在性”“证据语义”“局限说明”；
- 每条证据显示评论 ID、原文、中文摘要和审计理由；
- PRD 明确显示“目标版本：V1.0”和“业务假设”，不把目标版本标成假设。

- [ ] **Step 2: 验证 RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ui_localization.py tests/test_app_smoke.py -q`

Expected: 中文摘要空列和明确状态文本断言失败。

- [ ] **Step 3: 实现最小展示调整**

评论表对 `content_summary_zh` 使用“待生成”占位。Finding 卡片从清洗评论索引读取证据详情；徽章和字段标题分别表达 Schema、引用、语义和局限。PRD 元数据新增独立目标版本行，`assumptions` 标题改为“业务假设”。

- [ ] **Step 4: 验证 GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ui_localization.py tests/test_app_smoke.py -q`

Expected: 全部通过。

- [ ] **Step 5: 提交**

```powershell
git add tests/test_ui_localization.py tests/test_app_smoke.py src/app_review_insights/ui/components.py
git commit -m "fix: clarify evidence and prd validation states"
```

### Task 7: 根目录评测入口

**Files:**
- Create: `run_eval.ps1`
- Modify: `docs/model-and-prompts.md`
- Create: `tests/test_eval_entrypoint.py`

- [ ] **Step 1: 写入口失败测试**

使用 `subprocess.run` 调用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\run_eval.ps1
```

断言退出码为 0、输出 JSON 中 `live_model_called` 为 `false`。另用临时目录模拟缺少虚拟环境，断言中文提示和非零退出码。

- [ ] **Step 2: 验证 RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_eval_entrypoint.py -q`

Expected: `run_eval.ps1` 不存在。

- [ ] **Step 3: 实现 PowerShell 包装脚本**

脚本接受 `-Live`、`-Dataset`、`-Output`，验证 `.venv\Scripts\python.exe` 与 `scripts\run_eval.py` 后使用参数数组调用 Python；不拼接可执行字符串，不读取或打印密钥。

- [ ] **Step 4: 验证 GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_eval_entrypoint.py -q`

Run: `.\run_eval.ps1`

Expected: 测试和非实时评测均成功。

- [ ] **Step 5: 提交**

```powershell
git add run_eval.ps1 docs/model-and-prompts.md tests/test_eval_entrypoint.py
git commit -m "feat: add direct prompt evaluation entrypoint"
```

### Task 8: 自动化回归与真实端到端验收

**Files:**
- Modify: tests and source files only if a newly reproduced bug first receives a failing regression test
- Modify: `.planning/2026-08-15-demo-offline/task_plan.md` and progress files (ignored local planning records)

- [ ] **Step 1: 全量静态和自动化验证**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m compileall -q src scripts app.py
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

Expected: 全部退出码为 0。

- [ ] **Step 2: 用户 CSV 与 JSON 验收**

使用隔离数据库运行用户 CSV 100 条完整流水线；下载清洗评论 JSON、PRD JSON、测试用例 CSV 和证据链 CSV，并重新解析所有文件。另用 `data/samples/reviews-sample.json` 验证 JSON 模式。

Expected: Schema 通过，所有引用 ID 存在，语义审计已完成，追溯链通过；英文评论有中文摘要或明确“待生成”，原文不丢失。

- [ ] **Step 3: 真实在线验收**

使用真实 URL 和隔离数据库运行 20 条及至少 100 条场景。检查采集页码、唯一 ID、实际数量、状态事件和下载文件。

Expected: 采集成功；若 Apple 后续页瞬时失败，首批数据保留且页面明确披露短缺，不出现虚假 100% 数据覆盖声明。

- [ ] **Step 4: 模型状态与不中断验收**

分别启动：

- 有效 `.env`；
- `MODEL_ENABLED=false`；
- 临时无 `.env`/无密钥的隔离进程；
- 无效密钥的隔离进程。

Expected: 按钮和状态与设计一致；无效密钥运行进入等待恢复，采集/清洗输出仍存在；切回有效密钥后同一 `run_id` 可继续。

- [ ] **Step 5: 浏览器交互与证据人工抽查**

在本地 Streamlit 页面切换在线、JSON、CSV；检查窄屏和正常宽度；逐个 Finding 至少抽查一条支持证据，重点审查低证据量、冲突证据、相似主题和旧运行中曾出现的 F-008/F-002/F-010 类场景。

Expected: 页面无控制台 error/warn；证据原文、中文摘要和审计理由对号入座；目标版本与业务假设没有混淆。

- [ ] **Step 6: 实时 Prompt 评测与安全检查**

Run:

```powershell
.\run_eval.ps1 -Live -Output output\manual-prompt-eval.json
git grep -n -I -E "sk-[A-Za-z0-9_-]{8,}|DEEPSEEK_API_KEY=.+" -- . ":(exclude).env" ":(exclude)data/runs/**"
git status --short
```

Expected: 评测报告能解析、structured output 有明确结果；仓库没有密钥；只存在本任务预期变更。

- [ ] **Step 7: 最终提交**

```powershell
git add -A
git commit -m "fix: harden real-data evidence workflow"
```

仅在所有验收证据完成后提交；若前面任务已分别提交且无额外改动，则不创建空提交。

### Task 9: 清洗阶段消除重复评论 ID 歧义

**Files:**
- Modify: `src/app_review_insights/cleaning.py`
- Modify: `src/app_review_insights/ui/components.py`
- Test: `tests/test_cleaning.py`
- Test: `tests/test_app_smoke.py`

- [ ] **Step 1: 写重复 ID 的失败测试**

在 `tests/test_cleaning.py` 新增测试：输入三条相同 `review_id`，其中两条规范化正文相同、第三条正文不同。断言相同正文只保留一条，不同正文保留且得到以 `原 ID--内容哈希短后缀` 开头的唯一 ID；`review_id_collisions == 1`，清洗后所有 ID 唯一。

在 `tests/test_app_smoke.py` 新增 `_run_limitations` 测试，断言 `review_id_collisions > 0` 时返回中文披露，说明冲突记录已重命名且证据链使用新 ID。

- [ ] **Step 2: 验证 RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cleaning.py tests/test_app_smoke.py -q
```

Expected: `CleaningStats` 尚无 `review_id_collisions`，且冲突 ID 尚未重命名，因此新测试失败。

- [ ] **Step 3: 实现最小清洗修复**

在 `CleaningStats` 增加默认值为 `0` 的 `review_id_collisions`。`clean_reviews` 为每个原始 ID 维护已见规范化正文集合：相同 ID + 相同正文计入精确重复并跳过；相同 ID + 不同正文计入冲突，并用当前内容哈希构造稳定短后缀，必要时增加数字后缀避免极小概率碰撞。写入 `kept` 前保证最终 ID 未被使用。

`_run_limitations` 在保留在线采集短缺说明的同时，为任意数据源追加重复 ID 冲突说明。

- [ ] **Step 4: 验证 GREEN 并提交**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cleaning.py tests/test_app_smoke.py -q
```

Expected: 全部通过。

```powershell
git add src/app_review_insights/cleaning.py src/app_review_insights/ui/components.py tests/test_cleaning.py tests/test_app_smoke.py
git commit -m "fix: keep review evidence ids unique"
```

### Task 10: 为归并和证据复核补齐元数据

**Files:**
- Modify: `src/app_review_insights/pipeline/analyze.py`
- Test: `tests/test_analysis.py`

- [ ] **Step 1: 写证据元数据的失败测试**

在 `tests/test_analysis.py` 分别调用 `consolidate_findings` 和 `audit_finding_evidence`，传入 `rating=1`、`app_version="2.4.1"`、`language="en"` 的引用评论。解析或检查 Provider 收到的用户 Prompt，断言两处证据载荷均包含这三个字段及其值，且未引用的评论不进入载荷。

- [ ] **Step 2: 验证 RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_analysis.py -q
```

Expected: Prompt 目前只包含 ID、原文和中文摘要，新断言失败。

- [ ] **Step 3: 实现最小证据载荷修复**

把 `consolidate_findings` 和 `audit_finding_evidence` 两处 `model_dump(include=...)` 的字段集合统一扩展为：

```python
{
    "review_id",
    "content_original",
    "content_summary_zh",
    "rating",
    "app_version",
    "language",
}
```

不添加默认评分、版本或语言，保持缺失值为 `null`。

- [ ] **Step 4: 验证 GREEN 并提交**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_analysis.py -q
```

Expected: 全部通过。

```powershell
git add src/app_review_insights/pipeline/analyze.py tests/test_analysis.py
git commit -m "fix: include metadata in evidence review prompts"
```

### Task 11: 将模型验证状态绑定当前配置

**Files:**
- Modify: `src/app_review_insights/ui/main.py`
- Test: `tests/test_app_smoke.py`

- [ ] **Step 1: 写配置指纹的失败测试**

在 `tests/test_app_smoke.py` 新增纯函数级测试：配置 A 成功后状态为 `verified`；仅更换密钥、模型名或 Base URL 后状态回到 `configured`；重新记录成功后绑定新配置。断言会话状态不保存明文密钥。

- [ ] **Step 2: 验证 RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_app_smoke.py -q
```

Expected: 当前只有 `model_verified: bool`，无法区分配置变化，新测试失败。

- [ ] **Step 3: 实现最小指纹修复**

新增 `_model_config_fingerprint(settings)`，对 `deepseek_api_key`、`model_provider`、`model_name` 和 `model_base_url` 以不可歧义分隔符拼接后计算 SHA-256。`_model_state` 仅在会话中的 `model_verified_fingerprint` 与当前指纹一致时返回 `verified`；`_record_model_success` 在首批模型输出确实持久化后记录当前指纹。两个函数允许注入映射用于单元测试，默认使用 `st.session_state`。

- [ ] **Step 4: 验证 GREEN、全量回归并提交**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_app_smoke.py -q
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check src tests scripts app.py
.\.venv\Scripts\python.exe -m compileall -q src scripts app.py
.\.venv\Scripts\python.exe -m pip check
.\run_eval.ps1
git diff --check
```

Expected: 全部退出码为 0；测试总数增加且全部通过。

```powershell
git add src/app_review_insights/ui/main.py tests/test_app_smoke.py
git commit -m "fix: bind model verification to current config"
```
