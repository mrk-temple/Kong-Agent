"""Chat Completions transport for DeepSeek, relays and local compatible servers.

Decisions use a JSON envelope, avoiding provider-specific function calling dialects.
One generate call is one HTTP request; no invisible retries or router calls.
"""
import asyncio
import codecs
import json
import httpx
from pydantic import ValidationError

from kong.config import ModelConfig
from kong.context import Context
from kong.continuity.contracts import ContextPacket
from kong.contracts import DECISION_ADAPTER, Decision
from kong.models.base import ModelError, ModelProtocolError
from kong.models.diagnostics import protocol_diagnostics


class CompatibleModel:
    def __init__(self, config: ModelConfig, client: httpx.AsyncClient | None = None):
        self.config = config
        self._client = client
        self._memory_proposal = None
        self.last_usage = {}
        self.on_stream = lambda data: None

    def take_memory_delta(self):
        proposal, self._memory_proposal = self._memory_proposal, None
        return proposal

    async def generate(self, context: Context | ContextPacket) -> Decision:
        self._memory_proposal = None
        payload = {"model": self.config.model, "messages": context.messages,
                   "stream": self.config.stream, "max_tokens": self.config.max_tokens}
        if self.config.stream:
            payload["stream_options"] = {"include_usage": True}
        self.last_usage = {}
        if self.config.enable_thinking is not None:
            payload["enable_thinking"] = self.config.enable_thinking
        if self.config.thinking_mode is not None:
            payload["thinking"] = {"type": self.config.thinking_mode}
        if self.config.json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        key = self.config.api_key.get_secret_value()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        endpoint = self.config.base_url + "/chat/completions"
        try:
            if self._client:
                body = await self._request(self._client, endpoint, payload, headers)
            else:
                async with httpx.AsyncClient(follow_redirects=False) as client:
                    body = await self._request(client, endpoint, payload, headers)
        except (httpx.TimeoutException, TimeoutError):
            raise ModelError("Model request timed out. Check the service or raise the profile timeout.", category="timeout", retryable=True) from None
        except httpx.HTTPError:
            raise ModelError("Cannot connect to the model service. Check base_url and network.", category="connection", retryable=True) from None
        return self._parse(body)

    def _check_status(self, response):
        if response.status_code != 200:
            hints = {401: "Check the API key environment variable.", 403: "Access denied by the provider.",
                     402: "Check account payment/quota status with the provider.",
                     404: "Check base_url and model name.", 429: "Provider rate limit or quota reached.",
                     400: "Check model name and JSON mode support (json_mode=false for unsupported servers)."}
            status = response.status_code
            category = {400:"configuration", 401:"authentication", 402:"billing", 403:"access", 404:"configuration",
                        408:"timeout", 429:"rate_limit"}.get(status, "configuration" if 300 <= status < 500 else "service_http")
            if status == 429:
                try:
                    body = response.json()
                    error = body.get("error") if isinstance(body, dict) else None
                    code = error.get("code") if isinstance(error, dict) else None
                    if isinstance(code, str) and code in {"insufficient_quota", "insufficient_balance", "quota_exceeded"}:
                        category = "billing"
                except ValueError:
                    pass
            try:
                delay = float(response.headers.get("retry-after", "0"))
            except ValueError:
                delay = 0
            raise ModelError(f"Model service HTTP {status}. " + hints.get(status, "Check provider availability."),
                             category=category, retryable=category != "billing" and (status in {408, 429} or status >= 500),
                             status_code=status, retry_after=delay)

    async def _request(self, client, endpoint, payload, headers):
        # Total wall-clock deadline also bounds a server sending endless heartbeat lines.
        async with asyncio.timeout(self.config.timeout):
            if not self.config.stream:
                response = await client.post(endpoint, json=payload, headers=headers, timeout=self.config.timeout)
                self._check_status(response)
                try:
                    return response.json()
                except ValueError:
                    raise ModelProtocolError("Invalid provider response JSON.") from None
            async with client.stream("POST", endpoint, json=payload, headers=headers, timeout=self.config.timeout) as response:
                if response.status_code != 200:
                    await response.aread()
                    self._check_status(response)
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    await response.aread()
                    try:
                        return response.json()
                    except ValueError:
                        raise ModelProtocolError("Invalid provider response JSON.") from None
                return await self._read_stream(response)

    async def _read_stream(self, response):
        parts, usage, finish = [], {}, None
        size, received, done, lines = 0, 0, False, []
        async for line in self._bounded_lines(response):
            if line.startswith("data:"):
                lines.append(line[5:].lstrip())
            elif line == "" and lines:
                value, lines = "\n".join(lines), []
                if value == "[DONE]":
                    done = True
                    break
                try:
                    chunk = json.loads(value)
                    if not isinstance(chunk, dict) or "error" in chunk:
                        raise ValueError()
                    if isinstance(chunk.get("usage"), dict):
                        usage = chunk["usage"]
                    choices = chunk.get("choices", [])
                    for choice in choices:
                        if choice.get("index", 0) != 0:
                            continue
                        delta = choice.get("delta", {})
                        content = delta.get("content") or ""
                        reasoning = delta.get("reasoning_content") or ""
                        if not isinstance(content, str) or not isinstance(reasoning, str):
                            raise ValueError()
                        size += len(content)
                        received += len(content) + len(reasoning)
                        if received > 1_000_000:
                            raise ModelError("Model stream exceeded the bounded response limit.", category="configuration")
                        parts.append(content)
                        finish = choice.get("finish_reason") or finish
                        if content or reasoning:
                            # Raw fragments are unvalidated and may contain secrets or partial actions.
                            self.on_stream({"received_chars": received, "decision_chars": size})
                except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                    raise ModelProtocolError("Invalid provider stream event.") from None
        self.last_usage = {k: v for k, v in usage.items()
                           if k in {"prompt_tokens", "completion_tokens", "total_tokens"} and type(v) is int and v >= 0}
        if not done or finish != "stop":
            raise ModelProtocolError("Incomplete model stream; no decision authorized.")
        return {"choices": [{"message": {"content": "".join(parts)}, "finish_reason": finish}], "usage": usage}

    async def _bounded_lines(self, response):
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        buffered, byte_count = "", 0
        try:
            async for data in response.aiter_bytes():
                byte_count += len(data)
                if byte_count > 4_000_000:
                    raise ModelProtocolError("Provider stream exceeds the response byte limit.")
                buffered += decoder.decode(data)
                while "\n" in buffered:
                    line, buffered = buffered.split("\n", 1)
                    if len(line) > 128000:
                        raise ModelProtocolError("Provider stream line exceeds the bounded event limit.")
                    yield line.rstrip("\r")
                if len(buffered) > 128000:
                    raise ModelProtocolError("Provider stream line exceeds the bounded event limit.")
            buffered += decoder.decode(b"", final=True)
            if buffered:
                yield buffered.rstrip("\r")
        except UnicodeError:
            raise ModelProtocolError("Provider stream is not valid UTF-8.") from None

    def _parse(self, body):
        try:
            if not isinstance(body, dict):
                raise ValueError("Expected response object")
            usage = body.get("usage", {})
            if isinstance(usage, dict):
                self.last_usage = {k:v for k,v in usage.items()
                                   if k in {"prompt_tokens", "completion_tokens", "total_tokens"} and isinstance(v, int)}
            choice = body["choices"][0]
            if not isinstance(choice, dict):
                raise ValueError("Expected choice object")
            if choice.get("finish_reason") == "length":
                raise ModelError("Model output was truncated; raise max_tokens or simplify the request.")
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ModelError("Model returned empty content; check JSON output support.")
            content = content.strip()
            if content.startswith("```json\n") and content.endswith("```"):
                content = content[8:-3].strip()
            envelope = json.loads(content)
            if not isinstance(envelope, dict):
                raise ValueError("Decision must be an object")
            proposal = envelope.pop("memory_delta", None)
            from kong.runtime.metrics import measure
            with measure("validation_seconds"):
                decision = DECISION_ADAPTER.validate_python(envelope)
            # Auxiliary memory is validated by the ContextEngine after normal
            # runtime handling. An invalid delta must not invalidate a decision.
            self._memory_proposal = proposal
            return decision
        except (ValueError, KeyError, IndexError, TypeError, ValidationError) as exc:
            diagnostics = protocol_diagnostics(exc)
            detail = ", ".join(d["path"] + ":" + d["code"] + ("; " + d["hint"] if "hint" in d else "") for d in diagnostics)
            raise ModelProtocolError(f"Invalid Kong decision JSON ({detail}). Return exactly one decision matching the supplied schema, without extra fields.", diagnostics) from None
