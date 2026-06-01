"""Textual TUI for Multi-Piano: lobby browser and piano screen."""
import os
import time
import threading
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, ListItem, ListView, Static
from textual import events

from audio import AudioEngine, JitterBuffer
from network import NetworkManager

# ── Key ↔ note mapping ──────────────────────────────────────────────────────
#  Note IDs: 0=C4 1=C#4 2=D4 3=D#4 4=E4 5=F4 6=F#4 7=G4 8=G#4 9=A4 10=A#4 11=B4 12=C5
KEY_TO_NOTE: dict[str, int] = {
    'a': 0,  'w': 1,  's': 2,  'e': 3,
    'd': 4,  'f': 5,  't': 6,  'g': 7,
    'y': 8,  'h': 9,  'u': 10, 'j': 11, 'k': 12,
}

NOTE_LABELS = ['C4', 'C#4', 'D4', 'D#4', 'E4', 'F4', 'F#4', 'G4', 'G#4', 'A4', 'A#4', 'B4', 'C5']

# White key note IDs and their keyboard keys
WHITE_KEYS = [(0, 'a'), (2, 's'), (4, 'd'), (5, 'f'), (7, 'g'), (9, 'h'), (11, 'j'), (12, 'k')]
# Black key note IDs and their keyboard keys
BLACK_KEYS = [(1, 'w'), (3, 'e'), (6, 't'), (8, 'y'), (10, 'u')]


def _esc(key: str) -> str:
    """Escape a key label so Rich doesn't interpret e.g. [s] as strikethrough."""
    return f'\\[{key}]'


def _render_piano(pressed: set[int]) -> str:
    """Return a multi-line ASCII piano string with pressed keys highlighted."""
    col_w = 7

    # Black-key top row aligned over white-key columns.
    # black_at_white: white-key column index → (note_id, keyboard_key)
    black_at_white = {0: (1, 'w'), 1: (3, 'e'), 3: (6, 't'), 4: (8, 'y'), 5: (10, 'u')}

    top_line = ''
    for wi in range(8):
        if wi in black_at_white:
            nid, key = black_at_white[wi]
            cell = _esc(key).center(4)
            if nid in pressed:
                top_line += '   ' + f'[reverse]{cell}[/reverse]' + '  '
            else:
                top_line += '   ' + cell + '  '
        else:
            top_line += '       '

    white_row = ''
    for nid, key in WHITE_KEYS:
        # 2 spaces + escaped key (3 visible) + 2 spaces = 7 visible chars = col_w
        cell = f'  {_esc(key)}  '
        if nid in pressed:
            white_row += f'│[reverse]{cell}[/reverse]'
        else:
            white_row += f'│{cell}'
    white_row += '│'

    note_row = ''
    for nid, key in WHITE_KEYS:
        cell = NOTE_LABELS[nid].center(col_w)
        if nid in pressed:
            note_row += f'│[reverse]{cell}[/reverse]'
        else:
            note_row += f'│{cell}'
    note_row += '│'

    inner = '─' * (8 * (col_w + 1) - 1)
    return (
        f'{top_line}\n'
        f'┌{inner}┐\n'
        f'{white_row}\n'
        f'{note_row}\n'
        f'└{inner}┘'
    )


# ═══════════════════════════════════════════════════════════════════════════ #
#  Login / Lobby Screen                                                       #
# ═══════════════════════════════════════════════════════════════════════════ #

class LobbyScreen(Screen):
    """Name entry + lobby discovery + host/join."""

    DEFAULT_CSS = """
    LobbyScreen {
        layout: vertical;
    }
    #form {
        height: auto;
        padding: 1 2;
        border: solid $primary;
        margin: 1 2;
    }
    #form Label {
        margin-bottom: 0;
    }
    #form Input {
        margin-bottom: 1;
    }
    #buttons {
        height: auto;
        margin: 0 2;
        layout: horizontal;
    }
    #buttons Button {
        margin-right: 1;
    }
    #lobby-list-container {
        border: solid $primary;
        margin: 1 2;
        height: 1fr;
        padding: 0 1;
    }
    #status {
        height: auto;
        margin: 0 2 1 2;
        color: $warning;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Container(id='form'):
            yield Label('Player name:')
            yield Input(placeholder='e.g. Ömer', id='name-input')
            yield Label('Lobby name (when hosting):')
            yield Input(placeholder='e.g. Berkay\'s Room', id='lobby-name-input')
        with Horizontal(id='buttons'):
            yield Button('Host Lobby', id='btn-host', variant='primary')
            yield Button('Join Selected', id='btn-join', variant='success')
            yield Button('Refresh', id='btn-refresh', variant='default')
        yield Label('', id='status')
        with Container(id='lobby-list-container'):
            yield Label('[b]Discovered Lobbies[/b]')
            yield ListView(id='lobby-list')
        yield Footer()

    def on_mount(self) -> None:
        app: MultiPianoApp = self.app  # type: ignore
        app.network.on_lobby_discovered = self._on_lobby_found
        app.network.on_lobby_lost = self._on_lobby_lost

    def _on_lobby_found(self, info: dict) -> None:
        self.app.call_from_thread(self._add_lobby_item, info)

    def _on_lobby_lost(self, host_ip: str) -> None:
        self.app.call_from_thread(self._remove_lobby_item, host_ip)

    def _add_lobby_item(self, info: dict) -> None:
        lv: ListView = self.query_one('#lobby-list', ListView)
        host_ip = info['SENDER_IP']
        # Remove existing entry for this host if present
        for item in list(lv.children):
            if getattr(item, 'data_ip', None) == host_ip:
                item.remove()
        label = (f"{info['LOBBY_NAME']}  "
                 f"[{info['ACTIVE_PLAYERS']} player(s)]  "
                 f"— {host_ip}")
        item = ListItem(Label(label))
        item.data_ip = host_ip          # type: ignore[attr-defined]
        item.data_port = info['TCP_PORT']  # type: ignore[attr-defined]
        lv.append(item)

    def _remove_lobby_item(self, host_ip: str) -> None:
        lv: ListView = self.query_one('#lobby-list', ListView)
        for item in list(lv.children):
            if getattr(item, 'data_ip', None) == host_ip:
                item.remove()

    def _set_status(self, msg: str) -> None:
        self.query_one('#status', Label).update(msg)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        app: MultiPianoApp = self.app  # type: ignore
        name = self.query_one('#name-input', Input).value.strip()

        if event.button.id == 'btn-refresh':
            app.network._send_discover()
            self._set_status('Sent discovery broadcast...')
            return

        if not name:
            self._set_status('Enter a player name first.')
            return

        app.network.player_name = name

        if event.button.id == 'btn-host':
            lobby_name = self.query_one('#lobby-name-input', Input).value.strip()
            if not lobby_name:
                lobby_name = f"{name}'s Room"
            app.network.host_lobby(lobby_name)
            app.push_screen(PianoScreen())

        elif event.button.id == 'btn-join':
            lv: ListView = self.query_one('#lobby-list', ListView)
            highlighted = lv.highlighted_child
            if highlighted is None:
                self._set_status('Select a lobby from the list first.')
                return
            host_ip = getattr(highlighted, 'data_ip', None)
            port = getattr(highlighted, 'data_port', 12487)
            if not host_ip:
                self._set_status('Could not read lobby info.')
                return
            try:
                app.network.join_lobby(host_ip, port)
                app.push_screen(PianoScreen())
            except Exception as exc:
                self._set_status(f'Join failed: {exc}')


# ═══════════════════════════════════════════════════════════════════════════ #
#  Piano Screen                                                               #
# ═══════════════════════════════════════════════════════════════════════════ #

class PianoScreen(Screen):
    """In-lobby piano screen."""

    BINDINGS = [
        Binding('escape', 'leave', 'Leave lobby'),
        Binding('ctrl+q', 'app.quit', 'Quit'),
    ]

    DEFAULT_CSS = """
    PianoScreen {
        layout: horizontal;
    }
    #sidebar {
        width: 22;
        border: solid $primary;
        padding: 1;
        height: 1fr;
    }
    #sidebar Label {
        margin-bottom: 1;
    }
    #main-area {
        width: 1fr;
        padding: 1 2;
        layout: vertical;
    }
    #piano-widget {
        height: auto;
        border: solid $accent;
        padding: 1;
        margin-bottom: 1;
    }
    #key-help {
        height: auto;
        color: $text-muted;
        margin-bottom: 1;
    }
    #activity-log {
        height: 1fr;
        border: solid $primary;
        padding: 0 1;
        overflow-y: auto;
    }
    #players-label {
        color: $success;
    }
    """

    _pressed: reactive[frozenset] = reactive(frozenset())

    def __init__(self):
        super().__init__()
        self._held_keys: set[int] = set()
        self._release_timers: dict[int, threading.Timer] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id='sidebar'):
            yield Label('[b]Players[/b]', id='players-label')
            yield Label('(loading...)', id='players-list')
            yield Label('')
            yield Label('[b]Local IP[/b]')
            app: MultiPianoApp = self.app  # type: ignore
            yield Label(app.network.local_ip, id='local-ip')
        with Vertical(id='main-area'):
            yield Static('', id='piano-widget')
            yield Static(
                '[bold]White keys:[/bold]  A  S  D  F  G  H  J  K\n'
                '[bold]Black keys:[/bold]  W  E     T  Y  U\n'
                '(Escape → leave lobby)',
                id='key-help'
            )
            with ScrollableContainer(id='activity-log'):
                yield Label('[b]Activity[/b]')
                yield Label('', id='log-content')

    def on_mount(self) -> None:
        app: MultiPianoApp = self.app  # type: ignore
        app.network.on_state_update = self._on_state_update
        app.network.on_note_received = self._on_note_received
        app.network.on_disconnected = self._on_disconnected
        app.jitter.start()
        # Initial render
        self._refresh_piano()
        # Show current players (host has them already)
        if app.network.players:
            self._update_players_widget(app.network.players)

    def _refresh_piano(self) -> None:
        piano: Static = self.query_one('#piano-widget', Static)
        piano.update(_render_piano(self._held_keys))

    # ── Network callbacks (called from bg threads) ────────────────────── #

    def _on_state_update(self, players: list[str]) -> None:
        self.app.call_from_thread(self._update_players_widget, players)

    def _update_players_widget(self, players: list[str]) -> None:
        text = '\n'.join(
            f'[green]●[/green] {p}' + (' [dim](host)[/dim]' if i == 0 else '')
            for i, p in enumerate(players)
        )
        self.query_one('#players-list', Label).update(text or '(empty)')

    def _on_note_received(self, note_id: int, action: int, velocity: int,
                          seq: int, ts: float, sender_ip: str) -> None:
        app: MultiPianoApp = self.app  # type: ignore
        app.jitter.push(seq, ts, note_id, action, velocity)
        if action == 1:
            self.app.call_from_thread(self._flash_remote_key, note_id)

    def _flash_remote_key(self, note_id: int) -> None:
        self._held_keys.add(note_id)
        self._refresh_piano()
        def _clear():
            self.app.call_from_thread(self._unflash_key, note_id)
        t = threading.Timer(0.4, _clear)
        t.daemon = True
        t.start()

    def _unflash_key(self, note_id: int) -> None:
        self._held_keys.discard(note_id)
        self._refresh_piano()

    def _on_disconnected(self) -> None:
        self.app.call_from_thread(self._handle_disconnect)

    def _handle_disconnect(self) -> None:
        self._log('Host disconnected.')
        self.app.pop_screen()

    # ── Key press handling ─────────────────────────────────────────────── #

    def on_key(self, event: events.Key) -> None:
        key = event.key
        note_id = KEY_TO_NOTE.get(key)
        if note_id is None:
            return
        event.stop()

        # Debounce: ignore key-repeat events
        if note_id in self._held_keys:
            return

        app: MultiPianoApp = self.app  # type: ignore
        app.audio.play_note(note_id, 100)
        app.network.broadcast_note(note_id, 1, 100)

        self._held_keys.add(note_id)
        self._refresh_piano()

        # Auto-release highlight after 500 ms (no key-release events in TUI)
        if note_id in self._release_timers:
            self._release_timers[note_id].cancel()
        t = threading.Timer(0.5, self._auto_release, args=(note_id,))
        t.daemon = True
        t.start()
        self._release_timers[note_id] = t

    def _auto_release(self, note_id: int) -> None:
        app: MultiPianoApp = self.app  # type: ignore
        app.audio.stop_note(note_id)
        app.network.broadcast_note(note_id, 0, 0)
        self.app.call_from_thread(self._release_key_ui, note_id)

    def _release_key_ui(self, note_id: int) -> None:
        self._held_keys.discard(note_id)
        self._release_timers.pop(note_id, None)
        self._refresh_piano()

    # ── Actions ───────────────────────────────────────────────────────── #

    def action_leave(self) -> None:
        app: MultiPianoApp = self.app  # type: ignore
        app.audio.stop_all()
        app.jitter.stop()
        app.network.stop()
        # Re-init network for a fresh lobby session
        app.network = NetworkManager(app.network.player_name)
        app.network.start()
        self.app.pop_screen()

    def _log(self, msg: str) -> None:
        label: Label = self.query_one('#log-content', Label)
        current = str(label.renderable)
        ts = time.strftime('%H:%M:%S')
        label.update(f'{current}\n[{ts}] {msg}'.strip())


# ═══════════════════════════════════════════════════════════════════════════ #
#  App                                                                        #
# ═══════════════════════════════════════════════════════════════════════════ #

class MultiPianoApp(App):
    """Root Textual application for Multi-Piano."""

    TITLE = 'Multi-Piano'
    BINDINGS = [
        Binding('ctrl+q', 'quit', 'Quit'),
    ]

    def __init__(self, sounds_dir: str = 'sounds'):
        super().__init__()
        self.audio = AudioEngine(sounds_dir)
        self.network = NetworkManager(player_name='Player')
        self.jitter = JitterBuffer(self.audio)

    def on_mount(self) -> None:
        self.audio.init()
        self.network.start()
        self.push_screen(LobbyScreen())

    def on_unmount(self) -> None:
        self.audio.quit()
        self.network.stop()
        self.jitter.stop()
