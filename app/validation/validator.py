from __future__ import annotations

from app.core.types import EvidenceChunk, QueryResponse
from app.validation.rules import claim_has_source, claim_matches_evidence


class ResponseValidator:
    def validate(self, response: QueryResponse, chunks: list[EvidenceChunk]) -> bool:
        chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        for fact in response.verified_facts:
            if not claim_has_source(fact):
                return False
            if not claim_matches_evidence(fact, chunks_by_id):
                return False
        return True
