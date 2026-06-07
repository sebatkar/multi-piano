"""Networking layer: UDP lobby discovery, TCP lobby management, UDP note stream."""
import json
import socket
import struct
import threading
import time
from typing import Callable, Optional

DISCOVERY_PORT = 12488
TCP_PORT       = 12487
NOTE_PORT      = 12489

NOTE_FORMAT = '< I d B B B'
NOTE_SIZE   = struct.calcsize(NOTE_FORMAT)  # 15 bytes


def get_local_ip() -> str:
    # Try routing trick first (works on most LANs and Docker with internet access)
    for target in ('8.8.8.8', '192.168.1.1', '10.0.0.1'):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((target, 80))
                ip = s.getsockname()[0]
                if not ip.startswith('127.'):
                    return ip
        except Exception:
            pass
    # Fallback: hostname resolution (works in Docker bridge networks)
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if not ip.startswith('127.'):
            return ip
    except Exception:
        pass
    return '127.0.0.1'


def _send_json(sock: socket.socket, msg: dict) -> None:
    """Send a length-prefixed JSON message over TCP."""
    data = json.dumps(msg).encode()
    sock.sendall(len(data).to_bytes(4, 'big') + data)


def _recv_json(sock: socket.socket) -> Optional[dict]:
    """Receive a length-prefixed JSON message over TCP. Returns None on EOF."""
    try:
        raw = _recv_exact(sock, 4)
        if not raw:
            return None
        length = int.from_bytes(raw, 'big')
        data = _recv_exact(sock, length)
        if not data:
            return None
        return json.loads(data.decode())
    except Exception:
        return None


def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


class NetworkManager:
    """Manages all network operations for multi-piano.

    Callbacks (all called from background threads — wrap with call_from_thread):
        on_lobby_discovered(info: dict)
        on_lobby_lost(host_ip: str)
        on_state_update(players: list[str])
        on_note_received(note_id, action, velocity, seq, timestamp, sender_ip)
        on_disconnected()
    """

    def __init__(self, player_name: str):
        self.player_name = player_name
        self.local_ip = get_local_ip()

        self.is_host = False
        self.lobby_name = ''
        self.players: list[str] = []
        self.peer_ips: list[str] = []

        # Discovered lobbies: host_ip -> lobby info dict
        self.known_lobbies: dict[str, dict] = {}
        self._lobby_last_seen: dict[str, float] = {}  # host_ip -> epoch seconds
        self._LOBBY_TTL = 6.0  # seconds; host broadcasts every 2 s so 3 missed = gone

        self._lock = threading.Lock()
        self._running = False
        self._seq = 0
        self._seq_lock = threading.Lock()

        # --- Callbacks ---
        self.on_lobby_discovered: Optional[Callable] = None
        self.on_lobby_lost:       Optional[Callable] = None
        self.on_state_update:     Optional[Callable] = None
        self.on_note_received:    Optional[Callable] = None
        self.on_disconnected:     Optional[Callable] = None

        # --- Sockets ---
        self._disc_sock: Optional[socket.socket] = None   # UDP discovery (shared)
        self._note_sock: Optional[socket.socket] = None   # UDP notes     (shared)
        self._tcp_srv:   Optional[socket.socket] = None   # TCP server    (host only)
        self._tcp_cli:   Optional[socket.socket] = None   # TCP client    (guest only)

        # host only: ip -> (socket, player_name)
        self._clients: dict[str, tuple[socket.socket, str]] = {}

        # Clock synchronisation (NTP-style over UDP note port)
        # offset = peer_clock - my_clock; convert peer timestamp → local: ts - offset
        self._clock_offsets: dict[str, float] = {}
        self._pending_syncs: dict[str, float] = {}   # peer_ip -> t1 sent

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        self._running = True
        self._open_discovery_socket()
        self._open_note_socket()
        threading.Thread(target=self._discovery_loop,     daemon=True).start()
        threading.Thread(target=self._note_recv_loop,     daemon=True).start()
        threading.Thread(target=self._lobby_cleanup_loop, daemon=True).start()
        # Broadcast DISCOVER so existing hosts reply immediately
        threading.Thread(target=self._send_discover, daemon=True).start()

    def stop(self) -> None:
        self._running = False
        for sock in (self._disc_sock, self._note_sock, self._tcp_srv, self._tcp_cli):
            try:
                sock and sock.close()
            except Exception:
                pass
        for conn, _ in list(self._clients.values()):
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    #  Discovery                                                           #
    # ------------------------------------------------------------------ #

    def _open_discovery_socket(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(('', DISCOVERY_PORT))
        s.settimeout(1.0)
        self._disc_sock = s

    def _send_discover(self) -> None:
        msg = json.dumps({'type': 'DISCOVER', 'SENDER_IP': self.local_ip}).encode()
        try:
            self._disc_sock.sendto(msg, ('<broadcast>', DISCOVERY_PORT))
        except Exception:
            pass

    def _discovery_loop(self) -> None:
        while self._running:
            try:
                data, addr = self._disc_sock.recvfrom(2048)
                sender_ip = addr[0]
                msg = json.loads(data.decode())

                if msg['type'] == 'DISCOVER' and self.is_host:
                    self._send_lobby_reply(target=sender_ip)

                elif msg['type'] == 'LOBBY_REPLY' and sender_ip != self.local_ip:
                    with self._lock:
                        self.known_lobbies[sender_ip] = msg
                        self._lobby_last_seen[sender_ip] = time.time()
                    if self.on_lobby_discovered:
                        self.on_lobby_discovered(msg)

            except socket.timeout:
                continue
            except Exception:
                if not self._running:
                    break

    def _send_lobby_reply(self, target: Optional[str] = None) -> None:
        reply = {
            'type':           'LOBBY_REPLY',
            'LOBBY_NAME':     self.lobby_name,
            'SENDER_IP':      self.local_ip,
            'TCP_PORT':       TCP_PORT,
            'ACTIVE_PLAYERS': len(self.players),
        }
        data = json.dumps(reply).encode()
        dest = (target or '<broadcast>', DISCOVERY_PORT)
        try:
            self._disc_sock.sendto(data, dest)
        except Exception:
            pass

    def _lobby_cleanup_loop(self) -> None:
        """Periodically evict lobbies that stopped broadcasting (host gone)."""
        while self._running:
            time.sleep(3.0)
            now = time.time()
            with self._lock:
                stale = [ip for ip, ts in self._lobby_last_seen.items()
                         if now - ts > self._LOBBY_TTL]
            for ip in stale:
                self._evict_lobby(ip)

    def _evict_lobby(self, host_ip: str) -> None:
        """Remove a lobby from known_lobbies and fire on_lobby_lost."""
        with self._lock:
            self.known_lobbies.pop(host_ip, None)
            self._lobby_last_seen.pop(host_ip, None)
        if self.on_lobby_lost:
            self.on_lobby_lost(host_ip)

    # ------------------------------------------------------------------ #
    #  Hosting                                                             #
    # ------------------------------------------------------------------ #

    def host_lobby(self, lobby_name: str) -> None:
        self.is_host = True
        self.lobby_name = lobby_name
        self.players = [self.player_name]
        self.peer_ips = []

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('', TCP_PORT))
        srv.listen(8)
        srv.settimeout(1.0)
        self._tcp_srv = srv

        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._broadcast_loop, daemon=True).start()

    def _broadcast_loop(self) -> None:
        while self._running and self.is_host:
            self._send_lobby_reply()
            time.sleep(2.0)

    def _accept_loop(self) -> None:
        while self._running and self.is_host:
            try:
                conn, addr = self._tcp_srv.accept()
                threading.Thread(target=self._handle_client,
                                 args=(conn, addr[0]), daemon=True).start()
            except socket.timeout:
                continue
            except Exception:
                break

    def _handle_client(self, conn: socket.socket, ip: str) -> None:
        conn.settimeout(10.0)
        try:
            msg = _recv_json(conn)
            if not msg or msg.get('type') != 'JOIN':
                return
            name = msg['SENDER_NAME']

            with self._lock:
                self._clients[ip] = (conn, name)
                self.peer_ips.append(ip)
                self.players.append(name)

            self._broadcast_state()
            if self.on_state_update:
                self.on_state_update(list(self.players))
            self._start_sync_loop(ip)

            # Keep-alive: detect disconnect
            conn.settimeout(None)
            while self._running:
                try:
                    data = conn.recv(1)
                    if not data:
                        break
                except Exception:
                    break
        finally:
            with self._lock:
                entry = self._clients.pop(ip, None)
                if ip in self.peer_ips:
                    self.peer_ips.remove(ip)
                if entry:
                    _, name = entry
                    if name in self.players:
                        self.players.remove(name)
            self._broadcast_state()
            if self.on_state_update:
                self.on_state_update(list(self.players))
            try:
                conn.close()
            except Exception:
                pass

    def _broadcast_state(self) -> None:
        with self._lock:
            msg = {'type': 'STATE_UPDATE', 'PLAYERS': list(self.players)}
            targets = list(self._clients.values())
        for conn, _ in targets:
            try:
                _send_json(conn, msg)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    #  Joining                                                             #
    # ------------------------------------------------------------------ #

    def join_lobby(self, host_ip: str, tcp_port: int = TCP_PORT) -> None:
        self.is_host = False
        self.peer_ips = [host_ip]

        cli = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cli.connect((host_ip, tcp_port))
        self._tcp_cli = cli

        _send_json(cli, {
            'type':        'JOIN',
            'SENDER_NAME': self.player_name,
            'SENDER_IP':   self.local_ip,
        })

        threading.Thread(target=self._client_recv_loop, daemon=True).start()
        self._start_sync_loop(host_ip)

    def _client_recv_loop(self) -> None:
        while self._running:
            msg = _recv_json(self._tcp_cli)
            if msg is None:
                # Host dropped — remove their lobby immediately so it doesn't
                # reappear in the discovery list after returning to LobbyScreen.
                host_ip = self.peer_ips[0] if self.peer_ips else None
                if host_ip:
                    self._evict_lobby(host_ip)
                if self.on_disconnected:
                    self.on_disconnected()
                break
            if msg.get('type') == 'STATE_UPDATE':
                with self._lock:
                    self.players = msg['PLAYERS']
                if self.on_state_update:
                    self.on_state_update(list(self.players))

    # ------------------------------------------------------------------ #
    #  Note broadcasting / receiving                                       #
    # ------------------------------------------------------------------ #

    def _open_note_socket(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('', NOTE_PORT))
        s.settimeout(1.0)
        self._note_sock = s

    def _note_recv_loop(self) -> None:
        while self._running:
            try:
                data, addr = self._note_sock.recvfrom(2048)
                sender_ip = addr[0]
                if sender_ip == self.local_ip:
                    continue
                if len(data) == NOTE_SIZE:
                    seq, ts, note_id, action, velocity = struct.unpack(NOTE_FORMAT, data)
                    if self.on_note_received:
                        self.on_note_received(note_id, action, velocity, seq, ts, sender_ip)
                else:
                    try:
                        msg = json.loads(data.decode())
                        msg_type = msg.get('type')
                        if msg_type == 'SYNC_REQ':
                            self._handle_sync_req(msg, sender_ip)
                        elif msg_type == 'SYNC_ACK':
                            self._handle_sync_ack(msg, sender_ip)
                    except Exception:
                        pass
            except socket.timeout:
                continue
            except Exception:
                if not self._running:
                    break

    # ------------------------------------------------------------------ #
    #  Clock synchronisation (NTP-style, UDP)                             #
    # ------------------------------------------------------------------ #

    def get_clock_offset(self, peer_ip: str) -> float:
        """Return measured offset: peer_clock - my_clock (0.0 until first sync)."""
        return self._clock_offsets.get(peer_ip, 0.0)

    def _start_sync_loop(self, peer_ip: str) -> None:
        def _loop() -> None:
            while self._running:
                self._send_sync_req(peer_ip)
                time.sleep(5.0)
        threading.Thread(target=_loop, daemon=True).start()

    def _send_sync_req(self, peer_ip: str) -> None:
        t1 = time.time()
        self._pending_syncs[peer_ip] = t1
        msg = json.dumps({'type': 'SYNC_REQ', 't1': t1}).encode()
        try:
            self._note_sock.sendto(msg, (peer_ip, NOTE_PORT))
        except Exception:
            pass

    def _handle_sync_req(self, msg: dict, sender_ip: str) -> None:
        t2 = time.time()
        t3 = time.time()
        reply = json.dumps({
            'type': 'SYNC_ACK',
            't1': msg['t1'],
            't2': t2,
            't3': t3,
        }).encode()
        try:
            self._note_sock.sendto(reply, (sender_ip, NOTE_PORT))
        except Exception:
            pass

    def _handle_sync_ack(self, msg: dict, sender_ip: str) -> None:
        t4 = time.time()
        t1 = self._pending_syncs.get(sender_ip)
        if t1 is None:
            return
        t2 = msg['t2']
        t3 = msg['t3']
        # offset = peer_clock - my_clock  (NTP formula)
        self._clock_offsets[sender_ip] = ((t2 - t1) + (t3 - t4)) / 2

    # ------------------------------------------------------------------ #
    #  Note broadcasting                                                   #
    # ------------------------------------------------------------------ #

    def broadcast_note(self, note_id: int, action: int,
                       play_at: float, velocity: int = 100) -> None:
        """Send a note event to all peers.

        play_at is the absolute time.time() timestamp at which the note should
        be played (sender's local_time + PLAYOUT_DELAY). Receivers subtract
        their measured clock offset to convert it to their own clock.
        """
        if not self.peer_ips:
            return
        with self._seq_lock:
            seq = self._seq
            self._seq += 1
        packet = struct.pack(NOTE_FORMAT, seq, play_at, note_id, action, velocity)
        for ip in list(self.peer_ips):
            try:
                self._note_sock.sendto(packet, (ip, NOTE_PORT))
            except Exception:
                pass
