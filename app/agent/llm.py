"""LLM OpenAI (gpt-4o-mini) con tool-calling nativo, sin frameworks pesados.

Normaliza la respuesta al LLMResponse de app.agent.ports.
"""
import os

import httpx

from app.agent.ports import LLMResponse


class OpenAILLM:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gpt-4o-mini",
        base_url: str = "https://api.openai.com/v1",
    ):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = model
        self.base_url = base_url
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY requerido para OpenAILLM")

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> LLMResponse:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"

        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            r.raise_for_status()
            data = r.json()["choices"][0]["message"]

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
        )
