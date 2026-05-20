from __future__ import annotations

import csv
import html
import io
import json
import math
import os
import re
import sqlite3
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.request import Request, urlopen

try:
    from flask import current_app, jsonify, request
except Exception:  # Allows offline unit checks without Flask installed.
    current_app = None
    request = None

    def jsonify(obj=None, *args, **kwargs):
        return obj if obj is not None else {}

try:
    from sqlalchemy import text as sa_text
except Exception:  # pragma: no cover
    sa_text = None

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

# =============================================================================
# Chatbot configuration
# =============================================================================

IST = timezone(timedelta(hours=5, minutes=30))

SHEET_CSV_URL = os.environ.get(
    "YARD_SHEET_CSV_URL",
    "https://docs.google.com/spreadsheets/d/1TPt1wTmOFj4ydC_cGS59DGCf4enFGoe-9J3LW-nbRzE/export?format=csv",
)

# Optional local fallback. This is useful during development/testing when the
# Google Sheet is not reachable. The reader below uses only the Python standard
# library; it does not require pandas/openpyxl.
SHEET_XLSX_PATH = os.environ.get("YARD_SHEET_XLSX_PATH", "").strip()

QR_DB_PATH = os.getenv("YARD_DB_PATH", "yard_logic/yard_data.db")
QR_TABLE = "qr_code_batches"
QR_AUDIT_TABLE = "qr_audit_logs"

CHATBOT_CACHE_SECONDS = int(os.getenv("CHATBOT_SHEET_CACHE_SECONDS", "60") or "60")
CHATBOT_QR_GAP_MINUTES = int(os.getenv("CHATBOT_QR_GAP_MINUTES", "30") or "30")

YARD_BAYS = ["EF", "AC", "DE", "CD", "CTLCD", "CTLDE", "BWPH", "BWPG"]
YARD_BAY_COUNT = 8
YARD_BIN_COUNT = 307
SOFTWARE_MAKER = "Swetabh Shekhar Sinha"

SHIFT_WINDOWS = {
    "A": (dtime(6, 0), dtime(14, 0)),
    "B": (dtime(14, 0), dtime(22, 0)),
    "C": (dtime(22, 0), dtime(6, 0)),  # crosses midnight
}

ALL_COLUMNS = [
    "SO_ITEM",
    "PK_Mat_batch",
    "Customer",
    "Object",
    "Batch",
    "MVT",
    "Material",
    "Qty",
    "Status",
    "TimeOfEntry",
    "SO No",
    "StorageLocation",
    "DispMode",
    "FI_Rel_text",
    "SBU_RelStatus",
    "Material_Status",
    "SoldToParty",
    "ShipToParty",
    "PaymentStatus",
    "V_EXT_GRADE",
    "BinNo",
    "V_LENGTH",
    "V_WIDTH",
    "V_THICKNESS",
    "V_PIECES",
    "V_INT_GRADE",
    "CustomerName",
    "CustomerCity",
    "EXT_GRADE",
    "SLocation",
    "Shiping Destination",
    "Aging Days",
    "Unres. Stock",
    "QUALITYREMARK",
    "Planning Material",
    "Sold-to Party Code",
    "Party Trnsp/Co. Trnsp",
    "Sold to party",
    "Ship to party",
    "Payment Status",
    "Bal2Bill",
]

DEFAULT_MATERIAL_COLUMNS = [
    "Batch",
    "BinNo",
    "SO_ITEM",
    "Material",
    "Qty",
    "Status",
    "Material_Status",
    "FI_Rel_text",
    "SBU_RelStatus",
    "CustomerName",
    "CustomerCity",
    "Shiping Destination",
    "PaymentStatus",
    "V_EXT_GRADE",
    "V_LENGTH",
    "V_WIDTH",
    "V_THICKNESS",
    "V_PIECES",
    "Aging Days",
]

LIST_COLUMNS = [
    "Batch",
    "BinNo",
    "CustomerName",
    "CustomerCity",
    "Status",
    "Material_Status",
    "FI_Rel_text",
    "Material",
    "Qty",
]

# Human language aliases. Generic words are intentionally mapped to the fields
# most yard users mean. Example: "customer" normally means CustomerName, while
# "customer code" means Customer.
COLUMN_ALIASES: Dict[str, List[str]] = {
    "SO_ITEM": [
        "so item",
        "so_item",
        "soitem",
        "sales order item",
        "sales order line",
        "so line",
        "same so item",
    ],
    "PK_Mat_batch": ["pk mat batch", "pk_mat_batch", "pk material batch", "pk batch"],
    "Customer": ["customer code", "customer id", "customer number", "sold party customer code"],
    "Object": ["object", "object number", "object id"],
    "Batch": ["batch", "batch no", "batch number", "batch id", "material batch", "plate id", "coil id"],
    "MVT": ["mvt", "movement", "movement type"],
    "Material": ["material", "material code", "material name", "planning route material"],
    "Qty": ["qty", "quantity", "weight", "ton", "tons", "tonnage"],
    "Status": ["fg wip status", "sap status", "fg status", "wip status", "status"],
    "TimeOfEntry": ["time of entry", "entry time", "entry date", "timeofentry"],
    "SO No": ["so no", "so number", "sales order", "sales order no", "sales order number"],
    "StorageLocation": ["storage location", "storage loc", "storagelocation"],
    "DispMode": ["dispatch mode", "disp mode", "dispmode", "transport mode", "mode"],
    "FI_Rel_text": ["fi release", "fi released", "fi rel", "fi status", "fi_rel_text", "fi rel text"],
    "SBU_RelStatus": ["sbu status", "sbu release", "sbu rel status", "sbu_relstatus"],
    "Material_Status": [
        "material status",
        "plate status",
        "coil status",
        "finished status",
        "to be levelled",
        "to be leveled",
        "levelling status",
        "leveling status",
        "to be quenched",
        "quenching done",
        "to be normalized",
        "under testing",
        "tpi completed",
        "stacked for wip",
        "hot coil",
        "hot plate",
        "online hold",
        "for rework",
        "for customer inspection",
        "offer to pfp",
        "offer to ppc",
        "offer to qc",
        "levelling completed",
    ],
    "SoldToParty": ["sold to party code old", "soldtoparty", "sold to party code sap"],
    "ShipToParty": ["ship to party code old", "shiptoparty", "ship to party code sap"],
    "PaymentStatus": ["payment status", "payment", "lc status", "paymentstatus"],
    "V_EXT_GRADE": ["v ext grade", "external grade", "ext grade v", "v_ext_grade", "grade"],
    "BinNo": ["bin no", "bin number", "bin", "binno", "location", "where"],
    "V_LENGTH": ["v length", "length", "plate length", "coil length", "v_length"],
    "V_WIDTH": ["v width", "width", "plate width", "coil width", "v_width"],
    "V_THICKNESS": ["v thickness", "thickness", "thick", "plate thickness", "coil thickness", "v_thickness"],
    "V_PIECES": ["v pieces", "pieces", "piece", "no of pieces", "number of pieces", "v_pieces"],
    "V_INT_GRADE": ["v int grade", "internal grade", "v_int_grade"],
    "CustomerName": ["customer name", "customer", "party", "party name", "buyer", "client"],
    "CustomerCity": ["customer city", "city", "customer location", "destination city"],
    "EXT_GRADE": ["ext grade", "external grade actual", "ext_grade"],
    "SLocation": ["s location", "slocation", "s loc"],
    "Shiping Destination": [
        "shipping destination",
        "shiping destination",
        "destination",
        "delivery destination",
        "ship destination",
    ],
    "Aging Days": ["aging days", "age", "aging", "days in yard", "old days"],
    "Unres. Stock": ["unrestricted stock", "unres stock", "unres. stock", "stock"],
    "QUALITYREMARK": ["quality remark", "quality remarks", "quality", "qualityremark", "qc remark"],
    "Planning Material": ["planning material", "planned material"],
    "Sold-to Party Code": ["sold-to party code", "sold to party code", "sold party code"],
    "Party Trnsp/Co. Trnsp": ["party transport", "company transport", "party trnsp", "co trnsp", "transporter"],
    "Sold to party": ["sold to party", "sold party", "sold-to party"],
    "Ship to party": ["ship to party", "ship party", "ship-to party"],
    "Payment Status": ["payment status 2", "payment status sap", "payment status final"],
    "Bal2Bill": ["bal2bill", "balance to bill", "bal to bill", "billing balance", "balance billing"],
}

VALUE_SYNONYMS: List[Tuple[str, str, List[str]]] = [
    # ── Material_Status – all 18 values found in the yard sheet ──────────────
    ("Material_Status", "Finished Status", [
        "finished", "finished status", "finish status", "fg material",
        "finished material", "material finished",
    ]),
    ("Material_Status", "To be Levelled", [
        "to be levelled", "to be leveled", "level pending", "levelling pending",
        "leveling pending", "needs levelling", "needs leveling",
        "pending levelling", "pending leveling", "tbl",
    ]),
    ("Material_Status", "Levelling Completed", [
        "levelling completed", "leveling completed", "levelled", "leveled",
        "levelling done", "leveling done", "levelling complete", "leveling complete",
    ]),
    ("Material_Status", "To be Quenched", [
        "to be quenched", "quenching pending", "quench pending",
        "needs quenching", "pending quenching", "tbq",
    ]),
    ("Material_Status", "Quenching done", [
        "quenching done", "quenched", "quench done", "quenching completed",
        "quenching complete",
    ]),
    ("Material_Status", "To be Normalized", [
        "to be normalized", "to be normalised", "normalizing pending",
        "normalising pending", "needs normalizing", "needs normalising",
        "pending normalizing", "pending normalisation", "tbn",
    ]),
    ("Material_Status", "Under Testing", [
        "under testing", "testing", "test pending", "in testing",
        "being tested", "material under testing",
    ]),
    ("Material_Status", "TPI completed", [
        "tpi completed", "tpi complete", "tpi done", "tpi",
        "third party inspection completed", "third party inspection done",
        "tpi inspection completed",
    ]),
    ("Material_Status", "Stacked for WIP", [
        "stacked for wip", "wip stacked", "stacked wip",
        "stacked for work in progress",
    ]),
    ("Material_Status", "Hot Coil", [
        "hot coil", "hot coils",
    ]),
    ("Material_Status", "Hot Plate", [
        "hot plate", "hot plates",
    ]),
    ("Material_Status", "Online Hold", [
        "online hold", "on hold", "hold", "material on hold",
        "online hold status",
    ]),
    ("Material_Status", "For Rework", [
        "for rework", "rework", "rework pending", "needs rework",
        "material for rework", "sent for rework",
    ]),
    ("Material_Status", "For Customer Inspection", [
        "for customer inspection", "customer inspection", "inspection pending",
        "awaiting customer inspection", "pending customer inspection",
        "customer inspection pending",
    ]),
    ("Material_Status", "Offer to PFP/SSD", [
        "offer to pfp ssd", "offer to pfp", "offer to ssd",
        "pfp ssd", "pfp offer", "ssd offer",
    ]),
    ("Material_Status", "Offer to PPC/MKTG/RPM-CUST_CLE", [
        "offer to ppc mktg rpm cust cle", "offer to ppc", "offer to mktg",
        "offer to rpm", "ppc mktg", "ppc offer", "mktg offer", "rpm offer",
        "offer to ppc mktg", "ppc mktg rpm",
    ]),
    ("Material_Status", "Offer to QC - WIP", [
        "offer to qc wip", "offer to qc - wip", "qc wip offer",
        "offered to qc wip", "qc wip",
    ]),
    ("Material_Status", "Offer to QC-WIP", [
        "offer to qc-wip", "offer to qcwip", "qcwip offer",
        "offered to qcwip",
    ]),
    # ── FI_Rel_text ──────────────────────────────────────────────────────────
    ("FI_Rel_text", "FI Released(2)", ["fi released 2", "fi released(2)", "fi release 2"]),
    ("FI_Rel_text", "FI Released (1)", ["fi released 1", "fi released(1)", "fi released", "fi release done"]),
    ("FI_Rel_text", "Waiting for FI Rel.", ["waiting for fi", "fi pending", "fi not released"]),
    # ── SBU_RelStatus ────────────────────────────────────────────────────────
    ("SBU_RelStatus", "Approved", ["sbu approved", "approved"]),
    ("SBU_RelStatus", "Not Approved", ["sbu not approved", "not approved"]),
    # ── Status ───────────────────────────────────────────────────────────────
    ("Status", "FG", ["fg", "finished goods", "finished good"]),
    ("Status", "WIP", ["wip", "work in progress"]),
    # ── PaymentStatus ────────────────────────────────────────────────────────
    ("PaymentStatus", "Available", ["payment available", "available payment", "lc available"]),
    ("PaymentStatus", "Part Avl", ["part avl", "part available", "partial available"]),
    ("PaymentStatus", "Not Reqd", ["not reqd", "not required", "payment not required"]),
    ("PaymentStatus", "LC Expired", ["lc expired", "expired lc"]),
    ("PaymentStatus", "No LC", ["no lc"]),
    # ── DispMode ─────────────────────────────────────────────────────────────
    ("DispMode", "ZRAL", ["rail", "by rail", "rake"]),
    ("DispMode", "ZTRL", ["truck", "by truck", "road"]),
]

DETECTABLE_VALUE_COLUMNS = [
    "Material_Status",
    "FI_Rel_text",
    "SBU_RelStatus",
    "PaymentStatus",
    "Payment Status",
    "Status",
    "DispMode",
    "V_EXT_GRADE",
    "EXT_GRADE",
    "CustomerCity",
    "Shiping Destination",
    "StorageLocation",
    "SLocation",
    "QUALITYREMARK",
]

# =============================================================================
# General helpers
# =============================================================================

def _as_str(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _escape(v: Any) -> str:
    return html.escape(_as_str(v), quote=True)


def _norm_text(v: Any) -> str:
    s = _as_str(v).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _norm_key(v: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", _as_str(v).lower())


def _phrase_in_norm(needle_norm: str, haystack_norm: str) -> bool:
    if not needle_norm or not haystack_norm:
        return False
    return f" {needle_norm} " in f" {haystack_norm} "


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        s = _as_str(v).replace(",", "")
        if not s:
            return default
        return float(s)
    except Exception:
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(_as_str(v).replace(",", "")))
    except Exception:
        return default


def _format_identifier(v: Any) -> str:
    s = _as_str(v)
    if not s:
        return ""
    # Excel sometimes stores large identifiers as 2.500550188E9 or 10339.0.
    if re.fullmatch(r"[-+]?\d+\.0+", s):
        return s.split(".", 1)[0]
    if re.fullmatch(r"[-+]?(?:\d+\.\d+|\d+)e[-+]?\d+", s, re.IGNORECASE):
        try:
            f = float(s)
            if math.isfinite(f) and abs(f - round(f)) < 0.000001:
                return str(int(round(f)))
        except Exception:
            pass
    return s


def _excel_serial_to_datetime(serial: str) -> str:
    try:
        val = float(_as_str(serial))
        if val <= 10000:
            return _as_str(serial)
        base = datetime(1899, 12, 30, tzinfo=IST)
        dt = base + timedelta(days=val)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return _as_str(serial)


def _format_cell_value(column: str, value: Any) -> str:
    col = _canonical_column(column) or column
    if col in {"Batch", "Object", "Customer", "SoldToParty", "ShipToParty", "Sold-to Party Code"}:
        return _format_identifier(value)
    if col == "TimeOfEntry":
        return _excel_serial_to_datetime(_as_str(value))
    return _as_str(value)


def _canonical_column(column: str) -> str:
    nk = _norm_key(column)
    for c in ALL_COLUMNS:
        if _norm_key(c) == nk:
            return c
    return ""


def _build_alias_lookup() -> Dict[str, str]:
    lookup: Dict[str, str] = {}
    # Add long, explicit aliases first.
    for col, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            lookup.setdefault(_norm_text(alias), col)
            lookup.setdefault(_norm_key(alias), col)
    # Add generated aliases from actual column names.
    for col in ALL_COLUMNS:
        generated = {
            col,
            col.replace("_", " "),
            col.replace("-", " "),
            col.replace("/", " "),
            col.replace(".", " "),
        }
        for alias in generated:
            lookup.setdefault(_norm_text(alias), col)
            lookup.setdefault(_norm_key(alias), col)
    return lookup


_ALIAS_LOOKUP = _build_alias_lookup()


def _column_from_alias(text: str) -> str:
    if not text:
        return ""
    nk = _norm_key(text)
    nt = _norm_text(text)
    if nk in _ALIAS_LOOKUP:
        return _ALIAS_LOOKUP[nk]
    if nt in _ALIAS_LOOKUP:
        return _ALIAS_LOOKUP[nt]
    return ""


def _gget(row: Dict[str, Any], key: str) -> str:
    if not row:
        return ""
    if key in row:
        return _format_cell_value(key, row.get(key))
    want = _norm_key(key)
    for k, v in row.items():
        if _norm_key(k) == want:
            return _format_cell_value(k, v)
    return ""


def _raw_gget(row: Dict[str, Any], key: str) -> str:
    if not row:
        return ""
    if key in row:
        return _as_str(row.get(key))
    want = _norm_key(key)
    for k, v in row.items():
        if _norm_key(k) == want:
            return _as_str(v)
    return ""


def _row_to_columns(row: Dict[str, Any], columns: Iterable[str]) -> Dict[str, str]:
    return {c: _gget(row, c) for c in columns}


# =============================================================================
# Sheet data loading, including local .xlsx fallback
# =============================================================================

_SHEET_CACHE: Dict[str, Any] = {"at": 0.0, "rows": [], "source": "", "error": ""}
_SHEET_LOCK = threading.Lock()


def _fetch_csv_rows() -> Tuple[List[Dict[str, str]], str]:
    if not SHEET_CSV_URL:
        return [], "CSV URL is empty"
    req = Request(SHEET_CSV_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=25) as resp:
        raw = resp.read()
    txt = raw.decode("utf-8-sig", errors="replace")
    head = txt[:300].lower()
    if "<html" in head and "google" in head:
        return [], "Google Sheet returned HTML instead of CSV"
    reader = csv.DictReader(io.StringIO(txt))
    rows = []
    for r in reader:
        if not r:
            continue
        clean = {(_as_str(k) if k is not None else ""): (_as_str(v) if v is not None else "") for k, v in r.items()}
        if any(_as_str(v) for v in clean.values()):
            rows.append(clean)
    return _normalize_sheet_rows(rows), "google_sheet_csv"


def _candidate_xlsx_paths() -> List[str]:
    candidates = []
    if SHEET_XLSX_PATH:
        candidates.append(SHEET_XLSX_PATH)
    names = ["SAP_DATA_PlateMill (1).xlsx", "SAP_DATA_PlateMill.xlsx"]
    for name in names:
        candidates.append(os.path.join(os.getcwd(), name))
        candidates.append(os.path.join(os.path.dirname(__file__), name))
        candidates.append(os.path.join("/mnt/data", name))
    out = []
    seen = set()
    for p in candidates:
        p = os.path.abspath(p)
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _xlsx_col_to_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in _as_str(cell_ref) if ch.isalpha())
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return max(0, n - 1)


def _read_xlsx_rows(path: str) -> List[Dict[str, str]]:
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(path) as z:
        shared_strings: List[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall(ns + "si"):
                pieces = []
                for t in si.iter(ns + "t"):
                    pieces.append(t.text or "")
                shared_strings.append("".join(pieces))

        sheet_name = "xl/worksheets/sheet1.xml"
        if sheet_name not in z.namelist():
            sheet_files = [n for n in z.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
            if not sheet_files:
                return []
            sheet_name = sorted(sheet_files)[0]

        matrix: List[List[str]] = []
        with z.open(sheet_name) as f:
            for event, elem in ET.iterparse(f, events=("end",)):
                if elem.tag != ns + "row":
                    continue
                values_by_index: Dict[int, str] = {}
                for c in elem.findall(ns + "c"):
                    idx = _xlsx_col_to_index(c.attrib.get("r", ""))
                    ctype = c.attrib.get("t", "")
                    value = ""
                    if ctype == "inlineStr":
                        inline = c.find(ns + "is")
                        if inline is not None:
                            value = "".join(t.text or "" for t in inline.iter(ns + "t"))
                    else:
                        v = c.find(ns + "v")
                        if v is not None:
                            raw = v.text or ""
                            if ctype == "s":
                                try:
                                    value = shared_strings[int(raw)]
                                except Exception:
                                    value = raw
                            else:
                                value = raw
                    values_by_index[idx] = _as_str(value)
                if values_by_index:
                    max_idx = max(values_by_index)
                    matrix.append([values_by_index.get(i, "") for i in range(max_idx + 1)])
                elem.clear()

    if not matrix:
        return []
    headers = [_as_str(h) for h in matrix[0]]
    rows: List[Dict[str, str]] = []
    for row in matrix[1:]:
        d = {headers[i]: (row[i] if i < len(row) else "") for i in range(len(headers)) if headers[i]}
        if any(_as_str(v) for v in d.values()):
            rows.append(d)
    return _normalize_sheet_rows(rows)


def _fetch_xlsx_rows() -> Tuple[List[Dict[str, str]], str]:
    tried = []
    for path in _candidate_xlsx_paths():
        tried.append(path)
        if os.path.exists(path):
            rows = _read_xlsx_rows(path)
            return rows, f"xlsx:{path}"
    return [], "No local Excel file found. Tried: " + ", ".join(tried[:5])


def _normalize_sheet_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    normalized: List[Dict[str, str]] = []
    for row in rows or []:
        out: Dict[str, str] = {}
        for col in ALL_COLUMNS:
            out[col] = _gget(row, col)
        # Preserve any extra fields too.
        for k, v in row.items():
            if k and k not in out:
                out[k] = _as_str(v)
        if any(_as_str(out.get(c)) for c in ALL_COLUMNS):
            normalized.append(out)
    return normalized


def _fetch_sheet_rows(force: bool = False) -> List[Dict[str, str]]:
    now = time.time()
    with _SHEET_LOCK:
        if not force and _SHEET_CACHE["rows"] and (now - float(_SHEET_CACHE["at"] or 0)) < CHATBOT_CACHE_SECONDS:
            return list(_SHEET_CACHE["rows"])

    rows: List[Dict[str, str]] = []
    source = ""
    errors: List[str] = []

    # Prefer CSV in production; use xlsx only if CSV fails or env forces local.
    prefer_local = os.getenv("CHATBOT_PREFER_LOCAL_XLSX", "0").strip().lower() in {"1", "true", "yes", "y"}
    loaders = [_fetch_xlsx_rows, _fetch_csv_rows] if prefer_local else [_fetch_csv_rows, _fetch_xlsx_rows]

    for loader in loaders:
        try:
            loaded, src = loader()
            if loaded:
                rows, source = loaded, src
                break
            errors.append(src)
        except Exception as e:
            errors.append(f"{loader.__name__}: {e}")

    with _SHEET_LOCK:
        _SHEET_CACHE.update({"at": time.time(), "rows": rows, "source": source, "error": "; ".join(errors)})
    return list(rows)


def _sheet_source_info() -> Dict[str, Any]:
    return {
        "source": _SHEET_CACHE.get("source") or "not_loaded",
        "rows": len(_SHEET_CACHE.get("rows") or []),
        "cached_at": _SHEET_CACHE.get("at"),
        "error": _SHEET_CACHE.get("error") or "",
    }


# =============================================================================
# Yard and sheet query helpers
# =============================================================================

def _normalize_bay(text: str) -> str:
    raw = _as_str(text).upper().replace("-", " ")
    compact = re.sub(r"[^A-Z0-9]", "", raw)
    mapping = {
        "CTLCD": "CTLCD",
        "CTLDE": "CTLDE",
        "CTL CD": "CTLCD",
        "CTL DE": "CTLDE",
        "BWPH": "BWPH",
        "BWPG": "BWPG",
        "BWP H": "BWPH",
        "BWP G": "BWPG",
    }
    for k, v in mapping.items():
        if re.sub(r"[^A-Z0-9]", "", k) in compact:
            return v
    # Use word boundaries for two-letter bays so AC does not match "batch".
    for bay in ("EF", "AC", "DE", "CD"):
        if re.search(rf"(^|[^A-Z0-9]){bay}([^A-Z0-9]|$)", raw):
            return bay
        if f"{bay}BAY" in compact:
            return bay
    return ""


def _bay_of_bin(bin_value: Any) -> str:
    b = _as_str(bin_value).upper().replace(" ", "")
    if not b:
        return ""
    for prefix in ("CTLCD", "CTLDE", "BWPH", "BWPG", "EF", "DE", "CD", "AC"):
        if b.startswith(prefix):
            return prefix
    return ""


def _item_type(row: Dict[str, Any]) -> str:
    text = " ".join([_gget(row, "Material"), _gget(row, "PK_Mat_batch"), _gget(row, "Object")]).lower()
    return "Coil" if "coil" in text else "Plate"


def _valid_value(v: str) -> bool:
    s = _as_str(v)
    return bool(s and s.lower() not in {"none", "nan", "null", "-"})


def _unique_values(rows: List[Dict[str, str]], column: str, limit: Optional[int] = None) -> List[str]:
    c = Counter(_gget(r, column) for r in rows if _valid_value(_gget(r, column)))
    vals = [k for k, _ in c.most_common(limit)] if limit else list(c.keys())
    return vals


def _counter_for(rows: List[Dict[str, str]], column: str) -> Counter:
    return Counter(_gget(r, column) for r in rows if _valid_value(_gget(r, column)))


def _summary(rows: List[Dict[str, str]]) -> Dict[str, Any]:
    so_counts = _counter_for(rows, "SO_ITEM")
    customers = {_gget(r, "CustomerName") or _gget(r, "Customer") for r in rows}
    customers.discard("")
    bin_values = {_gget(r, "BinNo") for r in rows if _gget(r, "BinNo")}
    bays = Counter()
    for r in rows:
        bay = _bay_of_bin(_gget(r, "BinNo"))
        if bay:
            bays[bay] += 1
    item_types = Counter(_item_type(r) for r in rows)
    return {
        "total_items": len(rows),
        "plates": item_types.get("Plate", 0),
        "coils": item_types.get("Coil", 0),
        "unique_so_items": len(so_counts),
        "unique_so_item_rows": sum(1 for c in so_counts.values() if c == 1),
        "duplicate_so_item_groups": sum(1 for c in so_counts.values() if c > 1),
        "unique_customers": len(customers),
        "occupied_bins_in_sheet": len(bin_values),
        "yard_bays_configured": YARD_BAY_COUNT,
        "yard_bins_configured": YARD_BIN_COUNT,
        "bay_item_counts": dict(bays),
        "status_breakdown": dict(_counter_for(rows, "Status")),
        "material_status_breakdown": dict(_counter_for(rows, "Material_Status").most_common(25)),
        "fi_breakdown": dict(_counter_for(rows, "FI_Rel_text")),
        "payment_breakdown": dict(_counter_for(rows, "PaymentStatus")),
        "top_customers": dict(_counter_for(rows, "CustomerName").most_common(10)),
        "top_cities": dict(_counter_for(rows, "CustomerCity").most_common(10)),
    }


def _identifier_variants(v: Any) -> set:
    s = _as_str(v)
    if not s:
        return set()
    variants = {s, _format_identifier(s)}
    # Split PK_Mat_batch into material and batch parts.
    for part in re.split(r"[|,;\s]+", s):
        if part:
            variants.add(part)
            variants.add(_format_identifier(part))
    out = set()
    for x in variants:
        x = _as_str(x)
        if not x:
            continue
        out.add(x.upper())
        out.add(re.sub(r"[^A-Z0-9]", "", x.upper()))
    return {x for x in out if x}


def _row_identifier_text(row: Dict[str, Any]) -> set:
    fields = ["Batch", "PK_Mat_batch", "Material", "SO_ITEM", "SO No", "Object"]
    out = set()
    for f in fields:
        out |= _identifier_variants(_raw_gget(row, f))
        out |= _identifier_variants(_gget(row, f))
    return out


def _row_matches_identifier(row: Dict[str, Any], term: str) -> bool:
    term = _as_str(term)
    if not term:
        return False
    term_variants = _identifier_variants(term)
    if not term_variants:
        term_variants = {term.upper(), re.sub(r"[^A-Z0-9]", "", term.upper())}
    row_variants = _row_identifier_text(row)
    for tv in term_variants:
        if not tv or len(tv) < 3:
            continue
        for rv in row_variants:
            if tv == rv or tv in rv or rv in tv:
                return True
    return False


def _find_material_records(rows: List[Dict[str, str]], term: str) -> List[Dict[str, str]]:
    term = _as_str(term)
    if not term:
        return []
    exact: List[Dict[str, str]] = []
    partial: List[Dict[str, str]] = []
    term_vars = _identifier_variants(term)
    for row in rows:
        row_vars = _row_identifier_text(row)
        if row_vars & term_vars:
            exact.append(row)
        elif _row_matches_identifier(row, term):
            partial.append(row)
    return exact or partial


def _extract_identifier_from_question(question: str, rows: List[Dict[str, str]]) -> str:
    q = _as_str(question)
    # Direct phrases first.
    patterns = [
        r"(?:where\s+is|locate|find|details?\s+(?:of|for)|show\s+(?:me\s+)?details?\s+(?:of|for))\s+(?:batch|material|plate|coil|item)?\s*([A-Za-z0-9_.\-|/]+)",
        r"(?:batch|material|plate|coil|item)\s*(?:no\.?|number|id)?\s*(?:is|=|:)?\s*([A-Za-z0-9_.\-|/]{4,})",
        r"(?:for|of)\s+(?:batch|material|plate|coil|item)?\s*([A-Za-z0-9_.\-|/]{4,})",
    ]
    stop_words = {
        "where", "material", "plate", "coil", "batch", "details", "status", "customer", "payment",
        "finished", "levelled", "generated", "allocator", "dispatch", "unique", "same",
    }
    candidates: List[str] = []
    for pat in patterns:
        for m in re.finditer(pat, q, flags=re.IGNORECASE):
            c = _as_str(m.group(1)).strip("?.!,;:()[]{}")
            if len(c) >= 4 and _norm_text(c) not in stop_words:
                candidates.append(c)

    # Then token scan: most batches contain digits and are at least 6 chars.
    for token in re.findall(r"\b[A-Za-z0-9][A-Za-z0-9_.\-|/]{4,}\b", q):
        t = token.strip("?.!,;:()[]{}")
        tn = _norm_text(t)
        if tn in stop_words:
            continue
        if any(ch.isdigit() for ch in t) or "|" in t or "_" in t:
            candidates.append(t)

    # Pick the first candidate that actually exists in the sheet.
    seen = set()
    for c in candidates:
        key = c.upper()
        if key in seen:
            continue
        seen.add(key)
        if _find_material_records(rows, c):
            return c
    return candidates[0] if candidates else ""


# =============================================================================
# Filtering, column detection, aggregations
# =============================================================================

def _requested_columns(question: str) -> List[str]:
    qn = _norm_text(question)
    requested: List[str] = []

    if any(x in qn for x in ["all detail", "all data", "full detail", "complete detail", "everything"]):
        return list(ALL_COLUMNS)

    if any(x in qn for x in ["dimension", "size", "length width", "width thickness"]):
        requested.extend(["V_LENGTH", "V_WIDTH", "V_THICKNESS", "V_PIECES"])

    if any(x in qn for x in ["where", "location", "bin", "placed", "kept"]):
        requested.append("BinNo")

    # Sort aliases longest first so "payment status" wins before generic "status".
    alias_pairs: List[Tuple[str, str]] = []
    for col, aliases in COLUMN_ALIASES.items():
        for a in aliases:
            alias_pairs.append((_norm_text(a), col))
    alias_pairs.sort(key=lambda x: len(x[0]), reverse=True)

    for alias_norm, col in alias_pairs:
        if not alias_norm:
            continue
        if _phrase_in_norm(alias_norm, qn):
            if col == "Batch" and any(x in qn for x in ["batch no", "batch number", "batch id"]):
                requested.append("Batch")
            elif col == "Batch":
                # In most material questions, "batch" is the identifier, not requested output.
                continue
            else:
                requested.append(col)

    # Yard users saying "customer" usually need name and city.
    if _phrase_in_norm("customer", qn):
        requested.extend(["CustomerName", "CustomerCity"])

    # Preserve order and remove duplicates.
    out = []
    for c in requested:
        if c in ALL_COLUMNS and c not in out:
            out.append(c)
    return out


def _detect_breakdown_column(question: str) -> str:
    qn = _norm_text(question)
    if "status wise" in qn or "statuswise" in qn or "status breakdown" in qn:
        if "material" in qn:
            return "Material_Status"
        return "Status"
    for marker in [" by ", " wise", "-wise", "breakdown", "summary of", "count of each", "each"]:
        if marker.strip(" ") in qn:
            break
    else:
        return ""
    # Try explicit aliases.
    alias_pairs = []
    for col, aliases in COLUMN_ALIASES.items():
        for a in aliases:
            alias_pairs.append((_norm_text(a), col))
    alias_pairs.sort(key=lambda x: len(x[0]), reverse=True)
    for alias_norm, col in alias_pairs:
        if alias_norm and _phrase_in_norm(alias_norm, qn):
            return col
    if "customer" in qn:
        return "CustomerName"
    if "city" in qn or "destination" in qn:
        return "CustomerCity"
    if "grade" in qn:
        return "EXT_GRADE"
    if "bay" in qn:
        return "__bay__"
    return ""


def _detect_unique_column(question: str) -> str:
    qn = _norm_text(question)
    if not any(w in qn for w in ["unique", "distinct", "different"]):
        return ""
    if "so" in qn and "item" in qn:
        return "SO_ITEM"
    if "customer" in qn or "party" in qn:
        return "CustomerName"
    if "city" in qn:
        return "CustomerCity"
    if "bin" in qn:
        return "BinNo"
    if "grade" in qn:
        return "EXT_GRADE"
    if "status" in qn:
        return "Material_Status" if "material" in qn else "Status"
    for col, aliases in COLUMN_ALIASES.items():
        for a in aliases:
            if _phrase_in_norm(_norm_text(a), qn):
                return col
    return ""


def _detect_known_value(question: str, rows: List[Dict[str, str]], target_columns: Optional[List[str]] = None) -> Tuple[str, str]:
    qn = _norm_text(question)
    if not qn:
        return "", ""

    # Manual synonyms first.
    best_col = ""
    best_val = ""
    best_score = 0
    for col, val, synonyms in VALUE_SYNONYMS:
        if target_columns and col not in target_columns:
            continue
        for syn in synonyms:
            sn = _norm_text(syn)
            if sn and _phrase_in_norm(sn, qn):
                score = len(sn) + 1000
                if score > best_score:
                    best_col, best_val, best_score = col, val, score

    # Dynamic values from the current sheet.
    cols = target_columns or DETECTABLE_VALUE_COLUMNS
    for col in cols:
        for value in _unique_values(rows, col):
            vn = _norm_text(value)
            if len(vn) < 3:
                continue
            # Avoid accidental matching of short pure numbers like 1.0.
            if re.fullmatch(r"\d+(?:\.\d+)?", vn) and len(vn) < 4:
                continue
            if _phrase_in_norm(vn, qn):
                score = len(vn)
                if col == "Material_Status":
                    score += 300
                elif col in {"FI_Rel_text", "SBU_RelStatus", "PaymentStatus", "Status"}:
                    score += 200
                if score > best_score:
                    best_col, best_val, best_score = col, value, score
    return best_col, best_val


def _extract_keyword_value(question: str, keywords: List[str]) -> str:
    q = _as_str(question)
    for kw in keywords:
        pat = rf"\b{re.escape(kw)}\b\s*(?:name\s*)?(?:is|=|:|of|for)?\s*([A-Za-z0-9&.,/\- ]{{2,80}})"
        m = re.search(pat, q, flags=re.IGNORECASE)
        if not m:
            continue
        value = m.group(1).strip(" ?.!,;:()[]{}")
        # Cut at common next clauses.
        value = re.split(
            r"\b(?:in|inside|at|with|where|having|whose|and|or|from|to|bay|status|grade|thickness)\b",
            value,
            flags=re.IGNORECASE,
        )[0].strip(" ?.!,;:()[]{}")
        if value and _norm_text(value) not in {"is", "are", "there", "all", "the"}:
            return value
    return ""


def _extract_numeric_filter(question: str) -> Optional[Dict[str, Any]]:
    qn = _norm_text(question)
    numeric_aliases = {
        "V_THICKNESS": ["thickness", "thick", "v thickness"],
        "V_LENGTH": ["length", "v length"],
        "V_WIDTH": ["width", "v width"],
        "V_PIECES": ["pieces", "piece", "v pieces"],
        "Aging Days": ["aging", "aging days", "age", "days in yard", "old"],
        "Qty": ["qty", "quantity", "weight", "tons", "tonnage"],
        "Bal2Bill": ["bal2bill", "balance to bill", "billing balance"],
        "Unres. Stock": ["stock", "unrestricted stock", "unres stock"],
    }
    op_words = [
        ("greater than or equal to", ">="),
        ("more than or equal to", ">="),
        ("less than or equal to", "<="),
        ("at least", ">="),
        ("minimum", ">="),
        ("above", ">"),
        ("greater than", ">"),
        ("more than", ">"),
        ("older than", ">"),
        ("below", "<"),
        ("less than", "<"),
        ("under", "<"),
        ("equal to", "="),
        ("equals", "="),
        ("is", "="),
    ]
    for col, aliases in numeric_aliases.items():
        for alias in aliases:
            an = _norm_text(alias)
            if not _phrase_in_norm(an, qn):
                continue
            # Examples: "thickness 10", "aging more than 30 days".
            for words, op in op_words:
                pat = rf"{re.escape(alias)}\s*(?:is\s*)?(?:{re.escape(words)}\s*)?(\d+(?:\.\d+)?)"
                m = re.search(pat, question, flags=re.IGNORECASE)
                if m:
                    return {"type": "numeric", "col": col, "op": op, "value": float(m.group(1))}
            m2 = re.search(rf"{re.escape(alias)}\s*(?:=|:)?\s*(\d+(?:\.\d+)?)", question, flags=re.IGNORECASE)
            if m2:
                return {"type": "numeric", "col": col, "op": "=", "value": float(m2.group(1))}
    return None


def _extract_filters(question: str, rows: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    qn = _norm_text(question)
    filters: List[Dict[str, Any]] = []

    bay = _normalize_bay(question)
    if bay:
        filters.append({"type": "bay", "value": bay, "label": f"Bay = {bay}"})

    # Specific common keyword captures.
    customer_val = _extract_keyword_value(question, ["customer", "party", "client", "buyer"])
    if customer_val and len(_norm_text(customer_val)) >= 2:
        filters.append({"type": "text", "col": "CustomerName", "value": customer_val, "mode": "contains", "label": f"Customer contains {customer_val}"})

    city_val = _extract_keyword_value(question, ["city", "destination"])
    if city_val and len(_norm_text(city_val)) >= 2:
        col = "Shiping Destination" if "destination" in qn else "CustomerCity"
        filters.append({"type": "text", "col": col, "value": city_val, "mode": "contains", "label": f"{col} contains {city_val}"})

    grade_val = _extract_keyword_value(question, ["grade"])
    if grade_val and len(_norm_text(grade_val)) >= 2:
        filters.append({"type": "text", "col": "EXT_GRADE", "value": grade_val, "mode": "contains", "label": f"Grade contains {grade_val}"})

    # Known exact values from low-cardinality columns.
    col, val = _detect_known_value(question, rows)
    if col and val:
        filters.append({"type": "text", "col": col, "value": val, "mode": "equals_norm", "label": f"{col} = {val}"})

    nf = _extract_numeric_filter(question)
    if nf:
        op = nf.get("op", "=")
        filters.append({**nf, "label": f"{nf['col']} {op} {nf['value']:g}"})

    # SO_ITEM explicit filter.
    so = _extract_so_item(question)
    if so:
        filters.append({"type": "text", "col": "SO_ITEM", "value": so, "mode": "equals_norm", "label": f"SO_ITEM = {so}"})

    # Remove duplicate filters.
    out = []
    seen = set()
    for f in filters:
        key = json.dumps(f, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def _row_matches_filters(row: Dict[str, str], filters: List[Dict[str, Any]]) -> bool:
    for f in filters:
        ftype = f.get("type")
        if ftype == "bay":
            if _bay_of_bin(_gget(row, "BinNo")) != f.get("value"):
                return False
        elif ftype == "text":
            col = f.get("col", "")
            val = _as_str(f.get("value"))
            actual = _gget(row, col)
            mode = f.get("mode") or "contains"
            if mode == "equals_norm":
                if _norm_text(actual) != _norm_text(val):
                    return False
            else:
                if _norm_text(val) not in _norm_text(actual):
                    return False
        elif ftype == "numeric":
            actual = _safe_float(_gget(row, f.get("col", "")), default=float("nan"))
            if math.isnan(actual):
                return False
            val = float(f.get("value", 0))
            op = f.get("op") or "="
            if op == ">" and not (actual > val):
                return False
            if op == ">=" and not (actual >= val):
                return False
            if op == "<" and not (actual < val):
                return False
            if op == "<=" and not (actual <= val):
                return False
            if op == "=" and not (abs(actual - val) < 0.0001):
                return False
    return True


def _apply_filters(rows: List[Dict[str, str]], filters: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    if not filters:
        return rows
    return [r for r in rows if _row_matches_filters(r, filters)]


# =============================================================================
# HTML response helpers
# =============================================================================

def _html_table(headers: List[str], rows: List[Dict[str, Any]], max_rows: int = 12) -> str:
    if not rows:
        return ""
    visible = rows[:max_rows]
    th = "".join(f"<th>{_escape(h)}</th>" for h in headers)
    body = []
    for r in visible:
        cells = []
        for h in headers:
            val = r.get(h, "") if isinstance(r, dict) else ""
            cells.append(f"<td>{_escape(val)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    more = ""
    if len(rows) > max_rows:
        more = f"<div style='font-size:12px;color:#64748b;margin-top:6px;'>Showing {max_rows} of {len(rows)} rows.</div>"
    return (
        "<div style='overflow:auto;max-width:100%;'>"
        "<table class='chat-data-table'><thead><tr>"
        + th
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
        + more
    )


def _html_kv_table(data: Dict[str, Any], columns: Optional[List[str]] = None) -> str:
    cols = columns or list(data.keys())
    rows = [{"Field": c, "Value": data.get(c, "")} for c in cols]
    return _html_table(["Field", "Value"], rows, max_rows=len(rows))


def _small_note(text: str) -> str:
    return f"<div style='font-size:12px;color:#64748b;margin-top:8px;'>{_escape(text)}</div>"


def _filters_label(filters: List[Dict[str, Any]]) -> str:
    if not filters:
        return ""
    return ", ".join(_as_str(f.get("label")) for f in filters if f.get("label"))


def _top_counter_html(title: str, counter: Counter, limit: int = 12) -> str:
    rows = [{"Value": k, "Count": v} for k, v in counter.most_common(limit)]
    return f"<b>{_escape(title)}</b><br>" + _html_table(["Value", "Count"], rows, max_rows=limit)


# =============================================================================
# SO_ITEM, bay, material, and general sheet answers
# =============================================================================

def _extract_so_item(question: str) -> str:
    q = _as_str(question)
    patterns = [
        r"(?:so[_\s-]?item|so\s*item|sales\s*order\s*item)\s*(?:is|=|:|#|number|no)?\s*([0-9]{6,}\|[0-9]{3,})",
        r"\b([0-9]{6,}\|[0-9]{3,})\b",
        r"(?:so[_\s-]?item|sales\s*order\s*item)\s*(?:is|=|:|#|number|no)?\s*([0-9]{8,})",
    ]
    for pat in patterns:
        m = re.search(pat, q, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


def _answer_so_item(question: str, rows: List[Dict[str, str]]) -> Tuple[str, Dict[str, Any]]:
    qn = _norm_text(question)
    counts = _counter_for(rows, "SO_ITEM")
    so_item = _extract_so_item(question)

    if so_item:
        count = counts.get(so_item, 0)
        matching = [_row_to_columns(r, LIST_COLUMNS) for r in rows if _gget(r, "SO_ITEM") == so_item]
        html_out = (
            f"SO Item <b>{_escape(so_item)}</b> has <b>{count}</b> material(s)/plate(s)/coil(s) in the yard."
        )
        if matching:
            html_out += "<br><br>" + _html_table(LIST_COLUMNS, matching, max_rows=15)
        return html_out, {"intent": "SO_ITEM_GROUP_COUNT", "so_item": so_item, "count": count, "records": matching[:50]}

    if "unique" in qn or "only unique" in qn or "single so" in qn:
        unique_items = [si for si, c in counts.items() if c == 1]
        html_out = (
            f"There are <b>{len(unique_items)}</b> material(s)/plate(s)/coil(s) whose SO Item appears only once. "
            f"Total distinct SO Items: <b>{len(counts)}</b>."
        )
        sample = [{"SO_ITEM": si, "Count": 1} for si in unique_items[:20]]
        if sample:
            html_out += "<br><br><b>Sample unique SO Items:</b>" + _html_table(["SO_ITEM", "Count"], sample, max_rows=20)
        return html_out, {"intent": "UNIQUE_SO_ITEMS", "unique_count": len(unique_items), "total_so_items": len(counts)}

    duplicate_groups = {si: c for si, c in counts.items() if c > 1}
    duplicate_rows = sum(duplicate_groups.values())
    top = Counter(duplicate_groups)
    html_out = (
        f"There are <b>{len(duplicate_groups)}</b> SO Item group(s) where more than one material shares the same SO Item, "
        f"covering <b>{duplicate_rows}</b> material(s)/plate(s)/coil(s). "
        f"The largest group has <b>{top.most_common(1)[0][1] if top else 0}</b> material(s)."
    )
    if top:
        rows2 = [{"SO_ITEM": si, "Items": c} for si, c in top.most_common(15)]
        html_out += "<br><br><b>Top SO Item groups:</b>" + _html_table(["SO_ITEM", "Items"], rows2, max_rows=15)
    return html_out, {"intent": "SO_ITEM_GROUP_SUMMARY", "duplicate_groups": len(duplicate_groups), "duplicate_rows": duplicate_rows}


def _answer_material(question: str, rows: List[Dict[str, str]], identifier: str) -> Tuple[str, Dict[str, Any]]:
    records = _find_material_records(rows, identifier)
    requested = _requested_columns(question)
    qn = _norm_text(question)
    # For "where is batch/material" users normally expect the location plus useful
    # yard details, not only the BinNo field. Keep narrow output only when they
    # explicitly ask for a specific column such as PaymentStatus or thickness.
    if not requested or (any(x in qn for x in ["where", "location", "located", "kept", "placed"]) and set(requested).issubset({"BinNo", "Material"})):
        requested = list(DEFAULT_MATERIAL_COLUMNS)
    if "where" in qn or "location" in qn or "bin" in qn:
        if "BinNo" not in requested:
            requested.insert(0, "BinNo")
    if "Batch" not in requested:
        requested.insert(0, "Batch")

    if not records:
        html_out = f"I could not find any material/batch matching <b>{_escape(identifier)}</b> in the yard sheet."
        return html_out, {"intent": "MATERIAL_LOCATION", "identifier": identifier, "found_count": 0}

    formatted_records = [_row_to_columns(r, requested) for r in records]
    first = formatted_records[0]
    bin_no = first.get("BinNo") or _gget(records[0], "BinNo")
    bay = _bay_of_bin(bin_no)

    lead = f"Found <b>{len(records)}</b> record(s) for <b>{_escape(identifier)}</b>."
    if bin_no:
        lead += f" Current location: <b>{_escape(bin_no)}</b>"
        if bay:
            lead += f" in <b>{_escape(bay)}</b> bay"
        lead += "."

    # For one record, show a field-value table. For many, show row table.
    if len(formatted_records) == 1:
        html_out = lead + "<br><br>" + _html_kv_table(formatted_records[0], requested)
    else:
        html_out = lead + "<br><br>" + _html_table(requested, formatted_records, max_rows=12)
    return html_out, {
        "intent": "MATERIAL_DETAILS",
        "identifier": identifier,
        "found_count": len(records),
        "requested_columns": requested,
        "records": formatted_records[:50],
    }


def _answer_bay_customers(rows: List[Dict[str, str]], bay: str) -> Tuple[str, Dict[str, Any]]:
    bay = _normalize_bay(bay) or bay.upper()
    bay_rows = [r for r in rows if _bay_of_bin(_gget(r, "BinNo")) == bay]
    customers = Counter((_gget(r, "CustomerName") or _gget(r, "Customer")) for r in bay_rows if (_gget(r, "CustomerName") or _gget(r, "Customer")))
    html_out = f"There are <b>{len(customers)}</b> unique customer(s) in <b>{_escape(bay)}</b> bay across <b>{len(bay_rows)}</b> item(s)."
    if customers:
        html_out += "<br><br>" + _html_table(
            ["Customer", "Items"],
            [{"Customer": c, "Items": n} for c, n in customers.most_common(25)],
            max_rows=25,
        )
    return html_out, {"intent": "BAY_CUSTOMERS", "bay": bay, "unique_customers": len(customers), "items": len(bay_rows)}


def _answer_bay_summary(rows: List[Dict[str, str]], bay: str) -> Tuple[str, Dict[str, Any]]:
    bay = _normalize_bay(bay) or bay.upper()
    bay_rows = [r for r in rows if _bay_of_bin(_gget(r, "BinNo")) == bay]
    customers = {(_gget(r, "CustomerName") or _gget(r, "Customer")) for r in bay_rows if (_gget(r, "CustomerName") or _gget(r, "Customer"))}
    status = _counter_for(bay_rows, "Status")
    mat_status = _counter_for(bay_rows, "Material_Status")
    html_out = (
        f"<b>{_escape(bay)} bay summary:</b> <b>{len(bay_rows)}</b> item(s), "
        f"<b>{len(customers)}</b> unique customer(s)."
    )
    if status:
        html_out += "<br><br>" + _top_counter_html("FG/WIP status", status, limit=10)
    if mat_status:
        html_out += "<br><br>" + _top_counter_html("Material status", mat_status, limit=10)
    sample = [_row_to_columns(r, LIST_COLUMNS) for r in bay_rows[:20]]
    if sample:
        html_out += "<br><br><b>Sample materials:</b>" + _html_table(LIST_COLUMNS, sample, max_rows=12)
    return html_out, {"intent": "BAY_SUMMARY", "bay": bay, "items": len(bay_rows), "customers": len(customers)}


def _answer_column_breakdown(rows: List[Dict[str, str]], column: str) -> Tuple[str, Dict[str, Any]]:
    if column == "__bay__":
        counter = Counter(_bay_of_bin(_gget(r, "BinNo")) or "Other" for r in rows)
        title = "Bay-wise item count"
    else:
        counter = _counter_for(rows, column)
        title = f"{column} breakdown"
    html_out = _top_counter_html(title, counter, limit=25)
    return html_out, {"intent": "COLUMN_BREAKDOWN", "column": column, "breakdown": dict(counter)}


def _answer_unique_column(rows: List[Dict[str, str]], column: str) -> Tuple[str, Dict[str, Any]]:
    if column == "SO_ITEM":
        return _answer_so_item("unique so item", rows)
    vals = _unique_values(rows, column)
    html_out = f"There are <b>{len(vals)}</b> unique value(s) in <b>{_escape(column)}</b>."
    sample = [{column: v} for v in vals[:25]]
    if sample:
        html_out += "<br><br><b>Sample values:</b>" + _html_table([column], sample, max_rows=25)
    return html_out, {"intent": "UNIQUE_COLUMN", "column": column, "unique_count": len(vals), "sample": vals[:50]}


def _answer_filtered_rows(question: str, rows: List[Dict[str, str]], filters: List[Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    filtered = _apply_filters(rows, filters)
    qn = _norm_text(question)
    label = _filters_label(filters) or "your filter"

    # If asking how many customers with filter, count unique customers instead of rows.
    if "customer" in qn and any(w in qn for w in ["how many", "count", "number of"]):
        customers = {(_gget(r, "CustomerName") or _gget(r, "Customer")) for r in filtered if (_gget(r, "CustomerName") or _gget(r, "Customer"))}
        html_out = f"There are <b>{len(customers)}</b> unique customer(s) for <b>{_escape(label)}</b> across <b>{len(filtered)}</b> item(s)."
        if customers:
            c = Counter((_gget(r, "CustomerName") or _gget(r, "Customer")) for r in filtered if (_gget(r, "CustomerName") or _gget(r, "Customer")))
            html_out += "<br><br>" + _html_table(["Customer", "Items"], [{"Customer": k, "Items": v} for k, v in c.most_common(20)], max_rows=20)
        return html_out, {"intent": "FILTER_CUSTOMERS", "count": len(customers), "items": len(filtered), "filters": filters}

    # Item type-specific count.
    # Skip this secondary filter when a Material_Status filter is already active,
    # because status names like "Hot Coil" or "Hot Plate" contain the words
    # "coil"/"plate" but the physical items may be classified differently by
    # _item_type(), which would wrongly zero-out the results.
    has_material_status_filter = any(f.get("col") == "Material_Status" for f in filters)
    type_word = "item(s)/material(s)"
    if not has_material_status_filter:
        if "coil" in qn and "plate" not in qn:
            filtered = [r for r in filtered if _item_type(r) == "Coil"]
            type_word = "coil(s)"
        elif "plate" in qn and "coil" not in qn:
            filtered = [r for r in filtered if _item_type(r) == "Plate"]
            type_word = "plate(s)"

    html_out = f"Found <b>{len(filtered)}</b> {type_word} for <b>{_escape(label)}</b>."
    sample = [_row_to_columns(r, LIST_COLUMNS) for r in filtered[:30]]
    if sample and any(w in qn for w in ["show", "list", "which", "details", "where", "count", "how many"]):
        html_out += "<br><br>" + _html_table(LIST_COLUMNS, sample, max_rows=12)
    return html_out, {"intent": "FILTERED_ROWS", "count": len(filtered), "filters": filters, "records": sample[:50]}


def _answer_total_or_general_count(question: str, rows: List[Dict[str, str]]) -> Optional[Tuple[str, Dict[str, Any]]]:
    qn = _norm_text(question)
    is_count = any(x in qn for x in ["how many", "count", "number of", "total"])
    if not is_count:
        return None

    if "customer" in qn and "bay" not in qn:
        c = _counter_for(rows, "CustomerName")
        html_out = f"There are <b>{len(c)}</b> unique customer(s) in the yard sheet."
        html_out += "<br><br>" + _html_table(["Customer", "Items"], [{"Customer": k, "Items": v} for k, v in c.most_common(15)], max_rows=15)
        return html_out, {"intent": "TOTAL_CUSTOMERS", "unique_customers": len(c)}

    if "occupied" in qn and "bin" in qn:
        bins = {_gget(r, "BinNo") for r in rows if _gget(r, "BinNo")}
        html_out = f"There are <b>{len(bins)}</b> occupied/non-empty bin code(s) in the current yard sheet."
        if len(bins) <= YARD_BIN_COUNT:
            html_out += f" Configured yard bin count is <b>{YARD_BIN_COUNT}</b>, so estimated empty bins are <b>{YARD_BIN_COUNT - len(bins)}</b>."
        return html_out, {"intent": "OCCUPIED_BINS", "occupied_bins": len(bins), "configured_bins": YARD_BIN_COUNT}

    if "bin" in qn and any(x in qn for x in ["yard", "total", "there"]):
        html_out = f"There are <b>{YARD_BIN_COUNT}</b> bins in the yard."
        return html_out, {"intent": "YARD_BINS", "bins": YARD_BIN_COUNT}

    if "plate" in qn and "coil" not in qn:
        n = sum(1 for r in rows if _item_type(r) == "Plate")
        return f"There are <b>{n}</b> plate(s) in the current yard sheet.", {"intent": "TOTAL_PLATES", "count": n}
    if "coil" in qn and "plate" not in qn:
        n = sum(1 for r in rows if _item_type(r) == "Coil")
        return f"There are <b>{n}</b> coil(s) in the current yard sheet.", {"intent": "TOTAL_COILS", "count": n}
    if any(x in qn for x in ["material", "item", "items", "plates", "coils"]):
        return f"There are <b>{len(rows)}</b> material(s)/item(s) in the current yard sheet.", {"intent": "TOTAL_ITEMS", "count": len(rows)}
    return None


def _answer_numeric_summary(question: str, rows: List[Dict[str, str]]) -> Optional[Tuple[str, Dict[str, Any]]]:
    qn = _norm_text(question)
    if not any(w in qn for w in ["sum", "total", "average", "avg", "maximum", "minimum", "max", "min"]):
        return None
    target_col = ""
    for col in ["Qty", "Bal2Bill", "Aging Days", "Unres. Stock", "V_THICKNESS", "V_LENGTH", "V_WIDTH", "V_PIECES"]:
        aliases = COLUMN_ALIASES.get(col, []) + [col]
        if any(_phrase_in_norm(_norm_text(a), qn) for a in aliases):
            target_col = col
            break
    if not target_col:
        return None
    values = [_safe_float(_gget(r, target_col), default=float("nan")) for r in rows]
    values = [v for v in values if not math.isnan(v)]
    if not values:
        return f"No numeric values found for <b>{_escape(target_col)}</b>.", {"intent": "NUMERIC_SUMMARY", "column": target_col, "count": 0}
    total = sum(values)
    avg = total / len(values)
    html_out = (
        f"<b>{_escape(target_col)}</b> numeric summary: count <b>{len(values)}</b>, "
        f"total <b>{total:,.3f}</b>, average <b>{avg:,.3f}</b>, "
        f"minimum <b>{min(values):,.3f}</b>, maximum <b>{max(values):,.3f}</b>."
    )
    return html_out, {"intent": "NUMERIC_SUMMARY", "column": target_col, "count": len(values), "sum": total, "average": avg, "min": min(values), "max": max(values)}


def _answer_sheet_question(question: str) -> Tuple[str, Dict[str, Any]]:
    rows = _fetch_sheet_rows()
    if not rows:
        info = _sheet_source_info()
        html_out = "I could not read the yard sheet right now."
        if info.get("error"):
            html_out += _small_note(info.get("error"))
        return html_out, {"intent": "SHEET_UNAVAILABLE", **info}

    qn = _norm_text(question)

    # SO_ITEM questions.
    if ("so" in qn and "item" in qn) or "sales order item" in qn:
        if any(x in qn for x in ["how many", "count", "unique", "same", "under", "group"]):
            return _answer_so_item(question, rows)

    # Material/batch lookup should run before generic filters.
    identifier = _extract_identifier_from_question(question, rows)
    if identifier and any(x in qn for x in ["where", "detail", "show", "find", "locate", "what is", "which", "tell"]):
        return _answer_material(question, rows, identifier)

    # Bay queries.
    bay = _normalize_bay(question)
    if bay:
        if "customer" in qn:
            return _answer_bay_customers(rows, bay)
        return _answer_bay_summary(rows, bay)

    # Unique/distinct questions.
    unique_col = _detect_unique_column(question)
    if unique_col:
        return _answer_unique_column(rows, unique_col)

    # Breakdown/group-by questions.
    breakdown_col = _detect_breakdown_column(question)
    if breakdown_col:
        return _answer_column_breakdown(rows, breakdown_col)

    # Known material/status/grade/city/etc. value questions.
    filters = _extract_filters(question, rows)
    if filters:
        return _answer_filtered_rows(question, rows, filters)

    # Totals without filters.
    total_answer = _answer_total_or_general_count(question, rows)
    if total_answer:
        return total_answer

    # Numeric summary.
    numeric_answer = _answer_numeric_summary(question, rows)
    if numeric_answer:
        return numeric_answer

    # Direct "top" questions.
    if "top" in qn or "most" in qn:
        if "customer" in qn:
            return _top_counter_html("Top customers by item count", _counter_for(rows, "CustomerName"), 20), {"intent": "TOP_CUSTOMERS"}
        if "city" in qn or "destination" in qn:
            return _top_counter_html("Top cities by item count", _counter_for(rows, "CustomerCity"), 20), {"intent": "TOP_CITIES"}
        if "grade" in qn:
            return _top_counter_html("Top grades by item count", _counter_for(rows, "EXT_GRADE"), 20), {"intent": "TOP_GRADES"}
        if "status" in qn:
            return _top_counter_html("Top material statuses", _counter_for(rows, "Material_Status"), 20), {"intent": "TOP_STATUSES"}

    summary = _summary(rows)
    html_out = (
        "I can answer yard questions from the sheet and the live yard APIs. "
        f"Current sheet rows loaded: <b>{summary['total_items']}</b>. "
        "Try asking: <i>Where is batch 2518990ACA?</i>, <i>How many customers in EF bay?</i>, "
        "<i>How many plates with Finished Status?</i>, or <i>How many QR codes in A shift?</i>"
    )
    return html_out, {"intent": "GENERAL_HELP", "summary": summary}


# =============================================================================
# Static yard answers
# =============================================================================

def _answer_static(question: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    qn = _norm_text(question)
    if any(x in qn for x in ["who made", "who developed", "developer", "created this software", "made this software", "software maker"]):
        return f"This software was made by <b>{_escape(SOFTWARE_MAKER)}</b>.", {"intent": "SOFTWARE_MAKER", "maker": SOFTWARE_MAKER}

    if "bay" in qn and any(x in qn for x in ["how many bays", "number of bays", "total bays", "bays are there"]):
        return f"There are <b>{YARD_BAY_COUNT}</b> bays in the yard: <b>{', '.join(YARD_BAYS)}</b>.", {"intent": "YARD_BAYS", "count": YARD_BAY_COUNT, "bays": YARD_BAYS}

    if "bin" in qn and any(x in qn for x in ["how many bins", "number of bins", "total bins", "bins are there"]):
        return f"There are <b>{YARD_BIN_COUNT}</b> bins in the yard.", {"intent": "YARD_BINS", "count": YARD_BIN_COUNT}

    return None


# =============================================================================
# QR code history and answers
# =============================================================================

def _parse_dt_any(value: Any) -> Optional[datetime]:
    s = _as_str(value)
    if not s:
        return None
    # ISO with timezone.
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(IST)
    except Exception:
        pass
    # Common SQL-ish formats.
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S"]:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=IST)
        except Exception:
            continue
    return None


def _connect_qr_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(QR_DB_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(QR_DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _sqlite_table_exists(con: sqlite3.Connection, table: str) -> bool:
    try:
        cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        return cur.fetchone() is not None
    except Exception:
        return False


def _qr_events_from_db() -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    try:
        con = _connect_qr_db()
        try:
            if _sqlite_table_exists(con, QR_AUDIT_TABLE):
                cur = con.execute(
                    f"SELECT user_name, action, batch, timestamp FROM {QR_AUDIT_TABLE} ORDER BY timestamp ASC"
                )
                for r in cur.fetchall():
                    action = _as_str(r["action"]).lower()
                    if action and "generat" not in action:
                        continue
                    ts = _parse_dt_any(r["timestamp"])
                    if ts:
                        events.append({"batch": _as_str(r["batch"]), "timestamp": ts, "source": "qr_audit_logs", "user": _as_str(r["user_name"]), "action": _as_str(r["action"])})
            if not events and _sqlite_table_exists(con, QR_TABLE):
                cur = con.execute(
                    f"SELECT batch, created_at, updated_at FROM {QR_TABLE} ORDER BY created_at ASC"
                )
                for r in cur.fetchall():
                    ts = _parse_dt_any(r["created_at"] or r["updated_at"])
                    if ts:
                        events.append({"batch": _as_str(r["batch"]), "timestamp": ts, "source": QR_TABLE, "action": "generated"})
        finally:
            con.close()
    except Exception:
        return []
    events.sort(key=lambda x: x["timestamp"])
    return events


def _client_get_json(client: Any, path: str) -> Dict[str, Any]:
    if client is None:
        return {}
    try:
        res = client.get(path)
        if getattr(res, "status_code", 500) == 200:
            data = res.get_json(silent=True)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def _qr_events_from_api(client: Any) -> List[Dict[str, Any]]:
    data = _client_get_json(client, "/api/qr-code-generator/history")
    events: List[Dict[str, Any]] = []
    for r in data.get("history", []) or []:
        action = _as_str(r.get("action")).lower()
        if action and "generat" not in action:
            continue
        ts = _parse_dt_any(r.get("timestamp"))
        if ts:
            events.append({"batch": _as_str(r.get("batch")), "timestamp": ts, "source": "qr_history_api", "user": _as_str(r.get("user_name")), "action": _as_str(r.get("action"))})
    if not events:
        data2 = _client_get_json(client, "/api/qr-code-generator/list")
        for r in data2.get("rows", []) or []:
            ts = _parse_dt_any(r.get("created_at") or r.get("updated_at"))
            if ts:
                events.append({"batch": _as_str(r.get("batch")), "timestamp": ts, "source": "qr_list_api", "action": "generated"})
    events.sort(key=lambda x: x["timestamp"])
    return events


def _load_qr_events(client: Any = None) -> List[Dict[str, Any]]:
    events = _qr_events_from_db()
    if events:
        return events
    return _qr_events_from_api(client)


def _parse_date_from_question(question: str) -> date:
    qn = _norm_text(question)
    today = datetime.now(IST).date()
    if "yesterday" in qn:
        return today - timedelta(days=1)
    if "tomorrow" in qn:
        return today + timedelta(days=1)
    m = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", question)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            pass
    m = re.search(r"\b(\d{1,2})[-/](\d{1,2})[-/](20\d{2})\b", question)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except Exception:
            pass
    return today


def _parse_time_token(token: str) -> Optional[dtime]:
    s = _as_str(token).lower().replace(".", "").strip()
    s = re.sub(r"\s+", "", s)
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", s)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = m.group(3) or ""
    if ampm == "pm" and hour != 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return dtime(hour, minute)


def _extract_time_range(question: str) -> Tuple[Optional[dtime], Optional[dtime]]:
    patterns = [
        r"from\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+(?:to|till|until|and)\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
        r"between\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+(?:and|to)\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
        r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*[-–]\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))",
    ]
    for pat in patterns:
        m = re.search(pat, question, flags=re.IGNORECASE)
        if m:
            return _parse_time_token(m.group(1)), _parse_time_token(m.group(2))
    return None, None


def _shift_window(shift: str, base: Optional[date] = None) -> Tuple[datetime, datetime]:
    shift = _as_str(shift).upper()
    now = datetime.now(IST)
    if base is None:
        if shift == "C":
            if now.time() < dtime(6, 0):
                base = now.date() - timedelta(days=1)
            elif now.time() >= dtime(22, 0):
                base = now.date()
            else:
                base = now.date() - timedelta(days=1)
        else:
            base = now.date()
    start_t, end_t = SHIFT_WINDOWS[shift]
    start_dt = datetime.combine(base, start_t, tzinfo=IST)
    end_date = base + timedelta(days=1) if end_t <= start_t else base
    end_dt = datetime.combine(end_date, end_t, tzinfo=IST)
    # End is exclusive internally; subtract 1 second only for display if needed.
    return start_dt, end_dt


def _detect_shift(question: str) -> str:
    m = re.search(r"\b([ABCabc])\s*(?:shift|shft)\b", question)
    if m:
        return m.group(1).upper()
    qn = _norm_text(question)
    if "shift a" in qn:
        return "A"
    if "shift b" in qn:
        return "B"
    if "shift c" in qn:
        return "C"
    return ""


def _events_in_window(events: List[Dict[str, Any]], start_dt: datetime, end_dt: datetime) -> List[Dict[str, Any]]:
    return [e for e in events if start_dt <= e["timestamp"] < end_dt]


def _format_dt(dt: datetime) -> str:
    return dt.astimezone(IST).strftime("%d-%m-%Y %H:%M")


def _answer_qr(question: str, client: Any = None) -> Tuple[str, Dict[str, Any]]:
    events = _load_qr_events(client)
    qn = _norm_text(question)

    if not events:
        return "No QR generation history was found yet.", {"intent": "QR", "count": 0, "events": []}

    # Gap / downtime question.
    if any(x in qn for x in ["not generated", "gap", "gaps", "without qr", "idle", "no qr", "how much time"]):
        threshold = CHATBOT_QR_GAP_MINUTES
        m = re.search(r"(?:more than|above|over)\s+(\d+)\s*(?:min|minute|minutes)", question, flags=re.IGNORECASE)
        if m:
            threshold = int(m.group(1))
        gaps = []
        for prev, cur in zip(events, events[1:]):
            diff = cur["timestamp"] - prev["timestamp"]
            minutes = diff.total_seconds() / 60.0
            if minutes >= threshold:
                gaps.append({
                    "From": _format_dt(prev["timestamp"]),
                    "To": _format_dt(cur["timestamp"]),
                    "Gap Minutes": round(minutes, 1),
                    "Gap Hours": round(minutes / 60.0, 2),
                    "Previous Batch": prev.get("batch", ""),
                    "Next Batch": cur.get("batch", ""),
                })
        gaps.sort(key=lambda g: g["Gap Minutes"], reverse=True)
        if not gaps:
            html_out = f"No QR generation gap of <b>{threshold}</b> minutes or more was found in the available history."
        else:
            total_gap = sum(float(g["Gap Minutes"]) for g in gaps)
            html_out = (
                f"Found <b>{len(gaps)}</b> QR generation gap(s) of at least <b>{threshold}</b> minutes. "
                f"Combined gap time: <b>{total_gap:.1f}</b> minutes (<b>{total_gap / 60.0:.2f}</b> hours)."
                "<br><br>"
                + _html_table(["From", "To", "Gap Minutes", "Gap Hours", "Previous Batch", "Next Batch"], gaps, max_rows=10)
            )
        return html_out, {"intent": "QR_GAPS", "threshold_minutes": threshold, "gaps": gaps[:50], "total_events": len(events)}

    # Shift question.
    shift = _detect_shift(question)
    if shift:
        # If a date is explicitly mentioned, use that date. Otherwise current/recent shift window.
        base = _parse_date_from_question(question)
        if not any(x in qn for x in ["today", "yesterday", "tomorrow"]) and not re.search(r"\d{1,2}[-/]\d{1,2}[-/]20\d{2}|20\d{2}[-/]\d{1,2}[-/]\d{1,2}", question):
            base = None
        start_dt, end_dt = _shift_window(shift, base)
        found = _events_in_window(events, start_dt, end_dt)
        html_out = (
            f"<b>{len(found)}</b> QR code(s) were generated in <b>{shift} shift</b> "
            f"({_format_dt(start_dt)} to {_format_dt(end_dt)} IST)."
        )
        sample = [{"Batch": e.get("batch", ""), "Generated At": _format_dt(e["timestamp"]), "User": e.get("user", "")} for e in found[:20]]
        if sample:
            html_out += "<br><br>" + _html_table(["Batch", "Generated At", "User"], sample, max_rows=15)
        return html_out, {"intent": "QR_SHIFT", "shift": shift, "count": len(found), "start": start_dt.isoformat(), "end": end_dt.isoformat()}

    # Time range question.
    start_t, end_t = _extract_time_range(question)
    if start_t and end_t:
        base = _parse_date_from_question(question)
        start_dt = datetime.combine(base, start_t, tzinfo=IST)
        end_dt = datetime.combine(base, end_t, tzinfo=IST)
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        found = _events_in_window(events, start_dt, end_dt)
        html_out = (
            f"<b>{len(found)}</b> QR code(s) were generated from <b>{_format_dt(start_dt)}</b> "
            f"to <b>{_format_dt(end_dt)}</b> IST."
        )
        sample = [{"Batch": e.get("batch", ""), "Generated At": _format_dt(e["timestamp"]), "User": e.get("user", "")} for e in found[:20]]
        if sample:
            html_out += "<br><br>" + _html_table(["Batch", "Generated At", "User"], sample, max_rows=15)
        return html_out, {"intent": "QR_TIME_RANGE", "count": len(found), "start": start_dt.isoformat(), "end": end_dt.isoformat()}

    # General QR count: include total and today's count.
    today = datetime.now(IST).date()
    start_today = datetime.combine(today, dtime(0, 0), tzinfo=IST)
    end_today = start_today + timedelta(days=1)
    today_events = _events_in_window(events, start_today, end_today)
    last = events[-1]
    html_out = (
        f"Total QR generation records available: <b>{len(events)}</b>. "
        f"Generated today: <b>{len(today_events)}</b>. Last generated QR: <b>{_escape(last.get('batch', ''))}</b> "
        f"at <b>{_format_dt(last['timestamp'])}</b> IST."
    )
    return html_out, {"intent": "QR_COUNT", "total": len(events), "today": len(today_events), "last": last.get("batch", "")}


# =============================================================================
# Dispatch, Bin Allocator, and Vehicle Sequencing API answers
# =============================================================================

def _dispatch_summary_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    units = payload.get("units") or []
    full = []
    remainder = []
    for u in units:
        target = _safe_float(u.get("target_tons"), 0.0)
        if target >= 32.0:
            full.append(u)
        else:
            remainder.append(u)
    material_count = sum(_safe_int(u.get("count"), 0) for u in full)
    total_weight = sum(_safe_float(u.get("total_weight"), 0.0) for u in full)
    by_type = Counter()
    by_city = Counter()
    for u in full:
        by_city[_as_str(u.get("city")) or "Unknown"] += _safe_int(u.get("count"), 0)
        tg = _as_str(u.get("type_group"))
        if tg:
            by_type[tg.title()] += _safe_int(u.get("count"), 0)
        for it in u.get("items", []) or []:
            tg2 = _as_str(it.get("type_group") or it.get("type"))
            if tg2 and not tg:
                by_type[tg2.title()] += 1
    return {
        "units": units,
        "full_units": full,
        "remainder_units": remainder,
        "vehicles_ready": len(full),
        "materials_ready": material_count,
        "ready_weight": round(total_weight, 3),
        "by_type": dict(by_type),
        "by_city": dict(by_city.most_common(10)),
    }


def _answer_dispatch(question: str, client: Any = None) -> Tuple[str, Dict[str, Any]]:
    payload = _client_get_json(client, "/api/dispatch_suggestions")
    if not payload or "units" not in payload:
        return "Dispatch suggestions are not available right now.", {"intent": "DISPATCH", "error": "dispatch API unavailable"}

    s = _dispatch_summary_from_payload(payload)
    qn = _norm_text(question)

    if any(w in qn for w in ["vehicle", "vehicles", "truck", "trucks", "lorry"]):
        html_out = f"<b>{s['vehicles_ready']}</b> vehicle(s)/truck lot(s) are ready for dispatch as full lots."
    elif any(w in qn for w in ["material", "materials", "plate", "plates", "coil", "coils", "item", "items"]):
        html_out = (
            f"<b>{s['materials_ready']}</b> material(s)/plate(s)/coil(s) are ready for dispatch in full truck lots. "
            f"Ready weight: <b>{s['ready_weight']}</b> tons."
        )
    else:
        html_out = (
            f"<b>Dispatch summary:</b> <b>{s['vehicles_ready']}</b> full truck lot(s), "
            f"<b>{s['materials_ready']}</b> ready material(s), <b>{s['ready_weight']}</b> tons. "
            f"Partial/remainder lots: <b>{len(s['remainder_units'])}</b>."
        )

    rows = []
    for u in s["full_units"][:10]:
        rows.append({
            "Customer": _as_str(u.get("customer")),
            "City": _as_str(u.get("city")),
            "Bay": _as_str(u.get("bay")),
            "Type": _as_str(u.get("type_group")),
            "Items": _safe_int(u.get("count"), 0),
            "Weight": _safe_float(u.get("total_weight"), 0.0),
            "Target Tons": _safe_float(u.get("target_tons"), 0.0),
            "Lot": _as_str(u.get("lot_kind")),
        })
    if rows:
        html_out += "<br><br><b>Ready full lots:</b>" + _html_table(["Customer", "City", "Bay", "Type", "Items", "Weight", "Target Tons", "Lot"], rows, max_rows=10)
    return html_out, {"intent": "DISPATCH", **{k: v for k, v in s.items() if k not in {"units", "full_units", "remainder_units"}}}


def _allocator_transactions_from_engine(engine: Any) -> Dict[str, Any]:
    if engine is None or sa_text is None:
        return {"total": 0, "by_type": {}, "rows": []}
    try:
        with engine.begin() as con:
            rows = con.execute(
                sa_text(
                    """
                    SELECT COALESCE(item_type,'') AS item_type, COUNT(*) AS n
                    FROM yard_transactions
                    WHERE LOWER(COALESCE(method,'')) = 'bin allocator'
                    GROUP BY COALESCE(item_type,'')
                    """
                )
            ).mappings().all()
            total = con.execute(
                sa_text(
                    """
                    SELECT COUNT(*)
                    FROM yard_transactions
                    WHERE LOWER(COALESCE(method,'')) = 'bin allocator'
                    """
                )
            ).scalar() or 0
        return {"total": int(total), "by_type": {(_as_str(r.get("item_type")) or "Unknown"): int(r.get("n") or 0) for r in rows}}
    except Exception:
        return {"total": 0, "by_type": {}}


def _answer_allocator(question: str, client: Any = None, engine: Any = None) -> Tuple[str, Dict[str, Any]]:
    status_details = _client_get_json(client, "/api/allocator/status/details")
    summary = _client_get_json(client, "/api/allocator/unassigned/summary")
    tx = _allocator_transactions_from_engine(engine)

    decisions = status_details.get("rows") or []
    accepted = [r for r in decisions if _as_str(r.get("status")).lower() == "accepted"]
    rejected = [r for r in decisions if _as_str(r.get("status")).lower() == "rejected"]
    accepted_by_type = Counter(_as_str(r.get("type")) or _as_str(r.get("item_type")) or "Unknown" for r in accepted)

    qn = _norm_text(question)
    if "allocated" in qn or "allocation" in qn or "accepted" in qn:
        if tx.get("total"):
            html_out = f"<b>{tx['total']}</b> item(s) were allocated through the Bin Allocator transaction flow."
            if tx.get("by_type"):
                html_out += "<br>By type: " + ", ".join(f"<b>{_escape(k)}</b>: {_escape(v)}" for k, v in tx["by_type"].items())
        else:
            html_out = f"<b>{len(accepted)}</b> Bin Allocator suggestion(s) are marked as accepted."
            if accepted_by_type:
                html_out += "<br>By type: " + ", ".join(f"<b>{_escape(k)}</b>: {_escape(v)}" for k, v in accepted_by_type.items())
        sample = []
        for r in accepted[:12]:
            sample.append({
                "Item": _as_str(r.get("item_id")),
                "Type": _as_str(r.get("type") or r.get("item_type")),
                "Customer": _as_str(r.get("customer")),
                "Suggested Bin": _as_str(r.get("suggested_bin")),
                "Status": _as_str(r.get("status")),
            })
        if sample:
            html_out += "<br><br>" + _html_table(["Item", "Type", "Customer", "Suggested Bin", "Status"], sample, max_rows=12)
        return html_out, {"intent": "ALLOCATOR_ALLOCATED", "transactions": tx, "accepted": len(accepted), "rejected": len(rejected), "accepted_by_type": dict(accepted_by_type)}

    unassigned = summary.get("unassigned_rows", 0)
    by_type = summary.get("by_type") or {}
    html_out = (
        f"<b>Bin Allocator summary:</b> pending/unassigned candidates: <b>{_escape(unassigned)}</b>. "
        f"Accepted suggestions: <b>{len(accepted)}</b>, rejected suggestions: <b>{len(rejected)}</b>."
    )
    if by_type:
        html_out += "<br>Pending by type: " + ", ".join(f"<b>{_escape(k)}</b>: {_escape(v)}" for k, v in by_type.items())
    top_customers = summary.get("top_customers") or []
    if top_customers:
        rows = [{"Customer": x.get("customer"), "Rows": x.get("rows")} for x in top_customers[:10]]
        html_out += "<br><br>" + _html_table(["Customer", "Rows"], rows, max_rows=10)
    return html_out, {"intent": "ALLOCATOR_SUMMARY", "summary": summary, "accepted": len(accepted), "rejected": len(rejected), "transactions": tx}


def _answer_vehicle_sequencing(client: Any = None) -> Tuple[str, Dict[str, Any]]:
    payload = _client_get_json(client, "/api/vehicle_sequencing")
    loading_points = payload.get("loading_points") or []
    if not payload:
        return "Vehicle sequencing data is not available right now.", {"intent": "VEHICLE_SEQUENCING", "error": "API unavailable"}
    rows = []
    total_active = 0
    for lp in loading_points:
        vehicles = lp.get("vehicles") or []
        active = [v for v in vehicles if _safe_float(v.get("total_weight"), 0.0) > 0 or _safe_int(v.get("count"), 0) > 0]
        total_active += len(active)
        rows.append({
            "Loading Point": _as_str(lp.get("name") or lp.get("loading_point")),
            "Active Vehicles": len(active),
            "Total Slots": len(vehicles),
        })
    html_out = f"Vehicle sequencing has <b>{len(loading_points)}</b> loading point(s) and <b>{total_active}</b> active vehicle(s)."
    if rows:
        html_out += "<br><br>" + _html_table(["Loading Point", "Active Vehicles", "Total Slots"], rows, max_rows=12)
    return html_out, {"intent": "VEHICLE_SEQUENCING", "loading_points": len(loading_points), "active_vehicles": total_active}


# =============================================================================
# Optional Gemini formatting/fallback
# =============================================================================

def _call_gemini(api_key: str, prompt: str, temperature: float = 0.0) -> Optional[str]:
    if not api_key or requests is None:
        return None
    models = ["gemini-2.5-flash", "gemini-1.5-flash-latest"]
    headers = {"Content-Type": "application/json"}
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        try:
            resp = requests.post(
                url,
                headers=headers,
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": temperature},
                },
                timeout=12,
            )
            if resp.status_code == 200:
                return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            continue
    return None


# =============================================================================
# Main chatbot answer router
# =============================================================================

def answer_question(question: str, *, client: Any = None, engine: Any = None) -> Tuple[str, Dict[str, Any]]:
    question = _as_str(question)
    if not question:
        return "Please ask me a question.", {"intent": "EMPTY"}

    static = _answer_static(question)
    if static:
        return static

    qn = _norm_text(question)

    # QR Code API / DB
    if any(x in qn for x in ["qr", "qr code", "barcode", "label"]):
        return _answer_qr(question, client=client)

    # Dispatch suggestions API
    if any(x in qn for x in ["dispatch", "ready for dispatch", "shipment", "truck lot", "full truck"]):
        return _answer_dispatch(question, client=client)

    # Bin Allocator API / DB
    if any(x in qn for x in ["bin allocator", "allocator", "allocated by bin", "waiting for bin", "unassigned"]):
        return _answer_allocator(question, client=client, engine=engine)

    # Vehicle sequencing API
    if any(x in qn for x in ["vehicle sequencing", "vehicle sequence", "loading point", "crane sequence"]):
        return _answer_vehicle_sequencing(client=client)

    # Everything else comes from Google Sheet / Excel data.
    return _answer_sheet_question(question)


# =============================================================================
# API routes exposed by chatbot.py
# =============================================================================

def _json_response(reply: str, meta: Dict[str, Any], ok: bool = True):
    # Keep the old frontend contract: reply + data. Extra meta is safe for newer UI.
    return jsonify({"ok": ok, "reply": reply, "data": [], "meta": meta})


def _route_exists(app: Any, endpoint: str) -> bool:
    try:
        return endpoint in app.view_functions
    except Exception:
        return False


def register_chatbot_routes(app, engine=None):
    """Register chatbot and helper APIs.

    Primary route:
      POST /api/chatbot/ask      JSON: {"message": "Where is batch 2518990ACA?"}

    Helper APIs are intentionally under /api/chatbot/* so they do not conflict
    with your existing dispatch, QR, allocator, dashboard, or sequencing APIs.
    """

    if not _route_exists(app, "chatbot_ask"):
        @app.post("/api/chatbot/ask", endpoint="chatbot_ask")
        def chatbot_ask():
            payload = request.get_json(silent=True) or request.form.to_dict() or {}
            user_msg = _as_str(payload.get("message") or payload.get("question") or payload.get("q"))
            if not user_msg:
                return _json_response("Please ask me a question.", {"intent": "EMPTY"}, ok=False)
            client = current_app.test_client()
            try:
                reply, meta = answer_question(user_msg, client=client, engine=engine)
                return _json_response(reply, meta, ok=True)
            except Exception as e:
                return _json_response(f"I could not process this question: {_escape(e)}", {"intent": "ERROR", "error": str(e)}, ok=False)

    if not _route_exists(app, "chatbot_health"):
        @app.get("/api/chatbot/health", endpoint="chatbot_health")
        def chatbot_health():
            rows = _fetch_sheet_rows()
            return jsonify({
                "ok": True,
                "rows_loaded": len(rows),
                "sheet": _sheet_source_info(),
                "bays": YARD_BAYS,
                "yard_bay_count": YARD_BAY_COUNT,
                "yard_bin_count": YARD_BIN_COUNT,
                "qr_db_path": QR_DB_PATH,
            })

    if not _route_exists(app, "chatbot_help"):
        @app.get("/api/chatbot/help", endpoint="chatbot_help")
        def chatbot_help():
            examples = [
                "How many bays are there in the yard?",
                "How many bins are there in the yard?",
                "Who made this software?",
                "Where is batch 2518990ACA?",
                "What is PaymentStatus for batch 2518990ACA?",
                "Show all details of material 2500550188.",
                "How many items are under SO Item 0808722225|000010?",
                "How many materials have only unique SO Item?",
                "How many customers are there in EF bay?",
                "Show CD bay summary.",
                "How many plates have Finished Status?",
                "How many plates are To be Levelled?",
                "Material status wise count.",
                "How many FI Released materials are there?",
                "How many QR codes were generated in A shift?",
                "How many QR codes were generated from 6am to 2pm?",
                "For how much time was QR code not generated?",
                "How many vehicles are ready for dispatch?",
                "How many plates are ready for dispatch?",
                "How many plates were allocated by Bin Allocator?",
            ]
            return jsonify({"ok": True, "examples": examples, "columns": ALL_COLUMNS})

    if not _route_exists(app, "chatbot_summary"):
        @app.get("/api/chatbot/summary", endpoint="chatbot_summary")
        def chatbot_summary():
            rows = _fetch_sheet_rows(force=request.args.get("refresh") == "1")
            return jsonify({"ok": True, "summary": _summary(rows), "sheet": _sheet_source_info()})

    if not _route_exists(app, "chatbot_material"):
        @app.get("/api/chatbot/material", endpoint="chatbot_material")
        def chatbot_material():
            term = _as_str(request.args.get("batch") or request.args.get("material") or request.args.get("id") or request.args.get("q"))
            rows = _fetch_sheet_rows()
            records = _find_material_records(rows, term) if term else []
            requested = request.args.get("columns")
            cols = [c.strip() for c in requested.split(",") if _canonical_column(c.strip())] if requested else DEFAULT_MATERIAL_COLUMNS
            cols = [_canonical_column(c) or c for c in cols]
            return jsonify({
                "ok": True,
                "query": term,
                "count": len(records),
                "records": [_row_to_columns(r, cols) for r in records[:100]],
            })

    if not _route_exists(app, "chatbot_so_item"):
        @app.get("/api/chatbot/so-item", endpoint="chatbot_so_item")
        def chatbot_so_item():
            so_item = _as_str(request.args.get("so_item") or request.args.get("q"))
            rows = _fetch_sheet_rows()
            if so_item:
                reply, meta = _answer_so_item(f"how many under so item {so_item}", rows)
            else:
                reply, meta = _answer_so_item("same so item summary", rows)
            return jsonify({"ok": True, "reply": reply, "meta": meta})

    if not _route_exists(app, "chatbot_bay_customers"):
        @app.get("/api/chatbot/bay/<bay>/customers", endpoint="chatbot_bay_customers")
        def chatbot_bay_customers(bay: str):
            rows = _fetch_sheet_rows()
            reply, meta = _answer_bay_customers(rows, bay)
            return jsonify({"ok": True, "reply": reply, "meta": meta})

    if not _route_exists(app, "chatbot_bay_summary"):
        @app.get("/api/chatbot/bay/<bay>/summary", endpoint="chatbot_bay_summary")
        def chatbot_bay_summary(bay: str):
            rows = _fetch_sheet_rows()
            reply, meta = _answer_bay_summary(rows, bay)
            return jsonify({"ok": True, "reply": reply, "meta": meta})

    if not _route_exists(app, "chatbot_qr_summary"):
        @app.get("/api/chatbot/qr/summary", endpoint="chatbot_qr_summary")
        def chatbot_qr_summary():
            client = current_app.test_client()
            reply, meta = _answer_qr("qr count", client=client)
            return jsonify({"ok": True, "reply": reply, "meta": meta})

    if not _route_exists(app, "chatbot_qr_gaps"):
        @app.get("/api/chatbot/qr/gaps", endpoint="chatbot_qr_gaps")
        def chatbot_qr_gaps():
            client = current_app.test_client()
            reply, meta = _answer_qr("qr not generated gaps", client=client)
            return jsonify({"ok": True, "reply": reply, "meta": meta})

    if not _route_exists(app, "chatbot_dispatch_summary"):
        @app.get("/api/chatbot/dispatch/summary", endpoint="chatbot_dispatch_summary")
        def chatbot_dispatch_summary():
            client = current_app.test_client()
            reply, meta = _answer_dispatch("dispatch summary", client=client)
            return jsonify({"ok": True, "reply": reply, "meta": meta})

    if not _route_exists(app, "chatbot_allocator_summary"):
        @app.get("/api/chatbot/allocator/summary", endpoint="chatbot_allocator_summary")
        def chatbot_allocator_summary():
            client = current_app.test_client()
            reply, meta = _answer_allocator("bin allocator summary", client=client, engine=engine)
            return jsonify({"ok": True, "reply": reply, "meta": meta})

    return app