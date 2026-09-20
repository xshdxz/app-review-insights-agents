# Runbook：出问题照着做

> 每一条都是"**症状 → 先看什么 → 处置 → 怎么确认好了**"。写法上刻意对齐本项目真实踩过的坑
> （见 `docs/defect-list.md` 与 `AGENTS.md`），不是通用模板。

## 0. 通用开局

```powershell
docker compose ps                       # 容器在不在、健康不健康
curl -s http://localhost:9100/readyz    # worker 就绪（常驻进程，唯一被抓取的目标）
docker compose logs --tail 50 web       # 应用日志（LOG_FORMAT=json 时是 JSON Lines）
```

日志里每条都带 `run_id` / `stage`，**按 run_id 关联出整条链路**再判断，不要从最后一行倒着猜。

---

## 1. 页面打不开 / 容器反复重启

**先看什么**：`docker compose logs web | Select-Object -Last 30`。启动期崩溃通常是配置问题。

| 线索 | 判断 | 处置 |
|---|---|---|
| `DEMO_MODE=live` 且无密钥 | 装配期直接抛 `InputDataError` | 填密钥，或把 `DEMO_MODE` 改回 `auto` |
| `data/recordings/demo-replay.json` 缺失 + `DEMO_MODE=replay` | 同上，提示会明说录制文件缺失 | 恢复文件，或改用 `auto`（退到按钮禁用的降级形态） |
| 端口占用 | `8501` / `9100` 被占 | 改端口映射 |

**验证**：`curl -s http://localhost:8501/_stcore/health` 返回 `ok`。

> 已知坑：本机若设了 `HTTP_PROXY`/`HTTPS_PROXY`（如 `127.0.0.1:7897`）而代理没在跑，
> 所有外部调用会以 WinError 10061 失败。先确认代理，或临时清掉这两个环境变量。

## 2. `/readyz` 持续 503

**先看什么**：`ari_worker_ready` 为 0 说明调度器没起来。

| 线索 | 处置 |
|---|---|
| `SCHEDULER_ENABLED=false` | worker 容器必须设为 `true`；这是刻意的：web 进程不该跑定时任务 |
| 启动日志里有调度失败的 traceback | 按 traceback 修；失败已带上下文留痕 |

**验证**：`curl -s http://localhost:9100/readyz` 返回 200 且 `"ready": true`。

## 3. 运行停在 `waiting_for_model`

这是**可恢复**状态，不是故障：模型阶段失败后检查点已保存。

**先看什么**：运行面板的 `last_error`（已脱敏）。常见三种：

| 错误 | 原因 | 处置 |
|---|---|---|
| 认证失败 / 401 | 密钥无效 | 修正 `.env` 的 `DEEPSEEK_API_KEY` 后点"继续分析" |
| `Invalid JSON: EOF while parsing` | 输出被截断 | 确认 `MODEL_MAX_TOKENS=8192`（D-01：API 默认 4096 会截断大批次） |
| 连接超时 | 网络 / 代理 | 见第 0 节的代理排查 |

**验证**：点"继续分析"后沿用**同一 `run_id`** 跑到 `completed`，已完成批次不会被重跑。

## 4. 在线采集返回 0 条

**先看什么**：这是 **D-06 的已知形态**，不是代码坏了——Apple 的公开评论 RSS 多次变更，
主 URL 可能对全部 App 返回空 feed，采集器会自动回退旧式第一页 URL。

| 情况 | 处置 |
|---|---|
| 两个 URL 都空 | 改用 JSON/CSV 导入；**不要**把采集器改成静默返回空列表 |
| 只有部分 App 为空 | 该 App 确实没有可用的公开评论源 |

**验证**：`run` 的事件流里 `Review collection completed` 的 `review_count` 大于 0。

## 5. 同一 App 开不了新分析

**症状**：提示"该 App 已有进行中的运行"。

**先看什么**：运行记录上的租约（`lease_owner` / `heartbeat_at`）。

| 情况 | 判断 | 处置 |
|---|---|---|
| 状态 `running` 且持有者进程已不存在 | 崩溃留下的孤儿 | 直接点"继续分析"接管——**不必等**心跳超时 |
| 状态 `waiting_for_model` | 停在检查点等模型 | 续跑或放弃该运行 |
| 持有者进程还活着 | 真的有一个分析在跑 | 等它结束；并发分析只是把同一份结论算两遍并双倍计费 |

> 跨主机时判不出持有者死活，只能等 `lease_timeout_seconds`（下界 300 秒）。

## 6. 进程被硬杀后想续跑

**这是设计支持的**：状态停在 `running`、持有者进程已消失 ⇒ 点"继续分析"即可。
若中断发生在**采集完成之前**（检查点里还没有评论），界面会要求重新选择同一份评论文件。

**验证**：`docs/reliability.md` 里的场景矩阵——同样的崩溃点，续跑结果与"一次跑完"逐字节相同。

## 7. 磁盘只涨不跌

**先看什么**：`data/` 下的几个 SQLite。

```powershell
..venvScriptspython -m app_review_insights.maintenance   # 清理终态运行 + 过期缓存 + VACUUM
```

| 表 | 保留策略 |
|---|---|
| `runs` / `stage_outputs` / `events` / `model_usage` / `stage_timings` | `RETENTION_DAYS`（默认 90 天），**只清终态**，可续跑的永不删 |
| `events` | 每个运行保留最近 `EVENTS_KEEP_PER_RUN` 条 |
| `reports` | 每个 App 保留最近 `REPORTS_KEEP_PER_APP` 份 |
| 响应缓存 | 超过 `MODEL_CACHE_TTL_DAYS`（默认 7 天）的条目 |

worker 默认每天自动跑一次（`MAINTENANCE_ENABLED=true`）。

## 8. 费用超出预期

**先看什么**：`model_usage` 按阶段拆解（运行面板会展示），定位开销大头。

| 情况 | 处置 |
|---|---|
| 单次运行超预算 | 调 `MODEL_BUDGET_USD_PER_RUN`；超限会停在检查点，调高后可续跑 |
| 当日累计超预算 | 调 `MODEL_BUDGET_USD_PER_DAY`。注意它按 UTC 日窗口统计 |
| 评测很贵 | 用 `--max-cost-usd` 限制；跑稳定性前先跑一次单次评测估成本 |
| 重复跑同一输入 | 确认 `MODEL_CACHE_ENABLED=true`（缓存命中的部分不再计费） |

> 预算守卫挂在**每次模型调用之前**，所以边界是"超限后不再发起新的调用"，
> 而不是"总额绝不超过"——跨过阈值时已在途的那一笔仍会完成并计入。

## 9. 演示模式"回放未命中"

**症状**：无密钥部署点"开始分析"后停在 `waiting_for_model`，错误提到 `ReplayMissError`。

**原因**：回放按键取回，键由 `schema + system + user` 决定。改了 prompt 模板、样例数据或
分析目标，键就对不上了。

| 处置 | 说明 |
|---|---|
| 恢复被改动的 prompt / 样例 / `DEMO_ANALYSIS_GOAL` | 最省事，录制件继续有效 |
| 重新录制 | `scripts/record_demo.py`，**会产生一次真实费用** |

> 输入指纹不匹配会在**跑任何阶段之前**失败，连运行记录都不会创建——所以库里不该出现
> "跑了一半的回放运行"。出现了就说明指纹校验被绕过了，那是缺陷。

## 10. CI 红了

| 作业 | 常见原因 |
|---|---|
| Lint & Format | 忘了跑 `ruff format .`。**注意 `ruff check` 通过不代表 `format --check` 通过**——徽章曾因此红了三天 |
| Test | 本地绿、CI 红时先怀疑**测试依赖了开发机的 `.env`**：把 `.env` 临时改名即可复现 |
| Reliability | 崩溃矩阵红通常意味着"续跑入口"或"检查点"真的坏了，不要先怀疑环境 |
| Validate gold dataset | 评测集被改坏了（ID 重复、期望评论不存在、主题键不可比） |

```powershell
# 与 CI 完全一致的调用方式（裸 pytest 不会把 CWD 放进 sys.path）
..venvScriptspytest
```
