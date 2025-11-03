#!/usr/bin/env python3
import sys, time, json, socket, urllib.parse, urllib.request, os, threading, random
import datetime as dt
import board
import neopixel

# ============================================================
# ===================== USER SETTINGS ========================
# ============================================================

LED_COUNT      = 20
LED_PIN        = board.D18
LED_ORDER      = neopixel.GRB
LED_BRIGHTNESS = 0.6

# Animation behavior
ACTIVATE_WIND_ANIMATION      = True
ACTIVATE_LIGHTNING_ANIMATION = True
FADE_INSTEAD_OF_BLINK        = True
BLINK_SPEED_S                = 1.0
RANDOM_ANIMATION_PHASES      = True   # Each LED blinks/fades at random timing

# Lightning fade intensity (lower = faster fade)
LIGHTNING_FADE_INTENSITY     = 0.35

# Fade-in on new data/startup
REFRESH_FADE_S               = 1.0    # seconds to fade from OFF -> new colors

# Wind thresholds
WIND_ANIM_THRESHOLD_KT       = 25
ALWAYS_ANIMATE_FOR_GUSTS     = False
VERY_HIGH_WIND_YELLOW_KT     = 35

# Data fetch
FETCH_EVERY_S   = 600
ERROR_RETRY_S   = 60
LOOKBACK_HOURS  = 24
API_BASE        = "https://aviationweather.gov"
USER_AGENT      = "METARMap/2.0"
NETWORK_TIMEOUT_S = 10

# LED → Airport mapping
AIRPORTS = [
    "KRBG", "K77S", "KEUG", "KCVO", "KSLE",
    "KMMV", "KUAO", "KHIO", "KTTD", "KPDX",
    "KVUO", "KSPB", "KKLS", "K4S2", "KDLS",
    "KS33", "KS39", "KRDM", "KBDN", "KS21",
]

# Colors
COLOR_VFR       = (0, 255, 0)
COLOR_MVFR      = (0, 0, 255)
COLOR_IFR       = (255, 0, 0)
COLOR_LIFR      = (255, 0, 255)
COLOR_CLEAR     = (0, 0, 0)
COLOR_LIGHTNING = (255, 255, 255)
COLOR_HIGHWIND  = (255, 255, 0)
COLOR_NODATA    = (5, 5, 5)

# ============================================================
# ===================== IMPLEMENTATION =======================
# ============================================================

socket.setdefaulttimeout(NETWORK_TIMEOUT_S)
STATION_IDS = [a.strip().upper() for a in AIRPORTS if a]

def to_int(v, default=0):
    try: return int(round(float(str(v).replace('+', '').strip())))
    except: return default

def clear_terminal():
    os.system("cls" if os.name == "nt" else "clear")

def blend(c1, c2, alpha):
    a = max(0.0, min(1.0, float(alpha)))
    return (int(c1[0]*(1-a)+c2[0]*a),
            int(c1[1]*(1-a)+c2[1]*a),
            int(c1[2]*(1-a)+c2[2]*a))

def scale(c, alpha):
    a = max(0.0, min(1.0, float(alpha)))
    return (int(c[0]*a), int(c[1]*a), int(c[2]*a))

# -------- Fetch / Parse METAR --------
def fetch_bytes(url, tries=3, backoff=1.5):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req) as r:
                if r.status == 204:
                    return b"[]"
                return r.read()
        except Exception as e:
            last = e
            time.sleep(backoff); backoff *= 1.5
    if last: raise last

def fetch_metar_json_ids(stations, hours, chunk_size=150):
    ids = sorted({s.strip().upper() for s in stations if s})
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
            obs_dt = dt.datetime.now(dt.timezone.utc)
        if icao not in latest or obs_dt > latest[icao]["_dt"]:
            latest[icao] = {"r": r, "_dt": obs_dt}

    out = {}
    for icao, bundle in latest.items():
        r = bundle["r"]
        fc = (r.get("fltCat") or r.get("flight_category") or "").strip().upper()
        wspd = to_int(r.get("wspd") or r.get("windSpeedKt"))
        wgst = to_int(r.get("wgst") or r.get("gust") or r.get("windGustKt"))
        raw  = r.get("rawOb") or r.get("raw_text") or ""
        body = raw.split(" RMK", 1)[0]
        lightning = (("LTG" in body) or (" TS" in body)) and (" TSNO" not in raw)
        out[icao] = {
            "flightCategory": fc,
            "windSpeed": wspd,
            "windGustSpeed": wgst,
            "lightning": lightning,
            "obsTime": bundle["_dt"],
        }
    return out

# -------- Color / Animation Logic --------
def base_color(fc):
    if fc == "VFR":  return COLOR_VFR
    if fc == "MVFR": return COLOR_MVFR
    if fc == "IFR":  return COLOR_IFR
    if fc == "LIFR": return COLOR_LIFR
    return COLOR_CLEAR

def is_very_high_wind(cond):
    return max(cond.get("windSpeed", 0), cond.get("windGustSpeed", 0)) >= VERY_HIGH_WIND_YELLOW_KT

def wind_should_animate(cond):
    if not ACTIVATE_WIND_ANIMATION:
        return False
    if ALWAYS_ANIMATE_FOR_GUSTS and cond.get("windGustSpeed", 0) > 0:
        return True
    return max(cond.get("windSpeed", 0), cond.get("windGustSpeed", 0)) >= WIND_ANIM_THRESHOLD_KT

def pick_color_for_station(cond, blink_on):
    if cond is None:
        return COLOR_NODATA
    base = base_color(cond.get("flightCategory", ""))
    # Lightning flash behavior — flash white then fade back fast
    if ACTIVATE_LIGHTNING_ANIMATION and cond.get("lightning", False):
        if blink_on:
            return COLOR_LIGHTNING
        else:
            return blend(COLOR_LIGHTNING, base, LIGHTNING_FADE_INTENSITY)
    if is_very_high_wind(cond):
        return COLOR_HIGHWIND
    if wind_should_animate(cond):
        if FADE_INSTEAD_OF_BLINK:
            return blend(base, COLOR_HIGHWIND, 1.0 if blink_on else 0.0)
        else:
            return COLOR_HIGHWIND if blink_on else base
    return base

# -------- Background Fetcher + Refresh Trigger --------
_conds = {}
_conds_lock = threading.Lock()
_last_fetch = 0.0
_fetching = False

# refresh state
_refresh_request = False
_refresh_t0 = None

def _do_fetch():
    global _conds, _last_fetch, _fetching, _refresh_request
    try:
        recs = fetch_metar_json_ids(STATION_IDS, LOOKBACK_HOURS)
        new_conds = conditions_from_json(recs)
        clear_terminal()
        print(f"[{dt.datetime.now():%H:%M}] Updated METARs ({len(new_conds)} stations)")
        missing = [a for a in AIRPORTS if a and a not in new_conds]
        lightning = [k for k, v in new_conds.items() if v.get("lightning")]
        highwinds = [k for k, v in new_conds.items() if is_very_high_wind(v)]
        if missing:   print("No Recent Data:", ", ".join(missing))
        if lightning: print("Lightning:", ", ".join(lightning))
        if highwinds: print("High Winds:", ", ".join(highwinds))
        with _conds_lock:
            _conds = new_conds
        _last_fetch = time.time()
        _refresh_request = True   # <-- ask main loop to run a fade-in
    except Exception as e:
        print(f"Fetch error: {e}")
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
# =========================== MAIN ===========================
# ============================================================
def main():
    global _refresh_request, _refresh_t0

    usable_leds = min(len(AIRPORTS), LED_COUNT)
    pixels = neopixel.NeoPixel(
        LED_PIN, LED_COUNT, brightness=LED_BRIGHTNESS,
        pixel_order=LED_ORDER, auto_write=False
    )
    phases = [random.random() * BLINK_SPEED_S for _ in range(LED_COUNT)] if RANDOM_ANIMATION_PHASES else [0]*LED_COUNT

    # Trigger a refresh fade on the very first successful fetch
    _refresh_t0 = None  # not active until _refresh_request is set by fetcher

    while True:
        now = time.time()
        trigger_fetch_if_needed(now)

        # Activate refresh fade if requested
        if _refresh_request:
            _refresh_t0 = time.monotonic()
            _refresh_request = False

        with _conds_lock:
            conds = _conds.copy()

        tnow = time.monotonic()
        # compute alpha for refresh fade (0..1), or None if not in fade window
        if _refresh_t0 is not None:
            elapsed = tnow - _refresh_t0
            if elapsed < REFRESH_FADE_S:
                refresh_alpha = elapsed / max(0.001, REFRESH_FADE_S)
            else:
                refresh_alpha = None
                _refresh_t0 = None
        else:
            refresh_alpha = None

        for idx in range(usable_leds):
            icao = AIRPORTS[idx]
            cond = conds.get(icao)

            if RANDOM_ANIMATION_PHASES:
                phase = (tnow + phases[idx]) % (2 * BLINK_SPEED_S)
                blink_on = phase < BLINK_SPEED_S
            else:
                blink_on = (int(tnow / BLINK_SPEED_S) % 2 == 0)

            color = pick_color_for_station(cond, blink_on)
            # During refresh, fade from OFF -> target color
            if refresh_alpha is not None:
                color = scale(color, refresh_alpha)

            pixels[idx] = color

        for idx in range(usable_leds, LED_COUNT):
            pixels[idx] = COLOR_CLEAR

        pixels.show()
        time.sleep(0.05)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)