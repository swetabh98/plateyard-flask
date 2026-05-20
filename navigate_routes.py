# navigate_routes.py
from __future__ import annotations

import json
import math
from typing import Any

from flask import jsonify, make_response, redirect, render_template, request, session, url_for


NAVIGATE_BAYS = [
    {"key": "BWP-G", "label": "BWP-G"},
    {"key": "BWP-H", "label": "BWP-H"},
    {"key": "EF", "label": "EF"},
    {"key": "DE", "label": "DE"},
    {"key": "CD", "label": "CD"},
    {"key": "AC", "label": "AC"},
    {"key": "CTL-DE", "label": "CTL DE"},
    {"key": "CTL-CD", "label": "CTL CD"},
]

_BAY_LABELS = {b["key"]: b["label"] for b in NAVIGATE_BAYS}


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    try:
        if isinstance(value, float) and math.isnan(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def _norm_simple(value: Any) -> str:
    return _as_str(value).upper().replace(" ", "").replace("-", "").replace("_", "")


def _ci_get(row: dict, key: str):
    if not isinstance(row, dict):
        return None

    wanted = _norm_simple(key)
    for k, v in row.items():
        if _norm_simple(k) == wanted:
            return v
    return None


def _same_text(a: Any, b: Any) -> bool:
    return _norm_simple(a) == _norm_simple(b)


def _canonical_bay(value: Any) -> str:
    v = _norm_simple(value)

    if v in {"BWPG", "BWPGBAY"}:
        return "BWP-G"
    if v in {"BWPH", "BWPHBAY"}:
        return "BWP-H"
    if v == "EF":
        return "EF"
    if v == "DE":
        return "DE"
    if v == "CD":
        return "CD"
    if v == "AC":
        return "AC"
    if v == "CTLDE":
        return "CTL-DE"
    if v == "CTLCD":
        return "CTL-CD"

    return ""


def _bay_key_from_bin(bin_code: Any) -> str:
    b = _norm_simple(bin_code)

    if b.startswith("BWPG"):
        return "BWP-G"
    if b.startswith("BWPH"):
        return "BWP-H"
    if b.startswith("CTLDE"):
        return "CTL-DE"
    if b.startswith("CTLCD"):
        return "CTL-CD"
    if b.startswith("EF"):
        return "EF"
    if b.startswith("DE"):
        return "DE"
    if b.startswith("CD"):
        return "CD"
    if b.startswith("AC"):
        return "AC"

    return ""


def _bay_display_name(bay_key: str) -> str:
    return _BAY_LABELS.get(bay_key, bay_key)


def _safe_float(value: Any) -> float:
    try:
        s = _as_str(value).replace(",", "")
        return float(s) if s else 0.0
    except Exception:
        return 0.0


def _normalize_customer(value: Any) -> str:
    return " ".join(_as_str(value).split()).upper()


def _item_payload(item: dict) -> dict:
    raw_json_expanded = {}
    try:
        raw_json = item.get("raw_json")
        if isinstance(raw_json, str) and raw_json.strip():
            raw_json_expanded = json.loads(raw_json)
        elif isinstance(raw_json, dict):
            raw_json_expanded = raw_json
    except Exception:
        raw_json_expanded = {}

    return {
        "plate_id": _as_str(item.get("plate_id")),
        "type": _as_str(item.get("type")),
        "bin": _as_str(item.get("bin")),
        "seq": item.get("seq", ""),
        "status": _as_str(item.get("status")),
        "customer": _as_str(item.get("customer")),
        "grade": _as_str(item.get("grade")),
        "length": item.get("length"),
        "width": item.get("width"),
        "thickness": item.get("thickness"),
        "pieces": item.get("pieces"),
        "weight": item.get("weight"),
        "dispatch_mode": _as_str(item.get("dispatch_mode")),
        "FI_Rel_text": _as_str(item.get("FI_Rel_text")),
        "SBU_RelStatus": _as_str(item.get("SBU_RelStatus")),
        "CustomerCity": _as_str(item.get("CustomerCity")),
        "Material_Status": _as_str(item.get("Material_Status")),
        "added_at": _as_str(item.get("added_at")),
        "created_at": _as_str(item.get("created_at")),
        "updated_at": _as_str(item.get("updated_at")),
        "raw_json_expanded": raw_json_expanded,
    }


def _no_store_response(payload):
    response = make_response(payload)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def _sort_bin_key(bin_code: str):
    b = _as_str(bin_code).upper()
    prefix = ""
    number = 999999
    suffix = ""

    for i, ch in enumerate(b):
        if ch.isdigit():
            prefix = b[:i]
            rest = b[i:]
            digits = ""
            tail = ""
            for c in rest:
                if c.isdigit() and not tail:
                    digits += c
                else:
                    tail += c
            try:
                number = int(digits)
            except Exception:
                number = 999999
            suffix = tail
            break

    if not prefix:
        prefix = b

    return (prefix, number, suffix, b)


def _sort_item_key(item: dict):
    seq = item.get("seq", "")
    try:
        seq_n = int(seq)
    except Exception:
        seq_n = 999999
    return (seq_n, _as_str(item.get("plate_id")).upper())


def register_navigate_routes(
    app,
    *,
    fetch_sheet_rows,
    get_active_bin_entries,
    canon_bin,
):
    @app.get("/navigate", endpoint="navigate_page")
    def navigate_page():
        if not session.get("user_id"):
            return redirect(url_for("login"))

        html = render_template(
            "navigate.html",
            bay_options=NAVIGATE_BAYS,
        )
        return _no_store_response(html)

    @app.post("/api/navigate/search")
    def api_navigate_search():
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "Login required."}), 401

        payload = request.get_json(silent=True) or {}
        batch_id = _as_str(payload.get("batch_id"))
        selected_bay = _canonical_bay(payload.get("bay"))

        if not batch_id:
            return jsonify({"ok": False, "error": "Batch ID is required."}), 400

        if selected_bay not in _BAY_LABELS:
            return jsonify({"ok": False, "error": "Please select a valid bay."}), 400

        try:
            sheet_rows = fetch_sheet_rows()
        except Exception as exc:
            app.logger.exception("Navigate: failed to fetch Google Sheet rows")
            return jsonify(
                {
                    "ok": False,
                    "error": "Could not fetch Google Sheet data.",
                    "details": str(exc),
                }
            ), 500

        matched_sheet_row = None
        for row in sheet_rows:
            if _same_text(_ci_get(row, "Batch"), batch_id):
                matched_sheet_row = row
                break

        if not matched_sheet_row:
            return jsonify(
                {
                    "ok": False,
                    "error": f"Batch ID '{batch_id}' was not found in the Google Sheet Batch column.",
                    "batch_id": batch_id,
                }
            ), 404

        customer_name = (
            _as_str(_ci_get(matched_sheet_row, "CustomerName"))
            or _as_str(_ci_get(matched_sheet_row, "Customer"))
        )

        if not customer_name:
            return jsonify(
                {
                    "ok": False,
                    "error": f"Batch ID '{batch_id}' was found, but CustomerName is blank.",
                    "batch_id": batch_id,
                }
            ), 404

        try:
            active_bins = get_active_bin_entries()
        except Exception as exc:
            app.logger.exception("Navigate: failed to read active bin entries")
            return jsonify(
                {
                    "ok": False,
                    "error": "Could not read active yard inventory.",
                    "details": str(exc),
                }
            ), 500

        target_customer_norm = _normalize_customer(customer_name)

        grouped_bins = {}
        total_weight = 0.0
        total_items = 0

        for raw_bin, items in (active_bins or {}).items():
            bin_code = canon_bin(raw_bin)
            if _bay_key_from_bin(bin_code) != selected_bay:
                continue

            for item in items or []:
                item_customer_norm = _normalize_customer(item.get("customer"))
                if item_customer_norm != target_customer_norm:
                    continue

                item_out = _item_payload({**item, "bin": bin_code})
                grouped_bins.setdefault(
                    bin_code,
                    {
                        "bin": bin_code,
                        "bay": _bay_display_name(selected_bay),
                        "count": 0,
                        "total_weight": 0.0,
                        "items": [],
                    },
                )

                grouped_bins[bin_code]["items"].append(item_out)
                grouped_bins[bin_code]["count"] += 1

                wt = _safe_float(item.get("weight"))
                grouped_bins[bin_code]["total_weight"] += wt
                total_weight += wt
                total_items += 1

        bins = list(grouped_bins.values())
        bins.sort(key=lambda x: (-int(x.get("count") or 0), _sort_bin_key(x["bin"])))

        for b in bins:
            b["items"].sort(key=_sort_item_key)
            b["total_weight"] = round(b["total_weight"], 3)

        return jsonify(
            {
                "ok": True,
                "batch_id": batch_id,
                "customer": customer_name,
                "bay": selected_bay,
                "bay_label": _bay_display_name(selected_bay),
                "total_bins": len(bins),
                "total_items": total_items,
                "total_weight": round(total_weight, 3),
                "bins": bins,
            }
        )