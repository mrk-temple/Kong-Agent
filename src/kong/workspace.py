"""Shared workspace boundary for tools and deterministic verifiers."""
from pathlib import Path


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("workspace must be a directory")
        self._reserved_files: set[Path] = set()

    def reserve_file(self, path: str | Path) -> None:
        """Host-owned assets may not be accessed through ordinary file tools."""
        self._reserved_files.add(Path(path).resolve())

    def resolve(self, value: str) -> Path:
        target = (self.root / value).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("Path is outside the workspace")
        if target in self._reserved_files:
            raise ValueError("Path is reserved for host-owned Memory data")
        relative = target.relative_to(self.root)
        if any(part.casefold() in {".git", ".kong", ".venv", "node_modules"}
               or part.casefold().startswith(".env") or part.casefold() in {"kong.local.toml", "api.txt"}
               for part in relative.parts):
            raise ValueError("Path is reserved for local configuration or runtime data")
        return target
