#!/usr/bin/env python3
"""
SuperEnrich_v84
º Dynamic header:  OUTPUT_KEYS / PROMPT_KEYS are built from the *input*
  file’s header instead of being hard-coded.
º All other behaviour from v83 is unchanged (retry/back-off, --force,
  --log-failures, timestamped console + file logging, etc.).
"""
import argparse, csv, json
from json import JSONDecodeError
import logging, os, re, sys, textwrap, time, datetime
from typing import Dict, Any, List
from tqdm import tqdm

import requests
try:
    import google.generativeai as genai
except ImportError:
    genai = None

# ── Prompt template ─────────────────────────────────────────────────────────
PROMPT_TMPL = textwrap.dedent(r"""\
I need you to act like a expert horologist and an expert data analyst.
In the EXISTING_DATA for a watch:
- Translate COLLECTION and DESCRIPTION fields from french to english and Extract as much information as you can from english translated fields for COLLECTION and DESCRIPTION for DESIRED_KEYS.
- Convert the following watch specification blob into *valid JSON* containing exactly the keys listed under DESIRED_KEYS. If a value is missing, output an empty string—never invent data.
- If a reference number is found within the translated DESCRIPTION or COLLECTION, extract it and assign it to the 'Reference' field.
- If a Strap type is found within the english translated DESCRIPTION or COLLECTION, extract it and assign it to the 'BRACELET STRAP' field.
- MOVEMENT also called as caliber generally includes the frequency of the movement in vhp (Vibrations Per Hour) and rotor is part of the movement.
- CERTIFICATION if the watch or the movement had COSC or geneva seal or certfications, call out here.
- WATER RESISTANCE is in mt(meters) or atm. This calls out the water resistence of the watch.
- THICKNESS is in mm(millimeters), its the thickness of the watch measured from the front glass to the case back. thsi is less than the size of the watch.
- CASE MATERIAL is the metal or the material that the case of the watch is made. Some brans offer cases in multiple materials.
- COLLECTION field MUST include the full translated text from french to english.
- DESCRIPTION field MUST include the full translated text from french to english.
- NUMBER OF CARATS is if precious stones are used in the watch then total number of CARATS.
- WATER RESISTANCE  
    -Search the translated COLLECTION and DESCRIPTION for any pattern matching  
    - `\d+\s?(m|atm)` (case-insensitive), e.g. “30 m”, “3 atm”, “100 m”.  
    - Return exactly the number and unit (with a space before the unit).  
    - If no match is found, leave WATER RESISTANCE as an empty string.
- SIZE  
    - Search the translated fields for diameter or dimensions in millimetres, matching  
    `(?:Ø\s?)?\d+(\.\d+)?\s?mm` (e.g. “Ø 42 mm”, “38 mm”, “42.5 mm”).  
    - Return exactly the numeric value and “mm” (with or without the “Ø” prefix).  
    - If you find a range (e.g. “38 × 45 mm”), take the first number and unit (e.g. “38 mm”).  
    - If no clear pattern is found, output an empty string for SIZE.
-  PRICE INCL. VAT
  - In the translated COLLECTION and DESCRIPTION, look for any standalone price patterns, e.g. “15 000”, “€15 000”, “15,000 USD”, or “15 000 CHF”.  
  - If no “incl. VAT” or “excl. VAT” qualifier is present, **assume this is the inclusive-VAT MSRP** and assign it to **PRICE INCL. VAT** (include the currency symbol if given).  
  - Do **not** populate PRICE EXCL. VAT in this case (leave it empty).  
  - If the text explicitly says “excl. VAT,” then map that figure to PRICE EXCL. VAT instead.  
  - If no clear price is found, leave both PRICE INCL. VAT and PRICE EXCL. VAT empty.

EXISTING_DATA:
{existing}

DESIRED_KEYS (maintain this order):
{desired}

Respond with _only_ valid JSON**, with no surrounding commentary, and no trailing commas or comments.
All backslashes (\) in your JSON must be double-escaped (i.e. \\) so that the output is strictly valid JSON.
""")

# ── LLM constants ──────────────────────────────────────────────────────────
OLLAMA_URL   = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "gemma3:12b"
#OLLAMA_MODEL = "llama3.2:latest"
_json_re     = re.compile(r"\{.*\}", re.S)
_invalid_bs  = re.compile(r'\\(?!["\\/bfnrtu])')

# ── Helpers ────────────────────────────────────────────────────────────────
def build_prompt(row: Dict[str, str], output_keys: List[str],
                 prompt_keys: List[str]) -> str:
    existing = {k: (row.get(k, "") or "") for k in output_keys}
    return PROMPT_TMPL.format(
        existing=json.dumps(existing, ensure_ascii=False, indent=2),
        desired="\n".join(prompt_keys),
    )

def _extract_json(txt: str) -> str:
    m = _json_re.search(txt)
    if not m:
        raise ValueError("No JSON object found in model output")
    return m.group(0)

def call_ollama(prompt: str, *, debug=False,
                timeout=180, retries=3, backoff=3) -> Dict[str, Any]:
#def call_ollama(prompt: str, *, debug : bool =False,
#                timeout: int = 180,
#                retries: int = 3,
#                backoff: int = 3,
#                temperature: float = 0.2,
#                top_p: float = 0.9,
#                max_new_tokens: int = 1024) -> Dict[str, Any]:

    payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}
 #   payload = {
 #       "model":            OLLAMA_MODEL,
 #       "prompt":           prompt,
 #       "stream":           False,
 #       "temperature":      temperature,
 #       "top_p":            top_p,
 #       "max_new_tokens":   max_new_tokens
#  }

    for attempt in range(1, retries + 1):
        try:
            if debug:
                logging.debug("POST %s (attempt %d)", OLLAMA_URL, attempt)
            r = requests.post(OLLAMA_URL, json=payload, timeout=timeout)
            r.raise_for_status()
            raw = r.json().get("response", "")
            if debug:
                logging.debug("[Ollama raw]\n%s", raw)
            # extract the JSON blob and escape any stray backslashes
            json_text = _extract_json(raw)
            json_text = _invalid_bs.sub(r'\\\\', json_text)
            try:
                return json.loads(json_text)
            except JSONDecodeError:
                # Fallback: escape all backslashes and retry
                sanitized = json_text.replace("\\", "\\\\")
                return json.loads(sanitized)
        except (requests.Timeout, requests.ConnectionError) as exc:
            logging.warning("Ollama timeout/conn-err (%s) attempt %d/%d",
                            exc, attempt, retries)
            if attempt == retries:
                raise
            time.sleep(backoff * attempt)

def call_gemini(prompt: str, key: str, debug: bool = False) -> Dict[str, Any]:
    if genai is None:
        raise RuntimeError("google-generativeai package missing")
    genai.configure(api_key=key)
    model = genai.GenerativeModel("gemini-1.5-pro-latest")
    resp = model.generate_content(prompt)
    raw = resp.text
    if debug:
        logging.debug("[Gemini raw]\n%s", raw)
    # extract the JSON blob and escape any stray backslashes
    json_text = _extract_json(raw)
    json_text = _invalid_bs.sub(r'\\\\', json_text)
    try:
        return json.loads(json_text)
    except JSONDecodeError:
        # Fallback: escape all backslashes and retry
        sanitized = json_text.replace("\\", "\\\\")
        return json.loads(sanitized)

def clean_llm_result(d: Dict[str, Any], prompt_keys: List[str]) -> Dict[str, str]:
    trim = {k.strip(): v for k, v in d.items()}
    return {k: str(trim.get(k, "")).strip() for k in prompt_keys}

def has_new_data(row: Dict[str, str], prompt_keys: List[str]) -> bool:
    return all(row.get(f"{k}_NEW", "").strip() for k in prompt_keys)

# ── Main ───────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser("Enrich legacy GPHG specs with an LLM", add_help=True)
    ap.add_argument("-I", "--input", required=True)
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("-m", "--model", choices=("geminipro", "ollama"),
                    default="geminipro")
    ap.add_argument("-k", "--apikey")
    ap.add_argument("--force", action="store_true",
                    help="send every row to the model, ignore filters/_NEW flags")
    ap.add_argument("--log-failures", action="store_true",
                    help="write <output>.fail.csv with rows that still fail")
    ap.add_argument("--test", action="store_true", help="run in test mode (skip actual LLM calls)")
    ap.add_argument("--debug", action="store_true", help="enable debug logging")
    args = ap.parse_args()

    # ── console + file logging ───────────────────────────────────────────────
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fh = logging.FileHandler(f"SuperEnrich_{ts}.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
    fh.setLevel(logging.DEBUG if args.debug else logging.INFO)
    logging.getLogger().addHandler(fh)
    # ──────────────────────────────────────────────────────────────────────────

    if args.model == "geminipro" and not args.apikey:
        logging.error("--apikey is required when --model geminipro")
        sys.exit(1)

    # ── Load CSV + dynamic header ────────────────────────────────────────────
    with open(args.input, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="|")
        input_header: List[str] = [h.strip() for h in reader.fieldnames]
        all_rows: List[Dict[str, str]] = list(reader)

    # drop any pre-existing _NEW columns
    base_keys = [h for h in input_header if not h.endswith("_NEW")]

    OUTPUT_KEYS = base_keys
    PROMPT_KEYS = [k for k in OUTPUT_KEYS if k not in ("PrizeType","Category")]
    HEADER      = base_keys + [f"{k}_NEW" for k in PROMPT_KEYS]

    # ── Which to enrich ───────────────────────────────────────────────────────
    if args.force:
        target_rows = all_rows
    else:
        target_rows = [
            r for r in all_rows
            # if r.get("Year") in ("2001")
            if r.get("Year") in ("2001","2002","2003","2004","2005","2006","2007")
        ]

    logging.info("Loaded %d rows (%d to enrich); backend=%s-%s",
                 len(all_rows), len(target_rows), args.model, OLLAMA_MODEL)

    enriched, failed = 0, []

    for row in tqdm(target_rows, desc="Enriching rows", unit="row"):
        if not args.force and has_new_data(row, PROMPT_KEYS):
            continue

        logging.info("LLM → Year=%s, Brand=%s, Model=%s",
                     row.get("Year",""),
                     row.get("Brand",""),
                     row.get("Model",""))

        prompt = build_prompt(row, OUTPUT_KEYS, PROMPT_KEYS)
        if args.debug:
            logging.debug("[PROMPT]\n%s", prompt)

        try:
            if args.test:
                raise RuntimeError("test-mode (LLM skipped)")

            if args.model == "ollama":
                result = call_ollama(prompt, debug=args.debug)
            else:
                result = call_gemini(prompt, key=args.apikey, debug=args.debug)

            row.update({
                f"{k}_NEW": v
                for k,v in clean_llm_result(result, PROMPT_KEYS).items()
            })
            enriched += 1

            if not args.test and enriched % 50 == 0:
                logging.info("Processed %d rows …", enriched)

        except Exception as exc:
            logging.warning("Failed – %s %s: %s",
                            row.get("Year",""), row.get("Reference","??"), exc)
            # only collect these four columns for the .fail.csv
            failed.append({
                "Year":  row.get("Year",""),
                "Brand": row.get("Brand",""),
                "Model": row.get("Model",""),
                "Error": str(exc),
            })
            # ensure every *_NEW column still exists
            for k in PROMPT_KEYS:
                row.setdefault(f"{k}_NEW", "")

        if args.test:
            break

    # ── Write final enriched CSV ─────────────────────────────────────────────
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, delimiter="|")
        w.writeheader()
        for r in all_rows:
            for k in PROMPT_KEYS:
                r.setdefault(f"{k}_NEW","")
            w.writerow({c: r.get(c,"") for c in HEADER})

    logging.info("Done. Wrote %d rows to %s",
                 len(all_rows), os.path.abspath(args.output))

    # ── Optional failures side-car ─────────────────────────────────────────
    if args.log_failures and failed:
        fail_fn = args.output + ".fail.csv"
        with open(fail_fn, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f,
                fieldnames=["Year","Brand","Model","Error"], delimiter="|")
            w.writeheader()
            w.writerows(failed)
        logging.warning("Saved %d failed rows → %s",
                        len(failed), os.path.abspath(fail_fn))


if __name__ == "__main__":
    main()
