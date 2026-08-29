"""Embedding backendy: lokální sentence-transformers, nebo vzdálené
OpenAI-kompatibilní API (AiStack swarm-embed na SPARK :8005).

Různé E5 modely chtějí různé formátování vstupu:
  - multilingual-e5-large: prefixy "passage: " / "query: "
  - e5-mistral-7b-instruct: dokumenty bez prefixu, dotazy v Instruct formátu
"""

import math

DEFAULT_MODEL = "intfloat/multilingual-e5-large"
E5_MISTRAL_QUERY_PREFIX = (
    "Instruct: Given a question, retrieve passages that answer the question\nQuery: "
)


def _style(model_name: str) -> str:
    n = model_name.lower()
    if "e5-mistral" in n:
        return "e5-instruct"
    if "e5" in n:
        return "e5"
    return "plain"


def format_passage(text: str, model_name: str) -> str:
    return f"passage: {text}" if _style(model_name) == "e5" else text


def format_query(text: str, model_name: str) -> str:
    style = _style(model_name)
    if style == "e5":
        return f"query: {text}"
    if style == "e5-instruct":
        return E5_MISTRAL_QUERY_PREFIX + text
    return text


def pick_device(requested: str) -> str:
    if requested != "auto":
        return requested
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class LocalEmbedder:
    """sentence-transformers v procesu (cuda/mps/cpu).

    Na CUDA běží ve fp16: měřeno na GB10 28 → 95 pasáží/s, shoda s fp32
    min cos 0,9998 (bf16 je 165/s, ale min cos 0,998 — už se to hne).
    Index i dotaz musí používat totéž — server i embed_books jdou přes
    tuhle třídu. EMBED_DTYPE=fp32 to přebije."""

    def __init__(self, model_name: str, device: str = "auto", dtype: str | None = None):
        import os
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name
        dev = pick_device(device)
        dtype = dtype or os.getenv("EMBED_DTYPE") or ("fp16" if dev == "cuda" else "fp32")
        kwargs = {}
        if dtype in ("fp16", "bf16"):
            import torch
            kwargs["model_kwargs"] = {"torch_dtype": torch.float16 if dtype == "fp16" else torch.bfloat16}
        print(f"Načítám {model_name} (device={dev}, {dtype})...")
        self.model = SentenceTransformer(model_name, device=dev, **kwargs)

    def encode(self, texts: list[str], batch_size: int = 64) -> list[list[float]]:
        return self.model.encode(
            texts, batch_size=batch_size, normalize_embeddings=True,
            show_progress_bar=False,
        ).tolist()


class RemoteEmbedder:
    """OpenAI-kompatibilní /v1/embeddings endpoint (vLLM --task embed)."""

    def __init__(self, url: str, model_name: str, api_key: str = "dummy"):
        from openai import OpenAI

        self.model_name = model_name
        self.client = OpenAI(base_url=url, api_key=api_key)

    def encode(self, texts: list[str], batch_size: int = 64) -> list[list[float]]:
        out = []
        for i in range(0, len(texts), batch_size):
            resp = self.client.embeddings.create(
                model=self.model_name, input=texts[i : i + batch_size]
            )
            for item in resp.data:
                v = item.embedding
                norm = math.sqrt(sum(x * x for x in v)) or 1.0
                out.append([x / norm for x in v])
        return out


def make_embedder(model_name: str, device: str = "auto", url: str | None = None):
    if url:
        return RemoteEmbedder(url, model_name)
    return LocalEmbedder(model_name, device)
