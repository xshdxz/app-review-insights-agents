# 缺陷清单（2026-09-20 更新）

严重程度：`阻断` > `高` > `中` > `低`。
状态：`open` / `fixed` / `won't-fix` / `env`（环境问题，仅文档说明）。

## 高

| # | 严重程度 | 状态 | 问题 | 证据 | 修复 |
|---|---|---|---|---|---|
| D-01 | 高 | fixed | DeepSeek 输出未设置 `max_tokens`，使用默认 4096；大评论批次（如 200 条、每条需中文摘要）输出被截断，JSON 解析失败，重试后仍失败并进入 `waiting_for_model`。 | 真实运行 `2391b909` 的 `last_error`："Invalid JSON: EOF while parsing a value at line 1166 column 7"。 | 新增 `MODEL_MAX_TOKENS=8192`（deepseek-chat 上限），provider 显式传入；批次 Prompt 增加"每条摘要不超过 25 字"约束。已在真实运行上验证续跑成功。 |
| D-06 | 高 | fixed | Apple 公开评论 RSS 接口再次变化（2026-08-16 起）：主 URL（`urlDesc` 分页方案，8/17 时有效）对全部 App 返回空 feed；旧式 URL 仅部分 App 第一页可用（如 Notion 50 条），Workout/Todoist 等返回空。在线采集模式受影响。 | 实测：Notion 主 URL 空/旧式 48238 字节有数据；Workout 两种 URL 均空；Notion 旧式连续 5 次探测稳定。 | 采集器增加 fallback：主 URL 返回空时回退旧式第一页 URL，两者皆空才报"评论源返回 0 条"并引导改用 JSON/CSV 导入。已在真实运行验证 Notion 在线采集 50 条完成全流程。第三方数据可用性仍以 Apple 为准。 |

| D-07 | 高 | fixed | **进程被硬杀后无法续跑**：`orchestrator.resume()` 的状态门只放行 `waiting_for_model` / `timed_out`，而崩溃留下的运行停在 `running`，于是续跑静默变成空操作；界面按钮用同一份状态元组，也没有恢复入口。检查点完好却永远取不回来——"同一 `run_id` 续跑、不丢已完成工作"这条承诺在**进程级故障**下不成立（而它比模型失败更常见）。 | 崩溃一致性矩阵 15 个场景全红，且红在同一条断言上（`'running' != 'completed'`），前三条断言（崩溃生效 / 负向 / 原子性）全过 ⇒ 缺陷精确定位在状态门而非检查点机制。 | 引入运行租约（`storage/lease.py`）：持有者身份 `host:pid:token` + 进程存活判定，执行中的运行在确认持有者已消失后可**立即**接管；判不出来才退回心跳窗口；界面与编排器共用 `repository.can_resume`。修复后 21 项全绿，且反向验收（换回旧行为）会重新变红。 |

| D-10 | 高 | fixed | **评测的头条指标结构性恒为 0**：`scripts/run_eval.py` 的打分取的是 `finding.topic_label`，而按 prompt 的约定它是**中文**、黄金集里是 `subscription_transparency` 这类 ascii 键；规范化把非 ascii 字符整体替换成下划线，中文标签于是变成空串并被丢弃，`predicted_topics` 恒为空集。文档与 AGENTS.md 都写着"评测只比对 `topic_key`"——那次修复只改了 Schema 与 Prompt，**没落到打分代码里**。 | 单测把"中文 label + ascii key"喂进 `evaluate_case`，实测 `topic_recall = 0.0`（应 1.0）。它长期存活还有第二个原因：`tests/test_analysis.py` 的夹具用**英文 label 且没有 key**，与真实模型输出形状不一致。 | 打分改用 `topic_key`；新增 `topic_key_coverage` 让"模型没给出可比键"这一缺口可见；`_normalize_topic` 改为复用 Schema 的规范化（原先两份实现有分叉风险）。修复后首次真实运行：`topic_key_coverage = 1.0`、`topic_recall = 0.033`——后者暴露出下一个问题（D-11）。 |

| D-12 | 高 | fixed | **容器健康检查永远失败，整套 `docker compose up` 实际是坏的**：healthcheck 用 YAML 折叠标量（`>`）写多行 `python -c "..."`，折叠后会在引号内行首留下一个空格，python 抛 `IndentationError`，web 恒为 unhealthy；worker 因 `depends_on: web.service_healthy` 永远起不来。 | `docker inspect repo-web-1` 的 healthcheck 输出 `IndentationError: unexpected indent`，而同一容器的日志显示 Streamlit 早已在 8501 正常服务——**探针报的病不是应用真有的病**。 | 改成单行 `CMD-SHELL`；新增 `tests/test_ops_assets.py::test_container_healthchecks_embed_valid_python`，把健康检查里的 python 源码交给 `compile()`——旧写法会立刻 SyntaxError（已用真实 YAML 验证会红）。修复后 web 转为 healthy。 |

| D-13 | 高 | fixed | **worker 永远不会调度**：compose 里 worker 只写 `env_file: .env`，而 `.env` 是不入库的本机配置（`SCHEDULER_ENABLED=false`）。"只有 worker 设 true"这个设计意图只写在 AGENTS.md 里，代码里从未落实——新克隆必然得到无限重启的 worker；反过来本机 `.env` 若为 true，web 与 worker 会**同时**调度，定时报告发两遍。 | `docker logs repo-worker-1`：每 2 秒一条「SCHEDULER_ENABLED=false，worker 退出」，`docker compose ps` 显示 `Restarting`。 | 在 compose 里显式声明角色（worker=`true` / web=`false`，`environment` 覆盖 `env_file`）；新增 `test_exactly_one_service_schedules` 把"调度者唯一"钉成不变量。修复后 worker 转为 healthy，调度器 0 jobs 常驻。 |

## 中

| # | 严重程度 | 状态 | 问题 | 证据 | 修复 |
|---|---|---|---|---|---|
| D-02 | 中 | env | 本机 `HTTP_PROXY`/`HTTPS_PROXY` 指向 `127.0.0.1:7897`，代理进程未运行时，在线采集与 DeepSeek 调用全部失败（WinError 10061）。 | 2026-08-16 实测：带代理变量直连失败；移除代理变量后 iTunes RSS 200、DeepSeek 401（可达）。 | 环境问题，不改代码。README 增加排障说明：确保本地代理可用，或临时移除代理环境变量。 |
| D-03 | 中 | open | 在线采集实际条数可能少于请求条数（如请求 200 实际 100），样本短缺影响证据覆盖度。 | 真实运行 `2391b909`：cleaning stats input_count=100 < review_limit=200。 | UI 已有 `_run_limitations` 披露"在线采集目标 N 条，实际获得 M 条"。需在 8/17 真实演示中确认页面展示。 |

| D-08 | 中 | fixed | **采集完成前中断的运行无法续跑**：界面调用 `resume()` 时从不传 `imported_reviews`，而该参数在"采集阶段尚未完成"时是必需的——即便放开了状态门，续跑也只会以 `CollectionError` 收场。 | `ui/main.py` 的 `_resume_analysis` 签名里没有这个参数；`orchestrator.resume()` 的 docstring 明确要求它。 | 新增 `_reviews_for_resume`：COLLECT 已有输出则直接用检查点里的评论；否则按来源重新导入（缺文件时给出中文提示而不是崩在采集阶段）。 |
| D-09 | 中 | fixed | **同一 App 并发互斥存在 check-then-act 竞态**：`find_active_run()` 与 `save_run()` 分两步执行，中间的空档里另一个进程可以插进来，同一个 App 被分析两遍、模型额度烧两份。孤儿判定还依赖"60 分钟未更新"这一魔数，语义也不对——更新得早不等于没人拥有它。 | 两处调用在 `AnalysisOrchestrator.start()` 里相邻但不在同一事务内。 | 改为 `BEGIN IMMEDIATE` 事务内原子占用（`repository.acquire_run`）；互斥判定改为租约语义（`lease.blocks_new_run`），保留对无租约旧记录的"最近更新时间"回退。 |

| D-14 | 中 | fixed | **抓取目标永远 down，告警常态误报**：web 的 `/metrics` 端点挂在 Streamlit 脚本里，而 Streamlit 的脚本**按会话执行**——没人打开页面时端点根本不存在。`AriProcessDown` 因此对 `ari-web` 一直为真，「一条永远在响的告警等于没有告警」。 | Prometheus targets API：`ari-web http://web:9101/metrics down`，同时 `ari-worker http://worker:9100/metrics up`。 | 抓取目标收敛到常驻的 worker——阶段耗时本就写在共享 SQLite 里，worker 读同一份数据，不抓 web 不丢信息；测试改为断言只抓常驻进程。web 的存活交给容器健康检查（Streamlit 自带 `/_stcore/health`）。 |

| D-16 | 中 | fixed | **取消请求被静默丢弃**：第一版把"请求取消"标志写在运行记录里，而运行记录的执行者只有流水线自己——请求方写进去的标志，会被执行者的**下一次写入整体覆盖**。批次边界的一次 `_update_run` 就足以把它冲掉，于是"点了停止"看起来受理成功、实际照跑到底（还在继续烧模型额度）。 | 新增的 `test_cancel_takes_effect_at_the_next_stage_boundary` 红了：请求取消之后运行仍然是 `completed`。定位很快——同阶段内的批次还在继续，说明标志在**下一个阶段边界之前**就没了。 | 取消请求改放**独立的表**（`run_cancellations`，迁移 v3），运行记录的合法写者只剩流水线自己；执行者在每个阶段边界查一次表。**同一份状态有两个写者，就一定有丢更新**——与 D-09（check-then-act 竞态）同族，只是失效方向相反：那次是多写一遍，这次是标志没了。 |

| D-11 | 中 | open | **`topic_recall` 缺共享词表**：它按集合精确匹配 `topic_key`，而键是模型自由生成的。2026-09-20 首次真实运行显示，模型识别出的问题**语义正确但粒度更细**：gold `subscription_transparency` ↔ 模型 `pre_trial_price_visibility` / `subscription_terms_clarity` / `trial_renewal_disclosure`；25+ 个预测键每个只出现一次，而标注只有 22 个粗粒度类目。因此该指标实际测的是"与标注者选词的词面一致率"。 | 首次真实运行 `topic_recall = 0.033` 而 `topic_key_coverage = 1.0`——键本身是规范的，排除了规范化问题。 | **本次只修了一半**：新增不依赖词表的 `reference_recall`（标注认为相关的评论被覆盖了多少）。彻底修法有三条路且都未做：① 给模型一套受控词表（与"动态识别主题、不用预设分类表"的设计取舍直接冲突）；② 在黄金集里为每个主题标注同义键；③ 用语义相似度替代精确匹配。留待评估。 |

## 低

| # | 严重程度 | 状态 | 问题 | 证据 | 修复 |
|---|---|---|---|---|---|
| D-04 | 低 | fixed | SQLite 连接未显式关闭，全量测试产生 736 条 ResourceWarning。 | `python -m pytest` 输出 "138 passed, 736 warnings"（2026-08-16 基线）。 | 仓库改用 `_session` 上下文（成功提交 + 始终关闭），全量测试 140 passed 且 0 warnings。 |
| D-05 | 低 | open | 需求数量可能少于 5（如本次 4 个），与计划"5–10 个 PRD 需求"存在偏差。 | 真实运行 `2391b909`：4 requirements。 | 行为符合设计：证据不足时如实披露（`quantity_notice`），不凑数。保留观察，不修复。 |

## 观察（非缺陷）

- 真实运行中 F-001 同时包含 15 条支持与 3 条冲突评论，冲突证据在页面"冲突评论"中展示。
- 所有发现的 `support_count`/`conflict_count` 与引用列表一致，置信度区间 (0,1]，无虚构引用。
- 清洗阶段 100 条输入 0 重复：真实评论无重复文本，符合预期（夹具中重复/近似重复路径由单元测试覆盖）。
