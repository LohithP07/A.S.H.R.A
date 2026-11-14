"""The local lane: an Ollama adapter with no network egress.

PRD section 2M.  Ollama serves ``/api/chat`` on loopback only, so a request
that reaches this module has already been decided as local by
:mod:`ashra.router`.  The adapter has three jobs:

* send the options the model needs to be fast and predictable — thinking off,
  an explicit context size, an optional JSON schema;
* refuse to pretend: a missing server or model is a visible limitation, never
  a reason to reach the cloud;
* surface health so the Desk can say *why* the lane is unavailable.

Nothing here reads a credential.  There is no API key on this lane by design.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import AppConfig
from .errors import ProviderError
from .providers import (
    ToolCall,
    ToolOutcome,
    ToolTurn,
    _content_text,
    _error_message,
    _outcome_text,
    _parse_openai_calls,
)

# Ollama's own default is generous; the PRD's latency budget is not.  A local
# call that takes longer than this is a failure the user should see.
HEALTH_TIMEOUT = 2


def _base_url(api_base: str) -> str:
    """Accept ``http://host:11434``, ``.../v1`` or a full endpoint."""
    base = (api_base or "").strip().rstrip("/")
    if not base:
        base = "http://127.0.0.1:11434"
    if base.endswith("/v1"):
        base = base[:-3]
    for suffix in ("/api/chat", "/api/generate", "/api/tags"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base


class OllamaProvider:
    """A chat provider for a local Ollama server."""

    tool_schema_style = "openai"

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------
    def _request(self, path: str, payload: dict[str, Any] | None, timeout: int) -> dict[str, Any]:
        url = f"{_base_url(self.config.api_base)}{path}"
        data = None
        method = "GET"
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            method = "POST"
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = _error_message(exc.read().decode("utf-8", "replace"))
            raise ProviderError(
                detail or f"Ollama returned HTTP {exc.code}."
            ) from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise ProviderError(
                f"The local model is not reachable ({reason}). "
                "Start Ollama, then retry."
            ) from None
        except TimeoutError:
            raise ProviderError(
                f"The local model did not answer within {timeout} s."
            ) from None

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise ProviderError("Ollama returned an unreadable response.") from None
        if not isinstance(parsed, dict):
            raise ProviderError("Ollama returned an unreadable response.")
        return parsed

    def _options(self) -> dict[str, Any]:
        return {
            "temperature": self.config.temperature,
            "num_predict": self.config.max_tokens,
            "num_ctx": self.config.local_num_ctx,
        }

    def _payload(
        self,
        system: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        structured: bool | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": False,
            # Thinking mode is on by default in Qwen 3.5.  Left enabled it
            # spends the whole token budget reasoning and returns nothing, so
            # the adapter always states the intent explicitly.
            "think": False,
            "options": self._options(),
        }
        want_json = self.config.local_format == "json" if structured is None else structured
        if want_json:
            payload["format"] = "json"
        if tools:
            payload["tools"] = tools
        return payload

    def _chat(
        self,
        system: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        structured: bool | None = None,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        payload = self._payload(system, messages, tools=tools, structured=structured)
        data = self._request("/api/chat", payload, self.config.local_timeout)
        message = data.get("message")
        if not isinstance(message, dict):
            raise ProviderError("The local model returned no message.")
        text = _content_text(message.get("content"))
        return text, message, data

    # ------------------------------------------------------------------
    # ChatProvider
    # ------------------------------------------------------------------
    def complete(self, system: str, messages: list[dict[str, Any]]) -> str:
        text, _message, data = self._chat(system, messages)
        if text:
            return text
        if data.get("done_reason") == "length":
            raise ProviderError(
                "The local model used its whole token budget before answering. "
                "Raise max_tokens in config.json."
            )
        raise ProviderError("The local model returned an empty answer.")

    def complete_json(
        self,
        system: str,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Ask for one JSON object and parse it.

        Classification and extraction use this; a parse failure is a provider
        error rather than a silent empty dict.
        """
        text, _message, _data = self._chat(system, messages, structured=True)
        try:
            parsed = json.loads(text) if text.strip() else None
        except json.JSONDecodeError:
            raise ProviderError(
                "The local model did not return valid JSON for a structured task."
            ) from None
        if not isinstance(parsed, dict):
            raise ProviderError(
                "The local model returned valid JSON that was not an object."
            )
        return parsed

    # ------------------------------------------------------------------
    # ToolCallingProvider
    # ------------------------------------------------------------------
    def complete_turn(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ToolTurn:
        text, message, data = self._chat(system, messages, tools=tools or None)
        calls: list[ToolCall] = _parse_openai_calls(message.get("tool_calls"))
        if not text and not calls:
            if data.get("done_reason") == "length":
                raise ProviderError(
                    "The local model used its whole token budget before answering. "
                    "Raise max_tokens in config.json."
                )
            raise ProviderError("The local model returned an empty answer.")

        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": message.get("content") or "",
        }
        if calls:
            assistant_message["content"] = message.get("content") or None
            assistant_message["tool_calls"] = message.get("tool_calls")

        return ToolTurn(text=text, calls=tuple(calls), assistant_message=assistant_message)

    def tool_messages(
        self,
        turn: ToolTurn,
        outcomes: list[ToolOutcome],
    ) -> list[dict[str, Any]]:
        return [
            {
                "role": "tool",
                "tool_call_id": outcome.call_id,
                "content": _outcome_text(outcome),
            }
            for outcome in outcomes
        ]

    # ------------------------------------------------------------------
    # health
    # ------------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        """Report whether the lane can answer, and why not."""
        model = self.config.model
        try:
            tags = self._request("/api/tags", None, HEALTH_TIMEOUT)
        except ProviderError as exc:
            return {
                "status": "down",
                "model": model,
                "detail": str(exc),
                "models": [],
            }

        models = [
            str(item.get("name") or "")
            for item in tags.get("models", [])
            if isinstance(item, dict)
        ]
        if not model:
            return {
                "status": "model_missing",
                "model": model,
                "detail": "No local model is configured.",
                "models": models,
            }
        if model not in models:
            # Ollama accepts a bare name for the :latest tag, so compare both.
            if f"{model}:latest" not in models:
                return {
                    "status": "model_missing",
                    "model": model,
                    "detail": f"Model '{model}' is not installed. Run: ollama pull {model}",
                    "models": models,
                }

        return {
            "status": "ok",
            "model": model,
            "detail": "",
            "models": models,
        }


def local_health(config: AppConfig) -> dict[str, Any]:
    """Health for the local lane, including the 'not an Ollama config' case."""
    local = config.for_local()
    return OllamaProvider(local).health()
