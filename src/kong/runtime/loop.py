"""One runtime loop, shared by every mode. Policy lives in the controller."""
import asyncio
import hashlib
import json
from time import perf_counter
from collections.abc import Callable

from kong.context import ContextBuilder
from kong.continuity.contracts import ContextBudget, Thread
from kong.continuity.engine import ContextEngine
from kong.continuity.storage import ThreadStore
from kong.continuity.history import HistorySelector
from kong.contracts import (
    Act, History, Mode, NeedDiscovery, Observation, RunState,
    Status, Transition, RunMetrics,
)
from kong.models.base import Model, ModelError, ModelProtocolError
from kong.runtime.controller import RuntimeController
from kong.runtime.gates import CompletionGate, ProgressGate
from kong.runtime.state import create_run_state
from kong.storage import RunStore, Snapshot
from kong.tools.base import ToolCall
from kong.tools.executor import ToolExecutor
from kong.tools.registry import ToolRegistry
from kong.workspace import Workspace
from kong.skills.tools import SkillTool


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


class Runtime:
    def __init__(self, model: Model, workspace: Workspace, registry: ToolRegistry,
                 state: RunState, *, history: History | None = None,
                 store: RunStore | None = None, context_builder: ContextBuilder | None = None,
                 context_engine: ContextEngine | None = None, thread_id: str | None = None,
                 thread_store: ThreadStore | None = None,
                 memory_store=None, project_id: str | None = None,
                 on_event: Callable[[str, dict], None] | None = None, driver: str = "compatible"):
        self.model = model
        self.workspace = workspace
        self.registry = registry
        self.executor = ToolExecutor(registry)
        self.state = state
        self.history = history if history is not None else History()
        self._record_initial_goal = history is None
        self.store = store
        self.context_builder = context_builder or ContextBuilder()
        if context_engine is not None and context_builder is not None:
            raise ValueError("Pass context_engine or context_builder, not both")
        self.context_engine = context_engine or ContextEngine(context_builder)
        self.thread_store = thread_store or (ThreadStore(store.directory.parent / "context.db") if store else None)
        if self.thread_store:
            existing = self.thread_store.thread_for_run(state.run_id)
            chosen = thread_id or existing
            self.thread = (self.thread_store.get(chosen) if chosen else
                           self.thread_store.create(workspace.root, state.goal, thread_id=state.run_id))
            if self.store:
                for prior_id in self.thread_store.runs(self.thread.thread_id):
                    try:
                        prior = self.store.load(prior_id)
                    except (OSError, ValueError):
                        continue
                    if project_id is None:
                        project_id = prior.project_id
                    elif project_id != prior.project_id:
                        raise ValueError("Thread already belongs to a different project scope; start a new Thread")
                    break
            self.thread_store.attach_run(self.thread.thread_id, state.run_id, workspace.root)
        else:
            self.thread = Thread(thread_id=thread_id or state.run_id, workspace=str(workspace.root), title=state.goal)
        if self.store and self.thread_store:
            self.context_engine.history_selector = HistorySelector(self.store, self.thread_store)
        self.context_engine.bind(self.thread, self.store, long_term_store=memory_store, project_id=project_id,
                                 thread_store=self.thread_store, run_id=state.run_id)
        if self.store and not self.context_engine.legacy:
            from kong.continuity.recall import RecallHistory
            self.registry = ToolRegistry()
            for name in registry.names():
                if name != "recall_history":
                    self.registry.register(registry.get(name))
            self.registry.register(RecallHistory(self.store, self.thread_store, self.thread))
            self.executor = ToolExecutor(self.registry)
        if context_engine is None and hasattr(model, "config"):
            self.context_engine.budget = ContextBudget(window_tokens=model.config.context_window_tokens,
                                                      output_reserved_tokens=model.config.max_tokens)
        self.controller = RuntimeController(CompletionGate(workspace))
        self.on_event = on_event or (lambda kind, data: None)
        self.pending_action_id: str | None = None
        self.driver = driver
        self.metrics = RunMetrics()

    @classmethod
    def new(cls, goal: str, model: Model, workspace: Workspace, registry: ToolRegistry,
            mode: Mode = Mode.AUTO, **kwargs) -> "Runtime":
        return cls(model, workspace, registry, create_run_state(goal, mode), **kwargs)

    @classmethod
    def restore(cls, snapshot: Snapshot, model: Model, workspace: Workspace,
                registry: ToolRegistry, **kwargs) -> "Runtime":
        from pathlib import Path
        if Path(snapshot.workspace).resolve() != workspace.root:
            raise ValueError("Resume must use the original workspace")
        if "thread_id" in kwargs:
            raise ValueError("Restore uses the saved Thread association")
        if kwargs.get("project_id") is None:
            kwargs["project_id"] = snapshot.project_id
        elif snapshot.project_id is not None and kwargs["project_id"] != snapshot.project_id:
            raise ValueError("Restore must preserve the original project scope")
        thread_store = kwargs.get("thread_store")
        if thread_store is None and kwargs.get("store") is not None:
            thread_store = ThreadStore(kwargs["store"].directory.parent / "context.db")
            kwargs["thread_store"] = thread_store
        if snapshot.thread_id and thread_store:
            # The snapshot is authoritative for membership if derived SQLite
            # metadata was lost. Rebuild lazily, without migrating old Runs.
            try:
                thread_store.get(snapshot.thread_id)
            except ValueError:
                thread_store.create(workspace.root, snapshot.state.goal, thread_id=snapshot.thread_id)
        runtime = cls(model, workspace, registry, snapshot.state, history=snapshot.history,
                      driver=snapshot.driver, thread_id=snapshot.thread_id, **kwargs)
        runtime.pending_action_id = snapshot.pending_action_id
        runtime.metrics = snapshot.metrics or RunMetrics(legacy_unknown=True)
        from kong.reporting import resource_handles
        unavailable = {}
        for name, ids in resource_handles(runtime.history).items():
            tool = runtime.registry.get(name) if name in runtime.registry.names() else None
            known = getattr(tool, {"terminal":"sessions", "browser":"pages", "mcp":"connections"}[name], {})
            missing = sorted(ids - set(known))
            if missing:
                unavailable[name] = missing
        if unavailable:
            runtime.history.append("feedback", runtime.state.turn_count, unavailable_resources=unavailable,
                instruction="恢复了任务记录，但这些交互连接不可用。先检查已有产物与副作用；不要自动重放旧操作，旧句柄不能当作活连接。")
        if snapshot.pending_action_id:
            runtime.state.status = Status.WAITING_USER
            runtime.state.pending_question = (
                "上次执行在动作期间中断，结果未知。请检查工作区，再使用 /resolve 描述实际结果；不会自动重放动作。")
        return runtime

    def snapshot(self) -> Snapshot:
        return Snapshot(workspace=str(self.workspace.root), state=self.state,
                        history=self.history, pending_action_id=self.pending_action_id, driver=self.driver,
                        thread_id=self.thread.thread_id, project_id=self.context_engine.project_id, metrics=self.metrics)

    def save(self) -> None:
        if self.store:
            self.store.save(self.snapshot())

    def human(self, command: str, **kwargs) -> None:
        if self.pending_action_id:
            raise ValueError("Resolve the interrupted action with /resolve before continuing")
        self.controller.human(self.state, self.history, command, **kwargs)
        self.save()

    async def memory_command(self, command: str, *, item_id: str = "", text: str = "",
                             kind: str = "fact", key: str = "", scope: str = "thread"):
        """Human-only memory management; never changes Runtime control state."""
        from kong.continuity.memory import source_for, stable_id
        from kong.continuity.memory_contracts import MemoryDelta, MemoryMutation, MemoryScope, MemoryStatus
        from kong.continuity.retrieval import scope_matches
        engine = self.context_engine
        if not engine.store:
            raise ValueError("Memory management requires persistent storage")
        if command == "rebuild":
            return await engine.rebuild()
        current = (self.state.run_id, self.history)
        if command == "add":
            MemoryScope(scope)
            # Validate before writing any source event.
            if kind not in {"fact", "preference", "constraint", "decision", "open_loop", "artifact", "procedure", "project"} or not key or not text.strip():
                raise ValueError("Expected kind, key, scope and nonempty content")
            if scope == "project" and not engine.project_id:
                raise ValueError("Project scope requires --project-id")
            MemoryMutation(kind=kind, key=key, content=text,
                           sources=[source_for(self.thread.thread_id, self.state.run_id, len(self.history.events))])
            self.history.append("human", self.state.turn_count, command="memory_add", text=text,
                                kind=kind, key=key, scope=scope)
            self.save()
            await engine.commit(state=self.state, history=self.history)
            return
        thread_state = engine.store.state(self.thread.thread_id)
        target = next((i for i in thread_state.items if i.id == item_id), None)
        record, record_store = None, None
        if target is None:
            for memory_store in {s.path: s for s in [engine.store, engine.long_term_store]}.values():
                try:
                    record = memory_store.get_memory(item_id)
                    record_store = memory_store
                    break
                except ValueError:
                    pass
            if record is None or not scope_matches(record, self.thread, engine.project_id):
                raise ValueError("Unknown memory in current scope")
            target = next((i for i in thread_state.items if i.id == record.thread_item_id), None)
        if command == "confirm":
            if target is None:
                raise ValueError("Confirm a candidate in its original Thread")
            if target.status not in {MemoryStatus.CANDIDATE, MemoryStatus.ACTIVE}:
                raise ValueError("Cannot confirm an inactive memory")
            self.history.append("human", self.state.turn_count, command="memory_confirm", text="Confirmed exact memory",
                                item_id=target.id, content_hash=stable_id(target.content))
            self.save()
            source = source_for(self.thread.thread_id, self.state.run_id, len(self.history.events) - 1)
            engine.manager.confirm(target.id, source, current)
        elif command in {"resolve", "reject", "forget"}:
            self.history.append("human", self.state.turn_count, command="memory_" + command,
                                text=text or command, item_id=target.id if target else item_id, requested_id=item_id)
            self.save()
            source = source_for(self.thread.thread_id, self.state.run_id, len(self.history.events) - 1)
            if target:
                engine.manager.merge(MemoryDelta(thread_id=self.thread.thread_id, base_revision=thread_state.revision,
                    mutations=[MemoryMutation(operation="resolve" if command == "resolve" else "reject",
                        key=target.key, kind=target.kind, content=target.content, target_id=target.id, sources=[source])]), current)
            if record:
                record.status = MemoryStatus.RESOLVED if command == "resolve" else MemoryStatus.REJECTED
                record.sources.append(source)
                record_store.save_memory(record)
        else:
            raise ValueError("Unknown memory command")
        await engine.commit(state=self.state, history=self.history)

    def resolve_interrupted_action(self, text: str) -> None:
        if not self.pending_action_id or not text.strip():
            raise ValueError("There is no uncertain action, or the description is empty")
        self.history.append("human", self.state.turn_count, command="resolve_interrupted_action",
                            action_id=self.pending_action_id, text=text,
                            instruction="Inspect real environment evidence before further writes or completion.")
        self.pending_action_id = None
        self.controller.human(self.state, self.history, "answer", text=text)
        self.save()

    async def run(self) -> RunState:
        started = perf_counter()
        try:
            return await self._run_impl()
        finally:
            self.metrics.active_seconds += perf_counter() - started
            self.save()
            if self.store:
                from kong.reporting import run_report
                self.store.save_report(self.state.run_id, run_report(self))

    async def _generate(self, context):
        started = perf_counter()
        self.metrics.model_calls += 1
        try:
            return await self.model.generate(context)
        finally:
            self.metrics.model_seconds += perf_counter() - started
            usage = getattr(self.model, "last_usage", {})
            clean = {k:v for k,v in usage.items() if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
                     and type(v) is int and v >= 0} if isinstance(usage, dict) else {}
            if "total_tokens" in clean:
                self.metrics.usage_calls += 1
            for key, value in clean.items():
                self.metrics.tokens[key] = self.metrics.tokens.get(key, 0) + value

    async def _run_impl(self) -> RunState:
        if self._record_initial_goal and self.state.status == Status.RUNNING:
            self.history.append("human", self.state.turn_count, command="goal", text=self.state.goal)
            self._record_initial_goal = False
        self.save()
        try:
            while self.controller.start_turn(self.state):
                # Save the consumed turn before the request, including failed HTTP requests.
                self.save()
                try:
                    skill_tool = next((self.registry.get(n) for n in self.registry.names()
                                       if isinstance(self.registry.get(n), SkillTool)), None)
                    self.context_engine.supplemental_items = (skill_tool.catalog.context_items(self.history, self.state.run_id)
                                                              if skill_tool else [])
                    context = await self.context_engine.prepare(thread=self.thread, state=self.state,
                                                                history=self.history, tools=self.registry.definitions())
                    decision = await self._generate(context)
                except ModelError as exc:
                    previous = next((e for e in reversed(self.history.events)
                                     if e.type in {"decision", "model_error"}), None)
                    self.history.append("model_error", self.state.turn_count, error=str(exc))
                    if isinstance(exc, ModelProtocolError) and (previous is None or previous.type != "model_error"):
                        self.history.append("feedback", self.state.turn_count,
                            error=str(exc), instruction="No action executed. Correct the JSON format on the next budgeted turn.")
                        self.on_event("transition", {"turn":self.state.turn_count, "reason":"protocol_repair", "control":"continue"})
                        self.save()
                        continue
                    self.state.status = Status.WAITING_USER
                    self.state.pending_question = str(exc) + " 修正服务配置后回复 /answer 重试。"
                    self.save()
                    break
                except Exception as exc:
                    self.history.append("model_error", self.state.turn_count, error=type(exc).__name__)
                    self.state.status = Status.FAILED
                    self.state.stop_reason = "unexpected_model_error"
                    self.save()
                    break
                self.history.append("decision", self.state.turn_count, decision=decision.model_dump(mode="json"))
                proposal_provider = getattr(self.model, "take_memory_delta", None)
                if proposal_provider:
                    proposal = proposal_provider()
                    if proposal is not None:
                        self.history.append("feedback", self.state.turn_count, memory_delta_proposal=proposal)
                context_only = (isinstance(decision, (Act, NeedDiscovery)) and all(
                    action.name in self.registry.names() and isinstance(self.registry.get(action.name), SkillTool)
                    for action in decision.actions))
                if isinstance(decision, (Act, NeedDiscovery)) and not context_only:
                    self.state.requires_evidence = True
                denial = self.controller.authorize(self.state, decision, context_only=context_only)
                if denial:
                    self.history.append("feedback", self.state.turn_count, error=denial)
                    transition = self.controller.checkpoint(self.state)
                else:
                    if context_only:
                        for action in decision.actions:
                            result = await self.executor.execute(ToolCall(name=action.name, arguments=action.arguments))
                            self.history.append("feedback", self.state.turn_count, skill_action=action.model_dump(),
                                                skill_result=result.model_dump())
                            self.save()
                        # One bounded startup allowance for actually loading a skill
                        # plus a reference. No evidence/progress credit, no hard-limit
                        # increase, and repeated loads cannot earn it.
                        resources = {(e.payload["skill_result"]["output"].get("id"),
                                      e.payload["skill_result"]["output"].get("resource"))
                                     for e in self.history.events
                                     if e.payload.get("skill_result", {}).get("success")
                                     and isinstance(e.payload["skill_result"].get("output"), dict)
                                     and e.payload["skill_result"]["output"].get("resource")}
                        if (self.state.mode == Mode.AUTO and not self.history.observations
                                and self.state.turn_count == self.state.turn_budget == 4
                                and len(resources) >= 2):
                            self.state.turn_budget = min(6, self.state.hard_turn_limit)
                            self.history.append("feedback", self.state.turn_count,
                                instruction="Two startup turns granted after distinct skill/reference preparation; no environment progress credited.",
                                skill_preparation_allowance=2)
                        transition = (Transition(control="continue", reason="skill_context_loaded")
                                      if self.state.pending_approval else self.controller.checkpoint(self.state))
                    elif isinstance(decision, (Act, NeedDiscovery)):
                        await self._actions(decision)
                        transition = self.controller.transition(self.state, decision, self.history)
                    else:
                        transition = self.controller.transition(self.state, decision, self.history)
                self.history.append("feedback", self.state.turn_count, transition=transition.model_dump())
                self.on_event("transition", {"turn": self.state.turn_count, **transition.model_dump()})
                self.save()
                await self.context_engine.commit(state=self.state, history=self.history)
        except asyncio.CancelledError:
            self.controller.human(self.state, self.history, "interrupt")
            self.save()
            await self.context_engine.commit(state=self.state, history=self.history)
            raise
        self.save()
        await self.context_engine.commit(state=self.state, history=self.history)
        return self.state

    async def _actions(self, decision: Act | NeedDiscovery) -> None:
        if any(action.name in self.registry.names() and isinstance(self.registry.get(action.name), SkillTool)
               for action in decision.actions):
            self.history.append("feedback", self.state.turn_count, error="Separate skill context operations from environment actions; batch rejected.")
            return
        if isinstance(decision, NeedDiscovery):
            # Validate the entire discovery batch before touching the environment.
            for action in decision.actions:
                try:
                    if not self.registry.get(action.name).read_only:
                        self.history.append("feedback", self.state.turn_count,
                                            error="Discovery allows only read-only actions; batch rejected.")
                        return
                except KeyError:
                    self.history.append("feedback", self.state.turn_count, error="Unknown discovery tool; batch rejected.")
                    return
        for action in decision.actions:
            # Include unresolved intents so crash recovery can never reuse an evidence ID.
            action_id = f"a{1 + sum(event.type == 'action_intent' for event in self.history.events)}"
            self.pending_action_id = action_id
            self.history.append("action_intent", self.state.turn_count, action_id=action_id, action=action.model_dump())
            self.save()
            if action.name in {"process_exec", "terminal", "browser", "mcp"}:
                self.on_event("action_started", {"id": action_id, "tool": action.name})
            result = await self.executor.execute(ToolCall(name=action.name, arguments=action.arguments))
            progress_output = (self.registry.get(action.name).progress_output(result.output)
                               if action.name in self.registry.names() else result.output)
            observation = Observation(
                action_id=action_id, turn=self.state.turn_count, action=action,
                success=result.success, output=result.output, error=result.error,
                fingerprint=digest(action.model_dump()),
                # Reading a different required file is new evidence even if its text
                # matches another file. The same source+result cannot earn another lease.
                output_hash=digest({"action": action.model_dump(), "output": progress_output}),
            )
            self.history.observations.append(observation)
            self.history.append("observation", self.state.turn_count, observation=observation.model_dump(mode="json"))
            self.pending_action_id = None
            ProgressGate.observe(self.state, self.history)
            self.save()
            self.on_event("observation", {"id": action_id, "tool": action.name, "success": result.success})
            if not result.success or ProgressGate.trigger(self.state) in {"consecutive_failures", "repeated_actions"}:
                break


async def run_agent(user_input: str, model: Model, registry: ToolRegistry,
                    workspace: Workspace, mode: Mode = Mode.AUTO) -> RunState:
    return await Runtime.new(user_input, model, workspace, registry, mode).run()
