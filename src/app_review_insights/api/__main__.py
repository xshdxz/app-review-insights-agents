"""\`python -m app_review_insights.api\`：起 HTTP 服务。

绑定地址默认 127.0.0.1 —— 一个能提交分析（要花钱）的服务不该默认监听所有网卡。
容器里要对外时必须显式给 \`ARI_API_HOST=0.0.0.0\`。
"""

from __future__ import annotations

import os

import uvicorn

from app_review_insights.api import create_app


def main() -> None:
    uvicorn.run(
        create_app(),
        host=os.environ.get("ARI_API_HOST", "127.0.0.1"),
        port=int(os.environ.get("ARI_API_PORT", "8000")),
        log_level=os.environ.get("ARI_API_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
