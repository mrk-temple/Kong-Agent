"""Measure Runtime and actual filesystem/process tools without provider waiting.

Uses fixed decisions, real tools and fresh workspaces. Not a model-quality test.
"""
import argparse
import asyncio
import json
from pathlib import Path
import statistics
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from kong.contracts import Act, Action, Criterion, FinalResponse
from kong.environments.docker import DockerBackend
from kong.evaluation.runner import _implementation_sha256, _behavior_resources, _digest
from kong.models.fake import ScriptedModel
from kong.reporting import run_report
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.tools.permissions import PermissionPolicy
from kong.workspace import Workspace


async def measure(output, repetitions, image):
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for backend_name in ['host', *(['docker'] if image else [])]:
        for repeat in range(1, repetitions+1):
            folder = output / backend_name / f'trial-{repeat:02d}'
            job = folder / 'job'
            job.mkdir(parents=True)
            (job / 'input.json').write_text('{"numbers":[20,22]}',encoding='utf-8')
            workspace = Workspace(folder)
            backend = DockerBackend(workspace,'job',image) if image and backend_name == 'docker' else None
            registry = default_registry(workspace,allow_process=True,process_backend=backend)
            script = "import json; from pathlib import Path; data=json.loads(Path('input.json').read_text()); Path('result.json').write_text(json.dumps({'total':sum(data['numbers'])}))"
            decisions = [Act(actions=[
                Action(name='read_file',arguments={'path':'job/input.json'}),
                Action(name='write_file',arguments={'path':'job/process.py','content':script}),
                Action(name='process_exec',arguments={'argv':['python' if backend else sys.executable,'process.py'],'cwd':'job'}),
                Action(name='read_file',arguments={'path':'job/result.json'}),
            ]), FinalResponse(content='Checked',evidence_ids=['a4'])]
            runtime = Runtime.new('sum the input',ScriptedModel(decisions),workspace,registry,
                                  store=RunStore(folder/'.kong/runs'),
                                  permissions=PermissionPolicy(read_roots=['job'],write_roots=['job']) if backend else PermissionPolicy())
            runtime.state.success_criteria = [Criterion(id='total',description='exact total',kind='file_json',path='job/result.json',json_match={'total':42})]
            started = perf_counter()
            await runtime.run()
            total = perf_counter()-started
            report = run_report(runtime)
            verified = json.loads((job/'result.json').read_text(encoding='utf-8')) == {'total':42}
            rows.append(dict(backend=backend_name,repetition=repeat,full_task_seconds=round(total,6),
                             passed=verified and runtime.state.status.value == 'completed',
                             metrics=report['metrics'],snapshot=str(runtime.store.path(runtime.state.run_id))))
            await registry.aclose()
    summaries = []
    for backend in dict.fromkeys(r['backend'] for r in rows):
        group = [r for r in rows if r['backend'] == backend]
        summaries.append(dict(backend=backend,repetitions=len(group),
            median_full_task_seconds=statistics.median(r['full_task_seconds'] for r in group),
            median_executor_seconds=statistics.median(r['metrics']['tool_seconds'] for r in group),
            median_batch_wall_seconds=statistics.median(r['metrics']['tool_wall_seconds'] for r in group),
            median_runtime_overhead_seconds=statistics.median(r['metrics']['runtime_overhead_seconds'] for r in group)))
    report = dict(schema=1,implementation_sha256=_implementation_sha256(),resource_fingerprint=_digest(_behavior_resources()),
                  scope='Fixed decisions, real filesystem/process tools, fresh workspaces; scripted model overhead only, no LLM requests; small sample, not a production latency target',
                  docker_image=image,all_passed=all(r['passed'] for r in rows),trials=rows,summaries=summaries)
    (output/'timing.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--repetitions',type=int,default=3)
    parser.add_argument('--docker-image',help='Explicit installed trusted Linux Python image; no pull')
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 10:
        parser.error('--repetitions must be 1..10')
    report = asyncio.run(measure(args.output,args.repetitions,args.docker_image))
    print(json.dumps(dict(all_passed=report['all_passed'],summaries=report['summaries']),ensure_ascii=False))
    return 0 if report['all_passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
