# PiZeroMetarMap


#######################################################################################
########################### METARmap Configuration Overview ###########################
#######################################################################################

## LED Hardware
| Variable | Description |
|-----------|-------------|
| `LED_COUNT` | Total number of LEDs connected to your strip. |
| `LED_PIN` | GPIO pin used for LED data (commonly `board.D18` for WS2812). |
| `LED_ORDER` | Color order of the LED strip (usually `neopixel.GRB` or `neopixel.RGB`). |
| `LED_BRIGHTNESS` | Overall LED brightness (range 0.0–1.0). Controls light intensity. |

## Animation Settings
| Variable | Description |
|-----------|-------------|
| `ACTIVATE_WIND_ANIMATION` | Enables or disables wind animations for windy stations. |
| `ACTIVATE_LIGHTNING_ANIMATION` | Enables or disables lightning flash animations. |
| `FADE_INSTEAD_OF_BLINK` | Switches between smooth fading or sharp blinking. |
| `BLINK_SPEED_S` | Controls the blink or fade rate for animations (in seconds). |
| `RANDOMIZE_PHASES` | Randomizes animation timing so LEDs are not synchronized. |

## Wind Animation
| Variable | Description |
|-----------|-------------|
| `WIND_ANIM_THRESHOLD_KT` | Minimum steady wind speed (in knots) that triggers wind animation. |
| `ALWAYS_ANIMATE_FOR_GUSTS` | If true, any gust triggers animation even below the steady wind threshold. |
| `VERY_HIGH_WIND_YELLOW_KT` | When exceeded, LED turns solid yellow instead of animating. |

## Lightning Animation
| Variable | Description |
|-----------|-------------|
| `LIGHTNING_FADE_INTENSITY` | Strength of fade transition back to base color after a lightning flash. |
| `LIGHTNING_FLASH_MS` | Duration of each white lightning flash (in milliseconds). |

## Refresh Animation
| Variable | Description |
|-----------|-------------|
| `REFRESH_FADE_S` | Duration of fade from dark to full brightness when data updates. |
| `REFRESH_DISABLE_EFFECTS` | If true, disables wind/lightning effects during refresh fade for a clean transition. |
| `REFRESH_ANIMATION` | Type of refresh effect (`"fade"` for smooth, `"blink"` for quick flashes). |
| `REFRESH_BLINKS` | Number of on/off flashes when using blink-style refresh animation. |

## Data Fetch Settings
| Variable | Description |
|-----------|-------------|
| `FETCH_EVERY_S` | Interval between METAR data updates (in seconds). |
| `ERROR_RETRY_S` | Delay before retrying after a failed API request. |
| `LOOKBACK_HOURS` | Number of past hours of METAR data to request. |
| `API_BASE` | Base URL used to fetch METAR data. |
| `USER_AGENT` | Custom user agent string for API identification. |
| `NETWORK_TIMEOUT_S` | Maximum allowed time for a network request (in seconds). |

## LED–Airport Mapping
| Variable | Description |
|-----------|-------------|
| `AIRPORTS` | Ordered list of ICAO codes. Each entry corresponds to one LED position. |

## Color Settings
| Variable | Description |
|-----------|-------------|
| `COLOR_VFR` | Color used for VFR (Visual Flight Rules) conditions. |
| `COLOR_MVFR` | Color used for MVFR (Marginal VFR) conditions. |
| `COLOR_IFR` | Color used for IFR (Instrument Flight Rules) conditions. |
| `COLOR_LIFR` | Color used for LIFR (Low IFR) conditions. |
| `COLOR_CLEAR` | LED color when no condition applies (off/black). |
| `COLOR_LIGHTNING` | Color used for lightning flashes. |
| `COLOR_HIGHWIND` | Color used for high-wind animations. |
| `COLOR_NODATA` | Color shown when no recent METAR data is available. |

#######################################################################################
############################### Set up Automated Startup ##############################
#######################################################################################
1️⃣ Create a service file
sudo nano /etc/systemd/system/metarmap.service

#Paste this inside:

[Unit]
Description=METAR Map LED Service
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/METARmap.py  #change to path of python code
WorkingDirectory=/home/pi                        #change to /home/username
Restart=always          
RestartSec=10
User=pi

[Install]
WantedBy=multi-user.target


2️⃣ Enable and start the service
sudo systemctl daemon-reload
sudo systemctl enable metarmap.service
sudo systemctl start metarmap.service

3️⃣ Check that it’s running
systemctl status metarmap.service



Check logs with
journalctl -u metarmap.service -f


