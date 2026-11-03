"""Command-line interface shared by local users and the GitHub Action."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from contextlib import redirect_stdout
from pathlib import Path

from . import __version__
from .audits import AuditRecord, publish_audits
from .config import Config
from .extract import load_project
from .sync import Synchronizer


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openmetadata-sqlmesh",
        description="Publish SQLMesh metadata and audit results to OpenMetadata",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="Synchronize model metadata and lineage")
    sync.add_argument("--project", default=".", help="SQLMesh project directory")
    sync.add_argument("--gateway", help="SQLMesh gateway to load")
    audits = commands.add_parser(
        "audit-results", help="Publish an array of explicit audit results"
    )
    audits.add_argument("results", type=Path, help="Audit result JSON file")
    for command in (sync, audits):
        command.add_argument(
            "--config", type=Path, required=True, help="OpenMetadata JSON configuration"
        )
        command.add_argument(
            "--dry-run",
            action="store_true",
            help="Emit JSON without contacting OpenMetadata",
        )
        command.add_argument(
            "--output", type=Path, help="Write JSON to this file instead of stdout"
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    config = None
    try:
        config = Config.from_file(args.config)
        if args.output and not args.output.parent.is_dir():
            raise ValueError("The output directory does not exist")
        if args.command == "sync":
            # SQLMesh status messages must not corrupt the JSON artifact.
            with redirect_stdout(sys.stderr):
                metadata = load_project(args.project, config, gateway=args.gateway)
            result = (
                metadata.to_dict(config)
                if args.dry_run
                else Synchronizer(config).sync(metadata).to_dict()
            )
        else:
            try:
                values = json.loads(args.results.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                raise ValueError("Cannot read a valid audit result JSON file") from None
            if not isinstance(values, list) or any(
                not isinstance(value, dict) for value in values
            ):
                raise ValueError("Audit input must be a JSON array of result objects")
            try:
                records = [AuditRecord(**value) for value in values]
            except TypeError:
                raise ValueError(
                    "Invalid audit result fields; model, audit, and timestamp are required"
                ) from None
            result = publish_audits(records, config, dry_run=args.dry_run)
        rendered = (
            json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        )
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
        return 1 if result.get("errors") else 0
    except Exception as exc:
        message = str(exc)
        if config and config.token:
            message = message.replace(config.token, "[REDACTED]")
        print(f"openmetadata-sqlmesh: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
