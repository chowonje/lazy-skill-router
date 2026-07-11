from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lazy_skill_router_policy_ir import parse_policy_config


@dataclass(frozen=True)
class Finding:
    severity: str
    message: str


def load_config(path: Path) -> tuple[dict[str, Any] | None, list[Finding]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except FileNotFoundError:
        return None, [Finding("ERROR", f"file not found: {path}")]
    except json.JSONDecodeError as exc:
        return None, [Finding("ERROR", f"invalid JSON: {exc}")]
    except OSError as exc:
        return None, [Finding("ERROR", f"failed to read file: {exc}")]
    if not isinstance(loaded, dict):
        return None, [Finding("ERROR", "config root must be an object")]
    return loaded, []


def validate_config(config: dict[str, Any]) -> list[Finding]:
    return [Finding(item.severity, item.message) for item in parse_policy_config(config).findings]


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a lazy-skill-router routes JSON file.")
    parser.add_argument("config", help="Path to routes JSON.")
    args = parser.parse_args()

    config, load_findings = load_config(Path(args.config).expanduser())
    findings = load_findings if config is None else validate_config(config)
    for finding in findings:
        print(f"{finding.severity}: {finding.message}")
    if any(finding.severity == "ERROR" for finding in findings):
        return 1
    print("OK: route config is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
