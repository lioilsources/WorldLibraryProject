"""Čištění textu před detekcí kapitol a chunkováním.

Všechno, co tu je, vzniklo z měření na korpusu, ne z opatrnosti:
- pálijské PDF (VRI) nesou na každé straně patičku „Vipassana Research
  Institute / www.tipitaka.org / Page N sur M" — bylo to v 10 982 chuncích
  a překladač z toho dělal nesmysl;
- Hegelovy Suhrkamp PDF mají 18–21 % řádků rozdělených spojovníkem
  („Ungenügsam-\\nkeit"), což rozbíjí fulltext i embedding;
- Mahábhárata (Ganguli, TXT) má CRLF, na kterém `^…$` regexy tiše selžou.

Funkce jsou čisté a bezstavové; `clean(text, lang)` je vstupní bod, který
skládá jen to, co pro daný jazyk dává smysl.
"""

import re

# --- patičky VRI (pálí) ------------------------------------------------------

_VRI_FOOTER_LINES = (
    re.compile(r"^\s*Vipassana Research Institute\s*$", re.IGNORECASE),
    re.compile(r"^\s*(?:https?://)?www\.tipitaka\.org\s*$", re.IGNORECASE),
    re.compile(r"^\s*Page\s+\d+\s+(?:sur|of)\s+\d+\s*$", re.IGNORECASE),
    re.compile(r"^\s*\d{1,4}\s*$"),  # holé číslo stránky
)


def strip_vri_footer(text: str) -> str:
    """Odstraní řádky patičky VRI. Holé číslo se maže jen tehdy, když
    sousedí s jiným patičkovým řádkem — samotná čísla v textu (verše
    Dhammapady „469.") mají tečku a tady neprojdou, ale řádek „12" uprostřed
    výčtu by mohl; sousedství s patičkou to hlídá."""
    lines = text.split("\n")
    is_footer = [any(rx.match(line) for rx in _VRI_FOOTER_LINES[:3]) for line in lines]
    is_number = [bool(_VRI_FOOTER_LINES[3].match(line)) for line in lines]
    out = []
    for i, line in enumerate(lines):
        if is_footer[i]:
            continue
        if is_number[i]:
            near = any(is_footer[j] for j in range(max(0, i - 2), min(len(lines), i + 3)))
            if near:
                continue
        out.append(line)
    return "\n".join(out)


# --- spojovníky na konci řádku (němčina) -------------------------------------

# řádek končí spojovníkem, další začíná malým písmenem → jedno slovo;
# výjimka: pokračuje-li spojkou (Vor- und Nachteile), je spojovník součást
# souřadné složeniny a zůstává
_HYPHEN_BREAK = re.compile(r"(\w)[-¬]\n(?!(?:und|oder|bzw|sowie)\b)(?=[a-zäöüß])")


def dehyphenate_de(text: str) -> str:
    """„Ungenügsam-\\nkeit" → „Ungenügsamkeit". Sešívá jen tam, kde
    pokračování začíná malým písmenem; „Vor- und Nachteile" (další řádek
    „und") zůstává."""
    def join(m: re.Match) -> str:
        return m.group(1)
    return _HYPHEN_BREAK.sub(join, text)


def hyphen_break_ratio(text: str) -> float:
    """Podíl řádků končících spojovníkem — metrika pro ověření."""
    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        return 0.0
    return sum(1 for line in lines if line.rstrip().endswith(("-", "¬"))) / len(lines)


# --- obecné ------------------------------------------------------------------

_NBSP = " "


def normalize_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(_NBSP, " ")
    text = re.sub(r"[ \t]+\n", "\n", text)          # trailing mezery
    text = re.sub(r"\n{3,}", "\n\n", text)          # max jeden prázdný řádek
    return text


def clean(text: str, lang: str, source_kind: str = "txt") -> str:
    """Vstupní bod: whitespace vždy; VRI patičky u pálí z PDF; spojovníky
    u němčiny z PDF."""
    text = normalize_whitespace(text)
    if lang == "pi" and source_kind == "pdf":
        text = strip_vri_footer(text)
    if lang == "de" and source_kind == "pdf":
        text = dehyphenate_de(text)
    return text


def _selftest() -> None:
    vri = ("469. Tassa puṭṭho viyākāsi;\nPage 111 sur 233\nVipassana Research Institute\n"
           "www.tipitaka.org\n112\nVipākaṃ pāpakammānaṃ.\n")
    out = strip_vri_footer(vri)
    assert "Vipassana" not in out and "tipitaka.org" not in out and "Page 111" not in out
    assert "\n112\n" not in out                     # číslo stránky u patičky pryč
    assert "469." in out and "Vipākaṃ" in out
    # holé číslo daleko od patičky zůstává (může být součást textu)
    assert strip_vri_footer("a\nb\nc\n7\nd\ne\nf") == "a\nb\nc\n7\nd\ne\nf"

    de = "die Ungenügsam-\nkeit des Geistes und Vor-\nund Nachteile"
    out = dehyphenate_de(de)
    assert "Ungenügsamkeit" in out, out
    assert "Vor-\nund" in out, out                  # souřadná složenina zůstala
    assert hyphen_break_ratio("a-\nb\nc") == 1 / 3

    assert normalize_whitespace("a\r\nb\r\n\r\n\r\nc  \n") == "a\nb\n\nc\n"
    assert clean("x\r\ny", "en") == "x\ny"
    print("clean_text.py: selftest ok")


if __name__ == "__main__":
    _selftest()
