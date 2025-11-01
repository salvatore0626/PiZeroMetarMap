#!/usr/bin/env python3
import sys, time, json, socket, urllib.parse, urllib.request, os, threading
import datetime as dt
import board
import neopixel

# ============================================================
# =============== SETTINGS — EDIT THESE ONLY =================
# ============================================================

# LED hardware
LED_COUNT      = 20
LED_PIN        = board.D18           # same as old script (GPIO18 / PWM)
LED_ORDER      = neopixel.GRB
LED_BRIGHTNESS = 0.6

# --- Animation style (match metar.py behavior) ---
ACTIVATE_WIND_ANIMATION      = True   # True = animate winds; False = static
ACTIVATE_LIGHTNING_ANIMATION = True   # True = animate lightning; False = static
FADE_INSTEAD_OF_BLINK        = True   # True = fade/blend; False = on/off blink
BLINK_SPEED_S                = 1.0    # seconds between animation toggles (≈1 Hz)

# Wind thresholds (similar semantics to your metar.py)
WIND_ANIM_THRESHOLD_KT       = 25     # >= this → animate (blink/fade) for wind
ALWAYS_ANIMATE_FOR_GUSTS     = False  # if True, any gust animates regardless of speed
VERY_HIGH_WIND_YELLOW_KT     = 35     # >= this → solid yellow (set -1 to disable)

# Data fetch
FETCH_EVERY_S   = 600                # normal interval (10 min)
ERROR_RETRY_S   = 60                 # retry sooner after an error
LOOKBACK_HOURS  = 5
API_BASE        = "https://aviationweather.gov"
USER_AGENT      = "METARMap/2.0 (+contact@example.com)"
NETWORK_TIMEOUT_S = 10

# LED -> Airport mapping
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

# -------- Utilities --------
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

def blend(c1, c2, alpha):
    """Linear blend between two RGB tuples. alpha in [0..1]."""
    a = max(0.0, min(1.0, float(alpha)))
    return (int(c1[0]*(1-a)+c2[0]*a), int(c1[1]*(1-a)+c2[1]*a), int(c1[2]*(1-a)+c2[2]*a))

# -------- Fetch / parse METAR (JSON endpoint) --------
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
    """
    Return dict[ICAO] -> condition dict (latest per station).
    Keys used: flightCategory, windSpeed, windGustSpeed, lightning, obsTime
    """
    latest = {}
    for r in records:
        icao = (r.get("icaoId") or r.get("station") or r.get("station_id") or "").strip().upper()
        if not icao: continue

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
        r     = bundle["r"]
        fc    = (r.get("fltCat") or r.get("flight_category") or "").strip().upper()
        wspd  = to_int(r.get("wspd") or r.get("windSpeedKt"))
        wgst  = to_int(
            r.get("wgst") or r.get("gust") or r.get("gustKt") or
            r.get("windGustKt") or r.get("wind_gust_kt") or r.get("gust_kts")
        )
        raw   = r.get("rawOb") or r.get("raw_text") or ""
        body  = raw.split(" RMK", 1)[0]
        lightning = (("LTG" in body) or (" TS" in body)) and (" TSNO" not in raw)

        out[icao] = {
            "flightCategory": fc,
            "windSpeed": wspd,
            "windGustSpeed": wgst,
            "lightning": lightning,
            "obsTime": bundle["_dt"],
        }
    return out

# -------- Color logic like metar.py --------
def base_color(fc):
    if fc == "VFR":  return COLOR_VFR
    if fc == "MVFR": return COLOR_MVFR
    if fc == "IFR":  return COLOR_IFR
    if fc == "LIFR": return COLOR_LIFR
    return COLOR_CLEAR

def is_very_high_wind(cond):
    if VERY_HIGH_WIND_YELLOW_KT < 0:
        return False
    return max(cond.get("windSpeed", 0), cond.get("windGustSpeed", 0)) >= VERY_HIGH_WIND_YELLOW_KT

def wind_should_animate(cond):
    if not ACTIVATE_WIND_ANIMATION:
        return False
    if ALWAYS_ANIMATE_FOR_GUSTS and cond.get("windGustSpeed", 0) > 0:
        return True
    return max(cond.get("windSpeed", 0), cond.get("windGustSpeed", 0)) >= WIND_ANIM_THRESHOLD_KT

def pick_color_for_station(cond, blink_on):
    """metar.py priority: Lightning flash > High-wind yellow (optional solid) > Base category."""
    if cond is None:
        return COLOR_NODATA

    base = base_color(cond.get("flightCategory", ""))

    # Lightning animation
    if ACTIVATE_LIGHTNING_ANIMATION and cond.get("lightning", False):
        if FADE_INSTEAD_OF_BLINK:
            return blend(base, COLOR_LIGHTNING, 1.0 if blink_on else 0.0)
        else:
            return COLOR_LIGHTNING if blink_on else base

    # Very high wind → solid yellow (overrides animation)
    if is_very_high_wind(cond):
        return COLOR_HIGHWIND

    # Normal wind animation (blink/fade to yellow)
    if wind_should_animate(cond):
        if FADE_INSTEAD_OF_BLINK:
            return blend(base, COLOR_HIGHWIND, 1.0 if blink_on else 0.0)
        else:
            return COLOR_HIGHWIND if blink_on else base

    # Otherwise, static base color
    return base

# -------- Background fetcher (non-blocking, keeps old data on error) --------
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

        with _conds_lock:
            _conds = new_conds        # ✅ swap in fresh data
        _last_fetch = time.time()
    except Exception as e:
        # ❌ keep old _conds; schedule earlier retry
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

# ============================================================
# Main (metar.py-style animation cadence)
# ============================================================
def main():
    if len(AIRPORTS) != LED_COUNT:
        print(f"NOTE: AIRPORTS has {len(AIRPORTS)} entries but LED_COUNT={LED_COUNT}. Using smaller of the two.")
    usable_leds = min(len(AIRPORTS), LED_COUNT)

    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] Starting METAR Map — {len(STATION_IDS)} stations")
    print("Hint: if LEDs freeze on GPIO18, disable HDMI audio or use SPI driver.")

    pixels = neopixel.NeoPixel(
        LED_PIN, LED_COUNT,
        brightness=LED_BRIGHTNESS,
        pixel_order=LED_ORDER,
        auto_write=False
    )

    # metar.py-style toggling: simple 1 Hz blink/fade gate
    blink_on = False
    last_toggle = time.monotonic()

    while True:
        now = time.time()
        trigger_fetch_if_needed(now)

        # Toggle blink/fade at BLINK_SPEED_S (≈1 Hz)
        tnow = time.monotonic()
        if (tnow - last_toggle) >= BLINK_SPEED_S:
            blink_on = not blink_on
            last_toggle = tnow

        # Snapshot conditions
        with _conds_lock:
            conds = _conds.copy()

        # Render one frame (no per-station phase math; match metar.py feel)
        for idx in range(usable_leds):
            icao = AIRPORTS[idx]
            c = conds.get(icao) if icao else None
            pixels[idx] = pick_color_for_station(c, blink_on)

        # Any extra LEDs beyond mapping → off
        for idx in range(usable_leds, LED_COUNT):
            pixels[idx] = COLOR_CLEAR

        try:
            pixels.show()
        except Exception as e:
            print("LED driver error:", e)

        # Sleep a modest amount; metar.py used ~1 Hz cadence.
        # We’ll do ~10 fps so fades look smooth when FADE_INSTEAD_OF_BLINK=True.
        time.sleep(0.1 if FADE_INSTEAD_OF_BLINK else 0.05)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
