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
    """Priority-queue buffer that plays remote notes at their sender-scheduled time.

    The sender stamps each note with (local_time + PLAYOUT_DELAY). The receiver
    converts that timestamp to its own clock via the measured clock offset and
    waits until that moment to play. This keeps all players in sync without
    needing clock synchronisation to happen before the first note arrives —
    if the offset is still 0.0 (not yet measured) the note plays ~30 ms after
    arrival, which is the same behaviour as the old fixed-delay buffer.
    """

    def __init__(self, audio: AudioEngine):
        self._audio = audio
        self._queue: PriorityQueue = PriorityQueue()
        self._seen: set[tuple[str, int]] = set()
        self._running = False

    def start(self) -> None:
        self._running = True
        threading.Thread(target=self._consumer, daemon=True).start()

    def stop(self) -> None:
        self._running = False

    def push(self, seq: int, play_at: float,
             note_id: int, action: int, velocity: int,
             sender_id: str = '') -> None:
        """Schedule a note for playback.

        play_at is an absolute time.time()-based timestamp (already converted
        to the local clock by the caller using the measured peer clock offset).
        """
        key = (sender_id, seq)
        if key in self._seen:
            return
        # Dedup here, not in the consumer, to prevent races with rapid duplicates.
        self._seen.add(key)
        if len(self._seen) > 2000:
            self._seen.clear()
        # Convert the wall-clock play_at to a monotonic deadline so the consumer
        # is immune to NTP steps during playback.
        play_at_mono = time.monotonic() + (play_at - time.time())
        self._queue.put((play_at_mono, note_id, action, velocity))

    def _consumer(self) -> None:
        while self._running:
            try:
                item = self._queue.get(timeout=0.05)
            except Empty:
                continue

            play_at_mono, note_id, action, velocity = item
            wait = play_at_mono - time.monotonic()
            if wait > 0:
                time.sleep(wait)

            # Discard if more than 200 ms overdue (e.g. thread was starved).
            if time.monotonic() - play_at_mono > 0.200:
                continue

            if action == 1:
                self._audio.play_note(note_id, velocity)
            else:
                self._audio.stop_note(note_id)
