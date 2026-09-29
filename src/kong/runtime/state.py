from kong.contracts import Mode, RunState, Status
from kong.runtime.policy import ModePolicy


def create_run_state(goal: str, mode: Mode = Mode.AUTO, **kwargs) -> RunState:
    policy = ModePolicy.for_mode(mode)
    return RunState(goal=goal, mode=mode, turn_budget=policy.lease,
                    hard_turn_limit=policy.hard_limit, **kwargs)


__all__ = ["Mode", "RunState", "Status", "create_run_state"]
