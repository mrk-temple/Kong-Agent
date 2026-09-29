"""Context compilation and derived memory lifecycle. No additional model calls."""
import json
import sqlite3

from kong.context import ContextBuilder
from kong.continuity.contracts import (
    ContextBudget, ContextItem, ContextPacket, ContextSource, Thread, WorkingSet,
)
from kong.contracts import History, RunState
from kong.tools.base import ToolDefinition
from kong.continuity.compiler import BudgetManager, ContextPlanner, ContextRenderer, item, memory_protocol
from kong.continuity.compaction import Compactor, observation_digests
from kong.continuity.memory import ExplicitExtractor, PromotionPolicy, ThreadMemoryManager, source_for, stable_id
from kong.continuity.memory_contracts import ContinuityPolicy, MemoryDelta, MemoryScope, MemoryStatus, RankingConfig
from kong.continuity.memory_store import MemoryStore, lexical_terms
from kong.continuity.retrieval import MemoryRetriever, SQLiteMemoryRetriever
from kong.continuity.history import HistorySelector
from kong.continuity.contracts import RecentContextPolicy


class WorkingSetBuilder:
    def build(self, state: RunState, history: History) -> WorkingSet:
        step = next((s for s in state.plan.steps if s.id == state.current_plan_step), None) if state.plan else None
        items = [ContextItem(category="goal", priority=0, content=state.goal,
                             sources=[ContextSource(kind="state", run_id=state.run_id, path="goal")])]
        for name in ("current_plan_step", "pending_approval", "pending_question", "success_criteria"):
            value = state.model_dump(mode="json")[name]
            if value:
                items.append(ContextItem(category=name, priority=0 if name != "success_criteria" else 1,
                                         content=json.dumps(value, ensure_ascii=False), sources=[ContextSource(
                                             kind="state", run_id=state.run_id, path=name)]))
        for index in range(len(history.events) - 1, -1, -1):
            event = history.events[index]
            if event.type == "human":
                items.append(ContextItem(category="latest_human_event", priority=0,
                                         content=event.model_dump_json(), sources=[ContextSource(
                                             kind="event", run_id=state.run_id, event_index=index)]))
                break
        return WorkingSet(run_id=state.run_id, goal=state.goal,
                          current_plan_step=step.model_copy(deep=True) if step else None,
                          pending_approval=state.pending_approval.model_copy(deep=True) if state.pending_approval else None,
                          pending_question=state.pending_question, items=items,
                          source_event_count=len(history.events),
                          current_focus=(history.observations[-1].action.name + " " + json.dumps({
                              key: str(value)[:160] for key, value in history.observations[-1].action.arguments.items()
                              if key != "content"}, ensure_ascii=False)[:500] if history.observations else None),
                          progress_summary=f"{sum(o.success for o in history.observations)} successful observations; {state.turn_count} turns consumed",
                          active_constraints=[c.description for c in state.success_criteria],
                          important_observation_ids=[o.action_id for o in history.observations if o.success][-3:],
                          unresolved_items=[state.pending_question] if state.pending_question else [],
                          recent_failures=[f"{o.action_id}: {o.error}" for o in history.observations if not o.success][-3:])


class ContextEngine:
    """Compile bounded context and merge source-grounded deltas. An explicitly
    supplied legacy builder retains the V0.1 rendering compatibility path.
    """
    def __init__(self, builder: ContextBuilder | None = None, budget: ContextBudget | None = None, *,
                 policy: ContinuityPolicy | None = None, ranking: RankingConfig | None = None,
                 retriever: MemoryRetriever | None = None):
        self.legacy = builder is not None
        self.builder = builder or ContextBuilder()
        self.budget = budget or ContextBudget()
        self.working_set_builder = WorkingSetBuilder()
        self.working_set: WorkingSet | None = None
        self.last_packet: ContextPacket | None = None
        self.history_selector = None
        self.store = None
        self.runs = None
        self.long_term_store = None
        self.thread = None
        self.project_id = None
        self.policy = policy or ContinuityPolicy()
        self.ranking = ranking or RankingConfig()
        self.retriever = retriever
        self.extractor = ExplicitExtractor()
        self.planner = ContextPlanner()
        self.renderer = ContextRenderer()
        self.budget_manager = BudgetManager(self.renderer)
        self.diagnostics = []
        self.supplemental_items = []

    def audit(self, run_id, kind, **data):
        self.diagnostics.append({"run_id": run_id, "kind": kind, **data})
        if self.store:
            try:
                self.store.audit(run_id, kind, **data)
            except (OSError, sqlite3.Error):
                pass  # Keep an in-memory diagnostic if the derived DB is unavailable.

    def bind(self, thread, runs, *, long_term_store=None, project_id=None, thread_store=None, run_id=None):
        self.thread, self.runs = thread, runs
        self.project_id = project_id
        if runs:
            self.store = MemoryStore(thread_store.path if thread_store else runs.directory.parent / "context.db")
            self.long_term_store = long_term_store or self.store
            self.history_selector = HistorySelector(runs, self.store, RecentContextPolicy(
                max_messages=self.policy.recent_messages, max_chars=self.policy.recent_chars))
            self.manager = ThreadMemoryManager(self.store, runs)
            self.compactor = Compactor(self.store, self.policy)
            self.retriever = self.retriever or SQLiteMemoryRetriever(list({s.path: s for s in [self.store, self.long_term_store]}.values()), self.ranking)
            self.last_packet = self.store.packet(run_id) if run_id else None

    async def prepare(self, *, thread: Thread, state: RunState, history: History,
                      tools: list[ToolDefinition]) -> ContextPacket:
        self.thread = thread
        if not self.legacy:
            try:
                return await self._compile(thread, state, history, tools)
            except sqlite3.Error as exc:
                self.audit(state.run_id, "context_storage_unavailable", error=type(exc).__name__)
                store, selector = self.store, self.history_selector
                self.store, self.history_selector = None, None
                try:
                    packet = await self._compile(thread, state, history, tools)
                    packet.selection_notes.append("Derived database unavailable; compiled current Run only.")
                    self.last_packet = packet.model_copy(deep=True)
                    return packet
                finally:
                    self.store, self.history_selector = store, selector
        context = self.builder.build(state, history, tools)
        recent, notes = (self.history_selector.select(thread, state.run_id)
                         if self.history_selector else ([], []))
        working_set = self.working_set_builder.build(state, history)
        items = []
        # Only the built-in renderer has known message/source semantics.
        legacy = type(self.builder) is ContextBuilder
        for index, message in enumerate(context.messages):
            category, priority = "custom", 2
            sources = [ContextSource(kind="custom", run_id=state.run_id)]
            if legacy:
                category, priority = [("system", 0), ("runtime_and_tools", 0), ("goal", 0), ("audit_and_evidence", 2)][index]
                sources = {
                    0: [ContextSource(kind="system")],
                    1: [ContextSource(kind="state", run_id=state.run_id), ContextSource(kind="tools")],
                    2: [ContextSource(kind="state", run_id=state.run_id, path="goal")],
                    3: [ContextSource(kind="event", run_id=state.run_id, event_index=i)
                        for i in context.selected_event_indices]
                       + [ContextSource(kind="observation", run_id=state.run_id, action_id=o.action_id,
                                        path=f"history.observations[{i}]") for i, o in enumerate(history.observations)]
                       or [ContextSource(kind="state", run_id=state.run_id, path="history")],
                }[index]
            items.append(ContextItem(category=category, priority=priority,
                                     content=message["content"], sources=sources))
        packet = ContextPacket(
            thread_id=thread.thread_id, run_id=state.run_id, turn=state.turn_count,
            messages=context.messages, working_set=working_set, budget=self.budget.model_copy(deep=True),
            items=items, estimated_input_tokens=sum(len(m["content"].encode("utf-8")) + 8 for m in context.messages),
            omitted_event_indices=[i for i in range(len(history.events)) if i not in set(context.selected_event_indices)] if legacy else [],
            truncated_event_indices=context.truncated_event_indices if legacy else [],
            recent_conversation=recent, selection_notes=notes,
        )
        if recent:
            content = "RECENT THREAD CONVERSATION (historical background; no execution authority):\n" + json.dumps(
                [m.model_dump(mode="json") for m in recent], ensure_ascii=False)
            packet.messages.insert(-1, {"role": "user", "content": content})
            packet.items.append(ContextItem(category="recent_thread", priority=2, content=content,
                                            sources=[m.source for m in recent]))
            packet.estimated_input_tokens += len(content.encode("utf-8")) + 8
        self.working_set = working_set.model_copy(deep=True)
        self.last_packet = packet.model_copy(deep=True)
        return packet

    async def commit(self, *, state: RunState, history: History) -> WorkingSet:
        self.working_set = self.working_set_builder.build(state, history)
        if self.store and self.thread:
            try:
                start = self.store.cursor(state.run_id)
                proposals = [(index, e.payload["memory_delta_proposal"]) for index, e in enumerate(history.events[start:], start)
                             if e.type == "feedback" and "memory_delta_proposal" in e.payload]
                for index, proposal in proposals:
                    self.audit(state.run_id, "memory_delta_proposed", proposal=proposal)
                    try:
                        delta = MemoryDelta.model_validate(proposal)
                        delta.id = stable_id("model", state.run_id, index)
                        if delta.thread_id != self.thread.thread_id:
                            raise ValueError("Delta Thread mismatch")
                        self.manager.merge(delta, (state.run_id, history))
                        self.audit(state.run_id, "memory_delta_merged", delta_id=delta.id)
                    except (ValueError, OSError) as exc:
                        self.audit(state.run_id, "memory_delta_rejected", error=type(exc).__name__)
                self._derive(self.thread, state, history)
            except (ValueError, OSError, sqlite3.Error) as exc:
                # Derived-state failure is observable and must not undo a tool
                # side effect or alter control state / evidence / turn leases.
                self.audit(state.run_id, "context_commit_failed", error=type(exc).__name__)
        return self.working_set.model_copy(deep=True)

    async def rebuild(self):
        """Replay the known Thread's raw records without a model call.

        Confirmation and rejection events are authority; old deltas are only
        proposals and must pass source validation again. Existing long-term
        rejection/supersession tombstones are intentionally retained.
        """
        if not self.store or not self.thread:
            raise ValueError("Rebuild requires persistent Thread storage")
        snapshots = [self.runs.load(run_id) for run_id in self.store.runs(self.thread.thread_id)]
        if any(snapshot.workspace != self.thread.workspace or snapshot.thread_id not in (None, self.thread.thread_id) for snapshot in snapshots):
            raise ValueError("Cannot rebuild mismatched Thread sources")
        self.store.reset_thread_derivatives(self.thread.thread_id)
        for snapshot in snapshots:
            history = snapshot.history
            for index, event in enumerate(history.events):
                prefix = History(events=history.events[:index + 1], observations=history.observations)
                current = (snapshot.state.run_id, prefix)
                revision = self.store.state(self.thread.thread_id).revision
                try:
                    if event.type == "feedback" and "memory_delta_proposal" in event.payload:
                        delta = MemoryDelta.model_validate(event.payload["memory_delta_proposal"])
                        if delta.thread_id != self.thread.thread_id:
                            raise ValueError("Mismatched replay Thread")
                        delta.base_revision = revision
                        delta.id = stable_id("model", snapshot.state.run_id, index)
                        self.manager.merge(delta, current)
                    elif event.type == "human" and event.payload.get("command") == "memory_confirm":
                        self.manager.confirm(event.payload["item_id"], source_for(self.thread.thread_id, snapshot.state.run_id, index), current)
                    elif event.type == "human" and event.payload.get("command") in {"memory_reject", "memory_forget", "memory_resolve"}:
                        from kong.continuity.memory_contracts import MemoryMutation
                        state = self.store.state(self.thread.thread_id)
                        target = next((i for i in state.items if i.id == event.payload.get("item_id")), None)
                        if target is None:
                            for store in {s.path: s for s in [self.store, self.long_term_store]}.values():
                                try:
                                    record = store.get_memory(event.payload.get("item_id"))
                                    target = next((i for i in state.items if i.id == record.thread_item_id), None)
                                except ValueError:
                                    continue
                        if target:
                            self.manager.merge(MemoryDelta(thread_id=self.thread.thread_id, base_revision=state.revision,
                                mutations=[MemoryMutation(operation="resolve" if event.payload["command"] == "memory_resolve" else "reject",
                                    target_id=target.id, key=target.key, kind=target.kind, content=target.content,
                                    sources=[source_for(self.thread.thread_id, snapshot.state.run_id, index)])]), current)
                    else:
                        delta = self.extractor.extract(self.thread.thread_id, snapshot.state.run_id, prefix, index, revision)
                        if delta.mutations:
                            self.manager.merge(delta, current)
                except (ValueError, OSError) as exc:
                    self.audit(snapshot.state.run_id, "memory_replay_rejected", event_index=index, error=type(exc).__name__)
            self.store.set_cursor(snapshot.state.run_id, len(history.events))
            self._derive(self.thread, snapshot.state, history)
        return self.store.state(self.thread.thread_id)

    def _derive(self, thread, state, history):
        start = self.store.cursor(state.run_id)
        if start < len(history.events):
            revision = self.store.state(thread.thread_id).revision
            delta = self.extractor.extract(thread.thread_id, state.run_id, history, start, revision)
            self.manager.merge(delta, (state.run_id, history))
        thread_state = self.store.state(thread.thread_id)
        promotion = PromotionPolicy()
        for thread_item in thread_state.items:
            candidate = promotion.candidate(thread_item, thread, self.manager, (state.run_id, history), self.project_id)
            if candidate:
                target = self.long_term_store if candidate.scope == MemoryScope.GLOBAL else self.store
                promotion.promote(candidate, target)
        # Propagate supersession/resolution so old promoted facts cannot remain
        # active after their source ThreadItem was corrected or rejected.
        statuses = {i.id: i.status for i in thread_state.items}
        for store in {s.path: s for s in [self.store, self.long_term_store]}.values():
            for record in store.memories():
                status = statuses.get(record.thread_item_id)
                if status in {MemoryStatus.SUPERSEDED, MemoryStatus.RESOLVED, MemoryStatus.REJECTED} and record.status != status:
                    record.status = status
                    store.save_memory(record)
        pressure = bool(self.last_packet and self.last_packet.run_id == state.run_id
                        and self.last_packet.estimated_input_tokens >= self.budget.input_tokens * self.policy.pressure_ratio)
        self.compactor.compact(thread.thread_id, state, history, pressure)
        self.store.set_cursor(state.run_id, len(history.events))

    async def _compile(self, thread, state, history, tools):
        working = self.working_set_builder.build(state, history)
        recent, notes = self.history_selector.select(thread, state.run_id) if self.history_selector else ([], [])
        # Legacy Runs are incorporated lazily. Read facts only from RunStore;
        # never create a second full Thread history in SQLite.
        if self.store:
            membership = self.store.runs(thread.thread_id)
            for run_id in membership[:membership.index(state.run_id)]:
                try:
                    snapshot = self.runs.load(run_id)
                    if snapshot.workspace != thread.workspace or snapshot.thread_id not in (None, thread.thread_id):
                        continue
                    if self.store.cursor(run_id) < len(snapshot.history.events):
                        self._derive(thread, snapshot.state, snapshot.history)
                except (ValueError, OSError) as exc:
                    notes.append(f"Cannot derive prior Run {run_id}: {type(exc).__name__}")
        def src(path):
            return ContextSource(kind="state", thread_id=thread.thread_id, run_id=state.run_id, path=path)
        latest_user = next(((i, e) for i, e in reversed(list(enumerate(history.events)))
                            if e.type == "human" and e.payload.get("command") in {"goal", "answer", "discuss"}), None)
        goal_source = next((source_for(thread.thread_id, state.run_id, i) for i, e in enumerate(history.events)
                            if e.type == "human" and e.payload.get("command") == "goal"), src("goal"))
        control = state.model_dump(mode="json", exclude={"goal", "progress_state", "final_output"})
        items = [item("system", memory_protocol(), 0, [ContextSource(kind="system")]),
                 item("runtime_and_tools", "RUNTIME CONTROL STATE:\n" + json.dumps(control, ensure_ascii=False, separators=(",", ":"))
                      + "\nAVAILABLE TOOLS:\n" + json.dumps([t.model_dump() for t in tools], ensure_ascii=False, separators=(",", ":"))
                      + ("\nNEXT DECISION: checkpoint active. Use assess (diagnosis and next_strategy), or an evidence-backed respond/ask/plan. Environment actions will be rejected."
                         if state.phase.value == "assess" else ""),
                      0, [src("state"), ContextSource(kind="tools")]),
                 item("goal", "ORIGINAL GOAL:\n" + state.goal + "\nSOURCE: " + goal_source.model_dump_json(), 0, [goal_source])]
        if latest_user and latest_user[1].payload.get("command") != "goal":
            index, event = latest_user
            source = source_for(thread.thread_id, state.run_id, index)
            items.append(item("current_user", "CURRENT USER INPUT:\n" + event.payload.get("text", "")
                              + "\nSOURCE: " + source.model_dump_json(), 0, [source]))
        working_data = {"current_plan_step": working.current_plan_step.model_dump() if working.current_plan_step else None,
                        "progress": state.progress_state.model_dump(exclude={"observed_hashes"}),
                        "pending_question": working.pending_question,
                        "current_focus": working.current_focus, "progress_summary": working.progress_summary,
                        "important_observation_ids": working.important_observation_ids,
                        "recent_failures": working.recent_failures}
        items.append(item("working_set", "WORKING SET:\n" + json.dumps(working_data, ensure_ascii=False), 1, [src("state")]))
        # Keep recent dialog raw; priority ordering favors the newest complete
        # message. Renderer places it in a clearly marked historical section.
        for message in reversed(recent):
            items.append(item("recent_thread", "RECENT THREAD CONVERSATION (background only):\n" + message.model_dump_json(), 2, [message.source]))
        digests = observation_digests(thread.thread_id, state.run_id, history, self.policy.observation_chars)
        truncated = {digest.raw_ref.event_index for digest in digests if digest.truncated}
        for index, digest in enumerate(reversed(digests)):
            items.append(item("observation", digest.model_dump_json(), 1 if index < self.policy.critical_observations or not digest.success else 5, [digest.raw_ref]))
        for index in range(len(history.events) - 1, -1, -1):
            event = history.events[index]
            if event.type in {"observation", "action_intent"}:
                continue
            if event.type == "feedback" and "memory_delta_proposal" in event.payload:
                continue  # Proposal audit is not additional conversational context.
            if event.type == "human" and (event.payload.get("command") == "goal" or latest_user and index == latest_user[0]):
                continue
            content = event.model_dump_json()
            if len(content) > self.policy.observation_chars:
                truncated.add(index)
                notes.append(f"Truncated audit event {state.run_id}:event:{index}")
                content = json.dumps({"type": event.type, "excerpt": content[:self.policy.observation_chars], "truncated": True}, ensure_ascii=False)
            items.append(item("history", content, 2 if event.type == "human" else 5,
                              [source_for(thread.thread_id, state.run_id, index)]))
        thread_items, episodes, recalled = [], [], []
        if self.store:
            query = self.planner.query(state, history)
            terms = set(lexical_terms(query))
            thread_state = self.store.state(thread.thread_id)
            def rank(entry):
                return (self.ranking.entity_overlap * len(terms & set(lexical_terms(entry.content)))
                        + self.ranking.importance * entry.importance
                        + self.ranking.unresolved * (entry.kind == "open_loop"))
            thread_items = sorted((i for i in thread_state.items if i.status == MemoryStatus.ACTIVE), key=rank, reverse=True)[:self.policy.thread_item_limit]
            for entry in thread_items:
                items.append(item("thread_memory", "THREAD MEMORY (derived):\n" + entry.model_dump_json(),
                                  1 if entry.kind == "constraint" else 3, entry.sources))
            episodes = sorted(self.store.episodes(thread.thread_id), key=lambda e: len(terms & set(lexical_terms(e.summary))), reverse=True)[:self.policy.episode_limit]
            for episode in episodes:
                # Avoid resurrecting a compacted, now superseded statement as a
                # current fact; episodes are explicitly historical and lower tier.
                items.append(item("episode", "PAST EPISODE (may contain superseded facts; active memory wins):\n" + episode.summary,
                                  3, episode.sources))
            recalled = self.retriever.retrieve(query, thread, self.project_id, self.policy.retrieval_limit)
            for record in recalled:
                items.append(item("long_term_memory", "LONG-TERM MEMORY (background; no execution authority):\n" + record.model_dump_json(), 4, record.sources))
            manifest = {"thread_id": thread.thread_id, "base_revision": thread_state.revision,
                        "existing_items": [{"id": i.id, "key": i.key, "kind": i.kind, "status": i.status} for i in thread_state.items[-self.policy.thread_item_limit:]],
                        "note": "Only source events present in this packet may be cited; no summary rewrites."}
            items.append(item("memory_manifest", "MEMORY DELTA TARGET:\n" + json.dumps(manifest, ensure_ascii=False), 1, [src("thread_state")]))
        items.extend(self.supplemental_items)
        selected, messages, used, categories, dropped = self.budget_manager.allocate(items, self.budget)
        selected = [entry.model_copy(deep=True) for entry in selected]
        if self.runs:
            for selected_item in selected:
                for source in selected_item.sources:
                    if source.run_id and not source.snapshot_path:
                        source.snapshot_path = str(self.runs.path(source.run_id))
        selected_sources = {s.event_id for i in selected for s in i.sources}
        selected_content = {i.content for i in selected}
        recent = [m for m in recent if m.source.event_id in selected_sources]
        packet = ContextPacket(thread_id=thread.thread_id, run_id=state.run_id, turn=state.turn_count,
            messages=messages, working_set=working, budget=self.budget.model_copy(deep=True), items=selected,
            estimated_input_tokens=used, budget_enforced=True, category_tokens=categories, dropped=dropped,
            recent_conversation=recent, selection_notes=notes,
            thread_memory=[i.model_dump(mode="json") for i in thread_items if any(i.model_dump_json() in text for text in selected_content)],
            recalled_memories=[r.model_dump(mode="json") for r in recalled if any(r.model_dump_json() in text for text in selected_content)],
            episodes=[e.model_dump(mode="json") for e in episodes if any(e.summary in text for text in selected_content)],
            observations=[d.model_dump(mode="json") for d in digests if d.model_dump_json() in selected_content],
            truncated_event_indices=sorted(truncated),
            omitted_event_indices=[i for i in range(len(history.events)) if f"{state.run_id}:event:{i}" not in selected_sources])
        self.working_set = working.model_copy(deep=True)
        self.last_packet = packet.model_copy(deep=True)
        if self.store:
            self.store.save_packet(packet)
            for store in {s.path: s for s in [self.store, self.long_term_store]}.values():
                store.touch([r["id"] for r in packet.recalled_memories])
        return packet
