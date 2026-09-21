# 文档导航

> 这一页回答一个问题：**我要找的东西在哪一篇里。**
> 项目门面是根目录的 [`README.md`](../README.md)（功能全览、快速开始、完整配置表）；
> 这里是深入阅读的入口：15 篇说明文档 + 1 篇实验记录 + 8 篇过程档案。

## 想跑起来 / 想看懂整体

| 文档 | 什么时候看 |
|---|---|
| [`../README.md`](../README.md) | 第一次接触：它解决什么问题、30 秒怎么跑、有哪些配置项 |
| [`architecture.md`](architecture.md) | 流水线有哪几个阶段、模块怎么分工、数据怎么流、为什么输出不可变 |
| [`agent-architecture.md`](agent-architecture.md) | Planner / Reviewer / 工具注册表的设计，以及**任何一环失败时的降级矩阵** |
| [`data-format.md`](data-format.md) | 要导入自己的数据：JSON / CSV 的必需字段、别名与示例 |
| [`deploy-streamlit-cloud.md`](deploy-streamlit-cloud.md) | 想部署在线 demo：为什么必须有 `requirements.txt`、无密钥为什么也能用、**实例默认可能只有你能打开** |

## 质量与评测（"凭什么信它的输出"）

| 文档 | 什么时候看 |
|---|---|
| [`defect-list.md`](defect-list.md) | 想看真实的翻车与修法：**19 条缺陷**（16 修复 / 2 保留观察 / 1 环境），每条都有证据与根因 |
| [`retrieval-eval.md`](retrieval-eval.md) | 想评估检索质量：21 条查询的评测集、recall@k / MRR / precision@k 口径、修复前后逐条对照、混合检索实测，以及**如实列出的未测部分** |
| [`model-and-prompts.md`](model-and-prompts.md) | 模型与 Prompt 的职责边界、评测记录（含一次输出被截断的完整复盘） |
| [`acceptance-matrix.md`](acceptance-matrix.md) | 14 项验收标准与"可复现证据"的对应关系 |
| [`test-record.md`](test-record.md) | 全链路测试记录（**当时的历史值**，当前数字以根 README 为准） |
| [`highlights.md`](highlights.md) | 一页速览 12 条技术亮点 |

## 运维与可靠性（"线上会不会安静地坏"）

| 文档 | 什么时候看 |
|---|---|
| [`slo.md`](slo.md) | 9 条 SLI 各自**怎么算**（PromQL / SQL）、SLO 目标与错误预算分档 |
| [`runbook.md`](runbook.md) | 出问题照做：11 个场景的「症状 → 先看什么 → 处置 → 怎么确认好了」 |
| [`reliability.md`](reliability.md) | 崩溃一致性：真子进程硬杀的矩阵、实测结果、反向验收与已知边界 |
| [`backup-drill.md`](backup-drill.md) | 备份恢复**真演练过**的记录，以及手工恢复步骤 |
| [`manual-testing.md`](manual-testing.md) | 不改代码手动验证两条关键路径：模型失败续跑、离线/演示模式 |

## 过程档案（"当时是怎么做出来的"）

| 文档 | 内容 |
|---|---|
| [`superpowers/specs/`](superpowers/specs/) | 4 份设计文档（多 Agent 升级、真实数据加固、演示与录制回放等） |
| [`superpowers/plans/`](superpowers/plans/) | 4 份实施计划：每个 Task 都是「Files / 失败测试 / 确认失败 / 实现 / 确认通过」 |
| [`experiments/langgraph-vs-native.md`](experiments/langgraph-vs-native.md) | LangGraph 状态图 vs 原生确定性校验，在归并子流程上的对比实验 |

## 素材与数据（不在 docs/ 下）

| 位置 | 内容 |
|---|---|
| [`images/`](images/) | 6 张截图（工作台、带冲突的发现、PRD 需求、测试用例、断点续跑、Grafana 看板），被根 README 引用 |
| [`../data/samples/`](../data/samples/) | 20 条样例评论（JSON / CSV）：离线演示与导入示例 |
| [`../data/recordings/`](../data/recordings/) | 演示模式的回放录制件（一次真实运行，零外部调用即可跑完整条流水线） |
| [`../evals/`](../evals/) | 评测黄金集（30 例 / 120 条评论 + 21 条检索查询）与历史评测结果 |
| [`../ops/`](../ops/) | 告警规则与 Grafana 看板（**即代码**，规则引用的每个指标都由测试核对存在） |

---

**改文档后请跑一次 `ruff format .`**：`docs/*.md` 里的 Python 代码块也在格式化管辖内，本地 `ruff check` 绿而 CI 的
`ruff format --check` 可能红（2026-09-20 踩过）。同理，改完文档记得跑一遍全量测试，确认没有测试引用被改动的路径。
