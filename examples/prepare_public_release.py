"""Create and scan a reviewable source snapshot; never publish or initialize Git.

Only the explicit public roots are copied. Optional local secret values are checked
in memory and never printed. Passing scans does not settle publication rights or
audit Git history.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
from uuid import uuid4
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DIRECTORIES = ("src/kong", "tests", "examples", ".github/workflows")
PUBLIC_FILES = (
    "README.md", "ARCHITECTURE.md", "EVALUATION.md", "RECOVERY.md",
    "CHANGELOG.md", "PUBLIC_RELEASE_CHECKLIST.md", ".env.example",
    "pyproject.toml", "uv.lock", "kong.example.toml", ".gitignore",
    "LICENSE", "NOTICE",
)
PUBLIC_ROOTS = PUBLIC_DIRECTORIES + PUBLIC_FILES
PRIVATE_PARTS = {
    ".git", ".kong", ".venv", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", ".nox", "__pycache__", "build", "dist",
    "kong-test", "api.txt", "kong.local.toml", "docs", "design.md",
    "upgrade_plan.md", "kong_evolution_plan.md", ".ds_store", ".coverage",
}
PRIVATE_SUFFIXES = (
    ".pyc", ".pyo", ".egg-info", ".log", ".db", ".sqlite",
    ".sqlite3", ".pem", ".key", ".p12", ".pfx", ".tmp",
    ".bak", ".orig", ".swp", ".swo", ".whl", ".zip", ".tar.gz",
)
# Keep these patterns local so a copied release candidate can run this script alone.
CREDENTIAL_PATTERNS = {
    "provider_token": re.compile(rb"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|xox[baprs]-[A-Za-z0-9-]{20,}|tvly-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{30,})"),
    "aws_access_key_id": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    "bearer_token": re.compile(rb"\bBearer\s+[A-Za-z0-9._~+/-]{24,}", re.I),
    "jwt": re.compile(rb"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}"),
    "literal_credential": re.compile(rb"[\"']?\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret[_-]?key|password)[\"']?\s*[:=]\s*[\"'][A-Za-z0-9_./+=-]{16,}[\"']", re.I),
    "url_credentials": re.compile(rb"https?://[^\s/:@]{1,80}:[^\s/@]{8,}@", re.I),
}


def private_path(path):
    normalized = path.as_posix().lower()
    return any(
        p in PRIVATE_PARTS
        or p.startswith(".env") and normalized != ".env.example"
        or p.endswith(PRIVATE_SUFFIXES)
        or ".log." in p
        or p.endswith(".toml") and normalized not in {"pyproject.toml", "kong.example.toml"}
        for p in (part.lower() for part in path.parts)
    )


def public_path(path):
    """Allow only reviewed roots, even when scanning a supplied archive."""
    name = path.as_posix()
    if private_path(path):
        return False
    return name in PUBLIC_FILES or any(name.startswith(root + "/") for root in PUBLIC_DIRECTORIES)


def redact_location(name, secrets):
    data = name.encode("utf-8")
    for secret in sorted(secrets, key=len, reverse=True):
        data = data.replace(secret, b"[REDACTED]")
    for pattern in CREDENTIAL_PATTERNS.values():
        data = pattern.sub(b"[REDACTED]", data)
    return data.decode("utf-8")


def public_files(root, secrets=()):
    found = []
    for entry in PUBLIC_ROOTS:
        start = root / entry
        candidates = start.rglob("*") if start.is_dir() else [start]
        for path in candidates:
            relative = path.relative_to(root)
            if not public_path(relative):
                continue
            if path.is_symlink() or (path.exists() and not path.resolve().is_relative_to(root.resolve())):
                raise ValueError("Public source contains a link outside the snapshot: " + redact_location(relative.as_posix(), secrets))
            if path.is_file():
                found.append(path)
    return sorted(set(found))


def secret_values(paths):
    values = set()
    for path in paths:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = re.split(r"\s*[:：=]\s*", line, maxsplit=1)
            if len(parts) == 2 and any(term in parts[0].lower() for term in ("key", "token", "secret", "password")):
                value = parts[1].strip().strip("\"'")
                if len(value) >= 8:
                    values.add(value.encode())
    if paths and not values:
        raise ValueError("No nonempty credential fields found in the explicitly supplied secret files")
    return values


def scan_bytes(name, data, secrets):
    # Return locations and categories only; never include matching values or lines.
    findings = []
    raw_name = name.encode("utf-8")
    name = redact_location(name, secrets)
    if any(secret in raw_name for secret in secrets) or any(pattern.search(raw_name) for pattern in CREDENTIAL_PATTERNS.values()):
        findings.append({"path": name, "kind": "credential_in_path"})
    if any(secret in data for secret in secrets):
        findings.append({"path": name, "kind": "known_secret_value"})
    findings.extend({"path": name, "kind": kind} for kind, pattern in CREDENTIAL_PATTERNS.items()
                    if pattern.search(data))
    return findings


def scan_archive(path, secrets):
    findings = scan_bytes(path.name, b"", secrets)
    members = []
    is_zip = path.suffix.lower() in {".whl", ".zip"}
    sdist_root = next((path.name[:-len(suffix)] for suffix in (".tar.gz", ".tgz", ".tar") if path.name.endswith(suffix)), None)
    if is_zip:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if stat.S_ISLNK(info.external_attr >> 16):
                    findings.append({"path": redact_location(info.filename, secrets), "kind": "archive_link"})
                members.append((info.filename, b"" if info.is_dir() else archive.read(info), info.is_dir()))
    else:
        with tarfile.open(path) as archive:
            for member in archive.getmembers():
                if member.issym() or member.islnk():
                    findings.append({"path":redact_location(member.name, secrets),"kind":"archive_link"})
                if not member.isfile() and not member.isdir() and not member.issym() and not member.islnk():
                    findings.append({"path": redact_location(member.name, secrets), "kind": "unsupported_archive_entry"})
                members.append((member.name, archive.extractfile(member).read() if member.isfile() else b"", member.isdir()))
    seen = set()
    for name, data, is_directory in members:
        member = PurePosixPath(name.replace("\\", "/"))
        safe_name = redact_location(name, secrets)
        relative = PurePosixPath(*member.parts[1:]) if not is_zip else member
        if member.as_posix() in seen:
            findings.append({"path":safe_name,"kind":"duplicate_member"})
        seen.add(member.as_posix())
        if not is_zip and (not member.parts or member.parts[0] != sdist_root or (len(member.parts) < 2 and not is_directory)):
            findings.append({"path":safe_name,"kind":"invalid_sdist_root"})
        if any(part.lower() in PRIVATE_PARTS for part in member.parts) or private_path(relative) or ".." in member.parts or member.is_absolute() or re.match(r"^[A-Za-z]:", name):
            findings.append({"path":safe_name,"kind":"private_or_unsafe_member"})
        elif path.suffix.lower() == ".whl":
            first = member.parts[0] if member.parts else ""
            allowed = (first.endswith(".dist-info") and (is_directory or len(member.parts) > 1)
                       or first == "kong" and (public_directory(PurePosixPath("src") / member) if is_directory else public_path(PurePosixPath("src") / member)))
            if not allowed:
                findings.append({"path":safe_name,"kind":"non_public_member"})
        else:
            allowed = ((not is_zip and relative.as_posix() == ".") or public_directory(relative)) if is_directory else (relative.as_posix() == "PKG-INFO" or public_path(relative))
            if not allowed:
                findings.append({"path":safe_name,"kind":"non_public_member"})
        findings.extend(scan_bytes(name, data, secrets))
    return {"name":redact_location(path.name, secrets),"sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
            "members":sum(not is_directory for _, _, is_directory in members),"entries":len(members),"findings":findings}


def public_directory(path):
    name = path.as_posix()
    return not private_path(path) and any(name == root or root.startswith(name + "/") or name.startswith(root + "/") for root in PUBLIC_DIRECTORIES)


def prepare(root, output, secrets=(), artifacts=()):
    files = public_files(root, secrets)
    findings = [finding for path in files for finding in scan_bytes(path.relative_to(root).as_posix(), path.read_bytes(), secrets)]
    packages = [scan_archive(path, secrets) for path in artifacts]
    if findings or any(p["findings"] for p in packages):
        # Deliberately avoid creating a distributable when the scan fails.
        raise ValueError(json.dumps({"source_findings":findings,"package_findings":packages},ensure_ascii=False))
    output.mkdir(parents=True, exist_ok=False)
    snapshot = output / "source"
    manifest = []
    for path in files:
        relative = path.relative_to(root)
        dest = snapshot / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        manifest.append({"path":relative.as_posix(),"bytes":dest.stat().st_size,
                         "sha256":hashlib.sha256(dest.read_bytes()).hexdigest()})
    archive = output / "kong-agent-public-candidate.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        for item in manifest:
            package.write(snapshot / item["path"], item["path"])
    archive_check = scan_archive(archive, secrets)
    if archive_check["findings"]:
        raise ValueError("Snapshot archive failed the post-copy scan")
    blockers = ["Author must confirm and document copyright and permitted use before publication"]
    report = {"schema_version":1,"created_at":datetime.now(timezone.utc).isoformat(),
              "stage":"review_candidate","published":False,"publication_blockers":blockers,
              "source_files":len(manifest),"source_findings":findings,"known_secret_values_checked":len(secrets),
              "artifacts":packages+[archive_check],"manifest":manifest,
              "scope":"Explicit source snapshot and supplied package bytes only; no Git history or universal secret-detection guarantee."}
    (output / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secret-file", action="append", type=Path, default=[], help="Explicit credential file; known values checked in memory only")
    parser.add_argument("--artifact", action="append", type=Path, default=[], help="Also inspect a wheel, zip or source tarball")
    parser.add_argument("--output", type=Path, help="New output directory (must not exist)")
    args = parser.parse_args()
    output = (args.output or ROOT / "dist" / ("public-candidate-"+uuid4().hex[:12])).resolve()
    if any(output == (ROOT / name).resolve() or output.is_relative_to((ROOT / name).resolve())
           for name in ("src", "tests", "examples", "docs", ".github")):
        parser.error("Output must be outside public source roots")
    secrets = set()
    try:
        secrets = secret_values(args.secret_file)
        report = prepare(ROOT, output, secrets, args.artifact)
    except (ValueError, OSError) as exc:
        parser.exit(2, redact_location(str(exc), secrets)+"\n")
    print(json.dumps({"report":redact_location(str(output / "manifest.json"), secrets),"source_files":report["source_files"],
                      "scan":"passed","publication_blockers":report["publication_blockers"]},ensure_ascii=False))


if __name__ == "__main__":
    main()
