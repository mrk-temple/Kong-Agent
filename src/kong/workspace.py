"""Shared workspace boundary for tools and deterministic verifiers."""
from pathlib import Path


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("workspace must be a directory")

    def resolve(self, value: str) -> Path:
        target = (self.root / value).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("Path is outside the workspace")
        relative = target.relative_to(self.root)
        if any(part.casefold() in {".git", ".kong", ".venv", "node_modules"}
               or part.casefold().startswith(".env") or part.casefold() in {"kong.local.toml", "api.txt"}
               for part in relative.parts):
            raise ValueError("Path is reserved for local configuration or runtime data")
        return target
