"""向量检索底座：OpenAI 兼容 embeddings / 本地 sentence-transformers + 纯 Python 余弦相似度。

未配置 embedding 时系统退化为纯 FTS5 检索（见 retrieval.py）。
"""

from __future__ import annotations

from typing import Any


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError(
            f"向量维度不匹配：{len(a)} != {len(b)}（请检查 EMBEDDING_MODEL 是否与索引时一致）"
        )
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class EmbeddingStore:
    """调用 OpenAI 兼容 /embeddings 端点并为语料生成向量。"""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        client: Any | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        if client is None:
            from openai import OpenAI

            self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=30, max_retries=1)
        else:
            self.client = client

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        response = self.client.embeddings.create(model=self.model, input=texts)
        ordered = sorted(response.data, key=lambda item: item.index)
        return [item.embedding for item in ordered]


class LocalEmbeddingStore:
    """本地 sentence-transformers 模型生成向量（无需 API Key，离线可用）。"""

    def __init__(self, model_path: str):
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(model_path)

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors = self.model.encode(texts)
        return [vec.tolist() for vec in vectors]
