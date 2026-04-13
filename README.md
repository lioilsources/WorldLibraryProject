# Filozofický Dataset – Download Pipeline

Stahování a příprava multijazyčného filozofického korpusu na Mac Mini M2.

## Struktura

```
.
├── urls.txt              ← tvůj seznam URL (sem patří)
├── run_pipeline.sh       ← hlavní vstupní bod
└── downloader/
    └── main.go           ← Go orchestrátor (parser + aria2c runner)
```

## Prerekvizity

```bash
brew install aria2 go
```

## Rychlý start

```bash
# 1. Zkontroluj co se bude stahovat
./run_pipeline.sh --dry-run

# 2. Zobraz parsované záznamy a tradice
go run downloader/main.go -input urls.txt --list

# 3. Spusť stahování (přes noc)
./run_pipeline.sh

# 4. Přeskoč stahování, jen extrahuj ZIPy
./run_pipeline.sh --skip-download
```

## Parametry

| Parametr | Výchozí | Popis |
|---|---|---|
| `--base=/cesta` | `/Volumes/ancient_origins_1TB` | Kořenový adresář |
| `--urls=soubor` | `urls.txt` | Vstupní soubor |
| `-j4` | `4` | Paralelní stahování |
| `--dry-run` | false | Jen vypiš, nestahuj |
| `--skip-download` | false | Přeskoč stahování |

## Formát urls.txt

Parser rozumí tomuto formátu (tvůj stávající formát):

```
# Komentář – ignorováno
https://example.com/soubor.pdf
  dir=/ancient_origins_1TB/tradice/podadresar
  out=nazev_souboru.pdf
```

- `dir=` může být absolutní nebo relativní vůči `--base`
- `out=` je volitelné – pokud chybí, odvodí se z URL
- Prázdný řádek nebo `#` komentář oddělují záznamy

## Chování při chybách

- Soubor který již existuje je přeskočen (resume-friendly)
- Jeden selhavší soubor nepřeruší zbytek
- aria2c automaticky resumuje přerušená stahování (`--continue=true`)
- Max 5 pokusů na soubor s 5s pauzou mezi nimi

## Po stažení

Přesuň `dataset.jsonl` (výstup Go Q&A generátoru) na DGX Spark:

```bash
rsync -avz --progress data/final/dataset.jsonl spark:/home/user/finetune/
```

Nebo přes Tailscale pokud Spark není ve stejné síti.
