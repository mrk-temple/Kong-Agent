import asyncio

from kong.contracts import FinalResponse, Status
from kong.models.base import ModelError, ModelProtocolError
from kong.runtime.loop import Runtime
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


class BrokenModel:
    def __init__(self, failures, error=ModelProtocolError):
        self.failures, self.error, self.calls = failures, error, 0

    async def generate(self, context):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error("safe diagnostic")
        return FinalResponse(content="done")


def test_protocol_error_has_one_visible_budgeted_repair(tmp_path):
    model = BrokenModel(1)
    runtime = Runtime.new("chat",model,Workspace(tmp_path),default_registry(Workspace(tmp_path)))
    state = asyncio.run(runtime.run())
    assert state.status == Status.COMPLETED and state.turn_count == model.calls == 2
    assert not runtime.history.observations
    assert any(e.type == "model_error" for e in runtime.history.events)


def test_repeated_protocol_errors_stop_and_http_errors_never_retry(tmp_path):
    for error, calls in [(ModelProtocolError,2),(ModelError,1)]:
        model = BrokenModel(3,error)
        runtime = Runtime.new("chat",model,Workspace(tmp_path),default_registry(Workspace(tmp_path)))
        assert asyncio.run(runtime.run()).status == Status.WAITING_USER
        assert model.calls == calls


def test_distinct_skill_preparation_gets_bounded_startup_allowance(tmp_path):
    from kong.contracts import Act, Action, NeedUserInput
    from kong.models.fake import ScriptedModel
    calls = [Act(actions=[Action(name="skill_load",arguments={"name":"coding"})]) for _ in range(3)]
    calls += [Act(actions=[Action(name="skill_read",arguments={"name":"coding","resource":"references/verification.md"})]),
              Act(actions=[Action(name="write_file",arguments={"path":"result.txt","content":"ok"})]),
              NeedUserInput(question="stop")]
    runtime = Runtime.new("task",ScriptedModel(calls),Workspace(tmp_path),default_registry(Workspace(tmp_path)))
    asyncio.run(runtime.run())
    assert (tmp_path / "result.txt").read_text() == "ok"
    assert runtime.state.turn_budget == 6
    assert runtime.state.progress_state.progress_count == 1
    assert sum(bool(e.payload.get("skill_preparation_allowance")) for e in runtime.history.events) == 1
