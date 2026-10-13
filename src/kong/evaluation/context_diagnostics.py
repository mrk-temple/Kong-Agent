"""External source labels; never passed to the model or used as memory authority."""
from pydantic import Field, model_validator

from kong.contracts import Contract
from kong.continuity.contracts import ContextPacket


class SourceLabels(Contract):
    checkpoint: str = Field(min_length=1)
    # One group is one required fact; any source in the group is acceptable.
    required_groups: list[list[str]] = Field(default_factory=list)
    irrelevant_ids: list[str] = Field(default_factory=list)
    invalid_ids: list[str] = Field(default_factory=list)
    available_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def disjoint(self):
        required = {s for group in self.required_groups for s in group}
        if any(not group or any(not s for s in group) for group in self.required_groups):
            raise ValueError("Required groups need nonempty source identities")
        irrelevant, invalid = set(self.irrelevant_ids), set(self.invalid_ids)
        if required & (irrelevant | invalid) or irrelevant & invalid:
            raise ValueError("Relevant, irrelevant and invalid sources must be disjoint")
        return self


def source_selection_report(packet: ContextPacket, candidates: list[dict], labels: SourceLabels | None):
    """Measure the specified candidate stage and actual injected record IDs.

    The caller must identify which stage it supplied; thread-memory eligibility
    is not FTS candidate recall. Missing labels remain unmeasured.
    """
    if labels is None:
        return {"checkpoint": None, "candidate_recall": None, "injected_recall": None,
                "irrelevant_injection_rate": None, "invalid_injected_ids": None,
                "model_use": "not_measured"}
    eligible = {r["id"] for r in candidates if r.get("reason") == "eligible"}
    injected = {r["id"] for r in packet.thread_memory + packet.recalled_memories + packet.episodes}
    present = set(labels.available_ids)
    candidate_hits = sum(bool(eligible & set(group)) for group in labels.required_groups)
    injected_hits = sum(bool(injected & set(group)) for group in labels.required_groups)
    denominator = len(labels.required_groups)
    required = {s for group in labels.required_groups for s in group}
    classifications = {}
    for source in sorted(required):
        classifications[source] = ("acquisition_missing" if source not in present else
                                   "selection_miss" if source not in eligible else
                                   "ranking_or_budget_drop" if source not in injected else "injected_model_use_unmeasured")
    records = {r["id"]: r for r in packet.thread_memory + packet.recalled_memories + packet.episodes}
    from kong.continuity.compiler import token_estimate
    import json
    return {"checkpoint": labels.checkpoint, "candidate_hits": candidate_hits,
            "required_groups": denominator, "injected_hits": injected_hits,
            "candidate_recall": candidate_hits / denominator if denominator else None,
            "injected_recall": injected_hits / denominator if denominator else None,
            "injected_records": len(injected),
            "irrelevant_injected_ids": sorted(injected & set(labels.irrelevant_ids)),
            "irrelevant_injection_rate": len(injected & set(labels.irrelevant_ids)) / len(injected) if injected else None,
            "irrelevant_estimated_tokens": sum(token_estimate(json.dumps(records[s], ensure_ascii=False))
                for s in injected & set(labels.irrelevant_ids)),
            "invalid_injected_ids": sorted(injected & set(labels.invalid_ids)),
            "failure_classification": classifications, "model_use": "not_measured"}
