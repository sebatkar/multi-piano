#!/usr/bin/env python3
"""Entry point for Multi-Piano."""
import os
import sys

SOUNDS_DIR = os.path.join(os.path.dirname(__file__), 'sounds')


def ensure_sounds() -> None:
    note_files = [
        'C4', 'Cs4', 'D4', 'Ds4', 'E4', 'F4',
        'Fs4', 'G4', 'Gs4', 'A4', 'As4', 'B4', 'C5',
    ]
    missing = [f for f in note_files
               if not os.path.exists(os.path.join(SOUNDS_DIR, f'{f}.wav'))]
    if missing:
        print('Generating piano sounds (first run)...')
        from generate_sounds import generate_all
        generate_all(SOUNDS_DIR)


def main() -> None:
    ensure_sounds()
    from piano_ui import MultiPianoApp
    app = MultiPianoApp(sounds_dir=SOUNDS_DIR)
    app.run()


if __name__ == '__main__':
    main()
