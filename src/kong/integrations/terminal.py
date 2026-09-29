"""Windows ConPTY sessions with bounded output and job-owned process trees."""
import asyncio
import codecs
import os
from pathlib import Path
import re
import shutil
import sys
from time import monotonic
from typing import Literal
from uuid import uuid4

from pydantic import Field
from kong.contracts import Contract
from kong.environments.process import process_environment
from kong.tools.base import Tool, ToolResult
from kong.tools.builtin.process_exec import ProcessArgs


class TerminalArgs(Contract):
    operation: Literal["start", "read", "write", "close", "list"]
    argv: list[str] = Field(default_factory=list, max_length=128)
    cwd: str = "."
    session: str = ""
    text: str = Field(default="", max_length=16000, description="Raw terminal input. Use \r to submit, \u0003 for Ctrl+C.")
    cursor: int = Field(default=0, ge=0)
    wait_seconds: float = Field(default=0.2, ge=0, le=10, allow_inf_nan=False)
    lifetime_seconds: int = Field(default=900, ge=1, le=3600)


class Session:
    def __init__(self, process, job, lifetime):
        self.process, self.job = process, job
        self.data, self.total, self.exit_code = "", 0, None
        self.closed, self.expired = False, False
        self.deadline = monotonic() + lifetime
        self.process.fileobj.setblocking(False)
        self.reader = asyncio.create_task(self.drain())

    async def drain(self):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while not self.closed:
                # Limit work per iteration even for a continuously noisy child.
                for _ in range(16):
                    try:
                        chunk = self.process.fileobj.recv(8192)
                    except BlockingIOError:
                        break
                    if not chunk:
                        return
                    text = decoder.decode(chunk)
                    self.total += len(text)
                    self.data = (self.data + text)[-64000:]
                if not self.process.isalive():
                    self.exit_code = self.process.exitstatus
                    # Descendants must not outlive the original command.
                    self.job.close()
                if monotonic() >= self.deadline:
                    self.expired = True
                    return
                await asyncio.sleep(0.02)
        finally:
            if not self.process.isalive():
                self.exit_code = self.process.exitstatus
            self.closed = True
            self.job.close()
            await asyncio.to_thread(self.process.close, True)

    async def aclose(self):
        # A task cancelled before its first instruction never executes finally.
        self.closed = True
        self.job.close()
        self.reader.cancel()
        await asyncio.gather(self.reader, return_exceptions=True)
        await asyncio.to_thread(self.process.close, True)

    def output(self, key, cursor):
        start = self.total - len(self.data)
        offset = max(cursor, start)
        # Strip common ANSI controls from the textual observation, retain cursor in raw chars.
        text = self.data[max(0, offset - start):max(0, offset - start) + 16000]
        end = min(self.total, offset + len(text))
        visible = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", "", text)
        return {"session": key, "running": not self.closed, "exit_code": self.exit_code,
                "expired": self.expired, "output": visible, "next_cursor": end,
                "truncated": cursor < start, "has_more": end < self.total}


class TerminalTool(Tool):
    name = "terminal"
    description = ("Persistent Windows ConPTY: start(argv,cwd) creates an interactive command; write(session,text) sends raw "
        "input (use carriage return to submit, Ctrl+C is \\u0003); read(session,cursor) returns bounded output and next_cursor; "
        "list shows sessions; close terminates its process tree. Use python for Kong's interpreter. "
        "At most 4 sessions, 1h lifetime; cleaned up at CLI exit, never restored from old IDs. "
        "Start/write success is NOT command completion: read exit_code and verify artifacts. "
        "For dev servers verify browser behavior while running. Native user privileges, not a filesystem sandbox. "
        "Do not bypass reserved file restrictions or run outside user scope.")
    args_model = TerminalArgs
    family = "process"
    effects = ("process_exec", "local_write", "network_possible")
    environment = "interactive_terminal"
    execution_timeout = 40

    def __init__(self, workspace):
        self.workspace, self.sessions = workspace, {}

    async def run(self, operation, argv=None, cwd=".", session="", text="", cursor=0,
                  wait_seconds=0.2, lifetime_seconds=900):
        if operation == "list":
            return ToolResult(success=True, output={"sessions": [
                {"session": key, "running": not s.closed, "exit_code": s.exit_code} for key, s in self.sessions.items()]})
        if operation == "start":
            if os.name != "nt":
                return ToolResult(success=False, error="This terminal adapter currently requires Windows ConPTY; use process_exec on other OSes")
            if len(self.sessions) >= 4:
                return ToolResult(success=False, error="At most 4 sessions; close an existing session first")
            args = list(ProcessArgs(argv=argv or []).argv)
            directory = self.workspace.resolve(cwd)
            if not directory.is_dir():
                return ToolResult(success=False, error="cwd must be an existing workspace directory")
            if args[0] == "python":
                args[0] = sys.executable
            resolved = shutil.which(str(directory / args[0])) or shutil.which(args[0])
            if not resolved or Path(resolved).suffix.lower() in {".bat", ".cmd"}:
                return ToolResult(success=False, error="Executable missing or batch requires explicit interpreter")
            args[0] = resolved
            from winpty import PtyProcess
            from kong.environments.windows_job import WindowsJob
            job, proc = WindowsJob(), None
            # Wait for parent to attach the launcher before user command can run.
            bootstrap = "import subprocess,sys; sys.stdin.readline(); sys.exit(subprocess.call(" + repr(args) + "))"
            spawning = asyncio.create_task(asyncio.to_thread(PtyProcess.spawn,
                [sys.executable, "-I", "-u", "-c", bootstrap], cwd=str(directory),
                env=process_environment(), dimensions=(30, 120), backend=1))
            try:
                try:
                    proc = await asyncio.shield(spawning)
                except asyncio.CancelledError:
                    proc = await spawning
                    raise
                job.assign(proc.pid)
                proc.write("\r")
                session = "term_" + uuid4().hex[:12]
                self.sessions[session] = Session(proc, job, lifetime_seconds)
            except BaseException:
                job.close()
                if proc:
                    await asyncio.to_thread(proc.close, True)
                raise
        if session not in self.sessions:
            return ToolResult(success=False, error="Unknown/expired terminal session. Inspect artifacts before starting a replacement.")
        target = self.sessions[session]
        if operation == "close":
            await target.aclose()
            del self.sessions[session]
            return ToolResult(success=True, output={"closed": session})
        if operation == "write":
            if target.closed:
                return ToolResult(success=False, error="Terminal already exited")
            target.process.write(text)
        if wait_seconds:
            await asyncio.sleep(wait_seconds)
        if cursor > target.total:
            return ToolResult(success=False, error="Cursor exceeds terminal output; use a previous next_cursor or zero")
        output = target.output(session, cursor)
        ok = not target.expired and (output["running"] or output["exit_code"] == 0)
        return ToolResult(success=ok, output=output, error=None if ok else "Terminal expired or exited unsuccessfully")

    async def aclose(self):
        for session in self.sessions.values():
            await session.aclose()
        self.sessions.clear()
