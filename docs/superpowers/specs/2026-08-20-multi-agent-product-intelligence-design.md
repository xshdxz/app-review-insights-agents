# 设计：多 Agent 产品情报系统（App Review Insights 能力升级）

- 日期：2026-08-20
- 状态：已获用户批准（Part 1/2/3 全部确认）
- 基线：现有 App Review Insights（确定性证据链流水线 + SQLite 检查点续跑）

## 1. 目标与背景

在现有 App Review Insights 基础上，将其升级为**多 Agent 产品情报系统**：保留证据链核心（评论 → 发现 → 需求 → 用例，全程确定性校验与 SQLite 检查点），在外部增加 Agent 编排层、RAG 问答、定时监控与群机器人报告推送，并做到部署就绪（Docker + 一键脚本，本机运行，可随时上线）。

## 2. 关键决策

| 决策点 | 选择 |
|---|---|
| 方向 | 多 Agent 产品情报系统（保留证据链核心 + Agent 编排 + RAG + 定时监控 + Webhook 推送） |
| 架构 | **方案 A**：原生 Agent 编排层（Planner + Reviewer + 工具注册表），另设 LangGraph 对比实验轨道（弹性目标） |
| 部署 | 部署就绪 + 本机运行（Docker + 一键脚本；无服务器，暂不公网部署） |
| 推送渠道 | 群机器人 Webhook：飞书 / 钉钉 / 企业微信 / Slack 四种适配器 |
| 数据源 | App Store 美区（现有）+ App Store 其他区 + Google Play + 社交舆情（X/Reddit，弹性目标） |
| RAG 形态 | 单 App 深聊 + 跨 App 对比，两种都要 |
| 模型策略 | DeepSeek 为主，OpenAI 兼容 provider 可切换（base_url + key + model 配置） |
| 产出 | 公开仓库 + 架构图/技术亮点文档；中文为主、英文为辅 |

## 3. 总体架构

```
┌──────────────────────────────────────────────────────────────┐
│ Streamlit Web UI（现有工作台 + 新增：监控任务 / RAG 问答 / 报告）│
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│ 新增 Agent 层（原生 Python，无重框架）                         │
│  Planner Agent ──工具调用──▶ 现有流水线模块变成「工具」          │
│  Reviewer Agent（证据复核 + 可选人工审批）                     │
│  Monitor Scheduler（APScheduler 定时监控任务）                 │
│  RAG Service（FTS5 + 可选向量，单 App / 跨 App 对比）           │
│  Webhook Reporter（飞书 / 钉钉 / 企业微信 / Slack 适配器）      │
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│ 现有核心（保持不变，作为工具被调用）                            │
│  采集 → 清洗 → 分批 → LLM 分析 → 证据校验 → PRD/用例 → 追溯     │
│  → SQLite 检查点（续跑能力保留）                               │
└──────────────────────────────────────────────────────────────┘
```

**核心原则**：Agent 层只负责规划、调度、复核、推送、问答；所有证据生成仍在确定性核心中完成（延续"模型负责判断、代码负责验证"哲学）。现有测试文件必须保持绿色。

## 4. Agent 编排层（新增 `src/app_review_insights/agent/`）

| 模块 | 职责 |
|---|---|
| `planner.py` | Planner Agent：接收目标（如"监控这个 App 的订阅转化口碑"），输出结构化工具调用计划（Pydantic 校验）。模型失败或计划校验失败时**回退到默认标准计划**（现有流水线顺序），保证离线演示永不失效 |
| `tools.py` | 工具注册表：`collect_reviews` / `run_analysis` / `query_corpus` / `get_latest_report` / `send_report` 等；每个工具带 JSON Schema、入参校验、结果序列化 |
| `reviewer.py` | Reviewer Agent：复用确定性 `validate.py` 做证据链复核；LLM 抽查摘要与结论一致性；违规则生成反馈回传 Planner 重做（**最多 2 轮**，防死循环） |
| `orchestrator.py` | 执行循环：规划 → 执行 → 复核 →（重试循环）→ 定稿；全程事件写入 SQLite（延续现有事件记录哲学） |
| `human_in_loop.py` | 可选人工审批闸门：任务配置 `require_approval=true` 时报告停在"待审批"，UI 中一键批准/驳回后再推送 |

**降级设计**：Planner 模型失败 → 用默认计划继续；Reviewer 模型失败 → 只做确定性复核。任何环节不因 Agent 层故障阻塞核心分析。

## 5. 数据源扩展（新增 `collectors/` 适配器，统一接口）

统一采集接口（现有 `app_store.py` 已符合，新增适配器遵循同一协议）：

```python
class ReviewCollector(Protocol):
    name: str

    def collect(self, source_url: str, limit: int, **kwargs) -> CollectResult: ...
```

| 适配器 | 实现要点 | 风险 |
|---|---|---|
| `app_store.py`（现有） | 保持 Apple RSS；**新增区域参数**（任意 storefront 代码，默认仍美区） | 低 |
| `google_play.py`（新增） | Google Play 无公开 RSS：应用详情页 + 评论页解析（httpx + 结构化提取）；**明确标记为尽力而为**，失败时提示走导入 | 中（上游页面可能变化） |
| `social/x_reddit.py`（新增，弹性目标） | 对产品名做 X 搜索 / Reddit 搜索（公开端点）；结果进"舆情语料"，与评论语料**分开存储、分开标记**（`source_type: review|social`），分析时可开关 | 高（限流/噪声/不稳定） |

**统一语料模型**：所有来源归一化为同一条评论结构（新增 `source` / `region` / `platform` 字段），清洗、去重、分批、分析全部复用。社交内容默认不参与 RAG 检索与证据链（避免噪声污染），仅显式开启时参与。

## 6. RAG 问答（新增 `src/app_review_insights/rag/`）

| 组件 | 设计 |
|---|---|
| 索引 | SQLite 内建 **FTS5**（零新依赖、确定性、可测试），每条评论一行：内容/语言/平台/区域/App/时间；**可选** OpenAI 兼容 embeddings 做向量召回，未配置时自动退化为纯 FTS5 |
| 检索 | 混合检索：FTS5 BM25 +（可选）向量 top-k → 合并去重 → 重排；单 App 检索限定 `app_id`，跨 App 检索按竞品组并行查各自语料 |
| 生成 | 回答必须**带证据引用**（评论 ID + 原文片段），复用"引用必须存在"的确定性校验；置信度低时明确说"证据不足" |
| 界面 | Streamlit 聊天界面：选一个 App 深聊 / 勾选多个 App 对比；引用可点击跳转到原评论详情 |

## 7. 调度与推送（新增 `src/app_review_insights/monitor/`）

| 组件 | 设计 |
|---|---|
| 调度器 | **APScheduler**（进程内）：任务表存 SQLite（`monitor_jobs`），支持 cron 表达式；web UI 增删改查任务；本机部署=应用启动时自动加载，Docker 部署=独立 worker 容器 |
| 执行 | 定时触发 → Agent 编排层（Planner → 分析 → Reviewer）→ 对比上次报告生成"新增/变化"摘要 → 推送 |
| Webhook | 适配器接口：飞书 / 钉钉 / 企业微信 / Slack（同一基类 + 四种消息模板）；**未配置 Webhook 时自动降级为仅存报告**（不阻塞任务） |
| 报告 | 结构化报告（发现/需求/用例摘要 + 变化点 + 数据可信度说明），存 SQLite + 导出 Markdown/JSON |

## 8. 部署（部署就绪 + 本机运行）

| 组件 | 设计 |
|---|---|
| Docker | `Dockerfile`（Python 3.11 slim）；`docker-compose.yml`：`web`（Streamlit + 调度器）+ 可选 `worker`（独立跑定时任务）；SQLite 挂 volume |
| 一键脚本 | `scripts/setup.ps1`（建 venv → 装依赖 → 生成 .env 模板 → 校验 key）；`scripts/deploy.ps1`（构建镜像 → 起容器）；幂等 |
| 配置 | `.env` 扩展：`WEBHOOK_TYPE/WEBHOOK_URL`（可多个）、`SCHEDULER_ENABLED`、`EMBEDDING_*`、`APPROVAL_REQUIRED` 等，全部有默认值 + 注释；密钥不落库、不进 git |
| 健康检查 | `GET /healthz`（版本 + 数据库可达性） |

## 9. 测试与质量

- **单元测试**：Agent 层（planner 计划生成/回退、工具 Schema、reviewer 重试上限）、RAG（FTS5 索引/混合检索/引用校验）、调度（cron 解析/任务 CRUD）、Webhook 适配器（四种格式快照测试）——全部 mock 模型，不耗 API
- **集成测试**：Agent 编排全链路用假模型跑通 规划→分析→复核→定稿；RAG 问答端到端
- **回归**：现有全部测试保持绿色；最终 `pytest` 全量 + `ruff check` + 覆盖率报告
- **真实运行验证**：扩展 `scripts/run_real_validation.py` 增加"监控任务真实执行 + Webhook 测试模式"（不发真实消息，只校验 payload 格式）

## 10. 文档

- `README.md` / `README.zh-CN.md` 重写：新增架构图（Mermaid）、Agent 层设计说明、快速上手（本机 + Docker 两条路）
- `docs/agent-architecture.md`：Planner/Reviewer/工具注册表设计 + 降级策略 + 与确定性核心的边界
- `docs/experiments/`：LangGraph 对比实验（弹性目标；同一子流程对比 自研 vs LangGraph 的质量/成本/延迟，中文结论）
- `docs/architecture.md` 更新 + 架构图
- 公开仓库：README 说明本升级与既有证据链核心的关系

## 11. 一周分阶段计划（先闭环、后扩展）

| 阶段 | 内容 | 产出 |
|---|---|---|
| D1–D2 | Agent 编排层 + 工具注册表 + Reviewer + 人工审批（现有流水线做工具） | 命令行跑通"目标→报告"全链路，测试全绿 |
| D3 | RAG 服务（FTS5 + 可选向量 + 引用校验）+ Streamlit 问答界面 | 可对话演示 |
| D4 | 调度器 + 监控任务 CRUD + Webhook 推送（四种适配器）+ 报告生成 | 定时监控→群推送演示 |
| D5 | 数据源扩展：App Store 多区 + Google Play + 社交舆情（尽力而为） | 多源采集演示 |
| D6 | Docker + 一键脚本 + 健康检查 + 配置整理 | 部署就绪 |
| D7 | 测试补齐、文档重写、架构图、LangGraph 对比实验（若有余力）、推送 | 文档齐备 |

**弹性目标**：社交舆情（X/Reddit）与 LangGraph 对比实验，若时间不够先保证核心闭环，明确标注"后续迭代"。

## 12. 范围外（YAGNI）

- 不做公网部署（部署就绪已覆盖"可上线"）
- 不做多用户账号/鉴权系统（单用户本地/自部署工具）
- 不做评论情感分析实时流（定时批处理已满足监控需求）
- 不引入向量数据库（SQLite FTS5 + 可选 embedding 已够，避免运维负担）
