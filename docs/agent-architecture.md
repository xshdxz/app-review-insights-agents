# Agent 架构说明

## 编排循环

```text
目标(goal) + App 链接
  → Planner Agent（LLM 生成工具调用计划；失败回退默认计划）
  → 工具执行（run_analysis / query_corpus / get_latest_report / send_report / collect_reviews）
  → Reviewer Agent（确定性证据复核优先，LLM 抽查目标覆盖；失败降级为纯确定性）
  → 未通过且轮数 < AGENT_MAX_REVIEW_ROUNDS：反馈并入目标重新分析
  → 通过 → 需审批则停在 WAITING_APPROVAL，否则 COMPLETED
```

## 与确定性核心的边界

- 证据生成（采集/清洗/分析/校验/PRD/用例/追溯）100% 在既有流水线内，Agent 层只通过工具调用触发。
- Agent 层引入的所有中间产物（计划、反馈、运行状态）持久化在 `data/agent/agent.sqlite3`，与流水线检查点（`data/runs/`）分离。

## 降级矩阵

| 故障 | 行为 |
|---|---|
| Planner 模型失败 / 计划无效 | 回退默认计划（仅 run_analysis） |
| Reviewer 模型失败 | 仅确定性复核（追溯校验） |
| 复核不通过达上限 | AgentRun 置 FAILED，保留 feedback 供人工查看 |
| Webhook 未配置/失败 | 报告入库不推送，UI 可手动推送 |
| RAG 模型失败 | 返回检索结果 + 明确 limitation，引用校验仍生效 |
| embedding 生成失败 | 语料仍入库（FTS5 可用），仅向量检索降级 |
