#!/usr/bin/env python3
"""
SuperScraper_v78  (v77 + canonical-key map + master header)

* New: KEY_ALIASES / canon()  → normalises variant spellings.
* New: gphg_header_master.json  → persistent superset of spec columns.
    The file updates itself; you normally never edit it by hand.
"""
import argparse, os, re, sys, csv, itertools, requests, json, logging
from bs4 import BeautifulSoup, Tag
from collections import OrderedDict
from datetime import datetime

# ── URLs ────────────────────────────────────────────────────────────────────
BASE    = "https://www.gphg.org"
ARCHIVE = BASE + "/en/archives"
PARTICIPANTS_2025 = BASE + "/en/gphg-2025/competing-watches"

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
    # …extend as you discover more variants…
}
ALIAS_MAP = [(re.compile(pat, re.I), canon) for pat, canon in KEY_ALIASES.items()]

def canon(key: str) -> str:
    """Return canonical column name."""
    key = key.strip().upper()
    for rx, canon_name in ALIAS_MAP:
        if rx.match(key):
            return canon_name
    return key

# ── Persistent master header ───────────────────────────────────────────────
MASTER_FILE = "gphg_header_master.json"
def load_master() -> set[str]:
    if os.path.exists(MASTER_FILE):
        with open(MASTER_FILE, encoding="utf-8") as fh:
            return set(json.load(fh))
    return set()

def save_master(fields: set[str]) -> None:
    with open(MASTER_FILE, "w", encoding="utf-8") as fh:
        json.dump(sorted(fields), fh, indent=2)

# ── Fixed columns ──────────────────────────────────────────────────────────
FIXED_FRONT = ["Year", "Brand", "Model", "Reference"]
FIXED_BACK  = ["PrizeType", "Category"]

# prevent case-duplication of fixed columns
RESERVED_UPPER = {c.upper() for c in FIXED_FRONT + FIXED_BACK}

# ── CLI parsing ────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser("GPHG scraper with canonical keys & master header")
    p.add_argument("-y", "--year",   type=int, help="Single year to scrape")
    p.add_argument("-e", "--export", action="store_true", help="Write TXT output")
    p.add_argument("-d", "--debug",  action="store_true", help="Debug logging")
    p.add_argument("-P", "--participants", action="store_true",
                   help="Also scrape 2025 competing watches page and append to output")
    p.add_argument("--participants-url", default=PARTICIPANTS_2025,
                   help="Override URL for 2025 competing watches page")
    return p.parse_args()

# ── Helper functions (mostly unchanged) ────────────────────────────────────
def find_archive_links():
    resp = requests.get(ARCHIVE); resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    links = {}
    for a in soup.select("a[href]"):
        m = re.search(r"/gphg[-/]?(\d{4})", a["href"])
        if m:
            links[int(m.group(1))] = BASE + a["href"]
    return links

def find_list_pages(year_url, year, debug=False):
    resp = requests.get(year_url); resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    pages = dict.fromkeys(("P","S","N","W"), None)

    for code in ("P","S","N"):
        for a in soup.select("a[href]"):
            href, txt = a["href"].lower(), a.get_text(" ",strip=True).lower()
            if any(re.search(p, href) or re.search(p, txt) for p in LIST_DEFS[code]["patterns"]):
                pages[code] = BASE + a["href"]; break

    yy2 = str(year)[-2:]
    txt_candidates = {f"prize list {year}", f"prize list {yy2}"}
    for a in soup.select("a[href]"):
        if a.get_text(strip=True).lower() in txt_candidates:
            pages["W"] = BASE + a["href"]; break
    if not pages["W"]:
        for a in soup.select("a[href]"):
            if re.search(rf"prize-list-{yy2}\b", a["href"].lower()):
                pages["W"] = BASE + a["href"]; break
    if not pages["W"]:
        pages["W"] = BASE + f"/en/prize-list-{yy2}"

    if debug:
        logging.debug("Pages %s: %s", year, pages)
    return pages

def extract_detail_links(list_url, code, debug=False):
    resp = requests.get(list_url); resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []

    if code == "W":
        top_cat = ""
        if (b := soup.select_one("div.box-main-title-container div.main-title")):
            top_cat = b.get_text(strip=True)
        for a in soup.select("a[href^='/en/watches/']"):
            url = BASE + a["href"]; cat = ""
            for prev in a.previous_elements:
                txt = prev.get_text(strip=True) if isinstance(prev, Tag) else str(prev)
                if re.search(r"(Prize|Grand Prix)$", txt):
                    cat = txt; break
            out.append((url, cat))
        if top_cat and out:
            out[0] = (out[0][0], top_cat)

    elif code == "N":
        for img in soup.select("a[href^='/en/watches/'] > img"):
            link = img.parent; url = BASE + link["href"]; cat = ""
            for prev in link.previous_elements:
                if isinstance(prev, Tag) and prev.name == "h3":
                    t = prev.get_text(strip=True)
                    if t.lower() != "nominated timepieces":
                        cat = t; break
            out.append((url, cat))

    else:
        for a in soup.select("a[href^='/en/watches/']"):
            out.append((BASE + a["href"], ""))

    if debug:
        logging.debug("%s: %d URLs", code, len(out))
    return out

def extract_participants_2025_links(list_url, debug=False):
    resp = requests.get(list_url); resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for a in soup.select("a[href^='/en/watches/']"):
        url = BASE + a["href"]
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
    if debug:
        logging.debug("P2025: %d URLs from %s", len(out), list_url)
    return out

def extract_specs(url):
    resp = requests.get(url); resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
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

    d.setdefault("REFERENCE", "")
    return d

# ── Main ───────────────────────────────────────────────────────────────────
def main():
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    out_fn = f"GPHG_Prize_{args.year}.txt" if args.year else "GPHG_Prize.txt"
    if args.export and os.path.exists(out_fn):
        os.remove(out_fn)
        logging.info("Removed existing %s", out_fn)

    all_rows, all_spec_keys = [], set()

    # Optional: scrape current-year competing watches first
    if args.participants:
        part_url = args.participants_url
        logging.info("Scraping 2025 competing watches: %s", part_url)
        try:
            p_links = extract_participants_2025_links(part_url, args.debug)
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
            specs = extract_specs(url)
            for k in specs:
                if k.upper() not in RESERVED_UPPER:
                    all_spec_keys.add(k)
            row = {
                "Year": 2025,
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
            logging.info("Completed participants (2025).")

    archive = find_archive_links()
    years = [args.year] if args.year else sorted(archive, reverse=True)

    for year in years:
        pages = find_list_pages(archive[year], year, args.debug)

        details = {}
        for code in LIST_DEFS:
            url = pages.get(code)
            if url:
                logging.info("Starting fetch for %s watches : %s", code, url)
                details[code] = extract_detail_links(url, code, args.debug)
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
                specs = extract_specs(url)
                for k in specs:
                    if k.upper() not in RESERVED_UPPER:  # ← new, case-insensitive
                        all_spec_keys.add(k)
                row = {
                    "Year": year,
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

    # ── Update & persist master header ──
    master = load_master()
    fresh_keys = all_spec_keys - master
    if fresh_keys:
        for k in sorted(fresh_keys):
            logging.info("NEW FIELD → %s", k)
        master.update(fresh_keys)
        save_master(master)
    header = FIXED_FRONT + sorted(master) + FIXED_BACK

    # ── Write file ──
    with open(out_fn, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header, delimiter="|",
                           quotechar='"', quoting=csv.QUOTE_ALL,
                           extrasaction="ignore")
        w.writeheader()
        for r in all_rows:
            r = {k: v for k, v in r.items() if k != "URL"}
            w.writerow(r)

    # ── Tab-scrub ──
    with open(out_fn, "r+", encoding="utf-8") as f:
        txt = f.read()
        if "\t" in txt:
            f.seek(0); f.write(txt.replace("\t", " ")); f.truncate()
            logging.info("Cleaned tabs → spaces in %s", out_fn)
        else:
            logging.info("No tab characters found in %s", out_fn)

    logging.info("Written %s rows → %s", len(all_rows), out_fn)

# ────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    main()
