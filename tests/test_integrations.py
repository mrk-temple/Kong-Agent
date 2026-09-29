import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
import threading
import socket

import pytest

from kong.integrations.config import MCPServer, load_servers
from kong.tools.base import ToolCall
from kong.tools.defaults import default_registry
from kong.tools.executor import ToolExecutor
from kong.workspace import Workspace


def test_opt_in_and_config(tmp_path):
    registry = default_registry(Workspace(tmp_path))
    assert not {"browser", "terminal", "mcp"} & set(registry.names())
    with pytest.raises(ValueError):
        MCPServer(transport="http", url="https://user:secret@example.com/mcp")
    config = tmp_path / "config.toml"
    config.write_text('[mcp.servers.demo]\ncommand="python"\nargs=["server.py"]\n')
    assert load_servers(config)["demo"].args == ["server.py"]


@pytest.mark.parametrize("name,args", [("terminal", {"operation":"start","argv":["python"]}),
    ("browser", {"operation":"open","url":"https://example.com"}), ("mcp", {"operation":"servers"})])
def test_discovery_cannot_launch_integrations(tmp_path, name, args):
    from kong.contracts import NeedDiscovery, Action
    from kong.models.fake import ScriptedModel
    from kong.runtime.loop import Runtime
    workspace = Workspace(tmp_path)
    registry = default_registry(workspace, allow_terminal=True, allow_browser=True, mcp_servers={})
    runtime = Runtime.new("inspect", ScriptedModel([]), workspace, registry)
    asyncio.run(runtime._actions(NeedDiscovery(reason="Inspect available tools", actions=[Action(name=name, arguments=args)])))
    assert not runtime.history.observations
    assert "read-only" in runtime.history.events[-1].payload["error"]


@pytest.mark.skipif(os.name != "nt", reason="Windows ConPTY adapter")
def test_terminal_interaction_and_cleanup(tmp_path):
    pytest.importorskip("winpty")
    async def scenario():
        registry = default_registry(Workspace(tmp_path), allow_terminal=True)
        executor = ToolExecutor(registry)
        async def call(**args):
            return await executor.execute(ToolCall(name="terminal", arguments=args))
        try:
            result = await call(operation="start", argv=["python", "-u", "-c",
                "from pathlib import Path; x=input('Name? '); Path('answer.txt').write_text(x); print('HELLO '+x)"], wait_seconds=0.5)
            assert result.success, result
            session = result.output["session"]
            result = await call(operation="write", session=session, text="Kong\r", wait_seconds=1)
            for _ in range(15):
                if not result.output["running"]:
                    break
                result = await call(operation="read", session=session, wait_seconds=0.2)
            assert result.success and result.output["exit_code"] == 0, result
            assert (tmp_path / "answer.txt").read_text() == "Kong"
            assert "HELLO Kong" in result.output["output"]
            bad = await call(operation="write", session=session, text="again\r")
            assert not bad.success
            await call(operation="close", session=session)
            result = await call(operation="start", argv=["python", "-c", "import time; time.sleep(60)"], wait_seconds=0.1)
            active = registry.get("terminal").sessions[result.output["session"]]
            await registry.aclose()
            assert active.closed and not active.process.isalive()
            assert not (await call(operation="read", session=session)).success
        finally:
            await registry.aclose()
    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "nt", reason="Windows ConPTY adapter")
def test_terminal_expiry_and_bounded_output(tmp_path):
    pytest.importorskip("winpty")
    async def scenario():
        registry = default_registry(Workspace(tmp_path), allow_terminal=True)
        tool = registry.get("terminal")
        try:
            result = await tool.run(operation="start", argv=["python", "-u", "-c",
                "import time; print('x'*200000); time.sleep(60)"], lifetime_seconds=1, wait_seconds=0.1)
            key = result.output["session"]
            await asyncio.sleep(1.8)
            result = await tool.run(operation="read", session=key)
            assert not result.success and result.output["expired"], result
            assert not result.output["running"]
            assert len(tool.sessions[key].data) <= 64000
            assert len(result.output["output"]) <= 16000
        finally:
            await registry.aclose()
    asyncio.run(scenario())


def test_mcp_discovery_schema_and_call(tmp_path):
    pytest.importorskip("mcp")
    script = tmp_path / "server.py"
    script.write_text('from mcp.server.fastmcp import FastMCP\nm=FastMCP("test")\n'
        '@m.tool()\ndef add(a:int,b:int)->int:\n return a+b\nm.run()\n')
    async def scenario():
        registry = default_registry(Workspace(tmp_path), mcp_servers={"demo": MCPServer(command="python", args=[str(script)])})
        executor = ToolExecutor(registry)
        async def call(**args):
            return await executor.execute(ToolCall(name="mcp", arguments=args))
        try:
            result = await call(operation="list", server="demo")
            assert result.success, result
            assert result.output["tools"][0]["name"] == "add"
            bad = await call(operation="call", server="demo", tool="add", arguments={"a": "bad", "b": 2})
            assert not bad.success
            result = await call(operation="call", server="demo", tool="add", arguments={"a": 19, "b": 23})
            assert result.success and result.output["structured_content"] == {"result": 42}, result
            assert not (await call(operation="call", server="unknown", tool="add")).success
        finally:
            await registry.aclose()
        assert not registry.get("mcp").connections
    asyncio.run(scenario())


def test_browser_real_form_and_stale_refs(tmp_path):
    pytest.importorskip("playwright")
    (tmp_path / "index.html").write_text('<!doctype html><title>Kong test</title><label>Name<input id="name"></label>'
        '<button onclick="document.querySelector(\'output\').textContent=\'Saved: \'+document.querySelector(\'input\').value">Save</button><output></output>')
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    async def scenario():
        registry = default_registry(Workspace(tmp_path), allow_browser=True)
        executor = ToolExecutor(registry)
        async def call(**args):
            return await executor.execute(ToolCall(name="browser", arguments=args))
        try:
            result = await call(operation="open", url=f"http://127.0.0.1:{server.server_port}/")
            assert result.success, result
            page = result.output["page"]
            old = result.output["elements"][0]["ref"]
            result = await call(operation="fill", page=page, ref=old, text="Kong")
            assert result.success, result
            assert not (await call(operation="fill", page=page, ref=old, text="wrong")).success
            button = next(e["ref"] for e in result.output["elements"] if e["name"] == "Save")
            result = await call(operation="click", page=page, ref=button)
            assert result.success and "Saved: Kong" in result.output["text"], result
            before = registry.get("browser").progress_output(result.output)
            fresh = await call(operation="snapshot", page=page)
            assert before == registry.get("browser").progress_output(fresh.output)
            shot = await call(operation="screenshot", page=page, path="proof.png")
            assert shot.success and (tmp_path / "proof.png").read_bytes().startswith(b"\x89PNG")
            assert not (await call(operation="screenshot", page=page, path="proof.png")).success
            assert not (await call(operation="open", url="file:///C:/Windows/win.ini")).success
            await call(operation="close", page=page)
            assert not (await call(operation="snapshot", page=page)).success
        finally:
            await registry.aclose()
        assert registry.get("browser").driver is None
    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_mcp_cancel_does_not_replay(tmp_path):
    pytest.importorskip("mcp")
    script = tmp_path / "slow.py"
    script.write_text('import asyncio\nfrom pathlib import Path\nfrom mcp.server.fastmcp import FastMCP\nm=FastMCP("slow")\n'
        '@m.tool()\nasync def slow()->str:\n Path("started").write_text("once")\n await asyncio.sleep(60)\n return "done"\nm.run()\n')
    async def scenario():
        registry = default_registry(Workspace(tmp_path), mcp_servers={"demo": MCPServer(command="python", args=[str(script)])})
        tool = registry.get("mcp")
        try:
            assert (await tool.run(operation="list", server="demo")).success
            task = asyncio.create_task(tool.run(operation="call", server="demo", tool="slow"))
            for _ in range(100):
                if (tmp_path / "started").exists():
                    break
                await asyncio.sleep(0.02)
            assert (tmp_path / "started").exists()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert tool.connections["demo"].task.done()
            assert not (await tool.run(operation="call", server="demo", tool="slow")).success
        finally:
            await registry.aclose()
    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "nt", reason="Local HTTP fixture uses terminal adapter")
def test_mcp_streamable_http(tmp_path):
    pytest.importorskip("mcp")
    pytest.importorskip("winpty")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    script = tmp_path / "http_mcp.py"
    script.write_text('from mcp.server.fastmcp import FastMCP\n'
        f'm=FastMCP("http", host="127.0.0.1", port={port})\n'
        '@m.tool()\ndef double(n:int)->int:\n return n*2\nm.run(transport="streamable-http")\n')
    async def scenario():
        registry = default_registry(Workspace(tmp_path), allow_terminal=True,
            mcp_servers={"http": MCPServer(transport="http", url=f"http://127.0.0.1:{port}/mcp")})
        try:
            result = await registry.get("terminal").run(operation="start", argv=["python", str(script)], wait_seconds=1)
            assert result.success, result
            for _ in range(100):
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(0.05)
            tool = registry.get("mcp")
            result = await tool.run(operation="list", server="http")
            assert result.success, result
            result = await tool.run(operation="call", server="http", tool="double", arguments={"n":21})
            assert result.success and result.output["structured_content"] == {"result":42}, result
        finally:
            await registry.aclose()
    asyncio.run(scenario())
