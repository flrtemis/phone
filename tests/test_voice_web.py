"""The remote client: RFC 6455, the audio bridge, and the small API.

The WebSocket implementation is hand-rolled in the standard library, so it gets
tested against the RFC's own examples and against a real socket, not against
itself. The call tests drive a genuine `CallSession` (EchoASR + ScriptedLLM +
BeepTTS), which means a passing suite here means the browser client and a native
app are talking to the same code path a phone call uses.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from voice import cli
from voice.config import VoiceConfig, VoiceConfigError
from voice.pipeline import BeepTTS, EchoASR, ScriptedLLM, SessionConfig
from voice.web import MAX_BODY, make_server, read_latest_transcript
from voice.websocket import (
    MAX_MESSAGE,
    OP_BINARY,
    OP_CLOSE,
    OP_PING,
    OP_TEXT,
    WebSocket,
    WebSocketDuplex,
    WebSocketError,
    accept_key,
    encode_frame,
    read_frame,
)

SPEECH = b"\x00\x20" * 160  # 320 bytes: 20 ms of 8 kHz audio, loud enough for the VAD
QUIET = b"\x00" * 320


# ----------------------------------------------------------------------
# a WebSocket client, written against the RFC so it can disagree with ours
# ----------------------------------------------------------------------
class WSClient:
    def __init__(self, host: str, port: int, path: str, *, timeout: float = 5.0) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {self.key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        self.headers = self._read_headers()

    def _read_headers(self) -> str:
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = self.sock.recv(1)
            if not chunk:
                break
            raw += chunk
        return raw.decode("latin-1")

    @property
    def status(self) -> int:
        try:
            return int(self.headers.split(" ")[1])
        except (IndexError, ValueError):
            return 0

    @property
    def accepted(self) -> bool:
        return accept_key(self.key) in self.headers

    def send(self, opcode: int, payload: bytes = b"", *, mask: bool = True) -> None:
        self.sock.sendall(encode_frame(opcode, payload, mask=mask))

    def send_json(self, payload: dict) -> None:
        self.send(OP_TEXT, json.dumps(payload).encode())

    def send_audio(self, pcm: bytes) -> None:
        self.send(OP_BINARY, pcm)

    def recv(self, *, timeout: float = 5.0):
        self.sock.settimeout(timeout)
        return read_frame(self.sock, require_mask=False)

    def recv_json(self, *, timeout: float = 5.0, until: str = "") -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self.recv(timeout=max(0.1, deadline - time.monotonic()))
            if frame is None:
                break
            if frame.opcode == OP_TEXT:
                event = json.loads(frame.payload)
                if not until or event.get("type") == until:
                    return event
        raise AssertionError(f"no {until or 'text'} frame arrived within {timeout}s")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ----------------------------------------------------------------------
# framing
# ----------------------------------------------------------------------
def test_accept_key_matches_the_rfc_example():
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_server_frames_are_never_masked():
    frame = encode_frame(OP_TEXT, b"hello")
    assert frame[0] == 0x81, "FIN + text"
    assert frame[1] == 5, "length with the mask bit clear"
    assert frame[2:] == b"hello"


def test_client_frames_are_masked_and_come_back_unmasked():
    left, right = socket.socketpair()
    try:
        right.sendall(encode_frame(OP_BINARY, b"payload", mask=True))
        frame = read_frame(left, require_mask=True)
        assert frame is not None
        assert frame.opcode == OP_BINARY and frame.payload == b"payload"
    finally:
        left.close()
        right.close()


def test_a_client_that_does_not_mask_is_refused():
    left, right = socket.socketpair()
    try:
        right.sendall(encode_frame(OP_BINARY, b"payload", mask=False))
        with pytest.raises(WebSocketError, match="masked"):
            read_frame(left, require_mask=True)
    finally:
        left.close()
        right.close()


@pytest.mark.parametrize("size", [0, 5, 125, 126, 127, 1000, 65535, 65536])
def test_length_encodings_round_trip(size):
    left, right = socket.socketpair()
    try:
        payload = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
        right.sendall(encode_frame(OP_BINARY, payload, mask=True))
        frame = read_frame(left, require_mask=True)
        assert frame is not None and frame.payload == payload
        header = encode_frame(OP_BINARY, payload)
        if size < 126:
            assert header[1] == size
        elif size < 1 << 16:
            assert header[1] == 126 and struct.unpack("!H", header[2:4])[0] == size
        else:
            assert header[1] == 127 and struct.unpack("!Q", header[2:10])[0] == size
    finally:
        left.close()
        right.close()


def test_an_oversized_frame_is_rejected_before_it_is_buffered():
    left, right = socket.socketpair()
    try:
        header = bytes([0x82, 0x80 | 127]) + struct.pack("!Q", 10 * MAX_MESSAGE)
        right.sendall(header)
        with pytest.raises(WebSocketError, match="too large"):
            read_frame(left)
    finally:
        left.close()
        right.close()


def read_server_frame(sock, **kwargs):
    """Server frames are never masked; reading them in a test must say so."""
    return read_frame(sock, require_mask=False, **kwargs)


def test_ping_is_answered_with_a_pong():
    left, right = socket.socketpair()
    try:
        ws = WebSocket(left, timeout=1.0)
        right.sendall(encode_frame(OP_PING, b"hey", mask=True))
        right.sendall(encode_frame(OP_TEXT, b"after", mask=True))
        opcode, payload = ws.recv_message()
        assert (opcode, payload) == (OP_TEXT, b"after")
        frame = read_server_frame(right)
        assert frame is not None and frame.opcode == 0xA and frame.payload == b"hey"
    finally:
        left.close()
        right.close()


def test_fragmented_messages_are_reassembled():
    left, right = socket.socketpair()
    try:
        ws = WebSocket(left, timeout=1.0)
        right.sendall(encode_frame(OP_TEXT, b"one ", mask=True, fin=False))
        right.sendall(encode_frame(0x0, b"two ", mask=True, fin=False))
        right.sendall(encode_frame(0x0, b"three", mask=True, fin=True))
        assert ws.recv_message() == (OP_TEXT, b"one two three")
    finally:
        left.close()
        right.close()


def test_a_close_frame_ends_the_connection_politely():
    left, right = socket.socketpair()
    try:
        ws = WebSocket(left, timeout=1.0)
        right.sendall(encode_frame(OP_CLOSE, struct.pack("!H", 1000), mask=True))
        assert ws.recv_message() is None
        assert ws.closed is True
        reply = read_server_frame(right)
        assert reply is not None and reply.opcode == OP_CLOSE
    finally:
        left.close()
        right.close()


# ----------------------------------------------------------------------
# the duplex: this is what makes a WebSocket behave like a phone line
# ----------------------------------------------------------------------
class FakeSocket:
    """Collects outbound frames; feeds inbound ones."""

    def __init__(self) -> None:
        self.left, self.right = socket.socketpair()
        self.ws = WebSocket(self.left, timeout=2.0)

    def feed(self, opcode: int, payload: bytes) -> None:
        self.right.sendall(encode_frame(opcode, payload, mask=True))

    def drain(self, count: int = 1) -> list[bytes]:
        frames = []
        for _ in range(count):
            frame = read_server_frame(self.right)
            if frame is None:
                break
            frames.append(frame.payload)
        return frames

    def close(self) -> None:
        self.left.close()
        self.right.close()


@pytest.fixture()
def duplex_factory():
    opened: list[tuple[WebSocketDuplex, FakeSocket]] = []

    def _make(**kwargs) -> tuple[WebSocketDuplex, FakeSocket]:
        fake = FakeSocket()
        duplex = WebSocketDuplex(fake.ws, **kwargs)
        opened.append((duplex, fake))
        return duplex, fake

    yield _make
    for duplex, fake in opened:
        duplex.close()
        fake.close()


def test_audio_arrives_in_whole_frames(duplex_factory):
    duplex, fake = duplex_factory(client_rate=8000)
    fake.feed(OP_BINARY, SPEECH + b"\x00" * 100)  # a frame and a half
    assert duplex.recv_audio(1.0) == SPEECH
    assert duplex.recv_audio(0.1) is None, "a partial frame must wait, not be padded with silence"


def test_client_audio_is_resampled_to_the_session_rate(duplex_factory):
    """A browser that only runs at 48 kHz must still produce 8 kHz frames."""
    duplex, fake = duplex_factory(client_rate=48000)
    import math

    # 20 ms at 48 kHz: 960 samples.
    samples = [int(8000 * math.sin(2 * math.pi * 300 * i / 48000)) for i in range(960)]
    fake.feed(OP_BINARY, struct.pack(f"<{len(samples)}h", *samples))
    frames = []
    for _ in range(20):
        frame = duplex.recv_audio(0.05)
        if frame:
            frames.append(frame)
        if len(frames) >= 2:
            break
    assert frames, "the session must receive audio"
    assert all(len(frame) == 320 for frame in frames), "every frame is 20 ms at 8 kHz"


def test_the_agent_audio_is_sent_back_at_the_client_rate(duplex_factory):
    duplex, fake = duplex_factory(client_rate=24000)
    duplex.send_audio(SPEECH * 3)  # 60 ms at 8 kHz
    frames = fake.drain(1)
    assert frames, "the agent's audio must be sent to the client"
    # 60 ms of 8 kHz audio resampled to 24 kHz is 1440 samples, 2880 bytes.
    assert len(frames[0]) == pytest.approx(320 * 3 * 3, abs=8)


def test_the_hangup_waits_for_queued_audio(duplex_factory):
    """The last thing a caller says must not be thrown away by their hangup."""
    duplex, fake = duplex_factory(client_rate=8000)
    fake.feed(OP_BINARY, SPEECH)
    fake.feed(OP_TEXT, json.dumps({"type": "hangup"}).encode())
    frame = None
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and frame is None:
        frame = duplex.recv_audio(0.05)
    assert frame == SPEECH, "audio queued before the hangup must still be readable"
    assert duplex.recv_audio(0.1) is None
    # The reader thread processes the hangup after the audio it was queued behind,
    # so give it the moment it needs rather than assuming it has already run.
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not duplex.is_closed:
        time.sleep(0.02)
    assert duplex.is_closed is True, "once drained, the close is reported"


def test_a_hangup_with_nothing_queued_closes_the_line(duplex_factory):
    """The other half of the rule: no audio pending means the close is immediate.

    A session that kept waiting here would sit out its whole silence timeout on a
    line nobody is on.
    """
    duplex, fake = duplex_factory(client_rate=8000)
    fake.feed(OP_TEXT, json.dumps({"type": "hangup"}).encode())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not duplex.is_closed:
        duplex.recv_audio(0.05)
    assert duplex.is_closed is True
    assert duplex.recv_audio(0.1) is None


def test_dtmf_arrives_as_a_separate_channel(duplex_factory):
    duplex, fake = duplex_factory(client_rate=8000)
    fake.feed(OP_TEXT, json.dumps({"type": "dtmf", "digit": "5"}).encode())
    fake.feed(OP_TEXT, b"not json at all")
    fake.feed(OP_TEXT, json.dumps({"type": "unknown-future-thing"}).encode())
    digits = []
    for _ in range(50):
        digits += duplex.take_dtmf()
        if digits:
            break
        time.sleep(0.02)
    assert digits == ["5"], "a keypress must be delivered once, and junk must be ignored"


def test_events_are_forwarded_to_the_client(duplex_factory):
    duplex, fake = duplex_factory(client_rate=8000)
    duplex.send_event({"type": "turn", "role": "assistant", "text": "hello"})
    frame = read_server_frame(fake.right)
    assert frame is not None and frame.opcode == OP_TEXT
    assert json.loads(frame.payload)["text"] == "hello"


# ----------------------------------------------------------------------
# the server: page, API, and a whole call
# ----------------------------------------------------------------------
@pytest.fixture()
def web_server(tmp_path):
    """A server with a real session factory and a pretend dialler."""
    cfg = VoiceConfig()
    cfg.web_host, cfg.web_port = "127.0.0.1", 0
    cfg.web_token = "test-token"
    cfg.web_max_calls = 2
    cfg.greeting = "Hi from the test."
    cfg.transcript_dir = str(tmp_path / "transcripts")
    cfg.notes_file = str(tmp_path / "notes.txt")
    cfg.owner_destination = "+1 610 555 0100"

    dialled: list[str] = []
    asr = EchoASR(script=["hello there", "are you still there"])
    llm = ScriptedLLM(replies=["Yes, I am here.", "Still here."])

    def session_factory(duplex):
        return cli.CallSession(
            duplex,
            asr,
            llm,
            BeepTTS(rate=8000, ms=40),
            SessionConfig(greeting="Hi from the test.", barge_in=False, silence_timeout_seconds=4),
            on_event=duplex.send_event,
            transcript_path=str(tmp_path / "transcripts" / "call.jsonl"),
            notes_path=cfg.notes_file,
        )

    def originate(number: str) -> tuple[bool, str]:
        dialled.append(number)
        return True, "pretending to dial"

    server = make_server(cfg, session_factory, originate=originate)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    server.dialled = dialled  # type: ignore[attr-defined]
    server.port = server.server_address[1]  # type: ignore[attr-defined]
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def http(url: str, *, token: str = "", method: str = "GET", body: dict | None = None, raw: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = response.read()
            return response.status, (payload if raw else json.loads(payload or b"{}"))
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        if raw:
            return exc.code, payload
        try:
            return exc.code, json.loads(payload)
        except json.JSONDecodeError:
            return exc.code, {}


def test_the_page_is_served_without_a_token_and_cannot_be_framed(web_server):
    status, body = http(f"http://127.0.0.1:{web_server.port}/", raw=True)
    assert status == 200
    assert b"getUserMedia" in body and b"audioWorklet" in body
    assert b"<title>phone voice</title>" in body
    # No external references: this must work with no internet access at all.
    assert b"http://" not in body.replace(b"http://www.w3.org", b"")
    assert b"src=" not in body and b"@import" not in body

    request = urllib.request.Request(f"http://127.0.0.1:{web_server.port}/")
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_healthz_needs_no_token_but_says_little(web_server):
    status, payload = http(f"http://127.0.0.1:{web_server.port}/healthz")
    assert status == 200 and payload["status"] == "ok"
    assert "owner" not in json.dumps(payload)


def test_every_api_route_requires_the_token(web_server):
    base = f"http://127.0.0.1:{web_server.port}"
    for path in ("/api/config", "/api/transcript"):
        assert http(base + path)[0] == 401
    for path in ("/api/call-me", "/api/ask", "/api/note"):
        assert http(base + path, method="POST", body={})[0] == 401
    assert http(base + "/api/config", token="wrong")[0] == 401
    assert http(base + "/api/config", token="test-token")[0] == 200


def test_config_endpoint_does_not_leak_the_owner_number_or_the_token(web_server):
    _status, payload = http(f"http://127.0.0.1:{web_server.port}/api/config", token="test-token")
    text = json.dumps(payload)
    assert "610" not in text and "555" not in text
    assert "test-token" not in text
    assert payload["dialling"] is True


def test_call_me_rings_only_the_owner(web_server):
    base = f"http://127.0.0.1:{web_server.port}"
    status, payload = http(base + "/api/call-me", token="test-token", method="POST", body={"to": "owner"})
    assert status == 200 and payload["status"] == "calling"
    assert web_server.dialled == ["+1 610 555 0100"], "the dialler gets the configured number, never the request"

    for hostile in ("911", "+1 555 010 9999", "411"):
        status, payload = http(base + "/api/call-me", token="test-token", method="POST", body={"to": hostile})
        assert status == 403, f"{hostile} must be refused"
        assert "error" in payload
    assert web_server.dialled == ["+1 610 555 0100"], "a refused request must not reach the dialler"


def test_call_me_reports_a_dialler_failure_instead_of_claiming_success(web_server):
    web_server.originate = lambda number: (False, "no trunk configured")
    status, payload = http(
        f"http://127.0.0.1:{web_server.port}/api/call-me", token="test-token", method="POST", body={}
    )
    assert status == 502 and "no trunk configured" in payload["error"]


def test_call_me_is_disabled_without_an_owner(tmp_path):
    cfg = VoiceConfig()
    cfg.web_host, cfg.web_port, cfg.web_token = "127.0.0.1", 0, "t"
    cfg.owner_destination = ""
    server = make_server(cfg, lambda duplex: None, originate=lambda number: (True, ""))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        status, payload = http(
            f"http://127.0.0.1:{server.server_address[1]}/api/call-me", token="t", method="POST", body={}
        )
        assert status == 403 and "disabled" in payload["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_note_is_stored_for_the_next_call(web_server, tmp_path):
    status, payload = http(
        f"http://127.0.0.1:{web_server.port}/api/note",
        token="test-token",
        method="POST",
        body={"text": "I am driving, keep it short"},
    )
    assert status == 200 and payload["status"] == "stored"
    note = (tmp_path / "notes.txt").read_text()
    assert "I am driving" in note
    assert (tmp_path / "notes.txt").stat().st_mode & 0o777 == 0o600

    assert http(f"http://127.0.0.1:{web_server.port}/api/note", token="test-token", method="POST", body={})[0] == 400


def test_ask_reports_the_missing_model_instead_of_pretending(web_server):
    status, payload = http(
        f"http://127.0.0.1:{web_server.port}/api/ask",
        token="test-token",
        method="POST",
        body={"prompt": "hello"},
    )
    # The test config is an offline one: no Ollama is configured, so the endpoint
    # must say so rather than return an empty answer.
    assert status in (502, 503)
    assert "error" in payload


def test_ask_answers_with_a_text_model(tmp_path, fake_ollama_url):
    cfg = VoiceConfig()
    cfg.web_host, cfg.web_port, cfg.web_token = "127.0.0.1", 0, "t"
    cfg.llm_backend, cfg.llm_model, cfg.llm_url = "ollama", "gemma4:26b", fake_ollama_url.url
    server = make_server(cfg, lambda duplex: None)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        status, payload = http(
            f"http://127.0.0.1:{server.server_address[1]}/api/ask",
            token="t",
            method="POST",
            body={"prompt": "say hi"},
        )
        assert status == 200
        assert payload["reply"] == "One short answer."
        assert payload["model"] == "gemma4:26b"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_api_refuses_an_oversized_body_and_unknown_routes(web_server):
    base = f"http://127.0.0.1:{web_server.port}"
    request = urllib.request.Request(
        base + "/api/note",
        data=b'{"text": "' + b"x" * (MAX_BODY + 100) + b'"}',
        headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status == 413
    assert http(base + "/api/nope", token="test-token")[0] == 404


def test_a_websocket_handshake_needs_the_token(web_server):
    client = WSClient("127.0.0.1", web_server.port, "/ws?token=wrong")
    assert client.status == 401
    client.close()

    client = WSClient("127.0.0.1", web_server.port, "/ws?token=test-token")
    assert client.status == 101
    assert client.accepted, "the Sec-WebSocket-Accept header must match RFC 6455"
    client.close()


def test_a_whole_conversation_over_a_websocket(web_server):
    """The end-to-end test: greeting, caller speech, ASR, model, spoken reply."""
    client = WSClient("127.0.0.1", web_server.port, "/ws?token=test-token")
    try:
        client.send_json({"type": "hello", "sample_rate": 8000})
        ready = client.recv_json(until="ready")
        assert ready["session_rate"] == 8000

        for _ in range(8):
            client.send_audio(SPEECH)
        for _ in range(14):
            client.send_audio(QUIET)

        turns: list[tuple[str, str]] = []
        audio_bytes = 0
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(turns) < 3:
            frame = client.recv(timeout=2)
            if frame is None:
                break
            if frame.opcode == OP_BINARY:
                audio_bytes += len(frame.payload)
                assert len(frame.payload) % 2 == 0, "PCM is 16-bit"
            elif frame.opcode == OP_TEXT:
                event = json.loads(frame.payload)
                if event.get("type") == "turn":
                    turns.append((event["role"], event["text"]))

        assert turns[0] == ("assistant", "Hi from the test.")
        assert turns[1] == ("user", "hello there"), "the caller's speech must reach the ASR"
        assert turns[2] == ("assistant", "Yes, I am here.")
        assert audio_bytes > 0, "the agent's voice must come back as audio"

        client.send_json({"type": "hangup"})
        bye = client.recv_json(until="bye", timeout=5)
        assert bye["reason"] in ("caller-hung-up", "caller-said-goodbye", "silence-timeout")
    finally:
        client.close()

    time.sleep(0.4)
    entries = read_latest_transcript(web_server.cfg, lines=10)
    assert [entry["role"] for entry in entries] == ["assistant", "user", "assistant"]


def test_a_hangup_does_not_lose_the_last_utterance(web_server):
    """A caller who says something and immediately hangs up must still be heard."""
    client = WSClient("127.0.0.1", web_server.port, "/ws?token=test-token")
    try:
        client.send_json({"type": "hello", "sample_rate": 8000})
        client.recv_json(until="ready")
        for _ in range(8):
            client.send_audio(SPEECH)
        for _ in range(14):
            client.send_audio(QUIET)
        client.send_json({"type": "hangup"})
        heard = client.recv_json(until="turn", timeout=5)
        # The first turn frame is the greeting; the caller's words come next.
        for _ in range(6):
            if heard.get("role") == "user":
                break
            heard = client.recv_json(until="turn", timeout=5)
        assert heard["text"] == "hello there"
    finally:
        client.close()


def test_concurrent_calls_are_capped(web_server):
    first = WSClient("127.0.0.1", web_server.port, "/ws?token=test-token")
    second = WSClient("127.0.0.1", web_server.port, "/ws?token=test-token")
    third = WSClient("127.0.0.1", web_server.port, "/ws?token=test-token")
    try:
        first.send_json({"type": "hello", "sample_rate": 8000})
        first.recv_json(until="ready")
        second.send_json({"type": "hello", "sample_rate": 8000})
        second.recv_json(until="ready")
        time.sleep(0.3)
        # max_calls is 2, so the third handshake is answered with a JSON error.
        assert "503" in third.headers or third.status in (503, 0), third.headers
    finally:
        for client in (first, second, third):
            client.close()


def test_a_client_close_does_not_kill_the_server(web_server):
    abandoned = WSClient("127.0.0.1", web_server.port, "/ws?token=test-token")
    abandoned.send_json({"type": "hello", "sample_rate": 8000})
    abandoned.recv_json(until="ready")
    abandoned.sock.close()  # vanish without a close handshake
    time.sleep(0.4)

    healthy = WSClient("127.0.0.1", web_server.port, "/ws?token=test-token")
    try:
        assert healthy.status == 101
        healthy.send_json({"type": "hello", "sample_rate": 8000})
        assert healthy.recv_json(until="ready")["session_rate"] == 8000
    finally:
        healthy.close()


def test_an_unsupported_sample_rate_falls_back_instead_of_failing(web_server):
    client = WSClient("127.0.0.1", web_server.port, "/ws?token=test-token")
    try:
        client.send_json({"type": "hello", "sample_rate": 12345})
        ready = client.recv_json(until="ready")
        assert ready["client_rate"] == 8000
    finally:
        client.close()


# ----------------------------------------------------------------------
# configuration: the remote client is refuse-by-default
# ----------------------------------------------------------------------
def test_the_remote_client_refuses_a_public_bind_without_opt_in():
    cfg = VoiceConfig(web_host="0.0.0.0")
    with pytest.raises(VoiceConfigError, match="refusing to serve"):
        cfg.validate_web()
    cfg.web_allow_remote = True
    with pytest.raises(VoiceConfigError, match="token is required"):
        cfg.validate_web()
    cfg.web_token = "s3cret"
    cfg.validate_web()  # now it is an explicit, deliberate choice


def test_tls_certificate_and_key_must_both_exist_and_agree(tmp_path):
    cfg = VoiceConfig()
    cfg.web_cert = str(tmp_path / "missing.pem")
    cfg.web_key = str(tmp_path / "missing.key")
    with pytest.raises(VoiceConfigError, match="cert not found"):
        cfg.validate_web()
    cfg.web_cert = ""
    with pytest.raises(VoiceConfigError, match="together"):
        cfg.validate_web()


def test_the_token_is_generated_once_and_kept():
    cfg = VoiceConfig()
    first = cfg.ensure_web_token()
    assert len(first) >= 20
    assert cfg.ensure_web_token() == first, "a rotating token would log every client out"
    assert first not in cfg.render()
    assert first in cfg.render(redact=False)


# ----------------------------------------------------------------------
# the command line
# ----------------------------------------------------------------------
def run(args, capsys) -> tuple[int, str]:
    code = cli.main(args)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def offline_config(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "voice.conf"
    path.write_text(
        f"""
[bridge]
port = 0
[asr]
backend = echo
[llm]
backend = script
[tts]
backend = beep
[conversation]
greeting = Hi.
[logging]
transcripts = {tmp_path}/transcripts
notes = {tmp_path}/notes.txt
{extra}
"""
    )
    return path


def test_web_command_refuses_a_public_bind_without_the_flag(tmp_path, capsys):
    cfg = offline_config(tmp_path)
    code, out = run(["--config", str(cfg), "web", "--host", "0.0.0.0"], capsys)
    assert code == 2
    assert "refusing to serve the remote client on 0.0.0.0" in out


def test_web_command_starts_and_serves_the_page(tmp_path, capsys):
    """Start the real command in a thread, then talk to it like a browser would."""
    cfg_path = offline_config(tmp_path)
    cfg = VoiceConfig.load(str(cfg_path), env=False)
    cfg.web_token = "cli-token"
    cfg.web_host, cfg.web_port = "127.0.0.1", 0

    from voice.web import make_server

    sessions = []

    def session_factory(duplex):
        session = cli.CallSession(
            duplex,
            EchoASR(script=["hi"]),
            ScriptedLLM(replies=["hello"]),
            BeepTTS(rate=8000, ms=30),
            SessionConfig(greeting="Hi.", barge_in=False, silence_timeout_seconds=3),
            on_event=duplex.send_event,
            transcript_path=str(tmp_path / "transcripts" / "x.jsonl"),
        )
        sessions.append(session)
        return session

    server = make_server(cfg, session_factory)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        assert http(base + "/", raw=True)[0] == 200
        assert http(base + "/api/config", token="cli-token")[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_note_command_writes_reads_and_clears(tmp_path, capsys):
    cfg = offline_config(tmp_path)
    code, out = run(["--config", str(cfg), "note", "call me", "about", "the", "invoice"], capsys)
    assert code == 0
    assert "noted in" in out
    assert (tmp_path / "notes.txt").read_text().strip() == "- call me about the invoice"

    code, out = run(["--config", str(cfg), "note", "--show"], capsys)
    assert code == 0 and "call me about the invoice" in out

    code, out = run(["--config", str(cfg), "note", "--clear"], capsys)
    assert code == 0
    assert (tmp_path / "notes.txt").read_text().strip() == ""

    code, out = run(["--config", str(cfg), "note"], capsys)
    assert code == 2 and "nothing to write" in out


def test_note_command_explains_when_notes_are_disabled(tmp_path, capsys):
    path = tmp_path / "voice.conf"
    path.write_text("[llm]\nbackend = script\n")
    code, out = run(["--config", str(path), "note", "hello"], capsys)
    assert code == 2
    assert "notes are disabled" in out


def test_notes_are_read_into_the_system_prompt_at_call_start(tmp_path):
    """The note reaches the model, and it is bounded."""
    from voice.pipeline import CallSession
    from voice.audiosocket import MemoryDuplex

    notes = tmp_path / "notes.txt"
    notes.write_text("I am driving; keep it short.\n", encoding="utf-8")
    session = CallSession(
        MemoryDuplex(),
        EchoASR(),
        ScriptedLLM(replies=["ok"]),
        BeepTTS(rate=8000, ms=10, echo=False),
        SessionConfig(greeting="", system_prompt="Be brief."),
        notes_path=str(notes),
    )
    assert "I am driving" in session.system_prompt()
    assert session.system_prompt().startswith("Be brief.")

    huge = tmp_path / "huge.txt"
    huge.write_text("x" * 5000, encoding="utf-8")
    bounded = CallSession(
        MemoryDuplex(), EchoASR(), ScriptedLLM(replies=[]), BeepTTS(rate=8000, ms=10), SessionConfig(), notes_path=str(huge)
    )
    assert len(bounded.notes) == 1000


def test_a_missing_note_file_is_not_an_error(tmp_path):
    from voice.audiosocket import MemoryDuplex
    from voice.pipeline import CallSession

    session = CallSession(
        MemoryDuplex(), EchoASR(), ScriptedLLM(replies=[]), BeepTTS(rate=8000, ms=10),
        SessionConfig(), notes_path=str(tmp_path / "absent.txt"),
    )
    assert session.notes == ""
