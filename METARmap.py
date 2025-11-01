import sys, time, json, socket, urllib.parse, urllib.request, os, threading
import datetime as dt
import board
import neopixel

# ============================================================
# =============== SETTINGS — EDIT THESE ONLY =================
# ============================================================

# LED strip
LED_COUNT   = 20
LED_PIN     = board.D18              # uses PWM; see note below about audio
LED_ORDER   = neopixel.GRB
LED_BRIGHT  = 0.6

# Animation / Flashing (intensity = fraction of each second 'on')
UPDATE_HZ            = 10.0          # render rate
LIGHTNING_INTENSITY  = 0.5           # 0=never flash, 1=solid white
HIGHWIND_INTENSITY   = 0.5           # 0=never flash, 1=solid yellow

# Optional heartbeat LED for diagnostics (blinks at 1 Hz regardless of weather)
# Set to an LED index (0..LED_COUNT-1) to enable, or None to disable.
HEARTBEAT_LED        = None          # e.g., set to LED_COUNT-1 to test

# Wind logic
HIGH_WIND_THRESHOLD_KT = 20
FLASH_ON_GUSTS         = True

# Data fetch
FETCH_EVERY_S   = 600                # normal fetch interval (10 minutes)
ERROR_RETRY_S   = 60                 # after an error, try again in ~60s
LOOKBACK_HOURS  = 5
API_BASE        = "https://aviationweather.gov"
USER_AGENT      = "METARMap/2.0 (+contact@example.com)"
NETWORK_TIMEOUT_S = 10

# Mapping (one per LED; use None for unused positions)
AIRPORTS = [
    "KRBG", "K77S", "KEUG", "KCVO", "KSLE",
    "KMMV", "KUAO", "KHIO", "KTTD", "KPDX",
    "KVUO", "KSPB", "KKLS", "K4S2", "KDLS",
    "KS33", "KS39", "KRDM", "KBDN", "KS21",
]

# Colors (R,G,B)
COLOR_VFR       = (0, 255, 0)
COLOR_MVFR      = (0, 0, 255)
COLOR_IFR       = (255, 0, 0)
COLOR_LIFR      = (255, 0, 255)
COLOR_CLEAR     = (0, 0, 0)
COLOR_LIGHTNING = (255, 255, 255)
COLOR_HIGHWIND  = (255, 255, 0)
COLOR_NODATA    = (5, 5, 5)

# ============================================================
# ====================== IMPLEMENTATION ======================
# ============================================================

socket.setdefaulttimeout(NETWORK_TIMEOUT_S)
STATION_IDS = [a.strip().upper() for a in AIRPORTS if a]
SLEEP_S = 1.0 / UPDATE_HZ

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
                if r.status == 204:
                    return b"[]"
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
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
        qs = urllib.parse.urlencode({"ids": ",".join(subset), "hours": hours, "format": "json"})
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
    latest = {}
    for r in records:
        icao = (r.get("icaoId") or r.get("station") or r.get("station_id") or "").strip().upper()
        if not icao:
            continue
        rt = r.get("reportTime")
        if rt:
            try:
                obs_dt = dt.datetime.fromisoformat(rt.replace("Z", "+00:00"))
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
        r = bundle["r"]
        fc = (r.get("fltCat") or r.get("flight_category") or "").strip().upper()
        wspd = to_int(r.get("wspd") or r.get("windSpeedKt"))
        wgst = to_int(
            r.get("wgst") or r.get("gust") or r.get("gustKt") or
            r.get("windGustKt") or r.get("wind_gust_kt") or r.get("gust_kts")
        )
        vis = to_int(r.get("visib") or r.get("visSM"))
        alt = to_float(r.get("altim") or r.get("altimHg"))
        raw = r.get("rawOb") or r.get("raw_text") or ""
        wx  = r.get("wxString") or r.get("wx_string") or ""

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
    if cond is None:
        return COLOR_NODATA
    if cond.get("lightning") and lightning_on:
        return COLOR_LIGHTNING
    if has_high_wind(cond) and highwind_on:
        return COLOR_HIGHWIND
    return base_color(cond.get("flightCategory", ""))

def station_phase_offset(icao: str) -> float:
    if not icao:
        return 0.0
    return (sum(ord(c) for c in icao) & 255) / 256.0

def flashing_state(intensity: float, t_now: float, phase_offset: float) -> bool:
    """
    Returns True/False depending on intensity 0–1.
    intensity is the fraction of each 1s cycle that's 'on'.
    """
    if intensity <= 0.0:
        return False
    if intensity >= 1.0:
        return True
    phase = (t_now + phase_offset) % 1.0
    return phase < intensity

# -----------------------------
# Background fetcher (non-blocking)
# -----------------------------
_conds = {}
_conds_lock = threading.Lock()
_last_fetch = 0.0
_fetching = False

def _do_fetch():
    global _conds, _last_fetch, _fetching
    try:
        recs = fetch_metar_json_ids(STATION_IDS, LOOKBACK_HOURS)
        new_conds = conditions_from_json(recs)

        clear_terminal()
        print(f"[{dt.datetime.now():%H:%M}] Updated METARs ({len(new_conds)} stations)")

        missing = [a for a in AIRPORTS if a and a not in new_conds]
        if missing:
            print("No recent METAR for:", ", ".join(missing))

        hw = [a for a, c in new_conds.items() if has_high_wind(c)]
        lt = [a for a, c in new_conds.items() if c.get("lightning")]
        if hw: print("High winds at:", ", ".join(hw))
        if lt: print("Lightning reported at:", ", ".join(lt))

        with _conds_lock:
            _conds = new_conds  # ✅ swap in fresh data
        _last_fetch = time.time()
    except Exception as e:
        # ❌ keep previous data; just report and adjust retry timing
        print(f"[{dt.datetime.now():%H:%M}] API error (keeping previous data): {e}")
        _last_fetch = time.time() - (FETCH_EVERY_S - ERROR_RETRY_S)
    finally:
        _fetching = False

def trigger_fetch_if_needed(now: float):
    global _fetching, _last_fetch
    need_initial = False
    with _conds_lock:
        need_initial = not bool(_conds)
    if ((now - _last_fetch) >= FETCH_EVERY_S or need_initial) and not _fetching:
        _fetching = True
        threading.Thread(target=_do_fetch, daemon=True).start()

# -----------------------------
# Main Loop
# -----------------------------
def main():
    if len(AIRPORTS) != LED_COUNT:
        print(f"NOTE: AIRPORTS has {len(AIRPORTS)} entries but LED_COUNT={LED_COUNT}. Using smaller of the two.")
    usable_leds = min(len(AIRPORTS), LED_COUNT)

    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] Starting METAR Map — {len(STATION_IDS)} stations")
    print("Note: If LEDs freeze, ensure onboard audio is disabled (add 'dtparam=audio=off' in /boot/config.txt and reboot).")

    pixels = neopixel.NeoPixel(
        LED_PIN, LED_COUNT,
        brightness=LED_BRIGHT,
        pixel_order=LED_ORDER,
        auto_write=False
    )

    # Watchdog to detect render stalls
    FRAME_BUDGET = (1.0 / UPDATE_HZ) * 4
    last_tick = time.monotonic()

    while True:
        now = time.time()

        # Non-blocking fetch trigger
        trigger_fetch_if_needed(now)

        # Snapshot the latest conditions for this frame
        with _conds_lock:
            conds = _conds.copy()

        # Time base for flashing
        t_phase = time.monotonic() % 1.0

        # Render one frame
        for idx in range(usable_leds):
            icao = AIRPORTS[idx]
            c = conds.get(icao) if icao else None
            phase = station_phase_offset(icao)
            lightning_on = flashing_state(LIGHTNING_INTENSITY, t_phase, phase)
            highwind_on  = flashing_state(HIGHWIND_INTENSITY,  t_phase, phase)
            pixels[idx] = pick_color(c, lightning_on, highwind_on)

        # Optional heartbeat LED (diagnostic only)
        if HEARTBEAT_LED is not None and 0 <= HEARTBEAT_LED < LED_COUNT:
            beat = (time.monotonic() % 1.0) < 0.5   # 1 Hz blink, 50% duty
            pixels[HEARTBEAT_LED] = (20, 20, 20) if beat else (0, 0, 0)

        # Clear any remaining LEDs beyond mapping
        for idx in range(usable_leds, LED_COUNT):
            if idx != HEARTBEAT_LED:
                pixels[idx] = COLOR_CLEAR

        try:
            pixels.show()
        except Exception as e:
            print("LED driver error:", e)

        # Render stall watchdog
        now_tick = time.monotonic()
        dtick = now_tick - last_tick
        if dtick > FRAME_BUDGET:
            print(f"[dbg] render stall: {dtick:.3f}s")
        last_tick = now_tick

        time.sleep(SLEEP_S)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
