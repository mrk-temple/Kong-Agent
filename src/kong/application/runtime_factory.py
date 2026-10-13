"""Reuse the existing Runtime, tools, storage, permission policy and read bridge."""

from pathlib import Path
from kong.application.memory_bridge import MemoryBridge
from kong.continuity.storage import ThreadStore
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.tools.permissions import PermissionPolicy
from kong.workspace import Workspace
from kong.contracts import Mode


def create_runtime(*args, **kwargs):
    return Runtime.new(*args, **kwargs)


def restore_runtime(*args, **kwargs):
    return Runtime.restore(*args, **kwargs)


class RuntimeFactory:
    def __init__(self, root, authority):
        self.root = Path(root).resolve()
        work = self.root / "workspace"
        work.mkdir(parents=True, exist_ok=True)
        self.workspace = Workspace(work)
        self.store = RunStore(self.root / "runs")
        self.threads = ThreadStore(self.root / "threads.db")
        self.authority = authority
        self.bridges = []
        self.permissions = PermissionPolicy(
            allowed_tools=[
                "list_dir",
                "read_file",
                "search_files",
                "write_file",
                "patch_file",
                "recall_history",
                "skill_list",
                "skill_load",
                "skill_read",
            ]
        )
        self.registries = []

    def bridge(self):
        bridge = MemoryBridge(self.root / "core.db", self.authority)
        self.bridges.append(bridge)
        return bridge

    def registry(self):
        registry = default_registry(self.workspace)
        self.registries.append(registry)
        return registry

    def new(self, body, thread_id, model, events):
        runtime = create_runtime(
            body.text,
            model,
            self.workspace,
            self.registry(),
            Mode(body.mode),
            store=self.store,
            thread_store=self.threads,
            thread_id=thread_id,
            memory_bridge=self.bridge(),
            memory_capture=False,
            permissions=self.permissions,
            model_retry_limit=0,
            on_event=events,
            completion_contract=body.completion_contract,
        )
        runtime.state.success_criteria = body.acceptance
        runtime.save()
        return runtime

    def restore(self, run_id, model, events):
        return restore_runtime(
            self.store.load(run_id),
            model,
            self.workspace,
            self.registry(),
            store=self.store,
            thread_store=self.threads,
            memory_bridge=self.bridge(),
            memory_capture=False,
            permissions=self.permissions,
            model_retry_limit=0,
            on_event=events,
        )

    async def close(self):
        for registry in self.registries:
            await registry.aclose()
        for bridge in self.bridges:
            await bridge.aclose()
