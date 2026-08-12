# 设计文档：App Review Insights

## 1. 项目背景

本项目定位为产品情报原型。目标是构建一个本地可运行的产品原型，将美国区 App Store 真实用户评论转化为有证据支撑的产品发现、版本规划、PRD 和测试用例。



## 2. 设计目标

### 2.1 项目目标

构建中文 Streamlit 工作台，使用户能够：

1. 输入有效的美国区 App Store 链接。
2. 输入自然语言分析目标或约束。
3. 在线采集美国区评论，或导入 JSON/CSV 评论数据。
4. 自动完成数据清洗、动态语义分析、证据校验、版本规划、PRD 和测试用例生成。
5. 查看每个阶段的进度、中间结果、错误、修订和最终交付物。
6. 从每条测试用例回溯到需求、产品发现和原始用户评论。
7. 在模型或网络暂时不可用时保留已有成果，并从检查点继续运行。

### 2.2 设计原则

项目需要自然展示以下能力，而不是依赖概念包装：

- 将真实业务问题拆解成 AI 工作流。
- 设计 Prompt、模型输入输出和工具调用边界。
- 合理组合确定性规则、统计方法和大语言模型。
- 控制模型幻觉和无证据结论。
- 使用真实任务和人工标注样本评估 Prompt 效果。
- 将分析流程产品化为可运行、可观察、可恢复的原型。

## 3. 非目标

首个提交版本不包含：

- 多个 Agent 互相对话的重型架构。
- 独立 FastAPI 服务和前后端分离部署。
- 用户注册、登录、权限和云端多租户。
- 自动修改或发布目标 iOS App。
- 与评分无关的复杂动画和大量装饰性图表。
- 为示例健身 App 硬编码分类、结论、需求或测试用例。

## 4. 技术方案

### 4.1 技术栈

- Python 3.11 或兼容版本。
- Streamlit：中文 UI 和本地运行入口。
- Pydantic：领域模型和模型结构化输出校验。
- DeepSeek `deepseek-chat`：默认语义模型。
- OpenAI-compatible 客户端：统一模型供应商接口。
- SQLite：保存运行记录、批次状态、检查点和结构化结果。
- JSON/CSV：外部数据导入、示例数据和结果导出。
- pytest：数据处理、证据校验、恢复流程和端到端测试。

### 4.2 架构原则

Streamlit 只负责交互和展示，业务逻辑放入独立 Python 模块。模块边界如下：

- `collectors`：App ID 解析、App 元数据和美国区评论采集。
- `importers`：JSON/CSV 格式校验与导入。
- `cleaning`：字段规范化、精确去重、近似去重和质量统计。
- `llm`：DeepSeek provider、Prompt、结构化输出和调用重试。
- `analysis`：分批主题发现、跨批归并、冲突识别和证据摘要。
- `planning`：优先级、版本规划和 PRD 生成。
- `testing_generation`：目标 App 测试用例生成。
- `validation`：引用、样本数、完整性和追溯链校验。
- `orchestration`：阶段状态、检查点、恢复和事件日志。
- `storage`：SQLite、缓存和导出。
- `ui`：Streamlit 页面、标签页、状态和错误展示。

首版采用单一轻量 orchestrator/state machine，不引入多 Agent 框架。若后续出现真实的并发任务或远程 API 需求，再评估 FastAPI。

## 5. 数据输入与来源

系统支持三条输入路径：

1. 美国区 App Store 在线评论采集，作为正式主路径。
2. 文档化 JSON/CSV 导入，用于陌生数据集和评测兼容。
3. 明确标记的历史缓存样例，用于断网审阅。

在线采集要求：

- 只使用美国区 storefront 评论。
- 记录来源、采集时间、App ID、分页范围和实际样本量。
- 默认最近 500 条，用户可在 100 至 1000 条之间调整。
- 实现分页、限速、超时和有限重试。
- 不通过抓取 App 详情页上有限的可见评论作为主数据源。
- 如果采集源受限或返回数据不足，必须在结果中明确披露。

缓存结果不能替代处理新输入的能力，UI 必须显示“历史缓存演示”状态。

## 6. 核心数据模型

### 6.1 Review

- `review_id`
- `app_id`
- `storefront`
- `title`
- `content_original`
- `content_summary_zh`，可选
- `rating`
- `app_version`
- `author`
- `published_at`
- `language`
- `source`
- `source_page`
- `content_hash`

原始评论永远保留原文。中文摘要只能辅助阅读，不能替代证据。

### 6.2 Finding

- `finding_id`
- `title`
- `problem_statement`
- `topic_label`
- `supporting_review_ids`
- `conflicting_review_ids`
- `support_count`
- `conflict_count`
- `confidence`
- `evidence_status`
- `model_reasoning_summary`
- `limitations`

### 6.3 Requirement

- `requirement_id`
- `finding_ids`
- `title`
- `user_problem`
- `objective`
- `scope`
- `non_goals`
- `functional_rules`
- `edge_cases`
- `acceptance_criteria`
- `success_metrics`
- `priority_inputs`
- `priority_score`
- `target_version`
- `source_review_ids`
- `assumptions`

### 6.4 TestCase

- `test_case_id`
- `requirement_id`
- `title`
- `preconditions`
- `steps`
- `expected_result`
- `case_type`
- `source_review_ids`

### 6.5 ValidationResult

- `validation_id`
- `entity_type`
- `entity_id`
- `rule`
- `status`
- `severity`
- `message`
- `revision_action`

### 6.6 RunState

- `run_id`
- `input_source`
- `analysis_goal`
- `current_stage`
- `current_batch`
- `total_batches`
- `status`
- `coverage_ratio`
- `model_provider`
- `model_name`
- `created_at`
- `updated_at`
- `last_error`

## 7. 工作流与模型调用

### 7.1 阶段状态机

标准流程为：

```text
scope
→ collect/import
→ clean
→ batch_analyze
→ consolidate
→ validate_findings
→ plan_versions
→ generate_prd
→ generate_test_cases
→ validate_traceability
→ complete
```

每个阶段有明确输入、输出、状态和事件记录。完成阶段后立即写入检查点。

### 7.2 模型调用策略

500 条评论默认分为约 5 批，每批约 100 条：

1. 每批动态发现候选问题，并返回引用评论 ID。
2. 跨批次合并同义问题，保留所有支持与冲突证据。
3. 基于通过校验的 Finding 生成版本计划和 5 至 10 个核心需求。
4. 基于需求生成测试用例。
5. 对问题归并或失败修订进行必要的额外模型调用。

一次完整运行预计约 8 至 10 次模型调用。模型调用数量不是硬指标，正确性和可恢复性优先。

### 7.3 Prompt 原则

- 只向模型提供带稳定 `review_id` 的评论。
- 要求严格结构化输出，并由 Pydantic 校验。
- 明确区分用户证据、确定性统计、模型归纳和产品假设。
- 禁止模型生成未经引用支持的精确数量或比例。
- 要求输出冲突反馈、未知项和数据局限。
- 需求只能基于已通过证据校验的 Finding。
- 实施复杂度是产品假设，必须单独标记。

## 8. 证据校验与防幻觉

完整追溯链为：

```text
Review → Finding → Requirement → TestCase
```

确定性校验至少包括：

- 所有引用的 `review_id` 必须存在。
- 支持样本数和冲突样本数由程序重新计算。
- 同一评论不能因重复导入被重复计数。
- Finding 没有足够证据时不能成为高优先级需求。
- Requirement 必须关联至少一个通过验证的 Finding。
- TestCase 必须关联 Requirement，并继承可追溯的来源评论。
- 验收标准必须可验证，不能只写“体验更好”等模糊描述。
- 无法修正的弱结论必须删除或标为低置信度假设。

证据门槛不采用一个对所有数据集僵化的固定数字。系统结合样本数量、来源独立性、评论具体程度、冲突比例和分析目标判断，并公开展示理由。

## 9. PRD 与版本规划

主 PRD 默认包含 5 至 10 个经过证据验证的核心需求。证据不足时允许少于 5 个，并解释原因。超过 10 个的低优先级候选进入 Backlog。

每个需求包含：

- 背景和用户问题。
- 来源 Finding 与原始评论。
- 产品目标和成功指标。
- 功能范围与非目标。
- 详细规则。
- 异常和边界场景。
- 验收标准。
- 优先级和目标版本。
- 已知假设与限制。

版本分为：

- `V1.0`：高影响、高置信度、相对可实施。
- `V1.1`：价值明确，但成本更高或依赖 V1.0。
- `Future/Backlog`：证据较弱、范围较大或需要进一步研究。

优先级综合考虑：

```text
用户影响 × 评论频次 × 证据置信度 ÷ 假设实施复杂度
```

该公式用于解释决策，不伪装成精确的商业价值计算。评分组成和假设在 UI 中可见。

## 10. 测试用例生成

每个需求默认生成 2 至 4 条测试用例，总量预计 15 至 30 条。

用例覆盖：

- 正常流程。
- 异常流程。
- 关键边界条件。
- 与原始投诉直接相关的回归场景。

每条用例必须包含需求 ID 和来源评论 ID。没有证据链的用例不能进入最终输出。

## 11. 模型和网络故障体验

故障处理的首要目标是不中断用户体验和不丢失成果，而不是偷偷切换模型。

处理流程：

1. 对超时、限流和临时服务异常做有限的指数退避重试。
2. 每批次和每阶段完成后保存检查点。
3. 持续失败时将运行状态设为“部分完成”或“等待恢复”。
4. 继续展示采集、清洗、确定性统计和已验证 Findings。
5. 展示已分析评论覆盖率和失败阶段。
6. 允许下载清洗数据和运行日志。
7. 提供“重试当前批次”和“继续分析”入口。
8. 页面刷新或应用重启后可从 `run_id` 恢复。
9. 未完成验证的内容不能进入正式 PRD。

默认不自动切换模型供应商，以免不同模型造成结论口径不一致。可替换 provider 是工程能力，不是隐式故障策略。

如果模型完全不可用，系统显示确定性数据报告和明确错误，不生成假 PRD。历史完整结果只能在单独的缓存演示模式中查看。

## 12. UI 信息架构

应用采用中文单页工作台。

### 12.1 顶部输入区

- App Store URL。
- 分析目标或约束。
- 评论数量。
- 在线采集或 JSON/CSV 导入。
- 模型和配置状态。
- 开始/继续分析按钮。

### 12.2 右侧执行区

- 阶段状态。
- 当前批次和覆盖率。
- 最近事件。
- 校验失败、重试和修订记录。
- 缓存、部分完成或等待恢复标记。

### 12.3 结果标签页

- 总览。
- 原始与清洗评论。
- 动态主题与 Findings。
- 版本计划与 PRD。
- 测试用例。
- Traceability Matrix。
- 运行日志与数据限制。

所有模型结论标明 `AI-generated`，确定性结果标明 `Deterministic`，通过校验的结果标明 `Validated`，证据不足内容标明 `Assumption`。

## 13. 工程测试与评估

### 13.1 自动化测试

- App ID 和 storefront 解析。
- JSON/CSV 格式与字段校验。
- 精确及近似去重。
- 字段规范化和质量统计。
- 无效引用拦截。
- 样本数重新计算。
- Finding、Requirement 和 TestCase 完整性。
- 不完整追溯链阻止最终发布。
- DeepSeek 超时、格式错误和重试。
- 批次检查点与断点续跑。
- 缓存模式显式标记。

### 13.2 泛化测试

- 示例健身 App。
- 至少一个陌生 App 或陌生兼容数据集。
- 中英文混合评论。
- 重复、冲突和低样本评论。
- 在线采集失败。
- 模型中途失败。
- 全新环境按 README 启动。

### 13.3 Prompt 评测

准备小型人工标注评测集，包含若干明确用户问题及其证据评论。对主要 Prompt 版本记录：

- 关键问题召回情况。
- 无依据结论数量。
- 评论引用正确率。
- 主题重复或过度合并情况。
- 结构化输出成功率。

提交时保留有意义的实验记录和迭代结论，不追求虚假的大规模 benchmark。

## 14. 仓库内容

GitHub 项目包含：

- 完整源码和依赖配置。
- `.env.example`，不含密钥。
- 中英双语 README，至少包含英文快速启动说明。
- 数据采集来源、限制和速率策略。
- JSON/CSV 导入格式文档与样例。
- 明确标记的缓存运行结果。
- 主要 Prompt、模型配置和失败处理说明。
- 自动化测试和运行方法。
- 示例截图或短演示。
- 有意义的完整 Git 提交历史。

## 15. 完成标准

项目达到完成标准需要同时满足：

- 可本地运行。
- 可处理在线链接和导入数据。
- 至少一个核心语义任务由 DeepSeek 在运行时完成。
- 不依赖示例 App 硬编码。
- 主要 Finding、Requirement 和 TestCase 可追溯。
- 模型故障不会丢失已完成工作。
- 缓存和假设均明确标记。
- 自动化测试、陌生数据测试和全新环境启动检查通过。
- README 能让读者独立运行并理解设计取舍。

