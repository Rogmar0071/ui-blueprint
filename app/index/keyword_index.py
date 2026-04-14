from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from app.core.types import EvidenceChunk, RetrievedChunk
from app.index.vector_store import tokenize


def build_keyword_index(chunks: list[EvidenceChunk]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = defaultdict(list)
    for chunk in chunks:
        for token in sorted(set(tokenize(chunk.text))):
            index[token].append(chunk.chunk_id)
    return dict(index)


def dump_keyword_index(path: Path, index: dict[str, list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, indent=2), encoding="utf-8")


def search_keyword_index(
    query: str,
    chunks: dict[str, EvidenceChunk],
    index: dict[str, list[str]],
    limit: int,
) -> list[RetrievedChunk]:
    scores: dict[str, float] = defaultdict(float)
    for token in tokenize(query):
        for chunk_id in index.get(token, []):
            scores[chunk_id] += 1.0
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
    return [
        RetrievedChunk(chunk=chunks[chunk_id], score=score, channels=("keyword",))
        for chunk_id, score in ranked
        if score > 0
    ]
