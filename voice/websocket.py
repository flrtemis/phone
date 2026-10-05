"""WebSocket transport for the voice agent: the same call, from anywhere.

Why this exists
---------------

The AudioSocket bridge is loopback-only by design and Asterisk has to be able to
reach it. That works at home. Away from home there are only two ways to talk to
the agent, and this module implements the free one:

* **Rented number (costs money).** Your mobile calls a DID; the carrier bridges
  the call over the PSTN into Asterisk, exactly as `docs/VOICE.md` describes.
  Your *carrier* number is genuinely involved, and so is a per-minute bill.
* **Data (free).** Your phone opens a socket to the agent and carries the audio
  itself. No DID, no carrier, no per-minute cost — but the call is inside an app
  or a browser tab, and it rings on your data connection rather than your SIM.

There is no third option. A companion app cannot make your carrier number reach
a server: inbound calls to a mobile number are delivered by the carrier's IMS to
the SIM that owns them, and no third-party application can register to that on
your behalf. (Call forwarding can point your number at a DID — which is the first
option, with a forwarding charge on top.) So the honest answer to "can an app let
me call the agent on my real number?" is: the app gives you the free data path,
and the real number needs a DID. Both are supported here.

The protocol
------------

Deliberately tiny, so a native Android or iOS client is an afternoon's work:

    client -> wss://host:8443/ws?token=...
    client -> {"type": "hello", "sample_rate": 8000, "rate": 8000}   (text)
    server -> {"type": "ready", "session_rate": 8000}                (text)
    both   -> binary frames of signed 16-bit little-endian mono PCM
    client -> {"type": "dtmf", "digit": "5"} | {"type": "hangup"}
    server -> {"type": "turn", "role": "user"|"assistant", "text": "..."}
    server -> {"type": "bye", "reason": "caller-hung-up"}

Audio is sent in short frames (20 ms is the convention here: 320 bytes at 8 kHz,
1920 at 48 kHz). The server resamples whatever rate the client declares, so a
browser whose AudioContext only runs at 48 kHz does not need to care.

RFC 6455 is implemented here in the standard library - handshake, masking,
ping/pong, fragmentation, size caps - because this project does not take a
dependency for something it can do in 200 lines.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import queue
import socket
import struct
import threading
from dataclasses import dataclass
from typing import Optional

from voice.audio import SAMPLE_WIDTH, StreamingResampler, frame_bytes
from voice.audiosocket import Duplex

LOG = logging.getLogger("phone.voice.websocket")

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

#: A single audio frame is ~2 kB; anything larger than this is a client bug or an
#: attack, and either way it is not worth buffering.
MAX_MESSAGE = 256 * 1024


class WebSocketError(Exception):
    """The peer sent something that is not valid WebSocket framing."""


def accept_key(client_key: str) -> str:
    """The Sec-WebSocket-Accept value for a given Sec-WebSocket-Key."""
    digest = hashlib.sha1((client_key + WS_GUID).encode("ascii")).digest()  # noqa: S324 - RFC 6455 mandates SHA-1
    return base64.b64encode(digest).decode("ascii")


def _recv_exactly(sock: socket.socket, count: int) -> Optional[bytes]:
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        try:
            chunk = sock.recv(remaining)
        except (TimeoutError, socket.timeout):
            return None
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _apply_mask(data: bytes, key: bytes) -> bytes:
    if not key:
        return data
    return bytes(byte ^ key[index % 4] for index, byte in enumerate(data))


@dataclass
class Frame:
    opcode: int
    payload: bytes
    fin: bool = True


def encode_frame(opcode: int, payload: bytes = b"", *, mask: bool = False, fin: bool = True) -> bytes:
    """Build a frame. Servers must not mask; clients must."""
    header = bytearray()
    header.append((0x80 if fin else 0x00) | (opcode & 0x0F))
    length = len(payload)
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        header.append(mask_bit | length)
    elif length < 1 << 16:
        header.append(mask_bit | 126)
        header += struct.pack("!H", length)
    else:
        header.append(mask_bit | 127)
        header += struct.pack("!Q", length)
    if mask:
        key = os.urandom(4)
        header += key
        payload = _apply_mask(payload, key)
    return bytes(header) + payload


def read_frame(sock: socket.socket, *, require_mask: bool = True) -> Optional[Frame]:
    """Read one frame, or None if the peer closed the connection."""
    header = _recv_exactly(sock, 2)
    if header is None:
        return None
    first, second = header[0], header[1]
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        extended = _recv_exactly(sock, 2)
        if extended is None:
            return None
        length = struct.unpack("!H", extended)[0]
    elif length == 127:
        extended = _recv_exactly(sock, 8)
        if extended is None:
            return None
        length = struct.unpack("!Q", extended)[0]
    if length > MAX_MESSAGE:
        raise WebSocketError(f"message too large: {length} bytes")
    if require_mask and not masked:
        # A client that does not mask is either broken or an attacker probing for
        # cache-poisoning behaviour in intermediaries. Either way, refuse it.
        raise WebSocketError("client frames must be masked (RFC 6455 section 5.1)")
    key = b""
    if masked:
        key = _recv_exactly(sock, 4)
        if key is None:
            return None
    payload = _recv_exactly(sock, length) if length else b""
    if payload is None:
        raise WebSocketError("truncated frame payload")
    return Frame(opcode=opcode, payload=_apply_mask(payload, key) if masked else payload, fin=fin)


class WebSocket:
    """A minimal server-side WebSocket connection."""

    def __init__(self, sock: socket.socket, *, timeout: Optional[float] = None) -> None:
        self.sock = sock
        self.timeout = timeout
        self.closed = False
        self.close_reason = ""
        if timeout:
            self.sock.settimeout(timeout)

    # ---- sending -----------------------------------------------------
    def send_binary(self, payload: bytes) -> bool:
        return self._send(OP_BINARY, payload)

    def send_text(self, text: str) -> bool:
        return self._send(OP_TEXT, text.encode("utf-8"))

    def send_json(self, payload: dict) -> bool:
        return self.send_text(json.dumps(payload, ensure_ascii=False))

    def ping(self) -> bool:
        return self._send(OP_PING, b"")

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        self._send(OP_CLOSE, struct.pack("!H", code) + reason.encode("utf-8")[:100])
        self.closed = True
        # Half-close: we are done sending, the peer may still have a close frame
        # or buffered audio for us. (SHUT_RDWR here would discard it, and the
        # socket is closed properly by the caller.)
        try:
            self.sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def _send(self, opcode: int, payload: bytes) -> bool:
        if self.closed:
            return False
        try:
            self.sock.sendall(encode_frame(opcode, payload, mask=False))
            return True
        except OSError:
            self.closed = True
            return False

    # ---- receiving ---------------------------------------------------
    def recv_message(self) -> Optional[tuple[int, bytes]]:
        """The next text/binary message, or None when the connection ended.

        Control frames are handled here: pings are answered, pongs ignored, and a
        close is echoed before returning None.
        """
        buffer = bytearray()
        message_opcode = 0
        while True:
            try:
                frame = read_frame(self.sock, require_mask=True)
            except (WebSocketError, OSError) as exc:
                LOG.debug("websocket read failed: %s", exc)
                self.closed = True
                return None
            if frame is None:
                self.closed = True
                return None

            if frame.opcode == OP_CLOSE:
                code = struct.unpack("!H", frame.payload[:2])[0] if len(frame.payload) >= 2 else 1000
                self.close_reason = f"peer closed ({code})"
                self.close(code)
                return None
            if frame.opcode == OP_PING:
                self._send(OP_PONG, frame.payload[:125])
                continue
            if frame.opcode == OP_PONG:
                continue

            if frame.opcode in (OP_TEXT, OP_BINARY):
                if buffer:
                    raise WebSocketError("new data frame inside a fragmented message")
                message_opcode = frame.opcode
                buffer.extend(frame.payload)
            elif frame.opcode == OP_CONTINUATION:
                if not buffer and message_opcode == 0:
                    raise WebSocketError("continuation frame with nothing to continue")
                buffer.extend(frame.payload)
            else:
                raise WebSocketError(f"unknown opcode 0x{frame.opcode:x}")

            if len(buffer) > MAX_MESSAGE:
                raise WebSocketError("fragmented message too large")
            if frame.fin:
                return message_opcode, bytes(buffer)


class WebSocketDuplex(Duplex):
    """Presents a WebSocket audio stream to the call session as a phone line.

    A background reader thread pulls messages out of the socket and turns them
    into audio frames on a queue, because the call session is pull-based: it asks
    for "the next 20 ms, waiting at most a frame time". Doing it the other way
    round (blocking on the socket inside recv_audio) would make barge-in polling
    impossible to reason about.

    Resampling lives here rather than in the browser: a client declares the rate
    it can produce (Chrome will happily give you 8 kHz; Safari often will not),
    and both directions are converted. The session always sees 8 kHz, which keeps
    the VAD thresholds and the timing maths meaningful.
    """

    def __init__(
        self,
        ws: WebSocket,
        *,
        client_rate: int = 8000,
        session_rate: int = 8000,
        on_event=None,
    ) -> None:
        self.ws = ws
        self.client_rate = client_rate or session_rate
        self.rate = session_rate
        self.on_event = on_event
        self.call_id = "web"
        self._closed = False
        self._eof = False
        self._dtmf: list[str] = []
        self._lock = threading.Lock()
        self._audio: "queue.Queue[bytes]" = queue.Queue()
        self._tail = bytearray()
        self._in_frame = frame_bytes(self.rate)
        self._to_session = (
            StreamingResampler(self.client_rate, self.rate) if self.client_rate != self.rate else None
        )
        self._to_client = (
            StreamingResampler(self.rate, self.client_rate) if self.client_rate != self.rate else None
        )
        self._reader = threading.Thread(target=self._pump, name="voice-ws-reader", daemon=True)
        self._reader.start()

    # ---- inbound -----------------------------------------------------
    def _pump(self) -> None:
        try:
            while not self._closed:
                message = self.ws.recv_message()
                if message is None:
                    break
                opcode, payload = message
                if opcode == OP_BINARY:
                    self._push_audio(payload)
                else:
                    self._handle_control(payload)
        except Exception:  # noqa: BLE001 - the call must not die with the reader thread
            LOG.exception("websocket reader failed")
        finally:
            self._eof = True

    def _push_audio(self, payload: bytes) -> None:
        if len(payload) % SAMPLE_WIDTH:
            payload = payload[: len(payload) - 1]  # a half sample is not audio
        if not payload:
            return
        pcm = self._to_session.process(payload) if self._to_session else payload
        if not pcm:
            return
        self._tail.extend(pcm)
        # Hand the session whole frames only. Padding a partial frame with silence
        # (which frames_of() would do) lowers its energy and can make the VAD
        # decide the caller stopped speaking mid-syllable.
        while len(self._tail) >= self._in_frame:
            self._audio.put(bytes(self._tail[: self._in_frame]))
            del self._tail[: self._in_frame]

    def _handle_control(self, payload: bytes) -> None:
        try:
            event = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            LOG.debug("ignoring unparseable control message")
            return
        kind = event.get("type", "")
        if kind == "dtmf":
            digit = str(event.get("digit", ""))[:1]
            if digit:
                with self._lock:
                    self._dtmf.append(digit)
        elif kind in ("hangup", "bye"):
            self._closed = True
        elif kind == "hello":
            LOG.debug("client hello: %s", event)
        else:
            LOG.debug("ignoring unknown control message type %r", kind)

    def recv_audio(self, timeout: float = 0.0) -> Optional[bytes]:
        try:
            return self._audio.get(timeout=timeout if timeout > 0 else 0.001)
        except queue.Empty:
            return None

    def take_dtmf(self) -> list[str]:
        with self._lock:
            digits, self._dtmf = self._dtmf, []
        return digits

    # ---- outbound ----------------------------------------------------
    def send_audio(self, pcm: bytes) -> None:
        if self._closed or not pcm:
            return
        out = self._to_client.process(pcm) if self._to_client else pcm
        if out and not self.ws.send_binary(out):
            self._closed = True

    def send_dtmf(self, digit: str) -> None:
        if digit:
            self.ws.send_json({"type": "dtmf", "digit": digit[:1]})

    def send_event(self, event: dict) -> None:
        self.ws.send_json(event)

    def hangup(self) -> None:
        self.ws.send_json({"type": "bye"})
        self._closed = True

    def close(self) -> None:
        self._closed = True
        self.ws.close()
        if self._reader.is_alive() and threading.current_thread() is not self._reader:
            self._reader.join(timeout=1.0)

    @property
    def is_closed(self) -> bool:
        """True once the connection is gone *and* nothing is left to read.

        The order matters. If a client hangs up while audio is still queued - a
        phone call where the caller says something and then hangs up, which is
        the normal way a call ends - the session must be allowed to drain what
        the caller said before it stops. Reporting the close first threw away the
        last thing the caller said, and (in the first version of this file) the
        whole utterance with it.
        """
        if not self._audio.empty():
            return False
        return self._closed or self._eof or self.ws.closed


def upgrade_response(client_key: str) -> bytes:
    """The bytes of a successful 101 response, for use by an HTTP handler."""
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept_key(client_key)}\r\n"
        "\r\n"
    ).encode("ascii")


def take_upgrade(handler, *, timeout: float = 20.0) -> Optional[WebSocket]:
    """Turn a handshaking HTTP handler into a WebSocket, or return None.

    Called *after* the handler has decided the request is authorised: everything
    above this point is a normal HTTP request with headers and a token, and
    everything below it is raw frames on the same connection.
    """
    key = handler.headers.get("Sec-WebSocket-Key", "")
    if not key or handler.headers.get("Upgrade", "").lower() != "websocket":
        return None
    handler.wfile.write(upgrade_response(key))
    handler.wfile.flush()
    handler.close_connection = True  # we own the socket from here
    sock = handler.connection
    sock.settimeout(timeout)
    return WebSocket(sock, timeout=timeout)
