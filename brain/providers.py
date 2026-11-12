"""Model providers.

Anthropic and NVIDIA/OpenAI-compatible endpoints are wired up now.  The
ChatProvider protocol is the seam for a future personal assistant: add another
backend here without changing retrieval, sessions, or the viewer.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Protocol

from .config import AppConfig
from .errors import ProviderError


class ChatProvider(Protocol):
    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        """Return the assistant's answer for a system prompt and message list."""


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


class AnthropicProvider:
    """Anthropic Messages API over the standard library."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        payload = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "system": system,
            "messages": messages,
        }
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
            with urllib.request.urlopen(request, timeout=60) as response:
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

        blocks = data.get("content") if isinstance(data, dict) else None
        if not isinstance(blocks, list):
            raise ProviderError("The model provider returned no answer.")

        text = "".join(
            str(block.get("text", ""))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        if not text:
            raise ProviderError("The model provider returned an empty answer.")
        return text


class OpenAICompatibleProvider:
    """OpenAI-style /chat/completions provider, including NVIDIA NIM."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def _post(self, payload: dict[str, object]) -> dict[str, object]:
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
    def _read_choice(data: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError("The model provider returned no answer.")
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        return choice, message

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        payload: dict[str, object] = {
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
            retry_tokens = min(4096, max(2048, self.config.max_tokens * 2))
            if retry_tokens > self.config.max_tokens:
                payload["max_tokens"] = retry_tokens
                data = self._post(payload)
                choice, message = self._read_choice(data)
                text = _content_text(message.get("content"))

        if text:
            return text
        if choice.get("finish_reason") == "length":
            raise ProviderError(
                "The model used its entire token budget on internal reasoning before "
                "writing an answer. Raise max_tokens in config.json (up to 4096)."
            )
        if message.get("reasoning_content"):
            raise ProviderError(
                "The model returned only internal reasoning, with no visible answer. "
                "Try rephrasing the question or raising max_tokens in config.json."
            )
        raise ProviderError("The model provider returned an empty answer.")


def make_provider(config: AppConfig) -> ChatProvider:
    if config.provider == "anthropic":
        return AnthropicProvider(config)
    if config.provider in {"nvidia", "openai_compatible"}:
        return OpenAICompatibleProvider(config)
    raise ProviderError(
        f"Unsupported provider '{config.provider}'. Add it in brain/providers.py."
    )
