from __future__ import annotations

import io
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, ValidationError

from app_review_insights.errors import InputDataError
from app_review_insights.models import Review


class ParsedAppUrl(BaseModel):
    country: str
    slug: str
    app_id: str


_APP_URL = re.compile(
    r"^https://apps\.apple\.com/(?P<country>[a-z]{2})/app/"
    r"(?P<slug>[^/]+)/id(?P<app_id>\d+)(?:[/?].*)?$",
    re.IGNORECASE,
)


def parse_app_store_url(url: str) -> ParsedAppUrl:
    match = _APP_URL.match(url.strip())
    if not match:
        raise InputDataError("请输入有效的 App Store 应用链接")

    parsed = ParsedAppUrl(**match.groupdict())
    if parsed.country.lower() != "us":
        raise InputDataError("在线评论分析仅接受美国区 App Store 链接")
    return parsed.model_copy(update={"country": parsed.country.lower()})


def _records_from_bytes(data: bytes, filename: str) -> list[dict[str, Any]]:
    suffix = Path(filename).suffix.lower()
    try:
        if suffix == ".json":
            decoded = json.loads(data.decode("utf-8-sig"))
            records = decoded.get("reviews", []) if isinstance(decoded, dict) else decoded
            if not isinstance(records, list):
                raise InputDataError("JSON 顶层必须是数组或包含 reviews 数组")
            if not all(isinstance(record, dict) for record in records):
                raise InputDataError("JSON reviews 数组中的每一项都必须是对象")
            return records
        if suffix == ".csv":
            return pd.read_csv(io.BytesIO(data)).where(pd.notna, None).to_dict("records")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputDataError(f"无法解析 {suffix[1:].upper()} 文件：{exc}") from exc
    except pd.errors.ParserError as exc:
        raise InputDataError(f"无法解析 CSV 文件：{exc}") from exc

    raise InputDataError("仅支持 .json 和 .csv 文件")


def _first(record: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if record.get(key) not in (None, ""):
            return record[key]
    return default


def _require(record: dict[str, Any], index: int, label: str, *keys: str) -> Any:
    value = _first(record, *keys)
    if value in (None, ""):
        raise InputDataError(f"第 {index} 条评论缺少 {label}")
    return value


def _parse_datetime(value: Any, index: int) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise InputDataError(f"第 {index} 条评论的日期不是有效 ISO 8601 格式") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_rating(value: Any, index: int) -> int:
    if isinstance(value, bool):
        raise InputDataError(f"第 {index} 条评论的 rating 必须是 1–5 的整数")

    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise InputDataError(f"第 {index} 条评论的 rating 必须是 1–5 的整数") from exc

    if not numeric.is_integer():
        raise InputDataError(f"第 {index} 条评论的 rating 必须是 1–5 的整数")
    return int(numeric)


def import_reviews(data: bytes, filename: str, app_id: str) -> list[Review]:
    reviews: list[Review] = []
    suffix = Path(filename).suffix.lower()
    for index, record in enumerate(_records_from_bytes(data, filename), start=1):
        content = _require(record, index, "content", "content_original", "content", "review")
        rating = _require(record, index, "rating", "rating")
        published = _require(record, index, "published_at/date", "published_at", "date", "updated")

        try:
            reviews.append(
                Review(
                    review_id=str(
                        _first(record, "review_id", "id", default=f"import-{index}")
                    ),
                    app_id=app_id,
                    storefront=str(_first(record, "storefront", default="us")),
                    title=str(_first(record, "title", default="")),
                    content_original=str(content),
                    rating=_parse_rating(rating, index),
                    app_version=_first(record, "app_version", "version"),
                    author=_first(record, "author", "userName", "username"),
                    published_at=_parse_datetime(published, index),
                    language=_first(record, "language"),
                    source=f"import:{suffix[1:]}",
                )
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise InputDataError(f"第 {index} 条评论字段无效：{exc}") from exc

    if not reviews:
        raise InputDataError("导入文件中没有评论")
    return reviews
