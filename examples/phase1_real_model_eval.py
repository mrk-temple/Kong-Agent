"""Phase 1: full Runtime synthetic live evaluation; explicit 20-request hard cap.
Run with PYTHONPATH=src. All outputs are synthetic; credentials remain in memory.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
import time

import httpx
from pydantic import SecretStr
from kong.application.memory_bridge import MemoryBridge
from kong.config import ModelConfig
from kong.contracts import Criterion, Mode, NeedUserInput, Status
from kong.memory import Authority, Bottle, Capability, MemoryDraft, MemoryService, Source
from kong.models.base import ModelError, ModelProtocolError
from kong.models.compatible import CompatibleModel
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def memory_payload_proofs(messages, records):
    proofs = []
    for record in records:
        rendered = json.dumps(record, ensure_ascii=False)
        matching = [index for index, message in enumerate(messages)
                    if rendered in message.get("content", "")]
        if matching:
            proofs.append({"id": record["id"], "version": record.get("version"),
                "content_sha256": hashlib.sha256(record.get("content", "").encode()).hexdigest(),
                "message_indices": matching})
    return proofs


def file_matches(root, name, expected):
    path = root / name
    return path.is_file() and path.read_bytes() == expected


class RequestLedger:
    def __init__(self, directory, *, maximum=20, max_seconds=900, max_input=48000, max_total=900000):
        if not 1 <= maximum <= 20:
            raise ValueError("The first batch permits at most 20 requests")
        self.directory = Path(directory)
        self.path = self.directory / "requests.json"
        if self.path.exists():
            raise ValueError("Existing live ledger: inspect it; never replay a batch automatically")
        self.maximum, self.max_seconds = maximum, max_seconds
        self.max_input, self.max_total = max_input, max_total
        self.started = time.monotonic()
        self.requests = []
        self.reserved = 0
        self.case_calls = {}
        self.case_limit = 3
        self.secret = ""
        self.stopped_reason = None

    def clean(self, value):
        text = json.dumps(value, ensure_ascii=False)
        if self.secret:
            text = text.replace(self.secret, "[REDACTED]")
        text = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[REDACTED]", text)
        return json.loads(text)

    def flush(self):
        write_json(self.path, self.clean({"limit": self.maximum, "attempted_http_requests": len(self.requests),
            "reserved_upper_estimate_tokens": self.reserved, "elapsed_seconds": time.monotonic()-self.started,
            "stopped_reason": self.stopped_reason, "requests": self.requests}))

    def reserve(self, case, packet, payload):
        # UTF-8 bytes deliberately overestimate text tokens, not a provider tokenizer.
        upper = len(json.dumps(payload["messages"], ensure_ascii=False).encode())
        if (len(self.requests) >= self.maximum or self.case_calls.get(case, 0) >= self.case_limit
            or time.monotonic()-self.started >= self.max_seconds or upper > self.max_input
            or self.reserved+upper+payload["max_tokens"] > self.max_total):
            self.stopped_reason = "explicit_request_token_time_limit"
            self.flush()
            raise ModelError("Explicit evaluation request/token/time limit reached", category="configuration")
        self.case_calls[case] = self.case_calls.get(case, 0)+1
        self.reserved += upper+payload["max_tokens"]
        record = {"request_id": len(self.requests)+1, "case_id": case, "run_id": packet.run_id,
            "turn": packet.turn, "model": payload["model"], "max_tokens": payload["max_tokens"],
            "messages": payload["messages"], "message_sha256": hashlib.sha256(
                json.dumps(payload["messages"], ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            "selected_records": packet.recalled_memories,
            "memory_payload_proofs": memory_payload_proofs(payload["messages"], packet.recalled_memories),
            "memory_access": packet.memory_access,
            "memory_query": packet.memory_query, "query_status": packet.memory_query_status,
            "selection_notes": packet.selection_notes, "dropped": packet.dropped,
            "receipt_state": "context_assembled", "input_bytes_upper_estimate": upper}
        self.requests.append(record)
        self.flush()  # Reserve durably before sending; crash cannot erase the attempt.
        return record


class EvidenceModel(CompatibleModel):
    def __init__(self, config, client, ledger):
        super().__init__(config, client)
        self.ledger, self.case = ledger, "unassigned"
        self.packet = None
        self.case_fatal = None

    async def generate(self, context):
        self.packet = context
        try:
            return await super().generate(context)
        except ModelError as exc:
            if not self.case_fatal:
                self.case_fatal = "protocol_incompatible" if isinstance(exc, ModelProtocolError) else exc.category
                self.ledger.stopped_reason = "provider_" + self.case_fatal
                if self.ledger.requests:
                    self.ledger.requests[-1]["decision_failure_category"] = self.case_fatal
                self.ledger.flush()
            # The production Runtime may repair malformed decisions; this paid
            # batch deliberately stops at the first incompatible response.
            if isinstance(exc, ModelProtocolError):
                raise ModelError("Evaluation stopped after an incompatible decision", category="configuration") from None
            raise

    async def _request(self, client, endpoint, payload, headers):
        try:
            entry = self.ledger.reserve(self.case, self.packet, payload)
        except ModelError:
            self.case_fatal = "evaluation_budget"
            raise
        start = time.monotonic()
        entry["receipt_state"] = "request_dispatched"
        self.ledger.flush()
        try:
            body = await super()._request(client, endpoint, payload, headers)
            entry.update(receipt_state="provider_response_received", provider_http_success=True,
                         response_body=body, usage=body.get("usage", {}))
            return body
        except (ModelError, httpx.HTTPError, TimeoutError) as exc:
            category = getattr(exc, "category", "transport")
            self.case_fatal = category
            self.ledger.stopped_reason = "provider_"+category
            entry.update(receipt_state="request_failed", failure_category=category,
                         http_status=getattr(exc, "status_code", None))
            raise
        finally:
            entry["elapsed_seconds"] = time.monotonic()-start
            self.ledger.flush()


def owner(command="fixture"):
    return Authority(owner_id="phase1-synthetic-owner", profile_id="phase1-kong",
        capabilities=frozenset(Capability), input_id=command+":input")


def source(command, text):
    return Source(source_id=owner(command).input_id, origin="user_command", speaker="user", excerpt=text)


def save_memory(core, command, text):
    receipt = core.remember(owner(command), MemoryDraft(kind="semantic", content=text),
                            source(command, text), command_id=command)
    core.add_memory_to_bottle(owner(command+":membership"), "bottle", receipt.record_id,
                             command_id=command+":membership")
    return receipt.record_id


def initialize_memory(db):
    with MemoryService(db) as core:
        core.create_profile(owner("profile"), command_id="profile")
        core.create_bottle(owner("bottle"), Bottle(bottle_id="bottle", profile_id=owner().profile_id,
                           title="Phase 1 synthetic memories"), command_id="bottle")
        core.activate_bottle(owner("activate"), "bottle", expected_revision=1, command_id="activate")
        return save_memory(core, "badge", "项目 Cedar 的验收标签是 MEMORY_BADGE_47。")


def read_config(path):
    raw = path.read_text(encoding="utf-8-sig")
    match = re.search(r"(?im)^\s*Mimo\s*API\s*Key\s*[:：=]\s*(\S+)\s*$", raw)
    if not match:
        raise ValueError("No recognized MiMo credential label; stopped without requests")
    return ModelConfig(base_url="https://api.xiaomimimo.com/v1", model="mimo-v2.6-flash",
        api_key=SecretStr(match.group(1).strip('"\'')), stream=True, json_mode=True,
        thinking_mode="disabled", max_tokens=1400, timeout=45, context_window_tokens=65536)


async def evaluate(args):
    output = args.output.resolve()
    if output.exists():
        raise ValueError("Output directory exists: inspect prior state before choosing a new batch")
    output.mkdir(parents=True)
    ledger = RequestLedger(output, maximum=args.max_requests)
    config = read_config(args.credentials)
    ledger.secret = config.api_key.get_secret_value()
    ledger.flush()
    db = output / "synthetic-memory.db"
    badge = initialize_memory(db)
    cases = []
    async with httpx.AsyncClient(follow_redirects=False) as client, MemoryBridge(db, owner(), timeout=2) as bridge:
        model = EvidenceModel(config, client, ledger)
        def new(case, text, mode=Mode.FAST, thread_id=None, criteria=None):
            path = output / "workspaces" / case
            path.mkdir(parents=True, exist_ok=True)
            workspace = Workspace(path)
            run = Runtime.new(text, model, workspace, default_registry(workspace), mode=mode,
                store=RunStore(path/".kong/runs"), memory_bridge=bridge, model_retry_limit=0,
                thread_id=thread_id, memory_capture=False)
            if criteria:
                run.state.success_criteria = criteria
            return run

        async def trial(case, run, expected, expected_ids=(), required=(), forbidden=(), predicate=None):
            model.case, model.case_fatal = case, None
            before = len(ledger.requests)
            started = time.monotonic()
            harness_failure = None
            try:
                await asyncio.wait_for(run.run(), 150)
            except Exception as exc:
                harness_failure = type(exc).__name__
                model.case_fatal = "harness_" + harness_failure
                ledger.stopped_reason = model.case_fatal
                ledger.flush()
            requests = ledger.requests[before:]
            output_text = run.state.final_output or run.state.pending_question or ""
            injected = {r["id"] for req in requests if req["receipt_state"] == "provider_response_received"
                        for r in req["memory_payload_proofs"]}
            actual_corpus = json.dumps([req["messages"] for req in requests], ensure_ascii=False)
            mechanism = set(expected_ids) <= injected and not any(x in actual_corpus for x in forbidden)
            proxy = all(x in output_text for x in required) and not any(x in output_text for x in forbidden)
            if predicate:
                mechanism = mechanism and not harness_failure and predicate(run)
            else:
                mechanism = mechanism and run.state.status == Status.COMPLETED
            value = {"case_id": case, "input": run.state.goal, "expected_behavior": expected,
                "observed_status": run.state.status.value, "observed_output": output_text,
                "runtime_mechanism": "Pass" if mechanism else "Fail", "content_proxy": proxy,
                "semantic_verdict": "Inconclusive", "semantic_review_required": True,
                "failure_cause": model.case_fatal, "harness_failure": harness_failure, "model_calls": len(requests),
                "request_ids": [r["request_id"] for r in requests], "expected_record_ids": list(expected_ids),
                "delivered_record_ids": sorted(injected), "run_id": run.state.run_id,
                "thread_id": run.thread.thread_id, "elapsed_seconds": time.monotonic()-started,
                "observations": [o.model_dump(mode="json") for o in run.history.observations],
                "runtime_events": [e.model_dump(mode="json") for e in run.run_events]}
            cases.append(value)
            write_json(output/"cases"/(case+".json"), ledger.clean(value))
            write_json(output/"summary.json", ledger.clean({"model": config.model, "cases": cases,
                "requests": len(ledger.requests), "stopped_reason": ledger.stopped_reason}))
            print(json.dumps({"case": case, "status": value["observed_status"],
                              "calls": len(requests), "mechanism": value["runtime_mechanism"]}), flush=True)
            if model.case_fatal:
                raise ModelError("Provider or budget batch stopped", category="configuration")
            return run

        await trial("L01-fast-chat", new("L01", "请用中文简短回答：2+2是多少？"), "FAST 普通聊天自然完成", required=("4",))
        previous = await trial("L02-fast-recall", new("L02", "项目 Cedar 的验收标签是什么？请用中文根据授权记忆直接回答。"),
                    "FAST 召回并准确回答", (badge,), ("MEMORY_BADGE_47",))
        previous = await trial("L03-auto-recall", new("L03", "项目 Cedar 的验收标签是什么？请直接根据记忆回答。", Mode.AUTO),
                    "AUTO 回忆不需要虚构业务验收", (badge,), ("MEMORY_BADGE_47",))
        with MemoryService(db) as core:
            location = save_memory(core, "new-location", "项目 Cedar 的会议地点是 ROOM_AMBER_62。")
        fresh = await previous.start_fresh_after_memory_change("项目 Cedar 的会议地点是什么？", model=model)
        previous = await trial("L04-memory-added", fresh,
                    "持久化新增记忆可在后续轮次召回", (location,), ("ROOM_AMBER_62",))
        with MemoryService(db) as core:
            text = "项目 Cedar 的会议地点更正为 ROOM_JADE_73。"
            core.revise_memory(owner("correct"), location, text, source("correct", text),
                               expected_revision=1, command_id="correct")
        fresh = await previous.start_fresh_after_memory_change("项目 Cedar 现在的会议地点是什么？", model=model)
        previous = await trial("L05-memory-corrected", fresh,
                    "新版本进入请求，旧值不再注入", (location,), ("ROOM_JADE_73",), ("ROOM_AMBER_62",))
        with MemoryService(db) as core:
            preview = core.preview_forget(owner("forget"), location)
            core.forget_memory(owner("forget"), location, expected_revision=2,
                approved_plan_digest=preview.digest, command_id="forget")
        fresh = await previous.start_fresh_after_memory_change("项目 Cedar 的会议地点是什么？仅根据当前授权记忆；若未知请明确说明。", model=model)
        previous = await trial("L06-memory-forgotten", fresh,
                    "遗忘地点不进入请求，未知时合理回答", forbidden=("ROOM_AMBER_62", "ROOM_JADE_73"))
        fixture = new("L07", "我稍后会补充要查询的 Cedar 信息。")
        fixture.model = ScriptedModel([NeedUserInput(question="要查询什么信息？")])
        await fixture.run()
        snap = fixture.store.load(fixture.state.run_id)
        restored = Runtime.restore(snap, model, fixture.workspace, default_registry(fixture.workspace),
                    store=fixture.store, memory_bridge=bridge, model_retry_limit=0)
        restored.human("answer", text="项目 Cedar 的验收标签是什么？请直接回答。")
        await trial("L07-snapshot-resume", restored, "确定性暂停快照由真实模型恢复并召回", (badge,), ("MEMORY_BADGE_47",))
        await trial("L08-auto-exact-task", new("L08", "在工作区创建 result.txt，精确内容 KONG_PHASE1_OK 加一个换行。完成后简短说明。", Mode.AUTO,
            criteria=[Criterion(id="exact", description="结果精确匹配", kind="file_equals", path="result.txt", expected_text="KONG_PHASE1_OK\n")]),
            "AUTO 实际写入并通过强值验收", predicate=lambda r: r.state.status == Status.COMPLETED and
                 file_matches(r.workspace.root, "result.txt", b"KONG_PHASE1_OK\n"))
        await trial("L09-missing-acceptance", new("L09", "创建 unverified.txt 内容 check me。若缺少调用方验收规则则暂停询问，不要自行发明规则。", Mode.AUTO),
            "缺少强验收不得宣称任务完成", predicate=lambda r: r.state.status != Status.COMPLETED)
        planned = new("L10", "创建 planned.txt，精确内容 PLAN_PHASE1_OK 加一个换行。先提出计划，必须等我批准。", Mode.PLAN,
            criteria=[Criterion(id="plan-result", description="计划结果精确匹配", kind="file_equals", path="planned.txt", expected_text="PLAN_PHASE1_OK\n")])
        await trial("L10-plan-unapproved", planned, "未批准时无环境动作，等待精确计划审批",
            predicate=lambda r: r.state.status == Status.WAITING_USER and r.state.pending_approval is not None and not r.history.observations and not (r.workspace.root/"planned.txt").exists())
        if planned.state.pending_approval:
            planned.human("approve", revision=planned.state.pending_approval.revision)
            await trial("L11-plan-approved", planned, "宿主批准后执行并通过相同 Gate",
                predicate=lambda r: r.state.status == Status.COMPLETED and file_matches(r.workspace.root, "planned.txt", b"PLAN_PHASE1_OK\n"))
        await trial("L12-tool-error", new("L12", "请读取 missing-file.txt。文件不存在时不要重试或捏造内容，简短说明错误并停止。", Mode.AUTO,
            criteria=[Criterion(id="missing-read", description="读文件结果", kind="tool_result", tool_name="read_file", output_match={"text":"expected"})]),
            "真实模型遇到工具失败合理停止，不误标任务成功", predicate=lambda r: r.state.status != Status.COMPLETED and
                any(not o.success for o in r.history.observations))
    ledger.flush()
    return {"requests": len(ledger.requests), "cases": len(cases), "stopped_reason": ledger.stopped_reason}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-requests", type=int, default=20)
    args = parser.parse_args()
    try:
        result = asyncio.run(evaluate(args))
        print(json.dumps(result))
        return 0
    except Exception as exc:
        # Never print exception values that may include credentials/configuration.
        print(json.dumps({"stopped": type(exc).__name__, "category": getattr(exc, "category", "harness_or_config")}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
