# 验收矩阵

验证标准：每项必须有可复现的证据（运行 ID、输出文件或测试结果），不能只凭按钮点击判断。

## 验收项

| # | 验收项 | 结果 | 证据 |
|---|---|---|---|
| 1 | 指定应用（Workout for Women）+ 订阅转化目标能够完成分析 | ✅ | 真实运行 `2391b909-6947-4f70-88d3-dc11636420d6`（在线采集 100 条，completed）；`output/real-validation-workout-2026-08-16.json` 全部检查通过 |
| 2 | 未见过的数据集 + 不同分析目标能够完成分析 | ✅ | Todoist 在线运行 `f0858d60`（200 条，34 发现/10 需求/40 用例，completed）；混合语言夹具运行 `real-validation-mixed-2026-08-17.json`。三组目标主题完全不同（订阅 vs 效率/同步 vs 计时器），无写死分类 |
| 3 | JSON 导入能够完成 | ✅ | 混合语言夹具 JSON 运行：10→8 清洗，2 发现，追溯 0 问题 |
| 4 | CSV 导入能够完成 | ✅ | `tmp/reviews-mixed.csv` 真实运行：10→8 清洗，4 发现，追溯 0 问题（`real-validation-csv-2026-08-17.json`） |
| 5 | 混合语言评论仍可追溯 | ✅ | 混合夹具运行：中英文 8 条评论全部有中文摘要，发现引用全部存在，追溯 0 问题 |
| 6 | 重复评论已删除并计数 | ✅ | 单测 `test_full_imported_pipeline_completes_with_traceability`：exact=1、near=1；`tests/test_cleaning.py` |
| 7 | 冲突评论清晰可见 | ✅ | Workout 运行 F-001：15 支持 + 3 冲突；UI 证据复核表展示角色列 |
| 8 | 证据不足时标记为假设（Assumption） | ✅ | `tests/test_validation.py` 覆盖；真实运行中单条证据发现均显示"按假设展示"局限 |
| 9 | DeepSeek 失败时保留进度 | ✅ | 真实运行 `2391b909` 首次尝试因输出截断失败进入 `waiting_for_model`，检查点保留（COLLECT/CLEAN 完整） |
| 10 | 续跑沿用原始 `run_id` 并跳过已完成批次 | ✅ | 同一 run_id 续跑完成；`analyzer.calls == 3` 回归测试；真实续跑事件序列确认 |
| 11 | 缓存被明确标记为历史数据/非实时数据 | ✅ | `data/cache/demo-run.json`：`mode=historical_cache_demo`、`is_live=false`；`test_demo_cache_is_explicitly_labeled` |
| 12 | 下载文件包含 ID 和 UTF-8 文本 | ⏳ | 从真实运行构造下载并校验（待执行） |
| 13 | 全新克隆后可按 README 完成启动 | ⏳ | 同级目录全新克隆安装验证（待完成） |
| 14 | 仓库未跟踪密钥或私有文件 | ✅ | `git status --short --ignored`：`.env`、`data/runs/`、`output/`、`tmp/`、`.planning/` 均忽略；密钥扫描无真实密钥命中 |

## 演示截图清单（docs/images/）

- [ ] 输入与运行进度页面
- [ ] 一个同时包含支持与冲突评论的已验证发现（Finding）
- [ ] 一个带验收标准的 PRD 需求
- [ ] 一条测试用例及其追溯路径
- [ ] 模型失败与续跑状态（可复用真实 `waiting_for_model` 记录或演示缓存）

## 最终验证命令

```powershell
.\.venv\Scripts\python -m pytest --cov=app_review_insights
.\.venv\Scripts\python -m ruff check .
git diff --check
git status --short
```
