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

- **离线演示模式**：无模型密钥的部署自动回放一次真实运行的录制
  （`data/recordings/demo-replay.json`），「开始分析」可点、跑完整条流水线；输入锁定为自带样例，
  界面显式标注为回放，全程不调用外部 API。无密钥部署从「只能浏览档案」变为「可点着跑」
- **模型层录制与回放**（`llm/recording.py`、`scripts/record_demo.py`）：按「Schema + 请求」指纹
  记录每次模型调用的请求与响应，回放按键取回、不发起外部调用。录制文件必须带
  `mode=recorded_live_run` / `is_live=false` 标记，标记不符即拒绝加载；请求未命中抛
  `ReplayMissError` 并停在检查点（状态 `waiting_for_model`），可用同一 `run_id` 续跑
- 新增 `DEMO_MODE` / `MODEL_RECORD_PATH` / `DEMO_REPLAY_PATH` 三项配置

### 修复

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
