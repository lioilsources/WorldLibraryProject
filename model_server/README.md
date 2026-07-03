# model-server (běží na SPARKu / NVIDIA DGX)

OpenAI-compatible inference služba volaná Go pipeline přes Tailscale mesh.
Kolo 1: pouze `POST /v1/chat/completions` + `GET /healthz`.

## Instalace (na SPARKu)

```bash
cd model_server
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

## Spuštění a navázání na Tailscale

```bash
export MODEL_NAME=qwen2.5-72b-instruct
export MODEL_PATH=Qwen/Qwen2.5-72B-Instruct      # HF repo nebo lokální cesta
export MODEL_CONTEXT=32768
export MODEL_API_KEY=…                            # stejný jako u Go pipeline
# Naslouchá na všech rozhraních; přístup omezuje Tailscale ACL.
uvicorn app:app --host 0.0.0.0 --port 8000
```

Pokud chceš vázat výhradně na Tailscale rozhraní, použij jeho IP:

```bash
uvicorn app:app --host "$(tailscale ip -4 | head -n1)" --port 8000
```

Go pipeline pak na stroji A nastaví `MODEL_BASE_URL` na Tailscale MagicDNS
název SPARKu, např. `http://spark:8000`.

## Ověření

```bash
curl -s http://spark:8000/healthz
curl -s -X POST http://spark:8000/v1/chat/completions \
  -H "Authorization: Bearer $MODEL_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5-72b-instruct","messages":[{"role":"user","content":"Řekni česky ahoj."}],"max_tokens":16}'
```

## Rozšíření

Backend je schovaný za funkcí `generate()` (snadná výměna za
transformers/llama.cpp přes `MODEL_BACKEND`). Diffusion/LoRA routy se budou
registrovat přes `EXTRA_ROUTERS` — v kole 1 nejsou implementované.
