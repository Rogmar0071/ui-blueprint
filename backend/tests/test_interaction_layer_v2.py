"""
Tests for INTERACTION_LAYER_V2 (POST /api/chat/intent).

Covers:
- Mode A output defaults when repo context is absent
- Mode B output when repo context is provided
- Determinism gates produce canExecuteDeterministically=false appropriately
- Output is parseable JSON and includes all required keys
- OpenAI key absent → deterministic Mode A fallback (no 503)
- OpenAI key present → calls _call_openai_intent_v2 and validates result
"""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data_v2")

from backend.app.main import app  # noqa: E402

TOKEN = "test-secret-key"

# ---------------------------------------------------------------------------
# Required top-level keys in every IntentV2 response
# ---------------------------------------------------------------------------

_REQUIRED_KEYS = {
    "schemaVersion",
    "intentId",
    "mode",
    "repoContextProvided",
    "intent",
    "structuralIntent",
    "impactAnalysis",
    "changePlan",
}

_REQUIRED_INTENT_KEYS = {"objective", "interpretedMeaning"}
_REQUIRED_STRUCTURAL_KEYS = {"operationType", "targetLayer", "scope"}
_REQUIRED_IMPACT_KEYS = {"affectedComponents", "riskLevel", "requiresRepoContext", "uncertainties"}
_REQUIRED_CHANGE_PLAN_KEYS = {
    "canExecuteDeterministically",
    "requiresStructuralMapping",
    "steps",
    "blockedReason",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _configure_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path):
    db_path = tmp_path / "test_v2.db"
    db_url = f"sqlite:///{db_path}"

    import backend.app.database as db_module

    db_module.reset_engine(db_url)
    db_module.init_db()
    monkeypatch.setenv("DATABASE_URL", db_url)

    yield

    db_module.reset_engine()


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", TOKEN)
    import backend.app.main as m

    monkeypatch.setattr(m, "API_KEY", TOKEN)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


def _auth() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def _post_intent(
    client: TestClient,
    message: str,
    repo_context: dict | None = None,
) -> dict:
    body: dict = {"message": message}
    if repo_context is not None:
        body["repo_context"] = repo_context
    resp = client.post("/api/chat/intent", json=body, headers=_auth())
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Helper: make a fake OpenAI response for the intent v2 endpoint
# ---------------------------------------------------------------------------


def _make_openai_intent_response(content: dict) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps(content)}}]
    }
    return mock_resp


def _valid_mode_b_llm_payload(message: str = "Add dark-mode toggle") -> dict:
    """Minimal valid Mode B JSON that the LLM would return."""
    return {
        "schemaVersion": "2",
        "intentId": str(uuid.uuid4()),
        "mode": "B",
        "repoContextProvided": True,
        "intent": {
            "objective": message,
            "interpretedMeaning": f"User wants to: {message}",
        },
        "structuralIntent": {
            "operationType": "modify",
            "targetLayer": "ui",
            "scope": "Settings screen component",
        },
        "impactAnalysis": {
            "affectedComponents": ["SettingsScreen", "ThemeProvider"],
            "riskLevel": "low",
            "requiresRepoContext": False,
            "uncertainties": [],
        },
        "changePlan": {
            "canExecuteDeterministically": True,
            "requiresStructuralMapping": False,
            "steps": [
                {
                    "stepId": "step-1",
                    "description": "Add toggle switch to SettingsScreen",
                    "targetFile": "src/Settings.tsx",
                }
            ],
            "blockedReason": None,
        },
    }


# ---------------------------------------------------------------------------
# Tests: required JSON keys are always present
# ---------------------------------------------------------------------------


class TestRequiredKeys:
    def test_mode_a_contains_all_required_keys(self, client, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        body = _post_intent(client, "Build me a todo app")

        assert _REQUIRED_KEYS.issubset(body.keys()), (
            f"Missing keys: {_REQUIRED_KEYS - body.keys()}"
        )

    def test_intent_subkeys(self, client, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        body = _post_intent(client, "Some request")
        assert _REQUIRED_INTENT_KEYS.issubset(body["intent"].keys())

    def test_structural_intent_subkeys(self, client, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        body = _post_intent(client, "Some request")
        assert _REQUIRED_STRUCTURAL_KEYS.issubset(body["structuralIntent"].keys())

    def test_impact_analysis_subkeys(self, client, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        body = _post_intent(client, "Some request")
        assert _REQUIRED_IMPACT_KEYS.issubset(body["impactAnalysis"].keys())

    def test_change_plan_subkeys(self, client, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        body = _post_intent(client, "Some request")
        assert _REQUIRED_CHANGE_PLAN_KEYS.issubset(body["changePlan"].keys())

    def test_schema_version_is_2(self, client, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        body = _post_intent(client, "hello")
        assert body["schemaVersion"] == "2"

    def test_intent_id_is_uuid(self, client, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        body = _post_intent(client, "hello")
        # Should be parseable as a UUID.
        uuid.UUID(body["intentId"])


# ---------------------------------------------------------------------------
# Tests: Mode A — no repo context
# ---------------------------------------------------------------------------


class TestModeA:
    def test_mode_is_a_when_no_repo_context(self, client, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        body = _post_intent(client, "Build a login form")
        assert body["mode"] == "A"

    def test_repo_context_provided_is_false(self, client, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        body = _post_intent(client, "Build a login form")
        assert body["repoContextProvided"] is False

    def test_requires_repo_context_is_true(self, client, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        body = _post_intent(client, "Build a login form")
        assert body["impactAnalysis"]["requiresRepoContext"] is True

    def test_cannot_execute_deterministically_in_mode_a(self, client, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        body = _post_intent(client, "Build a login form")
        assert body["changePlan"]["canExecuteDeterministically"] is False

    def test_requires_structural_mapping_in_mode_a(self, client, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        body = _post_intent(client, "Some change")
        assert body["changePlan"]["requiresStructuralMapping"] is True

    def test_steps_empty_in_mode_a(self, client, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        body = _post_intent(client, "Some change")
        assert body["changePlan"]["steps"] == []

    def test_blocked_reason_present_in_mode_a(self, client, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        body = _post_intent(client, "Some change")
        assert body["changePlan"]["blockedReason"] is not None
        assert len(body["changePlan"]["blockedReason"]) > 0

    def test_objective_contains_message(self, client, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        msg = "Build a login form with email and password"
        body = _post_intent(client, msg)
        # At least one of the intent fields must contain the original message text.
        objective = body["intent"]["objective"]
        interpreted = body["intent"]["interpretedMeaning"]
        assert (
            msg in objective or msg in interpreted
        ), (
            f"Message not found in intent fields: "
            f"objective={objective!r}, interpretedMeaning={interpreted!r}"
        )
        # Both fields must be non-empty strings (not just copies of each other).
        assert len(objective) > 0
        assert len(interpreted) > 0

    def test_mode_a_with_openai_key_but_repo_context_absent(self, client, monkeypatch):
        """When repo_context is omitted, mode must be A even if OpenAI key is set."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        llm_payload = _valid_mode_b_llm_payload()
        # Force mode A by removing repo_context from LLM response
        llm_payload["mode"] = "A"
        llm_payload["repoContextProvided"] = False
        llm_payload["impactAnalysis"]["requiresRepoContext"] = True
        llm_payload["impactAnalysis"]["uncertainties"] = ["no context"]
        llm_payload["changePlan"]["canExecuteDeterministically"] = False
        llm_payload["changePlan"]["requiresStructuralMapping"] = True

        with patch("backend.app.chat_routes.httpx.Client") as mock_cls:
            mock_http = MagicMock()
            mock_cls.return_value.__enter__.return_value = mock_http
            mock_http.post.return_value = _make_openai_intent_response(llm_payload)

            body = _post_intent(client, "Hello world")

        assert body["changePlan"]["canExecuteDeterministically"] is False


# ---------------------------------------------------------------------------
# Tests: Mode B — repo context provided
# ---------------------------------------------------------------------------


class TestModeB:
    def test_mode_is_b_when_repo_context_present(self, client, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
        llm_payload = _valid_mode_b_llm_payload()

        with patch("backend.app.chat_routes.httpx.Client") as mock_cls:
            mock_http = MagicMock()
            mock_cls.return_value.__enter__.return_value = mock_http
            mock_http.post.return_value = _make_openai_intent_response(llm_payload)

            body = _post_intent(
                client,
                "Add dark-mode toggle",
                repo_context={
                    "files": ["src/Settings.tsx"],
                    "components": ["SettingsScreen"],
                    "description": "React Native app",
                },
            )

        assert body["mode"] == "B"
        assert body["repoContextProvided"] is True

    def test_mode_b_can_have_steps(self, client, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
        llm_payload = _valid_mode_b_llm_payload()

        with patch("backend.app.chat_routes.httpx.Client") as mock_cls:
            mock_http = MagicMock()
            mock_cls.return_value.__enter__.return_value = mock_http
            mock_http.post.return_value = _make_openai_intent_response(llm_payload)

            body = _post_intent(
                client,
                "Add dark-mode toggle",
                repo_context={
                    "files": ["src/Settings.tsx", "src/theme.ts"],
                    "components": ["SettingsScreen", "ThemeProvider"],
                },
            )

        assert isinstance(body["changePlan"]["steps"], list)

    def test_mode_b_affected_components_populated(self, client, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
        llm_payload = _valid_mode_b_llm_payload()

        with patch("backend.app.chat_routes.httpx.Client") as mock_cls:
            mock_http = MagicMock()
            mock_cls.return_value.__enter__.return_value = mock_http
            mock_http.post.return_value = _make_openai_intent_response(llm_payload)

            body = _post_intent(
                client,
                "Add dark-mode toggle",
                repo_context={"files": ["src/Settings.tsx"], "components": ["SettingsScreen"]},
            )

        assert len(body["impactAnalysis"]["affectedComponents"]) > 0

    def test_repo_context_files_and_components_accepted(self, client, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
        llm_payload = _valid_mode_b_llm_payload()

        with patch("backend.app.chat_routes.httpx.Client") as mock_cls:
            mock_http = MagicMock()
            mock_cls.return_value.__enter__.return_value = mock_http
            mock_http.post.return_value = _make_openai_intent_response(llm_payload)

            resp = client.post(
                "/api/chat/intent",
                json={
                    "message": "Add a button",
                    "repo_context": {
                        "files": ["src/App.tsx"],
                        "components": ["App"],
                        "description": "Simple React app",
                    },
                },
                headers=_auth(),
            )

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests: Determinism gate
# ---------------------------------------------------------------------------


class TestDeterminismGate:
    def test_gate_blocks_when_no_repo_context(self, client, monkeypatch):
        """canExecuteDeterministically must be false when repoContextProvided=false."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        body = _post_intent(client, "Modify the header")
        assert body["changePlan"]["canExecuteDeterministically"] is False

    def test_gate_blocks_when_uncertainties_present(self, client, monkeypatch):
        """LLM returns uncertainties → gate must force canExecuteDeterministically=false."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        llm_payload = _valid_mode_b_llm_payload()
        # Even if LLM says true, uncertainties trigger the gate.
        llm_payload["impactAnalysis"]["uncertainties"] = ["dependency X is undefined"]
        llm_payload["changePlan"]["canExecuteDeterministically"] = True  # LLM tries to lie

        with patch("backend.app.chat_routes.httpx.Client") as mock_cls:
            mock_http = MagicMock()
            mock_cls.return_value.__enter__.return_value = mock_http
            mock_http.post.return_value = _make_openai_intent_response(llm_payload)

            body = _post_intent(
                client,
                "Change button color",
                repo_context={"files": ["src/Button.tsx"], "components": ["Button"]},
            )

        assert body["changePlan"]["canExecuteDeterministically"] is False

    def test_gate_blocks_when_structural_mapping_required(self, client, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        llm_payload = _valid_mode_b_llm_payload()
        llm_payload["changePlan"]["requiresStructuralMapping"] = True
        llm_payload["changePlan"]["canExecuteDeterministically"] = True  # LLM tries to bypass

        with patch("backend.app.chat_routes.httpx.Client") as mock_cls:
            mock_http = MagicMock()
            mock_cls.return_value.__enter__.return_value = mock_http
            mock_http.post.return_value = _make_openai_intent_response(llm_payload)

            body = _post_intent(
                client,
                "Refactor nav",
                repo_context={"files": ["src/Nav.tsx"], "components": ["Nav"]},
            )

        assert body["changePlan"]["canExecuteDeterministically"] is False

    def test_gate_blocks_when_affected_components_empty(self, client, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        llm_payload = _valid_mode_b_llm_payload()
        llm_payload["impactAnalysis"]["affectedComponents"] = []
        llm_payload["changePlan"]["canExecuteDeterministically"] = True

        with patch("backend.app.chat_routes.httpx.Client") as mock_cls:
            mock_http = MagicMock()
            mock_cls.return_value.__enter__.return_value = mock_http
            mock_http.post.return_value = _make_openai_intent_response(llm_payload)

            body = _post_intent(
                client,
                "Do something",
                repo_context={"files": ["src/App.tsx"]},
            )

        assert body["changePlan"]["canExecuteDeterministically"] is False

    def test_gate_allows_true_when_all_conditions_met(self, client, monkeypatch):
        """When all conditions are satisfied, canExecuteDeterministically may be true."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        llm_payload = _valid_mode_b_llm_payload()
        # Ensure all gate conditions are satisfied.
        assert llm_payload["repoContextProvided"] is True
        assert llm_payload["impactAnalysis"]["uncertainties"] == []
        assert llm_payload["changePlan"]["requiresStructuralMapping"] is False
        assert len(llm_payload["impactAnalysis"]["affectedComponents"]) > 0
        llm_payload["changePlan"]["canExecuteDeterministically"] = True

        with patch("backend.app.chat_routes.httpx.Client") as mock_cls:
            mock_http = MagicMock()
            mock_cls.return_value.__enter__.return_value = mock_http
            mock_http.post.return_value = _make_openai_intent_response(llm_payload)

            body = _post_intent(
                client,
                "Add dark-mode toggle",
                repo_context={
                    "files": ["src/Settings.tsx", "src/theme.ts"],
                    "components": ["SettingsScreen", "ThemeProvider"],
                },
            )

        assert body["changePlan"]["canExecuteDeterministically"] is True


# ---------------------------------------------------------------------------
# Tests: JSON output / parseable
# ---------------------------------------------------------------------------


class TestJsonOutput:
    def test_response_is_json(self, client, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        resp = client.post(
            "/api/chat/intent",
            json={"message": "hello"},
            headers=_auth(),
        )
        assert resp.status_code == 200
        # Must be valid JSON (raises if not).
        data = resp.json()
        assert isinstance(data, dict)

    def test_response_content_type_is_json(self, client, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        resp = client.post(
            "/api/chat/intent",
            json={"message": "hello"},
            headers=_auth(),
        )
        assert "application/json" in resp.headers.get("content-type", "")

    def test_markdown_fences_stripped_from_llm_response(self, client, monkeypatch):
        """LLM response wrapped in ``` fences should still parse correctly."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        inner = _valid_mode_b_llm_payload()
        fenced_text = f"```json\n{json.dumps(inner)}\n```"

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": fenced_text}}]
        }

        with patch("backend.app.chat_routes.httpx.Client") as mock_cls:
            mock_http = MagicMock()
            mock_cls.return_value.__enter__.return_value = mock_http
            mock_http.post.return_value = mock_resp

            body = _post_intent(
                client,
                "Add dark-mode toggle",
                repo_context={"files": ["src/Settings.tsx"], "components": ["SettingsScreen"]},
            )

        assert body["schemaVersion"] == "2"

    def test_openai_failure_returns_mode_a_fallback(self, client, monkeypatch):
        """When OpenAI call fails, a Mode A fallback dict is returned (no 500)."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        with patch("backend.app.chat_routes.httpx.Client") as mock_cls:
            mock_http = MagicMock()
            mock_cls.return_value.__enter__.return_value = mock_http
            mock_http.post.side_effect = Exception("connection refused")

            resp = client.post(
                "/api/chat/intent",
                json={"message": "hello"},
                headers=_auth(),
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["changePlan"]["canExecuteDeterministically"] is False

    def test_schema_version_always_2(self, client, monkeypatch):
        """schemaVersion is always forced to '2' by the server."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

        # LLM tries to set a different schema version.
        llm_payload = _valid_mode_b_llm_payload()
        llm_payload["schemaVersion"] = "99"

        with patch("backend.app.chat_routes.httpx.Client") as mock_cls:
            mock_http = MagicMock()
            mock_cls.return_value.__enter__.return_value = mock_http
            mock_http.post.return_value = _make_openai_intent_response(llm_payload)

            body = _post_intent(
                client,
                "hello",
                repo_context={"files": ["src/App.tsx"], "components": ["App"]},
            )

        assert body["schemaVersion"] == "2"


# ---------------------------------------------------------------------------
# Tests: Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_missing_message_returns_400(self, client):
        resp = client.post("/api/chat/intent", json={}, headers=_auth())
        assert resp.status_code == 400

    def test_empty_message_returns_400(self, client):
        resp = client.post(
            "/api/chat/intent",
            json={"message": "   "},
            headers=_auth(),
        )
        assert resp.status_code == 400

    def test_requires_auth(self, client):
        resp = client.post("/api/chat/intent", json={"message": "hello"})
        assert resp.status_code == 401

    def test_extra_body_fields_rejected(self, client, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        resp = client.post(
            "/api/chat/intent",
            json={"message": "hello", "unknown_field": "value"},
            headers=_auth(),
        )
        assert resp.status_code == 422

    def test_repo_context_empty_object_accepted(self, client, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        resp = client.post(
            "/api/chat/intent",
            json={"message": "hello", "repo_context": {}},
            headers=_auth(),
        )
        # Empty repo_context is treated as Mode A (no useful context).
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests: _build_intent_v2_mode_a_default (unit)
# ---------------------------------------------------------------------------


class TestModeADefaultHelper:
    def test_returns_dict_with_required_keys(self):
        from backend.app.chat_routes import _build_intent_v2_mode_a_default

        result = _build_intent_v2_mode_a_default("test message")
        assert _REQUIRED_KEYS.issubset(result.keys())

    def test_mode_is_a(self):
        from backend.app.chat_routes import _build_intent_v2_mode_a_default

        result = _build_intent_v2_mode_a_default("test")
        assert result["mode"] == "A"

    def test_cannot_execute(self):
        from backend.app.chat_routes import _build_intent_v2_mode_a_default

        result = _build_intent_v2_mode_a_default("test")
        assert result["changePlan"]["canExecuteDeterministically"] is False

    def test_intent_id_is_uuid(self):
        from backend.app.chat_routes import _build_intent_v2_mode_a_default

        result = _build_intent_v2_mode_a_default("test")
        uuid.UUID(result["intentId"])  # raises if not valid UUID


# ---------------------------------------------------------------------------
# Tests: _validate_intent_v2 (unit)
# ---------------------------------------------------------------------------


class TestValidateIntentV2:
    def test_determinism_gate_forces_false_on_no_context(self):
        from backend.app.chat_routes import _validate_intent_v2

        raw = _build_intent_v2_raw(repo_context_provided=False)
        validated = _validate_intent_v2(raw)
        assert validated.changePlan.canExecuteDeterministically is False

    def test_determinism_gate_forces_false_on_uncertainties(self):
        from backend.app.chat_routes import _validate_intent_v2

        raw = _build_intent_v2_raw(repo_context_provided=True)
        raw["impactAnalysis"]["uncertainties"] = ["something unclear"]
        raw["changePlan"]["canExecuteDeterministically"] = True
        validated = _validate_intent_v2(raw)
        assert validated.changePlan.canExecuteDeterministically is False

    def test_determinism_gate_forces_false_on_structural_mapping(self):
        from backend.app.chat_routes import _validate_intent_v2

        raw = _build_intent_v2_raw(repo_context_provided=True)
        raw["changePlan"]["requiresStructuralMapping"] = True
        raw["changePlan"]["canExecuteDeterministically"] = True
        validated = _validate_intent_v2(raw)
        assert validated.changePlan.canExecuteDeterministically is False

    def test_determinism_gate_allows_true_when_satisfied(self):
        from backend.app.chat_routes import _validate_intent_v2

        raw = _build_intent_v2_raw(repo_context_provided=True)
        raw["impactAnalysis"]["uncertainties"] = []
        raw["impactAnalysis"]["affectedComponents"] = ["ComponentA"]
        raw["changePlan"]["requiresStructuralMapping"] = False
        raw["changePlan"]["canExecuteDeterministically"] = True
        validated = _validate_intent_v2(raw)
        assert validated.changePlan.canExecuteDeterministically is True


# ---------------------------------------------------------------------------
# Helper for unit tests
# ---------------------------------------------------------------------------


def _build_intent_v2_raw(repo_context_provided: bool = True) -> dict:
    return {
        "schemaVersion": "2",
        "intentId": str(uuid.uuid4()),
        "mode": "B" if repo_context_provided else "A",
        "repoContextProvided": repo_context_provided,
        "intent": {
            "objective": "Do something",
            "interpretedMeaning": "User wants to do something",
        },
        "structuralIntent": {
            "operationType": "modify",
            "targetLayer": "ui",
            "scope": "some scope",
        },
        "impactAnalysis": {
            "affectedComponents": ["ComponentA"] if repo_context_provided else [],
            "riskLevel": "low",
            "requiresRepoContext": not repo_context_provided,
            "uncertainties": [] if repo_context_provided else ["no context"],
        },
        "changePlan": {
            "canExecuteDeterministically": repo_context_provided,
            "requiresStructuralMapping": not repo_context_provided,
            "steps": [],
            "blockedReason": None if repo_context_provided else "Repo context required",
        },
    }
