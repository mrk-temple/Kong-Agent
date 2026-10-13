import time
import pytest
from fastapi.testclient import TestClient
from kong.local_api.app import create_app
from kong.models.fake import ScriptedModel
from kong.contracts import FinalResponse


def client(tmp_path):
    return TestClient(
        create_app(
            tmp_path,
            origin="http://127.0.0.1:8765",
            token="test-session",
            model_factory=lambda: ScriptedModel(
                [FinalResponse(content="真实 Runtime 的接受回答")]
            ),
        ),
        base_url="http://127.0.0.1:8765",
        headers={
            "Authorization": "Bearer test-session",
            "Origin": "http://127.0.0.1:8765",
        },
    )


@pytest.mark.parametrize(
    "change",
    [
        {"Origin": "https://evil.example"},
        {"Host": "evil.example"},
        {"Authorization": "Bearer bad"},
    ],
)
def test_external_authority_rejected(tmp_path, change):
    with client(tmp_path) as api:
        response = api.post("/v1/threads", json={"command_id": "t"}, headers=change)
        assert response.status_code in (401, 403)


def test_forged_authority_and_missing_acceptance(tmp_path):
    with client(tmp_path) as api:
        assert (
            api.post(
                "/v1/threads",
                json={"command_id": "forged", "Authority": {"capabilities": ["all"]}},
            ).status_code
            == 422
        )
        thread = api.post("/v1/threads", json={"command_id": "t"}).json()
        response = api.post(
            f"/v1/threads/{thread['thread_id']}/runs",
            json={"command_id": "r", "text": "交付任务", "intent": "task"},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "acceptance_required"
        assert not api.app.state.service.factory.store.list_runs()


def test_cookie_pairing_requires_csrf_and_is_revocable(tmp_path):
    with client(tmp_path) as api:
        assert api.post("/v1/session", json={}).status_code == 200
        api.headers.pop("authorization", None)
        assert api.post("/v1/threads", json={"command_id": "t"}).status_code == 403
        csrf = api.get("/v1/session").json()["csrf_token"]
        assert (
            api.post(
                "/v1/threads", json={"command_id": "t"}, headers={"X-Kong-CSRF": csrf}
            ).status_code
            == 201
        )


def test_runtime_and_event_metadata_actual_api(tmp_path):
    with client(tmp_path) as api:
        thread = api.post("/v1/threads", json={"command_id": "t"}).json()
        body = {"command_id": "r", "text": "你好", "intent": "chat", "mode": "fast"}
        response = api.post(f"/v1/threads/{thread['thread_id']}/runs", json=body)
        assert response.status_code == 202
        receipt = response.json()
        assert (
            api.post(f"/v1/threads/{thread['thread_id']}/runs", json=body).json()
            == receipt
        )
        for _ in range(100):
            detail = api.get(f"/v1/runs/{receipt['run_id']}").json()
            if detail["status"] != "running" and not detail["driver_active"]:
                break
            time.sleep(0.02)
        assert detail["outcome_kind"] == "answer_finished"
        events = api.get("/v1/events/history").json()["items"]
        assert any(e["kind"] == "runtime.completed" for e in events)
        assert any(e["kind"] == "memory.context_assembled" for e in events)
        assert not any(e["kind"] == "memory.request_delivered" for e in events)
        assert api.get("/v1/events/history?after=other:1").status_code == 403


def test_key_never_returned_and_reserved_files(tmp_path):
    secret = "sensitive-fixture-key"
    with client(tmp_path) as api:
        response = api.post(
            "/v1/model",
            json={
                "command_id": "model",
                "base_url": "https://api.example/v1",
                "model": "test",
                "api_key": secret,
            },
        )
        assert response.status_code == 200
        assert secret not in response.text
        assert secret not in (tmp_path / "model-config.json").read_text(
            encoding="utf-8"
        )
        invalid = api.post(
            "/v1/model",
            json={
                "command_id": "bad",
                "base_url": "bad",
                "model": "test",
                "api_key": secret,
            },
        )
        assert secret not in invalid.text
        assert api.get("/v1/workspace/preview?path=../core.db").status_code == 422
