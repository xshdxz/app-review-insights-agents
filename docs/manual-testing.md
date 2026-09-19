# 手动测试指南：模型连接失败与离线模式

本指南用于在不改代码的情况下，手动验证两个关键可靠性场景：
1. **模型连接失败**：DeepSeek 不可用时，页面不中断、进度保留、可同 `run_id` 续跑。
2. **离线模式**：没有模型密钥时，演示与导入能力可用，不崩溃、不伪装实时结果。

> 对应自动化测试：`tests/test_app_smoke.py`、`tests/test_orchestrator.py`、`tests/test_offline_ui.py`、`tests/test_export.py`。
>
> 括号里的数字是写作时的历史值（当时全量 145 项）。**当前数字以 README 为准**：主套件 541 项、
> 可靠性套件（`pytest -m reliability`）21 项。

---

## 场景一：模型连接失败（DeepSeek 不可用）

### 准备

编辑项目根目录 `.env`（改完记得恢复）：

```dotenv
DEEPSEEK_API_KEY=sk-invalid-test-key   # 故意填无效密钥
MODEL_MAX_RETRIES=0                    # 快速失败，不用等重试
```

### 步骤

1. 启动：`.\.venv\Scripts\streamlit run app.py`
2. 数据来源选 **CSV 导入**，上传 `data/samples/reviews-sample.csv`（或 JSON 样例）
3. 点 **开始分析**

### 预期结果

| 检查点 | 预期 |
|---|---|
| 分析按钮 | 可点击（有密钥配置状态，未验证） |
| 运行状态面板 | 约几秒后显示 **“等待模型恢复”**（waiting_for_model） |
| 错误信息 | 显示失败原因，且**密钥已被脱敏**（`[REDACTED]`），不泄露真实密钥 |
| 页面中断 | **不中断**；评论数据/清洗统计仍可查看（检查点已保存到 SQLite） |
| 实时动态 | 事件日志显示“已保存进度，等待模型恢复” |

### 恢复与续跑

4. 把 `.env` 的 `DEEPSEEK_API_KEY` 改回**有效密钥**，`MODEL_MAX_RETRIES` 恢复为 `2`
5. 刷新页面 → 右侧出现 **继续分析** 按钮 → 点击
6. 预期：同一 `run_id` 从检查点继续，跳过已完成批次，最终状态 **“已完成”**，结果完整（发现/PRD/测试用例/证据链）

命令行等价验证（无需页面）：

```powershell
# 制造失败（无效密钥环境变量，注意 PowerShell 会覆盖 .env）
$env:DEEPSEEK_API_KEY='sk-invalid-test-key'
.\.venv\Scripts\python scripts/run_real_validation.py --file data/samples/reviews-sample.csv --goal "测试目标" --out output/fail-check.json
# 恢复有效密钥（移除环境变量后读取 .env）
Remove-Item Env:DEEPSEEK_API_KEY
.\.venv\Scripts\python scripts/run_real_validation.py --resume <RUN_ID> --goal "测试目标" --out output/resume-check.json
```

`run_real_validation.py` 会逐项校验证据链，任何违反都以非零退出码结束。

---

## 场景二：离线模式（无模型密钥）

> **先看这里**：本节表格描述的是**录制文件缺失**时的降级形态（按钮禁用）。只要
> `data/recordings/demo-replay.json` 在场，"开始分析"是**可点**的——应用进入演示模式，
> 在自带样例上回放一次真实运行的模型输出、零外部调用。两者的区别与判定条件见
> `docs/deploy-streamlit-cloud.md` 的「两种运行模式」。

### 准备

把 `.env` 中的 `DEEPSEEK_API_KEY` 清空（`DEEPSEEK_API_KEY=`），或设置 `MODEL_ENABLED=false`。

### 步骤

1. 启动：`.\.venv\Scripts\streamlit run app.py`

### 预期结果

| 检查点 | 预期 |
|---|---|
| 顶部横幅 | 黄色提醒“未配置 DEEPSEEK_API_KEY…”，**正常状态时无横幅**（只提醒故障） |
| 开始分析按钮 | **禁用**，且提示原因 |
| 查看历史缓存演示开关 | 可打开；展示历史缓存档案，**明确标注“历史缓存演示 / 非实时”** |
| 下载 | 演示档案 4 个下载（清洗评论/PRD/测试用例/证据链）可用；样例评论 JSON 可下载 |
| 页面崩溃 | 无异常（AppTest 断言 `not app.exception`） |

### 说明

- 离线模式**不会声称完成实时 AI 分析**：按钮禁用 + 缓存显式标记为历史数据。
- 导入文件在无密钥时也不能启动分析（需要模型），但可以先行准备文件。
- 命令行：无密钥时 `scripts/run_real_validation.py` 输出明确错误并退出码 `2`（“实时模型不可用”）。

---

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 在线采集“评论源返回 0 条数据” | Apple 公开评论接口临时波动（D-06） | 稍后重试或改用 JSON/CSV 导入 |
| 点击开始后一直“等待模型恢复” | 密钥无效/网络不通/接口故障 | 检查密钥与网络；修复后点“继续分析” |
| 设置了有效密钥仍显示未配置 | PowerShell 空环境变量未屏蔽 `.env`；或 `MODEL_ENABLED=false` | 检查 `.env` 与 `MODEL_ENABLED`；确认环境变量为空字符串时直接清空 `.env` 密钥 |
