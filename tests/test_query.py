from __future__ import annotations

from pathlib import Path

from app.core.config import Settings
from interface.cli import query_once


def test_query_returns_verified_facts_when_evidence_matches(tmp_path: Path) -> None:
    repo_root = tmp_path
    raw_dir = repo_root / "data" / "raw"
    raw_dir.mkdir(parents=True)
    (repo_root / "prompts").mkdir(parents=True)
    (repo_root / "prompts" / "system.txt").write_text("strict", encoding="utf-8")
    (repo_root / "prompts" / "query.txt").write_text("{query}\n{evidence_chunks}", encoding="utf-8")
    (repo_root / "prompts" / "validator.txt").write_text("validator", encoding="utf-8")
    (raw_dir / "engine.py").write_text(
        "class EventEngine:\n    \"\"\"Coordinates event playback.\"\"\"\n",
        encoding="utf-8",
    )

    response = query_once(
        "What does EventEngine do?",
        Settings(repo_root=repo_root, chunk_size_lines=10),
    )

    assert response["status"] == "OK"
    assert response["verified_facts"]
    fact = response["verified_facts"][0]
    assert fact["claim"] == "class EventEngine:"
    assert fact["source_chunk_ids"]
    assert fact["quote"] == "class EventEngine:"
    assert fact["file_path"] == "data/raw/engine.py"
    assert fact["start_line"] == 1
    assert fact["end_line"] == 2


def test_query_returns_unknown_when_no_evidence_matches(tmp_path: Path) -> None:
    repo_root = tmp_path
    raw_dir = repo_root / "data" / "raw"
    raw_dir.mkdir(parents=True)
    (repo_root / "prompts").mkdir(parents=True)
    (repo_root / "prompts" / "system.txt").write_text("strict", encoding="utf-8")
    (repo_root / "prompts" / "query.txt").write_text("{query}\n{evidence_chunks}", encoding="utf-8")
    (repo_root / "prompts" / "validator.txt").write_text("validator", encoding="utf-8")
    (raw_dir / "notes.txt").write_text("Only deployment notes live here.\n", encoding="utf-8")

    response = query_once(
        "What does EventEngine do?",
        Settings(repo_root=repo_root, chunk_size_lines=10),
    )

    assert response["status"] == "OK"
    assert response["verified_facts"] == []
    assert response["unknowns"] == ["UNKNOWN"]
