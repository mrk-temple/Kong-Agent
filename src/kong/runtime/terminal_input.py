"""One daemon reader for a CLI session; stdin never blocks the event loop."""
import asyncio
from collections import deque
from queue import Queue, Empty
import sys
from threading import Thread


class TerminalInput:
    def __init__(self):
        self.lines = Queue(maxsize=128)
        self.deferred = deque()
        self.eof = False
        Thread(target=self._read, daemon=True, name="kong-stdin").start()

    def _read(self):
        while line := sys.stdin.readline():
            self.lines.put(line.rstrip("\r\n"))
        self.lines.put(None)

    def poll(self):
        try:
            return True, self.lines.get_nowait()
        except Empty:
            return False, None

    async def get(self, prompt=""):
        if prompt:
            print(prompt, end="", flush=True)
        if self.deferred:
            return self.deferred.popleft()
        while not self.eof:
            ready, line = self.poll()
            if ready:
                if line is None:
                    self.eof = True
                    break
                return line
            await asyncio.sleep(0.025)
        raise EOFError()


async def run_with_input(runtime, reader):
    task = asyncio.create_task(runtime.run())
    try:
        while not task.done():
            await asyncio.sleep(0.025)
            while runtime.state.status == "running" and not task.done():
                ready, line = reader.poll()
                if not ready:
                    break
                if line is None:
                    reader.eof = True
                    break
                command, _, argument = line.strip().partition(" ")
                try:
                    if command in {"/cancel", "/add", "/goal"}:
                        runtime.submit_input({"/cancel": "cancel", "/add": "constraint", "/goal": "goal"}[command], argument)
                    else:
                        reader.deferred.append(line)
                except ValueError:
                    print("输入未加入：任务状态、文本或队列容量不允许。", flush=True)
        return await task
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
