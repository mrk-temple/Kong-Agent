import asyncio
import json
import os
import subprocess

import pytest

from kong.environments.docker import DockerBackend
from kong.tools.base import ToolCall, ToolResult
from kong.tools.defaults import default_registry
from kong.tools.executor import ToolExecutor
from kong.tools.permissions import PermissionPolicy
from kong.workspace import Workspace


def make_backend(path, image="busybox:latest"):
    (path / "job").mkdir(exist_ok=True)
    (path / "job").chmod(0o777)
    return DockerBackend(Workspace(path), "job", image)


def test_docker_arguments_enforce_explicit_boundary_and_cleanup(tmp_path):
    backend = make_backend(tmp_path)
    calls = []
    async def fake(args, timeout=15, limit=16000):
        calls.append(args)
        if args[:2] == ["image", "inspect"]:
            return ToolResult(success=True, output={"stdout": '"sha256:fixture"'})
        return ToolResult(success=True, output={"stdout": "ok", "exit_code": 0})
    backend._docker = fake
    async def scenario():
        result = await backend.execute(["echo", "a; no shell interpolation"], tmp_path / "job",
            timeout_seconds=1, max_output_bytes=500, encoding="utf-8")
        assert result.success
        creation = next(c for c in calls if c[0] == "create")
        assert creation[creation.index("--network") + 1] == "none"
        assert "--read-only" in creation and "--cap-drop" in creation and "--security-opt" in creation
        assert "--privileged" not in creation and "--publish" not in creation
        assert "--pull" in creation and creation[-2:] == ["sha256:fixture", "a; no shell interpolation"]
        assert calls[-1][:3] == ["rm", "--force", "--volumes"]
    asyncio.run(scenario())


def test_unavailable_docker_image_has_no_execution_fallback(tmp_path):
    backend = make_backend(tmp_path)
    calls = []
    async def fake(args, timeout=15, limit=16000):
        calls.append(args)
        return ToolResult(success=False, error="not available")
    backend._docker = fake
    with pytest.raises(ValueError, match="no automatic pull or host fallback"):
        asyncio.run(backend.prepare())
    assert len(calls) == 1 and calls[0][:2] == ["image", "inspect"]


def test_docker_rejects_workspace_root_and_reserved_mount_content(tmp_path):
    workspace = Workspace(tmp_path)
    with pytest.raises(ValueError, match="dedicated"):
        DockerBackend(workspace, ".", "busybox:latest")
    backend = make_backend(tmp_path)
    (tmp_path / "job" / "api.txt").write_text("fixture only")
    with pytest.raises(ValueError, match="reserved"):
        backend.validate_mount()


def test_read_only_resource_policy_mounts_docker_resource_read_only(tmp_path):
    backend = make_backend(tmp_path)
    calls = []
    backend.image_id = "sha256:fixture"
    async def fake(args, timeout=15, limit=16000):
        calls.append(args)
        return ToolResult(success=True, output={"stdout": "ok"})
    backend._docker = fake
    workspace = Workspace(tmp_path)
    executor = ToolExecutor(default_registry(workspace, allow_process=True, process_backend=backend),
        permissions=PermissionPolicy(read_roots=["job"], write_roots=[]))
    result = asyncio.run(executor.execute(ToolCall(name="process_exec", arguments={"argv": ["echo", "ok"], "cwd": "job"})))
    assert result.success
    creation = next(c for c in calls if c[0] == "create")
    assert creation[creation.index("--mount") + 1].endswith(",readonly")


def test_docker_cancellation_waits_for_creation_then_removes_container(tmp_path):
    async def scenario():
        backend = make_backend(tmp_path)
        backend.image_id = "sha256:fixture"
        created, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def fake(args, timeout=15, limit=16000):
            calls.append(args)
            if args[0] == "create":
                created.set()
                await release.wait()
            return ToolResult(success=True, output={"stdout": "ok"})
        backend._docker = fake
        task = asyncio.create_task(backend.execute(["sleep", "10"], tmp_path / "job",
            timeout_seconds=1, max_output_bytes=500, encoding="utf-8"))
        await created.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls[-1][:3] == ["rm", "--force", "--volumes"]
        assert not any(c[0] == "start" for c in calls)
    asyncio.run(scenario())


LIVE_IMAGE = os.environ.get("KONG_TEST_DOCKER_IMAGE")


@pytest.mark.skipif(not LIVE_IMAGE, reason="Fresh import verification requires an installed trusted Linux Python image")
def test_real_docker_fresh_python_reads_source_despite_same_timestamp_cache(tmp_path):
    async def scenario():
        backend = make_backend(tmp_path, LIVE_IMAGE)
        module = tmp_path / "job" / "subject.py"
        module.write_bytes(b"value = 1\n")
        before = module.stat()
        executor = ToolExecutor(default_registry(Workspace(tmp_path), allow_process=True, process_backend=backend))
        compiled = await executor.execute(ToolCall(name="process_exec", arguments={"cwd": "job",
            "argv": ["python", "-c", "import py_compile; py_compile.compile('subject.py', doraise=True)"]}))
        assert compiled.success, compiled
        module.write_bytes(b"value = 2\n")
        os.utime(module, ns=(before.st_atime_ns, before.st_mtime_ns))
        args = {"cwd": "job", "argv": ["python", "-c", "from subject import value; print(value)"]}
        legacy = await executor.execute(ToolCall(name="process_exec", arguments=args))
        assert legacy.success and legacy.output["stdout"].strip() == "1"
        checked = await executor.execute(ToolCall(name="process_exec", arguments={**args, "fresh_python": True}))
        assert checked.success and checked.output["stdout"].strip() == "2"
        assert checked.output["fresh_python_imports"] is True
    asyncio.run(scenario())


@pytest.mark.skipif(not LIVE_IMAGE, reason="Set KONG_TEST_DOCKER_IMAGE to an installed trusted Linux Python image for real boundary probes")
def test_real_docker_file_network_and_subprocess_boundaries(tmp_path):
    async def scenario():
        backend = make_backend(tmp_path, LIVE_IMAGE)
        (tmp_path / "outside-canary.txt").write_text("private host canary")
        workspace = Workspace(tmp_path)
        executor = ToolExecutor(default_registry(workspace, allow_process=True, process_backend=backend))
        # The workload deliberately tries host traversal, network, a child writer,
        # privileged/root writes and daemon socket access, then makes a valid artifact.
        script = """import json, os, pathlib, socket, subprocess, sys
checks = {}
checks['outside_absent'] = not pathlib.Path('/outside-canary.txt').exists() and not pathlib.Path('../outside-canary.txt').exists()
checks['docker_socket_absent'] = not pathlib.Path('/var/run/docker.sock').exists()
try:
    pathlib.Path('/root-escape.txt').write_text('escape')
    checks['root_readonly'] = False
except OSError:
    checks['root_readonly'] = True
sock = socket.socket()
sock.settimeout(0.3)
try:
    sock.connect(('1.1.1.1', 443))
    checks['network_blocked'] = False
except OSError:
    checks['network_blocked'] = True
finally:
    sock.close()
checks['unprivileged'] = os.geteuid() == 65534
checks['child_boundary'] = subprocess.run([sys.executable, '-c', "import pathlib,sys; sys.exit(0 if not pathlib.Path('/outside-canary.txt').exists() else 1)"]).returncode == 0
pathlib.Path('result.json').write_text(json.dumps(checks))
print(json.dumps(checks))
"""
        result = await executor.execute(ToolCall(name="process_exec", arguments={"argv": ["python", "-c", script],
            "cwd": "job", "timeout_seconds": 15}))
        assert result.success, result
        checks = json.loads(result.output["stdout"])
        assert all(checks.values()) and json.loads((tmp_path / "job" / "result.json").read_text()) == checks
        assert (tmp_path / "outside-canary.txt").read_text() == "private host canary"
        name = result.output["container"]
        assert subprocess.run(["docker", "container", "inspect", name], capture_output=True).returncode != 0
        # A read-only resource policy must be enforced by the real mount too.
        executor.permissions = PermissionPolicy(read_roots=["job"], write_roots=[])
        denied = await executor.execute(ToolCall(name="process_exec", arguments={"argv": ["python", "-c",
            "from pathlib import Path; Path('forbidden.txt').write_text('no')"], "cwd": "job"}))
        assert not denied.success and not (tmp_path / "job" / "forbidden.txt").exists()
    asyncio.run(scenario())


@pytest.mark.skipif(not LIVE_IMAGE, reason="Real Docker child cleanup requires an installed trusted Python image")
def test_real_docker_timeout_cleans_descendants_and_retains_unknown_effect(tmp_path):
    async def scenario():
        backend = make_backend(tmp_path, LIVE_IMAGE)
        workspace = Workspace(tmp_path)
        executor = ToolExecutor(default_registry(workspace, allow_process=True, process_backend=backend))
        child = "import time,pathlib; time.sleep(4); pathlib.Path('late.txt').write_text('escaped')"
        script = "import subprocess,sys,time,pathlib; pathlib.Path('started.txt').write_text('started'); subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); time.sleep(30)"
        result = await executor.execute(ToolCall(name="process_exec", arguments={"argv": ["python", "-c", script],
            "cwd": "job", "timeout_seconds": 2}))
        assert not result.success and result.outcome_unknown
        assert (tmp_path / "job" / "started.txt").exists()
        name = result.output["container"]
        assert subprocess.run(["docker", "container", "inspect", name], capture_output=True).returncode != 0
        await asyncio.sleep(4.2)
        assert not (tmp_path / "job" / "late.txt").exists()
    asyncio.run(scenario())
