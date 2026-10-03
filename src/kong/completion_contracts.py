"""Versioned host declarations for two bounded completion families.

These contracts are inputs to the runtime, never fields of a model Decision.
They describe requirements; they do not execute code or infer a natural-language goal.
"""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Declaration(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, strict=True)


class JsonField(Declaration):
    name: str = Field(min_length=1, max_length=200)
    type: Literal["string", "integer", "number", "boolean", "object", "array", "array_string", "array_integer", "null"]
    allowed: list[Any] = Field(default_factory=list, max_length=1000)


class ArtifactRequirement(Declaration):
    id: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=2000)
    format: Literal["file", "text", "json", "csv"] = "file"
    require_readback: bool = True
    expected_text: str | None = Field(default=None, max_length=1_000_000)
    exact_json: dict[str, Any] | None = None
    json_fields: list[JsonField] = Field(default_factory=list, max_length=100)
    additional_keys: bool = True
    csv_columns: list[str] = Field(default_factory=list, max_length=100)
    row_count: int | None = Field(default=None, ge=0, le=100_000)
    csv_rows: list[dict[str, str]] | None = Field(default=None, max_length=100_000)

    @model_validator(mode="after")
    def appropriate_fields(self):
        if self.expected_text is not None and self.format != "text":
            raise ValueError("expected_text requires format=text")
        if (self.exact_json is not None or self.json_fields or not self.additional_keys) and self.format != "json":
            raise ValueError("JSON conditions require format=json")
        if (self.csv_columns or self.row_count is not None or self.csv_rows is not None) and self.format != "csv":
            raise ValueError("CSV conditions require format=csv")
        if self.csv_rows is not None and (self.row_count is not None and len(self.csv_rows) != self.row_count
                or self.csv_columns and any(set(row) != set(self.csv_columns) for row in self.csv_rows)):
            raise ValueError("Declared CSV rows must match columns and row count")
        names = [field.name for field in self.json_fields]
        if len(set(names)) != len(names) or len(set(self.csv_columns)) != len(self.csv_columns):
            raise ValueError("Declared field names must be unique")
        return self


class CommandRequirement(Declaration):
    id: str = Field(min_length=1, max_length=200)
    argv: list[str] = Field(min_length=1, max_length=128)
    cwd: str = Field(default=".", min_length=1, max_length=2000)
    exit_code: int = 0
    stdout_contains: list[str] = Field(default_factory=list, max_length=30)
    stdout_exact: str | None = Field(default=None, max_length=64000)
    verify_final: bool = True
    fresh_python: bool = False


class CsvAggregate(Declaration):
    id: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=2000)
    artifact_id: str
    field: str = Field(min_length=1, max_length=200)
    operation: Literal["sum", "count", "unique_values", "sorted_values"]
    column: str | None = None
    filters: dict[str, list[str]] = Field(default_factory=dict)
    decimal_places: int | None = Field(default=None, ge=0, le=8)

    @model_validator(mode="after")
    def operation_fields(self):
        if self.operation != "count" and not self.column:
            raise ValueError("This aggregate requires column")
        if self.decimal_places is not None and self.operation != "sum":
            raise ValueError("decimal_places requires sum")
        if len(self.filters) > 20 or any(not values or len(values) > 100 for values in self.filters.values()):
            raise ValueError("Filters must have bounded nonempty allowed values")
        return self


class CompletionContract(Declaration):
    version: Literal[1] = 1
    family: Literal["code", "data"]
    artifacts: list[ArtifactRequirement] = Field(default_factory=list, max_length=100)
    protected_paths: list[str] = Field(default_factory=list, max_length=100)
    commands: list[CommandRequirement] = Field(default_factory=list, max_length=30)
    aggregates: list[CsvAggregate] = Field(default_factory=list, max_length=30)
    required_reads: list[str] = Field(default_factory=list, max_length=100)
    read_before_ask: list[str] = Field(default_factory=list, max_length=100)
    required_question: str | None = Field(default=None, max_length=16000)
    allow_completion: bool = True

    @model_validator(mode="after")
    def coherent_contract(self):
        for items in (self.artifacts, self.commands, self.aggregates):
            if len({item.id for item in items}) != len(items):
                raise ValueError("Requirement IDs must be unique within each section")
        if self.allow_completion and not self.artifacts:
            raise ValueError("Completion requires declared final artifacts")
        if self.family == "code" and (not self.commands or not any(c.verify_final for c in self.commands)):
            raise ValueError("Code completion requires a final verification command")
        for command in self.commands:
            executable = command.argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
            if command.fresh_python and command.argv[0] != "python":
                raise ValueError("Fresh Python verification uses executable alias python")
            if self.family == "code" and command.verify_final and executable in {"python", "python.exe", "python3"} and not command.fresh_python:
                raise ValueError("Final Python source verification requires fresh_python=true")
        if self.family == "data" and any(not artifact.require_readback for artifact in self.artifacts):
            raise ValueError("Data artifacts require a final readback")
        by_id = {item.id: item for item in self.artifacts}
        for aggregate in self.aggregates:
            artifact = by_id.get(aggregate.artifact_id)
            if not artifact or artifact.format != "json" or aggregate.source not in self.protected_paths:
                raise ValueError("Aggregate requires a declared JSON artifact and protected CSV source")
        return self

    def version_paths(self) -> list[str]:
        return sorted(set([a.path for a in self.artifacts] + self.protected_paths + self.required_reads + self.read_before_ask))


def has_business_conditions(contract):
    if not contract.allow_completion:
        return bool(contract.read_before_ask or contract.required_question)
    if contract.family == "code":
        return any(command.verify_final for command in contract.commands)
    return bool(contract.artifacts) and all(
        artifact.expected_text is not None or artifact.exact_json is not None or artifact.json_fields
        or artifact.csv_columns or artifact.row_count is not None or artifact.csv_rows is not None
        or any(rule.artifact_id == artifact.id for rule in contract.aggregates)
        for artifact in contract.artifacts)


def contract_for_task_family(family, contract):
    """Validate explicit caller scope, without classifying a natural-language goal."""
    if family is None:
        return contract
    if family not in {"code", "data"}:
        raise ValueError("Task family must be code or data")
    if contract is None:
        raise ValueError("Explicit code/data tasks require --completion-contract with host-owned requirements")
    if contract.family != family:
        raise ValueError("Completion contract family must match --task-family")
    if not has_business_conditions(contract):
        raise ValueError("Declared task family requires structure/value/business conditions, not file existence alone")
    return contract
