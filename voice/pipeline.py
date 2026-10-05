"""Speech pipeline: segment an utterance, transcribe, ask the model, speak.

The interesting properties, in order of how often they break in practice:

1. **Latency is measured, not assumed.** Each stage logs its own duration, so a
   slow turn is attributable instead of mysterious.
2. **Barge-in works.** While the agent is speaking it keeps reading inbound
   audio; if you start talking it stops mid-sentence. Without this a phone agent
   feels dead, and callers end up talking over it.
3. **The model cannot dial.** Speech is untrusted input. The LLM proposes an
   *intent*, and the intent is validated against a configured owner destination;
   emergency numbers are refused outright.
4. **Adapters are optional.** Whisper, Piper and Ollama are all optional. With
   none installed the `echo` and `wav` adapters let you exercise the whole path.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterator, Optional, Protocol

from voice.audio import (
    SAMPLE_WIDTH,
    EnergyVad,
    StreamingResampler,
    duration_ms,
    frame_bytes,
    frames_of,
    rms,
    silence,
    wav_read,
    wav_write,
)
from voice.audiosocket import Duplex, ProtocolError

LOG = logging.getLogger("phone.voice.pipeline")

#: Numbers the agent must never dial, whatever it is asked. Spelled out rather
#: than pattern-matched: an earlier version of this file used ^9{2,3}$ for 911,
#: which never matched 911 - the call was refused only because it also did not
#: match the owner's number. A refusal that happens for the wrong reason is not
#: a safety property, it is a coincidence.
EMERGENCY_NUMBERS = frozenset(
    {
        "911",   # North America
        "988",   # North America: suicide and crisis lifeline
        "112",   # EU / GSM
        "999",   # UK
        "000",   # Australia
        "111",   # New Zealand
        "110",   # Germany / Japan (police)
        "119",   # Japan (fire and ambulance)
        "118",   # Japan (coast guard) / EU directory
        "144",   # Switzerland
        "0",     # operator
    }
)

#: Short service codes that are not emergencies but are never worth dialling
#: from an automated line either.
SERVICE_NUMBERS = frozenset({"211", "311", "411", "511", "611", "711", "811"})


class PipelineError(Exception):
    pass


# ----------------------------------------------------------------------
# adapter interfaces
# ----------------------------------------------------------------------
class ASR(Protocol):
    def transcribe(self, pcm: bytes, rate: int) -> str: ...


class LLM(Protocol):
    def stream(self, messages: list[dict], system: str) -> Iterator[str]: ...


class TTS(Protocol):
    def synthesize(self, text: str) -> tuple[bytes, int]:
        """Return (PCM 16-bit mono, sample rate)."""
        ...


# ----------------------------------------------------------------------
# adapters that always work (used by tests and by pre-GPU setups)
# ----------------------------------------------------------------------
class EchoASR:
    """Never transcribes anything; the tests drive the pipeline with a script."""

    def __init__(self, script: Optional[list[str]] = None) -> None:
        self.script = list(script or [])
        self.seen: list[bytes] = []

    def transcribe(self, pcm: bytes, rate: int) -> str:
        self.seen.append(pcm)
        return self.script.pop(0) if self.script else ""


class WavTTS:
    """Speak by playing a pre-recorded WAV, resampled to the call rate.

    Useful as a greeting/fallback voice with no TTS engine installed, and as a
    deterministic stand-in in tests.
    """

    def __init__(self, path: str = "", fallback_rate: int = 8000) -> None:
        self.path = path
        self.fallback_rate = fallback_rate
        self._data: Optional[bytes] = None
        self._rate = fallback_rate

    def _load(self) -> None:
        if self._data is not None:
            return
        if self.path and os.path.isfile(self.path):
            self._data, self._rate = wav_read(self.path)
        else:
            self._data = b""  # silence: the caller hears nothing, but the loop runs
            self._rate = self.fallback_rate

    def synthesize(self, text: str) -> tuple[bytes, int]:
        self._load()
        return self._data or b"", self._rate


class PiperTTS:
    """Piper: local neural TTS, one subprocess per utterance, raw PCM out.

    Install: pip install piper-tts (or grab the binary), then download a voice:
        python -m piper.download_voices en_US-amy-medium
    The model files live in ~/.local/share/piper if you use the pip package.
    """

    def __init__(self, model: str, binary: str = "piper", sample_rate: int = 22050, speed: float = 1.0) -> None:
        self.model = os.path.expanduser(model)
        self.binary = binary
        self.sample_rate = sample_rate
        self.speed = speed

    def available(self) -> tuple[bool, str]:
        if shutil.which(self.binary) is None and not os.path.isfile(self.binary):
            return False, f"piper binary not found ({self.binary}); pip install piper-tts"
        if not os.path.isfile(self.model):
            return False, f"voice model not found ({self.model}); python -m piper.download_voices en_US-amy-medium"
        return True, ""

    def synthesize(self, text: str) -> tuple[bytes, int]:
        ok, why = self.available()
        if not ok:
            raise PipelineError(why)
        command = [self.binary, "--model", self.model, "--output_raw"]
        if self.speed != 1.0:
            command += ["--length_scale", f"{1.0 / self.speed:.3f}"]
        try:
            proc = subprocess.run(
                command,
                input=text.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PipelineError(f"piper failed: {exc}") from exc
        if proc.returncode != 0:
            raise PipelineError(f"piper exited {proc.returncode}: {proc.stderr.decode('utf-8', 'replace')[:200]}")
        if not proc.stdout:
            raise PipelineError("piper produced no audio")
        return proc.stdout, self.sample_rate


class BeepTTS:
    """A TTS stand-in that prints what it would say and emits a short tone.

    This exists so the whole loop - pacing, barge-in, transcript, timings - can be
    demonstrated and tested with nothing installed. It is obviously not speech,
    and it says so.
    """

    def __init__(self, rate: int = 8000, ms: int = 220, pitch: float = 440.0, echo: bool = False) -> None:
        self.rate = rate
        self.ms = ms
        self.pitch = pitch
        self.echo = echo
        self.spoken: list[str] = []

    def synthesize(self, text: str) -> tuple[bytes, int]:
        from voice.audio import tone

        self.spoken.append(text)
        if self.echo:
            print(f"  agent (not real speech): {text}")
        # A rising pair per sentence, so the end of a reply is audible by ear.
        return tone(self.rate, self.pitch, self.ms, amplitude=6000) + tone(
            self.rate, self.pitch * 1.25, self.ms, amplitude=6000
        ), self.rate


class WhisperASR:
    """faster-whisper on CPU or CUDA. Lazily imported; numpy comes with it."""

    def __init__(self, model: str = "small.en", device: str = "auto", compute_type: str = "auto", language: str = "en") -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self._model = None
        self._lock = threading.Lock()

    def available(self) -> tuple[bool, str]:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False, "faster-whisper not installed; pip install faster-whisper"
        return True, ""

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # type: ignore

            device = self.device
            if device == "auto":
                device = "cuda" if _cuda_available() else "cpu"
            compute = self.compute_type
            if compute == "auto":
                compute = "float16" if device == "cuda" else "int8"
            LOG.info("loading whisper model %s (device=%s compute=%s)", self.model_name, device, compute)
            self._model = WhisperModel(self.model_name, device=device, compute_type=compute)
        return self._model

    def transcribe(self, pcm: bytes, rate: int) -> str:
        ok, why = self.available()
        if not ok:
            raise PipelineError(why)
        import numpy as np  # ships with faster-whisper

        from voice.audio import resample

        target_rate = 16000
        audio = resample(pcm, rate, target_rate) if rate != target_rate else pcm
        samples = np.frombuffer(audio, dtype="<i2").astype("float32") / 32768.0
        with self._lock:
            model = self._load()
            segments, _info = model.transcribe(
                samples,
                language=self.language,
                beam_size=1,              # greedy: latency over marginal accuracy
                vad_filter=False,         # we already segmented the utterance
                condition_on_previous_text=False,
            )
            return " ".join(segment.text.strip() for segment in segments).strip()


class OllamaLLM:
    """Ollama over its local HTTP API. No third-party client library."""

    def __init__(
        self,
        model: str = "gemma4:26b",
        url: str = "http://127.0.0.1:11434",
        temperature: float = 0.6,
        num_predict: int = 120,
        timeout: float = 30.0,
        keep_alive: str = "30m",
    ) -> None:
        self.model = model
        self.url = url.rstrip("/")
        self.temperature = temperature
        self.num_predict = num_predict
        self.timeout = timeout
        self.keep_alive = keep_alive

    # ---- discovery / health -----------------------------------------
    def models(self) -> list[str]:
        with urllib.request.urlopen(f"{self.url}/api/tags", timeout=3) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
        return [entry.get("name", "") for entry in payload.get("models", [])]

    def generate(self, prompt: str, system: str = "") -> str:
        """Non-streaming helper, used by `phone voice ask` for quick checks."""
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "options": {"temperature": self.temperature, "num_predict": self.num_predict},
            "keep_alive": self.keep_alive,
        }
        request = urllib.request.Request(
            f"{self.url}/api/generate",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8")).get("response", "")

    def stream(self, messages: list[dict], system: str = "") -> Iterator[str]:
        payload = {
            "model": self.model,
            "messages": ([{"role": "system", "content": system}] if system else []) + messages,
            "stream": True,
            "options": {"temperature": self.temperature, "num_predict": self.num_predict},
            "keep_alive": self.keep_alive,
        }
        request = urllib.request.Request(
            f"{self.url}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                for raw in response:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("error"):
                        raise PipelineError(f"ollama: {event['error']}")
                    delta = event.get("message", {}).get("content", "")
                    if delta:
                        yield delta
                    if event.get("done"):
                        break
        except urllib.error.URLError as exc:
            raise PipelineError(f"cannot reach ollama at {self.url}: {exc}") from exc


class ScriptedLLM:
    """Deterministic model for tests and for `--no-llm` dry runs."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.seen: list[list[dict]] = []

    def stream(self, messages: list[dict], system: str = "") -> Iterator[str]:
        self.seen.append(list(messages))
        reply = self.replies.pop(0) if self.replies else "I did not catch that."
        for word in reply.split(" "):
            yield word + " "


def _cuda_available() -> bool:
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        result = subprocess.run(["nvidia-smi", "-L"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and b"GPU" in result.stdout


# ----------------------------------------------------------------------
# dialling intent: the model proposes, the code decides
# ----------------------------------------------------------------------
@dataclass
class DialRequest:
    destination: str
    reason: str = ""


# The destination may contain spaces and punctuation ("+1 (555) 010-9999"): a
# model that writes a number the way a human reads one must still be caught
# and refused, rather than slipping past the validator by not matching.
DIAL_INTENT = re.compile(
    r"\[\[dial:(?P<destination>[^\]|]+?)(?:\s*\|\s*(?P<reason>[^\]]*))?\]\]"
)


def extract_dial_request(text: str) -> tuple[str, Optional[DialRequest]]:
    """Pull a ``[[dial:DEST|reason]]`` directive out of a model reply.

    Directives are stripped from the spoken text so the caller never hears the
    markup. The destination is returned for *validation* - it is never dialled
    because a model asked for it.
    """
    match = DIAL_INTENT.search(text)
    if not match:
        return text, None
    request = DialRequest(match.group("destination").strip(), (match.group("reason") or "").strip())
    return DIAL_INTENT.sub("", text).strip(), request


def normalise_number(value: str) -> str:
    """Reduce a dial string to digits, keeping a leading '+'.

    ``+1 (555) 010-9999`` -> ``+15550109999``
    """
    text = value.strip()
    plus = text.startswith("+")
    digits = re.sub(r"[^0-9]", "", text)
    return ("+" + digits) if plus else digits


#: Words the model may use instead of the owner's number. It is never shown the
#: number itself, so it cannot leak it, misdial it, or be tricked into reading it
#: back to a caller: the config says which number "owner" means.
OWNER_ALIASES = frozenset(
    {
        "owner",
        "the owner",
        "owner's number",
        "owners number",
        "me",
        "myself",
        "my number",
        "my own number",
        "my phone",
        "you",
    }
)


def resolve_owner_alias(destination: str, owner: str) -> str:
    """Turn ``owner`` into the configured number; pass anything else through.

    A destination the model made up is *not* silently repaired - it goes to the
    validator exactly as written, where it is refused.
    """
    text = destination.strip().strip("<>").strip().lower()
    return owner if text in OWNER_ALIASES else destination


def is_emergency(destination: str) -> bool:
    """True for emergency numbers and the operator, however they are written."""
    return normalise_number(destination).lstrip("+") in EMERGENCY_NUMBERS


def is_service_code(destination: str) -> bool:
    return normalise_number(destination).lstrip("+") in SERVICE_NUMBERS


def validate_dial_request(request: DialRequest, owner: str) -> tuple[bool, str]:
    """Only the configured owner number may be dialled, ever.

    Returns (allowed, reason). A refusal is spoken to the caller, so the wording
    matters: it must not leak the owner's number.
    """
    if not request.destination:
        return False, "no destination"
    if is_emergency(request.destination):
        return False, "emergency numbers are never dialled by this agent"
    if is_service_code(request.destination):
        return False, "that is a service code, not a number I can call"
    if not owner:
        return False, "no owner destination is configured, so outbound dialling is disabled"
    resolved = resolve_owner_alias(request.destination, owner)
    if normalise_number(resolved) != normalise_number(owner):
        return False, "I can only call my configured owner number."
    return True, ""


def _read_notes(path: str, limit: int = 1000) -> str:
    """Read the caller's note file, refusing to ingest anything enormous.

    A note is untrusted input arriving from a remote client, so it is bounded
    and it is never executed or parsed - it is appended to the system prompt as
    text, and the dialling rules that matter are enforced in code, not in the
    prompt.
    """
    if not path:
        return ""
    try:
        with open(os.path.expanduser(path), encoding="utf-8", errors="replace") as handle:
            text = handle.read(limit + 1)
    except OSError:
        return ""
    return text[:limit].strip()


# ----------------------------------------------------------------------
# the call session
# ----------------------------------------------------------------------
@dataclass
class TurnTimings:
    asr_ms: float = 0.0
    llm_first_token_ms: float = 0.0
    tts_ms: float = 0.0
    playback_ms: float = 0.0
    interrupted: bool = False

    @property
    def total_ms(self) -> float:
        return self.asr_ms + self.llm_first_token_ms + self.tts_ms

    def line(self) -> str:
        return (
            f"asr {self.asr_ms:.0f}ms | llm-first-token {self.llm_first_token_ms:.0f}ms | "
            f"tts {self.tts_ms:.0f}ms | playback {self.playback_ms:.0f}ms"
            + (" | interrupted" if self.interrupted else "")
        )


@dataclass
class SessionConfig:
    system_prompt: str = (
        "You are a telephone assistant running entirely on the caller's own machine. "
        "Speak in short, natural sentences - one or two at most, because every word is "
        "spoken aloud in real time. Never read out formatting, lists, or code. "
        "If you did not understand, ask a short clarifying question. "
        "If you need to call the owner back later, emit [[dial:<number>|<reason>]] on its own."
    )
    greeting: str = "Hello, this is your local assistant. Go ahead."
    farewell: str = "Goodbye."
    #: Phrases that end the call when the caller says them.
    hangup_phrases: tuple[str, ...] = ("goodbye", "hang up", "end the call", "that's all", "thats all")
    owner_destination: str = ""
    max_turns: int = 60
    max_call_seconds: float = 900.0
    silence_timeout_seconds: float = 12.0
    barge_in: bool = True
    barge_in_threshold: float = 700.0
    barge_in_frames: int = 3
    speak_dial_refusal: bool = True
    history_turns: int = 12
    sample_rate: int = 8000


class CallSession:
    """One phone call: greeting, then turn-taking until someone hangs up."""

    def __init__(
        self,
        duplex: Duplex,
        asr: ASR,
        llm: LLM,
        tts: TTS,
        config: Optional[SessionConfig] = None,
        *,
        on_dial: Optional[Callable[[DialRequest], tuple[bool, str]]] = None,
        transcript_path: Optional[str] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        notes_path: str = "",
    ) -> None:
        self.duplex = duplex
        self.asr = asr
        self.llm = llm
        self.tts = tts
        self.cfg = config or SessionConfig()
        self.on_dial = on_dial
        self.on_event = on_event
        self.transcript_path = transcript_path
        #: A note file, read once at the start of each call. This is how a
        #: remote client (phone app, web page, a curl from a laptop) leaves
        #: something for the agent to bring up: "I am driving, keep it short".
        self.notes_path = notes_path
        self.notes = _read_notes(notes_path)
        self.history: list[dict] = []
        self.transcript: list[dict] = []
        self.turns: list[TurnTimings] = []
        #: Audio received while the agent was speaking; it is the caller's next
        #: utterance, not something to throw away.
        self._pending_audio: bytes = b""
        self.started_at = time.monotonic()
        self.ended_reason = ""

    # ---- helpers -----------------------------------------------------
    def _log_turn(self, role: str, text: str) -> None:
        self.transcript.append({"role": role, "text": text, "at": time.time()})
        if self.on_event is not None:
            try:
                self.on_event({"type": "turn", "role": role, "text": text, "at": time.time()})
            except Exception:  # noqa: BLE001 - a display must never break a call
                LOG.debug("on_event callback failed", exc_info=True)

    def _write_transcript(self) -> None:
        if not self.transcript_path:
            return
        try:
            # The directory may not exist on a first run (or after the user moved
            # their transcript directory); creating it here means the first call
            # is recorded, which is exactly the call people want to read.
            parent = os.path.dirname(os.path.abspath(self.transcript_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.transcript_path, "a", encoding="utf-8") as handle:
                for entry in self.transcript:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:  # never fail a call over logging
            LOG.warning("could not write transcript: %s", exc)

    def _speak(self, text: str, timings: Optional[TurnTimings] = None) -> bool:
        """Synthesise and play, watching for barge-in. Returns True if interrupted.

        Playback is paced against the wall clock. This matters more than it
        looks: the AudioSocket carries raw PCM, and if a three-second reply is
        dumped into the socket as fast as the CPU can write, Asterisk queues the
        surplus and the caller hears a delay that grows with every turn. Sending
        each 20 ms frame and then waiting until it *should* have been played
        keeps latency flat - and it is what makes barge-in meaningful, because
        the caller's speech arrives while the frame it interrupts is still being
        heard, not after the whole reply has already been flushed.
        """
        if not text:
            return False
        if self.duplex.is_closed:
            return False
        started = time.monotonic()
        try:
            pcm, rate = self.tts.synthesize(text)
        except PipelineError as exc:
            LOG.error("tts failed: %s", exc)
            self.duplex.send_audio(silence(self.cfg.sample_rate, 500))
            return False
        if timings:
            timings.tts_ms += (time.monotonic() - started) * 1000

        if rate != self.cfg.sample_rate:
            resampler: Optional[StreamingResampler] = StreamingResampler(rate, self.cfg.sample_rate)
        else:
            resampler = None

        interrupted = False
        playback_started = time.monotonic()
        sent_samples = 0
        for frame in frames_of(pcm, rate):
            chunk = resampler.process(frame) if resampler else frame
            if resampler and not chunk:
                continue
            self.duplex.send_audio(chunk)
            if rate != self.cfg.sample_rate and resampler is not None:
                # Pacing is in output samples, because that is what the caller hears.
                sent_samples += len(chunk) // SAMPLE_WIDTH
                due = playback_started + sent_samples / self.cfg.sample_rate
            else:
                sent_samples += len(frame) // SAMPLE_WIDTH
                due = playback_started + sent_samples / rate
            while True:
                remaining = due - time.monotonic()
                if remaining <= 0:
                    break
                if self.cfg.barge_in and self._heard_speech(min(remaining, 0.05)):
                    LOG.info("caller interrupted; stopping playback")
                    interrupted = True
                    break
                time.sleep(min(remaining, 0.005))
            if interrupted or self.duplex.is_closed:
                break
        if timings:
            timings.playback_ms += (time.monotonic() - playback_started) * 1000
            timings.interrupted = timings.interrupted or interrupted
        return interrupted

    def _heard_speech(self, budget: float = 0.06) -> bool:
        """Listen for the caller for up to ``budget`` seconds.

        Nothing is ever dropped: audio that is not a barge-in is pushed into
        ``_pending_audio`` and becomes the start of the caller's next utterance,
        so polling during playback cannot swallow the beginning of a sentence.
        """
        deadline = time.monotonic() + budget
        streak = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            frame = self.duplex.recv_audio(remaining)
            if frame is None:
                return False  # a timeout, or the call ended; not speech either way
            self._pending_audio += frame
            if rms(frame) >= self.cfg.barge_in_threshold:
                streak += 1
                if streak >= self.cfg.barge_in_frames:
                    return True
            else:
                streak = 0

    def _collect_utterance(self, frame_size: int) -> Optional[bytes]:
        """Gather one utterance using energy-based segmentation."""
        vad = EnergyVad(threshold=self.cfg.barge_in_threshold, frame_ms=20)
        collected = bytearray(self._pending_audio)
        self._pending_audio = b""
        idle_since = time.monotonic()
        speaking = False

        while True:
            if self.duplex.is_closed:
                # The caller hung up (or the socket died) while we were listening.
                self.ended_reason = self.ended_reason or "caller-hung-up"
                return None
            if time.monotonic() - self.started_at > self.cfg.max_call_seconds:
                self.ended_reason = "max-call-duration"
                self._speak(self.cfg.farewell)
                return None

            frame = self.duplex.recv_audio(0.02)
            if frame is None:
                if not speaking and time.monotonic() - idle_since > self.cfg.silence_timeout_seconds:
                    self.ended_reason = "silence-timeout"
                    return None
                time.sleep(0.01)
                continue

            for sub in frames_of(frame, self.cfg.sample_rate):
                decision = vad.push(sub)
                if decision.speech:
                    speaking = True
                    idle_since = time.monotonic()
                    collected.extend(sub)
                elif speaking:
                    collected.extend(sub)
                    if decision.reason == "end-of-utterance":
                        return bytes(collected)
                elif decision.reason == "end-of-utterance":
                    return bytes(collected) if collected else None

    # ---- main loop ---------------------------------------------------
    def run(self) -> str:
        """Handle the call until it ends. Returns the reason it ended."""
        LOG.info("session start (owner=%s, barge_in=%s)", self.cfg.owner_destination or "<unset>", self.cfg.barge_in)
        try:
            if self.cfg.greeting:
                self._speak(self.cfg.greeting)
                self._log_turn("assistant", self.cfg.greeting)

            frame_size = frame_bytes(self.cfg.sample_rate)
            for _turn in range(self.cfg.max_turns):
                utterance = self._collect_utterance(frame_size)
                if utterance is None:
                    break
                if duration_ms(utterance, self.cfg.sample_rate) < 200:
                    # Belt-and-braces: the VAD's min_speech_frames already drops
                    # clicks, so this only fires if someone tunes the gate down.
                    # ASR on 200 ms of anything but speech produces noise.
                    continue

                timings = TurnTimings()
                started = time.monotonic()
                try:
                    heard = self.asr.transcribe(utterance, self.cfg.sample_rate)
                except PipelineError as exc:
                    LOG.error("asr failed: %s", exc)
                    self._speak("Sorry, my speech recognition is not available.")
                    break
                timings.asr_ms = (time.monotonic() - started) * 1000
                heard = (heard or "").strip()
                if not heard:
                    continue
                LOG.info("caller: %s", heard)
                self._log_turn("user", heard)

                lowered = heard.lower()
                if any(phrase in lowered for phrase in self.cfg.hangup_phrases):
                    self._speak(self.cfg.farewell)
                    self._log_turn("assistant", self.cfg.farewell)
                    self.ended_reason = "caller-said-goodbye"
                    break

                reply, dial = self._ask_model(heard, timings)
                if dial is not None:
                    self._handle_dial(dial, timings)

                spoken = self._speak_reply(reply, timings)
                if spoken:
                    self._log_turn("assistant", spoken)
                self.turns.append(timings)
                LOG.info("turn timings: %s", timings.line())
            else:
                self.ended_reason = "max-turns"
                self._speak("I have to go now. Goodbye.")
        except (OSError, ProtocolError) as exc:
            # A transport failure ends the call; it must not be confused with a
            # bug in the session logic, which should surface as a traceback in
            # the server log instead of being silently reported as "hangup".
            LOG.error("transport error ended the call: %s", exc)
            self.ended_reason = self.ended_reason or "transport-error"
        except PipelineError as exc:
            LOG.error("pipeline error ended the call: %s", exc)
            self.ended_reason = self.ended_reason or "pipeline-error"
        finally:
            self._write_transcript()
            LOG.info("session end: %s", self.ended_reason or "hangup")
        return self.ended_reason or "hangup"

    def system_prompt(self) -> str:
        """The configured prompt, plus any note left for this call."""
        prompt = self.cfg.system_prompt
        if self.notes:
            prompt = f"{prompt}\n\nThe caller left this note for you before the call: {self.notes}"
        return prompt

    def _ask_model(self, heard: str, timings: TurnTimings) -> tuple[str, Optional[DialRequest]]:
        """Ask the model, streaming, and cut it off if it will not stop talking."""
        self.history.append({"role": "user", "content": heard})
        if len(self.history) > self.cfg.history_turns * 2:
            self.history = self.history[-self.cfg.history_turns * 2 :]

        started = time.monotonic()
        pieces: list[str] = []
        first_token_at: Optional[float] = None
        try:
            for delta in self.llm.stream(self.history, self.system_prompt()):
                if first_token_at is None:
                    first_token_at = time.monotonic()
                    timings.llm_first_token_ms = (first_token_at - started) * 1000
                pieces.append(delta)
                # Hard stop: a model that decides to write an essay must not hold
                # the line. 400 characters is about two spoken sentences.
                if sum(len(p) for p in pieces) > 400:
                    LOG.warning("model reply truncated at 400 characters")
                    break
        except PipelineError as exc:
            LOG.error("llm failed: %s", exc)
            return "Sorry, my language model is not reachable right now.", None

        reply = "".join(pieces).strip()
        if first_token_at is None:
            timings.llm_first_token_ms = (time.monotonic() - started) * 1000

        reply, dial = extract_dial_request(reply)
        # Strip stage directions the model sometimes emits ("(laughs)", "*sighs*").
        reply = re.sub(r"\*[^*]{1,40}\*", "", reply)
        reply = re.sub(r"^\s*\(.*?\)\s*", "", reply).strip()
        self.history.append({"role": "assistant", "content": reply})
        return reply, dial

    def _handle_dial(self, dial: DialRequest, timings: TurnTimings) -> None:
        allowed, reason = validate_dial_request(dial, self.cfg.owner_destination)
        if allowed and self.on_dial is not None:
            # Hand the dialler a concrete number. The model said "owner"; the
            # config decides what that is, and the dialler never sees the raw
            # model output.
            request = DialRequest(
                resolve_owner_alias(dial.destination, self.cfg.owner_destination), dial.reason
            )
            try:
                ok, message = self.on_dial(request)
            except Exception as exc:  # noqa: BLE001 - a dialling failure must not kill the call
                ok, message = False, f"dialling failed: {exc}"
            if not ok:
                LOG.error("originate failed: %s", message)
                if self.cfg.speak_dial_refusal:
                    self._speak("I could not place that call.")
            else:
                LOG.info("originated a call to the owner")
            return
        LOG.warning("refusing dial request %r: %s", dial.destination, reason)
        if self.cfg.speak_dial_refusal:
            self._speak(reason if reason.startswith("I can only") else "I am not able to call that number.")

    def _speak_reply(self, reply: str, timings: TurnTimings) -> str:
        """Speak sentence by sentence, so the first words start as soon as possible."""
        if not reply:
            self._speak("Sorry, I did not catch that.")
            return ""
        spoken: list[str] = []
        for sentence in _sentences(reply):
            if self._speak(sentence, timings):
                spoken.append(sentence)
                break
            spoken.append(sentence)
        return " ".join(spoken)


def _sentences(text: str) -> list[str]:
    """Split into speakable units without breaking decimals or abbreviations."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    merged: list[str] = []
    for part in parts:
        if merged and len(merged[-1]) < 12:  # glue "Yes." onto the next clause
            merged[-1] = merged[-1] + " " + part
        else:
            merged.append(part)
    return [part for part in merged if part.strip()]


# ----------------------------------------------------------------------
# offline driver: run a conversation through files, no phone required
# ----------------------------------------------------------------------
def run_wav_session(
    input_wav: str,
    asr: ASR,
    llm: LLM,
    tts: TTS,
    config: SessionConfig,
    duplex_factory: Callable[[int], Duplex],
    output_wav: str = "",
    on_dial: Optional[Callable[[DialRequest], tuple[bool, str]]] = None,
    realtime: bool = True,
) -> tuple[CallSession, bytes, int]:
    """Feed a WAV through the pipeline as if it were a call.

    This is how the pipeline is tested in CI (no PBX, no sound card) and how you
    can measure real end-to-end latency - ASR + model + TTS - before wiring up
    Asterisk at all.

    ``realtime=True`` replays the file at the speed it was recorded, which is the
    only way the wall-clock numbers mean anything. ``realtime=False`` feeds it as
    fast as possible and is what the tests use; barge-in still fires correctly
    because the session's playback pacing is what the caller's audio is measured
    against.
    """
    pcm, rate = wav_read(input_wav)
    duplex = duplex_factory(rate)
    session = CallSession(duplex, asr, llm, tts, config, on_dial=on_dial)

    def replay() -> None:
        started = time.monotonic()
        sent = 0
        for frame in frames_of(pcm, rate):
            duplex.feed(frame)  # type: ignore[attr-defined]
            sent += len(frame)
            if realtime:
                due = started + sent / (SAMPLE_WIDTH * rate)
                delay = due - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
        time.sleep(0.2)

    feeder = threading.Thread(target=replay, daemon=True)
    feeder.start()

    session.run()
    feeder.join(timeout=1.0)
    spoken = getattr(duplex, "outbound", b"")
    spoken_pcm = b"".join(spoken) if isinstance(spoken, list) else bytes(spoken)
    if output_wav and spoken_pcm:
        wav_write(output_wav, spoken_pcm, config.sample_rate)
    return session, spoken_pcm, config.sample_rate
