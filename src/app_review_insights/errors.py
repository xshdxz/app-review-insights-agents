class AppReviewInsightsError(Exception):
    """应用错误基类。"""


class InputDataError(AppReviewInsightsError):
    """输入链接或导入数据无效。"""


class CollectionError(AppReviewInsightsError):
    """在线采集失败或返回的数据不可用。"""


class RecoverableModelError(AppReviewInsightsError):
    """模型阶段失败，但可以从已保存的检查点继续。"""


class ModelBudgetExceeded(RecoverableModelError):
    """模型费用达到预算上限。

    刻意继承 `RecoverableModelError`：预算用尽时必须停在检查点上，
    已完成的工作全部保留，调高预算后可用同一 `run_id` 续跑，
    而不是把进度丢掉。
    """
