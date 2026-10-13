"""Standalone explicit-authority Memory Core; no Runtime or model required."""
from kong.memory.domain import (
    Authority, Candidate, Capability, CommandReceipt, DeletionPlan, DeletionReceipt, Identity,
    IdentityVersion, MemoryDraft, MemoryRecord, MemoryVersion, ParentVersion,
    Profile, RecordState, Retrieval, Scope, Source, candidate_digest, identity_digest,
    AccessState, Bottle, BottlePolicy, MemoryContext, RetrievalQuery,
    InteractionObservation, ObservationReceipt, Episode,
    Relationship, RelationshipVersion, relationship_digest,
)
from kong.memory.errors import (
    ForgottenSource, IdempotencyConflict, MemoryError, NotFound, PermissionDenied,
    PolicyViolation, RevisionConflict, SchemaError, ServiceClosed,
)
from kong.memory.service import MemoryService
from kong.memory.exchange import BottlePackage, ExchangeRecord, ImportPreview, parse_package
from kong.memory.formation import FormationBudget, FormationItem, ConsolidationPlan
from kong.memory.reflection import EvolutionProposal, ReflectionBudget, ReflectionReport, RollbackPlan

__all__ = [
    "Authority", "Candidate", "Capability", "CommandReceipt", "DeletionPlan", "DeletionReceipt", "Identity",
    "IdentityVersion", "MemoryDraft", "MemoryRecord", "MemoryVersion", "ParentVersion",
    "Profile", "RecordState", "Retrieval", "Scope", "Source", "candidate_digest", "identity_digest",
    "AccessState", "Bottle", "BottlePolicy", "MemoryContext", "RetrievalQuery",
    "InteractionObservation", "ObservationReceipt", "Episode", "FormationBudget", "FormationItem", "ConsolidationPlan",
    "Relationship", "RelationshipVersion", "relationship_digest", "EvolutionProposal", "ReflectionBudget", "ReflectionReport", "RollbackPlan",
    "BottlePackage", "ExchangeRecord", "ImportPreview", "parse_package",
    "ForgottenSource", "IdempotencyConflict", "MemoryError", "NotFound", "PermissionDenied",
    "PolicyViolation", "RevisionConflict", "SchemaError", "ServiceClosed", "MemoryService",
]
