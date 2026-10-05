"""Voice agent configuration: INI file + PHONE_VOICE_* environment + flags."""

from __future__ import annotations

import configparser
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_CONFIG_PATHS = (
    "~/.config/phone/voice.conf",
    "~/.phone/voice.conf",
)


class VoiceConfigError(Exception):
    """Raised when the voice configuration is missing or unsafe."""


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass
class VoiceConfig:
    # --- AudioSocket bridge (Asterisk connects *to us*) ---------------
    host: str = "127.0.0.1"
    port: int = 9092
    max_calls: int = 2
    allow_public_bind: bool = False

    # --- models -------------------------------------------------------
    asr_backend: str = "whisper"          # whisper | echo
    asr_model: str = "small.en"
    asr_device: str = "auto"              # auto | cuda | cpu
    asr_compute: str = "auto"
    asr_language: str = "en"

    llm_backend: str = "ollama"           # ollama | script
    llm_model: str = "gemma4:26b"         # MoE: near-26B quality at ~4B speed
    llm_url: str = "http://127.0.0.1:11434"
    llm_temperature: float = 0.6
    llm_max_tokens: int = 120
    llm_timeout: float = 30.0

    tts_backend: str = "piper"            # piper | wav | beep
    tts_binary: str = "piper"
    tts_model: str = "~/.local/share/piper/en_US-amy-medium.onnx"
    tts_sample_rate: int = 22050
    tts_speed: float = 1.0
    tts_fallback_wav: str = ""

    # --- conversation -------------------------------------------------
    greeting: str = "Hello, this is your local assistant. Go ahead."
    farewell: str = "Goodbye."
    system_prompt: str = ""
    hangup_phrases: str = "goodbye,hang up,end the call,that's all,thats all"
    max_turns: int = 60
    max_call_seconds: float = 900.0
    silence_timeout_seconds: float = 12.0
    history_turns: int = 12

    # --- barge-in -----------------------------------------------------
    barge_in: bool = True
    barge_in_threshold: float = 700.0
    barge_in_frames: int = 3

    # --- dialling (the model never chooses a number) ------------------
    owner_destination: str = ""
    originate_command: str = ""           # default: asterisk -rx 'channel originate ...'
    speak_dial_refusal: bool = True

    # --- transcripts --------------------------------------------------
    transcript_dir: str = "~/voice/transcripts"

    # --- asterisk provisioning ----------------------------------------
    asterisk_dir: str = "/etc/asterisk"
    handset_extension: str = "100"
    handset_password: str = ""
    handset_name: str = "phone"
    ai_extension: str = "600"
    tls_dir: str = "~/.config/phone/tls"   # the same directory `phone sip init` fills
    rtp_start: int = 4000
    rtp_end: int = 4010

    source_path: Optional[str] = None

    # ------------------------------------------------------------------
    @property
    def tls_path(self) -> Path:
        return Path(os.path.expanduser(self.tls_dir))

    @property
    def transcript_path(self) -> Path:
        return Path(os.path.expanduser(self.transcript_dir))

    @property
    def hangup_list(self) -> tuple[str, ...]:
        return tuple(phrase.lower() for phrase in _csv(self.hangup_phrases))

    @property
    def is_loopback(self) -> bool:
        return self.host in ("127.0.0.1", "::1", "localhost")

    def validate(self) -> None:
        if not (0 <= self.port < 65536):
            raise VoiceConfigError(f"port out of range: {self.port}")
        if not self.is_loopback and not self.allow_public_bind:
            raise VoiceConfigError(
                f"refusing to listen on {self.host}: the AudioSocket carries raw audio with no "
                "authentication. Keep it on loopback (Asterisk is local) or set allow_public_bind "
                "and firewall the port to the PBX only."
            )
        if self.asr_backend not in ("whisper", "echo"):
            raise VoiceConfigError(f"unknown asr backend: {self.asr_backend}")
        if self.llm_backend not in ("ollama", "script"):
            raise VoiceConfigError(f"unknown llm backend: {self.llm_backend}")
        if self.tts_backend not in ("piper", "wav", "beep"):
            raise VoiceConfigError(f"unknown tts backend: {self.tts_backend}")
        if self.max_calls < 1:
            raise VoiceConfigError("max_calls must be at least 1")
        if self.rtp_start >= self.rtp_end:
            raise VoiceConfigError("rtp_start must be below rtp_end")
        if self.owner_destination:
            from voice.pipeline import is_emergency, is_service_code, normalise_number

            if not normalise_number(self.owner_destination):
                raise VoiceConfigError(f"owner_destination is not dialable: {self.owner_destination!r}")
            if is_emergency(self.owner_destination) or is_service_code(self.owner_destination):
                # A configuration mistake that could cost someone their life, so
                # it is refused at load time rather than at dial time.
                raise VoiceConfigError(
                    f"owner_destination {self.owner_destination!r} is an emergency or service "
                    "number; this agent must never be able to dial it"
                )
        # Warn-by-default: a greeting with TTS on but no model is a common
        # first-run mistake, and it fails late (mid-call). Check what we can.
        return

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[str] = None, *, env: bool = True) -> "VoiceConfig":
        cfg = cls()
        chosen: Optional[Path] = None
        if path:
            chosen = Path(os.path.expanduser(path))
            if not chosen.is_file():
                raise VoiceConfigError(f"config file not found: {chosen}")
        else:
            for candidate in DEFAULT_CONFIG_PATHS:
                candidate_path = Path(os.path.expanduser(candidate))
                if candidate_path.is_file():
                    chosen = candidate_path
                    break
        if chosen:
            cfg._apply_file(chosen)
            cfg.source_path = str(chosen)
        if env:
            cfg._apply_env()
        cfg.validate()
        return cfg

    def _apply_file(self, path: Path) -> None:
        parser = configparser.ConfigParser()
        try:
            parser.read(path, encoding="utf-8")
        except configparser.Error as exc:
            raise VoiceConfigError(f"{path}: {exc}") from exc

        def get(section: str, option: str, default):
            if not parser.has_option(section, option):
                return default
            raw = parser.get(section, option).strip()
            if isinstance(default, bool):
                try:
                    return parser.getboolean(section, option)
                except ValueError as exc:
                    raise VoiceConfigError(f"[{section}] {option} is not a boolean") from exc
            if isinstance(default, int):
                try:
                    return parser.getint(section, option)
                except ValueError as exc:
                    raise VoiceConfigError(f"[{section}] {option} is not an integer") from exc
            if isinstance(default, float):
                try:
                    return parser.getfloat(section, option)
                except ValueError as exc:
                    raise VoiceConfigError(f"[{section}] {option} is not a number") from exc
            return raw

        self.host = get("bridge", "host", self.host)
        self.port = get("bridge", "port", self.port)
        self.max_calls = get("bridge", "max_calls", self.max_calls)
        self.allow_public_bind = get("bridge", "allow_public_bind", self.allow_public_bind)

        self.asr_backend = get("asr", "backend", self.asr_backend)
        self.asr_model = get("asr", "model", self.asr_model)
        self.asr_device = get("asr", "device", self.asr_device)
        self.asr_compute = get("asr", "compute", self.asr_compute)
        self.asr_language = get("asr", "language", self.asr_language)

        self.llm_backend = get("llm", "backend", self.llm_backend)
        self.llm_model = get("llm", "model", self.llm_model)
        self.llm_url = get("llm", "url", self.llm_url)
        self.llm_temperature = get("llm", "temperature", self.llm_temperature)
        self.llm_max_tokens = get("llm", "max_tokens", self.llm_max_tokens)
        self.llm_timeout = get("llm", "timeout", self.llm_timeout)

        self.tts_backend = get("tts", "backend", self.tts_backend)
        self.tts_binary = get("tts", "binary", self.tts_binary)
        self.tts_model = get("tts", "model", self.tts_model)
        self.tts_sample_rate = get("tts", "sample_rate", self.tts_sample_rate)
        self.tts_speed = get("tts", "speed", self.tts_speed)
        self.tts_fallback_wav = get("tts", "fallback_wav", self.tts_fallback_wav)

        self.greeting = get("conversation", "greeting", self.greeting)
        self.farewell = get("conversation", "farewell", self.farewell)
        self.system_prompt = get("conversation", "system_prompt", self.system_prompt)
        self.hangup_phrases = get("conversation", "hangup_phrases", self.hangup_phrases)
        self.max_turns = get("conversation", "max_turns", self.max_turns)
        self.max_call_seconds = get("conversation", "max_call_seconds", self.max_call_seconds)
        self.silence_timeout_seconds = get("conversation", "silence_timeout", self.silence_timeout_seconds)
        self.history_turns = get("conversation", "history_turns", self.history_turns)

        self.barge_in = get("barge_in", "enabled", self.barge_in)
        self.barge_in_threshold = get("barge_in", "threshold", self.barge_in_threshold)
        self.barge_in_frames = get("barge_in", "frames", self.barge_in_frames)

        self.owner_destination = get("dial", "owner", self.owner_destination)
        self.originate_command = get("dial", "originate_command", self.originate_command)
        self.speak_dial_refusal = get("dial", "speak_refusal", self.speak_dial_refusal)

        self.transcript_dir = get("logging", "transcripts", self.transcript_dir)

        self.asterisk_dir = get("asterisk", "dir", self.asterisk_dir)
        self.handset_extension = get("asterisk", "handset_extension", self.handset_extension)
        self.handset_password = get("asterisk", "handset_password", self.handset_password)
        self.handset_name = get("asterisk", "handset_name", self.handset_name)
        self.ai_extension = get("asterisk", "ai_extension", self.ai_extension)
        self.tls_dir = get("asterisk", "tls_dir", self.tls_dir)
        self.rtp_start = get("asterisk", "rtp_start", self.rtp_start)
        self.rtp_end = get("asterisk", "rtp_end", self.rtp_end)

    def _apply_env(self) -> None:
        env = os.environ
        for name, attr, caster in (
            ("PHONE_VOICE_HOST", "host", str),
            ("PHONE_VOICE_PORT", "port", int),
            ("PHONE_VOICE_ASR_MODEL", "asr_model", str),
            ("PHONE_VOICE_ASR_BACKEND", "asr_backend", str),
            ("PHONE_VOICE_ASR_DEVICE", "asr_device", str),
            ("PHONE_VOICE_LLM_MODEL", "llm_model", str),
            ("PHONE_VOICE_LLM_URL", "llm_url", str),
            ("PHONE_VOICE_LLM_BACKEND", "llm_backend", str),
            ("PHONE_VOICE_TTS_MODEL", "tts_model", str),
            ("PHONE_VOICE_TTS_BACKEND", "tts_backend", str),
            ("PHONE_VOICE_TTS_BINARY", "tts_binary", str),
            ("PHONE_VOICE_OWNER", "owner_destination", str),
            ("PHONE_VOICE_TRANSCRIPTS", "transcript_dir", str),
            ("PHONE_VOICE_ASTERISK_DIR", "asterisk_dir", str),
            ("PHONE_VOICE_TLS_DIR", "tls_dir", str),
        ):
            value = env.get(name)
            if value:
                try:
                    setattr(self, attr, caster(value))
                except ValueError as exc:
                    raise VoiceConfigError(f"{name} is not valid: {value!r}") from exc

    # ------------------------------------------------------------------
    def ensure_handset_password(self) -> str:
        """Generate a handset password once, so the SIP app has something sane."""
        if not self.handset_password:
            self.handset_password = secrets.token_urlsafe(18)
        return self.handset_password

    def render(self, *, redact: bool = True) -> str:
        password = "<unset>"
        if self.handset_password:
            password = "<set>" if redact else self.handset_password
        lines = [
            f"config file     : {self.source_path or '<defaults only>'}",
            f"bridge listen   : {self.host}:{self.port} (max {self.max_calls} concurrent calls)",
            f"asr             : {self.asr_backend} / {self.asr_model} (device {self.asr_device})",
            f"llm             : {self.llm_backend} / {self.llm_model} at {self.llm_url}",
            f"tts             : {self.tts_backend} / {self.tts_model}",
            f"greeting        : {self.greeting[:60]}{'...' if len(self.greeting) > 60 else ''}",
            f"barge-in        : {self.barge_in} (threshold {self.barge_in_threshold:.0f})",
            f"owner number    : {self.owner_destination or '<unset: outbound dialling disabled>'}",
            f"transcripts     : {self.transcript_path}",
            f"asterisk dir    : {self.asterisk_dir}",
            f"handset ext     : {self.handset_extension} ({self.handset_name})",
            f"handset password: {password}",
            f"ai extension    : {self.ai_extension}",
            f"RTP window      : {self.rtp_start}-{self.rtp_end}",
            f"tls dir         : {self.tls_path}",
        ]
        return "\n".join(lines)
