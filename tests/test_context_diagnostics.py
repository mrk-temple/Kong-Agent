import asyncio

import pytest

from kong.continuity.memory_contracts import ContinuityPolicy
from kong.evaluation.context_diagnostics import SourceLabels, source_selection_report
from kong.contracts import FinalResponse
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def test_external_labels_distinguish_missing_selection_and_budget_from_use(tmp_path):
    workspace = Workspace(tmp_path)
    runtime = Runtime.new("project invoice", ScriptedModel([FinalResponse(content="ok")]), workspace,
        default_registry(workspace), store=RunStore(tmp_path / ".kong" / "runs"))
    asyncio.run(runtime.run())
    engine = runtime.context_engine
    for key, text in (("invoice.currency", "invoice currency CNY"), ("weather.sun", "weather sunny")):
        asyncio.run(runtime.memory_command("add", kind="fact", key=key, text=text))
    entries = engine.store.state(runtime.thread.thread_id).items
    labels = SourceLabels(checkpoint="development:labelled", required_groups=[[entries[0].id], ["missing"]],
        irrelevant_ids=[entries[1].id], available_ids=[e.id for e in entries])
    packet = asyncio.run(engine.prepare(thread=runtime.thread, state=runtime.state, history=runtime.history,
        tools=runtime.registry.definitions()))
    candidates = engine.diagnostics[-1]["candidates"]
    report = source_selection_report(packet, candidates, labels)
    assert report["candidate_recall"] == report["injected_recall"] == 0.5
    assert report["irrelevant_injected_ids"] == [entries[1].id]
    assert report["failure_classification"]["missing"] == "acquisition_missing"
    assert report["model_use"] == "not_measured"
    assert "development:labelled" not in str(packet.messages)
    engine.policy = ContinuityPolicy(memory_mode="M2")
    packet = asyncio.run(engine.prepare(thread=runtime.thread, state=runtime.state, history=runtime.history,
        tools=runtime.registry.definitions()))
    filtered = source_selection_report(packet, engine.diagnostics[-1]["candidates"], labels)
    assert not filtered["irrelevant_injected_ids"] and filtered["injected_recall"] == 0.5
    assert source_selection_report(packet, [], None)["candidate_recall"] is None


def test_source_label_conflicts_and_empty_alternative_group_are_rejected():
    for values in ({"required_groups": [[]]}, {"required_groups": [["a"]], "invalid_ids": ["a"]},
                   {"irrelevant_ids": ["a"], "invalid_ids": ["a"]}):
        with pytest.raises(ValueError):
            SourceLabels(checkpoint="c", **values)
