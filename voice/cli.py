#!/usr/bin/env python3
"""voice.cli - run the local voice agent, and prove what is broken before a call.

    phone voice doctor            what works on this host, and what cannot work
    phone voice modelfile         build a phone-tuned Ollama model (num_ctx fix)
    phone voice ask "..."         one-shot model check, with timings
    phone voice demo              the whole pipeline offline, no models needed
    phone voice simulate          your own WAV through whisper+ollama+piper
    phone voice provision         write the Asterisk configs
    phone voice serve             answer calls (Asterisk connects to us)
    phone voice call --to owner   ring the owner's phone with the agent on the line
    phone voice dial-test         prove the model cannot dial anything but the owner
    phone voice transcript        read back what was said

Nothing here talks to the network except Ollama on loopback, Asterisk on
loopback, and - only if you ask it to - the one command that originates a call.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid as uuid_module
from datetime import datetime
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice import __version__
from voice.audio import silence, tone, wav_write
from voice.audiosocket import AudioSocketServer, MemoryDuplex
from voice.web import make_server
from voice.config import VoiceConfig, VoiceConfigError
from voice.modelfile import PHONE_SYSTEM_PROMPT, render_modelfile, write_modelfile
from voice.pipeline import (
    BeepTTS,
    CallSession,
    DialRequest,
    EchoASR,
    OllamaLLM,
    PiperTTS,
    PipelineError,
    ScriptedLLM,
    SessionConfig,
    WavTTS,
    WhisperASR,
    extract_dial_request,
    normalise_number,
    run_wav_session,
    validate_dial_request,
)

OK = "  ok   "
WARN = "  warn "
FAIL = "  FAIL "
NOTE = "       "


class Reporter:
    def __init__(self) -> None:
        self.fails = 0
        self.warns = 0

    def ok(self, message: str) -> None:
        print(f"{OK}{message}")

    def warn(self, message: str) -> None:
        self.warns += 1
        print(f"{WARN}{message}")

    def fail(self, message: str) -> None:
        self.fails += 1
        print(f"{FAIL}{message}")

    def note(self, message: str) -> None:
        for line in message.splitlines():
            print(f"{NOTE}{line}")

    def section(self, title: str) -> None:
        print(f"\n{title}")


# ----------------------------------------------------------------------
# adapter construction
# ----------------------------------------------------------------------
def build_asr(cfg: VoiceConfig):
    if cfg.asr_backend == "echo":
        return EchoASR()
    return WhisperASR(cfg.asr_model, cfg.asr_device, cfg.asr_compute, cfg.asr_language)


def build_llm(cfg: VoiceConfig, *, model: str = ""):
    if cfg.llm_backend == "script":
        return ScriptedLLM(["This is the offline scripted model."])
    return OllamaLLM(
        model or cfg.llm_model,
        url=cfg.llm_url,
        temperature=cfg.llm_temperature,
        num_predict=cfg.llm_max_tokens,
        timeout=cfg.llm_timeout,
    )


def build_tts(cfg: VoiceConfig):
    if cfg.tts_backend == "wav":
        return WavTTS(cfg.tts_fallback_wav, fallback_rate=cfg.tts_sample_rate)
    if cfg.tts_backend == "beep":
        # Not a voice: a tone per sentence, for checking the media path (codec,
        # jitter buffer, barge-in) without installing a TTS engine. If dialplan
        # extension 601 (echo) sounds fine and this does too, the plumbing is
        # sound and any remaining problem is the model or the voice.
        return BeepTTS(rate=8000)
    return PiperTTS(cfg.tts_model, cfg.tts_binary, cfg.tts_sample_rate, cfg.tts_speed)


def session_config(cfg: VoiceConfig) -> SessionConfig:
    return SessionConfig(
        system_prompt=cfg.system_prompt or PHONE_SYSTEM_PROMPT,
        greeting=cfg.greeting,
        farewell=cfg.farewell,
        hangup_phrases=cfg.hangup_list,
        owner_destination=cfg.owner_destination,
        max_turns=cfg.max_turns,
        max_call_seconds=cfg.max_call_seconds,
        silence_timeout_seconds=cfg.silence_timeout_seconds,
        barge_in=cfg.barge_in,
        barge_in_threshold=cfg.barge_in_threshold,
        barge_in_frames=cfg.barge_in_frames,
        speak_dial_refusal=cfg.speak_dial_refusal,
        history_turns=cfg.history_turns,
        sample_rate=8000,
    )


def load_config(args) -> VoiceConfig:
    try:
        return VoiceConfig.load(getattr(args, "config", None))
    except VoiceConfigError as exc:
        print(f"error {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def require_models(cfg: VoiceConfig) -> list[str]:
    """Return the list of missing pieces; empty means the stack is complete."""
    problems: list[str] = []
    if cfg.asr_backend == "whisper":
        ok, why = WhisperASR(cfg.asr_model, cfg.asr_device, cfg.asr_compute, cfg.asr_language).available()
        if not ok:
            problems.append(f"asr: {why}")
    if cfg.tts_backend == "piper":
        ok, why = PiperTTS(cfg.tts_model, cfg.tts_binary, cfg.tts_sample_rate, cfg.tts_speed).available()
        if not ok:
            problems.append(f"tts: {why}")
    if cfg.llm_backend == "ollama":
        try:
            OllamaLLM(cfg.llm_model, url=cfg.llm_url, timeout=3.0).models()
        except Exception as exc:  # noqa: BLE001 - any failure here is "unreachable"
            problems.append(f"llm: cannot reach ollama at {cfg.llm_url} ({exc})")
    return problems


# ----------------------------------------------------------------------
# doctor
# ----------------------------------------------------------------------
def gpu_report(reporter: Reporter) -> None:
    if shutil.which("nvidia-smi") is None:
        reporter.warn("no nvidia-smi: whisper will run on CPU (slow) and a 26B model will not fit in RAM")
        return
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        reporter.warn(f"nvidia-smi failed: {exc}")
        return
    for line in result.stdout.decode("utf-8", "replace").strip().splitlines():
        reporter.ok(f"GPU: {line.strip()}")

    # Fit check, done with arithmetic instead of hope. Q4_K_M is roughly 4.5
    # bits per weight plus ~1 GB of context/KV overhead at 8K context.
    for name, params_b in (("gemma4:26b (MoE, ~4B active)", 26), ("gemma4:31b (dense)", 31)):
        estimate = params_b * 4.5 / 8 + 1.0
        reporter.note(f"{name}: ~{estimate:.1f} GB at Q4 + 8K context")


def doctor(args) -> int:
    reporter = Reporter()
    print(f"phone voice doctor {__version__}")

    reporter.section("host")
    reporter.ok(f"python {sys.version.split()[0]} at {sys.executable}")
    if sys.version_info < (3, 9):
        reporter.fail("python 3.9+ required")

    try:
        cfg = VoiceConfig.load(args.config)
    except VoiceConfigError as exc:
        reporter.fail(f"configuration: {exc}")
        reporter.note("run `phone voice provision` first, or copy voice/voice.conf.example to ~/.config/phone/voice.conf")
        return 1
    reporter.ok(f"config: {cfg.source_path or '<defaults>'}")

    reporter.section("bridge")
    if cfg.is_loopback:
        reporter.ok(f"AudioSocket listens on {cfg.host}:{cfg.port} (loopback only)")
    elif cfg.allow_public_bind:
        reporter.warn(f"AudioSocket listens on {cfg.host}:{cfg.port} - publicly reachable, no authentication")
    else:
        reporter.fail(f"host {cfg.host} is not loopback and allow_public_bind is false: serve will refuse")
    try:
        probe = socket.socket()
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((cfg.host, cfg.port))
        probe.close()
        reporter.ok(f"port {cfg.port} is free")
    except OSError as exc:
        reporter.warn(f"port {cfg.port} is not bindable here: {exc}")

    reporter.section("models")
    gpu_report(reporter)
    if cfg.llm_backend == "ollama":
        client = OllamaLLM(cfg.llm_model, url=cfg.llm_url, timeout=5.0)
        try:
            models = client.models()
            reporter.ok(f"ollama at {cfg.llm_url}: {len(models)} model(s)")
            for name in models:
                marker = " <- configured" if name.split(":")[0] == cfg.llm_model.split(":")[0] else ""
                reporter.note(f"{name}{marker}")
            if any(name.split(":")[0] == cfg.llm_model.split(":")[0] for name in models):
                reporter.ok(f"configured model present: {cfg.llm_model}")
            else:
                reporter.fail(f"configured model not pulled: ollama pull {cfg.llm_model}")
            reporter.note("ollama loads every model at 2048 tokens of context by default;")
            reporter.note("run `phone voice modelfile` to build one with num_ctx set explicitly.")
        except Exception as exc:  # noqa: BLE001 - unreachable service, any error counts
            reporter.fail(f"cannot reach ollama at {cfg.llm_url}: {exc}")
            reporter.note("start it with: ollama serve   (and `ollama pull gemma4:26b`)")
    else:
        reporter.warn(f"llm backend is '{cfg.llm_backend}': no model is contacted")

    if cfg.asr_backend == "whisper":
        ok, why = WhisperASR(cfg.asr_model, cfg.asr_device, cfg.asr_compute, cfg.asr_language).available()
        (reporter.ok if ok else reporter.fail)(f"asr: whisper/{cfg.asr_model} " + ("available" if ok else why))
        if ok:
            reporter.note("first transcription downloads the model; run `phone voice simulate` once before a live call")
    else:
        reporter.warn(f"asr backend is '{cfg.asr_backend}': nothing will be transcribed")

    if cfg.tts_backend == "piper":
        ok, why = PiperTTS(cfg.tts_model, cfg.tts_binary, cfg.tts_sample_rate, cfg.tts_speed).available()
        (reporter.ok if ok else reporter.fail)("tts: piper " + ("available" if ok else why))
    else:
        reporter.warn(f"tts backend is '{cfg.tts_backend}'")

    reporter.section("asterisk")
    if shutil.which("asterisk") is None:
        reporter.warn("asterisk is not installed: no calls yet (this is expected until you install it)")
        reporter.note("the configs are already generated: phone voice provision --dir <dir>")
    else:
        try:
            version = subprocess.run(["asterisk", "-V"], stdout=subprocess.PIPE, timeout=10, check=False)
            reporter.ok(version.stdout.decode("utf-8", "replace").strip().splitlines()[0])
        except (OSError, subprocess.TimeoutExpired) as exc:
            reporter.warn(f"asterisk -V failed: {exc}")
        conf_dir = Path(os.path.expanduser(cfg.asterisk_dir))
        for name in ("pjsip.conf", "extensions.conf", "rtp.conf"):
            if (conf_dir / name).is_file():
                reporter.ok(f"{conf_dir / name}")
            else:
                reporter.warn(f"{conf_dir / name} missing: phone voice provision --dir {conf_dir}")
        reporter.note(f"dial {cfg.ai_extension} from the handset extension {cfg.handset_extension} to reach the agent")
        reporter.note(f"RTP window {cfg.rtp_start}-{cfg.rtp_end} must match the firewall rule exactly")

    reporter.section("remote client")
    try:
        cfg.validate_web()
        if cfg.web_is_loopback:
            reporter.ok(f"serves on {cfg.web_host}:{cfg.web_port} (loopback only, for a tunnel)")
        else:
            reporter.warn(f"serves on {cfg.web_host}:{cfg.web_port} - reachable from the network; keep the token")
        reporter.ok("TLS configured" if cfg.web_cert else "no TLS: browsers need HTTPS for the microphone, apps do not")
        reporter.ok("token set" if cfg.web_token else "no token configured yet (generated per run)")
        reporter.note("start it with: phone voice web   (see docs/REMOTE.md for the away-from-home story)")
    except VoiceConfigError as exc:
        reporter.fail(f"remote client: {exc}")

    reporter.section("dial safety")
    cases: list[tuple[str, bool]] = [("911", False), ("988", False), ("+1 555 010 9999", False)]
    if cfg.owner_destination:
        cases.append((cfg.owner_destination, True))
    else:
        reporter.warn("no owner destination: outbound dialling is disabled, which is the safe default")
    for destination, expected in cases:
        allowed, reason = validate_dial_request(DialRequest(destination), cfg.owner_destination)
        label = f"{destination!r} -> {'allowed' if allowed else 'refused'}"
        if allowed == expected:
            reporter.ok(label)
        else:
            reporter.fail(f"{label} (expected {'allowed' if expected else 'refused'}: {reason})")
    if not cfg.owner_destination:
        reporter.note("set [dial] owner in the config to allow the agent to call one number - yours")

    reporter.section("what this host cannot verify")
    reporter.note("No sound card, no PBX and no telephone line are touched by this command.")
    reporter.note("Everything below doctor's reach must be checked on your own machine:")
    reporter.note("* a real call: dial the AI extension from your handset, or `phone voice call`")
    reporter.note("* audio levels and echo: use dialplan extension 601 (echo test) first")
    reporter.note("* real latency: `phone voice simulate --in you.wav` measures it end to end")
    reporter.note("* a real phone number: that is a rented DID, or your own SIP app on the LAN")

    print()
    if reporter.fails:
        print(f"{reporter.fails} problem(s), {reporter.warns} warning(s)")
        return 1
    print(f"no blocking problems; {reporter.warns} warning(s)" + ("" if reporter.warns else ""))
    return 0


# ----------------------------------------------------------------------
# serve
# ----------------------------------------------------------------------
def make_dialer(cfg: VoiceConfig):
    """The only place a dial is ever executed. Used by the model's intent path."""
    def dial(request: DialRequest) -> tuple[bool, str]:
        allowed, reason = validate_dial_request(request, cfg.owner_destination)
        if not allowed:
            return False, reason
        return originate(cfg, cfg.owner_destination, print_command=False)
    return dial


def destination_channel(cfg: VoiceConfig, number: str) -> tuple[str, str]:
    """Where Asterisk should send the call. Returns (channel, warning)."""
    digits = normalise_number(number)
    if digits.lstrip("+") == normalise_number(cfg.handset_extension).lstrip("+"):
        return f"PJSIP/{cfg.handset_extension}", ""
    # Punctuation is stripped here, not passed through: a channel string with a
    # space in it ("PJSIP/+1 610 555 0100@trunk") is not a dial string at all.
    return (
        f"PJSIP/{digits}@trunk-provider",
        "this destination needs the SIP trunk from `phone voice provision` (trunk.conf.example)",
    )


def originate(cfg: VoiceConfig, number: str, *, print_command: bool = True) -> tuple[bool, str]:
    """Ask Asterisk to call ``number`` and attach the audio bridge to that leg."""
    allowed, reason = validate_dial_request(DialRequest(number), cfg.owner_destination)
    if not allowed:
        return False, reason
    uuid_text = str(uuid_module.uuid4())
    channel, warning = destination_channel(cfg, number)
    command = (
        f"channel originate {channel} application AudioSocket "
        f"{uuid_text},{cfg.host},{cfg.port}"
    )
    if print_command:
        print(f"asterisk -rx \"{command}\"")
    if warning:
        return False, warning
    if shutil.which("asterisk") is None:
        return False, (
            "asterisk is not installed here; run the command above on the PBX host "
            f"(with `phone voice serve` running there on port {cfg.port})"
        )
    try:
        result = subprocess.run(
            ["asterisk", "-rx", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"asterisk CLI failed: {exc}"
    output = result.stdout.decode("utf-8", "replace").strip()
    if print_command and output:
        print(output)
    if result.returncode != 0:
        return False, output or f"asterisk exited {result.returncode}"
    return True, output


def serve_forever(cfg: VoiceConfig, args) -> int:
    # getattr, not args.host: serve is also called from tests and from an
    # embedding supervisor with a hand-made namespace.
    if getattr(args, "host", None):
        cfg.host = args.host
    if getattr(args, "port", None):
        cfg.port = args.port
    cfg.validate()
    asr = build_asr(cfg)
    llm = build_llm(cfg, model=args.model or "")
    tts = build_tts(cfg)
    transcript_dir = cfg.transcript_path
    transcript_dir.mkdir(parents=True, exist_ok=True)

    def handle(duplex, info) -> None:
        call_id = duplex.call_id or "unknown"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        transcript_path = transcript_dir / f"{stamp}-{call_id[:8]}.jsonl"
        session = CallSession(
            duplex,
            asr,
            llm,
            tts,
            session_config(cfg),
            on_dial=None if args.no_dial else make_dialer(cfg),
            transcript_path=str(transcript_path),
        )
        reason = session.run()
        print(f"\ncall {call_id} ended: {reason}")
        print(f"transcript: {transcript_path}")

    server = AudioSocketServer((cfg.host, cfg.port), handle, max_calls=cfg.max_calls)
    print(f"listening for Asterisk on {cfg.host}:{cfg.port} (max {cfg.max_calls} calls)")
    print(f"models: asr={cfg.asr_backend}/{cfg.asr_model} llm={cfg.llm_backend}/{cfg.llm_model} tts={cfg.tts_backend}")
    print(f"dial the AI extension ({cfg.ai_extension}) from the handset, or run `phone voice call --to owner`")
    if args.once:
        print("waiting for exactly one call (--once)")

    stopping = {"now": False}

    def stop(_signum=None, _frame=None):
        stopping["now"] = True
        print("\nshutting down after the current call...")
        server.shutdown()

    # Signals can only be installed from the main thread. When serve is started
    # from a thread (tests, or an embedding supervisor) the KeyboardInterrupt
    # path still works, there is just nothing to install.
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)

    try:
        if args.once:
            # Serve until the first call has finished, then stop.
            finished = threading.Event()
            original = handle

            def once_handler(duplex, info) -> None:
                try:
                    original(duplex, info)
                finally:
                    finished.set()
                    server.shutdown()

            server.handler = once_handler
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            while not finished.wait(0.5):
                if stopping["now"]:
                    break
            thread.join(timeout=5)
        else:
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print(f"served {server.accepted} call(s), refused {server.rejected}")
    return 0


# ----------------------------------------------------------------------
# offline drivers
# ----------------------------------------------------------------------
def make_caller_wav(path: str, rate: int = 8000) -> str:
    """Two synthetic utterances: a tone where speech would be, then silence.

    Two of them rather than one so the caller sees a real turn boundary (and a
    real end-of-call) rather than a single exchange followed by a timeout.
    """
    # A person waits for the greeting before speaking; the file does too, so the
    # demo shows turn-taking rather than the agent being talked over instantly.
    pcm = silence(rate, 1500)
    for pitch in (220.0, 180.0):
        pcm += tone(rate, pitch, 1200, amplitude=8000)
        pcm += silence(rate, 900)
    wav_write(path, pcm, rate)
    return path


def _demo_dial(request: DialRequest) -> tuple[bool, str]:
    print(f"       (the model asked to dial {request.destination!r}; demo mode refuses)")
    return False, "demo mode"


def _no_dial(request: DialRequest) -> tuple[bool, str]:
    return False, "simulate mode never dials"


def demo(args) -> int:
    """Prove the plumbing with no models installed.

    EchoASR repeats a scripted sentence, ScriptedLLM answers it, BeepTTS emits a
    tone. That is not an assistant - it is a test that the turn-taking, the
    pacing, the barge-in path and the WAV plumbing all work before whisper,
    Ollama and Piper enter the picture.
    """
    cfg = load_config(args)
    workdir = Path(args.workdir or os.path.expanduser("~/voice"))
    workdir.mkdir(parents=True, exist_ok=True)
    caller_wav = args.input or make_caller_wav(str(workdir / "demo-caller.wav"))
    output_wav = args.output or str(workdir / "demo-reply.wav")

    asr = EchoASR(script=["hello, are you there?", "what time is it?"])
    llm = ScriptedLLM(
        replies=[
            "Yes, I am here, and I am running on your machine.",
            "It is [[dial:owner|time check]] whatever the clock says.",
        ]
    )
    tts = BeepTTS(rate=8000, ms=250, pitch=520.0, echo=True)
    config = session_config(cfg)
    config.silence_timeout_seconds = args.silence_timeout
    session, spoken, _rate = run_wav_session(
        caller_wav,
        asr,
        llm,
        tts,
        config,
        MemoryDuplex,
        output_wav=output_wav,
        on_dial=_demo_dial,
        realtime=not args.fast,
    )
    print(f"ended: {session.ended_reason}")
    for index, timings in enumerate(session.turns, 1):
        print(f"turn {index}: {timings.line()}")
    print(f"agent audio written to {output_wav} ({len(spoken)} bytes)")
    print("this ran with no ASR, no model and no TTS installed; swap them in with")
    print("`phone voice simulate --in <your speech>.wav` to measure the real thing")
    return 0


def simulate(args) -> int:
    """Your own audio through the real stack, without a PBX.

    This is the honest latency measurement: it uses whatever ``asr``, ``llm`` and
    ``tts`` are configured, so the numbers it prints are the numbers you would
    hear on a call, minus network jitter.
    """
    cfg = load_config(args)
    problems = require_models(cfg)
    if problems:
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        print("install those, or use `--fake` to exercise the plumbing without them", file=sys.stderr)
        if not args.fake:
            return 1
    if args.fake:
        asr = EchoASR(script=args.script or ["this is a fake transcription"])
        llm = ScriptedLLM(["This is a fake reply, produced without a model."])
        tts = BeepTTS(rate=8000, echo=True)
    else:
        asr = build_asr(cfg)
        llm = build_llm(cfg, model=args.model or "")
        tts = build_tts(cfg)

    input_wav = args.input
    if not input_wav:
        if args.workdir:
            workdir = Path(os.path.expanduser(args.workdir))
            workdir.mkdir(parents=True, exist_ok=True)
        else:
            # A stand-in the user did not ask for does not belong in ~/voice.
            workdir = Path(tempfile.mkdtemp(prefix="phone-voice-simulate-"))
        input_wav = make_caller_wav(str(workdir / "simulate-caller.wav"))
        print(f"no --in given; generated a tone stand-in at {input_wav}")
    output_wav = args.output or ""
    config = session_config(cfg)
    config.silence_timeout_seconds = args.silence_timeout
    session, spoken, rate = run_wav_session(
        input_wav,
        asr,
        llm,
        tts,
        config,
        MemoryDuplex,
        output_wav=output_wav,
        on_dial=_no_dial,
        realtime=not args.fast,
    )
    print(f"ended: {session.ended_reason}")
    for index, timings in enumerate(session.turns, 1):
        print(f"turn {index}: {timings.line()}")
    if session.turns:
        worst = max(session.turns, key=lambda t: t.total_ms)
        print(f"slowest turn: {worst.total_ms:.0f} ms before playback starts (human tolerance is ~1000 ms)")
    if output_wav:
        print(f"agent audio: {output_wav} ({len(spoken)} bytes at {rate} Hz)")
    return 0


# ----------------------------------------------------------------------
# ask / modelfile / provision / call / misc
# ----------------------------------------------------------------------
def ask(args) -> int:
    cfg = load_config(args)
    client = build_llm(cfg, model=args.model or "")
    system = args.system or cfg.system_prompt or PHONE_SYSTEM_PROMPT
    prompt = " ".join(args.prompt) if args.prompt else sys.stdin.read().strip()
    if not prompt:
        print("nothing to ask: pass a prompt or pipe one in", file=sys.stderr)
        return 2
    started = time.monotonic()
    first = None
    pieces: list[str] = []
    try:
        for delta in client.stream([{"role": "user", "content": prompt}], system):
            if first is None:
                first = time.monotonic()
            pieces.append(delta)
            if not args.quiet:
                sys.stdout.write(delta)
                sys.stdout.flush()
    except PipelineError as exc:
        print(f"\nerror {exc}", file=sys.stderr)
        return 1
    reply = "".join(pieces)
    spoken, dial = extract_dial_request(reply)
    if not args.quiet:
        print()
    if dial is not None:
        allowed, reason = validate_dial_request(dial, cfg.owner_destination)
        print(f"dial intent: {dial.destination!r} -> {'allowed' if allowed else 'refused'} {reason}")
    if args.timings:
        elapsed = time.monotonic() - started
        print(f"model      : {getattr(client, 'model', '?')}")
        print(f"first token: {(first - started) * 1000:.0f} ms" if first else "first token: never")
        print(f"total      : {elapsed * 1000:.0f} ms for {len(spoken)} characters")
        if first and elapsed > (first - started):
            rate_ps = len(spoken) / (elapsed - (first - started))
            print(f"throughput : {rate_ps:.0f} characters/second")
    return 0


def modelfile(args) -> int:
    cfg = load_config(args)
    base = args.base or cfg.llm_model
    text = render_modelfile(
        base,
        num_ctx=args.num_ctx,
        temperature=cfg.llm_temperature,
        num_predict=cfg.llm_max_tokens,
    )
    target = write_modelfile(args.output, text)
    name = args.name
    print(f"wrote {target} (base {base}, num_ctx {args.num_ctx})")
    print()
    print(f"  ollama create {name} -f {target}")
    print(f"  phone voice ask --model {name} \"say hello in six words\"")
    print()
    print(f"then set `[llm] model = {name}` in {cfg.source_path or '~/.config/phone/voice.conf'}")
    print("Ollama's default context is 2048 tokens for every model; the Modelfile sets it")
    print("explicitly so long conversations are not silently truncated.")
    return 0


def provision(args) -> int:
    cfg = load_config(args)
    from voice import asterisk as asterisk_config

    directory = args.dir or cfg.asterisk_dir
    settings_changed = False
    if not cfg.handset_password:
        cfg.ensure_handset_password()
        settings_changed = True
        if not args.password:
            args.password = cfg.handset_password
    if args.host and args.host != cfg.host:
        cfg.host = args.host
    if args.port and args.port != cfg.port:
        cfg.port = args.port
    if args.password:
        cfg.handset_password = args.password

    try:
        written = asterisk_config.provision(cfg, directory, force=args.force)
    except (OSError, FileExistsError) as exc:
        print(f"error {exc}", file=sys.stderr)
        print("hint: --dir ~/asterisk-conf to stage the files, then copy them as root", file=sys.stderr)
        return 1
    stub = asterisk_config.write_trunk_stub(directory)
    cert, key = asterisk_config.tls_files(cfg)
    if not (cert.is_file() and key.is_file()):
        print()
        print(f"note: {cert} / {key.name} do not exist yet.")
        print("Asterisk's TLS transport will not start without them; generate them with:")
        print(f"  {Path(__file__).resolve().parent.parent / 'sip' / 'gen-certs.sh'}")
    for path in written:
        print(f"wrote {path}")
    print(f"wrote {stub}")

    if not cfg.source_path:
        example = Path(__file__).resolve().parent / "voice.conf.example"
        print()
        print("no config file yet: copy the example and edit it")
        print(f"  mkdir -p ~/.config/phone && cp {example} ~/.config/phone/voice.conf")
    if settings_changed and cfg.source_path:
        print()
        print(f"generated a handset SIP password; put it in {cfg.source_path} as handset_password")
        print("and register your SIP app with it (it is in pjsip.conf too)")

    print()
    print("next, on the machine running Asterisk:")
    print(f"  sudo cp {directory}/{{pjsip,extensions,rtp}}.conf /etc/asterisk/")
    print("  sudo systemctl restart asterisk")
    print('  sudo asterisk -rx "pjsip show endpoints"      # your handset should register')
    print(f"  sudo phone voice serve                       # listens on {cfg.host}:{cfg.port}")
    print(f"  then dial {cfg.ai_extension} from the handset extension {cfg.handset_extension}")
    print()
    print("the firewall must allow the handset's SIP/TLS port and the RTP window:")
    print(f"  UDP {cfg.rtp_start}-{cfg.rtp_end} from the handset only, TCP 5061 from the handset only")
    return 0


def call(args) -> int:
    cfg = load_config(args)
    target = cfg.owner_destination if args.to in ("owner", "", None) else args.to
    if not target:
        print("no owner destination configured: set [dial] owner in the config", file=sys.stderr)
        return 2
    if not cfg.owner_destination:
        print("refusing to dial: [dial] owner is unset, so no destination is authorised", file=sys.stderr)
        return 2
    if normalise_number(target) != normalise_number(cfg.owner_destination):
        print(
            f"refusing to dial {target}: only the configured owner destination may be dialled",
            file=sys.stderr,
        )
        return 2
    if args.dry_run:
        channel, _ = destination_channel(cfg, target)
        uuid_text = str(uuid_module.uuid4())
        print(
            f"asterisk -rx \"channel originate {channel} application AudioSocket "
            f"{uuid_text},{cfg.host},{cfg.port}\""
        )
        return 0
    ok, message = originate(cfg, target, print_command=True)
    if not ok:
        print(f"FAIL {message}", file=sys.stderr)
        return 1
    print(f"call placed (agent audio is served on {cfg.host}:{cfg.port})")
    return 0


def web(args) -> int:
    """Serve the remote client: browser page, WebSocket audio, and the small API."""
    cfg = load_config(args)
    if args.host:
        cfg.web_host = args.host
    if args.port:
        cfg.web_port = args.port
    if args.cert:
        cfg.web_cert = args.cert
    if args.key:
        cfg.web_key = args.key
    if args.allow_remote:
        cfg.web_allow_remote = True
    if args.max_calls:
        cfg.web_max_calls = args.max_calls
    try:
        cfg.validate_web()
    except VoiceConfigError as exc:
        print(f"error {exc}", file=sys.stderr)
        return 2

    token_generated = False
    if not cfg.web_token and args.token is not False:
        cfg.web_token = args.token or cfg.ensure_web_token()
        token_generated = True

    asr = build_asr(cfg)
    llm = build_llm(cfg, model=args.model or "")
    tts = build_tts(cfg)
    transcripts = cfg.transcript_path
    transcripts.mkdir(parents=True, exist_ok=True)

    def session_factory(duplex) -> CallSession:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        transcript_path = transcripts / f"{stamp}-{str(uuid_module.uuid4())[:8]}-remote.jsonl"
        return CallSession(
            duplex,
            asr,
            llm,
            tts,
            session_config(cfg),
            on_dial=None if args.no_dial else make_dialer(cfg),
            transcript_path=str(transcript_path),
            on_event=duplex.send_event,
            notes_path=cfg.notes_file,
        )

    dialer = None if args.no_dial else (lambda number: originate(cfg, number, print_command=False))
    server = make_server(cfg, session_factory, originate=dialer)
    host, port = server.server_address[0], server.server_address[1]
    scheme = "https" if cfg.web_cert else "http"
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host

    # The token goes in the URL fragment (never sent to the server, so it stays out
    # of its logs) unless the operator prefers to keep it off the screen.
    show_token = cfg.web_token and (token_generated or args.print_token or not cfg.web_token)
    print(f"phone voice web on {host}:{port} ({'TLS' if cfg.web_cert else 'plain http'})")
    print(f"  page      : {scheme}://{shown}:{port}/#token={cfg.web_token if show_token else '<your token>'}")
    print(f"  websocket : {'wss' if cfg.web_cert else 'ws'}://{shown}:{port}/ws?token=<token>")
    print("  api       : POST /api/call-me | /api/ask | /api/note, GET /api/transcript | /healthz")
    print(f"  models    : asr={cfg.asr_backend}/{cfg.asr_model} llm={cfg.llm_backend}/{cfg.llm_model} tts={cfg.tts_backend}")
    if token_generated:
        print("  token     : generated for this run; set [web] token in the config to keep it")
    if not cfg.web_cert:
        print("  note      : a browser needs HTTPS for microphone access; an app inside a")
        print("              tunnel does not. See docs/REMOTE.md.")
    if not cfg.owner_destination:
        print("  note      : no [dial] owner, so 'Ring my phone' is disabled and /api/call-me refuses")
    print("  stop with Ctrl-C")

    stopping = {"now": False}

    def stop(_signum=None, _frame=None):
        stopping["now"] = True
        print("\nshutting down...")
        server.shutdown()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print(f"served {server.calls_accepted} call(s), refused {server.calls_refused}")
    return 0


def note(args) -> int:
    """Write the note the agent reads at the start of the next call."""
    cfg = load_config(args)
    if not cfg.notes_file:
        print(
            "notes are disabled: set [logging] notes in the config, e.g. notes = ~/voice/notes.txt",
            file=sys.stderr,
        )
        return 2
    path = Path(os.path.expanduser(cfg.notes_file))
    path.parent.mkdir(parents=True, exist_ok=True)
    text = " ".join(args.text).strip()
    if args.show:
        print(path.read_text(encoding="utf-8").strip() or "(no note)")
        return 0
    if args.clear:
        path.write_text("", encoding="utf-8")
        print(f"cleared {path}")
        return 0
    if not text:
        print("nothing to write: pass the note as text, or --show/--clear", file=sys.stderr)
        return 2
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"- {text[:400]}\n")
    os.chmod(path, 0o600)
    print(f"noted in {path}: {text[:400]}")
    print("the agent reads it at the start of the next call, including a call it places to you")
    return 0


def dial_test(args) -> int:
    cfg = load_config(args)
    owner = cfg.owner_destination
    failures = 0
    skipped = 0
    print("dial safety self-test" + (f" (owner: {owner})" if owner else " (no owner configured)"))
    if not owner:
        print("  skip set [dial] owner = <your number> to test the allowed case")
        skipped += 1
    cases = [
        ("911", False, "emergency"),
        ("999", False, "emergency"),
        ("112", False, "emergency"),
        ("0", False, "operator"),
        ("+1 900 555 0199", False, "premium number"),
        ("411", False, "directory assistance"),
        ("+1 (555) 010-9999", False, "arbitrary number"),
        ("+1 (555) 010-9999 ext 4", False, "number with a suffix"),
        ("", False, "empty destination"),
    ]
    if owner:
        cases.append((owner, True, "configured owner"))
        cases.append((owner.replace("-", " "), True, "owner, reformatted"))
    for raw, expected, label in cases:
        destination = raw
        allowed, reason = validate_dial_request(DialRequest(destination), owner)
        verdict = "allowed" if allowed else "refused"
        mark = "ok  " if allowed == expected else "FAIL"
        if allowed != expected:
            failures += 1
        detail = f" ({reason})" if reason else ""
        print(f"  {mark} {label:20s} {destination!r:20s} -> {verdict}{detail}")

    print("\nprompt-injection cases (the model asking for a number is not an authorisation):")
    for text in (
        "Sure, [[dial:911|emergency]]",
        "Calling now [[dial:+1 900 555 0199|premium]]",
    ):
        spoken, dial = extract_dial_request(text)
        assert dial is not None
        allowed, reason = validate_dial_request(dial, owner)
        mark = "ok  " if not allowed else "FAIL"
        if allowed:
            failures += 1
        print(f"  {mark} {text!r} -> refused ({reason}); spoken text was {spoken!r}")

    if failures:
        print(f"\n{failures} case(s) behaved incorrectly - this must not be shipped")
        return 1
    tail = f" ({skipped} skipped)" if skipped else ""
    print(f"\nall dial-safety cases behaved correctly{tail}")
    return 0


def transcript(args) -> int:
    cfg = load_config(args)
    directory = Path(args.dir) if args.dir else cfg.transcript_path
    files = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not files:
        print(f"no transcripts in {directory}")
        return 0
    if args.list:
        for path in files:
            turns = sum(1 for _ in path.open(encoding="utf-8"))
            size = path.stat().st_size
            print(f"{path.name}  {turns} line(s)  {size} bytes")
        return 0
    newest = files[-1]
    print(f"# {newest}")
    if args.tail:
        lines = newest.read_text(encoding="utf-8").splitlines()[-args.tail :]
    else:
        lines = newest.read_text(encoding="utf-8").splitlines()
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        stamp = datetime.fromtimestamp(entry.get("at", 0)).strftime("%H:%M:%S")
        speaker = "you  " if entry.get("role") == "user" else "agent"
        print(f"[{stamp}] {speaker}: {entry.get('text', '')}")
    return 0


# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phone voice",
        description="A local voice agent on a telephone line: Asterisk + whisper + Ollama + Piper.",
    )
    parser.add_argument("--version", action="version", version=f"phone voice {__version__}")
    parser.add_argument("--config", help="path to voice.conf (default ~/.config/phone/voice.conf)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="check every piece before a call")
    p.set_defaults(func=doctor)

    p = sub.add_parser("serve", help="answer calls; Asterisk connects here")
    p.add_argument("--once", action="store_true", help="serve exactly one call then exit")
    p.add_argument("--host", help="bind address (default from the config: 127.0.0.1)")
    p.add_argument("--port", type=int, help="bind port (default from the config: 9092)")
    p.add_argument("--model", help="override the Ollama model for this run")
    p.add_argument("--no-dial", action="store_true", help="refuse every dial intent, even to the owner")
    p.set_defaults(func=serve_forever)

    p = sub.add_parser("demo", help="the pipeline offline, no models required")
    p.add_argument("--in", dest="input", help="caller WAV (default: generated tone)")
    p.add_argument("--out", dest="output", help="where to write the agent's audio")
    p.add_argument("--workdir", help="directory for demo files (default ~/voice)")
    p.add_argument("--fast", action="store_true", help="do not pace playback in real time")
    p.add_argument("--silence-timeout", type=float, default=3.0, help="seconds of silence before the file driver ends the call (default 3)")
    p.set_defaults(func=demo)

    p = sub.add_parser("simulate", help="your own WAV through the configured models")
    p.add_argument("--in", dest="input", help="your speech, 16-bit mono WAV")
    p.add_argument("--out", dest="output", help="write the agent's reply here")
    p.add_argument("--workdir")
    p.add_argument("--model", help="override the Ollama model")
    p.add_argument("--fake", action="store_true", help="skip the models entirely (plumbing check)")
    p.add_argument("--script", action="append", help="with --fake: what the fake ASR should hear")
    p.add_argument("--fast", action="store_true")
    p.add_argument("--silence-timeout", type=float, default=3.0, help="seconds of silence before the file driver ends the call (default 3)")
    p.set_defaults(func=simulate)

    p = sub.add_parser("ask", help="one-shot prompt to the local model")
    p.add_argument("prompt", nargs="*", help="the prompt (or pipe it on stdin)")
    p.add_argument("--model", help="override the Ollama model")
    p.add_argument("--system", help="override the system prompt")
    p.add_argument("--timings", action="store_true", help="print first-token and total latency")
    p.add_argument("--quiet", action="store_true", help="do not print the reply")
    p.set_defaults(func=ask)

    p = sub.add_parser("modelfile", help="write a phone-tuned Ollama Modelfile")
    p.add_argument("--base", help="base model (default: the configured llm model)")
    p.add_argument("--name", default="phone-voice", help="name of the derived model")
    p.add_argument("--num-ctx", type=int, default=8192, help="context window (Ollama defaults to 2048!)")
    p.add_argument("--output", default="~/.config/phone/Modelfile")
    p.set_defaults(func=modelfile)

    p = sub.add_parser("provision", help="write the Asterisk configuration")
    p.add_argument("--dir", help="where to write it (default: the configured asterisk dir)")
    p.add_argument("--host", help="address Asterisk should connect back to (default 127.0.0.1)")
    p.add_argument("--port", type=int, help="bridge port")
    p.add_argument("--password", help="handset SIP password (generated if omitted)")
    p.add_argument("--force", action="store_true", help="back up and replace existing files")
    p.set_defaults(func=provision)

    p = sub.add_parser("call", help="ring the owner's phone with the agent on the line")
    p.add_argument("--to", default="owner", help="only the configured owner destination is accepted")
    p.add_argument("--dry-run", action="store_true", help="print the Asterisk command and stop")
    p.set_defaults(func=call)

    p = sub.add_parser("web", help="serve the browser/app client: call the agent from anywhere")
    p.add_argument("--host", help="bind address (default 127.0.0.1; see docs/REMOTE.md)")
    p.add_argument("--port", type=int, help="bind port (default 8443)")
    p.add_argument("--token", help="access token (default: generated for this run)")
    p.add_argument("--no-token", dest="token", action="store_const", const=False,
                   help="disable the token entirely (loopback only)")
    p.add_argument("--print-token", action="store_true", help="print the token even when it is configured")
    p.add_argument("--cert", help="TLS certificate chain (PEM); required for a browser microphone on anything but localhost")
    p.add_argument("--key", help="TLS private key (PEM)")
    p.add_argument("--allow-remote", action="store_true", help="permit a non-loopback bind (a token is still required)")
    p.add_argument("--max-calls", type=int, help="concurrent remote calls (default 2)")
    p.add_argument("--model", help="override the Ollama model")
    p.add_argument("--no-dial", action="store_true", help="refuse every dial intent, including /api/call-me")
    p.set_defaults(func=web)

    p = sub.add_parser("note", help="leave a note for the agent to read at the start of the next call")
    p.add_argument("text", nargs="*", help="the note")
    p.add_argument("--show", action="store_true", help="print the current note")
    p.add_argument("--clear", action="store_true", help="delete the current note")
    p.set_defaults(func=note)

    p = sub.add_parser("dial-test", help="prove the model cannot dial anything but the owner")
    p.set_defaults(func=dial_test)

    p = sub.add_parser("transcript", help="read back what was said on past calls")
    p.add_argument("--dir", help="transcript directory")
    p.add_argument("--list", action="store_true", help="list files instead of printing one")
    p.add_argument("--tail", type=int, default=0, help="only the last N entries")
    p.set_defaults(func=transcript)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
