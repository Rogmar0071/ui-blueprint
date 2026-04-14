from __future__ import annotations

from app.core.types import QueryResponse, RetrievedChunk, VerifiedFact
from app.index.vector_store import tokenize

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "do",
    "does",
    "for",
    "how",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "the",
    "to",
    "what",
    "when",
    "where",
    "which",
    "who",
}


class StrictEvidenceExecutor:
    def execute(self, query: str, chunks: list[RetrievedChunk], prompt: str) -> QueryResponse:
        del prompt
        if not chunks:
            return QueryResponse(unknowns=["UNKNOWN"])

        query_terms = [token for token in tokenize(query) if token not in STOPWORDS]
        if not query_terms:
            return QueryResponse(required_inputs=["Provide a more specific, project-bounded query."])

        facts: list[tuple[int, VerifiedFact]] = []
        for item in chunks:
            for line in item.chunk.text.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                overlap = len(set(tokenize(stripped)) & set(query_terms))
                if overlap <= 0:
                    continue
                facts.append(
                    (
                        overlap,
                        VerifiedFact(
                            claim=stripped,
                            source_chunk_ids=[item.chunk.chunk_id],
                            quote=stripped,
                            file_path=item.chunk.file_path,
                            start_line=item.chunk.start_line,
                            end_line=item.chunk.end_line,
                        ),
                    )
                )

        if not facts:
            return QueryResponse(unknowns=["UNKNOWN"])

        ordered_facts: list[VerifiedFact] = []
        seen_claims: set[tuple[str, str]] = set()
        for _, fact in sorted(facts, key=lambda item: item[0], reverse=True):
            key = (fact.file_path, fact.claim)
            if key in seen_claims:
                continue
            seen_claims.add(key)
            ordered_facts.append(fact)
            if len(ordered_facts) >= 5:
                break

        return QueryResponse(verified_facts=ordered_facts)
