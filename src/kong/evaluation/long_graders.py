"""External oracles. Agent execution never mounts grader resources or runs on host."""
import csv
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import tempfile
from uuid import uuid4

from kong.environments.docker import DockerBackend
from kong.workspace import Workspace


def digest_file(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def data_oracle(text: str):
    latest = {}
    for row in csv.DictReader(io.StringIO(text)):
        latest[row["id"]] = row
    totals = {}
    for row in latest.values():
        region, status = row["region"], row["status"]
        if not region or status not in {"paid", "refund"}:
            continue
        cents = int(row["amount_cents"]) * (-1 if status == "refund" else 1)
        totals[region] = totals.get(region, 0) + cents
    return [{"region": key, "net_amount": f"{Decimal(value) / 100:.2f}"} for key, value in sorted(totals.items())]


def check_data(task: Path, expected_input: str):
    expected = data_oracle(expected_input)
    try:
        with (task / "summary.csv").open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            actual = list(reader)
            columns = reader.fieldnames
        report = (task / "report.md").read_text(encoding="utf-8")
        # Parse an explicit public result section. Extra/duplicate/mismatched numbers fail.
        result_text = report.split("## Results\n", 1)[1].split("## ", 1)[0]
        reported = re.findall(r"^([^:\n]+): (-?\d+\.\d{2})$", result_text, re.M)
        expected_report = [(r["region"], r["net_amount"]) for r in expected]
        total = sum((Decimal(r["net_amount"]) for r in expected), Decimal(0))
        return {
            "all_rows": columns == ["region", "net_amount"] and actual == expected,
            "report_numbers": reported == [*expected_report, ("TOTAL", f"{total:.2f}")],
            "report_contract": all(x in report for x in ("# Data report", "## Rules", "## Results", "## Unverified", "orders.csv", "last", "unverified")),
        }
    except (OSError, ValueError, KeyError, IndexError):
        return {"all_rows": False, "report_numbers": False, "report_contract": False}


async def execute_isolated(folder: Path, image: str, argv: list[str], *, writable=False):
    backend = DockerBackend(Workspace(folder), "task", image)
    backend.writable = writable
    if argv[0] == "python":
        # -B alone prevents writes but still reads pre-existing bytecode caches.
        argv = ["python", "-X", "pycache_prefix=/tmp/kong-grade-" + uuid4().hex, "-B", *argv[1:]]
    return await backend.execute(argv, folder / "task", timeout_seconds=30, max_output_bytes=16000, encoding="utf-8")


async def grade(scenario, folder: Path, image: str, protected_hashes: dict[str, str], *, grader_root: Path):
    """Grade a new disposable copy; never repair a scored artifact or trust its stdout."""
    task = folder / "task"
    checks = {}
    for name, expected in protected_hashes.items():
        try:
            checks["protected:" + name] = digest_file(folder / name) == expected
        except OSError:
            checks["protected:" + name] = False
    checks["artifacts_exist"] = all((folder / p).is_file() and not (folder / p).is_symlink() for p in scenario.artifacts)
    if not all(checks.values()):
        return checks
    # Reject links before copy as well as before the container mount.
    DockerBackend(Workspace(folder), "task", image).validate_mount()
    grader_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="grade-", dir=grader_root) as temporary:
        copied = Path(temporary)
        shutil.copytree(task, copied / "task")
        public = await execute_isolated(copied, image, ["python", "-B", "check.py"])
        checks["public_behavior"] = public.success
        if scenario.family == "code":
            # Hidden variants exercise the full public contract in fresh imports.
            inputs = [[], [{"price": "1.005", "quantity": 1}],
                      [None, {"price": "99", "quantity": 5, "status": "cancelled"}],
                      [{"price": "3.335", "quantity": -2}, {"price": "0.005", "quantity": 1}],
                      [{"price": "0.005", "quantity": 1}] * 7]
            script = ("import sys,json;sys.path.insert(0,'/workspace');from invoice import total,USER_NOTE;"
                      "from decimal import Decimal;xs=json.loads(sys.argv[1]);"
                      "print(json.dumps({'note':USER_NOTE,'values':[{'decimal':isinstance(total(x),Decimal),'value':str(total(x))} for x in xs]}))")
            result = await execute_isolated(copied, image, ["python", "-I", "-B", "-c", script, json.dumps(inputs)])
            try:
                actual = json.loads(result.output["stdout"])
                checks["hidden_behavior"] = result.success and actual == {
                    "note": "keep my existing integration note",
                    "values": [{"decimal": True, "value": value} for value in ("0.00", "1.01", "0.00", "-6.66", "0.07")],
                }
            except (ValueError, TypeError, KeyError):
                checks["hidden_behavior"] = False
            added = await execute_isolated(copied, image, ["python", "-B", "test_added.py"])
            checks["added_tests_execute"] = added.success
        else:
            source = (task / "orders.csv").read_text(encoding="utf-8")
            checks.update(check_data(task, source))
            before = {name: digest_file(copied / "task" / name) for name in ("summary.csv", "report.md")}
            rerun = await execute_isolated(copied, image, ["python", "-B", "process.py"], writable=True)
            checks["rerun_same_bytes"] = rerun.success and all(digest_file(copied / "task" / name) == value for name, value in before.items())
            checks["rerun_protected_inputs"] = all(digest_file(copied / name) == expected for name, expected in protected_hashes.items())
            changed = "id,region,amount_cents,status\nx,South,17,paid\ny,East,123,paid\nx,South,31,refund\nz,East,-5,refund\ny,East,99,void\nk,,55,paid\n"
            (copied / "task" / "orders.csv").write_text(changed, encoding="utf-8")
            rerun = await execute_isolated(copied, image, ["python", "-B", "process.py"], writable=True)
            checks["unseen_input"] = rerun.success and all(check_data(copied / "task", changed).values())
            checks["unseen_input_protected"] = (copied / "task/orders.csv").read_text(encoding="utf-8") == changed and all(
                digest_file(copied / name) == expected for name, expected in protected_hashes.items() if name != "task/orders.csv")
    # Artifact fingerprints bind this particular grade; subsequent edits require a new grade.
    return checks
