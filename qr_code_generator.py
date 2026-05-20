from __future__ import annotations

import csv
import io
import os
import socket
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import quote
from urllib.request import Request, urlopen

from flask import jsonify, render_template, request, send_file, Response

try:
    import qrcode
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover
    qrcode = None
    Image = None
    ImageDraw = None
    ImageFont = None


QR_DB_PATH = os.getenv("YARD_DB_PATH", "yard_logic/yard_data.db")
QR_PUBLIC_BASE_URL = (os.getenv("QR_PUBLIC_BASE_URL") or "http://115.243.51.157:8026").rstrip("/")
QR_LOCAL_BASE_URL = (os.getenv("QR_LOCAL_BASE_URL") or "http://172.17.33.125:8026").rstrip("/")
QR_SHEET_CSV_URL = os.environ.get(
    "YARD_SHEET_CSV_URL",
    "https://docs.google.com/spreadsheets/d/1TPt1wTmOFj4ydC_cGS59DGCf4enFGoe-9J3LW-nbRzE/export?format=csv",
)
UPDATE_BINNO_URL = "https://jspls4app2.jspl.com:44301/sap/bc/gui/sap/its/zlm01_plm?sap-client=900"
QR_TABLE = "qr_code_batches"
QR_LOGO_PATH = os.getenv("QR_LOGO_PATH", "static/jindal-logo.png")

# ── PRINTER CONFIGURATION ─────────────────────────────────────────────────────
# Program runs on server, users access from different PCs in local network.
# Printer selection is based on CLIENT PC IP, not server IP.
#
# Current mapping:
# PC 172.17.33.122 prints to printer 172.17.33.128
# PC 172.17.33.190 prints to printer 172.17.33.67
#
# If any PC IP changes, update only this mapping.
CLIENT_PRINTER_MAP = {
    "172.17.33.122": "172.17.33.100",
    "172.17.33.190": "172.17.33.67",
}

QR_DEFAULT_PRINTER_IP = (os.getenv("QR_DEFAULT_PRINTER_IP") or "172.17.33.128").strip()
QR_PRINTER_PORT = int(os.getenv("QR_PRINTER_PORT", "9100"))
QR_PRINTER_TIMEOUT = int(os.getenv("QR_PRINTER_TIMEOUT", "25"))

# Optional server environment mapping format:
# QR_CLIENT_PRINTER_BINDINGS=172.17.33.45=172.17.33.128,172.17.33.82=172.17.33.67
QR_CLIENT_PRINTER_BINDINGS = (os.getenv("QR_CLIENT_PRINTER_BINDINGS") or "").strip()

# Per-printer locks prevent same-printer job mixing.
_PRINTER_LOCKS: Dict[str, threading.Lock] = {}
_PRINTER_LOCKS_GUARD = threading.Lock()

# ── Zebra ZT411 is 600 DPI ──────────────────────────────────────────────────
PRINTER_DPI   = 600
LABEL_W_MM    = 100
LABEL_H_MM    =  75        # each of the two copies
PAGE_H_MM     = 150        # total page = 2 × 75 mm

def _mm_to_px(mm: float) -> int:
    return round(mm / 25.4 * PRINTER_DPI)

LABEL_W  = _mm_to_px(LABEL_W_MM)   # 2362
LABEL_H  = _mm_to_px(LABEL_H_MM)   # 1772
PAGE_H   = _mm_to_px(PAGE_H_MM)    # 3543

DISPLAY_FIELDS = [
    "SO_ITEM", "PK_Mat_batch", "Customer", "Object", "Batch", "MVT",
    "Material", "Qty", "Status", "TimeOfEntry", "SO No", "StorageLocation",
    "DispMode", "FI_Rel_text", "SBU_RelStatus", "Material_Status",
    "SoldToParty", "ShipToParty", "PaymentStatus", "V_EXT_GRADE", "BinNo",
    "V_LENGTH", "V_WIDTH", "V_THICKNESS", "V_PIECES", "V_INT_GRADE",
    "CustomerName", "CustomerCity", "EXT_GRADE", "SLocation",
    "Shiping Destination", "Aging Days", "Unres. Stock", "QUALITYREMARK",
    "Planning Material", "Sold-to Party Code", "Party Trnsp/Co. Trnsp",
    "Sold to party", "Ship to party", "Payment Status", "Bal2Bill",
]

SEED_FIELDS = [
    "Batch", "V_LENGTH", "V_WIDTH", "V_THICKNESS", "V_PIECES", "V_EXT_GRADE",
]


IST = timezone(timedelta(hours=5, minutes=30))

def _utc_now_iso() -> str:
    return datetime.now(IST).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S+05:30")


def _as_str(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _safe_slug(v: str) -> str:
    return quote(_as_str(v), safe="")


def _request_base_url() -> str:
    try:
        return (request.url_root or "").rstrip("/")
    except Exception:
        return ""


def _is_local_request(base_url: str) -> bool:
    b = _as_str(base_url).lower()
    return ("172.17." in b) or ("localhost" in b) or ("127.0.0.1" in b)


def _client_ip() -> str:
    try:
        forwarded = _as_str(request.headers.get("X-Forwarded-For"))
        if forwarded:
            return forwarded.split(",")[0].strip()

        real_ip = _as_str(request.headers.get("X-Real-IP"))
        if real_ip:
            return real_ip

        return _as_str(request.remote_addr)
    except Exception:
        return ""


def _parse_client_printer_bindings() -> Dict[str, str]:
    bindings: Dict[str, str] = {}

    for client_ip, printer_ip in CLIENT_PRINTER_MAP.items():
        client_ip = _as_str(client_ip)
        printer_ip = _as_str(printer_ip)
        if client_ip and printer_ip and not client_ip.startswith("PUT_"):
            bindings[client_ip] = printer_ip

    raw = _as_str(QR_CLIENT_PRINTER_BINDINGS)
    if raw:
        for item in raw.split(","):
            item = item.strip()
            if not item or "=" not in item:
                continue
            left, right = item.split("=", 1)
            client_ip = _as_str(left)
            printer_ip = _as_str(right)
            if client_ip and printer_ip:
                bindings[client_ip] = printer_ip

    return bindings


def _selected_printer_ip() -> str:
    client_ip = _client_ip()
    bindings = _parse_client_printer_bindings()

    if client_ip and client_ip in bindings:
        return bindings[client_ip]

    return QR_DEFAULT_PRINTER_IP


def _printer_lock(printer_ip: str) -> threading.Lock:
    with _PRINTER_LOCKS_GUARD:
        if printer_ip not in _PRINTER_LOCKS:
            _PRINTER_LOCKS[printer_ip] = threading.Lock()
        return _PRINTER_LOCKS[printer_ip]


def _send_zpl_to_printer(zpl: str) -> str:
    client_ip = _client_ip()
    printer_ip = _selected_printer_ip()

    if not printer_ip:
        raise RuntimeError(f"Printer IP is not configured for client {client_ip}.")

    lock = _printer_lock(printer_ip)

    with lock:
        try:
            with socket.create_connection((printer_ip, QR_PRINTER_PORT), timeout=QR_PRINTER_TIMEOUT) as s:
                s.settimeout(QR_PRINTER_TIMEOUT)
                s.sendall(zpl.encode("utf-8"))
        except socket.timeout:
            raise RuntimeError(
                f"Timed out connecting to printer {printer_ip}:{QR_PRINTER_PORT} "
                f"from server for client {client_ip}. Check printer power, LAN cable, IP address, "
                f"and whether RAW port {QR_PRINTER_PORT} is enabled."
            )
        except OSError as e:
            raise RuntimeError(
                f"Could not print to printer {printer_ip}:{QR_PRINTER_PORT} "
                f"for client {client_ip}. Error: {e}"
            )

    return printer_ip


def _gget(row: Dict[str, str], key: str) -> Optional[str]:
    want = key.lower().replace(" ", "").replace("_", "").replace("-", "").replace(".", "")
    for k, v in row.items():
        kk = str(k).strip().lower().replace(" ", "").replace("_", "").replace("-", "").replace(".", "")
        if kk == want:
            return v
    return None


def _pick_any(row: Dict[str, str], keys: List[str]) -> str:
    for key in keys:
        v = _gget(row, key)
        if v is not None and _as_str(v) != "":
            return _as_str(v)
    return ""


def _connect_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(QR_DB_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(QR_DB_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row

    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA foreign_keys=ON")

    return con


def _init_db() -> None:
    con = _connect_db()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {QR_TABLE} (
                batch TEXT PRIMARY KEY,
                v_length TEXT,
                v_width TEXT,
                v_thickness TEXT,
                v_pieces TEXT,
                v_ext_grade TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS qr_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_name TEXT,
                action TEXT,
                batch TEXT,
                timestamp TEXT
            )
            """
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _log_action(user_name: str, action: str, batch: str) -> None:
    con = _connect_db()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "INSERT INTO qr_audit_logs (user_name, action, batch, timestamp) VALUES (?, ?, ?, ?)",
            (user_name, action, batch, _utc_now_iso())
        )
        con.commit()
    except Exception as e:
        con.rollback()
        print(f"Failed to save audit log: {e}")
    finally:
        con.close()


def _upsert_seed(payload: Dict[str, Any]) -> None:
    _init_db()
    batch = _as_str(payload.get("batch"))
    if not batch:
        raise ValueError("Batch is required")
    pieces = _as_str(payload.get("v_pieces"))
    if not pieces:
        raise ValueError("Pieces is required")
    now = _utc_now_iso()
    con = _connect_db()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            f"""
            INSERT INTO {QR_TABLE} (
                batch, v_length, v_width, v_thickness, v_pieces, v_ext_grade, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(batch) DO UPDATE SET
                v_length=excluded.v_length,
                v_width=excluded.v_width,
                v_thickness=excluded.v_thickness,
                v_pieces=excluded.v_pieces,
                v_ext_grade=excluded.v_ext_grade,
                updated_at=excluded.updated_at
            """,
            (
                batch,
                _as_str(payload.get("v_length")),
                _as_str(payload.get("v_width")),
                _as_str(payload.get("v_thickness")),
                pieces,
                _as_str(payload.get("grade")),
                now,
                now,
            ),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _delete_seed(batch: str) -> None:
    _init_db()
    con = _connect_db()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(f"DELETE FROM {QR_TABLE} WHERE batch=?", (_as_str(batch),))
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _get_seed(batch: str) -> Dict[str, str]:
    _init_db()
    con = _connect_db()
    try:
        cur = con.execute(
            f"""
            SELECT batch, v_length, v_width, v_thickness, v_pieces, v_ext_grade, created_at, updated_at
            FROM {QR_TABLE} WHERE batch=?
            """,
            (_as_str(batch),),
        )
        row = cur.fetchone()
        if not row:
            return {}
        return {
            "Batch":      _as_str(row["batch"]),
            "V_LENGTH":   _as_str(row["v_length"]),
            "V_WIDTH":    _as_str(row["v_width"]),
            "V_THICKNESS":_as_str(row["v_thickness"]),
            "V_PIECES":   _as_str(row["v_pieces"]),
            "V_EXT_GRADE":_as_str(row["v_ext_grade"]),
            "created_at": _as_str(row["created_at"]),
            "updated_at": _as_str(row["updated_at"]),
        }
    finally:
        con.close()


def _list_seeds(search: str = "") -> List[Dict[str, str]]:
    _init_db()
    con = _connect_db()
    try:
        s = _as_str(search)
        if s:
            cur = con.execute(
                f"""
                SELECT batch, v_length, v_width, v_thickness, v_pieces, v_ext_grade, created_at, updated_at
                FROM {QR_TABLE}
                WHERE LOWER(batch) LIKE ?
                ORDER BY updated_at DESC, batch ASC
                """,
                (f"%{s.lower()}%",),
            )
        else:
            cur = con.execute(
                f"""
                SELECT batch, v_length, v_width, v_thickness, v_pieces, v_ext_grade, created_at, updated_at
                FROM {QR_TABLE}
                ORDER BY updated_at DESC, batch ASC
                """
            )
        rows = []
        for r in cur.fetchall():
            rows.append({
                "batch":      _as_str(r["batch"]),
                "v_length":   _as_str(r["v_length"]),
                "v_width":    _as_str(r["v_width"]),
                "v_thickness":_as_str(r["v_thickness"]),
                "pieces":     _as_str(r["v_pieces"]),
                "grade":      _as_str(r["v_ext_grade"]),
                "created_at": _as_str(r["created_at"]),
                "updated_at": _as_str(r["updated_at"]),
            })
        return rows
    finally:
        con.close()


def _fetch_sheet_rows() -> List[Dict[str, str]]:
    req = Request(QR_SHEET_CSV_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=45) as resp:
        raw = resp.read()
    txt = raw.decode("utf-8", errors="replace")
    head = txt[:200].lower()
    if "<html" in head and "google" in head:
        return []
    buf = io.StringIO(txt)
    return [{k: (v if v is not None else "") for k, v in row.items()} for row in csv.DictReader(buf)]


def _find_sheet_row(batch: str) -> Dict[str, str]:
    target = _as_str(batch).lower()
    if not target:
        return {}
    for row in _fetch_sheet_rows():
        row_batch = _pick_any(row, ["Batch", "PK_Mat_batch"]).lower()
        if row_batch == target:
            return row
    return {}


def _combine_record(batch: str, current_base_url: str = "") -> Dict[str, Any]:
    current_base_url = (_as_str(current_base_url) or _request_base_url()).rstrip("/")
    if not current_base_url:
        current_base_url = QR_PUBLIC_BASE_URL

    seed      = _get_seed(batch)
    sheet_row = _find_sheet_row(batch)

    merged: Dict[str, Any] = {}
    for field in DISPLAY_FIELDS:
        val = _pick_any(sheet_row, [field]) if sheet_row else ""
        if not val and field in seed:
            val = seed.get(field, "")
        merged[field] = val

    if not merged.get("Batch"):
        merged["Batch"] = seed.get("Batch", _as_str(batch))

    for k in SEED_FIELDS:
        if not merged.get(k):
            merged[k] = seed.get(k, "")

    batch_final = merged.get("Batch") or _as_str(batch)
    slug = _safe_slug(batch_final)

    merged["__meta"] = {
        "batch":                    batch_final,
        "seed_found":               bool(seed),
        "sheet_found":              bool(sheet_row),
        "current_base_url":         current_base_url,
        "public_base_url":          QR_PUBLIC_BASE_URL,
        "local_base_url":           QR_LOCAL_BASE_URL,
        "current_detail_url":       f"{current_base_url}/qr/batch/{slug}",
        "public_detail_url":        f"{QR_PUBLIC_BASE_URL}/qr/batch/{slug}",
        "local_detail_url":         f"{QR_LOCAL_BASE_URL}/qr/batch/{slug}",
        "current_public_image_url": f"{current_base_url}/qr/batch/{slug}/image.png",
        "current_local_image_url":  f"{current_base_url}/qr/batch/{slug}/image.png",
        "public_image_url":         f"{QR_PUBLIC_BASE_URL}/qr/batch/{slug}/image.png",
        "local_image_url":          f"{QR_LOCAL_BASE_URL}/qr/batch/{slug}/image.png",
        "current_print_url":        f"{current_base_url}/qr/batch/{slug}/print",
        "public_print_url":         f"{QR_PUBLIC_BASE_URL}/qr/batch/{slug}/print",
        "local_print_url":          f"{QR_LOCAL_BASE_URL}/qr/batch/{slug}/print",
        "update_bin_url":           UPDATE_BINNO_URL,
        "created_at":               seed.get("created_at", ""),
        "updated_at":               seed.get("updated_at", ""),
        "current_is_local":         _is_local_request(current_base_url),
        "client_ip":                _client_ip(),
        "selected_printer_ip":      _selected_printer_ip(),
    }
    return merged


def _font(size: int, bold: bool = False):
    candidates = []
    if bold:
        candidates.extend([
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/calibrib.ttf",
        ])
    else:
        candidates.extend([
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/calibri.ttf",
        ])
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                pass
    return ImageFont.load_default()


def _load_logo_image() -> Optional["Image.Image"]:
    if Image is None:
        return None
    try:
        if os.path.exists(QR_LOGO_PATH):
            return Image.open(QR_LOGO_PATH).convert("RGBA")
    except Exception:
        pass
    return None


def _draw_wrapped_center(draw, text: str, box, font, fill="#111111", line_gap: int = 4):
    x1, y1, x2, y2 = box
    max_w = x2 - x1
    words = text.split()
    lines: List[str] = []
    current = ""
    for word in words:
        probe = word if not current else current + " " + word
        bbox = draw.textbbox((0, 0), probe, font=font)
        if bbox[2] - bbox[0] <= max_w:
            current = probe
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    if not lines:
        lines = [text]

    line_heights = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_heights.append(bbox[3] - bbox[1])

    total_h = sum(line_heights) + line_gap * max(0, len(lines) - 1)
    y = y1 + max(0, (y2 - y1 - total_h) // 2)

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x = x1 + max(0, (max_w - tw) // 2)
        draw.text((x, y), line, font=font, fill=fill)
        y += th + line_gap


def _build_single_label(record: Dict[str, Any], pub_url: str, loc_url: str) -> "Image.Image":

    W, H = LABEL_W, LABEL_H

    canvas = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(canvas)

    # Border
    draw.rectangle([10, 10, W-10, H-10], outline="black", width=4)

    # QR size (BIG + balanced)
    QR_SIZE = int(H * 0.6)

    # Center positions
    LEFT_X  = int(W * 0.1)
    RIGHT_X = int(W * 0.55)

    QR_Y = int(H * 0.08)

    # Fonts
    font_label = _font(int(H * 0.045), bold=True)
    font_main  = _font(int(H * 0.075), bold=True)
    font_sub   = _font(int(H * 0.05))

    # QR generator
    def make_qr(url):
        qr = qrcode.QRCode(
            version=4,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=1
        )
        qr.add_data(url)
        qr.make(fit=True)
        return qr.make_image(fill_color="black", back_color="white").convert("RGB").resize((QR_SIZE, QR_SIZE))

    img_pub = make_qr(pub_url)
    img_loc = make_qr(loc_url)

    # Titles
    draw.text((LEFT_X, 20), "Public IP", font=font_label, fill="black")
    draw.text((RIGHT_X, 20), "Local IP", font=font_label, fill="black")

    # Paste QR
    canvas.paste(img_pub, (LEFT_X, QR_Y))
    canvas.paste(img_loc, (RIGHT_X, QR_Y))

    # Text
    batch = _as_str(record.get("Batch"))

    dims = "T {} | W {} | L {} | Pcs {}".format(
        _as_str(record.get("V_THICKNESS")) or "-",
        _as_str(record.get("V_WIDTH")) or "-",
        _as_str(record.get("V_LENGTH")) or "-",
        _as_str(record.get("V_PIECES")) or "-"
    )

    # Center text helper
    def center_text(y, text, font):
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        draw.text(((W - tw) // 2, y), text, font=font, fill="black")

    center_text(int(H * 0.68), batch, font_main)
    center_text(int(H * 0.78), dims, font_sub)
    center_text(int(H * 0.88), "DO NOT REMOVE/TAMPER THIS LABEL", font_sub)

    return canvas


def _build_combined_label(batch: str) -> bytes:
    if qrcode is None or Image is None or ImageDraw is None or ImageFont is None:
        raise RuntimeError("Missing required libraries: qrcode and pillow")

    record     = _combine_record(batch, current_base_url=_request_base_url())
    batch_text = _as_str(record.get("Batch")) or _as_str(batch)
    slug       = _safe_slug(batch_text)

    pub_url = f"{QR_PUBLIC_BASE_URL.rstrip('/')}/qr/batch/{slug}"
    loc_url = f"{QR_LOCAL_BASE_URL.rstrip('/')}/qr/batch/{slug}"

    single = _build_single_label(record, pub_url, loc_url)

    # Stack two copies vertically
    combined = Image.new("RGB", (LABEL_W, PAGE_H), "#ffffff")
    combined.paste(single, (0, 0))
    combined.paste(single, (0, LABEL_H))

    out = io.BytesIO()
    combined.save(out, format="PNG", dpi=(PRINTER_DPI, PRINTER_DPI))
    return out.getvalue()


# ── FINAL CLEAN ROUTING ────────────────────────────────────────────────────────
def register_qr_code_generator(app):
    _init_db()

    @app.get("/qr-code-generator")
    def qr_code_generator_page():
        return render_template(
            "qr_code_generator.html",
            page_mode="generator",
            qr_public_base_url=QR_PUBLIC_BASE_URL,
            qr_local_base_url=QR_LOCAL_BASE_URL,
            current_base_url=_request_base_url(),
            update_bin_url=UPDATE_BINNO_URL,
        )

    @app.post("/api/qr-code-generator/generate")
    def api_qr_code_generate():
        payload   = request.get_json(silent=True) or request.form.to_dict() or {}
        batch     = _as_str(payload.get("batch"))
        user_name = _as_str(payload.get("user_name", "Unknown User"))
        action_type = _as_str(payload.get("action_type", "generated"))

        if not batch:
            return jsonify({"ok": False, "error": "Batch is required."}), 400

        if not _as_str(payload.get("v_pieces")):
            sheet_row = _find_sheet_row(batch)
            if sheet_row:
                payload["v_pieces"] = _pick_any(sheet_row, ["V_PIECES"])
            if not _as_str(payload.get("v_pieces")):
                return jsonify({"ok": False, "error": "Pieces is required."}), 400

        try:
            if not _as_str(payload.get("v_length")):
                sheet_row = _find_sheet_row(batch)
                if sheet_row:
                    payload["v_length"]    = _pick_any(sheet_row, ["V_LENGTH"])
                    payload["v_width"]     = _pick_any(sheet_row, ["V_WIDTH"])
                    payload["v_thickness"] = _pick_any(sheet_row, ["V_THICKNESS"])
                    payload["grade"]       = _pick_any(sheet_row, ["V_EXT_GRADE"])
            _upsert_seed(payload)
            _log_action(user_name, action_type, batch)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

        rec = _combine_record(batch, current_base_url=_request_base_url())
        return jsonify({
            "ok":   True,
            "batch": rec["__meta"]["batch"],
            "record": rec,
            "current_detail_url":       rec["__meta"]["current_detail_url"],
            "public_detail_url":        rec["__meta"]["public_detail_url"],
            "local_detail_url":         rec["__meta"]["local_detail_url"],
            "current_public_image_url": rec["__meta"]["current_public_image_url"],
            "current_local_image_url":  rec["__meta"]["current_local_image_url"],
            "current_print_url":        rec["__meta"]["current_print_url"],
            "client_ip":                rec["__meta"]["client_ip"],
            "selected_printer_ip":      rec["__meta"]["selected_printer_ip"],
        })

    @app.get("/api/qr-code-generator/list")
    def api_qr_code_list():
        search = _as_str(request.args.get("search"))
        rows   = _list_seeds(search)
        return jsonify({"ok": True, "rows": rows})

    @app.post("/api/qr-code-generator/delete")
    def api_qr_code_delete():
        payload   = request.get_json(silent=True) or request.form.to_dict() or {}
        batch     = _as_str(payload.get("batch"))
        user_name = _as_str(payload.get("user_name", "Unknown User"))
        if not batch:
            return jsonify({"ok": False, "error": "Batch is required."}), 400
        _delete_seed(batch)
        _log_action(user_name, "deleted", batch)
        return jsonify({"ok": True})

    @app.get("/api/qr-code-generator/history")
    def api_qr_code_history():
        con = _connect_db()
        try:
            cur = con.execute(
                "SELECT user_name, action, batch, timestamp FROM qr_audit_logs ORDER BY id DESC LIMIT 200"
            )
            rows = [dict(r) for r in cur.fetchall()]
            return jsonify({"ok": True, "history": rows})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})
        finally:
            con.close()

    @app.get("/api/qr/batch/<path:batch>")
    def api_qr_batch_details(batch: str):
        rec = _combine_record(batch, current_base_url=_request_base_url())
        if not rec.get("Batch") and not rec["__meta"]["seed_found"] and not rec["__meta"]["sheet_found"]:
            return jsonify({"ok": False, "error": "Batch not found."}), 404
        return jsonify({"ok": True, "record": rec})

    @app.get("/qr/batch/<path:batch>")
    def qr_batch_detail_page(batch: str):
        record = _combine_record(batch, current_base_url=_request_base_url())
        if not record.get("Batch") and not record["__meta"]["seed_found"] and not record["__meta"]["sheet_found"]:
            return render_template(
                "qr_code_generator.html",
                page_mode="detail",
                qr_public_base_url=QR_PUBLIC_BASE_URL,
                qr_local_base_url=QR_LOCAL_BASE_URL,
                current_base_url=_request_base_url(),
                update_bin_url=UPDATE_BINNO_URL,
                record=None,
                requested_batch=batch,
                display_fields=DISPLAY_FIELDS,
            ), 404
        return render_template(
            "qr_code_generator.html",
            page_mode="detail",
            qr_public_base_url=QR_PUBLIC_BASE_URL,
            qr_local_base_url=QR_LOCAL_BASE_URL,
            current_base_url=_request_base_url(),
            update_bin_url=UPDATE_BINNO_URL,
            record=record,
            requested_batch=batch,
            display_fields=DISPLAY_FIELDS,
        )

    @app.get("/qr/batch/<path:batch>/image.png")
    def qr_batch_image(batch: str):
        try:
            png = _build_combined_label(batch)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        return send_file(
            io.BytesIO(png),
            mimetype="image/png",
            download_name=f"{_as_str(batch)}_label.png",
        )

    @app.get("/qr/batch/<path:batch>/print")
    def qr_batch_print_page(batch: str):
        record = _combine_record(batch, current_base_url=_request_base_url())
        if not record.get("Batch"):
            return "Batch not found", 404

        batch_text = _as_str(record.get("Batch"))

        dims = "T {} W {} L {} Pcs {}".format(
            _as_str(record.get("V_THICKNESS")) or "-",
            _as_str(record.get("V_WIDTH")) or "-",
            _as_str(record.get("V_LENGTH")) or "-",
            _as_str(record.get("V_PIECES")) or "-"
        )

        slug = _safe_slug(batch_text)

        public_url = f"{QR_PUBLIC_BASE_URL}/qr/batch/{slug}"
        local_url  = f"{QR_LOCAL_BASE_URL}/qr/batch/{slug}"

        # 🔥 AUTO DPI FIX (ONLY CHANGE)
        printer_ip = _selected_printer_ip()

        if printer_ip == "172.17.33.67":  # NEW PRINTER (203 DPI)
            zpl = f"""
^XA
^PW800
^LL1200

^FX TOP LABEL

^FX BORDER
^FO7,7^GB785,585,2^FS

^FO85,74
^BQN,2,7
^FDLA,{public_url}^FS

^FO85,331^FB270,1,0,C
^A0N,17,17
^FDPublic QR^FS

^FO409,74
^BQN,2,7
^FDLA,{local_url}^FS

^FO409,331^FB270,1,0,C
^A0N,17,17
^FDLocal QR^FS

^FO0,372^FB800,1,0,C
^A0N,51,51
^FD{batch_text}^FS

^FO0,456^FB800,1,0,C
^A0N,30,30
^FD{dims}^FS

^FO0,507^FB800,1,0,C
^A0N,24,24
^FDDO NOT REMOVE/TAMPER THIS LABEL^FS

^FO0,541^FB800,1,0,C
^A0N,17,17
^FDGenerated from Yard Management Software^FS

^FO0,599^GB800,2,2^FS

^FO7,606^GB785,585,2^FS

^FO85,676
^BQN,2,7
^FDLA,{public_url}^FS

^FO85,933^FB270,1,0,C
^A0N,17,17
^FDPublic QR^FS

^FO409,676
^BQN,2,7
^FDLA,{local_url}^FS

^FO409,933^FB270,1,0,C
^A0N,17,17
^FDLocal QR^FS

^FO0,963^FB800,1,0,C
^A0N,51,51
^FD{batch_text}^FS

^FO0,1048^FB800,1,0,C
^A0N,30,30
^FD{dims}^FS

^FO0,1098^FB800,1,0,C
^A0N,24,24
^FDDO NOT REMOVE/TAMPER THIS LABEL^FS

^FO0,1132^FB800,1,0,C
^A0N,17,17
^FDGenerated from Yard Management Software^FS

^XZ
"""
        else:
            # OLD PRINTER (600 DPI) — EXACT SAME AS BEFORE
            zpl = f"""
^XA
^PW2362
^LL3543

^FX TOP LABEL

^FX BORDER
^FO20,20^GB2320,1730,6^FS

^FO250,220
^BQN,2,22
^FDLA,{public_url}^FS

^FO250,980^FB800,1,0,C
^A0N,50,50
^FDPublic QR^FS

^FO1210,220
^BQN,2,22
^FDLA,{local_url}^FS

^FO1210,980^FB800,1,0,C
^A0N,50,50
^FDLocal QR^FS

^FO0,1100^FB2362,1,0,C
^A0N,150,150
^FD{batch_text}^FS

^FO0,1350^FB2362,1,0,C
^A0N,90,90
^FD{dims}^FS

^FO0,1500^FB2362,1,0,C
^A0N,70,70
^FDDO NOT REMOVE/TAMPER THIS LABEL^FS

^FO0,1600^FB2362,1,0,C
^A0N,50,50
^FDGenerated from Yard Management Software^FS

^FO0,1772^GB2362,6,6^FS

^FO20,1792^GB2320,1730,6^FS

^FO250,2000
^BQN,2,22
^FDLA,{public_url}^FS

^FO250,2760^FB800,1,0,C
^A0N,50,50
^FDPublic QR^FS

^FO1210,2000
^BQN,2,22
^FDLA,{local_url}^FS

^FO1210,2760^FB800,1,0,C
^A0N,50,50
^FDLocal QR^FS

^FO0,2850^FB2362,1,0,C
^A0N,150,150
^FD{batch_text}^FS

^FO0,3100^FB2362,1,0,C
^A0N,90,90
^FD{dims}^FS

^FO0,3250^FB2362,1,0,C
^A0N,70,70
^FDDO NOT REMOVE/TAMPER THIS LABEL^FS

^FO0,3350^FB2362,1,0,C
^A0N,50,50
^FDGenerated from Yard Management Software^FS

^XZ
"""

        try:
            client_ip = _client_ip()
            printer_ip = _send_zpl_to_printer(zpl)
            return f"✅ Printed successfully from client {client_ip} on printer {printer_ip}"
        except Exception as e:
            client_ip = _client_ip()
            printer_ip = _selected_printer_ip()
            return (
                f"❌ Print failed for client {client_ip} on printer {printer_ip}:{QR_PRINTER_PORT}. "
                f"{str(e)}"
            )