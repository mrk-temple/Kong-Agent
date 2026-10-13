"""Stable failure types for hosts; failures never imply successful persistence."""


class MemoryError(Exception):
    pass


class PermissionDenied(MemoryError):
    pass


class NotFound(MemoryError):
    pass


class RevisionConflict(MemoryError):
    pass


class IdempotencyConflict(MemoryError):
    pass


class ForgottenSource(MemoryError):
    pass


class PolicyViolation(MemoryError):
    pass


class SchemaError(MemoryError):
    pass


class ServiceClosed(MemoryError):
    pass
