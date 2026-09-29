"""Opt-in real-model acceptance for terminal, MCP, and browser. Uses local api.txt in memory.

python examples/integration_evaluation.py --case terminal|mcp|browser
Local fixtures have no external side effects. No automatic plan approval or retries.
"""
import argparse
import asyncio
import json
from pathlib import Path
import time
from uuid import uuid4

import httpx
from pydantic import SecretStr
from live_evaluation import credentials, TimedModel
from kong.config import load_config
from kong.contracts import Criterion
from kong.integrations.config import MCPServer
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


FORM_SERVER = '''from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import json
HTML = b"""<!doctype html><meta charset="utf-8"><title>Kong acceptance</title>
<label>Name<input id="name"></label><button onclick="save()">Save</button><output></output>
<script>async function save(){const r=await fetch('/save',{method:'POST',body:JSON.stringify({name:document.querySelector('input').value})});const x=await r.json();document.querySelector('output').textContent='Saved: '+x.name}</script>"""
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(HTML)
 def do_POST(self):
  data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  Path('saved.json').write_text(json.dumps(data))
  self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(data).encode())
 def log_message(self,*args): pass
server=HTTPServer(('127.0.0.1',0),Handler)
print('URL=http://127.0.0.1:'+str(server.server_port)+'/',flush=True)
server.serve_forever()
'''


async def main(case):
    root = Path(__file__).resolve().parents[1]
    folder = root / ".kong" / "evaluations" / ("integrations-" + case + "-" + uuid4().hex[:8])
    folder.mkdir(parents=True)
    model_key, _ = credentials(root / "api.txt")
    config = load_config(root / "kong.local.toml", "relay")
    config.api_key = SecretStr(model_key)
    servers = None
    if case == "terminal":
        (folder / "ask.py").write_text("from pathlib import Path\nx=input('Name? '); Path('answer.txt').write_text(x); print('HELLO '+x)\n")
        goal = "使用 terminal 启动 python ask.py，在交互提示 Name? 后输入 Kong 并提交，读取进程结束状态并确认 answer.txt 恰好为 Kong。关闭会话后报告。不修改 ask.py，不用其他方式生成 answer.txt。"
        artifact = "answer.txt"
    elif case == "mcp":
        script = folder / "server.py"
        script.write_text('from mcp.server.fastmcp import FastMCP\nm=FastMCP("acceptance")\n@m.tool()\ndef add(a:int,b:int)->int:\n return a+b\nm.run()\n')
        servers = {"acceptance": MCPServer(command="python", args=[str(script)])}
        goal = "使用 mcp 查看配置的服务器并发现 acceptance 的工具，用其 add 工具计算 19+23。把服务器返回的数值写入 result.txt（仅数值），读取核验，再关闭连接。不修改 server.py，不用其他计算方式替代 MCP。"
        artifact = "result.txt"
    else:
        (folder / "app.py").write_text(FORM_SERVER)
        goal = "使用 terminal 启动 python app.py，从输出获取本地网页 URL。用 browser 真正打开网页，在 Name 输入框填 Kong 并点击 Save；观察 Saved: Kong，保存截图为 proof.png，并读取 saved.json 核验 name 为 Kong。关闭浏览器页面和服务器终端后报告。不修改 app.py，不直接请求后端或伪造 saved.json。技术路线已确定，请直接验证。"
        artifact = "saved.json"
    workspace = Workspace(folder)
    registry = default_registry(workspace, allow_terminal=case != "mcp", allow_browser=case == "browser", mcp_servers=servers)
    start = time.perf_counter()
    async with httpx.AsyncClient() as client:
        model = TimedModel(config, client)
        runtime = Runtime.new(goal, model, workspace, registry, store=RunStore(folder / ".kong" / "runs"))
        runtime.state.hard_turn_limit = 24
        runtime.state.success_criteria = [Criterion(id="artifact", description="Acceptance artifact exists", kind="file_exists", path=artifact)]
        if case == "terminal":
            runtime.state.success_criteria.append(Criterion(id="exit", description="Read a successful terminal exit", kind="tool_result",
                tool_name="terminal", operation="read", output_match={"running":False,"exit_code":0}))
        elif case == "mcp":
            runtime.state.success_criteria.append(Criterion(id="mcp_result", description="MCP returns the requested sum", kind="tool_result",
                tool_name="mcp", operation="call", arguments_match={"server":"acceptance","tool":"add","arguments":{"a":19,"b":23}},
                output_match={"structured_content":{"result":42}}))
        else:
            runtime.state.success_criteria += [Criterion(id="dom", description="Snapshot confirms saved UI", kind="tool_result",
                tool_name="browser", operation="snapshot", output_contains={"/text":"Saved: Kong"}),
                Criterion(id="image",description="Screenshot exists",kind="file_exists",path="proof.png")]
        print(json.dumps({"case": case, "workspace": str(folder)}), flush=True)
        try:
            await asyncio.wait_for(runtime.run(), timeout=600)
        except TimeoutError:
            runtime.save()
        finally:
            model_closed = all(not getattr(registry.get(name), attr) for name, attr in
                (("terminal", "sessions"), ("browser", "pages"), ("mcp", "connections")) if name in registry.names())
            await registry.aclose()
        observations = runtime.history.observations
        checks = {"model_closed_resources": model_closed}
        if case == "terminal":
            checks["exact_input"] = (folder / artifact).exists() and (folder / artifact).read_text() == "Kong"
            checks["exit_zero"] = any(o.action.name == "terminal" and o.success and isinstance(o.output, dict) and o.output.get("exit_code") == 0 for o in observations)
        elif case == "mcp":
            checks["exact_result"] = (folder / artifact).exists() and (folder / artifact).read_text().strip() == "42"
            checks["real_mcp_call"] = any(o.action.name == "mcp" and o.success and o.action.arguments.get("operation") == "call" and o.output.get("structured_content") == {"result":42} for o in observations)
        else:
            checks["backend_saved"] = (folder / artifact).exists() and json.loads((folder / artifact).read_text()) == {"name":"Kong"}
            checks["dom_confirmed"] = any(o.action.name == "browser" and o.success and "Saved: Kong" in o.output.get("text", "") for o in observations)
            checks["real_click"] = any(o.action.name == "browser" and o.success and o.action.arguments.get("operation") == "click" for o in observations)
            checks["screenshot"] = (folder / "proof.png").exists() and (folder / "proof.png").read_bytes().startswith(b"\x89PNG")
        report = {"case":case, "model":config.model, "status":runtime.state.status.value, "turns":runtime.state.turn_count,
            "model_errors":[e.payload.get("error") for e in runtime.history.events if e.type == "model_error"],
            "seconds":round(time.perf_counter()-start,3), "checks":checks, "run_id":runtime.state.run_id,
            "calls":model.calls, "failed_tools":[{"tool":o.action.name,"error":o.error} for o in observations if not o.success],
            "final":runtime.state.final_output, "stop_reason":runtime.state.stop_reason}
        (folder / "evaluation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k:v for k,v in report.items() if k not in {"calls","final"}}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=["terminal", "mcp", "browser"], required=True)
    asyncio.run(main(parser.parse_args().case))
