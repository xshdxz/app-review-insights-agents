# 演示模式与模型层录制回放设计

## 背景与目标

公网演示部署后暴露一个缺口：未配置模型密钥时 `build_pipeline_services`（`factory.py:68`）走
`if use_fake_provider or not settings.model_available` 分支，返回的 `PipelineServices` 里
`batch_analyzer=None`，界面据此把「开始分析」置为禁用。访客只能浏览附带的离线档案，
**无法触发任何流水线行为**——「这套系统真的能跑」在演示里看不到。

同时，仓库现有的离线素材粒度不对，无法直接复用：

- `data/cache/demo-run.json` 记录的是**阶段输出**（`clean` / `validate_findings` / `plan` /
  `generate_tests` / `validate_traceability`），不含原始模型响应；
- `tests/conftest.py` 的 `SchemaFakeProvider` 按顺序 `responses.pop(0)` 返回预置 dict，
  不做请求匹配，且位于测试目录，属测试夹具而非产品代码。

因此要让演示既能执行、又不依赖外部 API，需要先补一层**模型层录制回放**。

目标：

- 访客在公网演示上点击「开始分析」，能看到十一阶段流水线真实推进、检查点真实落盘、
  证据链校验与降级判定真实发生；
- 演示全程**零外部调用、零成本、结果确定可复现**，不受网络、限流、模型波动影响；
- 界面**绝不让访客误以为自己的输入被真实分析**；
- 回放机制同时提升测试保真度：回归测试可以跑真实录制，而非手写假响应。

## 方案选择

比较过三条路线：

1. **手写脚本响应**：新写一套与样例对齐的罐头响应。可离线，但内容是人编的，
   与真实模型输出分布脱节，且每次改 prompt 都要手工重对齐。
2. **直接渲染录制档案**：把 `demo-run.json` 按阶段依次播出来。复用现成资产，但那是
   **播放动画**，流水线并未执行，校验与降级都不会真的发生，说服力反而弱于现有的只读浏览。
3. **模型层录制回放**（采用）：在 provider 边界录制 `请求 → 响应`，之后按同一键回放。
   真实运行、真实执行、真实校验，只是模型那一步换成读文件。

选 3 的决定性理由是它同时解决一个**既有弱点**：当前回归测试依赖手写假 provider，
与真实模型的输出分布长期脱节；有了录制，测试可以跑真实录制，prompt 改动的影响也可对比。
这是一次投入、两处收益，而 1 和 2 只服务演示一件事。

## 设计

### 1. 模式判定

新增配置 `DEMO_MODE`，取值 `auto`（默认）/ `live` / `replay`：

| 取值 | 行为 |
|---|---|
| `auto` | 有可用密钥 → 真实模型；无密钥 → 回放 |
| `live` | 强制真实模型；无密钥时报配置错误，不静默降级 |
| `replay` | 强制回放；即使配了密钥也走回放（演示现场不怕网络与模型波动） |

「有可用密钥」沿用既有判定 `settings.model_available`，不新造语义。

### 2. 录制格式

录制文件放 `data/recordings/`，与已纳入版本管理的 `data/cache/`、`data/samples/` 同级
（`.gitignore` 只忽略 `data/runs/`、`data/uploads/`、`data/agent/`，本目录默认会被提交）。

```json
{
  "mode": "recorded_live_run",
  "is_live": false,
  "recorded_at": "2026-09-17T...Z",
  "model": "deepseek-chat",
  "input_fingerprint": "<清洗后评论集指纹>",
  "entries": [
    {
      "key": "<sha256(schema名 + \u0000 + system_prompt + \u0000 + user_prompt) 前 16 位十六进制>",
      "schema": "FindingDraftList",
      "request": { "system": "...", "user": "..." },
      "response": { }
    }
  ]
}
```

顶层必须携带 `mode` 与 `is_live`，并沿用 `storage/cache.py:26` 已确立的**强制标注约定**——
加载时校验 `mode == "recorded_live_run"` 且 `is_live is False`，缺失或标错即拒绝加载。
把录制文件伪装成实时结果这条路必须在加载层就堵死。

`request` 一并落盘，有两个用途：键冲突时可人工比对；回放未命中时错误信息里能给出
最接近的请求片段，便于定位是哪一步的 prompt 变了。

### 3. 录制触发

新增配置 `MODEL_RECORD_PATH`（默认空 = 不录制）。非空时，`factory.build_pipeline_services`
在构造 provider 后用 `RecordingProvider` 包装一层，把每次 `generate` 的请求与响应追加写盘。

录制产物就是回放来源：把 `MODEL_RECORD_PATH` 指向目标文件跑一次真实分析，得到的文件
再作为 `DEMO_REPLAY_PATH` 的内容提交进仓库。两者是同一个格式、同一条流水线产物，
不存在第二套数据。

录制不改变任何调用语义：包装层透传返回值与异常，写入失败只记 warning 不影响主流程
（与 `llm/provider.py:71` 处理用量计量失败的方式一致）。

### 4. provider 组装

`build_pipeline_services`（项目唯一装配点）按模式返回：

- `live`：`DeepSeekProvider`（可选被 `RecordingProvider` 包装）
- `replay`：`ReplayProvider`，读 `DEMO_REPLAY_PATH`（默认
  `data/recordings/demo-replay.json`）

调用方（`ui/main.py`、`agent/tools.py`、`scripts/*`）无需感知选择逻辑。

配置命名刻意区分方向，避免写成一对近似名而互相看错：

| 配置 | 方向 | 默认 |
|---|---|---|
| `DEMO_MODE` | 模式选择 | `auto` |
| `MODEL_RECORD_PATH` | **写**录制 | 空（不录制） |
| `DEMO_REPLAY_PATH` | **读**录制 | `data/recordings/demo-replay.json` |

三项都是新增配置，按仓库既有约定必须同步更新三处：`config.py`、`.env.example`、
`README.md` 的配置表。遗漏任何一处都算未完成。

### 5. 输入锁定

录制键里含 prompt，因此回放**只对同一输入有效**。演示模式下必须锁死输入：
`在线采集` 与 `JSON/CSV 导入` 一并禁用，固定为仓库自带样例，并就地说明原因。

不锁的后果是**撒谎**：访客贴进自己 App 的链接，看到的却是样例的结论。这直接违反项目
既有约束「离线演示不能失效，也不能撒谎」。

加载录制文件时先比对 `input_fingerprint` 与本次清洗后的评论集；不一致立即停下来并给出
可读提示，而不是跑到中途才因键未命中失败。

### 6. 回放未命中的处理

`ReplayProvider.generate` 键未命中时抛 `RecoverableModelError` 的子类（新增
`ReplayMissError`），而不是返回空值或猜测结果。流水线据此停在检查点，状态转为
`waiting_for_model`，已完成阶段不重跑——与模型调用失败走的是同一条既有路径，
不引入第二套失败语义。

### 7. 界面标注

演示模式顶部常驻一条说明：**「演示模式：回放一次真实运行的模型输出，不调用外部 API，
数据为仓库自带样例」**。结果视图沿用现有的 `is_live` / `mode` 标注机制，不新造标记。

### 8. 与既有「查看历史缓存演示」的区别

两者都保留，职责不同，必须在界面上分得清：

| | 查看历史缓存演示（既有） | 演示模式（本次） |
|---|---|---|
| 内容 | 一次运行的**阶段输出**档案 | 重跑**整条流水线** |
| 性质 | 只读浏览历史结果 | 可操作 |
| 数据流 | 直接渲染 JSON | 走完整十一个阶段 |
| 证明的事 | 真跑过 | 系统能跑 |

### 9. 输入状态的 URL 参数

现状：`ui/main.py:94` 的 `_sync_inputs_to_query_params()` 在每次重跑时把
`mode` / `url` / `goal` / `limit` / `upload` 写进查询参数，配套 `ui/main.py:67` 的
`_restore_inputs_from_query_params()` 用于刷新恢复。

它带来三个问题，对公开演示尤其明显：

1. 分享出去的链接会带上填写者的表单内容；
2. `upload` 会把上传文件名留在地址栏与浏览器历史里；
3. 空值也照写（`url=` 为空时仍然出现）。

本次修订：

- 演示模式下**完全不写**查询参数（输入已锁定，恢复机制没有意义）；
- 非演示模式下**不写空值**；
- README 与部署文档给出「分享用无参数裸链接」的说明。

### 10. 文档修订

`docs/deploy-streamlit-cloud.md` 有两处必须改：

- **第 72 行附近的预算说明给的是虚假安全感**。每日额度经 `spent_usd(None, start_of_today_utc())`
  从 `model_usage` **表**读取（`llm/budget.py:50`），该表在 `data/runs/runs.sqlite3`，
  而 Cloud 的文件系统在重启/休眠/重新部署后会重置——`MODEL_BUDGET_USD_PER_DAY` 
  **不是硬上限**。`MODEL_BUDGET_USD_PER_RUN` 不受影响，仍然完全有效。该段需如实改写，
  并以它论证「公开演示为什么不挂密钥」。
- 补演示模式的说明与「分享用裸链接」的指引。

## 数据流

```
无密钥启动
  → factory 判定 replay
  → 界面进入演示模式：输入锁定为样例、顶部标注、按钮可用
  → 「开始分析」
      → 采集/导入样例评论（确定性，真实执行）
      → 清洗（确定性，真实执行）
      → analyze_batches / consolidate / audit_evidence
            → ReplayProvider 按键查录制并返回（零外部调用）
      → validate_findings / plan / generate_tests / validate_traceability（确定性，真实执行）
  → 结果视图，标注 is_live=false、mode=replay
```

## 测试策略

全部离线，不调用真实模型，沿用现有约定：

- **录制往返**：录制包装层的输入输出与直接调用一致；录制文件可被回放层完整读回；
- **键稳定性**：同一 `(schema, system, user)` 得到同一键；任一变化都换键；
- **未命中失败**：键不存在时抛 `ReplayMissError`（`RecoverableModelError` 子类），
  **不是**返回空或默认值；流水线停在检查点、状态为 `waiting_for_model`；
- **标注强制**：`mode` 或 `is_live` 缺失/错误的录制文件被拒绝加载；
- **指纹校验**：`input_fingerprint` 与当前评论集不一致时，在运行前即失败；
- **输入锁定**：演示模式下采集与上传控件不可用；
- **URL 参数**：演示模式下不写入查询参数；非演示模式下空值不写入；
- **回归**：既有 459 项测试保持全绿，真实 provider 路径行为不变。

## 验收标准

1. 无密钥启动，界面进入演示模式，输入锁定且顶部标注清楚；
2. 点击「开始分析」可完整跑完十一个阶段并给出结果，全程无外部 API 调用；
3. 同一次演示重复运行结果一致；
4. 录制文件被篡改 `mode` / `is_live` / `input_fingerprint` 时被拒绝或提前失败，
   **不会**静默产出错误结果；
5. 真实密钥下的行为与本次改动前一致；
6. `ruff check .` 与 `ruff format --check .` 双绿，测试数只增不减。

## 明确不做

- 不做访问控制：公开演示不挂密钥，没有需要保护的成本面；且登录墙会让访客无法进入。
- 不做后台执行与取消：回放无网络调用，流水线秒级完成，不存在阻塞会话的问题。
  该改造属独立子项目，另行设计。
- 不引入 Redis/Celery 等外部队列。
