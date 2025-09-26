import os
import traceback
from typing import Optional
from flask import Flask, request, jsonify
from hyundai_kia_connect_api import VehicleManager, ClimateRequestOptions

app = Flask(__name__)

# --------- Environment ---------
USERNAME: Optional[str] = os.getenv("KIA_USERNAME")
PASSWORD: Optional[str] = os.getenv("KIA_PASSWORD")
PIN: Optional[str] = os.getenv("KIA_PIN")
SECRET_KEY: Optional[str] = os.getenv("SECRET_KEY")
VEHICLE_ID: Optional[str] = os.getenv("VEHICLE_ID")
REGION: int = int(os.getenv("KIA_REGION", "3"))
BRAND: int = int(os.getenv("KIA_BRAND", "1"))

if not USERNAME or not PASSWORD or not PIN or not SECRET_KEY:
    raise ValueError("Missing one or more required environment variables.")

app.secret_key = SECRET_KEY

# --------- Globals ---------
vehicle_manager: Optional[VehicleManager] = None
init_error: Optional[str] = None


def _init_vehicle_manager():
    """One-time initialization of VehicleManager with simple, predictable behavior."""
    global vehicle_manager, init_error, VEHICLE_ID
    if vehicle_manager is not None:
        return
    try:
        vm = VehicleManager(
            region=REGION,
            brand=BRAND,
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


# --------- Get current status ---------
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

    locked = getattr(v, "is_locked", None)
    lock_status = (
        "locked" if locked is True
        else "unlocked" if locked is False
        else "with unknown lock state"
    )

    charging = getattr(v, "is_charging", None)
    charging_clause = (
        "and charging" if charging is True
        else "and not charging" if charging is False
        else "and charging status unknown"
    )

    battery = getattr(v, "battery_level", None)
    battery_clause = f" Battery is at {battery}%." if battery is not None else ""

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
        run_clause = ""

    sentence = f"{name} is currently {lock_status} {charging_clause}.{battery_clause}{run_clause}"
    return sentence, 200, {"Content-Type": "text/plain; charset=utf-8"}


# --------- Lock / Unlock (Text) ---------
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


# --------- Start / Stop Climate (Text) ---------
@app.post("/start_climate")
def start_climate():
    ok, msg = ensure_initialized()
    if not ok:
        return msg, 500, {"Content-Type": "text/plain; charset=utf-8"}

    body = request.get_json(silent=True) or {}
    # duration 1..30 minutes
    try:
        duration = int(body.get("duration", 10))
    except Exception:
        duration = 15
    duration = max(1, min(duration, 30))
    defrost = bool(body.get("defrost", False))
    # default temp; set CLIMATE_DEGREES=C in env if your car uses °C
    units = os.getenv("CLIMATE_DEGREES", "F").upper()
    temperature = body.get("temperature", (22 if units == "C" else 70))

    # Helpers
    def build_opts(_duration, _defrost, _temp):
        # Prefer newer constructor signature; fall back to older then setattr
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

    def snapshot(v):
        # Normalize across versions
        return {
            "locked": getattr(v, "is_locked", None),
            "any_door_open": getattr(v, "is_any_door_open", None) or getattr(v, "door_open", None),
            "hood_open": getattr(v, "hood_open", None) or getattr(v, "is_hood_open", None),
            "trunk_open": getattr(v, "trunk_open", None) or getattr(v, "is_trunk_open", None) or getattr(v, "is_tailgate_open", None),
            "ignition_on": getattr(v, "ignition_on", None) or getattr(v, "engine_on", None),
            "gear": getattr(v, "gear", None) or getattr(v, "transmission_gear", None) or getattr(v, "gear_position", None),
        }

    def preflight(v):
        s = snapshot(v)
        missing = []
        # attempt to lock if unlocked
        if s["locked"] is False:
            try:
                vehicle_manager.lock(VEHICLE_ID)
                vehicle_manager.update_all_vehicles_with_cached_state()
                s = snapshot(v)
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
        gear = s["gear"]
        if gear not in (None, "P", "Park", "park", 0):
            missing.append("Transmission must be in Park")
        return missing

    try:
        # Keep session fresh and state current
        vehicle_manager.check_and_refresh_token()
        vehicle_manager.update_all_vehicles_with_cached_state()

        v = vehicle_manager.vehicles.get(VEHICLE_ID)
        if not v:
            return "Vehicle not found.", 404, {"Content-Type": "text/plain; charset=utf-8"}

        missing = preflight(v)
        if missing:
            return (
                "Preconditions not met: " + "; ".join(missing) + ".",
                400,
                {"Content-Type": "text/plain; charset=utf-8"},
            )

        # Attempt
        try:
            res = vehicle_manager.start_climate(VEHICLE_ID, build_opts(duration, defrost, temperature))
            name = getattr(v, "name", "Your car")
            return (
                f"{name}'s climate has been started for {duration} minutes at {temperature}°{units}.",
                200,
                {"Content-Type": "text/plain; charset=utf-8"},
            )
        except Exception:
            # Fallback attempt
            try:
                vehicle_manager.start_climate(VEHICLE_ID, build_opts(5, False, temperature))
                name = getattr(v, "name", "Your car")
                return (
                    f"{name}'s climate has been started for 5 minutes at {temperature}°{units}.",
                    200,
                    {"Content-Type": "text/plain; charset=utf-8"},
                )
            except Exception:
                return "There was an issue starting climate.", 400, {"Content-Type": "text/plain; charset=utf-8"}

    except Exception:
        return "There was an issue starting climate.", 400, {"Content-Type": "text/plain; charset=utf-8"}


@app.post("/stop_climate")
def stop_climate():
    ok, msg = ensure_initialized()
    if not ok:
        return msg, 500, {"Content-Type": "text/plain; charset=utf-8"}

    name = getattr(vehicle_manager.vehicles.get(VEHICLE_ID), "name", "Your car")
    try:
        vehicle_manager.stop_climate(VEHICLE_ID)
        return f"{name}'s climate has been stopped.", 200, {"Content-Type": "text/plain; charset=utf-8"}
    except Exception:
        return "There was an issue stopping climate.", 400, {"Content-Type": "text/plain; charset=utf-8"}


if __name__ == "__main__":
    print("Starting Kia Vehicle Control API...")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
