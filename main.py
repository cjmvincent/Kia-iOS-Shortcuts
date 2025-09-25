import os
import time
import traceback
from typing import Optional
from flask import Flask, request, jsonify
from hyundai_kia_connect_api import VehicleManager, ClimateRequestOptions
from hyundai_kia_connect_api.exceptions import AuthenticationError

app = Flask(__name__)

# --------- Environment ---------
USERNAME: Optional[str] = os.getenv("KIA_USERNAME")
PASSWORD: Optional[str] = os.getenv("KIA_PASSWORD")
PIN: Optional[str] = os.getenv("KIA_PIN")
SECRET_KEY: Optional[str] = os.getenv("SECRET_KEY")
VEHICLE_ID: Optional[str] = os.getenv("VEHICLE_ID")

if not USERNAME or not PASSWORD or not PIN or not SECRET_KEY:
    raise ValueError("Missing one or more required environment variables.")

# --------- Region/Brand fallback ---------
def _resolve_region_brand():
    try:
        from hyundai_kia_connect_api import Brand as _Brand, Region as _Region
        return getattr(_Region, "US", getattr(_Region, "NORTH_AMERICA", 3)), _Brand.KIA
    except Exception:
        try:
            from hyundai_kia_connect_api.const import Brand as _Brand, Region as _Region
            return getattr(_Region, "US", getattr(_Region, "NORTH_AMERICA", 3)), _Brand.KIA
        except Exception:
            return int(os.getenv("KIA_REGION", "3")), int(os.getenv("KIA_BRAND", "1"))

# --------- Globals ---------
vehicle_manager: Optional[VehicleManager] = None
init_error: Optional[str] = None

def _init_vehicle_manager():
    global vehicle_manager, init_error, VEHICLE_ID
    if vehicle_manager is not None:
        return
    if not USERNAME or not PASSWORD or not PIN:
        init_error = "Missing required env vars"
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
        vm.check_and_refresh_token()
        vm.update_all_vehicles_with_cached_state()
        if not vm.vehicles:
            init_error = "No vehicles found"
            return
        if not VEHICLE_ID:
            VEHICLE_ID = next(iter(vm.vehicles.keys()))
        vehicle_manager = vm
        init_error = None
    except Exception:
        init_error = traceback.format_exc()

def ensure_initialized():
    _init_vehicle_manager()
    if vehicle_manager is None:
        return False, (init_error or "Initialization failed")
    return True, "ok"

@app.get("/")
def health():
    ok, msg = ensure_initialized()
    return jsonify({"status": "ok" if ok else "error", "message": msg}), (200 if ok else 500)

@app.get("/status")
def status():
    ok, msg = ensure_initialized()
    if not ok:
        return jsonify({"error": msg}), 500
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

@app.post("/lock_car")
def lock_car():
    ok, msg = ensure_initialized()
    if not ok:
        return jsonify({"error": msg}), 500
    res = vehicle_manager.lock(VEHICLE_ID)
    return jsonify({"status": "locked", "result": res}), 200

@app.post("/unlock_car")
def unlock_car():
    ok, msg = ensure_initialized()
    if not ok:
        return jsonify({"error": msg}), 500
    res = vehicle_manager.unlock(VEHICLE_ID)
    return jsonify({"status": "unlocked", "result": res}), 200

@app.post("/start_climate")
def start_climate():
    ok, msg = ensure_initialized()
    if not ok:
        return jsonify({"error": msg}), 500

    body = request.get_json(silent=True) or {}
    # duration 1..30 minutes
    try:
        duration = int(body.get("duration", 10))
    except Exception:
        duration = 10
    duration = max(1, min(duration, 30))
    defrost = bool(body.get("defrost", False))
    # default temp; set CLIMATE_DEGREES=C in env if your car uses °C
    units = os.getenv("CLIMATE_DEGREES", "F").upper()
    temperature = body.get("temperature", (22 if units == "C" else 72))

    # Build options with constructor first; fall back to setting attributes
    def build_opts(_duration, _defrost, _temp):
        try:
            return ClimateRequestOptions(
                duration=_duration,
                defrost=_defrost,
                climate=True,
                heating=True,
                set_temp=_temp,
            )
        except TypeError:
            # Older sig: ClimateRequestOptions(duration, defrost)
            opts = ClimateRequestOptions(_duration, _defrost)
            for name in ("set_temperature", "target_temperature", "temperature", "targetTemperature"):
                if hasattr(opts, name):
                    setattr(opts, name, _temp)
                    break
            return opts

    try:
        # Keep session fresh and state current
        vehicle_manager.check_and_refresh_token()
        vehicle_manager.update_all_vehicles_with_cached_state()

        # Some NA trims require locked doors before remote start/climate
        try:
            vehicle_manager.lock(VEHICLE_ID)
        except Exception:
            pass  # ignore if not required/already locked

        # First attempt with requested options
        opts = build_opts(duration, defrost, temperature)
        try:
            res = vehicle_manager.start_climate(VEHICLE_ID, opts)
            return jsonify({"status": "climate_started", "result": res}), 200
        except Exception as e1:
            # Fallback: shorter duration & defrost off
            try:
                opts2 = build_opts(5, False, temperature)
                res2 = vehicle_manager.start_climate(VEHICLE_ID, opts2)
                return jsonify({"status": "climate_started", "result": res2, "note": "fallback options used"}), 200
            except Exception as e2:
                # Graceful JSON error (don’t 500 the whole app)
                detail = getattr(e2, "response", None) or getattr(e1, "response", None) or str(e2)
                return jsonify({
                    "error": "Start climate failed",
                    "detail": detail,
                    "debug": {"duration": duration, "defrost": defrost, "temperature": temperature, "units": units}
                }), 400

    except Exception as e:
        # Any unexpected failure -> clean JSON, not an Internal Server Error
        return jsonify({"error": "Start climate precheck failed", "detail": str(e)}), 400


@app.post("/stop_climate")
def stop_climate():
    ok, msg = ensure_initialized()
    if not ok:
        return jsonify({"error": msg}), 500
    res = vehicle_manager.stop_climate(VEHICLE_ID)
    return jsonify({"status": "climate_stopped", "result": res}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))