"""Offline guards for the paid-evaluation entrypoint; no network calls."""
from types import SimpleNamespace

import pytest
from kong.models.base import ModelError
from examples.phase1_real_model_eval import RequestLedger


def packet():
    return SimpleNamespace(run_id="a"*32, turn=1, recalled_memories=[], memory_access=None,
        memory_query=None, memory_query_status=None, selection_notes=[], dropped=[])


def payload():
    return {"messages": [{"role":"user", "content":"synthetic"}], "max_tokens":128, "model":"fixture"}


def test_global_request_cap_is_reserved_before_dispatch_and_cannot_be_reset(tmp_path):
    ledger = RequestLedger(tmp_path, maximum=2)
    ledger.reserve("one", packet(), payload())
    ledger.reserve("two", packet(), payload())
    with pytest.raises(ModelError, match="limit"):
        ledger.reserve("three", packet(), payload())
    assert len(ledger.requests) == 2
    with pytest.raises(ValueError, match="Existing live ledger"):
        RequestLedger(tmp_path)


def test_per_case_token_and_time_limits_are_independent(tmp_path):
    ledger = RequestLedger(tmp_path/"case")
    for _ in range(3):
        ledger.reserve("one", packet(), payload())
    with pytest.raises(ModelError):
        ledger.reserve("one", packet(), payload())
    ledger.reserve("two", packet(), payload())
    small = RequestLedger(tmp_path/"tokens", max_input=1)
    with pytest.raises(ModelError):
        small.reserve("one", packet(), payload())
    timed = RequestLedger(tmp_path/"time", max_seconds=1)
    timed.started -= 2
    with pytest.raises(ModelError):
        timed.reserve("one", packet(), payload())


def test_evidence_never_contains_secret_and_batch_limit_cannot_exceed_twenty(tmp_path):
    secret = "sk-" + "synthetic-SECRET-123456789"
    ledger = RequestLedger(tmp_path)
    ledger.secret = secret
    data = payload()
    data["messages"][0]["content"] = secret
    ledger.reserve("one", packet(), data)
    assert secret not in ledger.path.read_text(encoding="utf-8")
    assert "[REDACTED]" in ledger.path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="at most 20"):
        RequestLedger(tmp_path/"over", maximum=21)


def test_paid_batch_stops_after_post_http_protocol_parse_failure(tmp_path, monkeypatch):
    import asyncio
    from kong.config import ModelConfig
    from kong.models.base import ModelProtocolError
    from kong.models.compatible import CompatibleModel
    from examples.phase1_real_model_eval import EvidenceModel

    ledger = RequestLedger(tmp_path)
    config = ModelConfig(base_url="https://synthetic.invalid/v1", model="fixture")
    model = EvidenceModel(config, None, ledger)
    model.case = "one"

    async def incompatible(self, context):
        entry = ledger.reserve(self.case, context, payload())
        entry["receipt_state"] = "provider_response_received"
        raise ModelProtocolError("Invalid fixture decision")

    monkeypatch.setattr(CompatibleModel, "generate", incompatible)
    with pytest.raises(ModelError) as failure:
        asyncio.run(model.generate(packet()))
    assert failure.value.fatal_configuration and not failure.value.retryable
    assert model.case_fatal == "protocol_incompatible"
    assert ledger.stopped_reason == "provider_protocol_incompatible"
    assert len(ledger.requests) == 1
    assert ledger.requests[0]["receipt_state"] == "provider_response_received"
    assert ledger.requests[0]["decision_failure_category"] == "protocol_incompatible"


def test_receipt_requires_record_version_and_body_in_actual_messages():
    import json
    from examples.phase1_real_model_eval import memory_payload_proofs
    record = {"id": "fixture", "version": 2, "content": "NEW_BODY"}
    assert not memory_payload_proofs([{"content": "retrieved fixture"}], [record])
    messages = [{"content": "AUTHORIZED MEMORY:\n" + json.dumps(record, ensure_ascii=False)}]
    assert memory_payload_proofs(messages, [record])[0]["version"] == 2
    assert not memory_payload_proofs(messages, [dict(record, version=1)])
    assert not memory_payload_proofs(messages, [dict(record, content="OLD_BODY")])


def test_task_failure_without_output_file_is_recordable(tmp_path):
    from examples.phase1_real_model_eval import file_matches
    assert not file_matches(tmp_path, "missing.txt", b"expected")


def test_live_fixture_obeys_inactive_then_explicit_activation(tmp_path):
    from examples.phase1_real_model_eval import initialize_memory, owner
    from kong.memory import MemoryService
    db = tmp_path / "synthetic.db"
    record_id = initialize_memory(db)
    with MemoryService(db) as core:
        bottles = core.list_bottles(owner())
        assert len(bottles) == 1 and bottles[0].active
        assert core.get_record(owner(), record_id).current.content.endswith("MEMORY_BADGE_47。")
