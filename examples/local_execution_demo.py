"""Offline, real local execution with a scripted decision driver (no model/key)."""
import asyncio
from pathlib import Path
import sys
from uuid import uuid4

from kong.contracts import Act, Action, Criterion, FinalResponse, Status
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


async def main():
    root = Path.cwd() / ".kong" / "demos" / uuid4().hex
    root.mkdir(parents=True)
    workspace = Workspace(root)
    script = """import csv
from pathlib import Path
rows = [int(line) for line in Path('input.txt').read_text(encoding='utf-8').splitlines()]
with Path('report.csv').open('w', encoding='utf-8', newline='') as stream:
    writer = csv.writer(stream)
    writer.writerow(['count', 'total'])
    writer.writerow([len(rows), sum(rows)])
print('Created report.csv')
"""
    model = ScriptedModel([
        Act(actions=[
            Action(name="write_file", arguments={"path": "input.txt", "content": "3\n5\n8\n"}),
            Action(name="write_file", arguments={"path": "summarize.py", "content": script}),
            Action(name="read_file", arguments={"path": "input.txt"}),
        ]),
        Act(actions=[Action(name="process_exec", arguments={"argv": ["python", "summarize.py"]})]),
        Act(actions=[Action(name="read_file", arguments={"path": "report.csv"})]),
        FinalResponse(content="已实际生成并读取 report.csv：3 条记录，合计 16。", evidence_ids=["a5"]),
    ])
    runtime = Runtime.new("读取数字资料，执行汇总，生成 CSV 并验证合计", model, workspace,
        default_registry(workspace, allow_process=True), store=RunStore(root / ".kong" / "runs"))
    runtime.state.success_criteria = [Criterion(id="total", description="CSV contains expected count and sum",
        kind="file_contains", path="report.csv", contains="3,16")]
    print("离线固定决策演示：文件和进程真实执行，不代表真实模型自主能力。")
    state = await runtime.run()
    print(state.final_output or state.stop_reason or state.pending_question)
    print(f"工作区：{root.resolve()}")
    print(f"状态：{state.status}；模型轮次：{state.turn_count}")
    return 0 if state.status == Status.COMPLETED else 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(main()))
