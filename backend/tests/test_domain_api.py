"""
backend.tests.test_domain_api
==============================
API integration tests for the Domain Profile + Blueprint Compiler endpoints.

Steering contract v1.1.1 required test cases:
- derive -> confirm -> compile happy path
- PATCH rejected with 409 after confirm
- PATCH/GET unknown domain id returns 404
- compile missing domain_profile_id returns 400
- derive returns 400 when media_id or media_type missing
- confirm returns 409 for non-draft profiles (non-idempotent)
- all error responses use {"error": {"code", "message"}} shape
- all success responses include top-level schema_version
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_test_data")

import backend.app.domain_routes as _dr  # noqa: E402
from backend.app.main import app  # noqa: E402
from ui_blueprint.domain.ir import SCHEMA_VERSION  # noqa: E402
from ui_blueprint.domain.store import InMemoryDomainProfileStore  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_store() -> None:
    """Reset the in-memory store before every test."""
    _dr.set_store(InMemoryDomainProfileStore())


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MEDIA = {"media_id": "vid_001", "media_type": "video"}
_OPTIONS_MECH = {"hint": "drawer hinge cabinet assembly", "max_candidates": 1}


def _derive(client: TestClient, options: dict | None = None) -> dict:
    body = {"media": _MEDIA, "options": options or _OPTIONS_MECH}
    resp = client.post("/api/domains/derive", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["candidates"], "Expected at least one candidate"
    return data["candidates"][0]


def _confirm(client: TestClient, domain_profile_id: str) -> dict:
    resp = client.post(
        f"/api/domains/{domain_profile_id}/confirm",
        json={"confirmed_by": "test_user", "note": "LGTM"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _compile(client: TestClient, domain_profile_id: str) -> tuple[int, dict]:
    resp = client.post(
        "/api/blueprints/compile",
        json={"media": _MEDIA, "domain_profile_id": domain_profile_id},
    )
    return resp.status_code, resp.json()


# ---------------------------------------------------------------------------
# Error shape
# ---------------------------------------------------------------------------


class TestErrorShape:
    def test_404_uses_error_envelope(self, client: TestClient) -> None:
        resp = client.get("/api/domains/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404
        body = resp.json()
        assert "error" in body
        assert "code" in body["error"]
        assert "message" in body["error"]

    def test_409_uses_error_envelope(self, client: TestClient) -> None:
        candidate = _derive(client)
        pid = candidate["domain_profile_id"]
        _confirm(client, pid)
        resp = client.patch(
            f"/api/domains/{pid}", json={"patch": {"name": "X"}}
        )
        assert resp.status_code == 409
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == "state_conflict"

    def test_400_uses_error_envelope(self, client: TestClient) -> None:
        resp = client.post("/api/blueprints/compile", json={"media": _MEDIA})
        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body
        assert "code" in body["error"]
        assert "message" in body["error"]


# ---------------------------------------------------------------------------
# Success response schema_version
# ---------------------------------------------------------------------------


class TestResponseSchemaVersion:
    def test_derive_response_has_schema_version(self, client: TestClient) -> None:
        resp = client.post(
            "/api/domains/derive",
            json={"media": _MEDIA, "options": _OPTIONS_MECH},
        )
        assert resp.status_code == 200
        assert resp.json()["schema_version"] == SCHEMA_VERSION

    def test_get_response_has_schema_version(self, client: TestClient) -> None:
        candidate = _derive(client)
        resp = client.get(f"/api/domains/{candidate['domain_profile_id']}")
        assert resp.status_code == 200
        assert resp.json()["schema_version"] == SCHEMA_VERSION

    def test_compile_response_has_schema_version(self, client: TestClient) -> None:
        candidate = _derive(client)
        _confirm(client, candidate["domain_profile_id"])
        _, data = _compile(client, candidate["domain_profile_id"])
        assert data["schema_version"] == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Derive endpoint
# ---------------------------------------------------------------------------


class TestDerive:
    def test_derive_returns_candidates(self, client: TestClient) -> None:
        candidate = _derive(client)
        assert candidate["domain_profile_id"]
        assert candidate["status"] == "draft"
        assert candidate["name"]
        assert candidate["schema_version"]
        assert candidate["summary"]

    def test_derive_no_hint_includes_warning(self, client: TestClient) -> None:
        resp = client.post("/api/domains/derive", json={"media": _MEDIA, "options": {}})
        assert resp.status_code == 200
        data = resp.json()
        assert any("hint" in w.lower() for w in data["warnings"])

    def test_derive_persists_profile(self, client: TestClient) -> None:
        candidate = _derive(client)
        pid = candidate["domain_profile_id"]
        resp = client.get(f"/api/domains/{pid}")
        assert resp.status_code == 200
        assert resp.json()["domain_profile"]["id"] == pid

    def test_derive_respects_max_candidates(self, client: TestClient) -> None:
        resp = client.post(
            "/api/domains/derive",
            json={"media": _MEDIA, "options": {"max_candidates": 2}},
        )
        assert resp.status_code == 200
        assert len(resp.json()["candidates"]) <= 2

    def test_derive_missing_media_id_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/api/domains/derive",
            json={"media": {"media_type": "video"}, "options": {}},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "invalid_request"

    def test_derive_missing_media_type_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/api/domains/derive",
            json={"media": {"media_id": "vid_001"}, "options": {}},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "invalid_request"

    def test_derive_missing_multiple_required_fields_lists_all_in_message(
        self, client: TestClient
    ) -> None:
        resp = client.post("/api/domains/derive", json={"media": {}, "options": {}})
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "invalid_request"
        assert "media.media_id" in body["error"]["message"]
        assert "media.media_type" in body["error"]["message"]
        assert body["error"]["details"]["missing_fields"] == [
            "media_id",
            "media_type",
        ]


# ---------------------------------------------------------------------------
# Get endpoint
# ---------------------------------------------------------------------------


class TestGetDomain:
    def test_get_existing_profile(self, client: TestClient) -> None:
        candidate = _derive(client)
        pid = candidate["domain_profile_id"]
        resp = client.get(f"/api/domains/{pid}")
        assert resp.status_code == 200
        profile = resp.json()["domain_profile"]
        assert profile["id"] == pid
        assert profile["status"] == "draft"
        assert "capture_protocol" in profile
        assert "validators" in profile
        assert "exporters" in profile

    def test_get_unknown_profile_returns_404(self, client: TestClient) -> None:
        resp = client.get("/api/domains/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Patch endpoint
# ---------------------------------------------------------------------------


class TestPatchDomain:
    def test_patch_draft_name(self, client: TestClient) -> None:
        candidate = _derive(client)
        pid = candidate["domain_profile_id"]
        resp = client.patch(
            f"/api/domains/{pid}", json={"patch": {"name": "Updated Name"}}
        )
        assert resp.status_code == 200
        assert resp.json()["domain_profile"]["name"] == "Updated Name"

    def test_patch_draft_notes(self, client: TestClient) -> None:
        candidate = _derive(client)
        pid = candidate["domain_profile_id"]
        resp = client.patch(
            f"/api/domains/{pid}", json={"patch": {"notes": "My custom note"}}
        )
        assert resp.status_code == 200
        assert resp.json()["domain_profile"]["notes"] == "My custom note"

    def test_patch_draft_capture_protocol(self, client: TestClient) -> None:
        candidate = _derive(client)
        pid = candidate["domain_profile_id"]
        new_step = {
            "step_id": "custom-step-1",
            "title": "Custom Step",
            "instructions": "Do something custom",
            "required": True,
        }
        resp = client.patch(
            f"/api/domains/{pid}",
            json={"patch": {"capture_protocol": [new_step]}},
        )
        assert resp.status_code == 200
        protocol = resp.json()["domain_profile"]["capture_protocol"]
        assert len(protocol) == 1
        assert protocol[0]["title"] == "Custom Step"

    def test_patch_rejected_after_confirm(self, client: TestClient) -> None:
        """PATCH must return 409 after confirm (state_conflict)."""
        candidate = _derive(client)
        pid = candidate["domain_profile_id"]
        _confirm(client, pid)
        resp = client.patch(
            f"/api/domains/{pid}", json={"patch": {"name": "Should Fail"}}
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "state_conflict"

    def test_patch_unknown_profile_returns_404(self, client: TestClient) -> None:
        resp = client.patch(
            "/api/domains/00000000-0000-0000-0000-000000000000",
            json={"patch": {"name": "X"}},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Confirm endpoint
# ---------------------------------------------------------------------------


class TestConfirmDomain:
    def test_confirm_sets_status_to_confirmed(self, client: TestClient) -> None:
        candidate = _derive(client)
        pid = candidate["domain_profile_id"]
        result = _confirm(client, pid)
        assert result["domain_profile"]["status"] == "confirmed"
        assert result["domain_profile"]["id"] == pid

    def test_confirm_already_confirmed_returns_409(self, client: TestClient) -> None:
        """Confirm is non-idempotent: re-confirming returns 409 state_conflict."""
        candidate = _derive(client)
        pid = candidate["domain_profile_id"]
        _confirm(client, pid)
        resp = client.post(f"/api/domains/{pid}/confirm", json={"confirmed_by": "u2"})
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "state_conflict"

    def test_confirm_unknown_profile_returns_404(self, client: TestClient) -> None:
        resp = client.post(
            "/api/domains/00000000-0000-0000-0000-000000000000/confirm", json={}
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Compile endpoint
# ---------------------------------------------------------------------------


class TestCompile:
    def test_compile_missing_domain_profile_id_returns_400(
        self, client: TestClient
    ) -> None:
        resp = client.post("/api/blueprints/compile", json={"media": _MEDIA})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_request"

    def test_compile_unknown_domain_profile_id_returns_404(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/api/blueprints/compile",
            json={
                "media": _MEDIA,
                "domain_profile_id": "00000000-0000-0000-0000-000000000000",
            },
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_compile_unconfirmed_profile_returns_400(
        self, client: TestClient
    ) -> None:
        candidate = _derive(client)
        pid = candidate["domain_profile_id"]
        status_code, data = _compile(client, pid)
        assert status_code == 400
        assert data["error"]["code"] == "domain_not_confirmed"
        assert (
            data["error"]["message"]
            == "Compilation requires a confirmed domain profile. "
            "Derive a domain profile, confirm it, then compile."
        )

    def test_compile_happy_path(self, client: TestClient) -> None:
        """derive -> confirm -> compile produces a valid BlueprintIR."""
        candidate = _derive(client)
        pid = candidate["domain_profile_id"]
        _confirm(client, pid)

        status_code, data = _compile(client, pid)
        assert status_code == 200, data
        bp = data["blueprint"]

        assert bp["id"]
        assert bp["domain_profile_id"] == pid
        assert bp["schema_version"] == SCHEMA_VERSION
        assert bp["schema_version_used"] == SCHEMA_VERSION
        assert bp["source"]["media_id"] == _MEDIA["media_id"]
        assert 0.0 <= bp["completeness"]["score"] <= 1.0
        assert len(bp["entities"]) >= 1
        assert len(bp["provenance"]) >= 1

    def test_compiled_blueprint_entities_have_required_fields(
        self, client: TestClient
    ) -> None:
        candidate = _derive(client)
        _confirm(client, candidate["domain_profile_id"])
        _, data = _compile(client, candidate["domain_profile_id"])
        for entity in data["blueprint"]["entities"]:
            assert "schema_version" in entity
            assert "id" in entity
            assert "type" in entity
            assert "attributes" in entity
            assert "confidence" in entity

    def test_compiled_blueprint_relations_reference_valid_entity_ids(
        self, client: TestClient
    ) -> None:
        candidate = _derive(client)
        _confirm(client, candidate["domain_profile_id"])
        _, data = _compile(client, candidate["domain_profile_id"])
        bp = data["blueprint"]
        entity_ids = {e["id"] for e in bp["entities"]}
        for rel in bp["relations"]:
            assert rel["source_entity_id"] in entity_ids
            assert rel["target_entity_id"] in entity_ids
