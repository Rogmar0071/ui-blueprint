from __future__ import annotations

from app.core.types import EvidenceChunk, VerifiedFact


def claim_has_source(fact: VerifiedFact) -> bool:
    return bool(fact.source_chunk_ids)


def claim_matches_evidence(fact: VerifiedFact, chunks_by_id: dict[str, EvidenceChunk]) -> bool:
    for chunk_id in fact.source_chunk_ids:
        chunk = chunks_by_id.get(chunk_id)
        if chunk is None:
            return False
        if fact.quote not in chunk.text:
            return False
        if fact.claim != fact.quote:
            return False
    return True
