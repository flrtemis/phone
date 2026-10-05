#!/usr/bin/env python3
"""sms.cli - read your messages out of the spool directory.

Everything here is read-only except ``purge`` and ``export``, and every
subcommand works on the plain text files directly. No index, no lock
file, no server required: you can stop the daemon and still read mail.

    phone sms list --limit 20
    phone sms read latest
    phone sms tail --follow
    phone sms grep 'invoice|receipt' --ignore-case
    phone sms verify
    phone sms export --format jsonl --since 2026-10-01 > october.jsonl

The ``tail --follow`` implementation polls the directory and notices *new
filenames*, which is what a naive ``tail -f ~/sms/incoming/*.txt`` cannot
do: the shell expands that glob once at start-up, so files that arrive
later are never opened.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sms import __version__
from sms.config import Config, ConfigError
from sms.spool import Message, Spool, SpoolError, iso, parse_iso

USE_COLOUR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOUR else text


def bold(text: str) -> str:
    return paint(text, "1")


def dim(text: str) -> str:
    return paint(text, "2")


def cyan(text: str) -> str:
    return paint(text, "36")


def local_time(iso_text: str) -> str:
    dt = parse_iso(iso_text).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def load_spool(args: argparse.Namespace) -> Spool:
    cfg = Config.load(getattr(args, "config", None))
    if getattr(args, "spool_dir", None):
        cfg.spool_dir = args.spool_dir
    return Spool(
        cfg.spool_path,
        max_files=cfg.max_files,
        max_bytes=cfg.max_bytes,
        fsync=cfg.fsync,
        journal=cfg.journal,
    )


def select(
    spool: Spool,
    *,
    since: Optional[str] = None,
    sender: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[tuple[Message, Path]]:
    """Newest-last list of (message, path), optionally filtered."""
    rows: list[tuple[Message, Path]] = []
    since_dt = parse_iso(since) if since else None
    for path in spool.paths():
        try:
            message = Message.load(path)
        except (SpoolError, OSError):
            continue
        if since_dt and parse_iso(message.received_at) < since_dt:
            continue
        if sender and sender.lower() not in message.sender.lower():
            continue
        rows.append((message, path))
    if limit is not None:
        rows = rows[-limit:] if limit > 0 else []
    return rows


# ----------------------------------------------------------------------
# subcommands
# ----------------------------------------------------------------------
def cmd_list(args: argparse.Namespace) -> int:
    spool = load_spool(args)
    rows = select(spool, since=args.since, sender=args.sender, limit=args.limit)
    if args.json:
        for message, path in rows:
            print(
                json.dumps(
                    {
                        "id": message.id,
                        "from": message.sender,
                        "date": message.received_at,
                        "file": path.name,
                        "bytes": message.body_bytes,
                        "body": message.body,
                    },
                    ensure_ascii=False,
                )
            )
        return 0
    if not rows:
        print(dim("no messages in spool"), file=sys.stderr)
        return 0
    width = shutil.get_terminal_size((100, 24)).columns
    for message, _ in rows:
        stamp = local_time(message.received_at)
        preview = message.preview(max(20, width - 46))
        print(f"{dim(stamp)}  {cyan(message.id)}  {bold(message.sender):<20}  {preview}")
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    spool = load_spool(args)
    found = spool.by_id(args.needle)
    if not found:
        print(f"no message matching {args.needle!r}", file=sys.stderr)
        return 1
    message, path = found
    if args.raw:
        print(path.read_text(encoding="utf-8", errors="replace"), end="")
        return 0
    print(f"{bold('from')} {message.sender}   {bold('at')} {local_time(message.received_at)}   {dim(message.id)}")
    print(dim("-" * min(60, shutil.get_terminal_size((60, 24)).columns)))
    print(message.body)
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    """Show recent messages, optionally following for new arrivals.

    Everything already on disk is recorded as *seen* before the loop starts,
    so ``--last 0 --follow`` quietly waits for the next message instead of
    replaying the spool, and ``--last 5 --follow`` shows five then only new
    ones. Only filenames that appear later are ever printed as arrivals.
    """
    spool = load_spool(args)
    paths = spool.paths()
    known: set[str] = {path.name for path in paths}

    shown = paths if args.from_start else (paths[-args.last:] if args.last > 0 else [])
    for path in shown:
        try:
            print(render_entry(Message.load(path)))
        except (SpoolError, OSError):
            continue

    if not args.follow:
        return 0

    print(dim(f"--- following {spool.dir} (Ctrl-C to stop) ---"), file=sys.stderr)
    try:
        while True:
            time.sleep(args.interval)
            for path in spool.paths():
                if path.name in known:
                    continue
                known.add(path.name)
                try:
                    message = Message.load(path)
                except (SpoolError, OSError):
                    continue
                print(render_entry(message), flush=True)
    except KeyboardInterrupt:
        return 0


def render_entry(message: Message) -> str:
    flat = " ".join(message.body.split())
    return f"{dim(local_time(message.received_at))}  {cyan(message.id)}  {bold(message.sender):<20}  {flat}"


def cmd_grep(args: argparse.Namespace) -> int:
    spool = load_spool(args)
    flags = re.IGNORECASE if args.ignore_case else 0
    try:
        pattern = re.compile(args.pattern, flags)
    except re.error as exc:
        print(f"bad pattern: {exc}", file=sys.stderr)
        return 2
    hits = 0
    for message, _ in select(spool, since=args.since):
        haystack = {"from": message.sender, "body": message.body}.get(args.field, f"{message.sender}\n{message.body}")
        if pattern.search(haystack):
            hits += 1
            if args.json:
                print(json.dumps({"id": message.id, "from": message.sender, "date": message.received_at, "body": message.body}, ensure_ascii=False))
            else:
                print(f"{dim(local_time(message.received_at))}  {cyan(message.id)}  {bold(message.sender):<20}  {message.preview(80)}")
    if not args.json:
        print(dim(f"{hits} match(es)"), file=sys.stderr)
    return 0 if hits else 1


def cmd_stats(args: argparse.Namespace) -> int:
    spool = load_spool(args)
    stats = spool.stats()
    payload = {
        "dir": str(spool.dir),
        "count": stats.count,
        "bytes": stats.bytes,
        "oldest": iso(stats.oldest) if stats.oldest else None,
        "newest": iso(stats.newest) if stats.newest else None,
        "corrupt": stats.corrupt,
        "journal": spool.journal_path.is_file() if spool.journal_enabled else False,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    print(f"spool dir : {payload['dir']}")
    print(f"messages  : {stats.count}" + (f"  ({stats.corrupt} unreadable)" if stats.corrupt else ""))
    print(f"size      : {stats.bytes} bytes")
    print(f"oldest    : {local_time(payload['oldest']) if payload['oldest'] else '-'}")
    print(f"newest    : {local_time(payload['newest']) if payload['newest'] else '-'}")
    print(f"journal   : {'present' if payload['journal'] else 'absent'}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    spool = load_spool(args)
    ok, problems = spool.verify()
    if args.json:
        print(json.dumps({"ok": ok, "problems": problems}, indent=2))
    else:
        print(f"{ok} message(s) verified")
        for problem in problems:
            print(f"  ! {problem}", file=sys.stderr)
    return 0 if not problems else 1


def cmd_export(args: argparse.Namespace) -> int:
    spool = load_spool(args)
    rows = select(spool, since=args.since, sender=args.sender)

    def emit(out) -> None:
        for message, path in rows:
            if args.format == "jsonl":
                json.dump(
                    {
                        "id": message.id,
                        "from": message.sender,
                        "date": message.received_at,
                        "file": path.name,
                        "sha256": message.sha256,
                        "body": message.body,
                    },
                    out,
                    ensure_ascii=False,
                )
                out.write("\n")
            elif args.format == "txt":
                out.write(f"{local_time(message.received_at)}  {message.sender}\n{message.body}\n\n")
            elif args.format == "raw":
                out.write(path.read_text(encoding="utf-8", errors="replace"))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            emit(handle)
        print(f"wrote {len(rows)} message(s) to {args.out}", file=sys.stderr)
    else:
        emit(sys.stdout)
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    spool = load_spool(args)
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.older_than)
    victims = [path for message, path in select(spool) if parse_iso(message.received_at) < cutoff]
    if not victims:
        print("nothing to purge")
        return 0
    print(f"about to delete {len(victims)} file(s) older than {cutoff.date()}:")
    for path in victims[:10]:
        print(f"  {path.name}")
    if len(victims) > 10:
        print(f"  ... and {len(victims) - 10} more")
    if not args.yes:
        print("refusing to delete without --yes", file=sys.stderr)
        return 1
    for path in victims:
        path.unlink(missing_ok=True)
    print(f"deleted {len(victims)} file(s)")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    if args.spool_dir:
        cfg.spool_dir = args.spool_dir
    print(cfg.render())
    return 0


# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phone-sms",
        description="Read the SMS spool: plain text files, no messaging app.",
    )
    parser.add_argument("--config", help="path to sms.conf")
    parser.add_argument("--spool-dir", help="override spool directory")
    parser.add_argument("--version", action="version", version=f"phone-sms {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="list messages, oldest first")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--since", help="ISO date/time lower bound")
    p.add_argument("--from", dest="sender", help="substring match on sender")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("read", help="print one message (id, filename prefix, or 'latest')")
    p.add_argument("needle")
    p.add_argument("--raw", action="store_true", help="dump the file verbatim")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("tail", help="show recent messages and optionally follow")
    p.add_argument("--last", type=int, default=10, help="how many existing messages to print first (0 = none)")
    p.add_argument("--follow", "-f", action="store_true", help="keep watching for new messages")
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--from-start", action="store_true", help="print the entire spool before following")
    p.set_defaults(func=cmd_tail)

    p = sub.add_parser("grep", help="regex search across messages")
    p.add_argument("pattern")
    p.add_argument("--ignore-case", "-i", action="store_true")
    p.add_argument("--field", choices=["all", "from", "body"], default="all")
    p.add_argument("--since")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_grep)

    p = sub.add_parser("stats", help="counts and date range")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("verify", help="re-hash every message against its stored digest")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("export", help="dump the spool as jsonl/txt/raw")
    p.add_argument("--format", choices=["jsonl", "txt", "raw"], default="jsonl")
    p.add_argument("--since")
    p.add_argument("--from", dest="sender")
    p.add_argument("--out", help="write to a file instead of stdout")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("purge", help="delete messages older than N days")
    p.add_argument("--older-than", type=float, default=30.0)
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_purge)

    p = sub.add_parser("config", help="print effective configuration")
    p.set_defaults(func=cmd_config)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:  # `phone sms list | head`
        # Closing the stream here is courtesy: the reader is gone, and the
        # exit status should be "fine, we stopped because you stopped reading".
        try:
            sys.stdout.close()
        except OSError:  # noqa: BLE001 - nothing useful left to do
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
