from __future__ import annotations

from app.core.types import EvidenceChunk, QueryResponse, VerifiedFact
from app.validation.validator import ResponseValidator


def test_validator_rejects_claim_without_source() -> None:
    chunk = EvidenceChunk(
        chunk_id="c1",
        file_path="data/raw/sample.txt",
        start_line=1,
        end_line=1,
        text="EventEngine coordinates event playback.",
        sha256="hash",
    )
    response = QueryResponse(
        verified_facts=[
            VerifiedFact(
                claim="EventEngine coordinates event playback.",
                source_chunk_ids=[],
                quote="EventEngine coordinates event playback.",
                file_path=chunk.file_path,
                start_line=1,
                end_line=1,
            )
        ]
    )

    assert ResponseValidator().validate(response, [chunk]) is False


def test_validator_rejects_inferred_claim() -> None:
    chunk = EvidenceChunk(
        chunk_id="c1",
        file_path="data/raw/sample.txt",
        start_line=1,
        end_line=1,
        text="EventEngine coordinates event playback.",
        sha256="hash",
    )
    response = QueryResponse(
        verified_facts=[
            VerifiedFact(
                claim="EventEngine automates every workflow.",
                source_chunk_ids=["c1"],
                quote="EventEngine coordinates event playback.",
                file_path=chunk.file_path,
                start_line=1,
                end_line=1,
            )
        ]
    )

    assert ResponseValidator().validate(response, [chunk]) is False
