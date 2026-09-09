from __future__ import annotations

from io import BytesIO
import hashlib
import json
import re
import unicodedata
from typing import Dict, List, Tuple, Iterable


def _text(value) -> str:
    if value is None:
        return ""
    value = str(value).replace("\u00a0", " ")
    value = value.replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\s*\n\s*", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _norm(value) -> str:
    s = _text(value).casefold()
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _slug(value) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _norm(value)).strip("-") or "profil"


def _profile_key(mode: str, name: str) -> str:
    return f"{_slug(mode)}:{_slug(name)}"


def _owner_from_text(text: str, aliases: Iterable[str] | None = None) -> str:
    lines = [x.strip() for x in (text or "").replace("\r", "").splitlines() if x.strip()]
    labels = list(aliases or []) + ["consignor/shipper", "consignor", "shipper", "owner", "lender"]
    labels = list(dict.fromkeys(_norm(x) for x in labels if _norm(x)))
    for i, line in enumerate(lines):
        n = _norm(line)
        for nl in labels:
            if n.startswith(nl):
                rest = re.sub(r"^[^:]{0,60}:\s*", "", line).strip()
                if rest and _norm(rest) != nl:
                    return _text(rest)
                if i + 1 < len(lines):
                    return _text(lines[i + 1])
    m = re.search(r"property of\s+(.+?)(?:\.|\n| and are )", text or "", re.I | re.S)
    if m:
        return _text(m.group(1))
    return ""


def _money(value) -> Tuple[str, float | None]:
    raw = _text(value)
    if not raw:
        return "", None
    cleaned = re.sub(r"[^0-9,.-]", "", raw)
    if not cleaned:
        return raw, None
    # Documents de référence : virgule = séparateur de milliers, point = décimales.
    cleaned = cleaned.replace(",", "")
    try:
        number = float(cleaned)
    except Exception:
        return raw, None
    if number.is_integer():
        return f"{int(number):,}", number
    return f"{number:,.2f}", number


def _dimensions(value, default_unit: str = "") -> str:
    s = _text(value)
    if not s:
        return ""
    s = re.sub(r"^overall\s*:\s*", "", s, flags=re.I)
    s = s.replace('"', '″').replace("”", "″").replace("“", "″")
    s = re.sub(r"\s+[xX]\s+", " × ", s)
    s = re.sub(r"(?<=\d)[xX](?=\d)", " × ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if default_unit and not re.search(r"\b(?:cm|mm|m|in|inch|inches)\b|[″']", s, re.I):
        s += f" {default_unit}"
    return s


def _license_from_note(note) -> str:
    s = _text(note)
    if not s:
        return ""
    s = re.sub(r"(?<=-)\s+(?=[A-Za-z0-9])", "", s)
    patterns = [
        r"\b\d{2}CA\d{5}/[A-Z0-9]+\b",
        r"\b\d{4}-\d{2}-\d{2}-\d{2}-\d{3}\b",
        r"\b[A-Z]{1,5}-?\d{4,}(?:/[A-Z0-9-]+)?\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, s, re.I)
        if m:
            return m.group(0)
    if re.search(r"\b(?:permit|licen[cs]e|cites)\b", s, re.I):
        return s
    return ""


def _base_row(owner: str, source_file: str, source_page: int | str, reference: str = "") -> Dict:
    return {
        "owner": owner or "",
        "title": "",
        "medium": "",
        "hs_code": "",
        "license": "",
        "dimensions": "",
        "value_usd": "",  # Jamais rempli depuis CAD sans conversion utilisateur.
        "country_origin": "",
        "reference": reference or "",
        "source_file": source_file,
        "source_page": source_page,
        "source_value": "",
        "source_value_number": None,
        "source_currency": "",
        "source_headers": {},
    }


def _profile_name(full_text: str) -> str:
    n = _norm(full_text)
    if "museum of anthropology at ubc" in n and "object id" in n:
        return "MOA - Pro Forma"
    if "herve curat" in n and "item id" in n:
        return "Hervé Curat - Pro Forma"
    if "museum of vancouver" in n:
        return "Museum of Vancouver - Pro Forma"
    if "meghann o brien" in n and "spirit of shape" in n:
        return "Meghann O'Brien - Pro Forma"
    return "Générique"


_HEADER_ALIASES = {
    "owner": ["owner", "lender", "consignor", "consignor shipper", "shipper", "proprietaire", "preteur"],
    "reference": ["object id", "catalogue number", "catalog number", "item id", "reference", "ref", "inventory number", "accession number"],
    "title": ["object name title", "object name", "title", "description", "artwork", "artwork title"],
    "medium": ["medium", "materials", "material", "technique"],
    "hs_code": ["hs code", "tariff code", "commodity code", "customs code"],
    "license": ["license", "licence", "permit", "export permit", "cites"],
    "dimensions": ["dimensions", "dims", "size", "dimensions cm"],
    "country_origin": ["country of origin", "place of origin", "origin", "pays d origine"],
    "value_source": ["value for insurance cad", "value for insurance", "value cad", "value usd", "declared value", "customs value", "value"],
}


def _aliases_from_rules(learned_rules: List[Dict] | None, profile_key: str = "") -> Dict[str, List[str]]:
    aliases = {k: list(v) for k, v in _HEADER_ALIASES.items()}
    for rule in learned_rules or []:
        if not isinstance(rule, dict) or rule.get("active") is False:
            continue
        rule_profile = _text(rule.get("profile_key"))
        if rule_profile and profile_key and rule_profile != profile_key:
            continue
        if rule_profile and not profile_key:
            continue
        target = _text(rule.get("target_field"))
        alias = _text(rule.get("source_alias"))
        if target and alias:
            aliases.setdefault(target, [])
            if _norm(alias) not in {_norm(x) for x in aliases[target]}:
                aliases[target].append(alias)
    return aliases


def _match_header(value: str, aliases_map: Dict[str, List[str]] | None = None) -> str:
    n = _norm(value)
    if not n:
        return ""
    for field, aliases in (aliases_map or _HEADER_ALIASES).items():
        for alias in aliases:
            a = _norm(alias)
            if n == a or (len(a) >= 6 and a in n):
                return field
    return ""


def _currency_from_header(header: str, full_text: str) -> str:
    n = _norm(header)
    if "usd" in n:
        return "USD"
    if "cad" in n:
        return "CAD"
    t = _norm(full_text)
    if "cad" in t and "usd" not in t:
        return "CAD"
    if "usd" in t and "cad" not in t:
        return "USD"
    return ""


def _signature_terms_from_text(full_text: str) -> List[str]:
    terms = []
    for raw in (full_text or "").replace("\r", "").splitlines()[:120]:
        txt = _text(raw)
        n = _norm(txt)
        if not n or len(n) > 90:
            continue
        if any(k in n for k in (
            "pro forma", "invoice", "consignor", "shipper", "owner", "lender",
            "object id", "item id", "object name", "title", "dimensions", "value",
            "country of origin", "place of origin", "license", "permit", "cites",
        )):
            terms.append(n)
    return terms


def _signature_terms_pdf(pdf, full_text: str) -> List[str]:
    terms = _signature_terms_from_text(full_text)
    for page in list(pdf.pages)[:4]:
        try:
            tables = page.extract_tables() or []
        except Exception:
            tables = []
        for table in tables[:4]:
            for raw in (table or [])[:5]:
                for cell in raw or []:
                    txt = _text(cell)
                    n = _norm(txt)
                    if n and 2 <= len(n) <= 80 and not re.fullmatch(r"[0-9 .,/:-]+", n):
                        terms.append(n)
    return sorted(set(terms))[:120]


def _signature_terms_xlsx(wb) -> List[str]:
    terms = []
    for ws in wb.worksheets[:4]:
        terms.append(_norm(ws.title))
        for row_no in range(1, min(ws.max_row, 15) + 1):
            for col_no in range(1, min(ws.max_column, 40) + 1):
                txt = _text(ws.cell(row_no, col_no).value)
                n = _norm(txt)
                if n and 2 <= len(n) <= 80 and not re.fullmatch(r"[0-9 .,/:-]+", n):
                    terms.append(n)
    return sorted(set(x for x in terms if x))[:120]


def _signature_hash(signature: List[str]) -> str:
    raw = "|".join(sorted(set(_norm(x) for x in signature if _norm(x))))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _decode_signature(value) -> List[str]:
    if isinstance(value, list):
        return [_norm(x) for x in value if _norm(x)]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [_norm(x) for x in parsed if _norm(x)]
        except Exception:
            return [_norm(x) for x in value.split("|") if _norm(x)]
    return []


def _signature_similarity(a: List[str], b: List[str]) -> float:
    sa = set(_norm(x) for x in a if _norm(x))
    sb = set(_norm(x) for x in b if _norm(x))
    if not sa or not sb:
        return 0.0
    # Jaccard enrichi par recouvrement du plus petit profil : robuste aux colonnes ajoutées.
    inter = len(sa & sb)
    jaccard = inter / len(sa | sb)
    coverage = inter / min(len(sa), len(sb))
    return round((jaccard * 0.45) + (coverage * 0.55), 4)


def _best_learned_profile(signature: List[str], learned_profiles: List[Dict] | None, threshold: float = 0.52):
    best = None
    best_score = 0.0
    for profile in learned_profiles or []:
        if not isinstance(profile, dict) or profile.get("active") is False:
            continue
        score = _signature_similarity(signature, _decode_signature(profile.get("signature")))
        if score > best_score:
            best = profile
            best_score = score
    if best is not None and best_score >= threshold:
        return best, best_score
    return None, best_score


def _extract_moa(pdf, owner: str, filename: str) -> List[Dict]:
    rows = []
    seen = set()
    for page_no, page in enumerate(pdf.pages, 1):
        for table in page.extract_tables() or []:
            if not table or max((len(r or []) for r in table), default=0) < 10:
                continue
            for raw in table:
                raw = list(raw or []) + [""] * 11
                ref = _text(raw[2])
                if not ref or _norm(ref) in {"object id", "object id #"}:
                    continue
                if not re.search(r"[A-Za-z0-9]", ref):
                    continue
                title = _text(raw[3])
                value = _text(raw[7])
                if not title and not value:
                    continue
                key = ref.casefold()
                if key in seen:
                    continue
                seen.add(key)
                display_value, number = _money(value)
                row = _base_row(owner, filename, page_no, ref)
                row.update({
                    "title": title,
                    "country_origin": _text(raw[5]),
                    "dimensions": _dimensions(raw[8]),
                    "license": _license_from_note(raw[10]),
                    "source_value": display_value,
                    "source_value_number": number,
                    "source_currency": "CAD",
                    "source_headers": {
                        "reference": "Object ID #", "title": "Object Name/Title",
                        "country_origin": "Place of Origin", "dimensions": "Dimensions",
                        "license": "Note", "value_source": "Value for Insurance CAD",
                    },
                })
                rows.append(row)
    return rows


def _extract_herve(pdf, owner: str, filename: str) -> List[Dict]:
    rows = []
    seen = set()
    for page_no, page in enumerate(pdf.pages, 1):
        for table in page.extract_tables() or []:
            if not table or max((len(r or []) for r in table), default=0) < 9:
                continue
            for raw in table:
                raw = list(raw or []) + [""] * 9
                seq = _text(raw[0])
                ref = _text(raw[2])
                if not seq.isdigit() or not ref or ref.casefold() in seen:
                    continue
                seen.add(ref.casefold())
                display_value, number = _money(raw[8])
                row = _base_row(owner, filename, page_no, ref)
                row.update({
                    "title": _text(raw[5]),
                    "dimensions": _dimensions(raw[3], "cm"),
                    "country_origin": _text(raw[4]),
                    "source_value": display_value,
                    "source_value_number": number,
                    "source_currency": "CAD",
                    "source_headers": {
                        "reference": "Item ID", "dimensions": "Dimensions (cm)",
                        "country_origin": "Place of Origin", "title": "Description",
                        "value_source": "Value (CAD)",
                    },
                })
                rows.append(row)
    return rows


def _extract_mov(pdf, owner: str, filename: str) -> List[Dict]:
    rows = []
    seen = set()

    def add(ref, title, value, origin, page_no):
        ref = _text(ref)
        title = _text(title)
        if not ref or not title or ref.casefold() in seen or not re.match(r"^AA\s+", ref, re.I):
            return
        seen.add(ref.casefold())
        display_value, number = _money(value)
        row = _base_row(owner, filename, page_no, ref)
        row.update({
            "title": title,
            "country_origin": _text(origin),
            "source_value": display_value,
            "source_value_number": number,
            "source_currency": "CAD",
            "source_headers": {
                "reference": "Catalogue Number", "title": "Object Name",
                "country_origin": "Place of Origin", "value_source": "Value for Insurance (CAD)",
            },
        })
        rows.append(row)

    for page_no, page in enumerate(pdf.pages, 1):
        for table in page.extract_tables() or []:
            for raw in table or []:
                raw = list(raw or []) + [""] * 5
                add(raw[1], raw[2], raw[3], raw[4], page_no)
        try:
            table = page.extract_table({
                "vertical_strategy": "text", "horizontal_strategy": "text",
                "intersection_tolerance": 5, "snap_tolerance": 3, "join_tolerance": 3,
                "edge_min_length": 3, "min_words_vertical": 1, "min_words_horizontal": 1,
            })
        except Exception:
            table = None
        if table:
            pending_title = []
            pending_origin = []
            for raw in table:
                raw = list(raw or []) + [""] * 9
                ref = _text(raw[5])
                title_piece = _text(raw[6])
                value = _text(raw[7])
                origin_piece = _text(raw[8])
                if ref and re.match(r"^AA\s+", ref, re.I):
                    title = " ".join(pending_title + ([title_piece] if title_piece else []))
                    origin = " ".join(pending_origin + ([origin_piece] if origin_piece else []))
                    add(ref, title, value, origin, page_no)
                    pending_title = []
                    pending_origin = []
                else:
                    if title_piece and "total value" not in _norm(title_piece):
                        pending_title.append(title_piece)
                    if origin_piece:
                        pending_origin.append(origin_piece)
    return rows


def _extract_meghann(pdf, owner: str, filename: str) -> List[Dict]:
    rows = []
    for page_no, page in enumerate(pdf.pages, 1):
        for table in page.extract_tables() or []:
            for raw in table or []:
                raw = list(raw or []) + [""] * 5
                dims = _text(raw[1])
                title = _text(raw[2])
                value = _text(raw[3])
                origin = _text(raw[4])
                if not title or _norm(title) in {"object name", "title"}:
                    continue
                if not value or not re.search(r"\d", value):
                    continue
                display_value, number = _money(value)
                row = _base_row(owner, filename, page_no)
                row.update({
                    "title": title,
                    "dimensions": _dimensions(dims),
                    "country_origin": origin,
                    "source_value": display_value,
                    "source_value_number": number,
                    "source_currency": "CAD",
                    "source_headers": {
                        "dimensions": "Dims", "title": "Object Name",
                        "value_source": "Value for Insurance (CAD)", "country_origin": "Place of Origin",
                    },
                })
                rows.append(row)
    return rows


def _apply_generic_fields(row: Dict, mapping: Dict[str, int], raw: List, header_values: Dict[str, str], currency: str):
    def cell(field):
        idx = mapping.get(field)
        return raw[idx] if idx is not None and idx < len(raw) else ""

    ref = _text(cell("reference"))
    row["reference"] = ref
    row["title"] = _text(cell("title"))
    row["medium"] = _text(cell("medium"))
    row["hs_code"] = _text(cell("hs_code"))
    row["license"] = _license_from_note(cell("license")) or _text(cell("license"))
    row["dimensions"] = _dimensions(cell("dimensions"))
    row["country_origin"] = _text(cell("country_origin"))
    display_value, number = _money(cell("value_source"))
    row["source_value"] = display_value
    row["source_value_number"] = number
    row["source_currency"] = currency
    row["value_usd"] = display_value if currency == "USD" else ""
    row["source_headers"] = dict(header_values)

    reserved = {"owner", "reference", "title", "medium", "hs_code", "license", "dimensions", "country_origin", "value_source"}
    for field in mapping:
        if field not in reserved:
            row[field] = _text(cell(field))
    return row


def _extract_generic_pdf(pdf, owner: str, filename: str, full_text: str, aliases_map: Dict[str, List[str]]) -> List[Dict]:
    rows = []
    seen = set()
    for page_no, page in enumerate(pdf.pages, 1):
        for table in page.extract_tables() or []:
            if not table:
                continue
            header_idx = None
            mapping = {}
            header_values = {}
            for i, raw in enumerate(table[:6]):
                candidate = {}
                candidate_values = {}
                for col, cell_value in enumerate(raw or []):
                    field = _match_header(cell_value, aliases_map)
                    if field and field not in candidate:
                        candidate[field] = col
                        candidate_values[field] = _text(cell_value)
                if "title" in candidate and len(candidate) >= 2:
                    header_idx = i
                    mapping = candidate
                    header_values = candidate_values
                    break
            if header_idx is None:
                continue
            currency = _currency_from_header(header_values.get("value_source", ""), full_text)
            for raw in table[header_idx + 1:]:
                raw = list(raw or [])
                title_idx = mapping.get("title")
                title = _text(raw[title_idx]) if title_idx is not None and title_idx < len(raw) else ""
                if not title:
                    continue
                ref_idx = mapping.get("reference")
                ref = _text(raw[ref_idx]) if ref_idx is not None and ref_idx < len(raw) else ""
                signature = (ref.casefold(), title.casefold(), page_no)
                if signature in seen:
                    continue
                seen.add(signature)
                row = _base_row(owner, filename, page_no, ref)
                rows.append(_apply_generic_fields(row, mapping, raw, header_values, currency))
    return rows


def extract_customs_pdf(filename: str, content: bytes, learned_profiles: List[Dict] | None = None, learned_rules: List[Dict] | None = None) -> Dict:
    import pdfplumber

    with pdfplumber.open(BytesIO(content)) as pdf:
        page_texts = [p.extract_text() or "" for p in pdf.pages]
        full_text = "\n".join(page_texts)
        signature = _signature_terms_pdf(pdf, full_text)
        builtin_profile = _profile_name(full_text)

        matched_profile = None
        learned_score = 0.0
        if builtin_profile == "Générique":
            matched_profile, learned_score = _best_learned_profile(signature, learned_profiles)

        if builtin_profile != "Générique":
            profile = builtin_profile
            profile_key = _profile_key("douanes", profile)
            profile_confidence = 1.0
            profile_source = "built-in"
        elif matched_profile:
            profile = _text(matched_profile.get("name")) or "Profil appris"
            profile_key = _text(matched_profile.get("profile_key")) or _profile_key("douanes", profile)
            profile_confidence = learned_score
            profile_source = "learned"
        else:
            profile = "Générique"
            profile_key = f"douanes:generic:{_signature_hash(signature)}"
            profile_confidence = learned_score
            profile_source = "generic"

        aliases_map = _aliases_from_rules(learned_rules, profile_key)
        owner = _owner_from_text(full_text, aliases_map.get("owner"))

        if builtin_profile == "MOA - Pro Forma":
            rows = _extract_moa(pdf, owner, filename)
        elif builtin_profile == "Hervé Curat - Pro Forma":
            rows = _extract_herve(pdf, owner, filename)
        elif builtin_profile == "Museum of Vancouver - Pro Forma":
            rows = _extract_mov(pdf, owner, filename)
        elif builtin_profile == "Meghann O'Brien - Pro Forma":
            rows = _extract_meghann(pdf, owner, filename)
        else:
            rows = _extract_generic_pdf(pdf, owner, filename, full_text, aliases_map)

    if not rows and builtin_profile != "Générique":
        with pdfplumber.open(BytesIO(content)) as pdf:
            page_texts = [p.extract_text() or "" for p in pdf.pages]
            full_text = "\n".join(page_texts)
            aliases_map = _aliases_from_rules(learned_rules, profile_key)
            rows = _extract_generic_pdf(pdf, owner, filename, full_text, aliases_map)
            if rows:
                profile += " / secours générique"

    return {
        "filename": filename,
        "profile": profile,
        "profile_key": profile_key,
        "profile_confidence": profile_confidence,
        "profile_source": profile_source,
        "signature": signature,
        "owner": owner,
        "row_count": len(rows),
        "rows": rows,
    }


def extract_customs_xlsx(filename: str, content: bytes, learned_profiles: List[Dict] | None = None, learned_rules: List[Dict] | None = None) -> Dict:
    from openpyxl import load_workbook

    wb = load_workbook(BytesIO(content), read_only=True, data_only=True)
    rows_out = []
    signature = _signature_terms_xlsx(wb)
    matched_profile, learned_score = _best_learned_profile(signature, learned_profiles)
    if matched_profile:
        profile = _text(matched_profile.get("name")) or "Excel - profil appris"
        profile_key = _text(matched_profile.get("profile_key")) or _profile_key("douanes", profile)
        profile_confidence = learned_score
        profile_source = "learned"
    else:
        profile = "Excel - en-têtes reconnus"
        profile_key = f"douanes:excel:{_signature_hash(signature)}"
        profile_confidence = learned_score
        profile_source = "generic"
    aliases_map = _aliases_from_rules(learned_rules, profile_key)

    try:
        for ws in wb.worksheets:
            header_row = None
            mapping = {}
            header_values = {}
            for row_no in range(1, min(ws.max_row, 25) + 1):
                candidate = {}
                candidate_values = {}
                for col_no in range(1, ws.max_column + 1):
                    v = ws.cell(row_no, col_no).value
                    field = _match_header(v, aliases_map)
                    if field and field not in candidate:
                        candidate[field] = col_no
                        candidate_values[field] = _text(v)
                if "title" in candidate and len(candidate) >= 2:
                    header_row = row_no
                    mapping = candidate
                    header_values = candidate_values
                    break
            if not header_row:
                continue
            currency = _currency_from_header(header_values.get("value_source", ""), "")
            for row_no in range(header_row + 1, ws.max_row + 1):
                def raw_cell(field):
                    col = mapping.get(field)
                    return ws.cell(row_no, col).value if col else ""
                title = _text(raw_cell("title"))
                if not title:
                    continue
                ref = _text(raw_cell("reference"))
                raw = [None] * (max(mapping.values()) + 1 if mapping else 0)
                for field, col in mapping.items():
                    if col >= len(raw):
                        raw.extend([None] * (col - len(raw) + 1))
                    raw[col] = ws.cell(row_no, col).value
                zero_mapping = {field: col for field, col in mapping.items()}
                row = _base_row("", filename, row_no, ref)
                # openpyxl est indexé à partir de 1, notre tableau artificiel aussi.
                rows_out.append(_apply_generic_fields(row, zero_mapping, raw, header_values, currency))
    finally:
        wb.close()

    if not rows_out and not matched_profile:
        profile = "Excel - aucun tableau reconnu"

    return {
        "filename": filename,
        "profile": profile,
        "profile_key": profile_key,
        "profile_confidence": profile_confidence,
        "profile_source": profile_source,
        "signature": signature,
        "owner": "",
        "row_count": len(rows_out),
        "rows": rows_out,
    }


def extract_customs_document(filename: str, content: bytes, learned_profiles: List[Dict] | None = None, learned_rules: List[Dict] | None = None) -> Dict:
    lower = filename.lower()
    if lower.endswith(".pdf"):
        return extract_customs_pdf(filename, content, learned_profiles, learned_rules)
    if lower.endswith(".xlsx"):
        return extract_customs_xlsx(filename, content, learned_profiles, learned_rules)
    raise ValueError("Format non encore pris en charge par le moteur DOUANES (PDF et XLSX disponibles dans cette version).")
