#!/usr/bin/env python3
"""sms.poll - pull messages from a gateway that cannot push to you.

Some virtual-number providers will not call a webhook; they expose a
"messages since <cursor>" endpoint and expect you to ask. This poller
does exactly that, then hands each message to the same spool writer the
webhook daemon uses. Push and pull converge on one format, one directory.

Correctness properties that matter in practice:

  * **Idempotent.** Provider message ids (or a content digest) are
    remembered in a small state file, so a restart, a dropped response, or
    a repeated cursor never double-spools a message.
  * **Crash-safe state.** The cursor and seen-set are written atomically
    (tmp + ``os.replace``); losing power mid-write cannot corrupt them.
  * **Backoff, not hammering.** Failures back off exponentially with
    jitter, capped; a broken provider cannot make us DoS it (or you).
  * **TLS verified by default.** Plain HTTP is permitted only for a
    loopback gateway.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import random
import signal
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sms import __version__
from sms.config import Config, ConfigError
from sms.daemon import extract
from sms.spool import Message, Spool, SpoolError, iso, utcnow

LOG = logging.getLogger("phone.sms.poll")
MAX_SEEN = 20_000


# ----------------------------------------------------------------------
# state
# ----------------------------------------------------------------------
@dataclass
class PollState:
    """Cursor + seen-set, persisted as JSON, written atomically."""

    path: Path
    seen: list[str] = field(default_factory=list)
    since: str = ""
    last_ok: str = ""
    failures: int = 0

    @classmethod
    def load(cls, path: os.PathLike | str) -> "PollState":
        p = Path(os.path.expanduser(str(path)))
        state = cls(path=p)
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                state.seen = [str(x) for x in data.get("seen", [])][-MAX_SEEN:]
                state.since = str(data.get("since", ""))
                state.last_ok = str(data.get("last_ok", ""))
                state.failures = int(data.get("failures", 0))
            except (json.JSONDecodeError, OSError, ValueError, TypeError):
                LOG.warning("state file unreadable; starting fresh: %s", p)
        return state

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        payload = {
            "seen": self.seen[-MAX_SEEN:],
            "since": self.since,
            "last_ok": self.last_ok,
            "failures": self.failures,
        }
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def remember(self, key: str) -> bool:
        """Return True if ``key`` is new (and record it)."""
        if key in self.seen:
            return False
        self.seen.append(key)
        if len(self.seen) > MAX_SEEN:
            del self.seen[: len(self.seen) - MAX_SEEN]
        return True


# ----------------------------------------------------------------------
# provider client
# ----------------------------------------------------------------------
def dotted_get(obj: Any, path: str) -> Any:
    """``dotted_get({"data": {"m": [1]}}, "data.m") -> [1]``; '' returns obj."""
    if not path:
        return obj
    for part in path.split("."):
        if isinstance(obj, dict) and part in obj:
            obj = obj[part]
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


class ProviderClient:
    """Tiny, dependency-free HTTP client with TLS verification on."""

    def __init__(self, cfg: Config, timeout: float = 20.0) -> None:
        self.cfg = cfg
        self.timeout = timeout
        self.headers = {
            "Accept": "application/json",
            "User-Agent": "phone-sms-poll/" + __version__,
        }
        for pair in cfg.poll_extra_headers.split(",") if cfg.poll_extra_headers else []:
            if ":" in pair:
                key, value = pair.split(":", 1)
                self.headers[key.strip()] = value.strip()
        if cfg.poll_auth.startswith("bearer:"):
            self.headers["Authorization"] = "Bearer " + cfg.poll_auth.split(":", 1)[1]
        elif cfg.poll_auth.startswith("basic:"):
            creds = cfg.poll_auth.split(":", 2)[1:]
            if len(creds) == 2:
                raw = f"{creds[0]}:{creds[1]}".encode()
                self.headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()

    def fetch(self, since: str = "") -> Any:
        url = self.cfg.poll_url
        if since and "{since}" in url:
            url = url.replace("{since}", urllib.parse.quote(since, safe=""))
        elif since and self.cfg.poll_since_param:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{self.cfg.poll_since_param}={urllib.parse.quote(since, safe='')}"

        request = urllib.request.Request(url, method=self.cfg.poll_method, headers=self.headers)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
            raw = response.read(8 * 1024 * 1024)
        if not raw:
            return []
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SpoolError(f"provider returned non-JSON payload: {exc}") from exc


# ----------------------------------------------------------------------
# polling
# ----------------------------------------------------------------------
def messages_from_payload(payload: Any, cfg: Config) -> list[Message]:
    """Locate the message list inside a provider payload and map each entry."""
    items = dotted_get(payload, cfg.poll_json_path)
    if items is None:
        items = payload
    if isinstance(items, dict):
        # Some providers nest one more level, e.g. {"messages": {"items": []}}
        for key in ("items", "messages", "data", "results", "records"):
            if isinstance(items.get(key), list):
                items = items[key]
                break
        else:
            items = [items]
    if not isinstance(items, list):
        raise SpoolError("could not locate a message list in the provider payload")

    out: list[Message] = []
    for entry in items:
        if not isinstance(entry, dict):
            entry = {"text": str(entry)}
        try:
            message = extract(_lower_keys(entry), cfg)
        except SpoolError:
            continue
        out.append(message)
    return out


def _lower_keys(entry: dict[str, Any]) -> dict[str, Any]:
    return {str(k).lower(): v for k, v in entry.items()}


def dedupe_key(message: Message) -> str:
    return message.provider_id or f"sha:{message.sha256}"


def poll_once(
    cfg: Config,
    spool: Spool,
    state: PollState,
    *,
    dry_run: bool = False,
) -> int:
    """One fetch cycle. Returns the number of new messages spooled."""
    client = ProviderClient(cfg)
    payload = client.fetch(state.since)
    messages = messages_from_payload(payload, cfg)

    written = 0
    newest = state.since
    for message in messages:
        key = dedupe_key(message)
        # Only claim a message once it is genuinely on disk. Releasing a key
        # after a failed write means the next poll retries it instead of
        # dropping it forever.
        if key in state.seen:
            LOG.debug("skip duplicate %s", key)
            continue
        if dry_run:
            LOG.info("would spool %s from %s (%d bytes)", message.id, message.sender, message.body_bytes)
            written += 1
            continue
        try:
            path = spool.write(message)
        except SpoolError as exc:
            LOG.error("cannot spool %s (will retry next poll): %s", message.id, exc)
            continue
        state.remember(key)
        LOG.info("spooled %s from %s -> %s", message.id, message.sender, path.name)
        written += 1
        newest = max(newest, message.received_at)

    state.since = newest or iso(utcnow())
    state.last_ok = iso(utcnow())
    state.failures = 0
    if not dry_run:
        state.save()
    return written


def run_forever(cfg: Config, spool: Spool, state: PollState, interval: int) -> int:
    stop = threading.Event()

    def _stop(signum: int, _frame: Any) -> None:
        LOG.info("signal %s: stopping poller", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    backoff = interval
    while not stop.is_set():
        try:
            count = poll_once(cfg, spool, state)
            if count:
                LOG.info("%d new message(s)", count)
            backoff = interval
            wait = interval
        except (urllib.error.URLError, SpoolError, TimeoutError, OSError) as exc:
            state.failures += 1
            wait = min(backoff, 900)
            LOG.warning("poll failed (attempt %d): %s - retrying in %ss", state.failures, exc, wait)
            backoff = min(wait * 2, 900)
            try:
                state.save()
            except OSError:
                pass
        # jitter keeps many hosts from synchronising their requests
        stop.wait(wait * random.uniform(0.9, 1.1))
    return 0


# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phone-sms-poll",
        description="Poll an SMS gateway over HTTPS and spool messages to plain text files.",
    )
    parser.add_argument("--config", help="path to sms.conf")
    parser.add_argument("--url", help="override poll url (may contain {since})")
    parser.add_argument("--auth", help="override auth, e.g. bearer:TOKEN or basic:user:pass")
    parser.add_argument("--state-file", help="override state file path")
    parser.add_argument("--spool-dir", help="override spool directory")
    parser.add_argument("--interval", type=int, help="seconds between polls")
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument("--dry-run", action="store_true", help="report what would be spooled, write nothing")
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    parser.add_argument("--version", action="version", version=f"phone-sms-poll {__version__}")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        cfg = Config.load(args.config)
    except ConfigError as exc:
        LOG.error("%s", exc)
        return 2
    if args.url:
        cfg.poll_url = args.url
    if args.auth:
        cfg.poll_auth = args.auth
    if args.state_file:
        cfg.state_file = args.state_file
    if args.spool_dir:
        cfg.spool_dir = args.spool_dir
    if args.interval:
        cfg.poll_interval = args.interval

    if not cfg.poll_url:
        LOG.error("no poll url configured (set [poll] url in %s or pass --url)", cfg.source_path or "sms.conf")
        return 2
    try:
        cfg.validate()
    except ConfigError as exc:
        LOG.error("%s", exc)
        return 2

    spool = Spool(
        cfg.spool_path,
        max_files=cfg.max_files,
        max_bytes=cfg.max_bytes,
        fsync=cfg.fsync,
        journal=cfg.journal,
    ).ensure()
    state = PollState.load(cfg.state_file)

    if args.once or args.dry_run:
        try:
            count = poll_once(cfg, spool, state, dry_run=args.dry_run)
        except (urllib.error.URLError, SpoolError, TimeoutError, OSError) as exc:
            LOG.error("poll failed: %s", exc)
            return 1
        LOG.info("%d message(s) %s", count, "would be spooled" if args.dry_run else "spooled")
        return 0

    LOG.info("polling %s every %ss -> %s", cfg.poll_url, cfg.poll_interval, cfg.spool_path)
    return run_forever(cfg, spool, state, cfg.poll_interval)


if __name__ == "__main__":
    raise SystemExit(main())
