import asyncio
import json

import pytest

from kong.contracts import Act, Action, Criterion, FinalResponse, History, RunState, Status
from kong.models.fake import ScriptedModel
from kong.runtime.gates import CompletionGate
from kong.runtime.loop import Runtime
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def test_exact_text_rejects_wrapper_and_whitespace_and_rechecks_current_file(tmp_path):
    criterion = Criterion(id="plain", description="Plain result", kind="file_equals", path="result.txt", expected_text="42")
    gate = CompletionGate(Workspace(tmp_path))
    state = RunState(goal="output", success_criteria=[criterion])
    for value, accepted in [('42', True), ('{"result":42}', False), ('42\n', False), ('42', True)]:
        (tmp_path / "result.txt").write_bytes(value.encode())
        assert bool(gate.check(state, FinalResponse(content="done"), History())) is not accepted
    criterion.expected_text = ""
    (tmp_path / "result.txt").write_bytes(b"")
    assert not gate.check(state, FinalResponse(content="done"), History())


@pytest.mark.parametrize("text", ['{"ok":0}', '{"ok":"false"}', '{"ok":false,"extra":1}',
                                 '{"ok":true,"ok":false}', '[]', 'not json'])
def test_json_criteria_reject_wrong_types_extra_keys_duplicates_and_invalid_json(tmp_path, text):
    state = RunState(goal="json", success_criteria=[Criterion(id="typed", description="Typed output",
        kind="file_json", path="answer.json", json_match={"ok":False})])
    (tmp_path / "answer.json").write_text(text)
    assert CompletionGate(Workspace(tmp_path)).check(state, FinalResponse(content="done"), History())
    (tmp_path / "answer.json").write_bytes(b'\xef\xbb\xbf{ "ok" : false }')
    assert not CompletionGate(Workspace(tmp_path)).check(state, FinalResponse(content="done"), History())


def test_runtime_rejects_bad_output_then_allows_repair_without_budget_reset(tmp_path):
    async def scenario():
        def write(value):
            return Act(actions=[Action(name="write_file", arguments={"path":"research.json", "content":json.dumps(value), "overwrite":True})])
        def final(action):
            return FinalResponse(content="done", evidence_ids=[action])
        expected = {"first_version":"3.11", "supports_writing":False}
        model = ScriptedModel([write({**expected,"first_version":"Python 3.11"}), final("a1"), write(expected), final("a2")])
        workspace = Workspace(tmp_path)
        registry = default_registry(workspace)
        runtime = Runtime(model, workspace, registry, RunState(goal="Produce exact typed JSON", success_criteria=[Criterion(id="typed",
            description="Exact contract", kind="file_json", path="research.json", json_match=expected)]))
        try:
            await runtime.run()
            assert runtime.state.status == Status.COMPLETED
            assert runtime.metrics.model_calls == 4
            assert any("completion_rejected" in e.payload for e in runtime.history.events)
            assert json.loads((tmp_path / "research.json").read_text()) == expected
        finally:
            await registry.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["file_equals", "file_json"])
def test_exact_criteria_require_path_and_explicit_expected_values(kind):
    with pytest.raises(ValueError):
        Criterion(id="missing", description="Missing contract", kind=kind, path="answer.txt")
