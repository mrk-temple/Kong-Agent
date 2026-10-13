"""Freeze public executable source without requiring private development evidence."""

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = ROOT / "src/kong/local_api/source_identity.json"


def source_manifest():
    paths = set()
    for directory, suffixes in [
        ("src/kong", {".py", ".js", ".css", ".html", ".json", ".md"}),
        ("tests", {".py", ".json"}),
        ("examples", {".py", ".toml"}),
        ("web/src", {".ts", ".tsx", ".css"}),
        ("scripts", {".ps1"}),
    ]:
        for path in (ROOT / directory).rglob("*"):
            if (
                path.is_file()
                and path.suffix in suffixes
                and "__pycache__" not in path.parts
            ):
                paths.add(path)
    for name in [
        "pyproject.toml",
        "uv.lock",
        ".gitignore",
        ".gitattributes",
        "web/package.json",
        "web/package-lock.json",
        "web/tsconfig.json",
        "web/vite.config.ts",
        "web/index.html",
    ]:
        if (ROOT / name).is_file():
            paths.add(ROOT / name)
    paths.discard(IDENTITY)
    files = [
        {
            "path": path.relative_to(ROOT).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }
        for path in sorted(paths, key=lambda p: p.relative_to(ROOT).as_posix())
    ]
    canonical = "\n".join(item["path"] + "\t" + item["sha256"] for item in files)
    fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
    return {
        "product": "Kong Local Preview 0.0.0",
        "runtime_version": "0.5.0rc1",
        "fingerprint_sha256": fingerprint,
        "file_count": len(files),
        "exclusions": [
            "public prose and screenshots",
            "private notes and runtime evidence",
            "secrets and data",
            "venv node_modules bytecode",
            "derived local_api/source_identity.json",
        ],
        "files": files,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/source-manifest.json")
    parser.add_argument("--check", action="store_true", help="Check the bundled identity without writing files")
    args = parser.parse_args()
    manifest = source_manifest()
    identity = {key: manifest[key] for key in ("fingerprint_sha256", "file_count")}
    if args.check:
        if not IDENTITY.exists() or json.loads(IDENTITY.read_text(encoding="utf-8")) != identity:
            parser.exit(1, "Bundled source identity differs; rebuild the frontend and freeze the source again.\n")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        IDENTITY.write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(identity))


if __name__ == "__main__":
    main()
