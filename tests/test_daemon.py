"""Webhook receiver: the parsing contract and the failure modes that matter."""

from __future__ import annotations

import json

import pytest

from conftest import Client, make_config
from sms.config import Config, ConfigError
from sms.daemon import RateLimiter, Deduper, extract, first_present, normalise_time
from sms.spool import Spool


def bodies(spool_dir) -> list[str]:
    spool = Spool(spool_dir)
    return [m.body for m in spool.messages()]


# ---- happy paths -------------------------------------------------------
def test_json_webhook_writes_one_file(daemon: Client, spool_dir) -> None:
    status, _headers, payload = daemon.post_json(
        "/sms/incoming",
        {"from": "+15550109999", "text": "hello from a webhook", "timestamp": "2026-10-05T12:00:00Z"},
    )
    assert status == 201
    assert payload["status"] == "spooled"
    assert bodies(spool_dir) == ["hello from a webhook"]

    path = Spool(spool_dir).paths()[0]
    text = path.read_text()
    assert "FROM: +15550109999" in text
    assert "DATE: 2026-10-05T12:00:00+00:00" in text
    assert text.endswith("BODY:\nhello from a webhook\n")


def test_alternate_provider_field_names_are_mapped(daemon: Client, spool_dir) -> None:
    status, _, _ = daemon.post_json(
        "/sms/incoming", {"msisdn": "+447700900123", "msg": "different vocabulary"}
    )
    assert status == 201
    message = next(Spool(spool_dir).messages())
    assert message.sender == "+447700900123"
    assert message.body == "different vocabulary"


def test_form_encoded_and_plain_text_bodies(daemon: Client, spool_dir) -> None:
    status, _, _ = daemon.request(
        "POST",
        "/sms/incoming",
        "from=%2B15550108888&text=form+encoded",
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert status == 201
    status, _, _ = daemon.request(
        "POST", "/sms/incoming", "bare text body", {"Content-Type": "text/plain"}
    )
    assert status == 201
    assert bodies(spool_dir) == ["form encoded", "bare text body"]


def test_trailing_slash_and_query_string_are_tolerated(daemon: Client) -> None:
    status, _, _ = daemon.post_json("/sms/incoming/?source=carrier", {"text": "ok"})
    assert status == 201


def test_provider_id_is_recorded(daemon: Client, spool_dir) -> None:
    daemon.post_json("/sms/incoming", {"from": "a", "text": "x", "message_id": "SM1234"})
    assert next(Spool(spool_dir).messages()).provider_id == "SM1234"


def test_epoch_timestamps_are_converted(daemon: Client, spool_dir) -> None:
    from datetime import datetime, timezone

    daemon.post_json("/sms/incoming", {"from": "a", "text": "x", "timestamp": 1791000000})
    expected = datetime.fromtimestamp(1791000000, tz=timezone.utc).isoformat(timespec="seconds")
    assert next(Spool(spool_dir).messages()).received_at == expected


def test_missing_sender_defaults_to_unknown(daemon: Client, spool_dir) -> None:
    daemon.post_json("/sms/incoming", {"text": "no sender present"})
    assert next(Spool(spool_dir).messages()).sender == "unknown"


# ---- rejection paths ---------------------------------------------------
@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"from": "+1", "text": ""}, 400),
        ({"from": "+1", "text": "   "}, 400),
        ({"from": "+1"}, 400),
        ({}, 400),
    ],
)
def test_payloads_without_a_body_are_rejected(daemon: Client, payload, expected) -> None:
    status, _, _ = daemon.post_json("/sms/incoming", payload)
    assert status == expected


def test_invalid_json_is_rejected(daemon: Client) -> None:
    status, _, _ = daemon.request(
        "POST", "/sms/incoming", "{not json", {"Content-Type": "application/json"}
    )
    assert status == 400


def test_unsupported_content_type_is_rejected(daemon: Client) -> None:
    status, _, _ = daemon.request(
        "POST", "/sms/incoming", b"\x00\x01", {"Content-Type": "application/octet-stream"}
    )
    assert status == 415


def test_oversized_body_is_rejected(daemon_factory) -> None:
    client, _cfg, _ = daemon_factory(max_body_bytes=128)
    status, _, _ = client.request(
        "POST",
        "/sms/incoming",
        json.dumps({"from": "+1", "text": "x" * 500}),
        {"Content-Type": "application/json"},
    )
    assert status == 413


def test_chunked_body_is_rejected(daemon: Client) -> None:
    status, _, _ = daemon.request(
        "POST",
        "/sms/incoming",
        b"5\r\nhello\r\n0\r\n\r\n",
        {"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
    )
    assert status == 411


def test_unknown_endpoint_is_404(daemon: Client) -> None:
    assert daemon.request("POST", "/../etc/passwd", "x")[0] == 404
    assert daemon.request("GET", "/admin")[0] == 404


def test_wrong_methods_are_rejected_with_allow_header(daemon: Client) -> None:
    status, headers, _ = daemon.request("PUT", "/sms/incoming", "x")
    assert status == 405
    assert "POST" in headers.get("allow", "")


def test_head_sends_no_body(daemon: Client) -> None:
    status, headers, payload = daemon.request("HEAD", "/healthz", raw=True)
    assert status == 405
    assert payload == b""
    assert headers["content-length"] == "0"


def test_rate_limit_kicks_in(daemon_factory) -> None:
    client, _cfg, _ = daemon_factory(max_requests_per_minute=60, max_burst=3)
    codes = [
        client.post_json("/sms/incoming", {"from": "+1", "text": f"msg {i}"})[0] for i in range(5)
    ]
    assert codes[:3] == [201, 201, 201]
    assert codes[3:] == [429, 429]


def test_duplicate_webhook_replay_is_suppressed(daemon: Client, spool_dir) -> None:
    payload = {"from": "+1", "text": "retried delivery", "message_id": "SM999"}
    first = daemon.post_json("/sms/incoming", payload)
    second = daemon.post_json("/sms/incoming", payload)
    assert first[0] == 201
    assert second[0] == 200
    assert second[2]["status"] == "duplicate"
    assert bodies(spool_dir) == ["retried delivery"]


def test_dedupe_is_content_based_when_provider_gives_no_id(daemon: Client, spool_dir) -> None:
    payload = {"from": "+1", "text": "same body"}
    daemon.post_json("/sms/incoming", payload)
    daemon.post_json("/sms/incoming", payload)
    assert len(bodies(spool_dir)) == 1


def test_different_bodies_are_not_deduped(daemon: Client, spool_dir) -> None:
    daemon.post_json("/sms/incoming", {"from": "+1", "text": "one"})
    daemon.post_json("/sms/incoming", {"from": "+1", "text": "two"})
    assert len(bodies(spool_dir)) == 2


# ---- auth --------------------------------------------------------------
def test_auth_required_when_token_configured(auth_daemon: Client, spool_dir) -> None:
    assert auth_daemon.post_json("/sms/incoming", {"text": "x"})[0] == 401
    assert auth_daemon.post_json("/sms/incoming", {"text": "x"}, {"X-Phone-Token": "wrong"})[0] == 401
    assert bodies(spool_dir) == []


def test_auth_accepts_header_and_bearer(auth_daemon: Client, spool_dir) -> None:
    assert auth_daemon.post_json("/sms/incoming", {"text": "a"}, {"X-Phone-Token": "s3cret-token"})[0] == 201
    assert auth_daemon.post_json("/sms/incoming", {"text": "b"}, {"Authorization": "Bearer s3cret-token"})[0] == 201
    assert auth_daemon.post_json("/sms/incoming", {"text": "c"}, {"Authorization": "bearer s3cret-token"})[0] == 201
    assert len(bodies(spool_dir)) == 3


def test_recent_listing_requires_auth(auth_daemon: Client) -> None:
    assert auth_daemon.request("GET", "/sms/recent")[0] == 401


def test_auth_failure_is_not_logged_with_the_attempted_token(auth_daemon: Client) -> None:
    status, _, _ = auth_daemon.post_json("/sms/incoming", {"text": "x"}, {"X-Phone-Token": "guess-me"})
    assert status == 401


# ---- read endpoints ----------------------------------------------------
def test_healthz_reports_spool_state(daemon: Client, spool_dir) -> None:
    daemon.post_json("/sms/incoming", {"from": "+1", "text": "count me"})
    status, headers, payload = daemon.request("GET", "/healthz")
    assert status == 200
    assert payload["status"] == "ok"
    assert payload["spool"]["files"] == 1
    assert payload["spool"]["bytes"] > 0
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"


def test_server_banner_does_not_leak_the_interpreter(daemon: Client) -> None:
    _status, headers, _ = daemon.request("GET", "/healthz")
    assert "python" not in headers.get("server", "").lower()


def test_recent_returns_newest_first(daemon: Client) -> None:
    daemon.post_json("/sms/incoming", {"from": "+1", "text": "older"})
    daemon.post_json("/sms/incoming", {"from": "+1", "text": "newer"})
    status, _, payload = daemon.request("GET", "/sms/recent?limit=5")
    assert status == 200
    assert [m["body"] for m in payload["messages"]] == ["newer", "older"]


def test_recent_keeps_arrival_order_within_the_same_second(daemon: Client) -> None:
    """Providers stamp at second resolution; a feed must not reorder itself."""
    stamp = "2026-10-05T12:00:00Z"
    for text in ("first", "second", "third"):
        daemon.post_json("/sms/incoming", {"from": "+1", "text": text, "timestamp": stamp})
    status, _, payload = daemon.request("GET", "/sms/recent?limit=10")
    assert status == 200
    assert [m["body"] for m in payload["messages"]] == ["third", "second", "first"]


def test_recent_can_be_disabled(daemon_factory) -> None:
    client, _cfg, _ = daemon_factory(expose_recent=False)
    assert client.request("GET", "/sms/recent")[0] == 403


# ---- config safety -----------------------------------------------------
def test_public_bind_is_refused_without_explicit_opt_in() -> None:
    cfg = Config()
    cfg.host = "0.0.0.0"
    with pytest.raises(ConfigError, match="allow_public_bind"):
        cfg.validate()


def test_public_bind_requires_token_and_tls(tmp_path) -> None:
    cfg = Config()
    cfg.host = "0.0.0.0"
    cfg.allow_public_bind = True
    with pytest.raises(ConfigError, match="auth_token"):
        cfg.validate()
    cfg.token = "t"
    with pytest.raises(ConfigError, match="tls_cert"):
        cfg.validate()


def test_loopback_bind_needs_nothing_extra() -> None:
    Config().validate()


def test_check_config_redacts_the_token(tmp_path) -> None:
    cfg = make_config(tmp_path, token="top-secret")
    rendered = cfg.render()
    assert "top-secret" not in rendered
    assert "sha256:" in rendered


# ---- unit-level helpers ------------------------------------------------
def test_first_present_is_case_insensitive_and_skips_blanks() -> None:
    assert first_present({"From": "x"}, ["from"]) == "x"
    assert first_present({"from": ""}, ["from"]) is None
    assert first_present({"from": []}, ["from", "other"]) is None
    assert first_present({}, ["missing"]) is None


def test_normalise_time_accepts_common_shapes() -> None:
    from datetime import datetime, timezone

    expected = datetime.fromtimestamp(1791000000, tz=timezone.utc).isoformat(timespec="seconds")
    assert normalise_time("2026-10-05T12:00:00Z") == "2026-10-05T12:00:00+00:00"
    assert normalise_time(1791000000) == expected          # epoch seconds
    assert normalise_time(1791000000000) == expected       # epoch milliseconds
    assert normalise_time("1791000000") == expected        # epoch as string


def test_normalise_time_falls_back_to_now_on_junk() -> None:
    assert normalise_time("not-a-time").startswith("1970")


def test_extract_accepts_a_bare_list_and_a_bare_string() -> None:
    cfg = Config()
    assert extract([{"text": "wrapped"}], cfg).body == "wrapped"
    assert extract("plain string", cfg).body == "plain string"


def test_extract_rejects_structured_bodies() -> None:
    from sms.spool import SpoolError

    with pytest.raises(SpoolError):
        extract({"text": {"nested": "object"}}, Config())


def test_extract_strips_nul_bytes() -> None:
    assert extract({"text": "a\x00b"}, Config()).body == "ab"


def test_rate_limiter_refills_over_time() -> None:
    limiter = RateLimiter(per_minute=60, burst=2)
    assert limiter.allow("a") and limiter.allow("a")
    assert not limiter.allow("a")
    assert limiter.allow("b")  # per client


def test_deduper_expires_entries() -> None:
    deduper = Deduper(window=0.05, capacity=2)
    assert not deduper.seen("k")
    assert deduper.seen("k")
    import time

    time.sleep(0.1)
    assert not deduper.seen("k")
