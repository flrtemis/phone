"""Reader commands, driven through main() exactly as a user would."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sms import cli
from sms.spool import Message, Spool


def msg(body: str, sender: str = "+15550109999", when: str = "2026-10-05T12:00:00+00:00") -> Message:
    return Message(sender=sender, body=body, received_at=when)


@pytest.fixture()
def workspace(tmp_path: Path) -> tuple[Spool, list[str], Path]:
    """A spool with three messages plus the argv prefix that points at it."""
    spool = Spool(tmp_path / "incoming").ensure()
    spool.write(msg("first message about invoices", "+15550000001", "2026-10-01T09:00:00+00:00"))
    spool.write(msg("second message about lunch", "+15550000002", "2026-10-02T10:00:00+00:00"))
    spool.write(msg("third message about invoices again", "+15550000003", "2026-10-03T11:00:00+00:00"))

    conf = tmp_path / "sms.conf"
    conf.write_text(f"[spool]\ndir = {spool.dir}\n")
    return spool, ["--config", str(conf)], tmp_path


def run(argv: list[str]) -> int:
    return cli.main(argv)


# ---- list --------------------------------------------------------------
def test_list_shows_all_messages_oldest_first(workspace, capsys) -> None:
    _spool, base, _ = workspace
    assert run(base + ["list", "--limit", "10"]) == 0
    out = capsys.readouterr().out
    assert out.index("first message") < out.index("third message")
    assert "+15550000002" in out


def test_list_limit_keeps_the_newest(workspace, capsys) -> None:
    _spool, base, _ = workspace
    run(base + ["list", "--limit", "1"])
    out = capsys.readouterr().out
    assert "third message" in out
    assert "first message" not in out


def test_list_limit_zero_shows_nothing(workspace, capsys) -> None:
    _spool, base, _ = workspace
    run(base + ["list", "--limit", "0"])
    assert capsys.readouterr().out.strip() == ""


def test_list_json_is_machine_readable(workspace, capsys) -> None:
    _spool, base, _ = workspace
    run(base + ["list", "--json"])
    rows = [json.loads(line) for line in capsys.readouterr().out.strip().split("\n")]
    assert len(rows) == 3
    assert rows[0]["from"] == "+15550000001"
    assert {"id", "from", "date", "file", "body"} <= set(rows[0])


def test_list_since_filter(workspace, capsys) -> None:
    _spool, base, _ = workspace
    run(base + ["list", "--json", "--since", "2026-10-02T00:00:00+00:00"])
    rows = [json.loads(line) for line in capsys.readouterr().out.strip().split("\n")]
    assert [r["body"] for r in rows] == ["second message about lunch", "third message about invoices again"]


def test_list_sender_filter(workspace, capsys) -> None:
    _spool, base, _ = workspace
    run(base + ["list", "--json", "--from", "0002"])
    rows = [json.loads(line) for line in capsys.readouterr().out.strip().split("\n")]
    assert len(rows) == 1


def test_list_on_empty_spool_is_not_an_error(workspace, tmp_path, capsys) -> None:
    conf = tmp_path / "empty.conf"
    conf.write_text(f"[spool]\ndir = {tmp_path / 'nothing'}\n")
    assert run(["--config", str(conf), "list"]) == 0
    assert "no messages" in capsys.readouterr().err


# ---- read --------------------------------------------------------------
def test_read_latest_prints_body(workspace, capsys) -> None:
    _spool, base, _ = workspace
    assert run(base + ["read", "latest"]) == 0
    out = capsys.readouterr().out
    assert "third message about invoices again" in out
    assert "+15550000003" in out


def test_read_by_id_prefix(workspace, capsys) -> None:
    spool, base, _ = workspace
    target = Message.load(spool.paths()[0])
    assert run(base + ["read", target.id]) == 0
    assert "first message" in capsys.readouterr().out


def test_read_raw_is_byte_identical_to_the_file(workspace, capsys) -> None:
    spool, base, _ = workspace
    run(base + ["read", "latest", "--raw"])
    out = capsys.readouterr().out
    assert out == spool.paths()[-1].read_text()


def test_read_unknown_id_fails(workspace, capsys) -> None:
    _spool, base, _ = workspace
    assert run(base + ["read", "deadbeefdead"]) == 1
    assert "no message" in capsys.readouterr().err


# ---- grep --------------------------------------------------------------
def test_grep_finds_matches(workspace, capsys) -> None:
    _spool, base, _ = workspace
    assert run(base + ["grep", "invoices"]) == 0
    out = capsys.readouterr().out
    assert "first message" in out and "third message" in out
    assert "second message" not in out


def test_grep_no_match_exits_nonzero(workspace, capsys) -> None:
    _spool, base, _ = workspace
    assert run(base + ["grep", "nothing-matches-this"]) == 1
    assert "0 match" in capsys.readouterr().err


def test_grep_is_case_sensitive_unless_told_otherwise(workspace) -> None:
    _spool, base, _ = workspace
    assert run(base + ["grep", "LUNCH"]) == 1
    assert run(base + ["grep", "-i", "LUNCH"]) == 0


def test_grep_bad_pattern_exits_two(workspace, capsys) -> None:
    _spool, base, _ = workspace
    assert run(base + ["grep", "([unclosed"]) == 2
    assert "bad pattern" in capsys.readouterr().err


def test_grep_can_be_scoped_to_the_sender(workspace, capsys) -> None:
    _spool, base, _ = workspace
    assert run(base + ["grep", "0002", "--field", "from"]) == 0
    assert run(base + ["grep", "0002", "--field", "body"]) == 1


# ---- stats / verify ----------------------------------------------------
def test_stats_counts_and_range(workspace, capsys) -> None:
    _spool, base, _ = workspace
    assert run(base + ["stats", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 3
    assert payload["bytes"] > 0
    assert payload["oldest"].startswith("2026-10-01")
    assert payload["newest"].startswith("2026-10-03")
    assert payload["corrupt"] == 0


def test_verify_passes_on_a_healthy_spool(workspace, capsys) -> None:
    _spool, base, _ = workspace
    assert run(base + ["verify"]) == 0
    assert "3 message(s) verified" in capsys.readouterr().out


def test_verify_fails_after_tampering(workspace, capsys) -> None:
    spool, base, _ = workspace
    victim = spool.paths()[0]
    victim.write_text(victim.read_text().replace("invoices", "sausages"))
    assert run(base + ["verify", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] == 2
    assert "digest mismatch" in payload["problems"][0]


# ---- export ------------------------------------------------------------
def test_export_jsonl_to_stdout(workspace, capsys) -> None:
    _spool, base, _ = workspace
    assert run(base + ["export", "--format", "jsonl"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.strip().split("\n")]
    assert len(rows) == 3
    assert {"id", "from", "date", "file", "sha256", "body"} <= set(rows[0])


def test_export_to_file(workspace, tmp_path, capsys) -> None:
    _spool, base, _ = workspace
    target = tmp_path / "export.jsonl"
    assert run(base + ["export", "--format", "jsonl", "--out", str(target)]) == 0
    assert len(target.read_text().strip().split("\n")) == 3
    assert "wrote 3 message(s)" in capsys.readouterr().err


def test_export_txt_and_raw(workspace, capsys) -> None:
    _spool, base, _ = workspace
    run(base + ["export", "--format", "txt"])
    assert "first message about invoices" in capsys.readouterr().out
    run(base + ["export", "--format", "raw"])
    assert "PHONE-SPOOL: 1" in capsys.readouterr().out


# ---- purge -------------------------------------------------------------
def test_purge_refuses_without_yes(workspace, capsys) -> None:
    spool, base, _ = workspace
    assert run(base + ["purge", "--older-than", "0"]) == 1
    assert "refusing" in capsys.readouterr().err
    assert len(spool.paths()) == 3


def test_purge_deletes_with_yes(workspace, capsys) -> None:
    spool, base, _ = workspace
    assert run(base + ["purge", "--older-than", "0", "--yes"]) == 0
    assert spool.paths() == []
    assert "deleted 3 file(s)" in capsys.readouterr().out


def test_purge_keeps_recent_messages(workspace, capsys) -> None:
    spool, base, _ = workspace
    assert run(base + ["purge", "--older-than", "3650", "--yes"]) == 0
    assert len(spool.paths()) == 3


# ---- tail --------------------------------------------------------------
def test_tail_last_n(workspace, capsys) -> None:
    _spool, base, _ = workspace
    assert run(base + ["tail", "--last", "2"]) == 0
    out = capsys.readouterr().out
    assert "second message" in out and "third message" in out
    assert "first message" not in out


def test_tail_follow_sees_files_that_arrive_after_startup(workspace, capsys, monkeypatch) -> None:
    """The whole point: a glob expanded at start-up would never see this file."""
    spool, base, _ = workspace
    ticks = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] == 1:
            spool.write(msg("delivered while tailing", "+15550000009", "2026-10-04T12:00:00+00:00"))
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    assert run(base + ["tail", "--follow", "--last", "0"]) == 0
    out = capsys.readouterr().out
    assert "delivered while tailing" in out
    assert "first message" not in out


def test_tail_follow_shows_existing_messages_when_asked(workspace, capsys) -> None:
    _spool, base, _ = workspace
    assert run(base + ["tail", "--last", "2"]) == 0
    out = capsys.readouterr().out
    assert "second message" in out and "third message" in out
    assert "first message" not in out


def test_tail_from_start_prints_everything(workspace, capsys) -> None:
    _spool, base, _ = workspace
    assert run(base + ["tail", "--from-start"]) == 0
    out = capsys.readouterr().out
    assert out.count(" message about ") == 3


def test_tail_follow_ignores_unparseable_files(workspace, capsys, monkeypatch) -> None:
    spool, base, _ = workspace
    ticks = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] == 1:
            (spool.dir / "20261004-120000_junk_cccccccccccc.txt").write_text("not a message")
        elif ticks["n"] == 2:
            spool.write(msg("real one", "+15550000010", "2026-10-04T13:00:00+00:00"))
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    assert run(base + ["tail", "--follow", "--last", "0"]) == 0
    assert "real one" in capsys.readouterr().out


# ---- test (self-test of the whole local pipeline) -----------------------
def test_self_test_passes_on_a_healthy_install(workspace, capsys) -> None:
    _spool, base, _ = workspace
    assert run(base + ["test"]) == 0
    out = capsys.readouterr().out
    assert "webhook accepted" in out
    assert "body survived intact" in out
    assert "local pipeline works" in out


def test_self_test_leaves_no_message_behind(workspace) -> None:
    spool, base, _ = workspace
    before = {p.name for p in spool.paths()}
    run(base + ["test"])
    assert {p.name for p in spool.paths()} == before


def test_self_test_prints_the_webhook_url_and_warns_about_loopback(workspace, capsys) -> None:
    _spool, base, _ = workspace
    run(base + ["test"])
    out = capsys.readouterr().out
    assert "/sms/incoming" in out
    assert "loopback" in out          # the provider cannot reach 127.0.0.1
    assert "ssh -N -R" in out         # ...so it points at the tunnel recipe


def test_self_test_warns_when_no_token_is_configured(workspace, capsys) -> None:
    _spool, base, _ = workspace
    run(base + ["test"])
    assert "shared secret       : NONE" in capsys.readouterr().out


def test_self_test_works_on_an_empty_spool(workspace, tmp_path, capsys) -> None:
    conf = tmp_path / "fresh.conf"
    conf.write_text(f"[spool]\ndir = {tmp_path / 'brand-new'}\n")
    assert run(["--config", str(conf), "test"]) == 0
    out = capsys.readouterr().out
    assert "existing files  : 0" in out


def test_self_test_reports_a_broken_spool_clearly(workspace, tmp_path, capsys) -> None:
    """A spool path that cannot be used must produce a sentence, not a traceback.

    Note: an unwritable *directory* is deliberately repaired by ensure() (it
    re-chmods to 0700), so the honest way to break this is to point the spool
    at something that is not a directory at all.
    """
    conf = tmp_path / "broken.conf"
    not_a_dir = tmp_path / "i-am-a-file"
    not_a_dir.write_text("this is not a spool directory\n")
    conf.write_text(f"[spool]\ndir = {not_a_dir}\n")

    assert run(["--config", str(conf), "test"]) == 2
    captured = capsys.readouterr()
    assert "cannot use the spool" in captured.err
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


# ---- config ------------------------------------------------------------
def test_config_prints_effective_settings(workspace, capsys) -> None:
    _spool, base, _ = workspace
    assert run(base + ["config"]) == 0
    out = capsys.readouterr().out
    assert "spool dir" in out
    assert "listen" in out


def test_missing_config_file_is_reported(workspace, capsys) -> None:
    _spool, _base, tmp_path = workspace
    assert run(["--config", str(tmp_path / "nope.conf"), "list"]) == 2
    assert "configuration error" in capsys.readouterr().err
