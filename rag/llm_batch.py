"""Dávkové volání LLM pro obohacení korpusu — společný základ pro
enrich_chunks.py, enrich_chapters.py a enrich_works.py.

Proč vlastní vrstva a ne prosté volání OpenAI klienta:
- **Fallback se musí odmítnout.** LiteLLM při pádu `translate` tiše
  přepne na Qwen3-4B a data by se otrávila. Dávky proto jdou přímo na
  TRT-LLM (:8004) a každá odpověď se kontroluje podle `response.model`;
  cizí model = zahodit, počítat, po sérii odmítnutí spát.
- **Dlouhé běhy umírají zvenčí** (translate exit 137 = SIGKILL, ne OOM).
  Klient nekončí — při chybách spí a zkouší dál; resume je v DB přes
  input_sha, takže restart skriptu nic neopakuje.
- **Paralelismus** ≤ max_batch_size TRT (16); výchozí 12 vláken, zbytek
  zůstává chatu.
- **JSON z modelu** není spolehlivý: bere se blok mezi první '{' a
  poslední '}', <think> se stříhá.
- **Uvažování se vypíná.** Qwen3 na značkovací úlohu spálí v <think>
  násobek tokenů vlastní odpovědi a JSON se pak nevejde do max_tokens →
  useknutá odpověď bez závorky = neparsovatelná. `enable_thinking: false`
  jde do `chat_template_kwargs`; backend, který to neumí, se pozná z chyby
  a přepne se zpátky.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from openai import OpenAI

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


@dataclass
class BatchStats:
    done: int = 0
    failed: int = 0
    rejected_fallback: int = 0
    truncated: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    started: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def rate(self) -> str:
        dt = max(1e-6, time.time() - self.started)
        trunc = f", {self.truncated} useknuto" if self.truncated else ""
        return (f"{self.done} hotovo, {self.failed} chyb{trunc}, {self.rejected_fallback} fallback | "
                f"{self.done / dt * 60:.1f}/min, in {self.tokens_in / dt:.0f} tok/s, out {self.tokens_out / dt:.0f} tok/s")


def parse_json(text: str) -> dict | None:
    """Vezme první {...} blok; model občas obalí JSON textem nebo ```json."""
    text = THINK_RE.sub("", text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    blob = text[start:end + 1]
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        # nejčastější vada: koncová čárka před } nebo ]
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", blob))
        except json.JSONDecodeError:
            return None


def input_sha(prompt_version: str, *parts: str) -> str:
    """`verze:hexdigest` — prefix je schválně: díky němu se „změnil se prompt?"
    ptá SQL jako `input_sha NOT LIKE 'chunk-v1:%'`, tedy bez hashovací funkce
    na straně Postgresu (ten `sha1()` vůbec nemá, jen sha224+ přes bytea).
    Změnu *textu* řeší load_pg.replace_work(), který obohacení zahazuje, když
    nesedí `text_sha` — tady se proto nehashuje kvůli němu, ale kvůli vstupům,
    které v DB nejsou (složené promptu kapitol a děl)."""
    h = hashlib.sha1(prompt_version.encode("utf-8"))
    for p in parts:
        h.update(b"\x00")
        h.update((p or "").encode("utf-8"))
    return f"{prompt_version}:{h.hexdigest()}"


class LLMBatch:
    def __init__(self, url: str, model: str, *, workers: int = 12, temperature: float = 0.2,
                 max_tokens: int = 600, timeout: float = 180.0, accept_models: set[str] | None = None,
                 json_mode: bool = True, thinking: bool = False):
        self.client = OpenAI(base_url=url, api_key="dummy", timeout=timeout, max_retries=0)
        self.model = model
        self.workers = workers
        self.temperature = temperature
        self.max_tokens = max_tokens
        # TRT-LLM vrací jako `model` název ze --served_model_name; LiteLLM
        # by vrátil 'fallback' — cokoli mimo seznam se zahazuje
        self.accept = accept_models or {model}
        self.json_mode = json_mode
        self.thinking = thinking
        self.stats = BatchStats()
        self._consecutive_bad = 0
        self._shown_bad = False

    def one(self, messages: list[dict]) -> tuple[dict | None, str]:
        """Jeden request s tolerancí k výpadkům: při síťové chybě spí a
        zkouší znovu (neomezeně — běh má přežít restart modelu)."""
        backoff = 5.0
        while True:
            try:
                kwargs = {}
                if self.json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                if not self.thinking:
                    kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
                resp = self.client.chat.completions.create(
                    model=self.model, messages=messages, temperature=self.temperature,
                    max_tokens=self.max_tokens, **kwargs,
                )
            except Exception as exc:  # noqa: BLE001 — výpadek modelu není důvod skončit
                msg = str(exc)
                if "response_format" in msg or "json_object" in msg:
                    self.json_mode = False   # backend to neumí — prompt-only JSON
                    continue
                if "chat_template_kwargs" in msg or "enable_thinking" in msg:
                    self.thinking = True     # backend to neumí — nech model uvažovat
                    print("  backend nezná chat_template_kwargs — uvažování zůstává zapnuté", flush=True)
                    continue
                with self.stats.lock:
                    self._consecutive_bad += 1
                    bad = self._consecutive_bad
                if bad % 10 == 1:
                    print(f"  LLM nedostupný ({msg[:80]}), čekám {backoff:.0f} s …", flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 120.0)
                continue
            got_model = (resp.model or "").strip()
            if got_model not in self.accept and not any(got_model.startswith(a) for a in self.accept):
                with self.stats.lock:
                    self.stats.rejected_fallback += 1
                    self._consecutive_bad += 1
                    bad = self._consecutive_bad
                if bad >= 20:
                    print(f"  {bad}× po sobě cizí model ({got_model!r}) — translate nejspíš leží, spím 60 s", flush=True)
                    time.sleep(60)
                continue
            with self.stats.lock:
                self._consecutive_bad = 0
                if resp.usage:
                    self.stats.tokens_in += resp.usage.prompt_tokens or 0
                    self.stats.tokens_out += resp.usage.completion_tokens or 0
            choice = resp.choices[0]
            text = choice.message.content or ""
            parsed = parse_json(text)
            if parsed is None:
                with self.stats.lock:
                    if choice.finish_reason == "length":
                        self.stats.truncated += 1
                    show, self._shown_bad = not self._shown_bad, True
                if show:   # ať se nehádá naslepo, proč je „0 hotovo, N chyb"
                    print(f"  první neparsovatelná odpověď (finish_reason={choice.finish_reason}, "
                          f"{resp.usage.completion_tokens if resp.usage else '?'} tok): {text[:300]!r}", flush=True)
            return parsed, got_model

    def run(self, items, build_messages, on_result, *, label: str = "", report_every: int = 50):
        """items: iterovatelné položky; build_messages(item) → messages;
        on_result(item, parsed_json|None, model) → bool (uloženo?).
        Zpracovává paralelně, hlásí průběh."""
        pool = ThreadPoolExecutor(max_workers=self.workers)
        futures = {}
        n = 0

        def submit(item):
            return pool.submit(lambda it=item: (it, *self.one(build_messages(it))))

        it = iter(items)
        # udržovat frontu workers × 2, ne načítat všechno do paměti
        for item in it:
            futures[submit(item)] = None
            n += 1
            if len(futures) >= self.workers * 2:
                break
        while futures:
            for fut in as_completed(list(futures)):
                futures.pop(fut, None)
                item, parsed, model = fut.result()
                ok = on_result(item, parsed, model)
                with self.stats.lock:
                    if ok:
                        self.stats.done += 1
                    else:
                        self.stats.failed += 1
                    total = self.stats.done + self.stats.failed
                if total % report_every == 0:
                    print(f"  [{label}] {self.stats.rate()}", flush=True)
                nxt = next(it, None)
                if nxt is not None:
                    futures[submit(nxt)] = None
                    n += 1
                break
        pool.shutdown(wait=True)
        print(f"  [{label}] konec: {self.stats.rate()}", flush=True)
        return self.stats
