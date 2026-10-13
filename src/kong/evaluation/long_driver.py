"""Event-driven, task-budgeted development harness; Runtime remains authoritative."""
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys
import tempfile
import time
from uuid import uuid4

import httpx

from kong.completion_contracts import ArtifactRequirement, CommandRequirement, CompletionContract
from kong.continuity.contracts import ContextBudget, ContextItem, ContextSource
from kong.continuity.engine import ContextEngine
from kong.contracts import Status
from kong.environments.docker import DockerBackend
from kong.models.compatible import CompatibleModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore, atomic_replace
from kong.tools.defaults import default_registry
from kong.tools.permissions import PermissionPolicy
from kong.workspace import Workspace

from .long_graders import digest_file, grade
from .scenarios import DEVELOPMENT_SCENARIOS


PROTOCOL = "kong-long-eval-10"
HANDOFF_QUESTION = "Stage files checked. Please continue this task in the next Run."


class TaskLimit(RuntimeError):
    pass


def _write_json(path, data):
    """Durable evaluator metadata; retry file replacement, never an Agent write."""
    descriptor, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        atomic_replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class TaskLedger:
    """Never reset on process reconstruction or a new Run; unknown usage stays unknown."""
    def __init__(self, budget, *, live=True):
        self.budget = budget
        self.live = live
        self.runs = self.requests = self.decisions = self.known_tokens = self.estimated_tokens = 0
        self.usage_complete = True
        self.active_seconds = 0.0
        self.calls = []
        self.unknown_token_upper = 0
        self.pending_upper = 0
        self._active_started = None

    @property
    def elapsed(self):
        return self.active_seconds + (time.perf_counter() - self._active_started if self._active_started is not None else 0)

    def begin_run(self):
        if self.runs >= self.budget.runs:
            raise TaskLimit("task_run_budget")
        self.runs += 1

    def before_request(self, context, output_limit):
        if self.decisions >= self.budget.decisions:
            raise TaskLimit("task_decision_budget")
        if self.requests >= self.budget.requests:
            raise TaskLimit("task_request_budget")
        if self.elapsed >= self.budget.active_seconds:
            raise TaskLimit("task_time_budget")
        upper = context.estimated_input_tokens + output_limit
        if self.live and self.known_tokens + self.unknown_token_upper + upper > self.budget.total_tokens:
            raise TaskLimit("task_conservative_token_budget")
        self.requests += 1
        self.estimated_tokens += upper
        self.pending_upper = upper

    def record_usage(self, usage):
        value = usage.get("total_tokens") if isinstance(usage, dict) else None
        if type(value) is int and value >= 0:
            self.known_tokens += value
        else:
            self.usage_complete = False
            self.unknown_token_upper += self.pending_upper
        self.pending_upper = 0
        if self.live and self.known_tokens > self.budget.total_tokens:
            raise TaskLimit("task_actual_token_budget")

    def report(self):
        return {"limits": asdict(self.budget), "runs": self.runs, "requests": self.requests,
                "decisions": self.decisions, "actual_total_tokens": self.known_tokens if self.usage_complete and self.requests else None,
                "observed_partial_total_tokens": self.known_tokens, "usage_complete": self.usage_complete if self.requests else None,
                "estimated_request_token_upper": self.estimated_tokens, "actual_cost": None,
                "token_cap_applicable": self.live, "unknown_request_token_upper": self.unknown_token_upper,
                "active_seconds": round(self.elapsed, 6), "calls": self.calls}


def packet_diagnostic(packet, *, version, prepare_seconds):
    selected = [{"category": i.category, "priority": i.priority,
                 "sha256": hashlib.sha256(i.content.encode()).hexdigest(),
                 "bytes": len(i.content.encode()), "sources": [s.model_dump(mode="json") for s in i.sources]}
                for i in packet.items]
    return {"schema": "kong-context-diagnostic-1", "run_id": packet.run_id, "turn": packet.turn,
            "requirement_revision": version, "prepare_seconds": prepare_seconds,
            "estimated_input_tokens": packet.estimated_input_tokens, "category_tokens": packet.category_tokens,
            "budget": packet.budget.model_dump(), "selected": selected, "dropped": packet.dropped,
            "truncated_event_indices": packet.truncated_event_indices, "omitted_event_indices": packet.omitted_event_indices,
            "selection_notes": packet.selection_notes,
            "request_sha256": hashlib.sha256(json.dumps(packet.messages, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            "injected_memory_ids": [r["id"] for r in packet.recalled_memories],
            "injected_episode_ids": [r["id"] for r in packet.episodes],
            "candidate_retrieval_recall": None}  # requires external source labels


class ObservedEngine(ContextEngine):
    """Only measure prepare(); delegate selection and compaction unchanged to RC5."""
    async def prepare(self, **kwargs):
        start = time.perf_counter()
        state = kwargs["state"]
        contract = state.completion_contract
        self.supplemental_items = [i for i in self.supplemental_items if not any(s.path == "stage_handoff" for s in i.sources)]
        if contract and not contract.allow_completion:
            workspace = Workspace(Path(kwargs["thread"].workspace))
            restore_paths = getattr(self, "restore_artifact_paths", ())
            restore_floor = getattr(self, "restore_observation_floor", None)
            restore_pending = (restore_floor is not None and not restored_read_evidence(
                state, kwargs["history"], workspace, restore_paths, restore_floor))
            if restore_pending:
                # A caller challenge is additional to the unchanged Runtime contract.
                # Keep the original status and provenance, but do not imply ask is next.
                for item in self.supplemental_items:
                    if item.content.startswith("RUNTIME VERIFICATION STATUS:"):
                        prefix, body = item.content.split("\n", 1)
                        status = json.loads(body)
                        status["caller_post_restore_read_required"] = True
                        status["caller_post_restore_read_candidates"] = list(restore_paths)
                        status["instruction"] += (" Before the handoff ask, explicitly read one "
                            "current artifact in this restored process using read_file: " + json.dumps(restore_paths) +
                            ". Prior-process reads do not satisfy this caller challenge. Existing Runtime phase, "
                            "permissions and budgets still apply; no read or action is performed for you.")
                        item.content = prefix + "\n" + json.dumps(status, ensure_ascii=False)
                        item.sources.append(ContextSource(kind="state", run_id=state.run_id, path="post_restore_read"))
            guidance = stage_guidance(state, kwargs["history"], workspace)
            guidance["post_restore_read_required"] = bool(restore_pending)
            guidance["post_restore_read_candidates"] = list(restore_paths) if restore_pending else []
            self.supplemental_items.append(ContextItem(category="runtime_feedback", priority=0,
                content="PUBLIC STAGE HANDOFF STATUS:\n" + json.dumps(guidance, ensure_ascii=False),
                sources=[ContextSource(kind="state", run_id=state.run_id, path="stage_handoff")]))
        packet = await super().prepare(**kwargs)
        self.last_prepare_seconds = time.perf_counter() - start
        return packet


def restored_read_evidence(state, history, workspace, paths, observation_floor):
    """Require a version-bound artifact read after this process restored the Run."""
    from kong.runtime.completion import read_evidence
    current = history.model_copy(update={"observations": history.observations[observation_floor:]})
    return any(read_evidence(path, state, current, workspace) for path in paths)


def stage_guidance(state, history, workspace):
    """Expose existing caller requirements; never dispatch or bind a model action."""
    from kong.runtime.finalization import handoff_read_prerequisites
    from kong.runtime.progress import ProgressGate
    contract = state.completion_contract
    assessment = None
    if state.phase.value == "assess":
        # can_renew synchronizes scope; evaluate a copy so feedback grants no credit.
        verified_progress = ProgressGate.can_renew(state.model_copy(deep=True))
        remaining = max(0, state.hard_turn_limit - state.turn_count)
        assessment = {"verified_progress_available": verified_progress,
            "bounded_recovery_available": not verified_progress and state.progress_state.recovery_attempts < 1 and remaining > 0,
            "remaining_hard_turns": remaining, "continue_without_verified_progress_pauses": True}
    return {"completion_allowed": False, "required_question": contract.required_question,
        **handoff_read_prerequisites(state, history, workspace), "phase": state.phase.value, "assessment_options": assessment,
        "instruction": ("At an assess checkpoint, provide an honest assess decision before requesting more tools. "
            "next_strategy=continue requires verified progress; otherwise it pauses. When bounded_recovery_available "
            "is true, recover or discover can request the existing at-most-two-turn recovery window. This does not "
            "grant progress, reset a hard budget, or automatically choose recovery. "
            "Perform the authorized stage task and produce absent outputs before reading them. Missing outputs are "
            "your task work, not missing caller input; do not repeatedly try to read them before creation. Read current "
            "existing prerequisites within permission scope. When all prerequisites hold, use kind=ask "
            "with question exactly equal to required_question, without a prefix or explanation. A read instruction "
            "is not a question. Respond cannot finish an intermediate stage. This guidance grants no approval, "
            "permission or budget and never performs an implicit tool action.")}


class ObservedModel:
    def __init__(self, inner, ledger, driver, output_limit=4096):
        self.inner, self.ledger, self.driver = inner, ledger, driver
        self.output_limit = output_limit
        self._last_usage = getattr(inner, "last_usage", None)

    @property
    def last_usage(self):
        return self._last_usage

    @property
    def on_stream(self):
        return getattr(self.inner, "on_stream", None)

    @on_stream.setter
    def on_stream(self, callback):
        if hasattr(self.inner, "on_stream"):
            self.inner.on_stream = callback

    def take_memory_delta(self):
        provider = getattr(self.inner, "take_memory_delta", None)
        return provider() if provider else None

    async def generate(self, context):
        runtime = self.driver.runtime
        self._last_usage = None  # A discarded synthetic decision has no provider usage.
        # Derived episodes exist only after RC5 commit. Submit at a safe input boundary,
        # return no decision from the obsolete context: Runtime discards it normally.
        self.driver.observe_compaction()
        if runtime.queued_inputs:
            from kong.contracts import NeedUserInput
            return NeedUserInput(question="Apply pending scenario revision before the next decision.")
        try:
            self.ledger.before_request(context, self.output_limit)
        except TaskLimit as exc:
            self.driver.limit_reason = str(exc)
            raise
        diag = packet_diagnostic(context, version=self.driver.revision,
                                 prepare_seconds=runtime.context_engine.last_prepare_seconds)
        diag["requirement_sources"] = []
        rendered = "\n".join(m["content"] for m in context.messages)
        for intervention in self.driver.interventions:
            if intervention["input_id"] not in self.driver.applied:
                continue
            origin = self.driver.find_source(intervention)
            diag["requirement_sources"].append({"revision_id": intervention["id"], "source": origin,
                "raw_source_exists": origin is not None, "exact_text_in_final_request": intervention["text"] in rendered})
        self.driver.contexts.append(diag)
        _write_json(self.driver.report_dir / "contexts.json", self.driver.contexts)
        start = time.perf_counter()
        call = {"run_id": runtime.state.run_id, "turn": runtime.state.turn_count,
                "outcome": "error", "seconds": None, "usage": None}
        if hasattr(self.inner, "last_usage"):
            self.inner.last_usage = None
        try:
            decision = await self.inner.generate(context)
            self.ledger.decisions += 1
            call["outcome"] = "decision"
            return decision
        finally:
            call["seconds"] = time.perf_counter() - start
            call["usage"] = getattr(self.inner, "last_usage", None)
            self._last_usage = call["usage"]
            self.ledger.calls.append(call)
            try:
                self.ledger.record_usage(call["usage"])
            except TaskLimit as exc:
                self.driver.limit_reason = str(exc)
                raise
            _write_json(self.driver.report_dir / "usage.json", self.ledger.report())


class ScenarioDriver:
    def __init__(self, scenario, report_dir):
        self.scenario, self.report_dir = scenario, report_dir
        self.runtime = None
        self.run_number = 0
        self.revision = 0
        self.interventions = []
        self.contexts = []
        self.compactions = {}
        self.applied = set()
        self.restart = None
        self.limit_reason = None
        self.bound_revision = 0
        self.pauses = []

    def find_source(self, intervention):
        runtime = self.runtime
        if intervention["run_id"] == runtime.state.run_id:
            history = runtime.history
        else:
            try:
                history = runtime.store.load(intervention["run_id"]).history
            except (OSError, ValueError):
                return None
        return next((f"{intervention['run_id']}:event:{index}" for index, event in enumerate(history.events)
                     if event.type == "human" and event.payload.get("input_id") == intervention["input_id"]), None)

    def inject(self, entry):
        if any(i["id"] == entry.id for i in self.interventions):
            return
        input_id = self.runtime.submit_input("constraint", entry.text)
        self.interventions.append({**asdict(entry), "input_id": input_id,
                                   "run_id": self.runtime.state.run_id, "turn": self.runtime.state.turn_count})
        _write_json(self.report_dir / "interventions.json", self.interventions)

    def event(self, kind, data):
        if kind == "input_applied":
            self.applied.add(data["input_id"])
            self.revision = len(self.applied)
        if kind != "action_finished" or not data.get("success"):
            return
        observation = next((o for o in self.runtime.history.observations if o.action_id == data["id"]), None)
        if observation is None:
            return
        for entry in self.scenario.interventions:
            if entry.run != self.run_number:
                continue
            if entry.trigger == "first_write" and observation.action.name in {"write_file", "patch_file"}:
                self.inject(entry)
            if entry.trigger == "environment_read" and observation.action.name == "read_file" and observation.action.arguments.get("path") in self.scenario.protected:
                self.inject(entry)

    def observe_compaction(self):
        engine = self.runtime.context_engine
        if not engine.store:
            return
        for episode in engine.store.episodes(self.runtime.thread.thread_id):
            self.compactions[episode.id] = {"id": episode.id, "run_id": episode.run_id,
                "trigger": episode.trigger, "first_event": episode.first_event, "last_event": episode.last_event,
                "sources": [s.model_dump(mode="json") for s in episode.sources]}
        _write_json(self.report_dir / "compactions.json", list(self.compactions.values()))
        # commit() can create an Episode after a decision has paused the Run.
        # Observing that Episode must not bypass submit_input's running-only gate.
        if self.runtime.state.status == Status.RUNNING:
            for entry in self.pending_compaction_interventions():
                self.inject(entry)

    def pending_compaction_interventions(self):
        delivered = {i["id"] for i in self.interventions}
        if not any(e["run_id"] == self.runtime.state.run_id and e["trigger"] != "run_complete"
                   for e in self.compactions.values()):
            return []
        return [entry for entry in self.scenario.interventions
                if entry.run == self.run_number and entry.trigger == "compaction" and entry.id not in delivered]

    def coverage(self):
        nonfinal = [e for e in self.compactions.values() if e["trigger"] != "run_complete"]
        return {"three_runs": self.run_number == 3,
                "three_effective_revisions": len(self.applied) == len(self.scenario.interventions),
                "two_compaction_checkpoints": len(nonfinal) >= 2,
                "same_run_reconstruction": self.restart is not None,
                "process_restart": bool(self.restart and self.restart.get("kind") == "fresh_process"),
                "restored_artifact_read": bool(self.restart and self.restart.get("restored_artifact_read")),
                "token_pressure": any(e["trigger"] == "token_pressure" for e in nonfinal)}


def completion_contract(scenario, number=3):
    # A single receipt covers both entrypoints. Separate arbitrary processes are
    # potential mutations and correctly invalidate one another's RC5 evidence.
    scripts = ["check.py", "test_added.py"] if scenario.family == "code" else ["process.py", "check.py"]
    command = "import runpy;" + ";".join(f"runpy.run_path({name!r}, run_name='__main__')" for name in scripts)
    commands = [CommandRequirement(id="checks", argv=["python", "-c", command], cwd="task", fresh_python=True)]
    artifacts = [ArtifactRequirement(id=p.split("/")[-1], path=p) for p in scenario.artifacts]
    if scenario.family == "data":
        # Public structure is declared without computing the Agent's answer.
        # Script behavior and report numbers are independently graded afterward.
        artifacts = [ArtifactRequirement(id="summary", path="task/summary.csv", format="csv", csv_columns=["region", "net_amount"]),
                     ArtifactRequirement(id="report", path="task/report.md", binary_prefix_hex=b"# Data report\n".hex())]
    if number not in {1, 2, 3}:
        raise ValueError("Unknown stage number")
    return CompletionContract(version=2, family=scenario.family,
        protected_paths=list(scenario.protected),
        artifacts=artifacts if number == 3 else [], commands=commands if number == 3 else [], allow_completion=number == 3,
        read_before_ask=sorted(set(scenario.protected + scenario.artifacts)) if number < 3 else [],
        required_question=HANDOFF_QUESTION if number < 3 else None)


def fatal_model_error(runtime):
    return next((event.payload for event in reversed(runtime.history.events)
                 if event.type == "model_error" and event.payload.get("fatal_configuration")), None)


def terminal_service_error(runtime):
    """A recovered error is not a terminal interruption or a model capability failure."""
    if runtime.state.status != Status.WAITING_USER:
        return None
    latest = next((event for event in reversed(runtime.history.events) if event.type in {"decision", "model_error"}), None)
    if latest and latest.type == "model_error" and latest.payload.get("category") in {
            "timeout", "connection", "rate_limit", "service_http", "configuration", "authentication", "access", "billing"}:
        return latest.payload
    return None


def handoff_problems(runtime):
    """A pause is a handoff only after the explicit caller contract's ask."""
    from kong.contracts import NeedUserInput
    from kong.runtime.completion import ask_problems
    problems = []
    contract = runtime.state.completion_contract
    if runtime.state.status != Status.WAITING_USER:
        problems.append("not_waiting_user")
    if runtime.pending_action_ids or runtime.pending_action_id:
        problems.append("pending_effect")
    if runtime.state.pending_approval:
        problems.append("approval_required")
    if runtime.state.contract_invalidated or runtime.queued_inputs or runtime.cancel_input:
        problems.append("unacknowledged_or_invalidated_input")
    if fatal_model_error(runtime):
        problems.append("fatal_model_configuration")
    from kong.runtime.evidence import fingerprint
    if any(fingerprint(runtime.workspace, p) != version for p, version in runtime.state.contract_protected_versions.items()):
        problems.append("protected_resource_changed")
    if not contract or contract.allow_completion or contract.required_question != HANDOFF_QUESTION:
        problems.append("handoff_contract_missing")
    latest = next((e for e in reversed(runtime.history.events) if e.type in {"decision", "model_error"}), None)
    decision = latest.payload.get("decision", {}) if latest else {}
    if decision.get("kind") != "ask" or decision.get("question") != HANDOFF_QUESTION or runtime.state.pending_question != HANDOFF_QUESTION:
        problems.append("not_declared_handoff_question")
    elif contract:
        problems.extend(ask_problems(runtime.state, NeedUserInput(question=HANDOFF_QUESTION), runtime.history, runtime.workspace))
    return problems


async def drive_stage(runtime, driver, ledger, contract):
    """Resume only acknowledged scenario constraints, using existing host APIs."""
    while True:
        await asyncio.wait_for(runtime.run(), max(0.01, ledger.budget.active_seconds - ledger.elapsed))
        driver.observe_compaction()
        pending = driver.pending_compaction_interventions()
        if pending and not handoff_problems(runtime):
            # Only the declared, verified stage handoff authorizes continuation.
            # Arbitrary questions, approvals, cancellation and uncertain effects
            # remain paused; the caller does not rewrite Runtime control state.
            driver.pauses.append({"run_id": runtime.state.run_id, "turn": runtime.state.turn_count,
                "classification": "post_handoff_compaction_revision", "revisions": [e.id for e in pending]})
            _write_json(driver.report_dir / "pauses.json", driver.pauses)
            runtime.human("answer", text="A recorded compaction triggered the declared scenario correction. Continue this same Run to acknowledge it before handing off; existing permissions and budgets still apply.")
            driver.observe_compaction()
        if runtime.queued_inputs and not runtime.pending_action_ids and not fatal_model_error(runtime):
            known = {i["input_id"] for i in driver.interventions}
            if any(item.kind != "constraint" or item.id not in known for item in runtime.queued_inputs):
                return
            # Runtime.run applies and persists queued input even while paused.
            await asyncio.wait_for(runtime.run(), max(0.01, ledger.budget.active_seconds - ledger.elapsed))
        if not runtime.state.contract_invalidated:
            return
        if (runtime.state.status != Status.WAITING_USER or runtime.pending_action_ids
                or runtime.pending_action_id or runtime.state.pending_approval or runtime.cancel_input
                or fatal_model_error(runtime) or driver.revision <= driver.bound_revision):
            return
        if runtime.state.completion_contract != contract:
            return  # An external caller change cannot be blessed by the evaluator.
        protected = json.loads((driver.report_dir / "initial.json").read_text(encoding="utf-8"))["protected_hashes"]
        if any(digest_file(runtime.workspace.root / p) != expected for p, expected in protected.items()):
            raise TaskLimit("protected_resource_changed_before_rebind")
        runtime.set_completion_contract(contract)
        driver.bound_revision = driver.revision
        driver.pauses.append({"run_id": runtime.state.run_id, "turn": runtime.state.turn_count,
            "classification": "scenario_constraint_rebind", "revision": driver.revision})
        _write_json(driver.report_dir / "pauses.json", driver.pauses)
        runtime.human("answer", text="Caller re-bound the unchanged public stage contract after the acknowledged scenario correction. Continue within the existing permissions and budgets.")


async def wait_restore_process(process, report_dir, timeout_seconds, credential=None):
    """Drain worker pipes and preserve exit diagnostics; never replay an action."""
    stdout, stderr, interruption = b"", b"", None
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_seconds)
    except BaseException as exc:
        interruption = type(exc).__name__
        if process.returncode is None:
            process.kill()
        stdout, stderr = await process.communicate()
        raise
    finally:
        counts = {}
        for name, output in (("stdout", stdout), ("stderr", stderr)):
            original = output
            if credential:
                output = output.replace(credential.encode("utf-8"), b"[redacted]")
            output = re.sub(rb"sk-[A-Za-z0-9_-]{12,}", b"[redacted]", output)
            path = report_dir / ("restart-" + name + ".log")
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_bytes(output)
            atomic_replace(temporary, path)
            counts[name] = {"bytes": len(output), "sha256": digest_file(path), "redacted": output != original}
        _write_json(report_dir / "restart-diagnostics.json", {"schema": "kong-worker-diagnostics-1",
            "pid": process.pid, "returncode": process.returncode, "interruption": interruption,
            "capture_complete": interruption is None, "streams": counts})


async def run_trial(scenario, report_dir, image, model_factory, *, live=False, config=None):
    report_dir.mkdir(parents=True, exist_ok=False)
    folder = report_dir / "agent"
    folder.mkdir()
    for name, content in scenario.files:
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    workspace = Workspace(folder)
    backend = DockerBackend(workspace, "task", image)
    ledger = TaskLedger(scenario.budget, live=live)
    driver = ScenarioDriver(scenario, report_dir)
    protected = {p: digest_file(folder / p) for p in scenario.protected}
    _write_json(report_dir / "initial.json", {"scenario": scenario.manifest(), "protected_hashes": protected})
    registry = default_registry(workspace, allow_process=True, process_backend=backend)
    allowed = ["read_file", "list_dir", "search_files", "write_file", "patch_file", "process_exec", "recall_history"]
    permissions = PermissionPolicy(allowed_tools=allowed, read_roots=["task"], write_roots=["task"])
    store = RunStore(folder / ".kong" / "runs")
    run_records, checks = [], {}
    status, stop_reason, error_completion = "failed", None, False
    grading_seconds = None
    started = time.perf_counter()
    runtime = None
    worker_owned_run = False
    try:
        await backend.prepare()
        _write_json(report_dir / "isolation.json", {"backend": "docker", "image_id": backend.image_id,
            "mount": "task only", "file_policy": permissions.model_dump(),
            "grader_mount_during_agent_execution": False, "host_process_fallback": False})
        thread_id = None
        for number, goal in enumerate(scenario.goals, 1):
            ledger.begin_run()
            driver.run_number = number
            driver.bound_revision = driver.revision
            if any(not (folder / name).is_file() or digest_file(folder / name) != expected for name, expected in protected.items()):
                raise TaskLimit("protected_resource_changed_between_runs")
            if number == 3 and scenario.appended_input:
                name, addition = scenario.appended_input
                # Check before a declared evaluator edit; never bless an agent modification.
                if digest_file(folder / name) != protected[name]:
                    raise TaskLimit("protected_input_changed_before_append")
                with (folder / name).open("a", encoding="utf-8", newline="") as stream:
                    stream.write(addition)
                old = protected[name]
                protected[name] = digest_file(folder / name)
                _write_json(report_dir / "environment_change.json", {"path": name, "before": old, "after": protected[name], "actor": "scenario_driver"})
            if number == 3 and scenario.external_edit:
                name, content = scenario.external_edit
                old = digest_file(folder / name)
                (folder / name).write_text(content, encoding="utf-8")
                _write_json(report_dir / "environment_change.json", {"path": name, "before": old,
                    "after": digest_file(folder / name), "actor": "declared_external_edit", "protected_baseline_changed": False})
            engine = ObservedEngine(budget=ContextBudget(window_tokens=config.context_window_tokens if config else 65536,
                                                        output_reserved_tokens=config.max_tokens if config else 4096))
            inner = model_factory(scenario, number)
            model = ObservedModel(inner, ledger, driver, config.max_tokens if config else 4096)
            contract = completion_contract(scenario, number)
            runtime = Runtime.new(goal, model, workspace, registry, store=store, thread_id=thread_id,
                                  permissions=permissions, context_engine=engine, on_event=driver.event,
                                  progress_paths=[p for p, _ in scenario.files] + list(scenario.artifacts),
                                  completion_contract=contract)
            runtime.state.hard_turn_limit = scenario.budget.decisions // scenario.budget.runs
            # Keep the RC5 lease/checkpoint behavior; only the host hard limit is calibrated.
            driver.runtime = runtime
            thread_id = runtime.thread.thread_id
            ledger._active_started = time.perf_counter()
            worker_owned_run = False
            try:
                await drive_stage(runtime, driver, ledger, contract)
                if number == 1 and not handoff_problems(runtime):
                    runtime.save()
                    state_keys = ("runs", "requests", "decisions", "known_tokens", "estimated_tokens", "unknown_token_upper", "usage_complete", "active_seconds", "calls")
                    saved_ledger = {key: getattr(ledger, key) for key in state_keys}
                    saved_ledger["active_seconds"] = ledger.elapsed
                    request_path = report_dir.resolve() / "restart-request.json"
                    _write_json(request_path, {"scenario": scenario.id, "scenario_manifest": scenario.manifest(), "protocol": PROTOCOL, "run_id": runtime.state.run_id,
                        "workspace": str(folder.resolve()), "live": live,
                        "model": config.model_dump(mode="json") if config else None,
                        "ledger": saved_ledger, "interventions": driver.interventions,
                        "applied": sorted(driver.applied), "compactions": driver.compactions, "contexts": driver.contexts,
                        "bound_revision": driver.bound_revision, "pauses": driver.pauses,
                        "budget": engine.budget.model_dump(), "permissions": permissions.model_dump(), "image": backend.image_id})
                    environment = os.environ.copy()
                    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
                    if config:
                        environment["KONG_LONG_WORKER_API_KEY"] = config.api_key.get_secret_value()
                    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "kong.evaluation.long_worker", str(request_path),
                        env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    worker_owned_run = True
                    # Allow the child's timeout to finish Docker cleanup and persist
                    # pending effects; drain pipes so diagnostics cannot deadlock it.
                    await wait_restore_process(process, report_dir,
                        max(0.01, scenario.budget.active_seconds - ledger.elapsed) + 35,
                        config.api_key.get_secret_value() if config else None)
                    response_path = request_path.with_name("restart-result.json")
                    if not response_path.exists():
                        raise ValueError("Fresh-process worker did not return a durable result")
                    response = json.loads(response_path.read_text(encoding="utf-8"))
                    if response["pid"] == os.getpid() or response["run_id"] != runtime.state.run_id:
                        raise ValueError("Restart identity mismatch")
                    for key in state_keys:
                        if key != "active_seconds":  # Parent span already includes the child wall time.
                            setattr(ledger, key, response["ledger"][key])
                    driver.interventions, driver.contexts = response["interventions"], response["contexts"]
                    driver.applied, driver.compactions = set(response["applied"]), response["compactions"]
                    driver.revision, driver.limit_reason = len(driver.applied), response["limit_reason"]
                    driver.bound_revision, driver.pauses = response["bound_revision"], response["pauses"]
                    runtime = Runtime.restore(store.load(runtime.state.run_id), model, workspace, registry, store=store,
                        permissions=permissions, context_engine=ObservedEngine(budget=engine.budget), on_event=driver.event)
                    driver.runtime = runtime
                    driver.restart = {"run_id": runtime.state.run_id, "load_seconds": response["load_seconds"],
                                      "kind": "fresh_process", "parent_pid": os.getpid(), "worker_pid": response["pid"],
                                      "restored_artifact_read": response["restored_artifact_read"],
                                      "returncode": process.returncode, "diagnostics": "restart-diagnostics.json"}
                    if process.returncode:
                        raise ValueError("Fresh-process worker interrupted; inspect saved state, do not replay")
            finally:
                ledger.active_seconds = ledger.elapsed
                ledger._active_started = None
                if worker_owned_run:
                    # Even a killed worker may have newer pending-effect records.
                    # Never overwrite them with the parent's pre-restart snapshot.
                    runtime = Runtime.restore(store.load(runtime.state.run_id), model, workspace, registry, store=store,
                        permissions=permissions, context_engine=ObservedEngine(budget=engine.budget), on_event=driver.event)
                    driver.runtime = runtime
                else:
                    runtime.save()
                run_records.append({"run_id": runtime.state.run_id, "thread_id": thread_id,
                    "number": number, "status": runtime.state.status.value,
                    "turn_count": runtime.state.turn_count, "stop_reason": runtime.state.stop_reason,
                    "metrics": runtime.metrics.model_dump(mode="json")})
                _write_json(report_dir / "runs.json", run_records)
            if fatal_model_error(runtime):
                status, stop_reason = "environment_error", "fatal_model_configuration"
                break
            service_error = terminal_service_error(runtime)
            if service_error:
                status, stop_reason = "environment_error", "terminal_model_service:" + service_error["category"]
                break
            if driver.limit_reason:
                status, stop_reason = "budget_stop", driver.limit_reason
                break
            if runtime.state.status in {Status.FAILED, Status.STOPPED} or runtime.pending_action_ids:
                stop_reason = runtime.state.stop_reason or "runtime_stopped_or_unknown_effect"
                break
            if number < 3:
                problems = handoff_problems(runtime)
                driver.pauses.append({"run_id": runtime.state.run_id, "turn": runtime.state.turn_count,
                    "classification": "unplanned_pause" if problems else "verified_stage_handoff", "problems": problems})
                _write_json(report_dir / "pauses.json", driver.pauses)
                if problems:
                    stop_reason = "unplanned_stage_end"
                    break
        grading_started = time.perf_counter()
        checks = await grade(scenario, folder, backend.image_id or image, protected, grader_root=report_dir / "external")
        grading_seconds = time.perf_counter() - grading_started
        error_completion = runtime is not None and runtime.state.status == Status.COMPLETED and not all(checks.values())
        coverage = driver.coverage()
        required = ["three_runs", "three_effective_revisions", "two_compaction_checkpoints", "process_restart", "restored_artifact_read"]
        if runtime and runtime.state.status == Status.COMPLETED and all(checks.values()):
            status = "success" if all(coverage[k] for k in required) else "invalid_coverage"
    except TaskLimit as exc:
        status, stop_reason = "budget_stop", str(exc)
        grading_seconds = None
    except TimeoutError:
        status, stop_reason = "timeout", "task_time_budget"
        grading_seconds = None
    except asyncio.CancelledError:
        status, stop_reason = "cancelled", "evaluation_cancelled"
        raise
    except Exception as exc:
        status, stop_reason = "environment_error", type(exc).__name__ + ": " + str(exc)
        grading_seconds = None
    finally:
        # The original artifacts are unchanged by grade; bind every observed file to this result.
        artifacts = {p: digest_file(folder / p) for p in scenario.artifacts if (folder / p).is_file() and not (folder / p).is_symlink()}
        result = {"schema": "kong-long-result-1", "protocol": PROTOCOL, "scenario": scenario.id, "mode": "live" if live else "fixtures",
            "image_id": backend.image_id,
            "status": status, "stopped_reason": stop_reason, "checks": checks, "coverage": driver.coverage(),
            "error_completion": error_completion, "runtime_status": runtime.state.status.value if runtime else None,
            "artifact_hashes": artifacts, "protected_hashes": protected, "runs": run_records,
            "budget_usage": ledger.report(), "grading_seconds": grading_seconds,
            "wall_seconds": time.perf_counter() - started, "human_measured": False,
            "failure_diagnosis": {"artifact_checks": [name for name, passed in checks.items() if not passed],
                                  "acquisition_retrieval_ranking_version_model_use": "requires_labeled_checkpoint_review"},
            "supervision": {"corrections": None, "reminders": None, "rescues": None},
            "synthetic_revisions": len(driver.applied), "restart": driver.restart, "pauses": driver.pauses}
        _write_json(report_dir / "result.json", result)
    return result


def freeze_resources():
    package = Path(__file__).resolve().parents[1]
    resources = {"src/kong/" + p.relative_to(package).as_posix(): digest_file(p)
                 for p in sorted(package.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}
    root = package.parent.parent
    if package.parent.name == "src" and (root / "pyproject.toml").is_file():
        paths = list((root / "tests").glob("test_*.py"))
        paths += [root / n for n in ("pyproject.toml", "uv.lock")]
        paths += list((root / "examples").glob("*.py"))
        paths += [p for p in (root / "examples" / "v05-skills").rglob("*") if p.is_file()]
        resources.update({p.relative_to(root).as_posix(): digest_file(p) for p in sorted(paths) if p.is_file()})
    if "src/kong/evaluation/long_driver.py" not in resources:
        raise ValueError("The executing evaluation package could not be fingerprinted")
    return resources


async def run_long_suite(output, image, *, model_factory=None, config=None, scenarios=DEVELOPMENT_SCENARIOS):
    """Two calibration tasks only. Creating a fresh UUID prevents overwriting prior attempts."""
    if config is not None and model_factory is not None:
        raise ValueError("Select live config or fixed model factory, not both")
    if not scenarios or len({s.id for s in scenarios}) != len(scenarios):
        raise ValueError("Select a nonempty set of distinct scenarios")
    from .long_fixtures import fixture_model
    root = Path(output) / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:12])
    root.mkdir(parents=True, exist_ok=False)
    resources = freeze_resources()
    _write_json(root / "freeze.json", {"schema": "kong-long-freeze-1", "protocol": PROTOCOL, "kind": "development_calibration",
        "resource_scope": "repository" if "uv.lock" in resources else "installed_package",
        "baseline": "9a3e23e621404a5be8f35bc277a02ccb6fcc2f94", "resources": resources,
        "scenarios": [s.manifest() for s in scenarios], "platform": platform.platform(),
        "python": platform.python_version(), "image_requested": image,
        "model": config.model_dump(mode="json") if config else None,
        "formal_thresholds_frozen": False, "order": [s.id for s in scenarios]})
    results = []
    def summary(reason=None):
        return {"schema": "kong-long-summary-1", "mode": "live" if config else "fixtures",
            "planned_runs": len(scenarios), "recorded_runs": len(results),
            "not_attempted": [s.id for s in scenarios[len(results):]],
            "complete": len(results) == len(scenarios), "stopped_reason": reason,
            "results": [{"scenario": r["scenario"], "status": r["status"], "error_completion": r["error_completion"]} for r in results],
            "mechanism_successes": sum(r["status"] == "success" for r in results) if not config else None,
            "model_successes": sum(r["status"] == "success" for r in results) if config else None,
            "all_started_attempts": len(results), "human_measured": False}
    _write_json(root / "summary.json", summary())
    async with httpx.AsyncClient(follow_redirects=False) as client:
        factory = model_factory or ((lambda s, n: CompatibleModel(config, client)) if config else fixture_model)
        selected_image = image
        for scenario in scenarios:
            try:
                result = await run_trial(scenario, root / scenario.id, selected_image, factory, live=config is not None, config=config)
            except asyncio.CancelledError:
                path = root / scenario.id / "result.json"
                if path.exists():
                    results.append(json.loads(path.read_text(encoding="utf-8")))
                _write_json(root / "summary.json", summary("evaluation_cancelled"))
                raise
            results.append(result)
            if result["image_id"]:
                selected_image = result["image_id"]  # same immutable image for the rest of the suite
                _write_json(root / "image-identity.json", {"requested": image, "resolved": selected_image})
            stop = result["status"] == "environment_error"
            _write_json(root / "summary.json", summary(result["stopped_reason"] if stop else None))
            if stop:
                break
    end_resources = freeze_resources()
    _write_json(root / "resources-end.json", end_resources)
    final = summary(next((r["stopped_reason"] for r in results if r["status"] == "environment_error"), None))
    final["resource_identity_stable"] = resources == end_resources
    final["image_identity_stable"] = len({r["image_id"] for r in results if r["image_id"]}) <= 1
    final["completion_events"] = sum(r["runtime_status"] == "completed" for r in results)
    final["error_completion_tasks"] = sum(r["error_completion"] for r in results)
    final["error_completion_event_rate"] = final["error_completion_tasks"] / final["completion_events"] if final["completion_events"] else None
    final["error_completion_task_rate"] = final["error_completion_tasks"] / len(results) if results else None
    _write_json(root / "summary.json", final)
    return root, json.loads((root / "summary.json").read_text(encoding="utf-8"))
