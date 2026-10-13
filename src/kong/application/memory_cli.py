"""Explicit local owner management; never loaded from model/import authority."""

import json
from pathlib import Path
from uuid import uuid4

from kong.memory import (
    Authority,
    Bottle,
    Capability,
    Identity,
    MemoryDraft,
    MemoryService,
    Scope,
    Source,
    identity_digest,
    parse_package,
    Relationship,
    relationship_digest,
)
from kong.memory.exchange import MAX_PACKAGE_BYTES


def add_parser(commands):
    core = commands.add_parser(
        "memory-core",
        help="管理独立 Profile、Identity 和记忆瓶，不调用模型",
        epilog="遗忘为部分支持：清理 Core 内容并隔离受覆盖的模型上下文；不删除原始 Runtime 日志、用户导出或备份。",
    )
    core.add_argument(
        "action",
        choices=[
            "profile-create",
            "identity-set",
            "remember",
            "revise",
            "records",
            "forget-preview",
            "forget",
            "bottle-create",
            "bottles",
            "bottle-add",
            "bottle-remove",
            "bottle-memories",
            "bottle-on",
            "bottle-off",
            "export",
            "import-preview",
            "import",
            "observations",
            "formation-queue",
            "formation-scan",
            "formation-run",
            "candidates",
            "consolidation-preview",
            "consolidate",
            "pin",
            "unpin",
            "relationship-show",
            "relationship-set",
            "proposal-from-candidate",
            "proposals",
            "proposal-show",
            "proposal-approve",
            "proposal-reject",
            "proposal-rollback-preview",
            "proposal-rollback",
            "reflection-run",
            "reflection-report",
        ],
    )
    core.add_argument("--id", dest="object_id")
    core.add_argument("--bottle")
    core.add_argument("--title", default="Memories")
    core.add_argument("--kind", choices=["semantic", "episodic"], default="semantic")
    core.add_argument("--text")
    core.add_argument("--identity", type=Path, help="用户明确批准的 Identity JSON")
    core.add_argument(
        "--relationship", type=Path, help="用户明确批准的 Relationship JSON"
    )
    core.add_argument("--revision", type=int)
    core.add_argument("--approved-plan-digest")
    core.add_argument("--command-id")
    core.add_argument("--file", type=Path, help="版本化 Memory Bottle JSON 文件")
    core.add_argument(
        "--select", action="append", help="只选择指定的来源 Record ID；可重复"
    )
    core.add_argument("--approved-digest", help="import-preview 返回的选择与状态摘要")
    core.add_argument("--max-jobs", type=int, default=10)
    core.add_argument("--resolve-conflict", action="store_true")
    core.add_argument("--max-records", type=int, default=20)
    core.add_argument("--max-proposals", type=int, default=4)
    core.add_argument("--max-seconds", type=float, default=2)
    core.add_argument(
        "--proposal-state",
        choices=["pending", "approved", "rejected", "revoked"],
        default="pending",
    )


def host_authority(args, *, input_id=None):
    if not args.memory_core_db or not args.kong_profile:
        raise ValueError("Memory Core requires --memory-core-db and --kong-profile")
    scopes = [Scope(), Scope(kind="workspace", id=str(args.workspace.resolve()))]
    if args.project_id:
        scopes.append(Scope(kind="project", id=args.project_id))
    return Authority(
        owner_id=args.memory_owner,
        profile_id=args.kong_profile,
        scopes=tuple(scopes),
        capabilities=frozenset(Capability),
        input_id=input_id,
    )


def run(args):
    command = args.command_id or uuid4().hex
    if len(command) > 180:
        raise ValueError("Command ID exceeds 180 characters")
    auth = host_authority(args, input_id=command + ":input")
    with MemoryService(args.memory_core_db) as core:
        action = args.action
        if action == "profile-create":
            result = core.create_profile(auth, command_id=command)
        elif action == "identity-set":
            if args.identity is None or args.revision is None:
                raise ValueError("Identity requires --identity and --revision")
            identity = Identity.model_validate_json(
                args.identity.read_text(encoding="utf-8-sig")
            )
            result = core.set_identity(
                auth,
                identity,
                expected_revision=args.revision,
                approved_digest=identity_digest(identity),
                command_id=command,
            )
        elif action == "remember":
            if not args.text or not args.bottle:
                raise ValueError("Remember requires --text and --bottle")
            bottle = next(
                (b for b in core.list_bottles(auth) if b.bottle_id == args.bottle), None
            )
            if (
                bottle is None
                or bottle.kind != "memory"
                or bottle.policy.scope != Scope()
            ):
                raise ValueError(
                    "Select an existing Profile-scoped Memory Bottle before saving"
                )
            result = core.remember(
                auth,
                MemoryDraft(kind=args.kind, content=args.text),
                Source(
                    source_id=auth.input_id,
                    origin="user_command",
                    speaker="user",
                    excerpt=args.text,
                ),
                command_id=command,
            )
            core.add_memory_to_bottle(
                auth, args.bottle, result.record_id, command_id=command + ":membership"
            )
        elif action == "revise":
            if not args.object_id or not args.text or args.revision is None:
                raise ValueError("Revise requires --id, --text and --revision")
            result = core.revise_memory(
                auth,
                args.object_id,
                args.text,
                Source(
                    source_id=auth.input_id,
                    origin="user_command",
                    speaker="user",
                    excerpt=args.text,
                ),
                expected_revision=args.revision,
                command_id=command,
            )
        elif action == "records":
            result = core.list_records(auth)
        elif action == "relationship-show":
            version = core.get_relationship(auth, version=args.revision)
            result = version if version else {"relationship": None}
        elif action == "relationship-set":
            if args.relationship is None or args.revision is None:
                raise ValueError("Relationship requires --relationship and --revision")
            relationship = Relationship.model_validate_json(
                args.relationship.read_text(encoding="utf-8-sig")
            )
            result = core.set_relationship(
                auth,
                relationship,
                expected_revision=args.revision,
                approved_digest=relationship_digest(relationship),
                command_id=command,
            )
        elif action == "reflection-run":
            from kong.memory import ReflectionBudget

            result = core.run_reflection(
                auth,
                command_id=command,
                budget=ReflectionBudget(
                    max_records=args.max_records,
                    max_proposals=args.max_proposals,
                    max_seconds=args.max_seconds,
                ),
            )
        elif action == "proposals":
            result = core.list_proposals(auth, state=args.proposal_state)
        elif action in {
            "proposal-from-candidate",
            "proposal-show",
            "proposal-approve",
            "proposal-reject",
            "proposal-rollback-preview",
            "proposal-rollback",
            "reflection-report",
        }:
            if not args.object_id:
                raise ValueError("Evolution/reflection command requires --id")
            if action == "proposal-from-candidate":
                result = core.proposal_from_candidate(
                    auth, args.object_id, command_id=command
                )
            elif action == "proposal-show":
                result = core.get_proposal(auth, args.object_id)
            elif action == "reflection-report":
                result = core.get_reflection_report(auth, args.object_id)
            elif action == "proposal-rollback-preview":
                result = core.preview_rollback_proposal(auth, args.object_id)
            elif action == "proposal-rollback":
                if not args.approved_digest:
                    raise ValueError("Rollback requires --approved-digest from preview")
                result = core.rollback_proposal(
                    auth,
                    args.object_id,
                    approved_digest=args.approved_digest,
                    command_id=command,
                )
            else:
                if args.revision is None:
                    raise ValueError(
                        "Evolution review requires --revision from proposal-show"
                    )
                if action == "proposal-reject":
                    result = core.reject_proposal(
                        auth,
                        args.object_id,
                        expected_revision=args.revision,
                        command_id=command,
                    )
                else:
                    if not args.approved_digest:
                        raise ValueError(
                            "Evolution approval requires --approved-digest from proposal-show"
                        )
                    result = core.approve_proposal(
                        auth,
                        args.object_id,
                        expected_revision=args.revision,
                        approved_digest=args.approved_digest,
                        command_id=command,
                    )
        elif action == "observations":
            result = {"observations": core.list_observations(auth)}
        elif action == "formation-run":
            from kong.memory import FormationBudget

            result = core.run_formation(
                auth, budget=FormationBudget(max_jobs=args.max_jobs)
            )
        elif action == "formation-scan":
            result = core.queue_observations(
                auth, bottle_id=args.bottle, limit=args.max_jobs
            )
        elif action == "candidates":
            result = core.list_formation_candidates(auth)
        elif action in {
            "formation-queue",
            "consolidation-preview",
            "consolidate",
            "pin",
            "unpin",
        }:
            if not args.object_id:
                raise ValueError("Formation/review requires --id")
            if action == "formation-queue":
                result = {
                    "job_id": core.queue_formation(
                        auth, args.object_id, bottle_id=args.bottle
                    )
                }
            elif action == "consolidation-preview":
                result = core.preview_consolidation(auth, args.object_id)
            elif action == "consolidate":
                if not args.approved_digest:
                    raise ValueError(
                        "Consolidation requires --approved-digest from preview"
                    )
                result = core.consolidate_candidate(
                    auth,
                    args.object_id,
                    approved_digest=args.approved_digest,
                    command_id=command,
                    resolve_conflict=args.resolve_conflict,
                )
            else:
                if not args.bottle:
                    raise ValueError("Cherishing requires --bottle")
                result = core.pin_memory(
                    auth,
                    args.object_id,
                    args.bottle,
                    command_id=command,
                    pinned=action == "pin",
                )
        elif action == "bottle-create":
            result = core.create_bottle(
                auth,
                Bottle(
                    bottle_id=args.object_id or uuid4().hex,
                    profile_id=auth.profile_id,
                    title=args.title,
                ),
                command_id=command,
            )
        elif action == "bottles":
            result = core.list_bottles(auth)
        elif action in {"import-preview", "import"}:
            if args.file is None:
                raise ValueError("Import requires --file")
            with args.file.open("rb") as stream:
                package = parse_package(stream.read(MAX_PACKAGE_BYTES + 1))
            if action == "import-preview":
                result = core.preview_import(
                    auth, package, selected_records=args.select
                )
            else:
                if not args.approved_digest:
                    raise ValueError(
                        "Import requires --approved-digest from import-preview"
                    )
                result = core.import_bottle(
                    auth,
                    package,
                    selected_records=args.select,
                    approved_digest=args.approved_digest,
                    bottle_id=args.object_id,
                    command_id=command,
                )
        elif action == "export":
            if not args.object_id or args.file is None:
                raise ValueError("Export requires --id Bottle and --file")
            package = core.export_bottle(
                auth, args.object_id, selected_records=args.select
            )
            # Avoid silently overwriting another user-owned export.
            with args.file.open("x", encoding="utf-8") as stream:
                stream.write(package.model_dump_json(indent=2))
            result = {
                "file": str(args.file),
                "records": len(package.records),
                "checksum": package.checksum,
            }
        else:
            if not args.object_id:
                raise ValueError("This command requires --id")
            if action == "forget-preview":
                result = core.preview_forget(auth, args.object_id)
            elif action == "forget":
                if args.revision is None:
                    raise ValueError(
                        "Forget requires --revision; inspect forget-preview first"
                    )
                result = core.forget_memory(
                    auth,
                    args.object_id,
                    expected_revision=args.revision,
                    command_id=command,
                    approved_plan_digest=args.approved_plan_digest,
                )
                result = {
                    **result.model_dump(mode="json"),
                    "forgetting_support": "partial",
                    "retained": [
                        "raw_runtime_history",
                        "user_exports",
                        "backups",
                        "provider_requests_already_sent",
                    ],
                }
            elif action == "bottle-memories":
                result = core.list_bottle_memories(auth, args.object_id)
            elif action in {"bottle-add", "bottle-remove"}:
                if not args.bottle:
                    raise ValueError("Membership requires --bottle and --id record")
                operation = (
                    core.add_memory_to_bottle
                    if action == "bottle-add"
                    else core.remove_memory_from_bottle
                )
                result = operation(
                    auth, args.bottle, args.object_id, command_id=command
                )
            else:
                if args.revision is None:
                    raise ValueError("Activation requires --revision from bottles")
                operation = (
                    core.activate_bottle
                    if action == "bottle-on"
                    else core.deactivate_bottle
                )
                result = operation(
                    auth,
                    args.object_id,
                    expected_revision=args.revision,
                    command_id=command,
                )
    data = (
        [value.model_dump(mode="json") for value in result]
        if isinstance(result, tuple)
        else result
        if isinstance(result, dict)
        else result.model_dump(mode="json")
    )
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0
