"""I-07/10: an opt-in Python check must import final source, not timestamp-cache bytes."""
import asyncio
import os
import py_compile

from kong.tools.base import ToolCall
from kong.tools.defaults import default_registry
from kong.tools.executor import ToolExecutor
from kong.workspace import Workspace


def test_fresh_python_imports_ignore_same_size_same_timestamp_stale_bytecode(tmp_path):
    module = tmp_path / "subject.py"
    module.write_bytes(b"value = 1\n")
    before = module.stat()
    py_compile.compile(str(module), doraise=True)
    module.write_bytes(b"value = 2\n")
    os.utime(module, ns=(before.st_atime_ns, before.st_mtime_ns))
    executor = ToolExecutor(default_registry(Workspace(tmp_path), allow_process=True))
    async def scenario():
        argv = ["python", "-c", "from subject import value; print(value)"]
        legacy = await executor.execute(ToolCall(name="process_exec", arguments={"argv": argv}))
        assert legacy.success and legacy.output["stdout"].strip() == "1"  # permanent reproducer
        checked = await executor.execute(ToolCall(name="process_exec", arguments={"argv": argv, "fresh_python": True}))
        assert checked.success and checked.output["stdout"].strip() == "2"
        assert checked.output["fresh_python_imports"] is True
        assert module.read_bytes() == b"value = 2\n"
    asyncio.run(scenario())


def test_fresh_python_cannot_enable_execution_or_override_cache_scope(tmp_path):
    async def scenario():
        denied = ToolExecutor(default_registry(Workspace(tmp_path), allow_process=False))
        call = ToolCall(name="process_exec", arguments={"argv": ["python", "-c", "print('no')"], "fresh_python": True})
        assert not (await denied.execute(call)).success
        executor = ToolExecutor(default_registry(Workspace(tmp_path), allow_process=True))
        override = ToolCall(name="process_exec", arguments={"argv": ["python", "-X", "pycache_prefix=.", "-c", "print('no')"],
            "fresh_python": True})
        result = await executor.execute(override)
        assert not result.success and "cache" in result.error
    asyncio.run(scenario())
