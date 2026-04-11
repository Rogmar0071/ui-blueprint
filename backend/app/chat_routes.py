"""
backend.app.chat_routes
========================
FastAPI router for the global AI chat endpoints.

Endpoints
---------
GET  /api/chat                               list persisted chat history (newest-first)
POST /api/chat                               send a message and persist it
POST /api/chat/{message_id}/edit             create an edited user message (supersedes original)
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, Request as FastAPIRequest
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlmodel import Session, select

from backend.app.auth import require_auth
from ui_blueprint.domain.ir import SCHEMA_VERSION
from ui_blueprint.domain.openai_provider import _build_completions_url

router = APIRouter(prefix="/api")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MODEL_CHAT = "gpt-4.1-mini"
_DEFAULT_BASE_URL = "https://api.openai.com"
_DEFAULT_TIMEOUT = 30.0
_GLOBAL_CHAT_HISTORY_LIMIT = 20

_TOOLS_AVAILABLE = [
    "domains.derive",
    "domains.confirm",
    "blueprints.compile",
    "sessions.create",
    "sessions.status",
    "web_search",
]

_CHAT_SYSTEM_PROMPT = (
    "You are UI Blueprint Assistant, a helpful AI that guides users through "
    "the ui-blueprint pipeline: recording screen clips, deriving domain profiles, "
    "confirming them, and compiling blueprints. "
    "Be concise and practical."
)

_OPS_CONTEXT_HEADER = (
    "\n\n--- Recent system activity (last {n} ops events) ---\n"
    "{snippet}\n"
    "--- End of system activity ---"
)

# Keywords that indicate the user wants up-to-date / current information.
_RECENCY_PATTERN = re.compile(
    r"\b(latest|current|today|now|recent|news|price|release|ceo|just|trending|"
    r"this week|this month|right now|up.?to.?date|happening)\b",
    re.IGNORECASE,
)
_SEARCH_PREFIX = "search:"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ChatContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None
    domain_profile_id: str | None = None


class ChatMessageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: str
    context: ChatContext = Field(default_factory=ChatContext)
    superseded: bool = False


class ChatHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    messages: list[ChatMessageResponse]
    tools_available: list[str]


class ChatPostRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    context: ChatContext = Field(default_factory=ChatContext)
    agent_mode: bool = False

    @field_validator("message")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("message is required and must not be empty.")
        return text


class ChatPostResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    reply: str
    tools_available: list[str]
    user_message: ChatMessageResponse
    assistant_message: ChatMessageResponse


class ChatEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("content is required and must not be empty.")
        return text


class ChatEditResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    original_message: ChatMessageResponse
    new_message: ChatMessageResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_response(model: BaseModel, status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=model.model_dump(mode="json"))


def _error(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=body)


def _stub_reply(message: str) -> str:
    return (
        f"[Stub] You said: {message!r}. "
        "AI features are not enabled — set OPENAI_API_KEY on the server to activate them."
    )


def _db_session() -> Session | None:
    try:
        from backend.app.database import get_engine
    except RuntimeError:
        return None

    try:
        return Session(get_engine())
    except RuntimeError:
        return None


def _message_to_response(message: Any) -> ChatMessageResponse:
    created_at = message.created_at
    if isinstance(created_at, datetime):
        created_at_str = created_at.isoformat()
    else:
        created_at_str = str(created_at)
    return ChatMessageResponse(
        id=str(message.id),
        role=message.role,
        content=message.content,
        created_at=created_at_str,
        context=ChatContext(
            session_id=getattr(message, "session_id", None),
            domain_profile_id=getattr(message, "domain_profile_id", None),
        ),
        superseded=getattr(message, "superseded_by_id", None) is not None,
    )


def _new_ephemeral_message(
    role: Literal["user", "assistant", "system"],
    content: str,
    context: ChatContext,
) -> ChatMessageResponse:
    return ChatMessageResponse(
        id=str(uuid.uuid4()),
        role=role,
        content=content,
        created_at=datetime.now(timezone.utc).isoformat(),
        context=context,
    )


def _load_recent_history(db: Session | None) -> list[Any]:
    if db is None:
        return []

    from backend.app.models import GlobalChatMessage

    history = db.exec(
        select(GlobalChatMessage)
        .where(GlobalChatMessage.superseded_by_id == None)  # noqa: E711
        .order_by(GlobalChatMessage.created_at.desc())
        .limit(_GLOBAL_CHAT_HISTORY_LIMIT)
    ).all()
    return list(reversed(history))


def _list_persisted_messages(db: Session | None) -> list[Any]:
    if db is None:
        return []

    from backend.app.models import GlobalChatMessage

    return db.exec(
        select(GlobalChatMessage).order_by(GlobalChatMessage.created_at.desc())
    ).all()


def _persist_message(
    db: Session | None,
    role: Literal["user", "assistant", "system"],
    content: str,
    context: ChatContext,
) -> ChatMessageResponse:
    if db is None:
        return _new_ephemeral_message(role, content, context)

    from backend.app.models import GlobalChatMessage

    message = GlobalChatMessage(
        role=role,
        content=content,
        session_id=context.session_id,
        domain_profile_id=context.domain_profile_id,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return _message_to_response(message)


def _call_openai_chat(
    message: str,
    api_key: str,
    history: list[Any] | None = None,
    system_prompt: str | None = None,
) -> str:
    """Call OpenAI Chat Completions and return the assistant reply text."""
    model = os.environ.get("OPENAI_MODEL_CHAT", _DEFAULT_MODEL_CHAT)
    base_url = os.environ.get("OPENAI_BASE_URL", _DEFAULT_BASE_URL)
    timeout = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT))
    url = _build_completions_url(base_url)

    effective_prompt = system_prompt if system_prompt is not None else _CHAT_SYSTEM_PROMPT
    prompt_messages: list[dict[str, str]] = [{"role": "system", "content": effective_prompt}]
    for item in history or []:
        if item.role in ("user", "assistant", "system"):
            prompt_messages.append({"role": item.role, "content": item.content})
    prompt_messages.append({"role": "user", "content": message})

    payload = {
        "model": model,
        "messages": prompt_messages,
        "max_tokens": 512,
        "temperature": 0.7,
    }

    with httpx.Client(timeout=timeout) as http:
        response = http.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


def _build_chat_system_prompt(db) -> str:
    """Build the global chat system prompt with a bounded ops context window."""
    if db is None:
        return _CHAT_SYSTEM_PROMPT
    try:
        from backend.app.ops_routes import build_ops_context_snippet

        snippet = build_ops_context_snippet(db)
        if not snippet:
            return _CHAT_SYSTEM_PROMPT
        n = snippet.count("\n") + 1
        ops_section = _OPS_CONTEXT_HEADER.format(n=n, snippet=snippet)
        return _CHAT_SYSTEM_PROMPT + ops_section
    except Exception:
        return _CHAT_SYSTEM_PROMPT


def _needs_web_search(message: str) -> bool:
    """Return True when the message appears to request current/live information."""
    stripped = message.strip()
    if stripped.lower().startswith(_SEARCH_PREFIX):
        return True
    return bool(_RECENCY_PATTERN.search(stripped))


def _build_search_query(message: str) -> str:
    """Strip the 'search:' prefix if present and return the query."""
    stripped = message.strip()
    if stripped.lower().startswith(_SEARCH_PREFIX):
        return stripped[len(_SEARCH_PREFIX):].strip()
    return stripped


def _format_citations(results: list[dict[str, Any]]) -> str:
    """Format web search results as a Sources section appended to the reply."""
    if not results:
        return ""
    lines = ["\n\nSources:"]
    for i, r in enumerate(results, 1):
        title = r.get("title") or r.get("url", "")
        url = r.get("url", "")
        published = r.get("published_at")
        date_str = f" ({published})" if published else ""
        lines.append(f"{i}. [{title}]({url}){date_str}")
    return "\n".join(lines)


def _build_retrieval_system_prompt(db, search_results: list[dict[str, Any]]) -> str:
    """Build a system prompt that injects retrieved web snippets."""
    base = _build_chat_system_prompt(db)
    if not search_results:
        return base
    snippets = []
    for r in search_results:
        title = r.get("title", "")
        url = r.get("url", "")
        snippet = r.get("snippet", "")
        published = r.get("published_at", "")
        date_str = f" (published: {published})" if published else ""
        snippets.append(f"- {title}{date_str}\n  URL: {url}\n  {snippet}")
    retrieval_section = (
        "\n\n--- Web search results (use to answer the user's question) ---\n"
        + "\n".join(snippets)
        + "\n--- End of web search results ---\n"
        "Cite sources by their URL when referencing retrieved facts."
    )
    return base + retrieval_section


# ---------------------------------------------------------------------------
# GET /api/chat
# ---------------------------------------------------------------------------


@router.get("/chat", status_code=200, dependencies=[Depends(require_auth)])
def list_chat_messages() -> JSONResponse:
    """Return persisted global chat history (newest-first)."""
    db = _db_session()
    if db is None:
        return _error(
            503,
            "service_unavailable",
            "DATABASE_URL is not configured; persisted global chat is unavailable.",
        )

    try:
        messages = _list_persisted_messages(db)
        return _json_response(
            ChatHistoryResponse(
                messages=[_message_to_response(message) for message in messages],
                tools_available=_TOOLS_AVAILABLE,
            )
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# POST /api/chat
# ---------------------------------------------------------------------------


@router.post("/chat", status_code=200, dependencies=[Depends(require_auth)])
async def chat(http_request: FastAPIRequest, body: dict[str, Any]) -> JSONResponse:
    """
    Send a message to the UI Blueprint assistant.

    When the message contains recency keywords (latest, today, current, ...) or
    starts with "search:", a Tavily web search is performed and results are
    injected into the system prompt so the assistant can answer with up-to-date
    information.  Retrieved source URLs are appended to the reply.

    Agent mode can be enabled via:
    - Body field: ``agent_mode: true``
    - Header: ``X-Agent-Mode: 1``

    When enabled, the assistant is instructed to respond using ARTIFACT_*
    structured output sections.
    """
    try:
        request = ChatPostRequest.model_validate(body or {})
    except ValidationError as exc:
        if any(error["loc"] == ("message",) for error in exc.errors()):
            return _error(
                400,
                "invalid_request",
                "message is required and must not be empty.",
            )
        return _error(
            422,
            "invalid_request",
            "Request body failed validation.",
            {"errors": exc.errors()},
        )

    message = request.message
    context = request.context
    # Agent mode: body field takes precedence; header is a fallback.
    agent_mode = request.agent_mode or (
        http_request.headers.get("X-Agent-Mode", "0") == "1"
    )

    db = _db_session()
    try:
        user_message = _persist_message(db, "user", message, context)
        history = _load_recent_history(db)

        # Read OPENAI_API_KEY at call time -- never returned or logged.
        openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()

        if not openai_api_key:
            reply = _stub_reply(message)
        else:
            # Optionally retrieve web results for recency-sensitive queries.
            search_results: list[dict[str, Any]] = []
            if _needs_web_search(message):
                try:
                    from backend.app.web_search import TavilyKeyMissing, web_search

                    query = _build_search_query(message)
                    raw = web_search(query, recency_days=7, max_results=5)
                    search_results = raw.get("results", [])
                except TavilyKeyMissing:
                    logger.info("web_search: TAVILY_API_KEY not set; skipping retrieval.")
                except Exception:
                    logger.warning("web_search call failed; continuing without retrieval.")

            # Build system prompt with ops context + optional retrieval results.
            if search_results:
                system_prompt = _build_retrieval_system_prompt(db, search_results)
            else:
                system_prompt = _build_chat_system_prompt(db)

            # When agent_mode is enabled, append an instruction to use ARTIFACT format.
            if agent_mode:
                system_prompt += (
                    "\n\nRespond using structured ARTIFACT sections. "
                    "Each section must begin on its own line as: ARTIFACT_<NAME>: <value>. "
                    "Use concise section names like ARTIFACT_SUMMARY, ARTIFACT_DETAILS, "
                    "ARTIFACT_SOURCES, etc."
                )

            try:
                reply = _call_openai_chat(
                    message,
                    openai_api_key,
                    history[:-1] if history else [],
                    system_prompt=system_prompt,
                )
            except httpx.TimeoutException:
                return _error(
                    502,
                    "ai_provider_error",
                    "Chat request timed out.",
                    {"hint": "timeout"},
                )
            except httpx.RequestError:
                return _error(
                    502,
                    "ai_provider_error",
                    "Network error contacting AI.",
                    {"hint": "network_error"},
                )
            except (httpx.HTTPStatusError, KeyError, IndexError, ValueError):
                return _error(
                    502,
                    "ai_provider_error",
                    "Invalid response from AI.",
                    {"hint": "invalid_response"},
                )

            # Append citations to the reply if retrieval was performed.
            if search_results:
                reply += _format_citations(search_results)

        assistant_message = _persist_message(db, "assistant", reply, context)
        return _json_response(
            ChatPostResponse(
                reply=reply,
                tools_available=_TOOLS_AVAILABLE,
                user_message=user_message,
                assistant_message=assistant_message,
            )
        )
    finally:
        if db is not None:
            db.close()


# ---------------------------------------------------------------------------
# POST /api/chat/{message_id}/edit
# ---------------------------------------------------------------------------


@router.post(
    "/chat/{message_id}/edit",
    status_code=201,
    dependencies=[Depends(require_auth)],
)
def edit_chat_message(message_id: str, body: dict[str, Any]) -> JSONResponse:
    """
    Edit a user message by creating a new message and marking the original as
    superseded.

    The original message is preserved (not deleted) but its
    ``superseded_by_id`` field is set to the new message's id, so clients can
    hide it from the active conversation while retaining the audit trail.

    Only ``role="user"`` messages may be edited.

    Request body::

        { "content": "corrected message text" }

    Response (HTTP 201)::

        {
            "schema_version": "...",
            "original_message": { ... superseded=true ... },
            "new_message":      { ... superseded=false ... }
        }
    """
    try:
        request = ChatEditRequest.model_validate(body or {})
    except ValidationError:
        return _error(422, "invalid_request", "Request body failed validation.")

    # Validate message_id is a UUID.
    try:
        msg_uuid = uuid.UUID(message_id)
    except ValueError:
        return _error(400, "invalid_request", "message_id must be a valid UUID.")

    db = _db_session()
    if db is None:
        return _error(
            503,
            "service_unavailable",
            "DATABASE_URL is not configured; persisted global chat is unavailable.",
        )

    try:
        from backend.app.models import GlobalChatMessage

        original = db.get(GlobalChatMessage, msg_uuid)
        if original is None:
            return _error(404, "not_found", "Message not found.")
        if original.role != "user":
            return _error(
                400, "invalid_request", "Only user messages may be edited."
            )

        # Create the new (replacement) message.
        new_msg = GlobalChatMessage(
            role="user",
            content=request.content,
            session_id=original.session_id,
            domain_profile_id=original.domain_profile_id,
        )
        db.add(new_msg)
        db.flush()  # assign new_msg.id

        # Mark the original as superseded.
        original.superseded_by_id = new_msg.id
        db.add(original)
        db.commit()
        db.refresh(original)
        db.refresh(new_msg)

        return JSONResponse(
            status_code=201,
            content=ChatEditResponse(
                original_message=_message_to_response(original),
                new_message=_message_to_response(new_msg),
            ).model_dump(mode="json"),
        )
    finally:
        db.close()
