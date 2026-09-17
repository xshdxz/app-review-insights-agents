# 全链路测试记录

日期：2026-08-16
分支：`main`（HEAD：`ef7b054`）

## 自动化与安装验证

| 项目 | 结果 | 证据 |
|---|---|---|
| 完整 pytest | ✅ 全部通过 | 两遍独立运行：12.47s / 14.21s |
| 覆盖率 | ✅ 94% | 核心模块 traceability 100% / validate 100% / repository 100% / cleaning 97%（`pytest --cov` 输出） |
| Ruff | ✅ check 通过、format 通过 | `ruff check .` |
| pip check / git diff --check | ✅ | 均 exit 0 |
| 全新环境安装启动 | ✅ | 同级目录全新 clone → venv → `pip install -e ".[dev]"` → 测试全绿 → `streamlit run app.py` HTTP 200；无 `.env` 时开始按钮禁用、"未配置"提醒明确；演示模式可用（4 个下载）；验证后目录已删除 |
| 演示缓存 | ✅ | `data/cache/demo-run.json` 加载通过：`mode=historical_cache_demo`、`is_live=false`、追溯 valid |
| JSON 导入 | ✅ | 样例 20 条 + 真实评论 100 条 |
| CSV 导入 | ✅ | 混合夹具 10 条 |
| 在线采集 + DeepSeek 模式 | ✅ | 在线采集 50 条完成全流程（见下） |
| 模型失败保留进度 + 同 run_id 续跑 | ✅ | 真实故障验证（见下） |

## 真实场景验证

### 发现并修复 D-06（高优先级，当天修复）

- **现象**：在线采集返回 0 条。诊断发现 Apple 公开评论 RSS 再次变化：主 URL（`urlDesc` 分页方案）对全部 App 返回空 feed；旧式 URL 仅部分 App 第一页可用。
- **证据**：Notion 主 URL 空 / 旧式 48238 字节有数据；Workout 两种 URL 均空；Notion 旧式连续 5 次探测稳定。
- **修复**：采集器主 URL 空时回退旧式第一页 URL，两者皆空才报错引导导入；新增回归测试（`test_collector_falls_back_to_legacy_url_when_primary_feed_is_empty`），全量测试通过。提交 `ef7b054`。
- **取舍**：第三方兼容问题保留适配器与明确错误说明，同时确保 JSON/CSV 导入与离线演示始终可用。

### 真实运行

| 运行 | 数据 | 目标 | 结果 |
|---|---|---|---|
| 指定 App 重跑 | Workout for Women 真实 100 条评论（检查点导出 JSON 导入） | 订阅转化 | ✅ completed；3 发现/3 需求（证据不足如实披露）/12 用例；追溯 0 问题 |
| 陌生数据集（在线） | Notion 在线采集 50 条 | 新手引导/文档组织/协作效率 | ✅ completed；15 发现/10 需求/40 用例；50/50 中文摘要；追溯 0 问题 |
| 真实故障续跑 | 样例 20 条（无效密钥制造失败） | 模型失败保留进度 | ✅ 无效密钥 → `waiting_for_model`（analyze_batches，collect/clean 检查点保留）→ 恢复有效密钥同 run_id 续跑 → completed，全部校验通过 |

### 追溯逐条抽查（Workout 运行）

- F-001 "免费内容大幅减少，付费墙限制基本功能"（15 支持 + 3 冲突）：支持评论原文均为"更新后免费功能减少/被迫换应用"，冲突评论为正面评价 → REQ-001"优化免费内容与付费墙体验"（V1.0）→ TC-001~004（例："验证未订阅用户可完成至少 3 个免费基础训练"）✅
- F-002 "试用期扣费不透明"（7 支持）：支持评论均为"试用未提醒扣费/误以为年付" → REQ-002"提高试用期与续费透明度" → TC-005~008 ✅
- F-003 "取消订阅路径困难，客服支持无效"（5 支持）→ REQ-004"简化取消订阅流程并提升客服支持" → TC-013~016 ✅
- F-004 "广告体验差，强制跳转外部网站"（4 支持）→ REQ-003"改善广告体验，避免强制跳转"（V1.1）→ TC-009~012 ✅

结论：发现 ↔ 评论原文 ↔ 需求 ↔ 测试用例语义一致，中文摘要忠实，无虚构引用。

### 下载文件校验

- 证据链 CSV：UTF-8 BOM，表头 `review_ids,finding_id,requirement_id,test_case_id`，16 行，ID 完整 ✅
- 测试用例 CSV：8 字段，16 行，中文标题正常 ✅
- PRD JSON：4 需求含 ID ✅；清洗评论 JSON：100 条 ✅

## 最终验证命令

```powershell
.\.venv\Scripts\python -m pytest --cov=app_review_insights
.\.venv\Scripts\python -m ruff check .
git diff --check
git status --short
```

## 本次测试证据文件

- `output/testday-workout-2026-08-20.json` / `output/testday-notion-2026-08-20.json` / `output/testday-resume-2026-08-20.json`（本地，`output/` 已忽略）
- 运行数据库 `data/runs/runs.sqlite3`（本地，已忽略）
