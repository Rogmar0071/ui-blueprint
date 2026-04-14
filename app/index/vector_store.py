from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path

from app.core.types import EvidenceChunk, RetrievedChunk

TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+")


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text)]


def build_vector_index(chunks: list[EvidenceChunk]) -> dict[str, Counter[str]]:
    return {chunk.chunk_id: Counter(tokenize(chunk.text)) for chunk in chunks}


def dump_vector_index(path: Path, index: dict[str, Counter[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({chunk_id: dict(counter) for chunk_id, counter in index.items()}, indent=2),
        encoding="utf-8",
    )


def cosine_score(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    numerator = sum(left[token] * right[token] for token in set(left) & set(right))
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


def search_vector_index(
    query: str,
    chunks: dict[str, EvidenceChunk],
    index: dict[str, Counter[str]],
    limit: int,
) -> list[RetrievedChunk]:
    query_vector = Counter(tokenize(query))
    scored = [
        RetrievedChunk(chunk=chunks[chunk_id], score=cosine_score(query_vector, counter), channels=("vector",))
        for chunk_id, counter in index.items()
    ]
    return [item for item in sorted(scored, key=lambda item: item.score, reverse=True)[:limit] if item.score > 0]
