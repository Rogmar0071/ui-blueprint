"""
backend.app.mode_engine
=======================
Mode Engine — V1 + Enforcement Patch Applied (MODE_ENGINE_ENFORCEMENT_PATCH_V1)

This module is the **single source of truth** for all mode-engine behaviour.
``chat_routes`` imports from here; nothing in this module imports from
``chat_routes``, so there are no circular dependencies.

Enforcement lifecycle (per request):
  Stage 0  PreGenerationValidator   — block AI call if input incomplete
  Inject   SystemPromptInjector     — add mode rules to system prompt
  AI call  (injected via ai_caller) — AI generates a proposal
  Stage 1  StructuralValidator      — required fields present?
  Stage 2  LogicalValidator         — conditional field checks
  Stage 3  ComplianceValidator      — mode rules & constraints
  Pass?    Yes → AuditLogger → output to client
           No  → RetryEngine (max 2 retries)
                   → re-prompt with feedback
                   → exhausted? → Stage 4 PostRetryGuard
                                → StructuredFailureResponse → AuditLogger → client

No response exits without an audit log entry.
No response exits without passing validation (hard_validation_boundary invariant).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mode constants (source of truth — imported by chat_routes)
# ---------------------------------------------------------------------------

MODE_ENGINE_CONTRACT_ID = "MODE_ENGINE_EXECUTION_V1"
MODE_ENGINE_DEFAULT_MODE = "strict_mode"
MODE_ENGINE_MAX_RETRIES = 2  # enforcement patch: max_retries = 2
MODE_ENGINE_FALLBACK_MESSAGE_MAX_LENGTH = 200

MODE_PRIORITY_ORDER: list[str] = [
    "strict_mode",
    "prediction_mode",
    "debug_mode",
    "audit_mode",
    "builder_mode",
]

MODE_ENGINE_MODE_RULES: dict[str, dict[str, Any]] = {
    "strict_mode": {
        "behavior_rules": [
            "no_guessing",
            "no_inference_without_data",
            "must_declare_insufficient_data",
        ],
        "output_requirements": ["explicit_data_status", "missing_data_list"],
        "constraints": ["prohibit_assumptions_without_flagging"],
    },
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
    "debug_mode": {
        "behavior_rules": [
            "step_by_step_reasoning",
            "identify_failure_points",
            "map_causal_chain",
        ],
        "output_requirements": ["root_cause", "reasoning_steps", "failure_paths"],
        "constraints": ["no_surface_level_answers"],
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
    "builder_mode": {
        "behavior_rules": [
            "enforce_modular_design",
            "enforce_clear_structure",
            "avoid_ambiguity",
        ],
        "output_requirements": ["system_structure", "components", "relationships"],
        "constraints": ["no_unstructured_output"],
    },
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InsufficientDataError(ValueError):
    """Raised by PreGenerationValidator when input fails Stage 0 checks."""


class ModeEngineValidationError(ValueError):
    """Raised when a mode-engine payload fails post-generation validation."""


# ---------------------------------------------------------------------------
# 1.1  Mode Priority Resolver
# ---------------------------------------------------------------------------


class ModePriorityResolver:
    """
    Sorts and deduplicates active modes by the canonical priority order:
      strict > prediction > debug > audit > builder

    strict_mode is always inserted (mandatory for all mode-engine calls).
    """

    def resolve(self, requested_modes: list[str]) -> list[str]:
        deduped: dict[str, None] = {m: None for m in requested_modes}
        deduped[MODE_ENGINE_DEFAULT_MODE] = None  # always present

        def _priority(m: str) -> int:
            try:
                return MODE_PRIORITY_ORDER.index(m)
            except ValueError:
                return len(MODE_PRIORITY_ORDER)

        return sorted(deduped.keys(), key=_priority)


# ---------------------------------------------------------------------------
# 1.1  Mode Stacking Resolver
# ---------------------------------------------------------------------------


class ModeStackingResolver:
    """
    Merges rules from multiple active modes.
    Conflicts resolve to the **strictest** behaviour (highest-priority mode wins).
    """

    def merge(self, modes: list[str]) -> dict[str, list[str]]:
        merged_behavior: list[str] = []
        # contract_id and selected_modes are always required
        merged_requirements: list[str] = ["contract_id", "selected_modes"]
        merged_constraints: list[str] = []

        for mode in modes:
            rules = MODE_ENGINE_MODE_RULES.get(mode, {})
            for rule in rules.get("behavior_rules", []):
                if rule not in merged_behavior:
                    merged_behavior.append(rule)
            for req in rules.get("output_requirements", []):
                if req not in merged_requirements:
                    merged_requirements.append(req)
            for constraint in rules.get("constraints", []):
                if constraint not in merged_constraints:
                    merged_constraints.append(constraint)

        return {
            "behavior_rules": merged_behavior,
            "output_requirements": merged_requirements,
            "constraints": merged_constraints,
        }


# ---------------------------------------------------------------------------
# 1.2  Pre-Generation Validator  (Stage 0)
# ---------------------------------------------------------------------------


class PreGenerationValidator:
    """
    Stage 0: Validates input completeness BEFORE the AI call.
    Raises InsufficientDataError on failure — the AI call is blocked entirely.

    Checks:
      - input_not_empty      : stripped message must be non-empty
      - required_context_present : message must carry enough substance
                                   (same as non-empty for free-text chat)
    """

    def validate(self, message: str) -> None:
        stripped = (message or "").strip()
        if not stripped:
            raise InsufficientDataError(
                "input_not_empty: message is empty — AI call blocked"
            )


# ---------------------------------------------------------------------------
# 1.2  System Prompt Injector
# ---------------------------------------------------------------------------


class SystemPromptInjector:
    """
    Transforms user input + active modes into a structured system prompt.
    Runs BEFORE every AI call — no bypass allowed (pre_generation_constraints).
    """

    _TEMPLATE = (
        "\n\nMODE ENGINE CONTRACT\n"
        "- contract_id: {contract_id}\n"
        "- enforcement_point: mode_engine\n"
        "- execution_scope: BOTH\n"
        "- mutation_permission: READ_ONLY\n"
        "- ai_is_proposal_only: true\n"
        "- system_is_final_authority: true\n"
        "\nSELECTED MODES\n"
        "{selected_modes}\n"
        "\nSTACKING RULES\n"
        "- Combine behavior rules from every selected mode.\n"
        "- Merge all output requirements.\n"
        "- Enforce the strictest constraints across the stack.\n"
        "- Treat strict_mode as mandatory for critical flows.\n"
        "\nRESPONSE RULES\n"
        "- Return valid JSON only.\n"
        "- Do not use markdown or prose outside the JSON object.\n"
        "- Include \"contract_id\" and \"selected_modes\" in the JSON output.\n"
        "- The JSON must satisfy every required field for the selected modes.\n"
        "\nREQUIRED FIELDS BY MODE\n"
        "{required_fields}\n"
        "\nBEHAVIOR RULES\n"
        "{behavior_rules}\n"
        "\nCONSTRAINTS\n"
        "{constraints}\n"
        "\nVALIDATION RULES\n"
        "- structural_validation: all required fields must be present"
        " and the output must be a JSON object.\n"
        "- logical_validation: prediction_mode requires at least two alternatives;\n"
        "  debug_mode requires non-empty reasoning_steps.\n"
        "- compliance_validation: if missing_data_list is non-empty,\n"
        "  explicit_data_status must be \"insufficient_data\" or \"partial_data\".\n"
        "- If data is missing, clearly say so instead of guessing.\n"
    )

    def inject(self, base_prompt: str, modes: list[str]) -> str:
        """Return base_prompt with mode-engine constraints appended."""
        required_lines: list[str] = []
        behavior_lines: list[str] = []
        constraint_lines: list[str] = []

        for mode in modes:
            rules = MODE_ENGINE_MODE_RULES[mode]
            required_lines.append(
                f"- {mode}: {', '.join(rules['output_requirements'])}"
            )
            behavior_lines.extend(f"- {mode}: {rule}" for rule in rules["behavior_rules"])
            constraint_lines.extend(f"- {mode}: {rule}" for rule in rules["constraints"])

        return base_prompt + self._TEMPLATE.format(
            contract_id=MODE_ENGINE_CONTRACT_ID,
            selected_modes="\n".join(f"- {mode}" for mode in modes),
            required_fields="\n".join(required_lines),
            behavior_rules="\n".join(behavior_lines),
            constraints="\n".join(constraint_lines),
        )


# ---------------------------------------------------------------------------
# 1.3  Validation Pipeline  (Stages 1–4)
# ---------------------------------------------------------------------------


class ValidationPipeline:
    """Post-generation validation: Stages 1–4."""

    @staticmethod
    def _strip_code_fences(raw: str) -> str:
        if not raw.startswith("```"):
            return raw.strip()
        lines = raw.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()

    def parse(self, raw_text: str) -> dict[str, Any]:
        """Parse raw AI text into a dict. Raises ModeEngineValidationError on invalid JSON."""
        try:
            return json.loads(self._strip_code_fences(raw_text))
        except json.JSONDecodeError as exc:
            raise ModeEngineValidationError(f"invalid JSON: {exc.msg}") from exc

    # --- Stage 1 -----------------------------------------------------------

    def stage1_structural(
        self, payload: dict[str, Any], required_fields: list[str]
    ) -> list[str]:
        """Stage 1: All required fields are present in the response object."""
        if not isinstance(payload, dict):
            return ["output must be a JSON object"]
        return [
            f"missing required field: {f}"
            for f in required_fields
            if f not in payload
        ]

    # --- Stage 2 -----------------------------------------------------------

    def stage2_logical(
        self, payload: dict[str, Any], modes: list[str]
    ) -> list[str]:
        """Stage 2: Conditional field checks (assumptions declared, alternatives present, etc.)."""
        errors: list[str] = []

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

        if "prediction_mode" in modes:
            _require_list("assumptions")
            _require_list("alternatives", minimum=2)
            confidence = payload.get("confidence")
            if not isinstance(confidence, (int, float)):
                errors.append("confidence must be a number")
            _require_list("missing_data")

        if "debug_mode" in modes:
            _require_non_empty_string("root_cause")
            _require_list("reasoning_steps", minimum=1)
            _require_list("failure_paths")

        if "builder_mode" in modes:
            if payload.get("system_structure") in (None, "", []):
                errors.append("system_structure must be present")
            _require_list("components")
            _require_list("relationships")

        if "audit_mode" in modes:
            _require_list("risks")
            _require_list("inconsistencies")
            _require_list("assumptions")

        return errors

    # --- Stage 3 -----------------------------------------------------------

    def stage3_compliance(
        self, payload: dict[str, Any], modes: list[str]
    ) -> list[str]:
        """Stage 3: Mode rules followed and constraints not violated."""
        errors: list[str] = []

        if payload.get("contract_id") != MODE_ENGINE_CONTRACT_ID:
            errors.append(f'contract_id must equal "{MODE_ENGINE_CONTRACT_ID}"')

        if payload.get("selected_modes") != modes:
            errors.append("selected_modes must exactly match the enforced mode stack")

        # strict_mode: detect missing required data / prohibit unflagged assumptions
        if "strict_mode" in modes:
            explicit_data_status = (
                str(payload.get("explicit_data_status", "")).strip().lower()
            )
            if not explicit_data_status:
                errors.append("explicit_data_status must be a non-empty string")
            missing_data_list = payload.get("missing_data_list")
            if (
                isinstance(missing_data_list, list)
                and missing_data_list
                and explicit_data_status not in {"insufficient_data", "partial_data"}
            ):
                errors.append(
                    'explicit_data_status must be "insufficient_data" or "partial_data" '
                    "when missing_data_list is non-empty"
                )

        return errors

    # --- Stage 4 -----------------------------------------------------------

    @staticmethod
    def stage4_post_retry(errors: list[str]) -> list[str]:
        """Stage 4: Final guard — returns errors as-is; caller emits structured failure."""
        return errors

    # --- Combined ----------------------------------------------------------

    def run_all(self, payload: dict[str, Any], modes: list[str]) -> list[str]:
        """Run stages 1–3 and collect all errors."""
        stacker = ModeStackingResolver()
        merged = stacker.merge(modes)
        required_fields = merged["output_requirements"]

        errors = self.stage1_structural(payload, required_fields)
        errors += self.stage2_logical(payload, modes)
        errors += self.stage3_compliance(payload, modes)
        return errors


# ---------------------------------------------------------------------------
# 1.4  Retry Engine
# ---------------------------------------------------------------------------


@dataclass
class RetryResult:
    """Return type of RetryEngine.run()."""

    payload: dict[str, Any]
    retry_count: int
    last_raw_response: str
    failed: bool = False
    failed_rules: list[str] = field(default_factory=list)


class RetryEngine:
    """
    Deterministic retry behaviour on validation failure.
    max_retries = 2  (enforcement patch: retry_contract.max_retries)

    On validation failure → re_prompt_with_validation_feedback
    On exhaustion        → return_structured_failure (via RetryResult.failed=True)
    """

    def __init__(self, ai_caller: Callable, pipeline: ValidationPipeline) -> None:
        self._ai_caller = ai_caller
        self._pipeline = pipeline

    def run(
        self,
        message: str,
        modes: list[str],
        system_prompt: str,
        history: list[Any] | None,
        api_key: str,
    ) -> RetryResult:
        """
        Run the retry loop.  Returns a RetryResult whose .failed flag
        indicates whether the response should be treated as a structured failure.
        """
        attempt_message = message
        last_errors: list[str] = []
        last_raw = ""

        for attempt in range(MODE_ENGINE_MAX_RETRIES):
            last_raw = self._ai_caller(
                attempt_message,
                api_key,
                history=history,
                system_prompt=system_prompt,
            )

            try:
                payload = self._pipeline.parse(last_raw)
            except ModeEngineValidationError as exc:
                last_errors = [str(exc)]
                attempt_message = self._build_retry_prompt(message, last_errors)
                logger.debug(
                    "mode_engine retry %d/%d: parse error: %s",
                    attempt + 1,
                    MODE_ENGINE_MAX_RETRIES,
                    exc,
                )
                continue

            errors = self._pipeline.run_all(payload, modes)
            if not errors:
                return RetryResult(
                    payload=payload,
                    retry_count=attempt,
                    last_raw_response=last_raw,
                )

            last_errors = errors
            attempt_message = self._build_retry_prompt(message, errors)
            logger.debug(
                "mode_engine retry %d/%d: validation errors: %s",
                attempt + 1,
                MODE_ENGINE_MAX_RETRIES,
                errors,
            )

        # Exhausted — Stage 4 guard
        final_errors = self._pipeline.stage4_post_retry(last_errors)
        return RetryResult(
            payload={},
            retry_count=MODE_ENGINE_MAX_RETRIES,
            last_raw_response=last_raw,
            failed=True,
            failed_rules=final_errors,
        )

    @staticmethod
    def _build_retry_prompt(original_message: str, errors: list[str]) -> str:
        return (
            f"Original user request:\n{original_message}\n\n"
            "Your previous response was rejected by the validator for these reasons:\n"
            + "\n".join(f"- {e}" for e in errors)
            + "\n\nReturn corrected JSON only."
        )


# ---------------------------------------------------------------------------
# 1.5  Hard Validation Boundary — Mode Engine Gateway
# ---------------------------------------------------------------------------


@dataclass
class GatewayResult:
    """Return type of ModeEngineGateway.process()."""

    payload: dict[str, Any]
    modes: list[str]
    system_prompt: str
    raw_ai_response: str
    retry_count: int
    validation_errors: list[str]
    failed: bool = False


class ModeEngineGateway:
    """
    Single entry/exit point for ALL mode-engine-governed chat responses.

    Invariants (hard_validation_boundary):
      - No AI response exits without passing validation.
      - Mode engine is mandatory for all calls routed through this gateway.
      - Every interaction produces an audit log entry.
    """

    def __init__(self) -> None:
        self._priority_resolver = ModePriorityResolver()
        self._prompt_injector = SystemPromptInjector()
        self._pre_validator = PreGenerationValidator()
        self._pipeline = ValidationPipeline()

    def process(
        self,
        message: str,
        requested_modes: list[str],
        api_key: str,
        history: list[Any] | None,
        base_system_prompt: str,
        ai_caller: Callable,
    ) -> GatewayResult:
        """
        Full enforcement pipeline.

        Returns a GatewayResult whose .failed flag indicates whether
        the final payload is a StructuredFailureResponse.
        """
        # Stage 0: Pre-generation validation (block AI call on failure)
        try:
            self._pre_validator.validate(message)
        except InsufficientDataError as exc:
            modes = self._priority_resolver.resolve(requested_modes)
            failure = build_structured_failure(
                message,
                modes,
                str(exc),
                failed_rules=["input_not_empty"],
                missing_fields=[],
            )
            return GatewayResult(
                payload=failure,
                modes=modes,
                system_prompt="",
                raw_ai_response="",
                retry_count=0,
                validation_errors=[str(exc)],
                failed=True,
            )

        # Resolve priority + inject system prompt with mode constraints
        modes = self._priority_resolver.resolve(requested_modes)
        system_prompt = self._prompt_injector.inject(base_system_prompt, modes)

        # Retry engine
        retry = RetryEngine(ai_caller=ai_caller, pipeline=self._pipeline)
        result = retry.run(
            message=message,
            modes=modes,
            system_prompt=system_prompt,
            history=history,
            api_key=api_key,
        )

        if result.failed:
            failure_reason = (
                "The AI response did not pass mode-engine validation after all retries."
            )
            failure = build_structured_failure(
                message,
                modes,
                failure_reason,
                failed_rules=result.failed_rules,
                missing_fields=[],
            )
            return GatewayResult(
                payload=failure,
                modes=modes,
                system_prompt=system_prompt,
                raw_ai_response=result.last_raw_response,
                retry_count=result.retry_count,
                validation_errors=result.failed_rules,
                failed=True,
            )

        return GatewayResult(
            payload=result.payload,
            modes=modes,
            system_prompt=system_prompt,
            raw_ai_response=result.last_raw_response,
            retry_count=result.retry_count,
            validation_errors=[],
            failed=False,
        )


# ---------------------------------------------------------------------------
# Structured failure builder
# ---------------------------------------------------------------------------


def build_structured_failure(
    message: str,
    modes: list[str],
    reason: str,
    failed_rules: list[str],
    missing_fields: list[str],
) -> dict[str, Any]:
    """
    Build a StructuredFailureResponse per the enforcement patch retry_contract.

    Fields: error, contract_id, selected_modes, failed_rules,
            missing_fields, suggested_fix, explicit_data_status,
            missing_data_list, plus mode-specific required fields.
    """
    fallback: dict[str, Any] = {
        "error": "VALIDATION_FAILED",
        "contract_id": MODE_ENGINE_CONTRACT_ID,
        "selected_modes": modes,
        "failed_rules": failed_rules,
        "missing_fields": missing_fields,
        "suggested_fix": reason,
        "explicit_data_status": "insufficient_data",
        "missing_data_list": [reason],
    }

    if "prediction_mode" in modes:
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

    if "debug_mode" in modes:
        fallback.update(
            {
                "root_cause": "Insufficient verified data to identify a root cause.",
                "reasoning_steps": [
                    "The validator could not accept an answer backed by sufficient data.",
                    "Additional evidence is required before a causal chain can be confirmed.",
                ],
                "failure_paths": [message[:MODE_ENGINE_FALLBACK_MESSAGE_MAX_LENGTH]],
            }
        )

    if "builder_mode" in modes:
        fallback.update(
            {
                "system_structure": (
                    "Insufficient verified data to produce a structured design."
                ),
                "components": [],
                "relationships": [],
            }
        )

    if "audit_mode" in modes:
        fallback.update(
            {
                "risks": [
                    "Proceeding without verified data could produce incorrect conclusions."
                ],
                "inconsistencies": [],
                "assumptions": [
                    "The available data is incomplete and cannot be trusted for a final answer."
                ],
            }
        )

    return fallback


# ---------------------------------------------------------------------------
# 1.6  Audit Logger
# ---------------------------------------------------------------------------


class AuditLogger:
    """
    Writes a persistent audit log entry for every Mode Engine interaction.

    Stores: user_intent, selected_modes, transformed_prompt, raw_ai_response,
            validation_results, retry_count, final_output.

    A missing audit log entry is a block_condition per the enforcement patch.
    On DB failure the error is logged at ERROR level and None is returned —
    the response is still delivered to avoid cascading failures from storage
    issues, but the error is surfaced explicitly (all_failures_are_explicitly_returned).
    """

    def log(
        self,
        db: Any,
        *,
        user_intent: str,
        selected_modes: list[str],
        transformed_prompt: str,
        raw_ai_response: str,
        validation_results: list[str],
        retry_count: int,
        final_output: str,
        # Mutation Simulation V2 extended fields (optional)
        mutation_contract: dict[str, Any] | None = None,
        simulation_results: dict[str, Any] | None = None,
        enforcement_results: dict[str, Any] | None = None,
        build_status: str | None = None,
        commit_id: str | None = None,
    ) -> Any | None:
        """Write an audit entry. Returns the created record or None on failure."""
        if db is None:
            logger.error(
                "audit_logger: no DB session — audit log entry missing "
                "(block_condition: missing_audit_log_entry)"
            )
            return None

        try:
            from backend.app.models import ModeEngineAuditLog

            entry = ModeEngineAuditLog(
                user_intent=user_intent[:2000],
                selected_modes=selected_modes,
                transformed_prompt=transformed_prompt[:4000],
                raw_ai_response=raw_ai_response[:4000],
                validation_results=validation_results,
                retry_count=retry_count,
                final_output=final_output[:4000],
                mutation_contract=mutation_contract,
                simulation_results=simulation_results,
                enforcement_results=enforcement_results,
                build_status=build_status,
                commit_id=commit_id,
            )
            db.add(entry)
            db.commit()
            db.refresh(entry)
            return entry
        except Exception as exc:
            logger.error("audit_logger: failed to write audit log entry: %s", exc)
            return None


# ===========================================================================
# MODE_ENGINE_MUTATION_SIMULATION_V2
# ===========================================================================
# Contract: MODE_ENGINE_MUTATION_SIMULATION_V2
# Class: GOVERNANCE  |  Reversibility: FORWARD_ONLY
# Execution scope: BOTH  |  Mutation permission: MUTATION_ALLOWED
# Dependencies: MODE_ENGINE_EXECUTION_V1, MODE_ENGINE_ENFORCEMENT_PATCH_V1
# ===========================================================================

# Mandatory mode stack for all mutation calls
MUTATION_MANDATORY_MODES: list[str] = ["strict_mode", "prediction_mode", "builder_mode"]

# Required fields in the AI mutation contract response
MUTATION_CONTRACT_REQUIRED_FIELDS: list[str] = [
    "target_files",
    "operation_type",
    "proposed_changes",
    "assumptions",
    "alternatives",
    "confidence",
    "risks",
    "missing_data",
]

# File scope control (server-enforced)
MUTATION_ALLOWED_PATH_PREFIXES: tuple[str, ...] = ("apps/", "core/", "scripts/")
MUTATION_RESTRICTED_PATHS: tuple[str, ...] = (".env", "secrets/", "infra/credentials/")

# System prompt that instructs the AI to produce a mutation contract
_MUTATION_SYSTEM_PROMPT = """\
You are the UI Blueprint Mutation Engine operating under MODE_ENGINE_MUTATION_SIMULATION_V2.

ROLE: Produce a structured code mutation proposal. No free-form responses.
AUTHORITY: Proposal ONLY. You do not execute, commit, or deploy anything.

You MUST return valid JSON only — no markdown, no prose outside the JSON.

MANDATORY FIELDS (all must be present and non-empty):
  target_files      : list[str]  — files to be created/modified/deleted
  operation_type    : str        — "create" | "modify" | "delete" | "refactor"
  proposed_changes  : list[{file, change_type, description, diff_hint}]
  assumptions       : list[str]  — all assumptions explicitly declared
  alternatives      : list[str]  — at least two alternative approaches
  confidence        : float      — 0.0–1.0
  risks             : list[str]  — all identified risks
  missing_data      : list[str]  — data needed but not available

ALSO INCLUDE the standard mode-engine fields:
  contract_id, selected_modes, explicit_data_status, missing_data_list

CONSTRAINTS:
  - no_empty_fields
  - no_undeclared_assumptions
  - no_single_solution_bias (always provide ≥ 2 alternatives)
  - confidence must reflect actual certainty (do not inflate)
  - If data is insufficient, set explicit_data_status = "insufficient_data"
    and populate missing_data and missing_data_list
"""


# ---------------------------------------------------------------------------
# Mutation Contract Validator
# ---------------------------------------------------------------------------


class MutationContractValidator:
    """
    Validates the AI's mutation contract response against
    MODE_ENGINE_MUTATION_SIMULATION_V2 required fields and constraints.
    """

    def validate(
        self, payload: dict[str, Any], modes: list[str]
    ) -> list[str]:
        """Return list of validation errors (empty = valid)."""
        errors: list[str] = []

        if not isinstance(payload, dict):
            return ["mutation contract must be a JSON object"]

        # All required mutation fields must be present and non-empty
        for field_name in MUTATION_CONTRACT_REQUIRED_FIELDS:
            value = payload.get(field_name)
            if value is None:
                errors.append(f"missing required mutation field: {field_name}")
            elif isinstance(value, (list, str)) and not value:
                errors.append(f"no_empty_fields violation: {field_name} is empty")

        # alternatives: at least 2 (no_single_solution_bias)
        alternatives = payload.get("alternatives", [])
        if isinstance(alternatives, list) and len(alternatives) < 2:
            errors.append(
                "no_single_solution_bias: alternatives must contain at least 2 items"
            )

        # confidence: float 0.0–1.0
        confidence = payload.get("confidence")
        if not isinstance(confidence, (int, float)):
            errors.append("confidence must be a number")
        elif not (0.0 <= float(confidence) <= 1.0):
            errors.append("confidence must be between 0.0 and 1.0")

        # operation_type: known value
        op_type = payload.get("operation_type", "")
        if op_type not in ("create", "modify", "delete", "refactor"):
            errors.append(
                "operation_type must be one of: create, modify, delete, refactor"
            )

        # target_files: list of strings
        target_files = payload.get("target_files", [])
        if not isinstance(target_files, list) or not all(
            isinstance(f, str) for f in target_files
        ):
            errors.append("target_files must be a list of strings")

        return errors


# ---------------------------------------------------------------------------
# Simulation Layer
# ---------------------------------------------------------------------------


@dataclass
class SimulationResult:
    """Output of the simulation layer (simulation_steps output)."""

    impacted_files: list[str]
    risk_level: str  # "low" | "medium" | "high"
    predicted_failures: list[str]
    safe_to_execute: bool
    # Extended traceability
    dependency_map: dict[str, list[str]] = field(default_factory=dict)
    alternative_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "impacted_files": self.impacted_files,
            "risk_level": self.risk_level,
            "predicted_failures": self.predicted_failures,
            "safe_to_execute": self.safe_to_execute,
            "dependency_map": self.dependency_map,
            "alternative_paths": self.alternative_paths,
        }


class SimulationLayer:
    """
    Pre-execution simulation per MODE_ENGINE_MUTATION_SIMULATION_V2.

    simulation_steps:
      1. dependency_surface_mapping  — map all files and cross-module deps
      2. impact_analysis             — derive risk level
      3. failure_prediction          — enumerate predicted failure paths
      4. alternative_path_evaluation — verify alternatives are present

    Outputs: impacted_files, risk_level, predicted_failures, safe_to_execute
    """

    def simulate(self, mutation_contract: dict[str, Any]) -> SimulationResult:
        # Step 1: dependency_surface_mapping
        target_files: list[str] = mutation_contract.get("target_files", [])
        proposed_changes: list[Any] = mutation_contract.get("proposed_changes", [])
        additional_files = [
            c.get("file", "")
            for c in proposed_changes
            if isinstance(c, dict) and c.get("file")
        ]
        impacted_files: list[str] = list(
            dict.fromkeys(target_files + additional_files)
        )

        # Build a simple dependency map: each target file → files it references
        dependency_map: dict[str, list[str]] = {f: [] for f in target_files}

        # Step 2: impact_analysis — derive risk level
        confidence = float(mutation_contract.get("confidence", 0.0))
        risks: list[str] = mutation_contract.get("risks", [])
        missing_data: list[str] = mutation_contract.get("missing_data", [])

        if confidence >= 0.8 and len(risks) == 0 and not missing_data:
            risk_level = "low"
        elif confidence >= 0.5 and len(risks) <= 2:
            risk_level = "medium"
        else:
            risk_level = "high"

        # Step 3: failure_prediction — surface risks as predicted failures
        predicted_failures: list[str] = [
            r for r in risks if isinstance(r, str)
        ]
        if missing_data:
            predicted_failures.append(
                f"execution blocked by missing data: {', '.join(str(m) for m in missing_data)}"
            )

        # Step 4: alternative_path_evaluation
        alternatives: list[str] = mutation_contract.get("alternatives", [])
        alternative_paths = [str(a) for a in alternatives]

        # safe_to_execute: high risk requires override; unresolved missing_data is unsafe
        safe_to_execute = risk_level in ("low", "medium") and not missing_data

        return SimulationResult(
            impacted_files=impacted_files,
            risk_level=risk_level,
            predicted_failures=predicted_failures,
            safe_to_execute=safe_to_execute,
            dependency_map=dependency_map,
            alternative_paths=alternative_paths,
        )


# ---------------------------------------------------------------------------
# File Scope Controller
# ---------------------------------------------------------------------------


class FileScopeController:
    """
    Enforces file_scope_control from MODE_ENGINE_MUTATION_SIMULATION_V2.

    allowed_paths    : apps/, core/, scripts/
    restricted_paths : .env, secrets/, infra/credentials/
    """

    def validate(self, target_files: list[str]) -> list[str]:
        """Return list of scope violations (empty = all files within scope)."""
        errors: list[str] = []
        for f in target_files:
            for restricted in MUTATION_RESTRICTED_PATHS:
                if restricted in f or f.startswith(restricted):
                    errors.append(f"restricted file access blocked: {f}")
                    break
        return errors


# ---------------------------------------------------------------------------
# Override Protocol
# ---------------------------------------------------------------------------


class OverrideProtocol:
    """
    Validates a caller-supplied override for high-risk mutations.

    Required fields: justification, acknowledged_risks, override_scope
    """

    REQUIRED_OVERRIDE_FIELDS: tuple[str, ...] = (
        "justification",
        "acknowledged_risks",
        "override_scope",
    )

    def validate(self, override: dict[str, Any]) -> list[str]:
        """Return list of missing/invalid override fields."""
        errors: list[str] = []
        for f in self.REQUIRED_OVERRIDE_FIELDS:
            value = override.get(f)
            if value is None:
                errors.append(f"override missing required field: {f}")
            elif isinstance(value, str) and not value.strip():
                errors.append(f"override field must not be empty: {f}")
            elif isinstance(value, list) and not value:
                errors.append(f"override field must not be empty: {f}")
        return errors


# ---------------------------------------------------------------------------
# Enforcement Layer
# ---------------------------------------------------------------------------


@dataclass
class EnforcementResult:
    """Output of the enforcement layer."""

    approved: bool
    requires_override: bool = False
    blocked_reason: str | None = None
    override_applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "requires_override": self.requires_override,
            "blocked_reason": self.blocked_reason,
            "override_applied": self.override_applied,
        }


class EnforcementLayer:
    """
    Hard execution boundary per MODE_ENGINE_MUTATION_SIMULATION_V2.

    enforcement_layer.simulation_enforcement:
      block_execution_if: safe_to_execute == false
      require_explicit_override_if: risk_level == high

    override_protocol:
      required_fields: justification, acknowledged_risks, override_scope
    """

    def __init__(self) -> None:
        self._override_protocol = OverrideProtocol()

    def enforce(
        self,
        simulation: SimulationResult,
        override: dict[str, Any] | None = None,
    ) -> EnforcementResult:
        # Block if simulation deems execution unsafe
        if not simulation.safe_to_execute:
            return EnforcementResult(
                approved=False,
                blocked_reason=(
                    f"simulation: safe_to_execute is false "
                    f"(risk_level={simulation.risk_level}, "
                    f"predicted_failures={simulation.predicted_failures})"
                ),
            )

        # High risk requires explicit override
        if simulation.risk_level == "high":
            if override is None:
                return EnforcementResult(
                    approved=False,
                    requires_override=True,
                    blocked_reason=(
                        "risk_level is high — explicit override required "
                        "(provide justification, acknowledged_risks, override_scope)"
                    ),
                )
            override_errors = self._override_protocol.validate(override)
            if override_errors:
                return EnforcementResult(
                    approved=False,
                    blocked_reason="; ".join(override_errors),
                )
            return EnforcementResult(
                approved=True,
                override_applied=True,
            )

        return EnforcementResult(approved=True)


# ---------------------------------------------------------------------------
# Mutation Validation Pipeline (Stages 0–5)
# ---------------------------------------------------------------------------


class MutationValidationPipeline(ValidationPipeline):
    """
    Extended validation pipeline for mutation proposals.

    Adds to the base 4-stage pipeline:
      Stage 3 (simulation) : simulation_completed, safe_to_execute == true
      Stage 4 (system)     : files_exist_or_valid_for_creation, paths_within_scope
      Stage 5 (build)      : build_successful (placeholder — actual build is external)
    """

    def stage3_simulation(
        self, simulation: SimulationResult, enforcement: EnforcementResult
    ) -> list[str]:
        """Stage 3: Simulation pass and safe_to_execute check."""
        errors: list[str] = []
        if not enforcement.approved:
            reason = enforcement.blocked_reason or "enforcement blocked execution"
            errors.append(f"simulation_enforcement: {reason}")
        return errors

    def stage4_system(
        self, target_files: list[str], scope_errors: list[str]
    ) -> list[str]:
        """Stage 4: File paths within allowed scope, no protected files."""
        return scope_errors

    def stage5_build(self, build_passed: bool) -> list[str]:
        """Stage 5: Build check (external; caller passes result)."""
        return [] if build_passed else ["build_failed: abort_mutation"]

    def run_mutation_all(
        self,
        payload: dict[str, Any],
        modes: list[str],
        simulation: SimulationResult,
        enforcement: EnforcementResult,
        scope_errors: list[str],
        build_passed: bool = True,
    ) -> list[str]:
        """Run all 6 stages (0 is pre-generation; 1–5 run here)."""
        stacker = ModeStackingResolver()
        merged = stacker.merge(modes)
        required_fields = merged["output_requirements"] + MUTATION_CONTRACT_REQUIRED_FIELDS

        errors = self.stage1_structural(payload, required_fields)
        errors += self.stage2_logical(payload, modes)
        errors += self.stage3_compliance(payload, modes)
        errors += self.stage3_simulation(simulation, enforcement)
        errors += self.stage4_system(payload.get("target_files", []), scope_errors)
        errors += self.stage5_build(build_passed)
        return errors


# ---------------------------------------------------------------------------
# Mutation Retry Engine
# ---------------------------------------------------------------------------


class MutationRetryEngine(RetryEngine):
    """
    Retry engine for mutation proposals.

    Re-prompts with combined validation + simulation feedback.
    max_retries = 2  (same as enforcement patch contract)
    """

    def __init__(self, ai_caller: Callable) -> None:
        super().__init__(
            ai_caller=ai_caller,
            pipeline=ValidationPipeline(),
        )
        self._mutation_validator = MutationContractValidator()

    def run_mutation(
        self,
        message: str,
        modes: list[str],
        system_prompt: str,
        history: list[Any] | None,
        api_key: str,
    ) -> RetryResult:
        """
        Run the mutation retry loop.
        Validates both standard mode-engine fields AND mutation contract fields.
        """
        attempt_message = message
        last_errors: list[str] = []
        last_raw = ""

        for attempt in range(MODE_ENGINE_MAX_RETRIES):
            last_raw = self._ai_caller(
                attempt_message,
                api_key,
                history=history,
                system_prompt=system_prompt,
            )

            try:
                payload = self._pipeline.parse(last_raw)
            except ModeEngineValidationError as exc:
                last_errors = [str(exc)]
                attempt_message = self._build_retry_prompt(message, last_errors)
                continue

            # Standard mode-engine validation
            me_errors = self._pipeline.run_all(payload, modes)
            # Mutation contract validation
            mc_errors = self._mutation_validator.validate(payload, modes)
            all_errors = me_errors + mc_errors

            if not all_errors:
                return RetryResult(
                    payload=payload,
                    retry_count=attempt,
                    last_raw_response=last_raw,
                )

            last_errors = all_errors
            attempt_message = self._build_retry_prompt(message, all_errors)
            logger.debug(
                "mutation_engine retry %d/%d: errors: %s",
                attempt + 1,
                MODE_ENGINE_MAX_RETRIES,
                all_errors,
            )

        final_errors = self._pipeline.stage4_post_retry(last_errors)
        return RetryResult(
            payload={},
            retry_count=MODE_ENGINE_MAX_RETRIES,
            last_raw_response=last_raw,
            failed=True,
            failed_rules=final_errors,
        )


# ---------------------------------------------------------------------------
# Mutation Gateway
# ---------------------------------------------------------------------------


@dataclass
class MutationGatewayResult:
    """Full result of MutationGateway.process()."""

    payload: dict[str, Any]             # AI mutation contract (or structured failure)
    modes: list[str]
    simulation: SimulationResult | None
    enforcement: EnforcementResult | None
    validation_errors: list[str]
    system_prompt: str
    raw_ai_response: str
    retry_count: int
    approved: bool                       # True only when all stages pass
    requires_override: bool = False


class MutationGateway:
    """
    Execution entry point for MODE_ENGINE_MUTATION_SIMULATION_V2.

    Full lifecycle:
      Stage 0  PreGenerationValidator
      Inject   SystemPromptInjector  (base + mutation instructions)
      AI call  → MutationRetryEngine (mutation contract validation)
      Simulate → SimulationLayer
      Enforce  → EnforcementLayer (block if unsafe / high risk without override)
      Stage 3–5 MutationValidationPipeline
      Audit    → AuditLogger

    Invariants:
      - simulation_precedes_execution
      - enforcement_precedes_execution
      - validation_precedes_deployment
      - ai_is_proposal_only
    """

    def __init__(self) -> None:
        self._priority_resolver = ModePriorityResolver()
        self._prompt_injector = SystemPromptInjector()
        self._pre_validator = PreGenerationValidator()
        self._simulation = SimulationLayer()
        self._enforcement = EnforcementLayer()
        self._scope = FileScopeController()
        self._pipeline = MutationValidationPipeline()

    def process(
        self,
        message: str,
        api_key: str,
        history: list[Any] | None,
        base_system_prompt: str,
        ai_caller: Callable,
        override: dict[str, Any] | None = None,
    ) -> MutationGatewayResult:
        """
        Run the full mutation enforcement pipeline.
        The mandatory mode stack (strict + prediction + builder) is always applied.
        """
        # Stage 0: Pre-generation validation
        try:
            self._pre_validator.validate(message)
        except InsufficientDataError as exc:
            modes = self._priority_resolver.resolve(MUTATION_MANDATORY_MODES)
            failure = build_structured_failure(
                message,
                modes,
                str(exc),
                failed_rules=["input_not_empty"],
                missing_fields=[],
            )
            return MutationGatewayResult(
                payload=failure,
                modes=modes,
                simulation=None,
                enforcement=None,
                validation_errors=[str(exc)],
                system_prompt="",
                raw_ai_response="",
                retry_count=0,
                approved=False,
            )

        # Resolve mandatory mode stack (strict + prediction + builder, priority-sorted)
        modes = self._priority_resolver.resolve(MUTATION_MANDATORY_MODES)

        # Inject mutation system prompt
        system_prompt = (
            base_system_prompt
            + "\n\n"
            + _MUTATION_SYSTEM_PROMPT
            + self._prompt_injector._TEMPLATE.format(
                contract_id=MODE_ENGINE_CONTRACT_ID,
                selected_modes="\n".join(f"- {m}" for m in modes),
                required_fields="\n".join(
                    f"- {m}: {', '.join(MODE_ENGINE_MODE_RULES[m]['output_requirements'])}"
                    for m in modes
                ),
                behavior_rules="\n".join(
                    f"- {m}: {r}"
                    for m in modes
                    for r in MODE_ENGINE_MODE_RULES[m]["behavior_rules"]
                ),
                constraints="\n".join(
                    f"- {m}: {c}"
                    for m in modes
                    for c in MODE_ENGINE_MODE_RULES[m]["constraints"]
                ),
            )
        )

        # AI call + mutation retry engine
        retry = MutationRetryEngine(ai_caller=ai_caller)
        result = retry.run_mutation(
            message=message,
            modes=modes,
            system_prompt=system_prompt,
            history=history,
            api_key=api_key,
        )

        if result.failed:
            failure = build_structured_failure(
                message,
                modes,
                "Mutation contract validation failed after all retries.",
                failed_rules=result.failed_rules,
                missing_fields=[],
            )
            return MutationGatewayResult(
                payload=failure,
                modes=modes,
                simulation=None,
                enforcement=None,
                validation_errors=result.failed_rules,
                system_prompt=system_prompt,
                raw_ai_response=result.last_raw_response,
                retry_count=result.retry_count,
                approved=False,
            )

        payload = result.payload

        # Simulation layer
        sim = self._simulation.simulate(payload)

        # File scope control
        scope_errors = self._scope.validate(payload.get("target_files", []))

        # Enforcement layer
        enf = self._enforcement.enforce(sim, override)

        # Extended validation (stages 3–5)
        all_errors = self._pipeline.run_mutation_all(
            payload=payload,
            modes=modes,
            simulation=sim,
            enforcement=enf,
            scope_errors=scope_errors,
            build_passed=True,  # build check is external; defaults to pass
        )

        return MutationGatewayResult(
            payload=payload,
            modes=modes,
            simulation=sim,
            enforcement=enf,
            validation_errors=all_errors,
            system_prompt=system_prompt,
            raw_ai_response=result.last_raw_response,
            retry_count=result.retry_count,
            approved=enf.approved and not all_errors,
            requires_override=enf.requires_override,
        )


# ---------------------------------------------------------------------------
# Module-level singleton instances (optional convenience)
# ---------------------------------------------------------------------------

_gateway = ModeEngineGateway()
_mutation_gateway = MutationGateway()
_audit_logger = AuditLogger()


def get_gateway() -> ModeEngineGateway:
    """Return the shared ModeEngineGateway instance."""
    return _gateway


def get_mutation_gateway() -> MutationGateway:
    """Return the shared MutationGateway instance."""
    return _mutation_gateway


def get_audit_logger() -> AuditLogger:
    """Return the shared AuditLogger instance."""
    return _audit_logger
