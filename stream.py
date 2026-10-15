"""
stream.py - play the audio and stream the R,G,B,Master curve to the ESP32.

Usage:
  python stream.py test COM3            # wiring/colour test (white, red, green, blue sweeps)
  python stream.py yourfile.mp3 COM3    # play song + lights (needs curve.npy from analyze.py)
"""
import sys
import time
import numpy as np
import serial
import librosa
import sounddevice as sd

FPS = 40
LIGHT_DELAY = 0.0   # seconds. Positive = lights later (use if your speaker lags,
                    # e.g. Bluetooth). Negative = lights earlier.
HEADER = 255


def send(ser, r, g, b, m):
    ser.write(bytes([HEADER, int(r), int(g), int(b), int(m)]))


def main():
    target, port = sys.argv[1], sys.argv[2]
    ser = serial.Serial(port, 115200)
    time.sleep(2)                    # ESP32 resets when the port opens

    try:
        if target == "test":
            for r, g, b in [(254, 254, 254), (254, 0, 0), (0, 254, 0), (0, 0, 254)]:
                for m in list(range(0, 255, 3)) + list(range(254, -1, -3)):
                    send(ser, r, g, b, m)
                    time.sleep(0.01)
            return

        curve = np.load("curve.npy")
        data, sr = librosa.load(target, sr=None, mono=False)
        if data.ndim == 2:
            data = data.T            # sounddevice wants (samples, channels)

        send(ser, 0, 0, 0, 0)
        sd.play(data, sr)
        t0 = time.perf_counter()
        last = -1
        while True:
            i = int((time.perf_counter() - t0 - LIGHT_DELAY) * FPS)
            if i >= len(curve):
                break
            if i != last and i >= 0:
                send(ser, *curve[i])
                last = i
            time.sleep(0.002)
        sd.wait()
    except KeyboardInterrupt:
        sd.stop()
    finally:
        send(ser, 0, 0, 0, 0)
        ser.close()


if __name__ == "__main__":
    main()
