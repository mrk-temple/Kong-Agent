import asyncio

import pytest

from kong.tools.base import ToolCall
from kong.tools.defaults import default_registry
from kong.tools.executor import ToolExecutor
from kong.tools.permissions import PermissionPolicy
from kong.workspace import Workspace


def test_resource_policy_checks_actual_paths_and_denies_traversal(tmp_path):
    (tmp_path / "input").mkdir()
    (tmp_path / "output").mkdir()
    (tmp_path / "input" / "ok.txt").write_text("ok")
    (tmp_path / "other.txt").write_text("outside resource scope")
    workspace = Workspace(tmp_path)
    executor = ToolExecutor(default_registry(workspace), permissions=PermissionPolicy(
        read_roots=["input", "output"], write_roots=["output"]))
    async def scenario():
        for name, args, success in [
            ("read_file", {"path": "input/ok.txt"}, True),
            ("read_file", {"path": "input/../other.txt"}, False),
            ("read_file", {"path": "../outside.txt"}, False),
            ("search_files", {"path": ".", "text": "outside"}, False),
            ("list_dir", {"path": "."}, False),
            ("write_file", {"path": "input/write.txt", "content": "no"}, False),
            ("write_file", {"path": "output/write.txt", "content": "yes"}, True),
            ("write_file", {"path": "output/../../outside.txt", "content": "no"}, False),
        ]:
            result = await executor.execute(ToolCall(name=name, arguments=args))
            assert result.success is success, result
        assert not (tmp_path / "input" / "write.txt").exists()
    asyncio.run(scenario())


def test_operation_policy_allows_browser_snapshot_but_denies_click(tmp_path):
    from kong.integrations.browser import BrowserTool
    from kong.tools.registry import ToolRegistry
    from kong.tools.base import ToolResult
    tool = BrowserTool(Workspace(tmp_path))
    calls = []
    async def fake(**kwargs):
        calls.append(kwargs["operation"])
        return ToolResult(success=True, output={})
    tool.run = fake
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(registry, permissions=PermissionPolicy(operations={"browser": ["snapshot"]}))
    async def scenario():
        assert (await executor.execute(ToolCall(name="browser", arguments={"operation": "snapshot", "page": "p"}))).success
        result = await executor.execute(ToolCall(name="browser", arguments={"operation": "click", "page": "p", "ref": "r"}))
        assert not result.success and not result.outcome_unknown
        assert calls == ["snapshot"]
    asyncio.run(scenario())


def test_resource_policy_cannot_be_bypassed_via_host_process(tmp_path):
    workspace = Workspace(tmp_path)
    executor = ToolExecutor(default_registry(workspace, allow_process=True),
        permissions=PermissionPolicy(write_roots=[]))
    result = asyncio.run(executor.execute(ToolCall(name="process_exec", arguments={
        "argv": ["python", "-c", "from pathlib import Path; Path('bypass.txt').write_text('bad')"]})))
    assert not result.success and not result.outcome_unknown
    assert not (tmp_path / "bypass.txt").exists()


def test_browser_screenshot_obeys_write_roots_and_mcp_calls_obey_method_scope(tmp_path):
    from kong.integrations.browser import BrowserTool
    from kong.integrations.mcp import MCPTool
    (tmp_path / "output").mkdir()
    workspace = Workspace(tmp_path)
    policy = PermissionPolicy(write_roots=["output"], mcp_servers=["approved"], mcp_tools={"approved": ["read"]})
    browser = BrowserTool(workspace)
    assert policy.check(ToolCall(name="browser", arguments={"operation": "screenshot", "path": "outside.png"}), browser)
    assert policy.check(ToolCall(name="browser", arguments={"operation": "screenshot", "path": "output/ok.png"}), browser) is None
    mcp = MCPTool(workspace, {})
    assert policy.check(ToolCall(name="mcp", arguments={"operation": "call", "server": "approved", "tool": "write"}), mcp)
    assert policy.check(ToolCall(name="mcp", arguments={"operation": "call", "server": "approved", "tool": "read"}), mcp) is None


def test_resource_symlink_escape_is_rejected_when_available(tmp_path):
    (tmp_path / "allowed").mkdir()
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("must not read")
    link = tmp_path / "allowed" / "link"
    try:
        link.symlink_to(tmp_path / "outside", target_is_directory=True)
    except OSError:
        pytest.skip("Host does not grant symlink creation")
    workspace = Workspace(tmp_path)
    executor = ToolExecutor(default_registry(workspace), permissions=PermissionPolicy(read_roots=["allowed"]))
    result = asyncio.run(executor.execute(ToolCall(name="read_file", arguments={"path": "allowed/link/secret.txt"})))
    assert not result.success


def test_recursive_reads_recheck_resolved_descendant_resources(tmp_path):
    import os
    import subprocess
    (tmp_path / "allowed").mkdir()
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("scoped secret canary")
    link = tmp_path / "allowed" / "link"
    if os.name == "nt":
        created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(tmp_path / "outside")], capture_output=True)
        assert created.returncode == 0, "Creating a scoped temporary junction failed"
    else:
        link.symlink_to(tmp_path / "outside", target_is_directory=True)
    workspace = Workspace(tmp_path)
    registry = default_registry(workspace)
    executor = ToolExecutor(registry, permissions=PermissionPolicy(read_roots=["allowed"]))
    async def scenario():
        search, listing = await asyncio.gather(
            executor.execute(ToolCall(name="search_files", arguments={"path":"allowed", "text":"scoped secret"})),
            executor.execute(ToolCall(name="list_dir", arguments={"path":"allowed"})))
        assert search.success and search.output["matches"] == []
        assert listing.success and not listing.output
        assert registry.get("search_files").workspace is workspace
        # The same registered tool remains usable by a differently scoped executor.
        wider = ToolExecutor(registry)
        allowed = await wider.execute(ToolCall(name="read_file", arguments={"path":"allowed/link/secret.txt"}))
        assert allowed.success
    asyncio.run(scenario())


def test_patch_requires_read_and_write_authority(tmp_path):
    (tmp_path / "output").mkdir()
    target = tmp_path / "output" / "value.txt"
    target.write_text("old")
    workspace = Workspace(tmp_path)
    executor = ToolExecutor(default_registry(workspace), permissions=PermissionPolicy(read_roots=[], write_roots=["output"]))
    result = asyncio.run(executor.execute(ToolCall(name="patch_file", arguments={"path":"output/value.txt", "old_text":"old", "new_text":"new"})))
    assert not result.success and not result.outcome_unknown
    assert target.read_text() == "old"
