"""The spool format is the contract. These tests pin it down."""

from __future__ import annotations

import os
import stat
from datetime import timezone

import pytest

from sms.spool import (
    CorruptMessageError,
    Message,
    Spool,
    SpoolError,
    SpoolFullError,
    digest,
    parse_iso,
    slugify,
)


def msg(body="hello", sender="+15550109999", when="2026-10-05T12:00:00+00:00", **kw) -> Message:
    return Message(sender=sender, body=body, received_at=when, **kw)


# ---- slugify: the only place untrusted text can influence a path --------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+1 (555) 010-9999", "1-555-010-9999"),
        ("../../etc/passwd", "etc-passwd"),
        ("../../", "unknown"),
        ("", "unknown"),
        ("...", "unknown"),
        ("/etc/shadow", "etc-shadow"),
        ("a" * 100, "a" * 24),
        ("+1555\n0109999", "1555-0109999"),
        ("\u260e\u260e", "unknown"),
    ],
)
def test_slugify_is_safe_and_bounded(raw: str, expected: str) -> None:
    assert slugify(raw) == expected


def test_sender_cannot_escape_the_spool_directory(spool: Spool) -> None:
    path = spool.write(msg(sender="../../../../tmp/owned", body="x"))
    assert path.parent == spool.dir
    assert ".." not in path.name
    assert not (spool.dir.parent.parent.parent / "tmp" / "owned").exists()


# ---- format ------------------------------------------------------------
def test_round_trip_preserves_body_exactly() -> None:
    body = "line one\nline two\n\nBODY: not a marker\nFROM: not a header\n"
    original = msg(body=body)
    assert Message.parse(original.render()).body == body


def test_round_trip_preserves_all_fields() -> None:
    original = msg(body="hi", source="poll", provider_id="abc123", transport="sms")
    copy = Message.parse(original.render())
    assert (copy.sender, copy.body, copy.received_at, copy.source, copy.provider_id) == (
        original.sender,
        original.body,
        original.received_at,
        original.source,
        original.provider_id,
    )


def test_render_is_human_readable_header_block() -> None:
    text = msg(body="hello").render()
    assert text.startswith("PHONE-SPOOL: 1\n")
    assert "\nBODY:\nhello\n" in text
    assert text.split("BODY:", 1)[0].count("FROM:") == 1


def test_parse_rejects_garbage() -> None:
    for bad in ("", "no headers at all", "FROM: x\n(no body marker)", "FROM: x\n\nBODY:\nhi"):
        with pytest.raises(CorruptMessageError):
            Message.parse(bad)


def test_parse_rejects_missing_required_headers() -> None:
    with pytest.raises(CorruptMessageError):
        Message.parse("ID: abc\nBODY:\nhi\n")


def test_digest_changes_with_content() -> None:
    assert digest("a", "x") != digest("b", "x")
    assert digest("a", "x") != digest("a", "y")
    assert digest("a", "x") == digest("a", "x")


def test_verify_detects_tampering(spool: Spool) -> None:
    path = spool.write(msg(body="original text"))
    stored = Message.load(path)
    assert stored.verify(path.read_text())

    tampered = path.read_text().replace("original text", "edited text")
    assert not Message.parse(tampered).verify(tampered)


# ---- filesystem behaviour ---------------------------------------------
def test_write_is_atomic_and_leaves_no_temp_files(spool: Spool) -> None:
    for i in range(5):
        spool.write(msg(body=f"message {i}"))
    leftovers = [p.name for p in spool.dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_files_and_directory_are_owner_only(spool: Spool) -> None:
    path = spool.write(msg(body="private"))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(spool.dir).st_mode) == 0o700


def test_same_second_same_sender_does_not_collide(spool: Spool) -> None:
    first = spool.write(msg(body="one", when="2026-10-05T12:00:00+00:00"))
    second = spool.write(msg(body="two", when="2026-10-05T12:00:00+00:00"))
    assert first != second
    assert len(spool.paths()) == 2


def test_filename_sorts_chronologically(spool: Spool) -> None:
    spool.write(msg(body="later", when="2026-10-05T12:00:05+00:00"))
    spool.write(msg(body="earlier", when="2026-10-05T12:00:01+00:00"))
    bodies = [m.body for m in spool.messages()]
    assert bodies == ["earlier", "later"]


def test_empty_body_is_refused(spool: Spool) -> None:
    with pytest.raises(SpoolError):
        spool.write(msg(body="   \n  "))


def test_capacity_cap_fails_closed(spool_dir) -> None:
    small = Spool(spool_dir, max_files=2).ensure()
    small.write(msg(body="1"))
    small.write(msg(body="2"))
    with pytest.raises(SpoolFullError):
        small.write(msg(body="3"))


def test_journal_records_metadata_but_never_the_body(spool: Spool) -> None:
    spool.write(msg(body="SECRET-CANARY-TEXT"))
    journal = spool.journal_path.read_text()
    assert "SECRET-CANARY-TEXT" not in journal
    assert "+15550109999" in journal
    fields = journal.strip().split("\t")
    assert len(fields) == 7
    assert fields[2] == "+15550109999"


def test_journal_escapes_control_characters_in_sender(spool: Spool) -> None:
    spool.write(msg(sender="evil\nFROM: forged", body="x"))
    journal = spool.journal_path.read_text()
    assert journal.count("\n") == 1  # one record, not two


def test_journal_can_be_disabled(spool_dir) -> None:
    quiet = Spool(spool_dir, journal=False).ensure()
    quiet.write(msg(body="x"))
    assert not quiet.journal_path.exists()


def test_by_id_accepts_prefix_and_latest(spool: Spool) -> None:
    spool.write(msg(body="first"))
    last_path = spool.write(msg(body="second"))
    last = Message.load(last_path)

    assert spool.by_id("latest")[0].body == "second"  # type: ignore[index]
    assert spool.by_id(last.id)[0].body == "second"  # type: ignore[index]
    assert spool.by_id(last_path.name)[0].body == "second"  # type: ignore[index]
    assert spool.by_id(last.id[:6])[0].body == "second"  # type: ignore[index]
    assert spool.by_id("nope-not-here") is None


def test_stats_counts_and_skips_corrupt(spool: Spool) -> None:
    spool.write(msg(body="1", when="2026-10-05T10:00:00+00:00"))
    spool.write(msg(body="2", when="2026-10-05T11:00:00+00:00"))
    (spool.dir / "20261005-120000_junk_aaaaaaaaaaaa.txt").write_text("garbage")

    stats = spool.stats()
    assert stats.count == 2
    assert stats.corrupt == 1
    assert stats.oldest.hour == 10 and stats.newest.hour == 11  # type: ignore[union-attr]


def test_verify_reports_problems(spool: Spool) -> None:
    ok_path = spool.write(msg(body="clean"))
    (spool.dir / "20261005-130000_evil_bbbbbbbbbbbb.txt").write_text(
        msg(body="tampered").render().replace("SHA256: ", "SHA256: deadbeef", 1)
    )
    ok, problems = spool.verify()
    assert ok == 1
    assert len(problems) == 1
    assert "digest mismatch" in problems[0]
    assert Message.load(ok_path).verify()


# ---- timestamps --------------------------------------------------------
def test_parse_iso_handles_z_and_offsets_and_junk() -> None:
    assert parse_iso("2026-10-05T12:00:00Z").hour == 12
    assert parse_iso("2026-10-05T14:00:00+02:00").hour == 12
    assert parse_iso("2026-10-05T12:00:00").tzinfo == timezone.utc
    assert parse_iso("not a date").year == 1970


def test_message_id_is_stable_across_render_cycles() -> None:
    original = msg(body="stable")
    once = Message.parse(original.render())
    twice = Message.parse(once.render())
    assert original.id == once.id == twice.id


def test_arrival_time_is_stamped_by_the_writer(spool: Spool) -> None:
    first = Message.load(spool.write(msg(body="one", when="2026-10-05T12:00:00+00:00")))
    second = Message.load(spool.write(msg(body="two", when="2026-10-05T12:00:00+00:00")))
    assert first.received_at == second.received_at == "2026-10-05T12:00:00+00:00"
    assert first.spooled_at and second.spooled_at
    assert first.spooled_at != second.spooled_at       # sub-second resolution
    assert second.arrival > first.arrival              # arrival order survives
    assert "SPOOLED:" in first.render()


def test_arrival_stamp_does_not_affect_verification(spool: Spool) -> None:
    path = spool.write(msg(body="stable digest"))
    text = path.read_text()
    assert "SPOOLED:" in text
    assert Message.parse(text).verify(text)


def test_body_size_is_counted_in_bytes_not_characters() -> None:
    m = msg(body="\u00e9" * 10)  # 20 UTF-8 bytes, 10 chars
    assert m.body_bytes == 20
    assert "BYTES: 20" in m.render()
