"""Explicit frozen scope for a paid development batch; not a formal acceptance."""
from typing import Literal

from pydantic import Field, StrictInt, model_validator

from kong.contracts import Contract
from kong.evaluation.scenarios import Scenario


class LiveManifest(Contract):
    protocol: Literal["kong-live-development-1"] = "kong-live-development-1"
    kind: Literal["development"] = "development"
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    resources: dict[str, str]
    image: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    model: dict
    scenarios: list[dict] = Field(min_length=1, max_length=8)
    max_requests: StrictInt = Field(gt=0)
    max_total_tokens: StrictInt = Field(gt=0)
    max_active_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def scope_and_budget(self):
        scenarios = [Scenario.from_manifest(s) for s in self.scenarios]
        if len({s.id for s in scenarios}) != len(scenarios):
            raise ValueError("Manifest scenarios must be distinct")
        if self.max_requests != sum(s.budget.requests for s in scenarios):
            raise ValueError("Request cap must equal the frozen task budgets")
        if self.max_total_tokens != sum(s.budget.total_tokens for s in scenarios):
            raise ValueError("Token cap must equal the frozen task budgets")
        if self.max_active_seconds != sum(s.budget.active_seconds for s in scenarios):
            raise ValueError("Active-time cap must equal the frozen task budgets")
        if not self.resources or "src/kong/evaluation/long_driver.py" not in self.resources:
            raise ValueError("Manifest must identify the executing evaluation package")
        import re
        if any(not re.fullmatch(r"[0-9a-f]{64}", h) for h in self.resources.values()):
            raise ValueError("Resources need SHA-256 identities")
        if "api_key" in self.model:
            raise ValueError("Do not persist credentials in a live manifest")
        return self

    def verify(self, scenarios, resources, image, model):
        if [Scenario.from_manifest(s) for s in self.scenarios] != list(scenarios):
            raise ValueError("Selected task inputs or order differ from the frozen manifest")
        if self.resources != resources or self.image != image or self.model != model:
            raise ValueError("Source, image or model configuration differs from the frozen manifest")
