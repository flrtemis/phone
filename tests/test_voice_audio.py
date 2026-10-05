"""Audio primitives: resampling, voice-activity gating, WAV handling.

These are the pieces that decide whether a caller's sentence arrives intact, so
the assertions are about bytes and levels rather than about "it ran".
"""

from __future__ import annotations

import math
import random
import struct
import wave

import pytest

from voice.audio import (
    EnergyVad,
    SAMPLE_WIDTH,
    StreamingResampler,
    detect_dtmf,
    duration_ms,
    frame_bytes,
    frames_of,
    mix,
    pcm16_from_float,
    peak,
    resample,
    rms,
    silence,
    tone,
    wav_read,
    wav_write,
)


def sine(rate: int, freq: float, ms: int, amplitude: int = 12000) -> bytes:
    count = int(rate * ms / 1000)
    return struct.pack(f"<{count}h", *[int(amplitude * math.sin(2 * math.pi * freq * i / rate)) for i in range(count)])


def test_frame_bytes_is_20ms_of_16_bit_audio():
    assert frame_bytes(8000) == 320
    assert frame_bytes(16000) == 640
    assert frame_bytes(8000, 10) == 160


def test_frames_of_pads_the_tail_instead_of_dropping_it():
    data = b"\x01\x02" * 100  # 100 samples = 200 bytes, shorter than one 320-byte frame
    frames = list(frames_of(data, 8000))
    assert len(frames) == 1
    assert len(frames[0]) == 320
    assert frames[0].startswith(data)
    assert frames[0][len(data) :] == b"\x00" * (320 - len(data))


def test_rms_tracks_level_and_ignores_sign():
    assert rms(silence(8000, 20)) == 0.0
    assert rms(tone(8000, 440, 20, amplitude=8000)) > 5000
    assert rms(b"\x00\x80" * 160) > 32000  # every sample is -32768


def test_peak_finds_the_loudest_sample():
    assert peak(silence(8000, 20)) == 0
    assert peak(struct.pack("<4h", 0, -20000, 100, 5)) == 20000


def test_resample_same_rate_is_identity():
    data = tone(8000, 440, 20)
    assert resample(data, 8000, 8000) is data


@pytest.mark.parametrize("in_rate,out_rate", [(8000, 16000), (16000, 8000), (22050, 8000), (8000, 22050)])
def test_resample_produces_the_expected_number_of_samples(in_rate, out_rate):
    data = sine(in_rate, 300, 100)
    out = resample(data, in_rate, out_rate)
    expected_samples = int(len(data) / SAMPLE_WIDTH * out_rate / in_rate)
    # One sample of slack: the streaming resampler holds one input sample back.
    assert abs(len(out) // SAMPLE_WIDTH - expected_samples) <= 2


def test_streaming_resampler_matches_the_one_shot_version():
    """Chunk boundaries must not change the signal.

    This is the test that catches the classic streaming-resampler bug: consuming
    the last sample of a chunk removes the left endpoint of the next
    interpolation, which puts a click at every boundary and quietly doubles the
    perceived energy of the audio.
    """
    data = sine(22050, 300, 100)
    one_shot = resample(data, 22050, 8000)
    streamer = StreamingResampler(22050, 8000)
    chunk = 2 * 221  # an even number of bytes: never split a 16-bit sample
    chunks = [streamer.process(data[i : i + chunk]) for i in range(0, len(data), chunk)]
    streamed = b"".join(chunks) + streamer.flush()
    assert streamed == one_shot, "streamed output must be byte-identical to one-shot"
    assert abs(rms(streamed) - rms(one_shot)) < 1


def test_streaming_resampler_keeps_state_across_two_sample_chunks():
    """The worst case: one sample at a time, the way a jittery socket delivers."""
    data = sine(8000, 400, 40)
    streamer = StreamingResampler(8000, 16000)
    pieces = [streamer.process(data[i : i + 2]) for i in range(0, len(data), 2)]
    out = b"".join(pieces) + streamer.flush()
    assert out == resample(data, 8000, 16000)


def test_resampler_rejects_nonsense_rates():
    with pytest.raises(ValueError):
        StreamingResampler(0, 8000)
    with pytest.raises(ValueError):
        StreamingResampler(8000, -1)


def test_vad_ignores_a_click_but_accepts_speech():
    vad = EnergyVad(threshold=700, hangover_frames=10, min_speech_frames=3)
    decisions = [vad.push(tone(8000, 300, 20, amplitude=9000)) for _ in range(2)]
    assert not any(d.speech for d in decisions), "two frames (40 ms) is a click, not a word"
    decisions = [vad.push(tone(8000, 300, 20, amplitude=9000)) for _ in range(3)]
    assert decisions[-1].speech


def test_vad_reports_end_of_utterance_after_the_hangover():
    vad = EnergyVad(threshold=700, hangover_frames=5, min_speech_frames=1)
    for _ in range(5):
        vad.push(tone(8000, 300, 20, amplitude=9000))
    quiet = [vad.push(silence(8000, 20)) for _ in range(5)]
    assert quiet[-1].reason == "end-of-utterance"
    assert not quiet[-1].speech


def test_vad_resets_after_an_utterance_so_the_next_one_is_detected():
    vad = EnergyVad(threshold=700, hangover_frames=2, min_speech_frames=1)
    loud = tone(8000, 300, 20, amplitude=9000)
    for _ in range(2):
        vad.push(loud)
    for _ in range(2):
        vad.push(silence(8000, 20))
    assert vad.push(loud).speech, "the VAD must not stay latched on the previous utterance"


def test_vad_cuts_an_utterance_that_never_ends():
    vad = EnergyVad(threshold=700, min_speech_frames=1, max_utterance_ms=100)
    decisions = [vad.push(tone(8000, 300, 20, amplitude=9000)) for _ in range(8)]
    assert decisions[-1].reason == "max-duration"


def test_wav_round_trip_is_bit_exact(tmp_path):
    path = tmp_path / "clip.wav"
    original = tone(8000, 440, 250, amplitude=9000)
    wav_write(str(path), original, 8000)
    data, rate = wav_read(str(path))
    assert rate == 8000
    assert data == original


def test_wav_read_downmixes_stereo(tmp_path):
    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(struct.pack("<4h", 100, -100, 200, -200))
    data, rate = wav_read(str(path))
    assert rate == 8000
    assert struct.unpack("<2h", data) == (100, 200), "left channel wins, deterministically"


def test_wav_read_refuses_non_16_bit(tmp_path):
    path = tmp_path / "8bit.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(1)
        handle.setframerate(8000)
        handle.writeframes(b"\x80" * 160)
    with pytest.raises(ValueError, match="16-bit"):
        wav_read(str(path))


def test_duration_ms_is_derived_from_the_rate():
    assert duration_ms(b"\x00\x00" * 8000, 8000) == pytest.approx(1000.0)
    assert duration_ms(b"\x00\x00" * 4000, 8000) == pytest.approx(500.0)


def dtmf_pair(row: float, column: float, ms: int = 80, amplitude: int = 9000) -> bytes:
    return mix(tone(8000, row, ms, amplitude=amplitude), tone(8000, column, ms, amplitude=amplitude))


@pytest.mark.parametrize(
    "digit,row,column",
    [("1", 697, 1209), ("5", 770, 1336), ("9", 852, 1477), ("0", 941, 1336), ("#", 941, 1477)],
)
def test_dtmf_detector_identifies_real_tone_pairs(digit, row, column):
    assert detect_dtmf(dtmf_pair(row, column)) == digit


def test_dtmf_detector_rejects_speech_like_tones():
    # 697 Hz is the row frequency of 1/2/3 but has no column tone, so a lone
    # tone - or anything else that is not a pair - must not register.
    assert detect_dtmf(tone(8000, 697, 80, amplitude=9000)) == ""
    assert detect_dtmf(tone(8000, 440, 80, amplitude=9000)) == ""
    assert detect_dtmf(tone(8000, 1336, 80, amplitude=9000)) == ""


def test_dtmf_detector_needs_both_tones_at_a_comparable_level():
    loud_row = mix(
        tone(8000, 697, 80, amplitude=20000),
        tone(8000, 1209, 80, amplitude=300),
    )
    assert detect_dtmf(loud_row) == "", "one dominant tone with a whisper of the other is not a keypress"


def test_dtmf_detector_ignores_a_frame_that_is_too_short_to_hold_a_digit():
    assert detect_dtmf(tone(8000, 697, 8, amplitude=9000)) == ""


def test_mix_sums_without_wrapping_around():
    loud = struct.pack("<2h", 30000, -30000)
    mixed = mix(loud, loud)
    assert struct.unpack("<2h", mixed) == (32767, -32768), "clipping, not integer wraparound"


def test_pcm16_from_float_clamps_to_the_valid_range():
    data = pcm16_from_float([0.0, 1.0, -1.0, 2.0, -2.0])
    # -1.0 maps to -32768 (the negative range has one more step than the
    # positive one); anything beyond +/-1.0 saturates instead of wrapping.
    assert struct.unpack("<5h", data) == (0, 32767, -32768, 32767, -32768)


def test_silence_length_matches_the_requested_duration():
    assert len(silence(8000, 20)) == 320
    assert len(silence(16000, 20)) == 640


def test_resampling_noise_does_not_blow_up():
    """A resampler that aliases badly shows up as a much louder signal."""
    rng = random.Random(7)
    noise = struct.pack(f"<{8000}h", *[rng.randint(-3000, 3000) for _ in range(8000)])
    out = resample(noise, 8000, 16000)
    assert rms(out) < 4000
    assert len(out) // SAMPLE_WIDTH == pytest.approx(16000, abs=4)
