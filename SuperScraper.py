#!/usr/bin/env python3
"""
SuperScraper_v80

* Keeps gphg_header_master.json as the stable output contract.
* Reports newly discovered fields, but only mutates the master header when
  --update-header is supplied.
* Adds request timeouts/retries, safer year handling, detail-page caching, and
  atomic output writes.
"""
import argparse, os, re, sys, csv, itertools, requests, json, logging, html
from bs4 import BeautifulSoup, Tag
from collections import OrderedDict
from urllib.parse import urljoin

try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover - requests normally provides these
    HTTPAdapter = None
    Retry = None

# ── URLs ────────────────────────────────────────────────────────────────────
BASE    = "https://www.gphg.org"
ARCHIVE = BASE + "/en/archives"
DEFAULT_PARTICIPANTS_YEAR = 2025
REQUEST_TIMEOUT = 30
REQUEST_RETRIES = 3
USER_AGENT = "Mozilla/5.0 (compatible; GPHG-SuperScraper/80)"

# ── List page patterns ─────────────────────────────────────────────────────
LIST_DEFS = OrderedDict([
    ("P", {"patterns": (r"participant", r"competing")}),

    ("S", {"patterns": (r"pre[-\s]?selected",)}),
    ("N", {"patterns": (r"nominated",)}),
    ("W", {"patterns": ()}),             # winners => by text match
])

# ── Canonicalisation table ─────────────────────────────────────────────────
KEY_ALIASES = {
    r"^MATERIAL$": "CASE MATERIAL",
    r"^PRICE\s+EXC\.?\s*VAT$"        : "PRICE EXCL. VAT",
    r"^PRICE\s+EXCL\.?\s*VAT$"       : "PRICE EXCL. VAT",
    r"^PRICE\s+EXCLUDING\s+VAT$"     : "PRICE EXCL. VAT",
    r"^PRICE\s+EXCL\.?\s*TAX$"       : "PRICE EXCL. VAT",
    r"^CASE\s+MATERIALS?$"           : "CASE MATERIAL",
    r"^WATER\s+RESISTANCE$"          : "WATER RESISTANCE",
    r"^REF(?:E|ER|EREN)?$"           : "REFERENCE",
    # …extend as you discover more variants…
}
ALIAS_MAP = [(re.compile(pat, re.I), name) for pat, name in KEY_ALIASES.items()]

def canon(key: str) -> str:
    """Return canonical column name."""
    key = key.strip().upper()
    for rx, canon_name in ALIAS_MAP:
        if rx.match(key):
            return canon_name
    return key

# ── Persistent master header ───────────────────────────────────────────────
MASTER_FILE = "gphg_header_master.json"

def normalize_master_field(field: str) -> str:
    return canon(field)

def load_master() -> list[str]:
    if os.path.exists(MASTER_FILE):
        with open(MASTER_FILE, encoding="utf-8") as fh:
            return list(json.load(fh))
    return []

def save_master(fields) -> None:
    with open(MASTER_FILE, "w", encoding="utf-8") as fh:
        json.dump(sorted(fields), fh, indent=2)

# ── Fixed columns ──────────────────────────────────────────────────────────
FIXED_FRONT = ["Year", "Language", "Brand", "Model", "Reference"]
FIXED_BACK  = ["PrizeType", "Category"]
KNOWN_MASTER_FIELDS = {normalize_master_field(f) for f in load_master()}

# prevent case-duplication of fixed columns
RESERVED_UPPER = {c.upper() for c in FIXED_FRONT + FIXED_BACK}
RESERVED_UPPER.update({"REFERENCE", "URL"})

def language_for_year(year: int) -> str:
    """Language of the source description text for known GPHG archive periods."""
    if 2001 <= int(year) <= 2007:
        return "fr"
    if int(year) >= 2008:
        return "en"
    return "unknown"

# ── CLI parsing ────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser("GPHG scraper with canonical keys & master header")
    p.add_argument("-y", "--year",   type=int, help="Single year to scrape")
    p.add_argument("-e", "--export", action="store_true", help="Write TXT output")
    p.add_argument("-d", "--debug",  action="store_true", help="Debug logging")
    p.add_argument("-P", "--participants", action="store_true",
                   help="Also scrape current-year competing watches before archive output")
    p.add_argument("--participants-year", type=int, default=DEFAULT_PARTICIPANTS_YEAR,
                   help="Competition year to assign to current-year participant rows")
    p.add_argument("--participants-url",
                   help="Override the current-year competing watches URL")
    p.add_argument("--update-header", action="store_true",
                   help="Persist newly discovered spec fields into the master header")
    p.add_argument("--timeout", type=int, default=REQUEST_TIMEOUT,
                   help="HTTP request timeout in seconds")
    p.add_argument("--retries", type=int, default=REQUEST_RETRIES,
                   help="HTTP retry count for transient failures")
    return p.parse_args()

# ── Helper functions (mostly unchanged) ────────────────────────────────────
def make_session(retries: int) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    if HTTPAdapter and Retry:
        retry_args = {
            "total": retries,
            "connect": retries,
            "read": retries,
            "status": retries,
            "backoff_factor": 1,
            "status_forcelist": (429, 500, 502, 503, 504),
        }
        try:
            retry = Retry(allowed_methods=frozenset(["GET"]), **retry_args)
        except TypeError:
            retry = Retry(method_whitelist=frozenset(["GET"]), **retry_args)
        session.mount("http://", HTTPAdapter(max_retries=retry))
        session.mount("https://", HTTPAdapter(max_retries=retry))
    return session

def fetch_soup(session: requests.Session, url: str, timeout: int) -> BeautifulSoup:
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    soup.raw_html = resp.text
    return soup

def absolute_url(href: str) -> str:
    return urljoin(BASE, href)

def embedded_watch_links(raw_html: str) -> list[str]:
    return [url for url, _cat in embedded_watch_link_pairs(raw_html)]

def embedded_page_links(raw_html: str) -> list[str]:
    text = raw_html.replace('\\"', '"')
    out = []
    seen = set()
    for href in re.findall(r"/en/[^\"<\\\s?]+", text):
        if href not in seen:
            out.append(href)
            seen.add(href)
    return out

def embedded_watch_link_pairs(raw_html: str) -> list[tuple[str, str]]:
    text = raw_html.replace('\\"', '"')
    token_rx = re.compile(
        r'"id":"[^"]+","children":"([^"]+)"|'
        r'"href":"(/en/watches/[^"<\\\s?]+)"'
    )
    out = []
    seen = set()
    category = ""
    for match in token_rx.finditer(text):
        if match.group(1):
            candidate = clean_next_text(match.group(1))
            if (
                candidate
                and len(candidate) <= 80
                and not re.search(r"[{};=]|window|function|dataLayer|gtag", candidate)
            ):
                category = candidate
            continue
        href = match.group(2)
        url = absolute_url(href)
        if url not in seen:
            out.append((url, category))
            seen.add(url)
    if out:
        return out

    for href in re.findall(r"/en/watches/[^\"<\\\s?]+", raw_html):
        url = absolute_url(href)
        if url not in seen:
            out.append((url, ""))
            seen.add(url)
    return out

def find_archive_links(session, timeout):
    soup = fetch_soup(session, ARCHIVE, timeout)
    links = {}
    for a in soup.select("a[href]"):
        m = re.search(r"/gphg[-/]?(\d{4})", a["href"])
        if m:
            links[int(m.group(1))] = absolute_url(a["href"])
    for href in embedded_page_links(soup.raw_html):
        m = re.search(r"/en/gphg-(\d{4})\b", href)
        if m:
            links.setdefault(int(m.group(1)), absolute_url(href))
    return links

def find_list_pages(session, year_url, year, timeout, debug=False):
    soup = fetch_soup(session, year_url, timeout)
    pages = dict.fromkeys(("P","S","N","W"), None)

    for code in ("P","S","N"):
        for a in soup.select("a[href]"):
            href, txt = a["href"].lower(), a.get_text(" ",strip=True).lower()
            if any(re.search(p, href) or re.search(p, txt) for p in LIST_DEFS[code]["patterns"]):
                pages[code] = absolute_url(a["href"]); break

    yy2 = str(year)[-2:]
    txt_candidates = {f"prize list {year}", f"prize list {yy2}"}
    for a in soup.select("a[href]"):
        if a.get_text(strip=True).lower() in txt_candidates:
            pages["W"] = absolute_url(a["href"]); break
    if not pages["W"]:
        for a in soup.select("a[href]"):
            if re.search(rf"prize-list-{yy2}\b", a["href"].lower()):
                pages["W"] = absolute_url(a["href"]); break
    if not pages["W"]:
        pages["W"] = BASE + f"/en/prize-list-{yy2}"

    for href in embedded_page_links(soup.raw_html):
        low = href.lower()
        if not pages["P"] and re.search(r"(competing|participants?)", low):
            pages["P"] = absolute_url(href)
        elif not pages["N"] and re.search(r"(nominated|nominees)", low):
            pages["N"] = absolute_url(href)
        elif re.search(r"prize-list-(?:\d{2}|\d{4})", low):
            pages["W"] = absolute_url(href)

    # The current GPHG site is a Next.js app; some archive/year pages are no
    # longer server-rendered with normal anchor tags. Fall back to known routes.
    pages["P"] = pages["P"] or BASE + f"/en/gphg-{year}/competing-watches"
    pages["N"] = pages["N"] or BASE + f"/en/gphg-{year}/nominated-timepieces"

    if debug:
        logging.debug("Pages %s: %s", year, pages)
    return pages

def direct_year_url(year: int) -> str:
    return BASE + f"/en/gphg-{year}"

def participants_url(year: int) -> str:
    return BASE + f"/en/gphg-{year}/competing-watches"

def extract_detail_links(session, list_url, code, timeout, debug=False):
    soup = fetch_soup(session, list_url, timeout)
    out = []

    if code == "W":
        top_cat = ""
        if (b := soup.select_one("div.box-main-title-container div.main-title")):
            top_cat = b.get_text(strip=True)
        for a in soup.select("a[href^='/en/watches/']"):
            url = absolute_url(a["href"]); cat = ""
            for prev in a.previous_elements:
                txt = prev.get_text(strip=True) if isinstance(prev, Tag) else str(prev)
                if re.search(r"(Prize|Grand Prix)$", txt):
                    cat = txt; break
            out.append((url, cat))
        if not out:
            out.extend(embedded_watch_link_pairs(soup.raw_html))
        if top_cat and out:
            out[0] = (out[0][0], top_cat)

    elif code == "N":
        for img in soup.select("a[href^='/en/watches/'] > img"):
            link = img.parent; url = absolute_url(link["href"]); cat = ""
            for prev in link.previous_elements:
                if isinstance(prev, Tag) and prev.name == "h3":
                    t = prev.get_text(strip=True)
                    if t.lower() != "nominated timepieces":
                        cat = t; break
            out.append((url, cat))
        if not out:
            out.extend(embedded_watch_link_pairs(soup.raw_html))

    else:
        for a in soup.select("a[href^='/en/watches/']"):
            out.append((absolute_url(a["href"]), ""))
        if not out:
            out.extend(embedded_watch_link_pairs(soup.raw_html))

    if debug:
        logging.debug("%s: %d URLs", code, len(out))
    return out

def extract_participants_links(session, list_url, timeout, debug=False):
    soup = fetch_soup(session, list_url, timeout)
    out = []
    for a in soup.select("a[href^='/en/watches/']"):
        url = absolute_url(a["href"])
        # Try to infer category by scanning previous heading siblings
        cat = ""
        cur = a
        while cur:
            cur = cur.find_previous(["h2", "h3", "h4"])  # nearest heading
            if cur:
                t = cur.get_text(" ", strip=True)
                if t and not re.search(r"(competing|participants?)", t, re.I):
                    cat = t
                break
        out.append((url, cat))
    if not out:
        out.extend(embedded_watch_link_pairs(soup.raw_html))
    if debug:
        logging.debug("Participants: %d URLs from %s", len(out), list_url)
    return out

def extract_specs(session, url, timeout):
    soup = fetch_soup(session, url, timeout)
    d = {}

    if el := soup.select_one("div.right-content-breadcrumb"):
        d["Brand"] = el.get_text(strip=True)
    if h1 := soup.select_one("h1"):
        d["Model"] = h1.get_text(strip=True)

    if sec := soup.select_one("div.watch-detail-section-specs"):
        for lbl, val in zip(sec.select("div.field-label"), sec.select("div.field-items")):
            d[canon(lbl.get_text(" ", strip=True))] = val.get_text(" ", strip=True)

    for dt in soup.find_all("dt"):
        key = canon(dt.get_text(" ", strip=True))
        if dd := dt.find_next_sibling("dd"):
            d[key] = dd.get_text(" ", strip=True)

    if not d.get("Brand") or not d.get("Model"):
        if og := soup.select_one("meta[property='og:title']"):
            title = og.get("content", "")
        elif soup.title:
            title = soup.title.get_text(" ", strip=True).removesuffix("| GPHG").strip()
        else:
            title = ""
        if "," in title:
            brand, model = title.split(",", 1)
            d.setdefault("Brand", brand.strip())
            d.setdefault("Model", model.strip())

    if not any(k not in ("Brand", "Model") for k in d):
        d.update(extract_specs_from_next_html(soup.raw_html))

    d.setdefault("REFERENCE", "")
    return d

def _parse_next_child_value(text: str, pos: int, labels: dict[str, str]) -> str:
    while pos < len(text) and text[pos].isspace():
        pos += 1
    if pos >= len(text):
        return ""
    if text[pos] == '"':
        end = pos + 1
        while end < len(text):
            if text[end] == '"' and text[end - 1] != "\\":
                return clean_next_text(resolve_next_ref(text[pos + 1:end], text, labels))
            end += 1
        return ""
    if text[pos] != "[":
        return ""

    depth = 0
    in_str = False
    end = pos
    while end < len(text):
        ch = text[end]
        if ch == '"' and text[end - 1] != "\\":
            in_str = not in_str
        elif not in_str:
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end += 1
                    break
        end += 1
    fragment = text[pos:end]
    fragment = re.sub(
        r'"\$L([0-9a-f]+)"',
        lambda m: json.dumps(labels.get(m.group(1), ""), ensure_ascii=False),
        fragment,
    )
    if m := re.search(r'"__html":"(.*?)"', fragment):
        return clean_next_text(resolve_next_ref(m.group(1), text, labels))
    ignore = {
        "$", "div", "span", "a", "ul", "li", "null", "className", "children",
        "clearfix", "field-label", "field-items", "field-items field-description",
        "dangerouslySetInnerHTML", "__html", "target", "_blank", "href", "br",
    }
    parts = []
    for value in re.findall(r'"([^"]*)"', fragment):
        if value and value not in ignore and not value.startswith("watch-"):
            parts.append(value)
    return clean_next_text(join_next_parts(parts))

def join_next_parts(parts: list[str]) -> str:
    out = ""
    for part in parts:
        if not out:
            out = part
        elif out[-1:].isalnum() and part[:1].islower():
            out += part
        else:
            out += " " + part
    return out

def resolve_next_ref(value: str, text: str, labels: dict[str, str]) -> str:
    if not value.startswith("$"):
        return value
    token = value[1:]
    if token in labels:
        return labels[token]
    token_pos = text.find(f"{token}:")
    if token_pos == -1:
        return value
    push_pos = text.find('self.__next_f.push([1,"', token_pos)
    if push_pos == -1:
        return value
    start = push_pos + len('self.__next_f.push([1,"')
    end = text.find('"])', start)
    if end == -1:
        return value
    return text[start:end]

def clean_next_text(value: str) -> str:
    value = (
        value.replace(r"\u003c", "<")
        .replace(r"\u003e", ">")
        .replace(r"\u0026", "&")
        .replace(r"\n", " ")
        .replace(r"\r", " ")
    )
    value = html.unescape(value)
    if "<" in value and ">" in value:
        value = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    value = re.sub(r"(^|\s)br($|\s)", " ", value)
    return re.sub(r"\s+", " ", value).strip()

def extract_specs_from_next_html(raw_html: str) -> dict:
    text = raw_html.replace('\\"', '"')
    labels = dict(re.findall(r'([0-9a-f]+):"([^"]*)"', text))
    specs = {}
    label_rx = re.compile(
        r'className":"field-label","children":(?:"\$L([0-9a-f]+)"|"([^"]+)")'
    )
    for match in label_rx.finditer(text):
        label = labels.get(match.group(1), "") if match.group(1) else match.group(2)
        if not label:
            continue
        items_idx = text.find('className":"field-items', match.end())
        if items_idx == -1:
            continue
        child_idx = text.find('"children":', items_idx)
        if child_idx == -1:
            continue
        value = _parse_next_child_value(text, child_idx + len('"children":'), labels)
        if value:
            specs[canon(label)] = value
    return specs

def add_spec_keys(specs: dict, all_spec_keys: set[str]) -> None:
    for k in specs:
        ck = canon(k)
        if ck.upper() in RESERVED_UPPER:
            continue
        if any(ch in ck for ch in "[]<>") or ck.startswith(")") or len(ck) < 4:
            continue
        if ck not in KNOWN_MASTER_FIELDS and any(
            field.startswith(ck) for field in KNOWN_MASTER_FIELDS
        ):
            continue
        if ck:
            all_spec_keys.add(ck)

def get_specs(session, detail_cache, url, timeout):
    if url not in detail_cache:
        detail_cache[url] = extract_specs(session, url, timeout)
    return detail_cache[url]

def write_rows_atomic(out_fn: str, header: list[str], rows: list[dict]) -> None:
    tmp_fn = f"{out_fn}.tmp"
    with open(tmp_fn, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header, delimiter="|",
                           quotechar='"', quoting=csv.QUOTE_ALL,
                           extrasaction="ignore")
        w.writeheader()
        for r in rows:
            r = {k: v for k, v in r.items() if k != "URL"}
            r = {k: clean_output_value(v) for k, v in r.items()}
            w.writerow(r)

    with open(tmp_fn, "r+", encoding="utf-8") as f:
        txt = f.read()
        if "\t" in txt:
            f.seek(0)
            f.write(txt.replace("\t", " "))
            f.truncate()
            logging.info("Cleaned tabs → spaces in %s", tmp_fn)

    os.replace(tmp_fn, out_fn)

def clean_output_value(value) -> str:
    if value is None:
        return ""
    value = str(value)
    value = re.sub(r"\s*\]\)\s*$", "", value).strip()
    if "<" in value and ">" in value:
        value = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", value).strip()

# ── Main ───────────────────────────────────────────────────────────────────
def main():
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    session = make_session(args.retries)

    out_fn = f"GPHG_Prize_{args.year}.txt" if args.year else "GPHG_Prize.txt"
    all_rows, all_spec_keys = [], set()
    detail_cache = {}

    # Optional: scrape current-year competing watches first
    if args.participants:
        part_url = args.participants_url or participants_url(args.participants_year)
        logging.info("Scraping %s competing watches: %s",
                     args.participants_year, part_url)
        try:
            p_links = extract_participants_links(
                session, part_url, args.timeout, args.debug)
        except Exception as exc:
            logging.warning("Failed to fetch participants page (%s): %s", part_url, exc)
            p_links = []
        total = len(p_links)
        if total:
            logging.info("Fetching %s P detail pages…", total)
        # add spinner/progress like yearly loops
        spin = itertools.cycle(["-", "\\", "|", "/"])
        for idx, (url, cat) in enumerate(p_links, 1):
            sys.stdout.write(f"\r P {idx}/{total} {next(spin)}")
            sys.stdout.flush()
            try:
                specs = get_specs(session, detail_cache, url, args.timeout)
            except Exception as exc:
                logging.warning("Failed detail page (%s): %s", url, exc)
                continue
            add_spec_keys(specs, all_spec_keys)
            row = {
                "Year": args.participants_year,
                "Language": language_for_year(args.participants_year),
                "Brand": specs.get("Brand", ""),
                "Model": specs.get("Model", ""),
                "Reference": specs.get("REFERENCE", ""),
                "PrizeType": "P",
                "Category": cat,
                "URL": url,
            }
            row.update(specs)
            all_rows.append(row)
        if total:
            # clear the progress line
            sys.stdout.write("\r" + " " * 60 + "\r")
            logging.info("Completed participants (%s).", args.participants_year)

    archive = find_archive_links(session, args.timeout)
    years = [args.year] if args.year else sorted(archive, reverse=True)

    for year in years:
        year_url = archive.get(year) or direct_year_url(year)
        if year not in archive:
            logging.warning("Year %s not found in archive links; trying %s",
                            year, year_url)
        try:
            pages = find_list_pages(
                session, year_url, year, args.timeout, args.debug)
        except Exception as exc:
            logging.warning("Failed year page (%s): %s", year_url, exc)
            continue

        details = {}
        for code in LIST_DEFS:
            url = pages.get(code)
            if url:
                logging.info("Starting fetch for %s watches : %s", code, url)
                try:
                    details[code] = extract_detail_links(
                        session, url, code, args.timeout, args.debug)
                except Exception as exc:
                    logging.warning("Failed list page (%s %s): %s", code, url, exc)
                    details[code] = []
            else:
                details[code] = []

        for code, lst in details.items():
            total = len(lst)
            if total:
                logging.info("Fetching %s %s detail pages…", total, code)
            spin = itertools.cycle(["-", "\\", "|", "/"])
            for idx, (url, cat) in enumerate(lst, 1):
                sys.stdout.write(f"\r {code} {idx}/{total} {next(spin)}")
                sys.stdout.flush()
                try:
                    specs = get_specs(session, detail_cache, url, args.timeout)
                except Exception as exc:
                    logging.warning("Failed detail page (%s): %s", url, exc)
                    continue
                add_spec_keys(specs, all_spec_keys)
                row = {
                    "Year": year,
                    "Language": language_for_year(year),
                    "Brand": specs.get("Brand", ""),
                    "Model": specs.get("Model", ""),
                    "Reference": specs.get("REFERENCE", ""),
                    "PrizeType": code,
                    "Category": cat,
                    "URL": url,
                }
                row.update(specs)
                all_rows.append(row)
            if total:
                sys.stdout.write("\r" + " " * 60 + "\r")
                logging.info("Completed %s.", code)
        logging.info("Completed year %s", year)

    if not args.export:
        return

    if not all_rows:
        logging.error("No rows scraped; refusing to overwrite %s", out_fn)
        sys.exit(2)

    # ── Merge duplicates ──
    ORDER = {"P": 0, "S": 1, "N": 2, "W": 3}
    merged = {}
    for r in all_rows:
        key = r["URL"]
        base = merged.get(key)
        if not base:
            merged[key] = r
        else:
            if ORDER[r["PrizeType"]] > ORDER[base["PrizeType"]]:
                base["PrizeType"] = r["PrizeType"]
            if not base.get("Category") and r.get("Category"):
                base["Category"] = r["Category"]
    all_rows = list(merged.values())
    logging.info("Merged to %s unique rows", len(all_rows))

    # ── Compare/update master header ──
    raw_master = load_master()
    master = []
    seen_master = set()
    for field in raw_master:
        normalized = normalize_master_field(field)
        if normalized.upper() in RESERVED_UPPER:
            logging.debug("Skipping reserved master field: %s", field)
            continue
        if normalized not in seen_master:
            master.append(normalized)
            seen_master.add(normalized)

    fresh_keys = all_spec_keys - seen_master
    if fresh_keys:
        for k in sorted(fresh_keys):
            logging.info("NEW FIELD → %s", k)
        if args.update_header:
            seen_master.update(fresh_keys)
            master = sorted(seen_master)
            save_master(master)
            logging.info("Updated %s with %s new fields",
                         MASTER_FILE, len(fresh_keys))
        else:
            logging.info("Master header unchanged; use --update-header to persist new fields")
    header = FIXED_FRONT + sorted(master) + FIXED_BACK

    # ── Write file ──
    write_rows_atomic(out_fn, header, all_rows)

    logging.info("Written %s rows → %s", len(all_rows), out_fn)

# ────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    main()
