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

### 不配密钥（默认，推荐先这样）

应用进入**离线演示模式**：

- 界面完整可浏览，可打开附带的离线演示档案（真实流水线产物）
- 能看发现 / PRD 需求 / 测试用例 / 证据链与四个下载
- 「开始分析」按钮**禁用**并说明原因 —— 绝不把历史缓存伪装成实时结果
- 零 API 成本，不会被陌生人点着烧钱

### 配密钥（可选，能真实跑分析）

在 app 的 **Settings → Secrets** 里填入（TOML 格式）：

```toml
DEEPSEEK_API_KEY = "sk-..."
```

**必须是顶层键**。Streamlit Cloud 只把**顶层** secret 注入为环境变量；
写在 `[section]` 里的不会。本项目的 `Settings`（pydantic-settings）读环境变量，
所以顶层写法能直接被读到，**不需要改代码**。

**公开 demo 一定要同时设预算上限**：

```toml
DEEPSEEK_API_KEY = "sk-..."
MODEL_BUDGET_USD_PER_DAY = "1"
MODEL_BUDGET_USD_PER_RUN = "0.2"
```

没有上限的公开 LLM demo = 不可控开支。超预算时流水线会**停在检查点**并给出可读提示，
不会崩溃，调高上限后可用同一 `run_id` 续跑。

---

## 部署后自检

| 检查项 | 预期 |
|---|---|
| 页面能打开 | 出现「证据审阅工作台」标题 |
| 无密钥时 | 顶部黄色提醒「未配置 DEEPSEEK_API_KEY…」，开始分析按钮禁用 |
| 打开「查看历史缓存演示」 | 显示档案，且明确标注「历史缓存演示 / 非实时」 |
| 四个下载按钮 | 清洗评论 JSON / PRD JSON / 测试用例 CSV / 证据链 CSV 均可下载 |
| 配了密钥时 | 顶部显示「密钥已填写」，开始分析按钮可点 |

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
