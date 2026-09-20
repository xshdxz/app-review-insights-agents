"""HTTP 契约层（可选 extra：`pip install -e ".[api]"`）。"""

from app_review_insights.api.app import API_VERSION, create_app

__all__ = ["API_VERSION", "create_app"]
