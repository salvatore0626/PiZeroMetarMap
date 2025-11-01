import sys, time, json, socket, urllib.parse, urllib.request, os
import datetime as dt

# -----------------------------
# Hardware (NeoPixel)
# -----------------------------
import board
import neopixel

# ============================================================
# =============== SETTINGS — EDIT THESE ONLY =================
# ============================================================

# LED strip
LED_COUNT   = 20                 # number of LEDs on your strip/ring
LED_PIN     = board.D18
LED_ORDER   = neopixel.GRB
LED_BRIGHT  = 0.6

# Animation / Flashing
UPDATE_HZ            = 10.0      # LED update rate (e.g., 10 Hz)
LIGHTNING_INTENSITY  = 0.5       # 0.0=never flash, 1.0=solid white, 0.5=half-time white
HIGHWIND_INTENSITY   = 0.5       # 0.0=never flash, 1.0=solid yellow, 0.5=half-time yellow

# Wind logic
HIGH_WIND_THRESHOLD_KT = 20      # sustained >= threshold OR (gust >= threshold if enabled)
FLASH_ON_GUSTS         = True    # if True, gust >= threshold also triggers high-wind flash

# Data fetch / query
FETCH_EVERY_S    = 600           # re-fetch METARs every 10 minutes
LOOKBACK_HOURS   = 5             # consider reports from the last N hours
API_BASE         = "https://aviationweather.gov"
USER_AGENT       = "METARMap/2.0 (+contact@example.com)"
NETWORK_TIMEOUT_S = 10           # socket timeout in seconds

# LED -> Airport mapping (one entry per LED, in order)
# Use ICAO strings like "KPDX". Use None for unused LED positions.
AIRPORTS = [
    "KRBG", "K77S", "KEUG", "KCVO", "KSLE",
    "KMMV", "KUAO", "KHIO", "KTTD", "KPDX",
    "KVUO", "KSPB", "KKLS", "K4S2", "KDLS",
    "KS33", "KS39", "KRDM", "KBDN", "KS21",
]

# Colors (R,G,B) — NeoPixel handles GRB internally
COLOR_VFR       = (0, 255, 0)       # Green
COLOR_MVFR      = (0, 0, 255)       # Blue
COLOR_IFR       = (255, 0, 0)       # Red
COLOR_LIFR      = (255, 0, 255)     # Magenta
COLOR_CLEAR     = (0, 0, 0)         # Off
COLOR_LIGHTNING = (255, 255, 255)   # White (flash for lightning)
COLOR_HIGHWIND  = (255, 255, 0)     # Yellow (flash for high winds)
COLOR_NODATA    = (5, 5, 5)         # very dim gray for no recent data

# ============================================================
# ====================== IMPLEMENTATION ======================
# ============================================================

socket.setdefaulttimeout(NETWORK_TIMEOUT_S)
STATION_IDS = [a.strip().upper() for a in AIRPORTS if a]
SLEEP_S = 1.0 / UPDATE_HZ

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
    os.system("cls" if os.name == "nt" else "clear")

def fetch_bytes(url, tries=3, backoff=1.5):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
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

def fetch_metar_json_ids(stations, hours, chunk_size=150):
    ids = sorted({s.strip().upper() for s in stations if s and str(s).strip()})
    all_records = []
    for i in range(0, len(ids), chunk_size):
        subset = ids[i:i+chunk_size]
        qs = urllib.parse.urlencode({
            "ids": ",".join(subset),
            "hours": hours,
            "format": "json"
        })
        url = f"{API_BASE}/api/data/metar?{qs}"
        raw = fetch_bytes(url)
        all_records.extend(parse_json_records(raw))
    return all_records

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
        wgst  = to_int(
            r.get("wgst") or r.get("gust") or r.get("gustKt") or
            r.get("windGustKt") or r.get("wind_gust_kt") or r.get("gust_kts")
        )
        vis   = to_int(r.get("visib") or r.get("visSM"))
        alt   = to_float(r.get("altim") or r.get("altimHg"))
        raw   = r.get("rawOb") or r.get("raw_text") or ""
        wx    = r.get("wxString") or r.get("wx_string") or ""

        # Lightning heuristic (ignore remarks)
        body = raw.split(" RMK", 1)[0]
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
        (FLASH_ON_GUSTS and cond["windGustSpeed"] >= HIGH_WIND_THRESHOLD_KT)
    )

def base_color(fc):
    if fc == "VFR":  return COLOR_VFR
    if fc == "MVFR": return COLOR_MVFR
    if fc == "IFR":  return COLOR_IFR
    if fc == "LIFR": return COLOR_LIFR
    return COLOR_CLEAR

def pick_color(cond, lightning_on, highwind_on):
    """Priority: Lightning > High wind > Base category; None -> no-data color."""
    if cond is None:
        return COLOR_NODATA
    if cond.get("lightning") and lightning_on:
        return COLOR_LIGHTNING
    if has_high_wind(cond) and highwind_on:
        return COLOR_HIGHWIND
    return base_color(cond.get("flightCategory", ""))

# -----------------------------
# Flash helpers (intensity-based)
# -----------------------------
def station_phase_offset(icao: str) -> float:
    """Deterministic phase offset per station to de-sync flashing (0..1)."""
    if not icao:
        return 0.0
    return (sum(ord(c) for c in icao) & 255) / 256.0

def flashing_state(intensity: float, t_now: float, phase_offset: float) -> bool:
    """
    Returns True/False depending on intensity 0–1.
    intensity = fraction of each 1s cycle that's 'on'.
    """
    if intensity <= 0.0:
        return False
    if intensity >= 1.0:
        return True
    phase = (t_now + phase_offset) % 1.0
    return phase < intensity

# -----------------------------
# Main (continuous)
# -----------------------------
def main():
    if len(AIRPORTS) != LED_COUNT:
        print(f"NOTE: AIRPORTS has {len(AIRPORTS)} entries but LED_COUNT={LED_COUNT}. Using the smaller of the two.")
    usable_leds = min(len(AIRPORTS), LED_COUNT)

    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] Starting METAR Map — {len(STATION_IDS)} stations")

    pixels = neopixel.NeoPixel(
        LED_PIN, LED_COUNT,
        brightness=LED_BRIGHT,
        pixel_order=LED_ORDER,
        auto_write=False
    )

    conds = {}
    last_fetch = 0.0

    while True:
        now = time.time()

        # Fetch/update block
        if (now - last_fetch >= FETCH_EVERY_S) or not conds:
            try:
                recs = fetch_metar_json_ids(STATION_IDS, LOOKBACK_HOURS)
                conds = conditions_from_json(recs)

                clear_terminal()
                print(f"[{dt.datetime.now():%H:%M}] Updated METARs ({len(conds)} stations)")

                # Missing data report
                missing = [a for a in AIRPORTS if a and a not in conds]
                if missing:
                    print("No recent METAR for:", ", ".join(missing))
                    # Optional: uncomment to turn missing LEDs to COLOR_NODATA immediately:
                    # for a in missing:
                    #     conds.pop(a, None)

                # High winds & lightning report
                hw = [a for a, c in conds.items() if has_high_wind(c)]
                lt = [a for a, c in conds.items() if c.get("lightning")]
                if hw: print("High winds at:", ", ".join(hw))
                if lt: print("Lightning reported at:", ", ".join(lt))

            except Exception as e:
                print(f"[{dt.datetime.now():%H:%M}] API error: {e}")
                conds = {}  # LEDs show no-data until next retry

            last_fetch = now

        # Intensity-based flashing (per-station de-synced)
        t = time.monotonic() % 1.0  # 1-second cycle for intensity mapping

        # Render one frame
        for idx in range(usable_leds):
            icao = AIRPORTS[idx]
            c = conds.get(icao) if icao else None
            phase = station_phase_offset(icao)  # de-sync this LED's flash (set to zero for perfect sync)
            lightning_on = flashing_state(LIGHTNING_INTENSITY, t, phase)
            highwind_on  = flashing_state(HIGHWIND_INTENSITY,  t, phase)
            pixels[idx] = pick_color(c, lightning_on, highwind_on)

        # Clear any remaining LEDs beyond mapping
        for idx in range(usable_leds, LED_COUNT):
            pixels[idx] = COLOR_CLEAR

        pixels.show()
        time.sleep(SLEEP_S)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)