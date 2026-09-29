"""Read-only, redacted credential audit of a workspace and reachable Git history.

This is a bounded review aid, not a universal secret detector. It neither contacts
providers nor rotates keys. Run after fetching the remote refs you intend to audit.
Reports contain paths, object IDs and categories, never matching values or lines.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import zipfile


PATTERNS = {
    "provider_token": re.compile(rb"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|xox[baprs]-[A-Za-z0-9-]{20,}|tvly-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{30,})"),
    "aws_access_key_id": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    "bearer_token": re.compile(rb"\bBearer\s+[A-Za-z0-9._~+/-]{24,}", re.I),
    "jwt": re.compile(rb"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}"),
    "literal_credential": re.compile(rb"[\"']?\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret[_-]?key|password)[\"']?\s*[:=]\s*[\"'][A-Za-z0-9_./+=-]{16,}[\"']", re.I),
    "url_credentials": re.compile(rb"https?://[^\s/:@]{1,80}:[^\s/@]{8,}@", re.I),
}
EXCLUDED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"}
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024


def known_values(paths):
    """Read explicitly nominated credential files without echoing their contents."""
    found = set()
    for path in paths:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = re.split(r"\s*[:：=]\s*", line, maxsplit=1)
            if len(parts) == 2 and any(word in parts[0].lower() for word in ("key", "token", "secret", "password")):
                value = parts[1].strip().strip("\"'")
                if len(value) >= 8:
                    found.add(value.encode())
    if paths and not found:
        raise ValueError("No credential values found in nominated files")
    return found


def categories(data, secrets):
    result = [name for name, pattern in PATTERNS.items() if pattern.search(data)]
    if any(value in data for value in secrets):
        result.insert(0, "known_secret_value")
    return result


def redact_locations(value, secrets):
    """Locations can themselves contain credentials; never echo those either."""
    if isinstance(value, dict):
        return {key: redact_locations(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_locations(item, secrets) for item in value]
    if isinstance(value, str):
        data = value.encode("utf-8")
        for secret in sorted(secrets, key=len, reverse=True):
            data = data.replace(secret, b"[REDACTED]")
        for pattern in PATTERNS.values():
            data = pattern.sub(b"[REDACTED]", data)
        return data.decode("utf-8")
    return value


def audit_workspace(root, secrets, report_path):
    findings, skipped = [], []
    files = archive_members = 0
    for base, dirs, names in os.walk(root, followlinks=False):
        for name in list(dirs):
            path = Path(base) / name
            relative = path.relative_to(root).as_posix()
            if categories(relative.encode("utf-8"), secrets):
                findings.append({"path": relative, "categories": ["credential_in_path"]})
            if name in EXCLUDED_DIRS or (path / "pyvenv.cfg").exists() or path.is_symlink() or relative == ".kong/public-release-audit":
                dirs.remove(name)
                skipped.append({"path": relative, "reason": "audit_outputs_or_dependency_cache_or_git_storage" if not path.is_symlink() else "symlink"})
        for name in names:
            path = Path(base) / name
            relative = path.relative_to(root).as_posix()
            if path == report_path:
                continue
            try:
                if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
                    skipped.append({"path": relative, "reason": "symlink_or_file_size_limit"})
                    continue
                data = path.read_bytes()
                files += 1
                found = categories(data, secrets)
                if categories(relative.encode("utf-8"), secrets):
                    found.append("credential_in_path")
                if found:
                    findings.append({"path": relative, "categories": found})
                is_zip = path.suffix.lower() in {".zip", ".whl", ".docx", ".xlsx", ".pptx"}
                is_tar = path.name.lower().endswith((".tar.gz", ".tgz", ".tar"))
                if not is_zip and not is_tar:
                    continue
                total = 0
                with zipfile.ZipFile(path) if is_zip else tarfile.open(path) as archive:
                    members = archive.infolist() if is_zip else archive.getmembers()
                    for member in members:
                        member_name = member.filename if is_zip else member.name
                        member_size = member.file_size if is_zip else member.size
                        location = relative + "!" + member_name
                        if categories(location.encode("utf-8"), secrets):
                            findings.append({"path": location, "categories": ["credential_in_path"]})
                        if (is_zip and member.is_dir()) or (not is_zip and not member.isfile()):
                            continue
                        total += member_size
                        if member_size > MAX_FILE_BYTES or total > MAX_ARCHIVE_BYTES:
                            skipped.append({"path": location, "reason": "archive_size_limit"})
                            continue
                        content = archive.read(member) if is_zip else archive.extractfile(member).read()
                        archive_members += 1
                        found = categories(content, secrets)
                        if categories(location.encode("utf-8"), secrets):
                            found.append("credential_in_path")
                        if found:
                            findings.append({"path": location, "categories": found})
            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, tarfile.TarError) as error:
                skipped.append({"path": relative, "reason": type(error).__name__})
    return redact_locations({"files_scanned": files, "archive_members_scanned": archive_members,
                             "findings": findings, "skipped": skipped}, secrets)


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if result.returncode:
        raise RuntimeError("Git audit command failed; stderr omitted to avoid exposing repository data")
    return result.stdout


def audit_history(root, secrets):
    refs = git(root, "for-each-ref", "--format=%(refname) %(objectname)").decode().splitlines()
    objects = {}
    for line in git(root, "rev-list", "--objects", "--all").decode("utf-8", "replace").splitlines():
        oid, _, path = line.partition(" ")
        objects[oid] = path
    findings, skipped = [], []
    for ref in refs:
        ref_name = ref.rsplit(" ", 1)[0]
        found = categories(ref_name.encode("utf-8"), secrets)
        if found:
            findings.append({"type": "ref", "path": ref_name, "categories": found})
    counts = Counter()
    process = subprocess.Popen(["git", "-C", str(root), "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        for oid, path in objects.items():
            process.stdin.write((oid + "\n").encode())
            process.stdin.flush()
            header = process.stdout.readline().decode().strip().split()
            if len(header) != 3:
                raise RuntimeError("Git object read failed")
            kind, size = header[1], int(header[2])
            # Drain oversized objects so later reads remain aligned.
            if size > MAX_FILE_BYTES:
                remaining = size
                while remaining:
                    chunk = process.stdout.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        raise RuntimeError("Git object stream ended early")
                    remaining -= len(chunk)
                skipped.append({"object": oid, "path": path, "reason": "object_size_limit"})
                process.stdout.read(1)
                continue
            data = process.stdout.read(size)
            process.stdout.read(1)
            if kind == "tree":
                counts[kind] += 1
                # Every reachable tree has unique entry names even if several
                # commits reuse one content blob under different/deleted names.
                for entry in git(root, "ls-tree", "--name-only", "-z", oid).split(b"\0"):
                    if entry and categories(entry, secrets):
                        location = path + "/" + entry.decode("utf-8", "replace") if path else entry.decode("utf-8", "replace")
                        findings.append({"object": oid, "type": "tree", "path": location, "categories": ["credential_in_path"]})
                continue
            if kind not in {"blob", "commit", "tag"}:
                continue
            counts[kind] += 1
            found = categories(data, secrets)
            if categories(path.encode("utf-8"), secrets):
                found.append("credential_in_path")
            if found:
                findings.append({"object": oid, "type": kind, "path": path, "categories": found})
    finally:
        process.stdin.close()
        process.stdout.close()
        process.wait()
    return redact_locations({"refs": refs, "objects_scanned": dict(counts), "findings": findings, "skipped": skipped,
                             "scope": "Objects reachable from all fetched local refs, including deleted historical files, every tree entry name, and commit messages. Unreachable objects, GitHub caches, forks and unavailable refs are outside scope. Archived Git blobs are scanned as stored bytes; workspace archives are also expanded one level."}, secrets)


def audit(root, secret_files, output):
    root, output = root.resolve(), output.resolve()
    if output.exists():
        raise ValueError("Refusing to overwrite an earlier audit report")
    secrets = known_values(secret_files)
    workspace = audit_workspace(root, secrets, output)
    history = audit_history(root, secrets)
    lock = root / "uv.lock"
    report = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
              "head": git(root, "rev-parse", "HEAD").decode().strip(),
              "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None,
              "known_secret_values_checked": len(secrets), "workspace": workspace, "history": history,
              "limits": {"file_bytes": MAX_FILE_BYTES, "archive_expanded_bytes": MAX_ARCHIVE_BYTES,
                         "excluded_directory_names": sorted(EXCLUDED_DIRS)},
              "interpretation": "Findings require review: credential source files and synthetic test tokens can be expected. Zero findings is not proof that all unknown secret formats are absent. No matching text is emitted."}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--secret-file", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = audit(args.root, args.secret_file, args.output)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(2, "Audit failed: " + type(error).__name__ + "; no input contents emitted\n")
    print(json.dumps({"report": str(args.output), "files_scanned": report["workspace"]["files_scanned"],
                      "workspace_findings": len(report["workspace"]["findings"]),
                      "history_findings": len(report["history"]["findings"]),
                      "status": "review_required" if report["workspace"]["findings"] or report["history"]["findings"] else "no_pattern_matches"}))


if __name__ == "__main__":
    main()
