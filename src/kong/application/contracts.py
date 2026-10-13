"""Surface commands: identity and execution authority belong to the host."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool
from kong.contracts import Criterion
from kong.completion_contracts import CompletionContract


class AppError(ValueError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status

    def dto(self):
        return {"code": self.code, "message": self.message, "retryable": False}


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Command(DTO):
    command_id: str = Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9_.:-]+$")


class ThreadCreate(Command):
    title: str = Field(default="新对话", max_length=120)


class RunCreate(Command):
    text: str = Field(min_length=1, max_length=16000)
    intent: Literal["chat", "task"]
    mode: Literal["fast", "auto", "plan"] = "auto"
    acceptance: list[Criterion] = Field(default_factory=list, max_length=50)
    completion_contract: CompletionContract | None = None


class InputCommand(Command):
    kind: Literal["constraint", "goal", "cancel"]
    text: str = Field(default="", max_length=16000)


class HumanCommand(Command):
    kind: Literal["answer", "approve", "discuss", "reject", "resume", "resolve"]
    text: str = Field(default="", max_length=16000)
    revision: int | None = Field(default=None, ge=1)


class FreshCommand(RunCreate):
    pass


class MemoryCreate(Command):
    content: str = Field(min_length=1, max_length=6000)
    category: str = Field(default="fact", min_length=1, max_length=100)
    kind: Literal["semantic", "episodic"] = "semantic"
    sensitivity: Literal["public", "personal", "sensitive", "secret"] = "personal"
    bottle_id: str = "personal-memory"


class MemoryRevise(Command):
    content: str = Field(min_length=1, max_length=6000)
    expected_revision: int = Field(ge=1)


class ForgetCommand(Command):
    expected_revision: int = Field(ge=1)
    approved_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class ActivationCommand(Command):
    active: StrictBool
    expected_revision: int = Field(ge=1)


class PinCommand(Command):
    bottle_id: str
    pinned: StrictBool


class CandidateCommand(Command):
    approved_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    expected_revision: int | None = Field(default=None, ge=1)


class ModelSetup(Command):
    provider: str = Field(default="compatible", min_length=1, max_length=100)
    base_url: str = Field(max_length=1000)
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    stream: StrictBool = False
    json_mode: StrictBool = True
