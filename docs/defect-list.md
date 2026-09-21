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

| D-17 | 高 | fixed | **中文长问句检索必然落空**：`build_fts_query` 把没有空白或标点的中文查询变成一个 「字符间加空格的整段短语」，FTS5 里等价于要求**逐字相邻出现**；而 `search_corpus` 的「严格 AND → 无结果退化 OR」 两级用的是**同一个串**，所以换个说法的中文问句两轮都命中不了，RAG 页面回答「当前语料中没有找到与该问题相关的评论」 ——静默失败，还说了假话。（带全角标点时会被切成两段，偶有命中，所以一直没被发现。） | T6 检索评测首跑：`q-data-loss` 的查询与目标评论只差两个字（「我过去几个月」vs「过去三个月」）却召回 0； dump `build_fts_query` 的输出确认两轮都是同一条 17 字短语。评测数字：中文 recall@3 = 0.167、英文 0.500。 | **宽松回退那一级**对 CJK 长段展开为**相邻二元组**（保留「相邻」约束——`订阅` 仍不会命中订/阅分散的评论， 既有用例守着这一点），严格模式保持整段短语不变。修复后中文 recall@3 **0.167 → 0.722**、整体 recall@3 0.357 → 0.595、 MRR 0.667 → 0.873；英文 0.500 未变（无回归）；precision@5 0.333 → 0.319（宽松兜底多带回少量弱相关，代价已量化）。 **这一条是「先造尺子」的直接收益**：没有检索评测，它不会以任何方式暴露出来。 |

| D-18 | 中 | fixed | **查询改写被自己的评测证明是负收益**：README 此前写「提升召回率」，而检索评测跑出来全面下降 （recall@3 0.595 → 0.524、MRR 0.873 → 0.746、英文 0.500 → 0.417、中文 0.722 → 0.667）。 根因不在改写本身，而在**合并那一步跨查询比较分数**：`RagAnswerer` 把多条改写查询的结果按**归一化分数**排序， 而每条查询的 top1 都被 min-max 归一化成 1.0，于是改写出来的短查询（更泛）的头部结果会把原查询的精确结果挤下去。 | T6 检索评测的 `--live-rewrite` 配置：同一查询集、同一天、21 次改写调用（<$0.01）。见 `docs/retrieval-eval.md`。 | 取候选三条里最保守的 **③「只在原查询零结果时才启用改写」**，并把策略收敛到**唯一入口** `rag/answer.gather_evidence`（UI 与评测脚本共用同一函数——脚本原先另抄了一份合并逻辑，一边改了另一边没跟上，量出来的就不是上线那条路）。两条结构性保证：**命中即止**（原查询有结果就完全不碰改写 ⇒ 负收益在结构上不可能发生）；**回退路按名次轮转**（rank round-robin，不跨查询比分数——正是根因的直接修法）。复测 `rewrite_triggered = 0`，改写那一列与纯 FTS 逐位相同；**改写的上界仍未量到**（这批查询里它一次都没上场），已如实写进 `docs/retrieval-eval.md`。 |

| D-19 | 高 | fixed | **「混合检索」其实只是 FTS 的重排器**：`CorpusRetriever._rank` 只对 **FTS 已召回的候选**做融合 （`for chunk in chunks`），向量那一路找到的评论根本没有机会进入结果——这样的「混合」**结构上不可能提升召回**， 只能改动 FTS 命中的顺序，与 README「混合检索：FTS5 BM25（0.6）+ 向量（0.4）归一化融合」所暗示的能力不符。 | T6 造检索评测时读代码发现：`search_embeddings` 明明已经返回了 `content/app_id/platform/source/storefront`， 构造候选所需的数据全在手上，只是没被使用。 | 改成**两路召回**：向量那一路的候选（先过平台过滤）也并入候选集， FTS 分为 0、靠向量分竞争；融合权重与截断逻辑不变。新增两项测试——只有向量能找到的评论必须进入结果、 向量补进来的候选同样要过平台过滤（社交语料不能从侧门溜进来）。**质量随后已实测**（补上本地向量源之后）：装上 `sentence-transformers`（新增可选 extra `[embeddings]`）与本地 `BAAI/bge-small-zh-v1.5`，跑 `--hybrid` 得 recall@5 **0.595 → 0.714**、MRR 0.873 → 0.885、recall@3 0.595 → 0.619；逐条看 **4 条改善 / 0 条变差 / 17 条持平**（`q-localization` 0.00 → 1.00，它属于纯 FTS 一条都召回不了的那类问题）。代价如实列出：precision@5 0.319 → 0.286（召回换精度）。recall@3 的 +0.024 只有「一条查询里一个期望项」的量级，文档里写明「有信号但不显著」；0.6/0.4 权重仍未调优。 |

## 中

| # | 严重程度 | 状态 | 问题 | 证据 | 修复 |
|---|---|---|---|---|---|
| D-02 | 中 | env | 本机 `HTTP_PROXY`/`HTTPS_PROXY` 指向 `127.0.0.1:7897`，代理进程未运行时，在线采集与 DeepSeek 调用全部失败（WinError 10061）。 | 2026-08-16 实测：带代理变量直连失败；移除代理变量后 iTunes RSS 200、DeepSeek 401（可达）。 | 环境问题，不改代码。README 增加排障说明：确保本地代理可用，或临时移除代理环境变量。 |
| D-03 | 中 | open | 在线采集实际条数可能少于请求条数（如请求 200 实际 100），样本短缺影响证据覆盖度。 | 真实运行 `2391b909`：cleaning stats input_count=100 < review_limit=200。 | UI 已有 `_run_limitations` 披露"在线采集目标 N 条，实际获得 M 条"。需在 8/17 真实演示中确认页面展示。 |

| D-08 | 中 | fixed | **采集完成前中断的运行无法续跑**：界面调用 `resume()` 时从不传 `imported_reviews`，而该参数在"采集阶段尚未完成"时是必需的——即便放开了状态门，续跑也只会以 `CollectionError` 收场。 | `ui/main.py` 的 `_resume_analysis` 签名里没有这个参数；`orchestrator.resume()` 的 docstring 明确要求它。 | 新增 `_reviews_for_resume`：COLLECT 已有输出则直接用检查点里的评论；否则按来源重新导入（缺文件时给出中文提示而不是崩在采集阶段）。 |
| D-09 | 中 | fixed | **同一 App 并发互斥存在 check-then-act 竞态**：`find_active_run()` 与 `save_run()` 分两步执行，中间的空档里另一个进程可以插进来，同一个 App 被分析两遍、模型额度烧两份。孤儿判定还依赖"60 分钟未更新"这一魔数，语义也不对——更新得早不等于没人拥有它。 | 两处调用在 `AnalysisOrchestrator.start()` 里相邻但不在同一事务内。 | 改为 `BEGIN IMMEDIATE` 事务内原子占用（`repository.acquire_run`）；互斥判定改为租约语义（`lease.blocks_new_run`），保留对无租约旧记录的"最近更新时间"回退。 |

| D-14 | 中 | fixed | **抓取目标永远 down，告警常态误报**：web 的 `/metrics` 端点挂在 Streamlit 脚本里，而 Streamlit 的脚本**按会话执行**——没人打开页面时端点根本不存在。`AriProcessDown` 因此对 `ari-web` 一直为真，「一条永远在响的告警等于没有告警」。 | Prometheus targets API：`ari-web http://web:9101/metrics down`，同时 `ari-worker http://worker:9100/metrics up`。 | 抓取目标收敛到常驻的 worker——阶段耗时本就写在共享 SQLite 里，worker 读同一份数据，不抓 web 不丢信息；测试改为断言只抓常驻进程。web 的存活交给容器健康检查（Streamlit 自带 `/_stcore/health`）。 |

| D-16 | 中 | fixed | **取消请求被静默丢弃**：第一版把"请求取消"标志写在运行记录里，而运行记录的执行者只有流水线自己——请求方写进去的标志，会被执行者的**下一次写入整体覆盖**。批次边界的一次 `_update_run` 就足以把它冲掉，于是"点了停止"看起来受理成功、实际照跑到底（还在继续烧模型额度）。 | 新增的 `test_cancel_takes_effect_at_the_next_stage_boundary` 红了：请求取消之后运行仍然是 `completed`。定位很快——同阶段内的批次还在继续，说明标志在**下一个阶段边界之前**就没了。 | 取消请求改放**独立的表**（`run_cancellations`，迁移 v3），运行记录的合法写者只剩流水线自己；执行者在每个阶段边界查一次表。**同一份状态有两个写者，就一定有丢更新**——与 D-09（check-then-act 竞态）同族，只是失效方向相反：那次是多写一遍，这次是标志没了。 |

| D-11 | 中 | fixed | **`topic_recall` 缺共享词表**：它按集合精确匹配 `topic_key`，而键是模型自由生成的。 首次真实运行显示模型识别出的问题**语义正确但粒度更细**：gold `subscription_transparency` ↔ 模型的 `pre_trial_price_visibility` / `subscription_terms_clarity` / `trial_renewal_disclosure`；25+ 个预测键每个只出现一次， 而标注只有 22 个粗粒度类目。所以这个数实际测的是「与标注者选词的词面一致率」，不是召回。 | 首次真实运行 `topic_recall = 0.033` 而 `topic_key_coverage = 1.0`——键本身是规范的，排除了规范化问题。 | **处置＝改口径，而不是改模型**：① 保留数值但**改名**——评测页与文档里它叫「主题词面一致率」，`help` 里写明它不是召回率； ② 新增 **`topic_found_by_evidence`**（不依赖词表：标注主题的支撑评论至少有一条被模型引用 ⇒ 回答「这个问题被找到了吗」）； ③ 新增 **`topic_granularity`**（模型主题数 ÷ 标注主题数）——它正是词面一致率偏低的成因， 把「低得对不对」与「为什么低」分开呈现。**另外两条候选路线为何不做**：给模型受控词表与 「动态识别主题、不加预设分类表」的设计取舍直接冲突，且要改 prompt ⇒ 作废演示录制件（需重新付费录制）； 在黄金集里手工补同义键＝按模型这次的输出反推标注，等于把指标拟合到一次运行上，下次换个同义词又会掉下去。 |

## 低

| # | 严重程度 | 状态 | 问题 | 证据 | 修复 |
|---|---|---|---|---|---|
| D-04 | 低 | fixed | SQLite 连接未显式关闭，全量测试产生 736 条 ResourceWarning。 | `python -m pytest` 输出 "138 passed, 736 warnings"（2026-08-16 基线）。 | 仓库改用 `_session` 上下文（成功提交 + 始终关闭），全量测试 140 passed 且 0 warnings。 |
| D-05 | 低 | open | 需求数量可能少于 5（如本次 4 个），与计划"5–10 个 PRD 需求"存在偏差。 | 真实运行 `2391b909`：4 requirements。 | 行为符合设计：证据不足时如实披露（`quantity_notice`），不凑数。保留观察，不修复。 |
| D-21 | 低 | fixed | **测试产物被误提交进仓库**：仓库根多出一个 `MagicMock/mock.database_path/1955152130272` —— 某处把 `MagicMock` 当数据库路径用，`Path(...).mkdir(parents=True)` 于是在仓库根建出真实目录，随后被 `git add -A` 一并提交（`f71633d`，worker 队列那次）。它不会让任何测试失败，只会一直躺在仓库里，属于"没人会注意到"的那类卫生问题。 | `git ls-files` 里出现 `MagicMock/mock.database_path/1955152130272`；`git log -1 -- MagicMock` 指向 `f71633d`；`src/` 里没有任何 `MagicMock` 引用 ⇒ 来自测试/脚本侧。 | 从索引与工作区删除，并在 `.gitignore` 加 `MagicMock/` 兜底。**根因未定位**：删除后重跑全量 695 项，该目录**未再生成** ⇒ 触发它的那条路径在当前代码状态下已不可复现（可能随早前的 `_session` 连接改造一并消失）。留下防复发规则；若再次出现，二分 `pytest -x` 即可钉出用例。 |
| D-20 | 低 | fixed | **`--live-rewrite` 只活在文档里**：`scripts/run_retrieval_eval.py` 定义了这个开关却从未读过它，于是只要 `.env` 里有模型密钥，一次"离线"评测也会真实调用模型（21 次）——「默认离线、默认不花钱」这条不变量在代码里并不成立。 | 通读脚本时发现 `args.live_rewrite` 除 argparse 定义外**零引用**，而脚本头部 docstring 与 `docs/retrieval-eval.md` 都写着「仅 `--live-rewrite` 时跑」。 | 改为显式判定 `args.live_rewrite`，并把两个可选配置（查询改写 / 混合检索）拆成**互相独立**的开关；未启用时如实写进 `skipped`。 |

## 观察（非缺陷）

- 真实运行中 F-001 同时包含 15 条支持与 3 条冲突评论，冲突证据在页面"冲突评论"中展示。
- 所有发现的 `support_count`/`conflict_count` 与引用列表一致，置信度区间 (0,1]，无虚构引用。
- 清洗阶段 100 条输入 0 重复：真实评论无重复文本，符合预期（夹具中重复/近似重复路径由单元测试覆盖）。
