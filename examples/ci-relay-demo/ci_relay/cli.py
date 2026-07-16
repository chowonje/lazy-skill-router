"""Command-line entrypoint for the local CI relay demo."""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Optional

from .service import process_event


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Process one local CI event")
    parser.add_argument("--event", required=True, type=Path, help="Path to a JSON event")
    parser.add_argument("--workspace", required=True, type=Path, help="Local output directory")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = json.loads(args.event.read_text(encoding="utf-8"))
        result = process_event(payload, args.workspace, lambda message: print(message, file=sys.stderr))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ci-relay: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
