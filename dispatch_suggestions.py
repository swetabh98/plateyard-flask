# dispatch_suggestions.py
from __future__ import annotations

import math
import re
from dataclasses import dataclass, asdict
from typing import Any, Callable, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request

# -----------------------------------------------------------------------------
# Dispatch Suggestions + Consolidation Move Plan
#
# This module is designed to avoid DB-table-name assumptions.
# It works directly from your live in-memory snapshot:
#   assigned_bins = get_active_bin_entries()  # { "EF34A": [item,...], ... }
# where each item is a dict (row from plates table) and has `seq` set by app.py.
#
# Register from app.py:
#   from dispatch_suggestions import register_dispatch_suggestions_api
#   register_dispatch_suggestions_api(app,
#       get_active_bin_entries=get_active_bin_entries,
#       zones_provider=lambda: enrich_zones_from_labels(full_layout),
#   )
#
# Endpoint:
#   GET /api/dispatch_suggestions?limit=8&target_ton=32
# -----------------------------------------------------------------------------

dispatch_bp = Blueprint("dispatch_suggestions", __name__)

# Accept a broad range of FI release strings (case/spacing tolerant)
_FI_ALLOWED_RAW = {
    "FI RELEASED",
    "FI RELEASED(2)",
    "FI_RELEASED",
    "FI_RELEASED(2)",
    "FIRELEASED",
    "FIRELEASED(2)",
}

def _norm_text(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    # collapse spaces
    s = re.sub(r"\s+", " ", s)
    return s

def _norm_key(v: Any) -> str:
    s = _norm_text(v).upper()
    s = s.replace(" ", "")
    return s

def _is_fi_released(v: Any) -> bool:
    k = _norm_key(v)
    return k in _FI_ALLOWED_RAW

def _safe_float(v: Any) -> float:
    try:
        if v is None or v == "":
            return 0.0
        return float(str(v).replace(",", "").strip())
    except Exception:
        return 0.0

def _weight_to_ton(v: Any) -> float:
    """
    Heuristic:
      - if weight >= 1000 => assume KG and convert to tons
      - else assume already in tons
    """
    w = _safe_float(v)
    if w <= 0:
        return 0.0
    if w >= 1000:
        return w / 1000.0
    return w

def _bin_prefix(bin_name: str) -> str:
    m = re.match(r"^(EF|AC)\d{2}[A-G]$", (bin_name or "").upper())
    return m.group(1) if m else ""

def _allows_bin_for_type(bin_name: str, item_type: str) -> bool:
    pref = _bin_prefix(bin_name)
    t = (_norm_text(item_type) or "Plate").strip().lower()
    if "coil" in t:
        return pref == "AC"
    return pref in ("EF", "AC")

def _top_item_in_stack(items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not items:
        return None
    # seq created in app.py increases as you go up the stack.
    # max seq => top
    return max(items, key=lambda it: int(it.get("seq") or 0))

def _blocked_by_count(items: List[Dict[str, Any]], seq: int) -> int:
    if not items:
        return 0
    top_seq = max(int(it.get("seq") or 0) for it in items)
    return max(top_seq - int(seq or 0), 0)

def _distance(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])

def _extract_loading_anchors(zones: List[Dict[str, Any]]) -> List[Tuple[float, float]]:
    """
    Prefer TRUCK/LOADING anchors because most dispatch is by road.
    If none exist in zone names, return all bin centers as a fallback.
    """
    anchors: List[Tuple[float, float]] = []
    for z in zones or []:
        name = _norm_text(z.get("zone")).lower()
        if ("truck" in name) or ("loading" in name):
            cx = _safe_float(z.get("cx"))
            cy = _safe_float(z.get("cy"))
            if cx or cy:
                anchors.append((cx, cy))
    return anchors

def _candidate_staging_bins(
    zones: List[Dict[str, Any]],
    assigned_bins: Dict[str, List[Dict[str, Any]]],
    item_type: str,
    k: int = 6,
) -> List[Tuple[str, float]]:
    """
    Pick staging bins near loading anchors with low occupancy.
    Returns list of (bin_code, score) where lower score is better.
    """
    anchors = _extract_loading_anchors(zones)
    # If no anchors, place at center-ish of yard (still gives deterministic result)
    if not anchors:
        anchors = [(0.0, 0.0)]

    out: List[Tuple[str, float]] = []
    for z in zones or []:
        b = _norm_text(z.get("bin")).upper()
        if not b:
            continue
        if not _allows_bin_for_type(b, item_type):
            continue

        occ = len(assigned_bins.get(b, []))
        # prefer empty or almost empty bins for consolidation
        if occ > 1:
            continue

        cx = _safe_float(z.get("cx"))
        cy = _safe_float(z.get("cy"))

        # score = distance to nearest anchor + small penalty for occupied bin
        d = min(_distance((cx, cy), a) for a in anchors)
        score = d + (occ * 50.0)  # penalty
        out.append((b, score))

    out.sort(key=lambda t: t[1])
    return out[:k]

@dataclass
class DispatchItem:
    plate_id: str
    item_type: str
    bin: str
    seq: int
    blocked_by: int
    status: str
    customer: str
    customer_city: str
    fi_rel_text: str
    sbu_relstatus: str
    weight_ton: float

@dataclass
class DispatchUnit:
    customer: str
    customer_city: str
    target_ton: float
    total_ton: float
    dispatch_items: List[DispatchItem]        # the plates you can dispatch NOW (top of stacks)
    consolidation_bin: str                    # where to consolidate this customer's plates
    consolidation_moves: List[Dict[str, Any]] # move plan for ALL plates (even buried)

def _make_item(it: Dict[str, Any], *, bin_code: str, stack: List[Dict[str, Any]]) -> DispatchItem:
    return DispatchItem(
        plate_id=_norm_text(it.get("plate_id") or it.get("Plate") or it.get("id")),
        item_type=_norm_text(it.get("type") or "Plate"),
        bin=bin_code,
        seq=int(it.get("seq") or 0),
        blocked_by=_blocked_by_count(stack, int(it.get("seq") or 0)),
        status=_norm_text(it.get("status")),
        customer=_norm_text(it.get("customer")),
        customer_city=_norm_text(it.get("CustomerCity") or it.get("customer_city")),
        fi_rel_text=_norm_text(it.get("FI_Rel_text")),
        sbu_relstatus=_norm_text(it.get("SBU_RelStatus")),
        weight_ton=round(_weight_to_ton(it.get("weight")), 3),
    )

def _build_units(
    assigned_bins: Dict[str, List[Dict[str, Any]]],
    zones: List[Dict[str, Any]],
    target_ton: float,
    limit: int,
) -> List[DispatchUnit]:
    """
    1) Consider ALL customers (city-wise).
    2) For dispatch-now items: use only TOP-OF-STACK items that are FI Released.
    3) For consolidation: include ALL items for that customer+city (even buried) and suggest moves.
    """
    # Flatten inventory with stack context
    by_customer_city: Dict[Tuple[str, str], List[Tuple[str, Dict[str, Any], List[Dict[str, Any]]]]] = {}
    for b, stack in (assigned_bins or {}).items():
        bin_code = _norm_text(b).upper()
        if not bin_code:
            continue
        for it in stack:
            cust = _norm_text(it.get("customer"))
            city = _norm_text(it.get("CustomerCity") or it.get("customer_city")) or "Unknown"
            if not cust:
                # if customer missing, still group under "Unknown Customer" so UI shows something
                cust = "Unknown Customer"
            by_customer_city.setdefault((cust, city), []).append((bin_code, it, stack))

    units: List[DispatchUnit] = []

    # Build for each group
    for (cust, city), entries in by_customer_city.items():
        # Determine staging bin for this group (use plate type preference based on majority)
        type_votes = {"Plate": 0, "Coil": 0}
        for _, it, _ in entries:
            t = _norm_text(it.get("type") or "Plate").lower()
            if "coil" in t:
                type_votes["Coil"] += 1
            else:
                type_votes["Plate"] += 1
        group_type = "Coil" if type_votes["Coil"] > type_votes["Plate"] else "Plate"

        staging_choices = _candidate_staging_bins(zones, assigned_bins, group_type, k=8)
        consolidation_bin = staging_choices[0][0] if staging_choices else ""

        # Consolidation moves for ALL items in group (including buried)
        moves: List[Dict[str, Any]] = []
        for bin_code, it, stack in entries:
            pid = _norm_text(it.get("plate_id"))
            if not pid:
                continue
            seq = int(it.get("seq") or 0)
            blocked = _blocked_by_count(stack, seq)

            desired = consolidation_bin or bin_code
            # keep items already in the consolidation bin as-is
            needs_move = (consolidation_bin and bin_code != consolidation_bin)

            moves.append({
                "plate_id": pid,
                "from_bin": bin_code,
                "to_bin": desired,
                "blocked_by": blocked,  # 0 means it's on top (can move now)
                "can_move_now": (blocked == 0),
                "note": ("Top of stack" if blocked == 0 else f"Blocked by {blocked} item(s) above"),
            })

        # Dispatch-now candidates: FI Released AND top-of-stack per bin
        top_by_bin: Dict[str, Dict[str, Any]] = {}
        stack_by_bin: Dict[str, List[Dict[str, Any]]] = {}
        for bin_code, it, stack in entries:
            stack_by_bin[bin_code] = stack
            top = _top_item_in_stack(stack)
            if top is not None:
                top_by_bin[bin_code] = top

        dispatch_candidates: List[Tuple[float, DispatchItem]] = []
        for bin_code, top in top_by_bin.items():
            if _norm_text(top.get("customer")) != cust:
                continue
            top_city = _norm_text(top.get("CustomerCity") or top.get("customer_city")) or "Unknown"
            if top_city != city:
                continue
            if not _is_fi_released(top.get("FI_Rel_text")):
                # also allow some common variants in SBU status if FI field is blank
                sbu = _norm_key(top.get("SBU_RelStatus"))
                if "RELEASE" not in sbu:
                    continue

            ditem = _make_item(top, bin_code=bin_code, stack=stack_by_bin.get(bin_code, []))
            w = ditem.weight_ton
            if w <= 0:
                continue

            # priority: FI Released(2) first, then heavier first, then closer to top (always 0 blocked here)
            fi = _norm_key(ditem.fi_rel_text)
            fi_rank = 0 if "RELEASED(2)" in fi else 1
            score = (fi_rank * 1_000_000.0) - (w * 1000.0)
            dispatch_candidates.append((score, ditem))

        dispatch_candidates.sort(key=lambda t: t[0])

        # Greedy pack into one unit per customer+city (you can expand to multiple units later)
        chosen: List[DispatchItem] = []
        total = 0.0
        overshoot = 0.8  # allow small overshoot
        for _, ditem in dispatch_candidates:
            if total + ditem.weight_ton <= target_ton + overshoot:
                chosen.append(ditem)
                total += ditem.weight_ton
            # close enough
            if total >= target_ton - 0.4:
                break

        # If nothing eligible for immediate dispatch, still return a "consolidation-only" unit
        # so UI doesn't say "no units found".
        units.append(
            DispatchUnit(
                customer=cust,
                customer_city=city,
                target_ton=target_ton,
                total_ton=round(total, 3),
                dispatch_items=chosen,
                consolidation_bin=consolidation_bin,
                consolidation_moves=moves,
            )
        )

    # Ranking: units with more dispatchable weight first, then more dispatch items
    units.sort(key=lambda u: (-u.total_ton, -len(u.dispatch_items), u.customer, u.customer_city))
    return units[: max(limit, 1)]

def register_dispatch_suggestions_api(
    app,
    *,
    get_active_bin_entries: Callable[[], Dict[str, List[Dict[str, Any]]]],
    zones_provider: Callable[[], List[Dict[str, Any]]],
) -> None:
    """
    Call this from app.py once during init.
    """
    @dispatch_bp.get("/api/dispatch_suggestions")
    def api_dispatch_suggestions():
        target_ton = float(request.args.get("target_ton") or 32.0)
        limit = int(request.args.get("limit") or 12)

        assigned_bins = get_active_bin_entries() or {}
        zones = zones_provider() or []

        units = _build_units(assigned_bins, zones, target_ton=target_ton, limit=limit)
        payload = [asdict(u) for u in units]

        # Convenience counts for header badges
        total_dispatch_items = sum(len(u.dispatch_items) for u in units)
        total_consolidation_moves = sum(len(u.consolidation_moves) for u in units)

        return jsonify({
            "ok": True,
            "count": len(payload),
            "total_dispatch_items": total_dispatch_items,
            "total_consolidation_moves": total_consolidation_moves,
            "units": payload,
        })

    # Register blueprint once
    if "dispatch_suggestions" not in app.blueprints:
        app.register_blueprint(dispatch_bp)
