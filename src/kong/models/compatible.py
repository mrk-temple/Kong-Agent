"""Chat Completions transport for DeepSeek, relays and local compatible servers.

Decisions use a JSON envelope, avoiding provider-specific function calling dialects.
One generate call is one HTTP request; no invisible retries or router calls.
"""
import json
import httpx
from pydantic import ValidationError

from kong.config import ModelConfig
from kong.context import Context
from kong.continuity.contracts import ContextPacket
from kong.contracts import DECISION_ADAPTER, Decision
from kong.models.base import ModelError, ModelProtocolError


class CompatibleModel:
    def __init__(self, config: ModelConfig, client: httpx.AsyncClient | None = None):
        self.config = config
        self._client = client
        self._memory_proposal = None
        self.last_usage = {}

    def take_memory_delta(self):
        proposal, self._memory_proposal = self._memory_proposal, None
        return proposal

    async def generate(self, context: Context | ContextPacket) -> Decision:
        self._memory_proposal = None
        payload = {"model": self.config.model, "messages": context.messages,
                   "stream": False, "max_tokens": self.config.max_tokens}
        self.last_usage = {}
        if self.config.enable_thinking is not None:
            payload["enable_thinking"] = self.config.enable_thinking
        if self.config.json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        key = self.config.api_key.get_secret_value()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        endpoint = self.config.base_url + "/chat/completions"
        try:
            if self._client:
                response = await self._client.post(endpoint, json=payload, headers=headers, timeout=self.config.timeout)
            else:
                async with httpx.AsyncClient(follow_redirects=False) as client:
                    response = await client.post(endpoint, json=payload, headers=headers, timeout=self.config.timeout)
        except httpx.TimeoutException:
            raise ModelError("Model request timed out. Check the service or raise the profile timeout.") from None
        except httpx.HTTPError:
            raise ModelError("Cannot connect to the model service. Check base_url and network.") from None
        if response.status_code != 200:
            hints = {401: "Check the API key environment variable.", 403: "Access denied by the provider.",
                     404: "Check base_url and model name.", 429: "Provider rate limit or quota reached.",
                     400: "Check model name and JSON mode support (json_mode=false for unsupported servers)."}
            raise ModelError(f"Model service HTTP {response.status_code}. " + hints.get(response.status_code, "Check provider availability."))
        try:
            body = response.json()
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
            decision = DECISION_ADAPTER.validate_python(envelope)
            # Auxiliary memory is validated by the ContextEngine after normal
            # runtime handling. An invalid delta must not invalidate a decision.
            self._memory_proposal = proposal
            return decision
        except (ValueError, KeyError, IndexError, TypeError, ValidationError) as exc:
            # Error codes only: Pydantic messages/inputs can echo arbitrary content.
            detail = ",".join(sorted({e["type"] for e in exc.errors()})) if isinstance(exc, ValidationError) else type(exc).__name__
            raise ModelProtocolError(f"Invalid Kong decision JSON ({detail}). Return exactly one decision matching the supplied schema, without extra fields.") from None
