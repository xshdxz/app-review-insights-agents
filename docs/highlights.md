# 技术亮点

1. 多 Agent 编排：Planner → 工具调用 → Reviewer 复核循环，失败逐级降级（默认计划 / 纯确定性），离线演示永不失效
2. 确定性证据链与 Agent 层的混合架构：模型负责判断、代码负责验证，证据可复现可测试
3. RAG：FTS5 BM25 + 可选向量混合检索（min-max 归一化融合），单 App/跨 App 对比，引用存在性与原文片段双重确定性校验
4. 定时监控：APScheduler cron 任务 + 飞书/钉钉/企业微信/Slack 四渠道 Webhook + 变化摘要报告
5. 多源采集：App Store 任意区 / Google Play（尽力而为）/ Reddit 舆情，社交语料与评论语料隔离
6. 部署就绪：Docker + docker-compose（web + worker + 健康检查）+ PowerShell 一键脚本（setup/deploy）
7. 质量：全量 269 项 pytest + ruff 全绿 + 覆盖率 91% + 密钥扫描
8. 工程规范：TDD 全流程、SQLite 检查点续跑、JSON payload 持久化、CJK FTS5 检索定制（短语匹配 + 词边界保留）
