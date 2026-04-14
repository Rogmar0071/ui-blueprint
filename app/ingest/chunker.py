from __future__ import annotations

from app.core.types import EvidenceChunk


def build_line_chunks(
    file_path: str,
    lines: list[str],
    chunk_size_lines: int,
) -> list[tuple[int, int, str]]:
    chunks: list[tuple[int, int, str]] = []
    for start in range(0, len(lines), chunk_size_lines):
        end = min(start + chunk_size_lines, len(lines))
        window = lines[start:end]
        text = "\n".join(window).strip()
        if not text:
            continue
        chunks.append((start + 1, end, text))
    return chunks


def hydrate_chunks(
    file_path: str,
    raw_chunks: list[tuple[int, int, str]],
    hasher: callable,
) -> list[EvidenceChunk]:
    hydrated: list[EvidenceChunk] = []
    for start_line, end_line, text in raw_chunks:
        chunk_id = hasher(f"{file_path}:{start_line}:{end_line}:{text}")
        hydrated.append(
            EvidenceChunk(
                chunk_id=chunk_id,
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
                text=text,
                sha256=hasher(text),
            )
        )
    return hydrated
