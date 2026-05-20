# app.py
from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import threading
import uuid
import time
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool
from werkzeug.security import check_password_hash, generate_password_hash

# ✅ NEW: For IIS/ARR SSL termination support (X-Forwarded-Proto / Host)
from werkzeug.middleware.proxy_fix import ProxyFix

from admin import init_admin, register_admin_routes




# -----------------------------------------------------------------------------
# Timezone + timestamp helpers (Google Sheet timestamps -> UTC ISO Z)
# -----------------------------------------------------------------------------
try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:
    ZoneInfo = None

SHEET_LOCAL_TZ_NAME = os.getenv("SHEET_LOCAL_TZ", "Asia/Kolkata")
SHEET_LOCAL_TZ = (
    ZoneInfo(SHEET_LOCAL_TZ_NAME)
    if ZoneInfo
    else timezone(timedelta(hours=5, minutes=30))
)

_ISO_FMT_Z = "%Y-%m-%dT%H:%M:%SZ"


def utc_now_iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime(_ISO_FMT_Z)


def utc_today_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _try_parse_with_formats(s: str):
    s = (s or "").strip()
    if not s or s.lower() == "null":
        return None

    s2 = s.replace("\\", "/").replace("|", " ").replace(",", " ").strip()

    fmts = [
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%d-%m-%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y",
    ]
    for f in fmts:
        try:
            return datetime.strptime(s2, f)
        except Exception:
            pass

    try:
        return datetime.fromisoformat(s2.replace("Z", "+00:00"))
    except Exception:
        return None


def to_iso_utc_z(val) -> str | None:
    if val is None:
        return None

    if isinstance(val, datetime):
        dtv = val if val.tzinfo else val.replace(tzinfo=SHEET_LOCAL_TZ)
        return dtv.astimezone(timezone.utc).replace(microsecond=0).strftime(_ISO_FMT_Z)

    s = str(val).strip()
    if not s:
        return None

    dtp = _try_parse_with_formats(s)
    if not dtp:
        return None

    if dtp.tzinfo is None:
        dtp = dtp.replace(tzinfo=SHEET_LOCAL_TZ)

    return dtp.astimezone(timezone.utc).replace(microsecond=0).strftime(_ISO_FMT_Z)


def pick_event_time(before: dict | None, after: dict | None, default_iso: str) -> str:
    before = before or {}
    after = after or {}
    for blob in (after, before):
        for k in ("added_at", "created_at", "Created On", "createdOn"):
            if blob.get(k):
                iso = to_iso_utc_z(blob.get(k))
                if iso:
                    return iso
        if blob.get("event_time"):
            iso = to_iso_utc_z(blob.get("event_time"))
            if iso:
                return iso
    return to_iso_utc_z(default_iso) or utc_now_iso_z()


# -----------------------------------------------------------------------------
# Small text normalization helpers
# -----------------------------------------------------------------------------
def as_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    try:
        import math

        if isinstance(v, float) and math.isnan(v):
            return ""
    except Exception:
        pass
    return str(v).strip()


def none_if_blank(v):
    s = as_str(v)
    return s if s else None


def normalize_space(v):
    s = as_str(v)
    if not s:
        return None
    return re.sub(r"\s+", " ", s).strip()


def normalize_fi_rel_text(v):
    s = normalize_space(v)
    if not s:
        return None
    m = re.search(r"\bFI\s*RELEASED\b", s, flags=re.IGNORECASE)
    if not m:
        return s
    vmatch = re.search(r"\((\d+)\)", s) or re.search(
        r"\bFI\s*RELEASED\s*(\d+)\b", s, flags=re.IGNORECASE
    )
    if vmatch:
        return f"FI Released ({vmatch.group(1)})"
    return "FI Released"


def to_float(v):
    try:
        s = as_str(v).replace(",", "")
        return float(s) if s else None
    except Exception:
        return None


def to_int(v):
    try:
        s = as_str(v).replace(",", "")
        return int(float(s)) if s else None
    except Exception:
        return None


# -----------------------------------------------------------------------------
# Flask app
# -----------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")

# ✅ Keep users logged in until they explicitly click Logout.
# The session cookie is now persistent, so refreshes, tab changes, and browser restarts
# will not log the user out. Set SESSION_LIFETIME_DAYS if you ever want to change it.
app.config.update(
    SESSION_PERMANENT=True,
    PERMANENT_SESSION_LIFETIME=timedelta(
        days=int(os.getenv("SESSION_LIFETIME_DAYS", "3650"))
    ),
    SESSION_REFRESH_EACH_REQUEST=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE=os.getenv("SESSION_COOKIE_SAMESITE", "Lax"),
)

# ✅ NEW: IIS/ARR SSL termination support
# Enable by setting: TRUST_PROXY_HEADERS=1 (recommended when behind IIS reverse proxy)
_TRUST_PROXY = (os.getenv("TRUST_PROXY_HEADERS") or "").strip().lower() in (
    "1",
    "true",
    "yes",
    "y",
    "on",
)
if _TRUST_PROXY:
    # Trust one proxy hop: IIS/ARR -> Waitress/Flask
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# ✅ NEW: Secure session cookies (recommended for HTTPS)
# If you terminate SSL at IIS, keep these enabled.
_SECURE_COOKIES = (os.getenv("SECURE_COOKIES") or "").strip().lower() in (
    "1",
    "true",
    "yes",
    "y",
    "on",
)
if _SECURE_COOKIES:
    app.config.update(
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE=os.getenv("SESSION_COOKIE_SAMESITE", "Lax"),
    )

# ✅ NEW (optional): Force HTTPS redirect in Flask.
# Usually better to do this in IIS, but if you want Flask-side redirect:
# set FORCE_HTTPS=1
_FORCE_HTTPS = (os.getenv("FORCE_HTTPS") or "").strip().lower() in (
    "1",
    "true",
    "yes",
    "y",
    "on",
)


def _is_request_https() -> bool:
    # If ProxyFix is enabled, request.is_secure will respect X-Forwarded-Proto.
    if request.is_secure:
        return True
    # Fallback check (in case ProxyFix isn't enabled but header exists)
    xfproto = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    return xfproto == "https"


@app.before_request
def _maybe_force_https():
    if not _FORCE_HTTPS:
        return None
    # Do not redirect local health checks if you ever add them later.
    if _is_request_https():
        return None
    # Preserve path + query
    url = request.url.replace("http://", "https://", 1)
    return redirect(url, code=301)


@app.get("/api/routes")
def api_routes():
    rules = []
    for r in app.url_map.iter_rules():
        rules.append(
            {
                "rule": str(r),
                "endpoint": r.endpoint,
                "methods": sorted(list(r.methods or [])),
            }
        )
    rules.sort(key=lambda x: x["rule"])
    return jsonify(rules)


@app.get("/health")
def health():
    return "OK", 200

# -----------------------------------------------------------------------------
# Bays: EF, AC, DE, CD + ✅ CTL (NO mapping / NO aliasing)
# -----------------------------------------------------------------------------
BAY_CODES = ["EF", "AC", "DE", "CD", "CTL", "BWP"]

COIL_ALLOWED_BAYS = {"EF", "AC"}
PLATE_ALLOWED_BAYS = {"EF", "AC", "DE", "CD", "CTL", "BWP"}

# Bin patterns (existing bays)
BIN_OK = re.compile(r"^(?P<bay>EF|AC|DE|CD)(?P<col>\d{2})(?P<row>[A-G])$", re.I)
BIN_WITH_STATUS = re.compile(
    r"^(?P<bay>EF|AC|DE|CD)(?P<col>\d{2})(?P<code>[A-Z]{2})(?P<row>[A-G])$",
    re.I,
)
BIN_COIL_FLAG = re.compile(r"^(?P<bay>EF|AC|DE|CD)(?P<col>\d{2})C(?P<row>[A-G])$", re.I)

# ✅ Beam Welding Plant bins
# Examples preserved exactly:
#   BWPG01FGA -> BWPG01FGA
#   BWPH08FGA -> BWPH08FGA
#   BWPG17FGE -> BWPG17FGE
BWP_BIN_OK = re.compile(r"^(?P<section>BWP[GH])(?P<col>\d{2})(?P<face>F)(?P<row>[A-G])$", re.I)
BWP_BIN_WITH_SUFFIX = re.compile(r"^(?P<section>BWP[GH])(?P<col>\d{2})(?P<face>F)(?P<row>[A-G])(?P<suffix>[A-Z]{0,6})$", re.I)

# ✅ CTL bin patterns
# Examples:
#   CTLDE5B     -> CTLDE5B
#   CTLDE5PB    -> CTLDE5B   (drops status code P)
#   CTLCD12XB   -> CTLCD12B  (drops status code X)
CTL_BIN_OK = re.compile(r"^CTL(?P<sub>DE|CD)(?P<col>\d+)(?P<row>[A-G])$", re.I)
CTL_BIN_WITH_STATUS = re.compile(
    r"^CTL(?P<sub>DE|CD)(?P<col>\d+)(?P<code>[A-Z]{1,4})(?P<row>[A-G])$",
    re.I,
)

ROW_ORDER = list("GFEDCBA")
ROW_SET = set(ROW_ORDER)


def canon_bay(bay: str) -> str:
    return as_str(bay).upper()


def bin_prefix(bin_name: str) -> str:
    s = (bin_name or "").upper()
    m = re.match(r"^(EF|AC|DE|CD)\d{2}[A-G]$", s)
    if m:
        return m.group(1)

    if BWP_BIN_WITH_SUFFIX.match(s) or BWP_BIN_OK.match(s):
        return "BWP"

    # ✅ CTL bins look like CTLDE5B / CTLCD12A
    if re.match(r"^CTL(?:DE|CD)\d+[A-G]$", s):
        return "CTL"

    return ""


def canon_bin(bin_code: str) -> str:
    s = as_str(bin_code).upper().replace(" ", "")

    # ✅ CTL: drop status code between number and row letter
    m = CTL_BIN_WITH_STATUS.match(s)
    if m:
        return f"CTL{m.group('sub').upper()}{m.group('col')}{m.group('row').upper()}"

    m = CTL_BIN_OK.match(s)
    if m:
        return f"CTL{m.group('sub').upper()}{m.group('col')}{m.group('row').upper()}"

    # ✅ Beam Welding Plant: keep exact bin as-is
    m = BWP_BIN_WITH_SUFFIX.match(s)
    if m:
        suffix = m.group('suffix') or ''
        return f"{m.group('section').upper()}{m.group('col')}{m.group('face').upper()}{m.group('row').upper()}{suffix.upper()}"

    m = BWP_BIN_OK.match(s)
    if m:
        return f"{m.group('section').upper()}{m.group('col')}{m.group('face').upper()}{m.group('row').upper()}"

    # Existing bays: with-status -> base bin
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

    # ✅ CTL flexible separators (optional)
    m3 = re.match(r"^CTL[\s-]?(DE|CD)[\s-]?(\d+)[A-Z]{0,4}([A-G])$", s, flags=re.I)
    if m3:
        return f"CTL{m3.group(1).upper()}{m3.group(2)}{m3.group(3).upper()}"

    return s


def normalize_bin(raw_bin: str, product_type: str) -> str | None:
    b = as_str(raw_bin).upper().replace(" ", "")
    t = as_str(product_type).lower()

    # ✅ CTL BIN normalization (plates)
    # Example: CTLDE5PB -> CTLDE5B
    if b.startswith("CTL"):
        m = CTL_BIN_WITH_STATUS.match(b)
        if m:
            return f"CTL{m.group('sub').upper()}{m.group('col')}{m.group('row').upper()}"
        m = CTL_BIN_OK.match(b)
        if m:
            return f"CTL{m.group('sub').upper()}{m.group('col')}{m.group('row').upper()}"
        m3 = re.match(r"^CTL[\s-]?(DE|CD)[\s-]?(\d+)[A-Z]{0,4}([A-G])$", b, flags=re.I)
        if m3:
            return f"CTL{m3.group(1).upper()}{m3.group(2)}{m3.group(3).upper()}"
        return None

    if BWP_BIN_WITH_SUFFIX.match(b) or BWP_BIN_OK.match(b):
        return canon_bin(b)

    if ("coil" in t) and ("ctl" not in t):
        m = BIN_COIL_FLAG.match(b)
        if m:
            return f"{m.group('bay').upper()}{m.group('col')}{m.group('row').upper()}"

    m = BIN_WITH_STATUS.match(b)
    if m:
        return f"{m.group('bay').upper()}{m.group('col')}{m.group('row').upper()}"

    m = BIN_OK.match(b)
    if m:
        return f"{m.group('bay').upper()}{m.group('col')}{m.group('row').upper()}"

    m2 = re.match(r"^(EF|AC|DE|CD)[\s-]?(\d{2})[\s-]?([A-G])$", b, flags=re.I)
    if m2:
        return f"{m2.group(1).upper()}{m2.group(2)}{m2.group(3).upper()}"

    return None


def looks_like_coil_from_id(pid: str) -> bool:
    p = as_str(pid).upper()
    return p.startswith("C") or p.startswith("COIL")


def guess_type_from_pdtype(pdtype, raw_bin=None, plate_id=None):
    s = as_str(pdtype).lower()
    if "coil" in s and "ctl" not in s:
        return "Coil"
    if BIN_COIL_FLAG.match(as_str(raw_bin).upper()):
        return "Coil"
    if looks_like_coil_from_id(plate_id):
        return "Coil"
    return "Plate"


def runtime_fix_type(row: dict) -> dict:
    t_raw = as_str(row.get("type"))
    pid = row.get("plate_id")

    raw = {}
    try:
        raw = (
            json.loads(row.get("raw_json") or "{}")
            if isinstance(row.get("raw_json"), (str, bytes))
            else (row.get("raw_json") or {})
        )
    except Exception:
        raw = {}

    raw_bin_txt = as_str(
        raw.get("Batch Storage Bin") or raw.get("batch storage bin") or raw.get("BinNo")
    )

    says_coil = ("coil" in t_raw.lower() and "ctl" not in t_raw.lower())
    id_is_coil = looks_like_coil_from_id(pid)
    excel_coil = BIN_COIL_FLAG.match(raw_bin_txt.upper()) is not None

    inferred = "Coil" if (says_coil or id_is_coil or excel_coil) else "Plate"

    if row.get("bin"):
        row["bin"] = canon_bin(row["bin"])

    pref = bin_prefix(row.get("bin") or "")
    if inferred == "Coil" and pref in {"DE", "CD"}:
        row["type"] = "Plate"
    else:
        row["type"] = inferred

    return row


# -----------------------------------------------------------------------------
# Layout JSON
# -----------------------------------------------------------------------------
def _resolve_layout_path(raw_path: str) -> str:
    candidates = []
    p = (raw_path or "").strip()
    if p:
        candidates.append(p)

    candidates.append("yard_logic/full_yard_layout.json")
    candidates.append("/mnt/data/full_yard_layout.json")

    try:
        here = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(here, "yard_logic", "full_yard_layout.json"))
        candidates.append(os.path.join(here, "full_yard_layout.json"))
    except Exception:
        pass

    for c in candidates:
        try:
            if c and os.path.exists(c):
                return c
        except Exception:
            pass

    return p or "yard_logic/full_yard_layout.json"


LAYOUT_PATH = _resolve_layout_path(
    os.getenv("YARD_LAYOUT_PATH", "yard_logic/full_yard_layout.json")
)
with open(LAYOUT_PATH, "r", encoding="utf-8") as f:
    FULL_LAYOUT_RAW = json.load(f)


def layout_for_index() -> dict:
    labels = FULL_LAYOUT_RAW.get("labels") or []
    zones = []

    raw_zones = FULL_LAYOUT_RAW.get("zones") or []
    for z in raw_zones:
        left = z.get("left", z.get("x_pos"))
        top = z.get("top", z.get("y_pos"))
        width = z.get("width")
        height = z.get("height")
        if left is None or top is None or width is None or height is None:
            continue

        bin_code = z.get("bin") or z.get("bin_id") or z.get("id") or ""
        bin_code = canon_bin(bin_code)

        classes = z.get("classes") or z.get("classList") or []
        if isinstance(classes, str):
            classes = classes.split()

        cls = [c for c in classes if c]
        if "zone" not in cls:
            cls.insert(0, "zone")

        zones.append(
            {
                "bin": bin_code,
                "text": z.get("zone") or z.get("text") or z.get("name") or "",
                "top": int(top),
                "left": int(left),
                "width": int(width),
                "height": int(height),
                "classes": cls,
            }
        )

    return {
        "canvas": {"width": 3600, "height": 2000},
        "labels": labels,
        "zones": zones,
    }


def layout_for_3d() -> dict:
    out = {"zones": [], "labels": FULL_LAYOUT_RAW.get("labels") or []}

    raw_zones = FULL_LAYOUT_RAW.get("zones") or []
    for z in raw_zones:
        x = z.get("x_pos", z.get("left"))
        y = z.get("y_pos", z.get("top"))
        w = z.get("width")
        h = z.get("height")
        if x is None or y is None or w is None or h is None:
            continue

        out["zones"].append(
            {
                "bin": canon_bin(z.get("bin") or ""),
                "zone": z.get("zone") or z.get("text") or z.get("name") or "",
                "row": z.get("row") or "",
                "bay": canon_bay(z.get("bay") or ""),
                "x_pos": int(x),
                "y_pos": int(y),
                "width": int(w),
                "height": int(h),
            }
        )

    return out


def _enrich_layout_bins_for_tools() -> list[dict]:
    """
    Allocator + Dispatch tools expect cx/cy and a per-bin record.
    """
    out = []
    for z in (layout_for_3d().get("zones") or []):
        b = canon_bin(z.get("bin") or "")

        m = BIN_OK.match(b)
        m_bwp = None if m else (BWP_BIN_WITH_SUFFIX.match(b) or BWP_BIN_OK.match(b))
        m_ctl = None if (m or m_bwp) else CTL_BIN_OK.match(b)

        if not m and not m_bwp and not m_ctl:
            continue

        if m:
            bay = m.group("bay").upper()
            row = m.group("row").upper()
        elif m_bwp:
            bay = "BWP"
            row = m_bwp.group("row").upper()
        else:
            bay = "CTL"
            row = m_ctl.group("row").upper()

        x, y, w, h = int(z["x_pos"]), int(z["y_pos"]), int(z["width"]), int(z["height"])
        cx, cy = x + (w / 2.0), y + (h / 2.0)

        if bay == "AC":
            allows = "Plate/Coil"
        else:
            allows = "Plate"

        out.append(
            {
                "bin": b,
                "zone": z.get("zone") or "",
                "row": row,
                "bay": bay,
                "x_pos": x,
                "y_pos": y,
                "width": w,
                "height": h,
                "excluded": False,
                "type": "FG" if allows else "Other",
                "allows": allows,
                "cx": cx,
                "cy": cy,
            }
        )
    return out


def _anchors_from_zones(zs: list[dict]) -> dict:
    rail, truck = [], []
    for z in zs:
        name = (z.get("zone") or "").lower()
        if "track" in name or "rake" in name:
            rail.append({"cx": z["cx"], "cy": z["cy"]})
        if "truck" in name or "loading" in name:
            truck.append({"cx": z["cx"], "cy": z["cy"]})
    return {"rail": rail, "truck": truck}


# -----------------------------------------------------------------------------
# DB (SQLAlchemy)
# -----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IS_VERCEL = bool(os.environ.get("VERCEL"))

DEFAULT_DB_PATH = (
    os.path.join("/tmp", "yard_data.db")
    if IS_VERCEL
    else os.path.join(BASE_DIR, "yard_logic", "yard_data.db")
)

DB_PATH = os.getenv("YARD_DB_PATH", DEFAULT_DB_PATH)
if not os.path.isabs(DB_PATH):
    DB_PATH = os.path.join(BASE_DIR, DB_PATH)


def normalize_db_url(raw: str) -> str:
    import importlib.util

    u = (raw or "").strip()
    if not u:
        return u

    if "neon.tech" in u and "sslmode=" not in u:
        u += ("&" if "?" in u else "?") + "sslmode=require"

    if u.startswith("postgresql://") and "+psycopg" not in u and "+psycopg2" not in u:
        if importlib.util.find_spec("psycopg"):
            u = u.replace("postgresql://", "postgresql+psycopg://", 1)
        elif importlib.util.find_spec("psycopg2"):
            u = u.replace("postgresql://", "postgresql+psycopg2://", 1)

    return u


DATABASE_URL = normalize_db_url(os.environ.get("DATABASE_URL", f"sqlite:///{DB_PATH}"))
engine = create_engine(DATABASE_URL, poolclass=QueuePool, pool_pre_ping=True, future=True)


def _exec(con, sql, params=None):
    return con.execute(text(sql), params or {})


def _fetchall_dicts(con, sql, params=None):
    return _exec(con, sql, params).mappings().all()


def _fetchone_scalar(con, sql, params=None):
    return _exec(con, sql, params).scalar_one()


def ensure_schema():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)) or ".", exist_ok=True)
    with engine.begin() as con:
        _exec(
            con,
            """
            CREATE TABLE IF NOT EXISTS plates(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plate_id TEXT,
                bin TEXT,
                type TEXT,
                length REAL, width REAL, thickness REAL,
                pieces INTEGER, weight REAL,
                grade TEXT, customer TEXT, status TEXT, urgency TEXT, dispatch_mode TEXT,
                FI_Rel_text TEXT,
                SBU_RelStatus TEXT,
                CustomerCity TEXT,
                Material_Status TEXT,
                added_at TEXT, updated_at TEXT, created_at TEXT,
                raw_json TEXT
            )
            """,
        )
        _exec(con, "CREATE INDEX IF NOT EXISTS idx_plates_bin ON plates(bin)")
        _exec(con, "CREATE INDEX IF NOT EXISTS idx_plates_pid ON plates(plate_id)")
        _exec(
            con,
            "CREATE INDEX IF NOT EXISTS idx_plates_ts ON plates(substr(COALESCE(updated_at,added_at,created_at),1,19))",
        )

        for col in (
            "FI_Rel_text",
            "SBU_RelStatus",
            "CustomerCity",
            "dispatch_mode",
            "Material_Status",
        ):
            try:
                _exec(con, f"ALTER TABLE plates ADD COLUMN {col} TEXT")
            except Exception:
                pass

        _exec(
            con,
            """
            CREATE TABLE IF NOT EXISTS yard_transactions(
                id TEXT PRIMARY KEY,
                item_type TEXT NOT NULL,
                item_id TEXT NOT NULL,
                action TEXT NOT NULL,
                method TEXT NOT NULL,
                source_bin TEXT,
                dest_bin TEXT,
                timestamp TEXT NOT NULL,
                edited INTEGER NOT NULL DEFAULT 0,
                before_snapshot TEXT,
                after_snapshot TEXT,
                user TEXT,
                status TEXT,
                customer TEXT,
                urgency TEXT,
                event_time TEXT
            )
            """,
        )
        _exec(con, "CREATE INDEX IF NOT EXISTS idx_tx_time ON yard_transactions(timestamp)")
        _exec(con, "CREATE INDEX IF NOT EXISTS idx_tx_item ON yard_transactions(item_type,item_id)")
        _exec(con, "CREATE INDEX IF NOT EXISTS idx_tx_event_time ON yard_transactions(event_time)")

        # ---------------------------------------------------------------------
        # ✅ Oracle cache tables (stored in yard_data.db)
        # ---------------------------------------------------------------------
        _exec(
            con,
            """
            CREATE TABLE IF NOT EXISTS oracle_inventory_snapshot (
                item_id TEXT PRIMARY KEY,
                item_type TEXT,
                bin TEXT,
                seq INTEGER DEFAULT 0,

                customer TEXT,
                grade TEXT,
                reservation TEXT,
                qc_status TEXT,
                urgency TEXT,
                dispatch_mode TEXT,

                length REAL,
                width REAL,
                thickness REAL,
                weight REAL,
                pieces INTEGER DEFAULT 1,

                added_at TEXT,
                created_at TEXT,
                updated_at TEXT,

                raw_json TEXT,
                snapshot_at TEXT
            )
            """,
        )
        _exec(con, "CREATE INDEX IF NOT EXISTS idx_oracle_inv_bin ON oracle_inventory_snapshot(bin)")
        _exec(con, "CREATE INDEX IF NOT EXISTS idx_oracle_inv_type ON oracle_inventory_snapshot(item_type)")
        _exec(con, "CREATE INDEX IF NOT EXISTS idx_oracle_inv_snap ON oracle_inventory_snapshot(snapshot_at)")

        _exec(
            con,
            """
            CREATE TABLE IF NOT EXISTS oracle_sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_at TEXT NOT NULL,
                ok INTEGER NOT NULL,
                rows INTEGER DEFAULT 0,
                message TEXT,
                error TEXT
            )
            """,
        )

        _exec(
            con,
            """
            CREATE TABLE IF NOT EXISTS oracle_sync_lock (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                locked INTEGER NOT NULL DEFAULT 0,
                locked_at TEXT
            )
            """,
        )
        _exec(con, "INSERT OR IGNORE INTO oracle_sync_lock(id, locked, locked_at) VALUES(1, 0, NULL)")


# -----------------------------------------------------------------------------
# Users DB (auth)
# -----------------------------------------------------------------------------
DEFAULT_USERS_DB = (
    os.path.join("/tmp", "users.db")
    if IS_VERCEL
    else os.path.join(BASE_DIR, "yard_logic", "users.db")
)
USERS_DB = os.getenv("USERS_DB_PATH", DEFAULT_USERS_DB)
if not os.path.isabs(USERS_DB):
    USERS_DB = os.path.join(BASE_DIR, USERS_DB)


def get_user_db():
    db = getattr(g, "_users_db", None)
    if db is None:
        os.makedirs(os.path.dirname(os.path.abspath(USERS_DB)) or ".", exist_ok=True)
        db = g._users_db = sqlite3.connect(USERS_DB)
        db.row_factory = sqlite3.Row
    return db


@app.before_request
def ensure_user_store():
    db = get_user_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    db.commit()
    init_admin(db) # ✅ Add this line!


@app.teardown_appcontext
def close_user_store(exception=None):
    db = getattr(g, "_users_db", None)
    if db is not None:
        db.close()


@app.get("/login")
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.post("/login")
def login_post():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    db = get_user_db()
    user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if user and check_password_hash(user["password_hash"], password):
        # ✅ Enforce admin approval
        if not user["is_approved"]:
            flash("Your account is pending admin approval.", "error")
            return redirect(url_for("login"))

        # ✅ Always create a fresh persistent login session.
        # The user stays logged in across refreshes, tab changes, and browser restarts
        # until they explicitly click Logout.
        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]
        session["user_name"] = user["name"]
        session["is_admin"] = bool(user["is_admin"]) # ✅ Save admin role to session
        return redirect(url_for("dashboard"))
    flash("Invalid email or password.", "error")
    return redirect(url_for("login"))


@app.get("/register")
def register():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return render_template("register.html")


@app.post("/register")
def register_post():
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    confirm = request.form.get("confirm") or ""
    if not name or not email or not password:
        flash("All fields are required.", "error")
        return redirect(url_for("register"))
    if password != confirm:
        flash("Passwords do not match.", "error")
        return redirect(url_for("register"))

    db = get_user_db()
    try:
        db.execute(
            "INSERT INTO users(name,email,password_hash) VALUES(?,?,?)",
            (name, email, generate_password_hash(password)),
        )
        db.commit()
    except sqlite3.IntegrityError:
        flash("Email already registered.", "error")
        return redirect(url_for("register"))

    flash("Registration successful. Please wait for admin approval before logging in.", "success")
    return redirect(url_for("login"))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# -----------------------------------------------------------------------------
# Inventory snapshot
# -----------------------------------------------------------------------------
def get_active_bin_entries():
    with engine.begin() as con:
        rows = _fetchall_dicts(
            con,
            """
            SELECT * FROM plates
            WHERE COALESCE(status,'') != 'Dispatched'
            ORDER BY bin,
                     substr(COALESCE(updated_at,added_at,created_at),1,19) ASC,
                     id ASC
            """,
        )

    out: dict[str, list[dict]] = {}
    for row in rows:
        r = runtime_fix_type(dict(row))
        b = canon_bin(r.get("bin") or "")
        r["bin"] = b
        lst = out.setdefault(b, [])
        r["seq"] = len(lst)
        lst.append(r)
    return out


# -----------------------------------------------------------------------------
# Transaction logger
# -----------------------------------------------------------------------------
def _snap_to_dict(v):
    if not v:
        return {}
    if isinstance(v, dict):
        return v
    try:
        return json.loads(v)
    except Exception:
        return {}


def log_tx(
    *,
    item_type,
    item_id,
    action,
    source_bin=None,
    dest_bin=None,
    before=None,
    after=None,
    user=None,
    status=None,
    customer=None,
    urgency=None,
    _conn=None,
):
    a = (action or "").lower().strip()
    method = "Bin Allocator" if a == "added" else "Manual"
    edited = 1 if a in ("moved", "edited") else 0

    before_dict = _snap_to_dict(before)
    after_dict = _snap_to_dict(after)

    now_iso = utc_now_iso_z()
    ev_iso = pick_event_time(before_dict, after_dict, now_iso)

    if source_bin:
        source_bin = canon_bin(source_bin)
    if dest_bin:
        dest_bin = canon_bin(dest_bin)

    row = {
        "id": str(uuid.uuid4()),
        "item_type": item_type,
        "item_id": item_id,
        "action": a,
        "method": method,
        "source_bin": source_bin,
        "dest_bin": dest_bin,
        "timestamp": now_iso,
        "edited": edited,
        "before_snapshot": json.dumps(before_dict, ensure_ascii=False) if before_dict else None,
        "after_snapshot": json.dumps(after_dict, ensure_ascii=False) if after_dict else None,
        "user": user,
        "status": status or after_dict.get("status") or before_dict.get("status"),
        "customer": customer or after_dict.get("customer") or before_dict.get("customer"),
        "urgency": urgency or after_dict.get("urgency") or before_dict.get("urgency"),
        "event_time": ev_iso,
    }

    sql = """
        INSERT INTO yard_transactions
          (id,item_type,item_id,action,method,source_bin,dest_bin,timestamp,edited,
           before_snapshot,after_snapshot,user,status,customer,urgency,event_time)
        VALUES
          (:id,:item_type,:item_id,:action,:method,:source_bin,:dest_bin,:timestamp,:edited,
           :before_snapshot,:after_snapshot,:user,:status,:customer,:urgency,:event_time)
    """
    if _conn is not None:
        _conn.execute(text(sql), row)
    else:
        with engine.begin() as con:
            _exec(con, sql, row)


# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------
def validate_bin_for_type(bin_name: str, item_type: str):
    bin_name = canon_bin((bin_name or "").strip().upper())
    item_type = (item_type or "Plate").strip()
    pref = bin_prefix(bin_name)

    if item_type == "Coil" and pref not in COIL_ALLOWED_BAYS:
        abort(400, "Coils are allowed only in EF / AC bays.")
    if item_type == "Plate" and pref not in PLATE_ALLOWED_BAYS:
        abort(400, "Invalid bay for plates.")


# -----------------------------------------------------------------------------
# Pages
# -----------------------------------------------------------------------------
@app.get("/")
def index():
    if not session.get("user_id"):
        return redirect(url_for("login"))
        
    assigned_bins = get_active_bin_entries()
    return render_template(
        "index.html",
        assigned_bins=assigned_bins,
        layout_json=layout_for_index(),
        layout=layout_for_index(),
    )


@app.get("/api/layout")
def api_layout():
    return jsonify(layout_for_index())


@app.get("/3d")
def yard_3d_view():
    assigned_bins = get_active_bin_entries()
    return render_template(
        "yard_3d_view.html",
        assigned_bins=assigned_bins,
        layout=layout_for_3d(),
        layout_json=layout_for_index(),
    )


@app.get("/allocator", endpoint="allocator")
def allocator():
    return render_template("allocator.html")


@app.get("/vehicle_sequencing")
def vehicle_sequencing():
    return render_template("vehicle_sequencing.html")


@app.get("/transactions")
def transactions_page():
    return render_template("transactions.html")


@app.get("/3d-layout")
def three_d_layout_page():
    return redirect(url_for("yard_3d_view"))


# -----------------------------------------------------------------------------
# Assign / Edit / Move / Unassign (restored)
# -----------------------------------------------------------------------------
@app.post("/assign")
def assign():
    f = request.form
    validate_bin_for_type(f["bin"], f.get("type", "Plate"))

    def _g(k, d=""):
        return f[k] if k in f else d

    now = utc_now_iso_z()
    with engine.begin() as con:
        _exec(
            con,
            """INSERT INTO plates
              (bin,plate_id,type,length,width,thickness,pieces,weight,grade,customer,status,urgency,dispatch_mode,
               FI_Rel_text,SBU_RelStatus,CustomerCity,Material_Status,
               added_at,created_at,raw_json)
              VALUES (:bin,:plate_id,:type,:length,:width,:thickness,:pieces,:weight,:grade,:customer,:status,:urgency,:dispatch_mode,
                      :FI_Rel_text,:SBU_RelStatus,:CustomerCity,:Material_Status,
                      :added_at,:created_at,:raw_json)""",
            {
                "bin": canon_bin(f["bin"]),
                "plate_id": _g("plate_id"),
                "type": _g("type", "Plate"),
                "length": _g("length"),
                "width": _g("width"),
                "thickness": _g("thickness"),
                "pieces": _g("pieces"),
                "weight": _g("weight"),
                "grade": _g("grade"),
                "customer": _g("customer"),
                "status": _g("status"),
                "urgency": _g("urgency"),
                "dispatch_mode": _g("dispatch_mode"),
                "FI_Rel_text": normalize_fi_rel_text(_g("FI_Rel_text")),
                "SBU_RelStatus": normalize_space(_g("SBU_RelStatus")),
                "CustomerCity": normalize_space(_g("CustomerCity")),
                "Material_Status": normalize_space(_g("Material_Status")),
                "added_at": now,
                "created_at": now,
                "raw_json": None,
            },
        )

    after = {
        "plate_id": _g("plate_id"),
        "type": _g("type", "Plate"),
        "length": _g("length"),
        "width": _g("width"),
        "thickness": _g("thickness"),
        "pieces": _g("pieces"),
        "weight": _g("weight"),
        "grade": _g("grade"),
        "customer": _g("customer"),
        "status": _g("status"),
        "urgency": _g("urgency"),
        "dispatch_mode": _g("dispatch_mode"),
        "FI_Rel_text": normalize_fi_rel_text(_g("FI_Rel_text")),
        "SBU_RelStatus": normalize_space(_g("SBU_RelStatus")),
        "CustomerCity": normalize_space(_g("CustomerCity")),
        "bin": canon_bin(f["bin"]),
        "added_at": now,
        "created_at": now,
    }

    log_tx(
        item_type=after["type"],
        item_id=after["plate_id"],
        action="added",
        source_bin=None,
        dest_bin=after["bin"],
        before=None,
        after=after,
        status=after.get("status"),
        customer=after.get("customer"),
        urgency=after.get("urgency"),
    )

    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True})
    return redirect("/")


@app.post("/edit")
def edit_item():
    pid = (request.form.get("plate_id") or "").strip()
    if not pid:
        abort(400, "plate_id is required")

    allowed = [
        "type",
        "customer",
        "grade",
        "length",
        "width",
        "thickness",
        "pieces",
        "weight",
        "status",
        "bin",
        "urgency",
        "dispatch_mode",
        "FI_Rel_text",
        "SBU_RelStatus",
        "CustomerCity",
    ]

    with engine.begin() as con:
        before_row = (
            _exec(
                con,
                """
            SELECT id, * FROM plates WHERE plate_id=:pid
            ORDER BY COALESCE(updated_at,added_at,created_at) DESC, id DESC
            LIMIT 1
            """,
                {"pid": pid},
            )
            .mappings()
            .first()
        )
        if not before_row:
            abort(404, "Item not found")

        before = dict(before_row)
        fields = {}

        for k in allowed:
            if k in request.form:
                v = request.form.get(k)
                v = as_str(v) if isinstance(v, str) else v
                fields[k] = (None if (isinstance(v, str) and v == "") else v)

        if not fields:
            abort(400, "No editable fields provided")

        if "FI_Rel_text" in fields:
            fields["FI_Rel_text"] = normalize_fi_rel_text(fields.get("FI_Rel_text"))
        if "SBU_RelStatus" in fields:
            fields["SBU_RelStatus"] = normalize_space(fields.get("SBU_RelStatus"))
        if "CustomerCity" in fields:
            fields["CustomerCity"] = normalize_space(fields.get("CustomerCity"))
        if "customer" in fields:
            fields["customer"] = normalize_space(fields.get("customer"))

        if "bin" in fields and fields["bin"]:
            new_bin = canon_bin(fields["bin"])
            validate_bin_for_type(new_bin, fields.get("type") or before.get("type") or "Plate")
            fields["bin"] = new_bin

        fields["updated_at"] = utc_now_iso_z()

        set_clause = ", ".join([f"{k}=:{k}" for k in fields.keys()])
        params = dict(fields)
        params["rid"] = before_row["id"]

        _exec(con, f"UPDATE plates SET {set_clause} WHERE id=:rid", params)
        after = (
            _exec(con, "SELECT * FROM plates WHERE id=:rid", {"rid": before_row["id"]})
            .mappings()
            .first()
        )

    log_tx(
        item_type=(after["type"] if after else before.get("type")),
        item_id=(after["plate_id"] if after else pid),
        action="edited",
        source_bin=before.get("bin"),
        dest_bin=(after.get("bin") if after else None),
        before=before,
        after=dict(after) if after else None,
        status=(after.get("status") if after else before.get("status")),
        customer=(after.get("customer") if after else before.get("customer")),
        urgency=(after.get("urgency") if after else before.get("urgency")),
    )

    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True})
    return redirect("/")


@app.post("/move")
def move_plate():
    p = request.get_json(silent=True) if request.is_json else request.form
    pid = (p.get("plate_id") or "").strip()
    new_bin = canon_bin((p.get("new_bin") or "").strip().upper())
    if not pid or not new_bin:
        abort(400, "plate_id and new_bin are required")

    with engine.begin() as con:
        row = (
            _exec(
                con,
                """
            SELECT id, * FROM plates WHERE plate_id=:pid
            ORDER BY COALESCE(updated_at,added_at,created_at) DESC, id DESC
            LIMIT 1
            """,
                {"pid": pid},
            )
            .mappings()
            .first()
        )
        if not row:
            abort(404, "Item not found.")

        row = dict(row)
        old_bin = canon_bin(row.get("bin") or "")
        item_type = (row.get("type") or "Plate").strip()

        if new_bin == old_bin:
            if request.headers.get("X-Requested-With") == "fetch" or request.is_json:
                return jsonify({"ok": True, "moved": False, "bin": old_bin})
            return redirect("/")

        validate_bin_for_type(new_bin, item_type)

        _exec(
            con,
            "UPDATE plates SET bin=:b, updated_at=:u WHERE id=:rid",
            {"b": new_bin, "u": utc_now_iso_z(), "rid": row["id"]},
        )

    after = dict(row)
    after["bin"] = new_bin
    after["updated_at"] = utc_now_iso_z()

    log_tx(
        item_type=row.get("type") or "Plate",
        item_id=row.get("plate_id") or pid,
        action="moved",
        source_bin=old_bin,
        dest_bin=new_bin,
        before=row,
        after=after,
        status=after.get("status"),
        customer=after.get("customer"),
        urgency=after.get("urgency"),
    )

    if request.headers.get("X-Requested-With") == "fetch" or request.is_json:
        return jsonify({"ok": True, "moved": True, "bin": new_bin})
    return redirect("/")


@app.post("/unassign")
def unassign():
    pid = (request.form.get("plate_id") or "").strip()
    if not pid:
        abort(400, "plate_id is required")

    with engine.begin() as con:
        before_row = (
            _exec(
                con,
                """
            SELECT id, * FROM plates WHERE plate_id=:pid
            ORDER BY COALESCE(updated_at,added_at,created_at) DESC, id DESC
            LIMIT 1
            """,
                {"pid": pid},
            )
            .mappings()
            .first()
        )

        if before_row:
            _exec(con, "DELETE FROM plates WHERE id=:rid", {"rid": before_row["id"]})
            before = dict(before_row)
            log_tx(
                item_type=before.get("type") or "Plate",
                item_id=before.get("plate_id") or pid,
                action="removed",
                source_bin=before.get("bin"),
                dest_bin=None,
                before=before,
                after=None,
                status=before.get("status"),
                customer=before.get("customer"),
                urgency=before.get("urgency"),
            )

    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True})
    return redirect("/")


# -----------------------------------------------------------------------------
# /api/bins (Allocator) - enriched (cx/cy + anchors)
# -----------------------------------------------------------------------------
@app.get("/api/bins")
def api_bins():
    assigned = get_active_bin_entries()
    zones = _enrich_layout_bins_for_tools()
    anchors = _anchors_from_zones(zones)
    excluded = []
    bin_counts = {b: len(items) for b, items in assigned.items()}

    return jsonify(
        {
            "assigned_bins": assigned,
            "bin_counts": bin_counts,
            "zones": zones,
            "excluded_bins": excluded,
            "anchors": anchors,
        }
    )


# -----------------------------------------------------------------------------
# ✅ Bin Allocator Suggest API wiring + ✅ Oracle endpoints
# -----------------------------------------------------------------------------
from bin_allocator import register_bin_allocator_api
from qr_code_generator import register_qr_code_generator


def config_provider() -> dict:
    oracle_enabled = (os.getenv("ORACLE_ENABLED") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
        "on",
    )

    cfg = {
        "sqlite_path": DB_PATH,
        "layout_json_path": LAYOUT_PATH,
        "google_sheet_csv_url": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv" if SHEET_ID else "",
        "weights": {
            "rehandles": 5.0,
            "travel": 3.0,
            "utilization": 1.5,
            "sla": 2.5,
            "risk": 4.0,
        },
        "hot_list": {"enabled": True, "horizon_hours": 48},
        "oracle": {
            "enabled": bool(oracle_enabled),
            "dsn": (os.getenv("ORACLE_DSN") or os.getenv("MES_ORACLE_DSN") or "").strip(),
            "user": (os.getenv("ORACLE_USER") or os.getenv("MES_ORACLE_DSN") or "").strip(),
            "password": (os.getenv("ORACLE_PASSWORD") or os.getenv("MES_ORACLE_PASSWORD") or "").strip(),
            "mode": (os.getenv("ORACLE_MODE") or "thin").strip(),
            "encoding": (os.getenv("ORACLE_ENCODING") or "UTF-8").strip(),
            "nencoding": (os.getenv("ORACLE_NENCODING") or "UTF-8").strip(),
        },
    }
    return cfg


@app.get("/api/oracle/test")
def api_oracle_test():
    cfg = config_provider().get("oracle") or {}
    if not cfg.get("enabled"):
        return jsonify(
            {
                "ok": False,
                "enabled": False,
                "message": "Oracle is disabled. Set ORACLE_ENABLED=true to enable.",
                "at": utc_now_iso_z(),
            }
        ), 400

    try:
        mes = OracleMESClient(cfg)
        con = mes.connect()
        try:
            cur = con.cursor()
            try:
                cur.execute("SELECT 1 FROM dual")
                one = cur.fetchone()
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
        finally:
            try:
                con.close()
            except Exception:
                pass

        return jsonify(
            {
                "ok": True,
                "enabled": True,
                "message": "Oracle connection OK.",
                "result": (one[0] if one else None),
                "dsn": (cfg.get("dsn") or ""),
                "mode": (cfg.get("mode") or ""),
                "at": utc_now_iso_z(),
            }
        )
    except Exception as e:
        app.logger.exception("Oracle test failed")
        return jsonify(
            {
                "ok": False,
                "enabled": True,
                "message": "Oracle connection FAILED.",
                "error": str(e),
                "at": utc_now_iso_z(),
            }
        ), 500


@app.get("/api/oracle/cache/status")
def api_oracle_cache_status():
    try:
        with engine.begin() as con:
            row = _exec(
                con,
                """
                SELECT
                  (SELECT COUNT(*) FROM oracle_inventory_snapshot) AS rows,
                  (SELECT MAX(snapshot_at) FROM oracle_inventory_snapshot) AS latest_snapshot,
                  (SELECT ok FROM oracle_sync_log ORDER BY id DESC LIMIT 1) AS last_ok,
                  (SELECT snapshot_at FROM oracle_sync_log ORDER BY id DESC LIMIT 1) AS last_log_time
                """
            ).mappings().first()

        return jsonify(
            {
                "ok": True,
                "rows": int(row["rows"] or 0),
                "latest_snapshot": row["latest_snapshot"],
                "last_ok": (int(row["last_ok"] or 0) == 1) if row["last_ok"] is not None else None,
                "last_log_time": row["last_log_time"],
                "at": utc_now_iso_z(),
            }
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "at": utc_now_iso_z()}), 500


register_bin_allocator_api(
    app,
    get_active_bin_entries=get_active_bin_entries,
    zones_provider=lambda: _enrich_layout_bins_for_tools(),
    anchors_provider=lambda zones: _anchors_from_zones(zones),
    config_provider=config_provider,
    engine=engine,
    _exec=_exec,
)

register_qr_code_generator(app)


# -----------------------------------------------------------------------------
# Dashboard helpers/APIs (RESTORED)
# -----------------------------------------------------------------------------
def _parse_blob(val):
    if not val:
        return None
    if isinstance(val, dict):
        return val
    try:
        return json.loads(val)
    except Exception:
        return None


def _get_ci(d, *keys):
    if not d:
        return ""
    lower = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        v = lower.get(str(k).lower())
        if v not in (None, ""):
            return v
    return ""


def _tx_is_dispatch(row: dict) -> bool:
    a = (row.get("action") or "").lower()
    if a == "removed":
        return True
    if a == "edited":
        try:
            if row.get("status") and str(row["status"]).lower() == "dispatched":
                return True
        except Exception:
            pass
        try:
            after = json.loads(row.get("after_snapshot") or "{}")
            return (after.get("status", "").lower() == "dispatched")
        except Exception:
            return False
    return False


def _norm_mode(s: str) -> str:
    m = (s or "").strip().upper()
    if not m:
        return "Unknown"
    if m in ("ZRAL", "RAIL", "BY RAIL"):
        return "Rail"
    if m in ("ZTLR", "ZTRK", "ZTRL", "TRUCK", "BY TRUCK"):
        return "Truck"
    if "rail" in m.lower():
        return "Rail"
    if "truck" in m.lower():
        return "Truck"
    return m.title()


def _extract_mode_from_snapshots(before_blob, after_blob) -> str:
    for blob in (after_blob or {}, before_blob or {}):
        val = _get_ci(blob, "dispatch_mode", "transport", "mode")
        if val:
            return _norm_mode(val)
    return "Unknown"


@app.get("/api/activity/today")
def api_activity_today():
    """
    Aggregate Adds vs Dispatch by hour-of-day across ALL history.
    Also returns:
      - modes: current inventory counts (rail/truck/unknown)
      - dispatch_breakdown: total dispatched by mode across history
    """

    def _p(v):
        if not v:
            return {}
        if isinstance(v, dict):
            return v
        try:
            return json.loads(v)
        except Exception:
            return {}

    try:
        with engine.begin() as con:
            rows = [
                dict(m)
                for m in _exec(
                    con,
                    """
                    SELECT id, item_type, item_id, action, method, source_bin, dest_bin,
                           timestamp, edited, before_snapshot, after_snapshot, status,
                           customer, urgency, event_time
                    FROM yard_transactions
                    ORDER BY substr(COALESCE(event_time,timestamp),1,19) DESC
                    """,
                )
                .mappings()
                .all()
            ]

            pmodes = _fetchall_dicts(
                con,
                "SELECT dispatch_mode, COUNT(*) AS count "
                "FROM plates WHERE COALESCE(status,'') != 'Dispatched' AND dispatch_mode IS NOT NULL "
                "GROUP BY dispatch_mode",
            )

        buckets = {f"{h:02d}": {"added": 0, "removed": 0} for h in range(24)}
        dispatch_breakdown = {}
        details = []

        for r in rows:
            before = _p(r.get("before_snapshot"))
            after = _p(r.get("after_snapshot"))
            blob = after or before or {}

            ts = (r.get("event_time") or r.get("timestamp") or "")
            hh = (ts[11:13] if len(ts) >= 13 else "00")
            hh = hh if hh in buckets else "00"

            act = (r.get("action") or "").lower()
            if act == "added":
                buckets[hh]["added"] += 1
            elif act == "removed":
                buckets[hh]["removed"] += 1
            elif act == "edited":
                try:
                    if (r.get("status") or (after or {}).get("status") or "").strip().lower() == "dispatched":
                        buckets[hh]["removed"] += 1
                except Exception:
                    pass

            if _tx_is_dispatch(r):
                mode = _extract_mode_from_snapshots(before, after)
                dispatch_breakdown[mode] = dispatch_breakdown.get(mode, 0) + 1

            if len(details) < 150:
                details.append(
                    {
                        "timestamp": ts,
                        "action": r.get("action") or "",
                        "item_type": r.get("item_type") or "",
                        "item_id": r.get("item_id") or "",
                        "source_bin": r.get("source_bin") or "",
                        "dest_bin": r.get("dest_bin") or "",
                        "status": r.get("status") or "",
                        "customer": r.get("customer") or "",
                        "urgency": r.get("urgency") or "",
                        "dispatch_mode": _extract_mode_from_snapshots(before, after),
                        "weight": (blob or {}).get("weight") or "",
                    }
                )

        labels = [f"{h:02d}" for h in range(24)]
        adds = [buckets[h]["added"] for h in labels]
        dispatch = [buckets[h]["removed"] for h in labels]

        modes = {"Rail": 0, "Truck": 0, "Unknown": 0}
        for row in pmodes:
            modes[_norm_mode(row["dispatch_mode"])] = modes.get(_norm_mode(row["dispatch_mode"]), 0) + int(
                row["count"]
            )

        modes_lc = {
            "rail": modes.get("Rail", 0),
            "truck": modes.get("Truck", 0),
            "unknown": modes.get("Unknown", 0),
        }

        return jsonify(
            {
                "details": details,
                "series": {"labels": labels, "adds": adds, "dispatch": dispatch},
                "modes": modes_lc,
                "dispatch_breakdown": dispatch_breakdown,
            }
        )

    except Exception as e:
        app.logger.exception("api_activity_today failed")
        labels = [f"{h:02d}" for h in range(24)]
        return jsonify(
            {
                "details": [],
                "series": {"labels": labels, "adds": [0] * 24, "dispatch": [0] * 24},
                "modes": {"rail": 0, "truck": 0, "unknown": 0},
                "dispatch_breakdown": {},
                "error": str(e),
            }
        )


@app.get("/api/inventory")
def api_inventory():
    status = (request.args.get("status") or "").strip()
    item_type = (request.args.get("type") or "").strip()
    since = request.args.get("since")
    until = request.args.get("until")
    limit = int(request.args.get("limit") or 500)

    wh, pa = ["1=1"], {}
    if status:
        wh.append("status=:status")
        pa["status"] = status
    if item_type:
        wh.append("type=:itype")
        pa["itype"] = item_type
    if since:
        wh.append("(substr(COALESCE(added_at,created_at),1,10) >= :since)")
        pa["since"] = since
    if until:
        wh.append("(substr(COALESCE(added_at,created_at),1,10) <= :until)")
        pa["until"] = until

    sql = f"""
        SELECT plate_id,type,bin,status,customer,urgency,grade,weight,length,width,thickness,pieces,dispatch_mode,
               FI_Rel_text,SBU_RelStatus,CustomerCity,
               added_at,created_at,updated_at,raw_json
        FROM plates
        WHERE {' AND '.join(wh)}
        ORDER BY COALESCE(updated_at,added_at,created_at) DESC
        LIMIT :lim
    """
    pa["lim"] = limit

    with engine.begin() as con:
        rows = [runtime_fix_type(dict(r)) for r in _fetchall_dicts(con, sql, pa)]
    return jsonify(rows)


@app.get("/api/planned_deliveries")
def api_planned_deliveries():
    """
    Planned deliveries = items still in yard with a dispatch mode set.
    Returns table rows and chart summaries grouped by mode.
    """
    status = (request.args.get("status") or "").strip()
    item_type = (request.args.get("type") or "").strip()
    limit = int(request.args.get("limit") or 1000)

    wh, pa = ["COALESCE(status,'') != 'Dispatched'", "dispatch_mode IS NOT NULL"], {}
    if status and status.lower() != "all":
        wh.append("status=:status")
        pa["status"] = status
    if item_type and item_type.lower() != "all":
        wh.append("type=:itype")
        pa["itype"] = item_type

    sql = f"""
        SELECT plate_id, type, bin, status, customer, weight, dispatch_mode,
               COALESCE(updated_at, added_at, created_at) AS added_at
        FROM plates
        WHERE {' AND '.join(wh)}
        ORDER BY added_at DESC
        LIMIT :lim
    """
    pa["lim"] = limit

    with engine.begin() as con:
        rows = [dict(r) for r in _fetchall_dicts(con, sql, pa)]

    def _safe_float(x):
        try:
            return float(x or 0)
        except Exception:
            return 0.0

    summary_counts = {"Rail": 0, "Truck": 0}
    summary_weights = {"Rail": 0.0, "Truck": 0.0}
    out_rows = []

    now_dt = datetime.now(timezone.utc).replace(microsecond=0)

    for r in rows:
        mode = _norm_mode(r.get("dispatch_mode"))
        added = r.get("added_at") or ""
        hours = "—"
        try:
            base = added.replace("Z", "+00:00") if added and added.endswith("Z") else added
            base_dt = datetime.fromisoformat(base)
            if base_dt.tzinfo is None:
                base_dt = base_dt.replace(tzinfo=timezone.utc)
            diff = now_dt - base_dt.astimezone(timezone.utc)
            hours = max(int(diff.total_seconds() // 3600), 0)
        except Exception:
            pass

        out_rows.append(
            {
                "plate_id": r["plate_id"],
                "status": r.get("status") or "",
                "bin": canon_bin(r.get("bin") or ""),
                "customer": r.get("customer") or "",
                "weight": r.get("weight"),
                "mode": mode,
                "added_at": added,
                "hours": hours,
            }
        )

        if mode in ("Rail", "Truck"):
            summary_counts[mode] += 1
            summary_weights[mode] += _safe_float(r.get("weight"))

    return jsonify({"rows": out_rows, "summary": {"counts": summary_counts, "weights": summary_weights}})


# -----------------------------------------------------------------------------
# Transactions API (RESTORED full payload)
# -----------------------------------------------------------------------------
@app.get("/api/transactions")
def api_transactions():
    action = request.args.get("action")
    method = request.args.get("method")
    item_type = request.args.get("item_type")
    edited_only = request.args.get("edited")
    start = request.args.get("start")
    end = request.args.get("end")
    status = request.args.get("status")
    customer = request.args.get("customer")
    urgency = request.args.get("urgency")

    wh, pa = ["1=1"], {}

    if action and action != "all":
        wh.append("LOWER(action)=LOWER(:action)")
        pa["action"] = action

    if method and method != "all":
        wh.append("LOWER(method)=:method")
        pa["method"] = method.replace("_", " ").strip().lower()

    if item_type and item_type != "all":
        wh.append("item_type=:itype")
        pa["itype"] = item_type

    if edited_only == "1":
        wh.append("edited=1")

    if start:
        wh.append("substr(COALESCE(event_time,timestamp),1,10) >= :start")
        pa["start"] = start
    if end:
        wh.append("substr(COALESCE(event_time,timestamp),1,10) <= :end")
        pa["end"] = end

    if status and status != "all":
        wh.append("status=:status")
        pa["status"] = status

    if urgency and urgency != "all":
        wh.append("urgency=:urgency")
        pa["urgency"] = urgency

    if customer:
        pa["cust"] = f"%{customer.lower()}%"
        wh.append("LOWER(customer) LIKE :cust")

    sql = f"""
        SELECT id, item_type, item_id, action, method, source_bin, dest_bin,
               timestamp, edited, status, customer, urgency,
               before_snapshot, after_snapshot,
               COALESCE(event_time, timestamp) AS when_ts,
               event_time
        FROM yard_transactions
        WHERE {' AND '.join(wh)}
        ORDER BY substr(COALESCE(event_time, timestamp), 1, 19) DESC
        LIMIT 500
    """

    with engine.begin() as con:
        rows = _exec(con, sql, pa).mappings().all()

        ids = list({str(r["item_id"]) for r in rows})
        plate_map = {}
        if ids:
            q = ",".join(f":p{i}" for i in range(len(ids)))
            params = {f"p{i}": v for i, v in enumerate(ids)}
            for pr in _exec(
                con,
                f"SELECT plate_id, weight, customer FROM plates WHERE plate_id IN ({q})",
                params,
            ).mappings().all():
                plate_map[str(pr["plate_id"])] = {"weight": pr["weight"], "customer": pr["customer"]}

    def _first_nonempty(*vals):
        for v in vals:
            if v is None:
                continue
            s = str(v).strip()
            if s and s.lower() != "null":
                return v
        return None

    def _is_midnight(iso_s: str | None) -> bool:
        return bool(iso_s) and iso_s.endswith("T00:00:00Z")

    now_dt = datetime.now(timezone.utc)
    out = []

    for r in rows:
        before = _parse_blob(r.get("before_snapshot"))
        after = _parse_blob(r.get("after_snapshot"))
        blob = after or before or {}

        weight = _get_ci(blob, "weight")
        cust_b = _get_ci(blob, "customer")
        po = _get_ci(blob, "po", "po_no", "po_number", "bill", "bill_no")
        transport = _get_ci(blob, "transport", "dispatch_mode", "mode")

        pid = str(r["item_id"])
        if (not weight) and pid in plate_map and plate_map[pid].get("weight"):
            weight = plate_map[pid]["weight"]
        if (not cust_b) and pid in plate_map and plate_map[pid].get("customer"):
            cust_b = plate_map[pid]["customer"]

        raw_start = _first_nonempty(
            _get_ci(after, "added_at", "created_at", "Created On", "createdOn"),
            _get_ci(before, "added_at", "created_at", "Created On", "createdOn"),
            r.get("event_time"),
            r.get("when_ts"),
        )
        age_start_iso = to_iso_utc_z(raw_start)

        if _is_midnight(age_start_iso) and r.get("when_ts"):
            age_start_iso = to_iso_utc_z(r["when_ts"])

        age_hours = None
        if age_start_iso:
            try:
                start_dt = datetime.strptime(age_start_iso, _ISO_FMT_Z).replace(tzinfo=timezone.utc)
                diff = now_dt - start_dt
                age_hours = 0 if diff.total_seconds() < 0 else int(diff.total_seconds() // 3600)
            except Exception:
                age_hours = None

        display_ts = age_start_iso or r["when_ts"]

        out.append(
            {
                "id": r["id"],
                "item_type": r["item_type"],
                "item_id": r["item_id"],
                "action": r["action"],
                "method": r["method"],
                "source_bin": canon_bin(r["source_bin"] or "") if r["source_bin"] else None,
                "dest_bin": canon_bin(r["dest_bin"] or "") if r["dest_bin"] else None,
                "timestamp": display_ts,
                "age_start": age_start_iso,
                "age_hours": age_hours,
                "edited": bool(r["edited"]),
                "status": r["status"],
                "customer": cust_b or (r.get("customer") or ""),
                "urgency": r["urgency"],
                "weight": weight or "",
                "po": po or "",
                "transport": transport or "",
                "before_snapshot": before,
                "after_snapshot": after,
            }
        )

    return jsonify(out)


# -----------------------------------------------------------------------------
# Locate & Bin detail (RESTORED)
# -----------------------------------------------------------------------------
def _normalize_pid(pid: str) -> str:
    if not pid:
        return ""
    p = re.sub(r"[\s-]", "", str(pid).upper())
    p = re.sub(r"^(PLATE|PL|COIL)", "", p)
    digits = re.sub(r"\D+", "", p)
    return digits.lstrip("0") or p


def _build_seq_map():
    seq_map = {}
    assigned = get_active_bin_entries()
    for b, items in assigned.items():
        for it in items:
            upid = str(it.get("plate_id", "")).upper()
            seq_map[(upid, b)] = it.get("seq", 0)
    return seq_map


@app.get("/api/locate")
def api_locate():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"query": [], "found": [], "missing": []})

    tokens = [s for s in re.split(r"[,\s]+", q) if s.strip()]
    tokens_up = [t.upper() for t in tokens]
    tokens_norm = [_normalize_pid(t) for t in tokens]

    with engine.begin() as con:
        rows = _fetchall_dicts(
            con,
            """SELECT plate_id, bin, type, status, customer, grade,
                      FI_Rel_text,SBU_RelStatus,CustomerCity,
                      length, width, thickness, pieces,
                      COALESCE(updated_at,added_at,created_at) AS t,
                      added_at, created_at, updated_at, raw_json
               FROM plates"""
        )

    by_exact, by_norm, by_upper = {}, {}, {}

    def _push(dct, key, idx):
        dct.setdefault(key, set()).add(idx)

    for i, r in enumerate(rows):
        pid = str(r.get("plate_id") or "").strip()
        if not pid:
            continue
        up = pid.upper()
        nm = _normalize_pid(pid)
        _push(by_exact, up, i)
        _push(by_norm, nm, i)
        _push(by_upper, up, i)

    seq_map = _build_seq_map()

    def emit(idx):
        r = runtime_fix_type(dict(rows[idx]))
        up = str(r["plate_id"]).upper()
        b = canon_bin(str(r.get("bin") or ""))
        return {
            "item_id": up,
            "bin": b,
            "seq": seq_map.get((up, b), 0),
            "type": r.get("type"),
            "status": r.get("status"),
            "length": r.get("length"),
            "width": r.get("width"),
            "thickness": r.get("thickness"),
            "pieces": r.get("pieces"),
            "grade": r.get("grade"),
            "customer": r.get("customer"),
            "FI_Rel_text": r.get("FI_Rel_text"),
            "SBU_RelStatus": r.get("SBU_RelStatus"),
            "CustomerCity": r.get("CustomerCity"),
            "added_at": r.get("added_at"),
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
        }

    found, missing = [], []
    for raw, up_tok, norm_tok in zip(tokens, tokens_up, tokens_norm):
        emitted = False
        for idx in sorted(by_exact.get(up_tok, [])):
            found.append(emit(idx))
            emitted = True
        if not emitted:
            for idx in sorted(by_norm.get(norm_tok, [])):
                found.append(emit(idx))
                emitted = True
        if not emitted:
            for up_id, idxs in by_upper.items():
                if up_tok in up_id:
                    for idx in sorted(idxs):
                        found.append(emit(idx))
                        emitted = True
        if not emitted:
            missing.append(raw)

    seen, unique = set(), []
    for f in found:
        key = (f["item_id"], f["bin"], f["seq"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)

    unique.sort(key=lambda r: (r["item_id"], r["bin"], r["seq"]))
    return jsonify({"query": tokens, "found": unique, "missing": missing})


@app.get("/api/bin")
def api_bin_detail():
    bin_code = canon_bin((request.args.get("code") or "").strip().upper())
    if not bin_code:
        abort(400, "Missing ?code=EF42D")

    assigned = get_active_bin_entries()
    items = assigned.get(bin_code, [])
    out = []
    for it in items:
        it2 = dict(it)
        try:
            it2["raw_json_expanded"] = json.loads(it2.get("raw_json") or "{}")
        except Exception:
            it2["raw_json_expanded"] = {}
        out.append(it2)

    return jsonify({"bin": bin_code, "count": len(out), "items": out})


# -----------------------------------------------------------------------------
# Dashboard moved to dashboard_routes.py (REGISTERED HERE)
# IMPORTANT: removed the inline /dashboard route to avoid duplicate endpoint.
# -----------------------------------------------------------------------------
from dashboard_routes import register_dashboard_routes

register_dashboard_routes(
    app,
    engine=engine,
    _exec=_exec,
    _fetchall_dicts=_fetchall_dicts,
    _fetchone_scalar=_fetchone_scalar,
    utc_today_str=utc_today_str,
    canon_bin=canon_bin,
    _tx_is_dispatch=_tx_is_dispatch,
    _norm_mode=_norm_mode,
    BAY_CODES=BAY_CODES,
)


# -----------------------------------------------------------------------------
# Dispatch Suggestions (RESTORED registration + fallback API)
# -----------------------------------------------------------------------------
try:
    from dispatch_suggestions_citywise import register_dispatch_suggestions_api
except Exception:
    register_dispatch_suggestions_api = None

if register_dispatch_suggestions_api:
    register_dispatch_suggestions_api(
        app,
        get_active_bin_entries=get_active_bin_entries,
        zones_provider=lambda: _enrich_layout_bins_for_tools(),
    )
else:

    @app.get("/api/dispatch_suggestions")
    def api_dispatch_suggestions_fallback():
        with engine.begin() as con:
            rows = _fetchall_dicts(
                con,
                """
                SELECT plate_id, bin, type, status, customer, weight, dispatch_mode, CustomerCity
                FROM plates
                WHERE COALESCE(status,'') != 'Dispatched'
                """,
            )

        groups = {}
        total_units = 0
        for r in rows:
            city = normalize_space(r.get("CustomerCity")) or "Unknown"
            mode = _norm_mode(r.get("dispatch_mode"))
            key = (city, mode)
            g2 = groups.setdefault(
                key, {"city": city, "mode": mode, "count": 0, "weight": 0.0, "items": []}
            )
            g2["count"] += 1
            total_units += 1
            try:
                g2["weight"] += float(r.get("weight") or 0)
            except Exception:
                pass
            if len(g2["items"]) < 200:
                g2["items"].append(
                    {
                        "plate_id": r.get("plate_id"),
                        "bin": canon_bin(r.get("bin") or ""),
                        "status": r.get("status") or "",
                        "customer": r.get("customer") or "",
                        "weight": r.get("weight") or "",
                    }
                )

        items = list(groups.values())
        items.sort(key=lambda x: (-x["count"], x["city"], x["mode"]))

        return jsonify({"units": total_units, "items": items})


# -----------------------------------------------------------------------------
# Vehicle Sequencing (Plate Mill Yard Management)
# -----------------------------------------------------------------------------
try:
    from vehicle_sequencing import register_vehicle_sequencing_api
    register_vehicle_sequencing_api(
        app,
        get_active_bin_entries_fn=get_active_bin_entries,
        enrich_layout_bins_fn=_enrich_layout_bins_for_tools,
    )
    print("[vehicle_sequencing] API registered")
except Exception as e:
    print(f"[vehicle_sequencing] Could not register vehicle sequencing API: {e}")


# -----------------------------------------------------------------------------
# Google Sheet import (same as your new app.py; unchanged except already bay-safe)
# -----------------------------------------------------------------------------
try:
    import requests  # optional
except Exception:
    requests = None

SHEET_ID = (os.getenv("SHEET_ID") or "").strip()
DEFAULT_SHEET_ID_FALLBACK = "1TPt1wTmOFj4ydC_cGS59DGCf4enFGoe-9J3LW-nbRzE"
if not SHEET_ID:
    SHEET_ID = DEFAULT_SHEET_ID_FALLBACK

SHEET_NAME = os.getenv("SHEET_TAB_NAME")
SHEETS_MODE = (os.getenv("GOOGLE_SHEETS_MODE") or "public").lower().strip()


def _gget(row: dict, key: str):
    if not row:
        return None
    want = key.lower().replace(" ", "").replace("_", "")
    for k, v in row.items():
        kk = str(k).strip().lower().replace(" ", "").replace("_", "")
        if kk == want:
            return v
    return None


def _fetch_rows_public_csv():
    if not SHEET_ID:
        return []

    if SHEET_NAME:
        url = (
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv&sheet={SHEET_NAME}"
        )
    else:
        url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"

    print(f"📄 GoogleSheet URL: {url}")

    if requests is not None:
        resp = requests.get(url, timeout=45)
        resp.raise_for_status()
        resp_text = resp.text
    else:
        with urlopen(url) as resp:
            resp_text = resp.read().decode("utf-8", errors="replace")

    head = resp_text[:200].lower()
    if "<html" in head and "google" in head:
        print("⚠️ GoogleSheet response looks like HTML (sheet may not be public / accessible).")
        return []

    buf = io.StringIO(resp_text)
    rows = list(csv.DictReader(buf))
    print(f"✅ GoogleSheet fetched rows: {len(rows)}")
    return rows


def _rows_from_values(values):
    if not values:
        return []
    header_idx = 0
    while header_idx < len(values) and all(str(c).strip() == "" for c in values[header_idx]):
        header_idx += 1
    if header_idx >= len(values):
        return []
    headers = [str(h or "").strip() for h in values[header_idx]]
    out = []
    for row in values[header_idx + 1 :]:
        if len(row) < len(headers):
            row = list(row) + [""] * (len(headers) - len(row))
        elif len(row) > len(headers):
            row = row[: len(headers)]
        d = {headers[i]: row[i] for i in range(len(headers))}
        if all(str(v).strip() == "" for v in d.values()):
            continue
        out.append(d)
    return out


def _fetch_rows_private_sa():
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except Exception:
        raise RuntimeError("Install gspread & google-auth, or use public CSV mode.")

    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    inline = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if inline and inline.strip().startswith("{"):
        creds = Credentials.from_service_account_info(json.loads(inline), scopes=scopes)
    else:
        key_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if not key_path:
            raise RuntimeError(
                "Set GOOGLE_APPLICATION_CREDENTIALS or GOOGLE_APPLICATION_CREDENTIALS_JSON for private mode"
            )
        creds = Credentials.from_service_account_file(key_path, scopes=scopes)

    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.worksheet(SHEET_NAME) if SHEET_NAME else sh.sheet1
    values = ws.get_all_values()
    rows = _rows_from_values(values)
    print(f"✅ GoogleSheet fetched rows (private): {len(rows)}")
    return rows


def fetch_sheet_rows():
    if not SHEET_ID:
        return []
    if SHEETS_MODE == "private":
        try:
            return _fetch_rows_private_sa()
        except Exception as e:
            print(f"⚠️ Private mode failed ({e}). Falling back to public CSV.")
            return _fetch_rows_public_csv()
    return _fetch_rows_public_csv()

# -----------------------------------------------------------------------------
# Navigate tab routes
# -----------------------------------------------------------------------------
from navigate_routes import register_navigate_routes

register_navigate_routes(
    app,
    fetch_sheet_rows=fetch_sheet_rows,
    get_active_bin_entries=get_active_bin_entries,
    canon_bin=canon_bin,
)


def _status_from_sheet_or_mvt(status_val, mvt_val) -> str:
    status = as_str(status_val)
    if status:
        return status
    mvt = as_str(mvt_val)
    if mvt in ("601", "641"):
        return "Dispatched"
    return "Others"


def _action_from_mvt(mvt: str) -> str:
    m = as_str(mvt)
    if m in ("601", "641"):
        return "removed"
    if m == "101":
        return "added"
    return "edited"


def snapshot_from_row(r: dict):
    batch = _gget(r, "Batch")
    material = _gget(r, "Material")
    plate_id = as_str(batch) or as_str(material)

    obj = _gget(r, "Object")
    raw_bin = as_str(_gget(r, "BinNo"))
    itype = guess_type_from_pdtype(obj, raw_bin=raw_bin, plate_id=plate_id)
    bin_code = normalize_bin(raw_bin, itype) if raw_bin else None

    # ✅ NEW FG / WIP LOGIC USING Material_Status ONLY
    raw_material_status = (
        _gget(r, "Material_Status")
        or _gget(r, "MATERIAL_STATUS")
        or _gget(r, "Material Status")
        or _gget(r, "MATERIAL STATUS")
    )

    material_status_norm = normalize_space(raw_material_status or "")
    material_status_lower = (material_status_norm or "").lower()

    FG_MATERIAL_STATUSES = {
        "finished status",
        "tpi completed",
        "levelling completed",
        "offer to pfp/ssd",
        "quenching done",
    }

    if material_status_lower in FG_MATERIAL_STATUSES:
        status = "FG"
    else:
        status = "WIP"

    # ✅ EVERYTHING BELOW MUST STAY OUTSIDE IF/ELSE
    length = to_float(_gget(r, "V_LENGTH"))
    width = to_float(_gget(r, "V_WIDTH"))
    thick = to_float(_gget(r, "V_THICKNESS"))
    pieces = to_int(_gget(r, "V_PIECES")) or 1
    grade = none_if_blank(_gget(r, "V_INT_GRADE"))
    customer = none_if_blank(_gget(r, "CustomerName")) or none_if_blank(_gget(r, "Customer"))
    disp_mode = none_if_blank(_gget(r, "DispMode"))
    qty_weight = to_float(_gget(r, "Qty"))
    created_iso = to_iso_utc_z(_gget(r, "TimeOfEntry")) or utc_now_iso_z()
    mvt = as_str(_gget(r, "MVT"))

    fi_rel_text = normalize_fi_rel_text(_gget(r, "FI_Rel_text"))
    sbu_relstatus = normalize_space(_gget(r, "SBU_RelStatus"))
    customer_city = normalize_space(_gget(r, "CustomerCity"))

    material_status = normalize_space(raw_material_status)

    raw_payload = {
        "Customer": _gget(r, "Customer"),
        "CustomerName": _gget(r, "CustomerName"),
        "CustomerCity": customer_city,
        "Material_Status": material_status,
        "FI_Rel_text": fi_rel_text,
        "SBU_RelStatus": sbu_relstatus,
        "Object": obj,
        "Batch": batch,
        "Material": material,
        "MVT": mvt,
        "DispMode": disp_mode,
        "BinNo": raw_bin,
        "Qty": _gget(r, "Qty"),
        "TimeOfEntry": _gget(r, "TimeOfEntry"),
        "Status": status,  # ✅ now controlled by Material_Status
    }

    return {
        "plate_id": plate_id,
        "type": itype,
        "bin": canon_bin(bin_code) if bin_code else None,
        "status": status,
        "length": length,
        "width": width,
        "thickness": thick,
        "pieces": pieces,
        "weight": qty_weight,
        "grade": grade,
        "customer": customer,
        "FI_Rel_text": fi_rel_text,
        "SBU_RelStatus": sbu_relstatus,
        "CustomerCity": customer_city,
        "Material_Status": material_status,
        "urgency": None,
        "dispatch_mode": disp_mode,
        "added_at": created_iso,
        "created_at": created_iso,
        "raw_json": json.dumps(raw_payload, ensure_ascii=False),
        "mvt": mvt,
        "time": created_iso,
    }


def import_google_sheet_once() -> dict:
    rows = fetch_sheet_rows()
    fetched = len(rows)

    grouped: dict[str, list[dict]] = {}
    mapped = 0

    for r in rows:
        snap = snapshot_from_row(r)
        pid = snap["plate_id"]
        if not pid:
            continue

        act = _action_from_mvt(snap["mvt"])
        if (act != "removed") and not snap.get("bin"):
            continue

        mapped += 1
        grouped.setdefault(pid, []).append(snap)

    current_inventory = []
    tx_to_log = []

    for pid, events in grouped.items():
        events.sort(key=lambda s: s["time"])
        prev = None
        for e in events:
            act = _action_from_mvt(e["mvt"])
            if act == "removed":
                e = dict(e)
                e["status"] = "Dispatched"

            if act == "added":
                tx_to_log.append(("added", None, e.get("bin"), None, e))
                prev = e
            elif act == "removed":
                src = (prev or {}).get("bin") or e.get("bin")
                tx_to_log.append(("removed", src, None, prev, e))
                prev = None
            else:
                if prev and e.get("bin") and e.get("bin") != prev.get("bin"):
                    tx_to_log.append(("moved", prev.get("bin"), e.get("bin"), prev, e))
                else:
                    tx_to_log.append(("edited", prev.get("bin") if prev else None, e.get("bin"), prev, e))
                prev = e

        if prev and (prev.get("status") or "").lower() != "dispatched":
            current_inventory.append(prev)

    with engine.begin() as con:
        _exec(con, "DELETE FROM plates")
        _exec(con, "DELETE FROM yard_transactions")

        if current_inventory:
            _exec(
                con,
                """
                INSERT INTO plates
                (bin,plate_id,type,length,width,thickness,pieces,weight,grade,customer,status,
                 urgency,dispatch_mode,
                 FI_Rel_text,SBU_RelStatus,CustomerCity,Material_Status,
                 added_at,created_at,raw_json)
                VALUES
                (:bin,:plate_id,:type,:length,:width,:thickness,:pieces,:weight,:grade,:customer,:status,
                 :urgency,:dispatch_mode,
                 :FI_Rel_text,:SBU_RelStatus,:CustomerCity,:Material_Status,
                 :added_at,:created_at,:raw_json)
                """,
                current_inventory,
            )

        def _when(before, after):
            return pick_event_time(before or {}, after or {}, utc_now_iso_z())

        tx_to_log.sort(key=lambda t: _when(t[3], t[4]))
        for (action, src_bin, dst_bin, before, after) in tx_to_log:
            log_tx(
                item_type=(after or before or {}).get("type") or "Plate",
                item_id=(after or before or {}).get("plate_id") or "",
                action=action,
                source_bin=src_bin,
                dest_bin=dst_bin,
                before=before,
                after=after,
                status=(after or before or {}).get("status"),
                customer=(after or before or {}).get("customer"),
                urgency=(after or before or {}).get("urgency"),
                _conn=con,
            )

    return {
        "rows_fetched": fetched,
        "rows_mapped": mapped,
        "inventory": len(current_inventory),
        "at": utc_now_iso_z(),
    }


def seed_demo_data_if_empty():
    return


# -----------------------------------------------------------------------------
# Init + 15-min background refresh
# -----------------------------------------------------------------------------
_init_lock = threading.Lock()
_init_done = False


def _sheet_refresh_loop(interval_seconds: int = 15 * 60):
    while True:
        try:
            stats = import_google_sheet_once()
            print(f"🔁 Refresh import @ {stats.get('at')}")
            print(f"   Rows fetched : {stats.get('rows_fetched', 0)}")
            print(f"   Rows mapped  : {stats.get('rows_mapped', 0)}")
            print(f"   Inventory    : {stats.get('inventory', 0)}")
            print("")
        except Exception as e:
            print(f"⚠️ Sheet refresh failed: {e}")
            print("")
        time.sleep(interval_seconds)


_refresh_thread_started = False


def app_init_once():
    global _init_done, _refresh_thread_started
    if _init_done:
        return
    with _init_lock:
        if _init_done:
            return

        ensure_schema()

        if IS_VERCEL:
            # Vercel serverless functions use a read-only project directory and
            # short-lived execution. Keep startup light and avoid background threads.
            _init_done = True
            return

        try:
            stats = import_google_sheet_once()
            print(f"✅ Startup import @ {stats.get('at')}")
            print(f"   Rows fetched : {stats.get('rows_fetched', 0)}")
            print(f"   Rows mapped  : {stats.get('rows_mapped', 0)}")
            print(f"   Inventory    : {stats.get('inventory', 0)}")
            print("")
        except Exception as e:
            print(f"⚠️ Startup import failed: {e}")
            seed_demo_data_if_empty()

        if not _refresh_thread_started and SHEET_ID:
            t = threading.Thread(target=_sheet_refresh_loop, daemon=True)
            t.start()
            _refresh_thread_started = True

        _init_done = True


with app.app_context():
    app_init_once()


@app.before_request
def _ensure_inited():
    app_init_once()


# -----------------------------------------------------------------------------
# Utility: discover LAN IP
# -----------------------------------------------------------------------------
def get_lan_ip() -> str:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
    finally:
        try:
            s.close()
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Main entrypoint (Waitress)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    app_init_once()

    register_admin_routes(app, get_user_db)

    # ✅ ADDED FOR CHATBOT
    from chatbot import register_chatbot_routes
    register_chatbot_routes(app, engine)

    from waitress import serve
    port = int(os.environ.get("PORT", "8026"))
    threads = int(os.environ.get("WAITRESS_THREADS", "8"))

    # ✅ NEW: When hosting behind IIS (reverse proxy), bind Waitress to localhost for safety.
    # Enable by setting: BIND_LOCALHOST_ONLY=1
    _BIND_LOCALHOST_ONLY = (os.getenv("BIND_LOCALHOST_ONLY") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
        "on",
    )

    if _BIND_LOCALHOST_ONLY:
        host = "127.0.0.1"
    else:
        host = os.environ.get("HOST", "0.0.0.0")

    lan_ip = get_lan_ip()

    scheme = "https" if (_TRUST_PROXY or _FORCE_HTTPS) else "http"

    print("")
    print("✅ Server is starting (Waitress)...")
    print(f"   Listening on: {host}:{port}")
    print("")
    print("🌐 Access URLs:")
    print(f"   Local (this PC):      {scheme}://localhost:{port}")
    if host != "127.0.0.1":
        print(f"   LAN (other PCs):      {scheme}://{lan_ip}:{port}")
        print("   (Make sure firewall allows inbound TCP on this port.)")
    else:
        print("   LAN (other PCs):      (via IIS only — Waitress bound to localhost)")
    print("")
    print("🏷️ Bays enabled: EF, AC, DE, CD, CTL (NO mapping / NO aliasing)")
    print("")
    print(f"🗺️ Layout file: {LAYOUT_PATH}")
    print("")
    if _TRUST_PROXY:
        print("🔐 SSL mode: IIS/ARR reverse proxy enabled (TRUST_PROXY_HEADERS=1)")
    if _SECURE_COOKIES:
        print("🍪 Secure cookies: enabled (SECURE_COOKIES=1)")
    if _FORCE_HTTPS:
        print("➡️  Force HTTPS redirect: enabled (FORCE_HTTPS=1)")
    if _BIND_LOCALHOST_ONLY:
        print("🛡️  Waitress binding: localhost only (BIND_LOCALHOST_ONLY=1)")
    print("")

    serve(app, host=host, port=port, threads=threads)