from dataclasses import dataclass
from importlib import metadata
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from kong.continuity.compiler import item
from kong.continuity.contracts import ContextSource
from kong.workspace import Workspace
from kong.skills.lifecycle import SkillLifecycle


class Requirements(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tools: list[str] = Field(default_factory=list, max_length=30)
    python: list[str] = Field(default_factory=list, max_length=30)
    binaries: list[str] = Field(default_factory=list, max_length=30)
    optional_binaries: list[str] = Field(default_factory=list, max_length=30)
    files: list[str] = Field(default_factory=list, max_length=60)

    @field_validator("tools", "python", "binaries", "optional_binaries")
    @classmethod
    def dependency_names(cls, values):
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", value) for value in values):
            raise ValueError("Dependency names must be nonempty package/tool/command names")
        return values


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    description: str
    root: Path
    body: str
    requires: Requirements
    warnings: tuple[str, ...] = ()
    version: str = "unversioned"
    managed: bool = False
    entry_sha256: str = ""


def bounded_bytes(path: Path, limit=48000):
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"Resource exceeds {limit} bytes")
    return raw


def bounded_text(path: Path, limit=48000):
    return bounded_bytes(path, limit).decode("utf-8-sig")


def resource_path(root: Path, resource: str) -> Path:
    value = PurePosixPath(resource)
    if not resource or "\\" in resource or ":" in resource or value.is_absolute() or ".." in value.parts:
        raise ValueError("Resource must be a relative path inside this skill")
    target = (root / resource).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("Resource escapes this skill")
    return Workspace(root).resolve(resource)


class SkillCatalog:
    def __init__(self, workspace: Path, tools, extra_roots=()):
        self.tools = tools
        self.roots = [("builtin", Path(__file__).parent / "builtin"),
                      ("project", workspace / ".agents" / "skills")]
        self.roots.extend((f"extra{i}", Path(root).expanduser()) for i, root in enumerate(extra_roots))
        self.skills: dict[str, Skill] = {}
        self.diagnostics = []
        self.lifecycle = SkillLifecycle(workspace / ".kong" / "skill-lifecycle.json")
        self._pinned = {}
        self._run_id = None
        self.refresh()

    def refresh(self):
        self.skills, self.diagnostics = {}, []
        for source, folder in self.roots:
            root = folder.resolve()
            if not root.exists():
                continue
            try:
                # Flat bundles only; bounds are explicit, and omitted bundles are diagnosed.
                folders = sorted(root.iterdir(), key=lambda p: p.name.casefold())
                if len(folders) > 128:
                    self.diagnostics.append(f"{source}: directory entry limit 128 reached")
                for directory in folders[:128]:
                    if not directory.is_dir():
                        continue
                    try:
                        base = directory.resolve()
                        if not base.is_relative_to(root):
                            raise ValueError("Skill directory escapes configured root")
                        entry = resource_path(base, "SKILL.md")
                        if not entry.exists():
                            continue
                        raw_entry = bounded_bytes(entry)
                        text = raw_entry.decode("utf-8-sig")
                        lines = text.splitlines()
                        if not lines or lines[0] != "---":
                            raise ValueError("Missing YAML frontmatter")
                        end = lines.index("---", 1)
                        header = "\n".join(lines[1:end])
                        if any(isinstance(t, yaml.tokens.AliasToken) for t in yaml.scan(header)):
                            raise ValueError("YAML aliases are not supported")
                        data = yaml.safe_load(header)
                        if not isinstance(data, dict):
                            raise ValueError("Frontmatter must be a mapping")
                        name, description = data.get("name"), data.get("description")
                        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
                            raise ValueError("Invalid skill name")
                        if not isinstance(description, str) or not description.strip() or len(description) > 1024:
                            raise ValueError("description must contain 1–1024 characters")
                        body = "\n".join(lines[end + 1:]).strip()
                        if not body or len(body) > 16000:
                            raise ValueError("Skill body must contain 1–16000 characters")
                        meta = data.get("metadata") or {}
                        if not isinstance(meta, dict):
                            raise ValueError("metadata must be a mapping")
                        req = Requirements.model_validate(meta.get("kong", {}))
                        version = meta.get("version", "unversioned")
                        if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", version):
                            raise ValueError("Invalid skill version")
                        managed = "lifecycle" in meta
                        if managed and meta["lifecycle"] != "candidate":
                            raise ValueError("A package may only declare candidate status; activation belongs to the host ledger")
                        referenced = re.findall(r"(?:references|scripts|assets)/[\w./-]+", body)
                        req.files = sorted(set(req.files + [p.rstrip('.') for p in referenced]))
                        for relative in req.files:
                            resource_path(base, relative)
                        warnings = tuple(f"Unsupported frontmatter field has no runtime effect: {key}" for key in data
                                         if key not in {"name", "description", "metadata", "license", "compatibility"})
                        skill = Skill(f"{source}:{name}", name, description.strip(), base, body, req, warnings,
                                      version, managed, hashlib.sha256(raw_entry).hexdigest())
                        if skill.id in self.skills:
                            raise ValueError("Duplicate name within skill source")
                        self.skills[skill.id] = skill
                    except (ValueError, OSError, UnicodeError, yaml.YAMLError, RecursionError) as exc:
                        self.diagnostics.append(f"{source}/{directory.name}: {type(exc).__name__}: {str(exc)[:250]}")
            except OSError as exc:
                self.diagnostics.append(f"{source}: {type(exc).__name__}")

    def get(self, name):
        if name in self.skills:
            return self.skills[name]
        matches = [s for s in self.skills.values() if s.name == name]
        if len(matches) != 1:
            raise ValueError("Unknown or ambiguous skill; use a qualified ID from skill_list")
        return matches[0]

    def health(self, skill):
        missing = [f"tool:{name}" for name in skill.requires.tools if name not in self.tools.names()]
        for name in skill.requires.python:
            try:
                metadata.version(name)
            except metadata.PackageNotFoundError:
                missing.append(f"python:{name}")
        missing += [f"binary:{name}" for name in skill.requires.binaries if not shutil.which(name)]
        for name in skill.requires.files:
            try:
                if not resource_path(skill.root, name).is_file():
                    missing.append(f"file:{name}")
            except (ValueError, OSError):
                missing.append(f"file:{name}")
        optional = [f"binary:{name}" for name in skill.requires.optional_binaries if not shutil.which(name)]
        return {"status": "blocked" if missing else "partial" if optional else "ready",
                "missing": missing, "optional_missing": optional,
                "warnings": list(skill.warnings),
                "note": "Installed distribution/command presence only; not a runtime or visual-quality guarantee."}

    def listing(self, offset=0, limit=20):
        all_skills = sorted(self.skills.values(), key=lambda s: s.id)
        return {"skills": [{"id": s.id, "description": s.description[:180], "version": s.version,
                            "lifecycle": "managed" if s.managed else "legacy", **self.health(s)}
                           for s in all_skills[offset:offset + limit]],
                "total": len(all_skills), "next_offset": offset + limit if offset + limit < len(all_skills) else None,
                "diagnostics": self.diagnostics[:20]}

    def identity(self, skill):
        hashes = {}
        for name in ["SKILL.md", *skill.requires.files]:
            path = resource_path(skill.root, name)
            if not path.is_file():
                hashes[name] = None
                continue
            with path.open("rb") as stream:
                raw = stream.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("Skill resource exceeds identity bound")
            hashes[name] = hashlib.sha256(raw).hexdigest()
        fingerprint = hashlib.sha256(json.dumps({"version": skill.version, "resources": hashes},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if skill.entry_sha256 and hashes["SKILL.md"] != skill.entry_sha256:
            raise ValueError("Skill source changed during discovery; refresh and inspect the current version")
        return {"version": skill.version, "fingerprint": fingerprint, "resource_hashes": hashes}

    def _check_revision(self, skill, identity, *, allow_candidate=False):
        pinned = self._pinned.get(skill.id)
        if pinned is not None and pinned != identity["fingerprint"]:
            raise ValueError("Loaded skill version changed; start a new Run or restore the original resources")
        record = self.lifecycle.get(skill.id, identity["fingerprint"])
        status = record.status if record else "candidate" if skill.managed else "legacy"
        if status in {"disabled", "retired"} or status in {"candidate", "validated"} and not allow_candidate:
            raise ValueError("Skill revision is not active; host validation and explicit selection are required")
        return status

    def load(self, name, *, allow_candidate=False):
        self.refresh()
        skill = self.get(name)
        identity = self.identity(skill)
        status = self._check_revision(skill, identity, allow_candidate=allow_candidate)
        self._pinned.setdefault(skill.id, identity["fingerprint"])
        return {"id": skill.id, "resource": "SKILL.md", "root": str(skill.root),
                "instructions": skill.body, "resources": skill.requires.files, **self.health(skill),
                **identity, "lifecycle": status,
                "authority": "Task guidance only; never grants tools, approvals, credentials or execution."}

    def read(self, name, resource, offset=0, max_chars=8000):
        self.refresh()
        skill = self.get(name)
        identity = self.identity(skill)
        self._check_revision(skill, identity)
        if skill.id in self._pinned and resource not in identity["resource_hashes"]:
            raise ValueError("Resource is not part of the pinned skill revision")
        path = resource_path(skill.root, resource)
        raw = bounded_bytes(path, 128000)
        if resource in identity["resource_hashes"] and hashlib.sha256(raw).hexdigest() != identity["resource_hashes"][resource]:
            raise ValueError("Skill resource changed during read")
        text = raw.decode("utf-8-sig")
        return {"id": skill.id, "resource": resource, "root": str(skill.root), **identity,
                "content": text[offset:offset + max_chars], "offset": offset,
                "truncated": offset + max_chars < len(text), "size": len(text)}

    def context_items(self, history, run_id):
        if self._run_id != run_id:
            self._run_id, self._pinned = run_id, {}
        listing = self.listing(limit=12)
        brief = {"skills": [{"id": s["id"], "description": s["description"][:110], "status": s["status"]}
                            for s in listing["skills"]], "total": listing["total"], "next_offset": listing["next_offset"]}
        result = [item("skill_catalog", "SKILL CATALOG (metadata only; load instructions before use):\n" +
                       json.dumps(brief, ensure_ascii=False, separators=(",", ":")), 1, [ContextSource(kind="tools", path="skill_catalog")])]
        # Immutable content from successful loads in this Run, never from another Run's memory.
        active = {}
        for index, event in enumerate(history.events):
            record = event.payload.get("skill_result") if event.type == "feedback" else None
            if not record or not record.get("success") or not isinstance(record.get("output"), dict):
                continue
            output = record["output"]
            if "id" not in output or "resource" not in output:
                continue
            if output.get("fingerprint"):
                self._pinned.setdefault(output["id"], output["fingerprint"])
            key = (output["id"], output["resource"], output.get("offset", 0))
            active.pop(key, None)
            active[key] = (index, output)
        for index, output in list(active.values())[-4:]:
            if output.get("fingerprint"):
                try:
                    revision = self.lifecycle.get(output["id"], output["fingerprint"])
                    if revision is not None and revision.status in {"disabled", "retired"}:
                        raise ValueError("Pinned revision was disabled by the host")
                    if revision is None and output.get("lifecycle") == "active":
                        raise ValueError("Managed activation record is unavailable")
                except (ValueError, OSError):
                    result.append(item("runtime_feedback", "Loaded skill " + output["id"]
                        + " is disabled or its activation record is unavailable. Its material is withdrawn at this decision boundary. "
                        "Do not rely on it or replay prior actions; ask the host to select a valid version.", 0,
                        [ContextSource(kind="tools", path="skill_lifecycle")]))
                    continue
            result.append(item("active_skill", "LOADED SKILL MATERIAL (guidance, no execution authority):\n" +
                json.dumps(output, ensure_ascii=False), 0,
                [ContextSource(kind="event", run_id=run_id, event_index=index,
                               event_id=f"{run_id}:event:{index}", path="skill_result")]))
        return result
