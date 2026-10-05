"""Shared fixtures. No network, no real phone numbers, no external services."""

from __future__ import annotations

import http.client
import json
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sms.config import Config  # noqa: E402
from sms.daemon import make_server  # noqa: E402
from sms.spool import Spool  # noqa: E402


@pytest.fixture()
def spool_dir(tmp_path: Path) -> Path:
    return tmp_path / "incoming"


@pytest.fixture()
def spool(spool_dir: Path) -> Spool:
    return Spool(spool_dir).ensure()


def make_config(spool_dir: Path, **overrides: Any) -> Config:
    cfg = Config()
    cfg.spool_dir = str(spool_dir)
    cfg.host = "127.0.0.1"
    cfg.port = 0  # ephemeral
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


class Client:
    """Thin HTTP client with explicit control over method and headers."""

    def __init__(self, port: int, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = port

    def request(
        self,
        method: str,
        path: str,
        body: Optional[bytes | str] = None,
        headers: Optional[dict[str, str]] = None,
        *,
        raw: bool = False,
    ) -> tuple[int, dict[str, str], Any]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            payload = response.read()
            out_headers = {k.lower(): v for k, v in response.getheaders()}
            if raw:
                return response.status, out_headers, payload
            text = payload.decode("utf-8", "replace")
            try:
                return response.status, out_headers, json.loads(text) if text else None
            except json.JSONDecodeError:
                return response.status, out_headers, text
        finally:
            conn.close()

    def post_json(self, path: str, obj: Any, headers: Optional[dict[str, str]] = None) -> tuple[int, dict[str, str], Any]:
        merged = {"Content-Type": "application/json"}
        merged.update(headers or {})
        return self.request("POST", path, json.dumps(obj), merged)


@pytest.fixture()
def daemon_factory(spool_dir: Path, tmp_path: Path) -> Callable[..., tuple[Client, Config, threading.Thread]]:
    """Start a webhook daemon on an ephemeral port; yield a client."""

    started: list[Any] = []

    def _start(**overrides: Any):
        cfg = make_config(spool_dir, **overrides)
        server = make_server(cfg)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        started.append((server, thread))
        client = Client(server.server_address[1])
        return client, cfg, thread

    yield _start

    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def daemon(daemon_factory: Callable[..., Any]) -> Client:
    client, _cfg, _thread = daemon_factory()
    return client


@pytest.fixture()
def auth_daemon(daemon_factory: Callable[..., Any]) -> Client:
    client, _cfg, _thread = daemon_factory(token="s3cret-token")
    return client
