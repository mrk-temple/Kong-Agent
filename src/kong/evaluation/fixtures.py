"""Deterministic offline drivers. They validate the harness, not agent capability."""
import base64
import json

from kong.contracts import Act, Action, Criterion, FinalResponse, NeedUserInput, Plan, PlanProposal, PlanStep
from kong.integrations.browser import BrowserArgs
from kong.integrations.mcp import MCPArgs
from kong.integrations.terminal import TerminalArgs
from kong.models.fake import ScriptedModel
from kong.tools.base import Tool, ToolResult
from .specs import EVIDENCE_REQUEST


def _act(name, **arguments):
    return Act(actions=[Action(name=name, arguments=arguments)])


_GROUP_SCRIPT = '''import csv
from collections import defaultdict
from decimal import Decimal
totals=defaultdict(lambda:[0,Decimal('0')])
with open('{source}',encoding='utf-8',newline='') as stream:
 for row in csv.DictReader(stream):
  if row['status']!='paid': continue
  key=row['{key}'];q=int(row['quantity']);totals[key][0]+=q;totals[key][1]+=q*Decimal(row['unit_price'])
with open('summary.csv','w',encoding='utf-8',newline='') as stream:
 out=csv.writer(stream,lineterminator='\\n');out.writerow(['{key}','quantity','revenue'])
 for key in sorted(totals):
  q,v=totals[key];out.writerow([key,q,f'{{v:.2f}}'])
'''

_REFUND_SCRIPT = '''import csv
from collections import defaultdict
from decimal import Decimal
totals=defaultdict(lambda:Decimal('0'))
with open('ledger.csv',encoding='utf-8',newline='') as stream:
 for row in csv.DictReader(stream):
  value=Decimal(row['amount']);totals[row['product']]+=value if row['kind']=='sale' else -value
with open('summary.csv','w',encoding='utf-8',newline='') as stream:
 out=csv.writer(stream,lineterminator='\\n');out.writerow(['product','net_revenue'])
 for key in sorted(totals):out.writerow([key,f'{totals[key]:.2f}'])
'''


def fixture_model(case_id: str) -> ScriptedModel:
    """A fixed action trace used only for implementation regression."""
    if case_id in {"data_paid_product", "data_region", "data_refunds"}:
        source = {"data_paid_product":"orders.csv", "data_region":"sales.csv", "data_refunds":"ledger.csv"}[case_id]
        code = (_REFUND_SCRIPT if case_id == "data_refunds" else
                _GROUP_SCRIPT.format(source=source, key="product" if case_id == "data_paid_product" else "region"))
        decisions = [_act("read_file", path=source), _act("write_file", path="process.py", content=code),
                     _act("process_exec", argv=["python", "process.py"]),
                     _act("read_file", path="summary.csv"), FinalResponse(content="Fixture artifact created", evidence_ids=["a4"])]
    elif case_id == "repair_total":
        decisions = [_act("read_file", path="calc.py"),
                     _act("patch_file", path="calc.py", old_text="sum(values) + 1", new_text="sum(values)"),
                     _act("process_exec", argv=["python", "check.py"]),
                     FinalResponse(content="Fixture repair checked", evidence_ids=["a3"])]
    elif case_id == "source_tomllib":
        answer = {"first_version":"3.11", "supports_writing":False, "load_mode":"rb", "source_file":"source.txt"}
        decisions = [_act("read_file", path="source.txt"),
                     _act("write_file", path="research.json", content=json.dumps(answer)),
                     _act("read_file", path="research.json"),
                     FinalResponse(content="Fixture source synthesis", evidence_ids=["a3"])]
    elif case_id == "terminal_prompt":
        decisions = [_act("terminal", operation="start", argv=["python", "ask.py"]),
                     _act("terminal", operation="write", session="fixture_session", text="Kong\r"),
                     _act("terminal", operation="read", session="fixture_session"),
                     _act("read_file", path="answer.txt"),
                     _act("terminal", operation="close", session="fixture_session"),
                     FinalResponse(content="Fixture terminal done", evidence_ids=["a4"])]
    elif case_id == "mcp_add":
        decisions = [_act("mcp", operation="servers"), _act("mcp", operation="list", server="acceptance"),
                     _act("mcp", operation="call", server="acceptance", tool="add", arguments={"a":19,"b":23}),
                     _act("write_file", path="result.txt", content="42\n"),
                     _act("read_file", path="result.txt"), _act("mcp", operation="close", server="acceptance"),
                     FinalResponse(content="Fixture MCP done", evidence_ids=["a5"])]
    elif case_id == "browser_form":
        decisions = [_act("terminal", operation="start", argv=["python", "app.py"]),
                     _act("browser", operation="open", url="http://127.0.0.1:12345/"),
                     _act("browser", operation="fill", page="fixture_page", ref="fixture_name", text="Kong"),
                     _act("browser", operation="click", page="fixture_page", ref="fixture_save"),
                     _act("browser", operation="snapshot", page="fixture_page"),
                     _act("browser", operation="screenshot", page="fixture_page", path="proof.png"),
                     _act("read_file", path="saved.json"),
                     _act("browser", operation="close", page="fixture_page"),
                     _act("terminal", operation="close", session="fixture_session"),
                     FinalResponse(content="Fixture browser done", evidence_ids=["a7"])]
    elif case_id == "tool_error_recovery":
        decisions = [_act("read_file", path="missing.txt"), _act("read_file", path="instructions.txt"),
                     _act("write_file", path="result.txt", content="RECOVERED"),
                     _act("read_file", path="result.txt"),
                     FinalResponse(content="Recovered from fixture error", evidence_ids=["a4"])]
    elif case_id == "plan_approval":
        plan = Plan(goal="Create output", rationale="Approval required before a file write",
                    steps=[PlanStep(id="s1", title="Read and transform")],
                    success_criteria=[Criterion(id="output", description="Output exists", kind="file_exists", path="output.txt")])
        decisions = [PlanProposal(plan=plan)]
    elif case_id == "insufficient_evidence":
        decisions = [_act("read_file", path="claim.txt"), NeedUserInput(question=EVIDENCE_REQUEST)]
    elif case_id == "unknown_action_restore":
        decisions = []
    else:
        raise ValueError("Unknown fixture case")
    return ScriptedModel(decisions)


class FixtureTerminal(Tool):
    name = "terminal"
    description = "Simulated terminal for offline harness verification."
    args_model = TerminalArgs
    family = "process"
    environment = "fixture"

    def __init__(self, workspace):
        self.workspace = workspace
        self.sessions = {}

    async def run(self, **kw):
        op = kw["operation"]
        if op == "start":
            self.sessions["fixture_session"] = kw["argv"][-1]
            output = "URL=http://127.0.0.1:12345/\n" if kw["argv"][-1] == "app.py" else "Name? "
            return ToolResult(success=True, output={"session":"fixture_session", "running":True, "output":output})
        if kw.get("session") not in self.sessions:
            return ToolResult(success=False, error="Fixture session missing")
        if op == "write":
            if self.sessions[kw["session"]] == "ask.py" and kw["text"] == "Kong\r":
                (self.workspace.root / "answer.txt").write_text("Kong", encoding="utf-8")
            return ToolResult(success=True, output={"session":kw["session"], "written":True})
        if op == "read":
            return ToolResult(success=True, output={"session":kw["session"], "running":False,
                                                    "exit_code":0, "output":"HELLO Kong"})
        if op == "close":
            del self.sessions[kw["session"]]
            return ToolResult(success=True, output={"closed":kw["session"]})
        return ToolResult(success=False, error="Unsupported fixture operation")

    async def aclose(self):
        self.sessions.clear()


class FixtureMCP(Tool):
    name = "mcp"
    description = "Simulated MCP server for offline harness verification."
    args_model = MCPArgs
    environment = "fixture"

    def __init__(self):
        self.connections = {}

    async def run(self, **kw):
        op = kw["operation"]
        if op == "servers":
            return ToolResult(success=True, output={"servers":["acceptance"]})
        if kw.get("server") != "acceptance":
            return ToolResult(success=False, error="Unknown fixture server")
        if op == "list":
            self.connections["acceptance"] = True
            return ToolResult(success=True, output={"tools":[{"name":"add"}]})
        if op == "call" and self.connections and kw["tool"] == "add" and kw["arguments"] == {"a":19,"b":23}:
            return ToolResult(success=True, output={"structured_content":{"result":42}})
        if op == "close":
            self.connections.clear()
            return ToolResult(success=True, output={"closed":"acceptance"})
        return ToolResult(success=False, error="Invalid fixture MCP sequence")

    async def aclose(self):
        self.connections.clear()


_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lXcAAAAASUVORK5CYII=")


class FixtureBrowser(Tool):
    name = "browser"
    description = "Simulated browser for offline harness verification."
    args_model = BrowserArgs
    family = "browser"
    environment = "fixture"

    def __init__(self, workspace):
        self.workspace = workspace
        self.pages = {}
        self.value = ""
        self.saved = False

    async def run(self, **kw):
        op = kw["operation"]
        if op == "open" and kw["url"] == "http://127.0.0.1:12345/":
            self.pages["fixture_page"] = True
            return ToolResult(success=True, output={"page":"fixture_page", "url":kw["url"], "text":"Name Save"})
        if kw.get("page") not in self.pages:
            return ToolResult(success=False, error="Fixture page missing")
        if op == "fill" and kw["ref"] == "fixture_name":
            self.value = kw["text"]
            return ToolResult(success=True, output={"filled":True})
        if op == "click" and kw["ref"] == "fixture_save":
            (self.workspace.root / "saved.json").write_text(json.dumps({"name":self.value}), encoding="utf-8")
            self.saved = True
            return ToolResult(success=True, output={"clicked":True})
        if op == "snapshot":
            return ToolResult(success=True, output={"page":"fixture_page", "text":"Saved: " + self.value if self.saved else "Name Save"})
        if op == "screenshot":
            (self.workspace.root / kw["path"]).write_bytes(_PNG)
            return ToolResult(success=True, output={"path":kw["path"]})
        if op == "close":
            self.pages.clear()
            return ToolResult(success=True, output={"closed":"fixture_page"})
        return ToolResult(success=False, error="Invalid fixture browser sequence")

    async def aclose(self):
        self.pages.clear()
