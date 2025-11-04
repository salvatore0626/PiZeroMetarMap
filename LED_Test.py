import board, neopixel, time
pixels = neopixel.NeoPixel(board.D18, 8, brightness=0.2, auto_write=False)
for i in range(8):
    pixels[i] = (255, 0, 0)
pixels.show()
time.sleep(2)
pixels.fill((0, 0, 0))
pixels.show()