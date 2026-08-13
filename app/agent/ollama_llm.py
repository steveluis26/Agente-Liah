"""Adaptador Ollama para LLMPort (demo local, gratis, sin API key).

Implementa el mismo contrato que OpenAILLM: chat(messages, tools) -> LLMResponse
con tool_calls o content. Permite verificar el demo end-to-end sin OpenAI.

Requiere 'ollama' instalado y corriendo (ollama serve) con un modelo que soporte
function calling (ej. llama3.2).
"""
import os

import httpx

from app.agent.ports import LLMResponse


class OllamaLLM:
    """LLM vía Ollama (local). Misma interfaz que OpenAILLM."""

    def __init__(self, model: str | None = None, base_url: str | None = None):
        self.model = model or os.getenv("OLLAMA_MODEL", "llama3.2")
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")).rstrip("/")

    async def chat(self, messages, tools=None, tool_choice=None) -> LLMResponse:
        # Normaliza tool_calls entrantes: Ollama espera arguments como dict,
        # el engine los pasa como string JSON.
        import json as _json

        norm_messages = []
        for m in messages:
            nm = dict(m)
            if m.get("tool_calls"):
                tcs = []
                for tc in m["tool_calls"]:
                    fn = dict(tc.get("function", {}))
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = _json.loads(args)
                        except _json.JSONDecodeError:
                            args = {}
                    fn["arguments"] = args
                    tcs.append({"id": tc.get("id", "c"), "type": "function", "function": fn})
                nm["tool_calls"] = tcs
            norm_messages.append(nm)

        payload = {
            "model": self.model,
            "messages": norm_messages,
            "stream": False,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["function"]["name"],
                        "description": t["function"].get("description", ""),
                        "parameters": t["function"].get("parameters", {}),
                    },
                }
                for t in tools
            ]

        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(f"{self.base_url}/api/chat", json=payload)
            if r.status_code >= 400:
                raise RuntimeError(
                    f"Ollama /api/chat {r.status_code}: {r.text[:500]}"
                )
            data = r.json()

        msg = data.get("message", {})
        tool_calls = msg.get("tool_calls") or []
        content = msg.get("content") or None

        # Ollama a veces devuelve el tool call embebido en content como JSON.
        # Intento extraerlo defensivamente.
        if not tool_calls and content:
            import json
            import re

            try:
                m = re.search(r"\{.*\}", content, re.DOTALL)
                if m:
                    obj = json.loads(m.group(0))
                    if "name" in obj and "parameters" in obj:
                        tool_calls = [{
                            "function": {
                                "name": obj["name"],
                                "arguments": obj["parameters"],
                            }
                        }]
                        content = None
            except (json.JSONDecodeError, TypeError):
                pass

        if tool_calls:
            parsed = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    import json

                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                parsed.append({
                    "id": f"call_{len(parsed)}",
                    "name": fn.get("name", ""),
                    "arguments": args,
                })
            return LLMResponse(
                content=content,
                finish_reason="tool_calls",
                tool_calls=parsed,
            )
        return LLMResponse(content=content, finish_reason="stop")
