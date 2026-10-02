"""V0.4A variation set. These contracts do not alter the legacy twelve tasks."""

from dataclasses import dataclass

from .specs import Case, _BASE, _DATA


@dataclass(frozen=True)
class VariationContract:
    artifact: str
    editable: tuple[str, ...] = ()
    required_reads: tuple[str, ...] = ()
    required_failed_read: str | None = None
    required_failed_process: tuple[str, ...] | None = None
    required_process: str | None = None
    hidden_inputs: bool = False
    continuation: bool = False


VARIATION_CASES: tuple[Case, ...] = (
    Case("v04_repair_clamp", "repair_v04", "Fix limits.py clamp(value, low, high) so values below low become low, values above high become high, and in-range values stay unchanged. Run check.py. Preserve check.py.",
         {"limits.py":"def clamp(value, low, high):\n    return min(low, max(value, high))\n", "check.py":"from limits import clamp\nassert clamp(5, 0, 10) == 5\nassert clamp(-1, 0, 10) == 0\nprint('ok')\n"}, _DATA,
         "Hidden boundary and negative-range inputs pass; check.py unchanged; successful process run after edit."),
    Case("v04_repair_mean", "repair_v04", "Fix stats.py mean(values): return the arithmetic mean, and return None for an empty list. Run check.py and preserve it.",
         {"stats.py":"def mean(values):\n    return sum(values) // len(values)\n", "check.py":"from stats import mean\nassert mean([2, 4]) == 3\nassert mean([]) is None\nprint('ok')\n"}, _DATA,
         "Hidden fractional, negative and singleton inputs pass; check.py unchanged; successful process run after edit."),
    Case("v04_repair_slug", "repair_v04", "Fix textutil.py slug(text): lowercase words, strip surrounding whitespace, and replace any run of internal whitespace with one hyphen. Run check.py. Preserve check.py.",
         {"textutil.py":"def slug(text):\n    return text.lower().replace(' ', '-')\n", "check.py":"from textutil import slug\nassert slug('  Hello World  ') == 'hello-world'\nprint('ok')\n"}, _DATA,
         "Hidden tab/newline and repeated-space inputs pass; check.py unchanged; successful process run after edit."),
    Case("v04_repair_merge", "repair_v04", "Fix counts.py merge(left, right) to return a new dictionary adding counts for matching keys, without mutating either input. Run check.py. Preserve check.py.",
         {"counts.py":"def merge(left, right):\n    left.update(right)\n    return left\n", "check.py":"from counts import merge\na={'A':2};b={'A':3,'B':1}\nassert merge(a,b)=={'A':5,'B':1}\nassert a=={'A':2}\nprint('ok')\n"}, _DATA,
         "Hidden overlap and disjoint inputs pass without mutation; check.py unchanged; successful process run after edit."),
    Case("v04_data_month", "data_v04", "Read orders.csv. Write and run process.py to create summary.csv with month,paid_revenue, grouped by YYYY-MM for paid orders only, Decimal to two decimals, month ascending. Run check.py after generation. Preserve orders.csv and check.py.",
         {"orders.csv":"date,amount,status\n2026-01-05,10.20,paid\n2026-02-01,5.00,void\n2026-01-19,0.80,paid\n2026-02-03,3.25,paid\n",
          "check.py":"import csv\nwith open('summary.csv',newline='',encoding='utf-8') as f:\n r=list(csv.DictReader(f))\nassert r==[{'month':'2026-01','paid_revenue':'11.00'},{'month':'2026-02','paid_revenue':'3.25'}]\nprint('ok')\n"}, _DATA,
         "Exact monthly CSV and protected inputs; successful final check; executable script also passes a separate unseen dataset."),
    Case("v04_data_inventory", "data_v04", "Read movements.csv. Write and run process.py to create summary.csv with sku,stock; add in quantities and subtract out quantities, sorted by sku. Run check.py after generation. Preserve movements.csv and check.py.",
         {"movements.csv":"sku,kind,quantity\nB,in,10\nA,in,3\nB,out,4\nA,out,1\nB,in,2\n",
          "check.py":"import csv\nwith open('summary.csv',newline='',encoding='utf-8') as f:\n r=list(csv.DictReader(f))\nassert r==[{'sku':'A','stock':'2'},{'sku':'B','stock':'8'}]\nprint('ok')\n"}, _DATA,
         "Exact inventory CSV and protected inputs; successful final check; executable script also passes a separate unseen dataset."),
    Case("v04_data_dedupe", "data_v04", "Read events.csv. Write and run process.py to create summary.csv with customer,paid_total. Count each order_id only once, taking its last event by input order; sum only final paid events with Decimal, two decimals, customer ascending. Run check.py after generation. Preserve events.csv and check.py.",
         {"events.csv":"order_id,customer,status,amount\n1,A,paid,5.00\n2,B,paid,2.10\n1,A,refunded,5.00\n3,A,paid,1.25\n",
          "check.py":"import csv\nwith open('summary.csv',newline='',encoding='utf-8') as f:\n r=list(csv.DictReader(f))\nassert r==[{'customer':'A','paid_total':'1.25'},{'customer':'B','paid_total':'2.10'}]\nprint('ok')\n"}, _DATA,
         "Exact last-event CSV and protected inputs; successful final check; executable script also passes a separate unseen dataset."),
    Case("v04_data_tax", "data_v04", "Read invoices.csv. Write and run process.py to create summary.csv with region,gross; gross is each paid row's net*(1+tax_rate), Decimal rounded HALF_UP to cents per row before summing, region ascending. Run check.py after generation. Preserve invoices.csv and check.py.",
         {"invoices.csv":"region,net,tax_rate,status\nEast,0.05,0.10,paid\nEast,0.05,0.10,paid\nWest,12.50,0.08,paid\nWest,100.00,0.08,void\n",
          "check.py":"import csv\nwith open('summary.csv',newline='',encoding='utf-8') as f:\n r=list(csv.DictReader(f))\nassert r==[{'region':'East','gross':'0.12'},{'region':'West','gross':'13.50'}]\nprint('ok')\n"}, _DATA,
         "Exact per-row rounding CSV and protected inputs; successful final check; executable script also passes a separate unseen dataset."),
    Case("v04_recover_missing", "recovery_v04", "Try reading missing.txt, then recover using instructions.txt. Create result.txt with the exact recovery code from instructions.txt and read it back. Preserve instructions.txt.",
         {"instructions.txt":"recovery_code=RESET-42\n"}, _BASE,
         "Failed read recorded, later source read and result read recorded; exact protected-source value."),
    Case("v04_recover_invalid", "recovery_v04", "Run python -m json.tool stale.json and observe its failure. Recover by reading canonical.json and write result.json containing only its value field as a JSON string; read result.json back. Preserve both inputs.",
         {"stale.json":"{invalid\n", "canonical.json":"{\"value\": \"CURRENT\"}\n"}, _DATA,
         "Failed JSON parse observed, canonical source used, exact typed result and protected inputs."),
    Case("v04_recover_absent", "recovery_v04", "Try reading report.txt, which may not exist. Read recipe.txt, create report.txt with the requested marker alone, then read it back. Preserve recipe.txt.",
         {"recipe.txt":"marker=READY-7\n"}, _BASE,
         "Initial missing read, source read, later artifact read and exact marker."),
    Case("v04_revision_status", "revision_v04", "Read request.md and correction.md. The correction supersedes the initial request. Write report.json with keys included_statuses and total, summing only included rows from rows.csv. Read it back. Preserve all inputs.",
         {"request.md":"Initial request: count every row.\n", "correction.md":"Correction: count only active rows.\n", "rows.csv":"status,value\nactive,4\narchived,50\nactive,3\n"}, _BASE,
         "Later correction governs typed JSON and protected input hashes; source reads recorded."),
    Case("v04_revision_currency", "revision_v04", "Read brief.md, change.md and amounts.csv. Follow the later change: report only USD total in report.json with keys currency and total (two-decimal string). Read it back. Preserve inputs.",
         {"brief.md":"Initial request: sum all currencies.\n", "change.md":"Later change: only USD, do not convert EUR.\n", "amounts.csv":"currency,amount\nUSD,1.20\nEUR,99.00\nUSD,2.35\n"}, _BASE,
         "Later currency restriction governs typed JSON and protected input hashes; source reads recorded."),
    Case("v04_revision_schema", "revision_v04", "Read initial.md, update.md and users.csv. Follow the later update: write report.json as an object with active_names sorted ascending; omit inactive users and the old count field. Read it back. Preserve inputs.",
         {"initial.md":"Initial request: output a count field.\n", "update.md":"Correction: output only active_names; omit count.\n", "users.csv":"name,status\nZoe,active\nAda,active\nKim,inactive\n"}, _BASE,
         "Later schema restriction governs exact JSON shape and protected input hashes; source reads recorded."),
    Case("v04_continue_export", "continuation_v04", "Continue the saved project work recorded in handoff.md. Fix export.py so export_rows(rows) returns a CSV header and one line per row sorted by id; run check.py, and preserve handoff.md and check.py.",
         {"handoff.md":"Earlier session: export_rows remains broken. Next session should finish it and run check.py.\n", "export.py":"def export_rows(rows):\n    return 'id,name\\n'\n", "check.py":"from export import export_rows\nassert export_rows([(2,'B'),(1,'A')]) == 'id,name\\n1,A\\n2,B\\n'\nprint('ok')\n"}, _DATA,
         "Saved prior run and thread association retained; hidden export inputs pass; protected handoff and check unchanged."),
    Case("v04_continue_failures", "continuation_v04", "Continue the saved project work recorded in handoff.md and failures.txt. Fix parser.py parse_int(text) so surrounding whitespace is accepted but invalid text raises ValueError. Run check.py and preserve the previous records.",
         {"handoff.md":"Earlier session: parser fix is incomplete. The failed case in failures.txt must guide the next session.\n", "failures.txt":"parse_int('  -7  ') returned the wrong result.\n", "parser.py":"def parse_int(text):\n    return int(text.strip()) + 1\n", "check.py":"from parser import parse_int\nassert parse_int('  -7  ') == -7\nprint('ok')\n"}, _DATA,
         "Saved prior run and thread association retained; hidden invalid and numeric inputs pass; prior records protected."),
)


CONTRACTS = {
    "v04_repair_clamp": VariationContract("limits.py", ("limits.py",), ("limits.py",), required_process="check.py", hidden_inputs=True),
    "v04_repair_mean": VariationContract("stats.py", ("stats.py",), ("stats.py",), required_process="check.py", hidden_inputs=True),
    "v04_repair_slug": VariationContract("textutil.py", ("textutil.py",), ("textutil.py",), required_process="check.py", hidden_inputs=True),
    "v04_repair_merge": VariationContract("counts.py", ("counts.py",), ("counts.py",), required_process="check.py", hidden_inputs=True),
    "v04_data_month": VariationContract("summary.csv", ("process.py",), ("orders.csv",), required_process="process.py", hidden_inputs=True),
    "v04_data_inventory": VariationContract("summary.csv", ("process.py",), ("movements.csv",), required_process="process.py", hidden_inputs=True),
    "v04_data_dedupe": VariationContract("summary.csv", ("process.py",), ("events.csv",), required_process="process.py", hidden_inputs=True),
    "v04_data_tax": VariationContract("summary.csv", ("process.py",), ("invoices.csv",), required_process="process.py", hidden_inputs=True),
    "v04_recover_missing": VariationContract("result.txt", required_reads=("instructions.txt", "result.txt"), required_failed_read="missing.txt"),
    "v04_recover_invalid": VariationContract("result.json", required_reads=("canonical.json", "result.json"),
                                             required_failed_process=("python", "-m", "json.tool", "stale.json")),
    "v04_recover_absent": VariationContract("report.txt", required_reads=("recipe.txt", "report.txt"), required_failed_read="report.txt"),
    "v04_revision_status": VariationContract("report.json", required_reads=("request.md", "correction.md", "rows.csv", "report.json")),
    "v04_revision_currency": VariationContract("report.json", required_reads=("brief.md", "change.md", "amounts.csv", "report.json")),
    "v04_revision_schema": VariationContract("report.json", required_reads=("initial.md", "update.md", "users.csv", "report.json")),
    "v04_continue_export": VariationContract("export.py", ("export.py",), ("handoff.md", "export.py"), required_process="check.py", hidden_inputs=True, continuation=True),
    "v04_continue_failures": VariationContract("parser.py", ("parser.py",), ("handoff.md", "failures.txt", "parser.py"), required_process="check.py", hidden_inputs=True, continuation=True),
}
