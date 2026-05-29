# gphgData

This repo extracts historic and current watch data from https://www.gphg.org/en and enriches the raw rows into normalized `_NEW` columns.

## Tools

- **SuperScraper.py** scrapes archive years and optional current-year participants pages, then writes pipe-delimited raw data.
- **SuperScraperMT.py** is the multithreaded scraper. It keeps the same output contract as `SuperScraper.py`, but fetches independent watch detail pages in parallel.
- **SuperEnrich.py** enriches raw rows with local Ollama models by default, with optional Gemini and DeepL workflows.
- **gphg_header_master.json** defines the stable scraper spec-column contract used by both scrapers.

## Repository Contents

- `SuperScraper.py`: sequential scraper and compatibility baseline.
- `SuperScraperMT.py`: multithreaded scraper for normal full-history extraction.
- `SuperEnrich.py`: enrichment and translation pipeline.
- `gphg_header_master.json`: committed master list of raw specification fields.
- `README.md`: usage notes, conventions, and test results.

Generated scrape outputs, enrichment outputs, logs, failure CSVs, regression artifacts, and exploratory versioned scripts are intentionally not tracked in this repo unless they are promoted as release artifacts.

## Quick Start

```bash
# Scrape all archive years
python SuperScraper.py -e

# Faster full scrape using the multithreaded scraper
python SuperScraperMT.py -e --workers 4 --rate-limit 0.05

# Scrape one archive year
python SuperScraperMT.py -y 2001 -e --workers 4 --rate-limit 0.05

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

## Local Model Testing Notes

The enrichment workflow was tested with Gemma and DeepSeek R1 through Ollama on an Apple Silicon machine with 64 GB RAM.

- `gemma3:27b` is the current recommended local model for full-row enrichment. It produced the best balance of extraction quality, JSON compliance, and practical runtime in the 2001 tests.
- `gemma3:12b` is a useful fallback when memory or latency matters more than maximum extraction quality.
- `deepseek-r1:32b` was tested but is not currently recommended for this workflow. It was memory constrained on the test machine and showed higher extraction risk, including hallucinated structured values such as `SIZE_NEW: 42 mm` when the source text did not support that case size.

Because the goal is normalized factual extraction, prefer the model that is conservative with source evidence over the model that gives the most fluent answer.

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

The spec columns come from `gphg_header_master.json`. That committed static header keeps column order stable across sequential and multithreaded runs. The file is not regenerated during a normal scrape.

The current master header contains these 19 source specification fields:

```text
BRACELET STRAP
BUCKLE
CASE MATERIAL
CERTIFICATION
COLLECTION
DESCRIPTION
DIAL FINISH
FUNCTIONS
LAUNCH DATE
MATERIAL
MOVEMENT
NUMBER OF CARATS
PRICE EXCL. VAT
PRICE INCL. VAT
REFERENCE
SIZE
SUSTAINABILITY
THICKNESS
WATER RESISTANCE
```

New columns discovered during scraping are logged as new source fields. They are only added to the master header when `--update-header` is used, and the updated `gphg_header_master.json` should be reviewed and committed with the scraper change.

Some source labels are canonicalized before writing output: `MATERIAL` is rolled into `CASE MATERIAL`, and `REFERENCE` plus truncated labels such as `REFE`, `REFER`, and `REFEREN` are rolled into the fixed `Reference` output column.

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

## SuperScraperMT.py

`SuperScraperMT.py` is the multithreaded version of the scraper. It is intended for normal full-history extraction because the slowest part of scraping is fetching each independent watch detail page.

The script keeps archive discovery, year-page discovery, list-page discovery, output headers, `Language`, duplicate merge, `PrizeType` promotion, and atomic output writes compatible with `SuperScraper.py`.

### Why It Is Faster

GPHG list pages are small and sequentially define the scrape plan. The expensive work is fetching hundreds of individual watch detail pages per year. Those pages are independent, so `SuperScraperMT.py` uses a bounded `ThreadPoolExecutor` for detail pages only.

It is faster because it overlaps network wait time:

- each worker has its own thread-local `requests.Session`
- detail pages are fetched in parallel
- results are sorted back into the original list order before writing
- duplicate merging still happens after all rows are collected
- a shared cache prevents refetching the same detail URL when a watch appears as participant, nominee, and winner

Use a conservative worker count. The tested default is `6`, but `4` with a small rate limit is a polite and stable setting:

```bash
python SuperScraperMT.py -e --workers 4 --rate-limit 0.05
```

Use `--workers 1` if you want the multithreaded script to behave like a sequential detail-page fetcher for debugging.

### CLI

```text
usage: SuperScraperMT.py [-h] [-y YEAR] [-e] [-d] [-P]
                         [--participants-year PARTICIPANTS_YEAR]
                         [--participants-url PARTICIPANTS_URL]
                         [--update-header]
                         [--timeout TIMEOUT]
                         [--retries RETRIES]
                         [--workers WORKERS]
                         [--rate-limit RATE_LIMIT]

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
  --workers                  Parallel detail-page workers. Defaults to 6.
  --rate-limit               Optional seconds to sleep before each detail-page request per worker.
```

### Examples

```bash
# Full archive scrape with polite parallelism
python SuperScraperMT.py -e --workers 4 --rate-limit 0.05

# Single year
python SuperScraperMT.py -y 2025 -e --workers 4 --rate-limit 0.05

# Current-year participants page
python SuperScraperMT.py -e -P --participants-year 2026 --workers 4 --rate-limit 0.05
```

### Regression Test Results

`SuperScraperMT.py` was regression-tested against `SuperScraper.py` using full archive extraction for 2001-2025. Both runs used the same `gphg_header_master.json` and wrote isolated outputs.

Preserved comparison files from the test run:

```text
regression_outputs/v80_vs_v81_full_2001_2025/GPHG_Prize_v80_full_2001_2025.txt
regression_outputs/v80_vs_v81_full_2001_2025/GPHG_Prize_v81_full_2001_2025.txt
```

Coverage matched:

```text
Data rows: 5652 vs 5652
Lines including header: 5653 vs 5653
Columns: 23 vs 23
Headers: identical
Watch key set: identical
Language distribution: en=4183, fr=1469 in both
PrizeType distribution: P=4922, N=398, W=332 in both
```

Timing from the same regression run:

```text
SuperScraperMT.py: about 16m 05s
SuperScraper.py:   about 34m 17s
```

The multithreaded run was a little over twice as fast because it overlapped network-bound detail-page requests.

Content differences were limited to cleanup of bad page-title values:

```text
Rows with field differences: 128
FUNCTIONS differences: 115
CERTIFICATION differences: 13
Bad values ending in "| GPHG":
  SuperScraper.py:   128
  SuperScraperMT.py: 0
```

The removed values looked like `Brand, Model | GPHG`, for example `BVLGARI, Lvcea Notte Di Luce | GPHG`. These are page titles, not real watch specifications. `SuperScraperMT.py` filters them out instead of writing them into `FUNCTIONS` or `CERTIFICATION`.

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

1. Scrape with `SuperScraperMT.py` for faster full-history extraction, or `SuperScraper.py` for the sequential baseline.
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
- `PrizeType` records the strongest GPHG status found for a watch:
  - `P` = participant or competing watch
  - `S` = pre-selected watch
  - `N` = nominated watch
  - `W` = winning watch
- When the same watch appears in multiple sections, `PrizeType` is promoted in the order `P < S < N < W`.
- `Reference` comes from the source site when available. `Reference_NEW` can be enriched from the model text and technical sheet details.
- Technical sheet sections such as `Fiche Technique` are prioritized for reference, calibre, indications, frequency, power reserve, water resistance, case, dial, strap, and clasp details.
