"""Stream knihovního chatu musí vrátit odpověď i od serveru s reasoning parserem.

Noční režim posílá chat na swarm-directora, který běží s
`--reasoning-parser nemotron_v3`. Ten ve streamu klasifikoval celý výstup jako
`reasoning_content`, `content` zůstal prázdný a generátor tak neposlal ani
jednu deltu — klient čekal do timeoutu a hlásil „stream skončil bez odpovědi".
"""
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _chunk(content=None, reasoning=None, model="swarm-director"):
    delta = SimpleNamespace(content=content)
    if reasoning is not None:
        delta.reasoning_content = reasoning
    return SimpleNamespace(model=model, choices=[SimpleNamespace(delta=delta)])


def drain(chunks):
    """Kopie smyčky z RAGServer.chat_stream — deltas, které by šly ke klientovi."""
    parts, reasoning, sent = [], [], []
    for chunk in chunks:
        if not chunk.choices:
            continue
        choice_delta = chunk.choices[0].delta
        reasoning.append(getattr(choice_delta, "reasoning_content", None) or "")
        delta = choice_delta.content or ""
        if delta:
            parts.append(delta)
            sent.append(delta)
    if not parts and any(reasoning):
        salvaged = THINK_RE.sub("", "".join(reasoning)).strip()
        if salvaged:
            parts.append(salvaged)
            sent.append(salvaged)
    return sent, THINK_RE.sub("", "".join(parts)).strip()


def test_bezny_server_posila_content():
    sent, answer = drain([_chunk(content="Proces "), _chunk(content="je román.")])
    assert sent == ["Proces ", "je román."]
    assert answer == "Proces je román."


def test_reasoning_parser_ve_streamu_neni_ztrata():
    """Vše přišlo jako reasoning_content — odpověď se musí zachránit, ne zmizet."""
    sent, answer = drain([
        _chunk(content=None, reasoning="Proces "),
        _chunk(content=None, reasoning="je román."),
    ])
    assert sent == ["Proces je román."], "klient nesmí dostat prázdný stream"
    assert answer == "Proces je román."


def test_think_bloky_se_do_odpovedi_nedostanou():
    sent, answer = drain([
        _chunk(content=None, reasoning="<think>uživatel se ptá na Kafku</think>Je to román."),
    ])
    assert "<think>" not in "".join(sent)
    assert answer == "Je to román."


def test_kdyz_prijde_content_reasoning_se_ignoruje():
    """Když parser funguje, přemýšlení uživateli neposíláme."""
    sent, answer = drain([
        _chunk(content=None, reasoning="Hmm, přemýšlím…"),
        _chunk(content="Je to román."),
    ])
    assert sent == ["Je to román."]
    assert "přemýšlím" not in answer


def test_prazdny_stream_zustane_prazdny():
    sent, answer = drain([_chunk(content=None, reasoning=None)])
    assert sent == []
    assert answer == ""


def test_json_delty_jsou_serializovatelne():
    sent, _ = drain([_chunk(content=None, reasoning="Příliš žluťoučký kůň.")])
    line = "data: " + json.dumps({"delta": sent[0]}, ensure_ascii=False)
    assert "žluťoučký" in line
