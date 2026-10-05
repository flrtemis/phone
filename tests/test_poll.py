"""Poller: idempotence, cursors, and the failure behaviour that keeps
messages from being silently dropped."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sms.config import Config, ConfigError
from sms.poll import (
    PollState,
    dedupe_key,
    messages_from_payload,
    poll_once,
)
from sms.spool import Message, Spool, SpoolError


class StubProvider:
    """A fake gateway. Serves queued payloads and records what was asked."""

    def __init__(self, payloads: list) -> None:
        self.payloads = list(payloads)
        self.requests: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:  # silence
                pass

            def do_GET(self) -> None:  # noqa: N802
                stub.requests.append(
                    {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}}
                )
                if not stub.payloads:
                    body = b"[]"
                else:
                    item = stub.payloads.pop(0) if len(stub.payloads) > 1 else stub.payloads[0]
                    body = item if isinstance(item, bytes) else json.dumps(item).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/messages"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture()
def provider():
    stub = StubProvider([{"messages": []}])
    yield stub
    stub.close()


def cfg_for(provider_url: str, spool_dir: Path, tmp_path: Path, **over) -> Config:
    cfg = Config()
    cfg.poll_url = provider_url
    cfg.spool_dir = str(spool_dir)
    cfg.state_file = str(tmp_path / "poll.state.json")
    cfg.poll_json_path = "messages"
    for key, value in over.items():
        setattr(cfg, key, value)
    return cfg


def payload(*bodies, sender="+15550101111") -> dict:
    return {
        "messages": [
            {"id": f"prov-{i}", "from": sender, "text": body, "timestamp": "2026-10-05T12:00:00Z"}
            for i, body in enumerate(bodies)
        ]
    }


# ---- happy path --------------------------------------------------------
def test_poll_spools_messages_and_advances_cursor(provider, spool_dir, tmp_path) -> None:
    provider.payloads = [payload("first", "second")]
    cfg = cfg_for(provider.url, spool_dir, tmp_path)
    spool = Spool(spool_dir).ensure()
    state = PollState.load(cfg.state_file)

    assert poll_once(cfg, spool, state) == 2
    assert [m.body for m in spool.messages()] == ["first", "second"]
    assert state.since  # cursor advanced
    assert Path(cfg.state_file).is_file()
    assert json.loads(Path(cfg.state_file).read_text())["seen"]


def test_poll_is_idempotent_across_restarts(provider, spool_dir, tmp_path) -> None:
    provider.payloads = [payload("delivered once")]
    cfg = cfg_for(provider.url, spool_dir, tmp_path)
    spool = Spool(spool_dir).ensure()

    first_state = PollState.load(cfg.state_file)
    assert poll_once(cfg, spool, first_state) == 1

    # Simulate a restart: fresh state object, same file, provider still
    # returns the same message (many do, until you advance the cursor).
    restarted = PollState.load(cfg.state_file)
    assert poll_once(cfg, spool, restarted) == 0
    assert len(spool.paths()) == 1


def test_second_poll_picks_up_only_new_messages(provider, spool_dir, tmp_path) -> None:
    provider.payloads = [payload("old"), payload("old", "new")]
    cfg = cfg_for(provider.url, spool_dir, tmp_path)
    spool = Spool(spool_dir).ensure()
    state = PollState.load(cfg.state_file)

    assert poll_once(cfg, spool, state) == 1
    assert poll_once(cfg, spool, state) == 1
    assert [m.body for m in spool.messages()] == ["old", "new"]


def test_since_cursor_is_sent_to_the_provider(provider, spool_dir, tmp_path) -> None:
    provider.payloads = [payload("x")]
    cfg = cfg_for(provider.url, spool_dir, tmp_path, poll_since_param="since")
    spool = Spool(spool_dir).ensure()
    state = PollState.load(cfg.state_file)
    state.since = "2026-10-01T00:00:00+00:00"
    poll_once(cfg, spool, state)
    assert "since=2026-10-01T00" in provider.requests[0]["path"]


def test_since_placeholder_in_url(provider, spool_dir, tmp_path) -> None:
    provider.payloads = [payload("x")]
    cfg = cfg_for(provider.url + "?cursor={since}", spool_dir, tmp_path)
    spool = Spool(spool_dir).ensure()
    state = PollState.load(cfg.state_file)
    state.since = "2026-10-01T00:00:00+00:00"
    poll_once(cfg, spool, state)
    assert "cursor=2026-10-01T00" in provider.requests[0]["path"]


def test_dry_run_writes_nothing_but_reports(provider, spool_dir, tmp_path) -> None:
    provider.payloads = [payload("not written")]
    cfg = cfg_for(provider.url, spool_dir, tmp_path)
    spool = Spool(spool_dir).ensure()
    state = PollState.load(cfg.state_file)

    assert poll_once(cfg, spool, state, dry_run=True) == 1
    assert spool.paths() == []
    assert not Path(cfg.state_file).exists()


# ---- auth / headers ----------------------------------------------------
def test_bearer_and_basic_auth_headers(provider, spool_dir, tmp_path) -> None:
    provider.payloads = [payload("x"), payload("y")]
    spool = Spool(spool_dir).ensure()

    bearer = cfg_for(provider.url, spool_dir, tmp_path, poll_auth="bearer:t0ken")
    poll_once(bearer, spool, PollState.load(bearer.state_file))
    assert provider.requests[-1]["headers"]["authorization"] == "Bearer t0ken"

    basic = cfg_for(provider.url, spool_dir, tmp_path, poll_auth="basic:user:pa:ss")
    poll_once(basic, spool, PollState.load(basic.state_file))
    import base64

    expected = "Basic " + base64.b64encode(b"user:pa:ss").decode()
    assert provider.requests[-1]["headers"]["authorization"] == expected


def test_extra_headers_are_applied(provider, spool_dir, tmp_path) -> None:
    provider.payloads = [payload("x")]
    cfg = cfg_for(provider.url, spool_dir, tmp_path, poll_extra_headers="X-Api-Version: 2, X-Tenant: acme")
    poll_once(cfg, Spool(spool_dir).ensure(), PollState.load(cfg.state_file))
    headers = provider.requests[-1]["headers"]
    assert headers["x-api-version"] == "2"
    assert headers["x-tenant"] == "acme"


# ---- payload shapes ----------------------------------------------------
def test_dotted_json_path_selects_the_list() -> None:
    cfg = Config()
    cfg.poll_json_path = "data.messages"
    body = {"data": {"messages": [{"text": "deep"}]}}
    assert [m.body for m in messages_from_payload(body, cfg)] == ["deep"]


def test_nested_items_container_is_understood() -> None:
    cfg = Config()
    cfg.poll_json_path = "messages"
    body = {"messages": {"items": [{"text": "nested"}]}}
    assert [m.body for m in messages_from_payload(body, cfg)] == ["nested"]


def test_single_object_payload_is_accepted() -> None:
    cfg = Config()
    cfg.poll_json_path = "data"
    assert [m.body for m in messages_from_payload({"data": {"text": "solo"}}, cfg)] == ["solo"]


def test_top_level_list_is_accepted() -> None:
    cfg = Config()
    cfg.poll_json_path = "messages"
    assert [m.body for m in messages_from_payload([{"text": "flat"}], cfg)] == ["flat"]


def test_entries_without_a_body_are_skipped_not_fatal() -> None:
    cfg = Config()
    cfg.poll_json_path = "messages"
    body = {"messages": [{"from": "a"}, {"text": "kept"}, "bare string"]}
    bodies = [m.body for m in messages_from_payload(body, cfg)]
    assert bodies == ["kept", "bare string"]


def test_non_json_response_is_an_error(spool_dir, tmp_path) -> None:
    stub = StubProvider([b"<html>gateway error</html>"])
    try:
        cfg = cfg_for(stub.url, spool_dir, tmp_path)
        with pytest.raises(SpoolError):
            poll_once(cfg, Spool(spool_dir).ensure(), PollState.load(cfg.state_file))
    finally:
        stub.close()


def test_connection_failure_raises_url_error(spool_dir, tmp_path) -> None:
    import urllib.error

    cfg = cfg_for("http://127.0.0.1:1/messages", spool_dir, tmp_path)
    with pytest.raises(urllib.error.URLError):
        poll_once(cfg, Spool(spool_dir).ensure(), PollState.load(cfg.state_file))


# ---- state integrity ---------------------------------------------------
def test_state_file_is_owner_only_and_atomic(tmp_path) -> None:
    import os
    import stat

    state = PollState.load(tmp_path / "nested" / "poll.state.json")
    state.remember("k1")
    state.save()
    assert stat.S_IMODE(os.stat(state.path).st_mode) == 0o600
    assert not list(state.path.parent.glob("*.tmp"))


def test_corrupt_state_file_does_not_crash(tmp_path) -> None:
    path = tmp_path / "poll.state.json"
    path.write_text("{not json")
    state = PollState.load(path)
    assert state.seen == []
    assert state.since == ""


def test_remember_bounds_the_seen_set(tmp_path) -> None:
    state = PollState.load(tmp_path / "poll.state.json")
    for i in range(10):
        assert state.remember(f"k{i}")
    assert not state.remember("k9")
    assert state.seen[-1] == "k9"


def test_failed_write_is_retried_on_the_next_poll(provider, spool_dir, tmp_path, monkeypatch) -> None:
    """A message that cannot be written must not be marked as seen."""
    provider.payloads = [payload("needs retry")]
    cfg = cfg_for(provider.url, spool_dir, tmp_path)
    spool = Spool(spool_dir).ensure()
    state = PollState.load(cfg.state_file)

    def boom(_message):
        raise SpoolError("disk on fire")

    monkeypatch.setattr(spool, "write", boom)
    assert poll_once(cfg, spool, state) == 0
    assert state.seen == []            # not claimed
    monkeypatch.undo()

    assert poll_once(cfg, spool, state) == 1
    assert [m.body for m in spool.messages()] == ["needs retry"]


def test_dedupe_key_prefers_provider_id() -> None:
    with_id = Message(sender="a", body="x", received_at="2026-10-05T12:00:00+00:00", provider_id="p1")
    without = Message(sender="a", body="x", received_at="2026-10-05T12:00:00+00:00")
    assert dedupe_key(with_id) == "p1"
    assert dedupe_key(without).startswith("sha:")


# ---- config guards -----------------------------------------------------
def test_plain_http_to_a_remote_host_is_refused() -> None:
    cfg = Config()
    cfg.poll_url = "http://sms.example.com/messages"
    with pytest.raises(ConfigError, match="https"):
        cfg.validate()


def test_https_is_accepted() -> None:
    cfg = Config()
    cfg.poll_url = "https://sms.example.com/messages"
    cfg.validate()


def test_loopback_http_is_allowed_for_local_gateways() -> None:
    cfg = Config()
    cfg.poll_url = "http://127.0.0.1:9000/messages"
    cfg.validate()
