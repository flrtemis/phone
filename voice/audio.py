"""Audio primitives: resampling, voice-activity gating, and WAV helpers.

Pure standard library. `audioop` is intentionally avoided: it is deprecated and
was removed in Python 3.13, and telephony audio is small enough that a simple
linear resampler is plenty.
"""

from __future__ import annotations

import array
import math
import struct
import wave
from dataclasses import dataclass
from typing import Iterator

# Everything on the wire is signed 16-bit little-endian mono.
SAMPLE_WIDTH = 2
FRAME_MS = 20  # 20 ms per frame is the Asterisk convention


def samples_for(rate: int, ms: int = FRAME_MS) -> int:
    return int(rate * ms / 1000)


def frame_bytes(rate: int, ms: int = FRAME_MS) -> int:
    return samples_for(rate, ms) * SAMPLE_WIDTH


def to_samples(data: bytes) -> array.array:
    out = array.array("h")
    # Tolerate a truncated tail rather than raising: a half frame at hangup is
    # normal, and dropping it is better than crashing a live call.
    usable = len(data) - (len(data) % SAMPLE_WIDTH)
    out.frombytes(data[:usable])
    return out


def to_bytes(samples: array.array) -> bytes:
    return samples.tobytes()


def rms(data: bytes) -> float:
    """Root-mean-square level, 0..32767. Used for speech/silence decisions."""
    samples = to_samples(data)
    if not samples:
        return 0.0
    # Mean of squares via integer accumulation is fast enough at 8 kHz and
    # avoids pulling in numpy for a two-line calculation.
    total = 0
    for value in samples:
        total += value * value
    return math.sqrt(total / len(samples))


class StreamingResampler:
    """Linear-interpolation resampler that keeps state across chunks.

    Telephony gives us 8 kHz; Whisper wants 16 kHz and Piper emits 22.05 kHz.
    Resampling per chunk with no state produces a click at every boundary, so
    the read position carries over between calls - and it does so as an *exact*
    integer numerator over the output rate rather than an accumulating float.
    A float position drifts by a sample every few minutes of audio and makes the
    output depend on how the input happened to be chunked; integer arithmetic
    makes chunked output byte-identical to one-shot resampling, which is what
    the tests assert.
    """

    def __init__(self, in_rate: int, out_rate: int) -> None:
        if in_rate <= 0 or out_rate <= 0:
            raise ValueError("sample rates must be positive")
        self.in_rate = in_rate
        self.out_rate = out_rate
        self.ratio = in_rate / out_rate
        self._buffer = array.array("h")  # consumed from the left
        self._index = 0  # integer part: which buffered sample we are between
        self._remainder = 0  # fractional part, as a numerator over out_rate

    def process(self, data: bytes) -> bytes:
        if not data:
            return b""
        self._buffer.extend(to_samples(data))
        if self.in_rate == self.out_rate:
            out = to_bytes(self._buffer)
            self._buffer = array.array("h")
            return out

        out = array.array("h")
        # We can only interpolate between two known input samples, so leave the
        # final sample for next time.
        limit = len(self._buffer) - 1
        scale = self.out_rate
        while self._index < limit:
            index = self._index
            left = float(self._buffer[index])
            right = float(self._buffer[index + 1])
            frac = self._remainder / scale if scale else 0.0
            out.append(int(left + (right - left) * frac))
            self._remainder += self.in_rate
            self._index += self._remainder // scale
            self._remainder %= scale

        # Never consume the final sample: it is the left endpoint of the first
        # interpolation in the next chunk, and dropping it puts a click at every
        # chunk boundary. The position may legitimately run past the end of the
        # buffer, where the next chunk's samples continue the timeline.
        consumed = min(self._index, len(self._buffer) - 1)
        if consumed > 0:
            del self._buffer[:consumed]
            self._index -= consumed
        return to_bytes(out)

    def flush(self) -> bytes:
        """Emit whatever remains (call this at the end of a stream)."""
        if len(self._buffer) <= 1 and self._index >= len(self._buffer):
            self._buffer = array.array("h")
            self._index = 0
            self._remainder = 0
            return b""
        out = array.array("h")
        while self._index < len(self._buffer):
            out.append(self._buffer[self._index])
            self._remainder += self.in_rate
            self._index += self._remainder // self.out_rate
            self._remainder %= self.out_rate
        self._buffer = array.array("h")
        self._index = 0
        self._remainder = 0
        return to_bytes(out)


def resample(data: bytes, in_rate: int, out_rate: int) -> bytes:
    """One-shot resample (uses the streaming version internally)."""
    if in_rate == out_rate:
        return data
    resampler = StreamingResampler(in_rate, out_rate)
    return resampler.process(data) + resampler.flush()


@dataclass
class VadDecision:
    speech: bool
    level: float
    reason: str = ""


class EnergyVad:
    """Energy-based speech/silence gate.

    Not a machine-learning VAD: it is a threshold with hysteresis and a hangover
    counter, which is enough to segment utterances on a phone call (the line is
    quiet between turns) and cheap enough to run on every 20 ms frame. If the
    line is noisy, `--vad-threshold` is the knob, and the `doctor` command
    reports the levels it observes so the threshold is not a guess.
    """

    def __init__(
        self,
        threshold: float = 500.0,
        hangover_frames: int = 12,          # ~240 ms of silence ends an utterance
        min_speech_frames: int = 3,         # ~60 ms; ignores clicks
        max_utterance_ms: int = 15_000,
        frame_ms: int = FRAME_MS,
    ) -> None:
        self.threshold = threshold
        self.hangover_frames = hangover_frames
        self.min_speech_frames = min_speech_frames
        self.max_utterance_ms = max_utterance_ms
        self.frame_ms = frame_ms
        self.reset()

    def reset(self) -> None:
        self._speech_frames = 0
        self._silence_run = 0
        self._utterance_ms = 0
        self._in_speech = False

    def push(self, frame: bytes) -> VadDecision:
        level = rms(frame)
        self._utterance_ms += self.frame_ms

        if level >= self.threshold:
            self._silence_run = 0
            self._speech_frames += 1
            if not self._in_speech and self._speech_frames >= self.min_speech_frames:
                self._in_speech = True
            if self._utterance_ms > self.max_utterance_ms and self._in_speech:
                return VadDecision(True, level, "max-duration")
            return VadDecision(self._in_speech, level)

        # Quiet frame.
        self._silence_run += 1
        if self._in_speech and self._silence_run >= self.hangover_frames:
            self.reset()
            return VadDecision(False, level, "end-of-utterance")
        if not self._in_speech and self._silence_run > self.hangover_frames:
            self._speech_frames = 0  # decay stray clicks
        return VadDecision(False, level)


def wav_read(path: str) -> tuple[bytes, int]:
    """Read a WAV file, returning (raw 16-bit mono bytes, sample rate)."""
    with wave.open(path, "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if width != SAMPLE_WIDTH:
        raise ValueError(f"only 16-bit PCM WAV is supported (got {width * 8}-bit)")
    if channels == 2:
        samples = to_samples(frames)
        mono = array.array("h", samples[0::2])  # left channel wins
        frames = to_bytes(mono)
    elif channels != 1:
        raise ValueError(f"unsupported channel count: {channels}")
    return frames, rate


def wav_write(path: str, data: bytes, rate: int) -> None:
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(SAMPLE_WIDTH)
        handle.setframerate(rate)
        handle.writeframes(data)


def silence(rate: int, ms: int) -> bytes:
    return b"\x00" * (samples_for(rate, ms) * SAMPLE_WIDTH)


def frames_of(data: bytes, rate: int, ms: int = FRAME_MS) -> Iterator[bytes]:
    size = frame_bytes(rate, ms)
    for offset in range(0, len(data), size):
        chunk = data[offset : offset + size]
        if len(chunk) < size:
            chunk = chunk + b"\x00" * (size - len(chunk))
        yield chunk


def duration_ms(data: bytes, rate: int) -> float:
    return (len(data) / SAMPLE_WIDTH) / rate * 1000.0


def peak(data: bytes) -> int:
    samples = to_samples(data)
    return max((abs(v) for v in samples), default=0)


def clip16(value: int) -> int:
    return max(-32768, min(32767, value))


def mix(a: bytes, b: bytes) -> bytes:
    """Mix two equal-length streams (used to duck TTS under a barge-in tone)."""
    left, right = to_samples(a), to_samples(b)
    length = min(len(left), len(right))
    return to_bytes(array.array("h", [clip16(left[i] + right[i]) for i in range(length)]))


def tone(rate: int, freq: float, ms: int, amplitude: int = 8000) -> bytes:
    """Generate a sine tone - used by tests and by the DTMF-free 'thinking' cue."""
    count = samples_for(rate, ms)
    out = array.array("h")
    for i in range(count):
        out.append(int(amplitude * math.sin(2 * math.pi * freq * i / rate)))
    return to_bytes(out)


def detect_dtmf(data: bytes, threshold: float = 20000.0) -> str:
    """Very small DTMF detector, enough to notice a keypress during a prompt.

    Returns the digit, or "" when nothing recognisable is present. Each DTMF
    digit is one low tone (the row) plus one high tone (the column); a digit is
    only reported when *both* are present above ``threshold`` and roughly equal
    in strength.

    ``threshold`` is in Goertzel power units, normalised so that a plain sine of
    amplitude A scores about (A/2)^2. The default of 20000 therefore needs a
    tone of roughly amplitude 300 at each frequency - quiet for a telephone
    line, and far above what a single non-DTMF tone leaks into a DTMF bin.

    This is not a full DTMF receiver: it has no twist, harmonic or digit-length
    checking, and a long frame is assumed. It exists so a caller can press a
    digit to interrupt a prompt, and it is tested against synthetic tone pairs
    rather than a real line.
    """
    samples = to_samples(data)
    if len(samples) < 200:
        return ""
    rate = 8000
    rows = {"1": 697, "2": 697, "3": 697, "4": 770, "5": 770, "6": 770,
            "7": 852, "8": 852, "9": 852, "*": 941, "0": 941, "#": 941}
    cols = {"1": 1209, "2": 1336, "3": 1477, "4": 1209, "5": 1336, "6": 1477,
            "7": 1209, "8": 1336, "9": 1477, "*": 1209, "0": 1336, "#": 1477}
    length = len(samples)

    def power(freq: float) -> float:
        omega = 2 * math.pi * freq / rate
        coeff = 2 * math.cos(omega)
        s1 = s2 = 0.0
        for value in samples:
            s0 = value + coeff * s1 - s2
            s2, s1 = s1, s0
        return (s1 * s1 + s2 * s2 - coeff * s1 * s2) / max(1, length * length)

    best_digit, best_score = "", 0.0
    for digit, row in rows.items():
        row_power = power(row)
        if row_power < threshold:
            continue
        col_power = power(cols[digit])
        if col_power < threshold:
            continue
        # Both tones must be present and comparable: a lone loud tone bleeding
        # into one bin scores far below this.
        balance = 1.0 - abs(row_power - col_power) / (row_power + col_power)
        score = min(row_power, col_power) * balance
        if score > best_score:
            best_digit, best_score = digit, score
    return best_digit


def pcm16_from_float(values, scale: float = 32768.0) -> bytes:
    """Convert a float sequence (-1..1) to 16-bit PCM without numpy.

    The scale is 32768 rather than 32767 so that -1.0 maps to -32768 (full scale
    in the negative direction, which has one more step than the positive one)
    instead of -32767. A silent off-by-one here is audible as a DC offset once
    the samples are summed.
    """
    out = array.array("h")
    for value in values:
        out.append(clip16(int(max(-1.0, min(1.0, float(value))) * scale)))
    return to_bytes(out)


def pack(header: int, payload: bytes = b"") -> bytes:
    return struct.pack("!BH", header, len(payload)) + payload
