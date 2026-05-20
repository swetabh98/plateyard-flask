from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone, timedelta
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

# =============================================================================
# Bin Allocator (Customer-first MixedLot-style Suggestion for UNASSIGNED)
#
# KEEP EVERYTHING SAME as your last working version, EXCEPT:
#
# NEW CHANGE (ONLY):
#   Bin numbering for ALL bays starts from column 34.
#   -> Do NOT consider / suggest any bin with col < 34 (EF01..EF33, etc.)
#
# Existing rules preserved:
#  - Suggest ONLY EF/AC/DE/CD (NO CTL)
#  - Coil -> ONLY AC39B-G .. AC47B-G
#  - Plates in AC bay -> ONLY from AC48B-G onwards (B-G rows)
#  - Thick plates (>=40mm) -> AC-only (but still AC48B-G onwards)
#  - Bin pools ONLY from full_yard_layout.json (no fabricated bins)
#  - MixedLot strongest-bin for existing customers
#  - If no existing customer: choose EMPTY bin from JSON pool
#  - Grouping: same (customer, type, thickness_bucket) shares same suggested bin
#
# ADDITIONS IMPLEMENTED NOW:
#  1) Only show items where pop_EN_DATE is today OR yesterday (UTC-based).
#  2) Cross-check Google Sheet: if material is already present in sheet -> do NOT show/suggest it.
#  3) Avoid duplicates across refresh: once a plate/coil is already fetched, do not fetch again.
#  4) Coil-bin capacity: bins AC39B-G..AC47B-G must not exceed 18 coils (assumes these bins contain coils only).
#  5) API returns "new_items" count for the current refresh window (so UI can show "X new plates added").
#
# FIX IMPLEMENTED NOW (ONLY):
#  ✅ Accepted/Rejected tables must NOT depend on /unassigned/suggest (which can be empty).
#  ✅ New endpoint: GET /api/allocator/status/details
#     - Returns all status rows (Accepted/Rejected), enriched with item details from merged table
#     - Also stores method/score/reasons at decision time so tables show full info.
#
# ENDPOINTS
#   GET  /api/allocator/state
#   GET  /api/allocator/unassigned/summary
#   GET  /api/allocator/unassigned/suggest?limit=ALL
#   GET  /api/allocator/mixedlot/suggest
#   GET  /api/allocator/bin_details?bin=AC39B   <-- reads from Google Sheet
#   GET  /api/allocator/status/list
#   GET  /api/allocator/status/details   <-- NEW
#   POST /api/allocator/status/set
#   POST /api/allocator/status/undo
#   POST /api/allocator/status/check
# =============================================================================


# -----------------------------------------------------------------------------#
# Time helpers
# -----------------------------------------------------------------------------#
_ISO_FMT_Z = "%Y-%m-%dT%H:%M:%SZ"


def _utc_now_iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime(_ISO_FMT_Z)


def _utc_today_ymd() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc_yesterday_ymd() -> str:
    # NOTE: kept as-is (original file behavior), not used by the date filter below
    return (
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
        - 86400
    )


def _as_str(x: Any) -> str:
    return "" if x is None else str(x).strip()


def _norm_space(x: Any) -> str:
    return " ".join(_as_str(x).split())


def _lower(x: Any) -> str:
    return _as_str(x).lower().strip()


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        if x is None or _as_str(x) == "":
            return default
        return int(float(_as_str(x)))
    except Exception:
        return default


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        s = _as_str(x)
        if not s:
            return default
        s = s.lower().replace("mm", "").strip()
        return float(s)
    except Exception:
        return default


# -----------------------------------------------------------------------------#
# Bin canonicalization (Only EF/AC/DE/CD)
# -----------------------------------------------------------------------------#
BIN_OK = re.compile(r"^(?P<bay>EF|AC|DE|CD)(?P<col>\d{2})(?P<row>[A-G])$", re.I)
BIN_WITH_STATUS = re.compile(
    r"^(?P<bay>EF|AC|DE|CD)(?P<col>\d{2})(?P<code>[A-Z]{1,8})(?P<row>[A-G])$",
    re.I,
)
BIN_COIL_FLAG = re.compile(r"^(?P<bay>EF|AC|DE|CD)(?P<col>\d{2})C(?P<row>[A-G])$", re.I)

_ALLOWED_BAYS = {"EF", "AC", "DE", "CD"}

# NEW: all bays start from col 34
MIN_BIN_COL = int(os.environ.get("MIN_BIN_COL", "34"))

# NEW: coil capacity per bin (AC39B-G..AC47B-G)
MAX_COILS_PER_COIL_BIN = int(os.environ.get("MAX_COILS_PER_COIL_BIN", "18"))


def _canon_bin(bin_code: str) -> str:
    s = _as_str(bin_code).upper().replace(" ", "").replace("\t", "")

    m = BIN_WITH_STATUS.match(s)
    if m:
        return f"{m.group('bay').upper()}{m.group('col')}{m.group('row').upper()}"

    m = BIN_COIL_FLAG.match(s)
    if m:
        return f"{m.group('bay').upper()}{m.group('col')}{m.group('row').upper()}"

    m = BIN_OK.match(s)
    if m:
        return f"{m.group('bay').upper()}{m.group('col')}{m.group('row').upper()}"

    m2 = re.match(r"^(EF|AC|DE|CD)[\s-]?(\d{2})[\s-]?([A-G])$", s, flags=re.I)
    if m2:
        return f"{m2.group(1).upper()}{m2.group(2)}{m2.group(3).upper()}"

    return s


def _canon_customer(c: str) -> str:
    s = _norm_space(c)
    while s.endswith(","):
        s = s[:-1].strip()
    return s.lower()


def _bin_bay(bin_code: str) -> str:
    b = _canon_bin(bin_code)
    return b[:2] if len(b) >= 2 else ""


def _bin_col(bin_code: str) -> Optional[int]:
    """
    Returns integer column for EF34A -> 34, else None if not a normal bin.
    """
    b = _canon_bin(bin_code)
    m = BIN_OK.match(b)
    if not m:
        return None
    try:
        return int(m.group("col"))
    except Exception:
        return None


def _bin_meets_min_col(bin_code: str) -> bool:
    c = _bin_col(bin_code)
    return (c is not None) and (c >= MIN_BIN_COL)


# -----------------------------------------------------------------------------#
# COIL RULE: only AC39B-G .. AC47B-G
# -----------------------------------------------------------------------------#
_COIL_ALLOWED_RE = re.compile(r"^AC(3[9]|4[0-7])[B-G]$", re.I)


def _coil_bin_allowed(bin_code: str) -> bool:
    return bool(_COIL_ALLOWED_RE.match(_canon_bin(bin_code)))


# -----------------------------------------------------------------------------#
# PLATE AC RULE: only AC48B-G onwards
# -----------------------------------------------------------------------------#
def _plate_ac_bin_allowed(bin_code: str) -> bool:
    b = _canon_bin(bin_code)
    m = BIN_OK.match(b)
    if not m:
        return False
    if m.group("bay").upper() != "AC":
        return False
    col = int(m.group("col"))
    row = m.group("row").upper()
    return (col >= 48) and (row in list("BCDEFG"))


# -----------------------------------------------------------------------------#
# Plate rule: thickness >= 40 => AC bay only
# -----------------------------------------------------------------------------#
THICK_PLATE_MM_THRESHOLD = float(os.environ.get("THICK_PLATE_MM_THRESHOLD", "40"))


def _is_thick_plate_mm(thk_mm: float) -> bool:
    return thk_mm >= THICK_PLATE_MM_THRESHOLD


# -----------------------------------------------------------------------------#
# Config
# -----------------------------------------------------------------------------#
DEFAULT_CONFIG: Dict[str, Any] = {
    "google_sheet_csv_url": os.environ.get(
        "YARD_SHEET_CSV_URL",
        "https://docs.google.com/spreadsheets/d/1TPt1wTmOFj4ydC_cGS59DGCf4enFGoe-9J3LW-nbRzE/export?format=csv",
    ),
    "sqlite_path": os.environ.get("YARD_SQLITE_DB", "yard_data.db"),
    "merged_table": os.environ.get("YARD_MERGED_TABLE", "oracle_ORDER_MERGED_T"),
    "layout_json_path": os.environ.get("YARD_LAYOUT_JSON", "full_yard_layout.json"),
    "engine_cache_seconds": int(os.environ.get("ALLOC_ENGINE_CACHE_SECONDS", "60")),
    # status checking interval (seconds). Requirement: check Google Sheet every 15 minutes.
    "status_check_seconds": int(os.environ.get("ALLOC_STATUS_CHECK_SECONDS", str(15 * 60))),
}


# -----------------------------------------------------------------------------#
# Status persistence (Accepted / Rejected) + compliance check
# -----------------------------------------------------------------------------#
_STATUS_TABLE = "allocator_status_decisions"


def _connect_sqlite(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _sqlite_table_columns(con: sqlite3.Connection, table: str) -> List[str]:
    cur = con.cursor()
    try:
        cur.execute(f'PRAGMA table_info("{table}")')
        return [r[1] for r in cur.fetchall()]
    finally:
        cur.close()


def _ensure_table_has_columns(con: sqlite3.Connection, table: str, cols: Dict[str, str]) -> None:
    """
    Adds missing columns (ALTER TABLE) if table already exists.
    This is additive-only to preserve existing data and logic.
    """
    existing = set(_sqlite_table_columns(con, table))
    cur = con.cursor()
    try:
        for col, ddl in cols.items():
            if col not in existing:
                cur.execute(f'ALTER TABLE "{table}" ADD COLUMN {ddl}')
        con.commit()
    finally:
        cur.close()


def _init_status_table(con: sqlite3.Connection) -> None:
    cur = con.cursor()
    try:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_STATUS_TABLE} (
              item_id TEXT PRIMARY KEY,
              status TEXT NOT NULL,                 -- 'Accepted' or 'Rejected'
              suggested_bin TEXT,
              decided_at_utc TEXT,
              followed INTEGER DEFAULT NULL,        -- NULL when unknown/not checked, 1 followed, 0 not followed
              current_bin TEXT,                     -- Actual bin from Google Sheet
              note TEXT
            )
            """
        )
        con.commit()
    finally:
        cur.close()

    # ✅ NEW (additive-only): store display fields so Accepted/Rejected tables don't vanish
    _ensure_table_has_columns(
        con,
        _STATUS_TABLE,
        {
            "item_type": 'item_type TEXT',
            "customer": 'customer TEXT',
            "grade": 'grade TEXT',
            "thickness_mm": 'thickness_mm REAL',
            "method": 'method TEXT',
            "score": 'score REAL',
            "reasons_json": 'reasons_json TEXT',
        },
    )


def _status_set(
    *,
    sqlite_path: str,
    item_id: str,
    status: str,
    suggested_bin: str,
    item_type: str = "",
    customer: str = "",
    grade: str = "",
    thickness_mm: Any = None,
    method: str = "",
    score: Any = None,
    reasons_json: str = "",
) -> None:
    item_id = _as_str(item_id)
    status = _as_str(status)
    suggested_bin = _canon_bin(_as_str(suggested_bin))

    if not item_id:
        raise ValueError("item_id is required")

    if status not in ("Accepted", "Rejected"):
        raise ValueError("status must be Accepted or Rejected")

    con = _connect_sqlite(sqlite_path)
    try:
        _init_status_table(con)
        cur = con.cursor()
        try:
            cur.execute(
                f"""
                INSERT INTO {_STATUS_TABLE}(
                  item_id, status, suggested_bin, decided_at_utc,
                  followed, current_bin, note,
                  item_type, customer, grade, thickness_mm, method, score, reasons_json
                )
                VALUES(?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                  status=excluded.status,
                  suggested_bin=excluded.suggested_bin,
                  decided_at_utc=excluded.decided_at_utc,
                  followed=NULL,
                  current_bin=NULL,
                  note=NULL,
                  item_type=excluded.item_type,
                  customer=excluded.customer,
                  grade=excluded.grade,
                  thickness_mm=excluded.thickness_mm,
                  method=excluded.method,
                  score=excluded.score,
                  reasons_json=excluded.reasons_json
                """,
                (
                    item_id,
                    status,
                    suggested_bin,
                    _utc_now_iso_z(),
                    _as_str(item_type),
                    _norm_space(customer),
                    _norm_space(grade),
                    None if thickness_mm in (None, "", "null") else float(thickness_mm),
                    _as_str(method),
                    None if score in (None, "", "null") else float(score),
                    _as_str(reasons_json),
                ),
            )
            con.commit()
        finally:
            cur.close()
    finally:
        con.close()


def _status_undo(*, sqlite_path: str, item_id: str) -> None:
    item_id = _as_str(item_id)
    if not item_id:
        return
    con = _connect_sqlite(sqlite_path)
    try:
        _init_status_table(con)
        cur = con.cursor()
        try:
            cur.execute(f'DELETE FROM "{_STATUS_TABLE}" WHERE item_id=?', (item_id,))
            con.commit()
        finally:
            cur.close()
    finally:
        con.close()


def _status_list(*, sqlite_path: str) -> List[Dict[str, Any]]:
    con = _connect_sqlite(sqlite_path)
    try:
        _init_status_table(con)
        cur = con.cursor()
        try:
            cur.execute(
                f"""
                SELECT item_id, status, suggested_bin, decided_at_utc, followed, current_bin, note,
                       item_type, customer, grade, thickness_mm, method, score, reasons_json
                FROM {_STATUS_TABLE}
                ORDER BY decided_at_utc DESC
                """
            )
            out: List[Dict[str, Any]] = []
            for r in cur.fetchall():
                out.append({k: r[k] for k in r.keys()})
            return out
        finally:
            cur.close()
    finally:
        con.close()


# -----------------------------------------------------------------------------#
# Seen-items persistence (avoid duplication across refresh)
# -----------------------------------------------------------------------------#
_SEEN_TABLE = "allocator_seen_items"


def _init_seen_table(con: sqlite3.Connection) -> None:
    cur = con.cursor()
    try:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_SEEN_TABLE} (
              item_id TEXT PRIMARY KEY,
              first_seen_at_utc TEXT
            )
            """
        )
        con.commit()
    finally:
        cur.close()


def _seen_get_set(sqlite_path: str, item_ids: List[str]) -> Tuple[set, int]:
    """
    Returns:
      (already_seen_set, newly_added_count)
    Also inserts new ones in the seen table.
    """
    norm_ids = [(_as_str(x).strip()) for x in (item_ids or []) if _as_str(x).strip()]
    if not norm_ids:
        return set(), 0

    con = _connect_sqlite(sqlite_path)
    try:
        _init_seen_table(con)
        cur = con.cursor()
        try:
            q_marks = ",".join(["?"] * len(norm_ids))
            cur.execute(f"SELECT item_id FROM {_SEEN_TABLE} WHERE item_id IN ({q_marks})", tuple(norm_ids))
            already = {str(r[0]) for r in (cur.fetchall() or [])}

            new_ids = [x for x in norm_ids if x not in already]
            if new_ids:
                payload = [(x, _utc_now_iso_z()) for x in new_ids]
                cur.executemany(
                    f"INSERT OR IGNORE INTO {_SEEN_TABLE}(item_id, first_seen_at_utc) VALUES(?, ?)",
                    payload,
                )
                con.commit()
            return already, len(new_ids)
        finally:
            cur.close()
    finally:
        con.close()


# -----------------------------------------------------------------------------#
# GoogleSheet -> item_id set / item->bin map
# -----------------------------------------------------------------------------#
def _fetch_csv_rows(url: str, timeout: int = 45) -> List[Dict[str, str]]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()

    txt = raw.decode("utf-8", errors="replace")
    head = txt[:200].lower()
    if "<html" in head and "google" in head:
        return []

    buf = io.StringIO(txt)
    return [{k: (v if v is not None else "") for k, v in r.items()} for r in csv.DictReader(buf)]


def _gget(row: Dict[str, str], key: str) -> Optional[str]:
    want = key.lower().replace(" ", "").replace("_", "")
    for k, v in row.items():
        kk = str(k).strip().lower().replace(" ", "").replace("_", "")
        if kk == want:
            return v
    return None


def _sheet_item_id_set(csv_url: str) -> set:
    rows = _fetch_csv_rows(csv_url)
    out = set()
    for r in rows:
        item = (
            _gget(r, "Material/Plate ID")
            or _gget(r, "MaterialPlateID")
            or _gget(r, "MaterialID")
            or _gget(r, "MaterialNo")
            or _gget(r, "MATERIAL_NO")
            or _gget(r, "Material")
            or _gget(r, "PlateID")
            or _gget(r, "ItemID")
            or _gget(r, "ITEM_ID")
            or ""
        )
        item = _as_str(item).strip()
        if item:
            out.add(item.lower())
    return out


def _sheet_item_bin_map(csv_url: str) -> Dict[str, str]:
    rows = _fetch_csv_rows(csv_url)
    m: Dict[str, str] = {}
    for r in rows:
        item = (
            _gget(r, "Material/Plate ID")
            or _gget(r, "MaterialPlateID")
            or _gget(r, "MaterialID")
            or _gget(r, "Material")
            or _gget(r, "PlateID")
            or _gget(r, "ItemID")
            or _gget(r, "ITEM_ID")
            or ""
        )
        item = _as_str(item)
        if not item:
            continue
        bin_raw = (
            _gget(r, "BinNo")
            or _gget(r, "BIN_NO")
            or _gget(r, "Bin")
            or _gget(r, "BIN")
            or ""
        )
        b = _canon_bin(_as_str(bin_raw))
        if not b:
            continue
        m[item.strip().lower()] = b
    return m


def _status_check_and_update(*, sqlite_path: str, csv_url: str) -> int:
    sheet_map = _sheet_item_bin_map(csv_url)
    con = _connect_sqlite(sqlite_path)
    try:
        _init_status_table(con)
        cur = con.cursor()
        try:
            cur.execute(f"SELECT item_id, suggested_bin FROM {_STATUS_TABLE} WHERE status='Accepted'")
            rows = cur.fetchall()
            updated = 0
            for r in rows:
                item_id = _as_str(r[0])
                sug = _canon_bin(_as_str(r[1]))
                cur_bin = sheet_map.get(item_id.strip().lower())
                if not cur_bin:
                    continue
                followed = 1 if _canon_bin(cur_bin) == sug else 0
                note = None if followed == 1 else "Allocation accepted but not followed"
                cur.execute(
                    f"""
                    UPDATE {_STATUS_TABLE}
                    SET followed=?, current_bin=?, note=?
                    WHERE item_id=?
                    """,
                    (followed, _canon_bin(cur_bin), note, item_id),
                )
                updated += 1
            con.commit()
            return updated
        finally:
            cur.close()
    finally:
        con.close()


# -----------------------------------------------------------------------------#
# Layout JSON -> valid bins
# -----------------------------------------------------------------------------#
def _load_layout_bins(layout_json_path: str) -> Dict[str, List[str]]:
    with open(layout_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    zones = data.get("zones") or []
    all_bins_raw: List[str] = []

    for z in zones:
        b = _canon_bin(_as_str(z.get("bin")))
        if not b:
            continue
        bay = _bin_bay(b)
        if bay not in _ALLOWED_BAYS:
            continue
        if not BIN_OK.match(b):
            continue
        if not _bin_meets_min_col(b):
            continue
        all_bins_raw.append(b)

    def _sort_key(x: str) -> Tuple[str, int, str]:
        m = BIN_OK.match(x)
        if not m:
            return (x[:2], 9999, x[-1:])
        return (m.group("bay").upper(), int(m.group("col")), m.group("row").upper())

    all_bins_raw = sorted(set(all_bins_raw), key=_sort_key)

    ac_any = [b for b in all_bins_raw if _bin_bay(b) == "AC"]
    non_ac = [b for b in all_bins_raw if _bin_bay(b) != "AC"]

    coil_bins = [b for b in ac_any if _coil_bin_allowed(b)]
    ac_plate_bins = [b for b in ac_any if _plate_ac_bin_allowed(b)]

    plate_all_pool = non_ac + ac_plate_bins
    plate_all_pool = sorted(set(plate_all_pool), key=_sort_key)

    return {
        "all": plate_all_pool,
        "ac_plate": ac_plate_bins,
        "ac_any": ac_any,
        "non_ac": non_ac,
        "coil": coil_bins,
    }


# -----------------------------------------------------------------------------#
# GoogleSheet occupancy (counts per customer per bin)
# -----------------------------------------------------------------------------#
def build_bin_customer_counts_from_sheet(csv_url: str) -> Tuple[
    Dict[str, Dict[str, int]],
    Dict[str, int],
]:
    rows = _fetch_csv_rows(csv_url)
    bcc: Dict[str, Dict[str, int]] = {}
    btc: Dict[str, int] = {}

    for r in rows:
        cust_raw = _as_str(_gget(r, "CustomerName") or _gget(r, "Customer") or "")
        bin_raw = _as_str(_gget(r, "BinNo") or _gget(r, "BIN_NO") or _gget(r, "BIN") or "")

        b = _canon_bin(bin_raw)
        if not b:
            continue

        bay = _bin_bay(b)
        if bay not in _ALLOWED_BAYS:
            continue
        if not BIN_OK.match(b):
            continue
        if not _bin_meets_min_col(b):
            continue

        ck = _canon_customer(cust_raw)
        if not ck:
            continue

        bcc.setdefault(b, {})
        bcc[b][ck] = int(bcc[b].get(ck, 0)) + 1
        btc[b] = int(btc.get(b, 0)) + 1

    return bcc, btc


def _pick_first_existing(columns: List[str], candidates: List[str]) -> str:
    s = set(columns)
    for c in candidates:
        if c in s:
            return c
    return ""


def _merged_bin_col(con: sqlite3.Connection, table: str) -> str:
    cols = _sqlite_table_columns(con, table)
    c = _pick_first_existing(cols, ["pop_BIN_NO", "BIN_NO", "bin_no", "BIN"])
    if not c:
        raise RuntimeError(f'BIN column not found in "{table}". Expected pop_BIN_NO / BIN_NO.')
    return c


def _merged_customer_col(con: sqlite3.Connection, table: str) -> str:
    cols = _sqlite_table_columns(con, table)
    return _pick_first_existing(cols, ["am_CUSTOMER_NAME", "CUSTOMER_NAME", "CUSTOMER", "pop_CUSTOMER_NAME"])


def _merged_route_col(con: sqlite3.Connection, table: str) -> str:
    cols = _sqlite_table_columns(con, table)
    return _pick_first_existing(cols, ["pop_ROUTE_NAME", "ROUTE_NAME", "ROUTE", "am_ROUTE_DESC"])


def _merged_product_col(con: sqlite3.Connection, table: str) -> str:
    cols = _sqlite_table_columns(con, table)
    return _pick_first_existing(cols, ["pop_PRODUCT_NAME", "PRODUCT_NAME", "PRODUCT"])


def _merged_grade_col(con: sqlite3.Connection, table: str) -> str:
    cols = _sqlite_table_columns(con, table)
    return _pick_first_existing(
        cols,
        ["pop_INTERNAL_GRADE", "pop_EXTERNAL_GRADE", "GRADE", "am_INTERNAL_GRADE", "am_EXTERNAL_GRADE"],
    )


def _merged_itemid_col(con: sqlite3.Connection, table: str) -> str:
    cols = _sqlite_table_columns(con, table)
    return _pick_first_existing(cols, ["pop_MATERIAL_NO", "ORDER_ID", "pop_ORDER_NO", "pop_RECORD_NO", "pop_SR_NO"])


def _merged_thickness_col(con: sqlite3.Connection, table: str) -> str:
    cols = _sqlite_table_columns(con, table)
    candidates = [
        "pop_THICKNESS",
        "THICKNESS",
        "THK",
        "THK_MM",
        "THICKNESS_MM",
        "pop_THK",
        "am_THICKNESS",
        "am_THK",
        "pop_SIZE_THK",
        "SIZE_THK",
    ]
    return _pick_first_existing(cols, candidates)


def _merged_endate_col(con: sqlite3.Connection, table: str) -> str:
    cols = _sqlite_table_columns(con, table)
    return _pick_first_existing(cols, ["pop_EN_DATE", "EN_DATE", "pop_ENDATE", "ENDATE", "pop_ENTRY_DATE", "ENTRY_DATE"])


def _date_ymd_from_any(v: Any) -> str:
    s = _as_str(v)
    if not s:
        return ""
    s = s.strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if m:
        dd = int(m.group(1))
        mm = int(m.group(2))
        yy = int(m.group(3))
        if yy < 100:
            yy = 2000 + yy
        return f"{yy:04d}-{mm:02d}-{dd:02d}"
    return ""


def _time_hms_from_any(v: Any) -> str:
    s = _as_str(v)
    if not s:
        return ""
    s = s.strip()

    m = re.search(r"(\d{2}):(\d{2}):(\d{2})", s)
    if m:
        return f"{m.group(1)}:{m.group(2)}:{m.group(3)}"

    m = re.search(r"(\d{2}):(\d{2})", s)
    if m:
        return f"{m.group(1)}:{m.group(2)}"

    return ""


def load_unassigned_from_merged_sqlite(
    *,
    sqlite_path: str,
    table: str,
    limit: Optional[int],
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    CHANGE APPLIED (as requested earlier):
      ✅ We NO LONGER filter rows by "bin column is blank".

    Existing NEW filters preserved:
      - only today OR yesterday by pop_EN_DATE (UTC day)
    """
    con = _connect_sqlite(sqlite_path)
    try:
        bin_col = _merged_bin_col(con, table)
        cur = con.cursor()
        try:
            sql = f"""
                SELECT *
                FROM "{table}"
                ORDER BY rowid DESC
            """
            params: Tuple[Any, ...] = ()
            if limit is not None:
                sql += " LIMIT ?"
                params = (int(limit),)

            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            cur.close()

        today = _utc_today_ymd()
        y_dt = datetime.now(timezone.utc) - timedelta(days=1)
        ymd_yesterday = y_dt.strftime("%Y-%m-%d")

        ed_col = _merged_endate_col(con, table)
        if ed_col:
            filtered = []
            for r in rows:
                ymd = _date_ymd_from_any(r.get(ed_col))
                if ymd in (today, ymd_yesterday):
                    filtered.append(r)
            rows = filtered

        return bin_col, rows
    finally:
        con.close()


# -----------------------------------------------------------------------------#
# MixedLot-style strongest bin per customer
# + coil capacity enforcement for best bin selection
# -----------------------------------------------------------------------------#
def build_best_bin_for_customer(
    bin_customer_counts: Dict[str, Dict[str, int]],
    *,
    item_type: str,
    only_bay: Optional[str] = None,
    bin_total_counts: Optional[Dict[str, int]] = None,
) -> Dict[str, Tuple[str, int]]:
    best: Dict[str, Tuple[str, int]] = {}

    for b, cc in (bin_customer_counts or {}).items():
        if not cc:
            continue

        bay = _bin_bay(b)
        if bay not in _ALLOWED_BAYS:
            continue
        if only_bay and bay != only_bay:
            continue
        if not BIN_OK.match(b):
            continue
        if not _bin_meets_min_col(b):
            continue

        if item_type == "Coil":
            if not _coil_bin_allowed(b):
                continue
            if bin_total_counts is not None:
                cur_cnt = _safe_int(bin_total_counts.get(_canon_bin(b), 0), 0)
                if cur_cnt >= MAX_COILS_PER_COIL_BIN:
                    continue
        else:
            if bay == "AC" and not _plate_ac_bin_allowed(b):
                continue

        for cust_key, cnt in cc.items():
            n = _safe_int(cnt, 0)
            if n <= 0:
                continue
            prev = best.get(cust_key)
            if (prev is None) or (n > prev[1]) or (n == prev[1] and b < prev[0]):
                best[cust_key] = (b, n)

    return best


def infer_item_type(route_val: str, prod_val: str) -> str:
    route = _lower(route_val)
    prod = _lower(prod_val)
    return "Coil" if ("coil" in route or "coil" in prod) else "Plate"


# -----------------------------------------------------------------------------#
# Engine cache (sheet + layout bins)
# -----------------------------------------------------------------------------#
_ENGINE_CACHE: Dict[str, Any] = {
    "built_at": 0.0,
    "sheet": None,   # (bcc, btc)
    "layout": None,
    "meta": {},
}


def _parse_limit_any(limit_s: str) -> Optional[int]:
    s = _as_str(limit_s).upper()
    if s in ("", "ALL", "0", "NONE"):
        return None
    try:
        n = int(float(s))
        return max(1, n)
    except Exception:
        return None


def _get_cached(cfg: Dict[str, Any]) -> Tuple[
    Dict[str, Dict[str, int]],
    Dict[str, int],
    Dict[str, List[str]],
    Dict[str, Any],
]:
    now = time.time()
    cache_seconds = int(cfg.get("engine_cache_seconds") or 60)

    if _ENGINE_CACHE.get("sheet") is not None and _ENGINE_CACHE.get("layout") is not None:
        if (now - float(_ENGINE_CACHE.get("built_at") or 0.0)) < cache_seconds:
            bcc, btc = _ENGINE_CACHE["sheet"]
            layout = _ENGINE_CACHE["layout"]
            return bcc, btc, layout, dict(_ENGINE_CACHE.get("meta") or {})

    csv_url = _as_str(cfg.get("google_sheet_csv_url"))
    layout_path = _as_str(cfg.get("layout_json_path"))

    bcc, btc = build_bin_customer_counts_from_sheet(csv_url)
    layout = _load_layout_bins(layout_path)

    meta = {
        "csv_url": csv_url,
        "layout_json_path": layout_path,
        "layout_bins_total": len(layout.get("all") or []),
        "layout_ac_plate_bins_total": len(layout.get("ac_plate") or []),
        "layout_coil_bins_total": len(layout.get("coil") or []),
        "sheet_occupied_bins": sum(1 for _, v in (btc or {}).items() if int(v or 0) > 0),
        "sheet_occupied_items": sum(int(v or 0) for v in (btc or {}).values()),
        "sheet_distinct_customers": len({c for d in (bcc or {}).values() for c in d.keys()}),
        "thick_plate_mm_threshold": THICK_PLATE_MM_THRESHOLD,
        "plate_ac_min_col": 48,
        "plate_ac_rows": "B-G",
        "min_bin_col": MIN_BIN_COL,
        "max_coils_per_bin": MAX_COILS_PER_COIL_BIN,
    }

    _ENGINE_CACHE["built_at"] = now
    _ENGINE_CACHE["sheet"] = (bcc, btc)
    _ENGINE_CACHE["layout"] = layout
    _ENGINE_CACHE["meta"] = meta

    return bcc, btc, layout, meta


# -----------------------------------------------------------------------------#
# Empty bin picker (ONLY from JSON bins)
# + coil capacity enforcement
# -----------------------------------------------------------------------------#
def _pick_empty_bin(
    *,
    item_type: str,
    is_thick_plate: bool,
    layout_bins: Dict[str, List[str]],
    occupied_bins: set,
    reserved_bins: set,
    bin_total_counts: Optional[Dict[str, int]] = None,
) -> str:
    if item_type == "Coil":
        pool = layout_bins.get("coil") or []
    else:
        pool = (layout_bins.get("ac_plate") or []) if is_thick_plate else (layout_bins.get("all") or [])

    for b in pool:
        cb = _canon_bin(b)
        if cb in occupied_bins:
            continue
        if cb in reserved_bins:
            continue

        if not BIN_OK.match(cb) or _bin_bay(cb) not in _ALLOWED_BAYS:
            continue
        if not _bin_meets_min_col(cb):
            continue

        if item_type == "Coil":
            if not _coil_bin_allowed(cb):
                continue
            if bin_total_counts is not None:
                cur_cnt = _safe_int(bin_total_counts.get(cb, 0), 0)
                if cur_cnt >= MAX_COILS_PER_COIL_BIN:
                    continue

        if item_type == "Plate":
            if _bin_bay(cb) == "AC" and not _plate_ac_bin_allowed(cb):
                continue
            if is_thick_plate and _bin_bay(cb) != "AC":
                continue

        return cb

    return ""


# -----------------------------------------------------------------------------#
# NEW: Build status "details" rows (Accepted/Rejected tables must never vanish)
# -----------------------------------------------------------------------------#
def _status_details_rows(*, sqlite_path: str, merged_table: str) -> List[Dict[str, Any]]:
    """
    Returns all status decisions (Accepted/Rejected), enriched with merged-table fields if missing.
    This ensures Accepted/Rejected tables remain visible even when /unassigned/suggest returns 0.
    """
    rows = _status_list(sqlite_path=sqlite_path)
    if not rows:
        return []

    con = _connect_sqlite(sqlite_path)
    try:
        itemid_col = _merged_itemid_col(con, merged_table)
        cust_col = _merged_customer_col(con, merged_table)
        route_col = _merged_route_col(con, merged_table)
        prod_col = _merged_product_col(con, merged_table)
        grade_col = _merged_grade_col(con, merged_table)
        thk_col = _merged_thickness_col(con, merged_table)
    finally:
        con.close()

    want_ids = [(_as_str(r.get("item_id")).strip()) for r in rows if _as_str(r.get("item_id")).strip()]
    if not want_ids:
        return rows

    # Lookup in merged table (best effort)
    merged_map: Dict[str, Dict[str, Any]] = {}
    con = _connect_sqlite(sqlite_path)
    try:
        cur = con.cursor()
        try:
            # SQLite max vars: keep chunked
            CHUNK = 800
            for i in range(0, len(want_ids), CHUNK):
                chunk = want_ids[i:i + CHUNK]
                q = ",".join(["?"] * len(chunk))
                cur.execute(
                    f'SELECT * FROM "{merged_table}" WHERE "{itemid_col}" IN ({q})',
                    tuple(chunk),
                )
                for rr in cur.fetchall():
                    d = dict(rr)
                    mid = _as_str(d.get(itemid_col)).strip()
                    if mid and mid not in merged_map:
                        merged_map[mid] = d
        finally:
            cur.close()
    finally:
        con.close()

    out: List[Dict[str, Any]] = []
    for r in rows:
        item_id = _as_str(r.get("item_id")).strip()
        m = merged_map.get(item_id, {}) if item_id else {}

        # Fill any missing display info from merged table
        itype = _as_str(r.get("item_type")) or infer_item_type(_as_str(m.get(route_col)), _as_str(m.get(prod_col)))
        customer = _as_str(r.get("customer")) or _norm_space(m.get(cust_col))
        grade = _as_str(r.get("grade")) or _norm_space(m.get(grade_col))
        thk = r.get("thickness_mm")
        if thk in (None, "", "null"):
            thk = _safe_float(m.get(thk_col), default=0.0) or None

        # Reasons
        reasons = []
        try:
            if _as_str(r.get("reasons_json")):
                reasons = json.loads(_as_str(r.get("reasons_json")))
                if not isinstance(reasons, list):
                    reasons = []
        except Exception:
            reasons = []

        out.append(
            {
                "item_id": item_id,
                "type": itype,
                "customer": customer,
                "grade": grade,
                "thickness_mm": thk,
                "suggested_bin": _as_str(r.get("suggested_bin")),
                "status": _as_str(r.get("status")),
                "followed": r.get("followed"),
                "note": _as_str(r.get("note")),
                "actual_bin": _as_str(r.get("current_bin")),  # UI expects actual_bin
                "method": _as_str(r.get("method")),
                "score": r.get("score"),
                "reasons": reasons,
                "ok": True,
            }
        )
    return out


# -----------------------------------------------------------------------------#
# Unassigned candidate preparation
# -----------------------------------------------------------------------------#
def _get_unassigned_candidate_rows(
    *,
    sqlite_path: str,
    table: str,
    csv_url: str,
    limit: Optional[int],
) -> Tuple[str, List[Dict[str, Any]], int]:
    """
    Returns:
      (bin_col, candidate_rows, new_items_count)

    Candidate rows are the rows that should actually be shown in Unassigned:
      - today/yesterday only (already handled by load_unassigned_from_merged_sqlite)
      - not already present in Google Sheet
      - not already Accepted/Rejected

    Seen-items table is used only to compute new_items_count. It does NOT hide rows.
    """
    sheet_items = _sheet_item_id_set(csv_url)
    bin_col, rows = load_unassigned_from_merged_sqlite(sqlite_path=sqlite_path, table=table, limit=limit)

    con = _connect_sqlite(sqlite_path)
    try:
        itemid_col = _merged_itemid_col(con, table)
    finally:
        con.close()

    decided_ids = {
        _as_str(r.get("item_id")).strip().lower()
        for r in _status_list(sqlite_path=sqlite_path)
        if _as_str(r.get("status")) in ("Accepted", "Rejected")
        and _as_str(r.get("item_id")).strip()
    }

    candidate_rows: List[Dict[str, Any]] = []
    item_ids_all: List[str] = []

    for r in rows:
        item_id = _as_str(r.get(itemid_col) if itemid_col else "").strip()
        if not item_id:
            continue

        item_id_l = item_id.lower()
        if item_id_l in sheet_items:
            continue
        if item_id_l in decided_ids:
            continue

        candidate_rows.append(r)
        item_ids_all.append(item_id)

    _already_seen, newly_added = _seen_get_set(sqlite_path, item_ids_all)
    return bin_col, candidate_rows, int(newly_added)


# -----------------------------------------------------------------------------#
# Flask API registration
# -----------------------------------------------------------------------------#
def register_bin_allocator_api(
    app,
    *,
    get_active_bin_entries: Callable[[], Dict[str, List[Dict[str, Any]]]] = None,
    zones_provider: Callable[[], List[Dict[str, Any]]] = None,
    anchors_provider: Optional[Callable[[List[Dict[str, Any]]], Dict[str, List[Dict[str, float]]]]] = None,
    config_provider: Optional[Callable[[], Dict[str, Any]]] = None,
    engine=None,
    _exec=None,
):
    _bg_started = {"v": False}

    def _get_cfg() -> Dict[str, Any]:
        cfg = dict(DEFAULT_CONFIG)
        if config_provider:
            try:
                cfg.update(config_provider() or {})
            except Exception:
                pass
        return cfg

    @app.get("/api/allocator/state")
    def api_allocator_state():
        cfg = _get_cfg()
        _, _, _, meta = _get_cached(cfg)
        return {
            "ok": True,
            "at": _utc_now_iso_z(),
            "sqlite_db": _as_str(cfg.get("sqlite_path")),
            "sqlite_table": _as_str(cfg.get("merged_table")),
            "status_table": _STATUS_TABLE,
            "seen_table": _SEEN_TABLE,
            **(meta or {}),
        }

    # ------------------------------------------------------------------
    # Status APIs
    # ------------------------------------------------------------------
    @app.get("/api/allocator/status/list")
    def api_status_list():
        cfg = _get_cfg()
        sqlite_path = _as_str(cfg.get("sqlite_path"))
        return {"ok": True, "at": _utc_now_iso_z(), "rows": _status_list(sqlite_path=sqlite_path)}

    # ✅ NEW: status/details (Accepted/Rejected tables are built from this)
    @app.get("/api/allocator/status/details")
    def api_status_details():
        cfg = _get_cfg()
        sqlite_path = _as_str(cfg.get("sqlite_path"))
        table = _as_str(cfg.get("merged_table"))
        rows = _status_details_rows(sqlite_path=sqlite_path, merged_table=table)
        return {"ok": True, "at": _utc_now_iso_z(), "rows": rows}

    @app.post("/api/allocator/status/set")
    def api_status_set():
        from flask import request

        cfg = _get_cfg()
        sqlite_path = _as_str(cfg.get("sqlite_path"))
        payload = request.get_json(silent=True) or {}

        _status_set(
            sqlite_path=sqlite_path,
            item_id=_as_str(payload.get("item_id")),
            status=_as_str(payload.get("status")),
            suggested_bin=_as_str(payload.get("suggested_bin")),
            item_type=_as_str(payload.get("item_type")),
            customer=_as_str(payload.get("customer")),
            grade=_as_str(payload.get("grade")),
            thickness_mm=payload.get("thickness_mm"),
            method=_as_str(payload.get("method")),
            score=payload.get("score"),
            reasons_json=_as_str(payload.get("reasons_json")),
        )
        return {"ok": True, "at": _utc_now_iso_z()}

    @app.post("/api/allocator/status/undo")
    def api_status_undo():
        from flask import request

        cfg = _get_cfg()
        sqlite_path = _as_str(cfg.get("sqlite_path"))
        payload = request.get_json(silent=True) or {}
        _status_undo(sqlite_path=sqlite_path, item_id=_as_str(payload.get("item_id")))
        return {"ok": True, "at": _utc_now_iso_z()}

    @app.post("/api/allocator/status/check")
    def api_status_check():
        cfg = _get_cfg()
        sqlite_path = _as_str(cfg.get("sqlite_path"))
        csv_url = _as_str(cfg.get("google_sheet_csv_url"))
        n = _status_check_and_update(sqlite_path=sqlite_path, csv_url=csv_url)
        return {"ok": True, "at": _utc_now_iso_z(), "updated": n}

    @app.get("/api/allocator/unassigned/summary")
    def api_unassigned_summary():
        cfg = _get_cfg()
        sqlite_path = _as_str(cfg.get("sqlite_path"))
        table = _as_str(cfg.get("merged_table"))
        csv_url = _as_str(cfg.get("google_sheet_csv_url"))

        bin_col, rows, _new_items = _get_unassigned_candidate_rows(
            sqlite_path=sqlite_path,
            table=table,
            csv_url=csv_url,
            limit=None,
        )

        by_type: Dict[str, int] = {"Plate": 0, "Coil": 0}
        by_customer: Dict[str, int] = {}

        con = _connect_sqlite(sqlite_path)
        try:
            cust_col = _merged_customer_col(con, table)
            route_col = _merged_route_col(con, table)
            prod_col = _merged_product_col(con, table)
        finally:
            con.close()

        for r in rows:
            cust = _norm_space(r.get(cust_col) if cust_col else "") or "UNKNOWN"
            itype = infer_item_type(
                r.get(route_col) if route_col else "",
                r.get(prod_col) if prod_col else "",
            )
            by_type[itype] = by_type.get(itype, 0) + 1
            by_customer[cust] = by_customer.get(cust, 0) + 1

        top_customers = sorted(by_customer.items(), key=lambda x: (-x[1], x[0]))[:50]

        return {
            "ok": True,
            "at": _utc_now_iso_z(),
            "sqlite_db": sqlite_path,
            "table": table,
            "bin_col": bin_col,
            "unassigned_rows": len(rows),
            "by_type": by_type,
            "distinct_customers_unassigned": len(by_customer),
            "top_customers": [{"customer": k, "rows": v} for k, v in top_customers],
        }

    @app.get("/api/allocator/unassigned/suggest")
    def api_unassigned_suggest():
        from flask import request

        cfg = _get_cfg()
        sqlite_path = _as_str(cfg.get("sqlite_path"))
        table = _as_str(cfg.get("merged_table"))
        csv_url = _as_str(cfg.get("google_sheet_csv_url"))

        limit_raw = request.args.get("limit") or "ALL"
        limit = _parse_limit_any(limit_raw)

        bcc, btc, layout_bins, meta = _get_cached(cfg)

        occupied_bins = {
            _canon_bin(b)
            for b in (btc or {}).keys()
            if BIN_OK.match(_canon_bin(b))
            and _bin_bay(_canon_bin(b)) in _ALLOWED_BAYS
            and _bin_meets_min_col(_canon_bin(b))
        }

        bin_col, rows2, newly_added = _get_unassigned_candidate_rows(
            sqlite_path=sqlite_path,
            table=table,
            csv_url=csv_url,
            limit=limit,
        )

        con = _connect_sqlite(sqlite_path)
        try:
            cust_col = _merged_customer_col(con, table)
            route_col = _merged_route_col(con, table)
            prod_col = _merged_product_col(con, table)
            grade_col = _merged_grade_col(con, table)
            itemid_col = _merged_itemid_col(con, table)
            thk_col = _merged_thickness_col(con, table)
            ed_col = _merged_endate_col(con, table)
        finally:
            con.close()

        best_plate_any = build_best_bin_for_customer(bcc, item_type="Plate", only_bay=None, bin_total_counts=btc)
        best_plate_ac = build_best_bin_for_customer(bcc, item_type="Plate", only_bay="AC", bin_total_counts=btc)
        best_coil = build_best_bin_for_customer(bcc, item_type="Coil", only_bay="AC", bin_total_counts=btc)

        assigned_for_group: Dict[Tuple[str, str, str], str] = {}
        reserved_bins: set = set()
        suggestions: List[Dict[str, Any]] = []

        for r in rows2:
            cust_raw = r.get(cust_col) if cust_col else ""
            cust_key = _canon_customer(_as_str(cust_raw))

            route_v = r.get(route_col) if route_col else ""
            prod_v = r.get(prod_col) if prod_col else ""
            itype = infer_item_type(route_v, prod_v)

            item_id = _as_str(r.get(itemid_col) if itemid_col else "") or "(unknown)"
            grade = _norm_space(_as_str(r.get(grade_col) if grade_col else ""))

            thk_mm = _safe_float(r.get(thk_col) if thk_col else "", default=0.0)
            is_thick_plate = (itype == "Plate") and _is_thick_plate_mm(thk_mm)
            thickness_bucket = "THK_GE_40" if is_thick_plate else "THK_LT_40"

            group_key = (cust_key, itype, thickness_bucket)

            suggested_bin = ""
            score = 0
            method = ""
            reasons: List[str] = []
            warnings: List[str] = []

            if group_key in assigned_for_group:
                suggested_bin = assigned_for_group[group_key]
                method = "grouped_customer_bin"
                score = 1
                reasons.append(
                    f"Grouped allocation: all {itype} items for this customer ({thickness_bucket}) share bin {suggested_bin}."
                )
            else:
                if itype == "Coil":
                    best = best_coil.get(cust_key)
                    if best:
                        suggested_bin, score = best
                        method = "mixedlot_customer_first_coil"
                        reasons.append(
                            f"Customer strongest COIL bin (restricted AC39B..AC47G): {suggested_bin} (count={score})."
                        )
                    else:
                        empty = _pick_empty_bin(
                            item_type="Coil",
                            is_thick_plate=False,
                            layout_bins=layout_bins,
                            occupied_bins=occupied_bins,
                            reserved_bins=reserved_bins,
                            bin_total_counts=btc,
                        )
                        if empty:
                            suggested_bin = empty
                            score = 0
                            method = "empty_bin_new_customer_coil"
                            reasons.append(
                                f"Customer has no existing COIL bin; assigned empty allowed coil bin {suggested_bin}."
                            )
                            reserved_bins.add(suggested_bin)
                        else:
                            method = "no_empty_coil_bin_available"
                            reasons.append(
                                f"No empty coil bin available in AC39B..AC47G (or all reached {MAX_COILS_PER_COIL_BIN} capacity)."
                            )

                else:
                    if is_thick_plate:
                        best = best_plate_ac.get(cust_key)
                        if best:
                            suggested_bin, score = best
                            method = "mixedlot_customer_first_plate_thick_ac"
                            reasons.append(
                                f"Plate thickness {thk_mm:g}mm >= {THICK_PLATE_MM_THRESHOLD:g}mm -> AC-only. "
                                f"Customer strongest PLATE bin in AC (AC48B-G..end): {suggested_bin} (count={score})."
                            )
                        else:
                            empty = _pick_empty_bin(
                                item_type="Plate",
                                is_thick_plate=True,
                                layout_bins=layout_bins,
                                occupied_bins=occupied_bins,
                                reserved_bins=reserved_bins,
                                bin_total_counts=btc,
                            )
                            if empty:
                                suggested_bin = empty
                                score = 0
                                method = "empty_bin_new_customer_plate_thick_ac"
                                reasons.append(
                                    f"Plate thickness {thk_mm:g}mm >= {THICK_PLATE_MM_THRESHOLD:g}mm -> AC-only. "
                                    f"No existing AC bin for customer; assigned empty AC bin {suggested_bin} (AC48B-G..end)."
                                )
                                reserved_bins.add(suggested_bin)
                            else:
                                method = "no_empty_plate_ac_bin_available"
                                reasons.append(
                                    "Thick plate requires AC bay (AC48B-G..end), but no empty AC plate bin available (from layout JSON)."
                                )
                    else:
                        best = best_plate_any.get(cust_key)
                        if best:
                            suggested_bin, score = best
                            method = "mixedlot_customer_first_plate"
                            reasons.append(f"Customer strongest PLATE bin: {suggested_bin} (count={score}).")
                        else:
                            empty = _pick_empty_bin(
                                item_type="Plate",
                                is_thick_plate=False,
                                layout_bins=layout_bins,
                                occupied_bins=occupied_bins,
                                reserved_bins=reserved_bins,
                                bin_total_counts=btc,
                            )
                            if empty:
                                suggested_bin = empty
                                score = 0
                                method = "empty_bin_new_customer_plate"
                                reasons.append(f"Customer has no existing PLATE bin; assigned empty bin {suggested_bin}.")
                                reserved_bins.add(suggested_bin)
                            else:
                                method = "no_empty_plate_bin_available"
                                reasons.append("No empty plate bin available (from layout JSON).")

                if suggested_bin:
                    cb = _canon_bin(suggested_bin)

                    if not BIN_OK.match(cb) or _bin_bay(cb) not in _ALLOWED_BAYS:
                        warnings.append(f"Suggested bin rejected (not a valid EF/AC/DE/CD bin): {cb}")
                        suggested_bin = ""
                    elif not _bin_meets_min_col(cb):
                        warnings.append(f"Suggested bin rejected (min col {MIN_BIN_COL}): {cb}")
                        suggested_bin = ""
                    elif itype == "Coil":
                        if not _coil_bin_allowed(cb):
                            warnings.append(f"Suggested bin rejected (coil restriction AC39B..AC47G): {cb}")
                            suggested_bin = ""
                        else:
                            cur_cnt = _safe_int(btc.get(cb, 0), 0)
                            if cur_cnt >= MAX_COILS_PER_COIL_BIN:
                                warnings.append(
                                    f"Suggested bin rejected (coil capacity {MAX_COILS_PER_COIL_BIN} reached): {cb} (current={cur_cnt})."
                                )
                                suggested_bin = ""
                    elif itype == "Plate":
                        if _bin_bay(cb) == "AC" and not _plate_ac_bin_allowed(cb):
                            warnings.append(f"Suggested bin rejected (plate AC must be AC48B-G..end): {cb}")
                            suggested_bin = ""
                        elif is_thick_plate and _bin_bay(cb) != "AC":
                            warnings.append(f"Suggested bin rejected (thick plate AC-only): {cb}")
                            suggested_bin = ""

                    if suggested_bin:
                        assigned_for_group[group_key] = cb
                        suggested_bin = cb

            pop_en_date_raw = r.get(ed_col) if ed_col else ""
            production_date = _date_ymd_from_any(pop_en_date_raw)
            production_time = _time_hms_from_any(pop_en_date_raw)

            suggestions.append(
                {
                    "item_id": item_id,
                    "type": itype,
                    "customer": _norm_space(cust_raw),
                    "grade": grade,
                    "production_date": production_date,
                    "production_time": production_time,
                    "thickness_mm": thk_mm if thk_mm else None,
                    "current_bin": "",
                    "suggested_bin": suggested_bin,
                    "method": method,
                    "ok": bool(suggested_bin),
                    "score": float(score),
                    "reasons": reasons,
                    "warnings": warnings,
                    "status": "",
                    "followed": None,
                    "note": "",
                }
            )

        # ✅ NEW: Gemini AI Summary Integration
        ai_summary = ""
        api_key = os.environ.get("GEMINI_API_KEY")
        if api_key and suggestions:
            try:
                method_counts = {}
                for s in suggestions:
                    m = str(s.get("method") or "unknown")
                    method_counts[m] = method_counts.get(m, 0) + 1
                
                prompt_text = (
                    f"You are an expert logistics AI assistant for a steel yard. We just ran the automated bin allocation algorithm. "
                    f"Total unassigned items processed: {len(suggestions)}. "
                    f"Breakdown of allocation methods used by the system: {json.dumps(method_counts)}. "
                    "Please provide a detailed, professional summary for the yard manager explaining how these items were allocated. "
                    "Include insights on the primary strategies used (e.g., grouping by customer, routing coils to specific AC bins, "
                    "or handling thick plates). Use a short introductory paragraph followed by a brief bulleted list of key allocation highlights."
                )

                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
                req_data = json.dumps({"contents": [{"parts": [{"text": prompt_text}]}]}).encode('utf-8')
                req = urllib.request.Request(url, data=req_data, headers={'Content-Type': 'application/json'}, method='POST')
                
                with urllib.request.urlopen(req, timeout=15) as response:
                    res_body = json.loads(response.read().decode('utf-8'))
                    ai_summary = res_body['candidates'][0]['content']['parts'][0]['text']
            except Exception as e:
                ai_summary = f"Failed to connect to AI: {str(e)}"
        elif not suggestions:
            ai_summary = "No unassigned items require allocation at this time."
        else:
            ai_summary = "AI API Key not configured."

        return {
            "ok": True,
            "at": _utc_now_iso_z(),
            "sqlite_db": sqlite_path,
            "table": table,
            "bin_col": bin_col,
            "requested_limit": limit_raw,
            "returned": len(suggestions),
            "new_items": int(newly_added),
            **(meta or {}),
            "suggestions": suggestions,
            "ai_summary": ai_summary, # ✅ Included the new AI summary
        }

    # ------------------------------------------------------------------
    # Background checker (every 15 minutes by default)
    # ------------------------------------------------------------------
    def _bg_loop():
        while True:
            try:
                cfg = _get_cfg()
                sqlite_path = _as_str(cfg.get("sqlite_path"))
                csv_url = _as_str(cfg.get("google_sheet_csv_url"))
                _status_check_and_update(sqlite_path=sqlite_path, csv_url=csv_url)
            except Exception:
                pass
            try:
                cfg = _get_cfg()
                time.sleep(int(cfg.get("status_check_seconds") or (15 * 60)))
            except Exception:
                time.sleep(15 * 60)

    if not _bg_started["v"]:
        _bg_started["v"] = True
        threading.Thread(target=_bg_loop, name="allocator_status_checker", daemon=True).start()

    # -------------------------------------------------------------------------
    # Bin Details (Google Sheet)
    # -------------------------------------------------------------------------
    @app.get("/api/allocator/bin_details")
    def api_allocator_bin_details():
        from flask import request

        cfg = _get_cfg()
        csv_url = _as_str(cfg.get("google_sheet_csv_url"))

        bin_q = _as_str(request.args.get("bin") or "")
        bin_canon = _canon_bin(bin_q)

        if not bin_canon or not BIN_OK.match(bin_canon) or _bin_bay(bin_canon) not in _ALLOWED_BAYS:
            return {"ok": False, "error": "Invalid bin"}, 400

        sheet_rows = _fetch_csv_rows(csv_url)

        def pick_any(row, keys):
            for k in keys:
                v = _gget(row, k)
                if v is not None and _as_str(v):
                    return _as_str(v)
            return ""

        items = []
        for r in sheet_rows:
            b_raw = pick_any(r, ["BinNo", "BIN_NO", "BIN", "Bin", "pop_BIN_NO"])
            if not b_raw:
                continue
            if _canon_bin(b_raw) != bin_canon:
                continue

            cust = pick_any(r, ["CustomerName", "Customer", "CUSTOMER_NAME", "am_CUSTOMER_NAME"])
            route_v = pick_any(r, ["RouteName", "ROUTE_NAME", "ROUTE", "pop_ROUTE_NAME", "am_ROUTE_DESC"])
            prod_v = pick_any(r, ["ProductName", "PRODUCT_NAME", "PRODUCT", "pop_PRODUCT_NAME"])
            grade = pick_any(r, ["Grade", "INTERNAL_GRADE", "EXTERNAL_GRADE", "pop_INTERNAL_GRADE", "pop_EXTERNAL_GRADE"])
            item_id = pick_any(
                r,
                [
                    "MaterialNo",
                    "Material",
                    "Material/Plate ID",
                    "MATERIAL_NO",
                    "pop_MATERIAL_NO",
                    "ORDER_ID",
                    "pop_ORDER_NO",
                    "pop_RECORD_NO",
                    "pop_SR_NO",
                ],
            )
            thk_s = pick_any(
                r,
                [
                    "Thickness",
                    "THICKNESS",
                    "THK",
                    "THK_MM",
                    "THICKNESS_MM",
                    "pop_THICKNESS",
                    "am_THICKNESS",
                    "am_THK",
                    "pop_SIZE_THK",
                    "SIZE_THK",
                ],
            )

            itype = infer_item_type(route_v, prod_v)

            items.append(
                {
                    "item_id": item_id,
                    "type": itype,
                    "customer": _norm_space(cust),
                    "grade": _norm_space(grade),
                    "thickness_mm": _safe_float(thk_s, default=0.0) or None,
                    "bin": bin_canon,
                }
            )

        return {
            "ok": True,
            "at": _utc_now_iso_z(),
            "source": "google_sheet",
            "csv_url": csv_url,
            "bin": bin_canon,
            "count": len(items),
            "items": items,
        }

    # -------------------------------------------------------------------------
    # Mixed-lot consolidation suggestions (kept)
    # -------------------------------------------------------------------------
    @app.get("/api/allocator/mixedlot/suggest")
    def api_mixedlot_suggest():
        from flask import request

        cfg = _get_cfg()
        bcc, btc, layout_bins, meta = _get_cached(cfg)

        min_customers = _parse_limit_any(request.args.get("min_customers") or "2") or 2
        min_items_in_bin = _parse_limit_any(request.args.get("min_items_in_bin") or "1") or 1
        limit_raw = request.args.get("limit") or "ALL"
        limit = _parse_limit_any(limit_raw)

        best_for_customer: Dict[str, Tuple[str, int]] = {}
        for b, cust_counts in (bcc or {}).items():
            bb = _canon_bin(b)
            if not BIN_OK.match(bb):
                continue
            if _bin_bay(bb) not in _ALLOWED_BAYS:
                continue
            if not _bin_meets_min_col(bb):
                continue
            if not cust_counts:
                continue

            for cust_key, cnt in cust_counts.items():
                n = _safe_int(cnt, 0)
                if n <= 0:
                    continue
                prev = best_for_customer.get(cust_key)
                if (prev is None) or (n > prev[1]) or (n == prev[1] and bb < prev[0]):
                    best_for_customer[cust_key] = (bb, n)

        suggestions: List[Dict[str, Any]] = []
        for src_bin, cust_counts in sorted((bcc or {}).items()):
            src_bin = _canon_bin(src_bin)
            if not BIN_OK.match(src_bin):
                continue
            if _bin_bay(src_bin) not in _ALLOWED_BAYS:
                continue
            if not _bin_meets_min_col(src_bin):
                continue
            if not cust_counts:
                continue

            present = [(c, _safe_int(v, 0)) for c, v in cust_counts.items() if _safe_int(v, 0) > 0]
            if len(present) < int(min_customers):
                continue

            total_here = _safe_int(btc.get(src_bin, 0), 0)
            if total_here < int(min_items_in_bin):
                continue

            for cust_key, here_cnt in sorted(present, key=lambda x: (-x[1], x[0])):
                best = best_for_customer.get(cust_key)
                if not best:
                    continue
                tgt_bin, tgt_cnt = best
                if tgt_bin == src_bin:
                    continue
                if int(tgt_cnt) <= int(here_cnt):
                    continue

                suggestions.append(
                    {
                        "customer_key": cust_key,
                        "from_bin": src_bin,
                        "to_bin": tgt_bin,
                        "here_count": int(here_cnt),
                        "there_count": int(tgt_cnt),
                        "reason": "Target bin has highest count for this customer in yard (sheet).",
                    }
                )

                if limit is not None and len(suggestions) >= limit:
                    break

            if limit is not None and len(suggestions) >= limit:
                break

        return {
            "ok": True,
            "at": _utc_now_iso_z(),
            "requested_limit": limit_raw,
            "returned": len(suggestions),
            **(meta or {}),
            "suggestions": suggestions,
        }

    return None