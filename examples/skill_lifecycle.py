"""Host-only management of a skill candidate. Never invokes a model or runs a skill script."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

def main(argv=None):
    from kong.tools.defaults import default_registry
    from kong.workspace import Workspace
    parser = argparse.ArgumentParser(description="Register, validate, select or disable a content-addressed skill")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--skill-dir", type=Path, action="append", default=[])
    parser.add_argument("--name", required=True)
    parser.add_argument("--fingerprint", help="Explicit older identity for disable/rollback; does not restore files")
    parser.add_argument("--operation", choices=["show", "register", "record", "activate", "disable", "retire"], default="show")
    parser.add_argument("--registration", type=Path, help="Scope, required_tasks, contract_version and source_trials JSON")
    parser.add_argument("--evidence", type=Path, help="Host verifier JSON with task_id, contract_version, passed and source_trial")
    parser.add_argument("--reason", default="")
    args = parser.parse_args(argv)
    workspace = Workspace(args.workspace)
    registry = default_registry(workspace, skill_roots=args.skill_dir)
    catalog = registry.get("skill_load").catalog
    skill = catalog.get(args.name)
    identity = catalog.identity(skill)
    fingerprint = args.fingerprint or identity["fingerprint"]
    ledger = catalog.lifecycle
    if args.operation == "register":
        if not args.registration or args.fingerprint:
            parser.error("Register current resources with --registration; no older fingerprint")
        data = json.loads(args.registration.read_text(encoding="utf-8-sig"))
        revision = ledger.register(skill_id=skill.id, **identity, **data)
    elif args.operation == "record":
        if not args.evidence or fingerprint != identity["fingerprint"]:
            parser.error("Record only for the current matching resources, with --evidence")
        raw = args.evidence.read_bytes()
        if len(raw) > 2_000_000:
            parser.error("Evidence file exceeds its bound")
        data = json.loads(raw.decode("utf-8-sig"))
        data["evidence_sha256"] = hashlib.sha256(raw).hexdigest()
        revision = ledger.record(skill.id, fingerprint, data)
    elif args.operation in {"activate", "disable", "retire"}:
        revision = ledger.select(skill.id, fingerprint, args.operation, reason=args.reason)
    else:
        revision = ledger.get(skill.id, fingerprint)
    print(json.dumps({"skill_id": skill.id, "current_identity": identity,
        "revision": revision.model_dump(mode="json") if revision else None,
        "authority": "Host bookkeeping only; validation records are not model-effect or permission grants"},
        ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
