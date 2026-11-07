"""Lab report extraction.

Two extractors sit behind one interface:

* ``claude`` – used when ANTHROPIC_API_KEY is set. Sends the PDF/image to the Claude Messages API
  and asks for structured JSON. Model is configurable with ONCOCARE_EXTRACTION_MODEL.
* ``rules`` – always available. Reads the text layer of text files and simple PDFs and matches the
  stored lab catalog aliases line by line.

Both only ever produce *unverified candidates*; a clinician must confirm each value.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
import zlib
from datetime import date
from typing import Any

from .reference import LAB_CATALOG, normalise_unit, to_canonical

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+"


# ---------------------------------------------------------------------------
# Text layer
# ---------------------------------------------------------------------------

def _pdf_literal(raw: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == 0x5C and i + 1 < len(raw):  # backslash escape
            nxt = raw[i + 1]
            mapping = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12, ord("("): 40, ord(")"): 41, 0x5C: 0x5C}
            if nxt in mapping:
                out.append(mapping[nxt]); i += 2; continue
            if 48 <= nxt <= 55:
                octal = re.match(rb"[0-7]{1,3}", raw[i + 1:i + 4]).group(0)
                out.append(int(octal, 8) & 0xFF); i += 1 + len(octal); continue
            i += 1; continue
        out.append(ch); i += 1
    return out.decode("latin-1")


def _pdf_text_items(content: bytes) -> list[tuple[float, float, str]]:
    """Return (y, x, text) for every text-showing operator, tracking Tm/Td/TD/T*/TL positions."""
    items: list[tuple[float, float, str]] = []
    token = re.compile(rb"\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f\s]*>|\[|\]|T\*|Td|TD|Tm|TL|Tj|TJ|'|\"|ET|BT|-?\d*\.?\d+")
    numbers: list[float] = []
    pending: list[str] = []
    line_x = line_y = 0.0
    leading = 0.0
    in_array = False
    for match in token.finditer(content):
        tok = match.group(0)
        if tok.startswith(b"("):
            pending.append(_pdf_literal(tok[1:-1]))
        elif tok.startswith(b"<"):
            hexdata = re.sub(rb"\s", b"", tok[1:-1])
            try:
                decoded = bytes.fromhex(hexdata.decode()).decode("latin-1")
                if decoded.isprintable():
                    pending.append(decoded)
            except ValueError:
                pass
        elif tok == b"[":
            in_array = True
        elif tok == b"]":
            in_array = False
        elif re.fullmatch(rb"-?\d*\.?\d+", tok):
            if in_array:
                # large negative kerning inside TJ arrays usually means a visual space
                if float(tok) < -200 and pending:
                    pending.append(" ")
            else:
                numbers.append(float(tok))
        elif tok == b"BT":
            line_x = line_y = 0.0
            numbers.clear()
        elif tok == b"Tm" and len(numbers) >= 6:
            line_x, line_y = numbers[-2], numbers[-1]
            numbers.clear()
        elif tok in (b"Td", b"TD") and len(numbers) >= 2:
            line_x += numbers[-2]
            line_y += numbers[-1]
            if tok == b"TD":
                leading = -numbers[-1]
            numbers.clear()
        elif tok == b"TL" and numbers:
            leading = numbers[-1]
            numbers.clear()
        elif tok == b"T*":
            line_y -= leading
        elif tok in (b"Tj", b"TJ", b"'", b'"'):
            if tok in (b"'", b'"'):
                line_y -= leading
            text = "".join(pending)
            if text.strip():
                items.append((line_y, line_x, text))
            pending = []
            numbers.clear()
        else:
            numbers.clear()
    return items


def _pdf_text_from_content(content: bytes) -> str:
    items = _pdf_text_items(content)
    rows: list[tuple[float, list[tuple[float, str]]]] = []
    for y, x, text in sorted(items, key=lambda i: (-i[0], i[1])):
        if rows and abs(rows[-1][0] - y) <= 2.5:
            rows[-1][1].append((x, text))
        else:
            rows.append((y, [(x, text)]))
    return "\n".join("   ".join(t for _, t in sorted(cells)) for _, cells in rows)


def _decode_stream(header: bytes, body: bytes) -> bytes | None:
    filters = re.findall(rb"/(ASCII85Decode|A85|FlateDecode|Fl|ASCIIHexDecode|AHx|[A-Za-z0-9]+Decode)", header.split(b"/Filter", 1)[1]) if b"/Filter" in header else []
    for name in filters:
        try:
            if name in (b"ASCII85Decode", b"A85"):
                raw = body.strip()
                raw = raw[2:] if raw.startswith(b"<~") else raw
                raw = raw[:-2] if raw.endswith(b"~>") else raw
                body = base64.a85decode(raw)
            elif name in (b"FlateDecode", b"Fl"):
                body = zlib.decompress(body)
            elif name in (b"ASCIIHexDecode", b"AHx"):
                body = bytes.fromhex(re.sub(rb"[^0-9A-Fa-f]", b"", body).decode())
            else:
                return None  # DCT/JBIG2 etc: images, not text
        except Exception:
            return None
    return body


def pdf_text(data: bytes) -> str:
    chunks = []
    for match in re.finditer(rb"stream\r?\n", data):
        begin = match.end()
        if data[match.start() - 3:match.start()] == b"end":
            continue
        finish = data.find(b"endstream", begin)
        if finish < 0:
            continue
        obj_start = data.rfind(b"obj", 0, match.start())
        header = data[obj_start:match.start()]
        body = data[begin:finish].rstrip(b"\r\n")
        decoded = _decode_stream(header, body)
        if decoded and b"BT" in decoded and (b"Tj" in decoded or b"TJ" in decoded):
            chunks.append(_pdf_text_from_content(decoded))
    return "\n".join(chunk for chunk in chunks if chunk)


def extract_text(data: bytes, content_type: str, filename: str) -> tuple[str, str]:
    name = filename.lower()
    if content_type.startswith("text/") or name.endswith((".txt", ".csv", ".tsv")):
        for encoding in ("utf-8", "latin-1"):
            try:
                return data.decode(encoding), "text file"
            except UnicodeDecodeError:
                continue
    if content_type == "application/pdf" or name.endswith(".pdf"):
        return pdf_text(data), "PDF text layer"
    return "", "image (no text layer)"


# ---------------------------------------------------------------------------
# Rule-based parser
# ---------------------------------------------------------------------------

def parse_date(text: str) -> str | None:
    text = text.strip()
    patterns = [
        (r"(\d{4})-(\d{1,2})-(\d{1,2})", lambda m: (int(m[1]), int(m[2]), int(m[3]))),
        (r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", lambda m: (int(m[3]), int(m[2]), int(m[1]))),  # day-first
        (r"(\d{1,2})[\s-]([A-Za-z]{3})[A-Za-z]*[\s,-]+(\d{4})", lambda m: (int(m[3]), MONTHS.get(m[2].lower()[:3], 0), int(m[1]))),
        (r"([A-Za-z]{3})[A-Za-z]*\s+(\d{1,2}),?\s+(\d{4})", lambda m: (int(m[3]), MONTHS.get(m[1].lower()[:3], 0), int(m[2]))),
    ]
    for pattern, build in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                year, month, day = build(match)
                return date(year, month, day).isoformat()
            except ValueError:
                continue
    return None


def find_collection_date(text: str) -> str | None:
    for line in text.splitlines():
        if re.search(r"collect|sample|specimen|drawn|received|report(ed)?\s*(on|date)|\bdate\b", line, re.I):
            found = parse_date(line)
            if found:
                return found
    return None


def _alias_patterns() -> list[tuple[str, str, re.Pattern[str]]]:
    patterns = []
    for test_name, info in LAB_CATALOG.items():
        for alias in info["aliases"] + [test_name.lower()]:
            patterns.append((test_name, alias, re.compile(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])")))
    patterns.sort(key=lambda item: -len(item[1]))
    return patterns


ALIAS_PATTERNS = _alias_patterns()
EXCLUDE = re.compile(r"indirect|%|percent|ratio|hba1c|mchc|\\bmch\\b|\\bmcv\\b|clearance|a/g")
UNIT_RE = re.compile(r"^\s*((?:x\s*)?(?:10\s*[\^*e]?\s*\d+|10³|lakhs?)?\s*/?\s*(?:[a-zµμ]+/)?[a-zµμ]+(?:\d)?|/[a-zµμ]+\d?)", re.I)


def parse_lab_text(text: str) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower().replace("\t", " ")
        for test_name, alias, pattern in ALIAS_PATTERNS:
            match = pattern.search(lower)
            if not match:
                continue
            if EXCLUDE.search(lower) and not (test_name == "ANC" and "absolute" in lower):
                break
            rest = re.sub(r"\([^)]*\)", " ", line[match.end():])
            rest = re.sub(r"^[\s:=,|-]+", "", rest)
            number = re.match(r"(?:[HLhl*]\s+)?(" + NUMBER + r")\s*(?:[HLhl*](?![a-z]))?", rest)
            if not number:
                break
            value = float(number.group(1).replace(",", ""))
            after = rest[number.end():]
            unit_match = UNIT_RE.match(after)
            unit = unit_match.group(1).strip() if unit_match and re.search(r"[a-zµμ/]", unit_match.group(1), re.I) else ""
            if unit_match and unit:
                after = after[unit_match.end():]
            ref = re.search(r"(" + NUMBER + r")\s*[-–to]+\s*(" + NUMBER + r")", after)
            ref_low = float(ref.group(1).replace(",", "")) if ref else None
            ref_high = float(ref.group(2).replace(",", "")) if ref else None
            candidate = {"test_name": test_name, "value": value, "unit": unit or LAB_CATALOG[test_name]["unit"],
                         "unit_missing": not unit,
                         "ref_low": ref_low, "ref_high": ref_high, "raw_line": line[:200], "alias_length": len(alias)}
            existing = found.get(test_name)
            if existing is None or len(alias) > existing["alias_length"]:
                found[test_name] = candidate
            break
    return list(found.values())


def to_canonical_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Convert units to the catalog's canonical unit so trends stay comparable."""
    test_name = candidate["test_name"]
    catalog = LAB_CATALOG.get(test_name)
    if not catalog:
        return candidate
    if normalise_unit(candidate["unit"]) == normalise_unit(catalog["unit"]):
        candidate["unit"] = catalog["unit"]
        return candidate
    converted, problem = to_canonical(test_name, candidate["value"], candidate["unit"])
    if converted is None:
        candidate["note"] = problem
        return candidate
    factor = converted / candidate["value"] if candidate["value"] else 1
    if abs(factor - 1) < 1e-9:
        candidate["unit"] = catalog["unit"]
        return candidate
    candidate["note"] = f"Converted from {candidate['value']:g} {candidate['unit']}"
    candidate["value"] = round(converted, 3)
    candidate["unit"] = catalog["unit"]
    if candidate.get("ref_low") is not None:
        candidate["ref_low"] = round(candidate["ref_low"] * factor, 3)
    if candidate.get("ref_high") is not None:
        candidate["ref_high"] = round(candidate["ref_high"] * factor, 3)
    return candidate


# ---------------------------------------------------------------------------
# Optional Claude extractor
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "claude-sonnet-5"
SUPPORTED_MODELS = ["claude-sonnet-5", "claude-haiku-4-5-20251001", "claude-opus-5-5"]
ENV_FILE = Path(os.environ.get("ONCOCARE_ENV_FILE", Path(__file__).resolve().parents[1] / ".env"))


SAMPLING_PARAMS = ("temperature", "top_p", "top_k")


def _anthropic_post(api_key: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    """POST to the Messages API. Newer models reject sampling parameters, so we never send them and, if a caller
    does, retry once without them when the API says they are not supported."""
    base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    request = urllib.request.Request(f"{base}/v1/messages", data=json.dumps(body).encode(), method="POST", headers={
        "content-type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read()).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        sent = [p for p in SAMPLING_PARAMS if p in body]
        if error.code == 400 and sent and any(p in detail for p in sent):
            return _anthropic_post(api_key, {k: v for k, v in body.items() if k not in SAMPLING_PARAMS}, timeout)
        raise RuntimeError(f"Claude API HTTP {error.code}: {detail or error.reason}") from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"Cannot reach api.anthropic.com ({error.reason})") from None
    except OSError as error:  # a timeout waiting for the reply is not wrapped in URLError
        raise RuntimeError(f"No reply from api.anthropic.com ({error or type(error).__name__})") from None


def test_ai(api_key: str, model: str) -> dict[str, Any]:
    reply = _anthropic_post(api_key, {"model": model, "max_tokens": 5, "messages": [{"role": "user", "content": "Reply with OK."}]}, 30)
    return {"ok": True, "model": reply.get("model", model)}


def save_ai_settings(api_key: str | None, model: str | None) -> dict[str, Any]:
    """Validate the key with a tiny request, then persist to .env and the running process."""
    key = (api_key or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    model = (model or os.environ.get("ONCOCARE_EXTRACTION_MODEL") or DEFAULT_MODEL).strip()
    if not key:
        raise ValueError("Enter an Anthropic API key")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{20,200}", key):
        raise ValueError("That does not look like an Anthropic API key")
    test_ai(key, model)
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.is_file() else []
    updates = {"ANTHROPIC_API_KEY": key, "ONCOCARE_EXTRACTION_MODEL": model, "ONCOCARE_EXTRACTION": "auto"}
    kept = [line for line in lines if line.split("=", 1)[0].strip() not in updates]
    kept += [f"{k}={v}" for k, v in updates.items()]
    ENV_FILE.write_text("\n".join(kept) + "\n")
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass
    os.environ.update(updates)
    return ai_status()


def ai_status() -> dict[str, Any]:
    mode = os.environ.get("ONCOCARE_EXTRACTION", "auto").lower()
    enabled = bool(os.environ.get("ANTHROPIC_API_KEY")) and mode != "rules"
    key = os.environ.get("ANTHROPIC_API_KEY") or ""
    return {"enabled": enabled, "provider": "Anthropic Claude" if enabled else None, "key_hint": f"…{key[-4:]}" if key else None,
            "models": SUPPORTED_MODELS,
            "model": os.environ.get("ONCOCARE_EXTRACTION_MODEL", DEFAULT_MODEL) if enabled else None,
            "hint": None if enabled else "Set ANTHROPIC_API_KEY in .env (see .env.example) and restart the server to enable AI extraction."}


def claude_extract(data: bytes, content_type: str, text: str) -> tuple[list[dict[str, Any]], str | None]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    model = os.environ.get("ONCOCARE_EXTRACTION_MODEL", DEFAULT_MODEL)
    tests = "\n".join(f"- {name} (canonical unit {info['unit']}; also printed as: {', '.join(info['aliases'])})" for name, info in LAB_CATALOG.items())
    instruction = (
        "You are extracting structured values from a laboratory report (CBC/CBP, LFT, RFT and electrolytes) "
        "for clinician verification. Return ONLY a JSON object, no prose:\n"
        '{"collection_date": "YYYY-MM-DD" or null, "results": [{"test_name": <one of the names below>, "value": <number>, '
        '"unit": <unit exactly as printed>, "ref_low": <number or null>, "ref_high": <number or null>, "printed_name": <label as printed>}]}\n'
        "Rules: use the sample collection date (dates are usually DD/MM/YYYY); copy numbers exactly as printed (remove thousands separators); "
        "keep the printed unit, do not convert; skip percentages of differential counts and any test not listed; "
        "never guess a value you cannot read — omit it instead.\nAllowed test names:\n" + tests
    )
    if content_type == "application/pdf":
        source: dict[str, Any] = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(data).decode()}}
    elif content_type in ("image/png", "image/jpeg", "image/webp", "image/gif"):
        source = {"type": "image", "source": {"type": "base64", "media_type": content_type, "data": base64.b64encode(data).decode()}}
    else:
        source = {"type": "text", "text": text[:50000]}
    payload = _anthropic_post(api_key, {"model": model, "max_tokens": 4000,
                                        "messages": [{"role": "user", "content": [source, {"type": "text", "text": instruction}]}]}, 120)
    reply = "".join(part.get("text", "") for part in payload.get("content", []))
    match = re.search(r"\{.*\}", reply, re.S)
    parsed = json.loads(match.group(0)) if match else {"results": []}
    results = []
    for item in parsed.get("results", []):
        name = item.get("test_name")
        value = item.get("value")
        if isinstance(value, str):
            try:
                value = float(value.replace(",", ""))
            except ValueError:
                continue
        if name in LAB_CATALOG and isinstance(value, (int, float)):
            results.append({"test_name": name, "value": float(value), "unit": item.get("unit") or LAB_CATALOG[name]["unit"],
                            "ref_low": item.get("ref_low"), "ref_high": item.get("ref_high"),
                            "raw_line": f"AI read: {item.get('printed_name') or name}"})
    return results, parsed.get("collection_date")


def extract_candidates(data: bytes, content_type: str, filename: str) -> dict[str, Any]:
    text, text_source = extract_text(data, content_type, filename)
    method, note = "rules", None
    candidates: list[dict[str, Any]] = []
    report_date = find_collection_date(text) if text else None
    rule_candidates = [to_canonical_candidate(c) for c in parse_lab_text(text)] if text else []
    if ai_status()["enabled"]:
        try:
            candidates, claude_date = claude_extract(data, content_type, text)
            report_date = parse_date(claude_date) if claude_date else report_date
            method = "claude"
        except Exception as error:  # fall back to rules; never block the upload
            note = f"AI extraction failed ({type(error).__name__}: {str(error)[:120]}); used the text-layer parser."
    if method == "rules":
        candidates = rule_candidates
        if not text:
            note = (note + " " if note else "") + (
                "No text layer found (scanned image or photo). Enable AI extraction (ANTHROPIC_API_KEY) or enter values manually.")
    else:
        candidates = [to_canonical_candidate(c) for c in candidates]
        # Independent cross-check against the text layer when one exists.
        by_test = {c["test_name"]: c for c in rule_candidates}
        for candidate in candidates:
            other = by_test.get(candidate["test_name"])
            if other and abs(other["value"] - candidate["value"]) > max(1e-6, abs(candidate["value"]) * 0.001):
                candidate["note"] = (candidate.get("note") or "") + f" Cross-check: text layer reads {other['value']:g} {other['unit']} — confirm."
            elif other:
                candidate["note"] = (candidate.get("note") or "") + " Cross-checked with text layer."
    for candidate in candidates:
        candidate.pop("alias_length", None)
        if candidate.pop("unit_missing", False):
            candidate["note"] = (candidate.get("note") or "") + " Unit not printed; catalog unit assumed — check."
    return {"candidates": candidates, "report_date": report_date, "collection_date": report_date, "method": method,
            "model": ai_status()["model"] if method == "claude" else None, "text_source": text_source, "note": note}
