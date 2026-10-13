"""Trace the actual validated packet; transport receipts originate in CompatibleModel."""

import hashlib
import json
from uuid import uuid4


def message_hash(messages):
    return hashlib.sha256(
        json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


class TracedModel:
    def __init__(self, model, sink):
        self.model, self.sink = model, sink
        self.engine = None

    def __getattr__(self, name):
        return getattr(self.model, name)

    @property
    def on_stream(self):
        return getattr(self.model, "on_stream", None)

    @on_stream.setter
    def on_stream(self, value):
        self.model.on_stream = value

    async def generate(self, packet):
        request_id = uuid4().hex
        selected = []
        for item in getattr(packet, "items", []):
            for source in item.sources:
                if (
                    source.kind == "memory"
                    and source.path
                    and source.path.startswith("memory/")
                ):
                    _, profile, record, version = source.path.split("/")
                    selected.append({"record_id": record, "version": int(version[1:])})
        context = getattr(self.engine, "last_memory_context", None)
        base = {"selected": selected, "message_sha256": message_hash(packet.messages)}

        def emit(kind, data):
            self.sink(
                kind, {**base, **data}, request_id, packet.run_id, packet.thread_id
            )

        emit(
            "memory.retrieved",
            {
                "records": [
                    {"record_id": r.record_id, "version": r.current.version}
                    for r in context.records
                ]
                if context
                else [],
                "query_status": getattr(packet, "memory_query_status", None),
            },
        )
        emit(
            "memory.selected",
            {
                "input_estimate": getattr(packet, "estimated_input_tokens", None),
                "dropped_count": len(getattr(packet, "dropped", [])),
            },
        )
        emit(
            "memory.context_assembled",
            {"input_estimate": getattr(packet, "estimated_input_tokens", None)},
        )
        dispatched, delivered = False, False

        def receipt(stage, data):
            nonlocal dispatched, delivered
            dispatched = dispatched or stage == "dispatched"
            delivered = delivered or stage == "delivered"
            emit("memory.request_" + stage, data)

        if hasattr(self.model, "on_request"):
            self.model.on_request = receipt
        try:
            decision = await self.model.generate(packet)
        except BaseException as error:
            emit(
                "memory.request_failed"
                if hasattr(self.model, "on_request")
                else "application.generation_failed",
                {
                    "category": getattr(error, "category", "interrupted"),
                    "delivery_known": "yes"
                    if delivered
                    else "unknown"
                    if dispatched
                    else "no",
                    "stage": "protocol"
                    if getattr(error, "category", None) == "protocol"
                    else "request",
                },
            )
            raise
        return decision
