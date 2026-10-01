"""Index bundlů pro Kindlify — čistě nad soubory, bez Postgresu."""

import json

from export_bundle import asset_name, build_bundle
from kindlify_sync import INDEX, build_index
from test_export_bundle import CHAPTERS, CHUNK_ROWS, WORK


def write(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def test_index_drzi_rucni_label_dema_a_bere_exportovana_dila(tmp_path):
    demo = {"manifest": {"slug": "dao-de-jing", "title": "Tao Te Ťing", "sourceLanguage": "zh",
                         "pipelineVersion": "demo-1.2",
                         "tree": {"id": "root", "children": [{"id": "a", "children": []}]}}}
    write(tmp_path / "dao_de_jing.json", demo)
    write(tmp_path / INDEX, {"bundles": [{"asset": "dao_de_jing", "label": "Tao Te Ťing (demo)"}]})
    bundle = build_bundle(WORK, CHAPTERS, CHUNK_ROWS, generated_at="2026-10-01T00:00:00Z")
    write(tmp_path / asset_name(bundle["manifest"]["slug"]), bundle)
    (tmp_path / "rozbity.json").write_text("{", encoding="utf-8")

    entries = build_index(tmp_path)["bundles"]
    assert [e["asset"] for e in entries] == ["dao_de_jing", "zh_daodejing"]   # demo napřed
    assert entries[0]["label"] == "Tao Te Ťing (demo)"                       # ruční label přežije
    assert entries[0]["nodes"] == 2
    assert entries[1]["label"] == "Tao te ťing"
    assert entries[1]["slug"] == "zh-daodejing"
    assert entries[1]["pipelineVersion"].startswith("pg-1+")
