import asyncio
from time import perf_counter

from kong.contracts import Act, Action, NeedUserInput, Status
from kong.models.fake import ScriptedModel
from kong.reporting import run_report
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.base import ToolResult
from kong.tools.builtin.read_file import ReadFileTool
from kong.tools.registry import ToolRegistry
from kong.workspace import Workspace


def test_parallel_timings_correlate_results_and_separate_sum_from_wall(tmp_path):
    observed = {}
    class SlowRead(ReadFileTool):
        async def run(self, **kwargs):
            started = perf_counter()
            await asyncio.sleep(0.06)
            result = await super().run(**kwargs)
            observed[kwargs['path']] = perf_counter() - started
            return result
    for name in ['one.txt','two.txt']:
        (tmp_path / name).write_text(name)
    workspace = Workspace(tmp_path)
    registry = ToolRegistry()
    registry.register(SlowRead(workspace))
    model = ScriptedModel([Act(actions=[Action(name='read_file',arguments={'path':n}) for n in ['one.txt','two.txt']]),
                           NeedUserInput(question='review')])
    runtime = Runtime.new('read',model,workspace,registry,store=RunStore(tmp_path/'.kong/runs'))
    assert asyncio.run(runtime.run()).status == Status.WAITING_USER
    m = runtime.metrics
    assert m.tool_calls == 2 and [t.action_id for t in m.tool_timings] == ['a1','a2']
    # Loop timers can use a coarser clock than perf_counter. Compare with actual
    # independently observed work, rather than the requested sleep interval.
    assert all(t.success and t.elapsed_seconds >= observed[path] > 0
               for t, path in zip(m.tool_timings, ['one.txt', 'two.txt']))
    assert m.tool_seconds == sum(t.elapsed_seconds for t in m.tool_timings)
    assert m.tool_seconds > m.tool_wall_seconds
    report = run_report(runtime)['metrics']
    assert report['runtime_overhead_seconds'] >= 0 and report['tool_timing_complete']
    finished = [e for e in runtime.run_events if e.kind == 'action_finished']
    assert [e.data['elapsed_seconds'] for e in finished] == [t.elapsed_seconds for t in m.tool_timings]
    restored = Runtime.restore(runtime.store.load(runtime.state.run_id),model,workspace,registry,store=runtime.store)
    assert restored.metrics.tool_timings == m.tool_timings and restored.metrics.tool_seconds == m.tool_seconds


def test_failed_tool_and_cancelled_execution_keep_their_own_durations(tmp_path):
    async def scenario():
        entered = asyncio.Event()
        class Read(ReadFileTool):
            async def run(self, **kwargs):
                if kwargs['path'] == 'fail.txt':
                    return ToolResult(success=False,error='fixture failure')
                entered.set()
                await asyncio.sleep(60)
        workspace = Workspace(tmp_path)
        registry = ToolRegistry()
        registry.register(Read(workspace))
        runtime = Runtime.new('read',ScriptedModel([]),workspace,registry,store=RunStore(tmp_path/'.kong/runs'))
        failed = await runtime._execute_timed('a1',Action(name='read_file',arguments={'path':'fail.txt'}))
        assert not failed.success
        task = asyncio.create_task(runtime._execute_timed('a2',Action(name='read_file',arguments={'path':'slow.txt'})))
        await entered.wait()
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        one,two = runtime.metrics.tool_timings
        assert one.success is False and one.elapsed_seconds is not None
        assert two.success is None and two.cancelled and two.elapsed_seconds is not None
        assert not two.outcome_unknown  # Read-only cancellation has no unknown write effect.
        assert runtime.metrics.tool_calls == 2
    asyncio.run(scenario())


def test_legacy_tool_durations_are_unknown_and_never_filled_from_event_wall_clock(tmp_path):
    workspace = Workspace(tmp_path)
    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace))
    runtime = Runtime.new('old',ScriptedModel([]),workspace,registry)
    runtime.history.append('action_intent',1,action_id='a1',action={'name':'read_file','arguments':{'path':'one.txt'}})
    restored = Runtime.restore(runtime.snapshot(),ScriptedModel([]),workspace,registry)
    report = run_report(restored)['metrics']
    assert report['tool_timing_complete'] is False
    assert report['runtime_overhead_seconds'] is None and not report['tool_timings']


def test_timing_malformed_arguments_cannot_break_validation_or_drop_other_results(tmp_path):
    (tmp_path/'ok.txt').write_text('ok')
    workspace = Workspace(tmp_path)
    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace))
    model = ScriptedModel([Act(actions=[Action(name='read_file',arguments={'path':123,'operation':False}),
                                       Action(name='read_file',arguments={'path':'ok.txt'})]),
                           NeedUserInput(question='review')])
    runtime = Runtime.new('read',model,workspace,registry)
    assert asyncio.run(runtime.run()).status == Status.WAITING_USER
    assert [o.success for o in runtime.history.observations] == [False,True]
    assert runtime.metrics.tool_calls == 2 and runtime.metrics.tool_timings[0].operation == 'invalid'
