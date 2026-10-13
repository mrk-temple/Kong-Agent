"""Conservative, pure source policy. It never opens the Memory database."""


class SourceAccessGuard:
    def __init__(self, principal, access=None):
        self.principal = principal
        self.access = access

    def allows_binding(self, binding):
        return bool(self.access is not None and binding is not None
                    and all(binding.get(key) == value for key, value in self.principal.items())
                    and binding.get("access") == self.access)

    def allows_snapshot(self, snapshot):
        # Unmapped legacy history is quarantined rather than guessed safe.
        return self.allows_binding(snapshot.memory_binding)
