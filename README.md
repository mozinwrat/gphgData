# gphgData

This repo extracts historic and current watch data from https://www.gphg.org/en and enriches the raw rows into normalized `_NEW` columns.

## Tools

- **SuperScraper.py** scrapes archive years and optional current-year participants pages, then writes pipe-delimited raw data.
- **SuperEnrich.py** enriches raw rows with local Ollama models by default, with optional Gemini and DeepL workflows.

## Quick Start

```bash
# Scrape all archive years
python SuperScraper.py -e

# Scrape one archive year
python SuperScraper.py -y 2001 -e

# Enrich older French rows with Ollama auto mode
env OLLAMA_MODEL=gemma3:27b python SuperEnrich.py \
  -I GPHG_Prize.txt \
  -o GPHG_Prize_enriched_auto.txt \
  -m ollama \
  --task auto \
  --years 2001-2007 \
  --output-target-only \
  --log-failures
```

## Installation

Python 3.10+ is recommended.

```bash
python -m pip install requests beautifulsoup4 google-generativeai
```

For local enrichment, install Ollama from https://ollama.com and pull a model:

```bash
ollama pull gemma3:27b
```

`gemma3:27b` gives stronger extraction quality on Apple Silicon with enough memory. `gemma3:12b` is a faster fallback.

## SuperScraper.py

The scraper discovers GPHG archive pages, follows participant, pre-selected, nominated, and winner links, and merges duplicate watch URLs using the promotion order `P < S < N < W`.

### Output

The output delimiter is `|`.

```text
Year | Language | Brand | Model | Reference | <master spec columns> | PrizeType | Category
```

`Language` is row metadata used by the enricher:

- `fr` for 2001-2007
- `en` for 2008 and later
- `unknown` for years outside the known mapping

The spec columns come from `gphg_header_master.json`. That static header keeps column order stable across runs. New columns discovered during scraping are logged; they are only added to the master header when `--update-header` is used.

### CLI

```text
usage: SuperScraper.py [-h] [-y YEAR] [-e] [-d] [-P]
                       [--participants-year PARTICIPANTS_YEAR]
                       [--participants-url PARTICIPANTS_URL]
                       [--update-header]
                       [--timeout TIMEOUT]
                       [--retries RETRIES]

options:
  -y, --year                 Scrape a single archive year. If omitted, scrape all archive years found.
  -e, --export               Write the pipe-delimited output file.
  -d, --debug                Verbose logging.
  -P                         Include the current-year participants page.
  --participants-year        Year for the participants page. Defaults to the configured current participant year.
  --participants-url         Explicit participants URL override.
  --update-header            Add newly discovered spec columns to gphg_header_master.json.
  --timeout                  HTTP timeout in seconds.
  --retries                  HTTP retry count.
```

### Examples

```bash
# Archive only
python SuperScraper.py -e

# Single year
python SuperScraper.py -y 2024 -e

# Current-year participants, even before the year appears in the archive
python SuperScraper.py -e -P --participants-year 2026

# Use an explicit participants URL
python SuperScraper.py -e -P --participants-url "https://gphg.org/en/gphg-2026/competing-watches"
```

## SuperEnrich.py

The enricher reads scraper output, adds audit columns, and populates normalized enrichment columns. It can translate older French descriptions and can also run extraction-only for English rows.

### Language and Task Selection

`--task auto` is the recommended mode. It detects language from the `DESCRIPTION` field row by row, then falls back to the row `Language` value or known year mapping when the description is not decisive.

- French descriptions use `translate-enrich`.
- English descriptions use `extract-only`.
- Unknown descriptions default to the safest task for the available row metadata.

This avoids a model call just to detect language.

### Output Columns

The output keeps all original input columns and adds:

- `DetectedLanguage`
- `LanguageDetectionScore`
- `EnrichmentTask`
- One `_NEW` column for each enrichable source field, excluding metadata like `PrizeType`, `Category`, and `Language`
- Additional extracted fields including `FREQUENCY_NEW` and `POWER RESERVE_NEW`

`DESCRIPTION_NEW` is English in translate mode. In extract-only mode, English source text is preserved and normalized into the `_NEW` fields.

### CLI

```text
usage: SuperEnrich.py [-h] -I INPUT -o OUTPUT
                      [-m {ollama,geminipro,deepl}]
                      [-k APIKEY]
                      [--task {auto,translate-enrich,extract-only}]
                      [--year YEAR]
                      [--years YEARS]
                      [--brand-contains BRAND_CONTAINS]
                      [--model-contains MODEL_CONTAINS]
                      [--limit LIMIT]
                      [--force]
                      [--output-target-only]
                      [--log-failures]
                      [--test]
                      [--debug]
                      [--ollama-timeout OLLAMA_TIMEOUT]
                      [--ollama-retries OLLAMA_RETRIES]
                      [--ollama-num-predict OLLAMA_NUM_PREDICT]
                      [--ollama-no-think]
                      [--deepl-key DEEPL_KEY]
                      [--deepl-url DEEPL_URL]
                      [--deepl-pro]
                      [--target-lang TARGET_LANG]
```

### Common Examples

```bash
# Auto-enrich 2001-2007, writing only rows that match the requested years
env OLLAMA_MODEL=gemma3:27b python SuperEnrich.py \
  -I GPHG_Prize.txt \
  -o GPHG_Prize_2001_2007_enriched.txt \
  -m ollama \
  --task auto \
  --years 2001-2007 \
  --output-target-only \
  --log-failures

# Target a single watch
env OLLAMA_MODEL=gemma3:27b python SuperEnrich.py \
  -I GPHG_Prize_2001.txt \
  -o vacheron_regulateur_2001.txt \
  -m ollama \
  --task auto \
  --year 2001 \
  --brand-contains "Vacheron Constantin" \
  --model-contains "Régulateur Dual Time" \
  --force \
  --output-target-only \
  --log-failures

# Extract only from already-English 2024 rows
env OLLAMA_MODEL=gemma3:12b python SuperEnrich.py \
  -I GPHG_Prize_2024.txt \
  -o GPHG_Prize_2024_enriched_sample.txt \
  -m ollama \
  --task extract-only \
  --year 2024 \
  --limit 2 \
  --output-target-only
```

## Workflow

1. Scrape with `SuperScraper.py`.
2. Enrich with `SuperEnrich.py --task auto`.
3. Review `<output>.fail.csv` if `--log-failures` was enabled.
4. Rerun targeted rows with `--year`, `--brand-contains`, and `--model-contains` when a row needs focused cleanup.

## Troubleshooting

- **Unexpected `years=ALL` in logs**: use `--year` or `--years` when you want the log and output scope to show a specific year filter.
- **Output has more rows than expected**: add `--output-target-only`; otherwise non-target rows are carried through unchanged.
- **Malformed model JSON**: use `--debug` to capture raw model output. The script attempts JSON extraction and repair before marking a row failed.
- **Ollama timeouts**: increase `--ollama-timeout`, lower `--ollama-num-predict`, or use `gemma3:12b`.
- **New scraper fields are missing from output**: run once with `--update-header` after confirming the new source column should become part of the stable contract.

## Conventions

- Output delimiter is `|`.
- `PrizeType` promotion is `P < S < N < W`.
- `Reference` comes from the source site when available. `Reference_NEW` can be enriched from the model text and technical sheet details.
- Technical sheet sections such as `Fiche Technique` are prioritized for reference, calibre, indications, frequency, power reserve, water resistance, case, dial, strap, and clasp details.
