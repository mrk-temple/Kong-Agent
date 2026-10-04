"""Permanent RC3 counterexample regressions."""
import asyncio
from kong.contracts import Act, Action, Criterion, FinalResponse
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.tools.defaults import default_registry
from kong.workspace import Workspace

def test_existing_create_is_known_and_does_not_dispatch_following_command(tmp_path):
    (tmp_path/'parser.py').write_text('original', encoding='utf-8')
    ws=Workspace(tmp_path)
    rt=Runtime.new('fix parser',ScriptedModel([]),ws,default_registry(ws,allow_process=True))
    asyncio.run(rt._actions(Act(actions=[Action(name='write_file',arguments={'path':'parser.py','content':'replacement'}),
        Action(name='process_exec',arguments={'argv':['python','check.py']})])))
    assert (tmp_path/'parser.py').read_text()=='original'
    assert len(rt.history.observations)==1
    obs=rt.history.observations[0]
    assert not obs.success and not obs.outcome_unknown
    assert 'patch_file' in obs.error and not rt.pending_action_ids


def test_failure_during_content_write_remains_unknown(tmp_path,monkeypatch):
    from pathlib import Path
    original=Path.open
    class BadStream:
        def __enter__(self): return self
        def __exit__(self,*args): self.inner.close()
        def write(self,content):
            self.inner.write('partial')
            self.inner.flush()
            raise FileExistsError('after write started')
    def failing(path,*args,**kwargs):
        stream=original(path,*args,**kwargs)
        if path.name=='new.txt' and args and args[0]=='x':
            bad=BadStream(); bad.inner=stream; return bad
        return stream
    monkeypatch.setattr(Path,'open',failing)
    ws=Workspace(tmp_path)
    from kong.tools.executor import ToolExecutor
    from kong.tools.base import ToolCall
    result=asyncio.run(ToolExecutor(default_registry(ws)).execute(ToolCall(name='write_file',arguments={'path':'new.txt','content':'new'})))
    assert not result.success and result.outcome_unknown
    assert (tmp_path/'new.txt').read_text()=='partial'


def test_parent_creation_race_remains_unknown(tmp_path,monkeypatch):
    from pathlib import Path
    original=Path.open
    calls=0
    def racing(path,*args,**kwargs):
        nonlocal calls
        if path.name=='created.txt' and args and args[0]=='x':
            calls+=1
            if calls==2:
                with original(path,'w',encoding='utf-8') as stream: stream.write('external')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',racing)
    ws=Workspace(tmp_path)
    from kong.tools.executor import ToolExecutor
    from kong.tools.base import ToolCall
    r=asyncio.run(ToolExecutor(default_registry(ws)).execute(ToolCall(name='write_file',arguments={'path':'new/created.txt','content':'ours'})))
    assert not r.success and r.outcome_unknown
    assert (tmp_path/'new/created.txt').read_text()=='external'


def test_known_create_failure_can_recover_with_patch_in_a_later_decision(tmp_path):
    (tmp_path/'parser.py').write_text('old',encoding='utf-8')
    ws=Workspace(tmp_path)
    model=ScriptedModel([
      Act(actions=[Action(name='write_file',arguments={'path':'parser.py','content':'new'})]),
      Act(actions=[Action(name='patch_file',arguments={'path':'parser.py','old_text':'old','new_text':'new'})]),
      Act(actions=[Action(name='read_file',arguments={'path':'parser.py'})]),
      FinalResponse(content='done',evidence_ids=['a3'])])
    rt=Runtime.new('fix existing parser',model,ws,default_registry(ws))
    rt.state.success_criteria=[Criterion(id='exact',description='exact',kind='file_equals',path='parser.py',expected_text='new')]
    asyncio.run(rt.run())
    assert rt.state.status.value=='completed'
    assert (tmp_path/'parser.py').read_text()=='new'
    assert len(rt.history.observations)==3 and not rt.history.observations[0].outcome_unknown
