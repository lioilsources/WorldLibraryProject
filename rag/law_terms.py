"""Mapa laických pojmů → právní terminologie a předpis (`registry/law/legal_terms.yaml`).

Levný krok před vyhledáním, bez LLM: vektor nerozliší tři procesní kodexy
(„kdo platí náklady soudního řízení" vrací ZŘS a SŘS místo OSŘ) a laické
slovo často nestojí v zákoně vůbec („kauce" je „jistota", „vyhodit z bytu" je
„výpověď nájmu"). Mapa přidá k dotazu právní termíny (jdou do embeddingu) a
smí doporučit předpis, ale jen jako **záložní** směrování — když si dotaz
předpis jmenuje sám, aliasy v `retrieval.route()` mají přednost.

Kmeny se hledají jako v `route()`: folded (bez diakritiky, malá písmena) od
hranice slova, takže „kauci" trefí „kauce" i „kauci" bez výpisu všech pádů.

Čistá logika bez DB: `python3 law_terms.py` spustí selftest.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from retrieval import fold


class TermsMap:
    """Laický kmen → (právní termíny, doporučené předpisy)."""

    def __init__(self, entries: list[dict] | None = None):
        self.entries: list[dict] = []
        for e in entries or []:
            lays = [fold(x) for x in (e.get("lay") or []) if x]
            if not lays:
                continue
            self.entries.append({
                "lay": lays,
                "terms": list(e.get("terms") or []),
                "works": list(e.get("works") or []),
                "note": e.get("note") or "",
            })
        # delší kmen napřed: „naklady rizeni" má vyhrát nad „rizeni"
        self.entries.sort(key=lambda e: -max(len(l) for l in e["lay"]))

    @classmethod
    def load(cls, path: str | Path | None) -> TermsMap:
        if not path:
            return cls([])
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
        return cls(data if isinstance(data, list) else data.get("terms") or [])

    def __len__(self) -> int:
        return len(self.entries)

    def match(self, query: str) -> tuple[list[str], list[str]]:
        """→ (termíny k přidání do dotazu, doporučené předpisy). Bez duplikátů,
        v pořadí od nejdelšího trefeného kmene."""
        folded = fold(query)
        terms: list[str] = []
        works: list[str] = []
        for e in self.entries:
            if not any(re.search(r"\b" + re.escape(l), folded) for l in e["lay"]):
                continue
            for t in e["terms"]:
                if t not in terms:
                    terms.append(t)
            for w in e["works"]:
                if w not in works:
                    works.append(w)
        return terms, works

    def expand(self, query: str) -> tuple[str, list[str]]:
        """Dotaz pro embedding (původní + termíny) a doporučené předpisy."""
        terms, works = self.match(query)
        return (f"{query} {' '.join(terms)}" if terms else query), works


def _selftest() -> None:
    m = TermsMap([
        {"lay": ["naklady soudniho rizeni", "kdo plati soud"], "terms": ["náhrada nákladů řízení"],
         "works": ["99/1963 Sb."]},
        {"lay": ["kauc"], "terms": ["jistota"]},
        {"lay": ["rizeni"], "terms": ["nic"]},
    ])
    q, works = m.expand("Kdo platí náklady soudního řízení?")
    assert "náhrada nákladů řízení" in q and works == ["99/1963 Sb."], (q, works)
    # delší kmen napřed, ale trefí se i obecný — pořadí termínů podle délky kmene
    assert m.match("Kdo platí náklady soudního řízení?")[0][0] == "náhrada nákladů řízení"
    # kmen, ne celé slovo: „kauc" chytí „kauce" i „kauci" (proto se píší kmeny)
    assert m.match("Jak vysokou kauci může chtít?")[0] == ["jistota"]
    assert m.match("Vrátí mi pronajímatel kauce?")[0] == ["jistota"]
    # od hranice slova: „rizeni" nesmí chytit „bezpříčinné" apod. uvnitř slova
    assert m.match("Jak vysoká je smluvní pokuta?") == ([], [])
    # bez mapy se nic nemění
    empty = TermsMap([])
    assert empty.expand("cokoli") == ("cokoli", []) and len(empty) == 0
    print("law_terms.py: selftest ok")


if __name__ == "__main__":
    _selftest()
