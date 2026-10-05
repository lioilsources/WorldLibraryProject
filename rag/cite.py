"""Deterministický lookup citace paragrafu pro Právníka — intent `cite`.

„Co říká § 2079 občanského zákoníku?", „§ 51 odst. 1 ZP", „čl. 10 Listiny",
„§ 29 trestního zákoníku": nejcennější právnický dotaz nemá procházet
vektorovým hledáním. Parser vytáhne odkaz (§/čl., odstavec, písmeno, bod) a
předpis (číslo „89/2012", kmen z aliasů registru, nebo zkratka jako celý token
— `OZ`, `ZP`, `TZ`; zkratky se schválně nedávají mezi kmenové aliasy, protože
kmen „zp" od hranice slova chytí „zpět"), a server pak vezme celý § z Postgresu.

Čistá logika bez DB: `python3 cite.py` spustí selftest. Registr zkratek a
aliasů se čte z registry/law/tier1.yaml (tentýž, ze kterého jde ingest).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from retrieval import fold

# „§ 2079 odst. 1 písm. a) bod 2", „§2079odst.1", „paragraf 2079", „čl. 10 odst. 2"
_REF = re.compile(
    # písmenná přípona § („§ 5a") jen když za ní nejde další písmeno — jinak by
    # „§51odst.2" dalo „§ 51o"
    r"(?:(?P<par>§|paragraf(?:u|em|y)?)\s*(?P<pnum>\d+(?:[a-z](?![a-z]))?)"
    r"|(?P<cl>čl\.?|článek|článku|clanek|clanku)\s*(?P<cnum>\d+(?:[a-z](?![a-z]))?))"
    r"(?:\s*odst(?:\.|avec|avce|avci)?\s*(?P<odst>\d+[a-z]?))?"
    r"(?:\s*písm(?:\.|eno|ene)?\s*(?P<pism>[a-z])\)?)?"
    r"(?:\s*bod(?:u|em)?\s*(?P<bod>\d+))?",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"\b(\d{1,3})/(\d{4})\b")


@dataclass
class Citation:
    ref: str                 # „§ 2079" / „čl. 10" — jak stojí v chapters.ref
    odst: str | None = None
    pism: str | None = None
    bod: str | None = None
    act_hint: str | None = None   # work_id, když ho dotaz jmenuje (číslem, aliasem, zkratkou)

    @property
    def unit_ref(self) -> str | None:
        """Odstavec, na který se ptá — tvar chunks.ref_start."""
        return f"{self.ref} odst. {self.odst}" if self.odst else None

    @property
    def label(self) -> str:
        out = self.ref
        if self.odst:
            out += f" odst. {self.odst}"
        if self.pism:
            out += f" písm. {self.pism})"
        if self.bod:
            out += f" bod {self.bod}"
        return out


def ref_span(a: str, b: str) -> str:
    """„§ 2079 odst. 1" + „§ 2079 odst. 3" → „§ 2079 odst. 1–3"; stejné → jedno.
    Sdílí ho ingest (title chunku) i server (title hitu z PG), aby appka
    viděla tentýž tvar bez ohledu na to, odkud hit přišel."""
    if a == b:
        return a
    ma, mb = re.match(r"(.*) odst\. (\S+)$", a or ""), re.match(r"(.*) odst\. (\S+)$", b or "")
    if ma and mb and ma.group(1) == mb.group(1):
        return f"{ma.group(1)} odst. {ma.group(2)}–{mb.group(2)}"
    return f"{a} – {b}"


class LawRegistry:
    """Číslo / alias / zkratka → work_id, z registry/law/tier1.yaml."""

    def __init__(self, path: Path):
        reg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        self.by_number: dict[str, str] = {}
        self.aliases: list[tuple[str, str]] = []     # (folded kmen, work_id), delší napřed
        self.abbr: dict[str, str] = {}               # zkratka (jak je psaná) → work_id
        self.short: dict[str, str] = {}
        for a in reg.get("acts") or []:
            cislo, rok = str(a["cislo"]).split("/")
            wid = a.get("id") or f"cz.{a.get('sb', 'sb')}.{rok}.{cislo}"
            self.by_number[f"{cislo}/{rok}"] = wid
            self.short[wid] = a.get("short") or a["cislo"]
            for al in a.get("aliases") or []:
                self.aliases.append((fold(al), wid))
            for ab in a.get("abbr") or []:
                self.abbr[ab] = wid
        self.aliases.sort(key=lambda kv: -len(kv[0]))
        # zkratka jako celý token; krátké (≤ 2 znaky) jen VELKÝMI, delší i malými,
        # ale vždy ohraničené — „OZ" ano, „ozdoba" ne
        self._abbr_re = {
            ab: re.compile(r"(?<![\w§])" + re.escape(ab) + r"(?!\w)", 0 if len(ab) <= 2 else re.IGNORECASE)
            for ab in self.abbr
        }

    def resolve(self, text: str) -> str | None:
        m = _NUMBER.search(text)
        if m and f"{m.group(1)}/{m.group(2)}" in self.by_number:
            return self.by_number[f"{m.group(1)}/{m.group(2)}"]
        folded = fold(text)
        for alias, wid in self.aliases:
            if re.search(r"\b" + re.escape(alias), folded):
                return wid
        for ab, rx in self._abbr_re.items():
            if rx.search(text):
                return self.abbr[ab]
        return None


def parse_citation(text: str, registry: LawRegistry | None = None) -> Citation | None:
    """První citace v dotazu, nebo None, když se dotaz na konkrétní ustanovení neptá."""
    m = _REF.search(text)
    if not m:
        return None
    if m.group("pnum"):
        ref = f"§ {m.group('pnum').lower()}"
    else:
        ref = f"čl. {m.group('cnum').lower()}"
    return Citation(
        ref=ref,
        odst=(m.group("odst") or None),
        pism=(m.group("pism") or "").lower() or None,
        bod=m.group("bod") or None,
        act_hint=registry.resolve(text) if registry else None,
    )


def lookup(conn, cit: Citation, candidates: list[str] | None = None, limit_acts: int = 6) -> list[dict]:
    """Chunky paragrafu z Postgresu: [{chunk_id, work_id, text, ref_start, ref_end, …}].

    S act_hint jen ten předpis; bez něj všechny předpisy priority 1 (nebo
    `candidates`), aby volající mohl při víc zásazích položit protiotázku."""
    sql = """
        SELECT c.id, c.work_id, w.title, w.name_cs, w."group", c.text, c.ref_start, c.ref_end, c.seq,
               c.seq_in_chapter, ch.id, ch.path, ch.heading, w.source_path, w.edition
        FROM chunks c
        JOIN chapters ch ON ch.id = c.chapter_id
        JOIN works w ON w.id = c.work_id
        WHERE ch.ref = %s AND ch.level = 6
    """
    params: list = [cit.ref]
    if cit.act_hint:
        sql += " AND c.work_id = %s"
        params.append(cit.act_hint)
    elif candidates:
        sql += " AND c.work_id = ANY(%s)"
        params.append(list(candidates))
    else:
        sql += " AND w.priority = 1"
    sql += " ORDER BY w.priority, w.id, c.seq"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({"chunk_id": r[0], "work_id": r[1], "work": r[2], "name_cs": r[3], "group": r[4], "text": r[5],
                    "ref_start": r[6], "ref_end": r[7], "seq": r[8], "seq_in_chapter": r[9], "chapter_id": r[10],
                    "chapter_path": r[11], "heading": r[12], "source_path": r[13], "edition": r[14]})
    acts = []
    for h in out:
        if h["work_id"] not in acts:
            acts.append(h["work_id"])
    if len(acts) > limit_acts:
        keep = set(acts[:limit_acts])
        out = [h for h in out if h["work_id"] in keep]
    return out


def narrow_to_unit(hits: list[dict], cit: Citation) -> list[dict]:
    """Když se ptá na konkrétní odstavec a § je rozdělený do víc chunků, vrátí
    ten, který odstavec obsahuje (podle ref_start/ref_end); jinak vše."""
    unit = cit.unit_ref
    if not unit or not cit.odst:
        return hits
    want = int(re.sub(r"\D", "", cit.odst) or 0)

    def covers(h):
        a, b = h.get("ref_start") or "", h.get("ref_end") or ""
        ma, mb = re.search(r"odst\. (\d+)", a), re.search(r"odst\. (\d+)", b)
        if not ma:
            return True     # celý § v jednom chunku bez odstavců
        lo, hi = int(ma.group(1)), int(mb.group(1)) if mb else int(ma.group(1))
        return lo <= want <= hi

    narrowed = [h for h in hits if covers(h)]
    return narrowed or hits


# --- selftest --------------------------------------------------------------------

def _selftest() -> int:
    reg = LawRegistry(Path(__file__).parent / "registry" / "law" / "tier1.yaml")
    cases = [
        ("Co říká § 2079 občanského zákoníku?", "§ 2079", None, "cz.sb.2012.89"),
        ("§ 51 odst. 1 ZP", "§ 51", "1", "cz.sb.2006.262"),
        ("§51odst.2 zákoníku práce", "§ 51", "2", "cz.sb.2006.262"),
        ("čl. 10 Listiny", "čl. 10", None, "cz.sb.1993.2"),
        ("článek 1 odst. 1 Ústavy", "čl. 1", "1", "cz.sb.1993.1"),
        ("paragraf 29 trestního zákoníku — nutná obrana", "§ 29", None, "cz.sb.2009.40"),
        ("§ 2079 odst. 2 písm. a) bod 1 zákona č. 89/2012 Sb.", "§ 2079", "2", "cz.sb.2012.89"),
        ("§ 5a OZ", "§ 5a", None, "cz.sb.2012.89"),
        ("co je v § 12", "§ 12", None, None),
        ("jak zpět vrátit zboží podle § 1829", "§ 1829", None, None),   # „zpět" nesmí být ZP
        ("ozdoba podle § 3", "§ 3", None, None),                         # „ozdoba" nesmí být OZ
        ("§ 4 zákona o ochraně spotřebitele", "§ 4", None, "cz.sb.1992.634"),
    ]
    bad = 0
    for text, ref, odst, act in cases:
        c = parse_citation(text, reg)
        got = (c.ref, c.odst, c.act_hint) if c else None
        if got != (ref, odst, act):
            bad += 1
            print(f"FAIL {text!r}: {got} != {(ref, odst, act)}")
    for text in ("Jaká je výpovědní doba u pracovního poměru?", "kolik je 2 + 2", "od 1. 1. 2026"):
        if parse_citation(text, reg):
            bad += 1
            print(f"FAIL {text!r}: neměla být citace")
    assert parse_citation("§ 2079 odst. 2 písm. a) bod 1").label == "§ 2079 odst. 2 písm. a) bod 1"
    hits = [{"ref_start": "§ 9 odst. 1", "ref_end": "§ 9 odst. 2"}, {"ref_start": "§ 9 odst. 3", "ref_end": "§ 9 odst. 4"}]
    assert narrow_to_unit(hits, Citation("§ 9", odst="3")) == hits[1:]
    assert narrow_to_unit(hits, Citation("§ 9")) == hits
    print("cite selftest:", "OK" if not bad else f"{bad} chyb")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
