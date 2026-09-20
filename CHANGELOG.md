# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/) 与 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 约定。

## [Unreleased]

### 新增

- **崩溃一致性基座**（`tests/reliability/`）：用真实子进程硬杀验证"进程被杀之后能否用同一
  `run_id` 接着跑完"。覆盖 10 个确定性崩溃点（9 个阶段边界 + 1 个批次边界）与 5 个随机时刻
  硬杀，断言收敛性（与一次跑完**逐字节相同**）、原子性、负向边界与"已完成的工作不被重做"。
  全部走回放 ⇒ 零模型成本；`pytest -m reliability`，默认不随主套件运行
- **运行租约**（`storage/lease.py`）：运行记录带持有者身份（`host:pid:token`）与心跳。
  同主机且持有者进程已不存在时**立即**可以接管；判不出来（跨主机、旧记录）才退回心跳窗口
- **修 D-19（混合检索只是重排器）**：`CorpusRetriever._rank` 此前只对 FTS 已召回的候选做融合，向量那一路找到的评论进不来，
  那样的「混合」结构上不可能提升召回。改为**两路召回**（向量候选并入，先过平台过滤）；两项测试钉住。
  向量那一路的**质量仍未测**（本机无 provider），评测脚本 `--hybrid` 会如实记进 skipped
- **检索质量评测**（T6）：`evals/gold-retrieval.json`（21 条查询，答案锚定既有黄金集）+ `scripts/run_retrieval_eval.py`
  （recall@k / MRR / precision@k，口径写进 docstring 与单测）+ `docs/retrieval-eval.md`；离线配置进 CI 并带阈值门禁。
  **首跑撞出 D-17**：没有空白或标点的中文长问句会被构造成一条必须逐字出现的 FTS 短语，严格 AND 与宽松 OR 又是同一个串，
  于是换个说法的中文问句两轮都落空、界面回答「没有找到相关评论」。修法是让宽松回退对 CJK 长段展开为相邻二元组——
  中文 recall@3 0.167 → 0.722、整体 0.357 → 0.595、MRR 0.667 → 0.873，英文 0.500 未变，precision@5 略降 0.333 → 0.319（代价已量化）。
  向量/混合那一路因本机无 embedding provider 标注为**未测**
- **运行队列与服务化**（T1）：编排器拆出「提交」与「执行」两半——`enqueue` 只落一条排队记录，
  worker 侧的常驻执行者原子认领并跑完（`RUN_QUEUE_ENABLED`）。**运行不再依赖浏览器标签页**：
  界面在 `EXECUTION_MODE=queued` 下提交即撒手，并如实显示有没有执行者在消费队列（执行者心跳）；
  取消是协作式的（下一个阶段边界生效，已完成的阶段全部保留、可续跑）；
  认领了却起不来的运行如实判失败，不退回队列变成毒丸
- **HTTP 契约层**（可选 extra `[api]`：fastapi + uvicorn）：`POST /v1/runs` 提交即返回 202；
  `GET /v1/runs/{id}`、`/events`（含 SSE）、`/result`、`/v1/queue`、`/healthz`、`/metrics`；
  写端点需 `API_TOKEN`（未配置时 503——提交要花钱），队列积压超限 429，默认只监听 127.0.0.1。
  契约由路由表快照测试守着；主 CI job 不装 extra（降级路径持续被验），新增一个 job 装上真跑契约测试
- **队列可观测**：`ari_queue_depth` / `ari_queue_oldest_wait_seconds` / `ari_queue_executor_seen`
  三条指标与两条告警（积压过久、有排队却没人消费）
- **可观测性**：阶段耗时落盘（`stage_timings` 表，跟着运行一起被保留策略清理）并在
  `/metrics` 暴露 P50/P95（以秒为基本单位）；web 进程也有健康/指标端点（`WEB_HEALTH_HOST` /
  `WEB_HEALTH_PORT`，但 Streamlit 按会话执行脚本 ⇒ 不作为抓取目标，详见 `ops/prometheus.yml`）；
  可选三层追踪（`[observability]` extra：run → stage → model call，**真嵌套**，
  未装或未配 `OTEL_EXPORTER_OTLP_ENDPOINT` 时完全 no-op）
- **运维资产即代码**：`ops/`（Prometheus 抓取配置、告警规则、Grafana 看板与 provisioning）+
  `docker compose --profile observability`。告警规则引用的每个指标都由测试核对**真实存在**——
  引用不存在的指标等于一条永不触发的假告警
- **修掉三个不真跑就发现不了的部署缺陷**（D-12 / D-13 / D-14，全部有回归测试）：
  容器健康检查的多行 `python -c` 被 YAML 折叠成行首带空格的单行 ⇒ `IndentationError`，web 恒 unhealthy、
  worker 因 `depends_on` 永远起不来；worker 的调度开关只靠不入库的 `.env` ⇒ 新克隆必然得到无限重启的 worker；
  web 的 `/metrics` 挂在按会话执行的 Streamlit 脚本里 ⇒ 抓取目标永远 down、`AriProcessDown` 常态误报
- **备份恢复演练**（`scripts/backup_restore_drill.py`）：用 SQLite 在线备份 API（而非复制文件，
  WAL 下复制可能拿到不一致快照），**从备份**恢复后逐表核对行数与内容摘要
- **模型响应缓存**（`llm/cache.py`）：内容寻址，键含模型/温度/输出上限/Schema/请求文本——
  任何一项变了都不会命中。`MODEL_CACHE_ENABLED` 一键开关、`MODEL_CACHE_TTL_DAYS` 控制有效期，
  数据维护顺带清理过期条目
- **Prompt 版本化**：运行记录带上 prompt 版本标签与**文本指纹**（只进记录、不进请求文本，
  因此不影响录制件）。指纹由测试钉死——改了 prompt 文本却忘改版本号会红
- **评测体系**：新增 `reference_recall`（不依赖词表的证据覆盖口径）、`hallucination_rate`
  （引用了不存在评论的比例）、`--stability N`（同输入 N 次的主题集合一致度）、`--save-history`
  与 `scripts/compare_eval.py`（跨版本比较 + 手动门禁）。`--live` 评测现在会记账
  （`usage.estimated_cost_usd`）并受 `--max-cost-usd` 上限保护

- **离线演示模式**：无模型密钥的部署自动回放一次真实运行的录制
  （`data/recordings/demo-replay.json`），「开始分析」可点、跑完整条流水线；输入锁定为自带样例，
  界面显式标注为回放，全程不调用外部 API。无密钥部署从「只能浏览档案」变为「可点着跑」
- **模型层录制与回放**（`llm/recording.py`、`scripts/record_demo.py`）：按「Schema + 请求」指纹
  记录每次模型调用的请求与响应，回放按键取回、不发起外部调用。录制文件必须带
  `mode=recorded_live_run` / `is_live=false` 标记，标记不符即拒绝加载；请求未命中抛
  `ReplayMissError` 并停在检查点（状态 `waiting_for_model`），可用同一 `run_id` 续跑
- 新增 `DEMO_MODE` / `MODEL_RECORD_PATH` / `DEMO_REPLAY_PATH` 三项配置

### 修复

- **评测的头条指标结构性恒为 0**：打分取的是中文 `topic_label`，与黄金集里的 ascii 主题键
  不可比，规范化后成空串被丢弃 ⇒ `predicted_topics` 恒为空集。文档宣称的修复只到了 Schema
  与 Prompt，没落到打分代码里（D-10）
- **进程被硬杀后无法续跑**：`orchestrator.resume()` 只接受 `waiting_for_model` / `timed_out`，
  而崩溃留下的运行停在 `running`，于是续跑静默变成空操作、界面也没有续跑入口——检查点完好
  却永远取不回来。现在执行中的运行在确认持有者进程已消失后可以接管
- **采集完成前中断的运行无法续跑**：界面调用 `resume()` 时从不传 `imported_reviews`，
  而该参数在"采集阶段尚未完成"时是必需的，续跑只会以 `CollectionError` 收场
- **同一 App 并发互斥存在 check-then-act 竞态**：`find_active_run()` 与 `save_run()` 分两步
  执行，中间的空档里另一个进程可以插进来，同一个 App 被分析两遍。改为 `BEGIN IMMEDIATE`
  事务内原子占用；孤儿判定也从 `updated_at` 陈旧度（60 分钟魔数）改为租约语义
- **分享链接带出填写者内容**：演示模式不再写入任何 URL 查询参数，并清掉链接带进来的参数；
  普通模式下空值不入 URL，被清空的字段会从 URL 中删除，不再刷新后复活

## [0.1.0] - 2026-08-23

### 新增

- **确定性证据链流水线**：采集 → 清洗 → 分批分析 → 跨批归并 → 证据审计 → 确定性校验 → PRD 需求 → 测试用例 → 追溯校验，全链路可追溯
- **多 Agent 编排层**：Planner 规划、工具注册表、Reviewer 复核循环、人工审批门控，失败逐级降级
- **RAG 问答**：FTS5 BM25 + 可选向量混合检索、查询改写、引用存在性与原文子串双重校验、跨 App 对比
- **定时监控**：APScheduler cron 任务 + 飞书/钉钉/企业微信/Slack 四渠道 Webhook + 变化摘要报告
- **多源采集**：App Store（任意区）、Google Play（尽力而为）、Reddit/X 社交舆情
- **Streamlit 工作台**：主页面 + 产品情报问答 / 监控任务 / 评测中心 / 语料库管理四个子页面
- **SQLite 检查点续跑**：模型失败后可用同一 `run_id` 从中断处继续，不丢失已完成工作
- **部署就绪**：Docker + docker-compose（web + worker + 健康检查）+ PowerShell 一键脚本
- **293 项 pytest 测试**：全部离线运行，不调用真实模型

### 说明

- 离线演示档案为真实流水线产物，显式标记为 `mode=historical_cache_demo` / `is_live=false`，不伪装成实时结果
- 密钥仅在运行时读入 provider，不写日志、不导出、错误信息自动脱敏
