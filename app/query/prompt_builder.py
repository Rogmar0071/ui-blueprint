from __future__ import annotations

from pathlib import Path

from app.core.config import Settings
from app.core.types import RetrievedChunk


def _load_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def build_prompt(settings: Settings, query: str, chunks: list[RetrievedChunk]) -> str:
    prompts_dir = settings.repo_root / "prompts"
    system_prompt = _load_prompt(prompts_dir / "system.txt")
    query_template = _load_prompt(prompts_dir / "query.txt")
    serialized_chunks = "\n\n".join(
        (
            f"[chunk_id={item.chunk.chunk_id}]\n"
            f"path: {item.chunk.file_path}\n"
            f"lines: {item.chunk.start_line}-{item.chunk.end_line}\n"
            f"text:\n{item.chunk.text}"
        )
        for item in chunks
    )
    return "\n\n".join(
        [
            system_prompt,
            query_template.format(query=query, evidence_chunks=serialized_chunks or "<no evidence retrieved>"),
        ]
    )
