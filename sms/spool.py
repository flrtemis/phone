"""On-disk message format and spool writer/reader.

Design rules (these are deliberate; see docs/FORMAT.md):

1.  **Plain text, one message per file, self-describing.** A message is a
    short header block followed by a literal `BODY:` line and then the raw
    body bytes. `cat`, `grep`, `grep -c`, `awk` and 40-year-old shell
    tools work on it. No SQLite, no JSON blobs, no schema migrations.

2.  **The filesystem is the queue.** No broker, no daemon state beyond the
    files themselves. If the daemon dies mid-message the spool is still
    consistent, because writes are atomic (`write tmp` -> `fsync` ->
    `os.replace`).

3.  **Never trust the network for a path.** The sender string arrives from
    an untrusted gateway. It is sanitised into a bounded slug and the
    filename always carries a content-addressed suffix, so `../../etc/
    passwd` cannot escape the spool directory and two messages in the same
    second cannot overwrite each other.

4.  **Content is never written to a log.** Logs carry metadata only
    (sender, length, digest). Message bodies exist in exactly two places:
    the spool file, and your terminal.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

SPOOL_FORMAT = "1"
HEADER_RE = re.compile(r"^(?P<key>[A-Z][A-Z0-9-]{0,31}): ?(?P<value>.*)$")
SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
MAX_SLUG_LEN = 24
MAX_SENDER_LEN = 128
MAX_BODY_BYTES = 64 * 1024  # a single webhook payload has no business being bigger


class SpoolError(Exception):
    """Base class for spool failures."""


class CorruptMessageError(SpoolError):
    """A file in the spool does not parse as a message."""


class SpoolFullError(SpoolError):
    """Writing would exceed the configured spool caps (fail closed)."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime, timespec: str = "seconds") -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec=timespec)


def slugify(value: str, maxlen: int = MAX_SLUG_LEN) -> str:
    """Reduce an arbitrary untrusted string to a safe, bounded filename part.

    ``"+1 (555) 010-9999"`` -> ``"1-555-010-9999"``
    ``"../../etc/passwd"``  -> ``"etc-passwd"``
    ``"../../"``            -> ``"unknown"``

    Never returns an empty string, never returns a path separator, never
    returns a name that starts with a dot (no hidden files, no `..`).
    """
    text = unicodedata.normalize("NFKD", str(value))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = SAFE_RE.sub("-", text).strip("-.")
    if len(text) > maxlen:
        text = text[:maxlen].rstrip("-.")
    return text or "unknown"


def digest(body: str, sender: str = "", received_at: str = "") -> str:
    h = hashlib.sha256()
    for part in (sender, received_at, body):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x1f")
    return h.hexdigest()


@dataclass(frozen=True)
class Message:
    """One incoming message, already normalised."""

    sender: str
    body: str
    received_at: str  # ISO-8601 UTC, as stated by the sender/provider
    source: str = "webhook"
    provider_id: str = ""
    transport: str = "sms"
    # Local arrival time with microsecond precision, stamped by the writer.
    # Providers stamp at second resolution (and many back-fill old messages),
    # while filesystem mtime is coarse on some filesystems, so this is the
    # only reliable way to answer "which one landed first?".
    spooled_at: str = ""

    @property
    def sha256(self) -> str:
        return digest(self.body, self.sender, self.received_at)

    @property
    def id(self) -> str:
        """Stable short id derived from content (dedupe + filename suffix)."""
        return self.sha256[:12]

    @property
    def body_bytes(self) -> int:
        return len(self.body.encode("utf-8", "replace"))

    def filename(self) -> str:
        """``20261005-164501_15550109999_9f2c1a0b3d4e.txt``

        Timestamp sorts lexicographically == chronologically, the slug is
        human-readable, and the id suffix makes collisions impossible.
        """
        dt = parse_iso(self.received_at)
        stamp = dt.strftime("%Y%m%d-%H%M%S")
        return f"{stamp}_{slugify(self.sender)}_{self.id}.txt"

    def render(self) -> str:
        """Serialise to the on-disk format. Body is verbatim and last."""
        headers = [
            f"PHONE-SPOOL: {SPOOL_FORMAT}",
            f"ID: {self.id}",
            f"FROM: {self.sender}",
            f"DATE: {self.received_at}",
        ]
        if self.spooled_at:
            headers.append(f"SPOOLED: {self.spooled_at}")
        headers += [
            f"SOURCE: {self.source}",
            f"TRANSPORT: {self.transport}",
        ]
        if self.provider_id:
            headers.append(f"PROVIDER-ID: {self.provider_id}")
        headers.append(f"BYTES: {self.body_bytes}")
        headers.append(f"SHA256: {self.sha256}")
        return "\n".join(headers) + "\nBODY:\n" + self.body + "\n"

    @classmethod
    def parse(cls, text: str) -> "Message":
        """Inverse of :meth:`render`. Strict: raises on anything unexpected."""
        if not text:
            raise CorruptMessageError("empty file")
        header: dict[str, str] = {}
        lines = text.split("\n")
        idx = None
        for i, line in enumerate(lines):
            if line == "BODY:":
                idx = i
                break
            if not line:
                raise CorruptMessageError(f"blank line inside header at {i}")
            m = HEADER_RE.match(line)
            if not m:
                raise CorruptMessageError(f"unparseable header line {i}: {line[:40]!r}")
            header[m.group("key")] = m.group("value")
        if idx is None:
            raise CorruptMessageError("missing BODY: marker")
        if "FROM" not in header or "DATE" not in header:
            raise CorruptMessageError("missing FROM/DATE header")

        body = "\n".join(lines[idx + 1 :])
        if body.endswith("\n"):  # render() adds exactly one trailing newline
            body = body[:-1]
        return cls(
            sender=header["FROM"],
            body=body,
            received_at=header["DATE"],
            source=header.get("SOURCE", "unknown"),
            provider_id=header.get("PROVIDER-ID", ""),
            transport=header.get("TRANSPORT", "sms"),
            spooled_at=header.get("SPOOLED", ""),
        )

    @property
    def arrival(self) -> datetime:
        """Local arrival time when known, else the provider-stated time."""
        return parse_iso(self.spooled_at or self.received_at)

    @classmethod
    def load(cls, path: Path) -> "Message":
        return cls.parse(Path(path).read_text(encoding="utf-8", errors="replace"))

    def verify(self, text: Optional[str] = None) -> bool:
        """Check the stored digest against the stored body."""
        if text is None:
            text = self.render()
        m = HEADER_RE.search(text)
        stored = None
        for line in text.split("\n"):
            m = HEADER_RE.match(line)
            if not m:
                break
            if m.group("key") == "SHA256":
                stored = m.group("value")
        if not stored:
            return False
        return stored == self.sha256

    def preview(self, width: int = 60) -> str:
        flat = " ".join(self.body.split())
        return flat[: width - 1] + "\u2026" if len(flat) > width else flat


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z``."""
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class SpoolStats:
    count: int = 0
    bytes: int = 0
    oldest: Optional[datetime] = None
    newest: Optional[datetime] = None
    corrupt: int = 0


class Spool:
    """Read/write side of the spool directory.

    Safe for use from multiple threads in one process and from multiple
    processes: every write lands via ``os.replace`` on a uniquely named
    temp file, so readers never observe a partial message.
    """

    def __init__(
        self,
        directory: os.PathLike | str,
        *,
        max_files: int = 20_000,
        max_bytes: int = 256 * 1024 * 1024,
        fsync: bool = True,
        journal: bool = True,
    ) -> None:
        self.dir = Path(directory).expanduser()
        self.max_files = max_files
        self.max_bytes = max_bytes
        self.fsync = fsync
        self.journal_enabled = journal

    # ---- lifecycle ---------------------------------------------------
    def ensure(self) -> "Spool":
        """Create the spool directory, owner-only (0700)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        # mkdir honours umask; force it explicitly anyway.
        os.chmod(self.dir, 0o700)
        return self

    @property
    def journal_path(self) -> Path:
        return self.dir / "journal.log"

    # ---- write -------------------------------------------------------
    def write(self, msg: Message, *, now: Optional[datetime] = None) -> Path:
        """Persist ``msg`` atomically. Returns the path written."""
        if not msg.body.strip():
            raise SpoolError("refusing to spool an empty body")
        if msg.body_bytes > MAX_BODY_BYTES:
            raise SpoolError(f"body exceeds {MAX_BODY_BYTES} bytes")
        if not msg.spooled_at:
            msg = replace(msg, spooled_at=iso(utcnow(), timespec="microseconds"))
        self.ensure()
        self._check_capacity()

        target = self.dir / msg.filename()
        tmp = self.dir / f".{msg.filename()}.{os.getpid()}.tmp"
        payload = msg.render()

        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                if self.fsync:
                    os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        if self.fsync:
            self._fsync_dir()

        self._append_journal(msg, target, now=now)
        return target

    def _check_capacity(self) -> None:
        files = 0
        total = 0
        try:
            with os.scandir(self.dir) as it:
                for entry in it:
                    if entry.name.endswith(".txt") or entry.name.endswith(".tmp"):
                        files += 1
                        total += entry.stat().st_size
        except FileNotFoundError:
            return
        if files >= self.max_files:
            raise SpoolFullError(f"spool holds {files} files (cap {self.max_files})")
        if total >= self.max_bytes:
            raise SpoolFullError(f"spool holds {total} bytes (cap {self.max_bytes})")

    def _append_journal(self, msg: Message, path: Path, *, now: Optional[datetime] = None) -> None:
        """Append-only ledger: id, time, sender, digest, filename.

        Metadata only - never the body. Lets `phone sms verify` detect
        after-the-fact edits or deletions.
        """
        if not self.journal_enabled:
            return
        stamp = iso(now or utcnow())
        sender = msg.sender.replace("\t", " ").replace("\n", " ")[:MAX_SENDER_LEN]
        line = "\t".join(
            [msg.id, stamp, sender, msg.source, str(msg.body_bytes), msg.sha256, path.name]
        )
        fd = os.open(self.journal_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def _fsync_dir(self) -> None:
        try:
            fd = os.open(self.dir, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    # ---- read --------------------------------------------------------
    def paths(self) -> list[Path]:
        """Message files, oldest first (names sort chronologically)."""
        if not self.dir.is_dir():
            return []
        return sorted(
            p for p in self.dir.iterdir() if p.is_file() and p.suffix == ".txt" and not p.name.startswith(".")
        )

    def messages(self) -> Iterator[Message]:
        for path in self.paths():
            try:
                yield Message.load(path)
            except (CorruptMessageError, OSError):
                continue

    def by_id(self, needle: str) -> Optional[tuple[Message, Path]]:
        """Resolve a short id, a full filename, or the literal ``latest``."""
        paths = self.paths()
        if not paths:
            return None
        if needle in ("latest", "last", "-1"):
            path = paths[-1]
            return Message.load(path), path
        for path in reversed(paths):
            if path.name == needle or path.name.startswith(needle):
                return Message.load(path), path
            try:
                if Message.load(path).id.startswith(needle):
                    return Message.load(path), path
            except (CorruptMessageError, OSError):
                continue
        return None

    def stats(self) -> SpoolStats:
        stats = SpoolStats()
        for path in self.paths():
            try:
                msg = Message.load(path)
            except (CorruptMessageError, OSError):
                stats.corrupt += 1
                continue
            stats.count += 1
            stats.bytes += path.stat().st_size
            dt = parse_iso(msg.received_at)
            stats.oldest = dt if stats.oldest is None or dt < stats.oldest else stats.oldest
            stats.newest = dt if stats.newest is None or dt > stats.newest else stats.newest
        return stats

    def verify(self) -> tuple[int, list[str]]:
        """Return (ok_count, list_of_problems)."""
        ok = 0
        problems: list[str] = []
        for path in self.paths():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                msg = Message.parse(text)
            except (CorruptMessageError, OSError) as exc:
                problems.append(f"{path.name}: {exc}")
                continue
            if not msg.verify(text):
                problems.append(f"{path.name}: digest mismatch (file edited?)")
            else:
                ok += 1
        return ok, problems
