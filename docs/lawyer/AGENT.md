# Právník jako agent — stav k 2026-09-28

Plán: `LAWYER_AGENT_PLAN.md` (mimo repo). Předchůdci: `CURRENT_STATE.md` (právní
index), `TEMPLATES.md` (14 šablon). Tady je, co je hotové, kde to bydlí a co
z plánu čeká — včetně jedné věci, která blokuje celou fázi 1.

## Discovery: dvě zjištění, která změnila architekturu

**1. Ol1nLLM nemá Go backend.** `Ol1nLLM/backend/` je docker-compose a tři Python
mikroslužby (image-api, ocr-api, nim-kontext-proxy). Flutter klient mluví přímo
na `law-chat` (`rag/server.py`, SPARK :8098) a na LiteLLM. Balíček
`internal/agents/lawyer` v Go, se kterým plán počítá, by musel vzniknout od nuly
a volat přes HTTP to, co už je v témže procesu jako `server.py`. Agent proto žije
v **`rag/agent/`** vedle právního indexu a šablon: nástroje jsou přímé volání
Pythonu, ne síť.

**2. V parku neběží chat model celý den.** `rag-schedule` přepnul 2026-09-28 ve
12:20 do denního režimu: director dolů, `translate` zůstává dolů záměrně, Qwen
agent (`openclaw-default`, Qwen3.6-35B-A3B — v LiteLLM označený jako **ten**
model pro tool calling) jede jen 00:08–00:55. `POST /agent/chat` proto vrací 503
„model není dostupný" a tool calling **není na čem změřit**. Agent je napsaný tak,
aby to nebyl blokátor pro vývoj: model je injektovaný (`agent/llm.py`) a všechno
ostatní je deterministické a otestované.

## Kde co je

| Co | Kde |
|---|---|
| Nástroje (8) | `rag/agent/tools.py` |
| Smyčka, režimy, routing | `rag/agent/loop.py` |
| Přístup k modelu + skriptovaný model pro testy | `rag/agent/llm.py` |
| Stav rozpracovaného dokumentu | `rag/agent/session.py`, tabulky v `rag/sql/0005_lawyer_sessions.sql` |
| Deterministická revize cizí smlouvy | `rag/agent/review.py` |
| Nástroje jako HTTP služba | endpointy v `rag/server.py` (zapnou se s `--cite-registry` + PG) |
| Eval | `rag/eval/lawyer_agent/` — `make eval-agent` |
| Testy | `rag/tests/test_agent.py` (v `make test`) |

## Nástroje

| tool | vstup | co vrací | mění stav |
|---|---|---|---|
| `search_law` | query, oblast?, top_k? | úryvky s § a přilehlými § | ne |
| `get_paragraph` | zakon, paragraf, odstavec? | plné znění, nadpis, datum účinnosti, citace | ne |
| `list_templates` | dotaz? | 14 šablon s popisem a stranami | ne |
| `get_template` | typ | proměnné s otázkami, klauzule, kontroly, checklist, upozornění | ne |
| `save_intake` | session_id, typ?, promenne?, vypnute_klauzule? | co chybí (max 3 otázky), porušené limity, připravenost | **ano** |
| `render_document` | session_id, format | markdown + checklist + upozornění, nebo důvod odmítnutí | **ano** |
| `review_document` | text, typ? | audit citací, nálezy limitů, částky, lhůty, pokrytí checklistu | ne |
| `ask_user` | otazky[] (max 3) | strukturované otázky pro kartu v klientovi | ne |

Popisy nástrojů jsou česky a konkrétní (model z nich volí). Každé volání se loguje
do `lawyer_tool_calls` (nástroj, vstup, shrnutí výstupu, ms, chyba) a jde přečíst
přes `GET /agent/log/{session_id}`.

## Co je tvrdé a nezávisí na modelu

1. **`render_document` odmítne render**, dokud chybí povinný údaj nebo je porušený
   kogentní limit ze šablony. Model to nemůže přemluvit — vrátí se mu důvod
   s citací §.
2. **Nástroje s vedlejším efektem se nevolají z obsahu souboru.** Krok
   s `zdroj="dokument"` má `povolit_zmeny=False`; `save_intake` a `render_document`
   pak vrátí chybu („nelze volat na základě obsahu nahraného souboru"). Text
   dokumentu jde do promptu v ohraničeném bloku jako data (plán §6).
3. **Limit volání na krok** (`--agent-max-volani`, default 8). Po vyčerpání dostane
   model šanci odpovědět textem, bez nástrojů.
4. **Retence**: každá session má `expires_at` (`--retence-dnu`, default 30) a
   `make agent-uklid` expirované maže. Nadiktované údaje jsou osobní údaje.

## Endpointy

```
GET  /law/paragraph?zakon=89/2012 Sb.&paragraf=§ 2254[&odstavec=1]
GET  /templates[?dotaz=]              GET /templates/{typ}
POST /agent/intake {session_id, typ?, promenne?, vypnute_klauzule?}
GET  /agent/intake/{session_id}       → další otázky bez modelu
POST /agent/render {session_id}       POST /agent/review {text, typ?}
GET  /agent/sessions                  DELETE /agent/sessions/{session_id}
GET  /agent/log/{session_id}
POST /agent/chat {message, session_id?, mode?, zdroj?}   ← potřebuje model
```

`/law/paragraph` má zákon v query parametru, ne v cestě — číslo předpisu obsahuje
lomítko. Tím je zároveň doplněné to, co chtěl plán RAG (`GET /law/paragraph`).

Ověřeno proti běžící službě na SPARKu (2026-09-28): `/templates` vrací 14 šablon,
`/law/paragraph` § 75 ZP, intake → odmítnutý render → doplnění → render 1 266 znaků
plné moci, log čtyř volání s časy.

## Eval — 22 scénářů, všechny bez LLM

`make eval-agent`:

* **draft, 12 scénářů** (`scenarios_draft.jsonl`): skriptovaný uživatel sype
  odpovědi po trojicích jako z karet `ask_user`. Kontroluje se, že intake
  postupuje, dokument se vyrenderuje jen když má, v hotovém nezůstal `{{…}}`
  a je v něm zápatí s upozorněním. Pět scénářů je **záměrně nezákonných** (jistota
  5× nájemné, DPP na 400 hodin, zkušební doba 6 měsíců, DPČ 30 h/týden,
  předžalobní výzva se třídenní lhůtou, výpověď zaměstnavatele bez důvodu,
  úplatná licence bez odměny) — u nich se ověřuje, že render **nevznikne** a že
  hláška cituje správné pravidlo.
* **review, 10 scénářů** (`scenarios_review.jsonl`): podstrčené vady v textu cizí
  smlouvy — jistota 5× nájemné, citace `§ 2239 odst. 3`, který v účinném znění
  není, vymyšlené `§ 9998`, chybějící cena díla, citace bez uvedení předpisu —
  plus jedna čistá smlouva jako kontrola falešných poplachů.

Výsledek 2026-09-28: **12/12 draft, 10/10 review**. Co se změřit nedá: task
success s reálným modelem, počet volání modelu a latence — to je na modelové okno
(§7 plánu zůstává v téhle části otevřený).

## Fáze plánu

| Fáze | Stav |
|---|---|
| 1. Tool-calling skeleton + QA s citacemi | **kód hotový**, nasazený; nezměřeno, protože neběží model |
| 2. Draft pro 3 šablony + intake UI | **backend hotový pro všech 14 šablon** (intake, limity, render, sessiony); UI ve Flutteru ne — viz níž |
| 3. Review | **deterministická část hotová** (audit citací, limity, checklist); segmentace a posouzení klauzulí modelem ne; docx/pdf na vstupu ne |
| 4. Zbytek šablon, sessiony, judikatura | šablony hotové (14), sessiony hotové, judikatura ne |

## Co chybí a proč

* **Model.** Rozhodnuto benchmarkem 2026-10-01 (AiStack `PLAN-model-bench.md` §6a):
  agent = **Gemma-4-31B** přes alias LiteLLM **`pravnik-agent`** (draft 92 %, revize
  100 %, odolnost vůči injection 100 %, ~7 tok/s, ~4–5 min na návrh). Gemma běží jen
  v profilu SPARKu **gemma** na vyžádání (AiStack `PLAN-spark-scheduler.md`), mimo něj
  alias padá na `openclaw-default` (qwen36: 50/50 %, injection 60 %) a `fallback`.
  Zbývá: přepnout `/agent/chat` z `translate` na `pravnik-agent` a změřit draft
  v okně gemma. Director (Nemotron, `qwen3_coder` parser) a translate (bez tool
  parseru) pro agenta nepoužívat.
* **Flutter UI** (plán §4): karty pro `ask_user`, progress bar podle povinných
  proměnných, průběžný náhled, seznam uložených sessionů. Server pro to má
  všechno (`otazky` v odpovědi `/agent/chat`, `GET /agent/intake/{id}`,
  `GET /agent/sessions`), ale klient to zatím nevykresluje — appka dnes mluví jen
  SSE dialektem `/chat/stream`.
* **Vstup docx/pdf do revize.** `review_document` bere text. Konverze chybí
  (`pandoc` na M2 není, rozhodnutí o knihovně je stejné jako u exportu šablon).
* **Export docx/pdf** z renderu (`format` zatím jen `md`).
* **Judikatura** jako druhá kolekce (plán fáze 4, zdroj prověřený v `CURRENT_STATE.md`).

## Otevřené otázky z plánu — a co k nim vyšlo

**Který model bude mozek?** Park má dva kandidáty a ani jeden neběží přes den:
`swarm-director` = Nemotron-3-Super-120B-A12B (0.60 GPU util, tool parser
`qwen3_coder` — podezřelé), `openclaw-default` = Qwen3.6-35B-A3B (36 GiB,
rezidentní jen 00:08–00:55, v konfiguraci výslovně „jiný model by rozbil tool
calling"). Nemotron-3-**Nano**, se kterým plán počítal na routing, v parku není.
Doporučení: **Qwen3.6-35B jako mozek agenta** (menší, určený na tool calling)
a routing nechat na heuristice (`--agent-router heuristika`), která je v kódu
a v testech — LLM router by za jeden krok agenta přidal další volání modelu bez
měřitelného přínosu. To ale znamená nechat qwen36-agent běžet i mimo noční okno,
tedy rozhodnout o 36 GiB z 121.

**Jen pro tebe, nebo produkt?** Kód na to nespoléhá: sessiony, retence a log
volání jsou hotové, auth ne. Až bude jasno, přidat autorizaci na `/agent/*`
(dnes je služba jen v LAN a za Cloudflare Access) a zkrátit retenci.
