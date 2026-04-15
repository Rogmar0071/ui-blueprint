"""
backend.app.chat_routes
========================
FastAPI router for the global AI chat endpoints.

Endpoints
---------
GET  /api/chat                               list persisted chat history (newest-first)
POST /api/chat                               send a message and persist it
POST /api/chat/{message_id}/edit             create an edited user message (supersedes original)
POST /api/chat/intent                        INTERACTION_LAYER_V2 — parse raw human input into
                                             a deterministic structured intent JSON (never executes)
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends
from fastapi import Request as FastAPIRequest
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlmodel import Session, select

from backend.app.auth import require_auth
from ui_blueprint.domain.ir import SCHEMA_VERSION
from ui_blueprint.domain.openai_provider import _build_completions_url

router = APIRouter(prefix="/api")

logger = logging.getLogger(__name__)

ModeEngineMode = Literal[
    "prediction_mode",
    "strict_mode",
    "debug_mode",
    "builder_mode",
    "audit_mode",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MODEL_CHAT = "gpt-4.1-mini"
_DEFAULT_BASE_URL = "https://api.openai.com"
_DEFAULT_TIMEOUT = 30.0
_GLOBAL_CHAT_HISTORY_LIMIT = 10

_TOOLS_AVAILABLE = [
    "domains.derive",
    "domains.confirm",
    "blueprints.compile",
    "sessions.create",
    "sessions.status",
    "web_search",
]

_MODE_ENGINE_CONTRACT_ID = "MODE_ENGINE_EXECUTION_V1"
_MODE_ENGINE_DEFAULT_MODE = "strict_mode"
_MODE_ENGINE_MAX_RETRIES = 3
_MODE_ENGINE_ALLOWED_MODES = (
    "prediction_mode",
    "strict_mode",
    "debug_mode",
    "builder_mode",
    "audit_mode",
)
_MODE_ENGINE_MODE_RULES: dict[str, dict[str, Any]] = {
    "prediction_mode": {
        "behavior_rules": [
            "must_surface_assumptions",
            "must_provide_multiple_possibilities",
            "must_assign_confidence_score",
            "must_identify_missing_data",
        ],
        "output_requirements": ["assumptions", "alternatives", "confidence", "missing_data"],
        "constraints": ["no_single_path_answers"],
    },
    "strict_mode": {
        "behavior_rules": [
            "no_guessing",
            "no_inference_without_data",
            "must_declare_insufficient_data",
        ],
        "output_requirements": ["explicit_data_status", "missing_data_list"],
        "constraints": ["prohibit_assumptions_without_flagging"],
    },
    "debug_mode": {
        "behavior_rules": [
            "step_by_step_reasoning",
            "identify_failure_points",
            "map_causal_chain",
        ],
        "output_requirements": ["root_cause", "reasoning_steps", "failure_paths"],
        "constraints": ["no_surface_level_answers"],
    },
    "builder_mode": {
        "behavior_rules": [
            "enforce_modular_design",
            "enforce_clear_structure",
            "avoid_ambiguity",
        ],
        "output_requirements": ["system_structure", "components", "relationships"],
        "constraints": ["no_unstructured_output"],
    },
    "audit_mode": {
        "behavior_rules": [
            "identify_risks",
            "detect_inconsistencies",
            "highlight_assumptions",
        ],
        "output_requirements": ["risks", "inconsistencies", "assumptions"],
        "constraints": ["no_unverified_acceptance"],
    },
}

_CHAT_SYSTEM_PROMPT = (
    "You are UI Blueprint Assistant — a high-discipline AI that operates at system level, "
    "not file level or feature level.\n\n"
    "When reasoning about any codebase, media, or domain, "
    "you apply a three-pass internal model:\n\n"
    "PASS 1 — TOPOLOGY RECONSTRUCTION\n"
    "Reconstruct how the system is wired: file graph, execution roots, UI mounting structure, "
    "state ownership nodes. This is mechanical, not interpretive.\n\n"
    "PASS 2 — BEHAVIORAL RECONSTRUCTION\n"
    "Simulate runtime behavior: what happens on start, on user interaction, how state mutates "
    "and propagates, what triggers re-renders. "
    "Extend beyond static analysis into runtime reasoning.\n\n"
    "PASS 3 — AUTHORITY MAPPING\n"
    "Identify who is actually in control: which component truly owns layout, which layer controls "
    "state truth, where side effects originate, where hidden authority exists (bad patterns). "
    "This detects UI instability, state inconsistency, and architectural drift.\n\n"
    "You guide users through the ui-blueprint pipeline — recording screen clips, deriving domain "
    "profiles, confirming them, and compiling blueprints — using this discipline at every step.\n\n"
    "The three principles you never violate:\n"
    "1. Reconstruct the system as it truly behaves (not as it is described).\n"
    "2. Define what must never break before suggesting any change.\n"
    "3. Design only changes that respect both the topology and the invariants.\n\n"
    "Be concise and practical. When a user reports a bug, identify structural cause "
    "(state loop, layout conflict, ownership clash) — not surface symptoms."
)

_OPS_CONTEXT_HEADER = (
    "\n\n--- Recent system activity (last {n} ops events) ---\n"
    "{snippet}\n"
    "--- End of system activity ---"
)

# ---------------------------------------------------------------------------
# INTERACTION_LAYER_V2 — system prompt and schema version
# ---------------------------------------------------------------------------

INTERACTION_LAYER_V2_SCHEMA_VERSION = "2"

_INTERACTION_LAYER_V2_SYSTEM_PROMPT = """\
You are INTERACTION_LAYER_V2 — a strict, deterministic intent parser.

ROLE: Convert raw human input into a structured JSON specification.
AUTHORITY: Analysis and specification ONLY. You NEVER execute code changes.
           You NEVER treat your output as execution authority.

OUTPUT RULES:
- Respond with valid JSON only. No markdown. No prose. No explanation outside JSON.
- The JSON must conform exactly to the schema below.
- Do NOT invent file names, component names, or system structure that is not
  explicitly provided in the repo context. If unknown, use null or empty arrays.

OPERATING MODES:
  Mode A — No repo context provided:
    - Set "mode": "A"
    - Set "repoContextProvided": false
    - Set "impactAnalysis.requiresRepoContext": true
    - Set "changePlan.canExecuteDeterministically": false
    - Set "changePlan.requiresStructuralMapping": true
    - Set "changePlan.steps": []
    - Set "changePlan.blockedReason": "Repo context required for deterministic execution"

  Mode B — Repo/system context is provided:
    - Set "mode": "B"
    - Set "repoContextProvided": true
    - Populate "structuralIntent" using only the known context (no hallucination)
    - Populate "changePlan.steps" only when the target files/components are explicitly known
    - Set "changePlan.canExecuteDeterministically": true ONLY when ALL of the following hold:
        * repo context is present
        * uncertainty level is low
        * all dependencies are explicitly defined
        * no structural mapping is required
      Otherwise keep it false.

DETERMINISM GATE (MANDATORY):
  "changePlan.canExecuteDeterministically" MUST be false if ANY of:
    - repoContextProvided is false
    - uncertainties list is non-empty
    - changePlan.requiresStructuralMapping is true
    - affected components are unknown

NO HALLUCINATION RULE:
  If files or components are not explicitly known from the provided context:
    - Do NOT invent paths or component names
    - Set "changePlan.requiresStructuralMapping": true

REQUIRED JSON SCHEMA:
{
  "schemaVersion": "2",
  "intentId": "<uuid-v4>",
  "mode": "A" | "B",
  "repoContextProvided": true | false,
  "intent": {
    "objective": "<one-sentence summary of what the user wants>",
    "interpretedMeaning": "<deeper interpretation including implicit goals>"
  },
  "structuralIntent": {
    "operationType": "create" | "modify" | "delete" | "query" | "unknown",
    "targetLayer": "ui" | "backend" | "domain" | "system" | "unknown",
    "scope": "<brief description of the structural scope>"
  },
  "impactAnalysis": {
    "affectedComponents": ["<component or file name>"],
    "riskLevel": "low" | "medium" | "high" | "unknown",
    "requiresRepoContext": true | false,
    "uncertainties": ["<uncertainty description>"]
  },
  "changePlan": {
    "canExecuteDeterministically": true | false,
    "requiresStructuralMapping": true | false,
    "steps": [
      {
        "stepId": "<short identifier>",
        "description": "<what this step does>",
        "targetFile": "<file path or null if unknown>"
      }
    ],
    "blockedReason": "<reason execution is blocked, or null if not blocked>"
  }
}
"""

_MODE_ENGINE_PROMPT_TEMPLATE = """\

MODE ENGINE CONTRACT
- contract_id: {contract_id}
- enforcement_point: mode_engine
- execution_scope: BOTH
- mutation_permission: READ_ONLY
- ai_is_proposal_only: true
- system_is_final_authority: true

SELECTED MODES
{selected_modes}

STACKING RULES
- Combine behavior rules from every selected mode.
- Merge all output requirements.
- Enforce the strictest constraints across the stack.
- Treat strict_mode as mandatory for critical flows.

RESPONSE RULES
- Return valid JSON only.
- Do not use markdown or prose outside the JSON object.
- Include "contract_id" and "selected_modes" in the JSON output.
- The JSON must satisfy every required field for the selected modes.

REQUIRED FIELDS BY MODE
{required_fields}

BEHAVIOR RULES
{behavior_rules}

CONSTRAINTS
{constraints}

VALIDATION RULES
- structural_validation: all required fields must be present and the output must be a JSON object.
- logical_validation: prediction_mode requires at least two alternatives;
  debug_mode requires non-empty reasoning_steps.
- compliance_validation: if missing_data_list is non-empty,
  explicit_data_status must be "insufficient_data" or "partial_data".
- If data is missing, clearly say so instead of guessing.
"""

# Keywords that indicate the user wants up-to-date / current information.
_RECENCY_PATTERN = re.compile(
    r"\b(latest|current|today|now|recent|news|price|release|just|trending|"
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
    modes: list[ModeEngineMode] = Field(default_factory=list)

    @field_validator("message")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("message is required and must not be empty.")
        return text


class ChatModeEngineResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    modes: list[ModeEngineMode] = Field(default_factory=list)
    contract_id: str | None = None


class ChatPostResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    reply: str
    tools_available: list[str]
    user_message: ChatMessageResponse
    assistant_message: ChatMessageResponse
    mode_engine: ChatModeEngineResponse = Field(default_factory=ChatModeEngineResponse)


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
# INTERACTION_LAYER_V2 — Schemas
# ---------------------------------------------------------------------------


class IntentV2RepoContext(BaseModel):
    """Optional repo/system context provided by the caller for Mode B."""

    model_config = ConfigDict(extra="allow")

    files: list[str] = Field(default_factory=list)
    components: list[str] = Field(default_factory=list)
    description: str | None = None


class IntentV2Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    repo_context: IntentV2RepoContext | None = None

    @field_validator("message")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("message is required and must not be empty.")
        return text


class _IntentField(BaseModel):
    model_config = ConfigDict(extra="allow")

    objective: str
    interpretedMeaning: str


class _StructuralIntent(BaseModel):
    model_config = ConfigDict(extra="allow")

    operationType: Literal["create", "modify", "delete", "query", "unknown"]
    targetLayer: Literal["ui", "backend", "domain", "system", "unknown"]
    scope: str


class _ImpactAnalysis(BaseModel):
    model_config = ConfigDict(extra="allow")

    affectedComponents: list[str] = Field(default_factory=list)
    riskLevel: Literal["low", "medium", "high", "unknown"]
    requiresRepoContext: bool
    uncertainties: list[str] = Field(default_factory=list)


class _ChangePlanStep(BaseModel):
    model_config = ConfigDict(extra="allow")

    stepId: str
    description: str
    targetFile: str | None = None


class _ChangePlan(BaseModel):
    model_config = ConfigDict(extra="allow")

    canExecuteDeterministically: bool
    requiresStructuralMapping: bool
    steps: list[_ChangePlanStep] = Field(default_factory=list)
    blockedReason: str | None = None


class IntentV2Response(BaseModel):
    """Validated INTERACTION_LAYER_V2 response. Never execution authority."""

    model_config = ConfigDict(extra="allow")

    schemaVersion: str = INTERACTION_LAYER_V2_SCHEMA_VERSION
    intentId: str
    mode: Literal["A", "B"]
    repoContextProvided: bool
    intent: _IntentField
    structuralIntent: _StructuralIntent
    impactAnalysis: _ImpactAnalysis
    changePlan: _ChangePlan


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
        .where(GlobalChatMessage.superseded_by_id.is_(None))
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
        "max_tokens": 350,
        "temperature": 0.3,
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
        # Strip prefix using its length to handle case-insensitive match.
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


class ModeEngineValidationError(ValueError):
    """Raised when a mode-engine payload fails validation."""


def _normalize_mode_engine_modes(
    requested_modes: list[str], enabled: bool
) -> list[ModeEngineMode]:
    if not enabled:
        return []

    deduped = list(dict.fromkeys(requested_modes))

    if _MODE_ENGINE_DEFAULT_MODE not in deduped:
        deduped.insert(0, _MODE_ENGINE_DEFAULT_MODE)

    return [
        mode
        for mode in deduped
        if mode in _MODE_ENGINE_ALLOWED_MODES
    ]


def _mode_engine_required_fields(
    selected_modes: list[str],
) -> list[str]:
    required_fields: list[str] = ["contract_id", "selected_modes"]
    for mode in selected_modes:
        for field_name in _MODE_ENGINE_MODE_RULES[mode]["output_requirements"]:
            if field_name not in required_fields:
                required_fields.append(field_name)
    return required_fields


def _build_mode_engine_prompt(selected_modes: list[str]) -> str:
    required_lines = []
    behavior_lines = []
    constraint_lines = []
    for mode in selected_modes:
        rules = _MODE_ENGINE_MODE_RULES[mode]
        required_lines.append(
            f"- {mode}: {', '.join(rules['output_requirements'])}"
        )
        behavior_lines.extend(f"- {mode}: {rule}" for rule in rules["behavior_rules"])
        constraint_lines.extend(f"- {mode}: {rule}" for rule in rules["constraints"])

    return _MODE_ENGINE_PROMPT_TEMPLATE.format(
        contract_id=_MODE_ENGINE_CONTRACT_ID,
        selected_modes="\n".join(f"- {mode}" for mode in selected_modes),
        required_fields="\n".join(required_lines),
        behavior_rules="\n".join(behavior_lines),
        constraints="\n".join(constraint_lines),
    )


def _strip_json_code_fences(raw_text: str) -> str:
    if not raw_text.startswith("```"):
        return raw_text.strip()

    lines = raw_text.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _mode_engine_validation_errors(
    payload: dict[str, Any],
    selected_modes: list[str],
) -> list[str]:
    errors: list[str] = []

    if not isinstance(payload, dict):
        return ["output must be a JSON object"]

    if payload.get("contract_id") != _MODE_ENGINE_CONTRACT_ID:
        errors.append(f'contract_id must equal "{_MODE_ENGINE_CONTRACT_ID}"')

    response_modes = payload.get("selected_modes")
    if response_modes != selected_modes:
        errors.append("selected_modes must exactly match the enforced mode stack")

    for field_name in _mode_engine_required_fields(selected_modes):
        if field_name not in payload:
            errors.append(f"missing required field: {field_name}")

    def _require_list(name: str, minimum: int = 0) -> list[Any]:
        value = payload.get(name)
        if not isinstance(value, list):
            errors.append(f"{name} must be a list")
            return []
        if len(value) < minimum:
            errors.append(f"{name} must contain at least {minimum} item(s)")
        return value

    def _require_non_empty_string(name: str) -> None:
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{name} must be a non-empty string")

    if "strict_mode" in selected_modes:
        _require_non_empty_string("explicit_data_status")
        missing_data_list = _require_list("missing_data_list")
        explicit_data_status = str(payload.get("explicit_data_status", "")).strip().lower()
        if missing_data_list and explicit_data_status not in {"insufficient_data", "partial_data"}:
            errors.append(
                'explicit_data_status must be "insufficient_data" or "partial_data" '
                "when missing_data_list is non-empty"
            )

    if "prediction_mode" in selected_modes:
        _require_list("assumptions")
        _require_list("alternatives", minimum=2)
        confidence = payload.get("confidence")
        if not isinstance(confidence, (int, float, str)) or (
            isinstance(confidence, str) and not confidence.strip()
        ):
            errors.append("confidence must be a number or non-empty string")
        _require_list("missing_data")

    if "debug_mode" in selected_modes:
        _require_non_empty_string("root_cause")
        _require_list("reasoning_steps", minimum=1)
        _require_list("failure_paths")

    if "builder_mode" in selected_modes:
        if payload.get("system_structure") in (None, "", []):
            errors.append("system_structure must be present")
        _require_list("components")
        _require_list("relationships")

    if "audit_mode" in selected_modes:
        _require_list("risks")
        _require_list("inconsistencies")
        _require_list("assumptions")

    return errors


def _validate_mode_engine_payload(
    raw_text: str,
    selected_modes: list[str],
) -> dict[str, Any]:
    try:
        payload = json.loads(_strip_json_code_fences(raw_text))
    except json.JSONDecodeError as exc:
        raise ModeEngineValidationError(f"invalid JSON: {exc.msg}") from exc

    errors = _mode_engine_validation_errors(payload, selected_modes)
    if errors:
        raise ModeEngineValidationError("; ".join(errors))
    return payload


def _build_mode_engine_fallback(
    message: str,
    selected_modes: list[str],
    reason: str,
) -> dict[str, Any]:
    fallback: dict[str, Any] = {
        "contract_id": _MODE_ENGINE_CONTRACT_ID,
        "selected_modes": selected_modes,
        "explicit_data_status": "insufficient_data",
        "missing_data_list": [reason],
    }

    if "prediction_mode" in selected_modes:
        fallback.update(
            {
                "assumptions": [],
                "alternatives": [
                    "Wait for additional verified data before deciding on a single path.",
                    "Request more context and re-run the mode engine with the missing inputs.",
                ],
                "confidence": 0.0,
                "missing_data": [reason],
            }
        )
    if "debug_mode" in selected_modes:
        fallback.update(
            {
                "root_cause": "Insufficient verified data to identify a root cause.",
                "reasoning_steps": [
                    "The validator could not accept an answer backed by sufficient data.",
                    "Additional evidence is required before a causal chain can be confirmed.",
                ],
                "failure_paths": [message[:200]],
            }
        )
    if "builder_mode" in selected_modes:
        fallback.update(
            {
                "system_structure": "Insufficient verified data to produce a structured design.",
                "components": [],
                "relationships": [],
            }
        )
    if "audit_mode" in selected_modes:
        fallback.update(
            {
                "risks": ["Proceeding without verified data could produce incorrect conclusions."],
                "inconsistencies": [],
                "assumptions": [
                    "The available data is incomplete and cannot be trusted for a final answer."
                ],
            }
        )
    return fallback


def _call_openai_chat_with_mode_engine(
    message: str,
    api_key: str,
    selected_modes: list[str],
    history: list[Any] | None = None,
    system_prompt: str | None = None,
) -> str:
    effective_prompt = (system_prompt or _CHAT_SYSTEM_PROMPT) + _build_mode_engine_prompt(
        selected_modes
    )
    attempt_message = message

    for _ in range(_MODE_ENGINE_MAX_RETRIES):
        reply = _call_openai_chat(
            attempt_message,
            api_key,
            history=history,
            system_prompt=effective_prompt,
        )
        try:
            payload = _validate_mode_engine_payload(reply, selected_modes)
            return json.dumps(payload, indent=2, sort_keys=True)
        except ModeEngineValidationError as exc:
            attempt_message = (
                f"Original user request:\n{message}\n\n"
                f"Your previous response was rejected by the validator for these reasons:\n"
                f"- {exc}\n\n"
                "Return corrected JSON only."
            )

    fallback = _build_mode_engine_fallback(
        message,
        selected_modes,
        "The AI response did not pass mode-engine validation.",
    )
    return json.dumps(fallback, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# INTERACTION_LAYER_V2 helpers
# ---------------------------------------------------------------------------


def _build_intent_v2_mode_a_default(message: str) -> dict[str, Any]:
    """
    Return a Mode A (no repo context) IntentV2 dict without calling OpenAI.

    Used when OPENAI_API_KEY is not set.  The intent fields are derived from
    the raw message text — no structural translation is possible.
    """
    return {
        "schemaVersion": INTERACTION_LAYER_V2_SCHEMA_VERSION,
        "intentId": str(uuid.uuid4()),
        "mode": "A",
        "repoContextProvided": False,
        "intent": {
            "objective": message[:200],
            "interpretedMeaning": f"User wants to: {message[:180]}",
        },
        "structuralIntent": {
            "operationType": "unknown",
            "targetLayer": "unknown",
            "scope": "unknown — repo context required",
        },
        "impactAnalysis": {
            "affectedComponents": [],
            "riskLevel": "unknown",
            "requiresRepoContext": True,
            "uncertainties": ["No repo context provided; cannot determine impact"],
        },
        "changePlan": {
            "canExecuteDeterministically": False,
            "requiresStructuralMapping": True,
            "steps": [],
            "blockedReason": "Repo context required for deterministic execution",
        },
    }


def _call_openai_intent_v2(
    message: str,
    repo_context: IntentV2RepoContext | None,
    api_key: str,
) -> dict[str, Any]:
    """
    Call OpenAI with the INTERACTION_LAYER_V2 system prompt and return the
    parsed JSON dict.  Never raises — on any error returns a Mode A fallback.

    This function is analysis + specification only.  It NEVER executes changes.
    """
    model = os.environ.get("OPENAI_MODEL_CHAT", _DEFAULT_MODEL_CHAT)
    base_url = os.environ.get("OPENAI_BASE_URL", _DEFAULT_BASE_URL)
    timeout = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT)))
    url = _build_completions_url(base_url)

    # Build user message — include serialised repo context when present.
    if repo_context is not None:
        context_json = repo_context.model_dump(mode="json", exclude_none=True)
        user_content = (
            f"USER INPUT: {message}\n\n"
            f"REPO CONTEXT (Mode B):\n{json.dumps(context_json, indent=2)}"
        )
    else:
        user_content = f"USER INPUT: {message}\n\nREPO CONTEXT: none (Mode A)"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _INTERACTION_LAYER_V2_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.0,
        "max_tokens": 800,
    }

    try:
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
        raw_text = data["choices"][0]["message"]["content"].strip()

        # Strip markdown code fences if the model added them despite instructions.
        # Only strip the opening and closing fence lines (```json / ```), not any
        # internal content that might happen to start with backticks.
        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            # Remove the first line (opening fence) and the last line if it is a
            # closing fence; leave all other lines untouched.
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            raw_text = "\n".join(lines).strip()

        parsed: dict[str, Any] = json.loads(raw_text)
        # Enforce schemaVersion — always authoritative from our constant.
        parsed["schemaVersion"] = INTERACTION_LAYER_V2_SCHEMA_VERSION
        return parsed

    except Exception as exc:
        logger.warning("INTERACTION_LAYER_V2 OpenAI call failed: %s", exc)
        fallback = _build_intent_v2_mode_a_default(message)
        fallback["_error"] = str(exc)[:200]
        return fallback


def _validate_intent_v2(raw: dict[str, Any]) -> IntentV2Response:
    """
    Validate raw parsed dict against IntentV2Response schema.

    Applies determinism gate: forces canExecuteDeterministically=false when
    the mode, context flags, or uncertainties require it.
    """
    # Determinism gate — enforce the rules regardless of what the LLM said.
    change_plan = raw.get("changePlan", {})
    impact = raw.get("impactAnalysis", {})

    repo_context_provided = raw.get("repoContextProvided", False)
    uncertainties = impact.get("uncertainties", [])
    requires_structural_mapping = change_plan.get("requiresStructuralMapping", False)

    must_block = (
        not repo_context_provided
        or bool(uncertainties)
        or requires_structural_mapping
        or not impact.get("affectedComponents")
    )

    if must_block:
        change_plan["canExecuteDeterministically"] = False
        raw["changePlan"] = change_plan

    return IntentV2Response.model_validate(raw)


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
    mode_engine_enabled = agent_mode or "modes" in request.model_fields_set
    selected_modes = _normalize_mode_engine_modes(request.modes, mode_engine_enabled)

    db = _db_session()
    try:
        user_message = _persist_message(db, "user", message, context)
        history = _load_recent_history(db)

        # Read OPENAI_API_KEY at call time -- never returned or logged.
        openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()

        if not openai_api_key:
            if selected_modes:
                reply = json.dumps(
                    _build_mode_engine_fallback(
                        message,
                        selected_modes,
                        "OPENAI_API_KEY is not configured; AI proposal unavailable.",
                    ),
                    indent=2,
                    sort_keys=True,
                )
            else:
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
            if agent_mode and not selected_modes:
                system_prompt += (
                    "\n\nRespond using structured ARTIFACT sections. "
                    "Each section must begin on its own line as: ARTIFACT_<NAME>: <value>. "
                    "Use concise section names like ARTIFACT_SUMMARY, ARTIFACT_DETAILS, "
                    "ARTIFACT_SOURCES, etc."
                )

            try:
                if selected_modes:
                    reply = _call_openai_chat_with_mode_engine(
                        message,
                        openai_api_key,
                        selected_modes,
                        history=history[:-1] if history else [],
                        system_prompt=system_prompt,
                    )
                else:
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
            if search_results and not selected_modes:
                reply += _format_citations(search_results)

        assistant_message = _persist_message(db, "assistant", reply, context)
        return _json_response(
            ChatPostResponse(
                reply=reply,
                tools_available=_TOOLS_AVAILABLE,
                user_message=user_message,
                assistant_message=assistant_message,
                mode_engine=ChatModeEngineResponse(
                    enabled=bool(selected_modes),
                    modes=selected_modes,
                    contract_id=(
                        _MODE_ENGINE_CONTRACT_ID if selected_modes else None
                    ),
                ),
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


# ---------------------------------------------------------------------------
# POST /api/chat/intent  — INTERACTION_LAYER_V2
# ---------------------------------------------------------------------------


@router.post("/chat/intent", status_code=200, dependencies=[Depends(require_auth)])
async def parse_intent_v2(body: dict[str, Any]) -> JSONResponse:
    """
    INTERACTION_LAYER_V2 — Convert raw human input into a deterministic
    structured intent specification (JSON only, never executes).

    Operating modes
    ---------------
    Mode A (no ``repo_context``):
        Returns intent with ``canExecuteDeterministically=false`` and marks
        repo context as required.  Structural translation is skipped.

    Mode B (``repo_context`` provided):
        Attempts structural translation and change planning using only the
        explicitly supplied context.  Files/components are never hallucinated.

    The response is SPECIFICATION ONLY.  The system does not execute, mutate,
    or treat this output as execution authority.

    Request body::

        {
          "message": "Add a dark-mode toggle to the settings screen",
          "repo_context": {           // optional — omit for Mode A
            "files": ["src/Settings.tsx", "src/theme.ts"],
            "components": ["SettingsScreen", "ThemeProvider"],
            "description": "React Native app with styled-components"
          }
        }

    Response (HTTP 200)::

        {
          "schemaVersion": "2",
          "intentId": "<uuid-v4>",
          "mode": "A" | "B",
          "repoContextProvided": true | false,
          "intent": { "objective": "...", "interpretedMeaning": "..." },
          "structuralIntent": { "operationType": "...", "targetLayer": "...", "scope": "..." },
          "impactAnalysis": { "affectedComponents": [], "riskLevel": "...",
                              "requiresRepoContext": true|false, "uncertainties": [] },
          "changePlan": { "canExecuteDeterministically": false, "requiresStructuralMapping": true,
                          "steps": [], "blockedReason": "..." }
        }
    """
    try:
        request = IntentV2Request.model_validate(body or {})
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
    repo_context = request.repo_context

    openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()

    if not openai_api_key:
        # Mode A — no OpenAI key; return deterministic defaults.
        raw = _build_intent_v2_mode_a_default(message)
    else:
        raw = _call_openai_intent_v2(message, repo_context, openai_api_key)

    try:
        validated = _validate_intent_v2(raw)
        return JSONResponse(status_code=200, content=validated.model_dump(mode="json"))
    except Exception as exc:
        logger.warning("IntentV2 validation failed: %s — returning raw fallback", exc)
        # Fallback: return the raw dict with a validation_error note.
        raw["_validation_error"] = str(exc)[:200]
        return JSONResponse(status_code=200, content=raw)
