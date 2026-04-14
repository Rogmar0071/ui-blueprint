from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.core.config import Settings
from app.core.types import EvidenceChunk
from app.ingest.chunker import build_line_chunks, hydrate_chunks
from app.ingest.parser import iter_supported_files, read_text_file


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def ingest_project(settings: Settings) -> list[EvidenceChunk]:
    settings.processed_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[EvidenceChunk] = []
    for path in iter_supported_files(settings):
        relative_path = str(path.relative_to(settings.repo_root))
        raw_chunks = build_line_chunks(relative_path, read_text_file(path), settings.chunk_size_lines)
        chunks.extend(hydrate_chunks(relative_path, raw_chunks, sha256_text))
    write_chunk_manifest(settings.chunk_manifest_path, chunks)
    return chunks


def write_chunk_manifest(path: Path, chunks: list[EvidenceChunk]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(
                json.dumps(
                    {
                        "chunk_id": chunk.chunk_id,
                        "file_path": chunk.file_path,
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                        "text": chunk.text,
                        "sha256": chunk.sha256,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
