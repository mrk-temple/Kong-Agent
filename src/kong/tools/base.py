from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    success: bool
    output: Any = None
    error: str | None = None


class ToolDefinition(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool = False
    family: str = "filesystem"
    effects: list[str] = Field(default_factory=list)
    environment: str = "workspace"
    output_policy: str = "bounded"


class Tool(ABC):
    name: str
    description: str
    args_model: type[BaseModel]
    read_only: bool = False
    family: str = "filesystem"
    effects: tuple[str, ...] | None = None
    environment: str = "workspace"
    execution_timeout: float | None = None

    def progress_output(self, output: Any) -> Any:
        return output

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=self.args_model.model_json_schema(),
            read_only=self.read_only,
            family=self.family,
            effects=list(self.effects if self.effects is not None else
                         (() if self.read_only else ("local_write",))),
            environment=self.environment,
        )

    @abstractmethod
    async def run(self, **kwargs: Any) -> ToolResult:
        pass
