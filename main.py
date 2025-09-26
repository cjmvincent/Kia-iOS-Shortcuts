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

# --------- Get current startus of the vehicle ---------
@app.get("/status")
def status_text():
    ok, msg = ensure_initialized()
    if not ok:
        return msg, 500, {"Content-Type": "text/plain; charset=utf-8"}

    vehicle_manager.update_all_vehicles_with_cached_state()
    v = vehicle_manager.vehicles.get(VEHICLE_ID)
    if not v:
        return (f"Vehicle {VEHICLE_ID} not found.", 404,
                {"Content-Type": "text/plain; charset=utf-8"})

    name = getattr(v, "name", None) or "Your car"

    # Lock
    locked = getattr(v, "is_locked", None)
    lock_status = (
        "locked" if locked is True
        else "unlocked" if locked is False
        else "with unknown lock state"
    )

    # Charging
    charging = getattr(v, "is_charging", None)
    charging_clause = (
        "and charging" if charging is True
        else "and not charging" if charging is False
        else "and charging status unknown"
    )

    # Battery
    battery = getattr(v, "battery_level", None)
    battery_clause = f" Battery is at {battery}%." if battery is not None else ""

    # Ignition / climate
    ignition_on = getattr(v, "ignition_on", None) or getattr(v, "engine_on", None)
    climate_on = getattr(v, "climate_on", None) or getattr(v, "is_climate_running", None)

    if ignition_on is True and climate_on is True:
        run_clause = " The car and climate are on."
    elif ignition_on is True and climate_on is False:
        run_clause = " The car is on and climate is off."
    elif ignition_on is False and climate_on is True:
        run_clause = " The car is off and climate is on."
    elif ignition_on is False and climate_on is False:
        run_clause = " The car and climate are off."
    else:
        run_clause = "and run status us unknown"

    sentence = f"{name} is currently {lock_status} {charging_clause}.{battery_clause}{run_clause}"
    return sentence, 200, {"Content-Type": "text/plain; charset=utf-8"}

# --------- Lock the vehicle ---------
@app.post("/lock_car")
def lock_car():
    ok, msg = ensure_initialized()
    if not ok:
        return msg, 500, {"Content-Type": "text/plain; charset=utf-8"}

    name = getattr(vehicle_manager.vehicles.get(VEHICLE_ID), "name", "Your car")
    try:
        vehicle_manager.lock(VEHICLE_ID)
        return f"{name} has been locked.", 200, {"Content-Type": "text/plain; charset=utf-8"}
    except Exception:
        return "There was an issue locking the car.", 400, {"Content-Type": "text/plain; charset=utf-8"}

# --------- Unlock the vehicle ---------
@app.post("/unlock_car")
def unlock_car():
    ok, msg = ensure_initialized()
    if not ok:
        return msg, 500, {"Content-Type": "text/plain; charset=utf-8"}

    name = getattr(vehicle_manager.vehicles.get(VEHICLE_ID), "name", "Your car")
    try:
        vehicle_manager.unlock(VEHICLE_ID)
        return f"{name} has been unlocked.", 200, {"Content-Type": "text/plain; charset=utf-8"}
    except Exception:
        return "There was an issue unlocking the car.", 400, {"Content-Type": "text/plain; charset=utf-8"}

# --------- Start the vehicle or climate ---------
@app.post("/start_climate")
def start_climate():
    ok, msg = ensure_initialized()
    if not ok:
        return msg, 500, {"Content-Type": "text/plain; charset=utf-8"}

    body = request.get_json(silent=True) or {}
    try:
        duration = int(body.get("duration", 10))
    except Exception:
        duration = 10
    duration = max(1, min(duration, 30))
    defrost = bool(body.get("defrost", False))
    units = os.getenv("CLIMATE_DEGREES", "F").upper()
    temperature = body.get("temperature", 22 if units == "C" else 72)

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
            opts = ClimateRequestOptions(_duration, _defrost)
            for name in ("set_temperature", "target_temperature", "temperature", "targetTemperature"):
                if hasattr(opts, name):
                    setattr(opts, name, _temp)
                    break
            return opts

    try:
        vehicle_manager.check_and_refresh_token()
        vehicle_manager.update_all_vehicles_with_cached_state()

        opts = build_opts(duration, defrost, temperature)
        res = vehicle_manager.start_climate(VEHICLE_ID, opts)
        name = getattr(vehicle_manager.vehicles.get(VEHICLE_ID), "name", "Your car")
        return (
            f"{name}'s climate has been started for {duration} minutes at {temperature}°{units}.",
            200,
            {"Content-Type": "text/plain; charset=utf-8"},
        )
    except Exception as e:
        return (
            f"Failed to start climate: {str(e)}",
            400,
            {"Content-Type": "text/plain; charset=utf-8"},
        )

# --------- Turn off the vehicle or climate ---------
@app.post("/stop_climate")
def stop_climate():
    ok, msg = ensure_initialized()
    if not ok:
        return jsonify({"error": msg}), 500
    res = vehicle_manager.stop_climate(VEHICLE_ID)
    return jsonify({"status": "climate_stopped", "result": res}), 200

if __name__ == "__main__":
    print("Starting Kia Vehicle Control API...")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))