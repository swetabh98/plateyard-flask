# sheets.py
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from flask import Blueprint, current_app, jsonify

# ---- internal state (module-level, safe for single-process) -----------------
_bp: Optional[Blueprint] = None
_import_fn: Optional[Callable[[], int]] = None

_interval_seconds: int = 900  # default 15 minutes
_stop_evt = threading.Event()
_runner_thread: Optional[threading.Thread] = None
_oneoff_lock = threading.Lock()

_status = {
    "running": False,
    "runs": 0,
    "failures": 0,
    "last_run": None,      # ISO8601 Z
    "last_end": None,      # ISO8601 Z
    "last_success": None,  # ISO8601 Z
    "last_error": None,    # str
    "last_count": None,    # int (items in inventory after import)
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run_once(app) -> None:
    """Run the provided import function exactly once, updating status."""
    global _status
    if _import_fn is None:
        return
    _status["running"] = True
    _status["last_run"] = _utc_now_iso()
    _status["last_error"] = None
    try:
        with app.app_context():
            count = _import_fn()
        _status["last_count"] = int(count) if count is not None else None
        _status["runs"] += 1
        _status["last_success"] = _utc_now_iso()
    except Exception as e:
        _status["failures"] += 1
        _status["last_error"] = f"{type(e).__name__}: {e}"
        # best-effort logging via Flask logger (if available)
        try:
            app.logger.exception("Google Sheets import failed")
        except Exception:
            pass
    finally:
        _status["last_end"] = _utc_now_iso()
        _status["running"] = False


def _runner_loop(app):
    """Background loop: run immediately once, then every interval until stopped."""
    # First immediate run happens only if you want it; comment out next line to skip
    # (kept enabled because your app already does an initial import in _app_init_once)
    # _run_once(app)

    while not _stop_evt.is_set():
        # Wait for the interval with cooperative stop
        if _stop_evt.wait(_interval_seconds):
            break
        _run_once(app)


def start_sheets_scheduler(app, import_fn: Callable[[], int], interval_seconds: int = 900) -> None:
    """
    Start background scheduler that calls `import_fn()` every `interval_seconds`.
    Safe to call multiple times – it will only start once.
    """
    global _runner_thread, _import_fn, _interval_seconds
    if _runner_thread and _runner_thread.is_alive():
        return
    _import_fn = import_fn
    _interval_seconds = max(60, int(interval_seconds))  # hard floor: 60s
    _stop_evt.clear()
    _runner_thread = threading.Thread(
        target=_runner_loop, args=(app,), name="sheets-scheduler", daemon=True
    )
    _runner_thread.start()
    try:
        app.logger.info("Sheets scheduler started (every %ss).", _interval_seconds)
    except Exception:
        pass


def stop_sheets_scheduler() -> None:
    """Optional – stop the background scheduler."""
    _stop_evt.set()


def register_sheets_api(app, import_fn: Callable[[], int]) -> None:
    """
    Register two endpoints:
      GET  /api/sheets/status      -> scheduler & last-run status
      POST /api/sheets/import-now  -> trigger an on-demand import (non-blocking)
    """
    global _bp, _import_fn
    _import_fn = import_fn

    if _bp is not None:
        # already registered
        return

    _bp = Blueprint("sheets", __name__)

    @_bp.get("/api/sheets/status")
    def sheets_status():
        return jsonify({
            "interval_seconds": _interval_seconds,
            **_status,
        })

    @_bp.post("/api/sheets/import-now")
    def sheets_import_now():
        # Kick off an immediate one-off run in a short-lived thread.
        if _status["running"]:
            return jsonify({"ok": False, "running": True, "message": "Importer already running"}), 409

        def _kick():
            # shield against accidental concurrent kicks
            with _oneoff_lock:
                _run_once(current_app._get_current_object())

        t = threading.Thread(target=_kick, name="sheets-import-now", daemon=True)
        t.start()
        return jsonify({"ok": True, "started": True, "at": _utc_now_iso()})

    app.register_blueprint(_bp)
