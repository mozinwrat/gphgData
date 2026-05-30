#!/usr/bin/env python3
"""
SuperEnrich_v85

Enrich GPHG scraper output with a selectable translation/enrichment backend.

Fixes from v83:
- safer JSON extraction from LLM output
- retries malformed LLM JSON responses
- Ollama is the default local backend
- dry-run/test mode no longer records fake failures
- skip logic tolerates sparse *_NEW fields
- failure sidecar includes row number and reference
- output files are written atomically
- initial DeepL translation backend
"""
import argparse
import csv
import datetime
import json
import logging
import os
import re
import sys
import tempfile
import textwrap
import time
from json import JSONDecodeError
from typing import Any, Dict, Iterable, List

import requests

try:
    import google.generativeai as genai
except ImportError:
    genai = None


PROMPT_TMPL = textwrap.dedent(
    r"""\
    I need you to act like an expert horologist and an expert data analyst.
    In the EXISTING_DATA for a watch:
    {task_instructions}
    - Process COLLECTION and DESCRIPTION according to the task instructions above, then extract as much information as you can from those fields for DESIRED_KEYS.
    - Prioritize structured specification sections such as "Fiche Technique", "Données Techniques", "Technical data", "Technical specifications", or their English translations over marketing prose.
    - When a structured specification section is present, treat it as the authoritative source for Reference, Movement, Functions, Thickness, Case Material, Water Resistance, Dial Finish, Bracelet Strap, Buckle, Certification, and other spec fields.
    - Preserve all alternatives listed in a structured specification section. If the source says "ou" / "or", keep every option and separate multiple extracted values with "; " where useful.
    - Convert the watch data into valid JSON containing exactly the keys listed under DESIRED_KEYS. If a value is missing, output an empty string. Never invent data.
    - Every textual value in the JSON response must be in English, even when extracted from French source specifications.
    - Do not leave French words or phrases in fields such as CASE MATERIAL, BRACELET STRAP, BUCKLE, FUNCTIONS, MOVEMENT, DIAL FINISH, CERTIFICATION, SUSTAINABILITY, COLLECTION, or DESCRIPTION.
    - Preserve only brand names, model names, proper collection names, reference numbers, calibre names/numbers, units, and currency symbols exactly when appropriate.
    - Translate common watch terms, for example: or=gold, or gris=white gold, or rose=rose gold, acier=steel, platine=platinum, cuir=leather, boucle deployante=folding/deployant clasp, remontage automatique=automatic winding, remontage manuel=manual winding, heures=hours, minutes=minutes, secondes=seconds.
    - If a reference number is found within the translated DESCRIPTION or COLLECTION, extract it and assign it to the 'Reference' field.
    - If multiple reference numbers are found, return all of them in the Reference field separated by "; ".
    - If a strap type is found within the translated DESCRIPTION or COLLECTION, extract it and assign it to the 'BRACELET STRAP' field.
    - MOVEMENT, also called caliber, should preserve technical details from the structured specification section, including calibre/caliber number, winding type, jewel count, power reserve, movement diameter, and movement thickness when present.
    - FREQUENCY is the movement beat rate. Extract it separately when present, converting terms such as "a/h", "alternances par heure", "vph", or "vibrations per hour" into a normalized value such as "28,800 vph".
    - If frequency appears in MOVEMENT too, that is acceptable, but FREQUENCY must contain the standalone normalized frequency value.
    - POWER RESERVE is the duration the movement runs when fully wound. Extract it separately when present, converting terms such as "réserve de marche", "power reserve", "> 60 heures", or "jusqu'à 40 heures" into normalized English values such as ">60 hours" or "up to 40 hours".
    - If power reserve appears in MOVEMENT too, that is acceptable, but POWER RESERVE must contain the standalone normalized power-reserve value.
    - CERTIFICATION should call out COSC, Geneva Seal, or similar watch/movement certifications.
    - WATER RESISTANCE should be in m or atm.
    - THICKNESS should be in mm and is measured from the front glass to the case back.
    - CASE MATERIAL is the metal or material used for the watch case.
    - CASE MATERIAL must preserve all listed case material variants, for example "or jaune 3N ou gris" should become "3N yellow gold or white/grey gold".
    - SUSTAINABILITY describes sustainable, recycled, traceable, responsible, certified, or ethically sourced materials/processes when the source has a dedicated sustainability field.
    - SUSTAINABILITY must not be inferred from CASE MATERIAL alone. If the source only says "Steel", "Titanium", "Gold", or another material without a dedicated sustainability/recycled/responsible-sourcing claim, leave SUSTAINABILITY empty.
    - If the source SUSTAINABILITY field contains material text because GPHG provided it there, translate and preserve that text in SUSTAINABILITY rather than moving it to CASE MATERIAL.
    - BRACELET STRAP should preserve material and construction details, for example alligator species, padded/bombé construction, and hand stitching.
    - BUCKLE must distinguish clasp types accurately: "boucle à ardillon" means pin/tang buckle, not deployant clasp; "boucle déployante" means deployant/folding clasp.
    - COLLECTION must include the full processed collection text in English.
    - DESCRIPTION must include the full processed description text in English.
    - NUMBER OF CARATS is the total carat weight if precious stones are used.
    - WATER RESISTANCE:
      - Search translated COLLECTION and DESCRIPTION for patterns such as 30 m, 3 atm, or 100 m.
      - Return exactly the number and unit, with a space before the unit.
      - If no match is found, leave WATER RESISTANCE empty.
    - SIZE:
      - SIZE means the watch case diameter or watch case dimensions only.
      - Search translated fields for case/watch diameter or case/watch dimensions in millimetres, such as "42 mm", "42.5 mm", or "38 x 45 mm".
      - If you find a case/watch range, take the first number and unit.
      - Never use movement, calibre, caliber, or mechanism diameter/dimensions for SIZE.
      - If the only millimetre value is a movement/calibre/mechanism diameter or thickness, leave SIZE empty.
      - If no clear case/watch size is found, leave SIZE empty.
    - PRICE INCL. VAT:
      - If a standalone price appears without an incl./excl. VAT qualifier, assume this is the inclusive-VAT MSRP and assign it to PRICE INCL. VAT.
      - If text explicitly says excl. VAT, assign that figure to PRICE EXCL. VAT.
      - If no clear price is found, leave both price fields empty.

    EXISTING_DATA:
    {existing}

    DESIRED_KEYS, maintaining this order:
    {desired}

    Respond with only valid JSON. Do not include surrounding commentary, markdown fences, trailing commas, or comments.
    All backslashes in JSON strings must be double-escaped.
    Do not use double quote characters inside JSON string values. If a phrase was quoted in the source, remove the inner quotes or use apostrophes instead.
    """
)


OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:12b")
DEEPL_FREE_URL = "https://api-free.deepl.com/v2/translate"
DEEPL_PRO_URL = "https://api.deepl.com/v2/translate"

_invalid_bs = re.compile(r'\\(?!["\\/bfnrtu])')
_trailing_comma = re.compile(r",(\s*[\]}])")
_mm_value_re = re.compile(r"\b\d+(?:[.,]\d+)?\s*mm\b", re.I)
_case_size_hint_re = re.compile(r"\b(case|watch|bo[iî]te|dimension|size)\b", re.I)
_movement_size_hint_re = re.compile(r"\b(calibre|caliber|movement|mouvement|mechanism|m[ée]canisme)\b", re.I)
_accent_re = re.compile(r"[àâäçéèêëîïôöùûüÿœæ]", re.I)
_word_re = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ']+")

TRANSLATABLE_KEYS = ("COLLECTION", "DESCRIPTION")
NON_NEW_KEYS = {
    "Year", "Language", "DetectedLanguage", "LanguageDetectionScore",
    "EnrichmentTask", "Brand", "Model", "PrizeType", "Category",
}
EXTRA_ENRICH_KEYS = ("FREQUENCY", "POWER RESERVE")
AUDIT_KEYS = ("DetectedLanguage", "LanguageDetectionScore", "EnrichmentTask")
TASK_TRANSLATE_ENRICH = "translate-enrich"
TASK_EXTRACT_ONLY = "extract-only"

TASK_INSTRUCTIONS = {
    TASK_TRANSLATE_ENRICH: (
        "- The source DESCRIPTION/COLLECTION may be French. Translate French text to English before extracting structured values.\n"
        "- DESCRIPTION and COLLECTION in the JSON response must be English translations when source text is French."
    ),
    TASK_EXTRACT_ONLY: (
        "- The source DESCRIPTION/COLLECTION is already English or should be treated as English. Do not translate it.\n"
        "- Copy, clean, and normalize existing English text into DESCRIPTION/COLLECTION, and extract structured values from existing fields and technical data."
    ),
}

FR_WORDS = {
    "le", "la", "les", "des", "du", "de", "une", "un", "avec", "pour",
    "dans", "sur", "par", "est", "sont", "cette", "son", "ses", "montre",
    "montres", "boitier", "boîtier", "bracelet", "mouvement", "cadran",
    "aiguilles", "heures", "minutes", "secondes", "reserve", "réserve",
    "marche", "etanche", "étanche", "etancheite", "étanchéité", "or",
    "acier", "cuir", "remontage", "manuel", "automatique", "rubis",
    "calibre", "frequence", "fréquence", "epaisseur", "épaisseur",
    "diametre", "diamètre", "fiche", "technique",
}
EN_WORDS = {
    "the", "and", "with", "for", "in", "on", "by", "is", "are", "this",
    "its", "watch", "watches", "case", "bracelet", "strap", "movement",
    "dial", "hands", "hours", "minutes", "seconds", "power", "reserve",
    "water", "resistant", "resistance", "gold", "steel", "leather",
    "winding", "manual", "automatic", "jewels", "calibre", "caliber",
    "frequency", "thickness", "diameter", "technical", "data",
}
FR_PHRASES = (
    "réserve de marche", "fiche technique", "données techniques",
    "remontage automatique", "remontage manuel", "boîte", "étanche", "cadran",
)
EN_PHRASES = (
    "power reserve", "technical data", "technical specifications",
    "automatic winding", "manual winding", "water resistant",
    "water resistance", "case",
)


def build_prompt(row: Dict[str, str], output_keys: List[str],
                 prompt_keys: List[str], task: str) -> str:
    existing = {k: (row.get(k, "") or "") for k in output_keys}
    return PROMPT_TMPL.format(
        task_instructions=TASK_INSTRUCTIONS[task],
        existing=json.dumps(existing, ensure_ascii=False, indent=2),
        desired="\n".join(prompt_keys),
    )


def _extract_json(txt: str) -> str:
    start = txt.find("{")
    if start == -1:
        raise ValueError("No JSON object found in model output")

    in_string = False
    escaped = False
    depth = 0
    for idx, ch in enumerate(txt[start:], start):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_string:
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return txt[start : idx + 1]

    raise ValueError("Unbalanced JSON braces in model output")


def _loads_model_json(raw: str) -> Dict[str, Any]:
    json_text = _extract_json(raw)
    json_text = _invalid_bs.sub(r"\\\\", json_text)
    json_text = _trailing_comma.sub(r"\1", json_text)
    try:
        return json.loads(json_text)
    except JSONDecodeError:
        sanitized = json_text.replace("\\", "\\\\")
        sanitized = _trailing_comma.sub(r"\1", sanitized)
        try:
            return json.loads(sanitized)
        except JSONDecodeError:
            repaired = _repair_unescaped_inner_quotes(json_text)
            repaired = _invalid_bs.sub(r"\\\\", repaired)
            try:
                return json.loads(repaired)
            except JSONDecodeError:
                repaired_sanitized = _repair_unescaped_inner_quotes(sanitized)
                repaired_sanitized = _trailing_comma.sub(r"\1", repaired_sanitized)
                return json.loads(repaired_sanitized)


def _repair_unescaped_inner_quotes(json_text: str) -> str:
    chars: List[str] = []
    in_string = False
    escaped = False
    length = len(json_text)

    for idx, ch in enumerate(json_text):
        if escaped:
            chars.append(ch)
            escaped = False
            continue
        if ch == "\\" and in_string:
            chars.append(ch)
            escaped = True
            continue
        if ch == '"':
            if not in_string:
                in_string = True
                chars.append(ch)
                continue

            next_nonspace = ""
            for nxt in json_text[idx + 1 :]:
                if not nxt.isspace():
                    next_nonspace = nxt
                    break
            if next_nonspace in {":", ",", "}", "]"} or idx == length - 1:
                in_string = False
                chars.append(ch)
            else:
                chars.append(r"\"")
            continue
        chars.append(ch)

    return "".join(chars)


def call_ollama(
    prompt: str,
    *,
    debug: bool = False,
    timeout: int = 180,
    retries: int = 5,
    backoff: int = 5,
    temperature: float = 0.1,
    top_p: float = 0.9,
    num_predict: int = 2048,
    think: bool | None = None,
) -> Dict[str, Any]:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "top_p": top_p,
            "num_predict": num_predict,
        },
    }
    if think is not None:
        payload["think"] = think

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            if debug:
                logging.debug("POST %s with model=%s attempt=%d", OLLAMA_URL, OLLAMA_MODEL, attempt)
            r = requests.post(OLLAMA_URL, json=payload, timeout=timeout)
            r.raise_for_status()
            raw = r.json().get("response", "")
            if debug:
                logging.debug("[Ollama raw]\n%s", raw)
            return _loads_model_json(raw)
        except (requests.RequestException, JSONDecodeError, ValueError) as exc:
            last_exc = exc
            logging.warning("Ollama failed attempt %d/%d: %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(backoff * attempt)

    raise RuntimeError(f"Ollama failed after {retries} attempts: {last_exc}")


def call_gemini(prompt: str, key: str, debug: bool = False) -> Dict[str, Any]:
    if genai is None:
        raise RuntimeError("google-generativeai package missing")
    genai.configure(api_key=key)
    model = genai.GenerativeModel("gemini-1.5-pro-latest")
    resp = model.generate_content(prompt)
    raw = resp.text
    if debug:
        logging.debug("[Gemini raw]\n%s", raw)
    return _loads_model_json(raw)


def call_deepl_text(
    text: str,
    *,
    api_key: str,
    url: str,
    target_lang: str,
    timeout: int = 60,
    retries: int = 3,
    backoff: int = 3,
) -> str:
    if not text.strip():
        return ""

    headers = {"Authorization": f"DeepL-Auth-Key {api_key}"}
    data = {
        "text": text,
        "source_lang": "FR",
        "target_lang": target_lang,
        "preserve_formatting": "1",
    }

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(url, headers=headers, data=data, timeout=timeout)
            r.raise_for_status()
            translations = r.json().get("translations", [])
            if not translations:
                raise ValueError("DeepL response did not include translations")
            return str(translations[0].get("text", "")).strip()
        except (requests.RequestException, ValueError, JSONDecodeError) as exc:
            last_exc = exc
            logging.warning("DeepL failed attempt %d/%d: %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(backoff * attempt)

    raise RuntimeError(f"DeepL failed after {retries} attempts: {last_exc}")


def call_deepl(row: Dict[str, str], prompt_keys: Iterable[str], args: argparse.Namespace) -> Dict[str, str]:
    result = {k: "" for k in prompt_keys}
    for key in TRANSLATABLE_KEYS:
        if key in result:
            result[key] = call_deepl_text(
                row.get(key, "") or "",
                api_key=args.deepl_key,
                url=args.deepl_url,
                target_lang=args.target_lang,
            )
    return result


def clean_result(d: Dict[str, Any], prompt_keys: List[str]) -> Dict[str, str]:
    trim = {str(k).strip(): v for k, v in d.items()}
    return {k: str(trim.get(k, "") or "").strip() for k in prompt_keys}


def _window(text: str, start: int, end: int, radius: int = 80) -> str:
    return text[max(0, start - radius) : min(len(text), end + radius)]


def _size_has_case_evidence(size: str, source_text: str) -> bool:
    normalized_size = size.replace(",", ".")
    for match in _mm_value_re.finditer(source_text):
        source_value = match.group(0).replace(",", ".")
        if source_value.lower() != normalized_size.lower():
            continue
        context = source_text[max(0, match.start() - 80) : match.start()]
        if _case_size_hint_re.search(context):
            return True
    return False


def _size_has_movement_evidence(size: str, source_text: str) -> bool:
    normalized_size = size.replace(",", ".")
    for match in _mm_value_re.finditer(source_text):
        source_value = match.group(0).replace(",", ".")
        if source_value.lower() != normalized_size.lower():
            continue
        context = _window(source_text, match.start(), match.end())
        if _movement_size_hint_re.search(context):
            return True
    return False


def guard_size_result(row: Dict[str, str], cleaned: Dict[str, str]) -> None:
    size = (cleaned.get("SIZE") or "").strip()
    if not size:
        return

    source_text = " ".join(
        row.get(key, "") or ""
        for key in ("COLLECTION", "DESCRIPTION", "MOVEMENT", "CASE MATERIAL", "SIZE")
    )
    if not source_text.strip():
        cleaned["SIZE"] = ""
        return

    if _size_has_case_evidence(size, source_text):
        return

    if _size_has_movement_evidence(size, source_text):
        logging.info("Clearing SIZE=%s because the source evidence is movement/calibre-related", size)
        cleaned["SIZE"] = ""


def year_language_fallback(year: str) -> str:
    try:
        y = int(year)
    except (TypeError, ValueError):
        return "unknown"
    if 2001 <= y <= 2007:
        return "fr"
    if y >= 2008:
        return "en"
    return "unknown"


def detect_description_language(row: Dict[str, str]) -> tuple[str, int, int]:
    text = row.get("DESCRIPTION", "") or ""
    if not text.strip():
        return "unknown", 0, 0

    low = text.casefold()
    words = [w.casefold() for w in _word_re.findall(low)]
    fr_score = sum(1 for w in words if w in FR_WORDS)
    en_score = sum(1 for w in words if w in EN_WORDS)
    fr_score += sum(4 for phrase in FR_PHRASES if phrase in low)
    en_score += sum(4 for phrase in EN_PHRASES if phrase in low)
    fr_score += min(len(_accent_re.findall(text)), 10) * 2

    total = fr_score + en_score
    if total < 3 or abs(fr_score - en_score) < 3:
        return "unknown", fr_score, en_score
    return ("fr" if fr_score > en_score else "en"), fr_score, en_score


def resolve_enrichment_task(row: Dict[str, str], requested_task: str) -> tuple[str, str]:
    detected, fr_score, en_score = detect_description_language(row)
    row["DetectedLanguage"] = detected
    row["LanguageDetectionScore"] = f"fr={fr_score};en={en_score}"

    if requested_task != "auto":
        row["EnrichmentTask"] = requested_task
        return requested_task, detected

    language = detected
    if language == "unknown":
        language = (row.get("Language") or "").strip().lower() or year_language_fallback(row.get("Year", ""))
        if language not in {"fr", "en"}:
            language = "en"

    task = TASK_TRANSLATE_ENRICH if language == "fr" else TASK_EXTRACT_ONLY
    row["EnrichmentTask"] = task
    return task, detected


def existing_new_keys(row: Dict[str, str], prompt_keys: List[str]) -> List[str]:
    return [k for k in prompt_keys if (row.get(f"{k}_NEW", "") or "").strip()]


def has_enough_new_data(row: Dict[str, str], prompt_keys: List[str]) -> bool:
    translated_originals = [k for k in TRANSLATABLE_KEYS if k in prompt_keys and (row.get(k, "") or "").strip()]
    if translated_originals:
        return all((row.get(f"{k}_NEW", "") or "").strip() for k in translated_originals)
    return bool(existing_new_keys(row, prompt_keys))


def test_result(row: Dict[str, str], prompt_keys: List[str], model: str) -> Dict[str, str]:
    result = {k: "" for k in prompt_keys}
    if model == "deepl":
        for key in TRANSLATABLE_KEYS:
            if key in result:
                original = row.get(key, "") or ""
                result[key] = f"[TEST translation skipped] {original}".strip()
    else:
        for key in prompt_keys:
            result[key] = row.get(key, "") or ""
    return result


def setup_logging(debug: bool) -> str:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = f"SuperEnrich_{ts}.log"
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
    fh.setLevel(logging.DEBUG if debug else logging.INFO)
    logging.getLogger().addHandler(fh)
    return os.path.abspath(log_file)


def load_rows(path: str) -> tuple[List[str], List[Dict[str, str]]]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="|")
        if not reader.fieldnames:
            raise ValueError(f"{path} does not contain a header row")
        input_header = [h.strip() for h in reader.fieldnames]
        return input_header, list(reader)


def write_rows_atomic(path: str, header: List[str], rows: List[Dict[str, str]]) -> None:
    out_dir = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".SuperEnrich_", suffix=".tmp", dir=out_dir, text=True)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=header, delimiter="|")
            writer.writeheader()
            for row in rows:
                writer.writerow({col: row.get(col, "") for col in header})
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def parse_years(value: str) -> set[str]:
    years: set[str] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            years.update(str(y) for y in range(int(start), int(end) + 1))
        else:
            years.add(part)
    return years


def contains_filter(value: str, needle: str | None) -> bool:
    if not needle:
        return True
    return needle.casefold() in (value or "").casefold()


def main() -> None:
    ap = argparse.ArgumentParser("Enrich legacy GPHG specs with translation or LLM enrichment")
    ap.add_argument("-I", "--input", required=True)
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("-m", "--model", choices=("ollama", "geminipro", "deepl"), default="ollama")
    ap.add_argument("--task", choices=("auto", TASK_TRANSLATE_ENRICH, TASK_EXTRACT_ONLY), default="auto",
                    help="auto chooses translate vs extract from DESCRIPTION language")
    ap.add_argument("-k", "--apikey", help="Gemini API key; kept for backward compatibility")
    ap.add_argument("--deepl-key", default=os.environ.get("DEEPL_API_KEY"), help="DeepL API key or DEEPL_API_KEY env var")
    ap.add_argument("--deepl-url", default=os.environ.get("DEEPL_URL", DEEPL_FREE_URL))
    ap.add_argument("--deepl-pro", action="store_true", help="use the DeepL Pro endpoint")
    ap.add_argument("--target-lang", default="EN-US", help="DeepL target language, e.g. EN-US or EN-GB")
    ap.add_argument("--years", default="2001-2007", help="comma/range list of years to enrich when --force is not used")
    ap.add_argument("--year", help="single-year shortcut for --years")
    ap.add_argument("--brand-contains", help="only enrich rows whose Brand contains this text")
    ap.add_argument("--model-contains", help="only enrich rows whose Model contains this text")
    ap.add_argument("--limit", type=int, help="process at most this many candidate rows")
    ap.add_argument("--output-target-only", action="store_true", help="write only candidate rows instead of the full input file")
    ap.add_argument("--ollama-timeout", type=int, default=180, help="per-request Ollama timeout in seconds")
    ap.add_argument("--ollama-retries", type=int, default=5, help="number of Ollama retry attempts")
    ap.add_argument("--ollama-num-predict", type=int, default=2048, help="maximum Ollama tokens to generate")
    ap.add_argument("--ollama-no-think", action="store_true", help="disable thinking mode for Ollama models that support it")
    ap.add_argument("--force", action="store_true", help="send every row to the backend, ignore filters/_NEW flags")
    ap.add_argument("--log-failures", action="store_true", help="write <output>.fail.csv with rows that still fail")
    ap.add_argument("--test", action="store_true", help="dry-run one row without calling external services")
    ap.add_argument("--debug", action="store_true", help="enable debug logging")
    args = ap.parse_args()

    if args.deepl_pro:
        args.deepl_url = DEEPL_PRO_URL

    log_file = setup_logging(args.debug)
    logging.info("Log file: %s", log_file)

    if args.model == "geminipro" and not args.apikey:
        logging.error("--apikey is required when --model geminipro")
        sys.exit(1)
    if args.model == "deepl" and not args.deepl_key and not args.test:
        logging.error("--deepl-key or DEEPL_API_KEY is required when --model deepl")
        sys.exit(1)

    input_header, all_rows = load_rows(args.input)
    base_keys = [h for h in input_header if not h.endswith("_NEW")]
    header_base_keys = list(base_keys)
    for key in AUDIT_KEYS:
        if key not in header_base_keys:
            header_base_keys.append(key)
    output_keys = [k for k in base_keys if k not in AUDIT_KEYS]
    for key in EXTRA_ENRICH_KEYS:
        if key not in output_keys:
            output_keys.append(key)
    prompt_keys = [k for k in output_keys if k not in NON_NEW_KEYS]
    header = header_base_keys + [f"{k}_NEW" for k in prompt_keys]

    year_filter = args.year or args.years
    target_years = parse_years(year_filter)
    if args.force:
        target_rows = [
            (data_row, data_row + 1, row)
            for data_row, row in enumerate(all_rows, start=1)
        ]
    else:
        target_rows = [
            (data_row, data_row + 1, row)
            for data_row, row in enumerate(all_rows, start=1)
            if row.get("Year") in target_years
        ]
    target_rows = [
        item for item in target_rows
        if contains_filter(item[2].get("Brand", ""), args.brand_contains)
        and contains_filter(item[2].get("Model", ""), args.model_contains)
    ]
    if args.limit is not None:
        target_rows = target_rows[: max(args.limit, 0)]

    logging.info(
        "Loaded %d data rows (%d candidate rows); backend=%s; year_filter=%s; force=%s",
        len(all_rows),
        len(target_rows),
        args.model,
        year_filter,
        args.force,
    )

    enriched = 0
    skipped = 0
    failed: List[Dict[str, str]] = []

    for data_row, csv_line, row in target_rows:
        task, detected_language = resolve_enrichment_task(row, args.task)
        for key in prompt_keys:
            row.setdefault(f"{key}_NEW", "")

        if not args.force and has_enough_new_data(row, prompt_keys):
            skipped += 1
            continue

        logging.info(
            "Enrich data_row=%s csv_line=%s Year=%s detected_language=%s task=%s Brand=%s Model=%s",
            data_row,
            csv_line,
            row.get("Year", ""),
            detected_language,
            task,
            row.get("Brand", ""),
            row.get("Model", ""),
        )

        try:
            if args.test:
                result = test_result(row, prompt_keys, args.model)
            elif args.model == "ollama":
                result = call_ollama(
                    build_prompt(row, output_keys, prompt_keys, task),
                    debug=args.debug,
                    timeout=args.ollama_timeout,
                    retries=args.ollama_retries,
                    num_predict=args.ollama_num_predict,
                    think=False if args.ollama_no_think else None,
                )
            elif args.model == "deepl":
                result = call_deepl(row, prompt_keys, args)
            else:
                result = call_gemini(build_prompt(row, output_keys, prompt_keys, task), key=args.apikey, debug=args.debug)

            cleaned = clean_result(result, prompt_keys)
            guard_size_result(row, cleaned)
            row.update({f"{k}_NEW": v for k, v in cleaned.items()})
            enriched += 1
            if enriched % 50 == 0:
                logging.info("Processed %d rows", enriched)
        except Exception as exc:
            logging.warning(
                "Failed data_row=%s csv_line=%s Year=%s Reference=%s: %s",
                data_row,
                csv_line,
                row.get("Year", ""),
                row.get("Reference", ""),
                exc,
            )
            failed.append(
                {
                    "DataRow": str(data_row),
                    "CsvLine": str(csv_line),
                    "Year": row.get("Year", ""),
                    "Brand": row.get("Brand", ""),
                    "Model": row.get("Model", ""),
                    "Reference": row.get("Reference", ""),
                    "Error": str(exc),
                }
            )

        if args.test:
            break

    output_rows = [item[2] for item in target_rows] if args.output_target_only else all_rows

    for row in output_rows:
        for key in prompt_keys:
            row.setdefault(f"{key}_NEW", "")

    write_rows_atomic(args.output, header, output_rows)
    logging.info(
        "Done. Wrote %d rows to %s; enriched=%d skipped=%d failed=%d",
        len(output_rows),
        os.path.abspath(args.output),
        enriched,
        skipped,
        len(failed),
    )

    if args.log_failures and failed:
        fail_fn = args.output + ".fail.csv"
        write_rows_atomic(fail_fn, ["DataRow", "CsvLine", "Year", "Brand", "Model", "Reference", "Error"], failed)
        logging.warning("Saved %d failed rows to %s", len(failed), os.path.abspath(fail_fn))
    elif args.log_failures:
        fail_fn = args.output + ".fail.csv"
        if os.path.exists(fail_fn):
            os.unlink(fail_fn)
            logging.info("Removed stale failure file %s", os.path.abspath(fail_fn))


if __name__ == "__main__":
    main()
