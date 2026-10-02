"""Copy evaluation JSON audit evidence outside ignored Runtime storage."""

import hashlib
import json
from pathlib import Path
import shutil


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def archive_fixture_trial(trial: Path) -> dict:
    """Create a new audit directory; never replace an earlier audit or snapshot."""
    workspace = trial / "workspace"
    sources = []
    for group in ("runs", "reports"):
        source_dir = workspace / ".kong" / group
        if source_dir.is_dir():
            sources.extend((group, source) for source in sorted(source_dir.glob("*.json"))
                           if source.is_file() and not source.is_symlink())
    audit = trial / "audit"
    audit.mkdir(exist_ok=False)
    files = []
    for group, source in sources:
        target_dir = audit / group
        target_dir.mkdir(exist_ok=True)
        target = target_dir / source.name
        with target.open("xb") as output, source.open("rb") as stream:
            shutil.copyfileobj(stream, output)
        if _sha256(source) != _sha256(target):
            raise OSError("Trial audit copy hash mismatch")
        files.append({"path":target.relative_to(audit).as_posix(),
                      "bytes":target.stat().st_size, "sha256":_sha256(target)})
    index = {"schema":"v04a-runtime-audit-1", "scope":"JSON Runtime snapshots and reports only; SQLite and caches excluded",
             "files":files}
    with (audit / "index.json").open("x", encoding="utf-8") as stream:
        json.dump(index, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return {"path":"audit/index.json", "files":len(files), "sha256":_sha256(audit / "index.json")}
