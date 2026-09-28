# Knihovna šablon smluv — jak je psát a čím se hlídají

Stav k 2026-09-28. Plán: `LAWYER_TEMPLATES_PLAN.md` (mimo repo, vkládá ho Ol1n
do chatu). Předchůdce: `docs/lawyer/CURRENT_STATE.md` — právní index, ze kterého
šablony berou obsah.

## Kde co je

| Co | Kde |
|---|---|
| Šablony (data) | `data/templates/{typ}.yaml` |
| Evidence zdrojů a licencí | `data/templates/sources.yaml` |
| Schéma šablony | `rag/docgen/schema.json` |
| Stroj (načtení, výrazy, render) | `rag/docgen/templates.py` — `python3 -m docgen.templates` je selftest |
| Validace | `rag/docgen/validate.py` — `make validate-templates` |
| CLI renderu a otázek | `rag/docgen/render.py` — `make render-doc TYP=… VSTUP=…` |
| Fixtury a snapshoty | `rag/docgen/fixtures/`, `rag/docgen/fixtures/snapshots/` |
| Testy | `rag/tests/test_docgen.py` (v `make test`) |

## Pravidlo, na kterém to celé stojí

**Klauzule bez § neexistuje.** Každá klauzule má `zaklad` a `make
validate-templates` ověří proti právnímu indexu (Postgres `law`, 53 předpisů
vrstvy 1), že ten § v účinném znění skutečně je. Dnes je takto ověřených
**92 odkazů na zákon** ve třech šablonách. Test `test_kazda_klauzule_ma_pravni_zaklad`
neprojde, když klauzule oporu nemá.

Obsah šablon **nevzniká z cizích vzorů.** Důvod je v autorském zákoně, který máme
zaindexovaný: podle § 2 odst. 6 zák. č. 121/2000 Sb. není dílem „námět sám o sobě,
… myšlenka, postup, princip, metoda", takže struktura a náležitosti smluvního typu
chráněné nejsou — chráněné je konkrétní vyjádření. Proto se klauzule píšou z textu
zákona vlastními slovy a `sources.yaml` drží, co bylo prověřeno a zamítnuto
(Frank Bold bez licence, dTest za anti-bot ochranou, zakonyprolidi zakázané).

## Formát šablony

```yaml
typ: najemni_smlouva_byt          # = jméno souboru
nazev: Nájemní smlouva (byt)
verze: 1                          # zvýšit při každé věcné změně textu
jazyk: cs
druh: smlouva | jednostranny_dokument
forma: {pisemna: true, zaklad: {zakon: "89/2012 Sb.", par: "§ 2237"}}
pravni_zaklad: [{zakon: "89/2012 Sb.", paragrafy: "§ 2235 a násl."}]
strany:   [{role: pronajimatel, nazev: Pronajímatel, typ: [fyzicka, pravnicka]}, …]
promenne: [{id: najemne, typ: money, povinna: true, otazka: "Měsíční nájemné v Kč?",
            napoveda: "…", zaklad: {zakon: "89/2012 Sb.", par: "§ 2246"}}, …]
kontroly: [{vyraz: "jistota + smluvni_pokuta <= 3 * najemne",
            zprava: "…", zaklad: {zakon: "89/2012 Sb.", par: "§ 2254"}}]
klauzule: [{id: jistota, nazev: Jistota, povinna: false, podminka: "jistota != null",
            zaklad: [{zakon: "89/2012 Sb.", par: "§ 2254"}], text: "… {{ jistota|kc }} …"}, …]
checklist:   [{bod: "…", zaklad: {…}}]
upozorneni:  [{text: "…", zaklad: {…}}]
```

Detaily, které se snadno přehlédnou:

- **Strany** se v dokumentu vykreslí z konvence `<role>_jmeno`,
  `<role>_identifikace`, `<role>_adresa` — tyhle proměnné musí šablona
  deklarovat, validátor to hlídá. U jednostranného dokumentu se k stranám
  dopíše „(odesílatel)" a „(adresát)"; `oznaceni` to přepíše (plná moc má
  „udílí plnou moc" / „přijímá plnou moc") a `podpisuje: true` přidá druhý
  podpis, když dokument přijímá i druhá strana.
- **Filtry** v textu: `{{ najemne|kc }}` → „16 500 Kč", `{{ den|datum }}` →
  „1. října 2026", `{{ vymera|cislo }}`. Bez filtru se vloží text.
- **`kontroly`** jsou strojové: výraz nad proměnnými, chybějící číslo je nula,
  takže limit `jistota + smluvni_pokuta <= 3 * najemne` projde i bez jistoty.
  Se `zaklad` jde o kogentní limit zákona, bez něj o formální úplnost.
  Vyhodnocuje je vlastní evaluátor nad AST — žádný `eval()`, žádné volání funkcí
  mimo `min/max/abs`.
- **`varianty`** místo copy-paste klauzulí: `{podle: doba, hodnoty: {urcita: "…",
  neurcita: "…"}}`; `podle` musí být proměnná typu `enum` a varianty musí pokrýt
  všechny její hodnoty.
- **`upozorneni`** nahradilo `zakazane_klauzule` s regexy z plánu: šablonu píšeme
  my, takže zakázanou klauzuli v ní prostě nemáme. Upozornění je pro člověka při
  revizi a pro agenta, když si uživatel vyžádá vlastní formulaci.

## Recept na další šablonu

1. **Vytáhni paragrafy z indexu** a přečti si je celé, ne jen nadpisy:
   ```bash
   cd rag && .venv/bin/python3 -c "
   import psycopg
   dsn=[l.split('=',1)[1].strip() for l in open('.env') if l.startswith('LAW_PG_DSN')][0]
   with psycopg.connect(dsn) as c, c.cursor() as cur:
       cur.execute('''select string_agg(k.text, chr(10) order by k.seq_in_chapter)
                      from chapters ch join chunks k on k.chapter_id=ch.id
                      where ch.work_id='cz.sb.2012.89' and ch.ref='§ 2586' group by ch.id''')
       print(cur.fetchone()[0])"
   ```
   Nebo přes Právníka: `GET /search?q=…` na `law-chat` (port 8098).
2. **Napiš klauzule vlastními slovy.** Kde zákon formulaci diktuje (poučení
   o námitkách u výpovědi, výčet nezbytných služeb), drž se zákona.
3. **Kogentní limity dej do `kontroly`**, ne do komentáře — ať je z nich test.
4. **Fixtura + snapshot**: `rag/docgen/fixtures/{typ}__{pripad}.json` (meta klíče
   `_popis`, `_k_datu`, `_vypnute`), pak `make update-template-snapshots`.
   Minimálně dvě fixtury: plná a „povinné minimum".
5. `make validate-templates` a `make test`.
6. **Revize člověkem.** Tohle je místo, kde se rozhoduje kvalita — stroj ověří,
   že § existuje, ne že klauzule říká, co má.

## Stav první sady — hotová

Startovní sada z plánu je celá napsaná: 10 položek pokrývá **14 šablon**,
104 klauzulí, **362 odkazů na § ověřených proti indexu**, 24 fixtur se snapshoty.

| # | Položka plánu | Šablona (`data/templates/…`) | Klauzulí / otázek / kontrol |
|---|---|---|---|
| 1 | Nájemní smlouva — byt | `najemni_smlouva_byt` | 14 / 21 / 4 |
| 2 | Kupní smlouva — movitá věc (+ spotřebitel) | `kupni_smlouva_movita_vec` | 7 / 15 / 3 |
| 3 | Smlouva o dílo | `smlouva_o_dilo` | 9 / 16 / 3 |
| 4 | NDA (jednostranná / vzájemná) | `nda` | 9 / 12 / 2 |
| 5 | Pracovní smlouva, DPP, DPČ | `pracovni_smlouva`, `dohoda_o_provedeni_prace`, `dohoda_o_pracovni_cinnosti` | 9 / 18 / 6 · 7 / 15 / 3 · 7 / 15 / 4 |
| 6 | Výpověď z pracovního poměru | `vypoved_z_pracovniho_pomeru` | 4 / 12 / 2 |
| 7 | Plná moc (obecná / speciální) | `plna_moc` | 4 / 12 / 1 |
| 8 | Smlouva o poskytování služeb (IT) | `smlouva_o_poskytovani_sluzeb` | 10 / 17 / 4 |
| 9 | Licenční smlouva k software | `licencni_smlouva_software` | 8 / 17 / 3 |
| 10 | Odstoupení, reklamace, předžalobní výzva | `odstoupeni_od_smlouvy_na_dalku`, `reklamace`, `predzalobni_vyzva` | 7 / 14 / 1 · 4 / 15 / 2 · 5 / 14 / 2 |

Co zbývá: **revize člověkem** (stroj ověřil existenci §, ne že klauzule říká, co má),
export do .docx/.pdf a API pro agenta.

## Co plán říkal a co z toho vyšlo jinak

- **§1–§2 (fetcher cizích vzorů, LLM extrakce struktury): vynecháno.** Licence to
  nedovolují (Frank Bold neuvádí žádnou, dTest je za Anubisem) a hlavně to není
  potřeba — náležitosti stojí v zákoně, který máme. `sources.yaml` drží evidenci
  místo adresáře `raw/`.
- **Go → Python.** Šablony bydlí vedle právního RAG (rozhodnutí 2026-09-28), takže
  validace § jde přímo do Postgresu bez HTTP a všechno jede v jednom venv. `GET
  /law/paragraph` z plánu RAG tím pádem zatím není potřeba.
- **`zakazane_klauzule` regexy → `upozorneni`.** Viz výše.
- **§6 export do .docx/.pdf: zatím ne.** Render dává Markdown; `pandoc` na M2
  není nainstalovaný a volba knihovny (unidoc/gooxml/python-docx) je rozhodnutí,
  které stojí za samostatný krok. Zápatí s verzí šablony, datem a upozorněním
  („Nejde o právní službu…") už v dokumentu je, podpisová pole taky.
- **Akceptační API `POST /docgen/render`** taky ne — API si definuje agent
  z `LAWYER_AGENT_PLAN.md`; dnes je vstupem `docgen/render.py` a JSON s odpověďmi.

## Co se přitom ověřilo v zákoně (a vyvrátilo předpoklady)

1. **Smluvní pokuta u nájmu bytu zakázaná není.** Plán ji měl v příkladu
   `zakazane_klauzule` jako zakázanou s odkazem na § 2239. Účinné znění § 2239
   ale mluví o „zjevně nepřiměřené povinnosti" a § 2254 smluvní pokutu výslovně
   dovoluje — jen **spolu s jistotou** nesmí přesáhnout trojnásobek měsíčního
   nájemného. V šabloně je to `kontroly[0]` a test to hlídá na hraně 3×.
2. **Na dohody se nepoužijí ustanovení o odměňování** (§ 77 odst. 2 písm. e) ZP),
   takže splatnost odměny z DPP se **sjednává** (§ 138) — spoléhat na pravidla
   o splatnosti mzdy by byla chyba.
3. **Minimální mzda na odměnu z dohody dopadá** — § 111 odst. 1 to říká výslovně
   („Mzda nebo odměna z dohody nesmí být nižší než minimální mzda“), protože
   dohody jsou základní pracovněprávní vztah podle § 3 ZP. Konkrétní částku ale
   stanoví nařízení vlády, které v indexu není → checklist žádá ruční kontrolu.
4. **Zkušební doba je dnes 4 měsíce, ne 3** (8 u vedoucího místo 6) — § 35 odst. 2
   v účinném znění. Starší vzory a články na webu mají staré číslo; šablona má
   z obojího strojovou kontrolu.
5. **Limit u DPČ není počet hodin za rok, ale průměr** — nejvýše polovina stanovené
   týdenní pracovní doby posuzovaná za celou dobu dohody, nejdéle za 52 týdnů
   (§ 76 odst. 2 a 3). Roční limit 300 hodin má jen DPP.
6. **Licenci nelze poskytnout ke způsobům užití, které dnes nejsou známé** — § 2372
   odst. 1 říká, že k opačnému ujednání se nepřihlíží. Formulace „všemi způsoby,
   včetně budoucích", kterou vzory rády mají, tedy nefunguje. A § 2370 dává licenci
   na dobu neurčitou roční výpovědní účinnost — kdo chce kratší, musí si ji ujednat.
7. **Předžalobní výzva je podmínka, ne zdvořilost** — bez výzvy zaslané alespoň
   7 dnů před podáním návrhu nemusí soud úspěšnému žalobci přiznat náhradu nákladů
   řízení (§ 142a OSŘ). Šablona to hlídá kontrolou `lhuta_dni >= 7`.
8. **Dílo na zakázku není zaměstnanecké dílo.** Bez licenční klauzule nemá klient
   právo vzniklý kód užít — zaplacení samo majetková práva nepřevádí (§ 58 AZ
   dopadá jen na zaměstnance). Proto má smlouva o poskytování služeb volitelnou
   klauzuli o licenci k výsledkům.
9. **Odměna z příkazu přísluší, i když výsledek nenastal** (§ 2438 odst. 2) — kdo
   chce platit za výsledek, potřebuje smlouvu o dílo, ne smlouvu o službách.
10. **Chybějící předpisy v indexu se projevily hned**: rozúčtování služeb u nájmu
   je v zák. č. 67/2013 Sb. (v indexu není), minimální mzda v nařízení vlády,
   dovolená u dohod se v ZP nedala dohledat na jednom místě. Všechno je v
   checklistech jako bod bez §, ať se na to nezapomene — a je to argument pro
   rozšíření indexu (viz „chybějící data" v `CURRENT_STATE.md`).
