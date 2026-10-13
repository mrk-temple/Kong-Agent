"""Fixed development traces: known solutions test mechanisms, never model ability."""
from collections import deque

from kong.contracts import Act, Action, FinalResponse, NeedUserInput, ProgressAssessment
from kong.models.base import ModelError


MONEY = """from decimal import Decimal, ROUND_HALF_UP
def money(line):
    if line is None or line.get('status') == 'cancelled':
        return Decimal('0.00')
    return (Decimal(str(line['price'])) * line['quantity']).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
"""
INVOICE = """from decimal import Decimal
from money import money
USER_NOTE = 'keep my existing integration note'
def total(lines):
    return sum((money(x) for x in lines), Decimal('0.00'))
"""
ADDED = """from decimal import Decimal
from invoice import total
assert total([None, {'price':'1.005','quantity':1}]) == Decimal('1.01')
assert total([{'price':'3.335','quantity':-2}]) == Decimal('-6.67')
print('regression checks passed')
"""
PIPELINE = """import csv
from collections import defaultdict
from decimal import Decimal
latest={}
with open('orders.csv', encoding='utf-8', newline='') as stream:
    for row in csv.DictReader(stream): latest[row['id']]=row
totals=defaultdict(int)
for row in latest.values():
    if not row['region'] or row['status'] not in {'paid','refund'}: continue
    totals[row['region']]+=int(row['amount_cents'])*(-1 if row['status']=='refund' else 1)
rows=[(k, f'{Decimal(v)/100:.2f}') for k,v in sorted(totals.items())]
with open('summary.csv','w',encoding='utf-8',newline='') as stream:
    out=csv.writer(stream,lineterminator='\\n');out.writerow(['region','net_amount']);out.writerows(rows)
report='# Data report\\n\\n## Rules\\nSource orders.csv; last physical row per id wins; signed refunds are subtracted; empty regions are skipped.\\n\\n## Results\\n'
report+=''.join(f'{k}: {v}\\n' for k,v in rows)
report+=f"TOTAL: {sum((Decimal(v) for _,v in rows),Decimal(0)):.2f}\\n\\n## Unverified\\nExternal behavior is unverified.\\n"
with open('report.md','w',encoding='utf-8') as stream: stream.write(report)
"""


def act(name, **arguments):
    return Act(actions=[Action(name=name, arguments=arguments)])


class Fixture:
    def __init__(self, decisions):
        self.decisions = deque(decisions)

    async def generate(self, context):
        if any('"phase":"assess"' in message["content"] for message in context.messages if message["content"].startswith("RUNTIME CONTROL STATE:")):
            return ProgressAssessment(diagnosis="Inspect remaining current artifacts within the available progress or bounded recovery window.", next_strategy="recover")
        if not self.decisions:
            raise ModelError("Long-task fixture exhausted; no fallback")
        decision = self.decisions.popleft()
        if isinstance(decision, FinalResponse):
            # Runtime only accepts real current observations, never invented evidence IDs.
            decision.evidence_ids = context.working_set.important_observation_ids
        return decision


def fixture_model(scenario, number):
    from .long_driver import HANDOFF_QUESTION, completion_contract
    reads = [act("read_file", path=p) for p, _ in scenario.files]
    if scenario.family == "code":
        writes = [act("write_file", path="task/money.py", content=MONEY, overwrite=True),
                  act("write_file", path="task/invoice.py", content=INVOICE, overwrite=True),
                  act("write_file", path="task/test_added.py", content=ADDED, overwrite=True)]
        readback = [act("read_file", path=p) for p in scenario.artifacts]
    else:
        writes = [act("write_file", path="task/process.py", content=PIPELINE, overwrite=True)]
        readback = [act("read_file", path=p) for p in scenario.artifacts]
    command = completion_contract(scenario).commands[0]
    checks = [act("process_exec", argv=command.argv, cwd="task", fresh_python=True)]
    if scenario.family == "data" and number == 3:
        # The first execution regenerates appended-input outputs; only the next
        # stable execution proves their current versions under the RC5 contract.
        checks += [act("process_exec", argv=command.argv, cwd="task", fresh_python=True)]
    decisions = reads + writes + checks + readback
    if number < 3:
        decisions += [NeedUserInput(question=HANDOFF_QUESTION)]
        if number == 1:
            decisions += [act("read_file", path=scenario.artifacts[0]), NeedUserInput(question=HANDOFF_QUESTION)]
    else:
        decisions += [FinalResponse(content="Fixed fixture delivery; independent checks still required.")]
    return Fixture(decisions)
