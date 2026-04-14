from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class Settings:
    repo_root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2])
    raw_data_dir: Path | None = None
    processed_dir: Path | None = None
    index_dir: Path | None = None
    chunk_size_lines: int = 20
    top_k: int = 5
    supported_suffixes: tuple[str, ...] = (
        ".md",
        ".txt",
        ".py",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".xml",
        ".kt",
        ".java",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".html",
        ".css",
        ".sql",
        ".sh",
    )

    def __post_init__(self) -> None:
        data_dir = self.repo_root / "data"
        if self.raw_data_dir is None:
            self.raw_data_dir = data_dir / "raw"
        if self.processed_dir is None:
            self.processed_dir = data_dir / "processed"
        if self.index_dir is None:
            self.index_dir = data_dir / "index"

    @property
    def chunk_manifest_path(self) -> Path:
        return self.processed_dir / "chunks.jsonl"

    @property
    def keyword_index_path(self) -> Path:
        return self.index_dir / "keyword_index.json"

    @property
    def vector_index_path(self) -> Path:
        return self.index_dir / "vector_index.json"
