"""文本向量化：默认零依赖，可切换为 BGE-M3。

项目正式环境应使用 BGE-M3（中文术语召回明显更准），
但在没有 GPU / 没装 sentence-transformers 的机器上，链路也必须能跑通和自测，
所以默认用确定性哈希向量（字符 n-gram），效果弱但完全离线、可复现。
"""

from __future__ import annotations

import math
import os
import re
from typing import Protocol, Sequence

_TOKEN = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]")


class Embedder(Protocol):
    dim: int

    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """字符 2-gram / 3-gram 哈希到固定维度，L2 归一化。"""

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def _grams(self, text: str) -> list[str]:
        text = text.lower().strip()
        chars = [t for t in _TOKEN.findall(text) if t.strip()]
        grams = list(chars)
        for n in (2, 3):
            for i in range(len(chars) - n + 1):
                grams.append("".join(chars[i:i + n]))
        return grams

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vec = [0.0] * self.dim
            for g in self._grams(text):
                h = _stable_hash(g) % self.dim
                vec[h] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors


class BGEEmbedder:
    """可选：BGE-M3。首次使用时才加载模型，避免没装依赖时导入失败。"""

    def __init__(self, model_name: str = "BAAI/bge-m3") -> None:
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415

        self._model = SentenceTransformer(model_name)
        self.dim = self._model.get_sentence_embedding_dimension()

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        return self._model.encode(list(texts), normalize_embeddings=True).tolist()


def _stable_hash(text: str) -> int:
    """跨进程稳定的哈希（内置 hash() 受 PYTHONHASHSEED 影响，缓存会失效）。"""
    h = 2166136261
    for ch in text:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def get_embedder() -> Embedder:
    """默认哈希向量；只有显式 EMBEDDER=bge 时才加载 BGE-M3。

    不要让默认路径去 import / 下载模型：服务启动会被网络卡住，
    Docker 镜像里也没有模型权重。生产部署时再挂载模型并设置环境变量。
    """
    backend = os.getenv("EMBEDDER", "hashing").lower()
    if backend == "bge":
        try:
            return BGEEmbedder(os.getenv("EMBEDDER_MODEL", "BAAI/bge-m3"))
        except Exception as exc:  # noqa: BLE001 - 缺依赖/缺模型都降级
            print(f"[embeddings] BGE 不可用，降级到哈希向量：{exc}")
    return HashingEmbedder()
