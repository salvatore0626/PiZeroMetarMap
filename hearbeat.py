import time, board, neopixel

LED_COUNT = 20
LED_PIN   = board.D18
LED_ORDER = neopixel.GRB
BRIGHT    = 0.2
HB_LED    = LED_COUNT - 1  # pick one LED

pixels = neopixel.NeoPixel(LED_PIN, LED_COUNT,
                           brightness=BRIGHT,
                           pixel_order=LED_ORDER,
                           auto_write=False)

t0 = time.monotonic()
on = False
while True:
    # blink 1 Hz, 50% duty
    if (time.monotonic() - t0) >= 0.5:
        on = not on
        t0 = time.monotonic()
    for i in range(LED_COUNT):
        pixels[i] = (0,0,0)
    pixels[HB_LED] = (20,20,20) if on else (0,0,0)
    pixels.show()
    time.sleep(0.02)