"""The AudioSocket wire format, the server, and the duplex abstraction.

The framing details here are copied from Asterisk's documentation and are easy
to get subtly wrong (the length field is *big endian*, the header is three
bytes, one message per frame), so they are pinned with byte-level assertions
rather than round-tripped through the same helper that produced them.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
import uuid

import pytest

from voice.audiosocket import (
    AUDIO_RATES,
    MSG_AUDIO_8000,
    MSG_DTMF,
    MSG_ERROR,
    MSG_HANGUP,
    MSG_UUID,
    AudioSocketServer,
    MessageStream,
    MemoryDuplex,
    ProtocolError,
    SocketDuplex,
    encode,
    encode_audio,
)


def test_header_is_type_then_big_endian_length():
    message = encode(MSG_AUDIO_8000, b"\x01\x02\x03")
    assert message == b"\x10\x00\x03\x01\x02\x03"
    # 300 bytes must not be written as 0x2c 0x01 (little endian).
    big = encode(MSG_AUDIO_8000, b"\x00" * 300)
    assert big[:3] == b"\x10\x01\x2c"
    assert encode(MSG_HANGUP) == b"\x00\x00\x00"


def test_encode_refuses_a_payload_that_does_not_fit_the_length_field():
    with pytest.raises(ProtocolError):
        encode(MSG_AUDIO_8000, b"\x00" * 70000)


def test_every_documented_audio_rate_is_known():
    assert AUDIO_RATES[0x10] == 8000
    assert AUDIO_RATES[0x11] == 12000
    assert AUDIO_RATES[0x12] == 16000
    assert AUDIO_RATES[0x18] == 192000
    assert set(AUDIO_RATES) == set(range(0x10, 0x19))


def test_encode_audio_splits_into_frames_and_pads_the_tail():
    pcm = b"\x01\x02" * 500  # 1000 bytes = 3.125 frames of 320
    messages = list(encode_audio(pcm, 320))
    assert len(messages) == 4
    assert all(message[:1] == bytes([MSG_AUDIO_8000]) for message in messages)
    assert all(struct.unpack("!BH", message[:3])[1] == 320 for message in messages)
    assert messages[0][3:] == pcm[:320]
    assert messages[-1][3:] == pcm[960:] + b"\x00" * (320 - 40)


def test_encode_audio_of_nothing_produces_nothing():
    assert list(encode_audio(b"", 320)) == []


def test_message_stream_reads_from_a_socket_pair():
    left, right = socket.socketpair()
    try:
        right.sendall(encode(MSG_UUID, uuid.UUID(int=7).bytes) + encode(MSG_AUDIO_8000, b"\xaa" * 320))
        stream = MessageStream(left)
        first = stream.read()
        second = stream.read()
        assert first is not None and first.kind == MSG_UUID
        assert uuid.UUID(bytes=first.payload).int == 7
        assert second is not None and second.is_audio and second.rate == 8000
        assert second.payload == b"\xaa" * 320
    finally:
        left.close()
        right.close()


def test_message_stream_returns_none_on_a_closed_socket():
    left, right = socket.socketpair()
    right.close()
    try:
        assert MessageStream(left).read() is None
    finally:
        left.close()


def test_truncated_payload_is_a_protocol_error_not_silence():
    left, right = socket.socketpair()
    try:
        right.sendall(encode(MSG_AUDIO_8000, b"\x00" * 320)[:100])
        right.close()
        with pytest.raises(ProtocolError, match="truncated"):
            MessageStream(left).read()
    finally:
        left.close()


def test_socket_duplex_sends_audio_and_reads_it_back():
    left, right = socket.socketpair()
    try:
        duplex = SocketDuplex(right)
        duplex.send_audio(b"\x11" * 320)
        kind, length = struct.unpack("!BH", left.recv(3))
        assert kind == MSG_AUDIO_8000 and length == 320
        assert left.recv(320) == b"\x11" * 320
    finally:
        left.close()
        right.close()


def test_socket_duplex_consumes_the_uuid_and_keeps_dtmf_out_of_the_audio():
    left, right = socket.socketpair()
    try:
        duplex = SocketDuplex(right)
        call_uuid = uuid.uuid4()
        left.sendall(encode(MSG_UUID, call_uuid.bytes) + encode(MSG_DTMF, b"5") + encode(MSG_AUDIO_8000, b"\x01" * 320))
        assert duplex.expect_uuid(1.0) == str(call_uuid)
        assert duplex.recv_audio(1.0) == b"\x01" * 320
        assert duplex.take_dtmf() == ["5"]
    finally:
        left.close()
        right.close()


def test_socket_duplex_reports_a_hangup_as_closed():
    left, right = socket.socketpair()
    try:
        duplex = SocketDuplex(right)
        left.sendall(encode(MSG_HANGUP))
        assert duplex.recv_audio(0.5) is None
        assert duplex.is_closed is True
    finally:
        left.close()
        right.close()


def test_socket_duplex_tolerates_audio_arriving_before_the_uuid():
    """Some Asterisk builds and the AMI variant simply do not send one."""
    left, right = socket.socketpair()
    try:
        duplex = SocketDuplex(right)
        left.sendall(encode(MSG_AUDIO_8000, b"\x02" * 320))
        assert duplex.expect_uuid(1.0) == "unknown"
        assert duplex.take_prebuffer() == b"\x02" * 320
    finally:
        left.close()
        right.close()


def test_memory_duplex_can_be_marked_closed_and_reports_througput():
    duplex = MemoryDuplex()
    assert duplex.is_closed is False
    duplex.feed(b"\x01")
    assert duplex.recv_audio() == b"\x01"
    assert duplex.recv_audio() is None
    duplex.hangup()
    assert duplex.hung_up is True
    duplex.close()
    assert duplex.is_closed is True


# ----------------------------------------------------------------------
# the server: this is what Asterisk connects to
# ----------------------------------------------------------------------
class ServerHarness:
    def __init__(self, handler, **kwargs):
        self.server = AudioSocketServer(("127.0.0.1", 0), handler, **kwargs)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def connect(self) -> socket.socket:
        client = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        client.settimeout(5)
        return client

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture()
def harness_factory():
    started: list[ServerHarness] = []

    def _start(handler, **kwargs) -> ServerHarness:
        harness = ServerHarness(handler, **kwargs)
        started.append(harness)
        return harness

    yield _start

    for harness in started:
        harness.close()


def test_server_hands_a_call_to_the_handler_and_closes_cleanly(harness_factory):
    seen: dict[str, object] = {}
    done = threading.Event()

    def handler(duplex, info):
        seen["call_id"] = duplex.call_id
        seen["audio"] = duplex.recv_audio(2.0)
        duplex.send_audio(b"\x7f" * 320)
        seen["address"] = info["address"]
        done.set()

    harness = harness_factory(handler)
    client = harness.connect()
    call_uuid = uuid.uuid4()
    client.sendall(encode(MSG_UUID, call_uuid.bytes))
    client.sendall(encode(MSG_AUDIO_8000, b"\x05" * 320))
    try:
        assert done.wait(5)
        kind, length = struct.unpack("!BH", client.recv(3))
        assert (kind, length) == (MSG_AUDIO_8000, 320)
        assert client.recv(320) == b"\x7f" * 320
        assert seen["call_id"] == str(call_uuid)
        assert seen["audio"] == b"\x05" * 320
        assert seen["address"][0] == "127.0.0.1"
    finally:
        client.close()


def test_server_refuses_a_second_call_when_at_capacity(harness_factory):
    release = threading.Event()

    def handler(duplex, info):
        release.wait(5)

    harness = harness_factory(handler, max_calls=1)
    first = harness.connect()
    first.sendall(encode(MSG_UUID, uuid.UUID(int=1).bytes))
    time.sleep(0.3)

    second = harness.connect()
    second.sendall(encode(MSG_UUID, uuid.UUID(int=2).bytes))
    try:
        header = second.recv(3)
        assert header[:1] == bytes([MSG_ERROR]), "an overflowing call must get a real error, not a silent drop"
        kind, length = struct.unpack("!BH", header)
        assert length == 1
        assert second.recv(1) == b"\x04"  # "unsupported audio format" / busy
        assert harness.server.rejected == 1
        assert harness.server.accepted == 1
    finally:
        first.close()
        second.close()
        release.set()


def test_server_counts_calls_and_frees_a_slot_after_a_hangup(harness_factory):
    done = threading.Event()

    def handler(duplex, info):
        while not duplex.is_closed:
            if duplex.recv_audio(0.5) is None:
                break
        done.set()

    harness = harness_factory(handler, max_calls=1)
    client = harness.connect()
    client.sendall(encode(MSG_UUID, uuid.UUID(int=3).bytes))
    time.sleep(0.2)
    assert harness.server.active_calls == 1
    client.sendall(encode(MSG_HANGUP))
    assert done.wait(5)
    for _ in range(50):
        if harness.server.active_calls == 0:
            break
        time.sleep(0.05)
    assert harness.server.active_calls == 0, "the call slot must be released at hangup"
    client.close()


def test_a_handler_that_raises_does_not_kill_the_server(harness_factory):
    calls: list[str] = []

    def handler(duplex, info):
        calls.append(duplex.call_id)
        if len(calls) == 1:
            raise RuntimeError("simulated bug in the pipeline")
        duplex.send_audio(b"\x01" * 320)

    harness = harness_factory(handler)
    first = harness.connect()
    first.sendall(encode(MSG_UUID, uuid.UUID(int=4).bytes))
    time.sleep(0.3)
    first.close()

    second = harness.connect()
    second.sendall(encode(MSG_UUID, uuid.UUID(int=5).bytes))
    try:
        header = second.recv(3)
        assert header[:1] == bytes([MSG_AUDIO_8000])
        second.recv(320)
        assert harness.server.accepted == 2
    finally:
        second.close()
