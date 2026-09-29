"""Extractive observation digests and immutable Episodes; never summarize summaries."""
import json

from kong.continuity.memory import source_for, stable_id
from kong.continuity.memory_contracts import ContinuityPolicy, Episode, ObservationDigest
from kong.continuity.memory_store import MemoryStore
from kong.contracts import History, RunState, Status


def web_excerpt(output, max_chars):
    """Keep readable evidence ahead of bulky links/metadata and expose next offset."""
    if not isinstance(output, dict) or not isinstance(output.get("content"), str):
        return None
    content = output["content"]
    base = {k: output[k] for k in ("source_id", "url", "path", "kind", "offset", "size", "historical_only", "source_truncated") if k in output}
    def encode(count):
        return json.dumps({**base, "content": content[:count],
            "next_offset": output.get("offset", 0) + count,
            "truncated": count < len(content) or bool(output.get("truncated")),
            "untrusted": True}, ensure_ascii=False)
    low, high = 0, len(content)
    if len(encode(0)) > max_chars:
        return None
    while low < high:
        mid = (low + high + 1) // 2
        if len(encode(mid)) <= max_chars:
            low = mid
        else:
            high = mid - 1
    return encode(low)


def observation_digests(thread_id: str, run_id: str, history: History, max_chars: int) -> list[ObservationDigest]:
    result = []
    for index, event in enumerate(history.events):
        if event.type != "observation":
            continue
        observation = event.payload.get("observation", {})
        raw = json.dumps(observation.get("output"), ensure_ascii=False)
        output = observation.get("output")
        action_name = observation.get("action", {}).get("name")
        if action_name == "read_file" and isinstance(output, str) and len(raw) > max_chars:
            output = {"path":observation["action"]["arguments"].get("path"),
                      "offset":0, "size":len(output), "content":output}
        excerpt = (web_excerpt(output, max_chars)
                   if action_name in {"web_fetch", "web_read", "read_file"} else None)
        error = observation.get("error")
        action = observation.get("action", {})
        summary = f"{action.get('name', 'tool')}: {'success' if observation.get('success') else 'failed'}"
        arguments = {key: str(value)[:160] for key, value in action.get("arguments", {}).items() if key != "content"}
        if arguments:
            summary += " " + json.dumps(arguments, ensure_ascii=False)[:500]
        if error:
            summary += ": " + str(error)[:300]
        source = source_for(thread_id, run_id, index)
        source.action_id = observation.get("action_id")
        result.append(ObservationDigest(action_id=observation.get("action_id", "unknown"),
            success=bool(observation.get("success")), summary=summary,
            important_excerpts=[excerpt if excerpt is not None else raw[:max_chars]], raw_ref=source, size=len(raw.encode("utf-8")),
            truncated=len(raw) > max_chars or bool(isinstance(observation.get("output"), dict)
                                                  and observation["output"].get("truncated"))))
    return result


class Compactor:
    def __init__(self, store: MemoryStore, policy: ContinuityPolicy):
        self.store, self.policy = store, policy

    def compact(self, thread_id: str, state: RunState, history: History, pressure: bool = False) -> Episode | None:
        previous = [e for e in self.store.episodes(thread_id) if e.run_id == state.run_id]
        start = max((e.last_event + 1 for e in previous), default=0)
        events = history.events[start:]
        if not events:
            return None
        turns = {e.turn for e in events if e.type == "decision"}
        done_steps = {s.id for s in state.plan.steps if s.done} if state.plan else set()
        phase_end = any(e.type == "decision" and e.payload.get("decision", {}).get("kind") == "complete_step"
                        and e.payload["decision"].get("step_id") in done_steps for e in events)
        if state.status == Status.COMPLETED:
            trigger = "run_complete"
        elif phase_end:
            trigger = "phase_end"
        elif pressure:
            trigger = "token_pressure"
        elif len(turns) >= self.policy.compact_turns:
            trigger = "turn_threshold"
        else:
            return None
        excerpts = []
        for index, event in enumerate(events, start):
            text = None
            if event.type == "human":
                text = event.payload.get("text")
            elif event.type == "decision":
                decision = event.payload.get("decision", {})
                text = decision.get("content") or decision.get("question") or decision.get("diagnosis")
            elif event.type == "observation":
                obs = event.payload.get("observation", {})
                text = f"Tool {obs.get('action', {}).get('name')} {obs.get('action_id')}: success={obs.get('success')}"
            if text:
                excerpts.append(f"[{state.run_id}:event:{index}] {str(text)[:500]}")
        summary = "Extractive episode; excerpts may omit details, inspect raw refs.\n" + "\n".join(excerpts)
        episode = Episode(id=stable_id(thread_id, state.run_id, start, len(history.events), trigger),
            thread_id=thread_id, run_id=state.run_id, first_event=start, last_event=len(history.events) - 1,
            summary=summary[:self.policy.episode_chars], trigger=trigger,
            sources=[source_for(thread_id, state.run_id, i) for i in range(start, len(history.events))])
        self.store.save_episode(episode)
        return episode
