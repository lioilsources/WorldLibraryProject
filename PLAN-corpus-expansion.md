# PLAN — rozšíření korpusu o chybějící tradice (jen originály)

Plán pro Claude Code (Opus). Vznikl na M2; pokračování na MacBooku popisuje
`Prompts/handoff-macbook-corpus-expansion.md`. Cíl: doplnit do knihovny díla, která
v ní chybí, **v původním jazyce**, ze strojově čitelných zdrojů, a dostat
je celou cestou: `downloads/` → ingest → Postgres + Chroma → obohacení →
Knihovník (chat) → bundly pro Kindlify.

Stav zdrojů je ověřený k **2026-11-22** (existence rep přes GitHub API,
dostupnost webů přes HTTP). Co ověřené není, je označené „ověřit".

## Jak s plánem pracovat

- Jedna fáze = jeden PR. Fáze 0 je předpoklad všech ostatních, fáze 1–7
  jsou na sobě nezávislé a jdou v uvedeném pořadí (podle poměru užitek/práce).
- Po každé fázi: `make test`, `make ingest-stats`, eval proti baseline
  (`make eval`), krátké hlášení. Nezastavuj se, pokračuj další fází.
- Rozhodnutí vlastníka jsou zapsaná na konci (sekce **Rozhodnuto**). Stůj
  jen u rozhodnutí, které tam není. Mazání z databáze má vlastní postup:
  záloha, dry-run s počty, teprve pak ostrý běh.
- GPU práce na SPARKu (embedding, obohacení) jde přes `spark-broker`.
- Repo je veřejné: do commitů a docs nepatří adresy strojů ani lokální cesty.

## 1. Výchozí stav (změřeno z `rag/works.jsonl` a `downloads/`)

| Skupina | Děl | Jazyk v korpusu | Poznámka |
|---|---:|---|---|
| greek_latin (Perseus) | 1080 | grc 768, lat 312 | originály, TEI |
| pali | 61 | pi | Tipitaka mūla z PDF, bez komentářů |
| sanskrit | 19 | **en** | Mahábhárata (18 parv) a Rgvéd jen v překladu |
| chinese | 4 | lzh 3, **en** 1 | Čuang-c' jen anglicky (Giles) |
| european_philosophy | 3 | de | tři svazky Hegela; Kant vyřazen (fraktura) |
| european | 4 | **en** | Beowulf 2×, Edda 2× — jen překlady |
| avesta, egypt | 2 | **en** | Darmesteter, Allen — jen překlady |

Celkem 1173 děl, 164 tis. chunků. **26 děl je anglický překlad**, což je
proti zásadě z `CLAUDE.md` („knihovna drží originály").

Stažené, ale nepoužitelné (viz `EXCLUDE_PATH_MARKERS` v `rag/ingest_books.py`):
hebrejská Bible (PDF s mojibake), Rámájana a BORI Mahábhárata (skeny),
Avesta Geldner (sken bez textu), mayské kodexy (obrazová faksimile), Kant.

Zapsané ve zdrojích, ale na disku chybí: `sanskrit/gretil`, `islam/openiti`,
`sumer/cdli`, Korán (`islam/quran` je prázdný), `sumer/` celý.

**Rozpracováno mimo tento plán (2026-11-22, necommitnuto):** Rámájana
z GRETIL — sedm kánd jako TXT v `downloads/sanskrit/ramayana/gretil/`,
záznamy `sa.ram1`–`sa.ram7` v `works.yaml` (IAST, detektor `mbh_bare_number`,
`priority: 1`). Tamtéž jsou stažené, zatím bez záznamu v registru:
`downloads/bible/hebrew/morphhb/` (WLC), `downloads/greek_latin/first1k/`
a `downloads/european_philosophy/latin/`. Nedělej to znovu; než začneš,
zjisti stav (`git fetch`, `git log --all`) a navaž. Převodní skript
z HTML/TEI do TXT v repu zatím není — pokud existuje, patří do
`rag/sources/` nebo `bin/`.

**Úplně chybí:** hebrejská Bible, Korán, rabínská literatura, arabská a perská
filozofie a súfismus, tibetský buddhismus, mahájána a čchan, většina čínské
klasiky, upanišady a darśany, Mezopotámie, Japonsko, Afrika mimo Egypt,
Ameriky, Oceánie.

## 2. Je GRETIL dobrý zdroj?

Ano — pro sanskrt je to nejlepší volně dostupný souhrnný zdroj a má se stát
páteří indické části. Zrcadlo `INDOLOGY/GRETIL-mirror` (990 MB) obsahuje
**781 sanskrtských děl v TEI/XML** v adresáři
`gretil.sub.uni-goettingen.de/gretil/corpustei/` (soubory `sa_*.xml`).
Ověřeně tam jsou mimo jiné: `sa_Rgveda-edAufrecht`, `sa_rAmAyaNa`,
`sa_bhagavadgItA-comm`, `sa_pataJjali-yogasUtra`, `sa_bAdarAyaNa-brahmasUtra`,
`sa_nAgArjuna-mUlamadhyamakakArikA`, `sa_vasubandhu-abhidharmakoza`,
`sa_zAntideva-bodhicaryAvatAra`, `sa_saddharmapuNDarIkasUtra`,
`sa_manusmRti`, `sa_kauTilya-arthazAstra`, `sa_abhinavagupta-tantrAloka`,
`sa_bhAgavatapurANa`, `sa_svAtmArAma-haThayogapradIpikA`.

Na co si dát pozor:

1. **Uzavřený projekt.** Od července 2025 ho univerzita v Göttingenu vede
   jako uzavřený a přestěhovala ho do TextGridu; nové texty ani opravy
   nepřibývají. Zrcadlo na GitHubu je stabilní snímek.
2. **Nestejná kvalita.** Texty přepisovaly desítky lidí z různých edic,
   některé nejsou korigované. U každého díla zapiš edici z TEI hlavičky
   do `edition` v registru.
3. **Více variant jednoho díla** (`-alt`, `-comm`, `-comm2`). Kurátorsky
   vyber jednu jako kanonickou; komentář je samostatné dílo.
4. **Přepis IAST, ne dévanágarí.** Viz Rozhodnutí vlastníka č. 2.
5. **Mahábhárata v TEI není** — jen HTML v `gretil/1_sanskr/2_epic/mbh/`.
6. **Licence CC BY-NC-SA 4.0** je v TEI hlavičce (`<availability>`, ověřeno
   na vzorku): šířit lze, ale nekomerčně, s uvedením zdroje a pod stejnou
   licencí. U každého díla ji opiš do `edition` i se jmény přepisovatelů.
   Hlavička zároveň říká, že text byl dodán „v dobré víře" — u moderních
   kritických edic může být nárok vydavatele.
7. Zrcadlo je už v `git_wget_sources.txt`, ale na M2 naklonované není.

GRETIL obsahuje i pálí (171 souborů), tamilštinu (201), prákrty, hindštinu
a starou jávštinu — hodí se pro fázi 2.

## 3. Další zdroje — co na co použít

Sloupec „Stav": ✔ = ověřeno, že existuje a odpovídá; licence podle GitHub API
nebo dokumentace zdroje, jinak „ověřit".

### Indie, buddhismus

| Zdroj | Co dává | Formát | Stav / licence |
|---|---|---|---|
| `INDOLOGY/GRETIL-mirror` | 781 sanskrtských děl, pálí, tamilština | TEI, HTML | ✔ CC BY-NC-SA 4.0 |
| `sarit/SARIT-corpus` | śāstra, kvalitní kritické edice | TEI | ✔ 165 MB, ověřit |
| `OliverHellwig/sanskrit` (DCS) | lemmatizovaný sanskrt | CoNLL-U | ✔ 1,9 GB, ověřit |
| `suttacentral/bilara-data` | Tipitaka po segmentech s ID (`mn1:1.1`) | JSON | ✔ 974 MB, ověřit |
| `VipassanaTech/tipitaka-xml` | Tipitaka + **aṭṭhakathā, ṭīkā, Visuddhimagga** | XML | ✔ 798 MB, ověřit |
| dsbcproject.org | sanskrtský buddhistický kánon | HTML | ✔ web |
| `shabados/database` | Guru Granth Sáhib (gurmukhí) | JSON | ✔ MIT, archivováno |
| projectmadurai.org | tamilská klasika (Tirukkural, Téváram) | HTML/TXT | ✔ web |

### Čína, Tibet, Japonsko

| Zdroj | Co dává | Formát | Stav / licence |
|---|---|---|---|
| organizace `kanripo` | 9 355 textů, jeden repozitář = jedno dílo; klasika, mistři, Tao-cang | TXT (org-mode) | ✔ ID hledej v `kanripo/KR-Catalog` |
| `cbeta-org/xml-p5` | čínský buddhistický kánon (Taišó) | TEI P5 | ✔ 1,4 GB, nekomerční — ověřit |
| `chinese-poetry/chinese-poetry` | poezie + Čtyři knihy a Pět kanonických | JSON | ✔ MIT |
| `Esukhia/derge-kangyur`, `derge-tengyur` | tibetský kánon (Derge), unicode | TXT | ✔ 351 MB + 1,4 GB, ověřit |
| library.bdrc.io | tibetské e-texty mimo kánon | API | ✔ web |
| aozora.gr.jp | japonská klasika ve volném díle | TXT/HTML | ✔ web |
| 21dzk.l.u-tokyo.ac.jp (SAT) | Taišó sv. 56–84 (japonští autoři, Dógen) | jen web | ✔ web |

### Blízký východ a Středomoří

| Zdroj | Co dává | Formát | Stav / licence |
|---|---|---|---|
| `openscriptures/morphhb` | hebrejská Bible (WLC) s morfologií | OSIS XML | ✔ 80 MB |
| `ETCBC/dss` | svitky od Mrtvého moře | text-fabric | ✔ 205 MB |
| Sefaria (GCS bucket, index v `Sefaria/Sefaria-Export`) | Mišna, Talmud, midraše, Zohar, Maimonides, kabala, chasidut | JSON, ~26 GB | ✔ licence per text v metadatech |
| tanzil.net | Korán, uthmání text | TXT | ✔ CC BY, text se nesmí měnit |
| `OpenITI/RELEASE` | 10 000+ arabských a perských textů | mARkdown | ✔ 10,7 GB, ověřit |
| api.ganjoor.net | perská poezie: Rúmí, Háfiz, Sa'dí, Attár, Firdausí | JSON API | ✔ |
| `OpenGreekAndLatin/First1KGreek` | řečtina mimo Perseus (církevní otcové, komentátoři) | TEI/CTS | ✔ CC BY-SA 4.0 |
| `eliranwong/LXX-Rahlfs-1935` | Septuaginta | TXT | ✔ 192 MB, ověřit |
| `srophe/syriac-corpus` | syrština (Efrém, Afrahat) | TEI | ✔ 13 MB |
| `CopticScriptorium/corpora` | koptština (Šenute, Apofthegmata) | TEI | ✔ 2,7 GB |
| `BetaMasaheft/Works` | ge'ez (Henoch, Kebra nagast) | TEI | ✔ plné texty ověřit |
| avesta.org, TITUS | Avesta v transliteraci, stará perština | HTML | ✔ web, licence ověřit |

### Mezopotámie, Egypt

| Zdroj | Co dává | Formát | Stav / licence |
|---|---|---|---|
| ORACC, `oracc.museum.upenn.edu/json/<projekt>.zip` | `epsd2-literary` (sumerská literatura), `blms`, `cams-gkab`, `rinap`, `saao` | JSON | ✔ URL odpovídají |
| etcsl.orinst.ox.ac.uk | sumerská literatura, transliterace + překlad | XML | ✔ web |
| ebl.lmu.de/corpus | Enúma eliš, Gilgameš, Ludlul | API | ✔ web, licence ověřit |
| `cdli-gh/data` | 390 tis. tabulek, hlavně účetnictví | ATF | ✔ 6 GB, nízká priorita |
| Hugging Face `thesaurus-linguae-aegyptiae/*` | egyptština: starší, pozdní, démotická | věty | ✔ 3 sady; ověřit, zda nesou ID textu |

### Evropa

| Zdroj | Co dává | Formát | Stav / licence |
|---|---|---|---|
| deutschestextarchiv.de/download | Kant, Hegel, Fichte, Schelling, Schopenhauer, Nietzsche v prvních vydáních | TEI | ✔ web |
| korpora.org/kant | Kant, akademické vydání | HTML | ✔ web |
| corpusthomisticum.org | Tomáš Akvinský, celé dílo | HTML | ✔ web |
| mlat.uzh.ch (Corpus Corporum) | středověká latina, patristika | TEI | ✔ web |
| `OpenGreekAndLatin/csel-dev`, `patrologia_latina-dev` | latinští otcové | TEI (OCR) | ✔ kvalita kolísá |
| thelatinlibrary.com | Spinoza, Descartes, Boethius, Augustin | HTML | ✔ web |
| heimskringla.no | Edda a ságy ve staroseverštině | HTML | ✔ web |
| celt.ucc.ie | staroirské texty | HTML/TXT | ✔ web |

### Ameriky, Oceánie

| Zdroj | Co dává | Formát | Stav / licence |
|---|---|---|---|
| `Multepal/popolwuj-original` | Popol Vuh v k'iče' | TEI | ✔ MIT, archivováno |
| florentinecodex.getty.edu | Florentský kodex v nahuatlu | web | ✔ export ověřit |

Obecné agregátory: **Wikisource** (dumpy po jazycích — nejširší záloha pro
cokoli, co chybí jinde), **Internet Archive** (jen skeny; pro exotická písma
bez OCR nepoužitelné — tudy vznikla většina dnešních vyřazených PDF),
**sacred-texts.com** (skoro jen anglické překlady, pro tenhle projekt nevhodné).

## 4. Doporučené pořadí

1. **GRETIL** — jedním adaptérem přibude 781 děl a opraví se Mahábhárata,
   Rgvéd i Rámájana.
2. **Hebrejská Bible + Korán** — dvě základní písma lidstva chybí úplně
   a obě mají malý, čistý, strukturovaný zdroj.
3. **Čína: Kanripo + CBETA** — klasika a mahájána/čchan.
4. **First1KGreek** — stejná struktura jako Perseus, skoro bez kódu.
5. **Deutsches Textarchiv** — čistý Kant a Hegel místo frakturových PDF.
6. Sefaria, OpenITI (výběr), Ganjoor, tibetský kánon.
7. Mezopotámie, Egypt, koptština, syrština, Ameriky.

**Obohacení:** stávající korpus je od 2026-11-19 obohacený celý (164 344
chunků, kapitoly až na jednu). Udělala to výplň na L40S (`rag/l40s_filler.py`,
plán `PLAN-l40s-enrich.md` je zatím mimo `main`): kolem 120 chunků za minutu,
SPARK sám asi 7. Dokud sandbox L40S běží, obohacení hrdlem není; bez něj ano.
Výplň po dokončení skončila, pro nová díla ji musí někdo znovu pustit.

Nové tradice přesto přidávej kurátorsky po desítkách děl s `priority: 1`,
ne celé korpusy najednou: u nového jazyka nejdřív ověř kvalitu obohacení
na vzorku (`--benchmark`). Velké zdroje (781 textů GRETIL, 9 355 Kanripo)
zůstanou v klonu a do registru jde jen výběr.

## Fáze 0 — adaptéry zdrojů a připravenost registru

Dnes ingest umí TXT, PDF a Perseus TEI (`rag/perseus_tei.py`:
`scan_works()` + `extract()` → kapitoly s `ref`, `heading`, `text`).
Rámájana šla cestou „převést do TXT a kapitoly najít detektorem". Pro pár
děl to stačí; pro stovky strukturovaných souborů je levnější a přesnější
jít stejnou cestou jako Perseus, protože kapitoly a čísla veršů už ve
zdroji jsou a regex je jen znovu hádá.

1. Zobecni cestu Perseu na **adaptéry**: modul `rag/sources/<jméno>.py`
   se stejným rozhraním (`scan_works(root)`, `extract(file)`), volaný
   z `ingest_books.py` podle pole `source.adapter` v `works.yaml`.
   `perseus_tei` zůstane první adaptér, chování se nesmí změnit
   (`make ingest-stats` před a po musí dát stejné počty).
2. Do `rag/registry/validate.py` doplň jazyky (`bo`, `ja`, `cop`, `syc`,
   `gez`, `akk`, `sux`, `ta`, `pa`, `quc`, `nah`, `sga`, `cs` — podle fází)
   a nové skupiny.
3. **Skupina ≠ tradice.** `GROUP_ALIASES` v `rag/retrieval.py` dnes mapuje
   „buddhis" jen na `pali`. Jakmile přibude sanskrtský, čínský a tibetský
   buddhismus, musí dotaz na buddhismus mířit na všechny. Přidej do registru
   pole `tradition` a směruj podle něj; selftest v `retrieval.py` rozšiř.
4. Klony upstream rep patří mimo repo (proměnná `UPSTREAM_DIR`), do
   `downloads/` jde jen výběr. `git_wget_sources.txt` doplň o nové zdroje
   s připnutým commitem, ať je ingest reprodukovatelný.
5. Eval: ke každému novému dílu priority 1 přidej **před ingestem** 2–3
   dotazy do `rag/eval/golden_v3.jsonl`, ať je přínos i regrese měřitelná.

Hotovo, když: `make test` projde, Perseus dává stejná čísla, existuje
jeden nový adaptér s testem na fixtuře.

## Fáze 1 — nahradit 26 překladů originály

| Dnes (překlad) | Originál odkud |
|---|---|
| `sa.mbh01`–`18` (anglicky) | GRETIL, `1_sanskr/2_epic/mbh/` (HTML, kritická edice BORI) |
| `sa.rigveda_griffith` | GRETIL `sa_Rgveda-edAufrecht.xml` |
| Rámájana (vyřazené skeny) | GRETIL — **rozpracováno** (`sa.ram1`–`7`), jen dokončit |
| `zh.zhuangzi` (Giles) | Kanripo, ID najdi v `KR-Catalog` |
| `ae.avesta_darmesteter` | avesta.org nebo TITUS (ověřit licenci a strukturu) |
| `egy.pyramid_texts_allen` | TLA (ověřit, zda jde složit dílo, ne jen věty) |
| `ang.beowulf_*` | staroanglický text (Gutenberg #9701 nebo jiné vydání — ověřit) |
| `non.edda_*` | heimskringla.no (Eddukvæði, Snorra Edda) |

Postup u každého: adaptér → záznam v registru (`lang_corpus` = jazyk
originálu) → `make ingest-stats` → kontrola kapitol → ingest. Překlad se
vyřadí až poté, co originál projde evalem; smazání je schválené (viz
Rozhodnuto č. 3).

Nástroj na mazání díla zatím neexistuje: `load_pg.py` maže a nahrazuje jen
chunky a kapitoly děl, která jsou ve vstupu, a `embed_books.py --sync`
uklízí jen díla, která ve vstupu vidí. Dílo, které ze vstupu zmizelo,
zůstane v Postgresu i v Chromě. Napiš `--drop-work <id>` s `--dry-run`
(počty řádků v `works`, `chapters`, `chunks`, `chunk_enrichment` a počet
vektorů v obou kolekcích) a s testem.

Když originál nejde získat v použitelné kvalitě (hrozí u Avesty a Textů
pyramid), dílo zůstane v registru jako překlad s poznámkou, proč.

## Fáze 2 — Indie a buddhismus do hloubky

Z GRETIL TEI (stejný adaptér jako ve fázi 1), první vlna asi 40 děl:
hlavní upanišady, Bhagavadgíta, Jógasútra, Brahmasútra, Sánkhjakáriká,
Njájasútra, Vaišéšikasútra, Manusmrti, Arthašástra, Múlamadhjamakakáriká,
Abhidharmakóša, Bódhičarjávatára, Lankávatára, Lotosová sútra, Aštávakragíta,
Vivékačúdámani, Hathajógapradípiká, Šivasútra, Spandakáriká, Bhágavata
a Višnu purána, Gítagóvinda.

Pálí: přidej komentáře a Visuddhimaggu z `VipassanaTech/tipitaka-xml`.
Zvaž přechod kánonu z PDF na `suttacentral/bilara-data` — segmentová ID
dávají přesné citace pro `rag/verse_refs.py` (samostatný PR, ne nutný).

Pak tamilština (Tirukkural), Guru Granth Sáhib, džinistické prákrty.

## Fáze 3 — Čína, Tibet, Japonsko

- **Kanripo:** I-ťing, Š'-ťing, Šu-ťing, Li-ťi (s Velkým učením a Doktrínou
  středu), Cuo-čuan, Sün-c', Mo-c', Chan Fej-c', Lie-c', Sun-c', Chuaj-nan-c'.
  Stávající detektory `zh_zhang` / `zh_juan` v `rag/chapters.py` nejspíš
  poslouží; formát Kanripa je org-mode s `#+TITLE` a značkami stránek.
- **CBETA:** Sútra srdce (T0251), Diamantová (T0235), Lotosová (T0262),
  Vimalakírti (T0475), Tribunová sútra (T2008), Lin-ťi lu (T1985),
  Wu-men kuan (T2005), Pi-jen lu (T2003), Sin-sin ming (T2010)
  a ágamy (T0001, T0026, T0099, T0125) jako protějšek pálijských nikájí.
- **Tibet:** Derge Kangjur z Esukhie, výběrem (Pradžňápáramitá, vinaja);
  mimo kánon Bardo thödol, Milaräpa, Congkhapa přes BDRC.
- **Japonsko:** Aozora (Kodžiki, Hódžóki, Curezuregusa, Hagakure).

## Fáze 4 — Bible, judaismus, islám, Persie

- Hebrejská Bible z `morphhb` (kniha → kapitola → verš); stará PDF vyřadit
  z LFS. Septuaginta jako samostatné dílo. NZ už je z Perseu.
- Korán z Tanzilu: `https://tanzil.net/pub/download/index.php?quranType=uthmani&outType=txt-2&agree=true`
  (ověřeno, 1,4 MB). 114 súr = 114 kapitol. Text neupravovat (licence),
  takže `clean_text` pro něj vypnout a uvést zdroj.
- Sefaria: Mišna, Pirkej avot, výběr z Talmudu, Midraš raba, Sefer jecira,
  Zohar, Maimonidův Průvodce a Mišne Tora, Kuzari, Tanja. Stahuj jednotlivé
  texty podle `books.json`, ne celý bucket; filtruj podle licence.
- OpenITI: nenaklánět do LFS. Výběr podle URI: Ghazzálí (Ihjá),
  Ibn Arabí (Fusús), Ibn Síná, Fárábí, Ibn Rušd, Ibn Chaldún (Muqaddima),
  Buchárí, Nahdž al-balágha.
- Ganjoor: Rúmí (Masnaví), Háfiz, Sa'dí, Attár, Chajjám, Firdausí.
- Syrština, koptština, ge'ez — malé zdroje, jeden adaptér TEI.

## Fáze 5 — Mezopotámie a Egypt

ORACC JSON (`epsd2-literary` = sumerská literatura, `blms`, `cams-gkab`)
a eBL pro Enúma eliš a Gilgameše. CDLI až nakonec a jen žánr „Literary".
Egypt z TLA. Transliterace klínopisu a egyptštiny jsou mimo jazyky, na
kterých se učil embedding model — viz Rizika.

## Fáze 6 — Evropa

- First1KGreek přes stávající `perseus_tei` (stejná struktura `__cts__.xml`);
  pohlídat duplicity s Perseem podle URN.
- Kant a Hegel z Deutsches Textarchiv v TEI; frakturová PDF vyřadit.
- Tomáš Akvinský (Corpus Thomisticum), Augustin a Boethius (Corpus
  Corporum), Spinoza a Descartes latinsky.
- Staroseverské ságy, staroirské texty.
- **České originály** (schváleno, Rozhodnuto č. 4): Komenský (Labyrint
  světa a ráj srdce, Kšaft; latinsky Didactica magna), Hus (česky Postila,
  Dcerka; latinsky De ecclesia), Chelčický (Síť víry), Štítný. Zdroje:
  česká Wikisource (stránky autorů ověřeny, dump na dumps.wikimedia.org),
  ediční modul Vokabuláře webového (starší čeština; licenci edic ověřit),
  e-knihy Městské knihovny v Praze. Z novějších jsou volní Masaryk
  a Klíma; Patočka je chráněný do konce roku 2047.
- **Heidegger, Sein und Zeit** je v `urls.txt` (sekce 20), ale autor zemřel
  1976 a dílo je chráněné do konce roku 2046. Do veřejného repa ani LFS
  nepatří; záznam z `urls.txt` odstranit.

## Fáze 7 — Ameriky, Afrika, Oceánie

Popol Vuh v k'iče' (`Multepal/popolwuj-original`), Florentský kodex
v nahuatlu, Kebra nagast a Henoch v ge'ezu. Mayské kodexy v `downloads/maya`
jsou obrázky glyfů a pro RAG nemají cenu — vyřazení z LFS (152 MB) je
schválené (Rozhodnuto č. 5).
Pro jorubské Ifá, kečuánský rukopis z Huarochirí, havajské Kumulipo
a maorské texty spolehlivý strojový zdroj ověřený není; zapiš jako otevřené.

## Cesta díla až do Kindlify

1. zdroj v `UPSTREAM_DIR`, výběr v `downloads/<skupina>/`
2. záznam v `rag/registry/works.yaml` (`name_cs`, `author`, `lang_original`,
   `lang_corpus`, `form`, `period`, `edition`, `priority`, `aliases`,
   `tradition`, `source`), pak `python3 rag/registry/validate.py`
3. `make ingest-stats` (kontrola počtu kapitol a mediánu chunku), `make ingest`
4. `make sync-spark`, na SPARKu `make load-pg`, `make embed`, `make embed-gloss`
5. obohacení (`enrich_chunks`, `enrich_chapters`, `enrich_works`) v okně
   Directoru na SPARKu, nebo výplní na L40S
6. `make eval` proti baseline, `make kindlify-sync`

Kindlify sync vynechává překlady a jednokapitolová díla, takže dílo bez
funkčního dělení na kapitoly se do čtečky nedostane.

## Rizika

- **Embedding mimo naučené jazyky.** `multilingual-e5-large` stojí na
  XLM-R; ten zná sanskrt, hebrejštinu, arabštinu, perštinu, čínštinu,
  japonštinu a tamilštinu, ale ne tibetštinu, pálí, koptštinu, syrštinu,
  ge'ez, akkadštinu, sumerštinu ani egyptštinu. U nich ponese retrieval
  česká glosa (`books_gloss`) a fulltext. Změř to evalem po jazycích,
  je to přesně otázka, kvůli které projekt vznikl.
- **IAST vs. dévanágarí.** XLM-R viděl sanskrt hlavně v dévanágarí;
  kvalita vektorů nad IAST je neznámá. Porovnej obě varianty na pár dílech,
  než zaindexuješ všech 781.
- **Obohacení bez L40S.** Na samotném SPARKu jde asi 7 chunků za minutu.
  Sanskrtská Mahábhárata je řádově 10× větší než cokoli priority 1 — dej jí
  `priority: 2` a počítej s ní, jen dokud běží výplň na L40S.
- **Duplicity.** Totéž dílo z více zdrojů (Dhammapada pálijsky i v GRETIL,
  NZ v Perseu i jinde). Jedno dílo = jeden záznam, ostatní edice vynechat.
- **Licence.** Řada rep nemá soubor LICENSE; před commitem dat ověř v README.

## Rozhodnuto (Olin, 2026-11-22)

1. **Co smí do LFS:** malé normalizované texty vybraných děl ano (jako
   Rámájana, jednotky MB), celé klony zdrojů ne — ty si stroj stáhne podle
   připnutého commitu. Důvod: kvóta LFS a to, že GRETIL, CBETA i OpenITI
   jsou jen pro nekomerční užití se závazkem stejné licence.
2. **Sanskrt se ukládá v IAST** (věrně zdroji, jako už Rámájana); pro čtečku
   se dá převádět deterministicky. Otevřené zůstává jen, v čem embedovat —
   viz Rizika; to rozhodne měření.
3. **Smazat 26 překladů z Postgresu a Chromy — ano, vždy až po nahrazení
   originálem.** Týká se právě těchto děl: `sa.mbh01`–`sa.mbh18`,
   `sa.rigveda_griffith`, `zh.zhuangzi`, `ae.avesta_darmesteter`,
   `egy.pyramid_texts_allen`, `ang.beowulf_gummere`, `ang.beowulf_hall`,
   `non.edda_poetic`, `non.edda_prose`. Podmínky: originál je nahraný
   a prošel evalem; obohacení mazaného díla je předem uložené do souboru
   v `run/`; dry-run ukázal počty; výsledek se nahlásí. Kde originál získat
   nejde, překlad zůstává a hlásí se to.
4. **České originály do knihovny patří** (Komenský, Hus, Chelčický a další).
   Zásada „žádné české překlady" míří na překlady, ne na česky psaná díla.
5. **Nepoužitelná PDF z LFS pryč:** schváleno pro hebrejskou Bibli (719 MB,
   až bude nahraná z `morphhb`) a mayské kodexy (152 MB, hned). Ostatní
   vyřazená PDF (BORI Mahábhárata, skeny Rámájany, Kant, Avesta Geldner)
   jsou stejný případ, ale jmenovaná nebyla — před smazáním se zeptej.
   Technicky jde o `git rm` a úklid `urls.txt` a `EXCLUDE_PATH_MARKERS`;
   historie se nepřepisuje, takže se zmenší nové klony, ne úložiště LFS.
