# 部署到 Streamlit Community Cloud

免费的公开 demo 托管。**不用装环境就能在线打开** —— 比 README 里的一张截图更有说服力。

---

## 为什么这个仓库必须有 `requirements.txt`

Streamlit Cloud 会按固定优先级寻找依赖文件，**用找到的第一个**：

```text
uv.lock → Pipfile → environment.yml → requirements.txt → pyproject.toml (poetry)
```

本仓库有两个坑，`requirements.txt` 正好同时解决：

1. **`pyproject.toml` 会被当成 poetry 文件**。本项目用的是 setuptools 后端，Cloud 按 poetry 解析必然出问题。`requirements.txt` 优先级更高，会先被采用。
2. **`src/` 布局**。Cloud 只把**仓库根**加进 `sys.path`，而包在 `src/app_review_insights/`。不安装包，`app.py` 里的 import 直接失败。

所以 `requirements.txt` 的内容是一行 `-e .` —— 让 pip 从 `pyproject.toml` 安装本项目及其全部依赖。
依赖清单只有一处（`pyproject.toml`），不会两边不同步。

---

## 部署步骤

1. 打开 <https://share.streamlit.io>，用 **GitHub 账号登录**
2. 点 **Create app**（或 New app）
3. 填三项：

   | 字段 | 值 |
   |---|---|
   | Repository | `xshdxz/app-review-insights-agents` |
   | Branch | `main` |
   | Main file path | `app.py` |

4. 点 **Advanced settings…**（关键，别跳过）：

   - **Python version** 选 **3.11**
     （项目要求 `>=3.11`；选 Cloud 默认的更新版本未经验证，3.11 是 CI 覆盖过的最稳版本）
   - **Secrets** 可以留空（见下一节）

5. 点 **Deploy**，等构建（首次约 2–5 分钟）

部署完成后会得到一个形如 `https://<app-name>.streamlit.app` 的公开链接。

---

## 两种运行模式

### 不配密钥（默认，推荐）：演示模式回放录制

不配置任何密钥时，应用进入**演示模式**：在自带的 20 条样例评论上回放一次真实运行的模型
输出，**不发起任何外部 API 调用**，而「开始分析」是活的。

进入演示模式的条件按 `DEMO_MODE` 分两档（`config.py` 的 `demo_replay_active`）：

- **`auto`（默认）**：下面三条**同时**成立 ——
  1. `DEMO_MODE` 保持默认的 `auto`；
  2. 没有可用密钥 —— `DEEPSEEK_API_KEY` / `MODEL_API_KEY` 均为空，**或** `MODEL_ENABLED=false`；
  3. 录制文件存在（`DEMO_REPLAY_PATH`，默认 `data/recordings/demo-replay.json`，随仓库提交）。
- **`replay`（显式）**：**只**要求录制文件存在。这一档直接判定为回放，根本不看密钥 ——
  配了密钥也照样走回放（`factory.py` 里回放分支排在密钥判断之前）。

录制文件缺失时，两档的界面表现不同：

- `replay` 档装配失败并抛 `InputDataError`，界面在「模型装配未完成」横幅里**明说录制文件缺失**
  （文案给出 `DEMO_REPLAY_PATH` 的路径与 `scripts/record_demo.py`），开始分析按钮禁用。
- `auto` 档退回旧行为：装出一个没有模型的降级流水线，按钮禁用，界面出现的是**模型状态提醒**
  （「未配置 MODEL_API_KEY…」或「已显式禁用（MODEL_ENABLED=false）…」），**不会提到录制文件缺失**。

界面呈现为：

- 顶部黄色提醒：**「演示模式：回放一次真实运行的模型输出，不调用外部 API，输入固定为仓库自带样例；点「开始分析」可在样例上跑完整条流水线。」**
  （旁边那条「模型状态」提醒说的是实时模型调用不可用，与回放互不影响：密钥为空 →「未配置 MODEL_API_KEY…」，
  `MODEL_ENABLED=false` →「已显式禁用（MODEL_ENABLED=false）…」）
- 「数据来源」锁为**演示样例**，「分析目标」与「评论数量」禁用 —— 回放按请求指纹命中，
  改动任一项都会全量未命中，所以干脆不给改；
- 「开始分析」**可点**：跑完整条流水线（十一个阶段）到 `completed`，给出发现 / PRD 需求 /
  测试用例 / 证据链与四个下载；同一份录制重复运行，结果一致；
- 零 API 成本，陌生人怎么点都不会产生费用。

**那条标注的含义**：结果来自一次真实付费运行的录制（`mode=recorded_live_run` /
`is_live=false`），不是本次调用模型产生的。标注被篡改（`mode` 或 `is_live` 不符）时
文件会被拒绝加载；请求在录制里没有对应键时抛 `ReplayMissError`、停在检查点、状态
`waiting_for_model`，原因都会显示在界面上。

**下载的产物带同一套标注**：回放与历史档案的四个下载，两个 JSON 各内嵌 `mode` / `is_live`
两个键、两个 CSV 各加同名的两列（回放取 `recorded_live_run`，历史档案取
`historical_cache_demo`，`is_live` 均为 `false`）；**实时运行的产物不带** —— 它本来就是
本次调用的结果，盖章会把实时结果伪装成回放。

「查看历史缓存演示」开关仍然保留，它打开的是另一份离线档案（`data/cache/demo-run.json`），
与上面的回放互不影响。

**分享请使用不带查询参数的裸链接**：演示模式不写入任何 URL 查询参数，还会清掉链接里带进来
的旧参数（`?goal=`、`?url=` …），所以直接复制地址栏得到的就是干净链接。普通模式同样只写
非空值，被清空的字段会从 URL 中删除，不会在刷新后复活。

**重新录制**（需要密钥，会产生一次真实费用）：

```powershell
.\.venv\Scripts\python scripts/record_demo.py
```

脚本在同一个样例上跑一次完整分析，把每次模型调用的请求与响应写进
`data/recordings/demo-replay.json`（原子写：先落临时文件再替换）。**换了样例、改了 Prompt
或 Schema 都要重新录制**：换了样例时，回放在**跑任何阶段之前**就因输入指纹不符而失败
（不创建运行记录、不落盘任何阶段输出）；改了 Prompt 或 Schema 时，请求逐条未命中、抛
`ReplayMissError` 停在检查点。演示模式本身不需要密钥。

> 回放覆盖的是**分析流水线**（工作台主页面）。Agent 侧（产品情报问答 / 监控任务）仍然要求
> 配置密钥，无密钥时按既有的降级路径处理。

### 配密钥（可选）：密钥有效且未被 `MODEL_ENABLED=false` 禁用时才走真实模型

在 app 的 **Settings → Secrets** 里填入（TOML 格式）：

```toml
DEEPSEEK_API_KEY = "sk-..."
```

**必须是顶层键**。Streamlit Cloud 只把**顶层** secret 注入为环境变量；
写在 `[section]` 里的不会。本项目的 `Settings`（pydantic-settings）读环境变量，
所以顶层写法能直接被读到，**不需要改代码**。

**预算上限的真实边界**：

两个变量读的是**同一张 `model_usage` 表**（`factory.py` 里由同一个回调供给），该表位于
`data/runs/runs.sqlite3`。它们的区别不在「数据存在哪儿」，而在**约束的是什么**：

| 变量 | 约束的是什么 | 依据 |
|---|---|---|
| `MODEL_BUDGET_USD_PER_RUN` | **单次运行**的花费：按当前 `run_id` 汇总 | `llm/budget.py`：`spent_usd(current_run_id.get(), None)` |
| `MODEL_BUDGET_USD_PER_DAY` | **当日累计**花费：按 UTC 当日窗口汇总 | `llm/budget.py`：`spent_usd(None, start_of_today_utc())` |

守卫挂在**每次模型调用之前**（`llm/provider.py` 的 `generate`），所以准确的边界是
「超限后不再发起新的模型调用」，而不是「总额绝不超过上限」—— 跨过阈值的那一刻，
已经在途的那一笔调用仍会完成并计入。

这个库**不是持久存储**：Streamlit Community Cloud 的容器文件系统在重启、休眠唤醒与
重新部署之后不再保留写在里面的记录。这一条对两个计数都成立，影响要分开说：

- `MODEL_BUDGET_USD_PER_DAY`：当日计数与容器同生共死，容器一换就归零。
  把它当成公开部署的兜底，得到的只是虚假安全感。
- `MODEL_BUDGET_USD_PER_RUN`：计数同样存在这个库、同样会随容器归零 —— 但**这次运行本身也已经没了**，
  续跑要从头开始，不存在「换个容器接着花上一轮额度」这回事。它管住的是单次运行的开销面，
  不是一份能跨容器累计的账。

**公开部署的正确做法是不挂密钥、留在演示模式**（本节开头那一档）：没有密钥就没有成本面，
不需要靠预算上限兜底。

确实要给受控范围内的部署配密钥时，至少设 `MODEL_BUDGET_USD_PER_RUN`：

```toml
DEEPSEEK_API_KEY = "sk-..."
MODEL_BUDGET_USD_PER_RUN = "0.2"
```

超预算时流水线会**停在检查点**并给出可读提示，不会崩溃，调高上限后可用同一 `run_id` 续跑。

---

## 部署后自检

| 检查项 | 预期 |
|---|---|
| 页面能打开 | 出现「证据审阅工作台」标题 |
| 无密钥时（`DEMO_MODE=auto` 且录制文件存在） | 出现「演示模式：回放一次真实运行…」提醒；「模型状态」那条按原因二选一 —— 密钥为空 →「未配置 MODEL_API_KEY…」，`MODEL_ENABLED=false` →「已显式禁用（MODEL_ENABLED=false）…」。数据来源锁为「演示样例」，开始分析按钮**可点** |
| 演示模式下点「开始分析」 | 跑完整条流水线并给出结果；地址栏不出现任何查询参数 |
| 打开「查看历史缓存演示」 | 显示档案，且明确标注「历史缓存演示 / 非实时」 |
| 四个下载按钮 | 清洗评论 JSON / PRD JSON / 测试用例 CSV / 证据链 CSV 均可下载 |
| 配了密钥且密钥有效（`MODEL_ENABLED=true`，`DEMO_MODE` 为 `auto`/`live`） | 「模型状态」提醒消失，开始分析按钮可点，走真实模型调用 |
| 配了密钥但校验没通过 | 「模型状态」提醒**不消失**：认证失败 →「DeepSeek 密钥无效（认证失败）…」，连不上模型服务 →「暂时无法连接模型服务…」。按钮仍可点，但分析会停在模型环节并保留检查点，修正后可用同一 `run_id` 续跑 |
| 配了密钥但 `DEMO_MODE=replay` | 仍走回放、不是真实调用 —— `replay` 档不看密钥（见「两种运行模式」） |

---

## 常见问题

### 构建失败，日志提到 `pyproject.toml` 或 poetry

说明 Cloud 没有优先采用 `requirements.txt`。确认该文件在**仓库根目录**（与 `app.py` 同级）。

### 构建失败，日志提到找不到包 / ImportError: app_review_insights

说明 `-e .` 没有生效。备用方案：把 `requirements.txt` 改成显式依赖列表
（`streamlit`、`pydantic`、`pydantic-settings`、`openai`、`httpx`、`pandas`、`rapidfuzz`、`langdetect`、`apscheduler`，
版本区间照抄 `pyproject.toml`），并在 `app.py` 顶部加三行把 `src` 加进 `sys.path`：

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
```

### 提示内存超限 / 应用被重启

免费层上限 **1 GB**。本项目运行时不会加载 `sentence-transformers`（它是可选依赖、懒加载），
常规占用远低于上限。若仍超限，检查是否误开了 `EMBEDDING_ENABLED=true`。

### 一段时间没人访问后打开很慢

免费层无人访问会**休眠**，首次访问需要唤醒（约 30 秒）。这是正常行为，不是故障。

---

## 相关文件

| 文件 | 作用 |
|---|---|
| `requirements.txt` | Cloud 的依赖入口（`-e .`） |
| `pyproject.toml` | 依赖与工具配置的唯一事实来源 |
| `.streamlit/config.toml` | 主题配色（Cloud 会自动读取） |
| `app.py` | 入口文件 |
