"""One runtime loop, shared by every mode. Policy lives in the controller."""
import asyncio
import hashlib
import json
from time import perf_counter
from collections.abc import Callable

from kong.context import ContextBuilder
from kong.continuity.contracts import ContextBudget, Thread, ContextItem, ContextSource
from kong.continuity.engine import ContextEngine
from kong.continuity.storage import ThreadStore
from kong.continuity.history import HistorySelector
from kong.contracts import (
    Act, History, Mode, NeedDiscovery, Observation, RunState,
    Status, Transition, RunMetrics, Phase, ToolTiming,
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
from kong.runtime.evidence import capture_versions, action_effect
from kong.runtime.interaction import RunEvent, RunInput
from kong.tools.permissions import PermissionPolicy, policy_fingerprint
from kong.completion_contracts import CompletionContract
from kong.runtime.metrics import measure, metrics_scope


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


class Runtime:
    def __init__(self, model: Model, workspace: Workspace, registry: ToolRegistry,
                 state: RunState, *, history: History | None = None,
                 store: RunStore | None = None, context_builder: ContextBuilder | None = None,
                 context_engine: ContextEngine | None = None, thread_id: str | None = None,
                 thread_store: ThreadStore | None = None,
                 memory_store=None, project_id: str | None = None,
                 on_event: Callable[[str, dict], None] | None = None, driver: str = "compatible",
                 permissions: PermissionPolicy | None = None, read_concurrency: int = 3,
                 model_retry_limit: int = 2, completion_contract: CompletionContract | dict | None = None):
        self.model = model
        self.workspace = workspace
        self.registry = registry
        self.executor = ToolExecutor(registry)
        self.state = state
        self.history = history if history is not None else History()
        self._record_initial_goal = history is None
        self._driver_started = None
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
        self.metrics = RunMetrics(attribution_coverage="current_driver")
        if not 1 <= read_concurrency <= 8 or not 0 <= model_retry_limit <= 3:
            raise ValueError("Read concurrency must be 1..8; model retry limit must be 0..3")
        self.read_concurrency = read_concurrency
        self.model_retry_limit = model_retry_limit
        self.executor.permissions = permissions or PermissionPolicy()
        self.pending_action_ids: list[str] = []
        self.queued_inputs: list[RunInput] = []
        self.cancel_input: RunInput | None = None
        self.cancel_applied = False
        self.constraints: list[str] = []
        self.run_events: list[RunEvent] = []
        self._running = False
        self._model_task = None
        self._input_signal = asyncio.Event()
        self._last_delta = 0
        self._cancel_requested = False
        self._active_cancellable_task = None
        if completion_contract is not None:
            if history is not None:
                raise ValueError("Restore preserves its saved completion contract; update it explicitly after restore")
            self.set_completion_contract(completion_contract)

    def set_completion_contract(self, contract: CompletionContract | dict) -> None:
        """Host-only binding; never rebaseline already protected bytes on recovery."""
        if self._running or self.pending_action_ids or self.state.turn_count and not self.state.contract_invalidated:
            raise ValueError("Bind before execution, or explicitly replace an invalidated host contract while paused")
        contract = CompletionContract.model_validate(contract).model_copy(deep=True)
        for path in contract.version_paths():
            self.workspace.resolve(path)
        for command in contract.commands:
            self.workspace.resolve(command.cwd)
        from kong.runtime.evidence import fingerprint
        protected = {}
        for path in contract.protected_paths:
            if path in self.state.contract_protected_versions:
                protected[path] = self.state.contract_protected_versions[path]
            elif self.history.observations:
                raise ValueError("Cannot establish a new protected-source baseline after actions have run")
            else:
                protected[path] = fingerprint(self.workspace, path)
            if protected[path] in {None, "missing"}:
                raise ValueError("A protected source must have a readable initial version")
        self.state.completion_contract = contract
        self.state.contract_protected_versions = protected
        self.state.contract_invalidated = False
        self.history.append("feedback", self.state.turn_count, completion_contract_bound=digest(contract.model_dump(mode="json")))
        self.save()

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
        runtime.pending_action_ids = list(snapshot.pending_action_ids)
        if snapshot.pending_action_id and snapshot.pending_action_id not in runtime.pending_action_ids:
            runtime.pending_action_ids.insert(0, snapshot.pending_action_id)
        runtime.queued_inputs = list(snapshot.queued_inputs)
        runtime.cancel_input = snapshot.cancel_input
        runtime.cancel_applied = snapshot.cancel_applied
        runtime._cancel_requested = runtime.cancel_input is not None
        runtime.constraints = list(snapshot.constraints)
        runtime.run_events = list(snapshot.run_events)
        runtime.metrics = snapshot.metrics or RunMetrics(legacy_unknown=True)
        if runtime.metrics.attribution_coverage == "current_driver":
            runtime.metrics.attribution_coverage = "partial_history"
        not_dispatched = {o.action_id for o in runtime.history.observations if not o.dispatched}
        recorded_calls = sum((e.type == "action_intent" and e.payload.get("action_id") not in not_dispatched)
                             or "skill_action" in e.payload for e in runtime.history.events)
        if (runtime.metrics.legacy_unknown or recorded_calls > runtime.metrics.tool_calls
                or any(t.elapsed_seconds is None for t in runtime.metrics.tool_timings)):
            runtime.metrics.tool_timing_complete = False
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
        if runtime.pending_action_ids:
            runtime.pending_action_id = runtime.pending_action_ids[0]
            runtime.state.status = Status.WAITING_USER
            runtime.state.pending_question = (
                "上次执行在动作期间中断，结果未知。请检查工作区，再使用 /resolve 描述实际结果；不会自动重放动作。")
        return runtime

    def snapshot(self) -> Snapshot:
        return Snapshot(workspace=str(self.workspace.root), state=self.state,
                        history=self.history, pending_action_id=self.pending_action_id, driver=self.driver,
                        thread_id=self.thread.thread_id, project_id=self.context_engine.project_id, metrics=self.metrics,
                        pending_action_ids=self.pending_action_ids, queued_inputs=self.queued_inputs,
                        cancel_input=self.cancel_input, cancel_applied=self.cancel_applied,
                        constraints=self.constraints, run_events=self.run_events)

    def save(self) -> None:
        if self.store:
            self.metrics.snapshot_calls += 1
            with measure("snapshot_seconds", self.metrics):
                size = self.store.save(self.snapshot())
            self.metrics.snapshot_bytes = size if type(size) is int else None

    def emit(self, kind: str, data: dict) -> None:
        event = RunEvent(sequence=1 + len(self.run_events),
                         run_id=self.state.run_id, thread_id=self.thread.thread_id,
                         turn=self.state.turn_count, status=self.state.status.value, kind=kind, data=data)
        self.run_events.append(event)
        self.save()  # A displayed event always has a durable counterpart when storage is enabled.
        try:
            self.on_event(kind, {**data, "event": event.model_dump(mode="json")})
        except Exception as exc:
            self.history.append("feedback", self.state.turn_count, event_callback_error=type(exc).__name__)

    def submit_input(self, kind: str, text: str = "") -> str:
        """Call on the owning event loop; other threads use call_soon_threadsafe."""
        if kind == "cancel" and self.cancel_input is not None:
            return self.cancel_input.id  # One durable request, including duplicate delivery after stopping.
        if self.state.status != Status.RUNNING or (kind != "cancel" and len(self.queued_inputs) >= 32):
            raise ValueError("Input queue requires a running task and has a 32-message limit")
        item = RunInput(kind=kind, text=text.strip())
        if item.kind != "cancel" and not item.text:
            raise ValueError("A constraint or replacement goal requires text")
        if item.kind == "cancel":
            self.cancel_input = item
            self.cancel_applied = False
            self._cancel_requested = True
        else:
            self.queued_inputs.append(item)
        self._input_signal.set()
        self.emit("input_queued", {"input_id": item.id, "kind": item.kind, "boundary": "before_next_action_or_decision"})
        if kind == "cancel" and self._model_task and not self._model_task.done():
            self._model_task.cancel()
        if kind == "cancel":
            if self._active_cancellable_task and not self._active_cancellable_task.done():
                self._active_cancellable_task.cancel()
        return item.id

    def _apply_inputs(self) -> bool:
        if not self._inputs_pending():
            return False
        items, self.queued_inputs = self.queued_inputs, []
        if self.cancel_input is not None and not self.cancel_applied:
            items.append(self.cancel_input)
            self.cancel_applied = True
        # Versions 1..3 persisted cancel in the ordinary queue.
        legacy_cancel = next((item for item in items if item.kind == "cancel"), None)
        if legacy_cancel is not None:
            self.cancel_input = legacy_cancel
            self.cancel_applied = True
            self._cancel_requested = True
        self._input_signal.clear()
        cancelling = any(item.kind == "cancel" for item in items)
        contract_changed = self.state.completion_contract is not None and any(item.kind != "cancel" for item in items)
        for item in items:
            previous = {}
            if item.kind == "goal":
                previous = {"previous_goal": self.state.goal,
                            "retired_criteria": [c.model_dump(mode="json") for c in self.state.success_criteria]}
            if item.kind != "cancel" and self.state.plan:
                previous["retired_plan"] = self.state.plan.model_dump(mode="json")
            self.history.append("human", self.state.turn_count, command="input_" + item.kind,
                                text=item.text, input_id=item.id, **previous)
            if item.kind == "constraint":
                self.constraints.append(item.text)
            elif item.kind == "goal":
                self.state.goal = item.text
                self.state.success_criteria = []  # Old goal contracts remain in audit history, not new authority.
            if item.kind != "cancel" and self.state.plan:
                self.state.plan = None
                self.state.requires_replan = True
                self.state.pending_approval = None
                self.state.current_plan_step = None
        if contract_changed:
            self.state.contract_invalidated = True
        if cancelling:
            self.state.status = Status.STOPPED
            self.state.stop_reason = "user_interrupted"
            self.state.pending_question = None
        elif contract_changed:
            self.state.status = Status.WAITING_USER
            self.state.pending_question = "目标或约束已改变。请调用方更新完成合同后明确继续；模型不能替换合同。"
        elif self.state.status == Status.RUNNING:
            self.state.phase = Phase.DECIDE
        self.save()
        # Commit the whole input batch and its resulting state before acknowledging
        # any member. A crash cannot lose the remaining drained input or cancellation.
        for item in items:
            self.emit("input_applied", {"input_id": item.id, "kind": item.kind})
        return True

    def _inputs_pending(self) -> bool:
        return bool(self.queued_inputs) or (self.cancel_input is not None and not self.cancel_applied)

    def _stream_progress(self, data):
        if perf_counter() - self._last_delta >= 0.5:
            self._last_delta = perf_counter()
            self.emit("model_output", data)

    def human(self, command: str, **kwargs) -> None:
        if self.pending_action_id:
            raise ValueError("Resolve the interrupted action with /resolve before continuing")
        self.controller.human(self.state, self.history, command, **kwargs)
        if command == "resume":
            self.cancel_input = None
            self.cancel_applied = False
            self._cancel_requested = False
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
        if self.pending_action_id in self.pending_action_ids:
            self.pending_action_ids.remove(self.pending_action_id)
        self.pending_action_id = next(iter(self.pending_action_ids), None)
        if self.pending_action_id:
            self.state.pending_question = "还有结果未知的动作：" + ", ".join(self.pending_action_ids) + "。请逐项 /resolve。"
            self.save()
            return
        if self.cancel_input is not None:
            # Resolving an effect is not permission to resume a cancelled run.
            self.state.status = Status.STOPPED
            self.state.stop_reason = "user_interrupted"
            self.state.pending_question = None
            self.save()
            return
        if self.state.status == Status.STOPPED:
            self.state.status = Status.WAITING_USER
            self.state.pending_question = "已核查未知动作；继续检查实际环境。"
        self.controller.human(self.state, self.history, "answer", text=text)
        self.save()

    async def run(self) -> RunState:
        if self.metrics.attribution_coverage == "unmeasured":
            self.metrics.attribution_coverage = "partial_history"
        with metrics_scope(self.metrics):
            return await self._drive()

    async def _drive(self) -> RunState:
        if self._running:
            raise ValueError("This Runtime already has an active driver")
        self._running = True
        self._cancel_requested = self.cancel_input is not None or any(i.kind == "cancel" for i in self.queued_inputs)
        started = perf_counter()
        self._driver_started = started
        try:
            policy = self.executor.permissions
            self.emit("permissions", {"fingerprint": policy_fingerprint(policy), "policy": policy.model_dump(),
                                      "read_concurrency": self.read_concurrency})
            for name in self.registry.names():
                backend = getattr(self.registry.get(name), "backend", None)
                if backend:
                    await backend.prepare()
                    self.emit("isolation", {"backend": "docker", "root": backend.relative_root,
                                            "image_id": backend.image_id, "network": "none"})
            return await self._run_impl()
        except asyncio.CancelledError:
            if self.state.status == Status.RUNNING:
                self.controller.human(self.state, self.history, "interrupt")
            raise
        except Exception:
            self.state.status = Status.FAILED
            self.state.stop_reason = "runtime_error"
            raise
        finally:
            self._running = False
            self.metrics.active_seconds += perf_counter() - started
            self._driver_started = None
            kind = {Status.COMPLETED: "completed", Status.WAITING_USER: "paused",
                    Status.STOPPED: "cancelled" if self.state.stop_reason == "user_interrupted" else "stopped",
                    Status.FAILED: "failed"}.get(self.state.status, "paused")
            self.emit(kind, {"reason": self.state.stop_reason or self.state.status.value,
                             "pending_action_ids": self.pending_action_ids})
            self.save()
            if self.store:
                from kong.reporting import run_report
                with metrics_scope(None):  # Derived report validation is outside driver attribution.
                    self.store.save_report(self.state.run_id, run_report(self))

    async def _generate(self, context):
        started = perf_counter()
        self.metrics.model_calls += 1
        try:
            self._last_delta = 0
            if hasattr(self.model, "on_stream"):
                self.model.on_stream = self._stream_progress
            self._model_task = asyncio.create_task(self.model.generate(context))
            self.emit("model_waiting", {"reason": "waiting_for_validated_model_decision"})
            return await self._model_task
        finally:
            self._model_task = None
            self.emit("model_finished", {})
            self.metrics.model_seconds += perf_counter() - started
            usage = getattr(self.model, "last_usage", {})
            clean = {k:v for k,v in usage.items() if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
                     and type(v) is int and v >= 0} if isinstance(usage, dict) else {}
            if "total_tokens" in clean:
                self.metrics.usage_calls += 1
            for key, value in clean.items():
                self.metrics.tokens[key] = self.metrics.tokens.get(key, 0) + value

    async def _run_impl(self) -> RunState:
        self._apply_inputs()  # A persisted priority cancel also applies before a restored paused driver.
        if self.pending_action_id and self.state.status == Status.RUNNING:
            self.state.status = Status.WAITING_USER
            self.state.pending_question = "仍有结果未知的动作；请先 /resolve。"
        if self._record_initial_goal and self.state.status == Status.RUNNING:
            self.history.append("human", self.state.turn_count, command="goal", text=self.state.goal)
            self._record_initial_goal = False
        self.save()
        retries = sum(e.kind == "model_retry" for e in self.run_events)
        try:
            while self.state.status == Status.RUNNING:
                self._apply_inputs()
                if not self.controller.start_turn(self.state):
                    break
                # Save the consumed turn before the request, including failed HTTP requests.
                self.save()
                try:
                    skill_tool = next((self.registry.get(n) for n in self.registry.names()
                                       if isinstance(self.registry.get(n), SkillTool)), None)
                    self.context_engine.supplemental_items = (skill_tool.catalog.context_items(self.history, self.state.run_id)
                                                              if skill_tool else [])
                    if self.constraints or self.state.requires_replan or self.executor.permissions != PermissionPolicy():
                        self.context_engine.supplemental_items.append(ContextItem(category="current_user", priority=0,
                            content=json.dumps({"active_constraints": self.constraints,
                            "permission_policy": self.executor.permissions.model_dump(),
                            "input_rule": "Newest goal/constraints override stale decisions; use fresh evidence for a changed goal."},
                            ensure_ascii=False), sources=[ContextSource(kind="state", run_id=self.state.run_id)]))
                    from kong.runtime.finalization import completion_guidance
                    with measure("validation_seconds"):
                        guidance = completion_guidance(self.controller.completion_gate, self.state, self.history)
                    if guidance:
                        self.context_engine.supplemental_items.append(ContextItem(category="runtime_feedback", priority=0,
                            content="RUNTIME VERIFICATION STATUS:\n" + json.dumps(guidance, ensure_ascii=False, separators=(",", ":")),
                            sources=[ContextSource(kind="state", run_id=self.state.run_id, path="completion_contract")]))
                    if not guidance or guidance["additional_tool_required"]:
                        from kong.runtime.recovery_feedback import patch_failure_guidance
                        with measure("validation_seconds"):
                            patch_feedback = patch_failure_guidance(self.state, self.history, self.workspace)
                        if patch_feedback:
                            self.context_engine.supplemental_items.append(ContextItem(category="runtime_feedback", priority=0,
                                content="PATCH FAILURE STATUS:\n" + json.dumps(patch_feedback, ensure_ascii=False, separators=(",", ":")),
                                sources=[ContextSource(kind="observation", run_id=self.state.run_id, action_id=patch_feedback["failed_observation"])]))
                    with measure("context_build_seconds"):
                        context = await self.context_engine.prepare(thread=self.thread, state=self.state,
                                                                    history=self.history, tools=self.registry.definitions())
                    if self._apply_inputs():
                        continue
                    decision = await self._generate(context)
                except ModelError as exc:
                    previous = next((e for e in reversed(self.history.events)
                                     if e.type in {"decision", "model_error"}), None)
                    self.history.append("model_error", self.state.turn_count, error=str(exc),
                                        diagnostics=getattr(exc, "diagnostics", []), category=exc.category,
                                        retryable=exc.retryable, status_code=exc.status_code,
                                        fatal_configuration=exc.fatal_configuration)
                    self.emit("model_error", {"category": exc.category, "retryable": exc.retryable,
                                              "status_code": exc.status_code, "error": str(exc)})
                    if self._apply_inputs():
                        continue
                    if isinstance(exc, ModelProtocolError) and (previous is None or previous.type != "model_error"):
                        from kong.models.diagnostics import repair_feedback
                        self.history.append("feedback", self.state.turn_count,
                            protocol_rejected=repair_feedback(exc.diagnostics),
                            instruction="No action executed. Correct the JSON format at the listed paths on the next budgeted turn.")
                        self.emit("transition", {"turn":self.state.turn_count, "reason":"protocol_repair", "control":"continue"})
                        self.save()
                        continue
                    if (not isinstance(exc, ModelProtocolError) and exc.retryable and retries < self.model_retry_limit
                            and self.state.turn_count < self.state.hard_turn_limit):
                        retries += 1
                        delay = max(0.1, min(5, exc.retry_after or 0.25 * 2 ** (retries - 1)))
                        self.emit("model_retry", {"category": exc.category, "attempt": retries,
                                                  "limit": self.model_retry_limit, "delay_seconds": delay})
                        try:
                            with measure("retry_wait_seconds"):
                                await asyncio.wait_for(self._input_signal.wait(), timeout=delay)
                        except TimeoutError:
                            pass
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
                if self._apply_inputs():
                    self.emit("decision_discarded", {"reason": "new_user_input"})
                    continue
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
                            if self._apply_inputs():
                                self.emit("batch_revoked", {"reason": "new_user_input"})
                                break
                            skill_id = f"context{1 + self.metrics.tool_calls}"
                            tool_wall_start = perf_counter()
                            try:
                                with measure("tool_wait_seconds"):
                                    result = await self._execute_timed(skill_id, action)
                            finally:
                                self.metrics.tool_wall_seconds += perf_counter() - tool_wall_start
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
                        revoked = await self._actions(decision)
                        if revoked:
                            transition = Transition(control="pause" if self.state.status == Status.WAITING_USER else
                                "stop" if self.state.status == Status.STOPPED else "continue", reason="batch_revoked")
                        else:
                            transition = self.controller.transition(self.state, decision, self.history)
                    else:
                        transition = self.controller.transition(self.state, decision, self.history)
                self._record_verification_checkpoint()
                self.history.append("feedback", self.state.turn_count, transition=transition.model_dump())
                self.emit("transition", {"turn": self.state.turn_count, **transition.model_dump()})
                self.save()
                await self.context_engine.commit(state=self.state, history=self.history)
        except asyncio.CancelledError:
            self._apply_inputs()
            if self.state.status == Status.RUNNING:
                self.controller.human(self.state, self.history, "interrupt")
            self.save()
            await self.context_engine.commit(state=self.state, history=self.history)
            if not self._cancel_requested:
                raise
        self.save()
        await self.context_engine.commit(state=self.state, history=self.history)
        return self.state

    def _record_verification_checkpoint(self):
        if self._driver_started is None or self.state.pending_approval or self.state.requires_replan:
            return
        if self.state.mode == Mode.PLAN and (not self.state.plan or self.state.plan.approved_revision != self.state.plan.revision):
            return
        from kong.runtime.verification_reporting import checkpoint_values, verification_epoch
        last = next((e.payload["decision"] for e in reversed(self.history.events)
                     if e.type == "decision" and e.payload.get("decision", {}).get("kind") == "respond"), {})
        with measure("validation_seconds"):
            criteria = self.controller.completion_gate.criteria_results(self.state, self.history, last.get("criteria_evidence", {}))
        if not criteria:
            return
        values = {**checkpoint_values(criteria), "status": self.state.status.value,
                  "scope_event_index": verification_epoch(self.history)}
        previous = next((e.payload["verification_checkpoint"] for e in reversed(self.history.events)
                         if "verification_checkpoint" in e.payload), {})
        if all(previous.get(key) == value for key, value in values.items()):
            return
        self.history.append("feedback", self.state.turn_count, verification_checkpoint={**values,
            "turn": self.state.turn_count, "model_calls": self.metrics.model_calls, "tool_calls": self.metrics.tool_calls,
            "active_seconds": self.metrics.active_seconds + perf_counter() - self._driver_started})

    async def _actions(self, decision: Act | NeedDiscovery) -> bool:
        with metrics_scope(self.metrics):
            return await self._action_batch(decision)

    async def _action_batch(self, decision: Act | NeedDiscovery) -> bool:
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
        index = 0
        while index < len(decision.actions):
            if self._apply_inputs():
                self.emit("batch_revoked", {"remaining": len(decision.actions) - index, "reason": "new_user_input"})
                return True
            group = [decision.actions[index]]
            if self._parallel_read(group[0]):
                while index + len(group) < len(decision.actions) and len(group) < self.read_concurrency:
                    candidate = decision.actions[index + len(group)]
                    if not self._parallel_read(candidate) or candidate in group:
                        break
                    group.append(candidate)
            work = []
            for action in group:
                # No await between the safe input boundary and intent allocation.
                # A callback may submit input, so check before allocating every member.
                if self._inputs_pending():
                    break
                action_id = f"a{1 + sum(event.type == 'action_intent' for event in self.history.events)}"
                self.pending_action_ids.append(action_id)
                self.pending_action_id = self.pending_action_ids[0]
                self.history.append("action_intent", self.state.turn_count, action_id=action_id, action=action.model_dump())
                versions = capture_versions(self.workspace, self.state, action)
                self.emit("action_started", {"id": action_id, "tool": action.name,
                                            "operation": action.arguments.get("operation", "run")})
                work.append((action_id, action, versions))
            if self._inputs_pending():
                # Callbacks can request cancel after intent allocation but before
                # executor dispatch. These effects are known not to have started.
                for action_id, action, versions in work:
                    from kong.tools.base import ToolResult
                    self._record_result(action_id, action, versions,
                        ToolResult(success=False, error="Action not started: new user input at dispatch boundary."),
                        dispatched=False)
                self._apply_inputs()
                self.emit("batch_revoked", {"remaining": len(decision.actions) - index,
                                            "reason": "new_user_input_before_dispatch"})
                return True
            tool_wall_start = perf_counter()
            tasks = [asyncio.create_task(self._execute_timed(action_id, action)) for action_id, action, _ in work]
            if len(work) == 1 and work[0][1].name in {"process_exec", "terminal", "mcp", "browser"}:
                self._active_cancellable_task = tasks[0]
            gathering = asyncio.gather(*tasks)
            cancelled = False
            try:
                with measure("tool_wait_seconds"):
                    results = await asyncio.shield(gathering) if len(group) > 1 else await gathering
            except asyncio.CancelledError:
                if len(group) == 1:
                    raise  # The saved intent remains unknown; never replay automatically.
                cancelled = True
                with measure("tool_wait_seconds"):
                    results = await gathering  # Drain every started read; no results are dropped.
            finally:
                self._active_cancellable_task = None
                self.metrics.tool_wall_seconds += perf_counter() - tool_wall_start
            # Persist in intent order even when completion order differs.
            for (action_id, action, versions), result in zip(work, results):
                self._record_result(action_id, action, versions, result)
            index += len(work)
            if cancelled:
                raise asyncio.CancelledError()
            if self._apply_inputs():
                self.emit("batch_revoked", {"remaining": len(decision.actions) - index, "reason": "new_user_input"})
                return True
            if self.pending_action_ids:
                self.state.status = Status.WAITING_USER
                self.state.pending_question = "动作结果未知，请检查环境后逐项 /resolve；不会自动重试。"
                self.emit("action_unknown", {"ids": self.pending_action_ids})
                return True
            if any(not r.success for r in results) or ProgressGate.trigger(self.state) in {"consecutive_failures", "repeated_actions"}:
                break
        return False

    def _parallel_read(self, action):
        return (action.name in {"read_file", "list_dir", "search_files"} and action.name in self.registry.names()
                and self.registry.get(action.name).read_only and self.registry.get(action.name).parallel_safe)

    async def _execute_timed(self, action_id, action):
        operation = action.arguments.get("operation", "run")
        timing = ToolTiming(action_id=action_id, tool=action.name,
                            operation=operation if isinstance(operation, str) else "invalid")
        self.metrics.tool_calls += 1
        self.metrics.tool_timings.append(timing)
        self.save()  # A crash during execution leaves an explicitly unknown duration.
        started = perf_counter()
        try:
            result = await self.executor.execute(ToolCall(name=action.name, arguments=action.arguments))
            timing.success = result.success
            timing.outcome_unknown = result.outcome_unknown
            return result
        except asyncio.CancelledError:
            timing.cancelled = True
            timing.outcome_unknown = (action.name not in self.registry.names()
                                      or not self.registry.get(action.name).read_only)
            raise
        finally:
            timing.elapsed_seconds = perf_counter() - started
            self.metrics.tool_seconds += timing.elapsed_seconds

    def _record_result(self, action_id, action, versions_before, result, *, dispatched=True):
        progress_output = (self.registry.get(action.name).progress_output(result.output)
                           if action.name in self.registry.names() else result.output)
        observation = Observation(action_id=action_id, turn=self.state.turn_count, action=action,
            success=result.success, output=result.output, error=result.error, outcome_unknown=result.outcome_unknown,
            dispatched=dispatched,
            fingerprint=digest(action.model_dump()),
            output_hash=digest({"action": action.model_dump(), "output": progress_output}),
            evidence_version=2, artifact_versions=capture_versions(self.workspace, self.state, action),
            artifact_versions_before=versions_before, effect=action_effect(action, self.registry))
        self.history.observations.append(observation)
        self.history.append("observation", self.state.turn_count, observation=observation.model_dump(mode="json"))
        if not result.outcome_unknown:
            self.pending_action_ids.remove(action_id)
        self.pending_action_id = next(iter(self.pending_action_ids), None)
        ProgressGate.observe(self.state, self.history)
        timing = next((t for t in reversed(self.metrics.tool_timings) if t.action_id == action_id), None)
        self.emit("action_finished", {"id": action_id, "tool": action.name, "success": result.success,
                                      "outcome_unknown": result.outcome_unknown,
                                      "elapsed_seconds": timing.elapsed_seconds if timing else None})
        self.emit("observation", {"id": action_id, "tool": action.name, "success": result.success})


async def run_agent(user_input: str, model: Model, registry: ToolRegistry,
                    workspace: Workspace, mode: Mode = Mode.AUTO) -> RunState:
    return await Runtime.new(user_input, model, workspace, registry, mode).run()
