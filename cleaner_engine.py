from __future__ import annotations

from io import BytesIO
import re
import unicodedata
from typing import Dict, List, Tuple


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


def _owner_from_text(text: str) -> str:
    lines = [x.strip() for x in (text or "").replace("\r", "").splitlines() if x.strip()]
    labels = ("consignor/shipper", "consignor", "shipper", "owner", "lender")
    for i, line in enumerate(lines):
        n = _norm(line)
        for label in labels:
            nl = _norm(label)
            if n.startswith(nl):
                rest = re.sub(r"^[^:]{0,40}:\s*", "", line).strip()
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
    # These source documents use comma as a thousands separator and dot as decimal separator.
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
    # Repair line wraps inside permit numbers, e.g. 0495- 26-08-26-001.
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


def _base_row(owner: str, source_file: str, source_page: int, reference: str = "") -> Dict:
    return {
        "owner": owner or "",
        "title": "",
        "medium": "",
        "hs_code": "",
        "license": "",
        "dimensions": "",
        "value_usd": "",
        "country_origin": "",
        "reference": reference or "",
        "source_file": source_file,
        "source_page": source_page,
        "source_value": "",
        "source_value_number": None,
        "source_currency": "",
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
                })
                rows.append(row)
    return rows


def _extract_herve(pdf, owner: str, filename: str) -> List[Dict]:
    rows = []
    seen = set()
    for page_no, page in enumerate(pdf.pages, 1):
        tables = page.extract_tables() or []
        for table in tables:
            if not table or max((len(r or []) for r in table), default=0) < 9:
                continue
            for raw in table:
                raw = list(raw or []) + [""] * 9
                seq = _text(raw[0])
                ref = _text(raw[2])
                if not seq.isdigit() or not ref:
                    continue
                if ref.casefold() in seen:
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
                })
                rows.append(row)
    return rows


def _extract_mov(pdf, owner: str, filename: str) -> List[Dict]:
    rows = []
    seen = set()

    def add(ref, title, value, origin, page_no):
        ref = _text(ref)
        title = _text(title)
        if not ref or not title or ref.casefold() in seen:
            return
        if not re.match(r"^AA\s+", ref, re.I):
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
        })
        rows.append(row)

    for page_no, page in enumerate(pdf.pages, 1):
        for table in page.extract_tables() or []:
            for raw in table or []:
                raw = list(raw or []) + [""] * 5
                add(raw[1], raw[2], raw[3], raw[4], page_no)

        # Continuation pages can lose the table header/border. Text-based cell detection
        # recovers the same columns and lets us rebuild wrapped titles/origins.
        try:
            table = page.extract_table({
                "vertical_strategy": "text",
                "horizontal_strategy": "text",
                "intersection_tolerance": 5,
                "snap_tolerance": 3,
                "join_tolerance": 3,
                "edge_min_length": 3,
                "min_words_vertical": 1,
                "min_words_horizontal": 1,
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
                })
                rows.append(row)
    return rows


_HEADER_ALIASES = {
    "reference": ["object id", "catalogue number", "catalog number", "item id", "reference", "ref", "inventory number", "accession number"],
    "title": ["object name title", "object name", "title", "description", "artwork", "artwork title"],
    "medium": ["medium", "materials", "material", "technique"],
    "hs_code": ["hs code", "tariff code", "commodity code", "customs code"],
    "license": ["license", "licence", "permit", "export permit", "cites"],
    "dimensions": ["dimensions", "dims", "size", "dimensions cm"],
    "country_origin": ["country of origin", "place of origin", "origin", "pays d origine"],
    "value_source": ["value for insurance cad", "value for insurance", "value cad", "value usd", "declared value", "customs value", "value"],
}


def _match_header(value: str) -> str:
    n = _norm(value)
    if not n:
        return ""
    for field, aliases in _HEADER_ALIASES.items():
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
    # Only use a document-wide currency when it is explicit.
    t = _norm(full_text)
    if "cad" in t and "usd" not in t:
        return "CAD"
    if "usd" in t and "cad" not in t:
        return "USD"
    return ""


def _extract_generic_pdf(pdf, owner: str, filename: str, full_text: str) -> List[Dict]:
    rows = []
    seen = set()
    for page_no, page in enumerate(pdf.pages, 1):
        for table in page.extract_tables() or []:
            if not table:
                continue
            header_idx = None
            mapping = {}
            for i, raw in enumerate(table[:5]):
                candidate = {}
                for col, cell in enumerate(raw or []):
                    field = _match_header(cell)
                    if field and field not in candidate:
                        candidate[field] = col
                if "title" in candidate and len(candidate) >= 2:
                    header_idx = i
                    mapping = candidate
                    break
            if header_idx is None:
                continue
            value_header = ""
            if "value_source" in mapping:
                value_header = _text(table[header_idx][mapping["value_source"]])
            currency = _currency_from_header(value_header, full_text)
            for raw in table[header_idx + 1:]:
                raw = list(raw or [])
                def cell(field):
                    idx = mapping.get(field)
                    return raw[idx] if idx is not None and idx < len(raw) else ""
                ref = _text(cell("reference"))
                title = _text(cell("title"))
                if not title:
                    continue
                signature = (ref.casefold(), title.casefold(), page_no)
                if signature in seen:
                    continue
                seen.add(signature)
                display_value, number = _money(cell("value_source"))
                row = _base_row(owner, filename, page_no, ref)
                row.update({
                    "title": title,
                    "medium": _text(cell("medium")),
                    "hs_code": _text(cell("hs_code")),
                    "license": _license_from_note(cell("license")) or _text(cell("license")),
                    "dimensions": _dimensions(cell("dimensions")),
                    "country_origin": _text(cell("country_origin")),
                    "source_value": display_value,
                    "source_value_number": number,
                    "source_currency": currency,
                    "value_usd": display_value if currency == "USD" else "",
                })
                rows.append(row)
    return rows


def extract_customs_pdf(filename: str, content: bytes) -> Dict:
    import pdfplumber

    with pdfplumber.open(BytesIO(content)) as pdf:
        page_texts = [p.extract_text() or "" for p in pdf.pages]
        full_text = "\n".join(page_texts)
        owner = _owner_from_text(full_text)
        profile = _profile_name(full_text)
        if profile == "MOA - Pro Forma":
            rows = _extract_moa(pdf, owner, filename)
        elif profile == "Hervé Curat - Pro Forma":
            rows = _extract_herve(pdf, owner, filename)
        elif profile == "Museum of Vancouver - Pro Forma":
            rows = _extract_mov(pdf, owner, filename)
        elif profile == "Meghann O'Brien - Pro Forma":
            rows = _extract_meghann(pdf, owner, filename)
        else:
            rows = _extract_generic_pdf(pdf, owner, filename, full_text)

    # If a profile-specific parser found nothing, always try the generic parser once.
    if not rows and profile != "Générique":
        with pdfplumber.open(BytesIO(content)) as pdf:
            page_texts = [p.extract_text() or "" for p in pdf.pages]
            full_text = "\n".join(page_texts)
            rows = _extract_generic_pdf(pdf, owner, filename, full_text)
            if rows:
                profile += " / secours générique"

    return {
        "filename": filename,
        "profile": profile,
        "owner": owner,
        "row_count": len(rows),
        "rows": rows,
    }


def extract_customs_xlsx(filename: str, content: bytes) -> Dict:
    from openpyxl import load_workbook

    wb = load_workbook(BytesIO(content), read_only=True, data_only=True)
    rows_out = []
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
                    field = _match_header(v)
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
                def cell(field):
                    col = mapping.get(field)
                    return ws.cell(row_no, col).value if col else ""
                title = _text(cell("title"))
                ref = _text(cell("reference"))
                if not title:
                    continue
                display_value, number = _money(cell("value_source"))
                row = _base_row("", filename, row_no, ref)
                row.update({
                    "title": title,
                    "medium": _text(cell("medium")),
                    "hs_code": _text(cell("hs_code")),
                    "license": _text(cell("license")),
                    "dimensions": _dimensions(cell("dimensions")),
                    "country_origin": _text(cell("country_origin")),
                    "source_value": display_value,
                    "source_value_number": number,
                    "source_currency": currency,
                    "value_usd": display_value if currency == "USD" else "",
                })
                rows_out.append(row)
    finally:
        wb.close()

    return {
        "filename": filename,
        "profile": "Excel - en-têtes reconnus" if rows_out else "Excel - aucun tableau reconnu",
        "owner": "",
        "row_count": len(rows_out),
        "rows": rows_out,
    }


def extract_customs_document(filename: str, content: bytes) -> Dict:
    lower = filename.lower()
    if lower.endswith(".pdf"):
        return extract_customs_pdf(filename, content)
    if lower.endswith(".xlsx"):
        return extract_customs_xlsx(filename, content)
    raise ValueError("Format non encore pris en charge par le moteur DOUANES (PDF et XLSX disponibles dans cette version).")
