# dispatch_suggestions_citywise.py
from __future__ import annotations

import os
import time
import hashlib
import requests
from collections import defaultdict
from typing import Callable, Dict, List, Tuple, Any, Optional

from flask import jsonify, request

# --- Truck capacities (tons) ---
CAPS = (40.0, 36.0, 32.0)  # prefer 40, then 36, then 32
MIN_CAP = 32.0

# --- AI Cache to prevent 429 Quota Exhaustion ---
_AI_CACHE = {
    "data_hash": "",
    "summary": "",
    "timestamp": 0
}

# --- FI allowed (normalize variations like "FI Released(2)" vs "FI Released (2)") ---
def _norm_fi(v: Any) -> str:
    s = _as_str(v).lower()
    s = " ".join(s.split())
    # normalize parentheses spacing: "released (2)" -> "released(2)"
    s = s.replace("released (2)", "released(2)")
    s = s.replace("fi released (2)", "fi released(2)")
    return s

FI_ALLOWED_NORM = {"fi released", "fi released(2)"}


def _as_str(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _as_float(v) -> float:
    try:
        s = _as_str(v).replace(",", "")
        return float(s) if s else 0.0
    except Exception:
        return 0.0


def _norm_city(city: Any) -> str:
    s = _as_str(city).lower()
    s = " ".join(s.split())
    return s


def _norm_customer(cust: Any) -> str:
    s = _as_str(cust).lower()
    s = " ".join(s.split())
    return s


def _norm_bin(b: Any) -> str:
    return _as_str(b).upper().replace(" ", "")


def _bay_of_bin(bin_code: str) -> str:
    """
    Returns bay identifier derived from bin prefix.
    Supports: EF, DE, CD, AC, CTLDE, CTLCD.
    """
    b = _norm_bin(bin_code)

    # CTL first (more specific prefixes)
    if b.startswith("CTLDE"):
        return "CTLDE"
    if b.startswith("CTLCD"):
        return "CTLCD"

    # Main bays
    if b.startswith("EF"):
        return "EF"
    if b.startswith("DE"):
        return "DE"
    if b.startswith("CD"):
        return "CD"
    if b.startswith("AC"):
        return "AC"

    return ""


def _is_coil(item: dict) -> bool:
    return "coil" in _as_str(item.get("type")).lower()


def _type_group(item: dict) -> str:
    # keep it simple: either COIL or PLATE (everything else treated as plate)
    return "COIL" if _is_coil(item) else "PLATE"


def _bin_meta_map(zones: List[dict]) -> Dict[str, dict]:
    m = {}
    for z in zones or []:
        b = _norm_bin(z.get("bin"))
        if b:
            m[b] = dict(z)
    return m


def _stack_order_key(item: dict) -> Tuple[int, str, str]:
    """
    We rely on seq created by get_active_bin_entries():
      - seq 0 = bottom (oldest in that bin)
      - seq last = top
    For top-first: sort by seq descending.
    """
    seq = item.get("seq", 0)
    try:
        seq = int(seq)
    except Exception:
        seq = 0
    pid = _as_str(item.get("plate_id") or item.get("item_id"))
    b = _norm_bin(item.get("bin"))
    return (-seq, b, pid)


def _flatten_items(assigned_bins: Dict[str, List[dict]]) -> List[dict]:
    out: List[dict] = []
    for b, items in (assigned_bins or {}).items():
        if not items:
            continue
        for it in items:
            d = dict(it or {})
            d["bin"] = _norm_bin(d.get("bin") or b)
            out.append(d)
    # top preference across whole yard (still ok because we later re-group by bay/type)
    out.sort(key=_stack_order_key)
    return out


def _is_fg_status(item: dict) -> bool:
    s = _as_str(item.get("Material_Status")).lower()
    if "finished status" in s or "tpi completed" in s:
        return True
    return False


def _eligible(item: dict) -> bool:
    # FI Released / FI Released(2) only (robust matching)
    fi_norm = _norm_fi(item.get("FI_Rel_text"))
    if fi_norm not in FI_ALLOWED_NORM:
        return False

    # Must be FG (now using Material_Status logic)
    if not _is_fg_status(item):
        return False

    # Must have customer & city
    cust = _as_str(item.get("customer"))
    city = _as_str(item.get("CustomerCity"))
    if not cust or not city:
        return False

    # Not dispatched
    st = _as_str(item.get("status")).lower()
    if st == "dispatched":
        return False

    # Must be in one of the supported bays (if unknown bay, skip from dispatch suggestions)
    b = _norm_bin(item.get("bin"))
    if _bay_of_bin(b) not in ("EF", "DE", "CD", "AC", "CTLCD", "CTLDE"):
        return False

    return True


def _group_candidates(all_items: List[dict]) -> Dict[Tuple[str, str, str, str], List[dict]]:
    """
    Groups by (customer_norm, city_norm, bay, type_group).
    This guarantees:
      - same customer
      - same city
      - same bay
      - no mixing plates & coils
    """
    g: Dict[Tuple[str, str, str, str], List[dict]] = defaultdict(list)
    for it in all_items:
        if not _eligible(it):
            continue
        cust_n = _norm_customer(it.get("customer"))
        city_n = _norm_city(it.get("CustomerCity"))
        bay = _bay_of_bin(it.get("bin"))
        tg = _type_group(it)
        g[(cust_n, city_n, bay, tg)].append(it)

    # Within each group, keep top-first priority
    for k in list(g.keys()):
        g[k].sort(key=_stack_order_key)
    return g


def _display_customer_city(items: List[dict]) -> Tuple[str, str]:
    if not items:
        return "", ""
    return _as_str(items[0].get("customer")), _as_str(items[0].get("CustomerCity"))


def _all_customer_names_label(items: List[dict]) -> str:
    """
    Build a display label containing ALL unique customer names present in `items`.
    Keeps first-seen order (top-first order already).
    """
    seen = set()
    names: List[str] = []
    for it in items or []:
        c = _as_str(it.get("customer"))
        if not c:
            continue
        key = _norm_customer(c)
        if key in seen:
            continue
        seen.add(key)
        names.append(c)
    # If somehow empty, keep a sane fallback
    return " + ".join(names) if names else "MIXED"


def _choose_target_bin_for_lot(lot_items: List[dict]) -> str:
    """
    Choose consolidation target bin as:
      - among bins participating in this lot, pick the bin with the highest count of lot items
      - tie-breaker: bin that has the highest 'seq' item (topmost) in that lot
    This aligns with: "move into bins where already more plates are there".
    """
    if not lot_items:
        return ""
    by_bin: Dict[str, List[dict]] = defaultdict(list)
    for it in lot_items:
        b = _norm_bin(it.get("bin"))
        if b:
            by_bin[b].append(it)

    # sort bins by (count desc, best-top seq desc, bin name)
    def top_seq(bin_items: List[dict]) -> int:
        best = -10**9
        for x in bin_items:
            try:
                best = max(best, int(x.get("seq", 0)))
            except Exception:
                pass
        return best

    best_bin = sorted(
        by_bin.items(),
        key=lambda kv: (-len(kv[1]), -top_seq(kv[1]), kv[0])
    )[0][0]
    return best_bin


def _suggest_consolidation_moves(lot_items: List[dict]) -> List[dict]:
    """
    Suggest moving items to a single target bin.
    Since lots are already bay-safe (same bay), moves are never cross-bay.
    """
    if not lot_items:
        return []

    target_bin = _choose_target_bin_for_lot(lot_items)
    if not target_bin:
        return []

    out: List[dict] = []
    for it in lot_items:
        b = _norm_bin(it.get("bin"))
        if not b or b == target_bin:
            continue
        out.append({
            "plate_id": _as_str(it.get("plate_id") or it.get("item_id")),
            "from_bin": b,
            "to_bin": target_bin,
            "reason": "Consolidate within same bay into the bin that already has the most eligible items",
            "customer": _as_str(it.get("customer")),
            "city": _as_str(it.get("CustomerCity")),
            "bay": _bay_of_bin(b),
            "type_group": _type_group(it),
        })
    return out


def _pack_lot_no_exceed(items: List[dict], target: float) -> Tuple[List[dict], float, List[dict]]:
    """
    Build a lot without exceeding 'target' (truck capacity).
    Top-of-stack preference: iterate in current order (already sorted by seq desc).
    We add an item only if it does not exceed the target.
    Items with zero/unknown weight are skipped for capacity building, but kept for later remainder.
    Returns (chosen, total_weight, remaining_items)
    """
    chosen: List[dict] = []
    total = 0.0
    remaining: List[dict] = []

    for it in items:
        w = _as_float(it.get("weight"))
        if w <= 0:
            remaining.append(it)
            continue
        if total + w <= target + 1e-9:
            chosen.append(it)
            total += w
        else:
            remaining.append(it)

    return chosen, total, remaining


def _make_capacity_plan(total: float) -> List[float]:
    """
    Create a list of target truck sizes using 40,36,32 preference.
    Must not exceed total (remaining).
    Example:
      112 -> 40,40,32
      116 -> 40,40,36
      96  -> 32,32,32
    """
    plan: List[float] = []
    rem = total

    # We only "plan" full trucks >=32.
    while rem >= MIN_CAP - 1e-9:
        # choose the largest cap that fits in remaining
        chosen = None
        for cap in CAPS:
            if rem >= cap - 1e-9:
                chosen = cap
                break
        if chosen is None:
            # rem is between 32 and 36? then 32
            chosen = 32.0
        plan.append(float(chosen))
        rem -= chosen

    return plan


def _sum_weights(items: List[dict]) -> float:
    return sum(_as_float(it.get("weight")) for it in items if _as_float(it.get("weight")) > 0)


def _items_payload(items: List[dict]) -> List[dict]:
    out = []
    for it in items:
        out.append({
            "plate_id": _as_str(it.get("plate_id") or it.get("item_id")),
            "type": _as_str(it.get("type")),
            "bin": _norm_bin(it.get("bin")),
            "seq": it.get("seq", 0),
            "weight": it.get("weight"),
            "status": _as_str(it.get("status")),
            "Material_Status": _as_str(it.get("Material_Status")),
            "FI_Rel_text": _as_str(it.get("FI_Rel_text")),
            "SBU_RelStatus": _as_str(it.get("SBU_RelStatus")),
            "CustomerCity": _as_str(it.get("CustomerCity")),
            "customer": _as_str(it.get("customer")),
            "bay": _bay_of_bin(it.get("bin")),
            "type_group": _type_group(it),
        })
    return out


def _build_customer_lots_for_group(
    candidates: List[dict],
    *,
    target_caps: Tuple[float, float, float] = CAPS,
) -> Tuple[List[dict], List[dict]]:
    """
    Build lots for one (customer, city, bay, type_group) group.

    Returns:
      (lots, leftovers)
    lots: list of {target_tons, total_weight, items, move_suggestions, ...}
    leftovers: eligible items not used in full lots (including unknown/0 weight)
    """
    lots: List[dict] = []
    remaining = list(candidates)

    total_w = _sum_weights(remaining)
    plan = _make_capacity_plan(total_w)

    # For each planned truck size, try to pack without exceeding.
    for cap in plan:
        chosen, chosen_w, remaining2 = _pack_lot_no_exceed(remaining, cap)

        # If we failed to even get close (e.g., only tiny weights), stop planning.
        # But keep leftovers for later remainder / city clubbing.
        if not chosen:
            break

        # If chosen weight is too low to be a meaningful truck (e.g., < 0.5t),
        # we don't create a lot. (Still keep for remainder logic.)
        if chosen_w <= 0.0:
            remaining = remaining2
            continue

        move_suggestions = _suggest_consolidation_moves(chosen)
        cust, city = _display_customer_city(chosen)
        bay = _bay_of_bin(chosen[0].get("bin"))
        tg = _type_group(chosen[0])

        lots.append({
            "customer": cust,
            "city": city,
            "bay": bay,
            "type_group": tg,
            "total_weight": round(chosen_w, 3),
            "target_tons": cap,
            "count": len(chosen),
            "items": _items_payload(chosen),
            "move_suggestions": move_suggestions,
            "lot_kind": "CUSTOMER",
        })

        remaining = remaining2

    # Whatever remains (including <32 total, and including 0/unknown weight) becomes leftovers
    return lots, remaining


def _build_city_clubbed_lots(
    leftovers: List[dict],
) -> Tuple[List[dict], List[dict]]:
    """
    Club leftovers across customers by (city, bay, type_group).
    Still no cross-bay and no plate/coil mixing.
    Build as many 40/36/32 as possible, and then output final remainder even if <32.
    """
    lots: List[dict] = []
    remaining_all: List[dict] = []

    by_key: Dict[Tuple[str, str, str], List[dict]] = defaultdict(list)
    for it in leftovers:
        if not _eligible(it):
            continue
        city_n = _norm_city(it.get("CustomerCity"))
        bay = _bay_of_bin(it.get("bin"))
        tg = _type_group(it)
        by_key[(city_n, bay, tg)].append(it)

    # top-first order inside each clubbing pool
    for k in by_key:
        by_key[k].sort(key=_stack_order_key)

    for (city_n, bay, tg), items in by_key.items():
        if not items:
            continue

        pool = list(items)
        total_w = _sum_weights(pool)
        plan = _make_capacity_plan(total_w)

        # pack planned full trucks
        for cap in plan:
            chosen, chosen_w, pool2 = _pack_lot_no_exceed(pool, cap)
            if not chosen:
                break
            if chosen_w <= 0.0:
                pool = pool2
                continue

            city_disp = _as_str(chosen[0].get("CustomerCity"))
            move_suggestions = _suggest_consolidation_moves(chosen)

            lots.append({
                "customer": _all_customer_names_label(chosen),
                "city": city_disp,
                "bay": bay,
                "type_group": tg,
                "total_weight": round(chosen_w, 3),
                "target_tons": cap,
                "count": len(chosen),
                "items": _items_payload(chosen),
                "move_suggestions": move_suggestions,
                "lot_kind": "CITY_MIXED",
            })
            pool = pool2

        # final remainder lot (even if <32), if anything is left
        # include items even if weight is unknown/0 so they still show up
        if pool:
            rem_w = _sum_weights(pool)
            if pool:  # always true here
                city_disp = _as_str(pool[0].get("CustomerCity"))
                move_suggestions = _suggest_consolidation_moves(pool)
                lots.append({
                    "customer": _all_customer_names_label(pool),
                    "city": city_disp,
                    "bay": bay,
                    "type_group": tg,
                    "total_weight": round(rem_w, 3),
                    "target_tons": 0.0,  # indicates remainder (not a full truck)
                    "count": len(pool),
                    "items": _items_payload(pool),
                    "move_suggestions": move_suggestions,
                    "lot_kind": "CITY_REMAINDER",
                })
            else:
                remaining_all.extend(pool)

    return lots, remaining_all


def build_dispatch_suggestions(
    assigned_bins: Dict[str, List[dict]],
    zones: List[dict],
    target_tons: float = 32.0,
    max_units: int = 200,
) -> dict:
    """
    NOTE: target_tons is kept for API compatibility but we build using fixed capacities 32/36/40.

    Returns:
      { "units": [ ... ] }
    """
    _ = _bin_meta_map(zones)  # reserved; kept for compatibility

    all_items = _flatten_items(assigned_bins)
    grouped = _group_candidates(all_items)

    units: List[dict] = []
    all_leftovers: List[dict] = []

    # 1) Build customer lots per (customer, city, bay, plate/coil)
    for (_cust_n, _city_n, _bay, _tg), candidates in grouped.items():
        if not candidates:
            continue

        lots, leftovers = _build_customer_lots_for_group(candidates)
        units.extend(lots)
        all_leftovers.extend(leftovers)

        if len(units) >= max_units:
            break

    # 2) Club leftovers by city (same city + same bay + same type_group)
    if len(units) < max_units and all_leftovers:
        mixed_lots, _unused = _build_city_clubbed_lots(all_leftovers)
        units.extend(mixed_lots)

    # 3) Final sort: prefer CUSTOMER lots, then heavier first, then count
    kind_rank = {
        "CUSTOMER": 0,
        "CITY_MIXED": 1,
        "CITY_REMAINDER": 2,
    }

    def sort_key(u: dict):
        rk = kind_rank.get(u.get("lot_kind"), 9)
        return (
            rk,
            -_as_float(u.get("total_weight")),
            -int(u.get("count", 0)),
            _as_str(u.get("city")).lower(),
            _as_str(u.get("customer")).lower(),
            _as_str(u.get("bay")),
            _as_str(u.get("type_group")),
        )

    units.sort(key=sort_key)

    # cap output
    units = units[:max_units]

    return {"units": units}


def register_dispatch_suggestions_api(
    app,
    *,
    get_active_bin_entries: Callable[[], Dict[str, List[dict]]],
    zones_provider: Callable[[], List[dict]],
):
    """
    Registers:
      GET /api/dispatch_suggestions
      GET /api/dispatch_suggestions_ai  <-- NEW GEMINI AI ENDPOINT
    Query params (optional):
      - max_units=200
    """
    
    # ---------------- STANDARD ENDPOINT ----------------
    @app.get("/api/dispatch_suggestions")
    def api_dispatch_suggestions():
        try:
            max_units = int(request.args.get("max_units") or 200)
        except Exception:
            max_units = 200

        assigned_bins = get_active_bin_entries()
        zones = zones_provider() if zones_provider else []

        payload = build_dispatch_suggestions(
            assigned_bins=assigned_bins,
            zones=zones,
            target_tons=32.0,   # kept for compatibility; internal logic uses 40/36/32
            max_units=max_units,
        )
        return jsonify(payload)

    # ---------------- NEW AI ENDPOINT ----------------
    @app.get("/api/dispatch_suggestions_ai")
    def api_dispatch_suggestions_ai():
        try:
            max_units = int(request.args.get("max_units") or 200)
        except Exception:
            max_units = 200

        assigned_bins = get_active_bin_entries()
        zones = zones_provider() if zones_provider else []

        payload = build_dispatch_suggestions(
            assigned_bins=assigned_bins,
            zones=zones,
            target_tons=32.0,
            max_units=max_units,
        )
        
        # Pull API key from environment variables
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            payload["ai_summary"] = "Error: GEMINI_API_KEY environment variable not configured."
            return jsonify(payload)

        try:
            headers = {'Content-Type': 'application/json'}
            
            # Format a lightweight string so we don't overload the prompt
            summary_data = []
            for u in payload.get("units", []):
                summary_data.append(
                    f"Lot: {u.get('lot_kind')} | Customer: {u.get('customer')} | City: {u.get('city')} | "
                    f"Weight: {u.get('total_weight')}t | Type: {u.get('type_group')} | Bay: {u.get('bay')} | "
                    f"Items: {u.get('count')}"
                )
            
            # --- SMART CACHE LOGIC ---
            # Creates a unique fingerprint of current trucks. If it hasn't changed since the 
            # last 30-second poll, skip the API call to save your Free Tier Quota!
            data_string = "\n".join(summary_data)
            current_hash = hashlib.md5(data_string.encode('utf-8')).hexdigest()

            if _AI_CACHE["data_hash"] == current_hash and _AI_CACHE["summary"] and not _AI_CACHE["summary"].startswith(("AI API Error", "Failed to")):
                payload["ai_summary"] = _AI_CACHE["summary"]
                return jsonify(payload)

            if not summary_data:
                 prompt_text = "You are a logistics assistant. The data shows no trucks are currently ready for dispatch. Write a single, brief sentence stating there are no dispatch suggestions available at this time."
            else:
                prompt_text = (
                    "You are an expert logistics AI assistant. Below is a parsed list of suggested truck dispatches "
                    "for a steel yard. Please write a brief, human-readable summary for the yard manager. "
                    "Highlight the total number of full trucks ready, key destination cities, and any notable mixed "
                    "or remainder lots that need attention.\n\n"
                    "Data:\n" + data_string
                )

            data = {"contents": [{"parts": [{"text": prompt_text}]}]}
            
            # USE THE SUPPORTED ACTIVE MODEL FOR 2026: gemini-2.5-flash
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
            
            resp = requests.post(url, headers=headers, json=data, timeout=45)

            if resp.status_code == 200:
                result = resp.json()
                summary_text = result['candidates'][0]['content']['parts'][0]['text']
                payload["ai_summary"] = summary_text
                
                # Update the cache with the successful response
                _AI_CACHE["data_hash"] = current_hash
                _AI_CACHE["summary"] = summary_text
                _AI_CACHE["timestamp"] = time.time()
                
            elif resp.status_code == 429:
                # If we hit the rate limit but have a cached version, display the cached version safely
                if _AI_CACHE["summary"]:
                    payload["ai_summary"] = _AI_CACHE["summary"] + "\n\n(Note: Cached summary shown due to API rate limits)"
                else:
                    payload["ai_summary"] = "AI API Error 429: Free Tier Quota Exceeded. The system is paused to respect limits. Please try again later."
            else:
                payload["ai_summary"] = f"AI API Error {resp.status_code}: {resp.text}"

        except Exception as e:
            # Safely catch ANY API error so the screen never blanks out
            payload["ai_summary"] = f"Failed to connect to AI: {str(e)}"

        return jsonify(payload)