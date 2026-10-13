import asyncio
import json

import pytest

from kong.contracts import Act, Action, FinalResponse
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.skills.lifecycle import SkillLifecycle
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def bundle(path, version="1", body="PINNED_PROCEDURE\nRead references/check.md."):
    root = path / ".agents" / "skills" / "tested"
    root.mkdir(parents=True, exist_ok=True)
    (root / "references").mkdir(exist_ok=True)
    (root / "references" / "check.md").write_text("original reference", encoding="utf-8")
    (root / "SKILL.md").write_text("---\nname: tested\ndescription: Test procedure\nmetadata:\n"
        f"  version: '{version}'\n  lifecycle: candidate\n---\n{body}", encoding="utf-8")
    workspace = Workspace(path)
    registry = default_registry(workspace)
    catalog = registry.get("skill_load").catalog
    skill = catalog.get("tested")
    identity = catalog.identity(skill)
    return root, workspace, registry, catalog, identity


def register(catalog, identity):
    return catalog.lifecycle.register(skill_id="project:tested", **identity, applies_to=["small Python repair"],
        excludes=["arbitrary execution"], required_tasks=["variation", "negative"], contract_version="c1",
        source_trials=["development:1"])


def validate(catalog, identity, task="variation", passed=True):
    return catalog.lifecycle.record("project:tested", identity["fingerprint"],
        {"task_id": task, "contract_version": "c1", "passed": passed,
         "evidence_sha256": "a" * 64, "source_trial": "development:" + task})


def enable(catalog, identity):
    register(catalog, identity)
    validate(catalog, identity)
    validate(catalog, identity, "negative")
    return catalog.lifecycle.select("project:tested", identity["fingerprint"], "activate", reason="Host development experiment")


def test_candidate_requires_matching_all_validations_and_explicit_activation(tmp_path):
    _, _, _, catalog, identity = bundle(tmp_path)
    with pytest.raises(ValueError, match="not active"):
        catalog.load("tested")
    register(catalog, identity)
    assert validate(catalog, identity).status == "candidate"
    with pytest.raises(ValueError, match="every declared task"):
        catalog.lifecycle.select("project:tested", identity["fingerprint"], "activate", reason="too early")
    assert validate(catalog, identity, "negative").status == "validated"
    with pytest.raises(ValueError, match="not active"):
        catalog.read("tested", "references/check.md")
    catalog.lifecycle.select("project:tested", identity["fingerprint"], "activate", reason="explicit experiment")
    loaded = catalog.load("tested")
    assert loaded["fingerprint"] == identity["fingerprint"] and loaded["lifecycle"] == "active"
    assert loaded["authority"].startswith("Task guidance only")


def test_changed_reference_and_body_cannot_reuse_validation_or_mix_same_run(tmp_path):
    root, _, _, catalog, original = bundle(tmp_path)
    enable(catalog, original)
    catalog.load("tested")
    (root / "references" / "check.md").write_text("different reference", encoding="utf-8")
    with pytest.raises(ValueError, match="version changed"):
        catalog.read("tested", "references/check.md")
    fresh = default_registry(Workspace(tmp_path)).get("skill_load").catalog
    current = fresh.identity(fresh.get("tested"))
    assert current["fingerprint"] != original["fingerprint"]
    with pytest.raises(ValueError, match="not active"):
        fresh.load("tested")
    (root / "SKILL.md").write_text((root / "SKILL.md").read_text(encoding="utf-8") + "\nchanged body", encoding="utf-8")
    fresh.refresh()
    assert fresh.identity(fresh.get("tested"))["fingerprint"] != current["fingerprint"]


def test_failure_disables_active_revision_and_withdraws_material_after_restore(tmp_path):
    _, workspace, registry, catalog, identity = bundle(tmp_path)
    enable(catalog, identity)
    model = ScriptedModel([Act(actions=[Action(name="skill_load", arguments={"name": "tested"})]), FinalResponse(content="done")])
    runtime = Runtime.new("explain method", model, workspace, registry, store=RunStore(tmp_path / ".kong" / "runs"))
    asyncio.run(runtime.run())
    assert "PINNED_PROCEDURE" in json.dumps(model.contexts[-1].messages)
    assert validate(catalog, identity, "variation", False).status == "disabled"
    restored = Runtime.restore(runtime.snapshot(), ScriptedModel([]), workspace, default_registry(workspace), store=runtime.store)
    new_catalog = restored.registry.get("skill_list").catalog
    restored.context_engine.supplemental_items = new_catalog.context_items(restored.history, restored.state.run_id)
    packet = asyncio.run(restored.context_engine.prepare(thread=restored.thread, state=restored.state,
        history=restored.history, tools=restored.registry.definitions()))
    assert "PINNED_PROCEDURE" not in json.dumps(packet.messages)
    assert any(i.category == "runtime_feedback" and "withdrawn" in i.content for i in packet.items)
    assert not runtime.history.observations and runtime.state.progress_state.progress_count == 0
    with pytest.raises(ValueError, match="not active"):
        new_catalog.load("tested")


def test_rollback_selects_validated_old_content_without_erasing_history(tmp_path):
    _, _, _, catalog, first = bundle(tmp_path)
    enable(catalog, first)
    _, _, _, second_catalog, second = bundle(tmp_path, version="2", body="second version")
    enable(second_catalog, second)
    assert catalog.lifecycle.get("project:tested", first["fingerprint"]).status == "disabled"
    second_catalog.lifecycle.select("project:tested", first["fingerprint"], "activate", reason="rollback")
    assert second_catalog.lifecycle.get("project:tested", second["fingerprint"]).status == "disabled"
    with pytest.raises(ValueError, match="not active"):
        second_catalog.load("tested")
    # Selecting an old identity does not restore old files; the host restores them separately.
    _, _, _, restored_catalog, restored = bundle(tmp_path)
    assert restored == first
    assert restored_catalog.load("tested")["version"] == "1"
    data = SkillLifecycle(catalog.lifecycle.path).read()
    assert len(data.revisions) == 2 and all(r.validations and r.decisions for r in data.revisions)


def test_validation_cannot_change_registered_contract_or_claim_success_with_string(tmp_path):
    _, _, _, catalog, identity = bundle(tmp_path)
    register(catalog, identity)
    before = catalog.lifecycle.path.read_bytes()
    for changes in ({"contract_version": "c2"}, {"passed": "true"}, {"task_id": "unseen"}):
        data = {"task_id": "variation", "contract_version": "c1", "passed": True,
                "evidence_sha256": "b" * 64, "source_trial": "test", **changes}
        with pytest.raises(ValueError):
            catalog.lifecycle.record("project:tested", identity["fingerprint"], data)
        assert catalog.lifecycle.path.read_bytes() == before
    with pytest.raises(ValueError, match="immutable"):
        catalog.lifecycle.register(skill_id="project:tested", **identity, applies_to=["different scope"],
            required_tasks=["variation"], contract_version="c1", source_trials=["new"])


def test_resource_race_cannot_return_bytes_under_an_old_fingerprint(tmp_path, monkeypatch):
    root, _, _, catalog, identity = bundle(tmp_path)
    enable(catalog, identity)
    catalog.load("tested")
    monkeypatch.setattr(catalog, "identity", lambda skill: identity)
    (root / "references" / "check.md").write_text("changed after fingerprint", encoding="utf-8")
    with pytest.raises(ValueError, match="changed during read"):
        catalog.read("tested", "references/check.md")
