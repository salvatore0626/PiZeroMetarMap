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

# ---- Animation behavior (wind/lightning) ----
ACTIVATE_WIND_ANIMATION      = True
ACTIVATE_LIGHTNING_ANIMATION = True
FADE_INSTEAD_OF_BLINK        = True     # wind: fade vs. hard blink
WIND_BLINK_SPEED_S           = 1.0      # base period for wind animation (per LED, de-synced)

# Lightning behavior
LIGHTNING_FADE_INTENSITY     = 0.35     # after white flash, blend back toward base at this ratio
LIGHTNING_FLASH_PERIOD_S     = 1.0      # per-LED lightning cycle (phase-shifted)

# ---- Refresh animation (separate & independent) ----
REFRESH_ANIMATION            = "fade"   # "fade" or "blink"
REFRESH_DISABLE_EFFECTS      = True     # during refresh, suppress wind/lightning
REFRESH_FADE_S               = 3.0      # used if REFRESH_ANIMATION == "fade"
REFRESH_BLINKS               = 2        # used if REFRESH_ANIMATION == "blink"
REFRESH_BLINK_PERIOD_S       = 0.25     # on/off cadence for refresh blinking

# --- Refresh “river” animation settings ---
REFRESH_FLOW_SPEED_S = 0.05   # per-LED fade time in the river
REFRESH_FADE_STEPS   = 20     # smoothness (higher = smoother)

# Refresh state flags
_refreshing = False
_refresh_stale = False

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

# ---------- utils ----------
def to_int(v, default=0):
    try: return int(round(float(str(v).replace('+', '').strip())))
    except: return default

def clear_terminal():
    os.system("cls" if os.name == "nt" else "clear")

def clamp01(x): return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x

def blend(c1, c2, alpha):
    a = clamp01(alpha)
    return (int(c1[0]*(1-a)+c2[0]*a),
            int(c1[1]*(1-a)+c2[1]*a),
            int(c1[2]*(1-a)+c2[2]*a))

def scale(c, alpha):
    a = clamp01(alpha)
    return (int(c[0]*a), int(c[1]*a), int(c[2]*a))

# Stable per-station pseudo-random (no global RNG needed for determinism)
def _hash01(s: str, mod: int = 997):
    if not s: return 0.0
    return ((sum(ord(c) for c in s) % mod) + 0.5) / mod

# ---------- METAR fetch/parse ----------
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
        if not icao: continue
        rt = r.get("reportTime")
        if rt:
            try: obs_dt = dt.datetime.fromisoformat(rt.replace("Z","+00:00"))
            except: obs_dt = dt.datetime.now(dt.timezone.utc)
        else:
            obs_dt = dt.datetime.now(dt.timezone.utc)
        if icao not in latest or obs_dt > latest[icao]["_dt"]:
            latest[icao] = {"r": r, "_dt": obs_dt}

    out = {}
    for icao, bundle in latest.items():
        r  = bundle["r"]
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

# ---------- color/animation ----------
def base_color(fc):
    if fc == "VFR":  return COLOR_VFR
    if fc == "MVFR": return COLOR_MVFR
    if fc == "IFR":  return COLOR_IFR
    if fc == "LIFR": return COLOR_LIFR
    return COLOR_CLEAR

def is_very_high_wind(cond):
    return max(cond.get("windSpeed", 0), cond.get("windGustSpeed", 0)) >= VERY_HIGH_WIND_YELLOW_KT

def wind_should_animate(cond):
    if not ACTIVATE_WIND_ANIMATION: return False
    if ALWAYS_ANIMATE_FOR_GUSTS and cond.get("windGustSpeed", 0) > 0: return True
    return max(cond.get("windSpeed", 0), cond.get("windGustSpeed", 0)) >= WIND_ANIM_THRESHOLD_KT

def wind_blink_on(t, icao):
    # independent phase per station
    phase = (t + _hash01(icao, 991)*WIND_BLINK_SPEED_S) % (2*WIND_BLINK_SPEED_S)
    return (phase < WIND_BLINK_SPEED_S)

def lightning_gate_and_fade(t, icao):
    """
    Returns (flash_on, fade_alpha) for lightning.
    - flash_on: brief white flash window
    - fade_alpha: when not in flash window, amount to blend white→base (0=no lightning)
    Period is per-station, phase-shifted by ICAO; short flash at cycle start.
    """
    period = max(0.2, LIGHTNING_FLASH_PERIOD_S)
    start_offset = _hash01(icao[::-1], 953) * period
    x = (t + start_offset) % period
    flash_window = 0.08  # 80 ms white pop (visual; adjust if you like)
    if x < flash_window:
        return True, 0.0
    # after flash window, apply one step of quick fade if lightning present
    return False, LIGHTNING_FADE_INTENSITY

def pick_color_for_station(cond, tnow, icao):
    """
    Priority (outside refresh):
      Lightning flash/fade (if reported) >
      Very high wind (solid yellow) >
      Wind animation (yellow over base) >
      Base color.
    """
    if cond is None:
        return COLOR_NODATA

    base = base_color(cond.get("flightCategory", ""))

    if ACTIVATE_LIGHTNING_ANIMATION and cond.get("lightning", False):
        flash_on, fade_alpha = lightning_gate_and_fade(tnow, icao)
        if flash_on:
            return COLOR_LIGHTNING
        if fade_alpha > 0.0:
            return blend(COLOR_LIGHTNING, base, fade_alpha)

    if is_very_high_wind(cond):
        return COLOR_HIGHWIND

    if wind_should_animate(cond):
        on = wind_blink_on(tnow, icao)
        if FADE_INSTEAD_OF_BLINK:
            return blend(base, COLOR_HIGHWIND, 1.0 if on else 0.0)
        else:
            return COLOR_HIGHWIND if on else base

    return base

def run_refresh_animation(pixels, conds, stale=False):
    """
    River fade: turn all LEDs OFF, then fade them in one-by-one (0..N-1).
    If 'stale' is True (no new data), fade to COLOR_NODATA instead of base weather color.
    """
    usable_leds = min(len(AIRPORTS), LED_COUNT)

    # 1) All off
    for i in range(LED_COUNT):
        pixels[i] = COLOR_CLEAR
    pixels.show()

    # 2) Targets for each mapped LED
    targets = []
    for idx in range(usable_leds):
        icao = AIRPORTS[idx]
        cond = conds.get(icao) if icao else None
        if stale:
            targets.append(COLOR_NODATA)
        else:
            if cond is None:
                targets.append(COLOR_NODATA)
            else:
                targets.append(base_color(cond.get("flightCategory", "")))

    # 3) Sequential fade (OFF -> target)
    step_sleep = REFRESH_FLOW_SPEED_S / max(1, REFRESH_FADE_STEPS)
    for idx in range(usable_leds):
        tgt = targets[idx]
        for s in range(1, REFRESH_FADE_STEPS + 1):
            a = s / REFRESH_FADE_STEPS
            pixels[idx] = (int(tgt[0]*a), int(tgt[1]*a), int(tgt[2]*a))
            pixels.show()
            time.sleep(step_sleep)

    # 4) Any extra LEDs beyond mapping -> off
    for idx in range(usable_leds, LED_COUNT):
        pixels[idx] = COLOR_CLEAR
    pixels.show()

# ---------- data / refresh state ----------
_conds = {}
_conds_lock = threading.Lock()
_last_fetch = 0.0
_fetching = False

_refresh_request = False
_refresh_t0 = None

def _do_fetch():
    global _conds, _last_fetch, _fetching, _refreshing, _refresh_stale
    try:
        # Keep a snapshot for change detection
        with _conds_lock:
            prev_conds = _conds.copy()

        recs = fetch_metar_json_ids(STATION_IDS, LOOKBACK_HOURS)
        new_conds = conditions_from_json(recs)

        # CLI summary
        clear_terminal()
        print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] Updated METARs ({len(new_conds)} stations)")
        missing   = [a for a in AIRPORTS if a and a not in new_conds]
        lightning = sorted([k for k, v in new_conds.items() if v.get("lightning")])
        highwinds = sorted([k for k, v in new_conds.items()
                            if max(v.get('windSpeed',0), v.get('windGustSpeed',0)) >= VERY_HIGH_WIND_YELLOW_KT])
        if missing:   print("No Recent Data:", " ".join(sorted(missing)))
        if lightning: print("Lightning:", " ".join(lightning))
        if highwinds: print("High Winds:", " ".join(highwinds))

        # Simple signature to detect “meaningful” changes
        def _sig(d):
            return sorted(
                (k,
                 d[k].get("flightCategory"),
                 d[k].get("windSpeed"), d[k].get("windGustSpeed"),
                 bool(d[k].get("lightning")),
                 str(d[k].get("obsTime")))
                for k in d.keys()
            )

        changed = _sig(prev_conds) != _sig(new_conds)

        # Commit new data
        with _conds_lock:
            _conds = new_conds
        _last_fetch = time.time()

        # Trigger river refresh
        _refresh_stale = (not changed)   # stale => dim gray river
        _refreshing = True

    except Exception as e:
        print(f"Fetch error (keeping previous data): {e}")
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
    global _refreshing, _refresh_stale

    usable_leds = min(len(AIRPORTS), LED_COUNT)
    pixels = neopixel.NeoPixel(
        LED_PIN, LED_COUNT, brightness=LED_BRIGHTNESS,
        pixel_order=LED_ORDER, auto_write=False
    )

    did_startup_refresh = False

    while True:
        now = time.time()
        trigger_fetch_if_needed(now)

        # Snapshot current conditions
        with _conds_lock:
            conds = _conds.copy()

        # 1) First-time river after initial data is available
        if not did_startup_refresh and conds:
            run_refresh_animation(pixels, conds, stale=False)
            did_startup_refresh = True

        # 2) River after each fetch completes
        if _refreshing:
            run_refresh_animation(pixels, conds, stale=_refresh_stale)
            _refreshing = False

        # 3) Normal animation frame
        tnow = time.monotonic()
        for idx in range(usable_leds):
            icao = AIRPORTS[idx]
            cond = conds.get(icao) if icao else None
            pixels[idx] = pick_color_for_station(cond, tnow, icao)

        for idx in range(usable_leds, LED_COUNT):
            pixels[idx] = COLOR_CLEAR

        pixels.show()
        time.sleep(0.05)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)