"""Permanent RC3 counterexample regressions."""
import asyncio
from kong.contracts import Act, Action, Criterion, FinalResponse
from kong.models.fake import ScriptedModel
from kong.runtime.gates import CompletionGate
from kong.runtime.loop import Runtime
from kong.tools.defaults import default_registry
from kong.workspace import Workspace

def act(rt, name, **arguments):
    return asyncio.run(rt._actions(Act(actions=[Action(name=name, arguments=arguments)])))


def weak_run(path):
    (path/'answer.txt').write_text('Kong', encoding='utf-8')
    ws = Workspace(path)
    rt = Runtime.new('verify answer', ScriptedModel([]), ws, default_registry(ws))
    rt.state.success_criteria = [Criterion(id='answer', description='answer', kind='file_exists', path='answer.txt')]
    act(rt, 'read_file', path='answer.txt')
    return rt


def test_missing_authority_has_consistent_ask_feedback(tmp_path):
    rt = weak_run(tmp_path)
    gate = CompletionGate(rt.workspace)
    decision = FinalResponse(content='done', evidence_ids=['a1'])
    f = gate.rejection_feedback(rt.state, decision, rt.history, gate.check(rt.state, decision, rt.history))
    assert f['remaining_steps'][0]['kind'] == 'ask'
    assert 'Ask' in f['next_action'] and 'corrected respond' not in f['next_action']
    assert 'authority:acceptance_conditions' in f['unsatisfied']
    assert f['additional_tool_required'] is False
    assert len(rt.history.observations) == 1


def test_missing_authority_context_never_requests_completion(tmp_path):
    rt = weak_run(tmp_path)
    from kong.runtime.finalization import completion_guidance
    f = completion_guidance(CompletionGate(rt.workspace), rt.state, rt.history)
    assert 'Ask' in f['instruction'] and 'submit a corrected respond' not in f['instruction']
