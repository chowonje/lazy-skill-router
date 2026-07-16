#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$project_root"

python3 -m unittest discover -s tests -p "test*.py"
python3 -m py_compile ci_relay/*.py tests/test*.py

workspace="$(mktemp -d "${TMPDIR:-/tmp}/ci-relay-demo.XXXXXX")"
trap 'rm -rf "$workspace"' EXIT
python3 -m ci_relay.cli --event fixtures/sample_ci_event.json --workspace "$workspace" >/dev/null

echo "CI Relay demo verification passed."
