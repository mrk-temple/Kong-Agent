"""I-03/04: intervening reads must not hide an unchanged failed selector."""
import asyncio

from kong.contracts import Act, Action, FinalResponse, Status
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def test_read_between_failed_patches_preserves_actionable_warning(tmp_path):
    (tmp_path / "value.py").write_text("value = 7\n", encoding="utf-8")
    patch = Act(actions=[Action(name="patch_file", arguments={"path": "value.py", "old_text": "value = 3", "new_text": "value = 9"})])
    read = Act(actions=[Action(name="read_file", arguments={"path": "value.py"})])
    model = ScriptedModel([patch, read, patch, read, FinalResponse(content="read current file", evidence_ids=["a4"])])
    workspace = Workspace(tmp_path)
    runtime = Runtime.new("inspect value", model, workspace, default_registry(workspace), completion_scope="mechanical")
    assert asyncio.run(runtime.run()).status == Status.COMPLETED
    warning = next(item.content for item in model.contexts[-1].items if "PATCH FAILURE STATUS:" in item.content)
    assert '"same_selector_failures":2' in warning and "a4" in warning
    assert runtime.state.hard_turn_limit == 40 and len(runtime.history.observations) == 4
    assert (tmp_path / "value.py").read_text() == "value = 7\n"


def test_patch_warning_expires_when_file_version_changes(tmp_path):
    from kong.runtime.recovery_feedback import patch_failure_guidance
    (tmp_path / "value.py").write_text("value = 7\n", encoding="utf-8")
    workspace = Workspace(tmp_path)
    runtime = Runtime.new("inspect", ScriptedModel([]), workspace, default_registry(workspace))
    asyncio.run(runtime._actions(Act(actions=[Action(name="patch_file", arguments={"path": "value.py", "old_text": "bad", "new_text": "new"})])))
    assert patch_failure_guidance(runtime.state, runtime.history, workspace)
    (tmp_path / "value.py").write_text("value = 8\n", encoding="utf-8")
    assert patch_failure_guidance(runtime.state, runtime.history, workspace) is None


def test_successful_patch_removes_failed_selector_warning(tmp_path):
    from kong.runtime.recovery_feedback import patch_failure_guidance
    (tmp_path / "value.py").write_text("value = 7\n", encoding="utf-8")
    workspace = Workspace(tmp_path)
    runtime = Runtime.new("inspect", ScriptedModel([]), workspace, default_registry(workspace))
    async def scenario():
        await runtime._actions(Act(actions=[Action(name="patch_file", arguments={"path": "value.py", "old_text": "bad", "new_text": "new"})]))
        await runtime._actions(Act(actions=[Action(name="patch_file", arguments={"path": "value.py", "old_text": "value = 7", "new_text": "value = 8"})]))
    asyncio.run(scenario())
    assert patch_failure_guidance(runtime.state, runtime.history, workspace) is None
