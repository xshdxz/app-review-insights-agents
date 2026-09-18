# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/) 与 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 约定。

## [Unreleased]

### 新增

- **离线演示模式**：无模型密钥的部署自动回放一次真实运行的录制
  （`data/recordings/demo-replay.json`），「开始分析」可点、跑完整条流水线；输入锁定为自带样例，
  界面显式标注为回放，全程不调用外部 API。无密钥部署从「只能浏览档案」变为「可点着跑」
- **模型层录制与回放**（`llm/recording.py`、`scripts/record_demo.py`）：按「Schema + 请求」指纹
  记录每次模型调用的请求与响应，回放按键取回、不发起外部调用。录制文件必须带
  `mode=recorded_live_run` / `is_live=false` 标记，标记不符即拒绝加载；请求未命中抛
  `ReplayMissError` 并停在检查点（状态 `waiting_for_model`），可用同一 `run_id` 续跑
- 新增 `DEMO_MODE` / `MODEL_RECORD_PATH` / `DEMO_REPLAY_PATH` 三项配置

### 修复

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
