"""
backend.app.tool_routes
=======================
FastAPI router for backend tool endpoints used by the AI assistant.

Endpoints
---------
POST /v1/tools/web_search          Perform a web search via Tavily and return results.
GET  /v1/global/messages           Alias for GET /api/chat (newest-first global history).
POST /v1/global/messages/{id}/edit Alias for POST /api/chat/{id}/edit.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.app.auth import require_auth
from backend.app.web_search import TavilyKeyMissing, web_search

router = APIRouter()

_tools_prefix = APIRouter(prefix="/v1/tools")
_global_prefix = APIRouter(prefix="/v1/global")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class WebSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    recency_days: int | None = Field(default=None, ge=1, le=365)
    max_results: int = Field(default=5, ge=1, le=20)


class WebSearchResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    title: str
    url: str
    snippet: str
    published_at: str | None = None
    source: str | None = None


class WebSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[WebSearchResult]
    provider: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


# ---------------------------------------------------------------------------
# POST /v1/tools/web_search
# ---------------------------------------------------------------------------


@_tools_prefix.post("/web_search", status_code=200, dependencies=[Depends(require_auth)])
def post_web_search(body: dict[str, Any]) -> JSONResponse:
    """
    Perform a web search via Tavily.

    Returns 503 if TAVILY_API_KEY is not configured.

    Request body::

        {
            "query": "latest AI news",
            "recency_days": 7,   // optional, 1-365
            "max_results": 5     // optional, 1-20, default 5
        }

    Response::

        {
            "results": [
                {
                    "title": "...",
                    "url": "...",
                    "snippet": "...",
                    "published_at": "2025-01-01" | null,
                    "source": "example.com"
                },
                ...
            ],
            "provider": "tavily"
        }
    """
    try:
        req = WebSearchRequest.model_validate(body or {})
    except ValidationError:
        return _error(422, "invalid_request", "Request body failed validation.")

    if not req.query.strip():
        return _error(400, "invalid_request", "query must not be empty.")

    try:
        raw = web_search(
            req.query,
            recency_days=req.recency_days,
            max_results=req.max_results,
        )
    except TavilyKeyMissing:
        return _error(
            503,
            "tavily_key_missing",
            "TAVILY_API_KEY is not configured. Set this environment variable to enable web search.",
        )

    response = WebSearchResponse(
        results=[WebSearchResult(**r) for r in raw.get("results", [])],
        provider=raw.get("provider", "tavily"),
    )
    return JSONResponse(content=response.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# GET /v1/global/messages  (alias for GET /api/chat)
# ---------------------------------------------------------------------------


@_global_prefix.get("/messages", status_code=200, dependencies=[Depends(require_auth)])
def get_global_messages() -> JSONResponse:
    """
    Return persisted global chat history (newest-first).

    This is an alias for GET /api/chat that follows the /v1/global/ namespace.
    """
    from backend.app.chat_routes import list_chat_messages

    return list_chat_messages()


# ---------------------------------------------------------------------------
# POST /v1/global/messages/{message_id}/edit  (alias for POST /api/chat/{id}/edit)
# ---------------------------------------------------------------------------


@_global_prefix.post(
    "/messages/{message_id}/edit",
    status_code=201,
    dependencies=[Depends(require_auth)],
)
def edit_global_message(message_id: str, body: dict[str, Any]) -> JSONResponse:
    """
    Edit a user message by creating a new message that supersedes the original.

    This is an alias for POST /api/chat/{message_id}/edit that follows the
    /v1/global/ namespace.
    """
    from backend.app.chat_routes import edit_chat_message

    return edit_chat_message(message_id, body)


# ---------------------------------------------------------------------------
# Register sub-routers
# ---------------------------------------------------------------------------

router.include_router(_tools_prefix)
router.include_router(_global_prefix)
