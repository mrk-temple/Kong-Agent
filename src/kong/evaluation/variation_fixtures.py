"""Known traces used to smoke-test the variation harness, not model ability."""

import json

from kong.contracts import Act, Action, FinalResponse
from kong.models.fake import ScriptedModel

from .variations import CONTRACTS


def _act(name, **arguments):
    return Act(actions=[Action(name=name, arguments=arguments)])


SOLUTIONS = {
    "v04_repair_clamp": "def clamp(value, low, high):\n    return max(low, min(value, high))\n",
    "v04_repair_mean": "def mean(values):\n    return sum(values) / len(values) if values else None\n",
    "v04_repair_slug": "import re\ndef slug(text):\n    return re.sub(r'\\s+', '-', text.strip().lower())\n",
    "v04_repair_merge": "def merge(left, right):\n    result = dict(left)\n    for key, value in right.items():\n        result[key] = result.get(key, 0) + value\n    return result\n",
    "v04_data_month": """import csv
from collections import defaultdict
from decimal import Decimal
total=defaultdict(lambda:Decimal('0'))
with open('orders.csv',encoding='utf-8',newline='') as f:
 for row in csv.DictReader(f):
  if row['status']=='paid': total[row['date'][:7]]+=Decimal(row['amount'])
with open('summary.csv','w',encoding='utf-8',newline='') as f:
 out=csv.writer(f,lineterminator='\\n');out.writerow(['month','paid_revenue'])
 for month,value in sorted(total.items()):out.writerow([month,f'{value:.2f}'])
""",
    "v04_data_inventory": """import csv
from collections import defaultdict
total=defaultdict(int)
with open('movements.csv',encoding='utf-8',newline='') as f:
 for row in csv.DictReader(f):total[row['sku']]+=int(row['quantity'])*(1 if row['kind']=='in' else -1)
with open('summary.csv','w',encoding='utf-8',newline='') as f:
 out=csv.writer(f,lineterminator='\\n');out.writerow(['sku','stock'])
 for sku,value in sorted(total.items()):out.writerow([sku,value])
""",
    "v04_data_dedupe": """import csv
from collections import defaultdict
from decimal import Decimal
latest={}
with open('events.csv',encoding='utf-8',newline='') as f:
 for row in csv.DictReader(f):latest[row['order_id']]=row
total=defaultdict(lambda:Decimal('0'))
for row in latest.values():
 if row['status']=='paid':total[row['customer']]+=Decimal(row['amount'])
with open('summary.csv','w',encoding='utf-8',newline='') as f:
 out=csv.writer(f,lineterminator='\\n');out.writerow(['customer','paid_total'])
 for customer,value in sorted(total.items()):out.writerow([customer,f'{value:.2f}'])
""",
    "v04_data_tax": """import csv
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
total=defaultdict(lambda:Decimal('0'))
with open('invoices.csv',encoding='utf-8',newline='') as f:
 for row in csv.DictReader(f):
  if row['status']=='paid':total[row['region']]+=(Decimal(row['net'])*(1+Decimal(row['tax_rate']))).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)
with open('summary.csv','w',encoding='utf-8',newline='') as f:
 out=csv.writer(f,lineterminator='\\n');out.writerow(['region','gross'])
 for region,value in sorted(total.items()):out.writerow([region,f'{value:.2f}'])
""",
    "v04_continue_export": "def export_rows(rows):\n    return 'id,name\\n' + ''.join(f'{i},{name}\\n' for i,name in sorted(rows))\n",
    "v04_continue_failures": "def parse_int(text):\n    return int(text.strip())\n",
}


OUTPUTS = {
    "v04_recover_missing": "RESET-42",
    "v04_recover_invalid": json.dumps("CURRENT"),
    "v04_recover_absent": "READY-7",
    "v04_revision_status": json.dumps({"included_statuses":["active"],"total":7}),
    "v04_revision_currency": json.dumps({"currency":"USD","total":"3.55"}),
    "v04_revision_schema": json.dumps({"active_names":["Ada","Zoe"]}),
}


def variation_fixture_model(case_id: str) -> ScriptedModel:
    contract = CONTRACTS[case_id]
    decisions = []
    if contract.required_failed_read:
        decisions.append(_act("read_file", path=contract.required_failed_read))
    if contract.required_failed_process:
        decisions.append(_act("process_exec", argv=list(contract.required_failed_process)))
    for path in contract.required_reads:
        if path != contract.artifact or path in {"stale.json", "canonical.json"}:
            decisions.append(_act("read_file", path=path))
    if case_id in SOLUTIONS:
        path = contract.editable[0]
        decisions.append(_act("write_file", path=path, content=SOLUTIONS[case_id],
                              overwrite=case_id.startswith(("v04_repair_", "v04_continue_"))))
        decisions.append(_act("process_exec", argv=["python", contract.required_process]))
        if case_id.startswith("v04_data_"):
            decisions.append(_act("process_exec", argv=["python", "check.py"]))
        if contract.artifact != path:
            decisions.append(_act("read_file", path=contract.artifact))
    else:
        decisions.append(_act("write_file", path=contract.artifact, content=OUTPUTS[case_id]))
        decisions.append(_act("read_file", path=contract.artifact))
    decisions.append(FinalResponse(content="Fixture contract completed", evidence_ids=[f"a{len(decisions)}"]))
    return ScriptedModel(decisions)
