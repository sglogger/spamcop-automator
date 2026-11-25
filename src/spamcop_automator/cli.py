"""Command line entry point."""

from __future__ import annotations

import argparse
import configparser
import contextlib
import logging
import os
import stat
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

from . import __version__
from .client import BASE_URL, SpamCopClient, SpamCopError

log = logging.getLogger("spamcop")

DEFAULT_CONFIG = Path(
    os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
) / "spamcop-automator" / "config.ini"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2


class ConfigError(Exception):
    pass


def load_credentials(config_path: Path | None) -> tuple[str, str]:
    """Environment variables win; otherwise read the config file."""
    username = os.environ.get("SPAMCOP_USERNAME")
    password = os.environ.get("SPAMCOP_PASSWORD")

    path = config_path or DEFAULT_CONFIG
    if not (username and password):
        if path.is_file():
            if os.name == "posix" and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                log.warning("%s is readable by other users; run: chmod 600 %s", path, path)
            parser = configparser.ConfigParser(interpolation=None)
            parser.read(path, encoding="utf-8")
            section = parser["spamcop"] if parser.has_section("spamcop") else {}
            username = username or section.get("username")
            password = password or section.get("password")
        elif config_path is not None:
            raise ConfigError(f"Config file not found: {config_path}")

    if not (username and password):
        raise ConfigError(
            "No credentials. Set SPAMCOP_USERNAME and SPAMCOP_PASSWORD, or create "
            f"{path} with a [spamcop] section containing username and password."
        )
    return username, password


@contextlib.contextmanager
def single_instance() -> Iterator[bool]:
    """Yield False if another run already holds the lock."""
    try:
        import fcntl
    except ImportError:  # not POSIX: no locking
        yield True
        return

    uid = os.getuid()
    path = Path(tempfile.gettempdir()) / f"spamcop-automator-{uid}.lock"
    with open(path, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spamcop-automator",
        description="Confirm all pending spam reports on SpamCop.net.",
    )
    parser.add_argument("-c", "--config", type=Path, help=f"config file (default: {DEFAULT_CONFIG})")
    parser.add_argument(
        "-n", "--dry-run", action="store_true", help="log in and inspect, but do not send anything"
    )
    parser.add_argument("-m", "--max-reports", type=int, metavar="N", help="stop after N reports")
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="only print skipped reports and errors (for cron)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug output")
    parser.add_argument("--log-file", type=Path, help="append log output to this file as well")
    parser.add_argument(
        "--dump-dir",
        type=Path,
        help="save every fetched page here for debugging (contains account data)",
    )
    parser.add_argument("--base-url", default=BASE_URL, help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def configure_logging(args: argparse.Namespace) -> None:
    level = logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )
    log.setLevel(level)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args)

    try:
        username, password = load_credentials(args.config)
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_CONFIG

    with single_instance() as acquired:
        if not acquired:
            log.warning("Another spamcop-automator run is still active; exiting.")
            return EXIT_OK
        try:
            with SpamCopClient(
                username,
                password,
                base_url=args.base_url,
                dry_run=args.dry_run,
                max_reports=args.max_reports,
                dump_dir=args.dump_dir,
            ) as client:
                summary = client.run()
        except SpamCopError as exc:
            log.error("%s", exc)
            return EXIT_FAILED
        except KeyboardInterrupt:
            log.error("Interrupted")
            return 130

    if summary.handled == 0 and not summary.stopped_early:
        log.info("No pending reports.")
        return EXIT_OK

    if summary.stopped_early:
        log.warning("Stopped: %s", summary.stopped_early)
    if args.dry_run:
        log.info(
            "Dry run: %d sendable, %d skipped, %d unrecognised. Nothing was sent.",
            summary.would_send,
            summary.skipped,
            summary.unknown,
        )
    else:
        log.info(
            "Done: %d sent, %d skipped, %d unrecognised.",
            summary.sent,
            summary.skipped,
            summary.unknown,
        )
    return EXIT_FAILED if summary.unknown else EXIT_OK
