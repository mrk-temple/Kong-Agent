"""Recent original conversation, read through membership without duplicating History."""
from pathlib import Path

from kong.continuity.contracts import ConversationMessage, ContextSource, RecentContextPolicy, Thread
from kong.continuity.storage import ThreadStore
from kong.storage import RunStore, Snapshot


class HistorySelector:
    def __init__(self, runs: RunStore, threads: ThreadStore, policy: RecentContextPolicy | None = None):
        self.runs = runs
        self.threads = threads
        self.policy = policy or RecentContextPolicy()

    @staticmethod
    def conversation(snapshot: Snapshot, thread_id: str) -> list[ConversationMessage]:
        run_id = snapshot.state.run_id
        # V0.1 stored initial user input in state.goal, not an Event. The stable
        # goal reference names that actual source; it does not invent an event.
        messages = [ConversationMessage(role="user", content=snapshot.state.goal, source=ContextSource(
            kind="state", thread_id=thread_id, run_id=run_id, event_id=f"{run_id}:goal", path="state.goal"))]
        for index, event in enumerate(snapshot.history.events):
            role, content = None, None
            if event.type == "human" and event.payload.get("command") in {"answer", "discuss", "resolve_interrupted_action"}:
                role, content = "user", event.payload.get("text")
            elif event.type == "decision":
                decision = event.payload.get("decision", {})
                if decision.get("kind") == "ask":
                    role, content = "assistant", decision.get("question")
                elif decision.get("kind") == "respond":
                    # Only completed, accepted outputs become conversational
                    # answers. Rejected completion attempts stay in raw audit.
                    following = []
                    for later in snapshot.history.events[index + 1:]:
                        if later.type in {"decision", "human"}:
                            break
                        following.append(later)
                    accepted = any(e.type == "feedback" and e.payload.get("transition", {}).get("reason")
                                   in {"completion_gate_passed", "plan_discussion"} for e in following)
                    if accepted:
                        role, content = "assistant", decision.get("content")
            if isinstance(content, str) and content and role:
                messages.append(ConversationMessage(role=role, content=content, source=ContextSource(
                    kind="event", thread_id=thread_id, run_id=run_id, event_index=index,
                    event_id=f"{run_id}:event:{index}", path=f"history.events[{index}]")))
        return messages

    def select(self, thread: Thread, current_run_id: str) -> tuple[list[ConversationMessage], list[str]]:
        membership = self.threads.runs(thread.thread_id)
        if current_run_id not in membership:
            raise ValueError("Current Run is not a member of this Thread")
        # Resuming an earlier Run must not inject conversations from later Runs.
        preceding = membership[:membership.index(current_run_id)]
        selected, notes, used = [], [], 0
        for run_id in reversed(preceding):
            if len(selected) >= self.policy.max_messages or used >= self.policy.max_chars:
                break
            try:
                snapshot = self.runs.load(run_id)
            except (OSError, ValueError):
                notes.append(f"Skipped unreadable Run {run_id}")
                continue
            if (Path(snapshot.workspace).resolve() != Path(thread.workspace).resolve()
                    or snapshot.thread_id not in (None, thread.thread_id)):
                notes.append(f"Skipped mismatched Run {run_id}")
                continue
            for message in reversed(self.conversation(snapshot, thread.thread_id)):
                if len(selected) >= self.policy.max_messages:
                    break
                if used + len(message.content) > self.policy.max_chars:
                    notes.append(f"Omitted oversized original message {message.source.event_id}")
                    continue
                selected.append(message)
                used += len(message.content)
        return list(reversed(selected)), notes
