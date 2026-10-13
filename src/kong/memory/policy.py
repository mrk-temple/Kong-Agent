"""Conservative Phase 1 policy: candidates never automatically activate."""
import re

from kong.memory.domain import Authority, Capability, MemoryDraft, Scope, Source
from kong.memory.errors import PermissionDenied, PolicyViolation


def require(auth: Authority, capability: Capability, *, explicit: bool = False) -> None:
    if capability not in auth.capabilities:
        raise PermissionDenied(f"Missing capability: {capability}")
    if explicit and (auth.actor != "owner" or not auth.input_id):
        raise PermissionDenied("An explicit owner input is required")


def require_scope(auth: Authority, scope: Scope) -> None:
    if scope not in auth.scopes:
        raise PermissionDenied("Scope is not granted")


def validate_draft(draft: MemoryDraft) -> None:
    # This is a narrow guard, not a comprehensive natural-language classifier.
    secret_pattern = r"(?i)(password|api[_ -]?key|access[_ -]?token|密码|私钥)(?:\s*[:=：]\s*|\s+)\S{6,}|-----BEGIN .*PRIVATE KEY-----"
    if draft.sensitivity == "secret" or re.search(secret_pattern, draft.content):
        raise PolicyViolation("Credentials are not supported by ordinary Memory storage")
    if draft.subject_kind == "third_party" and draft.sensitivity == "sensitive":
        raise PolicyViolation("Third-party sensitive memory is unsupported in Phase 1")


def validate_explicit_source(auth: Authority, draft: MemoryDraft, source: Source) -> None:
    if source.source_id != auth.input_id or source.origin != "user_command" or source.speaker != "user":
        raise PermissionDenied("Source must bind to the current explicit owner command")
    if draft.content not in source.excerpt:
        raise PolicyViolation("Explicit content must be present in the approved source excerpt")
    # Do not retain extra credential material in a seemingly innocuous source.
    validate_draft(draft.model_copy(update={"content": source.excerpt}))
