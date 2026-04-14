from __future__ import annotations

from pathlib import Path

from app.core.config import Settings


def iter_supported_files(settings: Settings) -> list[Path]:
    files: list[Path] = []
    for path in settings.raw_data_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in settings.supported_suffixes:
            continue
        files.append(path)
    return sorted(files)


def read_text_file(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="ignore").splitlines()
