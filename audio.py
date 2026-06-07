"""Audio engine (pygame.mixer) and jitter buffer for remote note playback."""
import os
import time
import threading
from queue import PriorityQueue, Empty

os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')

import pygame

PLAYOUT_DELAY = 0.030  # 30 ms
STUCK_NOTE_TIMEOUT = 3.0  # force-stop notes held > 3 s

NOTE_NAMES = ['C4', 'Cs4', 'D4', 'Ds4', 'E4', 'F4', 'Fs4', 'G4', 'Gs4', 'A4', 'As4', 'B4', 'C5']


class AudioEngine:
    def __init__(self, sounds_dir: str = 'sounds'):
        self._sounds_dir = sounds_dir
        self._sounds: dict[int, pygame.mixer.Sound] = {}
        self._channels: dict[int, pygame.mixer.Channel] = {}
        self._press_times: dict[int, float] = {}
        self._lock = threading.Lock()
        self._running = False
        self.available = False  # False when mixer could not be initialised

    def init(self) -> None:
        try:
            pygame.mixer.pre_init(frequency=44100, size=-16, channels=1, buffer=512)
            pygame.mixer.init()
            pygame.mixer.set_num_channels(32)
            self._load_sounds()
            self.available = True
            self._running = True
            threading.Thread(target=self._watchdog, daemon=True).start()
        except Exception as exc:
            print(f'[audio] mixer unavailable: {exc} — running without sound')

    def _load_sounds(self) -> None:
        for i, name in enumerate(NOTE_NAMES):
            path = os.path.join(self._sounds_dir, f'{name}.wav')
            if os.path.exists(path):
                snd = pygame.mixer.Sound(path)
                snd.set_volume(0.7)
                self._sounds[i] = snd

    def play_note(self, note_id: int, velocity: int = 100) -> None:
        if not self.available or note_id not in self._sounds:
            return
        vol = min(1.0, velocity / 100.0) * 0.8
        channel = pygame.mixer.find_channel(True)
        if channel is None:
            return
        snd = self._sounds[note_id]
        snd.set_volume(vol)
        channel.play(snd)
        with self._lock:
            self._channels[note_id] = channel
            self._press_times[note_id] = time.time()

    def stop_note(self, note_id: int) -> None:
        if not self.available:
            return
        with self._lock:
            ch = self._channels.pop(note_id, None)
            self._press_times.pop(note_id, None)
        if ch:
            ch.fadeout(200)

    def stop_all(self) -> None:
        if not self.available:
            return
        pygame.mixer.fadeout(300)
        with self._lock:
            self._channels.clear()
            self._press_times.clear()

    def _watchdog(self) -> None:
        while self._running:
            now = time.time()
            with self._lock:
                stuck = [nid for nid, t in self._press_times.items()
                         if now - t > STUCK_NOTE_TIMEOUT]
            for nid in stuck:
                self.stop_note(nid)
            time.sleep(0.5)

    def quit(self) -> None:
        self._running = False
        if self.available:
            try:
                pygame.mixer.quit()
            except Exception:
                pass


class JitterBuffer:
    """Priority-queue buffer that delays remote note events by PLAYOUT_DELAY."""

    def __init__(self, audio: AudioEngine, playout_delay: float = PLAYOUT_DELAY):
        self._audio = audio
        self._delay = playout_delay
        self._queue: PriorityQueue = PriorityQueue()
        self._seen: set[tuple[str, int]] = set()
        self._running = False

    def start(self) -> None:
        self._running = True
        threading.Thread(target=self._consumer, daemon=True).start()

    def stop(self) -> None:
        self._running = False

    def push(self, seq: int, _timestamp: float,
             note_id: int, action: int, velocity: int,
             sender_id: str = '') -> None:
        key = (sender_id, seq)
        if key in self._seen:
            return  # duplicate
        # Remote wall clocks are not synchronized, so schedule from local arrival time.
        playout_at = time.monotonic() + self._delay
        self._queue.put((playout_at, key, note_id, action, velocity))

    def _consumer(self) -> None:
        while self._running:
            try:
                item = self._queue.get(timeout=0.05)
            except Empty:
                continue

            playout_at, key, note_id, action, velocity = item
            wait = playout_at - time.monotonic()
            if wait > 0:
                time.sleep(wait)

            # Discard if now too late
            if time.monotonic() - playout_at > 0.200:
                continue

            self._seen.add(key)
            if len(self._seen) > 2000:
                self._seen.clear()

            if action == 1:
                self._audio.play_note(note_id, velocity)
            else:
                # Only release if the note is actually playing (press wasn't lost)
                self._audio.stop_note(note_id)
