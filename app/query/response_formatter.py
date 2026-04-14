from __future__ import annotations

from app.core.types import QueryResponse


INSUFFICIENT_EVIDENCE = {
    "status": "INSUFFICIENT EVIDENCE",
    "verified_facts": [],
    "unknowns": ["UNKNOWN"],
    "invalid_scope": [],
    "required_inputs": [],
}


def format_response(response: QueryResponse) -> dict[str, object]:
    payload = response.to_dict()
    payload["status"] = "OK"
    return payload
