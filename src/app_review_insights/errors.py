class AppReviewInsightsError(Exception):
    """应用错误基类。"""


class InputDataError(AppReviewInsightsError):
    """输入链接或导入数据无效。"""


class CollectionError(AppReviewInsightsError):
    """在线采集失败或返回的数据不可用。"""


class RecoverableModelError(AppReviewInsightsError):
    """模型阶段失败，但可以从已保存的检查点继续。"""


class RunDeadlineExceeded(AppReviewInsightsError):
    """整轮运行超过配置的墙钟上限。

    单次调用早有超时（模型 60s、采集 20s、Webhook 15s），但整轮运行此前没有上限：
    输入异常大或模型持续变慢时，一次分析可以跑上几小时没人察觉。
    停在检查点上，调高上限即可续跑。
    """


class ConcurrentRunError(AppReviewInsightsError):
    """同一 App 已有进行中的分析运行。

    不是数据格式问题，而是**白花钱**问题：并发分析各烧一遍模型额度，
    得到的还是同一份结论。属于可展示给用户的前置条件失败。
    """


class ModelBudgetExceeded(RecoverableModelError):
    """模型费用达到预算上限。

    刻意继承 `RecoverableModelError`：预算用尽时必须停在检查点上，
    已完成的工作全部保留，调高预算后可用同一 `run_id` 续跑，
    而不是把进度丢掉。
    """
