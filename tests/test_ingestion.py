from __future__ import annotations

import json
from pathlib import Path

from app.core.config import Settings
from app.ingest.metadata import ingest_project


def test_ingestion_writes_chunk_metadata(tmp_path: Path) -> None:
    repo_root = tmp_path
    raw_dir = repo_root / "data" / "raw"
    raw_dir.mkdir(parents=True)
    (raw_dir / "sample.py").write_text("def EventEngine():\n    return 'ok'\n", encoding="utf-8")

    settings = Settings(repo_root=repo_root, chunk_size_lines=2)
    chunks = ingest_project(settings)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.file_path == "data/raw/sample.py"
    assert chunk.start_line == 1
    assert chunk.end_line == 2
    assert chunk.sha256

    written = settings.chunk_manifest_path.read_text(encoding="utf-8").strip().splitlines()
    payload = json.loads(written[0])
    assert payload["file_path"] == "data/raw/sample.py"
    assert payload["start_line"] == 1
    assert payload["end_line"] == 2
