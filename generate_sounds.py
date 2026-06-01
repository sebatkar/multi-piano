"""Generate piano note WAV files using sine wave harmonics + ADSR envelope."""
import math
import os
import struct
import wave

SAMPLE_RATE = 44100
DURATION = 3.0

NOTES = {
    'C4':  261.63,
    'Cs4': 277.18,
    'D4':  293.66,
    'Ds4': 311.13,
    'E4':  329.63,
    'F4':  349.23,
    'Fs4': 369.99,
    'G4':  392.00,
    'Gs4': 415.30,
    'A4':  440.00,
    'As4': 466.16,
    'B4':  493.88,
    'C5':  523.25,
}

HARMONICS = [(1, 1.0), (2, 0.6), (3, 0.3), (4, 0.15), (5, 0.08), (6, 0.04)]
HARMONIC_SUM = sum(a for _, a in HARMONICS)

ATTACK  = int(0.008 * SAMPLE_RATE)   # 8 ms
DECAY   = int(0.15  * SAMPLE_RATE)   # 150 ms
SUSTAIN = 0.45
RELEASE = int(0.6   * SAMPLE_RATE)   # 600 ms


def _envelope(i: int, n_samples: int) -> float:
    release_start = n_samples - RELEASE
    if i < ATTACK:
        return i / ATTACK
    if i < ATTACK + DECAY:
        t = (i - ATTACK) / DECAY
        return 1.0 - (1.0 - SUSTAIN) * t
    if i >= release_start:
        t = (i - release_start) / RELEASE
        return SUSTAIN * (1.0 - t)
    return SUSTAIN


def generate_note(freq: float) -> list[int]:
    n = int(SAMPLE_RATE * DURATION)
    out = []
    for i in range(n):
        t = i / SAMPLE_RATE
        env = _envelope(i, n)
        sample = sum(a * math.sin(2 * math.pi * freq * h * t) for h, a in HARMONICS)
        sample = sample * env / HARMONIC_SUM
        out.append(int(sample * 32767 * 0.85))
    return out


def write_wav(path: str, samples: list[int]) -> None:
    with wave.open(path, 'w') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        data = struct.pack(f'<{len(samples)}h',
                           *(max(-32768, min(32767, s)) for s in samples))
        f.writeframes(data)


def generate_all(sounds_dir: str = 'sounds') -> None:
    os.makedirs(sounds_dir, exist_ok=True)
    for name, freq in NOTES.items():
        path = os.path.join(sounds_dir, f'{name}.wav')
        print(f'  {name:5s} {freq:7.2f} Hz  -> {path}')
        write_wav(path, generate_note(freq))
    print('Done.')


if __name__ == '__main__':
    print('Generating piano notes...')
    generate_all()
