import sys, time, json, socket, urllib.parse, urllib.request, os
import datetime as dt

# -----------------------------
# Hardware (NeoPixel)
# -----------------------------
import board
import neopixel

# ============================================================
# ============== EDIT THESE FEW SETTINGS ONLY ================
# ============================================================

# --- LED strip ---
LED_COUNT     = 20               # <— change to your LED count
LED_PIN       = board.D18
LED_ORDER     = neopixel.GRB
LED_BRIGHT    = 0.5

# --- Flash behavior ---
BLINK_SPEED_S          = 1.0      # seconds per flash step
HIGH_WIND_THRESHOLD_KT = 25       # flash yellow when sustained OR gust >= this
ALWAYS_FLASH_FOR_GUSTS = True     # gust >= threshold triggers yellow

# --- Duty-cycle controls (per 10-step cycle) ---
FLASH_CYCLE_STEPS       = 10
DUTY_LIGHTNING_ON_STEPS = 9       # 90% on for lightning
DUTY_HIGHWIND_ON_STEPS  = 5       # 50% on for high winds

# --- Data refresh ---
FETCH_INTERVAL_S = 600           # re-fetch METARs every 10 minutes

# --- What to fetch (state wildcard) ---
STATE_CODE   = "OR"              # "OR" for Oregon; e.g., "WA" for Washington
LOOKBACK_HRS = 5                 # hours before now to search

# --- LED -> Airport mapping (one entry per LED, in order) ---
# Use ICAO strings like "KPDX". Use None for unused LED positions.
AIRPORTS = [
    "KRBG", "K77S", "KEUG", "KCVO", "KSLE",
    "KMMV", "KUAO", "KHIO", "KTTD", "KPDX",
    "KVUO", "KSPB", "KKLS", "K4S2", "KDLS",
    "KS33", "KS39", "KRDM", "KBDN", "KS21",
]

AWC_BASE   = "https://aviationweather.gov"
UA_STRING  = "METARMap/2.0 (+contact@example.com)"  # set your contact
REQUEST_FMT = "json"
socket.setdefaulttimeout(10)

# Standard aviation colors (R,G,B) — NeoPixel handles GRB internally.
COLOR_VFR       = (0, 255, 0)      # Green
COLOR_MVFR      = (0, 0, 255)      # Blue
COLOR_IFR       = (255, 0, 0)      # Red
COLOR_LIFR      = (255, 0, 255)    # Magenta
COLOR_CLEAR     = (0, 0, 0)        # Off
COLOR_LIGHTNING = (255, 255, 255)  # White (flash for lightning)
COLOR_HIGHWIND  = (255, 255, 0)    # Yellow (flash for high winds)
COLOR_NODATA    = (5, 5, 5)

# -----------------------------
# Helpers
# -----------------------------
def to_int(v, default=0):
    try:
        return int(round(float(str(v).replace('+', '').strip())))
    except Exception:
        return default

def to_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default
    
def clear_terminal():
    # Works on macOS, Linux, and Windows
    os.system("cls" if os.name == "nt" else "clear")

def fetch_bytes(url, tries=3, backoff=1.5):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA_STRING})
            with urllib.request.urlopen(req) as r:
                if r.status == 204:  # valid request, no data
                    return b"[]"
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited
                time.sleep(backoff); backoff *= 2; last = e; continue
            last = e; time.sleep(backoff); backoff *= 1.5
        except Exception as e:
            last = e; time.sleep(backoff); backoff *= 1.5
    if last: raise last

def fetch_metar_json_state(state_code, hours, fmt="json"):
    ids_value = "@"+state_code
    qs = urllib.parse.urlencode({"ids": ids_value, "hours": hours, "format": fmt})
    url = f"{AWC_BASE}/api/data/metar?{qs}"
    return fetch_bytes(url)

def parse_json_records(raw):
    try:
        j = json.loads(raw.decode("utf-8"))
    except Exception:
        return []
    if isinstance(j, dict) and j.get("type") == "FeatureCollection":
        return [f.get("properties", {}) for f in j.get("features", [])]
    if isinstance(j, dict):
        return j.get("data") or j.get("metar") or []
    if isinstance(j, list):
        return j
    return []

def conditions_from_json(records):
    """Return dict[ICAO] -> condition dict (latest per station)."""
    latest = {}
    for r in records:
        icao = (r.get("icaoId") or r.get("station") or r.get("station_id") or "").strip().upper()
        if not icao:
            continue

        # Prefer ISO reportTime; else epoch obsTime
        rt = r.get("reportTime")
        if rt:
            try:
                obs_dt = dt.datetime.fromisoformat(rt.replace("Z","+00:00"))
            except Exception:
                obs_dt = dt.datetime.now(dt.timezone.utc)
        else:
            try:
                obs_dt = dt.datetime.fromtimestamp(int(r.get("obsTime", 0)), tz=dt.timezone.utc)
            except Exception:
                obs_dt = dt.datetime.now(dt.timezone.utc)

        if icao not in latest or obs_dt > latest[icao]["_dt"]:
            latest[icao] = {"r": r, "_dt": obs_dt}

    out = {}
    for icao, bundle in latest.items():
        r     = bundle["r"]
        fc    = (r.get("fltCat") or r.get("flight_category") or "").strip().upper()
        wspd  = to_int(r.get("wspd") or r.get("windSpeedKt"))
        wgst  = to_int(r.get("wgst") or r.get("windGustKt"))
        vis   = to_int(r.get("visib") or r.get("visSM"))
        alt   = to_float(r.get("altim") or r.get("altimHg"))
        raw   = r.get("rawOb") or r.get("raw_text") or ""
        wx    = r.get("wxString") or r.get("wx_string") or ""

        # Lightning heuristic (ignore remarks)
        body = raw.split(" RMK ")[0]
        lightning = (("LTG" in body) or (" TS" in body)) and (" TSNO" not in raw)

        out[icao] = {
            "flightCategory": fc,
            "windSpeed": wspd,
            "windGustSpeed": wgst,
            "vis": vis,
            "altimHg": alt,
            "obs": wx,
            "lightning": lightning,
            "obsTime": bundle["_dt"],
        }
    return out

# -----------------------------
# LED color logic
# -----------------------------
def has_high_wind(cond):
    return (
        (cond["windSpeed"] >= HIGH_WIND_THRESHOLD_KT) or
        (ALWAYS_FLASH_FOR_GUSTS and cond["windGustSpeed"] >= HIGH_WIND_THRESHOLD_KT)
    )

def base_color(fc):
    if fc == "VFR":  return COLOR_VFR
    if fc == "MVFR": return COLOR_MVFR
    if fc == "IFR":  return COLOR_IFR
    if fc == "LIFR": return COLOR_LIFR
    return COLOR_CLEAR

def pick_color(cond, lightning_on, highwind_on):
    if cond is None:
        return COLOR_NODATA
    # Priority: Lightning > High wind > Base category
    if cond["lightning"] and lightning_on:
        return COLOR_LIGHTNING
    if has_high_wind(cond) and highwind_on:
        return COLOR_HIGHWIND
    return base_color(cond["flightCategory"])

# -----------------------------
# Main (continuous)
# -----------------------------
def main():
    # Sanity + setup
    if len(AIRPORTS) != LED_COUNT:
        print(f"NOTE: AIRPORTS has {len(AIRPORTS)} entries but LED_COUNT={LED_COUNT}. Using the smaller of the two.")
    usable_leds = min(len(AIRPORTS), LED_COUNT)

    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] Starting METAR Map — @{STATE_CODE}")

    pixels = neopixel.NeoPixel(
        LED_PIN, LED_COUNT,
        brightness=LED_BRIGHT,
        pixel_order=LED_ORDER,
        auto_write=False
    )

    # State for updates/animation
    conds = {}
    last_fetch = 0.0
    step = 0

    # Continuous loop
    while True:
        now = time.time()

        # Fetch/update block
        if (now - last_fetch >= FETCH_INTERVAL_S) or not conds:
            try:
                raw = fetch_metar_json_state(STATE_CODE, LOOKBACK_HRS, REQUEST_FMT)
                recs = parse_json_records(raw)
                conds = conditions_from_json(recs)

                # Optional: clear terminal for a clean dashboard
                clear_terminal()

                print(f"[{dt.datetime.now():%H:%M}] Updated METARs ({len(conds)} stations)")

                # Log which mapped stations have no data
                missing = [a for a in AIRPORTS if a and a not in conds]
                if missing:
                    print("No recent METAR for:", ", ".join(missing))

                # Log high winds & lightning stations
                high_wind = [a for a, c in conds.items() if has_high_wind(c)]
                lightning = [a for a, c in conds.items() if c.get("lightning")]

                if high_wind:
                    print("High winds at:", ", ".join(high_wind))
                if lightning:
                    print("Lightning reported at:", ", ".join(lightning))

            except Exception as e:
                print(f"[{dt.datetime.now():%H:%M}] API error: {e}")
                conds = {}  # keep LEDs in no-data state until next retry

            last_fetch = now

        # Duty-cycle animation (90/10 lightning, 50/50 high wind)
        lightning_on = (step % FLASH_CYCLE_STEPS) < DUTY_LIGHTNING_ON_STEPS
        highwind_on  = (step % FLASH_CYCLE_STEPS) < DUTY_HIGHWIND_ON_STEPS

        # Render one frame
        for idx in range(usable_leds):
            icao = AIRPORTS[idx]
            c = conds.get(icao) if icao else None
            pixels[idx] = pick_color(c, lightning_on, highwind_on)

        # Clear any remaining LEDs beyond mapping
        for idx in range(usable_leds, LED_COUNT):
            pixels[idx] = COLOR_CLEAR

        pixels.show()
        time.sleep(BLINK_SPEED_S)
        step += 1

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
