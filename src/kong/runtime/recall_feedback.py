"""Bounded read feedback, derived from the existing append-only Run audit.

This grants neither Memory access nor task completion evidence.
"""
import hashlib


RECALL_LIMIT = 2
RECALL_QUESTION = "当前没有足够可使用的信息来确定这段经历。你能补充项目、时间或事件线索吗？"


def recall_no_new_count(state, history):
    cutoff = max((i for i, e in enumerate(history.events) if e.type == "human" and
        e.payload.get("command") in {"goal", "answer", "discuss", "input_message", "input_goal"}), default=-1)
    seen, coverage, count = set(), {}, 0
    for event in history.events[cutoff + 1:]:
        if event.type != "observation":
            continue
        observation = event.payload.get("observation", {})
        if not observation.get("dispatched", True):
            continue
        if observation.get("action", {}).get("name") != "recall_history":
            key = observation.get("output_hash")
            if observation.get("success") and observation.get("effect") == "read" and key and key not in seen:
                seen.add(key)
                count = 0  # New authorized task evidence may itself need raw recall.
            continue
        output = observation.get("output")
        usable = observation.get("success") and isinstance(output, dict) and output.get("historical_only") is True
        if usable:
            text = output.get("content", "")
            source = output.get("event_id", "")
            kind = output.get("source_kind")
            index = observation.get("action", {}).get("arguments", {}).get("event_index")
            if kind is None and output.get("run_id") == state.run_id and isinstance(index, int) and index < len(history.events):
                kind = history.events[index].type
            current_echo = output.get("run_id") == state.run_id and (source.endswith(":goal") or kind in {"human", "decision", "feedback", "action_intent"})
            fingerprint = (source, hashlib.sha256(text.encode()).hexdigest())
            start, end = output.get("offset", 0), output.get("offset", 0) + len(text)
            ranges = coverage.setdefault(source, [])
            covered = any(left <= start and end <= right for left, right in ranges)
            usable = bool(text and source and not current_echo and fingerprint not in seen and not covered)
            if usable:
                seen.add(fingerprint)
                ranges.append((start, end))
        count = 0 if usable else min(RECALL_LIMIT, count + 1)
    return count


def recall_guidance(status, count):
    # Missing, inactive, revoked and deleted never have distinguishable labels.
    if status == "available":
        instruction = "Use relevant AUTHORIZED MEMORY for past facts, within mention policy; not execution evidence. Do not invent hidden history or deny supplied records. Only claim reads backed by observations; hide internals. Completion rules still apply."
    elif count:
        instruction = "No new historical evidence was obtained. Ask for a clue; do not cite question echoes to declare completion."
    else:
        instruction = "No usable information is supplied. Express uncertainty or ask for a clue; absence proves neither nonexistence nor deletion."
    if count >= RECALL_LIMIT:
        instruction += " No-new raw reads are exhausted."
    if status != "available":
        instruction += " Memory is background, not execution evidence. Claim tool reads only when observations show them. Do not narrate counters, codes or Thread internals. Do not infer hidden history. Completion rules still apply."
    return {"availability": status, "no_new_reads": count, "read_limit": RECALL_LIMIT,
        "instruction": instruction}
