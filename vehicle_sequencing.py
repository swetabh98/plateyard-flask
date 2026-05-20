"""
Vehicle Sequencing (LOT-BASED) for Plate Mill Yard Management

Enhancements (aligned + bulletproof):
1) Remove routing: `stops` is now an "items list" (same field name to avoid UI break).
2) Lock/Release workflow: prevent double assignment across sessions/users.
3) Inventory hash + caching: stable suggestions until inventory changes (or cache TTL).
4) Align sequencing order to dispatch_suggestions_citywise.py logic:
   - CUSTOMER lots first
   - Then CITY_MIXED
   - Then CITY_REMAINDER
   - Weight desc, count desc, city/customer/bay/type_group tie-breakers

STRICT eligibility stays intact via dispatch_suggestions_citywise `_eligible` rules:
- status must indicate FG / Finished Good
- FI_Rel_text must be ONLY: "FI Released" or "FI Released (2)" (NO (1))
- No cross-bay transfers (EF/DE/AC/CD/CTL.. kept isolated by LP filters)
- No mixing: bay and type_group are never mixed (handled by dispatch grouping),
  plus vehicles are filled per unit in order.

New (required for crane-span reduction):
- CUSTOMER lots are assigned to ONLY ONE loading point: the closest LP (min distance of any eligible item/bin to LP anchor).
  This prevents same customer being suggested across different LPs in the same bay.

Fixes requested:
- Trailer Loading Point 4 anchor corrected to DE34 (was EF37*).
- Add CTLCD Loading Point and CTLDE Loading Point.

Critical Fix (this patch):
- Previously vehicles were created ONLY from FULL (>=32t) lots; REMAINDER lots were ignored.
  This caused CTL LPs to show EMPTY when lots were <32t, and also prevented "next vehicles"
  from appearing for any LP.
- Now: FULL lots fill first, then REMAINDER lots fill remaining vehicles (next best suggestions).
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple
import hashlib
import json
import math
import re
import os
import threading
import traceback

from flask import jsonify, request

from dispatch_suggestions_citywise import build_dispatch_suggestions  # type: ignore


VEH_SEQ_VERSION = "2026-02-12.lots.v7.lp_ranges_priorities.lp11_lp12"

# -----------------------------
# Lock store (in-memory)
# -----------------------------
_LOCKS_TTL_MIN_DEFAULT = 60
_lock_mu = threading.Lock()
_locks: Dict[str, Dict[str, Any]] = {}  # plate_id -> meta

# -----------------------------
# Cache store (in-memory)
# -----------------------------
_CACHE_TTL_SEC_DEFAULT = 15 * 60
_cache_mu = threading.Lock()
_cache: Dict[str, Dict[str, Any]] = {}


def _log(msg: str) -> None:
    print(f"[vehicle_sequencing:{VEH_SEQ_VERSION}] {msg}")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _cleanup_expired_locks(now: Optional[datetime] = None) -> None:
    now = now or _now_utc()
    with _lock_mu:
        expired = []
        for pid, meta in _locks.items():
            exp = _try_parse_dt(str(meta.get("expires_at_utc") or ""))
            if exp and exp <= now:
                expired.append(pid)
        for pid in expired:
            _locks.pop(pid, None)


def _get_active_locks_snapshot(now: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
    _cleanup_expired_locks(now=now)
    with _lock_mu:
        return dict(_locks)


def _locks_fingerprint(locks: Dict[str, Dict[str, Any]]) -> str:
    items = []
    for pid, meta in locks.items():
        items.append((pid, str(meta.get("expires_at_utc") or "")))
    items.sort()
    raw = json.dumps(items, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _compute_inventory_hash(entries: List[Dict[str, Any]]) -> str:
    rows = []
    for r in entries:
        if not isinstance(r, dict):
            continue
        pid = str(_pick_first(r, ["plate_id", "material_id", "id", "Plate_ID", "Material_ID"]) or "").strip()
        b = str(r.get("bin") or r.get("bin_id") or r.get("BIN") or "").strip().upper().replace(" ", "")
        if not pid or not b:
            continue
        seq = _as_int(r.get("seq", r.get("stack_pos", 0)), 0, -10_000, 10_000)
        st = str(_pick_first(r, ["status", "Status"]) or "")
        fi = str(_pick_first(r, ["FI_Rel_text", "fi_rel_text", "FI_Rel", "fi"]) or "")
        cust = str(_pick_first(r, ["customer", "CustomerName", "Customer", "customer_name", "cust_name"]) or "")
        city = str(_pick_first(r, ["CustomerCity", "customer_city", "City", "city"]) or "")
        typ = str(_pick_first(r, ["type", "material_type", "Material_Type"]) or "")
        wt = str(_pick_first(r, ["weight", "Weight", "wt", "Qty"]) or "")
        rows.append((b, seq, pid, st, fi, cust, city, typ, wt))
    rows.sort()
    raw = json.dumps(rows, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _cache_key_from_params(params: Dict[str, Any]) -> str:
    raw = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _cache_get(cache_key: str) -> Optional[Dict[str, Any]]:
    now = _now_utc().timestamp()
    with _cache_mu:
        item = _cache.get(cache_key)
        if not item:
            return None
        created_at = float(item.get("created_at") or 0.0)
        ttl = float(item.get("ttl_sec") or _CACHE_TTL_SEC_DEFAULT)
        if now - created_at > ttl:
            _cache.pop(cache_key, None)
            return None
        return dict(item)


def _cache_put(cache_key: str, payload: Dict[str, Any], inventory_hash: str, locks_fp: str, ttl_sec: int) -> None:
    now = _now_utc().timestamp()
    with _cache_mu:
        _cache[cache_key] = {
            "created_at": now,
            "ttl_sec": int(ttl_sec),
            "payload": payload,
            "inventory_hash": inventory_hash,
            "locks_fingerprint": locks_fp,
        }


# -----------------------------------------------------------------------------
# Public register
# -----------------------------------------------------------------------------

def register_vehicle_sequencing_api(
    app,
    get_active_bin_entries_fn: Callable[[], Any],
    enrich_layout_bins_fn: Callable[[], List[Dict[str, Any]]],
):
    # -----------------------------
    # Locks API
    # -----------------------------
    @app.get("/api/vehicle_sequencing/locks")
    def api_vehicle_sequencing_locks():
        now = _now_utc()
        locks = _get_active_locks_snapshot(now=now)
        return jsonify(
            {
                "version": VEH_SEQ_VERSION,
                "generated_at_utc": now.isoformat(),
                "count": len(locks),
                "locks": [
                    {
                        "plate_id": pid,
                        "locked_by": meta.get("locked_by"),
                        "locked_at_utc": meta.get("locked_at_utc"),
                        "expires_at_utc": meta.get("expires_at_utc"),
                    }
                    for pid, meta in sorted(locks.items(), key=lambda kv: kv[0])
                ],
            }
        )

    @app.post("/api/vehicle_sequencing/lock")
    def api_vehicle_sequencing_lock():
        now = _now_utc()
        _cleanup_expired_locks(now=now)

        body = request.get_json(silent=True) or {}
        plate_ids = body.get("plate_ids") or []
        if not isinstance(plate_ids, list):
            plate_ids = []

        locked_by = str(body.get("locked_by") or "").strip() or (request.remote_addr or "user")
        ttl_min = _as_int(body.get("ttl_min"), _LOCKS_TTL_MIN_DEFAULT, 1, 24 * 60)

        exp = now + timedelta(minutes=int(ttl_min))

        added = []
        already = []
        with _lock_mu:
            for pid in plate_ids:
                pid2 = str(pid or "").strip()
                if not pid2:
                    continue
                if pid2 in _locks:
                    already.append(pid2)
                    continue
                _locks[pid2] = {
                    "locked_by": locked_by,
                    "locked_at_utc": now.isoformat(),
                    "expires_at_utc": exp.isoformat(),
                }
                added.append(pid2)

        return jsonify(
            {
                "version": VEH_SEQ_VERSION,
                "ok": True,
                "added": added,
                "already_locked": already,
                "expires_at_utc": exp.isoformat(),
            }
        )

    @app.post("/api/vehicle_sequencing/release")
    def api_vehicle_sequencing_release():
        now = _now_utc()
        _cleanup_expired_locks(now=now)

        body = request.get_json(silent=True) or {}
        plate_ids = body.get("plate_ids") or []
        if not isinstance(plate_ids, list):
            plate_ids = []

        removed = []
        with _lock_mu:
            for pid in plate_ids:
                pid2 = str(pid or "").strip()
                if not pid2:
                    continue
                if pid2 in _locks:
                    _locks.pop(pid2, None)
                    removed.append(pid2)

        return jsonify({"version": VEH_SEQ_VERSION, "ok": True, "removed": removed})

    # -----------------------------
    # Main sequencing API
    # -----------------------------
    @app.get("/api/vehicle_sequencing")
    def api_vehicle_sequencing():
        debug = _as_bool01(request.args.get("debug"), default=False) or (
            os.getenv("VEH_SEQ_DEBUG", "0").strip() == "1"
        )

        try:
            max_stops_per_vehicle = _as_int(request.args.get("max_stops_per_vehicle"), 12, 1, 500)

            pickable_only = _as_bool01(request.args.get("pickable_only"), default=True)
            max_rehandles_default = 3 if pickable_only else 9999
            max_rehandles = _as_int(request.args.get("max_rehandles"), max_rehandles_default, 0, 9999)

            max_bins_considered_req = _as_int(request.args.get("max_bins_considered"), 5000, 10, 200000)
            max_items_per_bin = _as_int(request.args.get("max_items_per_bin"), 10, 1, 200)

            customer_filter = str(request.args.get("customer") or "").strip()
            customer_filter_norm = customer_filter.lower() if customer_filter else ""

            type_group_filter = str(request.args.get("type_group") or "").strip().upper()
            if type_group_filter not in ("", "PLATE", "COIL"):
                type_group_filter = ""

            diversify_customers = _as_bool01(request.args.get("diversify_customers"), default=True)
            cache_ttl_sec = _as_int(request.args.get("cache_ttl_sec"), _CACHE_TTL_SEC_DEFAULT, 0, 24 * 3600)

            now = _now_utc()

            # 1) Layout bins
            layout_bins = enrich_layout_bins_fn() or []
            layout_map: Dict[str, Dict[str, Any]] = {}
            for b in layout_bins:
                if not isinstance(b, dict):
                    continue
                bid = str(b.get("bin") or b.get("id") or "").strip()
                if bid:
                    layout_map[bid] = b

            full_layout_count = len(layout_map)
            max_bins_considered = max(full_layout_count, max_bins_considered_req)

            # 2) Active entries
            raw_entries = get_active_bin_entries_fn()
            entries: List[Dict[str, Any]] = _normalize_entries(raw_entries)

            if customer_filter_norm:
                entries = [r for r in entries if customer_filter_norm in _norm(_pick_customer_name(r))]

            inventory_hash = _compute_inventory_hash(entries)

            locks = _get_active_locks_snapshot(now=now)
            locks_fp = _locks_fingerprint(locks)
            locked_plate_ids = set(locks.keys())

            # Cache check
            params_for_cache = {
                "max_stops_per_vehicle": max_stops_per_vehicle,
                "pickable_only": pickable_only,
                "max_rehandles": max_rehandles,
                "max_bins_considered": max_bins_considered,
                "max_items_per_bin": max_items_per_bin,
                "customer": customer_filter_norm,
                "type_group": type_group_filter,
                "diversify_customers": diversify_customers,
            }
            ck = _cache_key_from_params(params_for_cache)
            cached = _cache_get(ck)
            if cached and cached.get("inventory_hash") == inventory_hash and cached.get("locks_fingerprint") == locks_fp:
                payload = dict(cached.get("payload") or {})
                payload["cached"] = True
                payload["cache"] = {
                    "inventory_hash": inventory_hash,
                    "locks_fingerprint": locks_fp,
                    "ttl_sec": cache_ttl_sec,
                }
                return jsonify(payload)

            # 3) Group by bin
            bins_to_stack: Dict[str, List[Dict[str, Any]]] = {}
            for it in entries:
                if not isinstance(it, dict):
                    continue
                bin_id = str(it.get("bin") or it.get("bin_id") or it.get("BIN") or "").strip()
                if bin_id:
                    bins_to_stack.setdefault(bin_id, []).append(it)

            for bin_id, stack in bins_to_stack.items():
                stack.sort(key=lambda x: _as_int(x.get("stack_pos", x.get("seq")), 0), reverse=False)

            # 4) Per-LP sequencing
            loading_points = _loading_point_config()

            lp_work: List[Dict[str, Any]] = []
            customer_best_dist: Dict[str, Tuple[float, str]] = {}

            for lp in loading_points:
                anchor_xy = _compute_anchor_xy(lp["anchor_bins"], layout_map)
                allowed_bays = lp.get("allowed_bays") or _allowed_bays_from_prefixes(lp.get("allowed_prefixes"))
                allowed_prefixes = lp.get("allowed_prefixes")
                vehicle_count = int(lp["vehicle_count"])

                candidate_bins: List[Tuple[float, str]] = []
                if anchor_xy is not None:
                    for bid, b in layout_map.items():
                        if allowed_prefixes and not any(str(bid).startswith(pfx) for pfx in allowed_prefixes):
                            continue
                        if allowed_bays and (_bay_of_bin(bid) not in allowed_bays):
                            continue
                        # NEW: enforce LP range families (vehicle sequencing update)
                        if not _bin_in_lp_family(str(bid), _lp_family_id(str(lp["id"]))):
                            continue
                        cx, cy = b.get("cx"), b.get("cy")
                        if cx is None or cy is None:
                            continue
                        d = _euclid(anchor_xy[0], anchor_xy[1], float(cx), float(cy))
                        candidate_bins.append((d, bid))

                candidate_bins.sort(key=lambda t: t[0])
                candidate_bins = candidate_bins[:max_bins_considered]

                assigned_bins_for_lp: Dict[str, List[Dict[str, Any]]] = {}
                candidate_items_count = 0
                filtered_locked_count = 0

                for dist_to_anchor, bid in candidate_bins:
                    stack = bins_to_stack.get(bid) or []
                    if not stack:
                        continue

                    n = len(stack)
                    out_items: List[Dict[str, Any]] = []

                    for i in range(n - 1, -1, -1):
                        rehandles = (n - 1) - i
                        if rehandles > max_rehandles:
                            continue

                        row = stack[i]
                        if not isinstance(row, dict):
                            continue

                        pid = str(_pick_first(row, ["plate_id", "material_id", "id", "Plate_ID", "Material_ID"]) or "").strip()
                        if not pid:
                            continue

                        if pid in locked_plate_ids:
                            filtered_locked_count += 1
                            continue

                        tg = _type_group_from_row(_pick_first(row, ["type", "material_type", "Material_Type"]))
                        if type_group_filter and tg != type_group_filter:
                            continue

                        cust_name = _pick_customer_name(row)

                        item = {
                            "plate_id": pid,
                            "bin": str(bid),
                            "seq": _as_int(_pick_first(row, ["seq", "stack_pos", "stack_position", "pos"]), i, -10_000, 10_000),
                            "rehandles": int(rehandles),
                            "pickable": True,
                            "type": str(_pick_first(row, ["type", "material_type", "Material_Type"]) or ""),
                            "weight": _pick_first(row, ["weight", "Weight", "wt", "Qty"]),
                            "status": _pick_first(row, ["status", "Status"]),
                            "FI_Rel_text": _pick_first(row, ["FI_Rel_text", "fi_rel_text", "FI_Rel", "fi"]),
                            "customer": cust_name,
                            "CustomerCity": _pick_customer_city(row),
                        }
                        out_items.append(item)
                        candidate_items_count += 1

                        if cust_name:
                            prev = customer_best_dist.get(cust_name)
                            if prev is None or float(dist_to_anchor) < prev[0]:
                                customer_best_dist[cust_name] = (float(dist_to_anchor), str(lp["id"]))

                        if len(out_items) >= max_items_per_bin:
                            break

                    if out_items:
                        assigned_bins_for_lp[str(bid)] = out_items

                lp_work.append(
                    {
                        "lp": lp,
                        "anchor_xy": anchor_xy,
                        "allowed_bays": allowed_bays,
                        "allowed_prefixes": allowed_prefixes,
                        "vehicle_count": vehicle_count,
                        "candidate_bins": candidate_bins,
                        "assigned_bins_for_lp": assigned_bins_for_lp,
                        "candidate_items_count": candidate_items_count,
                        "filtered_locked_count": filtered_locked_count,
                    }
                )

            results: List[Dict[str, Any]] = []
            global_used_plate_ids: set = set()
            global_used_customers: set = set()

            for w in lp_work:
                lp = w["lp"]
                anchor_xy = w["anchor_xy"]
                allowed_bays = w["allowed_bays"]
                allowed_prefixes = w["allowed_prefixes"]
                vehicle_count = w["vehicle_count"]
                candidate_bins = w["candidate_bins"]
                assigned_bins_for_lp = w["assigned_bins_for_lp"]
                candidate_items_count = w["candidate_items_count"]
                filtered_locked_count = w["filtered_locked_count"]

                ds_payload = build_dispatch_suggestions(
                    assigned_bins=assigned_bins_for_lp,
                    zones=[],
                    target_tons=32.0,
                    max_units=5000,
                )
                units = ds_payload.get("units") or []
                if not isinstance(units, list):
                    units = []

                # enforce CUSTOMER-to-closest-LP
                lp_id = str(lp["id"])
                filtered_units: List[Dict[str, Any]] = []
                suppressed_customers = 0
                for u in units:
                    if not isinstance(u, dict):
                        continue
                    lot_kind_src = str(u.get("lot_kind") or "")
                    cust = str(u.get("customer") or "").strip()
                    if lot_kind_src == "CUSTOMER" and cust:
                        best = customer_best_dist.get(cust)
                        if best and best[1] != lp_id:
                            suppressed_customers += 1
                            continue
                    filtered_units.append(u)
                units = filtered_units

                # optional soft diversification
                if diversify_customers and global_used_customers:
                    def seen_first(u: Dict[str, Any]) -> int:
                        cust2 = str(u.get("customer") or "").strip()
                        if " + " in cust2:
                            parts = [p.strip() for p in cust2.split("+")]
                            return 1 if any(p in global_used_customers for p in parts) else 0
                        return 1 if cust2 in global_used_customers else 0

                    indexed = list(enumerate(units))
                    indexed.sort(key=lambda t: (seen_first(t[1]), t[0]))
                    units = [u for _, u in indexed]

                lots_priority: List[Dict[str, Any]] = []
                for u in units:
                    lot_kind_src = str(u.get("lot_kind") or "")
                    target = float(_as_float(u.get("target_tons"), 0.0, 0.0, 1e12))
                    total_w = float(_as_float(u.get("total_weight"), 0.0, 0.0, 1e12))
                    items = u.get("items") or []
                    if not isinstance(items, list):
                        items = []

                    kind = "FULL" if target >= 32.0 - 1e-9 else "REMAINDER"

                    bins_preview = sorted(
                        list({str(it.get("bin") or "").upper().replace(" ", "") for it in items if it.get("bin")})
                    )[:10]

                    lots_priority.append(
                        {
                            "lot_kind": kind,
                            "source_kind": lot_kind_src,
                            "vehicle_customer": str(u.get("customer") or "").strip(),
                            "vehicle_city": str(u.get("city") or "").strip(),
                            "type_group": str(u.get("type_group") or "").strip(),
                            "bay": str(u.get("bay") or "").strip(),
                            "target_tons": target if target > 0 else None,
                            "total_weight": round(total_w, 3),
                            "items_count": int(u.get("count") or len(items) or 0),
                            "preview_bins": bins_preview,
                            "_items_internal": items,
                        }
                    )

                # --------------------------
                # FIX: assign vehicles from FULL first, THEN REMAINDER (next vehicles)
                # --------------------------
                vehicles_out: List[Dict[str, Any]] = []
                lp_used_plate_ids = set()

                full_lots = [lot for lot in lots_priority if lot.get("lot_kind") == "FULL"]
                rem_lots = [lot for lot in lots_priority if lot.get("lot_kind") == "REMAINDER"]
                lots_in_fill_order = full_lots + rem_lots  # FULL priority, but don't drop remainder

                vehicle_no = 1
                for lot in lots_in_fill_order:
                    if vehicle_no > vehicle_count:
                        break

                    chosen_items: List[Dict[str, Any]] = []
                    for it in lot.get("_items_internal", []) or []:
                        pid = str(it.get("plate_id") or it.get("item_id") or "").strip()
                        if not pid:
                            continue
                        if pid in lp_used_plate_ids:
                            continue
                        if pid in global_used_plate_ids:
                            continue
                        if pid in locked_plate_ids:
                            continue
                        chosen_items.append(it)

                    if not chosen_items:
                        continue

                    for it in chosen_items:
                        pid = str(it.get("plate_id") or "").strip()
                        if pid:
                            lp_used_plate_ids.add(pid)
                            global_used_plate_ids.add(pid)

                    if diversify_customers:
                        cust = str(lot.get("vehicle_customer") or "").strip()
                        if cust:
                            global_used_customers.add(cust)

                    stops_out = _items_to_stops_list(chosen_items, layout_map=layout_map, anchor_xy=anchor_xy)

                    v_bins = sorted(list({str(s.get("bin_id") or "").strip().upper().replace(" ", "") for s in stops_out if s.get("bin_id")}))

                    cap = _infer_truck_capacity_tons(
                        total_weight=float(lot.get("total_weight") or 0.0),
                        target_tons=lot.get("target_tons"),
                    )
                    pr = _compute_vehicle_priority(
                        loading_point_id=str(lp.get("id")),
                        vehicle_customer=str(lot.get("vehicle_customer") or "").strip(),
                        vehicle_bins=v_bins,
                        truck_capacity_tons=cap,
                        total_weight=float(lot.get("total_weight") or 0.0),
                    )

                    vehicles_out.append(
                        {
                            "vehicle_no": vehicle_no,
                            "vehicle_customer": lot.get("vehicle_customer"),
                            "vehicle_city": lot.get("vehicle_city"),
                            "type_group": lot.get("type_group"),
                            "target_tons": lot.get("target_tons"),
                            "total_weight": round(float(lot.get("total_weight") or 0.0), 3),
                            "truck_capacity_tons": cap,
                            "priority": pr,
                            "stops": stops_out,
                            "lot_kind": lot.get("lot_kind"),
                            "source_kind": lot.get("source_kind"),
                            "loading_point_id": lp["id"],
                            "loading_point_name": lp["name"],
                        }
                    )
                    vehicle_no += 1

                while vehicle_no <= vehicle_count:
                    vehicles_out.append(
                        {
                            "vehicle_no": vehicle_no,
                            "vehicle_customer": None,
                            "vehicle_city": None,
                            "type_group": None,
                            "target_tons": None,
                            "total_weight": 0.0,
                            "truck_capacity_tons": None,
                            "priority": None,
                            "stops": [],
                            "lot_kind": "EMPTY",
                            "source_kind": None,
                            "loading_point_id": lp["id"],
                            "loading_point_name": lp["name"],
                        }
                    )
                    vehicle_no += 1

                lots_priority_out: List[Dict[str, Any]] = []
                for lot in lots_priority:
                    dct = dict(lot)
                    dct.pop("_items_internal", None)
                    lots_priority_out.append(dct)

                p1 = sum(1 for v in vehicles_out if int(v.get("priority") or 0) == 1)
                p2 = sum(1 for v in vehicles_out if int(v.get("priority") or 0) == 2)
                p3 = sum(1 for v in vehicles_out if int(v.get("priority") or 0) == 3)
                priorities_summary = {"P1": p1, "P2": p2, "P3": p3}
                results.append(
                    {
                        "id": lp["id"],
                        "name": lp["name"],
                        "vehicle_count": vehicle_count,
                        "anchor_bins": lp["anchor_bins"],
                        "allowed_prefixes": allowed_prefixes,
                        "allowed_bays": allowed_bays,
                        "anchor_xy": {"x": anchor_xy[0], "y": anchor_xy[1]} if anchor_xy else None,
                        "vehicles": vehicles_out,
                        "priorities_summary": priorities_summary,
                        "lots_priority": lots_priority_out,
                        "candidate_bins_considered": len(candidate_bins),
                        "candidate_items_considered": int(candidate_items_count),
                        "filtered_locked_items": int(filtered_locked_count),
                        "active_locks": len(locked_plate_ids),
                        "suppressed_customer_units": int(suppressed_customers),
                        "full_lots_count": len(full_lots),
                        "remainder_lots_count": len(rem_lots),
                    }
                )

            payload: Dict[str, Any] = {
                "version": VEH_SEQ_VERSION,
                "cached": False,
                "generated_at_utc": now.isoformat(),
                "cache": {
                    "inventory_hash": inventory_hash,
                    "locks_fingerprint": locks_fp,
                    "ttl_sec": cache_ttl_sec,
                },
                "params": {
                    "max_stops_per_vehicle": max_stops_per_vehicle,
                    "pickable_only": pickable_only,
                    "max_rehandles": max_rehandles,
                    "max_bins_considered": max_bins_considered,
                    "max_items_per_bin": max_items_per_bin,
                    "customer": customer_filter if customer_filter else None,
                    "type_group": type_group_filter if type_group_filter else None,
                    "diversify_customers": diversify_customers,
                    "cache_ttl_sec": cache_ttl_sec,
                },
                "filters": {
                    "aligned_to_dispatch_suggestions_citywise": True,
                    "routing_removed": True,
                    "lock_exclusion": True,
                    "customer_assigned_to_closest_lp": True,
                    "remainder_lots_used_for_next_vehicles": True,
                },
                "locks": {"count": len(locked_plate_ids), "ttl_default_min": _LOCKS_TTL_MIN_DEFAULT},
                "loading_points": results,
            }

            if debug:
                payload["_debug"] = {
                    "raw_type": str(type(raw_entries)),
                    "normalized_count": len(entries),
                    "bins_with_stack": len(bins_to_stack),
                    "layout_bins_count": len(layout_bins),
                    "layout_map_count": len(layout_map),
                    "customer_filter_norm": customer_filter_norm,
                    "type_group_filter": type_group_filter,
                    "diversify_customers": diversify_customers,
                    "note": "Vehicles now filled from FULL lots first, then REMAINDER lots for next vehicles (fixes CTL showing EMPTY).",
                }

            if cache_ttl_sec > 0:
                _cache_put(ck, payload, inventory_hash=inventory_hash, locks_fp=locks_fp, ttl_sec=cache_ttl_sec)

            return jsonify(payload)

        except Exception as e:
            _log(f"ERROR in /api/vehicle_sequencing: {e}")
            _log(traceback.format_exc())
            if debug:
                return jsonify({"version": VEH_SEQ_VERSION, "error": str(e), "trace": traceback.format_exc()}), 500
            raise


# -----------------------------------------------------------------------------
# Items -> Stops list (NO ROUTING)
# -----------------------------------------------------------------------------

def _items_to_stops_list(
    items: List[Dict[str, Any]],
    *,
    layout_map: Dict[str, Dict[str, Any]],
    anchor_xy: Optional[Tuple[float, float]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for it in items or []:
        b = str(it.get("bin") or "").strip().upper().replace(" ", "")
        pid = str(it.get("plate_id") or it.get("item_id") or "").strip()
        if not b or not pid:
            continue

        dist = None
        if anchor_xy is not None:
            bm = layout_map.get(b) or {}
            cx, cy = bm.get("cx"), bm.get("cy")
            if cx is not None and cy is not None:
                dist = _euclid(anchor_xy[0], anchor_xy[1], float(cx), float(cy))

        out.append(
            {
                "bin_id": b,
                "bay": _bay_of_bin(b),
                "plate_id": pid,
                "material_type": str(it.get("type") or ""),
                "type_group": str(it.get("type_group") or _type_group_from_row(it.get("type"))),
                "weight": _as_float(it.get("weight"), 0.0, 0.0, 1e12),
                "customer": str(it.get("customer") or ""),
                "customer_city": str(it.get("CustomerCity") or ""),
                "FG_text": str(it.get("status") or ""),
                "FI_Rel_text": str(it.get("FI_Rel_text") or ""),
                "seq": _as_int(it.get("seq"), 0, -10_000, 10_000),
                "rehandles": _as_int(it.get("rehandles"), 0, 0, 9999),
                "pickable": bool(it.get("pickable", True)),
                "distance": round(float(dist or 0.0), 3) if dist is not None else 0.0,
                "score": 0.0,
                "updated_at": None,
            }
        )
    return out


# -----------------------------------------------------------------------------
# Normalization + misc helpers
# -----------------------------------------------------------------------------

def _normalize_entries(raw: Any) -> List[Dict[str, Any]]:
    def flatten(obj: Any) -> List[Dict[str, Any]]:
        if obj is None:
            return []

        if isinstance(obj, dict):
            for k in ("inventory", "bins", "data", "rows", "items"):
                if k in obj and obj[k] is not None:
                    return flatten(obj[k])

            if any(isinstance(v, list) for v in obj.values()):
                out: List[Dict[str, Any]] = []
                for bin_name, rows in obj.items():
                    if not isinstance(rows, list):
                        continue
                    for r in rows:
                        if not isinstance(r, dict):
                            continue
                        rr = dict(r)
                        rr.setdefault("bin", str(bin_name))
                        rr.setdefault("bin_id", str(bin_name))
                        out.append(rr)
                return out

            if any(k in obj for k in ("bin", "bin_id", "id", "plate_id", "material_id", "FI_Rel_text", "status")):
                rr = dict(obj)
                if "bin" in rr and "bin_id" not in rr:
                    rr["bin_id"] = rr["bin"]
                return [rr]

            out2: List[Dict[str, Any]] = []
            for mk, mv in obj.items():
                if isinstance(mv, dict):
                    rr = dict(mv)
                    rr.setdefault("plate_id", str(mk))
                    out2.append(rr)
            return out2

        if isinstance(obj, list):
            out3: List[Dict[str, Any]] = []
            for r in obj:
                if isinstance(r, dict):
                    out3.append(dict(r))
                elif isinstance(r, list):
                    out3.extend(flatten(r))
            return out3

        return []

    rows = flatten(raw)

    clean: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        rr = dict(r)
        if "bin" not in rr and "bin_id" in rr:
            rr["bin"] = rr.get("bin_id")
        if "bin_id" not in rr and "bin" in rr:
            rr["bin_id"] = rr.get("bin")
        clean.append(rr)

    return clean


def _norm(s: Any) -> str:
    if s is None:
        return ""
    return str(s).strip().lower().replace("\u00a0", " ")


def _pick_customer_name(row: Dict[str, Any]) -> str:
    v = _pick_first(row, ["customer", "CustomerName", "Customer", "customer_name", "cust_name"])
    return str(v or "").strip()


def _pick_customer_city(row: Dict[str, Any]) -> str:
    v = _pick_first(row, ["CustomerCity", "customer_city", "City", "city"])
    return str(v or "").strip()


def _pick_first(d: Dict[str, Any], keys: List[str]) -> Any:
    for k in keys:
        if k in d and d.get(k) not in (None, ""):
            return d.get(k)
    return None


def _as_int(v: Any, default: int, lo: int = -(10**9), hi: int = 10**9) -> int:
    try:
        x = int(float(str(v).strip()))
        return max(lo, min(hi, x))
    except Exception:
        return default


def _as_float(v: Any, default: float, lo: float, hi: float) -> float:
    try:
        x = float(str(v).strip())
        if math.isnan(x) or math.isinf(x):
            return default
        return max(lo, min(hi, x))
    except Exception:
        return default


def _as_bool01(v: Any, default: bool) -> bool:
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


def _try_parse_dt(s: str) -> Optional[datetime]:
    try:
        s = s.strip()
        if not s:
            return None
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _compute_anchor_xy(anchor_bins: List[str], layout_map: Dict[str, Dict[str, Any]]) -> Optional[Tuple[float, float]]:
    pts: List[Tuple[float, float]] = []
    for bid in anchor_bins:
        b = layout_map.get(str(bid).strip())
        if not b:
            continue
        cx, cy = b.get("cx"), b.get("cy")
        if cx is None or cy is None:
            continue
        pts.append((float(cx), float(cy)))
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _euclid(x1: float, y1: float, x2: float, y2: float) -> float:
    return math.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2)


def _type_group_from_row(material_type: Any) -> str:
    t = _norm(material_type)
    return "COIL" if ("coil" in t) else "PLATE"


# -----------------------------------------------------------------------------
# Bay helpers
# -----------------------------------------------------------------------------

def _bay_of_bin(bin_id: Any) -> str:
    b = str(bin_id or "").strip().upper()
    if not b:
        return ""
    if b.startswith("CTL"):
        if b.startswith("CTLCD"):
            return "CTLCD"
        if b.startswith("CTLDE"):
            return "CTLDE"
        return "CTL"
    return b[:2]


def _allowed_bays_from_prefixes(prefixes: Optional[List[str]]) -> List[str]:
    if not prefixes:
        return []
    out = []
    for p in prefixes:
        p2 = str(p).strip().upper()
        if not p2:
            continue
        out.append(p2[:2])
    return sorted(list(set(out)))


# -----------------------------------------------------------------------------
# Vehicle Priority (NEW)
# -----------------------------------------------------------------------------

_TRUCK_CAPACITY_OPTIONS = (32.0, 36.0, 40.0)


def _parse_bin_prefix_number(bin_id: Any) -> Tuple[str, Optional[int]]:
    """
    Extract (prefix, number) from bin IDs like EF37D, DE34C, EF45A.
    Returns ("CTLDE"/"CTLCD"/"CTL", None) for CTL-style bins that don't follow the 2-letter+number format.
    """
    b = str(bin_id or "").strip().upper().replace(" ", "")
    if not b:
        return ("", None)

    if b.startswith("CTLDE"):
        return ("CTLDE", None)
    if b.startswith("CTLCD"):
        return ("CTLCD", None)
    if b.startswith("CTL"):
        return ("CTL", None)

    m = re.match(r"^([A-Z]{2})(\d{1,3})", b)
    if not m:
        return (b[:2], None)
    return (m.group(1), int(m.group(2)))


def _lp_id_for_bin(bin_id: str) -> Optional[str]:
    """
    Determine which configured LP range a bin belongs to for priority calculations.
    This does NOT affect eligibility; it is only used to label vehicle priority.
    """
    pfx, num = _parse_bin_prefix_number(bin_id)

    # EF families (existing)
    if pfx == "EF" and num is not None:
        if 34 <= num <= 40:
            return "lp1"
        if 45 <= num <= 54:
            return "lp2"
        if 55 <= num <= 67:
            return "lp3"
        return None

    # DE34-39 existing
    if pfx == "DE" and num is not None:
        if 34 <= num <= 39:
            return "lp4"

        # ✅ NEW: Trailer LP7 belongs to DE66-67 family (for priority labels)
        if 66 <= num <= 67:
            return "lp7"
        return None

    # ✅ NEW: Trailer LP9 belongs to AC66-67 family (for priority labels)
    if pfx == "AC" and num is not None:
        if 66 <= num <= 67:
            return "lp9"
        return None

    if pfx == "CTLDE":
        return "lp11"
    if pfx == "CTLCD":
        return "lp12"

    return None


def _lp_family_id(lp_id: str) -> str:
    """
    Normalize LP IDs for range families that have multiple named points.
    """
    lp_id = str(lp_id or "").strip()

    # Existing EF family mapping
    if lp_id in ("lp1",):
        return "ef_34_40"
    if lp_id in ("lp2",):
        return "ef_45_54"
    if lp_id == "lp3":
        return "ef_55_67"
    if lp_id == "lp4":
        return "de_34_39"
    if lp_id == "lp11":
        return "ctlde"
    if lp_id == "lp12":
        return "ctlcd"

    # ✅ FIXED: Trailer LP7 and LP9 are NOT EF families.
    if lp_id == "lp7":
        return "de_66_67"
    if lp_id == "lp9":
        return "ac_66_67"

    return lp_id


def _bin_in_lp_family(bin_id: str, lp_family: str) -> bool:
    """Return True if bin_id belongs to the given LP family range."""
    b = str(bin_id or "").strip().upper().replace(" ", "")
    fam = str(lp_family or "").strip()

    pfx, num = _parse_bin_prefix_number(b)

    if fam == "ef_34_40":
        return (pfx == "EF" and num is not None and 34 <= num <= 40)
    if fam == "ef_45_54":
        return (pfx == "EF" and num is not None and 45 <= num <= 54)
    if fam == "ef_55_67":
        return (pfx == "EF" and num is not None and 55 <= num <= 67)
    if fam == "de_34_39":
        return (pfx == "DE" and num is not None and 34 <= num <= 39)
    if fam == "ctlde":
        return pfx == "CTLDE"
    if fam == "ctlcd":
        return pfx == "CTLCD"

    # ✅ NEW families
    if fam == "de_66_67":
        return (pfx == "DE" and num is not None and 66 <= num <= 67)
    if fam == "ac_66_67":
        return (pfx == "AC" and num is not None and 66 <= num <= 67)

    return True


def _infer_truck_capacity_tons(*, total_weight: float, target_tons: Any) -> Optional[float]:
    try:
        tt = float(target_tons) if target_tons is not None else None
    except Exception:
        tt = None

    if tt is not None:
        best = min(_TRUCK_CAPACITY_OPTIONS, key=lambda c: abs(c - tt))
        if abs(best - tt) <= 1.0:
            return float(best)

    w = float(total_weight or 0.0)
    for cap in _TRUCK_CAPACITY_OPTIONS:
        if w <= cap + 0.75:
            return float(cap)
    return float(_TRUCK_CAPACITY_OPTIONS[-1])


def _is_capacity_ready(*, total_weight: float, truck_capacity_tons: Optional[float]) -> bool:
    if truck_capacity_tons is None:
        return False
    w = float(total_weight or 0.0)
    cap = float(truck_capacity_tons)
    return (cap in _TRUCK_CAPACITY_OPTIONS) and (w >= cap - 0.75)


def _compute_vehicle_priority(
    *,
    loading_point_id: str,
    vehicle_customer: str,
    vehicle_bins: List[str],
    truck_capacity_tons: Optional[float],
    total_weight: float,
) -> Optional[int]:
    if not vehicle_bins:
        return None
    if truck_capacity_tons is None or float(truck_capacity_tons) not in _TRUCK_CAPACITY_OPTIONS:
        return None

    lp_family = _lp_family_id(loading_point_id)

    bin_lp_families: List[str] = []
    for b in vehicle_bins:
        lp_id = _lp_id_for_bin(b)
        if not lp_id:
            continue
        bin_lp_families.append(_lp_family_id(lp_id))

    unique_bins = list(dict.fromkeys(vehicle_bins))
    unique_lp_families = sorted(list(set(bin_lp_families)))

    if unique_lp_families and all(x == lp_family for x in unique_lp_families):
        if len(unique_bins) == 1:
            return 1
        return 2

    if vehicle_customer:
        if len(unique_bins) >= 2 and len(unique_bins) <= 3 and len(unique_lp_families) >= 2 and len(unique_lp_families) <= 3:
            return 3

    return None


# -----------------------------------------------------------------------------
# Loading point configuration
# -----------------------------------------------------------------------------


def _loading_point_config() -> List[Dict[str, Any]]:
    """
    Loading point ranges updated per Feb-2026 change request.

    Ranges (inclusive):
    - Loading Point 1 : EF34 .. EF40
    - Loading Point 2 : EF45 .. EF54
    - Loading Point 3 : EF55 .. EF67
    - Loading Point 4 : DE34 .. DE39

    ✅ FIXED (requested):
    - Trailer Loading Point 7 : DE66 .. DE67 (Anchor bins must be DE66D, DE67D)
    - Trailer Loading Point 9 : AC66 .. AC67 (Anchor bins must be AC66E/F + AC67E/F)

    - Loading Point 11: CTLDE
    - Loading Point 12: CTLCD
    """
    return [
        # EF Bay
        {
            "id": "lp1",
            "name": "Loading Point 1",
            "vehicle_count": 1,
            "anchor_bins": ["EF37C", "EF37D"],
            "allowed_prefixes": ["EF"],
            "allowed_bays": ["EF"],
        },
        {
            "id": "lp2",
            "name": "Loading Point 2",
            "vehicle_count": 2,
            "anchor_bins": ["EF49C", "EF49D", "EF50C", "EF50D"],
            "allowed_prefixes": ["EF"],
            "allowed_bays": ["EF"],
        },
        {
            "id": "lp3",
            "name": "Loading Point 3",
            "vehicle_count": 2,
            "anchor_bins": ["EF61C", "EF61D"],
            "allowed_prefixes": ["EF"],
            "allowed_bays": ["EF"],
        },

        # DE Bay
        {
            "id": "lp4",
            "name": "Loading Point 4",
            "vehicle_count": 1,
            "anchor_bins": ["DE36C", "DE36D"],
            "allowed_prefixes": ["DE"],
            "allowed_bays": ["DE"],
        },

        # ✅ FIXED: Trailer LP7 (DE 66–67D)
        {
            "id": "lp7",
            "name": "Loading Point 7",
            "vehicle_count": 1,
            "anchor_bins": ["DE66D", "DE67D"],
            "allowed_prefixes": ["DE"],
            "allowed_bays": ["DE"],
        },

        # ✅ FIXED: Trailer LP9 (AC 66–67 E/F)
        {
            "id": "lp9",
            "name": "Loading Point 9",
            "vehicle_count": 1,
            "anchor_bins": ["AC66E", "AC66F", "AC67E", "AC67F"],
            "allowed_prefixes": ["AC"],
            "allowed_bays": ["AC"],
        },

        # Renamed CTL points
        {
            "id": "lp11",
            "name": "Loading Point 11 (CTLDE)",
            "vehicle_count": 1,
            "anchor_bins": [
                "CTLDE14A", "CTLDE14B", "CTLDE14C",
                "CTLDE15B", "CTLDE15C",
                "CTLDE16B", "CTLDE16C", "CTLDE16D",
                "CTLDE17B", "CTLDE17C", "CTLDE17D", "CTLDE17E",
            ],
            "allowed_prefixes": ["CTLDE"],
            "allowed_bays": ["CTLDE"],
        },
        {
            "id": "lp12",
            "name": "Loading Point 12 (CTLCD)",
            "vehicle_count": 1,
            "anchor_bins": [
                "CTLCD14A", "CTLCD14B", "CTLCD14C",
                "CTLCD15B", "CTLCD15C",
                "CTLCD16B", "CTLCD16C", "CTLCD16D",
                "CTLCD17B", "CTLCD17C", "CTLCD17D", "CTLCD17E",
            ],
            "allowed_prefixes": ["CTLCD"],
            "allowed_bays": ["CTLCD"],
        },
    ]


def _expand_bins(prefix: str, rows: List[str], col_start: int, col_end: int) -> List[str]:
    out: List[str] = []
    for c in range(col_start, col_end + 1):
        for r in rows:
            out.append(f"{prefix}{c}{r}")
    return out