"""LLM provider adapters.

An adapter turns one (system, user) prompt pair into an LLMResponse with a specific
vendor SDK and declares which SDK errors are permanent (auth) and which are worth
retrying. Retry, circuit breaking, and cost tracking live in LLMClient, shared by all.
"""

import asyncio
import base64
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import groq
import httpx

from jev_invoice.ingest.parsing import PageImage


@dataclass(frozen=True)
class RateLimit:
    """The provider's per-minute token allowance, as its response headers report it."""

    limit_tokens: int
    remaining_tokens: int
    reset_seconds: float
    """Until the allowance is full again; it refills steadily over this time."""


@dataclass(frozen=True)
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    rate_limit: RateLimit | None = None
    truncated: bool = False


_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_UNIT_SECONDS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}


def parse_rate_limit(headers: Mapping[str, str]) -> RateLimit | None:
    """Groq's x-ratelimit-*-tokens headers (reset like "1m4.8s"), or None when absent."""
    try:
        limit = int(headers["x-ratelimit-limit-tokens"])
        remaining = int(headers["x-ratelimit-remaining-tokens"])
        reset = headers["x-ratelimit-reset-tokens"].strip()
    except (KeyError, ValueError):
        return None
    parts = _DURATION_PART.findall(reset)
    if not parts or "".join(number + unit for number, unit in parts) != reset:
        return None
    seconds = sum(float(number) * _UNIT_SECONDS[unit] for number, unit in parts)
    return RateLimit(limit, remaining, seconds)


class LLMProvider(Protocol):
    name: str
    model: str
    permanent_errors: tuple[type[Exception], ...]
    retryable_errors: tuple[type[Exception], ...]

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        json_mode: bool,
        images: Sequence[PageImage] = (),
    ) -> LLMResponse: ...


class GroqProvider:
    """Groq chat completions (OpenAI-compatible), for text only.

    Scans and photos are read locally (OllamaProvider); page images never go to Groq.
    """

    name = "groq"
    permanent_errors: tuple[type[Exception], ...] = (
        groq.AuthenticationError,
        groq.PermissionDeniedError,
        groq.NotFoundError,  # unknown model: retrying will not help
    )
    # BadRequestError is retryable: in JSON mode Groq answers 400 `json_validate_failed`
    # when the model emits invalid JSON, and a second attempt usually succeeds.
    retryable_errors: tuple[type[Exception], ...] = (groq.APIError,)

    def __init__(self, client: groq.AsyncGroq, model: str) -> None:
        self._client = client
        self.model = model

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        json_mode: bool,
        images: Sequence[PageImage] = (),
    ) -> LLMResponse:
        if images:
            raise ValueError("Page images are read locally, never sent to Groq.")
        messages: list[Any] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        # The raw response carries the rate-limit headers that pace the next call.
        raw = await self._client.chat.completions.with_raw_response.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=0,
            messages=messages,
            response_format={"type": "json_object"} if json_mode else {"type": "text"},
        )
        completion = await raw.parse()
        usage = completion.usage
        return LLMResponse(
            text=completion.choices[0].message.content or "",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            model=completion.model or self.model,
            rate_limit=parse_rate_limit(raw.headers),
            truncated=getattr(completion.choices[0], "finish_reason", None) == "length",
        )


class OllamaModelMissingError(Exception):
    """The model is not pulled on the Ollama server (permanent until someone pulls it)."""


class OllamaServerError(Exception):
    """Ollama answered with a server error; worth retrying."""


class OllamaProvider:
    """A model served by a local Ollama (http://localhost:11434), e.g. GLM-OCR.

    Runs on this machine: free, and documents never leave it. Calls are serialised,
    because a CPU-bound local model only slows down when given several pages at once.
    """

    name = "ollama"
    permanent_errors: tuple[type[Exception], ...] = (OllamaModelMissingError,)
    retryable_errors: tuple[type[Exception], ...] = (httpx.TransportError, OllamaServerError)

    def __init__(
        self,
        client: httpx.AsyncClient,
        model: str,
        *,
        num_ctx: int,
        max_output_tokens: int = 2048,
        stop: Sequence[str] = (),
    ) -> None:
        self._client = client
        self.model = model
        self._num_ctx = num_ctx
        # A local model that starts repeating itself can run for minutes; cap it.
        self._max_output_tokens = max_output_tokens
        self._stop = list(stop)
        self._one_at_a_time = asyncio.Lock()

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        json_mode: bool,
        images: Sequence[PageImage] = (),
    ) -> LLMResponse:
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
        messages.append(
            {
                "role": "user",
                "content": user,
                "images": [base64.b64encode(image.data).decode("ascii") for image in images],
            }
        )
        options: dict[str, Any] = {
            "temperature": 0,
            # Ollama's default 4096-token context is too small for a page image.
            "num_ctx": self._num_ctx,
            "num_predict": min(max_tokens, self._max_output_tokens),
        }
        if self._stop:
            options["stop"] = self._stop
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": options,
        }
        if json_mode:
            body["format"] = "json"
        async with self._one_at_a_time:
            response = await self._client.post("/api/chat", json=body)
        if response.status_code == 404:
            raise OllamaModelMissingError(
                f"Ollama has no model '{self.model}'. Run: ollama pull {self.model}"
            )
        if response.status_code >= 500:
            raise OllamaServerError(f"Ollama error {response.status_code}: {response.text[:200]}")
        response.raise_for_status()
        data = response.json()
        return LLMResponse(
            text=(data.get("message") or {}).get("content") or "",
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            model=str(data.get("model") or self.model),
            truncated=(
                data.get("done_reason") == "length"
                or int(data.get("eval_count") or 0) >= options["num_predict"]
            ),
        )
