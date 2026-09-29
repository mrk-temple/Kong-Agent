from dataclasses import dataclass

from kong.contracts import Mode


@dataclass(frozen=True)
class ModePolicy:
    lease: int
    hard_limit: int
    plan_required: bool = False
    plan_allowed: bool = True

    @classmethod
    def for_mode(cls, mode: Mode) -> "ModePolicy":
        return {
            Mode.FAST: cls(4, 4, plan_allowed=False),
            Mode.AUTO: cls(4, 40),
            Mode.PLAN: cls(6, 60, plan_required=True),
        }[mode]
