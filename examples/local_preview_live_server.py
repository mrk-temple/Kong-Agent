"""New paid evaluator. Human approval is required; an old ledger is never auto-replayed."""

import argparse
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
import uvicorn
import kong.application.service as application
from kong.application.local_cli import model_configuration, prepare_environment
from kong.application.tracing import message_hash
from kong.local_api.app import create_app
from kong.models.compatible import CompatibleModel
from kong.models.base import ModelError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--resume-ledger", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    prepare_environment(output)
    output.mkdir(parents=True, exist_ok=True)
    identity = (
        Path(__file__).resolve().parents[1]
        / "docs/local-preview-0.0.0/evidence/phase2-source-manifest.json"
    )
    frozen = json.loads(identity.read_text(encoding="utf-8"))["fingerprint_sha256"]
    prior_paths = [
        p for p in output.parent.glob("live-phase2-*/ledger.json") if p.parent != output
    ]
    prior_requests = [
        r
        for p in prior_paths
        for r in json.loads(p.read_text(encoding="utf-8"))["requests"]
    ]
    prior_count = len(prior_requests)
    prior_reserved = sum(r["reserved_tokens"] for r in prior_requests)
    config = model_configuration(SimpleNamespace(api_file=args.api_file))
    path = output / "ledger.json"
    if path.exists():
        if not args.resume_ledger:
            raise RuntimeError("Ledger exists; no automatic paid replay")
        ledger = json.loads(path.read_text(encoding="utf-8"))
        if ledger["source_identity"] != frozen:
            raise RuntimeError("Source changed")
    else:
        ledger = {
            "source_identity": frozen,
            "authorization": "2026-10-12 human approved MiMo <=10,000,000 tokens",
            "caps": {
                "requests": 20,
                "reservation_tokens": min(2000000, 10000000 - prior_reserved),
                "seconds": 900,
                "per_run": 8,
            },
            "started_unix": time.time(),
            "requests": [],
        }

    def persist():
        temporary = path.with_suffix(".new")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(ledger, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)

    ledger["prior_round"] = {
        "ledgers": [str(p) for p in prior_paths],
        "requests": prior_count,
        "reserved_tokens": prior_reserved,
    }
    persist()
    secret = config.api_key.get_secret_value()

    def clean(value):
        return json.loads(
            json.dumps(value, ensure_ascii=False).replace(secret, "[redacted]")
        )

    class BoundedModel(CompatibleModel):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.calls = 0
            self.row = None

        def _check_status(self, response):
            if self.row:
                self.row["http_status"] = response.status_code
                persist()
            return super()._check_status(response)

        async def _request(self, client, endpoint, payload, headers):
            self.calls += 1
            reserve = (
                max(
                    len(json.dumps(payload["messages"], ensure_ascii=False).encode())
                    + 4096,
                    self.config.context_window_tokens,
                )
                + payload["max_tokens"]
            )
            if (
                self.calls > 8
                or len(ledger["requests"]) >= ledger["caps"]["requests"]
                or sum(r["reserved_tokens"] for r in ledger["requests"]) + reserve
                > ledger["caps"]["reservation_tokens"]
                or time.time() - ledger["started_unix"] > 900
            ):
                raise ModelError(
                    "Explicit live evaluation budget reached; no dispatch.",
                    category="budget",
                )
            self.row = {
                "number": len(ledger["requests"]) + 1,
                "state": "reserved_before_send",
                "reserved_tokens": reserve,
                "started_unix": time.time(),
                "message_sha256": message_hash(payload["messages"]),
                "model": payload["model"],
                "messages": clean(payload["messages"]),
                "usage": {},
            }
            ledger["requests"].append(self.row)
            persist()
            try:
                body = await super()._request(client, endpoint, payload, headers)
                self.row.update(
                    state="response_received",
                    finished_unix=time.time(),
                    response=clean(body),
                    usage={
                        k: v
                        for k, v in body.get("usage", {}).items()
                        if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
                        and type(v) is int
                        and v >= 0
                    },
                )
                persist()
                return body
            except BaseException as error:
                self.row.update(
                    state="interrupted_or_failed",
                    finished_unix=time.time(),
                    category=getattr(error, "category", type(error).__name__),
                )
                persist()
                raise

    application.CompatibleModel = BoundedModel
    app = create_app(
        output / "data",
        origin=f"http://127.0.0.1:{args.port}",
        token="live-evaluator-pairing",
        model_config=config,
    )
    uvicorn.run(
        app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning"
    )


if __name__ == "__main__":
    main()
