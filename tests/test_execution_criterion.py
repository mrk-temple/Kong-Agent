from kong.contracts import Action, Criterion, FinalResponse, History, Observation, RunState
from kong.runtime.gates import CompletionGate
from kong.workspace import Workspace


def obs(number, tool, success=True):
    return Observation(action_id=f"a{number}",turn=number,action=Action(name=tool),
                       success=success,fingerprint=str(number),output_hash=str(number))


def test_verification_before_edit_does_not_satisfy_after_edit_requirement(tmp_path):
    state = RunState(goal="fix and test",success_criteria=[Criterion(id="verified",description="test after edits",
                     kind="tool_success",tool_name="process_exec",after_last_write=True)])
    history = History(observations=[obs(1,"process_exec"),obs(2,"patch_file")])
    gate = CompletionGate(Workspace(tmp_path))
    assert any("after the latest" in p for p in gate.check(state,FinalResponse(content="done",evidence_ids=["a2"]),history))
    history.observations.append(obs(3,"process_exec",False))
    assert gate.check(state,FinalResponse(content="done",evidence_ids=["a2"]),history)
    history.observations.append(obs(4,"process_exec"))
    assert not gate.check(state,FinalResponse(content="done",evidence_ids=["a4"]),history)
