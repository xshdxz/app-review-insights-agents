# App Review Insights

Turn US App Store reviews into evidence-grounded product findings, versioned PRD requirements, and traceable test cases — a local, single-page Chinese analysis workbench.

This repository is an engineering project. It deliberately keeps every claim verifiable: **Review → Finding → Requirement → TestCase** is the only path into the final result view, and every step is persisted as a checkpoint so a failed model call never loses completed work.

> 中文说明见 [README.zh-CN.md](README.zh-CN.md)（中英文描述同一功能范围）。

---

## What it does

1. **Input**
   - **Online collection** from the US App Store via Apple's public RSS feed (up to 500 reviews in practice, most recent first). Only US storefront links are accepted.
   - **JSON/CSV import** of your own review files (see `docs/data-format.md`).
   - **Offline demo archive**: a bundled, clearly labeled *historical cache* (never presented as a live result) so the UI can be demonstrated without an API key.

2. **Deterministic processing** (Python, no model involved)
   - Normalization, language detection, exact + near-duplicate removal, review-ID collision handling.
   - Batching by review count and character count.
   - Evidence validation: model-cited review IDs must exist; support/conflict counts, confidence, and limitations are recomputed deterministically; findings without support are rejected, and findings with thin support are marked as **Assumption**.
   - Priority scoring and traceability validation of the full chain.

3. **Structured LLM analysis** (DeepSeek `deepseek-chat`)
   - Per-batch dynamic topic discovery with a Chinese summary for every review.
   - Cross-batch consolidation, per-citation evidence audit, requirement drafting (5–10 when evidence allows, honestly fewer otherwise), and test-case drafting (2–4 per requirement).
   - JSON output is validated against Pydantic schemas; retries are bounded; on final failure the run is paused at a checkpoint.

4. **Checkpoint resume**
   - Every stage and batch output is persisted to SQLite (`data/runs/runs.sqlite3` by default).
   - A paused run can be resumed with the **same `run_id`**; completed batches are never re-run, even if batch sizes change.

5. **Deliverables** — download cleaned reviews (JSON), PRD (JSON), test cases (CSV), and the full traceability matrix (CSV), all UTF-8 with stable IDs.

## Quick start (Windows PowerShell)

```powershell
git clone https://github.com/xshdxz/app-review-insights-agents.git
cd app-review-insights
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\streamlit run app.py
```

Open http://localhost:8501. Without a DeepSeek key you can still explore the offline demo archive and the sample data; the "开始分析" button stays disabled and the UI says why.

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `DEEPSEEK_API_KEY` | *(empty)* | DeepSeek API key. Never commit a real key; `.env` is git-ignored. |
| `MODEL_ENABLED` | `true` | Set to `false` to force demo mode even with a key. |
| `MODEL_NAME` | `deepseek-chat` | Model name. |
| `MODEL_BASE_URL` | `https://api.deepseek.com` | OpenAI-compatible endpoint. |
| `MODEL_TIMEOUT_SECONDS` | `60` | Per-request timeout. |
| `MODEL_MAX_RETRIES` | `2` | Bounded retry budget (application-level; SDK retries are disabled). |
| `MODEL_MAX_TOKENS` | `8192` | Max completion tokens. Must be high enough for per-review summaries; 4096 (API default) truncates large batches. |
| `DATABASE_PATH` | `data/runs/runs.sqlite3` | Checkpoint database. |
| `DEFAULT_REVIEW_LIMIT` | `500` | Default slider value; both modes allow 100–1000 (online collection returns whatever Apple's feed provides, disclosed in run limitations). |
| `BATCH_REVIEW_LIMIT` / `BATCH_MAX_CHARACTERS` | `100` / `60000` | Batching limits per model call. |

Key handling: the key is only read into the provider at runtime; it is never logged, exported, or included in downloads; error messages redact key-like strings, review texts, and `.env` references.

## Responsibility split (why no multi-agent framework)

The pipeline is a **synchronous state machine orchestrator** with single-purpose Python modules, not a multi-agent system. Reasons:

- The task is a fixed, sequential pipeline with deterministic acceptance checks between stages. Agents add orchestration overhead and nondeterminism without adding capability.
- Evidence integrity depends on *programmatic* validation (ID existence, counts, confidence, traceability) that must be reproducible and testable — this is deterministic code, not model judgment.
- Every stage persists before the next begins, so a failed model call pauses exactly where it happened and resumes without repeating completed work. Checkpoint semantics are trivial to reason about in a state machine.

Model responsibilities: topic discovery, Chinese summarization, consolidation, evidence audit rationales, requirement/test drafting.
Program responsibilities: collection/import, cleaning, counting, ID validation, confidence, priority scoring, schema validation, traceability, checkpointing, exports.

## Data source and limits

- Apple RSS: `https://itunes.apple.com/us/rss/customerreviews/page={n}/id={appId}/sortby=mostrecent/json` — up to 50 reviews per page, 10 pages, **500 reviews max**.
- Online collection only accepts US storefront links; other regions must use JSON/CSV import.
- Apple occasionally changes this public feed. The collector tries the current feed URL first and falls back to the legacy first-page URL; if both return no data, a clear message directs you to JSON/CSV import or retry later. During such upstream changes some apps may return fewer or no reviews; the shortfall is disclosed in the run's limitation list.
- If the feed returns fewer reviews than requested, the shortfall is disclosed in the run's limitation list.
- If your machine has an HTTP proxy configured (`HTTP_PROXY`/`HTTPS_PROXY`) that is not running, collection and model calls fail with a connection error — start the proxy or temporarily remove those environment variables.

## Testing and evaluation

```powershell
# Full test suite
.\.venv\Scripts\python -m pytest

# Coverage report
.\.venv\Scripts\python -m pytest --cov=app_review_insights --cov-report=term-missing

# Lint and format
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m ruff format --check .

# Prompt evaluation (dry run: validates the dataset only)
.\run_eval.ps1
# Live DeepSeek evaluation
.\run_eval.ps1 -Live -Output output\prompt-eval.json

# Real end-to-end validation with live model (online collection or import)
.\.venv\Scripts\python scripts/run_real_validation.py `
    --app-url "https://apps.apple.com/us/app/workout-for-women-home-gym/id839285684" `
    --goal "重点分析订阅转化" --limit 200 --out output/real-run.json
.\.venv\Scripts\python scripts/run_real_validation.py `
    --file data/samples/reviews-sample.json --goal "识别易用性问题" --out output/import-run.json
# Resume a paused run with the same run_id
.\.venv\Scripts\python scripts/run_real_validation.py `
    --resume <RUN_ID> --goal "同原目标" --out output/resumed-run.json
```

The real-run script verifies the evidence chain (referenced IDs exist, counts match, confidence in range, inheritance rules, traceability valid) and exits non-zero on any violation.

## Repository hygiene

- `.env`, `data/runs/`, `output/`, `tmp/`, `.planning/`, `.superpowers/`, and the private local materials are git-ignored.
- Before submitting, run `git status --short --ignored` and the key scan:

```powershell
git grep -n -I -E "(sk-[A-Za-z0-9_-]{12,}|DEEPSEEK_API_KEY=.+)" -- . ':!.env.example'
```

## Architecture and docs

- `docs/architecture.md` — pipeline stages and persistence.
- `docs/data-format.md` — JSON/CSV import format.
- `docs/model-and-prompts.md` — model/prompt design and evaluation records.
- `docs/defect-list.md` — known issues by severity (updated during development).
