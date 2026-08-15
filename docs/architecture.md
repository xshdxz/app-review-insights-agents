# 架构

## 流水线总览

```text
Streamlit 用户界面
  → 编排器（AnalysisOrchestrator）/ RunRepository（SQLite 检查点）
  → 采集器（Apple RSS）或导入器（JSON/CSV）
  → 清洗（规范化 / 语言检测 / 精确+近似去重 / ID 冲突处理）
  → 分批（数量 + 字符数）
  → DeepSeek 结构化分析（逐批主题发现 + 每条评论中文摘要）
  → 跨批次归并（模型）
  → 逐条证据语义审计（模型）
  → 确定性证据校验（引用存在性 / 计数 / 置信度 / 假设标记）
  → PRD 需求生成（模型）+ 确定性优先级评分
  → 测试用例生成（模型）+ 继承式追溯
  → 追溯校验（确定性）→ 导出 / 下载
```

每个阶段都在下一阶段开始前把输出持久化到 SQLite。UI 重新运行时只读取已持久化的输出，绝不隐式重复调用模型；`Stage.ANALYZE_BATCHES` 还保存批次边界清单（`batch_review_indices`），续跑时按原边界恢复批次，避免配置变化导致已完成的批次结果错位。

## 模块职责

| 模块 | 职责 |
|---|---|
| `ui/main.py`、`ui/components.py` | Streamlit 单页工作台：输入表单、运行状态、结果标签页、下载、离线演示档案。不含业务逻辑。 |
| `pipeline/orchestrator.py` | 同步状态机编排器：阶段推进、检查点读写、`start`/`resume`、事件记录。 |
| `pipeline/analyze.py` | 批次分析、归并、证据审计的模型调用与 Prompt 组装。 |
| `pipeline/validate.py` | 确定性证据校验：删除虚构引用、重算计数与置信度、Assumption/Validated/Rejected 判定。 |
| `pipeline/planning.py` | 需求草拟（模型）与确定性优先级评分、版本建议。 |
| `pipeline/test_generation.py` | 测试用例草拟（模型），评论来源从需求继承。 |
| `pipeline/traceability.py` | 全链路追溯校验：ID 唯一、引用存在、来源完整继承、每需求 2–4 条用例。 |
| `cleaning.py` | 规范化、语言检测、去重、ID 冲突稳定重命名。 |
| `batching.py` | 按数量与字符数分批，超长单条评论独占一批。 |
| `collectors/app_store.py` | Apple RSS 采集适配器（httpx），与 `app-store-scraper` 的旧依赖冲突隔离。 |
| `input_parsing.py` | App 链接解析（仅美国区）与 JSON/CSV 导入。 |
| `llm/provider.py` | DeepSeek 客户端：JSON Schema 校验、有限重试、`max_tokens` 上限、错误脱敏。 |
| `llm/prompts.py` | 各阶段 Prompt 与评论渲染。 |
| `llm/schemas.py` | 模型草稿 Schema（Pydantic）。 |
| `storage/repository.py` | SQLite 仓库：runs / stage_outputs / events，连接使用提交+关闭的会话上下文。 |
| `storage/cache.py` | 离线演示档案的严格加载（必须带 `historical_cache_demo` / `is_live=false` 标签）与导出、下载构造。 |
| `export.py` | 证据链 CSV / JSON 下载内容构造。 |

## 状态机

运行状态：`pending → running → waiting_for_model / failed / partial / completed`。

阶段顺序：`scope → collect → clean → analyze_batches → consolidate → audit_evidence → validate_findings → plan → generate_tests → validate_traceability → complete`。

- `waiting_for_model`：模型阶段抛 `RecoverableModelError`，保存检查点，可沿用同一 `run_id` 续跑。
- `failed`：采集失败等不可恢复错误。
- `partial`：追溯校验未通过，结果不可作为正式交付物。
- `completed`：追溯链有效，所有阶段输出齐全。

## 数据流与不可变性

1. 采集/导入的原始评论存 `Stage.COLLECT`，清洗结果存 `Stage.CLEAN`（含统计与冲突披露）。
2. 批次分析结果按 `(run_id, stage, batch_index)` 存储；续跑时先读检查点。
3. 发现、需求、测试用例均保存模型草稿经确定性校验后的最终对象。
4. 证据链校验通过后，UI 才允许下载交付物。

## 技术栈

Python 3.11+、Streamlit 1.61+、Pydantic v2、pydantic-settings、OpenAI 兼容协议的 DeepSeek 客户端、httpx、Apple RSS JSON、pandas、rapidfuzz、langdetect、SQLite、pytest、Ruff。
