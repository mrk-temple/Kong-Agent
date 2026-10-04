"""Permanent RC3 counterexample regressions."""
import asyncio
import pytest
from kong.contracts import Act, Action, FinalResponse
from kong.models.fake import ScriptedModel
from kong.runtime.gates import CompletionGate
from kong.runtime.loop import Runtime
from kong.tools.base import Tool, ToolResult
from kong.tools.defaults import default_registry
from kong.workspace import Workspace
from pydantic import BaseModel

class LifecycleArgs(BaseModel):
    operation: str
    session: str = ''
    page: str = ''
    unknown: bool = False
    failed_open: bool = False


class LifecycleTool(Tool):
    args_model = LifecycleArgs
    def __init__(self, name):
        self.name = name
        self.resources = set()
        self.counter = 0

    async def run(self, **kwargs):
        key = 'session' if self.name == 'terminal' else 'page'
        if kwargs['operation'] != 'close':
            self.counter += 1
            rid = f'{self.name}{self.counter}'
            self.resources.add(rid)
            return ToolResult(success=not kwargs.get('failed_open'), output={key: rid})
        rid = kwargs[key]
        if kwargs.get('unknown'):
            return ToolResult(success=False, outcome_unknown=True, error='unknown close effect')
        if rid not in self.resources:
            return ToolResult(success=False, error='unknown resource')
        self.resources.remove(rid)
        return ToolResult(success=True, output={'closed': rid})


def act(rt, name, **arguments):
    return asyncio.run(rt._actions(Act(actions=[Action(name=name, arguments=arguments)])))


def owned_run(path):
    (path/'answer.txt').write_text('Kong', encoding='utf-8')
    ws = Workspace(path)
    registry = default_registry(ws)
    registry.register(LifecycleTool('terminal'))
    rt = Runtime.new('exact answer then close session', ScriptedModel([]), ws, registry,
        completion_contract={'version':2, 'family':'data',
          'artifacts':[{'id':'answer','path':'answer.txt','format':'text','expected_text':'Kong'}],
          'resource_closures':[{'id':'terminal','tool':'terminal'}]})
    act(rt, 'terminal', operation='start')
    act(rt, 'read_file', path='answer.txt')
    return rt


def cleanup_row(rt):
    return next(row for row in CompletionGate(rt.workspace).criteria_results(rt.state, rt.history, {})
                if row['id']=='cleanup:terminal')


@pytest.mark.parametrize('case', ['missing','wrong','partial','reopened','unknown'])
def test_cleanup_requires_every_actual_opened_resource(tmp_path, case):
    rt = owned_run(tmp_path)
    if case == 'wrong': act(rt, 'terminal', operation='close', session='unrelated')
    if case == 'partial':
        act(rt, 'terminal', operation='start')
        act(rt, 'terminal', operation='close', session='terminal2')
    if case == 'reopened':
        act(rt, 'terminal', operation='close', session='terminal1')
        act(rt, 'terminal', operation='start')
    if case == 'unknown': act(rt, 'terminal', operation='close', session='terminal1', unknown=True)
    assert not cleanup_row(rt)['verified']
    assert len(rt.registry.get('terminal').resources) >= 1


def test_correct_final_value_readback_and_actual_cleanup_are_accepted(tmp_path):
    rt = owned_run(tmp_path)
    act(rt, 'terminal', operation='close', session='terminal1')
    row = cleanup_row(rt)
    assert row['verified'] and row['evidence_ids'] == ['a3']
    act(rt, 'read_file', path='answer.txt')
    gate = CompletionGate(rt.workspace)
    assert not gate.check(rt.state, FinalResponse(content='done', evidence_ids=['a4','a3']), rt.history)
    assert not rt.registry.get('terminal').resources


def test_cleanup_guidance_uses_observed_ids_without_dispatch(tmp_path):
    rt = owned_run(tmp_path)
    gate = CompletionGate(rt.workspace)
    decision = FinalResponse(content='done', evidence_ids=['a2'])
    f = gate.rejection_feedback(rt.state, decision, rt.history, gate.check(rt.state, decision, rt.history))
    close = next(s for s in f['remaining_steps'] if s['kind']=='close_resource')
    assert close['action']=={'name':'terminal','arguments':{'operation':'close','session':'terminal1'}}
    reads=[i for i,s in enumerate(f['remaining_steps']) if s.get('action',{}).get('name')=='read_file']
    assert reads and f['remaining_steps'].index(close)<reads[-1]
    assert len(rt.history.observations)==2 and 'terminal1' in rt.registry.get('terminal').resources


def test_current_binary_prefix_is_verified_and_wrong_bytes_rejected(tmp_path):
    from kong.completion_contracts import CompletionContract, has_business_conditions
    contract = CompletionContract.model_validate({'version':2,'family':'data','artifacts':[
        {'id':'proof','path':'proof.png','format':'file','binary_prefix_hex':'89504e470d0a1a0a','require_readback':False},
        {'id':'anchor','path':'state.json','format':'json','exact_json':{'status':'ok'}}]})
    assert has_business_conditions(contract)
    ws = Workspace(tmp_path)
    rt = Runtime.new('PNG header', ScriptedModel([]), ws, default_registry(ws), completion_contract=contract)
    (tmp_path/'proof.png').write_bytes(b'garbage')
    gate = CompletionGate(ws)
    assert not next(r for r in gate.criteria_results(rt.state, rt.history, {}) if r['id']=='artifact:proof')['verified']
    (tmp_path/'proof.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'fixture')
    assert next(r for r in gate.criteria_results(rt.state, rt.history, {}) if r['id']=='artifact:proof')['verified']


def test_old_v1_binding_identity_is_unchanged_and_new_fields_need_v2(tmp_path):
    from kong.runtime.loop import digest
    from kong.completion_contracts import CompletionContract
    old = CompletionContract.model_validate({'family':'data','artifacts':[{'id':'a','path':'a','format':'text','expected_text':'x'}]})
    dumped = old.model_dump(mode='json')
    dumped.pop('resource_closures')
    for a in dumped['artifacts']: a.pop('binary_prefix_hex')
    assert old.identity_dump() == dumped
    ws=Workspace(tmp_path)
    rt=Runtime.new('old contract',ScriptedModel([]),ws,default_registry(ws),completion_contract=old)
    assert next(e.payload['completion_contract_bound'] for e in rt.history.events if 'completion_contract_bound' in e.payload)==digest(dumped)
    with pytest.raises(ValueError, match='version=2'):
        CompletionContract.model_validate({**dumped,'resource_closures':[{'id':'t','tool':'terminal'}]})


def test_failed_navigation_still_owns_its_returned_page(tmp_path):
    rt=owned_run(tmp_path)
    rt.registry.register(LifecycleTool('browser'))
    from kong.completion_contracts import ResourceClosureRequirement
    rt.state.completion_contract.resource_closures.append(ResourceClosureRequirement(id='browser',tool='browser'))
    act(rt,'browser',operation='open',failed_open=True)
    row=next(r for r in CompletionGate(rt.workspace).criteria_results(rt.state,rt.history,{}) if r['id']=='cleanup:browser')
    assert row['required_actions']==[{'name':'browser','arguments':{'operation':'close','page':'browser1'}}]
    assert not row['verified']


def test_only_missing_cleanup_ids_is_a_submission_repair(tmp_path):
    rt=owned_run(tmp_path)
    act(rt,'terminal',operation='close',session='terminal1')
    act(rt,'read_file',path='answer.txt')
    gate=CompletionGate(rt.workspace)
    d=FinalResponse(content='done',evidence_ids=['a4'])
    f=gate.rejection_feedback(rt.state,d,rt.history,gate.check(rt.state,d,rt.history))
    assert 'submission:cleanup_evidence' in f['unsatisfied']
    assert f['additional_tool_required'] is False
    assert [s['kind'] for s in f['remaining_steps']]==['respond']
    assert 'a3' in f['current_valid_observations']
