# 模型与 Prompt 说明

## 默认配置

- Provider：DeepSeek
- Model：`deepseek-chat`
- Temperature：`0.1`
- Max tokens：`8192`（`MODEL_MAX_TOKENS`；API 默认 4096 会截断含逐条评论摘要的大批次输出）
- 结构化输出：JSON object + Pydantic JSON Schema 校验
- 失败策略：有限重试；仍失败时抛出可恢复错误，由流水线保存检查点并进入“等待模型恢复”状态

## 模型与程序的职责边界

模型负责动态主题发现、跨批次归并、需求草拟和测试用例草拟。Python 程序负责评论采集/导入、去重清洗、计数、评论 ID 校验、置信度计算、需求优先级计算、Schema 校验、证据链校验和检查点持久化。

因此，模型输出不是直接交付物。只有经过程序校验、保留真实 `review_id`，并能形成 `Review → Finding → Requirement → TestCase` 链路的内容才进入正式结果视图。

## 当前 Prompt 约束

- 评论正文始终被声明为待分析数据，其中的指令性文字不能改变任务。
- 模型不得编造评论 ID、数量、比例、版本信息或用户动机。
- 主题动态发现，不依赖预设关键词分类表。
- 每条评论的中文摘要不超过 25 字（控制输出 token 预算，防止大批次截断）。
- 证据不足时允许少于 5 个需求，不能为了凑数量编造需求；证据充分时目标为 5–10 个核心需求。
- 测试用例必须具体、可观察、可重复，并引用现有 Requirement。

## 黄金评测集

`evals/gold-reviews.json` 包含 **30 个人工标注用例、120 条评论**，覆盖：

- 中英单语与中英混排；订阅/价格/试用扣费/取消/退款等付费链路；
- 计时器可靠性、语音指导、崩溃、同步失败、数据丢失、登录失败；
- 付费墙、广告、新手引导、搜索质量、性能、耗电、离线、通知、本地化、客服；
- **冲突证据**（同一主题正反评论并存）、**证据不足**（应拒绝下结论）、
  **重复评论**、**Prompt 注入**、**全正面**（应无可验证问题）。

### 主题键 topic_key，与它暴露出的"词表问题"

早期评测拿模型生成的**中文标签**（`topic_label`）与黄金集做精确字符串比对，所以
`topic_recall` 长期是 0.000。当时判断"不是模型没识别出主题，而是指标不可比"，于是给 Schema
加上语言无关的 `topic_key`（ascii snake_case），并要求模型为同一具体问题复用同一个键。

**但那次修复只到了 Schema 与 Prompt，没落到打分代码里**：`run_eval.py` 仍然取 `topic_label`，
中文标签被规范化成空串后丢弃，`predicted_topics` 恒为空集——指标依旧结构性恒为 0
（缺陷 **D-10**，2026-09-20 修复）。

修好之后第一次真实运行（30 用例）给出两个数字：

- `topic_key_coverage = 1.0` —— 模型每次都给出了规范的可比键，排除"压根没给键"这一解释；
- `topic_recall = 0.033` —— 仍然极低。

看逐用例对照才看得清原因：**模型找对了问题，只是粒度比标注细**。

| 黄金标注（粗粒度） | 模型给出（细粒度） |
|---|---|
| `subscription_transparency` | `pre_trial_price_visibility`、`subscription_terms_clarity`、`trial_renewal_disclosure` |
| `timer_reliability` | `timer_continues_after_pause`、`timer_freeze_on_screen_lock`、`timer_works_on_some_devices` |
| `cancellation_difficulty` | `cancellation_multi_step_flow`、`no_in_app_cancel_entry`、`charged_after_cancellation` |

  "与标注者选词的词面一致率"，而不是主题识别能力——**这是指标设计问题，不是模型问题**。

**处置（D-11，2026-09-20 结案）：改口径，而不是改模型。** 评测里现在并列几条互补的口径：

| 口径 | 回答的问题 | 依赖词表？ |
|---|---|---|
| `topic_recall`（页面标签：**主题词面一致率**） | 模型选的键与标注键逐字一致吗 | 是，所以它天生偏低 |
| `topic_found_by_evidence`（**主题命中率**） | 标注主题的支撑评论有没有被模型引用 | **否** |
| `topic_granularity`（**主题粒度比**） | 模型给的主题比标注细多少 | 否（它解释上一条为什么低） |
| `reference_recall` / `reference_precision` | 标注相关的评论覆盖了多少 / 引用里有多少在标注内 | **否** |

**2026-09-20 的真实数字把这件事说清楚了**：`topic_recall = 0.033`，而 `topic_found_by_evidence = 1.000`、
`topic_granularity = 2.15`。同一个系统、同一次运行——**按词面口径只对上了 3.3%，按证据口径每个标注主题都被找到了，
而模型给出的粒度是标注的 2.15 倍**，这正是词面对不上的原因。单看任何一个数都会被误导。

**两条看似更彻底的路线为什么没走**：给模型一套受控词表，与本项目「动态识别主题、不加预设分类表」的设计取舍直接冲突，
而且要改 prompt ⇒ 作废演示录制件（需重新付费录制）；在黄金集里手工补同义键，是拿模型这次的输出反推标注，
等于把指标拟合到一次运行上，换个同义词又会掉下去。**在开放标签空间里，「主题召回」本身就不是一个可测的量**——
能测的是「这批证据有没有被用上」，剩下的一半只能靠人看逐案对照。

### 回归门禁

- **数据集完整性**：`python scripts/run_eval.py` 校验用例 ID 唯一、期望评论存在、
  主题键可比、评分合法等，CI 每次 push 都跑（不调用模型）。
- **指标门禁**：`.github/workflows/eval.yml`（手动触发，需配置 `DEEPSEEK_API_KEY`）
  支持传入 `--fail-under-topic-recall` / `--fail-under-reference-precision`，
  指标跌破阈值时 workflow 失败。实时评测要花钱，所以不挂在每次 push 上。

评分脚本：

```powershell
# 推荐：从项目根目录直接运行，脚本会自动定位 .venv
.\run_eval.ps1

# 调用当前 DeepSeek 配置并保存详细结果
.\run_eval.ps1 -Live -Output output\prompt-eval-2026-08-15.json

# 只校验数据集，不调用模型
.\.venv\Scripts\python scripts/run_eval.py

# 调用当前 DeepSeek 配置并保存详细结果
.\.venv\Scripts\python scripts/run_eval.py --live --output output/prompt-eval-2026-08-15.json

# 测稳定性：每个用例重复 3 次（成本约 ×3，仍受 --max-cost-usd 约束）
.\.venv\Scripts\python scripts/run_eval.py --live --stability 3

# 存进评测历史（文件名带 prompt 指纹与时分），供跨版本比较
.\.venv\Scripts\python scripts/run_eval.py --live --stability 3 --save-history

# 比较两次报告；--fail-on-regression 供手动触发的门禁使用
.\.venv\Scripts\python scripts/compare_eval.py evals/history/<基线>.json evals/history/<本次>.json
```

评测报告里带 **prompt 版本 + 文本指纹**、**实际花费**（`usage.estimated_cost_usd`）与
**完成用例数**。`--max-cost-usd` 默认 1.0 美元是安全上限，触顶会停下并如实标注
`budget_exceeded`，而不是装作跑完了。

根目录 PowerShell 入口只负责定位项目虚拟环境并转发参数，不读取或打印
`DEEPSEEK_API_KEY`。缺少 `.venv` 或评测脚本时会给出中文错误提示。

## 实验记录

| 日期 | Prompt / Schema 版本 | 数据集 | Topic recall | Reference precision | Structured output success | 观察到的失败 | 下一步改动及原因 |
|---|---|---|---:|---:|---:|---|---|
| 2026-08-15 | batch-v1 / `FindingDraft`-v1 | gold-reviews（3 cases） | 0.000 | 0.556 | 1.000 | 模型输出中文主题标签，而黄金标签为英文，精确字符串匹配全部失败；订阅用例把一条正向评论列为 supporting；证据不足用例仍输出两个单条证据主题 | 为 Schema 增加稳定、语言无关的 `topic_key`，展示层继续保留本地化 `topic_label`；补充“正向评论优先放入 conflicting_review_ids”“单条证据默认标记 assumption”的 Prompt 约束，再扩充数据集后复测 |
| 2026-08-16 | batch-v1 + max_tokens / `FindingDraft`-v1 | Workout for Women 真实评论（100 条） | — | — | 1.000（修复后） | 未设置 `max_tokens` 时使用 API 默认 4096，大批次输出（每条中文摘要 + 发现）被截断，JSON 解析失败，重试后仍失败进入等待恢复 | 显式设置 `MODEL_MAX_TOKENS=8192`；批次 Prompt 增加“每条摘要不超过 25 字”约束；已在真实运行上验证续跑成功（同一 `run_id`） |

### 2026-09-20 首次完整评测（六个指标）

同一天跑了三次（同一 prompt，指纹 `9e70a08b5eef`，每次 30 用例 × 3 次重复），
累计花费 **$0.075 × 3 ≈ $0.23**。下表取第 1 次与第 3 次：

| 指标 | 第 1 次 | 第 3 次 | 含义 |
|---|---:|---:|---|
| 引用召回 `reference_recall` | — | **0.950** | 标注认为相关的评论，模型覆盖了多少（不依赖词表） |
| 引用精确率 `reference_precision` | 0.796 | 0.795 | 模型引用的评论里有多少在标注集内 |
| 主题词面一致率 `topic_recall` | 0.033 | 0.033 | 集合精确匹配；**受词表粒度影响，它不是召回率**（D-11） |
| 主题命中率 `topic_found_by_evidence` | — | **1.000** | 标注主题的支撑评论至少有一条被引用（不依赖词表） |
| 主题粒度比 `topic_granularity` | — | **2.15** | 模型主题数 ÷ 标注主题数——词面一致率偏低的成因 |
| 主题键覆盖率 `topic_key_coverage` | 1.000 | 1.000 | 模型给出的键全部规范可比 |
| 幻觉率 `hallucination_rate` | 0.000 | 0.000 | 引用了输入中**不存在**的 `review_id` 的比例 |
| 稳定性 `stability`（N=3） | 0.497 | 0.492 | 同输入三次运行的主题集合一致度 |
| 结构化输出成功率 | 1.000 | 1.000 | |

三个结论，都可复现（历史报告在 `evals/history/`，用 `scripts/compare_eval.py` 可自行复算）：

1. **指标本身是可复现的**：两次独立运行的差值都在 ±0.005 以内，主题召回甚至完全相同。
   所以后面按版本比较趋势是可信的——这一条不成立的话，其余数字都没意义。
2. **模型的证据覆盖很好，但用词不稳定**：引用召回 0.95、幻觉率 0（90 次运行一次都没编造评论 ID），
   而稳定性只有 0.49——同一批评论跑三次，主题集合只有约一半重合。测量噪声已被第 1 条排除，
   所以这是**模型输出的性质**，不是评测的抖动。
3. **`topic_recall = 0.033` 不等于"模型没找到问题"**：逐用例对照显示模型给出的键语义正确、
   粒度更细（gold 的 `subscription_transparency` ↔ 模型的 `trial_renewal_disclosure` 等），
   属于指标缺共享词表（D-11），不是模型能力问题。

## 首次结果解读（2026-08-15；数字已被后续轮次取代，保留作方法演进记录）

结构化输出成功率为 100%，说明当前 JSON Schema、低温度和自动修复重试可以稳定获得可解析对象。`reference_precision=0.556` 表明模型仍会混淆支持证据与冲突/不足证据，这正是后续程序校验不能省略的原因。

`topic_recall=0` 不能简单理解为“主题完全没识别出来”：模型实际输出了“订阅透明度”“价格信息缺失”“计时器功能”等相关中文标签，而黄金标签是英文精确字符串。这个结果暴露的是评测标签缺少稳定 canonical key，而不是应该用关键词表替代动态主题发现。当前先如实记录，不为了提高数字而修改黄金答案。

该数据集只有 3 个用例，只适合做回归信号和失败分析，不能作为模型总体质量结论。后续应增加多语言、冲突证据、重复评论、Prompt 注入文本和模型超时用例。
