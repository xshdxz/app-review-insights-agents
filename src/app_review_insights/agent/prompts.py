import json

PLANNER_SYSTEM_PROMPT = (
    "你是产品情报系统的规划 Agent。任务：根据用户的分析目标，从可用工具中选择并"
    "编排工具调用计划。规则：\n"
    "1. 必须且只能调用一个 run_analysis（证据必须来自确定性流水线）。\n"
    "2. 可在 run_analysis 之后补充 query_corpus（对已建语料追问）或 get_latest_report。\n"
    "3. 输出 JSON：{rationale: 简短理由, tool_calls: [{tool, arguments}]}。\n"
    "4. 不确定时只输出 run_analysis 一个调用。"
)


def render_planner_user_prompt(goal: str, app_url: str, tool_schemas: dict) -> str:
    return (
        f"分析目标：{goal}\n"
        f"App 链接：{app_url}\n"
        f"可用工具：{json.dumps(tool_schemas, ensure_ascii=False)}\n"
    )
