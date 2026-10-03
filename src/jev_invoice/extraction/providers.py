"""LLM provider adapters.

An adapter turns one (system, user) prompt pair into an LLMResponse with a specific
vendor SDK and declares which SDK errors are permanent (auth) and which are worth
retrying. Retry, circuit breaking, and cost tracking live in LLMClient, shared by all.
"""

import asyncio
import base64
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import groq
import httpx

from jev_invoice.ingest.parsing import PageImage


@dataclass(frozen=True)
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    model: str


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
    """Groq chat completions (OpenAI-compatible)."""

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
        user_content: str | list[dict[str, Any]] = user
        if images:
            # OpenAI-style content parts; Groq accepts base64 data URIs (up to 3 per request).
            user_content = [
                {"type": "text", "text": user},
                *(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{image.mime_type};base64,"
                            + base64.b64encode(image.data).decode("ascii")
                        },
                    }
                    for image in images
                ),
            ]
        # The SDK's TypedDicts cannot express mixed content parts built at runtime.
        messages: list[Any] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        completion = await self._client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=0,
            messages=messages,
            response_format={"type": "json_object"} if json_mode else {"type": "text"},
        )
        usage = completion.usage
        return LLMResponse(
            text=completion.choices[0].message.content or "",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            model=completion.model or self.model,
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
        )
