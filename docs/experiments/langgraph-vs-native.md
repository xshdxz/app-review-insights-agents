# LangGraph vs 原生编排：归并子流程对比（实验记录）

- 日期：2026-08-22
- 方法：同一批 fixture 数据（5 条发现，含标题重复和缺证据项），分别用 LangGraph 状态图与原生 `validate_finding_drafts` 跑归并+校验
- 指标：输出一致性、耗时、代码量、依赖成本

## 实验条件

- **Fixture**: `tests/fixtures/langgraph-findings.json` — 5 条发现（f1、f1-dup 重复标题、f2/f3/f4 独立），含各种边界（冲突证据、空引用）
- **LangGraph 版本**: 0.6.11（通过 `pip install -e ".[experiments]"` 安装）
- **运行环境**: Windows，Python 3.13.12，SQLite 3.51.1

## 运行结果

### LangGraph 图（`scripts/experiments/langgraph_consolidation.py`）

```
输出: 4 findings（f1 标题去重合并 + f2/f3/f4 保留）
耗时: 2.98ms
```

图结构：`consolidate（标题去重）→ validate（过滤无支持证据）→ END`

### 原生路径（`validate_finding_drafts`）

```
输入: 5 findings → 输出: 0 validated findings（10 个校验问题）
耗时: 0.08ms
```

原生校验器做了完整的语义检查：证据 ID 存在性、置信度合理性、支持/冲突计数一致性等，发现全部输入不满足规范化约束。

## 结论

| 维度 | LangGraph 图 | 原生路径 |
|---|---|---|
| **校验深度** | 仅标题去重 + 支持数过滤（浅层） | 完整语义校验（ID存在性、置信度、计数一致性） |
| **输出数量** | 4/5 通过（缺深层校验时高估） | 0/5 通过（严格规范化，无例外） |
| **耗时** | 2.98ms（含图编译 + 状态传播） | 0.08ms（纯函数调用） |
| **代码量** | ~68 行（TypedDict + StateGraph 构建） | ~76 行（validate.py，同一文件已有） |
| **依赖成本** | 新增 langgraph (~15 个依赖包，含 langchain-core 等) | 零新依赖 |

### 关键发现

1. **架构层面**：LangGraph 图结构（节点 + 边 + 状态传播）清晰表达了"先归并后校验"的两阶段流程，但该流程用普通函数组合已足够简单（原生代码不到 80 行），图的编译/调度开销反而引入了约 37 倍的延迟。
2. **校验深度差异**：原生路径通过 `validate_finding_drafts` 的 Pydantic 模型强制校验（`Finding` 的 `min_length=1` on supporting_review_ids、`confidence ge=0 le=1` 等），在数据入库前拦截不规范输入；LangGraph 版的 validate 节点只做了最基本的过滤（有无 supporting_review_ids），要达到同等校验深度需在节点内复用 Pydantic 校验，此时又回到了原生代码的功能。
3. **适用场景**：当流程需要动态分支（如模型输出决定走哪条边）、多 Agent 协作、或复杂条件路由时，LangGraph 的图结构有明显表达优势；当前的"先归并后校验"是确定性线性流程，原生状态机更合适。

### 工程决策

保持现有**确定性状态机核心 + Agent 外层**架构（`AgentOrchestrator` 作为 Planner→Tools→Reviewer 的编排层），在确定性核心内不引入 LangGraph；如果未来需要"动态决策图"（如 Planner 根据发现类型选择不同处理路径），可考虑在 Agent 层引入 LangGraph 而非改造核心。

## 后续可选

- 把图节点内的校验逻辑替换为 `Finding.model_validate` + `validate_finding_drafts`，公平对比同等校验深度下的性能差异
- 测试大规模 fixture（100+ findings）下的性能曲线
- 探索 LangGraph 的"条件边"特性（如 findings 需要补充证据时路由回采集节点）
