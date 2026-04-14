from __future__ import annotations

import json
from collections import Counter

from app.core.config import Settings
from app.core.types import EvidenceChunk, RetrievedChunk
from app.index.keyword_index import build_keyword_index, dump_keyword_index, search_keyword_index
from app.index.vector_store import build_vector_index, dump_vector_index, search_vector_index


class HybridRetriever:
    def __init__(self, settings: Settings, chunks: list[EvidenceChunk]) -> None:
        self.settings = settings
        self.chunks = chunks
        self.chunk_map = {chunk.chunk_id: chunk for chunk in chunks}
        self.keyword_index = build_keyword_index(chunks)
        self.vector_index = build_vector_index(chunks)

    def persist(self) -> None:
        dump_keyword_index(self.settings.keyword_index_path, self.keyword_index)
        dump_vector_index(self.settings.vector_index_path, self.vector_index)
        self.settings.index_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "chunk_count": len(self.chunks),
            "chunk_ids": [chunk.chunk_id for chunk in self.chunks],
        }
        (self.settings.index_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2),
            encoding="utf-8",
        )

    def retrieve(self, query: str, limit: int | None = None) -> list[RetrievedChunk]:
        top_k = limit or self.settings.top_k
        vector_hits = search_vector_index(query, self.chunk_map, self.vector_index, top_k)
        keyword_hits = search_keyword_index(query, self.chunk_map, self.keyword_index, top_k)

        combined_scores: dict[str, float] = Counter()
        channels: dict[str, set[str]] = {}
        for hit in vector_hits + keyword_hits:
            combined_scores[hit.chunk.chunk_id] += hit.score
            channels.setdefault(hit.chunk.chunk_id, set()).update(hit.channels)

        ranked = sorted(combined_scores.items(), key=lambda item: item[1], reverse=True)[:top_k]
        return [
            RetrievedChunk(
                chunk=self.chunk_map[chunk_id],
                score=score,
                channels=tuple(sorted(channels.get(chunk_id, set()))),
            )
            for chunk_id, score in ranked
        ]
