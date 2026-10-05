"""Asterisk AudioSocket: framing, server, and a duplex abstraction.

Protocol (docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket):

    each message = 1 byte type | 2 bytes payload length (big endian) | payload

    0x00  terminate the connection (``00 00 00`` is a hangup)
    0x01  payload is the 16-byte UUID of the audio stream
    0x03  payload is one ASCII DTMF digit
    0x10  payload is signed 16-bit 8 kHz mono PCM (little endian)
    0x11-0x18  the same at 12/16/24/32/44.1/48/96/192 kHz
    0xff  error; payload is an optional application error code

Asterisk is the TCP *client* here: the dialplan action ``AudioSocket(uuid,host,
port)`` connects out to us, sends the UUID, then streams audio in both
directions. We therefore bind a listening socket and never dial back.

The server binds loopback by default. It carries raw uncompressed speech with no
authentication whatsoever, so exposing it to a network would be an open
microphone; ``phone voice doctor`` refuses to start it on a public address
without an explicit opt-in (same fail-closed posture as the SMS receiver).
"""

from __future__ import annotations

import logging
import socket
import time
import socketserver
import struct
import threading
import uuid as uuid_module
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from voice.audio import frame_bytes, silence

LOG = logging.getLogger("phone.voice.audiosocket")

HEADER_SIZE = 3

MSG_HANGUP = 0x00
MSG_UUID = 0x01
MSG_DTMF = 0x03
MSG_AUDIO_8000 = 0x10
MSG_ERROR = 0xFF

#: Audio type code -> sample rate, for every rate the protocol defines.
AUDIO_RATES: dict[int, int] = {
    0x10: 8000,
    0x11: 12000,
    0x12: 16000,
    0x13: 24000,
    0x14: 32000,
    0x15: 44100,
    0x16: 48000,
    0x17: 96000,
    0x18: 192000,
}

ASTERISK_ERRORS: dict[int, str] = {
    1: "no such channel",
    2: "channel is not answered",
    3: "channel has no audio",
    4: "unsupported audio format",
}


class ProtocolError(Exception):
    """The peer sent something that is not valid AudioSocket framing."""


@dataclass(frozen=True)
class Message:
    kind: int
    payload: bytes = b""

    @property
    def is_audio(self) -> bool:
        return self.kind in AUDIO_RATES

    @property
    def rate(self) -> int:
        return AUDIO_RATES.get(self.kind, 8000)

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        if self.is_audio:
            return f"Message(audio {self.rate}Hz, {len(self.payload)} bytes)"
        if self.kind == MSG_HANGUP:
            return "Message(hangup)"
        if self.kind == MSG_UUID:
            try:
                return f"Message(uuid {uuid_module.UUID(bytes=self.payload)})"
            except (ValueError, AttributeError):
                return f"Message(uuid {self.payload.hex()})"
        if self.kind == MSG_DTMF:
            return f"Message(dtmf {self.payload.decode('ascii', 'replace')!r})"
        if self.kind == MSG_ERROR:
            code = self.payload[0] if self.payload else None
            return f"Message(error {code}: {ASTERISK_ERRORS.get(code, 'unknown')})"
        return f"Message(type 0x{self.kind:02x}, {len(self.payload)} bytes)"


def encode(kind: int, payload: bytes = b"") -> bytes:
    if len(payload) > 0xFFFF:
        raise ProtocolError(f"payload too large for the 16-bit length field: {len(payload)}")
    return struct.pack("!BH", kind, len(payload)) + payload


def encode_audio(pcm: bytes, frame_size: int) -> Iterator[bytes]:
    """Split PCM into correctly sized AudioSocket messages.

    Asterisk expects a frame per message; sending 5 seconds in one packet makes
    it buffer badly, so this chunks to the caller's frame size (320 bytes =
    20 ms at 8 kHz) and pads a short tail with silence rather than truncating it.
    """
    for offset in range(0, len(pcm), frame_size):
        chunk = pcm[offset : offset + frame_size]
        if len(chunk) < frame_size:
            chunk = chunk + b"\x00" * (frame_size - len(chunk))
        yield encode(MSG_AUDIO_8000, chunk)
    if not pcm:
        return


class MessageStream:
    """Reads AudioSocket messages from a socket or file-like object."""

    def __init__(self, sock: socket.socket | object) -> None:
        self._recv = sock.recv if hasattr(sock, "recv") else sock.read
        #: True once the peer has closed the connection or the socket failed.
        #: A timeout is *not* a close: the difference decides whether a caller
        #: who hung up is noticed immediately or after the silence timeout.
        self.closed = False

    def _read_exactly(self, count: int) -> Optional[bytes]:
        chunks = []
        remaining = count
        while remaining > 0:
            try:
                chunk = self._recv(remaining)
            except (TimeoutError, socket.timeout):
                return None
            except OSError:
                self.closed = True
                return None
            if not chunk:
                self.closed = True  # EOF: the peer is gone
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def read(self) -> Optional[Message]:
        """Return the next message, or None when the peer closed or timed out."""
        header = self._read_exactly(HEADER_SIZE)
        if header is None:
            return None
        kind, length = struct.unpack("!BH", header)
        payload = b""
        if length:
            payload = self._read_exactly(length)
            if payload is None:
                raise ProtocolError(f"truncated payload: wanted {length} bytes for type 0x{kind:02x}")
        return Message(kind, payload)


class Duplex:
    """The audio interface the call session talks to.

    Real calls use :class:`SocketDuplex`; tests use :class:`MemoryDuplex`. The
    session logic therefore never touches a socket, which is what makes barge-in
    and turn-taking testable without a PBX.
    """

    rate: int = 8000

    @property
    def is_closed(self) -> bool:
        """True once the channel is gone, so the session can stop early.

        Without this a caller who hangs up mid-reply is not noticed until the
        silence timeout expires - twelve seconds of a dead line.
        """
        return False

    def send_audio(self, pcm: bytes) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def recv_audio(self, timeout: float = 0.0) -> Optional[bytes]:  # pragma: no cover
        raise NotImplementedError

    def send_dtmf(self, digit: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def hangup(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class SocketDuplex(Duplex):
    """AudioSocket over a live TCP connection."""

    def __init__(self, sock: socket.socket, rate: int = 8000) -> None:
        self.sock = sock
        self.rate = rate
        self.stream = MessageStream(sock)
        self.call_id = ""
        self._frame_size = frame_bytes(rate)
        self._closed = False
        self._dtmf: list[str] = []
        self._lock = threading.Lock()

    # ---- setup -------------------------------------------------------
    def expect_uuid(self, timeout: float = 5.0) -> str:
        """Consume the leading UUID message Asterisk sends on connect.

        The UUID identifies the channel for logging and for correlating the
        transcript with Asterisk's CDR. Some versions and the channel-interface
        variant omit it, so a first audio or DTMF message is tolerated.
        """
        self.sock.settimeout(timeout)
        while True:
            message = self.stream.read()
            if message is None:
                raise ProtocolError("connection closed before the UUID message")
            if message.kind == MSG_UUID:
                self.call_id = self._uuid_text(message.payload)
                return self.call_id
            if message.is_audio:
                # Audio before the UUID: keep the bytes for the session.
                self._prebuffer = message.payload
                self.call_id = "unknown"
                return self.call_id
            if message.kind == MSG_DTMF:
                self._dtmf.append(message.payload.decode("ascii", "replace"))
                continue
            if message.kind == MSG_HANGUP:
                raise ProtocolError("hangup received before the UUID message")
            if message.kind == MSG_ERROR:
                raise ProtocolError(str(message))

    def take_prebuffer(self) -> bytes:
        data = getattr(self, "_prebuffer", b"")
        self._prebuffer = b""
        return data

    @staticmethod
    def _uuid_text(payload: bytes) -> str:
        try:
            return str(uuid_module.UUID(bytes=payload))
        except (ValueError, AttributeError):
            return payload.hex()

    # ---- audio -------------------------------------------------------
    @property
    def is_closed(self) -> bool:
        return self._closed

    def send_audio(self, pcm: bytes) -> None:
        if self._closed:
            return
        try:
            self.sock.sendall(b"".join(encode_audio(pcm, self._frame_size)))
        except OSError:
            self._closed = True

    def recv_audio(self, timeout: float = 0.0) -> Optional[bytes]:
        """Return the next audio payload, or None if none arrived in time.

        Non-audio messages do not end the wait: a DTMF keypress that happens to
        arrive just before a frame is recorded and skipped, because returning
        None for it would make the caller think the line had gone quiet. A
        hangup or an error does end it.

        ``timeout`` of 0 means "do not wait": that is the barge-in poll, which
        must never block playback.
        """
        if self._closed:
            return None
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0.001, deadline - time.monotonic())
            self.sock.settimeout(remaining)
            try:
                message = self.stream.read()
            except (ProtocolError, OSError):
                self._closed = True
                return None
            if message is None:
                if self.stream.closed:
                    # The peer went away without a hangup message - a dropped
                    # mobile call looks exactly like this, and waiting for the
                    # silence timeout would keep the line dead for twelve
                    # seconds while the pipeline holds the resources.
                    self._closed = True
                return None
            if message.is_audio:
                return message.payload
            if message.kind == MSG_DTMF:
                self._dtmf.append(message.payload.decode("ascii", "replace"))
                continue
            if message.kind in (MSG_HANGUP, MSG_ERROR):
                if message.kind == MSG_ERROR:
                    LOG.warning("asterisk reported an error: %s", message)
                self._closed = True
                return None
            # An unknown message type is ignored rather than fatal: a future
            # Asterisk may add one, and dropping a call over it would be worse
            # than skipping it.
            LOG.debug("ignoring unknown AudioSocket message type 0x%02x", message.kind)
            if timeout <= 0:
                return None

    def take_dtmf(self) -> list[str]:
        with self._lock:
            digits, self._dtmf = self._dtmf, []
        return digits

    def send_dtmf(self, digit: str) -> None:
        if self._closed or not digit:
            return
        try:
            self.sock.sendall(encode(MSG_DTMF, digit[:1].encode("ascii")))
        except (OSError, UnicodeEncodeError):
            self._closed = True

    def send_silence(self, ms: int = 100) -> None:
        self.send_audio(silence(self.rate, ms))

    def hangup(self) -> None:
        try:
            self.sock.sendall(encode(MSG_HANGUP))
        except OSError:
            pass
        self._closed = True

    def close(self) -> None:
        self._closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class MemoryDuplex(Duplex):
    """In-memory duplex for tests: write inbound PCM, read what the agent said."""

    def __init__(self, rate: int = 8000) -> None:
        self.rate = rate
        self.inbound: list[bytes] = []
        self.outbound: list[bytes] = []
        self.dtmf_sent: list[str] = []
        self.hung_up = False
        self.closed = False
        self._lock = threading.Lock()

    @property
    def is_closed(self) -> bool:
        return self.closed

    def feed(self, pcm: bytes) -> None:
        with self._lock:
            self.inbound.append(pcm)

    def send_audio(self, pcm: bytes) -> None:
        with self._lock:
            self.outbound.append(pcm)

    def recv_audio(self, timeout: float = 0.0) -> Optional[bytes]:
        with self._lock:
            return self.inbound.pop(0) if self.inbound else None

    def send_dtmf(self, digit: str) -> None:
        self.dtmf_sent.append(digit)

    def hangup(self) -> None:
        self.hung_up = True

    def close(self) -> None:
        self.closed = True

    @property
    def spoken(self) -> bytes:
        with self._lock:
            return b"".join(self.outbound)


class AudioSocketServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(
        self,
        address: tuple[str, int],
        handler: Callable[[SocketDuplex, dict], None],
        *,
        max_calls: int = 4,
        on_call: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.handler = handler
        self.max_calls = max_calls
        self.on_call = on_call
        self._active = 0
        self._active_lock = threading.Lock()
        self.accepted = 0
        self.rejected = 0
        parent = self

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:  # noqa: D102
                with parent._active_lock:
                    if parent._active >= parent.max_calls:
                        parent.rejected += 1
                        LOG.warning("refusing call: %d already in progress", parent._active)
                        # 0xff with a code the dialplan can log; then close.
                        try:
                            self.request.sendall(encode(MSG_ERROR, bytes([4])))
                        except OSError:
                            pass
                        return
                    parent._active += 1
                    parent.accepted += 1
                duplex = SocketDuplex(self.request)
                try:
                    duplex.expect_uuid()
                    LOG.info("call %s connected (%d active)", duplex.call_id, parent._active)
                    if parent.on_call:
                        parent.on_call(duplex.call_id)
                    parent.handler(duplex, {"address": self.client_address})
                except Exception:  # noqa: BLE001 - one bad call must not kill the server
                    LOG.exception("call handler failed")
                finally:
                    duplex.close()
                    with parent._active_lock:
                        parent._active -= 1
                    LOG.info("call %s ended (%d active)", duplex.call_id, parent._active)

        super().__init__(address, _Handler)

    @property
    def active_calls(self) -> int:
        return self._active


def make_server(
    host: str,
    port: int,
    handler: Callable[[SocketDuplex, dict], None],
    *,
    max_calls: int = 4,
) -> AudioSocketServer:
    return AudioSocketServer((host, port), handler, max_calls=max_calls)
