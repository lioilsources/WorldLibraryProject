"""Rozpočet vstupu souhrnů kapitol a 400 „přes kontext" — bez modelu i DB.

2026-10-02 stála noční služba library-chapters 100 minut: kapitola v původním
písmu přelezla max-model-len directora a 400 se brala jako výpadek modelu.
"""

from types import SimpleNamespace

from enrich_chapters import est_tokens, fit, fit_list, windows
from llm_batch import LLMBatch, is_context_error


def test_odhad_je_pro_puvodni_pismo_pesimisticky():
    assert est_tokens("a" * 3500) < 1100            # latinka ~3,5 znaku/token
    assert est_tokens("道" * 1000) >= 1000           # han: aspoň token na znak
    # dřívější strop 60 000 znaků by u čínštiny dal ~70k tokenů, ne „~20k"
    assert est_tokens("道" * 60_000) > 32_768


def test_odhad_nepodstreli_rejstrik_ani_pali():
    # Skutečnost podle tokenizéru directora (vLLM /tokenize, 2026-10-02):
    # rejstřík Avesty 51 684 znaků = 32 070 tokenů, první odhad dal 16 273.
    rejstrik = "p. 362 p. 363\nAhura Mazda, i. 4, 12; ii. 7, 33-35; xix. 1.\n" * 800
    assert est_tokens(rejstrik) >= len(rejstrik) / 1.7
    # páli s diakritikou: ~2 znaky na token, první odhad dal ~2,7
    pali = "Kusalā dhammā, akusalā dhammā, abyākatā dhammā. " * 1000
    assert est_tokens(pali) >= len(pali) / 2.2


def test_fit_a_fit_list_drzi_rozpocet():
    text = "學而時習之" * 2000
    assert est_tokens(fit(text, 1000)) <= 1000
    assert fit("krátké", 1000) == "krátké"
    assert fit_list(["a" * 30, "b" * 30, "c" * 3000], 30) == ["a" * 30, "b" * 30]


def test_okna_se_vejdou_i_s_prerostlym_chunkem():
    chunks = ["道" * 500] * 40 + ["德" * 50_000]     # poslední chunk sám přes rozpočet
    out = windows(chunks, 4000)
    assert len(out) > 1
    assert all(est_tokens(w) <= 4000 + 40 for w in out)
    assert out[-1].startswith("德")


class Boom(Exception):
    def __init__(self, msg, status_code=400):
        super().__init__(msg)
        self.status_code = status_code


VLLM_400 = ("Error code: 400 - {'object': 'error', 'message': \"This model's maximum context length "
            "is 32768 tokens. However, you requested 34120 tokens (32520 in the messages, 1600 in the "
            "completion).\", 'type': 'BadRequestError'}")


def test_chyba_kontextu_se_pozna():
    assert is_context_error(Boom(VLLM_400))
    assert not is_context_error(Boom("Connection refused", status_code=None))
    assert not is_context_error(Boom("Internal server error", status_code=500))


def test_chyba_kontextu_neni_vypadek_polozka_padne_a_jede_se_dal():
    calls = []

    def create(**_kw):
        calls.append(1)
        raise Boom(VLLM_400)

    llm = LLMBatch("http://x/v1", "swarm-director", workers=1)
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert llm.one([{"role": "user", "content": "x"}]) == (None, "")
    assert len(calls) == 1                           # žádné čekání a opakování
    assert llm.stats.too_long == 1
    assert "přes kontext" in llm.stats.rate()
