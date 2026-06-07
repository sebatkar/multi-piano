# Multi-Piano

A LAN-based collaborative piano application with a terminal user interface. Multiple players on the same network can discover each other, join a shared lobby, and play a 13-key one-octave keyboard (C4–C5) together in real time.

## What We Built

The application uses three separate network channels. Lobby discovery runs over UDP broadcast so players find each other without any prior configuration. Lobby membership — join, leave, player list — is managed over a TCP connection to the host, using length-prefixed JSON messages. Note events travel over a second UDP socket as compact 15-byte binary packets carrying a sequence number, a scheduled play timestamp, the note ID, action (press/release), and velocity.

The audio pipeline was designed around **scheduled playback**: when a player presses a key, the application computes `play_at = now + 30 ms` and uses that absolute timestamp for both the local sound (via a `threading.Timer`) and the broadcast to peers. Every peer receives the packet, converts the sender's timestamp to their own local clock, and waits until that exact moment to play the note. The goal is that all players hear every note simultaneously regardless of who pressed it.

To make timestamp conversion possible across machines with unsynchronised clocks, we implemented an **NTP-style clock offset measurement** on the same UDP note port. After a player joins, both sides exchange `SYNC_REQ` / `SYNC_ACK` messages carrying four timestamps. The standard NTP formula `((t2 - t1) + (t3 - t4)) / 2` gives the offset between the two clocks, which is refreshed every five seconds. The remote playback queue — renamed from `JitterBuffer` to `NoteScheduler` to reflect that it no longer adds any delay itself — is a priority queue sorted by monotonic deadline that consumes events in scheduled order.

## Challenges

The trickiest part was getting the clock synchronisation correct. The NTP four-timestamp formula looks simple, but deriving *which direction* to apply the offset when converting a received timestamp takes care: if `offset = peer_clock − my_clock`, then `local_play_at = peer_timestamp − offset`. Getting the sign wrong means notes play twice as far in the future or in the past. We verified this by working through the algebra with concrete example clocks before writing any code.

A subtler bug existed in the original `JitterBuffer`: duplicate UDP packets were checked against a `_seen` set, but the key was only *added* to that set inside the consumer thread after playback. If the same packet arrived twice before the consumer had processed the first copy, both passed the check and both played. We fixed this by adding the key to `_seen` immediately in `push()` instead of in the consumer.

Sharing the UDP note port between binary note packets and JSON clock-sync messages required a dispatch strategy. Rather than adding a new port, we distinguish packet types by length: a note packet is always exactly 15 bytes, so anything else is attempted as JSON. This avoids opening extra sockets while keeping the protocol simple.

## Shortcomings

The 30 ms playout delay applies to the local player as well, because they must hear their own note at the same moment as everyone else. This introduces 30 ms of input latency, which is at the edge of what a pianist notices. On a well-configured LAN the tradeoff is worth it, but it would be uncomfortable for a solo player with no peers.

The 30 ms budget also sets a hard ceiling on supportable one-way network latency. On a LAN this is rarely a problem (typical latency is 1–5 ms), but the application would not work correctly over the internet or between networks with higher round-trip times.

Note routing follows a star topology: guests only know the host's IP, so notes from one guest are received by the host but are never forwarded to other guests. In a three-player session, players B and C cannot hear each other — only the host hears everyone. Fixing this requires either host-side note relay or exchanging all peer IPs during the join handshake, neither of which is currently implemented.

Finally, the visual key flash on the piano display fires immediately when a remote note packet *arrives*, not when the audio actually plays 30 ms later. The discrepancy is small enough to ignore in practice, but it means the visual and audio are not precisely aligned for remote notes.
