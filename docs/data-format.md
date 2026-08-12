# 评论导入格式

应用支持 JSON 和 CSV 两种评论导入格式，文件必须使用 UTF-8 编码。

## 必需语义字段

- 评论正文：`content`、`content_original` 或 `review`。
- 评分：`rating`，取值为 1–5。
- 发布时间：`published_at`、`date` 或 `updated`，使用 ISO 8601 格式。

## 推荐字段

- 评论 ID：`review_id` 或 `id`。
- 标题：`title`。
- App 版本：`app_version` 或 `version`。
- 作者：`author`、`userName` 或 `username`。
- 语言：`language`。
- 商店地区：`storefront`，默认值为 `us`。

## JSON 示例

JSON 顶层可以直接是数组，也可以是包含 `reviews` 数组的对象：

```json
{
  "reviews": [
    {
      "review_id": "r-001",
      "content": "The trial price is unclear.",
      "rating": 2,
      "published_at": "2026-08-01T10:00:00Z",
      "app_version": "8.5.0"
    }
  ]
}
```

## CSV 示例

CSV 使用相同字段名作为表头：

```csv
review_id,content,rating,published_at,title
r-002,Workout timer freezes,1,2026-08-02T10:00:00Z,Timer bug
```
