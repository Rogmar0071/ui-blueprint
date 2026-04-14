from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class EvidenceChunk:
    chunk_id: str
    file_path: str
    start_line: int
    end_line: int
    text: str
    sha256: str


@dataclass(slots=True)
class RetrievedChunk:
    chunk: EvidenceChunk
    score: float
    channels: tuple[str, ...] = ()


@dataclass(slots=True)
class VerifiedFact:
    claim: str
    source_chunk_ids: list[str]
    quote: str
    file_path: str
    start_line: int
    end_line: int


@dataclass(slots=True)
class QueryResponse:
    verified_facts: list[VerifiedFact] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    invalid_scope: list[str] = field(default_factory=list)
    required_inputs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified_facts": [
                {
                    "claim": fact.claim,
                    "source_chunk_ids": fact.source_chunk_ids,
                    "quote": fact.quote,
                    "file_path": fact.file_path,
                    "start_line": fact.start_line,
                    "end_line": fact.end_line,
                }
                for fact in self.verified_facts
            ],
            "unknowns": self.unknowns,
            "invalid_scope": self.invalid_scope,
            "required_inputs": self.required_inputs,
        }
