"""Shared fixtures. No network, no real phone numbers, no external services."""

from __future__ import annotations

import http.client
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

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


# ---------------------------------------------------------------------------
# A fake Ollama: the real HTTP protocol, no model, no network, no GPU.
# ---------------------------------------------------------------------------
class FakeOllama(BaseHTTPRequestHandler):
    models: list[dict] = [{"name": "gemma4:26b"}, {"name": "gemma4:31b"}]
    requests: list[dict] = []
    deltas: list[str] = ["Hello", " from", " your", " machine."]
    fail_with: str = ""

    def log_message(self, *args: Any) -> None:  # keep test output readable
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        if self.path == "/api/tags":
            self._json({"models": FakeOllama.models})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        FakeOllama.requests.append({"path": self.path, "payload": payload})
        if self.path == "/api/generate":
            self._json({"response": "One short answer.", "done": True})
            return
        if self.path != "/api/chat":
            self._json({"error": "not found"}, 404)
            return
        if FakeOllama.fail_with:
            self._json({"error": FakeOllama.fail_with})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for delta in FakeOllama.deltas:
            self.wfile.write((json.dumps({"message": {"content": delta}, "done": False}) + "\n").encode())
            self.wfile.flush()
        self.wfile.write((json.dumps({"message": {"content": ""}, "done": True}) + "\n").encode())


class FakeOllamaControl:
    """Handle on the fake server, so a test can change what the model 'says'."""

    def __init__(self, url: str) -> None:
        self.url = url

    @property
    def requests(self) -> list[dict]:
        """Every request the model received, in order. Do not import the class
        from a test module: pytest loads conftest.py under a different name, and
        the two module objects would each have their own list."""
        return FakeOllama.requests

    def set_deltas(self, *deltas: str) -> "FakeOllamaControl":
        FakeOllama.deltas = list(deltas)
        return self

    def fail(self, message: str) -> "FakeOllamaControl":
        FakeOllama.fail_with = message
        return self


@pytest.fixture()
def fake_ollama_url() -> Iterator[FakeOllamaControl]:
    FakeOllama.models = [{"name": "gemma4:26b"}, {"name": "gemma4:31b"}]
    FakeOllama.requests = []
    FakeOllama.deltas = ["Hello", " from", " your", " machine."]
    FakeOllama.fail_with = ""
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield FakeOllamaControl(f"http://127.0.0.1:{server.server_address[1]}")
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
