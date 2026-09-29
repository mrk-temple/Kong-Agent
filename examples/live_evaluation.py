"""Opt-in paid evaluation. Credentials stay in memory, outside agent workspaces.

Run with --case probe|data|research|fullstack. No hidden retries or auto approvals.
The independent verifier runs outside the agent's context.
"""
import argparse
import asyncio
import csv
import json
import re
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import SecretStr

from kong.config import load_config
from kong.contracts import Criterion
from kong.models.compatible import CompatibleModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.web.config import WebConfig
from kong.web.service import WebService
from kong.workspace import Workspace


def credentials(path):
    fields = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        parts = re.split(r"\s*[:：=]\s*", line, maxsplit=1)
        if len(parts) == 2:
            fields[parts[0].strip().lower()] = parts[1].strip().strip('"\'')
    model = fields.get("siliconflow api key", "")
    search = fields.get("tavily_api_key", "")
    if not model or not search:
        raise ValueError("Expected SiliconFlow API Key and TAVILY_API_KEY fields")
    return model, search


class TimedModel(CompatibleModel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []

    async def generate(self, context):
        start = time.perf_counter()
        record = {"input_chars": sum(len(m["content"]) for m in context.messages)}
        try:
            result = await super().generate(context)
            record["decision"] = result.kind
            return result
        finally:
            record["seconds"] = round(time.perf_counter() - start, 3)
            record["usage"] = self.last_usage
            self.calls.append(record)
            print(json.dumps({"model_call": len(self.calls), **record}), flush=True)


GOALS = {
    "repair": "修复当前 calc.py 的 total(values) 函数，应返回所有输入整数之和，空列表返回0，支持负数。不要修改 check.py。实际执行 python check.py 验证，再报告改动和结果。仅操作当前工作区。",
    "data": "处理当前目录 orders.csv，按 product 汇总已付款(status=paid)订单的 quantity 和 quantity*unit_price；忽略 cancelled，使用十进制金额避免浮点误差，按 product 升序写 summary.csv，列为 product,quantity,revenue，金额保留两位。实际运行处理脚本并核对文件，报告总收入。仅在当前工作区内操作，不读取环境变量或工作区外文件。",
    "research": "研究 Python 标准库 tomllib：首次加入哪个 Python 版本、是否支持写入 TOML、load 的文件应该以何种模式打开。必须使用搜索工具找到 Python 官方文档，并抓取原文逐项核对，将中文结论和逐项原始链接写入 research.md。不要以搜索摘要代替原文，也不要安装依赖。仅在当前工作区内操作。",
    "fullstack": "在当前目录创建一个可运行的待办应用：Python 标准库 HTTP 后端 app.py（python app.py --port 端口，绑定127.0.0.1），前端 index.html（原生JS/CSS）。GET /api/tasks 返回JSON数组；POST /api/tasks 接收 {title:字符串} 并返回带id,title,done=false的对象，状态201；空白title返回400；PATCH /api/tasks/<id> 接收 {done:true} 更新并返回对象；数据在当前工作区 tasks.json 持久化。GET / 提供页面，包含任务输入框、添加按钮、可切换完成的任务项、清楚的错误提示。界面中文，手机宽度可用。使用标准库自行启动临时服务进行HTTP验证，结束测试须关闭服务。写 README.md 启动说明。仅在当前工作区内操作，不安装依赖，不读取环境变量或工作区外文件。技术路线已指定，请直接实现并验证。",
}


async def main(args):
    root = Path(__file__).resolve().parents[1]
    model_key, search_key = credentials(root / "api.txt")
    config = load_config(root / "kong.local.toml", "relay")
    config.api_key = SecretStr(model_key)
    config.timeout = 120
    if args.no_thinking:
        config.enable_thinking = False
    eval_root = root / ".kong" / "evaluations"
    folder = (eval_root / args.resume).resolve() if args.resume else eval_root / (args.case + "-" + uuid4().hex[:8])
    if not folder.is_relative_to(eval_root.resolve()):
        raise ValueError("Resume must stay inside evaluation directory")
    folder.mkdir(parents=True, exist_ok=bool(args.resume))
    web = WebConfig(mode="live", provider="tavily", api_key_env="TAVILY_API_KEY", api_key=SecretStr(search_key), proxy_url=args.proxy)
    print(json.dumps({"workspace": str(folder), "case": args.case, "model": config.model}), flush=True)
    start = time.perf_counter()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        model = TimedModel(config, client)
        if args.case == "probe":
            service = WebService(folder, web)
            report = {}
            for name, operation in [
                ("search", lambda: service.search("Python tomllib documentation", count=3, domains=["docs.python.org"])),
                ("fetch", lambda: service.fetch("https://docs.python.org/3/library/tomllib.html")),
            ]:
                tick = time.perf_counter()
                try:
                    output = await operation()
                    report[name] = {"success": True, "seconds": round(time.perf_counter()-tick,3), "output": output}
                except Exception as exc:
                    from kong.web.transport import WebError
                    report[name] = {"success": False, "error_type": type(exc).__name__,
                                    "error":str(exc) if isinstance(exc, WebError) else "unexpected error"}
                print(json.dumps({name: {k:v for k,v in report[name].items() if k != "output"}}), flush=True)
            from kong.context import Context
            try:
                result = await model.generate(Context(messages=[{"role":"user", "content":'Return JSON only: {"kind":"respond","content":"ready"}'}]))
                report["model"] = {"success": result.content == "ready", "calls": model.calls}
            except Exception as exc:
                report["model"] = {"success":False, "error_type":type(exc).__name__}
        else:
            if args.case == "data" and not args.resume:
                (folder / "orders.csv").write_text("product,quantity,unit_price,status\n苹果,3,1.10,paid\n香蕉,2,2.35,paid\n苹果,2,1.10,paid\n香蕉,99,2.35,cancelled\n梨,1,0.10,paid\n", encoding="utf-8")
            if args.case == "repair" and not args.resume:
                (folder / "calc.py").write_text("def total(values):\n    return sum(values) + 1\n", encoding="utf-8")
                (folder / "check.py").write_text("from calc import total\nassert total([2,3]) == 5\nassert total([]) == 0\nassert total([-3,1]) == -2\nprint('checks passed')\n", encoding="utf-8")
            registry = default_registry(Workspace(folder), allow_process=True, web_config=web)
            store = RunStore(folder / ".kong" / "runs")
            def events(kind, data):
                print(json.dumps({"event":kind, **data},ensure_ascii=False),flush=True)
            if args.resume:
                previous = json.loads((folder / "evaluation.json").read_text(encoding="utf-8"))
                snapshot = store.load(previous["run_id"])
                if args.follow_up:
                    runtime = Runtime.new(args.follow_up, model, Workspace(folder), registry, store=store,
                                          thread_id=snapshot.thread_id, on_event=events)
                    runtime.state.hard_turn_limit = 20
                else:
                    model.calls = previous["calls"]
                    runtime = Runtime.restore(snapshot, model, Workspace(folder), registry, store=store, on_event=events)
                if args.approve_plan and runtime.state.pending_approval:
                    runtime.human("approve", revision=runtime.state.pending_approval.revision)
                elif runtime.state.pending_question:
                    runtime.human("answer", text="继续原任务；保持范围与验收条件，遵守决策JSON格式。")
            else:
                runtime = Runtime.new(GOALS[args.case], model, Workspace(folder), registry, store=store, on_event=events)
                runtime.state.hard_turn_limit = 24
            expected_file = {"data":"summary.csv", "research":"research.md", "fullstack":"index.html", "repair":"calc.py"}[args.case]
            runtime.state.success_criteria = [Criterion(id="artifact", description="Requested artifact exists", kind="file_exists", path=expected_file)]
            if args.case in {"data", "fullstack", "repair"}:
                runtime.state.success_criteria.append(Criterion(id="execution", description="Run verification after file edits", kind="tool_success", tool_name="process_exec", after_last_write=True))
            try:
                await asyncio.wait_for(runtime.run(), timeout=900)
            except TimeoutError:
                runtime.save()
            checks = {}
            if args.case == "repair":
                from kong.environments.process import execute_process
                verify = await execute_process([sys.executable, "-I", "-c",
                    "import sys; sys.path.insert(0,'.'); from calc import total; assert total([7,9])==16; assert total([])==0; assert total([-5,2])==-3; print('independent passed')"],
                    folder, timeout_seconds=10, max_output_bytes=2000, encoding="utf-8")
                checks["independent_cases"] = verify.success
            if args.case == "data" and (folder / expected_file).exists():
                with (folder / expected_file).open(encoding="utf-8-sig", newline="") as stream:
                    rows = list(csv.DictReader(stream))
                checks["exact_totals_and_order"] = rows == [
                    {"product":"梨","quantity":"1","revenue":"0.10"},
                    {"product":"苹果","quantity":"5","revenue":"5.50"},
                    {"product":"香蕉","quantity":"2","revenue":"4.70"}]
            if args.case == "research" and (folder / expected_file).exists():
                content = (folder / expected_file).read_text(encoding="utf-8-sig")
                checks = {"mentions_3_11":"3.11" in content, "official_link":"https://docs.python.org/" in content,
                          "fetched_official_page":any(o.success and o.action.name == "web_fetch" and "docs.python.org" in str(o.output.get("url", "")) for o in runtime.history.observations)}
            report = {"case": args.case, "model":config.model, "status":runtime.state.status.value,
                "model_errors":[e.payload.get("error") for e in runtime.history.events if e.type == "model_error"],
                "stop_reason":runtime.state.stop_reason, "question":runtime.state.pending_question,
                "run_id":runtime.state.run_id, "turns":runtime.state.turn_count, "calls":model.calls,
                "observations":len(runtime.history.observations), "failed_tools":[{"tool":o.action.name,"error":o.error} for o in runtime.history.observations if not o.success],
                "checks":checks,"final":runtime.state.final_output}
        report["seconds"] = round(time.perf_counter()-start + (previous["seconds"] if args.resume and not args.follow_up else 0),3)
        report_path = folder / ("followup-evaluation.json" if args.follow_up else "evaluation.json")
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({"report":str(report_path), **{k:v for k,v in report.items() if k in {"status","checks","seconds","stop_reason"}}},ensure_ascii=False),flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=["probe", *GOALS], required=True)
    parser.add_argument("--proxy", help="Explicit trusted HTTP(S) proxy for web tools")
    parser.add_argument("--resume", help="Existing evaluation directory name")
    parser.add_argument("--approve-plan", action="store_true", help="Operator approves the reviewed pending plan when resuming")
    parser.add_argument("--no-thinking", action="store_true", help="Explicit SiliconFlow non-thinking mode comparison")
    parser.add_argument("--follow-up", help="New goal in an existing evaluation Thread; requires --resume")
    args = parser.parse_args()
    if (args.follow_up or args.approve_plan) and not args.resume:
        parser.error("--follow-up/--approve-plan requires --resume")
    if args.case == "probe" and args.resume:
        parser.error("probe cannot resume")
    asyncio.run(main(args))
