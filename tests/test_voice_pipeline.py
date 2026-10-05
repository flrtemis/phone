"""Turn-taking, barge-in, transcript, dial safety, and the model adapters.

The session is driven through MemoryDuplex, so every one of these tests runs
without a PBX, a sound card, or a network - which is the only reason it is
possible to test a phone agent at all.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time

import pytest

from voice.audio import SAMPLE_WIDTH, silence, tone, wav_read, wav_write
from voice.audiosocket import MemoryDuplex
from voice.pipeline import (
    BeepTTS,
    CallSession,
    DialRequest,
    EchoASR,
    OllamaLLM,
    PipelineError,
    PiperTTS,
    ScriptedLLM,
    SessionConfig,
    WavTTS,
    WhisperASR,
    extract_dial_request,
    is_emergency,
    normalise_number,
    run_wav_session,
    validate_dial_request,
)

SPEECH = tone(8000, 300, 20, amplitude=9000)
QUIET = silence(8000, 20)


def config(**overrides) -> SessionConfig:
    cfg = SessionConfig(
        greeting="",
        farewell="Goodbye.",
        owner_destination="+1 610 555 0100",
        barge_in_threshold=700,
        silence_timeout_seconds=0.4,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def utterance(frame_count: int = 8, quiet_frames: int = 14) -> list[bytes]:
    """One spoken turn: enough loud frames to count as speech, then silence."""
    return [SPEECH] * frame_count + [QUIET] * quiet_frames


class PacedDuplex(MemoryDuplex):
    """A duplex that runs the session in a thread and can be fed while it runs."""

    def __init__(self, rate: int = 8000) -> None:
        super().__init__(rate)
        self.done = threading.Event()

    def run(self, session: CallSession) -> threading.Thread:
        def target():
            try:
                session.run()
            finally:
                self.done.set()

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread

    def wait(self, timeout: float = 5.0) -> bool:
        return self.done.wait(timeout)


def test_session_greets_then_answers_and_records_timings(tmp_path):
    transcript = tmp_path / "call.jsonl"
    duplex = MemoryDuplex()
    for frame in utterance():
        duplex.feed(frame)
    asr = EchoASR(script=["what is the weather"])
    llm = ScriptedLLM(replies=["It is raining on your machine."])
    tts = WavTTS(fallback_rate=8000)

    session = CallSession(
        duplex, asr, llm, tts, config(greeting="Hello, go ahead."), transcript_path=str(transcript)
    )
    reason = session.run()

    assert reason == "silence-timeout"
    assert asr.seen, "the utterance must reach the ASR"
    assert duck_typed_length(asr.seen[0]) >= 8 * 320
    assert llm.seen and llm.seen[0][0]["content"] == "what is the weather"
    assert len(session.turns) == 1
    assert session.turns[0].tts_ms >= 0

    entries = [json.loads(line) for line in transcript.read_text().splitlines()]
    assert [e["role"] for e in entries] == ["assistant", "user", "assistant"]
    assert entries[1]["text"] == "what is the weather"


def duck_typed_length(data: bytes) -> int:
    assert isinstance(data, (bytes, bytearray))
    return len(data)


def test_a_click_is_not_treated_as_an_utterance():
    """Two loud frames (40 ms) are a click: below the VAD's onset, no ASR call."""
    duplex = MemoryDuplex()
    for frame in [SPEECH] * 2 + [QUIET] * 20:
        duplex.feed(frame)
    asr = EchoASR(script=["should not be used"])
    session = CallSession(duplex, asr, ScriptedLLM(replies=["no"]), WavTTS(), config())
    reason = session.run()
    assert session.turns == []
    assert asr.seen == [], "a click must never reach the recogniser"
    assert reason == "silence-timeout"


def test_barge_in_stops_playback_and_keeps_the_interrupting_audio():
    """The whole point of a barge-in: nothing the caller said is lost."""
    duplex = MemoryDuplex()
    tts = BeepTTS(rate=8000, ms=1000, echo=False)  # long enough to be interrupted
    asr = EchoASR(script=["hello there"])
    session = CallSession(duplex, asr, ScriptedLLM(replies=["I hear you."]), tts, config(greeting="A long greeting."))

    # The caller starts talking while the greeting plays.
    for frame in utterance():
        duplex.feed(frame)
    session.run()

    assert session.turns, "the interrupted utterance must still be processed"
    assert len(asr.seen[0]) >= 8 * 320, "the opening frames of the utterance must survive the barge-in"
    assert duplex.spoken, "the greeting must have been sent before it was cut off"


def test_playback_is_paced_in_real_time():
    """A 1-second reply must take about a second to send, not zero time."""
    duplex = MemoryDuplex()
    tts = BeepTTS(rate=8000, ms=500, echo=False)  # two 500 ms tones = 1 s of audio
    session = CallSession(duplex, EchoASR(), ScriptedLLM(replies=[]), tts, config(barge_in=False))
    started = time.monotonic()
    session._speak("one second of audio")
    elapsed = time.monotonic() - started
    assert 0.9 < elapsed < 1.6, f"playback took {elapsed:.2f}s for 1.0s of audio"
    assert len(duplex.spoken) // (SAMPLE_WIDTH * 8000) == pytest.approx(1.0, abs=0.05)


def test_a_hangup_is_noticed_immediately_instead_of_after_a_timeout():
    duplex = MemoryDuplex()
    duplex.close()
    session = CallSession(duplex, EchoASR(), ScriptedLLM(replies=[]), WavTTS(), config())
    started = time.monotonic()
    reason = session.run()
    elapsed = time.monotonic() - started
    assert reason == "caller-hung-up"
    assert elapsed < 1.0, f"a dead line took {elapsed:.2f}s to notice"


def test_saying_goodbye_ends_the_call_with_the_farewell():
    duplex = MemoryDuplex()
    for frame in utterance():
        duplex.feed(frame)
    tts = BeepTTS(rate=8000, ms=20, echo=False)
    session = CallSession(duplex, EchoASR(script=["that's all, goodbye"]), ScriptedLLM(replies=["never used"]), tts, config())
    reason = session.run()
    assert reason == "caller-said-goodbye"
    assert tts.spoken[-1] == "Goodbye."


def test_a_very_long_model_reply_is_truncated_before_it_holds_the_line():
    duplex = MemoryDuplex()
    for frame in utterance():
        duplex.feed(frame)

    class Chatterbox:
        def stream(self, messages, system=""):
            for _ in range(400):
                yield "word "

    tts = BeepTTS(rate=8000, ms=5, echo=False)
    session = CallSession(duplex, EchoASR(script=["tell me everything"]), Chatterbox(), tts, config())
    session.run()
    spoken = " ".join(tts.spoken)
    assert len(spoken) <= 420, f"truncation failed: {len(spoken)} characters were synthesized"


def test_max_turns_bounds_a_call_that_never_ends():
    duplex = MemoryDuplex()
    for _ in range(6):
        for frame in utterance():
            duplex.feed(frame)
    session = CallSession(
        duplex, EchoASR(script=["again", "again", "again"]), ScriptedLLM(replies=["ok", "ok", "ok"]), WavTTS(),
        config(max_turns=3, silence_timeout_seconds=0.1),
    )
    reason = session.run()
    assert reason == "max-turns"
    assert len(session.turns) == 3


def test_a_missing_tts_voice_does_not_kill_the_call():
    duplex = MemoryDuplex()
    for frame in utterance():
        duplex.feed(frame)
    # No WAV path and no fallback: synthesize() returns silence, not an exception.
    session = CallSession(duplex, EchoASR(script=["hello"]), ScriptedLLM(replies=["hi"]), WavTTS(""), config())
    session.run()
    assert len(session.turns) == 1


# ----------------------------------------------------------------------
# dial safety: the model proposes, the code decides
# ----------------------------------------------------------------------
def test_dial_directive_is_extracted_and_hidden_from_speech():
    spoken, dial = extract_dial_request("I will call you back. [[dial:+1 610 555 0100|checking in]]")
    assert spoken == "I will call you back."
    assert dial is not None and dial.destination == "+1 610 555 0100" and dial.reason == "checking in"


def test_a_dial_directive_writeen_on_multiple_lines_still_extracts():
    spoken, dial = extract_dial_request("Sure.\n[[dial:owner|you asked me to]]\nAnything else?")
    assert dial is not None and dial.destination == "owner"


@pytest.mark.parametrize("number", ["911", "999", "112", "0", "411", ""])
def test_emergency_and_service_numbers_are_refused_even_if_the_model_asks(number):
    allowed, _reason = validate_dial_request(DialRequest(number), "+1 610 555 0100")
    assert allowed is False


def test_every_emergency_number_is_recognised_however_it_is_written():
    for number in ("911", "9-1-1", "(911)", "911", " 911 ", "999", "112", "000", "988", "0"):
        assert is_emergency(number), f"{number!r} must be recognised as an emergency/service number"
    assert not is_emergency("+1 610 555 0100")
    assert not is_emergency("9111"), "a number that merely starts with 911 is a normal number"


def test_emergency_numbers_are_refused_by_the_validator_even_for_if_they_look_like_the_owner():
    """The emergency check must not be a side effect of the owner comparison."""
    for number in ("911", "988", "999", "0"):
        allowed, reason = validate_dial_request(DialRequest(number), "+1 610 555 0100")
        assert allowed is False
        assert "emergency" in reason or "service" in reason, f"{number} was refused for the wrong reason: {reason}"


def test_the_config_refuses_to_make_an_emergency_number_the_owner(tmp_path):
    from voice.config import VoiceConfig, VoiceConfigError

    path = tmp_path / "voice.conf"
    path.write_text("[dial]\nowner = 911\n")
    with pytest.raises(VoiceConfigError, match="emergency or service"):
        VoiceConfig.load(str(path), env=False)


def test_only_the_exact_owner_number_is_allowed():
    owner = "+1 (610) 555-0100"
    assert validate_dial_request(DialRequest("+16105550100"), owner)[0] is True
    assert validate_dial_request(DialRequest("+16105550199"), owner)[0] is False
    # A number that merely starts with the owner's digits must not pass.
    assert validate_dial_request(DialRequest("+161055501001"), owner)[0] is False


def test_outbound_dialling_is_disabled_without_a_configured_owner():
    allowed, reason = validate_dial_request(DialRequest("+16105550100"), "")
    assert allowed is False and "disabled" in reason


def test_normalise_number_keeps_a_leading_plus_and_nothing_else():
    assert normalise_number("+1 (610) 555-0100") == "+16105550100"
    assert normalise_number("610.555.0100") == "6105550100"
    assert normalise_number("") == ""


def test_a_dial_intent_from_the_model_is_validated_before_anything_happens():
    calls: list[str] = []
    duplex = MemoryDuplex()
    for frame in utterance():
        duplex.feed(frame)
    tts = BeepTTS(rate=8000, ms=5, echo=False)
    session = CallSession(
        duplex,
        EchoASR(script=["call my other number now"]),
        ScriptedLLM(replies=["Calling it. [[dial:+1 555 010 9999|asked]]"]),
        tts,
        config(owner_destination="+1 610 555 0100"),
        on_dial=lambda request: (calls.append(request.destination), (True, ""))[1],
    )
    session.run()
    assert calls == [], "the model must never reach the dialler with a non-owner number"
    spoken = " ".join(tts.spoken)
    assert "I can only call my configured owner number" in spoken, "the caller must be told it was refused"
    assert "555" not in spoken and "9999" not in spoken, "the refusal must not read the number back"


def test_the_model_says_owner_and_the_config_decides_which_number_that_is():
    """The number never enters the model's context, so it cannot leak it."""
    prompt_hint = "[[dial:owner|reason]]"
    assert "owner" in prompt_hint
    assert validate_dial_request(DialRequest("owner"), "+1 610 555 0100")[0] is True
    assert validate_dial_request(DialRequest("  The Owner  "), "+1 610 555 0100")[0] is True
    # ...and a made-up number is still refused, not silently rewritten.
    assert validate_dial_request(DialRequest("+1 555 010 9999"), "+1 610 555 0100")[0] is False


def test_an_owner_dial_intent_reaches_the_dialler_and_reports_failure_out_loud():
    calls: list[str] = []
    duplex = MemoryDuplex()
    for frame in utterance():
        duplex.feed(frame)
    tts = BeepTTS(rate=8000, ms=5, echo=False)
    session = CallSession(
        duplex,
        EchoASR(script=["call me back later"]),
        ScriptedLLM(replies=["Will do. [[dial:owner|you asked]]"]),
        tts,
        config(owner_destination="+1 610 555 0100"),
        on_dial=lambda request: (calls.append(request.destination), (False, "no trunk configured"))[1],
    )
    session.run()
    assert calls == ["+1 610 555 0100"], "the dialler must receive the configured number, not the word 'owner'"
    assert "could not place that call" in " ".join(tts.spoken)


# ----------------------------------------------------------------------
# Ollama, over a fake HTTP server: no model, no network, real protocol
# ----------------------------------------------------------------------
def test_ollama_client_lists_models(fake_ollama_url):
    assert OllamaLLM("gemma4:26b", url=fake_ollama_url.url).models() == ["gemma4:26b", "gemma4:31b"]


def test_ollama_client_streams_deltas_and_asks_for_a_long_keep_alive(fake_ollama_url):
    client = OllamaLLM("gemma4:26b", url=fake_ollama_url.url, keep_alive="30m")
    deltas = list(client.stream([{"role": "user", "content": "hello"}], system="be brief"))
    assert "".join(deltas) == "Hello from your machine."
    sent = fake_ollama_url.requests[-1]["payload"]
    assert sent["stream"] is True
    assert sent["keep_alive"] == "30m"
    assert sent["messages"][0] == {"role": "system", "content": "be brief"}
    assert sent["messages"][1]["content"] == "hello"
    assert sent["options"]["num_predict"] == 120


def test_ollama_client_reports_an_error_event_instead_of_a_partial_reply(fake_ollama_url):
    fake_ollama_url.fail("model requires more system memory")
    with pytest.raises(PipelineError, match="more system memory"):
        list(OllamaLLM("gemma4:26b", url=fake_ollama_url.url).stream([{"role": "user", "content": "hi"}]))


def test_ollama_client_turns_an_unreachable_server_into_a_readable_error():
    # Port 1 is reserved and never listening.
    with pytest.raises(PipelineError, match="cannot reach ollama"):
        list(OllamaLLM("gemma4:26b", url="http://127.0.0.1:1", timeout=0.5).stream([{"role": "user", "content": "hi"}]))


def test_ollama_generate_returns_one_reply(fake_ollama_url):
    client = OllamaLLM("gemma4:26b", url=fake_ollama_url.url)
    assert client.generate("say hi", system="short") == "One short answer."


# ----------------------------------------------------------------------
# optional adapters degrade with a sentence, not a traceback
# ----------------------------------------------------------------------
def test_piper_reports_what_is_missing_with_the_command_that_fixes_it():
    missing_binary = PiperTTS("~/.local/share/piper/nonexistent.onnx", binary="piper-does-not-exist")
    ok, why = missing_binary.available()
    assert ok is False and "piper" in why

    # /bin/true stands in for an installed binary so the missing-voice branch runs.
    missing_voice = PiperTTS("~/.local/share/piper/nonexistent.onnx", binary="/bin/true")
    ok, why = missing_voice.available()
    assert ok is False and "download_voices" in why


def test_whisper_availability_is_a_sentence_when_it_is_not_installed():
    asr = WhisperASR("small.en")
    ok, why = asr.available()
    if ok:  # faster-whisper *is* installed on this machine
        pytest.skip("faster-whisper is installed here; the failure path cannot be exercised")
    assert "pip install faster-whisper" in why
    with pytest.raises(PipelineError, match="faster-whisper"):
        asr.transcribe(silence(8000, 20), 8000)


def test_scripted_llm_streams_words_so_barge_in_can_be_tested():
    llm = ScriptedLLM(replies=["one two three"])
    assert "".join(llm.stream([], "")) .split() == ["one", "two", "three"]


# ----------------------------------------------------------------------
# the offline driver
# ----------------------------------------------------------------------
def test_run_wav_session_reads_a_file_and_writes_the_reply(tmp_path):
    caller = tmp_path / "caller.wav"
    pcm = b"".join(utterance())
    wav_write(str(caller), pcm, 8000)
    reply = tmp_path / "reply.wav"

    def factory(rate):
        return MemoryDuplex(rate)

    asr = EchoASR(script=["are you there"])
    session, spoken, rate = run_wav_session(
        str(caller), asr, ScriptedLLM(replies=["Yes, I am."]), BeepTTS(rate=8000, ms=50), config(), factory,
        output_wav=str(reply), realtime=False,
    )
    assert rate == 8000
    assert asr.seen, "the file's audio must be transcribed"
    assert spoken, "the agent must have said something"
    written, written_rate = wav_read(str(reply))
    assert written_rate == 8000
    assert written == spoken
    assert session.ended_reason in ("silence-timeout", "caller-hung-up")


def test_a_socket_duplex_can_be_driven_by_the_session_end_to_end():
    """The real duplex, over a real socket, with no PBX in between.

    This is as close to a live call as CI gets: Asterisk's side of a socketpair
    sends a UUID and a spoken turn, and the session's reply comes back as
    AudioSocket audio frames.
    """
    import uuid as uuid_module

    from voice.audiosocket import MSG_AUDIO_8000, MSG_HANGUP, MSG_UUID, SocketDuplex, encode

    asterisk, agent = socket.socketpair()
    try:
        duplex = SocketDuplex(agent)
        asterisk.settimeout(5)
        asterisk.sendall(encode(MSG_UUID, uuid_module.uuid4().bytes))
        for frame in utterance():
            asterisk.sendall(encode(MSG_AUDIO_8000, frame))
        session = CallSession(
            duplex, EchoASR(script=["hello"]), ScriptedLLM(replies=["hi there"]), BeepTTS(rate=8000, ms=20), config()
        )

        result: list[str] = []
        thread = threading.Thread(target=lambda: result.append(session.run()), daemon=True)
        thread.start()

        header = b""
        while len(header) < 3:
            header += asterisk.recv(3 - len(header))
        kind, length = struct.unpack("!BH", header)
        assert kind == MSG_AUDIO_8000 and length == 320, "the agent must speak AudioSocket frames"
        assert len(asterisk.recv(320)) == 320

        asterisk.sendall(encode(MSG_HANGUP))
        thread.join(timeout=5)
        assert result == ["caller-hung-up"]
    finally:
        asterisk.close()
        agent.close()
