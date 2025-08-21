# gphgData

This repo extracts historic and current data from https://www.gphg.org/en and then uses local LLM models to enrich missing specifications.

---

## Tools

- **SuperScraper.py** — scrapes GPHG archive years (2001…latest) and, optionally, the **2025 “Competing watches”** page. Produces a pipe‑delimited master file.
- **SuperEnrich.py** — LLM‑powered enrichment (translation + structured extraction into `_NEW` columns) with retries, a progress bar, and failure sidecars.

> Note: The scripts previously lived as `SuperScraper_v79.py` and `SuperEnrich_v84.py`. The README now refers to them as `SuperScraper.py` and `SuperEnrich.py`. If your working directory still has the old filenames, either rename them or update your commands accordingly.

---

## Quick start

```bash
# 1) Extract (history + 2025 participants) and write master file
python SuperScraper.py -e -P

# 2) Enrich the extracted file with a local LLM (via Ollama)
python SuperEnrich.py -I GPHG_Prize.txt -o enriched.txt -m ollama --log-failures
```

---

## Installation

Python 3.10+ recommended.

```bash
python -m pip install requests beautifulsoup4 tqdm google-generativeai
```

**Ollama (local LLM runtime)**  
Install from https://ollama.com and pull at least one model:

```bash
ollama pull gemma3:12b
# optional:
ollama pull llama3.2:latest
```

Apple Silicon (M‑series) usually uses the GPU automatically.

---

## 1) SuperScraper.py — Extraction

### CLI

```
usage: SuperScraper.py [-h] [-y YEAR] [-e] [-d] [-P]

options:
  -y, --year YEAR   Scrape a single year (e.g., 2009). If omitted, scrapes all archive years it can find.
  -e, --export      Write the TXT output (pipe‑delimited). If omitted, just fetch/merge in memory.
  -d, --debug       Verbose logging.
  -P                Also fetch the 2025 “Competing watches” page.
```

### What it does

- Finds GPHG year pages and discovers links for:
  - **P** participants/competing, **S** pre‑selected, **N** nominated, **W** winners.
- Visits each watch page and collects **Brand**, **Model**, **Reference** (if present), and all visible **specs**.
- Canonicalises common label variants (e.g., `MATERIAL` → `CASE MATERIAL`).
- **Merges duplicates by URL** (PrizeType promotion order `P < S < N < W`, fill `Category` when it appears).
- Builds a header from this run:
  ```
  Year | Brand | Model | Reference | <sorted spec keys seen this run> | PrizeType | Category
  ```
- Writes `GPHG_Prize.txt` (or `GPHG_Prize_<year>.txt` when `-y` is set).
- Scrubs literal tab characters, logs progress like `P 23/230 |`, and prints `Completed year YYYY` once a year finishes.

### 2025 participants (-P)

Adds: `https://gphg.org/en/gphg-2025/competing-watches`  
Rows from this page are appended in the same run so their fields participate in the header.

### Examples

```bash
# Single year
python SuperScraper.py -y 2009 -e

# All years (archive only)
python SuperScraper.py -e

# All years + 2025 participants
python SuperScraper.py -e -P

# Debug
python SuperScraper.py -e -d -P
```

---

## 2) SuperEnrich.py — Enrichment

### CLI

```
usage: SuperEnrich.py [-h] -I INPUT -o OUTPUT [-m {geminipro,ollama}] [-k APIKEY]
                      [--force] [--log-failures] [--test] [--debug]

options:
  -I, --input         Scraper output (e.g., GPHG_Prize.txt)
  -o, --output        Enriched file to write (e.g., enriched.txt)
  -m, --model         "ollama" (local default) or "geminipro"
  -k, --apikey        API key, only required for --model geminipro
  --force             Enrich all rows (ignore default filters and existing *_NEW)
  --log-failures      Write <output>.fail.csv with Year|Brand|Model|Error
  --test              Process one row then exit
  --debug             Verbose logs and prompt/response dumps to a log file
```

### What it does

- Reads the input header and builds enrichment targets dynamically:
  - `PROMPT_KEYS` = all input columns except `PrizeType` and `Category`.
  - For each key `k`, a new column `k_NEW` is created in the output.
- The prompt asks the LLM to:
  - Translate **COLLECTION** and **DESCRIPTION** to English (keep full text),
  - Extract structured fields (e.g., **BRACELET STRAP**, **CASE MATERIAL**, **SIZE**, **MOVEMENT**, **WATER RESISTANCE**, etc.),
  - Emit **JSON only**.
- Writes the original columns + all `_NEW` columns.
- Shows a **tqdm** progress bar and logs to `SuperEnrich_YYYYMMDD_HHMMSS.log`.
- If `--log-failures` is used, appends a concise `<output>.fail.csv`.

### Models & tips

- **Local (recommended):** `gemma3:12b` in Ollama is the most stable for strict JSON output in this workflow.
- **Alternative:** `llama3.2:latest` is faster but may produce malformed JSON more often.
- To reduce first‑call latency, “warm” the model:
  ```bash
  ollama run gemma3:12b
  ```
  then `Ctrl+C`.

### Examples

```bash
# Gemma via Ollama
python SuperEnrich.py -I GPHG_Prize.txt -o enriched.txt -m ollama --log-failures

# Gemini (cloud)
export GEMINI_API_KEY=…
python SuperEnrich.py -I GPHG_Prize.txt -o enriched_gemini.txt -m geminipro -k "$GEMINI_API_KEY" --log-failures

# Test one row with debug
python SuperEnrich.py -I GPHG_Prize.txt -o enriched.txt -m ollama --test --debug
```

---

## Workflow

1. **Extract** with `SuperScraper.py` (add `-P` if you want 2025 participants included).
2. **Enrich** with `SuperEnrich.py` and inspect any `<output>.fail.csv`.
3. Iterate on model choice or prompt if you see malformed JSON.

---

## Troubleshooting

- **Header looks “short”**  
  The scraper’s header is the union of fields seen **in this run**. To maximise coverage, scrape all years and add `-P`.

- **Malformed JSON / “No JSON object found”**  
  Prefer `gemma3:12b` locally. Use `--debug` to capture the raw model output that failed.

- **Timeouts to Ollama**  
  The client retries automatically. Ensure Ollama is up to date and the model is pulled.

---

## Conventions

- Output delimiter is the pipe `|`.
- `PrizeType` promotion: `P < S < N < W`.
- `Reference` in the scraper reflects the site; enrichment may add `Reference_NEW`.

---

Happy scraping and enriching!
