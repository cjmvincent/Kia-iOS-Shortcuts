import os
import traceback
import logging
from typing import Optional
from flask import Flask, request, jsonify
from hyundai_kia_connect_api import VehicleManager, ClimateRequestOptions
from hyundai_kia_connect_api.exceptions import AuthenticationError

app = Flask(__name__)

# Verbose logging to help diagnose issues in Vercel logs
logging.basicConfig(level=logging.INFO)
logging.getLogger("urllib3").setLevel(logging.WARNING)
try:
    from importlib.metadata import version as _pkg_version  # py3.8+
    _hkc_ver = _pkg_version("hyundai-kia-connect-api")
except Exception:
    _hkc_ver = "unknown"
app.logger.info("CLEANED_MAIN v4 loaded (hyundai-kia-connect-api=%s)", _hkc_ver)

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
    """Return (region, brand) candidates for VehicleManager.
    We return a *list* of candidates to try in order, to survive enum drift.
    """
    candidates = []
    # Try newer-style enums
    try:
        from hyundai_kia_connect_api import Brand as _Brand, Region as _Region  # type: ignore
        if hasattr(_Region, "US"):
            candidates.append((_Region.US, _Brand.KIA))
        if hasattr(_Region, "NORTH_AMERICA"):
            candidates.append((_Region.NORTH_AMERICA, _Brand.KIA))
    except Exception:
        pass
    # Try older-style enums under .const
    try:
        from hyundai_kia_connect_api.const import Brand as _Brand, Region as _Region  # type: ignore
        if hasattr(_Region, "US"):
            candidates.append((_Region.US, _Brand.KIA))
        if hasattr(_Region, "NORTH_AMERICA"):
            candidates.append((_Region.NORTH_AMERICA, _Brand.KIA))
    except Exception:
        pass
    # Fallback to integers
    try:
        region_int = int(os.getenv("KIA_REGION", "3"))
        brand_int = int(os.getenv("KIA_BRAND", "1"))
    except Exception:
        region_int, brand_int = 3, 1
    candidates.append((region_int, brand_int))
    return candidates

# --------- Globals (lazy init) ---------
vehicle_manager: Optional[VehicleManager] = None
init_error: Optional[str] = None


def _init_vehicle_manager() -> None:
    """One-time, robust initialization of the VehicleManager with retries and fallbacks."""
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

    import time
    last_err = None
    for attempt in range(1, 3):  # simple retry loop
        for region_val, brand_val in _resolve_region_brand():
            try:
                app.logger.info("[init] Attempting to authenticate and refresh token… (attempt %d, region=%s, brand=%s)", attempt, region_val, brand_val)
                vm = VehicleManager(
                    region=region_val,
                    brand=brand_val,
                    username=USERNAME,
                    password=PASSWORD,
                    pin=str(PIN),
                )
                vm.check_and_refresh_token()
                app.logger.info("[init] Token refreshed. Updating vehicle states…")
                vm.update_all_vehicles_with_cached_state()
                if not vm.vehicles:
                    raise RuntimeError("Authenticated but no vehicles were returned by the API.")
                if not VEHICLE_ID:
                    VEHICLE_ID = next(iter(vm.vehicles.keys()))
                    app.logger.info("[init] No VEHICLE_ID provided. Using first vehicle: %s", VEHICLE_ID)
                # Monkey-patch: verbose HTTP logging of outgoing requests when LOG_HTTP=1
                if os.getenv("LOG_HTTP") == "1":
                    try:
                        import functools
                        sess = getattr(vm.api, "session", None)
                        if sess and hasattr(sess, "request"):
                            orig_req = sess.request
                            @functools.wraps(orig_req)
                            def _req(method, url, *args, **kwargs):
                                body = kwargs.get("json") or kwargs.get("data")
                                app.logger.info("[http] %s %s body=%s", method, url, body)
                                resp = orig_req(method, url, *args, **kwargs)
                                try:
                                    app.logger.info("[http] -> %s %s", resp.status_code, getattr(resp, "text", ""))
                                except Exception:
                                    pass
                                return resp
                            sess.request = _req
                            app.logger.info("[init] HTTP logging enabled for hyundai-kia-connect-api session")
                    except Exception:
                        app.logger.exception("[init] Failed to enable HTTP logging")
                vehicle_manager = vm
                init_error = None
                return
            except Exception as e:
                last_err = e
                app.logger.error("[init] init attempt failed: %s", traceback.format_exc())
        time.sleep(0.8)  # brief backoff

    init_error = f"Initialization failed after retries: {last_err}"


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
    detail = None
    if not ok:
        detail = init_error
    else:
        try:
            vehicle_ids = list(vehicle_manager.vehicles.keys())
        except Exception:
            vehicle_ids = []
    return jsonify({
        "ok": ok,
        "init_error": detail,
        "vehicle_ids": (vehicle_ids if ok else []),
        "lib_version": _hkc_ver,
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
    try:
        duration = int(body.get("duration", 10))
    except Exception:
        duration = 10
    duration = max(1, min(duration, 30))
    defrost = bool(body.get("defrost", False))

    temp_env_units = (os.getenv("CLIMATE_DEGREES", "F").upper())
    default_temp = 72 if temp_env_units == "F" else 22
    temperature = body.get("temperature", default_temp)
    force = bool(body.get("force", False))  # skip preflight if true

    def _apply_temperature(opts, temp):
        try:
            if temp is None:
                return False
            for name in ("temperature", "target_temperature", "set_temperature", "targetTemperature"):
                if hasattr(opts, name):
                    setattr(opts, name, temp)
                    return True
        except Exception:
            pass
        return False

    def _vehicle_state_snapshot(v):
        return {
            "locked": getattr(v, "is_locked", None),
            "any_door_open": getattr(v, "is_any_door_open", None) or getattr(v, "door_open", None),
            "hood_open": getattr(v, "hood_open", None) or getattr(v, "is_hood_open", None),
            "trunk_open": getattr(v, "trunk_open", None) or getattr(v, "is_trunk_open", None) or getattr(v, "is_tailgate_open", None),
            "ignition_on": getattr(v, "ignition_on", None) or getattr(v, "engine_on", None),
            "gear": getattr(v, "gear", None) or getattr(v, "transmission_gear", None) or getattr(v, "gear_position", None),
        }

    def _preflight(v):
        s = _vehicle_state_snapshot(v)
        missing = []
        # try to lock if unlocked
        if s["locked"] is False:
            try:
                vehicle_manager.lock(VEHICLE_ID)
                s["locked"] = True
            except Exception:
                pass
        if s["locked"] is False:
            missing.append("Doors must be locked")
        if s["any_door_open"] is True:
            missing.append("All doors must be closed")
        if s["hood_open"] is True:
            missing.append("Hood must be closed")
        if s["trunk_open"] is True:
            missing.append("Trunk/tailgate must be closed")
        if s["ignition_on"] is True:
            missing.append("Ignition/ACC must be off")
        # Gear check: accept values that look like 'P', 'Park', or 0 for park; flag otherwise if known
        gear = s["gear"]
        if gear not in (None, "P", "Park", "park", 0):
            missing.append("Transmission must be in Park")
        return missing, s

    try:
        vehicle_manager.check_and_refresh_token()
        vehicle_manager.update_all_vehicles_with_cached_state()
        v = vehicle_manager.vehicles.get(VEHICLE_ID)
        if not v:
            return jsonify({"error": f"Vehicle {VEHICLE_ID} not found."}), 404

        if not force:
            missing, snap = _preflight(v)
            if missing:
                return jsonify({
                    "error": "Preconditions not met for remote climate",
                    "missing": missing,
                    "snapshot": snap
                }), 400

        # Build options (newer ctor first)
        try:
            opts = ClimateRequestOptions(
                duration=duration,
                defrost=defrost,
                climate=True,
                heating=True,
                set_temp=temperature,
            )
            temp_applied = True
        except TypeError:
            opts = ClimateRequestOptions(duration, defrost)
            temp_applied = _apply_temperature(opts, temperature)

        app.logger.info(
            "/start_climate with duration=%s, defrost=%s, temp=%s, temp_applied=%s, force=%s",
            duration, defrost, temperature, temp_applied, force,
        )

        try:
            res = vehicle_manager.start_climate(VEHICLE_ID, opts)
            return jsonify({"status": "climate_started", "result": res}), 200
        except Exception as e1:
            app.logger.exception("/start_climate first attempt failed: %s", e1)
            try:
                opts2 = ClimateRequestOptions(5, False)
                _apply_temperature(opts2, temperature)
                res2 = vehicle_manager.start_climate(VEHICLE_ID, opts2)
                return jsonify({"status": "climate_started", "result": res2, "note": "fallback options used"}), 200
            except Exception as e2:
                app.logger.exception("/start_climate fallback failed: %s", e2)
                detail = getattr(e2, "response", None) or getattr(e1, "response", None) or str(e2)
                return jsonify({
                    "error": "Start climate failed",
                    "detail": detail,
                    "debug": {"duration": duration, "defrost": defrost, "temp": temperature, "units": temp_env_units}
                }), 500

    except Exception as e:
        app.logger.exception("/start_climate failed before request: %s", e)
        return jsonify({"error": "Start climate precheck failed", "detail": traceback.format_exc()}), 500


@app.get("/debug/capabilities")
def debug_capabilities():
    """Return basic capability hints from the vehicle object (if exposed by the lib)."""
    ok, msg = ensure_initialized()
    if not ok:
        return jsonify({"error": msg}), 500
    try:
        vehicle_manager.update_all_vehicles_with_cached_state()
        v = vehicle_manager.vehicles.get(VEHICLE_ID)
        if not v:
            return jsonify({"error": f"Vehicle {VEHICLE_ID} not found."}), 404
        caps = {
            "supports_remote_start": getattr(v, "supports_remote_start", None),
            "supports_climate": getattr(v, "supports_climate", None),
            "is_ev": getattr(v, "is_ev", None),
            "engine_type": getattr(v, "engine_type", None),
            "model": getattr(v, "model", None),
            "year": getattr(v, "year", None),
        }
        return jsonify(caps), 200
    except Exception:
        app.logger.exception("/debug/capabilities failed")
        return jsonify({"error": "Failed to read capabilities", "detail": traceback.format_exc()}), 500


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
