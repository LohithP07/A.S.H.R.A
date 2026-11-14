"""Model providers.

Anthropic and NVIDIA/OpenAI-compatible endpoints are wired up now.  The
ChatProvider protocol is the seam for a future personal assistant: add another
backend here without changing retrieval, sessions, or the viewer.

Providers may also implement :class:`ToolCallingProvider`.  The orchestrator
uses that richer interface to run a bounded tool loop; providers that only
implement ``complete`` still work, with tools simply unavailable.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import AppConfig
from .errors import ProviderError

MAX_TOOL_RESULT_CHARS = 12_000


class ChatProvider(Protocol):
    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        """Return the assistant's answer for a system prompt and message list."""


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolOutcome:
    """The result of running (or refusing) one tool call."""

    call_id: str
    name: str
    ok: bool
    result: Any = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "name": self.name,
            "ok": self.ok,
            "error": self.error,
        }


@dataclass(frozen=True)
class ToolTurn:
    """One model turn that may contain text, tool calls, or both."""

    text: str
    calls: tuple[ToolCall, ...]
    assistant_message: dict[str, Any]


class ToolCallingProvider(Protocol):
    """A provider that can ask for tools and accept their results."""

    tool_schema_style: str

    def complete_turn(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ToolTurn:
        """Return the next model turn, including any requested tool calls."""

    def tool_messages(
        self,
        turn: ToolTurn,
        outcomes: list[ToolOutcome],
    ) -> list[dict[str, Any]]:
        """Format tool outcomes as provider-native messages."""


def _error_message(raw: str) -> str:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        data = None

    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if message:
                return str(message)
        if isinstance(error, str):
            return error
        for key in ("detail", "message", "title"):
            value = data.get(key)
            if value:
                return str(value)

    text = raw.strip().replace("\n", " ")
    return text[:300] if text else "The model provider returned an error."


def _redact(message: str, api_key: str) -> str:
    if api_key:
        return message.replace(api_key, "[redacted]")
    return message


def _content_text(content: object) -> str:
    """Normalize OpenAI-style string or multimodal content blocks."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type in {"text", "output_text"} and block.get("text"):
            parts.append(str(block["text"]))
        elif isinstance(block.get("content"), str):
            parts.append(block["content"])
    return "".join(parts).strip()


def _chat_completions_url(api_base: str) -> str:
    """Accept either a base URL or the full /chat/completions endpoint."""
    base = (api_base or "").strip().rstrip("/")
    if not base:
        raise ProviderError("api_base is empty in config.json.")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _outcome_text(outcome: ToolOutcome) -> str:
    """Serialize a tool outcome for the model, capped to a sane size."""
    payload: dict[str, Any] = {"ok": outcome.ok}
    if outcome.ok:
        payload["result"] = outcome.result
    else:
        payload["error"] = outcome.error or "The tool failed."
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = json.dumps({"ok": outcome.ok, "error": str(payload)}, ensure_ascii=False)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + "…"
    return text


def _parse_openai_calls(raw: object) -> list[ToolCall]:
    calls: list[ToolCall] = []
    if not isinstance(raw, list):
        return calls

    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else {}
        name = str(function.get("name") or "").strip()
        if not name:
            continue

        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                parsed: Any = json.loads(arguments) if arguments.strip() else {}
            except (TypeError, ValueError):
                parsed = {"_raw_arguments": arguments[:2000]}
        elif isinstance(arguments, dict):
            parsed = arguments
        else:
            parsed = {}

        calls.append(
            ToolCall(
                id=str(item.get("id") or f"call_{index}"),
                name=name,
                arguments=parsed if isinstance(parsed, dict) else {"value": parsed},
            )
        )
    return calls


class AnthropicProvider:
    """Anthropic Messages API over the standard library."""

    tool_schema_style = "anthropic"

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.config.api_base,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.config.api_key,
                "anthropic-version": self.config.anthropic_version,
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = _error_message(exc.read().decode("utf-8", "replace"))
            raise ProviderError(
                _redact(detail or f"Anthropic API returned HTTP {exc.code}.", self.config.api_key)
            ) from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise ProviderError(f"Could not reach the model provider: {reason}") from None
        except TimeoutError:
            raise ProviderError("The model provider timed out.") from None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise ProviderError("The model provider returned an unreadable response.") from None
        if not isinstance(data, dict):
            raise ProviderError("The model provider returned an unreadable response.")
        return data

    @staticmethod
    def _blocks(data: dict[str, Any]) -> list[dict[str, Any]]:
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ProviderError("The model provider returned no answer.")
        return [block for block in blocks if isinstance(block, dict)]

    @staticmethod
    def _text(blocks: list[dict[str, Any]]) -> str:
        return "".join(
            str(block.get("text", ""))
            for block in blocks
            if block.get("type") == "text"
        ).strip()

    def complete(self, system: str, messages: list[dict[str, Any]]) -> str:
        data = self._post(
            {
                "model": self.config.model,
                "max_tokens": self.config.max_tokens,
                "temperature": self.config.temperature,
                "system": system,
                "messages": messages,
            }
        )
        text = self._text(self._blocks(data))
        if not text:
            raise ProviderError("The model provider returned an empty answer.")
        return text

    def complete_turn(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ToolTurn:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "system": system,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = {"type": "auto"}

        blocks = self._blocks(self._post(payload))
        calls: list[ToolCall] = []
        for block in blocks:
            if block.get("type") != "tool_use":
                continue
            arguments = block.get("input")
            calls.append(
                ToolCall(
                    id=str(block.get("id") or f"tool_{len(calls)}"),
                    name=str(block.get("name") or ""),
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )

        text = self._text(blocks)
        if not text and not calls:
            raise ProviderError("The model provider returned an empty answer.")

        return ToolTurn(
            text=text,
            calls=tuple(calls),
            assistant_message={"role": "assistant", "content": blocks},
        )

    def tool_messages(
        self,
        turn: ToolTurn,
        outcomes: list[ToolOutcome],
    ) -> list[dict[str, Any]]:
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": outcome.call_id,
                        "content": _outcome_text(outcome),
                        "is_error": not outcome.ok,
                    }
                    for outcome in outcomes
                ],
            }
        ]


class OpenAICompatibleProvider:
    """OpenAI-style /chat/completions provider, including NVIDIA NIM."""

    tool_schema_style = "openai"

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            _chat_completions_url(self.config.api_base),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = _error_message(exc.read().decode("utf-8", "replace"))
            raise ProviderError(
                _redact(detail or f"Model API returned HTTP {exc.code}.", self.config.api_key)
            ) from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise ProviderError(f"Could not reach the model provider: {reason}") from None
        except TimeoutError:
            raise ProviderError("The model provider timed out.") from None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise ProviderError("The model provider returned an unreadable response.") from None
        if not isinstance(data, dict):
            raise ProviderError("The model provider returned an unreadable response.")
        return data

    @staticmethod
    def _read_choice(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError("The model provider returned no answer.")
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        return choice, message

    def _reasoning_retry(self, payload: dict[str, Any]) -> int | None:
        retry_tokens = min(4096, max(2048, self.config.max_tokens * 2))
        if retry_tokens <= self.config.max_tokens:
            return None
        payload["max_tokens"] = retry_tokens
        return retry_tokens

    @staticmethod
    def _empty_answer_error(choice: dict[str, Any], message: dict[str, Any]) -> ProviderError:
        if choice.get("finish_reason") == "length":
            return ProviderError(
                "The model used its entire token budget on internal reasoning before "
                "writing an answer. Raise max_tokens in config.json (up to 4096)."
            )
        if message.get("reasoning_content"):
            return ProviderError(
                "The model returned only internal reasoning, with no visible answer. "
                "Try rephrasing the question or raising max_tokens in config.json."
            )
        return ProviderError("The model provider returned an empty answer.")

    def complete(self, system: str, messages: list[dict[str, Any]]) -> str:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "stream": False,
        }

        data = self._post(payload)
        choice, message = self._read_choice(data)
        text = _content_text(message.get("content"))

        # Reasoning models (such as Nemotron 3.5 Super VL) can spend the whole
        # token budget thinking and return no visible content. Retry once with
        # the largest budget the config system permits before giving up.
        if not text and choice.get("finish_reason") == "length":
            if self._reasoning_retry(payload) is not None:
                data = self._post(payload)
                choice, message = self._read_choice(data)
                text = _content_text(message.get("content"))

        if text:
            return text
        raise self._empty_answer_error(choice, message)

    def complete_turn(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ToolTurn:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        data = self._post(payload)
        choice, message = self._read_choice(data)
        text = _content_text(message.get("content"))
        calls = _parse_openai_calls(message.get("tool_calls"))

        if not text and not calls and choice.get("finish_reason") == "length":
            if self._reasoning_retry(payload) is not None:
                data = self._post(payload)
                choice, message = self._read_choice(data)
                text = _content_text(message.get("content"))
                calls = _parse_openai_calls(message.get("tool_calls"))

        if not text and not calls:
            raise self._empty_answer_error(choice, message)

        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": message.get("content") or "",
        }
        if calls:
            assistant_message["content"] = message.get("content") or None
            assistant_message["tool_calls"] = message.get("tool_calls")

        return ToolTurn(
            text=text,
            calls=tuple(calls),
            assistant_message=assistant_message,
        )

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


def make_provider(config: AppConfig) -> ChatProvider:
    if config.provider == "anthropic":
        return AnthropicProvider(config)
    if config.provider in {"nvidia", "openai_compatible"}:
        return OpenAICompatibleProvider(config)
    if config.provider in {"ollama", "local"}:
        # Imported here so the local adapter's dependencies never load on a
        # cloud-only path, and so the two modules can stay independent.
        from .ollama import OllamaProvider

        return OllamaProvider(config)
    raise ProviderError(
        f"Unsupported provider '{config.provider}'. Add it in ashra/providers.py."
    )
