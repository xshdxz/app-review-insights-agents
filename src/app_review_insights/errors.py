class AppReviewInsightsError(Exception):
    """应用错误基类。"""


class InputDataError(AppReviewInsightsError):
    """输入链接或导入数据无效。"""


class CollectionError(AppReviewInsightsError):
    """在线采集失败或返回的数据不可用。"""


class RecoverableModelError(AppReviewInsightsError):
    """模型阶段失败，但可以从已保存的检查点继续。"""
