"""V0.5 development contracts. These are calibration inputs, not held-out tasks."""
from dataclasses import asdict, dataclass, replace
import math
from pathlib import PurePosixPath


@dataclass(frozen=True)
class TaskBudget:
    runs: int = 3
    decisions: int = 60
    requests: int = 60  # includes recovery calls; separate from returned decisions
    active_seconds: float = 900
    total_tokens: int = 500_000

    def __post_init__(self):
        if any(type(value) not in {int, float} or not math.isfinite(value) or value <= 0 for value in asdict(self).values()):
            raise ValueError("Task budgets must be positive")
        if any(type(getattr(self, field)) is not int for field in ("runs", "decisions", "requests", "total_tokens")):
            raise ValueError("Count and token budgets must be integers")


@dataclass(frozen=True)
class Intervention:
    id: str
    run: int
    trigger: str
    text: str
    supersedes: str | None = None


@dataclass(frozen=True)
class Scenario:
    id: str
    family: str
    files: tuple[tuple[str, str], ...]
    protected: tuple[str, ...]
    artifacts: tuple[str, ...]
    goals: tuple[str, ...]
    interventions: tuple[Intervention, ...]
    appended_input: tuple[str, str] | None = None
    external_edit: tuple[str, str] | None = None
    task_group: str = "calibration"
    schema: str = "kong-long-task-1"
    budget: TaskBudget = TaskBudget()

    def __post_init__(self):
        names = [name for name, _ in self.files]
        paths = names + list(self.protected) + list(self.artifacts)
        if self.appended_input:
            paths.append(self.appended_input[0])
        if self.external_edit:
            paths.append(self.external_edit[0])
            if self.external_edit[0] not in names or self.external_edit[0] in self.protected:
                raise ValueError("External edits must target an existing unprotected file")
        for name in paths:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name or len(path.parts) < 2 or path.parts[0] != "task":
                raise ValueError("Scenario paths must be relative POSIX files inside task/")
        if self.family not in {"code", "data"}:
            raise ValueError("Unknown task family")
        if len(set(names)) != len(names) or not set(self.protected) <= set(names):
            raise ValueError("Duplicate files or missing protected inputs")
        if len(self.goals) != self.budget.runs or self.budget.runs != 3:
            raise ValueError("Version 1 requires exactly three Run goals")
        ids = [i.id for i in self.interventions]
        if len(ids) != len(set(ids)):
            raise ValueError("Intervention IDs must be unique")
        previous = set()
        for entry in self.interventions:
            if not 1 <= entry.run <= self.budget.runs or entry.trigger not in {"first_write", "compaction", "environment_read"}:
                raise ValueError("Unknown intervention event")
            if entry.supersedes and entry.supersedes not in previous:
                raise ValueError("Revision must cite a preceding intervention")
            previous.add(entry.id)

    def manifest(self):
        return asdict(self)

    @classmethod
    def from_manifest(cls, data):
        data = dict(data)
        data["files"] = tuple(tuple(row) for row in data["files"])
        for field in ("protected", "artifacts", "goals"):
            data[field] = tuple(data[field])
        data["interventions"] = tuple(Intervention(**row) for row in data["interventions"])
        data["budget"] = TaskBudget(**data["budget"])
        for field in ("appended_input", "external_edit"):
            if data.get(field) is not None:
                data[field] = tuple(data[field])
        return cls(**data)


_SCOPE = ("Work only in task/. Use process_exec with cwd=task. Preserve the protected files. "
          "Requirements are public; hidden inputs only vary these requirements. "
          "Before continuing, read the current input and requirements.md. ")
_CODE_FINAL = ("Invoice input schema: each non-None line is a mapping with price (decimal-compatible string or number), "
               "quantity (integer), and optional status (string). A cancelled line means status == 'cancelled'; "
               "missing status and all other status values are chargeable. Cancellation is not a boolean cancelled field. "
               "Invoice contract: money(line) for a chargeable line is Decimal(str(price))*quantity rounded HALF_UP to cents "
               "per line; total skips None and status == 'cancelled' lines, retains negative quantities as credits. "
               "invoice.total(lines) sums these rounded line amounts as Decimal, empty input is 0.00. "
               "Preserve invoice.USER_NOTE and the public check.py. Add regression tests in test_added.py. "
               "Run python check.py and python test_added.py after edits; read back both changed modules.")
_DATA_FINAL = ("Read orders.csv, deduplicate by id using the last physical row (including void rows). "
               "Skip empty region and statuses other than paid/refund. amount_cents is integer cents; "
               "refund subtracts the signed amount, paid adds it. summary.csv columns region,net_amount; "
               "region ascending, amounts with exactly two decimals. Empty results have only the CSV header and TOTAL: 0.00 in the report. "
               "Region names contain no colon/newline and are never TOTAL. Use Decimal or integer cents. "
               "Write a rerunnable process.py, run it, read back summary.csv and report.md. "
               "Report headings # Data report, ## Rules, ## Results, ## Unverified. "
               "Results has one line region: amount per group and TOTAL: amount; cite orders.csv, "
               "state last-row deduplication and that external behavior is unverified.")

CODE_SCENARIO = Scenario(
    id="dev_invoice_continuation_v2", family="code", task_group="code_multi_file",
    files=(
        ("task/money.py", "from decimal import Decimal\ndef money(line):\n    return Decimal(str(line['price'])) * line['quantity']\n"),
        ("task/invoice.py", "from money import money\nUSER_NOTE = 'keep my existing integration note'\ndef total(lines):\n    return sum((money(x) for x in lines), 0) + 1\n"),
        ("task/check.py", "from decimal import Decimal\nfrom invoice import total, USER_NOTE\nassert USER_NOTE == 'keep my existing integration note'\nassert isinstance(total([]), Decimal) and total([]) == Decimal('0.00')\nassert total([{'price':'2.00','quantity':3}]) == Decimal('6.00')\nassert total([None, {'price':'7.25','quantity':2,'status':'cancelled'}]) == Decimal('0.00')\nassert total([{'price':'1.015','quantity':1}]) == Decimal('1.02')\nprint('public checks passed')\n"),
        ("task/requirements.md", _CODE_FINAL + "\n"),
        ("task/notes.md", "Integration notes: preserve USER_NOTE; prices are per-item dollars, quantities may be negative.\n"),
    ), protected=("task/check.py", "task/requirements.md", "task/notes.md"),
    artifacts=("task/money.py", "task/invoice.py", "task/test_added.py"),
    goals=tuple(_SCOPE + text for text in (
        "Inspect the multi-file invoice bug and repair it. Ask for a handoff when ready; do not claim final delivery in this Run.",
        "Continue in this Thread, reconcile current files with prior requirements; implement rounding and regression tests. Ask for handoff; final delivery is in the next Run.",
        "Complete the invoice repair on the current files. " + _CODE_FINAL)),
    interventions=(
        Intervention("code-r1", 1, "first_write", "Correction: total([]) must be Decimal('0.00'), never int; keep USER_NOTE."),
        Intervention("code-r2", 1, "compaction", "Refinement: ignore None and lines with status == 'cancelled'; round each line HALF_UP before summing.", "code-r1"),
        Intervention("code-r3", 2, "environment_read", "Correction: negative quantities remain credits; do not filter them. Preserve all prior constraints.", "code-r2"),
    ),
)

DATA_SCENARIO = Scenario(
    id="dev_orders_revision_v2", family="data", task_group="data_report",
    files=(
        ("task/orders.csv", "id,region,amount_cents,status\na,East,125,paid\nb,West,310,paid\na,East,200,paid\nc,East,50,refund\nd,,900,paid\ne,West,25,void\n"),
        ("task/requirements.md", _DATA_FINAL + "\n"),
        ("task/check.py", "import csv\nfrom decimal import Decimal\nwith open('summary.csv', encoding='utf-8', newline='') as stream:\n    reader=csv.DictReader(stream)\n    assert reader.fieldnames==['region','net_amount']\n    rows=list(reader)\nassert [r['region'] for r in rows]==sorted(r['region'] for r in rows)\nassert all(Decimal(r['net_amount']).is_finite() for r in rows)\nprint('public checks passed')\n"),
        ("task/notes.md", "Accounting notes: cents are not dollars; a refund reverses the signed amount.\n"),
    ), protected=("task/orders.csv", "task/requirements.md", "task/check.py", "task/notes.md"),
    artifacts=("task/process.py", "task/summary.csv", "task/report.md"),
    goals=tuple(_SCOPE + text for text in (
        "Inspect orders.csv and write the data pipeline and provisional report. Ask for handoff; no final delivery yet.",
        "Continue the data pipeline with corrected deduplication, refund and missing-region rules. Ask for handoff; next Run receives more input.",
        "Input has been appended by the evaluator. Reconcile the current CSV before final delivery. " + _DATA_FINAL)),
    interventions=(
        Intervention("data-r1", 1, "first_write", "Correction: amount_cents is cents; output dollars with two decimal places. Keep orders.csv intact."),
        Intervention("data-r2", 1, "compaction", "Refinement: last physical row per id wins, even if void; ignore empty region, and subtract refunds.", "data-r1"),
        Intervention("data-r3", 2, "environment_read", "Correction: refund negates the signed amount; a negative refund adds credit back. Retain all earlier rules.", "data-r2"),
    ), appended_input=("task/orders.csv", "f,West,-20,refund\nb,West,999,void\ng,North,1205,paid\n"),
)

DEVELOPMENT_SCENARIOS = (CODE_SCENARIO, DATA_SCENARIO)


def _variant(base, name, group, updates, **kwargs):
    return replace(base, id=name, task_group=group,
        files=tuple((p, updates.get(p, text)) for p, text in base.files), **kwargs)


DEVELOPMENT_LIBRARY = (
    *DEVELOPMENT_SCENARIOS,
    _variant(CODE_SCENARIO, "dev_fractional_credits_v1", "code_multi_file", {
        "task/money.py": "def money(line):\n    return round(float(line['price']) * line['quantity'], 2)\n",
        "task/invoice.py": "from money import money\nUSER_NOTE = 'keep my existing integration note'\ndef total(lines):\n    return int(sum(money(x) for x in lines))\n"}),
    _variant(CODE_SCENARIO, "dev_rounding_external_edit_v1", "code_continuation", {},
        external_edit=("task/money.py", "from decimal import Decimal\n# External integration reverted rounding and cancellation handling.\ndef money(line):\n    return Decimal(str(line['price'])) * line['quantity']\n"),
        goals=(*CODE_SCENARIO.goals[:2], "money.py has received a declared external integration edit. Reconcile the current module before final delivery. " + CODE_SCENARIO.goals[2])),
    _variant(CODE_SCENARIO, "dev_total_external_edit_v1", "code_continuation", {},
        external_edit=("task/invoice.py", dict(CODE_SCENARIO.files)["task/invoice.py"]),
        goals=(*CODE_SCENARIO.goals[:2], "invoice.py has received a declared external integration edit. Reconcile the current module before final delivery. " + CODE_SCENARIO.goals[2])),
    _variant(DATA_SCENARIO, "dev_empty_accounting_report_v1", "data_report", {
        "task/orders.csv": "id,region,amount_cents,status\na,East,125,paid\na,East,125,void\nb,,900,paid\n"},
        appended_input=("task/orders.csv", "c,West,-20,void\nd,,1205,paid\n")),
    _variant(DATA_SCENARIO, "dev_unicode_signed_refunds_v1", "data_continuation", {
        "task/orders.csv": 'id,region,amount_cents,status\na,华东,125,paid\nb,"South, Coast",-31,refund\na,华东,200,paid\nc,华东,50,refund\nd,,900,paid\ne,华北,25,void\n'},
        appended_input=("task/orders.csv", 'f,"South, Coast",-20,refund\na,华东,999,void\ng,华北,1205,paid\n')),
    _variant(DATA_SCENARIO, "dev_large_cents_revisions_v1", "data_continuation", {
        "task/orders.csv": "id,region,amount_cents,status\na,East,9007199254740993,paid\nb,West,-101,refund\nc,West,100,paid\na,East,9007199254740995,paid\nd,,900,paid\ne,West,25,void\n"},
        appended_input=("task/orders.csv", "f,East,-31,refund\nc,West,999,void\ng,North,0,paid\n")),
)
