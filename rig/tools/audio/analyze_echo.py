import wave
import struct
import math
import sys

def get_rms(path):
    with wave.open(path, 'rb') as w:
        frames = w.readframes(w.getnframes())
        samples = struct.unpack(f'<{w.getnframes() * w.getnchannels()}h', frames)

        sum_sq = 0
        for s in samples:
            sum_sq += (s / 32768.0)**2

        rms = math.sqrt(sum_sq / len(samples))
        db = 20 * math.log10(rms) if rms > 0 else -100
        return db

if __name__ == "__main__":
    hfp_file = sys.argv[1]
    lark_file = sys.argv[2]

    hfp_db = get_rms(hfp_file)
    lark_db = get_rms(lark_file)

    print(f"HFP Downlink (Ref) RMS: {hfp_db:.2f} dBFS")
    print(f"Lark Mic (Capture) RMS: {lark_db:.2f} dBFS")
    print(f"Echo Coupling: {lark_db - hfp_db:.2f} dB")
