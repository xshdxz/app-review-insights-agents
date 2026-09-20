# 技术亮点

1. 多 Agent 编排：Planner → 工具调用 → Reviewer 复核循环，失败逐级降级（默认计划 / 纯确定性），离线演示永不失效
2. 确定性证据链与 Agent 层的混合架构：模型负责判断、代码负责验证，证据可复现可测试
3. RAG：FTS5 BM25 + 可选向量混合检索（min-max 归一化融合），单 App/跨 App 对比，引用存在性与原文片段双重确定性校验
4. 定时监控：APScheduler cron 任务 + 飞书/钉钉/企业微信/Slack 四渠道 Webhook + 变化摘要报告
5. 多源采集：App Store 任意区 / Google Play（尽力而为）/ Reddit 舆情，社交语料与评论语料隔离
6. 部署就绪：Docker + docker-compose（web + worker 共用一份镜像 + 健康检查）+ PowerShell 一键脚本（setup/deploy）
7. 可观测性与运维：阶段与模型耗时落盘并在 `/metrics` 暴露 P50/P95（以秒为基本单位）；可选 OTel 三层追踪（run → stage → model call，**真嵌套**，未安装即完全 no-op）；告警规则与 Grafana 看板即代码，**规则引用的每个指标都由测试拿真实渲染结果逐条核对**；SLO 文档写明每条 SLI 怎么算，Runbook 按"症状 → 判断 → 处置 → 验证"写；备份恢复演练真跑过一次并把结果写进文档
8. 崩溃一致性：在**真实子进程**里硬杀（阶段边界 + 随机时刻），再用同一 `run_id` 续跑，断言结果与"一次跑完"**逐字节相同**；运行租约（`host:pid:token` + 进程存活判定）让崩溃后的运行可被立即接管。22 项、全部走回放 ⇒ 零模型成本，见 `docs/reliability.md`
9. 工程规范：TDD 全流程、SQLite 检查点续跑、版本化 schema 迁移、结构化日志（JSON Lines + `run_id` 关联）、模型用量与费用计量、CJK FTS5 检索定制（短语匹配 + 词边界保留）
10. 质量：主套件 620 项 + 可靠性套件 22 项全绿、ruff 全绿、覆盖率 92%、密钥扫描
