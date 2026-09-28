"""Přístup k modelu — tenká vrstva, aby se agent dal testovat bez LLM.

V parku na SPARKu neběží chat model celý den (denní režim shodí directora
i translate, Qwen agent jede 00:08–00:55), takže agent musí být napsaný tak,
že jeho logika je testovatelná se skriptovaným modelem a teprve nasazení
potřebuje okno s modelem. Proto tady dvě implementace:

* `OpenAIKlient` — OpenAI-kompatibilní endpoint (LiteLLM na SPARKu, vLLM).
  Tool calling se posílá jako `tools`, strukturovaný výstup jako
  `response_format={"type": "json_schema", ...}`, s fallbackem na `json_object`
  pro backendy, které schema neumí.
* `SkriptovanyKlient` — vrací předem dané odpovědi. Používá ho eval i testy.

Odpověď se vrací v tvaru OpenAI zprávy: `{"content": str|None, "tool_calls": [...]}`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


class ChybaModelu(Exception):
    """Model není dostupný nebo vrátil nesmysl."""


@dataclass
class OdpovedModelu:
    content: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    model: str = ""
    ms: int = 0

    @property
    def chce_nastroj(self) -> bool:
        return bool(self.tool_calls)


class OpenAIKlient:
    def __init__(self, url: str, model: str, api_key: str = "dummy", timeout: float = 300.0,
                 temperature: float = 0.2):
        from openai import OpenAI

        self.model = model
        self.temperature = temperature
        self.client = OpenAI(base_url=url, api_key=api_key, timeout=timeout)

    def chat(self, zpravy: list[dict], tools: list[dict] | None = None,
             json_schema: dict | None = None, max_tokens: int = 1200) -> OdpovedModelu:
        import time

        kwargs: dict = {"model": self.model, "messages": zpravy,
                        "temperature": self.temperature, "max_tokens": max_tokens}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if json_schema is not None:
            kwargs["response_format"] = {"type": "json_schema",
                                         "json_schema": {"name": "vystup", "schema": json_schema,
                                                         "strict": False}}
        t0 = time.monotonic()
        try:
            r = self.client.chat.completions.create(**kwargs)
        except Exception as e:
            # některé backendy json_schema neumí — zkus volnější json_object
            if json_schema is not None and "response_format" in str(e).lower():
                kwargs["response_format"] = {"type": "json_object"}
                r = self.client.chat.completions.create(**kwargs)
            else:
                raise ChybaModelu(f"{type(e).__name__}: {e}") from None
        ms = int((time.monotonic() - t0) * 1000)
        m = r.choices[0].message
        volani = [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                  for tc in (m.tool_calls or [])]
        return OdpovedModelu(content=m.content, tool_calls=volani,
                             model=getattr(r, "model", self.model), ms=ms)

    def dostupny(self) -> tuple[bool, str]:
        try:
            self.chat([{"role": "user", "content": "ping"}], max_tokens=5)
            return True, "ok"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"


class SkriptovanyKlient:
    """Odpovědi ze seznamu; každá položka je `OdpovedModelu` nebo dict/str.

    Když seznam dojde, vrátí prázdnou odpověď — tím se smyčka ukončí a test to
    pozná, místo aby se zacyklila.
    """

    def __init__(self, odpovedi: list):
        self.odpovedi = list(odpovedi)
        self.volani: list[dict] = []          # co dostal — pro asserty v testech

    def chat(self, zpravy: list[dict], tools: list[dict] | None = None,
             json_schema: dict | None = None, max_tokens: int = 1200) -> OdpovedModelu:
        self.volani.append({"zpravy": zpravy, "tools": [t["function"]["name"] for t in tools or []],
                            "json_schema": bool(json_schema)})
        if not self.odpovedi:
            return OdpovedModelu(content="(skript vyčerpán)")
        o = self.odpovedi.pop(0)
        if isinstance(o, OdpovedModelu):
            return o
        if isinstance(o, str):
            return OdpovedModelu(content=o)
        if isinstance(o, dict) and "tool" in o:
            return OdpovedModelu(tool_calls=[{"id": f"call_{len(self.volani)}", "name": o["tool"],
                                              "arguments": json.dumps(o.get("args") or {},
                                                                      ensure_ascii=False)}])
        if isinstance(o, dict):
            return OdpovedModelu(content=o.get("content"), tool_calls=o.get("tool_calls") or [])
        raise ChybaModelu(f"neznámý tvar skriptované odpovědi: {o!r}")

    def dostupny(self) -> tuple[bool, str]:
        return True, "skriptovaný"
