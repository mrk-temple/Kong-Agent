"""Capacity-defined Memory reservations; no window expansion or model calls."""
import json

import pytest

from kong.continuity.compiler import BudgetManager, ContextBudgetExceeded, ContextRenderer, item
from kong.continuity.contracts import ContextBudget, ContextSource
from kong.evaluation.memory_behavior import owner
from kong.memory import Bottle, MemoryDraft, MemoryService, RetrievalQuery, Source


def entry(category, text, priority=0):
    return item(category, text, priority, [ContextSource(kind="system")])


@pytest.mark.parametrize("size", range(100, 700, 30))
def test_twenty_capacity_conditions_preserve_key_episode_identity_rules_and_current_input(size):
    required = [entry("system", "host safety"), entry("goal", "current request"),
                entry("core_identity", "stable identity"), entry("core_relationship", "on_request")]
    episode = entry("core_memory", "EPISODE " + "x" * size, 3)
    episode.memory_kind, episode.pinned = "episodic", True
    candidates = required + [entry("working_set", "task evidence", 1)] + [
        entry("recent_thread", "irrelevant history " * 50, 2) for _ in range(10)] + [episode]
    budget = ContextBudget(window_tokens=2500, output_reserved_tokens=500)
    selected, messages, used, categories, dropped = BudgetManager(ContextRenderer()).allocate(candidates, budget)
    assert all(required_item in selected for required_item in required) and episode in selected
    assert used <= budget.input_tokens and sum(categories.values()) == used
    assert any(d["category"] == "recent_thread" for d in dropped)
    assert "current request" in str(messages)


def test_pinned_episode_beats_other_memory_but_cannot_overflow_or_replace_host():
    pinned = entry("core_memory", "cherished " * 55, 3)
    pinned.memory_kind, pinned.pinned = "episodic", True
    other = entry("core_memory", "ordinary " * 70, 2)
    other.memory_kind = "semantic"
    budget = ContextBudget(window_tokens=1050, output_reserved_tokens=200)
    selected, _, used, _, dropped = BudgetManager(ContextRenderer()).allocate([
        entry("system", "safety" * 25), other, pinned], budget)
    assert pinned in selected and other not in selected and used <= budget.input_tokens
    assert dropped[0]["memory_kind"] == "semantic" and dropped[0]["sources"]
    selected, _, _, _, dropped = BudgetManager(ContextRenderer()).allocate([
        entry("system", "x" * 700), pinned], budget)
    assert pinned not in selected and dropped[0]["pinned"]
    with pytest.raises(ContextBudgetExceeded):
        BudgetManager(ContextRenderer()).allocate([entry("system", "x" * 1000), pinned], budget)


def test_pin_metadata_requires_relevant_active_membership_and_does_not_widen_read_scope(tmp_path):
    with MemoryService(tmp_path / "pins.db") as core:
        core.create_profile(owner(), command_id="profile")
        for name in ("active", "inactive"):
            core.create_bottle(owner(), Bottle(bottle_id=name, profile_id=owner().profile_id, title=name), command_id=name)
        ids = []
        for key, project, bottle in (("relevant", "Orion", "active"), ("unrelated", "Lyra", "active"), ("hidden", "Orion", "inactive")):
            content = f"The {project} outage ended with rollback badge {key}."
            saved = core.remember(owner(key), MemoryDraft(kind="episodic", content=content),
                Source(source_id=key, origin="user_command", speaker="user", excerpt=content), command_id=key)
            ids.append(saved.record_id)
            core.add_memory_to_bottle(owner(), bottle, saved.record_id, command_id="member-" + key)
            core.pin_memory(owner(), saved.record_id, bottle, command_id="pin-" + key)
        bottle = next(b for b in core.list_bottles(owner()) if b.bottle_id == "active")
        core.activate_bottle(owner(), "active", expected_revision=bottle.revision, command_id="on")
        context = core.retrieve_context(owner(), RetrievalQuery(text="Recall the Orion outage."))
        assert context.pinned_record_ids == (ids[0],) and [r.record_id for r in context.records] == [ids[0]]
        assert not core.retrieve_context(owner().model_copy(update={"scopes": ()}), RetrievalQuery(text="Recall Orion.")).pinned_record_ids
        core.remove_memory_from_bottle(owner(), "active", ids[0], command_id="remove")
        assert not core.retrieve_context(owner(), RetrievalQuery(text="Recall Orion.")).pinned_record_ids


def test_reservation_releases_unused_capacity_and_drops_record_with_provenance():
    record = entry("core_memory", "x" * 900, 3)
    record.memory_kind = "episodic"
    selected, _, _, _, dropped = BudgetManager(ContextRenderer()).allocate([
        entry("system", "host"), entry("recent_thread", "small", 2), record],
        ContextBudget(window_tokens=600, output_reserved_tokens=100))
    assert any(i.category == "recent_thread" for i in selected)
    assert dropped[0]["reason"] == "input_token_budget"
    json.dumps(dropped)  # Inspectable, serializable exclusion receipt.
