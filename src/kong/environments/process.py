"""Bounded foreground execution with concurrent pipe draining and tree cleanup."""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from kong.tools.base import ToolResult


# No user program runs until the parent attaches this launcher to its Windows job.
_LAUNCHER = """import json, subprocess, sys
args = json.loads(sys.stdin.buffer.readline())
try:
    child = subprocess.Popen(args, stdin=subprocess.DEVNULL, shell=False)
except OSError as exc:
    print('Unable to start executable: ' + str(exc), file=sys.stderr)
    sys.exit(127)
sys.exit(child.wait())
"""


def process_environment() -> dict[str, str]:
    # Deliberate allowlist: provider credentials and Python injection settings
    # are not inherited. This does not prevent a trusted program reading disk.
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
               "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL",
               "VIRTUAL_ENV"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    return env


class _Capture:
    def __init__(self, limit: int):
        self.limit = limit
        self.data = bytearray()
        self.total = 0

    async def drain(self, stream):
        while chunk := await stream.read(8192):
            self.total += len(chunk)
            self.data.extend(chunk[:max(0, self.limit - len(self.data))])

    def fields(self, name, encoding):
        return {name: self.data.decode(encoding, errors="replace"),
                name + "_bytes": self.total, name + "_truncated": self.total > self.limit}


async def execute_process(argv: list[str], cwd: Path, *, timeout_seconds: float,
                          max_output_bytes: int, encoding: str) -> ToolResult:
    proc, job = None, None
    drains = []
    out, err = _Capture(max_output_bytes), _Capture(max_output_bytes)
    timed_out = False
    exit_code = None
    if os.name == "nt":
        from kong.environments.windows_job import WindowsJob
        job = WindowsJob()
    try:
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if job else {"start_new_session": True}
        command = [sys.executable, "-I", "-u", "-c", _LAUNCHER] if job else argv
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
            *command, cwd=cwd, env=process_environment(),
            stdin=asyncio.subprocess.PIPE if job else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options))
        try:
            proc = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            # Retain ownership even if cancellation races with process creation.
            proc = await spawning
            raise
        drains = [asyncio.create_task(out.drain(proc.stdout)), asyncio.create_task(err.drain(proc.stderr))]
        if job:
            job.assign(proc.pid)  # Fail closed: the waiting launcher has no command yet.
            proc.stdin.write(json.dumps(argv).encode("utf-8") + b"\n")
            await proc.stdin.drain()
            proc.stdin.close()
        try:
            async with asyncio.timeout(timeout_seconds):
                # wait() can wait for inherited pipes after the root exits.
                # Observe root exit first, then close descendants and drain EOF.
                while proc.returncode is None:
                    await asyncio.sleep(0.02)
            exit_code = proc.returncode
        except TimeoutError:
            timed_out = True
    finally:
        if job:
            job.close()
        if proc:
            if os.name != "nt":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif proc.returncode is None:
                try:
                    proc.kill()  # Also handles cancellation before job assignment.
                except ProcessLookupError:
                    pass
            await proc.wait()
        if drains:
            await asyncio.gather(*drains)
    output = {"exit_code": exit_code, "timed_out": timed_out,
              **out.fields("stdout", encoding), **err.fields("stderr", encoding)}
    error = ("Process timed out; partial side effects may exist. Inspect outputs before retrying."
             if timed_out else None if exit_code == 0 else f"Process exited with code {exit_code}")
    return ToolResult(success=exit_code == 0 and not timed_out, output=output, error=error)
