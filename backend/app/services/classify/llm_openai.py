"""OpenAI som alternativ AI-leverantör (Chat Completions med strukturerad JSON)."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.services.classify.llm import (
    ASSESSMENT_SCHEMA,
    DEFAULT_OPENAI_MODEL,
    SYSTEM_PROMPT,
    LLMError,
    LLMResult,
    _coerce,
    _user_content,
)

logger = logging.getLogger(__name__)

OPENAI_BASE_URL = "https://api.openai.com/v1"


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        *,
        base_url: str = OPENAI_BASE_URL,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.model = model or DEFAULT_OPENAI_MODEL
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self._client.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise LLMError("Kunde inte nå OpenAI.", retryable=True) from exc
        if resp.status_code in (401, 403):
            raise LLMError("OpenAI avvisade API-nyckeln.", auth=True)
        if resp.status_code == 404:
            raise LLMError(f"Modellen {self.model!r} finns inte.")
        if resp.status_code == 429:
            raise LLMError("OpenAI begränsar anropstakten just nu.", retryable=True)
        if resp.status_code >= 400:
            raise LLMError(f"Fel från OpenAI ({resp.status_code}).", retryable=resp.status_code >= 500)
        try:
            return resp.json()
        except ValueError as exc:
            raise LLMError("OpenAI svarade inte med JSON.") from exc

    def assess(self, document: dict[str, Any]) -> LLMResult | None:
        body = self._post(
            "/chat/completions",
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _user_content(document)},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "trip_assessment", "strict": True, "schema": ASSESSMENT_SCHEMA},
                },
            },
        )
        # Svaret kommer utifrån: varje del kontrolleras, så att ett oväntat format blir ett
        # LLMError (resan bedöms då med enbart reglerna) och aldrig ett ohanterat undantag.
        try:
            message = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("Oväntat svar från OpenAI.") from exc
        if not isinstance(message, dict):
            raise LLMError("Oväntat svar från OpenAI.")
        if message.get("refusal"):
            logger.info("AI-leverantören avböjde att bedöma resan.")
            return None
        content = message.get("content")
        if not content:
            raise LLMError("AI-svaret saknade innehåll.")
        if not isinstance(content, str):
            raise LLMError("AI-svaret hade ett oväntat format.")
        try:
            data = json.loads(content)
        except ValueError as exc:  # json.JSONDecodeError är en ValueError
            raise LLMError("AI-svaret var inte giltig JSON.") from exc
        usage = body.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        model = body.get("model")
        return LLMResult(
            data=_coerce(data),
            provider=self.name,
            model=model if isinstance(model, str) and model else self.model,
            usage={"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")},
        )

    def ping(self) -> str:
        try:
            resp = self._client.get(f"/models/{self.model}")
        except httpx.HTTPError as exc:
            raise LLMError("Kunde inte nå OpenAI.", retryable=True) from exc
        if resp.status_code in (401, 403):
            raise LLMError("OpenAI avvisade API-nyckeln.", auth=True)
        if resp.status_code == 404:
            raise LLMError(f"Modellen {self.model!r} finns inte.")
        if resp.status_code >= 400:
            raise LLMError(f"Fel från OpenAI ({resp.status_code}).")
        return self.model
