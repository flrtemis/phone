"""Config loading, the Asterisk generator, and the `voice` command line.

The generator is tested by reading what it writes, because the dangerous
mistakes there are single lines: an optimistic SRTP flag, a `0.0.0.0/0`
identify, or a dialplan pattern that reaches the whole PSTN.
"""

from __future__ import annotations

import argparse
import json
import socket
import threading
import time
import uuid
from pathlib import Path

import pytest

from voice import asterisk as asterisk_config
from voice import cli
from voice.config import VoiceConfig, VoiceConfigError
from voice.modelfile import PHONE_SYSTEM_PROMPT, render_modelfile


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "voice.conf"
    path.write_text(body, encoding="utf-8")
    return path


OFFLINE_CONFIG = """\
[bridge]
port = 0
max_calls = 1
[asr]
backend = echo
[llm]
backend = script
[tts]
backend = beep
[silence]
[conversation]
greeting = Hi from the test.
silence_timeout = 1
[dial]
owner = +1 610 555 0100
[logging]
transcripts = {transcripts}
[asterisk]
dir = {asterisk_dir}
handset_extension = 100
handset_password = test-password
"""


def offline_config(tmp_path: Path, **overrides) -> Path:
    return write_config(
        tmp_path,
        OFFLINE_CONFIG.format(transcripts=tmp_path / "transcripts", asterisk_dir=tmp_path / "asterisk"),
    )


# ----------------------------------------------------------------------
# configuration
# ----------------------------------------------------------------------
def test_defaults_are_loopback_only():
    cfg = VoiceConfig()
    assert cfg.host == "127.0.0.1"
    assert cfg.is_loopback is True
    assert cfg.llm_model.startswith("gemma4:")
    cfg.validate()


def test_config_file_is_read_and_named_in_the_report(tmp_path):
    path = write_config(
        tmp_path,
        "[llm]\nmodel = gemma4:31b\ntemperature = 0.2\n[tts]\nsample_rate = 16000\n[dial]\nowner = +1 610 555 0100\n",
    )
    cfg = VoiceConfig.load(str(path), env=False)
    assert cfg.llm_model == "gemma4:31b"
    assert cfg.llm_temperature == pytest.approx(0.2)
    assert cfg.tts_sample_rate == 16000
    assert cfg.owner_destination == "+1 610 555 0100"
    assert str(path) in cfg.render()


def test_environment_overrides_the_file(tmp_path, monkeypatch):
    path = write_config(tmp_path, "[llm]\nmodel = gemma4:26b\n")
    monkeypatch.setenv("PHONE_VOICE_LLM_MODEL", "gemma4:31b")
    monkeypatch.setenv("PHONE_VOICE_PORT", "9191")
    cfg = VoiceConfig.load(str(path))
    assert cfg.llm_model == "gemma4:31b"
    assert cfg.port == 9191


def test_a_missing_config_file_is_an_error_not_a_silent_default(tmp_path):
    with pytest.raises(VoiceConfigError, match="not found"):
        VoiceConfig.load(str(tmp_path / "nope.conf"), env=False)


def test_a_public_bind_is_refused_unless_it_is_explicit():
    cfg = VoiceConfig(host="0.0.0.0")
    with pytest.raises(VoiceConfigError, match="refusing to listen"):
        cfg.validate()
    cfg.allow_public_bind = True
    cfg.validate()


def test_unknown_backends_and_bad_ports_are_refused():
    with pytest.raises(VoiceConfigError, match="unknown asr backend"):
        VoiceConfig(asr_backend="magic").validate()
    with pytest.raises(VoiceConfigError, match="unknown llm backend"):
        VoiceConfig(llm_backend="gpt").validate()
    with pytest.raises(VoiceConfigError, match="unknown tts backend"):
        VoiceConfig(tts_backend="festival").validate()
    with pytest.raises(VoiceConfigError, match="port out of range"):
        VoiceConfig(port=70000).validate()
    with pytest.raises(VoiceConfigError, match="rtp_start"):
        VoiceConfig(rtp_start=4010, rtp_end=4000).validate()


def test_a_non_dialable_owner_is_refused():
    with pytest.raises(VoiceConfigError, match="not dialable"):
        VoiceConfig(owner_destination="call my mum").validate()


def test_the_password_is_generated_once_and_never_printed_in_a_report(tmp_path):
    cfg = VoiceConfig()
    first = cfg.ensure_handset_password()
    assert len(first) >= 16
    assert cfg.ensure_handset_password() == first, "regenerating would silently break the handset"
    assert first not in cfg.render()
    assert "<set>" in cfg.render()
    assert first in cfg.render(redact=False)


# ----------------------------------------------------------------------
# the Asterisk generator: one wrong line here is a security hole
# ----------------------------------------------------------------------
def test_provision_writes_the_three_files_and_a_trunk_stub(tmp_path):
    cfg = VoiceConfig(handset_password="pw", owner_destination="+1 610 555 0100")
    written = asterisk_config.provision(cfg, str(tmp_path / "ast"))
    names = sorted(path.name for path in written)
    assert names == ["extensions.conf", "pjsip.conf", "rtp.conf"]
    assert (tmp_path / "ast" / "trunk.conf.example").is_file() is False
    stub = asterisk_config.write_trunk_stub(str(tmp_path / "ast"))
    assert "trunk-provider" in stub.read_text()
    assert "OPTIONAL" in stub.read_text()


def test_generated_pjsip_keeps_encryption_mandatory(tmp_path):
    cfg = VoiceConfig(handset_password="pw", handset_name="phone", handset_extension="100")
    text = asterisk_config.render_pjsip(cfg)
    assert "protocol=tls" in text
    assert "media_encryption=sdes" in text
    assert "media_encryption_optimistic=no" in text, "optimistic SRTP is a silent plaintext downgrade"
    assert "direct_media=no" in text, "direct media would bypass the model entirely"
    assert "dtmf_mode=rfc4733" in text, "in-band DTMF would be transcribed as digits"
    assert "password=pw" in text
    assert "allow=ulaw,alaw" in text, "two allow= lines would replace rather than merge"
    # Comments mention the pattern deliberately; no *config line* may use it.
    lines = [line.strip() for line in text.splitlines()]
    assert not any(line.startswith("match=") for line in lines), "allowing 0.0.0.0/0 identifies the whole internet"
    assert not any(line.startswith("type=identify") for line in lines)


def test_asterisk_uses_the_same_certificates_the_sip_leg_generates():
    """One `phone sip init` must produce the credentials for both legs."""
    root = Path(__file__).resolve().parent.parent
    pjsua_template = (root / "sip" / "pjsua.conf.example").read_text()
    cert, key = asterisk_config.tls_files(VoiceConfig(tls_dir="~/.config/phone/tls"))
    assert cert.name in pjsua_template, f"{cert.name} is not what the SIP leg generates"
    assert key.name in pjsua_template
    assert "@HOME@/.config/phone/tls/" + cert.name in pjsua_template


def test_generated_dialplan_routes_the_bridge_and_refuses_everything_else(tmp_path):
    cfg = VoiceConfig(handset_password="pw", host="127.0.0.1", port=9092, owner_destination="+1 610 555 0100")
    text = asterisk_config.render_extensions(cfg)
    assert "AudioSocket(${AUDIOSOCKET_UUID},${BRIDGE_HOST},${BRIDGE_PORT})" in text
    assert "exten => 600,1,NoOp" in text
    assert "exten => _X.,1,Playback(ss-noservice)" in text, "unmatched extensions must be refused"
    assert "exten => 601" in text and "Echo()" in text, "the echo test must exist to isolate audio problems"


def test_the_owner_number_is_baked_in_literally_not_as_a_pattern(tmp_path):
    with_owner = asterisk_config.render_extensions(VoiceConfig(owner_destination="+1 (610) 555-0100"))
    assert "exten => +16105550100,1,NoOp(agent calls the configured owner only)" in with_owner
    # No wildcard destination anywhere: a pattern is how a model reaches a stranger.
    for line in with_owner.splitlines():
        if line.startswith("exten => ") and "@trunk-provider" in line:
            assert "_" not in line.split(",", 1)[0], "the PSTN context must not contain a pattern"


def test_without_an_owner_the_pstn_context_can_only_refuse():
    text = asterisk_config.render_extensions(VoiceConfig())
    assert "@trunk-provider" not in text
    assert "refusing outbound call to ${EXTEN}" in text


def test_rtp_window_matches_the_configuration(tmp_path):
    cfg = VoiceConfig(rtp_start=4100, rtp_end=4119)
    text = asterisk_config.render_rtp(cfg)
    assert "rtpstart=4100" in text and "rtpend=4119" in text
    assert "strictrtp=yes" in text


def test_provision_refuses_to_clobber_existing_files_without_force(tmp_path):
    cfg = VoiceConfig(handset_password="pw")
    target = tmp_path / "ast"
    asterisk_config.provision(cfg, str(target))
    with pytest.raises(FileExistsError, match="already exists"):
        asterisk_config.provision(cfg, str(target))
    asterisk_config.provision(cfg, str(target), force=True)
    backups = list(target.glob("*.bak.*"))
    assert len(backups) == 3, "a forced overwrite must leave the old files behind"


# ----------------------------------------------------------------------
# the Modelfile
# ----------------------------------------------------------------------
def test_modelfile_sets_num_ctx_because_ollama_defaults_to_2048():
    text = render_modelfile("gemma4:26b", num_ctx=8192)
    assert "FROM gemma4:26b" in text
    assert "PARAMETER num_ctx 8192" in text
    assert "PARAMETER num_predict 120" in text
    assert "SYSTEM \"\"\"" in text
    assert "Never output markdown" in text or "Never output markdown" in PHONE_SYSTEM_PROMPT


def test_the_system_prompt_forbids_dialling_and_emergency_numbers():
    lowered = PHONE_SYSTEM_PROMPT.lower()
    assert "911" in lowered and "never" in lowered
    assert "[[dial:owner" in PHONE_SYSTEM_PROMPT
    assert "phone number" in lowered


# ----------------------------------------------------------------------
# the command line
# ----------------------------------------------------------------------
def run(args, capsys) -> tuple[int, str]:
    code = cli.main(args)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_dial_test_passes_and_names_the_dangerous_cases(tmp_path, capsys):
    cfg = offline_config(tmp_path)
    code, out = run(["--config", str(cfg), "dial-test"], capsys)
    assert code == 0
    assert "911" in out and "emergency" in out
    assert "all dial-safety cases behaved correctly" in out
    assert "prompt-injection" in out


def test_dial_test_notices_a_broken_validator(tmp_path, capsys, monkeypatch):
    """The self-test must fail loudly if the safety rule is ever weakened."""
    cfg = offline_config(tmp_path)
    monkeypatch.setattr(cli, "validate_dial_request", lambda request, owner: (True, ""))
    code, out = run(["--config", str(cfg), "dial-test"], capsys)
    assert code == 1
    assert "behaved incorrectly" in out


def test_demo_runs_without_any_model_installed(tmp_path, capsys):
    code, out = run(["demo", "--workdir", str(tmp_path / "demo"), "--fast"], capsys)
    assert code == 0
    assert "turn 1:" in out
    assert (tmp_path / "demo" / "demo-caller.wav").is_file()
    assert (tmp_path / "demo" / "demo-reply.wav").is_file()
    assert "no ASR, no model and no TTS installed" in out


def test_simulate_fake_mode_exercises_the_plumbing(tmp_path, capsys):
    cfg = offline_config(tmp_path)
    code, out = run(["--config", str(cfg), "simulate", "--fake", "--fast", "--script", "hello there"], capsys)
    assert code == 0
    assert "turn 1:" in out
    assert "slowest turn" in out


def test_simulate_refuses_to_pretend_when_the_models_are_missing(tmp_path, capsys):
    cfg = offline_config(tmp_path).read_text().replace("backend = echo", "backend = whisper")
    path = write_config(tmp_path, cfg)
    code, out = run(["--config", str(path), "simulate"], capsys)
    assert code == 1
    assert "install those" in out


def test_provision_command_writes_the_config_and_prints_the_checklist(tmp_path, capsys):
    cfg = offline_config(tmp_path)
    target = tmp_path / "ast-out"
    code, out = run(["--config", str(cfg), "provision", "--dir", str(target)], capsys)
    assert code == 0
    assert (target / "pjsip.conf").is_file()
    assert "pjsip show endpoints" in out
    assert "sudo cp" in out
    assert "UDP 4000-4010" in out


def test_provision_without_write_permission_suggests_staging(tmp_path, capsys):
    cfg = offline_config(tmp_path)
    code, out = run(["--config", str(cfg), "provision", "--dir", "/proc/definitely-not-writable"], capsys)
    assert code == 1
    assert "hint:" in out


def test_modelfile_command_writes_a_file_and_prints_the_ollama_command(tmp_path, capsys):
    cfg = offline_config(tmp_path)
    output = tmp_path / "Modelfile"
    code, out = run(["--config", str(cfg), "modelfile", "--output", str(output)], capsys)
    assert code == 0
    assert "ollama create phone-voice" in out
    assert "PARAMETER num_ctx 8192" in output.read_text()


def test_call_dry_run_prints_exactly_the_asterisk_command(tmp_path, capsys):
    cfg = offline_config(tmp_path)
    code, out = run(["--config", str(cfg), "call", "--dry-run"], capsys)
    assert code == 0
    assert 'asterisk -rx "channel originate ' in out
    assert "AudioSocket" in out
    assert "PJSIP/+16105550100@trunk-provider" in out or "PJSIP/100" in out


def test_call_refuses_any_destination_but_the_owner(tmp_path, capsys):
    cfg = offline_config(tmp_path)
    code, out = run(["--config", str(cfg), "call", "--to", "+1 555 010 9999", "--dry-run"], capsys)
    assert code == 2
    assert "refusing to dial" in out


def test_call_with_no_owner_configured_refuses(tmp_path, capsys):
    body = OFFLINE_CONFIG.format(transcripts=tmp_path / "t", asterisk_dir=tmp_path / "a").replace(
        "owner = +1 610 555 0100", "owner ="
    )
    cfg = write_config(tmp_path, body)
    code, out = run(["--config", str(cfg), "call", "--to", "owner", "--dry-run"], capsys)
    assert code == 2
    assert "no owner destination configured" in out


def test_call_uses_the_handset_extension_over_sip_when_the_owner_is_an_extension(tmp_path, capsys):
    body = OFFLINE_CONFIG.format(transcripts=tmp_path / "t", asterisk_dir=tmp_path / "a").replace(
        "owner = +1 610 555 0100", "owner = 100"
    )
    cfg = write_config(tmp_path, body)
    code, out = run(["--config", str(cfg), "call", "--dry-run"], capsys)
    assert code == 0
    assert "PJSIP/100 " in out
    assert "@trunk-provider" not in out, "an extension on the LAN must not need the PSTN"


def test_doctor_reports_the_offline_stack_and_what_it_cannot_check(tmp_path, capsys):
    cfg = offline_config(tmp_path)
    code, out = run(["--config", str(cfg), "doctor"], capsys)
    assert code == 0, out
    assert "what this host cannot verify" in out
    assert "AudioSocket listens on 127.0.0.1" in out
    assert "dial safety" in out
    assert "no model is contacted" in out


def test_doctor_fails_on_a_broken_config(tmp_path, capsys):
    cfg = write_config(tmp_path, "[bridge]\nhost = 0.0.0.0\n")
    code, out = run(["--config", str(cfg), "doctor"], capsys)
    assert code == 1
    assert "refusing to listen on 0.0.0.0" in out
    assert "allow_public_bind" in out


def test_ask_streams_a_reply_from_a_fake_ollama(tmp_path, capsys, fake_ollama_url):
    cfg = write_config(
        tmp_path,
        f"[llm]\nbackend = ollama\nmodel = gemma4:26b\nurl = {fake_ollama_url.url}\n[tts]\nbackend = wav\n",
    )
    code, out = run(["--config", str(cfg), "ask", "hello", "--timings"], capsys)
    assert code == 0
    assert "Hello from your machine." in out
    assert "first token:" in out


def test_ask_reports_a_dial_intent_and_refuses_it_when_it_is_not_the_owner(tmp_path, capsys, fake_ollama_url):
    """The model's dial marker is surfaced as a decision, and refused."""
    fake_ollama_url.set_deltas("Sure, calling now. [[dial:+1 555 010 9999|asked]]")
    cfg = write_config(
        tmp_path,
        f"[llm]\nbackend = ollama\nurl = {fake_ollama_url.url}\n[dial]\nowner = +1 610 555 0100\n",
    )
    code, out = run(["--config", str(cfg), "ask", "hello"], capsys)
    assert code == 0
    assert "dial intent:" in out
    assert "refused" in out
    assert "+1 555 010 9999" in out, "the operator must see which number was asked for"


def test_transcript_reads_back_a_call(tmp_path, capsys):
    directory = tmp_path / "transcripts"
    directory.mkdir()
    (directory / "20260101-120000-abc.jsonl").write_text(
        "\n".join(
            json.dumps(entry)
            for entry in (
                {"role": "assistant", "text": "Hello.", "at": 1767225600},
                {"role": "user", "text": "Are you there?", "at": 1767225605},
                {"role": "assistant", "text": "I am.", "at": 1767225606},
            )
        )
    )
    code, out = run(["transcript", "--dir", str(directory)], capsys)
    assert code == 0
    assert "you  : Are you there?" in out
    assert "agent: I am." in out

    code, out = run(["transcript", "--dir", str(directory), "--list"], capsys)
    assert code == 0
    assert "3 line(s)" in out


# ----------------------------------------------------------------------
# serve: the whole bridge, once, against a fake Asterisk
# ----------------------------------------------------------------------
def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_serve_answers_one_call_and_writes_a_transcript(tmp_path, capsys):
    port = free_port()
    transcripts = tmp_path / "transcripts"
    body = OFFLINE_CONFIG.format(transcripts=transcripts, asterisk_dir=tmp_path / "a").replace(
        "[bridge]\nport = 0", f"[bridge]\nport = {port}"
    )
    cfg_path = write_config(tmp_path, body)
    cfg = VoiceConfig.load(str(cfg_path), env=False)

    # Serve one call. The session is EchoASR + ScriptedLLM + silence, so the
    # assertions are about the plumbing: UUID, audio in, audio out, transcript.
    result: list[int] = []
    args = argparse.Namespace(once=True, model="", no_dial=True)
    thread = threading.Thread(target=lambda: result.append(cli.serve_forever(cfg, args)), daemon=True)
    thread.start()

    client = None
    for _ in range(100):
        try:
            client = socket.create_connection(("127.0.0.1", port), timeout=2)
            break
        except OSError:
            time.sleep(0.05)
    assert client is not None, "the bridge never started listening"

    from voice.audio import tone

    call_uuid = uuid.uuid4()
    try:
        from voice.audiosocket import MSG_AUDIO_8000, MSG_HANGUP, MSG_UUID, encode

        client.sendall(encode(MSG_UUID, call_uuid.bytes))
        speech = tone(8000, 300, 20, amplitude=9000)
        for _ in range(8):
            client.sendall(encode(MSG_AUDIO_8000, speech))
        quiet = b"\x00" * 320
        for _ in range(14):
            client.sendall(encode(MSG_AUDIO_8000, quiet))
        client.settimeout(5)
        header = client.recv(3)
        assert header[:1] == bytes([MSG_AUDIO_8000]), "the agent must speak back"
        client.sendall(encode(MSG_HANGUP))
    finally:
        if client is not None:
            client.close()
    thread.join(timeout=10)

    written = list(transcripts.glob("*.jsonl"))
    assert written, "every call must leave a transcript"
    entries = [json.loads(line) for line in written[0].read_text().splitlines()]
    assert entries[0]["role"] == "assistant" and entries[0]["text"] == "Hi from the test."
    assert str(call_uuid)[:8] in written[0].name

