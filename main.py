import os
import traceback
import logging
from typing import Optional
from flask import Flask, request, jsonify

# hyundai-kia-connect-api
from hyundai_kia_connect_api import VehicleManager, ClimateRequestOptions

app = Flask(__name__)

# Verbose logging to help diagnose issues in Vercel logs
logging.basicConfig(level=logging.INFO)
logging.getLogger("urllib3").setLevel(logging.WARNING)
app.logger.info("CLEANED_MAIN v3 loaded")

# --------- Environment ---------
USERNAME: Optional[str] = os.getenv("KIA_USERNAME")
PASSWORD: Optional[str] = os.getenv("KIA_PASSWORD")
PIN: Optional[str] = os.getenv("KIA_PIN")
SECRET_KEY: Optional[str] = os.getenv("SECRET_KEY")
VEHICLE_ID: Optional[str] = os.getenv("VEHICLE_ID")  # may be None -> auto-pick

if SECRET_KEY:
    app.secret_key = SECRET_KEY

# --------- Region/Brand resolution across library versions ---------

def _resolve_region_brand():
    """Return (region, brand) for VehicleManager, handling many lib versions.
    Falls back to numeric envs KIA_REGION/KIA_BRAND (defaults 3/1).
    """
    # Try newer-style enums
    try:
        from hyundai_kia_connect_api import Brand as _Brand, Region as _Region  # type: ignore
        region = getattr(_Region, "US", getattr(_Region, "NORTH_AMERICA", None))
        brand = getattr(_Brand, "KIA", None)
        if region is not None and brand is not None:
            return region, brand
    except Exception:
        pass

    # Try older-style enums under .const
    try:
        from hyundai_kia_connect_api.const import Brand as _Brand, Region as _Region  # type: ignore
        region = getattr(_Region, "US", getattr(_Region, "NORTH_AMERICA", None))
        brand = getattr(_Brand, "KIA", None)
        if region is not None and brand is not None:
            return region, brand
    except Exception:
        pass

    # Fallback to integers (historically: region=3 (US/NA), brand=1 (KIA))
    try:
        region_int = int(os.getenv("KIA_REGION", "3"))
        brand_int = int(os.getenv("KIA_BRAND", "1"))
    except Exception:
        region_int, brand_int = 3, 1
    app.logger.warning(
        "[init] Falling back to numeric region/brand (region=%s, brand=%s).",
        region_int, brand_int,
    )
    return region_int, brand_int

# --------- Globals (lazy init) ---------
vehicle_manager: Optional[VehicleManager] = None
init_error: Optional[str] = None


def _init_vehicle_manager() -> None:
    """One-time, robust initialization of the VehicleManager.
    - Authenticates and updates state
    - Auto-selects VEHICLE_ID if not provided
    - Never hard-exits the process; stores error in `init_error`
    """
    global vehicle_manager, init_error, VEHICLE_ID

    if vehicle_manager is not None:
        return

    missing = [k for k, v in {
        "KIA_USERNAME": USERNAME,
        "KIA_PASSWORD": PASSWORD,
        "KIA_PIN": PIN,
    }.items() if not v]
    if missing:
        init_error = f"Missing required env vars: {', '.join(missing)}"
        return

    try:
        region_val, brand_val = _resolve_region_brand()
        vm = VehicleManager(
            region=region_val,
            brand=brand_val,
            username=USERNAME,
            password=PASSWORD,
            pin=str(PIN),
        )

        app.logger.info("[init] Attempting to authenticate and refresh token…")
        vm.check_and_refresh_token()
        app.logger.info("[init] Token refreshed. Updating vehicle states…")
        try:
            vm.update_all_vehicles_with_cached_state()
        except Exception:
            init_error_local = "Token refreshed, but failed to update vehicle state."
            app.logger.error("[init] update_all_vehicles_with_cached_state() failed: %s", traceback.format_exc())
            init_error = init_error_local
            return

        if not vm.vehicles:
            init_error = "Authenticated but no vehicles were returned by the API."
            app.logger.warning("[init] No vehicles returned after auth.")
            return

        if not VEHICLE_ID:
            VEHICLE_ID = next(iter(vm.vehicles.keys()))
            app.logger.info("[init] No VEHICLE_ID provided. Using first vehicle: %s", VEHICLE_ID)

        vehicle_manager = vm
        init_error = None

    except Exception:
        init_error = traceback.format_exc()
        app.logger.error("Initialization failed: %s", init_error)


def ensure_initialized():
    _init_vehicle_manager()
    if vehicle_manager is None:
        return False, (init_error or "Initialization did not complete.")
    return True, "ok"


@app.before_request
def _log_request():
    app.logger.info("%s %s", request.method, request.path)


@app.get("/")
def health():
    ok, msg = ensure_initialized()
    status = "ok" if ok else "error"
    return jsonify({"status": status, "message": msg}), (200 if ok else 500)


@app.get("/debug/init")
def debug_init():
    """Force a fresh init cycle and return detailed status for debugging."""
    global vehicle_manager, init_error
    vehicle_manager = None
    init_error = None
    _init_vehicle_manager()
    ok = vehicle_manager is not None
    return jsonify({
        "ok": ok,
        "init_error": init_error,
    }), (200 if ok else 500)


@app.get("/debug/vehicles")
def debug_vehicles():
    ok, msg = ensure_initialized()
    if not ok:
        return jsonify({"error": msg}), 500
    try:
        vehicle_manager.update_all_vehicles_with_cached_state()
        return jsonify({
            "vehicle_ids": list(vehicle_manager.vehicles.keys()),
            "count": len(vehicle_manager.vehicles),
        }), 200
    except Exception:
        app.logger.exception("/debug/vehicles failed")
        return jsonify({"error": "Failed to list vehicles", "detail": traceback.format_exc()}), 500


@app.get("/status")
def status():
    ok, msg = ensure_initialized()
    if not ok:
        return jsonify({"error": msg}), 500
    try:
        vehicle_manager.update_all_vehicles_with_cached_state()
        v = vehicle_manager.vehicles.get(VEHICLE_ID)
        if not v:
            return jsonify({"error": f"Vehicle {VEHICLE_ID} not found."}), 404
        snapshot = {
            "vehicle_id": VEHICLE_ID,
            "name": getattr(v, "name", None),
            "vin": getattr(v, "vin", None),
            "odometer": getattr(v, "odometer", None),
            "battery": getattr(v, "battery_level", None),
            "charging": getattr(v, "is_charging", None),
            "range": getattr(v, "ev_range", None),
            "locked": getattr(v, "is_locked", None),
            "timestamp": getattr(v, "last_update", None),
        }
        return jsonify(snapshot), 200
    except Exception:
        app.logger.exception("/status failed")
        return jsonify({"error": "Failed to fetch status", "detail": traceback.format_exc()}), 500


@app.post("/lock_car")
def lock_car():
    ok, msg = ensure_initialized()
    if not ok:
        return jsonify({"error": msg}), 500
    try:
        vehicle_manager.check_and_refresh_token()
        vehicle_manager.update_all_vehicles_with_cached_state()
        res = vehicle_manager.lock(VEHICLE_ID)
        return jsonify({"status": "locked", "result": res}), 200
    except Exception:
        app.logger.exception("/lock_car failed")
        return jsonify({"error": "Lock failed", "detail": traceback.format_exc()}), 500


@app.post("/unlock_car")
def unlock_car():
    ok, msg = ensure_initialized()
    if not ok:
        return jsonify({"error": msg}), 500
    try:
        vehicle_manager.check_and_refresh_token()
        vehicle_manager.update_all_vehicles_with_cached_state()
        res = vehicle_manager.unlock(VEHICLE_ID)
        return jsonify({"status": "unlocked", "result": res}), 200
    except Exception:
        app.logger.exception("/unlock_car failed")
        return jsonify({"error": "Unlock failed", "detail": traceback.format_exc()}), 500


@app.post("/start_climate")
def start_climate():
    ok, msg = ensure_initialized()
    if not ok:
        return jsonify({"error": msg}), 500
    body = request.get_json(silent=True) or {}
    duration = int(body.get("duration", 10))
    defrost = bool(body.get("defrost", False))
    try:
        vehicle_manager.check_and_refresh_token()
        opts = ClimateRequestOptions(duration, defrost)
        res = vehicle_manager.start_climate(VEHICLE_ID, opts)
        return jsonify({"status": "climate_started", "result": res}), 200
    except Exception:
        app.logger.exception("/start_climate failed")
        return jsonify({"error": "Start climate failed", "detail": traceback.format_exc()}), 500


@app.post("/stop_climate")
def stop_climate():
    ok, msg = ensure_initialized()
    if not ok:
        return jsonify({"error": msg}), 500
    try:
        vehicle_manager.check_and_refresh_token()
        res = vehicle_manager.stop_climate(VEHICLE_ID)
        return jsonify({"status": "climate_stopped", "result": res}), 200
    except Exception:
        app.logger.exception("/stop_climate failed")
        return jsonify({"error": "Stop climate failed", "detail": traceback.format_exc()}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
