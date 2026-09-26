"""Clinical context layer: DailyMed drug labels + CTCAE v6.0 reference import.

DailyMed (NLM) structured product labels are fetched per regimen drug, cached on disk, and scanned
for CTCAE terms in the Boxed Warning, Warnings and Precautions and Adverse Reactions sections. The
result tells the analysis which CTCAE terms a drug's label mentions, with the quoted sentence.

The official NCI CTCAE v6.0 workbook (.xlsx) can be imported so the stored grading text shown to
clinicians is the published v6.0 wording. Both are *context*: they never set a grade or decide.

CLI:
    python3 -m backend.context refresh-dailymed cisplatin paclitaxel
    python3 -m backend.context import-ctcae ~/Downloads/CTCAE_v6.0.xlsx
    python3 -m backend.context status
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import reference

ROOT = Path(__file__).resolve().parents[1]
CONTEXT_DIR = Path(os.environ.get("ONCOCARE_CONTEXT_DIR", ROOT / "data" / "context"))
DAILYMED_DIR = CONTEXT_DIR / "dailymed"
CTCAE_FILE = CONTEXT_DIR / "ctcae_v6.json"
DAILYMED_BASE = "https://dailymed.nlm.nih.gov/dailymed/services/v2"
SPL_NS = "{urn:hl7-org:v3}"
SECTION_CODES = {
    "34066-1": "Boxed Warning",
    "43685-7": "Warnings and Precautions",
    "34071-1": "Warnings",
    "34084-4": "Adverse Reactions",
}
SECTION_WEIGHT = {"Boxed Warning": 3, "Warnings and Precautions": 2, "Warnings": 2, "Adverse Reactions": 1}

# Label vocabulary -> CTCAE term. Matching is case-insensitive on word boundaries.
LABEL_SYNONYMS: dict[str, list[str]] = {
    "Nausea": ["nausea"],
    "Vomiting": ["vomiting", "emesis"],
    "Diarrhea": ["diarrhea", "diarrhoea"],
    "Constipation": ["constipation"],
    "Mucositis oral": ["stomatitis", "mucositis", "mouth ulcer"],
    "Anorexia": ["anorexia", "decreased appetite", "loss of appetite"],
    "Dysgeusia": ["dysgeusia", "taste disturbance", "altered taste"],
    "Fatigue": ["fatigue", "asthenia"],
    "Fever": ["pyrexia", "fever"],
    "Edema limbs": ["peripheral edema", "edema"],
    "Infusion related reaction": ["infusion reaction", "infusion-related reaction", "hypersensitivity reaction", "anaphylactic", "anaphylaxis"],
    "Febrile neutropenia": ["febrile neutropenia"],
    "Peripheral sensory neuropathy": ["peripheral neuropathy", "neuropathy", "paresthesia", "neurotoxicity"],
    "Peripheral motor neuropathy": ["motor neuropathy", "weakness of extremities"],
    "Hearing impaired": ["ototoxicity", "hearing loss", "hearing impairment", "deafness"],
    "Tinnitus": ["tinnitus"],
    "Alopecia": ["alopecia"],
    "Palmar-plantar erythrodysesthesia syndrome": ["palmar-plantar", "hand-foot syndrome"],
    "Rash maculo-papular": ["rash", "dermatitis"],
    "Myalgia": ["myalgia"],
    "Arthralgia": ["arthralgia"],
    "Acute kidney injury": ["acute kidney injury", "acute renal failure", "renal failure"],
    "Chronic kidney disease": ["renal insufficiency", "decreased creatinine clearance", "renal impairment"],
    "Creatinine increased": ["nephrotoxicity", "serum creatinine", "creatinine increased", "renal toxicity", "blood urea nitrogen"],
    "Cystitis noninfective": ["hemorrhagic cystitis", "cystitis"],
    "Ejection fraction decreased": ["cardiomyopathy", "ejection fraction", "congestive heart failure", "cardiotoxicity"],
    "Pneumonitis": ["pneumonitis", "interstitial lung disease", "pulmonary toxicity"],
    "Neutrophil count decreased": ["neutropenia", "neutrophil", "myelosuppression", "bone marrow suppression"],
    "Platelet count decreased": ["thrombocytopenia", "platelet", "myelosuppression", "bone marrow suppression"],
    "White blood cell decreased": ["leukopenia", "leucopenia", "myelosuppression", "bone marrow suppression"],
    "Anemia": ["anemia", "anaemia", "myelosuppression", "bone marrow suppression"],
    "Hypomagnesemia": ["hypomagnesemia", "magnesium"],
    "Hypokalemia": ["hypokalemia"],
    "Hyperkalemia": ["hyperkalemia"],
    "Hyponatremia": ["hyponatremia", "inappropriate antidiuretic", "siadh"],
    "Hypocalcemia": ["hypocalcemia"],
    "Hypercalcemia": ["hypercalcemia"],
    "Hyperuricemia": ["hyperuricemia", "uric acid"],
    "Hypoalbuminemia": ["hypoalbuminemia"],
    "Alanine aminotransferase increased": ["alt increased", "alanine aminotransferase", "transaminase", "hepatotoxicity", "liver enzyme"],
    "Aspartate aminotransferase increased": ["ast increased", "aspartate aminotransferase", "transaminase", "hepatotoxicity", "liver enzyme"],
    "Blood bilirubin increased": ["hyperbilirubinemia", "bilirubin"],
    "Alkaline phosphatase increased": ["alkaline phosphatase"],
}

_fetch_lock = threading.Lock()
_recent_failures: dict[str, float] = {}
_in_flight: set[str] = set()


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "drug"


def _http_get(url: str, timeout: float = 15) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "ThraviOncoCare/0.3 (prototype; contact: local)"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


# ---------------------------------------------------------------------------
# DailyMed
# ---------------------------------------------------------------------------

def parse_spl(xml_bytes: bytes) -> dict[str, str]:
    """Return {section title: plain text} for the label sections we use."""
    root = ET.fromstring(xml_bytes)
    sections: dict[str, str] = {}
    for section in root.iter(f"{SPL_NS}section"):
        code = section.find(f"{SPL_NS}code")
        name = SECTION_CODES.get(code.get("code")) if code is not None else None
        if not name:
            continue
        text = " ".join(" ".join(section.itertext()).split())
        if name in sections:
            if text in sections[name]:
                continue
            text = sections[name] + " " + text
        sections[name] = text
    title = root.find(f"{SPL_NS}title")
    if title is not None:
        sections["_title"] = " ".join(" ".join(title.itertext()).split())
    return sections


def _snippet(text: str, start: int, end: int, width: int = 170) -> str:
    left = max(text.rfind(". ", 0, start) + 2, start - width, 0)
    right_candidates = [p for p in (text.find(". ", end), text.find("; ", end)) if p != -1]
    right = min(min(right_candidates) + 1 if right_candidates else len(text), end + width)
    snippet = text[left:right].strip()
    return ("…" if left > 0 and text[left - 2:left] != ". " else "") + snippet + ("…" if right < len(text) and not snippet.endswith(".") else "")


def map_label_to_ctcae(sections: dict[str, str]) -> dict[str, dict[str, Any]]:
    mapped: dict[str, dict[str, Any]] = {}
    for term, phrases in LABEL_SYNONYMS.items():
        for section, text in sections.items():
            if section.startswith("_"):
                continue
            lower = text.lower()
            for phrase in phrases:
                match = re.search(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])", lower)
                if not match:
                    continue
                entry = mapped.setdefault(term, {"sections": [], "snippets": [], "boxed": False})
                if section not in entry["sections"]:
                    entry["sections"].append(section)
                    if len(entry["snippets"]) < 2:
                        entry["snippets"].append({"section": section, "phrase": phrase, "text": _snippet(text, match.start(), match.end())})
                if section == "Boxed Warning":
                    entry["boxed"] = True
                break
    for entry in mapped.values():
        entry["sections"].sort(key=lambda s: -SECTION_WEIGHT.get(s, 0))
        entry["snippets"].sort(key=lambda s: -SECTION_WEIGHT.get(s["section"], 0))
    return mapped


def _published(value: str | None) -> str:
    """DailyMed returns dates like 'Jul 06, 2026'; normalise to ISO so they sort correctly."""
    if not value:
        return ""
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _choose_spl(drug: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not items:
        return None
    key = drug.lower()

    def score(item: dict[str, Any]) -> tuple:
        title = (item.get("title") or "").lower()
        return (key in title, "injection" in title or "for injection" in title, _published(item.get("published_date")))
    return sorted(items, key=score, reverse=True)[0]


def fetch_dailymed(drug: str, getter: Callable[[str], bytes] = _http_get) -> dict[str, Any]:
    canonical = reference.normalise_drug(drug) or drug.strip().title()
    query = urllib.parse.urlencode({"drug_name": canonical, "pagesize": 50})
    listing = json.loads(getter(f"{DAILYMED_BASE}/spls.json?{query}"))
    spl = _choose_spl(canonical, listing.get("data", []))
    if not spl:
        raise LookupError(f"No DailyMed label found for {canonical}")
    setid = spl["setid"]
    sections = parse_spl(getter(f"{DAILYMED_BASE}/spls/{setid}.xml"))
    context = {
        "drug": canonical, "setid": setid, "title": spl.get("title") or sections.get("_title"),
        "spl_version": spl.get("spl_version"), "published_date": _published(spl.get("published_date")) or spl.get("published_date"),
        "url": f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={setid}",
        "fetched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "sections": {k: len(v) for k, v in sections.items() if not k.startswith("_")},
        "terms": map_label_to_ctcae(sections),
        "source": "DailyMed (U.S. National Library of Medicine) structured product label",
    }
    DAILYMED_DIR.mkdir(parents=True, exist_ok=True)
    (DAILYMED_DIR / f"{_slug(canonical)}.json").write_text(json.dumps(context, indent=1))
    return context


def cached_dailymed(drug: str) -> dict[str, Any] | None:
    canonical = reference.normalise_drug(drug) or drug.strip().title()
    path = DAILYMED_DIR / f"{_slug(canonical)}.json"
    if path.is_file():
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
    return None


def refresh_dailymed(drugs: list[str], getter: Callable[[str], bytes] = _http_get) -> list[dict[str, Any]]:
    results = []
    for drug in drugs:
        try:
            context = fetch_dailymed(drug, getter)
            _recent_failures.pop(_slug(drug), None)
            results.append({"drug": drug, "ok": True, "title": context["title"], "terms": len(context["terms"]), "setid": context["setid"]})
        except Exception as error:
            _recent_failures[_slug(drug)] = time.time()
            results.append({"drug": drug, "ok": False, "error": f"{type(error).__name__}: {str(error)[:160]}"})
    return results


def prefetch_async(drugs: list[str]) -> None:
    """Fetch missing labels in the background (at most one attempt per drug every 10 minutes)."""
    if os.environ.get("ONCOCARE_DAILYMED", "auto").lower() == "off":
        return
    todo = []
    with _fetch_lock:
        for drug in drugs:
            slug = _slug(reference.normalise_drug(drug) or drug)
            if cached_dailymed(drug) or slug in _in_flight or time.time() - _recent_failures.get(slug, 0) < 600:
                continue
            _in_flight.add(slug)
            todo.append((drug, slug))
    if not todo:
        return

    def run() -> None:
        for drug, slug in todo:
            try:
                refresh_dailymed([drug])
            finally:
                _in_flight.discard(slug)
    threading.Thread(target=run, daemon=True).start()


def dailymed_for_drugs(drugs: list[str]) -> dict[str, dict[str, Any]]:
    out = {}
    for drug in drugs:
        context = cached_dailymed(drug)
        if context:
            out[drug] = context
    return out


# ---------------------------------------------------------------------------
# CTCAE v6.0 workbook import
# ---------------------------------------------------------------------------

def _xlsx_rows(path: Path) -> list[list[list[str]]]:
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{ns}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{ns}t")))
        sheets = sorted(n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
        workbook = []
        for sheet in sheets:
            rows = []
            for row in ET.fromstring(z.read(sheet)).iter(f"{ns}row"):
                cells: dict[int, str] = {}
                for c in row.iter(f"{ns}c"):
                    ref = c.get("r", "")
                    letters = re.match(r"[A-Z]+", ref)
                    col = 0
                    for ch in (letters.group(0) if letters else "A"):
                        col = col * 26 + ord(ch) - 64
                    value = c.find(f"{ns}v")
                    inline = c.find(f"{ns}is")
                    if c.get("t") == "s" and value is not None:
                        text = shared[int(value.text)]
                    elif inline is not None:
                        text = "".join(t.text or "" for t in inline.iter(f"{ns}t"))
                    else:
                        text = value.text if value is not None else ""
                    cells[col - 1] = (text or "").strip()
                if cells:
                    rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
            workbook.append(rows)
    return workbook


def import_ctcae_workbook(path: str | Path, version: str = "6.0") -> dict[str, Any]:
    terms: dict[str, dict[str, Any]] = {}
    for rows in _xlsx_rows(Path(path)):
        header_index = None
        for i, row in enumerate(rows[:15]):
            lowered = [c.lower() for c in row]
            if any("ctcae term" in c for c in lowered) and any(c.startswith("grade 1") for c in lowered):
                header_index = i
                break
        if header_index is None:
            continue
        header = [c.lower() for c in rows[header_index]]
        col = lambda pred: next((i for i, c in enumerate(header) if pred(c)), None)  # noqa: E731
        term_col = col(lambda c: "ctcae term" in c)
        soc_col = col(lambda c: "soc" in c or "system organ class" in c)
        code_col = col(lambda c: "meddra code" in c or c == "meddra code")
        def_col = col(lambda c: "definition" in c)
        grade_cols = {g: col(lambda c, g=g: c.startswith(f"grade {g}")) for g in range(1, 6)}
        for row in rows[header_index + 1:]:
            get = lambda i: row[i].strip() if i is not None and i < len(row) else ""  # noqa: E731
            term = get(term_col)
            if not term:
                continue
            grades = {g: get(i) for g, i in grade_cols.items() if get(i) and get(i) not in ("-", "–", "—")}
            if not grades:
                continue
            terms[term] = {"soc": get(soc_col), "meddra_code": get(code_col), "definition": get(def_col), "grades": grades}
    if not terms:
        raise ValueError("No CTCAE rows found. Expected a sheet with 'CTCAE Term' and 'Grade 1'..'Grade 5' columns.")
    payload = {"version": version, "source_file": Path(path).name,
               "imported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "terms": terms}
    CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    CTCAE_FILE.write_text(json.dumps(payload, indent=1))
    apply_imported_ctcae()
    return {"version": version, "terms": len(terms), "file": str(CTCAE_FILE)}


TERM_RENAMES = {"Mucositis oral": ["Oral mucositis"], "Rash maculo-papular": ["Maculopapular rash"]}


def apply_imported_ctcae() -> int:
    """Overlay imported v6.0 criteria text onto the in-memory reference. Returns number of terms replaced."""
    if not CTCAE_FILE.is_file():
        return 0
    try:
        data = json.loads(CTCAE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return 0
    imported = data.get("terms", {})
    label = f"CTCAE v{data.get('version', '6.0')} (NCI workbook {data.get('source_file', '')}, imported {data.get('imported_at', '')[:10]})"
    replaced = 0
    for term, info in reference.CTCAE_TERMS.items():
        source = imported.get(term) or next((imported[n] for n in TERM_RENAMES.get(term, []) if n in imported), None)
        if not source:
            continue
        new_grades = {int(g): text for g, text in source["grades"].items()}
        builtin = info.setdefault("builtin_grades", dict(info["grades"]))
        norm = lambda d: {g: re.sub(r"\W+", "", t.lower()) for g, t in d.items()}  # noqa: E731
        if info.get("kind") == "lab":
            info["threshold_check"] = "matches" if norm(new_grades) == norm(builtin) else "differs"
        info["grades"] = new_grades
        info["source"] = label
        replaced += 1
    reference.CTCAE_VERSION = str(data.get("version", "6.0"))
    return replaced


def context_status() -> dict[str, Any]:
    labels = []
    if DAILYMED_DIR.is_dir():
        for path in sorted(DAILYMED_DIR.glob("*.json")):
            try:
                c = json.loads(path.read_text())
                labels.append({"drug": c["drug"], "title": c.get("title"), "fetched_at": c.get("fetched_at"), "terms": len(c.get("terms", {})), "url": c.get("url")})
            except (OSError, json.JSONDecodeError, KeyError):
                continue
    imported = None
    if CTCAE_FILE.is_file():
        try:
            data = json.loads(CTCAE_FILE.read_text())
            imported = {"version": data.get("version"), "terms": len(data.get("terms", {})), "source_file": data.get("source_file"), "imported_at": data.get("imported_at")}
        except (OSError, json.JSONDecodeError):
            imported = None
    verified = sum(1 for t in reference.CTCAE_TERMS if reference.term_provenance(t).startswith("CTCAE v6"))
    return {"ctcae": {"version": reference.CTCAE_VERSION, "imported_workbook": imported, "terms_with_v6_text": verified,
                      "terms_total": len(reference.CTCAE_TERMS)},
            "dailymed": {"labels": labels, "enabled": os.environ.get("ONCOCARE_DAILYMED", "auto").lower() != "off"}}


apply_imported_ctcae()


def _main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "import-ctcae":
        print(json.dumps(import_ctcae_workbook(argv[1]), indent=1))
        return 0
    if argv and argv[0] == "refresh-dailymed":
        drugs = argv[1:] or [d for d in reference.DRUG_TOXICITIES]
        print(json.dumps(refresh_dailymed(drugs), indent=1))
        return 0
    if argv and argv[0] == "status":
        print(json.dumps(context_status(), indent=1))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
