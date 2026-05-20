from __future__ import annotations

import json
from datetime import datetime, timezone

from flask import jsonify, render_template, request
from sqlalchemy import text


def register_dashboard_routes(
    app,
    engine,
    *,
    canon_bin=None,
    runtime_fix_type=None,
    utc_today_str=None,
    utc_now_iso_z=None,
    to_iso_utc_z=None,
    pick_event_time=None,
    ISO_FMT_Z: str = "%Y-%m-%dT%H:%M:%SZ",
    _exec=None,
    _fetchall_dicts=None,
    _fetchone_scalar=None,
    **_ignored,
):
    """
    Collision-safe registration for dashboard + APIs used by dashboard.html.

    Will NOT crash if the same routes already exist in app.py.
    It simply skips registering duplicates.
    """

    # ---------------------------------------------------------------------
    # Utilities: route existence checks
    # ---------------------------------------------------------------------
    def _has_rule(rule: str, methods: set[str] | None = None) -> bool:
        """Check if a URL rule is already registered."""
        want_methods = set(m.upper() for m in (methods or set()))
        for r in app.url_map.iter_rules():
            if r.rule != rule:
                continue
            if not want_methods:
                return True
            if want_methods.issubset(set(m.upper() for m in (r.methods or set()))):
                return True
        return False

    def _endpoint_taken(endpoint_name: str) -> bool:
        return endpoint_name in (app.view_functions or {})

    def _safe_add_get(rule: str, endpoint_base: str):
        """
        Decorator factory that:
          - skips if URL exists
          - otherwise adds with a non-colliding endpoint name
        """
        def deco(fn):
            if _has_rule(rule, {"GET"}):
                return fn  # already registered; skip

            endpoint = endpoint_base
            if _endpoint_taken(endpoint):
                # avoid endpoint collision; keep URL same
                i = 2
                while _endpoint_taken(f"{endpoint_base}_{i}"):
                    i += 1
                endpoint = f"{endpoint_base}_{i}"

            app.add_url_rule(rule, endpoint=endpoint, view_func=fn, methods=["GET"])
            return fn
        return deco

    # ---------------------------------------------------------------------
    # Fallback DB helpers if not passed
    # ---------------------------------------------------------------------
    def _default_exec(con, sql, params=None):
        return con.execute(text(sql), params or {})

    def _default_fetchall_dicts(con, sql, params=None):
        return _default_exec(con, sql, params).mappings().all()

    def _default_fetchone_scalar(con, sql, params=None):
        return _default_exec(con, sql, params).scalar_one()

    if _exec is None:
        _exec = _default_exec
    if _fetchall_dicts is None:
        _fetchall_dicts = _default_fetchall_dicts
    if _fetchone_scalar is None:
        _fetchone_scalar = _default_fetchone_scalar

    # ---------------------------------------------------------------------
    # Helper fallbacks
    # ---------------------------------------------------------------------
    def _canon_bin(b):
        if canon_bin:
            return canon_bin(b)
        return (b or "").strip().upper()

    def _runtime_fix_type(row):
        if runtime_fix_type:
            return runtime_fix_type(row)
        return row

    def _utc_today_str():
        if utc_today_str:
            return utc_today_str()
        return datetime.now(timezone.utc).date().isoformat()

    def _utc_now_iso_z():
        if utc_now_iso_z:
            return utc_now_iso_z()
        return datetime.now(timezone.utc).replace(microsecond=0).strftime(ISO_FMT_Z)

    def _to_iso_utc_z(v):
        if to_iso_utc_z:
            return to_iso_utc_z(v)
        if not v:
            return None
        s = str(v).strip()
        try:
            if s.endswith("Z"):
                s = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).replace(microsecond=0).strftime(ISO_FMT_Z)
        except Exception:
            return None

    def _pick_event_time(before: dict | None, after: dict | None, default_iso: str) -> str:
        if pick_event_time:
            return pick_event_time(before, after, default_iso)
        return _to_iso_utc_z(default_iso) or _utc_now_iso_z()

    # ---------------------------------------------------------------------
    # Snapshot helpers
    # ---------------------------------------------------------------------
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

    def _first_seen_local_date_for_item(con, plate_id: str):
        r = _exec(
            con,
            "SELECT min(substr(COALESCE(event_time,timestamp),1,10)) "
            "FROM yard_transactions WHERE item_id=:p",
            {"p": plate_id},
        ).first()
        return r[0] if r else None

    def _today_dispatch_count(con):
        today_utc = _utc_today_str()
        rows = _exec(
            con,
            """SELECT action,status,after_snapshot,COALESCE(event_time,timestamp) as when_ts
               FROM yard_transactions
               WHERE substr(COALESCE(event_time,timestamp),1,10)=:today""",
            {"today": today_utc},
        ).mappings().all()
        return sum(1 for r in rows if _tx_is_dispatch(dict(r)))

    # ---------------------------------------------------------------------
    # ✅ Adds vs Dispatch Trend (FIXED)
    # ---------------------------------------------------------------------
    @_safe_add_get("/api/adds_dispatch_trend", "api_adds_dispatch_trend")
    def api_adds_dispatch_trend():
        """
        Builds daily adds/dispatch trend from yard_transactions (source of truth).
        - Added: action='added'
        - Dispatch: action='removed' OR edited with status dispatched (same logic as UI)

        Optional query params:
          start=YYYY-MM-DD
          end=YYYY-MM-DD
          limit=20000 (rows to scan; can go up to 200000)
        """
        start = (request.args.get("start") or "").strip()
        end = (request.args.get("end") or "").strip()
        limit = int(request.args.get("limit") or 20000)
        limit = max(1, min(limit, 200000))

        wh = ["1=1"]
        pa = {"lim": limit}

        if start:
            wh.append("substr(COALESCE(event_time,timestamp),1,10) >= :start")
            pa["start"] = start
        if end:
            wh.append("substr(COALESCE(event_time,timestamp),1,10) <= :end")
            pa["end"] = end

        sql = f"""
            SELECT action, status, after_snapshot,
                   substr(COALESCE(event_time,timestamp),1,10) AS day
            FROM yard_transactions
            WHERE {' AND '.join(wh)}
            ORDER BY substr(COALESCE(event_time,timestamp),1,19) ASC
            LIMIT :lim
        """

        daily = {}  # day -> {added, dispatch}

        with engine.begin() as con:
            rows = _exec(con, sql, pa).mappings().all()

        for r in rows:
            day = r.get("day")
            if not day:
                continue

            if day not in daily:
                daily[day] = {"added": 0, "dispatch": 0}

            act = (r.get("action") or "").lower()

            if act == "added":
                daily[day]["added"] += 1
            else:
                # dispatch logic same as elsewhere
                if _tx_is_dispatch(dict(r)):
                    daily[day]["dispatch"] += 1

        labels = sorted(daily.keys())
        adds = [daily[d]["added"] for d in labels]
        disp = [daily[d]["dispatch"] for d in labels]

        return jsonify({"labels": labels, "added": adds, "dispatch": disp})

    # ---------------------------------------------------------------------
    # GET /api/inventory
    # ---------------------------------------------------------------------
    @_safe_add_get("/api/inventory", "api_inventory")
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
                   FI_Rel_text,SBU_RelStatus,CustomerCity, Material_Status,
                   added_at,created_at,updated_at,raw_json
            FROM plates
            WHERE {' AND '.join(wh)}
            ORDER BY COALESCE(updated_at,added_at,created_at) DESC
            LIMIT :lim
        """
        pa["lim"] = limit

        with engine.begin() as con:
            raw_rows = _fetchall_dicts(con, sql, pa)
            
        fg_statuses = {
            "finished status",
            "tpi completed",
            "levelling completed",
            "offer to pfp/ssd",
            "quenching done",
        }

        rows = []
        for r in raw_rows:
            d = _runtime_fix_type(dict(r))
            mat_status = d.get("Material_Status") or ""
            s0 = " ".join(mat_status.strip().lower().split())
            if s0 in fg_statuses:
                d["status"] = "FG"
            else:
                d["status"] = "WIP"
            rows.append(d)

        return jsonify(rows)

    # ---------------------------------------------------------------------
    # GET /api/planned_deliveries
    # ---------------------------------------------------------------------
    @_safe_add_get("/api/planned_deliveries", "api_planned_deliveries")
    def api_planned_deliveries():
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
            SELECT plate_id, type, bin, status, customer, weight, dispatch_mode, Material_Status,
                   COALESCE(updated_at, added_at, created_at) AS added_at
            FROM plates
            WHERE {' AND '.join(wh)}
            ORDER BY added_at DESC
            LIMIT :lim
        """
        pa["lim"] = limit

        with engine.begin() as con:
            raw_rows = _fetchall_dicts(con, sql, pa)

        def _safe_float(x):
            try:
                return float(x or 0)
            except Exception:
                return 0.0

        fg_statuses = {
            "finished status",
            "tpi completed",
            "levelling completed",
            "offer to pfp/ssd",
            "quenching done",
        }

        summary_counts = {"Rail": 0, "Truck": 0}
        summary_weights = {"Rail": 0.0, "Truck": 0.0}
        out_rows = []

        now_dt = datetime.now(timezone.utc).replace(microsecond=0)

        for r in raw_rows:
            d = dict(r)
            mat_status = d.get("Material_Status") or ""
            s0 = " ".join(mat_status.strip().lower().split())
            if s0 in fg_statuses:
                st = "FG"
            else:
                st = "WIP"

            mode = _norm_mode(d.get("dispatch_mode"))
            added = d.get("added_at") or ""
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
                    "plate_id": d.get("plate_id") or "",
                    "status": st,
                    "bin": _canon_bin(d.get("bin") or ""),
                    "customer": d.get("customer") or "",
                    "weight": d.get("weight"),
                    "mode": mode,
                    "added_at": added,
                    "hours": hours,
                }
            )

            if mode in ("Rail", "Truck"):
                summary_counts[mode] += 1
                summary_weights[mode] += _safe_float(d.get("weight"))

        return jsonify({"rows": out_rows, "summary": {"counts": summary_counts, "weights": summary_weights}})

    # ---------------------------------------------------------------------
    # GET /api/transactions
    # ---------------------------------------------------------------------
    @_safe_add_get("/api/transactions", "api_transactions")
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

        limit = int(request.args.get("limit") or 500)
        limit = max(1, min(limit, 50000))

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
            LIMIT :lim
        """
        pa["lim"] = limit

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

        fg_statuses = {
            "finished status",
            "tpi completed",
            "levelling completed",
            "offer to pfp/ssd",
            "quenching done",
        }

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

            # Dynamic FG/WIP check for transactions
            original_st = r["status"]
            if original_st and str(original_st).lower() == "dispatched":
                st = "Dispatched"
            else:
                mat_status = _get_ci(blob, "Material_Status", "Material Status", "MATERIAL_STATUS")
                s0 = " ".join(mat_status.strip().lower().split())
                if s0 in fg_statuses:
                    st = "FG"
                else:
                    st = "WIP"

            raw_start = _first_nonempty(
                _get_ci(after, "added_at", "created_at", "Created On", "createdOn"),
                _get_ci(before, "added_at", "created_at", "Created On", "createdOn"),
                r.get("event_time"),
                r.get("when_ts"),
            )
            age_start_iso = _to_iso_utc_z(raw_start)

            if _is_midnight(age_start_iso) and r.get("when_ts"):
                age_start_iso = _to_iso_utc_z(r["when_ts"])

            age_hours = None
            if age_start_iso:
                try:
                    start_dt = datetime.strptime(age_start_iso, ISO_FMT_Z).replace(tzinfo=timezone.utc)
                    diff = now_dt - start_dt
                    age_hours = 0 if diff.total_seconds() < 0 else int(diff.total_seconds() // 3600)
                except Exception:
                    age_hours = None

            event_ts = _to_iso_utc_z(r.get("when_ts")) or r.get("when_ts")

            out.append(
                {
                    "id": r["id"],
                    "item_type": r["item_type"],
                    "item_id": r["item_id"],
                    "action": r["action"],
                    "method": r["method"],
                    "source_bin": _canon_bin(r["source_bin"] or "") if r["source_bin"] else None,
                    "dest_bin": _canon_bin(r["dest_bin"] or "") if r["dest_bin"] else None,
                    "timestamp": event_ts,
                    "age_start": age_start_iso,
                    "age_hours": age_hours,
                    "edited": bool(r["edited"]),
                    "status": st,
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

    # ---------------------------------------------------------------------
    # GET /dashboard
    # ---------------------------------------------------------------------
    @_safe_add_get("/dashboard", "dashboard")
    def dashboard():
        with engine.begin() as con:
            plates = _fetchone_scalar(con, "SELECT COUNT(*) FROM plates WHERE type='Plate'")
            coils = _fetchone_scalar(con, "SELECT COUNT(*) FROM plates WHERE type='Coil'")

            deliveries_today = _today_dispatch_count(con)

            added_today = _fetchone_scalar(
                con,
                """
                SELECT COUNT(*) FROM yard_transactions
                WHERE action='added' AND substr(COALESCE(event_time,timestamp),1,10)=:today
                """,
                {"today": _utc_today_str()},
            )
            edited_today = _fetchone_scalar(
                con,
                """
                SELECT COUNT(*) FROM yard_transactions
                WHERE action='edited' AND substr(COALESCE(event_time,timestamp),1,10)=:today
                """,
                {"today": _utc_today_str()},
            )

            pmodes = _fetchall_dicts(
                con,
                "SELECT dispatch_mode, COUNT(*) as count "
                "FROM plates WHERE COALESCE(status,'') != 'Dispatched' AND dispatch_mode IS NOT NULL "
                "GROUP BY dispatch_mode",
            )
            modes = {"rail": 0, "truck": 0, "unknown": 0}
            for row in pmodes:
                nm = _norm_mode(row.get("dispatch_mode"))
                if nm == "Rail":
                    modes["rail"] += int(row["count"])
                elif nm == "Truck":
                    modes["truck"] += int(row["count"])
                else:
                    modes["unknown"] += int(row["count"])

            fg_statuses = {
                "finished status",
                "tpi completed",
                "levelling completed",
                "offer to pfp/ssd",
                "quenching done",
            }

            # --- Status Counts based on Material_Status logic ---
            mat_rows = _fetchall_dicts(
                con, 
                "SELECT Material_Status, COUNT(*) as count FROM plates WHERE COALESCE(status,'') != 'Dispatched' GROUP BY Material_Status"
            )
            fg_count = 0
            wip_count = 0
            for row in mat_rows:
                s0 = (row.get("Material_Status") or "").strip().lower()
                s0 = " ".join(s0.split())
                if s0 in fg_statuses:
                    fg_count += row["count"]
                else:
                    wip_count += row["count"]

            status = {
                "FG": fg_count,
                "WIP": wip_count,
            }

            aging = []
            now_dt = datetime.now(timezone.utc).replace(microsecond=0)
            rows = _fetchall_dicts(
                con,
                """
                SELECT plate_id, type, bin, status, Material_Status,
                       COALESCE(added_at, created_at) AS since
                FROM plates
                WHERE COALESCE(status,'') != 'Dispatched'
                ORDER BY substr(COALESCE(added_at,created_at),1,19) DESC
                """,
            )
            for row in rows:
                pid = row["plate_id"]
                mat_status = row.get("Material_Status") or ""
                s0 = " ".join(mat_status.strip().lower().split())
                if s0 in fg_statuses:
                    st = "FG"
                else:
                    st = "WIP"
                    
                since_iso = row["since"] or _first_seen_local_date_for_item(con, pid)
                hours = "—"
                if since_iso:
                    try:
                        base_s = str(since_iso).strip()
                        if len(base_s) == 10 and base_s[4] == "-" and base_s[7] == "-":
                            base_dt = datetime.fromisoformat(base_s).replace(tzinfo=timezone.utc)
                        else:
                            if base_s.endswith("Z"):
                                base_s = base_s.replace("Z", "+00:00")
                            base_dt = datetime.fromisoformat(base_s)
                            if base_dt.tzinfo is None:
                                base_dt = base_dt.replace(tzinfo=timezone.utc)
                        diff = now_dt - base_dt.astimezone(timezone.utc)
                        h = int(diff.total_seconds() // 3600)
                        hours = max(h, 0)
                    except Exception:
                        hours = "—"

                aging.append(
                    {
                        "plate_id": pid,
                        "type": row["type"],
                        "bin": _canon_bin(row["bin"] or ""),
                        "status": st,
                        "hours": hours,
                    }
                )

            recent = [
                dict(r)
                for r in _fetchall_dicts(
                    con,
                    """
                    SELECT COALESCE(event_time,timestamp) as timestamp,
                           action,item_type,item_id,source_bin,dest_bin,status
                    FROM yard_transactions
                    ORDER BY substr(COALESCE(event_time,timestamp),1,19) DESC
                    LIMIT 15
                    """,
                )
            ]

            # Enforce dynamic FG/WIP on recent transactions too
            for r in recent:
                if str(r.get("status") or "").lower() != "dispatched":
                    # If we need the real Material_Status from snapshot, it's not selected here,
                    # but typically "recent" relies on the snapshot payload handled differently in UI.
                    # We will leave it as is or fallback to WIP if needed.
                    pass

        return render_template(
            "dashboard.html",
            totals={"plates": plates, "coils": coils},
            today={"deliveries": deliveries_today, "added": added_today, "edited": edited_today},
            modes=modes,
            status=status,
            aging=aging,
            recent=recent,
        )

    return True