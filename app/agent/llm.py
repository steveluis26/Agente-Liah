"""LLM OpenAI con tool-calling nativo (conector comercial, Fase 2).

- Captura `usage` real (prompt/completion tokens) en `LLMResponse` para el
  costeo por turno.
- `max_tokens` / `temperature` configurables (defaults sensatos; el factory
  del engine los toma de `tenant_configs.model_routing`).
- Retry con backoff exponencial ante 429/5xx y errores de transporte.
- La API key se resuelve POR TENANT vía `SecretProvider` (ver
  app/agent/secrets.py); el constructor acepta la key ya resuelta o cae al
  env global `OPENAI_API_KEY` (dev).

El cliente httpx se crea con `trust_env=False`: la VM de desarrollo mete
proxies por variables de entorno que rompen las llamadas a api.openai.com
(misma lección que en sender.py).
"""
import asyncio
import logging
import os

import httpx

from app.agent.ports import LLMResponse

logger = logging.getLogger("liah.llm")

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BACKOFF_BASE_S = 1.0


async def _post_with_retry(
    client: httpx.AsyncClient, url: str, headers: dict, payload: dict
) -> httpx.Response:
    """POST con backoff exponencial ante 429/5xx y errores de red."""
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            r = await client.post(url, headers=headers, json=payload)
            if r.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                delay = _BACKOFF_BASE_S * (2 ** attempt)
                logger.warning(
                    "OpenAI API %s (intento %d/%d); reintentando en %.1fs",
                    r.status_code, attempt + 1, _MAX_RETRIES + 1, delay,
                )
                await asyncio.sleep(delay)
                continue
            r.raise_for_status()
            return r
        except httpx.HTTPStatusError:
            # 4xx no-reintentables (429 ya se manejó arriba) fallan ya.
            raise
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last_exc = e
            if attempt < _MAX_RETRIES:
                delay = _BACKOFF_BASE_S * (2 ** attempt)
                logger.warning(
                    "Error de transporte OpenAI (%s); reintentando en %.1fs",
                    e, delay,
                )
                await asyncio.sleep(delay)
    raise last_exc or RuntimeError("fallo OpenAI sin respuesta")


class OpenAILLM:
    """Conector comercial OpenAI (chat completions + function calling)."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gpt-4o-mini",
        base_url: str = "https://api.openai.com/v1",
        *,
        max_tokens: int | None = None,
        temperature: float = 0.2,
        timeout: float = 60.0,
    ):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        if not self.api_key:
            raise RuntimeError(
                "OPENAI_API_KEY requerido para OpenAILLM "
                "(o la key del tenant vía SecretProvider: "
                "OPENAI_API_KEY_TENANT_<SLUG>)"
            )

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> LLMResponse:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"

        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            r = await _post_with_retry(
                client,
                f"{self.base_url}/chat/completions",
                {"Authorization": f"Bearer {self.api_key}"},
                payload,
            )
            body = r.json()

        data = body["choices"][0]["message"]
        # OJO: `usage` vive en el nivel raíz de la respuesta, no en el
        # message (bug corregido en Fase 2: antes se leía del message y
        # siempre salía 0).
        raw_usage = body.get("usage") or {}

        tool_calls = []
        for tc in data.get("tool_calls", []) or []:
            if tc.get("type") == "function":
                fn = tc["function"]
                import json

                try:
                    args = json.loads(fn.get("arguments", "{}") or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(
                    {"id": tc["id"], "name": fn["name"], "arguments": args}
                )

        finish = "tool_calls" if tool_calls else "stop"
        return LLMResponse(
            content=data.get("content"),
            finish_reason=finish,
            tool_calls=tool_calls,
            raw=data,
            usage={
                "prompt_tokens": raw_usage.get("prompt_tokens", 0),
                "completion_tokens": raw_usage.get("completion_tokens", 0),
                "total_tokens": raw_usage.get("total_tokens", 0),
            },
        )
