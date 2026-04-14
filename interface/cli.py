from __future__ import annotations

import json

from app.core.config import Settings
from app.ingest.metadata import ingest_project
from app.index.retriever import HybridRetriever
from app.query.executor import StrictEvidenceExecutor
from app.query.prompt_builder import build_prompt
from app.query.response_formatter import INSUFFICIENT_EVIDENCE, format_response
from app.validation.validator import ResponseValidator


def query_once(query: str, settings: Settings | None = None) -> dict[str, object]:
    resolved_settings = settings or Settings()
    chunks = ingest_project(resolved_settings)
    retriever = HybridRetriever(resolved_settings, chunks)
    retriever.persist()
    retrieved = retriever.retrieve(query)
    prompt = build_prompt(resolved_settings, query, retrieved)
    response = StrictEvidenceExecutor().execute(query, retrieved, prompt)
    if not ResponseValidator().validate(response, [item.chunk for item in retrieved]):
        return INSUFFICIENT_EVIDENCE.copy()
    return format_response(response)


def run_cli() -> None:
    settings = Settings()
    print("Evidence-only query system ready. Type 'exit' to quit.")
    while True:
        query = input("query> ").strip()
        if query.lower() in {"exit", "quit"}:
            break
        print(json.dumps(query_once(query, settings), indent=2))
