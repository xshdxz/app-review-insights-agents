# App Review Insights

把美国区 App Store 评论整理为有证据支撑的产品发现、分版本 PRD 需求与可追溯测试用例的中文本地单页分析工作台。

本仓库是确定性证据链流水线，刻意让每一条结论都可核验：**Review → Finding → Requirement → TestCase** 是进入正式结果视图的唯一路径；每个阶段都会持久化为检查点，模型调用失败不会丢失已完成的工作。

> English version: [README.md](README.md)（中英文描述同一功能范围）。

---

## 功能

1. **输入**
   - **在线采集**：通过 Apple 公开 RSS 接口采集美国区 App Store 评论（实际最多约 500 条，按最新排序），只接受美国区链接。
   - **JSON/CSV 导入**：导入自有评论文件（格式见 `docs/data-format.md`）。
   - **离线演示档案**：随仓库附带的、明确标记为"历史缓存"的结果（绝不被伪装成实时结果），没有 API Key 也能演示界面。

2. **确定性处理**（纯 Python，不经过模型）
   - 文本规范化、语言检测、精确去重 + 近似去重、评论 ID 冲突处理。
   - 按评论数量与字符数分批。
   - 证据校验：模型引用的评论 ID 必须真实存在；支持数/冲突数、置信度、局限说明全部由程序确定性重算；无支持证据的发现被拒绝，证据不足的发现标记为 **Assumption（假设）**。
   - 优先级评分与全链路追溯校验。

3. **结构化模型分析**（DeepSeek `deepseek-chat`）
   - 逐批动态主题发现，并为每条评论生成中文摘要。
   - 跨批次归并、逐条引用证据审计、需求草拟（证据充分时 5–10 个，不足时如实减少）、测试用例草拟（每个需求 2–4 条）。
   - JSON 输出经 Pydantic Schema 校验；有限重试；最终失败时运行停在检查点。

4. **检查点续跑**
   - 每个阶段与批次输出都持久化到 SQLite（默认 `data/runs/runs.sqlite3`）。
   - 暂停的运行可用**同一 `run_id`** 续跑；已完成的批次绝不重复执行，即使批次大小配置发生变化。

5. **交付物下载**：清洗评论（JSON）、PRD（JSON）、测试用例（CSV）、完整证据链矩阵（CSV），全部 UTF-8 且带稳定 ID。

## 快速开始（Windows PowerShell）

```powershell
git clone https://github.com/xshdxz/app-review-insights-agents.git
cd app-review-insights
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\streamlit run app.py
```

打开 http://localhost:8501。没有 DeepSeek Key 时仍可查看离线演示档案与样例数据；"开始分析"按钮保持禁用并说明原因。

## 配置（`.env`）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | *(空)* | DeepSeek API Key。绝不提交真实 Key；`.env` 已被 Git 忽略。 |
| `MODEL_ENABLED` | `true` | 设为 `false` 可强制进入演示模式（即使配置了 Key）。 |
| `MODEL_NAME` | `deepseek-chat` | 模型名。 |
| `MODEL_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容端点。 |
| `MODEL_TIMEOUT_SECONDS` | `60` | 单次请求超时。 |
| `MODEL_MAX_RETRIES` | `2` | 有限重试预算（应用层控制；SDK 内层重试已关闭）。 |
| `MODEL_MAX_TOKENS` | `8192` | 最大输出 token。必须足够容纳每条评论的摘要；API 默认 4096 会截断大批次输出。 |
| `DATABASE_PATH` | `data/runs/runs.sqlite3` | 检查点数据库。 |
| `DEFAULT_REVIEW_LIMIT` | `500` | 默认评论数量；两种模式统一支持 100–1000（在线采集以 Apple 接口实际返回为准，不足部分在运行局限中披露）。 |
| `BATCH_REVIEW_LIMIT` / `BATCH_MAX_CHARACTERS` | `100` / `60000` | 每次模型调用的分批上限。 |

密钥处理：Key 只在运行时读入 provider；从不写入日志、导出文件或下载内容；错误信息会脱敏 Key 样式字符串、评论原文和 `.env` 引用。

## 规则与模型的职责边界（为什么不用多智能体）

流水线是**同步状态机编排器**加职责单一的 Python 模块，而不是多智能体系统，原因：

- 任务是一条固定的、带确定性验收检查的串行流水线。智能体只会增加编排开销与非确定性，不增加能力。
- 证据完整性依赖*程序化*校验（ID 存在性、计数、置信度、追溯），必须可复现、可测试——这是确定性代码，不是模型判断。
- 每个阶段都在下一阶段开始前持久化，模型调用失败时精确停在失败位置，续跑不重复已完成工作。状态机里的检查点语义最容易被推理。

模型负责：主题发现、中文摘要、归并、证据审计理由、需求与测试用例草拟。
程序负责：采集/导入、清洗、计数、ID 校验、置信度、优先级评分、Schema 校验、追溯校验、检查点、导出。

## 数据来源与限制

- Apple RSS：`https://itunes.apple.com/us/rss/customerreviews/page={n}/id={appId}/sortby=mostrecent/json` —— 每页最多 50 条、最多 10 页，**上限 500 条**。
- 在线采集只接受美国区链接；其他地区请使用 JSON/CSV 导入。
- Apple 会不定期调整该公开接口。采集器先请求当前主 URL，为空时自动回退旧式第一页 URL；两者都为空时给出明确提示并引导改用 JSON/CSV 导入或稍后重试。接口调整期间部分 App 可能返回较少甚至 0 条评论，短缺量会在运行局限说明中如实披露。
- 评论源实际返回少于请求数量时，会在运行局限说明中如实披露。
- 如果本机配置了未运行的 HTTP 代理（`HTTP_PROXY`/`HTTPS_PROXY`），采集与模型调用会报连接错误——请启动代理，或临时移除这两个环境变量。

## 测试与评测

```powershell
# 完整测试套件
.\.venv\Scripts\python -m pytest

# 覆盖率报告
.\.venv\Scripts\python -m pytest --cov=app_review_insights --cov-report=term-missing

# 静态检查与格式
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m ruff format --check .

# Prompt 评测（试运行：只校验数据集）
.\run_eval.ps1
# 调用真实 DeepSeek 评测
.\run_eval.ps1 -Live -Output output\prompt-eval.json

# 真实端到端验证（在线采集或文件导入，调用真实模型）
.\.venv\Scripts\python scripts/run_real_validation.py `
    --app-url "https://apps.apple.com/us/app/workout-for-women-home-gym/id839285684" `
    --goal "重点分析订阅转化" --limit 200 --out output/real-run.json
.\.venv\Scripts\python scripts/run_real_validation.py `
    --file data/samples/reviews-sample.json --goal "识别易用性问题" --out output/import-run.json
# 用同一 run_id 续跑暂停的运行
.\.venv\Scripts\python scripts/run_real_validation.py `
    --resume <RUN_ID> --goal "同原目标" --out output/resumed-run.json
```

真实运行脚本会逐项校验证据链（引用 ID 存在、计数一致、置信度在 (0,1]、继承规则、追溯有效），任何一项违反都以非零退出码结束。

## 仓库卫生

- `.env`、`data/runs/`、`output/`、`tmp/`、`.planning/`、`.superpowers/` 均被 Git 忽略。
- 提交前运行 `git status --short --ignored` 与密钥扫描：

```powershell
git grep -n -I -E "(sk-[A-Za-z0-9_-]{12,}|DEEPSEEK_API_KEY=.+)" -- . ':!.env.example'
```

## 架构与文档

- `docs/architecture.md` — 流水线阶段与持久化。
- `docs/data-format.md` — JSON/CSV 导入格式。
- `docs/model-and-prompts.md` — 模型/Prompt 设计与评测记录。
- `docs/defect-list.md` — 按严重程度记录的已知问题（开发过程中持续更新）。
